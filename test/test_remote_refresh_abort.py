"""Real-subprocess regression for item 7: abort, not close, on expiry.

Uses the REAL `SshSession` (no scripted double) against a local fake `ssh`
executable pointed to by `CCB_SSH_BIN`. The fake answers `hello` and then
either answers `list` (positive control) or stalls forever (the case that used
to make the deadline cleanup block ~5.5 s).

Kept in its own module so the subprocess timing is measured in a fresh process
tree of its own, and so the fake's pid file cannot be confused with anything
another test spawns.
"""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

import remote_client
import remote_discovery
from test_remote_client import HOST_ALIAS, _write_hosts

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Long enough that a graceful `close()` (5 s + 5 s) is unmistakable, short
#: enough that the test suite is not held up by a leaked child.
STALL_SECONDS = 60

_FAKE_SSH = '''#!/usr/bin/env python3
"""Fake `ssh`: speaks the frame protocol like a destination, then stalls."""
import json
import os
import sys
import time

sys.path.insert(0, {lib_dir!r})
import remote_protocol as proto

pid_file = os.environ.get("CCB_FAKE_SSH_PIDFILE")
if pid_file:
    with open(pid_file, "a", encoding="utf-8") as handle:
        handle.write(str(os.getpid()) + "\\n")

mode = os.environ.get("CCB_FAKE_SSH_MODE", "stall")

# 1. the hello request -> answer it (advertising M3 list detail)
hello_request = proto.read_frame(sys.stdin.buffer)
proto.write_frame(
    sys.stdout.buffer,
    {{
        "ok": True,
        "op": "hello",
        "op_id": hello_request.get("op_id"),
        "capabilities": {{
            "verbs": ["hello", "list", "submit", "status", "fetch"],
            "reply_modes": {{
                "claude": ["delivery-only", "explicit-reply"],
                "codex": ["delivery-only", "explicit-reply"],
            }},
            "max_frame": 262144,
            "proto": {{"major": 1, "minor": 1}},
            "list_sessions": True,
        }},
    }},
)

# 2. the list request -> answer it, or stall (the bug being regressed)
list_request = proto.read_frame(sys.stdin.buffer)
if mode == "full":
    proto.write_frame(
        sys.stdout.buffer,
        {{
            "ok": True,
            "op": "list",
            "op_id": list_request.get("op_id"),
            "projects": [
                {{
                    "alias": "demo",
                    "providers": [
                        {{
                            "provider": "claude",
                            "present": True,
                            "state": "present",
                            "reason": "",
                            "sessions": [{{"live_id": "live-claude-1", "active": True}}],
                        }}
                    ],
                }}
            ],
        }},
    )
    sys.exit(0)

# Stall: ignore everything until killed.
time.sleep({stall})
'''


def _write_fake_ssh(tmp_path: Path, mode: str) -> Path:
    script = tmp_path / f"fake-ssh-{mode}"
    script.write_text(
        _FAKE_SSH.format(
            lib_dir=str(REPO_ROOT / "lib"), stall=STALL_SECONDS
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _pids(path: Path) -> list[int]:
    try:
        return [int(line) for line in path.read_text().split() if line.strip()]
    except OSError:
        return []


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pid: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return not _alive(pid)


def _hosts_file_with(tmp_path: Path, aliases: tuple[str, ...]) -> None:
    known = tmp_path / "known_hosts"
    known.write_text("example.test ssh-ed25519 AAAA\n", encoding="utf-8")
    identity = tmp_path / "id_ed25519"
    identity.write_text("private", encoding="utf-8")
    hosts = {
        alias: {
            "ssh_target": f"relay@{alias}.example.test",
            "port": 22,
            "identity_file": str(identity),
            "known_hosts": str(known),
            "issuer_token": "abc1",
        }
        for alias in aliases
    }
    path = Path.home() / ".ccb" / "remote" / "hosts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hosts": hosts}), encoding="utf-8")


def _prepare(tmp_path: Path, monkeypatch, mode: str) -> Path:
    _write_hosts(tmp_path)
    fake = _write_fake_ssh(tmp_path, mode)
    pids = tmp_path / "fake-ssh-pids"
    monkeypatch.setenv("CCB_SSH_BIN", str(fake))
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", mode)
    monkeypatch.setenv("CCB_FAKE_SSH_PIDFILE", str(pids))
    return pids


def test_a_stalled_real_ssh_child_is_aborted_without_waiting(tmp_path, monkeypatch):
    """The deadline path must not inherit `close()`'s 5 s (then 5 s) wait."""
    pids = _prepare(tmp_path, monkeypatch, "stall")
    host = remote_client.load_hosts()[HOST_ALIAS]

    started = time.monotonic()
    outcome = remote_discovery.refresh(
        host, timeout_s=0.5, out=io.StringIO(), err=io.StringIO()
    )
    elapsed = time.monotonic() - started

    print("abort timing:", round(elapsed, 3), "s", outcome)
    assert outcome["ok"] is False
    assert outcome["reason"] == "timed out after 0.5s"
    # A graceful close() alone spends ~5.5 s here; the whole refresh must not.
    assert elapsed < 1.5

    children = _pids(pids)
    assert children, "the fake ssh never ran"
    assert all(_wait_gone(pid) for pid in children), children


def test_a_real_ssh_that_answers_is_untouched_by_the_abort_path(tmp_path, monkeypatch):
    """Positive control: the same real session still succeeds when it answers."""
    pids = _prepare(tmp_path, monkeypatch, "full")
    host = remote_client.load_hosts()[HOST_ALIAS]

    outcome = remote_discovery.refresh(
        host, timeout_s=2.0, out=io.StringIO(), err=io.StringIO()
    )

    print("full mode outcome:", outcome)
    assert outcome["ok"] is True, outcome
    view = remote_discovery.cache_view(HOST_ALIAS, host=host)
    assert view["status"] == "known"
    assert view["projects"][0]["providers"][0]["sessions"] == [
        {"live_id": "live-claude-1", "active": True}
    ]
    assert all(_wait_gone(pid) for pid in _pids(pids))


def test_two_stalled_hosts_stay_inside_the_total_deadline(
    tmp_path, monkeypatch, capsys
):
    """The wall clock `ccb-list --remote` promises holds with real children."""
    import importlib.machinery
    import importlib.util

    _hosts_file_with(tmp_path, ("server-a", "server-b"))
    fake = _write_fake_ssh(tmp_path, "stall")
    pids = tmp_path / "fake-ssh-pids"
    monkeypatch.setenv("CCB_SSH_BIN", str(fake))
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "stall")
    monkeypatch.setenv("CCB_FAKE_SSH_PIDFILE", str(pids))
    monkeypatch.setenv(remote_discovery.REFRESH_TIMEOUT_ENV, "0.5")
    monkeypatch.setenv(remote_discovery.TOTAL_REFRESH_TIMEOUT_ENV, "1.0")

    path = REPO_ROOT / "bin" / "ccb-list"
    loader = importlib.machinery.SourceFileLoader("ccb_list_abort_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, "_session_entries", lambda include_stale: [])

    started = time.monotonic()
    assert module.main(["--remote"]) == 0
    elapsed = time.monotonic() - started

    out = capsys.readouterr().out
    print("total deadline with real children:", round(elapsed, 3), "s")
    assert "Active CCB Projects:" in out
    assert "Other machines (last observed, not live):" in out
    assert elapsed < 2.5
    assert all(_wait_gone(pid) for pid in _pids(pids))
