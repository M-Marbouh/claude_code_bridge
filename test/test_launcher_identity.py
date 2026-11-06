"""Registry ownership means the launcher PROCESS, not merely a live pid.

A thread id passes `kill(pid, 0)`, and a reused pid belongs to another process;
both made a dead launch record look alive, so `ask` routed into the old launch
(`CCB_ROUTE_ERROR reason=pane_dead`) while this launch's pane was fine.

Stored evidence is RAW identity -- the start offset in ticks since boot plus
the boot id -- never a wall-clock epoch: `btime` moves when the clock is
stepped (NTP, settimeofday, resume), and an epoch-based check then called a
genuine live launcher dead.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

import launcher_identity
import pane_registry

PROCFS_AVAILABLE = launcher_identity._procfs_available()
_needs_procfs = pytest.mark.skipif(
    not PROCFS_AVAILABLE, reason="needs Linux /proc for process-start evidence"
)


def _self_start_epoch() -> float:
    start = launcher_identity.process_start_epoch(os.getpid())
    assert start is not None
    return start


def _self_identity() -> dict:
    identity = launcher_identity.process_identity(os.getpid())
    assert identity and "ccb_pid_start_ticks" in identity and "ccb_boot_id" in identity
    return identity


def _record(**overrides) -> dict:
    """A record whose owner is this very (live) pytest process.

    No stored raw identity by default: the `ai-<epoch>-<pid>` launch id is the
    legacy evidence, exactly like a record written before this deploy.
    """
    start = _self_start_epoch()
    record = {
        "ccb_session_id": f"ai-{int(start) + 1}-{os.getpid()}",
        "ccb_pid": os.getpid(),
        "work_dir": "/tmp/proj",
        "terminal": "tmux",
    }
    record.update(overrides)
    return record


def _real_boot_epoch() -> float:
    boot = launcher_identity._boot_epoch()
    assert boot is not None
    return boot


def _thread_native_id() -> int:
    ids: list[int] = []
    ready = threading.Event()

    def worker() -> None:
        ids.append(threading.get_native_id())
        ready.set()
        time.sleep(30)  # stays a live thread for the whole test

    threading.Thread(target=worker, daemon=True).start()
    assert ready.wait(5)
    return ids[0]


@_needs_procfs
def test_the_recording_process_itself_reads_alive_with_stored_evidence() -> None:
    record = _record(**_self_identity())

    assert pane_registry._registry_owner_alive(record) is True


@_needs_procfs
@pytest.mark.parametrize("shift", [30.0, -30.0])
def test_a_genuine_launcher_survives_a_wall_clock_step(
    monkeypatch: pytest.MonkeyPatch, shift: float
) -> None:
    """Stored raw ticks are unaffected by a clock step that moves `btime`."""
    record = _record(**_self_identity())
    boot = _real_boot_epoch()
    before = _self_start_epoch()
    monkeypatch.setattr(launcher_identity, "_boot_epoch", lambda: boot + shift)

    # The epoch-based view of this very process really did move ...
    assert launcher_identity.process_start_epoch(os.getpid()) == pytest.approx(
        before + shift, abs=0.02
    )
    # ... and the raw identity still identifies the same live process.
    assert pane_registry._registry_owner_alive(record) is True


@_needs_procfs
def test_a_record_from_a_previous_boot_is_not_the_launcher() -> None:
    identity = _self_identity()
    identity["ccb_boot_id"] = "00000000-0000-0000-0000-000000000000"
    record = _record(**identity)

    assert pane_registry._registry_owner_alive(record) is False


@_needs_procfs
def test_stored_ticks_must_match_exactly() -> None:
    for delta in (-1, 1, 3600):
        identity = _self_identity()
        identity["ccb_pid_start_ticks"] += delta
        assert pane_registry._registry_owner_alive(_record(**identity)) is False


@_needs_procfs
def test_the_writer_stores_no_partial_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Without a boot id, nothing is written: a tick offset alone is not identity."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(launcher_identity, "boot_id", lambda: None)
    session_id = f"ai-{int(time.time())}-{os.getpid()}"

    assert launcher_identity.process_identity(os.getpid()) == {}
    assert pane_registry.upsert_registry({
        "ccb_session_id": session_id,
        "ccb_pid": os.getpid(),
        "work_dir": str(tmp_path),
        "terminal": "tmux",
    })

    data = json.loads(
        pane_registry.registry_path_for_session(session_id).read_text(encoding="utf-8")
    )
    assert data["ccb_pid"] == os.getpid()
    assert "ccb_pid_start_ticks" not in data
    assert "ccb_boot_id" not in data


@_needs_procfs
def test_ticks_without_a_boot_id_never_identify_a_process_alone() -> None:
    ticks = launcher_identity.process_start_ticks(os.getpid())
    assert ticks is not None
    # These ticks ARE this live process's, but without the boot id they are not
    # identity evidence -- and the launch id names an hour ago.
    record = _record(
        ccb_pid_start_ticks=ticks,
        ccb_session_id=f"ai-{int(time.time()) - 3600}-{os.getpid()}",
    )

    assert pane_registry._registry_owner_alive(record) is False


@_needs_procfs
def test_ticks_without_a_boot_id_fall_back_to_the_launch_id_window() -> None:
    ticks = launcher_identity.process_start_ticks(os.getpid())
    start = _self_start_epoch()
    record = _record(
        ccb_pid_start_ticks=ticks,
        ccb_session_id=f"ai-{int(start) + 1}-{os.getpid()}",
    )

    assert pane_registry._registry_owner_alive(record) is True


@_needs_procfs
def test_unreadable_current_boot_id_makes_stored_evidence_unproven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record(**_self_identity())
    monkeypatch.setattr(launcher_identity, "boot_id", lambda: None)

    assert pane_registry._registry_owner_alive(record) is None


@_needs_procfs
def test_a_legacy_record_with_matching_creation_evidence_reads_alive() -> None:
    # No stored raw identity: the launch id's epoch is the creation evidence,
    # and the launcher stamps it ~1 s after its own start (measured).
    start = _self_start_epoch()
    record = _record(ccb_pid=os.getpid(), ccb_session_id=f"ai-{int(start) + 1}-{os.getpid()}")

    assert pane_registry._registry_owner_alive(record) is True


@_needs_procfs
def test_legacy_window_absorbs_a_wall_clock_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy records must also survive a clock step since their launch."""
    record = _record()
    boot = _real_boot_epoch()
    monkeypatch.setattr(launcher_identity, "_boot_epoch", lambda: boot + 30.0)

    assert pane_registry._registry_owner_alive(record) is True

    # The widened window is still bounded: an hour away is not this launch.
    monkeypatch.setattr(launcher_identity, "_boot_epoch", lambda: boot + 3600.0)
    assert pane_registry._registry_owner_alive(record) is False


@_needs_procfs
def test_a_thread_id_is_not_the_launcher() -> None:
    tid = _thread_native_id()
    assert launcher_identity._task_tgid(tid) != tid  # the bug: kill(tid, 0) succeeds
    # Even when the raw identity it carries matches that task, a thread id is
    # not a process that a launch record can belong to.
    identity = launcher_identity.process_identity(tid)
    assert identity is not None
    record = _record(ccb_pid=tid, ccb_session_id=f"ai-{int(time.time())}-{tid}", **identity)

    assert pane_registry._registry_owner_alive(record) is False


@_needs_procfs
def test_a_reused_pid_is_not_the_launcher() -> None:
    # A live process with that numeric id, but its creation is an hour away
    # from the launch id it is supposed to have written.
    record = _record(ccb_session_id=f"ai-{int(time.time()) - 3600}-{os.getpid()}")

    assert pane_registry._registry_owner_alive(record) is False


@_needs_procfs
def test_a_live_pid_without_creation_evidence_is_unknown_not_dead() -> None:
    # No stored identity and no ai-<epoch>-<pid> id: existence alone proves
    # nothing, but nothing proves death either -- never strand a live launch.
    record = {"ccb_pid": os.getpid(), "ccb_session_id": "custom-id"}

    assert pane_registry._registry_owner_alive(record) is None


@_needs_procfs
def test_unreadable_start_ticks_for_a_live_process_are_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The process demonstrably exists; only its start offset is unavailable.
    # Never turn an evidence limit into a death sentence.
    record = _record(**_self_identity())
    monkeypatch.setattr(launcher_identity, "process_start_ticks", lambda _pid: None)

    assert pane_registry._registry_owner_alive(record) is None


def test_non_linux_keeps_the_documented_bare_pid_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    # Deliberate: no portable creation-time source, so the historical bare-pid
    # answer stays, and is named here so its limits are visible in tests.
    calls: list[int] = []

    def _fake_is_pid_alive(pid: int) -> bool:
        calls.append(pid)
        return pid == 4242

    monkeypatch.setattr(launcher_identity, "_procfs_available", lambda: False)
    monkeypatch.setattr(launcher_identity, "_is_pid_alive", _fake_is_pid_alive)

    assert launcher_identity.launcher_alive({"ccb_pid": 4242}, 4242) is True
    assert launcher_identity.launcher_alive({"ccb_pid": 4243}, 4243) is False
    # No /proc evidence is consulted at all on such a platform.
    assert calls == [4242, 4243]


@_needs_procfs
def test_upsert_registry_persists_the_owners_raw_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    session_id = f"ai-{int(time.time())}-{os.getpid()}"

    assert pane_registry.upsert_registry({
        "ccb_session_id": session_id,
        "ccb_pid": os.getpid(),
        "work_dir": str(tmp_path),
        "terminal": "tmux",
    })

    path = pane_registry.registry_path_for_session(session_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ccb_pid"] == os.getpid()
    assert data["ccb_pid_start_ticks"] == launcher_identity.process_start_ticks(os.getpid())
    assert data["ccb_boot_id"] == launcher_identity.boot_id()
    # The old wall-clock field is gone: nothing reads it, and it was the thing
    # that broke under a clock step.
    assert "ccb_pid_started_at" not in data
    assert pane_registry._registry_owner_alive(data) is True


@_needs_procfs
def test_upsert_registry_never_writes_another_processs_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # `ai-<epoch>-1`: pid 1 is not this test process, so this write is a member
    # update of someone else's record and must not stamp its own identity.
    session_id = f"ai-{int(time.time())}-1"
    assert pane_registry.upsert_registry({
        "ccb_session_id": session_id,
        "ccb_pid": 1,
        "work_dir": str(tmp_path),
        "terminal": "tmux",
    })

    data = json.loads(
        pane_registry.registry_path_for_session(session_id).read_text(encoding="utf-8")
    )
    assert data["ccb_pid"] == 1
    assert "ccb_pid_start_ticks" not in data
    assert "ccb_boot_id" not in data
