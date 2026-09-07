from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import ccb_runtime_status
import pane_registry
from ccb_runtime_status import ProjectRuntimeStatus, ProviderRuntimeStatus, resolve_project_runtime_status
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
