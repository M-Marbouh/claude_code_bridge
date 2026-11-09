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
    bad_ack             hello + an ack for a different op_id
    hang                never answer (exercises the per-op deadline)

``CCB_FAKE_SSH_LOG`` receives the argv as JSON lines, one call per line.
"""

from __future__ import annotations

import json
import os
import struct
import sys


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


def _hello(op_id: str, *, major: int = 1, delivery: bool = True) -> dict:
    return {
        "ok": True,
        "op": "hello",
        "op_id": op_id,
        "capabilities": {
            "verbs": ["hello", "list", "submit", "status"],
            "reply_modes": {
                "claude": ["delivery-only"] if delivery else [],
                "codex": ["delivery-only"] if delivery else [],
            },
            "max_frame": 262144,
            "proto": {"major": major, "minor": 0},
        },
    }


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
        ),
    )

    if mode in {"major2", "no_delivery"}:
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
    if mode == "ack_ok":
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
