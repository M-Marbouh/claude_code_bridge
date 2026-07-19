from __future__ import annotations

import importlib.util
import json
import threading
from types import SimpleNamespace
from importlib.machinery import SourceFileLoader
from pathlib import Path

import askd.daemon as askd_daemon
import askd_runtime
from askd.adapters.base import ProviderRequest, QueuedTask
from askd.adapters.claude import ClaudeAdapter
from askd.adapters.gemini import GeminiAdapter
from codex_comm import CodexLogReader
from completion_hook import completion_status_marker


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_completion_hook_uses_status_marker_and_directional_workdir_matching() -> None:
    hook = _load_script_module("ccb_completion_hook_script", REPO_ROOT / "bin" / "ccb-completion-hook")

    message = hook._render_terminal_message(
        "Codex",
        "req-1",
        "cancelled",
        output_file="",
        status="cancelled",
    )

    assert completion_status_marker("cancelled") in message
    assert hook._work_dirs_compatible("/repo", "/repo/subdir") is True
    assert hook._work_dirs_compatible("/repo/subdir", "/repo") is False


def test_completion_hook_manual_caller_is_noop(monkeypatch) -> None:
    hook = _load_script_module("ccb_completion_hook_manual", REPO_ROOT / "bin" / "ccb-completion-hook")
    monkeypatch.setenv("CCB_CALLER", "manual")
    monkeypatch.setenv("CCB_COMPLETION_STATUS", "cancelled")
    monkeypatch.setattr("sys.argv", ["ccb-completion-hook", "--provider", "codex", "--req-id", "req-1"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    assert hook.main() == 0


def test_completion_hook_wezterm_fallback_honors_send_failure(monkeypatch) -> None:
    hook = _load_script_module("ccb_completion_hook_wezterm", REPO_ROOT / "bin" / "ccb-completion-hook")

    monkeypatch.setattr(hook, "find_wezterm_cli", lambda: "wezterm")

    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="err")

    monkeypatch.setattr(hook.subprocess, "run", _fake_run)

    ok = hook.send_via_wezterm("pane-1", "hello", {})

    assert ok is False
    assert len(calls) == 2
    assert "send-text" in calls[0]
    assert "--no-paste" in calls[1]


def test_completion_hook_wezterm_send_key_fallbacks_to_cr(monkeypatch) -> None:
    hook = _load_script_module("ccb_completion_hook_wezterm_submit", REPO_ROOT / "bin" / "ccb-completion-hook")

    monkeypatch.setattr(hook, "find_wezterm_cli", lambda: "wezterm")

    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "send-key" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if "--no-paste" in cmd and kwargs.get("input") == b"\r":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hook.subprocess, "run", _fake_run)

    ok = hook.send_via_wezterm("pane-1", "hello", {})

    assert ok is True
    assert any("send-key" in call for call in calls)
    assert any("--no-paste" in call for call in calls)


def test_completion_hook_tmux_enter_retries_with_variants(monkeypatch) -> None:
    hook = _load_script_module("ccb_completion_hook_tmux", REPO_ROOT / "bin" / "ccb-completion-hook")

    key_calls: list[str] = []

    def _fake_run(cmd, **kwargs):
        if cmd[:3] == ["tmux", "display-message", "-p"]:
            return SimpleNamespace(returncode=0, stdout="0", stderr="")
        if cmd[:3] == ["tmux", "load-buffer", "-b"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["tmux", "paste-buffer", "-p"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["tmux", "send-keys", "-t"]:
            key = cmd[-1]
            key_calls.append(key)
            rc = 0 if key == "Return" else 1
            return SimpleNamespace(returncode=rc, stdout="", stderr="")
        if cmd[:3] == ["tmux", "delete-buffer", "-b"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hook.subprocess, "run", _fake_run)
    monkeypatch.setenv("CCB_TMUX_ENTER_DELAY", "0")
    monkeypatch.setenv("CCB_TMUX_ENTER_RETRY_DELAY", "0")

    ok = hook.send_via_tmux("%1", "hello")

    assert ok is True
    assert key_calls[:2] == ["Enter", "Return"]


def test_maybe_start_unified_daemon_honors_autostart_opt_out(monkeypatch, tmp_path: Path) -> None:
    ask = _load_script_module("ask_script_opt_out", REPO_ROOT / "bin" / "ask")
    popen_calls: list[dict] = []

    monkeypatch.setenv("CCB_ASKD_AUTOSTART", "0")
    monkeypatch.setattr(askd_runtime, "state_file_candidates", lambda name, **_kwargs: [tmp_path / name])
    monkeypatch.setattr(askd_daemon, "ping_daemon", lambda **kwargs: False)
    monkeypatch.setattr(ask.subprocess, "Popen", lambda *args, **kwargs: popen_calls.append(kwargs))

    assert ask._maybe_start_unified_daemon() is False
    assert popen_calls == []


def test_maybe_start_unified_daemon_scrubs_parent_env(monkeypatch, tmp_path: Path) -> None:
    ask = _load_script_module("ask_script_scrub", REPO_ROOT / "bin" / "ask")
    captured: dict[str, object] = {}

    class _DummyProcess:
        pass

    def _fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _DummyProcess()

    monkeypatch.delenv("CCB_ASKD_AUTOSTART", raising=False)
    monkeypatch.setenv("CCB_PARENT_PID", "12345")
    monkeypatch.setenv("CCB_MANAGED", "1")
    monkeypatch.setattr(askd_runtime, "state_file_candidates", lambda name, **_kwargs: [tmp_path / name])
    monkeypatch.setattr(ask, "_find_running_unified_state_file", lambda **_kwargs: None)
    monkeypatch.setattr(askd_daemon, "ping_daemon", lambda **kwargs: True)
    monkeypatch.setattr(ask.subprocess, "Popen", _fake_popen)

    assert ask._maybe_start_unified_daemon() is True
    child_env = captured["kwargs"]["env"]
    assert "CCB_PARENT_PID" not in child_env
    assert "CCB_MANAGED" not in child_env
    assert child_env["CCB_RUN_DIR"] == str(tmp_path)


def test_launcher_waits_long_enough_for_slow_askd_start(monkeypatch, tmp_path: Path, capsys) -> None:
    ccb = _load_script_module("ccb_script_slow_askd", REPO_ROOT / "ccb")
    clock = {"now": 0.0}

    class _DummyProcess:
        def poll(self):
            return None

    launcher = object.__new__(ccb.AILauncher)
    launcher.providers = ["codex"]
    launcher.script_dir = REPO_ROOT
    launcher._askd_checked = False
    launcher._daemon_proc_lock = threading.Lock()
    launcher._daemon_proc = None
    launcher.ccb_pid = 12345

    monkeypatch.delenv("CCB_ASKD_START_TIMEOUT_S", raising=False)
    monkeypatch.setattr(askd_runtime, "state_file_path", lambda _name: tmp_path / "askd.json")
    monkeypatch.setattr(askd_daemon, "ping_daemon", lambda **_kwargs: clock["now"] >= 3.0)
    monkeypatch.setattr(
        askd_daemon,
        "read_state",
        lambda **_kwargs: {"host": "127.0.0.1", "port": 31337},
    )
    monkeypatch.setattr(ccb.subprocess, "Popen", lambda *_args, **_kwargs: _DummyProcess())
    monkeypatch.setattr(ccb.time, "time", lambda: clock["now"])
    monkeypatch.setattr(ccb.time, "sleep", lambda seconds: clock.update(now=clock["now"] + seconds))

    launcher._maybe_start_provider_daemon("codex")

    output = capsys.readouterr().out
    assert "askd started at 127.0.0.1:31337" in output
    assert "not reachable yet" not in output
    assert clock["now"] >= 3.0


def test_launcher_reuses_owned_child_but_requires_rpc_readiness(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    ccb = _load_script_module("ccb_script_owned_askd_state", REPO_ROOT / "ccb")
    unified_state = tmp_path / "askd.json"
    legacy_state = tmp_path / "caskd.json"
    popen_calls: list[object] = []
    ping_state_files: list[Path] = []
    clock = {"now": 0.0}

    class _DummyProcess:
        pid = 2468

        def poll(self):
            return None

    proc = _DummyProcess()
    launcher = object.__new__(ccb.AILauncher)
    launcher.providers = ["codex"]
    launcher.script_dir = REPO_ROOT
    launcher._askd_checked = False
    launcher._daemon_proc_lock = threading.Lock()
    launcher._daemon_proc = None
    launcher.ccb_pid = 12345

    def _fake_ping(**kwargs):
        ping_state_files.append(kwargs["state_file"])
        return False

    monkeypatch.setenv("CCB_ASKD_STATE_FILE", str(unified_state))
    monkeypatch.setenv("CCB_CASKD_STATE_FILE", str(legacy_state))
    monkeypatch.setenv("CCB_ASKD_START_TIMEOUT_S", "2")
    monkeypatch.setattr(askd_daemon, "ping_daemon", _fake_ping)
    monkeypatch.setattr(
        askd_daemon,
        "read_state",
        lambda **_kwargs: {
            "pid": proc.pid,
            "parent_pid": launcher.ccb_pid,
            "managed": True,
            "host": "127.0.0.1",
            "port": 31337,
        },
    )
    monkeypatch.setattr(
        ccb.subprocess,
        "Popen",
        lambda *_args, **_kwargs: popen_calls.append(object()) or proc,
    )
    monkeypatch.setattr(ccb.time, "time", lambda: clock["now"])
    monkeypatch.setattr(ccb.time, "sleep", lambda seconds: clock.update(now=clock["now"] + seconds))

    launcher._maybe_start_provider_daemon("codex")
    launcher._maybe_start_provider_daemon("codex")

    output = capsys.readouterr().out
    assert len(popen_calls) == 1
    assert ping_state_files
    assert set(ping_state_files) == {unified_state}
    assert str(legacy_state) not in output
    assert "askd started" not in output
    assert "askd already running" not in output
    assert output.count("askd process is running, but readiness state is unavailable") == 2
    assert "exit code 2" not in output


def test_launcher_treats_lock_loser_as_existing_daemon(monkeypatch, tmp_path: Path, capsys) -> None:
    ccb = _load_script_module("ccb_script_askd_lock_loser", REPO_ROOT / "ccb")
    ping_results = iter([False, False, True])

    class _LockLoserProcess:
        pid = 1357

        def poll(self):
            return 2

    launcher = object.__new__(ccb.AILauncher)
    launcher.providers = ["codex"]
    launcher.script_dir = REPO_ROOT
    launcher._askd_checked = False
    launcher._daemon_proc_lock = threading.Lock()
    launcher._daemon_proc = None
    launcher.ccb_pid = 12345

    monkeypatch.setenv("CCB_ASKD_STATE_FILE", str(tmp_path / "askd.json"))
    monkeypatch.setattr(askd_daemon, "ping_daemon", lambda **_kwargs: next(ping_results))
    monkeypatch.setattr(
        askd_daemon,
        "read_state",
        lambda **_kwargs: {"host": "127.0.0.1", "port": 31337},
    )
    monkeypatch.setattr(ccb.subprocess, "Popen", lambda *_args, **_kwargs: _LockLoserProcess())

    launcher._maybe_start_provider_daemon("codex")

    output = capsys.readouterr().out
    assert "askd already running at 127.0.0.1:31337" in output
    assert "failed to start" not in output
    assert "exit code 2" not in output


def test_codex_log_reader_keeps_bound_session(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    work_dir = tmp_path / "repo"
    work_dir.mkdir()
    preferred = root / "2026" / "abc-session.jsonl"
    newer = root / "2026" / "other-session.jsonl"
    preferred.parent.mkdir(parents=True)

    meta = json.dumps({"type": "session_meta", "payload": {"cwd": str(work_dir)}}) + "\n"
    preferred.write_text(meta, encoding="utf-8")
    newer.write_text(meta, encoding="utf-8")
    newer.touch()

    reader = CodexLogReader(
        root=root,
        log_path=preferred,
        session_id_filter="abc",
        work_dir=work_dir,
    )

    assert reader.current_log_path() == preferred


def test_gemini_adapter_reports_cancelled_status(monkeypatch, tmp_path: Path) -> None:
    from askd.adapters import gemini as gemini_mod

    notifications: list[dict] = []

    class _Session:
        work_dir = str(tmp_path)
        gemini_session_path = None
        data = {}

        def ensure_pane(self):
            return True, "pane-1"

    class _Backend:
        def send_text(self, pane_id: str, prompt: str) -> None:
            return None

        def is_alive(self, pane_id: str) -> bool:
            return True

    class _Reader:
        def __init__(self, work_dir: Path):
            self.session_path = tmp_path / "session.json"

        def set_preferred_session(self, path: Path) -> None:
            return None

        def capture_state(self) -> dict:
            return {"msg_count": 0, "session_path": self.session_path}

        def wait_for_message(self, state: dict, timeout: float):
            return "", {"msg_count": 1, "session_path": self.session_path}

    monkeypatch.setattr(gemini_mod, "load_project_session", lambda work_dir, instance=None: _Session())
    monkeypatch.setattr(gemini_mod, "get_backend_for_session", lambda data: _Backend())
    monkeypatch.setattr(gemini_mod, "GeminiLogReader", _Reader)
    monkeypatch.setattr(gemini_mod, "_detect_request_cancelled", lambda *args, **kwargs: True)
    monkeypatch.setattr(gemini_mod, "notify_completion", lambda **kwargs: notifications.append(kwargs))
    monkeypatch.setattr(gemini_mod, "_write_log", lambda line: None)

    req = ProviderRequest(
        client_id="c1",
        work_dir=str(tmp_path),
        timeout_s=5.0,
        quiet=False,
        message="hello",
        caller="claude",
    )
    task = QueuedTask(
        request=req,
        created_ms=0,
        req_id="req-1",
        done_event=threading.Event(),
        cancel_event=threading.Event(),
    )

    result = GeminiAdapter().handle_task(task)

    assert result.status == "cancelled"
    assert notifications[0]["status"] == "cancelled"


def test_claude_adapter_honors_cancel_event(monkeypatch, tmp_path: Path) -> None:
    from askd.adapters import claude as claude_mod

    notifications: list[dict] = []

    class _Session:
        work_dir = str(tmp_path)
        claude_session_path = None
        data = {}

        def ensure_pane(self):
            return True, "pane-1"

    class _Backend:
        def send_text(self, pane_id: str, prompt: str) -> None:
            return None

        def is_alive(self, pane_id: str) -> bool:
            return True

    class _Reader:
        def __init__(self, work_dir: Path, use_sessions_index: bool = True):
            self.work_dir = work_dir

        def set_preferred_session(self, path: Path) -> None:
            return None

        def capture_state(self) -> dict:
            return {}

        def wait_for_events(self, state: dict, timeout: float):
            return [], state

    monkeypatch.setattr(claude_mod, "load_project_session", lambda work_dir, instance=None: _Session())
    monkeypatch.setattr(claude_mod, "get_backend_for_session", lambda data: _Backend())
    monkeypatch.setattr(claude_mod, "ClaudeLogReader", _Reader)
    monkeypatch.setattr(claude_mod, "notify_completion", lambda **kwargs: notifications.append(kwargs))
    monkeypatch.setattr(claude_mod, "_write_log", lambda line: None)

    req = ProviderRequest(
        client_id="c1",
        work_dir=str(tmp_path),
        timeout_s=5.0,
        quiet=False,
        message="hello",
        caller="claude",
    )
    cancel_event = threading.Event()
    cancel_event.set()
    task = QueuedTask(
        request=req,
        created_ms=0,
        req_id="req-1",
        done_event=threading.Event(),
        cancelled=True,
        cancel_event=cancel_event,
    )

    result = ClaudeAdapter().handle_task(task)

    assert result.status == "cancelled"
    assert notifications[0]["status"] == "cancelled"


def test_claude_adapter_does_not_capture_assistant_without_anchor(tmp_path: Path, monkeypatch) -> None:
    from askd.adapters import claude as claude_mod

    class _Clock:
        def __init__(self) -> None:
            self.now = 1000.0

        def time(self) -> float:
            self.now += 0.6
            return self.now

    class _Reader:
        def wait_for_events(self, state: dict, timeout: float):
            return [("assistant", "Unrelated WP-CLI explanation from the local conversation.")], state

    class _Backend:
        def is_alive(self, pane_id: str) -> bool:
            return True

    req = ProviderRequest(
        client_id="c1",
        work_dir=str(tmp_path),
        timeout_s=10.0,
        quiet=False,
        message="peer request",
        caller="claude",
    )
    task = QueuedTask(
        request=req,
        created_ms=0,
        req_id="req-1",
        done_event=threading.Event(),
    )

    clock = _Clock()
    monkeypatch.setattr(claude_mod.time, "time", clock.time)

    result = ClaudeAdapter()._wait_for_response(
        task,
        SimpleNamespace(work_dir=str(tmp_path)),
        "claude:test",
        0,
        _Reader(),
        {},
        _Backend(),
        "pane-1",
        deadline=1006.0,
    )

    assert result.anchor_seen is False
    assert result.done_seen is False
    assert result.reply == ""
    assert result.status == "incomplete"


def test_claude_adapter_delivery_only_returns_after_anchor_and_suppresses_hook(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from askd.adapters import claude as claude_mod

    notifications: list[dict] = []
    sent_prompts: list[str] = []

    class _Session:
        work_dir = str(tmp_path)
        claude_session_path = None
        data = {}

        def ensure_pane(self):
            return True, "pane-1"

    class _Backend:
        def send_text(self, pane_id: str, prompt: str) -> None:
            sent_prompts.append(prompt)

        def is_alive(self, pane_id: str) -> bool:
            return True

    class _Reader:
        def __init__(self, work_dir: Path, use_sessions_index: bool = True):
            self.work_dir = work_dir

        def set_preferred_session(self, path: Path) -> None:
            return None

        def capture_state(self) -> dict:
            return {}

        def wait_for_events(self, state: dict, timeout: float):
            return [("user", "CCB_REQ_ID: req-1\n\nhello")], state

    monkeypatch.setattr(claude_mod, "load_project_session", lambda work_dir, instance=None: _Session())
    monkeypatch.setattr(claude_mod, "get_backend_for_session", lambda data: _Backend())
    monkeypatch.setattr(claude_mod, "ClaudeLogReader", _Reader)
    monkeypatch.setattr(claude_mod, "notify_completion", lambda **kwargs: notifications.append(kwargs))
    monkeypatch.setattr(claude_mod, "_write_log", lambda line: None)

    req = ProviderRequest(
        client_id="c1",
        work_dir=str(tmp_path),
        timeout_s=30.0,
        quiet=False,
        message="hello\n\nCCB_REPLY_TARGET: /tmp/sender",
        caller="claude",
        delivery_only=True,
        suppress_completion_hook=True,
    )
    task = QueuedTask(
        request=req,
        created_ms=0,
        req_id="req-1",
        done_event=threading.Event(),
    )

    result = ClaudeAdapter().handle_task(task)

    assert result.exit_code == 0
    assert result.reply == "Peer message delivered."
    assert result.anchor_seen is True
    assert result.extra == {"confirmation": "observed"}
    assert result.done_seen is False
    assert notifications == []
    assert "CCB_REQ_ID: req-1" in sent_prompts[0]
    assert "CCB_DONE: req-1" not in sent_prompts[0]


def test_claude_adapter_delivery_only_accepts_successful_send_without_log_anchor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from askd.adapters import claude as claude_mod

    class _Session:
        work_dir = str(tmp_path)
        claude_session_path = None
        data = {}

        def ensure_pane(self):
            return True, "pane-1"

    class _Backend:
        def send_text(self, pane_id: str, prompt: str) -> None:
            return None

        def is_alive(self, pane_id: str) -> bool:
            return True

    class _Reader:
        def __init__(self, work_dir: Path, use_sessions_index: bool = True):
            self.work_dir = work_dir

        def set_preferred_session(self, path: Path) -> None:
            return None

        def capture_state(self) -> dict:
            return {}

        def wait_for_events(self, state: dict, timeout: float):
            return [], state

        def current_session_path(self):
            return None

    monkeypatch.setenv("CCB_LASKD_DELIVERY_ACK_TIMEOUT", "0.05")
    monkeypatch.setattr(claude_mod, "load_project_session", lambda work_dir, instance=None: _Session())
    monkeypatch.setattr(claude_mod, "get_backend_for_session", lambda data: _Backend())
    monkeypatch.setattr(claude_mod, "ClaudeLogReader", _Reader)
    monkeypatch.setattr(claude_mod, "_write_log", lambda line: None)

    req = ProviderRequest(
        client_id="c1",
        work_dir=str(tmp_path),
        timeout_s=30.0,
        quiet=False,
        message="hello",
        caller="claude",
        delivery_only=True,
        suppress_completion_hook=True,
    )
    task = QueuedTask(
        request=req,
        created_ms=0,
        req_id="req-1",
        done_event=threading.Event(),
    )

    result = ClaudeAdapter().handle_task(task)

    assert result.exit_code == 0
    assert result.reply == "Peer message accepted."
    assert result.anchor_seen is False
    assert result.status == "completed"
    assert result.extra == {"confirmation": "sent"}
