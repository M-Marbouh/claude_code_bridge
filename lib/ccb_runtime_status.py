from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import askd_rpc
from ccb_start_config import load_start_config
from pane_registry import (
    _coerce_updated_at,
    _get_providers_map,
    _is_stale,
    _iter_registry_files,
    _load_registry_file,
    _provider_pane_alive,
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
        }


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


def _project_run_dir(project_id: str) -> Path:
    return Path.home() / ".cache" / "ccb" / "projects" / ((project_id or "")[:16] or "unknown")


def _state_file_candidates(project_id: str) -> list[Path]:
    candidates: list[Path] = []
    override = (os.environ.get("CCB_RUN_DIR") or "").strip()
    if override:
        candidates.append(Path(override).expanduser() / "askd.json")
    candidates.append(_project_run_dir(project_id) / "askd.json")
    return list(dict.fromkeys(candidates))


def is_project_askd_online(work_dir: Path, project_id: str, *, timeout_s: float = 0.2) -> bool:
    for state_file in _state_file_candidates(project_id):
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
        if askd_rpc.ping_daemon("ask", timeout_s=timeout_s, state_file=state_file):
            return True
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


def iter_registry_provider_records(*, project_id: str | None = None, include_stale: bool = False) -> list[RegistryProviderRecord]:
    records: list[RegistryProviderRecord] = []
    for path in _iter_registry_files():
        record = _load_registry_file(path)
        if not record:
            continue
        updated_at = _coerce_updated_at(record.get("updated_at"), path)
        timestamp_stale = _is_stale(updated_at)
        if timestamp_stale and not include_stale:
            continue
        effective = _effective_project_id(record)
        if not effective or (project_id and effective != project_id):
            continue
        work_dir = str(record.get("work_dir") or "").strip()
        if not work_dir:
            continue
        for provider, entry in _get_providers_map(record).items():
            if provider not in SUPPORTED_PROVIDERS or not isinstance(entry, dict):
                continue
            records.append(RegistryProviderRecord(effective, work_dir, provider, dict(entry), record, updated_at, timestamp_stale))
    return records


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


def _reason(configured: bool, registered: bool, stale: bool, pane_alive: bool, session_bound: bool, daemon_online: bool) -> str:
    if registered and not stale and pane_alive and session_bound and daemon_online:
        return ""
    if not configured and not registered:
        return "not_configured"
    if not registered:
        return "not_registered"
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
) -> ProjectRuntimeStatus:
    resolved = Path(work_dir or Path.cwd()).expanduser().resolve()
    project_id = (project_id or compute_ccb_project_id(resolved)).strip()
    records = iter_registry_provider_records(project_id=project_id, include_stale=include_stale)
    configured = _configured_providers(resolved)
    selected = _select_records(records)
    daemon_online = is_project_askd_online(resolved, project_id) if check_daemon else False
    statuses: dict[str, ProviderRuntimeStatus] = {}
    for provider in sorted(configured | set(selected)):
        record = selected.get(provider)
        entry = record.provider_entry if record else {}
        registered = record is not None
        stale = bool(record.timestamp_stale) if record else False
        pane_alive = bool(_provider_pane_alive(record.registry_record, provider)) if record else False
        bound, session_file = _session_bound(resolved, project_id, provider, entry) if record else (False, "")
        mounted = bool(registered and not stale and pane_alive and bound and daemon_online)
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
            reason=_reason(provider in configured, registered, stale, pane_alive, bound, daemon_online),
            pane_id=str(entry.get("pane_id") or "").strip(),
            pane_title_marker=str(entry.get("pane_title_marker") or "").strip(),
            session_file=session_file,
            timestamp_stale=stale,
            updated_at=record.updated_at if record else 0,
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
