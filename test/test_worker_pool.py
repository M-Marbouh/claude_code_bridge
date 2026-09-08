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
