"""Fix 6: routed adapters take the pane-injection lock BEFORE their final
identity/role-policy check, and keep `send_text` re-entrant inside it."""

# This repo puts lib/ on sys.path at runtime (test/conftest.py) and ships no pyright
# configuration, so the workspace-rooted analyzer cannot see these flat imports.
# pyright: reportMissingImports=false

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import pane_lock
from askd.adapters.base import ProviderRequest, QueuedTask
from pi_comm import PiState

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "test" / "fixtures" / "pi"

#: Every adapter that performs a pre-send identity/policy check has to take the
#: same cross-process lock around it.
ADAPTERS = ("claude", "codex", "pi", "hermes", "opencode", "gemini")


# --------------------------------------------------------------------------- #
# Static shape guard (cheap and complete for all six)
# --------------------------------------------------------------------------- #


def _lock_comes_first(source: str) -> bool:
    lock = source.find("pane_injection_lock(")
    policy = source.find("refusal_for_request(")
    send = source.find("backend.send_text(")
    return -1 < lock < policy < send


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_adapter_takes_the_lock_before_the_policy_check_and_send(adapter: str):
    source = (REPO_ROOT / "lib" / "askd" / "adapters" / f"{adapter}.py").read_text(encoding="utf-8")
    assert "PaneLockError" in source, adapter
    assert _lock_comes_first(source), adapter


def test_the_shape_guard_detects_a_missing_lock():
    """Positive control for the guard above."""
    assert _lock_comes_first("pane_injection_lock(backend, pane)\nrefusal_for_request(...)\nbackend.send_text(...)")
    assert not _lock_comes_first("refusal_for_request(...)\nbackend.send_text(...)\n")
    assert not _lock_comes_first("refusal_for_request(...)\npane_injection_lock(...)\nbackend.send_text(...)")


# --------------------------------------------------------------------------- #
# Behavioural proof on one adapter (pi has the lightest harness)
# --------------------------------------------------------------------------- #


class _FakeBackend:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def is_alive(self, _pane):
        return True

    def find_pane_by_title_marker(self, _marker, _cwd):
        return "%12"

    def send_text(self, pane, text):
        self.sent.append((pane, text))


def _pi_task(work_dir: Path) -> QueuedTask:
    request = ProviderRequest(
        client_id="test", work_dir=str(work_dir), timeout_s=0.3, quiet=True,
        message="do it", caller="claude",
    )
    return QueuedTask(request=request, created_ms=0, req_id="pane-lock-req", done_event=threading.Event())


def _binding(tmp_path: Path) -> dict:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    return {
        "active": True, "work_dir": str(tmp_path), "start_dir": str(tmp_path),
        "session_id": "launch-1", "pi_session_id": "pi-1",
        "pi_session_dir": str(session_dir), "pane_id": "%12", "pane_title_marker": "Pi-marker",
    }


def _probe_lock_free(backend, pane: str, budget: float = 0.15) -> bool:
    """True when a DIFFERENT thread can take the pane lock right now."""
    result: dict[str, bool] = {}

    def probe() -> None:
        try:
            with pane_lock.pane_injection_lock(backend, pane, budget):
                result["free"] = True
        except pane_lock.PaneLockTimeout:
            result["free"] = False

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(10.0)
    return result["free"]


def _run_pi(monkeypatch, tmp_path, backend, *, probe=None, refusal=""):
    import delivery_policy

    from askd.adapters.pi import PiAdapter

    monkeypatch.setattr(
        "askd.adapters.pi._load_destination", lambda _req: (tmp_path / ".pi-session", _binding(tmp_path), "")
    )
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    monkeypatch.setattr("askd.adapters.pi.PRE_SEND_RETRY_WAIT_S", 0.0)
    monkeypatch.setattr("askd.adapters.pi.time.sleep", lambda _s: None)
    # This file proves lock/policy ordering: the pane always reports ready.
    monkeypatch.setattr("askd.adapters.pi._read_pi_state", lambda *_a, **_k: PiState("ready"))
    monkeypatch.setattr("askd.adapters.pi._confirm_pi_ready", lambda *_a, **_k: PiState("ready"))

    if probe is not None:
        def policy(*_args, **_kwargs):
            probe()
            return refusal
        monkeypatch.setattr(delivery_policy, "refusal_for_request", policy)
    else:
        monkeypatch.setattr(delivery_policy, "refusal_for_request", lambda *_a, **_k: refusal)

    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)
    return adapter.handle_task(_pi_task(tmp_path))


def test_policy_check_runs_while_the_pane_lock_is_held(monkeypatch, tmp_path):
    observed: dict[str, bool] = {}

    def probe() -> None:
        observed["lock_free"] = _probe_lock_free(backend, "%12")

    backend = _FakeBackend()
    _run_pi(monkeypatch, tmp_path, backend, probe=probe)

    # A concurrent sender could NOT get in while the policy check ran: the
    # check happens after the wait, inside the lock.
    assert observed["lock_free"] is False
    assert len(backend.sent) == 1


def test_policy_check_probe_can_see_a_free_lock(monkeypatch, tmp_path):
    """Positive control: with the lock off, the probe gets in during the check."""
    observed: dict[str, bool] = {}
    monkeypatch.setenv("CCB_PANE_LOCK_DISABLE", "1")

    def probe() -> None:
        observed["lock_free"] = _probe_lock_free(backend, "%12")

    backend = _FakeBackend()
    _run_pi(monkeypatch, tmp_path, backend, probe=probe)

    assert observed["lock_free"] is True
    assert len(backend.sent) == 1


def test_revoked_permission_after_the_wait_sends_nothing(monkeypatch, tmp_path):
    """The review's probe: permission revoked while the adapter waited."""
    backend = _FakeBackend()
    result = _run_pi(
        monkeypatch, tmp_path, backend,
        probe=lambda: None,
        refusal="role policy refused: external senders may only send to a session holding the lead role",
    )

    assert result.exit_code == 1
    assert "role policy refused" in result.reply
    assert backend.sent == []


def test_adapter_lock_acquisition_failure_sends_nothing(monkeypatch, tmp_path):
    backend = _FakeBackend()
    monkeypatch.setenv("CCB_PANE_LOCK_TIMEOUT_S", "0.1")
    release = threading.Event()
    started = threading.Event()

    def hold() -> None:
        with pane_lock.pane_injection_lock(backend, "%12", 5.0):
            started.set()
            release.wait(5.0)

    thread = threading.Thread(target=hold)
    thread.start()
    assert started.wait(5.0)
    try:
        result = _run_pi(monkeypatch, tmp_path, backend)
    finally:
        release.set()
        thread.join(5.0)

    assert result.exit_code == 1
    assert "Pane injection lock unavailable" in result.reply
    assert backend.sent == []
