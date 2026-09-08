from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import ccb_runtime_status
import pane_registry
from ccb_runtime_status import (
    ProjectRuntimeStatus,
    ProviderRuntimeStatus,
    resolve_live_route,
    resolve_project_runtime_status,
)
from project_id import compute_ccb_project_id


class _FakeBackend:
    def __init__(self, alive: set[str], marker_map: dict[str, str] | None = None):
        self.alive = set(alive)
        self.marker_map = dict(marker_map or {})

    def is_alive(self, pane_id: str) -> bool:
        return pane_id in self.alive

    def find_pane_by_title_marker(self, marker: str, cwd_hint: str = "") -> str | None:
        return self.marker_map.get(marker)


def _write_config(work_dir: Path, providers: str = "codex,claude") -> None:
    cfg = work_dir / ".ccb"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "ccb.config").write_text(providers + "\n", encoding="utf-8")


def _write_session(work_dir: Path, filename: str, *, provider: str, pane_id: str, project_id: str) -> None:
    cfg = work_dir / ".ccb"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / filename).write_text(
        json.dumps(
            {
                "active": True,
                "provider": provider,
                "ccb_project_id": project_id,
                "work_dir": str(work_dir),
                "pane_id": pane_id,
            }
        ),
        encoding="utf-8",
    )


def _write_registry(home: Path, session_id: str, payload: dict) -> None:
    path = home / ".ccb" / "run" / f"ccb-session-{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def runtime_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    _write_config(work_dir)
    project_id = compute_ccb_project_id(work_dir)
    monkeypatch.setattr(ccb_runtime_status, "is_project_askd_online", lambda *_args, **_kwargs: True)
    return tmp_path, work_dir, project_id


def test_runtime_status_reports_mounted_provider(runtime_env, monkeypatch: pytest.MonkeyPatch) -> None:
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-session", provider="codex", pane_id="%2", project_id=project_id)
    _write_registry(
        home,
        "live",
        {
            "ccb_session_id": "live",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-test": "%2"}),
    )

    status = resolve_project_runtime_status(work_dir).providers["codex"]

    assert status.configured is True
    assert status.registered is True
    assert status.pane_alive is True
    assert status.session_bound is True
    assert status.daemon_online is True
    assert status.mounted is True
    assert status.reason == ""


def test_runtime_status_registered_but_pane_dead(runtime_env, monkeypatch: pytest.MonkeyPatch) -> None:
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-session", provider="codex", pane_id="%2", project_id=project_id)
    _write_registry(
        home,
        "dead",
        {
            "ccb_session_id": "dead",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
        },
    )
    monkeypatch.setattr(pane_registry, "get_backend_for_session", lambda _rec: _FakeBackend(set()))

    status = resolve_project_runtime_status(work_dir).providers["codex"]

    assert status.registered is True
    assert status.pane_alive is False
    assert status.mounted is False
    assert status.reason == "pane_dead"


def test_runtime_status_inactive_session_is_not_bound(runtime_env, monkeypatch: pytest.MonkeyPatch) -> None:
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-session", provider="codex", pane_id="%2", project_id=project_id)
    session_file = work_dir / ".ccb" / ".codex-session"
    data = json.loads(session_file.read_text(encoding="utf-8"))
    data["active"] = False
    session_file.write_text(json.dumps(data), encoding="utf-8")
    _write_registry(
        home,
        "inactive",
        {
            "ccb_session_id": "inactive",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-test": "%2"}),
    )

    status = resolve_project_runtime_status(work_dir).providers["codex"]

    assert status.pane_alive is True
    assert status.session_bound is False
    assert status.mounted is False
    assert status.reason == "session_unbound"


def test_runtime_status_configured_but_not_registered(runtime_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _home, work_dir, _project_id = runtime_env
    monkeypatch.setattr(pane_registry, "get_backend_for_session", lambda _rec: _FakeBackend(set()))

    status = resolve_project_runtime_status(work_dir).providers["codex"]

    assert status.configured is True
    assert status.registered is False
    assert status.mounted is False
    assert status.reason == "not_registered"


def test_runtime_status_daemon_offline(runtime_env, monkeypatch: pytest.MonkeyPatch) -> None:
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-session", provider="codex", pane_id="%2", project_id=project_id)
    _write_registry(
        home,
        "live",
        {
            "ccb_session_id": "live",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-test": "%2"}),
    )
    monkeypatch.setattr(ccb_runtime_status, "is_project_askd_online", lambda *_args, **_kwargs: False)

    status = resolve_project_runtime_status(work_dir).providers["codex"]

    assert status.pane_alive is True
    assert status.session_bound is True
    assert status.daemon_online is False
    assert status.mounted is False
    assert status.reason == "daemon_offline"


def test_project_askd_online_retries_valid_project_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    project_id = compute_ccb_project_id(work_dir)
    state_file = tmp_path / "askd.json"
    ping_results = iter([False, True])
    ping_timeouts: list[float] = []
    sleeps: list[float] = []

    monkeypatch.setattr(
        ccb_runtime_status,
        "state_file_candidates",
        lambda *_args, **_kwargs: [state_file],
    )
    monkeypatch.setattr(
        ccb_runtime_status.askd_rpc,
        "read_state",
        lambda _path: {"token": "token", "work_dir": str(work_dir)},
    )

    def _ping(_prefix, *, timeout_s, state_file):
        ping_timeouts.append(timeout_s)
        return next(ping_results)

    monkeypatch.setattr(ccb_runtime_status.askd_rpc, "ping_daemon", _ping)
    monkeypatch.setattr(ccb_runtime_status.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert ccb_runtime_status.is_project_askd_online(work_dir, project_id) is True
    assert ping_timeouts == [0.2, 0.3]
    assert sleeps == [0.05]


def test_resolve_daemon_work_dir_uses_reachable_state_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    subdir = project / "nested"
    run_dir = tmp_path / "run"
    subdir.mkdir(parents=True)
    run_dir.mkdir()
    state_file = run_dir / "askd.json"
    seen_work_dirs: list[Path] = []

    def _find(_name, *, protocol_prefix, work_dir, timeout_s):
        assert protocol_prefix == "ask"
        assert timeout_s == 0.5
        seen_work_dirs.append(Path(work_dir))
        return state_file

    monkeypatch.setenv("CCB_RUN_DIR", str(run_dir))
    monkeypatch.setattr(ccb_runtime_status, "find_running_state_file", _find)
    monkeypatch.setattr(
        ccb_runtime_status.askd_rpc,
        "read_state",
        lambda _path: {"token": "tok", "work_dir": str(project)},
    )

    assert ccb_runtime_status.resolve_daemon_work_dir(subdir) == project
    assert seen_work_dirs == [subdir]


def test_resolve_daemon_work_dir_without_managed_run_dir_keeps_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    subdir = project / "nested"
    subdir.mkdir(parents=True)

    monkeypatch.delenv("CCB_RUN_DIR", raising=False)
    monkeypatch.setattr(
        ccb_runtime_status,
        "find_running_state_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("daemon discovery used")),
    )

    assert ccb_runtime_status.resolve_daemon_work_dir(subdir) == subdir


def test_runtime_status_excludes_dead_launcher_unless_stale_requested(
    runtime_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-session", provider="codex", pane_id="%2", project_id=project_id)
    _write_registry(
        home,
        "orphan",
        {
            "ccb_session_id": "ai-123-99999999",
            "ccb_pid": 99999999,
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-test": "%2"}),
    )

    active = resolve_project_runtime_status(work_dir).providers["codex"]
    historical = resolve_project_runtime_status(work_dir, include_stale=True).providers["codex"]

    assert active.registered is False
    assert active.reason == "not_registered"
    assert historical.registered is True
    assert historical.pane_alive is False
    assert historical.mounted is False
    assert historical.reason == "launcher_dead"


def test_runtime_status_unknown_provider_is_not_exposed(runtime_env, monkeypatch: pytest.MonkeyPatch) -> None:
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-session", provider="codex", pane_id="%1", project_id=project_id)
    _write_registry(
        home,
        "base",
        {
            "ccb_session_id": "base",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%1", "pane_title_marker": "CCB-Codex-test"}},
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%1"}, {"CCB-Codex-test": "%1"}),
    )

    project = resolve_project_runtime_status(work_dir)

    assert project.providers["codex"].pane_alive is True
    assert set(project.providers) == {"claude", "codex"}


def test_runtime_status_delegates_from_managed_codex_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work_dir = tmp_path / "project"
    run_dir = tmp_path / "run"
    work_dir.mkdir()
    run_dir.mkdir()
    expected = ProjectRuntimeStatus(
        work_dir=str(work_dir),
        ccb_project_id="project-id",
        terminal="wezterm",
        updated_at=123,
        providers={
            "claude": ProviderRuntimeStatus(
                key="claude",
                provider="claude",
                capable=True,
                configured=True,
                registered=True,
                pane_alive=True,
                session_bound=True,
                daemon_online=True,
                mounted=True,
                reason="",
                pane_id="4",
            )
        },
    )
    captured: dict = {}
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setenv("CCB_MANAGED", "1")
    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setenv("CCB_RUN_DIR", str(run_dir))
    monkeypatch.setattr(ccb_runtime_status.askd_rpc, "read_state", lambda _path: {"token": "secret"})

    def _request(_state, request, **kwargs):
        captured.update(request=request, kwargs=kwargs)
        return {"type": "ask.response", "exit_code": 0, "project": expected.to_dict()}

    monkeypatch.setattr(ccb_runtime_status.askd_rpc, "request_daemon", _request)
    monkeypatch.setattr(
        ccb_runtime_status,
        "iter_registry_provider_records",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("sandbox performed direct terminal discovery")),
    )

    project = resolve_project_runtime_status(work_dir)

    assert project == expected
    assert captured["request"]["operation"] == "runtime_status"
    assert captured["request"]["work_dir"] == str(work_dir)
    assert captured["kwargs"]["response_timeout_s"] == 8.0


def test_managed_codex_sandbox_detection_does_not_require_run_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setenv("CCB_MANAGED", "1")
    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.delenv("CCB_RUN_DIR", raising=False)

    assert ccb_runtime_status.inside_managed_codex_sandbox() is True


def test_runtime_status_reports_ambiguous_for_two_sessions_of_one_provider(
    runtime_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2"},
                {"live_id": "s2", "provider": "codex", "pane_id": "%3"},
            ],
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2", "%3"}, {"CCB-Codex-test": "%2"}),
    )

    status = resolve_project_runtime_status(work_dir).providers["codex"]

    assert status.ambiguous is True
    assert sorted(status.candidates) == ["s1", "s2"]
    assert status.mounted is False
    assert status.reason == "ambiguous_sessions"
    assert status.registered is True


def test_runtime_status_single_session_output_unchanged_by_ambiguity_check(
    runtime_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-session", provider="codex", pane_id="%2", project_id=project_id)
    _write_registry(
        home,
        "live",
        {
            "ccb_session_id": "live",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-test": "%2"}),
    )

    status = resolve_project_runtime_status(work_dir).providers["codex"]

    assert status.configured is True
    assert status.registered is True
    assert status.pane_alive is True
    assert status.session_bound is True
    assert status.daemon_online is True
    assert status.mounted is True
    assert status.reason == ""
    assert status.ambiguous is False
    assert status.candidates == ()


def test_runtime_status_invalid_inventory_never_reports_mounted(
    runtime_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-session", provider="codex", pane_id="%2", project_id=project_id)
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            # A healthy legacy entry that a fallback would happily mount.
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
            "live_sessions": "nope",
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-test": "%2"}),
    )

    status = resolve_project_runtime_status(work_dir).providers["codex"]

    assert status.mounted is False
    assert status.reason == "invalid_inventory"
    assert status.ambiguous is False
    assert status.registered is True


def test_runtime_status_valid_single_session_uses_inventory_pane_not_legacy_pane(
    runtime_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            # Legacy pane %2 is alive in the fake backend below; the
            # inventory names a different pane, %9, which is not.
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-legacy"}},
            "live_sessions": [
                {
                    "live_id": "s1",
                    "provider": "codex",
                    "pane_id": "%9",
                    "pane_title_marker": "CCB-Codex-inventory",
                },
            ],
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-legacy": "%2"}),
    )

    status = resolve_project_runtime_status(work_dir).providers["codex"]

    # If the legacy pane were still consulted, pane_alive would be True
    # (it's the one the fake backend reports alive). It isn't: the
    # inventory's own (dead, per this backend) pane governs instead.
    assert status.pane_id == "%9"
    assert status.pane_alive is False
    assert status.mounted is False


def test_runtime_status_provider_present_only_in_inventory_still_appears(
    runtime_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "pane_title_marker": "CCB-Codex-test"},
                # opencode never appears in `providers` at all.
                {"live_id": "s2", "provider": "opencode", "pane_id": "%5", "pane_title_marker": "CCB-Opencode-test"},
            ],
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2", "%5"}, {"CCB-Codex-test": "%2", "CCB-Opencode-test": "%5"}),
    )

    project = resolve_project_runtime_status(work_dir)

    assert "opencode" in project.providers
    status = project.providers["opencode"]
    assert status.registered is True
    assert status.pane_id == "%5"
    assert status.pane_alive is True


def test_iter_registry_provider_records_returns_legacy_records(
    runtime_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Isolated coverage of iter_registry_provider_records itself (bin/ask
    # consumes it directly), independent of resolve_project_runtime_status —
    # locks in that last round's refactor onto a shared generator didn't
    # change what it returns for a plain legacy record.
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
        },
    )

    records = ccb_runtime_status.iter_registry_provider_records(project_id=project_id)

    assert len(records) == 1
    record = records[0]
    assert record.provider == "codex"
    assert record.project_id == project_id
    assert record.work_dir == str(work_dir)
    assert record.provider_entry.get("pane_id") == "%2"


def test_runtime_status_inactive_sole_session_never_reports_mounted(
    runtime_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-session", provider="codex", pane_id="%2", project_id=project_id)
    session_file = str(work_dir / ".ccb" / ".codex-session")
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
            "live_sessions": [
                {
                    "live_id": "s1",
                    "provider": "codex",
                    "pane_id": "%2",
                    "pane_title_marker": "CCB-Codex-test",
                    "session_file": session_file,
                    "active": False,
                },
            ],
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-test": "%2"}),
    )

    status = resolve_project_runtime_status(work_dir).providers["codex"]

    # Pane, binding and daemon are all otherwise healthy — only the
    # session's own recorded inactivity should stop it from mounting.
    assert status.pane_alive is True
    assert status.session_bound is True
    assert status.daemon_online is True
    assert status.mounted is False
    assert status.reason == "session_inactive"


def test_runtime_status_ambiguity_still_counts_inactive_members(
    runtime_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Item 1 only changes the single-session outcome; an inactive member
    # must still count toward ambiguity detection exactly as before.
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
                {"live_id": "s2", "provider": "codex", "pane_id": "%3", "active": False},
            ],
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-test": "%2"}),
    )

    status = resolve_project_runtime_status(work_dir).providers["codex"]

    assert status.ambiguous is True
    assert sorted(status.candidates) == ["s1", "s2"]


def test_runtime_status_uses_sessions_own_file_over_provider_default(
    runtime_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, work_dir, project_id = runtime_env
    # No provider-default `.codex-session` file exists at all: if binding
    # ever fell back to it for this inventory session, it would fail.
    own_file = work_dir / ".ccb" / "codex-session-s1.json"
    own_file.parent.mkdir(parents=True, exist_ok=True)
    own_file.write_text(
        json.dumps(
            {
                "active": True,
                "provider": "codex",
                "ccb_project_id": project_id,
                "work_dir": str(work_dir),
                "pane_id": "%2",
            }
        ),
        encoding="utf-8",
    )
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
            "live_sessions": [
                {
                    "live_id": "s1",
                    "provider": "codex",
                    "pane_id": "%2",
                    "pane_title_marker": "CCB-Codex-test",
                    "session_file": str(own_file),
                },
            ],
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-test": "%2"}),
    )

    status = resolve_project_runtime_status(work_dir).providers["codex"]

    assert status.session_bound is True
    assert status.session_file == str(own_file)
    assert status.mounted is True


def test_runtime_status_session_without_file_reference_reports_unbound(
    runtime_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, work_dir, project_id = runtime_env
    # A HEALTHY provider-default file exists — if binding ever fell back to
    # it for an inventory session with no file of its own, this would
    # incorrectly report bound.
    _write_session(work_dir, ".codex-session", provider="codex", pane_id="%2", project_id=project_id)
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "pane_title_marker": "CCB-Codex-test"},
            ],
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-test": "%2"}),
    )

    status = resolve_project_runtime_status(work_dir).providers["codex"]

    assert status.session_bound is False
    assert status.mounted is False


# --- resolve_live_route -----------------------------------------------------
# These exercise the client-side route-resolution entry point `bin/ask`'s
# `_preflight_target` calls: it must find the SAME record
# `resolve_project_runtime_status` would, and only produce a route when that
# record actually carries a `live_sessions` inventory.


def test_resolve_live_route_picks_sibling_for_identified_caller(
    runtime_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Finding 6: caller-aware resolution supersedes the provider-wide
    # AMBIGUITY verdict only -- the destination it picks must still pass
    # the same operational checks (pane liveness, session binding) a
    # non-routed status check would run, hence the backend/session-file
    # setup below.
    home, work_dir, project_id = runtime_env
    sibling_session_file = work_dir / ".ccb" / "codex-session-s2.json"
    sibling_session_file.parent.mkdir(parents=True, exist_ok=True)
    sibling_session_file.write_text(
        json.dumps({"active": True, "provider": "codex", "ccb_project_id": project_id, "pane_id": "%3"}),
        encoding="utf-8",
    )
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "terminal": "tmux"},
                {
                    "live_id": "s2",
                    "provider": "codex",
                    "pane_id": "%3",
                    "terminal": "tmux",
                    "pane_title_marker": "CCB-Codex-s2",
                    "session_file": str(sibling_session_file),
                },
            ],
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2", "%3"}, {"CCB-Codex-s2": "%3"}),
    )

    outcome = resolve_live_route("codex", work_dir, caller_pane_id="%2", caller_terminal="tmux")

    assert outcome is not None
    resolution, caller = outcome
    assert resolution.ok is True, resolution
    assert resolution.session.live_id == "s2"
    assert resolution.session.launch_id == "ai-1"
    assert caller is not None and caller.live_id == "s1"


def test_resolve_live_route_refuses_unknown_caller_among_two_sessions(
    runtime_env,
) -> None:
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2"},
                {"live_id": "s2", "provider": "codex", "pane_id": "%3"},
            ],
        },
    )

    outcome = resolve_live_route("codex", work_dir)

    assert outcome is not None
    resolution, caller = outcome
    assert resolution.ok is False
    assert caller is None


def test_resolve_live_route_returns_none_when_record_has_no_inventory(
    runtime_env,
) -> None:
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
        },
    )

    assert resolve_live_route("codex", work_dir) is None


def test_resolve_live_route_refuses_on_invalid_inventory(runtime_env) -> None:
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": "nope",
        },
    )

    outcome = resolve_live_route("codex", work_dir)

    assert outcome is not None
    resolution, caller = outcome
    assert resolution.ok is False
    assert resolution.error == "invalid_inventory"


# --- pane_registry.validate_route / resolve_live_session_by_id -------------
# The "confirm, never re-select" checkpoint Task 3 requires at both enqueue
# and send. These must establish identity (does this live_id still exist in
# this exact launch record?) before ever looking at availability.


def test_validate_route_accepts_still_active_destination(runtime_env) -> None:
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
            ],
        },
    )

    outcome = pane_registry.validate_route(live_id="s1", launch_id="ai-1", provider="codex")

    assert outcome.ok is True
    assert outcome.session.live_id == "s1"


def test_validate_route_refuses_when_destination_is_gone(runtime_env) -> None:
    # No registry record at all for this launch_id: the record was cleaned
    # up, replaced, or never existed under this id.
    outcome = pane_registry.validate_route(live_id="s1", launch_id="ai-missing", provider="codex")

    assert outcome.ok is False
    assert outcome.session is None


def test_validate_route_refuses_when_destination_now_inactive(runtime_env) -> None:
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": False},
            ],
        },
    )

    outcome = pane_registry.validate_route(live_id="s1", launch_id="ai-1", provider="codex")

    assert outcome.ok is False


def test_validate_route_never_redirects_to_sibling_when_id_vanishes(runtime_env) -> None:
    # The routed id is gone, but a DIFFERENT live session of the same
    # provider is still present in the record. This must still refuse --
    # never quietly resolve to the sibling instead.
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%3"}},
            "live_sessions": [
                {"live_id": "s2", "provider": "codex", "pane_id": "%3", "active": True},
            ],
        },
    )

    outcome = pane_registry.validate_route(live_id="s1", launch_id="ai-1", provider="codex")

    assert outcome.ok is False
    assert outcome.session is None


def test_session_data_from_live_reads_its_own_file_never_a_default(tmp_path: Path) -> None:
    own_file = tmp_path / "codex-session-s1.json"
    own_file.write_text(
        json.dumps({"codex_session_path": "/tmp/own.jsonl", "codex_session_id": "own-id"}),
        encoding="utf-8",
    )
    session = pane_registry.LiveSession(
        live_id="s1",
        provider="codex",
        launch_id="ai-1",
        pane_id="%2",
        terminal="tmux",
        work_dir=str(tmp_path),
        session_file=str(own_file),
    )

    data = pane_registry.session_data_from_live(session)

    assert data is not None
    assert data["codex_session_path"] == "/tmp/own.jsonl"
    assert data["codex_session_id"] == "own-id"
    assert data["pane_id"] == "%2"


def test_session_data_from_live_refuses_when_no_file_of_its_own(tmp_path: Path) -> None:
    session = pane_registry.LiveSession(
        live_id="s1",
        provider="codex",
        launch_id="ai-1",
        pane_id="%2",
        terminal="tmux",
        work_dir=str(tmp_path),
        session_file="",
    )

    assert pane_registry.session_data_from_live(session) is None


def test_session_data_from_live_refuses_when_file_scope_contradicts_route(tmp_path: Path) -> None:
    # Finding 4: a session file whose OWN recorded pane_id disagrees with
    # the inventory entry it is named from must be rejected outright, not
    # merged over -- this is validation, never a silent overwrite.
    own_file = tmp_path / "codex-session-s1.json"
    own_file.write_text(
        json.dumps({"pane_id": "%99", "codex_session_path": "/tmp/own.jsonl"}),
        encoding="utf-8",
    )
    session = pane_registry.LiveSession(
        live_id="s1",
        provider="codex",
        launch_id="ai-1",
        pane_id="%2",  # disagrees with the file's own "%99"
        terminal="tmux",
        work_dir=str(tmp_path),
        session_file=str(own_file),
    )

    assert pane_registry.session_data_from_live(session) is None


def test_session_data_from_live_refuses_when_file_work_dir_contradicts_route(tmp_path: Path) -> None:
    own_file = tmp_path / "codex-session-s1.json"
    other_dir = tmp_path / "other-project"
    own_file.write_text(json.dumps({"pane_id": "%2", "work_dir": str(other_dir)}), encoding="utf-8")
    session = pane_registry.LiveSession(
        live_id="s1",
        provider="codex",
        launch_id="ai-1",
        pane_id="%2",
        terminal="tmux",
        work_dir=str(tmp_path),  # disagrees with the file's own recorded work_dir
        session_file=str(own_file),
    )

    assert pane_registry.session_data_from_live(session) is None


def test_validate_route_refuses_when_pane_was_replaced_under_the_same_id(runtime_env) -> None:
    # Finding 4: a bare live_id match is not enough to detect replacement.
    # The route's own endpoint evidence (the pane it was resolved against)
    # must be compared to a FRESH read, and a mismatch refuses.
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%9"}},
            "live_sessions": [
                # Same live_id as originally resolved, but now a DIFFERENT
                # pane -- the session was replaced underneath the id.
                {"live_id": "s1", "provider": "codex", "pane_id": "%99", "active": True},
            ],
        },
    )

    outcome = pane_registry.validate_route(
        live_id="s1", launch_id="ai-1", provider="codex", pane_id="%2",
    )

    assert outcome.ok is False
    assert outcome.session is None


def test_validate_route_refuses_when_session_file_was_replaced_under_the_same_id(runtime_env) -> None:
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {
                    "live_id": "s1",
                    "provider": "codex",
                    "pane_id": "%2",
                    "session_file": str(work_dir / ".ccb" / "codex-session-REPLACED.json"),
                    "active": True,
                },
            ],
        },
    )

    outcome = pane_registry.validate_route(
        live_id="s1",
        launch_id="ai-1",
        provider="codex",
        pane_id="%2",
        session_file=str(work_dir / ".ccb" / "codex-session-ORIGINAL.json"),
    )

    assert outcome.ok is False


def test_validate_route_refuses_forged_self_route(runtime_env) -> None:
    # Finding 2: a forged caller_live_id inside the route payload must
    # never be trusted for the self-route check. The ACTUAL caller is
    # re-derived from caller_pane_id/caller_terminal (the request's own
    # terminal-environment evidence) against the fresh launch record, and
    # a route whose destination turns out to BE that caller is refused --
    # even though identity, evidence and provider all otherwise check out.
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
                {"live_id": "s2", "provider": "codex", "pane_id": "%3", "active": True},
            ],
        },
    )

    # The route CLAIMS to target s2 (a legitimate-looking sibling), but the
    # request is actually being submitted FROM pane %2 -- which the
    # inventory says is s1, not s2. A forged route could try to route
    # someone to themselves by mis-describing who the destination is
    # relative to who is actually asking; here we directly prove the
    # self-route case: the caller's real pane matches the DESTINATION's
    # own live_id.
    outcome = pane_registry.validate_route(
        live_id="s1",
        launch_id="ai-1",
        provider="codex",
        caller_pane_id="%2",
        caller_terminal="tmux",
    )

    assert outcome.ok is False
    assert outcome.error == pane_registry.SELF_ONLY


def test_resolve_live_route_rejects_forged_caller_live_id_contradicting_pane(runtime_env) -> None:
    # A caller_live_id that contradicts the supplied pane/terminal evidence
    # must never be trusted -- the caller is then unidentified, not
    # "identified as whatever the forged id claims."
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
                {"live_id": "s2", "provider": "codex", "pane_id": "%3", "active": True},
            ],
        },
    )

    # Really at pane %2 (== s1), but forges caller_live_id="s2".
    outcome = resolve_live_route(
        "codex", work_dir, caller_pane_id="%2", caller_terminal="tmux", caller_live_id="s2",
    )

    assert outcome is not None
    resolution, caller = outcome
    assert resolution.ok is False
    assert caller is None


def test_resolve_live_route_uses_verified_live_credential_when_sandbox_strips_pane(
    runtime_env, monkeypatch
) -> None:
    home, work_dir, project_id = runtime_env
    _write_registry(home, "ai-1", {
        "ccb_session_id": "ai-1", "ccb_project_id": project_id,
        "work_dir": str(work_dir), "terminal": "tmux", "updated_at": int(time.time()),
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "%2", "auth_token": "token-1"},
            {"live_id": "s2", "provider": "codex", "pane_id": "%3", "auth_token": "token-2"},
        ],
    })
    monkeypatch.setattr(ccb_runtime_status, "_operational_refusal", lambda *_a, **_k: None)
    outcome = resolve_live_route(
        "codex", work_dir, caller_live_id="s1", caller_token="token-1",
        check_daemon=False, _allow_daemon_proxy=False,
    )
    assert outcome is not None
    resolution, caller = outcome
    assert resolution.session.live_id == "s2"
    assert caller.live_id == "s1"
    forged = resolve_live_route(
        "codex", work_dir, caller_live_id="s1", caller_token="wrong",
        check_daemon=False, _allow_daemon_proxy=False,
    )
    assert forged is not None
    assert forged[0].error == ccb_runtime_status.UNKNOWN_CALLER


def test_resolve_live_route_refuses_two_competing_launches_without_caller_evidence(runtime_env) -> None:
    # Finding 3: two unrelated CCB launches in the same project, both
    # carrying a codex inventory. Without caller evidence to place the
    # request in ONE of them, this must refuse -- never guess a
    # project-wide "the" record.
    home, work_dir, project_id = runtime_env
    for session_id, pane in (("ai-1", "%2"), ("ai-2", "%20")):
        _write_registry(
            home,
            session_id,
            {
                "ccb_session_id": session_id,
                "ccb_project_id": project_id,
                "work_dir": str(work_dir),
                "terminal": "tmux",
                "updated_at": int(time.time()),
                "providers": {"codex": {"pane_id": pane}},
                "live_sessions": [
                    {"live_id": f"{session_id}-s1", "provider": "codex", "pane_id": pane, "active": True},
                ],
            },
        )

    outcome = resolve_live_route("codex", work_dir)

    assert outcome is not None
    resolution, caller = outcome
    assert resolution.ok is False
    assert caller is None


def test_resolve_live_route_uses_correct_launch_when_caller_identifies_it(runtime_env, monkeypatch) -> None:
    # The SAME two-launch setup as above, but this time the caller's own
    # pane places them unambiguously in "ai-2" -- resolution must proceed
    # strictly within that launch and never even consider "ai-1"'s.
    home, work_dir, project_id = runtime_env
    session_file_2 = work_dir / ".ccb" / "codex-session-ai2.json"
    session_file_2.parent.mkdir(parents=True, exist_ok=True)
    session_file_2.write_text(
        json.dumps({"active": True, "provider": "codex", "ccb_project_id": project_id, "pane_id": "%21"}),
        encoding="utf-8",
    )
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "shared", "provider": "codex", "pane_id": "%2", "active": True},
            ],
        },
    )
    _write_registry(
        home,
        "ai-2",
        {
            "ccb_session_id": "ai-2",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%20"}},
            "live_sessions": [
                {"live_id": "caller", "provider": "codex", "pane_id": "%20", "active": True},
                {
                    "live_id": "shared",
                    "provider": "codex",
                    "pane_id": "%21",
                    "pane_title_marker": "CCB-Codex-ai2",
                    "session_file": str(session_file_2),
                    "active": True,
                },
            ],
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2", "%20", "%21"}, {"CCB-Codex-ai2": "%21"}),
    )

    outcome = resolve_live_route("codex", work_dir, caller_pane_id="%20", caller_terminal="tmux")

    assert outcome is not None
    resolution, caller = outcome
    assert resolution.ok is True, resolution
    assert resolution.session.launch_id == "ai-2"
    assert resolution.session.pane_id == "%21"
    assert caller is not None and caller.live_id == "caller"


def test_resolve_live_route_refuses_after_identity_succeeds_but_pane_is_dead(
    runtime_env, monkeypatch
) -> None:
    # Finding 6: caller-aware resolution supersedes the AMBIGUITY verdict
    # only. Identity here resolves cleanly (single session, no caller
    # needed) -- but the destination's pane is not actually alive, and
    # that operational failure must still refuse, exactly as a non-routed
    # status check would.
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
            ],
        },
    )
    # No pane in the fake backend's alive set: pane_dead.
    monkeypatch.setattr(pane_registry, "get_backend_for_session", lambda _rec: _FakeBackend(set()))

    outcome = resolve_live_route("codex", work_dir)

    assert outcome is not None
    resolution, caller = outcome
    assert resolution.ok is False
    assert resolution.error == "pane_dead"


def test_resolve_live_route_proxies_through_daemon_rpc_inside_managed_sandbox(monkeypatch) -> None:
    # Finding 1: a sandboxed caller must get a REAL, host-computed answer
    # via the authenticated daemon RPC -- never a local bypass. Proven two
    # ways: (a) direct registry access is never attempted from here (it
    # would raise if it were), and (b) the RPC round-trip's response is
    # what determines the outcome.
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setenv("CCB_MANAGED", "1")
    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setattr(
        ccb_runtime_status,
        "_iter_qualifying_registry_records",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("sandbox bypassed to direct registry access")),
    )
    monkeypatch.setattr(ccb_runtime_status.askd_rpc, "read_state", lambda _path: {"token": "secret"})

    captured: dict = {}

    def _fake_request(_state, request, **kwargs):
        captured.update(request=request, kwargs=kwargs)
        return {
            "type": "ask.response",
            "exit_code": 0,
            "route_outcome": {
                "kind": "route",
                "live_id": "s2",
                "launch_id": "ai-1",
                "provider": "codex",
                "pane_id": "%3",
                "terminal": "tmux",
                "work_dir": "/tmp/proj",
                "ccb_project_id": "proj-1",
                "session_file": "/tmp/s2.json",
                "active": True,
                "caller_live_id": "s1",
            },
        }

    monkeypatch.setattr(ccb_runtime_status.askd_rpc, "request_daemon", _fake_request)

    outcome = resolve_live_route(
        "codex", "/tmp/proj", caller_pane_id="%2", caller_terminal="tmux",
    )

    assert outcome is not None
    resolution, caller = outcome
    assert resolution.ok is True
    assert resolution.session.live_id == "s2"
    assert resolution.session.launch_id == "ai-1"
    assert caller is not None and caller.live_id == "s1"
    assert captured["request"]["operation"] == "resolve_route"
    assert captured["request"]["provider"] == "codex"
    assert captured["request"]["caller_pane_id"] == "%2"


def test_resolve_live_route_sandbox_proxy_reports_explicit_refusal(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setenv("CCB_MANAGED", "1")
    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setattr(ccb_runtime_status.askd_rpc, "read_state", lambda _path: {"token": "secret"})
    monkeypatch.setattr(
        ccb_runtime_status.askd_rpc,
        "request_daemon",
        lambda _state, _request, **_kwargs: {
            "type": "ask.response",
            "exit_code": 0,
            "route_outcome": {"kind": "refused", "reason": "unknown_caller", "detail": "no verified caller", "candidates": []},
        },
    )

    outcome = resolve_live_route("codex", "/tmp/proj", caller_pane_id="%2", caller_terminal="tmux")

    assert outcome is not None
    resolution, caller = outcome
    assert resolution.ok is False
    assert resolution.error == "unknown_caller"
    assert caller is None


def test_resolve_live_route_sandbox_proxy_reports_no_inventory(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setenv("CCB_MANAGED", "1")
    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setattr(ccb_runtime_status.askd_rpc, "read_state", lambda _path: {"token": "secret"})
    monkeypatch.setattr(
        ccb_runtime_status.askd_rpc,
        "request_daemon",
        lambda _state, _request, **_kwargs: {
            "type": "ask.response",
            "exit_code": 0,
            "route_outcome": {"kind": "no_inventory"},
        },
    )

    assert resolve_live_route("codex", "/tmp/proj", caller_pane_id="%2", caller_terminal="tmux") is None


# --- Hole 2: caller re-validation must fail CLOSED, not open ---------------


def test_validate_route_refuses_when_caller_evidence_matches_nothing(runtime_env) -> None:
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
                {"live_id": "s2", "provider": "codex", "pane_id": "%3", "active": True},
            ],
        },
    )

    # Pane %999 belongs to no session in this launch at all.
    outcome = pane_registry.validate_route(
        live_id="s2", launch_id="ai-1", provider="codex",
        caller_pane_id="%999", caller_terminal="tmux",
    )

    assert outcome.ok is False
    assert outcome.error == pane_registry.UNKNOWN_CALLER


def test_validate_route_refuses_when_caller_contradicts_routes_saved_caller(runtime_env) -> None:
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
                {"live_id": "s2", "provider": "codex", "pane_id": "%3", "active": True},
                {"live_id": "s3", "provider": "codex", "pane_id": "%4", "active": True},
            ],
        },
    )

    # The request is genuinely being submitted from pane %2 (== s1), but
    # the route it is carrying claims its caller was "s3" -- a mismatch
    # that must refuse rather than either trusting the route's claim or
    # silently ignoring it.
    outcome = pane_registry.validate_route(
        live_id="s2", launch_id="ai-1", provider="codex",
        caller_pane_id="%2", caller_terminal="tmux", caller_live_id="s3",
    )

    assert outcome.ok is False
    assert outcome.error == pane_registry.UNKNOWN_CALLER


def test_validate_route_refuses_when_launch_record_vanishes_between_reads(runtime_env, monkeypatch) -> None:
    # Simulates the launch record disappearing between an earlier resolve
    # and this validation checkpoint: load_registry_by_session_id must be
    # read ONCE and, finding nothing, refuse outright rather than treating
    # a vanished record as any kind of pass.
    monkeypatch.setattr(pane_registry, "load_registry_by_session_id", lambda _session_id: None)

    outcome = pane_registry.validate_route(
        live_id="s2", launch_id="ai-1", provider="codex",
        caller_pane_id="%2", caller_terminal="tmux",
    )

    assert outcome.ok is False
    assert outcome.session is None


def test_validate_route_refuses_when_inventory_becomes_invalid_between_reads(runtime_env) -> None:
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            # Broken: was valid when the route was first resolved, is not
            # valid any more by the time this checkpoint reads it.
            "live_sessions": "nope",
        },
    )

    outcome = pane_registry.validate_route(
        live_id="s2", launch_id="ai-1", provider="codex",
        caller_pane_id="%2", caller_terminal="tmux",
    )

    assert outcome.ok is False


def test_validate_route_reads_the_launch_record_exactly_once(runtime_env, monkeypatch) -> None:
    # Hole 2: one snapshot for the whole checkpoint -- destination,
    # caller, and owner checks must all come from the SAME read.
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
                {"live_id": "s2", "provider": "codex", "pane_id": "%3", "active": True},
            ],
        },
    )
    calls: list[str] = []
    real_loader = pane_registry.load_registry_by_session_id

    def _counting_loader(session_id):
        calls.append(session_id)
        return real_loader(session_id)

    monkeypatch.setattr(pane_registry, "load_registry_by_session_id", _counting_loader)

    outcome = pane_registry.validate_route(
        live_id="s2", launch_id="ai-1", provider="codex",
        caller_pane_id="%2", caller_terminal="tmux",
    )

    assert outcome.ok is True
    assert calls == ["ai-1"]


# --- Hole 3: a present, valid inventory is authoritative; evidence that ----
# --- resolves to nothing must fail, not fall back to a callerless pick. ---


def test_resolve_live_route_refuses_when_valid_inventory_lacks_the_provider(runtime_env, monkeypatch) -> None:
    # Hole 3(a): the caller's own launch has a VALID inventory that simply
    # doesn't mention "codex" (it's Claude-only). A healthy LEGACY codex
    # registration sits right alongside it in the same registry file. The
    # present inventory is authoritative: this must refuse, never fall
    # through to that legacy provider entry.
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-session", provider="codex", pane_id="%2", project_id=project_id)
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            # A healthy legacy codex registration -- would happily mount
            # if the caller ever fell back to it.
            "providers": {
                "codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"},
                "claude": {"pane_id": "%9"},
            },
            "live_sessions": [
                {"live_id": "c1", "provider": "claude", "pane_id": "%9", "active": True},
            ],
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-test": "%2"}),
    )

    outcome = resolve_live_route("codex", work_dir)

    assert outcome is not None
    resolution, caller = outcome
    assert resolution.ok is False
    assert resolution.error != ""

    # Proof that "refuses" here really is overriding a healthy fallback,
    # not coincidentally agreeing with an already-broken one: the legacy
    # codex pane itself checks out as alive on its own terms.
    legacy_pane_alive = pane_registry._provider_pane_alive(
        {"providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}}, "terminal": "tmux", "work_dir": str(work_dir)},
        "codex",
    )
    assert legacy_pane_alive is True


def test_resolve_live_route_refuses_when_evidence_matches_nothing_but_one_destination_exists(
    runtime_env,
) -> None:
    # Hole 3(b): caller evidence was SUPPLIED but matches no launch at all,
    # while exactly one otherwise-eligible destination sits in the
    # project. Must refuse -- never silently fall back to the callerless
    # single-destination pick, which would have succeeded.
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
            ],
        },
    )

    # Pane %999 matches nothing anywhere in the project.
    outcome = resolve_live_route("codex", work_dir, caller_pane_id="%999", caller_terminal="tmux")

    assert outcome is not None
    resolution, caller = outcome
    assert resolution.ok is False
    assert resolution.error == "unknown_caller"
    assert caller is None


def test_resolve_live_route_still_falls_back_when_no_evidence_and_no_inventory_anywhere(
    runtime_env, monkeypatch
) -> None:
    # Guardrail against over-correcting Hole 3(b): a caller that supplies
    # NO evidence at all, in a project with NO inventory anywhere, must
    # still get today's byte-identical un-routed behaviour -- this is the
    # Phase 1 hard requirement, and it must survive the Hole 3 fix.
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-session", provider="codex", pane_id="%2", project_id=project_id)
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"}},
            # No "live_sessions" key at all anywhere in the project.
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-test": "%2"}),
    )

    assert resolve_live_route("codex", work_dir) is None


# --- Item 1: caller proof is required by TOPOLOGY, not by whether -----------
# --- evidence happened to be supplied. ---------------------------------


def test_validate_route_refuses_duplicate_pool_no_evidence_saved_caller_is_destination(
    runtime_env,
) -> None:
    # A complete, well-formed route into a two-Codex inventory, saved
    # caller_live_id naming the DESTINATION itself, with NEITHER
    # caller_pane_id NOR caller_terminal supplied. Mandatory endpoint
    # fields alone don't catch this -- they only prove the destination is
    # what it was, never that it isn't the caller. Must refuse.
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
                {"live_id": "s2", "provider": "codex", "pane_id": "%3", "active": True},
            ],
        },
    )

    outcome = pane_registry.validate_route(
        live_id="s2", launch_id="ai-1", provider="codex", caller_live_id="s2",
    )

    assert outcome.ok is False
    assert outcome.error == pane_registry.UNKNOWN_CALLER


def test_validate_route_refuses_duplicate_pool_no_evidence_saved_caller_is_sibling(
    runtime_env,
) -> None:
    # Same duplicate-provider pool, but the saved caller_live_id names the
    # SIBLING, not the destination -- still refuses, because a saved
    # identity with nothing corroborating it is weaker than no identity at
    # all: it must never be trusted on its own.
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
                {"live_id": "s2", "provider": "codex", "pane_id": "%3", "active": True},
            ],
        },
    )

    outcome = pane_registry.validate_route(
        live_id="s2", launch_id="ai-1", provider="codex", caller_live_id="s1",
    )

    assert outcome.ok is False
    assert outcome.error == pane_registry.UNKNOWN_CALLER


def test_validate_route_accepts_verified_live_credential_without_terminal_evidence(runtime_env) -> None:
    home, work_dir, project_id = runtime_env
    _write_registry(home, "ai-1", {
        "ccb_session_id": "ai-1", "ccb_project_id": project_id,
        "work_dir": str(work_dir), "terminal": "tmux", "updated_at": int(time.time()),
        "live_sessions": [
            {"live_id": "s1", "provider": "codex", "pane_id": "%2", "auth_token": "token-1"},
            {"live_id": "s2", "provider": "codex", "pane_id": "%3", "auth_token": "token-2"},
        ],
    })
    outcome = pane_registry.validate_route(
        live_id="s2", launch_id="ai-1", provider="codex",
        caller_live_id="s1", caller_token="token-1",
    )
    assert outcome.ok
    forged = pane_registry.validate_route(
        live_id="s2", launch_id="ai-1", provider="codex",
        caller_live_id="s1", caller_token="wrong",
    )
    assert forged.error == pane_registry.UNKNOWN_CALLER
    self_route = pane_registry.validate_route(
        live_id="s1", launch_id="ai-1", provider="codex",
        caller_live_id="s1", caller_token="token-1",
    )
    assert self_route.error == pane_registry.SELF_ONLY


def test_validate_route_refuses_saved_caller_without_corroborating_evidence_even_in_unique_pool(
    runtime_env,
) -> None:
    # A SINGLE-session provider pool (no ambiguity from topology alone),
    # but the route still saved a caller_live_id and supplies nothing to
    # corroborate it. That saved identity must not be trusted on its own
    # either.
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
            ],
        },
    )

    outcome = pane_registry.validate_route(
        live_id="s1", launch_id="ai-1", provider="codex", caller_live_id="someone-else",
    )

    assert outcome.ok is False
    assert outcome.error == pane_registry.UNKNOWN_CALLER


def test_validate_route_still_allows_genuinely_callerless_routing_to_unique_destination(
    runtime_env,
) -> None:
    # Positive control: a single-session provider pool, no saved caller,
    # no evidence supplied at all -- this must remain allowed. (Mirrors
    # `test_validate_route_accepts_still_active_destination`, stated
    # explicitly here as the Item 1 positive case.)
    home, work_dir, project_id = runtime_env
    _write_registry(
        home,
        "ai-1",
        {
            "ccb_session_id": "ai-1",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex": {"pane_id": "%2"}},
            "live_sessions": [
                {"live_id": "s1", "provider": "codex", "pane_id": "%2", "active": True},
            ],
        },
    )

    outcome = pane_registry.validate_route(live_id="s1", launch_id="ai-1", provider="codex")

    assert outcome.ok is True
