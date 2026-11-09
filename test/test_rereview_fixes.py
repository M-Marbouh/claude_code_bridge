"""Regressions for the re-review fixes (ratifier CONTEST on T2fix + T3)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import remote_client
import remote_protocol as proto
from askd.adapters.base import ProviderRequest, QueuedTask
from remote_client import (
    EXIT_ACCEPTANCE_UNKNOWN,
    EXIT_ACCEPTED,
    EXIT_NOT_DELIVERED,
    STATE_ACCEPTED,
    STATE_ACCEPTANCE_UNKNOWN,
    STATE_NOT_DELIVERED,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HOST_ALIAS = "testhost"


# --------------------------------------------------------------------------- #
# Shared harness: a scripted destination, no real ssh
# --------------------------------------------------------------------------- #


def _write_hosts(tmp_path: Path, *, known_hosts_name: str = "known_hosts", issuer: str = "abc1") -> None:
    identity = tmp_path / "id_ed25519"
    identity.write_text("private", encoding="utf-8")
    known = tmp_path / known_hosts_name
    known.parent.mkdir(parents=True, exist_ok=True)
    known.write_text("example.test ssh-ed25519 AAAA\n", encoding="utf-8")
    path = Path.home() / ".ccb" / "remote" / "hosts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hosts": {
                    HOST_ALIAS: {
                        "ssh_target": "relay@example.test",
                        "port": 2222,
                        "identity_file": str(identity),
                        "known_hosts": str(known),
                        "issuer_token": issuer,
                    }
                }
            }
        ),
        encoding="utf-8",
    )


class _Destination:
    """Answers hello, then replies to submit with a chosen ack."""

    def __init__(self, *, ack: dict | None = None, mode: str = "ok", drop: bool = False):
        self.mode = mode
        self.ack = ack or {}
        self.drop = drop
        self.hello_op_id = ""
        self.submit: dict | None = None
        self.writes: list[bytes] = []
        self.closed = False

    # -- transport ------------------------------------------------------ #
    def write_all(self, payload: bytes, timeout_s=None) -> int:
        self.writes.append(payload)
        frame = json.loads(payload[4:].decode("utf-8"))
        if frame.get("op") == "hello":
            self.hello_op_id = str(frame.get("op_id") or "")
        else:
            self.submit = frame
        return len(payload)

    def read_frame(self, timeout_s=None) -> dict:
        if self.submit is None:
            return {
                "ok": True,
                "op": "hello",
                "op_id": self.hello_op_id,
                "capabilities": {
                    "verbs": ["hello", "list", "submit", "status"],
                    "reply_modes": {"claude": ["delivery-only"], "codex": ["delivery-only"]},
                    "max_frame": 262144,
                    "proto": {"major": 1, "minor": 0},
                },
            }
        if self.drop:
            raise remote_client.SessionError("eof", "connection closed before the acknowledgement")
        body = self.submit["body"]
        base = {
            "ok": True,
            "op": "submit",
            "op_id": self.submit["op_id"],
            "request_id": body["request_id"],
            "digest": body["digest"],
            "state": "accepted-pending",
        }
        base.update(self.ack)
        return base

    def close(self) -> None:
        self.closed = True


def _submit(dest: _Destination, **kwargs):
    return remote_client.submit(
        HOST_ALIAS,
        "example-project",
        "claude",
        "hello from the source",
        session_factory=lambda _argv: dest,
        out=open(os.devnull, "w"),
        err=open(os.devnull, "w"),
        **kwargs,
    )


@pytest.fixture()
def hosts(tmp_path: Path) -> Path:
    _write_hosts(tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- #
# Fix 1: a receipt whose directory cannot be synced never opens a connection
# --------------------------------------------------------------------------- #


def test_receipt_dir_fsync_failure_sends_nothing(hosts, monkeypatch):
    started: list[list[str]] = []

    def boom(_path):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(remote_client, "_fsync_dir", boom)
    dest = _Destination()
    result = remote_client.submit(
        HOST_ALIAS,
        "example-project",
        "claude",
        "hello",
        session_factory=lambda argv: started.append(argv) or dest,
        out=open(os.devnull, "w"),
        err=open(os.devnull, "w"),
    )

    assert started == [], "ssh was spawned although the receipt could not be made durable"
    assert result.state == STATE_NOT_DELIVERED
    assert result.exit_code == EXIT_NOT_DELIVERED
    assert "receipt could not be written" in result.reason
    assert dest.writes == []


def test_fsync_dir_propagates_errors(hosts, monkeypatch):
    directory = Path.home() / ".ccb" / "remote" / "outbound"
    directory.mkdir(parents=True, exist_ok=True)
    assert remote_client._fsync_dir(directory) is None  # positive control

    def boom(_fd):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(remote_client.os, "fsync", boom)
    with pytest.raises(OSError):
        remote_client._fsync_dir(directory)


# --------------------------------------------------------------------------- #
# Fix 2: the acknowledgement fails closed
# --------------------------------------------------------------------------- #


def test_a_full_valid_ack_is_accepted(hosts):
    dest = _Destination()
    result = _submit(dest)
    assert result.state == STATE_ACCEPTED
    assert result.exit_code == EXIT_ACCEPTED
    assert result.remote_state == "accepted-pending"


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_id": ""},  # missing request id
        {"request_id": "20260101-000000-000-1-hzzzz"},  # someone else's request
        {"digest": "b" * 64},  # different payload
        {"state": "failed"},  # not a state acceptance can be in
        {"state": ""},  # no state at all
        {"ok": "true"},  # not a boolean verdict
        {"ok": None},
    ],
)
def test_untrustworthy_acceptance_is_acceptance_unknown(hosts, overrides):
    dest = _Destination(ack=overrides)
    result = _submit(dest)

    assert result.state == STATE_ACCEPTANCE_UNKNOWN, (overrides, result)
    assert result.exit_code == EXIT_ACCEPTANCE_UNKNOWN


@pytest.mark.parametrize(
    "code",
    [
        "unauthorized",
        "not_found",
        "invalid_request",
        "unsupported_provider",
        "reply_expected_unsupported",
        "destination_unavailable",
        "journal_failure",
        "idempotency_conflict",
        "daemon_unavailable",
        "config_invalid",
    ],
)
def test_allowlisted_pre_acceptance_refusals_are_not_delivered(hosts, code):
    dest = _Destination(ack={"ok": False, "error": {"code": code, "message": "refused"}})
    result = _submit(dest)

    assert result.state == STATE_NOT_DELIVERED, (code, result)
    assert result.exit_code == EXIT_NOT_DELIVERED
    assert result.error_code == code


@pytest.mark.parametrize(
    "code",
    [
        "internal_error",  # raised AFTER the record exists
        "acceptance_unknown",
        "malformed_frame",
        "protocol_version",
        "weird_future_code",
        "",
    ],
)
def test_everything_else_after_the_write_is_acceptance_unknown(hosts, code):
    ack: dict = {"ok": False}
    if code:
        ack["error"] = {"code": code, "message": "refused"}
    dest = _Destination(ack=ack)
    result = _submit(dest)

    assert result.state == STATE_ACCEPTANCE_UNKNOWN, (code, result)
    assert result.exit_code == EXIT_ACCEPTANCE_UNKNOWN


def test_ack_for_another_operation_is_acceptance_unknown(hosts):
    dest = _Destination(ack={"op_id": "somebody-elses-op"})
    result = _submit(dest)
    assert result.state == STATE_ACCEPTANCE_UNKNOWN


# --------------------------------------------------------------------------- #
# Fix 3: writes never block past the deadline and never lose byte progress
# --------------------------------------------------------------------------- #


def _session_for_sleeper() -> remote_client.SshSession:
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    return remote_client.SshSession(argv)


def _close_hard(session: remote_client.SshSession) -> None:
    """Kill the sleeper first: `close()` would otherwise wait its 5 s grace."""
    try:
        session._proc.kill()
    except Exception:
        pass
    session.close()


def test_write_does_not_block_past_the_deadline_on_a_full_pipe():
    session = _session_for_sleeper()
    try:
        payload = b"x" * 8_000_000  # far beyond any pipe buffer
        started = time.monotonic()
        written = session.write_all(payload, timeout_s=0.25)
        elapsed = time.monotonic() - started
    finally:
        _close_hard(session)

    assert elapsed < 5.0, elapsed
    assert 0 < written < len(payload), written


def test_write_failure_after_progress_preserves_the_count(monkeypatch):
    session = _session_for_sleeper()
    calls = {"n": 0}
    real_write = os.write

    def partial_then_fail(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, bytes(data[:100]))
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(remote_client.os, "write", partial_then_fail)
    try:
        written = session.write_all(b"y" * 200_000, timeout_s=5.0)
    finally:
        _close_hard(session)
    assert written == 100


def test_write_select_failure_after_progress_preserves_the_count(monkeypatch):
    session = _session_for_sleeper()
    real_select = remote_client.select.select
    calls = {"n": 0}

    def first_ready_then_fail(read, write, exc, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_select(read, write, exc, timeout)
        raise OSError("connection reset")

    monkeypatch.setattr(remote_client.select, "select", first_ready_then_fail)
    try:
        written = session.write_all(b"z" * 8_000_000, timeout_s=5.0)
    finally:
        _close_hard(session)
    assert written > 0


def test_write_failure_with_no_progress_is_an_error(monkeypatch):
    session = _session_for_sleeper()

    def always_fail(_fd, _data):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(remote_client.os, "write", always_fail)
    try:
        with pytest.raises(remote_client.SessionError) as excinfo:
            session.write_all(b"w" * 1024, timeout_s=1.0)
    finally:
        _close_hard(session)
    assert excinfo.value.kind == "closed"


# --------------------------------------------------------------------------- #
# Fix 4: daemon shutdown releases the singleton lock
# --------------------------------------------------------------------------- #


def _server(tmp_path: Path, lock_name: str):
    import askd_server
    from providers import ProviderDaemonSpec

    spec = ProviderDaemonSpec(
        daemon_key="askd",
        protocol_prefix="ask",
        state_file_name="askd.json",
        log_file_name="askd.log",
        idle_timeout_env="X",
        lock_name=lock_name,
    )
    return askd_server.AskDaemonServer(
        spec=spec,
        token="t",
        state_file=tmp_path / "askd.json",
        request_handler=lambda msg: {},
    )


def test_shutdown_releases_ownership_so_a_new_server_can_take_it(tmp_path):
    first = _server(tmp_path, f"reprobe-{os.getpid()}-a")
    second = _server(tmp_path, f"reprobe-{os.getpid()}-a")  # same lock name

    assert first._take_ownership() is True
    assert second._take_ownership() is False, "two owners of one daemon"

    first._release_ownership()

    assert second._take_ownership() is True
    second._release_ownership()


def test_release_ownership_is_idempotent(tmp_path):
    server = _server(tmp_path, f"reprobe-{os.getpid()}-b")
    assert server._take_ownership() is True
    server._release_ownership()
    server._release_ownership()  # no lock held: must be a no-op


def test_shutdown_path_uses_the_ownership_accessor():
    source = Path("lib/askd_server.py").read_text(encoding="utf-8")
    assert "self._release_ownership()" in source
    # The old bug: releasing a name that is out of scope in serve_forever.
    serve = source.split("def serve_forever(self) -> int:", 1)[1]
    assert "lock.release()" not in serve


# --------------------------------------------------------------------------- #
# Fix 6: ssh_config filenames with spaces stay ONE filename
# --------------------------------------------------------------------------- #


def test_known_hosts_path_with_a_space_is_one_ssh_config_filename(tmp_path):
    known_dir = tmp_path / "with space"
    _write_hosts(tmp_path, known_hosts_name="with space/known_hosts")
    hosts = remote_client.load_hosts()
    host = hosts[HOST_ALIAS]
    assert " " in host.known_hosts

    argv = remote_client.ssh_argv(host)
    encoded = f"-o UserKnownHostsFile={remote_client.ssh_config_filename(host.known_hosts)}"
    assert "-o" in argv
    assert f'UserKnownHostsFile="{host.known_hosts}"' in argv

    # A REAL ssh_config parse of the value we hand sshd's client.
    proc = subprocess.run(
        ["ssh", "-G", "-F", "/dev/null", "-o", f'UserKnownHostsFile="{host.known_hosts}"', "example.test"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    parsed = [
        line.split(" ", 1)[1]
        for line in proc.stdout.splitlines()
        if line.startswith("userknownhostsfile ")
    ]
    assert parsed and parsed[0] == str(host.known_hosts), parsed
    assert known_dir.exists()


def test_quoting_is_what_keeps_a_space_path_single(tmp_path):
    """Positive control: the SAME parser rejects the unquoted form."""
    spacey = tmp_path / "with space" / "id_ed25519"
    quoted = subprocess.run(
        ["ssh", "-G", "-F", "/dev/null", "-o", f'IdentityFile="{spacey}"', "example.test"],
        capture_output=True, text=True, timeout=60,
    )
    unquoted = subprocess.run(
        ["ssh", "-G", "-F", "/dev/null", "-o", f"IdentityFile={spacey}", "example.test"],
        capture_output=True, text=True, timeout=60,
    )
    assert quoted.returncode == 0, quoted.stderr
    assert unquoted.returncode != 0
    assert "extra arguments" in unquoted.stderr
    assert f'identityfile {spacey}' in quoted.stdout


def test_quotes_and_backslashes_in_a_path_round_trip(tmp_path):
    tricky = str(tmp_path / 'a"b' / "back\\slash" / "known_hosts")
    encoded = remote_client.ssh_config_filename(tricky)
    proc = subprocess.run(
        ["ssh", "-G", "-F", "/dev/null", "-o", f"UserKnownHostsFile={encoded}", "example.test"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    parsed = [
        line.split(" ", 1)[1]
        for line in proc.stdout.splitlines()
        if line.startswith("userknownhostsfile ")
    ]
    assert parsed and parsed[0] == tricky, (encoded, parsed)


# --------------------------------------------------------------------------- #
# Fix 5: routed Claude/Codex re-confirm the destination AFTER the lock wait
# --------------------------------------------------------------------------- #

from live_sessions import LiveSession, Resolution  # noqa: E402


class _FakeBackend:
    """A pane backend whose marker resolves to the same pane it is asked about."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.is_alive_calls = 0

    def is_alive(self, _pane):
        self.is_alive_calls += 1
        return True

    def find_pane_by_title_marker(self, _marker, _cwd):
        return "%9"

    def pane_matches_cwd_strict(self, _pane, _cwd):
        return True

    def send_text(self, pane, text):
        self.sent.append((pane, text))


def _routed_session_file(tmp_path: Path) -> Path:
    path = tmp_path / "codex-session-s2.json"
    path.write_text(
        json.dumps(
            {
                "pane_id": "%9",
                "pane_title_marker": "CCB-Codex-route",
                "work_dir": str(tmp_path),
                "session_id": "ai-1",
            }
        ),
        encoding="utf-8",
    )
    return path


def _route(tmp_path: Path, session_file: Path):
    from askd.adapters.base import ResolvedRoute

    return ResolvedRoute(
        live_id="s2",
        launch_id="ai-1",
        caller_live_id="s1",
        pane_id="%9",
        terminal="tmux",
        session_file=str(session_file),
        ccb_project_id="proj-1",
    )


def _live(tmp_path: Path, session_file: Path) -> LiveSession:
    return LiveSession(
        live_id="s2",
        provider="codex",
        launch_id="ai-1",
        pane_id="%9",
        terminal="tmux",
        pane_title_marker="CCB-Codex-route",
        work_dir=str(tmp_path),
        session_file=str(session_file),
        ccb_project_id="proj-1",
    )


def _hold_pane_lock(backend, pane: str):
    """Hold the pane injection lock in another thread for the duration."""
    import pane_lock

    started = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with pane_lock.pane_injection_lock(backend, pane, 10.0):
            started.set()
            release.wait(10.0)

    thread = threading.Thread(target=hold)
    thread.start()
    assert started.wait(5.0)
    return thread, release


class _DestinationWatch:
    """A validate_route stand-in whose verdict can flip while a task waits."""

    def __init__(self, live):
        self.live = live
        self.calls = 0
        self.replaced = False

    def __call__(self, **_kwargs):
        self.calls += 1
        if self.replaced:
            return Resolution(error="unavailable", detail="the routed session was replaced")
        return Resolution(session=self.live)


def _run_while_replacing(adapter, task, backend, watch, *, replace_after: float | None):
    """Run handle_task while the pane lock is held; optionally replace the target."""
    thread, release = _hold_pane_lock(backend, "%9")
    outcome: dict = {}

    def run() -> None:
        outcome["result"] = adapter.handle_task(task)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        time.sleep(0.3)  # the adapter is now waiting for the pane lock
        if replace_after is not None:
            watch.replaced = True
    finally:
        release.set()
        thread.join(10.0)
    worker.join(30.0)
    assert not worker.is_alive()
    return outcome["result"]


def _build_routed_case(adapter_name, tmp_path, monkeypatch):
    if adapter_name == "codex":
        import askd.adapters.codex as adapter_module
        import caskd_session as session_module
    else:
        import askd.adapters.claude as adapter_module
        import laskd_session as session_module

    session_file = _routed_session_file(tmp_path)
    live = _live(tmp_path, session_file)
    backend = _FakeBackend()
    watch = _DestinationWatch(live)

    monkeypatch.setattr(adapter_module, "validate_route", watch)
    monkeypatch.setattr(adapter_module, "get_backend_for_session", lambda _data: backend)
    monkeypatch.setattr(session_module, "get_backend_for_session", lambda _data: backend)
    monkeypatch.setattr(adapter_module, "_write_log", lambda _line: None)
    monkeypatch.setattr(adapter_module, "notify_completion", lambda **_kw: None)
    monkeypatch.setattr(adapter_module, "persist_proven_result", lambda **_kw: None)
    monkeypatch.setattr(adapter_module, "wrap_codex_delivery_prompt", lambda message, _req: message, raising=False)
    monkeypatch.setattr(adapter_module, "wrap_claude_delivery_prompt", lambda message, _req: message, raising=False)
    monkeypatch.setattr(adapter_module, "wrap_codex_prompt", lambda message, _req: message, raising=False)
    monkeypatch.setattr(adapter_module, "wrap_claude_prompt", lambda message, _req: message, raising=False)

    class _Reader:
        def capture_state(self):
            return {}

        def wait_for_event(self, state, _timeout):
            return None, state

        def read_reply(self):
            return "final answer", None

    monkeypatch.setattr(adapter_module, "CodexLogReader", lambda **_kw: _Reader(), raising=False)
    monkeypatch.setattr(adapter_module, "ClaudeLogReader", lambda **_kw: _Reader(), raising=False)

    adapter = adapter_module.__dict__[f"{adapter_name.capitalize()}Adapter"]()
    return adapter_module, adapter, backend, watch, session_file


def _routed_task(tmp_path: Path, session_file: Path, *, delivery_only: bool = True) -> QueuedTask:
    request = ProviderRequest(
        client_id="c",
        work_dir=str(tmp_path),
        timeout_s=0.2,
        quiet=True,
        message="do it",
        caller="claude",
        req_id="20261109-182536-922-2626995",
        delivery_only=delivery_only,
        route=_route(tmp_path, session_file),
    )
    return QueuedTask(request=request, created_ms=0, req_id=request.req_id, done_event=threading.Event())


@pytest.mark.parametrize("adapter_name", ["codex", "claude"])
def test_routed_destination_is_reconfirmed_after_the_lock_wait(adapter_name, tmp_path, monkeypatch):
    adapter_module, adapter, backend, watch, session_file = _build_routed_case(
        adapter_name, tmp_path, monkeypatch
    )
    result = _run_while_replacing(
        adapter, _routed_task(tmp_path, session_file), backend, watch, replace_after=0.3
    )

    assert result.exit_code == 1, result
    assert "routed destination" in result.reply.lower(), result.reply
    assert backend.sent == [], "injected although the destination changed while waiting"
    assert watch.calls >= 2, "the destination was not revalidated after the wait"


@pytest.mark.parametrize("adapter_name", ["codex", "claude"])
def test_unchanged_routed_destination_is_delivered(adapter_name, tmp_path, monkeypatch):
    """Positive control: the same harness with a stable destination sends once."""
    adapter_module, adapter, backend, watch, session_file = _build_routed_case(
        adapter_name, tmp_path, monkeypatch
    )
    result = _run_while_replacing(
        adapter, _routed_task(tmp_path, session_file), backend, watch, replace_after=None
    )

    assert backend.sent, result.reply
    assert watch.calls >= 2
    assert len(backend.sent) == 1
