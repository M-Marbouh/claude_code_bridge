from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import socket
from pathlib import Path

import askd_rpc


ROOT = Path(__file__).resolve().parents[1]


def _load_ask_module():
    path = ROOT / "bin" / "ask"
    loader = importlib.machinery.SourceFileLoader("ask_client_flags_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _Socket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._response = b'{"exit_code":0,"reply":""}\n'

    def settimeout(self, _timeout: float) -> None:
        return None

    def connect(self, _addr) -> None:
        return None

    def sendall(self, payload: bytes) -> None:
        self.sent.append(json.loads(payload.decode("utf-8").strip()))

    def recv(self, _size: int) -> bytes:
        response = self._response
        self._response = b""
        return response

    def close(self) -> None:
        return None


def _capture_unified_request(monkeypatch, tmp_path: Path, *, show_tier_env: str | None) -> dict:
    ask = _load_ask_module()
    fake_socket = _Socket()

    if show_tier_env is None:
        monkeypatch.delenv("CCB_CODEX_SHOW_TIER", raising=False)
    else:
        monkeypatch.setenv("CCB_CODEX_SHOW_TIER", show_tier_env)
    monkeypatch.setattr(
        askd_rpc,
        "read_state",
        lambda _path: {"host": "127.0.0.1", "port": 31337, "token": "tok", "work_dir": str(tmp_path)},
    )
    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: fake_socket)
    monkeypatch.setattr(ask, "_maybe_start_unified_daemon", lambda: False)
    monkeypatch.setattr(ask, "_caller_pane_info", lambda: ("%1", "tmux"))

    rc = ask._send_via_unified_daemon("codex", "hello", 1.0, False, "claude")

    assert rc == 0
    assert len(fake_socket.sent) == 1
    return fake_socket.sent[0]


def test_unified_daemon_request_forwards_show_tier_env(monkeypatch, tmp_path: Path) -> None:
    sent = _capture_unified_request(monkeypatch, tmp_path, show_tier_env="1")

    assert sent["show_tier"] is True


def test_unified_daemon_request_omits_show_tier_by_default(monkeypatch, tmp_path: Path) -> None:
    sent = _capture_unified_request(monkeypatch, tmp_path, show_tier_env=None)

    assert "show_tier" not in sent


def test_unified_daemon_preserves_outer_async_request_id(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CCB_REQ_ID", "20260711-220118-845-190737")

    sent = _capture_unified_request(monkeypatch, tmp_path, show_tier_env=None)

    assert sent["req_id"] == "20260711-220118-845-190737"


def test_ask_rejects_provider_instances_before_dispatch(capsys) -> None:
    ask = _load_ask_module()

    rc = ask.main(["ask", "codex:worker", "hello"])

    assert rc == 1
    assert "Provider instances are no longer supported" in capsys.readouterr().err


def test_ask_rejects_removed_route_tags_before_dispatch(capsys) -> None:
    ask = _load_ask_module()

    rc = ask.main(["ask", "codex", "--foreground", "[WORKER] hello"])

    assert rc == 1
    assert "routing tags are no longer supported" in capsys.readouterr().err


def test_peer_notify_uses_one_way_foreground_delivery(monkeypatch) -> None:
    ask = _load_ask_module()
    captured: dict = {}

    def _capture(
        target: str,
        provider: str,
        timeout: float,
        message: str,
        foreground: bool,
        intent: str,
        reply_to: str,
    ) -> int:
        captured.update(
            target=target,
            provider=provider,
            timeout=timeout,
            message=message,
            foreground=foreground,
            intent=intent,
            reply_to=reply_to,
        )
        return 0

    monkeypatch.setattr(ask, "_run_peer_bridge", _capture)

    rc = ask._handle_peer_mode(["--peer", "/tmp/peer", "--notify", "result"])

    assert rc == 0
    assert captured == {
        "target": "/tmp/peer",
        "provider": "claude",
        "timeout": 3600.0,
        "message": "result",
        "foreground": True,
        "intent": "notify",
        "reply_to": "",
    }


def test_peer_notify_rejects_direct_question(monkeypatch, capsys) -> None:
    ask = _load_ask_module()
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge",
        lambda *_args: (_ for _ in ()).throw(AssertionError("contradictory notify must not send")),
    )

    rc = ask._handle_peer_mode(["--peer", "/tmp/peer", "--notify", "Can you confirm?"])

    assert rc == 1
    assert "use --background or --wait" in capsys.readouterr().err


def test_peer_reply_to_is_forwarded(monkeypatch) -> None:
    ask = _load_ask_module()
    captured: dict = {}
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge",
        lambda target, provider, timeout, message, foreground, intent, reply_to: captured.update(
            target=target,
            provider=provider,
            intent=intent,
            reply_to=reply_to,
        ) or 0,
    )

    rc = ask._handle_peer_mode(
        ["--peer", "/tmp/peer", "--notify", "--reply-to", "20260711-212112-453-72347", "Done."]
    )

    assert rc == 0
    assert captured["intent"] == "notify"
    assert captured["reply_to"] == "20260711-212112-453-72347"


def test_codex_peer_notify_runs_in_background_and_preserves_provider(monkeypatch) -> None:
    ask = _load_ask_module()
    captured: dict = {}
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge",
        lambda target, provider, timeout, message, foreground, intent, reply_to: captured.update(
            target=target,
            provider=provider,
            foreground=foreground,
            intent=intent,
        ) or 0,
    )

    rc = ask.main(["ask", "codex", "--peer", "/tmp/peer", "--notify", "FYI"])

    assert rc == 0
    assert captured == {
        "target": "/tmp/peer",
        "provider": "codex",
        "foreground": False,
        "intent": "notify",
    }


def test_peer_routing_rejects_unsupported_provider(capsys) -> None:
    ask = _load_ask_module()

    rc = ask.main(["ask", "gemini", "--peer", "/tmp/peer", "hello"])

    assert rc == 1
    assert "does not support cross-project peer routing: gemini" in capsys.readouterr().err
