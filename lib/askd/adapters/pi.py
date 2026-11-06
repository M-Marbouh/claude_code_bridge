"""Unified askd adapter for a Pi coding-agent pane."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from askd.adapters.base import BaseProviderAdapter, ProviderRequest, ProviderResult, QueuedTask, route_session_key
from askd_runtime import log_path, write_log
from ccb_protocol import strip_done_text, wrap_codex_prompt
from completion_hook import (
    COMPLETION_STATUS_CANCELLED,
    COMPLETION_STATUS_COMPLETED,
    COMPLETION_STATUS_FAILED,
    COMPLETION_STATUS_INCOMPLETE,
    notify_completion,
)
from pane_registry import validate_route
from pi_comm import (
    PiExchange,
    PiSessionError,
    capture_confirmed_state,
    capture_screen_state,
    classify_pane_screen,
    load_candidate_sessions,
    screen_context,
)
from project_id import compute_ccb_project_id
from providers import PI_SPEC
from task_receipts import PersistOutcome, persist_proven_result
from terminal import get_backend_for_session

PARK_TIMEOUT_S = 60.0
ANCHOR_TIMEOUT_S = 12.0
# Pi writes its retry context_edit ~1 ms after a failed row; a session read can land in between.
ERROR_GRACE_S = 10.0
# A retry whose pane stays confirmed idle this long has stopped without writing a reply.
RETRY_IDLE_S = 30.0


def _write_log(line: str) -> None:
    write_log(log_path(PI_SPEC.log_file_name), line)


def _load_destination(req: ProviderRequest) -> tuple[Path | None, dict[str, Any] | None, str]:
    work_dir = Path(req.work_dir).expanduser().resolve()
    session_path = Path(req.route.session_file).expanduser() if req.route.present else None
    from pi_comm import load_session_file

    loaded = load_session_file(work_dir, session_path)
    if not loaded:
        return None, None, "No active Pi session found for work_dir."
    path, data = loaded
    if data.get("active") is not True:
        return path, data, "Pi session is inactive."
    if not str(data.get("pi_session_id") or "").strip() or not str(data.get("pi_session_dir") or "").strip():
        return path, data, "Pi session binding is incomplete."
    try:
        if Path(str(data.get("work_dir") or "")).expanduser().resolve() != work_dir:
            return path, data, "Pi session work directory does not match the request."
    except OSError:
        return path, data, "Pi session work directory cannot be verified."
    if req.route.present:
        try:
            project_id = compute_ccb_project_id(work_dir)
        except Exception:
            project_id = ""
        route = validate_route(
            live_id=req.route.live_id, launch_id=req.route.launch_id, provider="pi",
            pane_id=req.route.pane_id, terminal=req.route.terminal,
            session_file=req.route.session_file, ccb_project_id=req.route.ccb_project_id,
            request_project_id=project_id,
            caller_pane_id=req.caller_pane_id or req.route.caller_pane_id,
            caller_terminal=req.caller_terminal or req.route.caller_terminal,
            caller_live_id=req.route.caller_live_id, caller_token=req.route.caller_token,
        )
        if not route.ok:
            return path, data, f"Routed Pi destination is no longer available ({route.error})."
    return path, data, ""


def _check_pane(data: dict[str, Any], work_dir: Path):
    pane_id = str(data.get("pane_id") or "").strip()
    marker = str(data.get("pane_title_marker") or "").strip()
    if not pane_id or not marker:
        return None, "Pi session pane identity is incomplete."
    backend = get_backend_for_session(data)
    if backend is None:
        return None, "Terminal backend not available."
    try:
        if not backend.is_alive(pane_id):
            return None, "Pi pane is not available."
        finder = getattr(backend, "find_pane_by_title_marker", None)
        resolved = str(finder(marker, str(work_dir)) or "").strip() if callable(finder) else ""
    except Exception as exc:
        return None, f"Pi pane could not be verified: {type(exc).__name__}: {exc}"
    if resolved != pane_id:
        return None, "Pi pane identity changed or could not be verified."
    return backend, ""


# The capture helpers live in pi_comm so autonew's Pi gate and this adapter share one implementation.
_capture_screen_state = capture_screen_state
_screen_context = screen_context
_capture_confirmed_state = capture_confirmed_state


def _anchor_landed(
    session_dir: Path,
    work_dir: Path,
    req_id: str,
    *,
    bound_session_id: str,
    start_dir: str | Path | None,
    cache: dict,
) -> bool:
    try:
        found, _exchange, lookup_error, _candidates = _find_request_session(
            session_dir, work_dir, req_id, bound_session_id=bound_session_id, start_dir=start_dir, cache=cache,
        )
    except PiSessionError:
        return True  # the main poll reports the session error
    return found is not None or lookup_error == "ambiguous"


def _find_request_session(
    session_dir: Path,
    work_dir: Path,
    req_id: str,
    *,
    bound_session_id: str,
    start_dir: str | Path | None,
    cache: dict,
):
    candidates = load_candidate_sessions(
        session_dir, work_dir, bound_session_id=bound_session_id, start_dir=start_dir, cache=cache,
    )
    matches = [candidate for candidate in candidates if candidate.anchors(req_id)]
    if len(matches) > 1:
        return None, None, "ambiguous", candidates
    if not matches:
        return None, None, "missing", candidates
    return matches[0], matches[0].exchange(req_id), "", candidates


class PiAdapter(BaseProviderAdapter):
    @property
    def key(self) -> str:
        return "pi"

    @property
    def spec(self):
        return PI_SPEC

    @property
    def session_filename(self) -> str:
        return ".pi-session"

    def load_session(self, work_dir: Path):
        from pi_comm import load_session_file

        loaded = load_session_file(work_dir)
        return loaded[1] if loaded else None

    def compute_session_key(self, session: Any) -> str:
        if not isinstance(session, dict):
            return "pi:unknown"
        project_id = str(session.get("ccb_project_id") or "").strip()
        return f"pi:{project_id}" if project_id else "pi:unknown"

    def handle_task(self, task: QueuedTask) -> ProviderResult:
        req = task.request
        started = time.monotonic()
        work_dir = Path(req.work_dir).expanduser().resolve()
        _session_path, session, error = _load_destination(req)
        session_key = route_session_key(self.key, req.route) if req.route.present else self.compute_session_key(session)
        if error or session is None:
            return self._finish(task, ProviderResult(
                exit_code=1, reply=error or "Pi session unavailable.", req_id=task.req_id,
                session_key=session_key, done_seen=False, status=COMPLETION_STATUS_FAILED,
            ))
        backend, error = _check_pane(session, work_dir)
        if error or backend is None:
            return self._finish(task, ProviderResult(
                exit_code=1, reply=error, req_id=task.req_id, session_key=session_key,
                done_seen=False, status=COMPLETION_STATUS_FAILED,
            ))

        session_dir = Path(session["pi_session_dir"]).expanduser()
        bound_session_id = str(session["pi_session_id"])
        pi_session_id = bound_session_id
        session_cache: dict = {}
        try:
            candidates = load_candidate_sessions(
                session_dir, work_dir, bound_session_id=bound_session_id,
                start_dir=session.get("start_dir"), cache=session_cache,
            )
        except PiSessionError as exc:
            return self._finish(task, ProviderResult(
                exit_code=1, reply=f"Pi session {exc.kind}: {exc}", req_id=task.req_id,
                session_key=session_key, done_seen=False, status=COMPLETION_STATUS_FAILED,
            ))
        existing_anchors = [
            candidate for candidate in candidates
            if candidate.anchors(task.req_id)
        ]
        if existing_anchors:
            ambiguous = len(existing_anchors) > 1 or any(
                len(candidate.anchors(task.req_id)) > 1 for candidate in existing_anchors
            )
            reason = f"Pi request anchor is ambiguous for {task.req_id}." if ambiguous else f"Pi request anchor already exists for {task.req_id}."
            return self._finish(task, ProviderResult(
                exit_code=2 if ambiguous else 1, reply=reason,
                req_id=task.req_id, session_key=session_key, done_seen=False,
                status=COMPLETION_STATUS_INCOMPLETE if ambiguous else COMPLETION_STATUS_FAILED,
            ))

        screen_state = _capture_confirmed_state(backend, str(session["pane_id"]))
        if screen_state != "idle":
            if screen_state == "dialog":
                reason = "Pi is waiting for a project-trust or selector decision in its pane."
            elif screen_state == "busy":
                reason = "Pi pane is busy; refusing to send to avoid steering an active turn."
            else:
                reason = "Pi pane readiness could not be verified; refusing to send."
            return self._finish(task, ProviderResult(
                exit_code=1, reply=reason, req_id=task.req_id, session_key=session_key,
                done_seen=False, status=COMPLETION_STATUS_FAILED,
            ))

        # Phase 3 (E5): role-policy recheck as the LAST step before injection.
        from delivery_policy import refusal_for_request

        refusal = refusal_for_request(req, self.key, expected_pane=str(session["pane_id"]), session=session)
        if refusal:
            _write_log(f"[INFO] policy refused provider=pi req_id={task.req_id} reason={refusal}")
            return self._finish(task, ProviderResult(
                exit_code=1, reply=refusal, req_id=task.req_id, session_key=session_key,
                done_seen=False, status=COMPLETION_STATUS_FAILED,
            ))
        try:
            backend.send_text(str(session["pane_id"]), wrap_codex_prompt(req.message, task.req_id))
        except Exception as exc:
            return self._finish(task, ProviderResult(
                exit_code=1, reply=f"Failed to send to Pi pane: {exc}", req_id=task.req_id,
                session_key=session_key, done_seen=False, status=COMPLETION_STATUS_FAILED,
            ))
        anchor_deadline = time.monotonic() + ANCHOR_TIMEOUT_S

        deadline = None if float(req.timeout_s) < 0 else started + float(req.timeout_s)
        parked_at: float | None = None
        errored_at: float | None = None
        errored_id = ""
        retry_idle_at: float | None = None
        anchor_seen = False
        anchor_ms: int | None = None
        saw_busy = False
        transcript_path = ""
        result = None
        while True:
            now = time.monotonic()
            if task.cancel_event and task.cancel_event.is_set():
                result = ProviderResult(
                    exit_code=1, reply="Pi request cancelled before a terminal reply was observed.",
                    req_id=task.req_id, session_key=session_key, done_seen=False,
                    anchor_seen=anchor_seen, anchor_ms=anchor_ms, status=COMPLETION_STATUS_CANCELLED,
                )
                break
            if not backend.is_alive(str(session["pane_id"])):
                result = ProviderResult(
                    exit_code=2, reply="Pi pane died during the request.", req_id=task.req_id,
                    session_key=session_key, done_seen=False, anchor_seen=anchor_seen,
                    anchor_ms=anchor_ms, status=COMPLETION_STATUS_INCOMPLETE,
                )
                break
            screen_state, _screen = _capture_screen_state(backend, str(session["pane_id"]))
            saw_busy = saw_busy or screen_state == "busy"
            try:
                found, exchange, lookup_error, candidates = _find_request_session(
                    session_dir, work_dir, task.req_id, bound_session_id=bound_session_id,
                    start_dir=session.get("start_dir"), cache=session_cache,
                )
            except PiSessionError as exc:
                result = ProviderResult(
                    exit_code=1, reply=f"Pi session {exc.kind}: {exc}", req_id=task.req_id,
                    session_key=session_key, done_seen=False, anchor_seen=anchor_seen,
                    anchor_ms=anchor_ms, status=COMPLETION_STATUS_FAILED,
                )
                break
            bound_session = next((candidate for candidate in candidates if candidate.session_id == pi_session_id), None)
            if bound_session is not None:
                transcript_path = str(bound_session.path)
            if lookup_error == "ambiguous":
                exchange = PiExchange("ambiguous", reason=f"Pi request anchor is ambiguous for {task.req_id}.")
            elif found is not None:
                pi_session_id = found.session_id
                transcript_path = str(found.path)
            if exchange:
                anchor_seen = exchange.state not in {"missing"}
                if anchor_seen and anchor_ms is None:
                    anchor_ms = int((now - started) * 1000)
                if exchange.state == "completed":
                    result = ProviderResult(
                        exit_code=0, reply=strip_done_text(exchange.reply, task.req_id), req_id=task.req_id,
                        session_key=session_key, done_seen=True, anchor_seen=True,
                        anchor_ms=anchor_ms, done_ms=int((now - started) * 1000),
                        log_path=transcript_path, status=COMPLETION_STATUS_COMPLETED,
                        extra={"destination_transcript_path": transcript_path, "destination_conversation_id": pi_session_id},
                    )
                    break
                if exchange.state in {"ambiguous", "branch_switched", "superseded", "incomplete", "invalid"}:
                    result = ProviderResult(
                        exit_code=2, reply=exchange.reason, req_id=task.req_id,
                        session_key=session_key, done_seen=False, anchor_seen=anchor_seen,
                        anchor_ms=anchor_ms, log_path=transcript_path,
                        status=COMPLETION_STATUS_INCOMPLETE,
                        extra={"destination_transcript_path": transcript_path, "destination_conversation_id": pi_session_id},
                    )
                    break
                if exchange.state == "parked" and parked_at is None:
                    parked_at = now
                if exchange.state == "parked" and task.has_actionable_waiting and task.has_actionable_waiting():
                    result = ProviderResult(
                        exit_code=2,
                        reply="[Incomplete: Pi turn was interrupted; released for queued work.]",
                        req_id=task.req_id, session_key=session_key, done_seen=False,
                        anchor_seen=anchor_seen, anchor_ms=anchor_ms, log_path=transcript_path,
                        status=COMPLETION_STATUS_INCOMPLETE,
                        extra={"destination_transcript_path": transcript_path, "destination_conversation_id": pi_session_id},
                    )
                    break
                if exchange.state != "parked":
                    parked_at = None
                if exchange.state == "errored":
                    # Grace is per failed entry id: a later failed retry starts a fresh window.
                    if errored_at is None or exchange.error_id != errored_id:
                        errored_id = exchange.error_id
                        errored_at = now
                    elif errored_at is not None and now - errored_at >= ERROR_GRACE_S:
                        result = ProviderResult(
                            exit_code=2, reply=exchange.reason, req_id=task.req_id,
                            session_key=session_key, done_seen=False, anchor_seen=anchor_seen,
                            anchor_ms=anchor_ms, log_path=transcript_path,
                            status=COMPLETION_STATUS_INCOMPLETE,
                            extra={"destination_transcript_path": transcript_path, "destination_conversation_id": pi_session_id},
                        )
                        break
                else:
                    errored_id = ""
                    errored_at = None
                if exchange.state == "retrying":
                    # Pi retries on its own: watch the pane instead of the parked deadline.
                    # Unknown reads reset the timer, so an operator draft here waits for the overall
                    # timeout just like pending; that residual is unchanged from today.
                    if _capture_confirmed_state(backend, str(session["pane_id"])) == "idle":
                        if retry_idle_at is None:
                            retry_idle_at = now
                        elif now - retry_idle_at >= RETRY_IDLE_S:
                            result = ProviderResult(
                                exit_code=2, reply="Pi retry stopped before a reply",
                                req_id=task.req_id, session_key=session_key, done_seen=False,
                                anchor_seen=anchor_seen, anchor_ms=anchor_ms, log_path=transcript_path,
                                status=COMPLETION_STATUS_INCOMPLETE,
                                extra={"destination_transcript_path": transcript_path, "destination_conversation_id": pi_session_id},
                            )
                            break
                    else:
                        retry_idle_at = None
                else:
                    retry_idle_at = None
                if parked_at is not None and now - parked_at >= PARK_TIMEOUT_S:
                    result = ProviderResult(
                        exit_code=2, reply="Pi interrupted request continuation deadline expired.",
                        req_id=task.req_id, session_key=session_key, done_seen=False,
                        anchor_seen=anchor_seen, anchor_ms=anchor_ms, log_path=transcript_path,
                        status=COMPLETION_STATUS_INCOMPLETE,
                        extra={"destination_transcript_path": transcript_path, "destination_conversation_id": pi_session_id},
                    )
                    break
            if not anchor_seen and now >= anchor_deadline:
                deadline_state = screen_state
                if deadline_state == "idle":
                    deadline_state = _capture_confirmed_state(backend, str(session["pane_id"]))
                if deadline_state in {"idle", "dialog"} and _anchor_landed(
                    session_dir, work_dir, task.req_id, bound_session_id=bound_session_id,
                    start_dir=session.get("start_dir"), cache=session_cache,
                ):
                    # The request reached Pi's transcript during confirmation; let the
                    # next poll track it instead of reporting a lost request.
                    continue
                if deadline_state == "idle":
                    if saw_busy:
                        result = ProviderResult(
                            exit_code=1,
                            reply="Pi showed activity, but CCB could not locate this request in its session folder.",
                            req_id=task.req_id, session_key=session_key, done_seen=False,
                            anchor_seen=False, status=COMPLETION_STATUS_FAILED,
                            log_path=transcript_path or None,
                        )
                    else:
                        result = ProviderResult(
                            exit_code=1, reply="Pi did not accept the request (input not received).",
                            req_id=task.req_id, session_key=session_key, done_seen=False,
                            anchor_seen=False, status=COMPLETION_STATUS_FAILED,
                            log_path=transcript_path or None,
                        )
                    break
                if deadline_state == "dialog":
                    result = ProviderResult(
                        exit_code=1, reply="Pi did not accept the request (Pi is showing a dialog).",
                        req_id=task.req_id, session_key=session_key, done_seen=False,
                        anchor_seen=False, status=COMPLETION_STATUS_FAILED,
                        log_path=transcript_path or None,
                    )
                    break
                if deadline_state == "busy":
                    saw_busy = True
            if deadline is not None and now >= deadline:
                result = ProviderResult(
                    exit_code=2, reply="Pi request timed out before a terminal CCB_DONE was observed.",
                    req_id=task.req_id, session_key=session_key, done_seen=False,
                    anchor_seen=anchor_seen, anchor_ms=anchor_ms, log_path=transcript_path,
                    status=COMPLETION_STATUS_INCOMPLETE,
                    extra={"destination_transcript_path": transcript_path, "destination_conversation_id": pi_session_id},
                )
                break
            time.sleep(0.25)
        return self._finish(task, result, transcript_path=transcript_path, conversation_id=pi_session_id)

    def _finish(self, task: QueuedTask, result: ProviderResult, *, transcript_path: str = "", conversation_id: str = "") -> ProviderResult:
        req = task.request
        try:
            persisted = persist_proven_result(
                task.req_id, reply=result.reply, status=result.status,
                transcript_path=transcript_path, conversation_id=conversation_id,
            )
        except Exception:
            persisted = PersistOutcome.FAILED
        if req.suppress_completion_hook or persisted == PersistOutcome.FAILED:
            return result
        notify_completion(
            provider="pi", output_file=req.output_path, reply=result.reply, req_id=task.req_id,
            done_seen=result.done_seen, status=result.status, caller=req.caller,
            email_req_id=req.email_req_id, email_msg_id=req.email_msg_id, email_from=req.email_from,
            work_dir=req.caller_work_dir or req.work_dir,
            caller_pane_id=req.caller_pane_id or (req.route.caller_pane_id if req.route.present else ""),
            caller_terminal=req.caller_terminal or (req.route.caller_terminal if req.route.present else ""),
            caller_live_id=req.route.caller_live_id if req.route.present else "",
            route_launch_id=req.route.launch_id if req.route.present else "",
        )
        return result
