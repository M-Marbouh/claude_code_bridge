from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_bridge_module():
    path = ROOT / "bin" / "ccb-bridge-ask"
    loader = importlib.machinery.SourceFileLoader("ccb_bridge_ask_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _Lock:
    def fileno(self) -> int:
        return 0

    def close(self) -> None:
        pass


class _Fcntl:
    LOCK_UN = 8

    def flock(self, _fd: int, _op: int) -> None:
        pass


def test_bridge_diagnostics_print_when_reply_empty(monkeypatch, capsys) -> None:
    bridge = _load_bridge_module()
    target = {
        "index": 1,
        "work_dir": "/tmp/project",
        "ccb_project_id": "abcd1234",
        "providers": {"claude": {"alive": True}},
    }

    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    monkeypatch.setattr(bridge, "_acquire_lock", lambda _hash: (_Lock(), _Fcntl()))
    monkeypatch.setattr(
        bridge,
        "_send_to_daemon",
        lambda *_args: (
            0,
            "",
            {
                "done_seen": False,
                "anchor_seen": True,
                "fallback_scan": True,
                "status": "incomplete",
                "req_id": "req-1",
            },
        ),
    )

    rc = bridge.main(["--target", "1", "--caller-pane-id", "%cnt", "--caller-terminal", "tmux", "hello"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "[BRIDGE] done_seen=False anchor_seen=True fallback_scan=True status=incomplete req_id=req-1" in captured.err


def test_bridge_appends_reply_target() -> None:
    bridge = _load_bridge_module()

    assert bridge._append_reply_target("hello", "/tmp/sender") == "hello\n\nCCB_REPLY_TARGET: /tmp/sender"
    assert bridge._append_reply_target("hello\n", "") == "hello"


def test_bridge_request_is_delivery_only(monkeypatch) -> None:
    bridge = _load_bridge_module()
    sent: dict = {}

    class _Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def settimeout(self, _timeout) -> None:
            return None

        def sendall(self, payload: bytes) -> None:
            sent.update(json.loads(payload.decode("utf-8")))

        def recv(self, _size: int) -> bytes:
            return b'{"exit_code":0,"reply":"Peer message delivered.","meta":{"status":"completed"}}\n'

    monkeypatch.setattr(bridge.askd_rpc, "read_state", lambda _path: {"port": 1234, "token": "tok"})
    monkeypatch.setattr(bridge.socket, "create_connection", lambda *_args, **_kwargs: _Socket())

    exit_code, reply, _meta = bridge._send_to_daemon(
        {"work_dir": "/tmp/target"},
        "hello",
        10.0,
        "pane-1",
        "wezterm",
        "/tmp/sender",
    )

    assert exit_code == 0
    assert reply == "Peer message delivered."
    assert sent["delivery_only"] is True
    assert sent["suppress_completion_hook"] is True
    assert sent["message"] == "hello\n\nCCB_REPLY_TARGET: /tmp/sender"
