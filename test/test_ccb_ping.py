from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import types
from pathlib import Path

from ccb_runtime_status import ProviderRuntimeStatus


ROOT = Path(__file__).resolve().parents[1]


def _load_ping_module():
    path = ROOT / "bin" / "ccb-ping"
    loader = importlib.machinery.SourceFileLoader("ccb_ping_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _status(*, mounted: bool, reason: str = "") -> ProviderRuntimeStatus:
    return ProviderRuntimeStatus(
        key="claude",
        provider="claude",
        capable=True,
        configured=True,
        registered=True,
        pane_alive=mounted,
        session_bound=mounted,
        daemon_online=mounted,
        mounted=mounted,
        reason=reason,
    )


def test_ccb_ping_uses_host_runtime_status_in_managed_codex(monkeypatch, capsys) -> None:
    ping = _load_ping_module()
    monkeypatch.setattr(sys, "argv", ["ccb-ping", "claude"])
    monkeypatch.setattr(ping, "inside_managed_codex_sandbox", lambda: True)
    monkeypatch.setattr(ping, "provider_status_for_target", lambda *_args, **_kwargs: _status(mounted=True))

    rc = ping.main()

    assert rc == 0
    assert "host runtime verified" in capsys.readouterr().out


def test_ccb_ping_reports_host_runtime_failure_reason(monkeypatch, capsys) -> None:
    ping = _load_ping_module()
    monkeypatch.setattr(sys, "argv", ["ccb-ping", "claude"])
    monkeypatch.setattr(ping, "inside_managed_codex_sandbox", lambda: True)
    monkeypatch.setattr(
        ping,
        "provider_status_for_target",
        lambda *_args, **_kwargs: _status(mounted=False, reason="session_unbound"),
    )

    rc = ping.main()

    assert rc == 1
    assert "session_unbound" in capsys.readouterr().out


def test_ccb_ping_uses_daemon_project_root_for_implicit_managed_target(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    ping = _load_ping_module()
    project = tmp_path / "project"
    subdir = project / "nested"
    subdir.mkdir(parents=True)
    seen: list[Path] = []

    monkeypatch.chdir(subdir)
    monkeypatch.setenv("CCB_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(sys, "argv", ["ccb-ping", "claude"])
    monkeypatch.setattr(ping, "inside_managed_codex_sandbox", lambda: True)
    monkeypatch.setattr(ping, "resolve_daemon_work_dir", lambda _work_dir: project)

    def _capture_status(_provider: str, *, work_dir: Path) -> ProviderRuntimeStatus:
        seen.append(Path(work_dir))
        return _status(mounted=True)

    monkeypatch.setattr(ping, "provider_status_for_target", _capture_status)

    rc = ping.main()

    assert rc == 0
    assert seen == [project]
    assert "host runtime verified" in capsys.readouterr().out


def test_ccb_ping_fails_when_connected_provider_queue_is_stuck(monkeypatch, capsys) -> None:
    ping = _load_ping_module()
    monkeypatch.setattr(sys, "argv", ["ccb-ping", "claude"])
    monkeypatch.setattr(ping, "inside_managed_codex_sandbox", lambda: True)
    monkeypatch.setattr(ping, "resolve_daemon_work_dir", lambda work_dir: work_dir)
    monkeypatch.setattr(ping, "provider_status_for_target", lambda *_args, **_kwargs: _status(mounted=True))
    stuck_queue = [
        {
            "provider": "claude",
            "waiting": 2,
            "in_flight": {
                "req_id": "20260923-195638-295-2386635-3",
                "running_s": 900,
                "progress": {"phase": "turn_ended_without_marker", "stalled": True},
            },
        }
    ]
    monkeypatch.setattr(ping, "daemon_queue_status", lambda *_args, **_kwargs: stuck_queue)

    rc = ping.main()

    out = capsys.readouterr().out
    assert rc == 1
    assert "STUCK" in out and "20260923-195638-295-2386635-3" in out


def test_ccb_ping_reports_unknown_queue_without_failing(monkeypatch, capsys) -> None:
    ping = _load_ping_module()
    monkeypatch.setattr(sys, "argv", ["ccb-ping", "claude"])
    monkeypatch.setattr(ping, "inside_managed_codex_sandbox", lambda: True)
    monkeypatch.setattr(ping, "resolve_daemon_work_dir", lambda work_dir: work_dir)
    monkeypatch.setattr(ping, "provider_status_for_target", lambda *_args, **_kwargs: _status(mounted=True))

    def _no_daemon(*_args, **_kwargs):
        raise RuntimeError("Unified askd daemon state is unavailable")

    monkeypatch.setattr(ping, "daemon_queue_status", _no_daemon)

    rc = ping.main()

    assert rc == 0
    assert "queue: unknown" in capsys.readouterr().out


def test_paired_codex_ping_checks_each_registered_pane(monkeypatch, tmp_path: Path, capsys) -> None:
    ping = _load_ping_module()
    from live_sessions import LiveSession

    sessions = [
        LiveSession("live-a", "codex", "ai-test", pane_id="%12", terminal="tmux", session_file=str(tmp_path / "a")),
        LiveSession("live-b", "codex", "ai-test", pane_id="%13", terminal="tmux", session_file=str(tmp_path / "b")),
    ]
    for name in ("a", "b"):
        (tmp_path / name).write_text(json.dumps({
            "active": "True", "runtime_dir": str(tmp_path / name),
            "input_fifo": str(tmp_path / name / "input.fifo"), "terminal": "tmux",
            "tmux_session": "None", "pane_id": "%12" if name == "a" else "%13",
        }))
    monkeypatch.setenv("CODEX_SESSION_ID", "ai-test")

    class _Inventory:
        valid = True
        def __init__(self, items): self.sessions = items

    monkeypatch.setattr("live_sessions.read_inventory", lambda _record: _Inventory(sessions))
    monkeypatch.setattr("pane_registry.load_registry_by_session_id", lambda _launch: {"registry": True})
    monkeypatch.setattr("live_roles.roles_for_record", lambda _record: {"live-a": "implementer"})
    results = iter([(True, "✅ healthy"), (False, "❌ pane dead")])
    fake = types.ModuleType("codex_comm")
    class _Communicator:
        def ping(self, display=False): return next(results)
    fake.CodexCommunicator = _Communicator
    monkeypatch.setitem(sys.modules, "codex_comm", fake)
    monkeypatch.setattr(sys, "argv", ["ccb-ping", "codex"])

    assert ping.main() == 1
    out = capsys.readouterr().out
    assert "live-a (%12, implementer): ✅ healthy" in out
    assert "live-b (%13): ❌ pane dead" in out


def test_explicit_codex_session_file_overrides_inherited_runtime(monkeypatch, tmp_path: Path) -> None:
    from codex_comm import CodexCommunicator

    runtime = tmp_path / "live-id"
    runtime.mkdir()
    session_file = tmp_path / ".codex-session"
    session_file.write_text(json.dumps({
        "active": "True",
        "session_id": "pane-session",
        "runtime_dir": str(runtime),
        "input_fifo": str(runtime / "input.fifo"),
        "terminal": "tmux",
        "tmux_session": "None",
    }))
    monkeypatch.setenv("CODEX_SESSION_ID", "launch-id")
    monkeypatch.setenv("CODEX_RUNTIME_DIR", str(tmp_path / "codex"))
    monkeypatch.setenv("CODEX_INPUT_FIFO", str(tmp_path / "codex" / "input.fifo"))
    monkeypatch.setenv("CCB_SESSION_FILE", str(session_file))

    comm = CodexCommunicator(lazy_init=True)

    assert comm.runtime_dir == runtime
    assert comm.session_info["_session_file"] == str(session_file)


def test_missing_explicit_codex_session_fails_closed(monkeypatch, tmp_path: Path) -> None:
    from codex_comm import CodexCommunicator

    monkeypatch.setenv("CODEX_SESSION_ID", "launch-id")
    monkeypatch.setenv("CODEX_RUNTIME_DIR", str(tmp_path / "existing-runtime"))
    monkeypatch.setenv("CODEX_INPUT_FIFO", str(tmp_path / "input.fifo"))
    monkeypatch.setenv("CCB_SESSION_FILE", str(tmp_path / ".codex-session"))

    try:
        CodexCommunicator(lazy_init=True)
    except RuntimeError as exc:
        assert "explicit" in str(exc).lower()
    else:
        raise AssertionError("missing explicit file must not use inherited runtime")


def test_inactive_explicit_codex_session_is_rejected(monkeypatch, tmp_path: Path) -> None:
    from codex_comm import CodexCommunicator

    session_file = tmp_path / ".codex-session"
    session_file.write_text(json.dumps({
        "active": "False", "session_id": "pane-session",
        "runtime_dir": str(tmp_path), "input_fifo": str(tmp_path / "input.fifo"),
    }))
    monkeypatch.setenv("CODEX_SESSION_ID", "launch-id")
    monkeypatch.setenv("CODEX_RUNTIME_DIR", str(tmp_path / "inherited"))
    monkeypatch.setenv("CODEX_INPUT_FIFO", str(tmp_path / "input.fifo"))
    monkeypatch.setenv("CCB_SESSION_FILE", str(session_file))

    try:
        CodexCommunicator(lazy_init=True)
    except RuntimeError as exc:
        assert "active" in str(exc).lower()
    else:
        raise AssertionError("inactive explicit session must be rejected")


def test_sandbox_unknown_live_id_fails(monkeypatch, capsys) -> None:
    ping = _load_ping_module()
    monkeypatch.setattr(sys, "argv", ["ccb-ping", "codex", "--live-id", "missing"])
    monkeypatch.setattr(ping, "inside_managed_codex_sandbox", lambda: True)
    monkeypatch.setenv("CODEX_SESSION_ID", "launch-id")
    monkeypatch.setattr("pane_registry.load_registry_by_session_id", lambda _launch: {"live_sessions": []})

    rc = ping.main()

    assert rc == 1
    assert "unknown" in capsys.readouterr().err.lower()


def test_sandbox_known_live_id_refuses_unverifiable_health(monkeypatch, capsys) -> None:
    ping = _load_ping_module()
    from live_sessions import LiveSession

    class _Inventory:
        valid = True
        sessions = [LiveSession("live-a", "codex", "launch-id")]

    monkeypatch.setattr(sys, "argv", ["ccb-ping", "codex", "--live-id", "live-a"])
    monkeypatch.setattr(ping, "inside_managed_codex_sandbox", lambda: True)
    monkeypatch.setenv("CODEX_SESSION_ID", "launch-id")
    monkeypatch.setattr("pane_registry.load_registry_by_session_id", lambda _launch: {"live_sessions": []})
    monkeypatch.setattr("live_sessions.read_inventory", lambda _record: _Inventory())

    assert ping.main() == 1
    assert "cannot verify" in capsys.readouterr().err.lower()


def test_live_id_and_explicit_file_are_rejected_together(monkeypatch, capsys) -> None:
    ping = _load_ping_module()
    monkeypatch.setattr(sys, "argv", ["ccb-ping", "codex", "--live-id", "live-a", "--session-file", "/tmp/.codex-session"])
    monkeypatch.setattr(ping, "inside_managed_codex_sandbox", lambda: False)

    assert ping.main() == 1
    assert "cannot be combined" in capsys.readouterr().err.lower()


def test_paired_ping_autostarts_and_reports_queue(monkeypatch, tmp_path: Path, capsys) -> None:
    ping = _load_ping_module()
    from live_sessions import LiveSession

    sessions = [
        LiveSession("live-a", "codex", "launch-id", pane_id="%12", session_file=str(tmp_path / "a")),
        LiveSession("live-b", "codex", "launch-id", pane_id="%13", session_file=str(tmp_path / "b")),
    ]
    for name in ("a", "b"):
        (tmp_path / name).write_text(json.dumps({
            "active": "True", "session_id": name,
            "runtime_dir": str(tmp_path), "input_fifo": str(tmp_path / "input.fifo"),
        }))
    class _Inventory:
        valid = True
        def __init__(self):
            self.sessions = sessions

    monkeypatch.setenv("CODEX_SESSION_ID", "launch-id")
    monkeypatch.setattr("pane_registry.load_registry_by_session_id", lambda _launch: {"live_sessions": []})
    monkeypatch.setattr("live_sessions.read_inventory", lambda _record: _Inventory())
    monkeypatch.setattr("live_roles.roles_for_record", lambda _record: {})
    started = []
    monkeypatch.setattr(ping, "inside_managed_codex_sandbox", lambda: False)
    monkeypatch.setattr(sys, "argv", ["ccb-ping", "codex", "--autostart"])

    import askd_client
    monkeypatch.setattr(askd_client, "maybe_start_daemon", lambda *_args: started.append("start"))
    monkeypatch.setattr(askd_client, "wait_for_daemon_ready", lambda *_args: None)
    fake = types.ModuleType("codex_comm")
    class _Communicator:
        def ping(self, display=False): return True, "✅ healthy"
    fake.CodexCommunicator = _Communicator
    monkeypatch.setitem(sys.modules, "codex_comm", fake)
    monkeypatch.setattr(ping, "resolve_daemon_work_dir", lambda work_dir: work_dir)
    monkeypatch.setattr(ping, "_queue_report", lambda *_args: (False, "queue: empty"))

    rc = ping.main()

    assert rc == 0
    assert started == ["start"]
    assert "queue: empty" in capsys.readouterr().out
