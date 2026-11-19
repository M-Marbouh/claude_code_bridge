"""The CCB side of Pi readiness: every Pi launch carries its own state channel.

The reader in `pi_comm` trusts a snapshot only when it names the token of the launch that started
the pane, so the pane's environment and the `.pi-session` binding must agree on exactly one
(state file, token) pair, and the launch command must load the extension that publishes it.
"""
# This repo puts lib/ on sys.path at runtime (test/conftest.py) and ships no pyright
# configuration, so the workspace-rooted analyzer cannot see these flat imports.
# pyright: reportMissingImports=false
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import uuid
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

EXTENSION = REPO / "lib" / "pi_extension" / "ccb-state.ts"


def _ccb_module():
    loader = SourceFileLoader("ccb_script_pi_launch", str(REPO / "ccb"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _launcher(tmp_path: Path, *, providers: tuple[str, ...] = ("pi",)):
    """A launcher with only the state this wiring touches (no registry, no terminal)."""
    launcher = object.__new__(_ccb_module().AILauncher)
    launcher.script_dir = REPO
    launcher.runtime_dir = tmp_path / "rt"
    launcher.project_root = tmp_path
    launcher.invocation_dir = tmp_path
    launcher.project_id = "abc12345"
    launcher.providers = list(providers)
    launcher._live_slots = []
    launcher._launch_slot = 0
    launcher.launch_env = {}
    launcher.launch_args = {}
    launcher.ccb_pid = os.getpid()
    launcher.session_id = "ai-test-launch"
    launcher.terminal_type = "tmux"
    launcher._member_credential = lambda _provider: ("", "")
    # Nothing in this file starts a daemon or prunes records; the binding write is the subject.
    launcher._askd_checked = True
    launcher._auto_prune_done = True
    launcher._pi_binding = lambda: ("pi-session-1", tmp_path / "sessions")
    (tmp_path / ".ccb").mkdir(exist_ok=True)
    return launcher


# The record from the 2026-11-19 incident. The session id and the field names are verbatim; the
# project-anchored fields are re-homed onto the tmp project, because the resume path compares them
# against the project it is launching in.
PRODUCTION_SESSION_ID = "aea1925b-cfe0-4dcc-bd59-ed2b7dd96003"


def _resume_launcher(tmp_path: Path, *, resume: bool = True):
    """The real `_pi_binding` path; `_launcher` stubs it for the state-channel tests."""
    launcher = _launcher(tmp_path)
    del launcher._pi_binding
    launcher.resume = resume
    return launcher


def _record_binding(
    tmp_path: Path,
    session_dir: Path,
    *,
    session_id: str = PRODUCTION_SESSION_ID,
    work_dir: Path | None = None,
    project_id: str | None = None,
) -> Path:
    """Write a `.pi-session` the way `_write_pi_session` does, with a chosen anchor."""
    if project_id is None:
        project_id = _ccb_module().compute_ccb_project_id(tmp_path)
    data = {
        "session_id": "ai-test-launch",
        "ccb_project_id": project_id,
        "runtime_dir": str(tmp_path / "rt" / "pi"),
        "terminal": "tmux",
        "tmux_session": None,
        "pane_id": "%7",
        "pane_title_marker": "CCB-Pi-abc12345",
        "work_dir": str(work_dir or tmp_path),
        "start_dir": str(tmp_path),
        "active": True,
        "started_at": "2026-11-19 15:36:52",
        "start_cmd": "pi --no-approve",
        "pi_session_id": session_id,
        "pi_session_dir": str(session_dir),
        "ccb_managed_launch": True,
        "ccb_launch_caller": "pi",
        "ccb_launch_provider": "pi",
    }
    path = tmp_path / ".ccb" / ".pi-session"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _header(*, cwd: Path, session_id: str = PRODUCTION_SESSION_ID, **overrides) -> dict:
    """A first-row header shaped like the real one Pi writes."""
    header = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-11-19T15:36:52.000Z",
        "cwd": str(cwd),
    }
    header.update(overrides)
    return header


def _transcript(session_dir: Path, name: str, header: dict) -> Path:
    """Pi keys its lookup on this header, so every fixture transcript carries one."""
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / name
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    return path


def test_the_pi_pane_environment_carries_one_state_channel_per_launch(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    env = launcher._provider_env_overrides("pi")

    state_file = Path(env["CCB_PI_STATE_FILE"])
    assert state_file == tmp_path / "rt" / "pi" / "pi-state.json"
    assert len(env["CCB_PI_LAUNCH_TOKEN"]) == 32
    assert stat.S_IMODE(state_file.parent.stat().st_mode) == 0o700
    # The pane env is rebuilt for every pane: the same launch must keep one token, because the
    # binding is written separately and has to match what this Pi was started with.
    again = launcher._provider_env_overrides("pi")
    assert again["CCB_PI_LAUNCH_TOKEN"] == env["CCB_PI_LAUNCH_TOKEN"]
    assert again["CCB_PI_STATE_FILE"] == env["CCB_PI_STATE_FILE"]


def test_a_new_launch_gets_its_own_token_and_drops_the_previous_snapshot(tmp_path: Path) -> None:
    first = _launcher(tmp_path)._provider_env_overrides("pi")
    state_file = Path(first["CCB_PI_STATE_FILE"])
    state_file.write_text('{"v": 1}\n', encoding="utf-8")

    second = _launcher(tmp_path)._provider_env_overrides("pi")

    assert second["CCB_PI_LAUNCH_TOKEN"] != first["CCB_PI_LAUNCH_TOKEN"]
    # A snapshot from the previous launch must never be readable as this launch's readiness.
    assert not state_file.exists()


def test_other_providers_get_no_pi_state_channel(tmp_path: Path) -> None:
    env = _launcher(tmp_path, providers=("codex",))._provider_env_overrides("codex")
    assert "CCB_PI_STATE_FILE" not in env
    assert "CCB_PI_LAUNCH_TOKEN" not in env


def test_the_pi_start_command_loads_the_shipped_extension(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    command = launcher._get_start_cmd("pi")

    assert command.agent_command.startswith("pi --no-approve --session-id pi-session-1")
    assert f"--extension {EXTENSION}" in command.agent_command
    assert EXTENSION.is_file()


def test_a_missing_extension_still_starts_pi_but_warns(tmp_path: Path, capsys) -> None:
    launcher = _launcher(tmp_path)
    launcher.script_dir = tmp_path / "empty-install"
    command = launcher._get_start_cmd("pi")

    # Launching without the extension is not fatal: the reader refuses that pane instead.
    assert "--extension" not in command.agent_command
    assert "Pi state extension missing" in capsys.readouterr().err


def test_the_binding_records_the_same_channel_its_pane_was_given(tmp_path: Path, monkeypatch) -> None:
    ccb = _ccb_module()
    launcher = _launcher(tmp_path)
    monkeypatch.setattr(ccb, "upsert_registry", lambda _record: True)
    env = launcher._provider_env_overrides("pi")

    assert launcher._write_pi_session(tmp_path / "rt" / "pi", pane_id="%7", pane_title_marker="CCB-Pi-abc12345") is True

    data = json.loads((tmp_path / ".ccb" / ".pi-session").read_text(encoding="utf-8"))
    assert data["pi_launch_token"] == env["CCB_PI_LAUNCH_TOKEN"]
    assert data["pi_state_file"] == env["CCB_PI_STATE_FILE"]
    assert data["pi_session_id"] == "pi-session-1"
    assert data["pane_id"] == "%7"


# --- `ccb -r` resume of the recorded Pi session -------------------------------------------------


def _fresh_id(command: str) -> str:
    """The session id a start command carries, asserted to be a real uuid."""
    parts = command.split()
    session_id = parts[parts.index("--session-id") + 1]
    assert session_id != PRODUCTION_SESSION_ID
    uuid.UUID(session_id)
    return session_id


def test_resume_reuses_the_recorded_pi_session(tmp_path: Path, monkeypatch, capsys) -> None:
    session_dir = tmp_path / "sessions" / "ccb-recorded"
    _transcript(session_dir, f"fake_{PRODUCTION_SESSION_ID}.jsonl", _header(cwd=tmp_path))
    _record_binding(tmp_path, session_dir)
    launcher = _resume_launcher(tmp_path)

    assert launcher._pi_binding() == (PRODUCTION_SESSION_ID, session_dir)
    command = launcher._get_start_cmd("pi")

    assert f"--session-id {PRODUCTION_SESSION_ID}" in command.agent_command
    assert f"--session-dir {session_dir}" in command.agent_command
    out = capsys.readouterr().out
    assert out.count("Resuming Pi session") == 1
    assert "Resuming Pi session: aea1925b" in out

    # The binding is re-read when it is written: still one resume line, still the same session.
    ccb = _ccb_module()
    monkeypatch.setattr(ccb, "upsert_registry", lambda _record: True)
    assert launcher._write_pi_session(tmp_path / "rt" / "pi", pane_id="%7") is True
    assert capsys.readouterr().out.count("Resuming Pi session") == 0
    data = json.loads((tmp_path / ".ccb" / ".pi-session").read_text(encoding="utf-8"))
    assert data["pi_session_id"] == PRODUCTION_SESSION_ID
    assert data["pi_session_dir"] == str(session_dir)


def test_a_binding_recorded_for_another_project_is_discarded(tmp_path: Path, capsys) -> None:
    other = tmp_path / "other-project"
    (other / ".ccb").mkdir(parents=True)
    session_dir = tmp_path / "sessions" / "ccb-other"
    _transcript(session_dir, f"fake_{PRODUCTION_SESSION_ID}.jsonl", _header(cwd=other))
    _record_binding(
        tmp_path,
        session_dir,
        work_dir=other,
        project_id=_ccb_module().compute_ccb_project_id(other),
    )

    command = _resume_launcher(tmp_path)._get_start_cmd("pi")

    assert _fresh_id(command.agent_command)
    out = capsys.readouterr().out
    assert "Resuming Pi session" not in out
    assert out.count("discarding recorded session aea1925b") == 1
    assert "recorded for another project" in out


def test_a_deleted_session_directory_falls_back_to_a_fresh_id(tmp_path: Path, capsys) -> None:
    _record_binding(tmp_path, tmp_path / "sessions" / "gone")

    command = _resume_launcher(tmp_path)._get_start_cmd("pi")

    assert _fresh_id(command.agent_command)
    out = capsys.readouterr().out
    assert "Resuming Pi session" not in out
    assert "session directory is gone" in out


def test_an_existing_directory_without_a_transcript_is_not_a_resume(tmp_path: Path, capsys) -> None:
    # Pi opens a new session when the id lookup misses, so the directory alone proves nothing.
    session_dir = tmp_path / "sessions" / "ccb-empty"
    session_dir.mkdir(parents=True)
    _record_binding(tmp_path, session_dir)

    command = _resume_launcher(tmp_path)._get_start_cmd("pi")

    assert _fresh_id(command.agent_command)
    out = capsys.readouterr().out
    assert "Resuming Pi session" not in out
    assert "no single transcript for it" in out


def test_two_transcripts_for_one_id_are_ambiguous(tmp_path: Path, capsys) -> None:
    session_dir = tmp_path / "sessions" / "ccb-two"
    _transcript(session_dir, f"fake_{PRODUCTION_SESSION_ID}.jsonl", _header(cwd=tmp_path))
    _transcript(session_dir, f"other_{PRODUCTION_SESSION_ID}.jsonl", _header(cwd=tmp_path))
    _record_binding(tmp_path, session_dir)

    command = _resume_launcher(tmp_path)._get_start_cmd("pi")

    assert _fresh_id(command.agent_command)
    out = capsys.readouterr().out
    assert "Resuming Pi session" not in out
    assert "no single transcript for it" in out


def test_an_empty_transcript_file_is_not_a_resume(tmp_path: Path, capsys) -> None:
    session_dir = tmp_path / "sessions" / "ccb-blank"
    session_dir.mkdir(parents=True)
    (session_dir / f"fake_{PRODUCTION_SESSION_ID}.jsonl").write_text("", encoding="utf-8")
    _record_binding(tmp_path, session_dir)

    command = _resume_launcher(tmp_path)._get_start_cmd("pi")

    assert _fresh_id(command.agent_command)
    out = capsys.readouterr().out
    assert "Resuming Pi session" not in out
    assert "transcript header does not match" in out


def test_a_transcript_recorded_for_another_directory_is_not_a_resume(tmp_path: Path, capsys) -> None:
    # Pi's own lookup keys on the header cwd: a filename match from elsewhere resumes nothing.
    foreign = tmp_path / "somewhere-else"
    foreign.mkdir()
    session_dir = tmp_path / "sessions" / "ccb-foreign-cwd"
    _transcript(session_dir, f"fake_{PRODUCTION_SESSION_ID}.jsonl", _header(cwd=foreign))
    _record_binding(tmp_path, session_dir)

    command = _resume_launcher(tmp_path)._get_start_cmd("pi")

    assert _fresh_id(command.agent_command)
    out = capsys.readouterr().out
    assert "Resuming Pi session" not in out
    assert "transcript header does not match" in out


def test_a_transcript_whose_header_names_another_session_is_not_a_resume(tmp_path: Path, capsys) -> None:
    session_dir = tmp_path / "sessions" / "ccb-foreign-id"
    _transcript(
        session_dir,
        f"fake_{PRODUCTION_SESSION_ID}.jsonl",
        _header(cwd=tmp_path, id="0f0f0f0f-1111-4222-8333-444455556666"),
    )
    _record_binding(tmp_path, session_dir)

    command = _resume_launcher(tmp_path)._get_start_cmd("pi")

    assert _fresh_id(command.agent_command)
    out = capsys.readouterr().out
    assert "Resuming Pi session" not in out
    assert "transcript header does not match" in out


def test_an_unreadable_recorded_directory_is_a_stale_binding(tmp_path: Path, capsys) -> None:
    # Past NAME_MAX, Path.is_dir() raises OSError(ENAMETOOLONG); the launch must still start.
    _record_binding(tmp_path, tmp_path / ("d" * 300))

    command = _resume_launcher(tmp_path)._get_start_cmd("pi")

    assert _fresh_id(command.agent_command)
    out = capsys.readouterr().out
    assert "Resuming Pi session" not in out
    assert "session directory is unreadable" in out


def test_without_resume_a_recorded_binding_is_left_alone(tmp_path: Path, monkeypatch, capsys) -> None:
    agent_dir = tmp_path / "agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    session_dir = tmp_path / "sessions" / "ccb-recorded"
    _transcript(session_dir, f"fake_{PRODUCTION_SESSION_ID}.jsonl", _header(cwd=tmp_path))
    _record_binding(tmp_path, session_dir)

    launcher = _resume_launcher(tmp_path, resume=False)
    command = launcher._get_start_cmd("pi")

    fresh = _fresh_id(command.agent_command)
    assert f"--session-dir {agent_dir / 'sessions'}" in command.agent_command
    out = capsys.readouterr().out
    assert "Resuming Pi session" not in out
    assert "discarding recorded session" not in out
    assert "No Pi history found" not in out
    assert fresh


def test_resume_without_a_binding_starts_fresh(tmp_path: Path, capsys) -> None:
    command = _resume_launcher(tmp_path)._get_start_cmd("pi")

    assert _fresh_id(command.agent_command)
    out = capsys.readouterr().out
    assert "Resuming Pi session" not in out
    assert "discarding recorded session" not in out
    assert "No Pi history found, starting fresh" in out


def test_a_second_launch_resumes_what_the_first_one_recorded(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    ccb = _ccb_module()
    monkeypatch.setattr(ccb, "upsert_registry", lambda _record: True)

    first = _resume_launcher(tmp_path, resume=False)
    first_cmd = first._get_start_cmd("pi")
    assert first._write_pi_session(tmp_path / "rt" / "pi", pane_id="%1") is True
    first_id = json.loads((tmp_path / ".ccb" / ".pi-session").read_text(encoding="utf-8"))["pi_session_id"]
    assert f"--session-id {first_id}" in first_cmd.agent_command
    # Pi created the transcript while the first launch ran.
    first_dir = tmp_path / "agent" / "sessions" / f"ccb-{ccb.compute_ccb_project_id(tmp_path)}"
    _transcript(first_dir, f"fake_{first_id}.jsonl", _header(cwd=tmp_path, session_id=first_id))
    capsys.readouterr()

    second = _resume_launcher(tmp_path)
    second_cmd = second._get_start_cmd("pi")

    assert f"--session-id {first_id}" in second_cmd.agent_command
    io = json.loads((tmp_path / ".ccb" / ".pi-session").read_text(encoding="utf-8"))
    assert second._pi_binding() == (first_id, Path(io["pi_session_dir"]))
    out = capsys.readouterr().out
    assert out.count("Resuming Pi session") == 1
    assert first_id[:8] in out
