from __future__ import annotations

import json
from pathlib import Path

import pytest

from caskd_session import CodexProjectSession
from gaskd_session import GeminiProjectSession
from oaskd_session import OpenCodeProjectSession


SESSION_CLASSES = [OpenCodeProjectSession, GeminiProjectSession, CodexProjectSession]
INCIDENT_WORK_DIR = "/tmp/ccb-test-foreign-project"


class FakeBackend:
    def __init__(
        self,
        *,
        alive: set[str] = frozenset(),
        dead: set[str] = frozenset(),
        cwd_matches: set[str] = frozenset(),
        marker_map: dict[str, str] | None = None,
        foreign_markers: set[str] = frozenset(),
        dead_probe_failures: set[str] = frozenset(),
    ) -> None:
        self.alive = set(alive)
        self.dead = set(dead)
        self.cwd_matches = set(cwd_matches)
        self.marker_map = marker_map or {}
        self.foreign_markers = set(foreign_markers)
        self.dead_probe_failures = set(dead_probe_failures)
        self.respawned: list[tuple[str, bool]] = []
        self.option_calls: list[str] = []
        self.log_calls: list[str] = []
        self.crash_logs: list[str] = []
        self.resolver_calls: list[tuple[str, str]] = []

    def is_alive(self, pane_id: str) -> bool:
        return pane_id in self.alive

    def is_pane_dead(self, pane_id: str) -> bool:
        return pane_id in self.dead and pane_id not in self.dead_probe_failures

    def pane_belongs_to_cwd(self, pane_id: str, work_dir: str) -> bool:
        return pane_id in self.cwd_matches

    def pane_has_different_ccb_marker(self, pane_id: str, marker: str) -> bool:
        return pane_id in self.foreign_markers

    def find_pane_by_title_marker(self, marker: str, cwd_hint: str = "") -> str | None:
        self.resolver_calls.append((marker, cwd_hint))
        return self.marker_map.get(marker)

    def ensure_pane_log(self, pane_id: str) -> None:
        self.log_calls.append(pane_id)

    def save_crash_log(self, pane_id: str, crash_log_path: str, *, lines: int = 1000) -> None:
        self.crash_logs.append(pane_id)

    def respawn_pane(
        self,
        pane_id: str,
        *,
        cmd: str,
        cwd: str | None = None,
        remain_on_exit: bool = True,
        kill_live: bool = True,
        **kwargs,
    ) -> None:
        self.respawned.append((pane_id, kill_live))
        self.alive.add(pane_id)


def _make_session(cls, tmp_path: Path, backend: FakeBackend, *, work_dir: str = "/tmp/project"):
    session_file = tmp_path / ".session"
    marker = "CCB-OpenCode-0a1b2c3d"
    data = {
        "terminal": "tmux",
        "pane_id": "%0",
        "pane_title_marker": marker,
        "tmux_session": None,
        "work_dir": work_dir,
        "runtime_dir": str(tmp_path / "runtime"),
        "start_cmd": "export CCB_MANAGED=1; opencode",
        "codex_start_cmd": "export CCB_MANAGED=1; codex",
    }
    session_file.write_text(json.dumps(data), encoding="utf-8")
    session = cls.__new__(cls)
    session.data = data
    session.session_file = session_file
    session._backend = backend
    session.backend = lambda: backend
    session._attach_pane_log = lambda terminal_backend, pane_id: terminal_backend.ensure_pane_log(pane_id)
    writes: list[None] = []

    def write_back() -> None:
        writes.append(None)
        session_file.write_text(json.dumps(session.data), encoding="utf-8")

    session._write_back = write_back
    return session, writes, session_file


@pytest.mark.parametrize("cls", SESSION_CLASSES, ids=lambda c: c.__name__)
def test_refuses_incident_live_unowned_pane_without_side_effects(cls, tmp_path: Path) -> None:
    backend = FakeBackend(alive={"%0"}, cwd_matches=set())
    session, writes, session_file = _make_session(cls, tmp_path, backend, work_dir=INCIDENT_WORK_DIR)
    before = session_file.read_text(encoding="utf-8")

    ok, result = session.ensure_pane()

    assert (ok, result) == (False, "Pane not alive: %0")
    assert backend.respawned == []
    assert backend.option_calls == []
    assert backend.log_calls == []
    assert backend.crash_logs == []
    assert writes == []
    assert session_file.read_text(encoding="utf-8") == before
    assert backend.resolver_calls  # Marker rediscovery happens before refusal.


@pytest.mark.parametrize("cls", SESSION_CLASSES, ids=lambda c: c.__name__)
def test_dead_owned_cached_pane_respawns_without_kill_live(cls, tmp_path: Path) -> None:
    backend = FakeBackend(dead={"%0"}, cwd_matches={"%0"})
    session, writes, _ = _make_session(cls, tmp_path, backend)

    ok, pane = session.ensure_pane()

    assert (ok, pane) == (True, "%0")
    assert backend.respawned == [("%0", False)]
    assert writes


@pytest.mark.parametrize("cls", SESSION_CLASSES, ids=lambda c: c.__name__)
def test_marker_resolved_dead_pane_respawns(cls, tmp_path: Path) -> None:
    backend = FakeBackend(dead={"%9"}, marker_map={"CCB-OpenCode-0a1b2c3d": "%9"})
    session, writes, _ = _make_session(cls, tmp_path, backend)

    ok, pane = session.ensure_pane()

    assert (ok, pane) == (True, "%9")
    assert backend.respawned == [("%9", False)]
    assert writes


@pytest.mark.parametrize("cls", SESSION_CLASSES, ids=lambda c: c.__name__)
@pytest.mark.parametrize("state", ["missing", "probe_failure", "foreign_marker"])
def test_refuses_uncertain_or_conflicting_cached_pane_without_side_effects(
    cls, state: str, tmp_path: Path
) -> None:
    kwargs = {"cwd_matches": {"%0"}}
    if state == "probe_failure":
        kwargs.update(dead={"%0"}, dead_probe_failures={"%0"})
    elif state == "foreign_marker":
        kwargs.update(dead={"%0"}, foreign_markers={"%0"})
    backend = FakeBackend(**kwargs)
    session, writes, _ = _make_session(cls, tmp_path, backend)

    ok, result = session.ensure_pane()

    assert (ok, result) == (False, "Pane not alive: %0")
    assert backend.respawned == []
    assert backend.option_calls == []
    assert backend.log_calls == []
    assert backend.crash_logs == []
    assert writes == []


@pytest.mark.parametrize("cls", SESSION_CLASSES, ids=lambda c: c.__name__)
def test_live_matching_pane_is_returned_without_respawn(cls, tmp_path: Path) -> None:
    backend = FakeBackend(alive={"%0"}, cwd_matches={"%0"})
    session, writes, _ = _make_session(cls, tmp_path, backend)

    ok, pane = session.ensure_pane()

    assert (ok, pane) == (True, "%0")
    assert backend.respawned == []
    assert not writes


@pytest.mark.parametrize("cls", SESSION_CLASSES, ids=lambda c: c.__name__)
def test_marker_found_on_another_live_pane_is_returned(cls, tmp_path: Path) -> None:
    backend = FakeBackend(
        alive={"%0", "%9"},
        marker_map={"CCB-OpenCode-0a1b2c3d": "%9"},
    )
    session, writes, _ = _make_session(cls, tmp_path, backend)

    ok, pane = session.ensure_pane()

    assert (ok, pane) == (True, "%9")
    assert session.data["pane_id"] == "%9"
    assert backend.respawned == []
    assert writes
