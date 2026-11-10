"""M3 W2: other-machine discovery, its cache, and the agent-facing guidance.

The fixtures use the same generic names as the shipped guide (`server`, `demo`,
`alice`). Every cache entry is written under the test's isolated ``HOME``, so no
test reads or writes the machine's real ``~/.ccb/remote``.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import remote_client
import remote_discovery
import remote_protocol as proto
from test_remote_client import (  # noqa: F401 - fixture re-export
    HOST_ALIAS,
    ISSUER_TOKEN,
    TARGET,
    _ScriptedSession,
    _write_hosts,
    fake_ssh,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: An OLD receiver's list reply (shipped before M3): presence only.
OLD_RECEIVER_PROJECTS = [
    {
        "alias": "demo",
        "providers": [
            {"provider": "codex", "present": False},
            {"provider": "claude", "present": True},
        ],
    }
]

#: A NEW receiver's reply: availability plus the exact session choices.
NEW_RECEIVER_PROJECTS = [
    {
        "alias": "demo",
        "providers": [
            {
                "provider": "claude",
                "present": True,
                "state": "present",
                "reason": "",
                "sessions": [{"live_id": "live-claude-1", "active": True}],
            },
            {
                "provider": "codex",
                "present": False,
                "state": "ambiguous",
                "reason": "more than one live codex session",
                "sessions": [
                    {"live_id": "live-codex-1", "active": True},
                    {"live_id": "live-codex-2", "active": True},
                ],
            },
        ],
    }
]


def _hello(*, list_sessions: bool = True) -> dict:
    capabilities = {
        "verbs": ["hello", "list", "submit", "status", "fetch"],
        "reply_modes": {
            "claude": ["delivery-only", "explicit-reply"],
            "codex": ["delivery-only", "explicit-reply"],
        },
        "max_frame": 262144,
        "proto": {"major": 1, "minor": 1},
    }
    if list_sessions:
        capabilities["list_sessions"] = True
    return {"ok": True, "op": "hello", "op_id": "hello-1", "capabilities": capabilities}


def _list_reply(projects: list[dict]) -> dict:
    return {"ok": True, "op": "list", "op_id": "list-1", "projects": projects}


def _run_list(tmp_path, projects, *, list_sessions=True):
    _write_hosts(tmp_path)
    session = _ScriptedSession([_hello(list_sessions=list_sessions), _list_reply(projects)])
    out = io.StringIO()
    result = remote_client.list_projects(
        HOST_ALIAS, session_factory=lambda _argv: session, out=out, err=io.StringIO()
    )
    return result, out.getvalue()


def _load_ccb_list_module():
    path = REPO_ROOT / "bin" / "ccb-list"
    loader = importlib.machinery.SourceFileLoader("ccb_list_w2_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _cache_file(alias: str = HOST_ALIAS) -> Path:
    return Path.home() / ".ccb" / "remote" / "discovery" / f"{alias}.json"


def _hosts() -> dict:
    return remote_client.load_hosts()


# --------------------------------------------------------------------------- #
# Sender: an old receiver still works, a new one is understood
# --------------------------------------------------------------------------- #


def test_an_old_receiver_reply_renders_without_the_new_fields(tmp_path):
    result, rendered = _run_list(tmp_path, OLD_RECEIVER_PROJECTS, list_sessions=False)

    assert result.exit_code == remote_client.EXIT_ACCEPTED
    # Byte-for-byte the pre-M3 shape: no state, reason or sessions invented.
    assert result.projects == OLD_RECEIVER_PROJECTS
    assert "demo" in rendered and "claude=present" in rendered and "codex=absent" in rendered


def test_a_new_receiver_reply_carries_state_and_session_choices(tmp_path):
    result, rendered = _run_list(tmp_path, NEW_RECEIVER_PROJECTS)

    providers = result.projects[0]["providers"]
    by_name = {entry["provider"]: entry for entry in providers}
    assert by_name["claude"] == {
        "provider": "claude",
        "present": True,
        "state": "present",
        "reason": "",
        "sessions": [{"live_id": "live-claude-1", "active": True}],
    }
    assert by_name["codex"]["state"] == "ambiguous"
    assert [session["live_id"] for session in by_name["codex"]["sessions"]] == [
        "live-codex-1",
        "live-codex-2",
    ]
    # The human output of `ccb-remote list` keeps its old form.
    assert "claude=present" in rendered and "codex=absent" in rendered


def test_unknown_states_and_anonymous_sessions_are_dropped(tmp_path):
    projects = [
        {
            "alias": "demo",
            "providers": [
                {
                    "provider": "claude",
                    "present": True,
                    "state": "invented",
                    "reason": "  spaced   reason  ",
                    "sessions": [{"live_id": "", "active": True}, {"active": True}, "junk"],
                }
            ],
        }
    ]
    result, _ = _run_list(tmp_path, projects)

    entry = result.projects[0]["providers"][0]
    assert entry["state"] == ""
    assert entry["reason"] == "spaced reason"
    assert entry["sessions"] == []


# --------------------------------------------------------------------------- #
# Cache: written on success, kept on failure, invalidated by the connection
# --------------------------------------------------------------------------- #


def test_a_successful_list_writes_a_private_cache_entry(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "list_ok")

    result = remote_client.list_projects(HOST_ALIAS, out=io.StringIO(), err=io.StringIO())

    assert result.exit_code == remote_client.EXIT_ACCEPTED
    path = _cache_file()
    document = json.loads(path.read_text())
    connection = document["connection"]
    assert document["alias"] == HOST_ALIAS
    assert document["schema_version"] == remote_discovery.SCHEMA_VERSION
    assert connection["ssh_target"] == TARGET
    assert connection["user"] == "relay"
    assert connection["host"] == "example.test"
    assert connection["port"] == 2222
    assert connection["identity_file"] == str(tmp_path / "id_ed25519")
    assert connection["known_hosts"] == str(tmp_path / "known_hosts")
    # The whole pinned file, digested -- never the file's contents verbatim.
    assert len(connection["known_hosts_sha256"]) == 64
    # The issuer token is covered by a digest, and appears nowhere in clear.
    assert len(connection["issuer_token_sha256"]) == 64
    assert ISSUER_TOKEN not in json.dumps(document)
    assert document["projects"] == result.projects
    # The file carries the connection it was observed over; keep it private.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_a_failed_refresh_keeps_the_last_observation_and_records_why(
    tmp_path, fake_ssh, monkeypatch
):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "list_ok")
    remote_client.list_projects(HOST_ALIAS, out=io.StringIO(), err=io.StringIO())
    before = json.loads(_cache_file().read_text())

    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "list_refused")
    failed = remote_client.list_projects(HOST_ALIAS, out=io.StringIO(), err=io.StringIO())
    assert failed.exit_code == remote_client.EXIT_NOT_DELIVERED

    after = json.loads(_cache_file().read_text())
    assert after["projects"] == before["projects"]
    assert after["observed_at"] == before["observed_at"]
    failure = after["last_refresh_failure"]
    assert failure["at"] and "principal is not configured" in failure["reason"]

    view = remote_discovery.cache_view(HOST_ALIAS, host=_hosts()[HOST_ALIAS])
    assert view["status"] == "known"
    assert [project["alias"] for project in view["projects"]] == ["example-project"]
    assert view["last_refresh_failure"]["reason"].startswith("principal is not configured")


def test_a_changed_hosts_entry_makes_the_cache_unknown(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "list_ok")
    remote_client.list_projects(HOST_ALIAS, out=io.StringIO(), err=io.StringIO())

    # Same alias, different endpoint: the cached projects belong to the old one.
    _write_hosts(tmp_path, port=2200)
    host = _hosts()[HOST_ALIAS]

    assert remote_discovery.read_cache(HOST_ALIAS, host=host) is None
    view = remote_discovery.cache_view(HOST_ALIAS, host=host)
    assert view["status"] == "unknown"
    assert view["projects"] == []
    assert view["observed_at"] is None


def test_a_changed_pinned_host_key_makes_the_cache_unknown(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "list_ok")
    remote_client.list_projects(HOST_ALIAS, out=io.StringIO(), err=io.StringIO())
    assert remote_discovery.read_cache(HOST_ALIAS) is not None

    # A re-keyed host: same alias, same endpoint, different pinned key.
    (tmp_path / "known_hosts").write_text(
        "example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIother\n", encoding="utf-8"
    )
    host = _hosts()[HOST_ALIAS]

    assert remote_discovery.read_cache(HOST_ALIAS, host=host) is None
    assert remote_discovery.cache_view(HOST_ALIAS, host=host)["status"] == "unknown"


def test_a_host_that_was_never_checked_is_unknown():
    view = remote_discovery.cache_view(HOST_ALIAS, host=None)

    assert view == {
        "alias": HOST_ALIAS,
        "status": "unknown",
        "observed_at": None,
        "age_seconds": None,
        "projects": [],
        "last_refresh_failure": None,
    }


def test_the_age_is_measured_from_the_observation(tmp_path):
    host = _write_hosts(tmp_path) and _hosts()[HOST_ALIAS]
    observed = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    remote_discovery.write_success(host, OLD_RECEIVER_PROJECTS, now=observed)

    view = remote_discovery.cache_view(HOST_ALIAS, host=host, now=observed + timedelta(minutes=3))

    assert view["status"] == "known"
    assert view["observed_at"] == "2026-11-10T12:00:00+00:00"
    assert view["age_seconds"] == 180
    assert view["projects"] == OLD_RECEIVER_PROJECTS


def test_an_unreadable_cache_entry_is_ignored(tmp_path):
    path = _cache_file()
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    assert remote_discovery.read_cache(HOST_ALIAS) is None


# --------------------------------------------------------------------------- #
# ccb-list: next to unchanged local output
# --------------------------------------------------------------------------- #

LOCAL_ENTRIES = [
    {
        "index": 1,
        "work_dir": "/work/demo",
        "ccb_project_id": "abcdef0123456789",
        "providers": {},
        "sessions": [],
        "peer_providers": [],
        "peer_capable": False,
        "session_count": 0,
    }
]


@pytest.fixture
def ccb_list(monkeypatch):
    module = _load_ccb_list_module()
    monkeypatch.setattr(module, "_session_entries", lambda include_stale: list(LOCAL_ENTRIES))
    return module


def _seed_cache() -> None:
    host = _hosts()[HOST_ALIAS]
    remote_discovery.write_success(host, NEW_RECEIVER_PROJECTS)
    remote_discovery.record_failure(host, "ssh timed out")


def test_local_output_and_json_are_unchanged_with_a_hosts_config(
    tmp_path, ccb_list, capsys
):
    baseline = io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ccb_list, "_configured_hosts", lambda: {})
        ccb_list.main([])
        local_only = capsys.readouterr().out
        ccb_list.main(["--json"])
        json_only = capsys.readouterr().out

    _write_hosts(tmp_path)
    _seed_cache()
    ccb_list.main([])
    with_hosts = capsys.readouterr().out
    ccb_list.main(["--json"])
    json_with_hosts = capsys.readouterr().out

    assert json_only == json_with_hosts == json.dumps(LOCAL_ENTRIES, ensure_ascii=False, indent=2) + "\n"
    assert with_hosts.startswith(local_only)
    appended = with_hosts[len(local_only):]
    assert appended.startswith("Other machines (last observed, not live):")
    assert HOST_ALIAS in appended
    assert "last observed" in appended
    assert "      demo\n" in appended
    assert "claude  present (1 session)  live-id live-claude-1" in appended
    assert (
        "codex  ambiguous (2 sessions)  live-id live-codex-1, live-codex-2  "
        "reason: more than one live codex session"
    ) in appended
    assert "last refresh failed" in appended
    assert "ssh timed out" in appended


def test_remote_json_is_opt_in_and_shapes_the_cache(tmp_path, ccb_list, capsys):
    _write_hosts(tmp_path)
    _seed_cache()

    assert ccb_list.main(["--remote-json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["local"] == LOCAL_ENTRIES
    view = payload["remote"][HOST_ALIAS]
    assert view["status"] == "known"
    assert view["age_seconds"] is not None
    assert view["projects"] == NEW_RECEIVER_PROJECTS
    assert view["last_refresh_failure"]["reason"] == "ssh timed out"


def test_no_hosts_config_prints_no_remote_section(tmp_path, ccb_list, capsys):
    assert ccb_list.main([]) == 0
    out = capsys.readouterr().out

    assert "Active CCB Projects:" in out
    assert "Other machines" not in out


def test_a_hanging_refresh_is_bounded_and_keeps_the_local_output(
    tmp_path, ccb_list, capsys, monkeypatch
):
    _write_hosts(tmp_path)
    release = threading.Event()

    def hang(*_args, **_kwargs):
        release.wait(10)
        return remote_client.ListResult(HOST_ALIAS, remote_client.EXIT_ACCEPTED)

    monkeypatch.setenv(remote_discovery.REFRESH_TIMEOUT_ENV, "0.5")
    monkeypatch.setattr(remote_client, "list_projects", hang)
    try:
        started = time.monotonic()
        code = ccb_list.main(["--remote"])
        elapsed = time.monotonic() - started
    finally:
        release.set()

    out = capsys.readouterr().out
    assert code == 0
    assert elapsed < 4.0
    assert "Active CCB Projects:" in out
    assert "unknown (never checked)" in out
    assert "last refresh failed" in out and "timed out after 0.5s" in out


def test_a_failed_live_refresh_is_shown_not_raised(tmp_path, ccb_list, capsys, monkeypatch):
    _write_hosts(tmp_path)
    monkeypatch.setattr(
        remote_client,
        "list_projects",
        lambda *a, **kw: (_ for _ in ()).throw(remote_client.SessionError("timeout", "ssh timed out")),
    )

    assert ccb_list.main(["--remote"]) == 0
    out = capsys.readouterr().out

    assert "unknown (never checked)" in out
    assert "last refresh failed" in out and "ssh timed out" in out


def test_the_cache_still_reads_inside_the_codex_sandbox(capsys, monkeypatch, ccb_list):
    _write_hosts(Path(os.environ["HOME"]))
    _seed_cache()
    monkeypatch.setattr(ccb_list, "_inside_codex_network_sandbox", lambda: True)
    monkeypatch.setattr(ccb_list, "_daemon_session_entries", lambda include_stale: list(LOCAL_ENTRIES))

    assert ccb_list.main(["--remote-json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["local"] == LOCAL_ENTRIES
    assert payload["remote"][HOST_ALIAS]["status"] == "known"
    assert payload["remote"][HOST_ALIAS]["projects"] == NEW_RECEIVER_PROJECTS


# --------------------------------------------------------------------------- #
# P1: a session id is never shortened
# --------------------------------------------------------------------------- #

SHARED_PREFIX = "s" * 128
LONG_ID_ONE = f"{SHARED_PREFIX}-one"
LONG_ID_TWO = f"{SHARED_PREFIX}-two"


def _two_long_ids_reply() -> list[dict]:
    return [
        {
            "alias": "demo",
            "providers": [
                {
                    "provider": "claude",
                    "present": False,
                    "state": "ambiguous",
                    "reason": "more than one live claude session",
                    "sessions": [
                        {"live_id": LONG_ID_ONE, "active": True},
                        {"live_id": LONG_ID_TWO, "active": True},
                    ],
                }
            ],
        }
    ]


def test_ids_sharing_a_long_prefix_stay_distinct_on_the_sender(tmp_path):
    result, _ = _run_list(tmp_path, _two_long_ids_reply())

    ids = [entry["live_id"] for entry in result.projects[0]["providers"][0]["sessions"]]

    assert ids == [LONG_ID_ONE, LONG_ID_TWO]
    assert len(ids[0]) == len(LONG_ID_ONE)
    assert ids[0] != ids[1]


def test_an_invalid_session_id_is_dropped_never_shortened(tmp_path):
    oversized = "x" * (proto.MAX_SESSION_ID_LENGTH + 1)
    projects = [
        {
            "alias": "demo",
            "providers": [
                {
                    "provider": "claude",
                    "present": True,
                    "state": "present",
                    "reason": "",
                    "sessions": [
                        {"live_id": oversized, "active": True},
                        {"live_id": "live-ok", "active": True},
                    ],
                }
            ],
        }
    ]

    result, _ = _run_list(tmp_path, projects)

    ids = [entry["live_id"] for entry in result.projects[0]["providers"][0]["sessions"]]
    assert ids == ["live-ok"]


def test_a_long_id_is_shown_whole_in_the_human_section(tmp_path, ccb_list, capsys):
    _write_hosts(tmp_path)
    remote_discovery.write_success(_hosts()[HOST_ALIAS], _two_long_ids_reply())

    assert ccb_list.main([]) == 0
    out = capsys.readouterr().out

    # Display never trims an id: it is the value --live-id takes.
    assert f"live-id {LONG_ID_ONE}, {LONG_ID_TWO}" in out
    assert "reason: more than one live claude session" in out


# --------------------------------------------------------------------------- #
# P2: an expired refresh has no authority
# --------------------------------------------------------------------------- #


class _BlockedListSession:
    """Answers hello, then holds the list reply until released or closed."""

    def __init__(self, release: threading.Event):
        self.release = release
        self.closed = False
        self.writes = 0

    def write_all(self, payload, timeout_s=None) -> int:
        self.writes += 1
        return len(payload)

    def read_frame(self, timeout_s=None) -> dict:
        if self.writes <= 1:
            return _hello()
        self.release.wait(10)
        return _list_reply(NEW_RECEIVER_PROJECTS)

    def close(self) -> None:
        self.closed = True
        self.release.set()


def test_an_expired_refresh_cannot_overwrite_a_newer_observation(tmp_path):
    release = threading.Event()
    session = _BlockedListSession(release)
    _write_hosts(tmp_path)
    host = _hosts()[HOST_ALIAS]

    outcome = remote_discovery.refresh(
        host,
        timeout_s=0.2,
        session_factory=lambda _argv: session,
        out=io.StringIO(),
        err=io.StringIO(),
    )

    assert outcome["ok"] is False
    assert outcome["reason"] == "timed out after 0.2s"
    # The deadline closed the worker's own SSH process instead of leaking it.
    assert session.closed is True
    view = remote_discovery.cache_view(HOST_ALIAS, host=host)
    assert view["status"] == "unknown"
    assert view["last_refresh_failure"]["reason"] == "timed out after 0.2s"

    # A newer, successful observation lands while the old worker is still busy.
    newer = datetime(2026, 11, 10, 12, 30, 0, tzinfo=timezone.utc)
    remote_discovery.write_success(host, OLD_RECEIVER_PROJECTS, now=newer)

    # The abandoned worker now finishes with a DIFFERENT answer; it must not
    # commit anything, and the outcome already returned must not change.
    release.set()

    document = json.loads(_cache_file().read_text())
    assert document["observed_at"] == "2026-11-10T12:30:00+00:00"
    assert document["projects"] == OLD_RECEIVER_PROJECTS
    assert "last_refresh_failure" not in document
    assert outcome == {"alias": HOST_ALIAS, "ok": False, "reason": "timed out after 0.2s"}


def test_a_late_completion_cannot_flip_a_returned_failure(tmp_path):
    """The returned value is a snapshot: the worker cannot mutate it later."""
    release = threading.Event()
    session = _BlockedListSession(release)
    _write_hosts(tmp_path)
    host = _hosts()[HOST_ALIAS]

    outcome = remote_discovery.refresh(
        host,
        timeout_s=0.2,
        session_factory=lambda _argv: session,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    snapshot = dict(outcome)
    release.set()
    time.sleep(0.3)

    assert outcome == snapshot
    # The abandoned worker's own answer never reached the cache either.
    document = json.loads(_cache_file().read_text())
    assert document["observed_at"] is None
    assert document["projects"] == []


# --------------------------------------------------------------------------- #
# P2: identity covers the whole pinned file and the token
# --------------------------------------------------------------------------- #


def test_a_hashed_known_hosts_entry_change_invalidates_the_cache(
    tmp_path, fake_ssh, monkeypatch
):
    """A hashed entry cannot be attributed line by line -- so nothing is."""
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "list_ok")
    plain = "example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIplain"
    hashed = "|1|Zm9vYmFy=|YmF6cXV4 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIhashed"
    (tmp_path / "known_hosts").write_text(f"{plain}\n{hashed}\n", encoding="utf-8")
    remote_client.list_projects(HOST_ALIAS, out=io.StringIO(), err=io.StringIO())
    assert remote_discovery.read_cache(HOST_ALIAS, host=_hosts()[HOST_ALIAS]) is not None

    # ONLY the hashed line changes; the plain line for this host is untouched.
    rotated = "|1|Zm9vYmFy=|b3RoZXI ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIrotated"
    (tmp_path / "known_hosts").write_text(f"{plain}\n{rotated}\n", encoding="utf-8")

    assert remote_discovery.read_cache(HOST_ALIAS, host=_hosts()[HOST_ALIAS]) is None


def test_a_changed_issuer_token_makes_the_cache_unknown(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "list_ok")
    remote_client.list_projects(HOST_ALIAS, out=io.StringIO(), err=io.StringIO())
    assert remote_discovery.read_cache(HOST_ALIAS, host=_hosts()[HOST_ALIAS]) is not None

    _write_hosts(tmp_path, issuer_token="abc2")

    assert remote_discovery.read_cache(HOST_ALIAS, host=_hosts()[HOST_ALIAS]) is None
    view = remote_discovery.cache_view(HOST_ALIAS, host=_hosts()[HOST_ALIAS])
    assert view["status"] == "unknown" and view["projects"] == []


# --------------------------------------------------------------------------- #
# P2: one total deadline, local output first
# --------------------------------------------------------------------------- #


def _write_three_hosts(tmp_path: Path) -> dict:
    hosts = {}
    for alias in ("server-a", "server-b", "server-c"):
        identity = tmp_path / f"{alias}-id_ed25519"
        identity.write_text("private", encoding="utf-8")
        known = tmp_path / f"{alias}-known_hosts"
        known.write_text(f"{alias}.example.test ssh-ed25519 AAAA{alias}\n", encoding="utf-8")
        hosts[alias] = {
            "ssh_target": f"relay@{alias}.example.test",
            "port": 22,
            "identity_file": str(identity),
            "known_hosts": str(known),
            "issuer_token": "abc1",
        }
    path = Path.home() / ".ccb" / "remote" / "hosts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hosts": hosts}), encoding="utf-8")
    return hosts


def test_three_hanging_hosts_finish_inside_one_total_deadline(
    tmp_path, ccb_list, capsys, monkeypatch
):
    _write_three_hosts(tmp_path)
    release = threading.Event()

    def hang(*_args, **_kwargs):
        release.wait(10)
        return remote_client.ListResult(HOST_ALIAS, remote_client.EXIT_ACCEPTED)

    monkeypatch.setenv(remote_discovery.TOTAL_REFRESH_TIMEOUT_ENV, "1.0")
    monkeypatch.setattr(remote_client, "list_projects", hang)
    try:
        started = time.monotonic()
        code = ccb_list.main(["--remote"])
        elapsed = time.monotonic() - started
    finally:
        release.set()

    out = capsys.readouterr().out
    assert code == 0
    assert elapsed < 4.0
    # Local output first, and it is complete.
    assert out.index("Active CCB Projects:") < out.index("Other machines")
    assert "/work/demo" in out
    # The first host consumed the whole deadline; the rest were never reached.
    assert out.count("not refreshed (overall deadline)") == 2
    for alias in ("server-b", "server-c"):
        assert alias in out

def test_remote_json_reports_a_host_the_deadline_never_reached(
    tmp_path, ccb_list, capsys, monkeypatch
):
    _write_three_hosts(tmp_path)
    release = threading.Event()

    def hang(*_args, **_kwargs):
        release.wait(10)
        return remote_client.ListResult(HOST_ALIAS, remote_client.EXIT_ACCEPTED)

    monkeypatch.setenv(remote_discovery.TOTAL_REFRESH_TIMEOUT_ENV, "0.5")
    monkeypatch.setattr(remote_client, "list_projects", hang)
    try:
        assert ccb_list.main(["--remote", "--remote-json"]) == 0
    finally:
        release.set()

    payload = json.loads(capsys.readouterr().out)
    assert payload["local"] == LOCAL_ENTRIES
    skipped = [
        alias
        for alias, view in payload["remote"].items()
        if view.get("not_refreshed")
    ]
    assert skipped == ["server-b", "server-c"]
    assert all(
        payload["remote"][alias]["not_refreshed"] == "not refreshed (overall deadline)"
        for alias in skipped
    )


# --------------------------------------------------------------------------- #
# Skills: generic wording, no owner names
# --------------------------------------------------------------------------- #

SKILLS = [
    REPO_ROOT / "claude_skills" / "peer-ask" / "SKILL.md",
    REPO_ROOT / "codex_skills" / "peer-ask" / "SKILL.md",
]

REQUIRED_SKILL_TEXT = [
    "Another Machine",
    "<host>::<project>",
    "ccb-list --remote",
    "ccb-list --remote-json",
    "live-id",
    "case-insensitive",
    "exactly ONE match",
    "--live-id",
    "CCB_REMOTE_REPLY_COMMAND",
    "never guess",
    "--reply-to",
    "--foreground",
]


@pytest.mark.parametrize("skill", SKILLS, ids=lambda path: path.parent.name + "-" + path.parents[1].name)
def test_both_skill_variants_teach_another_machine(skill: Path):
    text = skill.read_text(encoding="utf-8").lower()

    missing = [needle for needle in REQUIRED_SKILL_TEXT if needle.lower() not in text]
    assert not missing, f"{skill}: missing {missing}"
    # The same-machine sections stay in place.
    assert "ccb_reply_target" in text


@pytest.mark.parametrize("skill", SKILLS, ids=lambda path: path.parent.name + "-" + path.parents[1].name)
def test_skill_text_carries_no_owner_specific_names(skill: Path):
    text = skill.read_text(encoding="utf-8").lower()

    # Generic markers of a real machine leaking into shipped text: home
    # directories and private LAN addresses. Examples must use placeholders.
    leaked = [marker for marker in ("/home/", "/users/", "192.168.", "10.0.0.") if marker in text]
    assert not leaked, f"{skill}: machine-specific text {leaked}"
