from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import socket
from pathlib import Path

import askd_rpc
from ccb_runtime_status import ProviderRuntimeStatus


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

    rc = ask._send_via_unified_daemon("codex:worker", "hello", 1.0, False, "claude")

    assert rc == 0
    assert len(fake_socket.sent) == 1
    return fake_socket.sent[0]


def test_unified_daemon_request_forwards_show_tier_env(monkeypatch, tmp_path: Path) -> None:
    sent = _capture_unified_request(monkeypatch, tmp_path, show_tier_env="1")

    assert sent["show_tier"] is True


def test_unified_daemon_request_omits_show_tier_by_default(monkeypatch, tmp_path: Path) -> None:
    sent = _capture_unified_request(monkeypatch, tmp_path, show_tier_env=None)

    assert "show_tier" not in sent


def _route_status(key: str, *, mounted: bool, reason: str = "") -> ProviderRuntimeStatus:
    provider, instance = key.split(":", 1) if ":" in key else (key, None)
    return ProviderRuntimeStatus(
        key=key,
        provider=provider,
        instance=instance,
        capable=True,
        configured=True,
        registered=mounted,
        pane_alive=mounted,
        session_bound=mounted,
        daemon_online=True,
        mounted=mounted,
        reason=reason,
    )


def _run_ask_main(monkeypatch, tmp_path: Path, argv: list[str], statuses: dict[str, ProviderRuntimeStatus] | None = None):
    ask = _load_ask_module()
    sent: list[tuple[str, str]] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CCB_CALLER", "claude")
    monkeypatch.setattr(ask, "_maybe_start_unified_daemon", lambda: True)
    monkeypatch.setattr(
        ask,
        "_send_via_unified_daemon",
        lambda provider, message, *_args: sent.append((provider, message)) or 0,
    )
    if statuses is None:
        monkeypatch.setattr(
            ask,
            "provider_status_for_target",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("status check should not run")),
        )
    else:
        monkeypatch.setattr(
            ask,
            "provider_status_for_target",
            lambda target, **_kwargs: statuses.get(target, _route_status(target, mounted=False, reason="not_registered")),
        )
    rc = ask.main(argv)
    return rc, sent


def test_tag_routing_no_tag_is_unchanged_and_does_not_check_status(monkeypatch, tmp_path: Path) -> None:
    rc, sent = _run_ask_main(monkeypatch, tmp_path, ["ask", "codex", "--foreground", "hello"])

    assert rc == 0
    assert sent == [("codex", "hello")]


def test_tag_routing_worker_routes_unqualified_provider_to_worker(monkeypatch, tmp_path: Path) -> None:
    rc, sent = _run_ask_main(
        monkeypatch,
        tmp_path,
        ["ask", "codex", "--foreground", "[WORKER] build it"],
        {"codex:worker": _route_status("codex:worker", mounted=True)},
    )

    assert rc == 0
    assert sent == [("codex:worker", "build it")]


def test_tag_routing_explicit_qualified_worker_wins_when_tag_matches(monkeypatch, tmp_path: Path) -> None:
    rc, sent = _run_ask_main(
        monkeypatch,
        tmp_path,
        ["ask", "codex:worker", "--foreground", "[WORKER] build it"],
        {"codex:worker": _route_status("codex:worker", mounted=True)},
    )

    assert rc == 0
    assert sent == [("codex:worker", "build it")]


def test_tag_routing_conflict_errors_without_dispatch(monkeypatch, tmp_path: Path, capsys) -> None:
    rc, sent = _run_ask_main(
        monkeypatch,
        tmp_path,
        ["ask", "codex:worker", "--foreground", "[ARCHITECT] design it"],
        {"codex:worker": _route_status("codex:worker", mounted=True)},
    )

    assert rc == 1
    assert sent == []
    assert "CCB_ROUTE_ERROR target=codex:worker reason=tag_conflict tag=ARCHITECT" in capsys.readouterr().err


def test_tag_routing_not_mounted_errors_with_machine_readable_fields(monkeypatch, tmp_path: Path, capsys) -> None:
    rc, sent = _run_ask_main(
        monkeypatch,
        tmp_path,
        ["ask", "codex", "--foreground", "[WORKER] build it"],
        {"codex:worker": _route_status("codex:worker", mounted=False, reason="pane_dead")},
    )

    assert rc == 1
    assert sent == []
    err = capsys.readouterr().err
    assert "CCB_ROUTE_ERROR target=codex:worker reason=not_mounted" in err
    assert "configured=true" in err
    assert "registered=false" in err
    assert "pane_alive=false" in err
    assert "session_bound=false" in err
    assert "daemon_online=true" in err


def test_tag_routing_explicit_fallback_prints_and_dispatches_base(monkeypatch, tmp_path: Path, capsys) -> None:
    rc, sent = _run_ask_main(
        monkeypatch,
        tmp_path,
        ["ask", "codex", "--foreground", "--route-fallback", "[WORKER] build it"],
        {
            "codex:worker": _route_status("codex:worker", mounted=False, reason="pane_dead"),
            "codex": _route_status("codex", mounted=True),
        },
    )

    assert rc == 0
    assert sent == [("codex", "build it")]
    assert "CCB_ROUTE_FALLBACK from=codex:worker to=codex" in capsys.readouterr().err
