"""Roles pre-assigned by ccb.config are claimed by the launcher, per occurrence.

The config reader is exercised through its real entry points
(`load_start_config`, `normalize_start_config_data`), and the claim path runs
through `AILauncher` with a stubbed `set_own_role` so the credential and
occurrence mapping is observable without a live launch.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import live_roles
import pane_registry
from live_sessions import read_inventory
from pane_registry import load_registry_by_session_id
from ccb_start_config import (
    load_start_config,
    normalize_start_config_data,
    provider_pairing_error,
    resolve_roles_for_providers,
)
from test_ccb_agent_composition import _load_ccb_module


REAL_ANNOTATED = "claude=lead, codex=ratifier, codex=implementer"
REAL_JSON = {
    "providers": ["claude", "pi", "codex"],
    "role_policy": "enforce",
    "roles": {"claude": "lead", "codex": "ratifier", "pi": "implementer"},
}


def _load_config(tmp_path: Path, text: str) -> dict:
    config_dir = tmp_path / ".ccb"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "ccb.config").write_text(text, encoding="utf-8")
    return load_start_config(tmp_path).data


# --- config parsing ---------------------------------------------------------------


def test_real_owner_plain_text_config_parses_exactly_as_before(tmp_path: Path) -> None:
    data = _load_config(tmp_path, "claude, codex, codex\n")
    assert data == {"providers": ["claude", "codex", "codex"]}
    assert "roles" not in data
    assert "roles_error" not in data


def test_real_annotated_plain_text_config_assigns_each_occurrence(tmp_path: Path) -> None:
    data = _load_config(tmp_path, REAL_ANNOTATED + "\n")
    assert data["providers"] == ["claude", "codex", "codex"]
    assert data["roles"] == {"claude": ["lead"], "codex": ["ratifier", "implementer"]}
    assert "roles_error" not in data

    assignments, error = resolve_roles_for_providers(data, data["providers"])
    assert error == ""
    assert assignments == ["lead", "ratifier", "implementer"]
    # The anchor is the last provider, i.e. the SECOND codex occurrence.
    assert data["providers"][-1] == "codex"
    assert assignments[-1] == "implementer"
    assert assignments[1] == "ratifier"


def test_real_json_launch_shape(tmp_path: Path) -> None:
    data = _load_config(tmp_path, json.dumps(REAL_JSON) + "\n")
    assert data["providers"] == ["claude", "pi", "codex"]
    assert data["role_policy"] == "enforce"
    assert data["roles"] == {"claude": ["lead"], "codex": ["ratifier"], "pi": ["implementer"]}

    assignments, error = resolve_roles_for_providers(data, data["providers"])
    assert error == ""
    assert assignments == ["lead", "implementer", "ratifier"]
    assert assignments[-1] == "ratifier"  # anchor = codex


def test_plain_text_cmd_and_annotations_coexist(tmp_path: Path) -> None:
    data = _load_config(tmp_path, "claude=lead, codex=ratifier, cmd\n")
    assert data["providers"] == ["claude", "codex"]
    assert data["cmd"] is True
    assert data["roles"] == {"claude": ["lead"], "codex": ["ratifier"]}


def test_annotations_keep_unassigned_occurrences_in_place(tmp_path: Path) -> None:
    data = _load_config(tmp_path, "claude=lead, codex, codex=implementer\n")
    assert data["providers"] == ["claude", "codex", "codex"]
    assert data["roles"] == {"claude": ["lead"], "codex": ["", "implementer"]}
    assignments, error = resolve_roles_for_providers(data, data["providers"])
    assert error == ""
    assert assignments == ["lead", "", "implementer"]


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("claude, codex=\n", "has no role after '='"),
        ("claude, =lead\n", "has no provider name before '='"),
        ("claude, codex=a=b\n", "invalid role name"),
        ("claude, codex=Not_A_Role\n", "invalid role name"),
        ("claude, cmd=lead\n", "the cmd pane cannot be given a role"),
        ("claude, nope=lead\n", "names unknown provider"),
    ],
)
def test_malformed_plain_text_annotation_is_an_error(tmp_path: Path, text, needle) -> None:
    data = _load_config(tmp_path, text)
    assert "roles" not in data
    assert needle in data["roles_error"]
    # The provider list is never silently trimmed of the broken token.
    assert data["providers"] == ["claude"]


@pytest.mark.parametrize(
    ("roles", "needle"),
    [
        ({"pi": "lead"}, "which this launch does not start"),
        ({"codex": ["ratifier"]}, "one role per codex session (2), got 1"),
        ({"codex": ["lead", "lead"]}, "assigned to more than one session"),
        ({"codex": ["ratifier", "implementer"], "claude": "lead"}, "") ,
    ],
)
def test_roles_must_match_the_effective_provider_list(roles, needle) -> None:
    data = {"providers": ["claude", "codex", "codex"], "roles": roles}
    normalized = normalize_start_config_data(data)
    assignments, error = resolve_roles_for_providers(normalized, normalized["providers"])
    if needle:
        assert error and needle in error
        assert assignments == []
    else:
        assert error == ""
        assert assignments == ["lead", "ratifier", "implementer"]


def test_bad_role_name_in_json_is_a_config_error() -> None:
    normalized = normalize_start_config_data(
        {"providers": ["claude"], "roles": {"claude": "Not A Role"}}
    )
    assert normalized["roles_error"]
    assignments, error = resolve_roles_for_providers(normalized, normalized["providers"])
    assert assignments == []
    assert "invalid role name" in error


def test_roles_given_twice_is_a_config_error(tmp_path: Path) -> None:
    data = _load_config(
        tmp_path,
        json.dumps(
            {"providers": ["claude=lead", "pi"], "roles": {"claude": "lead", "pi": "implementer"}}
        ),
    )
    assert "roles" not in data
    assert data["roles_error"] == "roles given both in providers and in roles; use one"
    assignments, error = resolve_roles_for_providers(data, data["providers"])
    assert assignments == []
    assert error == "roles given both in providers and in roles; use one"


def test_provider_tokens_without_a_role_do_not_conflict_with_roles(tmp_path: Path) -> None:
    # Unannotated provider tokens plus a roles object is the normal JSON form.
    data = _load_config(
        tmp_path,
        json.dumps({"providers": ["claude", "pi"], "roles": {"claude": "lead", "pi": "implementer"}}),
    )
    assert "roles_error" not in data
    assert resolve_roles_for_providers(data, data["providers"]) == (["lead", "implementer"], "")


@pytest.mark.parametrize(
    "config_text",
    [
        # Plain text: `pi` is an unannotated occurrence, so it adds a blank
        # entry that says nothing about that provider.
        "claude=lead, pi\n",
        # The equivalent JSON says the same thing without any token for `pi`.
        json.dumps({"providers": ["claude", "pi"], "roles": {"claude": "lead"}}),
    ],
)
def test_dropping_an_unassigned_provider_by_cli_override_is_not_an_error(
    tmp_path: Path, config_text
) -> None:
    data = _load_config(tmp_path, config_text)
    assignments, error = resolve_roles_for_providers(data, ["claude"])
    assert error == ""
    assert assignments == ["lead"]


@pytest.mark.parametrize(
    "config_text",
    [
        "claude=lead, codex\n",
        json.dumps({"providers": ["claude", "codex"], "roles": {"claude": "lead"}}),
    ],
)
def test_occurrences_added_by_cli_override_leave_the_rest_blank(
    tmp_path: Path, config_text
) -> None:
    # The config names two providers; the CLI expands codex to a pair. The
    # blank-only codex entry must not be indexed (that used to raise IndexError).
    data = _load_config(tmp_path, config_text)
    assignments, error = resolve_roles_for_providers(data, ["claude", "codex", "codex"])
    assert error == ""
    assert assignments == ["lead", "", ""]


def test_cmd_start_expands_occurrences_without_indexing_blank_roles(
    monkeypatch, tmp_path, capsys
) -> None:
    ccb = _load_ccb_module()
    result, constructed = _cmd_start(
        monkeypatch, tmp_path, "claude=lead, codex\n", ["claude", "codex", "codex"], ccb
    )
    assert result == 0
    assert constructed and constructed[0]["roles"] == ["lead", "", ""]
    assert capsys.readouterr().err == ""


def test_cli_override_that_drops_a_roled_provider_is_refused() -> None:
    normalized = normalize_start_config_data(dict(REAL_JSON))
    assignments, error = resolve_roles_for_providers(normalized, ["claude", "codex"])
    assert assignments == []
    assert "pi" in error and "does not start" in error


@pytest.mark.parametrize("blank", [None, ""])
def test_a_null_role_value_means_unassigned(blank) -> None:
    normalized = normalize_start_config_data(
        {"providers": ["codex", "codex"], "roles": {"codex": blank}}
    )
    assert "roles_error" not in normalized
    assignments, error = resolve_roles_for_providers(normalized, normalized["providers"])
    assert error == ""
    assert assignments == ["", ""]


# --- cmd_start refuses before any launcher/pane work ------------------------------


def _cmd_start(monkeypatch, tmp_path: Path, config_text: str, cli_providers, ccb):
    project_config = tmp_path / ".ccb"
    project_config.mkdir(exist_ok=True)
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

    constructed: list[dict] = []

    class _Launcher:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def run_up(self):
            return 0

    monkeypatch.setattr(ccb, "ProviderLock", _Lock)
    monkeypatch.setattr(ccb, "AILauncher", _Launcher)
    args = SimpleNamespace(providers=cli_providers, resume=False, auto=False)
    return ccb.cmd_start(args), constructed


def test_cmd_start_passes_resolved_roles_to_the_launcher(monkeypatch, tmp_path, capsys) -> None:
    ccb = _load_ccb_module()
    result, constructed = _cmd_start(monkeypatch, tmp_path, REAL_ANNOTATED + "\n", [], ccb)
    assert result == 0
    assert constructed and constructed[0]["roles"] == ["lead", "ratifier", "implementer"]
    assert capsys.readouterr().err == ""


def test_cmd_start_accepts_a_cli_override_that_drops_an_unassigned_provider(
    monkeypatch, tmp_path, capsys
) -> None:
    ccb = _load_ccb_module()
    result, constructed = _cmd_start(monkeypatch, tmp_path, "claude=lead, pi\n", ["claude"], ccb)
    assert result == 0
    assert constructed and constructed[0]["roles"] == ["lead"]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("config_text", "cli_providers", "needle"),
    [
        (
            json.dumps({"providers": ["claude", "codex", "codex"], "roles": {"codex": ["ratifier"]}}),
            [],
            "one role per codex session (2), got 1",
        ),
        ("claude=lead, claude=ratifier\n", [], "Only two Codex sessions may share a provider"),
        ("claude=lead, codex=ratifier, codex=ratifier\n", [], "assigned to more than one session"),
        ("claude, codex=\n", [], "has no role after '='"),
        (
            json.dumps({"providers": ["claude=lead"], "roles": {"claude": "lead"}}),
            [],
            "roles given both in providers and in roles; use one",
        ),
        (
            json.dumps({"providers": ["claude", "pi"], "roles": {"claude": "lead", "pi": "implementer"}}),
            ["claude"],
            "which this launch does not start",
        ),
    ],
)
def test_cmd_start_refuses_invalid_roles_before_creating_a_launcher(
    monkeypatch, tmp_path, capsys, config_text, cli_providers, needle
) -> None:
    ccb = _load_ccb_module()
    monkeypatch.setattr(ccb, "compute_ccb_project_id", lambda *_: pytest.fail("must refuse before project access"))
    result, constructed = _cmd_start(monkeypatch, tmp_path, config_text, cli_providers, ccb)
    assert result == 2
    assert constructed == []
    assert needle in capsys.readouterr().err


# --- claim mapping and failure reporting ------------------------------------------


def _launcher_for_claims(monkeypatch, tmp_path: Path, providers, roles):
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir(exist_ok=True)
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")
    launcher = ccb.AILauncher(providers=providers, roles=roles)
    launcher.runtime_dir = tmp_path / "runtime"
    launcher.runtime_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(launcher, "_maybe_start_provider_daemon", lambda _: None)
    return ccb, launcher


def test_role_claim_maps_original_occurrence_despite_reversed_pane_creation(
    monkeypatch, tmp_path
) -> None:
    ccb, launcher = _launcher_for_claims(
        monkeypatch, tmp_path, ["claude", "codex", "codex"], ["lead", "ratifier", "implementer"]
    )
    tokens_by_slot = list(launcher._live_tokens)
    claimed: list[tuple[str, str]] = []

    def _fake_set_own_role(member_id, token, role):
        claimed.append((token, role))
        return True, ""

    monkeypatch.setattr(live_roles, "set_own_role", _fake_set_own_role)
    monkeypatch.setattr(ccb, "_cleanup_stale_runtime_dirs", lambda **_: 0)
    monkeypatch.setattr(ccb, "_cleanup_tmpclaude_artifacts", lambda: 0)
    monkeypatch.setattr(ccb, "_shrink_ccb_logs", lambda: 0)
    monkeypatch.setattr(ccb, "shutdown_daemon", lambda *_: None)
    monkeypatch.setattr(launcher, "_set_tmux_ui_active", lambda _on: None)
    monkeypatch.setattr(launcher, "_set_current_pane_label", lambda _p: None)
    monkeypatch.setattr(launcher, "_start_daemon_watchdog", lambda: None)
    monkeypatch.setattr(launcher, "_sync_cend_registry", lambda: None)
    monkeypatch.setattr(launcher, "_warmup_provider", lambda _p: None)
    monkeypatch.setattr(launcher, "_publish_live_inventory", lambda: True)
    monkeypatch.setattr(launcher, "_publish_role_policy", lambda: True)
    monkeypatch.setattr(launcher, "_preflight_agent_policies", lambda: None)
    monkeypatch.setattr(launcher, "_require_project_config_dir", lambda: True)
    monkeypatch.setattr(launcher, "_backfill_claude_session_work_dir_fields", lambda: None)
    monkeypatch.setattr(launcher, "_cmd_settings", lambda: {"enabled": False})
    monkeypatch.setattr(launcher, "_current_pane_id", lambda: "%0")
    launcher.cleanup = lambda **_kwargs: None  # keep the registered atexit hook inert

    spawned: list[str] = []
    monkeypatch.setattr(
        launcher,
        "_start_provider",
        lambda provider, **_kwargs: spawned.append(provider) or f"%{len(spawned)}",
    )
    monkeypatch.setattr(
        launcher,
        "_start_claude_pane",
        lambda **_kwargs: spawned.append("claude") or f"%{len(spawned)}",
    )

    def _start_anchor(provider):
        # The anchor claims its role before the provider process starts.
        launcher._claim_launch_role()
        return 0

    monkeypatch.setattr(launcher, "_start_provider_in_current_pane", _start_anchor)

    assert launcher.run_up() == 0
    # Panes are created in reverse provider order (codex, then claude).
    assert spawned == ["codex", "claude"]
    # Each claim used the ORIGINAL occurrence identity, not creation order.
    assert claimed == [
        (tokens_by_slot[1], "ratifier"),
        (tokens_by_slot[0], "lead"),
        (tokens_by_slot[2], "implementer"),
    ]


def test_no_assigned_role_claims_nothing_and_says_nothing(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        live_roles,
        "set_own_role",
        lambda *_args: pytest.fail("nothing to claim"),
    )
    _ccb, launcher = _launcher_for_claims(monkeypatch, tmp_path, ["claude", "pi"], None)
    for slot in range(len(launcher.providers)):
        launcher._launch_slot = slot
        launcher._claim_launch_role()
    assert capsys.readouterr().err == ""


def test_claim_failure_warns_once_and_names_the_occurrence(monkeypatch, tmp_path, capsys) -> None:
    reason = "role 'implementer' is already held by another session of this launch"
    monkeypatch.setattr(live_roles, "set_own_role", lambda *_args: (False, reason))
    _ccb, launcher = _launcher_for_claims(
        monkeypatch, tmp_path, ["claude", "codex", "codex"], ["lead", "ratifier", "implementer"]
    )
    launcher._launch_slot = 2
    launcher._claim_launch_role()
    launcher._claim_launch_role()
    err = capsys.readouterr().err
    assert "codex session 2 of 2" in err
    assert reason in err
    assert err.count("DEGRADED") == 1
    assert "ccb-role set implementer" in err


def test_claim_without_a_credential_is_reported_not_faked(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        live_roles, "set_own_role", lambda *_args: pytest.fail("no credential, no claim")
    )
    _ccb, launcher = _launcher_for_claims(monkeypatch, tmp_path, ["pi"], ["implementer"])
    launcher._launch_slot = 0
    monkeypatch.setattr(launcher, "_member_credential", lambda _p: ("", ""))
    launcher._claim_launch_role()
    err = capsys.readouterr().err
    assert "no launch credential" in err
    assert "DEGRADED" in err


# --- unchanged behaviour ----------------------------------------------------------


@pytest.mark.parametrize(
    "providers",
    [["codex"], ["claude", "codex"], ["claude", "codex", "codex"], ["gemini", "codex", "codex", "pi"]],
)
def test_roles_are_optional_and_absent_roles_add_no_output(monkeypatch, tmp_path, capsys, providers) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    launcher = ccb.AILauncher(providers=providers)
    assert launcher.roles == [""] * len(providers)
    assert provider_pairing_error(providers) == ""
    assert capsys.readouterr().err == ""


def test_launcher_rejects_a_role_list_for_the_wrong_provider_count(monkeypatch, tmp_path) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    with pytest.raises(ValueError):
        ccb.AILauncher(providers=["claude", "codex", "codex"], roles=["lead"])


# --- pair launch: the anchor publishes its own inventory entry --------------------


class _PaneBackend:
    """Marker -> pane mapping, with no in-place respawn (so no pid witness)."""

    def __init__(self, panes):
        self.panes = dict(panes)

    def is_alive(self, pane_id):
        return str(pane_id) in self.panes.values()

    def find_pane_by_title_marker(self, marker, _cwd=""):
        return self.panes.get(marker)


def _pair_anchor_launcher(monkeypatch, tmp_path, roles):
    """A `codex, codex, pi` launch with every pane bound, anchor = pi."""
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "tmux")
    launcher = ccb.AILauncher(providers=["codex", "codex", "pi"], roles=roles)
    launcher.runtime_dir = tmp_path / "runtime"
    launcher.runtime_dir.mkdir()
    monkeypatch.setattr(launcher, "_maybe_start_provider_daemon", lambda _p: None)
    assert launcher._live_slots and launcher._launch_slot == 2

    # run_up's first publish happens before any pane exists.
    assert launcher._publish_live_inventory()

    markers = {}
    for slot in (0, 1):
        launcher._launch_slot = slot
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / f"codex-{slot}"))
        runtime = launcher._provider_runtime("codex")
        runtime.mkdir(parents=True)
        marker = launcher._provider_marker("codex")
        assert launcher._write_codex_session(
            runtime, None, runtime / "in", runtime / "out",
            pane_id=f"%{slot}", pane_title_marker=marker,
        )
        markers[marker] = f"%{slot}"

    # The anchor writes its own binding inside its startup method.
    launcher._launch_slot = 2
    anchor_marker = launcher._provider_marker("pi")
    markers[anchor_marker] = "%2"
    assert launcher._write_pi_session(
        launcher.runtime_dir / "pi", pane_id="%2", pane_title_marker=anchor_marker, start_cmd="pi",
    )
    monkeypatch.setattr(
        pane_registry, "get_backend_for_session", lambda _rec: _PaneBackend(markers)
    )
    return launcher


def test_pair_launch_pi_anchor_claims_its_role_from_the_live_inventory(
    monkeypatch, tmp_path, capsys
) -> None:
    launcher = _pair_anchor_launcher(monkeypatch, tmp_path, ["", "", "implementer"])

    launcher._claim_launch_role()

    record = load_registry_by_session_id(launcher.session_id)
    anchor = next(s for s in read_inventory(record).sessions if s.provider == "pi")
    assert anchor.active and anchor.pane_id == "%2"
    assert live_roles.roles_for_record(record) == {anchor.live_id: "implementer"}
    assert capsys.readouterr().err == ""


def test_pair_anchor_inventory_refresh_also_unblocks_a_manual_claim(
    monkeypatch, tmp_path, capsys
) -> None:
    # No role configured for the anchor: the launcher has nothing to claim, but
    # its inventory refresh is what lets the session claim a role itself later.
    launcher = _pair_anchor_launcher(monkeypatch, tmp_path, None)

    launcher._claim_launch_role()

    member_id, token = launcher._member_credential("pi")
    ok, detail = live_roles.set_own_role(member_id, token, "implementer")
    assert ok, detail
    record = load_registry_by_session_id(launcher.session_id)
    assert live_roles.roles_for_record(record) == {member_id: "implementer"}
    assert capsys.readouterr().err == ""


def test_republishing_the_inventory_for_a_claim_is_idempotent(monkeypatch, tmp_path) -> None:
    launcher = _pair_anchor_launcher(monkeypatch, tmp_path, ["", "", "implementer"])
    path = pane_registry.registry_path_for_session(launcher.session_id)
    # Bring the inventory current, exactly as the claim's refresh will.
    assert launcher._publish_live_inventory()
    before = json.loads(path.read_text(encoding="utf-8"))
    before.pop("updated_at", None)

    launcher._claim_launch_role()  # publishes the inventory again, then claims

    after = json.loads(path.read_text(encoding="utf-8"))
    after.pop("updated_at", None)
    assert after == before


def test_failed_inventory_publish_without_an_assigned_role_is_silent(
    monkeypatch, tmp_path, capsys
) -> None:
    launcher = _pair_anchor_launcher(monkeypatch, tmp_path, None)
    monkeypatch.setattr(launcher, "_publish_live_inventory", lambda: False)
    monkeypatch.setattr(
        live_roles, "set_own_role", lambda *_args: pytest.fail("no role assigned, nothing to claim")
    )

    launcher._claim_launch_role()

    assert capsys.readouterr().err == ""


def test_anchor_claim_reports_a_failed_inventory_publish(monkeypatch, tmp_path, capsys) -> None:
    launcher = _pair_anchor_launcher(monkeypatch, tmp_path, ["", "", "implementer"])
    monkeypatch.setattr(launcher, "_publish_live_inventory", lambda: False)
    monkeypatch.setattr(
        live_roles, "set_own_role", lambda *_args: pytest.fail("must not claim on stale inventory")
    )

    launcher._claim_launch_role()

    err = capsys.readouterr().err
    assert "launch inventory could not be published" in err
    assert "pi session 1 of 1" in err
    assert err.count("DEGRADED") == 1


# --- WezTerm: the title marker must exist before a claim --------------------------

import sys
import time


class _TitleRecordingStdout:
    """Stands in for the launcher's own terminal stream."""

    def __init__(self, events):
        self.events = events
        self.text = ""

    def write(self, data):
        self.text += data
        if "\033]0;" in data:
            self.events.append("title")
        return len(data)

    def flush(self):
        pass


class _WeztermBackend:
    """A marker resolver that only succeeds once a title sequence was written."""

    def __init__(self, events, pane_id, *, resolve=True, needs_title=True):
        self.events = events
        self.pane_id = str(pane_id)
        self.resolve = resolve
        self.needs_title = needs_title

    def is_alive(self, pane_id):
        return str(pane_id) == self.pane_id

    def find_pane_by_title_marker(self, marker, _cwd=""):
        if not self.resolve:
            return None
        if self.needs_title and "title" not in self.events:
            return None
        if "resolve" not in self.events:
            self.events.append("resolve")
        return self.pane_id


class _SubprocessStub:
    """Only Popen/run are stubbed; everything else is the real module."""

    def __init__(self, popen, run):
        self.Popen = popen
        self.run = run

    def __getattr__(self, name):
        import subprocess as real

        return getattr(real, name)


def _wezterm_launcher(monkeypatch, tmp_path, provider, roles, *, resolve=True):
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "wezterm")
    monkeypatch.setenv("WEZTERM_PANE", "9")
    launcher = ccb.AILauncher(providers=[provider], roles=roles)
    launcher.terminal_type = "wezterm"
    launcher.runtime_dir = tmp_path / "runtime"
    launcher.runtime_dir.mkdir()
    monkeypatch.setattr(launcher, "_maybe_start_provider_daemon", lambda _p: None)
    monkeypatch.setattr(launcher, "_auto_prune_session_records", lambda: None)
    monkeypatch.setattr(launcher, "_maybe_start_caskd", lambda: None)
    monkeypatch.setattr(ccb, "ROLE_CLAIM_MARKER_TIMEOUT_S", 0.2)
    monkeypatch.setattr(ccb, "ROLE_CLAIM_MARKER_POLL_S", 0.01)

    events: list[str] = []
    stdout = _TitleRecordingStdout(events)
    monkeypatch.setattr(sys, "stdout", stdout)
    backend = _WeztermBackend(events, "9", resolve=resolve)
    monkeypatch.setattr(pane_registry, "get_backend_for_session", lambda _rec: backend)
    monkeypatch.setattr(
        live_roles,
        "set_own_role",
        lambda *_args: (events.append("claim"), (True, ""))[1],
    )

    class _Popen:
        def __init__(self, *args, **_kwargs):
            events.append("start")

        def wait(self):
            return 0

    class _RunResult:
        returncode = 0

    def _run(*_args, **_kwargs):
        events.append("start")
        return _RunResult()

    monkeypatch.setattr(ccb, "subprocess", _SubprocessStub(_Popen, _run))
    monkeypatch.setattr(
        launcher, "_run_shell_command", lambda *_args, **_kwargs: (events.append("start"), 0)[1]
    )
    monkeypatch.setattr(launcher, "_claude_start_plan", lambda: (["claude"], tmp_path, False))
    return ccb, launcher, events, stdout


ANCHOR_START = {
    "claude": lambda launcher: launcher._start_claude(),
    "codex": lambda launcher: launcher._start_codex_current_pane(),
    "gemini": lambda launcher: launcher._start_gemini_current_pane(),
    "opencode": lambda launcher: launcher._start_opencode_current_pane(),
    "hermes": lambda launcher: launcher._start_hermes_current_pane(),
    "pi": lambda launcher: launcher._start_pi_current_pane(),
}


def _anchored_session_file(launcher, provider):
    if provider == "claude":
        return launcher._claude_session_file()
    return launcher._project_session_file(f".{provider.replace('opencode', 'opencode')}-session")


@pytest.mark.parametrize("provider", sorted(ANCHOR_START))
def test_wezterm_anchor_emits_its_marker_then_resolves_then_claims_then_starts(
    monkeypatch, tmp_path, capsys, provider
) -> None:
    ccb, launcher, events, stdout = _wezterm_launcher(
        monkeypatch, tmp_path, provider, ["implementer"]
    )
    if provider == "claude":
        # run_up writes the anchor's claude binding before the provider starts.
        launcher._write_local_claude_session(
            pane_id="9", pane_title_marker=launcher._claude_marker(), terminal="wezterm"
        )

    assert ANCHOR_START[provider](launcher) == 0

    marker = json.loads(_anchored_session_file(launcher, provider).read_text(encoding="utf-8"))[
        "pane_title_marker"
    ]
    # The launcher wrote exactly the OSC 0 sequence it would otherwise ask the
    # pane's command to run, and only then did the claim go through.
    assert stdout.text.count(f"\033]0;{marker}\007") == 1
    assert events == ["title", "resolve", "claim", "start"]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("provider", sorted(ANCHOR_START))
def test_wezterm_anchor_that_never_resolves_degrades_within_the_bound(
    monkeypatch, tmp_path, capsys, provider
) -> None:
    ccb, launcher, events, stdout = _wezterm_launcher(
        monkeypatch, tmp_path, provider, ["implementer"], resolve=False
    )
    if provider == "claude":
        launcher._write_local_claude_session(
            pane_id="9", pane_title_marker=launcher._claude_marker(), terminal="wezterm"
        )

    started = time.monotonic()
    assert ANCHOR_START[provider](launcher) == 0
    elapsed = time.monotonic() - started

    assert elapsed < 1.0  # bounded by ROLE_CLAIM_MARKER_TIMEOUT_S
    assert "claim" not in events  # nothing claimed on an unresolved marker
    assert events[-1] == "start"  # and the launch still continued
    err = capsys.readouterr().err
    assert "pane title marker did not resolve to this pane in time" in err
    assert err.count("DEGRADED") == 1


def test_wezterm_pi_anchor_claims_through_the_real_role_path(
    monkeypatch, tmp_path, capsys
) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "wezterm")
    monkeypatch.setenv("WEZTERM_PANE", "9")
    launcher = ccb.AILauncher(providers=["pi"], roles=["implementer"])
    launcher.terminal_type = "wezterm"
    launcher.runtime_dir = tmp_path / "runtime"
    launcher.runtime_dir.mkdir()
    monkeypatch.setattr(launcher, "_maybe_start_provider_daemon", lambda _p: None)
    monkeypatch.setattr(launcher, "_auto_prune_session_records", lambda: None)
    monkeypatch.setattr(ccb, "ROLE_CLAIM_MARKER_TIMEOUT_S", 0.2)
    monkeypatch.setattr(ccb, "ROLE_CLAIM_MARKER_POLL_S", 0.01)
    events: list[str] = []
    stdout = _TitleRecordingStdout(events)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _WeztermBackend(events, "9"),
    )
    monkeypatch.setattr(launcher, "_run_shell_command", lambda *_a, **_k: 0)

    assert launcher._start_pi_current_pane() == 0

    record = load_registry_by_session_id(launcher.session_id)
    member = record["providers"]["pi"]
    assert live_roles.roles_for_record(record) == {member["member_id"]: "implementer"}
    assert capsys.readouterr().err == ""


def test_wezterm_spawned_pane_waits_for_its_marker_without_emitting_one(
    monkeypatch, tmp_path, capsys
) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "wezterm")
    launcher = ccb.AILauncher(providers=["gemini"], roles=["implementer"])
    launcher.terminal_type = "wezterm"
    launcher.runtime_dir = tmp_path / "runtime"
    launcher.runtime_dir.mkdir()
    monkeypatch.setattr(launcher, "_maybe_start_provider_daemon", lambda _p: None)
    monkeypatch.setattr(launcher, "_auto_prune_session_records", lambda: None)
    monkeypatch.setattr(ccb, "ROLE_CLAIM_MARKER_TIMEOUT_S", 0.2)
    monkeypatch.setattr(ccb, "ROLE_CLAIM_MARKER_POLL_S", 0.01)
    events: list[str] = []
    stdout = _TitleRecordingStdout(events)
    monkeypatch.setattr(sys, "stdout", stdout)
    # The spawned pane's own command already emitted the title.
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _WeztermBackend(events, "7", needs_title=False),
    )
    monkeypatch.setattr(
        live_roles, "set_own_role", lambda *_args: (events.append("claim"), (True, ""))[1]
    )
    launcher.wezterm_panes["gemini"] = "7"
    launcher._launch_slot = 0
    assert launcher._write_gemini_session(
        launcher.runtime_dir / "gemini",
        None,
        pane_id="7",
        pane_title_marker=launcher._provider_marker("gemini"),
        start_cmd="gemini",
    )

    launcher._claim_launch_role()

    assert events == ["resolve", "claim"]
    assert "\033]0;" not in stdout.text
    assert capsys.readouterr().err == ""


def test_wezterm_spawned_pane_that_never_resolves_degrades(
    monkeypatch, tmp_path, capsys
) -> None:
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    launcher = ccb.AILauncher(providers=["gemini"], roles=["implementer"])
    launcher.terminal_type = "wezterm"
    launcher.runtime_dir = tmp_path / "runtime"
    launcher.runtime_dir.mkdir()
    monkeypatch.setattr(launcher, "_maybe_start_provider_daemon", lambda _p: None)
    monkeypatch.setattr(launcher, "_auto_prune_session_records", lambda: None)
    monkeypatch.setattr(ccb, "ROLE_CLAIM_MARKER_TIMEOUT_S", 0.15)
    monkeypatch.setattr(ccb, "ROLE_CLAIM_MARKER_POLL_S", 0.01)
    events: list[str] = []
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _WeztermBackend(events, "7", resolve=False, needs_title=False),
    )
    monkeypatch.setattr(
        live_roles,
        "set_own_role",
        lambda *_args: pytest.fail("must not claim an unresolved marker"),
    )
    launcher.wezterm_panes["gemini"] = "7"
    launcher._launch_slot = 0
    assert launcher._write_gemini_session(
        launcher.runtime_dir / "gemini",
        None,
        pane_id="7",
        pane_title_marker=launcher._provider_marker("gemini"),
        start_cmd="gemini",
    )

    started = time.monotonic()
    launcher._claim_launch_role()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    err = capsys.readouterr().err
    assert "pane title marker did not resolve to this pane in time" in err
    assert "DEGRADED" in err


# --- the WezTerm marker wait owns its deadline ------------------------------------

import inspect as _inspect

from terminal import TmuxBackend, WeztermBackend


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += max(0.0, seconds)


class _TimeProxy:
    """`ccb.time` with a controlled clock; everything else is the real module."""

    def __init__(self, clock):
        self._clock = clock

    def monotonic(self):
        return self._clock.now

    def sleep(self, seconds):
        self._clock.sleep(seconds)

    def __getattr__(self, name):
        import time as real

        return getattr(real, name)


def _slow_listing(clock, budgets, *, marker, pane_id, per_call, title_at, overrun=0.0):
    """A `wezterm cli list` stand-in that takes real simulated time."""

    def _list_panes(timeout=1.0):
        budgets.append(timeout)
        spent = min(per_call, max(0.0, timeout)) if not overrun else max(0.0, timeout) + overrun
        clock.now += spent
        if clock.now < title_at:
            return [{"pane_id": pane_id, "title": "something else"}]
        return [{"pane_id": pane_id, "title": marker}]

    return _list_panes


def _clocked_launcher(monkeypatch, tmp_path, clock, backend):
    ccb = _load_ccb_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ccb").mkdir()
    monkeypatch.setattr(ccb, "detect_terminal", lambda: "wezterm")
    monkeypatch.setenv("WEZTERM_PANE", "9")
    launcher = ccb.AILauncher(providers=["pi"], roles=["implementer"])
    launcher.terminal_type = "wezterm"
    launcher.runtime_dir = tmp_path / "runtime"
    launcher.runtime_dir.mkdir()
    monkeypatch.setattr(launcher, "_maybe_start_provider_daemon", lambda _p: None)
    monkeypatch.setattr(ccb, "ROLE_CLAIM_MARKER_TIMEOUT_S", 2.0)
    monkeypatch.setattr(ccb, "ROLE_CLAIM_MARKER_POLL_S", 0.05)
    monkeypatch.setattr(ccb, "time", _TimeProxy(clock))
    monkeypatch.setattr(pane_registry, "get_backend_for_session", lambda _rec: backend)
    return ccb, launcher


def test_wezterm_marker_wait_hands_the_remaining_budget_to_the_listing(
    monkeypatch, tmp_path, capsys
) -> None:
    clock = _FakeClock()
    budgets: list[float] = []
    marker = "CCB-Pi-deadbeef"
    backend = WeztermBackend()
    monkeypatch.setattr(
        backend,
        "_list_panes",
        _slow_listing(clock, budgets, marker=marker, pane_id="9", per_call=1.8, title_at=99),
    )
    ccb, launcher = _clocked_launcher(monkeypatch, tmp_path, clock, backend)
    monkeypatch.setattr(
        live_roles, "set_own_role", lambda *_args: pytest.fail("nothing resolved, nothing to claim")
    )

    launcher._claim_launch_role(marker, is_anchor=True)

    # Polling stopped at the deadline: at most the bound plus one poll.
    assert clock.now <= 2.0 + 0.05 + 1e-9
    assert budgets == sorted(budgets, reverse=True)  # each probe got what was left
    assert budgets[0] == pytest.approx(2.0)
    assert all(budget > 0 for budget in budgets)
    err = capsys.readouterr().err
    assert "pane title marker did not resolve to this pane in time" in err
    assert "DEGRADED" in err


def test_wezterm_marker_wait_rejects_a_resolution_after_the_deadline(
    monkeypatch, tmp_path, capsys
) -> None:
    clock = _FakeClock()
    budgets: list[float] = []
    marker = "CCB-Pi-deadbeef"
    backend = WeztermBackend()
    # The listing overruns the budget it was given and only then shows the
    # marker, so the pane never held it while it was being awaited.
    monkeypatch.setattr(
        backend,
        "_list_panes",
        _slow_listing(
            clock, budgets, marker=marker, pane_id="9", per_call=1.8, title_at=2.1, overrun=0.6
        ),
    )
    ccb, launcher = _clocked_launcher(monkeypatch, tmp_path, clock, backend)
    monkeypatch.setattr(
        live_roles, "set_own_role", lambda *_args: pytest.fail("a late resolution is not a claim")
    )

    launcher._claim_launch_role(marker, is_anchor=True)

    assert clock.now > 2.0  # it really did resolve late
    err = capsys.readouterr().err
    assert "pane title marker did not resolve to this pane in time" in err
    assert "DEGRADED" in err


def test_wezterm_listing_defaults_are_unchanged(monkeypatch) -> None:
    backend = WeztermBackend()
    seen: list[float] = []

    def _list_panes(timeout=1.0):
        seen.append(timeout)
        return [{"pane_id": "9", "title": "CCB-Pi-deadbeef"}]

    monkeypatch.setattr(backend, "_list_panes", _list_panes)

    # Existing callers pass no budget and keep the listing's own default.
    assert backend.find_pane_by_title_marker("CCB-Pi-deadbeef") == "9"
    assert seen == [1.0]

    seen.clear()
    assert backend.find_pane_by_title_marker("CCB-Pi-deadbeef", ".", timeout=0.3) == "9"
    assert seen == [0.3]

    signature = _inspect.signature(WeztermBackend.find_pane_by_title_marker)
    assert signature.parameters["timeout"].default is None
    assert signature.parameters["timeout"].kind is _inspect.Parameter.KEYWORD_ONLY
    # tmux keeps its signature: the budget seam exists only where the CLI does.
    assert "timeout" not in _inspect.signature(TmuxBackend.find_pane_by_title_marker).parameters
