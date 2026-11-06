from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"
BASH = shutil.which("bash") or "bash"
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
NEUTRAL_ROLE_SKILLS_DIR = REPO_ROOT / "role_skills"


def _role_skill_descriptions() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in (NEUTRAL_ROLE_SKILLS_DIR / "descriptions.tsv").read_text(
        encoding="utf-8"
    ).splitlines():
        role, separator, description = line.partition("\t")
        if separator:
            rows[role] = description
    return rows


CCB_ROLE_DESCRIPTIONS = _role_skill_descriptions()

# Copied verbatim from a real user skill that already lived in a Pi skills
# directory when the role skills were added there.
PI_TYPESCRIPT_SKILL = (
    "---\n"
    "name: pi-typescript\n"
    "description: Write and verify TypeScript changes using the current "
    "project's conventions.\n"
    "---\n"
    "Follow the project's stack and formatting; do not introduce another UI "
    "framework.\n"
    "Use precise types, avoid unjustified any, and verify external schemas at "
    "boundaries.\n"
    "Prefer existing utilities and installed dependencies over one-use "
    "abstractions.\n"
    "Use Context7 resolve-library-id then query-docs for uncertain library "
    "behavior.\n"
    "Test the affected contract and guard behavior; run the project's applicable "
    "type check.\n"
)



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
    env_extra: dict[str, Path | str | None] | None = None,
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
    # Never let the developer's real provider homes leak into a test run.
    for name in (
        "PI_CODING_AGENT_DIR",
        "OPENCODE_CONFIG_DIR",
        "GEMINI_CLI_HOME",
        "HERMES_HOME",
        "HERMES_DATA_DIR_SUFFIX",
    ):
        env.pop(name, None)
    for name, value in (env_extra or {}).items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = str(value)

    commands = "; ".join(functions)
    return subprocess.run(
        [BASH, "-c", f"source {shlex.quote(str(INSTALL_SH))}; {commands}"],
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


def _skill_frontmatter_and_body(skill_md: Path) -> tuple[str, str]:
    text = skill_md.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{skill_md} does not start with frontmatter"
    end = text.index("\n---\n", 4)
    frontmatter = text[len("---\n") : end + 1]
    remainder = text[end + len("\n---\n") :]
    assert remainder.startswith("\n"), f"{skill_md} has no blank line before its body"
    return frontmatter, remainder[1:]


def _assert_role_skill_bodies_match_neutral(copies: dict[str, list[Path]]) -> None:
    mismatched = []
    for role, paths in copies.items():
        neutral_body = (NEUTRAL_ROLE_SKILLS_DIR / f"{role}.md").read_text(encoding="utf-8")
        for path in paths:
            assert path.is_file(), f"missing role skill copy: {path}"
            _, body = _skill_frontmatter_and_body(path)
            if body != neutral_body:
                mismatched.append(str(path))
    assert not mismatched, f"role skill bodies diverged from the neutral source: {mismatched}"


def _provider_role_skill_paths(
    home: Path, overrides: dict[str, Path]
) -> dict[str, list[Path]]:
    """Every installed role skill copy, using each provider's verified path."""
    claude_root = home / ".claude" / "skills"
    codex_root = home / ".codex" / "skills"
    pi_root = overrides["PI_CODING_AGENT_DIR"] / "skills"
    opencode_root = overrides["OPENCODE_CONFIG_DIR"] / "skills"
    gemini_root = overrides["GEMINI_CLI_HOME"] / ".gemini" / "skills"
    hermes_root = overrides["HERMES_HOME"] / "skills"
    return {
        role: [
            REPO_ROOT / "claude_skills" / role / "SKILL.md",
            REPO_ROOT / "codex_skills" / role / "SKILL.md",
            claude_root / role / "SKILL.md",
            codex_root / role / "SKILL.md",
            pi_root / role / "SKILL.md",
            opencode_root / role / "SKILL.md",
            gemini_root / role / "SKILL.md",
            hermes_root / role / "SKILL.md",
        ]
        for role in CCB_ROLE_SKILLS
    }


def test_ccb_role_skill_bodies_are_provider_neutral_and_identical_everywhere(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)
    overrides = {
        "PI_CODING_AGENT_DIR": tmp_path / "pi-agent",
        "OPENCODE_CONFIG_DIR": tmp_path / "opencode-config",
        "GEMINI_CLI_HOME": tmp_path / "gemini-home",
        "HERMES_HOME": tmp_path / "hermes-home",
    }
    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=(
            "install_claude_skills",
            "install_codex_skills",
            "install_role_skills",
        ),
        env_extra=overrides,
    )

    _assert_role_skill_bodies_match_neutral(
        _provider_role_skill_paths(home, overrides)
    )

    for role in CCB_ROLE_SKILLS:
        neutral_body = (NEUTRAL_ROLE_SKILLS_DIR / f"{role}.md").read_text(encoding="utf-8")
        for provider_name in ("Claude", "Codex", "Gemini", "OpenCode", "Hermes", "Pi"):
            assert provider_name not in neutral_body, (
                f"neutral {role} body names {provider_name}"
            )
        assert (
            "Take this role when the user explicitly assigns it in the current "
            "conversation, when the user invokes this skill, or when `ccb-role brief` "
            "printed these instructions for this session." in neutral_body
        ), f"neutral {role} body lacks the explicit-assignment guard"
        # The automatic path must yield to a role that is already recorded.
        assert (
            "On that last path only, if this session's recorded role is a different one, "
            "do not take this role." in neutral_body
        ), f"neutral {role} body lacks the recorded-role check"

    # Pi is the only one of these loaders that documents the field, so only the
    # Pi copy may declare it; the other three rely on the body guard.
    pi_frontmatter, _ = _skill_frontmatter_and_body(
        overrides["PI_CODING_AGENT_DIR"] / "skills" / "ccb-lead" / "SKILL.md"
    )
    assert "disable-model-invocation: true" in pi_frontmatter
    for root in (
        overrides["OPENCODE_CONFIG_DIR"] / "skills",
        overrides["GEMINI_CLI_HOME"] / ".gemini" / "skills",
        overrides["HERMES_HOME"] / "skills",
    ):
        frontmatter, _ = _skill_frontmatter_and_body(root / "ccb-lead" / "SKILL.md")
        assert "disable-model-invocation" not in frontmatter
        assert "name: ccb-lead" in frontmatter


def test_role_skill_drift_guard_fails_on_one_edited_copy(tmp_path: Path) -> None:
    """Negative control: the body-equality guard is capable of failing."""
    neutral_body = (NEUTRAL_ROLE_SKILLS_DIR / "ccb-lead.md").read_text(encoding="utf-8")

    faithful = tmp_path / "faithful" / "ccb-lead" / "SKILL.md"
    faithful.parent.mkdir(parents=True)
    faithful.write_text(
        "---\nname: ccb-lead\ndescription: Assign the lead.\n---\n\n" + neutral_body,
        encoding="utf-8",
    )
    _assert_role_skill_bodies_match_neutral({"ccb-lead": [faithful]})

    edited = tmp_path / "edited" / "ccb-lead" / "SKILL.md"
    edited.parent.mkdir(parents=True)
    edited.write_text(
        "---\nname: ccb-lead\ndescription: Assign the lead.\n---\n\n"
        + neutral_body
        + "This line exists in only one copy.\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="diverged from the neutral source"):
        _assert_role_skill_bodies_match_neutral({"ccb-lead": [faithful, edited]})


ROLE_SKILL_FILES = tuple(
    [NEUTRAL_ROLE_SKILLS_DIR / f"{role}.md" for role in CCB_ROLE_SKILLS]
    + [
        REPO_ROOT / provider / role / "SKILL.md"
        for provider in ("claude_skills", "codex_skills")
        for role in CCB_ROLE_SKILLS
    ]
)


def test_role_skill_credential_wording_is_updated_in_every_role_file() -> None:
    """A role can be recorded from any launch, so the wording no longer claims
    that roles need a launch with live session IDs."""
    assert len(ROLE_SKILL_FILES) == 9
    for path in ROLE_SKILL_FILES:
        text = path.read_text(encoding="utf-8")
        assert "this session has no CCB launch credential" in text, (
            f"{path} does not carry the credential wording"
        )
        for stale in (
            "this launch has no live session IDs",
            "the launch has no paired session",
            "Roles exist only in launches with live session IDs",
        ):
            assert stale not in text, f"{path} still carries {stale!r}"


def test_role_skills_are_not_created_for_absent_provider_homes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)

    result = _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_role_skills",),
    )

    assert "Installed pi role skill" not in result.stdout
    assert not (home / ".pi").exists()
    assert not (home / ".hermes").exists()
    assert not (home / ".gemini").exists()
    assert not (home / ".config" / "opencode").exists()

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("uninstall_role_skills",),
    )
    assert not (home / ".pi").exists()
    assert not (home / ".hermes").exists()
    assert not (home / ".gemini").exists()


def test_role_skills_install_under_overrides_and_reinstall_is_idempotent(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)
    overrides = {
        "PI_CODING_AGENT_DIR": tmp_path / "pi-agent",
        "OPENCODE_CONFIG_DIR": tmp_path / "opencode-config",
        "GEMINI_CLI_HOME": tmp_path / "gemini-home",
        "HERMES_HOME": tmp_path / "hermes-home",
    }
    roots = (
        overrides["PI_CODING_AGENT_DIR"] / "skills",
        overrides["OPENCODE_CONFIG_DIR"] / "skills",
        overrides["GEMINI_CLI_HOME"] / ".gemini" / "skills",
        overrides["HERMES_HOME"] / "skills",
    )

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_role_skills",),
        env_extra=overrides,
    )

    def snapshot() -> dict[str, bytes]:
        return {
            str(path): path.read_bytes()
            for root in roots
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    first = snapshot()
    assert len(first) == len(roots) * len(CCB_ROLE_SKILLS)

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_role_skills",),
        env_extra=overrides,
    )
    assert snapshot() == first


def test_hermes_role_skills_use_the_suffixed_home_when_hermes_home_is_unset(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)
    suffixed_home = home / ".hermes-review"
    legacy_home = home / ".hermes"
    suffixed_home.mkdir(parents=True)
    legacy_home.mkdir()
    env_extra = {"HERMES_DATA_DIR_SUFFIX": "-review"}

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_role_skills",),
        env_extra=env_extra,
    )

    for role in CCB_ROLE_SKILLS:
        assert (suffixed_home / "skills" / role / "SKILL.md").is_file()
        assert not (legacy_home / "skills" / role).exists()
    assert not (home / ".pi").exists()
    assert not (home / ".gemini").exists()

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("uninstall_role_skills",),
        env_extra=env_extra,
    )
    for role in CCB_ROLE_SKILLS:
        assert not (suffixed_home / "skills" / role).exists()
        assert not (legacy_home / "skills" / role).exists()


def test_hermes_home_override_wins_over_the_suffix(tmp_path: Path) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)
    suffixed_home = home / ".hermes-review"
    override = tmp_path / "custom-hermes"
    suffixed_home.mkdir(parents=True)
    env_extra = {"HERMES_HOME": override, "HERMES_DATA_DIR_SUFFIX": "-review"}

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_role_skills",),
        env_extra=env_extra,
    )

    for role in CCB_ROLE_SKILLS:
        assert (override / "skills" / role / "SKILL.md").is_file()
    assert not (suffixed_home / "skills").exists()

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("uninstall_role_skills",),
        env_extra=env_extra,
    )
    for role in CCB_ROLE_SKILLS:
        assert not (override / "skills" / role).exists()
    assert not (suffixed_home / "skills").exists()


def test_hermes_home_override_expands_home_references(tmp_path: Path) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)
    home.mkdir(exist_ok=True)

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_role_skills",),
        env_extra={"HERMES_HOME": "$HOME/hermes-from-var"},
    )

    assert (home / "hermes-from-var" / "skills" / "ccb-lead" / "SKILL.md").is_file()
    assert not (home / ".hermes").exists()


def _restricted_bin_dir(tmp_path: Path, *, with_python: bool) -> Path:
    """A PATH holding only what the role-skill functions need."""
    bin_dir = tmp_path / "restricted-bin"
    bin_dir.mkdir()
    for name in ("cat", "chmod", "cp", "dirname", "mkdir", "rm"):
        target = shutil.which(name)
        assert target, f"{name} is required to build the restricted PATH"
        (bin_dir / name).symlink_to(target)
    if with_python:
        interpreter = shutil.which("python3") or sys.executable
        wrapper = bin_dir / "ccb-custom-python"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{interpreter}" "$@"\n', encoding="utf-8"
        )
        wrapper.chmod(0o755)
    return bin_dir


def test_hermes_home_expansion_uses_the_selected_python_interpreter(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)
    home.mkdir(exist_ok=True)
    # No python3 or python on PATH: only the interpreter named by CCB_PYTHON_BIN.
    bin_dir = _restricted_bin_dir(tmp_path, with_python=True)

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_role_skills",),
        env_extra={
            "PATH": bin_dir,
            "CCB_PYTHON_BIN": bin_dir / "ccb-custom-python",
            "HERMES_HOME": r"$HOME/custom-hermes",
        },
    )

    assert (home / "custom-hermes" / "skills" / "ccb-lead" / "SKILL.md").is_file()
    assert not (home / ".hermes").exists()
    assert not (REPO_ROOT / "$HOME").exists()


def test_unresolvable_hermes_home_is_skipped_without_failing_the_install(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)
    # A provider home that must still be handled with no interpreter available.
    (home / ".pi" / "agent").mkdir(parents=True)
    bin_dir = _restricted_bin_dir(tmp_path, with_python=False)
    env_extra = {"PATH": bin_dir, "HERMES_HOME": r"$HOME/literal-hermes"}

    install_result = _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_role_skills",),
        env_extra=env_extra,
    )

    assert "HERMES_HOME" in install_result.stderr
    assert "skipping Hermes role skills" in install_result.stderr
    assert (home / ".pi" / "agent" / "skills" / "ccb-lead" / "SKILL.md").is_file()
    assert not (REPO_ROOT / "$HOME").exists()
    assert not (tmp_path / "$HOME").exists()
    assert not (home / "literal-hermes").exists()

    uninstall_result = _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("uninstall_role_skills",),
        env_extra=env_extra,
    )

    assert "skipping Hermes role skills" in uninstall_result.stderr
    assert not (home / ".pi" / "agent" / "skills" / "ccb-lead").exists()
    assert not (REPO_ROOT / "$HOME").exists()
    assert not (tmp_path / "$HOME").exists()


def test_hermes_home_expansion_failure_skips_hermes_without_failing(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)
    (home / ".pi" / "agent").mkdir(parents=True)
    bin_dir = _restricted_bin_dir(tmp_path, with_python=True)
    # Passes the installer's interpreter probe, then fails the expansion itself.
    interpreter = shutil.which("python3") or sys.executable
    (bin_dir / "ccb-custom-python").write_text(
        f'#!/bin/sh\nif [ "$1" = "-" ]; then exit 3; fi\nexec "{interpreter}" "$@"\n',
        encoding="utf-8",
    )

    result = _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_role_skills",),
        env_extra={
            "PATH": bin_dir,
            "CCB_PYTHON_BIN": bin_dir / "ccb-custom-python",
            "HERMES_HOME": r"$HOME/literal-hermes",
        },
    )

    assert "Could not expand shell references in HERMES_HOME" in result.stderr
    assert (home / ".pi" / "agent" / "skills" / "ccb-lead" / "SKILL.md").is_file()
    assert not (REPO_ROOT / "$HOME").exists()
    assert not (tmp_path / "$HOME").exists()


def test_absent_suffixed_hermes_home_is_not_replaced_by_a_legacy_dot_hermes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)
    legacy_skills = home / ".hermes" / "skills"
    legacy_skills.mkdir(parents=True)
    env_extra = {"HERMES_DATA_DIR_SUFFIX": "-review"}

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_role_skills",),
        env_extra=env_extra,
    )

    assert not (home / ".hermes-review").exists()
    assert list(legacy_skills.iterdir()) == []

    # A blank HERMES_HOME counts as unset, exactly as Hermes resolves it.
    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_role_skills",),
        env_extra={**env_extra, "HERMES_HOME": "   "},
    )

    assert not (home / ".hermes-review").exists()
    assert list(legacy_skills.iterdir()) == []


def test_role_skills_leave_foreign_skills_untouched_and_uninstall_only_ccb_dirs(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)

    pi_skills = home / ".pi" / "agent" / "skills"
    fixture = pi_skills / "pi-typescript" / "SKILL.md"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(PI_TYPESCRIPT_SKILL, encoding="utf-8")

    foreign = {}
    for skills_root, name in (
        (home / ".hermes" / "skills", "notes"),
        (home / ".config" / "opencode" / "skills", "local-helper"),
        (home / ".gemini" / "skills", "local-review"),
    ):
        path = skills_root / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        content = f"---\nname: {name}\ndescription: Keep me.\n---\nkeep\n"
        path.write_text(content, encoding="utf-8")
        foreign[path] = content

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_role_skills",),
    )

    for name in CCB_ROLE_SKILLS:
        assert (pi_skills / name / "SKILL.md").is_file()
    assert sorted(p.name for p in pi_skills.iterdir()) == [
        "ccb-implementer",
        "ccb-lead",
        "ccb-ratifier",
        "pi-typescript",
    ]
    assert fixture.read_text(encoding="utf-8") == PI_TYPESCRIPT_SKILL
    for path, content in foreign.items():
        assert path.read_text(encoding="utf-8") == content

    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("uninstall_role_skills",),
    )

    for name in CCB_ROLE_SKILLS:
        assert not (pi_skills / name).exists()
        assert not (home / ".hermes" / "skills" / name).exists()
        assert not (home / ".config" / "opencode" / "skills" / name).exists()
        assert not (home / ".gemini" / "skills" / name).exists()
    assert fixture.read_text(encoding="utf-8") == PI_TYPESCRIPT_SKILL
    for path, content in foreign.items():
        assert path.read_text(encoding="utf-8") == content


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
    # `ccb-role brief` reads these from the install prefix, so the Windows copy
    # list must carry them too, not only the Unix rsync/tar tree.
    assert (install_prefix / "role_skills" / "ccb-lead.md").is_file()
    assert (install_prefix / "role_skills" / "descriptions.tsv").is_file()
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


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")
def test_windows_role_skills_render_the_shared_body_and_follow_resolved_homes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    local_appdata = tmp_path / "localappdata"
    overrides = {
        "pi": tmp_path / "override-pi",
        "opencode": tmp_path / "override-opencode",
        "gemini": tmp_path / "override-gemini",
        "hermes": tmp_path / "override-hermes",
    }
    home.mkdir(exist_ok=True)
    local_appdata.mkdir()

    def ps_quote(value: Path | str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    # A foreign skill living inside an active install target: uninstall must
    # leave it alone and must not alter its content.
    foreign_skill = "---\nname: local-notes\ndescription: Keep me.\n---\nkeep this content\n"
    ps_foreign_skill = "\"---`nname: local-notes`ndescription: Keep me.`n---`nkeep this content`n\""

    command = "\n".join(
        (
            "$ErrorActionPreference = 'Stop'",
            f". {ps_quote(INSTALL_PS1)}",
            f"$testRoot = {ps_quote(home)}",
            f"$lap = {ps_quote(local_appdata)}",
            "function Reset-RoleEnv {",
            "  $env:PI_CODING_AGENT_DIR = $null",
            "  $env:OPENCODE_CONFIG_DIR = $null",
            "  $env:XDG_CONFIG_HOME = $null",
            "  $env:GEMINI_CLI_HOME = $null",
            "  $env:HERMES_HOME = $null",
            "  $env:HERMES_DATA_DIR_SUFFIX = $null",
            "}",
            "function Get-TreeFingerprint([string]$Root) {",
            "  if (-not (Test-Path -LiteralPath $Root)) { return '' }",
            "  (Get-ChildItem -LiteralPath $Root -Recurse -File | Sort-Object FullName | ForEach-Object {",
            "    $_.FullName + ':' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash",
            "  }) -join '|'",
            "}",
            "function Assert-NoCcbRole([string]$SkillsRoot) {",
            "  foreach ($role in @('ccb-lead', 'ccb-implementer', 'ccb-ratifier')) {",
            "    if (Test-Path -LiteralPath (Join-Path $SkillsRoot $role)) {",
            "      throw \"unexpected role skill $role under $SkillsRoot\"",
            "    }",
            "  }",
            "}",
            "",
            "# 1. No provider home and no override: nothing may be created.",
            "Reset-RoleEnv",
            "foreach ($target in Get-RoleSkillTargets) {",
            "  if ($target.Present) { throw \"target $($target.Provider) should be absent\" }",
            "}",
            "Install-RoleSkills",
            "foreach ($path in @((Join-Path $testRoot '.pi'), (Join-Path $testRoot '.hermes'), (Join-Path $testRoot '.gemini'), (Join-Path $testRoot '.config/opencode'))) {",
            "  if (Test-Path -LiteralPath $path) { throw \"install created $path\" }",
            "}",
            "",
            "# 2. A legacy ~/.hermes is not the Hermes home when the suffixed default is absent.",
            "Reset-RoleEnv",
            "$env:HERMES_DATA_DIR_SUFFIX = '-review'",
            "$legacySkills = Join-Path $testRoot '.hermes/skills'",
            "New-Item -ItemType Directory -Force -Path (Join-Path $legacySkills 'notes') | Out-Null",
            "[System.IO.File]::WriteAllText((Join-Path $legacySkills 'notes/SKILL.md'), \"---`nname: notes`ndescription: Keep me.`n---`nkeep`n\", [System.Text.Encoding]::UTF8)",
            "$hermesTarget = Get-RoleSkillTargets | Where-Object { $_.Provider -eq 'hermes' }",
            "if ($hermesTarget.Present) { throw 'a legacy .hermes must not count as the resolved Hermes home' }",
            "if ($hermesTarget.Base -ne (Join-Path $lap 'hermes-review')) { throw \"unexpected Hermes base: $($hermesTarget.Base)\" }",
            "Install-RoleSkills",
            "if (Test-Path -LiteralPath (Join-Path $legacySkills 'ccb-lead')) { throw 'installed into a legacy .hermes' }",
            "",
            "# 3. Default homes present: the suffixed Hermes home is used.",
            "New-Item -ItemType Directory -Force -Path (Join-Path $testRoot '.pi/agent') | Out-Null",
            "New-Item -ItemType Directory -Force -Path (Join-Path $testRoot '.config/opencode') | Out-Null",
            "New-Item -ItemType Directory -Force -Path (Join-Path $testRoot '.gemini') | Out-Null",
            "New-Item -ItemType Directory -Force -Path (Join-Path $lap 'hermes-review') | Out-Null",
            "New-Item -ItemType Directory -Force -Path (Join-Path $testRoot '.pi/agent/skills/local-notes') | Out-Null",
            f"[System.IO.File]::WriteAllText((Join-Path $testRoot '.pi/agent/skills/local-notes/SKILL.md'), {ps_foreign_skill}, $script:utf8NoBom)",
            "Install-RoleSkills",
            "$piSkills = Join-Path $testRoot '.pi/agent/skills'",
            "$fingerprint = Get-TreeFingerprint $piSkills",
            "Install-RoleSkills",
            "if ((Get-TreeFingerprint $piSkills) -ne $fingerprint) { throw 'a repeat install changed the Pi skills tree' }",
            "Uninstall-RoleSkills",
            "foreach ($root in @($piSkills, (Join-Path $testRoot '.config/opencode/skills'), (Join-Path $testRoot '.gemini/skills'), (Join-Path $lap 'hermes-review/skills'))) {",
            "  Assert-NoCcbRole $root",
            "}",
            "if (-not (Test-Path -LiteralPath (Join-Path $legacySkills 'notes/SKILL.md'))) { throw 'uninstall removed a foreign skill from the legacy home' }",
            "if (-not (Test-Path -LiteralPath (Join-Path $testRoot '.pi/agent/skills/local-notes/SKILL.md'))) { throw 'uninstall removed a foreign skill from the Pi target' }",
            f"if ([System.IO.File]::ReadAllText((Join-Path $testRoot '.pi/agent/skills/local-notes/SKILL.md'), [System.Text.Encoding]::UTF8) -ne {ps_foreign_skill}) {{ throw 'uninstall changed a foreign skill in the Pi target' }}",
            "Install-RoleSkills",
            "",
            "# 4. Overrides win, including HERMES_HOME over HERMES_DATA_DIR_SUFFIX.",
            "Reset-RoleEnv",
            f"$env:PI_CODING_AGENT_DIR = {ps_quote(overrides['pi'])}",
            f"$env:OPENCODE_CONFIG_DIR = {ps_quote(overrides['opencode'])}",
            f"$env:GEMINI_CLI_HOME = {ps_quote(overrides['gemini'])}",
            f"$env:HERMES_HOME = {ps_quote(overrides['hermes'])}",
            "$env:HERMES_DATA_DIR_SUFFIX = '-ignored'",
            "foreach ($target in Get-RoleSkillTargets) {",
            "  if (-not $target.Present) { throw \"override target $($target.Provider) not present\" }",
            "}",
            "$hermesTarget = Get-RoleSkillTargets | Where-Object { $_.Provider -eq 'hermes' }",
            f"if ($hermesTarget.Base -ne {ps_quote(overrides['hermes'])}) {{",
            "  throw \"HERMES_HOME did not win: $($hermesTarget.Base)\"",
            "}",
            "New-Item -ItemType Directory -Force -Path (Join-Path $env:PI_CODING_AGENT_DIR 'skills/local-notes') | Out-Null",
            f"[System.IO.File]::WriteAllText((Join-Path $env:PI_CODING_AGENT_DIR 'skills/local-notes/SKILL.md'), {ps_foreign_skill}, $script:utf8NoBom)",
            "Install-RoleSkills",
            "Uninstall-RoleSkills",
            "foreach ($root in @((Join-Path $env:PI_CODING_AGENT_DIR 'skills'), (Join-Path $env:OPENCODE_CONFIG_DIR 'skills'), (Join-Path $env:GEMINI_CLI_HOME '.gemini/skills'), (Join-Path $env:HERMES_HOME 'skills'))) {",
            "  Assert-NoCcbRole $root",
            "}",
            "if (-not (Test-Path -LiteralPath (Join-Path $env:PI_CODING_AGENT_DIR 'skills/local-notes/SKILL.md'))) { throw 'uninstall removed a foreign skill from an override target' }",
            f"if ([System.IO.File]::ReadAllText((Join-Path $env:PI_CODING_AGENT_DIR 'skills/local-notes/SKILL.md'), [System.Text.Encoding]::UTF8) -ne {ps_foreign_skill}) {{ throw 'uninstall changed a foreign skill in an override target' }}",
            "if (-not (Test-Path -LiteralPath (Join-Path $testRoot '.pi/agent/skills/ccb-lead/SKILL.md'))) { throw 'an override uninstall removed a default-target role skill' }",
            "Install-RoleSkills",
            "",
            "# 5. ~ in HERMES_HOME expands through HOME first, then USERPROFILE.",
            "Reset-RoleEnv",
            f"$env:HOME = {ps_quote(tmp_path / 'posix-home')}",
            f"$env:USERPROFILE = {ps_quote(tmp_path / 'windows-home')}",
            "$env:HERMES_HOME = '~/hermes-tilde'",
            "$hermesTarget = Get-RoleSkillTargets | Where-Object { $_.Provider -eq 'hermes' }",
            f"if ($hermesTarget.Base -ne (Join-Path {ps_quote(tmp_path / 'posix-home')} 'hermes-tilde')) {{",
            "  throw \"~ did not expand through HOME: $($hermesTarget.Base)\"",
            "}",
            "$env:HOME = $null",
            "$hermesTarget = Get-RoleSkillTargets | Where-Object { $_.Provider -eq 'hermes' }",
            f"if ($hermesTarget.Base -ne (Join-Path {ps_quote(tmp_path / 'windows-home')} 'hermes-tilde')) {{",
            "  throw \"~ did not fall back to USERPROFILE: $($hermesTarget.Base)\"",
            "}",
            f"$env:HOME = {ps_quote(home)}",
            f"$env:USERPROFILE = {ps_quote(home)}",
            "Write-Host 'Windows role skills: cases complete'",
        )
    )

    env = os.environ.copy()
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    env["LOCALAPPDATA"] = str(local_appdata)
    for name in (
        "PI_CODING_AGENT_DIR",
        "OPENCODE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "GEMINI_CLI_HOME",
        "HERMES_HOME",
        "HERMES_DATA_DIR_SUFFIX",
    ):
        env.pop(name, None)

    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Windows role skills: cases complete" in result.stdout

    neutral = {
        role: (NEUTRAL_ROLE_SKILLS_DIR / f"{role}.md").read_text(encoding="utf-8")
        for role in CCB_ROLE_SKILLS
    }
    roots = {
        "default-pi": home / ".pi" / "agent" / "skills",
        "default-opencode": home / ".config" / "opencode" / "skills",
        "default-gemini": home / ".gemini" / "skills",
        "default-hermes": local_appdata / "hermes-review" / "skills",
        "override-pi": overrides["pi"] / "skills",
        "override-opencode": overrides["opencode"] / "skills",
        "override-gemini": overrides["gemini"] / ".gemini" / "skills",
        "override-hermes": overrides["hermes"] / "skills",
    }
    for label, root in roots.items():
        for role in CCB_ROLE_SKILLS:
            skill_md = root / role / "SKILL.md"
            assert skill_md.is_file(), f"{label} did not install {role}"
            frontmatter, body = _skill_frontmatter_and_body(skill_md)
            assert body == neutral[role], f"{label} {role} body diverged from the shared body"
            assert f"name: {role}" in frontmatter
            assert f"description: {CCB_ROLE_DESCRIPTIONS[role]}" in frontmatter
            if label.endswith("-pi"):
                assert "disable-model-invocation: true" in frontmatter
            else:
                assert "disable-model-invocation" not in frontmatter

    legacy_skills = home / ".hermes" / "skills"
    assert sorted(path.name for path in legacy_skills.iterdir()) == ["notes"]
    assert not (local_appdata / "hermes").exists()

    default_pi_foreign = home / ".pi" / "agent" / "skills" / "local-notes" / "SKILL.md"
    assert default_pi_foreign.is_file()
    assert default_pi_foreign.read_text(encoding="utf-8") == foreign_skill
    override_pi_foreign = overrides["pi"] / "skills" / "local-notes" / "SKILL.md"
    assert override_pi_foreign.is_file()
    assert override_pi_foreign.read_text(encoding="utf-8") == foreign_skill
    assert sorted(
        path.name for path in (home / ".pi" / "agent" / "skills").iterdir()
    ) == ["ccb-implementer", "ccb-lead", "ccb-ratifier", "local-notes"]


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


# --- N2: sessions read their own role instructions --------------------------------
#
# `ccb-role brief` only helps if the managed instruction blocks tell a top-level
# session to run it, so the paragraph is pinned in every block CCB writes.

BRIEF_PARAGRAPH_NEEDLES = (
    "If you are a top-level CCB session (`CCB_SESSION_ID` is set)",
    "never a subagent, helper or native agent spawned inside a session",
    "run `ccb-role brief` once at the start of each conversation",
    "a role CCB recorded for this session is the user's assignment",
    "If it fails, take no role",
    "An explicit assignment from the user later in the conversation overrides it",
)


def _brief_paragraph(text: str) -> str:
    """The paragraph exactly as one managed block spells it."""
    for line in text.splitlines():
        if line.startswith("If you are a top-level CCB session"):
            return line
    raise AssertionError("no `ccb-role brief` paragraph in this block")


def _assert_brief_paragraph(text: str, where: str) -> None:
    for needle in BRIEF_PARAGRAPH_NEEDLES:
        assert needle in text, f"{where} lacks the ccb-role brief paragraph ({needle!r})"


def _assert_no_personal_history_tool(text: str, where: str) -> None:
    assert "Memsearch" not in text, f"{where} still names Memsearch"
    assert "memsearch" not in text.lower(), f"{where} still names memsearch"
    assert "recalled session history" in text, f"{where} lost the history wording"


@pytest.mark.parametrize("mode", ["inline", "route"])
def test_claude_md_block_carries_the_ccb_role_brief_paragraph(
    tmp_path: Path, mode: str
) -> None:
    home = tmp_path / "home"
    install_prefix = _prepare_install_prefix(tmp_path)
    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=None,
        functions=("install_claude_md_config",),
        env_extra={"CCB_CLAUDE_MD_MODE": mode},
    )

    claude_text = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    template_text = (REPO_ROOT / "config" / "claude-md-ccb.md").read_text(encoding="utf-8")
    rules = home / ".claude" / "rules" / "ccb-config.md"
    if mode == "route":
        # Claude.md keeps only the pointer; the full text lands in rules/.
        assert "~/.claude/rules/ccb-config.md" in claude_text
        assert rules.is_file()
        full_text = rules.read_text(encoding="utf-8")
        _assert_brief_paragraph(full_text, "route-mode ccb-config.md")
        _assert_no_personal_history_tool(full_text, "route-mode ccb-config.md")
        assert _brief_paragraph(full_text) == _brief_paragraph(template_text)
    else:
        assert not rules.exists()
        _assert_brief_paragraph(claude_text, "inline-mode CLAUDE.md")
        _assert_no_personal_history_tool(claude_text, "inline-mode CLAUDE.md")
        assert _brief_paragraph(claude_text) == _brief_paragraph(template_text)


def test_codex_agents_block_carries_the_ccb_role_brief_paragraph(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    install_prefix = _prepare_install_prefix(tmp_path)
    _run_install_functions(
        home=home,
        install_prefix=install_prefix,
        codex_home=codex_home,
        functions=("install_agents_md_config",),
    )

    agents_text = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    _assert_brief_paragraph(agents_text, "Codex AGENTS.md")
    _assert_no_personal_history_tool(agents_text, "Codex AGENTS.md")
    # One paragraph, identically worded, in every managed block.
    assert _brief_paragraph(agents_text) == _brief_paragraph(
        (REPO_ROOT / "config" / "claude-md-ccb.md").read_text(encoding="utf-8")
    )


def test_no_managed_template_names_a_personal_history_tool() -> None:
    for name in ("claude-md-ccb.md", "claude-md-ccb-route.md", "agents-md-ccb.md"):
        text = (REPO_ROOT / "config" / name).read_text(encoding="utf-8")
        assert "Memsearch" not in text, f"{name} still names Memsearch"
        assert "memsearch" not in text.lower(), f"{name} still names memsearch"


def test_windows_copy_list_ships_the_role_skill_bodies() -> None:
    """`ccb-role brief` reads <prefix>/role_skills, so Windows must copy it.

    Asserted from the installer source because a functional run needs a
    PowerShell the installing host may not have.
    """
    source = INSTALL_PS1.read_text(encoding="utf-8")
    body = source.split("function Copy-ProjectItems", 1)[1]
    copy_list = body.split("foreach ($item in", 1)[1].split(")", 1)[0]
    for item in ('"ccb"', '"lib"', '"bin"', '"config"', '"role_skills"'):
        assert item in copy_list, f"Windows copy list is missing {item}"
