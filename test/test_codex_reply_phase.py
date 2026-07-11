"""
Tests for phase-based Codex reply assembly.

Codex writes every assistant message to its rollout log twice (an
event_msg/agent_message twin plus a response_item/message record). Interim
progress is tagged phase=="commentary" and the final report phase=="final_answer".
The reply Claude receives must contain ONLY the final report -- no commentary,
no duplicate lines -- while older logs without phase metadata keep working.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from askd.adapters import codex as codex_adapter
from askd.adapters.base import ProviderRequest, QueuedTask
from ccb_protocol import REQ_ID_PREFIX, make_req_id
from codex_comm import CodexLogReader, CodexTurnContext, read_latest_turn_context


# --------------------------------------------------------------------------
# _extract_event phase tagging
# --------------------------------------------------------------------------

def _resp_message(text: str, phase: str | None) -> dict:
    payload = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    if phase is not None:
        payload["phase"] = phase
    return {"type": "response_item", "payload": payload}


def _event_agent_message(text: str) -> dict:
    return {"type": "event_msg", "payload": {"type": "agent_message", "message": text}}


def _user_message(text: str) -> dict:
    return {"type": "event_msg", "payload": {"type": "user_message", "message": text}}


def test_extract_event_tags_final_answer() -> None:
    role, text, phase = CodexLogReader._extract_event(_resp_message("the report", "final_answer"))
    assert role == "assistant"
    assert text == "the report"
    assert phase == "final_answer"


def test_extract_event_tags_commentary() -> None:
    _, _, phase = CodexLogReader._extract_event(_resp_message("working on it", "commentary"))
    assert phase == "commentary"


def test_extract_event_event_twin_is_event_phase() -> None:
    # The agent_message twin carries no phase -> labelled "event" so it is
    # excluded from the final-answer selection (this is what kills the dup).
    role, text, phase = CodexLogReader._extract_event(_event_agent_message("the report"))
    assert role == "assistant"
    assert phase == "event"


def test_extract_event_response_item_without_phase_is_event() -> None:
    _, _, phase = CodexLogReader._extract_event(_resp_message("legacy text", None))
    assert phase == "event"


def test_extract_event_user_has_empty_phase() -> None:
    role, _, phase = CodexLogReader._extract_event(_user_message("a question"))
    assert role == "user"
    assert phase == ""


# --------------------------------------------------------------------------
# _assemble_reply
# --------------------------------------------------------------------------

def test_assemble_reply_returns_final_only() -> None:
    req_id = "20260603-101010-000-1-1"
    final_chunks = [f"Implemented the fix.\nFiles: a.ts\nCCB_DONE: {req_id}"]
    combined = "\n".join([
        "I'm checking blast radius first.",
        "tsc passed, running tests.",
        f"Implemented the fix.\nFiles: a.ts\nCCB_DONE: {req_id}",
    ])
    reply = codex_adapter._assemble_reply(final_chunks, combined, req_id)
    assert reply == "Implemented the fix.\nFiles: a.ts"
    assert "checking blast radius" not in reply
    assert "CCB_DONE" not in reply


def test_assemble_reply_legacy_fallback_when_no_phase() -> None:
    # Old Codex: no final_answer captured -> fall back to full anchor->DONE span.
    req_id = "20260603-101010-000-1-2"
    combined = f"Legacy single message reply.\nCCB_DONE: {req_id}"
    reply = codex_adapter._assemble_reply([], combined, req_id)
    assert reply == "Legacy single message reply."


# --------------------------------------------------------------------------
# Adapter end-to-end: handle_task drives phase filtering + DONE detection
# --------------------------------------------------------------------------

class _FakeBackend:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_text(self, pane_id, text) -> None:
        self.sent.append(text)

    def is_alive(self, pane_id) -> bool:
        return True


class _FakeSession:
    def __init__(self, work_dir: Path) -> None:
        self.work_dir = str(work_dir)
        self.data = {}
        self.codex_session_path = None
        self.codex_session_id = None
        self.bindings: list[dict] = []

    def ensure_pane(self):
        return True, "pane-1"

    def update_codex_log_binding(self, **kwargs) -> None:
        self.bindings.append(kwargs)


class _ScriptedReader:
    """Yields a fixed sequence of (role, text, phase) events, one per call."""

    def __init__(self, events: list[tuple[str, str, str]], log_path: Path | None = None) -> None:
        self._events = list(events)
        self._log_path = log_path

    def capture_state(self) -> dict:
        return {"log_path": self._log_path, "offset": 0}

    def wait_for_event(self, state, timeout):
        if self._events:
            return self._events.pop(0), state
        return None, state

    def current_log_path(self):
        return None


def _drive_handle_task(
    monkeypatch,
    tmp_path: Path,
    req_id: str,
    events: list[tuple[str, str, str]],
    *,
    show_tier: bool = False,
    include_anchor: bool = True,
    timeout_s: float = 5.0,
    log_path: Path | None = None,
    session_obj: _FakeSession | None = None,
):
    # Prepend the user anchor so anchor_seen flips before assistant events.
    scripted = list(events)
    if include_anchor:
        scripted = [("user", f"{REQ_ID_PREFIX} {req_id}", "")] + scripted

    session = session_obj or _FakeSession(tmp_path)
    monkeypatch.setattr(codex_adapter, "load_project_session", lambda wd: session)
    monkeypatch.setattr(codex_adapter, "get_backend_for_session", lambda data: _FakeBackend())
    monkeypatch.setattr(codex_adapter, "CodexLogReader", lambda **kw: _ScriptedReader(scripted, log_path=log_path))
    monkeypatch.setattr(codex_adapter, "notify_completion", lambda **kw: None)
    monkeypatch.setattr(codex_adapter, "_write_log", lambda line: None)

    req = ProviderRequest(
        client_id="c", work_dir=str(tmp_path), timeout_s=timeout_s, quiet=True,
        message="do the thing", caller="claude", req_id=req_id,
        show_tier=show_tier,
    )
    task = QueuedTask(request=req, created_ms=0, req_id=req_id, done_event=threading.Event())

    adapter = codex_adapter.CodexAdapter()
    return adapter.handle_task(task)


def test_handle_task_real_ordering_event_twin_done_first(monkeypatch, tmp_path: Path) -> None:
    # Models the ACTUAL Codex rollout ordering: each message is logged as an
    # event twin then the canonical response_item. The final answer's event
    # twin carries CCB_DONE and arrives BEFORE the phase=="final_answer" record,
    # so the loop breaks on the twin. The DONE-bearing message must still be
    # captured as the final report (regression guard for the twin-ordering bug).
    req_id = make_req_id()
    final = f"Implemented the fix.\nFiles: a.ts\nCCB_DONE: {req_id}"
    result = _drive_handle_task(monkeypatch, tmp_path, req_id, [
        ("assistant", "I'm checking blast radius first.", "commentary"),
        ("assistant", "I'm checking blast radius first.", "event"),   # twin
        ("assistant", "tsc passed, running the suite.", "commentary"),
        ("assistant", "tsc passed, running the suite.", "event"),     # twin
        ("assistant", final, "event"),                                # final twin (has DONE) FIRST
        ("assistant", final, "final_answer"),                         # canonical (never reached)
    ])

    assert result.done_seen is True
    assert result.reply == "Implemented the fix.\nFiles: a.ts"
    assert "blast radius" not in result.reply
    assert "tsc passed" not in result.reply
    assert "CCB_DONE" not in result.reply


def test_handle_task_final_answer_carries_done(monkeypatch, tmp_path: Path) -> None:
    # Variant where the canonical phase=="final_answer" is the DONE-bearing
    # message (no preceding event twin with DONE).
    req_id = make_req_id()
    result = _drive_handle_task(monkeypatch, tmp_path, req_id, [
        ("assistant", "Working on it.", "commentary"),
        ("assistant", f"Implemented the fix.\nFiles: a.ts\nCCB_DONE: {req_id}", "final_answer"),
    ])

    assert result.done_seen is True
    assert result.reply == "Implemented the fix.\nFiles: a.ts"
    assert "Working on it" not in result.reply


def test_handle_task_legacy_event_only_falls_back(monkeypatch, tmp_path: Path) -> None:
    # No final_answer phase anywhere (old Codex) -> legacy accumulation reply.
    req_id = make_req_id()
    result = _drive_handle_task(monkeypatch, tmp_path, req_id, [
        ("assistant", f"Legacy reply body.\nCCB_DONE: {req_id}", "event"),
    ])

    assert result.done_seen is True
    assert result.reply == "Legacy reply body."


def test_handle_task_unbound_requires_anchor(monkeypatch, tmp_path: Path) -> None:
    req_id = make_req_id()

    result = _drive_handle_task(
        monkeypatch,
        tmp_path,
        req_id,
        [("assistant", f"Wrong pane output.\nCCB_DONE: {req_id}", "event")],
        include_anchor=False,
        timeout_s=0.05,
    )

    assert result.done_seen is False
    assert result.anchor_seen is False
    assert result.reply == ""


def test_handle_task_anchor_confirmed_completion_repairs_binding(monkeypatch, tmp_path: Path) -> None:
    req_id = make_req_id()
    sid = "12345678-1234-1234-1234-123456789abc"
    log_path = tmp_path / f"{sid}.jsonl"
    log_path.write_text("", encoding="utf-8")
    session = _FakeSession(tmp_path)

    result = _drive_handle_task(
        monkeypatch,
        tmp_path,
        req_id,
        [("assistant", f"Done.\nCCB_DONE: {req_id}", "final_answer")],
        log_path=log_path,
        session_obj=session,
    )

    assert result.done_seen is True
    assert session.bindings == [{"log_path": str(log_path), "session_id": sid}]


def test_scan_latest_candidate_requires_anchor_and_honors_exclusions(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "codex-root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_SESSION_ROOT", str(root))
    req_id = make_req_id()
    base_id = "aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"
    sibling_id = "bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb"
    base_log = root / f"{base_id}.jsonl"
    sibling_log = root / f"{sibling_id}.jsonl"
    for path, sid in [(base_log, base_id), (sibling_log, sibling_id)]:
        path.write_text(
            "\n".join([
                json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(tmp_path)}}),
                json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": f"{REQ_ID_PREFIX} {req_id}"}}),
            ]) + "\n",
            encoding="utf-8",
        )
    base_log.touch()
    sibling_log.touch()

    selected = codex_adapter._scan_latest_candidate_log(
        tmp_path,
        exclude_session_ids={sibling_id},
        req_id=req_id,
    )

    assert selected == base_log

    missing = codex_adapter._scan_latest_candidate_log(
        tmp_path,
        exclude_session_ids={base_id, sibling_id},
        req_id=req_id,
    )
    assert missing is None


def test_read_latest_turn_context_reads_bound_log(tmp_path: Path) -> None:
    log_path = tmp_path / "codex.jsonl"
    log_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "s1"}}),
                "{not-json",
                json.dumps({"type": "turn_context", "payload": {"model": "old", "effort": "low", "sandbox_policy": {"type": "read-only"}}}),
                json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.4-mini", "effort": "medium", "sandbox_policy": {"type": "workspace-write"}}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    ctx = read_latest_turn_context(log_path, session_id_filter="s1")

    assert ctx == CodexTurnContext(
        model="gpt-5.4-mini",
        effort="medium",
        sandbox="workspace-write",
        raw_sandbox_policy={"type": "workspace-write"},
    )


def test_handle_task_footer_off_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CCB_CODEX_SHOW_TIER", raising=False)
    req_id = make_req_id()

    result = _drive_handle_task(monkeypatch, tmp_path, req_id, [
        ("assistant", f"Plain reply.\nCCB_DONE: {req_id}", "final_answer"),
    ])

    assert result.reply == "Plain reply."


def test_handle_task_footer_on_per_request_flag(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CCB_CODEX_SHOW_TIER", raising=False)
    req_id = make_req_id()

    result = _drive_handle_task(monkeypatch, tmp_path, req_id, [
        ("assistant", f"Plain reply.\nCCB_DONE: {req_id}", "final_answer"),
    ], show_tier=True)

    assert result.reply == "Plain reply.\n[codex model=unknown effort=unknown sandbox=unknown]"


def test_handle_task_footer_on_unknown_when_context_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CCB_CODEX_SHOW_TIER", "1")
    req_id = make_req_id()

    result = _drive_handle_task(monkeypatch, tmp_path, req_id, [
        ("assistant", f"Plain reply.\nCCB_DONE: {req_id}", "final_answer"),
    ])

    assert result.reply == "Plain reply.\n[codex model=unknown effort=unknown sandbox=unknown]"


def test_handle_task_footer_uses_single_provider_key(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CCB_CODEX_SHOW_TIER", "1")
    monkeypatch.setattr(
        codex_adapter,
        "read_latest_turn_context",
        lambda *a, **k: CodexTurnContext(model="gpt-5.4-mini", effort="medium", sandbox="workspace-write"),
    )
    req_id = make_req_id()

    result = _drive_handle_task(monkeypatch, tmp_path, req_id, [
        ("assistant", f"Codex reply.\nCCB_DONE: {req_id}", "final_answer"),
    ])

    assert result.reply == "Codex reply.\n[codex model=gpt-5.4-mini effort=medium sandbox=workspace-write]"
