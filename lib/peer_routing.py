"""Peer sender identity: which EXACT live session is the sender of a
cross-project peer ask, established through the SAME authoritative
inventory reader every other consumer of `live_sessions` uses.

The case this module exists for is TWO SESSIONS INSIDE ONE LAUNCH -- a
single registry record whose own `live_sessions` inventory names two
panes of one provider (e.g. two Codex panes started under one CCB
launch) -- not two separate registry records. `pane_registry.
read_inventory_for_record` is the sole source of truth for what a record
names: a VALID inventory contributes its real entries (so both sessions
of a duplicate pair are visible, never collapsed), an ABSENT one falls
back to the legacy per-provider projection that function itself derives
for that case, and a PRESENT-BUT-INVALID one refuses outright -- it is
NEVER treated as absent and NEVER lets the legacy `providers` map sitting
beside it stand in, exactly like every other consumer of `read_inventory`
in this codebase (`pane_registry.validate_route`, `ccb_runtime_status.
resolve_project_runtime_status`).

Identifying the caller's own pane among the candidates is not enough on
its own: a provider-WIDE aggregate (`ccb_runtime_status.
resolve_project_runtime_status`) reports the whole provider "ambiguous"
the moment two live sessions exist, regardless of which one the caller
actually is -- relying on that aggregate's `mounted` flag would reject a
perfectly valid sender for the sole reason that it has a live sibling.
Once the caller's EXACT candidate is found, this module re-checks THAT
candidate's own operational availability directly
(`ccb_runtime_status._operational_refusal` -- the same per-session check
`_resolve_within_launch` runs for LOCAL routing), so a sibling's status
can neither reject a valid sender nor authorise an unavailable one.

A sandboxed caller (managed Codex client) cannot always see the real
terminal/session state on this host directly, so the public entry point
here proxies through the authenticated askd RPC exactly the way
`ccb_runtime_status.resolve_live_route` does for LOCAL routing -- never a
silent bypass that reports "nothing else registered" just because this
process can't see the answer itself.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

UNAVAILABLE = "unavailable"
AMBIGUOUS = "ambiguous"
INVALID_INVENTORY = "invalid_inventory"
# Item 2: the ONLY outcome that means "the authoritative inventory had
# nothing to say here at all" -- no pane evidence was supplied, or no
# registry record anywhere in this project names ANY session for this
# provider (legacy or otherwise). This is the SOLE error a caller may
# treat as "fall through to an older, less rigorous lookup"; every other
# error here means the inventory WAS consulted and gave a definitive
# answer, and must refuse outright rather than resume a legacy check that
# could accept anyway.
NO_CANDIDATES = "no_candidates"


class InvalidInventoryError(RuntimeError):
    """A registry record relevant to this lookup carries a PRESENT but
    BROKEN `live_sessions` inventory. Raised instead of silently
    contributing zero candidates, so a caller can never mistake "refused"
    for "nothing registered" and fall back to that record's legacy
    `providers` map.
    """


@dataclass(frozen=True)
class SenderCandidate:
    """One registered live session for a provider -- everything a later
    re-validation needs, never a cache substituted for a fresh read.
    """

    launch_id: str
    live_id: str
    provider: str
    pane_id: str
    terminal: str
    session_file: str
    ccb_project_id: str
    work_dir: str
    active: bool = True
    pane_title_marker: str = ""


@dataclass(frozen=True)
class SenderResolution:
    candidate: SenderCandidate | None = None
    error: str = ""
    detail: str = ""
    candidates: Sequence[str] = ()

    @property
    def ok(self) -> bool:
        return self.candidate is not None and not self.error


def _candidate_from_live_session(session) -> SenderCandidate:
    return SenderCandidate(
        launch_id=session.launch_id,
        live_id=session.live_id,
        provider=session.provider,
        pane_id=session.pane_id,
        terminal=session.terminal,
        session_file=session.session_file,
        ccb_project_id=session.ccb_project_id,
        work_dir=session.work_dir,
        active=session.active,
        pane_title_marker=session.pane_title_marker,
    )


def _live_entries_for_project(project_id: str, provider: str) -> tuple[list, bool]:
    """Return candidate `(registry_record, LiveSession)` pairs and inventory presence
    of `provider` across every qualifying registry record for
    `project_id`, using each record's OWN authoritative inventory.

    Raises `InvalidInventoryError` the moment ANY qualifying record's
    inventory is present-but-invalid -- this stops there rather than
    continuing to yield from other, healthy records, because a peer
    destination or sender identity resolved while ignoring a broken
    record elsewhere in the same project is exactly the kind of "refusal
    turned into a guess" this whole mechanism exists to prevent.
    """
    from ccb_runtime_status import _iter_qualifying_registry_records
    from live_sessions import INVENTORY_INVALID
    from pane_registry import read_inventory_for_record

    project_id = (project_id or "").strip()
    provider = (provider or "").strip().lower()
    entries = []
    present = False
    if not project_id or not provider:
        return entries, present
    for record, _work_dir, effective, _updated_at, _stale in _iter_qualifying_registry_records(
        project_id=project_id
    ):
        inventory = read_inventory_for_record(record)
        present = present or inventory.present
        if inventory.status == INVENTORY_INVALID:
            raise InvalidInventoryError(
                f"registry record for project {effective} has a broken live_sessions inventory"
            )
        for session in inventory.sessions:
            if session.provider == provider:
                entries.append((record, session))
    return entries, present


def identify_sender_host(
    work_dir: str | Path,
    provider: str,
    *,
    pane_id: str,
    terminal: str = "",
    check_daemon: bool = False,
) -> SenderResolution:
    """The real, host-side sender identification: filesystem (and, when
    `check_daemon` is requested, daemon) visibility must be available to
    run this. Never called directly from inside a managed Codex sandbox --
    `identify_sender` proxies through the daemon RPC for that case.

    `check_daemon` defaults to `False`: this check is about whether the
    CALLER'S OWN pane is a real, live, registered session -- not about
    whether this project's askd happens to be reachable right now, which
    is a different, already-checked concern elsewhere in the send path.
    """
    from project_id import compute_ccb_project_id

    pane_id = (pane_id or "").strip()
    try:
        project_id = compute_ccb_project_id(Path(work_dir))
    except Exception:
        return SenderResolution(error=UNAVAILABLE, detail="could not resolve a project id for this work_dir")

    terminal_norm = (terminal or "").strip().lower()
    try:
        entries, inventory_present = _live_entries_for_project(project_id, provider)
    except InvalidInventoryError as exc:
        return SenderResolution(error=INVALID_INVENTORY, detail=str(exc))

    if not inventory_present:
        return SenderResolution(error=NO_CANDIDATES, detail="legacy project without live inventory")
    if not pane_id:
        return SenderResolution(error=UNAVAILABLE, detail="no caller pane evidence")
    if not entries:
        # Nothing registered for this provider anywhere in this project's
        # registry records at all (legacy or otherwise) -- there is no
        # authoritative answer to give here either way.
        return SenderResolution(
            error=UNAVAILABLE, detail="no registered session for this provider in this project"
        )

    matches = [
        (record, session)
        for record, session in entries
        if session.pane_id == pane_id
        and (not terminal_norm or not session.terminal or session.terminal == terminal_norm)
    ]
    if not matches:
        # Item 2: candidates DID exist for this provider/project and the
        # caller's own pane is not one of them -- the inventory answered,
        # and the answer was no. This refuses; it never falls through to
        # a legacy lookup that might accept a pane the authoritative
        # source just enumerated past.
        return SenderResolution(error=UNAVAILABLE, detail="caller pane not found among registered sessions")
    if len(matches) > 1:
        return SenderResolution(
            error=AMBIGUOUS,
            detail="caller pane matched more than one registered session",
            candidates=tuple(sorted(session.launch_id for _record, session in matches)),
        )

    record, session = matches[0]
    # Item 3: the caller's EXACT candidate's own operational availability
    # -- never a sibling's, and never the provider-wide aggregate, which
    # reports the whole provider "ambiguous"/unmounted the instant two
    # live sessions exist regardless of which one is asking.
    #
    # Deliberately NOT `ccb_runtime_status._operational_refusal`: that
    # check is designed for a DESTINATION -- something a message is about
    # to be sent TO, which genuinely needs a fresh, real tmux/wezterm pane
    # probe to confirm it is reachable. A SENDER identifying ITSELF has no
    # such need: the caller is, by construction, running inside this exact
    # pane right now, and a real terminal probe of it is exactly the kind
    # of environment-dependent check that can spuriously fail (no tmux/
    # wezterm available, timing, sandboxing) without the sender being
    # invalid in any way that matters here. What DOES matter is the
    # registry's own explicit state: the launcher process that registered
    # this session must still be running, and the session must not be
    # explicitly marked inactive.
    from pane_registry import _registry_owner_alive

    if _registry_owner_alive(record) is False:
        return SenderResolution(error="launcher_dead", detail="caller's own launch is no longer running")
    if not session.active:
        return SenderResolution(error="session_inactive", detail="caller's own session is marked inactive")
    return SenderResolution(candidate=_candidate_from_live_session(session))


def _identify_sender_daemon(
    work_dir: str | Path, provider: str, *, pane_id: str, terminal: str, check_daemon: bool
) -> SenderResolution:
    """Finding-1-style proxy: ask the host daemon to run
    `identify_sender_host` for us, because a managed Codex sandbox cannot
    reliably enumerate `~/.ccb/run/*` or inspect real terminal state
    itself.
    """
    import askd_rpc
    from askd_runtime import find_running_state_file

    state_file = find_running_state_file(
        "askd.json", protocol_prefix="ask", work_dir=work_dir, timeout_s=0.5
    )
    state = askd_rpc.read_state(state_file) if state_file is not None else None
    if not state:
        raise RuntimeError("Unified askd daemon state is unavailable")
    token = str(state.get("token") or "")
    if not token:
        raise RuntimeError("Unified askd daemon state is invalid")
    request = {
        "type": "ask.request",
        "v": 1,
        "id": f"peer-sender-{os.getpid()}",
        "token": token,
        "operation": "peer_identify_sender",
        "work_dir": str(work_dir),
        "provider": provider,
        "pane_id": pane_id,
        "terminal": terminal,
        "check_daemon": check_daemon,
    }
    response = askd_rpc.request_daemon(
        state, request, connect_timeout_s=2.0, response_timeout_s=8.0
    )
    if response.get("type") != "ask.response" or int(response.get("exit_code", 1)) != 0:
        raise RuntimeError(str(response.get("reply") or "askd rejected sender identification"))
    payload = response.get("resolution")
    if not isinstance(payload, dict):
        raise RuntimeError("askd returned an invalid sender resolution")
    return _resolution_from_dict(payload)


def _resolution_from_dict(payload: dict) -> SenderResolution:
    candidate_payload = payload.get("candidate")
    candidate = None
    if isinstance(candidate_payload, dict):
        candidate = SenderCandidate(
            launch_id=str(candidate_payload.get("launch_id") or ""),
            live_id=str(candidate_payload.get("live_id") or ""),
            provider=str(candidate_payload.get("provider") or ""),
            pane_id=str(candidate_payload.get("pane_id") or ""),
            terminal=str(candidate_payload.get("terminal") or ""),
            session_file=str(candidate_payload.get("session_file") or ""),
            ccb_project_id=str(candidate_payload.get("ccb_project_id") or ""),
            work_dir=str(candidate_payload.get("work_dir") or ""),
            active=bool(candidate_payload.get("active", True)),
            pane_title_marker=str(candidate_payload.get("pane_title_marker") or ""),
        )
    raw_candidates = payload.get("candidates")
    return SenderResolution(
        candidate=candidate,
        error=str(payload.get("error") or ""),
        detail=str(payload.get("detail") or ""),
        candidates=tuple(raw_candidates) if isinstance(raw_candidates, list) else (),
    )


def resolution_to_dict(resolution: SenderResolution) -> dict:
    """Serialize a `SenderResolution` for the RPC boundary. Inverse of
    `_resolution_from_dict`."""
    candidate_dict = None
    if resolution.candidate is not None:
        c = resolution.candidate
        candidate_dict = {
            "launch_id": c.launch_id,
            "live_id": c.live_id,
            "provider": c.provider,
            "pane_id": c.pane_id,
            "terminal": c.terminal,
            "session_file": c.session_file,
            "ccb_project_id": c.ccb_project_id,
            "work_dir": c.work_dir,
            "active": c.active,
            "pane_title_marker": c.pane_title_marker,
        }
    return {
        "candidate": candidate_dict,
        "error": resolution.error,
        "detail": resolution.detail,
        "candidates": list(resolution.candidates),
    }


def identify_sender(
    work_dir: str | Path,
    provider: str,
    *,
    pane_id: str,
    terminal: str = "",
    check_daemon: bool = False,
) -> SenderResolution:
    """Which EXACT candidate the caller's own pane identifies, or an
    explicit refusal -- never a default.

    "Matches nothing", "matches more than one", "the matched candidate's
    own operational check fails", and "a relevant record's inventory is
    broken" all refuse; only a single, unique, operationally available
    match resolves. Proxies through the host daemon when running inside a
    managed Codex sandbox (see module docstring); resolves directly
    otherwise.
    """
    from ccb_runtime_status import inside_managed_codex_sandbox

    if inside_managed_codex_sandbox():
        return _identify_sender_daemon(
            work_dir, provider, pane_id=pane_id, terminal=terminal, check_daemon=check_daemon
        )
    return identify_sender_host(work_dir, provider, pane_id=pane_id, terminal=terminal, check_daemon=check_daemon)


@dataclass(frozen=True)
class DestinationResolution:
    """The outcome of re-confirming a `PeerDestination` (see
    `askd.adapters.base.PeerDestination`) is still the same live, reachable
    pane it was validated against earlier."""

    error: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def revalidate_peer_destination(destination) -> DestinationResolution:
    """Re-confirm, from a FRESH read of real terminal state, that a saved
    peer destination is STILL the exact pane it was captured from -- never
    a substitute for that read, and never a search among candidates.

    This is the peer-dispatch analogue of `pane_registry.validate_route`'s
    two checkpoints (daemon-side enqueue and immediately before the
    actual send -- see `askd.daemon._UnifiedWorkerPool.submit` and
    `askd.daemon._SessionWorker._handle_task`), applying the SAME
    pane-alive / cwd-match / marker-match criteria
    `bin/ccb-bridge-ask._validated_direct_reply_target` already uses for
    its own one-time check, so a task sitting queued between those two
    moments cannot be silently redirected by a registry or pane change
    that happens in between.
    """
    from terminal import get_backend_for_session

    if not destination.present:
        return DestinationResolution(error=UNAVAILABLE, detail="no peer destination to validate")

    backend = get_backend_for_session({"terminal": destination.terminal})
    if backend is None:
        return DestinationResolution(
            error=UNAVAILABLE, detail=f"terminal backend is unavailable: {destination.terminal}"
        )
    try:
        pane_alive = bool(backend.is_alive(destination.pane_id))
    except Exception:
        pane_alive = False
    if not pane_alive:
        return DestinationResolution(error=UNAVAILABLE, detail="destination pane is no longer alive")

    cwd_check = getattr(backend, "pane_matches_cwd_strict", None)
    if not callable(cwd_check) or not bool(cwd_check(destination.pane_id, destination.work_dir)):
        return DestinationResolution(
            error=UNAVAILABLE, detail="destination pane no longer matches its recorded project"
        )

    # WezTerm pane titles are application-controlled and routinely change
    # while Claude/Codex is working.  The registry's own mounted check uses
    # the stable pane id plus cwd for WezTerm for that reason.  Keep the
    # stronger stable-marker check for tmux, where CCB stores @ccb_marker.
    if destination.terminal != "wezterm" and not destination.pane_title_marker:
        return DestinationResolution(error=UNAVAILABLE, detail="destination has no saved pane marker")
    if destination.terminal != "wezterm" and destination.pane_title_marker:
        resolver = getattr(backend, "find_pane_by_title_marker", None)
        resolved_pane = str(resolver(destination.pane_title_marker, destination.work_dir) or "").strip() if callable(resolver) else ""
        if resolved_pane != destination.pane_id:
            return DestinationResolution(
                error=UNAVAILABLE, detail="destination pane no longer matches its recorded marker"
            )

    return DestinationResolution()


def _write_peer_log(line: str) -> None:
    """Busy-wait and retry lines for a peer delivery: the Pi provider's own log file."""
    from askd_runtime import log_path, write_log
    from providers import PI_SPEC

    write_log(log_path(PI_SPEC.log_file_name), line)


def _pi_peer_delivery_deadline(task) -> float | None:
    """The sender's OWN finite budget for one Pi peer delivery, as a monotonic deadline.

    The budget is the request's own timeout, counted from when the task was created, so queue time,
    the readiness wait and the lock wait all spend the same money -- a delivery can never outlive the
    caller's wait. An unlimited request (`timeout_s < 0`) still gets `PI_PEER_DELIVERY_MAX_S`: a Pi
    pane that never becomes injectable must not hold a peer task for ever (A3).

    A1: when the request also carries the bridge's ABSOLUTE paste-start deadline
    (`paste_deadline_s`, wall clock), the EARLIER of the two governs. The relative budget restarts
    here at task creation, so on its own it could outlive a caller whose own deadline passed while
    the request was discovered, preflighted and queued.
    """
    from pi_comm import PI_PEER_DELIVERY_MAX_S

    request = task.request
    try:
        budget_s = float(request.timeout_s)
    except (TypeError, ValueError):
        budget_s = -1.0
    if not math.isfinite(budget_s):
        # A NaN/inf budget can never expire: refuse instead of inventing one.
        return None
    if budget_s < 0:
        budget_s = PI_PEER_DELIVERY_MAX_S
    created_ms = int(getattr(task, "created_ms", 0) or 0)
    elapsed_s = max(0.0, time.time() - created_ms / 1000.0) if created_ms else 0.0
    deadline = time.monotonic() + max(0.0, budget_s - elapsed_s)
    absolute_s = getattr(request, "paste_deadline_s", None)
    if absolute_s is not None:
        # Same instant, expressed in this process's monotonic clock, then the earlier-of rule.
        absolute_deadline = time.monotonic() + max(0.0, float(absolute_s) - time.time())
        deadline = min(deadline, absolute_deadline)
    return deadline


def _send_pi_peer_message(task, session_key: str):
    """Deliver one peer message into a Pi pane: the shared gate, then the lock, then the paste.

    Pi cannot queue typed input: a paste while it works becomes a steering message mid-turn, and a
    paste into a non-empty editor merges with the draft. So this path applies, in this order:

    1. the destination project's OWN Pi binding, corroborated against the selected live session
       (pane, terminal, launch) -- no binding, or any disagreement, refuses with zero sends;
    2. the pane incarnation a reply was captured for (tmux pid, or WezTerm launch/state evidence);
    3. `wait_until_pi_injectable` OUTSIDE the lock, inside the sender's own finite budget;
    4. the lock, a FRESH `confirm_pi_ready`, the destination revalidation and the role policy --
       a not-ready verdict releases the lock and resumes waiting, and never pastes.

    Delivery confirmation stays `sent`: Pi has no transcript anchor reader, so a peer delivery is
    complete once the message reached the pane (`provider_log_reader` is untouched).
    """
    from askd.adapters.base import ProviderResult
    from ccb_protocol import wrap_pi_delivery_prompt
    from completion_hook import COMPLETION_STATUS_FAILED
    from pane_lock import PaneLockError, default_timeout_s, pane_injection_lock
    from pi_comm import (
        BUSY_LOG_INTERVAL_S,
        PRE_SEND_RETRY_LIMIT,
        PRE_SEND_RETRY_WAIT_S,
        PiGatePolicy,
        confirm_pi_ready,
        peer_pi_destination_binding,
        pi_incarnation_problem,
        wait_until_pi_injectable,
    )
    from terminal import get_backend_for_session

    request = task.request
    destination = request.peer_destination
    if not request.delivery_only or request.route.present:
        raise ValueError("peer destinations require a delivery-only Pi request")

    def cancelled() -> bool:
        return bool(task.cancelled or (task.cancel_event and task.cancel_event.is_set()))

    def failed(reply: str, status: str = COMPLETION_STATUS_FAILED):
        return ProviderResult(exit_code=1, reply=reply, req_id=task.req_id, session_key=session_key,
                              done_seen=False, status=status)

    if cancelled():
        return ProviderResult(exit_code=2, reply="Peer delivery cancelled.", req_id=task.req_id,
                              session_key=session_key, done_seen=False, status="cancelled")

    binding, error = peer_pi_destination_binding(
        destination.work_dir,
        pane_id=destination.pane_id,
        terminal=destination.terminal,
        launch_id=destination.launch_id,
    )
    if error or binding is None:
        return failed(f"Peer Pi destination unavailable ({error or 'no Pi binding'}).")
    backend = get_backend_for_session({"terminal": destination.terminal})
    if backend is None:
        return failed("Peer destination terminal backend is unavailable.")
    # The incarnation witness (F1) is required for EVERY Pi destination, in both directions: a
    # forward send's pid is captured host-side before queueing, a reply's comes from the original
    # receipt. Here it is checked once to fail early -- and again inside the lock, because a pane
    # respawned in place keeps its id, its binding and often its launch token.
    problem = pi_incarnation_problem(
        backend,
        binding,
        pane_id=destination.pane_id,
        terminal=destination.terminal,
        recorded_pane_pid=destination.pane_pid,
        recorded_launch_token=destination.launch_token,
    )
    if problem:
        return failed(f"Peer Pi destination is not the pane that was selected ({problem}).")

    prompt = request.message if request.no_wrap else wrap_pi_delivery_prompt(request.message, task.req_id)
    deadline = _pi_peer_delivery_deadline(task)
    if deadline is None:
        return failed("Peer Pi delivery has a non-finite timeout; refusing to deliver.")

    from contextlib import ExitStack

    while True:
        verdict = wait_until_pi_injectable(
            lambda: binding,
            deadline,
            # Resolved from pi_comm's own defaults at call time (not bound into the
            # dataclass), so one place tunes the wait for every Pi peer delivery.
            policy=PiGatePolicy(
                retry_wait_s=PRE_SEND_RETRY_WAIT_S,
                retry_limit=PRE_SEND_RETRY_LIMIT,
                busy_log_interval_s=BUSY_LOG_INTERVAL_S,
            ),
            cancel=cancelled,
            log=_write_peer_log,
            label="provider=pi peer",
            req_id=task.req_id,
        )
        if verdict.outcome == "cancelled":
            return ProviderResult(exit_code=2, reply="Peer delivery cancelled.", req_id=task.req_id,
                                  session_key=session_key, done_seen=False, status="cancelled")
        if verdict.outcome == "expired":
            return failed("Peer Pi delivery timed out before Pi was ready; nothing was sent.")
        if not verdict.ready:
            state = verdict.state
            return failed(f"Peer Pi destination is not injectable (state: {state.status}; {state.reason}); nothing was sent.")

        # F3: an exhausted budget refuses BEFORE the lock is even attempted, so nothing can be
        # pasted for a caller that already stopped waiting.
        if cancelled():
            return ProviderResult(exit_code=2, reply="Peer delivery cancelled.", req_id=task.req_id,
                                  session_key=session_key, done_seen=False, status="cancelled")
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            return failed("Peer Pi delivery timed out before Pi was ready; nothing was sent.")
        lock_budget_s: float | None = min(default_timeout_s(), remaining_s)
        with ExitStack() as stack:
            # ONLY the acquisition is a proven zero-send refusal. Anything a PaneLockError raised
            # later (inside the body) means the lock WAS held and the send may have started, so it
            # must reach the caller as an exception (a remote record becomes `outcome-unknown`).
            try:
                stack.enter_context(pane_injection_lock(backend, destination.pane_id, lock_budget_s))
            except PaneLockError as exc:
                return failed(f"Pane injection lock unavailable: {exc}")
            if cancelled():
                return ProviderResult(exit_code=2, reply="Peer delivery cancelled.", req_id=task.req_id,
                                      session_key=session_key, done_seen=False, status="cancelled")
            if time.monotonic() >= deadline:
                return failed("Peer Pi delivery timed out before Pi was ready; nothing was sent.")

            # G1: the ORDER inside the lock is the safety property here, not any single check.
            #
            # 1. Revalidate the endpoint and apply the role policy. Both can take real time (the
            #    policy module reads the destination launch), and they must judge the endpoint this
            #    send resolved.
            outcome = revalidate_peer_destination(destination)
            if not outcome.ok:
                return failed(f"Peer destination unavailable ({outcome.error}).", status="failed")
            from delivery_policy import refusal_for_request

            refusal = refusal_for_request(request, "pi", expected_pane=destination.pane_id)
            if refusal:
                return failed(refusal)

            # 2. Reload the binding AFTER those checks: the readiness file, or the binding itself,
            #    can have been switched while they ran.
            lock_binding, lock_error = peer_pi_destination_binding(
                destination.work_dir,
                pane_id=destination.pane_id,
                terminal=destination.terminal,
                launch_id=destination.launch_id,
            )
            if lock_error or lock_binding is None:
                return failed(
                    f"Peer Pi destination changed while waiting ({lock_error or 'no Pi binding'})."
                )
            # 3. Confirm readiness against THE BINDING WE WOULD SEND WITH -- the reloaded one, not
            #    the pre-lock copy. Not ready: release the lock and resume the outer wait against
            #    the refreshed binding, still inside the original deadline; never paste.
            fresh = confirm_pi_ready(lock_binding, deadline)
            if fresh.status != "ready":
                _write_peer_log(
                    f"[INFO] peer delivery re-confirm after lock provider=pi req_id={task.req_id} "
                    f"state={fresh.status}"
                )
                binding = lock_binding
                continue
            # 4. The saved process witness, versus that same reloaded binding and a fresh ready
            #    snapshot. Nothing slow runs between here and the paste.
            problem = pi_incarnation_problem(
                backend,
                lock_binding,
                pane_id=destination.pane_id,
                terminal=destination.terminal,
                recorded_pane_pid=destination.pane_pid,
                recorded_launch_token=destination.launch_token,
                require_ready=True,
            )
            if problem:
                return failed(f"Peer Pi destination changed while waiting ({problem}).")
            binding = lock_binding
            # 5. Cancellation and expiry, last.
            if cancelled():
                return ProviderResult(exit_code=2, reply="Peer delivery cancelled.", req_id=task.req_id,
                                      session_key=session_key, done_seen=False, status="cancelled")
            if time.monotonic() >= deadline:
                return failed("Peer Pi delivery timed out before Pi was ready; nothing was sent.")
            # 6. Paste.
            backend.send_text(destination.pane_id, prompt)
        return ProviderResult(exit_code=0, reply="Peer message accepted.", req_id=task.req_id,
                              session_key=session_key, done_seen=False, status="completed",
                              extra={"confirmation": "sent"})


def send_peer_message(task, provider: str, session_key: str, *, before_dispatch=None):
    """Send a queued delivery-only message to its exact saved endpoint.

    The pane-injection lock is taken BEFORE `revalidate_peer_destination` and
    the role-policy recheck, so both judge the destination AFTER any wait for
    another sender -- a pane replaced while this task waited is refused, not
    sent to blindly. The backend's own `send_text` lock re-enters harmlessly.

    `before_dispatch` runs inside that lock, after those checks and
    immediately before `send_text`; T2's remote dispatch uses it to durably
    commit `dispatch-in-progress`. If it raises, nothing is sent (proven
    non-delivery) and the caller may record `failed`.

    Pi is the exception to "paste and go": a Pi pane cannot queue typed input, so
    `_send_pi_peer_message` runs the shared readiness gate outside the lock and confirms
    again inside it. A Pi delivery rejects `before_dispatch` outright (phase 1 has no Pi
    dispatch hook); the Claude/Codex paths are byte-for-byte what they were.
    """
    if provider == "pi":
        if before_dispatch is not None:
            # H1: a Pi peer delivery has no dispatch hook in phase 1. The hook exists for the
            # remote journal's durable commit, and Pi's remote path (with its journal + send
            # ordering) is phase 2. Refuse BEFORE any readiness wait, lock or paste -- a caller
            # that expects a commit it will never get must hear "nothing was sent", and the
            # callback must not run at all.
            from askd.adapters.base import ProviderResult as _ProviderResult
            from completion_hook import COMPLETION_STATUS_FAILED as _FAILED

            return _ProviderResult(
                exit_code=1,
                reply="Pi peer delivery has no dispatch hook in this phase; nothing was sent.",
                req_id=task.req_id,
                session_key=session_key,
                done_seen=False,
                status=_FAILED,
            )
        return _send_pi_peer_message(task, session_key)

    from askd.adapters.base import ProviderResult
    from ccb_protocol import wrap_codex_delivery_prompt
    from completion_hook import COMPLETION_STATUS_FAILED
    from laskd_protocol import wrap_claude_delivery_prompt
    from pane_lock import PaneLockError, pane_injection_lock
    from terminal import get_backend_for_session

    request = task.request
    destination = request.peer_destination
    if provider not in {"codex", "claude"} or not request.delivery_only or request.route.present:
        raise ValueError("peer destinations require a delivery-only Claude/Codex request")
    if task.cancelled or (task.cancel_event and task.cancel_event.is_set()):
        return ProviderResult(exit_code=2, reply="Peer delivery cancelled.", req_id=task.req_id,
                              session_key=session_key, done_seen=False, status="cancelled")
    wrapper = wrap_codex_delivery_prompt if provider == "codex" else wrap_claude_delivery_prompt
    prompt = request.message if request.no_wrap else wrapper(request.message, task.req_id)
    backend = get_backend_for_session({"terminal": destination.terminal})
    if backend is None:
        return ProviderResult(exit_code=1, reply="Peer destination terminal backend is unavailable.",
                              req_id=task.req_id, session_key=session_key, done_seen=False,
                              status=COMPLETION_STATUS_FAILED)
    from contextlib import ExitStack

    with ExitStack() as stack:
        # ONLY the acquisition is a proven zero-send refusal. Anything a
        # PaneLockError raised later (inside the body) means the lock WAS held
        # and the send may have started, so it must never be folded into this
        # non-delivery answer -- it has to reach the caller as an exception so
        # a remote record becomes `outcome-unknown`, not `failed`.
        try:
            stack.enter_context(pane_injection_lock(backend, destination.pane_id, None))
        except PaneLockError as exc:
            return ProviderResult(exit_code=1, reply=f"Pane injection lock unavailable: {exc}",
                                  req_id=task.req_id, session_key=session_key, done_seen=False,
                                  status=COMPLETION_STATUS_FAILED)
        else:
            outcome = revalidate_peer_destination(destination)
            if not outcome.ok:
                return ProviderResult(exit_code=1, reply=f"Peer destination unavailable ({outcome.error}).",
                                      req_id=task.req_id, session_key=session_key, done_seen=False,
                                      status="failed")
            # Phase 3 (E5/G6): the role-policy recheck runs here, immediately
            # before injection, against the endpoint this send resolved --
            # never earlier, so a role or pane change during the wait above
            # cannot slip through.
            from delivery_policy import refusal_for_request

            refusal = refusal_for_request(
                request, provider, expected_pane=destination.pane_id
            )
            if refusal:
                return ProviderResult(exit_code=1, reply=refusal, req_id=task.req_id,
                                      session_key=session_key, done_seen=False,
                                      status=COMPLETION_STATUS_FAILED)
            if before_dispatch is not None:
                try:
                    before_dispatch()
                except Exception as exc:
                    # No dispatch intent could be committed, so nothing was sent.
                    return ProviderResult(
                        exit_code=1,
                        reply=f"Dispatch intent could not be committed; nothing was sent: {exc}",
                        req_id=task.req_id,
                        session_key=session_key,
                        done_seen=False,
                        status=COMPLETION_STATUS_FAILED,
                    )
            backend.send_text(destination.pane_id, prompt)
    return ProviderResult(exit_code=0, reply="Peer message accepted.", req_id=task.req_id,
                          session_key=session_key, done_seen=False, status="completed",
                          extra={"confirmation": "sent"})
