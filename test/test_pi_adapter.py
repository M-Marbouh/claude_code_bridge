from __future__ import annotations

import json
import threading
from itertools import count
from pathlib import Path

import pytest

from askd.adapters.base import ProviderRequest, QueuedTask
from askd.adapters.pi import PiAdapter
from pi_comm import load_session

FIXTURES = Path(__file__).parent / "fixtures" / "pi"


class FakeBackend:
    sent: list[tuple[str, str]]

    def __init__(self):
        self.sent = []

    def is_alive(self, _pane):
        return True

    def find_pane_by_title_marker(self, _marker, _cwd):
        return "%12"

    def get_pane_content(self, _pane, lines=40):
        return (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8")

    def send_text(self, pane, text):
        self.sent.append((pane, text))


def _task(work_dir: Path, req_id: str, *, has_actionable_waiting=None, cancel_event=None) -> QueuedTask:
    request = ProviderRequest(
        client_id="test", work_dir=str(work_dir), timeout_s=-1, quiet=True,
        message="do it", caller="claude",
    )
    return QueuedTask(
        request=request, created_ms=0, req_id=req_id, done_event=threading.Event(),
        has_actionable_waiting=has_actionable_waiting, cancel_event=cancel_event,
    )


def _binding(tmp_path: Path, session_dir: Path, session_id: str) -> dict:
    return {
        "active": True, "work_dir": str(tmp_path), "pi_session_id": session_id,
        "pi_session_dir": str(session_dir), "pane_id": "%12", "pane_title_marker": "Pi-marker",
    }


def test_existing_anchor_refuses_with_zero_sends(monkeypatch, tmp_path: Path):
    header = json.loads((FIXTURES / "s8_pair_b.jsonl").read_text().splitlines()[0])
    session_id = header["id"]
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    target = session_dir / f"2026-11-04T00-00-00.000Z_{session_id}.jsonl"
    target.write_bytes((FIXTURES / "s8_pair_b.jsonl").read_bytes())
    binding = _binding(tmp_path, session_dir, session_id)
    backend = FakeBackend()
    monkeypatch.setattr("askd.adapters.pi._load_destination", lambda _req: (tmp_path / ".pi-session", binding, ""))
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    monkeypatch.setattr("askd.adapters.pi.get_backend_for_session", lambda _data: backend)
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)

    result = adapter.handle_task(_task(tmp_path, "s8-b"))
    assert result.exit_code == 1
    assert "anchor already exists" in result.reply
    assert backend.sent == []


def test_tree_selector_refuses_before_send(monkeypatch, tmp_path: Path):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    binding = _binding(tmp_path, session_dir, session_id)
    backend = FakeBackend()
    backend.get_pane_content = lambda _pane, lines=40: (FIXTURES / "screen-tree-selector.txt").read_text()
    monkeypatch.setattr("askd.adapters.pi._load_destination", lambda _req: (tmp_path / ".pi-session", binding, ""))
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)

    result = adapter.handle_task(_task(tmp_path, "selector-test"))
    assert result.exit_code == 1
    assert "selector decision" in result.reply
    assert backend.sent == []


def test_parked_abort_deadline_expires(monkeypatch, tmp_path: Path):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_path = session_dir / f"2026-11-04T00-00-00.000Z_{session_id}.jsonl"
    binding = _binding(tmp_path, session_dir, session_id)
    backend = FakeBackend()

    def send(pane, text):
        backend.sent.append((pane, text))
        user_text = text
        rows = [
            {"type": "session", "id": session_id, "cwd": str(tmp_path), "version": 3},
            {"type": "message", "id": "user1", "parentId": None, "message": {"role": "user", "content": [{"type": "text", "text": user_text}]}},
            {"type": "message", "id": "abort1", "parentId": "user1", "message": {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "This operation was aborted"}},
        ]
        session_path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    backend.send_text = send
    monkeypatch.setattr("askd.adapters.pi._load_destination", lambda _req: (tmp_path / ".pi-session", binding, ""))
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", iter([0.0, 0.0, 1.0, 62.0]).__next__)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)

    result = adapter.handle_task(_task(tmp_path, "parked-req"))
    assert result.exit_code == 2
    assert "continuation deadline expired" in result.reply
    assert len(backend.sent) == 1


def test_parked_abort_releases_queue_when_actionable_work_waits(monkeypatch, tmp_path: Path):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_path = session_dir / f"2026-11-04T00-00-00.000Z_{session_id}.jsonl"
    binding = _binding(tmp_path, session_dir, session_id)
    backend = FakeBackend()

    def send(pane, text):
        backend.sent.append((pane, text))
        rows = [
            {"type": "session", "id": session_id, "cwd": str(tmp_path), "version": 3},
            {"type": "message", "id": "user1", "parentId": None, "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
            {"type": "message", "id": "abort1", "parentId": "user1", "message": {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "This operation was aborted"}},
        ]
        session_path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    backend.send_text = send
    monkeypatch.setattr("askd.adapters.pi._load_destination", lambda _req: (tmp_path / ".pi-session", binding, ""))
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    ticks = count(0, 1)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", lambda: float(next(ticks)))
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)
    callback_calls = []
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)

    result = adapter.handle_task(_task(tmp_path, "queued-behind-abort", has_actionable_waiting=lambda: callback_calls.append(True) or True))
    assert callback_calls
    assert result.exit_code == 2
    assert "released for queued work" in result.reply
    assert len(backend.sent) == 1


def test_raw_continuation_clears_parked_deadline(monkeypatch, tmp_path: Path):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_path = session_dir / f"2026-11-04T00-00-00.000Z_{session_id}.jsonl"
    binding = _binding(tmp_path, session_dir, session_id)
    backend = FakeBackend()
    alive_calls = 0

    def send(pane, text):
        backend.sent.append((pane, text))
        rows = [
            {"type": "session", "id": session_id, "cwd": str(tmp_path), "version": 3},
            {"type": "message", "id": "user1", "parentId": None, "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
            {"type": "message", "id": "abort1", "parentId": "user1", "message": {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "This operation was aborted"}},
        ]
        session_path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def is_alive(_pane):
        nonlocal alive_calls
        alive_calls += 1
        if alive_calls == 2:
            with session_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "message", "id": "continue1", "parentId": "abort1",
                    "message": {"role": "user", "content": [{"type": "text", "text": "continue"}]},
                }) + "\n")
        return True

    class CancelAfterResume:
        checks = 0

        def is_set(self):
            self.checks += 1
            return self.checks >= 3

    backend.send_text = send
    backend.is_alive = is_alive
    monkeypatch.setattr("askd.adapters.pi._load_destination", lambda _req: (tmp_path / ".pi-session", binding, ""))
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", iter([0.0, 0.0, 1.0, 62.0, 63.0]).__next__)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)

    result = adapter.handle_task(_task(tmp_path, "resumed-request", cancel_event=CancelAfterResume()))
    assert result.status == "cancelled"
    assert "continuation deadline expired" not in result.reply


def test_missing_anchor_fails_fast_and_does_not_resend(monkeypatch, tmp_path: Path):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    binding = _binding(tmp_path, session_dir, session_id)
    backend = FakeBackend()
    monkeypatch.setattr("askd.adapters.pi._load_destination", lambda _req: (tmp_path / ".pi-session", binding, ""))
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    ticks = iter([0.0, 0.0, 0.1, 13.0])
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", ticks.__next__)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)

    result = adapter.handle_task(_task(tmp_path, "never-accepted"))
    assert result.exit_code == 1
    assert result.reply == "Pi did not accept the request (input not received)."
    assert backend.sent and len(backend.sent) == 1


def test_completed_reply_strips_trailing_done_marker(monkeypatch, tmp_path: Path):
    source = FIXTURES / "s8_pair_b.jsonl"
    session_id = json.loads(source.read_text(encoding="utf-8").splitlines()[0])["id"]
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    target = session_dir / f"2026-11-04T00-00-00.000Z_{session_id}.jsonl"
    req_id = "pi-reply-test"
    binding = _binding(tmp_path, session_dir, session_id)
    backend = FakeBackend()

    def accept(pane, text):
        backend.sent.append((pane, text))
        target.write_text(source.read_text(encoding="utf-8").replace("s8-b", req_id), encoding="utf-8")

    backend.send_text = accept
    monkeypatch.setattr("askd.adapters.pi._load_destination", lambda _req: (tmp_path / ".pi-session", binding, ""))
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    ticks = iter([0.0, 0.0, 0.1, 0.2])
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", ticks.__next__)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)

    result = adapter.handle_task(_task(tmp_path, req_id))
    assert result.exit_code == 0
    assert "CCB_DONE:" not in result.reply
    assert result.reply.strip() == "PANEB"
