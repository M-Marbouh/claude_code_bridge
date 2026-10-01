from __future__ import annotations

import ast
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CCB_PATH = REPO_ROOT / "ccb"


def _load_ccb_module() -> object:
    loader = SourceFileLoader("ccb_agent_composition_test", str(CCB_PATH))
    spec = importlib.util.spec_from_loader("ccb_agent_composition_test", loader)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _launcher(ccb, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(exist_ok=True)
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")
    return ccb.AILauncher(providers=["codex"], **kwargs)


def test_composition_seam_is_an_exact_noop(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    launcher = _launcher(ccb, monkeypatch, tmp_path)

    command = ccb.ShellAgentCommand(prelude="cd '/path with spaces'; ", agent_command="agent --flag 'value'")

    assert isinstance(command, str)
    assert launcher._compose_agent_shell("codex", command) == command.render_unwrapped()
    with pytest.raises(AttributeError, match="immutable"):
        command.agent_command = "changed"
    argv = ["/path with spaces/agent", "--flag", "value with spaces"]
    assert launcher._compose_agent_argv("claude", argv) == argv


@pytest.mark.parametrize(
    ("shell_type", "expected_prelude"),
    [
        ("posix", "cd '/resume dir'; "),
        ("powershell", "Set-Location -Path '/resume dir'; "),
    ],
)
def test_gemini_resume_boundary_is_structural_and_byte_exact(
    monkeypatch,
    tmp_path: Path,
    shell_type: str,
    expected_prelude: str,
) -> None:
    ccb = _load_ccb_module()
    launcher = _launcher(
        ccb,
        monkeypatch,
        tmp_path,
        resume=True,
        launch_args={"gemini": "--model gemini-test"},
    )
    monkeypatch.setattr(ccb, "get_shell_type", lambda: shell_type)
    monkeypatch.setattr(
        launcher,
        "_get_latest_gemini_project_hash",
        lambda: ("project", True, Path("/resume dir")),
    )

    command = launcher._get_start_cmd("gemini")

    assert command.prelude == expected_prelude
    assert command.agent_command == "gemini --resume latest --model gemini-test"
    assert launcher._compose_agent_shell("gemini", command) == (
        expected_prelude + "gemini --resume latest --model gemini-test"
    )


def test_launch_args_and_opaque_opencode_command_remain_byte_exact(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    launcher = _launcher(
        ccb,
        monkeypatch,
        tmp_path,
        launch_args={
            "codex": "--model codex-test",
            "opencode": "--agent build",
        },
    )
    monkeypatch.setattr(launcher, "_build_codex_start_cmd", lambda: "codex -c 'x=y'")
    monkeypatch.setattr(launcher, "_build_opencode_start_cmd", lambda: "env X='a b' custom-opencode --raw")

    codex = launcher._get_start_cmd("codex")
    opencode = launcher._get_start_cmd("opencode")

    assert launcher._compose_agent_shell("codex", codex) == "codex -c 'x=y' --model codex-test"
    assert launcher._compose_agent_shell("opencode", opencode) == (
        "env X='a b' custom-opencode --raw --agent build"
    )


def test_hermes_launch_is_pinned_and_session_records_effective_home(monkeypatch, tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    launcher = _launcher(
        ccb, monkeypatch, tmp_path, launch_args={"hermes": "--provider not-allowed --model not-allowed"},
    )
    command = launcher._get_start_cmd("hermes")
    assert command.agent_command == "hermes --cli --no-restore-cwd"
    assert launcher._compose_agent_shell("hermes", command) == "hermes --cli --no-restore-cwd"

    from ccb_start_config import DEFAULT_PROVIDERS

    assert "hermes" not in DEFAULT_PROVIDERS
    session_file = tmp_path / ".hermes-session"
    hermes_home = tmp_path / "user-hermes-home"
    launcher.session_id = "ai-test"
    launcher.project_root = tmp_path
    launcher.invocation_dir = tmp_path
    launcher.terminal_type = "tmux"
    launcher._live_slots = []
    launcher._project_session_file = lambda _name: session_file
    launcher._provider_env_overrides = lambda _provider: {"HERMES_HOME": str(hermes_home)}
    launcher._maybe_start_provider_daemon = lambda _provider: None
    launcher._auto_prune_session_records = lambda: None
    monkeypatch.setattr(ccb, "upsert_registry", lambda _record: True)

    assert launcher._write_hermes_session(tmp_path / "runtime", None, pane_id="%1", start_cmd=command.agent_command)
    import json

    recorded = json.loads(session_file.read_text())
    assert recorded["hermes_home"] == str(hermes_home.resolve())
    assert recorded["state_db_path"] == str((hermes_home / "state.db").resolve())


def test_every_agent_launcher_routes_through_the_typed_seam() -> None:
    tree = ast.parse(CCB_PATH.read_text(encoding="utf-8"), filename=str(CCB_PATH))
    launcher = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AILauncher"
    )
    methods = {
        node.name: node
        for node in launcher.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    expected = {
        "_start_provider_wezterm": {"_compose_agent_shell"},
        "_start_codex_tmux": {"_compose_agent_shell"},
        "_start_gemini_tmux": {"_compose_agent_shell"},
        "_start_opencode_tmux": {"_compose_agent_shell"},
        "_start_codex_current_pane": {"_compose_agent_shell", "_compose_agent_argv"},
        "_start_gemini_current_pane": {"_compose_agent_shell"},
        "_start_opencode_current_pane": {"_compose_agent_shell", "_compose_agent_argv"},
        "_start_claude": {"_compose_agent_argv"},
        "_start_claude_pane": {"_compose_agent_shell"},
    }

    for method_name, required_calls in expected.items():
        calls = {
            node.func.attr
            for node in ast.walk(methods[method_name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert required_calls <= calls, method_name

    cmd_pane_calls = {
        node.func.attr
        for node in ast.walk(methods["_start_cmd_pane"])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "_compose_agent_shell" not in cmd_pane_calls
    assert "_compose_agent_argv" not in cmd_pane_calls


def test_version_parser_finds_embedded_constants_after_import_block(tmp_path: Path) -> None:
    ccb = _load_ccb_module()
    installed = tmp_path / "installed"
    installed.mkdir()
    lines = ["# padding"] * 65 + [
        'VERSION = "0.13.1"',
        'GIT_COMMIT = "abc1234"',
        'GIT_DATE = "2026-09-08"',
    ]
    (installed / "ccb").write_text("\n".join(lines), encoding="utf-8")

    assert ccb._get_version_info(installed) == {
        "version": "0.13.1",
        "commit": "abc1234",
        "date": "2026-09-08",
    }
