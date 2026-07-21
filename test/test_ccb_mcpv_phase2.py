from __future__ import annotations

import importlib.util
import json
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

import caskd_session
import ccb_mcpv_bridge
import ccb_runtime_status
from ccb_mcpv_bridge import Decision, DecisionKind
from session_utils import remediate_ccb_permissions, safe_write_session


REPO_ROOT = Path(__file__).resolve().parents[1]
CCB_PATH = REPO_ROOT / "ccb"


def _load_ccb_module() -> object:
    loader = SourceFileLoader("ccb_mcpv_phase2_test", str(CCB_PATH))
    spec = importlib.util.spec_from_loader("ccb_mcpv_phase2_test", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _launcher(ccb, monkeypatch: pytest.MonkeyPatch, root: Path):
    monkeypatch.chdir(root)
    (root / ".ccb").mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")
    return ccb.AILauncher(providers=["codex"])


def _install_synthetic_policy(home: Path, source: str, *, mode: int = 0o600) -> Path:
    root = home / ".ccb"
    local = root / "local"
    root.mkdir(mode=0o700)
    local.mkdir(mode=0o700)
    path = local / "ccb_mcpv_local.py"
    path.write_text(source, encoding="utf-8")
    path.chmod(mode)
    return path


def _vault_manifest(root: Path, key: str = "phase2") -> dict:
    return {
        "version": 1,
        "projects": {
            key: {
                "root": str(root),
                "mcps": {
                    "example": {
                        "agents": ["claude", "codex"],
                        "args": [],
                        "command": "example",
                        "env": {"TOKEN": {"vault_key": "token"}},
                        "transport": "stdio",
                    }
                },
            }
        },
        "expected_matrix": {key: {"example": ["claude", "codex"]}},
    }


def test_bridge_identity_only_for_true_absence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ccb_mcpv_bridge.reset_policy_cache()
    decision = ccb_mcpv_bridge.decide("codex", tmp_path, True, "codex")
    assert decision.kind is DecisionKind.PLAIN
    assert decision.policy_present is False


@pytest.mark.parametrize(
    ("source", "mode", "reason"),
    [
        ("this is not python", 0o600, "policy_import_failed"),
        (
            "from ccb_mcpv_bridge import Decision\ndef decide_wrap(**kwargs): return Decision.plain(policy_present=True)\n",
            0o622,
            "policy_insecure",
        ),
        ("import module_that_does_not_exist\n", 0o600, "policy_import_failed"),
        ("raise SystemExit(1)\n", 0o600, "policy_import_failed"),
    ],
)
def test_bridge_fails_loud_for_broken_or_insecure_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    mode: int,
    reason: str,
) -> None:
    _install_synthetic_policy(tmp_path, source, mode=mode)
    monkeypatch.setenv("HOME", str(tmp_path))
    ccb_mcpv_bridge.reset_policy_cache()
    decision = ccb_mcpv_bridge.decide("codex", tmp_path, True, "codex")
    assert decision.kind is DecisionKind.ERROR
    assert decision.reason_code == reason


def test_bridge_rejects_insecure_policy_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _install_synthetic_policy(
        tmp_path,
        "from ccb_mcpv_bridge import Decision\n"
        "def decide_wrap(**kwargs): return Decision.plain(policy_present=True)\n",
    )
    path.parent.chmod(0o722)
    monkeypatch.setenv("HOME", str(tmp_path))
    ccb_mcpv_bridge.reset_policy_cache()
    decision = ccb_mcpv_bridge.decide("codex", tmp_path, True, "codex")
    assert decision.kind is DecisionKind.ERROR
    assert decision.reason_code == "policy_insecure"


def test_bridge_renders_shell_and_argv_from_one_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "from ccb_mcpv_bridge import Decision\n"
        "def decide_wrap(**kwargs): return Decision.wrap(('/opt/mcp ctl', 'run', 'key', '--'))\n"
    )
    _install_synthetic_policy(tmp_path, source)
    monkeypatch.setenv("HOME", str(tmp_path))
    ccb_mcpv_bridge.reset_policy_cache()
    decision = ccb_mcpv_bridge.decide("codex", tmp_path, True, "codex")
    assert decision.argv_prefix == ("/opt/mcp ctl", "run", "key", "--")
    assert ccb_mcpv_bridge.render_shell_prefix(decision, shell_type="posix") == (
        "'/opt/mcp ctl' run key -- "
    )
    assert ccb_mcpv_bridge.render_shell_prefix(decision, shell_type="powershell") == (
        "& '/opt/mcp ctl' 'run' 'key' '--' "
    )


def test_launcher_provider_gate_and_explicit_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ccb = _load_ccb_module()
    launcher = _launcher(ccb, monkeypatch, tmp_path)
    calls: list[tuple[str, str, bool, str]] = []

    def _decide(provider, project_root, managed, caller):
        calls.append((provider, str(project_root), managed, caller))
        return Decision.wrap(("mcpctl", "run", "key", "--"))

    monkeypatch.setattr(ccb, "decide_mcpv", _decide)
    monkeypatch.setenv("CCB_CALLER", "wrong-parent-value")

    assert launcher._agent_command_prefix("gemini").argv == ()
    assert launcher._agent_command_prefix("opencode").argv == ()
    assert calls == []
    assert launcher._agent_command_prefix("codex").argv == ("mcpctl", "run", "key", "--")
    assert calls == [("codex", str(tmp_path), True, "codex")]


def test_error_decision_is_recorded_and_never_rendered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ccb = _load_ccb_module()
    launcher = _launcher(ccb, monkeypatch, tmp_path)
    monkeypatch.setattr(ccb, "decide_mcpv", lambda *_args, **_kwargs: Decision.error("manifest_invalid"))

    with pytest.raises(ccb.PolicyDecisionError):
        launcher._compose_agent_argv("codex", ["codex"])
    status_path = tmp_path / ".ccb" / ccb_mcpv_bridge.STATUS_FILENAME
    status = json.loads(status_path.read_text())
    assert status["providers"]["codex"]["reason_code"] == "manifest_invalid"
    assert status_path.stat().st_mode & 0o777 == 0o600


def test_structural_insertion_position_for_shell_and_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ccb = _load_ccb_module()
    launcher = _launcher(ccb, monkeypatch, tmp_path)
    monkeypatch.setattr(
        launcher,
        "_agent_command_prefix",
        lambda _provider: ccb.AgentCommandPrefix(shell="probe -- ", argv=("probe", "--")),
    )
    command = ccb.ShellAgentCommand(prelude="cd '/resume'; ", agent_command="gemini --resume latest")
    assert launcher._compose_agent_shell("gemini", command) == (
        "cd '/resume'; probe -- gemini --resume latest"
    )
    assert launcher._compose_agent_argv("claude", ["claude", "--continue"]) == (
        ["probe", "--", "claude", "--continue"]
    )


class _DeadBackend:
    def __init__(self) -> None:
        self.respawned: list[str] = []

    def is_alive(self, _pane_id: str) -> bool:
        return False

    def find_pane_by_title_marker(self, _marker: str, _cwd: str = "") -> None:
        return None

    def respawn_pane(self, pane_id: str, **_kwargs) -> None:
        self.respawned.append(pane_id)


def _codex_session(tmp_path: Path) -> caskd_session.CodexProjectSession:
    session_file = tmp_path / ".ccb" / ".codex-session"
    session_file.parent.mkdir(mode=0o700)
    data = {
        "terminal": "tmux",
        "pane_id": "%1",
        "work_dir": str(tmp_path),
        "project_root": str(tmp_path),
        "ccb_managed_launch": True,
        "ccb_launch_caller": "codex",
        "mcpv_policy_required": True,
        "codex_session_id": "old",
        "codex_start_cmd": "mcpctl run key -- codex resume old",
    }
    session_file.write_text(json.dumps(data), encoding="utf-8")
    return caskd_session.CodexProjectSession(session_file=session_file, data=data)


def test_rebinding_rebuilds_wrapped_command_through_shared_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _codex_session(tmp_path)
    monkeypatch.setattr(
        caskd_session,
        "decide_mcpv",
        lambda *_args, **_kwargs: Decision.wrap(("/opt/mcpctl", "run", "key", "--")),
    )
    monkeypatch.setattr(caskd_session, "get_shell_type", lambda: "posix")
    session.update_codex_log_binding(log_path="/tmp/new.jsonl", session_id="new")
    assert session.data["codex_start_cmd"] == "/opt/mcpctl run key -- codex resume new"
    assert "mcpv_policy_error" not in session.data


def test_rebinding_error_never_writes_bare_and_blocks_self_heal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _codex_session(tmp_path)
    original = session.data["codex_start_cmd"]
    monkeypatch.setattr(
        caskd_session,
        "decide_mcpv",
        lambda *_args, **_kwargs: Decision.error("manifest_invalid"),
    )
    backend = _DeadBackend()
    monkeypatch.setattr(caskd_session, "get_backend_for_session", lambda _data: backend)

    session.update_codex_log_binding(log_path="/tmp/new.jsonl", session_id="new")
    assert session.data["codex_start_cmd"] == original
    assert session.data["mcpv_policy_error"] == "manifest_invalid"
    ok, reason = session.ensure_pane()
    assert ok is False
    assert reason == "launch_policy_error:manifest_invalid"
    assert backend.respawned == []

    monkeypatch.setattr(
        caskd_session,
        "decide_mcpv",
        lambda *_args, **_kwargs: Decision.wrap(("/opt/mcpctl", "run", "key", "--")),
    )
    monkeypatch.setattr(caskd_session, "get_shell_type", lambda: "posix")
    session.update_codex_log_binding(log_path="/tmp/new.jsonl", session_id="new")
    assert session.data["codex_start_cmd"] == "/opt/mcpctl run key -- codex resume new"
    assert "mcpv_policy_error" not in session.data


def test_secure_writer_and_remediation_modes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = project / ".ccb"
    home = tmp_path / "home"
    global_ccb = home / ".ccb"
    config.mkdir(parents=True, mode=0o777)
    global_ccb.mkdir(parents=True, mode=0o777)
    config.chmod(0o777)
    global_ccb.chmod(0o777)
    (config / "ccb.config").write_text("codex\n", encoding="utf-8")
    (config / ".codex-session").write_text("{}", encoding="utf-8")
    (global_ccb / "ccb.config").write_text("codex\n", encoding="utf-8")
    for path in (config / "ccb.config", config / ".codex-session", global_ccb / "ccb.config"):
        path.chmod(0o664)

    ok, errors = remediate_ccb_permissions(project, home_dir=home)
    assert ok, errors
    assert config.stat().st_mode & 0o777 == 0o700
    assert global_ccb.stat().st_mode & 0o777 == 0o700
    assert (config / "ccb.config").stat().st_mode & 0o777 == 0o600
    assert (config / ".codex-session").stat().st_mode & 0o777 == 0o600

    target = config / "state.json"
    write_ok, error = safe_write_session(target, "{}\n")
    assert write_ok, error
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(config.glob(".state.json.*.tmp"))


def test_remediation_refuses_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = project / ".ccb"
    home = tmp_path / "home"
    config.mkdir(parents=True)
    home.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.write_text("{}", encoding="utf-8")
    outside.chmod(0o664)
    (config / ".codex-session").symlink_to(outside)

    ok, errors = remediate_ccb_permissions(project, home_dir=home)
    assert ok is False
    assert errors
    assert outside.stat().st_mode & 0o777 == 0o664


def test_runtime_status_surfaces_durable_policy_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    config = project / ".ccb"
    config.mkdir(parents=True, mode=0o700)
    (config / "ccb.config").write_text("codex\n", encoding="utf-8")
    assert ccb_mcpv_bridge.record_decision_status(
        project, "codex", Decision.error("manifest_invalid")
    )
    monkeypatch.setattr(ccb_runtime_status, "is_project_askd_online", lambda *_args, **_kwargs: False)
    status = ccb_runtime_status.resolve_project_runtime_status(project).providers["codex"]
    assert status.mounted is False
    assert status.reason == "launch_policy_error:manifest_invalid"


def test_active_wrap_keeps_secret_out_of_argv_logs_and_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ccb = _load_ccb_module()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".ccb").mkdir(mode=0o700)
    policy_home = Path(os.environ["HOME"])
    _install_synthetic_policy(
        policy_home,
        "from ccb_mcpv_bridge import Decision\n"
        "def decide_wrap(**kwargs): return Decision.wrap(('/opt/mcpctl', 'run', 'phase2', '--'))\n",
    )
    sentinel = "vault-secret-SHOULD-NOT-LEAK"
    monkeypatch.setenv("MCPV_PHASE2_EXAMPLE_TOKEN", sentinel)
    monkeypatch.chdir(project)
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")
    ccb_mcpv_bridge.reset_policy_cache()
    launcher = ccb.AILauncher(providers=["codex"])

    argv = launcher._compose_agent_argv("codex", ["codex", "--version"])
    shell = launcher._compose_agent_shell(
        "codex", ccb.ShellAgentCommand(prelude="", agent_command="codex --version")
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pane_log = runtime / "bridge_output.log"
    pane_log.write_text("normal pane output\n", encoding="utf-8")
    assert launcher._write_codex_session(
        runtime,
        "tmux-session",
        runtime / "input.fifo",
        runtime / "output.fifo",
        pane_id="%1",
        codex_start_cmd=shell,
    )
    session_text = (project / ".ccb" / ".codex-session").read_text(encoding="utf-8")
    session_data = json.loads(session_text)

    assert "phase2" in argv and "phase2" in shell and "phase2" in session_text
    assert session_data["ccb_managed_launch"] is True
    assert session_data["ccb_launch_caller"] == "codex"
    assert session_data["mcpv_policy_required"] is True
    assert sentinel not in "\0".join(argv)
    assert sentinel not in pane_log.read_text(encoding="utf-8")
    assert sentinel not in session_text
