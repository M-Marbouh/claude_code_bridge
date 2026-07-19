from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import askd_rpc
import askd_runtime
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


def _provider_status(*, mounted: bool, daemon_online: bool = True) -> ProviderRuntimeStatus:
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
    assert captured["kwargs"] == {
        "delivery_only": True,
        "suppress_completion_hook": True,
        "daemon_context": context,
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
        lambda target, provider, timeout, message, foreground, intent, reply_to: captured.update(
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
        lambda target, provider, timeout, message, foreground, intent, reply_to: captured.update(
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
