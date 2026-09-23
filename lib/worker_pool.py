from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Generic, Optional, Protocol, TypeVar


ResultT = TypeVar("ResultT")


class QueuedTaskLike(Protocol[ResultT]):
    req_id: str
    done_event: threading.Event
    result: Optional[ResultT]


TaskT = TypeVar("TaskT", bound=QueuedTaskLike)


class BaseSessionWorker(threading.Thread, Generic[TaskT, ResultT]):
    def __init__(self, session_key: str):
        super().__init__(daemon=True)
        self.session_key = session_key
        self._q: "queue.Queue[TaskT]" = queue.Queue()
        self._stop_event = threading.Event()
        self._current: Optional[TaskT] = None
        self._current_started_at: Optional[float] = None

    def enqueue(self, task: TaskT) -> None:
        self._q.put(task)

    def stop(self) -> None:
        self._stop_event.set()

    def queue_snapshot(self) -> dict:
        """What this worker is doing now: the in-flight task and how many wait behind it."""
        current = self._current
        started = self._current_started_at
        in_flight = None
        if current is not None:
            progress = getattr(current, "progress", None)
            in_flight = {
                "req_id": getattr(current, "req_id", ""),
                "running_s": round(time.time() - started, 1) if started else None,
                "progress": dict(progress) if isinstance(progress, dict) else {},
            }
        return {"session_key": self.session_key, "waiting": self._q.qsize(), "in_flight": in_flight}

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                task = self._q.get(timeout=0.2)
            except queue.Empty:
                continue

            # Skip cancelled/expired tasks
            if hasattr(task, 'cancelled') and task.cancelled:
                task.done_event.set()
                continue

            self._current = task
            self._current_started_at = time.time()
            try:
                task.result = self._handle_task(task)
            except Exception as exc:
                task.result = self._handle_exception(exc, task)
            finally:
                self._current = None
                self._current_started_at = None
                task.done_event.set()

    def _handle_task(self, task: TaskT) -> ResultT:
        raise NotImplementedError

    def _handle_exception(self, exc: Exception, task: TaskT) -> ResultT:
        raise NotImplementedError


WorkerT = TypeVar("WorkerT", bound=threading.Thread)


class PerSessionWorkerPool(Generic[WorkerT]):
    def __init__(self):
        self._lock = threading.Lock()
        self._workers: dict[str, WorkerT] = {}

    def workers(self) -> list[WorkerT]:
        with self._lock:
            return list(self._workers.values())

    def get_or_create(self, session_key: str, factory: Callable[[str], WorkerT]) -> WorkerT:
        created = False
        with self._lock:
            worker = self._workers.get(session_key)
            # Check if worker thread is dead and needs replacement
            try:
                worker_alive = bool(worker.is_alive()) if worker is not None else False
            except AssertionError:
                # Some tests use a lightweight Thread double that marks itself
                # started without creating CPython's private thread state.
                worker_alive = True
            if worker is not None and not worker_alive:
                # Worker thread died, remove it and create a new one
                self._workers.pop(session_key, None)
                worker = None
            if worker is None:
                worker = factory(session_key)
                self._workers[session_key] = worker
                created = True
        if created:
            worker.start()
        return worker
