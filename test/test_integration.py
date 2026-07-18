from __future__ import annotations

import json
import os
import stat
import sys
import time
import types
from pathlib import Path
from threading import Thread
from typing import Any, Callable, Generator

import pytest

import askd_rpc
from askd_client import try_daemon_request
from askd_server import AskDaemonServer
from providers import ProviderClientSpec, ProviderDaemonSpec


def _wait_for_file(path: Path, timeout_s: float = 3.0) -> None:
    deadline = time.time() + max(0.1, float(timeout_s))
    while time.time() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for file: {path}")


def _make_spec() -> ProviderDaemonSpec:
    unique = f"itest-{os.getpid()}-{int(time.time() * 1000)}"
    return ProviderDaemonSpec(
        daemon_key=unique,
        protocol_prefix="itest",
        state_file_name=f"{unique}.json",
        log_file_name=f"{unique}.log",
        idle_timeout_env="CCB_ITEST_IDLE_TIMEOUT_S",
        lock_name=unique,
    )


@pytest.fixture()
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[ProviderDaemonSpec, Path, Thread], None, None]:
    # Isolate HOME (ProviderLock uses Path.home()) and run dir (askd_runtime).
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CCB_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("CCB_ITEST_IDLE_TIMEOUT_S", "0")  # disable idle shutdown in tests

    spec = _make_spec()
    state_file = tmp_path / "state" / "itest.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    def handler(msg: dict) -> dict:
        return {
            "type": f"{spec.protocol_prefix}.response",
            "v": 1,
            "id": msg.get("id"),
            "exit_code": 0,
            "reply": f"echo:{msg.get('message') or ''} out:{msg.get('output_path') or ''}",
        }

    server = AskDaemonServer(
        spec=spec,
        host="127.0.0.1",
        port=0,
        token="test-token",
        state_file=state_file,
        request_handler=handler,
    )

    thread = Thread(target=server.serve_forever, name="itest-daemon", daemon=True)
    thread.start()

    _wait_for_file(state_file, timeout_s=3.0)
    yield spec, state_file, thread

    # Best-effort shutdown if still alive.
    try:
        askd_rpc.shutdown_daemon(spec.protocol_prefix, timeout_s=0.5, state_file=state_file)
    except Exception:
        pass
    thread.join(timeout=3.0)


def test_daemon_server_writes_state_file(daemon: tuple[ProviderDaemonSpec, Path, Thread]) -> None:
    spec, state_file, _thread = daemon
    st = askd_rpc.read_state(state_file)
    assert isinstance(st, dict)
    for k in ("pid", "host", "port", "token"):
        assert k in st
    assert st["token"]
    assert int(st["pid"]) > 0
    assert int(st["port"]) > 0
    # host may be "127.0.0.1" or "localhost" depending on socketserver
    assert isinstance(st["host"], str) and st["host"]
    # connect_host should exist (AskDaemonServer writes it)
    assert isinstance(st.get("connect_host"), str)
    assert st.get("connect_host")
    if os.name != "nt":
        mailbox_path = Path(str(st.get("mailbox_dir") or ""))
        assert mailbox_path.is_dir()
        assert stat.S_IMODE(mailbox_path.stat().st_mode) == 0o700
        assert (mailbox_path / "requests").is_dir()
        assert (mailbox_path / "responses").is_dir()


def test_daemon_ping_pong(daemon: tuple[ProviderDaemonSpec, Path, Thread]) -> None:
    spec, state_file, _thread = daemon
    assert askd_rpc.ping_daemon(spec.protocol_prefix, timeout_s=0.5, state_file=state_file) is True


def test_daemon_mailbox_rejects_wrong_token(
    daemon: tuple[ProviderDaemonSpec, Path, Thread], monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, state_file, _thread = daemon
    state = askd_rpc.read_state(state_file)
    assert isinstance(state, dict)
    if not state.get("mailbox_dir"):
        pytest.skip("filesystem mailbox is unavailable on this platform")
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")

    response = askd_rpc.request_daemon(
        state,
        {"type": f"{spec.protocol_prefix}.ping", "v": 1, "id": "bad-token", "token": "wrong"},
        connect_timeout_s=0.5,
        response_timeout_s=0.5,
    )

    assert response["exit_code"] == 1
    assert response["reply"] == "Unauthorized"


def test_daemon_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Use a dedicated daemon instance for this test so we can shut it down.
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CCB_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("CCB_ITEST_IDLE_TIMEOUT_S", "0")

    spec = _make_spec()
    state_file = tmp_path / "state" / "itest.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    def handler(_msg: dict) -> dict:
        return {"type": f"{spec.protocol_prefix}.response", "v": 1, "id": "x", "exit_code": 0, "reply": "OK"}

    server = AskDaemonServer(
        spec=spec,
        host="127.0.0.1",
        port=0,
        token="test-token",
        state_file=state_file,
        request_handler=handler,
    )
    thread = Thread(target=server.serve_forever, name="itest-daemon-shutdown", daemon=True)
    thread.start()
    _wait_for_file(state_file, timeout_s=3.0)
    state = askd_rpc.read_state(state_file) or {}
    mailbox_path = Path(str(state.get("mailbox_dir") or "")) if state.get("mailbox_dir") else None

    assert askd_rpc.ping_daemon(spec.protocol_prefix, timeout_s=0.5, state_file=state_file) is True
    assert askd_rpc.shutdown_daemon(spec.protocol_prefix, timeout_s=0.5, state_file=state_file) is True
    thread.join(timeout=3.0)
    assert askd_rpc.ping_daemon(spec.protocol_prefix, timeout_s=0.2, state_file=state_file) is False
    if mailbox_path is not None:
        assert not mailbox_path.exists()


def test_client_try_daemon_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CCB_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("CCB_ITEST_IDLE_TIMEOUT_S", "0")

    spec = _make_spec()
    state_file = tmp_path / "state" / "itest.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    def handler(msg: dict) -> dict:
        return {
            "type": f"{spec.protocol_prefix}.response",
            "v": 1,
            "id": msg.get("id"),
            "exit_code": 0,
            "reply": f"echo:{msg.get('message') or ''} out:{msg.get('output_path') or ''}",
        }

    server = AskDaemonServer(
        spec=spec,
        host="127.0.0.1",
        port=0,
        token="test-token",
        state_file=state_file,
        request_handler=handler,
    )
    thread = Thread(target=server.serve_forever, name="itest-daemon-client", daemon=True)
    thread.start()
    _wait_for_file(state_file, timeout_s=3.0)

    # Create a fake daemon module that exposes read_state(state_file=...),
    # so askd_client.try_daemon_request can import it.
    module_name = "itest_daemon_module"
    mod = types.ModuleType(module_name)

    def _read_state(*, state_file: Path | None = None) -> dict | None:
        if state_file is None:
            return None
        return askd_rpc.read_state(state_file)

    mod.read_state = _read_state  # type: ignore[attr-defined]
    sys.modules[module_name] = mod

    client_spec = ProviderClientSpec(
        protocol_prefix=spec.protocol_prefix,
        enabled_env="CCB_ITEST_ENABLED",
        autostart_env_primary="CCB_ITEST_AUTOSTART",
        autostart_env_legacy="CCB_AUTO_ITEST",
        state_file_env="CCB_ITEST_STATE_FILE",
        session_filename=".itest-session",
        daemon_bin_name="itestd",
        daemon_module=module_name,
    )

    monkeypatch.setenv(client_spec.enabled_env, "1")
    # try_daemon_request requires a session file to exist in the work dir (or a parent).
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / client_spec.session_filename).write_text("{}", encoding="utf-8")

    out_path = tmp_path / "out.txt"
    reply, exit_code = try_daemon_request(
        client_spec,
        work_dir,
        message="hello",
        timeout=1.0,
        quiet=True,
        state_file=state_file,
        output_path=out_path,
    ) or (None, None)
    assert reply == f"echo:hello out:{out_path}"
    assert exit_code == 0

    assert askd_rpc.shutdown_daemon(spec.protocol_prefix, timeout_s=0.5, state_file=state_file) is True
    thread.join(timeout=3.0)
