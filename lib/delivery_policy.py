"""Role delivery policy: which sessions may send new work to which sessions.

This is a guard against misrouting, NOT a permission system. Every pane,
daemon and the launcher run as one OS user, so any process can rewrite the
files this module reads or type into another pane directly. What the gate
does guarantee is that every CCB ingress path applies the same rule, fails
closed, and none of them silently skips it. Action limits (restricting
edits, push or deploy) are not provided here and are not implemented
anywhere yet.

The policy is opt-in per LAUNCH: the destination launch record carries
``role_policy``. Absent means off, and an absent field is not the same
thing as an unreadable one -- a record we know about but cannot read is a
refusal, never "unmanaged". Only a destination with no launch record at
all falls back to today's behaviour.

Roles themselves are Phase 2 state, owned by ``live_roles``; this module
reads them (``roles_for_record``) and never writes or re-implements them.
"""
from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from live_roles import members_for_record
from live_sessions import INVENTORY_ABSENT, INVENTORY_VALID
from pane_registry import (
    _coerce_updated_at,
    _is_stale,
    _iter_registry_files,
    _registry_owner_alive,
    registry_path_for_session,
)
from project_id import compute_ccb_project_id

POLICY_FIELD = "role_policy"

MODE_UNMANAGED = "unmanaged"
MODE_OFF = "off"
MODE_ENFORCE = "enforce"
MODE_ERROR = "error"

POLICY_OFF = "off"
POLICY_ENFORCE = "enforce"
POLICY_VALUES = (POLICY_OFF, POLICY_ENFORCE)

KIND_REQUEST = "request"
KIND_NOTIFY = "notify"
KIND_REPLY = "reply"
REQUEST_KINDS = (KIND_REQUEST, KIND_NOTIFY)
ALL_KINDS = (KIND_REQUEST, KIND_NOTIFY, KIND_REPLY)

# Fixed matrix for v1. A sender role may only address the roles listed here;
# "lead" is the only role external senders (manual, email, another launch,
# cross-project peers, legacy senders, missing credentials) may address.
TARGETS_BY_SENDER_ROLE: Dict[str, Tuple[str, ...]] = {
    "lead": ("implementer", "ratifier"),
    "implementer": ("lead",),
    "ratifier": ("lead",),
}
EXTERNAL_TARGETS: Tuple[str, ...] = ("lead",)

# Discovery outcomes that mean "there was nothing to read", not "the records
# say something": only these leave the destination unknown for version checks.
UNKNOWN_SCOPE_REASONS = frozenset({
    "destination has no pane identity",
    "destination has no project scope",
    "no project scope for the destination",
})


@dataclass(frozen=True)
class Destination:
    """One resolved destination launch member, plus its policy mode."""

    mode: str = MODE_UNMANAGED
    reason: str = ""
    launch_id: str = ""
    provider: str = ""
    member_id: str = ""
    role: str = ""
    pane_id: str = ""
    ccb_project_id: str = ""
    session_file: str = ""
    # More than one launch owns this destination and every one of them is
    # non-enforcing. That is legitimate today, so a plain ask is allowed; an
    # explicit target-role assertion cannot be verified and is refused.
    ambiguous: bool = False
    record: Optional[Dict[str, Any]] = field(default=None, compare=False)


@dataclass(frozen=True)
class Sender:
    """The sending session, derived host-side from a launch credential."""

    member_id: str = ""
    role: str = ""
    external: bool = True
    reason: str = "no launch credential"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


ALLOW = Decision(True)


def _debug(message: str) -> None:
    from pane_registry import _debug as registry_debug

    registry_debug(message)


def policy_mode_for_record(record: Optional[Dict[str, Any]]) -> str:
    """``off``/``enforce`` for a readable record, ``error`` for a bad value."""
    if not isinstance(record, dict):
        return MODE_ERROR
    raw = str(record.get(POLICY_FIELD) or "").strip().lower()
    if raw in ("", POLICY_OFF, "disabled", "false", "0", "no"):
        return MODE_OFF
    if raw in (POLICY_ENFORCE, "enforced", "on", "true", "1", "yes"):
        return MODE_ENFORCE
    # An unrecognized value must never silently disable the gate.
    return MODE_ERROR


@dataclass(frozen=True)
class _Candidate:
    """One launch record that could own a destination."""

    path: Path
    record: Dict[str, Any]
    stale: bool = False


def _read_record_exact(launch_id: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Read one KNOWN launch record, distinguishing absent from unreadable.

    The ONE strict reader in this module. "Absent" is the only outcome that
    may become unmanaged; unreadable or malformed is a refusal, because a
    destination that opted in must not become unmanaged by breaking its own
    record.
    """
    launch_id = (launch_id or "").strip()
    if not launch_id:
        return None, ""
    path = registry_path_for_session(launch_id)
    if not path.exists():
        return None, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, f"launch record is unreadable ({path.name})"
    if not isinstance(data, dict):
        return None, f"launch record is malformed ({path.name})"
    return data, ""


# The historical name, kept for callers that already say "strict".
_read_record_strict = _read_record_exact


def _record_is_stale(record: Dict[str, Any], launch_id: str = "") -> bool:
    identity = str(record.get("ccb_session_id") or launch_id)
    path = registry_path_for_session(identity)
    return _is_stale(_coerce_updated_at(record.get("updated_at"), path))


def _current_roles(record: Dict[str, Any]) -> Dict[str, str]:
    """Indirection over Phase 2 role state (tests stub this, nothing else)."""
    from live_roles import roles_for_record

    return roles_for_record(record)


def _record_members(record: Dict[str, Any]):
    """The ONE member projection: `live_sessions` inventories AND providers."""
    return members_for_record(record)


def _record_project_id(record: Dict[str, Any]) -> str:
    project = str(record.get("ccb_project_id") or "").strip()
    if project:
        return project
    work_dir = str(record.get("work_dir") or "").strip()
    if not work_dir:
        return ""
    try:
        return compute_ccb_project_id(Path(work_dir))
    except Exception:
        return ""


def _destination_from_record(
    record: Dict[str, Any],
    provider: str,
    *,
    member_id: str = "",
    pane_id: str = "",
    stale: bool = False,
) -> Destination:
    """Project one launch record onto the destination member it names.

    A STALE record is judged by its own policy field: enforcing (or an
    unusable value) refuses, because its live claims no longer resolve;
    off stays off, so a launch that never opted in keeps working.
    """
    provider = (provider or "").strip().lower()
    launch_id = str(record.get("ccb_session_id") or "").strip()
    mode = policy_mode_for_record(record)
    reason = "" if mode != MODE_ERROR else f"{POLICY_FIELD} has an unusable value"
    members, status = _record_members(record)
    if status not in (INVENTORY_VALID, INVENTORY_ABSENT):
        return Destination(
            mode=MODE_ERROR,
            reason=reason or f"launch record has an unreadable member inventory ({status})",
            launch_id=launch_id,
            provider=provider,
            record=record,
        )
    wanted_id = (member_id or "").strip()
    wanted_pane = (pane_id or "").strip()
    # An explicit live-id selector wins outright; otherwise the bound pane
    # selects. Requiring BOTH would let a binding's stale pane veto the
    # selector the caller actually named.
    matching = [
        member
        for member in members
        if member.provider == provider
        and (
            member.member_id == wanted_id
            if wanted_id
            else (not wanted_pane or (member.pane_id or "").strip() == wanted_pane)
        )
    ]
    chosen = matching[0] if len(matching) == 1 else None
    if chosen is None and len(matching) > 1:
        # An explicit selector or the bound pane must name ONE member. Picking
        # the first same-provider member is how a pair's sibling gets the
        # message it was never addressed to.
        if mode in (MODE_ENFORCE, MODE_ERROR) or stale:
            return Destination(
                mode=MODE_ERROR,
                reason="cannot identify the destination session",
                launch_id=launch_id,
                provider=provider,
                record=record,
            )
        return Destination(
            mode=mode,
            reason="more than one member matches without a selector",
            launch_id=launch_id,
            provider=provider,
            record=record,
        )
    if chosen is None:
        return Destination(
            mode=mode,
            reason=f"the destination {provider} session is not a member of this launch",
            launch_id=launch_id,
            provider=provider,
            record=record,
        )
    if stale and mode in (MODE_ENFORCE, MODE_ERROR):
        return Destination(
            mode=MODE_ERROR,
            reason="the destination launch record is stale and enforces roles",
            launch_id=launch_id,
            provider=provider,
            member_id=chosen.member_id,
            pane_id=(chosen.pane_id or "").strip(),
            ccb_project_id=str(record.get("ccb_project_id") or "").strip(),
            record=record,
        )
    # Roles are read for every readable, current record: an explicit
    # --to-role assertion is verified even in a non-enforcing launch (A2).
    roles = _current_roles(record) if mode != MODE_ERROR and not stale else {}
    return Destination(
        mode=mode,
        reason=reason,
        launch_id=launch_id,
        provider=provider,
        member_id=chosen.member_id,
        role=roles.get(chosen.member_id, ""),
        pane_id=(chosen.pane_id or "").strip(),
        ccb_project_id=str(record.get("ccb_project_id") or "").strip(),
        session_file=(chosen.session_file or "").strip(),
        record=record,
    )


def _scan_candidates(
    provider: str,
    *,
    project_id: str = "",
    project_prefix: str = "",
    pane_id: str = "",
) -> Tuple[List[_Candidate], str]:
    """Launch records that could own this destination, plus read errors.

    Membership comes from `members_for_record`, so a launch that publishes a
    `live_sessions` inventory is matched by ITS members -- the shape a Codex
    pair really publishes, where the providers map need not carry them. STALE
    records are included and flagged rather than skipped: their policy field
    is still what decides, which is how a stale enforcing destination refuses
    instead of silently vanishing into "unmanaged".

    Unreadable files are reported separately and are never a refusal on their
    own: a destination whose session binds to no launch is unmanaged.
    """
    provider = (provider or "").strip().lower()
    wanted_project = (project_id or "").strip()
    wanted_prefix = (project_prefix or "").strip().lower()
    wanted_pane = (pane_id or "").strip()
    if not (wanted_project or wanted_prefix):
        return [], ""
    candidates: List[_Candidate] = []
    unreadable: List[str] = []
    for path in _iter_registry_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable.append(path.name)
            continue
        if not isinstance(data, dict):
            unreadable.append(path.name)
            continue
        record_project = _record_project_id(data)
        if wanted_project and record_project != wanted_project:
            continue
        if wanted_prefix and not record_project.lower().startswith(wanted_prefix):
            continue
        members, status = _record_members(data)
        if status not in (INVENTORY_VALID, INVENTORY_ABSENT):
            # A launch with a broken inventory is still a launch that owns
            # this destination; the brokenness must surface as its refusal.
            members = ()
        matched = False
        for member in members:
            if member.provider != provider:
                continue
            if wanted_pane and (member.pane_id or "").strip() != wanted_pane:
                continue
            matched = True
            break
        if not matched:
            continue
        candidates.append(_Candidate(path=path, record=data, stale=_record_is_stale(data, str(data.get("ccb_session_id") or ""))))
    return candidates, ", ".join(unreadable)


def _choose_candidate(
    candidates: List[_Candidate], unreadable: str
) -> Tuple[Optional[_Candidate], str, bool]:
    """Pick the one launch that owns this destination.

    Returns `(candidate, error, ambiguous_all_off)`. Ambiguity matters only
    when it could enforce something: several non-enforcing launches of one
    project are legitimate, so they stay allowed (a plain ask) rather than
    becoming a refusal a policy they never opted into would cause.
    """
    alive = [item for item in candidates if _registry_owner_alive(item.record) is not False]
    pool = alive or candidates
    if len(pool) > 1:
        if any(
            policy_mode_for_record(item.record) in (MODE_ENFORCE, MODE_ERROR) for item in pool
        ):
            return None, "more than one running launch claims this destination", False
        _debug(f"delivery policy: {len(pool)} non-enforcing launches share this destination")
        return None, "", True
    if len(pool) == 1:
        return pool[0], "", False
    if unreadable:
        _debug(
            "delivery policy: ignoring unreadable registry file(s) for an unmanaged "
            f"destination: {unreadable}"
        )
    return None, "", False


def ambiguity_all_off(ambiguous_off: bool, candidate: Optional[_Candidate]) -> bool:
    """True when the ambiguity resolved to "several non-enforcing launches"."""
    return bool(ambiguous_off) and candidate is None


def _resolve(
    provider: str,
    *,
    launch_id: str = "",
    pane_id: str = "",
    project_id: str = "",
    project_prefix: str = "",
    member_id: str = "",
) -> Destination:
    """THE destination resolution. Every caller goes through here."""
    provider = (provider or "").strip().lower()
    launch_id = (launch_id or "").strip()
    if launch_id:
        record, error = _read_record_exact(launch_id)
        if error:
            return Destination(mode=MODE_ERROR, reason=error, provider=provider, launch_id=launch_id)
        if record is not None:
            return _destination_from_record(
                record,
                provider,
                member_id=member_id,
                pane_id=pane_id,
                stale=_record_is_stale(record, launch_id),
            )
        # No record at all for a named launch: genuinely unmanaged.
        return Destination(mode=MODE_UNMANAGED, provider=provider)

    candidates, unreadable = _scan_candidates(
        provider, project_id=project_id, project_prefix=project_prefix, pane_id=pane_id
    )
    candidate, error, ambiguous_off = _choose_candidate(candidates, unreadable)
    if error:
        return Destination(mode=MODE_ERROR, reason=error, provider=provider)
    if ambiguity_all_off(ambiguous_off, candidate):
        return Destination(mode=MODE_OFF, ambiguous=True, provider=provider)
    if candidate is None:
        return Destination(mode=MODE_UNMANAGED, provider=provider)
    return _destination_from_record(
        candidate.record,
        provider,
        member_id=member_id,
        pane_id=pane_id,
        stale=candidate.stale,
    )


# --- entry points, all thin wrappers over `_resolve` ----------------------


def destination_from_launch(provider: str, launch_id: str, live_id: str = "") -> Destination:
    """A routed destination: the launch ID is already known exactly."""
    return _resolve(provider, launch_id=launch_id, member_id=live_id)


def destination_from_pane(
    provider: str,
    pane_id: str,
    *,
    ccb_project_id: str = "",
    work_dir: str = "",
) -> Destination:
    """A destination identified by its exact pane (peer and bridge paths)."""
    pane_id = (pane_id or "").strip()
    provider = (provider or "").strip().lower()
    if not pane_id:
        return Destination(mode=MODE_ERROR, reason="destination has no pane identity", provider=provider)
    project_id = (ccb_project_id or "").strip()
    if not project_id and (work_dir or "").strip():
        project_id = _project_id_for(work_dir)
    if not project_id:
        return Destination(
            mode=MODE_ERROR, reason="destination has no project scope", provider=provider
        )
    return _resolve(provider, pane_id=pane_id, project_id=project_id)


def _project_id_for(work_dir: str | Path) -> str:
    try:
        resolved = str(work_dir or "").strip()
        return compute_ccb_project_id(Path(resolved)) if resolved else ""
    except Exception:
        return ""


def _binding_fields(session: Any) -> Tuple[str, str, str, str, str]:
    """`(pane_id, work_dir, ccb_project_id, launch_id, session_file)` of a
    loaded provider session -- a `.data`-bearing object or a plain mapping.

    This is the session the ADAPTER will actually deliver to, so its own
    project scope wins over any hash of the request's work_dir: a request
    from a subdirectory can bind to the parent's session, and the policy
    must judge the destination that binding names.
    """
    data: Dict[str, Any] = {}
    holder = getattr(session, "data", None)
    if isinstance(holder, dict):
        data = holder
    elif isinstance(session, dict):
        data = session
    pane = str(
        data.get("pane_id")
        or data.get("claude_pane_id")
        or data.get("codex_pane_id")
        or getattr(session, "pane_id", "")
        or ""
    ).strip()
    work_dir = str(data.get("work_dir") or getattr(session, "work_dir", "") or "").strip()
    project_id = str(data.get("ccb_project_id") or "").strip()
    session_file = str(
        data.get("session_file") or getattr(session, "session_file", "") or ""
    ).strip()
    launch_id = ""
    for key in ("ccb_session_id", "session_id"):
        candidate = str(data.get(key) or "").strip()
        if candidate and registry_path_for_session(candidate).exists():
            launch_id = candidate
            break
    return pane, work_dir, project_id, launch_id, session_file


def destination_from_binding(
    provider: str,
    *,
    pane_id: str = "",
    work_dir: str = "",
    ccb_project_id: str = "",
    launch_id: str = "",
    member_id: str = "",
) -> Destination:
    """The destination a bound session names, read through that binding."""
    provider = (provider or "").strip().lower()
    project_id = (ccb_project_id or "").strip() or (
        _project_id_for(work_dir) if (work_dir or "").strip() else ""
    )
    if launch_id or (pane_id and project_id):
        # The pane is the member witness even when the launch id is known:
        # dropping it let a pair's FIRST member stand in for the bound one.
        return _resolve(
            provider,
            launch_id=launch_id,
            pane_id=pane_id,
            project_id=project_id,
            member_id=member_id,
        )
    if (work_dir or "").strip():
        return _resolve(provider, project_id=project_id)
    return Destination(mode=MODE_UNMANAGED, provider=provider)


def destination_for_session(
    provider: str, session: Any, member_id: str = ""
) -> Destination:
    """Convenience: destination of an adapter-loaded session object.

    A live-id selector wins over the binding's own pane; without one the
    binding's pane (or the member id it names) selects the member.
    """
    pane, work_dir, project_id, launch_id, _session_file = _binding_fields(session)
    if not member_id:
        member_id = _binding_member_id(session)
    return destination_from_binding(
        provider,
        pane_id=pane,
        work_dir=work_dir,
        ccb_project_id=project_id,
        launch_id=launch_id,
        member_id=member_id,
    )


def _binding_member_id(session: Any) -> str:
    """The member id a binding names, when it names one explicitly."""
    data = getattr(session, "data", None)
    if not isinstance(data, dict):
        data = session if isinstance(session, dict) else {}
    return str(data.get("member_id") or "").strip()


def load_session_binding(provider: str, work_dir: str | Path) -> Any:
    """Load the provider's session exactly the way its adapter does.

    Each loader walks upward from `work_dir`, so a request issued from a
    subdirectory binds to the project's session -- which is precisely the
    destination the transport will reach.
    """
    provider = (provider or "").strip().lower()
    directory = Path(work_dir or ".")
    try:
        if provider == "codex":
            from caskd_session import load_project_session

            return load_project_session(directory)
        if provider == "claude":
            from laskd_session import load_project_session

            return load_project_session(directory)
        if provider == "gemini":
            from gaskd_session import load_project_session

            return load_project_session(directory)
        if provider == "opencode":
            from oaskd_session import load_project_session

            return load_project_session(directory)
        if provider == "pi":
            from pi_comm import load_session_file

            loaded = load_session_file(directory)
            return loaded[1] if loaded else None
        if provider == "hermes":
            from hermes_comm import load_session_file

            loaded = load_session_file(directory)
            return loaded[1] if loaded else None
    except Exception as exc:
        _debug(f"delivery policy: could not load a {provider} session binding: {exc}")
    return None


def destination_from_work_dir(provider: str, work_dir: str | Path | None) -> Destination:
    """An unrouted destination: whatever session binding this directory has."""
    provider = (provider or "").strip().lower()
    session = load_session_binding(provider, work_dir or os.getcwd())
    if session is not None:
        return destination_for_session(provider, session)
    project_id = _project_id_for(work_dir or os.getcwd())
    if not project_id:
        return Destination(mode=MODE_ERROR, reason="no project scope for the destination", provider=provider)
    return _resolve(provider, project_id=project_id)


def destination_from_project_hint(
    provider: str, project_hint: str, member_id: str = ""
) -> Destination:
    """Best-effort destination for a peer target named by path or id prefix.

    Advisory only: the accept gate remains authoritative. A peer launch id is
    not known client-side, so the selector narrows WITHIN the project's own
    launches -- by path or by id prefix, identically.
    """
    hint = (project_hint or "").strip()
    provider = (provider or "").strip().lower()
    if not hint:
        return Destination(mode=MODE_UNMANAGED, provider=provider)
    expanded = Path(hint).expanduser()
    if expanded.is_dir():
        session = load_session_binding(provider, expanded)
        if session is not None:
            destination = destination_for_session(provider, session, member_id=member_id)
            if destination.mode != MODE_UNMANAGED:
                return destination
        return _resolve(provider, project_id=_project_id_for(expanded), member_id=member_id)
    prefix = hint.lower()
    if len(prefix) < 4 or any(ch not in "0123456789abcdef" for ch in prefix):
        return Destination(mode=MODE_UNMANAGED, provider=provider)
    return _resolve(provider, project_prefix=prefix, member_id=member_id)


def destination_for_request(
    request: Any, provider: str, session: Any = None
) -> Destination:
    """Resolve the destination the request is actually going to.

    The adapter's own loaded `session`, when supplied, is authoritative for
    an unrouted request: it names the pane, project and launch the delivery
    will actually reach (a subdirectory request binds to its project's
    parent session). A hash of the request's work_dir is only a last resort.
    """
    route = getattr(request, "route", None)
    if route is not None and getattr(route, "present", False):
        return destination_from_launch(provider, getattr(route, "launch_id", ""), getattr(route, "live_id", ""))
    peer = getattr(request, "peer_destination", None)
    if peer is not None and getattr(peer, "present", False):
        return destination_from_pane(
            provider,
            getattr(peer, "pane_id", ""),
            ccb_project_id=getattr(peer, "ccb_project_id", ""),
            work_dir=getattr(peer, "work_dir", ""),
        )
    if session is not None:
        return destination_for_session(provider, session)
    # An empty work_dir is not "no destination": every adapter resolves it as
    # its own cwd (Path("") == Path(".")), so policy discovery must look at
    # that same project rather than treating the destination as unknown.
    work_dir = str(getattr(request, "work_dir", "") or "").strip() or os.getcwd()
    return destination_from_work_dir(provider, work_dir)


def identify_sender(
    destination: Destination,
    member_id: str,
    member_token: str,
) -> Sender:
    """Verify a launch credential host-side and derive the sender's role.

    Only a credential belonging to THIS destination launch names a member.
    Everything else -- no credential, a wrong token, a credential from
    another launch (peer, cross-project, legacy, email, manual) -- is
    external. Client-asserted caller/provider/pane fields are never used.
    """
    record = destination.record
    if not isinstance(record, dict):
        return Sender(reason="the destination launch could not be read")
    member_id = (member_id or "").strip()
    member_token = (member_token or "").strip()
    if not member_id or not member_token:
        return Sender(reason="no CCB launch credential")
    members, _status = members_for_record(record)
    for member in members:
        if member.member_id != member_id:
            continue
        if not member.auth_token or not hmac.compare_digest(member.auth_token, member_token):
            return Sender(reason="the credential presented is not this launch's")
        roles = _current_roles(record)
        return Sender(member_id=member_id, role=roles.get(member_id, ""), external=False)
    return Sender(reason="the credential presented is not this launch's")


def kind_is_consistent(kind: str, *, delivery_only: bool, has_peer: bool) -> bool:
    """Is this (kind, transport) combination a real one?

    Logical intent is independent of transport: peer messages are always
    delivery_only, so ``request``/``notify`` with delivery_only is valid
    exactly there. An empty kind keeps legacy behaviour and is always
    consistent.
    """
    kind = (kind or "").strip().lower()
    if not kind:
        return True
    if kind == KIND_REQUEST:
        return (not delivery_only) or bool(has_peer)
    if kind == KIND_NOTIFY:
        return bool(delivery_only)
    if kind == KIND_REPLY:
        return bool(delivery_only) and bool(has_peer)
    return False


def verify_reply_correlation(
    destination: Destination,
    *,
    reply_to: str,
    provider: str,
) -> str:
    """Return ``""`` for a verified correlated reply, else the refusal reason.

    A reply is exempt from role checks ONLY when the saved receipt names
    this destination as the original requester: same peer task, reply
    expected, same provider, and the exact requester pane. A client-set
    delivery_only flag or body metadata alone never grants this.
    """
    from task_receipts import find_receipt

    reply_to = (reply_to or "").strip()
    if not reply_to:
        return "missing reply correlation"
    found = find_receipt(reply_to)
    if not found:
        return f"no receipt for reply task {reply_to}"
    receipt = found[1]
    if not str(receipt.get("provider") or "").startswith("peer-"):
        return "reply receipt is not a peer task"
    if not bool(receipt.get("reply_expected")):
        return "reply task does not expect a reply"
    expected_provider = str(receipt.get("caller") or "").strip().lower()
    if expected_provider != (provider or "").strip().lower():
        return f"reply provider mismatch: expected {expected_provider or 'unknown'}"
    receipt_pane = str(receipt.get("caller_pane_id") or "").strip()
    if not receipt_pane:
        return "reply receipt has no original requester pane"
    if destination.pane_id and receipt_pane != destination.pane_id:
        return "reply does not return to the original requester pane"
    receipt_project = str(receipt.get("ccb_project_id") or "").strip()
    if receipt_project and destination.ccb_project_id and receipt_project != destination.ccb_project_id:
        return "reply destination is not the original requester project"
    return ""


def destination_is_known(destination: Destination) -> bool:
    """Did discovery actually identify a launch, rather than find nothing?

    Version-safety checks ("does this daemon gate?") apply only to a
    destination we can speak about. A destination with no pane scope or no
    project is not evidence of an enforcing launch; the host gate decides it
    downstream.
    """
    if destination.launch_id or destination.ambiguous:
        return True
    if destination.mode in (MODE_ENFORCE, MODE_OFF):
        return True
    # An error produced by records we actually read (ambiguity, a stale or
    # corrupt enforcing record, an unusable policy value) is a discovery, so
    # the version check applies. Only "nothing to look at" and "no record at
    # all" are unknown.
    return destination.mode == MODE_ERROR and destination.reason not in UNKNOWN_SCOPE_REASONS


def _label(role: str) -> str:
    return role or "unassigned"


def _refusal(sender_label: str, target_role: str, allowed: Tuple[str, ...]) -> str:
    targets = ", ".join(allowed) if allowed else "no session (its role is not in the policy matrix)"
    return (
        f"role policy refused: sender role '{sender_label}' may not send to target role "
        f"'{_label(target_role)}'; allowed targets: {targets}"
    )


def evaluate(
    *,
    destination: Destination,
    sender: Sender,
    kind: str = "",
    to_role: str = "",
    delivery_only: bool = False,
    has_peer: bool = False,
    reply_to: str = "",
    expected_pane: str = "",
) -> Decision:
    """The one decision function every gate point calls."""
    if destination.mode == MODE_ERROR:
        return Decision(False, f"role policy cannot be determined: {destination.reason}")

    if not kind_is_consistent(kind, delivery_only=delivery_only, has_peer=has_peer):
        return Decision(False, f"inconsistent request kind '{kind}' for this transport")

    requested_role = (to_role or "").strip().lower()

    if (kind or "").strip().lower() == KIND_REPLY:
        # A verified correlated reply always reaches its original requester,
        # whatever role that requester holds now (E2).
        problem = verify_reply_correlation(
            destination, reply_to=reply_to, provider=destination.provider
        )
        if problem:
            return Decision(False, f"role policy refused: {problem}")
        return ALLOW

    if requested_role:
        # Always verified when supplied, with or without enforcement (A2/E4).
        # An unmanaged destination or an ambiguous non-enforcing owner has no
        # role to verify, so the assertion is refused rather than ignored.
        if destination.ambiguous:
            return Decision(
                False,
                "cannot verify target role: more than one launch owns this destination",
            )
        if destination.mode == MODE_UNMANAGED:
            return Decision(
                False,
                "cannot verify target role: the destination is not in a CCB launch",
            )
        if destination.role != requested_role:
            return Decision(
                False,
                f"target role mismatch: {destination.provider} is {_label(destination.role)}",
            )

    if destination.mode == MODE_UNMANAGED:
        return ALLOW

    if destination.mode == MODE_OFF:
        return ALLOW

    # Enforce.
    if not destination.member_id:
        return Decision(
            False,
            "role policy refused: "
            f"{destination.reason or 'the destination session could not be resolved'}",
        )
    if expected_pane and destination.pane_id and destination.pane_id != expected_pane.strip():
        return Decision(
            False,
            "role policy refused: the destination pane changed while the request was queued",
        )
    if not destination.role:
        return Decision(
            False,
            "role policy refused: assign a role first "
            f"(the destination {destination.provider} session has no role)",
        )
    # External first: an older client sends no credential and no kind, and
    # the external rule (destination must hold lead) needs neither. The
    # explicit-kind requirement is for member senders only (E3).
    if sender.external:
        if destination.role in EXTERNAL_TARGETS:
            return ALLOW
        return Decision(
            False,
            "role policy refused: external senders may only send to a session holding "
            f"the lead role; target {destination.provider} is {_label(destination.role)}",
        )
    if not (kind or "").strip():
        return Decision(False, "role policy requires an explicit request kind from a member sender")
    if not sender.role:
        return Decision(False, "role policy refused: assign a role first (this session has no role)")
    if not requested_role:
        # Required for member senders only: an external sender's destination
        # is fixed to `lead` by the rule above, so there is nothing to assert.
        return Decision(
            False,
            "role policy refused: name the destination's role with --to-role "
            "<lead|implementer|ratifier>",
        )
    allowed = TARGETS_BY_SENDER_ROLE.get(sender.role, ())
    if destination.role in allowed:
        return ALLOW
    return Decision(False, _refusal(sender.role, destination.role, allowed))


def evaluate_request(
    request: Any,
    provider: str,
    *,
    expected_pane: str = "",
    destination: Optional[Destination] = None,
    peer_transport: bool = False,
    session: Any = None,
) -> Decision:
    """Evaluate a ``ProviderRequest``-shaped object at any gate point."""
    resolved = (
        destination
        if destination is not None
        else destination_for_request(request, provider, session)
    )
    peer = getattr(request, "peer_destination", None)
    sender = identify_sender(
        resolved,
        getattr(request, "sender_member_id", ""),
        getattr(request, "sender_member_token", ""),
    )
    return evaluate(
        destination=resolved,
        sender=sender,
        kind=getattr(request, "kind", ""),
        to_role=getattr(request, "to_role", ""),
        delivery_only=bool(getattr(request, "delivery_only", False)),
        has_peer=bool(peer_transport or (peer is not None and getattr(peer, "present", False))),
        reply_to=getattr(request, "reply_to", ""),
        expected_pane=expected_pane,
    )


def refusal_for_request(
    request: Any, provider: str, *, expected_pane: str = "", session: Any = None
) -> str:
    """Convenience for adapters: "" when allowed, else the refusal text."""
    decision = evaluate_request(
        request, provider, expected_pane=expected_pane, session=session
    )
    return "" if decision.allowed else decision.reason
