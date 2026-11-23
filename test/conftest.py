from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
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


def pytest_configure(config) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    lib_dir = repo_root / "lib"
    sys.path.insert(0, str(lib_dir))
    # Registered here (not in a second `pytest_configure`, which would silently
    # replace this one) so `@pytest.mark.long_test` is a known marker.
    if hasattr(config, "addinivalue_line"):
        config.addinivalue_line(
            "markers",
            f"{LONG_TEST_MARKER}: test legitimately needs longer than CCB_TEST_TIMEOUT_S",
        )


def pytest_sessionfinish(*_args, **_kwargs) -> None:
    shutil.rmtree(_TMUX_SOCKET_BASE, ignore_errors=True)


# --------------------------------------------------------------------------- #
# F1: a per-test wall-clock limit, so a hung test can never stall a night run
# --------------------------------------------------------------------------- #
#
# Background: one adapter test spun for two hours (a fake clock advanced only
# from a callback that the hung loop never reached, with `time.sleep` patched
# out). Nothing in the suite bounded a single test, so the whole run stalled and
# the work queued behind it died.
#
# Rules, deliberately conservative:
#   * POSIX + MAIN THREAD only -- signal.alarm is per-process and the main
#     thread owns it, so a worker-thread test is left to the outer watchdog;
#   * the override is VALIDATED (a bad CCB_TEST_TIMEOUT_S falls back to the
#     default instead of silently disabling the guard);
#   * the previous SIGALRM handler and the previous timer are restored in
#     `finally`, so this can never eat another tool's alarm;
#   * expiry dumps every thread's stack (faulthandler) before failing the test,
#     which is the evidence a hung run needs.

#: Seconds a single test may run. Override with CCB_TEST_TIMEOUT_S.
DEFAULT_TEST_TIMEOUT_S = 120.0

#: Opt-out marker for a test that legitimately needs longer than the guard.
LONG_TEST_MARKER = "long_test"


class TestTimeout(BaseException):
    """Raised into a test that outlived its limit.

    Deliberately a ``BaseException``, not an ``Exception``: production code and
    test helpers are full of ``except Exception`` loops, and a one-shot
    ``TimeoutError`` thrown into one of those was swallowed -- the test kept
    looping and the guard silently did nothing. Only ``BaseException`` reaches
    every enclosing handler.
    """


def test_timeout_s() -> float | None:
    """The validated per-test limit in seconds, or None when the guard is off.

    ``CCB_TEST_TIMEOUT_S=0`` (or a negative/unparseable value) means the same as
    the default: a guard you can accidentally disable by typo is worse than no
    guard, so only an explicit zero disables it.
    """
    raw = (os.environ.get("CCB_TEST_TIMEOUT_S") or "").strip()
    if not raw:
        return DEFAULT_TEST_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TEST_TIMEOUT_S
    if value != value or value in (float("inf"), float("-inf")):
        return DEFAULT_TEST_TIMEOUT_S
    # ONLY an explicit zero turns the guard off. A negative value is a mistake,
    # not an instruction: it keeps the default rather than silently disabling
    # the very thing that exists to stop a hung run.
    if value == 0:
        return None
    if value < 0:
        return DEFAULT_TEST_TIMEOUT_S
    return value


def _request_marks(request) -> set[str]:
    try:
        return {mark.name for mark in request.node.iter_markers()}
    except Exception:
        return set()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Bound ONE test's call phase; restore the alarm state afterwards."""
    import faulthandler
    import signal
    import threading

    limit = test_timeout_s()
    if limit is None or os.name != "posix" or threading.current_thread() is not threading.main_thread():
        yield
        return
    if LONG_TEST_MARKER in {mark.name for mark in item.iter_markers()}:
        yield
        return

    state: dict = {}

    def _expired(_signum, _frame):
        state["expired"] = True
        state["hits"] = state.get("hits", 0) + 1
        # The stack dump IS the diagnosis: a hung test that only times out tells
        # you nothing about where it was stuck.
        with contextlib.suppress(Exception):
            faulthandler.dump_traceback()
        # RE-ARM before raising. One injection can be caught somewhere on the
        # stack, so the deadline is kept live until the test actually unwinds:
        # every later slice re-raises, and the outermost handler cannot eat all
        # of them.
        with contextlib.suppress(Exception):
            signal.setitimer(signal.ITIMER_REAL, 0.25)
        raise TestTimeout(
            f"test exceeded its {limit:g}s limit (CCB_TEST_TIMEOUT_S); "
            "mark it with @pytest.mark.long_test if it needs longer"
        )

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, limit)
    try:
        outcome = yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        # Put back whatever timer was already armed for someone else.
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])
    # A hookwrapper that just yields SWALLOWS the exception raised inside the
    # wrapped call. The expiry has to be re-raised as this hook's own failure,
    # or a hung test would be reported as passing.
    if state.get("expired"):
        # `outcome.excinfo` is an `ExceptionInfo` in newer pytest and a plain
        # (type, value, tb) tuple in older ones; take the value from either.
        cause = None
        exc = getattr(outcome, "excinfo", None)
        if exc is not None:
            cause = getattr(exc, "value", None)
            if cause is None and isinstance(exc, tuple) and len(exc) >= 2:
                cause = exc[1]
        raise TestTimeout(
            f"test exceeded its {limit:g}s limit (CCB_TEST_TIMEOUT_S); "
            "mark it with @pytest.mark.long_test if it needs longer"
        ) from (cause if isinstance(cause, BaseException) else None)


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


# --------------------------------------------------------------------------- #
# The suite must never reach the LIVE askd of the project it is running in
# --------------------------------------------------------------------------- #
#
# Regression guard for 2026-11-11: a full-suite run shut down the caller's own
# askd (its state file recorded a graceful shutdown with no idle/parent line,
# i.e. a shutdown request). `HOME` is replaced per test, but nothing stopped a
# test from naming the REAL home (or from leaving a child process without a
# HOME at all, which resolves to the account's home in the passwd database).
#
# The live daemon of this project is therefore captured once, before any
# fixture runs, and every in-process route to a daemon (the real state file,
# its endpoint, its mailbox) is refused for the duration of a test. A child
# process that would run with the real home is refused too, and the state file
# is checked after every test, so a leak is reported against the test that
# caused it instead of being discovered later.
_REAL_HOME = Path(os.environ.get("HOME") or Path.home())


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True



def _capture_live_daemons() -> list[dict]:
    """The real askd state files of this machine that are RUNNING right now."""
    found: list[dict] = []
    root = _REAL_HOME / ".cache" / "ccb"
    candidates = [root / "askd.json"] + sorted(root.glob("projects/*/askd.json"))
    for state in candidates:
        try:
            payload = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or not payload.get("pid") or not payload.get("port"):
            continue
        if not _pid_alive(int(payload["pid"])):
            continue
        try:
            resolved = state.resolve()
        except OSError:
            resolved = state
        found.append(
            {
                "state": resolved,
                "pid": int(payload.get("pid") or 0),
                "host": str(payload.get("host") or ""),
                "port": int(payload.get("port") or 0),
                "mailbox": str(payload.get("mailbox_dir") or ""),
            }
        )
    return found


_LIVE_ASKD = _capture_live_daemons()

#: A test may register a DISPOSABLE daemon here (temp state file, endpoint that
#: nothing listens on) so the guard's refusals can be exercised without ever
#: aiming a probe at the caller's real daemon.
_REGISTERED_LIVE: list[dict] = []
_GUARD_LOG = Path(os.environ.get("CCB_TEST_GUARD_LOG") or "/tmp/ccb-test-live-askd-guard.log")
_CURRENT = {"test": "(collection)"}


def live_states() -> list[dict]:
    """Every daemon the suite must not touch: the real ones, plus test doubles."""
    return list(_LIVE_ASKD) + list(_REGISTERED_LIVE)


def register_live_state(entry: dict) -> dict:
    """Make a disposable state/daemon 'live' for the guard (test doubles)."""
    _REGISTERED_LIVE.append(dict(entry))
    return dict(entry)


def unregister_live_state(entry: dict) -> None:
    for index, item in enumerate(_REGISTERED_LIVE):
        if item is entry or item == entry:
            del _REGISTERED_LIVE[index]
            return


def _resolve_text(value) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except (OSError, RuntimeError, ValueError, TypeError):
        return text


def _live_index() -> dict:
    """The live daemons' state paths, endpoints, tokens and mailboxes."""
    index = {"states": set(), "ports": set(), "tokens": set(), "mailboxes": set()}
    for item in live_states():
        if item.get("state"):
            index["states"].add(_resolve_text(item["state"]))
        try:
            port = int(item.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if port:
            index["ports"].add(port)
        if item.get("token"):
            index["tokens"].add(str(item["token"]))
        if item.get("mailbox"):
            index["mailboxes"].add(_resolve_text(item["mailbox"]))
    return index


def _live_reason(value) -> str:
    """Why this STATE (path or dict) belongs to a live daemon, or ""."""
    index = _live_index()
    if isinstance(value, dict):
        try:
            port = int(value.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if port and port in index["ports"]:
            return f"state dict for the live endpoint port {port}"
        token = str(value.get("token") or "")
        if token and token in index["tokens"]:
            return "state dict carrying the live daemon's token"
        mailbox = _resolve_text(value.get("mailbox_dir") or "")
        if mailbox and mailbox in index["mailboxes"]:
            return f"state dict naming the live mailbox {mailbox}"
        return ""
    resolved = _resolve_text(value)
    return f"state file {resolved}" if resolved and resolved in index["states"] else ""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _guard_loads_in_child(command) -> bool:
    """Would that child install THIS guard (a pytest run over this `test/`)?"""
    parts = [str(part) for part in (command if isinstance(command, (list, tuple)) else [command])]
    if "pytest" not in parts:
        return False
    test_dir = str(Path(__file__).resolve().parent).rstrip("/")
    return any(
        part == "test" or part.startswith("test/") or part.startswith(test_dir + "/")
        for part in parts
    )


def _same_home(value: str) -> bool:
    try:
        return Path(value).expanduser().resolve() == Path(_REAL_HOME).resolve()
    except (OSError, RuntimeError):
        return False


def _note(reason: str) -> None:
    """Record who touched the live daemon's files, without stopping them."""
    import traceback

    caller = "".join(traceback.format_stack()[-6:-2]).strip()
    try:
        _GUARD_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _GUARD_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"[{_CURRENT['test']}] {reason}\n{caller}\n\n")
    except OSError:
        pass


def _refuse(reason: str) -> None:
    """Refuse an attempt to reach the live daemon, and record who made it."""
    _note(reason)
    raise AssertionError(
        "this test tried to reach the LIVE askd of the project it runs in "
        f"({reason}); tests must use their own daemon under their own HOME"
    )


@pytest.fixture(autouse=True)
def guard_the_live_askd(request, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may read, ping, request, or shut down a live askd."""
    _CURRENT["test"] = request.node.nodeid

    import askd_rpc

    real_read_state = askd_rpc.read_state

    def read_state(state_file):
        reason = _live_reason(state_file)
        if reason:
            # Reading is harmless and is how a caller learns the endpoint, so
            # it is recorded rather than refused: a test that gets this far is
            # the one to look at, and the endpoints below stay unreachable.
            _note(f"read the live {reason}")
        return real_read_state(state_file)

    monkeypatch.setattr(askd_rpc, "read_state", read_state)

    def wrap(name: str, original):
        def guarded(*args, **kwargs):
            # A state PATH or a state DICT: both are refused when they name a
            # live daemon, whatever module hands them over.
            for candidate in (kwargs.get("state_file"), args[0] if args else None):
                if not isinstance(candidate, (str, Path, dict)):
                    continue
                reason = _live_reason(candidate)
                if reason:
                    _refuse(f"{name} against the {reason}")
            return original(*args, **kwargs)

        return guarded

    for name in ("ping_daemon", "request_daemon", "shutdown_daemon", "connect_daemon"):
        original = getattr(askd_rpc, name, None)
        if original is not None:
            monkeypatch.setattr(askd_rpc, name, wrap(name, original))

    # The mailbox is a filesystem channel: a request written into a live
    # daemon's mailbox is delivered without any TCP at all.
    real_write_json = getattr(askd_rpc, "_write_json_atomic", None)
    if real_write_json is not None:

        def write_json_atomic(path, payload):
            target = _resolve_text(path)
            for mailbox in _live_index()["mailboxes"]:
                if mailbox and target.startswith(mailbox + os.sep):
                    _refuse(f"write into the live mailbox {mailbox}")
            return real_write_json(path, payload)

        monkeypatch.setattr(askd_rpc, "_write_json_atomic", write_json_atomic)

    # Defense in depth: the TCP endpoint itself, whatever module dials it.
    real_connect = socket.socket.connect

    def connect(self, address):
        try:
            host, port = address[0], int(address[1])
        except (TypeError, ValueError, IndexError):
            return real_connect(self, address)
        if int(port) in _live_index()["ports"]:
            _refuse(f"tcp {host}:{port}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect, raising=False)

    # A child process that would run with the REAL home (or with no HOME at
    # all, which falls back to the passwd entry) can find that home's daemons.
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        env = kwargs.get("env")
        if env is not None and live_states():
            home = str(env.get("HOME") or env.get("USERPROFILE") or "")
            if not home or _same_home(home):
                command = args[0] if args else "?"
                if not _guard_loads_in_child(command):
                    _refuse(f"subprocess with the real home ({command})")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)

    before = {}
    for item in _LIVE_ASKD:
        try:
            before[str(item["state"])] = item["state"].read_bytes()
        except OSError:
            before[str(item["state"])] = b""
    yield
    for path, content in before.items():
        try:
            after = Path(path).read_bytes()
        except OSError:
            after = b""
        if after != content:
            _refuse(f"the live state file changed: {path}")


@pytest.fixture(autouse=True)
def no_launcher_daemon_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may leave a launcher watchdog thread behind.

    Regression guard for 2026-11-11: `AILauncher.run_up()` starts a background
    "askd-watchdog" thread. The thread outlived the test that started it, and
    once the per-test HOME override was undone it resolved the CALLER's own askd
    state (`state_file_path("askd.json")` under the real home), pinged that
    machine's daemon and -- when it judged that daemon unhealthy -- restarted or
    shut it down. That is how a full-suite run could take the caller's daemon
    with it.

    The thread is disarmed here (it is started and exits immediately, so the
    launcher's own `join()` keeps working). A test that ever needs the real loop
    patches this back with its own monkeypatch.
    """
    import threading

    real_start = threading.Thread.start

    def start(self):
        if self.name == "askd-watchdog" and getattr(self, "_target", None) is not None:
            _note("disarmed the launcher's askd watchdog thread")
            self._target = lambda *_args, **_kwargs: None
            self._args = ()
            self._kwargs = {}
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
