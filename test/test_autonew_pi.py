from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import time
from pathlib import Path

import pytest

from project_id import compute_ccb_project_id

FIXTURES = Path(__file__).parent / "fixtures" / "pi"
PANE = "%77"
MARKER = "Pi-autonew-marker"
SESSION_ID = "2c8ab895-d0ab-49c6-86c1-0f7ff9fb6e8e"


def _autonew():
    path = Path(__file__).resolve().parents[1] / "bin" / "autonew"
    loader = importlib.machinery.SourceFileLoader("autonew_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _screen(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _draft_screen() -> str:
    lines = _screen("screen-idle.txt").splitlines()
    rule = max(index for index, line in enumerate(lines) if line.startswith("─" * 8))
    lines[rule - 1] = "draft text"
    return "\n".join(lines)


class FakePiBackend:
    """Pane backend whose captures come from a script (repeats the last screen)."""

    def __init__(self, screens: list[str] | None = None, *, capture_error: bool = False):
        self.screens = list(screens or [])
        self.capture_error = capture_error
        self.sent: list[tuple[str, str]] = []
        self.marker_pane = PANE
        self.markers = {MARKER: PANE, "Codex-marker": "%90", "Record-marker": "%15"}
        self.alive_panes = {PANE, "%90"}

    def is_alive(self, pane: str) -> bool:
        return pane in self.alive_panes

    def find_pane_by_title_marker(self, marker: str, _cwd: str) -> str:
        if marker == MARKER:
            return self.marker_pane
        return self.markers.get(marker, "")

    def get_pane_content(self, _pane: str, lines: int = 40) -> str:
        if self.capture_error:
            raise RuntimeError("capture unavailable")
        if not self.screens:
            raise RuntimeError("no screen scripted")
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]

    def send_text(self, pane: str, text: str) -> None:
        self.sent.append((pane, text))


def _project(
    tmp_path: Path, *, binding: dict | None = None, write_session: bool = True, launch: bool = True,
) -> Path:
    project = tmp_path / "project"
    (project / ".ccb").mkdir(parents=True)
    session_dir = project / "sessions"
    session_dir.mkdir()
    if write_session:
        header = {"type": "session", "id": SESSION_ID, "cwd": str(project), "version": 3}
        (session_dir / f"2026-11-06T18-00-00.000Z_{SESSION_ID}.jsonl").write_text(json.dumps(header) + "\n")
    data = binding if binding is not None else {
        "active": True, "terminal": "tmux", "work_dir": str(project), "start_dir": str(project),
        "session_id": "launch-1", "pi_session_id": SESSION_ID, "pi_session_dir": str(session_dir),
        "pane_id": PANE, "pane_title_marker": MARKER,
    }
    (project / ".ccb" / ".pi-session").write_text(json.dumps(data), encoding="utf-8")
    if launch:
        # The one live Pi session this project publishes, matching the default binding.
        _write_launch(Path.home(), "launch-1", project, panes=(PANE,))
    return project


def _use_backend(monkeypatch: pytest.MonkeyPatch, backend: FakePiBackend) -> None:
    # pi_comm imports the backend factory lazily, pane_registry binds it at import time.
    monkeypatch.setattr("terminal.get_backend_for_session", lambda _data: backend)
    monkeypatch.setattr("pane_registry.get_backend_for_session", lambda _data: backend)


def _write_launch(
    home: Path, launch_id: str, project: Path, *, panes: tuple[str, ...], inventory: bool = True,
    updated_at: int | None = None, terminal: str = "tmux", marker: str = MARKER,
    markers: tuple[str, ...] | None = None,
) -> None:
    registry = home / ".ccb" / "run"
    registry.mkdir(parents=True, exist_ok=True)
    project_id = compute_ccb_project_id(project)
    record = {
        "ccb_session_id": launch_id, "ccb_project_id": project_id, "work_dir": str(project),
        "updated_at": int(time.time()) if updated_at is None else updated_at, "terminal": terminal,
    }
    entry_markers = markers or tuple(marker for _ in panes)
    if inventory:
        record["live_sessions"] = [
            {
                "live_id": f"{launch_id}-pi-{index}", "provider": "pi", "launch_id": launch_id,
                "pane_id": pane, "terminal": terminal, "pane_title_marker": entry_markers[index],
                "work_dir": str(project),
                "ccb_project_id": project_id, "active": True,
            }
            for index, pane in enumerate(panes)
        ]
        record["providers"] = {"pi": {"pane_id": panes[0] if panes else "", "pane_title_marker": entry_markers[0] if entry_markers else marker}}
    else:
        record["providers"] = {
            "codex": {"pane_id": "%90", "pane_title_marker": "Codex-marker"},
            "pi": {"pane_id": panes[0], "pane_title_marker": marker},
        }
    (registry / f"ccb-session-{launch_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _run(monkeypatch: pytest.MonkeyPatch, capsys, project: Path, provider: str = "pi") -> tuple[int, str, str]:
    _autonew()
    monkeypatch.chdir(project)
    code = _autonew().main(["autonew", provider])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_pi_is_a_supported_provider_with_slash_new(capsys) -> None:
    autonew = _autonew()
    assert autonew.PROVIDER_COMMANDS["pi"] == "/new"
    assert autonew.main(["autonew", "--help"]) == 0
    err = capsys.readouterr().err
    assert "gemini, codex, opencode, claude, pi" in err


def test_idle_pi_sends_new_once_and_leaves_the_binding_untouched(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path)
    backend = FakePiBackend([_screen("screen-idle.txt")])
    _use_backend(monkeypatch, backend)
    binding = project / ".ccb" / ".pi-session"
    before = hashlib.sha256(binding.read_bytes()).hexdigest()

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 0
    assert backend.sent == [(PANE, "/new")]
    assert "Sent /new to pi" in out
    assert err == ""
    assert hashlib.sha256(binding.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("screen_name,make_screen", [
    ("busy", lambda: _screen("screen-busy.txt")),
    ("trust-dialog", lambda: _screen("screen-trust-dialog.txt")),
    ("draft", _draft_screen),
])
def test_non_idle_pi_screen_refuses_with_zero_sends(monkeypatch, capsys, tmp_path: Path, screen_name, make_screen) -> None:
    project = _project(tmp_path)
    backend = FakePiBackend([make_screen()])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert out == ""
    assert "refusing to send /new" in err
    assert len([line for line in err.splitlines() if line.strip()]) == 1


@pytest.mark.parametrize("second_screen", ["busy", "dialog", "draft"])
def test_pane_that_changes_between_captures_refuses(monkeypatch, capsys, tmp_path: Path, second_screen: str) -> None:
    project = _project(tmp_path)
    follow = {
        "busy": _screen("screen-busy.txt"),
        "dialog": _screen("screen-trust-dialog.txt"),
        "draft": _draft_screen(),
    }[second_screen]
    backend = FakePiBackend([_screen("screen-idle.txt"), follow])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "refusing to send /new" in err


def test_capture_failure_refuses_with_zero_sends(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path)
    backend = FakePiBackend(capture_error=True)
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "Pi pane is unknown; refusing to send /new" in err


def test_two_live_pi_sessions_in_one_launch_refuse(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path, launch=False)
    _write_launch(Path.home(), "launch-a", project, panes=(PANE, "%78"), markers=(MARKER, "Record-marker-1"))
    backend = FakePiBackend([_screen("screen-idle.txt")])
    backend.markers["Record-marker-1"] = "%78"
    backend.alive_panes = {PANE, "%78"}
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "2 live Pi sessions for this project" in err


def test_two_launches_for_the_project_refuse(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path, launch=False)
    _write_launch(Path.home(), "launch-a", project, panes=(PANE,))
    _write_launch(Path.home(), "launch-b", project, panes=("%79",), marker="Record-marker-1")
    backend = FakePiBackend([_screen("screen-idle.txt")])
    backend.markers["Record-marker-1"] = "%79"
    backend.alive_panes = {PANE, "%79"}
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "2 live Pi sessions for this project" in err


def test_broken_pi_inventory_refuses(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path, launch=False)
    registry = Path.home() / ".ccb" / "run"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / "ccb-session-launch-broken.json").write_text(json.dumps({
        "ccb_session_id": "launch-broken", "ccb_project_id": compute_ccb_project_id(project),
        "work_dir": str(project), "updated_at": int(time.time()), "terminal": "tmux",
        "live_sessions": "not-a-list",
    }))
    backend = FakePiBackend([_screen("screen-idle.txt")])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "broken Pi inventory" in err


@pytest.mark.parametrize("case", ["inactive", "incomplete-binding", "marker-mismatch", "missing-binding"])
def test_unverifiable_destination_refuses(monkeypatch, capsys, tmp_path: Path, case: str) -> None:
    binding = {
        "active": True, "terminal": "tmux", "work_dir": str(tmp_path), "start_dir": str(tmp_path),
        "session_id": "launch-1", "pi_session_id": SESSION_ID,
        "pi_session_dir": str(tmp_path / "project" / "sessions"), "pane_id": PANE,
        "pane_title_marker": MARKER,
    }
    if case == "inactive":
        binding["active"] = False
    elif case == "incomplete-binding":
        binding.pop("pi_session_id")
        binding.pop("pi_session_dir")
    project = _project(tmp_path, binding=binding)
    backend = FakePiBackend([_screen("screen-idle.txt")])
    if case == "marker-mismatch":
        backend.marker_pane = "%99"
    _use_backend(monkeypatch, backend)
    if case == "missing-binding":
        (project / ".ccb" / ".pi-session").unlink()

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "[ERROR] pi: " in err


def test_codex_path_is_unchanged_and_still_sends_blindly(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_launch(Path.home(), "launch-a", project, panes=(PANE,), inventory=False)
    backend = FakePiBackend([_screen("screen-busy.txt")])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project, provider="codex")

    assert code == 0
    assert backend.sent == [("%90", "/new")]
    assert "Sent /new to codex" in out


def test_no_live_pi_session_refuses(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path, launch=False)
    backend = FakePiBackend([_screen("screen-idle.txt")])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "no live Pi session for this project" in err


def test_empty_inventory_refuses(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path, launch=False)
    _write_launch(Path.home(), "launch-1", project, panes=())
    backend = FakePiBackend([_screen("screen-idle.txt")])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "no live Pi session for this project" in err


def test_counted_session_on_another_pane_than_the_binding_refuses(monkeypatch, capsys, tmp_path: Path) -> None:
    binding = {
        "active": True, "terminal": "tmux", "work_dir": str(tmp_path), "start_dir": str(tmp_path),
        "session_id": "launch-1", "pi_session_id": SESSION_ID,
        "pi_session_dir": str(tmp_path / "project" / "sessions"), "pane_id": "%99",
        "pane_title_marker": MARKER,
    }
    project = _project(tmp_path, binding=binding, launch=False)
    _write_launch(Path.home(), "launch-1", project, panes=("%15",), marker="Record-marker")
    backend = FakePiBackend([_screen("screen-idle.txt")])
    backend.marker_pane = "%99"
    backend.alive_panes = {PANE, "%99", "%15"}
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "live pane %15 is not binding pane %99" in err


def test_record_from_a_different_launch_refuses(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path, launch=False)
    _write_launch(Path.home(), "launch-2", project, panes=(PANE,))
    backend = FakePiBackend([_screen("screen-idle.txt")])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "is not binding launch launch-1" in err


def test_record_from_a_different_terminal_backend_refuses(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path, launch=False)
    _write_launch(Path.home(), "launch-1", project, panes=(PANE,), terminal="wezterm")
    backend = FakePiBackend([_screen("screen-idle.txt")])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "is not binding terminal tmux" in err


def test_stale_record_is_not_counted(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path, launch=False)
    _write_launch(Path.home(), "launch-1", project, panes=(PANE,), updated_at=int(time.time()) - 30 * 24 * 60 * 60)
    backend = FakePiBackend([_screen("screen-idle.txt")])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "no live Pi session for this project" in err


def test_dead_pane_record_is_not_counted(monkeypatch, capsys, tmp_path: Path) -> None:
    # The ratifier's repro: two dead records used to count as two live sessions.
    project = _project(tmp_path, launch=False)
    _write_launch(Path.home(), "launch-a", project, panes=("%15",))
    _write_launch(Path.home(), "launch-b", project, panes=("%16",))
    backend = FakePiBackend([_screen("screen-idle.txt")])
    backend.alive_panes = {PANE}
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "no live Pi session for this project" in err
    assert "2 live Pi sessions" not in err


@pytest.mark.parametrize("decoy", ["stale", "dead-pane"])
def test_one_live_session_survives_an_unqualified_record(monkeypatch, capsys, tmp_path: Path, decoy: str) -> None:
    project = _project(tmp_path)
    dead_pane = "%16"
    _write_launch(
        Path.home(), "launch-decoy", project, panes=(dead_pane if decoy == "dead-pane" else PANE,),
        updated_at=int(time.time()) - 30 * 24 * 60 * 60 if decoy == "stale" else None,
    )
    backend = FakePiBackend([_screen("screen-idle.txt")])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 0
    assert backend.sent == [(PANE, "/new")]
    assert err == ""


def test_live_pi_sessions_reports_only_qualified_entries(monkeypatch, tmp_path: Path) -> None:
    from pi_comm import live_pi_sessions

    project = _project(tmp_path)
    _write_launch(Path.home(), "launch-stale", project, panes=(PANE,), updated_at=int(time.time()) - 30 * 24 * 60 * 60)
    _write_launch(Path.home(), "launch-dead", project, panes=("%16",))
    backend = FakePiBackend([_screen("screen-idle.txt")])
    backend.alive_panes = {PANE}
    _use_backend(monkeypatch, backend)

    sessions, error = live_pi_sessions(compute_ccb_project_id(project))

    assert error == ""
    assert [(s.launch_id, s.pane_id, s.terminal) for s in sessions] == [("launch-1", PANE, "tmux")]
