from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Thread

import askd_rpc


class _FakeSocket:
    def __init__(self, response: dict):
        self._payload = (json.dumps(response) + "\n").encode("utf-8")
        self.sent: list[bytes] = []

    def __enter__(self) -> "_FakeSocket":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def settimeout(self, _timeout: float | None) -> None:
        return None

    def close(self) -> None:
        return None

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


def test_request_daemon_uses_mailbox_when_network_is_disabled(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    requests = root / "requests"
    responses = root / "responses"
    requests.mkdir(parents=True)
    responses.mkdir()
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setattr(
        askd_rpc,
        "connect_daemon",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("TCP transport used")),
    )

    def _respond() -> None:
        deadline = time.time() + 1.0
        while time.time() < deadline:
            pending = list(requests.glob("*.json"))
            if pending:
                request_path = pending[0]
                request = json.loads(request_path.read_text(encoding="utf-8"))
                askd_rpc._write_json_atomic(
                    responses / request_path.name,
                    {"type": "ask.response", "exit_code": 0, "reply": request["message"]},
                )
                return
            time.sleep(0.01)

    responder = Thread(target=_respond, daemon=True)
    responder.start()
    response = askd_rpc.request_daemon(
        {"mailbox_dir": str(root), "token": "tok"},
        {"type": "ask.request", "token": "tok", "message": "hello"},
        connect_timeout_s=0.5,
        response_timeout_s=1.0,
    )
    responder.join(timeout=1.0)

    assert response["reply"] == "hello"
    assert list(requests.iterdir()) == []
    assert list(responses.iterdir()) == []


def test_request_daemon_falls_back_to_mailbox_when_tcp_is_unreachable(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    requests = root / "requests"
    responses = root / "responses"
    requests.mkdir(parents=True)
    responses.mkdir()
    monkeypatch.delenv("CODEX_SANDBOX_NETWORK_DISABLED", raising=False)
    monkeypatch.setattr(
        askd_rpc,
        "connect_daemon",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(askd_rpc.DaemonConnectionError("blocked")),
    )

    def _respond() -> None:
        deadline = time.time() + 1.0
        while time.time() < deadline:
            pending = list(requests.glob("*.json"))
            if pending:
                request_path = pending[0]
                request = json.loads(request_path.read_text(encoding="utf-8"))
                askd_rpc._write_json_atomic(
                    responses / request_path.name,
                    {"type": "ask.response", "exit_code": 0, "reply": request["message"]},
                )
                return
            time.sleep(0.01)

    responder = Thread(target=_respond, daemon=True)
    responder.start()
    response = askd_rpc.request_daemon(
        {"mailbox_dir": str(root), "token": "tok"},
        {"type": "ask.request", "token": "tok", "message": "hello"},
        connect_timeout_s=0.5,
        response_timeout_s=1.0,
    )
    responder.join(timeout=1.0)

    assert response["reply"] == "hello"
    assert list(requests.iterdir()) == []
    assert list(responses.iterdir()) == []
