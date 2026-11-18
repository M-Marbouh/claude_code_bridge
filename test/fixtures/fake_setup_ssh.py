#!/usr/bin/env python3
"""A fake `ssh` for the guided-setup tests.

It follows the same shape as `fake_pair_ssh.py`: the real code builds the ssh
argv, and this fixture answers the two exchanges the wizard makes.

    unauthenticated host-key probe   PreferredAuthentications=none
        -> writes the presented host key into UserKnownHostsFile and exits 255
           with the "Permission denied" a real probe produces after key exchange
    fresh key-only login            anything else
        -> exits 0, or 255 when login_denied is requested

Environment knobs:

    CCB_FAKE_SETUP_LOG       jsonl log of {"kind": ..., "argv": [...]}
    CCB_FAKE_SETUP_MODE      unreachable | login_denied | ok (default)
    CCB_FAKE_SETUP_HOSTKEY   "ssh-ed25519 AAAA..." the machine presents
"""

import json
import os
import sys


def _option(argv: list[str], name: str) -> str:
    for index, item in enumerate(argv):
        if item == "-o" and index + 1 < len(argv) and argv[index + 1].startswith(name + "="):
            return argv[index + 1].split("=", 1)[1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return ""


def _effective_config(argv: list[str]) -> str:
    port = "22"
    if "-p" in argv:
        port = argv[argv.index("-p") + 1]
    destination = ""
    for item in argv[1:]:
        if item.startswith("-"):
            continue
        destination = item
    user, _, host = destination.rpartition("@")
    proxyjump = (os.environ.get("CCB_FAKE_SETUP_PROXYJUMP") or "none").strip() or "none"
    return "\n".join(
        [
            f"hostname {host or '127.0.0.1'}",
            f"port {port}",
            f"user {user or 'b'}",
            "proxycommand none",
            f"proxyjump {proxyjump}",
            "controlpath none",
            "controlmaster false",
        ]
    ) + "\n"


def _log(kind: str, argv: list[str]) -> None:
    path = os.environ.get("CCB_FAKE_SETUP_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, "argv": argv}) + "\n")


def _probe(argv: list[str]) -> int:
    mode = (os.environ.get("CCB_FAKE_SETUP_MODE") or "").strip()
    if mode == "unreachable":
        sys.stderr.write(
            "ssh: connect to host 198.51.100.10 port 22: Connection timed out\n"
        )
        return 255
    known = _option(argv, "UserKnownHostsFile")
    host_key = (os.environ.get("CCB_FAKE_SETUP_HOSTKEY") or "").strip()
    if not known or not host_key:
        return 255
    port = argv[argv.index("-p") + 1] if "-p" in argv else "22"
    destination = argv[-2] if len(argv) >= 2 else "127.0.0.1"
    _user, _, host = destination.rpartition("@")
    name = host if port == "22" else f"[{host}]:{port}"
    with open(known, "a", encoding="utf-8") as handle:
        handle.write(f"{name} {host_key}\n")
    sys.stderr.write("Permission denied (publickey).\n")
    return 255


def main() -> int:
    argv = sys.argv[1:]
    if "-G" in argv:
        sys.stdout.write(_effective_config(argv))
        return 0
    if _option(argv, "PreferredAuthentications") == "none":
        _log("probe", argv)
        return _probe(argv)
    preferred = _option(argv, "PreferredAuthentications")
    if "password" in preferred or "keyboard-interactive" in preferred:
        _log("password", argv)
        methods = (os.environ.get("CCB_FAKE_SETUP_METHODS") or "publickey").strip()
        sys.stderr.write(f"Permission denied ({methods}).\n")
        return 255
    _log("login", argv)
    mode = (os.environ.get("CCB_FAKE_SETUP_MODE") or "").strip()
    if mode == "login_denied":
        sys.stderr.write("b@127.0.0.1: Permission denied (publickey).\n")
        return 255
    port = argv[argv.index("-p") + 1] if "-p" in argv else "22"
    connection = (
        os.environ.get("CCB_FAKE_SETUP_SSH_CONNECTION")
        or f"198.51.100.20 54321 192.0.2.5 {port}"
    ).strip()
    sys.stdout.write(connection + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
