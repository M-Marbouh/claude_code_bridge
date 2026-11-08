"""Read and validate Pi's session JSONL for CCB request exchanges."""
from __future__ import annotations

import json
import os
import re
import time
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
        for index in range(1, len(rows)):
            row = rows[index]
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
                reason = f"Pi turn failed: {message.get('errorMessage') or 'unknown error'}"
                error_id = str(row.get("id") or "")
                retry_index, blocked_index = _retry_edit_scan(rows, index)
                if retry_index is None and blocked_index is not None:
                    return PiExchange("incomplete", reason=reason, rows=tuple(rows))
                if retry_index is None:
                    # Pi writes its retry context_edit after flushing pending custom messages at turn
                    # end, so a read can land on a failed row before the edit exists. Carries the
                    # failed entry id for its own grace window.
                    return PiExchange("errored", reason=reason, rows=tuple(rows), error_id=error_id)
                if not any(
                    later.get("type") == "message" and _message(later).get("role") in {"user", "assistant"}
                    for later in rows[retry_index + 1:]
                ):
                    return PiExchange("retrying", reason=reason, rows=tuple(rows), error_id=error_id)
                continue
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
    error_id: str = ""


def _retry_edit_scan(rows: list[dict[str, Any]], index: int) -> tuple[int | None, int | None]:
    """Scan the rows after the failed assistant row at `index` for Pi's retry edit.

    Returns `(edit_index, blocked_index)`: `edit_index` is the retry edit (a null-replacement
    `context_edit` whose targetId is the failed row); `blocked_index` is the first row that ends the
    attempt, i.e. any message row other than the failed row's own toolResult rows. At most one is
    set; both are None when only permitted intervening rows follow and no retry edit exists yet
    (Pi flushes pending custom messages at turn end before deciding to retry, so those, model or
    thinking-level changes, other Pi non-message rows and unrelated edits may appear first).
    """
    failed = rows[index]
    error_id = str(failed.get("id") or "")
    tool_ids = {
        str(part.get("id") or "") for part in _message(failed).get("content") or []
        if isinstance(part, dict) and part.get("type") == "toolCall"
    }
    for later_index in range(index + 1, len(rows)):
        later = rows[later_index]
        if later.get("type") == "message":
            message = _message(later)
            if message.get("role") == "toolResult" and str(message.get("toolCallId") or "") in tool_ids:
                continue
            return None, later_index
        if later.get("type") == "context_edit" and later.get("replacement") is None and str(later.get("targetId") or "") == error_id:
            return later_index, None
    return None, None


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
        backend, pane_id, _data, error = _verify_pane(Path.cwd(), os.environ.get("CCB_SESSION_FILE"))
        if error:
            return False, error
        screen = backend.get_pane_content(pane_id, lines=40)
        state = classify_pane_screen(screen or "")
        if state != "idle":
            return False, f"Pi pane is {state}: {describe_pane_screen(screen)}; input is not verified idle"
        return True, "Pi pane and session are ready"


def _verify_pane(
    work_dir: Path, explicit_path: str | Path | None = None
) -> tuple[Any, str, dict[str, Any] | None, str]:
    """The ccb-ping readiness evidence: (backend, pane_id, binding, error).

    Identical checks and messages to `PiCommunicator.ping`, minus the idle screen read, so callers that
    must act on the pane (ccb-ping refuses, autonew sends) share one destination verification.
    """
    from terminal import get_backend_for_session

    loaded = load_session_file(work_dir, explicit_path)
    if not loaded:
        return None, "", None, "Pi session file not found"
    _path, data = loaded
    if data.get("active") is not True:
        return None, "", data, "Pi session is inactive"
    pane_id = str(data.get("pane_id") or "").strip()
    marker = str(data.get("pane_title_marker") or "").strip()
    backend = get_backend_for_session(data)
    if not pane_id or not marker or backend is None or not backend.is_alive(pane_id):
        return None, "", data, "Pi pane is not active"
    finder = getattr(backend, "find_pane_by_title_marker", None)
    if not callable(finder) or str(finder(marker, str(work_dir)) or "").strip() != pane_id:
        return None, "", data, "Pi pane identity could not be verified"
    session_dir_raw = str(data.get("pi_session_dir") or "").strip()
    session_dir = Path(session_dir_raw).expanduser() if session_dir_raw else None
    session_id = str(data.get("pi_session_id") or "").strip()
    if not session_id or session_dir is None:
        return None, "", data, "Pi session binding is incomplete"
    try:
        load_session(session_dir, session_id)
    except PiSessionError as exc:
        if exc.kind != "not_created":
            return None, "", data, str(exc)
    return backend, pane_id, data, ""


def capture_screen_state(backend: Any, pane_id: str) -> tuple[str, str | None]:
    """Classify one pane capture; ("unknown", None) when the capture is unavailable."""
    try:
        screen = backend.get_pane_content(pane_id, lines=40)
    except Exception:
        return "unknown", None
    if screen is None:
        return "unknown", None
    return classify_pane_screen(screen), screen


def screen_context(screen: str) -> tuple[str, ...] | None:
    """The two lines above the last editor rule; a repaint moves them."""
    lines = screen.splitlines()
    for index in range(len(lines) - 4, -1, -1):
        if lines[index].startswith("─"):
            return tuple(lines[max(0, index - 2):index])
    return None


def capture_confirmed_state(backend: Any, pane_id: str) -> str:
    """Double capture: "idle" only when two reads agree the pane is idle.

    Shared by the askd adapter (pre-send readiness, retry idle window) and autonew (a reset must never
    land on a busy pane or concatenate with an editor draft).
    """
    first_state, first_screen = capture_screen_state(backend, pane_id)
    if first_screen is None or first_state != "idle":
        return first_state
    time.sleep(0.2)
    second_state, second_screen = capture_screen_state(backend, pane_id)
    if second_screen is None:
        return "unknown"
    if screen_context(first_screen) != screen_context(second_screen):
        return "busy"
    return second_state


@dataclass(frozen=True)
class LivePiSession:
    """One live Pi session a launch record publishes (autonew destination evidence)."""

    launch_id: str
    pane_id: str
    terminal: str = ""


def _live_session_pane_alive(record: dict[str, Any], session: Any) -> bool:
    """Apply `_provider_pane_alive`'s pane rules to a `live_sessions` member.

    Reuses that one implementation by projecting the member into the record shape it reads
    (terminal, work_dir, providers), so inventory members and provider entries are judged alike.
    """
    from pane_registry import _provider_pane_alive

    return _provider_pane_alive(
        {
            "terminal": session.terminal or str(record.get("terminal") or ""),
            "work_dir": session.work_dir or str(record.get("work_dir") or ""),
            "providers": {
                "pi": {"pane_id": session.pane_id, "pane_title_marker": session.pane_title_marker},
            },
        },
        "pi",
    )


def live_pi_sessions(project_id: str) -> tuple[tuple[LivePiSession, ...], str]:
    """The live Pi sessions this project's launch records publish, by the registry's own rules.

    A record counts only when it is not stale and its project matches, exactly as
    `load_registry_by_project_id` qualifies records. A Pi session counts only when it is active AND
    its pane is alive with a matching title marker: the rule `_provider_pane_alive` applies to
    provider entries, applied here to `live_sessions` members too. Returns `(sessions, error)`;
    `error` is set when a launch's inventory is broken, because then "exactly one" cannot be proven.
    """
    from pane_registry import (
        _coerce_updated_at,
        _get_providers_map,
        _is_stale,
        _iter_registry_files,
        _load_registry_file,
        _provider_pane_alive,
        read_inventory_for_record,
    )
    from project_id import compute_ccb_project_id

    wanted = (project_id or "").strip()
    found: list[LivePiSession] = []
    for path in _iter_registry_files():
        record = _load_registry_file(path)
        if not record:
            continue
        if _is_stale(_coerce_updated_at(record.get("updated_at"), path)):
            continue
        record_project = str(record.get("ccb_project_id") or "").strip()
        if not record_project:
            work_dir = str(record.get("work_dir") or "").strip()
            try:
                record_project = compute_ccb_project_id(Path(work_dir)) if work_dir else ""
            except Exception:
                record_project = ""
        if not wanted or record_project != wanted:
            continue
        launch_id = str(record.get("ccb_session_id") or "").strip()
        record_terminal = str(record.get("terminal") or "").strip().lower()
        inventory = read_inventory_for_record(record)
        if inventory.status == "invalid":
            return (), f"a launch for this project ({launch_id}) publishes a broken Pi inventory; refusing to guess a destination"
        if inventory.status == "valid":
            for session in inventory.sessions:
                if session.provider != "pi" or not session.active:
                    continue
                if not _live_session_pane_alive(record, session):
                    continue
                found.append(LivePiSession(
                    launch_id=session.launch_id or launch_id,
                    pane_id=session.pane_id,
                    terminal=(session.terminal or record_terminal).strip().lower(),
                ))
            continue
        entry = _get_providers_map(record).get("pi")
        if isinstance(entry, dict) and _provider_pane_alive(record, "pi"):
            found.append(LivePiSession(
                launch_id=launch_id,
                pane_id=str(entry.get("pane_id") or "").strip(),
                terminal=str(entry.get("terminal") or record_terminal).strip().lower(),
            ))
    return tuple(found), ""


def reset_destination(work_dir: Path) -> tuple[Any, str, str]:
    """Resolve the single live, idle Pi pane that `/new` may be sent to: (backend, pane_id, error).

    Fail-closed. Requires exactly one eligible live Pi session for the project (none and several both
    refuse), and corroborates that session against the binding: same launch, same terminal backend,
    same pane id. Then the binding and pane identity must verify and the pane must be confirmed idle.
    """
    from project_id import compute_ccb_project_id

    try:
        project_id = compute_ccb_project_id(work_dir)
    except Exception as exc:
        return None, "", f"Pi project identity could not be computed: {exc}"
    sessions, inventory_error = live_pi_sessions(project_id)
    if inventory_error:
        return None, "", inventory_error
    if not sessions:
        return None, "", "no live Pi session for this project; refusing to send /new"
    if len(sessions) > 1:
        return None, "", f"{len(sessions)} live Pi sessions for this project; refusing to pick one"
    backend, pane_id, binding, error = _verify_pane(work_dir)
    if error:
        return None, "", error
    bound = binding or {}
    disagree = []
    launch_id = str(bound.get("session_id") or "").strip()
    terminal = str(bound.get("terminal") or "").strip().lower()
    sole = sessions[0]
    if sole.launch_id != launch_id:
        disagree.append(f"launch {sole.launch_id or '<none>'} is not binding launch {launch_id or '<none>'}")
    if sole.terminal != terminal:
        disagree.append(f"terminal {sole.terminal or '<none>'} is not binding terminal {terminal or '<none>'}")
    if sole.pane_id != pane_id:
        disagree.append(f"live pane {sole.pane_id or '<none>'} is not binding pane {pane_id or '<none>'}")
    if disagree:
        return None, "", "Pi destination disagrees with its binding: " + "; ".join(disagree)
    state = capture_confirmed_state(backend, pane_id)
    if state != "idle":
        return None, "", f"Pi pane is {state}; refusing to send /new"
    return backend, pane_id, ""

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
# The stats line (footer.js:150-178) always starts with a usage figure and survives right-side
# truncation: ↑/↓/R/W token counts, the cost, the context percentage, or the unknown-context "?/".
_STATS_LINE = re.compile(
    r"^\s*(?:[↑↓RW]\d+(?:\.\d+)?[kM]?(?=\s|\.\.\.|$)|\$\d|\d+(?:\.\d+)?%/|\?/)"
)
_RULE_LINE = re.compile(r"^\s*─{8,}\s*$")
_EDITOR_LINE = re.compile(r"^\s*(?:›\s*)?$")
_CWD_LINE = re.compile(r"^\s*(?:/|~/|<CCB_CWD>).*\s*$")
_OVERFLOW_LABEL = re.compile(r"↑\s*\d+\s+more")
_LOADER_LINE = re.compile(
    r"^\s*(?:.*\s)?(?:Working|Retrying \(|Compacting context|Auto-compacting|"
    r"Context overflow detected|Summarizing branch)"
)


def classify_pane_screen(screen: str) -> str:
    """Classify pane readiness from visible dialogs, activity, and editor box.

    The editor-box structure decides first; the dialog-marker scan only runs when the
    structure is unrecognised, so transcript text quoting a marker cannot fake a dialog.
    The status line is recognised by its stats prefix, not by the model/thinking suffix:
    that suffix changes with pane width, model type and thinking level.
    Accepted limitation: an extension overlay that draws a built-in dialog title while
    leaving the editor box visible now classifies as "idle" (the marker scan used to
    catch it). Built-in selectors replace the editor box entirely, so they are unaffected.
    """
    text = screen or ""
    state = _classify_pane_structure(text)
    if state in ("idle", "busy"):
        return state
    if any(marker.casefold() in text.casefold() for marker in _DIALOG_MARKERS):
        return "dialog"
    return "unknown"


def describe_pane_screen(screen: str | None) -> str:
    """Plain words for why a capture is not a confirmed idle input box.

    Walks the same structural steps as `_classify_pane_structure` and names the first one
    that failed, with an approximate pane width taken from the longest captured line.
    """
    if screen is None:
        return "pane capture unavailable"
    lines = screen.splitlines()
    width = max((len(line.rstrip()) for line in lines), default=0)
    width_note = f", pane ~{width} cols wide" if width else ""
    if len(lines) < 5:
        return "no input box on screen"
    if _classify_pane_structure(screen) == "unknown" and any(
        marker.casefold() in screen.casefold() for marker in _DIALOG_MARKERS
    ):
        return f"a dialog is on screen{width_note}"
    if not _STATS_LINE.match(lines[-1]):
        return f"status lines not recognised{width_note}"
    if not _CWD_LINE.fullmatch(lines[-2]):
        return f"working-directory line above the status lines not recognised{width_note}"
    if not _RULE_LINE.fullmatch(lines[-3]):
        return f"input box bottom border not recognised{width_note}"
    editor_end = len(lines) - 3
    top_index = editor_end - 1
    while top_index >= 0 and not lines[top_index].startswith("─"):
        top_index -= 1
    if top_index < 0:
        return f"input box top border not found{width_note}"
    if len(lines[top_index].rstrip()) != len(lines[-3].rstrip()):
        return f"input box borders differ in width{width_note}"
    border = _OVERFLOW_LABEL.sub("", lines[top_index])
    if re.sub(r"[─\s]", "", border):
        return "activity shown in the input box border (pane is busy)"
    if not _RULE_LINE.fullmatch(border):
        return f"input box top border not recognised{width_note}"
    if top_index >= 2 and not lines[top_index - 2].strip() and _LOADER_LINE.match(lines[top_index - 1]):
        return "activity message above the input box (pane is busy)"
    editor = lines[top_index + 1:editor_end]
    if len(editor) == 1 and _EDITOR_LINE.fullmatch(editor[0]):
        return "input box is empty and idle"
    return "input box has unsent text"


def _classify_pane_structure(screen: str) -> str:
    """Structural editor-box parse; "unknown" means no recognised editor box."""
    lines = screen.splitlines()
    if len(lines) < 5 or not _STATS_LINE.match(lines[-1]):
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
