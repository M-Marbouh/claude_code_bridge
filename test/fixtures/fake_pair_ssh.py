#!/usr/bin/env python3
"""A fake `ssh` for the pairing tests: it runs the REAL destination-side verbs.

`ccb-remote pair` shells out to the user's normal ssh. This fixture stands in
for that login and executes `<remote-ccb> pairing <verb>` locally, with HOME
pointed at the "other machine" (so the real B-side code edits real files under
a temporary home). It also logs every argv and can simulate the connection
failures the source side must refuse.

Environment knobs (all optional):

    CCB_FAKE_PAIR_LOG          jsonl log of {"argv": [...], "verb": ...}
    CCB_FAKE_PAIR_REMOTE_HOME  HOME for the destination-side run
    CCB_FAKE_PAIR_MODE         deny | unknown_host | stall | fail_apply_once
    CCB_FAKE_PAIR_HOSTKEY      "ssh-ed25519 AAAA..." override for inspect
    CCB_FAKE_PAIR_REPORTED_HOSTKEY
                               host keys the machine CLAIMS (probe still presents HOSTKEY)
    CCB_FAKE_PAIR_EXTRA_HOSTKEY "; "-separated extra keys the machine also reports
    CCB_FAKE_PAIR_GATE         replace the reported gate path
    CCB_FAKE_PAIR_NO_GATE      "1" -> report the gate as missing
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _forward(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _option(argv: list[str], name: str) -> str:
    for index, item in enumerate(argv):
        if item == f"-o" and index + 1 < len(argv) and argv[index + 1].startswith(name + "="):
            return argv[index + 1].split("=", 1)[1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return ""


def _effective_config(argv: list[str]) -> str:
    """What `ssh -G` answers: one unambiguous, direct endpoint."""
    port = "22"
    if "-p" in argv:
        port = argv[argv.index("-p") + 1]
    destination = ""
    for item in argv[1:]:
        if item.startswith("-"):
            continue
        destination = item
    user, _, host = destination.rpartition("@")
    return "\n".join(
        [
            f"hostname {host or '127.0.0.1'}",
            f"port {port}",
            f"user {user or 'b'}",
            "proxycommand none",
            "proxyjump none",
            "controlpath none",
            "controlmaster false",
        ]
    ) + "\n"


def _probe_host_key(argv: list[str]) -> int:
    """`PreferredAuthentications=none`: write the host key, authenticate nothing."""
    known = _option(argv, "UserKnownHostsFile")
    if not known:
        return 255
    host_key = (os.environ.get("CCB_FAKE_PAIR_HOSTKEY") or "").strip()
    if not host_key:
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
        return _probe_host_key(argv)
    try:
        index = argv.index("pairing")
    except ValueError:
        sys.stderr.write("fake ssh: no pairing verb in argv\n")
        return 255
    verb = argv[index + 1] if index + 1 < len(argv) else ""

    log = os.environ.get("CCB_FAKE_PAIR_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "verb": verb}) + "\n")

    mode = (os.environ.get("CCB_FAKE_PAIR_MODE") or "").strip()
    if mode == "deny":
        sys.stderr.write("b@127.0.0.1: Permission denied (publickey).\n")
        return 255
    if mode == "unknown_host":
        sys.stderr.write("Host key verification failed.\n")
        return 255
    if mode == "stall":
        time.sleep(30)
        return 255
    if mode == "change_before_apply" and verb == "apply":
        remote_home = os.environ.get("CCB_FAKE_PAIR_REMOTE_HOME") or str(Path.home())
        keys = Path(remote_home) / ".ssh" / "authorized_keys"
        keys.parent.mkdir(parents=True, exist_ok=True)
        with keys.open("a", encoding="utf-8") as handle:
            handle.write(
                'restrict,command="/opt/ccb/bin/ccb-remote-gate --principal foreign" '
                "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIforeign\n"
            )
    if mode == "fail_apply_once" and verb == "apply":
        marker = Path(os.environ.get("CCB_FAKE_PAIR_LOG", "/tmp/fake-pair.log")).with_suffix(".apply")
        if not marker.exists():
            marker.write_text("failed once", encoding="utf-8")
            sys.stderr.write("b@127.0.0.1: Connection reset by peer\n")
            return 255

    remote_home = os.environ.get("CCB_FAKE_PAIR_REMOTE_HOME") or str(Path.home())
    command = argv[index - 1] if index >= 1 else "ccb-remote"
    if command.startswith('"$HOME"/'):
        command = str(Path(remote_home) / command[len('"$HOME"/') :])
    elif command.startswith("~/"):
        command = str(Path(remote_home) / command[2:])
    env = dict(os.environ)
    env["HOME"] = remote_home
    payload = sys.stdin.buffer.read()
    completed = subprocess.run(
        [sys.executable, command, "pairing", verb],
        input=payload,
        capture_output=True,
        env=env,
    )
    stdout = completed.stdout.decode("utf-8", "replace")
    host_key = (os.environ.get("CCB_FAKE_PAIR_HOSTKEY") or "").strip()
    gate = (os.environ.get("CCB_FAKE_PAIR_GATE") or "").strip()
    no_gate = (os.environ.get("CCB_FAKE_PAIR_NO_GATE") or "").strip() == "1"
    if verb == "inspect" and (host_key or gate or no_gate):
        result = None
        for line in reversed(stdout.splitlines()):
            if line.strip().startswith("{"):
                try:
                    result = json.loads(line)
                    break
                except ValueError:
                    continue
        if isinstance(result, dict):
            if host_key:
                parts = host_key.split(" ", 2)
                # `REPORTED` is what the machine CLAIMS its host keys are; the
                # probe above still presents `CCB_FAKE_PAIR_HOSTKEY`. They must
                # agree, and a mismatch is the case pairing has to refuse.
                reported = (os.environ.get("CCB_FAKE_PAIR_REPORTED_HOSTKEY") or "").strip()
                if reported:
                    parts = reported.split(" ", 2)
                extra = (os.environ.get("CCB_FAKE_PAIR_EXTRA_HOSTKEY") or "").strip()
                result["host_keys"] = [{"type": parts[0], "key": parts[1]}]
                for item in [part for part in extra.split(";") if part.strip()]:
                    extra_parts = item.split(" ", 2)
                    result["host_keys"].append(
                        {"type": extra_parts[0], "key": extra_parts[1]}
                    )
            if gate:
                result["gate"] = gate
                result["gate_present"] = True
            if no_gate:
                result["gate"] = ""
                result["gate_present"] = False
            stdout = json.dumps(result) + "\n"
    _forward(stdout)
    sys.stderr.write(completed.stderr.decode("utf-8", "replace"))
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
