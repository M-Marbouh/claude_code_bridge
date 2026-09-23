"""Session-held role names for live sessions, for display only.

A live session (one pane of a launch with a `live_sessions` inventory) may
record the role its current conversation was assigned, so a lead that lost
its own context can rediscover who is who from `ccb-list`.

The rules that keep this from becoming authority or durable state:

- Routing never reads roles. A lead addresses a session by its live ID.
- Only the session itself can record its role: the claim must present the
  session's own per-launch credential (`CCB_LIVE_ID` + `CCB_LIVE_TOKEN`).
- Roles are scoped to one launch: they live in a per-launch file beside the
  launch record, never in project session files, and a new launch starts
  with none.
- A role is shown only while the session is active in the same pane it was
  claimed from. A dead or replaced pane silently drops it.
- A role name held by another session of the same launch cannot be claimed.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from live_sessions import LiveSession
from pane_registry import (
    _iter_registry_files,
    _load_registry_file,
    _registry_dir,
    read_inventory_for_record,
)

ROLES_PREFIX = "ccb-roles-"
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


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


def _claim_is_current(claim: Dict[str, Any], session: LiveSession) -> bool:
    pane = str(claim.get("pane_id") or "").strip()
    return bool(session.active and pane and pane == (session.pane_id or "").strip())


def roles_for_record(record: Dict[str, Any]) -> Dict[str, str]:
    """{live_id: role} for sessions of this launch whose claim is still current."""
    launch_id = str(record.get("ccb_session_id") or "").strip()
    if not launch_id:
        return {}
    claims = _read_claims(launch_id)
    if not claims:
        return {}
    inventory = read_inventory_for_record(record)
    if not inventory.valid:
        return {}
    out: Dict[str, str] = {}
    for session in inventory.sessions:
        claim = claims.get(session.live_id)
        if claim and _claim_is_current(claim, session):
            role = str(claim.get("role") or "")
            if ROLE_NAME_RE.match(role):
                out[session.live_id] = role
    return out


def _find_own_session(live_id: str, token: str) -> Tuple[Optional[Dict[str, Any]], Optional[LiveSession]]:
    for path in _iter_registry_files():
        record = _load_registry_file(path)
        if not record:
            continue
        inventory = read_inventory_for_record(record)
        if not inventory.valid:
            continue
        for session in inventory.sessions:
            if session.live_id != live_id:
                continue
            if not session.auth_token or not hmac.compare_digest(session.auth_token, token):
                return None, None
            return record, session
    return None, None


def set_own_role(live_id: str, token: str, role: str) -> Tuple[bool, str]:
    """Record (or with role="" clear) the calling session's own role."""
    live_id = (live_id or "").strip()
    token = (token or "").strip()
    role = (role or "").strip().lower()
    if not live_id or not token:
        return False, "this session has no live session ID; roles exist only in a launch with a Codex pair"
    if role and not ROLE_NAME_RE.match(role):
        return False, "role must be lowercase letters, digits or '-', starting with a letter (max 32)"
    record, session = _find_own_session(live_id, token)
    if record is None or session is None:
        return False, "this session's live ID or credential is not in any current launch"
    if role and not (session.active and session.pane_id):
        return False, "this session is not active in a known pane"
    launch_id = str(record.get("ccb_session_id") or "").strip()
    current = roles_for_record(record)
    if role:
        holder = next((lid for lid, held in current.items() if held == role and lid != live_id), "")
        if holder:
            return False, f"role '{role}' is already held by live session {holder}"
    claims = {lid: c for lid, c in _read_claims(launch_id).items() if lid in current}
    if role:
        claims[live_id] = {"role": role, "pane_id": session.pane_id, "claimed_at": int(time.time())}
    else:
        claims.pop(live_id, None)
    _write_claims(launch_id, claims)
    if role:
        return True, f"role '{role}' recorded for live session {live_id} ({session.provider}, pane {session.pane_id})"
    return True, f"role cleared for live session {live_id}"
