from __future__ import annotations

import importlib.util
import importlib.machinery
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import askd_timeout
from askd.adapters.base import ProviderResult
from askd.daemon import UnifiedAskDaemon


ROOT = Path(__file__).resolve().parents[1]


def _load_ask_module():
    loader = importlib.machinery.SourceFileLoader("ccb_ask_timeout_test", str(ROOT / "bin" / "ask"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_codex_timeout_environment_values_fall_back_for_invalid_or_nonpositive_values(monkeypatch) -> None:
    monkeypatch.setenv("CCB_CODEX_IDLE_TIMEOUT_S", "0")
    monkeypatch.setenv("CCB_CODEX_MAX_WAIT_S", "not-a-number")

    assert askd_timeout.codex_idle_timeout_s() == 1800
    assert askd_timeout.codex_max_wait_s() == 21600


def test_daemon_old_client_request_keeps_explicit_wall_clock_budget(monkeypatch) -> None:
    observed = {}
    monkeypatch.setenv("CCB_CODEX_MAX_WAIT_S", "23")
    monkeypatch.setenv("CCB_CODEX_IDLE_TIMEOUT_S", "17")

    class _WaitEvent:
        def wait(self, timeout=None):
            observed["wait_timeout"] = timeout
            return True

        def is_set(self):
            return True

    task = SimpleNamespace(
        req_id="task",
        done_event=_WaitEvent(),
        result=ProviderResult(exit_code=0, reply="ok", req_id="task", session_key="s", done_seen=True),
        cancelled=False,
        cancel_event=None,
    )

    class _Pool:
        def submit(self, provider, request):
            observed["request"] = request
            return task

    daemon = UnifiedAskDaemon.__new__(UnifiedAskDaemon)
    daemon.registry = SimpleNamespace(get=lambda provider: object())
    daemon.pool = _Pool()

    response = daemon._handle_request(
        {"id": "client", "provider": "codex", "caller": "claude", "timeout_s": 12}
    )

    assert response["exit_code"] == 0
    assert observed["request"].timeout_explicit is True
    assert observed["request"].max_wait_s == 23
    assert observed["request"].idle_timeout_s == 17
    assert observed["wait_timeout"] == 17


def test_daemon_progress_wait_budget_uses_max_wait_cap_plus_margin(monkeypatch) -> None:
    observed = {}
    monkeypatch.setenv("CCB_CODEX_MAX_WAIT_S", "90")
    monkeypatch.setenv("CCB_CODEX_IDLE_TIMEOUT_S", "80")

    class _WaitEvent:
        def wait(self, timeout=None):
            observed["wait_timeout"] = timeout
            return True

        def is_set(self):
            return True

    task = SimpleNamespace(
        req_id="task",
        done_event=_WaitEvent(),
        result=ProviderResult(exit_code=0, reply="ok", req_id="task", session_key="s", done_seen=True),
        cancelled=False,
        cancel_event=None,
    )

    class _Pool:
        def submit(self, provider, request):
            observed["request"] = request
            return task

    daemon = UnifiedAskDaemon.__new__(UnifiedAskDaemon)
    daemon.registry = SimpleNamespace(get=lambda provider: object())
    daemon.pool = _Pool()

    daemon._handle_request(
        {
            "id": "client",
            "provider": "codex",
            "caller": "claude",
            "timeout_s": 12,
            "timeout_explicit": False,
            "max_wait_s": 23,
            "idle_timeout_s": 7,
        }
    )

    assert observed["request"].timeout_explicit is False
    assert observed["request"].max_wait_s == 23
    assert observed["request"].idle_timeout_s == 7
    assert observed["wait_timeout"] == 28


def test_ask_rpc_budget_uses_codex_cap_only_for_default_timeout(monkeypatch) -> None:
    ask = _load_ask_module()
    askd_rpc = __import__("askd_rpc")
    observed = []
    monkeypatch.setenv("CCB_CODEX_MAX_WAIT_S", "23")
    monkeypatch.setenv("CCB_CODEX_IDLE_TIMEOUT_S", "7")
    monkeypatch.setattr(ask, "_caller_pane_info", lambda: ("", ""))
    monkeypatch.setattr(
        askd_rpc,
        "request_daemon",
        lambda state, request, **kwargs: observed.append((request, kwargs))
        or {"exit_code": 0, "reply": ""},
    )
    context = ask._UnifiedDaemonContext(Path("/state"), {"token": "token"}, Path("/work"))

    ask._send_via_unified_daemon(
        "codex", "message", 3600, False, "claude", daemon_context=context, timeout_explicit=False
    )
    ask._send_via_unified_daemon(
        "codex", "message", 12, False, "claude", daemon_context=context, timeout_explicit=True
    )

    assert observed[0][0]["timeout_explicit"] is False
    assert observed[0][0]["max_wait_s"] == 23
    assert observed[0][0]["idle_timeout_s"] == 7
    assert observed[0][1]["response_timeout_s"] == 33
    assert observed[1][0]["timeout_explicit"] is True
    assert observed[1][1]["response_timeout_s"] == 22


def test_detached_launcher_preserves_default_and_explicit_timeout_modes(monkeypatch, tmp_path: Path) -> None:
    ask = _load_ask_module()
    launched = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temp"))
    Path(tempfile.gettempdir()).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CCB_CALLER", "claude")
    monkeypatch.setattr(ask, "_default_foreground", lambda: False)
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: False)
    monkeypatch.setattr(ask, "_preflight_target", lambda *args, **kwargs: True)
    monkeypatch.setattr(ask, "_require_caller", lambda: "claude")
    monkeypatch.setattr(ask, "_caller_pane_info", lambda: ("", ""))
    monkeypatch.setattr(ask, "inside_managed_codex_sandbox", lambda: False)
    monkeypatch.setattr(
        ask.subprocess,
        "Popen",
        lambda argv, **kwargs: launched.append(Path(argv[1]).read_text(encoding="utf-8"))
        or SimpleNamespace(pid=123),
    )

    assert ask.main(["ask", "codex", "hello"]) == 0
    assert ask.main(["ask", "codex", "--timeout", "77", "hello"]) == 0

    assert "--foreground --timeout 3600.0 --ccb-default-timeout" in launched[0]
    assert "--foreground --timeout 77.0 <" in launched[1]
    assert "--ccb-default-timeout" not in launched[1]
