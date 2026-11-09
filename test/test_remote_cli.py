from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CCB_REMOTE = REPO_ROOT / "bin" / "ccb-remote"
FAKE_SSH = REPO_ROOT / "test" / "fixtures" / "fake_ssh.py"
REQUEST_ID = "20261109-182536-922-2626995-habc1"


def _write_hosts(tmp_path: Path) -> None:
    identity = tmp_path / "id_ed25519"
    identity.write_text("private", encoding="utf-8")
    known = tmp_path / "known_hosts"
    known.write_text("example.test ssh-ed25519 AAAA\n", encoding="utf-8")
    path = Path.home() / ".ccb" / "remote" / "hosts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hosts": {
                    "testhost": {
                        "ssh_target": "relay@example.test",
                        "port": 2222,
                        "identity_file": str(identity),
                        "known_hosts": str(known),
                        "issuer_token": "abc1",
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _write_receipt(host: str = "testhost") -> Path:
    path = Path.home() / ".ccb" / "remote" / "outbound" / f"{REQUEST_ID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "request_id": REQUEST_ID,
                "host": host,
                "dest_project": "example-project",
                "dest_provider": "claude",
                "dest_session": "",
                "digest": "a" * 64,
                "schema_version": 1,
                "intent": "notify",
                "caller": {"caller": "codex"},
                "created": "2026-11-09T18:00:00+00:00",
                "state": "acceptance-unknown",
                "history": [{"state": "acceptance-unknown", "at": "2026-11-09T18:00:01+00:00"}],
                "remote_state": "",
            }
        ),
        encoding="utf-8",
    )
    return path


def _run(args: list[str], *, mode: str = "status_delivered", env_extra: dict | None = None):
    env = dict(os.environ)
    env["CCB_SSH_BIN"] = str(FAKE_SSH)
    env["CCB_FAKE_SSH_MODE"] = mode
    log = Path.home() / ".ccb" / "remote" / "argv.jsonl"
    env["CCB_FAKE_SSH_LOG"] = str(log)
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [sys.executable, str(CCB_REMOTE), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    calls = []
    if log.exists():
        calls = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return result, calls, log


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    _write_hosts(tmp_path)


def test_usage_without_arguments_exits_nonzero():
    result, calls, _log = _run([])
    assert result.returncode == 1
    assert "Usage:" in result.stderr
    assert calls == []


def test_help_exits_zero():
    result, calls, _log = _run(["--help"])
    assert result.returncode == 0
    assert "ccb-remote status" in result.stdout
    assert calls == []


def test_status_recovers_the_destination_state():
    _write_receipt()
    result, calls, _log = _run(["status", REQUEST_ID], mode="status_delivered",
                               env_extra={"CCB_FAKE_SSH_STATE": "delivered"})
    assert result.returncode == 0, result.stderr
    assert "state=delivered" in result.stdout
    assert len(calls) == 1 and calls[0][-1] == "relay@example.test"
    receipt = json.loads((Path.home() / ".ccb" / "remote" / "outbound" / f"{REQUEST_ID}.json").read_text())
    assert receipt["state"] == "delivered"
    assert receipt["remote_state"] == "delivered"
    assert receipt["remote_history"][-1]["state"] == "delivered"


def test_status_unknown_or_not_authorized_never_says_more():
    _write_receipt()
    result, _calls, _log = _run(["status", REQUEST_ID], mode="status_not_found")
    assert result.returncode == 1
    assert result.stdout.strip() == "state=unknown-or-not-authorized"
    assert "no such request" not in result.stdout + result.stderr


def test_status_without_a_receipt_is_refused_before_ssh():
    result, calls, _log = _run(["status", REQUEST_ID])
    assert result.returncode == 1
    assert "no outbound receipt" in result.stderr
    assert calls == []


def test_status_rejects_a_malformed_id():
    result, calls, _log = _run(["status", "../../etc/passwd"])
    assert result.returncode == 1
    assert calls == []


def test_status_requires_exactly_one_argument():
    result, calls, _log = _run(["status"])
    assert result.returncode == 1
    assert "exactly one" in result.stderr
    assert calls == []


def test_list_prints_destinations():
    result, calls, _log = _run(["list", "testhost"], mode="list_ok")
    assert result.returncode == 0, result.stderr
    assert "example-project" in result.stdout
    assert "claude=present" in result.stdout
    assert "codex=absent" in result.stdout
    assert calls[0][-1] == "relay@example.test"


def test_list_refusal_is_an_error_not_an_empty_list():
    result, _calls, _log = _run(["list", "testhost"], mode="list_refused")
    assert result.returncode == 1
    assert "refused" in result.stderr
    assert "example-project" not in result.stdout


def test_list_unknown_host_is_refused():
    result, calls, _log = _run(["list", "ghost"], mode="list_ok")
    assert result.returncode == 1
    assert "not configured" in result.stderr
    assert calls == []


def test_unknown_command_is_refused():
    result, calls, _log = _run(["resubmit", REQUEST_ID])
    assert result.returncode == 1
    assert "Unknown ccb-remote command" in result.stderr
    assert calls == []


def test_no_automatic_resubmit_command_exists():
    """M1 has no resubmit path: `status` is the only recovery the CLI offers."""
    text = CCB_REMOTE.read_text(encoding="utf-8")
    for command in ('command == "resubmit"', "def _cmd_resubmit"):
        assert command not in text


@pytest.mark.parametrize("bad_id", ["", "not-an-id", "20261109-182536-922-2626995", "20261109-182536-922-2626995-habc1\n"])
def test_status_rejects_a_malformed_id_without_a_traceback(bad_id):
    result, calls, _log = _run(["status", bad_id])
    assert result.returncode == 1
    assert "not a host-qualified request id" in result.stderr
    assert "Traceback" not in result.stderr
    assert calls == []
