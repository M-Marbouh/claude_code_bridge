#!/usr/bin/env python3
"""Stub provider pane agent for the cross-machine rig.

Records every line it receives to a fixed log inside the rig's project, and --
for M2 reply rows -- acts on a delivered remote prompt: when a line carries
`CCB_REMOTE_REPLY_COMMAND`, it runs that exact printed command from its own
pane environment with the scripted answer on stdin, so the credential path is
the real one.

Behaviour is scripted per row by a JSON file the harness writes:

    {"mode": "reply", "answer": "...", "delay": 0}
    {"mode": "explicit_id", "answer": "...", "target_id": "<id>"}
    {"mode": "other_member", "answer": "..."}      # sibling pane's credential
    {"mode": "no_credential", "answer": "..."}     # no CCB member credential

No file, no action: M1 delivery rows keep the pure "record what arrived" stub.
Installed as both `codex` and `claude`.
"""
from __future__ import annotations

import glob
import json
import os
import shlex
import subprocess
import sys
import time

LOG = os.environ.get("CCB_RIG_STUB_LOG") or "/srv/demo/.stub-agent.log"
CONFIG = "/srv/demo/.stub-reply.json"
#: Give the delivery worker time to finish the paste and commit `delivered`
#: before answering: the reply command line arrives before the injection ends.
SETTLE_S = 2.5
RUN_DIR = "/home/b/.ccb/run"


def _log(text: str) -> None:
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()


def _config() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _launch_providers() -> dict:
    """The live launch record's provider entries (each carries a member credential)."""
    best: dict = {}
    newest = -1.0
    for path in glob.glob(f"{RUN_DIR}/ccb-session-*.json"):
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            continue
        try:
            stamp = float(record.get("updated_at") or 0)
        except (TypeError, ValueError):
            stamp = 0.0
        if stamp >= newest and isinstance(record.get("providers"), dict):
            newest = stamp
            best = record["providers"]
    return best


def _credential_env(mode: str, provider: str) -> dict:
    env = dict(os.environ)
    if mode == "no_credential":
        env.pop("CCB_MEMBER_ID", None)
        env.pop("CCB_MEMBER_TOKEN", None)
        env.pop("CCB_LIVE_ID", None)
        env.pop("CCB_LIVE_TOKEN", None)
        return env
    if mode == "other_member":
        providers = _launch_providers()
        sibling = next(
            (
                name
                for name in providers
                if name != provider
                and providers[name].get("member_id")
                and providers[name].get("member_token")
            ),
            "",
        )
        if sibling:
            env["CCB_MEMBER_ID"] = str(providers[sibling]["member_id"])
            env["CCB_MEMBER_TOKEN"] = str(providers[sibling]["member_token"])
            env.pop("CCB_LIVE_ID", None)
            env.pop("CCB_LIVE_TOKEN", None)
    return env


def _answer(command: str, provider: str, seen: set[str]) -> None:
    config = _config()
    mode = str(config.get("mode") or "reply")
    argv = shlex.split(command)
    if len(argv) < 3:
        _log(f"REPLY-SKIP malformed command={command!r}\n")
        return
    target_id = argv[-1]
    if mode == "explicit_id" and config.get("target_id"):
        target_id = str(config["target_id"])
        argv[-1] = target_id
    if target_id in seen:
        _log(f"REPLY-SKIP duplicate id={target_id}\n")
        return
    seen.add(target_id)
    delay = float(config.get("delay") or 0)
    time.sleep(max(SETTLE_S, float(config.get("settle") or 0)) + delay)
    env = _credential_env(mode, provider)
    _log(
        f"REPLY-DECISION mode={mode} id={target_id} "
        f"ambient_credential={'yes' if os.environ.get('CCB_MEMBER_ID') else 'no'} "
        f"env_credential={'yes' if env.get('CCB_MEMBER_ID') else 'no'}\n"
    )
    try:
        proc = subprocess.run(
            argv,
            input=str(config.get("answer") or "").encode("utf-8"),
            capture_output=True,
            env=env,
            timeout=180,
        )
    except Exception as exc:  # a broken reply must be visible, never fatal
        _log(f"REPLY exception={type(exc).__name__}: {exc}\n")
        return
    _log(
        "REPLY rc={} out={} err={}\n".format(
            proc.returncode,
            json.dumps(proc.stdout.decode("utf-8", "replace")),
            json.dumps(proc.stderr.decode("utf-8", "replace")),
        )
    )


def main() -> int:
    name = os.path.basename(sys.argv[0]) or "stub"
    provider = "codex" if name == "codex" else name
    _log(
        f"START {name} argv={sys.argv[1:]} "
        f"credential={'yes' if os.environ.get('CCB_MEMBER_ID') else 'no'}\n"
    )
    seen: set[str] = set()
    while True:
        try:
            line = sys.stdin.readline()
        except Exception:
            time.sleep(0.05)
            continue
        if not line:
            time.sleep(0.05)
            continue
        _log(f"LINE {line}" if line.endswith("\n") else f"LINE {line}\n")
        if line.startswith("CCB_REMOTE_REPLY_COMMAND:"):
            command = line.split(":", 1)[1].strip()
            try:
                _answer(command, provider, seen)
            except Exception as exc:  # never let a reply kill the pane agent
                _log(f"REPLY-BROKEN {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
