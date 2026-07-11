from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cli_output import atomic_write_text
from project_id import compute_ccb_project_id


RECEIPT_SCHEMA_VERSION = 1


def task_dir() -> Path:
    return Path(tempfile.gettempdir()) / "ccb-tasks"


def receipt_path(provider: str, task_id: str, *, root: Path | None = None) -> Path:
    safe_provider = (provider or "").strip().lower()
    safe_task = (task_id or "").strip()
    return (root or task_dir()) / f"ask-{safe_provider}-{safe_task}.json"


def caller_pane() -> tuple[str, str]:
    explicit = (os.environ.get("CCB_CALLER_PANE_ID") or "").strip()
    explicit_terminal = (os.environ.get("CCB_CALLER_TERMINAL") or "").strip()
    if explicit:
        return explicit, explicit_terminal
    wezterm = (os.environ.get("WEZTERM_PANE") or "").strip()
    if wezterm:
        return wezterm, "wezterm"
    tmux = (os.environ.get("TMUX_PANE") or "").strip()
    if tmux:
        return tmux, "tmux"
    return "", ""


def caller_session_id() -> str:
    return (os.environ.get("CCB_SESSION_ID") or "").strip()


def new_receipt(
    *,
    task_id: str,
    provider: str,
    caller: str,
    work_dir: Path,
    status_file: Path,
    log_file: Path,
) -> dict[str, Any]:
    pane_id, terminal = caller_pane()
    try:
        project_id = compute_ccb_project_id(work_dir)
    except Exception:
        project_id = ""
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "task_id": task_id,
        "provider": provider,
        "caller": caller,
        "caller_session_id": caller_session_id(),
        "caller_pane_id": pane_id,
        "caller_terminal": terminal,
        "work_dir": str(work_dir),
        "ccb_project_id": project_id,
        "status_file": str(status_file),
        "log_file": str(log_file),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


def write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")


def load_receipt(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def iter_receipts(*, root: Path | None = None) -> Iterable[tuple[Path, dict[str, Any]]]:
    directory = root or task_dir()
    if not directory.is_dir():
        return []
    records: list[tuple[Path, dict[str, Any]]] = []
    for path in directory.glob("ask-*.json"):
        data = load_receipt(path)
        if data:
            records.append((path, data))
    records.sort(
        key=lambda item: str(item[1].get("submitted_at") or item[0].name),
        reverse=True,
    )
    return records


def find_receipt(task_id: str, *, root: Path | None = None) -> tuple[Path, dict[str, Any]] | None:
    wanted = (task_id or "").strip()
    if not wanted:
        return None
    matches = [(path, data) for path, data in iter_receipts(root=root) if data.get("task_id") == wanted]
    return matches[0] if len(matches) == 1 else None
