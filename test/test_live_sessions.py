"""Tests for the live-session inventory and destination resolver.

The invariants under test are the three the feature is gated on: an ask reaches
the intended sibling and never self-routes, the destination is unique or the
request is refused, and identity stays exact enough to name who was addressed.
"""
from __future__ import annotations

from live_sessions import (
    AMBIGUOUS,
    NOT_MOUNTED,
    SELF_ONLY,
    UNAVAILABLE,
    UNKNOWN_CALLER,
    LiveSession,
    find_caller,
    live_sessions_from_record,
    resolve_local_target,
)

# Additive import for the read_inventory contract-gap coverage below — kept
# separate from the block above so nothing in that original import touches.
from live_sessions import (
    INVENTORY_ABSENT,
    INVENTORY_INVALID,
    INVENTORY_VALID,
    read_inventory,
)


def _session(live_id: str, provider: str, pane: str, *, terminal: str = "wezterm", active: bool = True) -> LiveSession:
    return LiveSession(
        live_id=live_id,
        provider=provider,
        launch_id="ai-1",
        pane_id=pane,
        terminal=terminal,
        active=active,
    )


# --------------------------------------------------------------------------
# Inventory reading
# --------------------------------------------------------------------------

def test_legacy_record_projects_one_session_per_provider() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "work_dir": "/w",
        "ccb_project_id": "abc",
        "terminal": "tmux",
        "providers": {"codex": {"pane_id": "%3"}, "claude": {"pane_id": "%2"}},
    }
    sessions = live_sessions_from_record(record)

    assert sorted(s.provider for s in sessions) == ["claude", "codex"]
    codex = next(s for s in sessions if s.provider == "codex")
    assert codex.pane_id == "%3"
    assert codex.terminal == "tmux"
    assert codex.work_dir == "/w"
    assert codex.ccb_project_id == "abc"
    # Derived, so the same record always yields the same identity.
    assert codex.live_id == live_sessions_from_record(record)[1].live_id


def test_live_sessions_key_supports_two_of_one_provider() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "work_dir": "/w",
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7", "terminal": "wezterm"},
            {"live_id": "s2", "provider": "codex", "pane_id": "8", "terminal": "wezterm"},
        ],
    }
    sessions = live_sessions_from_record(record)

    assert [s.live_id for s in sessions] == ["s1", "s2"]
    assert all(s.provider == "codex" for s in sessions)
    assert all(s.work_dir == "/w" for s in sessions)


def test_malformed_entry_fails_whole_inventory() -> None:
    # A defective member must not be silently dropped: doing so can turn an
    # ambiguous pair into a falsely unique destination.
    base = {"ccb_session_id": "ai-1"}
    missing_id = {**base, "live_sessions": [
        {"live_id": "s1", "provider": "codex", "pane_id": "7"},
        {"provider": "codex", "pane_id": "8"},  # no id
    ]}
    missing_provider = {**base, "live_sessions": [
        {"live_id": "s1", "provider": "codex", "pane_id": "7"},
        {"live_id": "s3", "pane_id": "9"},  # no provider
    ]}
    not_a_dict = {**base, "live_sessions": [
        {"live_id": "s1", "provider": "codex", "pane_id": "7"},
        "not-a-dict",
    ]}
    assert live_sessions_from_record(missing_id) == []
    assert live_sessions_from_record(missing_provider) == []
    assert live_sessions_from_record(not_a_dict) == []


def test_duplicate_live_id_fails_whole_inventory() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7"},
            {"live_id": "s1", "provider": "codex", "pane_id": "10"},
        ],
    }
    assert live_sessions_from_record(record) == []


def test_live_sessions_present_but_not_a_list_has_no_legacy_fallback() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "live_sessions": "nope",
        "providers": {"codex": {"pane_id": "%3"}},
    }
    assert live_sessions_from_record(record) == []


def test_entry_launch_id_contradicting_record_fails_whole_inventory() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7", "launch_id": "ai-2"},
        ],
    }
    assert live_sessions_from_record(record) == []


def test_entry_ccb_project_id_contradicting_record_fails_whole_inventory() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "ccb_project_id": "abc",
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7", "ccb_project_id": "xyz"},
        ],
    }
    assert live_sessions_from_record(record) == []


def test_entry_omitting_scope_inherits_the_record() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "ccb_project_id": "abc",
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7"},
        ],
    }
    session = live_sessions_from_record(record)[0]
    assert session.launch_id == "ai-1"
    assert session.ccb_project_id == "abc"


def test_new_style_entry_inherits_record_terminal() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "terminal": "tmux",
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7"},
        ],
    }
    assert live_sessions_from_record(record)[0].terminal == "tmux"


def test_new_style_inventory_without_record_launch_id_is_empty() -> None:
    # A new-style inventory needs a containing launch id to scope its entries
    # to, exactly as the legacy projection now requires below.
    record = {
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7"},
        ],
    }
    assert live_sessions_from_record(record) == []


def test_entry_work_dir_contradicting_record_fails_whole_inventory() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "work_dir": "/w/proj",
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7", "work_dir": "/w/other"},
        ],
    }
    assert live_sessions_from_record(record) == []


def test_entry_work_dir_agreeing_after_normalization_is_accepted() -> None:
    # A trailing slash is not a real disagreement once both sides are
    # normalized — only the raw compare would falsely reject it.
    record = {
        "ccb_session_id": "ai-1",
        "work_dir": "/w/proj",
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7", "work_dir": "/w/proj/"},
        ],
    }
    sessions = live_sessions_from_record(record)
    assert len(sessions) == 1
    assert sessions[0].work_dir == "/w/proj/"


def test_entry_omitting_work_dir_inherits_the_record() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "work_dir": "/w/proj",
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7"},
        ],
    }
    assert live_sessions_from_record(record)[0].work_dir == "/w/proj"


def test_active_string_false_is_parsed_as_inactive() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7", "active": "false"},
        ],
    }
    assert live_sessions_from_record(record)[0].active is False


def test_active_none_is_inactive() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7", "active": None},
        ],
    }
    assert live_sessions_from_record(record)[0].active is False


def test_active_unrecognized_string_is_inactive() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7", "active": "maybe"},
        ],
    }
    assert live_sessions_from_record(record)[0].active is False


def test_active_non_string_non_bool_value_is_inactive() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7", "active": 1},
        ],
    }
    assert live_sessions_from_record(record)[0].active is False


def test_active_supported_spellings_parse_as_intended() -> None:
    def active_of(value: object) -> bool:
        record = {
            "ccb_session_id": "ai-1",
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "7", "active": value},
            ],
        }
        return live_sessions_from_record(record)[0].active

    for spelling in ("true", "True", "TRUE", " true ", "1", "yes", "Yes", "YES"):
        assert active_of(spelling) is True, spelling
    for spelling in ("false", "False", "FALSE", " false ", "0", "no", "No", "NO"):
        assert active_of(spelling) is False, spelling


def test_active_absent_key_defaults_active() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "7"},
        ],
    }
    assert live_sessions_from_record(record)[0].active is True


def test_legacy_projection_with_empty_launch_id_is_empty() -> None:
    record = {
        "work_dir": "/w",
        "providers": {"codex": {"pane_id": "%3"}},
    }
    assert live_sessions_from_record(record) == []


def test_new_style_entry_with_legacy_prefixed_id_is_rejected() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "live_sessions": [
            {"live_id": "legacy:ai-1:codex", "provider": "codex", "pane_id": "7"},
        ],
    }
    assert live_sessions_from_record(record) == []


def test_inventory_of_unusable_record_is_empty() -> None:
    assert live_sessions_from_record({}) == []
    assert live_sessions_from_record({"providers": "nope"}) == []


# --------------------------------------------------------------------------
# Caller identification
# --------------------------------------------------------------------------

def test_caller_identified_by_pane() -> None:
    sessions = [_session("s1", "codex", "7"), _session("s2", "codex", "8")]
    assert find_caller(sessions, pane_id="8").live_id == "s2"


def test_caller_pane_must_match_terminal_backend() -> None:
    sessions = [_session("s1", "codex", "7", terminal="wezterm")]
    assert find_caller(sessions, pane_id="7", terminal="tmux") is None
    assert find_caller(sessions, pane_id="7", terminal="wezterm").live_id == "s1"


def test_caller_unknown_when_pane_claimed_twice() -> None:
    sessions = [_session("s1", "codex", "7"), _session("s2", "codex", "7")]
    assert find_caller(sessions, pane_id="7") is None


def test_live_id_contradicting_pane_is_rejected() -> None:
    # The design forbids self-routing absolutely: an id and a pane that
    # disagree must never be resolved in the id's favor.
    sessions = [_session("s1", "codex", "7"), _session("s2", "codex", "8")]
    assert find_caller(sessions, live_id="s1", pane_id="8") is None
    # Matching pane is still accepted.
    assert find_caller(sessions, live_id="s1", pane_id="7").live_id == "s1"


def test_live_id_contradicting_terminal_backend_is_rejected() -> None:
    sessions = [_session("s1", "codex", "7", terminal="wezterm")]
    assert find_caller(sessions, live_id="s1", pane_id="7", terminal="tmux") is None
    assert find_caller(sessions, live_id="s1", pane_id="7", terminal="wezterm").live_id == "s1"


def test_live_id_contradicting_terminal_backend_without_pane_is_rejected() -> None:
    # The terminal check must not live only inside the pane_id branch — a
    # supplied terminal that disagrees is a contradiction on its own.
    sessions = [_session("s1", "codex", "7", terminal="wezterm")]
    assert find_caller(sessions, live_id="s1", terminal="tmux") is None
    assert find_caller(sessions, live_id="s1", terminal="wezterm").live_id == "s1"
    assert find_caller(sessions, live_id="s1").live_id == "s1"


def test_live_id_matching_two_sessions_is_rejected() -> None:
    sessions = [_session("s1", "codex", "7"), _session("s1", "codex", "8")]
    assert find_caller(sessions, live_id="s1") is None


def test_live_id_still_prevents_pane_fallback_when_it_matches_nothing() -> None:
    sessions = [_session("s1", "codex", "7"), _session("s2", "codex", "8")]
    assert find_caller(sessions, live_id="gone", pane_id="8") is None


# --------------------------------------------------------------------------
# Destination resolution
# --------------------------------------------------------------------------

def test_unique_provider_resolves_without_a_caller() -> None:
    sessions = [_session("s1", "codex", "7"), _session("s2", "claude", "8")]
    res = resolve_local_target(sessions, provider="codex")

    assert res.ok
    assert res.session.live_id == "s1"


def test_pair_member_selects_the_other_in_both_directions() -> None:
    a, b = _session("s1", "codex", "7"), _session("s2", "codex", "8")

    assert resolve_local_target([a, b], provider="codex", caller=a).session.live_id == "s2"
    assert resolve_local_target([a, b], provider="codex", caller=b).session.live_id == "s1"


def test_pair_without_verified_caller_refuses() -> None:
    a, b = _session("s1", "codex", "7"), _session("s2", "codex", "8")
    res = resolve_local_target([a, b], provider="codex", caller=None)

    assert not res.ok
    assert res.error == UNKNOWN_CALLER
    assert sorted(res.candidates) == ["s1", "s2"]


def test_caller_outside_the_pair_cannot_choose_between_them() -> None:
    a, b = _session("s1", "codex", "7"), _session("s2", "codex", "8")
    outsider = _session("s3", "claude", "9")
    res = resolve_local_target([a, b, outsider], provider="codex", caller=outsider)

    assert not res.ok
    assert res.error == AMBIGUOUS


def test_dead_sibling_reports_unavailable_and_never_self_routes() -> None:
    a = _session("s1", "codex", "7")
    dead = _session("s2", "codex", "8", active=False)
    res = resolve_local_target([a, dead], provider="codex", caller=a)

    assert not res.ok
    assert res.error == UNAVAILABLE
    assert res.session is None


def test_unidentified_caller_refuses_even_when_only_one_member_is_active() -> None:
    # Identity must be decided from the full topology (inactive members
    # included) before availability narrows the pool — otherwise an
    # unidentified caller sitting in the sole active pane resolves to itself.
    a = _session("s1", "codex", "7")
    dead = _session("s2", "codex", "8", active=False)
    res = resolve_local_target([a, dead], provider="codex", caller=None)

    assert not res.ok
    assert res.error == UNKNOWN_CALLER
    assert res.session is None


def test_outsider_caller_refuses_even_when_only_one_member_is_active() -> None:
    a = _session("s1", "codex", "7")
    dead = _session("s2", "codex", "8", active=False)
    outsider = _session("s3", "claude", "9")
    res = resolve_local_target([a, dead, outsider], provider="codex", caller=outsider)

    assert not res.ok
    assert res.error == AMBIGUOUS
    assert res.session is None


def test_sole_session_asking_for_itself_is_refused() -> None:
    a = _session("s1", "codex", "7")
    res = resolve_local_target([a], provider="codex", caller=a)

    assert not res.ok
    assert res.error == SELF_ONLY
    assert res.session is None


def test_absent_provider_is_not_mounted() -> None:
    res = resolve_local_target([_session("s1", "codex", "7")], provider="gemini")

    assert not res.ok
    assert res.error == NOT_MOUNTED


def test_resolution_never_returns_a_session_alongside_an_error() -> None:
    a, b = _session("s1", "codex", "7"), _session("s2", "codex", "8")
    for res in (
        resolve_local_target([a, b], provider="codex"),
        resolve_local_target([a], provider="codex", caller=a),
        resolve_local_target([], provider="codex"),
    ):
        assert not res.ok
        assert res.session is None


# --------------------------------------------------------------------------
# read_inventory: explicit absent / valid / invalid signal
# --------------------------------------------------------------------------

def test_read_inventory_reports_absent_valid_invalid_distinctly() -> None:
    absent = read_inventory(
        {"ccb_session_id": "ai-1", "providers": {"codex": {"pane_id": "%1"}}}
    )
    valid = read_inventory(
        {
            "ccb_session_id": "ai-1",
            "live_sessions": [{"live_id": "s1", "provider": "codex", "pane_id": "7"}],
        }
    )
    invalid = read_inventory({"ccb_session_id": "ai-1", "live_sessions": "nope"})

    assert absent.status == INVENTORY_ABSENT
    assert absent.present is False
    assert absent.valid is False
    assert [s.provider for s in absent.sessions] == ["codex"]

    assert valid.status == INVENTORY_VALID
    assert valid.present is True
    assert valid.valid is True
    assert [s.live_id for s in valid.sessions] == ["s1"]

    assert invalid.status == INVENTORY_INVALID
    assert invalid.present is True
    assert invalid.valid is False
    assert invalid.sessions == ()


def test_live_sessions_from_record_is_a_thin_wrapper_over_read_inventory() -> None:
    record = {
        "ccb_session_id": "ai-1",
        "live_sessions": [{"live_id": "s1", "provider": "codex", "pane_id": "7"}],
    }
    assert live_sessions_from_record(record) == list(read_inventory(record).sessions)
