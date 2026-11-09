from __future__ import annotations

import threading
import time
import pytest
from dataclasses import dataclass
from typing import Optional

from worker_pool import BaseSessionWorker, PerSessionWorkerPool


@pytest.mark.parametrize("replace_saved_pane", [False, True])
def test_queued_peer_send_is_pinned_not_provider_default(monkeypatch, tmp_path, replace_saved_pane):
    import terminal
    from askd.adapters.base import PeerDestination, ProviderRequest
    from askd.daemon import _SessionWorker, _UnifiedWorkerPool
    from askd.registry import ProviderRegistry

    sent = []
    marker_pane = ["%1"]
    class Backend:
        def is_alive(self, pane):
            return True  # A stays alive while the default points to B.
        def pane_matches_cwd_strict(self, pane, cwd):
            return True
        def find_pane_by_title_marker(self, marker, cwd):
            return marker_pane[0]
        def send_text(self, pane, prompt):
            sent.append((pane, prompt))
    monkeypatch.setattr(terminal, "get_backend_for_session", lambda data: Backend())
    adapter = _RouteAwareAdapter()
    monkeypatch.setattr(adapter, "load_session", lambda wd: pytest.fail("provider-default lookup"))
    monkeypatch.setattr(adapter, "handle_task", lambda task: pytest.fail("provider-default send"))
    registry = ProviderRegistry()
    registry.register(adapter)
    pool = _UnifiedWorkerPool(registry)
    queued = []
    class HoldingPool:
        def get_or_create(self, key, factory):
            worker = _SessionWorker(key, adapter)
            worker.enqueue = queued.append
            return worker
    monkeypatch.setattr(pool, "_get_pool", lambda provider: HoldingPool())
    request = ProviderRequest(
        client_id="peer", work_dir=str(tmp_path), timeout_s=1, quiet=True,
        message="sentinel", caller="claude", delivery_only=True,
        peer_destination=PeerDestination(pane_id="%1", terminal="tmux",
            work_dir=str(tmp_path), ccb_project_id="p", pane_title_marker="owner-A"),
    )
    task = pool.submit("codex", request)
    assert queued == [task]
    if replace_saved_pane:
        marker_pane[0] = "%2"
    result = _SessionWorker("peer-key", adapter)._handle_task(task)
    if replace_saved_pane:
        assert result.exit_code != 0
        assert sent == []
    else:
        assert result.exit_code == 0
        assert [pane for pane, prompt in sent] == ["%1"]
        assert "sentinel" in sent[0][1]


def test_wezterm_peer_destination_uses_stable_pane_and_cwd_not_dynamic_title(monkeypatch, tmp_path):
    import terminal
    from askd.adapters.base import PeerDestination
    from peer_routing import revalidate_peer_destination

    class Backend:
        def is_alive(self, pane):
            return pane == "2"
        def pane_matches_cwd_strict(self, pane, cwd):
            return pane == "2" and cwd == str(tmp_path)
        def find_pane_by_title_marker(self, marker, cwd):
            return None  # Claude has replaced CCB's launch title.

    monkeypatch.setattr(terminal, "get_backend_for_session", lambda data: Backend())
    destination = PeerDestination(
        pane_id="2",
        terminal="wezterm",
        work_dir=str(tmp_path),
        ccb_project_id="project",
        pane_title_marker="CCB-Claude-project",
    )

    assert revalidate_peer_destination(destination).ok


class _NoopThread(threading.Thread):
    def __init__(self, session_key: str, started: list[str]):
        super().__init__(daemon=True)
        self.session_key = session_key
        self._track = started  # avoid overriding threading.Thread._started

    def start(self) -> None:  # type: ignore[override]
        self._track.append(self.session_key)
        # Mark as "alive" by setting _started event (required for is_alive() check)
        self._started.set()


def test_per_session_worker_pool_reuses_same_key() -> None:
    started: list[str] = []
    pool: PerSessionWorkerPool[_NoopThread] = PerSessionWorkerPool()
    w1 = pool.get_or_create("k1", lambda k: _NoopThread(k, started))
    w2 = pool.get_or_create("k1", lambda k: _NoopThread(k, started))
    w3 = pool.get_or_create("k2", lambda k: _NoopThread(k, started))

    assert w1 is w2
    assert w1 is not w3
    assert started.count("k1") == 1
    assert started.count("k2") == 1


@dataclass
class _Task:
    req_id: str
    done_event: threading.Event
    result: Optional[str] = None


class _EchoWorker(BaseSessionWorker[_Task, str]):
    def _handle_task(self, task: _Task) -> str:
        return f"ok:{task.req_id}"

    def _handle_exception(self, exc: Exception, task: _Task) -> str:
        return f"err:{task.req_id}:{exc}"


class _FailWorker(_EchoWorker):
    def _handle_task(self, task: _Task) -> str:
        raise RuntimeError("boom")


def test_base_session_worker_processes_task_and_sets_event() -> None:
    worker = _EchoWorker("s1")
    worker.start()
    try:
        task = _Task(req_id="r1", done_event=threading.Event())
        worker.enqueue(task)
        assert task.done_event.wait(timeout=2.0) is True
        assert task.result == "ok:r1"
    finally:
        worker.stop()
        worker.join(timeout=2.0)


def test_base_session_worker_exception_path() -> None:
    worker = _FailWorker("s1")
    worker.start()
    try:
        task = _Task(req_id="r2", done_event=threading.Event())
        worker.enqueue(task)
        assert task.done_event.wait(timeout=2.0) is True
        assert task.result is not None
        assert task.result.startswith("err:r2:")
    finally:
        worker.stop()
        worker.join(timeout=2.0)


# --- askd.daemon._UnifiedWorkerPool: routing by resolved destination -------
# Task 3 (validate, never re-select) and Task 4 (queue/load by session, not
# provider) both live in `askd.daemon._UnifiedWorkerPool.submit`.

import askd.daemon as daemon_mod
from askd.adapters.base import (
    BaseProviderAdapter,
    ProviderRequest,
    ProviderResult,
    QueuedTask,
    ResolvedRoute,
    route_session_key,
)
from askd.daemon import UnifiedAskDaemon, _UnifiedWorkerPool
from askd.registry import ProviderRegistry
from live_sessions import LiveSession, Resolution


class _RouteAwareAdapter(BaseProviderAdapter):
    """Minimal adapter whose `handle_task` can be told to block on a shared
    gate for specific req_ids, so tests can observe start order and prove
    concurrent vs. serialized progress.
    """

    def __init__(self, key: str = "codex"):
        self._key = key
        self.gate = threading.Event()
        self.hold_reqs: set[str] = set()
        self.started: list[str] = []
        self.finished: list[str] = []
        self.seen_routes: list[ResolvedRoute] = []

    @property
    def key(self) -> str:
        return self._key

    @property
    def spec(self):
        return None

    @property
    def session_filename(self) -> str:
        return ".x-session"

    def load_session(self, work_dir):
        return "unrouted-session"

    def compute_session_key(self, session) -> str:
        return f"{self._key}:{session}"

    def handle_task(self, task: QueuedTask) -> ProviderResult:
        self.started.append(task.req_id)
        self.seen_routes.append(task.request.route)
        if task.req_id in self.hold_reqs:
            # No internal timeout: a held task blocks until the test
            # explicitly releases `gate`. A bounded wait here would let a
            # session-key mutation that wrongly serializes two independent
            # routes pass by accident, once the internal timeout elapses
            # before the test's own wait does — this must block for real.
            self.gate.wait()
        self.finished.append(task.req_id)
        return ProviderResult(
            exit_code=0,
            reply="ok",
            req_id=task.req_id,
            session_key="unused",
            done_seen=True,
        )


def _wait_until(predicate, *, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _route_request(req_id: str, route: ResolvedRoute) -> ProviderRequest:
    return ProviderRequest(
        client_id=req_id,
        work_dir="/tmp/proj",
        timeout_s=5.0,
        quiet=True,
        message="hi",
        caller="claude",
        req_id=req_id,
        route=route,
    )


def test_submit_with_no_route_uses_adapters_own_session_key(monkeypatch) -> None:
    # Byte-identical behaviour for a request with no route: the pool must
    # key off `adapter.compute_session_key`, exactly as it always has, and
    # must never touch route validation.
    monkeypatch.setattr(
        daemon_mod,
        "validate_route",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("validate_route called for a routeless request")),
    )
    adapter = _RouteAwareAdapter()
    pool = _UnifiedWorkerPool(ProviderRegistry())
    pool._registry.register(adapter)

    req = ProviderRequest(
        client_id="a", work_dir="/tmp/proj", timeout_s=5.0, quiet=True,
        message="hi", caller="claude", req_id="req-a",
    )
    task = pool.submit("codex", req)

    assert task is not None
    assert task.done_event.wait(timeout=2.0) is True
    assert task.result.exit_code == 0
    assert adapter.started == ["req-a"]


def test_submit_two_different_routes_of_one_provider_progress_independently(monkeypatch) -> None:
    fake_session = LiveSession(live_id="s1", provider="codex", launch_id="ai-1")
    monkeypatch.setattr(daemon_mod, "validate_route", lambda **_kw: Resolution(session=fake_session))

    adapter = _RouteAwareAdapter()
    adapter.hold_reqs.add("req-a")
    pool = _UnifiedWorkerPool(ProviderRegistry())
    pool._registry.register(adapter)

    route_a = ResolvedRoute(live_id="s1", launch_id="ai-1")
    route_b = ResolvedRoute(live_id="s2", launch_id="ai-1")

    try:
        task_a = pool.submit("codex", _route_request("req-a", route_a))
        assert task_a is not None
        assert _wait_until(lambda: "req-a" in adapter.started)

        task_b = pool.submit("codex", _route_request("req-b", route_b))
        assert task_b is not None
        # A DIFFERENT live session must not be blocked behind req-a's gate,
        # which never releases on its own -- this can only pass if req-b's
        # worker never waited on it in the first place.
        assert task_b.done_event.wait(timeout=2.0) is True
        assert task_b.result.exit_code == 0
    finally:
        adapter.gate.set()
    assert task_a.done_event.wait(timeout=2.0) is True
    assert task_a.result.exit_code == 0


def test_submit_two_requests_to_the_same_route_serialize(monkeypatch) -> None:
    fake_session = LiveSession(live_id="s1", provider="codex", launch_id="ai-1")
    monkeypatch.setattr(daemon_mod, "validate_route", lambda **_kw: Resolution(session=fake_session))

    adapter = _RouteAwareAdapter()
    adapter.hold_reqs.add("req-a")
    pool = _UnifiedWorkerPool(ProviderRegistry())
    pool._registry.register(adapter)

    route = ResolvedRoute(live_id="s1", launch_id="ai-1")

    try:
        task_a = pool.submit("codex", _route_request("req-a", route))
        assert task_a is not None
        assert _wait_until(lambda: "req-a" in adapter.started)

        task_c = pool.submit("codex", _route_request("req-c", route))
        assert task_c is not None
        time.sleep(0.2)
        # Still queued behind req-a: the SAME live session serializes.
        assert "req-c" not in adapter.started
    finally:
        adapter.gate.set()
    assert task_a.done_event.wait(timeout=2.0) is True
    assert task_c.done_event.wait(timeout=2.0) is True
    assert adapter.started == ["req-a", "req-c"]


def test_submit_rejects_request_whose_route_is_no_longer_valid(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon_mod,
        "validate_route",
        lambda **_kw: Resolution(error="unavailable", detail="routed destination is gone"),
    )
    adapter = _RouteAwareAdapter()
    pool = _UnifiedWorkerPool(ProviderRegistry())
    pool._registry.register(adapter)

    route = ResolvedRoute(live_id="s1", launch_id="ai-1")
    task = pool.submit("codex", _route_request("req-a", route))

    assert task is not None
    assert task.done_event.is_set()
    assert task.result.exit_code == 1
    assert "unavailable" in task.result.reply
    # Never reached the worker/adapter at all -- refused, not redirected.
    assert adapter.started == []


def test_submit_route_session_key_is_keyed_by_launch_and_live_id(monkeypatch) -> None:
    # Finding 4: live-id uniqueness is only enforced within one launch's
    # inventory, never globally, so the queue key must include launch_id
    # too -- keying by live_id alone could conflate two different launches
    # that happen to reuse the same live_id string.
    fake_session = LiveSession(live_id="s1", provider="codex", launch_id="ai-1")
    monkeypatch.setattr(daemon_mod, "validate_route", lambda **_kw: Resolution(session=fake_session))
    adapter = _RouteAwareAdapter()

    route = ResolvedRoute(live_id="s1", launch_id="ai-1")
    assert route_session_key("codex", route) == "codex:launch:ai-1:live:s1"

    pool = _UnifiedWorkerPool(ProviderRegistry())
    pool._registry.register(adapter)
    task = pool.submit("codex", _route_request("req-a", route))
    assert task is not None
    assert task.done_event.wait(timeout=2.0) is True
    # The pool's own worker-selection key, independent of the adapter.
    assert "codex" in pool._pools
    assert "codex:launch:ai-1:live:s1" in pool._pools["codex"]._workers


def test_handle_request_parses_route_from_message_into_provider_request(monkeypatch) -> None:
    fake_session = LiveSession(live_id="s1", provider="codex", launch_id="ai-1")
    monkeypatch.setattr(daemon_mod, "validate_route", lambda **_kw: Resolution(session=fake_session))

    adapter = _RouteAwareAdapter()
    registry = ProviderRegistry()
    registry.register(adapter)
    daemon = UnifiedAskDaemon(registry=registry, work_dir="/tmp/proj")

    msg = {
        "id": "r1",
        "provider": "codex",
        "work_dir": "/tmp/proj",
        "timeout_s": 5.0,
        "message": "hi",
        "caller": "claude",
        "route": {
            "live_id": "s1",
            "launch_id": "ai-1",
            "caller_live_id": "s0",
            "pane_id": "%2",
            "terminal": "tmux",
            "session_file": "/tmp/s1.json",
            "ccb_project_id": "proj-1",
        },
    }

    response = daemon._handle_request(msg)

    assert response["exit_code"] == 0
    assert len(adapter.seen_routes) == 1
    seen = adapter.seen_routes[0]
    assert seen.live_id == "s1"
    assert seen.launch_id == "ai-1"
    assert seen.caller_live_id == "s0"
    assert seen.present is True


def test_handle_request_async_submit_returns_before_task_completion() -> None:
    adapter = _RouteAwareAdapter()
    registry = ProviderRegistry()
    registry.register(adapter)
    daemon = UnifiedAskDaemon(registry=registry, work_dir="/tmp/proj")
    pending = QueuedTask(
        request=ProviderRequest(
            client_id="r1", work_dir="/tmp/proj", timeout_s=5.0,
            quiet=False, message="hi", caller="codex",
        ),
        created_ms=0,
        req_id="task-1",
        done_event=threading.Event(),
    )
    daemon.pool.submit = lambda _provider, _request: pending

    response = daemon._handle_request({
        "id": "r1", "provider": "codex", "work_dir": "/tmp/proj",
        "timeout_s": 5.0, "message": "hi", "caller": "codex",
        "async_submit": True, "req_id": "task-1",
    })

    assert response["exit_code"] == 0
    assert response["accepted"] is True
    assert response["req_id"] == "task-1"
    assert pending.done_event.is_set() is False


def test_handle_request_rejects_malformed_route_without_reaching_adapter(monkeypatch) -> None:
    # Finding 5: a non-dict or partial route arriving over RPC must be
    # rejected outright, never silently ignored and downgraded to a
    # provider-default (legacy) request.
    monkeypatch.setattr(
        daemon_mod,
        "validate_route",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("validate_route reached for a malformed route")),
    )
    adapter = _RouteAwareAdapter()
    registry = ProviderRegistry()
    registry.register(adapter)
    daemon = UnifiedAskDaemon(registry=registry, work_dir="/tmp/proj")

    msg = {
        "id": "r1",
        "provider": "codex",
        "work_dir": "/tmp/proj",
        "timeout_s": 5.0,
        "message": "hi",
        "caller": "claude",
        "route": {"live_id": "s1"},  # launch_id missing: malformed, not absent
    }

    response = daemon._handle_request(msg)

    assert response["exit_code"] == 1
    assert "Malformed route" in response["reply"]
    assert adapter.started == []
    assert adapter.seen_routes == []


def test_handle_request_rejects_non_dict_route_without_downgrading_to_legacy(monkeypatch) -> None:
    adapter = _RouteAwareAdapter()
    registry = ProviderRegistry()
    registry.register(adapter)
    daemon = UnifiedAskDaemon(registry=registry, work_dir="/tmp/proj")

    msg = {
        "id": "r1",
        "provider": "codex",
        "work_dir": "/tmp/proj",
        "timeout_s": 5.0,
        "message": "hi",
        "caller": "claude",
        "route": "not-a-mapping",
    }

    response = daemon._handle_request(msg)

    assert response["exit_code"] == 1
    assert "Malformed route" in response["reply"]
    assert adapter.started == []


import pytest as _pytest


@_pytest.mark.parametrize("missing_field", ["pane_id", "terminal", "session_file", "ccb_project_id"])
def test_handle_request_rejects_route_missing_one_mandatory_evidence_field(missing_field) -> None:
    # Hole 1: the endpoint-evidence fields the replacement check depends on
    # are MANDATORY once a route is present at all -- a route missing just
    # ONE of them must still refuse, not silently succeed with a weaker
    # (unenforceable) contract, and must never reach the adapter/session.
    adapter = _RouteAwareAdapter()
    registry = ProviderRegistry()
    registry.register(adapter)
    daemon = UnifiedAskDaemon(registry=registry, work_dir="/tmp/proj")

    full_route = {
        "live_id": "s1",
        "launch_id": "ai-1",
        "pane_id": "%2",
        "terminal": "tmux",
        "session_file": "/tmp/s1.json",
        "ccb_project_id": "proj-1",
    }
    del full_route[missing_field]

    msg = {
        "id": "r1",
        "provider": "codex",
        "work_dir": "/tmp/proj",
        "timeout_s": 5.0,
        "message": "hi",
        "caller": "claude",
        "route": full_route,
    }

    response = daemon._handle_request(msg)

    assert response["exit_code"] == 1
    assert "Malformed route" in response["reply"]
    # NO TERMINAL SEND OCCURRED: the request never reached the adapter that
    # would have sent it, exactly as the unsupported-adapter guard proves.
    assert adapter.started == []
    assert adapter.seen_routes == []


def test_submit_refuses_duplicate_pool_route_missing_caller_evidence_via_real_validate_route(
    monkeypatch, tmp_path,
) -> None:
    # Item 1, exercised through the DAEMON ENQUEUE checkpoint using the
    # REAL (unmocked) validate_route against a real registry record -- the
    # validator being right in isolation is not the same as the daemon's
    # own call site using it correctly. A duplicate-provider pool with a
    # route whose saved caller_live_id names the destination itself, and
    # no fresh caller evidence on the request at all, must refuse before
    # ever reaching the adapter.
    import json as _json

    from project_id import compute_ccb_project_id

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    registry_dir = home / ".ccb" / "run"
    registry_dir.mkdir(parents=True)
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    project_id = compute_ccb_project_id(work_dir)
    (registry_dir / "ccb-session-ai-1.json").write_text(
        _json.dumps(
            {
                "ccb_session_id": "ai-1",
                "ccb_project_id": project_id,
                "work_dir": str(work_dir),
                "terminal": "tmux",
                "updated_at": 9999999999,
                "providers": {"codex": {"pane_id": "%2"}},
                "live_sessions": [
                    {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
                    {"live_id": "s2", "provider": "codex", "pane_id": "%3", "active": True},
                ],
            }
        ),
        encoding="utf-8",
    )

    adapter = _RouteAwareAdapter()
    pool = _UnifiedWorkerPool(ProviderRegistry())
    pool._registry.register(adapter)

    route = ResolvedRoute(
        live_id="s2",
        launch_id="ai-1",
        caller_live_id="s2",  # saved caller == the destination itself
        pane_id="%3",
        terminal="tmux",
        session_file="",
        ccb_project_id=project_id,
    )
    req = ProviderRequest(
        client_id="a", work_dir=str(work_dir), timeout_s=5.0, quiet=True,
        message="hi", caller="claude", req_id="req-a", route=route,
        # No fresh caller_pane_id/caller_terminal on the request at all.
    )

    task = pool.submit("codex", req)

    assert task is not None
    assert task.done_event.is_set()
    assert task.result.exit_code == 1
    assert "no longer available" in task.result.reply
    assert "unknown_caller" in task.result.reply
    # NO TERMINAL SEND OCCURRED: never reached the adapter.
    assert adapter.started == []


def test_session_worker_exposes_only_actionable_waiting_tasks():
    from askd.daemon import _SessionWorker

    class _QueuedAdapter(_RouteAwareAdapter):
        def __init__(self):
            super().__init__()
            self.first_started = threading.Event()
            self.release_first = threading.Event()
            self.waiting_checks: list[bool] = []

        def handle_task(self, task):
            self.started.append(task.req_id)
            if task.req_id == "first":
                self.first_started.set()
                while not self.release_first.is_set():
                    self.waiting_checks.append(bool(task.has_actionable_waiting()))
                    time.sleep(0.01)
            self.finished.append(task.req_id)
            return ProviderResult(exit_code=0, reply="ok", req_id=task.req_id,
                                  session_key="codex:test", done_seen=True)

    adapter = _QueuedAdapter()
    worker = _SessionWorker("codex:test", adapter)
    def make_task(req_id):
        request = ProviderRequest(client_id=req_id, work_dir="/tmp/test-project", timeout_s=5,
                                  quiet=True, message="message", caller="claude", req_id=req_id)
        return QueuedTask(request=request, created_ms=0, req_id=req_id,
                          done_event=threading.Event(), cancel_event=threading.Event())

    first, cancelled, next_task = make_task("first"), make_task("cancelled"), make_task("next")
    cancelled.cancelled = True
    cancelled.cancel_event.set()
    worker.start()
    try:
        worker.enqueue(first)
        assert adapter.first_started.wait(1)
        worker.enqueue(cancelled)
        time.sleep(0.05)
        assert adapter.waiting_checks and not any(adapter.waiting_checks)
        worker.enqueue(next_task)
        deadline = time.time() + 1
        while not any(adapter.waiting_checks) and time.time() < deadline:
            time.sleep(0.01)
        assert any(adapter.waiting_checks)
        adapter.release_first.set()
        assert first.done_event.wait(1)
        assert next_task.done_event.wait(1)
        assert adapter.started == ["first", "next"]
    finally:
        adapter.release_first.set()
        worker.stop()
        worker.join(timeout=2)


# --- queue-time expiry: a queued request is never lost silently -----------
# Real record, content-automation 2026-11-09 (request C): a request whose caller
# stopped waiting while it was still queued used to be skipped with no result and
# no hook. The worker and the daemon now claim a task under one lock, so exactly
# one side wins: the worker starts it, or the daemon records "Not sent".

from completion_hook import COMPLETION_STATUS_FAILED
from task_receipts import PersistOutcome
import askd.completion_delivery as delivery_mod


class _ExpiredWait:
    """A done_event whose wait expires immediately: the queued-expiry path, without sleeping."""

    def __init__(self):
        self.timeout = None
        self.set_called = False

    def wait(self, timeout=None):
        self.timeout = timeout
        return False

    def is_set(self):
        return False

    def set(self):
        self.set_called = True


def _expiry_task(
    req_id: str,
    *,
    started: bool = False,
    delivery_only: bool = False,
    peer: bool = False,
    suppress_hook: bool = False,
    done_event=None,
) -> QueuedTask:
    from askd.adapters.base import PeerDestination

    request = ProviderRequest(
        client_id="r1", work_dir="/tmp/proj", timeout_s=3600.0, quiet=False,
        message="hi", caller="claude", req_id=req_id,
        delivery_only=delivery_only, suppress_completion_hook=suppress_hook,
        output_path="/tmp/out.txt", caller_pane_id="%0", caller_terminal="tmux",
        caller_work_dir="/tmp/caller",
        peer_destination=(
            PeerDestination(pane_id="%9", terminal="tmux", work_dir="/tmp/peer") if peer else PeerDestination()
        ),
    )
    task = QueuedTask(
        request=request, created_ms=0, req_id=req_id,
        done_event=done_event if done_event is not None else _ExpiredWait(),
        cancel_event=threading.Event(),
    )
    task.started = started
    return task


def _expiry_daemon(monkeypatch, task: QueuedTask) -> UnifiedAskDaemon:
    adapter = _RouteAwareAdapter()
    registry = ProviderRegistry()
    registry.register(adapter)
    daemon = UnifiedAskDaemon(registry=registry, work_dir="/tmp/proj")
    daemon.pool.submit = lambda _provider, _request: task
    return daemon, adapter


def _expiry_message() -> dict:
    return {
        "id": "r1", "provider": "codex", "work_dir": "/tmp/proj",
        "timeout_s": 3600.0, "message": "hi", "caller": "claude", "req_id": "r1",
    }


def _record_delivery_calls(monkeypatch, outcome: str = PersistOutcome.SAVED) -> list:
    calls: list = []
    monkeypatch.setattr(
        delivery_mod, "persist_proven_result",
        lambda req_id, **kwargs: calls.append(("persist", req_id, kwargs)) or outcome,
    )
    monkeypatch.setattr(
        delivery_mod, "notify_completion",
        lambda **kwargs: calls.append(("notify", kwargs)),
    )
    return calls


def test_queued_expiry_records_and_notifies_an_unsent_request(monkeypatch) -> None:
    task = _expiry_task("20261109-105754-593-2384617")
    daemon, adapter = _expiry_daemon(monkeypatch, task)
    calls = _record_delivery_calls(monkeypatch)

    response = daemon._handle_request(_expiry_message())

    # The client contract is unchanged: exit 2 with an empty reply.
    assert response["exit_code"] == 2 and response["reply"] == ""
    # The expiry won the claim, so the adapter never touched the request.
    assert adapter.started == []
    assert task.cancelled is True and task.cancel_event.is_set()
    # Receipt first, notification second.
    assert [call[0] for call in calls] == ["persist", "notify"]
    _kind, req_id, persisted = calls[0]
    assert req_id == task.req_id
    assert persisted["status"] == COMPLETION_STATUS_FAILED
    assert persisted["reply"] == (
        "Not sent: this request waited in line behind earlier codex requests "
        "for its whole timeout (3600 s). Resend it if still needed."
    )
    notified = calls[1][1]
    assert notified["provider"] == "codex"
    assert notified["req_id"] == task.req_id
    assert notified["status"] == COMPLETION_STATUS_FAILED
    assert notified["done_seen"] is False
    # The same caller fields Pi's `_finish` passes.
    assert notified["caller"] == "claude"
    assert notified["output_file"] == "/tmp/out.txt"
    assert notified["work_dir"] == "/tmp/caller"
    assert notified["caller_pane_id"] == "%0"
    assert notified["caller_terminal"] == "tmux"
    assert task.done_event.set_called is True


def test_queued_expiry_does_not_notify_when_the_receipt_could_not_be_saved(monkeypatch) -> None:
    task = _expiry_task("failed-persist")
    daemon, _adapter = _expiry_daemon(monkeypatch, task)
    calls = _record_delivery_calls(monkeypatch, PersistOutcome.FAILED)

    daemon._handle_request(_expiry_message())

    assert [call[0] for call in calls] == ["persist"]


def test_queued_expiry_honours_suppress_completion_hook(monkeypatch) -> None:
    task = _expiry_task("suppressed-hook", suppress_hook=True)
    daemon, _adapter = _expiry_daemon(monkeypatch, task)
    calls = _record_delivery_calls(monkeypatch)

    daemon._handle_request(_expiry_message())

    assert [call[0] for call in calls] == ["persist"]


@pytest.mark.parametrize("kind", ["delivery_only", "peer"])
def test_queued_expiry_persists_only_for_delivery_only_and_peer(monkeypatch, kind: str) -> None:
    task = _expiry_task(
        f"{kind}-record", delivery_only=kind == "delivery_only", peer=kind == "peer",
    )
    daemon, _adapter = _expiry_daemon(monkeypatch, task)
    calls = _record_delivery_calls(monkeypatch)

    daemon._handle_request(_expiry_message())

    assert [call[0] for call in calls] == ["persist"]
    assert calls[0][2]["status"] == COMPLETION_STATUS_FAILED


def test_worker_wins_the_claim_and_gets_the_cancel_event_as_before(monkeypatch) -> None:
    task = _expiry_task("worker-wins", started=True)
    daemon, _adapter = _expiry_daemon(monkeypatch, task)
    calls = _record_delivery_calls(monkeypatch)

    response = daemon._handle_request(_expiry_message())

    assert response["exit_code"] == 2 and response["reply"] == ""
    assert task.cancelled is True and task.cancel_event.is_set()
    # A started task is never recorded as "not sent".
    assert calls == []


def test_queued_expiry_waits_for_the_worker_claim_lock(monkeypatch) -> None:
    task = _expiry_task("claim-ordering")
    daemon, adapter = _expiry_daemon(monkeypatch, task)
    calls = _record_delivery_calls(monkeypatch)
    responses: list[dict] = []

    def run_request() -> None:
        responses.append(daemon._handle_request(_expiry_message()))

    with task.claim_lock:  # the worker holds the claim while it dequeues
        requester = threading.Thread(target=run_request, daemon=True)
        requester.start()
        time.sleep(0.05)
        # The expiry cannot decide while the claim is held.
        assert task.cancelled is False and calls == []
        task.started = True  # the worker wins: it starts the task under the same lock
    requester.join(timeout=2)

    assert not requester.is_alive()
    assert responses and responses[0]["exit_code"] == 2
    assert task.cancelled is True and task.cancel_event.is_set()
    assert calls == []
    assert adapter.started == []


def test_worker_marks_a_dequeued_task_started() -> None:
    worker = _EchoWorker("claim-start")
    worker.start()
    try:
        task = _expiry_task("claimed-task", done_event=threading.Event())
        worker.enqueue(task)
        assert task.done_event.wait(timeout=2) is True
        assert task.started is True
        assert task.result == "ok:claimed-task"
    finally:
        worker.stop()
        worker.join(timeout=2)


def test_worker_skips_a_task_cancelled_while_queued_without_starting_it() -> None:
    worker = _EchoWorker("claim-skip")
    worker.start()
    try:
        task = _expiry_task("skipped-task", done_event=threading.Event())
        task.cancelled = True
        task.cancel_event.set()
        worker.enqueue(task)
        assert task.done_event.wait(timeout=2) is True
        assert task.started is False
        assert task.result is None
    finally:
        worker.stop()
        worker.join(timeout=2)
