from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Generic, Optional, Protocol, TypeVar

from askd.unsent import finalize_unsent


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
        """What this worker is doing now, including the identities waiting behind it."""
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
        with self._q.mutex:
            waiting_req_ids = [getattr(task, "req_id", "") for task in self._q.queue]
        return {
            "session_key": self.session_key,
            "waiting": len(waiting_req_ids),
            "waiting_req_ids": waiting_req_ids,
            "in_flight": in_flight,
        }

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                task = self._q.get(timeout=0.2)
            except queue.Empty:
                continue

            # Claiming is atomic with the daemon's wait expiry: a task that the
            # daemon cancelled while still queued is skipped here and never
            # handed to the adapter; anything else is marked started before the
            # handler runs (see `askd.daemon`'s expiry path).
            claim_lock = getattr(task, "claim_lock", None)
            if claim_lock is not None:
                with claim_lock:
                    # F2: the SAME lock the daemon's queue-expiry takes, so
                    # "skipped" and "started" are decided exactly once. A task
                    # whose queue deadline already passed is never injected,
                    # and a claimed task records WHEN, because its turn budget
                    # is counted from the claim rather than from submission.
                    if getattr(task, "cancelled", False):
                        task.done_event.set()
                        continue
                    queue_deadline = getattr(task, "queue_deadline", None)
                    if queue_deadline is not None and time.monotonic() >= queue_deadline:
                        # Expired while still queued: nothing was ever sent, so
                        # this is a PROVEN pre-dispatch refusal.
                        #
                        # Mark it cancelled UNDER the lock, then RELEASE it and
                        # finalize OUTSIDE. `finalize_unsent` takes this same
                        # non-reentrant lock, so calling it here would deadlock
                        # the worker forever (and block the daemon's expiry
                        # behind it).
                        task.cancelled = True
                        if task.cancel_event:
                            task.cancel_event.set()
                        expired = True
                    else:
                        expired = False
                        task.started = True
                        if getattr(task, "worker_cap_s", None) is not None:
                            task.claimed_at = time.monotonic()
                if expired:
                    # Outside the lock: record + publish exactly once, and give
                    # the caller a real reply instead of a silent None.
                    finalize_unsent(task, getattr(task, "provider_key", "") or "request")
                    continue
            elif hasattr(task, 'cancelled') and task.cancelled:
                # Tasks without a claim lock keep the pre-claim behaviour exactly.
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
