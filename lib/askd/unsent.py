"""One finalizer for a request that expired while still unsent.

A queued request can expire from TWO sides, and both must produce the SAME
outcome:

* the WORKER, when it dequeues a task whose queue deadline already passed -- it
  must not inject, and it must not leave the caller with nothing;
* the DAEMON, when its own wait elapses before the task was ever claimed.

Before this module existed the two sides disagreed. The worker set `done_event`
with no result at all, so the daemon skipped recording and the caller got
exit 2 with an empty reply: no receipt, no completion hook, no notification,
zero sends. The request was simply lost, silently. The daemon's own path did
record "Not sent", but its direct reply was empty too.

`finalize_unsent` is therefore the ONLY place that may record and publish this
outcome, and it does so EXACTLY ONCE: whichever side arrives first wins (decided
under the task's own `claim_lock`) and the other reuses its result. The reply it
produces is also what the caller sees directly, so the wording is never lost.
"""
from __future__ import annotations

from askd.adapters.base import ProviderResult, QueuedTask, route_session_key
from askd.completion_delivery import persist_and_notify
from completion_hook import COMPLETION_STATUS_FAILED


def _log(line: str) -> None:
    # The daemon's own log, resolved lazily so this module never imports
    # `askd.daemon` (which imports the adapters, which would cycle).
    from askd.daemon import ASKD_SPEC
    from askd_runtime import log_path, write_log

    write_log(log_path(ASKD_SPEC.log_file_name), line)


#: The agreed Pi wording, kept here so both the finalizer and the adapter use
#: ONE sentence for a request CCB never injected.
PI_UNSENT_REPLY = "Pi request was not sent: CCB stopped waiting before Pi was free."


def unsent_reply(provider: str, timeout_s: float, reason: str = "") -> str:
    """The one sentence a caller sees for a request that never went out.

    `reason` lets a provider-specific refusal keep its own established wording
    (Pi says "Pi request was not sent: ..."), while an ordinary queue expiry
    keeps the line it has always used.
    """
    if reason:
        # An explicit reason always wins: the caller supplied the words.
        return reason
    if provider == "pi":
        # A2: the agreed Pi sentence, whatever the duration -- it names no
        # number, so it can never disagree with the real cap.
        return PI_UNSENT_REPLY
    # Otherwise the line callers have always seen for an unsent expiry, quoting
    # the ACTUAL budget it waited out (`budget_s`), never the nominal timeout.
    return (
        f"Not sent: this request waited in line behind earlier {provider} requests "
        f"for its whole timeout ({int(timeout_s)} s). Resend it if still needed."
    )


def _build_result(
    task: QueuedTask, provider: str, *, budget_s: float | None, reason: str
) -> ProviderResult:
    """The unsent result for this task. Pure: no locks, no I/O."""
    request = task.request
    # A2: prefer the REAL queue cap the task carried over the nominal timeout,
    # so the sentence can never claim a duration the caller never waited.
    cap_s = getattr(task, "worker_cap_s", None)
    if budget_s is not None:
        timeout_s = float(budget_s)
    elif cap_s is not None:
        timeout_s = float(cap_s)
    else:
        timeout_s = float(request.timeout_s)
    return ProviderResult(
        exit_code=2,
        reply=unsent_reply(provider, timeout_s, reason),
        req_id=task.req_id,
        session_key=(
            route_session_key(provider, request.route)
            if request.route.present
            else f"{provider}:unknown"
        ),
        done_seen=False,
        status=COMPLETION_STATUS_FAILED,
    )


def finalize_unsent(
    task: QueuedTask,
    provider: str,
    *,
    budget_s: float | None = None,
    reason: str = "",
) -> ProviderResult:
    """Record and publish an unsent expiry exactly once; return its result.

    Two rules make this safe from BOTH expiry paths (the worker's queue check and
    the daemon's wait), and they are the whole point of the function:

    * **Never call this while holding ``claim_lock``.** The lock is a plain
      ``threading.Lock``, so re-entering it self-deadlocks. The worker marks the
      task cancelled UNDER the lock, RELEASES it, and only then finalizes;
      finalize itself takes the lock here.
    * **Ownership and publication happen together, under that lock.** The winner
      sets ``task.result`` while claiming, so a second caller can never observe
      ``unsent_finalized=True`` without a result to return -- which used to make
      the loser return ``None`` and the daemon fall back to an empty reply.

    Only the winner persists and notifies, and it does so OUTSIDE the lock so a
    slow receipt never blocks the other side.
    """
    claim_lock = getattr(task, "claim_lock", None)
    result = None

    def _claim() -> bool:
        nonlocal result
        if getattr(task, "unsent_finalized", False):
            # Published by whoever claimed it; a caller that raced us still gets
            # that same, already-visible result (never None).
            result = getattr(task, "result", None)
            return False
        task.unsent_finalized = True
        # Publish and own in the same critical section.
        result = getattr(task, "result", None)
        if result is None:
            result = _build_result(task, provider, budget_s=budget_s, reason=reason)
        task.result = result
        task.cancelled = True
        return True

    if claim_lock is not None:
        with claim_lock:
            won = _claim()
        # Lock RELEASED: persistence below cannot deadlock and cannot block the
        # other expiry path.
    else:  # pragma: no cover - every real task carries a claim lock
        won = _claim()

    done_event = getattr(task, "done_event", None)
    if done_event is not None:
        done_event.set()

    if not won:
        return result

    request = task.request
    _log(
        f"[WARN] queued request expired unsent provider={provider} "
        f"req_id={task.req_id}"
    )
    try:
        persist_and_notify(
            provider,
            request,
            req_id=task.req_id,
            reply=result.reply,
            status=result.status,
            done_seen=False,
            notify=not (request.delivery_only or request.peer_destination.present),
        )
    except Exception as exc:  # a record failure must never lose the reply
        _log(f"[WARN] unsent record/notify failed provider={provider} req_id={task.req_id}: {exc}")
    return result


__all__ = ["PI_UNSENT_REPLY", "finalize_unsent", "unsent_reply"]
