from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import time
from pathlib import Path

import completion_hook
from askd.adapters.base import ResolvedRoute
from task_receipts import (
    PersistOutcome,
    find_receipt,
    iter_receipts,
    new_peer_receipt,
    new_receipt,
    persist_proven_result,
    read_peer_reply,
    read_server_result,
    read_server_result_status,
    receipt_path,
    server_result_path,
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


def _load_completion_hook_module():
    path = ROOT / "bin" / "ccb-completion-hook"
    loader = importlib.machinery.SourceFileLoader("ccb_completion_hook_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _load_bridge_ask_module():
    path = ROOT / "bin" / "ccb-bridge-ask"
    loader = importlib.machinery.SourceFileLoader("ccb_bridge_ask_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_peer_reply_is_saved_before_delivery_is_ever_attempted(
    monkeypatch, tmp_path: Path
) -> None:
    """Task 2: the result must be persisted before any delivery is
    attempted. `ccb-bridge-ask` already implements exactly this for the one
    place in this codebase where "the result" and "delivery" are genuinely
    separate steps across a process/project boundary -- proven here by
    forcing EVERY delivery avenue to fail (both live-daemon target
    discovery and the direct-pane fallback) and confirming the answer is
    still saved, findable by its exact task id, and that no terminal send
    was ever attempted."""
    bridge = _load_bridge_ask_module()
    status = tmp_path / "task.status"
    log = tmp_path / "task.log"
    reply_file = tmp_path / "task.reply"
    receipt = new_peer_receipt(
        task_id="orig-task",
        peer_provider="codex",
        caller="claude",
        intent="wait",
        work_dir=tmp_path,
        status_file=status,
        log_file=log,
        reply_file=reply_file,
    )
    # No CCB_CALLER_PANE_ID/TMUX_PANE/WEZTERM_PANE is set (conftest already
    # scrubs them) -- the receipt's direct return address is incomplete, so
    # the direct-pane fallback below is guaranteed to refuse too.
    assert receipt["caller_pane_id"] == ""
    write_receipt(receipt_path("peer-codex", "orig-task"), receipt)

    backend_calls: list[str] = []
    monkeypatch.setattr(
        bridge,
        "get_backend_for_session",
        lambda *_a, **_k: backend_calls.append("get_backend_for_session") or (_ for _ in ()).throw(
            AssertionError("must never reach terminal-backend lookup once the direct address is incomplete")
        ),
    )
    monkeypatch.setattr(
        bridge,
        "_load_targets",
        lambda: (_ for _ in ()).throw(RuntimeError("ccb-list unavailable")),
    )

    rc = bridge.main(
        [
            "--target", str(tmp_path),
            "--provider", "claude",
            "--reply-to", "orig-task",
            "--intent", "wait",
            "The answer to your question",
        ]
    )

    assert rc == 1
    assert backend_calls == []  # NO TERMINAL SEND OCCURRED
    found = find_receipt("orig-task")
    assert found is not None
    assert read_peer_reply(found[1]) == "The answer to your question"


def test_completion_hook_delivers_when_caller_pane_still_confirms(
    monkeypatch, tmp_path: Path
) -> None:
    """Task 3, happy path: the caller pane still resolves to the exact live
    session the route recorded -- delivery proceeds, as it always has."""
    from pane_registry import upsert_registry

    hook = _load_completion_hook_module()
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path),
            "live_sessions": [
                {"live_id": "claude-1", "provider": "claude", "pane_id": "7", "terminal": "tmux"},
            ],
        }
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(hook, "send_via_tmux", lambda pane_id, message: sent.append(("tmux", pane_id)) or True)
    monkeypatch.setattr(hook, "send_via_wezterm", lambda *a, **k: sent.append(("wezterm", a[0])) or True)
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not fall back to ask --notify when a route is present")
    ))
    monkeypatch.setattr(hook.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        hook.sys,
        "argv",
        ["ccb-completion-hook", "--provider", "codex", "--caller", "claude", "--req-id", "task-1", "--reply", "Hello"],
    )
    monkeypatch.setenv("CCB_CALLER_PANE_ID", "7")
    monkeypatch.setenv("CCB_CALLER_TERMINAL", "tmux")
    monkeypatch.setenv("CCB_ROUTE_LAUNCH_ID", "ai-1")
    monkeypatch.setenv("CCB_CALLER_LIVE_ID", "claude-1")

    rc = hook.main()

    assert rc == 0
    assert sent == [("tmux", "7")]


def test_completion_hook_isolated_to_exact_codex_caller_in_three_member_inventory(
    monkeypatch, tmp_path: Path
) -> None:
    """A shared Claude responder must deliver each completion to its exact
    Codex caller when the launch contains two Codex sessions."""
    from pane_registry import upsert_registry

    hook = _load_completion_hook_module()
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path),
            "live_sessions": [
                {"live_id": "codex-a", "provider": "codex", "pane_id": "7", "terminal": "tmux"},
                {"live_id": "codex-b", "provider": "codex", "pane_id": "8", "terminal": "tmux"},
                {"live_id": "claude", "provider": "claude", "pane_id": "9", "terminal": "tmux"},
            ],
        }
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(hook, "send_via_tmux", lambda pane_id, message: sent.append(("tmux", pane_id)) or True)
    monkeypatch.setattr(hook.sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("CCB_ROUTE_LAUNCH_ID", "ai-1")
    monkeypatch.setenv("CCB_CALLER_TERMINAL", "tmux")

    for live_id, pane_id in (("codex-a", "7"), ("codex-b", "8")):
        monkeypatch.setenv("CCB_CALLER_LIVE_ID", live_id)
        monkeypatch.setenv("CCB_CALLER_PANE_ID", pane_id)
        monkeypatch.setattr(
            hook.sys,
            "argv",
            ["ccb-completion-hook", "--provider", "claude", "--caller", "codex",
             "--req-id", f"task-{live_id}", "--reply", "Hello"],
        )
        assert hook.main() == 0

    assert sent == [("tmux", "7"), ("tmux", "8")]


def test_completion_hook_delivers_for_todays_universal_legacy_registry_shape(
    monkeypatch, tmp_path: Path
) -> None:
    """Same happy path, but against the record shape every launch actually
    has today (`providers`, no `live_sessions` key at all). Task 3's new
    check must not suppress this common case -- see the near-miss this
    guards against in `pane_registry.confirm_caller_pane`."""
    from pane_registry import upsert_registry

    hook = _load_completion_hook_module()
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path),
            "providers": {"claude": {"pane_id": "7", "terminal": "tmux"}},
        }
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(hook, "send_via_tmux", lambda pane_id, message: sent.append(("tmux", pane_id)) or True)
    monkeypatch.setattr(hook.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        hook.sys,
        "argv",
        ["ccb-completion-hook", "--provider", "codex", "--caller", "claude", "--req-id", "task-1", "--reply", "Hello"],
    )
    monkeypatch.setenv("CCB_CALLER_PANE_ID", "7")
    monkeypatch.setenv("CCB_CALLER_TERMINAL", "tmux")
    monkeypatch.setenv("CCB_ROUTE_LAUNCH_ID", "ai-1")
    # No CCB_CALLER_LIVE_ID -- the legacy projection never hands one out.

    rc = hook.main()

    assert rc == 0
    assert sent == [("tmux", "7")]


def test_completion_hook_suppresses_delivery_when_pane_was_reused(
    monkeypatch, tmp_path: Path
) -> None:
    """Task 3: the pane recorded on the request has since been reused by a
    DIFFERENT live session. Completion must suppress the terminal push
    entirely -- never send to whoever is there now, and never fall back to
    hunting for somewhere else via `ask --notify`."""
    from pane_registry import upsert_registry

    hook = _load_completion_hook_module()
    assert upsert_registry(
        {
            "ccb_session_id": "ai-1",
            "work_dir": str(tmp_path),
            "live_sessions": [
                {"live_id": "new-owner", "provider": "codex", "pane_id": "7", "terminal": "tmux"},
            ],
        }
    )
    tmux_calls: list[tuple[str, str]] = []
    wezterm_calls: list[tuple] = []
    subprocess_calls: list[tuple] = []
    monkeypatch.setattr(hook, "send_via_tmux", lambda pane_id, message: tmux_calls.append(("tmux", pane_id)) or True)
    monkeypatch.setattr(hook, "send_via_wezterm", lambda *a, **k: wezterm_calls.append(a) or True)
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: subprocess_calls.append((a, k)) or (_ for _ in ()).throw(
        AssertionError("ask --notify fallback must never run when the caller could not be confirmed")
    ))
    monkeypatch.setattr(hook.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        hook.sys,
        "argv",
        ["ccb-completion-hook", "--provider", "codex", "--caller", "claude", "--req-id", "task-1", "--reply", "Hello"],
    )
    monkeypatch.setenv("CCB_CALLER_PANE_ID", "7")
    monkeypatch.setenv("CCB_CALLER_TERMINAL", "tmux")
    monkeypatch.setenv("CCB_ROUTE_LAUNCH_ID", "ai-1")
    monkeypatch.setenv("CCB_CALLER_LIVE_ID", "original-claude")

    rc = hook.main()

    assert rc == 0
    # NO TERMINAL SEND OCCURRED: neither backend was invoked, and no
    # fallback delivery path (ask --notify, which would itself re-run
    # provider selection) ran either.
    assert tmux_calls == []
    assert wezterm_calls == []
    assert subprocess_calls == []


def test_completion_hook_delivery_unaffected_when_no_route_was_ever_resolved(
    monkeypatch, tmp_path: Path
) -> None:
    """A request with no resolved route (legacy path, email/manual caller,
    no inventory) must behave exactly as before Task 3 existed: unconditional
    direct-pane delivery, no registry consulted at all."""
    hook = _load_completion_hook_module()
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(hook, "send_via_tmux", lambda pane_id, message: sent.append(("tmux", pane_id)) or True)
    monkeypatch.setattr(hook.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        hook.sys,
        "argv",
        ["ccb-completion-hook", "--provider", "codex", "--caller", "claude", "--req-id", "task-1", "--reply", "Hello"],
    )
    monkeypatch.setenv("CCB_CALLER_PANE_ID", "7")
    monkeypatch.setenv("CCB_CALLER_TERMINAL", "tmux")
    monkeypatch.delenv("CCB_ROUTE_LAUNCH_ID", raising=False)
    monkeypatch.delenv("CCB_CALLER_LIVE_ID", raising=False)

    rc = hook.main()

    assert rc == 0
    assert sent == [("tmux", "7")]


def test_completion_hook_suppresses_routed_request_with_no_caller_pane_at_all(
    monkeypatch,
) -> None:
    """Gap 5: a ROUTED request (`CCB_ROUTE_LAUNCH_ID` set) whose caller pane
    was never captured at all (empty `CCB_CALLER_PANE_ID`) is not "no
    evidence to check" -- it is a routed request that cannot be confirmed,
    and must suppress exactly like a reused pane would. The old
    `direct_pane_id and route_launch_id` gate skipped this case entirely
    and fell through to the legacy session-file lookup / `ask --notify`
    fallback, which is exactly the re-run-provider-selection behaviour
    Task 3 forbids."""
    hook = _load_completion_hook_module()
    tmux_calls: list[tuple] = []
    wezterm_calls: list[tuple] = []
    subprocess_calls: list[tuple] = []
    monkeypatch.setattr(hook, "send_via_tmux", lambda *a, **k: tmux_calls.append(a) or True)
    monkeypatch.setattr(hook, "send_via_wezterm", lambda *a, **k: wezterm_calls.append(a) or True)
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *a, **k: subprocess_calls.append((a, k))
        or (_ for _ in ()).throw(AssertionError("must never fall back to ask --notify")),
    )
    monkeypatch.setattr(hook.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        hook.sys,
        "argv",
        ["ccb-completion-hook", "--provider", "codex", "--caller", "claude", "--req-id", "task-1", "--reply", "Hello"],
    )
    monkeypatch.delenv("CCB_CALLER_PANE_ID", raising=False)
    monkeypatch.delenv("CCB_CALLER_TERMINAL", raising=False)
    monkeypatch.setenv("CCB_ROUTE_LAUNCH_ID", "ai-1")
    monkeypatch.setenv("CCB_CALLER_LIVE_ID", "claude-1")
    monkeypatch.setattr(hook, "find_ask_command", lambda: None)

    rc = hook.main()

    assert rc == 0
    # NO TERMINAL SEND OCCURRED, and no legacy fallback ran either.
    assert tmux_calls == []
    assert wezterm_calls == []
    assert subprocess_calls == []


def test_notify_completion_clears_stale_identity_env_before_next_request(
    monkeypatch,
) -> None:
    """Gap 5: `notify_completion` runs inside a long-lived daemon process
    that handles many requests over its lifetime. A prior request's
    identity env vars must never leak into a LATER request that has no
    opinion on them -- explicitly cleared, not merely overwritten only when
    truthy."""
    captured_envs: list[dict] = []

    class _FakeCompleted:
        returncode = 0
        stderr = b""

    def _fake_run(cmd, input=None, capture_output=None, timeout=None, env=None):
        captured_envs.append(dict(env or {}))
        return _FakeCompleted()

    monkeypatch.setattr(completion_hook.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        completion_hook.Path,
        "exists",
        lambda self: str(self).endswith("ccb-completion-hook"),
    )
    # Simulate the long-lived daemon PROCESS's own environment already
    # carrying identity values -- e.g. leftover from however it was
    # launched -- which `env = os.environ.copy()` would otherwise start
    # every subprocess invocation from.
    monkeypatch.setenv("CCB_ROUTE_LAUNCH_ID", "stale-launch-from-process-env")
    monkeypatch.setenv("CCB_CALLER_LIVE_ID", "stale-caller-from-process-env")
    monkeypatch.setenv("CCB_CALLER_PANE_ID", "stale-pane-from-process-env")

    # A request that has NO opinion on any of these (unrouted, no captured
    # caller pane) must not see the process environment's stale values.
    completion_hook.notify_completion(
        "codex", None, "reply", "task-1", True,
    )

    assert len(captured_envs) == 1
    assert "CCB_ROUTE_LAUNCH_ID" not in captured_envs[0]
    assert "CCB_CALLER_LIVE_ID" not in captured_envs[0]
    assert "CCB_CALLER_PANE_ID" not in captured_envs[0]


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


def test_sidecar_backed_oversized_reply_still_spills_through_prepare_agent_visible_reply(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """The sidecar-backed save path (Task 2/Item 5) must not bypass the
    existing oversized-reply handling -- proven directly, not claimed by
    inspection. A reply persisted via `persist_proven_result` that exceeds
    the inline byte limit, when read back through `_show_receipt`, must
    still spill to `completions/<task_id>.md` and print the
    `[CCB_RESULT_SPILLED]` banner, exactly as it would for any other
    oversized reply source."""
    pend = _load_pend_module()
    monkeypatch.setenv("CCB_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("CCB_COMPLETION_INLINE_MAX_BYTES", "64")

    log_file = tmp_path / "task.log"
    log_file.touch()
    task_id = "20260907-100000-001-9"
    data = new_receipt(
        task_id=task_id,
        provider="codex",
        caller="claude",
        work_dir=tmp_path,
        status_file=tmp_path / "task.status",
        log_file=log_file,
    )
    write_receipt(receipt_path("codex", task_id), data)

    oversized_reply = "x" * (1024 * 8)
    outcome = persist_proven_result(
        task_id, reply=oversized_reply, status=completion_hook.COMPLETION_STATUS_COMPLETED,
    )
    assert outcome == PersistOutcome.SAVED

    found = find_receipt(task_id)
    assert found is not None
    assert read_server_result(found[1]) == oversized_reply

    rc = pend._show_receipt(found[1])
    assert rc == pend.EXIT_OK
    output = capsys.readouterr().out
    assert "[CCB_RESULT_SPILLED]" in output
    assert oversized_reply not in output
    spilled_path = tmp_path / "run" / "completions" / f"{task_id}.md"
    assert spilled_path.read_text(encoding="utf-8") == oversized_reply


def test_persist_proven_result_writes_before_any_notification_could_run(tmp_path: Path) -> None:
    """Task 2 (rejected round-1 claim, now fixed): the adapter calls this
    BEFORE any notification is attempted. Verify the mechanism itself: the
    reply lands in a dedicated result file (never the shared `log_file`,
    which the client-side stdout capture still exclusively owns), and the
    proven transcript identity (Gap 1) is merged onto the receipt."""
    log_file = tmp_path / "task.log"
    log_file.touch()
    data = new_receipt(
        task_id="task-2-order",
        provider="codex",
        caller="claude",
        work_dir=tmp_path,
        status_file=tmp_path / "task.status",
        log_file=log_file,
    )
    write_receipt(receipt_path("codex", "task-2-order"), data)

    outcome = persist_proven_result(
        "task-2-order",
        reply="The proven answer",
        status=completion_hook.COMPLETION_STATUS_COMPLETED,
        transcript_path="/proven/transcript.jsonl",
        conversation_id="proven-session",
    )

    assert outcome == PersistOutcome.SAVED
    found = find_receipt("task-2-order")
    assert found is not None
    assert found[1]["destination_transcript_path"] == "/proven/transcript.jsonl"
    assert found[1]["destination_conversation_id"] == "proven-session"
    assert read_server_result(found[1]) == "The proven answer"
    assert read_server_result_status(found[1]) == completion_hook.COMPLETION_STATUS_COMPLETED
    result_path = server_result_path(found[1])
    assert result_path is not None
    assert result_path != Path(found[1]["log_file"])
    # log_file itself is untouched -- still exactly what bin/ask touch-
    # created it as, never written to by this mechanism.
    assert Path(found[1]["log_file"]).read_text(encoding="utf-8") == ""


def test_persist_proven_result_is_a_no_op_without_a_matching_receipt() -> None:
    """A foreground/notify-only call never created a receipt -- this must
    not raise, and must report NO_RECEIPT, never FAILED (that is reserved
    for a receipt that DID exist and could not be saved -- see Item 5)."""
    assert persist_proven_result("no-such-task", reply="anything") == PersistOutcome.NO_RECEIPT


def test_persist_proven_result_reports_failed_not_no_receipt_for_empty_body(
    tmp_path: Path,
) -> None:
    """Correction 2: once a receipt IS found, an empty reply body must
    report FAILED, never NO_RECEIPT -- NO_RECEIPT means "nothing was ever
    expected to be saved here," which stops being true the moment a
    receipt exists. Reporting NO_RECEIPT for this case would let the
    adapter notify normally (see `test_handle_task_suppresses_
    notification_when_result_could_not_be_saved` in test_codex_reply_
    phase.py, which proves a FAILED outcome suppresses notification for
    any cause) -- defeating save-before-notify for exactly the case where
    nothing was saved."""
    log_file = tmp_path / "task.log"
    log_file.touch()
    data = new_receipt(
        task_id="task-empty-body",
        provider="codex",
        caller="claude",
        work_dir=tmp_path,
        status_file=tmp_path / "task.status",
        log_file=log_file,
    )
    write_receipt(receipt_path("codex", "task-empty-body"), data)

    outcome = persist_proven_result(
        "task-empty-body", reply="", status=completion_hook.COMPLETION_STATUS_INCOMPLETE,
    )

    assert outcome == PersistOutcome.FAILED
    found = find_receipt("task-empty-body")
    assert found is not None
    assert read_server_result(found[1]) == ""


class _FakeClaudeBackend:
    def is_alive(self, pane_id: str) -> bool:
        return True


class _ScriptedClaudeReader:
    """Yields a fixed sequence of (role, text) events, one call per event."""

    def __init__(self, events: list[tuple[str, str]]) -> None:
        self._events = list(events)

    def wait_for_events(self, state: dict, timeout: float):
        if self._events:
            return [self._events.pop(0)], state
        return [], state


def test_claude_wait_for_response_records_transcript_proof_at_anchor_even_on_timeout(
    tmp_path: Path,
) -> None:
    """Item 2, the specific bug named for Claude: "Claude only supplies a
    log path when the completion marker was seen" -- so a request that
    times out AFTER its anchor was confirmed (no CCB_DONE ever arrives)
    used to lose proof it already had. Exercised directly against the
    real `_wait_for_response`, not a reimplementation."""
    import threading
    import time as _time

    from askd.adapters.base import ProviderRequest, QueuedTask
    from askd.adapters.claude import ClaudeAdapter
    from ccb_protocol import REQ_ID_PREFIX

    req_id = "20260907-100000-001-1"
    session_path = tmp_path / "claude-transcript.jsonl"
    session_path.write_text("", encoding="utf-8")

    req = ProviderRequest(
        client_id="c", work_dir=str(tmp_path), timeout_s=0.05, quiet=True,
        message="hi", caller="claude", req_id=req_id,
    )
    task = QueuedTask(request=req, created_ms=0, req_id=req_id, done_event=threading.Event())
    reader = _ScriptedClaudeReader([("user", f"{REQ_ID_PREFIX} {req_id}")])
    state = {"session_path": session_path}

    result = ClaudeAdapter()._wait_for_response(
        task, None, "session-key", 0, reader, state, _FakeClaudeBackend(), "pane-1",
        deadline=_time.time() + 0.05,
    )

    assert result.anchor_seen is True
    assert result.done_seen is False
    assert result.log_path == str(session_path)


def test_claude_wait_for_response_records_no_proof_when_unanchored(tmp_path: Path) -> None:
    """The other half: no anchor ever confirmed means no proof recorded."""
    import threading
    import time as _time

    from askd.adapters.base import ProviderRequest, QueuedTask
    from askd.adapters.claude import ClaudeAdapter

    req_id = "20260907-100000-001-2"
    session_path = tmp_path / "claude-transcript.jsonl"
    session_path.write_text("", encoding="utf-8")

    req = ProviderRequest(
        client_id="c", work_dir=str(tmp_path), timeout_s=0.05, quiet=True,
        message="hi", caller="claude", req_id=req_id,
    )
    task = QueuedTask(request=req, created_ms=0, req_id=req_id, done_event=threading.Event())
    reader = _ScriptedClaudeReader([("assistant", "Wrong pane output")])
    state = {"session_path": session_path}

    class _MinimalSession:
        work_dir = str(tmp_path)

    result = ClaudeAdapter()._wait_for_response(
        task, _MinimalSession(), "session-key", 0, reader, state, _FakeClaudeBackend(), "pane-1",
        deadline=_time.time() + 0.05,
    )

    assert result.anchor_seen is False
    assert result.done_seen is False
    assert result.log_path is None


def test_receipt_records_exact_route_identity_additively(tmp_path: Path, monkeypatch) -> None:
    """Task 1: a routed ask's receipt carries the exact destination and
    caller live-session identity, their shared launch, and the destination's
    own proven conversation path -- additively, alongside every field a
    receipt has always carried."""
    monkeypatch.setenv("CCB_SESSION_ID", "ccb-1")
    monkeypatch.setenv("TMUX_PANE", "%7")
    route = ResolvedRoute(
        live_id="codex-live-2",
        launch_id="ai-100-1",
        caller_live_id="claude-live-1",
        pane_id="%9",
        terminal="tmux",
        session_file="/tmp/codex-live-2.json",
        ccb_project_id="proj-abc",
    )

    data = new_receipt(
        task_id="20260901-120000-001-1",
        provider="codex",
        caller="claude",
        work_dir=tmp_path,
        status_file=tmp_path / "task.status",
        log_file=tmp_path / "task.log",
        route=route,
    )

    assert data["route_launch_id"] == "ai-100-1"
    assert data["destination_live_id"] == "codex-live-2"
    assert data["destination_pane_id"] == "%9"
    assert data["destination_terminal"] == "tmux"
    assert data["destination_session_file"] == "/tmp/codex-live-2.json"
    assert data["destination_ccb_project_id"] == "proj-abc"
    assert data["caller_live_id"] == "claude-live-1"
    # Every field a receipt carried before this change is still there,
    # unchanged.
    assert data["caller_session_id"] == "ccb-1"
    assert data["caller_pane_id"] == "%7"


def test_receipt_without_route_has_no_identity_fields(tmp_path: Path) -> None:
    """A request with no resolved route (legacy path, email/manual caller,
    no inventory) must produce a receipt byte-identical in shape to one
    created before Task 1 -- no identity keys at all, not even empty ones,
    and no identity is ever inferred for it."""
    data = new_receipt(
        task_id="20260901-120000-001-2",
        provider="codex",
        caller="claude",
        work_dir=tmp_path,
        status_file=tmp_path / "task.status",
        log_file=tmp_path / "task.log",
    )

    for key in (
        "route_launch_id",
        "destination_live_id",
        "destination_pane_id",
        "destination_terminal",
        "destination_session_file",
        "destination_ccb_project_id",
        "caller_live_id",
    ):
        assert key not in data

    # An empty (not-present) route must behave exactly the same as no route.
    data_absent_route = new_receipt(
        task_id="20260901-120000-001-3",
        provider="codex",
        caller="claude",
        work_dir=tmp_path,
        status_file=tmp_path / "task.status",
        log_file=tmp_path / "task.log",
        route=ResolvedRoute(),
    )
    assert set(data.keys()) == set(data_absent_route.keys())


def test_peer_receipt_uses_exact_inventory_member(tmp_path: Path, monkeypatch) -> None:
    from live_sessions import LiveSession

    monkeypatch.setenv("CCB_CALLER_PANE_ID", "%8")
    monkeypatch.setenv("CCB_CALLER_TERMINAL", "tmux")
    entries = [({}, LiveSession(
        live_id=f"s{n}", provider="codex", launch_id="launch",
        pane_id=f"%{n}", terminal="tmux", pane_title_marker=f"marker-{n}",
    )) for n in (7, 8)]
    monkeypatch.setattr("peer_routing._live_entries_for_project", lambda *_: (entries, True))
    monkeypatch.setattr("pane_registry.load_registry_by_project_id", lambda *_: (_ for _ in ()).throw(
        AssertionError("inventory must not fall back to provider lookup")))
    kwargs = dict(task_id="task", peer_provider="claude", caller="codex", intent="background",
                  work_dir=tmp_path, status_file=tmp_path / "task.status",
                  log_file=tmp_path / "task.log", reply_file=tmp_path / "task.reply")
    data = new_peer_receipt(**kwargs)
    assert data["caller_live_id"] == "s8"
    assert data["caller_registry_session_id"] == "launch"
    assert data["caller_pane_title_marker"] == "marker-8"
    entries.clear()
    data = new_peer_receipt(**kwargs)
    assert not data.get("caller_pane_title_marker")
    assert not data.get("caller_live_id")


def test_peer_receipt_explicit_caller_identity_overrides_ambient_pane(
    tmp_path: Path, monkeypatch
) -> None:
    from live_sessions import LiveSession

    monkeypatch.setenv("CCB_CALLER_PANE_ID", "%7")
    monkeypatch.setenv("CCB_CALLER_TERMINAL", "tmux")
    entries = [({}, LiveSession(
        live_id=f"s{n}", provider="codex", launch_id="launch",
        pane_id=f"%{n}", terminal="tmux", pane_title_marker=f"marker-{n}",
    )) for n in (7, 8)]
    monkeypatch.setattr("peer_routing._live_entries_for_project", lambda *_: (entries, True))

    data = new_peer_receipt(
        task_id="task",
        peer_provider="claude",
        caller="codex",
        intent="wait",
        work_dir=tmp_path,
        status_file=tmp_path / "task.status",
        log_file=tmp_path / "task.log",
        reply_file=tmp_path / "task.reply",
        caller_pane_id="%8",
        caller_terminal="tmux",
    )

    assert data["caller_pane_id"] == "%8"
    assert data["caller_live_id"] == "s8"
    assert data["caller_pane_title_marker"] == "marker-8"


def test_peer_receipt_keeps_host_validated_identity_when_local_inventory_is_unreadable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "peer_routing._live_entries_for_project",
        lambda *_args: (_ for _ in ()).throw(PermissionError("sandbox")),
    )

    data = new_peer_receipt(
        task_id="task",
        peer_provider="claude",
        caller="codex",
        intent="wait",
        work_dir=tmp_path,
        status_file=tmp_path / "task.status",
        log_file=tmp_path / "task.log",
        reply_file=tmp_path / "task.reply",
        caller_pane_id="30",
        caller_terminal="wezterm",
        caller_live_id="sender-live-id",
        caller_registry_session_id="launch",
        caller_pane_title_marker="sender-marker",
    )

    assert data["caller_pane_id"] == "30"
    assert data["caller_live_id"] == "sender-live-id"
    assert data["caller_registry_session_id"] == "launch"
    assert data["caller_pane_title_marker"] == "sender-marker"


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


def test_recover_reads_the_exact_recorded_codex_conversation_not_the_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    """Task 4/Gap 1: retrieval reads the task's own conversation from the
    adapter's OWN proof (`destination_transcript_path`), never "whatever
    session is current now" for that provider. `destination_session_file`
    -- the MUTABLE per-provider BINDING file (what a real `.codex-session`
    actually holds: `codex_session_path`/`codex_session_id`, not the
    transcript itself) -- is deliberately given a REAL binding-file shape
    here, pointing at neither the original nor the replacement transcript,
    so a test that accidentally reads it can never pass by coincidence. A
    pane that has since moved on to a newer, unrelated conversation must
    not leak into the old answer."""
    pend = _load_pend_module()
    req_id = "20260901-100000-001-1"
    original = tmp_path / "rollout-original.jsonl"
    replacement = tmp_path / "rollout-replacement.jsonl"
    binding_file = tmp_path / ".codex-session"
    original.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {"type": "session_meta", "payload": {"cwd": str(tmp_path)}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": f"CCB_REQ_ID: {req_id}\n\ntask"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": f"Original reply.\nCCB_DONE: {req_id}"}},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    replacement.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {"type": "session_meta", "payload": {"cwd": str(tmp_path)}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "Unrelated question"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "Unrelated answer"}},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # A real `.codex-session` binding file shape: it POINTS AT a transcript
    # (here, the newer replacement one -- exactly what a later ask in the
    # same pane would have repointed it to) rather than BEING one. If
    # recovery ever fed this straight to a log reader as if it were the
    # transcript, parsing it would find no exchanges at all here.
    binding_file.write_text(
        json.dumps({"codex_session_path": str(replacement), "codex_session_id": "replacement-session"}),
        encoding="utf-8",
    )
    old_time = time.time() - 100
    os.utime(original, (old_time, old_time))
    # The replacement conversation is newer -- if recovery ever fell back to
    # "whatever is current", this is what it would wrongly pick up.
    monkeypatch.delenv("CODEX_SESSION_ROOT", raising=False)

    receipt = {
        "task_id": req_id,
        "provider": "codex",
        "work_dir": str(tmp_path),
        "route_launch_id": "ai-1",
        "destination_session_file": str(binding_file),
        "destination_transcript_path": str(original),
        "destination_conversation_id": "original-session",
    }

    assert pend._recover_from_recorded_conversation(receipt, "codex") == "Original reply."
    assert pend._recover_provider_reply(receipt) == "Original reply."


def test_recover_reads_the_exact_recorded_claude_conversation(tmp_path: Path) -> None:
    """Same as above, but for Claude -- which had NO recovery fallback at
    all before Task 4."""
    pend = _load_pend_module()
    req_id = "20260901-100000-001-2"
    session = tmp_path / "claude-session.jsonl"
    binding_file = tmp_path / ".claude-session"
    session.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": f"CCB_REQ_ID: {req_id}\nQuestion"}],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"Claude reply.\nCCB_DONE: {req_id}"}],
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # A real `.claude-session` binding-file shape -- points at a claude
    # transcript path field, not a transcript itself, and here doesn't even
    # name a real file, to prove recovery never reads through it.
    binding_file.write_text(
        json.dumps({"claude_session_path": str(tmp_path / "unrelated.jsonl"), "claude_session_id": "unrelated"}),
        encoding="utf-8",
    )
    receipt = {
        "task_id": req_id,
        "provider": "claude",
        "work_dir": str(tmp_path),
        "route_launch_id": "ai-1",
        "destination_session_file": str(binding_file),
        "destination_transcript_path": str(session),
        "destination_conversation_id": "claude-session",
    }

    assert pend._recover_from_recorded_conversation(receipt, "claude") == "Claude reply."
    assert pend._recover_provider_reply(receipt) == "Claude reply."


def test_recover_from_recorded_conversation_is_a_pure_no_op_for_old_receipts(
    tmp_path: Path,
) -> None:
    """An old receipt that predates Task 4/Gap 1's `destination_transcript_
    path` field must not have identity guessed for it -- the new lookup
    returns `None` cleanly so `_recover_provider_reply` falls through to
    the existing legacy behaviour, unchanged (this receipt also carries no
    `route_launch_id`, so it is unrouted -- the fallback applies)."""
    pend = _load_pend_module()
    receipt = {"task_id": "20260901-100000-001-3", "provider": "codex", "work_dir": str(tmp_path)}

    assert pend._recover_from_recorded_conversation(receipt, "codex") is None
    assert pend._recover_from_recorded_conversation(receipt, "claude") is None


def test_timeout_placeholder_never_reads_as_done_and_a_later_exact_reply_supersedes_it(
    tmp_path: Path,
) -> None:
    """Item 1: a task that times out persists an INCOMPLETE placeholder
    (exactly what `default_reply_for_status` produces) -- that placeholder
    must not read as a finished answer, and must not block a later,
    genuinely completed reply from superseding it once the underlying
    process actually finishes and writes CCB_DONE to its transcript (Item
    2 is what makes this possible at all: transcript proof was captured at
    anchor time, even though this request timed out before done_seen)."""
    pend = _load_pend_module()
    req_id = "20260907-100000-001-1"
    transcript = tmp_path / "rollout.jsonl"
    status = tmp_path / "task.status"
    log_file = tmp_path / "task.log"
    log_file.touch()
    status.write_text("running pid=12345\n", encoding="utf-8")

    receipt = new_receipt(
        task_id=req_id,
        provider="codex",
        caller="claude",
        work_dir=tmp_path,
        status_file=status,
        log_file=log_file,
        # Routed (Task 1), so `_recover_provider_reply` uses the EXACT
        # proven-transcript path (Gap 2) rather than a legacy scan.
        route=ResolvedRoute(
            live_id="codex-1", launch_id="ai-1", caller_live_id="claude-1",
            pane_id="1", terminal="tmux", session_file="/binding.json", ccb_project_id="proj",
        ),
    )
    write_receipt(receipt_path("codex", req_id), receipt)

    # 1. The adapter's timeout path: anchor was confirmed (transcript_path
    #    recorded per Item 2) but CCB_DONE never arrived -- a placeholder
    #    is persisted with status=incomplete.
    transcript.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {"type": "session_meta", "payload": {"cwd": str(tmp_path)}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": f"CCB_REQ_ID: {req_id}\n\ntask"}},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    outcome = persist_proven_result(
        req_id,
        reply="Task ended without a confirmed completion marker.",
        status=completion_hook.COMPLETION_STATUS_INCOMPLETE,
        transcript_path=str(transcript),
        conversation_id="proven-session",
    )
    assert outcome == PersistOutcome.SAVED

    found = find_receipt(req_id)
    assert found is not None
    # The placeholder must NOT read as a finished answer.
    assert read_server_result(found[1]) == ""
    assert read_server_result_status(found[1]) == completion_hook.COMPLETION_STATUS_INCOMPLETE

    # 2. Time passes; the underlying Codex process was actually still
    #    running and eventually writes the real CCB_DONE to the SAME
    #    transcript Item 2 already proved belongs to this request.
    transcript.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {"type": "session_meta", "payload": {"cwd": str(tmp_path)}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": f"CCB_REQ_ID: {req_id}\n\ntask"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": f"The real answer.\nCCB_DONE: {req_id}"}},
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rc = pend._show_receipt(found[1])

    assert rc == pend.EXIT_OK


def test_routed_receipt_never_widens_to_project_wide_scan_when_proof_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """Gap 2: a routed task (carries `route_launch_id`) whose proven
    transcript is missing, unreadable, or not yet written must report
    unavailable -- it must NEVER fall through to `_recover_codex_reply`'s
    project-wide, work_dir-scoped marker scan, even when that scan would
    have found a matching completed exchange somewhere else in the same
    work_dir. Searching elsewhere is exactly the "I could not read the
    proven conversation" -> "search other conversations" degradation this
    guards against."""
    pend = _load_pend_module()
    req_id = "20260901-100000-001-9"
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    # A completed exchange for this exact req_id DOES exist somewhere the
    # legacy project-wide scan would find it -- proving a wrong answer is
    # available to fall back to, and that the fix deliberately refuses it.
    other_transcript = session_root / "unrelated-rollout.jsonl"
    other_transcript.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {"type": "session_meta", "payload": {"cwd": str(tmp_path)}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": f"CCB_REQ_ID: {req_id}\n\ntask"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": f"Found elsewhere.\nCCB_DONE: {req_id}"}},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_SESSION_ROOT", str(session_root))

    receipt = {
        "task_id": req_id,
        "provider": "codex",
        "work_dir": str(tmp_path),
        "route_launch_id": "ai-1",
        # No destination_transcript_path recorded -- the adapter never
        # confirmed one (crashed first, or is still running).
    }

    assert pend._recover_from_recorded_conversation(receipt, "codex") is None
    assert pend._recover_provider_reply(receipt) is None


def test_unrouted_receipt_still_uses_the_legacy_project_wide_scan(
    tmp_path: Path, monkeypatch
) -> None:
    """The inverse of the test above: a receipt with NO routed identity at
    all (today's universal legacy case, or one predating Task 1) keeps
    using the old project-wide scan exactly as it always has -- Gap 2 only
    narrows the ROUTED path."""
    pend = _load_pend_module()
    req_id = "20260901-100000-001-10"
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    rollout = session_root / "rollout.jsonl"
    rollout.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {"type": "session_meta", "payload": {"cwd": str(tmp_path)}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": f"CCB_REQ_ID: {req_id}\n\ntask"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": f"Legacy found.\nCCB_DONE: {req_id}"}},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_SESSION_ROOT", str(session_root))

    receipt = {"task_id": req_id, "provider": "codex", "work_dir": str(tmp_path)}

    assert pend._recover_provider_reply(receipt) == "Legacy found."


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


# --- Task 5: retrieval selection under a pair. All of these are gated on a
# REAL, valid `live_sessions` inventory (`_pair_scope`) -- a record with no
# inventory (today's universal case) is a pure no-op for every branch this
# section exercises, which is exactly what the existing tests above already
# prove by continuing to pass unmodified. ---


def _pair_record(project: str, session: str, sessions: list[dict]) -> dict:
    return {
        "ccb_project_id": project,
        "ccb_session_id": session,
        "work_dir": "/pair-launch",
        "live_sessions": [
            {
                "live_id": entry["live_id"],
                "provider": entry["provider"],
                "pane_id": entry["pane_id"],
                "terminal": entry.get("terminal", "tmux"),
            }
            for entry in sessions
        ],
    }


def _bind_pair(
    pend,
    monkeypatch,
    *,
    project: str,
    session: str,
    current_provider: str,
    executing_pane: tuple[str, str],
    sessions: list[dict],
) -> None:
    monkeypatch.setattr(pend, "_current_session_context", lambda: (project, session, current_provider))
    monkeypatch.setattr(
        pend, "_current_tab_registry", lambda p, s: _pair_record(project, session, sessions)
    )
    monkeypatch.setattr(pend, "_executing_pane", lambda: executing_pane)


def _identity_receipt(
    task_id: str,
    provider: str,
    *,
    session: str,
    project: str,
    submitted: str,
    caller_live_id: str,
    destination_live_id: str,
) -> dict:
    receipt = _receipt(task_id, provider, session=session, pane="ignored", project=project, submitted=submitted)
    receipt["caller_live_id"] = caller_live_id
    receipt["destination_live_id"] = destination_live_id
    return receipt


_TWO_CODEX_SESSIONS = [
    {"live_id": "codex-A", "provider": "codex", "pane_id": "10"},
    {"live_id": "codex-B", "provider": "codex", "pane_id": "11"},
]


def test_pair_scope_is_a_no_op_without_a_valid_inventory(monkeypatch) -> None:
    """Gap 3(a): a record with no `live_sessions` key at all (ABSENT) is a
    pure no-op -- distinct from a present-but-broken one (INVALID), which
    must refuse instead (see the test below)."""
    pend = _load_pend_module()
    monkeypatch.setattr(pend, "_current_tab_registry", lambda p, s: {"ccb_project_id": p, "ccb_session_id": s})

    assert pend._pair_scope("p", "ai-1") == ([], "", False, False)


def test_pair_scope_flags_a_present_but_invalid_inventory_for_refusal(monkeypatch) -> None:
    """Gap 3(a): a `live_sessions` key that IS present but malformed must
    not be collapsed into the same no-op as an absent one -- implicit
    retrieval has to refuse rather than silently behave as legacy."""
    pend = _load_pend_module()
    monkeypatch.setattr(
        pend,
        "_current_tab_registry",
        lambda p, s: {"ccb_project_id": p, "ccb_session_id": s, "live_sessions": "not-a-list"},
    )

    assert pend._pair_scope("p", "ai-1") == ([], "", True, True)


def test_pair_scope_distinguishes_valid_empty_inventory_from_absent(monkeypatch) -> None:
    """Item 3: a real, valid `live_sessions: []` is authoritative -- it
    must report `present=True` even though `sessions` is empty, distinct
    from a record with no `live_sessions` key at all."""
    pend = _load_pend_module()
    monkeypatch.setattr(
        pend,
        "_current_tab_registry",
        lambda p, s: {"ccb_project_id": p, "ccb_session_id": s, "live_sessions": []},
    )

    assert pend._pair_scope("p", "ai-1") == ([], "", False, True)


def test_overlay_never_reads_manual_history_from_the_wrong_pane_under_a_pair(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Gap 4: `pend codex` as codex-A, with two codex sessions in the
    launch, must never append manual transcript history from EITHER pane
    -- the receipts (already filtered by destination identity) are shown
    on their own. Proven two ways: `provider_log_reader` is tracked and
    must never be called at all, and distinct manual text unique to each
    pane must never appear in the output."""
    pend = _load_pend_module()
    _bind_pair(
        pend, monkeypatch,
        project="p", session="ai-1", current_provider="codex",
        executing_pane=("10", "tmux"), sessions=_TWO_CODEX_SESSIONS,
    )
    status = tmp_path / "from-a.status"
    log = tmp_path / "from-a.log"
    status.write_text("finished exit_code=0\n", encoding="utf-8")
    log.write_text("Real reply from A to B\n", encoding="utf-8")
    from_a = _identity_receipt(
        "from-a", "codex", session="ai-1", project="p", submitted="1",
        caller_live_id="codex-A", destination_live_id="codex-B",
    )
    from_a.update({"status_file": str(status), "log_file": str(log)})
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("from-a.json"), from_a)])

    reader_calls: list[tuple] = []

    class _LeakyReader:
        def latest_exchanges(self, _n: int) -> list[dict]:
            return [
                {"ts": "2026-09-07T10:00:00Z", "req_id": None, "question": "q", "reply": "PANE_A_SECRET_MANUAL_TEXT"},
                {"ts": "2026-09-07T10:00:01Z", "req_id": None, "question": "q", "reply": "PANE_B_SECRET_MANUAL_TEXT"},
            ]

    def _tracking_reader(*args, **kwargs):
        reader_calls.append((args, kwargs))
        return _LeakyReader()

    monkeypatch.setattr(pend, "provider_log_reader", _tracking_reader)

    rc = pend.main(["pend", "codex"])

    assert rc == pend.EXIT_OK
    output = capsys.readouterr().out
    assert "PANE_A_SECRET_MANUAL_TEXT" not in output
    assert "PANE_B_SECRET_MANUAL_TEXT" not in output
    assert "Real reply from A to B" in output
    assert reader_calls == []


def test_overlay_keeps_manual_history_for_an_ordinary_mixed_provider_peer(
    monkeypatch, capsys,
) -> None:
    """Item 4 (regression introduced by Gap 4's own fix): `pend peer` in
    the ORDINARY mixed-provider shape (exactly one codex + one claude --
    every real launch today) resolves an EXACT `required_destination_live_
    id` even though there is only ONE session of the responder's own
    provider. That is exact selection, not duplicate-provider ambiguity,
    and must NOT suppress the manual-history overlay -- a unique provider
    keeps its existing overlay behaviour exactly."""
    pend = _load_pend_module()
    mixed = [
        {"live_id": "codex-A", "provider": "codex", "pane_id": "10"},
        {"live_id": "claude-B", "provider": "claude", "pane_id": "7"},
    ]
    _bind_pair(
        pend, monkeypatch,
        project="p", session="ai-1", current_provider="codex",
        executing_pane=("10", "tmux"), sessions=mixed,
    )
    monkeypatch.setattr(pend, "iter_receipts", lambda: [])

    class _NormalReader:
        def latest_exchanges(self, _n: int) -> list[dict]:
            return [
                {"ts": "2026-09-07T10:00:00Z", "req_id": None, "question": "q", "reply": "Manual claude reply"},
            ]

    reader_calls: list[tuple] = []

    def _tracking_reader(*args, **kwargs):
        reader_calls.append((args, kwargs))
        return _NormalReader()

    monkeypatch.setattr(pend, "provider_log_reader", _tracking_reader)

    rc = pend.main(["pend", "peer"])

    assert rc == pend.EXIT_OK
    assert capsys.readouterr().out.strip() == "Manual claude reply"
    assert len(reader_calls) == 1


def test_resolve_retrieval_target_selects_other_codex_session_by_provider_name() -> None:
    pend = _load_pend_module()
    from live_sessions import live_sessions_from_record

    sessions = live_sessions_from_record(_pair_record("p", "ai-1", _TWO_CODEX_SESSIONS))

    responder, req_caller, req_dest, error = pend._resolve_retrieval_target(
        "codex", "codex", sessions, "codex-A", True
    )

    assert error == ""
    assert responder == "codex"
    assert req_caller == ""
    assert req_dest == "codex-B"


def test_resolve_retrieval_target_local_selects_callers_own_session() -> None:
    pend = _load_pend_module()
    from live_sessions import live_sessions_from_record

    sessions = live_sessions_from_record(_pair_record("p", "ai-1", _TWO_CODEX_SESSIONS))

    responder, req_caller, req_dest, error = pend._resolve_retrieval_target(
        "local", "codex", sessions, "codex-A", True
    )

    assert error == ""
    assert responder == "codex"
    assert req_dest == "codex-A"


def test_resolve_retrieval_target_peer_selects_sole_other_session_regardless_of_provider() -> None:
    """'peer' selects the sole other session in a two-session launch --
    exercised here with two DIFFERENT providers (codex + claude), which is
    every real launch's actual shape today, to prove the new identity-based
    rule reproduces exactly what the old static claude<->codex mapping gave
    for that common case."""
    pend = _load_pend_module()
    from live_sessions import live_sessions_from_record

    mixed = [
        {"live_id": "codex-A", "provider": "codex", "pane_id": "10"},
        {"live_id": "claude-B", "provider": "claude", "pane_id": "7"},
    ]
    sessions = live_sessions_from_record(_pair_record("p", "ai-1", mixed))

    responder, req_caller, req_dest, error = pend._resolve_retrieval_target(
        "peer", "codex", sessions, "codex-A", True
    )

    assert error == ""
    assert responder == "claude"
    assert req_dest == "claude-B"


def test_resolve_retrieval_target_refuses_when_more_than_two_sessions_of_provider() -> None:
    pend = _load_pend_module()
    from live_sessions import live_sessions_from_record

    three_codex = _TWO_CODEX_SESSIONS + [{"live_id": "codex-C", "provider": "codex", "pane_id": "12"}]
    sessions = live_sessions_from_record(_pair_record("p", "ai-1", three_codex))

    responder, _req_caller, _req_dest, error = pend._resolve_retrieval_target(
        "codex", "codex", sessions, "codex-A", True
    )

    assert responder == ""
    assert error != ""


def test_resolve_retrieval_target_refuses_when_current_identity_unknown() -> None:
    pend = _load_pend_module()
    from live_sessions import live_sessions_from_record

    sessions = live_sessions_from_record(_pair_record("p", "ai-1", _TWO_CODEX_SESSIONS))

    responder, _req_caller, _req_dest, error = pend._resolve_retrieval_target(
        "codex", "codex", sessions, "", True
    )

    assert responder == ""
    assert error != ""


def test_current_session_receipts_identity_filters_never_merge_crossed_exchanges() -> None:
    """Task 5's core correctness property: A asking B and B asking A, sharing
    one launch session id, must never merge into one history."""
    pend = _load_pend_module()
    from_a = _identity_receipt(
        "from-a", "codex", session="ai-1", project="p", submitted="2",
        caller_live_id="codex-A", destination_live_id="codex-B",
    )
    from_b = _identity_receipt(
        "from-b", "codex", session="ai-1", project="p", submitted="1",
        caller_live_id="codex-B", destination_live_id="codex-A",
    )
    records = [(Path("a.json"), from_a), (Path("b.json"), from_b)]
    pend.__dict__["iter_receipts"] = lambda: records

    as_a = pend._current_session_receipts("p", "ai-1", "codex", destination_live_id="codex-B")
    as_b = pend._current_session_receipts("p", "ai-1", "codex", destination_live_id="codex-A")

    assert [d["task_id"] for _p, d in as_a] == ["from-a"]
    assert [d["task_id"] for _p, d in as_b] == ["from-b"]


def test_three_member_exact_task_retrieval_keeps_codex_callers_isolated() -> None:
    pend = _load_pend_module()
    from_a = _identity_receipt(
        "from-a", "claude", session="ai-1", project="p", submitted="2",
        caller_live_id="codex-A", destination_live_id="claude-C",
    )
    from_b = _identity_receipt(
        "from-b", "claude", session="ai-1", project="p", submitted="1",
        caller_live_id="codex-B", destination_live_id="claude-C",
    )
    records = [(Path("a.json"), from_a), (Path("b.json"), from_b)]
    pend.iter_receipts = lambda: records

    as_a = pend._current_session_receipts(
        "p", "ai-1", "claude", caller_live_id="codex-A", destination_live_id="claude-C"
    )
    as_b = pend._current_session_receipts(
        "p", "ai-1", "claude", caller_live_id="codex-B", destination_live_id="claude-C"
    )

    assert [d["task_id"] for _p, d in as_a] == ["from-a"]
    assert [d["task_id"] for _p, d in as_b] == ["from-b"]


def test_current_session_receipts_excludes_old_receipts_without_identity_under_ambiguity() -> None:
    """An old receipt predating Task 1's identity fields must never be
    guessed into a disambiguated result -- it is excluded, not attributed."""
    pend = _load_pend_module()
    legacy = _receipt("legacy", "codex", session="ai-1", pane="10", project="p", submitted="1")
    records = [(Path("legacy.json"), legacy)]
    pend.iter_receipts = lambda: records

    found = pend._current_session_receipts("p", "ai-1", "codex", destination_live_id="codex-B")

    assert found == []


def test_pend_bare_retrieval_scopes_to_callers_own_identity_under_a_pair(monkeypatch) -> None:
    pend = _load_pend_module()
    _bind_pair(
        pend, monkeypatch,
        project="p", session="ai-1", current_provider="codex",
        executing_pane=("10", "tmux"), sessions=_TWO_CODEX_SESSIONS,
    )
    from_a = _identity_receipt(
        "from-a", "codex", session="ai-1", project="p", submitted="2",
        caller_live_id="codex-A", destination_live_id="codex-B",
    )
    from_b = _identity_receipt(
        "from-b", "codex", session="ai-1", project="p", submitted="1",
        caller_live_id="codex-B", destination_live_id="codex-A",
    )
    from_a.update({"status_file": "/nonexistent-a.status", "log_file": "/nonexistent-a.log"})
    from_b.update({"status_file": "/nonexistent-b.status", "log_file": "/nonexistent-b.log"})
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("a.json"), from_a), (Path("b.json"), from_b)])

    rc = pend.main(["pend"])

    # Exactly one task is attributable to codex-A (this pane): it resolves,
    # it does not report [AMBIGUOUS] for the two crossed receipts.
    assert rc != pend.EXIT_ERROR


def test_pend_bare_retrieval_refuses_with_unidentified_caller_and_one_foreign_receipt(
    monkeypatch,
) -> None:
    """Gap 3(b): the case where accidentally succeeding looks like working
    correctly. Two codex sessions exist, this pane's own identity could NOT
    be confirmed (`executing_pane` matches neither), and exactly ONE
    receipt is present -- belonging to the OTHER session, not this pane.
    Under the old bug, an empty identity filter would read it unfiltered,
    find exactly one record, and "succeed" by returning it: a receipt that
    was never this caller's. This must refuse instead."""
    pend = _load_pend_module()
    _bind_pair(
        pend, monkeypatch,
        project="p", session="ai-1", current_provider="codex",
        executing_pane=("99", "tmux"), sessions=_TWO_CODEX_SESSIONS,
    )
    foreign = _identity_receipt(
        "foreign", "codex", session="ai-1", project="p", submitted="1",
        caller_live_id="codex-B", destination_live_id="codex-A",
    )
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("foreign.json"), foreign)])

    rc = pend.main(["pend"])

    assert rc == pend.EXIT_ERROR


def test_pend_bare_retrieval_refuses_with_unknown_provider_and_one_foreign_receipt(
    monkeypatch,
) -> None:
    """Item 3's second correction: the old guard (`sessions and
    current_provider`) skipped verification ENTIRELY when this pane's own
    provider could not be determined, rather than refusing. Two codex
    sessions exist (real ambiguity to scope against), `current_provider`
    is unknown, and exactly ONE receipt is present -- belonging to a
    different session entirely. Under the old bug this "succeeds" by
    returning it."""
    pend = _load_pend_module()
    _bind_pair(
        pend, monkeypatch,
        project="p", session="ai-1", current_provider="",
        executing_pane=("99", "tmux"), sessions=_TWO_CODEX_SESSIONS,
    )
    foreign = _identity_receipt(
        "foreign", "codex", session="ai-1", project="p", submitted="1",
        caller_live_id="codex-B", destination_live_id="codex-A",
    )
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("foreign.json"), foreign)])

    rc = pend.main(["pend"])

    assert rc == pend.EXIT_ERROR


def test_pend_peer_refuses_against_a_valid_but_empty_inventory_with_one_foreign_receipt(
    monkeypatch,
) -> None:
    """Item 3's first correction: a real, VALID `live_sessions: []` is
    authoritative -- it must never be treated the same as "no inventory at
    all" and silently fall back to the legacy static peer mapping. Exactly
    ONE receipt exists, addressed to "claude" (what the legacy heuristic
    would have guessed for `peer` from `codex`) -- but this launch's own
    (empty) inventory says there is no session here at all. Under the old
    bug this "succeeds" by returning it anyway."""
    pend = _load_pend_module()
    _bind_pair(
        pend, monkeypatch,
        project="p", session="ai-1", current_provider="codex",
        executing_pane=("10", "tmux"), sessions=[],
    )
    foreign = _receipt(
        "foreign-claude", "claude", session="ai-1", pane="ignored", project="p", submitted="1",
    )
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("foreign.json"), foreign)])

    rc = pend.main(["pend", "peer"])

    assert rc == pend.EXIT_ERROR


def _bind_absent(
    pend,
    monkeypatch,
    *,
    project: str,
    session: str,
    current_provider: str,
    executing_pane: tuple[str, str],
) -> None:
    """The record shape every launch actually has today: no `live_sessions`
    key at all. `_pair_scope` reports `present=False` for this -- these are
    the positive controls proving every implicit-retrieval branch keeps
    working exactly as before once there is no inventory to be
    authoritative about."""
    monkeypatch.setattr(pend, "_current_session_context", lambda: (project, session, current_provider))
    monkeypatch.setattr(
        pend,
        "_current_tab_registry",
        lambda p, s: {
            "ccb_project_id": project,
            "ccb_session_id": session,
            "work_dir": "/absent-launch",
            "providers": {current_provider: {"pane_id": executing_pane[0]}} if current_provider else {},
        },
    )
    monkeypatch.setattr(pend, "_executing_pane", lambda: executing_pane)


# --- Correction 1: the presence distinction (Item 3) must be carried
# through EVERY implicit retrieval branch, not just `peer`. Each empty-
# inventory test below is paired with an absent-inventory positive control
# using the identical foreign receipt, so both directions are pinned. ---


def test_pend_bare_refuses_against_valid_empty_inventory_with_one_foreign_receipt(
    monkeypatch,
) -> None:
    pend = _load_pend_module()
    _bind_pair(
        pend, monkeypatch,
        project="p", session="ai-1", current_provider="codex",
        executing_pane=("10", "tmux"), sessions=[],
    )
    foreign = _receipt("foreign", "codex", session="ai-1", pane="ignored", project="p", submitted="1")
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("foreign.json"), foreign)])
    shown: list[dict] = []
    monkeypatch.setattr(pend, "_show_receipt", lambda receipt: shown.append(receipt) or pend.EXIT_OK)

    rc = pend.main(["pend"])

    assert rc == pend.EXIT_ERROR
    assert shown == []


def test_pend_bare_succeeds_against_absent_inventory_positive_control(monkeypatch) -> None:
    pend = _load_pend_module()
    _bind_absent(
        pend, monkeypatch,
        project="p", session="ai-1", current_provider="codex", executing_pane=("10", "tmux"),
    )
    receipt = _receipt("only-one", "codex", session="ai-1", pane="10", project="p", submitted="1")
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("only.json"), receipt)])
    shown: list[dict] = []
    monkeypatch.setattr(pend, "_show_receipt", lambda r: shown.append(r) or pend.EXIT_OK)

    rc = pend.main(["pend"])

    assert rc == pend.EXIT_OK
    assert len(shown) == 1
    assert shown[0]["task_id"] == "only-one"


def test_pend_local_refuses_against_valid_empty_inventory_with_one_foreign_receipt(
    monkeypatch,
) -> None:
    """'local' must not accept a zero-member pool: this inventory is
    authoritative and says `codex` has no live session at all."""
    pend = _load_pend_module()
    _bind_pair(
        pend, monkeypatch,
        project="p", session="ai-1", current_provider="codex",
        executing_pane=("10", "tmux"), sessions=[],
    )
    foreign = _receipt("foreign", "codex", session="ai-1", pane="ignored", project="p", submitted="1")
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("foreign.json"), foreign)])
    reader_calls: list[tuple] = []
    monkeypatch.setattr(
        pend, "provider_log_reader",
        lambda *a, **k: reader_calls.append((a, k)) or (_ for _ in ()).throw(
            AssertionError("must never read the default transcript reader")
        ),
    )

    rc = pend.main(["pend", "local"])

    assert rc == pend.EXIT_ERROR
    assert reader_calls == []


def test_pend_local_succeeds_against_absent_inventory_positive_control(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    pend = _load_pend_module()
    _bind_absent(
        pend, monkeypatch,
        project="p", session="ccb-1", current_provider="codex", executing_pane=("10", "tmux"),
    )
    receipt = _completed_overlay_receipt(
        tmp_path, "20260907-090000-001-1", submitted="1", finished="2026-09-07T09:00:01Z", reply="Local reply",
    )
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("r.json"), receipt)])
    reader_calls: list[tuple] = []

    class _EmptyReader:
        def latest_exchanges(self, _n: int) -> list[dict]:
            reader_calls.append(())
            return []

    monkeypatch.setattr(pend, "provider_log_reader", lambda *a, **k: _EmptyReader())

    rc = pend.main(["pend", "local"])

    assert rc == pend.EXIT_OK
    assert "Local reply" in capsys.readouterr().out
    assert len(reader_calls) == 1


def test_pend_explicit_provider_refuses_against_valid_empty_inventory_with_one_foreign_receipt(
    monkeypatch,
) -> None:
    """An explicit provider target ('codex') must not accept a zero-member
    pool either -- same defect as 'local', reached differently."""
    pend = _load_pend_module()
    _bind_pair(
        pend, monkeypatch,
        project="p", session="ai-1", current_provider="claude",
        executing_pane=("7", "tmux"), sessions=[],
    )
    foreign = _receipt("foreign", "codex", session="ai-1", pane="ignored", project="p", submitted="1")
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("foreign.json"), foreign)])
    reader_calls: list[tuple] = []
    monkeypatch.setattr(
        pend, "provider_log_reader",
        lambda *a, **k: reader_calls.append((a, k)) or (_ for _ in ()).throw(
            AssertionError("must never read the default transcript reader")
        ),
    )

    rc = pend.main(["pend", "codex"])

    assert rc == pend.EXIT_ERROR
    assert reader_calls == []


def test_pend_explicit_provider_succeeds_against_absent_inventory_positive_control(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    pend = _load_pend_module()
    _bind_absent(
        pend, monkeypatch,
        project="p", session="ccb-1", current_provider="claude", executing_pane=("7", "tmux"),
    )
    receipt = _completed_overlay_receipt(
        tmp_path, "20260907-090000-001-2", submitted="1", finished="2026-09-07T09:00:01Z", reply="Codex reply",
    )
    monkeypatch.setattr(pend, "iter_receipts", lambda: [(Path("r.json"), receipt)])
    reader_calls: list[tuple] = []

    class _EmptyReader:
        def latest_exchanges(self, _n: int) -> list[dict]:
            reader_calls.append(())
            return []

    monkeypatch.setattr(pend, "provider_log_reader", lambda *a, **k: _EmptyReader())

    rc = pend.main(["pend", "codex"])

    assert rc == pend.EXIT_OK
    assert "Codex reply" in capsys.readouterr().out
    assert len(reader_calls) == 1


def test_pend_legacy_refuses_against_valid_empty_inventory(monkeypatch) -> None:
    """`--legacy` always reads whatever the default session file currently
    says, with no identity check of its own -- once the inventory is
    authoritative and empty, it must refuse rather than fall through to
    that disconnected default."""
    pend = _load_pend_module()
    _bind_pair(
        pend, monkeypatch,
        project="p", session="ai-1", current_provider="codex",
        executing_pane=("10", "tmux"), sessions=[],
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        pend, "_legacy_pend",
        lambda *args: calls.append(args) or (_ for _ in ()).throw(
            AssertionError("--legacy must refuse, not run, against an authoritative empty inventory")
        ),
    )

    rc = pend.main(["pend", "codex", "--legacy"])

    assert rc == pend.EXIT_ERROR
    assert calls == []


def test_pend_legacy_succeeds_against_absent_inventory_positive_control(monkeypatch) -> None:
    pend = _load_pend_module()
    _bind_absent(
        pend, monkeypatch,
        project="p", session="ai-1", current_provider="codex", executing_pane=("10", "tmux"),
    )
    calls: list[tuple] = []
    monkeypatch.setattr(pend, "_legacy_pend", lambda *args: calls.append(args) or pend.EXIT_OK)

    rc = pend.main(["pend", "codex", "--legacy"])

    assert rc == pend.EXIT_OK
    assert calls == [("codex", [])]


def test_pend_legacy_refuses_when_provider_pool_is_genuinely_ambiguous(monkeypatch) -> None:
    pend = _load_pend_module()
    _bind_pair(
        pend, monkeypatch,
        project="p", session="ai-1", current_provider="codex",
        executing_pane=("10", "tmux"), sessions=_TWO_CODEX_SESSIONS,
    )
    monkeypatch.setattr(
        pend,
        "_legacy_pend",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy pend must refuse, not run, under ambiguity")),
    )

    rc = pend.main(["pend", "codex", "--legacy"])

    assert rc == pend.EXIT_ERROR


def test_pend_legacy_still_runs_for_a_unique_provider_under_a_pair(monkeypatch) -> None:
    """The legacy path is only refused when the SPECIFIC provider asked for
    is genuinely ambiguous -- a launch that happens to carry a valid
    inventory but asks about a provider with exactly one session must
    behave exactly as it always has."""
    pend = _load_pend_module()
    _bind_pair(
        pend, monkeypatch,
        project="p", session="ai-1", current_provider="codex",
        executing_pane=("10", "tmux"),
        sessions=_TWO_CODEX_SESSIONS + [{"live_id": "claude-1", "provider": "claude", "pane_id": "7"}],
    )
    calls = []
    monkeypatch.setattr(pend, "_legacy_pend", lambda provider, extra: calls.append((provider, extra)) or pend.EXIT_OK)

    rc = pend.main(["pend", "claude", "--legacy"])

    assert rc == pend.EXIT_OK
    assert calls == [("claude", [])]
