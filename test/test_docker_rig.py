"""End-to-end acceptance matrix for cross-machine CCB Milestone 1.

Runs only with `CCB_DOCKER_RIG=1`, because it builds a Docker image, starts a
disposable "machine B" container and drives a real sshd + tmux + CCB launch.
The default suite skips it.

    CCB_DOCKER_RIG=1 pytest test/test_docker_rig.py -q

Evidence for every row comes from the DESTINATION side (its journal records and
the stub agents' logs), never from sender output alone.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "rig"))

import remote_client  # noqa: E402  (source-side library, used for row 4)
import remote_protocol as proto  # noqa: E402
from rig import DEST_PROJECT, REPO, Rig, wait_until  # noqa: E402

RIG_ENABLED = os.environ.get("CCB_DOCKER_RIG") == "1"
#: The real HOME at import time: row 14 re-runs pytest, and some rows isolate
#: HOME for the source side.
_REAL_HOME = os.environ.get("HOME", "")

pytestmark = pytest.mark.skipif(
    not RIG_ENABLED,
    reason="cross-machine rig: set CCB_DOCKER_RIG=1 to build and run the Docker 'machine B' acceptance matrix",
)

#: The real message text used by the T1/T3 fixtures (em-dash, "->", newlines).
ROW1_TEXT = (
    "ratifier: Plan ratification request — Cross-machine CCB Milestone 1 (one-way delivery).\n\n"
    "Claim: the frozen Oct-3 design (docs/plans/cross-machine-ccb-proposal.md) can start with an "
    "even thinner first milestone than the one the user named: A->B delivery-only "
    "(reply_expected=no, terminal state 'delivered')"
)
ROW1_MARKER = "ROW1-ROUNDTRIP-MARKER-2f4c"
LOCAL_ID = "20261109-182536-922-2626995"


@pytest.fixture(scope="session")
def rig():
    instance = Rig()
    try:
        instance.start()
        yield instance
    finally:
        instance.stop()


def _delivered(rig: Rig, request_id: str, timeout: float = 40.0) -> dict:
    return wait_until(
        lambda: (record := rig.journal_record(request_id)) and record["state"] == "delivered" and record,
        timeout,
    )


def _new_request_id(local: str = LOCAL_ID, token: str = "riga1") -> str:
    return proto.make_remote_id(local, token)


# --------------------------------------------------------------------------- #
# Row 1: round trip, and the ACL negative control
# --------------------------------------------------------------------------- #


def test_row1_round_trip_delivery_and_acl_control(rig: Rig):
    rig.reset_destination()
    rig.clear_stub_log()

    proc = rig.ask("codex", "--peer", "rig::demo", "--notify", f"{ROW1_MARKER}\n{ROW1_TEXT}", timeout=180)
    assert proc.returncode == 0, proc.stderr
    receipt = rig.latest_receipt()
    assert receipt["state"] == "accepted", receipt
    request_id = receipt["request_id"]

    record = _delivered(rig, request_id)
    assert record and record["state"] == "delivered", rig.journal()
    assert record["principal"] == "rig-a"
    assert record["pane_id"] == rig.pane_id
    assert [entry["state"] for entry in record["history"]] == [
        "accepted-pending", "dispatch-in-progress", "delivered",
    ]

    log = rig.stub_log()
    assert log.count(ROW1_MARKER) == 1
    assert "CCB_REMOTE: this message was relayed from another machine" in log
    assert "CCB_REPLY_EXPECTED: no" in log
    assert f"CCB_REMOTE_REQUEST_ID: {request_id}" in log
    assert "A->B delivery-only (reply_expected=no, terminal state 'delivered')" in log

    status = rig.remote("status", request_id)
    assert status.returncode == 0, status.stderr
    assert "state=delivered" in status.stdout

    # Negative control: an alias outside the principal's ACL is refused before
    # acceptance, and nothing reaches the pane or the journal.
    before_records = rig.journal_count()
    before_log = rig.stub_log()
    denied = rig.probe("submit", "--host", "rig", "--project", "secret-project", "--text", "must not arrive")
    assert denied.get("ok") is False
    assert denied["ack"]["error"]["code"] == "unauthorized"
    assert rig.journal_count() == before_records
    assert rig.stub_log() == before_log

    # Negative control: a second principal with no grant for `demo`.
    denied_x = rig.probe("submit", "--host", "rigx", "--project", "demo", "--text", "must not arrive")
    assert denied_x.get("ok") is False
    assert denied_x["ack"]["error"]["code"] == "unauthorized"
    assert rig.journal_count() == before_records
    assert rig.stub_log() == before_log


# --------------------------------------------------------------------------- #
# Row 2: wrong key
# --------------------------------------------------------------------------- #


def test_row2_wrong_key_is_refused_and_the_right_key_reaches_the_gate(rig: Rig):
    # Control: the authorised key on the same command negotiates hello.
    control = rig.probe("hello", "--host", "rig")
    assert control.get("ok") is True, control
    assert control["capabilities"]["proto"]["major"] == 1

    before = rig.journal_count()
    log_before = rig.stub_log()
    # Same command, same forced-command entry, but an unauthorised key: the
    # probe selects the `badkey` host alias (token riga1, wrong identity file).
    bad = rig.probe("submit", "--host", "badkey", "--text", "wrong key must not arrive")
    assert bad.get("ok") is False
    assert bad.get("error") == "eof", bad
    assert rig.journal_count() == before
    assert rig.stub_log() == log_before
    log = rig.sshd_log()
    # sshd ends a failed key authentication at LogLevel VERBOSE like this.
    assert "authenticating user b" in log or "Failed publickey" in log or "Permission denied" in log

    # And a second wrong-key attempt through the CLI is reported as
    # `not-delivered`, never `accepted`.
    receipt_before = len(rig.receipts())
    asked = rig.ask("codex", "--peer", "badkey::demo", "--notify", "wrong key via ask")
    assert asked.returncode != 0
    new_receipts = [r for r in rig.receipts()[receipt_before:] if r]
    assert new_receipts and new_receipts[-1]["state"] == "not-delivered"


# --------------------------------------------------------------------------- #
# Row 3: host down
# --------------------------------------------------------------------------- #


def test_row3_host_down_is_bounded_not_delivered_and_recovers(rig: Rig):
    subprocess.run(["docker", "pause", rig.container], check=True, timeout=120)
    started = time.monotonic()
    try:
        proc = rig.ask("codex", "--peer", "rig::demo", "--notify", "host down must not arrive", timeout=180)
        elapsed = time.monotonic() - started
    finally:
        subprocess.run(["docker", "unpause", rig.container], check=True, timeout=120)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert elapsed < 60, elapsed
    receipt = rig.latest_receipt()
    assert receipt["state"] == "not-delivered", receipt
    assert "nothing accepted" not in receipt["state"]

    # Control: with the container up the same command reaches the gate.
    ok = wait_until(lambda: rig.probe("hello", "--host", "rig").get("ok") is True, 60)
    assert ok is True
    assert rig.stub_log().count("host down must not arrive") == 0


# --------------------------------------------------------------------------- #
# Row 4: lost acknowledgement
# --------------------------------------------------------------------------- #


class _DropSubmitAck:
    """A real connection whose submit acknowledgement is never read."""

    def __init__(self, inner):
        self.inner = inner
        self.reads = 0

    def write_all(self, payload: bytes, timeout_s=None) -> int:
        return self.inner.write_all(payload, timeout_s)

    def read_frame(self, timeout_s=None) -> dict:
        self.reads += 1
        if self.reads >= 2:
            raise remote_client.SessionError("eof", "connection closed before the acknowledgement")
        return self.inner.read_frame(timeout_s)

    def close(self) -> None:
        self.inner.close()


def test_row4_lost_ack_is_acceptance_unknown_then_status_recovers(rig: Rig, monkeypatch):
    rig.reset_destination()
    rig.clear_stub_log()
    monkeypatch.setenv("HOME", str(rig.tmp / "home"))
    for name in ("CCB_SSH_BIN", "CCB_REMOTE_OP_TIMEOUT_S", "CCB_PANE_LOCK_DISABLE"):
        monkeypatch.delenv(name, raising=False)

    marker = "ROW4-LOSTACK-MARKER"
    result = remote_client.submit(
        "rig", "demo", "codex", f"{marker} body",
        out=open(os.devnull, "w"), err=open(os.devnull, "w"),
        session_factory=lambda argv: _DropSubmitAck(remote_client.SshSession(argv)),
    )
    assert result.state == remote_client.STATE_ACCEPTANCE_UNKNOWN, result
    assert result.exit_code == remote_client.EXIT_ACCEPTANCE_UNKNOWN

    record = _delivered(rig, result.request_id)
    assert record and record["state"] == "delivered"
    assert rig.stub_log().count(marker) == 1

    # The recovery path: status mirrors the destination state into the receipt.
    status = rig.remote("status", result.request_id)
    assert status.returncode == 0, status.stderr
    assert "state=delivered" in status.stdout
    receipt = rig.latest_receipt()
    assert receipt["state"] == "delivered" and receipt["remote_state"] == "delivered"

    # Negative control: a failure before any submit byte is written is
    # `not-delivered`, not "unknown".
    def explode(_argv):
        raise OSError("connection refused before submit")

    dead = remote_client.submit(
        "rig", "demo", "codex", "never written",
        out=open(os.devnull, "w"), err=open(os.devnull, "w"), session_factory=explode,
    )
    assert dead.state == remote_client.STATE_NOT_DELIVERED


# --------------------------------------------------------------------------- #
# Row 5: duplicate id, same digest / changed payload / new id
# --------------------------------------------------------------------------- #


def test_row5_duplicate_id_semantics(rig: Rig):
    rig.reset_destination()
    rig.clear_stub_log()
    request_id = _new_request_id()
    marker = "ROW5-DUPLICATE-MARKER"

    first = rig.probe("submit", "--id", request_id, "--text", f"{marker} original")
    assert first.get("ok") is True, first
    assert _delivered(rig, request_id)
    assert rig.stub_log().count(marker) == 1

    again = rig.probe("submit", "--id", request_id, "--text", f"{marker} original")
    assert again.get("ok") is True, again
    assert again["ack"]["state"] == "delivered"  # idempotent: the CURRENT state
    assert rig.journal_count() == 1
    assert rig.stub_log().count(marker) == 1

    changed = rig.probe("submit", "--id", request_id, "--text", "changed text")
    assert changed.get("ok") is False
    assert changed["ack"]["error"]["code"] == "idempotency_conflict"
    assert rig.journal_count() == 1
    assert "changed text" not in rig.stub_log()

    new_id = _new_request_id(f"{LOCAL_ID}-1")
    fresh = rig.probe("submit", "--id", new_id, "--text", "fresh new id body")
    assert fresh.get("ok") is True, fresh
    assert rig.journal_count() == 2


# --------------------------------------------------------------------------- #
# Row 6: concurrent duplicate submit from two processes
# --------------------------------------------------------------------------- #


def test_row6_concurrent_duplicate_submit_delivers_once(rig: Rig):
    rig.reset_destination()
    rig.clear_stub_log()
    request_id = _new_request_id(f"{LOCAL_ID}-2")
    marker = "ROW6-CONCURRENT-MARKER"
    text = f"{marker} only once please"

    env = rig.source_env()
    argv = [
        sys.executable, str(REPO / "test" / "rig" / "protocol_probe.py"),
        "submit", "--id", request_id, "--text", text,
    ]
    procs = [subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(2)]
    outs = [proc.communicate(timeout=180) for proc in procs]
    assert all(proc.returncode == 0 for proc in procs), outs

    assert _delivered(rig, request_id)
    assert rig.journal_count() == 1, rig.journal()
    assert rig.stub_log().count(marker) == 1


# --------------------------------------------------------------------------- #
# Row 7: identical local id under two issuer tokens
# --------------------------------------------------------------------------- #


def test_row7_same_local_id_under_two_tokens_is_two_records(rig: Rig):
    rig.reset_destination()
    rig.clear_stub_log()
    first_id = _new_request_id(LOCAL_ID, "riga1")
    second_id = _new_request_id(LOCAL_ID, "rigb2")

    first = rig.probe("submit", "--host", "rig", "--id", first_id, "--text", "row7 from rig-a")
    second = rig.probe("submit", "--host", "rigb", "--id", second_id, "--text", "row7 from rig-b2")
    assert first.get("ok") is True, first
    assert second.get("ok") is True, second

    assert _delivered(rig, first_id)
    assert _delivered(rig, second_id)
    records = {record["request_id"]: record for record in rig.journal()}
    assert set(records) == {first_id, second_id}
    assert records[first_id]["principal"] == "rig-a"
    assert records[second_id]["principal"] == "rig-b2"
    assert rig.stub_log().count("row7 from rig-a") == 1
    assert rig.stub_log().count("row7 from rig-b2") == 1


# --------------------------------------------------------------------------- #
# Row 8: an unauthorised principal learns nothing
# --------------------------------------------------------------------------- #


def test_row8_unauthorised_principal_sees_nothing(rig: Rig):
    rig.reset_destination()
    delivered_id = _new_request_id()
    assert rig.probe("submit", "--id", delivered_id, "--text", "row8 private body").get("ok") is True
    assert _delivered(rig, delivered_id)

    listing = rig.probe("list", "--host", "rigx")
    assert listing.get("ok") is True, listing
    assert listing["response"]["projects"] == []

    other = rig.probe("status", "--host", "rigx", "--id", delivered_id)
    missing = rig.probe("status", "--host", "rigx", "--id", _new_request_id("20260125-143000-123-12345", "rigx1"))
    assert other.get("ok") is False and missing.get("ok") is False
    # Identical apart from the transport echo of our own op id.
    for response in (other["response"], missing["response"]):
        response.pop("op_id", None)
    assert other["response"] == missing["response"], (other, missing)
    assert other["response"]["error"]["code"] == "not_found"
    assert "row8 private body" not in json.dumps(other)


# --------------------------------------------------------------------------- #
# Row 9: gate hardening through the real sshd
# --------------------------------------------------------------------------- #


def _port_open_in_container(rig: Rig, port: int) -> bool:
    out = rig.dest(
        f"python3 -c \"import socket,sys\n"
        f"s=socket.socket(); s.settimeout(2)\n"
        f"sys.exit(0 if s.connect_ex(('127.0.0.1',{port}))==0 else 1)\"",
        check=False,
    )
    return out.returncode == 0


def _plain(text: str | None) -> str:
    """ssh's client output with the pty's carriage returns removed."""
    return (text or "").replace("\r", "")


def _sshd_accepted_sessions(rig: Rig) -> int:
    """How many public-key sessions sshd has ACCEPTED so far (destination side)."""
    return rig.sshd_log().count("Accepted publickey for b")


def _open_forward_listener(port: int, timeout: float = 15.0):
    """Connect to a local-forward listener, or fail the test if it is not there.

    A client-side failure (no listener, no route, auth refused) must FAIL the
    probe: it proves nothing about the destination.
    """
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError as exc:  # retried while ssh sets the listener up
            last = exc
            time.sleep(0.1)
    raise AssertionError(f"the local-forward listener on 127.0.0.1:{port} never accepted a connection: {last}")


def test_row9_real_sshd_refuses_pty_command_forwarding_and_subsystem(rig: Rig):
    before_journal = rig.journal_count()
    before_log = rig.stub_log()

    # 1. A remote command is refused by the GATE, at the destination: its own
    #    log line is on stderr, with its own exit code, and no file was created.
    accepted_before = _sshd_accepted_sessions(rig)
    shell = rig.raw_ssh("-T", command=["touch", "/tmp/rig-should-not-exist"])
    shell_err = _plain(shell.stderr)
    assert "outcome=refused-original-command" in shell_err, shell_err
    assert shell.returncode == 2, (shell.returncode, shell_err)
    assert rig.dest("test -e /tmp/rig-should-not-exist", check=False).returncode != 0
    assert _sshd_accepted_sessions(rig) == accepted_before + 1, "the probe never reached the destination sshd"

    # 2. A PTY request is refused, and the gate STILL RUNS without a pty.
    pty = rig.raw_ssh_with_pty()
    pty_out = _plain((pty.stdout or "") + (pty.stderr or ""))
    assert "PTY allocation request failed on channel 0" in pty_out, pty_out
    assert "ccb-remote-gate principal=rig-a" in pty_out, pty_out
    assert "outcome=eof" in pty_out, pty_out
    assert pty.returncode == 0, (pty.returncode, pty_out)

    # 3. Local forwarding: the client's listener accepts, the server refuses the
    #    channel ("administratively prohibited"), and the client stays alive.
    forward = subprocess.Popen(
        rig.ssh_argv(["-N", "-L", "127.0.0.1:38999:127.0.0.1:22"]),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        with _open_forward_listener(38999) as sock:
            sock.settimeout(10)
            assert sock.recv(16) == b"", "the forwarded channel carried data: forwarding was allowed"
        assert forward.poll() is None, "the ssh client exited instead of holding the forward"
    finally:
        forward.terminate()
        try:
            forward.wait(timeout=10)
        except subprocess.TimeoutExpired:
            forward.kill()
    forward_err = _plain(forward.stderr.read())
    assert "administratively prohibited" in forward_err, forward_err

    # 4. Remote forwarding: the server refuses to bind, and the port is unbound.
    remote_forward = rig.raw_ssh(
        "-o", "ExitOnForwardFailure=yes", "-R", "127.0.0.1:38998:127.0.0.1:22", "-N", timeout=30
    )
    remote_err = _plain(remote_forward.stderr)
    assert remote_forward.returncode != 0, (remote_forward.returncode, remote_err)
    assert "remote port forwarding failed for listen port 38998" in remote_err, remote_err
    assert not _port_open_in_container(rig, 38998), "the destination bound a forwarded port"

    # 5. A subsystem request reaches the destination unauthenticated by the
    #    protocol: the gate refuses it (exit 2), and sshd shows the session.
    accepted_before = _sshd_accepted_sessions(rig)
    subsystem = rig.raw_ssh("-s", command=["sftp"], timeout=30)
    assert subsystem.returncode == 2, (subsystem.returncode, _plain(subsystem.stderr))
    assert _sshd_accepted_sessions(rig) == accepted_before + 1, (
        "the subsystem probe never authenticated to the destination"
    )

    # Nothing was ever delivered to a pane, and the protocol still works.
    assert rig.journal_count() == before_journal
    assert rig.stub_log() == before_log
    control = rig.probe("hello", "--host", "rig")
    assert control.get("ok") is True, control

    # Evidence lines, quoted in the run report.
    print("ROW9 shell stderr:", shell_err.strip())
    print("ROW9 pty output:", " | ".join(pty_out.splitlines()[:3]))
    print("ROW9 local-forward stderr:", forward_err.strip())
    print("ROW9 remote-forward stderr:", remote_err.strip())


# --------------------------------------------------------------------------- #
# Row 10: restart while the record is accepted-pending
# --------------------------------------------------------------------------- #


def test_row10_restart_requeues_accepted_pending_and_delivers_once(rig: Rig):
    rig.reset_destination()
    rig.clear_stub_log()
    marker = "ROW10-RESTART-MARKER"
    holder = rig.hold_pane_lock(seconds=25)
    try:
        assert holder.stdout is not None
        assert wait_until(lambda: holder.stdout.readline().strip() == "held", 20)
        request_id = _new_request_id(f"{LOCAL_ID}-5")
        early = rig.probe("submit", "--id", request_id, "--text", f"{marker} after restart")
        assert early.get("ok") is True, early
        # Nothing is dispatched while the lock is held.
        record = wait_until(
            lambda: (r := rig.journal_record(request_id)) and r["state"] == "accepted-pending" and r, 20
        )
        assert record and record["state"] == "accepted-pending", rig.journal()

        rig.askd_restart()  # recovery runs here, after exclusive ownership
    finally:
        holder.kill()
        holder.wait(timeout=20)

    record = _delivered(rig, request_id, timeout=60)
    assert record and record["state"] == "delivered", rig.journal()
    assert [entry["state"] for entry in record["history"]].count("delivered") == 1
    assert rig.stub_log().count(marker) == 1


# --------------------------------------------------------------------------- #
# Row 11: destination replaced while queued
# --------------------------------------------------------------------------- #


def test_row11_replaced_pane_is_refused_and_never_redirected(rig: Rig):
    rig.reset_destination()
    rig.clear_stub_log()
    marker = "ROW11-REPLACED-MARKER"
    after_log = "/srv/demo/.stub-after.log"
    rig.dest(f": > {after_log}")

    holder = rig.hold_pane_lock(seconds=20)
    try:
        assert holder.stdout is not None
        assert wait_until(lambda: holder.stdout.readline().strip() == "held", 20)
        request_id = _new_request_id(f"{LOCAL_ID}-6")
        early = rig.probe("submit", "--id", request_id, "--text", f"{marker} must not be redirected")
        assert early.get("ok") is True, early
        assert wait_until(
            lambda: (r := rig.journal_record(request_id)) and r["state"] == "accepted-pending", 20
        )

        # The destination is REPLACED while the delivery waits: the pane the
        # record was accepted for no longer carries its CCB identity, and a
        # different pane (logging separately) now stands in for it. Both the
        # title AND the stable @ccb_marker option must change: the product
        # matches either. Killing the pane would also kill the askd that owns
        # the record, which would strand it at accepted-pending.
        rig.dest(
            f"tmux select-pane -t {rig.pane_id} -T 'foreign-pane' && "
            f"tmux set-option -p -t {rig.pane_id} @ccb_marker 'CCB-Foreign'",
            check=False,
        )
        new_pane = rig.dest(
            f"cd {DEST_PROJECT} && tmux new-window -d -t launch "
            f"'CCB_RIG_STUB_LOG={after_log} codex' && sleep 1 "
            "&& tmux list-panes -a -F '#{pane_id}' | tail -1",
            check=False,
        ).stdout.strip()
        assert new_pane and new_pane != rig.pane_id, new_pane
        time.sleep(1.5)
    finally:
        holder.kill()
        holder.wait(timeout=20)

    record = wait_until(
        lambda: (r := rig.journal_record(request_id)) and r["state"] in {"failed", "outcome-unknown"} and r, 60
    )
    assert record and record["state"] == "failed", rig.journal()
    reason = record["history"][-1].get("reason", "").lower()
    assert "marker" in reason or "destination" in reason, reason
    assert marker not in rig.stub_log()
    assert marker not in rig.dest(f"cat {after_log} 2>/dev/null", check=False).stdout


# --------------------------------------------------------------------------- #
# Row 12: concurrent local and remote delivery to one pane
# --------------------------------------------------------------------------- #


def test_row12_concurrent_local_and_remote_delivery_keeps_both_prompts(rig: Rig):
    rig.reset_destination()
    rig.clear_stub_log()
    rig.dest_root("mkdir -p /srv/other && chown b:b /srv/other")
    launched = rig.dest(
        "cd /srv/other && tmux new-session -d -s other 'cd /srv/other && ccb codex'",
        check=False,
    )
    assert launched.returncode == 0, launched.stderr
    ready = wait_until(
        lambda: rig.dest(
            "ls /home/b/.ccb/run/ccb-session-*.json 2>/dev/null | wc -l", check=False
        ).stdout.strip() == "2",
        60,
    )
    assert ready, "the second project did not launch"

    remote_marker = "ROW12-REMOTE-MARKER"
    local_marker = "ROW12-LOCAL-MARKER"
    remote_id = _new_request_id(f"{LOCAL_ID}-7")
    env = rig.source_env()
    remote_proc = subprocess.Popen(
        [sys.executable, str(REPO / "test" / "rig" / "protocol_probe.py"), "submit",
         "--id", remote_id, "--text", f"{remote_marker} remote body"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    local_proc = subprocess.Popen(
        ["docker", "exec", "-u", "b", rig.container, "bash", "-lc",
         "cd /srv/other && CCB_CALLER=codex CCB_WORK_DIR=/srv/other "
         f"ask codex --peer {DEST_PROJECT} --notify '{local_marker} local body'"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    remote_out = remote_proc.communicate(timeout=180)
    local_out = local_proc.communicate(timeout=180)
    assert remote_proc.returncode == 0, remote_out
    assert local_proc.returncode == 0, local_out

    assert _delivered(rig, remote_id, timeout=60)
    log = wait_until(lambda: (text := rig.stub_log()) and local_marker in text and text, 60)
    assert log and remote_marker in log and local_marker in log
    # Both prompts arrived whole: each marker line carries its own complete
    # body, so neither prompt was spliced into the other.
    lines = [line[5:] for line in log.splitlines() if line.startswith("LINE ")]
    local_lines = [line for line in lines if local_marker in line]
    remote_lines = [line for line in lines if remote_marker in line]
    assert len(local_lines) == 1 and len(remote_lines) == 1, (local_lines, remote_lines)
    assert "local body" in local_lines[0] and remote_marker not in local_lines[0]
    assert "remote body" in remote_lines[0] and local_marker not in remote_lines[0]


# --------------------------------------------------------------------------- #
# Row 13: role-enforcing destination
# --------------------------------------------------------------------------- #


def _set_destination_role(rig: Rig, role: str) -> None:
    record = rig.registry()
    member_id = record["providers"]["codex"].get("member_id", "")
    member_token = record["providers"]["codex"].get("member_token", "")
    assert member_id and member_token
    rig.dest(
        f"CCB_MEMBER_ID={member_id} CCB_MEMBER_TOKEN={member_token} "
        f"/home/b/.local/bin/ccb-role set {role}",
        check=False,
    )


def test_row13_role_enforcing_destination_refuses_non_lead(rig: Rig):
    rig.reset_destination()
    rig.clear_stub_log()
    # Destination-side STATE only: the launch record carries the policy and the
    # session's role. No product file is touched.
    rig.patch_registry(role_policy="enforce")
    _set_destination_role(rig, "implementer")

    refused_id = _new_request_id(f"{LOCAL_ID}-3")
    refused = rig.probe("submit", "--id", refused_id, "--text", "row13 must not be delivered")
    assert refused.get("ok") is True  # accepted: the refusal happens at dispatch
    record = wait_until(lambda: (r := rig.journal_record(refused_id)) and r["state"] == "failed" and r, 30)
    assert record and record["state"] == "failed", rig.journal()
    assert "role policy refused" in record["history"][-1].get("reason", "")
    assert rig.stub_log().count("row13 must not be delivered") == 0

    # Control: the same destination as lead accepts it.
    _set_destination_role(rig, "lead")
    lead_id = _new_request_id(f"{LOCAL_ID}-4")
    lead = rig.probe("submit", "--id", lead_id, "--text", "row13 lead delivery")
    assert lead.get("ok") is True, lead
    assert _delivered(rig, lead_id)
    assert rig.stub_log().count("row13 lead delivery") == 1


# --------------------------------------------------------------------------- #
# Row 14: the default suite is unaffected
# --------------------------------------------------------------------------- #


def test_row14_rig_is_skipped_without_the_environment_variable(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "CCB_DOCKER_RIG"}
    if _REAL_HOME:
        env["HOME"] = _REAL_HOME
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__).resolve()), "-q", "--collect-only"],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Collection is unchanged; the module-level gate is what skips the rows.
    source = Path(__file__).read_text(encoding="utf-8")
    assert 'os.environ.get("CCB_DOCKER_RIG") == "1"' in source
