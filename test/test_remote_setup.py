"""Guided remote setup (`ccb-remote setup`, phase 1).

The canary fixtures in `test/fixtures/setup_canary_states.py` are the real
2026-11-17 production failure shapes (sanitized): an empty drop-in that left
password auth on, a ufw default-deny on the SSH port, and a nologin account.
The passing control is the effective answer a hardened host gives.

`fake_setup_ssh.py` stands in for the sender's ssh so the CLI test runs the
REAL wizard path with no network.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "test" / "fixtures"))

import remote_pairing  # noqa: E402
import remote_setup_wizard as wizard  # noqa: E402
from setup_canary_states import (  # noqa: E402
    BLOCKED_REACHABILITY_DETAIL,
    EMPTY_DROPIN_CONFIG,
    EMPTY_DROPIN_EFFECTIVE,
    FIREWALLD_LIST_ALL_NO_SSH,
    HARDENED_EFFECTIVE,
    KBD_INTERACTIVE_STILL_ON,
    LADDR_MATCH_EFFECTIVE,
    LOOPBACK_NOTHING_LISTENING,
    LOOPBACK_SSHD_UP,
    LPORT_MATCH_EFFECTIVE,
    NOLOGIN_SHELL,
    REAL_SHELL,
    UFW_ACTIVE_ALLOW_SSH,
    UFW_ACTIVE_DENY_SSH,
    UFW_STATUS_INACTIVE,
    UFW_STATUS_UNKNOWN,
)

REPO = Path(__file__).resolve().parents[1]
CCB_REMOTE = REPO / "bin" / "ccb-remote"
FAKE_SETUP_SSH = REPO / "test" / "fixtures" / "fake_setup_ssh.py"

HOST = "127.0.0.1"
PORT = 2222
USER = "b"
SENDER = "198.51.100.20"
HOST_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIhosthosthosthosthosthosthosthosthostho1"
LOCAL_ADDR = "192.0.2.5"
HOST_KEY_LINE = f"ssh-ed25519 {HOST_KEY}"
HOST_FINGERPRINT = remote_pairing.fingerprint(HOST_KEY)


def _inputs(lines: list[str]):
    replies = list(lines)

    def input_fn(_prompt: str) -> str:
        return replies.pop(0) if replies else ""

    return input_fn


def _run(options: wizard.SetupOptions, probes: wizard.Probes, answers: list[str] | None = None):
    out, err = io.StringIO(), io.StringIO()
    code = wizard.run_setup(
        options, out=out, err=err, input_fn=_inputs(answers or []), probes=probes
    )
    return code, out.getvalue(), err.getvalue()


def _receiver_block(
    *,
    user: str = USER,
    host: str = SENDER,
    addr: str = SENDER,
    laddr: str = LOCAL_ADDR,
    port: int = PORT,
    output: str = HARDENED_EFFECTIVE,
) -> list[str]:
    return [
        f"connection: user={user} host={host} addr={addr} laddr={laddr} lport={port}"
    ] + output.splitlines() + [""]


def _receiver_block_text(**kwargs) -> str:
    return "\n".join(_receiver_block(**kwargs)) + "\n"


def _receiver_probes(**overrides) -> wizard.Probes:
    probes = wizard.Probes(
        which=lambda name: f"/usr/bin/{name}",
        current_user=lambda: "bob",
        account_shell=lambda account: REAL_SHELL,
        sshd_active=lambda port: True,
        loopback=lambda port: LOOPBACK_SSHD_UP,
        apt_based=lambda: False,
        local_address=lambda: LOCAL_ADDR,
        firewall=lambda port: wizard.parse_ufw_status(UFW_ACTIVE_ALLOW_SSH, port=port),
        host_fingerprint=lambda: "SHA256:receiver-displayed-fingerprint",
        staged_key=lambda account: f"ssh-ed25519 {HOST_KEY} laptop\n",
    )
    for name, value in overrides.items():
        setattr(probes, name, value)
    return probes


def _sender_probes(**overrides) -> wizard.Probes:
    probes = wizard.Probes(
        which=lambda name: f"/usr/bin/{name}",
        reachability=lambda host, port: wizard.Reachability(
            True, "", HOST_KEY_LINE, HOST_FINGERPRINT
        ),
        generate_key=lambda path: True,
        ssh_add=lambda path: True,
        public_key=lambda path: f"ssh-ed25519 {HOST_KEY} setup",
        fresh_login=lambda host, port, user, key, host_key: wizard.LoginResult(
            True,
            "",
            client_addr=SENDER,
            client_port=54321,
            server_addr=LOCAL_ADDR,
            server_port=PORT,
        ),
        password_probe=lambda host, port, user, host_key: wizard.PasswordProbe(("publickey",)),
        run_pair=lambda argv: 0,
    )
    for name, value in overrides.items():
        setattr(probes, name, value)
    return probes


def _sender_options(tmp_path: Path, **overrides) -> wizard.SetupOptions:
    options = wizard.SetupOptions(
        role="send",
        host=HOST,
        port=PORT,
        user=USER,
        sender=SENDER,
        key_path=str(tmp_path / "login-key"),
        fingerprint=HOST_FINGERPRINT,
        alias="server",
        yes=True,
    )
    for name, value in overrides.items():
        setattr(options, name, value)
    return options


def _receiver_options(**overrides) -> wizard.SetupOptions:
    options = wizard.SetupOptions(role="receive", port=22, user="bob", sender=SENDER, yes=True)
    for name, value in overrides.items():
        setattr(options, name, value)
    return options


# --------------------------------------------------------------------------- #
# Pure checks — the canary records and their controls
# --------------------------------------------------------------------------- #


def test_the_canary_files_looked_configured_but_were_not():
    """The recorded shape: an Include, no directives, password auth still on."""
    assert "Include /etc/ssh/sshd_config.d/*.conf" in EMPTY_DROPIN_CONFIG
    assert "PasswordAuthentication" not in EMPTY_DROPIN_CONFIG
    values = wizard.parse_sshd_effective(EMPTY_DROPIN_EFFECTIVE)
    assert values["passwordauthentication"] == "yes"
    assert values["kbdinteractiveauthentication"] == "yes"


def test_empty_drop_in_fails_the_effective_check():
    """Negative control (a): the real empty drop-in left password auth on."""
    check = wizard.check_effective_auth(
        EMPTY_DROPIN_EFFECTIVE,
        user="bob",
        host="laptop",
        addr=SENDER,
        laddr=LOCAL_ADDR,
        port=22,
    )

    assert check.status == wizard.STATUS_FAIL
    assert "passwordauthentication is yes" in check.finding.lower()
    assert (
        "sshd -T -C user=bob,host=laptop,addr="
        + SENDER
        + ",laddr="
        + LOCAL_ADDR
        + ",lport=22"
    ) in check.remediation


def test_hardened_effective_config_passes():
    check = wizard.check_effective_auth(
        HARDENED_EFFECTIVE,
        user="bob",
        host="laptop",
        addr=SENDER,
        laddr=LOCAL_ADDR,
        port=22,
    )

    assert check.status == wizard.STATUS_PASS
    assert "key-only" in check.finding


def test_keyboard_interactive_alone_does_not_pass_hardening():
    """A publickey offer plus passwordauth=no is still not the supported profile."""
    check = wizard.check_effective_auth(
        KBD_INTERACTIVE_STILL_ON,
        user="bob",
        host="laptop",
        addr=SENDER,
        laddr=LOCAL_ADDR,
        port=22,
    )

    assert check.status == wizard.STATUS_FAIL
    assert "kbdinteractiveauthentication is yes" in check.finding.lower()


def test_effective_command_uses_laddr_and_lport_and_parses_match_output():
    command = wizard.effective_config_command(
        user="bob", host="laptop", addr=SENDER, laddr=LOCAL_ADDR, port=2222
    )

    assert f",laddr={LOCAL_ADDR}" in command
    assert ",lport=2222" in command
    assert ",port=2222" not in command
    values = wizard.parse_sshd_effective(LPORT_MATCH_EFFECTIVE)
    assert values["passwordauthentication"] == "no"
    check = wizard.check_effective_auth(
        LPORT_MATCH_EFFECTIVE,
        user="bob",
        host="laptop",
        addr=SENDER,
        laddr=LOCAL_ADDR,
        port=2222,
    )
    assert check.status == wizard.STATUS_PASS


def test_laddr_keyed_match_output_changes_the_effective_check():
    command = wizard.effective_config_command(
        user="bob", host=SENDER, addr=SENDER, laddr=LOCAL_ADDR, port=2222
    )
    assert f"laddr={LOCAL_ADDR}" in command

    check = wizard.check_effective_auth(
        LADDR_MATCH_EFFECTIVE,
        user="bob",
        host=SENDER,
        addr=SENDER,
        laddr=LOCAL_ADDR,
        port=2222,
    )

    assert check.status == wizard.STATUS_FAIL
    assert "passwordauthentication is yes" in check.finding.lower()


def test_numeric_address_accepts_only_ip_literals():
    assert wizard.numeric_address("192.0.2.5") == "192.0.2.5"
    assert wizard.numeric_address("2001:db8::1") == "2001:db8::1"
    assert wizard.numeric_address("fe80::1") == "fe80::1"
    for value in (
        "receiver.example.com",
        "any",
        "*",
        "0.0.0.0/0",
        "user@192.0.2.5",
        "fe80::1%eth0",
    ):
        assert wizard.numeric_address(value) == ""


def test_connection_label_compares_addresses_numerically():
    label = {
        "user": USER,
        "addr": "2001:0db8::20",
        "laddr": "2001:0db8::5",
        "lport": str(PORT),
    }

    check = wizard.check_connection_label(
        label,
        user=USER,
        client_addr="2001:db8::20",
        server_addr="2001:db8::5",
        server_port=PORT,
    )

    assert check.status == wizard.STATUS_PASS


def test_ssh_connection_parses_ipv4_and_ipv6():
    assert wizard.parse_ssh_connection("198.51.100.20 54321 192.0.2.5 2222\n") == (
        "198.51.100.20",
        54321,
        "192.0.2.5",
        2222,
    )
    assert wizard.parse_ssh_connection("2001:db8::20 54321 2001:db8::5 2222") == (
        "2001:db8::20",
        54321,
        "2001:db8::5",
        2222,
    )
    assert wizard.parse_ssh_connection("not four fields") is None


def test_the_effective_check_cannot_pass_without_pasted_output():
    check = wizard.check_effective_auth("", user="bob", host="laptop", addr=SENDER, port=22)

    assert check.status == wizard.STATUS_FAIL
    assert "not `sshd -T` output" in check.finding
    assert "sshd -T -C" in check.remediation


def test_ufw_default_deny_fails_with_a_sender_scoped_tested_command():
    state = wizard.parse_ufw_status(UFW_ACTIVE_DENY_SSH, port=22)
    check = wizard.check_firewall(state, port=22, sender=SENDER)

    assert state.active is True
    assert state.default_incoming == "deny"
    assert check.status == wizard.STATUS_FAIL
    assert f"sudo ufw allow from {SENDER} to any port 22 proto tcp" in check.remediation
    assert "sudo ufw allow 22/tcp" not in check.remediation


def test_ufw_without_a_known_sender_never_prints_a_blanket_allow():
    state = wizard.parse_ufw_status(UFW_ACTIVE_DENY_SSH, port=22)
    check = wizard.check_firewall(state, port=22)

    assert check.status == wizard.STATUS_FAIL
    assert "sudo ufw allow 22/tcp" not in check.remediation
    assert "only the sending machine's address" in check.remediation


def test_ufw_rejects_a_wildcard_or_user_sender_in_the_scoped_rule():
    state = wizard.parse_ufw_status(UFW_ACTIVE_DENY_SSH, port=22)

    for bad in (
        "*",
        "any",
        "0.0.0.0/0",
        "receiver.example.com",
        "user@host",
        "host name",
        "fe80::1%eth0",
    ):
        check = wizard.check_firewall(state, port=22, sender=bad)
        assert check.status == wizard.STATUS_FAIL
        assert "sudo ufw allow" not in check.remediation


def test_ufw_scoped_rule_accepts_a_plain_ipv6_address():
    state = wizard.parse_ufw_status(UFW_ACTIVE_DENY_SSH, port=22)
    check = wizard.check_firewall(state, port=22, sender="fe80::1")

    assert check.status == wizard.STATUS_FAIL
    assert "sudo ufw allow from fe80::1 to any port 22 proto tcp" in check.remediation


def test_ufw_inactive_status_is_not_read_as_active():
    state = wizard.parse_ufw_status(UFW_STATUS_INACTIVE, port=22)

    assert state.status_known is True
    assert state.active is False
    check = wizard.check_firewall(state, port=22)
    assert check.status == wizard.STATUS_PASS
    assert "not active" in check.finding


def test_ufw_unknown_status_fails_closed():
    state = wizard.parse_ufw_status(UFW_STATUS_UNKNOWN, port=22)

    assert state.status_known is False
    check = wizard.check_firewall(state, port=22, sender=SENDER)
    assert check.status == wizard.STATUS_FAIL
    assert "sudo ufw status" in check.remediation


def test_ufw_allow_rule_passes():
    state = wizard.parse_ufw_status(UFW_ACTIVE_ALLOW_SSH, port=22)
    check = wizard.check_firewall(state, port=22)

    assert state.port_allowed is True
    assert check.status == wizard.STATUS_PASS


def test_no_firewall_passes_but_names_the_cross_machine_authority():
    check = wizard.check_firewall(wizard.FirewallState(kind="none"), port=22)

    assert check.status == wizard.STATUS_PASS
    assert "cross-machine test" in check.finding


def test_firewalld_failure_never_prints_a_guessed_command():
    state = wizard.parse_firewalld_list_all(FIREWALLD_LIST_ALL_NO_SSH, port=22)
    check = wizard.check_firewall(state, port=22)

    assert check.status == wizard.STATUS_FAIL
    assert "not been tested" in check.remediation
    assert "sudo ufw" not in check.remediation
    assert "sudo firewall-cmd" not in check.remediation
    assert "Troubleshooting" in check.remediation


def test_the_canary_blocked_ssh_port_fails_the_sender_reachability_check():
    """Negative control (b): the canary's ufw default-deny must stop the sender."""
    state = wizard.parse_ufw_status(UFW_ACTIVE_DENY_SSH, port=22)
    assert state.active is True and state.port_allowed is False

    check = wizard.check_sender_reachability(
        wizard.Reachability(False, BLOCKED_REACHABILITY_DETAIL), host=HOST, port=22
    )

    assert check.status == wizard.STATUS_FAIL
    assert "Cannot reach" in check.finding
    assert "Connection timed out" in check.finding


def test_a_reachable_endpoint_passes_the_sender_reachability_check():
    check = wizard.check_sender_reachability(wizard.Reachability(True), host=HOST, port=22)

    assert check.status == wizard.STATUS_PASS


def test_nologin_account_fails_the_receiver_shell_check():
    """Negative control (c): a nologin shell cannot start the gate."""
    check = wizard.check_login_shell(NOLOGIN_SHELL, account="bob")

    assert check.status == wizard.STATUS_FAIL
    assert NOLOGIN_SHELL in check.finding
    assert "local console" in check.remediation


def test_real_shell_passes():
    check = wizard.check_login_shell(REAL_SHELL, account="bob")

    assert check.status == wizard.STATUS_PASS


def test_missing_ccb_command_fails():
    check = wizard.check_ccb_present("/usr/bin/ccb-remote", None)

    assert check.status == wizard.STATUS_FAIL
    assert "ccb-remote-gate" in check.finding


def test_fingerprint_must_match_before_any_login():
    mismatch = wizard.check_fingerprint("SHA256:displayed", "SHA256:presented")

    assert mismatch.status == wizard.STATUS_FAIL
    assert "STOP" in mismatch.remediation
    assert wizard.check_fingerprint("SHA256:same", "same").status == wizard.STATUS_PASS
    # The receiver displays the whole `ssh-keygen -lf` line; the token is what
    # the sender compares.
    displayed = "256 SHA256:same root@receiver (ED25519)"
    assert wizard.check_fingerprint(displayed, "SHA256:same").status == wizard.STATUS_PASS
    assert wizard.check_fingerprint("", "SHA256:presented").status == wizard.STATUS_FAIL


def test_staged_key_check_ignores_the_restricted_ccb_gate_line():
    gate_only = (
        'restrict,command="/usr/bin/ccb-remote-gate --principal laptop-a" '
        f"ssh-ed25519 {HOST_KEY}\n"
    )

    assert wizard.check_staged_key(gate_only, account="bob").status == wizard.STATUS_FAIL
    assert (
        wizard.check_staged_key(f"ssh-ed25519 {HOST_KEY} laptop\n", account="bob").status
        == wizard.STATUS_PASS
    )


def test_handoff_command_passes_the_verified_endpoint_fingerprint_and_identity(tmp_path):
    command = wizard.pair_handoff_command(
        alias="server",
        user=USER,
        host=HOST,
        port=PORT,
        fingerprint=HOST_FINGERPRINT,
        key_path=tmp_path / "login-key",
    )

    assert command.startswith("ccb-remote pair server ")
    assert f"--destination {USER}@{HOST}" in command
    assert f"--port {PORT}" in command
    assert f"--fingerprint {HOST_FINGERPRINT}" in command
    assert f"--pairing-identity {tmp_path / 'login-key'}" in command
    assert "--known-hosts" not in command


# --------------------------------------------------------------------------- #
# Receiver flow
# --------------------------------------------------------------------------- #


def test_receiver_flow_passes_with_a_hardened_effective_answer():
    probes = _receiver_probes()
    answers = HARDENED_EFFECTIVE.splitlines() + [""]

    code, out, err = _run(_receiver_options(), probes, answers)

    assert code == 0, err
    assert "receiver-displayed-fingerprint" in out
    assert "Continue on the sending machine" in out
    assert "Harden sshd" not in out


def test_receiver_prints_a_labelled_copy_block_for_the_checked_connection():
    probes = _receiver_probes()
    answers = HARDENED_EFFECTIVE.splitlines() + [""]

    code, out, err = _run(_receiver_options(), probes, answers)

    assert code == 0, err
    assert "Copy this WHOLE block" in out
    assert (
        f"connection: user=bob host={SENDER} addr={SENDER} "
        f"laddr={LOCAL_ADDR} lport=22"
    ) in out
    assert "passwordauthentication no" in out


def test_receiver_rejects_a_hostname_laddr():
    probes = _receiver_probes(local_address=lambda: "receiver.example.com")

    code, out, _err = _run(_receiver_options(), probes, [])

    assert code == wizard.EXIT_ERROR
    assert "numeric" in out.lower()
    assert "hostname" in out.lower()


def test_receiver_rejects_a_scoped_ipv6_laddr():
    probes = _receiver_probes(local_address=lambda: "fe80::1%eth0")

    code, out, _err = _run(_receiver_options(), probes, [])

    assert code == wizard.EXIT_ERROR
    assert "numeric" in out.lower()


def test_receiver_empty_drop_in_is_shown_hardening_and_still_fails():
    """The canary's empty drop-in must stop before any pairing handoff."""
    probes = _receiver_probes()
    answers = (EMPTY_DROPIN_EFFECTIVE.splitlines() + [""]) * 2

    code, out, _err = _run(_receiver_options(), probes, answers)

    assert code == wizard.EXIT_ERROR
    assert "passwordauthentication is yes" in out.lower()
    assert "Harden sshd" in out
    assert "ccb-remote pair" not in out


def test_receiver_nologin_stops_before_showing_anything_else():
    probes = _receiver_probes(account_shell=lambda account: NOLOGIN_SHELL)

    code, out, _err = _run(_receiver_options(), probes, [])

    assert code == wizard.EXIT_ERROR
    assert NOLOGIN_SHELL in out
    assert "fingerprint" not in out.lower()


def test_receiver_sshd_not_listening_stops_before_the_effective_paste():
    probes = _receiver_probes(
        sshd_active=lambda port: None,
        loopback=lambda port: LOOPBACK_NOTHING_LISTENING,
    )

    code, out, _err = _run(_receiver_options(), probes, [])

    assert code == wizard.EXIT_ERROR
    assert "127.0.0.1:22" in out
    assert "fingerprint" not in out.lower()


def test_receiver_blocked_local_firewall_stops_before_the_fingerprint():
    probes = _receiver_probes(
        firewall=lambda port: wizard.parse_ufw_status(UFW_ACTIVE_DENY_SSH, port=port)
    )
    answers = HARDENED_EFFECTIVE.splitlines() + [""]

    code, out, _err = _run(_receiver_options(), probes, answers)

    assert code == wizard.EXIT_ERROR
    assert f"sudo ufw allow from {SENDER} to any port 22 proto tcp" in out
    assert "sudo ufw allow 22/tcp" not in out
    assert "fingerprint" not in out.lower()


def test_receiver_missing_sshd_on_apt_prints_the_install_step():
    check = wizard.check_sshd_local(
        service_active=False, loopback_ok=False, port=22, apt_based=True
    )

    assert check.status == wizard.STATUS_FAIL
    assert "sudo apt-get install -y openssh-server" in check.remediation
    assert "sudo systemctl enable --now ssh" in check.remediation
    assert "Debian/Ubuntu" in check.remediation


def test_missing_sshd_on_other_systems_gets_no_guessed_command():
    check = wizard.check_sshd_local(
        service_active=False, loopback_ok=False, port=22, apt_based=False
    )

    assert check.status == wizard.STATUS_FAIL
    assert "apt-get" not in check.remediation
    assert "Troubleshooting" in check.remediation


def test_receiver_pastes_ufw_status_when_it_needs_root():
    probes = _receiver_probes(
        firewall=lambda port: wizard.FirewallState(kind="ufw", active=True, port_allowed=None)
    )
    answers = (
        HARDENED_EFFECTIVE.splitlines()
        + [""]
        + UFW_ACTIVE_ALLOW_SSH.splitlines()
        + ["END"]
    )

    code, out, err = _run(_receiver_options(), probes, answers)

    assert code == 0, err
    assert "sudo ufw status" in out
    assert "ufw is active and allows TCP port 22" in out


def test_receiver_pasted_ufw_deny_prints_a_sender_scoped_rule():
    probes = _receiver_probes(
        firewall=lambda port: wizard.FirewallState(kind="ufw", active=True, port_allowed=None)
    )
    answers = (
        HARDENED_EFFECTIVE.splitlines()
        + [""]
        + UFW_ACTIVE_DENY_SSH.splitlines()
        + ["END"]
    )

    code, out, _err = _run(_receiver_options(), probes, answers)

    assert code == wizard.EXIT_ERROR
    assert f"sudo ufw allow from {SENDER} to any port 22 proto tcp" in out
    assert "sudo ufw allow 22/tcp" not in out


# --------------------------------------------------------------------------- #
# Sender flow
# --------------------------------------------------------------------------- #


def test_sender_passes_the_full_destination_to_the_endpoint_check(tmp_path):
    seen: list[tuple[str, int]] = []
    probes = _sender_probes(
        verified_endpoint=lambda destination, port: seen.append((destination, port))
        or (HOST, port)
    )

    code, _out, err = _run(
        _sender_options(tmp_path), probes, _receiver_block()
    )

    assert code == 0, err
    assert seen == [(f"{USER}@{HOST}", PORT)]


def test_sender_passes_the_full_destination_to_the_reachability_probe(tmp_path):
    seen: list[tuple[str, int]] = []
    probes = _sender_probes(
        reachability=lambda destination, port: seen.append((destination, port))
        or wizard.Reachability(True, "", HOST_KEY_LINE, HOST_FINGERPRINT)
    )

    code, _out, err = _run(
        _sender_options(tmp_path), probes, _receiver_block()
    )

    assert code == 0, err
    assert seen == [(f"{USER}@{HOST}", PORT)]


def test_sender_flow_ends_with_the_pair_command_using_the_verified_fingerprint(tmp_path):
    probes = _sender_probes()

    code, out, err = _run(
        _sender_options(tmp_path), probes, _receiver_block()
    )

    assert code == 0, err
    assert f"ccb-remote pair server --destination {USER}@{HOST} --port {PORT}" in out
    assert f"--fingerprint {HOST_FINGERPRINT}" in out
    assert f"--pairing-identity {tmp_path / 'login-key'}" in out
    assert "--known-hosts" not in out
    assert "never replaces it" in out


def test_sender_rejects_a_paste_from_a_different_account_or_port(tmp_path):
    probes = _sender_probes()

    for block in (
        _receiver_block(user="alice"),
        _receiver_block(port=PORT + 1),
        _receiver_block(addr="203.0.113.99"),
    ):
        code, out, _err = _run(_sender_options(tmp_path), probes, block)
        assert code == wizard.EXIT_ERROR
        assert "different connection" in out
        assert "block" in out and "observed" in out
        assert "ccb-remote pair" not in out


def test_sender_rejects_a_block_for_a_different_observed_listener(tmp_path):
    probes = _sender_probes()

    for block in (
        _receiver_block(laddr="192.0.2.99"),
        _receiver_block(port=PORT + 1),
    ):
        code, out, _err = _run(_sender_options(tmp_path), probes, block)
        assert code == wizard.EXIT_ERROR
        assert "different connection" in out
        assert "observed" in out
        assert "ccb-remote pair" not in out


def test_sender_fails_closed_when_the_login_reports_no_ssh_connection(tmp_path):
    probes = _sender_probes(
        fresh_login=lambda host, port, user, key, host_key: wizard.LoginResult(
            True, "login succeeded but no SSH_CONNECTION"
        )
    )

    code, out, _err = _run(_sender_options(tmp_path), probes, _receiver_block())

    assert code == wizard.EXIT_ERROR
    assert "SSH_CONNECTION" in out
    assert "ccb-remote pair" not in out


def test_sender_rejects_a_paste_without_the_connection_line(tmp_path):
    probes = _sender_probes()

    code, out, _err = _run(
        _sender_options(tmp_path), probes, HARDENED_EFFECTIVE.splitlines() + [""]
    )

    assert code == wizard.EXIT_ERROR
    assert "connection:" in out
    assert "whole copy block" in out
    assert "ccb-remote pair" not in out


def test_sender_endpoint_refusal_makes_no_reachability_probe(tmp_path):
    probed: list[tuple[str, int]] = []

    def refuse_endpoint(destination, port):
        raise remote_pairing.PairingError(
            "your ssh configuration sets proxyjump=bastion.example.com"
        )

    probes = _sender_probes(
        verified_endpoint=refuse_endpoint,
        reachability=lambda destination, port: probed.append((destination, port))
        or wizard.Reachability(True, "", HOST_KEY_LINE, HOST_FINGERPRINT),
    )

    code, out, _err = _run(_sender_options(tmp_path), probes)

    assert code == wizard.EXIT_ERROR
    assert "proxyjump=bastion.example.com" in out
    assert "direct ssh connection" in out
    assert probed == []


def test_sender_refuses_a_fingerprint_mismatch_before_writing_a_key(tmp_path):
    generated: list[Path] = []
    probes = _sender_probes(
        reachability=lambda host, port: wizard.Reachability(
            True, "", HOST_KEY_LINE, "SHA256:not-the-displayed-one"
        ),
        generate_key=lambda path: generated.append(path) or True,
    )

    code, out, _err = _run(
        _sender_options(tmp_path), probes, answers=[]
    )

    assert code == wizard.EXIT_ERROR
    assert "does not match" in out
    assert generated == []


def test_sender_stops_before_key_selection_when_the_ssh_config_uses_a_proxyjump(
    tmp_path, monkeypatch
):
    """`pair` would refuse this endpoint; the wizard must not say it is ready."""
    generated: list[Path] = []
    monkeypatch.setenv("CCB_SETUP_SSH_BIN", str(FAKE_SETUP_SSH))
    monkeypatch.setenv("CCB_FAKE_SETUP_PROXYJUMP", "bastion.example.com")
    probes = _sender_probes(
        generate_key=lambda path: generated.append(path) or True,
    )

    code, out, _err = _run(_sender_options(tmp_path), probes)

    assert code == wizard.EXIT_ERROR
    assert "proxyjump=bastion.example.com" in out
    assert "direct ssh connection" in out
    assert "fingerprint matches" not in out
    assert "ccb-remote pair" not in out
    assert generated == []


def test_sender_blocked_reachability_stops_before_key_selection(tmp_path):
    generated: list[Path] = []
    probes = _sender_probes(
        reachability=lambda host, port: wizard.Reachability(
            False, BLOCKED_REACHABILITY_DETAIL
        ),
        generate_key=lambda path: generated.append(path) or True,
    )

    code, out, _err = _run(_sender_options(tmp_path), probes)

    assert code == wizard.EXIT_ERROR
    assert "Cannot reach" in out
    assert generated == []


def test_sender_confirmation_names_key_account_and_persistence_and_precedes_writes(tmp_path):
    events: list[str] = []
    key = tmp_path / "login-key"

    def generate(path: Path) -> bool:
        events.append("generate")
        path.write_text("private", encoding="utf-8")
        Path(str(path) + ".pub").write_text(f"ssh-ed25519 {HOST_KEY} setup\n", encoding="utf-8")
        return True

    probes = _sender_probes(
        generate_key=generate,
        ssh_add=lambda path: events.append("ssh-add") or True,
        public_key=lambda path: f"ssh-ed25519 {HOST_KEY} setup",
    )
    options = _sender_options(tmp_path, yes=False)
    answers = ["yes", ""] + _receiver_block()

    code, out, err = _run(options, probes, answers)

    assert code == 0, err
    assert f"login key {key}" in out
    assert f"{USER}@{HOST}" in out
    assert "persistent SSH login" in out
    # The typed confirmation came before every user-level write.
    assert events and events[0] == "generate"
    assert "ssh-add" in events


def test_sender_declining_the_login_confirmation_writes_nothing(tmp_path):
    events: list[str] = []
    probes = _sender_probes(
        generate_key=lambda path: events.append("generate") or True,
        ssh_add=lambda path: events.append("ssh-add") or True,
    )

    code, out, _err = _run(_sender_options(tmp_path, yes=False), probes, ["no"])

    assert code == wizard.EXIT_ERROR
    assert "nothing was written" in out.lower()
    assert events == []


def test_password_methods_are_parsed_from_ssh_stderr():
    assert wizard.parse_advertised_methods("b@host: Permission denied (publickey).") == (
        "publickey",
    )
    assert wizard.parse_advertised_methods(
        "b@host: Permission denied (publickey,password,keyboard-interactive)."
    ) == ("publickey", "password", "keyboard-interactive")


def test_sender_stops_before_handoff_when_password_is_still_advertised(tmp_path):
    probes = _sender_probes(
        password_probe=lambda host, port, user, host_key: wizard.PasswordProbe(
            ("publickey", "password")
        )
    )

    code, out, _err = _run(_sender_options(tmp_path), probes)

    assert code == wizard.EXIT_ERROR
    assert "still allows password" in out
    assert "ccb-remote pair" not in out


def test_sender_hands_off_when_only_publickey_is_advertised(tmp_path):
    probes = _sender_probes(
        password_probe=lambda host, port, user, host_key: wizard.PasswordProbe(("publickey",))
    )

    code, out, err = _run(
        _sender_options(tmp_path), probes, _receiver_block()
    )

    assert code == 0, err
    assert "ccb-remote pair server" in out


def test_sender_stops_before_handoff_when_the_receiver_paste_allows_password(tmp_path):
    probes = _sender_probes()

    code, out, _err = _run(
        _sender_options(tmp_path), probes, _receiver_block(output=EMPTY_DROPIN_EFFECTIVE)
    )

    assert code == wizard.EXIT_ERROR
    assert "passwordauthentication is yes" in out.lower()
    assert "ccb-remote pair" not in out


def test_sender_fresh_login_failure_stops_after_the_attempt(tmp_path):
    probes = _sender_probes(
        fresh_login=lambda host, port, user, key, host_key: wizard.LoginResult(
            False, "Permission denied (publickey)."
        ),
    )

    code, out, _err = _run(_sender_options(tmp_path), probes)

    assert code == wizard.EXIT_ERROR
    assert "fresh key-only login" in out
    assert "ccb-remote pair" not in out


# --------------------------------------------------------------------------- #
# CLI wiring — the real `ccb-remote setup` path
# --------------------------------------------------------------------------- #


def _cli_env(tmp_path: Path, **knobs: str) -> dict[str, str]:
    env = {
        **os.environ,
        "PATH": f"{REPO / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        "CCB_SETUP_SSH_BIN": str(FAKE_SETUP_SSH),
        "CCB_FAKE_SETUP_LOG": str(tmp_path / "setup-ssh.jsonl"),
    }
    env.update(knobs)
    return env


def test_the_setup_command_is_wired_through_the_cli_and_stops_on_an_unreachable_host(
    tmp_path,
):
    completed = subprocess.run(
        [
            sys.executable, str(CCB_REMOTE), "setup",
            "--role", "send",
            "--host", HOST,
            "--port", str(PORT),
            "--user", USER,
            "--sender", SENDER,
            "--yes",
        ],
        capture_output=True,
        text=True,
        env=_cli_env(tmp_path, CCB_FAKE_SETUP_MODE="unreachable"),
        timeout=120,
    )

    assert completed.returncode == wizard.EXIT_ERROR, (completed.stdout, completed.stderr)
    assert "Cannot reach" in completed.stdout
    assert "Unknown ccb-remote command" not in completed.stderr


def test_the_setup_command_runs_the_sender_flow_and_prints_the_handoff(tmp_path):
    key = tmp_path / "login-key"
    key.write_text("private", encoding="utf-8")
    Path(str(key) + ".pub").write_text(f"ssh-ed25519 {HOST_KEY} setup\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable, str(CCB_REMOTE), "setup",
            "--role", "send",
            "--host", HOST,
            "--port", str(PORT),
            "--user", USER,
            "--sender", SENDER,
            "--key", str(key),
            "--fingerprint", HOST_FINGERPRINT,
            "--alias", "server",
            "--yes",
        ],
        input=_receiver_block_text(),
        capture_output=True,
        text=True,
        env=_cli_env(
            tmp_path,
            CCB_FAKE_SETUP_MODE="ok",
            CCB_FAKE_SETUP_HOSTKEY=HOST_KEY_LINE,
        ),
        timeout=120,
    )

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert "ccb-remote pair server" in completed.stdout
    assert f"--fingerprint {HOST_FINGERPRINT}" in completed.stdout
    assert "--pairing-identity" in completed.stdout
    assert "--known-hosts" not in completed.stdout
    # The wizard no longer leaves a persistent pin behind: `pair` owns pinning.
    stale_pin = Path(os.environ["HOME"]) / ".ccb" / "remote" / "setup" / "known_hosts"
    assert not stale_pin.exists()
    # Both authenticated probes isolate the key from the user's ssh config.
    log = [
        json.loads(line)
        for line in (tmp_path / "setup-ssh.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kinds = {entry["kind"] for entry in log}
    assert {"login", "password"} <= kinds
    for entry in log:
        if entry["kind"] in {"login", "password"}:
            assert "-F" in entry["argv"]
            assert entry["argv"][entry["argv"].index("-F") + 1] == os.devnull
