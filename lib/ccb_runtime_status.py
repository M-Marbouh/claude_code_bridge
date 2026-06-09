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
from providers import make_qualified_key, normalize_instance_name, parse_qualified_provider, session_filename_for_instance
from session_utils import find_project_session_file


CAPABLE_MULTI_INSTANCE = True
CAPABLE_TAG_ROUTING = True

_SESSION_FILENAMES = {
    "codex": ".codex-session",
    "gemini": ".gemini-session",
    "opencode": ".opencode-session",
    "claude": ".claude-session",
    "droid": ".droid-session",
    "copilot": ".copilot-session",
    "codebuddy": ".codebuddy-session",
    "qwen": ".qwen-session",
}


@dataclass(frozen=True)
class RegistryProviderRecord:
    project_id: str
    work_dir: str
    provider_key: str
    provider_entry: dict[str, Any]
    registry_record: dict[str, Any]
    updated_at: int
    timestamp_stale: bool


@dataclass(frozen=True)
class ProviderRuntimeStatus:
    key: str
    provider: str
    instance: str | None
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
            "instance": self.instance,
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
    project_hash = (project_id or "")[:16] or "unknown"
    return Path.home() / ".cache" / "ccb" / "projects" / project_hash


def _state_file_candidates(project_id: str) -> list[Path]:
    candidates: list[Path] = []
    raw_run_dir = (os.environ.get("CCB_RUN_DIR") or "").strip()
    if raw_run_dir:
        candidates.append(Path(raw_run_dir).expanduser() / "askd.json")
    candidates.append(_project_run_dir(project_id) / "askd.json")
    seen: set[str] = set()
    out: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _state_matches_project(state: dict[str, Any] | None, work_dir: Path, project_id: str) -> bool:
    if not isinstance(state, dict):
        return False
    raw_work_dir = str(state.get("work_dir") or "").strip()
    if raw_work_dir:
        try:
            return compute_ccb_project_id(Path(raw_work_dir)) == project_id
        except Exception:
            return False
    return True


def is_project_askd_online(work_dir: Path, project_id: str, *, timeout_s: float = 0.2) -> bool:
    for state_file in _state_file_candidates(project_id):
        state = askd_rpc.read_state(state_file)
        if not _state_matches_project(state, work_dir, project_id):
            continue
        if askd_rpc.ping_daemon("ask", timeout_s=timeout_s, state_file=state_file):
            return True
    return False


def _effective_project_id(record: dict[str, Any]) -> str:
    existing = str(record.get("ccb_project_id") or "").strip()
    if existing:
        return existing
    work_dir = str(record.get("work_dir") or "").strip()
    if not work_dir:
        return ""
    try:
        return compute_ccb_project_id(Path(work_dir))
    except Exception:
        return ""


def iter_registry_provider_records(
    *,
    project_id: str | None = None,
    include_stale: bool = False,
) -> list[RegistryProviderRecord]:
    records: list[RegistryProviderRecord] = []
    for path in _iter_registry_files():
        record = _load_registry_file(path)
        if not record:
            continue
        updated_at = _coerce_updated_at(record.get("updated_at"), path)
        timestamp_stale = _is_stale(updated_at)
        if timestamp_stale and not include_stale:
            continue
        effective_project_id = _effective_project_id(record)
        if not effective_project_id:
            continue
        if project_id and effective_project_id != project_id:
            continue
        work_dir = str(record.get("work_dir") or "").strip()
        if not work_dir:
            continue
        providers = _get_providers_map(record)
        for provider_key, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            records.append(
                RegistryProviderRecord(
                    project_id=effective_project_id,
                    work_dir=work_dir,
                    provider_key=str(provider_key).strip().lower(),
                    provider_entry=dict(entry),
                    registry_record=record,
                    updated_at=updated_at,
                    timestamp_stale=timestamp_stale,
                )
            )
    return records


def _configured_provider_keys(work_dir: Path) -> set[str]:
    try:
        config = load_start_config(work_dir).data
    except Exception:
        config = {}
    keys: set[str] = set()
    providers = config.get("providers") if isinstance(config, dict) else None
    if isinstance(providers, list):
        for item in providers:
            base, _instance = parse_qualified_provider(str(item or ""))
            if base:
                keys.add(base)
    instances = config.get("provider_instances") if isinstance(config, dict) else None
    if isinstance(instances, list):
        for item in instances:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "").strip().lower()
            instance = normalize_instance_name(str(item.get("instance") or ""))
            if provider and instance:
                keys.add(make_qualified_key(provider, instance))
    return keys


def _select_provider_records(records: Iterable[RegistryProviderRecord]) -> dict[str, RegistryProviderRecord]:
    selected: dict[str, RegistryProviderRecord] = {}
    for record in records:
        current = selected.get(record.provider_key)
        if current is None:
            selected[record.provider_key] = record
            continue
        if current.timestamp_stale and not record.timestamp_stale:
            selected[record.provider_key] = record
            continue
        if current.timestamp_stale == record.timestamp_stale and record.updated_at > current.updated_at:
            selected[record.provider_key] = record
    return selected


def _session_filename_for_key(provider: str, instance: str | None) -> str:
    base = _SESSION_FILENAMES.get(provider, f".{provider}-session")
    return session_filename_for_instance(base, instance)


def _load_json_dict(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _session_file_from_entry(work_dir: Path, provider: str, instance: str | None, entry: dict[str, Any]) -> Path | None:
    raw = str(entry.get("session_file") or "").strip()
    if raw:
        return Path(raw).expanduser()
    filename = _session_filename_for_key(provider, instance)
    return find_project_session_file(work_dir, filename)


def _session_bound(
    *,
    work_dir: Path,
    project_id: str,
    key: str,
    provider: str,
    instance: str | None,
    entry: dict[str, Any],
) -> tuple[bool, str]:
    session_file = _session_file_from_entry(work_dir, provider, instance, entry)
    if not session_file or not session_file.exists() or not session_file.is_file():
        return False, str(session_file or "")
    data = _load_json_dict(session_file)
    if not data:
        return False, str(session_file)

    recorded_project = str(data.get("ccb_project_id") or "").strip()
    if recorded_project and recorded_project != project_id:
        return False, str(session_file)

    recorded_key = str(data.get("qualified_provider") or "").strip().lower()
    if recorded_key and recorded_key != key:
        return False, str(session_file)

    recorded_provider = str(data.get("provider") or "").strip().lower()
    if recorded_provider and recorded_provider != provider:
        return False, str(session_file)

    recorded_instance = normalize_instance_name(str(data.get("instance") or ""))
    if recorded_instance != normalize_instance_name(instance):
        return False, str(session_file)

    entry_pane = str(entry.get("pane_id") or "").strip()
    session_pane = str(data.get("pane_id") or data.get("tmux_session") or "").strip()
    if entry_pane and session_pane and entry_pane != session_pane:
        return False, str(session_file)

    return True, str(session_file)


def _status_reason(
    *,
    configured: bool,
    registered: bool,
    timestamp_stale: bool,
    pane_alive: bool,
    session_bound: bool,
    daemon_online: bool,
) -> str:
    if registered and not timestamp_stale and pane_alive and session_bound and daemon_online:
        return ""
    if not configured and not registered:
        return "not_configured"
    if not registered:
        return "not_registered"
    if timestamp_stale:
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
    resolved_work_dir: Path | None = None
    if work_dir is not None:
        try:
            resolved_work_dir = Path(work_dir).expanduser().resolve()
        except Exception:
            resolved_work_dir = Path(work_dir).expanduser().absolute()
    if not project_id and resolved_work_dir is not None:
        try:
            project_id = compute_ccb_project_id(resolved_work_dir)
        except Exception:
            project_id = ""
    project_id = (project_id or "").strip()

    records = iter_registry_provider_records(project_id=project_id or None, include_stale=include_stale)
    if resolved_work_dir is None and records:
        try:
            resolved_work_dir = Path(records[0].work_dir).expanduser().resolve()
        except Exception:
            resolved_work_dir = Path(records[0].work_dir).expanduser().absolute()
    if resolved_work_dir is None:
        resolved_work_dir = Path.cwd().resolve()
    if not project_id:
        try:
            project_id = compute_ccb_project_id(resolved_work_dir)
        except Exception:
            project_id = ""
    if project_id:
        records = [record for record in records if record.project_id == project_id]

    configured_keys = _configured_provider_keys(resolved_work_dir)
    selected_records = _select_provider_records(records)
    keys = set(configured_keys) | set(selected_records)

    daemon_online = is_project_askd_online(resolved_work_dir, project_id) if check_daemon and project_id else False
    provider_statuses: dict[str, ProviderRuntimeStatus] = {}

    for key in sorted(keys):
        provider, instance = parse_qualified_provider(key)
        record = selected_records.get(key)
        entry = record.provider_entry if record else {}
        registered = record is not None
        timestamp_stale = bool(record.timestamp_stale) if record else False
        pane_alive = bool(_provider_pane_alive(record.registry_record, key)) if record else False
        session_ok, session_file = _session_bound(
            work_dir=resolved_work_dir,
            project_id=project_id,
            key=key,
            provider=provider,
            instance=instance,
            entry=entry,
        ) if record else (False, "")
        mounted = bool(registered and not timestamp_stale and pane_alive and session_ok and daemon_online)
        reason = _status_reason(
            configured=(key in configured_keys),
            registered=registered,
            timestamp_stale=timestamp_stale,
            pane_alive=pane_alive,
            session_bound=session_ok,
            daemon_online=daemon_online,
        )
        provider_statuses[key] = ProviderRuntimeStatus(
            key=key,
            provider=provider,
            instance=instance,
            capable=CAPABLE_MULTI_INSTANCE and CAPABLE_TAG_ROUTING,
            configured=(key in configured_keys),
            registered=registered,
            pane_alive=pane_alive,
            session_bound=session_ok,
            daemon_online=daemon_online,
            mounted=mounted,
            reason=reason,
            pane_id=str(entry.get("pane_id") or "").strip(),
            pane_title_marker=str(entry.get("pane_title_marker") or "").strip(),
            session_file=session_file,
            timestamp_stale=timestamp_stale,
            updated_at=int(record.updated_at) if record else 0,
        )

    terminal = ""
    updated_at = 0
    if records:
        newest = max(records, key=lambda record: record.updated_at)
        terminal = str(newest.registry_record.get("terminal") or "").strip()
        updated_at = int(newest.updated_at)
    return ProjectRuntimeStatus(
        work_dir=str(resolved_work_dir),
        ccb_project_id=project_id,
        terminal=terminal or "tmux",
        updated_at=updated_at,
        providers=provider_statuses,
    )


def list_project_runtime_statuses(*, include_stale: bool = False, check_daemon: bool = True) -> list[ProjectRuntimeStatus]:
    records = iter_registry_provider_records(include_stale=include_stale)
    grouped: dict[str, list[RegistryProviderRecord]] = {}
    for record in records:
        grouped.setdefault(record.project_id, []).append(record)

    projects: list[ProjectRuntimeStatus] = []
    for project_id, project_records in grouped.items():
        newest = max(project_records, key=lambda record: record.updated_at)
        work_dir = newest.work_dir
        try:
            if not Path(work_dir).expanduser().exists():
                continue
        except Exception:
            continue
        projects.append(
            resolve_project_runtime_status(
                work_dir,
                project_id=project_id,
                include_stale=include_stale,
                check_daemon=check_daemon,
            )
        )
    projects.sort(key=lambda project: project.updated_at, reverse=True)
    return projects


def provider_status_for_target(
    target: str,
    *,
    work_dir: str | Path | None = None,
    include_stale: bool = False,
    check_daemon: bool = True,
) -> ProviderRuntimeStatus:
    base, instance = parse_qualified_provider(target)
    key = make_qualified_key(base, instance)
    project = resolve_project_runtime_status(work_dir or Path.cwd(), include_stale=include_stale, check_daemon=check_daemon)
    status = project.providers.get(key)
    if status:
        return status
    return ProviderRuntimeStatus(
        key=key,
        provider=base,
        instance=instance,
        capable=CAPABLE_MULTI_INSTANCE and CAPABLE_TAG_ROUTING,
        configured=False,
        registered=False,
        pane_alive=False,
        session_bound=False,
        daemon_online=any(s.daemon_online for s in project.providers.values()),
        mounted=False,
        reason="not_configured",
    )
