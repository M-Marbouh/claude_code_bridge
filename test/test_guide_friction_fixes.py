"""Regressions for the four product-friction fixes found by the guide walkthrough."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import remote_client
from remote_client import (
    EXIT_NOT_DELIVERED,
    STATE_ACCEPTED,
    STATE_ACCEPTANCE_UNKNOWN,
    STATE_NOT_DELIVERED,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_SSH = REPO_ROOT / "test" / "fixtures" / "fake_ssh.py"
HOST_ALIAS = "testhost"


@pytest.fixture()
def hosts(tmp_path: Path, monkeypatch) -> Path:
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
                    HOST_ALIAS: {
                        "ssh_target": "bob@example.test",
                        "port": 2222,
                        "identity_file": "~/.ccb/remote/keys/id_ed25519",
                        "known_hosts": "~/.ccb/remote/known_hosts",
                        "issuer_token": "abc1",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    keys = Path.home() / ".ccb" / "remote" / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    (keys / "id_ed25519").write_text("private", encoding="utf-8")
    (Path.home() / ".ccb" / "remote" / "known_hosts").write_text(
        "example.test ssh-ed25519 AAAA\n", encoding="utf-8"
    )
    monkeypatch.setenv("CCB_SSH_BIN", str(FAKE_SSH))
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "ack_ok")
    return tmp_path


def _submit(**kwargs):
    return remote_client.submit(
        HOST_ALIAS,
        "example-project",
        "codex",
        "hello from the laptop",
        out=open(os.devnull, "w"),
        err=open(os.devnull, "w"),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 1. ssh's own error reaches the reason, bounded and sanitised
# --------------------------------------------------------------------------- #


def test_not_delivered_reason_carries_ssh_error(hosts, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "exit255_stderr")
    result = _submit()
    assert result.state == STATE_NOT_DELIVERED
    assert result.exit_code == EXIT_NOT_DELIVERED
    assert "ssh exited 255" in result.reason, result.reason
    assert "bob@example.test: Permission denied (publickey)." in result.reason, result.reason


def test_successful_delivery_reason_is_unchanged(hosts):
    """Positive control: a delivered request is untouched by the diagnostics."""
    result = _submit()
    assert result.state == STATE_ACCEPTED
    assert result.reason == ""
    assert result.exit_code == 0


def test_chatty_stderr_is_bounded_sanitised_and_last_lines_only(hosts, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "exit255_chatty")
    result = _submit()
    assert result.state == STATE_NOT_DELIVERED
    summary = result.reason.split("ssh exited 255:", 1)[-1].strip()
    assert 0 < len(summary) <= 200, (len(summary), summary)
    assert all(32 <= ord(char) <= 126 for char in summary), summary
    # The final line is what ssh ended with, not the earlier noise.
    assert "Permission denied (publickey)" in summary, summary
    assert "line 0 " not in summary


def test_stderr_summary_helper_is_bounded_and_printable():
    session = remote_client.SshSession.__new__(remote_client.SshSession)
    session._stderr_tail = "one\ntwo\x1b[31m three\n" + "y" * 400
    summary = session.stderr_summary()
    assert len(summary) <= 200
    assert all(32 <= ord(char) <= 126 for char in summary)
    # The LAST non-empty line is what ssh ended with; the first line is dropped.
    assert "yyyy" in summary, summary
    assert "one" not in summary, summary


def test_state_rules_are_unchanged_by_the_diagnostics(hosts, monkeypatch):
    """A written-but-unacknowledged submit stays acceptance-unknown."""
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "eof_after_submit")
    result = _submit()
    assert result.state == STATE_ACCEPTANCE_UNKNOWN
    assert result.exit_code == 2


# --------------------------------------------------------------------------- #
# 2. Remote asks default their intent to notify
# --------------------------------------------------------------------------- #


def _load_ask():
    import importlib.machinery
    import importlib.util

    path = REPO_ROOT / "bin" / "ask"
    loader = importlib.machinery.SourceFileLoader("ask_intent_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize("extra", [[], ["--notify"], ["--background"]])
def test_remote_asks_always_send_notify_intent(monkeypatch, capsys, extra):
    ask = _load_ask()
    import remote_client as client_module

    monkeypatch.setenv("CCB_CALLER", "codex")
    seen: list[dict] = []

    def fake_submit(host, project, provider, text, **kwargs):
        seen.append({"host": host, "project": project, "provider": provider, "text": text, **kwargs})
        return client_module.RemoteResult(
            request_id="20261109-182536-922-2626995-habc1",
            host=host,
            state=client_module.STATE_ACCEPTED,
            exit_code=0,
        )

    monkeypatch.setattr(client_module, "submit", fake_submit)
    rc = ask.main(["ask", "codex", "--peer", "testhost::example-project", *extra, "hi"])

    assert rc == 0
    assert seen and seen[0]["intent"] == "notify", seen


def test_explicit_wait_is_still_refused_for_remote(monkeypatch, capsys):
    ask = _load_ask()
    import remote_client as client_module

    monkeypatch.setenv("CCB_CALLER", "codex")
    monkeypatch.setattr(
        client_module, "submit", lambda *a, **k: pytest.fail("refused flags must not submit")
    )
    rc = ask.main(["ask", "codex", "--peer", "testhost::example-project", "--wait", "hi"])
    assert rc == 1
    assert "not available for cross-machine delivery" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 3. The CCB_CALLER warning is printed once on the remote path
# --------------------------------------------------------------------------- #


def test_caller_warning_is_printed_once(monkeypatch, capsys):
    ask = _load_ask()
    import remote_client as client_module

    for name in ("CCB_CALLER", "CCB_CALLER_DEFAULT", "TMUX_PANE", "WEZTERM_PANE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ask, "_detect_caller", lambda: "")
    monkeypatch.setattr(
        client_module,
        "submit",
        lambda host, project, provider, text, **kwargs: client_module.RemoteResult(
            request_id="20261109-182536-922-2626995-habc1",
            host=host,
            state=client_module.STATE_ACCEPTED,
            exit_code=0,
        ),
    )
    rc = ask.main(["ask", "codex", "--peer", "testhost::example-project", "hi"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.err.count("CCB_CALLER not set") == 1, captured.err


# --------------------------------------------------------------------------- #
# 4. `~` in hosts.json paths
# --------------------------------------------------------------------------- #


def test_hosts_paths_expand_a_leading_tilde(hosts):
    loaded = remote_client.load_hosts()
    host = loaded[HOST_ALIAS]
    home = str(Path.home())
    assert host.identity_file == f"{home}/.ccb/remote/keys/id_ed25519"
    assert host.known_hosts == f"{home}/.ccb/remote/known_hosts"
    assert not host.identity_file.startswith("~")


@pytest.mark.parametrize("bad", ["relative/known_hosts", "~/does-not-exist/known_hosts"])
def test_relative_or_missing_paths_are_still_refused(hosts, bad):
    path = Path.home() / ".ccb" / "remote" / "hosts.json"
    document = json.loads(path.read_text())
    document["hosts"][HOST_ALIAS]["known_hosts"] = bad
    path.write_text(json.dumps(document))
    with pytest.raises(remote_client.RemoteConfigError):
        remote_client.load_hosts()


def test_non_frame_response_text_is_reported(hosts, monkeypatch):
    """A nologin shell (or a login banner) prints text on the session stream."""
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "text_not_frame")
    result = _submit()
    assert result.state == STATE_NOT_DELIVERED
    assert "response text: This account is currently not available." in result.reason, result.reason
    assert "malformed response" in result.reason


def test_non_frame_preview_helper_is_bounded_and_printable():
    session = remote_client.SshSession.__new__(remote_client.SshSession)
    session._stream_head = "ok\\x1b[31m " + "z" * 400
    preview = session.response_preview()
    assert len(preview) <= 120
    assert all(32 <= ord(char) <= 126 for char in preview)


def test_frame_parse_errors_keep_the_ssh_summary_too(hosts, monkeypatch):
    """Positive control: a pure ssh failure still leads with `ssh exited …`."""
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "exit255_stderr")
    result = _submit()
    assert result.reason.startswith("ssh exited 255:")
