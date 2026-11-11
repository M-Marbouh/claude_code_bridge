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
from rig import DEST_PROJECT, DEST_STUB_CONFIG, REPO, Rig, wait_until  # noqa: E402

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

#: A reply body that exercises the wire: blank lines, unicode, a trailing
#: newline, and a line that looks like a completion marker.
ROW15_ANSWER = (
    "row15 answer, line one\n"
    "\n"
    "second — with an em-dash and 🙂 emoji\n"
    "\n"
    "CCB_DONE: 20261110-113136-582-89163-1-hmain1\n"
    "trailing newline follows\n"
)
ROW16_ANSWER = "row16 answer ✓ delivered once\n"
ROW17_ANSWER = "row17 answer after a dropped fetch\n"


@pytest.fixture(scope="session")
def rig():
    instance = Rig()
    try:
        instance.start()
        yield instance
    finally:
        instance.stop()


@pytest.fixture(scope="session")
def m1_rig():
    """A destination installed from the shipped M1 tree (compatibility row)."""
    instance = Rig(name_suffix="-m1", source_ref="b996fa6")
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
# M2 rows: explicit replies
# --------------------------------------------------------------------------- #


def _wait_record(rig: Rig, request_id: str, states: set[str], timeout: float = 120.0) -> dict | None:
    return wait_until(
        lambda: (record := rig.journal_record(request_id)) and record.get("state") in states and record,
        timeout,
    )


def test_row15_wait_round_trip_and_reply_controls(rig: Rig):
    rig.reset_destination()
    rig.clear_stub_log()
    rig.set_stub_config(mode="reply", answer=ROW15_ANSWER)
    marker = "ROW15-WAIT-MARKER"

    proc = rig.ask("codex", "--peer", "rig::demo", "--wait", "--timeout", "120",
                   f"{marker} body", timeout=240)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    # `ask` prints the acceptance line first (M1 behaviour), then the answer
    # itself byte for byte: unicode, blank lines and the CCB_DONE-looking line
    # are not reformatted, and the answer appears exactly once.
    assert proc.stdout.endswith(ROW15_ANSWER), repr(proc.stdout)
    assert proc.stdout.count(ROW15_ANSWER) == 1
    assert proc.stdout.splitlines()[0].startswith(f"id={rig.latest_receipt()['request_id']} state=accepted")
    assert proc.stdout == f"id={rig.latest_receipt()['request_id']} state=accepted destination=accepted-pending reply-expected=yes\n" + ROW15_ANSWER

    receipt = rig.latest_receipt()
    request_id = receipt["request_id"]
    assert receipt["state"] == "fetched", receipt
    assert receipt["reply_text"] == ROW15_ANSWER
    assert receipt["reply_digest"] == proto.compute_reply_digest(ROW15_ANSWER)
    assert f"id={request_id} state=fetched" in proc.stderr

    record = _wait_record(rig, request_id, {"answered"})
    assert record and record["state"] == "answered", rig.journal()
    assert record["reply_text"] == ROW15_ANSWER
    assert record["reply_digest"] == receipt["reply_digest"]
    assert [entry["state"] for entry in record["history"]] == [
        "accepted-pending", "dispatch-in-progress", "delivered", "answered",
    ]

    log = rig.stub_log()
    assert log.count(marker) == 1
    assert "REPLY-DECISION mode=reply" in log
    assert f"ccb-remote reply {request_id}" in log
    assert "REPLY rc=0" in log
    print("ROW15 answer:", repr(ROW15_ANSWER))
    print("ROW15 stub:", [line for line in log.splitlines() if line.startswith("REPLY")][-2:])


def test_row15_reply_to_a_delivery_only_request_is_refused(rig: Rig):
    rig.reset_destination()
    rig.clear_stub_log()
    rig.clear_stub_config()

    delivery = rig.ask("codex", "--peer", "rig::demo", "--notify", "ROW15-CTRL delivery-only body")
    assert delivery.returncode == 0, delivery.stderr
    delivery_id = rig.latest_receipt()["request_id"]
    assert _delivered(rig, delivery_id)
    before = rig.journal_record(delivery_id)

    # The stub answers the *delivery-only* id instead of the reply-expected one.
    rig.set_stub_config(mode="explicit_id", target_id=delivery_id, answer="row15 control answer\n")
    waited = rig.ask(
        "codex", "--peer", "rig::demo", "--wait", "--timeout", "15", "ROW15-CTRL reply body",
        timeout=240,
    )
    assert waited.returncode == 2, (waited.returncode, waited.stdout, waited.stderr)
    assert "still waiting; use pend" in waited.stderr
    v2_id = rig.latest_receipt()["request_id"]
    assert v2_id != delivery_id

    log = rig.stub_log()
    assert f"REPLY-DECISION mode=explicit_id id={delivery_id}" in log
    assert "REPLY rc=1" in log
    assert "did not ask for a reply" in log
    # Neither record changed: the delivery-only one still has no answer, and the
    # reply-expected one is still waiting.
    assert rig.journal_record(delivery_id) == before
    v2_record = rig.journal_record(v2_id)
    assert v2_record["state"] == "delivered"
    assert "reply_text" not in v2_record
    print("ROW15 control stub:", [l for l in log.splitlines() if l.startswith("REPLY")][-2:])
    print("ROW15 control journals:", before["state"], v2_record["state"])


def test_row16_background_injects_the_answer_into_the_caller_pane_once(rig: Rig):
    rig.reset_destination()
    rig.clear_stub_log()
    rig.set_stub_config(mode="reply", answer=ROW16_ANSWER)
    side = rig.start_source_side(with_askd=True)

    proc = side.ask("codex", "--peer", "rig::demo", "--background", "ROW16 body", timeout=300)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "CCB_BACKGROUND_SUBMITTED" in proc.stdout
    request_id = side.latest_receipt()["request_id"]
    assert side.latest_receipt()["poll_owner"] == str(side.state_file)

    receipt = wait_until(
        lambda: (r := side.receipt(request_id)) and r.get("state") == "notified" and r, 180
    )
    assert receipt and receipt["state"] == "notified", (receipt, side.read_askd_log()[-2000:])
    assert receipt["notify_attempted_at"] and receipt["notified_at"]
    assert receipt["reply_text"] == ROW16_ANSWER

    expected = ROW16_ANSWER.rstrip("\n")
    capture = wait_until(lambda: expected in side.capture_text() and side.capture_text(), 20)
    assert capture, "the answer never reached the caller pane"
    assert capture.count(expected) == 1
    assert f"CCB_REMOTE_REQUEST_ID: {request_id}" in capture
    record = _wait_record(rig, request_id, {"answered"})
    assert record and record["state"] == "answered"
    assert rig.stub_log().count("ROW16 body") == 1
    print("ROW16 capture:", " | ".join(capture.splitlines()[-3:]))
    print("ROW16 receipt:", {k: receipt.get(k) for k in ("state", "notify_attempted_at", "notified_at")})


def test_row16_replaced_caller_pane_is_not_notified_and_pend_shows_it(rig: Rig):
    rig.reset_destination()
    rig.clear_stub_log()
    rig.set_stub_config(mode="reply", answer="row16 control answer ✓\n", delay=8)
    side = rig.start_source_side(with_askd=True)

    proc = side.ask("codex", "--peer", "rig::demo", "--background", "ROW16-CTRL body", timeout=300)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    request_id = side.latest_receipt()["request_id"]

    # Replace the caller pane before the destination has answered.
    side.kill_caller_pane()
    # "At most once" means the attempt is followed by a recorded outcome; wait
    # for the outcome (a refusal here), not merely for the attempt marker.
    receipt = wait_until(
        lambda: (r := side.receipt(request_id)) and r.get("notify_refused") and r, 240
    )
    assert receipt and receipt.get("notify_refused"), (receipt, side.read_askd_log()[-2000:])
    assert receipt["state"] == remote_client.STATE_FETCHED, receipt
    assert not receipt.get("notified_at")
    assert side.capture_text() == ""
    print("ROW16 control receipt:", {k: receipt.get(k) for k in ("state", "notify_attempted_at", "notified_at")})

    pended = side.pend(request_id)
    assert pended.returncode == 0, (pended.stdout, pended.stderr)
    assert "row16 control answer" in pended.stdout
    assert side.receipt(request_id)["state"] == remote_client.STATE_FETCHED
    print("ROW16 control refusal:", receipt.get("notify_refused"))


def test_row17_lost_fetch_ack_keeps_the_answer_for_pend(rig: Rig, monkeypatch):
    rig.reset_destination()
    rig.clear_stub_log()
    rig.set_stub_config(mode="reply", answer=ROW17_ANSWER)
    side = rig.start_source_side()

    monkeypatch.setenv("HOME", str(side.home))
    monkeypatch.setenv("TMPDIR", str(side.home.parent / "tmp"))
    for name in ("CCB_SSH_BIN", "CCB_REMOTE_OP_TIMEOUT_S", "CCB_PANE_LOCK_DISABLE"):
        monkeypatch.delenv(name, raising=False)

    result = remote_client.submit(
        "rig", "demo", "codex", "ROW17 body",
        reply_expected=True, intent="wait",
        caller_identity={"pane_id": "", "terminal": ""},
        out=open(os.devnull, "w"), err=open(os.devnull, "w"),
    )
    assert result.state == remote_client.STATE_ACCEPTED, result
    request_id = result.request_id
    record = _wait_record(rig, request_id, {"answered"})
    assert record and record["state"] == "answered"

    # The answer is committed on B; A's fetch response never arrives.
    real_session = remote_client.SshSession

    class DroppedFetch(real_session):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._reads = 0

        def read_frame(self, timeout_s=None):
            self._reads += 1
            if self._reads >= 2:
                raise remote_client.SessionError(
                    "eof", "connection closed before the answer arrived"
                )
            return super().read_frame(timeout_s)

    monkeypatch.setattr(remote_client, "SshSession", DroppedFetch)
    receipt = remote_client.poll_receipt(request_id)
    assert receipt["state"] == remote_client.STATE_FETCH_UNAVAILABLE, receipt
    assert "eof" in str(receipt.get("last_fetch_error")) or "closed" in str(
        receipt.get("last_fetch_error")
    )
    monkeypatch.setattr(remote_client, "SshSession", real_session)

    pended = side.pend(request_id)
    assert pended.returncode == 0, (pended.stdout, pended.stderr)
    assert ROW17_ANSWER.rstrip("\n") in pended.stdout
    stored = side.receipt(request_id)
    assert stored["state"] == remote_client.STATE_FETCHED
    assert stored["reply_text"] == ROW17_ANSWER
    assert stored["reply_digest"] == record["reply_digest"]
    assert rig.stub_log().count("ROW17 body") == 1
    print("ROW17 pend:", " | ".join(pended.stdout.splitlines()[:3]))


def test_row18_reply_authority_is_the_accepted_member_and_pane(rig: Rig):
    rig.reset_destination(providers="codex claude")
    rig.clear_stub_log()

    # Positive control first: the bound pane's own credential answers.
    rig.set_stub_config(mode="reply", answer="row18 control answer\n")
    ok = rig.ask("codex", "--peer", "rig::demo", "--wait", "--timeout", "120",
                 "ROW18 control body", timeout=240)
    assert ok.returncode == 0, (ok.stdout, ok.stderr)
    control_id = rig.latest_receipt()["request_id"]
    control = _wait_record(rig, control_id, {"answered"})
    assert control and control["state"] == "answered"
    assert "REPLY rc=0" in rig.stub_log()

    def _delivered_v2(marker: str) -> str:
        rig.clear_stub_config()
        proc = rig.ask(
            "codex", "--peer", "rig::demo", "--wait", "--timeout", "10", marker, timeout=240
        )
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
        request_id = rig.latest_receipt()["request_id"]
        assert _delivered(rig, request_id, timeout=60)
        return request_id

    # 1. A credential from the SAME launch's other pane (sibling provider).
    other_id = _delivered_v2("ROW18 other-member body")
    before = rig.journal_record(other_id)
    rig.set_stub_config(mode="other_member", answer="row18 other member\n")
    retried = rig.ask(
        "codex", "--peer", "rig::demo", "--wait", "--timeout", "15", "ROW18 trigger other",
        timeout=240,
    )
    assert retried.returncode == 2
    log = rig.stub_log()
    assert "REPLY-DECISION mode=other_member" in log
    assert "REPLY rc=1" in log
    assert "not the request's destination member" in log
    assert rig.journal_record(other_id) == before
    print("ROW18 other-member stub:", [l for l in log.splitlines() if l.startswith("REPLY")][-2:])

    # 2. A process with no CCB launch credential.
    no_cred_id = _delivered_v2("ROW18 no-credential body")
    before = rig.journal_record(no_cred_id)
    rig.set_stub_config(mode="no_credential", answer="row18 no credential\n")
    retried = rig.ask(
        "codex", "--peer", "rig::demo", "--wait", "--timeout", "15", "ROW18 trigger none",
        timeout=240,
    )
    assert retried.returncode == 2
    log = rig.stub_log()
    assert "REPLY-DECISION mode=no_credential" in log
    assert "REPLY rc=1" in log
    assert "no CCB launch credential" in log
    assert rig.journal_record(no_cred_id) == before
    print("ROW18 no-credential stub:", [l for l in log.splitlines() if l.startswith("REPLY")][-2:])

    # 3. The same pane id respawned as a new process, answered by the pane's own
    #    (unchanged) launch credential: the saved process witness no longer matches.
    respawn_id = _delivered_v2("ROW18 respawn body")
    before = rig.journal_record(respawn_id)
    old_pid = rig.dest(
        f"tmux display-message -p -t {rig.pane_id} '#{{pane_pid}}'"
    ).stdout.strip()
    rig.dest(f"tmux respawn-pane -k -t {rig.pane_id} 'codex'", check=False)
    wait_until(
        lambda: rig.dest(
            f"tmux display-message -p -t {rig.pane_id} '#{{pane_pid}}'", check=False
        ).stdout.strip() not in ("", old_pid),
        20,
    )
    new_pid = rig.dest(
        f"tmux display-message -p -t {rig.pane_id} '#{{pane_pid}}'", check=False
    ).stdout.strip()
    assert new_pid and new_pid != old_pid
    member_id, member_token = rig.registry_credential("codex")
    reply = rig.dest(
        f"printf %s 'row18 respawned answer' | "
        f"CCB_MEMBER_ID={member_id} CCB_MEMBER_TOKEN={member_token} "
        f"/home/b/.local/bin/ccb-remote reply {respawn_id}",
        check=False,
    )
    assert reply.returncode == 1, (reply.returncode, reply.stdout, reply.stderr)
    assert "not the one the request was delivered to" in reply.stderr
    assert "pane process could not be confirmed" in reply.stderr
    assert rig.journal_record(respawn_id) == before
    print("ROW18 respawn pid:", old_pid, "->", new_pid)
    print("ROW18 respawn refusal:", reply.stderr.strip())


def test_row19_m1_destination_and_m1_source_compatibility(rig: Rig, m1_rig: Rig):
    # (a) M2 source -> M1 destination: reply-expected refused before submit,
    #     delivery-only still delivered.
    m1_rig.reset_destination()
    m1_rig.clear_stub_log()
    m1_rig.clear_stub_config()
    refused = m1_rig.ask(
        "codex", "--peer", "rig::demo", "--wait", "ROW19 must not arrive", timeout=180
    )
    assert refused.returncode == 1, (refused.returncode, refused.stdout, refused.stderr)
    assert "does not advertise explicit replies" in refused.stderr
    assert m1_rig.journal_count() == 0
    assert "ROW19 must not arrive" not in m1_rig.stub_log()

    delivered = m1_rig.ask(
        "codex", "--peer", "rig::demo", "--notify", "ROW19 delivery only", timeout=180
    )
    assert delivered.returncode == 0, delivered.stderr
    one_way_id = m1_rig.latest_receipt()["request_id"]
    record = _delivered(m1_rig, one_way_id)
    assert record and record["state"] == "delivered", m1_rig.journal()
    assert record["schema_version"] == 1
    assert record["reply_expected"] is False
    assert m1_rig.stub_log().count("ROW19 delivery only") == 1
    print("ROW19 M1 refusal:", (refused.stderr or refused.stdout).strip().splitlines()[-1:])
    print("ROW19 M1 journal:", record["state"], record["schema_version"])

    # (b) The shipped M1 source client -> M2 destination: delivery-only works.
    rig.reset_destination()
    rig.clear_stub_log()
    m1_source = rig.extract_ref("b996fa6")
    old = subprocess.run(
        [sys.executable, str(m1_source / "bin" / "ask"), "codex", "--peer", "rig::demo",
         "--notify", "ROW19 from the M1 client"],
        env=rig.source_env(), capture_output=True, text=True, timeout=300, cwd=str(rig.tmp),
    )
    assert old.returncode == 0, (old.stdout, old.stderr)
    old_id = old.stdout.split()[0].split("=", 1)[-1]
    old_record = _delivered(rig, old_id)
    assert old_record and old_record["state"] == "delivered", rig.journal()
    assert old_record["schema_version"] == 1
    assert rig.stub_log().count("ROW19 from the M1 client") == 1
    print("ROW19 old client:", old.stdout.strip().splitlines()[0])
    print("ROW19 M2 journal:", old_record["state"], old_record["schema_version"])


def test_row20_unauthorised_principal_fetch_is_identical_not_found(rig: Rig):
    rig.reset_destination()
    rig.clear_stub_log()
    rig.set_stub_config(mode="reply", answer="row20 answer\n")

    proc = rig.ask("codex", "--peer", "rig::demo", "--wait", "--timeout", "120",
                   "ROW20 body", timeout=240)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    request_id = rig.latest_receipt()["request_id"]
    assert _wait_record(rig, request_id, {"answered"})

    allowed = rig.probe("fetch", "--host", "rig", "--id", request_id)
    assert allowed.get("ok") is True, allowed
    assert allowed["response"]["state"] == "answered"
    assert allowed["response"]["reply_text"] == "row20 answer\n"

    other = rig.probe("fetch", "--host", "rigx", "--id", request_id)
    missing = rig.probe(
        "fetch", "--host", "rigx",
        "--id", _new_request_id("20260125-143000-123-12345", "rigx1"),
    )
    assert other.get("ok") is False and missing.get("ok") is False
    for response in (other["response"], missing["response"]):
        response.pop("op_id", None)
    assert other["response"] == missing["response"], (other, missing)
    assert other["response"]["error"]["code"] == "not_found"
    assert "row20 answer" not in json.dumps(other)
    print("ROW20 allowed:", allowed["response"]["state"], repr(allowed["response"]["reply_text"]))
    print("ROW20 other/missing identical:", other["response"] == missing["response"],
          other["response"]["error"])


# --------------------------------------------------------------------------- #
# Row 14: the default suite is unaffected
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# M3 W3 rows: guided pairing against a real sshd
# --------------------------------------------------------------------------- #

PAIR_ALIAS = "paired"
PAIR_PRINCIPAL = "rig-paired"
PAIR_TOKEN = "rigp1"
PAIR_REMOTE_CCB = "/home/b/.local/share/codex-dual/bin/ccb-remote"
ROW21_ANSWER = "row21 answer from a paired machine\n"


def _pair(rig: Rig, *extra: str, timeout: float = 300):
    return rig.remote(
        "pair", PAIR_ALIAS,
        "--destination", "b@127.0.0.1",
        "--port", str(rig.port),
        "--principal", PAIR_PRINCIPAL,
        "--token", PAIR_TOKEN,
        "--share", f"demo={DEST_PROJECT}",
        "--remote-ccb", PAIR_REMOTE_CCB,
        # The pairing channel is the user's normal ssh login; the rig names the
        # verified host key and the login key explicitly (its HOME is isolated,
        # and ssh expands `~` from the passwd entry, not $HOME).
        "--known-hosts", rig.pairing_known_hosts(),
        "--pairing-identity", rig.pairing_identity(),
        "--yes", *extra, timeout=timeout,
    )


def _dest_config(rig: Rig) -> dict:
    return json.loads(rig.dest_file("/home/b/.ccb/remote/config.json") or "{}")


def test_row21_pair_end_to_end_then_ask_succeeds(rig: Rig):
    rig.reset_destination()
    rig.clear_stub_log()
    rig.set_stub_config(mode="reply", answer=ROW21_ANSWER)

    paired = _pair(rig)

    assert paired.returncode == 0, (paired.stdout, paired.stderr)
    assert "paired." in paired.stdout
    # Destination side: the new restricted line sits beside the existing ones.
    keys = rig.dest_file("/home/b/.ssh/authorized_keys")
    assert f"--principal {PAIR_PRINCIPAL}" in keys
    assert "--principal rig-a" in keys  # untouched
    config = _dest_config(rig)
    grant = dict(config["principals"][PAIR_PRINCIPAL])
    assert grant.pop("pubkey_sha256")  # B5 ownership evidence
    assert grant == {
        "issuer_token": PAIR_TOKEN,
        "allowed_aliases": ["demo"],
    }
    assert config["aliases"]["demo"]["work_dir"] == DEST_PROJECT
    # Source side: key, pin, host entry -- all written where CCB looks.
    home = rig.remote_home()
    hosts = json.loads((home / ".ccb" / "remote" / "hosts.json").read_text())
    entry = hosts["hosts"][PAIR_ALIAS]
    assert entry["ssh_target"] == "b@127.0.0.1" and entry["port"] == rig.port
    assert entry["issuer_token"] == PAIR_TOKEN
    assert Path(entry["identity_file"]).is_file()
    # The other configured hosts survived the merge.
    assert {"rig", "rigx", "rigb", "badkey"} <= set(hosts["hosts"])
    pin = (home / ".ccb" / "remote" / "known_hosts").read_text()
    assert f"[127.0.0.1]:{rig.port}" in pin
    assert (home / ".ccb" / "remote" / "pairings" / f"{PAIR_ALIAS}.json").is_file()

    # And the pairing actually carries a reply-expected message end to end.
    marker = "ROW21-PAIRED-MARKER"
    waited = rig.ask(
        "codex", "--peer", f"{PAIR_ALIAS}::demo", "--wait", "--timeout", "120",
        f"{marker} body", timeout=300,
    )
    assert waited.returncode == 0, (waited.stdout, waited.stderr)
    assert waited.stdout.endswith(ROW21_ANSWER)
    receipt = rig.latest_receipt()
    assert receipt["state"] == "fetched", receipt
    assert receipt["reply_text"] == ROW21_ANSWER
    record = _wait_record(rig, receipt["request_id"], {"answered"})
    assert record and record["state"] == "answered", rig.journal()
    assert rig.stub_log().count(marker) == 1
    print("ROW21 pair+ask ok:", receipt["request_id"], repr(ROW21_ANSWER))


def test_row22_pair_repeat_is_a_noop(rig: Rig):
    home = rig.remote_home()
    keys_before = rig.dest_bytes("/home/b/.ssh/authorized_keys")
    config_before = rig.dest_bytes("/home/b/.ccb/remote/config.json")
    key_before = (home / ".ccb" / "remote" / "keys" / PAIR_ALIAS).read_bytes()
    hosts_before = (home / ".ccb" / "remote" / "hosts.json").read_text()

    again = _pair(rig)

    assert again.returncode == 0, (again.stdout, again.stderr)
    assert "reused key" in again.stdout
    assert rig.dest_bytes("/home/b/.ssh/authorized_keys") == keys_before
    assert rig.dest_bytes("/home/b/.ccb/remote/config.json") == config_before
    assert (home / ".ccb" / "remote" / "keys" / PAIR_ALIAS).read_bytes() == key_before
    assert (home / ".ccb" / "remote" / "hosts.json").read_text() == hosts_before
    assert rig.dest_file("/home/b/.ssh/authorized_keys").count(f"--principal {PAIR_PRINCIPAL}") == 1
    print("ROW22 repeat is a no-op")


def test_row23_unpair_revokes_its_grant_and_ask_is_refused(rig: Rig):
    home = rig.remote_home()

    removed = rig.remote(
        "unpair", PAIR_ALIAS,
        "--pairing-identity", rig.pairing_identity(),
        "--yes", timeout=300,
    )

    assert removed.returncode == 0, (removed.stdout, removed.stderr)
    keys = rig.dest_file("/home/b/.ssh/authorized_keys")
    assert f"--principal {PAIR_PRINCIPAL}" not in keys
    assert "--principal rig-a" in keys  # another principal's line survives
    config = _dest_config(rig)
    assert PAIR_PRINCIPAL not in config["principals"]
    assert "rig-a" in config["principals"]
    # The shared alias stays: it serves the other principal too.
    assert config["aliases"]["demo"]["work_dir"] == DEST_PROJECT
    # Local state is gone: host entry, key, this endpoint's pin, discovery cache.
    hosts = json.loads((home / ".ccb" / "remote" / "hosts.json").read_text())
    assert PAIR_ALIAS not in hosts["hosts"]
    assert not (home / ".ccb" / "remote" / "keys" / PAIR_ALIAS).exists()
    assert f"[127.0.0.1]:{rig.port}" not in (home / ".ccb" / "remote" / "known_hosts").read_text()
    assert not (home / ".ccb" / "remote" / "discovery" / f"{PAIR_ALIAS}.json").exists()

    refused = rig.ask("codex", "--peer", f"{PAIR_ALIAS}::demo", "--notify", "row23 after unpair")
    assert refused.returncode != 0, (refused.stdout, refused.stderr)
    assert PAIR_ALIAS in (refused.stderr or refused.stdout)
    print("ROW23 unpair ok:", removed.stdout.strip().splitlines()[:2])


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
