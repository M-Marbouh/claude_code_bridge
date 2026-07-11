#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

script_dir = Path(__file__).resolve().parent
lib_dir = script_dir.parent / "lib"
sys.path.insert(0, str(lib_dir))

from ccb_runtime_status import resolve_project_runtime_status
from session_utils import find_project_session_file


_BASE_SESSION_FILES = {
    "codex": ".codex-session",
    "gemini": ".gemini-session",
    "opencode": ".opencode-session",
    "claude": ".claude-session",
}


def _autostart_daemons(work_dir: Path) -> None:
    ccb_ping = script_dir / "ccb-ping"
    for provider, filename in _BASE_SESSION_FILES.items():
        try:
            if not find_project_session_file(work_dir, filename):
                continue
        except Exception:
            continue
        try:
            subprocess.run(
                [str(ccb_ping), provider, "--autostart"],
                cwd=str(work_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except Exception:
            continue


def _payload(work_dir: Path) -> dict:
    project = resolve_project_runtime_status(work_dir)
    statuses = {key: status.to_dict() for key, status in sorted(project.providers.items())}
    mounted = [key for key, status in sorted(project.providers.items()) if status.mounted]
    reasons = {key: status.reason for key, status in sorted(project.providers.items()) if not status.mounted}
    return {
        "cwd": str(work_dir),
        "mounted": mounted,
        "providers": statuses,
        "reasons": reasons,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report mounted CCB providers for a project.")
    parser.add_argument("--simple", action="store_true", help="Print a space-separated provider list.")
    parser.add_argument("--json", action="store_true", help="Print JSON output (default).")
    parser.add_argument("--autostart", action="store_true", help="Best-effort daemon autostart before checking.")
    parser.add_argument("path", nargs="?", default=os.getcwd())
    args = parser.parse_args(argv)

    work_dir = Path(args.path).expanduser()
    try:
        work_dir = work_dir.resolve()
    except Exception:
        work_dir = work_dir.absolute()

    if args.autostart:
        _autostart_daemons(work_dir)

    payload = _payload(work_dir)
    if args.simple:
        print(" ".join(payload["mounted"]))
    else:
        print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
