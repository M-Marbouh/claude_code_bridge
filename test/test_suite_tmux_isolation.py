"""The suite must never address the tmux server it was started from.

Regression guard for 2026-11-10: running `pytest test` inside a live CCB tmux
pane relabelled that pane (title and `@ccb_marker` set to a test project's
marker) because the suite inherited `TMUX` while some tests set `TMUX_PANE`
themselves. The autouse fixture in `test/conftest.py` removes the caller's tmux
identity and points every socket lookup at a per-test directory; these tests
prove that property against a real, disposable "caller" server.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
#: The interpreter that runs the suite resolves pytest from the real user site
#: directory, so a child run must keep HOME (the guard fixture replaces it).
REAL_HOME = os.environ.get("HOME", "")
CANARY_TITLE = "guard-caller-pane-title"
CANARY_MARKER = "guard-caller-pane-marker"

#: A probe test that reproduces the original hazard exactly: a test that sets
#: `TMUX_PANE` and then lets a real tmux client act on that pane without naming
#: a socket. With the conftest guard it cannot reach the caller's server; if
#: the guard regresses, this relabels the stand-in and fails.
PROBE = '''
import os
import subprocess


def test_a_test_may_not_reach_the_callers_tmux_server():
    assert "TMUX" not in os.environ, "the guard did not scrub the caller's tmux identity"
    binary = os.environ["GUARD_TMUX_BIN"]
    os.environ["TMUX_PANE"] = "%0"
    result = subprocess.run(
        [
            binary, "select-pane", "-t", "%0", "-T", "probe-title",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "the conftest guard leaked the caller's tmux server: "
        + result.stdout
        + result.stderr
    )
    result = subprocess.run(
        [binary, "set-option", "-p", "-t", "%0", "@ccb_marker", "probe-marker"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "the conftest guard leaked the caller's tmux server"
'''


def _server_env(socket_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["TMUX_TMPDIR"] = str(socket_dir)
    return env


@pytest.fixture
def caller_stand_in(tmp_path: Path):
    """A disposable tmux server whose first panes look like a live session."""
    binary = shutil.which("tmux")
    if not binary:
        pytest.skip("tmux is required for the isolation guard")
    socket = "g-" + uuid.uuid4().hex[:12]
    socket_dir = tmp_path / "guard-sockets"
    socket_dir.mkdir()
    env = _server_env(socket_dir)

    def run(*args: object, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [binary, "-L", socket, *map(str, args)],
            env=env,
            capture_output=True,
            text=True,
            check=check,
        )

    started = run(
        "-f", "/dev/null", "new-session", "-d", "-x", "120", "-y", "40",
        "-s", "guard", "-c", "/tmp", "sleep 600",
        check=False,
    )
    if started.returncode:
        pytest.skip(f"sandbox cannot create a disposable tmux server: {started.stderr.strip()}")
    try:
        # `%0` (what the old tests named) plus the higher ids some tests use.
        # New windows, not splits: no minimum-size constraints on the panes.
        for _ in range(7):
            run("new-window", "-d", "-t", "guard", "-c", "/tmp", "sleep 600")
        run("select-pane", "-t", "%0", "-T", CANARY_TITLE)
        run("set-option", "-p", "-t", "%0", "@ccb_marker", CANARY_MARKER)
        socket_path = run("display-message", "-p", "#{socket_path}").stdout.strip()
        session = run("display-message", "-p", "#{session_id}").stdout.strip()
        yield run, socket_path, socket_dir, session
    finally:
        run("kill-server", check=False)
        shutil.rmtree(socket_dir, ignore_errors=True)


def _panes(run) -> list[str]:
    listing = run(
        "list-panes", "-a", "-F", "#{pane_id}\t#{pane_title}\t#{@ccb_marker}"
    ).stdout
    return [line for line in listing.splitlines() if line.strip()]


def _contents(run) -> dict[str, str]:
    """Each pane's visible text: a paste into a real pane would show up here."""
    panes = {}
    for line in _panes(run):
        pane_id = line.split("\t", 1)[0]
        panes[pane_id] = run("capture-pane", "-p", "-t", pane_id, check=False).stdout
    return panes


def _child_env(socket_path: str, socket_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env["HOME"] = REAL_HOME
    env["USERPROFILE"] = REAL_HOME
    env["TMUX"] = f"{socket_path},{os.getpid()},0"
    env["TMUX_PANE"] = "%0"
    env["GUARD_TMUX_BIN"] = shutil.which("tmux") or "tmux"
    # Point an unscrubbed default-socket client at the stand-in server, so a
    # regression is caught here instead of being hidden by path luck.
    env["TMUX_TMPDIR"] = str(socket_dir)
    return env


def _run_child(args: list[str], env: dict) -> subprocess.CompletedProcess:
    # `--rootdir test` makes the child load test/conftest.py (the guard) even
    # when the only argument is a probe file in a hidden directory.
    return subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "--rootdir", "test", *args,
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )


def test_the_suite_cannot_relabel_the_pane_it_was_started_from(caller_stand_in, tmp_path):
    run, socket_path, socket_dir, _session = caller_stand_in
    before = _panes(run)
    before_contents = _contents(run)
    assert f"%0\t{CANARY_TITLE}\t{CANARY_MARKER}" in before
    env = _child_env(socket_path, socket_dir)

    # A dot-prefixed directory under test/ is never collected by the real
    # suite, but it is inside the guard's root, so the conftest fixture applies.
    probe_dir = ROOT / "test" / f".guard-probe-{uuid.uuid4().hex[:8]}"
    probe_dir.mkdir()
    try:
        (probe_dir / "test_probe.py").write_text(PROBE, encoding="utf-8")
        probe = _run_child([str(probe_dir / "test_probe.py")], env)
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    assert probe.returncode == 0, probe.stdout[-4000:] + probe.stderr[-4000:]
    assert "1 passed" in probe.stdout

    # The exact tests that used to write the caller's pane title, marker and
    # agent option through the inherited TMUX server...
    labellers = _run_child(
        [
            "test/test_ccb_tmux_split.py::test_run_up_sorts_providers_in_tmux",
            "test/test_ccb_tmux_split.py::test_run_up_backfills_existing_claude_session_work_dir_fields",
        ],
        env,
    )
    assert labellers.returncode == 0, labellers.stdout[-4000:] + labellers.stderr[-4000:]
    assert "2 passed" in labellers.stdout

    # ...and tests that issue raw tmux commands against hard-coded pane ids.
    raw = _run_child(["test/test_ask_cli.py", "-k", "peer"], env)
    assert raw.returncode == 0, raw.stdout[-4000:] + raw.stderr[-4000:]

    after = _panes(run)
    assert after == before
    assert not [line for line in after if "CCB-" in line or "probe-" in line]
    assert _contents(run) == before_contents


def test_the_guard_scrubs_the_callers_tmux_identity():
    assert "TMUX" not in os.environ
    assert "TMUX_PANE" not in os.environ
    socket_root = Path(os.environ["TMUX_TMPDIR"])
    assert socket_root.is_dir()
    # A unique per-test directory nested under the session socket base, so no
    # default-socket client can resolve to the caller's shared
    # /tmp/tmux-<uid> directory. Short enough for the tmux socket limit too.
    assert socket_root.parent.name.startswith("ccb-tmux-")
    assert str(socket_root) not in ("/tmp", "/tmp/tmux")
    assert len(socket_root.name) == 8
    assert len(str(socket_root)) < 80


def test_a_sanitized_environment_is_blocked_by_the_shim():
    """Even an env rebuilt from scratch cannot fall back to the caller's server."""
    live = os.environ.get("CCB_TEST_LIVE_TMUX_SOCKET", "").strip()
    shim = shutil.which("tmux") or ""
    if not live or not shim:
        pytest.skip("the suite was not started inside tmux")
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_TMPDIR")}
    result = subprocess.run(
        [shim, "list-panes"], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode != 0
    assert "refusing" in result.stderr

    # Positive control: the same shim with an isolated socket directory runs.
    isolated = {**env, "TMUX_TMPDIR": os.environ["TMUX_TMPDIR"]}
    allowed = subprocess.run(
        [shim, "-L", "guard-probe-" + uuid.uuid4().hex[:6], "list-sessions"],
        env=isolated,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "refusing" not in allowed.stderr


def test_the_guard_shim_forwards_a_real_failure_status_and_both_streams(tmp_path: Path):
    """The shim may only refuse the caller's socket; otherwise it is transparent."""
    from conftest import _TMUX_GUARD_SHIM

    real = tmp_path / "real-tmux"
    real.write_text(
        "#!/bin/sh\necho forwarded-stdout\necho forwarded-stderr >&2\nexit 37\n",
        encoding="utf-8",
    )
    real.chmod(0o700)
    blocked_socket = str(tmp_path / "caller-socket")
    shim = tmp_path / "tmux"
    shim.write_text(
        _TMUX_GUARD_SHIM.replace("__LIVE__", blocked_socket).replace("__REAL__", str(real)),
        encoding="utf-8",
    )
    shim.chmod(0o700)
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}

    blocked = subprocess.run(
        [str(shim), "-S", blocked_socket, "list-panes"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert blocked.returncode == 1
    assert "refusing" in blocked.stderr
    assert "forwarded" not in blocked.stdout

    allowed = subprocess.run(
        [str(shim), "-S", str(tmp_path / "isolated-socket"), "list-panes"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert allowed.returncode == 37
    assert allowed.stdout == "forwarded-stdout\n"
    assert allowed.stderr == "forwarded-stderr\n"
