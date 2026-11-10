from __future__ import annotations

import os
import secrets
import shutil
import stat
import tempfile
import sys
from pathlib import Path

import pytest

#: Every test gets its own tmux socket directory, so a stray default-socket
#: client cannot resolve to the caller's server. It lives ONCE per session at a
#: short path: tmux socket paths are limited to ~108 bytes and per-test tmp_path
#: names plus long `-L` socket names overflow that limit.
_TMUX_SOCKET_BASE = Path(tempfile.mkdtemp(prefix="ccb-tmux-"))

#: The caller's own tmux server, captured BEFORE anything is scrubbed. A test
#: path that rebuilds its environment (dropping TMUX_TMPDIR) would otherwise
#: fall back to `/tmp/tmux-<uid>` -- the live server -- so a session-scoped
#: shim earlier in PATH refuses exactly that socket and forwards everything
#: else unchanged.
_LIVE_TMUX_SOCKET = (os.environ.get("TMUX") or "").split(",", 1)[0].strip()
_REAL_TMUX = shutil.which("tmux") if os.name == "posix" else None


_TMUX_GUARD_SHIM = """#!/bin/sh
# Test-suite guard: never address the tmux server the suite was started from.
# Isolated (-L/-S, TMUX, TMUX_TMPDIR) calls are forwarded unchanged; only the
# caller's own socket is refused.
live="__LIVE__"
sock=""
name="default"
prev=""
for a in "$@"; do
  case "$prev" in
    -L) name="$a" ;;
    -S) sock="$a" ;;
  esac
  prev="$a"
done
if [ -z "$sock" ]; then
  if [ -n "${TMUX:-}" ]; then
    sock="${TMUX%%,*}"
  else
    sock="${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)/${name:-default}"
  fi
fi
if [ "$sock" = "$live" ]; then
  echo "tmux: refusing to address the suite caller's tmux server" >&2
  exit 1
fi
exec "__REAL__" "$@"
"""


def _install_tmux_guard() -> str:
    if not _REAL_TMUX or not _LIVE_TMUX_SOCKET:
        return ""
    guard_dir = _TMUX_SOCKET_BASE / "bin"
    guard_dir.mkdir(parents=True, exist_ok=True)
    shim = guard_dir / "tmux"
    shim.write_text(
        _TMUX_GUARD_SHIM.replace("__LIVE__", _LIVE_TMUX_SOCKET).replace(
            "__REAL__", _REAL_TMUX
        ),
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # Visible to the isolation guard test without being scrubbed per test.
    os.environ["CCB_TEST_LIVE_TMUX_SOCKET"] = _LIVE_TMUX_SOCKET
    return str(guard_dir)


_GUARD_DIR = _install_tmux_guard()
if _GUARD_DIR:
    os.environ["PATH"] = _GUARD_DIR + os.pathsep + os.environ.get("PATH", "")


def pytest_configure() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    lib_dir = repo_root / "lib"
    sys.path.insert(0, str(lib_dir))


def pytest_sessionfinish(*_args, **_kwargs) -> None:
    shutil.rmtree(_TMUX_SOCKET_BASE, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolate_process_temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CLI subprocess receipts, state, and registries out of live user paths."""
    for name in (
        "CCB_MANAGED",
        "CCB_CALLER",
        "CCB_RUN_DIR",
        "CCB_SESSION_ID",
        "CCB_CALLER_PANE_ID",
        "CCB_CALLER_TERMINAL",
        "CCB_ROUTE_LAUNCH_ID",
        "CCB_CALLER_LIVE_ID",
        # A suite run from inside a CCB pane inherits that pane's live
        # identity; tests must never depend on the launching environment.
        "CCB_MEMBER_ID",
        "CCB_MEMBER_TOKEN",
        "CCB_LIVE_ID",
        "CCB_LIVE_TOKEN",
        "CCB_ROUTE_LIVE_ID",
        "CCB_ROUTE_CALLER_LIVE_ID",
        "CCB_ROUTE_CALLER_TOKEN",
        "CCB_ROUTE_CALLER_PANE_ID",
        "CCB_ROUTE_CALLER_TERMINAL",
        "CCB_ROUTE_PANE_ID",
        "CCB_ROUTE_TERMINAL",
        "CCB_ROUTE_SESSION_FILE",
        "CCB_ROUTE_PROJECT_ID",
        "CCB_ROUTE_TASK_ID",
        "WEZTERM_PANE",
        "TMUX_PANE",
        # `TMUX` names the caller's LIVE tmux server socket. Leaving it in
        # place let tests that set `TMUX_PANE` (or name a pane id) drive real
        # tmux against the session the suite was started from: tmux retitled
        # the caller's pane and rewrote its @ccb_marker. Every test gets no
        # tmux client identity and its own socket directory instead.
        "TMUX",
    ):
        monkeypatch.delenv(name, raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # Even a stray default-socket client (no `-L`) must resolve here, never in
    # the shared /tmp socket directory where the caller's server lives.
    tmux_socket_root = _TMUX_SOCKET_BASE / secrets.token_hex(4)
    tmux_socket_root.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_socket_root))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
