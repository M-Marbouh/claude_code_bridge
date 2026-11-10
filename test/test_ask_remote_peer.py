from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_ask():
    path = REPO_ROOT / "bin" / "ask"
    loader = importlib.machinery.SourceFileLoader("ask_remote_peer_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _SubmitSpy:
    def __init__(self, result=None):
        self.calls: list[tuple] = []
        self.kwargs: list[dict] = []
        self.result = result
        self.error: Exception | None = None

    def __call__(self, host, project, provider, text, **kwargs):
        import remote_client

        if self.error is not None:
            raise self.error
        self.calls.append((host, project, provider, text))
        self.kwargs.append(kwargs)
        if self.result is not None:
            return self.result
        return remote_client.RemoteResult(
            request_id="20261109-182536-922-2626995-habc1",
            host=host,
            state=remote_client.STATE_ACCEPTED,
            exit_code=remote_client.EXIT_ACCEPTED,
            remote_state="accepted-pending",
        )


def _patch_submit(ask, monkeypatch) -> _SubmitSpy:
    import remote_client

    spy = _SubmitSpy()
    monkeypatch.setattr(remote_client, "submit", spy)
    # Never touch the local peer machinery for a remote target.
    monkeypatch.setattr(
        ask, "_run_peer_bridge_foreground", lambda *a, **k: pytest.fail("local path taken")
    )
    monkeypatch.setattr(
        ask, "_run_peer_bridge_background", lambda *a, **k: pytest.fail("local path taken")
    )
    return spy


def _fail_config(monkeypatch):
    import remote_client

    def boom(*_args, **_kwargs):
        raise AssertionError("local project lookup must not run for a remote target")

    for name in ("_resolve_sender_work_dir", "_require_caller"):
        if hasattr(remote_client, name):  # pragma: no cover - not present
            monkeypatch.setattr(remote_client, name, boom)


# --------------------------------------------------------------------------- #
# Remote selector reaches the remote client, never the local path
# --------------------------------------------------------------------------- #


def test_provider_peer_form_with_host_is_remote(monkeypatch):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "codex")
    spy = _patch_submit(ask, monkeypatch)

    rc = ask.main(["ask", "codex", "--peer", "testhost::example-project", "--notify", "hello"])

    assert rc == 0
    assert spy.calls == [("testhost", "example-project", "codex", "hello")]
    assert spy.kwargs[0]["dest_session"] == ""
    assert spy.kwargs[0]["intent"] == "notify"
    assert spy.kwargs[0]["caller_claims"]["caller"] == "codex"
    assert spy.kwargs[0]["caller_claims"]["verified"] is False


def test_bare_peer_form_with_host_is_remote(monkeypatch):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "claude")
    spy = _patch_submit(ask, monkeypatch)

    rc = ask._handle_peer_mode(["--peer", "testhost::other-project", "--notify", "hi"])

    assert rc == 0
    assert spy.calls == [("testhost", "other-project", "claude", "hi")]


def test_live_id_becomes_dest_session(monkeypatch):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "codex")
    spy = _patch_submit(ask, monkeypatch)

    rc = ask.main(
        ["ask", "codex", "--peer", "testhost::example-project", "--live-id", "live-b", "--notify", "hi"]
    )

    assert rc == 0
    assert spy.kwargs[0]["dest_session"] == "live-b"


def test_to_role_lead_is_allowed_and_recorded_as_a_claim(monkeypatch):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "codex")
    spy = _patch_submit(ask, monkeypatch)

    rc = ask.main(
        ["ask", "codex", "--peer", "testhost::example-project", "--to-role", "lead", "--notify", "hi"]
    )

    assert rc == 0
    assert spy.kwargs[0]["caller_claims"]["role_assertion"] == "lead"


@pytest.mark.parametrize("role", ["implementer", "ratifier"])
def test_to_role_other_than_lead_is_refused_before_any_receipt(monkeypatch, capsys, role):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "codex")
    spy = _patch_submit(ask, monkeypatch)

    rc = ask.main(
        ["ask", "codex", "--peer", "testhost::example-project", "--to-role", role, "--notify", "hi"]
    )

    assert rc == 1
    assert "--to-role lead" in capsys.readouterr().err
    assert spy.calls == []


# --------------------------------------------------------------------------- #
# The local path is untouched
# --------------------------------------------------------------------------- #


def test_local_target_without_separator_keeps_the_bridge_path(monkeypatch, tmp_path):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "codex")
    import remote_client

    monkeypatch.setattr(
        remote_client, "submit", lambda *a, **k: pytest.fail("remote path taken for a local target")
    )
    seen: dict = {}

    def fake_background(target, provider, timeout, message, caller, intent, reply_to, sender_work_dir, **kw):
        seen.update(target=target, provider=provider, message=message, intent=intent, kwargs=kw)
        return 0

    monkeypatch.setattr(ask, "_run_peer_bridge_background", fake_background)
    monkeypatch.setattr(ask, "_default_foreground", lambda: False)

    # --notify keeps this test off the sender-project resolution path; the point
    # here is that a local target still reaches the local bridge, unchanged.
    rc = ask.main(["ask", "codex", "--peer", "/tmp/peer-project", "--notify", "hello"])

    assert rc == 0
    assert seen["target"] == "/tmp/peer-project"
    assert seen["message"] == "hello"
    # The local launcher signature is untouched: no new keyword arguments.
    assert "wait_explicit" not in seen["kwargs"]
    assert set(seen["kwargs"]) == {"live_id", "preserve_task_id", "to_role"}


def test_local_bridge_cmd_is_unchanged(monkeypatch):
    """The local peer command line is byte-identical to before T3."""
    ask = _load_ask()
    cmd = ask._peer_bridge_cmd(
        "/tmp/peer-project",
        "codex",
        3600.0,
        "%22",
        "tmux",
        "/tmp/sender",
        "claude",
        "notify",
        task_id="20261109-182536-922-2626995",
        reply_to="",
        foreground=True,
        live_id="",
        to_role="",
    )
    assert cmd[1:3] == [str(REPO_ROOT / "bin" / "ccb-bridge-ask"), "--target"]
    assert "--target" in cmd and cmd[cmd.index("--target") + 1] == "/tmp/peer-project"
    assert "::" not in " ".join(cmd)


# --------------------------------------------------------------------------- #
# Reply-implying flags are refused for remote targets
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "extra",
    [
        ["--reply-to", "20261109-182536-922-2626995"],
        ["--foreground"],
    ],
)
def test_reply_implying_flags_are_refused_for_remote(monkeypatch, capsys, extra):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "codex")
    spy = _patch_submit(ask, monkeypatch)

    rc = ask.main(
        ["ask", "codex", "--peer", "testhost::example-project", *extra, "--notify", "hi"]
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "not available for cross-machine delivery" in err
    assert spy.calls == []


def test_wait_and_notify_together_are_refused_as_contradictory(monkeypatch, capsys):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "codex")
    spy = _patch_submit(ask, monkeypatch)

    rc = ask.main(["ask", "codex", "--peer", "testhost::example-project", "--wait", "--notify", "hi"])

    assert rc == 1
    assert "contradict each other" in capsys.readouterr().err
    assert spy.calls == []


def test_wait_submits_reply_expected_and_waits(monkeypatch, capsys):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "codex")
    spy = _patch_submit(ask, monkeypatch)
    waited: list[tuple[str, float]] = []

    import remote_client

    monkeypatch.setattr(
        remote_client,
        "wait_for_answer",
        lambda request_id, timeout_s: waited.append((request_id, timeout_s))
        or remote_client.EXIT_WAITING,
    )
    monkeypatch.setattr(
        remote_client,
        "capture_caller_identity",
        lambda **kwargs: {"pane_id": "%0", "terminal": "tmux", "work_dir": "/home/user/project"},
    )

    rc = ask.main(["ask", "codex", "--peer", "testhost::example-project", "--wait", "hi"])

    assert rc == remote_client.EXIT_WAITING
    assert spy.calls and spy.kwargs[-1]["reply_expected"] is True
    assert spy.kwargs[-1]["intent"] == "wait"
    assert spy.kwargs[-1]["caller_identity"]["pane_id"] == "%0"
    assert spy.kwargs[-1]["poll_owner"] == ""
    assert waited == [("20261109-182536-922-2626995-habc1", 3600.0)]


def test_background_registers_the_polling_owner(monkeypatch, capsys):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "codex")
    spy = _patch_submit(ask, monkeypatch)
    registered: list[str] = []

    import remote_client

    monkeypatch.setattr(ask, "_remote_poll_owner", lambda _work_dir: "/home/user/.cache/ccb/projects/0123456789abcdef/askd.json")
    monkeypatch.setattr(
        remote_client, "register_watch", lambda request_id: registered.append(request_id) or (True, "")
    )
    monkeypatch.setattr(
        remote_client,
        "capture_caller_identity",
        lambda **kwargs: {"pane_id": "%0", "terminal": "tmux", "work_dir": "/home/user/project"},
    )

    rc = ask.main(["ask", "codex", "--peer", "testhost::example-project", "--background", "hi"])

    assert rc == 0, capsys.readouterr().err
    assert spy.kwargs[-1]["reply_expected"] is True
    assert spy.kwargs[-1]["intent"] == "background"
    assert spy.kwargs[-1]["poll_owner"].endswith("askd.json")
    assert registered == ["20261109-182536-922-2626995-habc1"]
    assert "CCB_BACKGROUND_SUBMITTED" in capsys.readouterr().out


def test_background_without_a_polling_owner_is_refused_before_submit(monkeypatch, capsys):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "codex")
    import remote_client

    spy = _patch_submit(ask, monkeypatch)
    spy.result = remote_client.RemoteResult(
        request_id="20261110-113136-582-89163-1-habc1",
        host="testhost",
        state=remote_client.STATE_NOT_DELIVERED,
        exit_code=remote_client.EXIT_NOT_DELIVERED,
        reason="background replies need CCB running here; use --wait",
    )
    monkeypatch.setattr(ask, "_remote_poll_owner", lambda _work_dir: "")
    monkeypatch.setattr(
        remote_client, "register_watch", lambda _rid: pytest.fail("must not register")
    )

    rc = ask.main(["ask", "codex", "--peer", "testhost::example-project", "--background", "hi"])

    assert rc == remote_client.EXIT_NOT_DELIVERED
    assert spy.kwargs[-1]["reply_expected"] is True
    assert spy.kwargs[-1]["poll_owner"] == ""


def test_remote_refuses_a_provider_that_is_not_claude_or_codex(monkeypatch, capsys):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "claude")
    spy = _patch_submit(ask, monkeypatch)

    rc = ask.main(["ask", "gemini", "--peer", "testhost::example-project", "--notify", "hi"])

    assert rc == 1
    assert "gemini" in capsys.readouterr().err  # refused before any remote work
    assert spy.calls == []


def test_remote_refusal_helper_covers_resend_directly():
    """`--resend` is already blocked by ask's own rules before the remote branch."""
    ask = _load_ask()
    refusal = ask._remote_peer_refusal(
        provider="codex",
        reply_to="",
        wait_explicit=False,
        foreground_explicit=False,
        resend_seen=True,
        to_role="",
    )
    assert "--resend is not available" in refusal
    assert (
        ask._remote_peer_refusal(
            provider="codex",
            reply_to="",
            wait_explicit=False,
            foreground_explicit=False,
            resend_seen=False,
            to_role="",
        )
        == ""
    )


def test_remote_refusal_helper_covers_the_provider_rule_directly():
    ask = _load_ask()
    refusal = ask._remote_peer_refusal(
        provider="gemini",
        reply_to="",
        wait_explicit=False,
        foreground_explicit=False,
        resend_seen=False,
        to_role="",
    )
    assert "claude and codex" in refusal
    # Positive control: a supported provider with no reply expectations is allowed.
    for provider in ("claude", "codex"):
        assert (
            ask._remote_peer_refusal(
                provider=provider,
                reply_to="",
                wait_explicit=False,
                foreground_explicit=False,
                resend_seen=False,
                to_role="lead",
            )
            == ""
        )


def test_unknown_host_is_refused_and_never_falls_back_locally(monkeypatch, capsys, tmp_path):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "codex")
    import remote_client

    hosts = tmp_path / "hosts.json"
    hosts.write_text(json.dumps({"hosts": {}}), encoding="utf-8")
    monkeypatch.setattr(remote_client, "hosts_config_path", lambda: hosts)
    monkeypatch.setattr(
        ask, "_run_peer_bridge_background", lambda *a, **k: pytest.fail("fell back to local lookup")
    )
    monkeypatch.setattr(
        ask, "_run_peer_bridge_foreground", lambda *a, **k: pytest.fail("fell back to local lookup")
    )

    rc = ask.main(["ask", "codex", "--peer", "ghost::example-project", "hi"])

    assert rc == 1
    assert "not configured" in capsys.readouterr().err


def test_malformed_selector_is_refused(monkeypatch, capsys):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "codex")
    spy = _patch_submit(ask, monkeypatch)

    rc = ask.main(["ask", "codex", "--peer", "testhost::", "hi"])

    assert rc == 1
    assert "invalid destination project alias" in capsys.readouterr().err
    assert spy.calls == []


def test_config_error_is_reported_without_a_traceback(monkeypatch, capsys):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "codex")
    spy = _patch_submit(ask, monkeypatch)
    import remote_client

    spy.error = remote_client.RemoteConfigError("no remote hosts configured")

    rc = ask.main(["ask", "codex", "--peer", "testhost::example-project", "--notify", "hi"])

    assert rc == 1
    assert "no remote hosts configured" in capsys.readouterr().err


def test_delivery_failure_exit_code_is_propagated(monkeypatch, capsys):
    ask = _load_ask()
    monkeypatch.setenv("CCB_CALLER", "codex")
    spy = _patch_submit(ask, monkeypatch)
    import remote_client

    spy.result = remote_client.RemoteResult(
        request_id="20261109-182536-922-2626995-habc1",
        host="testhost",
        state=remote_client.STATE_ACCEPTANCE_UNKNOWN,
        exit_code=remote_client.EXIT_ACCEPTANCE_UNKNOWN,
    )

    rc = ask.main(["ask", "codex", "--peer", "testhost::example-project", "--notify", "hi"])

    assert rc == remote_client.EXIT_ACCEPTANCE_UNKNOWN
