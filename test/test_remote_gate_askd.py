from __future__ import annotations

import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import remote_protocol as proto

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "bin" / "ccb-remote-gate"


def _envelope(op: str, body: dict | None = None, op_id: str = "op-1") -> dict:
    return {
        "proto": {"major": 1, "minor": 0},
        "op": op,
        "op_id": op_id,
        "body": body if body is not None else {},
    }


def _record(**overrides) -> dict:
    kwargs = dict(
        dest_project="example-project",
        dest_provider="claude",
        intent="notify",
        text="hello from another machine",
        request_id=proto.make_remote_id("20261109-182536-922-2626995", "abc1"),
    )
    kwargs.update(overrides)
    return proto.build_request_record(**kwargs)


class FakeAskd:
    """A minimal newline-JSON askd that records what the gate sent."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[dict] = []
        self.token = "token-abc123"
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.2)  # so the serve loop can notice __exit__
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "FakeAskd":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(5.0)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        buf = b""
                        break
                    buf += chunk
                if not buf:
                    continue
                request = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
                self.requests.append(request)
                response = self.handler(request)
                if response is None:
                    continue  # close without answering
                conn.sendall((json.dumps(response) + "\n").encode("utf-8"))

    def state_file(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "askd.json"
        path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "host": "127.0.0.1",
                    "port": self.port,
                    "token": self.token,
                    "protocol": "ask",
                }
            ),
            encoding="utf-8",
        )
        return path


def _pong(request: dict) -> dict:
    return {"type": "ask.pong", "v": 1, "id": request.get("id"), "exit_code": 0}


def _run_gate(args: list[str], payload: bytes, *, env: dict):
    return subprocess.run(
        [sys.executable, str(GATE), *args],
        input=payload,
        capture_output=True,
        env=env,
        timeout=60,
    )


_READ = proto.read_frame


def _responses(stdout: bytes) -> list[dict]:
    stream = io.BytesIO(stdout)
    frames = []
    while True:
        frame = _READ(stream)
        if frame is None:
            return frames
        frames.append(frame)


def _env(run_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "SSH_ORIGINAL_COMMAND"}
    env["CCB_RUN_DIR"] = str(run_dir)
    return env


def test_list_is_forwarded_with_the_argv_principal_and_daemon_token(tmp_path):
    def handler(request: dict) -> dict:
        if request.get("type") == "ask.ping":
            return _pong(request)
        return {
            "type": "ask.response",
            "v": 1,
            "id": request.get("id"),
            "ok": True,
            "reply": "",
            "remote": {"projects": [{"alias": "example-project", "providers": [{"provider": "claude", "present": True}]}]},
        }

    with FakeAskd(handler) as daemon:
        run_dir = tmp_path / "run"
        daemon.state_file(run_dir)
        result = _run_gate(
            ["--principal", "bob"], proto.encode_frame(_envelope("list")), env=_env(run_dir)
        )

    assert result.returncode == 0, result.stderr
    [frame] = _responses(result.stdout)
    assert frame["ok"] is True
    assert frame["op"] == "list"
    assert frame["projects"][0]["alias"] == "example-project"

    [request] = [r for r in daemon.requests if r.get("operation")]
    assert request["operation"] == "remote_list"
    assert request["principal"] == "bob"
    assert request["token"] == daemon.token
    assert request["type"] == "ask.request"


def test_submit_body_reaches_the_daemon_unchanged(tmp_path):
    body = _record()

    def handler(request: dict) -> dict:
        if request.get("type") == "ask.ping":
            return _pong(request)
        return {
            "type": "ask.response",
            "v": 1,
            "id": request.get("id"),
            "ok": True,
            "reply": "",
            "remote": {
                "request_id": body["request_id"],
                "state": "accepted-pending",
                "digest": body["digest"],
                "history": [{"state": "accepted-pending", "at": "2026-11-09T18:00:00+00:00"}],
            },
        }

    with FakeAskd(handler) as daemon:
        run_dir = tmp_path / "run"
        daemon.state_file(run_dir)
        result = _run_gate(
            ["--principal", "bob"], proto.encode_frame(_envelope("submit", body)), env=_env(run_dir)
        )

    assert result.returncode == 0, result.stderr
    [frame] = _responses(result.stdout)
    assert frame["ok"] is True
    assert frame["op"] == "submit"
    assert frame["state"] == "accepted-pending"
    assert frame["request_id"] == body["request_id"]

    [request] = [r for r in daemon.requests if r.get("operation")]
    assert request["operation"] == "remote_submit"
    assert request["record"] == body


def test_status_forwards_the_request_id(tmp_path):
    def handler(request: dict) -> dict:
        if request.get("type") == "ask.ping":
            return _pong(request)
        return {
            "type": "ask.response",
            "v": 1,
            "id": request.get("id"),
            "ok": True,
            "reply": "",
            "remote": {"request_id": request.get("request_id"), "state": "delivered", "history": []},
        }

    body = {"request_id": "20261109-182536-922-2626995-habc1"}
    with FakeAskd(handler) as daemon:
        run_dir = tmp_path / "run"
        daemon.state_file(run_dir)
        result = _run_gate(
            ["--principal", "bob"], proto.encode_frame(_envelope("status", body)), env=_env(run_dir)
        )

    assert result.returncode == 0, result.stderr
    [frame] = _responses(result.stdout)
    assert frame["ok"] is True
    assert frame["state"] == "delivered"
    [request] = [r for r in daemon.requests if r.get("operation")]
    assert request["operation"] == "remote_status"
    assert request["request_id"] == body["request_id"]


def test_fetch_forwards_the_request_id_and_returns_the_answer(tmp_path):
    def handler(request: dict) -> dict:
        if request.get("type") == "ask.ping":
            return _pong(request)
        return {
            "type": "ask.response",
            "v": 1,
            "id": request.get("id"),
            "ok": True,
            "reply": "",
            "remote": {
                "request_id": request.get("request_id"),
                "state": "answered",
                "digest": "a" * 64,
                "reply_text": "the answer\n",
                "reply_digest": "b" * 64,
                "history": [],
            },
        }

    body = {"request_id": "20261110-113136-582-89163-1-hmain1"}
    with FakeAskd(handler) as daemon:
        run_dir = tmp_path / "run"
        daemon.state_file(run_dir)
        result = _run_gate(
            ["--principal", "main"], proto.encode_frame(_envelope("fetch", body)), env=_env(run_dir)
        )

    assert result.returncode == 0, result.stderr
    [frame] = _responses(result.stdout)
    assert frame["ok"] is True
    assert frame["op"] == "fetch"
    assert frame["state"] == "answered"
    assert frame["reply_text"] == "the answer\n"
    assert frame["reply_digest"] == "b" * 64
    [request] = [r for r in daemon.requests if r.get("operation")]
    assert request["operation"] == "remote_fetch"
    assert request["request_id"] == body["request_id"]


def test_a_reply_can_never_be_pushed_through_the_gate(tmp_path):
    # Replies are destination-local: the wire accepts no reply verb at all, so
    # a remote peer cannot write one and the gate forwards nothing.
    result = _run_gate(
        ["--principal", "main"],
        proto.encode_frame(_envelope("reply", {"request_id": "x", "reply_text": "forged"})),
        env=_env(tmp_path / "run"),
    )
    assert result.returncode == 0, result.stderr
    [frame] = _responses(result.stdout)
    assert frame["ok"] is False
    assert frame["error"]["code"] == proto.ERR_UNKNOWN_OP


def test_daemon_error_codes_are_mapped_and_unknown_codes_are_not_echoed(tmp_path):
    def handler(request: dict) -> dict:
        if request.get("type") == "ask.ping":
            return _pong(request)
        if request.get("operation") == "remote_list":
            return {
                "type": "ask.response",
                "v": 1,
                "id": request.get("id"),
                "ok": False,
                "error": {"code": proto.ERR_UNAUTHORIZED, "message": "principal is not configured on this destination"},
            }
        return {
            "type": "ask.response",
            "v": 1,
            "id": request.get("id"),
            "ok": False,
            "error": {"code": "not_a_real_code", "message": "whatever"},
        }

    with FakeAskd(handler) as daemon:
        run_dir = tmp_path / "run"
        daemon.state_file(run_dir)
        payload = proto.encode_frame(_envelope("list", op_id="op-1")) + proto.encode_frame(
            _envelope("submit", op_id="op-2")
        )
        result = _run_gate(["--principal", "bob"], payload, env=_env(run_dir))

    assert result.returncode == 0, result.stderr
    first, second = _responses(result.stdout)
    assert first["error"]["code"] == proto.ERR_UNAUTHORIZED
    assert first["error"]["message"] == "principal is not configured on this destination"
    assert second["error"]["code"] == proto.ERR_INTERNAL_ERROR
    assert "not_a_real_code" not in json.dumps(second)


def test_daemon_that_never_answers_reports_acceptance_unknown(tmp_path):
    def handler(request: dict):
        if request.get("type") == "ask.ping":
            return _pong(request)
        return None  # closes without answering

    with FakeAskd(handler) as daemon:
        run_dir = tmp_path / "run"
        daemon.state_file(run_dir)
        result = _run_gate(
            ["--principal", "bob"], proto.encode_frame(_envelope("submit", _record())), env=_env(run_dir)
        )

    assert result.returncode == 0, result.stderr
    [frame] = _responses(result.stdout)
    assert frame["ok"] is False
    # NOT "daemon unavailable": bytes were sent, so a resend could duplicate.
    assert frame["error"]["code"] == proto.ERR_ACCEPTANCE_UNKNOWN


def test_unreachable_daemon_state_file_reports_unavailable(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "askd.json").write_text(
        json.dumps({"pid": os.getpid(), "host": "127.0.0.1", "port": 1, "token": "t"}),
        encoding="utf-8",
    )
    result = _run_gate(
        ["--principal", "bob"], proto.encode_frame(_envelope("list")), env=_env(run_dir)
    )
    assert result.returncode == 0, result.stderr
    [frame] = _responses(result.stdout)
    assert frame["error"]["code"] == proto.ERR_DAEMON_UNAVAILABLE
