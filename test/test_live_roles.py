"""A lead can address one member of a Codex pair by live ID, and each session
can record its own role for `ccb-list` without roles ever reaching routing."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
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
    def __init__(
        self,
        alive: set[str],
        marker_map: dict[str, str],
        pane_pids: dict[str, str] | None = None,
    ):
        self.alive = alive
        self.marker_map = marker_map
        self.pane_pids = dict(pane_pids or {})

    def is_alive(self, pane_id: str) -> bool:
        return pane_id in self.alive

    def find_pane_by_title_marker(self, marker: str, cwd_hint: str = "") -> str | None:
        return self.marker_map.get(marker)

    def pane_pid(self, pane_id: str) -> str:
        return self.pane_pids.get(pane_id, "")


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
            json.dumps(
                {
                    "session_id": LAUNCH,
                    "active": True,
                    "provider": provider,
                    "ccb_project_id": project_id,
                    "pane_id": pane,
                }
            ),
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


def test_ccb_role_without_any_launch_credential_refuses(monkeypatch, capsys) -> None:
    cli = _load_role_cli()
    for key in ("CCB_MEMBER_ID", "CCB_MEMBER_TOKEN", "CCB_LIVE_ID", "CCB_LIVE_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    assert cli.main(["set", "implementer"]) == 1
    assert "no CCB launch credential" in capsys.readouterr().err


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


# --- roles in launches WITHOUT a live-session inventory ---------------------------
#
# The registry shape below is an actual non-pair record taken from a live
# launch: extra claude_* fields on one provider, top-level claude_pane_id /
# codex_pane_id with no pi equivalent, and no `live_sessions` key at all. Only
# the home paths are rewritten for the temp project, and claim credentials are
# added the way the launcher now writes them.
REAL_NON_PAIR_RECORD = {
    "ccb_session_id": "ai-1793895837-1479705",
    "ccb_project_id": "f8cc696c6fe8102f7f96e61fb986e6f989639baba0456d9919a111a164e1be2f",
    "work_dir": "/home/user/dev/claude_code_bridge",
    "terminal": "tmux",
    "providers": {
        "claude": {
            "pane_id": "%8",
            "pane_title_marker": "CCB-Claude-f8cc696c",
            "session_file": "/home/user/dev/claude_code_bridge/.ccb/.claude-session",
            "claude_session_id": "a971d925-d525-42a1-b186-be9695b99d04",
            "claude_session_path": "/home/user/.claude/projects/-home-user-dev-claude-code-bridge/a971d925-d525-42a1-b186-be9695b99d04.jsonl",
        },
        "pi": {
            "pane_id": "%11",
            "pane_title_marker": "CCB-Pi-f8cc696c",
            "session_file": "/home/user/dev/claude_code_bridge/.ccb/.pi-session",
        },
        "codex": {
            "pane_id": "%12",
            "pane_title_marker": "CCB-Codex-f8cc696c",
            "session_file": "/home/user/dev/claude_code_bridge/.ccb/.codex-session",
        },
    },
    "ccb_pid": 1479705,
    "updated_at": 1793895839,
    "claude_pane_id": "%8",
    "codex_pane_id": "%12",
}

ALL_PROVIDERS = ("claude", "codex", "gemini", "opencode", "hermes", "pi")


class NonPairLaunch:
    def __init__(self, work_dir: Path, record_path: Path, members: dict[str, tuple[str, str]], panes: dict[str, str]):
        self.work_dir = work_dir
        self.record_path = record_path
        self.members = members
        self.panes = panes

    def record(self) -> dict:
        return json.loads(self.record_path.read_text(encoding="utf-8"))


@pytest.fixture
def non_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real-shaped non-pair launch with claim metadata for all six providers."""

    def build(providers: tuple[str, ...] = ALL_PROVIDERS) -> NonPairLaunch:
        work_dir = tmp_path / "project"
        cfg = work_dir / ".ccb"
        cfg.mkdir(parents=True, exist_ok=True)
        project_id = compute_ccb_project_id(work_dir)
        record = json.loads(json.dumps(REAL_NON_PAIR_RECORD))
        record["ccb_project_id"] = project_id
        record["work_dir"] = str(work_dir)
        record["ccb_pid"] = os.getpid()
        record["updated_at"] = int(time.time())
        entry_by_provider = {}
        members: dict[str, tuple[str, str]] = {}
        pane_markers: dict[str, str] = {}
        for index, provider in enumerate(providers, start=1):
            pane = f"%{index}0"
            marker = f"CCB-{provider}-{project_id[:8]}"
            session_file = cfg / f".{provider}-session"
            session_file.write_text(
                json.dumps(
                    {
                        "session_id": record["ccb_session_id"],
                        "ccb_project_id": project_id,
                        "work_dir": str(work_dir),
                        "pane_id": pane,
                        "pane_title_marker": marker,
                        "terminal": "tmux",
                        "active": True,
                    }
                ),
                encoding="utf-8",
            )
            entry = {
                "pane_id": pane,
                "pane_title_marker": marker,
                "session_file": str(session_file),
                "member_id": f"member-{provider}",
                "member_token": f"token-{provider}",
            }
            if provider == "claude":
                entry["claude_session_id"] = "a971d925-d525-42a1-b186-be9695b99d04"
                entry["claude_session_path"] = str(cfg / "claude.jsonl")
            entry_by_provider[provider] = entry
            members[provider] = (entry["member_id"], entry["member_token"])
            pane_markers[pane] = marker
        record["providers"] = entry_by_provider
        record["claude_pane_id"] = entry_by_provider.get("claude", {}).get("pane_id", "")
        record["codex_pane_id"] = entry_by_provider.get("codex", {}).get("pane_id", "")
        path = tmp_path / ".ccb" / "run" / f"ccb-session-{record['ccb_session_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        monkeypatch.setenv("HOME", str(tmp_path))
        marker_to_pane = {marker: pane for pane, marker in pane_markers.items()}
        monkeypatch.setattr(
            pane_registry,
            "get_backend_for_session",
            lambda _rec: _FakeBackend(set(pane_markers), marker_to_pane),
        )
        return NonPairLaunch(work_dir, path, members, pane_markers)

    return build


def _roles_file(tmp_path: Path, launch_id: str = "ai-1793895837-1479705") -> Path:
    return tmp_path / ".ccb" / "run" / f"ccb-roles-{launch_id}.json"


def _snapshot_claims(tmp_path: Path) -> bytes:
    path = _roles_file(tmp_path)
    return path.read_bytes() if path.exists() else b""


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_every_provider_can_claim_and_clear_a_role_in_a_non_pair_launch(
    non_pair, tmp_path: Path, provider: str
) -> None:
    launch = non_pair((provider,))
    member_id, token = launch.members[provider]

    ok, detail = set_own_role(member_id, token, "implementer")
    assert ok, detail
    assert roles_for_record(launch.record()) == {member_id: "implementer"}

    ok, detail = set_own_role(member_id, token, "")
    assert ok, detail
    assert roles_for_record(launch.record()) == {}
    assert _roles_file(tmp_path).stat().st_mode & 0o777 == 0o600


def test_mixed_non_pair_launch_keeps_one_role_per_member(non_pair) -> None:
    launch = non_pair(("claude", "pi", "codex"))
    claude_id, claude_token = launch.members["claude"]
    pi_id, pi_token = launch.members["pi"]

    assert set_own_role(claude_id, claude_token, "lead")[0]
    assert set_own_role(pi_id, pi_token, "implementer")[0]
    assert roles_for_record(launch.record()) == {claude_id: "lead", pi_id: "implementer"}


def test_non_pair_claim_needs_that_members_own_credential(non_pair, tmp_path: Path) -> None:
    launch = non_pair(("claude", "pi"))
    claude_id, _ = launch.members["claude"]
    _, pi_token = launch.members["pi"]
    before = _snapshot_claims(tmp_path)

    ok, detail = set_own_role(claude_id, pi_token, "lead")

    assert not ok and "credential" in detail
    assert _snapshot_claims(tmp_path) == before


def test_missing_credential_is_refused_without_writing(non_pair, tmp_path: Path) -> None:
    launch = non_pair(("claude",))
    claude_id, _ = launch.members["claude"]

    for token in ("", "   "):
        ok, detail = set_own_role(claude_id, token, "lead")
        assert not ok and "credential" in detail
    assert not _roles_file(tmp_path).exists()


def test_cross_launch_credential_is_refused(non_pair, tmp_path: Path) -> None:
    first = non_pair(("claude",))
    second_record = first.record()
    second_record["ccb_session_id"] = "ai-2"
    second_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-2.json"
    second_path.write_text(json.dumps(second_record), encoding="utf-8")
    claude_id, claude_token = first.members["claude"]

    # The second launch reuses the member id with a different credential.
    ok, detail = set_own_role(claude_id, claude_token, "lead")
    assert not ok and "more than one launch record" in detail
    assert not _roles_file(tmp_path).exists()


def test_malformed_inventory_never_falls_back_to_provider_entries(non_pair) -> None:
    launch = non_pair(("claude", "pi"))
    record = launch.record()
    record["live_sessions"] = "not-a-list"
    launch.record_path.write_text(json.dumps(record), encoding="utf-8")
    claude_id, claude_token = launch.members["claude"]

    ok, detail = set_own_role(claude_id, claude_token, "lead")

    assert not ok
    assert roles_for_record(launch.record()) == {}


def test_dead_launch_owner_is_refused(non_pair, tmp_path: Path) -> None:
    launch = non_pair(("claude",))
    record = launch.record()
    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    exited.wait()
    record["ccb_pid"] = exited.pid
    launch.record_path.write_text(json.dumps(record), encoding="utf-8")
    claude_id, claude_token = launch.members["claude"]

    ok, detail = set_own_role(claude_id, claude_token, "lead")

    assert not ok and "no longer running" in detail
    assert not _roles_file(tmp_path).exists()


def test_claim_refuses_when_the_members_pane_is_gone(non_pair, tmp_path: Path) -> None:
    launch = non_pair(("claude",))
    claude_id, claude_token = launch.members["claude"]
    _only_pane_alive(launch, "%10", alive=False)

    ok, detail = set_own_role(claude_id, claude_token, "lead")

    assert not ok and "pane" in detail
    assert not _roles_file(tmp_path).exists()


def _only_pane_alive(launch: NonPairLaunch, pane: str, *, alive: bool) -> None:
    markers = dict(launch.panes)
    alive_set = set(markers) if alive else set(markers) - {pane}
    marker_to_pane = {marker: held_by for held_by, marker in markers.items()}
    pane_registry.get_backend_for_session = lambda _rec: _FakeBackend(alive_set, marker_to_pane)


def test_role_follows_pane_specifics_not_a_provider_name(pair) -> None:
    _, path = pair
    set_own_role("cx1", "tok-cx1", "implementer")
    record = _record(path)
    record["providers"]["codex"] = {"pane_id": "%3"}
    assert roles_for_record(record) == {"cx1": "implementer"}


def test_role_claim_write_is_serialized_across_processes(non_pair, tmp_path: Path) -> None:
    launch = non_pair(ALL_PROVIDERS)
    barrier = tmp_path / "go"
    results = _run_parallel_claims(
        tmp_path, launch, [launch.members[p] for p in ALL_PROVIDERS], "implementer", barrier
    )

    winners = [entry for entry in results if entry["ok"]]
    assert len(winners) == 1, results
    roles = roles_for_record(launch.record())
    assert list(roles.values()) == ["implementer"]


def test_unrelated_concurrent_claims_are_all_retained(non_pair, tmp_path: Path) -> None:
    launch = non_pair(ALL_PROVIDERS)
    role_names = ("lead", "implementer", "ratifier", "reviewer", "architect", "integrator")
    barrier = tmp_path / "go"
    results = _run_parallel_claims(
        tmp_path,
        launch,
        [launch.members[p] for p in ALL_PROVIDERS],
        role_names,
        barrier,
    )

    assert all(entry["ok"] for entry in results), results
    assert sorted(roles_for_record(launch.record()).values()) == sorted(role_names)


def _run_parallel_claims(
    tmp_path: Path,
    launch: NonPairLaunch,
    members: list[tuple[str, str]],
    roles,
    barrier: Path,
) -> list[dict]:
    if isinstance(roles, str):
        roles = [roles] * len(members)
    pane_to_marker = dict(launch.panes)
    marker_to_pane = {marker: pane for pane, marker in pane_to_marker.items()}
    script = (
        "import json, os, sys, time\n"
        f"sys.path.insert(0, {str(ROOT / 'lib')!r})\n"
        "import pane_registry\n"
        "from live_roles import set_own_role\n"
        f"alive = set({sorted(pane_to_marker)!r})\n"
        f"markers = {marker_to_pane!r}\n"
        "class _Backend:\n"
        "    def is_alive(self, pane_id):\n"
        "        return str(pane_id) in alive\n"
        "    def find_pane_by_title_marker(self, marker, cwd=''):\n"
        "        return markers.get(marker)\n"
        "pane_registry.get_backend_for_session = lambda _rec: _Backend()\n"
        f"while not os.path.exists({str(barrier)!r}):\n"
        "    time.sleep(0.002)\n"
        "member_id, token = sys.argv[1], sys.argv[2]\n"
        "ok, detail = set_own_role(member_id, token, sys.argv[3])\n"
        "print(json.dumps({'ok': ok, 'detail': detail}))\n"
    )
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, member_id, token, role],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for (member_id, token), role in zip(members, roles)
    ]
    barrier.write_text("go", encoding="utf-8")
    results = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=60)
        assert proc.returncode == 0, stderr
        assert stdout.strip(), stderr
        results.append(json.loads(stdout.strip().splitlines()[-1]))
    return results


def test_pane_replacement_invalidates_the_claim(non_pair, tmp_path: Path) -> None:
    launch = non_pair(("claude",))
    claude_id, claude_token = launch.members["claude"]
    assert set_own_role(claude_id, claude_token, "lead")[0]
    record = launch.record()
    previous_pane = record["providers"]["claude"]["pane_id"]

    removed = live_roles.invalidate_pane_claims(record["ccb_session_id"], previous_pane)

    assert removed == 1
    assert roles_for_record(record) == {}


def test_launch_end_clears_claims(non_pair, tmp_path: Path) -> None:
    launch = non_pair(("claude", "pi"))
    assert set_own_role(*launch.members["claude"], "lead")[0]
    assert live_roles.invalidate_roles_for_launch("ai-1793895837-1479705") == 1
    assert roles_for_record(launch.record()) == {}
    assert not _roles_file(tmp_path).exists()


def test_daemon_set_role_operation_accepts_a_member_credential(non_pair, tmp_path: Path) -> None:
    import askd.daemon as askd_daemon

    launch = non_pair(("pi",))
    member_id, token = launch.members["pi"]
    daemon = askd_daemon.UnifiedAskDaemon(state_file=tmp_path / "askd.json")

    bad = daemon._handle_request(
        {"operation": "set_role", "member_id": member_id, "member_token": "wrong", "role": "lead"}
    )
    good = daemon._handle_request(
        {"operation": "set_role", "member_id": member_id, "member_token": token, "role": "lead"}
    )

    assert bad["exit_code"] == 1 and "credential" in bad["reply"]
    assert good["exit_code"] == 0
    assert token not in good["reply"]
    assert roles_for_record(launch.record()) == {member_id: "lead"}


def test_ccb_role_cli_records_a_member_credential_outside_a_pair(non_pair, monkeypatch, capsys) -> None:
    launch = non_pair(("hermes",))
    member_id, token = launch.members["hermes"]
    cli = _load_role_cli()
    monkeypatch.setattr(cli, "inside_managed_codex_sandbox", lambda: False)
    monkeypatch.delenv("CCB_LIVE_ID", raising=False)
    monkeypatch.delenv("CCB_LIVE_TOKEN", raising=False)
    monkeypatch.setenv("CCB_MEMBER_ID", member_id)
    monkeypatch.setenv("CCB_MEMBER_TOKEN", token)

    assert cli.main(["set", "ratifier"]) == 0
    assert roles_for_record(launch.record()) == {member_id: "ratifier"}
    assert token not in capsys.readouterr().out


def test_wezterm_launch_can_claim_and_show_a_role(non_pair, tmp_path: Path) -> None:
    launch = non_pair(("claude",))
    record = launch.record()
    record["terminal"] = "wezterm"
    record["providers"]["claude"]["terminal"] = "wezterm"
    launch.record_path.write_text(json.dumps(record), encoding="utf-8")
    session_file = Path(record["providers"]["claude"]["session_file"])
    binding = json.loads(session_file.read_text(encoding="utf-8"))
    binding["terminal"] = "wezterm"
    session_file.write_text(json.dumps(binding), encoding="utf-8")
    claude_id, claude_token = launch.members["claude"]

    assert set_own_role(claude_id, claude_token, "lead")[0]
    assert roles_for_record(launch.record()) == {claude_id: "lead"}


def test_non_pair_launch_never_becomes_a_routing_target(non_pair) -> None:
    launch = non_pair(("claude", "pi"))
    before = resolve_live_route(
        "pi", launch.work_dir, caller_live_id="member-claude", caller_token="token-claude"
    )
    assert before is None

    assert set_own_role("member-claude", "token-claude", "lead")[0]
    assert roles_for_record(launch.record()) == {"member-claude": "lead"}

    after = resolve_live_route(
        "pi", launch.work_dir, caller_live_id="member-claude", caller_token="token-claude"
    )
    assert after is None
    assert set_own_role("member-claude", "token-claude", "")[0]
    assert (
        resolve_live_route(
            "pi", launch.work_dir, caller_live_id="member-claude", caller_token="token-claude"
        )
        is None
    )


def test_role_output_never_contains_a_member_token(non_pair, capsys) -> None:
    launch = non_pair(("claude", "pi"))
    claude_id, claude_token = launch.members["claude"]
    assert set_own_role(claude_id, claude_token, "lead")[0]
    assert set_own_role(claude_id, claude_token, "implementer")[0]
    assert set_own_role(claude_id, claude_token, "")[0]
    ok, detail = set_own_role(claude_id, "not-the-token", "lead")
    assert not ok and claude_token not in detail


# --- current membership: binding, marker, incarnation ----------------------------

def _rebind_backend(launch: NonPairLaunch, **kwargs) -> None:
    markers = dict(launch.panes)
    marker_to_pane = {marker: pane for pane, marker in markers.items()}
    pane_registry.get_backend_for_session = lambda _rec: _FakeBackend(
        set(markers), marker_to_pane, kwargs.get("pane_pids")
    )


def test_claim_refuses_when_the_session_binding_is_missing(non_pair, tmp_path: Path) -> None:
    launch = non_pair(("claude",))
    member_id, token = launch.members["claude"]
    Path(launch.record()["providers"]["claude"]["session_file"]).unlink()

    ok, detail = set_own_role(member_id, token, "lead")

    assert not ok and "binding" in detail
    assert not _roles_file(tmp_path).exists()


def test_claim_refuses_when_the_session_binding_names_another_pane(non_pair, tmp_path: Path) -> None:
    launch = non_pair(("claude",))
    member_id, token = launch.members["claude"]
    session_file = Path(launch.record()["providers"]["claude"]["session_file"])
    data = json.loads(session_file.read_text(encoding="utf-8"))
    data["pane_id"] = "%77"
    session_file.write_text(json.dumps(data), encoding="utf-8")

    ok, detail = set_own_role(member_id, token, "lead")

    assert not ok and "another pane" in detail
    assert not _roles_file(tmp_path).exists()


def test_claim_refuses_when_the_session_binding_names_another_launch(non_pair, tmp_path: Path) -> None:
    launch = non_pair(("claude",))
    member_id, token = launch.members["claude"]
    session_file = Path(launch.record()["providers"]["claude"]["session_file"])
    data = json.loads(session_file.read_text(encoding="utf-8"))
    data["session_id"] = "ai-other"
    session_file.write_text(json.dumps(data), encoding="utf-8")

    ok, detail = set_own_role(member_id, token, "lead")

    assert not ok and "another launch" in detail
    assert not _roles_file(tmp_path).exists()


def test_claim_refuses_when_the_marker_does_not_resolve(non_pair, tmp_path: Path) -> None:
    launch = non_pair(("claude",))
    member_id, token = launch.members["claude"]
    # Pane alive, session binding intact, but the marker resolves nowhere.
    pane_registry.get_backend_for_session = lambda _rec: _FakeBackend(set(launch.panes), {})

    ok, detail = set_own_role(member_id, token, "lead")

    assert not ok and "marker" in detail
    assert not _roles_file(tmp_path).exists()


def test_claim_refuses_when_the_marker_resolves_to_another_pane(non_pair, tmp_path: Path) -> None:
    launch = non_pair(("claude",))
    member_id, token = launch.members["claude"]
    markers = dict(launch.panes)
    stolen = {marker: "%99" for marker in markers}
    pane_registry.get_backend_for_session = lambda _rec: _FakeBackend(
        set(markers) | {"%99"}, stolen
    )

    ok, detail = set_own_role(member_id, token, "lead")

    assert not ok and "marker" in detail
    assert not _roles_file(tmp_path).exists()


def test_backend_without_a_marker_resolver_uses_alive_and_the_binding(
    non_pair,
) -> None:
    launch = non_pair(("claude",))
    member_id, token = launch.members["claude"]

    class _NoResolver:
        def is_alive(self, pane_id: str) -> bool:
            return pane_id in set(launch.panes)

    pane_registry.get_backend_for_session = lambda _rec: _NoResolver()

    ok, detail = set_own_role(member_id, token, "lead")

    assert ok, detail
    assert roles_for_record(launch.record()) == {member_id: "lead"}


def test_recycled_pane_id_with_another_marker_is_not_current(non_pair) -> None:
    """The pane id is alive again, but it is not this session any more."""
    launch = non_pair(("claude", "pi"))
    claude_id, claude_token = launch.members["claude"]
    pi_id, pi_token = launch.members["pi"]
    assert set_own_role(claude_id, claude_token, "lead")[0]

    record = launch.record()
    recycled = record["providers"]["claude"]["pane_id"]
    pi_pane = record["providers"]["pi"]["pane_id"]
    pi_marker = record["providers"]["pi"]["pane_title_marker"]
    # The recycled pane now carries someone else's title, so this session's
    # marker resolves nowhere while the pane itself is alive.
    pane_registry.get_backend_for_session = lambda _rec: _FakeBackend(
        {recycled, pi_pane}, {pi_marker: pi_pane}
    )

    assert roles_for_record(launch.record()) == {}
    assert set_own_role(pi_id, pi_token, "lead")[0]


def test_a_dead_holder_releases_its_role(non_pair, tmp_path: Path) -> None:
    launch = non_pair(("claude", "pi"))
    claude_id, claude_token = launch.members["claude"]
    pi_id, pi_token = launch.members["pi"]
    assert set_own_role(claude_id, claude_token, "implementer")[0]
    assert roles_for_record(launch.record()) == {claude_id: "implementer"}

    # The holder's pane dies; its claim stays on disk untouched.
    claude_pane = launch.record()["providers"]["claude"]["pane_id"]
    markers = dict(launch.panes)
    marker_to_pane = {marker: pane for pane, marker in markers.items()}
    alive = set(markers) - {claude_pane}
    pane_registry.get_backend_for_session = lambda _rec: _FakeBackend(alive, marker_to_pane)

    assert roles_for_record(launch.record()) == {}
    assert set_own_role(pi_id, pi_token, "implementer")[0]
    assert roles_for_record(launch.record()) == {pi_id: "implementer"}


def test_a_respawned_pane_does_not_inherit_the_claim(non_pair) -> None:
    launch = non_pair(("claude",))
    member_id, token = launch.members["claude"]
    pane = launch.record()["providers"]["claude"]["pane_id"]
    _rebind_backend(launch, pane_pids={pane: "1001"})
    assert set_own_role(member_id, token, "lead")[0]

    # Same pane id, same marker, new process: the claim must not be current.
    _rebind_backend(launch, pane_pids={pane: "2002"})

    assert roles_for_record(launch.record()) == {}


@pytest.mark.parametrize("failure", ["lock_timeout", "write_failure"])
def test_a_failed_invalidation_cannot_resurrect_the_claim(
    non_pair, monkeypatch, failure: str
) -> None:
    launch = non_pair(("claude", "pi"))
    claude_id, claude_token = launch.members["claude"]
    pi_id, pi_token = launch.members["pi"]
    pane = launch.record()["providers"]["claude"]["pane_id"]
    _rebind_backend(launch, pane_pids={pane: "1001"})
    assert set_own_role(claude_id, claude_token, "implementer")[0]

    launch_id = "ai-1793895837-1479705"
    if failure == "lock_timeout":
        monkeypatch.setattr(live_roles, "ROLE_LOCK_TIMEOUT_S", 0.05)
        held = live_roles._role_lock(launch_id)
        assert held.acquire()
        try:
            assert live_roles.invalidate_pane_claims(launch_id, pane) is None
        finally:
            held.release()
    else:
        original_write = live_roles._write_claims

        def _boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(live_roles, "_write_claims", _boom)
        assert live_roles.invalidate_pane_claims(launch_id, pane) is None
        monkeypatch.setattr(live_roles, "_write_claims", original_write)

    # Recovery proceeds: the pane respawns with a new process.
    _rebind_backend(launch, pane_pids={pane: "2002"})

    assert roles_for_record(launch.record()) == {}
    assert set_own_role(pi_id, pi_token, "implementer")[0]


# --- real writer bindings ---------------------------------------------------------
#
# `laskd` rewrites .claude-session from the registry projection
# (claude_session_resolver._data_from_registry -> update_claude_binding), which
# carries no launch id. This is the file an actual `ccb pi claude` run left
# behind, verbatim except for the home paths.
REAL_CLAUDE_SESSION_FIELDS = {
    "ccb_project_id": "eb24f3aebfb7009a506fc2d21ece36916af4998559a804ecd76eaa400c0c552f",
    "work_dir": "/home/user/proj",
    "terminal": "tmux",
    "pane_id": "%121",
    "pane_title_marker": "CCB-Claude-eb24f3ae",
    "work_dir_norm": "/home/user/proj",
    "claude_session_path": "/home/user/.claude/projects/-home-user-proj/101ec639-5c70-46d2-8e7d-75d6c38d9f1b.jsonl",
    "claude_session_id": "101ec639-5c70-46d2-8e7d-75d6c38d9f1b",
    "updated_at": "2026-11-05 19:50:39",
    "active": True,
}


def _real_claude_launch(tmp_path, monkeypatch):
    """A non-pair launch whose Claude binding is exactly what laskd writes."""
    work_dir = tmp_path / "proj"
    cfg = work_dir / ".ccb"
    cfg.mkdir(parents=True)
    project_id = compute_ccb_project_id(work_dir)
    session_file = cfg / ".claude-session"
    record = {
        "ccb_session_id": "ai-1793907200-555",
        "ccb_project_id": project_id,
        "work_dir": str(work_dir),
        "terminal": "tmux",
        "ccb_pid": os.getpid(),
        "updated_at": int(time.time()),
        "providers": {
            "claude": {
                "pane_id": "%121",
                "pane_title_marker": "CCB-Claude-eb24f3ae",
                "session_file": str(session_file),
                "member_id": "member-claude",
                "member_token": "token-claude",
            }
        },
    }
    data = dict(REAL_CLAUDE_SESSION_FIELDS)
    data["ccb_project_id"] = project_id
    data["work_dir"] = str(work_dir)
    data["work_dir_norm"] = str(work_dir)
    data["claude_session_path"] = str(cfg / "claude-log.jsonl")
    session_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1793907200-555.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%121"}, {"CCB-Claude-eb24f3ae": "%121"}),
    )
    return work_dir, path, session_file


def test_a_launchless_binding_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The old shape (no launch id) is a negative case now: pane ids repeat
    across tmux servers, so project+pane+marker cannot prove a launch."""
    _, path, session_file = _real_claude_launch(tmp_path, monkeypatch)
    record = json.loads(path.read_text(encoding="utf-8"))
    data = json.loads(session_file.read_text(encoding="utf-8"))
    assert "session_id" not in data and "ccb_session_id" not in data

    ok, detail = set_own_role("member-claude", "token-claude", "lead")

    assert not ok and "no launch ID" in detail
    assert roles_for_record(record) == {}


def test_a_launchless_binding_is_refused_by_every_launch_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, path, session_file = _real_claude_launch(tmp_path, monkeypatch)
    record = json.loads(path.read_text(encoding="utf-8"))
    second = dict(record)
    second["ccb_session_id"] = "ai-1793907200-777"
    second["providers"] = {
        "claude": {**record["providers"]["claude"], "member_id": "member-second", "member_token": "token-second"}
    }
    second_path = tmp_path / ".ccb" / "run" / "ccb-session-ai-1793907200-777.json"
    second_path.write_text(json.dumps(second), encoding="utf-8")

    for member_id, token in (("member-claude", "token-claude"), ("member-second", "token-second")):
        ok, detail = set_own_role(member_id, token, "lead")
        assert not ok and "no launch ID" in detail
    assert not _roles_file(tmp_path).exists()


def _post_fix_claude_launch(tmp_path, monkeypatch):
    """The same launch after the producer fix: laskd's rewrite keeps ccb_session_id."""
    work_dir, path, session_file = _real_claude_launch(tmp_path, monkeypatch)
    record = json.loads(path.read_text(encoding="utf-8"))
    data = json.loads(session_file.read_text(encoding="utf-8"))
    data["ccb_session_id"] = record["ccb_session_id"]
    session_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return work_dir, path, session_file


def test_a_post_fix_claude_binding_can_claim_and_be_displayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, path, _ = _post_fix_claude_launch(tmp_path, monkeypatch)
    record = json.loads(path.read_text(encoding="utf-8"))

    ok, detail = set_own_role("member-claude", "token-claude", "lead")

    assert ok, detail
    assert roles_for_record(record) == {"member-claude": "lead"}


def test_a_post_fix_claude_binding_can_be_reclaimed_after_the_holder_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, path, _ = _post_fix_claude_launch(tmp_path, monkeypatch)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert set_own_role("member-claude", "token-claude", "implementer")[0]
    assert roles_for_record(record) == {"member-claude": "implementer"}

    # The pane dies; the claim stays on disk but must stop being held.
    monkeypatch.setattr(
        pane_registry, "get_backend_for_session", lambda _rec: _FakeBackend(set(), {})
    )
    assert roles_for_record(record) == {}

    # The same member reclaims the name on its new pane incarnation.
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%121"}, {"CCB-Claude-eb24f3ae": "%121"}),
    )
    ok, detail = set_own_role("member-claude", "token-claude", "implementer")
    assert ok, detail
    assert roles_for_record(record) == {"member-claude": "implementer"}


def test_an_explicit_foreign_launch_id_in_a_binding_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, path, session_file = _real_claude_launch(tmp_path, monkeypatch)
    record = json.loads(path.read_text(encoding="utf-8"))
    data = json.loads(session_file.read_text(encoding="utf-8"))
    data["session_id"] = "ai-some-other-launch"
    session_file.write_text(json.dumps(data), encoding="utf-8")

    ok, detail = set_own_role("member-claude", "token-claude", "lead")

    assert not ok and "another launch" in detail
    assert roles_for_record(record) == {}


def test_a_binding_with_two_different_launch_ids_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, path, session_file = _post_fix_claude_launch(tmp_path, monkeypatch)
    record = json.loads(path.read_text(encoding="utf-8"))
    data = json.loads(session_file.read_text(encoding="utf-8"))
    data["session_id"] = "ai-some-other-launch"
    session_file.write_text(json.dumps(data), encoding="utf-8")

    ok, detail = set_own_role("member-claude", "token-claude", "lead")

    assert not ok and "contradicts itself" in detail
    assert roles_for_record(record) == {}


def test_a_binding_for_another_terminal_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, path, session_file = _post_fix_claude_launch(tmp_path, monkeypatch)
    record = json.loads(path.read_text(encoding="utf-8"))
    data = json.loads(session_file.read_text(encoding="utf-8"))
    data["terminal"] = "wezterm"
    session_file.write_text(json.dumps(data), encoding="utf-8")

    ok, detail = set_own_role("member-claude", "token-claude", "lead")

    assert not ok and "another terminal" in detail
    assert roles_for_record(record) == {}


def test_laskd_rewrite_of_a_launcher_written_claude_binding_still_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real regression: a launcher-written file loses session_id when laskd
    rewrites it from the registry, and must still bind afterwards."""
    import laskd_session

    _, path, session_file = _real_claude_launch(tmp_path, monkeypatch)
    record = json.loads(path.read_text(encoding="utf-8"))
    # Start from a launcher-style binding that does carry the launch id.
    data = json.loads(session_file.read_text(encoding="utf-8"))
    data["session_id"] = record["ccb_session_id"]
    session_file.write_text(json.dumps(data), encoding="utf-8")

    session = laskd_session.load_project_session(Path(record["work_dir"]))
    assert session is not None
    session.update_claude_binding(session_path=Path(data["claude_session_path"]), session_id="101ec639-5c70-46d2-8e7d-75d6c38d9f1b")

    rewritten = json.loads(session_file.read_text(encoding="utf-8"))
    # laskd's registry projection replaces the file: no session_id, but the CCB
    # launch identity survives under ccb_session_id.
    assert "session_id" not in rewritten
    assert rewritten["ccb_session_id"] == record["ccb_session_id"]
    ok, detail = set_own_role("member-claude", "token-claude", "lead")
    assert ok, detail
    assert roles_for_record(record) == {"member-claude": "lead"}


# --- in-place respawn: the pane process witness -----------------------------------

class _CapableBackend(_FakeBackend):
    """A backend that can respawn a pane in place (tmux), with a mutable pid."""

    def __init__(self, alive, marker_map, pane_pids=None, *, raise_on_pid=False):
        super().__init__(alive, marker_map, pane_pids)
        self.raise_on_pid = raise_on_pid

    def respawn_pane(self, pane_id, cmd="", cwd=None, **_kwargs):
        return None

    def pane_pid(self, pane_id):
        if self.raise_on_pid:
            raise RuntimeError("pane_pid unavailable")
        return super().pane_pid(pane_id)


class _CapableBackendWithoutPidApi(_CapableBackend):
    """Capable of respawning, but it cannot name the pane's process at all."""

    pane_pid = property(lambda self: None, lambda self, _value: None)


def _claude_member(launch: NonPairLaunch):
    claude_id, claude_token = launch.members["claude"]
    entry = launch.record()["providers"]["claude"]
    return claude_id, claude_token, entry


def _capable(mode: str, pane: str, marker: str):
    if mode == "missing":
        return _CapableBackendWithoutPidApi({pane}, {marker: pane})
    if mode == "empty":
        return _CapableBackend({pane}, {marker: pane}, {pane: ""})
    return _CapableBackend({pane}, {marker: pane}, {pane: "1001"}, raise_on_pid=True)


@pytest.mark.parametrize("mode", ["missing", "empty", "raises"])
def test_an_in_place_respawn_backend_refuses_a_claim_without_a_pid(
    non_pair, tmp_path: Path, mode: str
) -> None:
    launch = non_pair(("claude",))
    claude_id, claude_token, entry = _claude_member(launch)
    pane_registry.get_backend_for_session = lambda _rec: _capable(
        mode, entry["pane_id"], entry["pane_title_marker"]
    )

    ok, detail = set_own_role(claude_id, claude_token, "lead")

    assert not ok and "process" in detail
    assert not _roles_file(tmp_path).exists()


def test_an_old_claim_without_a_witness_is_not_current_where_respawn_exists(
    non_pair,
) -> None:
    launch = non_pair(("claude",))
    claude_id, claude_token, entry = _claude_member(launch)
    pane = entry["pane_id"]
    marker = entry["pane_title_marker"]
    _write_claim_file(
        "ai-1793895837-1479705", {claude_id: {"role": "lead", "pane_id": pane, "provider": "claude"}}
    )
    pane_registry.get_backend_for_session = lambda _rec: _CapableBackend(
        {pane}, {marker: pane}, {pane: "1001"}
    )

    assert roles_for_record(launch.record()) == {}

    ok, detail = set_own_role(claude_id, claude_token, "implementer")
    assert ok, detail
    stored = live_roles._read_claims("ai-1793895837-1479705")[claude_id]
    assert stored["pane_pid"] == "1001"
    assert roles_for_record(launch.record()) == {claude_id: "implementer"}


def _write_claim_file(launch_id: str, claims: dict) -> Path:
    path = live_roles.roles_path_for_launch(launch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"launch_id": launch_id, "claims": claims}), encoding="utf-8")
    return path


def test_a_saved_pid_witness_must_be_confirmed(non_pair) -> None:
    launch = non_pair(("claude",))
    claude_id, claude_token, entry = _claude_member(launch)
    pane = entry["pane_id"]
    marker = entry["pane_title_marker"]
    pids = {"value": "1001"}

    class _Mutable(_CapableBackend):
        def pane_pid(self, pane_id):
            return pids["value"]

    pane_registry.get_backend_for_session = lambda _rec: _Mutable({pane}, {marker: pane})
    assert set_own_role(claude_id, claude_token, "lead")[0]
    assert roles_for_record(launch.record()) == {claude_id: "lead"}

    for broken in ("", None):
        pids["value"] = broken or ""
        assert roles_for_record(launch.record()) == {}

    pids["value"] = "1001"
    assert roles_for_record(launch.record()) == {claude_id: "lead"}


def test_a_pid_read_that_raises_is_not_a_confirmation(non_pair) -> None:
    launch = non_pair(("claude",))
    claude_id, claude_token, entry = _claude_member(launch)
    pane = entry["pane_id"]
    marker = entry["pane_title_marker"]
    state = {"raise": False}

    class _Flaky(_CapableBackend):
        def pane_pid(self, pane_id):
            if state["raise"]:
                raise RuntimeError("pane_pid unavailable")
            return "1001"

    pane_registry.get_backend_for_session = lambda _rec: _Flaky({pane}, {marker: pane})
    assert set_own_role(claude_id, claude_token, "lead")[0]
    state["raise"] = True

    assert roles_for_record(launch.record()) == {}


def test_a_claim_written_after_a_respawn_is_still_invalid(non_pair, monkeypatch) -> None:
    """The store must use the pid that authorized the claim, not a later read."""
    launch = non_pair(("claude",))
    claude_id, claude_token, entry = _claude_member(launch)
    pane = entry["pane_id"]
    marker = entry["pane_title_marker"]
    pids = {"value": "1001"}

    class _Mutable(_CapableBackend):
        def pane_pid(self, pane_id):
            return pids["value"]

    pane_registry.get_backend_for_session = lambda _rec: _Mutable({pane}, {marker: pane})

    # The pane respawns after the membership check authorised the claim, but
    # before the claim record is built and written.
    original_current = live_roles.roles_for_record

    def _current_then_respawn(record):
        pids["value"] = "2002"
        return original_current(record)

    monkeypatch.setattr(live_roles, "roles_for_record", _current_then_respawn)

    ok, detail = set_own_role(claude_id, claude_token, "lead")

    assert ok, detail
    stored = live_roles._read_claims("ai-1793895837-1479705")[claude_id]
    assert stored["pane_pid"] == "1001"
    assert roles_for_record(launch.record()) == {}


def test_a_backend_without_in_place_respawn_needs_no_pid(non_pair) -> None:
    launch = non_pair(("claude",))
    claude_id, claude_token, entry = _claude_member(launch)
    old_pane = entry["pane_id"]
    marker = entry["pane_title_marker"]
    pane_registry.get_backend_for_session = lambda _rec: _FakeBackend(
        {old_pane}, {marker: old_pane}
    )

    ok, detail = set_own_role(claude_id, claude_token, "lead")

    assert ok, detail
    assert live_roles._read_claims("ai-1793895837-1479705")[claude_id]["pane_pid"] == ""
    assert roles_for_record(launch.record()) == {claude_id: "lead"}

    # A changed pane id is what invalidates the claim there.
    new_pane = "%77"
    record = launch.record()
    record["providers"]["claude"]["pane_id"] = new_pane
    launch.record_path.write_text(json.dumps(record), encoding="utf-8")
    session_file = Path(record["providers"]["claude"]["session_file"])
    binding = json.loads(session_file.read_text(encoding="utf-8"))
    binding["pane_id"] = new_pane
    session_file.write_text(json.dumps(binding), encoding="utf-8")
    pane_registry.get_backend_for_session = lambda _rec: _FakeBackend(
        {new_pane}, {marker: new_pane}
    )

    assert roles_for_record(launch.record()) == {}


def test_an_existing_pair_claim_without_a_witness_needs_reclaiming(pair) -> None:
    """Roles written by an older release (no pane_pid) are stale on tmux."""
    _, path = pair
    record = _record(path)
    panes = {entry["live_id"]: entry["pane_id"] for entry in record["live_sessions"]}
    _write_claim_file(
        LAUNCH,
        {
            "cx1": {"role": "implementer", "pane_id": panes["cx1"], "provider": "codex"},
        },
    )
    pane_registry.get_backend_for_session = lambda _rec: _CapableBackend(
        set(panes.values()),
        {f"CCB-{k}": v for k, v in panes.items()},
        {pane: f"pid-{live_id}" for live_id, pane in panes.items()},
    )

    assert roles_for_record(record) == {}

    ok, detail = set_own_role("cx1", "tok-cx1", "implementer")
    assert ok, detail
    assert live_roles._read_claims(LAUNCH)["cx1"]["pane_pid"]
    assert roles_for_record(record) == {"cx1": "implementer"}
