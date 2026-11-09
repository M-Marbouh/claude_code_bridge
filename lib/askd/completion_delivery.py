"""One place that records a proven result and notifies its caller.

Shared by the Pi adapter's ``_finish`` and the daemon's queued-expiry path so a
request that finishes without a waiting client -- a Pi turn delivered after the
caller stopped waiting, or a request that expired before it was ever sent --
produces exactly the same receipt plus completion hook the adapter's own
finish path produces.
"""
from __future__ import annotations

from askd.adapters.base import ProviderRequest
from completion_hook import notify_completion
from task_receipts import PersistOutcome, persist_proven_result


def persist_and_notify(
    provider: str,
    req: ProviderRequest,
    *,
    req_id: str,
    reply: str,
    status: str,
    done_seen: bool,
    transcript_path: str = "",
    conversation_id: str = "",
    notify: bool = True,
) -> PersistOutcome:
    """Persist the result first, then notify the caller.

    The receipt is written before the hook runs, and a FAILED persist (or a
    request that asked to suppress the hook) never notifies. ``notify=False``
    records the result without notifying -- for delivery_only and peer
    requests, whose caller is not waiting on a completion hook.
    """
    try:
        persisted = persist_proven_result(
            req_id, reply=reply, status=status,
            transcript_path=transcript_path, conversation_id=conversation_id,
        )
    except Exception:
        persisted = PersistOutcome.FAILED
    if not notify or req.suppress_completion_hook or persisted == PersistOutcome.FAILED:
        return persisted
    notify_completion(
        provider=provider, output_file=req.output_path, reply=reply, req_id=req_id,
        done_seen=done_seen, status=status, caller=req.caller,
        email_req_id=req.email_req_id, email_msg_id=req.email_msg_id, email_from=req.email_from,
        work_dir=req.caller_work_dir or req.work_dir,
        caller_pane_id=req.caller_pane_id or (req.route.caller_pane_id if req.route.present else ""),
        caller_terminal=req.caller_terminal or (req.route.caller_terminal if req.route.present else ""),
        caller_live_id=req.route.caller_live_id if req.route.present else "",
        route_launch_id=req.route.launch_id if req.route.present else "",
    )
    return persisted
