from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

from ccb_runtime_status import ProjectRuntimeStatus, ProviderRuntimeStatus


ROOT = Path(__file__).resolve().parents[1]


def _load_mounted_module():
    path = ROOT / "bin" / "ccb-mounted.py"
    loader = importlib.machinery.SourceFileLoader("ccb_mounted_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _status(key: str, *, mounted: bool, reason: str = "") -> ProviderRuntimeStatus:
    return ProviderRuntimeStatus(
        key=key,
        provider=key,
        capable=True,
        configured=True,
        registered=True,
        pane_alive=mounted,
        session_bound=mounted,
        daemon_online=mounted,
        mounted=mounted,
        reason=reason,
    )


def test_ccb_mounted_outputs_mounted_providers(monkeypatch, tmp_path: Path, capsys) -> None:
    mounted = _load_mounted_module()
    project = ProjectRuntimeStatus(
        work_dir=str(tmp_path),
        ccb_project_id="proj",
        terminal="tmux",
        updated_at=1,
        providers={
            "codex": _status("codex", mounted=True),
            "claude": _status("claude", mounted=False, reason="daemon_offline"),
        },
    )
    monkeypatch.setattr(mounted, "resolve_project_runtime_status", lambda _work_dir: project)

    rc = mounted.main(["--json", str(tmp_path)])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mounted"] == ["codex"]
    assert payload["reasons"]["claude"] == "daemon_offline"


def test_ccb_mounted_simple_outputs_space_separated_providers(monkeypatch, tmp_path: Path, capsys) -> None:
    mounted = _load_mounted_module()
    project = ProjectRuntimeStatus(
        work_dir=str(tmp_path),
        ccb_project_id="proj",
        terminal="tmux",
        updated_at=1,
        providers={
            "claude": _status("claude", mounted=True),
        },
    )
    monkeypatch.setattr(mounted, "resolve_project_runtime_status", lambda _work_dir: project)

    rc = mounted.main(["--simple", str(tmp_path)])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "claude"
