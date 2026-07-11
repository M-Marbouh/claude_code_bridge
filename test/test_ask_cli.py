from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _run_ask(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    exe = sys.executable
    script_path = _repo_root() / "bin" / "ask"
    return subprocess.run(
        [exe, str(script_path), *args],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_async_mode_fails_fast_when_unified_daemon_unavailable(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["CCB_CALLER"] = "claude"
    env["CCB_UNIFIED_ASKD"] = "1"
    env["CCB_ASKD_AUTOSTART"] = "0"
    env["CCB_RUN_DIR"] = str(tmp_path / "run")

    proc = _run_ask(["gemini", "hello"], cwd=tmp_path, env=env)

    assert proc.returncode == 1
    assert "Unified askd daemon not running" in proc.stderr
    assert "[CCB_ASYNC_SUBMITTED" not in proc.stdout


def test_peer_mode_defaults_async_for_claude_caller(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["CCB_CALLER"] = "claude"
    env["CCB_RUN_DIR"] = str(tmp_path / "run")
    env["CCB_CALLER_PANE_ID"] = "%cnt"
    env["CCB_CALLER_TERMINAL"] = "tmux"

    proc = _run_ask(["--peer", "abcd", "hello"], cwd=tmp_path, env=env)

    assert proc.returncode == 0
    assert "[CCB_ASYNC_SUBMITTED provider=peer-claude intent=wait]" in proc.stdout
    assert "[CCB_ASYNC_PID task=" in proc.stdout
    assert "[CCB_ASYNC_STATUS_FILE task=" in proc.stdout
    assert "[CCB_ASYNC_LOG_FILE task=" in proc.stdout
    assert "MANDATORY: END YOUR TURN NOW. Reply ONLY 'Peer Claude processing...', then stop." in proc.stdout


def test_peer_background_mode_does_not_emit_wait_guardrail(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["CCB_CALLER"] = "claude"
    env["CCB_RUN_DIR"] = str(tmp_path / "run")
    env["CCB_CALLER_PANE_ID"] = "%cnt"
    env["CCB_CALLER_TERMINAL"] = "tmux"

    proc = _run_ask(["--peer", "abcd", "--background", "hello"], cwd=tmp_path, env=env)

    assert proc.returncode == 0
    assert "[CCB_BACKGROUND_SUBMITTED provider=peer-claude intent=background]" in proc.stdout
    assert "continue current work" in proc.stdout
    assert "[CCB_ASYNC_SUBMITTED" not in proc.stdout
    assert "MANDATORY: END YOUR TURN NOW" not in proc.stdout


def test_provider_peer_form_honors_background_intent(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["CCB_CALLER"] = "claude"
    env["CCB_RUN_DIR"] = str(tmp_path / "run")

    proc = _run_ask(["claude", "--peer", "abcd", "--background", "hello"], cwd=tmp_path, env=env)

    assert proc.returncode == 0
    assert "[CCB_BACKGROUND_SUBMITTED provider=peer-claude intent=background]" in proc.stdout
    assert "[CCB_ASYNC_SUBMITTED" not in proc.stdout


def test_peer_mode_foreground_blocks_without_async_markers(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["CCB_CALLER"] = "claude"
    env["CCB_RUN_DIR"] = str(tmp_path / "run")
    env["CCB_CALLER_PANE_ID"] = "%cnt"
    env["CCB_CALLER_TERMINAL"] = "tmux"

    proc = _run_ask(["--peer", "abcd", "--foreground", "hello"], cwd=tmp_path, env=env)

    assert proc.returncode == 1
    assert "no active CCB project matches target: abcd" in proc.stderr
    assert "[CCB_ASYNC_SUBMITTED" not in proc.stdout
