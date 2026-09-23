"""A lead can address one member of a Codex pair by live ID, and each session
can record its own role for `ccb-list` without roles ever reaching routing."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import time
from pathlib import Path

import pytest

import ccb_runtime_status
import live_roles
import pane_registry
from ccb_runtime_status import resolve_live_route
from live_roles import roles_for_record, set_own_role
from project_id import compute_ccb_project_id

ROOT = Path(__file__).resolve().parents[1]
LAUNCH = "ai-1"


class _FakeBackend:
    def __init__(self, alive: set[str], marker_map: dict[str, str]):
        self.alive = alive
        self.marker_map = marker_map

    def is_alive(self, pane_id: str) -> bool:
        return pane_id in self.alive

    def find_pane_by_title_marker(self, marker: str, cwd_hint: str = "") -> str | None:
        return self.marker_map.get(marker)


@pytest.fixture
def pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """One launch: Claude lead plus two Codex, all live."""
    monkeypatch.setenv("HOME", str(tmp_path))
    work_dir = tmp_path / "project"
    cfg = work_dir / ".ccb"
    cfg.mkdir(parents=True)
    (cfg / "ccb.config").write_text("claude,codex,codex\n", encoding="utf-8")
    project_id = compute_ccb_project_id(work_dir)
    sessions = {"lead": ("claude", "%1"), "cx1": ("codex", "%2"), "cx2": ("codex", "%3")}
    entries = []
    for live_id, (provider, pane) in sessions.items():
        session_file = cfg / f"{live_id}-session.json"
        session_file.write_text(
            json.dumps({"active": True, "provider": provider, "ccb_project_id": project_id, "pane_id": pane}),
            encoding="utf-8",
        )
        entries.append(
            {
                "live_id": live_id,
                "provider": provider,
                "pane_id": pane,
                "terminal": "tmux",
                "pane_title_marker": f"CCB-{live_id}",
                "session_file": str(session_file),
                "auth_token": f"tok-{live_id}",
                "active": True,
            }
        )
    record = {
        "ccb_session_id": LAUNCH,
        "ccb_project_id": project_id,
        "work_dir": str(work_dir),
        "terminal": "tmux",
        "updated_at": int(time.time()),
        "providers": {},
        "live_sessions": entries,
    }
    path = tmp_path / ".ccb" / "run" / f"ccb-session-{LAUNCH}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(ccb_runtime_status, "is_project_askd_online", lambda *_a, **_k: True)
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%1", "%2", "%3"}, {f"CCB-{k}": v[1] for k, v in sessions.items()}),
    )
    return work_dir, path


def _record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _rewrite_entry(path: Path, live_id: str, **changes) -> None:
    record = _record(path)
    for entry in record["live_sessions"]:
        if entry["live_id"] == live_id:
            entry.update(changes)
    path.write_text(json.dumps(record), encoding="utf-8")


def _lead_route(work_dir: Path, provider: str, target: str):
    return resolve_live_route(
        provider, work_dir, caller_live_id="lead", caller_token="tok-lead", target_live_id=target
    )


# --- addressing one member of the pair ------------------------------------------


def test_lead_without_a_target_is_still_refused_as_ambiguous(pair) -> None:
    work_dir, _ = pair
    resolution, _caller = resolve_live_route("codex", work_dir, caller_live_id="lead", caller_token="tok-lead")
    assert not resolution.ok


@pytest.mark.parametrize("target", ["cx1", "cx2"])
def test_lead_reaches_the_exact_codex_it_names(pair, target: str) -> None:
    work_dir, _ = pair
    resolution, caller = _lead_route(work_dir, "codex", target)
    assert resolution.ok, resolution
    assert resolution.session.live_id == target
    assert caller is not None and caller.live_id == "lead"


def test_target_must_exist_in_the_callers_launch(pair) -> None:
    work_dir, _ = pair
    resolution, _ = _lead_route(work_dir, "codex", "not-here")
    assert not resolution.ok and resolution.error == "unavailable"


def test_target_must_belong_to_the_named_provider(pair) -> None:
    work_dir, _ = pair
    resolution, _ = _lead_route(work_dir, "claude", "cx1")
    assert not resolution.ok


def test_target_that_is_gone_is_refused(pair) -> None:
    work_dir, path = pair
    _rewrite_entry(path, "cx2", active=False)
    resolution, _ = _lead_route(work_dir, "codex", "cx2")
    assert not resolution.ok and resolution.error == "unavailable"


def test_target_cannot_be_the_caller_itself(pair) -> None:
    work_dir, _ = pair
    resolution, _ = resolve_live_route(
        "codex", work_dir, caller_live_id="cx1", caller_token="tok-cx1", target_live_id="cx1"
    )
    assert not resolution.ok and resolution.error == "self_only"


# --- roles: recorded by the session itself, shown only while current -------------


def test_session_records_its_own_role(pair) -> None:
    _, path = pair
    ok, _ = set_own_role("cx1", "tok-cx1", "implementer")
    assert ok
    assert roles_for_record(_record(path)) == {"cx1": "implementer"}


def test_role_needs_the_sessions_own_credential(pair) -> None:
    _, path = pair
    ok, detail = set_own_role("cx1", "tok-lead", "implementer")
    assert not ok and "credential" in detail
    assert roles_for_record(_record(path)) == {}


def test_a_held_role_cannot_be_claimed_twice(pair) -> None:
    set_own_role("cx1", "tok-cx1", "ratifier")
    ok, detail = set_own_role("cx2", "tok-cx2", "ratifier")
    assert not ok and "already held" in detail


def test_changing_role_replaces_the_old_one(pair) -> None:
    _, path = pair
    set_own_role("cx1", "tok-cx1", "ratifier")
    set_own_role("cx1", "tok-cx1", "implementer")
    assert roles_for_record(_record(path)) == {"cx1": "implementer"}
    ok, _ = set_own_role("cx2", "tok-cx2", "ratifier")
    assert ok


def test_clear_removes_the_role(pair) -> None:
    _, path = pair
    set_own_role("cx1", "tok-cx1", "implementer")
    ok, _ = set_own_role("cx1", "tok-cx1", "")
    assert ok and roles_for_record(_record(path)) == {}


def test_role_does_not_follow_a_replaced_pane(pair) -> None:
    _, path = pair
    set_own_role("cx1", "tok-cx1", "implementer")
    _rewrite_entry(path, "cx1", pane_id="%9")
    assert roles_for_record(_record(path)) == {}
    # And the stale claim no longer blocks another session from the name.
    ok, _ = set_own_role("cx2", "tok-cx2", "implementer")
    assert ok


def test_role_disappears_when_the_session_is_gone(pair) -> None:
    _, path = pair
    set_own_role("cx1", "tok-cx1", "implementer")
    _rewrite_entry(path, "cx1", active=False)
    assert roles_for_record(_record(path)) == {}


def test_a_new_launch_starts_without_roles(pair) -> None:
    _, path = pair
    set_own_role("cx1", "tok-cx1", "implementer")
    record = _record(path)
    record["ccb_session_id"] = "ai-2"
    assert roles_for_record(record) == {}


def test_role_names_are_restricted(pair) -> None:
    for bad in ("Implementer!", "x" * 40, "1st"):
        ok, _ = set_own_role("cx1", "tok-cx1", bad)
        assert not ok


def test_roles_never_reach_routing(pair) -> None:
    work_dir, _ = pair
    set_own_role("cx1", "tok-cx1", "implementer")
    resolution, _ = _lead_route(work_dir, "codex", "implementer")
    assert not resolution.ok


def test_role_file_is_private(pair) -> None:
    set_own_role("cx1", "tok-cx1", "implementer")
    mode = live_roles.roles_path_for_launch(LAUNCH).stat().st_mode & 0o777
    assert mode == 0o600


# --- the ccb-role command ---------------------------------------------------------


def _load_role_cli():
    loader = importlib.machinery.SourceFileLoader("ccb_role_test", str(ROOT / "bin" / "ccb-role"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_ccb_role_uses_the_sessions_environment(pair, monkeypatch, capsys) -> None:
    _, path = pair
    cli = _load_role_cli()
    monkeypatch.setattr(cli, "inside_managed_codex_sandbox", lambda: False)
    monkeypatch.setenv("CCB_LIVE_ID", "cx2")
    monkeypatch.setenv("CCB_LIVE_TOKEN", "tok-cx2")

    assert cli.main(["set", "ratifier"]) == 0
    assert roles_for_record(_record(path)) == {"cx2": "ratifier"}


def test_ccb_role_outside_a_pair_launch_refuses(monkeypatch, capsys) -> None:
    cli = _load_role_cli()
    monkeypatch.delenv("CCB_LIVE_ID", raising=False)
    monkeypatch.delenv("CCB_LIVE_TOKEN", raising=False)
    assert cli.main(["set", "implementer"]) == 1
    assert "no live session ID" in capsys.readouterr().err


def test_ccb_role_goes_through_the_daemon_inside_the_sandbox(monkeypatch, capsys) -> None:
    cli = _load_role_cli()
    seen = {}
    monkeypatch.setattr(cli, "inside_managed_codex_sandbox", lambda: True)
    monkeypatch.setattr(cli, "resolve_daemon_work_dir", lambda wd: wd)

    def _daemon(work_dir, live_id, token, role):
        seen.update(live_id=live_id, token=token, role=role)
        return True, "recorded"

    monkeypatch.setattr(cli, "daemon_set_role", _daemon)
    monkeypatch.setattr(cli, "set_own_role", lambda *_a: pytest.fail("sandbox must not write directly"))
    monkeypatch.setenv("CCB_LIVE_ID", "cx1")
    monkeypatch.setenv("CCB_LIVE_TOKEN", "tok-cx1")

    assert cli.main(["set", "implementer"]) == 0
    assert seen == {"live_id": "cx1", "token": "tok-cx1", "role": "implementer"}


def test_daemon_set_role_operation_applies_the_same_checks(pair, tmp_path) -> None:
    import askd.daemon as askd_daemon

    daemon = askd_daemon.UnifiedAskDaemon(state_file=tmp_path / "askd.json")
    bad = daemon._handle_request({"operation": "set_role", "live_id": "cx1", "live_token": "wrong", "role": "x"})
    good = daemon._handle_request(
        {"operation": "set_role", "live_id": "cx1", "live_token": "tok-cx1", "role": "implementer"}
    )
    assert bad["exit_code"] == 1
    assert good["exit_code"] == 0
