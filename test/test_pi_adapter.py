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


@pytest.fixture(autouse=True)
def _no_pre_send_retry_wait(monkeypatch):
    """Keep tests fast; retry tests set the wait they exercise explicitly."""
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 0.0)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)


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


def _task(work_dir: Path, req_id: str, *, has_actionable_waiting=None, cancel_event=None, timeout_s: float = -1) -> QueuedTask:
    request = ProviderRequest(
        client_id="test", work_dir=str(work_dir), timeout_s=timeout_s, quiet=True,
        message="do it", caller="claude",
    )
    return QueuedTask(
        request=request, created_ms=0, req_id=req_id, done_event=threading.Event(),
        has_actionable_waiting=has_actionable_waiting, cancel_event=cancel_event,
    )


def _binding(tmp_path: Path, session_dir: Path, session_id: str) -> dict:
    return {
        "active": True, "work_dir": str(tmp_path), "start_dir": str(tmp_path),
        "session_id": "launch-1", "pi_session_id": session_id,
        "pi_session_dir": str(session_dir), "pane_id": "%12", "pane_title_marker": "Pi-marker",
    }


def _write_anchor_session(session_dir: Path, work_dir: Path, session_id: str, req_id: str) -> Path:
    path = session_dir / f"2026-11-05T00-04-35-019Z_{session_id}.jsonl"
    rows = [
        {"type": "session", "id": session_id, "cwd": str(work_dir), "version": 3},
        {"type": "message", "id": "user", "parentId": None, "message": {"role": "user", "content": [{"type": "text", "text": f"CCB_REQ_ID: {req_id}\n\nrequest"}]}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _write_completed_session(session_dir: Path, work_dir: Path, session_id: str, req_id: str) -> Path:
    path = _write_anchor_session(session_dir, work_dir, session_id, req_id)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows.append({
        "type": "message", "id": "assistant", "parentId": "user",
        "message": {"role": "assistant", "content": [{"type": "text", "text": f"Recovered reply\nCCB_DONE: {req_id}"}], "stopReason": "stop"},
    })
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _adapter_with_real_binding(monkeypatch, tmp_path: Path, session_dir: Path, binding: dict, backend: FakeBackend):
    binding_path = tmp_path / ".pi-session"
    binding_path.write_text(json.dumps(binding, indent=2) + "\n", encoding="utf-8")
    binding_path.chmod(0o640)
    monkeypatch.setattr("askd.adapters.pi._load_destination", lambda _req: (binding_path, binding, ""))
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)
    return adapter, binding_path


def test_existing_anchor_refuses_with_zero_sends(monkeypatch, tmp_path: Path):
    header = json.loads((FIXTURES / "s8_pair_b.jsonl").read_text().splitlines()[0])
    session_id = header["id"]
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    target = session_dir / f"2026-11-04T00-00-00.000Z_{session_id}.jsonl"
    content = (FIXTURES / "s8_pair_b.jsonl").read_text(encoding="utf-8").replace("<SCRATCH>/work", str(tmp_path), 1)
    target.write_text(content, encoding="utf-8")
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
        content = source.read_text(encoding="utf-8").replace("<SCRATCH>/work", str(tmp_path), 1).replace("s8-b", req_id)
        target.write_text(content, encoding="utf-8")

    backend.send_text = accept
    capture_calls = count()

    def capture(_pane, lines=40):
        if next(capture_calls) >= 2:
            raise RuntimeError("terminal capture unavailable")
        return (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8")

    backend.get_pane_content = capture
    monkeypatch.setattr("askd.adapters.pi._load_destination", lambda _req: (tmp_path / ".pi-session", binding, ""))
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", count(0.1, 0.01).__next__)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)

    result = adapter.handle_task(_task(tmp_path, req_id))
    assert result.exit_code == 0
    assert "CCB_DONE:" not in result.reply
    assert result.reply.strip() == "PANEB"


def _refuse_with_screen(monkeypatch, tmp_path: Path, screen: str, req_id: str):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    binding = _binding(tmp_path, session_dir, session_id)
    backend = FakeBackend()
    backend.get_pane_content = lambda *_args, **_kwargs: screen
    adapter, _binding_path = _adapter_with_real_binding(monkeypatch, tmp_path, session_dir, binding, backend)
    return adapter.handle_task(_task(tmp_path, req_id)), backend


def test_real_trust_dialog_screen_refuses_with_zero_sends(monkeypatch, tmp_path: Path):
    screen = (FIXTURES / "screen-trust-dialog.txt").read_text(encoding="utf-8")
    result, backend = _refuse_with_screen(monkeypatch, tmp_path, screen, "trust-dialog-screen")
    assert result.exit_code == 1
    assert "project-trust or selector decision" in result.reply
    assert backend.sent == []


def test_editor_draft_with_quoted_marker_refuses_with_zero_sends(monkeypatch, tmp_path: Path):
    lines = (FIXTURES / "screen-idle-quoting-marker.txt").read_text(encoding="utf-8").splitlines()
    rules = [index for index, line in enumerate(lines) if line.startswith("\u2500" * 8)]
    lines[max(rules) - 1] = "draft text"
    result, backend = _refuse_with_screen(monkeypatch, tmp_path, "\n".join(lines), "draft-marker-screen")
    # An unrecognised editor region still falls back to the marker scan (ratified amendment 1),
    # so the draft screen is refused as a dialog; what matters is that nothing is sent.
    assert result.exit_code == 1
    assert "selector decision" in result.reply
    assert backend.sent == []


def test_capture_failure_before_send_refuses_without_sending(monkeypatch, tmp_path: Path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    binding = _binding(tmp_path, session_dir, "8a18f4e0-1b8f-4d3b-8588-c741a703bc75")
    backend = FakeBackend()
    backend.get_pane_content = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("capture failed"))
    adapter, _binding_path = _adapter_with_real_binding(monkeypatch, tmp_path, session_dir, binding, backend)

    result = adapter.handle_task(_task(tmp_path, "capture-failed-before-send"))
    assert result.exit_code == 1 and "readiness could not be verified" in result.reply
    assert backend.sent == []


@pytest.mark.parametrize("second_capture", [
    "busy", "dialog", "draft", "raises", "loader_change", "short_draft", "short_border",
])
def test_confirmed_pre_send_capture_refuses_unstable_or_uncertain_screen(monkeypatch, tmp_path: Path, second_capture: str):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    binding = _binding(tmp_path, session_dir, "8a18f4e0-1b8f-4d3b-8588-c741a703bc75")
    backend = FakeBackend()
    idle_screen = (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8")
    if second_capture == "busy":
        next_screen = (FIXTURES / "screen-busy.txt").read_text(encoding="utf-8")
    elif second_capture == "dialog":
        next_screen = (FIXTURES / "screen-tree-selector.txt").read_text(encoding="utf-8")
    elif second_capture == "draft":
        lines = idle_screen.splitlines()
        lines[2] = "draft text"
        next_screen = "\n".join(lines)
    elif second_capture == "short_draft":
        lines = idle_screen.splitlines()
        lines[2:3] = ["draft text", "──────", ""]
        next_screen = "\n".join(lines)
    elif second_capture == "short_border":
        lines = idle_screen.splitlines()
        lines[1] = "─"
        next_screen = "\n".join(lines)
    elif second_capture == "loader_change":
        lines = idle_screen.splitlines()
        lines[1:1] = ["", "Working"]
        next_screen = "\n".join(lines)
    else:
        next_screen = ""
    capture_calls = count()

    def capture(_pane, lines=40):
        if next(capture_calls) == 0:
            return idle_screen
        if second_capture == "raises":
            raise RuntimeError("second capture failed")
        return next_screen

    backend.get_pane_content = capture
    adapter, _binding_path = _adapter_with_real_binding(monkeypatch, tmp_path, session_dir, binding, backend)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)

    result = adapter.handle_task(_task(tmp_path, f"unstable-{second_capture}"))
    assert result.exit_code == 1
    assert backend.sent == []


def test_resumed_session_anchor_is_tracked_without_rewriting_binding(monkeypatch, tmp_path: Path):
    source = FIXTURES / "s11_resumed_session.jsonl"
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    session_id = rows[0]["id"]
    req_id = "20261105-011341-925-1992986"
    start_dir = tmp_path / "subdir"
    start_dir.mkdir()
    rows[0]["cwd"] = str(start_dir)
    anchor_index = next(
        index for index, row in enumerate(rows)
        if row.get("type") == "message" and req_id in row.get("message", {}).get("content", [{}])[0].get("text", "")
    )
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    target = session_dir / "2026-11-05T00-04-35-019Z_0483f4f5-72d3-476b-9bfa-25df0e3118be.jsonl"
    target.write_text("".join(json.dumps(row) + "\n" for row in rows[:anchor_index]), encoding="utf-8")
    fresh_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    binding = _binding(tmp_path, session_dir, fresh_id)
    binding["start_dir"] = str(start_dir)
    backend = FakeBackend()

    def accept(pane, _text):
        backend.sent.append((pane, _text))
        with target.open("a", encoding="utf-8") as handle:
            handle.write("".join(json.dumps(row) + "\n" for row in rows[anchor_index:]))

    backend.send_text = accept
    adapter, binding_path = _adapter_with_real_binding(monkeypatch, tmp_path, session_dir, binding, backend)
    original_binding = binding_path.read_bytes()
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", iter([0.0, 0.0, 0.1, 0.2]).__next__)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)

    result = adapter.handle_task(_task(tmp_path, req_id))
    assert result.exit_code == 0 and result.status == "completed"
    assert "CCB_DONE:" not in result.reply
    assert binding_path.read_bytes() == original_binding


def test_bound_session_from_subdirectory_launch_is_still_a_candidate(monkeypatch, tmp_path: Path):
    work_dir = tmp_path / "project"
    start_dir = work_dir / "subdir"
    session_dir = tmp_path / "sessions"
    start_dir.mkdir(parents=True)
    session_dir.mkdir()
    session_id = "6d9e8a0e-3653-4f6f-9869-1e143d73134a"
    req_id = "subdir-bound"
    session_path = session_dir / f"2026-11-05T00-04-35-019Z_{session_id}.jsonl"
    session_path.write_text(json.dumps({"type": "session", "id": session_id, "cwd": str(start_dir), "version": 3}) + "\n", encoding="utf-8")
    binding = _binding(work_dir, session_dir, session_id)
    binding["start_dir"] = str(start_dir)
    backend = FakeBackend()

    def accept(pane, text):
        backend.sent.append((pane, text))
        rows = [
            {"type": "message", "id": "user", "parentId": None, "message": {"role": "user", "content": [{"type": "text", "text": f"CCB_REQ_ID: {req_id}\nrequest"}]}},
            {"type": "message", "id": "assistant", "parentId": "user", "message": {"role": "assistant", "content": [{"type": "text", "text": f"Subdirectory reply\nCCB_DONE: {req_id}"}], "stopReason": "stop"}},
        ]
        with session_path.open("a", encoding="utf-8") as handle:
            handle.write("".join(json.dumps(row) + "\n" for row in rows))

    backend.send_text = accept
    adapter, _binding_path = _adapter_with_real_binding(monkeypatch, work_dir, session_dir, binding, backend)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", count(0.1, 0.01).__next__)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)

    result = adapter.handle_task(_task(work_dir, req_id))
    assert result.exit_code == 0 and result.reply.strip() == "Subdirectory reply"


def test_unchanged_candidate_session_is_parsed_once_across_polls(monkeypatch, tmp_path: Path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    candidate_path = _write_anchor_session(session_dir, tmp_path, "6d9e8a0e-3653-4f6f-9869-1e143d73134a", "other-request")
    candidate_path.write_text(json.dumps({"type": "session", "id": "6d9e8a0e-3653-4f6f-9869-1e143d73134a", "cwd": str(tmp_path), "version": 3}) + "\n", encoding="utf-8")
    binding = _binding(tmp_path, session_dir, "8a18f4e0-1b8f-4d3b-8588-c741a703bc75")
    backend = FakeBackend()
    adapter, _binding_path = _adapter_with_real_binding(monkeypatch, tmp_path, session_dir, binding, backend)
    import pi_comm

    original_load = pi_comm._load_session_path
    parses = []

    def count_parse(path, session_id):
        if path == candidate_path:
            parses.append(path)
        return original_load(path, session_id)

    monkeypatch.setattr(pi_comm, "_load_session_path", count_parse)
    screens = iter([
        (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8"),
        (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8"),
        "blank terminal", "blank terminal",
        (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8"),
        (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8"),
        (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8"),
    ])
    backend.get_pane_content = lambda _pane, lines=40: next(screens)
    monkeypatch.setattr("askd.adapters.pi.ANCHOR_TIMEOUT_S", 0.01)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", iter([0.0, 0.0, 0.02, 0.03, 0.04]).__next__)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)

    result = adapter.handle_task(_task(tmp_path, "never-found"))
    assert result.reply == "Pi did not accept the request (input not received)."
    assert len(parses) == 1


def test_dialog_after_anchor_deadline_fails_with_dialog_message(monkeypatch, tmp_path: Path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    binding = _binding(tmp_path, session_dir, "8a18f4e0-1b8f-4d3b-8588-c741a703bc75")
    backend = FakeBackend()
    screens = iter([
        (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8"),
        (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8"),
        (FIXTURES / "screen-tree-selector.txt").read_text(encoding="utf-8"),
    ])
    backend.get_pane_content = lambda _pane, lines=40: next(screens)
    adapter, _binding_path = _adapter_with_real_binding(monkeypatch, tmp_path, session_dir, binding, backend)
    monkeypatch.setattr("askd.adapters.pi.ANCHOR_TIMEOUT_S", 0.01)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", iter([0.0, 0.0, 0.02]).__next__)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)

    result = adapter.handle_task(_task(tmp_path, "dialog-no-anchor"))
    assert result.exit_code == 1 and not result.done_seen
    assert result.reply == "Pi did not accept the request (Pi is showing a dialog)."


def test_nonbound_existing_anchor_refuses_without_sending(monkeypatch, tmp_path: Path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    bound_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    _write_anchor_session(session_dir, tmp_path, "6d9e8a0e-3653-4f6f-9869-1e143d73134a", "duplicate")
    binding = _binding(tmp_path, session_dir, bound_id)
    backend = FakeBackend()
    adapter, _ = _adapter_with_real_binding(monkeypatch, tmp_path, session_dir, binding, backend)

    result = adapter.handle_task(_task(tmp_path, "duplicate"))
    assert result.exit_code == 1 and "anchor already exists" in result.reply
    assert backend.sent == []


def test_multiple_nonbound_anchors_are_ambiguous_without_rebinding(monkeypatch, tmp_path: Path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    bound_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    _write_anchor_session(session_dir, tmp_path, "6d9e8a0e-3653-4f6f-9869-1e143d73134a", "duplicate")
    _write_anchor_session(session_dir, tmp_path, "63f65251-7c83-4301-a171-c0c797927f83", "duplicate")
    binding = _binding(tmp_path, session_dir, bound_id)
    backend = FakeBackend()
    adapter, binding_path = _adapter_with_real_binding(monkeypatch, tmp_path, session_dir, binding, backend)

    result = adapter.handle_task(_task(tmp_path, "duplicate"))
    assert result.exit_code == 2 and result.status == "incomplete" and "ambiguous" in result.reply
    assert backend.sent == []
    assert json.loads(binding_path.read_text(encoding="utf-8"))["pi_session_id"] == bound_id


@pytest.mark.parametrize("invalid", ["foreign_cwd", "header_id"])
def test_invalid_or_foreign_candidate_is_ignored(monkeypatch, tmp_path: Path, invalid: str):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    bound_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    candidate_id = "6d9e8a0e-3653-4f6f-9869-1e143d73134a"
    candidate = _write_anchor_session(session_dir, tmp_path, candidate_id, "ignored")
    if invalid == "foreign_cwd":
        content = candidate.read_text(encoding="utf-8").replace(str(tmp_path), str(tmp_path / "elsewhere"), 1)
        candidate.write_text(content, encoding="utf-8")
    else:
        content = candidate.read_text(encoding="utf-8").replace(candidate_id, bound_id, 1)
        candidate.write_text(content, encoding="utf-8")
    binding = _binding(tmp_path, session_dir, bound_id)
    backend = FakeBackend()
    adapter, binding_path = _adapter_with_real_binding(monkeypatch, tmp_path, session_dir, binding, backend)
    monkeypatch.setattr("askd.adapters.pi.ANCHOR_TIMEOUT_S", 0.01)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", iter([0.0, 0.0, 0.1]).__next__)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)

    result = adapter.handle_task(_task(tmp_path, "ignored"))
    assert result.reply == "Pi did not accept the request (input not received)."
    assert json.loads(binding_path.read_text(encoding="utf-8"))["pi_session_id"] == bound_id


def test_busy_then_idle_without_anchor_reports_unlocated_activity(monkeypatch, tmp_path: Path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    binding = _binding(tmp_path, session_dir, "8a18f4e0-1b8f-4d3b-8588-c741a703bc75")
    backend = FakeBackend()
    screens = iter([
        (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8"),
        (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8"),
        (FIXTURES / "screen-busy.txt").read_text(encoding="utf-8"),
        (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8"),
        (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8"),
        (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8"),
    ])
    backend.get_pane_content = lambda _pane, lines=40: next(screens)
    adapter, _ = _adapter_with_real_binding(monkeypatch, tmp_path, session_dir, binding, backend)
    monkeypatch.setattr("askd.adapters.pi.ANCHOR_TIMEOUT_S", 0.01)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", iter([0.0, 0.0, 0.02, 0.03]).__next__)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)

    result = adapter.handle_task(_task(tmp_path, "activity-without-anchor"))
    assert result.exit_code == 1 and not result.done_seen
    assert result.reply == "Pi showed activity, but CCB could not locate this request in its session folder."


def test_unknown_screen_after_anchor_deadline_allows_late_completion(monkeypatch, tmp_path: Path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    binding = _binding(tmp_path, session_dir, "8a18f4e0-1b8f-4d3b-8588-c741a703bc75")
    backend = FakeBackend()
    screens = iter([
        (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8"),
        (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8"),
        "blank terminal", "blank terminal",
    ])
    original_get = backend.get_pane_content

    def get_screen(_pane, lines=40):
        screen = next(screens)
        if screen == "blank terminal":
            return screen
        return original_get(_pane, lines)

    backend.get_pane_content = get_screen
    adapter, _ = _adapter_with_real_binding(monkeypatch, tmp_path, session_dir, binding, backend)
    original_find = __import__("pi_comm").load_candidate_sessions
    calls = count()

    def late_candidate(directory, work_dir, **kwargs):
        if next(calls) == 2:
            _write_completed_session(directory, work_dir, "6d9e8a0e-3653-4f6f-9869-1e143d73134a", "late-anchor")
        return original_find(directory, work_dir, **kwargs)

    monkeypatch.setattr("askd.adapters.pi.load_candidate_sessions", late_candidate)
    monkeypatch.setattr("askd.adapters.pi.ANCHOR_TIMEOUT_S", 0.01)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", iter([0.0, 0.0, 0.02, 0.03, 0.04]).__next__)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)

    result = adapter.handle_task(_task(tmp_path, "late-anchor"))
    assert result.exit_code == 0 and result.reply.strip() == "Recovered reply"


def test_completion_landing_during_deadline_confirmation_is_delivered(monkeypatch, tmp_path: Path):
    source = FIXTURES / "s8_pair_b.jsonl"
    session_id = json.loads(source.read_text(encoding="utf-8").splitlines()[0])["id"]
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    target = session_dir / f"2026-11-04T00-00-00.000Z_{session_id}.jsonl"
    req_id = "pi-late-confirm"
    binding = _binding(tmp_path, session_dir, session_id)
    backend = FakeBackend()

    def sleep(seconds):
        # The anchored completion is persisted while the deadline idle check confirms.
        if backend.sent and seconds == pytest.approx(0.2) and not target.exists():
            content = source.read_text(encoding="utf-8").replace("<SCRATCH>/work", str(tmp_path), 1).replace("s8-b", req_id)
            target.write_text(content, encoding="utf-8")

    monkeypatch.setattr("askd.adapters.pi._load_destination", lambda _req: (tmp_path / ".pi-session", binding, ""))
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", count(0.1, 1.0).__next__)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", sleep)
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)

    result = adapter.handle_task(_task(tmp_path, req_id, timeout_s=60))
    assert result.exit_code == 0
    assert result.status == "completed"
    assert len(backend.sent) == 1


def test_spaced_custom_loader_frame_refuses_with_zero_sends(monkeypatch, tmp_path: Path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    binding = _binding(tmp_path, session_dir, "8a18f4e0-1b8f-4d3b-8588-c741a703bc75")
    backend = FakeBackend()
    idle = (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8").splitlines()
    top = len(idle) - 5
    screen = "\n".join(idle[:top - 1] + ["", " >> >> Working"] + idle[top:])
    backend.get_pane_content = lambda _pane, lines=40: screen
    monkeypatch.setattr("askd.adapters.pi._load_destination", lambda _req: (tmp_path / ".pi-session", binding, ""))
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)

    result = adapter.handle_task(_task(tmp_path, "spaced-frame"))
    assert result.exit_code == 1
    assert "busy" in result.reply
    assert backend.sent == []


class _Clock:
    """Monotonic clock driven by a tick list; the current tick is visible to screens."""

    def __init__(self, monkeypatch, ticks: list[float]):
        self.now = 0.0
        self._ticks = iter(ticks)
        monkeypatch.setattr("askd.adapters.pi.time.monotonic", self)
        monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)

    def __call__(self) -> float:
        self.now = next(self._ticks)
        return self.now


def _session_header(session_id: str, work_dir: Path) -> dict:
    return {"type": "session", "id": session_id, "cwd": str(work_dir), "version": 3}


def _anchor_row(req_id: str) -> dict:
    return {"type": "message", "id": "user1", "parentId": None, "message": {"role": "user", "content": [{"type": "text", "text": f"CCB_REQ_ID: {req_id}\nrequest"}]}}


def _error_session_row(row_id: str, parent: str | None) -> dict:
    return {"type": "message", "id": row_id, "parentId": parent, "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "t"}], "stopReason": "error", "errorMessage": "terminated"}}


def _retry_edit_row(row_id: str, parent: str, target: str) -> dict:
    return {"type": "context_edit", "id": row_id, "parentId": parent, "targetId": target, "replacement": None}


def _done_session_row(row_id: str, parent: str, req_id: str) -> dict:
    return {"type": "message", "id": row_id, "parentId": parent, "message": {"role": "assistant", "content": [{"type": "text", "text": f"answer\nCCB_DONE: {req_id}"}], "stopReason": "stop"}}


def _write_session(path: Path, work_dir: Path, session_id: str, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in [_session_header(session_id, work_dir), *rows]))


def _adapt_with_session(monkeypatch, tmp_path: Path, session_id: str, session_path: Path, backend: FakeBackend, first_rows: list[dict]):
    binding = _binding(tmp_path, session_path.parent, session_id)

    def send(pane, text):
        backend.sent.append((pane, text))
        _write_session(session_path, tmp_path, session_id, first_rows)

    backend.send_text = send
    monkeypatch.setattr("askd.adapters.pi._load_destination", lambda _req: (tmp_path / ".pi-session", binding, ""))
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)
    return adapter


def test_retrying_turn_delivers_the_later_reply(monkeypatch, tmp_path: Path):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_path = session_dir / f"2026-11-06T18-00-00.000Z_{session_id}.jsonl"
    req_id = "retry-then-done"
    backend = FakeBackend()
    adapter = _adapt_with_session(
        monkeypatch, tmp_path, session_id, session_path, backend,
        [_anchor_row(req_id), _error_session_row("err1", "user1"), _retry_edit_row("edit1", "err1", "err1")],
    )
    alive = {"calls": 0}

    def is_alive(_pane):
        alive["calls"] += 1
        if alive["calls"] == 2:
            _write_session(session_path, tmp_path, session_id, [
                _anchor_row(req_id), _error_session_row("err1", "user1"), _retry_edit_row("edit1", "err1", "err1"),
                _done_session_row("final1", "edit1", req_id),
            ])
        return True

    backend.is_alive = is_alive
    _Clock(monkeypatch, [0.0, 0.0, 0.0, 0.5])

    result = adapter.handle_task(_task(tmp_path, req_id))
    assert result.exit_code == 0
    assert result.reply.strip() == "answer"
    assert len(backend.sent) == 1


def test_retrying_turn_stops_after_confirmed_idle_retry_window(monkeypatch, tmp_path: Path):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_path = session_dir / f"2026-11-06T18-00-00.000Z_{session_id}.jsonl"
    req_id = "retry-cancelled"
    backend = FakeBackend()
    adapter = _adapt_with_session(
        monkeypatch, tmp_path, session_id, session_path, backend,
        [_anchor_row(req_id), _error_session_row("err1", "user1"), _retry_edit_row("edit1", "err1", "err1")],
    )
    clock = _Clock(monkeypatch, [0.0, 0.0, 0.0, 29.9, 30.1])

    result = adapter.handle_task(_task(tmp_path, req_id))
    assert result.exit_code == 2
    assert result.reply == "Pi retry stopped before a reply"
    assert result.status == "incomplete"
    # Survived 29.9 s of idle and ended at 30.1 s, well before the parked 60 s deadline.
    assert clock.now == 30.1
    assert len(backend.sent) == 1


def test_retrying_idle_window_resets_on_activity(monkeypatch, tmp_path: Path):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_path = session_dir / f"2026-11-06T18-00-00.000Z_{session_id}.jsonl"
    req_id = "retry-restarted-clock"
    backend = FakeBackend()
    adapter = _adapt_with_session(
        monkeypatch, tmp_path, session_id, session_path, backend,
        [_anchor_row(req_id), _error_session_row("err1", "user1"), _retry_edit_row("edit1", "err1", "err1")],
    )
    idle_screen = (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8")
    busy_screen = (FIXTURES / "screen-busy.txt").read_text(encoding="utf-8")
    clock = _Clock(monkeypatch, [0.0, 0.0, 0.0, 29.9, 30.0, 59.9, 90.1, 120.2])
    backend.get_pane_content = lambda _pane, lines=40: busy_screen if 30.0 <= clock.now < 60.0 else idle_screen

    result = adapter.handle_task(_task(tmp_path, req_id))
    assert result.exit_code == 2
    assert result.reply == "Pi retry stopped before a reply"
    # 30.1 s of idle only counted after the last non-idle read at 59.9, not from t=0.
    assert clock.now >= 90.1


def test_errored_turn_becomes_incomplete_after_grace(monkeypatch, tmp_path: Path):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_path = session_dir / f"2026-11-06T18-00-00.000Z_{session_id}.jsonl"
    req_id = "error-no-edit"
    backend = FakeBackend()
    adapter = _adapt_with_session(
        monkeypatch, tmp_path, session_id, session_path, backend,
        [_anchor_row(req_id), _error_session_row("err1", "user1")],
    )
    clock = _Clock(monkeypatch, [0.0, 0.0, 0.0, 9.9, 10.1])

    result = adapter.handle_task(_task(tmp_path, req_id))
    assert result.exit_code == 2
    assert result.reply == "Pi turn failed: terminated"
    assert result.status == "incomplete"
    # Ended only after the 10 s grace (still running at 9.9 s).
    assert clock.now == 10.1


def test_retry_edit_within_grace_keeps_waiting_and_completes(monkeypatch, tmp_path: Path):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_path = session_dir / f"2026-11-06T18-00-00.000Z_{session_id}.jsonl"
    req_id = "late-edit"
    backend = FakeBackend()
    adapter = _adapt_with_session(
        monkeypatch, tmp_path, session_id, session_path, backend,
        [_anchor_row(req_id), _error_session_row("err1", "user1")],
    )
    alive = {"calls": 0}

    def is_alive(_pane):
        alive["calls"] += 1
        if alive["calls"] == 2:
            _write_session(session_path, tmp_path, session_id, [
                _anchor_row(req_id), _error_session_row("err1", "user1"), _retry_edit_row("edit1", "err1", "err1"),
                _done_session_row("final1", "edit1", req_id),
            ])
        return True

    backend.is_alive = is_alive
    _Clock(monkeypatch, [0.0, 0.0, 0.0, 9.0])

    result = adapter.handle_task(_task(tmp_path, req_id))
    assert result.exit_code == 0
    assert result.reply.strip() == "answer"


def test_errored_grace_restarts_for_a_later_failed_retry(monkeypatch, tmp_path: Path):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_path = session_dir / f"2026-11-06T18-00-00.000Z_{session_id}.jsonl"
    req_id = "two-failures"
    backend = FakeBackend()
    adapter = _adapt_with_session(
        monkeypatch, tmp_path, session_id, session_path, backend,
        [_anchor_row(req_id), _error_session_row("err1", "user1")],
    )
    alive = {"calls": 0}

    def is_alive(_pane):
        alive["calls"] += 1
        if alive["calls"] == 2:
            _write_session(session_path, tmp_path, session_id, [
                _anchor_row(req_id), _error_session_row("err1", "user1"), _retry_edit_row("edit1", "err1", "err1"),
                _error_session_row("err2", "edit1"),
            ])
        return True

    backend.is_alive = is_alive
    clock = _Clock(monkeypatch, [0.0, 0.0, 0.0, 5.0, 12.0, 15.1])

    result = adapter.handle_task(_task(tmp_path, req_id))
    assert result.exit_code == 2
    assert result.reply == "Pi turn failed: terminated"
    assert result.status == "incomplete"
    # 12.0 - 0.0 is past the first grace, so the window restarted at the second failure (5.0)
    # and only expired at 15.1.
    assert clock.now == 15.1


def _custom_session_row(row_id: str, parent: str | None) -> dict:
    return {
        "type": "custom_message", "id": row_id, "parentId": parent, "timestamp": "2026-11-06T20:32:48.946Z",
        "customType": "placeholder", "content": [{"type": "text", "text": "placeholder"}], "display": True,
    }


def test_custom_message_flush_before_retry_edit_still_delivers_the_reply(monkeypatch, tmp_path: Path):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_path = session_dir / f"2026-11-06T18-00-00.000Z_{session_id}.jsonl"
    req_id = "flush-then-retry"
    backend = FakeBackend()
    adapter = _adapt_with_session(
        monkeypatch, tmp_path, session_id, session_path, backend,
        [_anchor_row(req_id), _error_session_row("err1", "user1")],
    )
    alive = {"calls": 0}

    def is_alive(_pane):
        alive["calls"] += 1
        if alive["calls"] == 2:
            _write_session(session_path, tmp_path, session_id, [
                _anchor_row(req_id), _error_session_row("err1", "user1"),
                _custom_session_row("custom1", "err1"),
            ])
        if alive["calls"] == 3:
            _write_session(session_path, tmp_path, session_id, [
                _anchor_row(req_id), _error_session_row("err1", "user1"),
                _custom_session_row("custom1", "err1"),
                _retry_edit_row("edit1", "custom1", "err1"),
                _done_session_row("final1", "edit1", req_id),
            ])
        return True

    backend.is_alive = is_alive
    _Clock(monkeypatch, [0.0, 0.0, 0.0, 1.0, 2.0])

    result = adapter.handle_task(_task(tmp_path, req_id))
    assert result.exit_code == 0
    assert result.reply.strip() == "answer"
    assert len(backend.sent) == 1


def test_errored_state_without_an_entry_id_still_expires_after_grace(monkeypatch, tmp_path: Path):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_path = session_dir / f"2026-11-06T18-00-00.000Z_{session_id}.jsonl"
    req_id = "error-without-id"
    backend = FakeBackend()
    identless_error = _error_session_row("ignored", "user1")
    identless_error.pop("id")
    adapter = _adapt_with_session(
        monkeypatch, tmp_path, session_id, session_path, backend,
        [_anchor_row(req_id), identless_error],
    )
    clock = _Clock(monkeypatch, [0.0, 0.0, 0.0, 10.1])

    result = adapter.handle_task(_task(tmp_path, req_id))
    assert result.exit_code == 2
    assert result.reply == "Pi turn failed: terminated"
    assert result.status == "incomplete"
    assert clock.now == 10.1


def _retrying_adapter(monkeypatch, tmp_path: Path, req_id: str, screen):
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    binding = _binding(tmp_path, session_dir, session_id)
    backend = FakeBackend()
    backend.get_pane_content = lambda _pane, lines=40: screen()
    loads = []

    def load(req):
        loads.append(req)
        return tmp_path / ".pi-session", binding, ""

    monkeypatch.setattr("askd.adapters.pi._load_destination", load)
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)
    return adapter, backend, loads


def test_pre_send_retry_becomes_idle_and_sends_exactly_once(monkeypatch, tmp_path: Path):
    req_id = "narrow-retry"
    idle = (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8")
    busy = (FIXTURES / "screen-busy.txt").read_text(encoding="utf-8")
    screens = iter([busy, idle, idle, idle])
    adapter, backend, _loads = _retrying_adapter(
        monkeypatch, tmp_path, req_id, lambda: next(screens, idle),
    )
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"

    def send(pane, text):
        backend.sent.append((pane, text))
        _write_completed_session(session_dir, tmp_path, session_id, req_id)

    backend.send_text = send
    logs: list[str] = []
    monkeypatch.setattr("askd.adapters.pi._write_log", logs.append)
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 1.0)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", count(0.1, 0.01).__next__)

    result = adapter.handle_task(_task(tmp_path, req_id))
    assert result.exit_code == 0 and result.status == "completed"
    assert result.reply.strip() == "Recovered reply"
    assert len(backend.sent) == 1
    assert any("pre-send retry" in line and "state=busy" in line for line in logs)


def test_pre_send_retry_budget_exhaustion_names_state_and_reason(monkeypatch, tmp_path: Path):
    busy = (FIXTURES / "screen-busy.txt").read_text(encoding="utf-8")
    adapter, backend, loads = _retrying_adapter(monkeypatch, tmp_path, "still-busy", lambda: busy)
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 0.0)

    result = adapter.handle_task(_task(tmp_path, "still-busy"))
    assert result.exit_code == 1 and result.status == "failed"
    assert "busy" in result.reply
    assert "state: busy" in result.reply
    assert "activity" in result.reply
    # Initial attempt plus PRE_SEND_RETRY_LIMIT retries.
    assert len(loads) == 1 + 3
    assert backend.sent == []


def test_pre_send_retry_never_widens_the_request_timeout(monkeypatch, tmp_path: Path):
    busy = (FIXTURES / "screen-busy.txt").read_text(encoding="utf-8")
    adapter, backend, loads = _retrying_adapter(monkeypatch, tmp_path, "short-timeout", lambda: busy)
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 20.0)

    result = adapter.handle_task(_task(tmp_path, "short-timeout", timeout_s=5))
    assert result.exit_code == 1 and "state: busy" in result.reply
    assert len(loads) == 1
    assert backend.sent == []


def test_pre_send_retry_cancelled_during_the_wait_sends_nothing(monkeypatch, tmp_path: Path):
    class CancelOnSecondCheck:
        checks = 0

        def is_set(self):
            self.checks += 1
            return self.checks >= 2

    busy = (FIXTURES / "screen-busy.txt").read_text(encoding="utf-8")
    adapter, backend, _loads = _retrying_adapter(monkeypatch, tmp_path, "cancel-in-wait", lambda: busy)
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 1.0)

    result = adapter.handle_task(_task(tmp_path, "cancel-in-wait", cancel_event=CancelOnSecondCheck()))
    assert result.status == "cancelled"
    assert "cancelled before a terminal reply" in result.reply
    assert backend.sent == []


def test_pre_send_retry_rereads_a_binding_that_changed_during_the_wait(monkeypatch, tmp_path: Path):
    req_id = "rebind-after-wait"
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    binding = _binding(tmp_path, session_dir, session_id)
    rebound = dict(binding, pane_id="%99")
    idle = (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8")
    busy = (FIXTURES / "screen-busy.txt").read_text(encoding="utf-8")
    screens = iter([busy, idle, idle, idle])
    backend = FakeBackend()
    backend.get_pane_content = lambda _pane, lines=40: next(screens, idle)
    loads: list[int] = []
    bindings = [binding, rebound]

    def load(_req):
        loads.append(len(loads))
        return tmp_path / ".pi-session", bindings[min(len(loads) - 1, 1)], ""

    def send(pane, text):
        backend.sent.append((pane, text))
        _write_completed_session(session_dir, tmp_path, session_id, req_id)

    backend.send_text = send
    monkeypatch.setattr("askd.adapters.pi._load_destination", load)
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 1.0)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", count(0.1, 0.01).__next__)
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)

    result = adapter.handle_task(_task(tmp_path, req_id))
    assert result.exit_code == 0
    assert len(loads) == 2
    assert [pane for pane, _text in backend.sent] == ["%99"]


def test_policy_refusal_after_a_wait_sends_nothing(monkeypatch, tmp_path: Path):
    idle = (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8")
    busy = (FIXTURES / "screen-busy.txt").read_text(encoding="utf-8")
    screens = iter([busy, idle, idle])
    adapter, backend, _loads = _retrying_adapter(monkeypatch, tmp_path, "policy-after-wait", lambda: next(screens, idle))
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 1.0)
    monkeypatch.setattr(
        "delivery_policy.refusal_for_request", lambda *_args, **_kwargs: "Role policy refuses this send.",
    )

    result = adapter.handle_task(_task(tmp_path, "policy-after-wait"))
    assert result.exit_code == 1 and result.reply == "Role policy refuses this send."
    assert backend.sent == []


def test_one_row_overlay_over_the_status_line_refuses_with_zero_sends(monkeypatch, tmp_path: Path):
    lines = (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8").splitlines()
    lines[-1] = " Select a session"
    result, backend = _refuse_with_screen(monkeypatch, tmp_path, "\n".join(lines), "overlay-status-screen")
    assert result.exit_code == 1
    assert "state: dialog" in result.reply
    assert backend.sent == []


def test_real_narrow_idle_capture_sends_once(monkeypatch, tmp_path: Path):
    # The reported bug: a 28-col idle pane read "unknown" and was refused before any send.
    req_id = "narrow-28cols"
    session_id = "8a18f4e0-1b8f-4d3b-8588-c741a703bc75"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    binding = _binding(tmp_path, session_dir, session_id)
    screen = (FIXTURES / "real-idle-28cols-tail.txt").read_text(encoding="utf-8")
    backend = FakeBackend()
    backend.get_pane_content = lambda _pane, lines=40: screen

    def send(pane, text):
        backend.sent.append((pane, text))
        _write_completed_session(session_dir, tmp_path, session_id, req_id)

    backend.send_text = send
    adapter, _binding_path = _adapter_with_real_binding(monkeypatch, tmp_path, session_dir, binding, backend)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", count(0.1, 0.01).__next__)

    result = adapter.handle_task(_task(tmp_path, req_id))
    assert result.exit_code == 0 and result.status == "completed"
    assert len(backend.sent) == 1


def test_pre_send_retry_binding_that_disappears_during_the_wait_sends_nothing(monkeypatch, tmp_path: Path):
    busy = (FIXTURES / "screen-busy.txt").read_text(encoding="utf-8")
    adapter, backend, loads = _retrying_adapter(monkeypatch, tmp_path, "binding-gone", lambda: busy)
    binding = _binding(tmp_path, tmp_path / "sessions", "8a18f4e0-1b8f-4d3b-8588-c741a703bc75")

    def load(_req):
        loads.append(1)
        if len(loads) == 1:
            return tmp_path / ".pi-session", binding, ""
        return tmp_path / ".pi-session", None, "Pi session is inactive."

    monkeypatch.setattr("askd.adapters.pi._load_destination", load)
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 1.0)

    result = adapter.handle_task(_task(tmp_path, "binding-gone"))
    assert result.exit_code == 1 and result.reply == "Pi session is inactive."
    assert len(loads) == 2
    assert backend.sent == []


def test_pre_send_wait_that_crosses_the_deadline_is_never_injected(monkeypatch, tmp_path: Path):
    # The wait fits inside the budget when it starts, but the deadline has passed when it
    # returns; a timed-out request must still never be injected.
    busy = (FIXTURES / "screen-busy.txt").read_text(encoding="utf-8")
    adapter, backend, _loads = _retrying_adapter(monkeypatch, tmp_path, "wait-crosses-deadline", lambda: busy)
    now = {"t": 0.0}
    waits = []

    def wait(_task, seconds):
        waits.append(seconds)
        now["t"] = 31.0
        return True

    monkeypatch.setattr("askd.adapters.pi.time.monotonic", lambda: now["t"])
    monkeypatch.setattr("askd.adapters.pi._wait_before_retry", wait)
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 20.0)

    result = adapter.handle_task(_task(tmp_path, "wait-crosses-deadline", timeout_s=30))
    assert waits == [20.0]
    assert result.exit_code == 2
    assert result.status == "incomplete"
    assert result.reply == "Pi request timed out before a terminal CCB_DONE was observed."
    assert backend.sent == []


class _CancelFlag:
    def __init__(self):
        self.flag = False

    def is_set(self):
        return self.flag


def test_pre_send_cancel_during_the_idle_recheck_sends_nothing(monkeypatch, tmp_path: Path):
    # The cancellation lands during the readiness re-check that follows a wait, so the
    # post-wait guard passes but the post-re-check guard must still refuse.
    idle = (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8")
    busy = (FIXTURES / "screen-busy.txt").read_text(encoding="utf-8")
    cancel = _CancelFlag()
    captures = {"count": 0}

    def screen():
        captures["count"] += 1
        if captures["count"] == 1:
            return busy
        cancel.flag = True
        return idle

    adapter, backend, _loads = _retrying_adapter(monkeypatch, tmp_path, "cancel-in-recheck", screen)
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 1.0)

    result = adapter.handle_task(_task(tmp_path, "cancel-in-recheck", cancel_event=cancel))
    assert result.status == "cancelled"
    assert "cancelled before a terminal reply" in result.reply
    assert backend.sent == []


def test_pre_send_deadline_passing_during_the_idle_recheck_sends_nothing(monkeypatch, tmp_path: Path):
    idle = (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8")
    busy = (FIXTURES / "screen-busy.txt").read_text(encoding="utf-8")
    now = {"t": 0.0}
    captures = {"count": 0}

    def screen():
        captures["count"] += 1
        if captures["count"] == 1:
            return busy
        now["t"] = 31.0
        return idle

    adapter, backend, _loads = _retrying_adapter(monkeypatch, tmp_path, "deadline-in-recheck", screen)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", lambda: now["t"])
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 20.0)

    result = adapter.handle_task(_task(tmp_path, "deadline-in-recheck", timeout_s=30))
    assert result.exit_code == 2
    assert result.status == "incomplete"
    assert result.reply == "Pi request timed out before a terminal CCB_DONE was observed."
    assert backend.sent == []


def test_first_attempt_idle_is_not_blocked_by_the_post_wait_guard(monkeypatch, tmp_path: Path):
    # No wait happened, so the post-wait guard must not run: the send still happens once and
    # only the existing post-send poll reacts to the pending cancellation.
    class AlwaysCancelled:
        def is_set(self):
            return True

    idle = (FIXTURES / "screen-idle.txt").read_text(encoding="utf-8")
    adapter, backend, _loads = _retrying_adapter(monkeypatch, tmp_path, "first-attempt-idle", lambda: idle)

    result = adapter.handle_task(_task(tmp_path, "first-attempt-idle", cancel_event=AlwaysCancelled()))
    assert len(backend.sent) == 1
    assert result.status == "cancelled"
