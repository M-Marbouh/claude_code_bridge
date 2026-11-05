from pathlib import Path

import pytest
from ccb_start_config import normalize_start_config_data, provider_pairing_error
from live_sessions import read_inventory, resolve_local_target
import pane_registry
from pane_registry import load_registry_by_session_id
from test_ccb_agent_composition import _load_ccb_module


def test_pair_publishes_two_independent_files_without_provider_projection(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")
    launcher = ccb.AILauncher(providers=["codex", "codex"])
    launcher.runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(launcher, "_maybe_start_provider_daemon", lambda _: None)
    assert launcher._publish_live_inventory()
    inventory = read_inventory(load_registry_by_session_id(launcher.session_id))
    assert len(inventory.sessions) == 2
    assert all(not session.active for session in inventory.sessions)

    paths, markers, env_ids = [], [], []
    for index in range(2):
        launcher._launch_slot = index
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / f"account-{index}"))
        runtime = launcher._provider_runtime("codex")
        runtime.mkdir(parents=True)
        marker = launcher._provider_marker("codex")
        assert launcher._write_codex_session(runtime, None, runtime / "in", runtime / "out",
                                             pane_id=f"%{index}", pane_title_marker=marker)
        paths.append(launcher._project_session_file(".codex-session"))
        markers.append(marker)
        env_ids.append(launcher._provider_env_overrides("codex")["CCB_LIVE_ID"])

    assert paths[0] != paths[1]
    assert markers[0] != markers[1]
    assert env_ids[0] != env_ids[1]
    assert not (tmp_path / ".ccb" / ".codex-session").exists()
    record = load_registry_by_session_id(launcher.session_id)
    assert not (record.get("providers") or {}).get("codex")
    sessions = read_inventory(record).sessions
    for index, session in enumerate(sessions):
        assert session.active
        assert session.pane_id == f"%{index}"
        assert Path(session.session_file) == paths[index]
        data = launcher._read_json_file(paths[index])
        assert data["codex_session_root"] == str(tmp_path / f"account-{index}" / "sessions")
        assert resolve_local_target(sessions, provider="codex", caller=session).session == sessions[1 - index]

    monkeypatch.setattr(ccb, "_cleanup_stale_runtime_dirs", lambda **_: 0)
    monkeypatch.setattr(ccb, "_cleanup_tmpclaude_artifacts", lambda: 0)
    monkeypatch.setattr(ccb, "_shrink_ccb_logs", lambda: 0)
    monkeypatch.setattr(launcher, "_set_tmux_ui_active", lambda _: None)
    monkeypatch.setattr(ccb, "shutdown_daemon", lambda *_: None)
    launcher.cleanup(kill_panes=False, remove_runtime=False, quiet=True)
    after = read_inventory(load_registry_by_session_id(launcher.session_id)).sessions
    assert len(after) == 2
    assert all(not session.active for session in after)
    assert all(not launcher._read_json_file(path)["active"] for path in paths)


def test_unique_provider_keeps_legacy_paths_and_marker(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    launcher = ccb.AILauncher(providers=["codex", "claude"])
    assert launcher._live_slots == []
    assert launcher._provider_runtime("codex") == launcher.runtime_dir / "codex"
    assert launcher._project_session_file(".codex-session") == tmp_path / ".ccb" / ".codex-session"
    assert launcher._provider_marker("codex") == f"CCB-Codex-{launcher.project_id[:8]}"
    assert launcher._provider_marker("claude") == f"CCB-Claude-{launcher.project_id[:8]}"
    assert launcher._claude_marker() == f"CCB-Claude-{launcher.project_id[:8]}"
    assert "CCB_LIVE_ID" not in launcher._provider_env_overrides("codex")


def test_claude_live_slot_uses_suffixed_marker_and_binding(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    launcher = ccb.AILauncher(providers=["codex", "codex", "claude"])
    launcher._launch_slot = 2
    launcher.anchor_provider = "claude"
    marker = launcher._provider_marker("claude")
    live_id = launcher._live_slots[2]
    assert marker == f"CCB-Claude-{launcher.project_id[:8]}-{live_id}"

    launcher._write_local_claude_session(
        pane_id="%2", pane_title_marker=marker, terminal="tmux", live_id=live_id
    )
    binding = launcher._read_json_file(launcher._claude_session_file())
    assert binding["pane_title_marker"] == marker
    assert binding["live_id"] == live_id


def test_wezterm_claude_anchor_keeps_short_marker_without_live_id(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    launcher = ccb.AILauncher(providers=["codex", "codex", "claude"])
    launcher.anchor_provider = "claude"
    launcher.terminal_type = "wezterm"
    assert launcher._claude_marker() == f"CCB-Claude-{launcher.project_id[:8]}"
    assert launcher._claude_live_id() is None


def test_claude_route_requires_exact_suffixed_marker_after_title_overwrite(monkeypatch):
    class _Backend:
        def is_alive(self, pane_id):
            return pane_id == "%2"

        def find_pane_by_title_marker(self, marker, _work_dir):
            # Simulates tmux matching its stable option after Claude changes
            # the visible pane title.
            return "%2" if marker == expected else None

    expected = "CCB-Claude-project-live-slot"
    monkeypatch.setattr(pane_registry, "get_backend_for_session", lambda _data: _Backend())
    base = {"terminal": "tmux", "work_dir": ".", "providers": {"claude": {
        "pane_id": "%2", "pane_title_marker": expected}}}
    assert pane_registry._provider_pane_alive(base, "claude")

    for marker, pane_id in (("CCB-Claude-project", "%2"),
                            ("CCB-Claude-project-wrong", "%2"),
                            (expected, "%3")):
        candidate = {"terminal": "tmux", "work_dir": ".", "providers": {"claude": {
            "pane_id": pane_id, "pane_title_marker": marker}}}
        assert not pane_registry._provider_pane_alive(candidate, "claude")


@pytest.mark.parametrize("tokens", [["codex", "codex"], ["codex,codex"], ["codex", "claude"], ["claude", "codex"]])
def test_launch_parser_preserves_occurrences_and_order(tokens):
    ccb = _load_ccb_module()
    expected = [part for token in tokens for part in token.split(",")]
    assert ccb._parse_providers(tokens) == expected
    assert ccb._parse_providers_with_cmd(tokens) == (expected, False, True)
    assert normalize_start_config_data({"providers": expected})["providers"] == expected


@pytest.mark.parametrize(
    ("cli_providers", "config_text", "expect_warning"),
    [
        (["gemini"], None, True),
        ([], "codex,gemini", True),
        (["codex"], None, False),
        ([], None, False),
    ],
)
def test_cmd_start_warns_only_when_gemini_is_requested(
    monkeypatch, tmp_path, capsys, cli_providers, config_text, expect_warning
):
    ccb = _load_ccb_module()
    project_config = tmp_path / ".ccb"
    project_config.mkdir()
    if config_text is not None:
        (project_config / "ccb.config").write_text(config_text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")
    monkeypatch.setattr(ccb, "remediate_ccb_permissions", lambda *_: (True, []))

    class _Lock:
        def __init__(self, *_args, **_kwargs):
            pass

        def try_acquire(self):
            return True

        def release(self):
            pass

    class _Launcher:
        def __init__(self, *, providers, **_kwargs):
            self.providers = providers

        def run_up(self):
            return 0

    monkeypatch.setattr(ccb, "ProviderLock", _Lock)
    monkeypatch.setattr(ccb, "AILauncher", _Launcher)

    result = ccb.cmd_start(type("Args", (), {"providers": cli_providers, "resume": False, "auto": False})())

    assert result == 0
    warning = "Gemini CLI was retired for individual accounts on 2026-06-18; enterprise accounts still work."
    warning_count = capsys.readouterr().err.count(warning)
    assert warning_count == (1 if expect_warning else 0)


@pytest.mark.parametrize("providers", [["codex"] * 3, ["claude"] * 2,
                                       ["gemini"] * 2, ["opencode"] * 2])
def test_unsupported_duplicates_refuse_before_launcher_mutation(monkeypatch, providers):
    ccb = _load_ccb_module()
    monkeypatch.setattr(ccb, "compute_ccb_project_id", lambda *_: pytest.fail("must refuse before project access"))
    assert not ccb._parse_providers_with_cmd(providers)[2]
    with pytest.raises(ValueError):
        ccb.AILauncher(providers=providers)


@pytest.mark.parametrize("providers", [
    ["codex", "codex", "claude"],
    ["codex", "claude", "codex"],
    ["claude", "codex", "codex"],
    ["gemini", "codex", "claude", "codex", "opencode"],
])
def test_codex_pair_with_unique_provider_is_accepted(providers):
    assert provider_pairing_error(providers) == ""


def test_three_member_inventory_has_distinct_live_bindings(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")
    launcher = ccb.AILauncher(providers=["codex", "codex", "claude"])
    launcher.runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(launcher, "_maybe_start_provider_daemon", lambda _: None)

    assert launcher._publish_live_inventory()
    sessions = read_inventory(load_registry_by_session_id(launcher.session_id)).sessions

    assert [session.provider for session in sessions] == ["codex", "codex", "claude"]
    assert len({session.live_id for session in sessions}) == 3
    assert len({session.auth_token for session in sessions}) == 3
    assert len({session.session_file for session in sessions}) == 3
    assert sessions[0].session_file != sessions[1].session_file


@pytest.mark.parametrize("providers", [
    ["codex", "codex"],
    ["codex", "codex", "claude"],
    ["claude", "codex", "codex"],
])
def test_pair_resume_refuses_without_disabling_unique_resume(monkeypatch, providers):
    ccb = _load_ccb_module()
    monkeypatch.setattr(ccb, "compute_ccb_project_id", lambda *_: pytest.fail("must refuse before project access"))
    with pytest.raises(ValueError, match="native resume"):
        ccb.AILauncher(providers=providers, resume=True)
    assert not provider_pairing_error(["codex", "claude"], resume=True)


def test_codex_launch_args_apply_to_each_pair_member(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    launcher = ccb.AILauncher(providers=["codex", "codex"],
                              launch_args={"codex": "--model gpt-5.6-luna"})
    assert launcher._build_codex_start_cmd().endswith("--model gpt-5.6-luna")


# --- claim credentials for launches without a live-session inventory --------------

import json
import os
import time

from ccb_start_config import provider_pairing_error as _pairing_error  # noqa: F401  (re-exported for clarity)
import live_roles


def _non_pair_launcher(ccb, monkeypatch, tmp_path, providers):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(exist_ok=True)
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")
    launcher = ccb.AILauncher(providers=providers)
    launcher.runtime_dir = tmp_path / "runtime"
    launcher.runtime_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(launcher, "_maybe_start_provider_daemon", lambda _: None)
    return launcher


def test_non_pair_launch_exports_only_member_credentials(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    launcher = _non_pair_launcher(ccb, monkeypatch, tmp_path, ["claude", "pi"])

    claude_env = launcher._provider_env_overrides("claude")
    pi_env = launcher._provider_env_overrides("pi")

    for env in (claude_env, pi_env):
        assert env["CCB_MEMBER_ID"] and env["CCB_MEMBER_TOKEN"]
        assert "CCB_LIVE_ID" not in env
        assert "CCB_LIVE_TOKEN" not in env
    assert claude_env["CCB_MEMBER_ID"] != pi_env["CCB_MEMBER_ID"]
    assert claude_env["CCB_MEMBER_TOKEN"] != pi_env["CCB_MEMBER_TOKEN"]
    assert claude_env["CCB_CALLER"] == "claude"
    assert pi_env["CCB_CALLER"] == "pi"
    # No authoritative inventory and no routing identity is published.
    assert launcher._publish_live_inventory() is True
    assert load_registry_by_session_id(launcher.session_id) is None


def test_pair_launch_member_credential_is_the_live_credential(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")
    launcher = ccb.AILauncher(providers=["codex", "codex"])
    launcher.runtime_dir = tmp_path / "runtime"
    launcher.runtime_dir.mkdir()
    launcher._launch_slot = 1

    env = launcher._provider_env_overrides("codex")

    assert env["CCB_MEMBER_ID"] == env["CCB_LIVE_ID"] == launcher._live_slots[1]
    assert env["CCB_MEMBER_TOKEN"] == env["CCB_LIVE_TOKEN"] == launcher._live_tokens[1]
    assert launcher._member_registry_fields("codex") == {}


def _write_provider_session(launcher, provider: str):
    runtime = launcher.runtime_dir / provider
    runtime.mkdir(parents=True, exist_ok=True)
    if provider == "claude":
        launcher._write_local_claude_session(
            pane_id="%1", pane_title_marker="CCB-Claude-x", terminal="tmux"
        )
        return True
    if provider == "codex":
        return launcher._write_codex_session(
            runtime, None, runtime / "in", runtime / "out",
            pane_id="%1", pane_title_marker="CCB-Codex-x",
        )
    if provider == "hermes":
        return launcher._write_hermes_session(
            runtime, None, pane_id="%1", pane_title_marker="CCB-Hermes-x"
        )
    if provider == "pi":
        return launcher._write_pi_session(runtime, pane_id="%1", pane_title_marker="CCB-Pi-x")
    if provider == "gemini":
        return launcher._write_gemini_session(
            runtime, None, pane_id="%1", pane_title_marker="CCB-Gemini-x"
        )
    if provider == "opencode":
        return launcher._write_opencode_session(
            runtime, None, pane_id="%1", pane_title_marker="CCB-OpenCode-x"
        )
    raise AssertionError(provider)


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini", "opencode", "hermes", "pi"])
def test_every_writer_records_the_member_credential_on_its_provider_entry(
    monkeypatch, tmp_path, provider
):
    ccb = _load_ccb_module()
    launcher = _non_pair_launcher(ccb, monkeypatch, tmp_path, [provider])
    env = launcher._provider_env_overrides(provider)

    assert _write_provider_session(launcher, provider)

    record = load_registry_by_session_id(launcher.session_id)
    assert "live_sessions" not in record
    # The credential lives only in the private registry record.
    registry_mode = pane_registry.registry_path_for_session(launcher.session_id).stat().st_mode & 0o777
    assert registry_mode == 0o600
    entry = record["providers"][provider]
    assert entry["member_id"] == env["CCB_MEMBER_ID"]
    assert entry["member_token"] == env["CCB_MEMBER_TOKEN"]
    # A member token never reaches the project session file.
    session_file = launcher._project_session_file(f".{provider}-session")
    assert env["CCB_MEMBER_TOKEN"] not in session_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("resume", [False, True])
def test_fresh_launch_gets_fresh_credentials_and_no_inherited_claim(
    monkeypatch, tmp_path, resume
):
    ccb = _load_ccb_module()
    first = _non_pair_launcher(ccb, monkeypatch, tmp_path, ["pi"])
    first_env = first._provider_env_overrides("pi")
    assert _write_provider_session(first, "pi")
    record = load_registry_by_session_id(first.session_id)
    record["ccb_pid"] = os.getpid()
    panel = {"pane_id": "%1", "pane_title_marker": "CCB-Pi-x"}
    record["providers"]["pi"].update(panel)
    pane_registry.registry_path_for_session(first.session_id).write_text(
        json.dumps(record), encoding="utf-8"
    )

    class _Backend:
        def is_alive(self, pane_id):
            return True

        def find_pane_by_title_marker(self, marker, _cwd=""):
            return "%1"

    monkeypatch.setattr(pane_registry, "get_backend_for_session", lambda _rec: _Backend())
    ok, detail = live_roles.set_own_role(
        first_env["CCB_MEMBER_ID"], first_env["CCB_MEMBER_TOKEN"], "lead"
    )
    assert ok, detail

    # Launcher ids embed a whole second, so step the clock to model a real
    # relaunch rather than a same-second second process.
    real_time = time.time
    monkeypatch.setattr(ccb.time, "time", lambda: real_time() + 10)
    second = ccb.AILauncher(providers=["pi"], resume=resume)
    monkeypatch.setattr(ccb.time, "time", real_time)
    second.runtime_dir = tmp_path / "runtime-2"
    second.runtime_dir.mkdir()
    second_env = second._provider_env_overrides("pi")

    assert second_env["CCB_MEMBER_ID"] != first_env["CCB_MEMBER_ID"]
    assert second_env["CCB_MEMBER_TOKEN"] != first_env["CCB_MEMBER_TOKEN"]
    assert live_roles.roles_path_for_launch(first.session_id).exists()
    assert not live_roles.roles_path_for_launch(second.session_id).exists()


def test_claiming_a_role_publishes_no_routing_identity(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    launcher = _non_pair_launcher(ccb, monkeypatch, tmp_path, ["pi"])
    env = launcher._provider_env_overrides("pi")
    assert _write_provider_session(launcher, "pi")
    record = load_registry_by_session_id(launcher.session_id)
    record["ccb_pid"] = os.getpid()
    record["providers"]["pi"].update({"pane_id": "%1", "pane_title_marker": "CCB-Pi-x"})
    path = pane_registry.registry_path_for_session(launcher.session_id)
    path.write_text(json.dumps(record), encoding="utf-8")
    before = json.loads(path.read_text(encoding="utf-8"))
    before.pop("updated_at")

    class _Backend:
        def is_alive(self, pane_id):
            return True

        def find_pane_by_title_marker(self, marker, _cwd=""):
            return "%1"

    monkeypatch.setattr(pane_registry, "get_backend_for_session", lambda _rec: _Backend())

    assert live_roles.set_own_role(env["CCB_MEMBER_ID"], env["CCB_MEMBER_TOKEN"], "lead")[0]

    after = json.loads(path.read_text(encoding="utf-8"))
    after.pop("updated_at", None)
    assert after == before
    assert "live_sessions" not in after
    assert launcher._provider_env_overrides("pi").get("CCB_LIVE_ID") is None


@pytest.mark.parametrize("slot", [0, 1, 2])
def test_pair_member_credential_matches_the_slot_in_any_position(monkeypatch, tmp_path, slot):
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")
    launcher = ccb.AILauncher(providers=["claude", "codex", "codex"])
    launcher._launch_slot = slot
    provider = launcher.providers[slot]

    env = launcher._provider_env_overrides(provider)

    assert env["CCB_LIVE_ID"] == launcher._live_slots[slot]
    assert env["CCB_LIVE_TOKEN"] == launcher._live_tokens[slot]
    assert env["CCB_MEMBER_ID"] == env["CCB_LIVE_ID"]
    assert env["CCB_MEMBER_TOKEN"] == env["CCB_LIVE_TOKEN"]
    assert launcher._member_registry_fields(provider) == {}


# --- launch credentials never reach a project session file ------------------------

PROVIDERS = ("claude", "codex", "gemini", "opencode", "hermes", "pi")


class _RecordingTmux:
    """Stand-in backend: records commands, never touches a real terminal."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def create_pane(self, cmd, cwd, direction="right", percent=50, parent_pane=None):
        self.commands.append(cmd)
        return "%90"

    def respawn_pane(self, pane_id, cmd, cwd=None, **_kwargs):
        self.commands.append(cmd)

    def set_pane_title(self, pane_id, title):
        return None

    def set_pane_user_option(self, pane_id, option, value):
        return None

    def pane_exists(self, pane_id):
        return True

    def get_current_pane_id(self):
        return "%1"

    def is_alive(self, pane_id):
        return True


def _session_texts(launcher, ccb) -> dict[str, str]:
    texts = {}
    for provider in launcher.providers:
        path = launcher._project_session_file(f".{provider}-session")
        if path.exists():
            texts[str(path)] = path.read_text(encoding="utf-8")
    return texts


@pytest.mark.parametrize("provider", PROVIDERS)
def test_launch_commands_keep_the_credential_out_of_the_persisted_form(
    monkeypatch, tmp_path, provider
):
    ccb = _load_ccb_module()
    launcher = _non_pair_launcher(ccb, monkeypatch, tmp_path, [provider])
    env = launcher._provider_env_overrides(provider)

    executed, persisted = launcher._launch_commands(provider, "agent-command")

    assert env["CCB_MEMBER_TOKEN"] in executed
    assert env["CCB_MEMBER_ID"] in executed
    assert env["CCB_MEMBER_TOKEN"] not in persisted
    assert env["CCB_MEMBER_ID"] not in persisted
    assert persisted.endswith("agent-command")


@pytest.mark.parametrize("provider", ["gemini", "opencode", "hermes", "pi"])
def test_no_provider_session_file_contains_the_member_token(monkeypatch, tmp_path, provider):
    ccb = _load_ccb_module()
    launcher = _non_pair_launcher(ccb, monkeypatch, tmp_path, [provider])
    backend = _RecordingTmux()
    monkeypatch.setattr(ccb, "TmuxBackend", lambda: backend)
    env = launcher._provider_env_overrides(provider)

    starter = getattr(launcher, f"_start_{provider}_tmux")
    assert starter() is not None

    texts = _session_texts(launcher, ccb)
    assert texts
    for text in texts.values():
        assert env["CCB_MEMBER_TOKEN"] not in text
    # ... while the executed pane command does carry it.
    assert any(env["CCB_MEMBER_TOKEN"] in cmd for cmd in backend.commands)


def test_codex_and_claude_session_files_never_contain_a_token(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    launcher = _non_pair_launcher(ccb, monkeypatch, tmp_path, ["codex", "claude"])
    runtime = launcher.runtime_dir / "codex"
    runtime.mkdir(parents=True, exist_ok=True)
    codex_env = launcher._provider_env_overrides("codex")
    agent_cmd = launcher._compose_agent_shell(
        "codex",
        ccb.ShellAgentCommand(prelude="", agent_command=launcher._build_codex_start_cmd()),
    )
    _executed, persisted = launcher._launch_commands("codex", agent_cmd)
    assert launcher._write_codex_session(
        runtime, None, runtime / "in", runtime / "out",
        pane_id="%1", pane_title_marker="CCB-Codex-x", codex_start_cmd=persisted,
    )
    launcher._write_local_claude_session(pane_id="%2", pane_title_marker="CCB-Claude-x", terminal="tmux")
    claude_env = launcher._provider_env_overrides("claude")

    texts = _session_texts(launcher, ccb)
    assert texts
    for text in texts.values():
        for env in (codex_env, claude_env):
            assert env["CCB_MEMBER_TOKEN"] not in text


def test_pair_session_files_never_contain_the_live_token(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")
    launcher = ccb.AILauncher(providers=["codex", "codex", "claude"])
    launcher.runtime_dir = tmp_path / "runtime"
    launcher.runtime_dir.mkdir()
    monkeypatch.setattr(launcher, "_maybe_start_provider_daemon", lambda _: None)
    assert launcher._publish_live_inventory()

    tokens = []
    for slot in range(2):
        launcher._launch_slot = slot
        runtime = launcher._provider_runtime("codex")
        runtime.mkdir(parents=True)
        env = launcher._provider_env_overrides("codex")
        tokens.append(env["CCB_LIVE_TOKEN"])
        agent_cmd = launcher._compose_agent_shell(
            "codex",
            ccb.ShellAgentCommand(prelude="", agent_command=launcher._build_codex_start_cmd()),
        )
        _executed, persisted = launcher._launch_commands("codex", agent_cmd)
        assert env["CCB_LIVE_TOKEN"] in _executed
        assert env["CCB_LIVE_TOKEN"] not in persisted
        assert launcher._write_codex_session(
            runtime, None, runtime / "in", runtime / "out",
            pane_id=f"%{slot}", pane_title_marker=launcher._provider_marker("codex"),
            codex_start_cmd=persisted,
        )
        session_file = launcher._project_session_file(".codex-session")
        text = session_file.read_text(encoding="utf-8")
        for token in launcher._live_tokens:
            assert token not in text

    launcher._launch_slot = 2
    launcher.anchor_provider = "claude"
    launcher._write_local_claude_session(
        pane_id="%2",
        pane_title_marker=launcher._claude_marker(),
        terminal="tmux",
        live_id=launcher._claude_live_id(),
    )
    claude_text = launcher._project_session_file(".claude-session").read_text(encoding="utf-8")
    for token in launcher._live_tokens:
        assert token not in claude_text


def test_wezterm_persisted_start_command_has_no_credential(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    launcher = _non_pair_launcher(ccb, monkeypatch, tmp_path, ["pi"])
    backend = _RecordingTmux()
    monkeypatch.setattr(ccb, "WeztermBackend", lambda: backend)
    env = launcher._provider_env_overrides("pi")

    assert launcher._start_provider_wezterm("pi") is not None

    session_file = launcher._project_session_file(".pi-session")
    text = session_file.read_text(encoding="utf-8")
    assert env["CCB_MEMBER_TOKEN"] not in text
    assert env["CCB_MEMBER_ID"] not in text
    assert "start_cmd" in text


# --- every real writer's binding can carry a role claim --------------------------

# The keys each writer emits that the membership check binds on. Read from the
# writer code, not invented: all six write session_id/ccb_project_id/pane_id/
# pane_title_marker/active at launch.
WRITER_BINDING_KEYS = {
    "claude": {"session_id", "ccb_project_id", "pane_id", "pane_title_marker", "active"},
    "codex": {"session_id", "ccb_project_id", "pane_id", "pane_title_marker", "active"},
    "gemini": {"session_id", "ccb_project_id", "pane_id", "pane_title_marker", "active"},
    "opencode": {"session_id", "ccb_project_id", "pane_id", "pane_title_marker", "active"},
    "hermes": {"session_id", "ccb_project_id", "pane_id", "pane_title_marker", "active"},
    "pi": {"session_id", "ccb_project_id", "pane_id", "pane_title_marker", "active"},
}


class _AlivePaneBackend:
    def __init__(self, pane_id: str, marker: str):
        self.pane_id = pane_id
        self.marker = marker

    def is_alive(self, pane_id):
        return str(pane_id) == self.pane_id

    def find_pane_by_title_marker(self, marker, cwd=""):
        return self.pane_id if marker == self.marker else None


@pytest.mark.parametrize("provider", sorted(WRITER_BINDING_KEYS))
def test_a_real_writer_binding_can_claim_and_show_a_role(monkeypatch, tmp_path, provider):
    ccb = _load_ccb_module()
    launcher = _non_pair_launcher(ccb, monkeypatch, tmp_path, [provider])
    assert _write_provider_session(launcher, provider)

    record = load_registry_by_session_id(launcher.session_id)
    record["ccb_pid"] = os.getpid()
    entry = record["providers"][provider]
    data = json.loads(pane_registry.registry_path_for_session(launcher.session_id).read_text(encoding="utf-8"))
    binding = json.loads(Path(entry["session_file"]).read_text(encoding="utf-8"))
    assert WRITER_BINDING_KEYS[provider] <= set(binding)
    pane_registry.registry_path_for_session(launcher.session_id).write_text(
        json.dumps(data), encoding="utf-8"
    )

    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _AlivePaneBackend(str(entry["pane_id"]), str(entry["pane_title_marker"])),
    )

    ok, detail = live_roles.set_own_role(entry["member_id"], entry["member_token"], "lead")

    assert ok, detail
    assert live_roles.roles_for_record(data) == {entry["member_id"]: "lead"}
