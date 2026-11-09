"""Source side of cross-machine CCB delivery (Milestone 1: one-way, no reply).

Responsibilities, in order:

1. Source config: ``~/.ccb/remote/hosts.json`` -- host alias -> ssh target,
   isolated key, pinned known-hosts file and the issuer token the destination
   binds to that key's principal.
2. A durable OUTBOUND receipt written BEFORE any SSH process is spawned, in a
   persistent per-user directory (local ask receipts live in the system temp
   dir, ``lib/task_receipts.py:25``, which can be cleared on reboot).
3. Exactly one SSH call, argv-only (never a shell string), with an isolated
   option set: one identity, no agent, no control connection, strict host-key
   checking against a pinned file, forwarding disabled, and no remote command
   (the forced command in ``authorized_keys`` is the only thing that runs).
4. Honest source states: ``not-delivered`` (no submit byte was written, or the
   destination refused before acceptance), ``acceptance-unknown`` (submit bytes
   were written but no valid acknowledgement arrived), ``accepted`` (valid
   ack). Nothing is ever resubmitted automatically.

The protocol itself lives in ``lib/remote_protocol.py`` (T1); the destination
implementations live in ``lib/remote_delivery.py`` (T2). This module never
decides authorization and never resolves a destination -- it can only name a
configured host alias and a destination-side project alias.
"""

from __future__ import annotations

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
    raw = value.strip()
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


class SshSession:
    """One SSH call: framed stdin/stdout, bounded per-operation waits."""

    def __init__(self, argv: list[str], *, timeout_s: float | None = None):
        self.argv = argv
        self.timeout_s = op_timeout_s() if timeout_s is None else timeout_s
        self._frames: queue.Queue = queue.Queue()
        self._stderr_tail = ""
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
        try:
            while True:
                frame = proto.read_frame(self._proc.stdout)
                if frame is None:
                    self._frames.put(("eof", None))
                    return
                self._frames.put(("frame", frame))
        except proto.ProtocolError as exc:
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


def _transition(record: dict, state: str, reason: str = "", **extra: Any) -> None:
    """Record the new state best-effort; never raises, never changes the outcome."""
    record["state"] = state
    _append_history(record, state, reason, **extra)
    try:
        save_receipt(record)
    except Exception as exc:
        print(f"[WARN] outbound receipt not updated ({exc}); state shown is authoritative", file=sys.stderr)


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
    hosts: dict[str, RemoteHost] | None = None,
    session_factory: Callable[[list[str]], SshSession] | None = None,
    out: Any = None,
    err: Any = None,
) -> RemoteResult:
    """Write the receipt, run one SSH call, and report the honest state."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
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
    _append_history(receipt, STATE_SUBMITTING)
    try:
        save_receipt(receipt)
    except Exception as exc:
        print(f"[ERROR] could not write the outbound receipt; nothing was sent: {exc}", file=err)
        return RemoteResult(request_id, host.alias, STATE_NOT_DELIVERED, EXIT_NOT_DELIVERED,
                            reason="outbound receipt could not be written", digest=record["digest"])

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
        except SessionError as exc:
            _transition(receipt, STATE_NOT_DELIVERED, exc.detail, ssh=str(exc.kind))
            print(f"id={request_id} state={STATE_NOT_DELIVERED} ({exc.detail})", file=err)
            return RemoteResult(request_id, host.alias, STATE_NOT_DELIVERED, EXIT_NOT_DELIVERED,
                                reason=exc.detail, error_code=exc.kind, digest=record["digest"])

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
            detail = exc.detail
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
            receipt["remote_state"] = remote_state
            _transition(receipt, STATE_ACCEPTED, "destination accepted the request",
                        remote_state=remote_state)
            print(f"id={request_id} state={STATE_ACCEPTED} destination={remote_state or '-'}", file=out)
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
    if remote_state:
        record["remote_state"] = remote_state
        record.setdefault("remote_history", []).extend(
            {"state": str(entry.get("state") or ""), "at": str(entry.get("at") or "")}
            for entry in history
            if entry.get("state")
        )
        # The receipt mirrors the destination: that is what makes a status
        # query the recovery path for an acceptance-unknown submission.
        _transition(record, remote_state, "destination state via status")
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
