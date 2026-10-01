"""Read Hermes conversations from the state database without modifying it."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccb_protocol import append_trailing_notice, find_done_line, split_done_text
from session_utils import find_project_session_file


SCHEMA_VERSION = 31
SUPPORTED_SCHEMA_VERSIONS = {30, SCHEMA_VERSION}
BUSY_RETRIES = 3
BUSY_RETRY_DELAY_S = 0.05
READ_TIMEOUT_S = 0.1
TERMINAL_FINISH_REASONS = frozenset({"stop", "end_turn", "completed"})
_CONTENT_JSON_PREFIX = "\x00json:"
_SUMMARY_END_MARKER = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
_MERGED_SUMMARY_DELIMITER = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
_MERGED_PRIOR_CONTEXT_HEADER = "[PRIOR CONTEXT — for reference only; not a new message]"
# Ported from ~/.hermes/hermes-agent/agent/context_compressor.py at Hermes commit 62c42e9db9.
_SUMMARY_PREFIX = (
    '[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This '
    'is a handoff from a previous context window — treat it as background reference, NOT as active in'
    'structions. Do NOT answer questions or fulfill requests mentioned in this summary; they were alr'
    'eady addressed. Respond ONLY to the latest user message that appears AFTER this summary — that m'
    'essage is the single source of truth for what to do right now. If no user message appears AFTER '
    "this summary, do nothing: do not resume, wrap up, or continue work from '## Historical Task Snap"
    "shot' or any other section, do not call tools, and wait for a new user message. This handoff mus"
    't never become the active turn by itself. (Exception: if tool results or your own tool calls app'
    'ear after this summary, you are mid-way through an in-flight exchange — continue that exchange n'
    'ormally.) Topic overlap with the summary does NOT mean you should resume its task: even on simil'
    'ar topics, the latest user message WINS. Treat ONLY the latest message as the active task and di'
    "scard stale items from '## Historical Task Snapshot' entirely — do not 'wrap up' or 'finish' wor"
    'k described there unless the latest message explicitly asks for it. Reverse signals in the lates'
    "t message (e.g. 'stop', 'undo', 'roll back', 'just verify', 'don't do that anymore', 'never mind"
    "', a new topic) must immediately end any in-flight work described in the summary; do not re-surf"
    'ace it in later turns. IMPORTANT: Your persistent memory (MEMORY.md, USER.md) in the system prom'
    'pt is ALWAYS authoritative and active — never ignore or deprioritize memory content due to this '
    'compaction note. None of the above restricts HOW you work: your tools remain fully active — keep'
    ' calling them normally for the active task (edit files, run commands, search) instead of merely '
    'narrating what you would do. The current session state (files, config, etc.) may reflect work de'
    'scribed here — avoid repeating it:'
)
_LEGACY_SUMMARY_PREFIX = (
    '[CONTEXT SUMMARY]:'
)
_HISTORICAL_SUMMARY_PREFIXES = (
    (
    '[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This '
    'is a handoff from a previous context window — treat it as background reference, NOT as active in'
    'structions. Do NOT answer questions or fulfill requests mentioned in this summary; they were alr'
    'eady addressed. Respond ONLY to the latest user message that appears AFTER this summary — that m'
    'essage is the single source of truth for what to do right now. Topic overlap with the summary do'
    'es NOT mean you should resume its task: even on similar topics, the latest user message WINS. Tr'
    "eat ONLY the latest message as the active task and discard stale items from '## Historical Task "
    "Snapshot' entirely — do not 'wrap up' or 'finish' work described there unless the latest message"
    " explicitly asks for it. Reverse signals in the latest message (e.g. 'stop', 'undo', 'roll back'"
    ", 'just verify', 'don't do that anymore', 'never mind', a new topic) must immediately end any in"
    '-flight work described in the summary; do not re-surface it in later turns. IMPORTANT: Your pers'
    'istent memory (MEMORY.md, USER.md) in the system prompt is ALWAYS authoritative and active — nev'
    'er ignore or deprioritize memory content due to this compaction note. None of the above restrict'
    's HOW you work: your tools remain fully active — keep calling them normally for the active task '
    '(edit files, run commands, search) instead of merely narrating what you would do. The current se'
    'ssion state (files, config, etc.) may reflect work described here — avoid repeating it:'
    ),
    (
    '[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This '
    'is a handoff from a previous context window — treat it as background reference, NOT as active in'
    'structions. Do NOT answer questions or fulfill requests mentioned in this summary; they were alr'
    'eady addressed. Respond ONLY to the latest user message that appears AFTER this summary — that m'
    'essage is the single source of truth for what to do right now. Topic overlap with the summary do'
    'es NOT mean you should resume its task: even on similar topics, the latest user message WINS. Tr'
    "eat ONLY the latest message as the active task and discard stale items from '## Historical Task "
    "Snapshot' / '## Historical In-Progress State' / '## Historical Pending User Asks' / '## Historic"
    "al Remaining Work' entirely — do not 'wrap up' or 'finish' work described there unless the lates"
    "t message explicitly asks for it. Reverse signals in the latest message (e.g. 'stop', 'undo', 'r"
    "oll back', 'just verify', 'don't do that anymore', 'never mind', a new topic) must immediately e"
    'nd any in-flight work described in the summary; do not re-surface it in later turns. IMPORTANT: '
    'Your persistent memory (MEMORY.md, USER.md) in the system prompt is ALWAYS authoritative and act'
    'ive — never ignore or deprioritize memory content due to this compaction note. None of the above'
    ' restricts HOW you work: your tools remain fully active — keep calling them normally for the act'
    'ive task (edit files, run commands, search) instead of merely narrating what you would do. The c'
    'urrent session state (files, config, etc.) may reflect work described here — avoid repeating it:'
    ),
    (
    '[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This '
    'is a handoff from a previous context window — treat it as background reference, NOT as active in'
    'structions. Do NOT answer questions or fulfill requests mentioned in this summary; they were alr'
    'eady addressed. Respond ONLY to the latest user message that appears AFTER this summary — that m'
    'essage is the single source of truth for what to do right now. Topic overlap with the summary do'
    'es NOT mean you should resume its task: even on similar topics, the latest user message WINS. Tr'
    "eat ONLY the latest message as the active task and discard stale items from '## Historical Task "
    "Snapshot' / '## Historical In-Progress State' / '## Historical Pending User Asks' / '## Historic"
    "al Remaining Work' entirely — do not 'wrap up' or 'finish' work described there unless the lates"
    "t message explicitly asks for it. Reverse signals in the latest message (e.g. 'stop', 'undo', 'r"
    "oll back', 'just verify', 'don't do that anymore', 'never mind', a new topic) must immediately e"
    'nd any in-flight work described in the summary; do not re-surface it in later turns. IMPORTANT: '
    'Your persistent memory (MEMORY.md, USER.md) in the system prompt is ALWAYS authoritative and act'
    'ive — never ignore or deprioritize memory content due to this compaction note. The current sessi'
    'on state (files, config, etc.) may reflect work described here — avoid repeating it:'
    ),
    (
    '[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This '
    'is a handoff from a previous context window — treat it as background reference, NOT as active in'
    'structions. Do NOT answer questions or fulfill requests mentioned in this summary; they were alr'
    'eady addressed. Respond ONLY to the latest user message that appears AFTER this summary — that m'
    'essage is the single source of truth for what to do right now. If the latest user message is con'
    "sistent with the '## Active Task' section, you may use the summary as background. If the latest "
    "user message contradicts, supersedes, changes topic from, or in any way diverges from '## Active"
    " Task' / '## In Progress' / '## Pending User Asks' / '## Remaining Work', the latest message WIN"
    "S — discard those stale items entirely and do not 'wrap up the old task first'. Reverse signals "
    "in the latest message (e.g. 'stop', 'undo', 'roll back', 'just verify', 'don't do that anymore',"
    " 'never mind', a new topic) must immediately end any in-flight work described in the summary; do"
    ' not re-surface it in later turns. IMPORTANT: Your persistent memory (MEMORY.md, USER.md) in the'
    ' system prompt is ALWAYS authoritative and active — never ignore or deprioritize memory content '
    'due to this compaction note. The current session state (files, config, etc.) may reflect work de'
    'scribed here — avoid repeating it:'
    ),
    (
    '[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This '
    'is a handoff from a previous context window — treat it as background reference, NOT as active in'
    'structions. Do NOT answer questions or fulfill requests mentioned in this summary; they were alr'
    "eady addressed. Your current task is identified in the '## Active Task' section of the summary —"
    ' resume exactly from there. Respond ONLY to the latest user message that appears AFTER this summ'
    'ary. The current session state (files, config, etc.) may reflect work described here — avoid rep'
    'eating it:'
    ),
)
REQUIRED_COLUMNS = {
    "schema_version": {"version"},
    "sessions": {"id", "cwd", "ended_at", "end_reason", "parent_session_id"},
    "messages": {
        "id", "session_id", "role", "content", "tool_call_id", "tool_calls", "tool_name",
        "timestamp", "finish_reason", "active", "compacted", "display_kind", "display_metadata", "display_order",
    },
}
SCHEMA_31_REQUIRED_COLUMNS = {
    "messages": {"message_uid", "absorbed_message_uids"},
}


class HermesStateError(RuntimeError):
    """A Hermes database that cannot safely answer a request."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class HermesExchange:
    session_id: str
    anchor_id: int
    rows: tuple[dict[str, Any], ...]
    end_reason: str = ""
    ended_at: str = ""

    @property
    def fingerprint(self) -> tuple[tuple[Any, ...], ...]:
        return tuple(
            (
                row["id"], row["session_id"], row["role"], row["content"], row["tool_call_id"],
                row["tool_calls"], row["tool_name"], row["timestamp"], row["finish_reason"],
                row["active"], row["compacted"], row["display_kind"], row["display_metadata"],
                row["display_order"],
            )
            for row in self.rows
        )

    @property
    def terminal_row(self) -> dict[str, Any] | None:
        for row in self.rows:
            finish = str(row.get("finish_reason") or "").strip().lower()
            if row.get("role") != "assistant" or _has_tool_calls(row.get("tool_calls")) or _model_only(row):
                continue
            if not (int(row.get("active") or 0) or int(row.get("compacted") or 0)):
                continue
            if finish not in TERMINAL_FINISH_REASONS:
                continue
            if find_done_line(_content_text(row.get("content")), row["req_id"]) is not None:
                return row
        return None

    def reply(self, req_id: str) -> str | None:
        row = self.terminal_row
        if row is None or row.get("req_id") != req_id:
            return None
        reply, trailing = split_done_text(_content_text(row.get("content")), req_id)
        return append_trailing_notice(reply, trailing)


def hermes_home_from_env(env: dict[str, str] | None = None) -> Path:
    """Match Hermes process-level HERMES_HOME resolution for a launched pane."""
    values = os.environ if env is None else env
    raw = str(values.get("HERMES_HOME") or "").strip()
    if not raw:
        suffix = str(values.get("HERMES_DATA_DIR_SUFFIX") or "")
        if sys.platform == "win32":
            base = Path(values.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            return base / ("hermes" + suffix)
        return Path.home() / (".hermes" + suffix)
    expanded = re.sub(
        r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
        lambda match: str(values.get(match.group(1) or match.group(2), match.group(0))),
        raw,
    )
    home = str(values.get("HOME") or Path.home())
    if expanded == "~" or expanded.startswith("~/") or expanded.startswith("~\\"):
        expanded = home + expanded[1:]
    return Path(expanded)


def session_home(data: dict[str, Any]) -> Path:
    raw = str(data.get("hermes_home") or "").strip()
    if not raw:
        raise HermesStateError("unavailable", "Hermes session file has no recorded hermes_home")
    return Path(raw).expanduser()


def session_db_path(data: dict[str, Any]) -> Path:
    home = session_home(data)
    raw = str(data.get("state_db_path") or "").strip()
    path = Path(raw).expanduser() if raw else home / "state.db"
    if path != home / "state.db":
        raise HermesStateError("incompatible", "Hermes session state DB path does not match its recorded home")
    return path


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = db_path.expanduser().absolute().as_uri() + "?mode=ro"
    last: Exception | None = None
    for attempt in range(BUSY_RETRIES):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT_S)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            return conn
        except sqlite3.OperationalError as exc:
            last = exc
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message and "i/o error" not in message:
                kind = "unavailable" if "unable to open" in message or "no such file" in message else "incompatible"
                raise HermesStateError(kind, f"Cannot open Hermes state DB {db_path}: {exc}") from exc
            if attempt + 1 < BUSY_RETRIES:
                time.sleep(BUSY_RETRY_DELAY_S)
    raise HermesStateError("busy", f"Hermes state DB remained busy: {db_path}") from last


def _verify_schema(conn: sqlite3.Connection) -> None:
    try:
        tables = {
            str(row[0]): {str(col[1]) for col in conn.execute(f"PRAGMA table_info({row[0]})")}
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table, columns in REQUIRED_COLUMNS.items():
            if table not in tables or not columns.issubset(tables[table]):
                missing = sorted(columns - tables.get(table, set()))
                raise HermesStateError("incompatible", f"Hermes state DB lacks {table} columns: {', '.join(missing)}")
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None or int(row[0]) not in SUPPORTED_SCHEMA_VERSIONS:
            found = "missing" if row is None else str(row[0])
            supported = ", ".join(str(version) for version in sorted(SUPPORTED_SCHEMA_VERSIONS))
            raise HermesStateError("incompatible", f"Hermes state DB schema version {found}; supported: {supported}")
        if int(row[0]) == SCHEMA_VERSION:
            for table, columns in SCHEMA_31_REQUIRED_COLUMNS.items():
                if not columns.issubset(tables.get(table, set())):
                    missing = sorted(columns - tables.get(table, set()))
                    raise HermesStateError("incompatible", f"Hermes state DB lacks {table} columns: {', '.join(missing)}")
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "locked" in message or "busy" in message or "i/o error" in message:
            raise HermesStateError("busy", f"Hermes state DB remained busy during schema check: {exc}") from exc
        raise HermesStateError("incompatible", f"Cannot inspect Hermes state DB schema: {exc}") from exc


def _content_text(content: Any) -> str:
    if isinstance(content, str) and content.startswith(_CONTENT_JSON_PREFIX):
        try:
            content = json.loads(content[len(_CONTENT_JSON_PREFIX):])
        except (TypeError, ValueError):
            pass
    if isinstance(content, str):
        return content
    if isinstance(content, (list, dict)):
        def collect(value: Any) -> list[str]:
            if isinstance(value, str):
                return [value]
            if isinstance(value, list):
                return [part for item in value for part in collect(item)]
            if isinstance(value, dict):
                text = value.get("text")
                if isinstance(text, str):
                    return [text]
                return [part for item in value.values() for part in collect(item)]
            return []
        return "\n".join(collect(content))
    return ""


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("display_metadata")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _model_only(row: dict[str, Any]) -> bool:
    return bool(_metadata(row).get("model_only"))


def _is_compressed_summary(row: dict[str, Any]) -> bool:
    return bool(row.get("_compressed_summary") or _metadata(row).get("_compressed_summary"))


def _content_part_text(part: Any) -> str | None:
    if isinstance(part, str):
        return part
    if isinstance(part, dict) and isinstance(part.get("text"), str):
        return part["text"]
    return None


def _with_part_text(part: Any, text: str) -> Any:
    return {**part, "text": text} if isinstance(part, dict) else text


def _content_text_for_contains(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(value for value in (_content_part_text(part) for part in content) if value)
    return "" if content is None else content if isinstance(content, str) else str(content)


def _starts_with_summary_prefix(text: str) -> bool:
    return text.startswith((_SUMMARY_PREFIX, _LEGACY_SUMMARY_PREFIX, *_HISTORICAL_SUMMARY_PREFIXES))


def _looks_like_handoff(content: Any) -> bool:
    text = _content_text_for_contains(content).lstrip()
    if _MERGED_SUMMARY_DELIMITER in text:
        after = text.split(_MERGED_SUMMARY_DELIMITER, 1)[1].lstrip()
        return _starts_with_summary_prefix(after)
    return _starts_with_summary_prefix(text)


def _strip_context_summary_handoff_message(message: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return message
    if not message.get("_compressed_summary") and not _looks_like_handoff(message.get("content")):
        return message.copy()
    content = message.get("content")

    def unwrapped(new_content: Any) -> dict[str, Any]:
        result = {**message, "content": new_content}
        result.pop("_compressed_summary", None)
        return result

    if isinstance(content, str):
        if _MERGED_SUMMARY_DELIMITER in content:
            prior = content.split(_MERGED_SUMMARY_DELIMITER, 1)[0].strip()
            if prior.startswith(_MERGED_PRIOR_CONTEXT_HEADER):
                prior = prior[len(_MERGED_PRIOR_CONTEXT_HEADER):].lstrip()
        elif _SUMMARY_END_MARKER in content:
            prior = content.split(_SUMMARY_END_MARKER, 1)[1].lstrip()
        else:
            prior = ""
        return unwrapped(prior) if prior else None
    if isinstance(content, list):
        prior_blocks: list[Any] = []
        found_delimiter = False
        for item in content:
            text = _content_part_text(item)
            if isinstance(text, str) and _MERGED_SUMMARY_DELIMITER in text:
                before = text.split(_MERGED_SUMMARY_DELIMITER, 1)[0]
                if before.strip():
                    prior_blocks.append(_with_part_text(item, before))
                found_delimiter = True
                break
            prior_blocks.append(item.copy() if isinstance(item, dict) else item)
        if not found_delimiter:
            for index, item in enumerate(content):
                text = _content_part_text(item)
                if isinstance(text, str) and _SUMMARY_END_MARKER in text:
                    remainder = text.split(_SUMMARY_END_MARKER, 1)[1].lstrip()
                    live_blocks = [_with_part_text(item, remainder)] if remainder else []
                    live_blocks += [later.copy() if isinstance(later, dict) else later for later in content[index + 1:]]
                    return unwrapped(live_blocks) if live_blocks else None
            return None

        for index, item in enumerate(prior_blocks):
            text = _content_part_text(item)
            if isinstance(text, str) and text.lstrip().startswith(_MERGED_PRIOR_CONTEXT_HEADER):
                prior = text.lstrip()[len(_MERGED_PRIOR_CONTEXT_HEADER):].lstrip()
                if prior:
                    prior_blocks[index] = _with_part_text(item, prior)
                else:
                    prior_blocks.pop(index)
                break
        return unwrapped(prior_blocks) if prior_blocks else None
    return None


def _live_user_projection(content: Any, *, is_summary: bool = False) -> Any | None:
    """Return the user-visible content using Hermes' summary handoff projection."""
    if isinstance(content, str) and content.startswith(_CONTENT_JSON_PREFIX):
        try:
            content = json.loads(content[len(_CONTENT_JSON_PREFIX):])
        except (TypeError, ValueError):
            pass
    message = {"role": "user", "content": content}
    if is_summary:
        message["_compressed_summary"] = True
    projected = _strip_context_summary_handoff_message(message)
    return projected.get("content") if projected is not None else None

def _has_tool_calls(value: Any) -> bool:
    if value in (None, "", "[]", "{}"):
        return False
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return True
    return bool(value)


def _has_anchor(row: dict[str, Any], req_id: str) -> bool:
    if row.get("role") != "user" or _model_only(row):
        return False
    display_kind = str(row.get("display_kind") or "")
    if display_kind and display_kind not in {"steer", "hidden"}:
        return False
    wanted = f"CCB_REQ_ID: {req_id}"
    content = _live_user_projection(row.get("content"), is_summary=_is_compressed_summary(row))
    return wanted in _content_text(content).splitlines()


def _read_exchange_once(
    db_path: Path,
    work_dir: Path,
    req_id: str,
    *,
    session_id: str = "",
) -> HermesExchange | None:
    """Read an anchored exchange from one read-only, consistent DB snapshot.

    ``session_id`` is supplied for exact pend recovery. Anchors are always checked across all
    project sessions first, so exact recovery cannot switch sessions or hide another anchor.
    """
    db_path = Path(db_path)
    work_dir = Path(work_dir).expanduser().resolve()
    work_dir_real = os.path.realpath(str(work_dir))
    conn = _connect_readonly(db_path)
    try:
        _verify_schema(conn)
        conn.execute("BEGIN")
        session_rows = conn.execute(
            "SELECT id, cwd, ended_at, end_reason, parent_session_id FROM sessions "
            "WHERE cwd IS NOT NULL AND cwd != ''"
        ).fetchall()
        session_by_id = {
            str(row["id"]): dict(row) for row in session_rows
            if os.path.realpath(str(row["cwd"] or "")) == work_dir_real
        }
        if not session_by_id:
            conn.execute("ROLLBACK")
            return None
        ids = tuple(session_by_id)
        marks = ",".join("?" for _ in ids)
        anchor_text = f"CCB_REQ_ID: {req_id}"
        raw_anchors = conn.execute(
            f"SELECT * FROM messages WHERE session_id IN ({marks}) AND role='user' AND instr(content, ?) > 0 ORDER BY id",
            (*ids, anchor_text),
        ).fetchall()
        anchors = [dict(row) for row in raw_anchors if _has_anchor(dict(row), req_id)]
        if not anchors:
            conn.execute("ROLLBACK")
            return None
        anchors_by_session: dict[str, list[dict[str, Any]]] = {}
        for row in anchors:
            owner = str(row["session_id"])
            anchors_by_session.setdefault(owner, []).append(row)

        anchors_by_session = {owner: min(rows, key=lambda row: int(row["id"])) for owner, rows in anchors_by_session.items()}
        session_anchor_groups = list(anchors_by_session.items())
        if len(session_anchor_groups) == 1:
            selected_session, anchor = session_anchor_groups[0]
        elif len(session_anchor_groups) == 2:
            (first_id, first), (second_id, second) = session_anchor_groups
            first_meta = session_by_id[first_id]
            second_meta = session_by_id[second_id]
            if second_meta.get("parent_session_id") == first_id and first_meta.get("end_reason") == "compression":
                selected_session, anchor = first_id, first
            elif first_meta.get("parent_session_id") == second_id and second_meta.get("end_reason") == "compression":
                selected_session, anchor = second_id, second
            else:
                raise HermesStateError("ambiguous", f"Ambiguous Hermes anchor for request {req_id}")
        else:
            raise HermesStateError("ambiguous", f"Ambiguous Hermes anchor for request {req_id}")

        selected_meta = session_by_id.get(selected_session)
        if selected_meta is None:
            raise HermesStateError("unavailable", "Hermes anchor session disappeared from snapshot")
        parent_meta = session_by_id.get(str(selected_meta.get("parent_session_id") or ""))
        if parent_meta and parent_meta.get("end_reason") == "compression":
            raise HermesStateError("rotated", "Hermes request anchor is in a compression child session")
        if session_id and selected_session != session_id:
            conn.execute("ROLLBACK")
            return None

        raw_session_rows = conn.execute(
            "SELECT * FROM messages WHERE session_id=? AND id>? ORDER BY id", (selected_session, int(anchor["id"]))
        ).fetchall()
        exchange_rows = [dict(row, req_id=req_id) for row in raw_session_rows]
        result = HermesExchange(
            session_id=selected_session,
            anchor_id=int(anchor["id"]),
            rows=tuple(exchange_rows),
            end_reason=str(selected_meta.get("end_reason") or ""),
            ended_at=str(selected_meta.get("ended_at") or ""),
        )
        conn.execute("ROLLBACK")
        return result
    except sqlite3.OperationalError as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        message = str(exc).lower()
        if "locked" in message or "busy" in message or "i/o error" in message:
            raise HermesStateError("busy", f"Hermes state DB remained busy: {db_path}") from exc
        raise HermesStateError("incompatible", f"Cannot read Hermes state DB: {exc}") from exc
    finally:
        conn.close()


def read_exchange(
    db_path: Path,
    work_dir: Path,
    req_id: str,
    *,
    session_id: str = "",
) -> HermesExchange | None:
    """Retry brief SQLite contention; keep it distinct from an absent reply."""
    for attempt in range(BUSY_RETRIES):
        try:
            return _read_exchange_once(db_path, work_dir, req_id, session_id=session_id)
        except HermesStateError as exc:
            if exc.kind != "busy" or attempt + 1 == BUSY_RETRIES:
                raise
            time.sleep(BUSY_RETRY_DELAY_S)
    raise AssertionError("bounded Hermes DB retry loop exhausted unexpectedly")


def load_session_file(work_dir: Path, explicit_path: str | Path | None = None) -> tuple[Path, dict[str, Any]] | None:
    path = Path(explicit_path).expanduser() if explicit_path else find_project_session_file(work_dir, ".hermes-session")
    if not path:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return path, data


class HermesCommunicator:
    """Small health probe used by ``ccb-ping hermes``."""

    def ping(self, display: bool = True) -> tuple[bool, str]:
        from terminal import TmuxBackend, WeztermBackend

        work_dir = Path.cwd()
        explicit = os.environ.get("CCB_SESSION_FILE")
        loaded = load_session_file(work_dir, explicit)
        if not loaded:
            return False, "Hermes session file not found"
        _path, data = loaded
        pane_id = str(data.get("pane_id") or "").strip()
        terminal = str(data.get("terminal") or "").strip().lower()
        backend = WeztermBackend() if terminal == "wezterm" else TmuxBackend() if terminal == "tmux" else None
        if not data.get("active") or not pane_id or backend is None or not backend.pane_exists(pane_id):
            return False, "Hermes pane is not active"
        try:
            db_path = session_db_path(data)
            conn = _connect_readonly(db_path)
            try:
                _verify_schema(conn)
            finally:
                conn.close()
        except HermesStateError as exc:
            return False, str(exc)
        return True, "Hermes pane and state DB OK"
