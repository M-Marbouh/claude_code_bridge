from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path


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


def test_bridge_diagnostics_print_when_reply_empty(monkeypatch, capsys) -> None:
    bridge = _load_bridge_module()
    target = {
        "index": 1,
        "work_dir": "/tmp/project",
        "ccb_project_id": "abcd1234",
        "providers": {"claude": {"alive": True}},
    }

    monkeypatch.setattr(bridge, "_load_targets", lambda: [target])
    monkeypatch.setattr(bridge, "_acquire_lock", lambda _hash: (_Lock(), _Fcntl()))
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
