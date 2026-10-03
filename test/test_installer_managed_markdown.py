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
CCB_ROLE_SKILLS = ("ccb-lead", "ccb-implementer", "ccb-ratifier")


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
    overlay_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["CODEX_INSTALL_PREFIX"] = str(install_prefix)
    env["CODEX_BIN_DIR"] = str(home / ".local" / "bin")
    if codex_home is None:
        env.pop("CODEX_HOME", None)
    else:
        env["CODEX_HOME"] = str(codex_home)
    if overlay_dir is not None:
        env["CCB_OVERLAY_DIR"] = str(overlay_dir)
    else:
        env.pop("CCB_OVERLAY_DIR", None)

    commands = "; ".join(functions)
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(INSTALL_SH))}; {commands}"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _assert_role_skills_present(skills_root: Path) -> None:
    missing = [name for name in CCB_ROLE_SKILLS if not (skills_root / name).is_dir()]
    assert not missing, f"missing CCB role skill directories: {', '.join(missing)}"


def _assert_skill_trees_equal(source: Path, destination: Path) -> None:
    source_files = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    destination_files = {
        path.relative_to(destination): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert destination_files == source_files


def _self_nested_skill_directories(skills_root: Path) -> list[Path]:
    nested = []
    for skill_dir in skills_root.iterdir():
        if not skill_dir.is_dir():
            continue
        nested.extend(
            path.relative_to(skills_root)
            for path in skill_dir.rglob("*")
            if path.is_dir() and (path / path.name).is_dir()
        )
    return nested


def test_ccb_role_skills_are_required_for_both_providers_and_install_verbatim(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)
    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=(
            "install_claude_skills",
            "install_codex_skills",
            "install_claude_skills",
            "install_codex_skills",
        ),
    )

    provider_roots = (
        (REPO_ROOT / "claude_skills", home / ".claude" / "skills"),
        (REPO_ROOT / "codex_skills", home / ".codex" / "skills"),
    )
    for source_root, installed_root in provider_roots:
        _assert_role_skills_present(source_root)
        _assert_role_skills_present(installed_root)
        nested = _self_nested_skill_directories(installed_root)
        assert not nested, f"self-nested skill directories found: {nested}"
        for name in CCB_ROLE_SKILLS:
            _assert_skill_trees_equal(source_root / name, installed_root / name)

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("uninstall_claude_skills", "uninstall_codex_skills"),
    )
    for _, installed_root in provider_roots:
        for name in CCB_ROLE_SKILLS:
            assert not (installed_root / name).exists()


def test_personal_overlay_replaces_adds_skips_escape_reinstalls_and_uninstalls(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)
    overlay = tmp_path / "overlay"
    claude_replacement = overlay / "claude_skills" / "ask"
    codex_addition = overlay / "codex_skills" / "private-added"
    claude_replacement.mkdir(parents=True)
    codex_addition.mkdir(parents=True)
    (claude_replacement / "SKILL.md").write_text("private ask replacement\n", encoding="utf-8")
    (codex_addition / "SKILL.md").write_text("private new skill\n", encoding="utf-8")
    (codex_addition / "references").mkdir()
    (codex_addition / "references" / "guide.md").write_text("nested content\n", encoding="utf-8")
    invalid_name = overlay / "codex_skills" / "not a plain name"
    invalid_name.mkdir()
    (invalid_name / "SKILL.md").write_text("must be skipped\n", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("outside data\n", encoding="utf-8")
    (overlay / "claude_skills" / "unsafe").symlink_to(outside, target_is_directory=True)
    codex_destination = home / ".codex" / "skills" / "private-added"
    codex_destination.parent.mkdir(parents=True)
    codex_destination.symlink_to(outside, target_is_directory=True)

    install_result = _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        overlay_dir=overlay,
        functions=(
            "install_claude_skills",
            "install_codex_skills",
            "install_personal_skill_overlay",
            "install_claude_skills",
            "install_codex_skills",
            "install_personal_skill_overlay",
        ),
    )

    claude_skills = home / ".claude" / "skills"
    codex_skills = home / ".codex" / "skills"
    assert (claude_skills / "ask" / "SKILL.md").read_text(encoding="utf-8") == "private ask replacement\n"
    assert not (claude_skills / "ask" / "SKILL.md.bash").exists()
    assert (codex_skills / "private-added" / "SKILL.md").read_text(encoding="utf-8") == "private new skill\n"
    assert (codex_skills / "private-added" / "references" / "guide.md").read_text(encoding="utf-8") == "nested content\n"
    assert not (claude_skills / "unsafe").exists()
    assert "Skipping unsafe overlay entry: claude_skills/unsafe" in install_result.stderr
    assert not (codex_skills / "not a plain name").exists()
    assert "Skipping invalid overlay entry: codex_skills/not a plain name" in install_result.stderr
    assert install_result.stdout.count("Applied personal overlay:") == 4
    assert not codex_destination.is_symlink()
    assert marker.read_text(encoding="utf-8") == "outside data\n"
    assert not _self_nested_skill_directories(claude_skills)
    assert not _self_nested_skill_directories(codex_skills)

    manifest = home / ".local" / "share" / "ccb" / "overlay-skills.manifest"
    assert manifest.read_text(encoding="utf-8").splitlines() == ["codex\tprivate-added"]
    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("uninstall_claude_skills", "uninstall_codex_skills"),
    )
    assert not (codex_skills / "private-added").exists()
    assert marker.read_text(encoding="utf-8") == "outside data\n"


@pytest.mark.parametrize("overlay_exists", [False, True])
def test_missing_or_empty_personal_overlay_adds_no_output_or_changes(
    tmp_path: Path, overlay_exists: bool
) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)
    overlay = tmp_path / "empty-overlay"
    if overlay_exists:
        overlay.mkdir()
    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_claude_skills", "install_codex_skills"),
    )
    before = {
        provider: {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        for provider, root in (
            ("claude", home / ".claude" / "skills"),
            ("codex", home / ".codex" / "skills"),
        )
    }
    overlay_result = _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        overlay_dir=overlay if overlay_exists else None,
        functions=("install_personal_skill_overlay",),
    )
    assert "overlay" not in overlay_result.stdout.lower()
    assert "overlay" not in overlay_result.stderr.lower()
    for provider, root in (
        ("claude", home / ".claude" / "skills"),
        ("codex", home / ".codex" / "skills"),
    ):
        after = {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        assert after == before[provider]


def test_ccb_role_skill_inventory_fails_when_one_directory_is_missing(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    for name in CCB_ROLE_SKILLS[:-1]:
        (skills_root / name).mkdir(parents=True, exist_ok=True)

    with pytest.raises(AssertionError, match="ccb-ratifier"):
        _assert_role_skills_present(skills_root)


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


def test_managed_templates_make_roles_current_session_operational_state() -> None:
    for name in ("claude-md-ccb.md", "agents-md-ccb.md"):
        content = (REPO_ROOT / "config" / name).read_text(encoding="utf-8")
        assert "current explicit assignment for this session determines its role" in content
        assert "Historical role assignments are context only" in content
        assert "Role assignment does not override standing engineering rules" in content
        assert "operational context, not architectural decisions" in content
        assert "Claude owns" not in content
        assert "Codex reviews" not in content


def test_managed_templates_keep_native_agents_outside_ccb_topology() -> None:
    for name in ("claude-md-ccb.md", "agents-md-ccb.md"):
        content = (REPO_ROOT / "config" / name).read_text(encoding="utf-8")
        assert "coordinates only mounted top-level sessions" in content
        assert "Native in-session agent tools remain available" in content
        assert "do not receive CCB routing identities" not in content

    peer_skill = (REPO_ROOT / "claude_skills" / "peer-ask" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Do not use Claude sub-agents" not in peer_skill
    assert "CCB addresses only mounted top-level panes" in peer_skill
