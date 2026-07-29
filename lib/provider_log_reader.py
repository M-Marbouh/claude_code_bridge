from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from claude_comm import ClaudeLogReader, _extract_message as _extract_claude_message
from ccb_protocol import REQ_ID_PREFIX
from codex_comm import CodexLogReader, SESSION_ID_PATTERN
from pane_registry import (
    load_registry_by_claude_pane,
    load_registry_by_project_id,
    load_registry_by_session_id,
    upsert_registry,
)
from project_id import compute_ccb_project_id
from session_utils import find_project_session_file


DebugFn = Callable[[str], None]


def _noop_debug(_message: str) -> None:
    return


def _record_project_id(record: dict[str, Any]) -> str:
    project_id = str(record.get("ccb_project_id") or "").strip()
    if project_id:
        return project_id
    work_dir = record.get("work_dir")
    if isinstance(work_dir, str) and work_dir.strip():
        try:
            return compute_ccb_project_id(Path(work_dir.strip()))
        except Exception:
            return ""
    return ""


def _provider_data(record: dict[str, Any], provider: str) -> dict[str, Any]:
    providers = record.get("providers")
    if not isinstance(providers, dict):
        return {}
    data = providers.get(provider)
    return data if isinstance(data, dict) else {}


def _provider_log_path(
    record: dict[str, Any],
    provider: str,
    *,
    legacy_registry: bool = False,
) -> Path | None:
    data = _provider_data(record, provider)
    key = f"{provider}_session_path"
    if legacy_registry:
        providers = record.get("providers")
        nested = providers.get(provider) if isinstance(providers, dict) else None
        raw = nested.get(key) if isinstance(nested, dict) else record.get(key)
    else:
        raw = data.get(key) or record.get(key)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw.strip()).expanduser()


def _load_session_log_path(
    provider: str,
    work_dir: Path,
    explicit_session_file: Path | None,
    debug: DebugFn,
) -> tuple[Path | None, str | None]:
    filename = f".{provider}-session"
    session_file = explicit_session_file or find_project_session_file(work_dir, filename)
    if not session_file:
        return None, None
    try:
        with session_file.open("r", encoding="utf-8-sig", errors="replace") as handle:
            data = json.load(handle)
        raw_path = data.get(f"{provider}_session_path")
        session_id = data.get(f"{provider}_session_id")
        if provider == "claude":
            session_id = session_id or data.get("session_id")
        if isinstance(raw_path, str) and raw_path.strip():
            return Path(raw_path.strip()).expanduser(), str(session_id or "").strip() or None
    except Exception as exc:
        debug(f"Failed to read {filename} ({session_file}): {exc}")
    return None, None


def _legacy_registry_log_path(
    provider: str,
    project_id: str,
    debug: DebugFn,
) -> tuple[Path | None, dict[str, Any] | None]:
    session_env = "CODEX_SESSION_ID" if provider == "codex" else "CCB_SESSION_ID"
    session_id = (os.environ.get(session_env) or "").strip()
    if session_id:
        record = load_registry_by_session_id(session_id)
        if record and project_id and _record_project_id(record) != project_id:
            record = None
        if record:
            path = _provider_log_path(record, provider, legacy_registry=True)
            if path:
                debug(f"Using registry by {session_env}: {path}")
                return path, record

    pane_id = (os.environ.get("WEZTERM_PANE") or os.environ.get("TMUX_PANE") or "").strip()
    if pane_id:
        # Preserve cpend/lpend compatibility: their pane fallback is intentionally
        # keyed to the Claude pane. Strict overlay lookup never uses this branch.
        record = load_registry_by_claude_pane(pane_id)
        if record and project_id and _record_project_id(record) != project_id:
            record = None
        if record:
            path = _provider_log_path(record, provider, legacy_registry=True)
            if path:
                label = "caller pane" if provider == "codex" else "pane id"
                debug(f"Using registry by {label}: {path}")
                return path, record
    return None, None


def _path_mtime_ns(path: Path | None) -> int:
    if not path:
        return -1
    try:
        return int(path.stat().st_mtime_ns)
    except Exception:
        return -1


def _pick_claude_log_path(
    registry_path: Path | None,
    session_path: Path | None,
    debug: DebugFn,
) -> tuple[Path | None, str]:
    registry = registry_path if registry_path and registry_path.exists() else None
    session = session_path if session_path and session_path.exists() else None
    if registry and session:
        try:
            if registry.resolve() == session.resolve():
                debug(f"Registry/session path identical: {registry}")
                return registry, "same"
        except Exception:
            pass
        if _path_mtime_ns(session) >= _path_mtime_ns(registry):
            debug(
                "Registry/session conflict detected; prefer .claude-session path "
                f"(session newer/equal). registry={registry} session={session}"
            )
            return session, "session"
        debug(
            "Registry/session conflict detected; prefer registry path "
            f"(registry newer). registry={registry} session={session}"
        )
        return registry, "registry"
    if session:
        debug(f"Using claude_session_path from .claude-session: {session}")
        return session, "session"
    if registry:
        debug(f"Using claude_session_path from registry: {registry}")
        return registry, "registry"
    return None, "none"


def _sync_registry_claude_path(
    record: dict[str, Any] | None,
    path: Path | None,
    debug: DebugFn,
) -> None:
    if not record or not path:
        return
    providers = record.get("providers")
    if not isinstance(providers, dict):
        return
    claude = providers.get("claude")
    if not isinstance(claude, dict):
        return
    new_path = str(path)
    if str(claude.get("claude_session_path") or "").strip() == new_path:
        return
    try:
        claude["claude_session_path"] = new_path
        record["providers"] = providers
        upsert_registry(record)
        debug(f"Registry claude_session_path refreshed: {new_path}")
    except Exception as exc:
        debug(f"Failed to refresh registry claude_session_path: {exc}")


def _codex_session_filter(
    log_path: Path | None,
    record: dict[str, Any] | None,
    session_id: str | None,
    *,
    strict_tab: bool = False,
) -> str | None:
    if record:
        provider_id = _provider_data(record, "codex").get("codex_session_id") if strict_tab else None
        registry_id = provider_id or record.get("codex_session_id")
        if isinstance(registry_id, str) and registry_id.strip():
            return registry_id.strip()
    if session_id:
        return session_id
    if log_path:
        for source in (log_path.stem, log_path.name):
            match = SESSION_ID_PATTERN.search(source)
            if match:
                return match.group(0)
        return log_path.name
    return None


def provider_log_reader(
    provider: str,
    work_dir: Path,
    project_id: str,
    *,
    explicit_session_file: Path | None = None,
    strict_tab: bool = False,
    ccb_session_id: str = "",
    registry_record: dict[str, Any] | None = None,
    update_registry: bool = False,
    debug: DebugFn | None = None,
) -> CodexLogReader | ClaudeLogReader | None:
    """Build a provider reader using either strict tab binding or legacy resolution."""
    debug_fn = debug or _noop_debug
    provider = (provider or "").strip().lower()
    if provider not in {"codex", "claude"}:
        return None

    if strict_tab:
        record = registry_record if isinstance(registry_record, dict) else None
        if not record or _record_project_id(record) != project_id:
            return None
        record_session = str(record.get("ccb_session_id") or record.get("session_id") or "").strip()
        if not ccb_session_id or record_session != ccb_session_id:
            return None
        log_path = _provider_log_path(record, provider)
        if not log_path or not log_path.exists():
            return None
        if provider == "codex":
            return CodexLogReader(
                log_path=log_path,
                session_id_filter=_codex_session_filter(log_path, record, None, strict_tab=True),
                work_dir=work_dir,
                allow_stale_switch=False,
            )
        reader = ClaudeLogReader(work_dir=work_dir, allow_session_switch=False)
        reader.set_preferred_session(log_path)
        return reader

    registry_path, record = _legacy_registry_log_path(provider, project_id, debug_fn)
    if not registry_path and project_id:
        project_record = load_registry_by_project_id(project_id, provider)
        if project_record:
            providers = project_record.get("providers")
            nested = providers.get(provider) if isinstance(providers, dict) else None
            raw_path = nested.get(f"{provider}_session_path") if isinstance(nested, dict) else None
            registry_path = Path(raw_path).expanduser() if isinstance(raw_path, str) and raw_path.strip() else None
            if registry_path:
                record = project_record
                debug_fn(f"Using registry by ccb_project_id: {registry_path}")
    session_path, session_id = _load_session_log_path(
        provider, work_dir, explicit_session_file, debug_fn
    )

    if provider == "codex":
        log_path = registry_path or session_path
        if not registry_path and log_path:
            debug_fn(f"Using codex_session_path from .codex-session: {log_path}")
        return CodexLogReader(
            log_path=log_path,
            session_id_filter=_codex_session_filter(log_path, record, session_id),
            work_dir=work_dir,
        )

    log_path, source = _pick_claude_log_path(registry_path, session_path, debug_fn)
    if update_registry and source in {"session", "same"}:
        _sync_registry_claude_path(record, log_path, debug_fn)
    reader = ClaudeLogReader(work_dir=work_dir)
    if log_path:
        reader.set_preferred_session(log_path)
    return reader


def _first_nonempty_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def provider_request_anchor_seen(
    provider: str,
    work_dir: Path,
    project_id: str,
    req_id: str,
    *,
    log_path: str | Path | None = None,
) -> bool:
    """Return whether the provider transcript contains this request's user anchor."""
    provider = (provider or "").strip().lower()
    marker = f"{REQ_ID_PREFIX} {(req_id or '').strip()}"
    if provider not in {"claude", "codex"} or not req_id:
        return False

    path = Path(log_path).expanduser() if log_path else None
    if path is None or not path.is_file():
        reader = provider_log_reader(provider, work_dir, project_id)
        if reader is None:
            return False
        if provider == "codex":
            path = reader.current_log_path()
        else:
            path = reader.current_session_path()
    if path is None or not path.is_file():
        return False

    tail_bytes = 8 * 1024 * 1024
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes), os.SEEK_SET)
            lines = handle.read(tail_bytes).splitlines()
    except OSError:
        return False

    for raw_line in reversed(lines):
        try:
            entry = json.loads(raw_line.decode("utf-8", errors="ignore"))
        except Exception:
            continue
        if provider == "codex":
            event = CodexLogReader._extract_event(entry)
            text = event[1] if event is not None and event[0] == "user" else ""
        else:
            text = _extract_claude_message(entry, "user") or ""
        if _first_nonempty_line(text) == marker:
            return True
    return False
