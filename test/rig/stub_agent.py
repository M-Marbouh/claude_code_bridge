#!/usr/bin/env python3
"""Stub provider pane agent for the cross-machine rig.

Records every line it receives to a fixed log inside the rig's project and
never replies: M1 delivery is one-way, so "the pane received the text" is the
whole destination-side signal. Installed as both `codex` and `claude`.
"""
from __future__ import annotations

import os
import sys
import time

LOG = os.environ.get("CCB_RIG_STUB_LOG") or "/srv/demo/.stub-agent.log"


def _log(text: str) -> None:
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()


def main() -> int:
    name = os.path.basename(sys.argv[0]) or "stub"
    _log(f"START {name} argv={sys.argv[1:]}\n")
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


if __name__ == "__main__":
    raise SystemExit(main())
