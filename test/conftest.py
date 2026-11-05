from __future__ import annotations

import tempfile
import sys
from pathlib import Path

import pytest


def pytest_configure() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    lib_dir = repo_root / "lib"
    sys.path.insert(0, str(lib_dir))


@pytest.fixture(autouse=True)
def isolate_process_temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CLI subprocess receipts, state, and registries out of live user paths."""
    for name in (
        "CCB_MANAGED",
        "CCB_CALLER",
        "CCB_RUN_DIR",
        "CCB_SESSION_ID",
        "CCB_CALLER_PANE_ID",
        "CCB_CALLER_TERMINAL",
        "CCB_ROUTE_LAUNCH_ID",
        "CCB_CALLER_LIVE_ID",
        # A suite run from inside a CCB pane inherits that pane's live
        # identity; tests must never depend on the launching environment.
        "CCB_MEMBER_ID",
        "CCB_MEMBER_TOKEN",
        "CCB_LIVE_ID",
        "CCB_LIVE_TOKEN",
        "CCB_ROUTE_LIVE_ID",
        "CCB_ROUTE_CALLER_LIVE_ID",
        "CCB_ROUTE_CALLER_TOKEN",
        "CCB_ROUTE_CALLER_PANE_ID",
        "CCB_ROUTE_CALLER_TERMINAL",
        "CCB_ROUTE_PANE_ID",
        "CCB_ROUTE_TERMINAL",
        "CCB_ROUTE_SESSION_FILE",
        "CCB_ROUTE_PROJECT_ID",
        "CCB_ROUTE_TASK_ID",
        "WEZTERM_PANE",
        "TMUX_PANE",
    ):
        monkeypatch.delenv(name, raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
