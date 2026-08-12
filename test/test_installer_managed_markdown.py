from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"
POWERSHELL = (
    os.environ.get("CCB_TEST_POWERSHELL")
    or shutil.which("pwsh")
    or shutil.which("powershell")
)

CLAUDE_START = "<!-- CCB_CONFIG_START -->"
CLAUDE_END = "<!-- CCB_CONFIG_END -->"
RATIFICATION_START = "<!-- MUTUAL_RATIFICATION_START -->"
RATIFICATION_END = "<!-- MUTUAL_RATIFICATION_END -->"


def _prepare_install_prefix(tmp_path: Path) -> Path:
    install_prefix = tmp_path / "install-prefix"
    config_dir = install_prefix / "config"
    config_dir.mkdir(parents=True)
    for name in ("claude-md-ccb.md", "claude-md-ccb-route.md", "agents-md-ccb.md"):
        shutil.copy2(REPO_ROOT / "config" / name, config_dir / name)
    return install_prefix


def _run_install_functions(
    *,
    home: Path,
    install_prefix: Path,
    codex_home: Path | None,
    functions: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CODEX_INSTALL_PREFIX"] = str(install_prefix)
    env["CODEX_BIN_DIR"] = str(home / ".local" / "bin")
    if codex_home is None:
        env.pop("CODEX_HOME", None)
    else:
        env["CODEX_HOME"] = str(codex_home)

    commands = "; ".join(functions)
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(INSTALL_SH))}; {commands}"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_managed_blocks_preserve_hand_authored_content_and_are_idempotent(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    claude_home = home / ".claude"
    codex_home = tmp_path / "custom-codex-home"
    claude_home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    install_prefix = _prepare_install_prefix(tmp_path)

    claude_md = claude_home / "CLAUDE.md"
    claude_md.write_text(
        "claude-before\n\n"
        f"{CLAUDE_START}\nold relay\n{CLAUDE_END}\n\n"
        "claude-after\n",
        encoding="utf-8",
    )

    agents_md = codex_home / "AGENTS.md"
    agents_md.write_text(
        "codex-before\n\n"
        "<!-- CCB_ROLES_START -->\nold roles\n<!-- CCB_ROLES_END -->\n\n"
        "codex-middle\n\n"
        "<!-- REVIEW_RUBRICS_START -->\nold rubric\n<!-- REVIEW_RUBRICS_END -->\n\n"
        "codex-after\n",
        encoding="utf-8",
    )

    legacy_agents_md = install_prefix / "AGENTS.md"
    legacy_agents_md.write_text(
        f"{RATIFICATION_START}\nold misplaced block\n{RATIFICATION_END}\n",
        encoding="utf-8",
    )

    functions = ("install_claude_md_config", "install_agents_md_config")
    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=codex_home,
        functions=functions,
    )

    first_claude = claude_md.read_text(encoding="utf-8")
    first_agents = agents_md.read_text(encoding="utf-8")

    assert "claude-before" in first_claude
    assert "claude-after" in first_claude
    assert "codex-before" in first_agents
    assert "codex-middle" in first_agents
    assert "codex-after" in first_agents
    assert "old relay" not in first_claude
    assert "old roles" not in first_agents
    assert "old rubric" not in first_agents
    assert first_claude.count(CLAUDE_START) == 1
    assert first_claude.count(CLAUDE_END) == 1
    assert first_agents.count(RATIFICATION_START) == 1
    assert first_agents.count(RATIFICATION_END) == 1
    assert not legacy_agents_md.exists()

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=codex_home,
        functions=functions,
    )

    assert claude_md.read_text(encoding="utf-8") == first_claude
    assert agents_md.read_text(encoding="utf-8") == first_agents


def test_agents_block_defaults_to_real_global_codex_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    install_prefix = _prepare_install_prefix(tmp_path)

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_agents_md_config",),
    )

    agents_md = home / ".codex" / "AGENTS.md"
    assert agents_md.is_file()
    assert agents_md.read_text(encoding="utf-8").count(RATIFICATION_START) == 1
    assert not (install_prefix / "AGENTS.md").exists()


def test_agents_block_appends_to_existing_unmanaged_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    codex_home.mkdir()
    install_prefix = _prepare_install_prefix(tmp_path)
    agents_md = codex_home / "AGENTS.md"
    agents_md.write_text("hand-authored guidance", encoding="utf-8")

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=codex_home,
        functions=("install_agents_md_config",),
    )

    first = agents_md.read_text(encoding="utf-8")
    assert first.startswith("hand-authored guidance\n\n")
    assert first.count(RATIFICATION_START) == 1

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=codex_home,
        functions=("install_agents_md_config",),
    )
    assert agents_md.read_text(encoding="utf-8") == first


def test_agents_block_warns_and_leaves_malformed_markers_unchanged(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    codex_home.mkdir()
    install_prefix = _prepare_install_prefix(tmp_path)
    agents_md = codex_home / "AGENTS.md"
    malformed = (
        "hand-authored-before\n\n"
        f"{RATIFICATION_START}\ntruncated managed content\n"
        "hand-authored-after\n"
    )
    agents_md.write_text(malformed, encoding="utf-8")

    result = _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=codex_home,
        functions=("install_agents_md_config",),
    )

    assert agents_md.read_text(encoding="utf-8") == malformed
    assert "malformed CCB marker structure" in result.stderr
    assert agents_md.read_text(encoding="utf-8").count(RATIFICATION_START) == 1


def test_agents_block_warns_when_nonempty_override_shadows_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    codex_home.mkdir()
    install_prefix = _prepare_install_prefix(tmp_path)
    (codex_home / "AGENTS.override.md").write_text(
        "temporary override\n", encoding="utf-8"
    )

    result = _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=codex_home,
        functions=("install_agents_md_config",),
    )

    assert "takes precedence" in result.stdout
    assert (codex_home / "AGENTS.md").is_file()


def test_symlinked_codex_home_aliasing_install_prefix_is_not_cleaned(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    install_prefix = _prepare_install_prefix(tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.symlink_to(install_prefix, target_is_directory=True)

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=codex_home,
        functions=("install_agents_md_config",),
    )

    agents_md = install_prefix / "AGENTS.md"
    assert agents_md.is_file()
    assert agents_md.read_text(encoding="utf-8").count(RATIFICATION_START) == 1


def test_uninstall_removes_only_the_managed_codex_block(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    codex_home = tmp_path / "codex-home"
    install_prefix = _prepare_install_prefix(tmp_path)

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=codex_home,
        functions=("install_agents_md_config",),
    )

    agents_md = codex_home / "AGENTS.md"
    agents_md.write_text(
        "hand-authored-before\n\n"
        + agents_md.read_text(encoding="utf-8")
        + "\nhand-authored-after\n",
        encoding="utf-8",
    )

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=codex_home,
        functions=("uninstall_agents_md_config",),
    )

    remaining = agents_md.read_text(encoding="utf-8")
    assert "hand-authored-before" in remaining
    assert "hand-authored-after" in remaining
    assert RATIFICATION_START not in remaining


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")
def test_windows_installer_functionally_copies_template_and_injects_block(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    install_prefix = tmp_path / "windows-install"
    home.mkdir(exist_ok=True)

    def ps_quote(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    command = "\n".join(
        (
            "$ErrorActionPreference = 'Stop'",
            f". {ps_quote(INSTALL_PS1)}",
            "Copy-ProjectItems "
            f"-SourceRoot {ps_quote(REPO_ROOT)} "
            f"-DestinationRoot {ps_quote(install_prefix)}",
            "$installed = Install-AgentsMdConfig "
            f"-InstallPrefix {ps_quote(install_prefix)}",
            "if (-not $installed) { throw 'AGENTS.md injection did not run' }",
        )
    )
    env = os.environ.copy()
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(codex_home)

    subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert (install_prefix / "config" / "agents-md-ccb.md").is_file()
    agents_md = codex_home / "AGENTS.md"
    content = agents_md.read_text(encoding="utf-8")
    assert content.count(RATIFICATION_START) == 1
    assert "co-equal collaborators" in content
    assert not (install_prefix / "AGENTS.md").exists()

    malformed = (
        "windows-hand-authored\n\n"
        f"{RATIFICATION_START}\ntruncated managed content\n"
    )
    agents_md.write_text(malformed, encoding="utf-8")
    malformed_command = "\n".join(
        (
            "$ErrorActionPreference = 'Stop'",
            f". {ps_quote(INSTALL_PS1)}",
            "Install-AgentsMdConfig "
            f"-InstallPrefix {ps_quote(install_prefix)} | Out-Null",
        )
    )
    malformed_result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            malformed_command,
        ],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert agents_md.read_text(encoding="utf-8") == malformed
    assert "Malformed CCB marker structure" in (
        malformed_result.stdout + malformed_result.stderr
    )


def test_managed_templates_have_symmetric_authority_without_fixed_roles() -> None:
    claude_template = (REPO_ROOT / "config" / "claude-md-ccb.md").read_text(
        encoding="utf-8"
    )
    agents_template = (REPO_ROOT / "config" / "agents-md-ccb.md").read_text(
        encoding="utf-8"
    )
    for content in (claude_template, agents_template):
        assert "co-equal collaborators" in content
        assert "authority follows evidence, not identity" in content

    assert "Claude proposes a claim" not in claude_template
    assert "When Claude sends a substantive proposal" not in agents_template


def test_managed_templates_state_work_placement_precedence() -> None:
    """Project instructions may specialize, but may not invert Work Placement."""
    for name in ("claude-md-ccb.md", "agents-md-ccb.md"):
        content = (REPO_ROOT / "config" / name).read_text(encoding="utf-8")
        assert "may not invert Work Placement" in content
        assert "Only an explicit current user instruction may opt out." in content
