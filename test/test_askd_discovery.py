from __future__ import annotations

import json
import subprocess

import askd.daemon as daemon_module
import ccb_runtime_status
from askd.daemon import UnifiedAskDaemon
from ccb_runtime_status import ProjectRuntimeStatus


def test_list_projects_operation_runs_direct_host_discovery(monkeypatch) -> None:
    captured: dict = {}

    def _run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([{"work_dir": "/tmp/peer"}]), stderr="")

    monkeypatch.setattr(daemon_module.subprocess, "run", _run)
    daemon = UnifiedAskDaemon(registry=object())  # type: ignore[arg-type]

    response = daemon._handle_request(
        {"type": "ask.request", "id": "list-1", "operation": "list_projects", "include_stale": True}
    )

    assert response["exit_code"] == 0
    assert response["entries"] == [{"work_dir": "/tmp/peer"}]
    assert "--json" in captured["argv"]
    assert "--direct" in captured["argv"]
    assert "--stale" in captured["argv"]
    assert captured["kwargs"]["timeout"] == 10


def test_list_projects_operation_rejects_invalid_output(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, stdout="{}", stderr=""),
    )
    daemon = UnifiedAskDaemon(registry=object())  # type: ignore[arg-type]

    response = daemon._handle_request(
        {"type": "ask.request", "id": "list-2", "operation": "list_projects"}
    )

    assert response["exit_code"] == 1
    assert "invalid result" in response["reply"]


def test_runtime_status_operation_forces_host_resolution(monkeypatch, tmp_path) -> None:
    expected = ProjectRuntimeStatus(
        work_dir=str(tmp_path),
        ccb_project_id="project-id",
        terminal="wezterm",
        updated_at=123,
        providers={},
    )
    captured: dict = {}

    def _resolve(work_dir, **kwargs):
        captured.update(work_dir=work_dir, kwargs=kwargs)
        return expected

    monkeypatch.setattr(ccb_runtime_status, "resolve_project_runtime_status", _resolve)
    daemon = UnifiedAskDaemon(registry=object(), work_dir=str(tmp_path))  # type: ignore[arg-type]

    response = daemon._handle_request(
        {
            "type": "ask.request",
            "id": "status-1",
            "operation": "runtime_status",
            "work_dir": str(tmp_path),
            "check_daemon": True,
        }
    )

    assert response["exit_code"] == 0
    assert response["project"] == expected.to_dict()
    assert captured["work_dir"] == str(tmp_path)
    assert captured["kwargs"]["_allow_daemon_proxy"] is False
    assert captured["kwargs"]["_daemon_online_override"] is True
