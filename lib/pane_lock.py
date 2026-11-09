"""Cross-process pane-injection lock.

Why this exists: local peer delivery and (from T2) remote delivery can target
the SAME physical pane from different askd daemons. Each daemon only
serialises its own workers per session key, so two sends can interleave text
and Enter -- one prompt swallowed, another submitted twice. The lock here is
keyed on the physical pane (terminal server identity + resolved pane id), not
on a CCB project or worker key, so every sender on the machine contends for
the same file.

Scope (ratified plan, docs/plans/cross-machine-m1-plan.md section 10 A1): the
lock is held only around text injection -- paste, the Enter delay, Enter and
buffer cleanup -- never around a provider turn. A remote delivery must not
queue behind a routed Codex turn of up to an hour.

Properties:

- Key = terminal backend kind + canonical terminal-server identity + tmux's own
  canonical pane id, taken from ONE query through the backend's own invocation
  (`display-message -p -t <target> '#{socket_path} #{pane_id}'`). That makes
  `-L`/`CCB_TMUX_SOCKET`/`$TMUX_TMPDIR` canonical without trusting the caller's
  environment, and makes a session-name target and `%0` for the same pane
  collapse onto one key. If that query fails the caller gets
  :class:`PaneLockError` (after a short retry) and sends nothing -- there is no
  fallback key, because a guessed key would serialise against nothing.
- Lock files live in a private directory under the CCB per-user home
  (`~/.ccb/locks/pane/`), mode 0700/0600, taken with `fcntl.flock`.
- Files are never unlinked on release: unlinking would let two processes lock
  two different inodes for the same key.
- Re-entrant per thread: acquiring the same key again on the same thread is a
  no-op, so an outer lock around "revalidate, then send" can wrap the
  backend's own `send_text` lock without deadlocking.
- Timeout raises :class:`PaneLockTimeout`; the caller must then send nothing.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

#: Generous relative to a single injection (paste + 0.5 s Enter delay), short
#: enough that a wedged sender cannot hold a queue forever.
DEFAULT_TIMEOUT_S = 60.0

DISABLE_ENV = "CCB_PANE_LOCK_DISABLE"
TIMEOUT_ENV = "CCB_PANE_LOCK_TIMEOUT_S"

_POLL_INTERVAL_S = 0.02

# Bound at import time: adapters' tests monkeypatch `time.monotonic`/`time.sleep`
# through the shared `time` module, and that must not reach the lock's deadline.
# perf_counter, not monotonic: adapter tests simulate `time.monotonic`
# (monkeypatched through the shared module) and a lock deadline must still
# be real wall time.
_monotonic = time.perf_counter
_sleep = time.sleep


class PaneLockError(RuntimeError):
    """Base class: the pane injection lock could not be taken or released."""


class PaneLockTimeout(PaneLockError):
    """Another sender held the pane's injection lock for the whole budget.

    Nothing has been sent; this is proven non-delivery for the caller.
    """


_STATE = threading.local()


def _held_keys() -> set[str]:
    held = getattr(_STATE, "held", None)
    if held is None:
        held = set()
        _STATE.held = held
    return held


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def lock_disabled() -> bool:
    """Test-only switch used to prove the cross-process test detects overlap."""
    return _truthy(os.environ.get(DISABLE_ENV))


def default_timeout_s() -> float:
    raw = (os.environ.get(TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def lock_root() -> Path:
    return Path.home() / ".ccb" / "locks" / "pane"


def _ensure_lock_dir() -> Path:
    root = lock_root()
    os.makedirs(root, mode=0o700, exist_ok=True)
    for path in (root.parent, root):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return root


def lock_path_for_key(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return lock_root() / f"{digest}.lock"


# --------------------------------------------------------------------------- #
# Terminal-server identity
# --------------------------------------------------------------------------- #


def _realpath(value: str) -> str:
    try:
        return os.path.realpath(value)
    except OSError:
        return value


#: Attempts for the one identity query, and the pause between them.
_TMUX_IDENTITY_ATTEMPTS = 4
_TMUX_IDENTITY_RETRY_S = 0.1


def _tmux_identity(backend, pane_id: str) -> str:
    """Canonical `<socket_path>|<pane_id>` for the server holding this pane.

    `TmuxBackend._tmux_run` already applies `-L <name>` (from
    `CCB_TMUX_SOCKET`), so `#{socket_path}` is the canonical server socket for
    THIS backend regardless of the process environment. The SAME query also
    returns tmux's canonical `#{pane_id}`, which is what makes a session-name
    target and `%0` for one pane collapse onto one key -- the caller's target
    string must never be the key.

    There is deliberately NO fallback key: if tmux cannot answer, the caller
    gets :class:`PaneLockError` after a short retry and sends nothing. A
    guessed key would serialise against nothing at all.
    """
    runner = getattr(backend, "_tmux_run", None)
    if not callable(runner):
        raise PaneLockError("tmux backend cannot be queried for a pane identity; refusing to guess a lock key")
    target = str(pane_id or "").strip()
    last_detail = "no answer"
    for attempt in range(_TMUX_IDENTITY_ATTEMPTS):
        try:
            completed = runner(
                ["display-message", "-p", "-t", target, "#{socket_path} #{pane_id}"],
                capture=True,
                timeout=2.0,
            )
            if int(getattr(completed, "returncode", 1) or 0) == 0:
                fields = str(getattr(completed, "stdout", "") or "").strip().split()
                if len(fields) == 2 and fields[0]:
                    return f"{_realpath(fields[0])}|{fields[1]}"
                last_detail = "incomplete identity answer"
            else:
                last_detail = f"tmux exit {int(getattr(completed, 'returncode', 1) or 1)}"
        except Exception as exc:
            last_detail = str(exc)
        if attempt + 1 < _TMUX_IDENTITY_ATTEMPTS:
            _sleep(_TMUX_IDENTITY_RETRY_S)
    raise PaneLockError(f"could not resolve the tmux pane identity for {target!r}: {last_detail}")


def wezterm_instance_identity() -> str:
    """STABLE WezTerm instance identity -- never a transient socket path.

    `wezterm cli` exposes no command that reports the mux socket it used, and
    both `WEZTERM_UNIX_SOCKET` and the `gui-sock-*` set in the runtime
    directory change when a second instance starts. Keying on either would
    SPLIT the lock for one pane the moment another window appeared, so the key
    uses only the knobs `WeztermBackend._cli_base_args` actually passes to pick
    an instance: the user runtime directory, `--class` and `--prefer-mux`.
    Over-serialising two same-class instances is acceptable; splitting is not.
    """
    getuid = getattr(os, "getuid", None)
    default_runtime = f"/run/user/{getuid()}" if callable(getuid) else ""
    runtime = (os.environ.get("XDG_RUNTIME_DIR") or "").strip() or default_runtime
    wezterm_class = (os.environ.get("CODEX_WEZTERM_CLASS") or os.environ.get("WEZTERM_CLASS") or "").strip()
    prefer_mux = _truthy(os.environ.get("CODEX_WEZTERM_PREFER_MUX"))
    return f"wezterm:runtime:{runtime or '-'}:class:{wezterm_class or '-'}:mux:{int(prefer_mux)}"


def _wezterm_identity(backend, pane_id: str) -> str:
    return wezterm_instance_identity()


def terminal_identity(backend, pane_id: str) -> str:
    """Stable identity of the terminal server holding `pane_id` on this host."""
    if callable(getattr(backend, "_tmux_run", None)) or callable(getattr(backend, "_tmux_base", None)):
        return f"tmux:{_tmux_identity(backend, pane_id)}"
    if callable(getattr(backend, "_cli_base_args", None)):
        return _wezterm_identity(backend, pane_id)
    name = type(backend).__name__.lower()
    if "tmux" in name:
        return f"tmux:{_tmux_identity(backend, pane_id)}"
    if "wezterm" in name:
        return _wezterm_identity(backend, pane_id)
    qualname = f"{type(backend).__module__}.{type(backend).__qualname__}"
    return f"backend:{qualname}"


def lock_key(backend, pane_id: str) -> str:
    """The physical pane's key: server identity + tmux's own canonical pane id.

    For tmux the canonical pane id comes from the identity query, so a session
    name and `%0` naming the same pane produce the SAME key. Raises
    :class:`PaneLockError` when the identity cannot be resolved.
    """
    pane = str(pane_id or "").strip()
    identity = terminal_identity(backend, pane)
    if identity.startswith("tmux:"):
        return identity  # already `<socket_path>|<canonical pane id>`
    return f"{identity};pane:{pane}"


# --------------------------------------------------------------------------- #
# Acquisition
# --------------------------------------------------------------------------- #


def _acquire(path: Path, timeout_s: float) -> int | None:
    """Take an exclusive flock, waiting up to `timeout_s`.

    Returns an open fd (or None where the platform has no `fcntl`). Raises
    :class:`PaneLockTimeout` when another holder keeps it for the whole
    budget, and :class:`PaneLockError` when the lock file itself is unusable.
    """
    try:
        import fcntl
    except Exception:  # pragma: no cover - non-POSIX platforms
        return None

    _ensure_lock_dir()
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise PaneLockError(f"pane lock file is unusable: {exc}") from exc
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass

    deadline = _monotonic() + max(0.0, float(timeout_s))
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if _monotonic() >= deadline:
                os.close(fd)
                raise PaneLockTimeout(
                    f"pane injection lock is held by another sender "
                    f"(waited {float(timeout_s):.1f}s); nothing was sent"
                )
            _sleep(_POLL_INTERVAL_S)
        except OSError as exc:
            os.close(fd)
            raise PaneLockError(f"pane lock could not be taken: {exc}") from exc


def _release(fd: int | None) -> None:
    if fd is None:
        return
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


@contextmanager
def pane_injection_lock(backend, pane_id: str, timeout_s: float | None = None):
    """Serialise text injection into one physical pane across processes."""
    if lock_disabled():
        yield
        return

    pane = str(pane_id or "").strip()
    if not pane:
        # Nothing to key on; the backend's own send will refuse this pane.
        yield
        return

    key = lock_key(backend, pane)
    held = _held_keys()
    if key in held:
        # Re-entrant on this thread: an outer lock (peer/remote dispatch) owns it.
        yield
        return

    budget = default_timeout_s() if timeout_s is None else float(timeout_s)
    fd = _acquire(lock_path_for_key(key), budget)
    held.add(key)
    try:
        yield
    finally:
        held.discard(key)
        _release(fd)
