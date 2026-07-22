from __future__ import annotations

import json
from pathlib import Path

from claude_comm import ClaudeLogReader
from codex_comm import CodexLogReader


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")


def _codex_user(text: str, timestamp: str) -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text},
    }


def _codex_assistant(text: str, timestamp: str, *, phase: str | None) -> dict:
    payload = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    if phase is not None:
        payload["phase"] = phase
    return {"timestamp": timestamp, "type": "response_item", "payload": payload}


def _codex_twin(text: str, timestamp: str) -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": text},
    }


def test_codex_latest_exchanges_prefers_final_and_suppresses_commentary_and_twin(
    tmp_path: Path,
) -> None:
    task_id = "20260722-100000-001-1"
    log = tmp_path / "rollout.jsonl"
    _write_jsonl(
        log,
        [
            _codex_user(f"CCB_REQ_ID: {task_id}\nQuestion", "2026-07-22T10:00:00Z"),
            _codex_twin("Commentary", "2026-07-22T10:00:01Z"),
            _codex_assistant("Commentary", "2026-07-22T10:00:01Z", phase="commentary"),
            _codex_twin(f"Final reply\nCCB_DONE: {task_id}", "2026-07-22T10:00:02Z"),
            _codex_assistant(
                f"Final reply\nCCB_DONE: {task_id}",
                "2026-07-22T10:00:02Z",
                phase="final_answer",
            ),
        ],
    )
    reader = CodexLogReader(
        log_path=log,
        session_id_filter="fixed",
        work_dir=tmp_path,
        allow_stale_switch=False,
    )

    exchanges = reader.latest_exchanges(5)

    assert exchanges == [
        {
            "ts": "2026-07-22T10:00:02Z",
            "req_id": task_id,
            "question": f"CCB_REQ_ID: {task_id}\nQuestion",
            "reply": "Final reply",
        }
    ]


def test_codex_latest_exchanges_keeps_legacy_event_only_reply(tmp_path: Path) -> None:
    log = tmp_path / "rollout.jsonl"
    _write_jsonl(
        log,
        [
            _codex_user("Manual question", "2026-07-22T10:00:00Z"),
            _codex_twin("Legacy reply", "2026-07-22T10:00:01Z"),
        ],
    )
    reader = CodexLogReader(
        log_path=log,
        session_id_filter="fixed",
        work_dir=tmp_path,
        allow_stale_switch=False,
    )

    assert reader.latest_exchanges(1) == [
        {
            "ts": "2026-07-22T10:00:01Z",
            "req_id": None,
            "question": "Manual question",
            "reply": "Legacy reply",
        }
    ]


def test_codex_latest_exchanges_does_not_treat_commentary_twin_as_final(
    tmp_path: Path,
) -> None:
    log = tmp_path / "rollout.jsonl"
    _write_jsonl(
        log,
        [
            _codex_user("Manual question", "2026-07-22T10:00:00Z"),
            _codex_twin("Still working", "2026-07-22T10:00:01Z"),
            _codex_assistant("Still working", "2026-07-22T10:00:01Z", phase="commentary"),
        ],
    )
    reader = CodexLogReader(
        log_path=log,
        session_id_filter="fixed",
        work_dir=tmp_path,
        allow_stale_switch=False,
    )

    assert reader.latest_exchanges(1) == []


def _claude_entry(role: str, text: str, timestamp: str) -> dict:
    return {
        "timestamp": timestamp,
        "type": role,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def test_claude_latest_exchanges_uses_final_assistant_per_user_turn(tmp_path: Path) -> None:
    session = tmp_path / "claude.jsonl"
    _write_jsonl(
        session,
        [
            _claude_entry("user", "Manual one", "2026-07-22T10:00:00Z"),
            _claude_entry("assistant", "Draft", "2026-07-22T10:00:01Z"),
            _claude_entry("assistant", "Final one", "2026-07-22T10:00:02Z"),
            _claude_entry("user", "Manual two", "2026-07-22T10:00:03Z"),
            _claude_entry("assistant", "Final two", "2026-07-22T10:00:04Z"),
        ],
    )
    reader = ClaudeLogReader(work_dir=tmp_path, allow_session_switch=False)
    reader.set_preferred_session(session)

    assert reader.latest_exchanges(2) == [
        {
            "ts": "2026-07-22T10:00:02Z",
            "req_id": None,
            "question": "Manual one",
            "reply": "Final one",
        },
        {
            "ts": "2026-07-22T10:00:04Z",
            "req_id": None,
            "question": "Manual two",
            "reply": "Final two",
        },
    ]


def test_exchange_req_id_must_be_on_first_non_empty_line() -> None:
    embedded = "Question quoting a marker\nCCB_REQ_ID: foreign"
    anchored = "\n  CCB_REQ_ID: current\nQuestion"

    assert CodexLogReader._exchange_req_id(embedded) is None
    assert ClaudeLogReader._exchange_req_id(embedded) is None
    assert CodexLogReader._exchange_req_id(anchored) == "current"
    assert ClaudeLogReader._exchange_req_id(anchored) == "current"


def test_claude_fixed_session_does_not_switch_to_newer_log(tmp_path: Path) -> None:
    preferred = tmp_path / "preferred.jsonl"
    newer = tmp_path / "newer.jsonl"
    _write_jsonl(preferred, [_claude_entry("assistant", "Preferred", "2026-07-22T10:00:00Z")])
    _write_jsonl(newer, [_claude_entry("assistant", "Newer", "2026-07-22T10:01:00Z")])
    newer.touch()
    reader = ClaudeLogReader(root=tmp_path, work_dir=tmp_path, allow_session_switch=False)
    reader.set_preferred_session(preferred)

    assert reader.latest_message() == "Preferred"
