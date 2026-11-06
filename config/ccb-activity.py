#!/usr/bin/env python3
"""Live CCB request activity for the tmux status bar.

    ccb-activity.py <pane_id> <pane_current_path>

Prints one short segment such as `X 1▶2` -- provider letter, running requests,
queued requests -- and nothing at all when no request is active or on ANY
error. It counts CCB requests only: work typed directly into a provider pane
never enters the askd queues and is not counted here.

Scope, which is the whole difficulty: the askd `queue_status` RPC has no
project filter, so a daemon shared by several projects returns every project's
queues. This helper therefore derives the focused pane's project itself and
keeps only queues that belong to it:

- routed keys (`<provider>:launch:<launch_id>:live:<live_id>`) match a launch
  id recorded for that project, so a Codex pair's two workers both count;
- per-session keys (`<provider>:<ccb_project_id>`) must carry that project id;
- `peer:` keys and `<provider>:unknown` are excluded: they name no project.
  Peer (cross-project) requests are therefore NOT counted, so not every `ask`
  appears here: a peer message is queued against a destination pane, not a
  project.

The project comes from the pane's own path mapped onto a recorded launch's
`work_dir`. An inherited `CCB_RUN_DIR`, `$PWD` or a hash of an arbitrary
subdirectory is never trusted; a directory only counts as a project on its
own if it directly contains `.ccb/` (or the legacy `.ccb_config/`).

Bounds: a hard deadline (default 1.0 s, `CCB_ACTIVITY_DEADLINE_S`) covers
discovery, RPC and rendering; a timeout prints nothing. Results are cached per
project and daemon identity (default 2 s, `CCB_ACTIVITY_CACHE_S`), including
empty results, and a failed read overwrites the cache with an empty result so
stale "running" counts never linger.

It must run from the installed layout too, so it locates `lib/` instead of
assuming the checkout.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

DEFAULT_DEADLINE_S = 1.0
DEFAULT_CACHE_TTL_S = 2.0

PROVIDER_ORDER = ("claude", "codex", "gemini", "opencode", "hermes", "pi")
PROVIDER_LABEL = {
    "claude": "C",
    "codex": "X",
    "gemini": "G",
    "opencode": "O",
    "hermes": "H",
    "pi": "P",
}


def _lib_candidates() -> Iterable[Path]:
    here = Path(__file__).resolve()
    env_dir = (os.environ.get("CCB_LIB_DIR") or "").strip()
    if env_dir:
        yield Path(env_dir)
    # Source checkout: <repo>/config/ccb-activity.py -> <repo>/lib
    yield here.parent.parent / "lib"
    # Shipped beside lib (install prefix or a copied tree).
    yield here.parent / "lib"
    # Installed runtime: helper in ~/.local/bin, package in ~/.local/share/codex-dual
    yield Path.home() / ".local" / "share" / "codex-dual" / "lib"
    prefix = (os.environ.get("CODEX_INSTALL_PREFIX") or "").strip()
    if prefix:
        yield Path(prefix) / "lib"


def _ensure_lib_on_path() -> None:
    for candidate in _lib_candidates():
        try:
            if (candidate / "ccb_runtime_status.py").is_file():
                sys.path.insert(0, str(candidate))
                return
        except OSError:
            continue


class _Deadline(BaseException):
    """Raised by the deadline alarm; never escapes main().

    Deliberately NOT an `Exception`: several helpers here (and in `askd_rpc` /
    `askd_runtime`) catch `Exception` to turn an error into a safe fallback,
    and a deadline they swallowed would let a slow discovery or RPC keep going
    past the budget -- and even print counts after expiry. Only `main` handles
    it.
    """


def _deadline_seconds() -> float:
    raw = (os.environ.get("CCB_ACTIVITY_DEADLINE_S") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_DEADLINE_S
    except ValueError:
        value = DEFAULT_DEADLINE_S
    return min(5.0, max(0.1, value))


def _cache_ttl_seconds() -> float:
    raw = (os.environ.get("CCB_ACTIVITY_CACHE_S") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_CACHE_TTL_S
    except ValueError:
        value = DEFAULT_CACHE_TTL_S
    return min(60.0, max(0.0, value))


def _arm_deadline(seconds: float) -> None:
    if not hasattr(signal, "SIGALRM"):
        return

    def _expire(_signum, _frame):
        raise _Deadline()

    try:
        signal.signal(signal.SIGALRM, _expire)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    except (OSError, ValueError):
        pass


def _disarm_deadline() -> None:
    if not hasattr(signal, "SIGALRM"):
        return
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
    except (OSError, ValueError):
        pass


def _registry_dir() -> Path:
    return Path.home() / ".ccb" / "run"


def _load_launch_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        paths = sorted(_registry_dir().glob("ccb-session-*.json"))
    except OSError:
        return records
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            records.append(data)
    return records


def _normalized(path: Any) -> Optional[Path]:
    try:
        return Path(str(path)).expanduser().resolve()
    except (OSError, ValueError):
        return None


def _resolve_project(pane_path: str) -> tuple[str, Optional[Path]]:
    """The focus pane's CCB project as `(project_id, project_root)`.

    The pane's path is mapped onto a recorded launch's `work_dir` (longest
    match wins). Nothing here reads `CCB_RUN_DIR`, `$PWD` or hashes a path
    that merely happens to sit somewhere under a project.
    """
    target = _normalized(pane_path) if pane_path else None
    if target is None or not target.is_dir():
        # Without a usable pane path the project is not established. Searching
        # the registry anyway would pick an arbitrary project and show its
        # activity for this pane, so stop before any RPC is attempted.
        return "", None
    best: Optional[tuple[int, str, Path]] = None
    for record in _load_launch_records():
        work_dir = str(record.get("work_dir") or "").strip()
        project_id = str(record.get("ccb_project_id") or "").strip()
        if not work_dir or not project_id:
            continue
        root = _normalized(work_dir)
        if root is None:
            continue
        if target is not None:
            try:
                target.relative_to(root)
            except ValueError:
                continue
        candidate = (len(str(root)), project_id, root)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is not None:
        return best[1], best[2]
    if target is not None and ((target / ".ccb").is_dir() or (target / ".ccb_config").is_dir()):
        try:
            from project_id import compute_ccb_project_id

            return compute_ccb_project_id(target), target
        except Exception:
            return "", None
    return "", None


def _launch_ids_for(project_id: str) -> set[str]:
    ids: set[str] = set()
    if not project_id:
        return ids
    for record in _load_launch_records():
        if str(record.get("ccb_project_id") or "").strip() != project_id:
            continue
        launch_id = str(record.get("ccb_session_id") or "").strip()
        if launch_id:
            ids.add(launch_id)
    return ids


def _queue_belongs(session_key: str, project_id: str, launch_ids: set[str]) -> bool:
    parts = session_key.split(":")
    if not parts or not parts[0] or parts[0] == "peer":
        return False
    if len(parts) >= 3 and parts[1] == "launch":
        return parts[2] in launch_ids
    if len(parts) == 2:
        return parts[1] == project_id and parts[1] != "unknown"
    return False


def _summarize(queues: Iterable[Any], project_id: str, launch_ids: set[str]) -> str:
    buckets: dict[str, list[int]] = {}
    for entry in queues:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("session_key") or "").strip()
        if not _queue_belongs(key, project_id, launch_ids):
            continue
        provider = str(entry.get("provider") or key.split(":", 1)[0]).strip().lower()
        if not provider:
            continue
        running = 1 if entry.get("in_flight") else 0
        try:
            waiting = int(entry.get("waiting") or 0)
        except (TypeError, ValueError):
            waiting = 0
        if running + waiting <= 0:
            continue
        bucket = buckets.setdefault(provider, [0, 0])
        bucket[0] += running
        bucket[1] += waiting
    ordered = list(PROVIDER_ORDER) + sorted(p for p in buckets if p not in PROVIDER_ORDER)
    parts: list[str] = []
    for provider in ordered:
        bucket = buckets.get(provider)
        if not bucket:
            continue
        label = PROVIDER_LABEL.get(provider) or provider[:1].upper()
        parts.append(f"{label} {bucket[0]}▶{bucket[1]}")
    return "  ".join(parts)


def _cache_dir() -> Optional[Path]:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    uid = os.getuid() if hasattr(os, "getuid") else 0
    path = Path(base) / f"ccb-activity-{uid}"
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        return None
    return path


def _select_daemon(project_root: Path) -> tuple[Optional[Path], Optional[dict]]:
    """The daemon this status line will talk to, chosen exactly once.

    Reachability decides it (`find_running_state_file` pings the same
    candidates `daemon_queue_status` used to), and the SAME `(state_file,
    state)` pair is then used for the cache identity and for the query. Keying
    the cache by the first *existing* file while querying the first *reachable*
    one mixed a dead daemon's entry with a live daemon's counts.
    """
    from askd_runtime import find_running_state_file
    import askd_rpc

    state_file = find_running_state_file(
        "askd.json", protocol_prefix="ask", work_dir=project_root, timeout_s=0.5
    )
    if state_file is None:
        return None, None
    state = askd_rpc.read_state(state_file) or {}
    if not state or not str(state.get("token") or ""):
        return None, None
    return state_file, state


def _identity(state_file: Optional[Path], state: Optional[dict]) -> str:
    if state_file is None or not state:
        return "none"
    return f"{state_file}|{state.get('pid')}|{state.get('port')}"


def _queue_status(state: dict) -> list[dict]:
    """Ask the selected daemon for its queues.

    Mirrors `ccb_runtime_status.daemon_queue_status`'s `queue_status` request,
    but takes the already-selected state so the query cannot land on a
    different daemon than the one the cache key names. No provider/session
    filter is sent: the project filtering happens here, over the full list.
    """
    import askd_rpc

    request = {
        "type": "ask.request",
        "v": 1,
        "id": f"activity-{os.getpid()}",
        "token": str(state.get("token")),
        "operation": "queue_status",
        "provider": "",
        "session_key": "",
    }
    timeout_s = min(0.6, _deadline_seconds())
    response = askd_rpc.request_daemon(
        state,
        request,
        connect_timeout_s=min(2.0, timeout_s),
        response_timeout_s=timeout_s,
    )
    if response.get("type") != "ask.response" or int(response.get("exit_code", 1)) != 0:
        raise RuntimeError("askd rejected queue status")
    queues = response.get("queues")
    if not isinstance(queues, list):
        raise RuntimeError("askd does not report queue status")
    return [q for q in queues if isinstance(q, dict)]


def _cache_path(project_id: str, daemon_id: str) -> Optional[Path]:
    directory = _cache_dir()
    if directory is None:
        return None
    digest = hashlib.sha256(f"{project_id}\0{daemon_id}".encode("utf-8")).hexdigest()[:32]
    return directory / f"{digest}.cache"


def _cache_read(path: Optional[Path], ttl: float) -> Optional[str]:
    if path is None or ttl <= 0:
        return None
    try:
        lines = path.read_text(encoding="utf-8").split("\n", 1)
        timestamp = int(lines[0])
        value = lines[1].rstrip("\n") if len(lines) > 1 else ""
    except (OSError, ValueError):
        return None
    if time.time() - timestamp >= ttl:
        return None
    return value


def _cache_write(path: Optional[Path], value: str) -> None:
    if path is None:
        return
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{int(time.time())}\n{value}\n")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def _activity_for(pane_path: str) -> str:
    project_id, project_root = _resolve_project(pane_path)
    if not project_id or project_root is None:
        # Not established: no RPC, no cache, no activity for this pane.
        return ""
    launch_ids = _launch_ids_for(project_id)
    state_file, state = _select_daemon(project_root)
    cache_path = _cache_path(project_id, _identity(state_file, state))
    cached = _cache_read(cache_path, _cache_ttl_seconds())
    if cached is not None:
        return cached
    try:
        queues = _queue_status(state) if state is not None else []
    except _Deadline:
        # Never turn a timeout into output, and never write it to the cache.
        raise
    except Exception:
        # No daemon, an unreachable or an old daemon: report nothing, and
        # overwrite any cached counts so stale "running" numbers cannot linger.
        _cache_write(cache_path, "")
        return ""
    value = _summarize(queues, project_id, launch_ids)
    _cache_write(cache_path, value)
    return value


def main(argv: list[str]) -> int:
    pane_path = argv[1] if len(argv) > 1 else ""
    # The helper must not inherit a launcher's run-dir override: the daemon and
    # the project both come from the pane's own location. Restore it afterwards
    # so an in-process caller (tests) is unaffected.
    inherited_run_dir = os.environ.pop("CCB_RUN_DIR", None)
    _arm_deadline(_deadline_seconds())
    try:
        _ensure_lib_on_path()
        value = _activity_for(pane_path)
    except _Deadline:
        value = ""
    except Exception:
        value = ""
    finally:
        _disarm_deadline()
        if inherited_run_dir is not None:
            os.environ["CCB_RUN_DIR"] = inherited_run_dir
    if value:
        print(value)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
