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
    monkeypatch.setattr(bridge, "_acquire_lock", lambda _hash, _provider: (_Lock(), _Fcntl()))
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


def test_bridge_appends_wait_context() -> None:
    bridge = _load_bridge_module()

    assert bridge._append_peer_context("hello", "/tmp/sender", "wait", "task-1", "parent-1") == (
        "hello\n\nCCB_PEER_INTENT: wait\nCCB_PEER_TASK_ID: task-1\nCCB_PEER_REPLY_TO: parent-1\n"
        "CCB_REPLY_TARGET: /tmp/sender\nCCB_REPLY_EXPECTED: yes"
    )


def test_bridge_notify_context_has_no_reply_target() -> None:
    bridge = _load_bridge_module()

    message = bridge._append_peer_context("hello", "/tmp/sender", "notify")

    assert message == "hello\n\nCCB_PEER_INTENT: notify\nCCB_REPLY_EXPECTED: no"
    assert "CCB_REPLY_TARGET" not in message


def test_bridge_rejects_notify_that_asks_for_reply(capsys) -> None:
    bridge = _load_bridge_module()

    rc = bridge.main(["--target", "1", "--intent", "notify", "Can you confirm?"])

    assert rc == 1
    assert "use --background or --wait" in capsys.readouterr().err


def test_bridge_rejects_codex_only_project(monkeypatch, capsys) -> None:
    bridge = _load_bridge_module()
    target = {
        "index": 1,
        "work_dir": "/tmp/project",
        "ccb_project_id": "abcd1234",
        "peer_capable": False,
        "providers": {"codex": {"alive": True}},
    }
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])

    rc = bridge.main(["--target", "1", "hello"])

    assert rc == 1
    assert "no mounted Claude pane" in capsys.readouterr().err


def test_bridge_accepts_codex_only_project(monkeypatch) -> None:
    bridge = _load_bridge_module()
    target = {
        "index": 1,
        "work_dir": "/tmp/project",
        "ccb_project_id": "abcd1234",
        "peer_capable": False,
        "providers": {"codex": {"alive": True, "mounted": True}},
    }
    captured: dict = {}
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    monkeypatch.setattr(bridge, "_acquire_lock", lambda _hash, _provider: (_Lock(), _Fcntl()))
    monkeypatch.setattr(
        bridge,
        "_send_to_daemon",
        lambda *args: captured.update(provider=args[-3], caller=args[-2], foreground=args[-1])
        or (0, "Codex result", {}),
    )

    rc = bridge.main(
        ["--target", "1", "--provider", "codex", "--caller", "claude", "--foreground", "hello"]
    )

    assert rc == 0
    assert captured == {"provider": "codex", "caller": "claude", "foreground": True}


def test_bridge_rejects_unmounted_claude_project(monkeypatch, capsys) -> None:
    bridge = _load_bridge_module()
    target = {
        "index": 1,
        "work_dir": "/tmp/project",
        "ccb_project_id": "abcd1234",
        "peer_capable": False,
        "providers": {"claude": {"alive": True, "mounted": False}},
    }
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])

    rc = bridge.main(["--target", "1", "hello"])

    assert rc == 1
    assert "no mounted Claude pane" in capsys.readouterr().err


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
        "background",
        "task-2",
        "parent-2",
    )

    assert exit_code == 0
    assert reply == "Peer message delivered."
    assert sent["delivery_only"] is True
    assert sent["suppress_completion_hook"] is True
    assert sent["req_id"] == "task-2"
    assert sent["message"] == (
        "hello\n\nCCB_PEER_INTENT: background\nCCB_PEER_TASK_ID: task-2\nCCB_PEER_REPLY_TO: parent-2\n"
        "CCB_REPLY_TARGET: /tmp/sender\nCCB_REPLY_EXPECTED: yes"
    )


def test_codex_bridge_request_captures_reply_automatically(monkeypatch) -> None:
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
            return b'{"exit_code":0,"reply":"Reviewed.","req_id":"task-3"}\n'

    monkeypatch.setattr(bridge.askd_rpc, "read_state", lambda _path: {"port": 1234, "token": "tok"})
    monkeypatch.setattr(bridge.socket, "create_connection", lambda *_args, **_kwargs: _Socket())

    exit_code, reply, _meta = bridge._send_to_daemon(
        {"work_dir": "/tmp/target"},
        "review this",
        10.0,
        "pane-1",
        "wezterm",
        "/tmp/sender",
        "background",
        "task-3",
        "",
        "codex",
        "claude",
        False,
    )

    assert exit_code == 0
    assert reply == "Reviewed."
    assert sent["provider"] == "codex"
    assert sent["caller"] == "claude"
    assert sent["caller_work_dir"] == "/tmp/sender"
    assert sent["req_id"] == "task-3"
    assert sent["delivery_only"] is False
    assert sent["suppress_completion_hook"] is False
    assert "CCB_REPLY_TARGET" not in sent["message"]
    assert "CCB_REPLY_MODE: automatic-capture" in sent["message"]
    assert "Do not send a reverse peer message." in sent["message"]


def test_codex_notify_suppresses_completion_hook(monkeypatch) -> None:
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
            return b'{"exit_code":0,"reply":"Acknowledged."}\n'

    monkeypatch.setattr(bridge.askd_rpc, "read_state", lambda _path: {"port": 1234, "token": "tok"})
    monkeypatch.setattr(bridge.socket, "create_connection", lambda *_args, **_kwargs: _Socket())

    bridge._send_to_daemon(
        {"work_dir": "/tmp/target"},
        "FYI",
        10.0,
        "pane-1",
        "tmux",
        "/tmp/sender",
        "notify",
        "task-4",
        "",
        "codex",
        "claude",
        False,
    )

    assert sent["delivery_only"] is False
    assert sent["suppress_completion_hook"] is True
    assert "CCB_REPLY_EXPECTED: no" in sent["message"]


def test_claude_peer_from_codex_names_reverse_reply_provider() -> None:
    bridge = _load_bridge_module()

    message = bridge._append_peer_context(
        "hello",
        "/tmp/sender",
        "wait",
        "task-5",
        provider="claude",
        caller="codex",
    )

    assert "CCB_REPLY_PROVIDER: codex" in message
