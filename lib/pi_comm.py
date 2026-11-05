"""Read and validate Pi's session JSONL for CCB request exchanges."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccb_protocol import done_line_re, find_done_line, is_done_text

SUPPORTED_SESSION_VERSIONS = frozenset({3})
ABORTED_ERROR = "This operation was aborted"
_REQ_LINE = re.compile(r"^CCB_REQ_ID: ([^\s]+)$", re.MULTILINE)


class PiSessionError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class PiSession:
    path: Path
    session_id: str
    header: dict[str, Any]
    entries: tuple[dict[str, Any], ...]

    def anchors(self, req_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            entry for entry in self.entries
            if entry.get("type") == "message"
            and _message(entry).get("role") == "user"
            and f"CCB_REQ_ID: {req_id}" in _text_parts(_message(entry).get("content")).splitlines()
        )

    def exchange(self, req_id: str) -> "PiExchange":
        anchors = self.anchors(req_id)
        if len(anchors) > 1:
            return PiExchange("ambiguous", reason=f"Pi request anchor is ambiguous for {req_id}.")
        if not anchors:
            return PiExchange("missing")
        anchor = anchors[0]
        by_id = {str(row.get("id")): row for row in self.entries if row.get("id")}
        chain: list[dict[str, Any]] = []
        cursor = self.entries[-1] if self.entries else None
        visited: set[str] = set()
        while cursor is not None:
            row_id = str(cursor.get("id") or "")
            if row_id and row_id in visited:
                return PiExchange("invalid", reason="Pi session contains a parentId cycle.")
            if row_id:
                visited.add(row_id)
            chain.append(cursor)
            parent = cursor.get("parentId")
            cursor = by_id.get(str(parent)) if parent else None
        chain.reverse()
        anchor_id = str(anchor.get("id") or "")
        chain_ids = [str(row.get("id") or "") for row in chain]
        if anchor_id not in chain_ids:
            return PiExchange("branch_switched", reason="Pi request anchor is not on the persisted active branch.")
        start = chain_ids.index(anchor_id)
        rows = chain[start:]
        parked = False
        for row in rows[1:]:
            message = _message(row)
            if row.get("type") == "message" and message.get("role") == "user":
                later_ids = [rid for rid in _REQ_LINE.findall(_text_parts(message.get("content"))) if rid]
                if any(rid != req_id for rid in later_ids):
                    return PiExchange("superseded", reason="A later CCB request superseded this Pi request.", rows=tuple(rows))
                if not later_ids:
                    if not parked:
                        return PiExchange("incomplete", reason="An unwrapped Pi user message interrupted the request without a prior abort.", rows=tuple(rows))
                    parked = False
            if row.get("type") != "message" or message.get("role") != "assistant":
                continue
            stop_reason = str(message.get("stopReason") or "")
            if stop_reason == "error":
                if message.get("errorMessage") == ABORTED_ERROR:
                    parked = True
                    continue
                return PiExchange("incomplete", reason=f"Pi turn failed: {message.get('errorMessage') or 'unknown error'}", rows=tuple(rows))
            if stop_reason != "stop":
                continue
            parts = message.get("content")
            if not isinstance(parts, list):
                return PiExchange("incomplete", reason="Pi turn ended without a valid assistant text completion.", rows=tuple(rows))
            text_parts = [part.get("text", "") for part in parts if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)]
            if any(isinstance(part, dict) and part.get("type") == "toolCall" for part in parts):
                return PiExchange("incomplete", reason="Pi assistant turn ended with a tool call.", rows=tuple(rows))
            text = "\n".join(text_parts)
            lines = text.rstrip().splitlines()
            if lines and done_line_re(req_id).fullmatch(lines[-1]) and find_done_line(text, req_id) is not None and is_done_text(text, req_id, turn_ended=True):
                return PiExchange("completed", reply=text, rows=tuple(rows), completion_id=str(row.get("id") or ""))
            return PiExchange("incomplete", reason="Pi turn ended without CCB_DONE.", rows=tuple(rows))
        if parked:
            return PiExchange("parked", reason="Pi turn was interrupted; waiting for a same-branch continuation.", rows=tuple(rows))
        return PiExchange("pending", rows=tuple(rows))

    def completed_exchange(self, req_id: str) -> "PiExchange":
        """Recover a completion on any proven descendant branch of its anchor."""
        anchors = self.anchors(req_id)
        if len(anchors) > 1:
            return PiExchange("ambiguous", reason=f"Pi request anchor is ambiguous for {req_id}.")
        if not anchors:
            return PiExchange("missing")
        anchor_id = str(anchors[0].get("id") or "")
        by_id = {str(row.get("id")): row for row in self.entries if row.get("id")}
        completed: dict[str, PiExchange] = {}
        for row in self.entries:
            message = _message(row)
            if row.get("type") != "message" or message.get("role") != "assistant" or message.get("stopReason") != "stop":
                continue
            parts = message.get("content")
            if not isinstance(parts, list) or any(isinstance(part, dict) and part.get("type") == "toolCall" for part in parts):
                continue
            text = _text_parts(parts)
            if find_done_line(text, req_id) is None:
                continue
            reverse_chain: list[dict[str, Any]] = []
            cursor: dict[str, Any] | None = row
            seen: set[str] = set()
            while cursor is not None:
                row_id = str(cursor.get("id") or "")
                if row_id and row_id in seen:
                    break
                if row_id:
                    seen.add(row_id)
                reverse_chain.append(cursor)
                parent = cursor.get("parentId")
                cursor = by_id.get(str(parent)) if parent else None
            branch = list(reversed(reverse_chain))
            if anchor_id not in {str(item.get("id") or "") for item in branch}:
                continue
            candidate = PiSession(self.path, self.session_id, self.header, tuple(branch)).exchange(req_id)
            if candidate.state == "completed":
                completion_id = candidate.completion_id
                completed[completion_id or f"reply:{candidate.reply}"] = candidate
        if len(completed) == 1:
            return next(iter(completed.values()))
        if len(completed) > 1:
            return PiExchange("ambiguous", reason=f"Pi has multiple completed branches for {req_id}.")
        return PiExchange("incomplete", reason=f"No proven Pi completion was found for {req_id}.")


@dataclass(frozen=True)
class PiExchange:
    state: str
    reply: str = ""
    reason: str = ""
    rows: tuple[dict[str, Any], ...] = ()
    completion_id: str = ""


def _message(entry: dict[str, Any]) -> dict[str, Any]:
    value = entry.get("message")
    return value if isinstance(value, dict) else {}


def _text_parts(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        part["text"] for part in content
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
    )


def matching_session_files(session_dir: Path, session_id: str) -> tuple[Path, ...]:
    return tuple(sorted(Path(session_dir).glob(f"*_{session_id}.jsonl")))


def load_session(session_dir: Path, session_id: str) -> PiSession:
    matches = matching_session_files(session_dir, session_id)
    if len(matches) != 1:
        kind = "ambiguous" if matches else "not_created"
        raise PiSessionError(kind, f"Pi session file {'is ambiguous' if matches else 'has not been created'} for {session_id}.")
    return _load_session_path(matches[0], session_id)


def _load_session_path(path: Path, session_id: str) -> PiSession:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PiSessionError("unavailable", f"Cannot read Pi session file: {exc}") from exc
    lines = raw.splitlines(keepends=True)
    if not lines:
        raise PiSessionError("incompatible", "Pi session file is empty.")
    entries: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if index == len(lines) - 1 and not line.endswith((b"\n", b"\r")):
            break
        try:
            item = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PiSessionError("incompatible", f"Invalid Pi session row {index + 1}: {exc}") from exc
        if not isinstance(item, dict):
            raise PiSessionError("incompatible", f"Invalid Pi session row {index + 1}: expected object.")
        entries.append(item)
    if not entries:
        raise PiSessionError("incompatible", "Pi session has no complete header row.")
    header = entries[0]
    if header.get("type") != "session" or header.get("id") != session_id:
        raise PiSessionError("incompatible", "Pi session header identity does not match the requested session.")
    if header.get("version") not in SUPPORTED_SESSION_VERSIONS:
        raise PiSessionError("unsupported", f"Pi session format unsupported: version {header.get('version')!r}.")
    ids = [str(item.get("id")) for item in entries[1:] if item.get("id")]
    if len(ids) != len(set(ids)):
        raise PiSessionError("incompatible", "Pi session contains duplicate entry IDs.")
    return PiSession(path, session_id, header, tuple(entries[1:]))


def _load_cached_session_path(
    path: Path,
    session_id: str,
    cache: dict[tuple[Path, int, int], tuple[PiSession | None, PiSessionError | None]] | None,
) -> PiSession:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise PiSessionError("unavailable", f"Cannot stat Pi session file: {exc}") from exc
    key = (path, metadata.st_mtime_ns, metadata.st_size)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            session, error = cached
            if error is not None:
                raise error
            if session is not None:
                return session
        for old_key in tuple(cache):
            if old_key[0] == path and old_key != key:
                del cache[old_key]
    try:
        session = _load_session_path(path, session_id)
    except PiSessionError as exc:
        if cache is not None:
            cache[key] = (None, exc)
        raise
    if cache is not None:
        cache[key] = (session, None)
    return session


def load_candidate_sessions(
    session_dir: Path,
    work_dir: Path,
    *,
    bound_session_id: str = "",
    start_dir: str | Path | None = None,
    cache: dict[tuple[Path, int, int], tuple[PiSession | None, PiSessionError | None]] | None = None,
) -> tuple[PiSession, ...]:
    """Load the bound session and valid same-workspace Pi sessions."""
    session_dir = Path(session_dir)
    bound_path: Path | None = None
    candidates: list[PiSession] = []
    if bound_session_id:
        matches = matching_session_files(session_dir, bound_session_id)
        if len(matches) > 1:
            raise PiSessionError("ambiguous", f"Pi session file is ambiguous for {bound_session_id}.")
        if matches:
            bound_path = matches[0]
            candidates.append(_load_cached_session_path(bound_path, bound_session_id, cache))

    allowed_cwds = {Path(work_dir).expanduser().resolve()}
    if start_dir:
        try:
            allowed_cwds.add(Path(start_dir).expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            pass
    for path in sorted(session_dir.glob("*.jsonl")):
        if path == bound_path:
            continue
        session_id = path.stem.rsplit("_", 1)[-1]
        if not session_id:
            continue
        try:
            session = _load_cached_session_path(path, session_id, cache)
            cwd_value = session.header.get("cwd")
            if not isinstance(cwd_value, str) or not cwd_value.strip():
                continue
            cwd = Path(cwd_value).expanduser().resolve()
            if cwd in allowed_cwds:
                candidates.append(session)
        except (OSError, PiSessionError, RuntimeError, ValueError):
            continue
    return tuple(candidates)


def load_session_file(work_dir: Path, explicit_path: str | Path | None = None) -> tuple[Path, dict[str, Any]] | None:
    from session_utils import find_project_session_file

    path = Path(explicit_path).expanduser() if explicit_path else find_project_session_file(work_dir, ".pi-session")
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return Path(path), data


class PiCommunicator:
    """Pane and exact-session health probe used by ccb-ping."""

    def ping(self, display: bool = True) -> tuple[bool, str]:
        from terminal import get_backend_for_session

        loaded = load_session_file(Path.cwd(), os.environ.get("CCB_SESSION_FILE"))
        if not loaded:
            return False, "Pi session file not found"
        _path, data = loaded
        if data.get("active") is not True:
            return False, "Pi session is inactive"
        pane_id = str(data.get("pane_id") or "").strip()
        marker = str(data.get("pane_title_marker") or "").strip()
        backend = get_backend_for_session(data)
        if not pane_id or not marker or backend is None or not backend.is_alive(pane_id):
            return False, "Pi pane is not active"
        finder = getattr(backend, "find_pane_by_title_marker", None)
        if not callable(finder) or str(finder(marker, str(Path.cwd())) or "").strip() != pane_id:
            return False, "Pi pane identity could not be verified"
        session_dir_raw = str(data.get("pi_session_dir") or "").strip()
        session_dir = Path(session_dir_raw).expanduser() if session_dir_raw else None
        session_id = str(data.get("pi_session_id") or "").strip()
        if not session_id or session_dir is None:
            return False, "Pi session binding is incomplete"
        try:
            load_session(session_dir, session_id)
        except PiSessionError as exc:
            if exc.kind != "not_created":
                return False, str(exc)
        screen = backend.get_pane_content(pane_id, lines=40)
        state = classify_pane_screen(screen or "")
        if state != "idle":
            return False, f"Pi pane is {state}; input is not verified idle"
        return True, "Pi pane and session are ready"


_DIALOG_MARKERS = (
    "Project trust",
    "Session Tree",
    "Type to search:",
    "Summarize with custom prompt",
    "Summarize branch?",
    "navigate  enter select",
    "enter select  escape/ctrl+c cancel",
    "Select a session",
)
_MODEL_FOOTER = re.compile(r"^.*\S+\s+[•·]\s+(?:off|minimal|low|medium|high|max)\s*$", re.IGNORECASE | re.MULTILINE)
_RULE_LINE = re.compile(r"^\s*─{8,}\s*$")
_EDITOR_LINE = re.compile(r"^\s*(?:›\s*)?$")
_CWD_LINE = re.compile(r"^\s*(?:/|~/|<CCB_CWD>).*\s*$")
_OVERFLOW_LABEL = re.compile(r"↑\s*\d+\s+more")
_LOADER_LINE = re.compile(
    r"^\s*(?:.*\s)?(?:Working|Retrying \(|Compacting context|Auto-compacting|"
    r"Context overflow detected|Summarizing branch)"
)


def classify_pane_screen(screen: str) -> str:
    """Classify pane readiness from visible dialogs, activity, and editor box."""
    text = screen or ""
    if any(marker.casefold() in text.casefold() for marker in _DIALOG_MARKERS):
        return "dialog"
    lines = text.splitlines()
    if len(lines) < 5 or not _MODEL_FOOTER.fullmatch(lines[-1]):
        return "unknown"
    if not _CWD_LINE.fullmatch(lines[-2]) or not _RULE_LINE.fullmatch(lines[-3]):
        return "unknown"
    editor_end = len(lines) - 3
    top_index = editor_end - 1
    while top_index >= 0 and not lines[top_index].startswith("─"):
        top_index -= 1
    if top_index < 0:
        return "unknown"
    if len(lines[top_index].rstrip()) != len(lines[-3].rstrip()):
        return "unknown"
    border = _OVERFLOW_LABEL.sub("", lines[top_index])
    if re.sub(r"[─\s]", "", border):
        return "busy"
    if not _RULE_LINE.fullmatch(border):
        return "unknown"
    # Built-in messages only: coverage is the embedded default editor (any frames/message) plus non-embedded loaders showing a built-in message. An extension editor with a custom message is detected only by the best-effort animation check above.
    if top_index >= 2 and not lines[top_index - 2].strip() and _LOADER_LINE.match(lines[top_index - 1]):
        return "busy"
    editor = lines[top_index + 1:editor_end]
    if len(editor) == 1 and _EDITOR_LINE.fullmatch(editor[0]):
        return "idle"
    return "unknown"
