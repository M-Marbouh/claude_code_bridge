from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import Mock

import codex_comm
import pane_registry


def _record(work_dir: Path, destination: Path, *, active: bool = True) -> dict:
    return {
        "ccb_session_id": "ai-1-2",
        "ccb_project_id": "project-id",
        "work_dir": str(work_dir),
        "terminal": "tmux",
        "updated_at": int(time.time()),
        "live_sessions": [
            {"live_id": "codex-a", "provider": "codex", "session_file": "/tmp/a/.codex-session", "active": active},
            {"live_id": "codex-b", "provider": "codex", "session_file": "/tmp/b/.codex-session", "active": active},
            {"live_id": "claude-a", "provider": "claude", "session_file": "/tmp/c/.claude-session", "active": True},
        ],
        "providers": {},
    }


def _run(
    monkeypatch,
    tmp_path: Path,
    *,
    cwd: Path | None = None,
    record: dict | None = None,
    binding_dir: str = ".ccb",
):
    work_dir = tmp_path / "project"
    ccb_dir = work_dir / binding_dir
    ccb_dir.mkdir(parents=True)
    destination = ccb_dir / ".codex-session"
    original = '{"terminal":"wezterm","pane_id":"12","ended_at":"old","codex_session_id":"A"}\n'
    destination.write_text(original, encoding="utf-8")
    reg_dir = tmp_path / "registry"
    reg_dir.mkdir()
    monkeypatch.setattr(pane_registry, "_registry_dir", lambda: reg_dir)
    if record is not None:
        record["work_dir"] = str(work_dir)
        record["ccb_project_id"] = codex_comm.compute_ccb_project_id(work_dir)
        (reg_dir / "ccb-session-one.json").write_text(json.dumps(record), encoding="utf-8")

    calls = {"binding": 0, "transfer": 0}

    class Session:
        def update_codex_log_binding(self, **_kwargs):
            calls["binding"] += 1
            calls["transfer"] += 1

    log = tmp_path / "rollout.jsonl"
    log.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(cwd or work_dir)}}) + "\n", encoding="utf-8")
    monkeypatch.setattr(codex_comm, "find_project_session_file", lambda *_args: destination)
    monkeypatch.setattr(codex_comm, "_codex_log_is_descendant", lambda _path: False)
    monkeypatch.setattr("caskd_session.load_project_session", lambda *_args: Session())
    codex_comm._handle_codex_log_event(log)
    return calls, destination.read_text(encoding="utf-8"), original, work_dir


def test_paired_inventory_refuses_root_and_subdirectory_rollouts(monkeypatch, tmp_path):
    work_dir = tmp_path / "project"
    destination = work_dir / ".ccb" / ".codex-session"
    record = _record(work_dir, destination)
    for cwd in (None, work_dir / "lib"):
        calls, content, original, _ = _run(monkeypatch, tmp_path / str(cwd), cwd=cwd, record=record)
        assert calls == {"binding": 0, "transfer": 0}
        assert content == original


def test_gate_does_not_probe_registry_or_pane_liveness(monkeypatch, tmp_path):
    record = _record(tmp_path / "project", tmp_path / "project/.ccb/.codex-session")
    owner_alive = Mock(return_value=False)
    provider_alive = Mock(return_value=False)
    pid_alive = Mock(return_value=False)
    monkeypatch.setattr(pane_registry, "_registry_owner_alive", owner_alive)
    monkeypatch.setattr(pane_registry, "_provider_pane_alive", provider_alive)
    monkeypatch.setattr(pane_registry, "_is_pid_alive", pid_alive)
    calls, *_ = _run(monkeypatch, tmp_path, record=record)
    assert calls == {"binding": 0, "transfer": 0}
    assert owner_alive.call_count == 0
    assert provider_alive.call_count == 0
    assert pid_alive.call_count == 0


def test_invalid_inventory_refuses_binding(monkeypatch, tmp_path):
    record = _record(tmp_path / "project", tmp_path / "project/.ccb/.codex-session")
    record["live_sessions"] = "broken"
    calls, *_ = _run(monkeypatch, tmp_path, record=record)
    assert calls == {"binding": 0, "transfer": 0}


def test_inactive_codex_members_allow_binding(monkeypatch, tmp_path):
    record = _record(tmp_path / "project", tmp_path / "project/.ccb/.codex-session", active=False)
    calls, *_ = _run(monkeypatch, tmp_path, record=record)
    assert calls == {"binding": 1, "transfer": 1}


def test_malformed_active_flags_are_treated_as_active(monkeypatch, tmp_path):
    for flag in (None, "maybe"):
        record = _record(tmp_path / str(flag), tmp_path / str(flag) / ".ccb" / ".codex-session")
        for member in record["live_sessions"][:2]:
            member["active"] = flag
        calls, *_ = _run(monkeypatch, tmp_path / str(flag), record=record)
        assert calls == {"binding": 0, "transfer": 0}


def test_ccb_config_destination_refuses_paired_binding(monkeypatch, tmp_path):
    work_dir = tmp_path / "project"
    destination = work_dir / ".ccb_config" / ".codex-session"
    record = _record(work_dir, destination)
    calls, *_ = _run(monkeypatch, tmp_path, record=record, binding_dir=".ccb_config")
    assert calls == {"binding": 0, "transfer": 0}


def test_single_launch_inventory_with_matching_file_allows_binding(monkeypatch, tmp_path):
    work_dir = tmp_path / "project"
    destination = work_dir / ".ccb" / ".codex-session"
    record = _record(work_dir, destination)
    record["live_sessions"] = [
        {"live_id": "codex-only", "provider": "codex", "session_file": str(destination), "active": True}
    ]
    calls, *_ = _run(monkeypatch, tmp_path, record=record)
    assert calls == {"binding": 1, "transfer": 1}


def test_legacy_and_missing_registry_allow_binding(monkeypatch, tmp_path):
    work_dir = tmp_path / "project"
    destination = work_dir / ".ccb" / ".codex-session"
    legacy = {"ccb_session_id": "old", "work_dir": str(work_dir), "updated_at": int(time.time()),
              "providers": {"codex": {"session_file": str(destination)}}}
    calls, *_ = _run(monkeypatch, tmp_path / "legacy", record=legacy)
    assert calls == {"binding": 1, "transfer": 1}
    calls, *_ = _run(monkeypatch, tmp_path / "missing")
    assert calls == {"binding": 1, "transfer": 1}


def test_stale_paired_registry_allows_binding(monkeypatch, tmp_path):
    record = _record(tmp_path / "project", tmp_path / "project/.ccb/.codex-session")
    record["updated_at"] = int(time.time()) - pane_registry.REGISTRY_TTL_SECONDS - 1
    calls, *_ = _run(monkeypatch, tmp_path, record=record)
    assert calls == {"binding": 1, "transfer": 1}
