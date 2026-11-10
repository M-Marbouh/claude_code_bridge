"""Source side of cross-machine CCB delivery (M1 delivery; M2 explicit replies).

Responsibilities, in order:

1. Source config: ``~/.ccb/remote/hosts.json`` -- host alias -> ssh target,
   isolated key, pinned known-hosts file and the issuer token the destination
   binds to that key's principal.
2. A durable OUTBOUND receipt written BEFORE any SSH process is spawned, in a
   persistent per-user directory (local ask receipts live in the system temp
   dir, ``lib/task_receipts.py:25``, which can be cleared on reboot). For a
   reply-expected submission the receipt also captures the caller's exact pane
   identity BEFORE the submit, and holds the answer once it is fetched.
3. Exactly one SSH call, argv-only (never a shell string), with an isolated
   option set: one identity, no agent, no control connection, strict host-key
   checking against a pinned file, forwarding disabled, and no remote command
   (the forced command in ``authorized_keys`` is the only thing that runs).
4. Honest source states: ``not-delivered`` (no submit byte was written, or the
   destination refused before acceptance), ``acceptance-unknown`` (submit bytes
   were written but no valid acknowledgement arrived), ``accepted`` (valid
   ack). Nothing is ever resubmitted automatically.

M2 explicit replies are pull-based: the source asks the destination for the
answer (``fetch``) and never accepts a reverse connection. The answer is
persisted in the receipt BEFORE any notification; the caller's pane is then
revalidated against the identity captured before submit and receives the answer
at most once, or the receipt stays ``fetched`` for ``pend``.

The protocol itself lives in ``lib/remote_protocol.py`` (T1); the destination
implementations live in ``lib/remote_delivery.py`` (T2). This module never
decides authorization and never resolves a destination -- it can only name a
configured host alias and a destination-side project alias.
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import remote_protocol as proto

SSH_BIN_ENV = "CCB_SSH_BIN"
#: Test-only: shorten the per-operation deadline so a stalled peer is provable
#: without a 30 s wait. Production always uses the plan's 30 s.
OP_TIMEOUT_ENV = "CCB_REMOTE_OP_TIMEOUT_S"

CONNECT_TIMEOUT_S = 10
SERVER_ALIVE_INTERVAL_S = 5
SERVER_ALIVE_COUNT_MAX = 3
OP_TIMEOUT_S = 30.0

STATE_SUBMITTING = "submitting"
STATE_NOT_DELIVERED = "not-delivered"
STATE_ACCEPTANCE_UNKNOWN = "acceptance-unknown"
STATE_ACCEPTED = "accepted"

#: Source-only states for a reply-expected (schema v2) submission. The
#: destination's own states are mirrored into ``remote_state`` instead; these
#: describe what the SOURCE has done with an answer.
#:   answer-available     destination reports `answered`; text not stored yet
#:   fetched              answer text is durably stored in this receipt
#:   notified             answer was injected into the captured caller pane
#:   fetch-unavailable    the last fetch failed / was refused (receipt kept)
STATE_ANSWER_AVAILABLE = "answer-available"
STATE_FETCHED = "fetched"
STATE_NOTIFIED = "notified"
STATE_FETCH_UNAVAILABLE = "fetch-unavailable"

#: Source states that already hold (or prove the existence of) an answer. A
#: status refresh may mirror the destination, but must never move a receipt
#: out of these, and a second fetch must never overwrite the first answer.
ANSWER_STATES = frozenset({STATE_ANSWER_AVAILABLE, STATE_FETCHED, STATE_NOTIFIED})

#: Bounded polling backoff shared by ``--wait`` and the source daemon's poller.
REPLY_BACKOFF_START_S = 2.0
REPLY_BACKOFF_CAP_S = 15.0

#: Deadlines use perf_counter, bound at import: adapter tests simulate
#: `time.monotonic` through the shared module, and that must not move a
#: transport deadline.
_monotonic = time.perf_counter

EXIT_ACCEPTED = 0
EXIT_NOT_DELIVERED = 1
EXIT_ACCEPTANCE_UNKNOWN = 2

#: The ONLY refusal codes that may claim `not-delivered`: each one is emitted
#: strictly BEFORE the destination durably accepts a request. Verified against
#: `remote_delivery.handle_submit` / `remote_journal` / `bin/ccb-remote-gate`:
#:
#:   unauthorized               ACL refusal, nothing written
#:   not_found                  duplicate id owned by another principal
#:   invalid_request            T1 schema/op_id refusal (and the gate's own)
#:   unsupported_provider       provider not allowed for the alias
#:   reply_expected_unsupported M1 advertises delivery-only
#:   destination_unavailable    resolution refused, or the enqueue-time check
#:                              refused before any worker saw the task
#:   journal_failure            journal write failed with `may_exist` FALSE, so
#:                              no record can exist (a may-exist failure answers
#:                              `acceptance_unknown` instead)
#:   idempotency_conflict       same id, different payload/target
#:   daemon_unavailable         the gate found no askd; nothing was forwarded
#:   config_invalid             destination config unusable
#:
#: Everything else -- `internal_error` (raised after the record exists), the
#: gate's frame-level codes, unknown or future codes, and `acceptance_unknown`
#: itself -- becomes `acceptance-unknown`, because it cannot be proven that
#: nothing was accepted.
_PROVEN_PRE_ACCEPTANCE_CODES = frozenset(
    {
        proto.ERR_UNAUTHORIZED,
        proto.ERR_NOT_FOUND,
        proto.ERR_INVALID_REQUEST,
        proto.ERR_UNSUPPORTED_PROVIDER,
        proto.ERR_REPLY_NOT_SUPPORTED,
        proto.ERR_DESTINATION_UNAVAILABLE,
        proto.ERR_JOURNAL_FAILURE,
        proto.ERR_IDEMPOTENCY_CONFLICT,
        proto.ERR_DAEMON_UNAVAILABLE,
        proto.ERR_CONFIG_INVALID,
    }
)

#: Destination states a valid acceptance may carry. M1 never captures a reply,
#: so `delivered` is the only terminal success state.
_ACCEPTED_DESTINATION_STATES = frozenset(
    {"accepted-pending", "dispatch-in-progress", "delivered"}
)

_HOST_ALIAS_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_SSH_TARGET_RE = re.compile(r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._-]*")
_ISSUER_TOKEN_RE = re.compile(proto.HOST_TOKEN_PATTERN)
_PROJECT_ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class RemoteConfigError(RuntimeError):
    """The source host config is unusable; no SSH call may be attempted."""


class RemoteRefusal(RuntimeError):
    """A local refusal before any receipt or connection was made."""


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def remote_root() -> Path:
    return Path.home() / ".ccb" / "remote"


def hosts_config_path() -> Path:
    return remote_root() / "hosts.json"


def outbound_dir() -> Path:
    return remote_root() / "outbound"


def receipt_path(request_id: str) -> Path:
    proto.parse_remote_id(request_id)  # id shape is also a filename safety check
    return outbound_dir() / f"{request_id}.json"


def _ensure_private_dir(path: Path) -> Path:
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _fsync_dir(path: Path) -> None:
    """fsync a directory; a failure is an ERROR, never silently ignored.

    The receipt is the source's only durable record of a submission, and it is
    written BEFORE ssh is spawned. If the directory cannot be synced, the
    rename is unconfirmed, so the caller must stop: nothing may be sent.
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
    temporary: Optional[Path] = None
    try:
        fd, name = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.", suffix=".tmp")
        temporary = Path(name)
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
        _fsync_dir(directory)
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


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Source config
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RemoteHost:
    alias: str
    ssh_target: str
    port: int
    identity_file: str
    known_hosts: str
    issuer_token: str


def _absolute_existing_file(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RemoteConfigError(f"{field_name} must be an absolute path")
    # `~` is expanded by US, not by the shell: this value lives in a JSON file.
    raw = os.path.expanduser(value.strip())
    if not os.path.isabs(raw):
        raise RemoteConfigError(f"{field_name} must be an absolute path")
    if not os.path.isfile(raw):
        raise RemoteConfigError(f"{field_name} does not exist: {raw}")
    return raw


def load_hosts(path: Path | None = None) -> dict[str, RemoteHost]:
    """Load and validate the source host config; fail closed on anything odd."""
    config_path = path or hosts_config_path()
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RemoteConfigError(
            f"no remote hosts configured ({config_path}); create it with one entry per host"
        ) from None
    except OSError as exc:
        raise RemoteConfigError(f"remote host config is unreadable: {exc}") from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise RemoteConfigError(f"remote host config is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise RemoteConfigError("remote host config must be a JSON object")

    entries = document.get("hosts")
    if not isinstance(entries, dict):
        raise RemoteConfigError('remote host config must contain a "hosts" object')

    hosts: dict[str, RemoteHost] = {}
    for alias, entry in entries.items():
        if not isinstance(alias, str) or not _HOST_ALIAS_RE.fullmatch(alias):
            raise RemoteConfigError(f"invalid host alias: {alias!r}")
        if not isinstance(entry, dict):
            raise RemoteConfigError(f"host {alias} must be an object")
        unknown = set(entry) - {
            "ssh_target",
            "port",
            "identity_file",
            "known_hosts",
            "issuer_token",
        }
        if unknown:
            raise RemoteConfigError(f"host {alias} has an unknown field")
        target = entry.get("ssh_target")
        if not isinstance(target, str) or not _SSH_TARGET_RE.fullmatch(target):
            raise RemoteConfigError(f"host {alias}: ssh_target must be [user@]host")
        if target.startswith("-"):
            raise RemoteConfigError(f"host {alias}: ssh_target may not start with '-'")
        port_raw = entry.get("port", 22)
        if isinstance(port_raw, bool) or not isinstance(port_raw, int) or not 1 <= port_raw <= 65535:
            raise RemoteConfigError(f"host {alias}: port must be 1-65535")
        token = entry.get("issuer_token")
        if not isinstance(token, str) or not _ISSUER_TOKEN_RE.fullmatch(token):
            raise RemoteConfigError(
                f"host {alias}: issuer_token must be 4-32 lowercase letters or digits"
            )
        hosts[alias] = RemoteHost(
            alias=alias,
            ssh_target=target,
            port=port_raw,
            identity_file=_absolute_existing_file(entry.get("identity_file"), f"host {alias}: identity_file"),
            known_hosts=_absolute_existing_file(entry.get("known_hosts"), f"host {alias}: known_hosts"),
            issuer_token=token,
        )
    return hosts


def host_for(alias: str, *, hosts: dict[str, RemoteHost] | None = None) -> RemoteHost:
    hosts = hosts if hosts is not None else load_hosts()
    entry = hosts.get((alias or "").strip())
    if entry is None:
        raise RemoteRefusal(
            f"host {alias!r} is not configured in {hosts_config_path()}; "
            "remote targets are <configured-host>::<project>"
        )
    return entry


def parse_selector(target: str) -> tuple[str, str]:
    """``<host>::<project>`` -> (host, project), both validated, never repaired."""
    raw = (target or "").strip()
    if raw.count("::") != 1:
        raise RemoteRefusal("remote target must be exactly <host>::<project>")
    host, project = raw.split("::", 1)
    if not _HOST_ALIAS_RE.fullmatch(host):
        raise RemoteRefusal(f"invalid remote host alias: {host!r}")
    if not _PROJECT_ALIAS_RE.fullmatch(project):
        raise RemoteRefusal(f"invalid destination project alias: {project!r}")
    return host, project


# --------------------------------------------------------------------------- #
# SSH argv and transport
# --------------------------------------------------------------------------- #


def ssh_config_filename(path: str) -> str:
    """Encode ONE filesystem path as an ssh_config filename argument.

    OpenSSH parses `UserKnownHostsFile` as a whitespace-separated LIST, so a
    path containing a space would silently become several files. Double-quoting
    with `\\` and `\"` escapes makes it one filename, exactly as ssh_config(5)
    specifies.
    """
    escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def ssh_argv(host: RemoteHost, *, ssh_bin: str | None = None) -> list[str]:
    """The isolated, argv-only SSH invocation. No remote command argument.

    ``-F /dev/null`` and ``GlobalKnownHostsFile=/dev/null`` mean the caller's
    own ``~/.ssh/config`` (a ``ProxyCommand``, extra identities, a competing
    control socket) cannot influence this call. The destination's forced
    command runs; nothing here is ever interpreted by a shell.
    """
    binary = ssh_bin or (os.environ.get(SSH_BIN_ENV) or "").strip() or (shutil.which("ssh") or "ssh")
    return [
        binary,
        "-F", "/dev/null",
        "-T",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none",
        "-o", "ControlPath=none",
        "-o", "ControlMaster=no",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={ssh_config_filename(host.known_hosts)}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}",
        "-o", f"ServerAliveInterval={SERVER_ALIVE_INTERVAL_S}",
        "-o", f"ServerAliveCountMax={SERVER_ALIVE_COUNT_MAX}",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "PermitLocalCommand=no",
        "-i", host.identity_file,
        "-p", str(host.port),
        host.ssh_target,
    ]


def op_timeout_s() -> float:
    raw = (os.environ.get(OP_TIMEOUT_ENV) or "").strip()
    if not raw:
        return OP_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return OP_TIMEOUT_S
    return value if value > 0 else OP_TIMEOUT_S


class SessionError(RuntimeError):
    """A transport failure, carrying whether any submit byte reached the pipe."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail


class _SniffingReader:
    """Pass-through reader that keeps the first bytes it saw, for diagnostics.

    A destination that answers with something other than a CCB frame (a login
    banner, a `nologin` notice, a proxy greeting) is otherwise reported only as
    `malformed response`. The first bytes are bounded and sanitised before they
    ever reach a reason string, and they cannot contain remote message text:
    that travels over stdin, never back through the response stream.
    """

    def __init__(self, stream, keep: int = 200):
        self._stream = stream
        self._keep = keep
        self.head = ""

    def read(self, size: int) -> bytes:
        chunk = self._stream.read(size)
        self._record(chunk)
        return chunk

    def _record(self, chunk: bytes) -> None:
        if not chunk or len(self.head) >= self._keep:
            return
        raw = chunk[: self._keep - len(self.head)]
        self.head += "".join(
            char if 32 <= ord(char) <= 126 else " " for char in raw.decode("utf-8", "replace")
        )

    def drain(self, timeout: float = 0.2) -> str:
        """Read a little more for the preview, bounded in time and size."""
        deadline = _monotonic() + timeout
        while len(self.head) < self._keep:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([self._stream], [], [], min(remaining, 0.1))
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                chunk = self._stream.read(self._keep - len(self.head))
            except (OSError, ValueError):
                break
            if not chunk:
                break
            self._record(chunk)
        return self.head


class SshSession:
    """One SSH call: framed stdin/stdout, bounded per-operation waits."""

    def __init__(self, argv: list[str], *, timeout_s: float | None = None):
        self.argv = argv
        self.timeout_s = op_timeout_s() if timeout_s is None else timeout_s
        self._frames: queue.Queue = queue.Queue()
        self._stderr_tail = ""
        self._stream_head = ""
        self._closing = False
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=True,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._err = threading.Thread(target=self._stderr_loop, daemon=True)
        self._err.start()

    # -- threads -------------------------------------------------------- #

    def _read_loop(self) -> None:
        sniffer = _SniffingReader(self._proc.stdout)
        try:
            while True:
                frame = proto.read_frame(sniffer)
                if frame is None:
                    self._frames.put(("eof", None))
                    return
                self._frames.put(("frame", frame))
        except proto.ProtocolError as exc:
            self._stream_head = " ".join(sniffer.drain().split())
            self._frames.put(("error", exc))
        except Exception as exc:  # pragma: no cover - transport closed under us
            self._frames.put(("error", exc))

    def _stderr_loop(self) -> None:
        try:
            while True:
                chunk = self._proc.stderr.read(4096)
                if not chunk:
                    return
                self._stderr_tail = (self._stderr_tail + chunk.decode("utf-8", "replace"))[-2000:]
        except Exception:
            return

    # -- io ------------------------------------------------------------- #

    def stderr_tail(self) -> str:
        return " ".join(self._stderr_tail.split())[-300:]

    def stderr_summary(self, limit: int = 200) -> str:
        """What ssh itself said: last non-empty lines, printable ASCII, bounded.

        Only ever used in failure REASONS. The message text never reaches ssh's
        stderr (it travels over stdin, not as an argument), and the buffer is
        capped in `_stderr_loop`, so this can neither grow without bound nor
        leak remote content.
        """
        lines = [line.strip() for line in self._stderr_tail.replace("\r", "\n").splitlines()]
        lines = [line for line in lines if line]
        joined = " | ".join(lines[-2:])
        printable = "".join(char if 32 <= ord(char) <= 126 else " " for char in joined)
        return " ".join(printable.split())[:limit]

    def response_preview(self, limit: int = 120) -> str:
        """The first bytes of a response that was not a frame (bounded, printable)."""
        return " ".join(self._stream_head.split())[:limit]

    def exit_detail(self, exc: SessionError) -> str:
        """A reason that says what ssh reported, e.g. `ssh exited 255: …`.

        Classification is unchanged: this only refines the text of a
        `not-delivered` result. The state machine decides, never this helper.
        """
        try:
            self._err.join(timeout=0.3)
        except Exception:
            pass
        returncode = self._proc.poll()
        summary = self.stderr_summary()
        # A pure transport failure has no useful detail of our own, so lead with
        # what ssh said. A protocol-level verdict we already have (incompatible
        # peer, refused handshake) keeps its own text and merely gains ssh's.
        if exc.kind in {"eof", "closed"}:
            if returncode is not None:
                base = f"ssh exited {returncode}"
                return f"{base}: {summary}" if summary else base
            return f"{exc.detail}: {summary}" if summary else exc.detail
        parts = [exc.detail]
        if summary:
            parts.append(summary)
        preview = self.response_preview()
        if preview:
            parts.append(f"response text: {preview}")
        return " | ".join(parts)

    def read_frame(self, timeout_s: float | None = None) -> dict:
        budget = self.timeout_s if timeout_s is None else timeout_s
        deadline = _monotonic() + max(0.0, budget)
        while True:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise SessionError("timeout", "no response from the destination in time")
            try:
                kind, payload = self._frames.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                if self._proc.poll() is not None:
                    # Let the reader flush whatever it already saw.
                    try:
                        kind, payload = self._frames.get(timeout=0.2)
                    except queue.Empty:
                        raise SessionError(
                            "eof", f"ssh exited ({self._proc.returncode}) without a complete response"
                        ) from None
                else:
                    continue
            if kind == "frame":
                return payload
            if kind == "eof":
                raise SessionError("eof", "the destination closed the connection without a response")
            if kind == "error":
                raise SessionError("malformed", f"malformed response: {getattr(payload, 'code', 'error')}")
            raise SessionError("malformed", "unexpected response")

    def write_all(self, payload: bytes, timeout_s: float | None = None) -> int:
        """Write one frame; returns bytes written (a partial write is reported, not hidden).

        The descriptor is non-blocking, so a partly full pipe can only ever cost
        the remaining deadline -- never an unbounded block past it. Byte progress
        is preserved on EVERY failure path: a select/write error after at least
        one byte returns the count, and the caller classifies that as
        `acceptance-unknown` (the destination may have part of the frame).
        Raises :class:`SessionError` only when NOTHING could be written.
        """
        budget = self.timeout_s if timeout_s is None else timeout_s
        deadline = _monotonic() + max(0.0, budget)
        stdin = self._proc.stdin
        assert stdin is not None
        fd = stdin.fileno()
        try:
            os.set_blocking(fd, False)
        except OSError:
            pass
        view = memoryview(payload)
        written = 0
        while view:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                if written == 0:
                    raise SessionError("timeout", "could not write the request in time")
                return written
            try:
                chunk = os.write(fd, view[:65536])
            except BlockingIOError:
                chunk = 0
            except (BrokenPipeError, OSError) as exc:
                if written == 0:
                    raise SessionError("closed", f"connection closed before the request was written: {exc}") from exc
                return written
            if chunk > 0:
                written += chunk
                view = view[chunk:]
                continue
            # Pipe is full right now: wait for writability, but only within the
            # operation deadline.
            try:
                _readable, ready, _exceptional = select.select([], [fd], [], min(remaining, 0.5))
            except (OSError, ValueError) as exc:
                if written == 0:
                    raise SessionError("closed", f"connection closed while writing: {exc}") from exc
                return written
            if not ready and self._proc.poll() is not None and written == 0:
                raise SessionError("closed", f"ssh exited ({self._proc.returncode}) before the request was written")
        return written

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=5)
            except Exception:
                pass
        for stream in (self._proc.stdout, self._proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    def __enter__(self) -> "SshSession":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass
class RemoteResult:
    request_id: str
    host: str
    state: str
    exit_code: int
    reason: str = ""
    error_code: str = ""
    remote_state: str = ""
    digest: str = ""

    @property
    def ok(self) -> bool:
        return self.state == STATE_ACCEPTED


@dataclass
class StatusResult:
    request_id: str
    host: str
    exit_code: int
    state: str = ""
    history: list[dict] = field(default_factory=list)
    reason: str = ""
    error_code: str = ""


@dataclass
class ListResult:
    host: str
    exit_code: int
    projects: list[dict] = field(default_factory=list)
    reason: str = ""
    error_code: str = ""


# --------------------------------------------------------------------------- #
# Receipts
# --------------------------------------------------------------------------- #


def read_receipt(request_id: str) -> dict:
    path = receipt_path(request_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RemoteRefusal(f"no outbound receipt for {request_id}") from None
    except OSError as exc:
        raise RemoteRefusal(f"outbound receipt is unreadable: {exc}") from exc
    try:
        record = json.loads(raw)
    except ValueError as exc:
        raise RemoteRefusal(f"outbound receipt is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise RemoteRefusal("outbound receipt is not an object")
    return record


def _append_history(record: dict, state: str, reason: str = "", **extra: Any) -> None:
    entry: dict[str, Any] = {"state": state, "at": _now()}
    if reason:
        entry["reason"] = reason
    for key, value in extra.items():
        if value not in (None, ""):
            entry[key] = value
    record.setdefault("history", []).append(entry)


def save_receipt(record: dict) -> None:
    request_id = str(record.get("request_id") or "")
    if not request_id:
        raise RemoteRefusal("receipt has no request id")
    _write_json_durable(receipt_path(request_id), record)


#: Re-entrant per-receipt file lock. Every receipt update -- submit, status,
#: --wait, the daemon poller and pend -- goes through it, so two processes can
#: never both transition the same receipt (one notification, one answer).
_LOCK_STATE = threading.local()


@contextmanager
def _receipt_lock(request_id: str, timeout_s: float = 15.0):
    proto.parse_remote_id(request_id)

    held = getattr(_LOCK_STATE, "held", None)
    if held is None:
        held = {}
        _LOCK_STATE.held = held
    if request_id in held:
        held[request_id] += 1
        try:
            yield
        finally:
            held[request_id] -= 1
            if held[request_id] <= 0:
                held.pop(request_id, None)
        return

    _ensure_private_dir(outbound_dir())
    path = outbound_dir() / f"{request_id}.lock"
    try:
        import fcntl as _fcntl
    except Exception:  # pragma: no cover - non-POSIX platforms
        _fcntl = None
    if _fcntl is None:  # pragma: no cover - non-POSIX platforms
        held[request_id] = 1
        try:
            yield
        finally:
            held.pop(request_id, None)
        return

    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    deadline = _monotonic() + max(0.0, float(timeout_s))
    while True:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if _monotonic() >= deadline:
                os.close(fd)
                raise RemoteRefusal("the outbound receipt is busy for this request id") from None
            time.sleep(0.02)
    held[request_id] = 1
    try:
        yield
    finally:
        held.pop(request_id, None)
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def update_receipt(request_id: str, mutate: Callable[[dict], Any]) -> dict:
    """Read-modify-write one receipt under its lock; ``mutate`` returns False to skip.

    Durability is the same same-directory temp + fsync + replace + fsync(dir)
    used for the initial receipt, so a crash can never leave a half-answer.
    """
    with _receipt_lock(request_id):
        record = read_receipt(request_id)
        decision = mutate(record)
        if decision is False:
            return record
        _write_json_durable(receipt_path(request_id), record)
        return record


#: Source states that carry (or prove the existence of) an answer, ranked so a
#: late lifecycle write can never undo one. Lower rank is earlier.
_ANSWER_STATE_RANK = {
    STATE_ANSWER_AVAILABLE: 1,
    STATE_FETCHED: 2,
    STATE_NOTIFIED: 3,
}


def apply_receipt_state(
    latest: dict,
    state: str,
    reason: str = "",
    *,
    remote_state: str | None = None,
    **extra: Any,
) -> bool:
    """Apply one source state under the shared precedence; False = refused.

    This is the ONE place that decides whether a state write is allowed, for
    every writer (submit ack, status mirror, fetch/poll, notification outcome):

    * an answer-bearing state is sticky against non-answer states;
    * answer states only move forward (answer-available -> fetched -> notified);
    * once a notification was attempted, a late lifecycle write may not replace
      the outcome that attempt owns.

    A refused write changes nothing, not even ``remote_state``, so a late
    ``accepted-pending`` ack can never hide a fetched answer from ``pend``.
    """
    current = str(latest.get("state") or "")
    current_rank = _ANSWER_STATE_RANK.get(current, 0)
    new_rank = _ANSWER_STATE_RANK.get(state, 0)
    if new_rank < current_rank:
        return False
    if current_rank and not new_rank:
        return False
    if latest.get("notify_attempted_at") and not new_rank:
        return False
    latest["state"] = state
    if remote_state is not None:
        latest["remote_state"] = remote_state
    _append_history(latest, state, reason, **extra)
    return True


def _transition(
    record: dict, state: str, reason: str = "", *, remote_state: str | None = None, **extra: Any
) -> None:
    """Record the new state best-effort; never raises, never changes the outcome.

    Uses :func:`apply_receipt_state`, so a lifecycle transition can never
    downgrade an answer the source daemon already fetched (or notified).
    """
    request_id = str(record.get("request_id") or "")
    applied = {"value": False}

    def mutate(latest: dict) -> bool:
        applied["value"] = apply_receipt_state(
            latest, state, reason, remote_state=remote_state, **extra
        )
        return applied["value"]

    try:
        update_receipt(request_id, mutate)
    except Exception as exc:
        print(f"[WARN] outbound receipt not updated ({exc}); state shown is authoritative", file=sys.stderr)
    # Keep the caller's in-memory copy useful for its own reporting.
    if applied["value"]:
        record["state"] = state
        if remote_state is not None:
            record["remote_state"] = remote_state
        _append_history(record, state, reason, **extra)


# --------------------------------------------------------------------------- #
# Protocol exchanges
# --------------------------------------------------------------------------- #


def _hello(session: SshSession, *, op_id: str) -> dict:
    session.write_all(
        proto.encode_frame(
            {
                "proto": {"major": proto.PROTO_MAJOR, "minor": proto.PROTO_MINOR},
                "op": "hello",
                "op_id": op_id,
                "body": {},
            }
        )
    )
    response = session.read_frame()
    if not isinstance(response, dict) or not response.get("ok"):
        raise SessionError(
            "refused",
            f"the destination refused the handshake ({_error_code(response) or 'no reason given'})",
        )
    capabilities = response.get("capabilities")
    if not isinstance(capabilities, dict):
        raise SessionError("incompatible", "the destination did not advertise capabilities")
    remote_proto = capabilities.get("proto")
    if not isinstance(remote_proto, dict) or remote_proto.get("major") != proto.PROTO_MAJOR:
        raise SessionError("incompatible", "incompatible protocol version at the destination")
    return capabilities


def _require_delivery_only(capabilities: dict, provider: str) -> None:
    modes = capabilities.get("reply_modes")
    modes_for_provider = modes.get(provider) if isinstance(modes, dict) else None
    if not isinstance(modes_for_provider, list) or "delivery-only" not in modes_for_provider:
        raise SessionError(
            "incompatible", f"the destination does not advertise delivery-only {provider} delivery"
        )


def _advertises_fetch(capabilities: dict) -> bool:
    verbs = capabilities.get("verbs") if isinstance(capabilities, dict) else None
    return isinstance(verbs, list) and "fetch" in verbs


def _require_explicit_reply(capabilities: dict, provider: str) -> None:
    """Refuse a reply-expected submit to a peer that cannot serve it (A5).

    Capability, never a minor version number: a 1.0/delivery-only destination
    keeps working for plain and --notify sends, and this check refuses BEFORE
    any submit byte is written.
    """
    if not proto.advertises(capabilities, provider, proto.EXPLICIT_REPLY_MODE):
        raise SessionError(
            "incompatible",
            f"the destination does not advertise explicit replies for {provider} "
            "(delivery-only); use --notify or send without --wait/--background",
        )
    if not _advertises_fetch(capabilities):
        raise SessionError(
            "incompatible",
            "the destination does not advertise the fetch verb, so an answer could never be "
            "collected; use --notify or send without --wait/--background",
        )


def _error_code(response: Any) -> str:
    if isinstance(response, dict):
        error = response.get("error")
        if isinstance(error, dict):
            return str(error.get("code") or "")
    return ""


def _error_message(response: Any) -> str:
    if isinstance(response, dict):
        error = response.get("error")
        if isinstance(error, dict):
            return " ".join(str(error.get("message") or "").split())[:300]
    return ""


def _exit_detail(session: Any, exc: SessionError) -> str:
    """`session.exit_detail` when available; the plain detail otherwise."""
    method = getattr(session, "exit_detail", None)
    if callable(method):
        try:
            return str(method(exc))
        except Exception:
            return exc.detail
    return exc.detail


def _acceptance_problem(
    ack: dict, *, request_id: str, digest: str, op: str, op_id: str
) -> str:
    """Why an `ok: true` acknowledgement cannot be trusted, else "".

    Acceptance requires ALL of: the op and op id we sent, our exact request id,
    our payload digest, and a destination state this milestone can actually be
    in. Anything missing or malformed means we cannot prove acceptance.
    """
    if ack.get("op") != op:
        return "the acknowledgement names a different operation"
    if ack.get("op_id") != op_id:
        return "the acknowledgement does not match this request"
    if ack.get("request_id") != request_id:
        return "the acknowledgement names a different request id"
    if str(ack.get("digest") or "") != digest:
        return "the acknowledgement carries a different payload digest"
    if str(ack.get("state") or "") not in _ACCEPTED_DESTINATION_STATES:
        return "the acknowledgement carries no valid destination state"
    return ""


def _reject_unexpected_ack(response: dict, *, op: str, op_id: str, request_id: str) -> str:
    """Reason string when an acknowledgement cannot be trusted, else ""."""
    if response.get("op") != op:
        return "the acknowledgement names a different operation"
    if response.get("op_id") != op_id:
        return "the acknowledgement does not match this request"
    echoed = response.get("request_id")
    if echoed and echoed != request_id:
        return "the acknowledgement names a different request"
    return ""


# --------------------------------------------------------------------------- #
# submit
# --------------------------------------------------------------------------- #


def submit(
    host_alias: str,
    project: str,
    provider: str,
    text: str,
    *,
    intent: str = "notify",
    dest_session: str = "",
    caller_claims: dict | None = None,
    reply_expected: bool = False,
    caller_identity: dict | None = None,
    poll_owner: str = "",
    hosts: dict[str, RemoteHost] | None = None,
    session_factory: Callable[[list[str]], SshSession] | None = None,
    out: Any = None,
    err: Any = None,
) -> RemoteResult:
    """Write the receipt, run one SSH call, and report the honest state.

    ``reply_expected=True`` (M2) submits schema v2 and additionally requires
    the destination's ``hello`` to advertise ``explicit-reply`` and ``fetch``
    BEFORE any submit byte is written. ``poll_owner`` is the source askd state
    file that will poll for the answer; ``--background`` refuses without it.
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    if reply_expected and intent not in ("wait", "background"):
        raise RemoteRefusal(
            "a reply-expected submission must use intent 'wait' or 'background'"
        )
    host = host_for(host_alias, hosts=hosts)

    from ccb_protocol import make_req_id

    request_id = proto.make_remote_id(make_req_id(), host.issuer_token)
    record = proto.build_request_record(
        dest_project=project,
        dest_provider=provider,
        intent=intent,
        text=text,
        request_id=request_id,
        source_claims=dict(caller_claims or {}),
        dest_session=dest_session or None,
        reply_expected=reply_expected,
    )

    receipt = {
        "request_id": request_id,
        "host": host.alias,
        "dest_project": project,
        "dest_provider": provider,
        "dest_session": dest_session,
        "digest": record["digest"],
        "schema_version": record["schema_version"],
        "intent": intent,
        "caller": dict(caller_claims or {}),
        "created": _now(),
        "state": STATE_SUBMITTING,
        "history": [],
        "remote_state": "",
    }
    if record["schema_version"] == proto.SCHEMA_VERSION_REPLY:
        # Captured BEFORE the submit: this copy is the authority a later
        # notification revalidates, never the audit-only `caller` claims.
        receipt["reply_expected"] = True
        receipt["caller_identity"] = dict(caller_identity or {})
        if poll_owner:
            receipt["poll_owner"] = str(poll_owner)
    _append_history(receipt, STATE_SUBMITTING)
    try:
        save_receipt(receipt)
    except Exception as exc:
        print(f"[ERROR] could not write the outbound receipt; nothing was sent: {exc}", file=err)
        return RemoteResult(request_id, host.alias, STATE_NOT_DELIVERED, EXIT_NOT_DELIVERED,
                            reason="outbound receipt could not be written", digest=record["digest"])

    if reply_expected and intent == "background" and not poll_owner:
        reason = "background replies need CCB running here; use --wait"
        _transition(receipt, STATE_NOT_DELIVERED, reason)
        print(f"id={request_id} state={STATE_NOT_DELIVERED} ({reason})", file=err)
        return RemoteResult(request_id, host.alias, STATE_NOT_DELIVERED, EXIT_NOT_DELIVERED,
                            reason=reason, digest=record["digest"])

    factory = session_factory or SshSession
    argv = ssh_argv(host)
    session: SshSession | None = None
    try:
        session = factory(argv)
    except Exception as exc:
        reason = f"ssh could not be started: {exc}"
        _transition(receipt, STATE_NOT_DELIVERED, reason)
        print(f"id={request_id} state={STATE_NOT_DELIVERED} ({reason})", file=err)
        return RemoteResult(request_id, host.alias, STATE_NOT_DELIVERED, EXIT_NOT_DELIVERED,
                            reason=reason, digest=record["digest"])

    written = 0
    frame = b""
    submit_op_id = f"submit-{request_id}"
    try:
        try:
            capabilities = _hello(session, op_id="hello-1")
            _require_delivery_only(capabilities, provider)
            if record["schema_version"] == proto.SCHEMA_VERSION_REPLY:
                _require_explicit_reply(capabilities, provider)
        except SessionError as exc:
            detail = _exit_detail(session, exc)
            _transition(receipt, STATE_NOT_DELIVERED, detail, ssh=str(exc.kind))
            print(f"id={request_id} state={STATE_NOT_DELIVERED} ({detail})", file=err)
            return RemoteResult(request_id, host.alias, STATE_NOT_DELIVERED, EXIT_NOT_DELIVERED,
                                reason=detail, error_code=exc.kind, digest=record["digest"])

        envelope = {
            "proto": {"major": proto.PROTO_MAJOR, "minor": proto.PROTO_MINOR},
            "op": "submit",
            "op_id": submit_op_id,
            "body": record,
        }
        frame = proto.encode_frame(envelope)
        try:
            written = session.write_all(frame)
        except SessionError as exc:
            # Nothing reached the pipe: the destination cannot have accepted it.
            detail = _exit_detail(session, exc)
            _transition(receipt, STATE_NOT_DELIVERED, detail, ssh=str(exc.kind))
            print(f"id={request_id} state={STATE_NOT_DELIVERED} ({detail})", file=err)
            return RemoteResult(request_id, host.alias, STATE_NOT_DELIVERED, EXIT_NOT_DELIVERED,
                                reason=detail, error_code=exc.kind, digest=record["digest"])

        frame_bytes = len(frame)
        try:
            ack = session.read_frame()
        except SessionError as exc:
            reason = (
                f"the request was written ({written}/{frame_bytes} bytes) but no valid "
                f"acknowledgement arrived: {exc.detail}"
                if written >= frame_bytes
                else f"the request was only partly written ({written}/{frame_bytes} bytes) and "
                     f"no acknowledgement arrived: {exc.detail}"
            )
            _transition(receipt, STATE_ACCEPTANCE_UNKNOWN, reason, ssh=str(exc.kind), bytes_written=written)
            print(f"id={request_id} state={STATE_ACCEPTANCE_UNKNOWN} ({reason})", file=err)
            print("      run `ccb-remote status " + request_id + "` to recover the destination state", file=err)
            return RemoteResult(request_id, host.alias, STATE_ACCEPTANCE_UNKNOWN,
                                EXIT_ACCEPTANCE_UNKNOWN, reason=reason, error_code=exc.kind,
                                digest=record["digest"])

        mismatch = _reject_unexpected_ack(ack, op="submit", op_id=submit_op_id, request_id=request_id)
        if mismatch:
            reason = f"unusable acknowledgement: {mismatch}"
            _transition(receipt, STATE_ACCEPTANCE_UNKNOWN, reason)
            print(f"id={request_id} state={STATE_ACCEPTANCE_UNKNOWN} ({reason})", file=err)
            return RemoteResult(request_id, host.alias, STATE_ACCEPTANCE_UNKNOWN,
                                EXIT_ACCEPTANCE_UNKNOWN, reason=reason, digest=record["digest"])

        ack_ok = ack.get("ok")
        if ack_ok is True:
            problem = _acceptance_problem(
                ack,
                request_id=request_id,
                digest=record["digest"],
                op="submit",
                op_id=submit_op_id,
            )
            if problem:
                reason = f"untrustworthy acceptance: {problem}"
                _transition(receipt, STATE_ACCEPTANCE_UNKNOWN, reason)
                print(f"id={request_id} state={STATE_ACCEPTANCE_UNKNOWN} ({reason})", file=err)
                return RemoteResult(request_id, host.alias, STATE_ACCEPTANCE_UNKNOWN,
                                    EXIT_ACCEPTANCE_UNKNOWN, reason=reason,
                                    digest=record["digest"])
            remote_state = str(ack.get("state") or "")
            _transition(receipt, STATE_ACCEPTED, "destination accepted the request",
                        remote_state=remote_state)
            expectation = " reply-expected=yes" if reply_expected else ""
            print(
                f"id={request_id} state={STATE_ACCEPTED} destination={remote_state or '-'}{expectation}",
                file=out,
            )
            return RemoteResult(request_id, host.alias, STATE_ACCEPTED, EXIT_ACCEPTED,
                                remote_state=remote_state, digest=record["digest"])

        if ack_ok is not False:
            # No boolean verdict at all: never guess, never call it a refusal.
            reason = "unusable acknowledgement: no boolean ok field"
            _transition(receipt, STATE_ACCEPTANCE_UNKNOWN, reason)
            print(f"id={request_id} state={STATE_ACCEPTANCE_UNKNOWN} ({reason})", file=err)
            return RemoteResult(request_id, host.alias, STATE_ACCEPTANCE_UNKNOWN,
                                EXIT_ACCEPTANCE_UNKNOWN, reason=reason,
                                digest=record["digest"])

        code = _error_code(ack)
        message = _error_message(ack)
        if code not in _PROVEN_PRE_ACCEPTANCE_CODES:
            reason = (
                f"the destination refused with {code or 'unknown'} after the request may "
                f"have been accepted"
            )
            if message:
                reason = f"{reason} ({message})"
            _transition(receipt, STATE_ACCEPTANCE_UNKNOWN, reason, error_code=code)
            print(f"id={request_id} state={STATE_ACCEPTANCE_UNKNOWN} ({reason})", file=err)
            return RemoteResult(request_id, host.alias, STATE_ACCEPTANCE_UNKNOWN,
                                EXIT_ACCEPTANCE_UNKNOWN, reason=reason, error_code=code,
                                digest=record["digest"])

        reason = f"the destination refused the request: {code}"
        if message:
            reason = f"{reason} ({message})"
        _transition(receipt, STATE_NOT_DELIVERED, reason, error_code=code)
        print(f"id={request_id} state={STATE_NOT_DELIVERED} ({reason})", file=err)
        return RemoteResult(request_id, host.alias, STATE_NOT_DELIVERED, EXIT_NOT_DELIVERED,
                            reason=reason, error_code=code, digest=record["digest"])
    except Exception as exc:  # unexpected: judge by whether submit bytes went out
        frame_bytes = len(frame) if frame else 0
        if frame_bytes and written > 0:
            reason = f"the request was written and then failed unexpectedly: {type(exc).__name__}"
            _transition(receipt, STATE_ACCEPTANCE_UNKNOWN, reason, bytes_written=written)
            print(f"id={request_id} state={STATE_ACCEPTANCE_UNKNOWN} ({reason})", file=err)
            return RemoteResult(request_id, host.alias, STATE_ACCEPTANCE_UNKNOWN,
                                EXIT_ACCEPTANCE_UNKNOWN, reason=reason, digest=record["digest"])
        reason = f"delivery failed before the request was written: {type(exc).__name__}"
        _transition(receipt, STATE_NOT_DELIVERED, reason)
        print(f"id={request_id} state={STATE_NOT_DELIVERED} ({reason})", file=err)
        return RemoteResult(request_id, host.alias, STATE_NOT_DELIVERED, EXIT_NOT_DELIVERED,
                            reason=reason, digest=record["digest"])
    finally:
        if session is not None:
            session.close()


# --------------------------------------------------------------------------- #
# status / list
# --------------------------------------------------------------------------- #


def status(
    request_id: str,
    *,
    session_factory: Callable[[list[str]], SshSession] | None = None,
    hosts: dict[str, RemoteHost] | None = None,
    out: Any = None,
    err: Any = None,
) -> StatusResult:
    """Query the destination state for a receipt's id; the recovery path."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    record = read_receipt(request_id)
    host = host_for(str(record.get("host") or ""), hosts=hosts)
    factory = session_factory or SshSession
    op_id = f"status-{request_id}"
    session: SshSession | None = None
    try:
        session = factory(ssh_argv(host))
        _hello(session, op_id="hello-1")
        session.write_all(
            proto.encode_frame(
                {
                    "proto": {"major": proto.PROTO_MAJOR, "minor": proto.PROTO_MINOR},
                    "op": "status",
                    "op_id": op_id,
                    "body": {"request_id": request_id},
                }
            )
        )
        response = session.read_frame()
    except SessionError as exc:
        print(f"[ERROR] status unavailable: {exc.detail}", file=err)
        return StatusResult(request_id, host.alias, EXIT_NOT_DELIVERED, reason=exc.detail,
                            error_code=exc.kind)
    except Exception as exc:
        print(f"[ERROR] status unavailable: {exc}", file=err)
        return StatusResult(request_id, host.alias, EXIT_NOT_DELIVERED, reason=str(exc))
    finally:
        if session is not None:
            session.close()

    mismatch = _reject_unexpected_ack(response, op="status", op_id=op_id, request_id=request_id)
    if mismatch:
        print(f"[ERROR] unusable status reply: {mismatch}", file=err)
        return StatusResult(request_id, host.alias, EXIT_NOT_DELIVERED, reason=mismatch)

    if not response.get("ok"):
        code = _error_code(response)
        if code == proto.ERR_NOT_FOUND:
            print("state=unknown-or-not-authorized", file=out)
            return StatusResult(request_id, host.alias, EXIT_NOT_DELIVERED,
                                reason="unknown or not authorized", error_code=code)
        reason = _error_message(response) or code or "refused"
        print(f"[ERROR] status refused: {reason}", file=err)
        return StatusResult(request_id, host.alias, EXIT_NOT_DELIVERED, reason=reason, error_code=code)

    remote_state = str(response.get("state") or "")
    history = [entry for entry in (response.get("history") or []) if isinstance(entry, dict)]
    reply_expected = record.get("reply_expected") is True

    if reply_expected:
        # A reply-expected receipt's SOURCE state is authoritative for the
        # answer. A status refresh only mirrors the destination, and can never
        # move the receipt out of an answer-bearing state or drop a stored
        # answer / notification.
        def mutate(latest: dict) -> None:
            if not remote_state:
                return
            latest["remote_state"] = remote_state
            latest.setdefault("remote_history", []).extend(
                {"state": str(entry.get("state") or ""), "at": str(entry.get("at") or "")}
                for entry in history
                if entry.get("state")
            )
            current = str(latest.get("state") or "")
            if remote_state == "answered" and current not in ANSWER_STATES:
                if current != STATE_NOT_DELIVERED:
                    apply_receipt_state(
                        latest, STATE_ANSWER_AVAILABLE, "destination reports an answer"
                    )

        try:
            updated = update_receipt(request_id, mutate)
        except Exception as exc:
            print(f"[ERROR] status unavailable: {exc}", file=err)
            return StatusResult(request_id, host.alias, EXIT_NOT_DELIVERED, reason=str(exc))
        state = str(updated.get("state") or "")
        if remote_state:
            print(f"id={request_id} state={state} destination={remote_state}", file=out)
        else:
            print(f"id={request_id} state={state} (no destination state reported)", file=out)
        return StatusResult(request_id, host.alias, EXIT_ACCEPTED, state=remote_state, history=history)

    if remote_state:
        def mutate(latest: dict) -> None:
            latest["remote_state"] = remote_state
            latest.setdefault("remote_history", []).extend(
                {"state": str(entry.get("state") or ""), "at": str(entry.get("at") or "")}
                for entry in history
                if entry.get("state")
            )
            # The receipt mirrors the destination: that is what makes a status
            # query the recovery path for an acceptance-unknown submission.
            latest["state"] = remote_state
            _append_history(latest, remote_state, "destination state via status")

        try:
            update_receipt(request_id, mutate)
        except Exception as exc:
            print(f"[ERROR] status unavailable: {exc}", file=err)
            return StatusResult(request_id, host.alias, EXIT_NOT_DELIVERED, reason=str(exc))
        print(f"id={request_id} state={remote_state} destination={remote_state}", file=out)
    else:
        print(f"id={request_id} state={record.get('state')} (no destination state reported)", file=out)
    return StatusResult(request_id, host.alias, EXIT_ACCEPTED, state=remote_state, history=history)


def list_projects(
    host_alias: str,
    *,
    session_factory: Callable[[list[str]], SshSession] | None = None,
    hosts: dict[str, RemoteHost] | None = None,
    out: Any = None,
    err: Any = None,
) -> ListResult:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    host = host_for(host_alias, hosts=hosts)
    factory = session_factory or SshSession
    op_id = "list-1"
    session: SshSession | None = None
    try:
        session = factory(ssh_argv(host))
        _hello(session, op_id="hello-1")
        session.write_all(
            proto.encode_frame(
                {
                    "proto": {"major": proto.PROTO_MAJOR, "minor": proto.PROTO_MINOR},
                    "op": "list",
                    "op_id": op_id,
                    "body": {},
                }
            )
        )
        response = session.read_frame()
    except SessionError as exc:
        print(f"[ERROR] list unavailable: {exc.detail}", file=err)
        return ListResult(host.alias, EXIT_NOT_DELIVERED, reason=exc.detail, error_code=exc.kind)
    except Exception as exc:
        print(f"[ERROR] list unavailable: {exc}", file=err)
        return ListResult(host.alias, EXIT_NOT_DELIVERED, reason=str(exc))
    finally:
        if session is not None:
            session.close()

    if response.get("op_id") != op_id:
        print("[ERROR] unusable list reply: it does not match this request", file=err)
        return ListResult(host.alias, EXIT_NOT_DELIVERED, reason="unusable list reply")
    if not response.get("ok"):
        code = _error_code(response)
        if code == proto.ERR_NOT_FOUND:
            print("no destinations authorized on this host", file=out)
            return ListResult(host.alias, EXIT_NOT_DELIVERED, reason="not authorized", error_code=code)
        reason = _error_message(response) or code or "refused"
        print(f"[ERROR] list refused: {reason}", file=err)
        return ListResult(host.alias, EXIT_NOT_DELIVERED, reason=reason, error_code=code)

    projects = response.get("projects") if isinstance(response.get("projects"), list) else []
    if not projects:
        print("no destinations authorized on this host", file=out)
    for project in projects:
        if not isinstance(project, dict):
            continue
        alias = str(project.get("alias") or "")
        providers = project.get("providers") if isinstance(project.get("providers"), list) else []
        rendered = ", ".join(
            f"{item.get('provider')}={'present' if item.get('present') else 'absent'}"
            for item in providers
            if isinstance(item, dict)
        )
        print(f"{alias}\t{rendered}", file=out)
    return ListResult(host.alias, EXIT_ACCEPTED, projects=projects)


# --------------------------------------------------------------------------- #
# Caller identity for the receipt (audit only; never authorization)
# --------------------------------------------------------------------------- #


def caller_claims(*, caller: str, work_dir: str = "", role_assertion: str = "") -> dict:
    """The exact caller identity CCB can prove locally, marked as claims."""
    from task_receipts import caller_pane, caller_session_id

    pane_id, terminal = caller_pane()
    return {
        "caller": (caller or "").strip(),
        "pane_id": pane_id,
        "terminal": terminal,
        "session_id": caller_session_id(),
        "work_dir": work_dir or "",
        "role_assertion": role_assertion or "",
        "verified": False,
    }


def capture_caller_identity(*, caller: str, work_dir: str = "", role_assertion: str = "") -> dict:
    """The caller's exact pane identity, captured BEFORE a reply-expected submit.

    Unlike ``caller_claims`` (audit-only, ``verified: false``), this copy is
    what a later notification revalidates: pane, terminal, work_dir, the
    launch/session ids CCB exported into the pane, and -- when the caller's own
    live session can be resolved from the same inventory peer delivery trusts
    -- its title marker, project id and live id. An unresolved caller leaves
    those empty, and the notification then fails closed rather than guessing.
    """
    from task_receipts import caller_pane, caller_session_id

    pane_id, terminal = caller_pane()
    identity = {
        "caller": (caller or "").strip(),
        "pane_id": pane_id,
        "terminal": terminal,
        "session_id": caller_session_id(),
        "work_dir": work_dir or "",
        "role_assertion": role_assertion or "",
        "launch_id": (os.environ.get("CCB_SESSION_ID") or "").strip(),
        "member_id": (
            os.environ.get("CCB_MEMBER_ID") or os.environ.get("CCB_LIVE_ID") or ""
        ).strip(),
        "live_id": (os.environ.get("CCB_LIVE_ID") or "").strip(),
        "pane_title_marker": "",
        "ccb_project_id": "",
        "pane_pid": "",
    }
    if not pane_id:
        return identity
    try:
        from terminal import get_backend_for_session

        backend = get_backend_for_session({"terminal": terminal})
        if backend is not None and callable(getattr(backend, "pane_pid", None)):
            identity["pane_pid"] = str(backend.pane_pid(pane_id) or "").strip()
    except Exception:
        pass
    provider = (caller or "").strip().lower()
    if provider not in proto.REMOTE_PROVIDERS or not work_dir:
        return identity
    try:
        from peer_routing import identify_sender

        resolution = identify_sender(
            work_dir, provider, pane_id=pane_id, terminal=terminal, check_daemon=False
        )
    except Exception:
        return identity
    candidate = getattr(resolution, "candidate", None)
    if candidate is None:
        return identity
    identity.update(
        {
            "pane_title_marker": str(getattr(candidate, "pane_title_marker", "") or ""),
            "ccb_project_id": str(getattr(candidate, "ccb_project_id", "") or ""),
            "live_id": str(getattr(candidate, "live_id", "") or "") or identity["live_id"],
            "launch_id": str(getattr(candidate, "launch_id", "") or "")
            or identity["launch_id"],
            "work_dir": str(getattr(candidate, "work_dir", "") or "") or identity["work_dir"],
        }
    )
    return identity


# --------------------------------------------------------------------------- #
# M2 pulls: fetch, durable answer, fail-closed notification
# --------------------------------------------------------------------------- #

#: "No reply yet" for a caller that waited past its deadline -- the same code
#: `pend` uses, and deliberately distinct from a delivery failure.
EXIT_WAITING = 2


@dataclass
class FetchResult:
    request_id: str
    host: str
    exit_code: int
    state: str = ""
    digest: str = ""
    reply_text: str = ""
    reply_digest: str = ""
    reason: str = ""
    error_code: str = ""

    @property
    def answered(self) -> bool:
        return self.state == "answered"


def fetch(
    request_id: str,
    *,
    session_factory: Callable[[list[str]], SshSession] | None = None,
    hosts: dict[str, RemoteHost] | None = None,
) -> FetchResult:
    """Ask the receipt's destination for the state, and the answer if answered.

    Read-only on the destination and silent on this side: the caller decides
    what to do with the result. An answer is only trusted when the destination
    echoes this request id and the reply digest matches the reply text.
    """
    record = read_receipt(request_id)
    host = host_for(str(record.get("host") or ""), hosts=hosts)
    factory = session_factory or SshSession
    op_id = f"fetch-{request_id}"
    session: SshSession | None = None
    try:
        session = factory(ssh_argv(host))
        capabilities = _hello(session, op_id="hello-1")
        if not _advertises_fetch(capabilities):
            return FetchResult(
                request_id,
                host.alias,
                EXIT_NOT_DELIVERED,
                reason="the destination does not advertise the fetch verb",
                error_code="incompatible",
            )
        session.write_all(
            proto.encode_frame(
                {
                    "proto": {"major": proto.PROTO_MAJOR, "minor": proto.PROTO_MINOR},
                    "op": "fetch",
                    "op_id": op_id,
                    "body": {"request_id": request_id},
                }
            )
        )
        response = session.read_frame()
    except SessionError as exc:
        return FetchResult(
            request_id, host.alias, EXIT_ACCEPTANCE_UNKNOWN, reason=exc.detail, error_code=exc.kind
        )
    except Exception as exc:
        return FetchResult(
            request_id, host.alias, EXIT_ACCEPTANCE_UNKNOWN, reason=str(exc), error_code="error"
        )
    finally:
        if session is not None:
            session.close()

    mismatch = _reject_unexpected_ack(response, op="fetch", op_id=op_id, request_id=request_id)
    if mismatch:
        return FetchResult(
            request_id, host.alias, EXIT_ACCEPTANCE_UNKNOWN, reason=mismatch, error_code="unusable"
        )
    if not response.get("ok"):
        code = _error_code(response)
        message = _error_message(response)
        reason = "unknown or not authorised" if code == proto.ERR_NOT_FOUND else (message or code or "refused")
        return FetchResult(
            request_id, host.alias, EXIT_NOT_DELIVERED, reason=reason, error_code=code
        )

    # Bind the answer to THIS request before anything is persisted or
    # notified: the frame must name our request id and carry the receipt's
    # request digest. A missing field is a refusal, never "close enough".
    if response.get("request_id") != request_id:
        return FetchResult(
            request_id,
            host.alias,
            EXIT_NOT_DELIVERED,
            reason="the answer does not name this request",
            error_code="untrusted_reply",
        )
    state = str(response.get("state") or "")
    digest = str(response.get("digest") or "")
    expected_digest = str(record.get("digest") or "")
    if (
        not digest
        or not expected_digest
        or not hmac.compare_digest(digest, expected_digest)
    ):
        return FetchResult(
            request_id,
            host.alias,
            EXIT_NOT_DELIVERED,
            reason="the answer is not bound to this request digest",
            error_code="untrusted_reply",
        )
    result = FetchResult(request_id, host.alias, EXIT_ACCEPTED, state=state, digest=digest)
    if state == "answered":
        text = response.get("reply_text")
        reply_digest = str(response.get("reply_digest") or "")
        if not isinstance(text, str) or not reply_digest:
            return FetchResult(
                request_id,
                host.alias,
                EXIT_NOT_DELIVERED,
                reason="the destination reported an answer without text or digest",
                error_code="untrusted_reply",
            )
        if not hmac.compare_digest(reply_digest, proto.compute_reply_digest(text)):
            return FetchResult(
                request_id,
                host.alias,
                EXIT_NOT_DELIVERED,
                reason="the answer does not match its digest",
                error_code="untrusted_reply",
            )
        result.reply_text = text
        result.reply_digest = reply_digest
    return result


def _spool_log(line: str) -> None:
    print(line, file=sys.stderr)


def _same_path(left: str, right: str) -> bool:
    """Same file, regardless of spelling (relative vs absolute, symlinks)."""
    if not left or not right:
        return False
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except (OSError, RuntimeError):
        return str(left) == str(right)


def watchable(record: dict) -> bool:
    """True while this receipt still needs a poller/waiter to act on it."""
    if record.get("reply_expected") is not True:
        return False
    state = str(record.get("state") or "")
    if state in {STATE_NOT_DELIVERED, STATE_NOTIFIED}:
        return False
    if state in ANSWER_STATES and record.get("notify_attempted_at"):
        # At most one notification attempt, ever: left for `pend`.
        return False
    return True


def watched_receipts(owner: str) -> list[str]:
    """Receipts this exact daemon owns and still has work for."""
    owner = str(owner or "").strip()
    if not owner:
        return []
    try:
        paths = sorted(outbound_dir().glob("*.json"))
    except OSError:
        return []
    request_ids: list[str] = []
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if not _same_path(str(record.get("poll_owner") or ""), owner):
            continue
        if not watchable(record):
            continue
        request_id = str(record.get("request_id") or "")
        try:
            proto.parse_remote_id(request_id)
        except proto.ProtocolError:
            continue
        request_ids.append(request_id)
    return request_ids


def watch_target_ok(request_id: str, owner: str) -> tuple[bool, str]:
    """Whether this daemon may be registered as the poller for a receipt."""
    try:
        record = read_receipt(request_id)
    except RemoteRefusal as exc:
        return False, str(exc)
    if record.get("reply_expected") is not True:
        return False, "not a reply-expected submission"
    registered = str(record.get("poll_owner") or "")
    if not _same_path(registered, str(owner or "")):
        return False, "this receipt is registered to another polling daemon"
    return True, ""


def register_watch(request_id: str, *, timeout_s: float = 5.0) -> tuple[bool, str]:
    """Tell the receipt's polling daemon that this id is ready to be watched."""
    import askd_rpc

    try:
        record = read_receipt(request_id)
    except RemoteRefusal as exc:
        return False, str(exc)
    owner = str(record.get("poll_owner") or "").strip()
    if not owner:
        return False, "this receipt has no polling owner"
    state = askd_rpc.read_state(Path(owner))
    if not isinstance(state, dict) or not state.get("token"):
        return False, "the polling daemon is not running"
    request = {
        "type": "ask.request",
        "v": 1,
        "id": f"watch-{request_id}",
        "token": state["token"],
        "operation": "remote_watch",
        "request_id": request_id,
    }
    try:
        response = askd_rpc.request_daemon(
            state, request, connect_timeout_s=timeout_s, response_timeout_s=timeout_s
        )
    except Exception as exc:
        return False, f"the polling daemon could not be reached ({type(exc).__name__})"
    if not isinstance(response, dict) or not response.get("ok"):
        error = response.get("error") if isinstance(response, dict) else None
        reason = str(error.get("message") or "") if isinstance(error, dict) else ""
        return False, reason or "the polling daemon refused the registration"
    return True, ""


def notify_text(request_id: str, host_alias: str, reply: str) -> str:
    """The labelled block injected into the caller's pane."""
    return (
        "CCB_REMOTE_REPLY: this is the answer to a request your session sent to "
        "another machine by CCB.\n"
        f"CCB_REMOTE_REQUEST_ID: {request_id}\n"
        f"CCB_REMOTE_SOURCE_HOST: {host_alias}\n"
        "\n"
        f"{reply}"
    )


def deliver_to_caller(identity: dict, text: str) -> tuple[bool, str]:
    """Revalidate the captured caller pane and inject the answer there, once.

    The same criteria peer delivery uses: the pane-injection lock, then a fresh
    revalidation of the saved endpoint (alive, cwd, marker), plus the captured
    pane process when there is one. Nothing is ever resolved again, retargeted,
    or created (`ensure_pane` is never called); any mismatch refuses.
    """
    from askd.adapters.base import PeerDestination
    from pane_lock import PaneLockError, pane_injection_lock
    from peer_routing import revalidate_peer_destination
    from terminal import get_backend_for_session

    pane_id = str(identity.get("pane_id") or "").strip()
    terminal = str(identity.get("terminal") or "").strip()
    if not pane_id or not terminal:
        return False, "no caller pane was captured before submit"
    backend = get_backend_for_session({"terminal": terminal})
    if backend is None:
        return False, f"the caller's terminal backend is unavailable: {terminal}"
    destination = PeerDestination(
        pane_id=pane_id,
        terminal=terminal,
        work_dir=str(identity.get("work_dir") or ""),
        ccb_project_id=str(identity.get("ccb_project_id") or ""),
        pane_title_marker=str(identity.get("pane_title_marker") or ""),
    )
    captured_pid = str(identity.get("pane_pid") or "").strip()
    # A backend that can respawn a pane IN PLACE keeps the pane id (and the
    # marker/cwd), so without the captured process there is no evidence that
    # this is still the pane that asked. Refuse: the answer stays in the
    # receipt for `pend`, and nothing is injected anywhere.
    if not captured_pid and callable(getattr(backend, "respawn_pane", None)):
        return False, "the caller pane process was not captured, so it cannot be revalidated"
    try:
        with pane_injection_lock(backend, pane_id, None):
            if captured_pid:
                current = ""
                try:
                    current = str(backend.pane_pid(pane_id) or "").strip()
                except Exception:
                    current = ""
                if current != captured_pid:
                    return False, "the caller pane is now a different process"
            outcome = revalidate_peer_destination(destination)
            if not outcome.ok:
                return False, f"the caller pane is not the captured one ({outcome.error})"
            backend.send_text(pane_id, text)
    except PaneLockError as exc:
        return False, f"the caller pane is busy ({exc})"
    except Exception as exc:
        return False, f"the caller pane could not be written ({type(exc).__name__})"
    return True, ""


def _notify_caller(request_id: str, host_alias: str, *, log: Callable[[str], None] | None = None) -> bool:
    """Notify the captured caller pane at most once; persists before sending."""
    log = log or _spool_log
    try:
        record = read_receipt(request_id)
    except RemoteRefusal as exc:
        log(f"[WARN] remote reply receipt is unreadable for {request_id}: {exc}")
        return False
    identity = record.get("caller_identity")
    identity = identity if isinstance(identity, dict) else {}
    reply = str(record.get("reply_text") or "")
    if not reply:
        return False

    claimed = {"value": False}

    def claim(latest: dict) -> bool:
        if latest.get("notify_attempted_at") or not latest.get("reply_text"):
            # At-most-once: another actor (poller, pend, a previous run) has
            # already attempted the one notification this receipt allows.
            return False
        latest["notify_attempted_at"] = _now()
        claimed["value"] = True
        return True

    try:
        update_receipt(request_id, claim)
    except Exception as exc:
        log(f"[WARN] remote reply notification claim failed for {request_id}: {exc}")
        return False
    if not claimed["value"]:
        return False

    ok, reason = deliver_to_caller(identity, notify_text(request_id, host_alias, reply))

    def settle(latest: dict) -> None:
        if ok:
            latest["notified_at"] = _now()
            apply_receipt_state(
                latest, STATE_NOTIFIED, "answer injected into the caller pane"
            )
        else:
            latest["notify_refused"] = reason
            _append_history(
                latest,
                str(latest.get("state") or STATE_FETCHED),
                f"notification not sent: {reason}",
            )

    try:
        update_receipt(request_id, settle)
    except Exception as exc:
        log(f"[WARN] remote reply notification result not recorded for {request_id}: {exc}")
    if not ok:
        log(f"[INFO] remote reply {request_id} left for pend: {reason}")
    return ok


def poll_receipt(
    request_id: str, *, notify: bool = False, log: Callable[[str], None] | None = None
) -> dict:
    """Fetch once, persist the answer durably, and optionally notify.

    Persistence always precedes notification, and a stored answer is never
    overwritten (a second, different answer is recorded as a conflict and the
    first is kept). Returns the receipt as it now stands.
    """
    record = read_receipt(request_id)
    if record.get("reply_expected") is not True:
        raise RemoteRefusal(f"{request_id} is not a reply-expected submission")
    host_alias = str(record.get("host") or "")

    result = fetch(request_id)

    if result.error_code or not result.state:
        def unavailable(latest: dict) -> None:
            latest["last_fetch_error"] = result.reason
            latest["last_fetch_at"] = _now()
            apply_receipt_state(latest, STATE_FETCH_UNAVAILABLE, result.reason)

        try:
            update_receipt(request_id, unavailable)
        except Exception:
            pass
        (log or _spool_log)(f"[WARN] remote fetch failed for {request_id}: {result.reason}")
        return read_receipt(request_id)

    if not result.answered:
        def pending(latest: dict) -> None:
            latest["remote_state"] = result.state
            latest["last_fetch_at"] = _now()
            if str(latest.get("state") or "") == STATE_FETCH_UNAVAILABLE:
                apply_receipt_state(
                    latest, STATE_ACCEPTED, f"destination state via fetch: {result.state}"
                )

        try:
            update_receipt(request_id, pending)
        except Exception:
            pass
        return read_receipt(request_id)

    def store(latest: dict) -> None:
        latest["remote_state"] = "answered"
        latest["last_fetch_at"] = _now()
        stored_digest = str(latest.get("reply_digest") or "")
        stored_text = str(latest.get("reply_text") or "")
        stored_valid = bool(stored_text) and hmac.compare_digest(
            stored_digest, proto.compute_reply_digest(stored_text)
        )
        if stored_digest and not stored_valid and hmac.compare_digest(stored_digest, result.reply_digest):
            # The saved text no longer matches its own digest, but the
            # destination returned the same, verified answer: repair the text.
            latest["reply_text"] = result.reply_text
            _append_history(
                latest,
                str(latest.get("state") or STATE_FETCHED),
                "stored answer text failed its digest check; restored from the destination",
            )
            return
        if stored_digest:
            if not hmac.compare_digest(stored_digest, result.reply_digest):
                latest["reply_conflict"] = True
                _append_history(
                    latest,
                    str(latest.get("state") or STATE_FETCHED),
                    "a second, different answer arrived; the first is kept",
                )
            return
        latest["reply_text"] = result.reply_text
        latest["reply_digest"] = result.reply_digest
        latest["answered_at"] = _now()
        apply_receipt_state(latest, STATE_FETCHED, "answer fetched from the destination")

    update_receipt(request_id, store)

    if notify:
        _notify_caller(request_id, host_alias, log=log)
    return read_receipt(request_id)


def wait_for_answer(
    request_id: str,
    *,
    timeout_s: float,
    out: Any = None,
    err: Any = None,
) -> int:
    """Poll until answered or the deadline, persist first, then print exactly one reply."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    deadline = _monotonic() + max(0.0, float(timeout_s))
    delay = REPLY_BACKOFF_START_S
    while True:
        try:
            receipt = poll_receipt(request_id, notify=False)
        except (RemoteRefusal, RemoteConfigError) as exc:
            print(f"[ERROR] {exc}", file=err)
            return EXIT_NOT_DELIVERED
        state = str(receipt.get("state") or "")
        text = str(receipt.get("reply_text") or "")
        if text and state in ANSWER_STATES:
            out.write(text if text.endswith("\n") else text + "\n")
            out.flush()
            print(
                f"id={request_id} state={state} host={receipt.get('host') or '-'}",
                file=err,
            )
            return EXIT_ACCEPTED
        remaining = deadline - _monotonic()
        if remaining <= 0:
            print(f"id={request_id} still waiting; use pend {request_id}", file=err)
            return EXIT_WAITING
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, REPLY_BACKOFF_CAP_S)


def remote_pend(request_id: str, *, out: Any = None, err: Any = None) -> int:
    """Show a remote receipt's answer (fetching it if needed), or its state."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    try:
        record = read_receipt(request_id)
    except RemoteRefusal as exc:
        print(f"[ERROR] {exc}", file=err)
        return EXIT_NOT_DELIVERED
    if record.get("reply_expected") is not True:
        print(f"[ERROR] {request_id} is not a reply-expected remote request", file=err)
        return EXIT_NOT_DELIVERED
    stored_text = str(record.get("reply_text") or "")
    stored_digest = str(record.get("reply_digest") or "")
    stored_answer = bool(stored_text) and bool(stored_digest) and hmac.compare_digest(
        stored_digest, proto.compute_reply_digest(stored_text)
    )
    if stored_answer:
        # The answer is already durable here: show it even when the host
        # configuration or the destination is gone. Nothing is re-fetched.
        receipt = record
    else:
        try:
            receipt = poll_receipt(request_id, notify=False)
        except (RemoteRefusal, RemoteConfigError, OSError) as exc:
            print(f"[ERROR] {exc}", file=err)
            return EXIT_NOT_DELIVERED
    state = str(receipt.get("state") or "")
    print(
        f"id={request_id} state={state} host={receipt.get('host') or '-'} "
        f"destination={receipt.get('remote_state') or '-'}",
        file=out,
    )
    text = str(receipt.get("reply_text") or "")
    digest = str(receipt.get("reply_digest") or "")
    if text and state in ANSWER_STATES and not (
        digest and hmac.compare_digest(digest, proto.compute_reply_digest(text))
    ):
        print(
            f"[ERROR] the stored answer for {request_id} failed its integrity check and "
            "could not be restored from the destination; nothing is shown",
            file=err,
        )
        return EXIT_NOT_DELIVERED
    if text and state in ANSWER_STATES:
        print("", file=out)
        out.write(text if text.endswith("\n") else text + "\n")
        out.flush()
        return EXIT_ACCEPTED
    if state == STATE_FETCH_UNAVAILABLE:
        reason = str(receipt.get("last_fetch_error") or "the destination could not be asked")
        print(f"[WARN] answer not fetched yet: {reason}", file=err)
    else:
        print("[WARN] no answer yet; try pend again later", file=err)
    return EXIT_WAITING


def watch_loop(stop_event, wake_event, *, owner: str, log: Callable[[str], None] | None = None) -> None:
    """Poll the receipts this daemon owns, with bounded per-receipt backoff.

    Only receipts whose ``poll_owner`` is this daemon are ever touched, so two
    daemons can never poll (or notify for) the same receipt.
    """
    log = log or _spool_log
    schedule: dict[str, tuple[float, float]] = {}
    while not stop_event.is_set():
        now = _monotonic()
        watched = watched_receipts(owner)
        for request_id in watched:
            next_at, delay = schedule.get(request_id, (now, REPLY_BACKOFF_START_S))
            if next_at > now:
                continue
            try:
                settled = not watchable(poll_receipt(request_id, notify=True, log=log))
            except Exception as exc:
                settled = False
                log(f"[WARN] remote reply poll failed for {request_id}: {exc}")
            if settled:
                schedule.pop(request_id, None)
            else:
                following = min(max(delay, REPLY_BACKOFF_START_S) * 2, REPLY_BACKOFF_CAP_S)
                schedule[request_id] = (_monotonic() + following, following)
        for request_id in list(schedule):
            if request_id not in watched:
                schedule.pop(request_id, None)
        timeout = 5.0 if not schedule else 1.0
        if schedule:
            timeout = min(timeout, max(0.05, min(at for at, _ in schedule.values()) - _monotonic()))
        wake_event.wait(timeout)
        wake_event.clear()
