from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from project_id import compute_ccb_project_id


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


def _peer_env(tmp_path: Path, caller: str = "claude") -> dict[str, str]:
    home = tmp_path / "home"
    registry_dir = home / ".ccb" / "run"
    registry_dir.mkdir(parents=True)
    pane_id = "%7"
    record = {
        "ccb_session_id": f"ai-test-{os.getpid()}",
        "ccb_project_id": compute_ccb_project_id(tmp_path),
        "ccb_pid": os.getpid(),
        "work_dir": str(tmp_path),
        "updated_at": int(time.time()),
        "providers": {caller: {"pane_id": pane_id}},
    }
    (registry_dir / "ccb-session-test.json").write_text(json.dumps(record), encoding="utf-8")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CCB_CALLER"] = caller
    env["CCB_WORK_DIR"] = str(tmp_path)
    env["CCB_RUN_DIR"] = str(tmp_path / "run")
    env["CCB_CALLER_PANE_ID"] = pane_id
    env["CCB_CALLER_TERMINAL"] = "tmux"
    return env


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
    env = _peer_env(tmp_path)

    proc = _run_ask(["--peer", "abcd", "hello"], cwd=tmp_path, env=env)

    assert proc.returncode == 0
    assert "[CCB_ASYNC_SUBMITTED provider=peer-claude intent=wait]" in proc.stdout
    assert "[CCB_ASYNC_PID task=" in proc.stdout
    assert "[CCB_ASYNC_STATUS_FILE task=" in proc.stdout
    assert "[CCB_ASYNC_LOG_FILE task=" in proc.stdout
    assert "MANDATORY: END YOUR TURN NOW. Reply ONLY 'Peer Claude processing...', then stop." in proc.stdout


def test_peer_background_mode_does_not_emit_wait_guardrail(tmp_path: Path) -> None:
    env = _peer_env(tmp_path)

    proc = _run_ask(["--peer", "abcd", "--background", "hello"], cwd=tmp_path, env=env)

    assert proc.returncode == 0
    assert "[CCB_BACKGROUND_SUBMITTED provider=peer-claude intent=background]" in proc.stdout
    assert "continue current work" in proc.stdout
    assert "[CCB_ASYNC_SUBMITTED" not in proc.stdout
    assert "MANDATORY: END YOUR TURN NOW" not in proc.stdout


def test_provider_peer_form_honors_background_intent(tmp_path: Path) -> None:
    env = _peer_env(tmp_path)

    proc = _run_ask(["claude", "--peer", "abcd", "--background", "hello"], cwd=tmp_path, env=env)

    assert proc.returncode == 0
    assert "[CCB_BACKGROUND_SUBMITTED provider=peer-claude intent=background]" in proc.stdout
    assert "[CCB_ASYNC_SUBMITTED" not in proc.stdout


def test_codex_peer_wait_uses_provider_specific_markers(tmp_path: Path) -> None:
    env = _peer_env(tmp_path)

    proc = _run_ask(["codex", "--peer", "abcd", "--wait", "hello"], cwd=tmp_path, env=env)

    assert proc.returncode == 0
    assert "[CCB_ASYNC_SUBMITTED provider=peer-codex intent=wait]" in proc.stdout
    assert "Reply ONLY 'Peer Codex processing...'" in proc.stdout


def test_codex_peer_notify_is_non_blocking_without_wait_guardrail(tmp_path: Path) -> None:
    env = _peer_env(tmp_path)

    proc = _run_ask(["codex", "--peer", "abcd", "--notify", "FYI"], cwd=tmp_path, env=env)

    assert proc.returncode == 0
    assert "[CCB_NOTIFY_SUBMITTED provider=peer-codex intent=notify]" in proc.stdout
    assert "MANDATORY: END YOUR TURN NOW" not in proc.stdout


def test_peer_mode_foreground_blocks_without_async_markers(tmp_path: Path) -> None:
    env = _peer_env(tmp_path)

    proc = _run_ask(["--peer", "abcd", "--foreground", "hello"], cwd=tmp_path, env=env)

    assert proc.returncode == 1
    assert "no active CCB project matches target: abcd" in proc.stderr
    assert "[CCB_ASYNC_SUBMITTED" not in proc.stdout


# --- Pi as a local caller -------------------------------------------------------

import importlib.machinery
import importlib.util


def _load_ask_module():
    path = _repo_root() / "bin" / "ask"
    loader = importlib.machinery.SourceFileLoader("ask_cli_pi_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_pi_is_a_valid_local_caller(tmp_path: Path, monkeypatch) -> None:
    ask = _load_ask_module()

    assert ask.CALLER_SESSION_FILES["pi"] == ".pi-session"
    assert "pi" in ask.VALID_CALLERS
    monkeypatch.setenv("CCB_CALLER", "pi")
    assert ask._detect_caller() == "pi"


def test_pi_caller_is_inferred_from_its_own_session_file(tmp_path: Path, monkeypatch) -> None:
    ask = _load_ask_module()
    project = tmp_path / "project"
    config = project / ".ccb"
    config.mkdir(parents=True)
    (config / ".pi-session").write_text(json.dumps({"pane_id": "%21"}), encoding="utf-8")
    (config / ".claude-session").write_text(json.dumps({"pane_id": "%11"}), encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.delenv("CCB_CALLER", raising=False)
    monkeypatch.setenv("TMUX_PANE", "%21")

    assert ask._infer_caller_from_pane() == "pi"


PI_PEER_ARGVS = (
    ["--peer", "abcd", "hello"],
    ["--peer", "abcd", "--notify", "hello"],
    ["claude", "--peer", "abcd", "hello"],
    ["claude", "--peer", "abcd", "--notify", "hello"],
)


def test_a_pi_caller_reaches_the_shared_bridge_for_every_peer_syntax(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Pi is a local peer sender like Claude and Codex: no refusal at the Pi boundary."""
    ask = _load_ask_module()
    monkeypatch.setenv("CCB_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(ask, "_detect_caller", lambda: "pi")
    dispatched: list[str] = []
    monkeypatch.setattr(ask, "_resolve_sender_work_dir", lambda _caller: tmp_path)
    monkeypatch.setattr(
        ask, "_run_peer_bridge_foreground", lambda *a, **k: dispatched.append("foreground") or 0
    )
    monkeypatch.setattr(
        ask, "_run_peer_bridge_background", lambda *a, **k: dispatched.append("background") or 0
    )

    for argv in PI_PEER_ARGVS:
        assert ask.main(["ask", *argv]) == 0
    assert dispatched
    assert "not available from a Pi session" not in capsys.readouterr().err


@pytest.mark.parametrize("argv", PI_PEER_ARGVS)
def test_a_pi_peer_request_no_longer_stops_at_a_pi_refusal(tmp_path: Path, argv) -> None:
    env = _peer_env(tmp_path, caller="pi")

    proc = _run_ask(list(argv), cwd=tmp_path, env=env)

    # The target does not exist here, so the run still fails -- but on its own merits, never
    # because the caller is Pi.
    assert proc.returncode == 1
    assert "not available from a Pi session" not in proc.stderr
    assert "[CCB_ASYNC_SUBMITTED" not in proc.stdout
    assert "[CCB_BACKGROUND_SUBMITTED" not in proc.stdout


def test_a_pi_caller_records_its_return_pane_and_launch_for_a_peer_task(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The return address a Pi sender records is the same shape Claude/Codex record."""
    ask = _load_ask_module()
    monkeypatch.setenv("CCB_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("CCB_CALLER", "pi")
    monkeypatch.setattr(ask, "_detect_caller", lambda: "pi")
    monkeypatch.setattr(ask, "_resolve_sender_work_dir", lambda _caller: tmp_path)
    monkeypatch.setattr(ask, "_preflight_delivery", lambda *a, **k: (True, ""))
    monkeypatch.setattr(ask, "_peer_caller_pane_info", lambda: ("%21", "tmux"))
    captured: dict = {}

    def prepare(peer_provider, message, caller, intent, sender_work_dir, timeout,
                caller_pane_id, caller_terminal, **kwargs):
        captured.update(
            provider=peer_provider, caller=caller, pane=caller_pane_id, terminal=caller_terminal,
        )
        raise SystemExit(0)

    monkeypatch.setattr(ask, "_prepare_peer_task", prepare)
    with pytest.raises(SystemExit):
        ask.main(["ask", "pi", "--peer", str(tmp_path), "hello"])
    assert captured == {"provider": "pi", "caller": "pi", "pane": "%21", "terminal": "tmux"}


def test_bare_peer_still_selects_claude(tmp_path: Path, monkeypatch) -> None:
    """A2: adding Pi to the peer providers must not move the DEFAULT destination."""
    ask = _load_ask_module()
    monkeypatch.setenv("CCB_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(ask, "_detect_caller", lambda: "claude")
    seen: dict = {}
    monkeypatch.setattr(
        ask, "_run_peer_bridge", lambda *a, **k: (seen.setdefault("provider", a[1]), 0)[1]
    )

    assert ask.main(["ask", "--peer", "abcd", "hello"]) == 0
    assert seen["provider"] == "claude"
    assert "pi" in ask.PEER_PROVIDERS


@pytest.mark.parametrize("caller", ["gemini", "opencode", "hermes"])
@pytest.mark.parametrize(
    "argv",
    (
        ["--peer", "abcd", "--notify", "hello"],
        ["claude", "--peer", "abcd", "--notify", "hello"],
    ),
)
def test_other_providers_still_reach_peer_dispatch_for_both_syntaxes(
    tmp_path: Path, monkeypatch, caller: str, argv
) -> None:
    """Behaviour these callers had before Pi could call at all is preserved."""
    ask = _load_ask_module()
    monkeypatch.setenv("CCB_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(ask, "_detect_caller", lambda: caller)
    dispatched: dict = {}
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge_foreground",
        lambda *a, **k: dispatched.setdefault("foreground", (a, k)) and 0,
    )
    monkeypatch.setattr(
        ask,
        "_run_peer_bridge_background",
        lambda *a, **k: dispatched.setdefault("background", (a, k)) and 0,
    )
    monkeypatch.setattr(
        ask, "_prepare_peer_task", lambda *a, **k: pytest.fail("no receipt for --notify")
    )

    assert ask.main(["ask", *argv]) == 0

    entry = dispatched.get("foreground") or dispatched.get("background")
    assert entry is not None, "peer dispatch was not reached"
    assert caller in repr(entry)


# --- F2: Pi peer messaging is local only ----------------------------------------


def test_a_pi_caller_is_refused_a_cross_machine_target_before_any_dispatch(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    ask = _load_ask_module()
    monkeypatch.setenv("CCB_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(ask, "_detect_caller", lambda: "pi")
    for name in ("_run_remote_peer", "_run_peer_bridge_foreground", "_run_peer_bridge_background",
                 "_prepare_peer_task", "_peer_bridge_cmd"):
        monkeypatch.setattr(
            ask, name, lambda *a, _name=name, **k: pytest.fail(f"{_name} must not run")
        )

    for argv in (
        ["ask", "claude", "--peer", "host::demo", "hello"],
        ["ask", "claude", "--peer", "host::demo", "--background", "hello"],
        ["ask", "pi", "--peer", "host::demo", "hello"],
    ):
        assert ask.main(argv) == 1
        assert "not available from a Pi session" in capsys.readouterr().err

    receipts = tmp_path / "ccb-tasks"
    assert not receipts.exists() or not list(receipts.rglob("*"))


def test_a_pi_destination_provider_is_refused_for_a_cross_machine_target(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`ask pi --peer host::project`: refused before any remote submit or receipt."""
    ask = _load_ask_module()
    monkeypatch.setenv("CCB_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(ask, "_detect_caller", lambda: "claude")

    for name in ("submit", "register_watch", "wait_for_answer", "capture_caller_identity"):
        monkeypatch.setattr(
            f"remote_client.{name}",
            lambda *a, _name=name, **k: pytest.fail(f"{_name} must not run"),
        )

    for argv in (
        ["ask", "pi", "--peer", "host::demo", "hello"],
        ["ask", "pi", "--peer", "host::demo", "--notify", "hello"],
    ):
        assert ask.main(argv) == 1
        assert "supports claude and codex only, not pi" in capsys.readouterr().err

    receipts = tmp_path / "ccb-tasks"
    assert not receipts.exists() or not list(receipts.rglob("*"))
