from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from cli_output import atomic_write_text
from project_id import compute_ccb_project_id
from askd.adapters.base import ResolvedRoute
from completion_hook import COMPLETION_STATUS_COMPLETED as _COMPLETION_STATUS_COMPLETED


RECEIPT_SCHEMA_VERSION = 1
PEER_DELIVERY_CONFIRMATIONS = {
    "cancelled",
    "failed",
    "observed",
    "sent",
}


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


def _identity_fields_from_route(route: Optional[ResolvedRoute]) -> dict[str, Any]:
    """The exact-identity block Task 1 adds to a receipt, additively.

    Deliberately EMPTY (no keys at all) unless `route` is a `ResolvedRoute`
    that was actually resolved (`route.present`): a receipt created for a
    request that never had a route (no inventory, legacy path, email/manual
    caller, notify-only) must come out byte-identical to a receipt created
    before this function existed. Nothing here ever infers identity from
    anything else on the receipt -- it only ever copies what the route
    already proved.
    """
    if route is None or not route.present:
        return {}
    fields = {
        "route_launch_id": route.launch_id,
        "destination_live_id": route.live_id,
        "destination_pane_id": route.pane_id,
        "destination_terminal": route.terminal,
        "destination_session_file": route.session_file,
        "destination_ccb_project_id": route.ccb_project_id,
        "caller_live_id": route.caller_live_id,
    }
    if route.caller_pane_id:
        fields["caller_pane_id"] = route.caller_pane_id
    if route.caller_terminal:
        fields["caller_terminal"] = route.caller_terminal
    return fields


def new_receipt(
    *,
    task_id: str,
    provider: str,
    caller: str,
    work_dir: Path,
    status_file: Path,
    log_file: Path,
    timeout_seconds: float | None = None,
    route: Optional[ResolvedRoute] = None,
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
    receipt.update(_identity_fields_from_route(route))
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

    # Inventory topology is authoritative, including an empty or broken
    # inventory. Never invent a provider-wide return marker for a pair.
    try:
        from peer_routing import _live_entries_for_project

        entries, inventory_present = _live_entries_for_project(project_id, caller)
    except Exception:
        return receipt
    if inventory_present:
        terminal = str(receipt.get("caller_terminal") or "").strip().lower()
        matches = [session for _record, session in entries
                   if session.matches_pane(pane_id, terminal) and session.active]
        if len(matches) == 1:
            session = matches[0]
            receipt["caller_live_id"] = session.live_id
            receipt["caller_registry_session_id"] = session.launch_id
            receipt["caller_pane_title_marker"] = session.pane_title_marker
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


def server_result_path(receipt: dict[str, Any]) -> Path | None:
    """Where the server-side (adapter/daemon) persisted result lives for a
    receipt -- derived from `log_file`, never a new field a receipt must
    already carry, so this resolves correctly even for a receipt written
    before this existed.

    Deliberately a DIFFERENT file from `log_file` itself: that file is, and
    remains, exclusively populated by the client-side stdout capture that
    has always owned it. Writing here from a second process (the daemon's
    worker thread, ahead of that capture) would otherwise race it instead
    of reliably beating it to disk.
    """
    raw = str(receipt.get("log_file") or "").strip()
    if not raw:
        return None
    return Path(raw).with_suffix(".result")


def _read_server_result_payload(receipt: dict[str, Any]) -> dict[str, Any] | None:
    path = server_result_path(receipt)
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def read_server_result(receipt: dict[str, Any]) -> str:
    """Item 1: the provider adapter's own pre-notification save, but ONLY
    when it represents a genuinely FINISHED answer (persisted `status` ==
    the completed status -- see `persist_proven_result`).

    A saved INCOMPLETE/CANCELLED/FAILED placeholder must never read as a
    finished answer: this returns `""` for one, exactly as it does for a
    receipt with nothing persisted at all, so every existing caller that
    treats a truthy result as "done" keeps working, and none of them start
    treating a placeholder as done. Callers needing the raw persisted
    status regardless of finality use `read_server_result_status`.
    """
    payload = _read_server_result_payload(receipt)
    if payload is None:
        return ""
    if str(payload.get("status") or "") != _COMPLETION_STATUS_COMPLETED:
        return ""
    return str(payload.get("reply") or "").strip()


def read_server_result_status(receipt: dict[str, Any]) -> str:
    """The raw persisted status, whatever it is -- empty when nothing has
    been persisted yet. Unlike `read_server_result`, this does NOT gate on
    finality; it is what lets a caller tell "nothing saved yet" apart from
    "a placeholder was saved" apart from "the real answer was saved"."""
    payload = _read_server_result_payload(receipt)
    if payload is None:
        return ""
    return str(payload.get("status") or "")


class PersistOutcome:
    """Item 5: what happened when a provider adapter tried to persist its
    proven result, for the adapter's OWN use in deciding whether to notify.

    - NO_RECEIPT: this request has no async receipt at all (a foreground or
      notify-only call never created one). Not a failure -- there was
      nothing to save to, so notification proceeds exactly as before this
      mechanism existed.
    - SAVED: the reply text was durably written to the receipt's result
      sidecar. Notification may proceed.
    - FAILED: a receipt WAS expected (one exists for this task id) but the
      reply text could not be written. The adapter must suppress automatic
      notification -- delivering "your task is done" when the durable
      answer failed to save is exactly the failure this ordering exists to
      prevent.
    """

    NO_RECEIPT = "no_receipt"
    SAVED = "saved"
    FAILED = "failed"


def persist_proven_result(
    req_id: str,
    *,
    reply: str = "",
    status: str = "",
    transcript_path: str = "",
    conversation_id: str = "",
    root: Path | None = None,
) -> str:
    """Called by a provider adapter, inside the daemon, BEFORE any
    completion notification is even attempted -- so a delivery that then
    fails or is suppressed always leaves the answer, and the exact
    conversation it was proven to come from, already durable and findable
    by `req_id` alone. Returns one of `PersistOutcome.{NO_RECEIPT,SAVED,
    FAILED}`; see that class for what the adapter must do with each.

    `status` (a `completion_hook.COMPLETION_STATUS_*` value) is persisted
    alongside the reply so a later reader can tell a genuinely finished
    answer apart from an incomplete/cancelled/failed placeholder --
    `read_server_result` only ever returns text for the former. This is
    also why a placeholder never blocks a later, real reply from
    superseding it: a placeholder is never treated as authoritative, so
    retrieval keeps falling through to recovery (`destination_transcript_
    path`, below) until a genuinely completed result is persisted.

    `transcript_path`/`conversation_id` are the adapter's OWN proof,
    captured at the moment it confirmed the request anchor -- the exact
    native transcript file this reply came from, and that transcript's own
    session identity -- never the mutable `destination_session_file`
    binding captured at ask-time, which a LATER, unrelated ask can silently
    repoint at a different conversation. A metadata-only update (this pair
    changed but the reply body did not, or could not, get written) is
    NEVER reported as SAVED on its own -- only a durably-written reply body
    counts as having saved the answer.
    """
    found = find_receipt(req_id, root=root)
    if found is None:
        return PersistOutcome.NO_RECEIPT
    path, receipt = found

    # Correction 2: once a receipt IS found, the default outcome is
    # FAILED, not NO_RECEIPT -- NO_RECEIPT means "nothing was ever
    # expected to be saved here," which is no longer true the moment a
    # receipt exists. An empty reply body (which should not happen in
    # practice; every adapter always computes a non-empty `reply_for_hook`)
    # must not silently read as "nothing to save" and let the caller
    # notify normally -- that defeats save-before-notify for exactly the
    # case where nothing was saved. Only a durably written reply body ever
    # moves this to SAVED.
    outcome = PersistOutcome.FAILED
    body = (reply or "").strip()
    if body:
        result_path = server_result_path(receipt)
        if result_path is None:
            outcome = PersistOutcome.FAILED
        else:
            try:
                payload = json.dumps({"reply": body, "status": status or ""}, ensure_ascii=False)
                result_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(result_path, payload + "\n")
                outcome = PersistOutcome.SAVED
            except Exception:
                outcome = PersistOutcome.FAILED

    updated = dict(receipt)
    changed = False
    if transcript_path and str(receipt.get("destination_transcript_path") or "") != transcript_path:
        updated["destination_transcript_path"] = transcript_path
        changed = True
    if conversation_id and str(receipt.get("destination_conversation_id") or "") != conversation_id:
        updated["destination_conversation_id"] = conversation_id
        changed = True
    if changed:
        try:
            write_receipt(path, updated)
        except Exception:
            pass

    return outcome


def update_peer_delivery(
    task_id: str,
    *,
    confirmation: str,
    target_work_dir: str = "",
    target_project_id: str = "",
    target_log_path: str = "",
    root: Path | None = None,
) -> dict[str, Any] | None:
    """Persist the latest delivery evidence for a peer task receipt."""
    normalized = (confirmation or "").strip().lower()
    if normalized not in PEER_DELIVERY_CONFIRMATIONS:
        raise ValueError(f"invalid peer delivery confirmation: {confirmation}")
    found = find_receipt(task_id, root=root)
    if found is None:
        return None
    path, receipt = found
    if not str(receipt.get("provider") or "").startswith("peer-"):
        return None

    updated = dict(receipt)
    updated["delivery_confirmation"] = normalized
    updated["delivery_updated_at"] = datetime.now(timezone.utc).isoformat()
    if target_work_dir:
        updated["peer_target_work_dir"] = str(target_work_dir)
    if target_project_id:
        updated["peer_target_project_id"] = str(target_project_id)
    if target_log_path:
        updated["peer_target_log_path"] = str(target_log_path)
    write_receipt(path, updated)
    return updated
