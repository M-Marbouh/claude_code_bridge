from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import remote_client
import remote_protocol as proto

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_SSH = REPO_ROOT / "test" / "fixtures" / "fake_ssh.py"
HOST_ALIAS = "testhost"
ISSUER_TOKEN = "abc1"
TARGET = "relay@example.test"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _write_hosts(tmp_path: Path, **overrides) -> Path:
    identity = tmp_path / "id_ed25519"
    identity.write_text("private", encoding="utf-8")
    known = tmp_path / "known_hosts"
    known.write_text("example.test ssh-ed25519 AAAA\n", encoding="utf-8")
    entry = {
        "ssh_target": TARGET,
        "port": 2222,
        "identity_file": str(identity),
        "known_hosts": str(known),
        "issuer_token": ISSUER_TOKEN,
    }
    entry.update(overrides)
    path = Path.home() / ".ccb" / "remote" / "hosts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hosts": {HOST_ALIAS: entry}}), encoding="utf-8")
    return path


@pytest.fixture()
def fake_ssh(monkeypatch, tmp_path: Path) -> Path:
    """Point CCB_SSH_BIN at the fixture and return the argv log path."""
    _write_hosts(tmp_path)
    log = tmp_path / "ssh-argv.jsonl"
    monkeypatch.setenv("CCB_SSH_BIN", str(FAKE_SSH))
    monkeypatch.setenv("CCB_FAKE_SSH_LOG", str(log))
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "ack_ok")
    return log


def _argv_log(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _receipt(request_id: str) -> dict:
    return json.loads(remote_client.receipt_path(request_id).read_text())


# --------------------------------------------------------------------------- #
# Scripted session: transport classification without a real pipe
# --------------------------------------------------------------------------- #


class _ScriptedSession:
    """A session whose frames/writes are scripted per call."""

    def __init__(self, responses: list, *, writes_until_error: int | None = None,
                 partial_write: int | None = None):
        self.responses = list(responses)
        self.writes: list[bytes] = []
        self.writes_until_error = writes_until_error
        self.partial_write = partial_write
        self.closed = False

    def write_all(self, payload: bytes, timeout_s: float | None = None) -> int:
        if self.writes_until_error is not None and len(self.writes) >= self.writes_until_error:
            raise remote_client.SessionError("closed", "connection closed before the request was written")
        self.writes.append(payload)
        if self.partial_write is not None:
            return self.partial_write
        return len(payload)

    def read_frame(self, timeout_s: float | None = None) -> dict:
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def _hello_ok(op_id: str = "hello-1", provider: str = "claude") -> dict:
    return {
        "ok": True,
        "op": "hello",
        "op_id": op_id,
        "capabilities": {
            "verbs": ["hello", "list", "submit", "status"],
            "reply_modes": {provider: ["delivery-only"]},
            "max_frame": 262144,
            "proto": {"major": 1, "minor": 0},
        },
    }


def _submit(tmp_path: Path, session, **kwargs):
    return remote_client.submit(
        HOST_ALIAS,
        "example-project",
        "claude",
        "hello from the source",
        session_factory=lambda _argv: session,
        out=io.StringIO(),
        err=io.StringIO(),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_valid_hosts_config_loads(tmp_path, fake_ssh):
    hosts = remote_client.load_hosts()
    host = hosts[HOST_ALIAS]
    assert host.ssh_target == TARGET
    assert host.port == 2222
    assert host.issuer_token == ISSUER_TOKEN
    assert Path(host.identity_file).is_file()


def test_missing_hosts_config_names_the_file(tmp_path):
    with pytest.raises(remote_client.RemoteConfigError) as excinfo:
        remote_client.load_hosts()
    assert "no remote hosts configured" in str(excinfo.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"ssh_target": "host with space"},
        {"ssh_target": "-oProxyCommand=evil"},
        {"ssh_target": "user@host::x"},
        {"port": 0},
        {"port": 70000},
        {"port": "22"},
        {"issuer_token": "ABC1"},
        {"issuer_token": "ab"},
        {"identity_file": "relative/key"},
        {"identity_file": "/nonexistent/key"},
        {"known_hosts": "/nonexistent/known_hosts"},
        {"unexpected_field": True},
    ],
)
def test_malformed_hosts_config_fails_closed(tmp_path, fake_ssh, overrides):
    _write_hosts(tmp_path, **overrides)
    with pytest.raises(remote_client.RemoteConfigError):
        remote_client.load_hosts()


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        "[]",
        '{"hosts": []}',
        '{"hosts": {"Bad_Alias": {}}}',
    ],
)
def test_malformed_hosts_document_fails_closed(tmp_path, fake_ssh, document):
    path = Path.home() / ".ccb" / "remote" / "hosts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    with pytest.raises(remote_client.RemoteConfigError):
        remote_client.load_hosts()


# --------------------------------------------------------------------------- #
# Selector
# --------------------------------------------------------------------------- #


def test_selector_parses_host_and_project():
    assert remote_client.parse_selector("testhost::example-project") == (
        "testhost",
        "example-project",
    )


@pytest.mark.parametrize(
    "target",
    [
        "testhost",
        "testhost::",
        "::example-project",
        "Bad_Host::example-project",
        "testhost::../etc",
        "testhost::a::b",
        "testhost ::example-project",
    ],
)
def test_bad_selectors_are_refused(target: str):
    with pytest.raises(remote_client.RemoteRefusal):
        remote_client.parse_selector(target)


def test_unknown_host_is_refused_without_any_ssh_call(tmp_path, fake_ssh):
    with pytest.raises(remote_client.RemoteRefusal) as excinfo:
        remote_client.host_for("nope")
    assert "not configured" in str(excinfo.value)
    assert _argv_log(fake_ssh) == []


# --------------------------------------------------------------------------- #
# Receipt before connect
# --------------------------------------------------------------------------- #


def test_receipt_write_failure_never_spawns_ssh(tmp_path, fake_ssh, monkeypatch):
    def boom(_record):
        raise OSError("disk full")

    monkeypatch.setattr(remote_client, "save_receipt", boom)
    result = remote_client.submit(
        HOST_ALIAS, "example-project", "claude", "hello", out=io.StringIO(), err=io.StringIO()
    )
    assert result.state == remote_client.STATE_NOT_DELIVERED
    assert result.exit_code == remote_client.EXIT_NOT_DELIVERED
    assert "receipt could not be written" in result.reason
    assert _argv_log(fake_ssh) == []


def test_receipt_is_written_before_connect_and_carries_identity(tmp_path, fake_ssh):
    result = remote_client.submit(
        HOST_ALIAS,
        "example-project",
        "claude",
        "hello from the source",
        intent="notify",
        caller_claims={"caller": "codex", "pane_id": "%22", "terminal": "tmux", "verified": False},
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert result.ok, result.reason
    receipt = _receipt(result.request_id)
    assert receipt["request_id"] == result.request_id
    assert receipt["host"] == HOST_ALIAS
    assert receipt["dest_project"] == "example-project"
    assert receipt["dest_provider"] == "claude"
    assert receipt["digest"] == result.digest
    assert receipt["created"]
    assert receipt["state"] == remote_client.STATE_ACCEPTED
    assert [entry["state"] for entry in receipt["history"]][0] == remote_client.STATE_SUBMITTING
    assert receipt["caller"]["pane_id"] == "%22"
    # The receipt proves the id shape T1 defines, and binds the issuer token.
    local, token = proto.parse_remote_id(result.request_id)
    assert token == ISSUER_TOKEN
    assert proto._LOCAL_ID_RE.fullmatch(local)
    path = remote_client.receipt_path(result.request_id)
    assert oct(path.stat().st_mode & 0o777) == oct(0o600)
    assert oct(path.parent.stat().st_mode & 0o777) == oct(0o700)
    # Nothing about the message text is persisted on the source side.
    assert "hello from the source" not in path.read_text()


def test_receipt_write_is_durable_without_temp_files_left(tmp_path, fake_ssh):
    result = remote_client.submit(
        HOST_ALIAS, "example-project", "claude", "hello", out=io.StringIO(), err=io.StringIO()
    )
    leftovers = [p.name for p in remote_client.outbound_dir().iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert result.ok


# --------------------------------------------------------------------------- #
# SSH argv
# --------------------------------------------------------------------------- #


_REQUIRED_OPTIONS = [
    "-F", "/dev/null",
    "-T",
    "-o", "BatchMode=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "IdentityAgent=none",
    "-o", "ControlPath=none",
    "-o", "ControlMaster=no",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "GlobalKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=3",
    "-o", "ForwardAgent=no",
    "-o", "ForwardX11=no",
    "-o", "ClearAllForwardings=yes",
    "-o", "PermitLocalCommand=no",
]


def test_ssh_argv_is_isolated_and_sends_no_remote_command(tmp_path, fake_ssh):
    result = remote_client.submit(
        HOST_ALIAS, "example-project", "claude", "hello", out=io.StringIO(), err=io.StringIO()
    )
    assert result.ok
    [argv] = _argv_log(fake_ssh)
    joined = " ".join(argv)
    for item in _REQUIRED_OPTIONS:
        assert item in argv, item
    assert "-i" in argv and argv[argv.index("-i") + 1].endswith("id_ed25519")
    assert "-p" in argv and argv[argv.index("-p") + 1] == "2222"
    assert f'UserKnownHostsFile="{tmp_path / "known_hosts"}"' in argv
    # Target is the last argument: nothing after it can be a remote command.
    assert argv[-1] == TARGET
    assert "-o" in argv and "ProxyCommand=none" not in joined
    assert "ProxyCommand" not in joined
    # No shell: every argument is a plain token.
    for token in argv:
        assert all(ch not in token for ch in (";", "|", "$(", "`", "&&"))
    # One call, not a retry loop.
    assert len(_argv_log(fake_ssh)) == 1


def test_user_ssh_config_proxycommand_cannot_change_the_call(tmp_path, fake_ssh, monkeypatch):
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    (ssh_dir / "config").write_text(
        "Host *\n  ProxyCommand /bin/false\n  IdentityFile /nonexistent\n", encoding="utf-8"
    )
    monkeypatch.setenv("SSH_AUTH_SOCK", "/nonexistent/agent.sock")
    result = remote_client.submit(
        HOST_ALIAS, "example-project", "claude", "hello", out=io.StringIO(), err=io.StringIO()
    )
    assert result.ok
    [argv] = _argv_log(fake_ssh)
    # -F /dev/null is what makes the user's config inert; nothing was picked up.
    # argv[0] of the log is the first OPTION: the fake records sys.argv[1:].
    assert argv[0:2] == ["-F", "/dev/null"]
    assert not any("/nonexistent" in token for token in argv)
    assert "IdentityAgent=none" in argv


# --------------------------------------------------------------------------- #
# Honest states
# --------------------------------------------------------------------------- #


def test_ssh_exit_255_is_not_delivered(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "exit255")
    result = remote_client.submit(
        HOST_ALIAS, "example-project", "claude", "hello", out=io.StringIO(), err=io.StringIO()
    )
    assert result.state == remote_client.STATE_NOT_DELIVERED
    assert result.exit_code == remote_client.EXIT_NOT_DELIVERED
    receipt = _receipt(result.request_id)
    assert receipt["state"] == remote_client.STATE_NOT_DELIVERED
    assert receipt["history"][-1]["ssh"] == "eof"


def test_full_ack_is_accepted(tmp_path, fake_ssh):
    out = io.StringIO()
    result = remote_client.submit(
        HOST_ALIAS, "example-project", "claude", "hello", out=out, err=io.StringIO()
    )
    assert result.state == remote_client.STATE_ACCEPTED
    assert result.exit_code == remote_client.EXIT_ACCEPTED
    assert result.remote_state == "accepted-pending"
    assert f"id={result.request_id}" in out.getvalue()
    assert "state=accepted" in out.getvalue()
    assert _receipt(result.request_id)["remote_state"] == "accepted-pending"


def test_eof_after_submit_is_acceptance_unknown_then_status_recovers(
    tmp_path, fake_ssh, monkeypatch
):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "eof_after_submit")
    result = remote_client.submit(
        HOST_ALIAS, "example-project", "claude", "hello", out=io.StringIO(), err=io.StringIO()
    )
    assert result.state == remote_client.STATE_ACCEPTANCE_UNKNOWN
    assert result.exit_code == remote_client.EXIT_ACCEPTANCE_UNKNOWN
    receipt = _receipt(result.request_id)
    assert receipt["state"] == remote_client.STATE_ACCEPTANCE_UNKNOWN
    assert receipt["history"][-1]["bytes_written"] > 0

    # Recovery: the destination state is now visible through `status`.
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "status_delivered")
    out = io.StringIO()
    status = remote_client.status(result.request_id, out=out, err=io.StringIO())
    assert status.exit_code == remote_client.EXIT_ACCEPTED
    assert status.state == "delivered"
    assert "state=delivered" in out.getvalue()
    recovered = _receipt(result.request_id)
    assert recovered["state"] == "delivered"
    assert recovered["remote_state"] == "delivered"
    assert recovered["remote_history"][-1]["state"] == "delivered"


def test_eof_after_submit_control_ack_ok_is_accepted(tmp_path, fake_ssh):
    """Positive control: the same run with an ack is accepted, not unknown."""
    result = remote_client.submit(
        HOST_ALIAS, "example-project", "claude", "hello", out=io.StringIO(), err=io.StringIO()
    )
    assert result.state == remote_client.STATE_ACCEPTED


def test_hello_only_write_never_reaches_submit(tmp_path, fake_ssh, monkeypatch):
    _write_hosts(tmp_path)
    for mode in ("major2", "no_delivery"):
        monkeypatch.setenv("CCB_FAKE_SSH_MODE", mode)
        if mode == "no_delivery":
            hello = _hello_ok(provider="claude", op_id="hello-1")
            hello["capabilities"]["reply_modes"] = {"claude": [], "codex": []}
        else:
            hello = {
                "ok": True,
                "op": "hello",
                "op_id": "hello-1",
                "capabilities": {
                    "verbs": ["hello"],
                    "reply_modes": {"claude": ["delivery-only"]},
                    "proto": {"major": 2, "minor": 0},
                },
            }
        session = _ScriptedSession(responses=[hello])
        result = _submit(tmp_path, session)
        assert result.state == remote_client.STATE_NOT_DELIVERED
        assert len(session.writes) == 1, "only the hello frame may be written"
        assert b'"op":"hello"' in session.writes[0] or b'"op": "hello"' in session.writes[0]


def test_eof_while_writing_submit_is_not_delivered(tmp_path, fake_ssh):
    session = _ScriptedSession(responses=[_hello_ok()], writes_until_error=1)
    result = _submit(tmp_path, session)
    assert result.state == remote_client.STATE_NOT_DELIVERED
    assert result.exit_code == remote_client.EXIT_NOT_DELIVERED
    assert len(session.writes) == 1  # hello only; the submit write raised


def test_partial_submit_write_then_silence_is_acceptance_unknown(tmp_path, fake_ssh):
    session = _ScriptedSession(
        responses=[_hello_ok(), remote_client.SessionError("eof", "closed")],
        partial_write=12,
    )
    result = _submit(tmp_path, session)
    assert result.state == remote_client.STATE_ACCEPTANCE_UNKNOWN
    assert "only partly written" in result.reason


def test_incompatible_hello_is_not_delivered(tmp_path, fake_ssh, monkeypatch):
    for mode, expected in (("major2", "incompatible"), ("no_delivery", "delivery-only")):
        monkeypatch.setenv("CCB_FAKE_SSH_MODE", mode)
        err = io.StringIO()
        result = remote_client.submit(
            HOST_ALIAS, "example-project", "claude", "hello", out=io.StringIO(), err=err
        )
        assert result.state == remote_client.STATE_NOT_DELIVERED, mode
        assert expected in result.reason, mode
        assert _receipt(result.request_id)["state"] == remote_client.STATE_NOT_DELIVERED


@pytest.mark.parametrize(
    "code",
    ["unauthorized", "destination_unavailable", "journal_failure", "daemon_unavailable", "invalid_request"],
)
def test_structured_refusals_are_not_delivered(tmp_path, fake_ssh, monkeypatch, code):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "ack_refused")
    monkeypatch.setenv("CCB_FAKE_SSH_CODE", code)
    result = remote_client.submit(
        HOST_ALIAS, "example-project", "claude", "hello", out=io.StringIO(), err=io.StringIO()
    )
    assert result.state == remote_client.STATE_NOT_DELIVERED
    assert result.error_code == code
    assert _receipt(result.request_id)["state"] == remote_client.STATE_NOT_DELIVERED


def test_acceptance_unknown_error_code_maps_to_acceptance_unknown(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "ack_unknown")
    result = remote_client.submit(
        HOST_ALIAS, "example-project", "claude", "hello", out=io.StringIO(), err=io.StringIO()
    )
    assert result.state == remote_client.STATE_ACCEPTANCE_UNKNOWN
    assert result.exit_code == remote_client.EXIT_ACCEPTANCE_UNKNOWN


def test_ack_for_another_operation_is_never_accepted(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "bad_ack")
    result = remote_client.submit(
        HOST_ALIAS, "example-project", "claude", "hello", out=io.StringIO(), err=io.StringIO()
    )
    assert result.state == remote_client.STATE_ACCEPTANCE_UNKNOWN
    assert "unusable acknowledgement" in result.reason


def test_per_operation_deadline_is_bounded(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "hang")
    monkeypatch.setenv("CCB_REMOTE_OP_TIMEOUT_S", "0.4")
    started = __import__("time").monotonic()
    result = remote_client.submit(
        HOST_ALIAS, "example-project", "claude", "hello", out=io.StringIO(), err=io.StringIO()
    )
    elapsed = __import__("time").monotonic() - started
    assert elapsed < 10
    assert result.state == remote_client.STATE_NOT_DELIVERED
    assert "in time" in result.reason


def test_dest_session_is_carried_in_the_record(tmp_path, fake_ssh):
    result = remote_client.submit(
        HOST_ALIAS,
        "example-project",
        "codex",
        "hello",
        dest_session="live-7",
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert result.ok
    assert _receipt(result.request_id)["dest_session"] == "live-7"


# --------------------------------------------------------------------------- #
# status / list
# --------------------------------------------------------------------------- #


def test_status_of_missing_receipt_is_refused_without_ssh(tmp_path, fake_ssh):
    request_id = f"20261109-182536-922-2626995-h{ISSUER_TOKEN}"
    with pytest.raises(remote_client.RemoteRefusal):
        remote_client.status(request_id, out=io.StringIO(), err=io.StringIO())
    assert _argv_log(fake_ssh) == []


def test_status_not_found_says_only_unknown_or_not_authorized(tmp_path, fake_ssh, monkeypatch):
    accepted = remote_client.submit(
        HOST_ALIAS, "example-project", "claude", "hello", out=io.StringIO(), err=io.StringIO()
    )
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "status_not_found")
    out = io.StringIO()
    result = remote_client.status(accepted.request_id, out=out, err=io.StringIO())
    assert result.exit_code == remote_client.EXIT_NOT_DELIVERED
    assert out.getvalue().strip() == "state=unknown-or-not-authorized"
    assert "no such request" not in out.getvalue()


def test_status_uses_the_receipt_host_and_updates_the_receipt(tmp_path, fake_ssh, monkeypatch):
    accepted = remote_client.submit(
        HOST_ALIAS, "example-project", "claude", "hello", out=io.StringIO(), err=io.StringIO()
    )
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "status_delivered")
    monkeypatch.setenv("CCB_FAKE_SSH_STATE", "outcome-unknown")
    result = remote_client.status(accepted.request_id, out=io.StringIO(), err=io.StringIO())
    assert result.state == "outcome-unknown"
    receipt = _receipt(accepted.request_id)
    assert receipt["state"] == "outcome-unknown"
    [argv] = _argv_log(fake_ssh)[-1:]
    assert argv[-1] == TARGET  # host resolved from the receipt


def test_list_prints_allowlisted_projects(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "list_ok")
    out = io.StringIO()
    result = remote_client.list_projects(HOST_ALIAS, out=out, err=io.StringIO())
    assert result.exit_code == remote_client.EXIT_ACCEPTED
    assert "example-project" in out.getvalue()
    assert "claude=present" in out.getvalue()
    assert "codex=absent" in out.getvalue()


def test_list_refusal_is_reported_not_silently_empty(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "list_refused")
    err = io.StringIO()
    result = remote_client.list_projects(HOST_ALIAS, out=io.StringIO(), err=err)
    assert result.exit_code == remote_client.EXIT_NOT_DELIVERED
    assert "unauthorized" in result.error_code
    assert "refused" in err.getvalue()


def test_caller_claims_mark_themselves_unverified(monkeypatch):
    monkeypatch.setenv("CCB_CALLER_PANE_ID", "%33")
    monkeypatch.setenv("CCB_CALLER_TERMINAL", "tmux")
    monkeypatch.setenv("CCB_SESSION_ID", "ai-1")
    claims = remote_client.caller_claims(caller="codex", work_dir="/tmp/proj", role_assertion="lead")
    assert claims == {
        "caller": "codex",
        "pane_id": "%33",
        "terminal": "tmux",
        "session_id": "ai-1",
        "work_dir": "/tmp/proj",
        "role_assertion": "lead",
        "verified": False,
    }
