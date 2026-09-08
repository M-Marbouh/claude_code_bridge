"""Live-session inventory and deterministic destination resolution.

A CCB launch registry record keys panes by provider (`providers.codex`), which
makes "provider" and "destination" the same thing. That holds only while every
provider appears once. To support two Codex panes in one launch, this module
reads an additive `live_sessions` list off the same record and resolves a
request to exactly one of them, or to a structured refusal.

Nothing here writes. Legacy records carry no `live_sessions` key and are
projected from their `providers` map, one live session per provider, so
existing single-provider launches resolve exactly as before.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from project_id import normalize_work_dir


@dataclass(frozen=True)
class LiveSession:
    """One provider process/pane inside one launch."""

    live_id: str
    provider: str
    launch_id: str
    pane_id: str = ""
    terminal: str = ""
    pane_title_marker: str = ""
    work_dir: str = ""
    ccb_project_id: str = ""
    active: bool = True
    # A per-session binding file reference. Unlike `pane_title_marker` or
    # `work_dir`, this inherits nothing from the containing record — there is
    # no record-level "session_file" to fall back to, only what one specific
    # entry names for itself.
    session_file: str = ""
    auth_token: str = ""

    def matches_pane(self, pane_id: str, terminal: str = "") -> bool:
        """Pane identity is only comparable within the same terminal backend."""
        pane = (pane_id or "").strip()
        if not pane or pane != (self.pane_id or "").strip():
            return False
        want = (terminal or "").strip().lower()
        have = (self.terminal or "").strip().lower()
        if want and have and want != have:
            return False
        return True


# Resolution outcomes. A refusal names why, so callers can report it rather
# than fall back to a guess.
AMBIGUOUS = "ambiguous"
UNAVAILABLE = "unavailable"
UNKNOWN_CALLER = "unknown_caller"
NOT_MOUNTED = "not_mounted"
SELF_ONLY = "self_only"


@dataclass(frozen=True)
class Resolution:
    session: Optional[LiveSession] = None
    error: str = ""
    detail: str = ""
    candidates: Sequence[str] = ()

    @property
    def ok(self) -> bool:
        return self.session is not None and not self.error


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _live_id_for_legacy(launch_id: str, provider: str) -> str:
    """Stable synthetic id for a record predating live sessions.

    Derived rather than random so repeated reads of the same record produce the
    same identity, and so a legacy id can never collide with a real one. The id
    identifies a record's provider slot, not a particular pane incarnation —
    callers must still validate pane/marker before trusting it as "the caller".
    """
    return f"legacy:{launch_id}:{provider}"


def _parse_active(value: Any) -> bool:
    """Parse an `active` flag explicitly rather than by truthiness.

    `bool("false")` is `True`, which would silently treat an explicitly
    inactive session as active — Python's stringly-typed `bool()` cannot be
    trusted here. Real booleans pass through unchanged. The recognized string
    spellings are "true"/"1"/"yes" (active) and "false"/"0"/"no" (inactive),
    case-insensitive and stripped. A missing key still defaults to active, via
    the caller's `entry.get("active", True)`.

    Anything else — `None`, an unrecognized string, a number, a list — is
    treated as INACTIVE. This is deliberately fail-closed: an unparseable
    value is a sign the record is malformed, and refusing to route toward it
    is the safe direction, unlike defaulting it into eligibility.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in ("true", "1", "yes"):
            return True
        return False
    return False


def _normalize_work_dir_or_none(value: str) -> Optional[str]:
    """`normalize_work_dir`, but a failure to normalize is not a free pass.

    Used only for comparison, never for storage — a failure here must read as
    "these two values cannot be shown to agree," not as a match.
    """
    try:
        return normalize_work_dir(value)
    except Exception:
        return None


# Outcome tags for `read_inventory`. `live_sessions_from_record` collapses
# INVENTORY_ABSENT and INVENTORY_INVALID to the same `[]` for callers that
# only ever wanted a list — but "no inventory key at all" and "the key is
# there and broken" are different facts, and a caller that must not confuse a
# broken record for a legacy one needs to tell them apart.
INVENTORY_ABSENT = "absent"
INVENTORY_VALID = "valid"
INVENTORY_INVALID = "invalid"


@dataclass(frozen=True)
class InventoryResult:
    """The outcome of reading one record's live-session inventory.

    `sessions` is the parsed list (empty for both ABSENT-but-projection-
    failed and INVALID; for ABSENT it's the legacy per-provider projection
    when that succeeds). `status` says which of the three cases produced it.
    """

    sessions: Sequence[LiveSession] = ()
    status: str = INVENTORY_ABSENT

    @property
    def present(self) -> bool:
        """True when the record carries a `live_sessions` key at all."""
        return self.status != INVENTORY_ABSENT

    @property
    def valid(self) -> bool:
        return self.status == INVENTORY_VALID


def read_inventory(record: Dict[str, Any]) -> InventoryResult:
    """Read one launch registry record's live-session inventory, explicitly.

    This is where the parsing and validation rules live. A `live_sessions`
    list, when present, is the sole source of truth: any defect in it (a
    malformed entry, a duplicate id, an entry that disagrees with the record
    it lives in) invalidates the WHOLE inventory rather than silently
    dropping members, because dropping one member of a pair turns an
    ambiguous destination into a falsely unique one. It also never falls back
    to the legacy projection below — only an absent key does that.

    `live_sessions_from_record` is a thin `[]`-on-failure wrapper around this
    function, kept for callers that only need the list and don't need to
    distinguish "valid, no sessions" from "present but invalid" — see
    `InventoryResult` for the signal this function adds on top.
    """
    if not isinstance(record, dict):
        return InventoryResult(sessions=(), status=INVENTORY_ABSENT)

    launch_id = _clean(record.get("ccb_session_id"))
    work_dir = _clean(record.get("work_dir"))
    project_id = _clean(record.get("ccb_project_id"))
    record_terminal = _clean(record.get("terminal"))

    if "live_sessions" in record:
        entries = record.get("live_sessions")
        if not isinstance(entries, list):
            return InventoryResult(sessions=(), status=INVENTORY_INVALID)
        if not launch_id:
            # A new-style inventory with no containing launch id has nothing
            # to scope its entries to — same requirement the legacy
            # projection enforces below, for the same reason.
            return InventoryResult(sessions=(), status=INVENTORY_INVALID)
        out: List[LiveSession] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                return InventoryResult(sessions=(), status=INVENTORY_INVALID)
            live_id = _clean(entry.get("live_id"))
            provider = _clean(entry.get("provider")).lower()
            if not live_id or not provider:
                return InventoryResult(sessions=(), status=INVENTORY_INVALID)
            if live_id in seen:
                return InventoryResult(sessions=(), status=INVENTORY_INVALID)
            if live_id.startswith("legacy:"):
                # Reserved for the derived legacy projection; a real entry
                # claiming this prefix would collide with one.
                return InventoryResult(sessions=(), status=INVENTORY_INVALID)
            entry_launch_id = _clean(entry.get("launch_id"))
            if entry_launch_id and entry_launch_id != launch_id:
                return InventoryResult(sessions=(), status=INVENTORY_INVALID)
            entry_project_id = _clean(entry.get("ccb_project_id"))
            if entry_project_id and entry_project_id != project_id:
                return InventoryResult(sessions=(), status=INVENTORY_INVALID)
            entry_work_dir = _clean(entry.get("work_dir"))
            if entry_work_dir:
                # A raw string compare would miss a trailing slash or other
                # equivalent spelling and falsely reject it; but a failed
                # normalization must not be waved through as equal either.
                entry_norm = _normalize_work_dir_or_none(entry_work_dir)
                record_norm = _normalize_work_dir_or_none(work_dir)
                if entry_norm is None or record_norm is None or entry_norm != record_norm:
                    return InventoryResult(sessions=(), status=INVENTORY_INVALID)
            seen.add(live_id)
            out.append(
                LiveSession(
                    live_id=live_id,
                    provider=provider,
                    launch_id=entry_launch_id or launch_id,
                    pane_id=_clean(entry.get("pane_id")),
                    terminal=_clean(entry.get("terminal")) or record_terminal,
                    pane_title_marker=_clean(entry.get("pane_title_marker")),
                    work_dir=entry_work_dir or work_dir,
                    ccb_project_id=entry_project_id or project_id,
                    active=_parse_active(entry.get("active", True)),
                    # Inherits nothing from the record — only this entry's
                    # own value, or the field-default empty string.
                    session_file=_clean(entry.get("session_file")),
                    auth_token=_clean(entry.get("auth_token")),
                )
            )
        return InventoryResult(sessions=tuple(out), status=INVENTORY_VALID)

    # Legacy projection: one live session per provider entry. The record has
    # no `live_sessions` key at all, so this is the ABSENT case regardless of
    # whether the projection below actually succeeds.
    providers = record.get("providers")
    if not isinstance(providers, dict):
        return InventoryResult(sessions=(), status=INVENTORY_ABSENT)
    if not launch_id:
        # No launch id to key off; deriving one anyway would collide across
        # different records that also lack one.
        return InventoryResult(sessions=(), status=INVENTORY_ABSENT)
    out = []
    for provider, entry in sorted(providers.items()):
        if not isinstance(provider, str) or not isinstance(entry, dict):
            continue
        key = provider.strip().lower()
        if not key:
            continue
        out.append(
            LiveSession(
                live_id=_live_id_for_legacy(launch_id, key),
                provider=key,
                launch_id=launch_id,
                pane_id=_clean(entry.get("pane_id")),
                terminal=_clean(entry.get("terminal")) or record_terminal,
                pane_title_marker=_clean(entry.get("pane_title_marker")),
                work_dir=work_dir,
                ccb_project_id=project_id,
                active=True,
                session_file=_clean(entry.get("session_file")),
            )
        )
    return InventoryResult(sessions=tuple(out), status=INVENTORY_ABSENT)


def live_sessions_from_record(record: Dict[str, Any]) -> List[LiveSession]:
    """Read the live-session inventory out of one launch registry record.

    A thin `[]`-on-failure wrapper over `read_inventory`: this function
    itself defines no validation rules, it just drops the validity signal
    that function carries. Use `read_inventory` directly when a broken
    inventory must be told apart from a genuinely empty one.
    """
    return list(read_inventory(record).sessions)


def find_caller(
    sessions: Iterable[LiveSession],
    *,
    live_id: str = "",
    pane_id: str = "",
    terminal: str = "",
) -> Optional[LiveSession]:
    """Identify which live session issued a request.

    An explicit live id must resolve to exactly one session and must not
    contradict any other evidence supplied alongside it — an id is only as
    trustworthy as the pane it is claimed from. Otherwise the caller is
    identified by pane, which is what a provider pane and anything it spawns
    actually carry.

    This is inventory MATCHING, not authentication: an id-only match only
    proves the id appears in the inventory, not that the caller is who it
    claims to be. Verifying actual pane ownership is the host integration's
    job, before it ever calls this.
    """
    pool = list(sessions)
    wanted = _clean(live_id)
    if wanted:
        matches = [s for s in pool if s.live_id == wanted]
        if len(matches) != 1:
            # No session carries this id, or more than one does: neither is
            # proof of identity.
            return None
        session = matches[0]
        want_terminal = _clean(terminal).lower()
        have_terminal = _clean(session.terminal).lower()
        if want_terminal and have_terminal and want_terminal != have_terminal:
            # The id and the terminal backend disagree about who is calling,
            # independent of whether a pane id was even supplied.
            return None
        if _clean(pane_id) and not session.matches_pane(pane_id, terminal):
            # The id and the pane disagree about who is calling; trust neither.
            return None
        return session

    matches = [s for s in pool if s.matches_pane(pane_id, terminal)]
    if len(matches) == 1:
        return matches[0]
    # No match, or a pane claimed by more than one record: not proof of identity.
    return None


def resolve_local_target(
    sessions: Iterable[LiveSession],
    *,
    provider: str,
    caller: Optional[LiveSession] = None,
) -> Resolution:
    """Pick the one live session a local `ask <provider>` addresses.

    Returns a refusal rather than a guess whenever the destination is not
    uniquely determined. Never returns the caller itself.
    """
    want = _clean(provider).lower()
    if not want:
        return Resolution(error=NOT_MOUNTED, detail="no provider requested")

    pool = [s for s in sessions if s.provider == want]
    if not pool:
        return Resolution(error=NOT_MOUNTED, detail=f"no {want} session in this launch")

    if len(pool) > 1:
        # Identity has to be established from the full topology BEFORE
        # availability narrows it: an inactive sibling still counts toward
        # "more than one", otherwise an unidentified caller sitting in the
        # only active pane would resolve to itself by default.
        if caller is None:
            return Resolution(
                error=UNKNOWN_CALLER,
                detail=f"{len(pool)} {want} sessions and no verified caller",
                candidates=tuple(s.live_id for s in pool),
            )
        if not any(s.live_id == caller.live_id for s in pool):
            return Resolution(
                error=AMBIGUOUS,
                detail=f"caller is not one of the {len(pool)} {want} sessions",
                candidates=tuple(s.live_id for s in pool),
            )
        siblings = [s for s in pool if s.live_id != caller.live_id]
        if len(siblings) != 1:
            # Identified, but still not down to one candidate.
            return Resolution(
                error=AMBIGUOUS,
                detail=f"{len(siblings)} eligible {want} sessions",
                candidates=tuple(s.live_id for s in siblings),
            )
        sibling = siblings[0]
        if not sibling.active:
            return Resolution(
                error=UNAVAILABLE,
                detail=f"the other {want} session is gone",
                candidates=(sibling.live_id,),
            )
        return Resolution(session=sibling)

    # Single-session pool: identity can't be ambiguous, only availability (or
    # the caller being that sole session) can still refuse.
    sole = pool[0]
    if caller and sole.live_id == caller.live_id:
        return Resolution(error=SELF_ONLY, detail=f"the only {want} session is this one")
    if not sole.active:
        return Resolution(
            error=UNAVAILABLE,
            detail=f"no reachable {want} session",
            candidates=(sole.live_id,),
        )
    return Resolution(session=sole)
