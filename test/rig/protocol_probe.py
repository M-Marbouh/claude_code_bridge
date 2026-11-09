#!/usr/bin/env python3
"""Raw protocol driver for the rig: fixed ids, chosen keys, controlled hangs.

`ask` always mints a fresh id, so the duplicate-id and lost-acknowledgement
rows need to speak the framed protocol directly. This tool never implements
protocol logic of its own: it composes `lib/remote_client.py` and
`lib/remote_protocol.py` exactly as the CLI does.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import remote_client  # noqa: E402
import remote_protocol as proto  # noqa: E402


def _emit(**fields: object) -> None:
    print(json.dumps(fields, sort_keys=True))


def _open(host_alias: str):
    host = remote_client.host_for(host_alias)
    session = remote_client.SshSession(remote_client.ssh_argv(host))
    return host, session


def _write_receipt(host, request_id: str, project: str, provider: str, text: str, state: str) -> None:
    record = proto.build_request_record(
        dest_project=project,
        dest_provider=provider,
        intent="notify",
        text=text,
        request_id=request_id,
    )
    remote_client.save_receipt(
        {
            "request_id": request_id,
            "host": host.alias,
            "dest_project": project,
            "dest_provider": provider,
            "dest_session": "",
            "digest": record["digest"],
            "schema_version": record["schema_version"],
            "intent": "notify",
            "caller": {"caller": "rig-probe", "verified": False},
            "created": remote_client._now(),
            "state": state,
            "history": [{"state": state, "at": remote_client._now()}],
            "remote_state": "",
        }
    )


def cmd_hello(args) -> int:
    host, session = _open(args.host)
    try:
        capabilities = remote_client._hello(session, op_id="hello-1")
        _emit(ok=True, host=host.alias, capabilities=capabilities)
        return 0
    except remote_client.SessionError as exc:
        _emit(ok=False, error=exc.kind, detail=exc.detail)
        return 1
    finally:
        session.close()


def cmd_submit(args) -> int:
    host, session = _open(args.host)
    request_id = args.id or proto.make_remote_id(
        __import__("ccb_protocol").make_req_id(), host.issuer_token
    )
    record = proto.build_request_record(
        dest_project=args.project,
        dest_provider=args.provider,
        intent="notify",
        text=args.text,
        request_id=request_id,
        dest_session=args.session or None,
    )
    _write_receipt(host, request_id, args.project, args.provider, args.text, remote_client.STATE_SUBMITTING)
    written = 0
    try:
        capabilities = remote_client._hello(session, op_id="hello-1")
        remote_client._require_delivery_only(capabilities, args.provider)
    except remote_client.SessionError as exc:
        _emit(ok=False, request_id=request_id, error=exc.kind, detail=exc.detail)
        return 1
    frame = proto.encode_frame(
        {
            "proto": {"major": proto.PROTO_MAJOR, "minor": proto.PROTO_MINOR},
            "op": "submit",
            "op_id": f"submit-{request_id}",
            "body": record,
        }
    )
    try:
        written = session.write_all(frame)
    except remote_client.SessionError as exc:
        _emit(ok=False, request_id=request_id, error=exc.kind, detail=exc.detail, bytes_written=written)
        return 1
    if args.no_ack:
        session.close()
        _emit(ok=False, request_id=request_id, error="no-ack-read", bytes_written=written)
        return 0
    try:
        ack = session.read_frame()
    except remote_client.SessionError as exc:
        _emit(ok=False, request_id=request_id, error=exc.kind, detail=exc.detail, bytes_written=written)
        return 1
    finally:
        session.close()
    _emit(ok=bool(ack.get("ok")), request_id=request_id, ack=ack)
    return 0 if ack.get("ok") else 1


def cmd_status(args) -> int:
    host, session = _open(args.host)
    try:
        remote_client._hello(session, op_id="hello-1")
        session.write_all(
            proto.encode_frame(
                {
                    "proto": {"major": proto.PROTO_MAJOR, "minor": proto.PROTO_MINOR},
                    "op": "status",
                    "op_id": f"status-{args.id}",
                    "body": {"request_id": args.id},
                }
            )
        )
        response = session.read_frame()
        _emit(ok=bool(response.get("ok")), response=response)
        return 0 if response.get("ok") else 1
    except remote_client.SessionError as exc:
        _emit(ok=False, error=exc.kind, detail=exc.detail)
        return 1
    finally:
        session.close()


def cmd_list(args) -> int:
    host, session = _open(args.host)
    try:
        remote_client._hello(session, op_id="hello-1")
        session.write_all(
            proto.encode_frame(
                {
                    "proto": {"major": proto.PROTO_MAJOR, "minor": proto.PROTO_MINOR},
                    "op": "list",
                    "op_id": "list-1",
                    "body": {},
                }
            )
        )
        response = session.read_frame()
        _emit(ok=bool(response.get("ok")), response=response)
        return 0 if response.get("ok") else 1
    except remote_client.SessionError as exc:
        _emit(ok=False, error=exc.kind, detail=exc.detail)
        return 1
    finally:
        session.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    hello = sub.add_parser("hello")
    hello.add_argument("--host", default="rig")
    hello.set_defaults(func=cmd_hello)

    submit = sub.add_parser("submit")
    submit.add_argument("--host", default="rig")
    submit.add_argument("--id", default="")
    submit.add_argument("--project", default="demo")
    submit.add_argument("--provider", default="codex")
    submit.add_argument("--session", default="")
    submit.add_argument("--text", required=True)
    submit.add_argument("--no-ack", action="store_true", help="write the frame, then close without reading")
    submit.set_defaults(func=cmd_submit)

    status = sub.add_parser("status")
    status.add_argument("--host", default="rig")
    status.add_argument("--id", required=True)
    status.set_defaults(func=cmd_status)

    listing = sub.add_parser("list")
    listing.add_argument("--host", default="rig")
    listing.set_defaults(func=cmd_list)

    args = parser.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
