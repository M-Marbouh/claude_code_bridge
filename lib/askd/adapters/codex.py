"""
Codex provider adapter for the unified ask daemon.

Wraps existing caskd_* modules to provide a consistent interface.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from askd.adapters.base import BaseProviderAdapter, ProviderRequest, ProviderResult, QueuedTask
from askd_runtime import log_path, write_log
from ccb_protocol import REQ_ID_PREFIX, is_done_text, strip_done_text, extract_reply_for_req, wrap_codex_prompt
from caskd_session import CodexProjectSession, compute_session_key, load_project_session
from codex_comm import CodexCommunicator, CodexLogReader, CodexTurnContext, read_latest_turn_context
from completion_hook import (
    COMPLETION_STATUS_CANCELLED,
    COMPLETION_STATUS_COMPLETED,
    COMPLETION_STATUS_FAILED,
    COMPLETION_STATUS_INCOMPLETE,
    default_reply_for_status,
    notify_completion,
)
from project_id import normalize_work_dir
from providers import CASKD_SPEC
from terminal import get_backend_for_session, is_windows


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write_log(line: str) -> None:
    write_log(log_path(CASKD_SPEC.log_file_name), line)


def _tail_state_for_log(log_path_val: Optional[Path], *, tail_bytes: int) -> dict:
    if not log_path_val:
        return {"log_path": None, "offset": 0}
    try:
        size = log_path_val.stat().st_size
    except OSError:
        size = 0
    offset = max(0, int(size) - int(tail_bytes))
    return {"log_path": log_path_val, "offset": offset}


def _assemble_reply(final_chunks: list[str], combined: str, req_id: str) -> str:
    """
    Assemble the reply Codex returns to the caller.

    Modern Codex logs tag the final report with phase=="final_answer"; interim
    progress is phase=="commentary" and the duplicate event_msg twin is
    phase=="event". When we captured any final_answer content, return only that
    (stripped of the trailing CCB_DONE marker). Otherwise fall back to the legacy
    behavior of extracting the full anchor->DONE span -- this preserves replies
    from older Codex builds that emit no phase metadata.
    """
    if final_chunks:
        return strip_done_text("\n".join(final_chunks), req_id)
    return extract_reply_for_req(combined, req_id)


def _show_tier_footer() -> bool:
    return (os.environ.get("CCB_CODEX_SHOW_TIER") or "").strip().lower() in {"1", "true", "yes", "on"}


def _format_tier_footer(provider_key: str, ctx: CodexTurnContext | None) -> str:
    model = ctx.model if ctx and ctx.model else "unknown"
    effort = ctx.effort if ctx and ctx.effort else "unknown"
    sandbox = ctx.sandbox if ctx and ctx.sandbox else "unknown"
    return f"[{provider_key} model={model} effort={effort} sandbox={sandbox}]"


def _append_tier_footer(reply: str, footer: str) -> str:
    base = reply.rstrip("\n")
    if not base:
        return footer
    return f"{base}\n{footer}"


def _log_contains_req_anchor(log_path: Path, req_id: str, *, tail_bytes: int = 2 * 1024 * 1024) -> bool:
    marker = f"{REQ_ID_PREFIX} {req_id}"
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes), os.SEEK_SET)
            text = handle.read(tail_bytes).decode("utf-8", errors="ignore")
            return marker in text
    except Exception:
        return False


def _codex_log_session_id(log_path: Path) -> Optional[str]:
    try:
        return CodexCommunicator._extract_session_id(log_path)
    except Exception:
        return None


def _codex_log_work_dir_matches(log_path: Path, work_dir: Path) -> bool:
    try:
        expected = normalize_work_dir(work_dir)
    except Exception:
        expected = str(work_dir)
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for _ in range(50):
                line = handle.readline()
                if not line:
                    break
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "session_meta":
                    continue
                payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
                cwd = payload.get("cwd")
                if not isinstance(cwd, str) or not cwd.strip():
                    return False
                try:
                    return normalize_work_dir(cwd) == expected
                except Exception:
                    return cwd == str(work_dir)
    except Exception:
        return False
    return False


def _scan_latest_candidate_log(
    work_dir: Path,
    *,
    exclude_session_ids: set[str] | None = None,
    req_id: str | None = None,
) -> Optional[Path]:
    root = Path(os.environ.get("CODEX_SESSION_ROOT") or (Path.home() / ".codex" / "sessions")).expanduser()
    if not root.exists():
        return None
    excluded = {str(s or "").strip() for s in (exclude_session_ids or set()) if str(s or "").strip()}
    try:
        logs = sorted(
            (p for p in root.glob("**/*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return None
    for candidate in logs[:400]:
        sid = _codex_log_session_id(candidate)
        if sid and sid in excluded:
            continue
        if not _codex_log_work_dir_matches(candidate, work_dir):
            continue
        # Never switch logs based only on cwd: the exact request anchor proves
        # that this Codex session received the task.
        if req_id and not _log_contains_req_anchor(candidate, req_id):
            continue
        return candidate
    return None


def _is_log_stale(preferred: Optional[Path], latest: Optional[Path], threshold_s: float) -> bool:
    if not latest:
        return False
    if not preferred or not preferred.exists():
        return True
    if threshold_s <= 0:
        return False
    try:
        preferred_mtime = preferred.stat().st_mtime
        latest_mtime = latest.stat().st_mtime
    except OSError:
        return True
    return latest_mtime - preferred_mtime >= threshold_s


class CodexAdapter(BaseProviderAdapter):
    """Adapter for Codex (WezTerm) provider."""

    @property
    def key(self) -> str:
        return "codex"

    @property
    def spec(self):
        return CASKD_SPEC

    @property
    def session_filename(self) -> str:
        return ".codex-session"

    def load_session(self, work_dir: Path) -> Optional[CodexProjectSession]:
        return load_project_session(work_dir)

    def compute_session_key(self, session: Any) -> str:
        return compute_session_key(session) if session else "codex:unknown"

    def handle_task(self, task: QueuedTask) -> ProviderResult:
        started_ms = _now_ms()
        started_at = time.time()
        req = task.request
        work_dir = Path(req.work_dir)
        _write_log(f"[INFO] start provider=codex req_id={task.req_id} work_dir={req.work_dir} caller={req.caller}")

        session = load_project_session(work_dir)
        session_key = self.compute_session_key(session)

        if not session:
            return ProviderResult(
                exit_code=1,
                reply="No active Codex session found for work_dir.",
                req_id=task.req_id,
                session_key=session_key,
                done_seen=False,
                status=COMPLETION_STATUS_FAILED,
            )

        ok, pane_or_err = session.ensure_pane()
        if not ok:
            return ProviderResult(
                exit_code=1,
                reply=f"Session pane not available: {pane_or_err}",
                req_id=task.req_id,
                session_key=session_key,
                done_seen=False,
                status=COMPLETION_STATUS_FAILED,
            )
        pane_id = pane_or_err

        backend = get_backend_for_session(session.data)
        if not backend:
            return ProviderResult(
                exit_code=1,
                reply="Terminal backend not available",
                req_id=task.req_id,
                session_key=session_key,
                done_seen=False,
                status=COMPLETION_STATUS_FAILED,
            )

        prompt = wrap_codex_prompt(req.message, task.req_id)
        preferred_log = session.codex_session_path or None
        codex_session_id = session.codex_session_id or None
        require_anchor_before_collect = True
        reader = CodexLogReader(
            log_path=preferred_log,
            session_id_filter=codex_session_id,
            work_dir=Path(session.work_dir),
            allow_stale_switch=False,
        )
        state = reader.capture_state()
        backend.send_text(pane_id, prompt)

        deadline = None if float(req.timeout_s) < 0.0 else (time.time() + float(req.timeout_s))
        chunks: list[str] = []
        final_chunks: list[str] = []
        anchor_seen = False
        done_seen = False
        anchor_ms: Optional[int] = None
        done_ms: Optional[int] = None
        fallback_scan = False

        # Idle timeout detection for degraded completion
        idle_timeout = float(os.environ.get("CCB_CASKD_IDLE_TIMEOUT", "8.0"))
        _last_reply_snapshot = ""
        _last_reply_changed_at = time.time()

        anchor_collect_grace = min(deadline, time.time() + 2.0) if deadline else (time.time() + 2.0)
        last_pane_check = time.time()
        default_interval = "5.0" if is_windows() else "2.0"
        pane_check_interval = float(os.environ.get("CCB_CASKD_PANE_CHECK_INTERVAL", default_interval))
        stale_grace_s = float(os.environ.get("CCB_CASKD_STALE_LOG_GRACE_SECONDS", "2.5"))
        stale_check_interval = float(os.environ.get("CCB_CASKD_STALE_LOG_CHECK_INTERVAL", "1.0"))
        stale_threshold_s = float(os.environ.get("CCB_CODEX_STALE_LOG_SECONDS", "10.0"))
        last_stale_check = time.time()

        while True:
            # Check for cancellation
            if task.cancel_event and task.cancel_event.is_set():
                _write_log(f"[INFO] Task cancelled during wait loop: req_id={task.req_id}")
                break

            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                wait_step = min(remaining, 0.5)
            else:
                wait_step = 0.5

            if time.time() - last_pane_check >= pane_check_interval:
                try:
                    alive = bool(backend.is_alive(pane_id))
                except Exception:
                    alive = False
                if not alive:
                    _write_log(f"[ERROR] Pane {pane_id} died during request req_id={task.req_id}")
                    codex_log_path = None
                    try:
                        lp = reader.current_log_path()
                        if lp:
                            codex_log_path = str(lp)
                    except Exception:
                        pass
                    return ProviderResult(
                        exit_code=1,
                        reply="Codex pane died during request",
                        req_id=task.req_id,
                        session_key=session_key,
                        done_seen=False,
                        anchor_seen=anchor_seen,
                        fallback_scan=fallback_scan,
                        anchor_ms=anchor_ms,
                        log_path=codex_log_path,
                        status=COMPLETION_STATUS_FAILED,
                    )
                last_pane_check = time.time()

            event, state = reader.wait_for_event(state, wait_step)

            if event is None:
                # Stale log detection: if no anchor and no chunks yet,
                # check whether a newer session log appeared (e.g. after pane restart).
                if (not anchor_seen) and (not chunks):
                    now = time.time()
                    if now - started_at >= stale_grace_s and now - last_stale_check >= stale_check_interval:
                        last_stale_check = now
                        latest_log = _scan_latest_candidate_log(
                            Path(session.work_dir),
                            req_id=task.req_id,
                        )
                        current_log = state.get("log_path")
                        if isinstance(current_log, str):
                            current_log = Path(current_log)
                        if latest_log and latest_log != current_log and _is_log_stale(current_log, latest_log, stale_threshold_s):
                            reader = CodexLogReader(
                                log_path=latest_log,
                                session_id_filter=None,
                                work_dir=Path(session.work_dir),
                                allow_stale_switch=False,
                            )
                            state = reader.capture_state()
                            fallback_scan = True
                            try:
                                new_session_id = CodexCommunicator._extract_session_id(latest_log)
                            except Exception:
                                new_session_id = None
                            try:
                                session.update_codex_log_binding(
                                    log_path=str(latest_log),
                                    session_id=new_session_id,
                                )
                            except Exception:
                                pass
                            preferred_log = str(latest_log)
                            codex_session_id = new_session_id or None
                            _write_log(f"[WARN] stale codex log detected; switching to {latest_log}")
                continue

            role, text, phase = event
            if role == "user":
                if f"{REQ_ID_PREFIX} {task.req_id}" in text:
                    anchor_seen = True
                    if anchor_ms is None:
                        anchor_ms = _now_ms() - started_ms
                continue

            if role != "assistant":
                continue

            # Never collect assistant text before this request's anchor.
            if (not anchor_seen) and (require_anchor_before_collect or time.time() < anchor_collect_grace):
                continue

            chunks.append(text)
            combined = "\n".join(chunks)
            done_now = is_done_text(combined, task.req_id)
            # A message belongs to the final report if Codex tagged it
            # phase=="final_answer" OR it carries the terminating CCB_DONE line.
            # The event_msg/agent_message twin of the final answer is logged
            # *before* the canonical phase=="final_answer" record and already
            # carries CCB_DONE, so the loop breaks on the twin first. We must
            # therefore treat the DONE-bearing message as final too; otherwise
            # final_chunks stays empty and we fall back to the noisy full span.
            # Interim commentary and its event twin (no DONE, not final_answer)
            # still feed `chunks` for completion/idle detection + legacy fallback.
            if (phase == "final_answer" or done_now) and (not final_chunks or final_chunks[-1] != text):
                final_chunks.append(text)
            if done_now:
                done_seen = True
                done_ms = _now_ms() - started_ms
                break

            # Idle-timeout: detect when Codex finished but forgot CCB_DONE
            if combined != _last_reply_snapshot:
                _last_reply_snapshot = combined
                _last_reply_changed_at = time.time()
            elif combined and (time.time() - _last_reply_changed_at >= idle_timeout):
                _write_log(
                    f"[WARN] Codex reply idle for {idle_timeout}s without CCB_DONE, "
                    f"accepting as complete req_id={task.req_id}"
                )
                done_seen = True
                done_ms = _now_ms() - started_ms
                break

        combined = "\n".join(chunks)
        reply = _assemble_reply(final_chunks, combined, task.req_id)
        status = COMPLETION_STATUS_COMPLETED if done_seen else COMPLETION_STATUS_INCOMPLETE
        if task.cancelled:
            status = COMPLETION_STATUS_CANCELLED

        codex_log_path = None
        try:
            lp = state.get("log_path")
            if lp:
                codex_log_path = str(lp)
        except Exception:
            pass

        if anchor_seen and done_seen and codex_log_path:
            try:
                confirmed_path = Path(codex_log_path)
                confirmed_sid = _codex_log_session_id(confirmed_path)
                session.update_codex_log_binding(
                    log_path=str(confirmed_path),
                    session_id=confirmed_sid,
                )
                codex_session_id = confirmed_sid or codex_session_id
            except Exception:
                pass

        if req.show_tier or _show_tier_footer():
            provider_key = "codex"
            ctx = read_latest_turn_context(
                codex_log_path,
                session_id_filter=codex_session_id,
            )
            reply = _append_tier_footer(reply, _format_tier_footer(provider_key, ctx))

        result = ProviderResult(
            exit_code=0 if done_seen else 2,
            reply=reply,
            req_id=task.req_id,
            session_key=session_key,
            done_seen=done_seen,
            done_ms=done_ms,
            anchor_seen=anchor_seen,
            anchor_ms=anchor_ms,
            fallback_scan=fallback_scan,
            log_path=codex_log_path,
            status=status,
        )
        _write_log(
            f"[INFO] done provider=codex req_id={task.req_id} exit={result.exit_code} "
            f"anchor={result.anchor_seen} done={result.done_seen}"
        )

        reply_for_hook = reply
        if not reply_for_hook.strip():
            reply_for_hook = default_reply_for_status(status, done_seen=done_seen)
        _write_log(f"[INFO] notify_completion caller={req.caller} status={status} done_seen={done_seen}")
        notify_completion(
            provider="codex",
            output_file=req.output_path,
            reply=reply_for_hook,
            req_id=task.req_id,
            done_seen=done_seen,
            status=status,
            caller=req.caller,
            email_req_id=req.email_req_id,
            email_msg_id=req.email_msg_id,
            email_from=req.email_from,
            work_dir=req.work_dir,
            caller_pane_id=req.caller_pane_id,
            caller_terminal=req.caller_terminal,
        )

        return result
