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

    called: list[tuple[str, str | None, str | None, str]] = []

    def _start_provider(p: str, **_kwargs) -> str:
        pane_id = f"%{len(called) + 1}"
        called.append((p, _kwargs.get("parent_pane"), _kwargs.get("direction"), pane_id))
        return pane_id

    monkeypatch.setattr(launcher, "_start_provider", _start_provider)
    monkeypatch.setattr(launcher, "_warmup_provider", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(launcher, "_maybe_start_caskd", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "_start_claude", lambda: 0)
    monkeypatch.setattr(launcher, "_start_provider_in_current_pane", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(launcher, "cleanup", lambda: None)

    rc = launcher.run_up()
    assert rc == 0
    assert called == [
        ("gemini", "%0", "right", "%1"),
        ("opencode", "%1", "bottom", "%2"),
    ]


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

    started: list[tuple[str, str | None, str | None, str | None, str]] = []

    def _start_provider(provider: str, **kwargs) -> str:
        pane_id = f"%{len(started) + 1}"
        started.append((provider, kwargs.get("instance"), kwargs.get("parent_pane"), kwargs.get("direction"), pane_id))
        return pane_id

    def _start_claude_pane(**kwargs) -> str:
        pane_id = f"%{len(started) + 1}"
        started.append(("claude", kwargs.get("instance"), kwargs.get("parent_pane"), kwargs.get("direction"), pane_id))
        return pane_id

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
    assert started == [
        ("codex", None, "%0", "right", "%1"),
        ("claude", "worker", "%0", "bottom", "%2"),
        ("codex", "worker", "%1", "bottom", "%3"),
    ]


def test_run_up_groups_provider_instances_with_cmd_pane(monkeypatch, tmp_path: Path) -> None:
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
        cmd_config={"enabled": True, "start_cmd": "bash"},
    )
    launcher.terminal_type = "tmux"

    started: list[tuple[str, str | None, str | None, str | None, str]] = []

    def _next_pane() -> str:
        return f"%{len(started) + 1}"

    def _start_cmd_pane(**kwargs) -> str:
        pane_id = _next_pane()
        started.append(("cmd", None, kwargs.get("parent_pane"), kwargs.get("direction"), pane_id))
        return pane_id

    def _start_provider(provider: str, **kwargs) -> str:
        pane_id = _next_pane()
        started.append((provider, kwargs.get("instance"), kwargs.get("parent_pane"), kwargs.get("direction"), pane_id))
        return pane_id

    def _start_claude_pane(**kwargs) -> str:
        pane_id = _next_pane()
        started.append(("claude", kwargs.get("instance"), kwargs.get("parent_pane"), kwargs.get("direction"), pane_id))
        return pane_id

    monkeypatch.setattr(launcher, "_start_cmd_pane", _start_cmd_pane)
    monkeypatch.setattr(launcher, "_start_provider", _start_provider)
    monkeypatch.setattr(launcher, "_start_claude_pane", _start_claude_pane)
    monkeypatch.setattr(launcher, "_warmup_provider", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(launcher, "_maybe_start_caskd", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "_start_provider_in_current_pane", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(launcher, "cleanup", lambda: None)

    rc = launcher.run_up()

    assert rc == 0
    assert started == [
        ("cmd", None, "%0", "right", "%1"),
        ("claude", "worker", "%0", "bottom", "%2"),
        ("codex", None, "%1", "bottom", "%3"),
        ("codex", "worker", "%3", "bottom", "%4"),
    ]


def test_cli_parser_rejects_unsupported_provider_instance(capsys) -> None:
    ccb = _load_ccb_module()

    providers, instances, cmd_enabled = ccb._parse_provider_specs(["gemini:worker"])

    assert providers == []
    assert instances == []
    assert cmd_enabled is False
    assert "invalid provider" in capsys.readouterr().err


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


def test_codex_start_cmd_bare_main_is_policy_inert(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(parents=True, exist_ok=True)

    launcher = ccb.AILauncher(providers=["codex"])
    cmd = launcher._build_codex_start_cmd()

    assert cmd == "codex -c disable_paste_burst=true"
    assert "--model" not in cmd
    assert "model_reasoning_effort" not in cmd
    assert "sandbox_mode" not in cmd


def test_codex_start_cmd_applies_main_policy_when_codex_instance_configured(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(parents=True, exist_ok=True)

    launcher = ccb.AILauncher(providers=["codex"], provider_instances=[{"provider": "codex", "instance": "worker"}])
    cmd = launcher._build_codex_start_cmd()

    assert "--model gpt-5.5" in cmd
    assert "-c model_reasoning_effort='\"xhigh\"'" in cmd
    assert '-c sandbox_mode="workspace-write"' in cmd
    assert "danger-full-access" not in cmd


def test_codex_start_cmd_applies_worker_policy(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(parents=True, exist_ok=True)

    launcher = ccb.AILauncher(providers=["codex"], provider_instances=[{"provider": "codex", "instance": "worker"}])
    cmd = launcher._build_codex_start_cmd("worker")

    assert "--model gpt-5.4-mini" in cmd
    assert "-c model_reasoning_effort='\"medium\"'" in cmd
    assert '-c sandbox_mode="workspace-write"' in cmd


def test_codex_start_cmd_partial_override_keeps_effort(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(parents=True, exist_ok=True)

    launcher = ccb.AILauncher(
        providers=["codex"],
        provider_instances=[{"provider": "codex", "instance": "worker"}],
        instance_overrides={"codex:worker": {"model": "gpt-5.4"}},
    )
    cmd = launcher._build_codex_start_cmd("worker")

    assert "--model gpt-5.4" in cmd
    assert "-c model_reasoning_effort='\"medium\"'" in cmd


def test_codex_auto_keeps_danger_sandbox_and_skips_policy_sandbox(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(parents=True, exist_ok=True)

    launcher = ccb.AILauncher(providers=["codex"], provider_instances=[{"provider": "codex", "instance": "worker"}], auto=True)
    monkeypatch.setattr(launcher, "_ensure_codex_auto_approval", lambda: None)

    cmd = launcher._build_codex_start_cmd()

    assert "--model gpt-5.5" in cmd
    assert "-c model_reasoning_effort='\"xhigh\"'" in cmd
    assert '-c sandbox_mode="danger-full-access"' in cmd
    assert '-c sandbox_mode="workspace-write"' not in cmd


def test_codex_auto_bare_main_keeps_legacy_danger_without_policy(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(parents=True, exist_ok=True)

    launcher = ccb.AILauncher(providers=["codex"], auto=True)
    monkeypatch.setattr(launcher, "_ensure_codex_auto_approval", lambda: None)

    cmd = launcher._build_codex_start_cmd()

    assert "--model" not in cmd
    assert "model_reasoning_effort" not in cmd
    assert '-c sandbox_mode="danger-full-access"' in cmd
    assert '-c sandbox_mode="workspace-write"' not in cmd


def test_claude_worker_start_plan_applies_haiku_without_pinning_main(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(parents=True, exist_ok=True)

    launcher = ccb.AILauncher(providers=["claude"], provider_instances=[{"provider": "claude", "instance": "worker"}])
    monkeypatch.setattr(launcher, "_find_claude_cmd", lambda: "claude")

    main_cmd, _, _ = launcher._claude_start_plan()
    worker_cmd, _, _ = launcher._claude_start_plan("worker")

    assert main_cmd == ["claude"]
    assert worker_cmd == ["claude", "--model", "haiku"]


def _write_codex_resume_fixture(session_file: Path, log_file: Path, *, session_id: str, work_dir: Path, project_id: str) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": session_id, "cwd": str(work_dir)}}) + "\n",
        encoding="utf-8",
    )
    session_file.write_text(
        json.dumps(
            {
                "active": True,
                "work_dir": str(work_dir),
                "work_dir_norm": ccb_mod_normalize_path(str(work_dir)),
                "ccb_project_id": project_id,
                "codex_session_id": session_id,
                "codex_session_path": str(log_file),
            }
        ),
        encoding="utf-8",
    )


def ccb_mod_normalize_path(value: str) -> str:
    ccb = _load_ccb_module()
    return ccb._normalize_path_for_match(value)


def test_codex_worker_resume_uses_bound_instance_session(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / ".ccb"
    cfg.mkdir(parents=True, exist_ok=True)
    session_id = "11111111-1111-1111-1111-111111111111"

    launcher = ccb.AILauncher(providers=["codex"], provider_instances=[{"provider": "codex", "instance": "worker"}], resume=True)
    log_file = tmp_path / "codex-sessions" / "worker.jsonl"
    _write_codex_resume_fixture(
        cfg / ".codex-worker-session",
        log_file,
        session_id=session_id,
        work_dir=tmp_path.resolve(),
        project_id=launcher.project_id,
    )
    monkeypatch.setattr(launcher, "_get_latest_codex_session_id", lambda: (_ for _ in ()).throw(AssertionError("latest-by-cwd should not be used")))

    cmd = launcher._build_codex_start_cmd("worker")

    assert f"resume {session_id}" in cmd


def test_codex_worker_resume_rejects_project_mismatch(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / ".ccb"
    cfg.mkdir(parents=True, exist_ok=True)
    session_id = "22222222-2222-2222-2222-222222222222"

    launcher = ccb.AILauncher(providers=["codex"], provider_instances=[{"provider": "codex", "instance": "worker"}], resume=True)
    log_file = tmp_path / "codex-sessions" / "worker.jsonl"
    _write_codex_resume_fixture(
        cfg / ".codex-worker-session",
        log_file,
        session_id=session_id,
        work_dir=tmp_path.resolve(),
        project_id="other-project",
    )

    cmd = launcher._build_codex_start_cmd("worker")

    assert " resume " not in cmd


def test_codex_worker_resume_starts_fresh_when_bound_id_missing(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / ".ccb"
    cfg.mkdir(parents=True, exist_ok=True)
    session_id = "33333333-3333-3333-3333-333333333333"

    launcher = ccb.AILauncher(providers=["codex"], provider_instances=[{"provider": "codex", "instance": "worker"}], resume=True)
    (cfg / ".codex-worker-session").write_text(
        json.dumps(
            {
                "active": True,
                "work_dir": str(tmp_path.resolve()),
                "work_dir_norm": ccb._normalize_path_for_match(str(tmp_path.resolve())),
                "ccb_project_id": launcher.project_id,
                "codex_session_id": session_id,
                "codex_session_path": str(tmp_path / "missing.jsonl"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_SESSION_ROOT", str(tmp_path / "empty-sessions"))

    cmd = launcher._build_codex_start_cmd("worker")

    assert " resume " not in cmd


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
