#!/usr/bin/env python3
"""A fake `ssh` used through CCB_SSH_BIN.

It records the argv it was given, then speaks the T1 frame protocol on
stdin/stdout, simulating the destination-side situations the source must
classify honestly. Behaviour is selected by ``CCB_FAKE_SSH_MODE``:

    exit255             exit 255 before writing anything (host down / auth refused)
    eof_before_submit   answer hello, then close stdin/stdout before submit
    major2              answer hello with protocol major 2
    no_delivery         answer hello without delivery-only for the provider
    eof_after_submit    read the submit frame, then close without acknowledging
    ack_ok              hello + a full acceptance
    ack_refused         hello + structured refusal (CCB_FAKE_SSH_CODE)
    ack_unknown         hello + acceptance_unknown
    status_delivered    hello + status reply (state from CCB_FAKE_SSH_STATE)
    status_not_found    hello + not_found refusal
    list_ok             hello + a project list
    list_refused        hello + unauthorized refusal
    no_explicit_reply   hello that advertises delivery-only without fetch/explicit-reply
    ack_ok_delivery_only  the same delivery-only hello, but submit is still acknowledged
                        (proves delivery-only keeps working against an M1 peer)
    fetch_answered      hello + fetch reply: answered (reply from CCB_FAKE_SSH_REPLY)
    fetch_delivered     hello + fetch reply: delivered (no answer yet)
    fetch_not_found     hello + fetch refused with not_found
    fetch_bad_digest    hello + fetch reply whose reply digest does not match its text
    fetch_sequence      hello + fetch reply that answers only after
                        CCB_FAKE_SSH_ANSWER_AFTER calls (counter in CCB_FAKE_SSH_COUNTER)
    bad_ack             hello + an ack for a different op_id
    exit255_stderr      exit 255 after printing ssh's own error
    exit255_chatty      exit 255 after a lot of noisy stderr
    text_not_frame      print text on stdout instead of a frame (nologin/banner)
    hang                never answer (exercises the per-op deadline)

``CCB_FAKE_SSH_LOG`` receives the argv as JSON lines, one call per line.
"""

from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path


def _read_exact(stream, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(stream):
    header = _read_exact(stream, 4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack(">I", header)
    payload = _read_exact(stream, length)
    if len(payload) < length:
        return None
    return json.loads(payload.decode("utf-8"))


def write_frame(stream, value: dict) -> None:
    payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack(">I", len(payload)) + payload)
    stream.flush()


def _hello(op_id: str, *, major: int = 1, delivery: bool = True, explicit: bool = True) -> dict:
    modes = ["delivery-only"] if delivery else []
    if delivery and explicit:
        modes.append("explicit-reply")
    verbs = ["hello", "list", "submit", "status"]
    if delivery and explicit:
        verbs.append("fetch")
    return {
        "ok": True,
        "op": "hello",
        "op_id": op_id,
        "capabilities": {
            "verbs": verbs,
            "reply_modes": {"claude": list(modes), "codex": list(modes)},
            "max_frame": 262144,
            "proto": {"major": major, "minor": 0},
        },
    }


def _reply_digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fetch_reply(op: str, op_id: str, request_id: str, *, state: str) -> dict:
    frame = {
        "ok": True,
        "op": op,
        "op_id": op_id,
        "request_id": request_id,
        "state": state,
        "digest": os.environ.get("CCB_FAKE_SSH_REQUEST_DIGEST", ""),
        "history": [{"state": "delivered", "at": "2026-11-10T11:31:40+00:00"}],
    }
    if state == "answered":
        text = os.environ.get("CCB_FAKE_SSH_REPLY", "the answer\n")
        frame["reply_text"] = text
        frame["reply_digest"] = os.environ.get("CCB_FAKE_SSH_REPLY_DIGEST") or _reply_digest(text)
        frame["history"].append({"state": "answered", "at": "2026-11-10T11:40:00+00:00"})
    return frame


def main() -> int:
    log_path = os.environ.get("CCB_FAKE_SSH_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(sys.argv[1:]) + "\n")

    mode = (os.environ.get("CCB_FAKE_SSH_MODE") or "ack_ok").strip()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    if mode == "exit255":
        return 255
    if mode == "exit255_stderr":
        # What ssh really prints when the key, account or host key is refused.
        sys.stderr.write("bob@example.test: Permission denied (publickey).\n")
        sys.stderr.flush()
        return 255
    if mode == "exit255_chatty":
        # Many lines, control characters and long content: the summary must stay
        # bounded, printable and limited to the last non-empty lines.
        for index in range(200):
            sys.stderr.write(f"line {index} \x1b[31mnoise\x1b[0m " + "x" * 40 + "\n")
        sys.stderr.write("bob@example.test: Permission denied (publickey).\n")
        sys.stderr.flush()
        return 255
    if mode == "text_not_frame":
        # What a `nologin` shell (or a login banner) does: prints to the session
        # stdout instead of speaking the protocol.
        sys.stdout.write("This account is currently not available.\n")
        sys.stdout.flush()
        return 1
    if mode == "hang":
        import time

        time.sleep(30)
        return 0

    first = read_frame(stdin)
    if first is None:
        return 255
    if mode == "eof_before_submit":
        return 0

    if mode == "bad_ack":
        write_frame(stdout, _hello(str(first.get("op_id"))))
        submit = read_frame(stdin) or {}
        body = submit.get("body") if isinstance(submit.get("body"), dict) else {}
        # Everything matches EXCEPT the op id, so only that check can catch it.
        write_frame(
            stdout,
            {
                "ok": True,
                "op": "submit",
                "op_id": "somebody-elses-op",
                "request_id": body.get("request_id", ""),
                "state": "accepted-pending",
            },
        )
        return 0

    write_frame(
        stdout,
        _hello(
            str(first.get("op_id")),
            major=2 if mode == "major2" else 1,
            delivery=mode != "no_delivery",
            explicit=mode not in ("no_explicit_reply", "ack_ok_delivery_only"),
        ),
    )

    if mode in {"major2", "no_delivery", "no_explicit_reply"}:
        # The source must not submit after an incompatible handshake.
        return 0

    second = read_frame(stdin)
    if second is None:
        return 0
    op = str(second.get("op") or "")
    op_id = str(second.get("op_id") or "")
    body = second.get("body") if isinstance(second.get("body"), dict) else {}
    request_id = str(body.get("request_id") or "")

    if mode == "eof_after_submit":
        return 0
    if mode == "hang":
        import time

        time.sleep(30)
        return 0

    if mode == "ack_refused":
        write_frame(
            stdout,
            {
                "ok": False,
                "op": op,
                "op_id": op_id,
                "error": {
                    "code": os.environ.get("CCB_FAKE_SSH_CODE", "unauthorized"),
                    "message": "refused by the fake destination",
                },
            },
        )
        return 0
    if mode == "ack_unknown":
        write_frame(
            stdout,
            {
                "ok": False,
                "op": op,
                "op_id": op_id,
                "error": {
                    "code": "acceptance_unknown",
                    "message": "the destination did not answer in time",
                },
            },
        )
        return 0
    if mode in {"ack_ok", "ack_ok_delivery_only"}:
        write_frame(
            stdout,
            {
                "ok": True,
                "op": op,
                "op_id": op_id,
                "request_id": request_id,
                "state": "accepted-pending",
                "digest": body.get("digest", ""),
            },
        )
        return 0
    if mode in {"status_delivered", "status_not_found"}:
        if mode == "status_not_found":
            write_frame(
                stdout,
                {
                    "ok": False,
                    "op": op,
                    "op_id": op_id,
                    "error": {"code": "not_found", "message": "no such request"},
                },
            )
            return 0
        write_frame(
            stdout,
            {
                "ok": True,
                "op": op,
                "op_id": op_id,
                "request_id": request_id,
                "state": os.environ.get("CCB_FAKE_SSH_STATE", "delivered"),
                "history": [
                    {"state": "accepted-pending", "at": "2026-11-09T18:00:00+00:00"},
                    {"state": os.environ.get("CCB_FAKE_SSH_STATE", "delivered"),
                     "at": "2026-11-09T18:00:05+00:00"},
                ],
            },
        )
        return 0
    if mode in {"fetch_answered", "fetch_delivered", "fetch_not_found", "fetch_bad_digest",
                "fetch_sequence"}:
        if op != "fetch":
            write_frame(
                stdout,
                {"ok": False, "op": op, "op_id": op_id,
                 "error": {"code": "unknown_op", "message": "the fake expected a fetch"}},
            )
            return 0
        if mode == "fetch_not_found":
            write_frame(
                stdout,
                {"ok": False, "op": op, "op_id": op_id,
                 "error": {"code": "not_found", "message": "no such request"}},
            )
            return 0
        state = "delivered"
        if mode in ("fetch_answered", "fetch_bad_digest"):
            state = "answered"
        elif mode == "fetch_sequence":
            counter_path = os.environ.get("CCB_FAKE_SSH_COUNTER", "")
            seen = 0
            if counter_path:
                try:
                    seen = int(Path(counter_path).read_text().strip() or 0)
                except (OSError, ValueError):
                    seen = 0
                Path(counter_path).write_text(str(seen + 1))
            if seen + 1 >= int(os.environ.get("CCB_FAKE_SSH_ANSWER_AFTER", "2")):
                state = "answered"
        frame = _fetch_reply(op, op_id, request_id, state=state)
        if mode == "fetch_bad_digest":
            frame["reply_digest"] = "0" * 64
        write_frame(stdout, frame)
        return 0
    if mode in {"list_ok", "list_refused"}:
        if mode == "list_refused":
            write_frame(
                stdout,
                {
                    "ok": False,
                    "op": op,
                    "op_id": op_id,
                    "error": {"code": "unauthorized", "message": "principal is not configured"},
                },
            )
            return 0
        write_frame(
            stdout,
            {
                "ok": True,
                "op": op,
                "op_id": op_id,
                "projects": [
                    {
                        "alias": "example-project",
                        "providers": [
                            {"provider": "claude", "present": True},
                            {"provider": "codex", "present": False},
                        ],
                    }
                ],
            },
        )
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
