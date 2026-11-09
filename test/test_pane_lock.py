from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import pane_lock
from pane_lock import PaneLockError, PaneLockTimeout, lock_key, pane_injection_lock
from terminal import TmuxBackend

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_LOCAL_ID = "20261109-182536-922-2626995"


def _tmux(socket_name: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-L", socket_name, *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
    )


@pytest.fixture()
def tmux_server(tmp_path: Path):
    """An isolated tmux server (`-L`) that is NOT the user's own server."""
    if not shutil_which("tmux"):
        pytest.skip("tmux is not installed")
    socket_name = f"ccb-lock-test-{os.getpid()}-{int(time.time() * 1000) % 100000}"
    log = tmp_path / "pane.log"
    log.write_text("")
    _tmux(socket_name, "new-session", "-d", "-s", "ccb-lock", f"exec cat >> {log}")
    try:
        yield socket_name, log
    finally:
        _tmux(socket_name, "kill-server", check=False)


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def _pane_id(socket_name: str) -> str:
    cp = _tmux(socket_name, "list-panes", "-s", "-F", "#{pane_id}")
    return cp.stdout.splitlines()[0].strip()


# --------------------------------------------------------------------------- #
# Key identity
# --------------------------------------------------------------------------- #


def test_tmux_key_is_the_real_server_socket_not_the_caller_env(tmux_server, monkeypatch):
    socket_name, _log = tmux_server
    pane = _pane_id(socket_name)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("CCB_TMUX_SOCKET", raising=False)

    backend = TmuxBackend(socket_name=socket_name)
    key = lock_key(backend, pane)

    assert key.startswith("tmux:")
    # Canonical: the server socket AND tmux's own pane id, not the target string.
    expected_socket = _tmux(
        socket_name, "display-message", "-p", "-t", pane, "#{socket_path}"
    ).stdout.strip()
    assert os.path.realpath(expected_socket) in key
    assert key.endswith(f"|{pane}")


def test_two_tmux_servers_never_share_a_key(tmux_server):
    socket_name, _log = tmux_server
    pane = _pane_id(socket_name)
    other = f"{socket_name}-b"
    try:
        _tmux(other, "new-session", "-d", "-s", "ccb-lock", "sleep 30")
        # Both servers name their only pane %0: only the socket path can differ.
        other_pane = _pane_id(other)
        first = lock_key(TmuxBackend(socket_name=socket_name), pane)
        second = lock_key(TmuxBackend(socket_name=other), other_pane)
        assert first.split("|", 1)[1] == second.split("|", 1)[1]
        assert first != second
    finally:
        _tmux(other, "kill-server", check=False)


def test_session_name_and_pane_id_targets_produce_one_key(tmux_server):
    """The split the review found: a session-name target vs `%0` for one pane."""
    socket_name, _log = tmux_server
    pane = _pane_id(socket_name)
    backend = TmuxBackend(socket_name=socket_name)
    assert lock_key(backend, pane) == lock_key(backend, "ccb-lock")
    assert lock_key(backend, pane) == lock_key(backend, f"ccb-lock:0.{pane.lstrip('%')}")
    # Same server, different panes: different keys.
    try:
        _tmux(socket_name, "split-window", "-d", "-t", pane, "sleep 30")
        panes = [line for line in _tmux(socket_name, "list-panes", "-s", "-F", "#{pane_id}").stdout.split() if line]
        assert len(set(lock_key(backend, p) for p in panes)) == len(set(panes))
    except Exception as exc:  # pragma: no cover - split support varies
        pytest.skip(f"could not split a pane here: {exc}")


def test_unresolvable_pane_identity_raises_and_creates_no_lock(tmp_path):
    """No fallback key: an unqueryable pane means zero sends."""
    backend = TmuxBackend(socket_name="ccb-lock-missing-server")
    started = time.monotonic()
    with pytest.raises(PaneLockError):
        lock_key(backend, "%0")
    assert time.monotonic() - started < 5.0
    with pytest.raises(PaneLockError):
        with pane_injection_lock(backend, "%0", 0.1):
            pytest.fail("an unresolvable identity must not acquire anything")
    assert list(pane_lock.lock_root().glob("*.lock")) == [] or not pane_lock.lock_root().exists()


def test_key_is_identical_in_a_fresh_process_without_tmux_env(tmux_server):
    socket_name, _log = tmux_server
    pane = _pane_id(socket_name)
    here = lock_key(TmuxBackend(socket_name=socket_name), pane)

    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from terminal import TmuxBackend\n"
        "from pane_lock import lock_key\n"
        "print(lock_key(TmuxBackend(socket_name=%r), %r))\n"
    ) % (str(REPO_ROOT / "lib"), socket_name, pane)
    env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE"}}
    env["CCB_TMUX_SOCKET"] = ""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == here


def test_wezterm_identity_ignores_socket_paths_and_socket_count(monkeypatch, tmp_path):
    """A second gui socket must NOT change the key (that would split the lock)."""
    for name in ("CODEX_WEZTERM_CLASS", "WEZTERM_CLASS", "CODEX_WEZTERM_PREFER_MUX"):
        monkeypatch.delenv(name, raising=False)
    runtime = tmp_path / "run"
    (runtime / "wezterm").mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    (runtime / "wezterm" / "gui-sock-1").write_text("")
    monkeypatch.setenv("WEZTERM_UNIX_SOCKET", str(runtime / "wezterm" / "gui-sock-1"))
    first = pane_lock.wezterm_instance_identity()

    # A second instance appears: another socket, and the env now points at it.
    (runtime / "wezterm" / "gui-sock-2").write_text("")
    monkeypatch.setenv("WEZTERM_UNIX_SOCKET", str(runtime / "wezterm" / "gui-sock-2"))
    second = pane_lock.wezterm_instance_identity()

    assert first == second
    assert "gui-sock" not in first
    assert str(runtime) in first and "class:-" in first and "mux:0" in first


def test_wezterm_key_depends_on_runtime_class_and_mux_only(monkeypatch, tmp_path):
    from terminal import WeztermBackend

    runtime = tmp_path / "run"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("WEZTERM_UNIX_SOCKET", raising=False)
    monkeypatch.delenv("WEZTERM_CLASS", raising=False)
    monkeypatch.delenv("CODEX_WEZTERM_CLASS", raising=False)
    monkeypatch.delenv("CODEX_WEZTERM_PREFER_MUX", raising=False)

    backend = WeztermBackend()
    base = lock_key(backend, "3")
    assert base.endswith(";pane:3")
    assert "wezterm:runtime:" in base

    monkeypatch.setenv("WEZTERM_CLASS", "com.example.Term")
    assert lock_key(WeztermBackend(), "3") != base
    monkeypatch.delenv("WEZTERM_CLASS")
    monkeypatch.setenv("CODEX_WEZTERM_PREFER_MUX", "1")
    assert lock_key(WeztermBackend(), "3") != base
    assert lock_key(WeztermBackend(), "4") != lock_key(WeztermBackend(), "3")


# --------------------------------------------------------------------------- #
# Lock file mechanics
# --------------------------------------------------------------------------- #


class _Backend:
    def is_alive(self, pane):  # pragma: no cover - unused
        return True


def test_lock_file_is_private_persistent_and_reusable(tmp_path):
    backend = _Backend()
    with pane_injection_lock(backend, "%42", 5.0):
        path = pane_lock.lock_path_for_key(lock_key(backend, "%42"))
        assert path.is_file()
        assert path.parent.parent == Path.home() / ".ccb" / "locks"
        assert oct(path.stat().st_mode & 0o777) == oct(0o600)
        assert oct(path.parent.stat().st_mode & 0o777) == oct(0o700)
    # Never unlinked: unlinking would let two processes lock two inodes.
    assert path.is_file()
    with pane_injection_lock(backend, "%42", 5.0):
        assert path.is_file()


def test_nested_acquire_on_the_same_thread_is_a_no_op():
    backend = _Backend()
    with pane_injection_lock(backend, "%7", 0.2):
        with pane_injection_lock(backend, "%7", 0.2):
            with pane_injection_lock(backend, "%7", 0.2):
                pass


def test_timeout_raises_and_leaves_nothing_held(tmp_path):
    backend = _Backend()
    path = pane_lock.lock_path_for_key(lock_key(backend, "%8"))
    released = threading.Event()

    def hold() -> None:
        with pane_injection_lock(backend, "%8", 5.0):
            released.wait(5.0)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        deadline = time.monotonic() + 5.0
        while not path.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        with pytest.raises(PaneLockTimeout):
            with pane_injection_lock(backend, "%8", 0.2):
                pytest.fail("the lock was taken while another holder had it")
    finally:
        released.set()
        holder.join(5.0)
    assert not holder.is_alive()
    # Control: once the holder is gone the same call succeeds.
    with pane_injection_lock(backend, "%8", 5.0):
        pass


def test_disable_switch_takes_no_lock(tmp_path):
    backend = _Backend()
    os.environ["CCB_PANE_LOCK_DISABLE"] = "1"
    try:
        path = pane_lock.lock_path_for_key(lock_key(backend, "%9"))
        with pane_injection_lock(backend, "%9", 0.1):
            pass
        assert not path.exists()
    finally:
        os.environ.pop("CCB_PANE_LOCK_DISABLE", None)


# --------------------------------------------------------------------------- #
# Cross-process injection
# --------------------------------------------------------------------------- #

_CHILD = """
import json, os, sys, time
sys.path.insert(0, {lib!r})
from pane_lock import pane_injection_lock
from terminal import TmuxBackend

socket_name, pane, marker, start_file, out_file = sys.argv[1:6]
backend = TmuxBackend(socket_name=socket_name)
deadline = time.time() + 30
while not os.path.exists(start_file):
    if time.time() > deadline:
        raise SystemExit("start barrier timed out")
    time.sleep(0.005)
# Measure the INJECTION WINDOW only: with the lock on, this window is entered
# only after the lock is held (the backend's own acquire re-enters), so two
# windows can never overlap; with CCB_PANE_LOCK_DISABLE=1 they can and do.
with pane_injection_lock(backend, pane, 60):
    started = time.monotonic()
    backend.send_text(pane, marker)
    finished = time.monotonic()
with open(out_file, "w", encoding="utf-8") as handle:
    json.dump({{"start": started, "end": finished}}, handle)
"""


def _child_script(tmp_path: Path) -> Path:
    script = tmp_path / "inject_child.py"
    script.write_text(_CHILD.format(lib=str(REPO_ROOT / "lib")), encoding="utf-8")
    return script


def _run_two_injections(tmp_path: Path, socket_name: str, pane: str, *, disable: bool):
    script = _child_script(tmp_path)
    start_file = tmp_path / "start.now"
    env = dict(os.environ)
    env["CCB_TMUX_ENTER_DELAY"] = "0.5"
    env["CCB_PANE_LOCK_TIMEOUT_S"] = "60"
    env.pop("CCB_TMUX_SOCKET", None)
    env.pop("CCB_PANE_LOCK_DISABLE", None)
    if disable:
        env["CCB_PANE_LOCK_DISABLE"] = "1"

    outputs = [tmp_path / "child-a.json", tmp_path / "child-b.json"]
    procs = []
    for marker, out in (("MARKER-ALPHA-1234", outputs[0]), ("MARKER-BETA-5678", outputs[1])):
        procs.append(
            subprocess.Popen(
                [sys.executable, str(script), socket_name, pane, marker, str(start_file), str(out)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        )
    time.sleep(0.3)  # both children are waiting on the barrier
    start_file.write_text("go")
    for proc in procs:
        _out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
    intervals = [json.loads(path.read_text()) for path in outputs]
    return intervals


def test_cross_process_injection_is_serialized(tmux_server, tmp_path):
    socket_name, log = tmux_server
    pane = _pane_id(socket_name)

    intervals = _run_two_injections(tmp_path, socket_name, pane, disable=False)
    first, second = sorted(intervals, key=lambda item: item["start"])
    assert first["end"] <= second["start"] + 0.05, (first, second)

    lines = [line for line in log.read_text().splitlines() if line.strip()]
    # Each prompt arrived whole and separately submitted.
    assert sorted(lines) == ["MARKER-ALPHA-1234", "MARKER-BETA-5678"]


def test_disable_switch_reproduces_the_overlap(tmux_server, tmp_path):
    """Negative control: without the lock the two injections DO overlap."""
    socket_name, _log = tmux_server
    pane = _pane_id(socket_name)

    intervals = _run_two_injections(tmp_path, socket_name, pane, disable=True)
    first, second = sorted(intervals, key=lambda item: item["start"])
    assert second["start"] < first["end"], (first, second)


# --------------------------------------------------------------------------- #
# peer_routing: the pane lock wraps revalidation, policy and the send
# --------------------------------------------------------------------------- #


class _RecordingBackend(_Backend):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, pane_id: str, text: str) -> None:
        self.sent.append((pane_id, text))


def _peer_task(provider: str = "codex"):
    from askd.adapters.base import PeerDestination, ProviderRequest, QueuedTask

    request = ProviderRequest(
        client_id="c1",
        work_dir="/tmp/peer-dest",
        timeout_s=30.0,
        quiet=True,
        message="remote body",
        caller="remote:bob",
        req_id=FIXTURE_LOCAL_ID,
        delivery_only=True,
        suppress_completion_hook=True,
        peer_destination=PeerDestination(
            pane_id="%3", terminal="tmux", work_dir="/tmp/peer-dest"
        ),
    )
    return QueuedTask(request=request, created_ms=0, req_id=FIXTURE_LOCAL_ID, done_event=threading.Event())


def _patch_peer(monkeypatch, backend, *, revalidate_ok=True, refusal=""):
    import delivery_policy
    import peer_routing
    import terminal

    monkeypatch.setattr(terminal, "get_backend_for_session", lambda _data: backend)
    monkeypatch.setattr(
        peer_routing,
        "revalidate_peer_destination",
        lambda _destination: peer_routing.DestinationResolution() if revalidate_ok
        else peer_routing.DestinationResolution(error="unavailable", detail="pane replaced"),
    )
    monkeypatch.setattr(delivery_policy, "refusal_for_request", lambda *a, **k: refusal)
    return peer_routing


def _hold_lock(backend, pane: str, seconds: float):
    started = threading.Event()
    released = threading.Event()

    def hold() -> None:
        with pane_injection_lock(backend, pane, 5.0):
            started.set()
            released.wait(seconds)

    thread = threading.Thread(target=hold)
    thread.start()
    assert started.wait(5.0)
    return thread, released


def test_revalidation_runs_after_the_wait_and_refuses_with_zero_sends(monkeypatch):
    backend = _RecordingBackend()
    peer_routing = _patch_peer(monkeypatch, backend, revalidate_ok=False)
    holder, released = _hold_lock(backend, "%3", 0.4)
    try:
        began = time.monotonic()
        result = peer_routing.send_peer_message(_peer_task(), "codex", "peer:key")
        elapsed = time.monotonic() - began
    finally:
        released.set()
        holder.join(5.0)

    assert elapsed >= 0.3, "revalidation must run only after the lock is held"
    assert result.status == "failed"
    assert "unavailable" in result.reply
    assert backend.sent == []


def test_role_refusal_also_runs_after_the_wait(monkeypatch):
    backend = _RecordingBackend()
    peer_routing = _patch_peer(monkeypatch, backend, refusal="role policy refused: no")
    holder, released = _hold_lock(backend, "%3", 0.3)
    try:
        result = peer_routing.send_peer_message(_peer_task(), "codex", "peer:key")
    finally:
        released.set()
        holder.join(5.0)
    assert result.status == "failed"
    assert "role policy refused" in result.reply
    assert backend.sent == []


def test_lock_timeout_is_reported_as_proven_non_delivery(monkeypatch):
    backend = _RecordingBackend()
    peer_routing = _patch_peer(monkeypatch, backend)
    monkeypatch.setenv("CCB_PANE_LOCK_TIMEOUT_S", "0.2")
    holder, released = _hold_lock(backend, "%3", 1.0)
    try:
        result = peer_routing.send_peer_message(_peer_task(), "codex", "peer:key")
    finally:
        released.set()
        holder.join(5.0)
    assert result.status == "failed"
    assert "lock" in result.reply.lower()
    assert backend.sent == []


def test_dispatch_hook_runs_inside_the_lock_immediately_before_the_send(monkeypatch):
    backend = _RecordingBackend()
    peer_routing = _patch_peer(monkeypatch, backend)
    order: list[str] = []

    def hook() -> None:
        order.append("hook")
        # The lock is already held here: re-entering must not block.
        with pane_injection_lock(backend, "%3", 0.1):
            order.append("hook-reentered")

    original_send = backend.send_text

    def recording_send(pane_id: str, text: str) -> None:
        order.append("send")
        original_send(pane_id, text)

    backend.send_text = recording_send  # type: ignore[assignment]
    result = peer_routing.send_peer_message(_peer_task(), "codex", "peer:key", before_dispatch=hook)

    assert result.status == "completed"
    assert order == ["hook", "hook-reentered", "send"]
    assert len(backend.sent) == 1


def test_hook_failure_aborts_before_any_send(monkeypatch):
    backend = _RecordingBackend()
    peer_routing = _patch_peer(monkeypatch, backend)

    def hook() -> None:
        raise OSError("journal disk full")

    result = peer_routing.send_peer_message(_peer_task(), "codex", "peer:key", before_dispatch=hook)
    assert result.status == "failed"
    assert "nothing was sent" in result.reply
    assert backend.sent == []


def test_peer_prompt_still_uses_the_delivery_wrapper(monkeypatch):
    backend = _RecordingBackend()
    peer_routing = _patch_peer(monkeypatch, backend)
    peer_routing.send_peer_message(_peer_task(), "codex", "peer:key")
    [(pane, prompt)] = backend.sent
    assert pane == "%3"
    assert f"CCB_REQ_ID: {FIXTURE_LOCAL_ID}" in prompt
    assert "asynchronous peer message" in prompt


_CHILD_BARE = """
import sys, time
sys.path.insert(0, {lib!r})
from terminal import TmuxBackend

socket_name, pane, marker = sys.argv[1:4]
backend = TmuxBackend(socket_name=socket_name)
backend.send_text(pane, marker)   # no explicit lock: send_text itself must lock
"""


def _start_bare_send(tmp_path: Path, socket_name: str, pane: str, marker: str, *, disable: bool):
    script = tmp_path / "bare_child.py"
    script.write_text(_CHILD_BARE.format(lib=str(REPO_ROOT / "lib")), encoding="utf-8")
    env = dict(os.environ)
    env["CCB_TMUX_ENTER_DELAY"] = "1.0"
    env.pop("CCB_TMUX_SOCKET", None)
    env.pop("CCB_PANE_LOCK_DISABLE", None)
    if disable:
        env["CCB_PANE_LOCK_DISABLE"] = "1"
    return subprocess.Popen(
        [sys.executable, str(script), socket_name, pane, marker],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_backend_send_text_itself_holds_the_lock_for_the_whole_injection(tmux_server, tmp_path):
    """The lock must cover text, the Enter delay and Enter -- not just the paste."""
    socket_name, _log = tmux_server
    pane = _pane_id(socket_name)
    backend = TmuxBackend(socket_name=socket_name)

    child = _start_bare_send(tmp_path, socket_name, pane, "MARKER-BARE-1111", disable=False)
    try:
        time.sleep(0.35)  # the child is now inside its 1 s Enter delay
        with pytest.raises(PaneLockTimeout):
            with pane_injection_lock(backend, pane, 0.2):
                pytest.fail("send_text did not hold the pane lock during its injection")
    finally:
        _out, err = child.communicate(timeout=60)
        assert child.returncode == 0, err
    # Control: once the send is done, the same acquire succeeds.
    with pane_injection_lock(backend, pane, 2.0):
        pass


def test_with_the_switch_off_the_same_probe_finds_no_lock(tmux_server, tmp_path):
    """Negative control for the test above: no lock, so the probe gets in."""
    socket_name, _log = tmux_server
    pane = _pane_id(socket_name)
    backend = TmuxBackend(socket_name=socket_name)

    child = _start_bare_send(tmp_path, socket_name, pane, "MARKER-BARE-2222", disable=True)
    try:
        time.sleep(0.35)
        with pane_injection_lock(backend, pane, 0.2):
            pass  # obtained while the child is mid-send: the lock is off
    finally:
        _out, err = child.communicate(timeout=60)
        assert child.returncode == 0, err


def _probe_lock(backend, pane: str, budget: float = 0.15) -> bool:
    """True when a *different* thread can take the pane lock right now."""
    result: dict[str, bool] = {}

    def probe() -> None:
        try:
            with pane_injection_lock(backend, pane, budget):
                result["taken"] = True
        except PaneLockTimeout:
            result["taken"] = False

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(10.0)
    assert not thread.is_alive()
    return result["taken"]


def test_wezterm_send_text_wraps_its_injection_in_the_lock(monkeypatch):
    from terminal import WeztermBackend

    backend = WeztermBackend()
    observed: dict[str, object] = {}

    def fake_locked(self, pane_id: str, text: str) -> None:
        observed["text"] = text
        observed["lock_free"] = _probe_lock(backend, pane_id)

    monkeypatch.setattr(WeztermBackend, "_send_text_locked", fake_locked)
    backend.send_text("3", "hello wezterm")

    assert observed["text"] == "hello wezterm"
    assert observed["lock_free"] is False  # the wrapper held the lock


def test_the_probe_used_above_can_succeed_when_nothing_holds_the_lock(monkeypatch):
    """Positive control for the wezterm wrapper test."""
    from terminal import WeztermBackend

    monkeypatch.setenv("CCB_PANE_LOCK_DISABLE", "1")
    backend = WeztermBackend()
    assert _probe_lock(backend, "3") is True


_CHILD_HOLD = """
import sys, time
sys.path.insert(0, {lib!r})
from pane_lock import pane_injection_lock
from terminal import TmuxBackend

socket_name, target, ready_file = sys.argv[1:4]
backend = TmuxBackend(socket_name=socket_name)
with pane_injection_lock(backend, target, 10.0):
    open(ready_file, "w").close()
    time.sleep(2.0)
"""


def test_cross_process_lock_is_shared_between_a_session_name_and_a_pane_id(tmux_server, tmp_path):
    """The split the review found: child locks via the session name, parent via %N."""
    socket_name, _log = tmux_server
    pane = _pane_id(socket_name)
    script = tmp_path / "hold_by_session.py"
    script.write_text(_CHILD_HOLD.format(lib=str(REPO_ROOT / "lib")), encoding="utf-8")
    ready = tmp_path / "ready"
    env = {k: v for k, v in os.environ.items() if k != "CCB_TMUX_SOCKET"}
    child = subprocess.Popen(
        [sys.executable, str(script), socket_name, "ccb-lock", str(ready)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "the child never took the lock"

        backend = TmuxBackend(socket_name=socket_name)
        with pytest.raises(PaneLockTimeout):
            with pane_injection_lock(backend, pane, 0.2):
                pytest.fail("the same physical pane was locked twice under two different keys")
    finally:
        _out, err = child.communicate(timeout=60)
        assert child.returncode == 0, err
    # Control: after the child released it, the pane id target can take it.
    with pane_injection_lock(TmuxBackend(socket_name=socket_name), pane, 5.0):
        pass
