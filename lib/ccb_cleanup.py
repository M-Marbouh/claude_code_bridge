from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pane_registry import _coerce_updated_at, _get_providers_map, _provider_pane_alive, _record_ccb_pid
from project_id import compute_ccb_project_id


DEFAULT_KEEP_NEWEST = 5
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
SESSION_RECORD_GLOB = "ccb-session-ai-*.json"

LivenessFunc = Callable[[dict[str, Any], str], bool | None]
ProcessAliveFunc = Callable[[int], bool | None]


@dataclass(frozen=True)
class PruneRecord:
    path: str
    project_id: str
    updated_at: int
    age_seconds: int
    reason: str
    provider_keys: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "project_id": self.project_id,
            "updated_at": self.updated_at,
            "age_seconds": self.age_seconds,
            "reason": self.reason,
            "provider_keys": list(self.provider_keys),
        }


@dataclass(frozen=True)
class PrunePlan:
    keep: int
    older_than_seconds: int
    project: str | None
    run_dir: str
    scanned: int
    kept: list[PruneRecord]
    prunable: list[PruneRecord]
    summary: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "keep": self.keep,
            "older_than_seconds": self.older_than_seconds,
            "project": self.project,
            "run_dir": self.run_dir,
            "scanned": self.scanned,
            "kept": [record.to_dict() for record in self.kept],
            "prunable": [record.to_dict() for record in self.prunable],
            "summary": dict(self.summary),
        }


@dataclass(frozen=True)
class PruneResult:
    dry_run: bool
    scanned: int
    planned: int
    kept: int
    deleted: int
    failed: int
    missing: int
    failures: list[dict[str, str]]
    plan: PrunePlan

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "scanned": self.scanned,
            "planned": self.planned,
            "kept": self.kept,
            "deleted": self.deleted,
            "failed": self.failed,
            "missing": self.missing,
            "failures": list(self.failures),
            "plan": self.plan.to_dict(),
        }


def _default_run_dir() -> Path:
    return Path.home() / ".ccb" / "run"


def _record_project_id(data: dict[str, Any]) -> str:
    existing = str(data.get("ccb_project_id") or "").strip()
    if existing:
        return existing
    work_dir = str(data.get("work_dir") or "").strip()
    if not work_dir:
        return ""
    try:
        return compute_ccb_project_id(Path(work_dir))
    except Exception:
        return ""


def _project_filter_id(project: str | Path | None) -> str | None:
    if project is None:
        return None
    raw = str(project).strip()
    if not raw:
        return None
    path_like = isinstance(project, Path) or "/" in raw or "\\" in raw or raw in {".", ".."} or raw.startswith("~")
    if path_like:
        try:
            return compute_ccb_project_id(Path(raw).expanduser())
        except Exception:
            return raw
    return raw


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def _has_provider_identity(entry: dict[str, Any]) -> bool:
    for key in ("pane_id", "pane_title_marker", "tmux_session"):
        if str(entry.get(key) or "").strip():
            return True
    return False


def _default_liveness(record: dict[str, Any], provider_key: str) -> bool | None:
    providers = _get_providers_map(record)
    entry = providers.get(provider_key)
    if not isinstance(entry, dict):
        return None
    if not _has_provider_identity(entry):
        return None

    terminal = str(record.get("terminal") or "tmux").strip().lower() or "tmux"
    if terminal == "tmux" and not (shutil.which("tmux") or shutil.which("tmux.exe")):
        return None
    if terminal == "wezterm" and not shutil.which("wezterm"):
        return None

    try:
        return bool(_provider_pane_alive(record, provider_key))
    except Exception:
        return None


def _liveness_reason(
    record: dict[str, Any],
    provider_keys: list[str],
    liveness_func: LivenessFunc,
) -> str:
    if not provider_keys:
        return "liveness_unknown"

    saw_unknown = False
    for key in provider_keys:
        try:
            alive = liveness_func(record, key)
        except Exception:
            alive = None
        if alive is True:
            return "live"
        if alive is None:
            saw_unknown = True

    if saw_unknown:
        return "liveness_unknown"
    return "dead"


def _default_process_alive(pid: int) -> bool | None:
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def _running_ccb_reason(data: dict[str, Any], process_alive_func: ProcessAliveFunc) -> str:
    pid = _record_ccb_pid(data)
    if not pid:
        return "no_pid"
    try:
        alive = process_alive_func(pid)
    except Exception:
        alive = None
    if alive is True:
        return "running_ccb"
    if alive is None:
        return "process_unknown"
    return "dead"


def _make_record(path: Path, data: dict[str, Any], *, project_id: str, now: int, reason: str) -> PruneRecord:
    updated_at = _coerce_updated_at(data.get("updated_at"), path)
    providers = sorted(_get_providers_map(data))
    return PruneRecord(
        path=str(path),
        project_id=project_id,
        updated_at=updated_at,
        age_seconds=max(0, int(now) - int(updated_at or 0)),
        reason=reason,
        provider_keys=providers,
    )


def _summary(kept: list[PruneRecord], prunable: list[PruneRecord], scanned: int) -> dict[str, int]:
    counts: dict[str, int] = {
        "scanned": scanned,
        "kept": len(kept),
        "prunable": len(prunable),
        "skipped_live": 0,
        "skipped_unknown": 0,
        "skipped_running": 0,
        "skipped_keep_newest": 0,
        "skipped_not_old_enough": 0,
    }
    for record in kept:
        if record.reason == "live":
            counts["skipped_live"] += 1
        elif record.reason == "liveness_unknown":
            counts["skipped_unknown"] += 1
        elif record.reason == "running_ccb":
            counts["skipped_running"] += 1
        elif record.reason == "keep_newest":
            counts["skipped_keep_newest"] += 1
        elif record.reason == "not_old_enough":
            counts["skipped_not_old_enough"] += 1
    return counts


def plan_prune(
    *,
    keep: int = DEFAULT_KEEP_NEWEST,
    older_than_seconds: int = DEFAULT_TTL_SECONDS,
    project: str | Path | None = None,
    run_dir: str | Path | None = None,
    liveness_func: LivenessFunc | None = None,
    process_alive_func: ProcessAliveFunc | None = None,
    now: int | None = None,
) -> PrunePlan:
    keep_count = max(0, int(keep))
    ttl_seconds = max(0, int(older_than_seconds))
    resolved_run_dir = Path(run_dir).expanduser() if run_dir is not None else _default_run_dir()
    now_ts = int(time.time()) if now is None else int(now)
    target_project = _project_filter_id(project)
    check_liveness = liveness_func or _default_liveness
    check_process = process_alive_func or _default_process_alive

    grouped: dict[str, list[tuple[Path, dict[str, Any], int]]] = {}
    kept: list[PruneRecord] = []
    scanned = 0

    if not resolved_run_dir.is_dir():
        return PrunePlan(
            keep=keep_count,
            older_than_seconds=ttl_seconds,
            project=target_project,
            run_dir=str(resolved_run_dir),
            scanned=0,
            kept=[],
            prunable=[],
            summary=_summary([], [], 0),
        )

    for path in sorted(resolved_run_dir.glob(SESSION_RECORD_GLOB)):
        if not path.is_file():
            continue
        data = _load_json_dict(path)
        if data is None:
            scanned += 1
            kept.append(
                PruneRecord(
                    path=str(path),
                    project_id="",
                    updated_at=_coerce_updated_at(None, path),
                    age_seconds=0,
                    reason="unreadable",
                    provider_keys=[],
                )
            )
            continue
        project_id = _record_project_id(data)
        if not project_id:
            scanned += 1
            kept.append(_make_record(path, data, project_id="", now=now_ts, reason="missing_project"))
            continue
        if target_project and project_id != target_project:
            continue
        scanned += 1
        updated_at = _coerce_updated_at(data.get("updated_at"), path)
        grouped.setdefault(project_id, []).append((path, data, updated_at))

    prunable: list[PruneRecord] = []
    for project_id, records in grouped.items():
        ordered = sorted(records, key=lambda item: (item[2], item[0].name), reverse=True)
        for index, (path, data, updated_at) in enumerate(ordered):
            if index < keep_count:
                kept.append(_make_record(path, data, project_id=project_id, now=now_ts, reason="keep_newest"))
                continue
            age_seconds = now_ts - int(updated_at or 0)
            if age_seconds <= ttl_seconds:
                kept.append(_make_record(path, data, project_id=project_id, now=now_ts, reason="not_old_enough"))
                continue
            process_reason = _running_ccb_reason(data, check_process)
            if process_reason == "running_ccb":
                kept.append(_make_record(path, data, project_id=project_id, now=now_ts, reason="running_ccb"))
                continue
            if process_reason == "process_unknown":
                kept.append(_make_record(path, data, project_id=project_id, now=now_ts, reason="liveness_unknown"))
                continue
            provider_keys = sorted(_get_providers_map(data))
            live_reason = _liveness_reason(data, provider_keys, check_liveness)
            if live_reason != "dead":
                kept.append(_make_record(path, data, project_id=project_id, now=now_ts, reason=live_reason))
                continue
            prunable.append(_make_record(path, data, project_id=project_id, now=now_ts, reason="stale_dead"))

    kept.sort(key=lambda record: record.path)
    prunable.sort(key=lambda record: record.path)
    return PrunePlan(
        keep=keep_count,
        older_than_seconds=ttl_seconds,
        project=target_project,
        run_dir=str(resolved_run_dir),
        scanned=scanned,
        kept=kept,
        prunable=prunable,
        summary=_summary(kept, prunable, scanned),
    )


def _planned_path_is_safe(path: Path, run_dir: Path) -> bool:
    if not path.name.startswith("ccb-session-ai-") or path.suffix != ".json":
        return False
    try:
        return path.resolve().parent == run_dir.resolve()
    except Exception:
        return False


def run_prune(plan: PrunePlan, *, dry_run: bool = False) -> PruneResult:
    run_dir = Path(plan.run_dir).expanduser()
    deleted = 0
    failed = 0
    missing = 0
    failures: list[dict[str, str]] = []

    if not dry_run:
        for record in plan.prunable:
            path = Path(record.path)
            if not _planned_path_is_safe(path, run_dir):
                failed += 1
                failures.append({"path": str(path), "error": "unsafe_path"})
                continue
            try:
                path.unlink()
                deleted += 1
            except FileNotFoundError:
                missing += 1
            except Exception as exc:
                failed += 1
                failures.append({"path": str(path), "error": str(exc)})

    return PruneResult(
        dry_run=bool(dry_run),
        scanned=plan.scanned,
        planned=len(plan.prunable),
        kept=len(plan.kept),
        deleted=deleted,
        failed=failed,
        missing=missing,
        failures=failures,
        plan=plan,
    )
