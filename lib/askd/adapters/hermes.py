"""Unified askd adapter for a Hermes CLI pane."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from askd.adapters.base import BaseProviderAdapter, PaneDeathTracker, ProviderRequest, ProviderResult, QueuedTask, probe_pane_liveness, route_session_key
from askd_runtime import log_path, write_log
from askd_timeout import codex_idle_timeout_s, codex_max_wait_s, positive_timeout_s
from ccb_protocol import wrap_codex_prompt
from completion_hook import (
    COMPLETION_STATUS_CANCELLED,
    COMPLETION_STATUS_COMPLETED,
    COMPLETION_STATUS_FAILED,
    COMPLETION_STATUS_INCOMPLETE,
    notify_completion,
)
from hermes_comm import HermesStateError, load_session_file, read_exchange, session_db_path
from pane_registry import validate_route
from project_id import compute_ccb_project_id
from providers import HASKD_SPEC
from task_receipts import PersistOutcome, persist_proven_result
from terminal import get_backend_for_session


def _write_log(line: str) -> None:
    write_log(log_path(HASKD_SPEC.log_file_name), line)


def _normalize_path(value: str) -> str:
    try:
        return str(Path(value).expanduser().resolve())
    except Exception:
        return os.path.normcase(os.path.abspath(os.path.expanduser(value)))


def _load_destination(req: ProviderRequest) -> tuple[Path | None, dict[str, Any] | None, str]:
    work_dir = Path(req.work_dir).expanduser().resolve()
    session_path = Path(req.route.session_file).expanduser() if req.route.present else None
    loaded = load_session_file(work_dir, session_path)
    if not loaded:
        return None, None, "No active Hermes session found for work_dir."
    path, data = loaded
    if data.get("active") is not True:
        return path, data, "Hermes session is inactive."
    recorded_work_dir = str(data.get("work_dir") or "").strip()
    if not recorded_work_dir or _normalize_path(recorded_work_dir) != _normalize_path(str(work_dir)):
        return path, data, "Hermes session work directory does not match the request."
    if req.route.present:
        try:
            request_project_id = compute_ccb_project_id(work_dir)
        except Exception:
            request_project_id = ""
        outcome = validate_route(
            live_id=req.route.live_id,
            launch_id=req.route.launch_id,
            provider="hermes",
            pane_id=req.route.pane_id,
            terminal=req.route.terminal,
            session_file=req.route.session_file,
            ccb_project_id=req.route.ccb_project_id,
            request_project_id=request_project_id,
            caller_pane_id=req.caller_pane_id or req.route.caller_pane_id,
            caller_terminal=req.caller_terminal or req.route.caller_terminal,
            caller_live_id=req.route.caller_live_id,
            caller_token=req.route.caller_token,
        )
        if not outcome.ok:
            return path, data, f"Routed Hermes destination is no longer available ({outcome.error})."
    return path, data, ""


def _check_pane(data: dict[str, Any], work_dir: Path) -> tuple[Any | None, str]:
    pane_id = str(data.get("pane_id") or "").strip()
    marker = str(data.get("pane_title_marker") or "").strip()
    terminal = str(data.get("terminal") or "").strip().lower()
    if not pane_id or not marker:
        return None, "Hermes session pane identity is incomplete."
    backend = get_backend_for_session(data)
    if backend is None:
        return None, "Terminal backend not available."
    try:
        if not backend.is_alive(pane_id):
            return None, "Hermes pane is not available."
        finder = getattr(backend, "find_pane_by_title_marker", None)
        resolved = str(finder(marker, str(work_dir)) or "").strip() if callable(finder) else ""
    except Exception as exc:
        return None, f"Hermes pane could not be verified: {type(exc).__name__}: {exc}"
    if resolved != pane_id:
        return None, "Hermes pane identity changed or could not be verified."
    return backend, ""


class HermesAdapter(BaseProviderAdapter):
    """Read exact Hermes exchanges from the database bound to a CCB pane."""

    @property
    def key(self) -> str:
        return "hermes"

    @property
    def spec(self):
        return HASKD_SPEC

    @property
    def session_filename(self) -> str:
        return ".hermes-session"

    def load_session(self, work_dir: Path):
        loaded = load_session_file(work_dir)
        return loaded[1] if loaded else None

    def compute_session_key(self, session: Any) -> str:
        if not isinstance(session, dict):
            return "hermes:unknown"
        project_id = str(session.get("ccb_project_id") or "").strip()
        return f"hermes:{project_id}" if project_id else "hermes:unknown"

    def handle_task(self, task: QueuedTask) -> ProviderResult:
        req = task.request
        started = time.monotonic()
        started_ms = int(time.time() * 1000)
        work_dir = Path(req.work_dir).expanduser().resolve()
        session_path, session, error = _load_destination(req)
        if error or session is None:
            return self._finish(task, ProviderResult(
                exit_code=1, reply=error or "Hermes session unavailable.", req_id=task.req_id,
                session_key=self.compute_session_key(session), done_seen=False, status=COMPLETION_STATUS_FAILED,
            ))

        session_key = route_session_key(self.key, req.route) if req.route.present else self.compute_session_key(session)
        backend, error = _check_pane(session, work_dir)
        if error or backend is None:
            return self._finish(task, ProviderResult(
                exit_code=1, reply=error, req_id=task.req_id, session_key=session_key,
                done_seen=False, status=COMPLETION_STATUS_FAILED,
            ))
        pane_id = str(session["pane_id"])
        try:
            db_path = session_db_path(session)
        except HermesStateError as exc:
            return self._finish(task, ProviderResult(
                exit_code=1, reply=str(exc), req_id=task.req_id, session_key=session_key,
                done_seen=False, status=COMPLETION_STATUS_FAILED,
            ))

        try:
            # Validate accessibility/schema before submitting text so an incompatible DB never
            # gets confused with a request that merely has no reply yet.
            existing = read_exchange(db_path, work_dir, task.req_id)
            if existing is not None:
                return self._finish(task, ProviderResult(
                    exit_code=1, reply=f"Hermes request anchor already exists for {task.req_id}.",
                    req_id=task.req_id, session_key=session_key, done_seen=False,
                    status=COMPLETION_STATUS_FAILED,
                ), database_path=str(db_path), conversation_id=existing.session_id)
        except HermesStateError as exc:
            status = COMPLETION_STATUS_INCOMPLETE if exc.kind in {"ambiguous", "rotated"} else COMPLETION_STATUS_FAILED
            if exc.kind == "ambiguous":
                message = f"Hermes ambiguous anchor: {exc}"
            elif exc.kind == "rotated":
                message = f"Hermes session rotated: {exc}"
            else:
                message = f"Hermes state DB {exc.kind}: {exc}"
            return self._finish(task, ProviderResult(
                exit_code=1, reply=message, req_id=task.req_id,
                session_key=session_key, done_seen=False, status=status,
            ), database_path=str(db_path))

        prompt = wrap_codex_prompt(req.message, task.req_id)
        try:
            backend.send_text(pane_id, prompt)
        except Exception as exc:
            return self._finish(task, ProviderResult(
                exit_code=1, reply=f"Failed to send to Hermes pane: {exc}", req_id=task.req_id,
                session_key=session_key, done_seen=False, status=COMPLETION_STATUS_FAILED,
            ), database_path=str(db_path))

        progress_mode = not req.timeout_explicit and not req.delivery_only
        idle_timeout = positive_timeout_s(req.idle_timeout_s, codex_idle_timeout_s()) if progress_mode else None
        max_wait = positive_timeout_s(req.max_wait_s, codex_max_wait_s()) if progress_mode else None
        deadline = started + max_wait if max_wait is not None else (None if float(req.timeout_s) < 0 else started + float(req.timeout_s))
        last_change_at = time.monotonic()
        last_fingerprint: tuple | None = None
        anchor_seen = False
        anchor_ms: int | None = None
        pane_death = PaneDeathTracker()
        last_pane_check = 0.0
        db_session_id = ""
        db_snapshot = None
        reply = "Hermes request incomplete: no terminal CCB_DONE was observed."
        result_status = COMPLETION_STATUS_INCOMPLETE
        exit_code = 2
        done_ms: int | None = None
        while True:
            if task.cancel_event and task.cancel_event.is_set():
                reply = "Hermes request cancelled before a terminal reply was observed."
                result_status = COMPLETION_STATUS_CANCELLED
                break
            now = time.monotonic()
            if now - last_pane_check >= 1.0:
                liveness = probe_pane_liveness(backend, pane_id)
                if pane_death.observe(liveness):
                    reply = "Hermes pane died during the request."
                    result_status = COMPLETION_STATUS_INCOMPLETE
                    break
                try:
                    finder = getattr(backend, "find_pane_by_title_marker")
                    if str(finder(session["pane_title_marker"], str(work_dir)) or "").strip() != pane_id:
                        reply = "Hermes pane identity changed during the request."
                        result_status = COMPLETION_STATUS_INCOMPLETE
                        break
                except Exception:
                    reply = "Hermes pane identity could not be verified during the request."
                    result_status = COMPLETION_STATUS_INCOMPLETE
                    break
                last_pane_check = now
            try:
                exchange = read_exchange(db_path, work_dir, task.req_id)
            except HermesStateError as exc:
                if exc.kind == "ambiguous":
                    reply = f"Hermes ambiguous anchor: {exc}"
                    result_status = COMPLETION_STATUS_INCOMPLETE
                elif exc.kind == "rotated":
                    reply = f"Hermes session rotated: {exc}"
                    result_status = COMPLETION_STATUS_INCOMPLETE
                else:
                    reply = f"Hermes state DB {exc.kind}: {exc}"
                    result_status = COMPLETION_STATUS_FAILED
                break
            if exchange is not None:
                anchor_seen = True
                db_session_id = exchange.session_id
                db_snapshot = exchange
                if anchor_ms is None:
                    anchor_ms = int((now - started) * 1000)
                fingerprint = exchange.fingerprint
                if fingerprint != last_fingerprint:
                    last_fingerprint = fingerprint
                    last_change_at = now
                    task.progress.update({"phase": "hermes_transcript", "last_change_at": now, "message_rows": len(exchange.rows)})
                recovered = exchange.reply(task.req_id)
                if recovered is not None:
                    reply = recovered
                    result_status = COMPLETION_STATUS_COMPLETED
                    exit_code = 0
                    done_ms = int((now - started) * 1000)
                    break
                if exchange.end_reason == "compression":
                    reply = "Hermes session rotated before the request completed."
                    result_status = COMPLETION_STATUS_INCOMPLETE
                    break
                if exchange.ended_at:
                    reply = "Hermes session closed before the request completed."
                    result_status = COMPLETION_STATUS_INCOMPLETE
                    break
                if req.delivery_only:
                    reply = "Hermes request delivered."
                    result_status = COMPLETION_STATUS_COMPLETED
                    exit_code = 0
                    break
                if idle_timeout is not None and now - last_change_at >= idle_timeout:
                    break
            if deadline is not None and now >= deadline:
                break
            time.sleep(0.25)

        result = ProviderResult(
            exit_code=exit_code, reply=reply, req_id=task.req_id, session_key=session_key,
            done_seen=result_status == COMPLETION_STATUS_COMPLETED and db_snapshot is not None and db_snapshot.reply(task.req_id) is not None,
            done_ms=done_ms, anchor_seen=anchor_seen, anchor_ms=anchor_ms,
            log_path=str(db_path), status=result_status,
            extra={"destination_database_path": str(db_path), "destination_conversation_id": db_session_id},
        )
        return self._finish(task, result, database_path=str(db_path) if db_session_id else "", conversation_id=db_session_id)

    def _finish(
        self,
        task: QueuedTask,
        result: ProviderResult,
        *,
        database_path: str = "",
        conversation_id: str = "",
    ) -> ProviderResult:
        req = task.request
        persist_outcome = PersistOutcome.NO_RECEIPT
        try:
            persist_outcome = persist_proven_result(
                task.req_id, reply=result.reply, status=result.status,
                database_path=database_path, conversation_id=conversation_id,
            )
        except Exception:
            persist_outcome = PersistOutcome.FAILED
        if req.suppress_completion_hook or persist_outcome == PersistOutcome.FAILED:
            return result
        notify_completion(
            provider="hermes", output_file=req.output_path, reply=result.reply, req_id=task.req_id,
            done_seen=result.done_seen, status=result.status, caller=req.caller,
            email_req_id=req.email_req_id, email_msg_id=req.email_msg_id, email_from=req.email_from,
            work_dir=req.caller_work_dir or req.work_dir,
            caller_pane_id=req.caller_pane_id or (req.route.caller_pane_id if req.route.present else ""),
            caller_terminal=req.caller_terminal or (req.route.caller_terminal if req.route.present else ""),
            caller_live_id=req.route.caller_live_id if req.route.present else "",
            route_launch_id=req.route.launch_id if req.route.present else "",
        )
        return result
