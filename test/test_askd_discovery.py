from __future__ import annotations

import json
import subprocess

import askd.daemon as daemon_module
from askd.daemon import UnifiedAskDaemon


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
