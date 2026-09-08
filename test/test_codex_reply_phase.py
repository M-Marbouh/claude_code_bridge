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

import caskd_session
from askd.adapters import codex as codex_adapter
from askd.adapters.base import ProviderRequest, QueuedTask, ResolvedRoute
from ccb_protocol import DONE_PREFIX, REQ_ID_PREFIX, make_req_id
from codex_comm import CodexLogReader, CodexTurnContext, read_latest_turn_context
from live_sessions import LiveSession, Resolution


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
    terminal_reply = f"Implemented the fix.\nFiles: a.ts\nCCB_DONE: {req_id}"
    combined = "\n".join([
        "I'm checking blast radius first.",
        "tsc passed, running tests.",
        terminal_reply,
    ])
    reply = codex_adapter._assemble_reply(terminal_reply, "Earlier final.", combined, req_id)
    assert reply == "Implemented the fix.\nFiles: a.ts"
    assert "checking blast radius" not in reply
    assert "Earlier final" not in reply
    assert "CCB_DONE" not in reply


def test_assemble_reply_uses_latest_final_for_degraded_completion() -> None:
    req_id = "20260603-101010-000-1-2"
    combined = "Earlier final.\nLatest final."
    reply = codex_adapter._assemble_reply(None, "Latest final.", combined, req_id)
    assert reply == "Latest final."


def test_assemble_reply_legacy_fallback_when_no_phase() -> None:
    # Old Codex: no final_answer captured -> fall back to full anchor->DONE span.
    req_id = "20260603-101010-000-1-3"
    combined = f"Legacy single message reply.\nCCB_DONE: {req_id}"
    reply = codex_adapter._assemble_reply(None, None, combined, req_id)
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
    suppress_completion_hook: bool = False,
    notifications: list[dict] | None = None,
    caller_work_dir: str = "",
):
    # Prepend the user anchor so anchor_seen flips before assistant events.
    scripted = list(events)
    if include_anchor:
        scripted = [("user", f"{REQ_ID_PREFIX} {req_id}", "")] + scripted

    session = session_obj or _FakeSession(tmp_path)
    monkeypatch.setattr(codex_adapter, "load_project_session", lambda wd: session)
    monkeypatch.setattr(codex_adapter, "get_backend_for_session", lambda data: _FakeBackend())
    monkeypatch.setattr(codex_adapter, "CodexLogReader", lambda **kw: _ScriptedReader(scripted, log_path=log_path))
    monkeypatch.setattr(
        codex_adapter,
        "notify_completion",
        lambda **kw: notifications.append(kw) if notifications is not None else None,
    )
    monkeypatch.setattr(codex_adapter, "_write_log", lambda line: None)

    req = ProviderRequest(
        client_id="c", work_dir=str(tmp_path), timeout_s=timeout_s, quiet=True,
        message="do the thing", caller="claude", req_id=req_id,
        show_tier=show_tier,
        suppress_completion_hook=suppress_completion_hook,
        caller_work_dir=caller_work_dir,
    )
    task = QueuedTask(request=req, created_ms=0, req_id=req_id, done_event=threading.Event())

    adapter = codex_adapter.CodexAdapter()
    return adapter.handle_task(task)


def test_handle_task_persists_proven_result_before_any_notification(
    monkeypatch, tmp_path: Path
) -> None:
    """Task 2 (round-1 claim rejected on review): Codex calls
    `notify_completion` before returning its result, so "a notification
    failure cannot prevent a later save" is NOT sufficient -- the adapter
    must persist the proven result to disk FIRST, server-side, before
    notification is even attempted. Verified here against the REAL
    `handle_task`, not a reimplementation: `persist_proven_result` is
    tracked to prove no notification has happened yet at the moment it
    runs, and exactly one notification follows it."""
    req_id = make_req_id()
    final = f"Implemented the fix.\nCCB_DONE: {req_id}"
    notifications: list[dict] = []
    persisted_before_notify: list[bool] = []

    def _tracking_persist(*_args, **_kwargs):
        persisted_before_notify.append(len(notifications) == 0)

    monkeypatch.setattr(codex_adapter, "persist_proven_result", _tracking_persist)

    _drive_handle_task(
        monkeypatch,
        tmp_path,
        req_id,
        [("assistant", final, "final_answer")],
        notifications=notifications,
    )

    assert persisted_before_notify == [True]
    assert len(notifications) == 1


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


def test_handle_task_replayed_finals_return_only_done_bearing_final(
    monkeypatch, tmp_path: Path
) -> None:
    req_id = make_req_id()
    result = _drive_handle_task(monkeypatch, tmp_path, req_id, [
        ("assistant", "Historical report that must not be delivered.", "final_answer"),
        ("assistant", "Historical report that must not be delivered.", "final_answer"),
        ("assistant", "Final complete report.\nCCB_DONE: " + req_id, "event"),
    ])

    assert result.done_seen is True
    assert result.reply == "Final complete report."
    assert "Historical report" not in result.reply


def test_handle_task_can_suppress_completion_hook(monkeypatch, tmp_path: Path) -> None:
    req_id = make_req_id()
    notifications: list[dict] = []

    result = _drive_handle_task(
        monkeypatch,
        tmp_path,
        req_id,
        [("assistant", f"Acknowledged.\nCCB_DONE: {req_id}", "final_answer")],
        suppress_completion_hook=True,
        notifications=notifications,
    )

    assert result.done_seen is True
    assert notifications == []


def test_handle_task_suppresses_notification_when_result_could_not_be_saved(
    monkeypatch, tmp_path: Path
) -> None:
    """Item 5: `persist_proven_result` swallowing write failures and the
    adapter notifying anyway is exactly the failure save-before-notify
    exists to prevent. Force the save to report FAILED (a receipt WAS
    expected) and prove notification is suppressed -- NO TERMINAL SEND
    (via `notify_completion`) occurs, even though the task itself still
    completed successfully and its result is still returned to the
    caller."""
    req_id = make_req_id()
    final = f"Implemented the fix.\nCCB_DONE: {req_id}"
    notifications: list[dict] = []
    monkeypatch.setattr(
        codex_adapter, "persist_proven_result", lambda *a, **k: "failed"
    )

    result = _drive_handle_task(
        monkeypatch,
        tmp_path,
        req_id,
        [("assistant", final, "final_answer")],
        notifications=notifications,
    )

    assert result.done_seen is True
    assert result.reply == "Implemented the fix."
    # NO TERMINAL SEND OCCURRED: notify_completion was never reached.
    assert notifications == []


def test_handle_task_notifies_normally_when_no_receipt_was_expected(
    monkeypatch, tmp_path: Path
) -> None:
    """The other half of Item 5: NO_RECEIPT (a foreground/notify-only call
    with nothing to save to) must NOT be treated as a failure -- it must
    not suppress notification."""
    req_id = make_req_id()
    final = f"Implemented the fix.\nCCB_DONE: {req_id}"
    notifications: list[dict] = []
    monkeypatch.setattr(
        codex_adapter, "persist_proven_result", lambda *a, **k: "no_receipt"
    )

    result = _drive_handle_task(
        monkeypatch,
        tmp_path,
        req_id,
        [("assistant", final, "final_answer")],
        notifications=notifications,
    )

    assert result.done_seen is True
    assert len(notifications) == 1


def test_handle_task_delivery_only_stops_at_anchor_without_capturing_reply(
    monkeypatch, tmp_path: Path
) -> None:
    req_id = make_req_id()
    session = _FakeSession(tmp_path)
    backend = _FakeBackend()
    reader = _ScriptedReader(
        [
            ("user", f"{REQ_ID_PREFIX} {req_id}", ""),
            ("assistant", "This later local output must not be captured.", "final_answer"),
        ]
    )
    notifications: list[dict] = []

    monkeypatch.setattr(codex_adapter, "load_project_session", lambda wd: session)
    monkeypatch.setattr(codex_adapter, "get_backend_for_session", lambda data: backend)
    monkeypatch.setattr(codex_adapter, "CodexLogReader", lambda **kw: reader)
    monkeypatch.setattr(codex_adapter, "notify_completion", lambda **kw: notifications.append(kw))
    monkeypatch.setattr(codex_adapter, "_write_log", lambda line: None)

    req = ProviderRequest(
        client_id="c",
        work_dir=str(tmp_path),
        timeout_s=5.0,
        quiet=True,
        message="peer request\n\nCCB_REPLY_TARGET: /tmp/sender",
        caller="claude",
        req_id=req_id,
        delivery_only=True,
        suppress_completion_hook=False,
    )
    task = QueuedTask(request=req, created_ms=0, req_id=req_id, done_event=threading.Event())

    result = codex_adapter.CodexAdapter().handle_task(task)

    assert result.exit_code == 0
    assert result.reply == "Peer message delivered."
    assert result.anchor_seen is True
    assert result.done_seen is False
    assert result.extra == {"confirmation": "observed"}
    assert notifications == []
    assert reader._events == [("assistant", "This later local output must not be captured.", "final_answer")]
    assert f"{REQ_ID_PREFIX} {req_id}" in backend.sent[0]
    assert DONE_PREFIX not in backend.sent[0]
    assert "send it explicitly with ask --peer" in backend.sent[0]


def test_handle_task_routes_completion_fallback_to_caller_work_dir(
    monkeypatch, tmp_path: Path
) -> None:
    req_id = make_req_id()
    notifications: list[dict] = []
    caller_work_dir = str(tmp_path / "sender")

    _drive_handle_task(
        monkeypatch,
        tmp_path,
        req_id,
        [("assistant", f"Done.\nCCB_DONE: {req_id}", "final_answer")],
        notifications=notifications,
        caller_work_dir=caller_work_dir,
    )

    assert notifications[0]["work_dir"] == caller_work_dir


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


def test_handle_task_records_transcript_proof_at_anchor_even_on_timeout(
    monkeypatch, tmp_path: Path
) -> None:
    """Item 2: transcript identity must be recorded at the moment the
    request ANCHOR is confirmed, not from finalization state. A request
    that times out AFTER its anchor was seen (no CCB_DONE ever arrives)
    must still have proof recorded -- that proof is what lets a LATER,
    genuinely completed reply be found by recovery, even though this
    request itself ended incomplete."""
    req_id = make_req_id()
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    log_path = tmp_path / f"{sid}.jsonl"
    log_path.write_text("", encoding="utf-8")
    persisted: list[dict] = []
    monkeypatch.setattr(
        codex_adapter,
        "persist_proven_result",
        lambda req_id, **kwargs: persisted.append(kwargs) or "saved",
    )

    result = _drive_handle_task(
        monkeypatch,
        tmp_path,
        req_id,
        [],  # anchor only (prepended by _drive_handle_task) -- no reply ever arrives
        timeout_s=0.05,
        log_path=log_path,
    )

    assert result.anchor_seen is True
    assert result.done_seen is False
    assert len(persisted) == 1
    assert persisted[0]["transcript_path"] == str(log_path)


def test_handle_task_never_records_transcript_proof_when_unanchored(
    monkeypatch, tmp_path: Path
) -> None:
    """Item 2's other half: a request that times out WITHOUT its anchor
    ever being confirmed must NOT have any transcript proof recorded --
    the adapter never established that this transcript belongs to this
    request at all, so recording one anyway would be proof it never
    earned."""
    req_id = make_req_id()
    sid = "11111111-2222-3333-4444-555555555555"
    log_path = tmp_path / f"{sid}.jsonl"
    log_path.write_text("", encoding="utf-8")
    persisted: list[dict] = []
    monkeypatch.setattr(
        codex_adapter,
        "persist_proven_result",
        lambda req_id, **kwargs: persisted.append(kwargs) or "saved",
    )

    result = _drive_handle_task(
        monkeypatch,
        tmp_path,
        req_id,
        [("assistant", f"Wrong pane output.\nCCB_DONE: {req_id}", "event")],
        include_anchor=False,
        timeout_s=0.05,
        log_path=log_path,
    )

    assert result.anchor_seen is False
    assert result.done_seen is False
    assert len(persisted) == 1
    assert persisted[0]["transcript_path"] == ""
    assert persisted[0]["conversation_id"] == ""


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


def test_scan_uses_recorded_root_not_daemon_account(monkeypatch, tmp_path: Path) -> None:
    req_id = make_req_id()
    own_root = tmp_path / "pane-account" / "sessions"
    daemon_root = tmp_path / "daemon-account" / "sessions"
    for root in (own_root, daemon_root):
        root.mkdir(parents=True)
        (root / "conversation.jsonl").write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {"id": root.parent.name, "cwd": str(tmp_path)}}),
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": f"{REQ_ID_PREFIX} {req_id}"}}),
        ]) + "\n")
    monkeypatch.setenv("CODEX_SESSION_ROOT", str(daemon_root))
    assert codex_adapter._scan_latest_candidate_log(tmp_path, req_id=req_id, root=own_root) == own_root / "conversation.jsonl"


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


def test_state_at_req_anchor_replays_anchor_written_before_log_switch(tmp_path: Path) -> None:
    req_id = make_req_id()
    log_path = tmp_path / "rollout.jsonl"
    prefix = json.dumps({"type": "session_meta", "payload": {"cwd": str(tmp_path)}}) + "\n"
    anchor = json.dumps(_user_message(f"CCB_REQ_ID: {req_id}\n\nrequest")) + "\n"
    final = json.dumps(_event_agent_message(f"Finished.\nCCB_DONE: {req_id}")) + "\n"
    log_path.write_text(prefix + anchor + final, encoding="utf-8")

    state = codex_adapter._state_at_req_anchor(log_path, req_id)
    reader = CodexLogReader(log_path=log_path, work_dir=tmp_path, allow_stale_switch=False)
    event, state = reader.try_get_event(state)

    assert event == ("user", f"CCB_REQ_ID: {req_id}\n\nrequest", "")
    event, _state = reader.try_get_event(state)
    assert event == ("assistant", f"Finished.\nCCB_DONE: {req_id}", "event")


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


def _write_rollout(path: Path, *, sid: str, cwd: Path, req_id: str, meta_extra: dict | None = None) -> None:
    payload = {"id": sid, "cwd": str(cwd)}
    payload.update(meta_extra or {})
    path.write_text(
        "\n".join([
            json.dumps({"type": "session_meta", "payload": payload}),
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": f"{REQ_ID_PREFIX} {req_id}"}}),
        ]) + "\n",
        encoding="utf-8",
    )


def test_scan_latest_candidate_skips_descendant_transcript_even_when_newer(monkeypatch, tmp_path: Path) -> None:
    """A native subagent rollout quotes the parent's anchor and shares its cwd.

    Selection is mtime-ordered, so the descendant can outrank the conversation
    that actually received the request. It must lose in both orderings.
    """
    root = tmp_path / "codex-root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_SESSION_ROOT", str(root))
    req_id = make_req_id()
    top_id = "cccccccc-3333-3333-3333-cccccccccccc"
    child_id = "dddddddd-4444-4444-4444-dddddddddddd"
    top_log = root / f"{top_id}.jsonl"
    child_log = root / f"{child_id}.jsonl"

    _write_rollout(top_log, sid=top_id, cwd=tmp_path, req_id=req_id,
                   meta_extra={"session_id": top_id, "parent_thread_id": None, "source": "cli"})
    _write_rollout(child_log, sid=child_id, cwd=tmp_path, req_id=req_id,
                   meta_extra={"session_id": top_id, "parent_thread_id": top_id,
                               "source": {"subagent": {"other": "guardian"}}})

    for newer, older in ((child_log, top_log), (top_log, child_log)):
        import os as _os
        now = _os.stat(newer).st_mtime
        _os.utime(older, (now - 60, now - 60))
        _os.utime(newer, (now, now))
        assert codex_adapter._scan_latest_candidate_log(tmp_path, req_id=req_id) == top_log

    # Sanity: the descendant is a genuine candidate apart from its lineage.
    assert codex_adapter._codex_log_work_dir_matches(child_log, tmp_path)
    assert codex_adapter._codex_log_is_descendant(child_log)
    assert not codex_adapter._codex_log_is_descendant(top_log)


def test_handle_task_rejects_helper_anchor_from_initial_reader(monkeypatch, tmp_path: Path) -> None:
    req_id = make_req_id()
    child = tmp_path / "child.jsonl"
    _write_rollout(child, sid="child", cwd=tmp_path, req_id=req_id,
                   meta_extra={"parent_thread_id": "parent", "source": {"subagent": {}}})
    session = _FakeSession(tmp_path)
    result = _drive_handle_task(
        monkeypatch, tmp_path, req_id,
        [("assistant", f"wrong helper reply\nCCB_DONE: {req_id}", "final_answer")],
        log_path=child, session_obj=session, timeout_s=0.02,
    )
    assert not result.anchor_seen
    assert not result.done_seen
    assert "wrong helper reply" not in result.reply
    assert session.bindings == []


def test_routed_pair_refuses_shared_native_conversation(monkeypatch, tmp_path: Path) -> None:
    req_id = make_req_id()
    live = LiveSession("dest", "codex", "launch", pane_id="%2", terminal="tmux",
                       session_file=str(tmp_path / "binding"), work_dir=str(tmp_path),
                       ccb_project_id="project")
    route = ResolvedRoute(live_id="dest", launch_id="launch", caller_live_id="caller",
                          pane_id="%2", terminal="tmux", session_file=live.session_file,
                          ccb_project_id="project")
    session = _FakeSession(tmp_path)
    session.codex_session_id = "conversation-owned-by-sibling"
    monkeypatch.setattr(codex_adapter, "validate_route", lambda **_: Resolution(session=live))
    monkeypatch.setattr(codex_adapter, "session_data_from_live", lambda _: {"active": True})
    monkeypatch.setattr(codex_adapter, "CodexProjectSession", lambda **_: session)
    monkeypatch.setattr(codex_adapter, "_sibling_conversation_ids",
                        lambda _: {"conversation-owned-by-sibling"})
    backend = _FakeBackend()
    monkeypatch.setattr(codex_adapter, "get_backend_for_session", lambda _: backend)
    req = ProviderRequest(client_id="c", work_dir=str(tmp_path), timeout_s=1, quiet=True,
                          message="do it", caller="codex", req_id=req_id, route=route,
                          caller_pane_id="%1", caller_terminal="tmux")
    result = codex_adapter.CodexAdapter().handle_task(
        QueuedTask(request=req, created_ms=0, req_id=req_id, done_event=threading.Event())
    )
    assert result.exit_code == 1
    assert "sibling's conversation" in result.reply
    assert backend.sent == []


def test_scan_latest_candidate_keeps_rollouts_without_lineage_metadata(monkeypatch, tmp_path: Path) -> None:
    """Older Codex builds write neither field; those rollouts stay eligible."""
    root = tmp_path / "codex-root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_SESSION_ROOT", str(root))
    req_id = make_req_id()
    legacy_id = "eeeeeeee-5555-5555-5555-eeeeeeeeeeee"
    legacy_log = root / f"{legacy_id}.jsonl"
    _write_rollout(legacy_log, sid=legacy_id, cwd=tmp_path, req_id=req_id)

    assert not codex_adapter._codex_log_is_descendant(legacy_log)
    assert codex_adapter._scan_latest_candidate_log(tmp_path, req_id=req_id) == legacy_log


# --------------------------------------------------------------------------
# Routed requests: send-time revalidation, and reading from the routed
# session's own conversation rather than a work_dir-wide default.
# --------------------------------------------------------------------------


def test_handle_task_refuses_routed_destination_that_became_invalid(monkeypatch, tmp_path: Path) -> None:
    """Enqueue-time validation (daemon.py) is not the only checkpoint: the
    adapter must re-confirm immediately before it would send, and refuse
    outright -- never fall back to the work_dir-wide session, never send
    anything -- when the routed destination no longer checks out.
    """
    req_id = make_req_id()
    backend = _FakeBackend()
    monkeypatch.setattr(codex_adapter, "get_backend_for_session", lambda data: backend)
    monkeypatch.setattr(
        codex_adapter,
        "validate_route",
        lambda **_kw: Resolution(error="unavailable", detail="routed destination is gone"),
    )
    monkeypatch.setattr(codex_adapter, "_write_log", lambda line: None)

    req = ProviderRequest(
        client_id="c", work_dir=str(tmp_path), timeout_s=5.0, quiet=True,
        message="do it", caller="claude", req_id=req_id,
        route=ResolvedRoute(live_id="s2", launch_id="ai-1", caller_live_id="s1"),
    )
    task = QueuedTask(request=req, created_ms=0, req_id=req_id, done_event=threading.Event())

    result = codex_adapter.CodexAdapter().handle_task(task)

    assert result.exit_code == 1
    assert result.status == codex_adapter.COMPLETION_STATUS_FAILED
    assert "no longer available" in result.reply
    assert backend.sent == []


def test_handle_task_reads_reply_from_routed_sessions_own_file(monkeypatch, tmp_path: Path) -> None:
    """A routed request must bind its reply reader to the ROUTED session's
    own `codex_session_path`/`codex_session_id`, never the work_dir-wide
    `.codex-session` default -- even when that default names a real,
    different session that would otherwise happily satisfy the adapter.
    """
    req_id = make_req_id()

    routed_log_path = tmp_path / "routed.jsonl"
    routed_log_path.write_text("", encoding="utf-8")
    own_session_file = tmp_path / "codex-session-s2.json"
    own_session_file.write_text(
        json.dumps(
            {
                "pane_id": "%9",
                "codex_session_path": str(routed_log_path),
                "codex_session_id": "routed-sid",
                "work_dir": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    live = LiveSession(
        live_id="s2",
        provider="codex",
        launch_id="ai-1",
        pane_id="%9",
        terminal="tmux",
        work_dir=str(tmp_path),
        session_file=str(own_session_file),
    )

    # The work_dir-wide default: a DIFFERENT, otherwise-healthy session. If
    # the adapter ever fell back to this, the reader would bind to it
    # instead of the routed session's own file.
    wrong_session = _FakeSession(tmp_path)
    wrong_session.codex_session_path = str(tmp_path / "wrong.jsonl")
    wrong_session.codex_session_id = "wrong-sid"
    monkeypatch.setattr(codex_adapter, "load_project_session", lambda wd: wrong_session)

    monkeypatch.setattr(codex_adapter, "validate_route", lambda **_kw: Resolution(session=live))
    monkeypatch.setattr(codex_adapter, "get_backend_for_session", lambda data: _FakeBackend())
    monkeypatch.setattr(caskd_session, "get_backend_for_session", lambda data: _FakeBackend())
    monkeypatch.setattr(codex_adapter, "notify_completion", lambda **kw: None)
    monkeypatch.setattr(codex_adapter, "_write_log", lambda line: None)

    captured_reader_kwargs: dict = {}

    def _fake_reader(**kwargs):
        captured_reader_kwargs.update(kwargs)
        scripted = [
            ("user", f"{REQ_ID_PREFIX} {req_id}", ""),
            ("assistant", f"Done.\nCCB_DONE: {req_id}", "final_answer"),
        ]
        return _ScriptedReader(scripted, log_path=kwargs.get("log_path"))

    monkeypatch.setattr(codex_adapter, "CodexLogReader", _fake_reader)

    req = ProviderRequest(
        client_id="c", work_dir=str(tmp_path), timeout_s=5.0, quiet=True,
        message="do it", caller="claude", req_id=req_id,
        route=ResolvedRoute(live_id="s2", launch_id="ai-1", caller_live_id="s1"),
    )
    task = QueuedTask(request=req, created_ms=0, req_id=req_id, done_event=threading.Event())

    result = codex_adapter.CodexAdapter().handle_task(task)

    assert result.done_seen is True
    assert result.reply == "Done."
    assert str(captured_reader_kwargs["log_path"]) == str(routed_log_path)
    assert captured_reader_kwargs["session_id_filter"] == "routed-sid"
    assert captured_reader_kwargs["work_dir"] == tmp_path


def test_handle_task_refuses_when_send_time_caller_evidence_matches_nothing(monkeypatch, tmp_path: Path) -> None:
    # Hole 2, exercised through the REAL (unmocked) validate_route against
    # a real registry record: the send-time caller re-check must fail
    # CLOSED when the request's own caller_pane_id matches no session in
    # the launch -- not silently let the send through. NO TERMINAL SEND
    # OCCURRED is the guard.
    import json as _json

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    work_dir = tmp_path / "project"
    work_dir.mkdir(parents=True)
    registry_dir = home / ".ccb" / "run"
    registry_dir.mkdir(parents=True)
    (registry_dir / "ccb-session-ai-1.json").write_text(
        _json.dumps(
            {
                "ccb_session_id": "ai-1",
                "ccb_project_id": "proj-1",
                "work_dir": str(work_dir),
                "terminal": "tmux",
                "updated_at": 9999999999,
                "providers": {"codex": {"pane_id": "%2"}},
                "live_sessions": [
                    {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
                    {"live_id": "s2", "provider": "codex", "pane_id": "%3", "active": True},
                ],
            }
        ),
        encoding="utf-8",
    )

    req_id = make_req_id()
    backend = _FakeBackend()
    monkeypatch.setattr(codex_adapter, "get_backend_for_session", lambda data: backend)
    monkeypatch.setattr(codex_adapter, "_write_log", lambda line: None)

    req = ProviderRequest(
        client_id="c",
        work_dir=str(work_dir),
        timeout_s=5.0,
        quiet=True,
        message="do it",
        caller="claude",
        req_id=req_id,
        # Claims pane %999, which matches no session in the launch at all.
        caller_pane_id="%999",
        caller_terminal="tmux",
        route=ResolvedRoute(
            live_id="s2",
            launch_id="ai-1",
            pane_id="%3",
            terminal="tmux",
            session_file="",
            ccb_project_id="proj-1",
        ),
    )
    task = QueuedTask(request=req, created_ms=0, req_id=req_id, done_event=threading.Event())

    result = codex_adapter.CodexAdapter().handle_task(task)

    assert result.exit_code == 1
    assert result.status == codex_adapter.COMPLETION_STATUS_FAILED
    assert "no longer available" in result.reply
    # NO TERMINAL SEND OCCURRED.
    assert backend.sent == []


def test_handle_task_refuses_duplicate_pool_route_missing_caller_evidence(monkeypatch, tmp_path: Path) -> None:
    # Item 1, exercised through the ADAPTER SEND checkpoint with the REAL
    # (unmocked) validate_route: a duplicate-provider pool, a route whose
    # saved caller_live_id names the SIBLING (not even the destination),
    # and no fresh caller evidence on the request at all. Must refuse
    # before any terminal send.
    import json as _json

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    work_dir = tmp_path / "project"
    work_dir.mkdir(parents=True)
    registry_dir = home / ".ccb" / "run"
    registry_dir.mkdir(parents=True)
    (registry_dir / "ccb-session-ai-1.json").write_text(
        _json.dumps(
            {
                "ccb_session_id": "ai-1",
                "ccb_project_id": "proj-1",
                "work_dir": str(work_dir),
                "terminal": "tmux",
                "updated_at": 9999999999,
                "providers": {"codex": {"pane_id": "%2"}},
                "live_sessions": [
                    {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
                    {"live_id": "s2", "provider": "codex", "pane_id": "%3", "active": True},
                ],
            }
        ),
        encoding="utf-8",
    )

    req_id = make_req_id()
    backend = _FakeBackend()
    monkeypatch.setattr(codex_adapter, "get_backend_for_session", lambda data: backend)
    monkeypatch.setattr(codex_adapter, "_write_log", lambda line: None)

    req = ProviderRequest(
        client_id="c",
        work_dir=str(work_dir),
        timeout_s=5.0,
        quiet=True,
        message="do it",
        caller="claude",
        req_id=req_id,
        # No caller_pane_id/caller_terminal at all on the request.
        route=ResolvedRoute(
            live_id="s2",
            launch_id="ai-1",
            caller_live_id="s1",  # saved caller == the sibling
            pane_id="%3",
            terminal="tmux",
            session_file="",
            ccb_project_id="proj-1",
        ),
    )
    task = QueuedTask(request=req, created_ms=0, req_id=req_id, done_event=threading.Event())

    result = codex_adapter.CodexAdapter().handle_task(task)

    assert result.exit_code == 1
    assert result.status == codex_adapter.COMPLETION_STATUS_FAILED
    assert "no longer available" in result.reply
    # NO TERMINAL SEND OCCURRED.
    assert backend.sent == []
