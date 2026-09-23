from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import askd_rpc
import askd_runtime
import ccb_runtime_status
import peer_routing
import pytest
from ccb_runtime_status import ProviderRuntimeStatus


ROOT = Path(__file__).resolve().parents[1]


def _load_ask_module():
    path = ROOT / "bin" / "ask"
    loader = importlib.machinery.SourceFileLoader("ask_client_flags_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _provider_status(
    *,
    mounted: bool,
    daemon_online: bool = True,
    pane_id: str = "",
) -> ProviderRuntimeStatus:
    return ProviderRuntimeStatus(
        key="codex",
        provider="codex",
        capable=True,
        configured=mounted,
        registered=mounted,
        pane_alive=mounted,
        session_bound=mounted,
        daemon_online=daemon_online,
        mounted=mounted,
        reason="" if mounted else "not_configured",
        pane_id=pane_id,
    )


def _registry_record(ask, work_dir: Path, provider: str, pane_id: str = "%1"):
    return ask.RegistryProviderRecord(
        project_id=ask.compute_ccb_project_id(work_dir),
        work_dir=str(work_dir),
        provider=provider,
        provider_entry={"pane_id": pane_id},
        registry_record={"work_dir": str(work_dir)},
        updated_at=1,
        timestamp_stale=False,
    )


def _capture_unified_request(monkeypatch, tmp_path: Path, *, show_tier_env: str | None) -> dict:
    ask = _load_ask_module()
    sent: dict = {}
    state_file = tmp_path / "askd.json"

    if show_tier_env is None:
        monkeypatch.delenv("CCB_CODEX_SHOW_TIER", raising=False)
    else:
        monkeypatch.setenv("CCB_CODEX_SHOW_TIER", show_tier_env)
    monkeypatch.setattr(
        askd_rpc,
        "read_state",
        lambda _path: {"host": "127.0.0.1", "port": 31337, "token": "tok", "work_dir": str(tmp_path)},
    )
    monkeypatch.setattr(
        askd_rpc,
        "request_daemon",
        lambda _state, request, **_kwargs: sent.update(request) or {"exit_code": 0, "reply": ""},
    )
    monkeypatch.setattr(ask, "_find_running_unified_state_file", lambda **_kwargs: state_file)
    monkeypatch.setattr(ask, "_maybe_start_unified_daemon", lambda: False)
    monkeypatch.setattr(ask, "_caller_pane_info", lambda: ("%1", "tmux"))

    rc = ask._send_via_unified_daemon("codex", "hello", 1.0, False, "claude")

    assert rc == 0
    return sent


def test_unified_daemon_request_forwards_show_tier_env(monkeypatch, tmp_path: Path) -> None:
    sent = _capture_unified_request(monkeypatch, tmp_path, show_tier_env="1")

    assert sent["show_tier"] is True


def test_unified_daemon_request_omits_show_tier_by_default(monkeypatch, tmp_path: Path) -> None:
    sent = _capture_unified_request(monkeypatch, tmp_path, show_tier_env=None)

    assert "show_tier" not in sent


def test_caller_pane_prefers_active_tmux_over_outer_wezterm(monkeypatch) -> None:
    ask = _load_ask_module()
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setenv("WEZTERM_PANE", "outer-pane")

    assert ask._caller_pane_info() == ("%7", "tmux")


def test_unified_daemon_uses_authenticated_route_caller_over_ambient_pane(
    monkeypatch, tmp_path: Path
) -> None:
    ask = _load_ask_module()
    sent: dict = {}
    context = ask._UnifiedDaemonContext(
        tmp_path / "askd.json", {"token": "tok"}, tmp_path
    )
    monkeypatch.setattr(ask, "_caller_pane_info", lambda: ("outer-pane", "wezterm"))
    monkeypatch.setattr(
        askd_rpc,
        "request_daemon",
        lambda _state, request, **_kwargs: sent.update(request) or {"exit_code": 0, "reply": ""},
    )
    route = ask.ResolvedRoute(
        live_id="s2",
        launch_id="ai-1",
        caller_live_id="s1",
        caller_token="secret",
        caller_pane_id="%2",
        caller_terminal="tmux",
        pane_id="%3",
        terminal="tmux",
        session_file=str(tmp_path / "s2.json"),
        ccb_project_id="project-1",
    )

    rc = ask._send_via_unified_daemon(
        "codex", "hello", 1.0, False, "codex", daemon_context=context, route=route
    )

    assert rc == 0
    assert sent["caller_pane_id"] == "%2"
    assert sent["caller_terminal"] == "tmux"


def test_unified_daemon_preserves_outer_async_request_id(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CCB_REQ_ID", "20260711-220118-845-190737")

    sent = _capture_unified_request(monkeypatch, tmp_path, show_tier_env=None)

    assert sent["req_id"] == "20260711-220118-845-190737"


def test_unified_daemon_forwards_delivery_only_flags(monkeypatch, tmp_path: Path) -> None:
    ask = _load_ask_module()
    sent: dict = {}
    state_file = tmp_path / "askd.json"
    monkeypatch.setattr(
        askd_rpc,
        "read_state",
        lambda _path: {"host": "127.0.0.1", "port": 31337, "token": "tok", "work_dir": str(tmp_path)},
    )
    monkeypatch.setattr(
        askd_rpc,
        "request_daemon",
        lambda _state, request, **_kwargs: sent.update(request) or {"exit_code": 0, "reply": ""},
    )
    monkeypatch.setattr(ask, "_find_running_unified_state_file", lambda **_kwargs: state_file)
    monkeypatch.setattr(ask, "_caller_pane_info", lambda: ("5", "wezterm"))

    rc = ask._send_via_unified_daemon(
        "claude",
        "FYI",
        1.0,
        False,
        "codex",
        delivery_only=True,
        suppress_completion_hook=True,
    )

    assert rc == 0
    assert sent["delivery_only"] is True
    assert sent["suppress_completion_hook"] is True


def test_claude_notify_uses_unified_delivery_only_transport(monkeypatch) -> None:
    ask = _load_ask_module()
    captured: dict = {}
    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, Path.cwd())
    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: True)
    monkeypatch.setattr(ask, "_resolve_unified_daemon_context", lambda: context)
    monkeypatch.setattr(ask, "_preflight_target", lambda _provider, **_kwargs: True)
    monkeypatch.setattr(
        ask,
        "_send_via_unified_daemon",
        lambda provider, message, timeout, no_wrap, caller, **kwargs: captured.update(
            provider=provider,
            message=message,
            caller=caller,
            kwargs=kwargs,
        ) or 0,
    )

    rc = ask.main(["ask", "claude", "--notify", "FYI"])

    assert rc == 0
    assert captured["provider"] == "claude"
    assert captured["caller"] == "codex"
    # Ruling: notify mode must carry the route too. `_preflight_target` is
    # mocked here (no inventory involved), so the carried route is the
    # empty ResolvedRoute -- but it must still be present in the call.
    assert captured["kwargs"] == {
        "delivery_only": True,
        "suppress_completion_hook": True,
        "daemon_context": context,
        "route": ask.ResolvedRoute(),
    }


def test_unified_state_discovery_uses_cwd_project_when_run_dir_missing(monkeypatch, tmp_path: Path) -> None:
    ask = _load_ask_module()
    work_dir = tmp_path / "project"
    (work_dir / ".ccb").mkdir(parents=True)
    monkeypatch.chdir(work_dir)
    monkeypatch.delenv("CCB_RUN_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    expected = askd_runtime.state_file_candidates("askd.json", work_dir=work_dir)[0]
    seen: list[Path] = []

    def _ping(*, timeout_s: float, state_file: Path) -> bool:
        assert timeout_s == 0.5
        seen.append(state_file)
        return state_file == expected

    monkeypatch.setattr(askd_rpc, "ping_daemon", lambda _prefix, timeout_s, state_file: _ping(
        timeout_s=timeout_s,
        state_file=state_file,
    ))

    assert ask._find_running_unified_state_file() == expected
    assert seen == [expected]
    assert expected.parent.parent.name == "projects"


def test_preflight_reports_runtime_proxy_failure(monkeypatch, capsys) -> None:
    ask = _load_ask_module()
    monkeypatch.setattr(
        ask,
        "provider_status_for_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("host status unavailable")),
    )

    assert ask._preflight_target("claude") is False
    assert "Provider runtime status unavailable: host status unavailable" in capsys.readouterr().err


def test_foreground_ask_uses_one_daemon_project_context_from_subdirectory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ask = _load_ask_module()
    project = tmp_path / "project"
    subdir = project / "nested"
    run_dir = tmp_path / "run"
    subdir.mkdir(parents=True)
    run_dir.mkdir()
    state_file = run_dir / "askd.json"
    state = {"token": "tok", "work_dir": str(project)}
    find_calls: list[dict] = []
    preflight_dirs: list[Path] = []
    statuses: list[ProviderRuntimeStatus] = []
    sent: dict = {}

    monkeypatch.chdir(subdir)
    monkeypatch.setenv("CCB_RUN_DIR", str(run_dir))
    monkeypatch.setenv("CCB_CALLER", "claude")
    monkeypatch.setattr(
        ask,
        "_find_running_unified_state_file",
        lambda **kwargs: find_calls.append(kwargs) or state_file,
    )
    monkeypatch.setattr(askd_rpc, "read_state", lambda _path: state)

    def _status(_provider: str, *, work_dir: Path) -> ProviderRuntimeStatus:
        preflight_dirs.append(Path(work_dir))
        status = _provider_status(mounted=True, daemon_online=True)
        statuses.append(status)
        return status

    monkeypatch.setattr(ask, "provider_status_for_target", _status)
    monkeypatch.setattr(
        askd_rpc,
        "request_daemon",
        lambda _state, request, **_kwargs: sent.update(request) or {"exit_code": 0, "reply": ""},
    )
    monkeypatch.setattr(ask, "_caller_pane_info", lambda: ("9", "wezterm"))

    rc = ask.main(["ask", "codex", "--foreground", "hello"])

    assert rc == 0
    assert find_calls == [{}]
    assert preflight_dirs == [project]
    assert statuses[0].daemon_online is True
    assert sent["work_dir"] == str(project)


def test_foreground_ask_rejects_genuinely_unmounted_provider(monkeypatch, tmp_path: Path, capsys) -> None:
    ask = _load_ask_module()
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    project.mkdir()
    run_dir.mkdir()
    state_file = run_dir / "askd.json"
    sent: list[dict] = []

    monkeypatch.chdir(project)
    monkeypatch.setenv("CCB_RUN_DIR", str(run_dir))
    monkeypatch.setenv("CCB_CALLER", "claude")
    monkeypatch.setattr(ask, "_find_running_unified_state_file", lambda **_kwargs: state_file)
    monkeypatch.setattr(
        askd_rpc,
        "read_state",
        lambda _path: {"token": "tok", "work_dir": str(project)},
    )
    monkeypatch.setattr(
        ask,
        "provider_status_for_target",
        lambda *_args, **_kwargs: _provider_status(mounted=False, daemon_online=True),
    )
    monkeypatch.setattr(askd_rpc, "request_daemon", lambda _state, request, **_kwargs: sent.append(request))

    rc = ask.main(["ask", "codex", "--foreground", "hello"])

    assert rc == 1
    assert sent == []
    error = capsys.readouterr().err
    assert "CCB_ROUTE_ERROR target=codex reason=not_mounted" in error
    assert "daemon_online=true" in error


def test_foreground_ask_without_managed_run_dir_falls_back_to_project_cwd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ask = _load_ask_module()
    project = tmp_path / "project"
    project.mkdir()
    state_file = tmp_path / "askd.json"
    preflight_dirs: list[Path] = []
    sent: dict = {}

    monkeypatch.chdir(project)
    monkeypatch.delenv("CCB_RUN_DIR", raising=False)
    monkeypatch.setenv("CCB_CALLER", "manual")
    monkeypatch.setattr(ask, "_find_running_unified_state_file", lambda **_kwargs: state_file)
    monkeypatch.setattr(askd_rpc, "read_state", lambda _path: {"token": "tok"})

    def _status(_provider: str, *, work_dir: Path) -> ProviderRuntimeStatus:
        preflight_dirs.append(Path(work_dir))
        return _provider_status(mounted=True, daemon_online=True)

    monkeypatch.setattr(ask, "provider_status_for_target", _status)
    monkeypatch.setattr(
        askd_rpc,
        "request_daemon",
        lambda _state, request, **_kwargs: sent.update(request) or {"exit_code": 0, "reply": ""},
    )
    monkeypatch.setattr(ask, "_caller_pane_info", lambda: ("", ""))

    rc = ask.main(["ask", "codex", "--foreground", "hello"])

    assert rc == 0
    assert preflight_dirs == [project]
    assert sent["work_dir"] == str(project)


def test_ask_rejects_provider_instances_before_dispatch(capsys) -> None:
    ask = _load_ask_module()

    rc = ask.main(["ask", "codex:worker", "hello"])

    assert rc == 1
    assert "Provider instances are no longer supported" in capsys.readouterr().err


def test_ask_rejects_removed_route_tags_before_dispatch(capsys) -> None:
    ask = _load_ask_module()

    rc = ask.main(["ask", "codex", "--foreground", "[WORKER] hello"])

    assert rc == 1
    assert "routing tags are no longer supported" in capsys.readouterr().err


def test_peer_notify_uses_one_way_foreground_delivery(monkeypatch) -> None:
    ask = _load_ask_module()
    captured: dict = {}

    def _capture(
        target: str,
        provider: str,
        timeout: float,
        message: str,
        foreground: bool,
        intent: str,
        reply_to: str,
        *,
        live_id: str = "",
    ) -> int:
        captured.update(
            target=target,
            provider=provider,
            timeout=timeout,
            message=message,
            foreground=foreground,
            intent=intent,
            reply_to=reply_to,
        )
        return 0

    monkeypatch.setattr(ask, "_run_peer_bridge", _capture)

    rc = ask._handle_peer_mode(["--peer", "/tmp/peer", "--notify", "result"])

    assert rc == 0
    assert captured == {
        "target": "/tmp/peer",
        "provider": "claude",
        "timeout": 3600.0,
        "message": "result",
        "foreground": True,
        "intent": "notify",
        "reply_to": "",
    }


def test_peer_notify_rejects_direct_question(monkeypatch, capsys) -> None:
    ask = _load_ask_module()
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge",
        lambda *_args: (_ for _ in ()).throw(AssertionError("contradictory notify must not send")),
    )

    rc = ask._handle_peer_mode(["--peer", "/tmp/peer", "--notify", "Can you confirm?"])

    assert rc == 1
    assert "use --background or --wait" in capsys.readouterr().err


def test_peer_reply_to_is_forwarded(monkeypatch) -> None:
    ask = _load_ask_module()
    captured: dict = {}
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge",
        lambda target, provider, timeout, message, foreground, intent, reply_to, **_kwargs: captured.update(
            target=target,
            provider=provider,
            intent=intent,
            reply_to=reply_to,
        ) or 0,
    )

    rc = ask._handle_peer_mode(
        ["--peer", "/tmp/peer", "--notify", "--reply-to", "20260711-212112-453-72347", "Done."]
    )

    assert rc == 0
    assert captured["intent"] == "notify"
    assert captured["reply_to"] == "20260711-212112-453-72347"


def test_codex_peer_notify_runs_in_background_and_preserves_provider(monkeypatch) -> None:
    ask = _load_ask_module()
    captured: dict = {}
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge",
        lambda target, provider, timeout, message, foreground, intent, reply_to, **_kwargs: captured.update(
            target=target,
            provider=provider,
            foreground=foreground,
            intent=intent,
        ) or 0,
    )

    rc = ask.main(["ask", "codex", "--peer", "/tmp/peer", "--notify", "FYI"])

    assert rc == 0
    assert captured == {
        "target": "/tmp/peer",
        "provider": "codex",
        "foreground": False,
        "intent": "notify",
    }


def test_peer_routing_rejects_unsupported_provider(capsys) -> None:
    ask = _load_ask_module()

    rc = ask.main(["ask", "gemini", "--peer", "/tmp/peer", "hello"])

    assert rc == 1
    assert "does not support cross-project peer routing: gemini" in capsys.readouterr().err


def test_sender_work_dir_prefers_validated_environment(monkeypatch, tmp_path: Path) -> None:
    ask = _load_ask_module()
    project = tmp_path / "project"
    project.mkdir()
    record = _registry_record(ask, project, "claude")

    monkeypatch.setenv("CCB_WORK_DIR", str(project))
    monkeypatch.setattr(ask, "resolve_daemon_work_dir", lambda: project)
    monkeypatch.setattr(ask, "iter_registry_provider_records", lambda **_kwargs: [record])
    monkeypatch.setattr(
        ask,
        "provider_status_for_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("host sender validation must stay on the direct registry path")
        ),
    )
    monkeypatch.setattr(ask, "_peer_caller_pane_info", lambda: ("%1", "tmux"))

    assert ask._resolve_sender_work_dir("claude") == project.resolve()


def test_sender_work_dir_falls_back_from_invalid_environment_to_daemon(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ask = _load_ask_module()
    invalid = tmp_path / "unmounted"
    daemon_project = tmp_path / "daemon-project"
    invalid.mkdir()
    daemon_project.mkdir()
    record = _registry_record(ask, daemon_project, "codex", pane_id="7")

    monkeypatch.setenv("CCB_WORK_DIR", str(invalid))
    monkeypatch.setattr(ask, "resolve_daemon_work_dir", lambda: daemon_project)
    monkeypatch.setattr(ask, "iter_registry_provider_records", lambda **_kwargs: [record])
    monkeypatch.setattr(ask, "_peer_caller_pane_info", lambda: ("7", "wezterm"))

    assert ask._resolve_sender_work_dir("codex") == daemon_project.resolve()


def test_sender_work_dir_rejects_missing_or_stale_project(monkeypatch, tmp_path: Path) -> None:
    ask = _load_ask_module()
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.chdir(project)
    monkeypatch.delenv("CCB_WORK_DIR", raising=False)
    monkeypatch.setattr(ask, "resolve_daemon_work_dir", lambda: project)
    monkeypatch.setattr(ask, "iter_registry_provider_records", lambda **_kwargs: [])

    resolution = ask._resolve_sender_work_dir("claude")

    assert resolution == ask._SenderWorkDirFailure("unmounted_sender", project.resolve())


def test_sender_work_dir_uses_daemon_proxy_when_sandbox_cannot_see_host_pid(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ask = _load_ask_module()
    monkeypatch.setattr(ask, "identify_sender", lambda *a, **k: peer_routing.SenderResolution(error=peer_routing.NO_CANDIDATES))
    scratch = tmp_path / "scratch"
    project = tmp_path / "project"
    scratch.mkdir()
    project.mkdir()

    monkeypatch.chdir(scratch)
    monkeypatch.delenv("CCB_WORK_DIR", raising=False)
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setenv("CCB_MANAGED", "1")
    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setattr(ask, "resolve_daemon_work_dir", lambda: project)
    monkeypatch.setattr(ask, "iter_registry_provider_records", lambda **_kwargs: [])
    monkeypatch.setattr(
        ask,
        "provider_status_for_target",
        lambda provider, **kwargs: (
            _provider_status(mounted=True, pane_id="8")
            if provider == "codex" and kwargs.get("work_dir") == project.resolve()
            else _provider_status(mounted=False)
        ),
    )
    monkeypatch.setattr(ask, "_peer_caller_pane_info", lambda: ("8", "tmux"))

    assert ask._resolve_sender_work_dir("codex") == project.resolve()


def test_sender_work_dir_rejects_sandbox_sender_when_daemon_proxy_reports_unmounted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ask = _load_ask_module()
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.delenv("CCB_WORK_DIR", raising=False)
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setenv("CCB_MANAGED", "1")
    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setattr(ask, "resolve_daemon_work_dir", lambda: project)
    monkeypatch.setattr(ask, "iter_registry_provider_records", lambda **_kwargs: [])
    monkeypatch.setattr(
        ask,
        "provider_status_for_target",
        lambda *_args, **_kwargs: _provider_status(mounted=False),
    )

    assert ask._resolve_sender_work_dir("codex") == ask._SenderWorkDirFailure(
        "unmounted_sender",
        project.resolve(),
    )


def test_sender_work_dir_rejects_sandbox_daemon_pane_mismatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ask = _load_ask_module()
    monkeypatch.setattr(ask, "identify_sender", lambda *a, **k: peer_routing.SenderResolution(error=peer_routing.NO_CANDIDATES))
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.delenv("CCB_WORK_DIR", raising=False)
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setenv("CCB_MANAGED", "1")
    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setattr(ask, "resolve_daemon_work_dir", lambda: project)
    monkeypatch.setattr(ask, "iter_registry_provider_records", lambda **_kwargs: [])
    monkeypatch.setattr(
        ask,
        "provider_status_for_target",
        lambda *_args, **_kwargs: _provider_status(mounted=True, pane_id="9"),
    )
    monkeypatch.setattr(ask, "_peer_caller_pane_info", lambda: ("8", "wezterm"))

    assert ask._resolve_sender_work_dir("codex") == ask._SenderWorkDirFailure(
        "sender_project_mismatch",
        project.resolve(),
    )


def test_sender_work_dir_rejects_environment_daemon_disagreement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ask = _load_ask_module()
    env_project = tmp_path / "env-project"
    daemon_project = tmp_path / "daemon-project"
    env_project.mkdir()
    daemon_project.mkdir()
    records = [
        _registry_record(ask, env_project, "claude"),
        _registry_record(ask, daemon_project, "claude"),
    ]

    monkeypatch.setenv("CCB_WORK_DIR", str(env_project))
    monkeypatch.setattr(ask, "resolve_daemon_work_dir", lambda: daemon_project)
    monkeypatch.setattr(ask, "iter_registry_provider_records", lambda **_kwargs: records)
    monkeypatch.setattr(ask, "_peer_caller_pane_info", lambda: ("%1", "tmux"))

    resolution = ask._resolve_sender_work_dir("claude")

    assert resolution == ask._SenderWorkDirFailure(
        "sender_project_mismatch",
        env_project.resolve(),
        daemon_project.resolve(),
    )


def test_sender_work_dir_accepts_one_of_two_live_provider_sessions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Task 2: a sender that is one of TWO live sessions of its provider in
    the same project must be identified as itself (by its own exact pane),
    not rejected because only one record happens to be checked."""
    ask = _load_ask_module()
    project = tmp_path / "project"
    project.mkdir()
    first = _registry_record(ask, project, "codex", pane_id="%1")
    second = _registry_record(ask, project, "codex", pane_id="%2")

    monkeypatch.setenv("CCB_WORK_DIR", str(project))
    monkeypatch.setattr(ask, "resolve_daemon_work_dir", lambda: project)
    monkeypatch.setattr(ask, "iter_registry_provider_records", lambda **_kwargs: [first, second])
    # The caller is running in the SECOND session's pane, not the first.
    monkeypatch.setattr(ask, "_peer_caller_pane_info", lambda: ("%2", "tmux"))

    assert ask._resolve_sender_work_dir("codex") == project.resolve()


def test_sender_work_dir_accepts_sandboxed_sibling_session(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Task 2 / Item 3: inside a managed Codex sandbox,
    `provider_status_for_target` collapses every live session of a
    provider down to ONE record and reports the provider "ambiguous" the
    instant two live sessions exist. A sandboxed sender actually running
    in a DIFFERENT, equally live sibling session must still be accepted --
    identified as itself, validated against its OWN operational
    availability via `identify_sender`, not rejected just because the
    provider-wide aggregate collapsed to (or excluded) some other pane."""
    ask = _load_ask_module()
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.delenv("CCB_WORK_DIR", raising=False)
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setenv("CCB_MANAGED", "1")
    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setattr(ask, "resolve_daemon_work_dir", lambda: project)
    monkeypatch.setattr(ask, "iter_registry_provider_records", lambda **_kwargs: [])
    monkeypatch.setattr(
        ask,
        "provider_status_for_target",
        lambda *_args, **_kwargs: _provider_status(mounted=False, pane_id=""),
    )
    monkeypatch.setattr(
        ask,
        "identify_sender",
        lambda *_a, **_k: ask.peer_routing.SenderResolution(
            candidate=ask.peer_routing.SenderCandidate(
                launch_id="ai-2-200",
                live_id="live-2",
                provider="codex",
                pane_id="8",
                terminal="tmux",
                session_file="",
                ccb_project_id="proj",
                work_dir=str(project),
            )
        ),
    )
    monkeypatch.setattr(ask, "_peer_caller_pane_info", lambda: ("8", "tmux"))

    assert ask._resolve_sender_work_dir("codex") == project.resolve()


@pytest.mark.parametrize("pane_id", ["%9", ""])
def test_peer_sender_empty_inventory_never_allows_legacy(monkeypatch, tmp_path, pane_id):
    record = {
        "ccb_session_id": "launch", "work_dir": str(tmp_path),
        "live_sessions": [], "providers": {"codex": {"pane_id": "%9"}},
    }
    monkeypatch.setattr(
        ccb_runtime_status, "_iter_qualifying_registry_records",
        lambda **kwargs: [(record, str(tmp_path), "project", 1, False)],
    )
    result = peer_routing.identify_sender_host(tmp_path, "codex", pane_id=pane_id)
    assert not result.ok
    assert result.error != peer_routing.NO_CANDIDATES


def test_peer_sender_rpc_failure_never_allows_legacy(monkeypatch, tmp_path):
    ask = _load_ask_module()
    def unavailable(*args, **kwargs):
        raise RuntimeError("host unavailable")
    monkeypatch.setattr(ask, "identify_sender", unavailable)
    monkeypatch.setattr(ask, "_active_sender_records", lambda *args: pytest.fail("legacy fallback"))
    assert not ask._sender_candidate_evidence(tmp_path, "codex").valid


def test_identify_sender_finds_itself_in_single_record_two_codex_inventory(
    monkeypatch, tmp_path: Path
) -> None:
    """Item 1/3: the case this feature exists for -- ONE registry record
    whose OWN `live_sessions` inventory names TWO Codex sessions (not two
    separate registry records). The sender running in the SECOND one must
    be identified as itself and validated against its own operational
    availability, never confused with or blocked by its sibling."""
    project = tmp_path / "project"
    project.mkdir()
    record = {
        "ccb_session_id": "ai-dup",
        "work_dir": str(project),
        "ccb_project_id": "proj",
        "terminal": "tmux",
        "live_sessions": [
            {"live_id": "l1", "provider": "codex", "pane_id": "%1", "terminal": "tmux"},
            {"live_id": "l2", "provider": "codex", "pane_id": "%2", "terminal": "tmux"},
        ],
    }
    monkeypatch.setattr(
        ccb_runtime_status,
        "_iter_qualifying_registry_records",
        lambda **_kwargs: [(record, str(project), "proj", 1, False)],
    )
    monkeypatch.setattr(ccb_runtime_status, "_operational_refusal", lambda *_a, **_k: None)

    resolution = peer_routing.identify_sender_host(project, "codex", pane_id="%2", terminal="tmux")

    assert resolution.ok
    assert resolution.candidate.pane_id == "%2"
    assert resolution.candidate.launch_id == "ai-dup"


def test_identify_sender_refuses_when_inventory_invalid_beside_healthy_legacy_map(
    monkeypatch, tmp_path: Path
) -> None:
    """Item 1: a record whose `live_sessions` key is PRESENT but malformed
    must refuse -- never quietly fall back to the healthy-looking legacy
    `providers` map sitting right beside it."""
    project = tmp_path / "project"
    project.mkdir()
    record = {
        "ccb_session_id": "ai-broken",
        "work_dir": str(project),
        "ccb_project_id": "proj",
        "terminal": "tmux",
        "live_sessions": [{"provider": "codex"}],  # missing live_id -> malformed
        "providers": {"codex": {"pane_id": "%9", "pane_title_marker": "CCB-Codex"}},
    }
    monkeypatch.setattr(
        ccb_runtime_status,
        "_iter_qualifying_registry_records",
        lambda **_kwargs: [(record, str(project), "proj", 1, False)],
    )

    resolution = peer_routing.identify_sender_host(project, "codex", pane_id="%9", terminal="tmux")

    assert not resolution.ok
    assert resolution.error == peer_routing.INVALID_INVENTORY


@pytest.mark.parametrize("foreground", [True, False])
def test_peer_sender_rejection_precedes_foreground_or_background_dispatch(
    monkeypatch,
    tmp_path: Path,
    capsys,
    foreground: bool,
) -> None:
    ask = _load_ask_module()
    rejected = tmp_path / "unmounted"
    rejected.mkdir()

    monkeypatch.setenv("CCB_CALLER", "claude")
    monkeypatch.setattr(
        ask,
        "_resolve_sender_work_dir",
        lambda _caller: ask._SenderWorkDirFailure("unmounted_sender", rejected),
    )
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge_foreground",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("foreground bridge dispatched")),
    )
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge_background",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("background receipt created")),
    )

    rc = ask._run_peer_bridge("/peer", "claude", 10.0, "hello", foreground, "wait")

    assert rc == ask.EXIT_ERROR
    error = capsys.readouterr().err
    assert "CCB_ROUTE_ERROR target=claude reason=unmounted_sender" in error
    assert f"sender_work_dir={rejected}" in error
    assert "caller=claude provider=claude" in error


@pytest.mark.parametrize("foreground", [True, False])
def test_peer_foreground_and_background_receive_same_resolved_sender(
    monkeypatch,
    tmp_path: Path,
    foreground: bool,
) -> None:
    ask = _load_ask_module()
    project = tmp_path / "project"
    project.mkdir()
    captured: list[Path] = []

    monkeypatch.setenv("CCB_CALLER", "claude")
    monkeypatch.setattr(ask, "_resolve_sender_work_dir", lambda _caller: project.resolve())
    monkeypatch.setattr(ask, "inside_managed_codex_sandbox", lambda: False)
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge_foreground",
        lambda *_args, **_kwargs: captured.append(_args[-1]) or 0,
    )
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge_background",
        lambda *_args, **_kwargs: captured.append(_args[-1]) or 0,
    )

    assert ask._run_peer_bridge("/peer", "claude", 10.0, "hello", foreground, "wait") == 0
    assert captured == [project.resolve()]


def test_peer_sandbox_forces_foreground_instead_of_detached_worker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ask = _load_ask_module()
    project = tmp_path / "project"
    project.mkdir()
    captured: list[tuple[str, Path]] = []

    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setattr(ask, "_resolve_sender_work_dir", lambda _caller: project.resolve())
    monkeypatch.setattr(ask, "inside_managed_codex_sandbox", lambda: True)
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge_foreground",
        lambda *_args, **_kwargs: captured.append((_args[-3], _args[-1])) or 0,
    )
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge_background",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("sandbox spawned detached worker")),
    )

    assert ask._run_peer_bridge("/peer", "claude", 10.0, "hello", False, "background") == 0
    assert captured == [("background", project.resolve())]


def test_peer_sandbox_foreground_wait_creates_correlated_receipt(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    ask = _load_ask_module()
    project = tmp_path / "project"
    project.mkdir()
    captured: dict = {}

    class _Result:
        returncode = 0

    def _receipt(**kwargs):
        captured["receipt_kwargs"] = kwargs
        return {"task_id": kwargs["task_id"]}

    def _run(cmd, **kwargs):
        captured.update(cmd=cmd, run_kwargs=kwargs)
        return _Result()

    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setattr(ask.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ask, "make_task_id", lambda: "task-fixed")
    monkeypatch.setattr(ask, "_cleanup_task_logs", lambda _path: None)
    monkeypatch.setattr(ask, "_resolve_sender_work_dir", lambda _caller: project.resolve())
    monkeypatch.setattr(ask, "inside_managed_codex_sandbox", lambda: True)
    monkeypatch.setattr(ask, "_peer_caller_pane_info", lambda: ("30", "wezterm"))
    monkeypatch.setattr(
        ask,
        "identify_sender",
        lambda *_args, **_kwargs: ask.peer_routing.SenderResolution(
            candidate=ask.peer_routing.SenderCandidate(
                launch_id="launch",
                live_id="sender-live-id",
                provider="codex",
                pane_id="30",
                terminal="wezterm",
                session_file="sender.json",
                ccb_project_id="sender-project",
                work_dir=str(project),
                pane_title_marker="sender-marker",
            )
        ),
    )
    monkeypatch.setattr(ask, "new_peer_receipt", _receipt)
    monkeypatch.setattr(ask.subprocess, "run", _run)

    rc = ask._run_peer_bridge(
        "/peer",
        "codex",
        10.0,
        "hello",
        False,
        "wait",
        live_id="target-live-id",
    )

    assert rc == ask.EXIT_OK
    assert captured["receipt_kwargs"]["caller_pane_id"] == "30"
    assert captured["receipt_kwargs"]["caller_terminal"] == "wezterm"
    assert captured["receipt_kwargs"]["caller_live_id"] == "sender-live-id"
    assert captured["receipt_kwargs"]["caller_pane_title_marker"] == "sender-marker"
    assert captured["cmd"][captured["cmd"].index("--peer-task-id") + 1] == "task-fixed"
    assert captured["cmd"][captured["cmd"].index("--live-id") + 1] == "target-live-id"
    assert captured["run_kwargs"]["env"]["CCB_REQ_ID"] == "task-fixed"
    output = capsys.readouterr().out
    assert "[CCB_ASYNC_SUBMITTED provider=peer-codex intent=wait]" in output
    assert "task-fixed" in output


def test_peer_foreground_notify_remains_receipt_free(monkeypatch, tmp_path: Path, capsys) -> None:
    ask = _load_ask_module()
    captured: dict = {}

    class _Result:
        returncode = 0

    monkeypatch.setattr(ask, "_peer_caller_pane_info", lambda: ("30", "wezterm"))
    monkeypatch.setattr(
        ask,
        "_prepare_peer_task",
        lambda *_args: (_ for _ in ()).throw(AssertionError("notify created a receipt")),
    )
    monkeypatch.setattr(
        ask.subprocess,
        "run",
        lambda cmd, **kwargs: captured.update(cmd=cmd, run_kwargs=kwargs) or _Result(),
    )

    rc = ask._run_peer_bridge_foreground(
        "/peer", "codex", 10.0, "FYI", "codex", "notify", "", tmp_path
    )

    assert rc == ask.EXIT_OK
    assert "--peer-task-id" not in captured["cmd"]
    assert "CCB_REQ_ID" not in captured["run_kwargs"]["env"]
    assert "CCB_ASYNC_SUBMITTED" not in capsys.readouterr().out


def test_peer_background_uses_resolved_sender_for_receipt_status_and_bridge(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ask = _load_ask_module()
    project = tmp_path / "project"
    project.mkdir()
    captured: dict = {}

    class _Proc:
        pid = 4242

    def _popen(cmd, **kwargs):
        captured.update(cmd=cmd, env=kwargs["env"], popen_kwargs=kwargs)
        return _Proc()

    monkeypatch.setattr(ask.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ask, "make_task_id", lambda: "task-fixed")
    monkeypatch.setattr(ask, "_cleanup_task_logs", lambda _path: None)
    monkeypatch.setattr(ask, "_peer_caller_pane_info", lambda: ("%1", "tmux"))
    monkeypatch.setattr(ask.subprocess, "Popen", _popen)

    rc = ask._run_peer_bridge_background(
        "/peer",
        "claude",
        10.0,
        "hello",
        "claude",
        "background",
        "",
        project.resolve(),
    )

    assert rc == ask.EXIT_OK
    task_dir = tmp_path / "ccb-tasks"
    receipt = json.loads((task_dir / "ask-peer-claude-task-fixed.json").read_text(encoding="utf-8"))
    assert receipt["work_dir"] == str(project.resolve())
    status = (task_dir / "ask-peer-claude-task-fixed.status").read_text(encoding="utf-8")
    assert f"work_dir={project.resolve()}" in status
    sender_index = captured["cmd"].index("--sender-work-dir") + 1
    assert captured["cmd"][sender_index] == str(project.resolve())
    assert captured["env"]["CCB_WORK_DIR"] == str(project.resolve())
    assert captured["env"]["CCB_PEER_PROVIDER"] == "claude"
    assert captured["env"]["CCB_PEER_LOG_FILE"].endswith(
        "ask-peer-claude-task-fixed.log"
    )
    if ask.os.name == "nt":
        assert captured["popen_kwargs"]["creationflags"]
    else:
        assert captured["popen_kwargs"]["start_new_session"] is True


def test_unified_daemon_spills_oversized_foreground_reply(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    ask = _load_ask_module()
    state_file = tmp_path / "askd.json"
    monkeypatch.setenv("CCB_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(
        askd_rpc,
        "read_state",
        lambda _path: {
            "host": "127.0.0.1",
            "port": 31337,
            "token": "tok",
            "work_dir": str(tmp_path),
        },
    )
    monkeypatch.setattr(
        askd_rpc,
        "request_daemon",
        lambda _state, _request, **_kwargs: {
            "exit_code": 0,
            "req_id": "req-large",
            "reply": "x" * (64 * 1024 + 1),
        },
    )
    monkeypatch.setattr(ask, "_find_running_unified_state_file", lambda **_kwargs: state_file)
    monkeypatch.setattr(ask, "_maybe_start_unified_daemon", lambda: False)
    monkeypatch.setattr(ask, "_caller_pane_info", lambda: ("%1", "tmux"))

    rc = ask._send_via_unified_daemon(
        "codex", "hello", 1.0, False, "claude"
    )

    output = capsys.readouterr().out
    assert rc == 0
    assert "[CCB_RESULT_SPILLED]" in output
    assert (
        tmp_path / "run" / "completions" / "req-large.md"
    ).stat().st_size == 64 * 1024 + 1


def test_peer_notify_does_not_require_mounted_sender(monkeypatch, tmp_path: Path) -> None:
    ask = _load_ask_module()
    captured: list[Path] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CCB_CALLER", "claude")
    monkeypatch.delenv("CCB_WORK_DIR", raising=False)
    monkeypatch.setattr(
        ask,
        "_resolve_sender_work_dir",
        lambda _caller: (_ for _ in ()).throw(AssertionError("notify validated sender")),
    )
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge_foreground",
        lambda *_args, **_kwargs: captured.append(_args[-1]) or 0,
    )

    rc = ask._run_peer_bridge("/peer", "claude", 10.0, "FYI", True, "notify")

    assert rc == 0
    assert captured == [tmp_path.resolve()]


# --- route resolution: _preflight_target, env carry, and background spawn --


def test_preflight_target_route_supersedes_ambiguous_status(monkeypatch) -> None:
    """An inventory-present record's route resolution, not the generic
    per-provider status, decides pass/fail: `provider_status_for_target`
    alone would refuse (its ambiguity check has no caller context), but a
    caller-identified sibling pick must still succeed and populate the
    route.
    """
    from live_sessions import LiveSession, Resolution

    ask = _load_ask_module()
    sibling = LiveSession(live_id="s2", provider="codex", launch_id="ai-1", pane_id="%3")
    caller = LiveSession(live_id="s1", provider="codex", launch_id="ai-1", pane_id="%2")

    monkeypatch.setattr(
        ask,
        "provider_status_for_target",
        lambda *_a, **_k: _provider_status(mounted=False, daemon_online=True),
    )
    monkeypatch.setattr(
        ask,
        "resolve_live_route",
        lambda *_a, **_k: (Resolution(session=sibling), caller),
    )

    route_out: dict = {}
    ok = ask._preflight_target(
        "codex",
        work_dir="/tmp/proj",
        caller_pane_id="%2",
        caller_terminal="tmux",
        route_out=route_out,
    )

    assert ok is True
    assert route_out == {
        "live_id": "s2",
        "launch_id": "ai-1",
            "caller_live_id": "s1",
                "caller_token": "",
            "caller_pane_id": "%2",
            "caller_terminal": "",
        "pane_id": "%3",
        "terminal": "",
        "session_file": "",
        "ccb_project_id": "",
    }


def test_preflight_target_refuses_when_route_present_but_unresolvable(monkeypatch, capsys) -> None:
    """Even a generically MOUNTED status must not override an inventory
    that is present but cannot be narrowed to one destination for this
    caller -- the route check supersedes the status check, not the other
    way around.
    """
    from live_sessions import Resolution

    ask = _load_ask_module()
    monkeypatch.setattr(
        ask,
        "provider_status_for_target",
        lambda *_a, **_k: _provider_status(mounted=True, daemon_online=True),
    )
    monkeypatch.setattr(
        ask,
        "resolve_live_route",
        lambda *_a, **_k: (Resolution(error="unknown_caller", detail="2 codex sessions and no verified caller"), None),
    )

    route_out: dict = {}
    ok = ask._preflight_target("codex", work_dir="/tmp/proj", route_out=route_out)

    assert ok is False
    assert route_out == {}
    err = capsys.readouterr().err
    assert "CCB_ROUTE_ERROR target=codex reason=unknown_caller" in err


def test_preflight_target_falls_back_to_status_when_no_inventory(monkeypatch) -> None:
    """`resolve_live_route` returning `None` (no `live_sessions` key on the
    record) must leave today's status-only decision completely untouched.
    """
    ask = _load_ask_module()
    monkeypatch.setattr(
        ask,
        "provider_status_for_target",
        lambda *_a, **_k: _provider_status(mounted=True, daemon_online=True),
    )
    monkeypatch.setattr(ask, "resolve_live_route", lambda *_a, **_k: None)

    route_out: dict = {}
    ok = ask._preflight_target("codex", work_dir="/tmp/proj", route_out=route_out)

    assert ok is True
    assert route_out == {}


def test_inherited_route_from_env_reads_all_fields_when_task_id_matches(monkeypatch) -> None:
    # Finding 5: a route env var must be bound to the task it was resolved
    # for -- the child only inherits it when CCB_ROUTE_TASK_ID matches
    # CCB_REQ_ID, never on field presence alone.
    ask = _load_ask_module()
    monkeypatch.setenv("CCB_REQ_ID", "task-1")
    monkeypatch.setenv(ask._ROUTE_ENV_TASK_ID, "task-1")
    monkeypatch.setenv(ask._ROUTE_ENV_LIVE_ID, "s2")
    monkeypatch.setenv(ask._ROUTE_ENV_LAUNCH_ID, "ai-1")
    monkeypatch.setenv(ask._ROUTE_ENV_CALLER_LIVE_ID, "s1")
    monkeypatch.setenv(ask._ROUTE_ENV_PANE_ID, "%3")
    monkeypatch.setenv(ask._ROUTE_ENV_TERMINAL, "tmux")
    monkeypatch.setenv(ask._ROUTE_ENV_SESSION_FILE, "/tmp/s2.json")
    monkeypatch.setenv(ask._ROUTE_ENV_PROJECT_ID, "proj-1")

    route = ask._inherited_route_from_env()

    assert route == ask.ResolvedRoute(
        live_id="s2",
        launch_id="ai-1",
        caller_live_id="s1",
        pane_id="%3",
        terminal="tmux",
        session_file="/tmp/s2.json",
        ccb_project_id="proj-1",
    )


def test_inherited_route_from_env_is_none_when_unset(monkeypatch) -> None:
    ask = _load_ask_module()
    monkeypatch.delenv(ask._ROUTE_ENV_LIVE_ID, raising=False)
    monkeypatch.delenv(ask._ROUTE_ENV_LAUNCH_ID, raising=False)
    monkeypatch.delenv(ask._ROUTE_ENV_TASK_ID, raising=False)
    monkeypatch.delenv("CCB_REQ_ID", raising=False)

    assert ask._inherited_route_from_env() is None


def test_inherited_route_from_env_ignores_stale_task_id_mismatch(monkeypatch) -> None:
    # A leftover route env var from a DIFFERENT, unrelated task must never
    # be mistaken for a fresh preflight result for THIS task.
    ask = _load_ask_module()
    monkeypatch.setenv("CCB_REQ_ID", "task-current")
    monkeypatch.setenv(ask._ROUTE_ENV_TASK_ID, "task-stale")
    monkeypatch.setenv(ask._ROUTE_ENV_LIVE_ID, "s2")
    monkeypatch.setenv(ask._ROUTE_ENV_LAUNCH_ID, "ai-1")

    assert ask._inherited_route_from_env() is None


def test_inherited_route_from_env_rejects_malformed_partial_route(monkeypatch) -> None:
    # Finding 5: a partial route (live_id without launch_id) must not be
    # silently downgraded to "no route" -- it must raise, so the caller
    # refuses the ask outright instead of falling back to legacy lookup.
    ask = _load_ask_module()
    monkeypatch.setenv("CCB_REQ_ID", "task-1")
    monkeypatch.setenv(ask._ROUTE_ENV_TASK_ID, "task-1")
    monkeypatch.setenv(ask._ROUTE_ENV_LIVE_ID, "s2")
    monkeypatch.delenv(ask._ROUTE_ENV_LAUNCH_ID, raising=False)

    import pytest as _pytest

    with _pytest.raises(ask.MalformedRouteError):
        ask._inherited_route_from_env()


def test_route_env_export_lines_shell_and_powershell_styles() -> None:
    ask = _load_ask_module()
    route = ask.ResolvedRoute(live_id="s2", launch_id="ai-1", caller_live_id="s1")

    sh = ask._route_env_export_lines(route, style="sh", task_id="task-1")
    assert "export CCB_ROUTE_TASK_ID=task-1\n" in sh
    assert "export CCB_ROUTE_LIVE_ID=s2\n" in sh
    assert "export CCB_ROUTE_LAUNCH_ID=ai-1\n" in sh
    assert "export CCB_ROUTE_CALLER_LIVE_ID=s1\n" in sh

    ps1 = ask._route_env_export_lines(route, style="ps1", task_id="task-1")
    assert "$env:CCB_ROUTE_TASK_ID = 'task-1'\n" in ps1
    assert "$env:CCB_ROUTE_LIVE_ID = 's2'\n" in ps1
    assert "$env:CCB_ROUTE_LAUNCH_ID = 'ai-1'\n" in ps1

    assert ask._route_env_export_lines(ask.ResolvedRoute(), style="sh", task_id="task-1") == ""
    assert ask._route_env_export_lines(None, style="sh", task_id="task-1") == ""


def test_route_env_export_lines_powershell_special_characters_survive_intact(monkeypatch) -> None:
    # Ruling: PowerShell values must be literal-safe, not naive
    # double-quote interpolation -- prove special characters round-trip.
    ask = _load_ask_module()
    tricky = "s2's \"pane\" `backtick` $env:HOME; Remove-Item -Recurse"
    route = ask.ResolvedRoute(live_id=tricky, launch_id="ai-1")

    ps1 = ask._route_env_export_lines(route, style="ps1", task_id="task-1")

    # The value must appear as a single-quoted literal with only `'`
    # doubled -- never split, never executed, never losing characters.
    expected_literal = ask._ps1_literal(tricky)
    assert f"$env:CCB_ROUTE_LIVE_ID = {expected_literal}\n" in ps1
    assert "Remove-Item" not in ps1.split("=", 1)[0]  # not outside the literal

    # Round-trip it back through a real PowerShell-style single-quote
    # parse to prove no information was lost.
    line = next(ln for ln in ps1.splitlines() if ln.startswith("$env:CCB_ROUTE_LIVE_ID"))
    literal = line.split("=", 1)[1].strip()
    assert literal.startswith("'") and literal.endswith("'")
    recovered = literal[1:-1].replace("''", "'")
    assert recovered == tricky


def test_default_async_background_script_carries_resolved_route_via_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The route this process resolves in preflight must reach the detached
    `--foreground` re-invocation through the environment the spawned script
    exports -- that child must never resolve it again.
    """
    ask = _load_ask_module()
    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, Path.cwd())

    class _Proc:
        pid = 999

    def _popen(cmd, **kwargs):
        return _Proc()

    def _fake_preflight(_provider, *, work_dir=None, caller_pane_id="", caller_terminal="",
                        caller_live_id="", caller_token="", route_out=None):
        if route_out is not None:
            route_out.update(
                live_id="s2",
                launch_id="ai-1",
                caller_live_id="s1",
                pane_id="%3",
                terminal="tmux",
                session_file="/tmp/s2.json",
                ccb_project_id="proj-1",
            )
        return True

    monkeypatch.setenv("CCB_CALLER", "claude")
    monkeypatch.setattr(ask.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ask, "make_task_id", lambda: "task-fixed")
    monkeypatch.setattr(ask, "_cleanup_task_logs", lambda _path: None)
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: True)
    monkeypatch.setattr(ask, "_resolve_unified_daemon_context", lambda: context)
    monkeypatch.setattr(ask, "_preflight_target", _fake_preflight)
    monkeypatch.setattr(ask.subprocess, "Popen", _popen)

    rc = ask.main(["ask", "codex", "hello"])

    assert rc == ask.EXIT_OK
    log_dir = tmp_path / "ccb-tasks"
    if ask.os.name == "nt":
        content = (log_dir / "ask-codex-task-fixed.ps1").read_text(encoding="utf-8")
        # Single-quoted PowerShell literals, matching `_ps1_literal`: this
        # branch never runs on Linux, so it has to be asserted against the
        # encoder's real output rather than assumed.
        assert "$env:CCB_ROUTE_LIVE_ID = 's2'" in content
        assert "$env:CCB_ROUTE_LAUNCH_ID = 'ai-1'" in content
        assert "$env:CCB_ROUTE_CALLER_LIVE_ID = 's1'" in content
    else:
        content = (log_dir / "ask-codex-task-fixed.sh").read_text(encoding="utf-8")
        assert "export CCB_ROUTE_LIVE_ID=s2" in content
        assert "export CCB_ROUTE_LAUNCH_ID=ai-1" in content
        assert "export CCB_ROUTE_CALLER_LIVE_ID=s1" in content


def test_managed_codex_background_submits_to_host_daemon_without_detached_child(
    monkeypatch, tmp_path: Path
) -> None:
    ask = _load_ask_module()
    context = ask._UnifiedDaemonContext(tmp_path / "askd.json", {"token": "tok"}, tmp_path)
    captured: dict = {}

    def _preflight(_provider, **kwargs):
        kwargs["route_out"].update(
            live_id="s2", launch_id="ai-1", caller_live_id="s1",
            caller_token="secret", caller_pane_id="%1", caller_terminal="tmux",
            pane_id="%2", terminal="tmux", session_file=str(tmp_path / "s2.json"),
            ccb_project_id="project-1",
        )
        return True

    def _send(*args, **kwargs):
        captured.update(kwargs)
        return ask.EXIT_OK

    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setenv("CCB_LIVE_ID", "s1")
    monkeypatch.setenv("CCB_LIVE_TOKEN", "secret")
    monkeypatch.setattr(ask.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ask, "make_task_id", lambda: "task-fixed")
    monkeypatch.setattr(ask, "inside_managed_codex_sandbox", lambda: True)
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: True)
    monkeypatch.setattr(ask, "_resolve_unified_daemon_context", lambda: context)
    monkeypatch.setattr(ask, "_preflight_target", _preflight)
    monkeypatch.setattr(ask, "_send_via_unified_daemon", _send)
    monkeypatch.setattr(
        ask.subprocess, "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("detached child started")),
    )

    assert ask.main(["ask", "codex", "--background", "hello"]) == ask.EXIT_OK
    assert captured["async_submit"] is True
    assert captured["request_id"] == "task-fixed"


def test_pair_credential_ignores_unrelated_ambient_terminal_pane(monkeypatch, tmp_path: Path) -> None:
    ask = _load_ask_module()
    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, Path.cwd())
    seen = {}

    def preflight(_provider, **kwargs):
        seen.update(kwargs)
        return False

    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setenv("CCB_LIVE_ID", "live-one")
    monkeypatch.setenv("CCB_LIVE_TOKEN", "secret")
    monkeypatch.setenv("WEZTERM_PANE", "unrelated-outer-pane")
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: True)
    monkeypatch.setattr(ask, "_resolve_unified_daemon_context", lambda: context)
    monkeypatch.setattr(ask, "_preflight_target", preflight)
    assert ask.main(["ask", "codex", "hello"]) == ask.EXIT_ERROR
    assert seen["caller_live_id"] == "live-one"
    assert seen["caller_token"] == "secret"
    assert seen["caller_pane_id"] == ""
    assert seen["caller_terminal"] == ""


def test_foreground_re_invocation_inherits_route_without_resolving_again(monkeypatch) -> None:
    """The detached `--foreground` re-invocation must use the route it
    inherited from the environment and must NOT call `_preflight_target`
    (which would mean resolving a second time).
    """
    ask = _load_ask_module()
    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, Path.cwd())
    captured: dict = {}

    monkeypatch.setenv("CCB_CALLER", "claude")
    monkeypatch.setenv("CCB_REQ_ID", "task-1")
    monkeypatch.setenv(ask._ROUTE_ENV_TASK_ID, "task-1")
    monkeypatch.setenv(ask._ROUTE_ENV_LIVE_ID, "s2")
    monkeypatch.setenv(ask._ROUTE_ENV_LAUNCH_ID, "ai-1")
    monkeypatch.setenv(ask._ROUTE_ENV_CALLER_LIVE_ID, "s1")
    monkeypatch.setenv(ask._ROUTE_ENV_PANE_ID, "%3")
    monkeypatch.setenv(ask._ROUTE_ENV_TERMINAL, "tmux")
    monkeypatch.setenv(ask._ROUTE_ENV_SESSION_FILE, "/tmp/s2.json")
    monkeypatch.setenv(ask._ROUTE_ENV_PROJECT_ID, "proj-1")
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: True)
    monkeypatch.setattr(ask, "_resolve_unified_daemon_context", lambda: context)
    monkeypatch.setattr(
        ask,
        "_preflight_target",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("resolved a second time")),
    )
    monkeypatch.setattr(
        ask,
        "_send_via_unified_daemon",
        lambda *_a, **kwargs: captured.update(route=kwargs.get("route")) or 0,
    )

    rc = ask.main(["ask", "codex", "--foreground", "hello"])

    assert rc == 0
    assert captured["route"] == ask.ResolvedRoute(
        live_id="s2",
        launch_id="ai-1",
        caller_live_id="s1",
        pane_id="%3",
        terminal="tmux",
        session_file="/tmp/s2.json",
        ccb_project_id="proj-1",
    )


def test_notify_mode_carries_a_resolved_route_when_inventory_applies(monkeypatch) -> None:
    # Ruling: notify mode must carry the route too -- an inventory-backed
    # destination cannot pass a route-aware preflight and then send without
    # that identity.
    from live_sessions import LiveSession, Resolution

    ask = _load_ask_module()
    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, Path.cwd())
    sibling = LiveSession(
        live_id="s2",
        provider="claude",
        launch_id="ai-1",
        pane_id="%3",
        terminal="tmux",
        session_file="/tmp/s2.json",
        ccb_project_id="proj-1",
    )
    caller = LiveSession(live_id="s1", provider="claude", launch_id="ai-1", pane_id="%2")
    captured: dict = {}

    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: True)
    monkeypatch.setattr(ask, "_resolve_unified_daemon_context", lambda: context)
    monkeypatch.setattr(ask, "provider_status_for_target", lambda *_a, **_k: _provider_status(mounted=True))
    monkeypatch.setattr(ask, "resolve_live_route", lambda *_a, **_k: (Resolution(session=sibling), caller))
    monkeypatch.setattr(
        ask,
        "_send_via_unified_daemon",
        lambda provider, message, timeout, no_wrap, caller, **kwargs: captured.update(
            provider=provider, kwargs=kwargs
        )
        or 0,
    )

    rc = ask.main(["ask", "claude", "--notify", "FYI"])

    assert rc == 0
    route = captured["kwargs"]["route"]
    assert route.live_id == "s2"
    assert route.launch_id == "ai-1"
    assert route.caller_live_id == "s1"
    assert route.present is True


@pytest.mark.parametrize("missing_field", ["pane_id", "terminal", "session_file", "ccb_project_id"])
def test_inherited_route_from_env_rejects_route_missing_one_mandatory_field(monkeypatch, missing_field) -> None:
    # Hole 1, env-boundary side: same requirement as the RPC boundary --
    # once a route is present at all, its endpoint-evidence fields are
    # mandatory. Removing just one must still raise, not parse as a
    # (weaker, unenforceable) valid route.
    ask = _load_ask_module()
    monkeypatch.setenv("CCB_REQ_ID", "task-1")
    monkeypatch.setenv(ask._ROUTE_ENV_TASK_ID, "task-1")
    env_values = {
        ask._ROUTE_ENV_LIVE_ID: "s2",
        ask._ROUTE_ENV_LAUNCH_ID: "ai-1",
        ask._ROUTE_ENV_PANE_ID: "%3",
        ask._ROUTE_ENV_TERMINAL: "tmux",
        ask._ROUTE_ENV_SESSION_FILE: "/tmp/s2.json",
        ask._ROUTE_ENV_PROJECT_ID: "proj-1",
    }
    field_to_env = {
        "pane_id": ask._ROUTE_ENV_PANE_ID,
        "terminal": ask._ROUTE_ENV_TERMINAL,
        "session_file": ask._ROUTE_ENV_SESSION_FILE,
        "ccb_project_id": ask._ROUTE_ENV_PROJECT_ID,
    }
    del env_values[field_to_env[missing_field]]
    for name, value in env_values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(field_to_env[missing_field], raising=False)

    with pytest.raises(ask.MalformedRouteError):
        ask._inherited_route_from_env()


@pytest.mark.parametrize("missing_field", ["pane_id", "terminal", "session_file", "ccb_project_id"])
def test_foreground_ask_refuses_and_never_sends_when_inherited_route_missing_one_field(
    monkeypatch, missing_field
) -> None:
    # Full pipeline, env boundary: an incomplete inherited route must
    # refuse the whole ask before ever reaching the daemon transport --
    # NO TERMINAL SEND OCCURRED, proven the same way the unsupported-
    # adapter tests prove it: the send function must never be called.
    ask = _load_ask_module()
    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, Path.cwd())

    monkeypatch.setenv("CCB_CALLER", "claude")
    monkeypatch.setenv("CCB_REQ_ID", "task-1")
    monkeypatch.setenv(ask._ROUTE_ENV_TASK_ID, "task-1")
    env_values = {
        ask._ROUTE_ENV_LIVE_ID: "s2",
        ask._ROUTE_ENV_LAUNCH_ID: "ai-1",
        ask._ROUTE_ENV_PANE_ID: "%3",
        ask._ROUTE_ENV_TERMINAL: "tmux",
        ask._ROUTE_ENV_SESSION_FILE: "/tmp/s2.json",
        ask._ROUTE_ENV_PROJECT_ID: "proj-1",
    }
    field_to_env = {
        "pane_id": ask._ROUTE_ENV_PANE_ID,
        "terminal": ask._ROUTE_ENV_TERMINAL,
        "session_file": ask._ROUTE_ENV_SESSION_FILE,
        "ccb_project_id": ask._ROUTE_ENV_PROJECT_ID,
    }
    del env_values[field_to_env[missing_field]]
    for name, value in env_values.items():
        monkeypatch.setenv(name, value)

    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: True)
    monkeypatch.setattr(ask, "_resolve_unified_daemon_context", lambda: context)
    monkeypatch.setattr(
        ask,
        "_preflight_target",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("resolved a second time")),
    )
    monkeypatch.setattr(
        ask,
        "_send_via_unified_daemon",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("terminal send reached despite malformed route")),
    )

    rc = ask.main(["ask", "codex", "--foreground", "hello"])

    assert rc == ask.EXIT_ERROR


def test_foreground_ask_refuses_and_never_sends_when_valid_inventory_lacks_provider(monkeypatch) -> None:
    # Hole 3(a): resolve_live_route refuses because the caller's own
    # launch has a present, valid inventory that doesn't name this
    # provider. The ask must stop right there -- NO TERMINAL SEND OCCURRED
    # -- never fall through to a legacy-lookup send.
    from live_sessions import Resolution

    ask = _load_ask_module()
    monkeypatch.setattr(ask, "provider_status_for_target", lambda *_a, **_k: _provider_status(mounted=True))
    monkeypatch.setattr(
        ask,
        "resolve_live_route",
        lambda *_a, **_k: (Resolution(error="not_mounted", detail="no codex session in this launch"), None),
    )
    monkeypatch.setattr(
        ask,
        "_send_via_unified_daemon",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("terminal send reached despite authoritative refusal")),
    )
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: False)

    rc = ask.main(["ask", "codex", "--foreground", "hello"])

    assert rc == ask.EXIT_ERROR


def test_foreground_ask_refuses_and_never_sends_when_caller_evidence_matches_nothing(monkeypatch) -> None:
    # Hole 3(b): evidence was supplied but matched no launch, even though
    # exactly one otherwise-eligible destination exists. Must refuse and
    # never send.
    from live_sessions import Resolution

    ask = _load_ask_module()
    monkeypatch.setattr(ask, "provider_status_for_target", lambda *_a, **_k: _provider_status(mounted=True))
    monkeypatch.setattr(
        ask,
        "resolve_live_route",
        lambda *_a, **_k: (Resolution(error="unknown_caller", detail="caller evidence did not match any launch in this project"), None),
    )
    monkeypatch.setattr(
        ask,
        "_send_via_unified_daemon",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("terminal send reached despite unresolvable caller evidence")),
    )
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: False)

    rc = ask.main(["ask", "codex", "--foreground", "hello"])

    assert rc == ask.EXIT_ERROR


# --- `--live-id`: peer-only selector plumbing and refusals ---


def test_peer_bridge_cmd_threads_live_id_as_its_own_option() -> None:
    ask = _load_ask_module()

    cmd = ask._peer_bridge_cmd(
        "/tmp/peer", "codex", 10.0, "%1", "tmux", "/tmp/sender", "claude", "wait",
        live_id="live-b",
    )

    assert "--live-id" in cmd
    assert cmd[cmd.index("--live-id") + 1] == "live-b"
    # Never folded into the target or the message.
    assert cmd[cmd.index("--target") + 1] == "/tmp/peer"


def test_peer_bridge_cmd_without_live_id_is_unchanged() -> None:
    ask = _load_ask_module()

    cmd = ask._peer_bridge_cmd(
        "/tmp/peer", "codex", 10.0, "%1", "tmux", "/tmp/sender", "claude", "wait"
    )

    assert "--live-id" not in cmd


def test_ask_forwards_live_id_for_provider_peer_form(monkeypatch) -> None:
    ask = _load_ask_module()
    captured: dict = {}
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge",
        lambda *_args, **kwargs: captured.update(kwargs) or 0,
    )

    rc = ask.main(
        ["ask", "codex", "--peer", "/tmp/peer", "--live-id", "live-b", "--notify", "FYI"]
    )

    assert rc == 0
    assert captured == {"live_id": "live-b"}


def test_peer_mode_parser_forwards_live_id(monkeypatch) -> None:
    ask = _load_ask_module()
    captured: dict = {}
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge",
        lambda *_args, **kwargs: captured.update(kwargs) or 0,
    )

    rc = ask._handle_peer_mode(["--peer", "/tmp/peer", "--live-id", "live-b", "--notify", "FYI"])

    assert rc == 0
    assert captured == {"live_id": "live-b"}


def _refuse_dispatch(ask, monkeypatch) -> None:
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("dispatched despite refusal")),
    )


def test_ask_local_live_id_without_inventory_refuses(monkeypatch, capsys) -> None:
    """Without --peer, --live-id names a session of this launch. A launch
    with no live-session inventory has nothing to name, and the request
    must refuse rather than fall through to the provider default."""
    ask = _load_ask_module()
    _refuse_dispatch(ask, monkeypatch)

    rc = ask.main(["ask", "codex", "--live-id", "live-b", "hello"])

    assert rc == 1
    assert "reason=no_inventory" in capsys.readouterr().err


def test_ask_live_id_requires_a_value(monkeypatch, capsys) -> None:
    ask = _load_ask_module()
    _refuse_dispatch(ask, monkeypatch)

    rc = ask.main(["ask", "codex", "--peer", "/tmp/peer", "--live-id"])

    assert rc == 1
    assert "--live-id requires a live session ID" in capsys.readouterr().err


def test_ask_live_id_rejects_repeated_use(monkeypatch, capsys) -> None:
    """The selector deliberately breaks the CLI's last-wins habit: a
    repeated selector is a conflicting instruction, not an override."""
    ask = _load_ask_module()
    _refuse_dispatch(ask, monkeypatch)

    rc = ask.main(
        ["ask", "codex", "--peer", "/tmp/peer", "--live-id", "live-a", "--live-id", "live-b", "hi"]
    )

    assert rc == 1
    assert "--live-id may only be given once" in capsys.readouterr().err


def test_ask_live_id_rejects_reply_to_combination(monkeypatch, capsys) -> None:
    ask = _load_ask_module()
    _refuse_dispatch(ask, monkeypatch)

    rc = ask.main(
        [
            "ask", "codex", "--peer", "/tmp/peer",
            "--live-id", "live-b",
            "--reply-to", "20260711-212112-453-72347",
            "Done.",
        ]
    )

    assert rc == 1
    assert "cannot be combined with --reply-to" in capsys.readouterr().err


def test_ask_live_id_requires_value_without_swallowing_option(monkeypatch, capsys) -> None:
    """A missing value can never swallow the following option token."""
    ask = _load_ask_module()
    _refuse_dispatch(ask, monkeypatch)

    rc = ask.main(["ask", "codex", "--live-id", "--peer", "/tmp/peer", "hello"])

    assert rc == 1
    assert "--live-id requires a live session ID" in capsys.readouterr().err


def test_ask_local_live_id_resolves_that_exact_session(monkeypatch) -> None:
    """A lead naming one member of a Codex pair passes that live ID to route
    resolution and sends on the route it returns."""
    from live_sessions import LiveSession, Resolution

    ask = _load_ask_module()
    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, Path.cwd())
    target = LiveSession(
        live_id="cx2",
        provider="codex",
        launch_id="ai-1",
        pane_id="%3",
        terminal="tmux",
        session_file="/tmp/cx2.json",
        ccb_project_id="proj-1",
    )
    lead = LiveSession(live_id="lead", provider="claude", launch_id="ai-1", pane_id="%1")
    seen: dict = {}
    captured: dict = {}

    def _resolve(*_a, **kwargs):
        seen.update(kwargs)
        return Resolution(session=target), lead

    monkeypatch.setenv("CCB_CALLER", "claude")
    monkeypatch.setenv("CCB_LIVE_ID", "lead")
    monkeypatch.setenv("CCB_LIVE_TOKEN", "tok-lead")
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: True)
    monkeypatch.setattr(ask, "_resolve_unified_daemon_context", lambda: context)
    monkeypatch.setattr(ask, "provider_status_for_target", lambda *_a, **_k: _provider_status(mounted=True))
    monkeypatch.setattr(ask, "resolve_live_route", _resolve)
    monkeypatch.setattr(
        ask,
        "_send_via_unified_daemon",
        lambda provider, message, timeout, no_wrap, caller, **kwargs: captured.update(kwargs=kwargs) or 0,
    )

    rc = ask.main(["ask", "codex", "--live-id", "cx2", "--notify", "implementer: status?"])

    assert rc == 0
    assert seen["target_live_id"] == "cx2"
    assert captured["kwargs"]["route"].live_id == "cx2"


def test_ask_local_live_id_refuses_without_unified_daemon(monkeypatch, capsys) -> None:
    ask = _load_ask_module()
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: False)

    rc = ask.main(["ask", "codex", "--live-id", "cx2", "hello"])

    assert rc == 1
    assert "--live-id needs the unified askd daemon" in capsys.readouterr().err
