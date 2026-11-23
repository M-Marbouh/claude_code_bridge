# This repo puts lib/ on sys.path at runtime (test/conftest.py) and ships no pyright
# configuration, so the workspace-rooted analyzer cannot see these flat imports.
# pyright: reportMissingImports=false
"""Pi long turns: keep waiting while Pi works (progress mode).

Plan and ratification: docs/plans/pi-long-turn-wait-plan.md (B1-B5).

The decision under test is narrow: once a request's anchor is PROVEN in a
transcript, Pi may keep working past the nominal timeout as long as it keeps
producing evidence. Everything before the anchor keeps wall-clock semantics, so
the pre-send and queue-time behaviour is untouched.

Activity is deliberately strict (B1): a growing ANCHORED transcript, or a pane
that reads `busy`. An open `prompt` is classified before `busy` and is never
activity, so a stuck dialog cannot keep a request alive; and the absolute cap
applies even under continuous busy.

Two test-harness rules, learned the hard way:

* the fake clock is advanced from `time.monotonic` ITSELF, never only from a
  patched collaborator, so no code path can spin on a frozen clock;
* Pi must read `ready` during the PRE-SEND confirmation. A `busy`/`prompt`
  script from the very first read parks the adapter in the pre-send wait, which
  is a different (and deliberately wall-clock) path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from askd.adapters.base import ProviderRequest, QueuedTask
from askd.adapters.pi import PiAdapter
from pi_comm import PiState

FIXTURES = Path(__file__).parent / "fixtures" / "pi"
REAL_SHAPE = FIXTURES / "long_turn_growing.jsonl"

READY = PiState("ready")
BUSY = PiState("busy", "Pi is working on a request")
PROMPT = PiState("prompt", "Pi is waiting for a decision in its pane")

REQ_ID = "long-turn-1"
SESSION_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """No real sleeping; the clock is scripted per test."""
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 0.0)


class _Phase:
    """The backend under test, so the scripted reader can see its phase."""

    backend: FakeBackend | None = None


_PHASE = _Phase()


class FakeBackend:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        #: Flips once the prompt has been injected and its anchor written.
        self.sent_anchor = False

    def is_alive(self, _pane):
        return True

    def find_pane_by_title_marker(self, _marker, _cwd):
        return "%12"

    def send_text(self, pane, text):
        self.sent.append((pane, text))


class Clock:
    """A monotonic clock that ADVANCES ON ITS OWN on every read.

    Advancing here (rather than inside a patched collaborator) is what makes a
    frozen-clock spin impossible: every loop iteration reads the clock, so every
    iteration makes progress towards a deadline.
    """

    def __init__(self, step: float = 1.0):
        self.now = 0.0
        self.step = step
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        value = self.now
        self.now += self.step
        return value


def _states(monkeypatch, *, after_send, before_send=None) -> None:
    """Script Pi's readiness: `before_send` repeats until the anchor lands.

    The pre-send confirmation must see `ready`, so `before_send` defaults to it.
    `after_send` takes over once `send_text` has run (a list, whose last item
    repeats, or a callable).
    """
    import askd.adapters.pi as adapter_module

    pre = list(before_send or [READY]) or [READY]
    post = list(after_send) if isinstance(after_send, list) else [after_send]

    def take(script, index):
        item = script[index] if index < len(script) else script[-1]
        return item() if callable(item) else item

    counters = {"pre": 0, "post": 0}

    def _read(*_a, **_k) -> PiState:
        injected = bool(_PHASE.backend and _PHASE.backend.sent_anchor)
        if not injected:
            value = take(pre, counters["pre"])
            counters["pre"] += 1
            return value
        value = take(post, counters["post"])
        counters["post"] += 1
        return value

    def _confirm(*_a, **_k) -> PiState:
        # Mirrors production: a ready first read needs a second, also-ready read.
        first = _read()
        return first if first.status != "ready" else _read()

    monkeypatch.setattr(adapter_module, "_read_pi_state", _read)
    monkeypatch.setattr(adapter_module, "_confirm_pi_ready", _confirm)


def _binding(tmp_path: Path, session_dir: Path, session_id: str) -> dict:
    return {
        "active": True, "work_dir": str(tmp_path), "start_dir": str(tmp_path),
        "session_id": "launch-1", "pi_session_id": session_id,
        "pi_session_dir": str(session_dir), "pane_id": "%12",
        "pane_title_marker": "Pi-marker",
    }


def _task(work_dir: Path, *, timeout_s: float = -1, timeout_explicit: bool = False,
          idle_timeout_s: float | None = None, max_wait_s: float | None = None) -> QueuedTask:
    import threading

    request = ProviderRequest(
        client_id="test", work_dir=str(work_dir), timeout_s=timeout_s, quiet=True,
        message="do it", caller="claude", timeout_explicit=timeout_explicit,
        idle_timeout_s=idle_timeout_s, max_wait_s=max_wait_s,
    )
    return QueuedTask(
        request=request, created_ms=0, req_id=REQ_ID, done_event=threading.Event()
    )


def _real_rows(tmp_path: Path, req_id: str = REQ_ID) -> list[dict]:
    """The real transcript SHAPE, with its scrubbed cwd pointed at `tmp_path`."""
    rows = [
        json.loads(line)
        for line in REAL_SHAPE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows[0]["cwd"] = str(tmp_path)
    for row in rows:
        message = row.get("message")
        if isinstance(message, dict):
            for part in message.get("content") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = part["text"].replace(
                        "CCB_REQ_ID: long-turn-1", f"CCB_REQ_ID: {req_id}"
                    )
    return rows


def _setup(monkeypatch, tmp_path: Path):
    """A session that gains its ANCHOR only when the prompt is injected.

    Writing the anchor up front would (correctly) trip the "anchor already
    exists" refusal, so the transcript starts as header + system rows and the
    anchor is appended by `send_text`, exactly as Pi does after receiving input.
    """
    session_dir = tmp_path / "sessions"
    # exist_ok: a test may build more than one independent turn in one tmp_path.
    session_dir.mkdir(exist_ok=True)
    session_path = session_dir / f"2026-11-23T00-00-00.000Z_{SESSION_ID}.jsonl"
    rows = _real_rows(tmp_path)
    head, anchor = rows[:4], rows[4]
    session_path.write_text(
        "".join(json.dumps(row) + "\n" for row in head), encoding="utf-8"
    )

    binding = _binding(tmp_path, session_dir, SESSION_ID)
    backend = FakeBackend()
    monkeypatch.setattr(
        "askd.adapters.pi._load_destination",
        lambda _req: (tmp_path / ".pi-session", binding, ""),
    )
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_a: (backend, ""))
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task_, result, **_k: result)

    def send(pane, text):
        backend.sent.append((pane, text))
        if "CCB_REQ_ID:" not in session_path.read_text(encoding="utf-8"):
            with session_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(anchor) + "\n")
        backend.sent_anchor = True

    backend.send_text = send
    _PHASE.backend = backend
    return adapter, backend, session_path


def _row(entry_id: str, text: str, *, parent: str, stop: str = "stop") -> dict:
    return {
        "type": "message", "id": entry_id, "parentId": parent,
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": text}], "stopReason": stop},
    }


def _last_id(path: Path) -> str:
    """The newest row's id: each appended row must chain from the previous one."""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return str(rows[-1].get("id") or "")


def _append(path: Path, rows: list[dict]) -> None:
    """Append rows, re-chaining each to the row before it."""
    with path.open("a", encoding="utf-8") as handle:
        parent = _last_id(path)
        for row in rows:
            row = dict(row, parentId=parent)
            parent = str(row.get("id") or parent)
            handle.write(json.dumps(row) + "\n")


# --------------------------------------------------------------------------- #
# The control: the wall clock is unchanged where it must be
# --------------------------------------------------------------------------- #


def test_an_explicit_timeout_is_still_a_wall_clock(monkeypatch, tmp_path):
    """CONTROL: explicit --timeout keeps today's semantics, growth or not."""
    adapter, backend, session_path = _setup(monkeypatch, tmp_path)
    _states(monkeypatch, after_send=[BUSY])
    clock = Clock(step=1.0)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", clock)

    def is_alive(self, _pane):
        _append(session_path, [_row(f"grow{clock.reads}", f"step {clock.reads}", parent="-", stop="tool_use")])
        return True

    monkeypatch.setattr(FakeBackend, "is_alive", is_alive)

    result = adapter.handle_task(
        _task(tmp_path, timeout_s=10, timeout_explicit=True, idle_timeout_s=1000, max_wait_s=100000)
    )

    assert result.status == "incomplete"
    assert result.done_seen is False
    assert "timed out" in result.reply
    # Wall clock: it ended at the explicit budget, not at the idle window.
    assert clock.now < 1000


def test_no_supplied_idle_budget_means_no_progress_mode(monkeypatch, tmp_path):
    """B2: an older client (no idle_timeout_s) keeps the wall clock."""
    adapter, backend, session_path = _setup(monkeypatch, tmp_path)
    _states(monkeypatch, after_send=[BUSY])
    clock = Clock(step=1.0)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", clock)

    result = adapter.handle_task(_task(tmp_path, timeout_s=10))

    assert result.status == "incomplete"
    assert result.done_seen is False
    assert clock.now < 1000


# --------------------------------------------------------------------------- #
# Progress mode: the incident
# --------------------------------------------------------------------------- #


def test_a_growing_transcript_keeps_the_request_alive_past_the_timeout(monkeypatch, tmp_path):
    """The real incident: Pi works long past timeout_s, its transcript grows.

    The pane reads READY throughout (Pi's own report lags a turn that is still
    writing), so TRANSCRIPT GROWTH is the ONLY thing that can keep this request
    alive. With a busy pane this test would pass even if growth tracking were
    entirely broken.
    """
    adapter, backend, session_path = _setup(monkeypatch, tmp_path)
    _states(monkeypatch, after_send=[READY])
    clock = Clock(step=1.0)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", clock)

    def is_alive(self, _pane):
        # Each poll: the transcript grows, so the request must stay alive.
        _append(session_path, [
            _row(f"grow{clock.reads}", f"still working {clock.reads}", parent="-", stop="tool_use")
        ])
        if clock.reads >= 40:
            _append(session_path, [_row("final", f"All packages pass.\nCCB_DONE: {REQ_ID}", parent="-")])
        return True

    monkeypatch.setattr(FakeBackend, "is_alive", is_alive)

    # The nominal timeout is 10 s; the turn actually needs ~40 s of work.
    result = adapter.handle_task(
        _task(tmp_path, timeout_s=10, idle_timeout_s=30, max_wait_s=100000)
    )

    assert result.exit_code == 0, result.reply
    assert result.status == "completed"
    assert result.done_seen is True
    assert "All packages pass." in result.reply
    # The marker itself is stripped from the delivered reply.
    assert "CCB_DONE" not in result.reply
    assert clock.now > 10, "the request must have outlived its nominal timeout"


# --------------------------------------------------------------------------- #
# B1: idle window; prompt is not activity; the cap applies under busy
# --------------------------------------------------------------------------- #


def test_idle_and_static_ends_incomplete(monkeypatch, tmp_path):
    adapter, backend, session_path = _setup(monkeypatch, tmp_path)
    _states(monkeypatch, after_send=[READY])
    clock = Clock(step=1.0)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", clock)

    result = adapter.handle_task(
        _task(tmp_path, timeout_s=10, idle_timeout_s=5, max_wait_s=100000)
    )

    assert result.exit_code == 2
    assert result.status == "incomplete"
    assert result.done_seen is False
    assert "timed out" in result.reply
    # It ended on the IDLE window, not by running to the cap.
    assert clock.now < 100000


def test_a_busy_pane_keeps_the_request_alive_through_the_idle_window(monkeypatch, tmp_path):
    """B1 extra: static transcript + continuous busy past the idle window."""
    adapter, backend, session_path = _setup(monkeypatch, tmp_path)
    _states(monkeypatch, after_send=[BUSY])
    clock = Clock(step=1.0)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", clock)

    def is_alive(self, _pane):
        if clock.reads == 40:
            # Still busy at t≈360, well past the 120 s idle window; now done.
            _append(session_path, [_row("late", f"Done.\nCCB_DONE: {REQ_ID}", parent="-")])
        return True

    monkeypatch.setattr(FakeBackend, "is_alive", is_alive)

    result = adapter.handle_task(
        _task(tmp_path, timeout_s=10, idle_timeout_s=5, max_wait_s=100000)
    )

    assert result.exit_code == 0, result.reply
    assert result.done_seen is True
    assert clock.now > 5, "busy must keep refreshing the idle clock"


def test_an_open_prompt_does_not_refresh_the_idle_clock(monkeypatch, tmp_path):
    """B1: `prompt` is classified before busy, so it is never activity."""
    adapter, backend, session_path = _setup(monkeypatch, tmp_path)
    _states(monkeypatch, after_send=[PROMPT])
    clock = Clock(step=1.0)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", clock)

    result = adapter.handle_task(
        _task(tmp_path, timeout_s=10, idle_timeout_s=5, max_wait_s=100000)
    )

    assert result.status == "incomplete"
    assert result.done_seen is False
    assert clock.now < 60, "an open prompt must not hold the request open"


def test_the_absolute_cap_ends_a_continuously_busy_turn(monkeypatch, tmp_path):
    """B1 extra: continuous busy still ends at the cap."""
    adapter, backend, session_path = _setup(monkeypatch, tmp_path)
    _states(monkeypatch, after_send=[BUSY])
    clock = Clock(step=1.0)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", clock)

    result = adapter.handle_task(
        _task(tmp_path, timeout_s=10, idle_timeout_s=1000, max_wait_s=25)
    )

    assert result.status == "incomplete"
    assert result.done_seen is False
    assert clock.now >= 25, "the cap must be reached"
    assert clock.now < 200, "continuous busy must not outlive the cap"


# --------------------------------------------------------------------------- #
# B5: the drain at expiry
# --------------------------------------------------------------------------- #


def test_the_drain_at_expiry_catches_a_reply_written_in_the_last_gap(monkeypatch, tmp_path):
    """B5: the reply is only visible to the DRAIN, never to the loop's own read.

    The loop's final transcript read sees `pending`, so the turn is on its way to
    "expired". The reply is written into the file at exactly that moment, which
    is the poll gap a real Pi produces when it finishes just as the cap lands.
    Only the drain at expiry can still find it -- and it must.
    """
    adapter, backend, session_path = _setup(monkeypatch, tmp_path)
    _states(monkeypatch, after_send=[READY])
    clock = Clock(step=1.0)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", clock)

    import askd.adapters.pi as adapter_module

    # The LOOP must never observe a completion: force its view to stay pending.
    original_read = adapter_module._find_request_session
    loop_states: list[str | None] = []

    def read_always_pending(*args, **kwargs):
        result = original_read(*args, **kwargs)
        loop_states.append(getattr(result[1], "state", None))
        found, _exchange, error, candidates = result
        # Report `pending` regardless: this models the write landing after the
        # read, so the loop is always one step behind the file.
        from pi_comm import PiExchange

        return found, PiExchange("pending"), error, candidates

    monkeypatch.setattr(adapter_module, "_find_request_session", read_always_pending)

    drains: list[str | None] = []
    original_drain = adapter_module._read_bound_exchange

    def drain_then_land(*args, **kwargs):
        # Write the reply the instant the drain begins, so it reads fresh bytes.
        _append(
            session_path,
            [_row("gapdone", f"Late but done.\nCCB_DONE: {REQ_ID}", parent="-")],
        )
        found = original_drain(*args, **kwargs)
        drains.append(getattr(found, "state", None))
        return found

    monkeypatch.setattr(adapter_module, "_read_bound_exchange", drain_then_land)

    result = adapter.handle_task(
        _task(tmp_path, timeout_s=10, idle_timeout_s=100000, max_wait_s=30)
    )

    assert "completed" not in loop_states, (
        "the loop must never see the completion; otherwise this proves nothing"
    )
    assert drains, "the drain must have run at expiry"
    assert drains[-1] == "completed", "the drain must find the late reply"
    assert result.exit_code == 0, result.reply
    assert result.done_seen is True
    assert "Late but done." in result.reply


# --------------------------------------------------------------------------- #
# B2/B3: the caller and daemon budgets
# --------------------------------------------------------------------------- #


def test_the_caller_sends_pi_its_own_progress_budgets(monkeypatch):
    """B2: Pi's env names must be independent of Codex's."""
    import askd_timeout

    monkeypatch.setenv("CCB_CODEX_IDLE_TIMEOUT_S", "7")
    monkeypatch.setenv("CCB_CODEX_MAX_WAIT_S", "8")
    monkeypatch.setenv("CCB_PI_IDLE_TIMEOUT_S", "123")
    monkeypatch.setenv("CCB_PI_MAX_WAIT_S", "456")

    assert askd_timeout.codex_idle_timeout_s() == 7
    assert askd_timeout.codex_max_wait_s() == 8
    assert askd_timeout.pi_idle_timeout_s() == 123
    assert askd_timeout.pi_max_wait_s() == 456
    # The provider-keyed view agrees.
    assert askd_timeout.idle_timeout_s("pi") == 123
    assert askd_timeout.max_wait_s("pi") == 456
    assert askd_timeout.idle_timeout_s("codex") == 7


def test_pi_is_a_progress_mode_provider_and_hermes_stays_one():
    from providers import PROGRESS_MODE_PROVIDERS

    assert "pi" in PROGRESS_MODE_PROVIDERS
    assert "codex" in PROGRESS_MODE_PROVIDERS
    assert "hermes" in PROGRESS_MODE_PROVIDERS


def test_the_daemon_requires_a_supplied_idle_budget_for_pi(monkeypatch):
    """B2: an older Pi client (no idle_timeout_s) keeps the wall clock."""
    from types import SimpleNamespace

    from askd.adapters.base import ProviderResult
    from askd.daemon import UnifiedAskDaemon

    observed = {}

    class _WaitEvent:
        def wait(self, timeout=None):
            observed["wait_timeout"] = timeout
            return True

        def is_set(self):
            return True

    task = SimpleNamespace(
        req_id="task", done_event=_WaitEvent(),
        result=ProviderResult(exit_code=0, reply="ok", req_id="task", session_key="s", done_seen=True),
        cancelled=False, cancel_event=None,
    )

    class _Pool:
        def submit(self, provider, request, **kwargs):
            observed["request"] = request
            observed["submit_kwargs"] = kwargs
            return task

    daemon = UnifiedAskDaemon.__new__(UnifiedAskDaemon)
    daemon.registry = SimpleNamespace(get=lambda provider: object())
    daemon.pool = _Pool()

    # No idle_timeout_s at all: the wall clock, as for any older client.
    daemon._handle_request(
        {"id": "c", "provider": "pi", "caller": "claude", "timeout_s": 12,
         "timeout_explicit": False}
    )
    assert observed["request"].idle_timeout_s is None
    assert observed["request"].max_wait_s is None
    assert observed["wait_timeout"] == 17

    # Supplied: Pi's own progress budget, with Pi's env values.
    monkeypatch.setenv("CCB_PI_MAX_WAIT_S", "90")
    monkeypatch.setenv("CCB_PI_IDLE_TIMEOUT_S", "30")
    daemon._handle_request(
        {"id": "c", "provider": "pi", "caller": "claude", "timeout_s": 12,
         "timeout_explicit": False, "idle_timeout_s": 30, "max_wait_s": 90}
    )
    assert observed["request"].idle_timeout_s == 30
    assert observed["request"].max_wait_s == 90
    assert observed["wait_timeout"] == 95




# --------------------------------------------------------------------------- #
# F2 / B3: the queue phase and the turn phase have SEPARATE budgets
# --------------------------------------------------------------------------- #
#
# These use a FAKE CLOCK and a fake `done_event`, so the two phases are placed
# at exact times instead of slept through. That matters: the distinguishing case
# is a task claimed LATE in the queue window, whose turn then runs past the old
# `max_wait + 5` deadline.


class _FakeEvent:
    """A `done_event` whose wait advances the test's clock."""

    def __init__(self, clock, script):
        self.clock = clock
        self.script = list(script)  # one callable per wait
        self.waits: list[float | None] = []
        self._set = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        step = self.script.pop(0) if self.script else (lambda _ev: None)
        step(self)
        return self._set

    def set(self):
        self._set = True

    def is_set(self):
        return self._set


def _daemon_for(task, submitted=None):
    from types import SimpleNamespace

    from askd.daemon import UnifiedAskDaemon

    record: dict = submitted if submitted is not None else {}

    class _Pool:
        def submit(self, provider, request, **kwargs):
            # ITEM 2: the daemon must pass the budgets INTO submit, so they are
            # set before the worker can claim. Record what it was given.
            task.worker_cap_s = kwargs.get("worker_cap_s")
            task.queue_deadline = kwargs.get("queue_deadline")
            record.update(kwargs)
            return task

    daemon = UnifiedAskDaemon.__new__(UnifiedAskDaemon)
    daemon.registry = SimpleNamespace(get=lambda provider: object())
    daemon.pool = _Pool()
    return daemon


def _queued_task(*, work_dir: str, timeout_s: float, timeout_explicit: bool, idle_timeout_s):
    import threading

    from askd.adapters.base import ProviderRequest, QueuedTask

    request = ProviderRequest(
        client_id="c", work_dir=work_dir, timeout_s=timeout_s, quiet=True,
        message="m", caller="claude", timeout_explicit=timeout_explicit,
        idle_timeout_s=idle_timeout_s,
        max_wait_s=idle_timeout_s,
    )
    return QueuedTask(
        request=request, created_ms=0, req_id="task", done_event=threading.Event(),
        cancel_event=threading.Event(),
    )


def test_a_task_claimed_late_still_gets_its_whole_turn(monkeypatch, tmp_path):
    """The ratifier probe, with the clock placed explicitly.

    cap 100 s, claimed at 90 s, turn needs 30 s -> due at 120 s. The old
    `max_wait + 5` (= 105 s) deadline cancelled it mid-turn; the two-phase
    budget gives the claimed turn its own allowance from the claim.
    """
    from askd.adapters.base import ProviderResult

    clock = {"now": 0.0}
    monkeypatch.setattr("askd.daemon.time.monotonic", lambda: clock["now"])

    task = _queued_task(work_dir=str(tmp_path), timeout_s=12, timeout_explicit=False, idle_timeout_s=100)

    def first_wait(event):
        # The worker claims at t=90 and the turn is still running at t=105.
        clock["now"] = 90.0
        with task.claim_lock:
            task.started = True
            task.claimed_at = 90.0
        clock["now"] = 105.0

    def second_wait(event):
        # The turn finishes at t=120, inside its own 100 s allowance.
        clock["now"] = 120.0
        task.result = ProviderResult(
            exit_code=0, reply="finished", req_id="task", session_key="s", done_seen=True
        )
        event.set()

    event = _FakeEvent(clock, [first_wait, second_wait])
    task.done_event = event

    response = _daemon_for(task)._handle_request(
        {
            "id": "c", "provider": "pi", "caller": "claude", "timeout_s": 12,
            "timeout_explicit": False, "idle_timeout_s": 100, "max_wait_s": 100,
        }
    )

    assert response["exit_code"] == 0
    assert response["reply"] == "finished"
    # TWO distinct budgets were used: the queue window, then the turn allowance.
    assert len(event.waits) == 2
    assert event.waits[0] == 105.0          # queue phase: cap + 5
    assert event.waits[1] == 90.0 + 100.0 - 105.0 + 5.0  # turn: from the CLAIM
    # The budget was announced to the worker before any claim could happen.
    assert task.worker_cap_s == 100


def test_an_unclaimed_task_expires_unsent(monkeypatch, tmp_path):
    """Nobody claimed it: the caller is told, and the worker will skip it."""
    clock = {"now": 0.0}
    monkeypatch.setattr("askd.daemon.time.monotonic", lambda: clock["now"])

    task = _queued_task(work_dir=str(tmp_path), timeout_s=12, timeout_explicit=False, idle_timeout_s=5)

    def first_wait(event):
        clock["now"] = 6.0  # past the queue window, never claimed

    event = _FakeEvent(clock, [first_wait])
    task.done_event = event

    response = _daemon_for(task)._handle_request(
        {
            "id": "c", "provider": "pi", "caller": "claude", "timeout_s": 12,
            "timeout_explicit": False, "idle_timeout_s": 5, "max_wait_s": 5,
        }
    )

    assert response["exit_code"] == 2
    assert task.cancelled is True
    # Fix 2 item 1: the expiry is FINALIZED, so there is a real result (and a
    # non-empty reply) instead of the old silent None.
    assert task.result is not None
    assert "not sent" in task.result.reply.lower()
    assert response["reply"], "the direct reply must never be empty"
    assert "not sent" in response["reply"].lower()
    # Exactly ONE wait: an unclaimed task gets no turn allowance at all.
    assert len(event.waits) == 1


def test_an_explicit_timeout_keeps_one_wall_clock_budget(monkeypatch, tmp_path):
    """No blanket allowance: explicit --timeout is one budget, as before."""
    clock = {"now": 0.0}
    monkeypatch.setattr("askd.daemon.time.monotonic", lambda: clock["now"])

    task = _queued_task(work_dir=str(tmp_path), timeout_s=30, timeout_explicit=True, idle_timeout_s=None)

    def first_wait(event):
        clock["now"] = 31.0
        with task.claim_lock:  # claimed, but an explicit request gets no turn budget
            task.started = True
            task.claimed_at = 30.0

    event = _FakeEvent(clock, [first_wait])
    task.done_event = event

    _daemon_for(task)._handle_request(
        {
            "id": "c", "provider": "pi", "caller": "claude", "timeout_s": 30,
            "timeout_explicit": True,
        }
    )

    assert len(event.waits) == 1
    assert task.worker_cap_s is None
    assert task.queue_deadline is None


def test_a_non_progress_provider_keeps_its_wait(monkeypatch, tmp_path):
    """Codex with no progress budget is untouched by F2."""
    clock = {"now": 0.0}
    monkeypatch.setattr("askd.daemon.time.monotonic", lambda: clock["now"])

    task = _queued_task(work_dir=str(tmp_path), timeout_s=20, timeout_explicit=True, idle_timeout_s=None)

    event = _FakeEvent(clock, [lambda _e: None])
    task.done_event = event

    _daemon_for(task)._handle_request(
        {
            "id": "c", "provider": "codex", "caller": "claude", "timeout_s": 20,
            "timeout_explicit": True,
        }
    )

    assert len(event.waits) == 1
    assert task.worker_cap_s is None
    assert task.queue_deadline is None


# --------------------------------------------------------------------------- #
# B5: the drain reads the PROVEN transcript, byte-level
# --------------------------------------------------------------------------- #


def _session_file(tmp_path: Path) -> tuple[Path, list[dict]]:
    """A real-shape session file whose anchor is already present."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    rows = _real_rows(tmp_path)
    path = session_dir / f"2026-11-23T00-00-00.000Z_{SESSION_ID}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path, rows


def test_a_trailing_partial_utf8_line_is_skipped_not_fatal(tmp_path):
    """The file is still being written: the last line may be half a character."""
    path, _rows = _session_file(tmp_path)
    raw = path.read_bytes()
    # A truncated multi-byte sequence, exactly as a mid-write read produces.
    path.write_bytes(raw + b'{"type":"message","id":"half","parentId":"a0000007","message":{"role":"assistant","content":[{"type":"text","text":"caf\xc3')

    from pi_comm import load_session_path

    session = load_session_path(path, SESSION_ID)

    # The complete rows are read; the partial one is simply absent.
    assert session.session_id == SESSION_ID
    assert all(row.get("id") != "half" for row in session.entries)


def test_a_replaced_header_id_is_not_trusted(tmp_path):
    """A header rewritten after the anchor must not make the drain read it."""
    path, rows = _session_file(tmp_path)
    rows[0] = dict(rows[0], id="11111111-1111-4111-8111-111111111111")
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    from pi_comm import PiSessionError, load_session_path

    with pytest.raises(PiSessionError) as raised:
        load_session_path(path, SESSION_ID)
    assert "header identity does not match" in str(raised.value)


def test_the_drain_uses_the_identity_proven_at_the_anchor(monkeypatch, tmp_path):
    """The adapter passes the ANCHORED id, never one re-read from the file."""
    adapter, _backend, session_path = _setup(monkeypatch, tmp_path)
    _states(monkeypatch, after_send=[READY])
    clock = Clock(step=1.0)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", clock)

    import askd.adapters.pi as adapter_module

    seen = []
    original = adapter_module._read_bound_exchange

    def spy(*args, **kwargs):
        seen.append(kwargs.get("bound_session_id"))
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter_module, "_read_bound_exchange", spy)

    result = adapter.handle_task(
        _task(tmp_path, timeout_s=10, idle_timeout_s=2, max_wait_s=100000)
    )

    assert result.status == "incomplete"
    assert seen, "the drain must run at expiry"
    # The PROVEN id, not "" and not a freshly parsed one.
    assert seen[0] == SESSION_ID


def test_the_drain_never_decodes_the_whole_file_as_text(tmp_path, monkeypatch):
    """B5: parsing is byte/line based, so a growing file cannot abort the read."""
    import pi_comm

    path, _rows = _session_file(tmp_path)
    decoded = []
    real_read_text = Path.read_text

    def spy_read_text(self, *args, **kwargs):
        decoded.append(self)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy_read_text)

    session = pi_comm.load_session_path(path, SESSION_ID)

    assert session.session_id == SESSION_ID
    # The transcript itself is never decoded as one text blob.
    assert path not in decoded


# --------------------------------------------------------------------------- #
# Fix 2 items 2 and 3
# --------------------------------------------------------------------------- #


def test_the_budgets_are_attached_before_the_task_is_enqueued(monkeypatch, tmp_path):
    """ITEM 2: a worker that claims INSTANTLY must already see its budgets.

    `pool.submit` hands the task to a worker immediately, and the async path
    returns before any later assignment. Setting the fields afterwards left a
    fast claim with `queue_deadline=None`.
    """
    from askd.adapters.base import ProviderResult

    clock = {"now": 0.0}
    monkeypatch.setattr("askd.daemon.time.monotonic", lambda: clock["now"])

    task = _queued_task(
        work_dir=str(tmp_path), timeout_s=12, timeout_explicit=False, idle_timeout_s=100
    )
    submitted: dict = {}
    daemon = _daemon_for(task, submitted)

    # The worker claims the instant it gets the task.
    def immediate_wait(event):
        assert task.queue_deadline is not None, "budget must exist BEFORE the claim"
        assert task.worker_cap_s == 100
        with task.claim_lock:
            task.started = True
            task.claimed_at = clock["now"]
        task.result = ProviderResult(
            exit_code=0, reply="fast", req_id="task", session_key="s", done_seen=True
        )
        event.set()

    task.done_event = _FakeEvent(clock, [immediate_wait])

    response = daemon._handle_request(
        {
            "id": "c", "provider": "pi", "caller": "claude", "timeout_s": 12,
            "timeout_explicit": False, "idle_timeout_s": 100, "max_wait_s": 100,
        }
    )

    assert response["exit_code"] == 0
    # The budgets travelled INTO submit, not assigned afterwards.
    assert submitted.get("worker_cap_s") == 100
    assert submitted.get("queue_deadline") is not None


def test_the_async_path_also_carries_the_deadline(monkeypatch, tmp_path):
    """ITEM 2: async_submit returns early, so the deadline must already be set."""
    task = _queued_task(
        work_dir=str(tmp_path), timeout_s=12, timeout_explicit=False, idle_timeout_s=100
    )
    submitted: dict = {}
    daemon = _daemon_for(task, submitted)

    response = daemon._handle_request(
        {
            "id": "c", "provider": "pi", "caller": "claude", "timeout_s": 12,
            "timeout_explicit": False, "idle_timeout_s": 100, "max_wait_s": 100,
            "async_submit": True,
        }
    )

    assert response["accepted"] is True
    assert submitted.get("worker_cap_s") == 100
    assert submitted.get("queue_deadline") is not None


def test_a_cap_expiring_during_pre_send_means_zero_sends(monkeypatch, tmp_path):
    """ITEM 3: pro-progress pre-send uses the WORKER deadline throughout."""
    adapter, backend, session_path = _setup(monkeypatch, tmp_path)
    # Pi is busy for the whole pre-send phase, so no injection ever happens.
    _states(monkeypatch, after_send=[BUSY], before_send=[BUSY])

    # A fine step so the two candidate deadlines (5 s cap vs 10 s nominal)
    # differ by many reads, making the choice observable.
    clock = Clock(step=0.5)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", clock)

    task = _task(tmp_path, timeout_s=10, idle_timeout_s=30, max_wait_s=5)
    # A claimed turn: its cap is counted from the claim.
    task.claimed_at = 0.0
    task.worker_cap_s = 5

    result = adapter.handle_task(task)

    # ITEM 3: nothing was ever injected, so this is the UNSENT outcome (exit 1,
    # cancelled, the agreed sentence) -- not "timed out before a CCB_DONE" about
    # a request Pi never saw.
    assert result.exit_code == 1
    assert result.status == "cancelled"
    assert "not sent" in result.reply.lower()
    assert backend.sent == [], "a cap that ran out before injection sends nothing"
    # ITEM 3's own point: the pre-send phase was bounded by the WORKER cap
    # (claim 0 + 5 s), not by `max(nominal timeout, cap)` = 10 s. With the cap
    # ignored, the wait would have run well past 5 s before giving up.
    assert clock.now < 10.0, (
        f"pre-send must stop at the worker cap (5 s), but it ran to {clock.now}s "
        "-- that is the nominal timeout winning instead of the cap"
    )


def _expiry_task(tmp_path, req_id: str = "late"):
    """A task whose queue deadline has already passed, with the REAL finalizer."""
    import threading
    import time as _time

    from askd.adapters.base import ProviderRequest, QueuedTask

    request = ProviderRequest(
        client_id="c", work_dir=str(tmp_path), timeout_s=10, quiet=True,
        message="m", caller="claude", timeout_explicit=False, idle_timeout_s=30,
    )
    task = QueuedTask(
        request=request, created_ms=0, req_id=req_id, done_event=threading.Event(),
        cancel_event=threading.Event(), provider_key="pi",
    )
    task.queue_deadline = _time.monotonic() - 1  # already past
    task.worker_cap_s = 10
    return task


def _real_finalizer_edges(monkeypatch):
    """Stub ONLY the finalizer's edges: persistence, notification and logging.

    The finalizer ITSELF is the unit under test, so it must run for real. An
    earlier version of these tests replaced `finalize_unsent` outright, which is
    exactly why they could not see the claim-lock deadlock inside it.
    """
    import askd.unsent as unsent_module

    calls: list[tuple[str, dict]] = []

    def fake_persist_and_notify(provider, request, **kwargs):
        calls.append(("persist", dict(kwargs)))

    monkeypatch.setattr(unsent_module, "persist_and_notify", fake_persist_and_notify)
    monkeypatch.setattr(unsent_module, "_log", lambda _line: None)
    return calls


def test_the_worker_finalizes_its_own_queue_expiry(tmp_path, monkeypatch):
    """P1 DEADLOCK: the REAL finalizer, called from the worker's claim path.

    The worker used to call `finalize_unsent` while HOLDING `claim_lock`, and
    the finalizer takes that same non-reentrant lock -- so the worker hung
    forever and the daemon could block behind it. This test runs the real
    finalizer (only persistence/notification/logging stubbed) under the suite's
    own per-test alarm, so a regression fails fast instead of hanging.
    """
    import queue
    import threading

    from askd.adapters.base import ProviderResult
    from worker_pool import BaseSessionWorker

    calls = _real_finalizer_edges(monkeypatch)
    delivered: list[str] = []

    class _Worker(BaseSessionWorker):
        def _handle_task(self, task):
            delivered.append(task.req_id)
            return ProviderResult(exit_code=0, reply="", req_id=task.req_id, session_key="s")

    worker = _Worker.__new__(_Worker)
    worker._stop_event = threading.Event()
    worker._q = queue.Queue()
    worker._current = None
    worker._current_started_at = None
    worker.session_key = "s"

    task = _expiry_task(tmp_path)
    worker._q.put(task)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        # The conftest alarm bounds this; a deadlock can never reach the assert.
        assert task.done_event.wait(timeout=5), "worker deadlocked in the finalizer"
    finally:
        worker._stop_event.set()
        thread.join(timeout=2)

    assert delivered == [], "an expired task is never injected"
    assert task.result is not None, "a real result, not the old silent None"
    assert "not sent" in task.result.reply.lower()
    # The REAL finalizer recorded exactly once.
    assert [call[0] for call in calls] == ["persist"]


def test_the_finalizer_takes_its_own_lock(tmp_path, monkeypatch):
    """P1 DEADLOCK, asserted directly: the caller must NOT hold `claim_lock`.

    A named regression for the exact shape that shipped. `claim_lock` is a plain
    `threading.Lock`, so re-entry is a self-deadlock; the finalizer takes it
    itself, and this test proves a lone caller completes rather than hangs.
    """
    import threading

    from askd.unsent import finalize_unsent

    calls = _real_finalizer_edges(monkeypatch)
    task = _expiry_task(tmp_path, req_id="lock-shape")

    result_box: dict = {}
    done = threading.Event()

    def call_outside():
        result_box["value"] = finalize_unsent(task, "pi")
        done.set()

    thread = threading.Thread(target=call_outside, daemon=True)
    thread.start()
    assert done.wait(timeout=5), "finalize_unsent deadlocked on its own lock"
    assert result_box["value"] is task.result
    assert result_box["value"].reply
    assert [call[0] for call in calls] == ["persist"]


def test_two_concurrent_finalizers_publish_one_result(tmp_path, monkeypatch):
    """P1 OWNERSHIP vs PUBLICATION: a loser must never see None.

    Ownership used to be published (`unsent_finalized=True`) while the result
    was still being built, so a second caller could observe "already finalised",
    get None, and the daemon turned that into an empty reply. Now the result is
    set in the SAME critical section as ownership.

    The race is made deterministic by pausing the winner at the exact moment it
    publishes: the loser is started there, so it either sees a result (fixed) or
    sees none (the shipped bug). Only result CONSTRUCTION is stubbed -- the
    ownership logic under test runs for real.
    """
    import threading

    import askd.unsent as unsent_module
    from askd.unsent import finalize_unsent

    calls: list[tuple[str, dict]] = []
    building = threading.Event()
    release = threading.Event()
    real_build = unsent_module._build_result

    def gated_build(task, provider, *, budget_s, reason):
        building.set()
        release.wait(timeout=5)
        return real_build(task, provider, budget_s=budget_s, reason=reason)

    def recording_persist(provider, request, **kwargs):
        calls.append(("persist", dict(kwargs)))

    monkeypatch.setattr(unsent_module, "_build_result", gated_build)
    monkeypatch.setattr(unsent_module, "persist_and_notify", recording_persist)
    monkeypatch.setattr(unsent_module, "_log", lambda _line: None)

    task = _expiry_task(tmp_path, req_id="race")
    results: dict = {}

    def winner():
        results["winner"] = finalize_unsent(task, "pi")

    thread = threading.Thread(target=winner, daemon=True)
    thread.start()
    assert building.wait(timeout=5), "the winner must reach result construction"

    # The winner has claimed ownership and is publishing RIGHT NOW. Race it.
    loser_box: dict = {}
    loser = threading.Thread(
        target=lambda: loser_box.__setitem__("value", finalize_unsent(task, "pi")),
        daemon=True,
    )
    loser.start()
    # Give the loser a moment to act IN the window. Under the fix the winner
    # still holds the claim lock here, so the loser waits; under the shipped
    # bug the lock was already free and the loser reads a result-less claim.
    loser.join(timeout=0.3)
    release.set()
    loser.join(timeout=5)
    thread.join(timeout=5)

    value = loser_box.get("value")
    assert value is not None, (
        "the racing caller saw None -- ownership was published before the result"
    )
    assert results["winner"] is value, "both callers must get the SAME result"
    assert value.reply, "the result is never empty"
    # Exactly ONE record/notification.
    assert [call[0] for call in calls] == ["persist"]


def test_a_held_pane_lock_ends_unsent_within_the_cap(monkeypatch, tmp_path):
    """ITEM 3: the lock wait is bounded by the remaining worker budget.

    Another holder owns the pane-injection lock. Without a bound the request
    waits past its cap and then injects anyway; with it, the turn ends UNSENT
    inside the cap and nothing is ever sent.
    """
    import pane_lock

    adapter, backend, _session_path = _setup(monkeypatch, tmp_path)
    _states(monkeypatch, after_send=[READY])
    clock = Clock(step=0.5)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", clock)

    seen_budgets: list[float | None] = []

    class _HeldLock:
        """A pane lock another process already owns."""

        def __init__(self, budget=None):
            self.budget = budget

        def __enter__(self):
            # Waiting for a held lock consumes the budget it was given; then it
            # gives up. That is what a real held lock does to the caller.
            clock.now += float(self.budget or 0.0)
            raise pane_lock.PaneLockError("pane lock is held by another process")

        def __exit__(self, *_exc):
            return False

    def fake_lock(_backend, _pane, timeout_s=None):
        seen_budgets.append(timeout_s)
        return _HeldLock(timeout_s)

    # A small step keeps the pre-send bookkeeping inside the 5 s cap while still
    # letting the clock reach it, so the test deterministically gets AS FAR AS
    # the lock (a frozen clock would spin forever in the wait loops).
    clock.step = 0.001

    monkeypatch.setattr(pane_lock, "pane_injection_lock", fake_lock, raising=False)

    task = _task(tmp_path, timeout_s=10, idle_timeout_s=30, max_wait_s=5)
    task.claimed_at = 0.0
    task.worker_cap_s = 5

    result = adapter.handle_task(task)

    assert backend.sent == [], "a held lock must never lead to a send"
    assert result.exit_code == 1
    assert result.status == "cancelled"
    assert "not sent" in result.reply.lower()
    # The lock was given the REMAINING budget, not the lock module's default.
    assert seen_budgets and seen_budgets[0] is not None
    assert seen_budgets[0] <= 5.0


# --------------------------------------------------------------------------- #
# Fix 4 amendments (A1-A3)
# --------------------------------------------------------------------------- #


def _held_lock_case(monkeypatch, tmp_path, *, timeout_s, claimed_at, cap):
    """Run a turn against a pane lock another holder owns; return the budgets seen."""
    import pane_lock

    adapter, backend, _session_path = _setup(monkeypatch, tmp_path)
    _states(monkeypatch, after_send=[READY])
    clock = Clock(step=0.001)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", clock)

    seen: list[float | None] = []

    class _HeldLock:
        def __init__(self, budget=None):
            self.budget = budget

        def __enter__(self):
            clock.now += float(self.budget or 0.0)
            raise pane_lock.PaneLockError("pane lock is held by another process")

        def __exit__(self, *_exc):
            return False

    def fake_lock(_backend, _pane, timeout_s=None):
        seen.append(timeout_s)
        return _HeldLock(timeout_s)

    monkeypatch.setattr(pane_lock, "pane_injection_lock", fake_lock, raising=False)

    task = _task(
        tmp_path,
        timeout_s=timeout_s,
        timeout_explicit=claimed_at is None,
        idle_timeout_s=cap,
    )
    if claimed_at is not None:
        task.claimed_at = claimed_at
        task.worker_cap_s = cap
    return adapter, backend, task, seen


def test_an_explicit_request_keeps_the_configured_lock_timeout(monkeypatch, tmp_path):
    """A1: a non-progress request gets `default_timeout_s()`, not the turn budget.

    With a 3 s configured lock limit, an explicit request must give up at ~3 s.
    Narrowing it to the turn budget (the shipped bug) made a 50 s turn wait ~50 s
    on the lock instead.
    """
    monkeypatch.setenv("CCB_PANE_LOCK_TIMEOUT_S", "3")
    adapter, backend, task, seen = _held_lock_case(
        monkeypatch, tmp_path, timeout_s=50, claimed_at=None, cap=None
    )

    result = adapter.handle_task(task)

    assert seen and seen[0] == 3.0, f"expected the configured 3 s, got {seen}"
    assert backend.sent == []
    assert result.exit_code == 1


def test_a_progress_request_takes_the_min_of_lock_timeout_and_budget(monkeypatch, tmp_path):
    """A1: progress mode uses min(configured lock timeout, remaining budget)."""
    monkeypatch.setenv("CCB_PANE_LOCK_TIMEOUT_S", "3")

    # Budget far larger than the lock limit -> the lock limit wins.
    adapter, _backend, task, seen = _held_lock_case(
        monkeypatch, tmp_path, timeout_s=-1, claimed_at=0.0, cap=100
    )
    adapter.handle_task(task)
    assert seen and seen[0] is not None and seen[0] <= 3.0, f"at most the 3 s limit, got {seen}"

    # Budget far smaller than the lock limit -> the budget wins.
    adapter2, _b2, task2, seen2 = _held_lock_case(
        monkeypatch, tmp_path, timeout_s=-1, claimed_at=0.0, cap=2
    )
    adapter2.handle_task(task2)
    assert seen2 and seen2[0] is not None and seen2[0] <= 2.0, f"at most the 2 s budget, got {seen2}"


def test_the_unsent_sentence_names_the_actual_queue_cap_for_pi(monkeypatch, tmp_path):
    """A2: the finalizer selects the Pi sentence and never quotes the nominal timeout."""
    import askd.unsent as unsent_module
    from askd.unsent import finalize_unsent

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        unsent_module, "persist_and_notify",
        lambda provider, request, **kw: calls.append(("persist", dict(kw))),
    )
    monkeypatch.setattr(unsent_module, "_log", lambda _line: None)

    import threading
    import time as _time

    from askd.adapters.base import ProviderRequest, QueuedTask

    # The caller's NOMINAL timeout is an hour; the real queue cap is 5 s.
    request = ProviderRequest(
        client_id="c", work_dir=str(tmp_path), timeout_s=3600, quiet=True,
        message="m", caller="claude", timeout_explicit=False, idle_timeout_s=30,
    )
    task = QueuedTask(
        request=request, created_ms=0, req_id="cap", done_event=threading.Event(),
        cancel_event=threading.Event(), provider_key="pi",
    )
    task.queue_deadline = _time.monotonic() - 1
    task.worker_cap_s = 5

    result = finalize_unsent(task, "pi")

    assert result is not None
    assert result.reply == (
        "Pi request was not sent: CCB stopped waiting before Pi was free."
    )
    assert "3600" not in result.reply, "must never quote the nominal timeout"
    assert [call[0] for call in calls] == ["persist"]


def test_the_codex_sentence_quotes_the_queue_cap_not_the_nominal_timeout(monkeypatch, tmp_path):
    """A2: the generic sentence reports the ACTUAL cap it waited out."""
    import threading

    import askd.unsent as unsent_module
    from askd.adapters.base import ProviderRequest, QueuedTask
    from askd.unsent import finalize_unsent

    monkeypatch.setattr(unsent_module, "persist_and_notify", lambda *a, **k: None)
    monkeypatch.setattr(unsent_module, "_log", lambda _line: None)

    request = ProviderRequest(
        client_id="c", work_dir=str(tmp_path), timeout_s=3600, quiet=True,
        message="m", caller="claude",
    )
    task = QueuedTask(
        request=request, created_ms=0, req_id="cap2", done_event=threading.Event(),
        cancel_event=threading.Event(), provider_key="codex",
    )
    task.worker_cap_s = 5

    result = finalize_unsent(task, "codex")

    assert "(5 s)" in result.reply, result.reply
    assert "3600" not in result.reply


def test_readiness_that_consumed_the_cap_ends_unsent(monkeypatch, tmp_path):
    """A3: expiry is judged BEFORE the readiness verdict.

    The readiness confirmation polls and can spend the last of the budget; it
    then reports `stale`. Branching on that first produced a generic readiness
    failure for a request CCB simply stopped waiting for.
    """
    adapter, backend, _session_path = _setup(monkeypatch, tmp_path)
    clock = Clock(step=1.0)
    monkeypatch.setattr("askd.adapters.pi.time.monotonic", clock)

    import askd.adapters.pi as adapter_module

    def stale_after_cap(*_a, **_k):
        # The confirmation burns the whole budget, then cannot confirm.
        clock.now += 1000.0
        return adapter_module.PiState("stale", "Pi did not report twice in a row")

    monkeypatch.setattr(adapter_module, "_confirm_pi_ready", stale_after_cap)
    monkeypatch.setattr(adapter_module, "_read_pi_state", lambda *_a, **_k: adapter_module.PiState("ready"))

    task = _task(tmp_path, timeout_s=10, idle_timeout_s=30, max_wait_s=5)
    task.claimed_at = 0.0
    task.worker_cap_s = 5

    result = adapter.handle_task(task)

    assert backend.sent == [], "an expired readiness wait must never send"
    assert result.status == "cancelled", result.status
    assert result.reply == (
        "Pi request was not sent: CCB stopped waiting before Pi was free."
    )
