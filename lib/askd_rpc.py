from __future__ import annotations

import json
import os
import secrets
import socket
import time
from pathlib import Path


class DaemonConnectionError(OSError):
    """Raised when none of the daemon's advertised transports are reachable."""


class DaemonResponseError(RuntimeError):
    """Raised after connecting when a daemon response is missing or malformed."""


def read_state(state_file: Path) -> dict | None:
    try:
        raw = state_file.read_text(encoding="utf-8")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def connect_daemon(state: dict, timeout_s: float) -> socket.socket:
    """Connect to a daemon's backward-compatible TCP endpoint."""
    try:
        host = state.get("connect_host") or state["host"]
        port = int(state["port"])
    except Exception as exc:
        raise DaemonConnectionError("no valid TCP endpoint in daemon state") from exc

    try:
        return socket.create_connection((host, port), timeout=timeout_s)
    except OSError as exc:
        raise DaemonConnectionError(f"tcp {host}:{port}: {exc}") from exc


def _network_disabled() -> bool:
    raw = (os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass


def _request_via_mailbox(state: dict, request: dict, response_timeout_s: float | None) -> dict:
    root_raw = str(state.get("mailbox_dir") or "").strip()
    if not root_raw:
        raise DaemonConnectionError("daemon state does not advertise a filesystem mailbox")
    root = Path(root_raw)
    requests = root / "requests"
    responses = root / "responses"
    if not requests.is_dir() or not responses.is_dir():
        raise DaemonConnectionError(f"daemon mailbox is unavailable: {root}")

    nonce = f"{os.getpid()}-{time.time_ns()}-{secrets.token_hex(8)}"
    request_path = requests / f"{nonce}.json"
    response_path = responses / f"{nonce}.json"
    deadline = None if response_timeout_s is None else time.monotonic() + max(0.0, response_timeout_s)
    try:
        _write_json_atomic(request_path, request)
        while True:
            if response_path.is_file():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise DaemonResponseError(f"invalid mailbox response: {exc}") from exc
                if not isinstance(response, dict):
                    raise DaemonResponseError("invalid mailbox response: expected an object")
                return response
            if deadline is not None and time.monotonic() >= deadline:
                raise DaemonResponseError(f"daemon mailbox response timed out: {root}")
            time.sleep(0.02)
    finally:
        for path in (request_path, response_path):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def request_daemon(
    state: dict,
    request: dict,
    *,
    connect_timeout_s: float,
    response_timeout_s: float | None,
) -> dict:
    """Send via TCP, using the filesystem mailbox when TCP is unavailable."""
    mailbox = str(state.get("mailbox_dir") or "").strip()
    if mailbox and _network_disabled():
        return _request_via_mailbox(state, request, response_timeout_s)
    try:
        sock = connect_daemon(state, connect_timeout_s)
    except DaemonConnectionError:
        if mailbox:
            return _request_via_mailbox(state, request, response_timeout_s)
        raise
    with sock:
        sock.settimeout(response_timeout_s)
        sock.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    if b"\n" not in buf:
        raise DaemonResponseError("daemon returned no complete response")
    line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace")
    try:
        response = json.loads(line)
    except Exception as exc:
        raise DaemonResponseError(f"invalid daemon response: {line}") from exc
    if not isinstance(response, dict):
        raise DaemonResponseError("invalid daemon response: expected an object")
    return response


def ping_daemon(protocol_prefix: str, timeout_s: float, state_file: Path) -> bool:
    st = read_state(state_file)
    if not st:
        return False
    try:
        token = st["token"]
    except Exception:
        return False
    try:
        req = {"type": f"{protocol_prefix}.ping", "v": 1, "id": "ping", "token": token}
        resp = request_daemon(
            st,
            req,
            connect_timeout_s=timeout_s,
            response_timeout_s=timeout_s,
        )
        return resp.get("type") in (f"{protocol_prefix}.pong", f"{protocol_prefix}.response") and int(resp.get("exit_code") or 0) == 0
    except Exception:
        return False


def shutdown_daemon(protocol_prefix: str, timeout_s: float, state_file: Path) -> bool:
    st = read_state(state_file)
    if not st:
        return False
    try:
        token = st["token"]
    except Exception:
        return False
    try:
        req = {"type": f"{protocol_prefix}.shutdown", "v": 1, "id": "shutdown", "token": token}
        resp = request_daemon(
            st,
            req,
            connect_timeout_s=timeout_s,
            response_timeout_s=timeout_s,
        )
        return resp.get("type") == f"{protocol_prefix}.response" and int(resp.get("exit_code", 1)) == 0
    except Exception:
        return False
