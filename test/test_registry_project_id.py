from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import pytest

import pane_registry
from pane_registry import (
    live_sessions_for_record,
    load_registry_by_pane,
    load_registry_by_project_id,
    upsert_registry,
)
from project_id import compute_ccb_project_id


class _FakeBackend:
    def __init__(
        self,
        alive: set[str],
        marker_map: Optional[dict[str, str]] = None,
        cwd_map: Optional[dict[str, str]] = None,
    ):
        self._alive = set(alive)
        self._marker_map = dict(marker_map or {})
        self._cwd_map = dict(cwd_map or {})

    def is_alive(self, pane_id: str) -> bool:
        return pane_id in self._alive

    def find_pane_by_title_marker(self, marker: str, cwd_hint: str = "") -> str | None:
        return self._marker_map.get(marker)

    def pane_belongs_to_cwd(self, pane_id: str, work_dir: str) -> bool:
        return self._cwd_map.get(pane_id) == work_dir


def _write_registry_file(home: Path, session_id: str, payload: dict) -> Path:
    path = home / ".ccb" / "run" / f"ccb-session-{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_upsert_registry_merges_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(pane_registry, "get_backend_for_session", lambda _rec: _FakeBackend(alive={"%1"}))

    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    pid = compute_ccb_project_id(work_dir)

    ok1 = upsert_registry(
        {
            "ccb_session_id": "s1",
            "ccb_project_id": pid,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "providers": {"codex": {"pane_id": "%1", "session_file": str(work_dir / ".ccb" / ".codex-session")}},
        }
    )
    assert ok1 is True

    ok2 = upsert_registry(
        {
            "ccb_session_id": "s1",
            "ccb_project_id": pid,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "providers": {"gemini": {"pane_id": "%1", "session_file": str(work_dir / ".ccb" / ".gemini-session")}},
        }
    )
    assert ok2 is True

    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-s1.json"
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    assert data["ccb_project_id"] == pid
    assert "providers" in data
    assert "codex" in data["providers"]
    assert "gemini" in data["providers"]


def test_upsert_registry_persists_launcher_pid_from_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert upsert_registry({"ccb_session_id": "ai-123-4242", "providers": {"codex": {"pane_id": "%1"}}})

    data = json.loads(
        (tmp_path / ".ccb" / "run" / "ccb-session-ai-123-4242.json").read_text(encoding="utf-8")
    )
    assert data["ccb_pid"] == 4242


def test_load_registry_by_project_id_filters_dead_panes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    pid = compute_ccb_project_id(work_dir)

    # Newer but dead.
    _write_registry_file(
        tmp_path,
        "new",
        {
            "ccb_session_id": "new",
            "ccb_project_id": pid,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%dead", "pane_title_marker": "CCB-Codex-new"}},
        },
    )
    # Older but alive.
    _write_registry_file(
        tmp_path,
        "old",
        {
            "ccb_session_id": "old",
            "ccb_project_id": pid,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()) - 10,
            "providers": {"codex": {"pane_id": "%alive", "pane_title_marker": "CCB-Codex-old"}},
        },
    )

    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend(alive={"%alive"}, marker_map={"CCB-Codex-old": "%alive"}),
    )
    rec = load_registry_by_project_id(pid, "codex")
    assert rec is not None
    assert rec.get("ccb_session_id") == "old"


def test_load_registry_by_pane_selects_exact_same_project_tab(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    pid = compute_ccb_project_id(work_dir)

    for session_id, pane_id, updated_at in (
        ("current-tab", "14", int(time.time()) - 10),
        ("newer-other-tab", "22", int(time.time())),
    ):
        _write_registry_file(
            tmp_path,
            session_id,
            {
                "ccb_session_id": session_id,
                "ccb_project_id": pid,
                "work_dir": str(work_dir),
                "terminal": "wezterm",
                "updated_at": updated_at,
                "providers": {
                    "claude": {"pane_id": "2" if pane_id == "14" else "21"},
                    "codex": {"pane_id": pane_id},
                },
            },
        )

    rec = load_registry_by_pane("14", ccb_project_id=pid, terminal="wezterm")

    assert rec is not None
    assert rec["ccb_session_id"] == "current-tab"


def test_load_registry_by_project_id_infers_missing_project_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(pane_registry, "get_backend_for_session", lambda _rec: _FakeBackend(alive={"%1"}))

    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    pid = compute_ccb_project_id(work_dir)

    # Legacy record missing ccb_project_id (should infer from work_dir).
    _write_registry_file(
        tmp_path,
        "legacy",
        {
            "ccb_session_id": "legacy",
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%1", "pane_title_marker": "CCB-Codex-legacy"}},
        },
    )

    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend(alive={"%1"}, marker_map={"CCB-Codex-legacy": "%1"}),
    )
    rec = load_registry_by_project_id(pid, "codex")
    assert rec is not None
    assert rec.get("ccb_session_id") == "legacy"


def test_load_registry_by_project_id_rejects_reused_tmux_pane_id_with_wrong_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    pid = compute_ccb_project_id(work_dir)

    _write_registry_file(
        tmp_path,
        "stale",
        {
            "ccb_session_id": "stale",
            "ccb_project_id": pid,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%4", "pane_title_marker": "CCB-Codex-stale"}},
        },
    )

    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend(alive={"%4"}, marker_map={"CCB-Codex-other": "%4"}),
    )

    assert load_registry_by_project_id(pid, "codex") is None


def test_load_registry_by_project_id_rejects_reused_wezterm_pane_id_with_wrong_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    pid = compute_ccb_project_id(work_dir)

    _write_registry_file(
        tmp_path,
        "stale",
        {
            "ccb_session_id": "stale",
            "ccb_project_id": pid,
            "work_dir": str(work_dir),
            "terminal": "wezterm",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "23", "pane_title_marker": "CCB-Codex-stale"}},
        },
    )

    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend(alive={"23"}, cwd_map={"23": str(other_dir)}),
    )

    assert load_registry_by_project_id(pid, "codex") is None


# --------------------------------------------------------------------------
# live_sessions inventory carried by the registry
# --------------------------------------------------------------------------

def test_live_sessions_round_trips_through_upsert_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "7"},
                {"live_id": "s2", "provider": "codex", "pane_id": "8"},
            ],
        }
    )

    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1.json"
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    assert {entry["live_id"] for entry in data["live_sessions"]} == {"s1", "s2"}

    sessions = live_sessions_for_record(data)
    assert sorted(s.live_id for s in sessions) == ["s1", "s2"]
    assert all(s.provider == "codex" for s in sessions)


def test_upsert_registry_incoming_write_without_the_key_preserves_stored_live_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [{"live_id": "s1", "provider": "codex", "pane_id": "7"}],
        }
    )

    # A later write that never mentions live_sessions (e.g. just touching
    # providers) must not erase the stored inventory.
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "providers": {"codex": {"pane_id": "7"}},
        }
    )

    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1.json"
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    assert [entry["live_id"] for entry in data["live_sessions"]] == ["s1"]


def test_upsert_registry_merges_live_sessions_by_live_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [{"live_id": "s1", "provider": "codex", "pane_id": "7"}],
        }
    )
    # A second write naming a different live_id must add to the inventory,
    # not replace it wholesale.
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [{"live_id": "s2", "provider": "codex", "pane_id": "8"}],
        }
    )

    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1.json"
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    assert {entry["live_id"] for entry in data["live_sessions"]} == {"s1", "s2"}


def test_legacy_record_without_live_sessions_key_resolves_exactly_as_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "providers": {"codex": {"pane_id": "7"}},
        }
    )

    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1.json"
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    assert "live_sessions" not in data

    sessions = live_sessions_for_record(data)
    assert len(sessions) == 1
    assert sessions[0].provider == "codex"
    assert sessions[0].live_id == "legacy:ai-1:codex"


def test_broken_live_sessions_key_reads_as_empty_not_a_providers_fallback() -> None:
    # A `live_sessions` key that fails to parse is a broken record, not a
    # legacy one: it must never be treated as if the key were absent, which
    # would resurrect a `providers`-derived session as a false "one true
    # destination."
    record = {
        "ccb_session_id": "ai-1",
        "work_dir": "/w",
        "live_sessions": "not-a-list",
        "providers": {"codex": {"pane_id": "%3"}},
    }
    assert live_sessions_for_record(record) == []


# --------------------------------------------------------------------------
# live_sessions write-path validation (fail-closed, never repair)
# --------------------------------------------------------------------------

def test_upsert_registry_rejects_malformed_incoming_entry_and_leaves_file_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [{"live_id": "s1", "provider": "codex", "pane_id": "7"}],
        }
    )
    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1.json"
    before = reg_path.read_bytes()

    ok = upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [
                {"live_id": "s2", "provider": "codex", "pane_id": "8"},
                "not-a-dict",
            ],
        }
    )

    assert ok is False
    assert reg_path.read_bytes() == before


def test_upsert_registry_rejects_duplicate_incoming_live_id_and_leaves_file_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [{"live_id": "s1", "provider": "codex", "pane_id": "7"}],
        }
    )
    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1.json"
    before = reg_path.read_bytes()

    ok = upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [
                {"live_id": "s2", "provider": "codex", "pane_id": "8"},
                {"live_id": "s2", "provider": "codex", "pane_id": "9"},
            ],
        }
    )

    assert ok is False
    assert reg_path.read_bytes() == before


def test_upsert_registry_rejects_non_list_live_sessions_and_leaves_file_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [{"live_id": "s1", "provider": "codex", "pane_id": "7"}],
        }
    )
    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1.json"
    before = reg_path.read_bytes()

    ok = upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": "nope",
        }
    )

    assert ok is False
    assert reg_path.read_bytes() == before


def test_upsert_registry_rejects_write_when_stored_inventory_already_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # Simulate a pre-existing broken record directly (upsert_registry itself
    # refuses to ever create one).
    _write_registry_file(
        tmp_path,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": "nope",
        },
    )
    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1.json"
    before = reg_path.read_bytes()

    ok = upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [{"live_id": "s1", "provider": "codex", "pane_id": "7"}],
        }
    )

    assert ok is False
    assert reg_path.read_bytes() == before


def test_upsert_registry_rejects_merged_result_whose_scope_disagrees_with_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [{"live_id": "s1", "provider": "codex", "pane_id": "7"}],
        }
    )
    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1.json"
    before = reg_path.read_bytes()

    # This entry's own launch_id disagrees with the record's ccb_session_id,
    # which makes the merged inventory invalid for its containing scope.
    ok = upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [
                {"live_id": "s2", "provider": "codex", "pane_id": "8", "launch_id": "ai-2"},
            ],
        }
    )

    assert ok is False
    assert reg_path.read_bytes() == before


def test_upsert_registry_valid_write_preserves_siblings_it_did_not_mention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "7"},
                {"live_id": "s2", "provider": "codex", "pane_id": "8"},
            ],
        }
    )

    # A write naming only s2 must not drop the sibling s1 it never mentioned.
    ok = upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [{"live_id": "s2", "provider": "codex", "pane_id": "9"}],
        }
    )
    assert ok is True

    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1.json"
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    by_id = {entry["live_id"]: entry for entry in data["live_sessions"]}
    assert by_id["s1"]["pane_id"] == "7"
    assert by_id["s2"]["pane_id"] == "9"


def test_upsert_registry_explicit_null_is_not_treated_as_keep_old_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [
                {
                    "live_id": "s1",
                    "provider": "codex",
                    "pane_id": "7",
                    "pane_title_marker": "CCB-Codex-1",
                }
            ],
        }
    )

    # A whole-record replace: an explicit null for pane_title_marker must
    # survive into the stored entry, not be silently read as "leave the old
    # marker alone" the way the legacy providers merge treats a None value.
    ok = upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "9", "pane_title_marker": None}
            ],
        }
    )
    assert ok is True

    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1.json"
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    entry = data["live_sessions"][0]
    assert entry["pane_id"] == "9"
    assert entry.get("pane_title_marker") is None


# --------------------------------------------------------------------------
# Retained inventory must be revalidated against a changed scope
# --------------------------------------------------------------------------

def test_upsert_registry_rejects_scope_change_that_invalidates_retained_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    proj = tmp_path / "proj"
    other = tmp_path / "other"
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(proj),
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "7", "work_dir": str(proj)},
            ],
        }
    )
    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1.json"
    before = reg_path.read_bytes()

    # Omits live_sessions entirely, but changes work_dir to something the
    # stored entry's own explicit work_dir now disagrees with.
    ok = upsert_registry({"ccb_session_id": "ai-1", "work_dir": str(other)})

    assert ok is False
    assert reg_path.read_bytes() == before


def test_upsert_registry_scope_compatible_write_without_live_sessions_key_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    proj = tmp_path / "proj"
    other = tmp_path / "other"
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(proj),
            # No explicit work_dir on the entry itself, so it inherits — a
            # later change to the record's work_dir doesn't create a conflict.
            "live_sessions": [{"live_id": "s1", "provider": "codex", "pane_id": "7"}],
        }
    )

    ok = upsert_registry({"ccb_session_id": "ai-1", "work_dir": str(other)})

    assert ok is True
    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1.json"
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    assert data["work_dir"] == str(other)
    assert [entry["live_id"] for entry in data["live_sessions"]] == ["s1"]


def test_upsert_registry_scope_change_without_any_stored_inventory_is_unaffected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    proj = tmp_path / "proj"
    other = tmp_path / "other"
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(proj),
            "providers": {"codex": {"pane_id": "7"}},
        }
    )

    ok = upsert_registry({"ccb_session_id": "ai-1", "work_dir": str(other)})

    assert ok is True
    reg_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1.json"
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    assert data["work_dir"] == str(other)
    assert "live_sessions" not in data


def test_upsert_registry_no_scope_change_leaves_already_broken_inventory_write_unaffected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A write that changes nothing about scope must behave exactly as it
    # does today, pre-existing brokenness included — today an absent-key
    # write never revalidates a retained inventory at all.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _write_registry_file(
        tmp_path,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "live_sessions": "nope",
        },
    )

    ok = upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path / "proj"),
            "providers": {"codex": {"pane_id": "7"}},
        }
    )

    assert ok is True
