"""Guided cross-machine setup (`ccb-remote setup`, phase 1).

This wizard owns the PREREQUISITES only. Project sharing, host-key pinning
verification and the pairing record stay in `ccb-remote pair`, which the
wizard ends by printing (or running, after one confirmation).

Two rules shape every step:

* **The wizard never runs sudo and never writes the receiving machine's sshd
  configuration or `authorized_keys`.** System changes are printed as exact
  commands for the user to run at that machine's local console; the wizard
  then verifies the result. For authentication the verification is the
  EFFECTIVE `sshd -T -C ...` answer the operator pastes back -- a "done"
  prompt alone can never pass it.
* **The only writes happen on the SENDER, at user level, after one
  confirmation that names the key, the destination account and whether the
  login grant persists:** the login key (when it does not exist) and
  `ssh-add`. Staging that key in the receiver account's `authorized_keys` is
  done BY THE USER at the receiver's local console. The fresh-login check
  verifies the receiver's host key against the fingerprint the receiver
  displayed, using an ephemeral known_hosts file; no persistent pin is
  written here.

Check functions are pure: they take observed values and return a `Check`
(status, plain-language finding, remediation command or None). Every real
probe lives in `Probes` and can be replaced in tests, so no test needs a
network or a real sshd.
"""

from __future__ import annotations

import getpass
import ipaddress
import os
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Sequence

import remote_client
import remote_pairing
from cli_output import EXIT_ERROR, EXIT_OK

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"

ROLE_RECEIVE = "receive"
ROLE_SEND = "send"

PROMPT = "> "
GUIDE = "guides/cross-machine.md"
GUIDE_TROUBLESHOOTING = f"Troubleshooting in {GUIDE}"

#: Mirrors the existing test/build knob for the pairing channel: the ssh the
#: wizard uses for its unauthenticated probe and its fresh login.
SETUP_SSH_ENV = "CCB_SETUP_SSH_BIN"

KEY_TYPE_PREFIXES = (
    "ssh-ed25519 ",
    "ssh-rsa ",
    "ssh-dss ",
    "ecdsa-sha2-",
    "sk-ssh-ed25519@",
    "sk-ecdsa-sha2-",
)


class Check(NamedTuple):
    """One plain-language finding plus the command that fixes it, if any."""

    status: str
    finding: str
    remediation: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.status == STATUS_PASS

    @property
    def failed(self) -> bool:
        return self.status == STATUS_FAIL


@dataclass(frozen=True)
class FirewallState:
    """What the local firewall probe could see, without guessing."""

    kind: str = "none"
    active: bool = False
    port_allowed: Optional[bool] = None
    default_incoming: str = ""
    status_known: bool = True


@dataclass(frozen=True)
class Reachability:
    """The sender's real TCP+SSH view of the receiver."""

    reachable: bool
    detail: str = ""
    host_key: str = ""
    fingerprint: str = ""


@dataclass(frozen=True)
class LoginResult:
    """One fresh, key-only login, with the connection sshd observed."""

    ok: bool
    detail: str = ""
    client_addr: str = ""
    client_port: int = 0
    server_addr: str = ""
    server_port: int = 0


@dataclass(frozen=True)
class PasswordProbe:
    """The authentication methods a password-only attempt was offered."""

    methods: tuple[str, ...] = ()
    detail: str = ""


@dataclass
class SetupOptions:
    role: str = ""
    host: str = ""
    port: int = 22
    user: str = ""
    sender: str = ""
    key_path: str = ""
    fingerprint: str = ""
    alias: str = ""
    yes: bool = False


# --------------------------------------------------------------------------- #
# Pure checks
# --------------------------------------------------------------------------- #


def check_ccb_present(ccb_remote: Optional[str], gate: Optional[str]) -> Check:
    missing = []
    if not ccb_remote:
        missing.append("ccb-remote")
    if not gate:
        missing.append("ccb-remote-gate")
    if missing:
        return Check(
            STATUS_FAIL,
            "CCB is not on PATH on this machine (missing: " + ", ".join(missing) + ").",
            "Install CCB here (a release that ships both cross-machine commands), then run "
            "`ccb-remote setup` again.",
        )
    return Check(STATUS_PASS, "CCB is installed here (ccb-remote and ccb-remote-gate found).")


def check_login_shell(shell: str, *, account: str = "the destination account") -> Check:
    value = (shell or "").strip()
    if not value:
        return Check(
            STATUS_FAIL,
            f"No login shell is recorded for {account}.",
            "At the receiving machine's local console, give that account a real shell (for "
            f"example /bin/bash), then run the wizard again. See 'Prerequisites' in {GUIDE}.",
        )
    name = value.rsplit("/", 1)[-1].lower()
    if name in ("nologin", "false"):
        return Check(
            STATUS_FAIL,
            f"{account} has the shell {value}, so sshd cannot start CCB for it.",
            "At the receiving machine's local console, change that account's shell to a real "
            f"one (for example /bin/bash), then run the wizard again. See 'Prerequisites' in {GUIDE}.",
        )
    return Check(STATUS_PASS, f"{account} has a real login shell ({value}).")


def _sshd_install_remediation(*, apt_based: bool) -> str:
    if apt_based:
        return (
            "At this machine's local console, install and start the SSH server "
            "(the Debian/Ubuntu command):\n"
            "sudo apt-get install -y openssh-server\n"
            "sudo systemctl enable --now ssh\n"
            "Then run the wizard again."
        )
    return (
        "At this machine's local console, install/start the SSH server (the service is "
        "`ssh` on Debian/Ubuntu and `sshd` on Fedora/RHEL), then run the wizard again. "
        f"See {GUIDE_TROUBLESHOOTING}."
    )


def check_sshd_local(
    *,
    service_active: Optional[bool],
    loopback_ok: Optional[bool],
    port: int,
    apt_based: bool = False,
) -> Check:
    if loopback_ok is True:
        # A socket-activated sshd can report "inactive" while serving; the
        # local connection is the authority.
        return Check(STATUS_PASS, f"sshd is running and answers on 127.0.0.1:{port}.")
    if service_active is not True:
        return Check(
            STATUS_FAIL,
            f"No SSH server answered on 127.0.0.1:{port}"
            + (" and the service is not active." if service_active is False else "."),
            _sshd_install_remediation(apt_based=apt_based),
        )
    return Check(
        STATUS_FAIL,
        f"sshd is active but nothing accepted a connection to 127.0.0.1:{port}.",
        "At this machine's local console, check that the SSH server listens on that port "
        f"(its `Port` directive), then run the wizard again. See {GUIDE_TROUBLESHOOTING}.",
    )


def parse_sshd_effective(text: str) -> dict[str, str]:
    """`sshd -T` output as lower-case key -> value (last answer wins)."""
    values: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        if key:
            values[key.strip().lower()] = value.strip()
    return values


def effective_config_command(*, user: str, host: str, addr: str, laddr: str, port: int) -> str:
    """The read-only command the operator runs and pastes back.

    `-C` is what makes Match blocks expand for the ACTUAL connection (user,
    source host/address and local address/port), so an empty drop-in behind an
    `Include` cannot look configured.
    """
    return (
        f"sudo sshd -T -C user={user},host={host},addr={addr},"
        f"laddr={laddr},lport={port}"
    )


def _effective_rerun_text(
    *, user: str, host: str, addr: str, laddr: str, port: int
) -> str:
    if laddr.strip():
        command = effective_config_command(
            user=user, host=host, addr=addr, laddr=laddr, port=port
        )
        return f"At the receiving machine's local console run:\n{command}\nand paste the whole output back."
    return (
        "Re-run the receiving machine's `sshd -T -C` effective-config check and paste the "
        "whole output back."
    )


def check_effective_auth(
    text: str,
    *,
    user: str,
    host: str,
    addr: str,
    port: int,
    laddr: str = "",
) -> Check:
    """Key-only login for THIS connection, from the effective configuration.

    The supported profile is `PasswordAuthentication no` AND
    `KbdInteractiveAuthentication no`. A key offer alone only describes the
    first authentication step; it never passes this check.
    """
    values = parse_sshd_effective(text)
    rerun = _effective_rerun_text(
        user=user, host=host, addr=addr, laddr=laddr, port=port
    )
    if not values:
        return Check(
            STATUS_FAIL,
            "That was not `sshd -T` output (no directives could be read), so key-only login is unproven.",
            rerun,
        )
    problems = []
    for directive in ("passwordauthentication", "kbdinteractiveauthentication"):
        value = values.get(directive)
        if value is None:
            problems.append(f"{directive} is not reported")
        elif value.strip().lower() != "no":
            problems.append(f"{directive} is {value}")
    if problems:
        return Check(
            STATUS_FAIL,
            "Key-only login is NOT in force for this connection ("
            + "; ".join(problems)
            + "). A key offer alone does not disable password or keyboard-interactive login.",
            "At the receiving machine's local console, set `PasswordAuthentication no` and "
            "`KbdInteractiveAuthentication no` for this connection (inside the matching Match "
            "block if one applies), run `sudo sshd -t`, reload sshd, then paste a fresh result "
            f"back. {rerun}",
        )
    return Check(
        STATUS_PASS,
        f"Effective configuration for {user}@{addr}:{port} is key-only "
        "(PasswordAuthentication no, KbdInteractiveAuthentication no).",
    )


def parse_connection_label(text: str) -> Optional[dict[str, str]]:
    """The first line of the receiver's copy block, or None."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.lower().startswith("connection:"):
            return None
        rest = line.split(":", 1)[1]
        return dict(re.findall(r"([A-Za-z][A-Za-z0-9_]*)=(\S+)", rest))
    return None


def _same_numeric_address(left: str, right: str) -> bool:
    left_value = numeric_address(left)
    right_value = numeric_address(right)
    return bool(left_value and right_value and left_value == right_value)


def check_connection_label(
    label: Optional[dict[str, str]],
    *,
    user: str,
    client_addr: str,
    server_addr: str,
    server_port: int,
) -> Check:
    """The pasted block must match the connection sshd reported for the login."""
    if label is None:
        return Check(
            STATUS_FAIL,
            "The pasted block has no `connection: ...` parameter line.",
            "Paste the receiving machine's whole copy block, unchanged, starting with its "
            "`connection:` line and followed by the validated `sshd -T` output.",
        )
    mismatches = []
    block_user = label.get("user", "")
    if block_user != user:
        mismatches.append(f"user: block {block_user!r}, observed {user!r}")
    for key, block_value, observed in (
        ("addr", label.get("addr", ""), client_addr),
        ("laddr", label.get("laddr", ""), server_addr),
    ):
        if not _same_numeric_address(block_value, observed):
            mismatches.append(f"{key}: block {block_value!r}, observed {observed!r}")
    block_port = label.get("lport", "")
    if block_port != str(server_port):
        mismatches.append(f"lport: block {block_port!r}, observed {str(server_port)!r}")
    if mismatches:
        return Check(
            STATUS_FAIL,
            "The pasted block was produced for a different connection ("
            + "; ".join(mismatches)
            + ").",
            "Re-run the receiving machine's wizard for this exact connection and paste ITS "
            "whole copy block, unchanged.",
        )
    return Check(
        STATUS_PASS,
        "The receiver's block matches the connection sshd reported "
        f"(user={user}, addr={client_addr}, laddr={server_addr}, lport={server_port}).",
    )


def _ufw_rule_allows(line: str, port: int) -> bool:
    lowered = line.lower()
    if "deny" in lowered or "reject" in lowered:
        return False
    target = lowered.split()[0] if lowered.split() else ""
    if target == "ssh":
        return port == 22
    base, _, proto = target.partition("/")
    if base.isdigit() and int(base) == port and proto in ("", "tcp"):
        return True
    return False


def parse_ufw_status(text: str, *, port: int) -> FirewallState:
    """`ufw status` output -> the state the check needs (no guessing).

    Only the exact values `active` and `inactive` are understood. Any other
    status word is an UNKNOWN state that fails closed; a substring check would
    read "inactive" as active.
    """
    status_known = False
    active = False
    default_incoming = ""
    allowed = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        lowered = line.lower()
        if lowered.startswith("status:"):
            value = lowered.split(":", 1)[1].strip()
            if value == "active":
                active, status_known = True, True
            elif value == "inactive":
                active, status_known = False, True
            else:
                status_known = False
        elif lowered.startswith("default:"):
            match = re.search(r"default:\s*(\w+)\s*\(incoming\)", lowered)
            if match:
                default_incoming = match.group(1)
        elif _ufw_rule_allows(line, port):
            allowed = True
    return FirewallState(
        kind="ufw",
        active=active,
        port_allowed=allowed if active and status_known else None,
        default_incoming=default_incoming,
        status_known=status_known,
    )


def parse_firewalld_list_all(text: str, *, port: int) -> FirewallState:
    """`firewall-cmd --list-all` output -> the state the check needs."""
    allowed = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.lower().startswith("ports:"):
            for token in line.split(":", 1)[1].split():
                base, _, proto = token.partition("/")
                if base.isdigit() and int(base) == port and proto.lower() in ("", "tcp"):
                    allowed = True
        elif line.lower().startswith("services:") and port == 22:
            if "ssh" in line.split(":", 1)[1].split():
                allowed = True
    return FirewallState(kind="firewalld", active=True, port_allowed=allowed)


def _validated_sender(value: str) -> str:
    """A single numeric IP (no hostname, user, wildcard or `any`)."""
    return numeric_address(value)


def check_firewall(state: FirewallState, *, port: int, sender: str = "") -> Check:
    """Whether the local firewall can pass the SSH port.

    Exact remediation is printed for the tested set ONLY: no firewall at all,
    and apt-based ufw. Any other firewall gets a plain-language description and
    the guide -- never a guessed command.
    """
    kind = (state.kind or "").strip().lower()
    if kind in ("", "none"):
        return Check(
            STATUS_PASS,
            "No local firewall was detected. A firewall elsewhere on the path cannot be seen "
            "from here; the cross-machine test from the sending machine is the authority.",
        )
    if kind == "ufw" and not state.status_known:
        return Check(
            STATUS_FAIL,
            "The ufw status output was not understood (expected exactly `Status: active` "
            "or `Status: inactive`), so the firewall state is unknown.",
            "Run `sudo ufw status` at this machine's local console and paste the whole output "
            "back, then run the wizard again.",
        )
    if not state.active:
        return Check(STATUS_PASS, f"{kind} is installed but not active here.")
    if state.port_allowed is True:
        return Check(STATUS_PASS, f"{kind} is active and allows TCP port {port}.")
    if state.port_allowed is False:
        if kind == "ufw":
            source = _validated_sender(sender)
            if source:
                remedy = (
                    f"sudo ufw allow from {source} to any port {port} proto tcp"
                    "    # at this machine's local console, then re-run the wizard"
                )
            else:
                remedy = (
                    f"Allow only the sending machine's address to TCP port {port} at this "
                    "machine's local console; re-run the wizard with a single sender address "
                    "so the exact rule can be printed."
                )
            return Check(
                STATUS_FAIL,
                f"ufw is active with default-{state.default_incoming or 'deny'} incoming and no "
                f"rule allows SSH on TCP port {port}.",
                remedy,
            )
        return Check(
            STATUS_FAIL,
            f"{kind} is active and does not appear to allow TCP port {port}.",
            f"Open the SSH port in {kind} at this machine's local console yourself; this "
            f"combination has not been tested by the wizard. See {GUIDE_TROUBLESHOOTING}.",
        )
    return Check(
        STATUS_WARN,
        f"{kind} is active but whether it allows TCP port {port} could not be read here.",
        f"If the sending machine cannot connect, open the SSH port in {kind} at this machine's "
        f"local console. See {GUIDE_TROUBLESHOOTING}.",
    )


def check_sender_reachability(result: Reachability, *, host: str, port: int) -> Check:
    if result.reachable:
        return Check(STATUS_PASS, f"{host}:{port} accepted a TCP+SSH connection.")
    detail = f" ({result.detail})" if result.detail else ""
    return Check(
        STATUS_FAIL,
        f"Cannot reach {host}:{port} over SSH{detail}.",
        f"On the receiving machine, check that sshd is running and that its firewall allows "
        f"TCP port {port}, then run the wizard here again. See {GUIDE_TROUBLESHOOTING}.",
    )


def _fingerprint_token(value: str) -> str:
    """The `SHA256:...` token from a display line, or the normalised value."""
    text = (value or "").strip()
    match = re.search(r"SHA256:[A-Za-z0-9+/=]+", text)
    if match:
        return match.group(0)
    try:
        return remote_pairing.normalize_fingerprint(text)
    except remote_pairing.PairingError:
        return text


def check_fingerprint(expected: str, presented: str) -> Check:
    """Refuse unless the receiver's displayed fingerprint is the one presented."""
    wanted = (expected or "").strip()
    seen = (presented or "").strip()
    if not wanted:
        return Check(
            STATUS_FAIL,
            "The receiving machine's host fingerprint was not verified.",
            "Run `ccb-remote setup` on the receiving machine first and compare the fingerprint "
            "it displays; pass it with --fingerprint or paste it when asked. Nothing was written.",
        )
    if not seen:
        return Check(
            STATUS_FAIL,
            "The machine answered but no host key could be read, so its fingerprint cannot be checked.",
            f"Confirm the address and port, then try again. See {GUIDE_TROUBLESHOOTING}.",
        )
    normalized_expected = _fingerprint_token(wanted)
    normalized_seen = _fingerprint_token(seen)
    if normalized_expected != normalized_seen:
        return Check(
            STATUS_FAIL,
            f"The receiving machine presented {normalized_seen}, which does not match the "
            f"fingerprint it displayed ({normalized_expected}).",
            "STOP: do not log in. Verify the address and the fingerprint out of band with the "
            "receiving machine's operator. Nothing was written.",
        )
    return Check(STATUS_PASS, f"The receiving machine's fingerprint matches ({normalized_seen}).")


def parse_advertised_methods(text: str) -> tuple[str, ...]:
    """The methods list from ssh's `Permission denied (...)` line."""
    methods: tuple[str, ...] = ()
    for line in (text or "").splitlines():
        match = re.search(r"Permission denied \(([^)]*)\)", line)
        if match:
            methods = tuple(
                item.strip().lower()
                for item in match.group(1).split(",")
                if item.strip()
            )
    return methods


def check_password_refusal(
    result: PasswordProbe,
    *,
    host: str,
    port: int,
) -> Check:
    """Prove the receiver refuses password and keyboard-interactive login."""
    methods = tuple(item.lower() for item in result.methods)
    if not methods:
        return Check(
            STATUS_FAIL,
            f"Could not classify the receiving machine's authentication response for {host}:{port}.",
            "At the receiving machine's local console, confirm PasswordAuthentication no and "
            "KbdInteractiveAuthentication no, reload sshd, then run the wizard again. "
            f"See 'Guided setup' in {GUIDE}.",
        )
    offered = ", ".join(methods)
    if "password" in methods or "keyboard-interactive" in methods:
        return Check(
            STATUS_FAIL,
            "The receiving machine still allows password or keyboard-interactive login "
            f"(it advertises: {offered}). The key login works, but the receiver is not key-only.",
            "At the receiving machine's local console, complete the hardening step: set "
            "`PasswordAuthentication no` and `KbdInteractiveAuthentication no`, run "
            "`sudo sshd -t`, reload sshd, then run the wizard again. "
            f"See 'Guided setup' in {GUIDE}.",
        )
    if "publickey" not in methods:
        return Check(
            STATUS_FAIL,
            f"The receiving machine's refusal does not advertise publickey (offered: {offered}).",
            "Confirm the login key is staged for that account and that sshd allows public-key "
            f"authentication. See {GUIDE_TROUBLESHOOTING}.",
        )
    return Check(
        STATUS_PASS,
        f"The receiving machine refuses password and keyboard-interactive login (it advertises: {offered}).",
    )


def check_fresh_login(
    result: LoginResult,
    *,
    user: str,
    host: str,
    port: int,
) -> Check:
    if result.ok:
        observed = (
            result.client_addr
            and result.client_port
            and result.server_addr
            and result.server_port
        )
        if observed:
            return Check(
                STATUS_PASS,
                f"A fresh key-only login to {user}@{host}:{port} works with the selected key; "
                f"sshd observed client {result.client_addr}:{result.client_port} to server "
                f"{result.server_addr}:{result.server_port}.",
            )
        return Check(
            STATUS_FAIL,
            "The key login succeeded, but the receiving machine did not report a parseable "
            "SSH_CONNECTION, so the connection facts cannot be verified.",
            "Confirm the receiving account's shell can run `printf` and that sshd sets "
            "SSH_CONNECTION, then run the wizard again. This is unclassifiable, so it fails closed.",
        )
    detail = f" ({result.detail})" if result.detail else ""
    return Check(
        STATUS_FAIL,
        f"A fresh key-only login to {user}@{host}:{port} failed{detail}.",
        "Confirm the login key's public half is in that account's authorized_keys, that the "
        "account still has a real shell, then retry. The receiving machine's SSH auth log has "
        f"the exact reason. See {GUIDE_TROUBLESHOOTING}.",
    )


def check_staged_key(text: str, *, account: str = "the destination account") -> Check:
    """A login key is staged at the receiver -- not the restricted CCB gate line."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "ccb-remote-gate" in line or "restrict," in line:
            continue
        if any(prefix in line for prefix in KEY_TYPE_PREFIXES):
            return Check(STATUS_PASS, f"A login key is staged for {account}.")
    return Check(
        STATUS_FAIL,
        f"No usable login key is staged for {account} yet.",
        "On the sending machine, let the wizard print the public key, then add that line to "
        f"{account}'s ~/.ssh/authorized_keys AT THIS MACHINE'S LOCAL CONSOLE. The restricted "
        "CCB gate key is installed later by `ccb-remote pair`, not by this wizard.",
    )


def hardening_commands(*, port: int = 22) -> tuple[str, ...]:
    """The show-and-verify commands; the wizard never runs them."""
    return (
        "# At THIS machine's local console (the receiving machine). Keep a working",
        "# local session open in case you need to undo this.",
        "sudo tee /etc/ssh/sshd_config.d/99-ccb-key-only.conf >/dev/null <<'EOF'",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "EOF",
        "sudo sshd -t && sudo systemctl reload ssh    # the service is \"sshd\" on some distributions",
        "",
        f"# The wizard then re-checks the EFFECTIVE answer (port {port}); nothing is",
        "# taken on trust from the reload command.",
    )


def login_confirmation_text(
    *,
    key_path: Path,
    account: str,
    host: str,
    persists: bool = True,
) -> str:
    persistence = (
        "This grants a persistent SSH login to that account until that key's line is removed "
        "from its authorized_keys."
        if persists
        else "This login grant is temporary."
    )
    return (
        f"Use the login key {key_path} on THIS machine for the destination account "
        f"{account}@{host}. {persistence}"
    )


def pair_handoff_command(
    *,
    alias: str,
    user: str,
    host: str,
    port: int,
    fingerprint: str,
    key_path: Path,
) -> str:
    """The `pair` command, with the SAME endpoint and login identity just verified.

    `--fingerprint` is the value the receiving machine displayed and this
    machine verified; `--pairing-identity` is the login key just tested. Both
    flags are documented in the guide, and `pair` does its own pinning.
    """
    argv = [
        "ccb-remote",
        "pair",
        alias,
        "--destination",
        f"{user}@{host}",
        "--port",
        str(port),
        "--fingerprint",
        fingerprint,
        "--pairing-identity",
        str(key_path),
    ]
    return " ".join(shlex.quote(part) for part in argv)


# --------------------------------------------------------------------------- #
# Real probes (all replaceable)
# --------------------------------------------------------------------------- #


def setup_ssh_bin() -> str:
    return (os.environ.get(SETUP_SSH_ENV) or "").strip() or (shutil.which("ssh") or "ssh")


def _keys_dir() -> Path:
    return remote_client.remote_root() / "keys"


def _verified_endpoint(destination: str, port: int) -> tuple[str, int]:
    """`pair`'s own endpoint refusal, so the wizard cannot disagree with it."""
    return remote_pairing.verified_endpoint(destination, port, ssh_bin=setup_ssh_bin())


def _current_user() -> str:
    return getpass.getuser()


def _account_shell(account: str) -> str:
    try:
        return pwd.getpwnam(account).pw_shell
    except KeyError:
        return ""


def _apt_based() -> bool:
    """The wizard's tested package-manager family: apt-based systems."""
    return shutil.which("apt-get") is not None


def numeric_address(value: str) -> str:
    """A single numeric IPv4/IPv6 address, or empty for anything else.

    A percent sign means an IPv6 zone identifier (fe80::1%eth0). Python's
    ipaddress accepts it, but ufw and the scoped rule do not, so reject it
    here for BOTH the receiver's laddr and the sender-scoped ufw rule.
    """
    text = (value or "").strip()
    if "%" in text:
        return ""
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return ""


def _local_address() -> str:
    """A numeric default for this machine's address on the SSH connection."""
    names = []
    for name in (socket.getfqdn(), socket.gethostname()):
        if name and name not in names:
            names.append(name)
    for name in names:
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                infos = socket.getaddrinfo(
                    name, None, family=family, type=socket.SOCK_STREAM
                )
            except OSError:
                continue
            for info in infos:
                address = numeric_address(info[4][0])
                if address:
                    return address
    return ""


def _sshd_active(port: int) -> Optional[bool]:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return None
    for service in ("ssh", "sshd"):
        try:
            completed = subprocess.run(
                [systemctl, "is-active", service],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if (completed.stdout or "").strip() == "active":
            return True
    # "inactive" may still be socket-activated; the loopback probe decides.
    return None


def _loopback(port: int) -> Optional[bool]:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except OSError:
        return False


def _read_authorized_keys(account: str) -> str:
    try:
        home = Path(pwd.getpwnam(account).pw_dir)
    except KeyError:
        home = Path.home()
    try:
        return (home / ".ssh" / "authorized_keys").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _local_host_fingerprint() -> str:
    lines = []
    for path in sorted(Path("/etc/ssh").glob("ssh_host_*_key.pub")):
        try:
            completed = subprocess.run(
                ["ssh-keygen", "-lf", str(path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0 and (completed.stdout or "").strip():
            lines.append(completed.stdout.strip())
    return "\n".join(lines)


def _detect_firewall(port: int) -> FirewallState:
    ufw = shutil.which("ufw")
    if ufw:
        try:
            completed = subprocess.run(
                [ufw, "status"], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None:
            if completed.returncode == 0:
                return parse_ufw_status(completed.stdout, port=port)
            return FirewallState(kind="ufw", active=True, port_allowed=None)
    firewalld = shutil.which("firewall-cmd")
    if firewalld:
        try:
            state = subprocess.run(
                [firewalld, "--state"], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired):
            state = None
        if state is not None and (state.stdout or "").strip().lower() == "running":
            try:
                listing = subprocess.run(
                    [firewalld, "--list-all"], capture_output=True, text=True, timeout=10
                )
            except (OSError, subprocess.TimeoutExpired):
                return FirewallState(kind="firewalld", active=True, port_allowed=None)
            return parse_firewalld_list_all(listing.stdout, port=port)
    return FirewallState(kind="none", active=False)


def _probe_reachability(destination: str, port: int) -> Reachability:
    with tempfile.TemporaryDirectory(prefix="ccb-setup-") as tmp:
        known = Path(tmp) / "known_hosts"
        try:
            presented = remote_pairing.probe_presented_host_keys(
                destination, port, known_hosts=known, ssh_bin=setup_ssh_bin()
            )
        except remote_pairing.PairingError as exc:
            return Reachability(False, str(exc))
        except OSError as exc:
            return Reachability(False, str(exc))
    if not presented:
        return Reachability(False, "no host key was presented")
    first = presented[0]
    host_key = f"{first['type']} {first['key']}"
    try:
        shown = remote_pairing.fingerprint(first["key"])
    except remote_pairing.PairingError:
        shown = ""
    return Reachability(True, "", host_key, shown)


def _generate_key(path: Path) -> bool:
    if path.exists():
        return True
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        completed = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _ssh_add(path: Path) -> bool:
    try:
        completed = subprocess.run(
            ["ssh-add", str(path)], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _public_key(path: Path) -> str:
    try:
        return Path(str(path) + ".pub").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def parse_ssh_connection(text: str) -> Optional[tuple[str, int, str, int]]:
    """Parse sshd's SSH_CONNECTION line: client-addr client-port server-addr server-port."""
    for line in reversed((text or "").splitlines()):
        fields = line.split()
        if len(fields) != 4:
            continue
        client_addr, client_port, server_addr, server_port = fields
        if not client_addr or not server_addr:
            continue
        try:
            client_port_value = int(client_port)
            server_port_value = int(server_port)
        except ValueError:
            continue
        if not (1 <= client_port_value <= 65535 and 1 <= server_port_value <= 65535):
            continue
        return client_addr, client_port_value, server_addr, server_port_value
    return None


def _fresh_login(
    host: str,
    port: int,
    user: str,
    key_path: Path,
    host_key: str,
) -> LoginResult:
    """One fresh key-only login, verified against the key just compared.

    The known_hosts entry is ephemeral: the receiver's fingerprint was already
    compared out of band, and the SAME host key is the only one this
    connection may present.
    """
    with tempfile.TemporaryDirectory(prefix="ccb-setup-") as tmp:
        known = Path(tmp) / "known_hosts"
        name = host if int(port) == 22 else f"[{host}]:{port}"
        known.write_text(f"{name} {host_key}\n", encoding="utf-8")
        argv = [
            setup_ssh_bin(),
            "-T",
            "-F", os.devnull,
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={known}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            # No silent fallback: this connection can only succeed with the key.
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "PreferredAuthentications=publickey",
            # No connection reuse: this must be a genuinely fresh connection.
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-o", "IdentitiesOnly=yes",
            "-i", str(key_path),
            "-p", str(port),
            f"{user}@{host}",
            'printf "%s\\n" "$SSH_CONNECTION"',
        ]
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return LoginResult(False, str(exc))
    if completed.returncode == 0:
        observed = parse_ssh_connection(completed.stdout or "")
        if observed is None:
            return LoginResult(
                True,
                "login succeeded but the receiving machine did not report SSH_CONNECTION",
            )
        client_addr, client_port, server_addr, server_port = observed
        return LoginResult(
            True,
            "",
            client_addr,
            client_port,
            server_addr,
            server_port,
        )
    detail = ""
    for line in reversed((completed.stderr or "").splitlines()):
        collapsed = " ".join(line.split())
        if collapsed:
            detail = collapsed[-200:]
            break
    return LoginResult(False, detail)


def _probe_password_refusal(
    host: str,
    port: int,
    user: str,
    host_key: str,
) -> PasswordProbe:
    """Offer ONLY password/keyboard-interactive and read the server's methods list."""
    with tempfile.TemporaryDirectory(prefix="ccb-setup-") as tmp:
        known = Path(tmp) / "known_hosts"
        name = host if int(port) == 22 else f"[{host}]:{port}"
        known.write_text(f"{name} {host_key}\n", encoding="utf-8")
        argv = [
            setup_ssh_bin(),
            "-T",
            "-F", os.devnull,
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={known}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "PubkeyAuthentication=no",
            "-o", "PasswordAuthentication=yes",
            "-o", "KbdInteractiveAuthentication=yes",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-p", str(port),
            f"{user}@{host}",
            "true",
        ]
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return PasswordProbe((), str(exc))
    detail = ""
    for line in reversed((completed.stderr or "").splitlines()):
        collapsed = " ".join(line.split())
        if collapsed:
            detail = collapsed[-200:]
            break
    return PasswordProbe(parse_advertised_methods(completed.stderr or ""), detail)


def _run_pair(argv: Sequence[str]) -> int:
    completed = subprocess.run(list(argv), check=False)
    return completed.returncode


@dataclass
class Probes:
    """Every side effect the wizard needs, defaulting to the real thing."""

    which: Callable[[str], Optional[str]] = shutil.which
    current_user: Callable[[], str] = _current_user
    account_shell: Callable[[str], str] = _account_shell
    sshd_active: Callable[[int], Optional[bool]] = _sshd_active
    loopback: Callable[[int], Optional[bool]] = _loopback
    apt_based: Callable[[], bool] = _apt_based
    local_address: Callable[[], str] = _local_address
    firewall: Callable[[int], FirewallState] = _detect_firewall
    host_fingerprint: Callable[[], str] = _local_host_fingerprint
    staged_key: Callable[[str], str] = _read_authorized_keys
    reachability: Callable[[str, int], Reachability] = _probe_reachability
    verified_endpoint: Callable[[str, int], tuple[str, int]] = _verified_endpoint
    generate_key: Callable[[Path], bool] = _generate_key
    ssh_add: Callable[[Path], bool] = _ssh_add
    public_key: Callable[[Path], str] = _public_key
    fresh_login: Callable[[str, int, str, Path, str], LoginResult] = _fresh_login
    password_probe: Callable[[str, int, str, str], PasswordProbe] = _probe_password_refusal
    run_pair: Callable[[Sequence[str]], int] = _run_pair


# --------------------------------------------------------------------------- #
# Interaction helpers (the same style as `ccb-remote pair`)
# --------------------------------------------------------------------------- #


def _ask_value(prompt: str, default: str, input_fn: Callable[[str], str]) -> str:
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    try:
        answer = (input_fn(shown) or "").strip()
    except EOFError:
        return default
    return answer or default


def _confirm(input_fn: Callable[[str], str], out, question: str) -> bool:
    print(question, file=out)
    print("Type 'yes' to continue, anything else to stop.", file=out)
    try:
        answer = (input_fn(PROMPT) or "").strip()
    except EOFError:
        return False
    return answer.lower() == "yes"


def _emit(out, check: Check) -> None:
    label = {
        STATUS_PASS: "[ok]",
        STATUS_FAIL: "[FAIL]",
        STATUS_WARN: "[warn]",
    }.get(check.status, "[?]")
    print(f"{label} {check.finding}", file=out)
    if check.remediation:
        for line in check.remediation.splitlines():
            print(f"       {line}", file=out)


def _pause(input_fn: Callable[[str], str], out, message: str) -> None:
    print(message, file=out)
    try:
        input_fn(PROMPT)
    except EOFError:
        pass


def _read_pasted(
    input_fn: Callable[[str], str],
    out,
    prompt: str,
    *,
    terminator: str = "",
) -> str:
    print(prompt, file=out)
    lines = []
    while True:
        try:
            line = input_fn(PROMPT)
        except EOFError:
            break
        if terminator:
            if line.strip().lower() == terminator.lower():
                break
        elif line == "":
            break
        lines.append(line)
    return "\n".join(lines)


def _pastable_effective(
    *,
    user: str,
    host: str,
    addr: str,
    laddr: str,
    port: int,
    input_fn: Callable[[str], str],
    out,
) -> tuple[Check, str]:
    command = effective_config_command(
        user=user, host=host, addr=addr, laddr=laddr, port=port
    )
    print("Run this READ-ONLY command at this machine's local console:", file=out)
    print(f"  {command}", file=out)
    text = _read_pasted(
        input_fn,
        out,
        "Paste its whole output here, then an empty line (a 'done' answer cannot pass this check):",
    )
    return (
        check_effective_auth(
            text, user=user, host=host, addr=addr, laddr=laddr, port=port
        ),
        text,
    )


def _paste_receiver_effective(
    *,
    user: str,
    client_addr: str,
    server_addr: str,
    server_port: int,
    input_fn: Callable[[str], str],
    out,
) -> Check:
    """Validate the receiving machine's labelled copy block."""
    print(
        "Paste the WHOLE copy block that the RECEIVING machine's wizard validated and told "
        "you to keep, unchanged. It starts with its `connection:` line;",
        file=out,
    )
    print(
        "that line must match what sshd reported for the successful key login (this account, "
        "this machine's address, the receiving listener's address and port). The refusal "
        "probe above is only supplemental; a 'done' answer cannot pass.",
        file=out,
    )
    text = _read_pasted(input_fn, out, "Paste the whole block here, then an empty line:")
    label = parse_connection_label(text)
    label_check = check_connection_label(
        label,
        user=user,
        client_addr=client_addr,
        server_addr=server_addr,
        server_port=server_port,
    )
    if label_check.failed:
        return label_check
    return check_effective_auth(
        text, user=user, host=client_addr, addr=client_addr, port=server_port
    )


def _resolve_role(options: SetupOptions, out, input_fn) -> str:
    value = (options.role or "").strip().lower()
    if value in ("receive", "receiver", "recv", "r"):
        return ROLE_RECEIVE
    if value in ("send", "sender", "s"):
        return ROLE_SEND
    if value:
        return ""
    print("What should this machine do?", file=out)
    print("  1) Receive requests here (this machine accepts messages)", file=out)
    print("  2) Send requests from here (this machine sends messages)", file=out)
    answer = _ask_value("Choose 1 or 2", "", input_fn)
    if answer.strip().lower() in ("1", "receive", "receiver", "r"):
        return ROLE_RECEIVE
    if answer.strip().lower() in ("2", "send", "sender", "s"):
        return ROLE_SEND
    return ""


def _default_alias(host: str) -> str:
    first = re.split(r"[.:]", host.strip(), maxsplit=1)[0].lower()
    cleaned = re.sub(r"[^a-z0-9-]", "-", first).strip("-")
    return cleaned[:32] or "peer"


# --------------------------------------------------------------------------- #
# The two flows
# --------------------------------------------------------------------------- #


def _receiver_flow(options: SetupOptions, out, input_fn, probes: Probes) -> int:
    account = options.user.strip() or probes.current_user()
    check = check_login_shell(probes.account_shell(account), account=account)
    _emit(out, check)
    if check.failed:
        return EXIT_ERROR

    check = check_sshd_local(
        service_active=probes.sshd_active(options.port),
        loopback_ok=probes.loopback(options.port),
        port=options.port,
        apt_based=probes.apt_based(),
    )
    _emit(out, check)
    if check.failed:
        return EXIT_ERROR

    sender = options.sender.strip() or _ask_value(
        "Address of the sending machine as this machine sees it", "", input_fn
    )
    if not sender:
        print("[ERROR] the sending machine's address is needed for the effective-config check", file=out)
        return EXIT_ERROR
    if options.yes:
        local_answer = probes.local_address()
    else:
        local_answer = _ask_value(
            "This machine's numeric address the sending machine connects to "
            "(for the local Match check)",
            probes.local_address(),
            input_fn,
        )
    local_addr = numeric_address(local_answer)
    if not local_addr:
        _emit(
            out,
            Check(
                STATUS_FAIL,
                "The local address must be a single numeric IPv4 or IPv6 address.",
                "sshd matches `Match LocalAddress` by numeric address, not by hostname. "
                "Enter this machine's numeric address that the sending machine connects to.",
            ),
        )
        return EXIT_ERROR
    effective, effective_text = _pastable_effective(
        user=account,
        host=sender,
        addr=sender,
        laddr=local_addr,
        port=options.port,
        input_fn=input_fn,
        out=out,
    )
    _emit(out, effective)
    # NOT a stop: the login key must be staged before hardening is safe, so the
    # receiver continues to show its fingerprint and wait for the sender.

    firewall = probes.firewall(options.port)
    if firewall.kind == "ufw" and firewall.port_allowed is None:
        print(
            "Reading ufw status needs root, so this machine could not read it. Run this "
            "READ-ONLY command at this machine's local console:",
            file=out,
        )
        print("  sudo ufw status", file=out)
        pasted = _read_pasted(
            input_fn,
            out,
            "Paste its whole output here, then type END on its own line:",
            terminator="END",
        )
        if "status:" not in pasted.lower():
            _emit(
                out,
                Check(
                    STATUS_FAIL,
                    "That was not `ufw status` output, so the firewall state is unknown.",
                    "Run `sudo ufw status` at this machine's local console and paste the whole "
                    "output, then run the wizard again.",
                ),
            )
            return EXIT_ERROR
        firewall = parse_ufw_status(pasted, port=options.port)
    check = check_firewall(firewall, port=options.port, sender=sender)
    _emit(out, check)
    if check.failed:
        return EXIT_ERROR

    fingerprint_text = (probes.host_fingerprint() or "").strip()
    print("", file=out)
    print("This machine's SSH host fingerprint:", file=out)
    if fingerprint_text:
        for line in fingerprint_text.splitlines():
            print(f"  {line}", file=out)
    else:
        print("  (could not read it here; run: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub)", file=out)
    print("", file=out)
    print("On the sending machine run:  ccb-remote setup", file=out)
    print("Compare that fingerprint BEFORE the first login; refuse if it differs.", file=out)

    print("", file=out)
    print(
        "Now have the sending machine print its login public key and stage it in "
        f"{account}'s ~/.ssh/authorized_keys AT THIS MACHINE'S LOCAL CONSOLE.",
        file=out,
    )
    while True:
        staged = check_staged_key(probes.staged_key(account), account=account)
        if staged.passed:
            _emit(out, staged)
            break
        _emit(out, staged)
        if options.yes:
            return EXIT_ERROR
        print("Press Enter to re-check after staging the key, or type 'stop' to quit:", file=out)
        try:
            if (input_fn(PROMPT) or "").strip().lower() == "stop":
                return EXIT_ERROR
        except EOFError:
            return EXIT_ERROR

    if effective.failed:
        print("", file=out)
        print("Harden sshd now that the key is staged:", file=out)
        for line in hardening_commands(port=options.port):
            print(f"  {line}" if line else "", file=out)
        print("", file=out)
        effective, effective_text = _pastable_effective(
            user=account,
            host=sender,
            addr=sender,
            laddr=local_addr,
            port=options.port,
            input_fn=input_fn,
            out=out,
        )
        _emit(out, effective)
        if effective.failed:
            return EXIT_ERROR

    print("", file=out)
    print(
        "Copy this WHOLE block to the sending machine's wizard, unchanged. It binds the "
        "validated result to this exact connection:",
        file=out,
    )
    print(
        f"connection: user={account} host={sender} addr={sender} "
        f"laddr={local_addr} lport={options.port}",
        file=out,
    )
    for line in effective_text.splitlines():
        print(line, file=out)
    print("", file=out)
    print(
        "Receiving side ready. Continue on the sending machine: it verifies a fresh key-only "
        "login, re-checks this pasted result, and then prints the exact `ccb-remote pair` command.",
        file=out,
    )
    return EXIT_OK


def _sender_flow(options: SetupOptions, out, input_fn, probes: Probes) -> int:
    ssh = probes.which("ssh")
    if not ssh:
        _emit(
            out,
            Check(
                STATUS_FAIL,
                "No ssh client was found on PATH on this machine.",
                "Install an OpenSSH client, then run the wizard again.",
            ),
        )
        return EXIT_ERROR
    _emit(out, Check(STATUS_PASS, "An ssh client is installed here."))

    host = options.host.strip() or _ask_value(
        "Receiving machine's hostname or IP address", "", input_fn
    )
    if not host:
        print("[ERROR] the receiving machine's address is needed", file=out)
        return EXIT_ERROR
    user = options.user.strip() or _ask_value("Destination account on the receiving machine", "", input_fn)
    if not user:
        print("[ERROR] the destination account is needed", file=out)
        return EXIT_ERROR
    port = int(options.port)
    destination = f"{user}@{host}"

    # `pair` refuses an ambiguous endpoint (ProxyJump/ProxyCommand,
    # ControlMaster/ControlPath, or a HostName/Port rewrite). Run the SAME
    # check BEFORE any probe, so a refusal makes no probe call at all.
    try:
        probes.verified_endpoint(destination, port)
    except remote_pairing.PairingError as exc:
        _emit(
            out,
            Check(
                STATUS_FAIL,
                f"Cannot verify one unambiguous endpoint for {host}:{port}: {exc}",
                "Use a direct ssh connection to that machine (no ProxyJump/ProxyCommand, "
                "no ControlMaster/ControlPath, and no HostName/Port rewrite), then run the "
                "wizard again.",
            ),
        )
        return EXIT_ERROR

    reachable = probes.reachability(destination, port)
    check = check_sender_reachability(reachable, host=destination, port=port)
    _emit(out, check)
    if check.failed:
        return EXIT_ERROR

    expected = options.fingerprint.strip() or _ask_value(
        "Fingerprint the receiving machine displayed (SHA256:...)", "", input_fn
    )
    check = check_fingerprint(expected, reachable.fingerprint)
    _emit(out, check)
    if check.failed:
        return EXIT_ERROR

    alias = options.alias.strip() or _ask_value(
        "Name for the receiving machine (the CCB host alias)", _default_alias(host), input_fn
    )
    try:
        alias = remote_pairing.validate_alias(alias, "machine name")
    except remote_pairing.PairingError as exc:
        print(f"[ERROR] {exc}", file=out)
        return EXIT_ERROR

    key_path = (
        Path(options.key_path).expanduser()
        if options.key_path.strip()
        else _keys_dir() / f"{alias}-login"
    )
    if not options.key_path.strip():
        key_path = Path(
            _ask_value("Login key to use (created if missing)", str(key_path), input_fn)
        ).expanduser()

    confirmation = login_confirmation_text(
        key_path=key_path, account=user, host=host, persists=True
    )
    if options.yes:
        # Non-interactive runs still say exactly what would be granted.
        print(confirmation, file=out)
    elif not _confirm(input_fn, out, confirmation):
        print("Stopped; nothing was written.", file=out)
        return EXIT_ERROR

    if not key_path.exists():
        ok = probes.generate_key(key_path)
        _emit(
            out,
            Check(
                STATUS_PASS if ok else STATUS_FAIL,
                f"Created the login key {key_path}." if ok else f"Could not create the login key {key_path}.",
                None if ok else "Create it yourself with `ssh-keygen -t ed25519 -f PATH`, then re-run the wizard.",
            ),
        )
        if not ok:
            return EXIT_ERROR
    else:
        _emit(out, Check(STATUS_PASS, f"Using the existing login key {key_path}."))

    if probes.ssh_add(key_path):
        _emit(out, Check(STATUS_PASS, f"Added {key_path} to the ssh agent."))
    else:
        _emit(
            out,
            Check(
                STATUS_WARN,
                "Could not add the key to an ssh agent (the key file is still usable with -i).",
            ),
        )

    public = probes.public_key(key_path)
    if not public:
        print(f"[ERROR] could not read the public half of {key_path}", file=out)
        return EXIT_ERROR
    print("", file=out)
    print(
        "Stage this login public key in the receiving account's ~/.ssh/authorized_keys at the "
        "RECEIVING machine's local console:",
        file=out,
    )
    print(f"  {public}", file=out)
    if not options.yes:
        _pause(input_fn, out, "Press Enter after the key is staged at the receiving machine:")

    login = probes.fresh_login(host, port, user, key_path, reachable.host_key)
    check = check_fresh_login(login, user=user, host=host, port=port)
    _emit(out, check)
    if check.failed:
        return EXIT_ERROR

    refusal = probes.password_probe(host, port, user, reachable.host_key)
    check = check_password_refusal(refusal, host=host, port=port)
    _emit(out, check)
    if check.failed:
        return EXIT_ERROR

    # The refusal probe is supplemental: OpenSSH can allow a key-only path and
    # a key-plus-password path at once. Require the receiver's validated
    # effective-config result as the authoritative proof before the handoff.
    receiver_effective = _paste_receiver_effective(
        user=user,
        client_addr=login.client_addr,
        server_addr=login.server_addr,
        server_port=login.server_port,
        input_fn=input_fn,
        out=out,
    )
    _emit(out, receiver_effective)
    if receiver_effective.failed:
        return EXIT_ERROR

    command = pair_handoff_command(
        alias=alias,
        user=user,
        host=host,
        port=port,
        fingerprint=reachable.fingerprint,
        key_path=key_path,
    )
    print("", file=out)
    print("Prerequisites are ready. Finish with the existing pairing command:", file=out)
    print(f"  {command}", file=out)
    print(
        "That command verifies the host key again, previews the project share and asks for its "
        "own approval; this wizard never replaces it.",
        file=out,
    )
    print("", file=out)
    print(
        "To also let the receiving machine START requests here, run `ccb-remote setup` on it "
        "after this pairing completes and choose 'Send requests from here'; that is a separate, "
        "independent pairing.",
        file=out,
    )
    if not options.yes and _confirm(input_fn, out, "Run that `ccb-remote pair` command now?"):
        # Only cli_output's vocabulary leaves this command; `pair`'s own
        # pending/refused distinction is reported by `pair` when run directly.
        return EXIT_OK if probes.run_pair(shlex.split(command)) == EXIT_OK else EXIT_ERROR
    return EXIT_OK


def run_setup(
    options: SetupOptions,
    *,
    out=sys.stdout,
    err=sys.stderr,
    input_fn: Callable[[str], str] = input,
    probes: Optional[Probes] = None,
) -> int:
    """One guided run on ONE machine; returns only `cli_output` exit codes."""
    probes = probes or Probes()
    role = _resolve_role(options, out, input_fn)
    if not role:
        print("[ERROR] choose 'receive' or 'send' (or pass --role receive|send)", file=err)
        return EXIT_ERROR
    try:
        port = int(options.port)
    except (TypeError, ValueError):
        port = 0
    if port <= 0 or port > 65535:
        print(f"[ERROR] invalid SSH port: {options.port}", file=err)
        return EXIT_ERROR
    options.port = port

    check = check_ccb_present(probes.which("ccb-remote"), probes.which("ccb-remote-gate"))
    _emit(out, check)
    if check.failed:
        return EXIT_ERROR

    if role == ROLE_RECEIVE:
        return _receiver_flow(options, out, input_fn, probes)
    return _sender_flow(options, out, input_fn, probes)
