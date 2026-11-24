# This repo puts lib/ on sys.path at runtime (test/conftest.py) and ships no pyright
# configuration, so the workspace-rooted analyzer cannot see these flat imports.
# pyright: reportMissingImports=false
"""Pi peer messaging, PHASE 1: the shared injection gate, the destination binding, and one paste.

The unit under test is the REAL gate, the REAL destination verification and the REAL
`peer_routing.send_peer_message`. Only the terminal backend is a double, and it records what a real
pane would receive; nothing here stubs readiness, sends, or the decision to send.

Every readiness verdict comes from a state file written the way CCB's in-Pi extension writes it
(`pi-state.json`: launch token, runtime instance, pid, mode, idle / editor_empty / prompt_open, seq,
`updated_at` in epoch MILLISECONDS), and the state file is rewritten on a timer exactly as Pi does,
so `confirm_pi_ready`'s two-read rule is exercised rather than simulated.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from askd.adapters.base import PeerDestination, ProviderRequest, QueuedTask
from ccb_protocol import wrap_codex_delivery_prompt, wrap_pi_delivery_prompt
from laskd_protocol import wrap_claude_delivery_prompt
from peer_routing import send_peer_message
from pi_comm import (
    PiGatePolicy,
    PiState,
    confirm_pi_ready,
    peer_pi_destination_binding,
    pi_incarnation_problem,
    pi_incarnation_witness,
    read_pi_state,
    wait_until_pi_injectable,
)
from project_id import compute_ccb_project_id

SESSION_ID = "2c8ab895-d0ab-49c6-86c1-0f7ff9fb6e8e"

# Assembled, never a literal: these stand in for credentials and must not read as one.
_TOKEN_ALPHA = "launch" + "-token-alpha"
_TOKEN_BETA = "launch" + "-token-beta"
_DAEMON_TOKEN = "daemon" + "-token"
#: The real confirmation, captured before any test replaces the module attribute.
pi_confirm = confirm_pi_ready
# Pi rewrites this often; a test writer must beat `PI_STATE_MAX_AGE_S` (3 s) easily.
WRITE_INTERVAL_S = 0.02


class FakePiPane:
    """A fake Pi pane whose readiness is a real `pi-state.json`, rewritten on a timer.

    The backend half of the double implements exactly what CCB asks a terminal for (liveness, cwd
    match, marker, pane pid, paste). `send_text` records the readiness Pi reported at the instant of
    the paste, which is how "never pasted while busy" is asserted rather than assumed.
    """

    def __init__(
        self,
        root: Path,
        *,
        terminal: str = "tmux",
        pane_id: str = "%12",
        marker: str = "Pi-marker",
        idle: bool = True,
        editor_empty: bool = True,
        prompt_open: bool = False,
        pid: int | None = None,
        writer: bool = True,
        turn_s: float = 0.0,
        mode: str = "tui",
    ):
        self.terminal = terminal
        self.pane_id = pane_id
        self.pane_title_marker = marker
        self.root = root
        self.state_path = root / "rt" / "pi-state.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.launch_token = _TOKEN_ALPHA
        self.runtime_instance_id = "43518-mwuktpns-3h8umdqf"
        self.pid = os.getpid() if pid is None else pid
        self.pane_process = "9001"
        self.sent: list[tuple[str, str]] = []
        self.ready_at_paste: list[str] = []
        #: When set, a paste BLOCKS before doing anything until this Event is released -- which
        #: keeps the caller inside the pane-injection lock for as long as a test wants.
        self.paste_gate: threading.Event | None = None
        #: Set by the first paste, so a test can wait until a sender is inside the lock.
        self.paste_started = threading.Event()
        self.paste_windows: list[tuple[float, float]] = []
        self.turn_s = turn_s
        self.answer_locally = True
        self._fields = {
            "idle": idle,
            "editor_empty": editor_empty,
            "prompt_open": prompt_open,
            "mode": mode,
        }
        self._lock = threading.Lock()
        self._seq = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.binding: dict[str, Any] = {}
        #: The project whose pane this is; set by `_project` (cwd matching uses it).
        self.work_dir: Path | None = None
        self.write()
        if writer:
            self.start()

    # -- the state file Pi's extension publishes ------------------------------
    def write(self) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq
            fields = dict(self._fields)
            payload = {
                "v": 1,
                "launch_token": self.launch_token,
                "runtime_instance_id": self.runtime_instance_id,
                "pid": self.pid,
                "mode": fields["mode"],
                "has_ui": True,
                "idle": fields["idle"],
                "editor_empty": fields["editor_empty"],
                "prompt_open": fields["prompt_open"],
                "seq": seq,
                "updated_at": int(time.time() * 1000),
            }
        # Atomic replace: a reader must never catch a half-written snapshot. A stopped pane (a
        # finished test) simply stops publishing.
        temporary = self.state_path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, self.state_path)
        except OSError:
            return

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(WRITE_INTERVAL_S):
                self.write()

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def set_state(self, **fields: Any) -> None:
        self._fields.update(fields)
        self.write()

    # -- the terminal backend -------------------------------------------------
    def is_alive(self, pane: str) -> bool:
        return pane == self.pane_id

    def pane_matches_cwd_strict(self, pane: str, cwd: str) -> bool:
        if pane != self.pane_id or self.work_dir is None:
            return False
        return Path(cwd).resolve() == self.work_dir.resolve()

    def find_pane_by_title_marker(self, marker: str, _cwd: str) -> str:
        return self.pane_id if marker == self.pane_title_marker else ""

    def pane_pid(self, pane: str) -> str:
        return self.pane_process if pane == self.pane_id else ""

    def send_text(self, pane: str, text: str) -> None:
        self.paste_started.set()
        if self.paste_gate is not None:
            # Hold the caller INSIDE the pane lock: nothing is recorded, and the pane stays ready,
            # so a sender that is not serialised would paste right now.
            self.paste_gate.wait(timeout=10)
        started = time.monotonic()
        with self._lock:
            self.ready_at_paste.append(read_pi_state(self.binding).status)
            self.sent.append((pane, text))
        if self.answer_locally:
            self.write_anchor(text)
        if self.turn_s:
            self.set_state(idle=False)

            def finish() -> None:
                time.sleep(self.turn_s)
                self.set_state(idle=True)

            threading.Thread(target=finish, daemon=True).start()
        with self._lock:
            self.paste_windows.append((started, time.monotonic()))

    def write_anchor(self, text: str) -> None:
        """Answer a LOCAL `ask pi` the way Pi would: an anchored turn that ends with CCB_DONE.

        A peer delivery explicitly forbids a local CCB_DONE, so it is left unanswered -- the peer
        path never reads the transcript anyway (its confirmation stays `sent`).
        """
        if "Do not produce a local CCB_DONE" in text:
            return
        match = re.search(r"^CCB_REQ_ID: (\S+)$", text, re.MULTILINE)
        if match is None or not self.binding:
            return
        session_dir = Path(str(self.binding.get("pi_session_dir") or ""))
        files = sorted(session_dir.glob("*.jsonl")) if session_dir.is_dir() else []
        if not files:
            return
        req_id = match.group(1)
        rows = [
            {
                "type": "message", "id": f"user-{req_id}", "parentId": None,
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            },
            {
                "type": "message", "id": f"assistant-{req_id}", "parentId": f"user-{req_id}",
                "message": {
                    "role": "assistant", "stopReason": "stop",
                    "content": [{"type": "text", "text": f"done\nCCB_DONE: {req_id}"}],
                },
            },
        ]
        try:
            with files[0].open("a", encoding="utf-8") as handle:
                handle.write("".join(json.dumps(row) + "\n" for row in rows))
        except OSError:
            return


def _launch_id() -> str:
    """A launch id naming THIS test process, so the registry launcher is provably alive."""
    from launcher_identity import process_start_epoch

    started = process_start_epoch(os.getpid())
    epoch = int(started) if started is not None else int(time.time())
    return f"ai-{epoch}-{os.getpid()}"


def _write_launch(home: Path, launch_id: str, project: Path, pane: FakePiPane) -> None:
    registry = home / ".ccb" / "run"
    registry.mkdir(parents=True, exist_ok=True)
    project_id = compute_ccb_project_id(project)
    record = {
        "ccb_session_id": launch_id,
        "ccb_project_id": project_id,
        "work_dir": str(project),
        "updated_at": int(time.time()),
        "terminal": pane.terminal,
        "live_sessions": [
            {
                "live_id": f"{launch_id}-pi-0",
                "provider": "pi",
                "launch_id": launch_id,
                "pane_id": pane.pane_id,
                "terminal": pane.terminal,
                "pane_title_marker": pane.pane_title_marker,
                "work_dir": str(project),
                "ccb_project_id": project_id,
                "active": True,
            }
        ],
    }
    (registry / f"ccb-session-{launch_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _project(
    tmp_path: Path,
    pane: FakePiPane,
    *,
    launch_id: str | None = None,
    binding_pane: str | None = None,
    binding_terminal: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """A destination project with its own `.ccb/.pi-session` and one live Pi launch."""
    project = tmp_path / "project"
    (project / ".ccb").mkdir(parents=True, exist_ok=True)
    session_dir = project / "sessions"
    session_dir.mkdir(exist_ok=True)
    header = {"type": "session", "id": SESSION_ID, "cwd": str(project), "version": 3}
    (session_dir / f"2026-11-06T18-00-00.000Z_{SESSION_ID}.jsonl").write_text(
        json.dumps(header) + "\n", encoding="utf-8"
    )
    launch = launch_id or _launch_id()
    binding = {
        "active": True,
        "terminal": binding_terminal or pane.terminal,
        "work_dir": str(project),
        "start_dir": str(project),
        "session_id": launch,
        "pi_session_id": SESSION_ID,
        "pi_session_dir": str(session_dir),
        "pane_id": binding_pane or pane.pane_id,
        "pane_title_marker": pane.pane_title_marker,
        "ccb_project_id": compute_ccb_project_id(project),
        "pi_state_file": str(pane.state_path),
        "runtime_dir": str(pane.state_path.parent),
        "pi_launch_token": pane.launch_token,
        "ccb_managed_launch": True,
        "ccb_launch_caller": "pi",
        "ccb_launch_provider": "pi",
    }
    (project / ".ccb" / ".pi-session").write_text(json.dumps(binding), encoding="utf-8")
    _write_launch(Path.home(), launch, project, pane)
    pane.binding = binding
    pane.work_dir = project
    return project, binding


@pytest.fixture
def use_pane(monkeypatch):
    """Point every backend factory at one fake pane (pi_comm and pane_registry both ask)."""

    def _use(pane: FakePiPane) -> None:
        monkeypatch.setattr("terminal.get_backend_for_session", lambda _data: pane)
        monkeypatch.setattr("pane_registry.get_backend_for_session", lambda _data: pane)
        # The Pi adapter binds the factory at import time, so it needs its own patch.
        monkeypatch.setattr("askd.adapters.pi.get_backend_for_session", lambda _data: pane)

    return _use


@pytest.fixture(autouse=True)
def _fast_gate(monkeypatch):
    """Keep the gate's real waits, at test speed."""
    monkeypatch.setattr("pi_comm.PRE_SEND_RETRY_WAIT_S", 0.05)
    monkeypatch.setattr("pi_comm.PRE_SEND_WAIT_POLL_S", 0.01)


def _peer_task(
    project: Path,
    pane: FakePiPane,
    *,
    pane_pid: str | None = None,
    launch_token: str = "",
    launch_id: str = "",
    timeout_s: float = 5.0,
    kind: str = "request",
    message: str = "check the parser",
    reply_to: str = "",
) -> QueuedTask:
    request = ProviderRequest(
        client_id="peer",
        work_dir=str(project),
        timeout_s=timeout_s,
        quiet=True,
        message=message,
        caller="claude",
        delivery_only=True,
        kind=kind,
        reply_to=reply_to,
        peer_destination=PeerDestination(
            pane_id=pane.pane_id,
            terminal=pane.terminal,
            work_dir=str(project),
            ccb_project_id=compute_ccb_project_id(project),
            pane_title_marker=pane.pane_title_marker,
            launch_id=launch_id,
            pane_pid=pane.pane_process if pane_pid is None else pane_pid,
            launch_token=launch_token,
        ),
    )
    return QueuedTask(
        request=request, created_ms=int(time.time() * 1000), req_id="peer-req-1",
        done_event=threading.Event(),
    )


# --------------------------------------------------------------------------- #
# The shared gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, "ready"),
        ({"idle": False}, "busy"),
        ({"editor_empty": False}, "draft"),
        ({"prompt_open": True}, "prompt"),
        ({"mode": "print"}, "mismatch"),
    ],
    ids=["ready", "busy", "draft", "prompt", "mismatch"],
)
def test_every_readiness_state_decides_as_the_gate_documents(
    tmp_path: Path, fields: dict[str, Any], expected: str
) -> None:
    """Each state the extension can publish has exactly one gate verdict."""
    pane = FakePiPane(tmp_path, **fields)
    try:
        policy = PiGatePolicy(busy_wait=False, retry_limit=0)
        verdict = wait_until_pi_injectable(
            lambda: _binding_for(pane), time.monotonic() + 2.0, policy=policy
        )
        assert verdict.outcome == ("ready" if expected == "ready" else "refused")
        assert verdict.state.status == expected
    finally:
        pane.stop()


def test_an_absent_state_file_refuses(tmp_path: Path) -> None:
    pane = FakePiPane(tmp_path, writer=False)
    pane.state_path.unlink()
    try:
        verdict = wait_until_pi_injectable(
            lambda: _binding_for(pane),
            time.monotonic() + 2.0,
            policy=PiGatePolicy(retry_limit=0, retry_wait_s=0.0),
        )
        assert (verdict.outcome, verdict.state.status) == ("refused", "absent")
    finally:
        pane.stop()


def test_a_stale_snapshot_refuses(tmp_path: Path) -> None:
    pane = FakePiPane(tmp_path, writer=False)
    payload = json.loads(pane.state_path.read_text(encoding="utf-8"))
    payload["updated_at"] = int((time.time() - 60) * 1000)
    pane.state_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        verdict = wait_until_pi_injectable(
            lambda: _binding_for(pane),
            time.monotonic() + 2.0,
            policy=PiGatePolicy(busy_wait=False, retry_limit=0),
        )
        assert (verdict.outcome, verdict.state.status) == ("refused", "stale")
    finally:
        pane.stop()


def _binding_for(pane: FakePiPane) -> dict[str, Any]:
    return {
        "pi_state_file": str(pane.state_path),
        "pi_launch_token": pane.launch_token,
        "session_id": "ai-1-launch",
    }


def test_a_busy_pane_is_waited_for_and_then_allowed(tmp_path: Path) -> None:
    pane = FakePiPane(tmp_path, idle=False)
    try:
        waits: list[float] = []

        def wait(seconds: float) -> bool:
            waits.append(seconds)
            pane.set_state(idle=True)
            return True

        verdict = wait_until_pi_injectable(
            lambda: _binding_for(pane),
            time.monotonic() + 5.0,
            policy=PiGatePolicy(retry_wait_s=0.25),
            wait=wait,
        )
        assert (verdict.outcome, verdict.state.status) == ("ready", "ready")
        assert waits == [0.25]
    finally:
        pane.stop()


def test_a_draft_spends_the_bounded_retries_and_then_refuses(tmp_path: Path) -> None:
    pane = FakePiPane(tmp_path, editor_empty=False)
    try:
        loads: list[int] = []
        waits: list[float] = []

        def resolve() -> dict[str, Any]:
            loads.append(1)
            return _binding_for(pane)

        def wait(seconds: float) -> bool:
            waits.append(seconds)
            return True

        verdict = wait_until_pi_injectable(
            resolve,
            time.monotonic() + 30.0,
            policy=PiGatePolicy(retry_wait_s=0.5, retry_limit=2),
            wait=wait,
        )
        assert verdict.outcome == "refused"
        assert verdict.state.status == "draft"
        # One initial read plus the two retries, each after a re-resolved binding.
        assert len(loads) == 3
        assert waits == [0.5, 0.5]
    finally:
        pane.stop()


def test_a_prompt_never_waits_and_never_spends_the_busy_budget(tmp_path: Path) -> None:
    pane = FakePiPane(tmp_path, prompt_open=True)
    try:
        waits: list[float] = []
        verdict = wait_until_pi_injectable(
            lambda: _binding_for(pane),
            time.monotonic() + 30.0,
            policy=PiGatePolicy(busy_wait=False, retry_limit=0),
            wait=lambda seconds: waits.append(seconds) or True,
        )
        assert (verdict.outcome, verdict.state.status) == ("refused", "prompt")
        assert waits == []
    finally:
        pane.stop()


def test_the_gate_re_resolves_the_binding_every_pass(tmp_path: Path) -> None:
    pane = FakePiPane(tmp_path, idle=False)
    try:
        seen: list[str] = []

        def resolve() -> dict[str, Any]:
            seen.append("read")
            if len(seen) > 1:
                pane.set_state(idle=True)
            return _binding_for(pane)

        verdict = wait_until_pi_injectable(
            resolve, time.monotonic() + 5.0, policy=PiGatePolicy(retry_wait_s=0.01),
        )
        assert verdict.ready and len(seen) == 2
    finally:
        pane.stop()


def test_expiry_and_cancellation_end_the_wait_with_nothing_ready(tmp_path: Path) -> None:
    pane = FakePiPane(tmp_path, idle=False)
    try:
        expired = wait_until_pi_injectable(
            lambda: _binding_for(pane),
            time.monotonic() + 0.05,
            policy=PiGatePolicy(retry_wait_s=0.01),
        )
        assert expired.outcome == "expired"

        checks = {"n": 0}

        def cancel() -> bool:
            checks["n"] += 1
            return checks["n"] > 1

        cancelled = wait_until_pi_injectable(
            lambda: _binding_for(pane), time.monotonic() + 30.0, cancel=cancel,
            policy=PiGatePolicy(retry_wait_s=0.01),
        )
        assert cancelled.outcome == "cancelled"
    finally:
        pane.stop()


def test_a_cancelled_wait_returns_without_a_ready_verdict(tmp_path: Path) -> None:
    pane = FakePiPane(tmp_path)
    try:
        def cancel() -> bool:
            return True

        verdict = wait_until_pi_injectable(
            lambda: _binding_for(pane), time.monotonic() + 30.0, cancel=cancel,
        )
        assert verdict.outcome == "cancelled"
        assert not verdict.ready
    finally:
        pane.stop()


# --------------------------------------------------------------------------- #
# The destination binding
# --------------------------------------------------------------------------- #


def test_the_destination_binding_is_corroborated_with_its_live_session(
    tmp_path: Path, use_pane
) -> None:
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        found, error = peer_pi_destination_binding(
            project, pane_id=pane.pane_id, terminal="tmux", launch_id=binding["session_id"]
        )
        assert error == "" and found is not None
        assert found["pane_id"] == pane.pane_id
    finally:
        pane.stop()


@pytest.mark.parametrize(
    ("pane_id", "terminal", "launch_id"),
    [
        ("%99", "tmux", None),
        ("%12", "wezterm", None),
        ("%12", "tmux", "ai-1-something-else"),
    ],
    ids=["other-pane", "other-terminal", "other-launch"],
)
def test_a_destination_binding_that_disagrees_refuses(
    tmp_path: Path, use_pane, pane_id: str, terminal: str, launch_id: str | None
) -> None:
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        _found, error = peer_pi_destination_binding(
            project,
            pane_id=pane_id,
            terminal=terminal,
            launch_id=binding["session_id"] if launch_id is None else launch_id,
        )
        assert error
    finally:
        pane.stop()


def test_a_missing_binding_or_missing_channel_refuses(tmp_path: Path, use_pane) -> None:
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        empty_project = tmp_path / "empty"
        empty_project.mkdir()
        assert peer_pi_destination_binding(
            empty_project, pane_id=pane.pane_id, terminal="tmux"
        )[1]
        without_channel = dict(binding)
        without_channel.pop("pi_launch_token")
        (project / ".ccb" / ".pi-session").write_text(json.dumps(without_channel), encoding="utf-8")
        assert peer_pi_destination_binding(
            project, pane_id=pane.pane_id, terminal="tmux"
        )[1]
    finally:
        pane.stop()


def test_the_tmux_pane_incarnation_must_match_what_was_captured(
    tmp_path: Path, use_pane
) -> None:
    pane = FakePiPane(tmp_path)
    _project(tmp_path, pane)
    use_pane(pane)
    try:
        binding = _binding_for(pane)
        assert pi_incarnation_problem(
            pane, binding, pane_id=pane.pane_id, terminal="tmux",
            recorded_pane_pid=pane.pane_process,
        ) == ""
        assert "no Pi process was recorded" in pi_incarnation_problem(
            pane, binding, pane_id=pane.pane_id, terminal="tmux", recorded_pane_pid=""
        )
        assert "different process" in pi_incarnation_problem(
            pane, binding, pane_id=pane.pane_id, terminal="tmux", recorded_pane_pid="1111"
        )
    finally:
        pane.stop()


def test_the_wezterm_incarnation_witness_is_the_pi_process_and_launch(
    tmp_path: Path, use_pane
) -> None:
    """WezTerm reports no pane pid, so the snapshot's own process and token are the witness."""
    pane = FakePiPane(tmp_path / "wz", terminal="wezterm")
    project, binding = _project(tmp_path / "wz", pane)
    use_pane(pane)
    try:
        witness, error = pi_incarnation_witness(
            project, pane_id=pane.pane_id, terminal="wezterm"
        )
        assert error == ""
        assert witness["pane_pid"] == str(os.getpid())
        assert witness["launch_token"] == pane.launch_token
        assert pi_incarnation_problem(
            pane, binding, pane_id=pane.pane_id, terminal="wezterm",
            recorded_pane_pid=witness["pane_pid"],
            recorded_launch_token=witness["launch_token"],
        ) == ""
        # runtime_instance_id changes legitimately on a reload: it is never the witness.
        pane.runtime_instance_id = "99999-other-runtime"
        pane.write()
        assert pi_incarnation_problem(
            pane, binding, pane_id=pane.pane_id, terminal="wezterm",
            recorded_pane_pid=witness["pane_pid"],
            recorded_launch_token=witness["launch_token"],
        ) == ""
        # A different Pi process in the same pane is exactly the replacement this catches.
        assert "different process" in pi_incarnation_problem(
            pane, binding, pane_id=pane.pane_id, terminal="wezterm",
            recorded_pane_pid="4242",
            recorded_launch_token=witness["launch_token"],
        )
        assert "no Pi launch token was recorded" in pi_incarnation_problem(
            pane, binding, pane_id=pane.pane_id, terminal="wezterm",
            recorded_pane_pid=witness["pane_pid"],
        )
        pane.launch_token = _TOKEN_BETA
        pane.write()
        assert "different launch" in pi_incarnation_problem(
            pane, binding, pane_id=pane.pane_id, terminal="wezterm",
            recorded_pane_pid=witness["pane_pid"],
            recorded_launch_token=witness["launch_token"],
        )
    finally:
        pane.stop()


def test_a_destination_witness_is_captured_from_the_live_pane(tmp_path: Path, use_pane) -> None:
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        witness, error = pi_incarnation_witness(
            project, pane_id=pane.pane_id, terminal="tmux", launch_id=binding["session_id"]
        )
        assert error == "" and witness["pane_pid"] == pane.pane_process
        # An unreadable pane process is no witness at all: the caller must refuse.
        pane.pane_process = ""
        witness, error = pi_incarnation_witness(
            project, pane_id=pane.pane_id, terminal="tmux", launch_id=binding["session_id"]
        )
        assert witness == {} and "could not be read" in error
    finally:
        pane.stop()


# --------------------------------------------------------------------------- #
# The delivery itself
# --------------------------------------------------------------------------- #


def test_a_ready_pi_peer_delivery_pastes_once_with_the_pi_wrapper(
    tmp_path: Path, use_pane
) -> None:
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        result = send_peer_message(
            _peer_task(project, pane, launch_id=binding["session_id"]), "pi", "pi:peer"
        )
        assert (result.exit_code, result.status) == (0, "completed")
        assert result.extra == {"confirmation": "sent"}
        assert [pane_id for pane_id, _ in pane.sent] == ["%12"]
        assert pane.ready_at_paste == ["ready"]
        pasted = pane.sent[0][1]
        assert "Do not produce a local CCB_DONE" in pasted
        assert "CCB_DONE: peer-req-1" not in pasted
    finally:
        pane.stop()


def test_a_busy_pi_peer_delivery_waits_then_pastes_once(tmp_path: Path, use_pane) -> None:
    pane = FakePiPane(tmp_path, idle=False)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        threading.Timer(0.15, lambda: pane.set_state(idle=True)).start()
        result = send_peer_message(
            _peer_task(project, pane, launch_id=binding["session_id"]), "pi", "pi:peer"
        )
        assert result.exit_code == 0
        assert len(pane.sent) == 1
        assert pane.ready_at_paste == ["ready"]
    finally:
        pane.stop()


def test_a_busy_pi_that_never_frees_times_out_with_zero_sends(
    tmp_path: Path, use_pane
) -> None:
    pane = FakePiPane(tmp_path, idle=False)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        result = send_peer_message(
            _peer_task(project, pane, launch_id=binding["session_id"], timeout_s=0.2),
            "pi", "pi:peer",
        )
        assert result.exit_code == 1
        assert "timed out before Pi was ready" in result.reply
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_draft_refuses_with_zero_sends(tmp_path: Path, use_pane) -> None:
    pane = FakePiPane(tmp_path, editor_empty=False)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        result = send_peer_message(
            _peer_task(project, pane, launch_id=binding["session_id"]), "pi", "pi:peer"
        )
        assert result.exit_code == 1 and "not injectable" in result.reply
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_binding_mismatch_refuses_with_zero_sends(tmp_path: Path, use_pane) -> None:
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane, binding_pane="%77")
    use_pane(pane)
    try:
        result = send_peer_message(
            _peer_task(project, pane, launch_id=binding["session_id"]), "pi", "pi:peer"
        )
        assert result.exit_code == 1 and "unavailable" in result.reply
        assert pane.sent == []
    finally:
        pane.stop()


@pytest.mark.parametrize("pane_pid", ["", "1111"], ids=["missing", "changed"])
def test_a_reply_to_a_replaced_tmux_pi_pane_refuses_with_zero_sends(
    tmp_path: Path, use_pane, pane_pid: str
) -> None:
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        result = send_peer_message(
            _peer_task(
                project, pane, launch_id=binding["session_id"], pane_pid=pane_pid,
                kind="reply", reply_to="original-task",
            ),
            "pi", "pi:peer",
        )
        assert result.exit_code == 1 and "not the pane that was selected" in result.reply
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_forward_delivery_without_a_witness_refuses(tmp_path: Path, use_pane) -> None:
    """F1: a forward send also needs the pid captured before queueing; missing means nothing."""
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        result = send_peer_message(
            _peer_task(project, pane, launch_id=binding["session_id"], pane_pid=""),
            "pi", "pi:peer",
        )
        assert result.exit_code == 1
        assert "no Pi process was recorded" in result.reply
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_wezterm_pane_with_another_launch_token_refuses(
    tmp_path: Path, use_pane
) -> None:
    pane = FakePiPane(tmp_path, terminal="wezterm")
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        witness, error = pi_incarnation_witness(
            project, pane_id=pane.pane_id, terminal="wezterm", launch_id=binding["session_id"]
        )
        assert error == ""
        pane.launch_token = _TOKEN_BETA
        pane.write()
        result = send_peer_message(
            _peer_task(
                project, pane, launch_id=binding["session_id"],
                pane_pid=witness["pane_pid"], launch_token=witness["launch_token"],
            ),
            "pi", "pi:peer",
        )
        assert result.exit_code == 1 and "different launch" in result.reply
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_cancelled_peer_delivery_pastes_nothing(tmp_path: Path, use_pane) -> None:
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        task = _peer_task(project, pane, launch_id=binding["session_id"])
        task.cancelled = True
        result = send_peer_message(task, "pi", "pi:peer")
        assert result.exit_code == 2 and result.status == "cancelled"
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_not_ready_verdict_inside_the_lock_resumes_waiting(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    """The gate can pass and the in-lock confirmation still refuse: then nothing is pasted yet.

    Simulated honestly: the pane is busy the first time the in-lock confirmation runs and idle
    afterwards, so the paste may only happen after a SECOND, ready in-lock confirmation.
    """
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    confirms: list[str] = []
    real_confirm = pi_confirm

    def confirm(bind, deadline=None, **kwargs) -> PiState:
        confirms.append("confirm")
        if len(confirms) == 2:
            # The second confirmation is the one inside the lock.
            pane.set_state(idle=False)
            state = real_confirm(bind, None)
            pane.set_state(idle=True)
            return state
        return real_confirm(bind, deadline, **kwargs)

    monkeypatch.setattr("pi_comm.confirm_pi_ready", confirm)
    try:
        result = send_peer_message(
            _peer_task(project, pane, launch_id=binding["session_id"]), "pi", "pi:peer"
        )
        assert result.exit_code == 0
        assert len(pane.sent) == 1
        # Every paste saw a ready pane, and more than one confirmation was needed.
        assert pane.ready_at_paste == ["ready"]
        assert len(confirms) >= 4
    finally:
        pane.stop()


# --------------------------------------------------------------------------- #
# One pane, three senders
# --------------------------------------------------------------------------- #


def test_peer_local_ask_and_autonew_never_paste_into_a_busy_pane(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    """Three Pi senders never paste into a pane that is not ready.

    This is the READINESS invariant across senders (a paste starts a turn, so a sender that ignores
    the gate pastes into a busy pane and is recorded as such). It does NOT prove the pane lock is
    doing the serialising -- it can pass with locking disabled; `_lock_serialisation_outcome`
    below is the canary for that, and it has its own lock-disabled negative control.
    """
    from askd.adapters.pi import PiAdapter

    # The adapter reads its OWN knob (as its test suite does); the shared gate's default is patched
    # by the autouse fixture. Both are the same policy, resolved at call time.
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 0.05)
    pane = FakePiPane(tmp_path, turn_s=0.15)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)
    monkeypatch.chdir(project)
    errors: list[BaseException] = []
    results: list[Any] = []
    autonew_codes: list[int] = []

    def peer() -> None:
        try:
            send_peer_message(
                _peer_task(project, pane, launch_id=binding["session_id"]), "pi", "pi:peer"
            )
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    def local_ask() -> None:
        try:
            request = ProviderRequest(
                client_id="local", work_dir=str(project), timeout_s=10.0, quiet=True,
                message="local work", caller="claude",
            )
            task = QueuedTask(
                request=request, created_ms=0, req_id="local-ask", done_event=threading.Event()
            )
            results.append(adapter.handle_task(task))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def autonew() -> None:
        import importlib.machinery
        import importlib.util

        try:
            path = Path(__file__).resolve().parents[1] / "bin" / "autonew"
            loader = importlib.machinery.SourceFileLoader("autonew_peer_test", str(path))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            assert spec is not None
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            autonew_codes.append(module.main(["autonew", "pi"]))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=target) for target in (peer, local_ask, autonew)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert errors == []
        # Nothing was ever pasted into a pane that was not ready.
        assert pane.ready_at_paste == ["ready"] * len(pane.sent)
        assert len(pane.sent) >= 2, (
            [(r.exit_code, r.status, r.reply) for r in results], autonew_codes, pane.ready_at_paste
        )
        # No two pastes overlapped: the pane lock serialised them.
        ordered = sorted(pane.paste_windows)
        for (_start, end), (later_start, _later_end) in zip(ordered, ordered[1:], strict=False):
            assert end <= later_start
        # autonew either refused a busy pane (its documented fail-closed rule) or sent /new.
        assert autonew_codes and autonew_codes[0] in (0, 1)
    finally:
        pane.stop()


# --------------------------------------------------------------------------- #
# Wrappers (golden)
# --------------------------------------------------------------------------- #


def test_the_pi_delivery_wrapper_carries_the_reply_command() -> None:
    text = wrap_pi_delivery_prompt("fix the parser", "req-7")
    assert text.startswith("CCB_REQ_ID: req-7\n\nfix the parser\n\n")
    assert 'ask <CCB_REPLY_PROVIDER> --peer "<CCB_REPLY_TARGET>" --reply-to <CCB_PEER_TASK_ID> --background' in text
    assert "<<'EOF'" in text
    assert "--notify only for a terminal answer" in text
    assert "Do not produce a local CCB_DONE" in text


def test_the_codex_and_claude_delivery_wrappers_are_unchanged() -> None:
    """Golden text: adding a Pi wrapper must not move a single byte of these two."""
    codex = wrap_codex_delivery_prompt("hello", "req-1")
    assert codex == (
        "CCB_REQ_ID: req-1\n\n"
        "hello\n\n"
        "This is an asynchronous peer message delivered by CCB.\n"
        "Read CCB_PEER_INTENT, CCB_REPLY_EXPECTED, CCB_REPLY_TARGET, and CCB_REPLY_PROVIDER.\n"
        "If CCB_REPLY_EXPECTED is no, do not send a reverse peer message; embedded questions are informational.\n"
        "If a reply is expected, send it explicitly with ask --peer to CCB_REPLY_TARGET using CCB_REPLY_PROVIDER.\n"
        "Preserve CCB_PEER_TASK_ID as --reply-to. Use --notify only for a terminal result with no follow-up question; otherwise use --background.\n"
        "Do not narrate CCB transport markers or confirmation diagnostics unless delivery fails.\n"
        "Do not produce a local CCB_DONE for this delivery; CCB is not capturing this turn automatically.\n"
    )
    claude = wrap_claude_delivery_prompt("hello", "req-1")
    assert claude.startswith("CCB_REQ_ID: req-1\n\nhello\n\n")
    assert claude == wrap_claude_delivery_prompt("hello", "req-1")



# --------------------------------------------------------------------------- #
# The bridge's direct reply path (daemon unreachable)
# --------------------------------------------------------------------------- #


def _load_bridge():
    import importlib.machinery
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "bin" / "ccb-bridge-ask"
    loader = importlib.machinery.SourceFileLoader("ccb_bridge_pi_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _write_pi_caller_receipt(project: Path, pane: FakePiPane, *, task_id: str) -> dict[str, Any]:
    from task_receipts import new_peer_receipt, receipt_path, write_receipt

    receipt = new_peer_receipt(
        task_id=task_id,
        peer_provider="claude",
        caller="pi",
        intent="background",
        work_dir=project,
        status_file=project / f"{task_id}.status",
        log_file=project / f"{task_id}.log",
        reply_file=project / f"{task_id}.reply",
        caller_pane_id=pane.pane_id,
        caller_terminal=pane.terminal,
    )
    write_receipt(receipt_path("peer-claude", task_id), receipt)
    return receipt


def _bridge_argv(project: Path, task_id: str) -> list[str]:
    return [
        "--target", str(project),
        "--provider", "pi",
        "--reply-to", task_id,
        "--caller", "pi",
        "--sender-work-dir", str(project),
        "--intent", "background",
        "the answer",
    ]


def _bridge_for(monkeypatch, bridge) -> None:
    monkeypatch.setattr(bridge, "_load_targets", list)


def test_the_direct_pi_reply_waits_for_a_busy_pane_then_pastes_once(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    pane = FakePiPane(tmp_path, idle=False)
    project, _binding = _project(tmp_path, pane)
    use_pane(pane)
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_load_targets", list)
    receipt = _write_pi_caller_receipt(project, pane, task_id="task-busy")
    # The receipt captured the pid the fake reports; the pane then goes idle.
    assert receipt["caller_pane_pid"] == pane.pane_process
    threading.Timer(0.15, lambda: pane.set_state(idle=True)).start()
    try:
        code = bridge.main(_bridge_argv(project, "task-busy"))
        assert code == 0
        assert len(pane.sent) == 1
        assert pane.ready_at_paste == ["ready"]
        assert "Do not produce a local CCB_DONE" in pane.sent[0][1]
    finally:
        pane.stop()


def test_the_direct_pi_reply_refuses_a_draft(tmp_path: Path, use_pane, monkeypatch) -> None:
    pane = FakePiPane(tmp_path)
    project, _binding = _project(tmp_path, pane)
    use_pane(pane)
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_load_targets", list)
    _write_pi_caller_receipt(project, pane, task_id="task-draft")
    pane.set_state(editor_empty=False)
    try:
        assert bridge.main(_bridge_argv(project, "task-draft")) == 1
        assert pane.sent == []
    finally:
        pane.stop()


@pytest.mark.parametrize("mode", ["missing", "changed", "binding"])
def test_the_direct_pi_reply_refuses_a_pane_it_cannot_prove(
    tmp_path: Path, use_pane, monkeypatch, mode: str
) -> None:
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_load_targets", list)
    receipt = _write_pi_caller_receipt(project, pane, task_id=f"task-{mode}")
    if mode == "missing":
        receipt.pop("caller_pane_pid")
        from task_receipts import receipt_path, write_receipt

        write_receipt(receipt_path("peer-claude", f"task-{mode}"), receipt)
    elif mode == "changed":
        pane.pane_process = "1234"
    else:
        (project / ".ccb" / ".pi-session").write_text(
            json.dumps(dict(binding, pane_id="%77")), encoding="utf-8"
        )
    try:
        assert bridge.main(_bridge_argv(project, f"task-{mode}")) == 1
        assert pane.sent == []
    finally:
        pane.stop()


def test_the_bridge_refuses_a_pi_peer_target_with_no_verified_endpoint(
    tmp_path: Path, monkeypatch
) -> None:
    """A5: no endpoint identity means no delivery -- never a generic Pi adapter send."""
    bridge = _load_bridge()
    target = {
        "index": 1,
        "work_dir": str(tmp_path),
        "ccb_project_id": "abcd1234",
        # Mounted, so the "no mounted pane" branch does not fire -- but with no pane id, marker or
        # live id there is nothing to paste into.
        "providers": {"pi": {"alive": True, "mounted": True, "pane_id": ""}},
        "peer_providers": ["pi"],
    }
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    monkeypatch.setattr(
        bridge, "_send_to_daemon", lambda *a, **k: pytest.fail("nothing may be delivered")
    )

    assert bridge.main(["--target", "1", "--provider", "pi", "hello"]) == 1


# --------------------------------------------------------------------------- #
# The daemon fails closed too
# --------------------------------------------------------------------------- #


class _FakeRegistry:
    def get(self, _provider: str):
        return object()


def _daemon_with_pool(pool):
    from askd.daemon import UnifiedAskDaemon

    daemon = UnifiedAskDaemon.__new__(UnifiedAskDaemon)
    daemon.registry = _FakeRegistry()
    daemon.pool = pool
    daemon.token = _DAEMON_TOKEN
    daemon.host = "127.0.0.1"
    daemon.port = 0
    daemon.work_dir = None
    return daemon


class _RecordingPool:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, Any]] = []

    def submit(self, provider: str, request: Any):
        self.submitted.append((provider, request))
        return None


def test_the_daemon_refuses_a_pi_peer_delivery_without_an_endpoint(tmp_path: Path) -> None:
    pool = _RecordingPool()
    daemon = _daemon_with_pool(pool)

    response = daemon._handle_request({
        "type": "ask.request",
        "v": 1,
        "id": "client-1",
        "token": _DAEMON_TOKEN,
        "provider": "pi",
        "work_dir": str(tmp_path),
        "timeout_s": 5.0,
        "message": "hello",
        "caller": "claude",
        "delivery_only": True,
        "peer_delivery": True,
    })

    assert response["exit_code"] == 1
    assert response["accepted"] is False
    assert "verified live Pi pane" in response["reply"]
    assert pool.submitted == []


def test_the_daemon_accepts_a_pi_peer_delivery_that_carries_its_endpoint(
    tmp_path: Path, monkeypatch
) -> None:
    """The fail-closed guard is narrow: with an endpoint the request proceeds normally."""
    pool = _RecordingPool()
    daemon = _daemon_with_pool(pool)
    monkeypatch.setattr(
        "peer_routing.revalidate_peer_destination", lambda _destination: _OK_RESOLUTION
    )

    response = daemon._handle_request({
        "type": "ask.request",
        "v": 1,
        "id": "client-2",
        "token": _DAEMON_TOKEN,
        "provider": "pi",
        "work_dir": str(tmp_path),
        "timeout_s": 5.0,
        "message": "hello",
        "caller": "claude",
        "delivery_only": True,
        "peer_delivery": True,
        "peer_destination": {
            "pane_id": "%12",
            "terminal": "tmux",
            "work_dir": str(tmp_path),
            "ccb_project_id": "abcd1234",
            "pane_title_marker": "Pi-marker",
        },
    })

    assert response.get("accepted") is not False
    assert [provider for provider, _ in pool.submitted] == ["pi"]


from peer_routing import DestinationResolution as _DestinationResolution  # noqa: E402

_OK_RESOLUTION = _DestinationResolution()


# --------------------------------------------------------------------------- #
# F1: a pane replaced AFTER the first identity check receives nothing
# --------------------------------------------------------------------------- #


class _RespawningPane(FakePiPane):
    """A pane whose process changes on a chosen read -- a respawn between two checks.

    The first `pane_pid` answer is the one the destination was captured with; from
    `respawn_after` calls onwards it is a different process, exactly like a pane respawned in
    place (same pane id, same marker, same binding file).
    """

    def __init__(self, root: Path, *, respawn_after: int = 1, **kwargs):
        self.pane_pid_calls = 0
        self.respawn_after = respawn_after
        self.original_pane_process = "9001"
        super().__init__(root, **kwargs)
        self.pane_process = self.original_pane_process

    def pane_pid(self, pane: str) -> str:
        self.pane_pid_calls += 1
        if self.pane_pid_calls > self.respawn_after:
            return "4242"
        return self.pane_process if pane == self.pane_id else ""


def test_a_pane_respawned_during_the_wait_gets_no_peer_delivery(
    tmp_path: Path, use_pane
) -> None:
    pane = _RespawningPane(tmp_path, respawn_after=1)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        # The recorded witness is the FIRST process; the pane answers 4242 from the in-lock read.
        result = send_peer_message(
            _peer_task(
                project, pane, launch_id=binding["session_id"],
                pane_pid=pane.original_pane_process,
            ),
            "pi", "pi:peer",
        )
        assert result.exit_code == 1
        assert "different process" in result.reply
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_pane_respawned_during_the_wait_gets_no_direct_reply(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    pane = _RespawningPane(tmp_path, respawn_after=1)
    project, _binding = _project(tmp_path, pane)
    use_pane(pane)
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_load_targets", list)
    receipt = _write_pi_caller_receipt(project, pane, task_id="task-respawn")
    assert receipt["caller_pane_pid"] == pane.original_pane_process
    try:
        assert bridge.main(_bridge_argv(project, "task-respawn")) == 1
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_queued_forward_pid_is_captured_before_queueing_and_rechecked(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    """F1: the daemon captures the forward pid at ADMISSION, then the worker re-proves it."""
    from askd.adapters.base import PeerDestination, ProviderRequest
    from askd.daemon import _SessionWorker, _UnifiedWorkerPool

    # The admission read sees the original process; every read after it sees a respawn.
    pane = _RespawningPane(tmp_path, respawn_after=1)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    queued = []

    class _Adapter:
        key = "pi"

        def load_session(self, *_args):
            raise AssertionError("a peer destination never re-resolves a provider default")

        def handle_task(self, *_args):
            raise AssertionError("the Pi adapter must never see a peer delivery")

    class _Registry:
        def get(self, _provider):
            return _Adapter()

    pool = _UnifiedWorkerPool(_Registry())
    captured: dict = {}

    class _HoldingPool:
        def get_or_create(self, key, factory):
            worker = _SessionWorker(key, _Adapter())
            worker.enqueue = queued.append
            captured["worker"] = worker
            return worker

    monkeypatch.setattr(pool, "_get_pool", lambda provider: _HoldingPool())
    request = ProviderRequest(
        client_id="peer", work_dir=str(project), timeout_s=5.0, quiet=True,
        message="forwarded", caller="claude", delivery_only=True,
        peer_destination=PeerDestination(
            pane_id=pane.pane_id, terminal=pane.terminal, work_dir=str(project),
            ccb_project_id=compute_ccb_project_id(project),
            pane_title_marker=pane.pane_title_marker,
            launch_id=binding["session_id"],
        ),
    )
    task = pool.submit("pi", request)
    assert queued == [task]
    # Captured at admission, BEFORE the worker ran -- which is what a replacement is judged against.
    assert task.request.peer_destination.pane_pid == pane.original_pane_process
    result = _SessionWorker("peer:key", _Adapter())._handle_task(task)
    assert result.exit_code != 0
    assert pane.sent == []
    pane.stop()


def test_a_forward_pi_request_without_a_readable_process_is_refused_before_queueing(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    from askd.adapters.base import PeerDestination, ProviderRequest
    from askd.daemon import _UnifiedWorkerPool

    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    pane.pane_process = ""  # the backend cannot read the pane process

    class _Registry:
        def get(self, _provider):
            return object()

    pool = _UnifiedWorkerPool(_Registry())
    queued: list = []
    monkeypatch.setattr(
        pool, "_get_pool", lambda provider: type("P", (), {"get_or_create": lambda *_a: (_ for _ in ()).throw(AssertionError("nothing may be queued"))})
    )
    request = ProviderRequest(
        client_id="peer", work_dir=str(project), timeout_s=5.0, quiet=True,
        message="forwarded", caller="claude", delivery_only=True,
        peer_destination=PeerDestination(
            pane_id=pane.pane_id, terminal=pane.terminal, work_dir=str(project),
            ccb_project_id=compute_ccb_project_id(project),
            pane_title_marker=pane.pane_title_marker,
            launch_id=binding["session_id"],
        ),
    )
    task = pool.submit("pi", request)
    assert task.done_event.is_set()
    assert task.result is not None and task.result.exit_code == 1
    assert "cannot be bound" in task.result.reply
    assert queued == []
    pane.stop()


# --------------------------------------------------------------------------- #
# F3: cancellation, expiry and timeouts are the last guards
# --------------------------------------------------------------------------- #


def test_a_cancel_during_the_in_lock_confirm_pastes_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    from askd.adapters.pi import PiAdapter

    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    monkeypatch.setattr("terminal.get_backend_for_session", lambda _data: pane)
    monkeypatch.setattr("pane_registry.get_backend_for_session", lambda _data: pane)
    monkeypatch.setattr("askd.adapters.pi.get_backend_for_session", lambda _data: pane)
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 0.05)
    cancel = threading.Event()
    calls = {"n": 0}
    real_confirm = pi_confirm

    def confirm(bind, deadline=None, **kwargs) -> PiState:
        calls["n"] += 1
        if calls["n"] == 2:
            # The gate's wait is done (one confirmation); this is the IN-LOCK one.
            cancel.set()
        return real_confirm(bind, deadline, **kwargs)

    # The adapter binds its own alias at import time, so THAT is the seam its own suite uses.
    monkeypatch.setattr("askd.adapters.pi._confirm_pi_ready", confirm)
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)
    request = ProviderRequest(
        client_id="local", work_dir=str(project), timeout_s=10.0, quiet=True,
        message="local work", caller="claude",
    )
    task = QueuedTask(
        request=request, created_ms=0, req_id="cancel-in-confirm",
        done_event=threading.Event(), cancel_event=cancel,
    )
    try:
        result = adapter.handle_task(task)
        assert result.status == "cancelled"
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_budget_spent_after_the_policy_check_sends_nothing(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    import delivery_policy

    real_refusal = delivery_policy.refusal_for_request

    def slow_policy(*args, **kwargs):
        time.sleep(0.3)  # reading the destination launch is real work and costs real time
        return real_refusal(*args, **kwargs)

    monkeypatch.setattr(delivery_policy, "refusal_for_request", slow_policy)
    try:
        result = send_peer_message(
            _peer_task(project, pane, launch_id=binding["session_id"], timeout_s=0.15),
            "pi", "pi:peer",
        )
        assert result.exit_code == 1
        assert "timed out before Pi was ready" in result.reply
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_zero_timeout_pi_peer_delivery_expires_with_zero_sends(
    tmp_path: Path, use_pane
) -> None:
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        result = send_peer_message(
            _peer_task(project, pane, launch_id=binding["session_id"], timeout_s=0.0),
            "pi", "pi:peer",
        )
        assert result.exit_code == 1 and "timed out" in result.reply
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_non_finite_pi_peer_timeout_is_refused(tmp_path: Path, use_pane) -> None:
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    try:
        for bad in (float("nan"), float("inf")):
            result = send_peer_message(
                _peer_task(project, pane, launch_id=binding["session_id"], timeout_s=bad),
                "pi", "pi:peer",
            )
            assert result.exit_code == 1 and "non-finite" in result.reply
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_zero_or_non_finite_timeout_refuses_a_direct_pi_reply(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    pane = FakePiPane(tmp_path)
    project, _binding = _project(tmp_path, pane)
    use_pane(pane)
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_load_targets", list)
    _write_pi_caller_receipt(project, pane, task_id="task-zero")
    base = _bridge_argv(project, "task-zero")
    try:
        for value in ("0", "nan", "inf"):
            argv = base[:]
            argv[argv.index("--intent"):argv.index("--intent")] = ["--timeout", value]
            assert bridge.main(argv) == 1
        assert pane.sent == []
    finally:
        pane.stop()


# --------------------------------------------------------------------------- #
# F1: the forward witness is captured at selection, before queueing
# --------------------------------------------------------------------------- #


def _pi_forward_target(project: Path, pane: FakePiPane, binding: dict[str, Any]) -> dict[str, Any]:
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
                "live_id": "pi-live-1",
            }
        },
        "peer_providers": ["pi"],
    }


def test_a_forward_pi_destination_carries_the_witness_captured_at_selection(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_load_targets", lambda: [_pi_forward_target(project, pane, binding)])
    captured: dict = {}
    monkeypatch.setattr(
        bridge, "_send_to_daemon",
        lambda target, *_a, **_k: (captured.update(target=target), (0, "ok", {}))[1],
    )
    try:
        assert bridge.main(["--target", "1", "--provider", "pi", "hello"]) == 0
    finally:
        pane.stop()
    destination = captured["target"]["peer_destination"]
    assert destination["pane_pid"] == pane.pane_process
    assert destination["launch_id"] == binding["session_id"]
    assert destination["terminal"] == "tmux"


def test_a_forward_wezterm_pi_destination_records_the_pi_process_and_token(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    pane = FakePiPane(tmp_path / "wz", terminal="wezterm")
    project, binding = _project(tmp_path / "wz", pane)
    use_pane(pane)
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_load_targets", lambda: [_pi_forward_target(project, pane, binding)])
    captured: dict = {}
    monkeypatch.setattr(
        bridge, "_send_to_daemon",
        lambda target, *_a, **_k: (captured.update(target=target), (0, "ok", {}))[1],
    )
    try:
        assert bridge.main(["--target", "1", "--provider", "pi", "hello"]) == 0
    finally:
        pane.stop()
    destination = captured["target"]["peer_destination"]
    assert destination["pane_pid"] == str(os.getpid())
    assert destination["launch_token"] == pane.launch_token


def test_a_caller_that_cannot_read_the_pane_process_still_routes_it(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    """A sandboxed caller cannot run tmux; the HOST daemon captures the witness at admission."""
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    pane.pane_process = ""  # this process cannot read the pane process
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_load_targets", lambda: [_pi_forward_target(project, pane, binding)])
    captured: dict = {}
    monkeypatch.setattr(
        bridge, "_send_to_daemon",
        lambda target, *_a, **_k: (captured.update(target=target), (0, "ok", {}))[1],
    )
    try:
        assert bridge.main(["--target", "1", "--provider", "pi", "hello"]) == 0
    finally:
        pane.stop()
    destination = captured["target"]["peer_destination"]
    # No witness was readable here, so none was claimed; the daemon admits only what IT captures.
    assert not destination.get("pane_pid")
    assert destination["launch_id"] == binding["session_id"]


def test_an_identity_mismatch_at_selection_refuses_before_any_delivery(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane, binding_pane="%77")
    use_pane(pane)
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_load_targets", lambda: [_pi_forward_target(project, pane, binding)])
    monkeypatch.setattr(
        bridge, "_send_to_daemon", lambda *a, **k: pytest.fail("nothing may be delivered")
    )
    try:
        assert bridge.main(["--target", "1", "--provider", "pi", "hello"]) == 1
    finally:
        pane.stop()


def test_a_wezterm_pi_caller_receipt_records_the_pi_process_and_token(
    tmp_path: Path, use_pane
) -> None:
    """F1 for the reply direction: WezTerm's witness is the Pi process plus its launch token."""
    pane = FakePiPane(tmp_path / "wz", terminal="wezterm")
    project, _binding = _project(tmp_path / "wz", pane)
    use_pane(pane)
    try:
        receipt = _write_pi_caller_receipt(project, pane, task_id="task-wz-receipt")
        assert receipt["caller_terminal"] == "wezterm"
        assert receipt["caller_pane_pid"] == str(os.getpid())
        assert receipt["caller_pi_launch_token"] == pane.launch_token
    finally:
        pane.stop()


# --------------------------------------------------------------------------- #
# G1: readiness is confirmed against the binding the send actually uses
# --------------------------------------------------------------------------- #


def _swap_readiness_under_the_policy(project: Path, pane: FakePiPane, *, draft: bool) -> None:
    """Point the binding at ANOTHER readiness file (same launch, same pid) that says draft.

    This is the ratifier's repro: the readiness file behind the binding is switched while the
    in-lock checks run, so a confirmation against the PRE-lock copy would still say ready.
    """
    binding = json.loads((project / ".ccb" / ".pi-session").read_text(encoding="utf-8"))
    swapped = pane.state_path.with_name("pi-state-swapped.json")
    payload = {
        "v": 1,
        "launch_token": pane.launch_token,
        "runtime_instance_id": pane.runtime_instance_id,
        "pid": os.getpid(),
        "mode": "tui",
        "has_ui": True,
        "idle": True,
        "editor_empty": not draft,
        "prompt_open": False,
        "seq": 10_000,
        "updated_at": int(time.time() * 1000),
    }
    swapped.write_text(json.dumps(payload), encoding="utf-8")
    binding["pi_state_file"] = str(swapped)
    (project / ".ccb" / ".pi-session").write_text(json.dumps(binding), encoding="utf-8")


def test_a_readiness_file_swapped_during_the_policy_check_sends_nothing(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    """Peer path: the in-lock confirmation must judge the RELOADED binding."""
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    import delivery_policy

    real_refusal = delivery_policy.refusal_for_request

    def swap_then_policy(*args, **kwargs):
        _swap_readiness_under_the_policy(project, pane, draft=True)
        return real_refusal(*args, **kwargs)

    monkeypatch.setattr(delivery_policy, "refusal_for_request", swap_then_policy)
    try:
        result = send_peer_message(
            _peer_task(project, pane, launch_id=binding["session_id"]), "pi", "pi:peer"
        )
        assert result.exit_code == 1
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_readiness_file_swapped_during_the_direct_policy_check_sends_nothing(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    """Direct reply path: same rule, judged against the reloaded binding."""
    pane = FakePiPane(tmp_path)
    project, _binding = _project(tmp_path, pane)
    use_pane(pane)
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_load_targets", list)
    _write_pi_caller_receipt(project, pane, task_id="task-swap")
    real_refusal = bridge._direct_reply_policy_refusal

    def swap_then_policy(**kwargs):
        _swap_readiness_under_the_policy(project, pane, draft=True)
        return real_refusal(**kwargs)

    monkeypatch.setattr(bridge, "_direct_reply_policy_refusal", swap_then_policy)
    try:
        assert bridge.main(_bridge_argv(project, "task-swap")) == 1
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_pane_respawned_during_the_peer_policy_check_sends_nothing(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    """Peer path: the witness is compared AFTER the policy, so a respawn there is caught."""
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    import delivery_policy

    real_refusal = delivery_policy.refusal_for_request

    def respawn_then_policy(*args, **kwargs):
        pane.pane_process = "4242"
        return real_refusal(*args, **kwargs)

    monkeypatch.setattr(delivery_policy, "refusal_for_request", respawn_then_policy)
    try:
        result = send_peer_message(
            _peer_task(project, pane, launch_id=binding["session_id"]), "pi", "pi:peer"
        )
        assert result.exit_code == 1
        assert "different process" in result.reply
        assert pane.sent == []
    finally:
        pane.stop()


def test_a_pane_respawned_during_the_direct_policy_check_sends_nothing(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    pane = FakePiPane(tmp_path)
    project, _binding = _project(tmp_path, pane)
    use_pane(pane)
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_load_targets", list)
    receipt = _write_pi_caller_receipt(project, pane, task_id="task-respawn-policy")
    assert receipt["caller_pane_pid"] == pane.pane_process
    real_refusal = bridge._direct_reply_policy_refusal

    def respawn_then_policy(**kwargs):
        pane.pane_process = "4242"
        return real_refusal(**kwargs)

    monkeypatch.setattr(bridge, "_direct_reply_policy_refusal", respawn_then_policy)
    try:
        assert bridge.main(_bridge_argv(project, "task-respawn-policy")) == 1
        assert pane.sent == []
    finally:
        pane.stop()


# --------------------------------------------------------------------------- #
# G2: a sandboxed caller defers to host admission
# --------------------------------------------------------------------------- #


def _sandbox(monkeypatch) -> dict[str, bool]:
    """Make this process look like a managed Codex sandbox to the bridge (and its probes)."""
    monkeypatch.setenv("CCB_MANAGED", "1")
    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    return {"client_probe_raises": True}


def _deny_client_terminal(pane: FakePiPane, gate: dict[str, bool], monkeypatch) -> None:
    def backend(data):
        if gate["client_probe_raises"]:
            raise PermissionError("operation not permitted: terminal socket is outside the sandbox")
        return pane

    monkeypatch.setattr("terminal.get_backend_for_session", backend)
    monkeypatch.setattr("pane_registry.get_backend_for_session", backend)
    monkeypatch.setattr("askd.adapters.pi.get_backend_for_session", backend)


def _live_id_target(project: Path, pane: FakePiPane, binding: dict[str, Any]) -> dict[str, Any]:
    target = _pi_forward_target(project, pane, binding)
    target["sessions"] = [
        {
            "session_id": binding["session_id"],
            "work_dir": str(project),
            "ccb_project_id": compute_ccb_project_id(project),
            "terminal": pane.terminal,
            "window_id": "window:$1:@1",
            "live_slot": 0,
            "updated_at": int(time.time()),
            "alive": True,
            "providers": {"pi": dict(target["providers"]["pi"])},
        }
    ]
    return target


@pytest.mark.parametrize("selection", ["ordinary", "live-id"])
def test_a_sandboxed_forward_send_is_admitted_and_witnessed_by_the_daemon(
    tmp_path: Path, monkeypatch, selection: str
) -> None:
    """G2: client-side liveness, marker and pid all unavailable -> admission does it."""
    from askd.daemon import _UnifiedWorkerPool

    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    gate = _sandbox(monkeypatch)
    _deny_client_terminal(pane, gate, monkeypatch)
    bridge = _load_bridge()
    target = _pi_forward_target(project, pane, binding)
    if selection == "live-id":
        target = _live_id_target(project, pane, binding)
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    captured: dict = {}
    monkeypatch.setattr(
        bridge, "_send_to_daemon",
        lambda submitted, *_a, **_k: (captured.update(target=submitted), (0, "ok", {}))[1],
    )
    argv = ["--target", "1", "--provider", "pi"]
    if selection == "live-id":
        argv += ["--live-id", "pi-live-1"]
    argv.append("hello")
    try:
        assert bridge.main(argv) == 0
        destination = captured["target"]["peer_destination"]
        # The identity travelled; the witness did NOT (this caller cannot read the pane).
        assert destination["pane_id"] == pane.pane_id
        assert destination["terminal"] == pane.terminal
        assert destination["ccb_project_id"] == compute_ccb_project_id(project)
        assert destination["pane_title_marker"] == pane.pane_title_marker
        assert destination["launch_id"] == binding["session_id"]
        assert not destination.get("pane_pid")

        # Now the HOST admits it: the same request, with the terminal readable again.
        gate["client_probe_raises"] = False
        from askd.adapters.base import PeerDestination, ProviderRequest

        request = ProviderRequest(
            client_id="peer", work_dir=str(project), timeout_s=5.0, quiet=True,
            message="forwarded", caller="codex", delivery_only=True,
            peer_destination=PeerDestination(**destination),
            peer_delivery=True,
        )
        queued: list = []

        class _Registry:
            def get(self, _provider):
                return object()

        pool = _UnifiedWorkerPool(_Registry())
        monkeypatch.setattr(
            pool, "_get_pool",
            lambda provider: type(
                "P", (), {"get_or_create": lambda _self, _key, _factory: type(
                    "W", (), {"enqueue": staticmethod(queued.append)}
                )()}
            )(),
        )
        task = pool.submit("pi", request)
        assert queued == [task]
        assert task.request.peer_destination.pane_pid == pane.pane_process
    finally:
        pane.stop()


def test_a_sandboxed_forward_send_the_daemon_cannot_witness_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    from askd.adapters.base import PeerDestination, ProviderRequest
    from askd.daemon import _UnifiedWorkerPool

    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    gate = _sandbox(monkeypatch)
    _deny_client_terminal(pane, gate, monkeypatch)
    pane.pane_process = ""  # the host cannot read it either
    # The daemon runs on the HOST: the pane is reachable, but its process is still unreadable.
    gate["client_probe_raises"] = False

    class _Registry:
        def get(self, _provider):
            return object()

    pool = _UnifiedWorkerPool(_Registry())
    monkeypatch.setattr(
        pool, "_get_pool",
        lambda provider: pytest.fail("nothing may be queued"),
    )
    request = ProviderRequest(
        client_id="peer", work_dir=str(project), timeout_s=5.0, quiet=True,
        message="forwarded", caller="codex", delivery_only=True, peer_delivery=True,
        peer_destination=PeerDestination(
            pane_id=pane.pane_id, terminal=pane.terminal, work_dir=str(project),
            ccb_project_id=compute_ccb_project_id(project),
            pane_title_marker=pane.pane_title_marker, launch_id=binding["session_id"],
        ),
    )
    try:
        task = pool.submit("pi", request)
        assert task.done_event.is_set()
        assert task.result is not None and task.result.exit_code == 1
        assert "cannot be bound" in task.result.reply
    finally:
        pane.stop()


# --------------------------------------------------------------------------- #
# G3: a zero / non-finite timeout is refused at the real RPC ingress
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("timeout_s", [0, 0.0, "nan", "inf", float("nan")], ids=str)
def test_the_daemon_ingress_refuses_a_zero_or_non_finite_pi_peer_timeout(
    tmp_path: Path, monkeypatch, timeout_s
) -> None:
    pool = _RecordingPool()
    daemon = _daemon_with_pool(pool)
    monkeypatch.setattr(
        "peer_routing.revalidate_peer_destination", lambda _destination: _OK_RESOLUTION
    )

    response = daemon._handle_request({
        "type": "ask.request",
        "v": 1,
        "id": "client-timeout",
        "token": _DAEMON_TOKEN,
        "provider": "pi",
        "work_dir": str(tmp_path),
        "timeout_s": timeout_s,
        "message": "hello",
        "caller": "claude",
        "delivery_only": True,
        "peer_delivery": True,
        "peer_destination": {
            "pane_id": "%12",
            "terminal": "tmux",
            "work_dir": str(tmp_path),
            "ccb_project_id": "abcd1234",
            "pane_title_marker": "Pi-marker",
        },
    })

    assert response["exit_code"] == 1
    assert response["accepted"] is False
    assert "Pi peer delivery needs" in response["reply"]
    assert pool.submitted == []


def test_the_daemon_ingress_keeps_the_historical_default_when_the_timeout_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """Only a SUPPLIED bad value is refused; a missing one keeps today's 300 s."""
    pool = _RecordingPool()
    daemon = _daemon_with_pool(pool)
    monkeypatch.setattr(
        "peer_routing.revalidate_peer_destination", lambda _destination: _OK_RESOLUTION
    )

    response = daemon._handle_request({
        "type": "ask.request",
        "v": 1,
        "id": "client-default",
        "token": _DAEMON_TOKEN,
        "provider": "pi",
        "work_dir": str(tmp_path),
        "message": "hello",
        "caller": "claude",
        "delivery_only": True,
        "peer_delivery": True,
        "peer_destination": {
            "pane_id": "%12",
            "terminal": "tmux",
            "work_dir": str(tmp_path),
            "ccb_project_id": "abcd1234",
            "pane_title_marker": "Pi-marker",
        },
    })

    assert response.get("accepted") is not False
    assert [provider for provider, _ in pool.submitted] == ["pi"]
    assert pool.submitted[0][1].timeout_s == 300.0


# --------------------------------------------------------------------------- #
# G4: the concurrency test must PROVE the pane lock, not merely pass
# --------------------------------------------------------------------------- #


def _lock_serialisation_outcome(
    tmp_path: Path, use_pane, monkeypatch, *, locking_disabled: bool
) -> tuple[list[tuple[str, int]], int, int]:
    """Run one peer delivery that HOLDS its paste, plus a competing local `ask pi`.

    Returns `(violations, pastes, competitor_confirms)`, where a violation is the competitor
    reaching its IN-LOCK confirmation (its second one) while the first paste is still open. With
    the lock working that is impossible; with `CCB_PANE_LOCK_DISABLE=1` it happens immediately,
    which is what makes the assertion in the test below able to fail.
    """
    from askd.adapters.pi import PiAdapter

    if locking_disabled:
        monkeypatch.setenv("CCB_PANE_LOCK_DISABLE", "1")
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 0.05)
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    pane.paste_gate = threading.Event()

    confirms: dict[str, int] = {}
    real_confirm = pi_confirm

    def recorder(bind, deadline=None, **kwargs) -> PiState:
        name = threading.current_thread().name
        confirms[name] = confirms.get(name, 0) + 1
        return real_confirm(bind, deadline, **kwargs)

    monkeypatch.setattr("pi_comm.confirm_pi_ready", recorder)
    monkeypatch.setattr("askd.adapters.pi._confirm_pi_ready", recorder)

    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)
    errors: list[BaseException] = []

    def peer() -> None:
        try:
            send_peer_message(
                _peer_task(project, pane, launch_id=binding["session_id"]), "pi", "pi:peer"
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def competitor() -> None:
        try:
            request = ProviderRequest(
                client_id="local", work_dir=str(project), timeout_s=10.0, quiet=True,
                message="competing work", caller="claude",
            )
            task = QueuedTask(
                request=request, created_ms=0, req_id="competitor",
                done_event=threading.Event(),
            )
            adapter.handle_task(task)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    competitor_thread = threading.Thread(target=competitor, name="competitor")
    peer_thread = threading.Thread(target=peer, name="peer")
    violations: list[tuple[str, int]] = []
    try:
        peer_thread.start()
        assert pane.paste_started.wait(timeout=10), "the first paste never started"
        competitor_thread.start()
        # While the first paste is open, the competitor must not reach its in-lock confirmation.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            count = confirms.get("competitor", 0)
            if count >= 2:
                violations.append(("competitor", count))
                break
            if not competitor_thread.is_alive():
                break
            time.sleep(0.02)
        pane.paste_gate.set()
        peer_thread.join(timeout=30)
        competitor_thread.join(timeout=30)
        assert errors == []
        return violations, len(pane.sent), confirms.get("competitor", 0)
    finally:
        pane.paste_gate.set()
        pane.stop()


def test_the_pane_lock_keeps_a_competing_sender_out_of_its_in_lock_confirm(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    violations, pastes, competitor_confirms = _lock_serialisation_outcome(
        tmp_path, use_pane, monkeypatch, locking_disabled=False
    )
    assert violations == []  # it never reached its in-lock confirm while the paste was held
    assert competitor_confirms >= 2  # ...and it DID reach it once the lock was released
    assert pastes == 2


def test_that_lock_check_can_fail_with_pane_locking_disabled(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    """The canary has teeth: without the lock the competitor enters its in-lock confirm at once."""
    violations, _pastes, competitor_confirms = _lock_serialisation_outcome(
        tmp_path, use_pane, monkeypatch, locking_disabled=True
    )
    assert violations, "the lock-disabled run must show the competitor reaching its in-lock confirm"
    assert competitor_confirms >= 2


# --------------------------------------------------------------------------- #
# H1: phase 1 has no Pi dispatch hook
# --------------------------------------------------------------------------- #


def test_a_pi_delivery_refuses_a_dispatch_hook_without_calling_it(
    tmp_path: Path, use_pane
) -> None:
    """H1: `before_dispatch` is a Phase 2 concern; a Pi delivery must refuse it outright."""
    pane = FakePiPane(tmp_path)
    project, binding = _project(tmp_path, pane)
    use_pane(pane)
    calls: list[int] = []
    try:
        result = send_peer_message(
            _peer_task(project, pane, launch_id=binding["session_id"]),
            "pi",
            "pi:peer",
            before_dispatch=lambda: calls.append(1),
        )
        assert result.exit_code == 1
        assert "no dispatch hook in this phase" in result.reply
        assert "nothing was sent" in result.reply
        assert calls == []
        assert pane.sent == []
    finally:
        pane.stop()


# --------------------------------------------------------------------------- #
# H2: the direct Pi reply envelope names the PARSED caller
# --------------------------------------------------------------------------- #


def test_a_direct_pi_reply_names_the_parsed_caller_in_its_envelope(
    tmp_path: Path, use_pane, monkeypatch
) -> None:
    """H2: with CCB_CALLER unset, `--caller claude` must still produce claude's reply provider."""
    pane = FakePiPane(tmp_path)
    project, _binding = _project(tmp_path, pane)
    use_pane(pane)
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "_load_targets", list)
    monkeypatch.delenv("CCB_CALLER", raising=False)
    _write_pi_caller_receipt(project, pane, task_id="task-caller")
    argv = list(_bridge_argv(project, "task-caller"))
    argv[argv.index("--caller") + 1] = "claude"
    try:
        assert bridge.main(argv) == 0
        assert len(pane.sent) == 1
        pasted = pane.sent[0][1]
        assert "CCB_REPLY_PROVIDER: claude" in pasted
        assert "CCB_REPLY_PROVIDER: pi" not in pasted
    finally:
        pane.stop()


# --------------------------------------------------------------------------- #
# H3: a timeout the daemon cannot wait for is refused at the RPC ingress
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "timeout_s",
    [10**400, 1e20],
    ids=["json-int-overflow", "float-beyond-timeout-max"],
)
def test_the_daemon_ingress_refuses_an_unwaitable_pi_peer_timeout(
    tmp_path: Path, monkeypatch, timeout_s
) -> None:
    pool = _RecordingPool()
    daemon = _daemon_with_pool(pool)
    monkeypatch.setattr(
        "peer_routing.revalidate_peer_destination", lambda _destination: _OK_RESOLUTION
    )

    response = daemon._handle_request({
        "type": "ask.request",
        "v": 1,
        "id": "client-huge-timeout",
        "token": _DAEMON_TOKEN,
        "provider": "pi",
        "work_dir": str(tmp_path),
        "timeout_s": timeout_s,
        "message": "hello",
        "caller": "claude",
        "delivery_only": True,
        "peer_delivery": True,
        "peer_destination": {
            "pane_id": "%12",
            "terminal": "tmux",
            "work_dir": str(tmp_path),
            "ccb_project_id": "abcd1234",
            "pane_title_marker": "Pi-marker",
        },
    })

    assert response["exit_code"] == 1
    assert response["accepted"] is False
    assert "the daemon can wait for" in response["reply"]
    assert pool.submitted == []
