from __future__ import annotations

import errno
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_bridge_module():
    path = ROOT / "bin" / "ccb-bridge-ask"
    loader = importlib.machinery.SourceFileLoader("ccb_bridge_ask_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _Lock:
    def fileno(self) -> int:
        return 0

    def close(self) -> None:
        pass


class _Fcntl:
    LOCK_UN = 8

    def flock(self, _fd: int, _op: int) -> None:
        pass


def test_bridge_lock_remains_exclusive_in_writable_task_dir(tmp_path: Path, monkeypatch) -> None:
    bridge = _load_bridge_module()
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))

    first_handle, first_fcntl = bridge._acquire_lock("abcd1234", "claude")
    try:
        with pytest.raises(RuntimeError, match="target provider is busy"):
            bridge._acquire_lock("abcd1234", "claude")
    finally:
        first_fcntl.flock(first_handle.fileno(), first_fcntl.LOCK_UN)
        first_handle.close()


def test_bridge_diagnostics_print_when_reply_empty(monkeypatch, capsys) -> None:
    bridge = _load_bridge_module()
    target = {
        "index": 1,
        "work_dir": "/tmp/project",
        "ccb_project_id": "abcd1234",
        "providers": {"claude": {"alive": True}},
    }

    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    monkeypatch.setattr(bridge, "_acquire_lock", lambda _hash, _provider: (_Lock(), _Fcntl()))
    monkeypatch.setattr(
        bridge,
        "_send_to_daemon",
        lambda *_args: (
            0,
            "",
            {
                "done_seen": False,
                "anchor_seen": True,
                "fallback_scan": True,
                "status": "incomplete",
                "req_id": "req-1",
            },
        ),
    )

    rc = bridge.main(["--target", "1", "--caller-pane-id", "%cnt", "--caller-terminal", "tmux", "hello"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "[BRIDGE] done_seen=False anchor_seen=True fallback_scan=True status=incomplete req_id=req-1" in captured.err


def test_bridge_persists_observed_delivery_confirmation(monkeypatch) -> None:
    bridge = _load_bridge_module()
    target = {
        "index": 1,
        "work_dir": "/tmp/target",
        "ccb_project_id": "project-target",
        "providers": {"claude": {"alive": True, "mounted": True}},
    }
    updates: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    monkeypatch.setattr(
        bridge, "_acquire_lock", lambda _hash, _provider: (_Lock(), _Fcntl())
    )
    monkeypatch.setattr(
        bridge,
        "_send_to_daemon",
        lambda *_args: (
            0,
            "Peer message delivered.",
            {
                "confirmation": "observed",
                "log_path": "/tmp/target.jsonl",
            },
        ),
    )
    monkeypatch.setattr(
        bridge,
        "update_peer_delivery",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    assert (
        bridge.main(
            [
                "--target",
                "1",
                "--peer-task-id",
                "task-1",
                "--intent",
                "background",
                "hello",
            ]
        )
        == 0
    )
    assert updates == [
        (
            ("task-1",),
            {
                "confirmation": "observed",
                "target_work_dir": "/tmp/target",
                "target_project_id": "project-target",
                "target_log_path": "/tmp/target.jsonl",
            },
        )
    ]


def test_bridge_persists_failed_confirmation_when_target_resolution_fails(
    monkeypatch, capsys
) -> None:
    bridge = _load_bridge_module()
    updates: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        bridge,
        "_load_targets",
        lambda: (_ for _ in ()).throw(RuntimeError("discovery failed")),
    )
    monkeypatch.setattr(
        bridge,
        "update_peer_delivery",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    assert (
        bridge.main(
            [
                "--target",
                "1",
                "--peer-task-id",
                "task-1",
                "--intent",
                "background",
                "hello",
            ]
        )
        == 1
    )
    assert "discovery failed" in capsys.readouterr().err
    assert updates == [
        (
            ("task-1",),
            {
                "confirmation": "failed",
                "target_work_dir": "",
                "target_project_id": "",
                "target_log_path": "",
            },
        )
    ]


def test_background_bridge_failure_notifies_original_caller(
    monkeypatch, tmp_path: Path
) -> None:
    bridge = _load_bridge_module()
    captured: dict = {}
    monkeypatch.setenv("CCB_PEER_STATUS_FILE", str(tmp_path / "task.status"))
    monkeypatch.setenv("CCB_PEER_LOG_FILE", str(tmp_path / "task.log"))
    monkeypatch.setenv("CCB_PEER_PROVIDER", "claude")
    monkeypatch.setenv("CCB_REQ_ID", "task-1")
    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setenv("CCB_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("CCB_CALLER_PANE_ID", "7")
    monkeypatch.setenv("CCB_CALLER_TERMINAL", "wezterm")
    monkeypatch.setattr(
        bridge,
        "notify_completion",
        lambda **kwargs: captured.update(kwargs),
    )

    bridge._notify_background_failure(1)

    assert captured["provider"] == "peer-claude"
    assert captured["req_id"] == "task-1"
    assert captured["status"] == bridge.COMPLETION_STATUS_FAILED
    assert captured["caller_pane_id"] == "7"
    assert "delivery failed" in captured["reply"]


def test_bridge_appends_wait_context() -> None:
    bridge = _load_bridge_module()

    assert bridge._append_peer_context("hello", "/tmp/sender", "wait", "task-1", "parent-1") == (
        "hello\n\nCCB_PEER_INTENT: wait\nCCB_PEER_TASK_ID: task-1\nCCB_PEER_REPLY_TO: parent-1\n"
        "CCB_REPLY_TARGET: /tmp/sender\nCCB_REPLY_PROVIDER: claude\nCCB_REPLY_EXPECTED: yes"
    )


def test_bridge_notify_context_has_no_reply_target() -> None:
    bridge = _load_bridge_module()

    message = bridge._append_peer_context("hello", "/tmp/sender", "notify")

    assert message == "hello\n\nCCB_PEER_INTENT: notify\nCCB_REPLY_EXPECTED: no"
    assert "CCB_REPLY_TARGET" not in message


def test_bridge_rejects_notify_that_asks_for_reply(capsys) -> None:
    bridge = _load_bridge_module()

    rc = bridge.main(["--target", "1", "--intent", "notify", "Can you confirm?"])

    assert rc == 1
    assert "use --background or --wait" in capsys.readouterr().err


def test_bridge_rejects_codex_only_project(monkeypatch, capsys) -> None:
    bridge = _load_bridge_module()
    target = {
        "index": 1,
        "work_dir": "/tmp/project",
        "ccb_project_id": "abcd1234",
        "peer_capable": False,
        "providers": {"codex": {"alive": True}},
    }
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])

    rc = bridge.main(["--target", "1", "hello"])

    assert rc == 1
    assert "no mounted Claude pane" in capsys.readouterr().err


def test_bridge_accepts_codex_only_project(monkeypatch) -> None:
    bridge = _load_bridge_module()
    target = {
        "index": 1,
        "work_dir": "/tmp/project",
        "ccb_project_id": "abcd1234",
        "peer_capable": False,
        "providers": {"codex": {"alive": True, "mounted": True}},
    }
    captured: dict = {}
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    monkeypatch.setattr(bridge, "_acquire_lock", lambda _hash, _provider: (_Lock(), _Fcntl()))
    monkeypatch.setattr(
        bridge,
        "_send_to_daemon",
        lambda *args: captured.update(provider=args[-3], caller=args[-2], foreground=args[-1])
        or (0, "Codex result", {}),
    )

    rc = bridge.main(
        ["--target", "1", "--provider", "codex", "--caller", "claude", "--foreground", "hello"]
    )

    assert rc == 0
    assert captured == {"provider": "codex", "caller": "claude", "foreground": True}


def test_bridge_rejects_unmounted_claude_project(monkeypatch, capsys) -> None:
    bridge = _load_bridge_module()
    target = {
        "index": 1,
        "work_dir": "/tmp/project",
        "ccb_project_id": "abcd1234",
        "peer_capable": False,
        "providers": {"claude": {"alive": True, "mounted": False}},
    }
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])

    rc = bridge.main(["--target", "1", "hello"])

    assert rc == 1
    assert "no mounted Claude pane" in capsys.readouterr().err


def test_bridge_request_is_delivery_only(monkeypatch) -> None:
    bridge = _load_bridge_module()
    sent: dict = {}

    monkeypatch.setattr(bridge.askd_rpc, "read_state", lambda _path: {"port": 1234, "token": "tok"})
    monkeypatch.setattr(bridge, "find_running_state_file", lambda *_args, **_kwargs: Path("/tmp/askd.json"))
    monkeypatch.setattr(
        bridge.askd_rpc,
        "request_daemon",
        lambda _state, request, **_kwargs: sent.update(request)
        or {"exit_code": 0, "reply": "Peer message delivered.", "meta": {"status": "completed"}},
    )

    exit_code, reply, _meta = bridge._send_to_daemon(
        {"work_dir": "/tmp/target"},
        "hello",
        10.0,
        "pane-1",
        "wezterm",
        "/tmp/sender",
        "background",
        "task-2",
        "parent-2",
    )

    assert exit_code == 0
    assert reply == "Peer message delivered."
    assert sent["delivery_only"] is True
    assert sent["suppress_completion_hook"] is True
    assert sent["req_id"] == "task-2"
    assert sent["message"] == (
        "hello\n\nCCB_PEER_INTENT: background\nCCB_PEER_TASK_ID: task-2\nCCB_PEER_REPLY_TO: parent-2\n"
        "CCB_REPLY_TARGET: /tmp/sender\nCCB_REPLY_PROVIDER: claude\nCCB_REPLY_EXPECTED: yes"
    )


def test_codex_bridge_request_is_delivery_only_with_explicit_reply_context(monkeypatch) -> None:
    bridge = _load_bridge_module()
    sent: dict = {}

    monkeypatch.setattr(bridge.askd_rpc, "read_state", lambda _path: {"port": 1234, "token": "tok"})
    monkeypatch.setattr(bridge, "find_running_state_file", lambda *_args, **_kwargs: Path("/tmp/askd.json"))
    monkeypatch.setattr(
        bridge.askd_rpc,
        "request_daemon",
        lambda _state, request, **_kwargs: sent.update(request)
        or {"exit_code": 0, "reply": "Peer message delivered.", "req_id": "task-3"},
    )

    exit_code, reply, _meta = bridge._send_to_daemon(
        {"work_dir": "/tmp/target"},
        "review this",
        10.0,
        "pane-1",
        "wezterm",
        "/tmp/sender",
        "background",
        "task-3",
        "",
        "codex",
        "claude",
        False,
    )

    assert exit_code == 0
    assert reply == "Peer message delivered."
    assert sent["provider"] == "codex"
    assert sent["caller"] == "claude"
    assert sent["caller_work_dir"] == "/tmp/sender"
    assert sent["req_id"] == "task-3"
    assert sent["delivery_only"] is True
    assert sent["suppress_completion_hook"] is True
    assert "CCB_REPLY_TARGET: /tmp/sender" in sent["message"]
    assert "CCB_REPLY_PROVIDER: claude" in sent["message"]
    assert "CCB_REPLY_MODE: automatic-capture" not in sent["message"]


def test_codex_notify_suppresses_completion_hook(monkeypatch) -> None:
    bridge = _load_bridge_module()
    sent: dict = {}

    monkeypatch.setattr(bridge.askd_rpc, "read_state", lambda _path: {"port": 1234, "token": "tok"})
    monkeypatch.setattr(bridge, "find_running_state_file", lambda *_args, **_kwargs: Path("/tmp/askd.json"))
    monkeypatch.setattr(
        bridge.askd_rpc,
        "request_daemon",
        lambda _state, request, **_kwargs: sent.update(request)
        or {"exit_code": 0, "reply": "Acknowledged."},
    )

    bridge._send_to_daemon(
        {"work_dir": "/tmp/target"},
        "FYI",
        10.0,
        "pane-1",
        "tmux",
        "/tmp/sender",
        "notify",
        "task-4",
        "",
        "codex",
        "claude",
        False,
    )

    assert sent["delivery_only"] is True
    assert sent["suppress_completion_hook"] is True
    assert "CCB_REPLY_EXPECTED: no" in sent["message"]
    assert "CCB_REPLY_TARGET" not in sent["message"]
    assert "CCB_REPLY_PROVIDER" not in sent["message"]


def test_claude_peer_from_codex_names_reverse_reply_provider() -> None:
    bridge = _load_bridge_module()

    message = bridge._append_peer_context(
        "hello",
        "/tmp/sender",
        "wait",
        "task-5",
        provider="claude",
        caller="codex",
    )

    assert "CCB_REPLY_PROVIDER: codex" in message


def test_codex_peer_from_claude_names_reverse_reply_provider() -> None:
    bridge = _load_bridge_module()

    message = bridge._append_peer_context(
        "hello",
        "/tmp/sender",
        "wait",
        "task-6",
        provider="codex",
        caller="claude",
    )

    assert "CCB_REPLY_TARGET: /tmp/sender" in message
    assert "CCB_REPLY_PROVIDER: claude" in message
    assert "CCB_REPLY_MODE: automatic-capture" not in message


class _DirectBackend:
    def __init__(self, pane_id: str = "%7") -> None:
        self.pane_id = pane_id
        self.sent: list[tuple[str, str]] = []

    def is_alive(self, pane_id: str) -> bool:
        return pane_id == self.pane_id

    def pane_matches_cwd_strict(self, pane_id: str, _work_dir: str) -> bool:
        return pane_id == self.pane_id

    def find_pane_by_title_marker(self, _marker: str, _work_dir: str) -> str:
        return self.pane_id

    def send_text(self, pane_id: str, prompt: str) -> None:
        self.sent.append((pane_id, prompt))


def test_wezterm_direct_reply_accepts_dynamic_application_title(monkeypatch, tmp_path: Path) -> None:
    bridge = _load_bridge_module()
    backend = _DirectBackend("2")
    backend.find_pane_by_title_marker = lambda _marker, _work_dir: None
    monkeypatch.setattr(bridge, "get_backend_for_session", lambda _session: backend)
    receipt = {
        "caller_pane_id": "2",
        "caller_terminal": "wezterm",
        "caller_pane_title_marker": "CCB-Claude-project",
        "work_dir": str(tmp_path),
        "ccb_project_id": "project",
    }

    target, resolved_backend = bridge._validated_direct_reply_target(receipt, "claude")

    assert target["providers"]["claude"]["pane_id"] == "2"
    assert resolved_backend is backend


def test_direct_reply_fallback_wraps_claude_and_codex_delivery_prompts() -> None:
    bridge = _load_bridge_module()

    for provider in ("claude", "codex"):
        backend = _DirectBackend()
        target = {
            "work_dir": "/tmp/sender",
            "ccb_project_id": "abcd1234",
            "providers": {provider: {"pane_id": "%7"}},
        }

        exit_code, reply, meta = bridge._send_to_direct_reply_target(
            target,
            backend,
            "terminal result",
            "/tmp/responder",
            "notify",
            "",
            "task-1",
            provider,
            "codex" if provider == "claude" else "claude",
        )

        assert exit_code == 0
        assert reply == "Peer reply delivered directly."
        assert meta["direct_reply_fallback"] is True
        assert meta["confirmation"] == "sent"
        assert backend.sent[0][0] == "%7"
        assert "terminal result" in backend.sent[0][1]
        assert "CCB_REPLY_EXPECTED: no" in backend.sent[0][1]
        if provider == "codex":
            assert "CCB is not capturing this turn automatically" in backend.sent[0][1]


def test_reverse_reply_uses_validated_receipt_when_project_is_not_live(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    bridge = _load_bridge_module()
    reply_file = tmp_path / "ask-peer-codex-task-1.reply"
    status_file = tmp_path / "ask-peer-codex-task-1.status"
    reply_file.touch()
    status_file.touch()
    receipt = {
        "provider": "peer-codex",
        "caller": "claude",
        "caller_pane_id": "%7",
        "caller_terminal": "tmux",
        "caller_pane_title_marker": "ccb-claude-sender",
        "work_dir": str(tmp_path),
        "ccb_project_id": "abcd1234",
        "reply_expected": True,
        "peer_reply_file": str(reply_file),
        "status_file": str(status_file),
    }
    backend = _DirectBackend()

    monkeypatch.setattr(bridge, "find_receipt", lambda _task: (tmp_path / "receipt.json", receipt))
    monkeypatch.setattr(bridge, "_load_targets", lambda: [])
    monkeypatch.setattr(bridge, "get_backend_for_session", lambda _session: backend)
    monkeypatch.setattr(bridge, "_acquire_lock", lambda _hash, _provider: (_Lock(), _Fcntl()))

    rc = bridge.main(
        [
            "--target",
            str(tmp_path),
            "--provider",
            "claude",
            "--caller",
            "codex",
            "--sender-work-dir",
            "/tmp/responder",
            "--intent",
            "notify",
            "--reply-to",
            "task-1",
            "terminal result",
        ]
    )

    assert rc == 0
    assert reply_file.read_text(encoding="utf-8").strip() == "terminal result"
    assert "delivery=direct exit_code=0" in status_file.read_text(encoding="utf-8")
    assert backend.sent and backend.sent[0][0] == "%7"
    assert "Peer reply delivered directly." in capsys.readouterr().out


def test_reverse_reply_prefers_live_project_and_saves_correlated_result(
    monkeypatch, tmp_path: Path
) -> None:
    bridge = _load_bridge_module()
    reply_file = tmp_path / "ask-peer-codex-task-live.reply"
    status_file = tmp_path / "ask-peer-codex-task-live.status"
    reply_file.touch()
    status_file.touch()
    receipt = {
        "provider": "peer-codex",
        "caller": "claude",
        "work_dir": str(tmp_path),
        "ccb_project_id": "abcd1234",
        "reply_expected": True,
        "peer_reply_file": str(reply_file),
        "status_file": str(status_file),
    }
    target = {
        "work_dir": str(tmp_path),
        "ccb_project_id": "abcd1234",
        "providers": {"claude": {"alive": True, "mounted": True}},
    }
    sent: dict = {}

    monkeypatch.setattr(bridge, "find_receipt", lambda _task: (tmp_path / "receipt.json", receipt))
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    monkeypatch.setattr(bridge, "_acquire_lock", lambda _hash, _provider: (_Lock(), _Fcntl()))
    monkeypatch.setattr(
        bridge,
        "_send_to_daemon",
        lambda *args: sent.update(target=args[0], message=args[1]) or (0, "Peer message delivered.", {}),
    )
    monkeypatch.setattr(
        bridge,
        "get_backend_for_session",
        lambda _session: (_ for _ in ()).throw(AssertionError("direct fallback must not run")),
    )

    rc = bridge.main(
        [
            "--target",
            str(tmp_path),
            "--provider",
            "claude",
            "--reply-to",
            "task-live",
            "live result",
        ]
    )

    assert rc == 0
    assert sent == {"target": target, "message": "live result"}
    assert reply_file.read_text(encoding="utf-8").strip() == "live result"
    assert "delivery=live exit_code=0" in status_file.read_text(encoding="utf-8")


def test_bridge_refuses_ambiguous_remote_destination_before_delivery(monkeypatch, capsys) -> None:
    """Task 3: an initial peer send whose target project has more than one
    live session of the requested provider must fail explicitly, before
    anything is delivered -- no role inference, no first-or-newest
    default."""
    bridge = _load_bridge_module()
    target = {
        "index": 1,
        "work_dir": "/tmp/project",
        "ccb_project_id": "abcd1234",
        "peer_capable": False,
        "providers": {
            "claude": {
                "alive": True,
                "mounted": False,
                "ambiguous": True,
                "candidates": [
                    {"launch_id": "ai-1", "pane_id": "%1", "terminal": "tmux", "session_file": ""},
                    {"launch_id": "ai-2", "pane_id": "%2", "terminal": "tmux", "session_file": ""},
                ],
            }
        },
    }
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    monkeypatch.setattr(
        bridge,
        "_send_to_daemon",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("delivery must not be attempted")),
    )
    monkeypatch.setattr(
        bridge,
        "get_backend_for_session",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("delivery must not be attempted")),
    )
    monkeypatch.setattr(
        bridge,
        "_acquire_lock",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("delivery must not be attempted")),
    )

    rc = bridge.main(["--target", "1", "--provider", "claude", "hello"])

    assert rc == 1
    assert "more than one live" in capsys.readouterr().err


def test_bridge_reply_uses_saved_identity_over_ambiguous_or_mismatched_generic_view(
    monkeypatch, tmp_path: Path
) -> None:
    """Task 4: a correlated reply whose receipt carries a saved return pane
    must be delivered there, even when the generic project+provider lookup
    also looks mounted (at a DIFFERENT pane) -- the saved identity is
    validated first and used instead, never overridden by a live-looking
    but unrelated session."""
    bridge = _load_bridge_module()
    reply_file = tmp_path / "ask-peer-codex-task-dup.reply"
    status_file = tmp_path / "ask-peer-codex-task-dup.status"
    reply_file.touch()
    status_file.touch()
    receipt = {
        "provider": "peer-codex",
        "caller": "claude",
        "caller_pane_id": "%7",
        "caller_terminal": "tmux",
        "caller_pane_title_marker": "ccb-claude-sender",
        "work_dir": str(tmp_path),
        "ccb_project_id": "abcd1234",
        "reply_expected": True,
        "peer_reply_file": str(reply_file),
        "status_file": str(status_file),
    }
    backend = _DirectBackend(pane_id="%7")
    # The generic project+provider view ALSO looks perfectly usable -- but
    # at a DIFFERENT, unrelated pane. If the saved identity were not
    # checked first, this is what a naive "prefer the live project" pick
    # would deliver to instead.
    other_target = {
        "work_dir": str(tmp_path),
        "ccb_project_id": "abcd1234",
        "providers": {"claude": {"alive": True, "mounted": True, "pane_id": "%99"}},
    }

    monkeypatch.setattr(bridge, "find_receipt", lambda _task: (tmp_path / "receipt.json", receipt))
    monkeypatch.setattr(bridge, "get_backend_for_session", lambda _session: backend)
    monkeypatch.setattr(bridge, "_acquire_lock", lambda _hash, _provider: (_Lock(), _Fcntl()))
    monkeypatch.setattr(bridge, "_load_targets", lambda: [other_target])
    monkeypatch.setattr(
        bridge,
        "_send_to_daemon",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("daemon path must not be used for a saved identity")),
    )

    rc = bridge.main(
        [
            "--target",
            str(tmp_path),
            "--provider",
            "claude",
            "--reply-to",
            "task-dup",
            "exact reply",
        ]
    )

    assert rc == 0
    assert backend.sent and backend.sent[0][0] == "%7"
    assert reply_file.read_text(encoding="utf-8").strip() == "exact reply"


def test_bridge_reply_uses_saved_identity_when_generic_lookup_is_ambiguous(
    monkeypatch, tmp_path: Path
) -> None:
    """Item 4: a correlated reply whose receipt carries a saved return pane
    stays exact even when the generic project+provider lookup itself is
    AMBIGUOUS (more than one live session of that provider) -- the saved
    identity wins and is delivered via the validated direct fallback,
    never refused and never guessed at via the daemon path."""
    bridge = _load_bridge_module()
    reply_file = tmp_path / "ask-peer-codex-task-amb.reply"
    status_file = tmp_path / "ask-peer-codex-task-amb.status"
    reply_file.touch()
    status_file.touch()
    receipt = {
        "provider": "peer-codex",
        "caller": "claude",
        "caller_pane_id": "%7",
        "caller_terminal": "tmux",
        "caller_pane_title_marker": "ccb-claude-sender",
        "work_dir": str(tmp_path),
        "ccb_project_id": "abcd1234",
        "reply_expected": True,
        "peer_reply_file": str(reply_file),
        "status_file": str(status_file),
    }
    backend = _DirectBackend(pane_id="%7")
    ambiguous_target = {
        "work_dir": str(tmp_path),
        "ccb_project_id": "abcd1234",
        "providers": {
            "claude": {
                "alive": True,
                "mounted": False,
                "ambiguous": True,
                "candidates": [
                    {"launch_id": "ai-1", "pane_id": "%7", "terminal": "tmux", "session_file": ""},
                    {"launch_id": "ai-2", "pane_id": "%8", "terminal": "tmux", "session_file": ""},
                ],
            }
        },
    }

    monkeypatch.setattr(bridge, "find_receipt", lambda _task: (tmp_path / "receipt.json", receipt))
    monkeypatch.setattr(bridge, "get_backend_for_session", lambda _session: backend)
    monkeypatch.setattr(bridge, "_acquire_lock", lambda _hash, _provider: (_Lock(), _Fcntl()))
    monkeypatch.setattr(bridge, "_load_targets", lambda: [ambiguous_target])
    monkeypatch.setattr(
        bridge,
        "_send_to_daemon",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("daemon path must not be used when ambiguous")),
    )

    rc = bridge.main(
        [
            "--target",
            str(tmp_path),
            "--provider",
            "claude",
            "--reply-to",
            "task-amb",
            "exact reply",
        ]
    )

    assert rc == 0
    assert backend.sent and backend.sent[0][0] == "%7"
    assert reply_file.read_text(encoding="utf-8").strip() == "exact reply"


def test_reverse_reply_continues_when_bridge_lock_filesystem_is_read_only(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    bridge = _load_bridge_module()
    reply_file = tmp_path / "ask-peer-codex-task-erofs.reply"
    status_file = tmp_path / "ask-peer-codex-task-erofs.status"
    reply_file.touch()
    status_file.touch()
    receipt = {
        "provider": "peer-codex",
        "caller": "claude",
        "work_dir": str(tmp_path),
        "ccb_project_id": "abcd1234",
        "reply_expected": True,
        "peer_reply_file": str(reply_file),
        "status_file": str(status_file),
    }
    target = {
        "work_dir": str(tmp_path),
        "ccb_project_id": "abcd1234",
        "providers": {"claude": {"alive": True, "mounted": True}},
    }
    deliveries: list[str] = []
    original_open = Path.open

    def _open(path: Path, *args, **kwargs):
        mode = str(args[0] if args else kwargs.get("mode", "r"))
        if path.name.startswith("bridge-") and "w" in mode:
            raise OSError(errno.EROFS, "Read-only file system", str(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(Path, "open", _open)
    monkeypatch.setattr(bridge, "find_receipt", lambda _task: (tmp_path / "receipt.json", receipt))
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    monkeypatch.setattr(
        bridge,
        "_send_to_daemon",
        lambda _target, message, *_args: deliveries.append(message) or (1, "", {}),
    )

    rc = bridge.main(
        [
            "--target",
            str(tmp_path),
            "--provider",
            "claude",
            "--reply-to",
            "task-erofs",
            "recoverable result",
        ]
    )

    assert rc == 1
    assert deliveries == ["recoverable result"]
    assert reply_file.read_text(encoding="utf-8").strip() == "recoverable result"
    captured = capsys.readouterr()
    assert "continuing unlocked" in captured.err
    assert "[RECOVERABLE] Reply saved for task task-erofs" in captured.err


def test_direct_reply_revalidates_after_lock(monkeypatch, tmp_path: Path) -> None:
    bridge = _load_bridge_module()
    backend = _DirectBackend()
    receipt = {
        "provider": "peer-codex",
        "caller": "claude", "caller_pane_id": "%7", "caller_terminal": "tmux",
        "caller_pane_title_marker": "ccb-claude-sender", "work_dir": str(tmp_path),
        "ccb_project_id": "abcd1234", "reply_expected": True,
        "peer_reply_file": str(tmp_path / "reply"), "status_file": str(tmp_path / "status"),
    }
    monkeypatch.setattr(bridge, "find_receipt", lambda _: (tmp_path / "receipt", receipt))
    monkeypatch.setattr(bridge, "_load_targets", lambda: [])
    monkeypatch.setattr(bridge, "get_backend_for_session", lambda _: backend)

    def acquire(*_):
        backend.pane_matches_cwd_strict = lambda *_: False
        return _Lock(), _Fcntl()

    monkeypatch.setattr(bridge, "_acquire_lock", acquire)
    assert bridge.main(["--target", str(tmp_path), "--provider", "claude",
                        "--reply-to", "task", "saved reply"]) == 1
    assert backend.sent == []
    assert (tmp_path / "reply").read_text().strip() == "saved reply"


def test_reverse_reply_rejects_reused_pane_but_preserves_result(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    bridge = _load_bridge_module()
    reply_file = tmp_path / "ask-peer-codex-task-2.reply"
    status_file = tmp_path / "ask-peer-codex-task-2.status"
    reply_file.touch()
    status_file.touch()
    receipt = {
        "provider": "peer-codex",
        "caller": "claude",
        "caller_pane_id": "%7",
        "caller_terminal": "tmux",
        "caller_pane_title_marker": "ccb-claude-sender",
        "work_dir": str(tmp_path),
        "ccb_project_id": "abcd1234",
        "reply_expected": True,
        "peer_reply_file": str(reply_file),
        "status_file": str(status_file),
    }
    backend = _DirectBackend()
    backend.pane_matches_cwd_strict = lambda _pane, _work_dir: False

    monkeypatch.setattr(bridge, "find_receipt", lambda _task: (tmp_path / "receipt.json", receipt))
    monkeypatch.setattr(bridge, "_load_targets", lambda: [])
    monkeypatch.setattr(bridge, "get_backend_for_session", lambda _session: backend)

    rc = bridge.main(
        [
            "--target",
            str(tmp_path),
            "--provider",
            "claude",
            "--reply-to",
            "task-2",
            "recoverable result",
        ]
    )

    assert rc == 1
    assert backend.sent == []
    assert reply_file.read_text(encoding="utf-8").strip() == "recoverable result"
    captured = capsys.readouterr()
    assert "no longer matches the receipt project" in captured.err
    assert f"pend task-2" in captured.err


# --- `--live-id`: explicit selection among several live peer sessions ---


def _live_status(live_id: str, pane_id: str, *, alive: bool = True, mounted: bool = True,
                 marker: str | None = None, reason: str = "") -> dict:
    return {"live_id": live_id, "pane_id": pane_id,
            "pane_title_marker": f"CCB-Codex-abcd1234-{live_id}" if marker is None else marker,
            "terminal": "tmux", "alive": alive, "mounted": mounted, "reason": reason}


def _session(providers: dict, *, session_id: str = "ai-1", alive: bool = True) -> dict:
    return {"session_id": session_id, "work_dir": "/tmp/project", "ccb_project_id": "abcd1234",
            "terminal": "tmux", "alive": alive, "providers": providers}


def _two_codex_session_target(sessions: list | None = None) -> dict:
    return {"index": 1, "work_dir": "/tmp/project", "ccb_project_id": "abcd1234", "terminal": "tmux",
            "providers": {"codex": {"alive": True, "mounted": False, "ambiguous": True,
                                      "pane_id": "", "reason": "ambiguous_sessions"}},
            "sessions": sessions or [_session({"codex": _live_status("live-a", "%28")}),
                                      _session({"codex": _live_status("live-b", "%27")})]}


def _no_delivery(bridge, monkeypatch) -> None:
    for name in ("_send_to_daemon", "get_backend_for_session", "_acquire_lock"):
        monkeypatch.setattr(bridge, name,
                            lambda *_a, **_k: (_ for _ in ()).throw(
                                AssertionError("delivery must not be attempted")))


def test_bridge_live_id_selects_one_of_two_codex_sessions(monkeypatch) -> None:
    bridge = _load_bridge_module()
    captured: dict = {}
    target = _two_codex_session_target([_session({"codex": _live_status("live-a", "%28")}),
                                        _session({"codex": _live_status("live-b/opaque id", "%27")})])
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    monkeypatch.setattr(bridge, "_acquire_lock", lambda _hash, _provider: (_Lock(), _Fcntl()))
    monkeypatch.setattr(bridge, "_send_to_daemon",
                        lambda target, *_a, **_k: (captured.update(target=target), (0, "ok", {}))[1])

    assert bridge.main(["--target", "1", "--provider", "codex", "--live-id", "live-b/opaque id", "hello"]) == 0
    target = captured["target"]
    assert target["peer_destination"] == {"pane_id": "%27", "terminal": "tmux", "work_dir": "/tmp/project",
                                           "ccb_project_id": "abcd1234",
                                           "pane_title_marker": "CCB-Codex-abcd1234-live-b/opaque id"}
    assert target["providers"]["codex"]["live_id"] == "live-b/opaque id"
    assert target["providers"]["codex"]["pane_id"] == "%27"


@pytest.mark.parametrize(
    ("target", "live_id", "expected"),
    [
        (_two_codex_session_target(), "live-zz", "matches no live codex session"),
        (_two_codex_session_target([_session({"codex": _live_status("live-a", "%28")}),
                                    _session({"claude": _live_status("live-c", "%30")})]),
         "live-c", "belongs to a different provider"),
        (_two_codex_session_target([_session({"codex": _live_status("live-a", "%28")}),
                                    _session({"codex": _live_status("live-b", "%27", mounted=False,
                                                                       reason="daemon_offline")})]),
         "live-b", "not a usable codex session"),
        (_two_codex_session_target([_session({"codex": _live_status("live-a", "%28")}),
                                    _session({"codex": _live_status("live-b", "%27")}, alive=False)]),
         "live-b", "no longer live"),
        (_two_codex_session_target([_session({"codex": _live_status("live-a", "%28")}, session_id="ai-1"),
                                    _session({"codex": _live_status("live-a", "%27")}, session_id="ai-2")]),
         "live-a", "more than one live codex session"),
        (_two_codex_session_target([_session({"codex": _live_status("live-a", "%28")}),
                                    _session({"codex": _live_status("live-a", "%27", alive=False)})]),
         "live-a", "more than one live codex session"),
        (_two_codex_session_target([_session({"codex": _live_status("", "", alive=False, mounted=False,
                                                                       marker="", reason="invalid_inventory")},
                                              alive=False)]),
         "live-a", "invalid live-session inventory"),
    ],
)
def test_bridge_live_id_refuses_non_unique_or_unavailable_inventory(target, live_id, expected, monkeypatch, capsys) -> None:
    bridge = _load_bridge_module()
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    _no_delivery(bridge, monkeypatch)
    assert bridge.main(["--target", "1", "--provider", "codex", "--live-id", live_id, "hello"]) == 1
    assert expected in capsys.readouterr().err


def _set_value(data: dict, path: tuple, value) -> None:
    for key in path[:-1]:
        data = data[key]
    data[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (("sessions", 0, "providers", "codex", "live_id"), 7, "non-string"),
        (("sessions", 0, "providers", "codex", "pane_id"), 7, "non-string"),
        (("sessions", 0, "providers", "codex", "terminal"), 7, "non-string"),
        (("sessions", 0, "work_dir"), 7, "non-string"),
        (("ccb_project_id",), 7, "non-string"),
        (("sessions", 0, "providers", "codex", "pane_title_marker"), 7, "non-string"),
        (("sessions", 0, "alive"), "false", "non-boolean"),
        (("sessions", 0, "providers", "codex", "alive"), "false", "non-boolean"),
        (("sessions", 0, "providers", "codex", "mounted"), "false", "non-boolean"),
    ],
)
def test_bridge_live_id_rejects_malformed_inventory_fields(path, value, expected, monkeypatch, capsys) -> None:
    bridge = _load_bridge_module()
    target = _two_codex_session_target([_session({"codex": _live_status("live-a", "%28")})])
    _set_value(target, path, value)
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    _no_delivery(bridge, monkeypatch)
    assert bridge.main(["--target", "1", "--provider", "codex", "--live-id", "live-a", "hello"]) == 1
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize(
    ("path", "value"),
    [(("sessions", 0, "work_dir"), "/tmp/other-project"),
     (("sessions", 0, "ccb_project_id"), "other-project"),
     (("sessions", 0, "terminal"), "wezterm"),
     (("sessions", 0, "providers", "codex", "pane_title_marker"), "other-marker")],
)
def test_bridge_live_id_rejects_conflicting_endpoint_identity(path, value, monkeypatch, capsys) -> None:
    bridge = _load_bridge_module()
    target = _two_codex_session_target([_session({"codex": _live_status("live-a", "%28")})])
    if path[-1] == "pane_title_marker":
        target["sessions"][0]["pane_title_marker"] = "session-marker"
    _set_value(target, path, value)
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    _no_delivery(bridge, monkeypatch)
    assert bridge.main(["--target", "1", "--provider", "codex", "--live-id", "live-a", "hello"]) == 1
    assert "conflicting" in capsys.readouterr().err


def test_bridge_live_id_requires_complete_endpoint(monkeypatch, capsys) -> None:
    bridge = _load_bridge_module()
    target = _two_codex_session_target([_session({"codex": _live_status("live-a", "%28")})])
    target["ccb_project_id"] = ""
    target["sessions"][0]["ccb_project_id"] = ""
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    _no_delivery(bridge, monkeypatch)
    assert bridge.main(["--target", "1", "--provider", "codex", "--live-id", "live-a", "hello"]) == 1
    assert "incomplete endpoint identity" in capsys.readouterr().err


def test_bridge_live_id_without_pane_marker_refuses_before_lock(monkeypatch, capsys) -> None:
    bridge = _load_bridge_module()
    target = _two_codex_session_target([_session({"codex": _live_status("live-a", "%28", marker="")})])
    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    _no_delivery(bridge, monkeypatch)
    assert bridge.main(["--target", "1", "--provider", "codex", "--live-id", "live-a", "hello"]) == 1
    assert "no CCB pane marker" in capsys.readouterr().err


def test_bridge_live_id_rejected_with_reply_to(monkeypatch, capsys) -> None:
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge,
        "_load_targets",
        lambda: (_ for _ in ()).throw(AssertionError("must refuse before resolving")),
    )

    rc = bridge.main(
        [
            "--target",
            "1",
            "--provider",
            "codex",
            "--live-id",
            "live-a",
            "--reply-to",
            "20260711-212112-453-72347",
            "hello",
        ]
    )

    assert rc == 1
    assert "cannot be combined with --reply-to" in capsys.readouterr().err


def test_bridge_without_live_id_still_refuses_ambiguous_sessions(monkeypatch, capsys) -> None:
    """Item 6: with no selector, the existing refusal is unchanged even
    though the inventory now names both candidates."""
    bridge = _load_bridge_module()
    monkeypatch.setattr(bridge, "_load_targets", lambda: [_two_codex_session_target()])
    _no_delivery(bridge, monkeypatch)

    rc = bridge.main(["--target", "1", "--provider", "codex", "hello"])

    assert rc == 1
    assert "more than one live" in capsys.readouterr().err
