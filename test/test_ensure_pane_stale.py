"""Tests for ensure_pane() title marker verification in the fast path.

Verifies that when a cached pane_id is alive but the title marker resolves
to a different pane (tmux ID recycling), ensure_pane() updates to the
correct pane instead of routing messages to the wrong process.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from gaskd_session import GeminiProjectSession
from caskd_session import CodexProjectSession
from oaskd_session import OpenCodeProjectSession
from laskd_session import ClaudeProjectSession


class _FakeBackend:
    """Fake terminal backend for testing ensure_pane()."""

    def __init__(
        self,
        alive_panes: set[str],
        marker_map: Optional[dict[str, str]] = None,
    ):
        self.alive_panes = alive_panes
        self.marker_map = marker_map or {}
        self.attached: list[str] = []

    def is_alive(self, pane_id: str) -> bool:
        return pane_id in self.alive_panes

    def find_pane_by_title_marker(self, marker: str, cwd_hint: str = "") -> Optional[str]:
        return self.marker_map.get(marker)

    def ensure_pane_log(self, pane_id: str) -> None:
        self.attached.append(pane_id)


def _write_session(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_session(cls, tmp_path: Path, pane_id: str, marker: str, backend: _FakeBackend):
    """Create a session object with a fake backend."""
    session_file = tmp_path / ".session"
    data = {
        "pane_id": pane_id,
        "pane_title_marker": marker,
        "terminal": "tmux",
        "work_dir": str(tmp_path),
    }
    _write_session(session_file, data)
    session = cls.__new__(cls)
    session.data = data
    session.session_file = session_file
    session._backend = backend

    # Override backend() to return our fake
    session.backend = lambda: backend
    # Override _attach_pane_log to be a no-op
    session._attach_pane_log = lambda b, pid: None
    # Override _write_back to update the file
    session._write_back = lambda: _write_session(session_file, session.data)
    return session


# All session classes to test
SESSION_CLASSES = [
    GeminiProjectSession,
    CodexProjectSession,
    OpenCodeProjectSession,
    ClaudeProjectSession,
]


@pytest.mark.parametrize("cls", SESSION_CLASSES, ids=lambda c: c.__name__)
def test_fast_path_returns_correct_pane_when_marker_matches(cls, tmp_path: Path) -> None:
    """When pane_id is alive AND marker resolves to the same pane, return it."""
    backend = _FakeBackend(
        alive_panes={"%10"},
        marker_map={"CCB-Gemini-abc": "%10"},
    )
    session = _make_session(cls, tmp_path, "%10", "CCB-Gemini-abc", backend)

    ok, pane = session.ensure_pane()

    assert ok is True
    assert pane == "%10"
    # pane_id should NOT change
    assert session.data["pane_id"] == "%10"


@pytest.mark.parametrize("cls", SESSION_CLASSES, ids=lambda c: c.__name__)
def test_fast_path_switches_to_marker_pane_when_id_stale(cls, tmp_path: Path) -> None:
    """When cached pane_id is alive but marker resolves to a DIFFERENT alive
    pane, ensure_pane() should switch to the marker's pane."""
    backend = _FakeBackend(
        alive_panes={"%10", "%20"},
        marker_map={"CCB-Gemini-abc": "%20"},
    )
    session = _make_session(cls, tmp_path, "%10", "CCB-Gemini-abc", backend)

    ok, pane = session.ensure_pane()

    assert ok is True
    assert pane == "%20"
    assert session.data["pane_id"] == "%20"


@pytest.mark.parametrize("cls", SESSION_CLASSES, ids=lambda c: c.__name__)
def test_fast_path_keeps_pane_when_no_marker(cls, tmp_path: Path) -> None:
    """When no title marker is set, fast path should return the alive pane."""
    backend = _FakeBackend(alive_panes={"%10"})
    session = _make_session(cls, tmp_path, "%10", "", backend)

    ok, pane = session.ensure_pane()

    assert ok is True
    assert pane == "%10"


@pytest.mark.parametrize("cls", SESSION_CLASSES, ids=lambda c: c.__name__)
def test_fallback_resolves_by_marker_when_pane_dead(cls, tmp_path: Path) -> None:
    """When cached pane_id is dead, fall through to marker resolution."""
    backend = _FakeBackend(
        alive_panes={"%20"},
        marker_map={"CCB-Gemini-abc": "%20"},
    )
    session = _make_session(cls, tmp_path, "%10", "CCB-Gemini-abc", backend)

    ok, pane = session.ensure_pane()

    assert ok is True
    assert pane == "%20"
    assert session.data["pane_id"] == "%20"


@pytest.mark.parametrize("cls", SESSION_CLASSES, ids=lambda c: c.__name__)
def test_fast_path_keeps_pane_when_resolver_raises(cls, tmp_path: Path) -> None:
    """If find_pane_by_title_marker raises, fast path should still work."""
    class _BrokenBackend(_FakeBackend):
        def find_pane_by_title_marker(self, marker: str, cwd_hint: str = "") -> Optional[str]:
            raise RuntimeError("tmux error")

    backend = _BrokenBackend(alive_panes={"%10"})
    session = _make_session(cls, tmp_path, "%10", "CCB-Gemini-abc", backend)

    ok, pane = session.ensure_pane()

    assert ok is True
    assert pane == "%10"
