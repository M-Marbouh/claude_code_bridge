from __future__ import annotations

import json
import hmac
import os
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any, Iterable, List

from cli_output import atomic_write_text
from live_sessions import (
    INVENTORY_INVALID,
    INVENTORY_VALID,
    AMBIGUOUS,
    SELF_ONLY,
    UNAVAILABLE,
    UNKNOWN_CALLER,
    InventoryResult,
    LiveSession,
    Resolution,
    find_caller,
    live_sessions_from_record,
    read_inventory,
)
from process_lock import _is_pid_alive
from project_id import compute_ccb_project_id, normalize_work_dir
from terminal import get_backend_for_session

REGISTRY_PREFIX = "ccb-session-"
REGISTRY_SUFFIX = ".json"
REGISTRY_TTL_SECONDS = 7 * 24 * 60 * 60


def _debug_enabled() -> bool:
    return os.environ.get("CCB_DEBUG") in ("1", "true", "yes")


def _debug(message: str) -> None:
    if not _debug_enabled():
        return
    print(f"[DEBUG] {message}", file=sys.stderr)


def _registry_dir() -> Path:
    return Path.home() / ".ccb" / "run"


def registry_path_for_session(session_id: str) -> Path:
    return _registry_dir() / f"{REGISTRY_PREFIX}{session_id}{REGISTRY_SUFFIX}"


def _iter_registry_files() -> Iterable[Path]:
    registry_dir = _registry_dir()
    if not registry_dir.exists():
        return []
    return sorted(registry_dir.glob(f"{REGISTRY_PREFIX}*{REGISTRY_SUFFIX}"))


def _coerce_updated_at(value: Any, fallback_path: Optional[Path] = None) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed.isdigit():
            try:
                return int(trimmed)
            except ValueError:
                pass
    if fallback_path:
        try:
            return int(fallback_path.stat().st_mtime)
        except OSError:
            return 0
    return 0


def _is_stale(updated_at: int, now: Optional[int] = None) -> bool:
    if updated_at <= 0:
        return True
    now_ts = int(time.time()) if now is None else int(now)
    return (now_ts - updated_at) > REGISTRY_TTL_SECONDS


def _record_ccb_pid(data: Dict[str, Any]) -> Optional[int]:
    """Return an explicit launcher PID or derive it from legacy ai-*-PID IDs."""
    for key in ("ccb_pid", "parent_pid"):
        try:
            pid = int(data.get(key) or 0)
        except Exception:
            pid = 0
        if pid > 0:
            return pid

    for key in ("ccb_session_id", "session_id"):
        raw = str(data.get(key) or "")
        parts = raw.split("-")
        if len(parts) >= 3 and parts[0] == "ai":
            try:
                pid = int(parts[-1])
            except Exception:
                pid = 0
            if pid > 0:
                return pid
    return None


def _registry_owner_alive(data: Dict[str, Any]) -> Optional[bool]:
    """Return launcher liveness, or None for records without owner identity."""
    pid = _record_ccb_pid(data)
    if pid is None:
        return None
    try:
        return bool(_is_pid_alive(pid))
    except Exception:
        return None


def _load_registry_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        _debug(f"Failed to read registry {path}: {exc}")
    return None


def _provider_entry_from_legacy(data: Dict[str, Any], provider: str) -> Dict[str, Any]:
    """
    Best-effort migration from legacy flat keys to providers.<provider>.*
    """
    provider = (provider or "").strip().lower()
    out: Dict[str, Any] = {}

    if provider == "codex":
        for k_src, k_dst in [
            ("codex_pane_id", "pane_id"),
            ("pane_title_marker", "pane_title_marker"),
            ("codex_session_id", "codex_session_id"),
            ("codex_session_path", "codex_session_path"),
        ]:
            v = data.get(k_src)
            if v:
                out[k_dst] = v
    elif provider == "gemini":
        for k_src, k_dst in [
            ("gemini_pane_id", "pane_id"),
            ("pane_title_marker", "pane_title_marker"),
            ("gemini_session_id", "gemini_session_id"),
            ("gemini_session_path", "gemini_session_path"),
        ]:
            v = data.get(k_src)
            if v:
                out[k_dst] = v
    elif provider == "opencode":
        for k_src, k_dst in [
            ("opencode_pane_id", "pane_id"),
            ("pane_title_marker", "pane_title_marker"),
        ]:
            v = data.get(k_src)
            if v:
                out[k_dst] = v
    elif provider == "claude":
        v = data.get("claude_pane_id")
        if v:
            out["pane_id"] = v

    return out


def _get_providers_map(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    providers = data.get("providers")
    if isinstance(providers, dict):
        out: Dict[str, Dict[str, Any]] = {}
        for k, v in providers.items():
            if isinstance(k, str) and isinstance(v, dict):
                out[k.strip().lower()] = dict(v)
        return out

    # Legacy flat format: derive providers on demand (no persistence here).
    out = {}
    for p in ("codex", "gemini", "opencode", "claude"):
        entry = _provider_entry_from_legacy(data, p)
        if entry:
            out[p] = entry
    return out


def _provider_pane_alive(record: Dict[str, Any], provider: str) -> bool:
    provider_key = (provider or "").strip().lower()
    providers = _get_providers_map(record)
    entry = providers.get(provider_key)
    if not isinstance(entry, dict):
        return False

    pane_id = str(entry.get("pane_id") or "").strip()
    marker = str(entry.get("pane_title_marker") or "").strip()

    backend = None
    try:
        backend = get_backend_for_session({"terminal": record.get("terminal", "tmux")})
    except Exception:
        backend = None
    if not backend:
        return False

    work_dir = str(record.get("work_dir") or "").strip()

    # Best-effort marker resolution if pane_id is missing/stale.
    if (not pane_id) and marker:
        resolver = getattr(backend, "find_pane_by_title_marker", None)
        if callable(resolver):
            try:
                pane_id = str(resolver(marker, work_dir) or "").strip()
            except Exception:
                pane_id = ""

    if not pane_id:
        return False

    try:
        if not bool(backend.is_alive(pane_id)):
            return False
    except Exception:
        return False

    terminal = str(record.get("terminal") or "tmux").strip().lower() or "tmux"
    if terminal == "wezterm":
        cwd_check = getattr(backend, "pane_belongs_to_cwd", None)
        if callable(cwd_check) and work_dir:
            try:
                if not bool(cwd_check(pane_id, work_dir)):
                    return False
            except Exception:
                return False
        return True

    if marker:
        resolver = getattr(backend, "find_pane_by_title_marker", None)
        if not callable(resolver):
            return False
        try:
            return str(resolver(marker, work_dir) or "").strip() == pane_id
        except Exception:
            return False

    return False


def live_sessions_for_record(record: Dict[str, Any]) -> List[LiveSession]:
    """Read the live-session inventory off a stored registry record.

    Delegates entirely to `live_sessions_from_record` for parsing and never
    adds a fallback of its own on top: that parser already decides, on its
    own fail-closed terms, when an empty result means "no live sessions" and
    when it means "this record's `live_sessions` key is broken." Either way
    the result here is an empty list, and this function must not reach into
    `providers` to paper over that — a broken inventory is broken, not
    legacy, and a caller here must not be able to tell the two empties apart
    by consulting `providers` itself.
    """
    return live_sessions_from_record(record)


def read_inventory_for_record(record: Dict[str, Any]) -> InventoryResult:
    """Read a stored registry record's live-session inventory, with its
    validity signalled explicitly (absent / valid / invalid).

    Thin wrapper over `live_sessions.read_inventory`, the same way
    `live_sessions_for_record` wraps `live_sessions_from_record` above: this
    module adds no validation rules of its own here either, only
    registry-specific plumbing sits on top of what `live_sessions` decides.
    """
    return read_inventory(record)


def resolve_live_session_by_id(launch_id: str, live_id: str) -> Optional[LiveSession]:
    """Re-read `launch_id`'s registry record and return the exact session
    named by `live_id`, or `None`.

    This is IDENTITY LOOKUP, not selection: unlike `live_sessions.
    resolve_local_target`, it never chooses among candidates and never
    falls back to a provider-wide search. A caller that gets `None` back
    must refuse -- the record is gone or stale, its inventory is no longer
    valid, or this id no longer appears in it (which also covers the case
    where the whole `live_sessions` key vanished: the record then only
    yields the legacy `legacy:...` projection, which can never match a real
    inventory id). Establishes identity before availability is even in
    scope: the caller decides what to do with `.active` on the result.
    """
    launch_id = (launch_id or "").strip()
    live_id = (live_id or "").strip()
    if not launch_id or not live_id:
        return None
    record = load_registry_by_session_id(launch_id)
    if not record:
        return None
    inventory = read_inventory_for_record(record)
    if not inventory.valid:
        return None
    matches = [session for session in inventory.sessions if session.live_id == live_id]
    if len(matches) != 1:
        return None
    return matches[0]


def confirm_caller_pane(
    *,
    launch_id: str,
    caller_live_id: str = "",
    pane_id: str,
    terminal: str = "",
) -> Optional[LiveSession]:
    """Re-confirm, from a FRESH registry read, that `pane_id` still names the
    SAME live session that originally issued a request -- used by completion
    delivery (Task 3) to decide whether it is safe to push the finished
    answer into that pane, never to pick a destination.

    Returns the live session `pane_id`/`terminal` currently resolve to
    within `launch_id`'s inventory, or `None` when that cannot be
    established: the launch record is gone or stale, its inventory is
    broken, the pane no longer identifies exactly one session there, that
    session is no longer active, or (when `caller_live_id` was supplied) the
    pane's current identity contradicts it. A caller getting `None` back
    must suppress delivery rather than send anyway or search for somewhere
    else to put the answer -- this function never selects among candidates.

    A record with no `live_sessions` key at all -- today's universal case
    -- is NOT the same as a broken one: `read_inventory` still projects it
    to one live session per provider (`INVENTORY_ABSENT`), and this
    function accepts that projection exactly the way every other consumer
    of `read_inventory_for_record` does. Only a `live_sessions` key that is
    actually present and malformed (`INVENTORY_INVALID`) refuses here --
    gating on `.valid` instead would suppress delivery for every launch
    that exists today, which is precisely the "record with no inventory
    behaves byte-identically to today" requirement this function must not
    violate.

    `caller_live_id` is corroborating evidence, not a shortcut: identity is
    always re-established from `pane_id`/`terminal` against a fresh read
    (via `live_sessions.find_caller`), and a supplied `caller_live_id` is
    only ever used to REFUSE on disagreement, never trusted on its own.
    """
    launch_id = (launch_id or "").strip()
    pane_id = (pane_id or "").strip()
    if not launch_id or not pane_id:
        return None

    record = load_registry_by_session_id(launch_id)
    if record is None:
        return None

    inventory = read_inventory_for_record(record)
    if inventory.status == INVENTORY_INVALID:
        return None

    session = find_caller(inventory.sessions, pane_id=pane_id, terminal=terminal)
    if session is None:
        return None

    wanted_live_id = (caller_live_id or "").strip()
    if wanted_live_id and session.live_id != wanted_live_id:
        return None

    if not session.active:
        return None

    return session


def _evidence_agrees(expected: str, actual: str) -> bool:
    """True unless both sides are non-empty and disagree.

    Used for endpoint-evidence comparisons: an EMPTY expectation means the
    route was resolved without an opinion on that field (nothing to
    contradict), but two non-empty values that differ are a real
    replacement and must refuse.
    """
    expected = (expected or "").strip()
    actual = (actual or "").strip()
    if not expected or not actual:
        return True
    return expected == actual


def validate_route(
    *,
    live_id: str,
    launch_id: str,
    provider: str,
    pane_id: str = "",
    terminal: str = "",
    session_file: str = "",
    ccb_project_id: str = "",
    request_project_id: str = "",
    caller_pane_id: str = "",
    caller_terminal: str = "",
    caller_live_id: str = "",
    caller_token: str = "",
) -> Resolution:
    """Re-confirm that a previously resolved route still names the SAME
    live session, without re-running selection.

    Used at both checkpoints Task 3 requires -- daemon-side enqueue and the
    provider adapter's own send -- so both share one definition of "still
    valid" instead of two that could drift.

    ONE registry read per call. The launch record is loaded exactly once,
    right here, and every check below -- the destination's identity and
    evidence, the caller, and launcher ownership -- is derived from that
    SAME snapshot. Reading it more than once would let this function see
    the record in two different states across two reads (one write landing
    in between) and blend them into a false "still matches"; it would also
    let a record that vanished entirely between two reads be read as
    "gone" by one check and silently skipped by another. A single read
    that comes back missing or unusable is one clean refusal for
    everything that depends on it.

    TRUST MODEL for `caller_pane_id` / `caller_terminal` / `caller_live_id`
    (all three: the request's own terminal-environment evidence, read from
    `ProviderRequest.caller_pane_id` / `.caller_terminal`, and the route's
    OWN saved `caller_live_id` from when it was first resolved): this
    phase runs under an explicitly TRUSTED-SAME-USER-CLIENT model. There is
    no socket peer-credential check and none is being added here --
    cross-checking this evidence against the launch record CCB itself
    wrote establishes CONSISTENCY with that record, not cryptographic
    proof of which real terminal pane sent the message. That trust does
    NOT extend to accepting evidence that is missing, unmatched, or
    self-contradictory: those cases refuse below, every time, precisely
    because "trusted" here means "internally consistent," not "exempt from
    checking."

    This self-route re-check applies to a LOCAL PAIR route only: one
    resolved and re-validated inside a single project's own launch
    registry. Every call site that reaches here today gates on `request.
    route.present`, which no cross-project peer request currently sets --
    a peer caller does not belong to the destination's own launch record
    at all, and peer routing must define its own authorization contract in
    a later phase rather than extend this same-launch assumption.

    `pane_id`, `terminal`, `session_file` and `ccb_project_id` are the
    ENDPOINT EVIDENCE the route was originally resolved against (see
    `ResolvedRoute`) -- deliberately NOT a cache: they are compared against
    this one fresh read and used ONLY to refuse on a mismatch, never
    substituted for what that read says. This is what turns a live_id
    whose pane or session file was silently swapped underneath it into a
    refusal rather than a silent follow. `request_project_id`, when given,
    is the CALLING request's own project scope, checked against the
    destination's recorded project independently of whatever
    `ccb_project_id` evidence was carried.

    A destination that fails here must be refused outright -- never
    redirected to a sibling and never silently resubmitted.
    """
    launch_id = (launch_id or "").strip()
    live_id = (live_id or "").strip()
    if not launch_id or not live_id:
        return Resolution(
            error=UNAVAILABLE,
            detail="routed destination is gone or its launch record is no longer readable",
        )

    record = load_registry_by_session_id(launch_id)
    if record is None:
        return Resolution(
            error=UNAVAILABLE,
            detail="routed destination is gone or its launch record is no longer readable",
        )

    inventory = read_inventory_for_record(record)
    if not inventory.valid:
        return Resolution(
            error=UNAVAILABLE,
            detail="routed destination is gone or its launch record is no longer readable",
        )

    matches = [entry for entry in inventory.sessions if entry.live_id == live_id]
    if len(matches) != 1:
        return Resolution(
            error=UNAVAILABLE,
            detail="routed destination is gone or its launch record is no longer readable",
        )
    session = matches[0]

    if session.provider != (provider or "").strip().lower():
        return Resolution(
            error=AMBIGUOUS,
            detail="routed destination no longer matches this provider",
            candidates=(session.live_id,),
        )

    # Whether caller proof is REQUIRED comes from the RECORDED TOPOLOGY and
    # the route's own saved identity -- never from whether evidence merely
    # happened to be supplied:
    #   - a duplicate-provider pool (more than one session of this
    #     provider in this launch) makes the destination ambiguous without
    #     a verified caller to exclude, so caller evidence is mandatory;
    #   - a route that SAVED a caller_live_id needs live evidence to
    #     corroborate it -- a saved identity with nothing backing it is
    #     weaker than no identity at all, not stronger, so losing that
    #     evidence refuses rather than silently trusting the saved value.
    # Only when NEITHER applies (a genuinely unique destination and no
    # caller was ever claimed) does an evidence-free route stay allowed.
    # Evidence supplied on top of that is still validated (Hole 2): once
    # supplied, it is never ignored.
    duplicate_provider_pool = sum(1 for entry in inventory.sessions if entry.provider == session.provider) > 1
    must_verify_caller = duplicate_provider_pool or bool(caller_live_id) or bool(
        caller_token or caller_pane_id or caller_terminal
    )

    if must_verify_caller:
        if caller_pane_id or caller_terminal:
            actual_caller = find_caller(
                inventory.sessions,
                pane_id=caller_pane_id,
                terminal=caller_terminal,
            )
        elif caller_live_id and caller_token:
            actual_caller = find_caller(inventory.sessions, live_id=caller_live_id)
            if actual_caller is not None and (
                not actual_caller.auth_token
                or not hmac.compare_digest(actual_caller.auth_token, caller_token)
            ):
                actual_caller = None
        else:
            actual_caller = None
        if actual_caller is None:
            return Resolution(
                error=UNKNOWN_CALLER,
                detail="the request's caller identity could not be uniquely verified in this launch",
                candidates=(session.live_id,),
            )
        if caller_token and (
            not actual_caller.auth_token
            or not hmac.compare_digest(actual_caller.auth_token, caller_token)
        ):
            return Resolution(
                error=UNKNOWN_CALLER,
                detail="the request's caller credential contradicts the route's saved caller",
                candidates=(session.live_id,),
            )
        if caller_live_id and actual_caller.live_id != caller_live_id.strip():
            return Resolution(
                error=UNKNOWN_CALLER,
                detail="the request's own caller identity contradicts the route's saved caller",
                candidates=(session.live_id,),
            )
        if actual_caller.live_id == session.live_id:
            return Resolution(
                error=SELF_ONLY,
                detail="the request's own caller pane matches the routed destination",
                candidates=(session.live_id,),
            )

    if not _evidence_agrees(pane_id, session.pane_id):
        return Resolution(
            error=UNAVAILABLE,
            detail="routed destination's pane no longer matches what it was resolved against",
            candidates=(session.live_id,),
        )
    if not _evidence_agrees(terminal, session.terminal):
        return Resolution(
            error=UNAVAILABLE,
            detail="routed destination's terminal backend no longer matches what it was resolved against",
            candidates=(session.live_id,),
        )
    if not _evidence_agrees(session_file, session.session_file):
        return Resolution(
            error=UNAVAILABLE,
            detail="routed destination's session file no longer matches what it was resolved against",
            candidates=(session.live_id,),
        )
    if not _evidence_agrees(ccb_project_id, session.ccb_project_id):
        return Resolution(
            error=AMBIGUOUS,
            detail="routed destination's project no longer matches what it was resolved against",
            candidates=(session.live_id,),
        )
    if not _evidence_agrees(request_project_id, session.ccb_project_id):
        return Resolution(
            error=AMBIGUOUS,
            detail="routed destination's project does not match the request's own project",
            candidates=(session.live_id,),
        )
    if _registry_owner_alive(record) is False:
        return Resolution(
            error=UNAVAILABLE,
            detail="the launch that owns this destination is no longer running",
            candidates=(session.live_id,),
        )
    if not session.active:
        return Resolution(
            error=UNAVAILABLE,
            detail="routed destination is no longer active",
            candidates=(session.live_id,),
        )
    return Resolution(session=session)


def session_data_from_live(session: LiveSession) -> Optional[Dict[str, Any]]:
    """Read a routed `LiveSession`'s OWN session-file JSON.

    Never a fallback to some other (e.g. work_dir-wide default) file: a
    session that names no file of its own, or whose file can't be read as a
    JSON object, returns `None` -- a fail-closed refusal, because a
    fallback here is exactly the "project-wide default" reply readers must
    not use for a routed request.

    The file's own recorded `pane_id`, `work_dir` and `ccb_project_id` must
    AGREE with the inventory entry (when both sides state an opinion) --
    this is validation, not a merge: a session file whose own scope
    CONTRADICTS the route it is named from returns `None` rather than
    being silently overwritten with the inventory's version. Only once
    that agreement is confirmed are the LiveSession's own pane-targeting
    fields (`pane_id`, `pane_title_marker`, `terminal`) applied, since the
    inventory entry -- not this file -- is what the route was resolved
    against and commits to being current.
    """
    raw = (session.session_file or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    data = _load_registry_file(path)
    if not data:
        return None

    file_pane_id = str(data.get("pane_id") or "").strip()
    if not _evidence_agrees(session.pane_id, file_pane_id):
        return None
    file_project_id = str(data.get("ccb_project_id") or "").strip()
    if not _evidence_agrees(session.ccb_project_id, file_project_id):
        return None
    file_work_dir = str(data.get("work_dir") or "").strip()
    session_work_dir = (session.work_dir or "").strip()
    if file_work_dir and session_work_dir:
        try:
            agrees = normalize_work_dir(file_work_dir) == normalize_work_dir(session_work_dir)
        except Exception:
            agrees = False
        if not agrees:
            return None

    data = dict(data)
    if session.pane_id:
        data["pane_id"] = session.pane_id
    if session.pane_title_marker:
        data["pane_title_marker"] = session.pane_title_marker
    if session.terminal:
        data["terminal"] = session.terminal
    data.setdefault("work_dir", session.work_dir)
    if session.ccb_project_id:
        data.setdefault("ccb_project_id", session.ccb_project_id)
    return data


def load_registry_by_session_id(session_id: str) -> Optional[Dict[str, Any]]:
    if not session_id:
        return None
    path = registry_path_for_session(session_id)
    if not path.exists():
        return None
    data = _load_registry_file(path)
    if not data:
        return None
    updated_at = _coerce_updated_at(data.get("updated_at"), path)
    if _is_stale(updated_at):
        _debug(f"Registry stale for session {session_id}: {path}")
        return None
    return data


def load_registry_by_pane(
    pane_id: str,
    *,
    ccb_project_id: str = "",
    terminal: str = "",
    provider: str = "",
    require_owner_alive: bool = False,
) -> Optional[Dict[str, Any]]:
    """Load the newest live registry record bound to an exact provider pane."""
    if not pane_id:
        return None
    wanted_project = (ccb_project_id or "").strip()
    wanted_terminal = (terminal or "").strip().lower()
    wanted_provider = (provider or "").strip().lower()
    best: Optional[Dict[str, Any]] = None
    best_ts = -1
    for path in _iter_registry_files():
        data = _load_registry_file(path)
        if not data:
            continue
        if wanted_project:
            effective_project = str(data.get("ccb_project_id") or "").strip()
            if not effective_project:
                work_dir = str(data.get("work_dir") or "").strip()
                if work_dir:
                    try:
                        effective_project = compute_ccb_project_id(Path(work_dir))
                    except Exception:
                        effective_project = ""
            if effective_project != wanted_project:
                continue
        record_terminal = str(data.get("terminal") or "").strip().lower()
        if wanted_terminal and record_terminal and record_terminal != wanted_terminal:
            continue
        providers = _get_providers_map(data)
        candidates = (
            {wanted_provider: providers.get(wanted_provider)}
            if wanted_provider
            else providers
        )
        if not any(
            isinstance(entry, dict) and str(entry.get("pane_id") or "").strip() == pane_id
            for entry in candidates.values()
        ):
            continue
        updated_at = _coerce_updated_at(data.get("updated_at"), path)
        if _is_stale(updated_at):
            _debug(f"Registry stale for pane {pane_id}: {path}")
            continue
        if require_owner_alive and _registry_owner_alive(data) is False:
            _debug(f"Registry owner is not running for pane {pane_id}: {path}")
            continue
        if updated_at > best_ts:
            best = data
            best_ts = updated_at
    return best


def load_registry_by_claude_pane(pane_id: str) -> Optional[Dict[str, Any]]:
    return load_registry_by_pane(pane_id, provider="claude")


def load_registry_by_project_id(ccb_project_id: str, provider: str) -> Optional[Dict[str, Any]]:
    """
    Load the newest alive registry record matching `{ccb_project_id, provider}`.

    This enforces directory isolation and avoids parent-directory pollution.
    """
    proj = (ccb_project_id or "").strip()
    prov = (provider or "").strip().lower()
    if not proj or not prov:
        return None

    best: Optional[Dict[str, Any]] = None
    best_ts = -1
    best_needs_migration = False

    for path in _iter_registry_files():
        data = _load_registry_file(path)
        if not data:
            continue
        updated_at = _coerce_updated_at(data.get("updated_at"), path)
        if _is_stale(updated_at):
            continue

        existing = (data.get("ccb_project_id") or "").strip()
        inferred = ""
        if not existing:
            # Back-compat: infer from work_dir (no side effects while scanning).
            wd = (data.get("work_dir") or "").strip()
            if wd:
                try:
                    inferred = compute_ccb_project_id(Path(wd))
                except Exception:
                    inferred = ""
        effective = existing or inferred

        if effective != proj:
            continue

        if not _provider_pane_alive(data, prov):
            continue

        # Prefer the newest record for this project+provider.
        if updated_at > best_ts:
            best = data
            best_ts = updated_at
            best_needs_migration = (not existing) and bool(inferred)

    if best and best_needs_migration:
        # Best-effort persistence: update only the winning record to include ccb_project_id.
        try:
            if not (best.get("ccb_project_id") or "").strip():
                wd = (best.get("work_dir") or "").strip()
                if wd:
                    best["ccb_project_id"] = compute_ccb_project_id(Path(wd))
                    upsert_registry(best)
        except Exception:
            pass

    return best


def upsert_registry(record: Dict[str, Any]) -> bool:
    session_id = record.get("ccb_session_id")
    if not session_id:
        _debug("Registry update skipped: missing ccb_session_id")
        return False
    path = registry_path_for_session(str(session_id))
    path.parent.mkdir(parents=True, exist_ok=True)

    data: Dict[str, Any] = {}
    if path.exists():
        existing = _load_registry_file(path)
        if isinstance(existing, dict):
            data.update(existing)

    # Captured before any of this write's own merges: the scope
    # (ccb_session_id, work_dir, ccb_project_id) a retained inventory was
    # last validated against, so a later step can tell whether THIS write
    # actually changes it.
    original_scope = (
        str(data.get("ccb_session_id") or "").strip(),
        str(data.get("work_dir") or "").strip(),
        str(data.get("ccb_project_id") or "").strip(),
    )

    # A write that itself carries a `live_sessions` key is "inventory-carrying".
    # Such a write must not silently merge over — i.e. implicitly repair — an
    # inventory that is already broken on disk; that would launder a broken
    # inventory into a valid one nobody asked to fix. Checked here, against
    # the on-disk record as loaded, before any of this write's own field
    # merges below can change the very scope fields (work_dir,
    # ccb_project_id, terminal) that validity depends on. A write that omits
    # `live_sessions` entirely is unaffected by this check and always leaves
    # the stored inventory exactly as it is.
    if "live_sessions" in record and read_inventory(data).status == INVENTORY_INVALID:
        _debug(f"Registry update rejected: stored live_sessions inventory is already broken: {path}")
        return False

    # Normalize to the new schema.
    providers = _get_providers_map(data)

    # Accept either a nested providers dict, or legacy flat keys, or explicit provider.
    incoming_providers = record.get("providers")
    if isinstance(incoming_providers, dict):
        for p, entry in incoming_providers.items():
            if not isinstance(p, str) or not isinstance(entry, dict):
                continue
            key = p.strip().lower()
            providers.setdefault(key, {})
            for k, v in entry.items():
                if v is None:
                    continue
                providers[key][k] = v

    provider = record.get("provider")
    if isinstance(provider, str) and provider.strip():
        p = provider.strip().lower()
        providers.setdefault(p, {})
        for k, v in record.items():
            if v is None:
                continue
            if k in {"provider", "providers"}:
                continue
            # Provider-scoped keys should be passed in nested form by new code.
            if k in {"pane_id", "pane_title_marker"} or k.endswith("_session_id") or k.endswith("_session_path") or k.endswith("_project_id"):
                providers[p][k] = v

    # Migrate legacy flat fields into providers.
    for p in ("codex", "gemini", "opencode", "claude"):
        legacy_entry = _provider_entry_from_legacy(record, p)
        if legacy_entry:
            providers.setdefault(p, {})
            providers[p].update({k: v for k, v in legacy_entry.items() if v is not None})

    # Merge the live-session inventory the same way `providers` is merged
    # above: by identity (here `live_id` rather than provider key), so an
    # incoming write updates or adds an entry without discarding sibling
    # entries it didn't mention. Unlike `providers`, though, an incoming
    # entry is a COMPLETE session record, not a field-level patch: it
    # wholesale replaces whatever is stored under the same `live_id` rather
    # than being merged key-by-key into it. A write that omits
    # `live_sessions` entirely leaves the stored inventory exactly as it is
    # — `pending_live_sessions` stays `None`, and `data["live_sessions"]`
    # (already loaded from the existing file, if any) is never touched.
    #
    # Validation happens in two passes. The first, here, is purely
    # structural and doesn't depend on anything the rest of this write still
    # has to decide: every incoming entry must be a dict carrying a usable
    # `live_id`, and no two incoming entries may claim the same one. A
    # failure here fails the whole write and leaves the stored file
    # untouched — nothing has been persisted yet.
    pending_live_sessions: Optional[List[Dict[str, Any]]] = None
    if "live_sessions" in record:
        incoming_live_sessions = record.get("live_sessions")
        if not isinstance(incoming_live_sessions, list):
            _debug(f"Registry update rejected: live_sessions is not a list: {path}")
            return False

        incoming_ids: set[str] = set()
        for entry in incoming_live_sessions:
            if not isinstance(entry, dict):
                _debug(f"Registry update rejected: live_sessions entry is not an object: {path}")
                return False
            lid = entry.get("live_id")
            if not isinstance(lid, str) or not lid.strip():
                _debug(f"Registry update rejected: live_sessions entry has no usable live_id: {path}")
                return False
            lid = lid.strip()
            if lid in incoming_ids:
                _debug(f"Registry update rejected: duplicate incoming live_id {lid!r}: {path}")
                return False
            incoming_ids.add(lid)

        by_live_id: Dict[str, Dict[str, Any]] = {}
        existing_live_sessions = data.get("live_sessions")
        if isinstance(existing_live_sessions, list):
            # The on-disk-invalid guard above already proved this is not a
            # broken inventory (or there was none to begin with), so every
            # entry here is a well-formed dict with a usable live_id.
            for entry in existing_live_sessions:
                if isinstance(entry, dict):
                    existing_lid = str(entry.get("live_id") or "").strip()
                    if existing_lid:
                        by_live_id[existing_lid] = dict(entry)
        for entry in incoming_live_sessions:
            lid = str(entry.get("live_id")).strip()
            # A whole-record replace, not a patch: keys are copied verbatim,
            # including an explicit `None`. The legacy `providers`/top-level
            # merges elsewhere in this function drop `None` values to mean
            # "leave the old value alone" — that reading is deliberately NOT
            # applied here, because a null must survive to be judged by the
            # scope/structure validation below rather than being quietly
            # absorbed into "unspecified."
            by_live_id[lid] = dict(entry)
        pending_live_sessions = list(by_live_id.values())

    # Top-level fields.
    for key, value in record.items():
        if value is None:
            continue
        if key in {"providers", "provider", "live_sessions"}:
            continue
        # Legacy provider-scoped keys stay duplicated for compatibility but won't be used for routing.
        data[key] = value

    if pending_live_sessions is not None:
        # Second validation pass: now that this write's top-level fields
        # (ccb_session_id, work_dir, ccb_project_id, terminal) have settled
        # into their final values, validate the merged inventory against
        # THAT scope, using the exact same rules the reader applies —
        # `read_inventory` itself, not a second set of rules kept in sync by
        # hand. A merged result that fails is rejected outright: this
        # function repairs nothing, it only accepts or refuses.
        scoped_record = dict(data)
        scoped_record["live_sessions"] = pending_live_sessions
        if read_inventory(scoped_record).status != INVENTORY_VALID:
            _debug(f"Registry update rejected: merged live_sessions inventory is invalid for its scope: {path}")
            return False
        data["live_sessions"] = pending_live_sessions
    elif "live_sessions" in data:
        # This write left live_sessions untouched — the stored inventory is
        # simply being retained. But it may still have changed the scope
        # (ccb_session_id, work_dir, ccb_project_id) those unchanged bytes
        # are implicitly read against, and the same bytes can silently mean
        # something different under a new scope. Only worth re-checking when
        # that scope actually moved: a write that changes nothing about it
        # must behave exactly as it does today, pre-existing brokenness
        # included, since nothing here asked to confront that.
        final_scope = (
            str(data.get("ccb_session_id") or "").strip(),
            str(data.get("work_dir") or "").strip(),
            str(data.get("ccb_project_id") or "").strip(),
        )
        if final_scope != original_scope and read_inventory(data).status != INVENTORY_VALID:
            _debug(
                f"Registry update rejected: retained live_sessions inventory no longer valid under this write's new scope: {path}"
            )
            return False

    data["providers"] = providers

    # Persist launcher ownership explicitly. Existing records remain compatible
    # because their PID can be derived from ccb_session_id (ai-<time>-<pid>).
    owner_pid = _record_ccb_pid(data)
    if owner_pid is not None:
        data["ccb_pid"] = owner_pid

    # Ensure ccb_project_id exists (best-effort from work_dir).
    if not (data.get("ccb_project_id") or "").strip():
        wd = (data.get("work_dir") or "").strip()
        if wd:
            try:
                data["ccb_project_id"] = compute_ccb_project_id(Path(wd))
            except Exception:
                pass

    data["updated_at"] = int(time.time())

    try:
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        return True
    except Exception as exc:
        _debug(f"Failed to write registry {path}: {exc}")
        return False
