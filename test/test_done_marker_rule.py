"""A reply is finished by a whole-line CCB_DONE once the provider's turn ends.

Regression coverage for the stalled-queue incident: Claude wrote a correct
CCB_BEGIN..CCB_DONE reply, added a prose note after the marker, and the task
stayed pending forever because the marker had to be the last line.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from askd.adapters.base import ProviderRequest, QueuedTask
from askd.adapters.claude import ClaudeAdapter
from ccb_protocol import (
    BEGIN_PREFIX,
    DONE_PREFIX,
    REQ_ID_PREFIX,
    is_done_text,
    select_codex_reply,
    split_done_text,
)
from ccb_runtime_status import describe_queue_status
from claude_comm import TURN_END_EVENT, ClaudeLogReader
from laskd_protocol import extract_reply_for_req, extract_trailing_for_req, wrap_claude_prompt
from worker_pool import BaseSessionWorker

REQ = "20260923-195638-295-2386635-3"
NEXT = "20260923-200123-202-2386635-4"
BEGIN = f"{BEGIN_PREFIX} {REQ}"
DONE = f"{DONE_PREFIX} {REQ}"
ANCHOR = ("user", f"{REQ_ID_PREFIX} {REQ}\n\nreview this\n\nReply using exactly this format:\n{BEGIN}\n<reply>\n{DONE}\n")
TURN_END = (TURN_END_EVENT, "")


# --- the shared marker rule -------------------------------------------------


def test_marker_quoted_inside_a_line_never_completes() -> None:
    for text in (
        f"End with `{DONE}` next time.",
        f"I will write {DONE} at the end",
        f"> quoting: {DONE} was sent",
    ):
        assert is_done_text(text, REQ) is False
        assert is_done_text(text, REQ, turn_ended=True) is False


def test_marker_followed_by_prose_completes_only_after_turn_end() -> None:
    text = f"{BEGIN}\nanswer\n{DONE}\n\nA note for the operator."
    assert is_done_text(text, REQ) is False
    assert is_done_text(text, REQ, turn_ended=True) is True


def test_marker_for_another_request_never_completes() -> None:
    text = f"answer\n{DONE_PREFIX} {NEXT}\nmore"
    assert is_done_text(text, REQ, turn_ended=True) is False


def test_split_done_text_separates_trailing_prose() -> None:
    body, trailing = split_done_text(f"answer\n{DONE}\n\nnote\nHARNESS_DONE\n", REQ)
    assert body == "answer"
    assert trailing == "note"


def test_codex_final_answer_with_trailing_prose_keeps_reply_and_reports_note() -> None:
    final = f"answer line\n{DONE}\nA late note."
    reply = select_codex_reply(final, final, final, REQ)
    assert reply.startswith("answer line")
    assert DONE not in reply
    assert "kept writing after its CCB_DONE" in reply
    assert reply.rstrip().endswith("A late note.")


def test_claude_prompt_says_nothing_may_follow_the_marker() -> None:
    assert f"Nothing may follow the {DONE_PREFIX} line" in wrap_claude_prompt("hi", REQ)


# --- Claude reply extraction --------------------------------------------------


def test_stall_released_turns_later_extracts_only_the_original_reply() -> None:
    combined = "\n".join(
        [
            BEGIN,
            "the real answer",
            DONE,
            "",
            "prose after the marker",
            "an answer to the operator's unrelated question",
            DONE,
        ]
    )
    assert extract_reply_for_req(combined, REQ) == "the real answer"


def test_resent_reply_wins_over_the_first_attempt() -> None:
    combined = "\n".join([BEGIN, "first", DONE, "note", BEGIN, "second", DONE])
    assert extract_reply_for_req(combined, REQ) == "second"


def test_two_replies_in_one_message_each_get_their_own_segment() -> None:
    combined = "\n".join(
        [BEGIN, "one", DONE, f"{BEGIN_PREFIX} {NEXT}", "two", f"{DONE_PREFIX} {NEXT}"]
    )
    assert extract_reply_for_req(combined, REQ) == "one"
    assert extract_reply_for_req(combined, NEXT) == "two"


def test_trailing_text_stops_at_the_next_reply() -> None:
    combined = "\n".join([BEGIN, "one", DONE, "note", f"{BEGIN_PREFIX} {NEXT}", "two"])
    assert extract_trailing_for_req(combined, REQ) == "note"


def test_begin_without_done_returns_the_reply_body() -> None:
    assert extract_reply_for_req(f"preamble\n{BEGIN}\nbody only", REQ) == "body only"


# --- the Claude transcript reader ---------------------------------------------


def _jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def test_reader_emits_turn_end_for_turn_duration_only(tmp_path: Path) -> None:
    session = tmp_path / "s.jsonl"
    _jsonl(
        session,
        [
            {"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn",
                                              "content": [{"type": "text", "text": "hi"}]}},
            {"type": "system", "subtype": "stop_hook_summary"},
            {"type": "system", "subtype": "turn_duration", "isSidechain": True},
            {"type": "system", "subtype": "turn_duration"},
        ],
    )
    reader = ClaudeLogReader(work_dir=tmp_path)
    events, _ = reader._read_new_events(session, {"session_path": session, "offset": 0})
    assert events == [("assistant", "hi"), TURN_END]


# --- the Claude adapter wait loop ---------------------------------------------


class _Reader:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self._events = list(events)

    def wait_for_events(self, state: dict, timeout: float):
        if self._events:
            return [self._events.pop(0)], state
        time.sleep(min(timeout, 0.01))
        return [], state


class _Backend:
    def is_alive(self, pane_id: str) -> bool:
        return True


def _run(tmp_path: Path, events: list[tuple[str, str]], *, wait_s: float = 0.3):
    req = ProviderRequest(
        client_id="c", work_dir=str(tmp_path), timeout_s=-1, quiet=True,
        message="review this", caller="codex", req_id=REQ,
    )
    task = QueuedTask(request=req, created_ms=0, req_id=REQ, done_event=threading.Event())
    result = ClaudeAdapter()._wait_for_response(
        task, None, "claude:test", 0, _Reader(events), {}, _Backend(), "pane-1",
        deadline=time.time() + wait_s,
    )
    return task, result


def test_incident_reply_with_prose_after_marker_completes_at_turn_end(tmp_path: Path) -> None:
    reply = f"{BEGIN}\nNo changes needed.\n{DONE}\n\nCodex had already asked about this query once."
    _task, result = _run(tmp_path, [ANCHOR, ("assistant", reply), TURN_END])
    assert result.done_seen is True
    assert result.status == "completed"
    assert result.reply.startswith("No changes needed.")
    assert DONE not in result.reply
    assert "Codex had already asked" in result.reply  # reported, not lost


def test_marker_mid_stream_waits_for_the_turn_to_end(tmp_path: Path) -> None:
    reply = f"{BEGIN}\nanswer\n{DONE}\nstill writing"
    _task, result = _run(tmp_path, [ANCHOR, ("assistant", reply)], wait_s=0.2)
    assert result.done_seen is False


def test_marker_only_in_the_request_never_completes(tmp_path: Path) -> None:
    _task, result = _run(tmp_path, [ANCHOR, TURN_END], wait_s=0.2)
    assert result.done_seen is False


def test_turn_end_after_begin_without_marker_releases_as_incomplete(tmp_path: Path) -> None:
    started = time.time()
    _task, result = _run(tmp_path, [ANCHOR, ("assistant", f"{BEGIN}\nhalf an answer"), TURN_END], wait_s=5.0)
    assert time.time() - started < 2.0, "must release at turn end, not at the deadline"
    assert result.done_seen is False
    assert result.status == "incomplete"
    assert result.reply.startswith("half an answer")
    assert "without the CCB_DONE line" in result.reply


def test_turn_end_while_waiting_on_delegated_work_keeps_waiting(tmp_path: Path) -> None:
    events = [
        ANCHOR,
        ("assistant", "Codex processing..."),
        TURN_END,
        ("user", "[CCB completion] codex result: looks fine"),
        ("assistant", f"{BEGIN}\nfinal answer\n{DONE}"),
        TURN_END,
    ]
    task, result = _run(tmp_path, events, wait_s=2.0)
    assert result.done_seen is True
    assert result.reply == "final answer"


def test_unmarked_turn_end_is_visible_in_task_progress(tmp_path: Path) -> None:
    task, result = _run(tmp_path, [ANCHOR, ("assistant", "Codex processing..."), TURN_END], wait_s=0.2)
    assert result.done_seen is False
    assert task.progress["phase"] == "turn_ended_without_marker"


# --- queue status -------------------------------------------------------------


def test_queue_status_reports_a_blocked_queue_as_stuck() -> None:
    worker = BaseSessionWorker("claude:test")
    task = QueuedTask(
        request=None, created_ms=0, req_id=REQ, done_event=threading.Event(),
        progress={"phase": "turn_ended_without_marker", "stalled": True},
    )
    worker._current = task
    worker._current_started_at = time.time() - 700
    worker._q.put(object())
    snapshot = worker.queue_snapshot()
    snapshot["provider"] = "claude"

    stuck, line = describe_queue_status([snapshot])
    assert stuck is True
    assert REQ in line and "1 waiting" in line


def test_queue_status_idle_and_busy_are_not_stuck() -> None:
    assert describe_queue_status([]) == (False, "queue: idle")
    busy = {"waiting": 0, "in_flight": {"req_id": REQ, "running_s": 30, "progress": {"phase": "replying"}}}
    stuck, line = describe_queue_status([busy])
    assert stuck is False and line.startswith("queue: busy")


def test_extraction_handles_request_ids_outside_the_standard_format() -> None:
    short = "20260923-195638-295-3"
    combined = f"{BEGIN_PREFIX} {short}\nanswer\n{DONE_PREFIX} {short}\nnote"
    assert extract_reply_for_req(combined, short) == "answer"
    assert extract_trailing_for_req(combined, short) == "note"


def test_daemon_queue_status_lists_in_flight_task(tmp_path: Path) -> None:
    import askd.daemon as askd_daemon

    daemon = askd_daemon.UnifiedAskDaemon(state_file=tmp_path / "askd.json")
    worker = BaseSessionWorker("claude:test")
    worker._current = QueuedTask(request=None, created_ms=0, req_id=REQ, done_event=threading.Event())
    worker._current_started_at = time.time()
    pool = daemon.pool._get_pool("claude")
    pool._workers["claude:test"] = worker

    response = daemon._handle_request({"operation": "queue_status", "provider": "claude", "id": "x"})

    assert response["exit_code"] == 0
    [entry] = response["queues"]
    assert entry["provider"] == "claude"
    assert entry["in_flight"]["req_id"] == REQ
    assert daemon._handle_request({"operation": "queue_status", "provider": "codex"})["queues"] == []
