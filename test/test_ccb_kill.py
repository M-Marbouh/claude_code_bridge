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
    project_id = ccb.compute_ccb_project_id(tmp_path)
    marker = f"CCB-Codex-{project_id[:8]}"
    session_file.write_text(json.dumps({
        "active": True,
        "terminal": "tmux",
        "pane_id": "%1",
        "pane_title_marker": marker,
        "work_dir": str(tmp_path),
        "ccb_project_id": project_id,
    }, ensure_ascii=True), encoding="utf-8")

    killed: list[str] = []

    class _FakeTmuxBackend:
        def find_pane_by_title_marker(self, pane_marker: str, cwd_hint: str = "") -> str | None:
            return "%1" if pane_marker == marker and cwd_hint == str(tmp_path) else None

        def is_alive(self, pane_id: str) -> bool:
            return pane_id == "%1"

        def pane_matches_cwd_strict(self, pane_id: str, work_dir: str) -> bool:
            return pane_id == "%1" and work_dir == str(tmp_path)

        def pane_shares_window(self, pane_id: str, caller_pane_id: str) -> bool:
            return (pane_id, caller_pane_id) == ("%1", "%0")

        def kill_pane(self, pane_id: str) -> None:
            killed.append(pane_id)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMUX_PANE", "%0")
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


def test_cmd_kill_skips_inactive_stale_wezterm_pane(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    session_dir = tmp_path / ".ccb"
    session_dir.mkdir(parents=True)
    project_id = ccb.compute_ccb_project_id(tmp_path)
    session_file = session_dir / ".gemini-session"
    session_file.write_text(json.dumps({
        "active": False,
        "terminal": "wezterm",
        "pane_id": "2",
        "pane_title_marker": f"CCB-Gemini-{project_id[:8]}",
        "work_dir": str(tmp_path),
        "ccb_project_id": project_id,
    }), encoding="utf-8")

    class _NeverKillBackend:
        def __init__(self) -> None:
            raise AssertionError("inactive sessions must not inspect or kill live panes")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ccb, "WeztermBackend", _NeverKillBackend)

    rc = ccb.cmd_kill(SimpleNamespace(force=False, daemon=False, providers=["gemini"]))

    assert rc == 0


def test_cmd_kill_skips_live_wezterm_pane_with_wrong_cwd_when_marker_is_missing(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    session_dir = tmp_path / ".ccb"
    session_dir.mkdir(parents=True)
    project_id = ccb.compute_ccb_project_id(tmp_path)
    marker = f"CCB-Codex-{project_id[:8]}"
    session_file = session_dir / ".codex-session"
    session_file.write_text(json.dumps({
        "active": True,
        "terminal": "wezterm",
        "pane_id": "7",
        "pane_title_marker": marker,
        "work_dir": str(tmp_path),
        "ccb_project_id": project_id,
    }), encoding="utf-8")
    killed: list[str] = []

    class _FakeWeztermBackend:
        def find_pane_by_title_marker(self, _marker: str, _cwd_hint: str = "") -> None:
            return None

        def is_alive(self, pane_id: str) -> bool:
            return pane_id == "7"

        def pane_matches_cwd_strict(self, _pane_id: str, _work_dir: str) -> bool:
            return False

        def kill_pane(self, pane_id: str) -> None:
            killed.append(pane_id)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ccb, "WeztermBackend", _FakeWeztermBackend)

    rc = ccb.cmd_kill(SimpleNamespace(force=False, daemon=False, providers=["codex"]))

    assert rc == 0
    assert killed == []
    assert json.loads(session_file.read_text(encoding="utf-8"))["active"] is True


def test_cmd_kill_kills_verified_active_wezterm_pane(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    session_dir = tmp_path / ".ccb"
    session_dir.mkdir(parents=True)
    project_id = ccb.compute_ccb_project_id(tmp_path)
    marker = f"CCB-Codex-{project_id[:8]}"
    session_id = "ai-test-current"
    session_file = session_dir / ".codex-session"
    session_file.write_text(json.dumps({
        "active": True,
        "session_id": session_id,
        "terminal": "wezterm",
        "pane_id": "7",
        "pane_title_marker": marker,
        "work_dir": str(tmp_path),
        "ccb_project_id": project_id,
    }), encoding="utf-8")
    killed: list[str] = []

    class _FakeWeztermBackend:
        def find_pane_by_title_marker(self, pane_marker: str, cwd_hint: str = "") -> str | None:
            return "7" if pane_marker == marker and cwd_hint == str(tmp_path) else None

        def is_alive(self, pane_id: str) -> bool:
            return pane_id == "7"

        def pane_matches_cwd_strict(self, pane_id: str, work_dir: str) -> bool:
            return pane_id == "7" and work_dir == str(tmp_path)

        def pane_shares_window(self, pane_id: str, caller_pane_id: str) -> bool:
            return (pane_id, caller_pane_id) == ("7", "6")

        def kill_pane(self, pane_id: str) -> None:
            killed.append(pane_id)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEZTERM_PANE", "6")
    monkeypatch.setattr(ccb, "WeztermBackend", _FakeWeztermBackend)
    monkeypatch.setattr(
        ccb,
        "load_registry_by_session_id",
        lambda requested: {
            "ccb_session_id": requested,
            "ccb_project_id": project_id,
            "work_dir": str(tmp_path),
            "providers": {
                "codex": {
                    "pane_id": "7",
                    "pane_title_marker": marker,
                },
            },
        } if requested == session_id else None,
    )

    rc = ccb.cmd_kill(SimpleNamespace(force=False, daemon=False, providers=["codex"]))

    assert rc == 0
    assert killed == ["7"]
    assert json.loads(session_file.read_text(encoding="utf-8"))["active"] is False


def test_cmd_kill_rejects_conflicting_session_registry(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    session_dir = tmp_path / ".ccb"
    session_dir.mkdir(parents=True)
    project_id = ccb.compute_ccb_project_id(tmp_path)
    marker = f"CCB-Codex-{project_id[:8]}"
    session_id = "ai-test-current"
    session_file = session_dir / ".codex-session"
    session_file.write_text(json.dumps({
        "active": True,
        "session_id": session_id,
        "terminal": "wezterm",
        "pane_id": "7",
        "pane_title_marker": marker,
        "work_dir": str(tmp_path),
        "ccb_project_id": project_id,
    }), encoding="utf-8")

    class _NeverInspectBackend:
        def __init__(self) -> None:
            raise AssertionError("a conflicting registry must be rejected before pane inspection")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ccb, "WeztermBackend", _NeverInspectBackend)
    monkeypatch.setattr(
        ccb,
        "load_registry_by_session_id",
        lambda requested: {
            "ccb_session_id": requested,
            "ccb_project_id": project_id,
            "work_dir": str(tmp_path),
            "providers": {
                "codex": {
                    "pane_id": "8",
                    "pane_title_marker": marker,
                },
            },
        },
    )

    rc = ccb.cmd_kill(SimpleNamespace(force=False, daemon=False, providers=["codex"]))

    assert rc == 0
    assert json.loads(session_file.read_text(encoding="utf-8"))["active"] is True


def test_cmd_kill_accepts_marker_match_from_adjacent_wezterm_tab(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    session_dir = tmp_path / ".ccb"
    session_dir.mkdir(parents=True)
    project_id = ccb.compute_ccb_project_id(tmp_path)
    marker = f"CCB-Codex-{project_id[:8]}"
    session_file = session_dir / ".codex-session"
    session_file.write_text(json.dumps({
        "active": True,
        "terminal": "wezterm",
        "pane_id": "7",
        "pane_title_marker": marker,
        "work_dir": str(tmp_path),
        "ccb_project_id": project_id,
    }), encoding="utf-8")
    killed: list[str] = []

    class _FakeWeztermBackend:
        def find_pane_by_title_marker(self, _marker: str, _cwd_hint: str = "") -> str:
            return "7"

        def is_alive(self, _pane_id: str) -> bool:
            return True

        def pane_matches_cwd_strict(self, _pane_id: str, _work_dir: str) -> bool:
            return True

        def pane_shares_window(self, _pane_id: str, _caller_pane_id: str) -> bool:
            return False

        def kill_pane(self, pane_id: str) -> None:
            killed.append(pane_id)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEZTERM_PANE", "6")
    monkeypatch.setattr(ccb, "WeztermBackend", _FakeWeztermBackend)

    rc = ccb.cmd_kill(SimpleNamespace(force=False, daemon=False, providers=["codex"]))

    assert rc == 0
    assert killed == ["7"]
    assert json.loads(session_file.read_text(encoding="utf-8"))["active"] is False


def test_cmd_kill_accepts_overwritten_wezterm_title_without_caller_context(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    session_dir = tmp_path / ".ccb"
    session_dir.mkdir(parents=True)
    project_id = ccb.compute_ccb_project_id(tmp_path)
    marker = f"CCB-Codex-{project_id[:8]}"
    session_file = session_dir / ".codex-session"
    session_file.write_text(json.dumps({
        "active": True,
        "terminal": "wezterm",
        "pane_id": "7",
        "pane_title_marker": marker,
        "work_dir": str(tmp_path),
        "ccb_project_id": project_id,
    }), encoding="utf-8")
    killed: list[str] = []

    class _FakeWeztermBackend:
        def find_pane_by_title_marker(self, _marker: str, _cwd_hint: str = "") -> None:
            return None

        def is_alive(self, pane_id: str) -> bool:
            return pane_id == "7"

        def pane_matches_cwd_strict(self, pane_id: str, work_dir: str) -> bool:
            return pane_id == "7" and work_dir == str(tmp_path)

        def pane_shares_window(self, pane_id: str, caller_pane_id: str) -> bool:
            return (pane_id, caller_pane_id) == ("7", "6")

        def kill_pane(self, pane_id: str) -> None:
            killed.append(pane_id)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ccb, "WeztermBackend", _FakeWeztermBackend)

    rc = ccb.cmd_kill(SimpleNamespace(force=False, daemon=False, providers=["codex"]))

    assert rc == 0
    assert killed == ["7"]
    assert json.loads(session_file.read_text(encoding="utf-8"))["active"] is False


def test_cmd_kill_accepts_overwritten_wezterm_title_from_adjacent_tab(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    session_dir = tmp_path / ".ccb"
    session_dir.mkdir(parents=True)
    project_id = ccb.compute_ccb_project_id(tmp_path)
    marker = f"CCB-Codex-{project_id[:8]}"
    session_file = session_dir / ".codex-session"
    session_file.write_text(json.dumps({
        "active": True,
        "terminal": "wezterm",
        "pane_id": "7",
        "pane_title_marker": marker,
        "work_dir": str(tmp_path),
        "ccb_project_id": project_id,
    }), encoding="utf-8")
    killed: list[str] = []

    class _FakeWeztermBackend:
        def find_pane_by_title_marker(self, _marker: str, _cwd_hint: str = "") -> None:
            return None

        def is_alive(self, _pane_id: str) -> bool:
            return True

        def pane_matches_cwd_strict(self, _pane_id: str, _work_dir: str) -> bool:
            return True

        def pane_shares_window(self, _pane_id: str, _caller_pane_id: str) -> bool:
            return False

        def kill_pane(self, pane_id: str) -> None:
            killed.append(pane_id)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEZTERM_PANE", "6")
    monkeypatch.setattr(ccb, "WeztermBackend", _FakeWeztermBackend)

    rc = ccb.cmd_kill(SimpleNamespace(force=False, daemon=False, providers=["codex"]))

    assert rc == 0
    assert killed == ["7"]
    assert json.loads(session_file.read_text(encoding="utf-8"))["active"] is False


def test_cmd_kill_accepts_overwritten_tmux_title_from_adjacent_window(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    session_dir = tmp_path / ".ccb"
    session_dir.mkdir(parents=True)
    project_id = ccb.compute_ccb_project_id(tmp_path)
    marker = f"CCB-Codex-{project_id[:8]}"
    session_file = session_dir / ".codex-session"
    session_file.write_text(json.dumps({
        "active": True,
        "terminal": "tmux",
        "pane_id": "%7",
        "pane_title_marker": marker,
        "work_dir": str(tmp_path),
        "ccb_project_id": project_id,
    }), encoding="utf-8")
    killed: list[str] = []

    class _FakeTmuxBackend:
        def find_pane_by_title_marker(self, _marker: str, _cwd_hint: str = "") -> None:
            return None

        def is_alive(self, pane_id: str) -> bool:
            return pane_id == "%7"

        def pane_matches_cwd_strict(self, pane_id: str, work_dir: str) -> bool:
            return pane_id == "%7" and work_dir == str(tmp_path)

        def pane_shares_window(self, _pane_id: str, _caller_pane_id: str) -> bool:
            return False

        def kill_pane(self, pane_id: str) -> None:
            killed.append(pane_id)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMUX_PANE", "%6")
    monkeypatch.setattr(ccb, "TmuxBackend", _FakeTmuxBackend)

    rc = ccb.cmd_kill(SimpleNamespace(force=False, daemon=False, providers=["codex"]))

    assert rc == 0
    assert killed == ["%7"]
    assert json.loads(session_file.read_text(encoding="utf-8"))["active"] is False


def test_cmd_kill_refuses_legacy_whole_tmux_session(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    session_dir = tmp_path / ".ccb"
    session_dir.mkdir(parents=True)
    project_id = ccb.compute_ccb_project_id(tmp_path)
    session_file = session_dir / ".codex-session"
    session_file.write_text(json.dumps({
        "active": True,
        "terminal": "tmux",
        "pane_id": "ccb-shared-session",
        "tmux_session": "ccb-shared-session",
        "pane_title_marker": f"CCB-Codex-{project_id[:8]}",
        "work_dir": str(tmp_path),
        "ccb_project_id": project_id,
    }), encoding="utf-8")

    class _NeverKillBackend:
        def __init__(self) -> None:
            raise AssertionError("whole tmux sessions must not be inspected or killed")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ccb, "TmuxBackend", _NeverKillBackend)

    rc = ccb.cmd_kill(SimpleNamespace(force=False, daemon=False, providers=["codex"]))

    assert rc == 0


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
