# This repo puts lib/ on sys.path at runtime (test/conftest.py) and ships no pyright
# configuration, so the workspace-rooted analyzer cannot see these flat imports.
# pyright: reportMissingImports=false
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pi_comm import (
    PiSessionError,
    confirm_pi_ready,
    load_session,
    read_pi_state,
)


FIXTURES = Path(__file__).parent / "fixtures" / "pi"


def _copy_session(tmp_path: Path, fixture: str) -> tuple[Path, str]:
    source = FIXTURES / fixture
    header = json.loads(source.read_text(encoding="utf-8").splitlines()[0])
    session_id = header["id"]
    target = tmp_path / f"2026-11-04T00-00-00.000Z_{session_id}.jsonl"
    shutil.copyfile(source, target)
    return target, session_id


def test_real_abort_continue_fixture_completes_original_request(tmp_path: Path) -> None:
    _path, session_id = _copy_session(tmp_path, "s4_abort_continue.jsonl")
    exchange = load_session(tmp_path, session_id).exchange("s4-orig")
    assert exchange.state == "completed"
    assert "CCB_DONE: s4-orig" in exchange.reply


def test_unwrapped_user_row_requires_a_prior_abort_to_continue(tmp_path: Path) -> None:
    session_id = "6d9e8a0e-3653-4f6f-9869-1e143d73134a"
    rows = [
        {"type": "session", "id": session_id, "cwd": "<SCRATCH>/work", "version": 3},
        {"type": "message", "id": "u1", "parentId": None, "message": {"role": "user", "content": [{"type": "text", "text": "CCB_REQ_ID: raw-steering\nrequest"}]}},
        {"type": "message", "id": "u2", "parentId": "u1", "message": {"role": "user", "content": [{"type": "text", "text": "manual unwrapped message"}]}},
        {"type": "message", "id": "a2", "parentId": "u2", "message": {"role": "assistant", "content": [{"type": "text", "text": "CCB_DONE: raw-steering"}], "stopReason": "stop"}},
    ]
    path = tmp_path / f"test_{session_id}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert load_session(tmp_path, session_id).exchange("raw-steering").state == "incomplete"


def test_real_abort_then_raw_continue_is_running_not_parked(tmp_path: Path) -> None:
    source = FIXTURES / "s4_abort_continue.jsonl"
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    continuation_index = next(
        index for index, row in enumerate(rows)
        if row.get("type") == "message"
        and row.get("message", {}).get("role") == "user"
        and row["message"].get("content", [{}])[0].get("text") == "continue"
    )
    session_id = rows[0]["id"]
    path = tmp_path / f"truncated_{session_id}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows[:continuation_index + 1]))
    assert load_session(tmp_path, session_id).exchange("s4-orig").state == "pending"


def test_real_steering_fixture_supersedes_original_request(tmp_path: Path) -> None:
    _path, session_id = _copy_session(tmp_path, "s10_steering.jsonl")
    exchange = load_session(tmp_path, session_id).exchange("s10-a")
    assert exchange.state == "superseded"


def test_real_tree_branch_fixture_does_not_reuse_old_branch_completion(tmp_path: Path) -> None:
    _path, session_id = _copy_session(tmp_path, "s5b_tree_branch.jsonl")
    exchange = load_session(tmp_path, session_id).exchange("s5b-1")
    assert exchange.state == "branch_switched"


def test_completed_exchange_ignores_later_quote_of_original_marker(tmp_path: Path) -> None:
    path, session_id = _copy_session(tmp_path, "s8_pair_b.jsonl")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    completion = next(
        row for row in rows
        if row.get("type") == "message"
        and row.get("message", {}).get("role") == "assistant"
        and "CCB_DONE: s8-b" in " ".join(
            part.get("text", "") for part in row["message"].get("content", []) if isinstance(part, dict)
        )
    )
    rows.extend([
        {"type": "message", "id": "ordinary-user", "parentId": completion["id"], "message": {"role": "user", "content": [{"type": "text", "text": "Quote the earlier completion marker: CCB_DONE: s8-b"}]}},
        {"type": "message", "id": "ordinary-assistant", "parentId": "ordinary-user", "message": {"role": "assistant", "content": [{"type": "text", "text": "The earlier response ended with:\nCCB_DONE: s8-b"}], "stopReason": "stop"}},
    ])
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    recovered = load_session(tmp_path, session_id).completed_exchange("s8-b")
    assert recovered.state == "completed"
    assert recovered.completion_id == completion["id"]
    assert "PANEB" in recovered.reply


def test_completed_exchange_refuses_two_distinct_completed_branches(tmp_path: Path) -> None:
    session_id = "63f65251-7c83-4301-a171-c0c797927f83"
    rows = [
        {"type": "session", "id": session_id, "cwd": "<SCRATCH>/work", "version": 3},
        {"type": "message", "id": "anchor", "parentId": None, "message": {"role": "user", "content": [{"type": "text", "text": "CCB_REQ_ID: forked\nDo task"}]}},
        {"type": "message", "id": "done-a", "parentId": "anchor", "message": {"role": "assistant", "content": [{"type": "text", "text": "Answer A\nCCB_DONE: forked"}], "stopReason": "stop"}},
        {"type": "message", "id": "done-b", "parentId": "anchor", "message": {"role": "assistant", "content": [{"type": "text", "text": "Answer B\nCCB_DONE: forked"}], "stopReason": "stop"}},
    ]
    (tmp_path / f"fork_{session_id}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    recovered = load_session(tmp_path, session_id).completed_exchange("forked")
    assert recovered.state == "ambiguous"


def _naive_last_assistant_done(session, req_id: str) -> bool:
    """Negative control for readers that ignore abort, branch, and ancestry."""
    assistants = [
        row["message"] for row in session.entries
        if row.get("type") == "message" and row.get("message", {}).get("role") == "assistant"
    ]
    if not assistants:
        return False
    content = assistants[-1].get("content") or []
    text = "\n".join(part.get("text", "") for part in content if part.get("type") == "text")
    return any(line.startswith("CCB_DONE: ") for line in text.splitlines())


def test_naive_last_done_reader_fails_abort_steering_and_branch_controls(tmp_path: Path) -> None:
    _path, abort_id = _copy_session(tmp_path, "s4_abort_continue.jsonl")
    abort = load_session(tmp_path, abort_id)
    assert abort.exchange("s4-b").state == "superseded"
    assert _naive_last_assistant_done(abort, "s4-b")  # wrongly accepts original DONE after the abort

    tmp_path2 = tmp_path / "steering"
    tmp_path2.mkdir()
    _path, steering_id = _copy_session(tmp_path2, "s10_steering.jsonl")
    steering = load_session(tmp_path2, steering_id)
    assert steering.exchange("s10-a").state == "superseded"
    assert _naive_last_assistant_done(steering, "s10-a")  # B's completion is not A's

    tmp_path3 = tmp_path / "branch"
    tmp_path3.mkdir()
    _path, branch_id = _copy_session(tmp_path3, "s5b_tree_branch.jsonl")
    branch = load_session(tmp_path3, branch_id)
    assert branch.exchange("s5b-1").state == "branch_switched"
    assert _naive_last_assistant_done(branch, "s5b-1")  # old branch completion is not on active ancestry


def test_real_sequential_relaunch_and_same_cwd_pair_fixtures_are_distinct(tmp_path: Path) -> None:
    a = FIXTURES / "s8_pair_a.jsonl"
    b = FIXTURES / "s8_pair_b.jsonl"
    a_header, b_header = (json.loads(p.read_text(encoding="utf-8").splitlines()[0]) for p in (a, b))
    assert a_header["cwd"] == b_header["cwd"]
    assert a_header["id"] != b_header["id"]
    pair_dir = tmp_path / "pair"
    pair_dir.mkdir()
    shutil.copyfile(a, pair_dir / f"a_{a_header['id']}.jsonl")
    shutil.copyfile(b, pair_dir / f"b_{b_header['id']}.jsonl")
    assert load_session(pair_dir, a_header["id"]).exchange("s8-long").state == "completed"
    assert load_session(pair_dir, b_header["id"]).exchange("s8-b").state == "completed"
    assert len(json.loads((FIXTURES / "s2_relaunch_append.jsonl").read_text().splitlines()[0])["id"]) == 36


def test_duplicate_matching_session_files_are_ambiguous(tmp_path: Path) -> None:
    path, session_id = _copy_session(tmp_path, "s8_pair_a.jsonl")
    shutil.copyfile(path, tmp_path / f"second_{session_id}.jsonl")
    with pytest.raises(PiSessionError, match="ambiguous"):
        load_session(tmp_path, session_id)


def test_unknown_header_version_and_torn_last_line(tmp_path: Path) -> None:
    path, session_id = _copy_session(tmp_path, "s8_pair_a.jsonl")
    rows = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(rows[0])
    header["version"] = 999
    rows[0] = json.dumps(header)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(PiSessionError, match="unsupported"):
        load_session(tmp_path, session_id)

    header["version"] = 3
    rows[0] = json.dumps(header)
    path.write_bytes(("\n".join(rows) + "\n{" ).encode())
    session = load_session(tmp_path, session_id)
    assert session.exchange("s8-long").state == "completed"
    path.write_bytes(("\n".join(rows) + "\nnot-json\n").encode())
    with pytest.raises(PiSessionError, match="Invalid Pi session row"):
        load_session(tmp_path, session_id)


def test_completion_controls_reject_wrong_id_user_toolcall_and_nonabort_error(tmp_path: Path) -> None:
    _path, session_id = _copy_session(tmp_path, "s8_pair_b.jsonl")
    session_path = next(tmp_path.glob(f"*_{session_id}.jsonl"))
    rows = [json.loads(line) for line in session_path.read_text().splitlines()]
    user = next(row for row in rows if row.get("type") == "message" and row["message"].get("role") == "user")
    assistant = next(row for row in rows if row.get("type") == "message" and row["message"].get("role") == "assistant")
    user["message"]["content"][0]["text"] = "CCB_REQ_ID: synthetic\n\nwrite CCB_DONE: synthetic"
    assistant["message"]["content"] = [{"type": "text", "text": "CCB_DONE: wrong"}]
    session_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    session = load_session(tmp_path, session_id)
    assert session.exchange("synthetic").state == "incomplete"

    assistant["message"]["content"] = [{"type": "text", "text": "CCB_DONE: synthetic"}, {"type": "toolCall", "name": "bash"}]
    session_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert load_session(tmp_path, session_id).exchange("synthetic").state == "incomplete"

    assistant["message"]["stopReason"] = "error"
    assistant["message"]["errorMessage"] = "provider failed"
    session_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    # An error as the last row has no retry edit yet: non-final "errored" (R4 plan); the adapter
    # still ends the task incomplete with this reason after its grace window.
    assert load_session(tmp_path, session_id).exchange("synthetic").state == "errored"

    # Neither a user-row marker nor a thinking part is a terminal assistant text row.
    assistant["message"]["stopReason"] = "stop"
    assistant["message"].pop("errorMessage", None)
    assistant["message"]["content"] = [{"type": "thinking", "thinking": "CCB_DONE: synthetic"}]
    session_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert load_session(tmp_path, session_id).exchange("synthetic").state == "incomplete"


REQ = "r4-retry"


def _write_rows(tmp_path: Path, session_id: str, rows: list[dict]) -> Path:
    path = tmp_path / f"synthetic_{session_id}.jsonl"
    header = {"type": "session", "id": session_id, "cwd": "<SCRATCH>/work", "version": 3}
    path.write_text("".join(json.dumps(row) + "\n" for row in [header, *rows]))
    return path


def _user_row(row_id: str, parent: str | None, req_id: str = REQ) -> dict:
    return {"type": "message", "id": row_id, "parentId": parent, "message": {"role": "user", "content": [{"type": "text", "text": f"CCB_REQ_ID: {req_id}\nrequest"}]}}


def _error_row(row_id: str, parent: str | None, message: str = "terminated", tool_call: str = "") -> dict:
    content: list[dict] = [{"type": "thinking", "thinking": "thinking"}]
    if tool_call:
        content.append({"type": "toolCall", "id": tool_call, "name": "bash", "arguments": {"command": "true"}})
    return {"type": "message", "id": row_id, "parentId": parent, "message": {"role": "assistant", "content": content, "stopReason": "error", "errorMessage": message}}


def _edit_row(row_id: str, parent: str | None, target: str, replacement=None) -> dict:
    return {"type": "context_edit", "id": row_id, "parentId": parent, "targetId": target, "replacement": replacement}


def _tool_result_row(row_id: str, parent: str | None, tool_call: str) -> dict:
    return {"type": "message", "id": row_id, "parentId": parent, "message": {"role": "toolResult", "toolCallId": tool_call, "toolName": "bash", "content": [{"type": "text", "text": "ok"}], "isError": False}}


def _done_row(row_id: str, parent: str | None, req_id: str = REQ) -> dict:
    return {"type": "message", "id": row_id, "parentId": parent, "message": {"role": "assistant", "content": [{"type": "text", "text": f"answer\nCCB_DONE: {req_id}"}], "stopReason": "stop"}}


def test_real_retry_record_completes_and_recovers_completion(tmp_path: Path) -> None:
    _path, session_id = _copy_session(tmp_path, "s11_retry_recovery.jsonl")
    session = load_session(tmp_path, session_id)
    exchange = session.exchange("20261106-203214-836-218252")
    assert exchange.state == "completed"
    assert exchange.completion_id == "0fe4f717"
    assert exchange.reply.rstrip().endswith("CCB_DONE: 20261106-203214-836-218252")
    recovered = session.completed_exchange("20261106-203214-836-218252")
    assert recovered.state == "completed"
    assert recovered.completion_id == "0fe4f717"


def test_error_with_retry_edit_and_no_later_row_is_retrying(tmp_path: Path) -> None:
    session_id = "11111111-1111-4111-8111-111111111111"
    _write_rows(tmp_path, session_id, [_user_row("u1", None), _error_row("e1", "u1"), _edit_row("x1", "e1", "e1")])
    exchange = load_session(tmp_path, session_id).exchange(REQ)
    assert exchange.state == "retrying"
    assert exchange.error_id == "e1"
    assert "terminated" in exchange.reason


def test_error_as_last_row_is_errored_with_its_entry_id(tmp_path: Path) -> None:
    session_id = "22222222-2222-4222-8222-222222222222"
    _write_rows(tmp_path, session_id, [_user_row("u1", None), _error_row("e1", "u1")])
    exchange = load_session(tmp_path, session_id).exchange(REQ)
    assert exchange.state == "errored"
    assert exchange.error_id == "e1"
    assert exchange.reason == "Pi turn failed: terminated"


def test_error_with_later_messages_and_no_retry_edit_is_incomplete(tmp_path: Path) -> None:
    session_id = "33333333-3333-4333-8333-333333333333"
    _write_rows(tmp_path, session_id, [_user_row("u1", None), _error_row("e1", "u1"), _done_row("a1", "e1")])
    exchange = load_session(tmp_path, session_id).exchange(REQ)
    assert exchange.state == "incomplete"
    assert exchange.reason == "Pi turn failed: terminated"


def test_two_retries_then_done_completes(tmp_path: Path) -> None:
    session_id = "44444444-4444-4444-8444-444444444444"
    _write_rows(tmp_path, session_id, [
        _user_row("u1", None),
        _error_row("e1", "u1"),
        _edit_row("x1", "e1", "e1"),
        _error_row("e2", "x1"),
        _edit_row("x2", "e2", "e2"),
        _done_row("a1", "x2"),
    ])
    assert load_session(tmp_path, session_id).exchange(REQ).state == "completed"


def test_exhausted_retry_ending_in_error_is_errored_then_final(tmp_path: Path) -> None:
    session_id = "55555555-5555-4555-8555-555555555555"
    _write_rows(tmp_path, session_id, [
        _user_row("u1", None),
        _error_row("e1", "u1"),
        _edit_row("x1", "e1", "e1"),
        _error_row("e2", "x1"),
    ])
    exchange = load_session(tmp_path, session_id).exchange(REQ)
    assert exchange.state == "errored" and exchange.error_id == "e2"

    # A trailing non-message row is a permitted intervening row, not a final outcome.
    _write_rows(tmp_path, session_id, [
        _user_row("u1", None),
        _error_row("e1", "u1"),
        _edit_row("x1", "e1", "e1"),
        _error_row("e2", "x1"),
        {"type": "model_change", "id": "m1", "parentId": "e2", "provider": "p", "modelId": "m"},
    ])
    trailing = load_session(tmp_path, session_id).exchange(REQ)
    assert trailing.state == "errored" and trailing.error_id == "e2"

    _write_rows(tmp_path, session_id, [
        _user_row("u1", None),
        _error_row("e1", "u1"),
        _edit_row("x1", "e1", "e1"),
        _error_row("e2", "x1"),
        _done_row("a1", "e2"),
    ])
    assert load_session(tmp_path, session_id).exchange(REQ).state == "incomplete"


def test_retry_edit_behind_own_tool_results_is_accepted(tmp_path: Path) -> None:
    session_id = "66666666-6666-4666-8666-666666666666"
    _write_rows(tmp_path, session_id, [
        _user_row("u1", None),
        _error_row("e1", "u1", tool_call="call-1"),
        _tool_result_row("t1", "e1", "call-1"),
        _edit_row("x1", "t1", "e1"),
    ])
    assert load_session(tmp_path, session_id).exchange(REQ).state == "retrying"


def test_user_message_before_retry_edit_is_not_a_retry(tmp_path: Path) -> None:
    session_id = "77777777-7777-4777-8777-777777777777"
    _write_rows(tmp_path, session_id, [
        _user_row("u1", None),
        _error_row("e1", "u1"),
        {"type": "message", "id": "manual", "parentId": "e1", "message": {"role": "user", "content": [{"type": "text", "text": "continue"}]}},
        _edit_row("x1", "manual", "e1"),
    ])
    exchange = load_session(tmp_path, session_id).exchange(REQ)
    assert exchange.state == "incomplete"
    assert exchange.reason == "Pi turn failed: terminated"


def test_unrelated_edit_does_not_suppress_the_error(tmp_path: Path) -> None:
    session_id = "88888888-8888-4888-8888-888888888888"
    # An edit for another entry is a permitted intervening row: the error is still not retried,
    # so the failure waits for its retry edit (non-final "errored") instead of being final.
    _write_rows(tmp_path, session_id, [_user_row("u1", None), _error_row("e1", "u1"), _edit_row("x1", "e1", "other")])
    unrelated = load_session(tmp_path, session_id).exchange(REQ)
    assert unrelated.state == "errored" and unrelated.error_id == "e1"

    _write_rows(tmp_path, session_id, [
        _user_row("u1", None),
        _error_row("e1", "u1"),
        _edit_row("x1", "e1", "other"),
        _edit_row("x2", "x1", "e1"),
    ])
    assert load_session(tmp_path, session_id).exchange(REQ).state == "retrying"

    _write_rows(tmp_path, session_id, [
        _user_row("u1", None),
        _error_row("e1", "u1"),
        _edit_row("x1", "e1", "other"),
        _done_row("a1", "x1"),
    ])
    assert load_session(tmp_path, session_id).exchange(REQ).state == "incomplete"


def test_non_null_replacement_edit_does_not_suppress_the_error(tmp_path: Path) -> None:
    session_id = "99999999-9999-4999-8999-999999999999"
    _write_rows(tmp_path, session_id, [
        _user_row("u1", None),
        _error_row("e1", "u1"),
        _edit_row("x1", "e1", "e1", replacement=[{"type": "text", "text": "edited"}]),
    ])
    replaced = load_session(tmp_path, session_id).exchange(REQ)
    assert replaced.state == "errored" and replaced.error_id == "e1"

    _write_rows(tmp_path, session_id, [
        _user_row("u1", None),
        _error_row("e1", "u1"),
        _edit_row("x1", "e1", "e1", replacement=[{"type": "text", "text": "edited"}]),
        _done_row("a1", "x1"),
    ])
    assert load_session(tmp_path, session_id).exchange(REQ).state == "incomplete"


def _custom_message_row(row_id: str, parent: str | None) -> dict:
    return {
        "type": "custom_message", "id": row_id, "parentId": parent, "timestamp": "2026-11-06T20:32:48.946Z",
        "customType": "placeholder", "content": [{"type": "text", "text": "placeholder"}], "display": True,
    }


def test_failed_turn_with_custom_message_flush_stays_errored_then_retries(tmp_path: Path) -> None:
    session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    # Pi flushes pending custom messages at turn end, before it writes the recovery edit.
    _write_rows(tmp_path, session_id, [_user_row("u1", None), _error_row("e1", "u1"), _custom_message_row("c1", "e1")])
    flushed = load_session(tmp_path, session_id).exchange(REQ)
    assert flushed.state == "errored"
    assert flushed.error_id == "e1"

    _write_rows(tmp_path, session_id, [
        _user_row("u1", None),
        _error_row("e1", "u1"),
        _custom_message_row("c1", "e1"),
        _edit_row("x1", "c1", "e1"),
    ])
    assert load_session(tmp_path, session_id).exchange(REQ).state == "retrying"

    _write_rows(tmp_path, session_id, [
        _user_row("u1", None),
        _error_row("e1", "u1"),
        _custom_message_row("c1", "e1"),
        _edit_row("x1", "c1", "e1"),
        _done_row("a1", "x1"),
    ])
    completed = load_session(tmp_path, session_id).exchange(REQ)
    assert completed.state == "completed"
    assert "CCB_DONE: " + REQ in completed.reply


# --------------------------------------------------------------------------- #
# Pi readiness comes from Pi's own state file (lib/pi_extension/ccb-state.ts)
# --------------------------------------------------------------------------- #

BINDING_FIXTURE = "binding-real.json"
SNAPSHOT_FIXTURE = "pi-state-real.json"


def _binding(tmp_path: Path, **overrides) -> dict:
    """The real binding's key set, pointed at this test's own state file and session dir."""
    data = json.loads((FIXTURES / BINDING_FIXTURE).read_text(encoding="utf-8"))
    runtime = tmp_path / "rt"
    runtime.mkdir(mode=0o700, exist_ok=True)
    data["pi_state_file"] = str(runtime / "pi-state.json")
    data["runtime_dir"] = str(runtime)
    data["pi_session_dir"] = str(tmp_path / "sessions")
    data["work_dir"] = str(tmp_path)
    data.update(overrides)
    return data


def _snapshot(**overrides) -> dict:
    """The captured real snapshot, re-stamped so it describes this test's moment and pid."""
    data = json.loads((FIXTURES / SNAPSHOT_FIXTURE).read_text(encoding="utf-8"))
    data["pid"] = os.getpid()
    data["updated_at"] = int(time.time() * 1000)
    data["seq"] = 1
    data.update(overrides)
    return data


def _write_state(binding: dict, snapshot: dict | None = None, *, raw: str | None = None) -> Path:
    path = Path(binding["pi_state_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text(json.dumps(snapshot if snapshot is not None else _snapshot()), encoding="utf-8")
    return path


def _read(binding: dict, **kwargs):
    """Readiness for a binding as the production call sites ask for it."""
    return read_pi_state(binding, **kwargs)


def _dead_pid() -> int:
    """A pid that has lived and exited, so liveness says no (not a reused live pid)."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    return child.pid


def test_fixtures_are_a_real_binding_and_a_real_snapshot() -> None:
    binding = json.loads((FIXTURES / BINDING_FIXTURE).read_text(encoding="utf-8"))
    snapshot = json.loads((FIXTURES / SNAPSHOT_FIXTURE).read_text(encoding="utf-8"))
    assert set(binding) == {
        "active", "ccb_launch_caller", "ccb_launch_provider", "ccb_managed_launch", "ccb_project_id",
        "pane_id", "pane_title_marker", "pi_launch_token", "pi_session_dir", "pi_session_id",
        "pi_state_file", "runtime_dir", "session_id", "start_cmd", "start_dir", "started_at",
        "terminal", "tmux_session", "work_dir",
    }
    # Captured from a throwaway Pi 1.5.2 launched with CCB's extension: a real TUI startup.
    assert snapshot["v"] == 1 and snapshot["mode"] == "tui" and snapshot["has_ui"] is True
    assert set(snapshot) == {
        "v", "launch_token", "runtime_instance_id", "pid", "mode", "has_ui", "idle",
        "editor_empty", "prompt_open", "seq", "updated_at",
    }


def test_a_real_snapshot_for_its_binding_is_ready(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    _write_state(binding, _snapshot(launch_token=binding["pi_launch_token"]))
    state = _read(binding)
    assert (state.status, state.reason) == ("ready", "")


def test_a_missing_state_file_is_absent_with_the_restart_hint(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    state = _read(binding)
    assert state.status == "absent"
    assert state.reason == "CCB's Pi extension is not reporting; restart CCB"


@pytest.mark.parametrize("raw", [
    "",
    "not json",
    json.dumps({"v": 2}),
    json.dumps([1, 2, 3]),
    json.dumps({"v": 1}),
])
def test_unreadable_or_unversioned_state_is_absent(tmp_path: Path, raw: str) -> None:
    binding = _binding(tmp_path)
    _write_state(binding, raw=raw or " ")
    assert _read(binding).status == "absent"


def test_a_newer_snapshot_version_is_not_read_as_version_one(tmp_path: Path) -> None:
    # A complete, well-typed snapshot from a future extension version is not this contract.
    binding = _binding(tmp_path)
    _write_state(binding, {**_snapshot(launch_token=binding["pi_launch_token"]), "v": 2})
    state = _read(binding)
    assert state.status == "absent"
    assert "restart CCB" in state.reason


def test_every_required_value_has_the_type_the_extension_writes(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    _write_state(binding, _snapshot(launch_token=binding["pi_launch_token"]))
    # The control for the malformed cases below: the captured shape itself reads ready.
    assert _read(binding).status == "ready"


@pytest.mark.parametrize("overrides", [
    {"launch_token": ""},
    {"launch_token": 5},
    {"runtime_instance_id": ""},
    {"runtime_instance_id": None},
    {"pid": "123"},
    {"pid": True},
    {"pid": None},
    {"seq": -1},
    {"seq": "3"},
    {"seq": None},
    {"mode": 5},
    {"mode": None},
    {"has_ui": "yes"},
    {"idle": None},
    {"editor_empty": 0},
    {"prompt_open": None},
    {"prompt_open": "false"},
    {"updated_at": "now"},
    {"updated_at": True},
])
def test_a_snapshot_with_a_malformed_value_is_absent(tmp_path: Path, overrides: dict) -> None:
    binding = _binding(tmp_path)
    snapshot = _snapshot(launch_token=binding["pi_launch_token"])
    snapshot.update(overrides)
    _write_state(binding, snapshot)
    state = _read(binding)
    # Key presence is not enough: a value the extension cannot have written is not a report.
    assert state.status == "absent"
    assert "restart CCB" in state.reason


def test_a_pre_upgrade_binding_without_a_state_channel_is_absent(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    del binding["pi_launch_token"]
    _write_state(binding)
    state = _read(binding)
    assert state.status == "absent"
    assert "restart CCB" in state.reason


def test_state_from_another_launch_is_mismatch(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    _write_state(binding, _snapshot(launch_token=binding["pi_launch_token"] + "-other"))
    state = _read(binding)
    assert state.status == "mismatch"
    assert "different launch" in state.reason


def test_a_dead_pi_process_is_stale(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    _write_state(binding, _snapshot(pid=_dead_pid(), launch_token=binding["pi_launch_token"]))
    state = _read(binding)
    assert state.status == "stale"
    assert "no longer running" in state.reason


def test_old_and_future_timestamps_are_stale(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    now_ms = int(time.time() * 1000)
    _write_state(binding, _snapshot(launch_token=binding["pi_launch_token"], updated_at=now_ms - 4000))
    old = _read(binding)
    assert old.status == "stale" and "stopped updating" in old.reason
    _write_state(binding, _snapshot(launch_token=binding["pi_launch_token"], updated_at=now_ms + 3000))
    future = _read(binding)
    assert future.status == "stale" and "future" in future.reason


def test_a_timestamp_in_milliseconds_is_the_unit_that_is_read(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    # The extension publishes Date.now(); a seconds value is not silently reinterpreted.
    _write_state(binding, _snapshot(launch_token=binding["pi_launch_token"], updated_at=int(time.time())))
    state = _read(binding)
    assert state.status == "stale" and "stopped updating" in state.reason


@pytest.mark.parametrize("overrides", [
    {"mode": "rpc"},
    {"mode": "json"},
    {"mode": "print"},
    {"has_ui": False},
])
def test_a_non_interactive_pi_is_not_ready(tmp_path: Path, overrides: dict) -> None:
    binding = _binding(tmp_path)
    _write_state(binding, _snapshot(launch_token=binding["pi_launch_token"], **overrides))
    state = _read(binding)
    assert state.status == "mismatch"
    assert "interactive" in state.reason


def test_prompt_busy_and_draft_are_distinguished(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    token = binding["pi_launch_token"]
    _write_state(binding, _snapshot(launch_token=token, prompt_open=True, idle=False))
    assert _read(binding).status == "prompt"
    _write_state(binding, _snapshot(launch_token=token, idle=False, editor_empty=False))
    assert _read(binding).status == "busy"
    _write_state(binding, _snapshot(launch_token=token, idle=True, editor_empty=False))
    state = _read(binding)
    assert state.status == "draft"
    assert "unsent text" in state.reason
    _write_state(binding, _snapshot(launch_token=token, idle=True, editor_empty=True))
    assert _read(binding).status == "ready"


def test_confirmation_needs_a_second_ready_snapshot(monkeypatch, tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    _write_state(binding, _snapshot(launch_token=binding["pi_launch_token"], seq=5))

    def advance(_seconds: float) -> None:
        _write_state(binding, _snapshot(launch_token=binding["pi_launch_token"], seq=6))

    monkeypatch.setattr(time, "sleep", advance)
    assert confirm_pi_ready(binding).status == "ready"


def test_a_snapshot_that_never_advances_is_not_confirmed(monkeypatch, tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    _write_state(binding, _snapshot(launch_token=binding["pi_launch_token"], seq=5))
    ticks = iter([0.0, 0.0, 5.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks, 5.0))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    state = confirm_pi_ready(binding)
    assert state.status == "stale"
    assert "did not report twice" in state.reason


def test_a_snapshot_that_does_not_advance_is_never_ready_inside_the_budget(monkeypatch, tmp_path: Path) -> None:
    # A static snapshot is read again and again inside the ~2 s window; without the sequence
    # comparison that first repeat would confirm a runtime that never published again.
    binding = _binding(tmp_path)
    _write_state(binding, _snapshot(launch_token=binding["pi_launch_token"], seq=5))
    now = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__("t", now["t"] + seconds))

    state = confirm_pi_ready(binding)

    assert state.status == "stale"
    assert "did not report twice" in state.reason
    assert now["t"] == pytest.approx(2.0, abs=0.2)


def test_the_second_snapshot_must_still_be_ready(monkeypatch, tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    token = binding["pi_launch_token"]
    _write_state(binding, _snapshot(launch_token=token, seq=5))

    def advance(_seconds: float) -> None:
        _write_state(binding, _snapshot(launch_token=token, seq=6, idle=False))

    monkeypatch.setattr(time, "sleep", advance)
    assert confirm_pi_ready(binding).status == "busy"


def test_a_busy_or_draft_first_read_returns_without_waiting(monkeypatch, tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    token = binding["pi_launch_token"]
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))
    _write_state(binding, _snapshot(launch_token=token, idle=False))
    assert confirm_pi_ready(binding).status == "busy"
    _write_state(binding, _snapshot(launch_token=token, editor_empty=False))
    assert confirm_pi_ready(binding).status == "draft"
    assert slept == []


def test_a_runtime_replacement_during_confirmation_needs_its_own_second_read(monkeypatch, tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    token = binding["pi_launch_token"]
    _write_state(binding, _snapshot(launch_token=token, seq=5, runtime_instance_id="instance-a"))
    step = {"n": 0}

    def advance(_seconds: float) -> None:
        step["n"] += 1
        if step["n"] == 1:
            # A new runtime took the pane: same binding, new instance, fresh sequence.
            _write_state(binding, _snapshot(launch_token=token, seq=1, runtime_instance_id="instance-b"))
        else:
            _write_state(binding, _snapshot(launch_token=token, seq=2, runtime_instance_id="instance-b"))

    monkeypatch.setattr(time, "sleep", advance)
    assert confirm_pi_ready(binding).status == "ready"
    assert step["n"] == 2


def test_confirmation_respects_the_request_deadline(monkeypatch, tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    _write_state(binding, _snapshot(launch_token=binding["pi_launch_token"]))
    now = {"t": 10.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])
    # A clock the poll advances, so a missing deadline clamp finishes late instead of hanging.
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__("t", now["t"] + seconds))
    state = confirm_pi_ready(binding, deadline=10.0)
    assert state.status == "stale"
    # Already at the deadline: the ~2 s confirmation budget must not extend the request.
    assert now["t"] == 10.0


def test_a_confirmation_that_would_answer_after_the_deadline_is_not_ready(monkeypatch, tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    token = binding["pi_launch_token"]
    _write_state(binding, _snapshot(launch_token=token, seq=5))
    now = {"t": 0.0}
    waits: list[float] = []
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])

    def overshoot(seconds: float) -> None:
        waits.append(seconds)
        now["t"] += 0.5  # the read takes far longer than the 10 ms left
        _write_state(binding, _snapshot(launch_token=token, seq=6))

    monkeypatch.setattr(time, "sleep", overshoot)
    state = confirm_pi_ready(binding, deadline=0.01)
    # The second read IS ready, but it landed past the caller's deadline: not ready.
    assert state.status == "stale"
    # The poll is clamped to the budget left instead of sleeping the full poll interval.
    assert waits == [pytest.approx(0.01)]
