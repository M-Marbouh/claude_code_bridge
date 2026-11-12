"""Read and validate Pi's session JSONL for CCB request exchanges."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

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
        _backend, _pane_id, binding, error = _verify_pane(
            Path.cwd(), os.environ.get("CCB_SESSION_FILE")
        )
        if error:
            return False, error
        state = confirm_pi_ready(binding or {})
        if state.status != "ready":
            return False, f"Pi pane is {state.status}: {state.reason}; input is not verified idle"
        return True, "Pi pane and session are ready"


def _verify_pane(
    work_dir: Path, explicit_path: str | Path | None = None
) -> tuple[Any, str, dict[str, Any] | None, str]:
    """The ccb-ping readiness evidence: (backend, pane_id, binding, error).

    Identical checks and messages to `PiCommunicator.ping`, minus the readiness confirmation, so
    callers that must act on the pane (ccb-ping refuses, autonew sends) share one destination
    verification and each applies the readiness report itself.
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


PI_STATE_FILENAME = "pi-state.json"
PI_STATE_MISSING_HINT = "CCB's Pi extension is not reporting; restart CCB"
PI_STATE_CONFIRM_TIMEOUT_S = 2.0
PI_STATE_CONFIRM_POLL_S = 0.1
# Pi rewrites its snapshot every 500 ms; three seconds without one means the runtime is gone even
# if a pid still exists, and a timestamp in the future cannot be trusted either.
PI_STATE_MAX_AGE_S = 3.0
PI_STATE_MAX_FUTURE_S = 2.0


@dataclass(frozen=True)
class PiState:
    """Pi's own readiness report for one binding: status plus the words to show a caller.

    status is one of ready | busy | draft | prompt | stale | absent | mismatch. Only "ready" may
    be typed into; every other value refuses, for its own reason.

    Known limitation (documented, not fixed here): Pi's BUILT-IN selectors (/tree, the model
    picker) replace the editor without emitting a ui_prompt_start event, so an open selector is
    reported as idle unless the user has typed into it. A prompt sent then lands in the selector
    as input and its trailing Enter can also confirm a selection. Only prompts opened through
    Pi's prompt API (project trust, extension dialogs) raise `prompt_open`.
    """

    status: str
    reason: str = ""


@dataclass(frozen=True)
class _Reading:
    """One snapshot read: the public state plus the runtime identity confirmation compares."""

    state: PiState
    launch_token: str = ""
    instance_id: str = ""
    seq: int = -1


def _pi_state_path(binding: Mapping[str, Any]) -> Path | None:
    explicit = str(binding.get("pi_state_file") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    runtime = str(binding.get("runtime_dir") or "").strip()
    if runtime:
        return Path(runtime).expanduser() / PI_STATE_FILENAME
    return None


def _coerce_int(value: Any) -> int | None:
    """An int from JSON; booleans are not ints here, and numeric strings are not guessed."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _coerce_epoch_seconds(value: Any) -> float | None:
    """The snapshot's `Date.now()` milliseconds as epoch seconds; None when unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number / 1000.0 if number > 0 else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) terminates the target on Windows: probe with OpenProcess instead.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_pi_reading(
    binding: Mapping[str, Any],
    *,
    now: float | None = None,
) -> _Reading:
    """Read and validate one snapshot against `binding`; never guesses."""
    moment = time.time() if now is None else now
    state_path = _pi_state_path(binding)
    token = str(binding.get("pi_launch_token") or "").strip()
    if state_path is None or not token:
        return _Reading(PiState("absent", PI_STATE_MISSING_HINT))
    try:
        raw = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _Reading(PiState("absent", PI_STATE_MISSING_HINT))
    if not isinstance(raw, dict) or raw.get("v") != 1:
        return _Reading(PiState("absent", PI_STATE_MISSING_HINT))

    launch_token = str(raw.get("launch_token") or "")
    instance_id = str(raw.get("runtime_instance_id") or "")
    seq = _coerce_int(raw.get("seq"))
    reading = _Reading(
        PiState("absent", PI_STATE_MISSING_HINT),
        launch_token=launch_token,
        instance_id=instance_id,
        seq=-1 if seq is None else seq,
    )

    def refused(status: str, reason: str) -> _Reading:
        return _Reading(PiState(status, reason), launch_token=launch_token,
                        instance_id=instance_id, seq=reading.seq)

    # Every field is then read with its own type check: a missing key fails that check, so key
    # presence needs no separate test - the values the extension can write are the contract.
    updated_raw = raw.get("updated_at")
    if (
        not isinstance(raw.get("launch_token"), str) or not launch_token.strip()
        or not isinstance(raw.get("runtime_instance_id"), str) or not instance_id.strip()
        or _coerce_int(raw.get("pid")) is None
        or seq is None or seq < 0
        or not isinstance(raw.get("mode"), str)
        or any(not isinstance(raw.get(key), bool) for key in ("has_ui", "idle", "editor_empty", "prompt_open"))
        or isinstance(updated_raw, bool)
        or not isinstance(updated_raw, (int, float))
    ):
        return _Reading(PiState("absent", PI_STATE_MISSING_HINT))

    # The launch token is the pane association: it only exists in the environment of the Pi this
    # launch started, so a snapshot from another pane can never be accepted here.
    if launch_token != token:
        return refused("mismatch", "Pi state belongs to a different launch")
    pid = _coerce_int(raw.get("pid"))
    if pid is None or not _pid_alive(pid):
        return refused("stale", "Pi is no longer running in this pane")
    updated = _coerce_epoch_seconds(updated_raw)
    if updated is None:
        return refused("absent", PI_STATE_MISSING_HINT)
    if moment - updated > PI_STATE_MAX_AGE_S:
        return refused("stale", "Pi's state stopped updating")
    if updated - moment > PI_STATE_MAX_FUTURE_S:
        return refused("stale", "Pi's state is dated in the future")
    if str(raw.get("mode") or "") != "tui" or raw.get("has_ui") is not True:
        return refused("mismatch", "Pi is not running an interactive session")
    if raw.get("prompt_open") is True:
        return refused("prompt", "Pi is waiting for a decision in its pane")
    if raw.get("idle") is not True:
        return refused("busy", "Pi is working on a request")
    if raw.get("editor_empty") is not True:
        return refused("draft", "Pi has unsent text in its editor")
    return _Reading(
        PiState("ready"),
        launch_token=launch_token,
        instance_id=instance_id,
        seq=reading.seq,
    )


def read_pi_state(
    binding: Mapping[str, Any],
    *,
    now: float | None = None,
) -> PiState:
    """Pi's own readiness report for this binding: ready | busy | draft | prompt | stale | absent | mismatch."""
    return _read_pi_reading(binding, now=now).state


def confirm_pi_ready(
    binding: Mapping[str, Any],
    deadline: float | None = None,
    *,
    timeout_s: float = PI_STATE_CONFIRM_TIMEOUT_S,
    poll_s: float = PI_STATE_CONFIRM_POLL_S,
    now: float | None = None,
) -> PiState:
    """Ready only when a SECOND snapshot from the same runtime also reads ready.

    Two reads with a strictly greater sequence number from the same launch/runtime replace the old
    double screen capture: one sample can catch Pi between a prompt submission and the agent
    actually starting, and a snapshot that never advances means the runtime stopped publishing.
    Waiting is bounded by `deadline` (a monotonic request deadline) and ~2 s.
    """
    baseline = _read_pi_reading(binding, now=now)
    if baseline.state.status != "ready":
        return baseline.state
    end = time.monotonic() + max(0.0, timeout_s)
    if deadline is not None:
        end = min(end, deadline)
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return PiState("stale", "Pi did not report twice in a row")
        # Never sleep past the deadline: the caller's budget is the hard bound, not the poll.
        time.sleep(min(poll_s, remaining))
        reading = _read_pi_reading(binding, now=now)
        if reading.launch_token != baseline.launch_token or reading.instance_id != baseline.instance_id:
            # A different runtime owns the pane now: it needs its own second read.
            if reading.state.status != "ready":
                return reading.state
            baseline = reading
            continue
        if time.monotonic() >= end:
            # The read itself can spend the last of the budget; a ready answer from here would
            # reach the caller after its deadline.
            return PiState("stale", "Pi did not report twice in a row")
        if reading.seq <= baseline.seq:
            continue
        if reading.state.status != "ready":
            return reading.state
        return PiState("ready")


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
    if len(sessions) != 1:
        if not sessions:
            return None, "", "no live Pi session for this project; refusing to send /new"
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
    state = confirm_pi_ready(bound)
    if state.status != "ready":
        return None, "", f"Pi pane is {state.status}: {state.reason}; refusing to send /new"
    return backend, pane_id, ""
