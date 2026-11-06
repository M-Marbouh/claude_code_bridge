"""Launcher identity for registry ownership checks.

A bare `kill(pid, 0)` proves only that the numeric id exists. On Linux a
thread id is a task id and passes that check, and a reused pid belongs to a
different process entirely. Both made a dead launch record look alive, so
`ask` routed into the old launch and failed with `reason=pane_dead` while the
current launch's pane was fine. This module answers a narrower question than
existence: is the process named by the record still the SAME process that
wrote the record?

It is used by `pane_registry._registry_owner_alive` only; the lock contract of
`process_lock._is_pid_alive` stays exactly as it is.

Evidence, strongest first:

1. Raw process identity on the record: `ccb_pid_start_ticks` (field 22 of
   `/proc/<pid>/stat`, the process start offset in clock ticks since boot) and
   `ccb_boot_id` (`/proc/sys/kernel/random/boot_id`), written by
   `upsert_registry` when the writer IS the owner. Both are invariant for the
   life of the process and are NOT derived from the wall clock, so an NTP
   step, a `settimeofday`, or a resume from suspend cannot change them. Same
   boot and the same ticks is the same process.
2. Otherwise (legacy records, e.g. written by `upsert_registry` from a member
   process, which derives the pid from the `ai-<epoch>-<pid>` launch id): the
   launch id's epoch is the record owner's creation evidence. The launcher
   generates that id inside `AILauncher.__init__`, so the owning process
   started at or shortly before it. This comparison necessarily goes through
   `btime`, which DOES shift when the wall clock is stepped: see
   `LEGACY_WINDOW_S`.

Epoch-based evidence (`ccb_pid_started_at`) was stored by an earlier revision
of this module and is deliberately no longer written or read: a value derived
from `btime` changes after a clock step, which turned a genuine launcher into
a false death. Such a record simply falls back to the legacy window above; no
reader needs the old field, so it is dropped rather than kept for show.

Return contract (see `launcher_alive`):

- `True` -- identified as the same live process.
- `False` -- not the same live process (gone, a thread id, another process,
  or an incarnation that cannot be the one that wrote the record).
- `None` -- the record carries no owner identity at all, or a live process
  matched the pid but no creation evidence can be compared (including an
  unreadable start time for a process whose `/proc` entry clearly exists).
  Callers keep their pre-existing behaviour for `None`; declaring a live
  process dead on missing evidence is what strands genuine launches.

On Linux a `/proc/<pid>` entry that cannot be read at all is reported `False`,
not `None`: any registry record this checker sees was written by a process of
the user reading it (the registry lives in that user's `~/.ccb/run`), and a
launcher's own `/proc` entry is always readable by that user. Treating an
unreadable id as alive is the bug being fixed, and returning `None` there
would keep the stale record eligible for routing. A process whose status IS
readable but whose start time is not is a different case: it demonstrably
exists, so its ownership is merely unproven and it is never declared dead.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

from process_lock import _is_pid_alive

# Legacy records have no stored raw identity, so the only evidence is the
# launch id `ai-<epoch>-<pid>`, generated in `AILauncher.__init__` (ccb) about
# 1.0 s after its process start (measured: interpreter start plus ccb module
# import). The comparison has to convert the recorded pid's tick offset through
# `btime`, and `btime` is derived from wall-clock minus boot time: it SHIFTS by
# any clock step since boot (NTP step, settimeofday, typical after resume). A
# mere 30 s step used to push a genuine launcher out of a 2 s window and kill
# the record, so the window is symmetric and wide enough to absorb a clock
# step. This is a heuristic, not identity: it carries a residual pid-reuse risk
# on systems with a small `pid_max` and a high process-creation rate, because a
# different process with the same pid started near the launch id's epoch would
# pass. It applies only to pre-deploy records that lack stored raw identity, so
# the exposure shrinks as those records age out.
LEGACY_WINDOW_S = 60.0
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def _procfs_available() -> bool:
    """Linux is the only platform with the `/proc` evidence this check needs."""
    return sys.platform.startswith("linux") and os.path.isdir("/proc")


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except FileNotFoundError:
        return None
    except OSError:
        return ""


def _boot_epoch() -> Optional[float]:
    text = _read_text("/proc/stat")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _task_tgid(pid: int) -> Optional[int]:
    """The thread group id of `pid`: equal to `pid` for a process, different
    for a thread id. `None` when the id names no task at all."""
    text = _read_text(f"/proc/{pid}/status")
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("Tgid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return -1
    return -1


def boot_id() -> Optional[str]:
    """The running kernel's boot UUID, or `None` when it cannot be read.

    This names the boot the recorded process belongs to, which is what makes a
    raw tick offset safe to compare: ticks repeat every boot.
    """
    text = _read_text(BOOT_ID_PATH)
    if not text:
        return None
    value = text.strip()
    return value or None


def _stat_start_ticks(pid: int) -> Optional[int]:
    """Field 22 (`starttime`) of `/proc/<pid>/stat`, in clock ticks since boot.

    The `comm` field may contain spaces and parentheses, so parsing starts
    after the last `)`.
    """
    text = _read_text(f"/proc/{pid}/stat")
    if not text or ")" not in text:
        return None
    try:
        return int(text[text.rindex(")") + 2:].split()[19])
    except (IndexError, ValueError):
        return None


def process_start_ticks(pid: int) -> Optional[int]:
    """The raw, wall-clock-independent start offset of `pid`, or `None`."""
    return _stat_start_ticks(pid)


def process_identity(pid: int) -> Dict[str, Any]:
    """The record fields that identify `pid`'s process incarnation.

    `ccb_pid_start_ticks` says where in this boot the process started;
    `ccb_boot_id` says which boot that was. Written by `upsert_registry` only
    when the writing process IS the recorded owner.

    Always a dict. `{}` when either half is unavailable, and then the caller
    must store NOTHING: a tick offset without its boot id does not identify a
    process (ticks repeat every boot), so a partial identity is worse than no
    identity. A record written that way stays legacy-shaped and is checked
    against the launch-id window instead.
    """
    ticks = process_start_ticks(pid)
    current_boot = boot_id()
    if ticks is None or not current_boot:
        return {}
    return {"ccb_pid_start_ticks": ticks, "ccb_boot_id": current_boot}


def process_start_epoch(pid: int) -> Optional[float]:
    """Linux: `btime + starttime / CLK_TCK` for `pid`, in epoch seconds.

    Only for the legacy launch-id comparison and for tests that need to model
    a wall-clock step. It is NOT identity evidence: `btime` moves when the
    wall clock is stepped, which is exactly why new records store raw ticks
    and the boot id instead. Returns `None` when the process is gone or the
    evidence is unreadable.
    """
    starttime = _stat_start_ticks(pid)
    if starttime is None:
        return None
    boot = _boot_epoch()
    if boot is None:
        return None
    try:
        return boot + starttime / float(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError):
        return None


def _clean_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _launch_id_epoch(record: Dict[str, Any]) -> Optional[float]:
    """The `<epoch>` of the record's `ai-<epoch>-<pid>` launch id, if any."""
    for key in ("ccb_session_id", "session_id"):
        raw = str(record.get(key) or "").strip()
        parts = raw.split("-")
        if len(parts) < 3 or parts[0] != "ai":
            continue
        try:
            return float(int(parts[1]))
        except ValueError:
            continue
    return None


def launcher_alive(record: Dict[str, Any], pid: int) -> Optional[bool]:
    """Is `pid` still the process that wrote `record`? See the module docstring."""
    if not _procfs_available():
        # Non-Linux (macOS, Windows): no creation-time evidence is available,
        # so keep today's bare-pid answer rather than inventing a weaker
        # heuristic. A reused pid can still fool this platform; a thread id
        # cannot be a launcher pid there.
        return bool(_is_pid_alive(pid))

    tgid = _task_tgid(pid)
    if tgid is None:
        return False  # no such task: the launcher is gone
    if tgid != pid:
        # A thread id passes kill(pid, 0); it cannot be the launcher process.
        return False
    if tgid < 0:
        # /proc/<pid>/status exists but carries no readable Tgid. Own-record
        # entries are readable; an unreadable one is not the launcher.
        return False

    ticks = process_start_ticks(pid)
    if ticks is None:
        # The task exists (its status was readable) but its start offset is
        # not. Ownership is unproven, not disproven: never declare it dead.
        return None

    stored_ticks = _clean_int(record.get("ccb_pid_start_ticks"))
    stored_boot = str(record.get("ccb_boot_id") or "").strip()
    if stored_ticks is not None and stored_boot:
        current_boot = boot_id()
        if current_boot is None:
            # Stored evidence names a boot, the current one cannot be read:
            # the comparison is unproven rather than failed.
            return None
        if current_boot != stored_boot:
            # A tick offset is only meaningful within its own boot, and a
            # process from another boot cannot still be running.
            return False
        return stored_ticks == ticks

    # Ticks WITHOUT their boot id prove nothing: the same 10 ms offset recurs
    # every boot, so this is not raw identity. Such a record (only a partial
    # writer could produce one) is treated exactly like one with no stored
    # identity at all: the launch-id window decides, never ticks alone.
    start = process_start_epoch(pid)
    if start is None:
        return None
    id_epoch = _launch_id_epoch(record)
    if id_epoch is None:
        # A live process matches the pid, but there is no creation evidence to
        # compare it with. Existing, not proven dead: report unknown.
        return None
    return (id_epoch - LEGACY_WINDOW_S) <= start <= (id_epoch + LEGACY_WINDOW_S)
