# This repo puts lib/ on sys.path at runtime (test/conftest.py) and ships no pyright
# configuration, so the workspace-rooted analyzer cannot see these flat imports.
# pyright: reportMissingImports=false
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from pi_comm import PiState
from project_id import compute_ccb_project_id

PANE = "%77"
MARKER = "Pi-autonew-marker"
SESSION_ID = "2c8ab895-d0ab-49c6-86c1-0f7ff9fb6e8e"

# Pi's own readiness report, as CCB's Pi extension publishes it. These tests script the report in
# place of the old screen captures: the signal is swapped, each test's decision is not.
READY = PiState("ready")
BUSY = PiState("busy", "Pi is working on a request")
DRAFT = PiState("draft", "Pi has unsent text in its editor")
PROMPT = PiState("prompt", "Pi is waiting for a decision in its pane")
ABSENT = PiState("absent", "CCB's Pi extension is not reporting; restart CCB")


def _autonew():
    path = Path(__file__).resolve().parents[1] / "bin" / "autonew"
    loader = importlib.machinery.SourceFileLoader("autonew_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakePiBackend:
    """Pane backend whose Pi readiness reports come from a script (repeats the last state)."""

    def __init__(self, states: list[PiState] | None = None):
        self.states = list(states or [READY])
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

    def next_state(self) -> PiState:
        if not self.states:
            raise RuntimeError("no state scripted")
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

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

    def _state(*_args, **_kwargs) -> PiState:
        return backend.next_state()

    def _confirm(*_args, **_kwargs) -> PiState:
        # Mirrors the production rule: a ready first read needs a second, also-ready read.
        first = backend.next_state()
        return first if first.status != "ready" else backend.next_state()

    monkeypatch.setattr("pi_comm.read_pi_state", _state)
    monkeypatch.setattr("pi_comm.confirm_pi_ready", _confirm)


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
    backend = FakePiBackend([READY])
    _use_backend(monkeypatch, backend)
    binding = project / ".ccb" / ".pi-session"
    before = hashlib.sha256(binding.read_bytes()).hexdigest()

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 0
    assert backend.sent == [(PANE, "/new")]
    assert "Sent /new to pi" in out
    assert err == ""
    assert hashlib.sha256(binding.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("state", [BUSY, PROMPT, DRAFT], ids=lambda s: s.status)
def test_non_ready_pi_refuses_with_zero_sends(monkeypatch, capsys, tmp_path: Path, state: PiState) -> None:
    project = _project(tmp_path)
    backend = FakePiBackend([state])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert out == ""
    assert f"Pi pane is {state.status}" in err
    assert "refusing to send /new" in err
    assert len([line for line in err.splitlines() if line.strip()]) == 1


@pytest.mark.parametrize("second_read", [BUSY, PROMPT, DRAFT], ids=lambda s: s.status)
def test_a_second_read_that_is_not_ready_refuses(monkeypatch, capsys, tmp_path: Path, second_read: PiState) -> None:
    project = _project(tmp_path)
    backend = FakePiBackend([READY, second_read])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "refusing to send /new" in err


def test_a_missing_extension_report_refuses_with_zero_sends(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path)
    backend = FakePiBackend([ABSENT])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "Pi pane is absent: CCB's Pi extension is not reporting; restart CCB" in err
    assert "refusing to send /new" in err


def test_two_live_pi_sessions_in_one_launch_refuse(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path, launch=False)
    _write_launch(Path.home(), "launch-a", project, panes=(PANE, "%78"), markers=(MARKER, "Record-marker-1"))
    backend = FakePiBackend([READY])
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
    backend = FakePiBackend([READY])
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
    backend = FakePiBackend([READY])
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
    backend = FakePiBackend([READY])
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
    backend = FakePiBackend([BUSY])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project, provider="codex")

    assert code == 0
    assert backend.sent == [("%90", "/new")]
    assert "Sent /new to codex" in out


def test_no_live_pi_session_refuses(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path, launch=False)
    backend = FakePiBackend([READY])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "no live Pi session for this project" in err


def test_empty_inventory_refuses(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path, launch=False)
    _write_launch(Path.home(), "launch-1", project, panes=())
    backend = FakePiBackend([READY])
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
    backend = FakePiBackend([READY])
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
    backend = FakePiBackend([READY])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "is not binding launch launch-1" in err


def test_record_from_a_different_terminal_backend_refuses(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path, launch=False)
    _write_launch(Path.home(), "launch-1", project, panes=(PANE,), terminal="wezterm")
    backend = FakePiBackend([READY])
    _use_backend(monkeypatch, backend)

    code, out, err = _run(monkeypatch, capsys, project)

    assert code == 1
    assert backend.sent == []
    assert "is not binding terminal tmux" in err


def test_stale_record_is_not_counted(monkeypatch, capsys, tmp_path: Path) -> None:
    project = _project(tmp_path, launch=False)
    _write_launch(Path.home(), "launch-1", project, panes=(PANE,), updated_at=int(time.time()) - 30 * 24 * 60 * 60)
    backend = FakePiBackend([READY])
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
    backend = FakePiBackend([READY])
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
    backend = FakePiBackend([READY])
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
    backend = FakePiBackend([READY])
    backend.alive_panes = {PANE}
    _use_backend(monkeypatch, backend)

    sessions, error = live_pi_sessions(compute_ccb_project_id(project))

    assert error == ""
    assert [(s.launch_id, s.pane_id, s.terminal) for s in sessions] == [("launch-1", PANE, "tmux")]


@pytest.mark.parametrize("args", [["pi", "--help"], ["codex", "-h"], ["--help", "pi"]])
def test_help_anywhere_prints_usage_and_sends_nothing(monkeypatch, capsys, tmp_path: Path, args) -> None:
    project = _project(tmp_path)
    _write_launch(Path.home(), "launch-a", project, panes=(PANE,), inventory=False)
    backend = FakePiBackend([READY])
    _use_backend(monkeypatch, backend)
    monkeypatch.chdir(project)

    code = _autonew().main(["autonew", *args])

    assert code == 0
    assert backend.sent == []
    assert "Usage: autonew <provider>" in capsys.readouterr().err


@pytest.mark.parametrize("provider", ["pi", "codex"])
def test_extra_arguments_refuse_and_send_nothing(monkeypatch, capsys, tmp_path: Path, provider: str) -> None:
    project = _project(tmp_path)
    _write_launch(Path.home(), "launch-a", project, panes=(PANE,), inventory=False)
    backend = FakePiBackend([READY])
    _use_backend(monkeypatch, backend)
    monkeypatch.chdir(project)

    code = _autonew().main(["autonew", provider, "now"])

    assert code == 1
    assert backend.sent == []
    assert "Unexpected arguments: now" in capsys.readouterr().err
