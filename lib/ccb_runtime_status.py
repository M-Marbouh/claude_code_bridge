from __future__ import annotations

import json
import hmac
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

import askd_rpc
from ccb_mcpv_bridge import read_degraded_states
from askd_runtime import find_running_state_file, state_file_candidates
from ccb_start_config import load_start_config
from live_sessions import (
    AMBIGUOUS,
    SELF_ONLY,
    UNKNOWN_CALLER,
    LiveSession,
    Resolution,
    find_caller,
    resolve_local_target,
)
from pane_registry import (
    _coerce_updated_at,
    _get_providers_map,
    _is_stale,
    _iter_registry_files,
    _load_registry_file,
    _provider_pane_alive,
    _registry_owner_alive,
    read_inventory_for_record,
)
from project_id import compute_ccb_project_id
from session_utils import find_project_session_file


SUPPORTED_PROVIDERS = ("claude", "codex", "gemini", "opencode")
SESSION_FILENAMES = {provider: f".{provider}-session" for provider in SUPPORTED_PROVIDERS}


@dataclass(frozen=True)
class RegistryProviderRecord:
    project_id: str
    work_dir: str
    provider: str
    provider_entry: dict[str, Any]
    registry_record: dict[str, Any]
    updated_at: int
    timestamp_stale: bool


@dataclass(frozen=True)
class ProviderRuntimeStatus:
    key: str
    provider: str
    capable: bool
    configured: bool
    registered: bool
    pane_alive: bool
    session_bound: bool
    daemon_online: bool
    mounted: bool
    reason: str
    pane_id: str = ""
    pane_title_marker: str = ""
    session_file: str = ""
    timestamp_stale: bool = False
    updated_at: int = 0
    # A record's own live-session inventory can hold more than one session of
    # this provider (e.g. two Codex panes in one launch). When it does, this
    # is the explicit "can't pick one" state, and `candidates` names who was
    # tied rather than leaving a caller to guess. Both default so every
    # existing single-session outcome is unaffected.
    ambiguous: bool = False
    candidates: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "provider": self.provider,
            "capable": self.capable,
            "configured": self.configured,
            "registered": self.registered,
            "pane_alive": self.pane_alive,
            "session_bound": self.session_bound,
            "daemon_online": self.daemon_online,
            "mounted": self.mounted,
            "reason": self.reason,
            "pane_id": self.pane_id,
            "pane_title_marker": self.pane_title_marker,
            "session_file": self.session_file,
            "timestamp_stale": self.timestamp_stale,
            "updated_at": self.updated_at,
            "ambiguous": self.ambiguous,
            "candidates": list(self.candidates),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProviderRuntimeStatus":
        raw_candidates = data.get("candidates")
        return cls(
            key=str(data.get("key") or ""),
            provider=str(data.get("provider") or ""),
            capable=bool(data.get("capable")),
            configured=bool(data.get("configured")),
            registered=bool(data.get("registered")),
            pane_alive=bool(data.get("pane_alive")),
            session_bound=bool(data.get("session_bound")),
            daemon_online=bool(data.get("daemon_online")),
            mounted=bool(data.get("mounted")),
            reason=str(data.get("reason") or ""),
            pane_id=str(data.get("pane_id") or ""),
            pane_title_marker=str(data.get("pane_title_marker") or ""),
            session_file=str(data.get("session_file") or ""),
            timestamp_stale=bool(data.get("timestamp_stale")),
            updated_at=_coerce_int(data.get("updated_at")),
            ambiguous=bool(data.get("ambiguous")),
            candidates=tuple(raw_candidates) if isinstance(raw_candidates, list) else (),
        )


@dataclass(frozen=True)
class ProjectRuntimeStatus:
    work_dir: str
    ccb_project_id: str
    terminal: str
    updated_at: int
    providers: dict[str, ProviderRuntimeStatus]

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_dir": self.work_dir,
            "ccb_project_id": self.ccb_project_id,
            "terminal": self.terminal,
            "updated_at": self.updated_at,
            "providers": {key: status.to_dict() for key, status in sorted(self.providers.items())},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectRuntimeStatus":
        raw_providers = data.get("providers")
        if not isinstance(raw_providers, dict):
            raise ValueError("runtime status has no provider map")
        providers: dict[str, ProviderRuntimeStatus] = {}
        for key, raw_status in raw_providers.items():
            if not isinstance(key, str) or not isinstance(raw_status, dict):
                raise ValueError("runtime status has an invalid provider entry")
            providers[key] = ProviderRuntimeStatus.from_dict(raw_status)
        return cls(
            work_dir=str(data.get("work_dir") or ""),
            ccb_project_id=str(data.get("ccb_project_id") or ""),
            terminal=str(data.get("terminal") or "tmux"),
            updated_at=_coerce_int(data.get("updated_at")),
            providers=providers,
        )


def _coerce_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def inside_managed_codex_sandbox() -> bool:
    network_disabled = (os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") or "").strip().lower()
    managed = (os.environ.get("CCB_MANAGED") or "").strip().lower()
    caller = (os.environ.get("CCB_CALLER") or "").strip().lower()
    return (
        network_disabled in {"1", "true", "yes", "on"}
        and managed in {"1", "true", "yes", "on"}
        and caller == "codex"
    )


def _daemon_project_runtime_status(
    work_dir: Path,
    *,
    include_stale: bool,
    check_daemon: bool,
) -> ProjectRuntimeStatus:
    state_file = find_running_state_file(
        "askd.json",
        protocol_prefix="ask",
        work_dir=work_dir,
        timeout_s=0.5,
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
        "id": f"runtime-status-{os.getpid()}",
        "token": token,
        "operation": "runtime_status",
        "work_dir": str(work_dir),
        "include_stale": include_stale,
        "check_daemon": check_daemon,
    }
    response = askd_rpc.request_daemon(
        state,
        request,
        connect_timeout_s=2.0,
        response_timeout_s=8.0,
    )
    if response.get("type") != "ask.response" or int(response.get("exit_code", 1)) != 0:
        raise RuntimeError(str(response.get("reply") or "askd rejected runtime status"))
    project = response.get("project")
    if not isinstance(project, dict):
        raise RuntimeError("askd returned an invalid runtime status")
    try:
        return ProjectRuntimeStatus.from_dict(project)
    except ValueError as exc:
        raise RuntimeError(f"askd returned an invalid runtime status: {exc}") from exc


def daemon_work_dir_from_state(state: dict[str, Any] | None, *, fallback: str | Path | None = None) -> Path:
    """Return a valid daemon project root, or the caller's work directory."""
    fallback_path = Path(fallback or Path.cwd()).expanduser()
    try:
        fallback_path = fallback_path.resolve()
    except Exception:
        fallback_path = fallback_path.absolute()
    if not isinstance(state, dict):
        return fallback_path
    raw = state.get("work_dir")
    if not isinstance(raw, str) or not raw.strip():
        return fallback_path
    candidate = Path(raw.strip()).expanduser()
    try:
        candidate = candidate.resolve()
    except Exception:
        candidate = candidate.absolute()
    return candidate if candidate.is_dir() else fallback_path


def resolve_daemon_work_dir(work_dir: str | Path | None = None) -> Path:
    """Resolve the reachable daemon's project root for an implicit managed target."""
    fallback = daemon_work_dir_from_state(None, fallback=work_dir)
    if not (os.environ.get("CCB_RUN_DIR") or "").strip():
        return fallback
    state_file = find_running_state_file(
        "askd.json",
        protocol_prefix="ask",
        work_dir=fallback,
        timeout_s=0.5,
    )
    state = askd_rpc.read_state(state_file) if state_file is not None else None
    return daemon_work_dir_from_state(state, fallback=fallback)


def is_project_askd_online(work_dir: Path, project_id: str, *, timeout_s: float = 0.2) -> bool:
    attempt_timeouts = (
        max(0.05, timeout_s),
        max(0.3, timeout_s),
        max(0.5, timeout_s),
    )
    for state_file in state_file_candidates("askd.json", work_dir=work_dir, project_id=project_id):
        state = askd_rpc.read_state(state_file)
        if not isinstance(state, dict):
            continue
        state_work_dir = str(state.get("work_dir") or "").strip()
        if state_work_dir:
            try:
                if compute_ccb_project_id(Path(state_work_dir)) != project_id:
                    continue
            except Exception:
                continue
        if not str(state.get("token") or "").strip():
            continue
        for index, attempt_timeout in enumerate(attempt_timeouts):
            if askd_rpc.ping_daemon("ask", timeout_s=attempt_timeout, state_file=state_file):
                return True
            if index < len(attempt_timeouts) - 1:
                time.sleep(0.05 * (index + 1))
    return False


def _effective_project_id(record: dict[str, Any]) -> str:
    project_id = str(record.get("ccb_project_id") or "").strip()
    if project_id:
        return project_id
    work_dir = str(record.get("work_dir") or "").strip()
    try:
        return compute_ccb_project_id(Path(work_dir)) if work_dir else ""
    except Exception:
        return ""


def _iter_qualifying_registry_records(
    *, project_id: str | None = None, include_stale: bool = False
) -> Iterable[tuple[dict[str, Any], str, str, int, bool]]:
    """Yield `(record, work_dir, effective_project_id, updated_at, timestamp_stale)`
    for registry files passing the staleness/owner/project checks every
    consumer of a raw registry file needs. `iter_registry_provider_records`
    (below) explodes each one into a `RegistryProviderRecord` per legacy
    provider; provider discovery driven off a record's `live_sessions`
    inventory instead of `providers` builds on this same filtered set, so a
    provider named only in the inventory isn't invisible just because
    `providers` never mentioned it. Pure refactor of what this loop already
    did — no filtering behaviour changes.
    """
    for path in _iter_registry_files():
        record = _load_registry_file(path)
        if not record:
            continue
        updated_at = _coerce_updated_at(record.get("updated_at"), path)
        timestamp_stale = _is_stale(updated_at)
        if timestamp_stale and not include_stale:
            continue
        owner_alive = _registry_owner_alive(record)
        if owner_alive is False and not include_stale:
            continue
        effective = _effective_project_id(record)
        if not effective or (project_id and effective != project_id):
            continue
        work_dir = str(record.get("work_dir") or "").strip()
        if not work_dir:
            continue
        yield record, work_dir, effective, updated_at, timestamp_stale


def iter_registry_provider_records(*, project_id: str | None = None, include_stale: bool = False) -> list[RegistryProviderRecord]:
    records: list[RegistryProviderRecord] = []
    for record, work_dir, effective, updated_at, timestamp_stale in _iter_qualifying_registry_records(
        project_id=project_id, include_stale=include_stale
    ):
        for provider, entry in _get_providers_map(record).items():
            if provider not in SUPPORTED_PROVIDERS or not isinstance(entry, dict):
                continue
            records.append(RegistryProviderRecord(effective, work_dir, provider, dict(entry), record, updated_at, timestamp_stale))
    return records


def _inventory_only_provider_records(
    *, project_id: str | None = None, include_stale: bool = False
) -> list[RegistryProviderRecord]:
    """Synthesize a `RegistryProviderRecord` for a provider that a record's
    (valid) `live_sessions` inventory names but its legacy `providers` map
    never does. Without this, such a provider is invisible to
    `resolve_project_runtime_status`, whose provider discovery otherwise runs
    entirely off `providers`. The synthetic entry's `provider_entry` is empty
    on purpose: the inventory-driven status path is what actually reads this
    provider's pane, straight off the matching `LiveSession`, never off this
    placeholder.
    """
    extra: list[RegistryProviderRecord] = []
    for record, work_dir, effective, updated_at, timestamp_stale in _iter_qualifying_registry_records(
        project_id=project_id, include_stale=include_stale
    ):
        inventory = read_inventory_for_record(record)
        if not inventory.valid:
            # An invalid inventory can't be trusted to name real providers;
            # the invalid-inventory refusal below only applies to a provider
            # already known through `providers`.
            continue
        legacy_providers = set(_get_providers_map(record))
        inventory_providers = {session.provider for session in inventory.sessions}
        for provider in sorted(inventory_providers - legacy_providers):
            if provider not in SUPPORTED_PROVIDERS:
                continue
            extra.append(RegistryProviderRecord(effective, work_dir, provider, {}, record, updated_at, timestamp_stale))
    return extra


def _configured_providers(work_dir: Path) -> set[str]:
    try:
        providers = load_start_config(work_dir).data.get("providers")
    except Exception:
        providers = []
    if not isinstance(providers, list):
        return set()
    return {str(provider).strip().lower() for provider in providers if str(provider).strip().lower() in SUPPORTED_PROVIDERS}


def _select_records(records: Iterable[RegistryProviderRecord]) -> dict[str, RegistryProviderRecord]:
    selected: dict[str, RegistryProviderRecord] = {}
    for record in records:
        current = selected.get(record.provider)
        if current is None or (current.timestamp_stale and not record.timestamp_stale) or (
            current.timestamp_stale == record.timestamp_stale and record.updated_at > current.updated_at
        ):
            selected[record.provider] = record
    return selected


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _session_bound(work_dir: Path, project_id: str, provider: str, entry: dict[str, Any]) -> tuple[bool, str]:
    raw = str(entry.get("session_file") or "").strip()
    session_file = Path(raw).expanduser() if raw else find_project_session_file(work_dir, SESSION_FILENAMES[provider])
    if not session_file or not session_file.is_file():
        return False, str(session_file or "")
    data = _load_json(session_file)
    if not data or data.get("active") is not True:
        return False, str(session_file)
    recorded_project = str(data.get("ccb_project_id") or "").strip()
    if recorded_project and recorded_project != project_id:
        return False, str(session_file)
    recorded_provider = str(data.get("provider") or "").strip().lower()
    if recorded_provider and recorded_provider != provider:
        return False, str(session_file)
    entry_pane = str(entry.get("pane_id") or "").strip()
    session_pane = str(data.get("pane_id") or data.get("tmux_session") or "").strip()
    if entry_pane and session_pane and entry_pane != session_pane:
        return False, str(session_file)
    return True, str(session_file)


def _reason(
    configured: bool,
    registered: bool,
    stale: bool,
    pane_alive: bool,
    session_bound: bool,
    daemon_online: bool,
    launcher_alive: bool | None = None,
) -> str:
    if registered and not stale and pane_alive and session_bound and daemon_online:
        return ""
    if not configured and not registered:
        return "not_configured"
    if not registered:
        return "not_registered"
    if launcher_alive is False:
        return "launcher_dead"
    if stale:
        return "registry_stale"
    if not pane_alive:
        return "pane_dead"
    if not session_bound:
        return "session_unbound"
    if not daemon_online:
        return "daemon_offline"
    return "not_mounted"


def resolve_project_runtime_status(
    work_dir: str | Path | None = None,
    *,
    project_id: str | None = None,
    include_stale: bool = False,
    check_daemon: bool = True,
    _allow_daemon_proxy: bool = True,
    _daemon_online_override: bool | None = None,
) -> ProjectRuntimeStatus:
    resolved = Path(work_dir or Path.cwd()).expanduser().resolve()
    if _allow_daemon_proxy and inside_managed_codex_sandbox():
        return _daemon_project_runtime_status(
            resolved,
            include_stale=include_stale,
            check_daemon=check_daemon,
        )
    project_id = (project_id or compute_ccb_project_id(resolved)).strip()
    records = iter_registry_provider_records(project_id=project_id, include_stale=include_stale)
    # A provider named only in a record's live_sessions inventory (never in
    # its legacy `providers` map) still needs to be discoverable, or it
    # silently vanishes from status entirely — provider discovery below is
    # driven off this combined list.
    records = records + _inventory_only_provider_records(project_id=project_id, include_stale=include_stale)
    configured = _configured_providers(resolved)
    selected = _select_records(records)
    degraded = read_degraded_states(resolved)
    daemon_online = (
        _daemon_online_override
        if _daemon_online_override is not None
        else (is_project_askd_online(resolved, project_id) if check_daemon else False)
    )
    statuses: dict[str, ProviderRuntimeStatus] = {}
    for provider in sorted(configured | set(selected) | set(degraded)):
        record = selected.get(provider)
        policy_error = degraded.get(provider) or {}

        # A record whose `live_sessions` key is present at all — valid or
        # not — must have every provider it can name derived from that
        # inventory alone, never from the legacy `providers` map: `providers`
        # may still name a pane the inventory has superseded, or may simply
        # agree by coincidence, and a broken inventory must never be quietly
        # bridged over by reading `providers` as though the key were absent.
        if record is not None:
            inventory = read_inventory_for_record(record.registry_record)
            if inventory.present:
                stale = bool(record.timestamp_stale)
                launcher_alive = _registry_owner_alive(record.registry_record)

                if not inventory.valid:
                    # Distinct from the ambiguous state below: this is "the
                    # inventory itself could not be read," not "it named more
                    # than one session." Never mounted, never consults
                    # `providers`.
                    statuses[provider] = ProviderRuntimeStatus(
                        key=provider,
                        provider=provider,
                        capable=True,
                        configured=provider in configured,
                        registered=True,
                        pane_alive=False,
                        session_bound=False,
                        daemon_online=daemon_online,
                        mounted=False,
                        reason="invalid_inventory",
                        timestamp_stale=stale,
                        updated_at=record.updated_at,
                    )
                    continue

                same_provider_sessions = [
                    session for session in inventory.sessions if session.provider == provider
                ]
                if len(same_provider_sessions) > 1:
                    # More than one session of this provider: there is no
                    # single pane to report on, so say that explicitly rather
                    # than picking one (today's newest-record-wins or
                    # first-match behaviour).
                    statuses[provider] = ProviderRuntimeStatus(
                        key=provider,
                        provider=provider,
                        capable=True,
                        configured=provider in configured,
                        registered=True,
                        pane_alive=False,
                        session_bound=False,
                        daemon_online=daemon_online,
                        mounted=False,
                        reason="ambiguous_sessions",
                        timestamp_stale=stale,
                        updated_at=record.updated_at,
                        ambiguous=True,
                        candidates=tuple(sorted(session.live_id for session in same_provider_sessions)),
                    )
                    continue

                # Zero or exactly one session of this provider: inspect that
                # session's own pane and binding (not the legacy projection,
                # which may name a different pane entirely).
                session = same_provider_sessions[0] if same_provider_sessions else None
                session_pane_id = session.pane_id if session else ""
                session_marker = session.pane_title_marker if session else ""
                session_record = {
                    "providers": {
                        provider: {"pane_id": session_pane_id, "pane_title_marker": session_marker}
                    },
                    "terminal": (session.terminal if session else "") or record.registry_record.get("terminal"),
                    "work_dir": (session.work_dir if session else "") or record.registry_record.get("work_dir"),
                }
                pane_alive = bool(_provider_pane_alive(session_record, provider)) if launcher_alive is not False else False
                # Bind against THIS session's own file reference only — never
                # the provider-default file `_session_bound` would otherwise
                # fall back to when an entry lacks "session_file". A session
                # (or the absence of one) with no explicit file is reported
                # unbound rather than borrowing an unrelated file's binding.
                session_file_ref = session.session_file if session else ""
                if session_file_ref:
                    bound, session_file = _session_bound(
                        resolved, project_id, provider, {"pane_id": session_pane_id, "session_file": session_file_ref}
                    )
                else:
                    bound, session_file = False, ""
                # A sole session's own recorded `active` flag governs mounted
                # status directly — ambiguity detection above already counted
                # inactive members, this is only about the single-session
                # outcome. An inactive session must never report mounted, and
                # must say so rather than reusing a reason that would
                # otherwise read "" (nothing wrong) for an inactive session
                # whose pane/binding/daemon all happen to look healthy.
                session_inactive = session is not None and not session.active
                mounted = bool(not stale and pane_alive and bound and daemon_online and not policy_error and not session_inactive)
                policy_reason = str(policy_error.get("reason_code") or "").strip()
                if policy_reason:
                    reason = f"launch_policy_error:{policy_reason}"
                elif session_inactive:
                    reason = "session_inactive"
                else:
                    reason = _reason(
                        provider in configured,
                        True,
                        stale,
                        pane_alive,
                        bound,
                        daemon_online,
                        launcher_alive,
                    )
                statuses[provider] = ProviderRuntimeStatus(
                    key=provider,
                    provider=provider,
                    capable=True,
                    configured=provider in configured,
                    registered=True,
                    pane_alive=pane_alive,
                    session_bound=bound,
                    daemon_online=daemon_online,
                    mounted=mounted,
                    reason=reason,
                    pane_id=session_pane_id,
                    pane_title_marker=session_marker,
                    session_file=session_file,
                    timestamp_stale=stale,
                    updated_at=max(record.updated_at, _coerce_int(policy_error.get("timestamp"))),
                )
                continue

        # No live_sessions key on this record (or no record at all): today's
        # behaviour, driven off the legacy `providers` map, unchanged.
        entry = record.provider_entry if record else {}
        registered = record is not None
        stale = bool(record.timestamp_stale) if record else False
        launcher_alive = _registry_owner_alive(record.registry_record) if record else None
        pane_alive = bool(_provider_pane_alive(record.registry_record, provider)) if record and launcher_alive is not False else False
        bound, session_file = _session_bound(resolved, project_id, provider, entry) if record else (False, "")
        mounted = bool(
            registered
            and not stale
            and pane_alive
            and bound
            and daemon_online
            and not policy_error
        )
        policy_reason = str(policy_error.get("reason_code") or "").strip()
        statuses[provider] = ProviderRuntimeStatus(
            key=provider,
            provider=provider,
            capable=True,
            configured=provider in configured,
            registered=registered,
            pane_alive=pane_alive,
            session_bound=bound,
            daemon_online=daemon_online,
            mounted=mounted,
            reason=(
                f"launch_policy_error:{policy_reason}"
                if policy_reason
                else _reason(
                    provider in configured,
                    registered,
                    stale,
                    pane_alive,
                    bound,
                    daemon_online,
                    launcher_alive,
                )
            ),
            pane_id=str(entry.get("pane_id") or "").strip(),
            pane_title_marker=str(entry.get("pane_title_marker") or "").strip(),
            session_file=session_file,
            timestamp_stale=stale,
            updated_at=max(
                record.updated_at if record else 0,
                _coerce_int(policy_error.get("timestamp")),
            ),
        )
    newest = max(records, key=lambda record: record.updated_at) if records else None
    return ProjectRuntimeStatus(
        work_dir=str(resolved),
        ccb_project_id=project_id,
        terminal=str(newest.registry_record.get("terminal") or "tmux") if newest else "tmux",
        updated_at=newest.updated_at if newest else 0,
        providers=statuses,
    )


def list_project_runtime_statuses(*, include_stale: bool = False, check_daemon: bool = True) -> list[ProjectRuntimeStatus]:
    records = iter_registry_provider_records(include_stale=include_stale)
    grouped: dict[str, list[RegistryProviderRecord]] = {}
    for record in records:
        grouped.setdefault(record.project_id, []).append(record)
    projects = [
        resolve_project_runtime_status(items[0].work_dir, project_id=project_id, include_stale=include_stale, check_daemon=check_daemon)
        for project_id, items in grouped.items()
        if Path(items[0].work_dir).expanduser().exists()
    ]
    return sorted(projects, key=lambda project: project.updated_at, reverse=True)


def provider_status_for_target(
    target: str,
    *,
    work_dir: str | Path | None = None,
    include_stale: bool = False,
    check_daemon: bool = True,
) -> ProviderRuntimeStatus:
    provider = str(target or "").strip().lower()
    project = resolve_project_runtime_status(work_dir or Path.cwd(), include_stale=include_stale, check_daemon=check_daemon)
    status = project.providers.get(provider)
    if status:
        return status
    return ProviderRuntimeStatus(
        key=provider,
        provider=provider,
        capable=provider in SUPPORTED_PROVIDERS,
        configured=False,
        registered=False,
        pane_alive=False,
        session_bound=False,
        daemon_online=any(item.daemon_online for item in project.providers.values()),
        mounted=False,
        reason="not_configured",
    )



# --------------------------------------------------------------------------
# Live-session route resolution
# --------------------------------------------------------------------------
#
# Resolves the exact live-session destination one `ask <provider>` invocation
# addresses, when the caller's own launch record carries a `live_sessions`
# inventory. Three findings from review govern the shape of this section:
#
#   - The caller's own LAUNCH is established first (by pane/terminal
#     evidence, independent of which provider is being asked), and
#     resolution then proceeds strictly within THAT launch's inventory --
#     never a project-wide, provider-selected record. Two unrelated CCB
#     launches in the same project must never be able to supply each
#     other's destinations or refusals.
#   - A sandboxed caller (managed Codex client) gets a REAL answer via the
#     authenticated daemon RPC/mailbox, computed host-side where real
#     terminal and filesystem state is visible -- never a bypass that
#     silently reports "no inventory" just because this process can't see
#     the answer itself.
#   - Caller-aware resolution supersedes the provider-wide AMBIGUITY
#     verdict ONLY. It still runs the same operational checks
#     (`resolve_project_runtime_status`'s inventory branch also runs:
#     launcher liveness, pane liveness, session binding, daemon
#     availability, launch policy) against the specific destination it
#     selects, so a caller-identified sibling that is not actually usable
#     is still refused, not reported as a successful route.


def _record_claims_pane(record: dict[str, Any], pane_id: str, terminal: str) -> bool:
    """True when this record's raw `providers` map names a pane matching
    the given evidence -- regardless of whether its `live_sessions`
    inventory (if any) is even readable.

    Used to recognize "this is the caller's OWN launch" even when that
    launch's inventory is broken: a caller must never be silently treated
    as unplaceable, and resolution silently proceed against some OTHER,
    unrelated launch instead, merely because the caller's own launch
    record happens to be corrupt. That must be a refusal, not a fallback.
    """
    pane_id = (pane_id or "").strip()
    if not pane_id:
        return False
    want_terminal = (terminal or "").strip().lower()
    record_terminal = str(record.get("terminal") or "").strip().lower()
    for entry in _get_providers_map(record).values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("pane_id") or "").strip() != pane_id:
            continue
        if want_terminal and record_terminal and want_terminal != record_terminal:
            continue
        return True
    return False


def _operational_refusal(
    record: dict[str, Any],
    session: LiveSession,
    provider: str,
    *,
    project_id: str,
    check_daemon: bool,
) -> Optional[str]:
    """The reason a resolved destination fails an OPERATIONAL check, or
    `None` when it passes all of them.

    Mirrors `resolve_project_runtime_status`'s own per-session inventory
    branch (launcher liveness, real pane liveness, session binding, daemon
    availability, launch policy) so the two never silently drift into
    different definitions of "usable." Finding 6: caller-aware resolution
    supersedes the provider-wide AMBIGUITY verdict only -- it must not also
    let a destination through that fails any of these.
    """
    launcher_alive = _registry_owner_alive(record)
    if launcher_alive is False:
        return "launcher_dead"
    session_record = {
        "providers": {
            provider: {"pane_id": session.pane_id, "pane_title_marker": session.pane_title_marker}
        },
        "terminal": session.terminal or record.get("terminal"),
        "work_dir": session.work_dir or record.get("work_dir"),
    }
    pane_alive = bool(_provider_pane_alive(session_record, provider)) if launcher_alive is not False else False
    if not pane_alive:
        return "pane_dead"
    if not session.session_file:
        return "session_unbound"
    work_dir_hint = Path(session.work_dir or record.get("work_dir") or ".")
    bound, _session_file = _session_bound(
        work_dir_hint, project_id, provider, {"pane_id": session.pane_id, "session_file": session.session_file}
    )
    if not bound:
        return "session_unbound"
    if check_daemon and not is_project_askd_online(work_dir_hint, project_id):
        return "daemon_offline"
    degraded = read_degraded_states(work_dir_hint)
    policy_error = degraded.get(provider) or {}
    policy_reason = str(policy_error.get("reason_code") or "").strip()
    if policy_reason:
        return f"launch_policy_error:{policy_reason}"
    if not session.active:
        return "session_inactive"
    return None


def _resolve_within_launch(
    record: dict[str, Any],
    provider: str,
    *,
    caller: Optional[LiveSession],
    project_id: str,
    check_daemon: bool,
) -> Optional[Tuple[Resolution, Optional[LiveSession]]]:
    """Resolve `provider`'s destination strictly within ONE already-
    identified launch record. Never consults any other record.
    """
    inventory = read_inventory_for_record(record)
    if not inventory.present:
        return None
    if not inventory.valid:
        return Resolution(error="invalid_inventory", detail="live_sessions inventory is invalid"), caller

    resolution = resolve_local_target(inventory.sessions, provider=provider, caller=caller)
    if not resolution.ok:
        return resolution, caller

    # Finding 2, repeated explicitly: resolve_local_target already excludes
    # the caller from candidates -- defeating exactly this property is what
    # a forged caller identity would be FOR, so it is re-asserted here
    # rather than trusted to have been enforced once, upstream.
    if caller is not None and resolution.session.live_id == caller.live_id:
        return Resolution(error=SELF_ONLY, detail="resolved destination is the caller itself"), caller

    reason = _operational_refusal(
        record, resolution.session, provider, project_id=project_id, check_daemon=check_daemon
    )
    if reason:
        return Resolution(
            error=reason,
            detail=f"routed destination failed an operational check: {reason}",
            candidates=(resolution.session.live_id,),
        ), caller

    return resolution, caller


def _resolve_live_route_host(
    provider: str,
    work_dir: str | Path,
    *,
    caller_pane_id: str = "",
    caller_terminal: str = "",
    caller_live_id: str = "",
    caller_token: str = "",
    check_daemon: bool = True,
) -> Optional[Tuple[Resolution, Optional[LiveSession]]]:
    """The real, host-side route resolution: filesystem and (via the
    operational checks) real terminal/daemon state must be visible to run
    this. Never called directly from inside a managed Codex sandbox --
    `resolve_live_route` proxies through the daemon RPC for that case.
    """
    try:
        resolved_work_dir = Path(work_dir).expanduser().resolve()
        project_id = compute_ccb_project_id(resolved_work_dir)
    except Exception:
        return None

    qualifying = list(_iter_qualifying_registry_records(project_id=project_id))
    records = [record for record, _wd, _eff, _ts, _stale in qualifying]
    have_evidence = bool(caller_pane_id or caller_terminal or caller_live_id or caller_token)

    # Finding 3: establish the CALLER'S OWN LAUNCH first, by searching
    # every qualifying record's own inventory for a session matching the
    # supplied evidence -- never a project-wide, provider-selected record.
    caller_matches: list[tuple[dict, LiveSession]] = []
    if have_evidence:
        for record in records:
            inventory = read_inventory_for_record(record)
            if not inventory.valid:
                continue
            found = find_caller(
                inventory.sessions,
                live_id=caller_live_id,
                pane_id=caller_pane_id,
                terminal=caller_terminal,
            )
            if found is not None:
                credential_only = bool(caller_live_id) and not (caller_pane_id or caller_terminal)
                if caller_token:
                    if not found.auth_token or not hmac.compare_digest(found.auth_token, caller_token):
                        continue
                elif credential_only:
                    continue
                caller_matches.append((record, found))

    if len(caller_matches) > 1:
        return Resolution(
            error=AMBIGUOUS,
            detail="caller pane matched sessions in more than one launch",
        ), None

    if caller_matches:
        caller_launch, caller = caller_matches[0]
        return _resolve_within_launch(
            caller_launch, provider, caller=caller, project_id=project_id, check_daemon=check_daemon
        )

    # Whether there is even a launch anywhere in the project the callerless
    # fallback below could match. Computed once, up front, because BOTH
    # branches need it: the evidence-supplied branch needs it to tell
    # "evidence matched nothing, but nothing was on offer anyway" (still
    # legacy) apart from "evidence matched nothing, and something WAS on
    # offer" (must refuse, not silently take that something).
    present_records = [record for record in records if read_inventory_for_record(record).present]

    # No VALID inventory could place the caller. Before falling back,
    # check whether the caller's own launch is identifiable by pane
    # evidence alone, even with a BROKEN inventory -- that must be a
    # refusal, never routing silently through some other, unrelated
    # launch just because this one's inventory happens to be corrupt.
    if have_evidence:
        for record in records:
            if not _record_claims_pane(record, caller_pane_id, caller_terminal):
                continue
            inventory = read_inventory_for_record(record)
            if inventory.present and not inventory.valid:
                return Resolution(
                    error="invalid_inventory",
                    detail="the caller's own launch record has a broken live_sessions inventory",
                ), None
            if not inventory.present:
                # The caller's own launch carries no inventory at all: the
                # legacy case for THIS caller, regardless of what any
                # other launch in the project carries.
                return None
        # Hole 3(b): evidence WAS supplied but matched NOTHING anywhere --
        # a different case from "no evidence was supplied at all", and one
        # that must FAIL rather than silently fall through to the
        # genuinely-no-evidence callerless path, GROUPING the two together
        # is exactly what let unresolvable-but-supplied evidence slip
        # through to a callerless single-destination pick. But this only
        # matters when the callerless path would have had something to
        # pick from at all: when the project carries NO inventory
        # anywhere, routing is genuinely inapplicable regardless of this
        # caller's evidence, and the Phase 1 "no inventory is
        # byte-identical to today" guarantee still governs -- a caller's
        # pane happening to be identifiable (or not) must never be able to
        # turn an ordinary, un-inventoried ask into a refusal.
        if present_records:
            return Resolution(
                error=UNKNOWN_CALLER,
                detail="caller evidence did not match any launch in this project",
            ), None
        return None

    # The ONLY way to reach here is genuinely NO evidence having been
    # supplied at all (have_evidence is False). Inventory-based routing
    # cannot apply without knowing which launch is being asked FROM --
    # fall back only when there is exactly one launch in the project that
    # carries a `live_sessions` key at all (valid or not), since then
    # there is no "wrong launch" to have picked between. A launch whose
    # inventory happens to be broken still counts here (and still refuses
    # below): silently skipping it would let an unrelated, unbroken launch
    # in the same project stand in for it undetected.
    if not present_records:
        return None
    if len(present_records) > 1:
        return Resolution(
            error=AMBIGUOUS,
            detail=f"{len(present_records)} launches carry a live_sessions inventory and the caller is unidentified",
        ), None

    only_record = present_records[0]
    inventory = read_inventory_for_record(only_record)
    if not inventory.valid:
        return Resolution(error="invalid_inventory", detail="live_sessions inventory is invalid"), None

    # Hole 3(a): a present, VALID inventory is AUTHORITATIVE for this
    # launch. Resolve strictly within it regardless of whether it happens
    # to name this provider -- `resolve_local_target` itself refuses
    # cleanly (NOT_MOUNTED) when the provider is absent from it. That
    # refusal must never be softened into `None`, which would send the
    # request on to consult a legacy provider file sitting alongside it.
    return _resolve_within_launch(
        only_record, provider, caller=None, project_id=project_id, check_daemon=check_daemon
    )


def _live_session_to_route_outcome_dict(
    outcome: Optional[Tuple[Resolution, Optional[LiveSession]]],
) -> dict[str, Any]:
    """Serialize a `resolve_live_route` outcome for the RPC boundary."""
    if outcome is None:
        return {"kind": "no_inventory"}
    resolution, caller = outcome
    if not resolution.ok:
        return {
            "kind": "refused",
            "reason": resolution.error,
            "detail": resolution.detail,
            "candidates": list(resolution.candidates),
        }
    session = resolution.session
    return {
        "kind": "route",
        "live_id": session.live_id,
        "launch_id": session.launch_id,
        "provider": session.provider,
        "pane_id": session.pane_id,
        "terminal": session.terminal,
        "work_dir": session.work_dir,
        "ccb_project_id": session.ccb_project_id,
        "session_file": session.session_file,
        "active": session.active,
        "caller_live_id": caller.live_id if caller else "",
    }


def _route_outcome_dict_to_live_session(
    payload: dict[str, Any], provider: str
) -> Optional[Tuple[Resolution, Optional[LiveSession]]]:
    """Inverse of `_live_session_to_route_outcome_dict`."""
    kind = payload.get("kind")
    if kind == "no_inventory":
        return None
    if kind == "refused":
        candidates = payload.get("candidates")
        return Resolution(
            error=str(payload.get("reason") or ""),
            detail=str(payload.get("detail") or ""),
            candidates=tuple(candidates) if isinstance(candidates, list) else (),
        ), None
    if kind == "route":
        session = LiveSession(
            live_id=str(payload.get("live_id") or ""),
            provider=str(payload.get("provider") or provider),
            launch_id=str(payload.get("launch_id") or ""),
            pane_id=str(payload.get("pane_id") or ""),
            terminal=str(payload.get("terminal") or ""),
            work_dir=str(payload.get("work_dir") or ""),
            ccb_project_id=str(payload.get("ccb_project_id") or ""),
            active=bool(payload.get("active", True)),
            session_file=str(payload.get("session_file") or ""),
        )
        caller_live_id = str(payload.get("caller_live_id") or "")
        caller = (
            LiveSession(live_id=caller_live_id, provider=session.provider, launch_id=session.launch_id)
            if caller_live_id
            else None
        )
        return Resolution(session=session), caller
    raise RuntimeError(f"askd returned an unknown route outcome: {kind!r}")


def _daemon_resolve_live_route(
    provider: str,
    work_dir: str | Path,
    *,
    caller_pane_id: str,
    caller_terminal: str,
    caller_live_id: str,
    caller_token: str,
    check_daemon: bool,
) -> Optional[Tuple[Resolution, Optional[LiveSession]]]:
    """Finding 1: a sandboxed caller cannot see real terminal/daemon state
    itself, so route resolution is proxied to the host daemon over the
    SAME authenticated RPC/mailbox transport `_daemon_project_runtime_
    status` already uses -- never a local bypass. The daemon runs
    `_resolve_live_route_host` directly (unsandboxed) and returns one of
    exactly three outcomes: an exact validated route, an explicit refusal,
    or a genuinely verified "no inventory" legacy case.
    """
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
        "id": f"resolve-route-{os.getpid()}",
        "token": token,
        "operation": "resolve_route",
        "work_dir": str(work_dir),
        "provider": provider,
        "caller_pane_id": caller_pane_id,
        "caller_terminal": caller_terminal,
        "caller_live_id": caller_live_id,
        "caller_token": caller_token,
        "check_daemon": check_daemon,
    }
    response = askd_rpc.request_daemon(
        state,
        request,
        connect_timeout_s=2.0,
        response_timeout_s=8.0,
    )
    if response.get("type") != "ask.response" or int(response.get("exit_code", 1)) != 0:
        raise RuntimeError(str(response.get("reply") or "askd rejected route resolution"))
    payload = response.get("route_outcome")
    if not isinstance(payload, dict):
        raise RuntimeError("askd returned an invalid route outcome")
    return _route_outcome_dict_to_live_session(payload, provider)


def resolve_live_route(
    provider: str,
    work_dir: str | Path,
    *,
    caller_pane_id: str = "",
    caller_terminal: str = "",
    caller_live_id: str = "",
    caller_token: str = "",
    check_daemon: bool = True,
    _allow_daemon_proxy: bool = True,
) -> Optional[Tuple[Resolution, Optional[LiveSession]]]:
    """Resolve the exact live-session destination `provider` names for this
    caller.

    Returns `None` when NO launch relevant to this caller/provider carries
    a `live_sessions` inventory at all: there is nothing to resolve, and
    the caller (`bin/ask`'s `_preflight_target`) must fall through to
    today's un-routed, per-provider status check untouched -- the "no
    inventory" case every touched sending path must leave byte-identical.

    A present-but-broken inventory, an unidentifiable caller, a
    self-route, or a destination that fails an operational check all come
    back as a `Resolution` refusal instead of `None`, so a caller can tell
    "not applicable" apart from "applicable and refused" and must never
    treat the latter as the former.

    Inside a managed Codex sandbox this proxies to the host daemon over
    RPC (Finding 1) rather than resolving locally or bypassing -- see
    `_daemon_resolve_live_route`. Resolution is always scoped to the
    CALLER'S OWN launch record (Finding 3) and, once a destination is
    picked, still subject to the same operational checks `resolve_project_
    runtime_status` would run (Finding 6): caller-awareness supersedes the
    provider-wide ambiguity verdict only.
    """
    if _allow_daemon_proxy and inside_managed_codex_sandbox():
        return _daemon_resolve_live_route(
            provider,
            work_dir,
            caller_pane_id=caller_pane_id,
            caller_terminal=caller_terminal,
            caller_live_id=caller_live_id,
            caller_token=caller_token,
            check_daemon=check_daemon,
        )
    return _resolve_live_route_host(
        provider,
        work_dir,
        caller_pane_id=caller_pane_id,
        caller_terminal=caller_terminal,
        caller_live_id=caller_live_id,
        caller_token=caller_token,
        check_daemon=check_daemon,
    )
