from pathlib import Path

import pytest
from ccb_start_config import normalize_start_config_data, provider_pairing_error
from live_sessions import read_inventory, resolve_local_target
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
    assert "CCB_LIVE_ID" not in launcher._provider_env_overrides("codex")


@pytest.mark.parametrize("tokens", [["codex", "codex"], ["codex,codex"], ["codex", "claude"], ["claude", "codex"]])
def test_launch_parser_preserves_occurrences_and_order(tokens):
    ccb = _load_ccb_module()
    expected = [part for token in tokens for part in token.split(",")]
    assert ccb._parse_providers(tokens) == expected
    assert ccb._parse_providers_with_cmd(tokens) == (expected, False, True)
    assert normalize_start_config_data({"providers": expected})["providers"] == expected


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
