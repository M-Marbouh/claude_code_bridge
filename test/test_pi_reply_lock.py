# This repo puts lib/ on sys.path at runtime (test/conftest.py) and ships no pyright
# configuration, so the workspace-rooted analyzer cannot see these flat imports.
# pyright: reportMissingImports=false
"""The 2026-11-27 incident: a WAITING Pi reply must not lock other deliveries out of that Pi.

Incident record: one reply to a busy Pi held the per-target bridge lock for the whole readiness
wait (12 minutes), and every later reply to the same Pi failed in under a second with
`target provider is busy` (exit 1). The fix (v3 + A1) keeps the end-to-end serialisation and makes
a contended Pi delivery WAIT for the lock inside its OWN deadline, with one absolute paste-start
deadline carried from the bridge into askd.

`test/fixtures/pi_reply_lock/` is the TRACKED, sanitized copy of that incident's receipt and
failure lines (paths and project identities replaced, task ids / pane ids / pid / error text
kept). These tests read only that copy.

The canaries here use REAL holding processes and REAL `flock` on the real lock file for lock
semantics, and the REAL Pi gate / destination checks / pane lock / paste for delivery behaviour.
Only the terminal backend is a double, and only the daemon transport is replaced by a function
that runs the daemon's own delivery (`peer_routing.send_peer_message`) in this process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from askd.adapters.base import PeerDestination, ProviderRequest, QueuedTask
from peer_routing import _pi_peer_delivery_deadline, send_peer_message
from project_id import compute_ccb_project_id
from task_receipts import find_receipt, receipt_path, write_receipt
from test_pi_peer import (  # noqa: F401
    FakePiPane,
    _bridge_argv,
    _fast_gate,
    _peer_task,
    _project,
    _write_pi_caller_receipt,
)

ROOT = Path(__file__).resolve().parents[1]
INCIDENT = ROOT / "test" / "fixtures" / "pi_reply_lock"
INCIDENT_TASK = "20261127-120115-988-1358137"
INCIDENT_RECEIPT = INCIDENT / "receipt.json"
INCIDENT_FAILURE = INCIDENT / "failure.txt"

#: The incident's project hash prefix -- the lock every failing attempt met in the log.
INCIDENT_HASH = "c9bbfe2f"


@pytest.fixture
def use_pane(bridge, monkeypatch):
    """Point every backend factory -- including the loaded script's own binding -- at one pane.

    `bin/ccb-bridge-ask` binds `terminal.get_backend_for_session` at import time and this module
    loads it before a pane exists, so its own binding is patched explicitly.
    """

    def _use(pane: FakePiPane) -> None:
        for target in ("terminal", "pane_registry", "askd.adapters.pi"):
            monkeypatch.setattr(f"{target}.get_backend_for_session", lambda _data: pane)
        monkeypatch.setattr(bridge, "get_backend_for_session", lambda _data: pane)

    return _use


# --------------------------------------------------------------------------- #
# Real processes on the real lock
# --------------------------------------------------------------------------- #

#: Holds the bridge lock from another OS process until it is released. Loads the module under
#: test from a path, so the very same harness can run against the pre-fix script.
_LOCK_HOLDER = """
import importlib.machinery
import importlib.util
import sys
import time

path, target_hash, provider, hold_s, lib_dir = (
    sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), sys.argv[5],
)
# The script under test imports its own repository's `lib/`; a copy elsewhere has no sibling.
sys.path.insert(0, lib_dir)
loader = importlib.machinery.SourceFileLoader("lock_holder_bridge", path)
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
handle, fcntl = module._acquire_lock(target_hash, provider)
print("held", flush=True)
time.sleep(hold_s)
fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
handle.close()
print("released", flush=True)
"""


def _load_bridge_at(path: Path):
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader(
        "ccb_bridge_reply_lock_test", str(path)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def bridge(tmp_path: Path, monkeypatch):
    """The bridge under test, with its task dir (receipts AND locks) isolated from the real /tmp.

    `CCB_BRIDGE_UNDER_TEST` selects another copy of the script (used for the negative control);
    the default is this repository's own `bin/ccb-bridge-ask`.
    """
    module = _load_bridge_at(_bridge_path())
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path))
    return module


def _start_holder(
    tmp_path: Path,
    target_hash: str,
    provider: str,
    hold_s: float,
    bridge_path: Path | None = None,
) -> subprocess.Popen:
    script = tmp_path / "lock_holder.py"
    script.write_text(_LOCK_HOLDER, encoding="utf-8")
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    proc = subprocess.Popen(
        [
            sys.executable,
            str(script),
            str(bridge_path or _bridge_path()),
            target_hash,
            provider,
            str(hold_s),
            str(ROOT / "lib"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    line = str(proc.stdout.readline() if proc.stdout else "").strip()
    if line != "held":
        stderr = proc.stderr.read() if proc.stderr else ""
        proc.kill()
        pytest.fail(f"the lock holder did not start: {line!r} / {stderr}")
    return proc


def _finish_holder(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    finally:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        proc.wait(timeout=10)


def _bridge_path() -> Path:
    override = (os.environ.get("CCB_BRIDGE_UNDER_TEST") or "").strip()
    return Path(override) if override else ROOT / "bin" / "ccb-bridge-ask"


def test_a_contended_pi_delivery_waits_for_the_real_lock_and_then_acquires_it(
    tmp_path: Path, bridge
) -> None:
    """F1: the incident's own lock name, held by a REAL process, makes the next Pi call wait."""
    holder = _start_holder(tmp_path, INCIDENT_HASH, "pi", 1.5)
    try:
        started = time.monotonic()
        handle, fcntl = bridge._acquire_lock(
            INCIDENT_HASH, "pi", wait_until=time.monotonic() + 20.0
        )
        waited = time.monotonic() - started
        try:
            assert waited >= 1.0, (
                f"it acquired the contended lock without waiting ({waited:.2f}s)"
            )
            assert waited < 10.0
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
    finally:
        _finish_holder(holder)


def test_a_contended_pi_delivery_refuses_when_its_own_deadline_expires(
    tmp_path: Path, bridge
) -> None:
    """F1: the wait is bounded by the caller's own deadline, with nothing sent."""
    holder = _start_holder(tmp_path, INCIDENT_HASH, "pi", 5.0)
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError) as refused:
            bridge._acquire_lock(INCIDENT_HASH, "pi", wait_until=time.monotonic() + 0.4)
        elapsed = time.monotonic() - started
        assert (
            f"another delivery to {INCIDENT_HASH} pi is still waiting for that Pi; nothing was sent"
            in str(refused.value)
        )
        assert elapsed < 3.0
    finally:
        _finish_holder(holder)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_contended_claude_or_codex_delivery_still_refuses_instantly(
    tmp_path: Path, bridge, provider: str
) -> None:
    """F2: only the wording changed; a Claude/Codex hold is still an instant refusal."""
    holder = _start_holder(tmp_path, "abcd1234ef", provider, 5.0)
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError) as refused:
            bridge._acquire_lock("abcd1234ef", provider)
        elapsed = time.monotonic() - started
        assert (
            f"another delivery to abcd1234 {provider} is in progress; nothing was sent"
            in str(refused.value)
        )
        assert elapsed < 1.0
        assert "busy" not in str(refused.value)
    finally:
        _finish_holder(holder)


def test_the_negative_control_a_pre_fix_bridge_refuses_the_second_delivery(
    tmp_path: Path, bridge
) -> None:
    """The control for the canaries below: the PRE-FIX script refuses, and cannot wait.

    Run it with `CCB_BRIDGE_UNDER_TEST=<a pre-fix copy of bin/ccb-bridge-ask>`; the fixed script
    has no instant-refusal path for Pi, so this only asserts the OLD behaviour when asked to.
    """
    if not (os.environ.get("CCB_BRIDGE_UNDER_TEST") or "").strip():
        pytest.skip("set CCB_BRIDGE_UNDER_TEST to a pre-fix bin/ccb-bridge-ask copy")

    holder = _start_holder(tmp_path, INCIDENT_HASH, "pi", 5.0)
    try:
        with pytest.raises(RuntimeError) as refused:
            # The pre-fix `_acquire_lock` takes neither `wait_until` nor any other escape.
            bridge._acquire_lock(INCIDENT_HASH, "pi")
        assert "target provider is busy: c9bbfe2f pi" in str(refused.value)
    finally:
        _finish_holder(holder)


# --------------------------------------------------------------------------- #
# The incident's own record, beside synthetic receipts
# --------------------------------------------------------------------------- #


def test_the_incident_record_is_the_receipt_and_the_failure_this_fix_answers(
    tmp_path: Path, bridge, monkeypatch
) -> None:
    """From the REAL record: its project hash names the lock, and a second delivery now waits."""
    receipt = json.loads(INCIDENT_RECEIPT.read_text(encoding="utf-8"))
    failing_line = INCIDENT_FAILURE.read_text(encoding="utf-8")
    assert "target provider is busy: c9bbfe2f pi" in failing_line
    assert receipt["ccb_project_id"][:8] == INCIDENT_HASH
    assert receipt["peer_provider"] == "pi" and receipt["caller"] == "pi"
    assert receipt["caller_pane_pid"] == "23253"

    # The real receipt, beside a synthetic one, in an isolated receipt store. Its own output
    # paths are redirected so the incident's files are never touched.
    store = tmp_path / "receipts"
    real = dict(receipt)
    real["status_file"] = str(tmp_path / "real.status")
    real["log_file"] = str(tmp_path / "real.log")
    real["peer_reply_file"] = str(tmp_path / "real.reply")
    write_receipt(receipt_path("peer-pi", receipt["task_id"], root=store), real)
    synthetic = dict(
        receipt, task_id="20261127-999999-999-0000000", ccb_project_id="feedbeef" * 8
    )
    synthetic["status_file"] = str(tmp_path / "synthetic.status")
    synthetic["log_file"] = str(tmp_path / "synthetic.log")
    synthetic["peer_reply_file"] = str(tmp_path / "synthetic.reply")
    synthetic["caller_pane_pid"] = "1"
    write_receipt(receipt_path("peer-pi", synthetic["task_id"], root=store), synthetic)
    monkeypatch.setattr(
        bridge, "find_receipt", lambda task_id: find_receipt(task_id, root=store)
    )

    matched = bridge._correlated_reply_receipt(
        receipt["task_id"], receipt["work_dir"], "pi"
    )
    assert matched is not None and matched["caller_pane_pid"] == "23253"
    # A correlated reply is pinned to its own project and provider, as before.
    with pytest.raises(ValueError):
        bridge._correlated_reply_receipt(receipt["task_id"], str(tmp_path), "pi")
    with pytest.raises(ValueError):
        bridge._correlated_reply_receipt(
            receipt["task_id"], receipt["work_dir"], "claude"
        )

    holder = _start_holder(tmp_path, INCIDENT_HASH, "pi", 1.5)
    try:
        started = time.monotonic()
        handle, fcntl = bridge._acquire_lock(
            INCIDENT_HASH, "pi", wait_until=time.monotonic() + 20.0
        )
        waited = time.monotonic() - started
        try:
            assert waited >= 1.0
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
    finally:
        _finish_holder(holder)


class _DeadPaneBackend:
    """The incident's return pane after the fact: no process there, and nothing can be pasted."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def is_alive(self, _pane_id: str) -> bool:
        return False

    def pane_matches_cwd_strict(self, _pane_id: str, _work_dir: str) -> bool:
        return False

    def find_pane_by_title_marker(self, _marker: str, _work_dir: str) -> str:
        return ""

    def send_text(self, pane_id: str, prompt: str) -> None:
        self.sent.append((pane_id, prompt))


def test_the_incident_reply_still_reports_recoverable_when_the_return_pane_is_gone(
    tmp_path: Path, bridge, monkeypatch, capsys
) -> None:
    """The real receipt, delivered for real: its return pane is gone, so nothing is pasted."""
    receipt = json.loads(INCIDENT_RECEIPT.read_text(encoding="utf-8"))
    store = tmp_path / "receipts"
    saved = dict(receipt)
    saved["status_file"] = str(tmp_path / "saved.status")
    saved["log_file"] = str(tmp_path / "saved.log")
    saved["peer_reply_file"] = str(tmp_path / "saved.reply")
    write_receipt(receipt_path("peer-pi", receipt["task_id"], root=store), saved)
    monkeypatch.setattr(
        bridge, "find_receipt", lambda task_id: find_receipt(task_id, root=store)
    )
    monkeypatch.setattr(bridge, "_load_targets", list)
    backend = _DeadPaneBackend()
    monkeypatch.setattr(bridge, "get_backend_for_session", lambda _session: backend)

    assert (
        bridge.main(
            [
                "--target",
                receipt["work_dir"],
                "--provider",
                "pi",
                "--reply-to",
                receipt["task_id"],
                "--caller",
                "pi",
                "--sender-work-dir",
                receipt["work_dir"],
                "--intent",
                "background",
                "the answer",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert f"[RECOVERABLE] Reply saved for task {receipt['task_id']}" in captured.err
    assert f"pend {receipt['task_id']}" in captured.err
    assert backend.sent == []
    assert Path(saved["peer_reply_file"]).read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------- #
# The canaries: A waits, B queues, nothing pastes while A is pending
# --------------------------------------------------------------------------- #


def _pi_reply_target(
    project: Path, pane: FakePiPane, binding: dict[str, Any]
) -> dict[str, Any]:
    return {
        "index": 1,
        "work_dir": str(project),
        "ccb_project_id": compute_ccb_project_id(project),
        "terminal": pane.terminal,
        "providers": {
            "pi": {
                "alive": True,
                "mounted": True,
                "pane_id": pane.pane_id,
                "pane_title_marker": pane.pane_title_marker,
                "terminal": pane.terminal,
                "launch_id": binding["session_id"],
            }
        },
        "peer_providers": ["pi"],
    }


def _daemon_double(pane: FakePiPane, deliveries: list[dict[str, Any]]):
    """The daemon branch's transport, running askd's OWN delivery in this process.

    Only the UNIX-socket hop is replaced: `send_peer_message` below is the real gate, the real
    destination revalidation and the real paste, and the request it builds is the one the daemon
    would have built from this envelope -- including the absolute `paste_deadline` (A1).
    """

    def send(
        target: dict[str, Any],
        message: str,
        timeout: float,
        caller_pane_id: str,
        caller_terminal: str,
        sender_work_dir: str,
        intent: str,
        peer_task_id: str,
        reply_to: str,
        provider: str = "claude",
        caller: str = "claude",
        foreground: bool = False,
        to_role: str = "",
        peer_pane_id: str = "",
        peer_project_id: str = "",
        paste_deadline: float | None = None,
    ):
        deliveries.append(
            {
                "message": message,
                "timeout": timeout,
                "paste_deadline": paste_deadline,
                "peer_task_id": peer_task_id,
                "caller": caller,
            }
        )
        request = ProviderRequest(
            client_id="daemon-double",
            work_dir=str(target.get("work_dir") or ""),
            timeout_s=timeout,
            quiet=True,
            message=message,
            caller=caller,
            delivery_only=True,
            peer_delivery=True,
            peer_destination=PeerDestination(**dict(target["peer_destination"])),
            kind="reply",
            reply_to=reply_to,
            paste_deadline_s=paste_deadline,
        )
        task = QueuedTask(
            request=request,
            created_ms=int(time.time() * 1000),
            req_id=peer_task_id or "peer-double",
            done_event=threading.Event(),
        )
        result = send_peer_message(task, "pi", "pi:daemon-double")
        return result.exit_code, result.reply, dict(result.extra or {})

    return send


class _BridgeRun:
    """One bridge process, run in this thread, whose exit code and output a test can inspect."""

    def __init__(self, module, argv: list[str]) -> None:
        self.module = module
        self.argv = argv
        self.exit_code: int | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.exit_code = self.module.main(self.argv)
        except Exception as exc:  # noqa: BLE001 - a canary must report, not hang the suite
            self.error = exc

    def start(self) -> _BridgeRun:
        self.thread.start()
        return self

    def join(self, timeout: float = 30.0) -> None:
        self.thread.join(timeout=timeout)


def _wait_for_send_entry(
    deliveries: list[dict[str, Any]], timeout_s: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if deliveries:
            return
        time.sleep(0.01)
    pytest.fail("the first delivery never entered the daemon branch")


def _two_replies(tmp_path: Path, pane: FakePiPane, project: Path) -> tuple[str, str]:
    for task_id in ("task-reply-a", "task-reply-b"):
        _write_pi_caller_receipt(project, pane, task_id=task_id)
    return "task-reply-a", "task-reply-b"


def _argv_for(project: Path, task_id: str, *, timeout: float, text: str) -> list[str]:
    argv = _bridge_argv(project, task_id)
    argv[argv.index("--intent") : argv.index("--intent")] = ["--timeout", str(timeout)]
    return [*argv[:-1], text]


def _wait_until_lock_is_held(bridge, target_hash: str, timeout_s: float = 10.0) -> None:
    """Block until ANOTHER caller holds the bridge lock (a real `flock`, real contention)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            handle, fcntl = bridge._acquire_lock(target_hash, "pi")
        except RuntimeError:
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        time.sleep(0.01)
    pytest.fail("no delivery ever took the bridge lock")


@pytest.fixture
def gate_entries(monkeypatch):
    """Every entry into the Pi readiness gate: the LAST step before either branch can submit.

    Both branches import `wait_until_pi_injectable` inside the delivering function, so one patch
    sees the bridge's direct branch and askd's worker alike. A delivery that is still waiting for
    the bridge lock never appears here at all -- which is exactly what the canaries assert.
    """
    import pi_comm

    real = pi_comm.wait_until_pi_injectable
    calls: list[str] = []

    def recording(*args, **kwargs):
        calls.append(str(kwargs.get("req_id") or ""))
        return real(*args, **kwargs)

    monkeypatch.setattr(pi_comm, "wait_until_pi_injectable", recording)
    return calls


def _assert_queued_behind_a_pending_reply(
    run: _BridgeRun, pane: FakePiPane, gate_calls: list[str]
) -> None:
    """B must still be waiting after 2 s: no gate entry, no paste, no exit."""
    run.thread.join(timeout=2.0)
    assert run.thread.is_alive(), (
        f"the queued delivery finished early (exit={run.exit_code})"
    )
    assert run.error is None
    assert run.exit_code is None
    assert len(gate_calls) == 1, f"a queued delivery reached the gate: {gate_calls}"
    assert pane.sent == []


def test_f1_daemon_routed_replies_queue_and_paste_in_order(
    tmp_path: Path, use_pane, bridge, monkeypatch, gate_entries
) -> None:
    """The incident's path: A waits for a busy Pi inside the daemon branch, B queues behind it."""
    pane = FakePiPane(tmp_path, idle=False)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    monkeypatch.setattr(
        bridge, "_load_targets", lambda: [_pi_reply_target(project, pane, binding)]
    )
    deliveries: list[dict[str, Any]] = []
    monkeypatch.setattr(bridge, "_send_to_daemon", _daemon_double(pane, deliveries))
    task_a, task_b = _two_replies(tmp_path, pane, project)

    first = _BridgeRun(
        bridge, _argv_for(project, task_a, timeout=30.0, text="first answer")
    ).start()
    try:
        # The first reply is inside the daemon branch, holding the bridge lock while Pi is busy.
        _wait_for_send_entry(deliveries)
        second = _BridgeRun(
            bridge, _argv_for(project, task_b, timeout=30.0, text="second answer")
        ).start()
        _assert_queued_behind_a_pending_reply(second, pane, gate_entries)
        # Submission is the daemon envelope itself: only A's has reached the transport.
        assert len(deliveries) == 1, deliveries

        pane.set_state(idle=True)
        first.join()
        second.join()

        assert first.error is None and second.error is None
        assert (first.exit_code, second.exit_code) == (0, 0)
        assert len(pane.sent) == 2, pane.sent
        assert "first answer" in pane.sent[0][1]
        assert "second answer" in pane.sent[1][1]
        assert [pane_id for pane_id, _ in pane.sent] == [pane.pane_id, pane.pane_id]
        assert pane.ready_at_paste == ["ready", "ready"]
        assert all("Do not produce a local CCB_DONE" in text for _, text in pane.sent)
        assert len(gate_entries) == 2, gate_entries
        # Each stayed inside its own deadline, and B was told what was left of its budget.
        assert deliveries[1]["timeout"] <= 30.0
        assert 0 < deliveries[1]["timeout"] <= 29.5
        assert deliveries[1]["paste_deadline"] > time.time() - 30.0
    finally:
        pane.stop()


def test_f1_direct_replies_queue_and_paste_in_order(
    tmp_path: Path, use_pane, bridge, monkeypatch, gate_entries
) -> None:
    """The same canary on the direct branch (no daemon reachable)."""
    pane = FakePiPane(tmp_path, idle=False)
    project, _binding = _project(tmp_path, pane)
    use_pane(pane)
    monkeypatch.setattr(bridge, "_load_targets", list)
    task_a, task_b = _two_replies(tmp_path, pane, project)

    first = _BridgeRun(
        bridge, _argv_for(project, task_a, timeout=30.0, text="first answer")
    ).start()
    try:
        # A holds the bridge lock while it waits in the gate; only then does B start.
        _wait_until_lock_is_held(bridge, compute_ccb_project_id(project)[:8])
        second = _BridgeRun(
            bridge, _argv_for(project, task_b, timeout=30.0, text="second answer")
        ).start()
        _assert_queued_behind_a_pending_reply(second, pane, gate_entries)

        pane.set_state(idle=True)
        first.join()
        second.join()

        assert first.error is None and second.error is None
        assert (first.exit_code, second.exit_code) == (0, 0)
        assert len(pane.sent) == 2, pane.sent
        assert "first answer" in pane.sent[0][1]
        assert "second answer" in pane.sent[1][1]
        assert pane.ready_at_paste == ["ready", "ready"]
        assert len(gate_entries) == 2, gate_entries
    finally:
        pane.stop()


def test_f1_a_queued_reply_whose_deadline_expires_while_waiting_sends_nothing(
    tmp_path: Path, use_pane, bridge, monkeypatch, capsys
) -> None:
    """B's own deadline, shorter than A's hold, ends the wait with nothing sent."""
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    monkeypatch.setattr(
        bridge, "_load_targets", lambda: [_pi_reply_target(project, pane, binding)]
    )
    deliveries: list[dict[str, Any]] = []
    monkeypatch.setattr(bridge, "_send_to_daemon", _daemon_double(pane, deliveries))
    task_b = _two_replies(tmp_path, pane, project)[1]
    target_hash = compute_ccb_project_id(project)[:8]

    holder = _start_holder(tmp_path, target_hash, "pi", 5.0)
    try:
        started = time.monotonic()
        assert (
            bridge.main(_argv_for(project, task_b, timeout=1.0, text="too late")) == 1
        )
        elapsed = time.monotonic() - started
    finally:
        _finish_holder(holder)

    captured = capsys.readouterr()
    assert (
        f"another delivery to {target_hash} pi is still waiting for that Pi; nothing was sent"
        in captured.err
    )
    assert f"[RECOVERABLE] Reply saved for task {task_b}" in captured.err
    assert elapsed <= 1.5, elapsed
    assert deliveries == []
    assert pane.sent == []
    pane.stop()


def test_f1_the_budget_the_lock_wait_spends_is_never_handed_to_the_delivery(
    tmp_path: Path, use_pane, bridge, monkeypatch
) -> None:
    """Budget accounting: B acquires after T s, so its delivery gets (timeout - T), not timeout."""
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    monkeypatch.setattr(
        bridge, "_load_targets", lambda: [_pi_reply_target(project, pane, binding)]
    )
    deliveries: list[dict[str, Any]] = []
    monkeypatch.setattr(bridge, "_send_to_daemon", _daemon_double(pane, deliveries))
    task_b = _two_replies(tmp_path, pane, project)[1]
    target_hash = compute_ccb_project_id(project)[:8]

    hold_s = 1.2
    holder = _start_holder(tmp_path, target_hash, "pi", hold_s)
    try:
        assert (
            bridge.main(_argv_for(project, task_b, timeout=10.0, text="queued answer"))
            == 0
        )
    finally:
        _finish_holder(holder)

    assert len(deliveries) == 1, deliveries
    handed = deliveries[0]["timeout"]
    assert handed < 10.0 - 0.5, handed
    assert handed >= 10.0 - hold_s - 1.0, handed
    assert deliveries[0]["paste_deadline"] is not None
    assert deliveries[0]["paste_deadline"] <= time.time() + handed + 0.5
    assert len(pane.sent) == 1
    pane.stop()


def test_a1_discovery_that_exhausts_the_budget_never_takes_the_lock(
    tmp_path: Path, use_pane, bridge, monkeypatch, capsys
) -> None:
    """The deadline starts at bridge start, so slow discovery spends it: no lock, no send."""
    pane = FakePiPane(tmp_path)
    project, _binding = _project(tmp_path, pane)
    use_pane(pane)
    monkeypatch.setattr(bridge, "_load_targets", list)
    task_id = _two_replies(tmp_path, pane, project)[1]

    real_lookup = bridge._correlated_reply_receipt

    def slow_discovery(*args, **kwargs):
        time.sleep(0.45)  # receipt handling and target discovery are real work
        return real_lookup(*args, **kwargs)

    monkeypatch.setattr(bridge, "_correlated_reply_receipt", slow_discovery)
    lock_attempts: list[str] = []
    real_acquire = bridge._acquire_lock

    def recording_acquire(target_hash: str, provider: str, **kwargs):
        lock_attempts.append(provider)
        return real_acquire(target_hash, provider, **kwargs)

    monkeypatch.setattr(bridge, "_acquire_lock", recording_acquire)

    assert bridge.main(_argv_for(project, task_id, timeout=0.3, text="too late")) == 1
    captured = capsys.readouterr()
    assert lock_attempts == []
    assert pane.sent == []
    assert "reached its paste deadline" in captured.err
    assert f"[RECOVERABLE] Reply saved for task {task_id}" in captured.err
    assert f"pend {task_id}" in captured.err

    # Positive control: the same slow discovery inside a budget that survives it takes the lock
    # and delivers once, so this test's signal could have failed.
    assert bridge.main(_argv_for(project, task_id, timeout=5.0, text="in time")) == 0
    assert lock_attempts == ["pi"]
    assert len(pane.sent) == 1
    pane.stop()


# --------------------------------------------------------------------------- #
# A1: the absolute paste-start deadline survives the preflight
# --------------------------------------------------------------------------- #


def test_a1_a_preflight_that_exhausts_the_budget_submits_nothing(
    tmp_path: Path, use_pane, bridge, monkeypatch
) -> None:
    """Daemon branch: the deadline is re-checked immediately before submission."""
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    monkeypatch.setattr(
        bridge, "_load_targets", lambda: [_pi_reply_target(project, pane, binding)]
    )
    task_b = _two_replies(tmp_path, pane, project)[1]

    monkeypatch.setattr(
        bridge, "find_running_state_file", lambda *_a, **_k: tmp_path / "askd.json"
    )
    monkeypatch.setattr(
        bridge.askd_rpc, "read_state", lambda _path: {"token": "daemon-token"}
    )
    submitted: list[dict[str, Any]] = []

    def record(state, request, **_kwargs):
        submitted.append(dict(request))
        return {"exit_code": 0, "reply": "ok", "meta": {"confirmation": "sent"}}

    monkeypatch.setattr(bridge.askd_rpc, "request_daemon", record)
    monkeypatch.setattr(bridge, "_bridge_gate_required", lambda **_kwargs: True)

    def slow_preflight(_state, _request):
        time.sleep(0.4)  # daemon discovery and a policy probe cost real time
        return ""

    monkeypatch.setattr(bridge, "_capability_preflight", slow_preflight)

    started = time.monotonic()
    assert bridge.main(_argv_for(project, task_b, timeout=0.3, text="too late")) == 1
    assert time.monotonic() - started <= 2.0
    assert submitted == [], submitted
    assert pane.sent == []

    # Positive control: the same path with a budget the preflight cannot exhaust submits ONCE,
    # carrying the absolute deadline, so this test's signal could have failed.
    assert bridge.main(_argv_for(project, task_b, timeout=5.0, text="in time")) == 0
    assert len(submitted) == 1, submitted
    assert submitted[0]["paste_deadline"] > time.time()
    assert submitted[0]["timeout_s"] <= 5.0
    # Submitting is all the daemon branch does; this stub records it instead of delivering.
    assert pane.sent == []
    pane.stop()


def test_a1_a_direct_reply_that_spends_its_budget_after_the_lock_pastes_nothing(
    tmp_path: Path, use_pane, bridge, monkeypatch
) -> None:
    """Direct branch: the deadline is re-checked right after acquisition, before the gate entry."""
    pane = FakePiPane(tmp_path)
    project, _binding = _project(tmp_path, pane)
    use_pane(pane)
    monkeypatch.setattr(bridge, "_load_targets", list)
    task_b = _two_replies(tmp_path, pane, project)[1]

    real_acquire = bridge._acquire_lock

    def slow_acquire(target_hash: str, provider: str, **kwargs):
        handle, fcntl = real_acquire(target_hash, provider, **kwargs)
        time.sleep(0.4)  # acquiring took real time; the budget is fixed at bridge start
        return handle, fcntl

    monkeypatch.setattr(bridge, "_acquire_lock", slow_acquire)

    assert bridge.main(_argv_for(project, task_b, timeout=0.3, text="too late")) == 1
    assert pane.sent == []

    # Positive control: the same slow acquisition inside a budget that survives it pastes once.
    assert bridge.main(_argv_for(project, task_b, timeout=5.0, text="in time")) == 0
    assert len(pane.sent) == 1
    pane.stop()


def test_a1_askd_takes_the_earlier_of_its_relative_budget_and_the_absolute_deadline(
    tmp_path: Path, use_pane
) -> None:
    """The worker's own deadline helper: an absolute one in the past wins, whatever the timeout."""
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    try:
        request = _peer_task(
            project,
            pane,
            launch_id=binding["session_id"],
            timeout_s=30.0,
            paste_deadline_s=time.time() + 0.2,
        )
        deadline = _pi_peer_delivery_deadline(request)
        assert deadline is not None
        assert deadline - time.monotonic() <= 0.5

        relative_only = _peer_task(
            project, pane, launch_id=binding["session_id"], timeout_s=30.0
        )
        wider = _pi_peer_delivery_deadline(relative_only)
        assert wider is not None and wider - time.monotonic() > 25.0
    finally:
        pane.stop()


def test_a1_askd_refuses_to_start_a_paste_at_the_absolute_cutoff(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    """A relative budget that would outlive the absolute deadline is cut at the absolute one."""
    pane = FakePiPane(tmp_path, idle=False)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    task = _peer_task(
        project,
        pane,
        launch_id=binding["session_id"],
        timeout_s=30.0,
        paste_deadline_s=time.time() + 0.25,
    )
    # Pi frees up long AFTER the absolute cutoff: only the absolute deadline can refuse this.
    threading.Timer(1.2, lambda: pane.set_state(idle=True)).start()
    try:
        started = time.monotonic()
        result = send_peer_message(task, "pi", "pi:peer")
        elapsed = time.monotonic() - started
        assert result.exit_code == 1
        assert "timed out before Pi was ready" in result.reply
        assert elapsed < 1.0, elapsed
        assert pane.sent == []
    finally:
        pane.stop()


@pytest.mark.parametrize("value", [0, 0.0, "0", float("nan"), float("inf"), True])
def test_a1_the_daemon_ingress_refuses_an_unusable_absolute_deadline(
    tmp_path: Path, monkeypatch, value
) -> None:
    """Zero/NaN/inf/bool are refused at the real ingress, before anything is queued."""
    from test_pi_peer import (
        _DAEMON_TOKEN,
        _OK_RESOLUTION,
        _daemon_with_pool,
        _RecordingPool,
    )

    pool = _RecordingPool()
    daemon = _daemon_with_pool(pool)
    monkeypatch.setattr(
        "peer_routing.revalidate_peer_destination", lambda _destination: _OK_RESOLUTION
    )

    response = daemon._handle_request(
        {
            "type": "ask.request",
            "v": 1,
            "id": "client-deadline",
            "token": _DAEMON_TOKEN,
            "provider": "pi",
            "work_dir": str(tmp_path),
            "timeout_s": 5.0,
            "message": "hello",
            "caller": "claude",
            "delivery_only": True,
            "peer_delivery": True,
            "paste_deadline": value,
            "peer_destination": {
                "pane_id": "%12",
                "terminal": "tmux",
                "work_dir": str(tmp_path),
                "ccb_project_id": "abcd1234",
                "pane_title_marker": "Pi-marker",
            },
        }
    )

    assert response["exit_code"] == 1
    assert response["accepted"] is False
    assert "paste deadline" in response["reply"]
    assert pool.submitted == []


def test_a1_the_daemon_accepts_an_absolute_deadline_beyond_the_relative_wait_limit(
    tmp_path: Path, monkeypatch
) -> None:
    """`threading.TIMEOUT_MAX` bounds the relative DURATION, never an epoch instant."""
    import threading

    from test_pi_peer import (
        _DAEMON_TOKEN,
        _OK_RESOLUTION,
        _daemon_with_pool,
        _RecordingPool,
    )

    pool = _RecordingPool()
    daemon = _daemon_with_pool(pool)
    monkeypatch.setattr(
        "peer_routing.revalidate_peer_destination", lambda _destination: _OK_RESOLUTION
    )
    # Just under the duration limit (the daemon's own allowance is 5 s)...
    relative = threading.TIMEOUT_MAX - 10.0
    # ...and its matching instant, which is an epoch and legitimately larger than that limit.
    absolute = time.time() + relative
    assert absolute > threading.TIMEOUT_MAX

    response = daemon._handle_request(
        {
            "type": "ask.request",
            "v": 1,
            "id": "client-deadline-epoch",
            "token": _DAEMON_TOKEN,
            "provider": "pi",
            "work_dir": str(tmp_path),
            "timeout_s": relative,
            "message": "hello",
            "caller": "claude",
            "delivery_only": True,
            "peer_delivery": True,
            "paste_deadline": absolute,
            "peer_destination": {
                "pane_id": "%12",
                "terminal": "tmux",
                "work_dir": str(tmp_path),
                "ccb_project_id": "abcd1234",
                "pane_title_marker": "Pi-marker",
            },
        }
    )

    assert response.get("accepted") is not False
    assert [provider for provider, _ in pool.submitted] == ["pi"]
    assert pool.submitted[0][1].paste_deadline_s == absolute


def test_a1_the_daemon_ingress_carries_a_usable_absolute_deadline_to_the_worker(
    tmp_path: Path, monkeypatch
) -> None:
    from test_pi_peer import (
        _DAEMON_TOKEN,
        _OK_RESOLUTION,
        _daemon_with_pool,
        _RecordingPool,
    )

    pool = _RecordingPool()
    daemon = _daemon_with_pool(pool)
    monkeypatch.setattr(
        "peer_routing.revalidate_peer_destination", lambda _destination: _OK_RESOLUTION
    )
    absolute = time.time() + 42.0

    response = daemon._handle_request(
        {
            "type": "ask.request",
            "v": 1,
            "id": "client-deadline-ok",
            "token": _DAEMON_TOKEN,
            "provider": "pi",
            "work_dir": str(tmp_path),
            "timeout_s": 5.0,
            "message": "hello",
            "caller": "claude",
            "delivery_only": True,
            "peer_delivery": True,
            "paste_deadline": absolute,
            "peer_destination": {
                "pane_id": "%12",
                "terminal": "tmux",
                "work_dir": str(tmp_path),
                "ccb_project_id": "abcd1234",
                "pane_title_marker": "Pi-marker",
            },
        }
    )

    assert response.get("accepted") is not False
    assert [provider for provider, _ in pool.submitted] == ["pi"]
    assert pool.submitted[0][1].paste_deadline_s == absolute
