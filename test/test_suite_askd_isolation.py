"""The suite must never reach the askd daemon it was started from.

Regression guard for 2026-11-11: a full-suite run left the caller's own askd
recording a graceful shutdown with no idle/parent line -- i.e. it received a
shutdown request. `test/conftest.py` captures the machine's live daemons before
any fixture runs and refuses their state files, state DICTS, endpoints, tokens
and mailboxes, plus any child process that would run with the real home.

Every probe here aims at a DISPOSABLE daemon this module registers with the
guard (a temp state file, an endpoint nothing listens on, a temp mailbox), never
at the real one: if the guard ever regresses, these tests fail on the double and
the caller's own daemon is never the thing that was asked to shut down.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import askd_rpc
import conftest


def _free_port() -> int:
    """A port nothing is listening on (bound, read, released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def fake_live_daemon(tmp_path: Path):
    """A disposable 'live' daemon, registered with the guard for this test."""
    root = tmp_path / "fake-live"
    mailbox = root / "mailbox"
    (mailbox / "requests").mkdir(parents=True)
    (mailbox / "responses").mkdir(parents=True)
    state = root / "askd.json"
    payload = {
        "pid": os.getpid(),
        "host": "127.0.0.1",
        "port": _free_port(),
        "token": "faketoken1234",
        "mailbox_dir": str(mailbox),
    }
    state.write_text(json.dumps(payload), encoding="utf-8")
    entry = conftest.register_live_state(
        {
            "state": state,
            "pid": payload["pid"],
            "host": payload["host"],
            "port": payload["port"],
            "token": payload["token"],
            "mailbox": str(mailbox),
        }
    )
    try:
        yield state, payload, mailbox
    finally:
        conftest.unregister_live_state(entry)


def test_the_guard_refuses_the_live_state_file(fake_live_daemon):
    state, _payload, _mailbox = fake_live_daemon

    with pytest.raises(AssertionError, match="LIVE askd"):
        askd_rpc.ping_daemon("ask", timeout_s=0.05, state_file=state)


def test_the_guard_refuses_shutdown_against_the_live_state(fake_live_daemon):
    state, _payload, _mailbox = fake_live_daemon

    with pytest.raises(AssertionError, match="LIVE askd"):
        askd_rpc.shutdown_daemon("ask", timeout_s=0.05, state_file=state)


def test_the_guard_refuses_a_live_state_dict(fake_live_daemon):
    _state, payload, _mailbox = fake_live_daemon
    state = {
        "host": payload["host"],
        "connect_host": payload["host"],
        "port": payload["port"],
        "token": payload["token"],
    }

    with pytest.raises(AssertionError, match="LIVE askd"):
        askd_rpc.request_daemon(
            state,
            {"type": "ask.shutdown", "v": 1, "id": "x", "token": payload["token"]},
            connect_timeout_s=0.05,
            response_timeout_s=0.05,
        )


def test_the_guard_refuses_a_write_into_the_live_mailbox(fake_live_daemon):
    _state, _payload, mailbox = fake_live_daemon
    request = mailbox / "requests" / "from-a-test.json"

    with pytest.raises(AssertionError, match="LIVE askd"):
        askd_rpc._write_json_atomic(request, {"type": "ask.shutdown"})
    assert not request.exists()


def test_the_guard_refuses_a_child_process_with_the_real_home(fake_live_daemon):
    with pytest.raises(AssertionError, match="LIVE askd"):
        subprocess.run(
            [sys.executable, "-c", "print(1)"],
            env={"HOME": str(conftest._REAL_HOME), "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
        )


def test_the_guard_knows_every_daemon_this_machine_is_running():
    """The captured set covers what is actually running (read-only check)."""
    running = conftest._capture_live_daemons()
    if not running:
        pytest.skip("no live askd on this machine")
    known = {str(item["state"]) for item in conftest.live_states() if item.get("state")}
    assert {str(item["state"]) for item in running} <= known


def test_no_test_damaged_the_live_daemon_state():
    """The canary: the caller's own state file is byte-identical after a test."""
    real = conftest._LIVE_ASKD
    if not real:
        pytest.skip("no live askd on this machine")
    for item in real:
        assert item["state"].is_file(), f"the live state file vanished: {item['state']}"


def test_a_test_cannot_leave_a_launcher_watchdog_thread_behind():
    """The launcher's watchdog thread would outlive the test and the fake HOME."""
    import threading

    ran: list[str] = []
    thread = threading.Thread(
        target=lambda: ran.append("watchdog"), name="askd-watchdog", daemon=True
    )
    thread.start()
    thread.join(timeout=5)

    assert not ran, "the launcher watchdog loop was allowed to run inside the suite"

    other = threading.Thread(
        target=lambda: ran.append("other"), name="not-a-watchdog", daemon=True
    )
    other.start()
    other.join(timeout=5)
    assert ran == ["other"], "an unrelated thread was disarmed"
