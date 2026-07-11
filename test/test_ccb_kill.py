from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace


def _load_ccb_module() -> object:
    repo_root = Path(__file__).resolve().parents[1]
    ccb_path = repo_root / "ccb"
    loader = SourceFileLoader("ccb_kill_script", str(ccb_path))
    spec = importlib.util.spec_from_loader("ccb_kill_script", loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cmd_kill_is_local_only_by_default(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    session_dir = tmp_path / ".ccb"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / ".codex-session"
    session_file.write_text(
        json.dumps({"active": True, "terminal": "tmux", "pane_id": "%1"}, ensure_ascii=True),
        encoding="utf-8",
    )

    killed: list[str] = []

    class _FakeTmuxBackend:
        def kill_pane(self, pane_id: str) -> None:
            killed.append(pane_id)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ccb, "TmuxBackend", _FakeTmuxBackend)
    monkeypatch.setattr(ccb.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(
        ccb,
        "shutdown_daemon",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("shutdown_daemon should not be called")),
    )

    rc = ccb.cmd_kill(SimpleNamespace(force=False, daemon=False, providers=["codex"]))

    assert rc == 0
    assert killed == ["%1"]
    data = json.loads(session_file.read_text(encoding="utf-8"))
    assert data["active"] is False
    assert data["ended_at"]


def test_cmd_kill_daemon_uses_shared_ask_prefix(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    askd_state = tmp_path / "run" / "askd.json"
    shutdown_calls: list[tuple[str, float, Path]] = []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ccb, "state_file_path", lambda _name: askd_state)
    monkeypatch.setattr(
        ccb,
        "read_state",
        lambda _path: {"parent_pid": 4242, "work_dir": str(tmp_path), "pid": 5252},
    )
    monkeypatch.setattr(
        ccb,
        "shutdown_daemon",
        lambda prefix, timeout_s, state_file: shutdown_calls.append((prefix, timeout_s, state_file)) or True,
    )
    monkeypatch.setattr(
        ccb,
        "_kill_pid",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("_kill_pid should not be called")),
    )

    rc = ccb.cmd_kill(SimpleNamespace(force=False, daemon=True, providers=["codex"]))

    assert rc == 0
    assert shutdown_calls == [("ask", 1.0, askd_state)]


def test_cmd_kill_daemon_skips_live_foreign_owner(monkeypatch, tmp_path: Path, capsys) -> None:
    ccb = _load_ccb_module()
    askd_state = tmp_path / "run" / "askd.json"
    other_project = tmp_path / "other-project"
    other_project.mkdir(parents=True, exist_ok=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ccb, "state_file_path", lambda _name: askd_state)
    monkeypatch.setattr(
        ccb,
        "read_state",
        lambda _path: {"parent_pid": 9999, "work_dir": str(other_project), "pid": 5252},
    )
    monkeypatch.setattr(ccb, "_is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        ccb,
        "shutdown_daemon",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("shutdown_daemon should not be called")),
    )

    rc = ccb.cmd_kill(SimpleNamespace(force=False, daemon=True, providers=["codex"]))

    assert rc == 0
    assert "another live project" in capsys.readouterr().out
