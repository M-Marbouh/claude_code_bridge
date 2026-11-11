"""M3 W3 — the guided `ccb-remote pair` / `unpair` source side.

The other machine is simulated by `test/fixtures/fake_pair_ssh.py`, which stands
in for the user's NORMAL ssh login and executes the REAL destination-side verbs
(`ccb-remote pairing inspect|apply|revoke`) against a temporary "remote" HOME.
So these tests exercise both halves of the product code, and every file they
touch lives under the test's isolated HOME.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import remote_client
import remote_discovery
import remote_pair_cli
import remote_pairing

REPO = Path(__file__).resolve().parents[1]
FAKE_SSH = REPO / "test" / "fixtures" / "fake_pair_ssh.py"
CCB_REMOTE = REPO / "bin" / "ccb-remote"

HOST = "127.0.0.1"
PORT = 2222
DESTINATION = "b@127.0.0.1"
HOST_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIhosthosthosthosthosthosthosthosthostho1"
HOST_KEY_LINE = f"ssh-ed25519 {HOST_KEY}"
OTHER_HOST_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIanotheranotheranotheranotheranotheran1="
PRINCIPAL = "laptop-a"
TOKEN = "alice1"
SHARES = {
    "demo": {"alias": "demo", "work_dir": "/srv/demo", "providers": ["codex", "claude"]}
}


class Env:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.remote_home = tmp_path / "remote-home"
        (self.remote_home / ".ssh").mkdir(parents=True, exist_ok=True)
        (self.remote_home / ".ccb" / "remote").mkdir(parents=True, exist_ok=True)
        (self.remote_home / ".ccb" / "run").mkdir(parents=True, exist_ok=True)
        self.log = tmp_path / "pair.jsonl"
        monkeypatch.setenv("CCB_PAIRING_SSH_BIN", str(FAKE_SSH))
        monkeypatch.setenv("CCB_FAKE_PAIR_LOG", str(self.log))
        monkeypatch.setenv("CCB_FAKE_PAIR_REMOTE_HOME", str(self.remote_home))
        monkeypatch.setenv("CCB_FAKE_PAIR_HOSTKEY", HOST_KEY_LINE)
        # The DELIVERY path (the `list` check after pairing) is a different ssh:
        # the dedicated key against B's gate. Stand in for it so a completed
        # pairing really verifies end to end.
        monkeypatch.setenv("CCB_SSH_BIN", str(REPO / "test" / "fixtures" / "fake_ssh.py"))
        monkeypatch.setenv("CCB_FAKE_SSH_MODE", "list_ok")
        monkeypatch.setenv("CCB_FAKE_SSH_LOG", str(tmp_path / "ssh-argv.jsonl"))
        monkeypatch.delenv("CCB_FAKE_PAIR_MODE", raising=False)
        monkeypatch.delenv("CCB_FAKE_PAIR_NO_GATE", raising=False)
        monkeypatch.delenv("CCB_FAKE_PAIR_GATE", raising=False)
        self.trust(HOST_KEY)

    # -- A-side paths ---------------------------------------------------- #
    @property
    def home(self) -> Path:
        return Path.home()

    @property
    def hosts_json(self) -> Path:
        return self.home / ".ccb" / "remote" / "hosts.json"

    @property
    def pin_file(self) -> Path:
        return self.home / ".ccb" / "remote" / "known_hosts"

    @property
    def key(self) -> Path:
        return self.home / ".ccb" / "remote" / "keys" / "server"

    @property
    def record(self) -> Path:
        return self.home / ".ccb" / "remote" / "pairings" / "server.json"

    # -- B-side paths ---------------------------------------------------- #
    @property
    def remote_keys(self) -> Path:
        return self.remote_home / ".ssh" / "authorized_keys"

    @property
    def remote_config(self) -> Path:
        return self.remote_home / ".ccb" / "remote" / "config.json"

    def trust(self, blob: str) -> None:
        known = self.home / ".ssh" / "known_hosts"
        known.parent.mkdir(parents=True, exist_ok=True)
        known.write_text(f"[{HOST}]:{PORT} ssh-ed25519 {blob}\n", encoding="utf-8")

    def set_mode(self, mode: str) -> None:
        self.monkeypatch.setenv("CCB_FAKE_PAIR_MODE", mode)

    def clear_mode(self) -> None:
        self.monkeypatch.delenv("CCB_FAKE_PAIR_MODE", raising=False)

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def verbs(self) -> list[str]:
        return [call["verb"] for call in self.calls()]

    def remote_state(self) -> tuple[bytes, bytes]:
        return (
            self.remote_keys.read_bytes() if self.remote_keys.exists() else b"",
            self.remote_config.read_bytes() if self.remote_config.exists() else b"",
        )


def _options(**overrides) -> remote_pair_cli.PairOptions:
    options = remote_pair_cli.PairOptions(
        alias="server",
        destination=DESTINATION,
        port=PORT,
        principal=PRINCIPAL,
        token=TOKEN,
        remote_ccb=str(CCB_REMOTE),
        shares=json.loads(json.dumps(SHARES)),
        yes=True,
    )
    for name, value in overrides.items():
        setattr(options, name, value)
    return options


def _run_pair(options: remote_pair_cli.PairOptions, answers: list[str] | None = None):
    out, err = io.StringIO(), io.StringIO()
    replies = list(answers or [])

    def input_fn(_prompt: str) -> str:
        if not replies:
            return ""
        return replies.pop(0)

    code = remote_pair_cli.pair(options, out=out, err=err, input_fn=input_fn)
    return code, out.getvalue(), err.getvalue()


def _run_unpair(options: remote_pair_cli.UnpairOptions, answers: list[str] | None = None):
    out, err = io.StringIO(), io.StringIO()
    replies = list(answers or [])

    def input_fn(_prompt: str) -> str:
        if not replies:
            return ""
        return replies.pop(0)

    code = remote_pair_cli.unpair(options, out=out, err=err, input_fn=input_fn)
    return code, out.getvalue(), err.getvalue()


def _unpair_options(**overrides) -> remote_pair_cli.UnpairOptions:
    options = remote_pair_cli.UnpairOptions(alias="server", yes=True)
    for name, value in overrides.items():
        setattr(options, name, value)
    return options


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    return Env(tmp_path, monkeypatch)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_pair_writes_the_same_artefacts_as_the_manual_steps(env):
    code, out, err = _run_pair(_options())

    assert code == 0, err
    # A: a dedicated key, the pin, the host entry, the pairing record.
    assert env.key.is_file()
    assert (env.home / ".ccb" / "remote" / "keys" / "server.pub").is_file()
    assert f"[{HOST}]:{PORT} ssh-ed25519 {HOST_KEY}" in env.pin_file.read_text()
    document = json.loads(env.hosts_json.read_text())
    entry = document["hosts"]["server"]
    assert entry["ssh_target"] == DESTINATION
    assert entry["port"] == PORT
    assert entry["identity_file"] == str(env.key)
    assert entry["known_hosts"] == str(env.pin_file)
    assert entry["issuer_token"] == TOKEN
    record = json.loads(env.record.read_text())
    assert record["principal"] == PRINCIPAL and record["pubkey"].startswith("ssh-ed25519 ")
    # B: exactly one restricted line plus the matching grants.
    keys = env.remote_keys.read_text()
    assert keys.count("--principal laptop-a") == 1
    assert keys.startswith('restrict,command="')
    assert "ccb-remote-gate --principal laptop-a" in keys
    remote = json.loads(env.remote_config.read_text())
    grant = dict(remote["principals"][PRINCIPAL])
    # B5 ownership evidence: which key this grant was made for.
    assert grant.pop("pubkey_sha256")
    assert grant == {"issuer_token": TOKEN, "allowed_aliases": ["demo"]}
    assert remote["aliases"]["demo"] == {
        "work_dir": "/srv/demo",
        "providers": ["claude", "codex"],
    }
    # The host entry the pairing wrote is one CCB itself accepts.
    loaded = remote_client.load_hosts()
    assert loaded["server"].issuer_token == TOKEN


def test_pair_prints_the_connection_check_and_an_example(env, monkeypatch):
    code, out, _err = _run_pair(_options())

    assert code == 0
    assert "That machine answers as:" in out
    assert "ask codex --peer \"server::demo\"" in out
    # The discovery cache the section uses was populated by that check.
    assert remote_discovery.read_cache("server") is not None


def test_pair_reuses_key_token_and_principal_on_a_rerun(env):
    assert _run_pair(_options())[0] == 0
    key_bytes = env.key.read_bytes()
    remote_before = env.remote_state()

    code, out, err = _run_pair(_options(principal="", token=""))

    assert code == 0, err
    assert env.key.read_bytes() == key_bytes
    assert env.remote_state() == remote_before
    assert "reused key" in out
    record = json.loads(env.record.read_text())
    assert record["principal"] == PRINCIPAL and record["issuer_token"] == TOKEN


# --------------------------------------------------------------------------- #
# Trust first (A1): nothing may be written before the machine is verified
# --------------------------------------------------------------------------- #


def test_pair_stops_with_zero_writes_when_the_machine_is_not_verified(env):
    (env.home / ".ssh" / "known_hosts").unlink()

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "verified host key" in err and "--fingerprint" in err
    assert env.verbs() == []  # not even a read-only connection
    assert not env.key.exists() and not env.hosts_json.exists() and not env.pin_file.exists()
    assert env.remote_state() == (b"", b"")


def test_pair_accepts_a_supplied_fingerprint_when_the_host_is_unknown(env):
    (env.home / ".ssh" / "known_hosts").unlink()
    good = remote_pairing.fingerprint(HOST_KEY)

    code, out, err = _run_pair(_options(fingerprint=good))

    assert code == 0, err
    assert "supplied fingerprint" in out
    assert f"ssh-ed25519 {HOST_KEY}" in env.pin_file.read_text()


def test_pair_refuses_a_fingerprint_mismatch_with_zero_writes(env):
    (env.home / ".ssh" / "known_hosts").unlink()
    wrong = remote_pairing.fingerprint(OTHER_HOST_KEY)

    code, _out, err = _run_pair(_options(fingerprint=wrong))

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "does not match" in err
    assert not env.key.exists() and not env.hosts_json.exists()
    assert env.remote_state() == (b"", b"")
    # The mismatch is caught from the no-authentication key probe, so not even
    # the read-only inspection ran against that machine.
    assert env.verbs() == []


def test_pair_refuses_when_the_reported_key_differs_from_the_verified_one(env):
    env.monkeypatch.setenv("CCB_FAKE_PAIR_HOSTKEY", f"ssh-ed25519 {OTHER_HOST_KEY}")

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "does not match the key selected for this pairing" in err
    assert not env.key.exists() and not env.hosts_json.exists()
    assert env.remote_state() == (b"", b"")


def test_pair_refuses_when_the_user_does_not_type_yes(env):
    code, out, _err = _run_pair(_options(yes=False), answers=["no"])

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "nothing was written" in out
    assert not env.key.exists() and not env.hosts_json.exists()
    assert env.remote_state() == (b"", b"")


def test_pair_refuses_when_the_other_machine_has_no_ccb(env):
    env.monkeypatch.setenv("CCB_FAKE_PAIR_NO_GATE", "1")

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "gate" in err.lower()
    assert not env.key.exists() and not env.hosts_json.exists()
    assert env.remote_state() == (b"", b"")


def test_pair_reports_a_refused_login_without_writing(env):
    env.set_mode("deny")

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "Permission denied" in err
    assert not env.key.exists() and not env.hosts_json.exists()


# --------------------------------------------------------------------------- #
# Partial failure and recovery (A2)
# --------------------------------------------------------------------------- #


def test_an_interrupted_apply_resumes_on_the_next_run(env):
    env.set_mode("fail_apply_once")

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "Re-run" in err
    # The key (step 1) exists; the remote grant and the local wiring do not.
    assert env.key.is_file()
    assert not env.hosts_json.exists() and not env.pin_file.exists()
    assert env.remote_state() == (b"", b"")

    env.clear_mode()
    code, out, err = _run_pair(_options())

    assert code == 0, err
    assert env.remote_keys.read_text().count("--principal laptop-a") == 1
    assert json.loads(env.hosts_json.read_text())["hosts"]["server"]["issuer_token"] == TOKEN


def test_a_remote_state_change_between_inspect_and_apply_is_refused(env):
    """`apply` must not write over a machine that changed after the diff."""

    env.set_mode("change_before_apply")

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "changed" in err and "Re-run" in err
    # The foreign line that appeared is untouched, and no pairing line was
    # added next to it.
    keys = env.remote_keys.read_text()
    assert "foreign" in keys and "--principal laptop-a" not in keys
    # The remote config was not touched either.
    assert env.remote_config.exists() is False or PRINCIPAL not in env.remote_config.read_text()
    # Local wiring is not claimed as done.
    assert not env.hosts_json.exists()


# --------------------------------------------------------------------------- #
# known_hosts merge (the guide's `cp` bug) and unpair
# --------------------------------------------------------------------------- #


def test_pair_merges_the_pin_and_keeps_another_host(env):
    other = f"example.test ssh-ed25519 {OTHER_HOST_KEY}"
    env.pin_file.parent.mkdir(parents=True, exist_ok=True)
    env.pin_file.write_text(other + "\n", encoding="utf-8")

    assert _run_pair(_options())[0] == 0

    lines = [line for line in env.pin_file.read_text().splitlines() if line.strip()]
    assert other in lines
    assert f"[{HOST}]:{PORT} ssh-ed25519 {HOST_KEY}" in lines
    # Re-pairing replaces only this endpoint's line, never duplicates it.
    assert _run_pair(_options())[0] == 0
    lines = [line for line in env.pin_file.read_text().splitlines() if line.strip()]
    assert len(lines) == 2 and lines.count(other) == 1


def test_unpair_revokes_only_its_grant_and_forgets_it_here(env):
    assert _run_pair(_options())[0] == 0
    other_line = 'restrict,command="/opt/ccb/bin/ccb-remote-gate --principal other-b" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIother'
    env.remote_keys.write_text(env.remote_keys.read_text() + other_line + "\n", encoding="utf-8")
    other_pin = f"example.test ssh-ed25519 {OTHER_HOST_KEY}"
    with env.pin_file.open("a", encoding="utf-8") as handle:
        handle.write(other_pin + "\n")

    code, out, err = _run_unpair(_unpair_options())

    assert code == 0, err
    assert "unpaired:" in out
    keys = env.remote_keys.read_text()
    assert "other-b" in keys and "--principal laptop-a" not in keys
    remote = json.loads(env.remote_config.read_text())
    assert "other-b" not in remote["principals"] and PRINCIPAL not in remote["principals"]
    # The shared alias survives, and is reported.
    assert "demo" in remote["aliases"]
    assert "kept on that machine" in out
    # Local state is gone; the other host's pin is not.
    assert not env.hosts_json.exists() or "server" not in json.loads(env.hosts_json.read_text())["hosts"]
    assert not env.key.exists() and not (env.key.parent / "server.pub").exists()
    assert not env.record.exists()
    assert other_pin in env.pin_file.read_text()
    assert f"[{HOST}]:{PORT}" not in env.pin_file.read_text()


def test_unpair_reports_pending_and_keeps_local_config_when_unreachable(env):
    assert _run_pair(_options())[0] == 0
    env.set_mode("deny")

    code, _out, err = _run_unpair(_unpair_options())

    assert code == remote_pair_cli.EXIT_REMOTE_PENDING
    assert "remote revocation PENDING" in err
    assert "pairing revoke" in err and PRINCIPAL in err
    # Local configuration is kept, because the remote grant is still live.
    assert json.loads(env.hosts_json.read_text())["hosts"]["server"]
    assert env.key.exists()
    assert "--principal laptop-a" in env.remote_keys.read_text()


def test_unpair_local_only_still_reports_pending(env):
    assert _run_pair(_options())[0] == 0
    env.set_mode("deny")

    code, out, err = _run_unpair(_unpair_options(local_only=True))

    assert code == remote_pair_cli.EXIT_REMOTE_PENDING
    assert "remote revocation is still PENDING" in err
    assert "local pairing removed" in out
    assert not env.key.exists()
    assert "--principal laptop-a" in env.remote_keys.read_text()


def test_unpair_keeps_a_key_and_pin_another_alias_shares(env):
    """One key may serve several aliases; unpair must not break the other one."""
    assert _run_pair(_options())[0] == 0
    shared_key = env.key
    shared_pin = env.pin_file.read_text()
    document = json.loads(env.hosts_json.read_text())
    document["hosts"]["second"] = dict(document["hosts"]["server"])
    env.hosts_json.write_text(json.dumps(document), encoding="utf-8")

    code, out, err = _run_unpair(_unpair_options())

    assert code == 0, err
    assert shared_key.is_file(), "the key another alias still uses was deleted"
    assert f"[{HOST}]:{PORT}" in env.pin_file.read_text(), "the shared endpoint pin was removed"
    assert "another host entry uses it" in out
    remaining = json.loads(env.hosts_json.read_text())
    assert list(remaining["hosts"]) == ["second"]
    assert shared_pin.strip() in env.pin_file.read_text()


def test_pair_refuses_to_reuse_an_alias_for_a_different_machine(env):
    assert _run_pair(_options())[0] == 0

    code, _out, err = _run_pair(_options(destination="b@198.51.100.9"))

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "already configured for a different machine" in err


def test_re_pairing_with_a_second_share_adds_it_and_keeps_the_first(env):
    assert _run_pair(_options())[0] == 0
    first = json.loads(env.remote_config.read_text())["principals"][PRINCIPAL]["allowed_aliases"]

    shares = json.loads(json.dumps(SHARES))
    shares["demo2"] = {"alias": "demo2", "work_dir": "/srv/demo2", "providers": ["codex"]}
    code, _out, err = _run_pair(_options(shares=shares))

    assert code == 0, err
    config = json.loads(env.remote_config.read_text())
    assert config["principals"][PRINCIPAL]["allowed_aliases"] == ["demo", "demo2"]
    assert first == ["demo"]
    assert config["aliases"]["demo2"]["work_dir"] == "/srv/demo2"
    assert env.remote_keys.read_text().count("--principal laptop-a") == 1


# --------------------------------------------------------------------------- #
# Values are data
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "overrides",
    [
        {"alias": "../escape"},
        {"alias": "Server"},
        {"principal": "laptop a"},
        {"principal": "$(touch /tmp/pwned)"},
        {"token": "bad token"},
        {"shares": {"demo": {"alias": "demo", "work_dir": "relative", "providers": ["codex"]}}},
    ],
)
def test_pair_refuses_values_that_would_break_a_line_or_a_path(env, overrides):
    code, _out, err = _run_pair(_options(**overrides))

    assert code == remote_pair_cli.EXIT_REFUSED, err
    assert not env.key.exists() and not env.hosts_json.exists()
    assert env.remote_state() == (b"", b"")


def test_metacharacters_in_a_work_dir_are_stored_as_data(env):
    marker = env.tmp / "pwned"
    work_dir = f"/srv/demo $(touch {marker}); `touch {marker}`"

    code, _out, err = _run_pair(
        _options(
            shares={
                "demo": {"alias": "demo", "work_dir": work_dir, "providers": ["codex"]}
            }
        )
    )

    assert code == 0, err
    stored = json.loads(env.remote_config.read_text())["aliases"]["demo"]["work_dir"]
    assert stored == work_dir
    assert not marker.exists()


# --------------------------------------------------------------------------- #
# Print the remote steps instead of connecting
# --------------------------------------------------------------------------- #


def test_print_remote_steps_prints_the_command_and_makes_no_remote_write(env):
    code, out, err = _run_pair(_options(print_remote_steps=True))

    # B7: completion is only claimed after hello + list, which a manual run
    # cannot have done -- so this is a prepared, explicitly incomplete result.
    assert code == remote_pair_cli.EXIT_REMOTE_PENDING, err
    assert "pairing apply" in out and "CCB_PAIRING" in out
    assert f'"principal": "{PRINCIPAL}"' in out
    assert env.verbs() == ["inspect"]  # read-only; the apply is the user's to run
    assert env.remote_state() == (b"", b"")
    # The local half is ready for when the user runs that command.
    assert env.key.is_file() and env.hosts_json.exists()


# --------------------------------------------------------------------------- #
# Direct dispatch of the destination-side verb through the CLI
# --------------------------------------------------------------------------- #


def test_the_pair_command_is_wired_through_the_cli(env):
    """The real `ccb-remote pair` path, not just the module API."""
    completed = subprocess.run(
        [
            sys.executable, str(CCB_REMOTE), "pair", "server",
            "--destination", DESTINATION,
            "--port", str(PORT),
            "--principal", PRINCIPAL,
            "--token", TOKEN,
            "--share", "demo=/srv/demo",
            "--remote-ccb", str(CCB_REMOTE),
            "--print-remote-steps",
            "--yes",
        ],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=300,
    )

    # B7: a manual preparation is explicitly incomplete (remote apply pending).
    assert completed.returncode == remote_pair_cli.EXIT_REMOTE_PENDING, (
        completed.stdout,
        completed.stderr,
    )
    assert "pairing apply" in completed.stdout
    assert env.key.is_file() and env.hosts_json.exists()


def test_the_unpair_command_is_wired_through_the_cli(env):
    assert _run_pair(_options())[0] == 0

    completed = subprocess.run(
        [sys.executable, str(CCB_REMOTE), "unpair", "server", "--yes"],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=300,
    )

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert "unpaired:" in completed.stdout
    assert "--principal laptop-a" not in env.remote_keys.read_text()


def test_the_pairing_verb_prints_one_json_result_and_never_prompts(env):
    completed = subprocess.run(
        [sys.executable, str(CCB_REMOTE), "pairing", "inspect"],
        input="{}",
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(env.remote_home)},
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip())
    assert payload["ok"] is True
    assert payload["gate"].endswith("ccb-remote-gate")


def test_the_pairing_verb_refuses_an_unknown_verb_as_json(env):
    completed = subprocess.run(
        [sys.executable, str(CCB_REMOTE), "pairing", "explode"],
        input="{}",
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(env.remote_home)},
        timeout=120,
    )

    assert completed.returncode != 0
    assert json.loads(completed.stdout.strip())["ok"] is False


def test_hashed_known_hosts_entries_name_the_endpoint():
    import base64
    import hashlib
    import hmac

    salt = b"0123456789abcdefghij"
    name = f"[{HOST}]:{PORT}".encode()
    digest = hmac.new(salt, name, hashlib.sha1).digest()
    pattern = (
        "|1|"
        + base64.b64encode(salt).decode()
        + "|"
        + base64.b64encode(digest).decode()
    )
    text = f"{pattern} ssh-ed25519 {HOST_KEY}\n"

    entries = remote_pairing.known_hosts_entries(text, HOST, PORT)
    assert [(item["type"], item["key"]) for item in entries] == [("ssh-ed25519", HOST_KEY)]
    assert remote_pairing.known_hosts_entries(text, HOST, 22) == []


# --------------------------------------------------------------------------- #
# Amendments B1-B7: trust narrowing, quoting, ordering, shared local files
# --------------------------------------------------------------------------- #


def test_only_the_key_the_users_ssh_accepted_is_pinned(env):
    """A machine reporting extra keys must not get them pinned (B1)."""
    extra_key = "AAAAB3NzaC1yc2EAAAADAQABAAABgQextraextraextraextraextraextra1"
    env.monkeypatch.setenv(
        "CCB_FAKE_PAIR_EXTRA_HOSTKEY", f"ssh-rsa {extra_key}"
    )

    code, _out, err = _run_pair(_options())

    assert code == 0, err
    pin = env.pin_file.read_text()
    assert f"ssh-ed25519 {HOST_KEY}" in pin
    assert extra_key not in pin
    assert len([line for line in pin.splitlines() if line.strip()]) == 1


def test_a_proxy_transport_is_refused_before_anything_is_written(env, monkeypatch):
    import remote_pairing as pairing

    monkeypatch.setattr(
        pairing,
        "_ssh_effective",
        lambda destination, port, **kw: {
            "hostname": HOST,
            "port": str(port),
            "user": "b",
            "proxycommand": "none",
            "proxyjump": "bastion.example.test",
            "controlpath": "none",
            "controlmaster": "false",
        },
    )

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "proxyjump" in err and "Nothing was written" in err
    assert not env.key.exists() and not env.hosts_json.exists()


def test_an_ssh_config_host_rename_is_refused(env, monkeypatch):
    import remote_pairing as pairing

    monkeypatch.setattr(
        pairing,
        "_ssh_effective",
        lambda destination, port, **kw: {
            "hostname": "somewhere-else.example.test",
            "port": str(port),
            "proxycommand": "none",
            "proxyjump": "none",
            "controlpath": "none",
            "controlmaster": "false",
        },
    )

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "use the real host name" in err
    assert not env.hosts_json.exists()


@pytest.mark.parametrize(
    "bad_path",
    [
        "/home/b/bin/ccb-remote --principal evil",
        "/home/b/bin/ccb-remote;sh",
        "/home/b/bin/$(touch /tmp/pwned)",
        "/home/b/bin/ccb-remote\nrm -rf /",
        "~/bin/../../etc/passwd",
        "ccb-remote",
        "-oProxyCommand=evil",
    ],
)
def test_a_remote_command_path_must_be_inert(env, bad_path):
    """ssh joins the remote argv with spaces: the PATH itself must be safe (B2)."""
    code, _out, err = _run_pair(_options(remote_ccb=bad_path))

    assert code == remote_pair_cli.EXIT_REFUSED, err
    assert env.verbs() == [] and not env.key.exists()


def test_the_default_remote_path_is_home_relative_and_resolved_remotely(env):
    """The default is `~/.local/bin/ccb-remote`, sent as `"$HOME"/…` (B2)."""
    assert remote_pairing.remote_command("~/.local/bin/ccb-remote") == (
        '"$HOME"/.local/bin/ccb-remote'
    )
    assert remote_pairing.remote_command("/opt/ccb/bin/ccb-remote") == (
        "/opt/ccb/bin/ccb-remote"
    )
    # Install B's CLI where the default expects it (exactly what install.sh
    # does), then pair with no --remote-ccb at all.
    installed = env.remote_home / ".local" / "bin" / "ccb-remote"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.symlink_to(CCB_REMOTE)
    code, _out, err = _run_pair(_options(remote_ccb=""))
    assert code == 0, err
    # The fake ssh resolved the home prefix against the "remote" home.
    assert any(
        call["argv"][-3] == '"$HOME"/.local/bin/ccb-remote' for call in env.calls()
    )


def test_metadata_is_written_before_b_is_touched(env):
    """B3: the approved identity and scope survive an interrupted apply."""
    env.set_mode("change_before_apply")

    code, _out, _err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    record = json.loads(env.record.read_text())
    assert record["principal"] == PRINCIPAL
    assert record["issuer_token"] == TOKEN
    assert record["pubkey_fingerprint"].startswith("SHA256:")
    assert [share["alias"] for share in record["shares"]] == ["demo"]
    assert record["pin"] and record["port"] == PORT


def test_a_proven_b_refusal_removes_the_key_and_metadata_this_run_created(env, monkeypatch):
    """B4 zero-write rule: nothing new is left behind by a definitive refusal."""
    import remote_pair_cli as cli

    real = cli._Channel.call

    def refusing(self, verb, request):
        if verb == "apply":
            return {"ok": False, "refused": True, "error": "alias demo conflicts"}
        return real(self, verb, request)

    monkeypatch.setattr(cli._Channel, "call", refusing)

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "Nothing was left behind" in err
    assert not env.key.exists() and not (env.key.parent / "server.pub").exists()
    assert not env.record.exists()
    assert not env.hosts_json.exists()


def test_a_lost_apply_answer_keeps_the_local_half_for_the_retry(env):
    """The opposite of a proven refusal: an unknown outcome must stay resumable."""
    env.set_mode("fail_apply_once")

    code, _out, _err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert env.key.exists() and env.record.exists()
    env.clear_mode()
    code, out, err = _run_pair(_options())
    assert code == 0, (out, err)
    assert env.remote_keys.read_text().count(f"--principal {PRINCIPAL}") == 1


def test_yes_never_bypasses_the_state_hash_check(env):
    env.set_mode("change_before_apply")

    code, _out, err = _run_pair(_options(yes=True))

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "changed" in err
    assert "--principal laptop-a" not in env.remote_keys.read_text()


def test_yes_never_bypasses_the_trust_check(env):
    (env.home / ".ssh" / "known_hosts").unlink()

    code, _out, err = _run_pair(_options(yes=True))

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "verified host key" in err
    assert env.verbs() == [] and not env.key.exists()


def test_a_different_existing_pin_needs_explicit_rotation(env):
    other_pin = f"[{HOST}]:{PORT} ssh-ed25519 {OTHER_HOST_KEY}"
    env.pin_file.parent.mkdir(parents=True, exist_ok=True)
    env.pin_file.write_text(other_pin + "\n", encoding="utf-8")

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "already pinned" in err and "--rotate-pin" in err
    assert env.remote_state() == (b"", b"")
    assert env.pin_file.read_text().strip() == other_pin

    code, _out, err = _run_pair(_options(rotate_pin=True))
    assert code == 0, err
    pin = env.pin_file.read_text()
    assert f"ssh-ed25519 {HOST_KEY}" in pin and other_pin not in pin


def test_pin_merge_keeps_another_name_sharing_a_line(env):
    shared = f"other.test,[{HOST}]:{PORT} ssh-ed25519 {HOST_KEY}"
    env.pin_file.parent.mkdir(parents=True, exist_ok=True)
    env.pin_file.write_text(shared + "\n", encoding="utf-8")

    code, _out, err = _run_pair(_options())

    assert code == 0, err
    lines = [line for line in env.pin_file.read_text().splitlines() if line.strip()]
    assert "other.test ssh-ed25519" in " ".join(lines)
    assert f"[{HOST}]:{PORT} ssh-ed25519 {HOST_KEY}" in lines
    assert "other.test,[{0}]".format(f"{HOST}]:{PORT}") not in env.pin_file.read_text()


def test_unpair_removes_only_our_name_from_a_shared_pin_line(env):
    assert _run_pair(_options())[0] == 0
    shared = f"other.test,[{HOST}]:{PORT} ssh-ed25519 {HOST_KEY}"
    env.pin_file.write_text(shared + "\n", encoding="utf-8")

    code, _out, err = _run_unpair(_unpair_options())

    assert code == 0, err
    text = env.pin_file.read_text()
    assert "other.test" in text and f"[{HOST}]:{PORT}" not in text


def test_unpair_inspects_before_revoking(env):
    """B5: revoke is bound to a fresh hash, so unpair must read state first."""
    assert _run_pair(_options())[0] == 0

    code, _out, err = _run_unpair(_unpair_options())

    assert code == 0, err
    verbs = env.verbs()
    assert verbs[-2:] == ["inspect", "revoke"]
    assert "--principal laptop-a" not in env.remote_keys.read_text()


def test_unpair_refuses_when_the_remote_principal_belongs_elsewhere(env):
    """B5: a stale unpair must not delete another pairing's grant."""
    assert _run_pair(_options())[0] == 0
    # Someone re-paired that principal with a different token.
    config = json.loads(env.remote_config.read_text())
    config["principals"][PRINCIPAL]["issuer_token"] = "other9"
    env.remote_config.write_text(json.dumps(config), encoding="utf-8")

    code, _out, err = _run_unpair(_unpair_options())

    assert code == remote_pair_cli.EXIT_REMOTE_PENDING
    assert "PENDING" in err
    assert json.loads(env.remote_config.read_text())["principals"][PRINCIPAL]
    assert f"--principal {PRINCIPAL}" in env.remote_keys.read_text()


def test_a_custom_principal_is_recovered_on_a_retry(env):
    options = _options(principal="laptop-7", token="tok7777")
    env.set_mode("fail_apply_once")
    assert _run_pair(options)[0] == remote_pair_cli.EXIT_REFUSED
    env.clear_mode()

    # No flags a second time: the metadata carries the approved identity.
    code, _out, err = _run_pair(_options(principal="", token=""))

    assert code == 0, err
    record = json.loads(env.record.read_text())
    assert record["principal"] == "laptop-7" and record["issuer_token"] == "tok7777"
    config = json.loads(env.remote_config.read_text())
    assert "laptop-7" in config["principals"]


def test_a_shared_endpoint_unpair_keeps_the_sharing_alias_working(env):
    assert _run_pair(_options())[0] == 0
    document = json.loads(env.hosts_json.read_text())
    document["hosts"]["second"] = dict(document["hosts"]["server"])
    env.hosts_json.write_text(json.dumps(document), encoding="utf-8")

    code, _out, err = _run_unpair(_unpair_options())

    assert code == 0, err
    assert env.pin_file.is_file()
    assert f"[{HOST}]:{PORT}" in env.pin_file.read_text()
    assert "second" in json.loads(env.hosts_json.read_text())["hosts"]


def test_the_restricted_gate_refuses_the_pairing_verbs():
    """Pairing is reachable over the normal login only, never through the gate."""
    import importlib.machinery
    import importlib.util

    gate_path = REPO / "bin" / "ccb-remote-gate"
    loader = importlib.machinery.SourceFileLoader("ccb_gate_pairing_test", str(gate_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)

    for verb in ("pairing", "pair", "unpair"):
        envelope = {
            "proto": {"major": 1, "minor": 1},
            "op": verb,
            "op_id": "probe-1",
            "body": {},
        }
        response = module.dispatch_remote_operation(envelope, principal="alice")
        assert response["ok"] is False
        assert response["error"]["code"] == "unknown_op"


# --------------------------------------------------------------------------- #
# Review fixes (task 20261111-180019, source side)
# --------------------------------------------------------------------------- #


def test_a_supplied_fingerprint_is_checked_even_when_ssh_already_knows_the_host(env):
    """Fix 1: a stale known_hosts entry must never outvote `--fingerprint`."""
    env.trust(HOST_KEY)  # the user's ssh already accepts this endpoint
    unrelated = remote_pairing.fingerprint(OTHER_HOST_KEY)

    code, _out, err = _run_pair(_options(fingerprint=unrelated))

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "does not match the key that machine presented" in err
    assert env.verbs() == []  # refused before any authenticated connection
    assert not env.key.exists() and not env.hosts_json.exists()
    assert env.remote_state() == (b"", b"")


def test_the_fingerprint_and_the_users_known_hosts_must_name_the_same_key(env):
    """Fix 1: the pinned key is one the user's ssh accepts AND the fingerprint names."""
    env.monkeypatch.setenv("CCB_FAKE_PAIR_HOSTKEY", f"ssh-ed25519 {OTHER_HOST_KEY}")

    code, _out, err = _run_pair(_options(fingerprint=remote_pairing.fingerprint(OTHER_HOST_KEY)))

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "your own ssh would not accept" in err
    assert not env.key.exists() and not env.hosts_json.exists()
    assert env.remote_state() == (b"", b"")


def test_several_accepted_host_keys_are_refused_rather_than_all_pinned(env):
    """Fix 1: pin exactly one key; without a fingerprint, ask for it."""
    known = env.home / ".ssh" / "known_hosts"
    known.write_text(
        known.read_text() + f"[{HOST}]:{PORT} ssh-ed25519 {OTHER_HOST_KEY}\n", encoding="utf-8"
    )
    env.monkeypatch.setenv("CCB_FAKE_PAIR_EXTRA_HOSTKEY", f"ssh-ed25519 {OTHER_HOST_KEY}")

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "pins exactly one" in err and "--fingerprint" in err
    assert not env.key.exists() and not env.hosts_json.exists()
    assert env.remote_state() == (b"", b"")


def test_one_accepted_host_key_without_a_fingerprint_is_pinned_alone(env):
    env.monkeypatch.setenv("CCB_FAKE_PAIR_EXTRA_HOSTKEY", f"ssh-ed25519 {OTHER_HOST_KEY}")

    code, _out, err = _run_pair(_options())

    assert code == 0, err
    lines = [line for line in env.pin_file.read_text().splitlines() if line.strip()]
    assert lines == [f"[{HOST}]:{PORT} ssh-ed25519 {HOST_KEY}"]


def test_pair_refuses_before_any_local_write_when_the_key_has_a_foreign_line(env):
    """Fix 2: a second line for one key is a conflict, caught before writes."""
    assert _run_pair(_options())[0] == 0
    blob = (env.key.parent / "server.pub").read_text().split()[1]
    env.remote_keys.write_text(
        env.remote_keys.read_text()
        + f'from="10.0.0.5",restrict,command="/opt/ccb/bin/ccb-remote-gate --principal {PRINCIPAL}" '
        + f"ssh-ed25519 {blob} ccb-remote main\n",
        encoding="utf-8",
    )
    keys_before = env.remote_keys.read_text()
    hosts_before = env.hosts_json.read_text()

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "already appears in an authorized_keys line" in err
    assert env.remote_keys.read_text() == keys_before
    assert env.hosts_json.read_text() == hosts_before


def test_unpair_matches_shared_files_however_they_are_spelled(env):
    """Fix 3: paths compare after expanduser+resolve; the user is not the endpoint."""
    assert _run_pair(_options())[0] == 0
    document = json.loads(env.hosts_json.read_text())
    second = dict(document["hosts"]["server"])
    second["identity_file"] = "~/.ccb/remote/keys/server"
    second["known_hosts"] = "~/.ccb/remote/known_hosts"
    second["ssh_target"] = "other@127.0.0.1"  # same machine, another login
    document["hosts"]["second"] = second
    env.hosts_json.write_text(json.dumps(document), encoding="utf-8")

    code, out, err = _run_unpair(_unpair_options())

    assert code == 0, err
    assert env.key.is_file(), "the key another alias still uses was deleted"
    assert "another host entry uses it" in out
    assert f"[{HOST}]:{PORT} ssh-ed25519 {HOST_KEY}" in env.pin_file.read_text()


def test_unpair_removes_the_pin_even_when_the_key_file_is_shared(env):
    """Fix 3: pin removal does not depend on whether the key is shared."""
    assert _run_pair(_options())[0] == 0
    document = json.loads(env.hosts_json.read_text())
    second = dict(document["hosts"]["server"])
    second["ssh_target"] = "b@198.51.100.9"  # a different machine, same key file
    document["hosts"]["second"] = second
    env.hosts_json.write_text(json.dumps(document), encoding="utf-8")

    code, out, err = _run_unpair(_unpair_options())

    assert code == 0, err
    assert env.key.is_file()
    assert f"[{HOST}]:{PORT}" not in env.pin_file.read_text()
    assert "1 pinned host key line(s)" in out


def test_unpair_takes_the_pairing_lock_for_its_shared_files(env, monkeypatch):
    """Fix 4: one critical section, the same lock `pair` uses."""
    import contextlib

    assert _run_pair(_options())[0] == 0
    entered: list[str] = []
    real = remote_pair_cli.pairing_lock

    @contextlib.contextmanager
    def watching(root, **kwargs):
        entered.append(str(root))
        with real(root, **kwargs):
            yield

    monkeypatch.setattr(remote_pair_cli, "pairing_lock", watching)

    code, _out, err = _run_unpair(_unpair_options())

    assert code == 0, err
    assert entered == [str(remote_pair_cli._remote_root())]


def test_a_retry_with_only_the_alias_resumes_the_approved_pairing(env):
    """Fix 5: `pair <alias>` alone finishes the transaction from the record."""
    env.set_mode("fail_apply_once")
    assert _run_pair(_options(principal="laptop-7", token="tok7777"))[0] == remote_pair_cli.EXIT_REFUSED
    env.clear_mode()

    code, out, err = _run_pair(
        _options(destination="", port=22, principal="", token="", shares={})
    )

    assert code == 0, err
    assert "resuming with the host key approved earlier" in out
    document = json.loads(env.remote_config.read_text())
    assert document["principals"]["laptop-7"]["issuer_token"] == "tok7777"
    assert document["principals"]["laptop-7"]["allowed_aliases"] == ["demo"]
    assert env.remote_keys.read_text().count("--principal laptop-7") == 1
    assert json.loads(env.hosts_json.read_text())["hosts"]["server"]["port"] == PORT


def test_a_retry_with_a_different_value_refuses_instead_of_overriding(env):
    """Fix 5: an explicit flag that differs from the record is a refusal."""
    env.set_mode("fail_apply_once")
    assert _run_pair(_options())[0] == remote_pair_cli.EXIT_REFUSED
    env.clear_mode()
    before = env.remote_state()

    code, _out, err = _run_pair(_options(destination="b@198.51.100.9"))

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "--destination does not match the pairing already in progress" in err
    assert env.remote_state() == before
    # The record survives, so the retry can still resume.
    assert env.record.is_file()


def test_a_retry_with_different_shares_refuses(env):
    env.set_mode("fail_apply_once")
    assert _run_pair(_options())[0] == remote_pair_cli.EXIT_REFUSED
    env.clear_mode()
    other = {"other": {"alias": "other", "work_dir": "/srv/other", "providers": ["codex"]}}

    code, _out, err = _run_pair(_options(shares=other))

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "--share does not match the pairing already in progress" in err


def test_apply_uses_the_state_hash_the_user_approved(env, monkeypatch):
    """Fix 8: no fresh-hash substitution between the diff and apply."""
    calls: list[dict] = []

    def fake_run(_argv, request, **_kwargs):
        calls.append(request)
        if request["verb"] == "inspect":
            return {
                "ok": True,
                "gate": "/opt/ccb/bin/ccb-remote-gate",
                "gate_present": True,
                "state_hash": "hash-approved",
                "projects": [{"work_dir": "/srv/demo", "name": "demo", "providers": ["codex"]}],
                "principals": {},
                "aliases": {},
                "bound_principals": [],
                "key_line_principals": [],
                "key_line_exact_principals": [],
                "key_blob_present": False,
                "key_line_foreign_blob": False,
                "host_keys": [{"type": "ssh-ed25519", "key": HOST_KEY}],
            }
        return {"ok": True, "state_hash": "hash-after"}

    monkeypatch.setattr(remote_pair_cli, "run_pairing", fake_run)

    code, _out, err = _run_pair(_options())

    assert code == 0, err
    assert [call["verb"] for call in calls] == ["inspect", "apply"]
    assert calls[-1]["expected_state_hash"] == "hash-approved"


def test_a_stale_refusal_keeps_the_approved_scope_for_the_retry(env):
    """Fix 6 + 8: \"the machine changed\" refuses, yet the record survives."""
    env.set_mode("change_before_apply")

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "changed since you approved" in err
    record = json.loads(env.record.read_text())
    assert record["principal"] == PRINCIPAL and record["issuer_token"] == TOKEN
    assert record["shares"][0]["alias"] == "demo"
    assert env.key.is_file(), "the key the retry reuses was thrown away"


def test_a_terminal_refusal_restores_an_earlier_runs_record(env):
    """Review2 item 3: only THIS run's record may be discarded."""
    assert _run_pair(_options())[0] == 0
    # That principal now belongs to another pairing on B: a terminal refusal
    # (not "the machine changed"), so this run's changes are rolled back.
    document = json.loads(env.remote_config.read_text())
    document["principals"][PRINCIPAL]["issuer_token"] = "other9"
    env.remote_config.write_text(json.dumps(document), encoding="utf-8")
    keys_before = env.remote_keys.read_text()
    record_before = env.record.read_bytes()

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "left behind" in err
    assert "different issuer token" in err
    # The record belongs to the earlier run: it is restored, never deleted.
    assert env.record.read_bytes() == record_before
    assert env.remote_keys.read_text() == keys_before


def test_a_terminal_refusal_drops_a_record_this_run_created(env):
    """Review2 item 3: a first run's own record and key do go away."""
    (env.remote_home / ".ccb" / "remote").mkdir(parents=True, exist_ok=True)
    env.remote_config.write_text(
        json.dumps(
            {
                "principals": {PRINCIPAL: {"issuer_token": "other9", "allowed_aliases": ["demo"]}},
                "aliases": {"demo": {"work_dir": "/srv/demo", "providers": ["claude", "codex"]}},
            }
        ),
        encoding="utf-8",
    )

    code, _out, err = _run_pair(_options())

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "different issuer token" in err
    assert not env.record.exists(), "this run's record was left behind"
    assert not env.key.exists(), "this run's key was left behind"


# --------------------------------------------------------------------------- #
# Re-review fixes (task 20261111-190934, source side)
# --------------------------------------------------------------------------- #


def test_the_authenticated_inspect_uses_only_the_key_that_was_selected(env):
    """Review2 item 1: K1+K2 accepted, fingerprint names K1, inspect reports K2."""
    known = env.home / ".ssh" / "known_hosts"
    known.write_text(
        f"[{HOST}]:{PORT} ssh-ed25519 {HOST_KEY}\n"
        f"[{HOST}]:{PORT} ssh-ed25519 {OTHER_HOST_KEY}\n",
        encoding="utf-8",
    )
    # The probe presents the key the fingerprint names...
    env.monkeypatch.setenv("CCB_FAKE_PAIR_HOSTKEY", f"ssh-ed25519 {HOST_KEY}")
    # ...while the authenticated connection answers as a machine that only has
    # the OTHER key. Selecting K1 must not be satisfiable by K2.
    env.monkeypatch.setenv("CCB_FAKE_PAIR_REPORTED_HOSTKEY", f"ssh-ed25519 {OTHER_HOST_KEY}")

    code, _out, err = _run_pair(_options(fingerprint=remote_pairing.fingerprint(HOST_KEY)))

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "does not match the key selected for this pairing" in err
    assert not env.key.exists() and not env.hosts_json.exists()
    assert env.remote_state() == (b"", b"")
    # Review3 item 2: the selected temporary pin does not outlive the refusal.
    leftovers = sorted((env.home / ".ccb" / "remote").glob(".pairing-pin.*.tmp"))
    assert leftovers == [], f"temporary pin left behind: {leftovers}"


def test_the_pin_file_is_what_the_inspect_connects_with(env):
    """Review2 item 1: strict against the selected key, never the user's file."""
    calls = []
    real_run = remote_pair_cli.run_pairing

    def watching(argv, request, **kwargs):
        if request.get("verb") == "inspect":
            calls.append(list(argv))
        return real_run(argv, request, **kwargs)

    env.monkeypatch.setattr(remote_pair_cli, "run_pairing", watching)

    code, _out, err = _run_pair(_options())

    assert code == 0, err
    pin_option = [arg for arg in calls[0] if arg.startswith("UserKnownHostsFile=")]
    assert pin_option, "the inspect ran without an explicit pin file"
    pinned = Path(pin_option[0].split("=", 1)[1])
    assert pinned != env.home / ".ssh" / "known_hosts"
    assert '"$HOME"' not in pin_option[0]
    # The file really contains exactly the selected key, and strict checking.
    assert "StrictHostKeyChecking=yes" in calls[0]
    assert not pinned.exists()  # the run's temp pin is cleaned up


def test_resuming_with_a_replaced_key_refuses(env):
    """Review2 item 2: the recorded key is the pairing's identity."""
    env.set_mode("fail_apply_once")
    assert _run_pair(_options())[0] == remote_pair_cli.EXIT_REFUSED
    env.clear_mode()
    record_before = env.record.read_bytes()
    # Same path, different public half (a replaced file).
    (env.key.parent / "server.pub").write_text(
        f"ssh-ed25519 {OTHER_HOST_KEY} ccb-server\n", encoding="utf-8"
    )

    code, _out, err = _run_pair(
        _options(destination="", port=22, principal="", token="", shares={})
    )

    assert code == remote_pair_cli.EXIT_REFUSED
    assert "cannot be resumed under a different key" in err
    assert env.record.read_bytes() == record_before
    assert env.remote_state() == (b"", b"")
