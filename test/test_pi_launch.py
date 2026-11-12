"""The CCB side of Pi readiness: every Pi launch carries its own state channel.

The reader in `pi_comm` trusts a snapshot only when it names the token of the launch that started
the pane, so the pane's environment and the `.pi-session` binding must agree on exactly one
(state file, token) pair, and the launch command must load the extension that publishes it.
"""
# This repo puts lib/ on sys.path at runtime (test/conftest.py) and ships no pyright
# configuration, so the workspace-rooted analyzer cannot see these flat imports.
# pyright: reportMissingImports=false
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

EXTENSION = REPO / "lib" / "pi_extension" / "ccb-state.ts"


def _ccb_module():
    loader = SourceFileLoader("ccb_script_pi_launch", str(REPO / "ccb"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _launcher(tmp_path: Path, *, providers: tuple[str, ...] = ("pi",)):
    """A launcher with only the state this wiring touches (no registry, no terminal)."""
    launcher = object.__new__(_ccb_module().AILauncher)
    launcher.script_dir = REPO
    launcher.runtime_dir = tmp_path / "rt"
    launcher.project_root = tmp_path
    launcher.invocation_dir = tmp_path
    launcher.project_id = "abc12345"
    launcher.providers = list(providers)
    launcher._live_slots = []
    launcher._launch_slot = 0
    launcher.launch_env = {}
    launcher.launch_args = {}
    launcher.ccb_pid = os.getpid()
    launcher.session_id = "ai-test-launch"
    launcher.terminal_type = "tmux"
    launcher._member_credential = lambda _provider: ("", "")
    # Nothing in this file starts a daemon or prunes records; the binding write is the subject.
    launcher._askd_checked = True
    launcher._auto_prune_done = True
    launcher._pi_binding = lambda: ("pi-session-1", tmp_path / "sessions")
    (tmp_path / ".ccb").mkdir(exist_ok=True)
    return launcher


def test_the_pi_pane_environment_carries_one_state_channel_per_launch(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    env = launcher._provider_env_overrides("pi")

    state_file = Path(env["CCB_PI_STATE_FILE"])
    assert state_file == tmp_path / "rt" / "pi" / "pi-state.json"
    assert len(env["CCB_PI_LAUNCH_TOKEN"]) == 32
    assert stat.S_IMODE(state_file.parent.stat().st_mode) == 0o700
    # The pane env is rebuilt for every pane: the same launch must keep one token, because the
    # binding is written separately and has to match what this Pi was started with.
    again = launcher._provider_env_overrides("pi")
    assert again["CCB_PI_LAUNCH_TOKEN"] == env["CCB_PI_LAUNCH_TOKEN"]
    assert again["CCB_PI_STATE_FILE"] == env["CCB_PI_STATE_FILE"]


def test_a_new_launch_gets_its_own_token_and_drops_the_previous_snapshot(tmp_path: Path) -> None:
    first = _launcher(tmp_path)._provider_env_overrides("pi")
    state_file = Path(first["CCB_PI_STATE_FILE"])
    state_file.write_text('{"v": 1}\n', encoding="utf-8")

    second = _launcher(tmp_path)._provider_env_overrides("pi")

    assert second["CCB_PI_LAUNCH_TOKEN"] != first["CCB_PI_LAUNCH_TOKEN"]
    # A snapshot from the previous launch must never be readable as this launch's readiness.
    assert not state_file.exists()


def test_other_providers_get_no_pi_state_channel(tmp_path: Path) -> None:
    env = _launcher(tmp_path, providers=("codex",))._provider_env_overrides("codex")
    assert "CCB_PI_STATE_FILE" not in env
    assert "CCB_PI_LAUNCH_TOKEN" not in env


def test_the_pi_start_command_loads_the_shipped_extension(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    command = launcher._get_start_cmd("pi")

    assert command.agent_command.startswith("pi --no-approve --session-id pi-session-1")
    assert f"--extension {EXTENSION}" in command.agent_command
    assert EXTENSION.is_file()


def test_a_missing_extension_still_starts_pi_but_warns(tmp_path: Path, capsys) -> None:
    launcher = _launcher(tmp_path)
    launcher.script_dir = tmp_path / "empty-install"
    command = launcher._get_start_cmd("pi")

    # Launching without the extension is not fatal: the reader refuses that pane instead.
    assert "--extension" not in command.agent_command
    assert "Pi state extension missing" in capsys.readouterr().err


def test_the_binding_records_the_same_channel_its_pane_was_given(tmp_path: Path, monkeypatch) -> None:
    ccb = _ccb_module()
    launcher = _launcher(tmp_path)
    monkeypatch.setattr(ccb, "upsert_registry", lambda _record: True)
    env = launcher._provider_env_overrides("pi")

    assert launcher._write_pi_session(tmp_path / "rt" / "pi", pane_id="%7", pane_title_marker="CCB-Pi-abc12345") is True

    data = json.loads((tmp_path / ".ccb" / ".pi-session").read_text(encoding="utf-8"))
    assert data["pi_launch_token"] == env["CCB_PI_LAUNCH_TOKEN"]
    assert data["pi_state_file"] == env["CCB_PI_STATE_FILE"]
    assert data["pi_session_id"] == "pi-session-1"
    assert data["pane_id"] == "%7"
