from __future__ import annotations

import importlib.machinery
import importlib.util
import threading
from pathlib import Path

from askd.adapters.base import QueuedTask
from askd.daemon import UnifiedAskDaemon
from worker_pool import BaseSessionWorker


ROOT = Path(__file__).resolve().parents[1]
REQ = "20261018-000000-000-100-1"


def _load_script(name: str):
    path = ROOT / "bin" / name
    loader = importlib.machinery.SourceFileLoader(f"{name.replace('-', '_')}_queue_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _task(req_id: str) -> QueuedTask:
    return QueuedTask(request=None, created_ms=0, req_id=req_id, done_event=threading.Event())


def test_queue_snapshot_exposes_waiting_request_ids() -> None:
    worker = BaseSessionWorker("codex:launch:launch-a:live:session-a")
    worker._q.put(_task("waiting-1"))
    worker._q.put(_task("waiting-2"))

    snapshot = worker.queue_snapshot()

    assert snapshot["waiting"] == 2
    assert snapshot["waiting_req_ids"] == ["waiting-1", "waiting-2"]


def test_daemon_queue_status_can_filter_by_exact_session_key(tmp_path: Path) -> None:
    daemon = UnifiedAskDaemon(state_file=tmp_path / "askd.json")
    pool = daemon.pool._get_pool("codex")
    for key, req_id in (
        ("codex:launch:launch-a:live:session-a", "busy-a"),
        ("codex:launch:launch-a:live:session-b", "busy-b"),
    ):
        worker = BaseSessionWorker(key)
        worker._current = _task(req_id)
        worker._current_started_at = 0
        pool._workers[key] = worker

    response = daemon._handle_request(
        {
            "operation": "queue_status",
            "provider": "codex",
            "session_key": "codex:launch:launch-a:live:session-b",
        }
    )

    [entry] = response["queues"]
    assert entry["session_key"] == "codex:launch:launch-a:live:session-b"
    assert entry["in_flight"]["req_id"] == "busy-b"


def test_ask_notice_queries_and_reports_only_the_exact_routed_queue(monkeypatch, tmp_path: Path) -> None:
    ask = _load_script("ask")
    route = ask.ResolvedRoute(live_id="session-a", launch_id="launch-a")
    calls: list[tuple] = []
    monkeypatch.setattr(
        ask,
        "daemon_queue_status",
        lambda *args, **kwargs: calls.append((args, kwargs)) or [
            {
                "session_key": "codex:launch:launch-a:live:session-a",
                "waiting": 2,
                "in_flight": {"req_id": "busy-task", "running_s": 93.2},
            }
        ],
    )

    notice = ask._queued_notice("codex", route, tmp_path)

    assert notice == "[CCB_QUEUED waiting=2 busy_with=busy-task running=93.2s]"
    (args, kwargs), = calls
    assert args[1:] == ("codex", "codex:launch:launch-a:live:session-a")
    assert kwargs == {"timeout_s": 0.25}


def test_ask_notice_is_silent_when_queue_status_fails(monkeypatch, tmp_path: Path) -> None:
    ask = _load_script("ask")
    route = ask.ResolvedRoute(live_id="session-a", launch_id="launch-a")

    def fail(*_args, **_kwargs):
        raise RuntimeError("daemon unavailable")

    monkeypatch.setattr(ask, "daemon_queue_status", fail)
    assert ask._queued_notice("codex", route, tmp_path) == ""


def test_ask_notice_refuses_a_provider_wide_or_wrong_session_result(monkeypatch, tmp_path: Path) -> None:
    ask = _load_script("ask")
    route = ask.ResolvedRoute(live_id="session-a", launch_id="launch-a")
    monkeypatch.setattr(
        ask,
        "daemon_queue_status",
        lambda *_args, **_kwargs: [
            {
                "session_key": "codex:launch:launch-a:live:session-b",
                "waiting": 0,
                "in_flight": {"req_id": "wrong-session", "running_s": 4},
            }
        ],
    )

    assert ask._queued_notice("codex", route, tmp_path) == ""


def test_pend_distinguishes_running_and_queued_exact_task(monkeypatch, tmp_path: Path) -> None:
    pend = _load_script("pend")
    receipt = {
        "provider": "codex",
        "destination_live_id": "session-a",
        "route_launch_id": "launch-a",
        "work_dir": str(tmp_path),
    }
    calls: list[tuple] = []

    def status(*args, **kwargs):
        calls.append((args, kwargs))
        return [
            {
                "session_key": "codex:launch:launch-a:live:session-a",
                "in_flight": {"req_id": "running-task", "running_s": 12.5},
                "waiting_req_ids": ["ahead-1", REQ, "behind-1"],
            }
        ]

    monkeypatch.setattr(pend, "daemon_queue_status", status)

    assert pend._pending_queue_notice(receipt, "running-task") == (
        "[CCB_QUEUE state=running task=running-task running=12.5s]"
    )
    assert pend._pending_queue_notice(receipt, REQ) == f"[CCB_QUEUE state=queued task={REQ} ahead=2]"
    (args, kwargs), = calls[:1]
    assert args[1:] == ("codex", "codex:launch:launch-a:live:session-a")
    assert kwargs == {"timeout_s": 0.5}


def test_pend_queued_count_excludes_missing_in_flight_task(monkeypatch, tmp_path: Path) -> None:
    pend = _load_script("pend")
    receipt = {
        "provider": "codex",
        "destination_live_id": "session-a",
        "route_launch_id": "launch-a",
        "work_dir": str(tmp_path),
    }
    monkeypatch.setattr(
        pend,
        "daemon_queue_status",
        lambda *_args, **_kwargs: [
            {
                "session_key": "codex:launch:launch-a:live:session-a",
                "in_flight": None,
                "waiting_req_ids": [REQ],
            }
        ],
    )

    assert pend._pending_queue_notice(receipt, REQ) == f"[CCB_QUEUE state=queued task={REQ} ahead=0]"


def test_pending_queue_notice_is_silent_without_exact_route(monkeypatch, tmp_path: Path) -> None:
    pend = _load_script("pend")
    called = False

    def status(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(pend, "daemon_queue_status", status)
    assert pend._pending_queue_notice({"provider": "codex", "work_dir": str(tmp_path)}, REQ) == ""
    assert called is False
