from __future__ import annotations

import json
from pathlib import Path

import askd_rpc


class _FakeSocket:
    def __init__(self, response: dict):
        self._payload = (json.dumps(response) + "\n").encode("utf-8")
        self.sent: list[bytes] = []

    def __enter__(self) -> "_FakeSocket":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, size: int) -> bytes:
        if not self._payload:
            return b""
        chunk = self._payload[:size]
        self._payload = self._payload[size:]
        return chunk


def test_shutdown_daemon_rejects_wrong_response_type(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "askd.json"
    fake_socket = _FakeSocket({"type": "other.response", "v": 1, "id": "shutdown", "exit_code": 0, "reply": "OK"})

    monkeypatch.setattr(
        askd_rpc,
        "read_state",
        lambda _path: {"host": "127.0.0.1", "port": 31337, "token": "tok"},
    )
    monkeypatch.setattr(askd_rpc.socket, "create_connection", lambda *_args, **_kwargs: fake_socket)

    assert askd_rpc.shutdown_daemon("ask", timeout_s=0.5, state_file=state_file) is False

    sent = json.loads(fake_socket.sent[0].decode("utf-8").strip())
    assert sent["type"] == "ask.shutdown"


def test_shutdown_daemon_rejects_nonzero_exit_code(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "askd.json"
    fake_socket = _FakeSocket({"type": "ask.response", "v": 1, "id": "shutdown", "exit_code": 1, "reply": "Invalid request"})

    monkeypatch.setattr(
        askd_rpc,
        "read_state",
        lambda _path: {"host": "127.0.0.1", "port": 31337, "token": "tok"},
    )
    monkeypatch.setattr(askd_rpc.socket, "create_connection", lambda *_args, **_kwargs: fake_socket)

    assert askd_rpc.shutdown_daemon("ask", timeout_s=0.5, state_file=state_file) is False
