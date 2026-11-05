"""Session-held role names for live sessions, for display only.

A session in any CCB launch may record the role its current conversation was
assigned, so a lead that lost its own context can rediscover who is who from
`ccb-list`.

The rules that keep this from becoming authority or durable state:

- Routing never reads roles. A lead addresses a session by its live ID, or
  by provider where a launch has no live-session inventory.
- Only the session itself can record its role: the claim must present that
  session's own per-launch credential. A launch with a live-session inventory
  uses `CCB_LIVE_ID`/`CCB_LIVE_TOKEN`; every other launch uses the member
  credential `CCB_MEMBER_ID`/`CCB_MEMBER_TOKEN` carried on its provider entry.
- Roles are scoped to one launch: they live in a per-launch file beside the
  launch record, never in project session files, and a new launch starts
  with none.
- A role is shown only while the membership it was claimed from is still
  active in the same pane. A dead or replaced pane drops it.
- A role name held by another session of the same launch cannot be claimed.

One membership projection serves both cases. When a record carries a
live-session inventory, that inventory is the sole source of members. Only a
record with NO inventory key at all falls back to the provider entries
that carry a member credential. A malformed inventory never falls back --
that would turn one broken record into a set of unverified identities.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import shlex
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from live_sessions import (
    INVENTORY_ABSENT,
    INVENTORY_INVALID,
    INVENTORY_VALID,
    LiveSession,
)
from pane_registry import (
    _get_providers_map,
    _iter_registry_files,
    _load_registry_file,
    _registry_dir,
    _registry_owner_alive,
    load_registry_by_session_id,
    read_inventory_for_record,
)
from process_lock import ProviderLock

ROLES_PREFIX = "ccb-roles-"
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
ROLE_LOCK_PROVIDER = "ccb-roles"
ROLE_LOCK_TIMEOUT_S = 10.0

NO_CREDENTIAL_MESSAGE = (
    "this session has no CCB launch credential; roles exist only inside a CCB launch"
)


@dataclass(frozen=True)
class RoleMember:
    """One claimable session: an inventory member or a credential-bearing
    provider entry of a launch without a live-session inventory."""

    member_id: str
    provider: str
    launch_id: str
    pane_id: str = ""
    pane_title_marker: str = ""
    terminal: str = ""
    session_file: str = ""
    work_dir: str = ""
    ccb_project_id: str = ""
    active: bool = True
    auth_token: str = ""
    source: str = ""


def roles_path_for_launch(launch_id: str) -> Path:
    return _registry_dir() / f"{ROLES_PREFIX}{launch_id}.json"


def _read_claims(launch_id: str) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(roles_path_for_launch(launch_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(claims, dict):
        return {}
    return {str(k): v for k, v in claims.items() if isinstance(v, dict)}


def _write_claims(launch_id: str, claims: Dict[str, Dict[str, Any]]) -> None:
    path = roles_path_for_launch(launch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".roles-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"launch_id": launch_id, "claims": claims}, handle, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _role_lock(launch_id: str) -> ProviderLock:
    """A launch-scoped file lock shared by CLI processes and daemon threads."""
    return ProviderLock(
        ROLE_LOCK_PROVIDER,
        timeout=ROLE_LOCK_TIMEOUT_S,
        cwd=launch_id,
    )


CREDENTIAL_ENV_KEYS = (
    "CCB_MEMBER_ID",
    "CCB_MEMBER_TOKEN",
    "CCB_LIVE_ID",
    "CCB_LIVE_TOKEN",
)


def credential_env_for_pane(
    launch_id: str, pane_id: str, session_file: str = ""
) -> Dict[str, str]:
    """Re-derive one pane's launch credential from the private registry.

    This is what recovery uses: a session file may not carry the token, so a
    respawned pane asks the launch record for it again. Returns {} whenever
    the launch or the pane cannot be identified -- the caller then respawns
    without a credential, which only costs that pane the ability to claim a
    role.
    """
    launch_id = _clean(launch_id)
    pane_id = _clean(pane_id)
    if not launch_id or not pane_id:
        return {}
    record = load_registry_by_session_id(launch_id)
    if not record:
        return {}
    members, _status = members_for_record(record)
    wanted_file = _clean(session_file)
    for member in members:
        if _clean(member.pane_id) != pane_id:
            continue
        member_file = _clean(member.session_file)
        if wanted_file and member_file and member_file != wanted_file:
            continue
        if member.source == "inventory":
            return {
                "CCB_LIVE_ID": member.member_id,
                "CCB_LIVE_TOKEN": member.auth_token,
                "CCB_MEMBER_ID": member.member_id,
                "CCB_MEMBER_TOKEN": member.auth_token,
            }
        return {
            "CCB_MEMBER_ID": member.member_id,
            "CCB_MEMBER_TOKEN": member.auth_token,
        }
    return {}


def credential_env_prefix_for_pane(
    launch_id: str, pane_id: str, session_file: str = ""
) -> str:
    """Shell prefix that restores a pane's credential at respawn time."""
    env = credential_env_for_pane(launch_id, pane_id, session_file)
    if not env:
        return ""
    try:
        from terminal import get_shell_type

        shell = get_shell_type()
    except Exception:
        shell = "bash"
    if shell == "powershell":
        parts = []
        for key, value in env.items():
            safe = str(value).replace("'", "''")
            parts.append(f"$env:{key} = '{safe}'; ")
        return "".join(parts)
    return "".join(f"export {key}={shlex.quote(str(value))}; " for key, value in env.items())


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _member_from_session(session: LiveSession, launch_id: str) -> RoleMember:
    return RoleMember(
        member_id=_clean(session.live_id),
        provider=_clean(session.provider).lower(),
        launch_id=launch_id,
        pane_id=_clean(session.pane_id),
        pane_title_marker=_clean(session.pane_title_marker),
        terminal=_clean(session.terminal),
        session_file=_clean(session.session_file),
        work_dir=_clean(session.work_dir),
        ccb_project_id=_clean(session.ccb_project_id),
        active=bool(session.active),
        auth_token=_clean(session.auth_token),
        source="inventory",
    )


def members_for_record(record: Dict[str, Any]) -> Tuple[Tuple[RoleMember, ...], str]:
    """Project one registry record to its claimable members.

    Returns `(members, status)`, where `status` is the inventory status
    (`valid`/`absent`/`invalid`). A malformed inventory yields no members at
    all: it must never decay into the provider-entry projection.
    """
    launch_id = _clean(record.get("ccb_session_id"))
    if not launch_id:
        return (), INVENTORY_ABSENT

    inventory = read_inventory_for_record(record)
    if inventory.status == INVENTORY_INVALID:
        return (), INVENTORY_INVALID
    if inventory.status == INVENTORY_VALID:
        members = tuple(
            member
            for member in (
                _member_from_session(session, launch_id) for session in inventory.sessions
            )
            if member.member_id and member.auth_token
        )
        return members, INVENTORY_VALID

    # ABSENT: only provider entries that actually carry a member credential
    # are members. Everything else on this record is routing inventory, not
    # an identity that may claim a role.
    record_terminal = _clean(record.get("terminal"))
    work_dir = _clean(record.get("work_dir"))
    project_id = _clean(record.get("ccb_project_id"))
    members = []
    for provider, entry in sorted(_get_providers_map(record).items()):
        if not isinstance(entry, dict):
            continue
        key = _clean(provider).lower()
        member_id = _clean(entry.get("member_id"))
        member_token = _clean(entry.get("member_token"))
        if not key or not member_id or not member_token:
            continue
        members.append(
            RoleMember(
                member_id=member_id,
                provider=key,
                launch_id=launch_id,
                pane_id=_clean(entry.get("pane_id")),
                pane_title_marker=_clean(entry.get("pane_title_marker")),
                terminal=_clean(entry.get("terminal")) or record_terminal,
                session_file=_clean(entry.get("session_file")),
                work_dir=work_dir,
                ccb_project_id=project_id,
                active=True,
                auth_token=member_token,
                source="provider-entry",
            )
        )
    return tuple(members), INVENTORY_ABSENT


@dataclass(frozen=True)
class MembershipCheck:
    """Outcome of the one current-membership check used everywhere."""

    current: bool
    reason: str = ""
    # False when this backend offers no marker resolver at all, in which case
    # pane-alive plus the session-file binding is all the evidence there is.
    marker_checked: bool = False
    # The pane's current process id when the backend can report one.
    pane_pid: str = ""


def _backend_for_member(record: Dict[str, Any], member: RoleMember):
    try:
        from pane_registry import get_backend_for_session

        return get_backend_for_session(
            {"terminal": _clean(member.terminal) or _clean(record.get("terminal")) or "tmux"}
        )
    except Exception:
        return None


def _session_binding_is_current(record: Dict[str, Any], member: RoleMember) -> Tuple[bool, str]:
    """The provider entry's session file must name this launch and this pane.

    The launch id may be stored as `session_id` (every writer stamps it) or
    as `ccb_session_id` (`laskd` rewrites `.claude-session` from
    `claude_session_resolver._data_from_registry`, which now carries the CCB
    launch id under that name because `session_id` is also what Claude's own
    session id falls back to). Both naming the same launch is fine; two
    different values, no launch id at all, another launch, another pane, a
    contradicting terminal, or an inactive binding all refuse.

    There is deliberately no project+marker+pane fallback: pane ids repeat
    across tmux servers running at the same time, so those three do not
    identify a launch. A session started before credentials existed has no
    member credential and cannot claim anyway.
    """
    launch_id = _clean(record.get("ccb_session_id"))
    project_id = _clean(record.get("ccb_project_id"))
    if not launch_id or member.launch_id != launch_id:
        return False, "launch mismatch"
    if member.ccb_project_id and project_id and member.ccb_project_id != project_id:
        return False, "project mismatch"
    session_file = _clean(member.session_file)
    if not session_file:
        return False, "no session binding"
    path = Path(session_file)
    if not path.is_file():
        return False, "session binding is missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "session binding is unreadable"
    if not isinstance(data, dict):
        return False, "session binding is malformed"
    raw_session_id = _clean(data.get("session_id"))
    raw_ccb_session_id = _clean(data.get("ccb_session_id"))
    if raw_session_id and raw_ccb_session_id and raw_session_id != raw_ccb_session_id:
        return False, "session binding contradicts itself"
    file_launch_id = raw_session_id or raw_ccb_session_id
    if not file_launch_id:
        return False, "session binding has no launch ID"
    if file_launch_id != launch_id:
        return False, "session binding names another launch"
    file_terminal = _clean(data.get("terminal"))
    record_terminal = _clean(record.get("terminal"))
    if file_terminal and record_terminal and file_terminal != record_terminal:
        return False, "session binding names another terminal"
    if _clean(data.get("pane_id")) != _clean(member.pane_id):
        return False, "session binding names another pane"
    if data.get("active") is False:
        return False, "session binding is inactive"
    return True, ""


def check_member(
    record: Dict[str, Any],
    member: RoleMember,
    *,
    claim: Optional[Dict[str, Any]] = None,
    backend: Any = None,
) -> MembershipCheck:
    """Is this membership current right now?

    One check, used by role validity, by the held-by test and by `ccb-list`:
    the record's launch and project, the provider entry's session file naming
    this pane and launch, the pane being alive, and -- on a backend that can
    resolve title markers -- the pane's marker resolving to this pane. A
    backend without a marker resolver (`marker_checked=False` in the result)
    is judged on pane-alive plus the session-file binding.

    When `claim` is supplied it must name this same pane and, on a backend
    that can respawn a pane in place (`respawn_pane`, today tmux), it must
    carry a pane process id that is re-read successfully and matches. Without
    that witness a claim is not current there, because the pane id survives
    the respawn. A new claim on such a backend needs a readable pid as well:
    the id of the process that was validated is what gets stored.

    Backends that cannot respawn in place (WezTerm) have no such hazard: they
    keep working without a pid, and a changed pane id is what invalidates a
    claim there.
    """
    ok, reason = _session_binding_is_current(record, member)
    if not ok:
        return MembershipCheck(False, reason)
    if not member.active:
        return MembershipCheck(False, "member is inactive")
    pane = _clean(member.pane_id)
    if not pane:
        return MembershipCheck(False, "no pane")
    if claim is not None:
        claimed_pane = _clean(claim.get("pane_id"))
        if claimed_pane and claimed_pane != pane:
            return MembershipCheck(False, "claim was recorded for another pane")
    if _registry_owner_alive(record) is False:
        return MembershipCheck(False, "launch is not running")
    if backend is None:
        backend = _backend_for_member(record, member)
    if backend is None:
        return MembershipCheck(False, "no terminal backend")
    try:
        if not bool(backend.is_alive(pane)):
            return MembershipCheck(False, "pane is gone")
    except Exception:
        return MembershipCheck(False, "pane liveness unknown")

    marker = _clean(member.pane_title_marker)
    resolver = getattr(backend, "find_pane_by_title_marker", None)
    marker_checked = False
    if marker and callable(resolver):
        try:
            resolved = _clean(resolver(marker, member.work_dir))
        except Exception:
            resolved = ""
        if resolved != pane:
            return MembershipCheck(
                False, "pane marker does not resolve to this pane", marker_checked=True
            )
        marker_checked = True

    pid = ""
    pid_getter = getattr(backend, "pane_pid", None)
    if callable(pid_getter):
        try:
            pid = _clean(pid_getter(pane))
        except Exception:
            pid = ""
    # The same capability probe recovery uses: a backend that can respawn a
    # pane in place must prove which process is behind the pane, because the
    # pane id alone survives the respawn.
    can_respawn = callable(getattr(backend, "respawn_pane", None))
    if claim is not None:
        claimed_pid = _clean(claim.get("pane_pid"))
        if claimed_pid:
            # A saved witness is only useful when it is positively confirmed:
            # an unreadable pid is not "no change", it is "cannot prove this
            # is the same pane process".
            if not pid or pid != claimed_pid:
                return MembershipCheck(
                    False, "pane process could not be confirmed", marker_checked, pid
                )
        elif can_respawn:
            # Claims recorded before the witness existed (older releases, and
            # pair roles from a10f048) can never be current here: the pane id
            # outlives the respawn they cannot prove.
            return MembershipCheck(
                False, "claim has no pane process witness", marker_checked, pid
            )
    elif can_respawn and not pid:
        return MembershipCheck(
            False, "pane process could not be read", marker_checked, pid
        )
    return MembershipCheck(True, "", marker_checked, pid)


def _role_for_claim(claim: Dict[str, Any]) -> str:
    role = _clean(claim.get("role"))
    return role if ROLE_NAME_RE.match(role) else ""


def roles_for_record(record: Dict[str, Any]) -> Dict[str, str]:
    """{member_id: role} for members of this launch whose claim is current.

    For a launch with a live-session inventory a member id IS the live ID, so
    callers that key by live ID keep working unchanged.
    """
    launch_id = _clean(record.get("ccb_session_id"))
    if not launch_id:
        return {}
    members, _status = members_for_record(record)
    if not members:
        return {}
    claims = _read_claims(launch_id)
    if not claims:
        return {}
    backends: Dict[str, Any] = {}
    out: Dict[str, str] = {}
    for member in members:
        claim = claims.get(member.member_id)
        if not claim:
            continue
        key = _clean(member.terminal) or _clean(record.get("terminal")) or "tmux"
        if key not in backends:
            backends[key] = _backend_for_member(record, member)
        check = check_member(record, member, claim=claim, backend=backends[key])
        if not check.current:
            continue
        role = _role_for_claim(claim)
        if role:
            out[member.member_id] = role
    return out


def _find_own_member(
    member_id: str, token: str
) -> Tuple[Optional[Dict[str, Any]], Optional[RoleMember], str]:
    """Find the one launch record whose membership this credential names.

    Returns `(record, member, reason)`; `record` is None on refusal.
    """
    found: list[Tuple[Dict[str, Any], RoleMember]] = []
    for path in _iter_registry_files():
        record = _load_registry_file(path)
        if not record:
            continue
        members, _status = members_for_record(record)
        for member in members:
            if member.member_id != member_id:
                continue
            if not member.auth_token or not hmac.compare_digest(member.auth_token, token):
                return None, None, "credential"
            found.append((record, member))
    if not found:
        return None, None, "missing"
    if len(found) > 1:
        return None, None, "contradictory"
    return found[0][0], found[0][1], ""


def _refusal_reason(reason: str, member_id: str) -> str:
    if reason == "credential":
        return f"the credential presented for {member_id} is not this session's own"
    if reason == "contradictory":
        return f"membership {member_id} appears in more than one launch record"
    return f"{member_id} is not a credential-bearing member of any current launch"


def set_own_role(member_id: str, token: str, role: str) -> Tuple[bool, str]:
    """Record (or with role="" clear) the calling session's own role.

    The whole read/check/write runs under a launch-scoped lock so two claims
    for the same role can never both win, across CLI processes and daemon
    threads alike.
    """
    member_id = _clean(member_id)
    token = _clean(token)
    role = _clean(role).lower()
    if not member_id or not token:
        return False, NO_CREDENTIAL_MESSAGE
    if role and not ROLE_NAME_RE.match(role):
        return False, "role must be lowercase letters, digits or '-', starting with a letter (max 32)"

    record, member, reason = _find_own_member(member_id, token)
    if record is None or member is None:
        return False, _refusal_reason(reason, member_id)

    launch_id = _clean(record.get("ccb_session_id"))
    if not launch_id:
        return False, "this launch record has no launch ID"

    lock = _role_lock(launch_id)
    if not lock.acquire():
        return False, f"another role update for launch {launch_id} is still in progress"
    try:
        # Re-read under the lock: the record may have been replaced while
        # this caller waited, and a stale identity must never write.
        record, member, reason = _find_own_member(member_id, token)
        if record is None or member is None:
            return False, _refusal_reason(reason, member_id)
        if _clean(record.get("ccb_session_id")) != launch_id:
            return False, "this session's launch changed while its role was being recorded"
        if _registry_owner_alive(record) is False:
            return False, "this session's launch is no longer running"
        if not member.active:
            return False, "this session is no longer active in its launch"
        check = check_member(record, member)
        if role and not check.current:
            return False, f"this session's pane is not current: {check.reason}"

        current = roles_for_record(record)
        if role:
            holder = next(
                (mid for mid, held in current.items() if held == role and mid != member_id),
                "",
            )
            if holder:
                return False, f"role '{role}' is already held by another session of this launch"
        claims = {mid: claim for mid, claim in _read_claims(launch_id).items() if mid in current}
        if role:
            claims[member_id] = {
                "role": role,
                "pane_id": member.pane_id,
                "provider": member.provider,
                "pane_pid": check.pane_pid,
                "claimed_at": int(time.time()),
            }
        else:
            claims.pop(member_id, None)
        _write_claims(launch_id, claims)
    finally:
        lock.release()

    if role:
        return True, f"role '{role}' recorded for this {member.provider} session (pane {member.pane_id})"
    return True, "role cleared for this session"


def invalidate_pane_claims(launch_id: str, pane_id: str) -> Optional[int]:
    """Drop claims recorded against `pane_id` in `launch_id`.

    Used when that pane is respawned in place or replaced by a different
    pane: a replacement pane never inherits an assignment.

    Returns the number removed (0 = nothing matched), or None when the claim
    file could not be updated at all (lock or write failure). This is
    tidy-up only: recovery ignores the result and still proceeds, because
    correctness rests on the stored pane process witness (or, on a backend
    without in-place respawn, on the pane id changing).
    """
    launch_id = _clean(launch_id)
    pane_id = _clean(pane_id)
    if not launch_id or not pane_id:
        return 0
    lock = _role_lock(launch_id)
    if not lock.acquire():
        return None
    try:
        claims = _read_claims(launch_id)
        kept = {
            member_id: claim
            for member_id, claim in claims.items()
            if _clean(claim.get("pane_id")) != pane_id
        }
        if len(kept) == len(claims):
            return 0
        try:
            _write_claims(launch_id, kept)
        except Exception:
            return None
        return len(claims) - len(kept)
    finally:
        lock.release()


def invalidate_roles_for_launch(launch_id: str) -> int:
    """Drop every claim of a launch that is ending."""
    launch_id = _clean(launch_id)
    if not launch_id:
        return 0
    lock = _role_lock(launch_id)
    if not lock.acquire():
        return 0
    try:
        claims = _read_claims(launch_id)
        try:
            roles_path_for_launch(launch_id).unlink()
        except OSError:
            pass
        return len(claims)
    finally:
        lock.release()
