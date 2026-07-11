from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

from task_receipts import find_receipt, iter_receipts, new_receipt, receipt_path, write_receipt


ROOT = Path(__file__).resolve().parents[1]


def _load_pend_module():
    path = ROOT / "bin" / "pend"
    loader = importlib.machinery.SourceFileLoader("pend_task_receipts_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _receipt(task_id: str, provider: str, *, session: str, pane: str, project: str, submitted: str) -> dict:
    return {
        "task_id": task_id,
        "provider": provider,
        "caller_session_id": session,
        "caller_pane_id": pane,
        "caller_terminal": "wezterm",
        "ccb_project_id": project,
        "submitted_at": submitted,
    }


def test_receipt_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CCB_SESSION_ID", "ccb-1")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.delenv("WEZTERM_PANE", raising=False)
    data = new_receipt(
        task_id="20260711-120000-001-99",
        provider="codex",
        caller="claude",
        work_dir=tmp_path,
        status_file=tmp_path / "task.status",
        log_file=tmp_path / "task.log",
    )
    path = receipt_path("codex", data["task_id"], root=tmp_path)
    write_receipt(path, data)

    found = find_receipt(data["task_id"], root=tmp_path)

    assert found is not None
    assert found[1]["caller_session_id"] == "ccb-1"
    assert found[1]["caller_pane_id"] == "%7"
    assert found[1]["caller_terminal"] == "tmux"
    assert list(iter_receipts(root=tmp_path))[0][0] == path


def test_pend_latest_is_scoped_to_caller_session(monkeypatch) -> None:
    pend = _load_pend_module()
    records = [
        (Path("new.json"), _receipt("new", "codex", session="ccb-2", pane="2", project="p", submitted="2")),
        (Path("same-session-other-pane.json"), _receipt("wrong", "codex", session="ccb-1", pane="9", project="p", submitted="3")),
        (Path("old.json"), _receipt("old", "codex", session="ccb-1", pane="1", project="p", submitted="1")),
    ]
    monkeypatch.setattr(pend, "iter_receipts", lambda: records)
    monkeypatch.setattr(pend, "caller_session_id", lambda: "ccb-1")
    monkeypatch.setattr(pend, "caller_pane", lambda: ("1", "wezterm"))

    found = pend._latest_for_current_caller("codex")

    assert found is not None
    assert found[1]["task_id"] == "old"


def test_pend_does_not_fall_back_to_another_caller(monkeypatch) -> None:
    pend = _load_pend_module()
    records = [
        (Path("other.json"), _receipt("other", "codex", session="ccb-2", pane="2", project="p", submitted="2")),
    ]
    monkeypatch.setattr(pend, "iter_receipts", lambda: records)
    monkeypatch.setattr(pend, "caller_session_id", lambda: "ccb-1")
    monkeypatch.setattr(pend, "caller_pane", lambda: ("1", "wezterm"))

    assert pend._latest_for_current_caller("codex") is None


def test_pend_provider_without_current_receipt_does_not_use_legacy(monkeypatch, capsys) -> None:
    pend = _load_pend_module()
    monkeypatch.setattr(pend, "_latest_for_current_caller", lambda _provider: None)
    monkeypatch.setattr(
        pend,
        "_legacy_pend",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy pend must be explicit")),
    )

    rc = pend.main(["pend", "codex"])

    assert rc == pend.EXIT_NO_REPLY
    assert "No codex task receipt for the current caller" in capsys.readouterr().err


def test_pend_refuses_ambiguous_same_project_receipts(monkeypatch, capsys) -> None:
    pend = _load_pend_module()
    records = [
        (Path("a.json"), _receipt("a", "codex", session="ccb-1", pane="1", project="p", submitted="2")),
        (Path("b.json"), _receipt("b", "codex", session="ccb-2", pane="2", project="p", submitted="1")),
    ]
    monkeypatch.setattr(pend, "iter_receipts", lambda: records)
    monkeypatch.setattr(pend, "caller_session_id", lambda: "")
    monkeypatch.setattr(pend, "caller_pane", lambda: ("", ""))
    monkeypatch.setattr(pend, "compute_ccb_project_id", lambda _path: "p")

    with pytest.raises(RuntimeError):
        pend._latest_for_current_caller("codex")

    assert "use pend <task-id>" in capsys.readouterr().err


def test_pend_reads_exact_completed_task_log(tmp_path: Path, capsys) -> None:
    pend = _load_pend_module()
    status = tmp_path / "task.status"
    log = tmp_path / "task.log"
    status.write_text("submitted\nfinished exit_code=0\n", encoding="utf-8")
    log.write_text("[CCB_TASK_START] task=x\nexact reply\n[CCB_TASK_END] task=x\n", encoding="utf-8")

    rc = pend._show_receipt({"task_id": "x", "provider": "codex", "status_file": str(status), "log_file": str(log)})

    assert rc == 0
    assert capsys.readouterr().out.strip() == "exact reply"
