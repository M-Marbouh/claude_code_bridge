from __future__ import annotations

import re
import secrets
from dataclasses import dataclass


REQ_ID_PREFIX = "CCB_REQ_ID:"
BEGIN_PREFIX = "CCB_BEGIN:"
DONE_PREFIX = "CCB_DONE:"

DONE_LINE_RE_TEMPLATE = r"^\s*CCB_DONE:\s*{req_id}\s*$"

_TRAILING_DONE_TAG_RE = re.compile(
    r"^\s*(?!CCB_DONE\s*:)[A-Z][A-Z0-9_]*_DONE(?:\s*:\s*\d{8}-\d{6}-\d{3}-\d+-\d+)?\s*$"
)
_ANY_CCB_DONE_LINE_RE = re.compile(r"^\s*CCB_DONE:\s*\d{8}-\d{6}-\d{3}-\d+-\d+\s*$")


def _is_trailing_noise_line(line: str) -> bool:
    if (line or "").strip() == "":
        return True
    # Some harnesses append a generic completion tag after the requested CCB_DONE line.
    # Treat it as ignorable trailer, not as a completion marker for our protocol.
    return bool(_TRAILING_DONE_TAG_RE.match(line or ""))


def strip_trailing_markers(text: str) -> str:
    """
    Remove trailing protocol/harness marker lines (blank lines, `CCB_DONE: <id>`, and other `*_DONE` tags).

    This is meant for "recall"/display commands (e.g. `cpend`) where we want a clean view of the reply.
    """
    lines = [ln.rstrip("\n") for ln in (text or "").splitlines()]
    while lines:
        last = lines[-1]
        if _is_trailing_noise_line(last) or _ANY_CCB_DONE_LINE_RE.match(last or ""):
            lines.pop()
            continue
        break
    return "\n".join(lines).rstrip()


_req_id_counter = 0


def make_req_id() -> str:
    # Use readable datetime-PID-counter format with millisecond precision
    # Format: YYYYMMDD-HHMMSS-mmm-PID-counter (e.g., 20260125-143000-123-12345-0)
    global _req_id_counter
    import os
    from datetime import datetime
    now = datetime.now()
    ms = now.microsecond // 1000
    _req_id_counter += 1
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{ms:03d}-{os.getpid()}-{_req_id_counter}"


def wrap_codex_prompt(message: str, req_id: str) -> str:
    message = (message or "").rstrip()
    return (
        f"{REQ_ID_PREFIX} {req_id}\n\n"
        f"{message}\n\n"
        "IMPORTANT:\n"
        "- Reply normally.\n"
        "- Reply normally, in English.\n"
        "- The marker-bearing reply is the sole delivered result; make it self-contained and do not repeat interim reports.\n"
        "- End your reply with this exact final line (verbatim, on its own line). Nothing may follow it:\n"
        f"{DONE_PREFIX} {req_id}\n"
    )


def wrap_codex_delivery_prompt(message: str, req_id: str) -> str:
    """Wrap an asynchronous peer delivery without arming reply capture."""
    message = (message or "").rstrip()
    return (
        f"{REQ_ID_PREFIX} {req_id}\n\n"
        f"{message}\n\n"
        "This is an asynchronous peer message delivered by CCB.\n"
        "Read CCB_PEER_INTENT, CCB_REPLY_EXPECTED, CCB_REPLY_TARGET, and CCB_REPLY_PROVIDER.\n"
        "If CCB_REPLY_EXPECTED is no, do not send a reverse peer message; embedded questions are informational.\n"
        "If a reply is expected, send it explicitly with ask --peer to CCB_REPLY_TARGET using CCB_REPLY_PROVIDER.\n"
        "Preserve CCB_PEER_TASK_ID as --reply-to. Use --notify only for a terminal result with no follow-up question; otherwise use --background.\n"
        "Do not narrate CCB transport markers or confirmation diagnostics unless delivery fails.\n"
        "Do not produce a local CCB_DONE for this delivery; CCB is not capturing this turn automatically.\n"
    )


def done_line_re(req_id: str) -> re.Pattern[str]:
    return re.compile(DONE_LINE_RE_TEMPLATE.format(req_id=re.escape(req_id)))


def find_done_line(text: str, req_id: str) -> int | None:
    """Index of the last line that is exactly `CCB_DONE: <req_id>`, or None.

    Only a whole line counts: a marker quoted mid-sentence or in backticks
    never matches.
    """
    pattern = done_line_re(req_id)
    lines = (text or "").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if pattern.match(lines[i] or ""):
            return i
    return None


def _inline_done_suffix(line: str, req_id: str) -> re.Match[str] | None:
    """Match an exact request marker appended after whitespace on a line."""
    pattern = re.compile(rf"\s+CCB_DONE:\s*{re.escape(req_id)}\s*$")
    return pattern.search(line or "")


def _last_non_noise_line(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if not _is_trailing_noise_line(lines[index]):
            return index
    return None


def is_done_text(
    text: str,
    req_id: str,
    *,
    turn_ended: bool = False,
    allow_inline: bool = False,
) -> bool:
    """Whether `text` completes `req_id`.

    While the provider may still be writing, the marker must be the last
    meaningful line: an earlier marker could be followed by more reply, and
    accepting it would cut the reply short. Once the caller has seen the
    provider's turn end (`turn_ended=True`), a whole-line marker anywhere in
    the text completes the request. Callers may separately opt into accepting
    an exact marker appended to the last meaningful line with `allow_inline`,
    but only after setting `turn_ended=True`.
    Text after a whole-line marker is ignored for the verdict.
    """
    if turn_ended:
        if find_done_line(text, req_id) is not None:
            return True
        lines = (text or "").splitlines()
        last = _last_non_noise_line(lines)
        return (
            allow_inline
            and last is not None
            and _inline_done_suffix(lines[last], req_id) is not None
        )
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    for i in range(len(lines) - 1, -1, -1):
        if _is_trailing_noise_line(lines[i]):
            continue
        return bool(done_line_re(req_id).match(lines[i]))
    return False


def split_done_text(
    text: str,
    req_id: str,
    *,
    allow_inline: bool = False,
) -> tuple[str, str]:
    """Split `text` at its last whole-line marker, or opted-in inline suffix.

    Returns (reply before the marker, text after it). Harness `*_DONE`
    trailers and blank lines after the marker are not reported as trailing
    text. Without a marker, returns (strip_done_text(text), "").
    """
    lines = (text or "").splitlines()
    idx = find_done_line(text, req_id)
    if idx is not None:
        body = "\n".join(lines[:idx]).rstrip()
        trailing = [ln for ln in lines[idx + 1 :] if not _is_trailing_noise_line(ln)]
        return body, "\n".join(trailing).strip()

    last = _last_non_noise_line(lines) if allow_inline else None
    if last is not None:
        match = _inline_done_suffix(lines[last], req_id)
        if match is not None:
            lines[last] = lines[last][: match.start()].rstrip()
            return "\n".join(lines[: last + 1]).rstrip(), ""
    return strip_done_text(text, req_id), ""


def append_trailing_notice(reply: str, trailing: str) -> str:
    """Tell the sender about text the provider wrote after its done marker."""
    trailing = (trailing or "").strip()
    if not trailing:
        return reply
    notice = (
        "[CCB warning: the provider kept writing after its CCB_DONE line. "
        "That text is not part of the reply; it is shown here so nothing is lost.]"
    )
    return f"{(reply or '').rstrip()}\n\n{notice}\n{trailing}".strip()


def strip_done_text(text: str, req_id: str, *, allow_inline: bool = False) -> str:
    lines = [ln.rstrip("\n") for ln in (text or "").splitlines()]
    if not lines:
        return ""

    while lines and _is_trailing_noise_line(lines[-1]):
        lines.pop()

    if lines and allow_inline:
        match = _inline_done_suffix(lines[-1], req_id)
        if match is not None:
            lines[-1] = lines[-1][: match.start()].rstrip()

    if lines and done_line_re(req_id).match(lines[-1] or ""):
        lines.pop()

    while lines and _is_trailing_noise_line(lines[-1]):
        lines.pop()

    return "\n".join(lines).rstrip()


def extract_reply_for_req(text: str, req_id: str, *, allow_inline: bool = False) -> str:
    """
    Extract the reply segment for req_id from a message.

    When multiple replies are present (each ending with CCB_DONE: <req_id>),
    extract only the segment between the previous done line and the done line
    for our req_id. This prevents mixing old/stale content into the current reply.
    `allow_inline` is for callers that already established turn completion.
    """
    lines = [ln.rstrip("\n") for ln in (text or "").splitlines()]
    if not lines:
        return ""

    # Find all done-line indices and target req_id indices
    target_re = re.compile(rf"^\s*CCB_DONE:\s*{re.escape(req_id)}\s*$", re.IGNORECASE)
    done_idxs = [i for i, ln in enumerate(lines) if _ANY_CCB_DONE_LINE_RE.match(ln or "")]
    target_idxs = [i for i in done_idxs if target_re.match(lines[i] or "")]

    if not target_idxs:
        last_non_noise = _last_non_noise_line(lines) if allow_inline else None
        inline_target = (
            _inline_done_suffix(lines[last_non_noise], req_id)
            if last_non_noise is not None
            else None
        )
        if inline_target is not None:
            prev_done_i = max((i for i in done_idxs if i < last_non_noise), default=-1)
            segment = lines[prev_done_i + 1 : last_non_noise + 1]
            segment[-1] = segment[-1][: inline_target.start()].rstrip()
            while segment and segment[0].strip() == "":
                segment = segment[1:]
            return "\n".join(segment).rstrip()
        # No CCB_DONE for our req_id found
        # If there are other CCB_DONE markers, this is likely old content - return empty
        # to avoid mixing old replies into current request
        if done_idxs:
            return ""  # Prevent returning old content
        # No CCB_DONE markers at all - fallback to strip behavior
        return strip_done_text(text, req_id, allow_inline=allow_inline)

    # Find the last occurrence of our req_id's done line
    target_i = target_idxs[-1]

    # Find the previous done line (any req_id)
    prev_done_i = -1
    for i in reversed(done_idxs):
        if i < target_i:
            prev_done_i = i
            break

    # Extract segment between previous done and our done
    segment = lines[prev_done_i + 1 : target_i]

    # Trim leading/trailing blank lines
    while segment and segment[0].strip() == "":
        segment = segment[1:]
    while segment and segment[-1].strip() == "":
        segment = segment[:-1]

    return "\n".join(segment).rstrip()


def select_codex_reply(
    terminal_reply: str | None,
    latest_final: str | None,
    combined: str,
    req_id: str,
) -> str:
    """Select the single Codex reply that belongs to a completed request."""
    if terminal_reply:
        selected, trailing = split_done_text(terminal_reply, req_id, allow_inline=True)
        if selected:
            return append_trailing_notice(selected, trailing)
    if latest_final:
        selected, trailing = split_done_text(latest_final, req_id, allow_inline=True)
        return append_trailing_notice(selected, trailing)
    return extract_reply_for_req(combined, req_id, allow_inline=True)


@dataclass(frozen=True)
class CaskdRequest:
    client_id: str
    work_dir: str
    timeout_s: float
    quiet: bool
    message: str
    output_path: str | None = None
    req_id: str | None = None
    caller: str = "claude"


@dataclass(frozen=True)
class CaskdResult:
    exit_code: int
    reply: str
    req_id: str
    session_key: str
    log_path: str | None
    anchor_seen: bool
    done_seen: bool
    fallback_scan: bool
    anchor_ms: int | None = None
    done_ms: int | None = None
