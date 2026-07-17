from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

from task_receipts import (
    find_receipt,
    iter_receipts,
    new_peer_receipt,
    new_receipt,
    read_peer_reply,
    receipt_path,
    write_peer_reply,
    write_receipt,
)


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


def test_peer_receipt_preserves_direct_return_route(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CCB_CALLER_PANE_ID", "%7")
    monkeypatch.setenv("CCB_CALLER_TERMINAL", "tmux")
    monkeypatch.setattr("pane_registry.load_registry_by_project_id", lambda _project, _provider: {
        "ccb_session_id": "ccb-1",
        "providers": {
            "claude": {"pane_id": "%7", "pane_title_marker": "ccb-claude-project"},
        },
    })
    reply_file = tmp_path / "ask-peer-codex-task.reply"

    data = new_peer_receipt(
        task_id="task",
        peer_provider="codex",
        caller="claude",
        intent="background",
        work_dir=tmp_path,
        status_file=tmp_path / "task.status",
        log_file=tmp_path / "task.log",
        reply_file=reply_file,
    )

    assert data["provider"] == "peer-codex"
    assert data["reply_expected"] is True
    assert data["caller_pane_id"] == "%7"
    assert data["caller_pane_title_marker"] == "ccb-claude-project"
    assert data["caller_registry_session_id"] == "ccb-1"

    write_peer_reply(data, "Exact peer response")
    assert read_peer_reply(data) == "Exact peer response"


def test_peer_receipt_captures_live_marker_when_registry_discovery_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    class _Backend:
        def is_alive(self, pane_id: str) -> bool:
            return pane_id == "8"

        def pane_matches_cwd_strict(self, pane_id: str, work_dir: str) -> bool:
            return pane_id == "8" and work_dir == str(tmp_path)

        def find_pane_by_title_marker(self, marker: str, work_dir: str) -> str:
            assert marker.startswith("CCB-Codex-")
            assert work_dir == str(tmp_path)
            return "8"

    monkeypatch.setenv("CCB_CALLER_PANE_ID", "8")
    monkeypatch.setenv("CCB_CALLER_TERMINAL", "wezterm")
    monkeypatch.setattr("pane_registry.load_registry_by_project_id", lambda _project, _provider: None)
    monkeypatch.setattr("terminal.get_backend_for_session", lambda _session: _Backend())

    data = new_peer_receipt(
        task_id="task",
        peer_provider="claude",
        caller="codex",
        intent="wait",
        work_dir=tmp_path,
        status_file=tmp_path / "task.status",
        log_file=tmp_path / "task.log",
        reply_file=tmp_path / "task.reply",
    )

    assert data["caller_pane_title_marker"] == f"CCB-Codex-{data['ccb_project_id'][:8]}"


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


def test_pend_does_not_fall_back_to_another_project(monkeypatch) -> None:
    pend = _load_pend_module()
    records = [
        (Path("other.json"), _receipt("other", "codex", session="ccb-2", pane="2", project="p", submitted="2")),
    ]
    monkeypatch.setattr(pend, "iter_receipts", lambda: records)
    monkeypatch.setattr(pend, "caller_session_id", lambda: "ccb-1")
    monkeypatch.setattr(pend, "caller_pane", lambda: ("1", "wezterm"))
    monkeypatch.setattr(pend, "compute_ccb_project_id", lambda _path: "different-project")

    assert pend._latest_for_current_caller("codex") is None


def test_pend_recovers_latest_same_pane_receipt_after_session_restart(monkeypatch) -> None:
    pend = _load_pend_module()
    records = [
        (Path("new.json"), _receipt("new", "codex", session="old-2", pane="5", project="p", submitted="2")),
        (Path("old.json"), _receipt("old", "codex", session="old-1", pane="5", project="p", submitted="1")),
    ]
    monkeypatch.setattr(pend, "iter_receipts", lambda: records)
    monkeypatch.setattr(pend, "caller_session_id", lambda: "restarted")
    monkeypatch.setattr(pend, "caller_pane", lambda: ("9", "wezterm"))
    monkeypatch.setattr(pend, "compute_ccb_project_id", lambda _path: "p")

    found = pend._latest_for_current_caller("codex")

    assert found is not None
    assert found[1]["task_id"] == "new"


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


def test_pend_restart_uses_latest_same_project_receipt_across_old_panes(monkeypatch) -> None:
    pend = _load_pend_module()
    records = [
        (Path("a.json"), _receipt("a", "codex", session="old-1", pane="1", project="p", submitted="2")),
        (Path("b.json"), _receipt("b", "codex", session="old-2", pane="2", project="p", submitted="1")),
    ]
    monkeypatch.setattr(pend, "iter_receipts", lambda: records)
    monkeypatch.setattr(pend, "caller_session_id", lambda: "restarted")
    monkeypatch.setattr(pend, "caller_pane", lambda: ("9", "wezterm"))
    monkeypatch.setattr(pend, "compute_ccb_project_id", lambda _path: "p")

    found = pend._latest_for_current_caller("codex")

    assert found is not None
    assert found[1]["task_id"] == "a"


def test_pend_reads_exact_completed_task_log(tmp_path: Path, capsys) -> None:
    pend = _load_pend_module()
    pend._recover_provider_reply = lambda _receipt: None
    status = tmp_path / "task.status"
    log = tmp_path / "task.log"
    status.write_text("submitted\nfinished exit_code=0\n", encoding="utf-8")
    log.write_text("[CCB_TASK_START] task=x\nexact reply\n[CCB_TASK_END] task=x\n", encoding="utf-8")

    rc = pend._show_receipt({"task_id": "x", "provider": "codex", "status_file": str(status), "log_file": str(log)})

    assert rc == 0
    assert capsys.readouterr().out.strip() == "exact reply"


def test_pend_peer_task_waits_after_delivery_until_explicit_reply(tmp_path: Path, capsys) -> None:
    pend = _load_pend_module()
    status = tmp_path / "task.status"
    log = tmp_path / "task.log"
    reply = tmp_path / "task.reply"
    status.write_text("finished exit_code=0\n", encoding="utf-8")
    log.write_text("Peer message accepted.\n", encoding="utf-8")
    reply.write_text("", encoding="utf-8")

    rc = pend._show_receipt(
        {
            "task_id": "task",
            "provider": "peer-codex",
            "reply_expected": True,
            "status_file": str(status),
            "log_file": str(log),
            "peer_reply_file": str(reply),
        }
    )

    assert rc == pend.EXIT_NO_REPLY
    assert "awaiting peer reply" in capsys.readouterr().err


def test_pend_peer_task_returns_saved_reply_even_when_direct_delivery_failed(
    tmp_path: Path, capsys
) -> None:
    pend = _load_pend_module()
    status = tmp_path / "task.status"
    log = tmp_path / "task.log"
    reply = tmp_path / "task.reply"
    status.write_text("finished exit_code=0\npeer_reply_saved delivery=failed\n", encoding="utf-8")
    log.write_text("Peer message accepted.\n", encoding="utf-8")
    reply.write_text("Recoverable response\n", encoding="utf-8")

    rc = pend._show_receipt(
        {
            "task_id": "task",
            "provider": "peer-codex",
            "reply_expected": True,
            "status_file": str(status),
            "log_file": str(log),
            "peer_reply_file": str(reply),
        }
    )

    assert rc == pend.EXIT_OK
    assert capsys.readouterr().out.strip() == "Recoverable response"


def test_pend_reports_dead_waiter_as_incomplete(tmp_path: Path, monkeypatch, capsys) -> None:
    pend = _load_pend_module()
    monkeypatch.setattr(pend, "_recover_provider_reply", lambda _receipt: None)
    status = tmp_path / "task.status"
    log = tmp_path / "task.log"
    status.write_text("submitted\nrunning pid=12345\n", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    monkeypatch.setattr(pend, "_pid_is_alive", lambda _pid: False)

    rc = pend._show_receipt(
        {"task_id": "x", "provider": "codex", "status_file": str(status), "log_file": str(log)}
    )

    assert rc == pend.EXIT_ERROR
    output = capsys.readouterr().err
    assert "[INCOMPLETE]" in output
    assert "waiter_pid=12345" in output


def test_pend_recovers_exact_codex_done_after_waiter_failed(tmp_path: Path, monkeypatch, capsys) -> None:
    pend = _load_pend_module()
    req_id = "20260711-232834-787-461101"
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    rollout = session_root / "rollout.jsonl"
    entries = [
        {"type": "session_meta", "payload": {"cwd": str(tmp_path)}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": f"CCB_REQ_ID: {req_id}\n\ntask"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": f"Recovered reply.\nCCB_DONE: {req_id}"}},
    ]
    rollout.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    status = tmp_path / "task.status"
    log = tmp_path / "task.log"
    status.write_text("finished exit_code=1\n", encoding="utf-8")
    log.write_text("Codex pane died during request\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_SESSION_ROOT", str(session_root))

    rc = pend._show_receipt(
        {
            "task_id": req_id,
            "provider": "codex",
            "work_dir": str(tmp_path),
            "status_file": str(status),
            "log_file": str(log),
        }
    )

    assert rc == pend.EXIT_OK
    assert capsys.readouterr().out.strip() == "Recovered reply."
