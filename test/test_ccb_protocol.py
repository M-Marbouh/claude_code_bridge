from __future__ import annotations

import re

from ccb_protocol import (
    DONE_PREFIX,
    REQ_ID_PREFIX,
    is_done_text,
    make_req_id,
    select_codex_reply,
    strip_done_text,
    strip_trailing_markers,
    wrap_codex_delivery_prompt,
    wrap_codex_prompt,
)
from laskd_protocol import wrap_claude_delivery_prompt


def test_make_req_id_format_and_uniqueness() -> None:
    ids = [make_req_id() for _ in range(2000)]
    assert len(set(ids)) == len(ids)
    for rid in ids:
        assert isinstance(rid, str)
        # Format: YYYYMMDD-HHMMSS-mmm-PID-counter
        assert re.fullmatch(r"\d{8}-\d{6}-\d{3}-\d+-\d+", rid) is not None


def test_wrap_codex_prompt_structure() -> None:
    req_id = make_req_id()
    message = "hello\nworld"
    prompt = wrap_codex_prompt(message, req_id)

    assert f"{REQ_ID_PREFIX} {req_id}" in prompt
    assert "IMPORTANT:" in prompt
    assert "- Reply normally." in prompt
    assert "sole delivered result" in prompt
    assert f"{DONE_PREFIX} {req_id}" in prompt
    assert prompt.endswith(f"{DONE_PREFIX} {req_id}\n")


def test_codex_delivery_prompt_requires_explicit_reverse_reply() -> None:
    prompt = wrap_codex_delivery_prompt("hello", "req-1")

    assert f"{REQ_ID_PREFIX} req-1" in prompt
    assert "send it explicitly with ask --peer" in prompt
    assert "Preserve CCB_PEER_TASK_ID as --reply-to" in prompt
    assert "CCB is not capturing this turn automatically" in prompt
    assert DONE_PREFIX not in prompt


def test_is_done_text_recognizes_last_nonempty_line() -> None:
    req_id = make_req_id()
    ok = f"hi\n{DONE_PREFIX} {req_id}\n"
    assert is_done_text(ok, req_id) is True

    ok_with_trailing_blanks = f"hi\n{DONE_PREFIX} {req_id}\n\n\n"
    assert is_done_text(ok_with_trailing_blanks, req_id) is True

    ok_with_trailing_harness_done = f"hi\n{DONE_PREFIX} {req_id}\nHARNESS_DONE\n"
    assert is_done_text(ok_with_trailing_harness_done, req_id) is True

    ok_with_trailing_harness_done_and_blanks = f"hi\n{DONE_PREFIX} {req_id}\n\nHARNESS_DONE\n\n"
    assert is_done_text(ok_with_trailing_harness_done_and_blanks, req_id) is True

    not_last = f"{DONE_PREFIX} {req_id}\nhi\n"
    assert is_done_text(not_last, req_id) is False

    other_id = make_req_id()
    wrong_id = f"hi\n{DONE_PREFIX} {other_id}\n"
    assert is_done_text(wrong_id, req_id) is False

    only_harness_done = "hi\nHARNESS_DONE\n"
    assert is_done_text(only_harness_done, req_id) is False


def test_strip_done_text_removes_done_line() -> None:
    req_id = make_req_id()
    text = f"line1\nline2\n{DONE_PREFIX} {req_id}\n\n"
    assert strip_done_text(text, req_id) == "line1\nline2"

    text_with_harness_done = f"line1\nline2\n{DONE_PREFIX} {req_id}\nHARNESS_DONE\n"
    assert strip_done_text(text_with_harness_done, req_id) == "line1\nline2"


def test_strip_trailing_markers_removes_done_and_harness_trailers() -> None:
    req_id = make_req_id()
    text = f"line1\nline2\n{DONE_PREFIX} {req_id}\nHARNESS_DONE\n\n"
    assert strip_trailing_markers(text) == "line1\nline2"


def test_claude_delivery_prompt_respects_peer_reply_intent() -> None:
    prompt = wrap_claude_delivery_prompt("hello", "req-1")

    assert "If CCB_REPLY_EXPECTED is no, do not send a reverse peer message" in prompt
    assert "preserve CCB_PEER_TASK_ID as --reply-to" in prompt
    assert "otherwise use --background" in prompt


def test_select_codex_reply_handles_marker_only_terminal_event() -> None:
    req_id = "20260729-120000-000-1"

    assert (
        select_codex_reply(
            f"CCB_DONE: {req_id}",
            "Actual final",
            f"Actual final\nCCB_DONE: {req_id}",
            req_id,
        )
        == "Actual final"
    )
