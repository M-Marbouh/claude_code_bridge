"""A launch write must leave exactly one launch identity in the provider file.

`ccb-role` refuses a binding whose `session_id` and `ccb_session_id` name two
different launches (`live_roles._session_binding_is_current`). laskd rewrites
`.claude-session` from the registry and writes the CCB launch id under
`ccb_session_id`, so a relaunch that only restamped `session_id` inherited the
previous launch's `ccb_session_id` and could never claim a role.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from live_roles import RoleMember, _session_binding_is_current
from test_ccb_agent_composition import _load_ccb_module

NEW_LAUNCH = "ai-1793971548-867082"

# Shape and values taken from a real .ccb/.claude-session observed after laskd
# had rewritten it (only the work_dir path is neutralised): both launch-id
# names still name the previous launch.
STALE_CLAUDE_BINDING = {
    "ccb_project_id": "f8cc696c6fe8102f7f96e61fb986e6f989639baba0456d9919a111a164e1be2f",
    "ccb_session_id": "ai-1793922344-4124109",
    "work_dir": "/home/user/project",
    "terminal": "tmux",
    "pane_id": "%1",
    "pane_title_marker": "CCB-Claude-f8cc696c",
    "claude_session_id": "4ca022d8-fa55-4caa-88d1-9f303a414a1c",
    "old_claude_session_id": "a971d925-d525-42a1-b186-be9695b99d04",
    "old_updated_at": "2026-11-05 23:50:39",
    "active": True,
    "session_id": "ai-1793922344-4124109",
}


def _launcher(ccb, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, providers, *, resume=False):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(exist_ok=True)
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")
    launcher = ccb.AILauncher(providers=providers, resume=resume)
    launcher.session_id = NEW_LAUNCH
    launcher.runtime_dir = tmp_path / "runtime"
    launcher.runtime_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(launcher, "_maybe_start_provider_daemon", lambda _: None)
    return launcher


def _write_old_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _read_session(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_claude_relaunch_restamps_both_launch_id_names(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    launcher = _launcher(ccb, monkeypatch, tmp_path, ["claude"])
    session_file = launcher._claude_session_file()
    _write_old_file(session_file, STALE_CLAUDE_BINDING)

    launcher._write_local_claude_session(pane_id="%1", terminal="tmux")

    data = _read_session(session_file)
    assert data["session_id"] == NEW_LAUNCH
    assert data["ccb_session_id"] == NEW_LAUNCH
    # Native Claude identity and every preserved field stay untouched.
    assert data["claude_session_id"] == STALE_CLAUDE_BINDING["claude_session_id"]
    assert data["old_claude_session_id"] == STALE_CLAUDE_BINDING["old_claude_session_id"]
    assert data["old_updated_at"] == STALE_CLAUDE_BINDING["old_updated_at"]
    assert data["pane_title_marker"] == STALE_CLAUDE_BINDING["pane_title_marker"]
    assert data["active"] is True


def test_codex_relaunch_restamps_both_launch_id_names(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    launcher = _launcher(ccb, monkeypatch, tmp_path, ["codex"], resume=True)
    session_file = launcher._project_session_file(".codex-session")
    stale = {
        "session_id": "ai-1793922344-4124109",
        "ccb_session_id": "ai-1793922344-4124109",
        "codex_session_id": "native-codex-launch",
        "codex_session_path": "/old/codex/session.jsonl",
        "tmux_log": "/old/log",
        "started_at": "2026-11-05 23:50:39",
        "active": True,
    }
    _write_old_file(session_file, stale)
    runtime = launcher.runtime_dir / "codex"
    runtime.mkdir()

    assert launcher._write_codex_session(
        runtime, "ccb-old", runtime / "in", runtime / "out",
        pane_id="%1", pane_title_marker="CCB-Codex-x",
    )

    data = _read_session(session_file)
    assert data["session_id"] == NEW_LAUNCH
    assert data["ccb_session_id"] == NEW_LAUNCH
    # Native Codex binding and the preserved fields stay untouched.
    assert data["codex_session_id"] == stale["codex_session_id"]
    assert data["codex_session_path"] == stale["codex_session_path"]
    assert data["pane_id"] == "%1"
    assert data["active"] is True


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_relaunch_without_a_previous_launch_id_writes_both_names(
    monkeypatch, tmp_path, provider
):
    """Choice: both names are written, never only `session_id`, so a later
    laskd rewrite and the next relaunch both see one identity."""
    ccb = _load_ccb_module()
    launcher = _launcher(ccb, monkeypatch, tmp_path, [provider])
    if provider == "claude":
        session_file = launcher._claude_session_file()
        _write_old_file(session_file, {"active": True})
        launcher._write_local_claude_session(pane_id="%1", terminal="tmux")
    else:
        session_file = launcher._project_session_file(".codex-session")
        _write_old_file(session_file, {"active": True})
        runtime = launcher.runtime_dir / "codex"
        runtime.mkdir()
        assert launcher._write_codex_session(
            runtime, "ccb", runtime / "in", runtime / "out",
            pane_id="%1", pane_title_marker="CCB-Codex-x",
        )

    data = _read_session(session_file)
    assert data["session_id"] == NEW_LAUNCH
    assert data["ccb_session_id"] == NEW_LAUNCH


def test_claude_relaunch_binding_stops_contradicting_itself(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    launcher = _launcher(ccb, monkeypatch, tmp_path, ["claude"])
    session_file = launcher._claude_session_file()
    # Exactly what the broken writer left behind: a new session_id beside the
    # previous launch's ccb_session_id.
    contradicted = dict(STALE_CLAUDE_BINDING)
    contradicted["session_id"] = NEW_LAUNCH
    _write_old_file(session_file, contradicted)

    member = RoleMember(
        member_id="member-claude",
        provider="claude",
        launch_id=NEW_LAUNCH,
        pane_id="%1",
        terminal="tmux",
        session_file=str(session_file),
    )
    record = {"ccb_session_id": NEW_LAUNCH, "terminal": "tmux"}
    ok, detail = _session_binding_is_current(record, member)
    assert not ok and "contradicts itself" in detail

    launcher._write_local_claude_session(pane_id="%1", terminal="tmux")

    ok, detail = _session_binding_is_current(record, member)
    assert ok, detail


def test_two_different_launch_ids_are_still_refused(tmp_path):
    session_file = tmp_path / ".ccb" / ".claude-session"
    _write_old_file(
        session_file,
        {"session_id": NEW_LAUNCH, "ccb_session_id": "ai-some-other-launch",
         "terminal": "tmux", "pane_id": "%1", "active": True},
    )
    member = RoleMember(
        member_id="member-claude",
        provider="claude",
        launch_id=NEW_LAUNCH,
        pane_id="%1",
        terminal="tmux",
        session_file=str(session_file),
    )

    ok, detail = _session_binding_is_current({"ccb_session_id": NEW_LAUNCH}, member)

    assert not ok and detail == "session binding contradicts itself"
