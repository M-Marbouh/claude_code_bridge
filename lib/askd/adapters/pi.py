"""Unified askd adapter for a Pi coding-agent pane."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from askd.adapters.base import BaseProviderAdapter, ProviderRequest, ProviderResult, QueuedTask, route_session_key
from askd.unsent import PI_UNSENT_REPLY
from askd.completion_delivery import persist_and_notify
from askd_timeout import codex_idle_timeout_s, codex_max_wait_s, positive_timeout_s
from askd_runtime import log_path, write_log
from ccb_protocol import strip_done_text, wrap_codex_prompt
from completion_hook import (
    COMPLETION_STATUS_CANCELLED,
    COMPLETION_STATUS_COMPLETED,
    COMPLETION_STATUS_FAILED,
    COMPLETION_STATUS_INCOMPLETE,
)
from pane_registry import validate_route
from pi_comm import (
    PiExchange,
    PiGatePolicy,
    PiSessionError,
    PiState,
    confirm_pi_ready,
    load_candidate_sessions,
    load_session_path,
    read_pi_state,
    wait_until_pi_injectable,
)
from project_id import compute_ccb_project_id
from providers import PI_SPEC
from terminal import get_backend_for_session

PARK_TIMEOUT_S = 60.0
ANCHOR_TIMEOUT_S = 12.0
# Pi writes its retry context_edit ~1 ms after a failed row; a session read can land in between.
ERROR_GRACE_S = 10.0
# A retry whose pane stays confirmed idle this long has stopped without writing a reply.
RETRY_IDLE_S = 30.0
# The pre-send readiness policy lives in pi_comm, where EVERY CCB send into a Pi pane reads it.
# These module-level aliases are the adapter's own knobs (its tests tune them), reseated from
# one source so the adapter, autonew and the peer transports can never drift apart:
# a non-idle pre-send reading is re-checked after PRE_SEND_RETRY_WAIT_S, PRE_SEND_RETRY_LIMIT
# times; a busy pane is waited for without spending that budget, logged at BUSY_LOG_INTERVAL_S.
from pi_comm import (  # noqa: E402  (imports below the module docstring and stdlib block)
    BUSY_LOG_INTERVAL_S,
    PRE_SEND_RETRY_LIMIT,
    PRE_SEND_RETRY_WAIT_S,
)
# A request stopped before injection was never delivered; its reply says so plainly.
#: The agreed sentence for a request CCB never injected. Shared with the unsent
#: finalizer so both paths cannot drift apart.
PRE_SEND_NOT_SENT_REPLY = PI_UNSENT_REPLY
# Cancellation is polled in slices this short while waiting to retry.
PRE_SEND_WAIT_POLL_S = 0.25
# Progress mode never sleeps longer than this, so a growing transcript and a
# change of pane state are both noticed promptly.
PROGRESS_POLL_S = 0.25


def _write_log(line: str) -> None:
    write_log(log_path(PI_SPEC.log_file_name), line)


class _PreSendStop(Exception):
    """Ends the readiness wait with a result the per-pass resolver already built.

    The destination, pane and anchor checks decide their own outcome (a failed
    request, or an ambiguous anchor) and must report it verbatim; raising keeps
    that verdict out of the shared gate's readiness vocabulary, which only knows
    ready / refused / cancelled / expired.
    """

    def __init__(self, result: ProviderResult):
        super().__init__(str(getattr(result, "reply", "")))
        self.result = result


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


# The state reader lives in pi_comm so autonew's Pi gate and this adapter share one implementation:
# Pi's own extension reports readiness, and no part of CCB reads the terminal screen for it.
_read_pi_state = read_pi_state
_confirm_pi_ready = confirm_pi_ready


def _wait_before_retry(task: QueuedTask, seconds: float) -> bool:
    """Wait before re-running the pre-send checks; False when the task was cancelled."""
    if task.cancel_event and task.cancel_event.is_set():
        return False
    remaining = seconds
    while remaining > 0:
        time.sleep(min(PRE_SEND_WAIT_POLL_S, remaining))
        remaining -= PRE_SEND_WAIT_POLL_S
        if task.cancel_event and task.cancel_event.is_set():
            return False
    return True


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


def _read_bound_exchange(
    session_dir: Path,
    work_dir: Path,
    req_id: str,
    *,
    bound_transcript: Path | None,
    bound_session_id: str,
    start_dir: str | Path | None,
    cache: dict,
) -> PiExchange | None:
    """The request's exchange read from the ANCHORED transcript alone (B5).

    Returns None when nothing is usable -- no proven transcript, an unreadable
    one, or a session error -- so the caller keeps its "expired" verdict rather
    than guessing. This is the drain the adapter performs once, at expiry, so a
    CCB_DONE written in the final poll gap is not lost.
    """
    if bound_transcript is None or not bound_session_id:
        return None
    try:
        session = load_session_path(bound_transcript, bound_session_id)
    except (PiSessionError, OSError):
        return None
    return session.exchange(req_id)


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
        # B2: a supplied idle budget is REQUIRED, so an older client that never
        # sent one keeps today's wall clock. The daemon already enforces this;
        # the adapter does not trust its input for a decision this expensive.
        progress_mode = (
            not req.timeout_explicit
            and not req.delivery_only
            and req.idle_timeout_s is not None
        )
        # F2 (B3): in progress mode the turn's budget is counted from the CLAIM
        # and SHARED by the pre-send readiness wait and the turn itself. A long
        # queue wait therefore cannot consume it, and a short cap cannot be
        # exhausted before injection. `deadline` remains the nominal wall clock
        # used by every non-progress path (explicit --timeout / legacy client).
        worker_cap_deadline: float | None = None
        if progress_mode:
            # The DAEMON announces this task's turn cap before it can be claimed
            # (the same number its own wait uses). Prefer it over re-deriving
            # one from the request, so the adapter and the daemon can never
            # disagree about how long the turn may run.
            announced_cap = getattr(task, "worker_cap_s", None)
            cap_s = positive_timeout_s(
                announced_cap if announced_cap is not None else req.max_wait_s,
                codex_max_wait_s(),
            )
            claimed_at = getattr(task, "claimed_at", None)
            worker_cap_deadline = (claimed_at if claimed_at is not None else started) + cap_s
            # ITEM 3: in progress mode the worker deadline governs the WHOLE
            # phase throughout -- pre-send included. Widening it to the nominal
            # timeout (as an earlier version did) let a short cap expire while
            # the pane was still busy, and the request was then injected
            # anyway. Non-progress paths keep their own wall clock below.
            deadline = worker_cap_deadline
        else:
            deadline = None if float(req.timeout_s) < 0 else started + float(req.timeout_s)
        progress_idle_s = (
            positive_timeout_s(req.idle_timeout_s, codex_idle_timeout_s())
            if progress_mode
            else None
        )
        progress_cap_deadline = worker_cap_deadline

        # Stage 1 of the shared Pi injection gate (pi_comm.wait_until_pi_injectable):
        # `resolve_binding` proves THIS pass's destination -- the session binding, the pane identity,
        # and the absence of an existing anchor for this request -- and the gate decides whether the
        # pane may be typed into. The per-pass resolution is why /new or a relaunch during a long
        # busy wait is judged fresh instead of against a stale binding.
        attempt: dict[str, Any] = {}

        def resolve_binding() -> dict[str, Any]:
            """Prove this pass's destination, or end the request with that pass's own result."""
            _session_path, session, error = _load_destination(req)
            session_key = route_session_key(self.key, req.route) if req.route.present else self.compute_session_key(session)
            attempt["session_key"] = session_key
            if error or session is None:
                raise _PreSendStop(self._finish(task, ProviderResult(
                    exit_code=1, reply=error or "Pi session unavailable.", req_id=task.req_id,
                    session_key=session_key, done_seen=False, status=COMPLETION_STATUS_FAILED,
                )))
            backend, pane_error = _check_pane(session, work_dir)
            if pane_error or backend is None:
                raise _PreSendStop(self._finish(task, ProviderResult(
                    exit_code=1, reply=pane_error, req_id=task.req_id, session_key=session_key,
                    done_seen=False, status=COMPLETION_STATUS_FAILED,
                )))
            # A cancelled or expired request is never injected, and this guard runs BEFORE any
            # transcript or anchor work -- exactly where it sat before the shared gate took over
            # the readiness loop. It is not merely cosmetic: reading candidates can take real time,
            # and a request whose caller stopped waiting must not be injected afterwards.
            stop = self._pre_send_stop(task, session_key, deadline, unsent=progress_mode)
            if stop is not None:
                raise _PreSendStop(self._finish(task, stop))

            session_dir = Path(session["pi_session_dir"]).expanduser()
            bound_session_id = str(session["pi_session_id"])
            session_cache: dict = {}
            try:
                candidates = load_candidate_sessions(
                    session_dir, work_dir, bound_session_id=bound_session_id,
                    start_dir=session.get("start_dir"), cache=session_cache,
                )
            except PiSessionError as exc:
                raise _PreSendStop(self._finish(task, ProviderResult(
                    exit_code=1, reply=f"Pi session {exc.kind}: {exc}", req_id=task.req_id,
                    session_key=session_key, done_seen=False, status=COMPLETION_STATUS_FAILED,
                ))) from exc
            existing_anchors = [
                candidate for candidate in candidates
                if candidate.anchors(task.req_id)
            ]
            if existing_anchors:
                ambiguous = len(existing_anchors) > 1 or any(
                    len(candidate.anchors(task.req_id)) > 1 for candidate in existing_anchors
                )
                reason = f"Pi request anchor is ambiguous for {task.req_id}." if ambiguous else f"Pi request anchor already exists for {task.req_id}."
                raise _PreSendStop(self._finish(task, ProviderResult(
                    exit_code=2 if ambiguous else 1, reply=reason,
                    req_id=task.req_id, session_key=session_key, done_seen=False,
                    status=COMPLETION_STATUS_INCOMPLETE if ambiguous else COMPLETION_STATUS_FAILED,
                )))
            attempt.update({
                "session": session,
                "backend": backend,
                "pane_id": str(session["pane_id"]),
                "session_dir": session_dir,
                "bound_session_id": bound_session_id,
                "pi_session_id": bound_session_id,
                "session_cache": session_cache,
            })
            return session

        # Phase 3 (E5) + Item 1: the identity/role-policy recheck runs INSIDE
        # the cross-process pane-injection lock, so a revoked permission or a
        # replaced destination during the lock wait is judged AFTER that wait and
        # immediately before injection. `send_text` re-enters the lock harmlessly.
        from delivery_policy import refusal_for_request
        from pane_lock import PaneLockError, pane_injection_lock

        gate_policy = PiGatePolicy(
            retry_wait_s=PRE_SEND_RETRY_WAIT_S,
            retry_limit=PRE_SEND_RETRY_LIMIT,
            busy_log_interval_s=BUSY_LOG_INTERVAL_S,
        )

        while True:
            try:
                verdict = wait_until_pi_injectable(
                    resolve_binding,
                    deadline,
                    policy=gate_policy,
                    confirm=_confirm_pi_ready,
                    cancel=lambda: bool(task.cancel_event and task.cancel_event.is_set()),
                    wait=lambda seconds: _wait_before_retry(task, seconds),
                    log=_write_log,
                    label="provider=pi",
                    req_id=task.req_id,
                )
            except _PreSendStop as stopped:
                return stopped.result
            session_key = attempt["session_key"]
            # A3: judge EXPIRY before the readiness VERDICT. The gate's confirmation polls (up to ~2 s)
            # and can itself spend the last of the budget, and a busy pane's clamped wait can end
            # exactly on the deadline; a generic readiness failure there would misreport a request
            # CCB stopped waiting for. A ready pane still wins, because an accepted read means the
            # request is deliverable.
            if verdict.outcome == "cancelled":
                return self._finish(task, self._not_sent(task, session_key))
            if verdict.outcome == "expired":
                stop = self._pre_send_stop(task, session_key, deadline, unsent=progress_mode)
                if stop is not None:
                    return self._finish(task, stop)
                return self._finish(task, ProviderResult(
                    exit_code=2, reply="Pi request timed out before a terminal CCB_DONE was observed.",
                    req_id=task.req_id, session_key=session_key,
                    done_seen=False, status=COMPLETION_STATUS_INCOMPLETE,
                ))
            if verdict.outcome != "ready":
                return self._refuse_state(task, session_key, verdict.state)

            session = attempt["session"]
            backend = attempt["backend"]
            pane_id = attempt["pane_id"]
            session_dir = attempt["session_dir"]
            bound_session_id = attempt["bound_session_id"]
            pi_session_id = attempt["pi_session_id"]
            session_cache = attempt["session_cache"]

            # The readiness re-check after a wait can itself consume the last of the budget or see a
            # cancellation; this guard is the LAST step before the policy gate, so nothing is sent
            # for a stopped request.
            stop = self._pre_send_stop(task, session_key, deadline, unsent=progress_mode)
            if stop is not None:
                return self._finish(task, stop)

            # A1: bound the lock ACQUISITION.
            #
            # PROGRESS MODE ONLY. The wait is min(configured lock timeout, whatever
            # is left of the worker budget); when no budget is left the lock is not
            # even attempted and the request ends UNSENT. Every EXPLICIT/legacy
            # request keeps `pane_lock.default_timeout_s()` exactly as it always
            # had -- their `deadline` is a turn budget, not a lock budget, and
            # narrowing it here once made a 3 s configured lock wait look like a
            # 50 s one (and vice versa).
            from pane_lock import default_timeout_s as _lock_default_timeout_s

            if progress_mode:
                configured_lock_s = _lock_default_timeout_s()
                remaining_budget_s = (
                    None if deadline is None else deadline - time.monotonic()
                )
                if remaining_budget_s is not None and remaining_budget_s <= 0:
                    return self._finish(task, self._not_sent(task, session_key))
                lock_budget_s: float | None = (
                    configured_lock_s
                    if remaining_budget_s is None
                    else min(configured_lock_s, remaining_budget_s)
                )
            else:
                lock_budget_s = _lock_default_timeout_s()

            retry_after_lock = False
            try:
                with pane_injection_lock(backend, pane_id, lock_budget_s):
                    # F2: the lock wait itself can consume the budget (it serialises
                    # every injection into this pane). Re-check expiry HERE, after
                    # the wait and immediately before injection, so a stopped
                    # request is never sent just because the guard ran earlier.
                    stop = self._pre_send_stop(task, session_key, deadline, unsent=progress_mode)
                    if stop is not None:
                        return self._finish(task, stop)
                    refusal = refusal_for_request(
                        req, self.key, expected_pane=pane_id, session=session
                    )
                    if refusal:
                        _write_log(
                            f"[INFO] policy refused provider=pi req_id={task.req_id} reason={refusal}"
                        )
                        return self._finish(task, ProviderResult(
                            exit_code=1, reply=refusal, req_id=task.req_id, session_key=session_key,
                            done_seen=False, status=COMPLETION_STATUS_FAILED,
                        ))
                    # ITEM 3: evaluating the policy can itself take time (it reads
                    # the destination session). Re-check expiry ONCE MORE here, the
                    # last possible moment before the keystrokes go out, so a cap
                    # that ran out during the wait or the policy check means ZERO
                    # sends rather than an injected request nobody is waiting for.
                    stop = self._pre_send_stop(task, session_key, deadline, unsent=progress_mode)
                    if stop is not None:
                        return self._finish(task, stop)
                    # Stage 2 of the shared gate (A4): a FRESH readiness confirmation runs
                    # INSIDE the lock, immediately before the keystrokes. Another sender (or the
                    # user) can start Pi between the wait outside the lock and this moment; a
                    # paste then would steer that turn or merge with that draft. A not-ready
                    # verdict releases the lock and resumes waiting inside the same budget --
                    # this pane is never pasted into while it is not ready.
                    fresh = _confirm_pi_ready(session, deadline)
                    if fresh.status != "ready":
                        _write_log(
                            f"[INFO] pre-send re-confirm after lock provider=pi "
                            f"req_id={task.req_id} state={fresh.status}"
                        )
                        retry_after_lock = True
                    else:
                        # F3: the confirmation polls for up to ~2 s, so cancellation and expiry are
                        # judged ONE more time immediately before the keystrokes go out.
                        stop = self._pre_send_stop(task, session_key, deadline, unsent=progress_mode)
                        if stop is not None:
                            return self._finish(task, stop)
                        try:
                            backend.send_text(pane_id, wrap_codex_prompt(req.message, task.req_id))
                        except Exception as exc:
                            return self._finish(task, ProviderResult(
                                exit_code=1, reply=f"Failed to send to Pi pane: {exc}", req_id=task.req_id,
                                session_key=session_key, done_seen=False, status=COMPLETION_STATUS_FAILED,
                            ))
            except PaneLockError as exc:
                # No injection happened: this IS proven non-delivery. When the cap
                # expired while waiting for the lock, report the explicit unsent
                # outcome (the agreed Pi sentence) rather than a generic failure.
                _write_log(
                    f"[WARN] pane injection lock unavailable provider=pi req_id={task.req_id}: {exc}"
                )
                if progress_mode and deadline is not None and time.monotonic() >= deadline:
                    return self._finish(task, self._not_sent(task, session_key))
                return self._finish(task, ProviderResult(
                    exit_code=1, reply=f"Pane injection lock unavailable: {exc}", req_id=task.req_id,
                    session_key=session_key, done_seen=False, status=COMPLETION_STATUS_FAILED,
                ))
            if retry_after_lock:
                continue
            break

        anchor_deadline = time.monotonic() + ANCHOR_TIMEOUT_S

        parked_at: float | None = None
        errored_at: float | None = None
        errored_id = ""
        retry_idle_at: float | None = None
        anchor_seen = False
        anchor_ms: int | None = None
        saw_busy = False
        transcript_path = ""
        caller_stopped_logged = False
        result = None
        # Progress bookkeeping. `bound_transcript` is the transcript the REQUEST
        # ANCHOR was proven in: only its growth counts as activity (B5), never a
        # neighbouring session's. `last_activity_at` is refreshed by provable
        # progress only -- a growing bound transcript, or the pane reading busy.
        bound_transcript: Path | None = None
        #: The session id PROVEN at the anchor. The drain trusts this, never the
        #: header the file carries later (B5).
        bound_transcript_session_id = ""
        last_transcript_size: int | None = None
        last_activity_at: float | None = None
        while True:
            now = time.monotonic()
            # The only production setter is the daemon's wait expiry, i.e. the caller stopped
            # waiting -- not an abort of Pi. Keep tracking to this adapter's deadline and
            # report the real outcome, so an already-injected turn is never lost.
            if (
                task.cancel_event
                and task.cancel_event.is_set()
                and not caller_stopped_logged
            ):
                caller_stopped_logged = True
                _write_log(
                    f"[INFO] caller stopped waiting; still tracking provider=pi req_id={task.req_id}"
                )
            if not backend.is_alive(str(session["pane_id"])):
                result = ProviderResult(
                    exit_code=2, reply="Pi pane died during the request.", req_id=task.req_id,
                    session_key=session_key, done_seen=False, anchor_seen=anchor_seen,
                    anchor_ms=anchor_ms, status=COMPLETION_STATUS_INCOMPLETE,
                )
                break
            pane_state = _read_pi_state(session)
            saw_busy = saw_busy or pane_state.status == "busy"
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
                if anchor_seen:
                    # B5: bind the transcript ONCE, at the anchor, and let only
                    # that file prove activity from then on.
                    if bound_transcript is None:
                        source = found if found is not None else bound_session
                        if source is not None:
                            bound_transcript = Path(source.path)
                            bound_transcript_session_id = str(source.session_id)
                            try:
                                last_transcript_size = bound_transcript.stat().st_size
                            except OSError:
                                last_transcript_size = None
                            last_activity_at = now
                    elif bound_transcript is not None:
                        try:
                            grown = bound_transcript.stat().st_size
                        except OSError:
                            grown = last_transcript_size
                        if grown is not None and (
                            last_transcript_size is None or grown > last_transcript_size
                        ):
                            last_transcript_size = grown
                            last_activity_at = now
                    # B1: a BUSY pane is activity -- Pi is visibly working even
                    # while its transcript is momentarily static. `prompt` (an
                    # open dialog) is classified BEFORE busy by pi_comm and is
                    # deliberately NOT activity: a stuck dialog must not keep a
                    # request alive for ever.
                    if pane_state.status == "busy":
                        last_activity_at = now
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
                    active_deadline = (
                        progress_cap_deadline if (progress_mode and anchor_seen) else deadline
                    )
                    if _confirm_pi_ready(session, active_deadline).status == "ready":
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
                deadline_state = pane_state.status
                if deadline_state == "ready":
                    deadline_state = _confirm_pi_ready(session, deadline).status
                if deadline_state in {"ready", "prompt"} and _anchor_landed(
                    session_dir, work_dir, task.req_id, bound_session_id=bound_session_id,
                    start_dir=session.get("start_dir"), cache=session_cache,
                ):
                    # The request reached Pi's transcript during confirmation; let the
                    # next poll track it instead of reporting a lost request.
                    continue
                if deadline_state == "ready":
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
                if deadline_state == "prompt":
                    result = ProviderResult(
                        exit_code=1, reply="Pi did not accept the request (Pi is showing a dialog).",
                        req_id=task.req_id, session_key=session_key, done_seen=False,
                        anchor_seen=False, status=COMPLETION_STATUS_FAILED,
                        log_path=transcript_path or None,
                    )
                    break
                if deadline_state == "busy":
                    saw_busy = True
            live_progress = progress_mode and anchor_seen
            if live_progress:
                # The absolute cap applies even under CONTINUOUS busy (B1): a
                # hung tool can publish fresh busy snapshots for ever, so no
                # amount of "activity" may outlive it.
                expired_reason = ""
                if progress_cap_deadline is not None and now >= progress_cap_deadline:
                    expired_reason = "cap"
                elif (
                    progress_idle_s is not None
                    and last_activity_at is not None
                    and now - last_activity_at >= progress_idle_s
                ):
                    expired_reason = "idle"
                if expired_reason:
                    # B5: drain the PROVEN transcript once before giving up. A
                    # CCB_DONE written in the last poll gap must still win.
                    size_before = last_transcript_size
                    drain = _read_bound_exchange(
                        session_dir, work_dir, task.req_id,
                        bound_transcript=bound_transcript,
                        bound_session_id=bound_transcript_session_id,
                        start_dir=session.get("start_dir"),
                        cache=session_cache,
                    )
                    if drain is not None and drain.state == "completed":
                        result = ProviderResult(
                            exit_code=0, reply=strip_done_text(drain.reply, task.req_id),
                            req_id=task.req_id, session_key=session_key, done_seen=True,
                            anchor_seen=True, anchor_ms=anchor_ms,
                            done_ms=int((now - started) * 1000), log_path=transcript_path,
                            status=COMPLETION_STATUS_COMPLETED,
                            extra={"destination_transcript_path": transcript_path, "destination_conversation_id": pi_session_id},
                        )
                        break
                    # The CAP always ends the turn (B1): a hung tool keeps
                    # publishing fresh busy snapshots, so no amount of activity
                    # may outlive it.
                    if expired_reason == "cap":
                        result = ProviderResult(
                            exit_code=2,
                            reply="Pi request reached its maximum wait without a terminal CCB_DONE.",
                            req_id=task.req_id, session_key=session_key, done_seen=False,
                            anchor_seen=anchor_seen, anchor_ms=anchor_ms, log_path=transcript_path,
                            status=COMPLETION_STATUS_INCOMPLETE,
                            extra={"destination_transcript_path": transcript_path, "destination_conversation_id": pi_session_id},
                        )
                        break
                    # IDLE: only REAL transcript growth during the drain grants
                    # another round. A busy pane does not, or a stuck tool would
                    # hold the request open for ever.
                    grew = False
                    if bound_transcript is not None:
                        try:
                            current_size = bound_transcript.stat().st_size
                        except OSError:
                            current_size = size_before
                        grew = (
                            current_size is not None
                            and size_before is not None
                            and current_size > size_before
                        )
                        if grew:
                            last_transcript_size = current_size
                            last_activity_at = now
                    if grew:
                        continue
                    result = ProviderResult(
                        exit_code=2, reply="Pi request timed out before a terminal CCB_DONE was observed.",
                        req_id=task.req_id, session_key=session_key, done_seen=False,
                        anchor_seen=anchor_seen, anchor_ms=anchor_ms, log_path=transcript_path,
                        status=COMPLETION_STATUS_INCOMPLETE,
                        extra={"destination_transcript_path": transcript_path, "destination_conversation_id": pi_session_id},
                    )
                    break
            elif deadline is not None and now >= deadline:
                result = ProviderResult(
                    exit_code=2, reply="Pi request timed out before a terminal CCB_DONE was observed.",
                    req_id=task.req_id, session_key=session_key, done_seen=False,
                    anchor_seen=anchor_seen, anchor_ms=anchor_ms, log_path=transcript_path,
                    status=COMPLETION_STATUS_INCOMPLETE,
                    extra={"destination_transcript_path": transcript_path, "destination_conversation_id": pi_session_id},
                )
                break
            time.sleep(PROGRESS_POLL_S)
        return self._finish(task, result, transcript_path=transcript_path, conversation_id=pi_session_id)

    def _not_sent(self, task: QueuedTask, session_key: str) -> ProviderResult:
        """The terminal result for a request stopped before injection: never delivered."""
        return ProviderResult(
            exit_code=1, reply=PRE_SEND_NOT_SENT_REPLY, req_id=task.req_id, session_key=session_key,
            done_seen=False, status=COMPLETION_STATUS_CANCELLED,
        )

    def _pre_send_stop(
        self,
        task: QueuedTask,
        session_key: str,
        deadline: float | None,
        *,
        unsent: bool = False,
    ) -> ProviderResult | None:
        """Cancelled or expired before injection: the terminal result, else None.

        `unsent` selects the wording for an expiry that happened while CCB was
        still trying to inject (progress mode): NOTHING was sent, so "timed out
        before a terminal CCB_DONE" would be a lie about a request Pi never saw.
        The agreed sentence ("Pi request was not sent: CCB stopped waiting before
        Pi was free.") is used instead.
        """
        if task.cancel_event and task.cancel_event.is_set():
            return self._not_sent(task, session_key)
        if deadline is not None and time.monotonic() >= deadline:
            if unsent:
                return self._not_sent(task, session_key)
            return ProviderResult(
                exit_code=2, reply="Pi request timed out before a terminal CCB_DONE was observed.",
                req_id=task.req_id, session_key=session_key, done_seen=False,
                status=COMPLETION_STATUS_INCOMPLETE,
            )
        return None

    def _refuse_state(
        self, task: QueuedTask, session_key: str, state: PiState,
    ) -> ProviderResult:
        """Refuse a pane Pi does not report ready, naming its state and why."""
        if state.status == "prompt":
            lead = "Pi is waiting for a decision in its pane; refusing to send into the prompt."
        elif state.status == "busy":
            lead = "Pi pane is busy; refusing to send to avoid steering an active turn."
        elif state.status == "draft":
            lead = "Pi has unsent text in its editor; refusing to send to avoid concatenating with it."
        else:
            lead = "Pi pane readiness could not be verified; refusing to send."
        return self._finish(task, ProviderResult(
            exit_code=1, reply=f"{lead} (state: {state.status}; {state.reason})", req_id=task.req_id,
            session_key=session_key, done_seen=False, status=COMPLETION_STATUS_FAILED,
        ))

    def _finish(self, task: QueuedTask, result: ProviderResult, *, transcript_path: str = "", conversation_id: str = "") -> ProviderResult:
        persist_and_notify(
            "pi", task.request, req_id=task.req_id, reply=result.reply, status=result.status,
            done_seen=result.done_seen, transcript_path=transcript_path, conversation_id=conversation_id,
        )
        return result
