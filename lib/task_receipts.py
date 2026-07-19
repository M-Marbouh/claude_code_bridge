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
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    pane_id, terminal = caller_pane()
    try:
        project_id = compute_ccb_project_id(work_dir)
    except Exception:
        project_id = ""
    receipt = {
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
    if timeout_seconds is not None:
        receipt["timeout_seconds"] = float(timeout_seconds)
    return receipt


def new_peer_receipt(
    *,
    task_id: str,
    peer_provider: str,
    caller: str,
    intent: str,
    work_dir: Path,
    status_file: Path,
    log_file: Path,
    reply_file: Path,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Create a peer receipt with a validated reverse-route snapshot."""
    receipt = new_receipt(
        task_id=task_id,
        provider=f"peer-{peer_provider}",
        caller=caller,
        work_dir=work_dir,
        status_file=status_file,
        log_file=log_file,
        timeout_seconds=timeout_seconds,
    )
    receipt.update(
        {
            "peer_provider": peer_provider,
            "peer_intent": intent,
            "reply_expected": intent != "notify",
            "peer_reply_file": str(reply_file),
        }
    )

    pane_id = str(receipt.get("caller_pane_id") or "").strip()
    project_id = str(receipt.get("ccb_project_id") or "").strip()
    if not pane_id or not project_id or caller not in {"claude", "codex"}:
        return receipt

    try:
        from pane_registry import load_registry_by_project_id

        record = load_registry_by_project_id(project_id, caller)
    except Exception:
        record = None
    if isinstance(record, dict):
        providers = record.get("providers")
        provider_data = providers.get(caller) if isinstance(providers, dict) else None
        if (
            isinstance(provider_data, dict)
            and str(provider_data.get("pane_id") or "").strip() == pane_id
        ):
            marker = str(provider_data.get("pane_title_marker") or "").strip()
            if marker:
                receipt["caller_pane_title_marker"] = marker
            registry_session_id = str(record.get("ccb_session_id") or "").strip()
            if registry_session_id:
                receipt["caller_registry_session_id"] = registry_session_id
                if not receipt.get("caller_session_id"):
                    receipt["caller_session_id"] = registry_session_id

    if not receipt.get("caller_pane_title_marker"):
        terminal = str(receipt.get("caller_terminal") or "").strip().lower()
        display = "Codex" if caller == "codex" else "Claude"
        marker = f"CCB-{display}-{project_id[:8]}"
        try:
            from terminal import get_backend_for_session

            backend = get_backend_for_session({"terminal": terminal})
            resolver = getattr(backend, "find_pane_by_title_marker", None)
            cwd_check = getattr(backend, "pane_matches_cwd_strict", None)
            marker_pane = str(resolver(marker, str(work_dir)) or "").strip() if callable(resolver) else ""
            cwd_matches = bool(cwd_check(pane_id, str(work_dir))) if callable(cwd_check) else False
            pane_alive = bool(backend and backend.is_alive(pane_id))
        except Exception:
            marker_pane = ""
            cwd_matches = False
            pane_alive = False
        if pane_alive and cwd_matches and marker_pane == pane_id:
            receipt["caller_pane_title_marker"] = marker
    return receipt


def write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")


def load_receipt(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def peer_reply_path(receipt: dict[str, Any]) -> Path | None:
    raw = str(receipt.get("peer_reply_file") or "").strip()
    return Path(raw) if raw else None


def read_peer_reply(receipt: dict[str, Any]) -> str:
    path = peer_reply_path(receipt)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace").strip()
    except Exception:
        return ""


def write_peer_reply(receipt: dict[str, Any], reply: str) -> None:
    path = peer_reply_path(receipt)
    if path is None:
        raise ValueError("peer receipt has no reply file")
    body = (reply or "").strip()
    if not body:
        raise ValueError("peer reply cannot be empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, body + "\n")


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
