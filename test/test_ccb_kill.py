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


def test_cmd_kill_enumerates_instance_files_and_filters_project(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    session_dir = tmp_path / ".ccb"
    session_dir.mkdir(parents=True, exist_ok=True)
    project_id = ccb.compute_ccb_project_id(tmp_path)

    def _write(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")

    _write(session_dir / ".codex-session", {
        "session_id": "run-1",
        "provider": "codex",
        "ccb_project_id": project_id,
        "terminal": "tmux",
        "pane_id": "%1",
        "active": True,
    })
    _write(session_dir / ".codex-worker-session", {
        "session_id": "run-1",
        "provider": "codex",
        "instance": "worker",
        "qualified_provider": "codex:worker",
        "ccb_project_id": project_id,
        "terminal": "tmux",
        "pane_id": "%2",
        "active": True,
    })
    _write(session_dir / ".claude-worker-session", {
        "session_id": "run-1",
        "provider": "claude",
        "instance": "worker",
        "qualified_provider": "claude:worker",
        "ccb_project_id": project_id,
        "terminal": "wezterm",
        "pane_id": "9",
        "active": True,
    })
    _write(session_dir / ".codex-other-session", {
        "session_id": "run-2",
        "provider": "codex",
        "instance": "other",
        "qualified_provider": "codex:other",
        "ccb_project_id": "foreign",
        "terminal": "tmux",
        "pane_id": "%3",
        "active": True,
    })

    killed_tmux: list[str] = []
    killed_wezterm: list[str] = []
    registry_updates: list[dict] = []

    class _FakeTmuxBackend:
        def is_alive(self, pane_id: str) -> bool:
            return True

        def pane_belongs_to_cwd(self, pane_id: str, work_dir: str) -> bool:
            return True

        def kill_pane(self, pane_id: str) -> None:
            killed_tmux.append(pane_id)

    class _FakeWeztermBackend:
        def is_alive(self, pane_id: str) -> bool:
            return True

        def pane_belongs_to_cwd(self, pane_id: str, work_dir: str) -> bool:
            return True

        def kill_pane(self, pane_id: str) -> None:
            killed_wezterm.append(pane_id)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ccb, "TmuxBackend", _FakeTmuxBackend)
    monkeypatch.setattr(ccb, "WeztermBackend", _FakeWeztermBackend)
    monkeypatch.setattr(ccb.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(ccb, "upsert_registry", lambda payload: registry_updates.append(payload) or True)

    rc = ccb.cmd_kill(SimpleNamespace(force=False, daemon=False, providers=[]))

    assert rc == 0
    assert set(killed_tmux) == {"%1", "%2"}
    assert killed_wezterm == ["9"]
    assert json.loads((session_dir / ".codex-other-session").read_text(encoding="utf-8"))["active"] is True
    assert json.loads((session_dir / ".codex-worker-session").read_text(encoding="utf-8"))["active"] is False
    updated_keys = {next(iter(update["providers"])) for update in registry_updates}
    assert {"codex", "codex:worker", "claude:worker"} <= updated_keys
    assert "codex:other" not in updated_keys


def test_cmd_kill_accepts_qualified_provider_arg(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    session_dir = tmp_path / ".ccb"
    session_dir.mkdir(parents=True, exist_ok=True)
    project_id = ccb.compute_ccb_project_id(tmp_path)
    for name, pane_id in {
        ".codex-session": "%1",
        ".codex-worker-session": "%2",
    }.items():
        (session_dir / name).write_text(
            json.dumps({
                "session_id": "run-1",
                "provider": "codex",
                "instance": "worker" if "worker" in name else None,
                "ccb_project_id": project_id,
                "terminal": "tmux",
                "pane_id": pane_id,
                "active": True,
            }),
            encoding="utf-8",
        )

    killed: list[str] = []

    class _FakeTmuxBackend:
        def is_alive(self, pane_id: str) -> bool:
            return True

        def pane_belongs_to_cwd(self, pane_id: str, work_dir: str) -> bool:
            return True

        def kill_pane(self, pane_id: str) -> None:
            killed.append(pane_id)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ccb, "TmuxBackend", _FakeTmuxBackend)
    monkeypatch.setattr(ccb.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(ccb, "upsert_registry", lambda payload: True)

    rc = ccb.cmd_kill(SimpleNamespace(force=False, daemon=False, providers=["codex:worker"]))

    assert rc == 0
    assert killed == ["%2"]
    assert json.loads((session_dir / ".codex-session").read_text(encoding="utf-8"))["active"] is True
    assert json.loads((session_dir / ".codex-worker-session").read_text(encoding="utf-8"))["active"] is False
