"""Destination-side journal and config for remote CCB delivery (M1).

Ownership model (ratified: docs/plans/cross-machine-m1-plan.md sections 3, 10):
the journal belongs to the DESTINATION USER, not to one project, because a
remote request may name any allowlisted alias and because `status` must answer
even when the project daemon that accepted it is gone. It therefore lives in a
private per-user directory:

    ~/.ccb/remote/config.json          principals + aliases (0600)
    ~/.ccb/remote/journal/<id>.json    one record per accepted request (0600)
    ~/.ccb/remote/journal/<id>.lock    per-id serialisation (flock, never unlinked)

Each record names the daemon that accepted it (`owner_daemon`, the absolute
askd state file) and that daemon alone processes it: it is the only process
that can enqueue into its own pool, and recovery re-enqueues only records this
daemon owns. Two daemons can never act on one record: the per-id flock covers
every read-compare-write-enqueue, every state transition re-checks the owner,
and the owner field decides who may resume work.

Orphan records (M1, accepted): a record accepted by a daemon that never comes
back stays exactly as it is -- `accepted-pending` or `outcome-unknown` -- and
is visible through `status`. No other daemon adopts it, because adopting a
record would mean dispatching work whose queue and ownership are gone. Manual
resolution (or a future `ccb remote cleanup`) is the M1 answer; nothing in the
journal deletes it.

Durability: every write is a same-directory temp file, `fsync(file)`,
`os.replace`, `fsync(dir)`. Any failure raises :class:`JournalError` and the
caller must not acknowledge acceptance or dispatch.

States (M1 delivery-only):

    accepted-pending -> dispatch-in-progress -> delivered | failed | outcome-unknown

States (M2, a schema v2 record that asked for one reply):

    delivered -> answered

`answered` is written in the SAME durable write that stores the reply text and
its digest, so a reader never sees an answer without the state or the state
without the answer. `delivered` stays terminal for a delivery-only record.

`failed` is reserved for proven pre-dispatch refusal. An exception from
`send_text`, or anything after `dispatch-in-progress` was committed, is
`outcome-unknown` -- never `failed`.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

STATE_ACCEPTED = "accepted-pending"
STATE_DISPATCHING = "dispatch-in-progress"
STATE_DELIVERED = "delivered"
STATE_ANSWERED = "answered"
STATE_FAILED = "failed"
STATE_UNKNOWN = "outcome-unknown"

STATES = (
    STATE_ACCEPTED,
    STATE_DISPATCHING,
    STATE_DELIVERED,
    STATE_ANSWERED,
    STATE_FAILED,
    STATE_UNKNOWN,
)
#: For a delivery-only record `delivered` is terminal; an explicit-reply record
#: moves on to `answered`, which is terminal for the reply but never reached by
#: a v1 record.
TERMINAL_STATES = frozenset({STATE_DELIVERED, STATE_ANSWERED, STATE_FAILED})


# Bound at import time so a monkeypatched shared `time` module (used by several
# adapter tests) cannot alter the journal lock's deadline arithmetic.
# perf_counter, not monotonic: adapter tests simulate `time.monotonic`
# (monkeypatched through the shared module) and a lock deadline must still
# be real wall time.
_monotonic = time.perf_counter
_sleep = time.sleep


class RemoteConfigError(RuntimeError):
    """The destination config is unusable; every verb must refuse."""


class JournalError(RuntimeError):
    """A journal read/write failed.

    `may_exist` is True when the failure happened AFTER the record was
    renamed into place, so the file might be there even though the write was
    not confirmed durable. Callers must then treat acceptance as AMBIGUOUS:
    answer `acceptance_unknown`, never "nothing was accepted".
    """

    def __init__(self, message: str, *, may_exist: bool = False):
        super().__init__(message)
        self.may_exist = may_exist


def remote_root() -> Path:
    return Path.home() / ".ccb" / "remote"


def _ensure_private_dir(path: Path) -> Path:
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _fsync_dir(path: Path) -> None:
    """fsync a directory; a failure is an ERROR, never silently ignored.

    os.open/os.fsync failures here mean the rename is not confirmed durable,
    which is exactly the ambiguity the record's state must reflect.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_durable(path: Path, payload: dict) -> None:
    """temp file in the same dir -> fsync -> replace -> fsync(dir)."""
    directory = path.parent
    _ensure_private_dir(directory)
    handle = None
    temporary = None
    replaced = False
    try:
        fd, temporary_name = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.", suffix=".tmp")
        temporary = Path(temporary_name)
        os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(str(temporary), str(path))
        temporary = None
        replaced = True
        _fsync_dir(directory)
    except OSError as exc:
        raise JournalError(f"journal write failed: {exc}", may_exist=replaced) from exc
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Destination config
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Principal:
    name: str
    issuer_token: str
    allowed_aliases: tuple[str, ...] = ()
    #: Opt-in "share every eligible CCB project on this machine" (default off).
    #: A strict bool: anything else is a config error (fail closed).
    share_all: bool = False
    #: Providers a DERIVED alias may be addressed on. Only meaningful together
    #: with `share_all_providers_given`: an OMITTED key means the default set,
    #: an explicitly empty list means NO provider, so `[]` and absent must stay
    #: distinguishable all the way through the loader.
    share_all_providers: tuple[str, ...] = ()
    #: Whether the config actually carried a `share_all_providers` key.
    share_all_providers_given: bool = False


@dataclass(frozen=True)
class Alias:
    name: str
    work_dir: str
    providers: tuple[str, ...] = ()
    #: Set on an alias DERIVED from `share_all`, never on a configured one: the
    #: project identity the sender saw in `list` and must send back (A5).
    project_id: str = ""
    derived: bool = False


#: Providers a `share_all` grant covers when the config names none. Intersected
#: with the protocol's own provider set by the caller.
DEFAULT_SHARE_ALL_PROVIDERS = ("claude", "codex")

#: The alias shape every grant, configured or derived, must have (guide §5).
SHARE_ALIAS_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


def _remote_providers() -> tuple[str, ...]:
    """The protocol's own provider set, imported lazily (no import cycle)."""
    from remote_protocol import REMOTE_PROVIDERS

    return tuple(REMOTE_PROVIDERS)


def derive_alias_name(work_dir: str) -> str:
    """The alias a `share_all` grant derives from one work_dir.

    The folder name lowercased, every run of non `[a-z0-9]` collapsed to `-`,
    trimmed and truncated to 32 characters. A name that still does not match
    the alias shape is returned unchanged for the caller to reject: a derived
    alias must satisfy exactly the rule a configured one does.
    """
    name = Path(str(work_dir or "")).name.lower()
    name = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return name[:32].strip("-")


@dataclass(frozen=True)
class EffectiveAliases:
    """What one principal may actually address right now.

    Computed per request from the config PLUS the projects eligible for this
    destination, and never written back: `share_all` adds no config entries.
    `skipped` names a derived alias that was refused because its name was
    ambiguous, with the reason `list` reports.
    """

    aliases: dict[str, Alias] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    share_all: bool = False

    def derived_names(self) -> tuple[str, ...]:
        return tuple(name for name, alias in self.aliases.items() if alias.derived)


def _resolved_path(value: str) -> str:
    """A canonical absolute path for comparison, or "" when unusable."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except (OSError, RuntimeError):
        return text


@dataclass(frozen=True)
class RemoteConfig:
    principals: dict[str, Principal] = field(default_factory=dict)
    aliases: dict[str, Alias] = field(default_factory=dict)

    def principal(self, name: str) -> Principal | None:
        return self.principals.get((name or "").strip())

    def effective_aliases(
        self, principal_name: str, projects: object = ()
    ) -> EffectiveAliases:
        """The ONE seam both `list` and `submit` authorize through.

        Explicit aliases always apply. With `share_all` on, every eligible
        project (a registry work_dir that exists, is not under the system temp
        dir and holds a project config dir) is added under its derived alias:

        * an explicit alias of the same name wins, whatever it points at;
        * an explicit alias pointing at the same canonical work_dir suppresses
          the derived one (one project, one name);
        * two derived projects whose names normalise identically are BOTH
          dropped and reported as skipped -- an ambiguous name must never be
          resolved by picking one.
        """
        principal = self.principal(principal_name)
        if principal is None:
            return EffectiveAliases()
        aliases: dict[str, Alias] = {
            name: self.aliases[name]
            for name in principal.allowed_aliases
            if name in self.aliases
        }
        if not principal.share_all:
            return EffectiveAliases(aliases=aliases, share_all=False)

        if principal.share_all_providers_given:
            # P2-b: an explicit list is honoured EXACTLY, so `[]` derives
            # nothing at all rather than silently falling back to the default.
            providers = tuple(
                provider
                for provider in principal.share_all_providers
                if provider in _remote_providers()
            )
        else:
            providers = tuple(
                provider
                for provider in DEFAULT_SHARE_ALL_PROVIDERS
                if provider in _remote_providers()
            )
        if not providers:
            # No provider is addressable, so no derived alias may exist: `list`
            # omits them entirely and `submit` refuses them, which keeps the two
            # verbs in agreement. `share_all` still reports as on -- it is the
            # grant that is on, with nothing derived under it.
            return EffectiveAliases(aliases=aliases, share_all=True)
        explicit_dirs = {
            _resolved_path(alias.work_dir) for alias in aliases.values()
        }
        candidates: list[tuple[str, dict]] = []
        seen_dirs: set[str] = set()
        for project in projects if isinstance(projects, (list, tuple)) else ():
            if not isinstance(project, dict):
                continue
            # R1: the project's work_dir is used EXACTLY as the eligibility
            # pass produced it -- one resolve, one identity. Re-resolving here
            # would let a directory swapped for a symlink between `list` and
            # `submit` pair a fresh path with the hash computed for the old one.
            # Delivery re-resolves independently, and its result is compared
            # against this identity before anything is written.
            work_dir = str(project.get("work_dir") or "").strip()
            if not work_dir or work_dir in seen_dirs:
                continue
            seen_dirs.add(work_dir)
            candidates.append((work_dir, project))

        by_name: dict[str, list[str]] = {}
        for work_dir, _project in candidates:
            name = derive_alias_name(work_dir)
            if not SHARE_ALIAS_RE.fullmatch(name):
                continue
            by_name.setdefault(name, []).append(work_dir)
        derived: dict[str, Alias] = {}
        skipped: dict[str, str] = {}
        for name in sorted(by_name):
            work_dirs = by_name[name]
            if name in aliases:
                # An explicit alias wins on a name clash.
                continue
            if len(work_dirs) > 1:
                skipped[name] = (
                    "skipped: more than one project on this machine normalises to "
                    f"this alias ({len(work_dirs)}), so none is shared under it"
                )
                continue
            work_dir = work_dirs[0]
            if work_dir in explicit_dirs:
                # Already shared explicitly under another name.
                continue
            project = next(
                item for candidate, item in candidates if candidate == work_dir
            )
            derived[name] = Alias(
                name=name,
                work_dir=work_dir,
                providers=providers,
                project_id=str(project.get("project_id") or ""),
                derived=True,
            )
        aliases.update(derived)
        return EffectiveAliases(aliases=aliases, skipped=skipped, share_all=True)

    def may_use(self, principal_name: str, alias_name: str, projects: object = ()) -> bool:
        return alias_name in self.effective_aliases(principal_name, projects).aliases

    def providers_for(
        self, principal_name: str, alias_name: str, projects: object = ()
    ) -> tuple[str, ...]:
        alias = self.effective_aliases(principal_name, projects).aliases.get(alias_name)
        return tuple(alias.providers) if alias else ()

    def allowed_aliases(self, principal_name: str, projects: object = ()) -> tuple[str, ...]:
        return tuple(self.effective_aliases(principal_name, projects).aliases)


def _string_list(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise RemoteConfigError(f"{field_name} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _strict_bool(value: object, field_name: str) -> bool:
    """A JSON boolean and nothing else.

    `share_all` decides a whole-machine grant, so a ``"yes"``, ``1`` or
    ``null`` must be a config error rather than a truthy guess: every verb
    then refuses until the config is fixed, which is the fail-closed answer.
    """
    if not isinstance(value, bool):
        raise RemoteConfigError(f"{field_name} must be true or false")
    return value


def load_config(path: Path | None = None) -> RemoteConfig:
    """Load the destination config; fail closed on anything unusable.

    A missing file means "no principals, no aliases" (every verb refuses); an
    unreadable or malformed file raises, so callers refuse with a config error
    instead of silently degrading to an empty allowlist.
    """
    config_path = path or (remote_root() / "config.json")
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return RemoteConfig()
    except OSError as exc:
        raise RemoteConfigError(f"destination config is unreadable: {exc}") from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise RemoteConfigError(f"destination config is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise RemoteConfigError("destination config must be a JSON object")

    principals: dict[str, Principal] = {}
    raw_principals = document.get("principals")
    if raw_principals is None:
        raw_principals = {}
    if not isinstance(raw_principals, dict):
        raise RemoteConfigError("principals must be an object")
    for name, entry in raw_principals.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(entry, dict):
            raise RemoteConfigError("each principal must be a named object")
        token = entry.get("issuer_token")
        if not isinstance(token, str) or not token.strip():
            raise RemoteConfigError(f"principal {name} has no issuer_token")
        share_all = (
            _strict_bool(entry.get("share_all"), f"principal {name} share_all")
            if "share_all" in entry
            else False
        )
        principals[name.strip()] = Principal(
            name=name.strip(),
            issuer_token=token.strip(),
            allowed_aliases=_string_list(entry.get("allowed_aliases"), "allowed_aliases"),
            share_all=share_all,
            share_all_providers=_string_list(
                entry.get("share_all_providers"),
                f"principal {name} share_all_providers",
            ),
            share_all_providers_given="share_all_providers" in entry,
        )

    aliases: dict[str, Alias] = {}
    raw_aliases = document.get("aliases")
    if raw_aliases is None:
        raw_aliases = {}
    if not isinstance(raw_aliases, dict):
        raise RemoteConfigError("aliases must be an object")
    for name, entry in raw_aliases.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(entry, dict):
            raise RemoteConfigError("each alias must be a named object")
        work_dir = entry.get("work_dir")
        if not isinstance(work_dir, str) or not work_dir.strip():
            raise RemoteConfigError(f"alias {name} has no work_dir")
        aliases[name.strip()] = Alias(
            name=name.strip(),
            work_dir=work_dir.strip(),
            providers=_string_list(entry.get("providers"), "providers"),
        )

    return RemoteConfig(principals=principals, aliases=aliases)


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #


_LOCK_STATE = threading.local()


def _held_depths() -> dict[str, int]:
    depths = getattr(_LOCK_STATE, "held", None)
    if depths is None:
        depths = {}
        _LOCK_STATE.held = depths
    return depths


class RemoteJournal:
    """Durable per-request records under the destination user's home."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else remote_root()

    @property
    def journal_dir(self) -> Path:
        return self.root / "journal"

    def ensure(self) -> Path:
        _ensure_private_dir(self.root)
        return _ensure_private_dir(self.journal_dir)

    def record_path(self, request_id: str) -> Path:
        _local, _token = _split_id(request_id)
        return self.journal_dir / f"{request_id}.json"

    def lock_path(self, request_id: str) -> Path:
        _local, _token = _split_id(request_id)
        return self.journal_dir / f"{request_id}.lock"

    # -- per-id serialisation ------------------------------------------- #

    @contextmanager
    def locked(self, request_id: str, timeout_s: float = 15.0):
        """Serialise read-compare-write-enqueue for one request id.

        Re-entrant on the calling thread: the submit handler holds this lock
        across the enqueue, and a worker that starts synchronously inside that
        enqueue must be able to take it again without deadlocking.
        """
        held = _held_depths()
        if request_id in held:
            held[request_id] += 1
            try:
                yield
            finally:
                held[request_id] -= 1
                if held[request_id] <= 0:
                    held.pop(request_id, None)
            return

        self.ensure()
        path = self.lock_path(request_id)
        fd = None
        fcntl = None
        try:
            import fcntl as _fcntl

            fcntl = _fcntl
        except Exception:  # pragma: no cover - non-POSIX platforms
            fcntl = None
        if fcntl is None:
            held[request_id] = 1
            try:
                yield
            finally:
                held.pop(request_id, None)
            return
        try:
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise JournalError(f"journal lock file is unusable: {exc}") from exc
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        deadline = _monotonic() + max(0.0, float(timeout_s))
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if _monotonic() >= deadline:
                    os.close(fd)
                    fd = None
                    raise JournalError("journal is busy for this request id")
                _sleep(0.02)
            except OSError as exc:
                os.close(fd)
                fd = None
                raise JournalError(f"journal lock failed: {exc}") from exc
        held[request_id] = 1
        try:
            yield
        finally:
            held.pop(request_id, None)
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    os.close(fd)
                except OSError:
                    pass

    # -- records -------------------------------------------------------- #

    def read(self, request_id: str) -> dict | None:
        path = self.record_path(request_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise JournalError(f"journal record is unreadable: {exc}") from exc
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise JournalError(f"journal record is not valid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise JournalError("journal record is not an object")
        return record

    def write(self, record: dict) -> None:
        request_id = str(record.get("request_id") or "")
        _write_json_durable(self.record_path(request_id), record)

    def create(self, record: dict) -> None:
        record.setdefault("history", [])
        record["history"].append({"state": record.get("state") or STATE_ACCEPTED, "at": _now()})
        self.write(record)

    def transition(self, request_id: str, state: str, reason: str = "") -> dict:
        """Move a record to `state` durably, appending to its history."""
        if state not in STATES:
            raise JournalError(f"unknown journal state: {state}")
        record = self.read(request_id)
        if record is None:
            raise JournalError("journal record is missing")
        record["state"] = state
        entry = {"state": state, "at": _now()}
        if reason:
            entry["reason"] = reason
        record.setdefault("history", []).append(entry)
        self.write(record)
        return record

    def transition_if(
        self,
        request_id: str,
        expected: str | tuple[str, ...],
        state: str,
        reason: str = "",
        *,
        owner: str = "",
    ) -> dict | None:
        """Write `state` only while the record is still in an `expected` state.

        Returns the updated record, or None when nothing was written -- a
        record that already moved past `expected` (a worker committed dispatch
        while the submit handler was still classifying), a record owned by a
        different daemon, or a missing record. Callers must treat None as
        "this actor may not claim that state", never as success.
        """
        if state not in STATES:
            raise JournalError(f"unknown journal state: {state}")
        expected_states = (expected,) if isinstance(expected, str) else tuple(expected)
        with self.locked(request_id):
            record = self.read(request_id)
            if record is None:
                raise JournalError("journal record is missing")
            if owner and str(record.get("owner_daemon") or "") != owner:
                return None
            if str(record.get("state") or "") not in expected_states:
                return None
            record["state"] = state
            entry: dict = {"state": state, "at": _now()}
            if reason:
                entry["reason"] = reason
            record.setdefault("history", []).append(entry)
            self.write(record)
            return record

    def confirm_durable(self, request_id: str) -> None:
        """Re-sync ONE record file and its directory; raise if it cannot be proven.

        Used when an earlier write may have landed but was never confirmed
        durable (`may_exist`): the content is assumed correct, so a retry's
        success must mean "this exact file is on disk and its rename is
        synced", never "it apparently worked last time".
        """
        path = self.record_path(request_id)
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError as exc:
            raise JournalError(f"journal record cannot be re-synced: {exc}") from exc
        try:
            os.fsync(fd)
        except OSError as exc:
            raise JournalError(f"journal record sync failed: {exc}", may_exist=True) from exc
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            _fsync_dir(self.journal_dir)
        except OSError as exc:
            raise JournalError(f"journal directory sync failed: {exc}", may_exist=True) from exc

    def iter_records(self) -> list[dict]:
        directory = self.journal_dir
        if not directory.is_dir():
            return []
        records: list[dict] = []
        for path in sorted(directory.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and record.get("request_id"):
                records.append(record)
        return records


def answer_record(record: dict, text: str, digest: str) -> dict:
    """Mutate one record into `answered`, carrying the answer and its digest.

    The caller writes the returned record with :meth:`RemoteJournal.write`, so
    the answer, its digest and the `answered` state land in ONE durable write.
    The first answer is never overwritten: callers apply the reply-digest rule
    before calling this.
    """
    record["state"] = STATE_ANSWERED
    record["reply_text"] = text
    record["reply_digest"] = digest
    record.setdefault("history", []).append({"state": STATE_ANSWERED, "at": _now()})
    return record


def _split_id(request_id: str) -> tuple[str, str]:
    """Validate the id shape with the protocol module (no path traversal)."""
    from remote_protocol import parse_remote_id

    return parse_remote_id(str(request_id or ""))
