from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

from ccb_runtime_status import ProviderRuntimeStatus


ROOT = Path(__file__).resolve().parents[1]


def _load_ping_module():
    path = ROOT / "bin" / "ccb-ping"
    loader = importlib.machinery.SourceFileLoader("ccb_ping_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _status(*, mounted: bool, reason: str = "") -> ProviderRuntimeStatus:
    return ProviderRuntimeStatus(
        key="claude",
        provider="claude",
        capable=True,
        configured=True,
        registered=True,
        pane_alive=mounted,
        session_bound=mounted,
        daemon_online=mounted,
        mounted=mounted,
        reason=reason,
    )


def test_ccb_ping_uses_host_runtime_status_in_managed_codex(monkeypatch, capsys) -> None:
    ping = _load_ping_module()
    monkeypatch.setattr(sys, "argv", ["ccb-ping", "claude"])
    monkeypatch.setattr(ping, "inside_managed_codex_sandbox", lambda: True)
    monkeypatch.setattr(ping, "provider_status_for_target", lambda *_args, **_kwargs: _status(mounted=True))

    rc = ping.main()

    assert rc == 0
    assert "host runtime verified" in capsys.readouterr().out


def test_ccb_ping_reports_host_runtime_failure_reason(monkeypatch, capsys) -> None:
    ping = _load_ping_module()
    monkeypatch.setattr(sys, "argv", ["ccb-ping", "claude"])
    monkeypatch.setattr(ping, "inside_managed_codex_sandbox", lambda: True)
    monkeypatch.setattr(
        ping,
        "provider_status_for_target",
        lambda *_args, **_kwargs: _status(mounted=False, reason="session_unbound"),
    )

    rc = ping.main()

    assert rc == 1
    assert "session_unbound" in capsys.readouterr().out
