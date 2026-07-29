from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import time
from pathlib import Path

from task_receipts import (
    find_receipt,
    iter_receipts,
    new_peer_receipt,
    new_receipt,
    read_peer_reply,
    receipt_path,
    update_peer_delivery,
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


def test_pend_codex_recovery_returns_only_marker_bearing_final(
    monkeypatch, tmp_path: Path
) -> None:
    pend = _load_pend_module()
    task_id = "20260729-120000-000-1"
    session_root = tmp_path / "sessions"
    log = session_root / "rollout.jsonl"
    log.parent.mkdir(parents=True)
    entries = [
        {
            "type": "session_meta",
            "payload": {"cwd": str(tmp_path)},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": f"CCB_REQ_ID: {task_id}\nQuestion",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Historical A"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Historical B"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [
                    {
                        "type": "output_text",
                        "text": f"Actual final\nCCB_DONE: {task_id}",
                    }
                ],
            },
        },
    ]
    log.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_SESSION_ROOT", str(session_root))

    recovered = pend._recover_codex_reply(
        {"task_id": task_id, "work_dir": str(tmp_path)}
    )

    assert recovered == "Actual final"


def test_peer_delivery_update_persists_target_and_confirmation(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("task_receipts.tempfile.gettempdir", lambda: str(tmp_path))
    status = tmp_path / "task.status"
    log = tmp_path / "task.log"
    reply = tmp_path / "task.reply"
    receipt = new_peer_receipt(
        task_id="task-1",
        peer_provider="claude",
        caller="codex",
        intent="background",
        work_dir=tmp_path,
        status_file=status,
        log_file=log,
        reply_file=reply,
    )
    write_receipt(receipt_path("peer-claude", "task-1"), receipt)

    updated = update_peer_delivery(
        "task-1",
        confirmation="sent",
        target_work_dir="/tmp/target",
        target_project_id="project-target",
        target_log_path="/tmp/target.jsonl",
    )

    assert updated is not None
    assert updated["delivery_confirmation"] == "sent"
    assert updated["peer_target_work_dir"] == "/tmp/target"
    assert updated["peer_target_project_id"] == "project-target"
    assert updated["peer_target_log_path"] == "/tmp/target.jsonl"
    persisted = find_receipt("task-1")
    assert persisted is not None
    assert persisted[1]["delivery_confirmation"] == "sent"


def test_pend_reconciles_sent_peer_delivery_to_observed(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    pend = _load_pend_module()
    status = tmp_path / "task.status"
    log = tmp_path / "task.log"
    status.write_text(
        "2026-07-29T21:00:00+00:00 finished exit_code=0\n",
        encoding="utf-8",
    )
    log.write_text("", encoding="utf-8")
    receipt = {
        "task_id": "task-1",
        "provider": "peer-claude",
        "peer_provider": "claude",
        "reply_expected": True,
        "status_file": str(status),
        "log_file": str(log),
        "delivery_confirmation": "sent",
        "peer_target_work_dir": "/tmp/target",
        "peer_target_project_id": "project-target",
        "peer_target_log_path": "/tmp/target.jsonl",
    }
    updates: list[dict] = []
    monkeypatch.setattr(
        pend,
        "provider_request_anchor_seen",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        pend,
        "update_peer_delivery",
        lambda *_args, **kwargs: updates.append(kwargs)
        or {**receipt, "delivery_confirmation": "observed"},
    )

    rc = pend._show_receipt(receipt)

    assert rc == pend.EXIT_NO_REPLY
    assert updates == [
        {
            "confirmation": "observed",
            "target_work_dir": "/tmp/target",
            "target_project_id": "project-target",
            "target_log_path": "/tmp/target.jsonl",
        }
    ]
    assert "delivery=observed" in capsys.readouterr().err


def test_pend_skills_preserve_same_turn_async_guardrail() -> None:
    for relative in (
        "claude_skills/pend/SKILL.md",
        "claude_skills/pend/SKILL.md.powershell",
        "codex_skills/pend/SKILL.md",
        "codex_skills/pend/SKILL.md.powershell",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "[CCB_ASYNC_SUBMITTED ...]" in source
        assert "in the current turn, do not run `pend`" in source


def _receipt(
    task_id: str,
    provider: str,
    *,
    session: str,
    pane: str,
    project: str,
    submitted: str,
    caller: str = "claude",
) -> dict:
    return {
        "task_id": task_id,
        "provider": provider,
        "caller": caller,
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
        timeout_seconds=45,
    )
    path = receipt_path("codex", data["task_id"], root=tmp_path)
    write_receipt(path, data)

    found = find_receipt(data["task_id"], root=tmp_path)

    assert found is not None
    assert found[1]["caller_session_id"] == "ccb-1"
    assert found[1]["caller_pane_id"] == "%7"
    assert found[1]["caller_terminal"] == "tmux"
    assert found[1]["timeout_seconds"] == 45.0
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


def test_pend_receipts_are_scoped_to_current_session_not_caller_pane(monkeypatch) -> None:
    pend = _load_pend_module()
    records = [
        (
            Path("other-session.json"),
            _receipt("other", "codex", session="ccb-2", pane="2", project="p", submitted="4"),
        ),
        (Path("peer.json"), _receipt("peer", "peer-codex", session="ccb-1", pane="2", project="p", submitted="3")),
        (
            Path("local.json"),
            _receipt(
                "local",
                "codex",
                session="ccb-1",
                pane="14",
                project="p",
                submitted="2",
                caller="codex",
            ),
        ),
    ]
    monkeypatch.setattr(pend, "iter_receipts", lambda: records)

    found = pend._current_session_receipts("p", "ccb-1", "codex")

    assert [data["task_id"] for _path, data in found] == ["peer", "local"]


def test_pend_receipts_do_not_cross_projects(monkeypatch) -> None:
    pend = _load_pend_module()
    records = [
        (Path("other.json"), _receipt("other", "codex", session="ccb-1", pane="2", project="other", submitted="2")),
        (Path("current.json"), _receipt("current", "codex", session="ccb-1", pane="2", project="p", submitted="1")),
    ]
    monkeypatch.setattr(pend, "iter_receipts", lambda: records)

    found = pend._current_session_receipts("p", "ccb-1", "codex")

    assert [data["task_id"] for _path, data in found] == ["current"]


def test_pend_restart_does_not_fall_back_to_old_session_receipts(monkeypatch) -> None:
    pend = _load_pend_module()
    records = [
        (Path("new.json"), _receipt("new", "codex", session="old-2", pane="5", project="p", submitted="2")),
        (Path("old.json"), _receipt("old", "codex", session="old-1", pane="5", project="p", submitted="1")),
    ]
    monkeypatch.setattr(pend, "iter_receipts", lambda: records)

    found = pend._current_session_receipts("p", "restarted", "codex")

    assert found == []


def test_pend_session_resolution_prefers_environment(monkeypatch) -> None:
    pend = _load_pend_module()
    monkeypatch.setattr(pend, "_current_project_id", lambda: "p")
    monkeypatch.setattr(pend, "_executing_pane", lambda: ("14", "wezterm"))
    monkeypatch.setattr(pend, "caller_session_id", lambda: "env-session")
    monkeypatch.setattr(
        pend,
        "load_registry_by_pane",
        lambda *_args, **_kwargs: {
            "ccb_session_id": "registry-session",
            "providers": {"codex": {"pane_id": "14"}},
        },
    )
    monkeypatch.setenv("CCB_CALLER", "codex")

    assert pend._current_session_context() == ("p", "env-session", "codex")


def test_pend_session_resolution_falls_back_to_registry_pane(monkeypatch) -> None:
    pend = _load_pend_module()
    monkeypatch.setattr(pend, "_current_project_id", lambda: "p")
    monkeypatch.setattr(pend, "_executing_pane", lambda: ("14", "wezterm"))
    monkeypatch.setattr(pend, "caller_session_id", lambda: "")
    monkeypatch.setattr(
        pend,
        "load_registry_by_pane",
        lambda pane, **kwargs: {
            "ccb_session_id": "registry-session",
            "providers": {"codex": {"pane_id": pane}},
        },
    )

    assert pend._current_session_context() == ("p", "registry-session", "codex")


def test_pend_without_session_requires_exact_task_id(monkeypatch, capsys) -> None:
    pend = _load_pend_module()
    monkeypatch.setattr(pend, "_current_session_context", lambda: ("p", "", ""))

    rc = pend.main(["pend", "codex"])

    assert rc == pend.EXIT_NO_REPLY
    assert "use an exact task ID" in capsys.readouterr().err


def test_pend_restart_message_keeps_old_receipts_exact_id_only(monkeypatch, capsys) -> None:
    pend = _load_pend_module()
    records = [
        (Path("old.json"), _receipt("old", "codex", session="old-session", pane="2", project="p", submitted="1")),
    ]
    monkeypatch.setattr(pend, "iter_receipts", lambda: records)
    monkeypatch.setattr(pend, "_current_session_context", lambda: ("p", "new-session", "claude"))

    rc = pend.main(["pend", "codex"])

    assert rc == pend.EXIT_NO_REPLY
    assert "No codex task in current CCB session; use exact task ID" in capsys.readouterr().err


def test_pend_relational_selectors_require_bound_model(monkeypatch, capsys) -> None:
    pend = _load_pend_module()
    monkeypatch.setattr(pend, "_current_session_context", lambda: ("", "", ""))

    rc = pend.main(["pend", "peer"])

    assert rc == pend.EXIT_ERROR
    assert "requires a bound model pane" in capsys.readouterr().err


def test_pend_peer_and_local_resolve_relative_to_model() -> None:
    pend = _load_pend_module()

    assert pend._resolve_responder("peer", "claude") == ("codex", "")
    assert pend._resolve_responder("local", "claude") == ("claude", "")
    assert pend._resolve_responder("peer", "codex") == ("claude", "")
    assert pend._resolve_responder("local", "codex") == ("codex", "")


def test_pend_provider_count_skips_unfinished_and_backfills_completed(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    pend = _load_pend_module()
    current_status = tmp_path / "current.status"
    current_log = tmp_path / "current.log"
    current_status.write_text("running pid=12345\n", encoding="utf-8")
    current_log.write_text("", encoding="utf-8")

    peer_status = tmp_path / "peer.status"
    peer_log = tmp_path / "peer.log"
    peer_reply = tmp_path / "peer.reply"
    peer_status.write_text("finished exit_code=0\n", encoding="utf-8")
    peer_log.write_text("Peer message accepted.\n", encoding="utf-8")
    peer_reply.write_text("reply-new\n", encoding="utf-8")

    old_status = tmp_path / "old.status"
    old_log = tmp_path / "old.log"
    old_status.write_text("finished exit_code=0\n", encoding="utf-8")
    old_log.write_text("reply-old\n", encoding="utf-8")

    current = _receipt("current", "codex", session="ccb-1", pane="2", project="p", submitted="3")
    current.update({"status_file": str(current_status), "log_file": str(current_log)})
    peer = _receipt("new", "peer-codex", session="ccb-1", pane="2", project="p", submitted="2")
    peer.update(
        {
            "status_file": str(peer_status),
            "log_file": str(peer_log),
            "peer_reply_file": str(peer_reply),
            "reply_expected": True,
        }
    )
    old = _receipt("old", "codex", session="ccb-1", pane="2", project="p", submitted="1")
    old.update({"status_file": str(old_status), "log_file": str(old_log)})
    records = [
        (Path("current.json"), current),
        (Path("new.json"), peer),
        (Path("old.json"), old),
    ]
    monkeypatch.setattr(pend, "iter_receipts", lambda: records)
    monkeypatch.setattr(pend, "_current_session_context", lambda: ("p", "ccb-1", "claude"))
    monkeypatch.setattr(
        pend,
        "_legacy_pend",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy pend must be explicit")),
    )

    rc = pend.main(["pend", "codex", "2"])

    assert rc == pend.EXIT_OK
    assert capsys.readouterr().out.splitlines() == [
        "[TASK new]",
        "reply-new",
        "---",
        "[TASK old]",
        "reply-old",
    ]


def test_peer_history_requires_saved_reply_not_finished_transport(tmp_path: Path) -> None:
    pend = _load_pend_module()
    status = tmp_path / "peer.status"
    reply = tmp_path / "peer.reply"
    status.write_text("finished exit_code=0\n", encoding="utf-8")
    reply.write_text("", encoding="utf-8")
    receipt = {
        "provider": "peer-codex",
        "status_file": str(status),
        "peer_reply_file": str(reply),
        "reply_expected": True,
    }

    assert pend._receipt_has_completed_reply(receipt) is False


def test_pend_legacy_history_requires_explicit_flag(monkeypatch) -> None:
    pend = _load_pend_module()
    calls = []
    monkeypatch.setattr(pend, "_legacy_pend", lambda provider, extra: calls.append((provider, extra)) or pend.EXIT_OK)

    rc = pend.main(["pend", "codex", "--legacy", "3"])

    assert rc == pend.EXIT_OK
    assert calls == [("codex", ["3"])]


def test_bare_pend_fails_when_current_session_has_multiple_tasks(monkeypatch, capsys) -> None:
    pend = _load_pend_module()
    records = [
        (Path("a.json"), _receipt("a", "codex", session="ccb-1", pane="2", project="p", submitted="2")),
        (Path("b.json"), _receipt("b", "claude", session="ccb-1", pane="14", project="p", submitted="1")),
    ]
    monkeypatch.setattr(pend, "iter_receipts", lambda: records)
    monkeypatch.setattr(pend, "_current_session_context", lambda: ("p", "ccb-1", "codex"))

    rc = pend.main(["pend"])

    assert rc == pend.EXIT_ERROR
    output = capsys.readouterr().err
    assert "[AMBIGUOUS]" in output
    assert "a, b" in output


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


def test_pend_reports_pid_invisible_fresh_waiter_as_pending(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    pend = _load_pend_module()
    monkeypatch.setattr(pend, "_recover_provider_reply", lambda _receipt: None)
    status = tmp_path / "task.status"
    log = tmp_path / "task.log"
    status.write_text("submitted\nrunning pid=12345\n", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    monkeypatch.setattr(pend, "_pid_is_alive", lambda _pid: False)

    rc = pend._show_receipt(
        {
            "task_id": "x",
            "provider": "codex",
            "status_file": str(status),
            "log_file": str(log),
            "timeout_seconds": 60,
        }
    )

    assert rc == pend.EXIT_NO_REPLY
    output = capsys.readouterr().err
    assert "[PENDING]" in output
    assert "[INCOMPLETE]" not in output


def test_pend_reports_pid_invisible_stale_waiter_as_incomplete(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    pend = _load_pend_module()
    monkeypatch.setattr(pend, "_recover_provider_reply", lambda _receipt: None)
    status = tmp_path / "task.status"
    log = tmp_path / "task.log"
    status.write_text("submitted\nrunning pid=12345\n", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    stale_time = time.time() - 100
    os.utime(status, (stale_time, stale_time))
    monkeypatch.setattr(pend, "_pid_is_alive", lambda _pid: False)

    rc = pend._show_receipt(
        {
            "task_id": "x",
            "provider": "codex",
            "status_file": str(status),
            "log_file": str(log),
            "timeout_seconds": 60,
        }
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


class _ExchangeReader:
    def __init__(self, exchanges: list[dict]) -> None:
        self.exchanges = exchanges
        self.requests: list[int] = []

    def latest_exchanges(self, n: int) -> list[dict]:
        self.requests.append(n)
        return self.exchanges[-n:]


def _bind_overlay(pend, monkeypatch, reader: _ExchangeReader) -> None:
    monkeypatch.setattr(pend, "_current_session_context", lambda: ("p", "ccb-1", "claude"))
    monkeypatch.setattr(
        pend,
        "_current_tab_registry",
        lambda project, session: {
            "ccb_project_id": project,
            "ccb_session_id": session,
            "work_dir": str(ROOT),
        },
    )
    monkeypatch.setattr(pend, "provider_log_reader", lambda *_args, **_kwargs: reader)


def _completed_overlay_receipt(
    tmp_path: Path,
    task_id: str,
    *,
    submitted: str,
    finished: str,
    reply: str,
) -> dict:
    status = tmp_path / f"{task_id}.status"
    log = tmp_path / f"{task_id}.log"
    status.write_text(f"{finished} finished exit_code=0\n", encoding="utf-8")
    log.write_text(f"{reply}\n", encoding="utf-8")
    receipt = _receipt(task_id, "codex", session="ccb-1", pane="2", project="p", submitted=submitted)
    receipt.update({"status_file": str(status), "log_file": str(log)})
    return receipt


def test_pend_peer_surfaces_manual_exchange_without_receipt(monkeypatch, capsys) -> None:
    pend = _load_pend_module()
    reader = _ExchangeReader(
        [{"ts": "2026-07-22T10:00:00Z", "req_id": None, "question": "Manual question", "reply": "Manual reply"}]
    )
    _bind_overlay(pend, monkeypatch, reader)
    monkeypatch.setattr(pend, "iter_receipts", lambda: [])

    rc = pend.main(["pend", "peer"])

    assert rc == pend.EXIT_OK
    assert capsys.readouterr().out.strip() == "Manual reply"


def test_pend_overlay_orders_manual_and_completed_without_duplicate(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pend = _load_pend_module()
    task_id = "20260722-100000-001-1"
    receipt = _completed_overlay_receipt(
        tmp_path,
        task_id,
        submitted="2026-07-22T09:00:00Z",
        finished="2026-07-22T10:02:00+00:00",
        reply="Receipt reply",
    )
    reader = _ExchangeReader(
        [
            {
                "ts": "2026-07-22T10:02:00Z",
                "req_id": task_id,
                "question": f"CCB_REQ_ID: {task_id}\nQuestion",
                "reply": "Transcript duplicate",
            },
            {"ts": "2026-07-22T10:01:00Z", "req_id": None, "question": "Manual", "reply": "Manual reply"},
        ]
    )
    _bind_overlay(pend, monkeypatch, reader)
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("receipt.json"), receipt)])

    rc = pend.main(["pend", "peer", "2"])

    assert rc == pend.EXIT_OK
    output = capsys.readouterr().out
    assert output.splitlines() == ["[TASK " + task_id + "]", "Receipt reply", "---", "Manual reply"]
    assert "Transcript duplicate" not in output


def test_pend_overlay_keeps_pending_and_hides_partial_twin(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pend = _load_pend_module()
    task_id = "20260722-100100-001-1"
    status = tmp_path / "pending.status"
    log = tmp_path / "pending.log"
    status.write_text("2026-07-22T10:01:00+00:00 running pid=12345\n", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    receipt = _receipt(
        task_id,
        "codex",
        session="ccb-1",
        pane="2",
        project="p",
        submitted="2026-07-22T10:01:00Z",
    )
    receipt.update({"status_file": str(status), "log_file": str(log)})
    reader = _ExchangeReader(
        [{"ts": "2026-07-22T10:02:00Z", "req_id": task_id, "question": f"CCB_REQ_ID: {task_id}", "reply": "Partial leak"}]
    )
    _bind_overlay(pend, monkeypatch, reader)
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("pending.json"), receipt)])
    monkeypatch.setattr(pend, "_pid_is_alive", lambda _pid: True)

    rc = pend.main(["pend", "peer"])

    captured = capsys.readouterr()
    assert rc == pend.EXIT_NO_REPLY
    assert "[PENDING]" in captured.err
    assert "Partial leak" not in captured.out + captured.err


def test_pend_peer_count_returns_three_newest_eligible_merged_items(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pend = _load_pend_module()
    receipt = _completed_overlay_receipt(
        tmp_path,
        "20260722-095900-001-1",
        submitted="2026-07-22T09:59:00Z",
        finished="2026-07-22T10:03:00Z",
        reply="Newest receipt",
    )
    reader = _ExchangeReader(
        [
            {"ts": "2026-07-22T10:00:00Z", "req_id": None, "question": "m1", "reply": "Manual one"},
            {"ts": "2026-07-22T10:01:00Z", "req_id": None, "question": "m2", "reply": "Manual two"},
            {"ts": "2026-07-22T10:02:00Z", "req_id": None, "question": "m3", "reply": "Manual three"},
        ]
    )
    _bind_overlay(pend, monkeypatch, reader)
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("receipt.json"), receipt)])

    rc = pend.main(["pend", "peer", "3"])

    assert rc == pend.EXIT_OK
    output = capsys.readouterr().out
    assert "Newest receipt" in output
    assert "Manual three" in output
    assert "Manual two" in output
    assert "Manual one" not in output


def test_pend_overlay_uses_result_time_for_long_task_ordering(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pend = _load_pend_module()
    receipt = _completed_overlay_receipt(
        tmp_path,
        "20260722-090000-001-1",
        submitted="2026-07-22T09:00:00Z",
        finished="2026-07-22T11:00:00+01:00",
        reply="Long task result",
    )
    reader = _ExchangeReader(
        [{"ts": "2026-07-22T09:30:00Z", "req_id": None, "question": "Manual", "reply": "Later question reply"}]
    )
    _bind_overlay(pend, monkeypatch, reader)
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("receipt.json"), receipt)])

    rc = pend.main(["pend", "peer"])

    assert rc == pend.EXIT_OK
    assert capsys.readouterr().out.strip() == "Long task result"


def test_pend_overlay_fetch_depth_survives_more_than_five_deduped_exchanges(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pend = _load_pend_module()
    records = []
    exchanges = [{"ts": "2026-07-22T09:00:00Z", "req_id": None, "question": "Manual", "reply": "Deep manual"}]
    for index in range(6):
        task_id = f"20260722-10000{index}-001-1"
        receipt = _completed_overlay_receipt(
            tmp_path,
            task_id,
            submitted=f"2026-07-22T10:00:0{index}Z",
            finished=f"2026-07-22T10:00:0{index}Z",
            reply=f"Receipt {index}",
        )
        records.append((Path(f"{index}.json"), receipt))
        exchanges.append(
            {"ts": f"2026-07-22T10:00:0{index}Z", "req_id": task_id, "question": f"CCB_REQ_ID: {task_id}", "reply": "Duplicate"}
        )
    reader = _ExchangeReader(exchanges)
    _bind_overlay(pend, monkeypatch, reader)
    monkeypatch.setattr(pend, "iter_receipts", lambda: records)

    rc = pend.main(["pend", "peer", "7"])

    assert rc == pend.EXIT_OK
    output = capsys.readouterr().out
    assert reader.requests == [13]
    assert "Deep manual" in output
    assert "Duplicate" not in output


def test_pend_overlay_excludes_unmatched_non_null_req_id(monkeypatch, capsys) -> None:
    pend = _load_pend_module()
    reader = _ExchangeReader(
        [
            {"ts": "2026-07-22T10:00:00Z", "req_id": "foreign-task", "question": "CCB_REQ_ID: foreign-task", "reply": "Foreign"},
            {"ts": "2026-07-22T09:00:00Z", "req_id": None, "question": "Manual", "reply": "Manual"},
        ]
    )
    _bind_overlay(pend, monkeypatch, reader)
    monkeypatch.setattr(pend, "iter_receipts", lambda: [])

    rc = pend.main(["pend", "peer"])

    assert rc == pend.EXIT_OK
    assert capsys.readouterr().out.strip() == "Manual"
