"""A launch that starts exactly one agent session applies no ccb.config role.

Roles exist for agents working together. A solo launch therefore maps to no
assignment, still refuses a shape-broken config exactly as before, and says once
which role it dropped. Multi-agent launches keep every role, error and note they
had (commit 97a07dd behaviour).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from ccb_start_config import (
    normalize_start_config_data,
    resolve_roles_for_providers,
    roles_skipped_for_launch,
    solo_role_note,
)
from test_ccb_agent_composition import _load_ccb_module
from test_launch_roles_config import _cmd_start, _load_config

# A typical three-role plain-text config.
TRIO_TEXT = "codex=ratifier, pi=implementer, claude=lead\n"
# The same three roles as a JSON config with per-provider launch arguments.
TRIO_JSON = {
    "providers": ["codex", "pi", "claude"],
    "launch_args": {"pi": "--example-flag"},
    "roles": {"codex": "ratifier", "pi": "implementer", "claude": "lead"},
}
CODEX_PAIR_JSON = {
    "providers": ["codex", "codex"],
    "roles": {"codex": ["ratifier", "implementer"]},
}


# --- the rule --------------------------------------------------------------------


def test_trio_config_launched_as_pi_alone_assigns_no_role(tmp_path: Path, capsys) -> None:
    data = _load_config(tmp_path, TRIO_TEXT)
    assignments, error = resolve_roles_for_providers(data, ["pi"])
    assert error == ""
    assert assignments == [""]

    note = solo_role_note(data, ["pi"])
    assert "Starting pi alone" in note
    assert "implementer" in note
    assert "ccb-role set" in note
    # The dropped-role note replaces the skipped-roles note; never both.
    assert roles_skipped_for_launch(data, ["pi"]) == [
        ("codex", ["ratifier"]),
        ("claude", ["lead"]),
    ]


def test_trio_json_config_launched_in_full_keeps_every_role(tmp_path: Path) -> None:
    data = _load_config(tmp_path, json.dumps(TRIO_JSON) + "\n")
    assignments, error = resolve_roles_for_providers(data, data["providers"])
    assert error == ""
    assert assignments == ["ratifier", "implementer", "lead"]
    assert solo_role_note(data, data["providers"]) == ""


def test_a_config_listing_only_pi_still_roles_nobody_for_a_solo_launch(tmp_path: Path) -> None:
    data = _load_config(tmp_path, json.dumps({"providers": ["pi"], "roles": {"pi": "implementer"}}))
    assert data["providers"] == ["pi"]
    assignments, error = resolve_roles_for_providers(data, data["providers"])
    assert error == ""
    assert assignments == [""]
    assert "implementer" in solo_role_note(data, data["providers"])


def test_a_codex_pair_launched_as_a_pair_keeps_per_occurrence_roles() -> None:
    data = normalize_start_config_data(json.loads(json.dumps(CODEX_PAIR_JSON)))
    assignments, error = resolve_roles_for_providers(data, ["codex", "codex"])
    assert error == ""
    assert assignments == ["ratifier", "implementer"]
    assert solo_role_note(data, ["codex", "codex"]) == ""


def test_the_same_pair_launched_as_one_codex_gets_no_role_and_no_count_error() -> None:
    data = normalize_start_config_data(json.loads(json.dumps(CODEX_PAIR_JSON)))
    assignments, error = resolve_roles_for_providers(data, ["codex"])
    assert error == ""
    assert assignments == [""]
    # Nothing to name twice, so the note names the one entry the config had...
    note = solo_role_note(data, ["codex"])
    assert "Starting codex alone" in note


@pytest.mark.parametrize(
    ("providers", "config_text", "needle"),
    [
        # A role for a provider the config does not list is a shape error.
        (
            ["pi"],
            json.dumps({"providers": ["pi"], "roles": {"pi": "implementer", "claude": "lead"}}),
            "which ccb.config's providers do not include",
        ),
        # A malformed annotation is a shape error too.
        (["pi"], "pi=\n", "has no role after '='"),
        # Roles given twice is a shape error.
        (
            ["pi"],
            json.dumps({"providers": ["pi=implementer"], "roles": {"pi": "implementer"}}),
            "roles given both in providers and in roles; use one",
        ),
    ],
)
def test_a_solo_launch_still_refuses_a_shape_broken_config(
    tmp_path: Path, providers, config_text, needle
) -> None:
    data = _load_config(tmp_path, config_text)
    assignments, error = resolve_roles_for_providers(data, providers)
    assert assignments == []
    assert needle in error


def test_a_cli_subset_of_a_trio_config_still_skips_the_roles_it_omits(
    tmp_path: Path,
) -> None:
    data = _load_config(tmp_path, TRIO_TEXT)
    assignments, error = resolve_roles_for_providers(data, ["pi", "claude"])
    assert error == ""
    assert assignments == ["implementer", "lead"]
    assert solo_role_note(data, ["pi", "claude"]) == ""
    assert roles_skipped_for_launch(data, ["pi", "claude"]) == [("codex", ["ratifier"])]


# --- cmd_start: one note, and it never doubles ------------------------------------


def test_cmd_start_prints_only_the_solo_note_for_a_lone_pi(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    ccb = _load_ccb_module()
    result, constructed = _cmd_start(monkeypatch, tmp_path, TRIO_TEXT, ["pi"], ccb)
    assert result == 0
    assert constructed and constructed[0]["roles"] == [""]
    err = capsys.readouterr().err
    assert "Starting pi alone" in err
    assert "implementer" in err
    assert "Not starting" not in err
    assert err.count("ℹ️") == 1


def test_cmd_start_treats_one_agent_plus_the_cmd_pane_as_solo(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    ccb = _load_ccb_module()
    result, constructed = _cmd_start(monkeypatch, tmp_path, "pi=implementer, cmd\n", ["pi"], ccb)
    assert result == 0
    assert constructed and constructed[0]["roles"] == [""]
    # The cmd pane is not an agent, so it does not make the launch multi-agent.
    assert constructed[0]["cmd_config"]
    err = capsys.readouterr().err
    assert "Starting pi alone" in err
    assert "Not starting" not in err


def test_cmd_start_refuses_a_malformed_solo_config_before_any_launcher(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    ccb = _load_ccb_module()
    config_text = json.dumps(
        {"providers": ["pi"], "roles": {"pi": "implementer", "claude": "lead"}}
    )
    result, constructed = _cmd_start(monkeypatch, tmp_path, config_text, ["pi"], ccb)
    assert result == 2
    # RA1: the solo shortcut must never run before validation, so nothing started.
    assert constructed == []
    err = capsys.readouterr().err
    assert "which ccb.config's providers do not include" in err
    assert "Starting pi alone" not in err


def test_cmd_start_keeps_role_policy_enforce_while_dropping_the_solo_role(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    ccb = _load_ccb_module()
    config_text = json.dumps(
        {
            "providers": ["codex", "pi", "claude"],
            "role_policy": "enforce",
            "roles": {"codex": "ratifier", "pi": "implementer", "claude": "lead"},
        }
    )
    result, constructed = _cmd_start(monkeypatch, tmp_path, config_text, ["pi"], ccb)
    assert result == 0
    assert constructed and constructed[0]["roles"] == [""]
    assert constructed[0]["role_policy"] == "enforce"
    assert "Starting pi alone" in capsys.readouterr().err
