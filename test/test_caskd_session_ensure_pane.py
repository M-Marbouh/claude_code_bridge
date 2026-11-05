from __future__ import annotations

import json
from pathlib import Path

import pytest

import caskd_session


class FakeTmuxBackend:
    def __init__(self) -> None:
        self.alive: dict[str, bool] = {}
        self.crash_logs: list[tuple[str, str]] = []
        self.respawned: list[str] = []
        self.respawn_cmds: list[str] = []
        self.marker_map: dict[str, str] = {}
        # A respawn gives the pane a new process, and therefore a new pid.
        self.pane_pids: dict[str, str] = {}
        self.next_pane_pid: str = ""

    def pane_pid(self, pane_id: str) -> str:
        return self.pane_pids.get(pane_id, "")

    def is_alive(self, pane_id: str) -> bool:
        return bool(self.alive.get(pane_id, False))

    def is_pane_dead(self, pane_id: str) -> bool:
        return pane_id in self.alive and not self.alive[pane_id]

    def pane_belongs_to_cwd(self, pane_id: str, work_dir: str) -> bool:
        return True

    def pane_has_different_ccb_marker(self, pane_id: str, marker: str) -> bool:
        return False

    def find_pane_by_title_marker(self, marker: str, cwd_hint: str = "") -> str | None:
        for prefix, pane in self.marker_map.items():
            if marker.startswith(prefix) or prefix.startswith(marker):
                return pane
        return None

    def save_crash_log(self, pane_id: str, crash_log_path: str, *, lines: int = 1000) -> None:
        self.crash_logs.append((pane_id, crash_log_path))

    def respawn_pane(self, pane_id: str, *, cmd: str, cwd: str | None = None,
                     stderr_log_path: str | None = None, remain_on_exit: bool = True,
                     kill_live: bool = True) -> None:
        self.respawned.append(pane_id)
        self.respawn_cmds.append(cmd)
        self.alive[pane_id] = True
        if self.next_pane_pid:
            self.pane_pids[pane_id] = self.next_pane_pid


def test_caskd_ensure_pane_respawns_dead_pane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When pane is dead, ensure_pane should respawn it and update session file."""
    session_path = tmp_path / ".codex-session"
    session_path.write_text(
        json.dumps({
            "session_id": "test-session",
            "terminal": "tmux",
            "pane_id": "%1",
            "pane_title_marker": "CCB-codex-test",
            "runtime_dir": str(tmp_path),
            "work_dir": str(tmp_path),
            "active": True,
            "codex_start_cmd": "codex resume deadbeef",
        }),
        encoding="utf-8",
    )

    backend = FakeTmuxBackend()
    backend.alive = {"%1": False, "%2": False}
    backend.marker_map = {"CCB-codex": "%2"}
    monkeypatch.setattr(caskd_session, "get_backend_for_session", lambda data: backend)

    sess = caskd_session.load_project_session(tmp_path)
    assert sess is not None

    ok, pane = sess.ensure_pane()
    assert ok is True
    assert pane == "%2"
    assert "%2" in backend.respawned

    data = json.loads(session_path.read_text(encoding="utf-8"))
    assert data["pane_id"] == "%2"


def test_caskd_ensure_pane_already_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When pane is already alive, ensure_pane should return success immediately."""
    session_path = tmp_path / ".codex-session"
    session_path.write_text(
        json.dumps({
            "session_id": "test-session",
            "terminal": "tmux",
            "pane_id": "%1",
            "pane_title_marker": "CCB-codex-test",
            "work_dir": str(tmp_path),
            "active": True,
        }),
        encoding="utf-8",
    )

    backend = FakeTmuxBackend()
    backend.alive = {"%1": True}
    monkeypatch.setattr(caskd_session, "get_backend_for_session", lambda data: backend)

    sess = caskd_session.load_project_session(tmp_path)
    assert sess is not None

    ok, pane = sess.ensure_pane()
    assert ok is True
    assert pane == "%1"
    assert backend.respawned == []  # No respawn needed


def test_caskd_ensure_pane_marker_rediscover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When original pane is dead but marker finds alive pane, should update pane_id."""
    session_path = tmp_path / ".codex-session"
    session_path.write_text(
        json.dumps({
            "session_id": "test-session",
            "terminal": "tmux",
            "pane_id": "%1",
            "pane_title_marker": "CCB-codex-test",
            "work_dir": str(tmp_path),
            "active": True,
        }),
        encoding="utf-8",
    )

    backend = FakeTmuxBackend()
    backend.alive = {"%1": False, "%2": True}  # %2 is alive
    backend.marker_map = {"CCB-codex": "%2"}
    monkeypatch.setattr(caskd_session, "get_backend_for_session", lambda data: backend)

    sess = caskd_session.load_project_session(tmp_path)
    assert sess is not None

    ok, pane = sess.ensure_pane()
    assert ok is True
    assert pane == "%2"
    assert backend.respawned == []  # No respawn needed, just rediscovered

    data = json.loads(session_path.read_text(encoding="utf-8"))
    assert data["pane_id"] == "%2"


def _write_launch_record(tmp_path: Path, *, with_credential: bool) -> str:
    import os

    import pane_registry

    launch_id = "ai-claims-respawn"
    session_file = tmp_path / ".codex-session"
    providers = {
        "codex": {
            "pane_id": "%1",
            "pane_title_marker": "CCB-codex-test",
            "session_file": str(session_file),
        }
    }
    if with_credential:
        providers["codex"].update(
            {"member_id": "member-codex", "member_token": "respawn-secret"}
        )
    assert pane_registry.upsert_registry(
        {
            "ccb_session_id": launch_id,
            "ccb_project_id": "p" * 8,
            "work_dir": str(tmp_path),
            "terminal": "tmux",
            "ccb_pid": os.getpid(),
            "providers": providers,
        }
    )
    return launch_id


def test_respawn_restores_the_credential_from_the_private_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch_id = _write_launch_record(tmp_path, with_credential=True)
    session_path = tmp_path / ".codex-session"
    session_path.write_text(
        json.dumps({
            "session_id": launch_id,
            "terminal": "tmux",
            "pane_id": "%1",
            "pane_title_marker": "CCB-codex-test",
            "runtime_dir": str(tmp_path),
            "work_dir": str(tmp_path),
            "active": True,
            "codex_start_cmd": "codex resume deadbeef",
        }),
        encoding="utf-8",
    )
    backend = FakeTmuxBackend()
    backend.alive = {"%1": False}
    monkeypatch.setattr(caskd_session, "get_backend_for_session", lambda data: backend)

    sess = caskd_session.load_project_session(tmp_path)
    assert sess is not None
    ok, pane = sess.ensure_pane()

    assert ok and pane == "%1"
    assert backend.respawn_cmds
    command = backend.respawn_cmds[-1]
    assert "CCB_MEMBER_ID=member-codex" in command
    assert "CCB_MEMBER_TOKEN=respawn-secret" in command
    assert command.endswith("codex resume deadbeef")
    # The stored command itself still carries no credential.
    assert "respawn-secret" not in session_path.read_text(encoding="utf-8")


def test_respawn_without_a_registry_record_still_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_path = tmp_path / ".codex-session"
    session_path.write_text(
        json.dumps({
            "session_id": "ai-no-record",
            "terminal": "tmux",
            "pane_id": "%1",
            "pane_title_marker": "CCB-codex-test",
            "runtime_dir": str(tmp_path),
            "work_dir": str(tmp_path),
            "active": True,
            "codex_start_cmd": "codex resume deadbeef",
        }),
        encoding="utf-8",
    )
    backend = FakeTmuxBackend()
    backend.alive = {"%1": False}
    monkeypatch.setattr(caskd_session, "get_backend_for_session", lambda data: backend)

    sess = caskd_session.load_project_session(tmp_path)
    assert sess is not None
    ok, pane = sess.ensure_pane()

    assert ok and pane == "%1"
    assert backend.respawn_cmds == ["codex resume deadbeef"]
    assert "CCB_MEMBER_TOKEN" not in backend.respawn_cmds[0]


@pytest.mark.parametrize("failure", ["lock_timeout", "write_failure"])
def test_recovery_proceeds_and_the_old_pid_bound_role_stays_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import os

    import live_roles

    launch_id = _write_launch_record(tmp_path, with_credential=True)
    session_path = tmp_path / ".codex-session"
    session_path.write_text(
        json.dumps({
            "session_id": launch_id,
            "terminal": "tmux",
            "pane_id": "%1",
            "pane_title_marker": "CCB-codex-test",
            "runtime_dir": str(tmp_path),
            "work_dir": str(tmp_path),
            "active": True,
            "codex_start_cmd": "codex resume deadbeef",
        }),
        encoding="utf-8",
    )
    live_roles.roles_path_for_launch(launch_id).write_text(
        json.dumps({
            "launch_id": launch_id,
            "claims": {
                "member-codex": {
                    "role": "lead",
                    "pane_id": "%1",
                    "provider": "codex",
                    "pane_pid": "1001",
                    "claimed_at": 1,
                }
            },
        }),
        encoding="utf-8",
    )
    import pane_registry

    record = pane_registry.load_registry_by_session_id(launch_id)
    assert record is not None

    backend = FakeTmuxBackend()
    backend.alive = {"%1": False}
    backend.pane_pids = {"%1": "1001"}
    backend.next_pane_pid = "2002"
    monkeypatch.setattr(caskd_session, "get_backend_for_session", lambda data: backend)

    if failure == "lock_timeout":
        monkeypatch.setattr(live_roles, "ROLE_LOCK_TIMEOUT_S", 0.05)
        held = live_roles._role_lock(launch_id)
        assert held.acquire()
        try:
            sess = caskd_session.load_project_session(tmp_path)
            assert sess is not None
            ok, pane = sess.ensure_pane()
        finally:
            held.release()
    else:
        def _boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(live_roles, "_write_claims", _boom)
        sess = caskd_session.load_project_session(tmp_path)
        assert sess is not None
        ok, pane = sess.ensure_pane()

    # Recovery itself is never blocked by role bookkeeping ...
    assert ok and pane == "%1"
    assert backend.respawned == ["%1"]
    assert "respawn-secret" in backend.respawn_cmds[-1]

    # ... and the old assignment cannot come back, because its witness is the
    # panes previous process.
    monkeypatch.setattr(pane_registry, "_is_pid_alive", lambda pid: True)
    assert record["ccb_pid"] == os.getpid()
    assert live_roles.roles_for_record(record) == {}
