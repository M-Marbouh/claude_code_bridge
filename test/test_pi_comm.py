from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pi_comm import PiSessionError, classify_pane_screen, load_session


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
    assert load_session(tmp_path, session_id).exchange("synthetic").state == "incomplete"

    # Neither a user-row marker nor a thinking part is a terminal assistant text row.
    assistant["message"]["stopReason"] = "stop"
    assistant["message"].pop("errorMessage", None)
    assistant["message"]["content"] = [{"type": "thinking", "thinking": "CCB_DONE: synthetic"}]
    session_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert load_session(tmp_path, session_id).exchange("synthetic").state == "incomplete"


def test_screen_classifier_handles_observed_dialog_busy_and_footer() -> None:
    assert classify_pane_screen((FIXTURES / "screen-tree-selector.txt").read_text()) == "dialog"
    assert classify_pane_screen((FIXTURES / "screen-busy.txt").read_text()) == "busy"
    assert classify_pane_screen((FIXTURES / "screen-idle.txt").read_text()) == "idle"
    assert classify_pane_screen("/work (main)\ndeepseek-flash • high") == "unknown"
    assert classify_pane_screen("blank terminal") == "unknown"


@pytest.mark.parametrize("reply", [
    "see this working copy", "the editor was Working well", "the WORKING marker appeared",
    "tests are Running now", "we are running tests",
    "it is Retrying (after timeout) in the log", "compacting context appeared in the report",
    "we enabled Auto-compacting yesterday", "the output was summarizing branch details",
    "this working copy is clean",
])
def test_screen_reply_words_do_not_make_idle_pane_busy(reply: str) -> None:
    screen = (FIXTURES / "screen-idle.txt").read_text().replace("Already at this point", f"{reply}\nwith another line")
    assert classify_pane_screen(screen) == "idle"


@pytest.mark.parametrize("border", [
    "── ⠙ Reticulating splines ───",
    "───⠙──────",
    "── ⠋ Retrying (1/3) in 2s... (esc to cancel) ───",
    "── * Working ───",
])
def test_screen_editor_border_activity_is_busy(border: str) -> None:
    lines = (FIXTURES / "screen-idle.txt").read_text().splitlines()
    width = len(lines[3].rstrip())
    padded_border = border + "─" * (width - len(border))
    screen = "\n".join([lines[0], padded_border, *lines[2:]])
    assert classify_pane_screen(screen) == "busy"


@pytest.mark.parametrize("loader", [
    " X Working", " >>> Working", " [=====] Working", " Working", " >> >> Working", " ( o o ) Retrying (1/3)",
])
def test_non_embedded_loader_above_plain_editor_box_is_busy(loader: str) -> None:
    screen = "\n".join([
        "", loader, "────────────────────", "", "────────────────────",
        "<CCB_CWD> (master)", "model • high",
    ])
    assert classify_pane_screen(screen) == "busy"


def test_plain_working_paragraph_above_box_is_refuse_safe_busy() -> None:
    screen = "\n".join([
        "", "Working tree clean", "────────────────────", "", "────────────────────",
        "<CCB_CWD> (master)", "model • high",
    ])
    assert classify_pane_screen(screen) == "busy"


def test_editor_draft_is_unknown_even_with_overflow_label() -> None:
    screen = (FIXTURES / "screen-idle.txt").read_text().replace("─" * 8, "─── ↑ 3 more ───", 1)
    screen = screen.replace("\n\n────────────────", "\ndraft\n────────────────", 1)
    assert classify_pane_screen(screen) == "unknown"


def test_short_rule_and_multiline_draft_are_not_idle() -> None:
    screen = "\n".join([
        "reply", "──────", "draft", "", "────────────────────",
        "<CCB_CWD> (master)", "model • high",
    ])
    assert classify_pane_screen(screen) == "unknown"


def test_one_character_top_border_is_not_idle() -> None:
    screen = "\n".join([
        "reply", "─", "", "────────────────────",
        "<CCB_CWD> (master)", "model • high",
    ])
    assert classify_pane_screen(screen) == "unknown"


def _idle_marker_rule_index(lines: list[str]) -> int:
    return max(index for index, line in enumerate(lines) if line.startswith("─" * 8))


def test_real_trust_dialog_screen_is_dialog() -> None:
    screen = (FIXTURES / "screen-trust-dialog.txt").read_text()
    assert "Trust project folder?" in screen
    assert classify_pane_screen(screen) == "dialog"


def test_real_idle_screen_quoting_a_dialog_marker_stays_idle() -> None:
    screen = (FIXTURES / "screen-idle-quoting-marker.txt").read_text()
    assert "does not require project trust" in screen
    assert classify_pane_screen(screen) == "idle"


def test_real_busy_screen_quoting_a_dialog_marker_stays_busy() -> None:
    screen = (FIXTURES / "screen-busy-quoting-marker.txt").read_text()
    assert "does not require project trust" in screen
    assert classify_pane_screen(screen) == "busy"


def test_editor_draft_with_quoted_dialog_marker_is_not_idle() -> None:
    lines = (FIXTURES / "screen-idle-quoting-marker.txt").read_text().splitlines()
    lines[_idle_marker_rule_index(lines) - 1] = "draft text"
    assert classify_pane_screen("\n".join(lines)) != "idle"
