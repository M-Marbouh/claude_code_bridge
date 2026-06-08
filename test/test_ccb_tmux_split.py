from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace


def _load_ccb_module() -> object:
    repo_root = Path(__file__).resolve().parents[1]
    ccb_path = repo_root / "ccb"
    loader = SourceFileLoader("ccb_script", str(ccb_path))
    spec = importlib.util.spec_from_loader("ccb_script", loader)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_run_up_sorts_providers_in_tmux(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TMUX_PANE", "%0")
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")

    launcher = ccb.AILauncher(providers=["opencode", "gemini", "codex"])
    launcher.terminal_type = "tmux"

    called: list[str] = []

    def _start_provider(p: str, **_kwargs) -> str:
        called.append(p)
        return f"%{len(called)}"

    monkeypatch.setattr(launcher, "_start_provider", _start_provider)
    monkeypatch.setattr(launcher, "_warmup_provider", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(launcher, "_maybe_start_caskd", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "_start_claude", lambda: 0)
    monkeypatch.setattr(launcher, "_start_provider_in_current_pane", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(launcher, "cleanup", lambda: None)

    rc = launcher.run_up()
    assert rc == 0
    assert called == ["gemini", "opencode"]


def test_run_up_spawns_provider_instances_without_polluting_providers(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TMUX_PANE", "%0")
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")

    launcher = ccb.AILauncher(
        providers=["codex", "claude"],
        provider_instances=[
            {"provider": "codex", "instance": "worker", "key": "codex:worker"},
            {"provider": "claude", "instance": "worker", "key": "claude:worker"},
        ],
    )
    launcher.terminal_type = "tmux"

    started: list[tuple[str, str | None]] = []

    def _start_provider(provider: str, **kwargs) -> str:
        started.append((provider, kwargs.get("instance")))
        return f"%{len(started)}"

    def _start_claude_pane(**kwargs) -> str:
        started.append(("claude", kwargs.get("instance")))
        return f"%{len(started)}"

    monkeypatch.setattr(launcher, "_start_provider", _start_provider)
    monkeypatch.setattr(launcher, "_start_claude_pane", _start_claude_pane)
    monkeypatch.setattr(launcher, "_warmup_provider", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(launcher, "_maybe_start_caskd", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "_start_provider_in_current_pane", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(launcher, "cleanup", lambda: None)

    rc = launcher.run_up()

    assert rc == 0
    assert launcher.providers == ["codex", "claude"]
    assert launcher.anchor_provider == "claude"
    assert started == [("codex", None), ("codex", "worker"), ("claude", "worker")]


def test_start_codex_tmux_writes_bridge_pid(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TMUX_PANE", "%0")

    # Ensure runtime dir lands under tmp_path.
    monkeypatch.setattr(ccb.tempfile, "gettempdir", lambda: str(tmp_path))

    # Avoid creating real FIFOs in unit tests.
    monkeypatch.setattr(ccb.os, "mkfifo", lambda p, _mode=0o600: Path(p).write_text("", encoding="utf-8"))

    # Fake tmux backend methods (no real tmux dependency).
    class _FakeTmuxBackend:
        def __init__(self, *args, **kwargs):
            self._created = 0

        def create_pane(
            self,
            cmd: str,
            cwd: str,
            direction: str = "right",
            percent: int = 50,
            parent_pane: str | None = None,
        ) -> str:
            self._created += 1
            return f"%{10 + self._created}"

        def set_pane_title(self, pane_id: str, title: str) -> None:
            return None

        def set_pane_user_option(self, pane_id: str, name: str, value: str) -> None:
            return None

        def respawn_pane(
            self,
            pane_id: str,
            *,
            cmd: str,
            cwd: str | None = None,
            stderr_log_path: str | None = None,
            remain_on_exit: bool = True,
        ) -> None:
            return None

    monkeypatch.setattr(ccb, "TmuxBackend", _FakeTmuxBackend)

    # Fake `tmux display-message ... #{pane_pid}`.
    def _fake_run(argv, *args, **kwargs):
        if argv[:3] == ["tmux", "display-message", "-p"] and "#{pane_pid}" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="12345\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(ccb.subprocess, "run", _fake_run)

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            self.pid = 999

    monkeypatch.setattr(ccb.subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))

    launcher = ccb.AILauncher(providers=["codex"])
    launcher.terminal_type = "tmux"

    pane_id = launcher._start_codex_tmux()
    assert pane_id is not None

    runtime = Path(launcher.runtime_dir) / "codex"
    assert (runtime / "bridge.pid").exists()
    assert (runtime / "bridge.pid").read_text(encoding="utf-8").strip() == "999"


def test_start_codex_tmux_worker_uses_instance_resources(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TMUX_PANE", "%0")
    monkeypatch.setattr(ccb.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ccb.os, "mkfifo", lambda p, _mode=0o600: Path(p).write_text("", encoding="utf-8"))

    class _FakeTmuxBackend:
        def create_pane(self, *args, **kwargs) -> str:
            return "%42"

        def respawn_pane(self, *args, **kwargs) -> None:
            return None

        def set_pane_title(self, *args, **kwargs) -> None:
            return None

        def set_pane_user_option(self, *args, **kwargs) -> None:
            return None

    monkeypatch.setattr(ccb, "TmuxBackend", _FakeTmuxBackend)
    monkeypatch.setattr(
        ccb.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="12345\n", stderr=""),
    )

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            self.pid = 999

    monkeypatch.setattr(ccb.subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))

    launcher = ccb.AILauncher(providers=["codex"], provider_instances=[{"provider": "codex", "instance": "worker"}])
    launcher.terminal_type = "tmux"

    pane_id = launcher._start_codex_tmux(instance="worker")

    assert pane_id == "%42"
    assert launcher.tmux_panes["codex:worker"] == "%42"
    runtime = Path(launcher.runtime_dir) / "codex-worker"
    assert (runtime / "bridge.pid").read_text(encoding="utf-8").strip() == "999"
    session_file = tmp_path / ".ccb" / ".codex-worker-session"
    data = json.loads(session_file.read_text(encoding="utf-8"))
    assert data["instance"] == "worker"
    assert data["qualified_provider"] == "codex:worker"
    assert data["runtime_dir"] == str(runtime)
    assert data["pane_title_marker"].startswith("CCB-Codex-worker-")


def test_start_claude_worker_pane_uses_instance_resources(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TMUX_PANE", "%0")
    monkeypatch.setattr(ccb.tempfile, "gettempdir", lambda: str(tmp_path))

    class _FakeTmuxBackend:
        def create_pane(self, *args, **kwargs) -> str:
            return "%77"

        def respawn_pane(self, *args, **kwargs) -> None:
            return None

        def set_pane_title(self, *args, **kwargs) -> None:
            return None

        def set_pane_user_option(self, *args, **kwargs) -> None:
            return None

    monkeypatch.setattr(ccb, "TmuxBackend", _FakeTmuxBackend)

    launcher = ccb.AILauncher(providers=["claude"], provider_instances=[{"provider": "claude", "instance": "worker"}])
    launcher.terminal_type = "tmux"
    monkeypatch.setattr(launcher, "_claude_start_plan", lambda instance=None: (["claude"], str(tmp_path), False))
    monkeypatch.setattr(launcher, "_claude_env_overrides", lambda: {})

    pane_id = launcher._start_claude_pane(parent_pane="%0", direction="right", instance="worker")

    assert pane_id == "%77"
    assert launcher.tmux_panes["claude:worker"] == "%77"
    session_file = tmp_path / ".ccb" / ".claude-worker-session"
    data = json.loads(session_file.read_text(encoding="utf-8"))
    assert data["instance"] == "worker"
    assert data["qualified_provider"] == "claude:worker"
    assert data["pane_title_marker"].startswith("CCB-Claude-worker-")


def test_run_up_backfills_existing_claude_session_work_dir_fields(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / ".ccb"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    session_file = cfg_dir / ".claude-session"
    session_file.write_text(json.dumps({"active": True}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("TMUX_PANE", "%0")
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")

    launcher = ccb.AILauncher(providers=["codex"])
    launcher.terminal_type = "tmux"

    monkeypatch.setattr(launcher, "_start_provider_in_current_pane", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(launcher, "cleanup", lambda: None)

    rc = launcher.run_up()
    assert rc == 0

    data = json.loads(session_file.read_text(encoding="utf-8"))
    assert data.get("work_dir") == str(tmp_path.resolve())
    assert data.get("work_dir_norm")
