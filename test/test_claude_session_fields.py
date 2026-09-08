from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_comm import ClaudeCommunicator
from laskd_registry import LaskdSessionRegistry
from laskd_session import ClaudeProjectSession
from project_id import normalize_work_dir


def test_laskd_session_update_backfills_work_dir_fields(tmp_path: Path) -> None:
    cfg = tmp_path / ".ccb"
    cfg.mkdir(parents=True, exist_ok=True)
    session_file = cfg / ".claude-session"
    session_file.write_text("{}", encoding="utf-8")

    session = ClaudeProjectSession(
        session_file=session_file,
        data={
            "claude_session_id": "old-id",
            "claude_session_path": str(tmp_path / "old.jsonl"),
        },
    )
    session.update_claude_binding(session_path=tmp_path / "new-id.jsonl", session_id="new-id")

    data = json.loads(session_file.read_text(encoding="utf-8"))
    assert data["active"] is True
    assert data["work_dir"] == str(tmp_path)
    assert data["work_dir_norm"] == normalize_work_dir(str(tmp_path))


def test_registry_direct_update_backfills_work_dir_fields(tmp_path: Path) -> None:
    cfg = tmp_path / ".ccb"
    cfg.mkdir(parents=True, exist_ok=True)
    session_file = cfg / ".claude-session"
    session_file.write_text(json.dumps({"active": True}), encoding="utf-8")

    log_path = tmp_path / "new-id.jsonl"
    log_path.write_text("", encoding="utf-8")

    registry = LaskdSessionRegistry()
    registry._update_session_file_direct(session_file, log_path, "new-id")

    data = json.loads(session_file.read_text(encoding="utf-8"))
    assert data["claude_session_id"] == "new-id"
    assert data["claude_session_path"] == str(log_path)
    assert data["work_dir"] == str(tmp_path)
    assert data["work_dir_norm"] == normalize_work_dir(str(tmp_path))


def test_claude_comm_remember_backfills_work_dir_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / ".ccb"
    cfg.mkdir(parents=True, exist_ok=True)
    session_file = cfg / ".claude-session"
    session_file.write_text("{}", encoding="utf-8")

    log_path = tmp_path / "new-id.jsonl"
    log_path.write_text("", encoding="utf-8")

    comm = ClaudeCommunicator.__new__(ClaudeCommunicator)
    comm.project_session_file = str(session_file)
    comm.session_info = {"work_dir": str(tmp_path)}
    comm.session_id = "ccb-session-id"
    comm.terminal = "tmux"
    monkeypatch.setattr(ClaudeCommunicator, "_publish_registry", lambda self: None)

    comm._remember_claude_session(log_path)

    data = json.loads(session_file.read_text(encoding="utf-8"))
    assert data["claude_session_path"] == str(log_path)
    assert data["claude_session_id"] == "new-id"
    assert data["work_dir"] == str(tmp_path)
    assert data["work_dir_norm"] == normalize_work_dir(str(tmp_path))
