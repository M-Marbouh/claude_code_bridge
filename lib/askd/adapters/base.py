"""
Base provider adapter interface for the unified ask daemon.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, TypeVar

from providers import ProviderDaemonSpec


@dataclass(frozen=True)
class ResolvedRoute:
    """A destination resolved once, host-side, before an ask is ever
    acknowledged.

    `live_id` names the destination live session exactly
    (`live_sessions.LiveSession.live_id`); `launch_id` is the launch-registry
    record scope that session was resolved under (`LiveSession.launch_id`,
    i.e. the record's `ccb_session_id` -- `pane_registry.
    resolve_live_session_by_id` re-reads that exact record to validate
    against, never a provider-wide search). `caller_live_id` is the identity
    of the session that issued the request (empty when the caller could not
    be, or did not need to be, identified).

    `pane_id`, `terminal`, `session_file` and `ccb_project_id` are ENDPOINT
    EVIDENCE captured at resolution time -- what the destination looked like
    when this route was made. They are deliberately NOT a cache: nothing
    ever reads them as a substitute for a fresh registry read. Their only
    use is at a validation checkpoint, AFTER re-reading the launch record
    fresh, to detect that the live_id has been silently repointed at a
    different pane, terminal backend, session file, or project -- turning
    that replacement into a refusal instead of a silent follow.

    `live_id` and `launch_id` default empty, meaning "no route": every
    existing `ProviderRequest` construction site is unaffected, and a
    record with no `live_sessions` inventory never produces one.
    """

    live_id: str = ""
    launch_id: str = ""
    caller_live_id: str = ""
    pane_id: str = ""
    terminal: str = ""
    session_file: str = ""
    ccb_project_id: str = ""

    @property
    def present(self) -> bool:
        return bool(self.live_id) and bool(self.launch_id)


def route_session_key(provider: str, route: ResolvedRoute) -> str:
    """The queue/session-routing key for a resolved route.

    Keyed by BOTH `launch_id` and `live_id`: live-id uniqueness is only
    enforced within one launch record's inventory (`live_sessions.
    read_inventory`), never globally, so two different launches could
    legally reuse the same `live_id` string. Keying by `live_id` alone
    would then wrongly serialize (or wrongly conflate) two entirely
    unrelated sessions. Defined once here so the daemon's enqueue-time
    worker selection and an adapter's own send-time bookkeeping can never
    derive two different strings for the same route.
    """
    return f"{provider}:launch:{route.launch_id}:live:{route.live_id}"


class MalformedRouteError(ValueError):
    """Raised when route data is SUPPLIED but does not form a coherent,
    complete route.

    This is deliberately a different outcome from "no route was supplied
    at all" (which parses to the empty `ResolvedRoute`, `.present is
    False`, and takes today's un-routed path): malformed data must never
    be quietly downgraded into "no route" and allowed to fall through to
    provider-default lookup. A caller that catches this must refuse the
    request outright.
    """


# The identity anchor: without both, there is no route to speak of.
_ROUTE_ANCHOR_FIELDS = ("live_id", "launch_id")
# The endpoint evidence Task/Finding 4's replacement detection depends on.
# Once a route is present at all, these are NOT optional: a route carrying
# only the anchor pair would parse "successfully" while defeating every
# comparison `pane_registry.validate_route` runs against it, because there
# would be nothing to compare -- an empty expectation is (deliberately,
# for the CAPTURED-at-resolution-time case) never treated as a
# contradiction. Ingested route data does not get that latitude.
_ROUTE_MANDATORY_EVIDENCE_FIELDS = ("pane_id", "terminal", "session_file", "ccb_project_id")
# Legitimately optional: a route can be resolved with no caller identified
# at all (a single unambiguous destination needs no caller to pick it).
_ROUTE_OPTIONAL_FIELDS = ("caller_live_id",)
_ROUTE_MAPPING_FIELDS = _ROUTE_ANCHOR_FIELDS + _ROUTE_MANDATORY_EVIDENCE_FIELDS + _ROUTE_OPTIONAL_FIELDS


def parse_route_mapping(raw: Any) -> ResolvedRoute:
    """Parse a route mapping -- an RPC JSON object, an env-var-derived
    dict, or a freshly resolved route about to be carried -- into a
    `ResolvedRoute`, distinguishing three states:

      - `raw` is `None`, or a dict with none of the route fields set at
        all: ABSENT. No route was supplied. Returns the empty
        `ResolvedRoute` -- the legacy, un-routed case.
      - `raw` is not a dict, or is a dict naming the anchor pair (`live_id`
        and `launch_id`) but missing one of the MANDATORY endpoint
        evidence fields (`pane_id`, `terminal`, `session_file`,
        `ccb_project_id`), or with a field of the wrong type: INCOMPLETE /
        malformed. Raises `MalformedRouteError`. This is a hard failure,
        never downgraded to "no route" -- absent and incomplete are kept
        visibly distinct at every ingestion boundary that calls this.
      - a coherent, complete dict: returns the populated `ResolvedRoute`.
    """
    if raw is None:
        return ResolvedRoute()
    if not isinstance(raw, dict):
        raise MalformedRouteError("route must be an object")

    values: dict[str, str] = {}
    for key in _ROUTE_MAPPING_FIELDS:
        value = raw.get(key)
        if value is None:
            values[key] = ""
            continue
        if not isinstance(value, str):
            raise MalformedRouteError(f"route.{key} must be a string")
        values[key] = value.strip()

    if not any(values.values()):
        return ResolvedRoute()

    missing = [
        field_name
        for field_name in (_ROUTE_ANCHOR_FIELDS + _ROUTE_MANDATORY_EVIDENCE_FIELDS)
        if not values[field_name]
    ]
    if missing:
        raise MalformedRouteError(
            "route is present but missing required field(s): " + ", ".join(missing)
        )

    return ResolvedRoute(**values)


@dataclass
class ProviderRequest:
    """Unified request structure for all providers."""
    client_id: str
    work_dir: str
    timeout_s: float
    quiet: bool
    message: str
    caller: str
    output_path: Optional[str] = None
    req_id: Optional[str] = None
    no_wrap: bool = False
    show_tier: bool = False
    delivery_only: bool = False
    suppress_completion_hook: bool = False
    # Email-related fields for email caller
    email_req_id: str = ""
    email_msg_id: str = ""
    email_from: str = ""
    # Caller pane ID for direct routing back to the originating terminal pane
    caller_pane_id: str = ""
    caller_terminal: str = ""
    caller_work_dir: str = ""
    # Route snapshot: the exact destination this request was resolved to in
    # client-side preflight, if any. Defaults to the empty `ResolvedRoute`
    # (`route.present` is False), so every existing construction site is
    # unaffected. When present, this is authoritative: nothing downstream
    # may re-run provider lookup to pick a destination, only re-validate
    # this exact one (see `pane_registry.validate_route`).
    route: ResolvedRoute = field(default_factory=ResolvedRoute)


@dataclass
class ProviderResult:
    """Unified result structure for all providers."""
    exit_code: int
    reply: str
    req_id: str
    session_key: str
    done_seen: bool
    done_ms: Optional[int] = None
    anchor_seen: bool = False
    anchor_ms: Optional[int] = None
    fallback_scan: bool = False
    log_path: Optional[str] = None
    extra: Optional[dict] = None
    status: str = ""


class QueuedTaskLike(Protocol):
    """Protocol for queued tasks."""
    req_id: str
    done_event: threading.Event
    result: Optional[ProviderResult]


@dataclass
class QueuedTask:
    """A task queued for processing by a provider adapter."""
    request: ProviderRequest
    created_ms: int
    req_id: str
    done_event: threading.Event
    result: Optional[ProviderResult] = None
    cancelled: bool = False  # Cancellation flag for timeout/expiry
    cancel_event: Optional[threading.Event] = None  # Event for cooperative cancellation


class BaseProviderAdapter(ABC):
    """
    Abstract base class for provider adapters.

    Each provider (codex, gemini, opencode, claude) implements
    this interface to integrate with the unified daemon.
    """

    @property
    @abstractmethod
    def key(self) -> str:
        """Provider key (codex, gemini, opencode, or claude)."""
        ...

    @property
    @abstractmethod
    def spec(self) -> ProviderDaemonSpec:
        """Provider daemon specification."""
        ...

    @property
    @abstractmethod
    def session_filename(self) -> str:
        """Session file name (e.g., '.codex-session')."""
        ...

    @abstractmethod
    def load_session(self, work_dir: Path) -> Optional[Any]:
        """Load session for the given work directory."""
        ...

    @abstractmethod
    def compute_session_key(self, session: Any) -> str:
        """Compute a unique session key for routing."""
        ...

    @abstractmethod
    def handle_task(self, task: QueuedTask) -> ProviderResult:
        """
        Handle a queued task and return the result.

        This is the main entry point for processing requests.
        """
        ...

    def handle_exception(self, exc: Exception, task: QueuedTask) -> ProviderResult:
        """Handle an exception during task processing."""
        return ProviderResult(
            exit_code=1,
            reply=str(exc),
            req_id=task.req_id,
            session_key=f"{self.key}:unknown",
            done_seen=False,
            status="failed",
        )

    def on_start(self) -> None:
        """Called when the daemon starts. Override for initialization."""
        pass

    def on_stop(self) -> None:
        """Called when the daemon stops. Override for cleanup."""
        pass
