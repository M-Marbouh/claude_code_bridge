from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import ccb_runtime_status
import pane_registry
from ccb_runtime_status import resolve_project_runtime_status
from pane_registry import load_registry_by_project_id
from project_id import compute_ccb_project_id


class _FakeBackend:
    def __init__(self, alive: set[str], marker_map: dict[str, str] | None = None):
        self.alive = set(alive)
        self.marker_map = dict(marker_map or {})

    def is_alive(self, pane_id: str) -> bool:
        return pane_id in self.alive

    def find_pane_by_title_marker(self, marker: str, cwd_hint: str = "") -> str | None:
        return self.marker_map.get(marker)


def _write_config(work_dir: Path, providers: str = "codex,codex:worker,claude,claude:worker") -> None:
    cfg = work_dir / ".ccb"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "ccb.config").write_text(providers + "\n", encoding="utf-8")


def _write_session(work_dir: Path, filename: str, *, key: str, provider: str, pane_id: str, project_id: str) -> None:
    cfg = work_dir / ".ccb"
    cfg.mkdir(parents=True, exist_ok=True)
    base, instance = key.split(":", 1) if ":" in key else (key, None)
    (cfg / filename).write_text(
        json.dumps(
            {
                "provider": provider,
                "instance": instance,
                "qualified_provider": key,
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


def test_runtime_status_reports_mounted_instance(runtime_env, monkeypatch: pytest.MonkeyPatch) -> None:
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-worker-session", key="codex:worker", provider="codex", pane_id="%2", project_id=project_id)
    _write_registry(
        home,
        "live",
        {
            "ccb_session_id": "live",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex:worker": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-worker-test"}},
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-worker-test": "%2"}),
    )

    status = resolve_project_runtime_status(work_dir).providers["codex:worker"]

    assert status.configured is True
    assert status.registered is True
    assert status.pane_alive is True
    assert status.session_bound is True
    assert status.daemon_online is True
    assert status.mounted is True
    assert status.reason == ""


def test_runtime_status_registered_but_pane_dead(runtime_env, monkeypatch: pytest.MonkeyPatch) -> None:
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-worker-session", key="codex:worker", provider="codex", pane_id="%2", project_id=project_id)
    _write_registry(
        home,
        "dead",
        {
            "ccb_session_id": "dead",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex:worker": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-worker-test"}},
        },
    )
    monkeypatch.setattr(pane_registry, "get_backend_for_session", lambda _rec: _FakeBackend(set()))

    status = resolve_project_runtime_status(work_dir).providers["codex:worker"]

    assert status.registered is True
    assert status.pane_alive is False
    assert status.mounted is False
    assert status.reason == "pane_dead"


def test_runtime_status_configured_but_not_registered(runtime_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _home, work_dir, _project_id = runtime_env
    monkeypatch.setattr(pane_registry, "get_backend_for_session", lambda _rec: _FakeBackend(set()))

    status = resolve_project_runtime_status(work_dir).providers["codex:worker"]

    assert status.configured is True
    assert status.registered is False
    assert status.mounted is False
    assert status.reason == "not_registered"


def test_runtime_status_daemon_offline(runtime_env, monkeypatch: pytest.MonkeyPatch) -> None:
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-worker-session", key="codex:worker", provider="codex", pane_id="%2", project_id=project_id)
    _write_registry(
        home,
        "live",
        {
            "ccb_session_id": "live",
            "ccb_project_id": project_id,
            "work_dir": str(work_dir),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {"codex:worker": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-worker-test"}},
        },
    )
    monkeypatch.setattr(
        pane_registry,
        "get_backend_for_session",
        lambda _rec: _FakeBackend({"%2"}, {"CCB-Codex-worker-test": "%2"}),
    )
    monkeypatch.setattr(ccb_runtime_status, "is_project_askd_online", lambda *_args, **_kwargs: False)

    status = resolve_project_runtime_status(work_dir).providers["codex:worker"]

    assert status.pane_alive is True
    assert status.session_bound is True
    assert status.daemon_online is False
    assert status.mounted is False
    assert status.reason == "daemon_offline"


def test_runtime_status_base_alive_does_not_mount_missing_instance(runtime_env, monkeypatch: pytest.MonkeyPatch) -> None:
    home, work_dir, project_id = runtime_env
    _write_session(work_dir, ".codex-session", key="codex", provider="codex", pane_id="%1", project_id=project_id)
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
    assert project.providers["codex:worker"].registered is False
    assert project.providers["codex:worker"].mounted is False
    assert load_registry_by_project_id(project_id, "codex") is not None
    assert load_registry_by_project_id(project_id, "codex:worker") is None
