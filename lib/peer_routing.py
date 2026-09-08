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

import os
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


def send_peer_message(task, provider: str, session_key: str):
    """Send a queued delivery-only message to its exact saved endpoint."""
    from askd.adapters.base import ProviderResult
    from ccb_protocol import wrap_codex_delivery_prompt
    from laskd_protocol import wrap_claude_delivery_prompt
    from terminal import get_backend_for_session

    request = task.request
    destination = request.peer_destination
    if provider not in {"codex", "claude"} or not request.delivery_only or request.route.present:
        raise ValueError("peer destinations require a delivery-only Claude/Codex request")
    if task.cancelled or (task.cancel_event and task.cancel_event.is_set()):
        return ProviderResult(exit_code=2, reply="Peer delivery cancelled.", req_id=task.req_id,
                              session_key=session_key, done_seen=False, status="cancelled")
    outcome = revalidate_peer_destination(destination)
    if not outcome.ok:
        return ProviderResult(exit_code=1, reply=f"Peer destination unavailable ({outcome.error}).",
                              req_id=task.req_id, session_key=session_key, done_seen=False, status="failed")
    wrapper = wrap_codex_delivery_prompt if provider == "codex" else wrap_claude_delivery_prompt
    prompt = request.message if request.no_wrap else wrapper(request.message, task.req_id)
    backend = get_backend_for_session({"terminal": destination.terminal})
    backend.send_text(destination.pane_id, prompt)
    return ProviderResult(exit_code=0, reply="Peer message accepted.", req_id=task.req_id,
                          session_key=session_key, done_seen=False, status="completed",
                          extra={"confirmation": "sent"})
