from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ccb_protocol import (
    BEGIN_PREFIX,
    DONE_PREFIX,
    REQ_ID_PREFIX,
    is_done_text,
    make_req_id,
    strip_done_text,
)

# Match new req_id format: YYYYMMDD-HHMMSS-mmm-PID-counter
ANY_DONE_LINE_RE = re.compile(r"^\s*CCB_DONE:\s*\d{8}-\d{6}-\d{3}-\d+-\d+\s*$", re.IGNORECASE)
_SKILL_CACHE: str | None = None


def _wants_markdown_table(message: str) -> bool:
    msg = (message or "").lower()
    if "markdown" not in msg:
        return False
    return ("table" in msg) or ("\u8868\u683c" in message)


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    val = raw.strip().lower()
    if val in {"0", "false", "no", "off"}:
        return False
    if val in {"1", "true", "yes", "on"}:
        return True
    return default


def _language_hint() -> str:
    lang = (os.environ.get("CCB_REPLY_LANG") or os.environ.get("CCB_LANG") or "").strip().lower()
    if lang in {"zh", "cn", "chinese"}:
        return "Reply in Chinese."
    if lang in {"en", "english"}:
        return "Reply in English."
    return ""


def _load_claude_skills() -> str:
    global _SKILL_CACHE
    if _SKILL_CACHE is not None:
        return _SKILL_CACHE
    if not _env_bool("CCB_CLAUDE_SKILLS", True):
        _SKILL_CACHE = ""
        return _SKILL_CACHE
    skills_dir = Path(__file__).resolve().parent.parent / "claude_skills"
    if not skills_dir.is_dir():
        _SKILL_CACHE = ""
        return _SKILL_CACHE
    parts: list[str] = []
    # Load short skill files.
    for name in ("ask.md",):
        path = skills_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if text:
            parts.append(text)
    _SKILL_CACHE = "\n\n".join(parts).strip()
    return _SKILL_CACHE


def _reply_bounds(lines: list[str], req_id: str) -> tuple[int, int | None] | None:
    """Locate this request's reply as (first line, done-line index or None).

    Pairs the latest `CCB_BEGIN: <req_id>` that is followed by a done line
    with the FIRST done line after it. When a stalled request is released
    turns later by a bare `CCB_DONE`, this keeps the original reply and
    drops the unrelated turns in between. A BEGIN with no done line yet
    (the reply was never finished) runs to the end of the text.
    """
    target_re = re.compile(rf"^\s*CCB_DONE:\s*{re.escape(req_id)}\s*$", re.IGNORECASE)
    begin_re = re.compile(rf"^\s*{re.escape(BEGIN_PREFIX)}\s*{re.escape(req_id)}\s*$", re.IGNORECASE)
    target_idxs = [i for i, ln in enumerate(lines) if target_re.match(ln or "")]
    done_idxs = sorted(set(target_idxs) | {i for i, ln in enumerate(lines) if ANY_DONE_LINE_RE.match(ln or "")})
    begin_idxs = [i for i, ln in enumerate(lines) if begin_re.match(ln or "")]

    for begin_i in reversed(begin_idxs):
        after = [i for i in target_idxs if i > begin_i]
        if after:
            return begin_i + 1, after[0]
    if target_idxs:
        # No BEGIN line: the reply runs from the previous done line (any req_id).
        target_i = target_idxs[-1]
        prev_done_i = max((i for i in done_idxs if i < target_i), default=-1)
        return prev_done_i + 1, target_i
    if begin_idxs:
        return begin_idxs[-1] + 1, None
    return None


def _trim_blank_edges(segment: list[str]) -> str:
    while segment and segment[0].strip() == "":
        segment = segment[1:]
    while segment and segment[-1].strip() == "":
        segment = segment[:-1]
    return "\n".join(segment).rstrip()


def extract_reply_for_req(text: str, req_id: str) -> str:
    """
    Extract the reply segment for req_id from Claude's assistant text.

    Claude sometimes emits multiple replies in a single assistant message, each ending with its own
    `CCB_DONE: <req_id>` line; each request gets only its own segment. Text after the done line is
    never part of the reply (see `extract_trailing_for_req`).
    """
    lines = [ln.rstrip("\n") for ln in (text or "").splitlines()]
    if not lines:
        return ""
    bounds = _reply_bounds(lines, req_id)
    if bounds is None:
        # No markers for this request at all: keep the legacy strip behavior.
        return strip_done_text(text, req_id)
    start, done_i = bounds
    return _trim_blank_edges(lines[start : done_i if done_i is not None else len(lines)])


def extract_trailing_for_req(text: str, req_id: str) -> str:
    """Text Claude wrote after this request's done line, up to its next marker."""
    lines = [ln.rstrip("\n") for ln in (text or "").splitlines()]
    bounds = _reply_bounds(lines, req_id)
    if bounds is None or bounds[1] is None:
        return ""
    trailing: list[str] = []
    for ln in lines[bounds[1] + 1 :]:
        if ANY_DONE_LINE_RE.match(ln or "") or ln.strip().startswith(BEGIN_PREFIX):
            break
        trailing.append(ln)
    return _trim_blank_edges(trailing)


def wrap_claude_prompt(message: str, req_id: str) -> str:
    message = (message or "").rstrip()
    skills = _load_claude_skills()
    if skills:
        message = f"{skills}\n\n{message}".strip()
    extra_lines: list[str] = []
    if _wants_markdown_table(message):
        extra_lines.append("If asked for a Markdown table, output only pipe-and-dash Markdown table syntax (no box-drawing characters).")
    lang_hint = _language_hint()
    if lang_hint:
        extra_lines.append(lang_hint)
    extra = "\n".join(extra_lines).strip()
    if extra:
        extra = f"{extra}\n\n"
    return (
        f"{REQ_ID_PREFIX} {req_id}\n\n"
        f"{message}\n\n"
        f"{extra}"
        "Reply using exactly this format:\n"
        f"{BEGIN_PREFIX} {req_id}\n"
        "<reply>\n"
        f"{DONE_PREFIX} {req_id}\n"
        "\n"
        f"Nothing may follow the {DONE_PREFIX} line; put any other notes before {BEGIN_PREFIX}.\n"
    )


def wrap_claude_delivery_prompt(message: str, req_id: str) -> str:
    message = (message or "").rstrip()
    return (
        f"{REQ_ID_PREFIX} {req_id}\n\n"
        f"{message}\n\n"
        "This is an asynchronous peer message delivered by CCB.\n"
        "Read CCB_PEER_INTENT and CCB_REPLY_EXPECTED in the message.\n"
        "If CCB_REPLY_EXPECTED is no, do not send a reverse peer message; embedded questions are informational.\n"
        "If a reply is expected, preserve CCB_PEER_TASK_ID as --reply-to. Use ask --peer --notify only for a terminal result with no follow-up question; otherwise use --background.\n"
        "Do not narrate CCB transport markers or confirmation diagnostics to the user unless delivery fails.\n"
        "Do not wait to produce a local CCB_DONE for this delivery.\n"
    )


@dataclass(frozen=True)
class LaskdRequest:
    client_id: str
    work_dir: str
    timeout_s: float
    quiet: bool
    message: str
    output_path: str | None = None
    req_id: str | None = None
    no_wrap: bool = False


@dataclass(frozen=True)
class LaskdResult:
    exit_code: int
    reply: str
    req_id: str
    session_key: str
    done_seen: bool
    done_ms: int | None = None
    anchor_seen: bool = False
    fallback_scan: bool = False
    anchor_ms: int | None = None


__all__ = [
    "wrap_claude_prompt",
    "extract_reply_for_req",
    "extract_trailing_for_req",
    "LaskdRequest",
    "LaskdResult",
    "make_req_id",
    "is_done_text",
    "strip_done_text",
]
