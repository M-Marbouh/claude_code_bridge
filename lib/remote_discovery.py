"""Source-side discovery cache for other machines (M3 W2).

`ccb-list` shows what each configured host last reported: its shared project
aliases, each provider's availability, and the exact live-session choices a
sender could name. Asking a host costs an SSH round trip, so the answer is
CACHED per host alias and every rendering says "last observed" -- never that it
is live.

Invalidation is by CONNECTION IDENTITY, not by age alone. A cache entry records
the connection it was observed over (ssh target, port, identity file,
known-hosts file and a digest of that host's pinned key lines). When the current
hosts entry or the pinned key no longer matches, the entry reads as ABSENT, so a
re-pointed alias can never show the projects of the machine it used to name.

Layout mirrors `remote_client.remote_root()`::

    ~/.ccb/remote/discovery/<alias>.json

A failed refresh records only *when* and *why*; the last good observation (its
timestamp and projects) is kept, so a temporary outage never erases what the
host last told us. Nothing here ever raises into a caller: discovery is
advisory, and `ccb-list` must always print its local output.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Bumped only when the meaning of a stored field changes; an entry written by
#: another shape is ignored rather than guessed at.
SCHEMA_VERSION = 2

#: A whole live refresh of ONE host: the SSH handshake plus the list read.
REFRESH_TIMEOUT_ENV = "CCB_REMOTE_LIST_TIMEOUT_S"
DEFAULT_REFRESH_TIMEOUT_S = 8.0
#: A whole `--remote` refresh across EVERY configured host. Hosts not reached
#: before it is spent are reported as "not refreshed (overall deadline)".
TOTAL_REFRESH_TIMEOUT_ENV = "CCB_REMOTE_LIST_TOTAL_TIMEOUT_S"
DEFAULT_TOTAL_REFRESH_TIMEOUT_S = 15.0

_ALIAS_RE = r"[a-z0-9][a-z0-9-]{0,31}"

#: The states a `list` reply may report for a provider (M3). Anything else is
#: dropped rather than rendered as if it were one of these.
PROVIDER_STATES = ("present", "absent", "ambiguous", "unavailable")


def _remote_root() -> Path:
    """Same location `remote_client.remote_root()` uses, kept local on purpose.

    This module must not import `remote_client` at module scope: the client
    writes the cache, so a top-level dependency the other way would be a cycle.
    """
    return Path.home() / ".ccb" / "remote"


def discovery_dir() -> Path:
    return _remote_root() / "discovery"


def cache_path(alias: str) -> Path:
    return discovery_dir() / f"{alias}.json"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _alias_ok(alias: str) -> bool:
    import re

    return bool(re.fullmatch(_ALIAS_RE, str(alias or "")))


def _positive_env(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def refresh_timeout_s() -> float:
    """Budget for ONE host's refresh."""
    return _positive_env(REFRESH_TIMEOUT_ENV, DEFAULT_REFRESH_TIMEOUT_S)


def total_refresh_timeout_s() -> float:
    """Budget for one `--remote` run across ALL configured hosts."""
    return _positive_env(TOTAL_REFRESH_TIMEOUT_ENV, DEFAULT_TOTAL_REFRESH_TIMEOUT_S)


# --------------------------------------------------------------------------- #
# Connection identity
# --------------------------------------------------------------------------- #


def known_hosts_digest(host: Any) -> str:
    """A digest of the WHOLE pinned known-hosts file, or "" if unreadable.

    The file is the only thing `ssh_argv` trusts (`StrictHostKeyChecking=yes`,
    `UserKnownHostsFile=<pinned file>`, `GlobalKnownHostsFile=/dev/null`), so
    its content IS the connection's key evidence. Digesting all of it is the
    conservative choice: `ssh` itself cannot be asked to attribute a line to a
    host for a hashed (``|1|...``) or wildcard entry, and a per-host choice
    made here would silently skip exactly those files. Any change to the file
    therefore invalidates the cache, which can only ever turn an entry into
    "unknown".
    """
    try:
        data = Path(str(getattr(host, "known_hosts", ""))).read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


def _token_digest(host: Any) -> str:
    """A digest of this host's issuer token: never the value itself.

    The token is a shared routing label, not a secret, but a cache file has no
    reason to carry it. A digest is enough to notice that the pairing changed.
    """
    token = str(getattr(host, "issuer_token", "") or "")
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def connection_identity(host: Any) -> dict:
    """Everything that must still be true for a cached observation to apply.

    Every configured connection field is covered -- endpoint, port, identity
    file, known-hosts file -- plus digests of the two things only their content
    can prove: the pinned key file and the issuer token.
    """
    try:
        port = int(getattr(host, "port", 22))
    except (TypeError, ValueError):
        port = 22
    target = str(getattr(host, "ssh_target", "") or "")
    user, _, address = target.partition("@")
    return {
        "alias": str(getattr(host, "alias", "") or ""),
        "ssh_target": target,
        "user": user if _ else "",
        "host": address if _ else target,
        "port": port,
        "identity_file": str(getattr(host, "identity_file", "") or ""),
        "known_hosts": str(getattr(host, "known_hosts", "") or ""),
        "known_hosts_sha256": known_hosts_digest(host),
        "issuer_token_sha256": _token_digest(host),
    }


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _read_raw(alias: str) -> dict | None:
    if not _alias_ok(alias):
        return None
    try:
        raw = cache_path(alias).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _matches_connection(document: dict, host: Any) -> bool:
    if host is None:
        return True
    cached = document.get("connection")
    return isinstance(cached, dict) and cached == connection_identity(host)


def _valid_document(document: dict, alias: str) -> bool:
    if document.get("schema_version") != SCHEMA_VERSION:
        return False
    if str(document.get("alias") or "") != alias:
        return False
    if _parse_iso(document.get("observed_at")) is None:
        return False
    return isinstance(document.get("projects"), list)


def read_cache(alias: str, *, host: Any = None) -> dict | None:
    """The cached observation for `alias`, or None when it does not apply.

    None covers every reason a sender must NOT show these projects: never
    queried, unreadable, written by another schema, or observed over a
    different connection identity than the one configured now.
    """
    document = _read_raw(alias)
    if document is None:
        return None
    if not _valid_document(document, alias):
        return None
    if not _matches_connection(document, host):
        return None
    return document


def _age_seconds(value: Any, now: datetime) -> int | None:
    observed = _parse_iso(value)
    if observed is None:
        return None
    return max(0, int((now - observed).total_seconds()))


def _failure_view(failure: Any, now: datetime) -> dict | None:
    if not isinstance(failure, dict):
        return None
    at = str(failure.get("at") or "").strip()
    return {
        "at": at,
        "age_seconds": _age_seconds(at, now),
        "reason": str(failure.get("reason") or "").strip()[:200],
    }


def cache_view(alias: str, *, host: Any = None, now: datetime | None = None) -> dict:
    """One host's cache, shaped for rendering and JSON output.

    ``status`` is ``known`` only for an observation made over the CURRENT
    connection identity; otherwise the host reads as ``unknown`` and no
    projects are exposed. A refresh failure is reported only when it belongs to
    that same connection -- an old failure against a connection we no longer
    use says nothing about this one.
    """
    now = now or _utcnow()
    view = {
        "alias": alias,
        "status": "unknown",
        "observed_at": None,
        "age_seconds": None,
        "projects": [],
        "last_refresh_failure": None,
    }
    document = _read_raw(alias)
    if document is None or not _matches_connection(document, host):
        return view
    view["last_refresh_failure"] = _failure_view(document.get("last_refresh_failure"), now)
    if not _valid_document(document, alias):
        return view
    view["status"] = "known"
    view["observed_at"] = str(document.get("observed_at") or "")
    view["age_seconds"] = _age_seconds(document.get("observed_at"), now)
    view["projects"] = document.get("projects") or []
    return view


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def _write_document(alias: str, document: dict) -> bool:
    """Atomic, private write of one cache entry; False on any failure."""
    if not _alias_ok(alias):
        return False
    directory = discovery_dir()
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(directory),
            prefix=f".{alias}.",
            suffix=".tmp",
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, cache_path(alias))
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(str(directory), flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception:
        return False
    return True


def write_success(host: Any, projects: list[dict], *, now: datetime | None = None) -> bool:
    """Record a successful observation for this host. Never raises.

    The refresh-failure marker is cleared: this observation supersedes it.
    """
    alias = str(getattr(host, "alias", "") or "")
    moment = now or _utcnow()
    document = {
        "schema_version": SCHEMA_VERSION,
        "alias": alias,
        "observed_at": _iso(moment),
        "connection": connection_identity(host),
        "projects": projects if isinstance(projects, list) else [],
    }
    return _write_document(alias, document)


def record_failure(host: Any, reason: str, *, now: datetime | None = None) -> bool:
    """Record that a refresh failed; keep the last good observation. Never raises.

    Only the failure marker is touched. A host that was never observed keeps an
    empty project list and no `observed_at`, so it renders as "unknown (never
    checked)" plus the failure line -- it never borrows another observation.
    """
    alias = str(getattr(host, "alias", "") or "")
    moment = now or _utcnow()
    clean_reason = " ".join(str(reason or "").split())[:200] or "refresh failed"
    existing = _read_raw(alias)
    if existing is not None and _valid_document(existing, alias):
        document = dict(existing)
        if not _matches_connection(document, host):
            # The last observation was made over a different connection; it
            # must not survive under the current one. Keep its timestamp and
            # projects out of the record entirely.
            document = {
                "schema_version": SCHEMA_VERSION,
                "alias": alias,
                "observed_at": None,
                "connection": connection_identity(host),
                "projects": [],
            }
    else:
        document = {
            "schema_version": SCHEMA_VERSION,
            "alias": alias,
            "observed_at": None,
            "connection": connection_identity(host),
            "projects": [],
        }
    document["last_refresh_failure"] = {"at": _iso(moment), "reason": clean_reason}
    return _write_document(alias, document)


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #


def _abort_quietly(session: Any) -> None:
    """Abandon a session without waiting for it to shut down.

    The expiry path must never block: `SshSession.abort()` kills the child at
    once instead of waiting up to 5 s (then 5 s more) the way `close()` does.
    A session object that has no `abort` (a test double) still gets a `close`,
    because nothing else is available -- that path is never a real ssh call.
    """
    abort = getattr(session, "abort", None)
    try:
        if callable(abort):
            abort()
        else:
            session.close()
    except Exception:
        pass


def refresh(
    host: Any,
    *,
    timeout_s: float | None = None,
    session_factory: Any = None,
    out: Any = None,
    err: Any = None,
) -> dict:
    """One bounded live refresh of `host`; the cache is the result. Never raises.

    The whole call runs on a worker thread and a call still running at the
    deadline is reported (and recorded) as a refresh failure, so an SSH
    handshake that never returns cannot hang `ccb-list`.

    An abandoned worker is treated as having no authority at all. A single
    lock-guarded step -- the only place this refresh may write the cache or set
    the reported outcome -- refuses to act once the deadline has set the
    refresh's `expired` flag, and every session the worker opened is ABORTED at
    that moment (killed, not gracefully closed: the caller is already late and
    must not inherit another wait). A slow worker that finishes after the
    deadline therefore can neither overwrite a newer observation nor change the
    result already returned to the caller.
    """
    alias = str(getattr(host, "alias", "") or "")
    budget = refresh_timeout_s() if timeout_s is None else max(0.0, float(timeout_s))

    lock = threading.Lock()
    state = {"expired": False, "outcome": {"alias": alias, "ok": False, "reason": ""}}
    opened: list = []

    def settle(kind: str, payload: Any, *, ok: bool, reason: str) -> dict | None:
        """The one commit point: writes only before the deadline expires."""
        with lock:
            if state["expired"]:
                return None
            if kind == "success":
                write_success(host, payload)
            elif kind == "failure":
                record_failure(host, str(payload))
            state["outcome"] = {"alias": alias, "ok": ok, "reason": reason}
            return dict(state["outcome"])

    def commit(kind: str, payload: Any) -> None:
        # `list_projects` reports every outcome through here, so a late worker
        # has no other way to touch the cache.
        settle(kind, payload, ok=kind == "success", reason="" if kind == "success" else str(payload))

    def open_session(argv: list[str]) -> Any:
        factory = session_factory or _default_session_factory(budget)
        session = factory(argv)
        with lock:
            expired = state["expired"]
            if not expired:
                opened.append(session)
        if expired:
            # The refresh is over; the worker must not use a live SSH process.
            _abort_quietly(session)
        return session

    def run() -> None:
        try:
            import remote_client

            remote_client.list_projects(
                alias,
                hosts={alias: host},
                session_factory=open_session,
                out=out,
                err=err,
                timeout_s=budget,
                commit=commit,
            )
        except Exception as exc:  # never let a refresh break ccb-list
            reason = f"{type(exc).__name__}: {exc}"
            settle("failure", reason, ok=False, reason=reason)

    worker = threading.Thread(target=run, name=f"ccb-remote-refresh-{alias}", daemon=True)
    worker.start()
    # The worker's own socket reads are already bounded by `budget`, so this is
    # the hard wall-clock bound of the whole refresh.
    worker.join(budget)
    if worker.is_alive():
        # One decimal so a slice measured in milliseconds reads sanely too.
        timed_out = f"timed out after {budget:.1f}s"
        with lock:
            state["expired"] = True
            record_failure(host, timed_out)
            state["outcome"] = {"alias": alias, "ok": False, "reason": timed_out}
            sessions = list(opened)
        for session in sessions:
            # ABORT, never close: this caller is already past its deadline, and
            # a graceful close can wait twice for a stalled child.
            _abort_quietly(session)
    with lock:
        return dict(state["outcome"])


def _default_session_factory(budget: float):
    def factory(argv: list[str]) -> Any:
        import remote_client

        return remote_client.SshSession(argv, timeout_s=budget)

    return factory
