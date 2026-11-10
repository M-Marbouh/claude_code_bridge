"""M2 source side: reply-expected submit, fetch, receipts, notification, pend.

Fixtures use generic names only; the request id and caller-claims shape follow
the 2026-11-10 hardware canary with neutral paths.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import remote_client
import remote_protocol as proto
import terminal
from askd.daemon import UnifiedAskDaemon
from test_remote_client import (  # noqa: F401 - fixture re-export
    HOST_ALIAS,
    _argv_log,
    _receipt,
    _write_hosts,
    fake_ssh,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PEND = REPO_ROOT / "bin" / "pend"
LOCAL_ID = "20261110-113136-582-89163-1"
REQUEST_ID = f"{LOCAL_ID}-hmain1"
CALLER_IDENTITY = {
    "caller": "claude",
    "pane_id": "%0",
    "terminal": "tmux",
    "session_id": "ai-main1-0001",
    "work_dir": "/home/user/dev/project",
    "role_assertion": "",
    "launch_id": "ai-main1-0001",
    "member_id": "member-claude",
    "live_id": "live-claude",
    "pane_title_marker": "CCB-Claude-1",
    "ccb_project_id": "proj-main1",
    "pane_pid": "4242",
}


def _m2_hello(op_id: str = "hello-1", provider: str = "claude") -> dict:
    return {
        "ok": True,
        "op": "hello",
        "op_id": op_id,
        "capabilities": {
            "verbs": ["hello", "list", "submit", "status", "fetch"],
            "reply_modes": {provider: ["delivery-only", "explicit-reply"]},
            "max_frame": 262144,
            "proto": {"major": 1, "minor": 1},
        },
    }


class _ScriptedSession:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.writes: list[dict] = []
        self.closed = False

    def write_all(self, payload: bytes, timeout_s=None) -> int:
        self.writes.append(proto.read_frame(io.BytesIO(payload)))
        return len(payload)

    def read_frame(self, timeout_s=None) -> dict:
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def _submit(tmp_path: Path, session=None, **kwargs):
    return remote_client.submit(
        HOST_ALIAS,
        "example-project",
        "claude",
        "hello from the source",
        session_factory=(lambda _argv: session) if session is not None else None,
        out=io.StringIO(),
        err=io.StringIO(),
        **kwargs,
    )


def _accepted_reply_receipt(
    monkeypatch, *, poll_owner: str = "", identity=None, next_mode: str = ""
) -> str:
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "ack_ok")
    result = _submit(
        Path("."),
        reply_expected=True,
        intent="wait" if not poll_owner else "background",
        caller_identity=identity if identity is not None else dict(CALLER_IDENTITY),
        poll_owner=poll_owner,
    )
    assert result.state == remote_client.STATE_ACCEPTED, result.reason
    # The real destination echoes the request digest; the fake takes it from
    # the receipt so the fetch path verifies the same binding.
    monkeypatch.setenv(
        "CCB_FAKE_SSH_REQUEST_DIGEST", remote_client.read_receipt(result.request_id)["digest"]
    )
    if next_mode:
        monkeypatch.setenv("CCB_FAKE_SSH_MODE", next_mode)
    return result.request_id


def _set_receipt(request_id: str, **fields) -> dict:
    def mutate(latest: dict) -> None:
        latest.update(fields)

    return remote_client.update_receipt(request_id, mutate)


def _answered(text: str) -> remote_client.FetchResult:
    return remote_client.FetchResult(
        request_id=REQUEST_ID,
        host=HOST_ALIAS,
        exit_code=remote_client.EXIT_ACCEPTED,
        state="answered",
        reply_text=text,
        reply_digest=proto.compute_reply_digest(text),
    )


def _fetch_error(reason: str = "ssh timed out") -> remote_client.FetchResult:
    return remote_client.FetchResult(
        request_id=REQUEST_ID,
        host=HOST_ALIAS,
        exit_code=remote_client.EXIT_ACCEPTANCE_UNKNOWN,
        reason=reason,
        error_code="timeout",
    )


class _PaneBackend:
    def __init__(self, *, marker_pane: str = "%0", pid: str = "4242", alive: bool = True):
        self.marker_pane = marker_pane
        self.pid = pid
        self.alive = alive
        self.sent: list[tuple[str, str]] = []

    def is_alive(self, pane_id: str) -> bool:
        return self.alive

    def pane_matches_cwd_strict(self, pane_id: str, work_dir: str) -> bool:
        return True

    def find_pane_by_title_marker(self, marker: str, cwd_hint: str = ""):
        return self.marker_pane

    def pane_pid(self, pane_id: str) -> str:
        return self.pid

    def send_text(self, pane_id: str, text: str) -> None:
        self.sent.append((pane_id, text))


@pytest.fixture
def pane_backend(monkeypatch):
    backend = _PaneBackend()
    monkeypatch.setattr(terminal, "get_backend_for_session", lambda _rec: backend)
    return backend


# --------------------------------------------------------------------------- #
# Capability gating (A5)
# --------------------------------------------------------------------------- #


def test_reply_expected_submit_to_a_delivery_only_destination_is_not_delivered(
    tmp_path, fake_ssh, monkeypatch
):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "no_explicit_reply")
    result = _submit(
        tmp_path, reply_expected=True, intent="wait", caller_identity=dict(CALLER_IDENTITY)
    )

    assert result.state == remote_client.STATE_NOT_DELIVERED
    assert "does not advertise explicit replies" in result.reason
    receipt = _receipt(result.request_id)
    assert receipt["state"] == remote_client.STATE_NOT_DELIVERED
    assert "accepted" not in [entry["state"] for entry in receipt["history"]]
    # Nothing but the handshake reached the destination.
    assert len(_argv_log(fake_ssh)) == 1

    # Positive control: the same M1/delivery-only capability table still
    # accepts a plain delivery-only submit.
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "ack_ok_delivery_only")
    plain = _submit(tmp_path)
    assert plain.state == remote_client.STATE_ACCEPTED


def test_a_hello_without_fetch_is_refused_before_submit(tmp_path):
    _write_hosts(tmp_path)
    capabilities = _m2_hello()
    capabilities["capabilities"]["verbs"] = ["hello", "list", "submit", "status"]
    session = _ScriptedSession([capabilities])

    result = _submit(
        tmp_path, session, reply_expected=True, intent="wait",
        caller_identity=dict(CALLER_IDENTITY),
    )

    assert result.state == remote_client.STATE_NOT_DELIVERED
    assert "fetch" in result.reason
    # Nothing but the handshake was written.
    assert [frame["op"] for frame in session.writes] == ["hello"]

    # Positive control: the same wrong-op hello is refused only because of the
    # capability table; with fetch advertised the source does submit.
    proper = _ScriptedSession([_m2_hello(), {"ok": False, "op": "submit", "op_id": "x"}])
    _submit(
        tmp_path, proper, reply_expected=True, intent="wait",
        caller_identity=dict(CALLER_IDENTITY),
    )
    assert [frame["op"] for frame in proper.writes] == ["hello", "submit"]


def test_m2_destination_accepts_and_the_record_is_schema_v2(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "ack_ok")
    result = _submit(
        tmp_path, reply_expected=True, intent="wait", caller_identity=dict(CALLER_IDENTITY)
    )

    assert result.state == remote_client.STATE_ACCEPTED
    receipt = _receipt(result.request_id)
    assert receipt["schema_version"] == proto.SCHEMA_VERSION_REPLY
    assert receipt["reply_expected"] is True
    assert receipt["intent"] == "wait"
    assert receipt["caller_identity"]["pane_id"] == "%0"
    assert receipt["caller_identity"]["pane_pid"] == "4242"
    assert receipt["caller_identity"]["ccb_project_id"] == "proj-main1"
    # The caller identity copy never carries a credential token.
    assert "token" not in json.dumps(receipt["caller_identity"]).lower()
    assert "poll_owner" not in receipt


def test_delivery_only_receipts_keep_their_m1_shape(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "ack_ok")
    result = _submit(tmp_path)

    receipt = _receipt(result.request_id)
    assert receipt["schema_version"] == 1
    for key in ("reply_expected", "caller_identity", "poll_owner", "reply_text"):
        assert key not in receipt


# --------------------------------------------------------------------------- #
# Background ownership (A3)
# --------------------------------------------------------------------------- #


def test_background_without_a_polling_owner_is_refused_before_submit(
    tmp_path, fake_ssh, monkeypatch
):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "ack_ok")
    result = _submit(
        tmp_path,
        reply_expected=True,
        intent="background",
        caller_identity=dict(CALLER_IDENTITY),
        poll_owner="",
    )

    assert result.state == remote_client.STATE_NOT_DELIVERED
    assert "background replies need CCB running here; use --wait" in result.reason
    # No SSH process was spawned at all.
    assert _argv_log(fake_ssh) == []
    receipt = _receipt(result.request_id)
    assert receipt["state"] == remote_client.STATE_NOT_DELIVERED
    assert "poll_owner" not in receipt


def test_background_receipt_records_its_poll_owner(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "ack_ok")
    owner = "/home/user/.cache/ccb/projects/0123456789abcdef/askd.json"
    result = _submit(
        tmp_path,
        reply_expected=True,
        intent="background",
        caller_identity=dict(CALLER_IDENTITY),
        poll_owner=owner,
    )

    assert result.state == remote_client.STATE_ACCEPTED
    assert _receipt(result.request_id)["poll_owner"] == owner


def test_watch_registration_is_scoped_to_the_registered_owner(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "ack_ok")
    daemon = UnifiedAskDaemon(state_file=tmp_path / "askd.json")
    request_id = _accepted_reply_receipt(monkeypatch, poll_owner=str(daemon.state_file))

    assert remote_client.watch_target_ok(request_id, str(daemon.state_file)) == (True, "")
    ok, reason = remote_client.watch_target_ok(request_id, "/home/user/other/askd.json")
    assert ok is False and "another polling daemon" in reason

    watched: list[str] = []
    monkeypatch.setattr(
        remote_client,
        "watch_loop",
        lambda stop, wake, *, owner, log=None: watched.append(owner),
    )
    response = daemon._handle_request(
        {"operation": "remote_watch", "id": "op-watch", "request_id": request_id}
    )
    assert response["ok"] is True
    assert response["remote"]["request_id"] == request_id
    assert watched == [str(daemon.state_file)]


def test_watch_registration_refuses_an_unknown_or_foreign_receipt(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "ack_ok")
    daemon = UnifiedAskDaemon(state_file=tmp_path / "askd.json")

    assert remote_client.watch_target_ok(REQUEST_ID, str(daemon.state_file))[0] is False

    request_id = _accepted_reply_receipt(monkeypatch)  # no poll_owner at all
    ok, reason = remote_client.watch_target_ok(request_id, str(daemon.state_file))
    assert ok is False and "another polling daemon" in reason


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #


def test_fetch_returns_the_exact_answer(tmp_path, fake_ssh, monkeypatch):
    reply = "multi\n\nline — with an em-dash and 🙂 emoji\n"
    monkeypatch.setenv("CCB_FAKE_SSH_REPLY", reply)
    request_id = _accepted_reply_receipt(monkeypatch, next_mode="fetch_answered")

    result = remote_client.fetch(request_id)

    assert result.exit_code == remote_client.EXIT_ACCEPTED
    assert result.state == "answered"
    assert result.reply_text == reply
    assert result.reply_digest == proto.compute_reply_digest(reply)


def test_fetch_refuses_an_answer_whose_digest_does_not_match(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_REPLY", "forged answer")
    request_id = _accepted_reply_receipt(monkeypatch, next_mode="fetch_bad_digest")

    result = remote_client.fetch(request_id)

    assert result.answered is False
    assert result.error_code == "untrusted_reply"
    assert result.reply_text == ""


def test_fetch_refusal_reads_unknown_or_not_authorised(tmp_path, fake_ssh, monkeypatch):
    request_id = _accepted_reply_receipt(monkeypatch, next_mode="fetch_not_found")

    result = remote_client.fetch(request_id)

    assert result.error_code == proto.ERR_NOT_FOUND
    assert result.reason == "unknown or not authorised"
    assert result.answered is False


# --------------------------------------------------------------------------- #
# Durable answer, one notification, receipts
# --------------------------------------------------------------------------- #


def test_poll_persists_the_answer_before_notifying(tmp_path, fake_ssh, monkeypatch):
    request_id = _accepted_reply_receipt(monkeypatch)
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _answered("the answer\n"))
    seen: list[dict] = []

    def deliver(identity, text):
        seen.append(remote_client.read_receipt(request_id))
        return True, ""

    monkeypatch.setattr(remote_client, "deliver_to_caller", deliver)

    receipt = remote_client.poll_receipt(request_id, notify=True)

    assert seen, "notification must run"
    assert seen[0]["state"] == remote_client.STATE_FETCHED
    assert seen[0]["reply_text"] == "the answer\n"
    assert seen[0]["reply_digest"] == proto.compute_reply_digest("the answer\n")
    assert receipt["state"] == remote_client.STATE_NOTIFIED
    assert receipt["notify_attempted_at"]
    assert receipt["notified_at"]


def test_a_second_different_answer_never_replaces_the_first(tmp_path, fake_ssh, monkeypatch):
    request_id = _accepted_reply_receipt(monkeypatch)
    answers = [_answered("first answer"), _answered("second answer")]
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: answers.pop(0))

    remote_client.poll_receipt(request_id, notify=False)
    receipt = remote_client.poll_receipt(request_id, notify=False)

    assert receipt["reply_text"] == "first answer"
    assert receipt["reply_conflict"] is True
    assert receipt["state"] == remote_client.STATE_FETCHED


def test_fetch_failure_keeps_the_receipt_and_recovers(tmp_path, fake_ssh, monkeypatch):
    request_id = _accepted_reply_receipt(monkeypatch)
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _fetch_error("ssh exited 255"))

    receipt = remote_client.poll_receipt(request_id, notify=False)

    assert receipt["state"] == remote_client.STATE_FETCH_UNAVAILABLE
    assert "ssh exited 255" in receipt["last_fetch_error"]
    assert "reply_text" not in receipt

    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _answered("late answer"))
    receipt = remote_client.poll_receipt(request_id, notify=False)
    assert receipt["state"] == remote_client.STATE_FETCHED
    assert receipt["reply_text"] == "late answer"


def test_status_refresh_never_drops_a_stored_answer(tmp_path, fake_ssh, monkeypatch):
    request_id = _accepted_reply_receipt(monkeypatch)
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _answered("stored answer"))
    remote_client.poll_receipt(request_id, notify=False)

    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "status_delivered")
    monkeypatch.setenv("CCB_FAKE_SSH_STATE", "answered")
    status = remote_client.status(request_id, out=io.StringIO(), err=io.StringIO())

    receipt = _receipt(request_id)
    assert status.state == "answered"
    assert receipt["state"] == remote_client.STATE_FETCHED
    assert receipt["reply_text"] == "stored answer"
    assert receipt["reply_digest"] == proto.compute_reply_digest("stored answer")
    assert receipt["remote_state"] == "answered"


def test_status_marks_answer_available_when_the_text_is_not_stored_yet(
    tmp_path, fake_ssh, monkeypatch
):
    request_id = _accepted_reply_receipt(monkeypatch)

    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "status_delivered")
    monkeypatch.setenv("CCB_FAKE_SSH_STATE", "answered")
    out = io.StringIO()
    remote_client.status(request_id, out=out, err=io.StringIO())

    receipt = _receipt(request_id)
    assert receipt["state"] == remote_client.STATE_ANSWER_AVAILABLE
    assert "reply_text" not in receipt
    assert f"state={remote_client.STATE_ANSWER_AVAILABLE}" in out.getvalue()


def test_status_of_a_delivery_only_receipt_is_unchanged(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "ack_ok")
    result = _submit(tmp_path)
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "status_delivered")
    monkeypatch.setenv("CCB_FAKE_SSH_STATE", "delivered")

    remote_client.status(result.request_id, out=io.StringIO(), err=io.StringIO())

    receipt = _receipt(result.request_id)
    assert receipt["state"] == "delivered"
    assert receipt["remote_state"] == "delivered"


def test_ccb_remote_status_shows_the_source_state(tmp_path, fake_ssh, monkeypatch):
    request_id = _accepted_reply_receipt(monkeypatch)
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _answered("stored answer"))
    remote_client.poll_receipt(request_id, notify=False)
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "status_delivered")
    monkeypatch.setenv("CCB_FAKE_SSH_STATE", "answered")

    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "ccb-remote"), "status", request_id],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert f"state={remote_client.STATE_FETCHED} destination=answered" in completed.stdout
    assert _receipt(request_id)["reply_text"] == "stored answer"


# --------------------------------------------------------------------------- #
# --wait
# --------------------------------------------------------------------------- #


def test_wait_prints_the_exact_reply(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setattr(remote_client, "REPLY_BACKOFF_START_S", 0.01)
    monkeypatch.setattr(remote_client, "REPLY_BACKOFF_CAP_S", 0.02)
    reply = "first line\n\nsecond — line 🙂\n"
    monkeypatch.setenv("CCB_FAKE_SSH_ANSWER_AFTER", "2")
    monkeypatch.setenv("CCB_FAKE_SSH_REPLY", reply)
    monkeypatch.setenv("CCB_FAKE_SSH_COUNTER", str(tmp_path / "fetch-count"))
    request_id = _accepted_reply_receipt(monkeypatch, next_mode="fetch_sequence")

    out, err = io.StringIO(), io.StringIO()
    rc = remote_client.wait_for_answer(request_id, timeout_s=10, out=out, err=err)

    assert rc == remote_client.EXIT_ACCEPTED
    assert out.getvalue() == reply
    assert f"id={request_id} state={remote_client.STATE_FETCHED}" in err.getvalue()
    assert _receipt(request_id)["reply_text"] == reply


def test_wait_deadline_leaves_the_answer_for_pend(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setattr(remote_client, "REPLY_BACKOFF_START_S", 0.01)
    monkeypatch.setattr(remote_client, "REPLY_BACKOFF_CAP_S", 0.02)
    request_id = _accepted_reply_receipt(monkeypatch, next_mode="fetch_delivered")

    out, err = io.StringIO(), io.StringIO()
    rc = remote_client.wait_for_answer(request_id, timeout_s=0.05, out=out, err=err)

    assert rc == remote_client.EXIT_WAITING
    assert out.getvalue() == ""
    assert f"still waiting; use pend {request_id}" in err.getvalue()

    # Later, the answer is available and pend collects it.
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "fetch_answered")
    monkeypatch.setenv("CCB_FAKE_SSH_REPLY", "the late answer\n")
    pend_out, pend_err = io.StringIO(), io.StringIO()
    assert remote_client.remote_pend(request_id, out=pend_out, err=pend_err) == remote_client.EXIT_ACCEPTED
    assert "the late answer" in pend_out.getvalue()
    assert _receipt(request_id)["state"] == remote_client.STATE_FETCHED


def test_pend_reports_an_unfetched_answer_without_inventing_one(tmp_path, fake_ssh, monkeypatch):
    request_id = _accepted_reply_receipt(monkeypatch, next_mode="fetch_not_found")

    out, err = io.StringIO(), io.StringIO()
    rc = remote_client.remote_pend(request_id, out=out, err=err)

    assert rc == remote_client.EXIT_WAITING
    assert remote_client.STATE_FETCH_UNAVAILABLE in out.getvalue()
    assert "unknown or not authorised" in err.getvalue()


def test_pend_refuses_a_delivery_only_receipt(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "ack_ok")
    result = _submit(tmp_path)

    err = io.StringIO()
    rc = remote_client.remote_pend(result.request_id, out=io.StringIO(), err=err)

    assert rc == remote_client.EXIT_NOT_DELIVERED
    assert "not a reply-expected remote request" in err.getvalue()


def test_pend_cli_shows_a_remote_id_and_keeps_local_ids(
    tmp_path, fake_ssh, monkeypatch, capsys
):
    monkeypatch.setenv("CCB_FAKE_SSH_REPLY", "from the other machine\n")
    request_id = _accepted_reply_receipt(monkeypatch, next_mode="fetch_answered")

    env = dict(__import__("os").environ)
    completed = subprocess.run(
        [sys.executable, str(PEND), request_id],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert "from the other machine" in completed.stdout
    assert _receipt(request_id)["state"] == remote_client.STATE_FETCHED


# --------------------------------------------------------------------------- #
# Notification (fail closed, at most once)
# --------------------------------------------------------------------------- #


def test_notification_injects_the_labelled_block_into_the_captured_pane(
    tmp_path, fake_ssh, monkeypatch, pane_backend
):
    request_id = _accepted_reply_receipt(monkeypatch)
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _answered("the answer"))
    remote_client.poll_receipt(request_id, notify=False)

    assert remote_client._notify_caller(request_id, HOST_ALIAS) is True

    assert len(pane_backend.sent) == 1
    pane, text = pane_backend.sent[0]
    assert pane == "%0"
    assert f"CCB_REMOTE_REQUEST_ID: {request_id}" in text
    assert f"CCB_REMOTE_SOURCE_HOST: {HOST_ALIAS}" in text
    assert text.endswith("the answer")
    receipt = _receipt(request_id)
    assert receipt["state"] == remote_client.STATE_NOTIFIED
    assert receipt["notify_attempted_at"]


def test_a_replaced_caller_pane_is_never_notified(tmp_path, fake_ssh, monkeypatch):
    request_id = _accepted_reply_receipt(monkeypatch)
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _answered("private answer"))
    remote_client.poll_receipt(request_id, notify=False)

    replaced = _PaneBackend(marker_pane="%9")
    monkeypatch.setattr(terminal, "get_backend_for_session", lambda _rec: replaced)

    assert remote_client._notify_caller(request_id, HOST_ALIAS) is False
    assert replaced.sent == []
    receipt = _receipt(request_id)
    assert receipt["state"] == remote_client.STATE_FETCHED
    assert receipt["notify_refused"]
    assert receipt["notify_attempted_at"]

    # The answer is still available to pend, and never injected anywhere.
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _answered("private answer"))
    out = io.StringIO()
    assert remote_client.remote_pend(request_id, out=out, err=io.StringIO()) == remote_client.EXIT_ACCEPTED
    assert "private answer" in out.getvalue()
    assert replaced.sent == []


def test_same_pane_respawned_is_not_notified(tmp_path, fake_ssh, monkeypatch):
    request_id = _accepted_reply_receipt(monkeypatch)
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _answered("the answer"))
    remote_client.poll_receipt(request_id, notify=False)
    backend = _PaneBackend(pid="9999")
    monkeypatch.setattr(terminal, "get_backend_for_session", lambda _rec: backend)

    assert remote_client._notify_caller(request_id, HOST_ALIAS) is False
    assert backend.sent == []
    assert "different process" in _receipt(request_id)["notify_refused"]


def test_missing_or_unidentified_caller_identity_fails_closed(
    tmp_path, fake_ssh, monkeypatch
):
    request_id = _accepted_reply_receipt(monkeypatch, identity={})
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _answered("the answer"))
    remote_client.poll_receipt(request_id, notify=False)

    assert remote_client._notify_caller(request_id, HOST_ALIAS) is False
    receipt = _receipt(request_id)
    assert receipt["state"] == remote_client.STATE_FETCHED
    assert "no caller pane" in receipt["notify_refused"]


def test_crash_after_notify_attempted_is_never_resent(
    tmp_path, fake_ssh, monkeypatch, pane_backend
):
    request_id = _accepted_reply_receipt(monkeypatch)
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _answered("the answer"))
    remote_client.poll_receipt(request_id, notify=False)
    # Simulate a crash AFTER the durable attempt marker but before/after a send
    # whose outcome was never recorded.
    _set_receipt(
        request_id,
        notify_attempted_at="2026-11-10T11:41:00+00:00",
    )

    assert remote_client._notify_caller(request_id, HOST_ALIAS) is False
    assert pane_backend.sent == []
    receipt = _receipt(request_id)
    assert receipt["state"] == remote_client.STATE_FETCHED
    assert remote_client.watchable(receipt) is False


def test_concurrent_poller_and_pend_notify_exactly_once(
    tmp_path, fake_ssh, monkeypatch, pane_backend
):
    request_id = _accepted_reply_receipt(monkeypatch)
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _answered("the answer"))
    barrier = threading.Barrier(2)
    results: list[bool] = []
    errors: list[BaseException] = []

    def run():
        try:
            barrier.wait(timeout=5)
            remote_client.poll_receipt(request_id, notify=True)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors
    assert len(pane_backend.sent) == 1
    receipt = _receipt(request_id)
    assert receipt["state"] == remote_client.STATE_NOTIFIED
    answers = [
        entry
        for entry in receipt["history"]
        if entry.get("state") == remote_client.STATE_FETCHED
    ]
    assert len(answers) == 1


# --------------------------------------------------------------------------- #
# A late submit ack never undoes an answer (state precedence)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "answer_before_ack", [False, True], ids=["ack-first-control", "answer-before-ack"]
)
def test_submit_ack_never_downgrades_an_answer_already_stored(
    tmp_path, fake_ssh, monkeypatch, answer_before_ack
):
    """The watcher may fetch+notify while `submit()` is still waiting for its ack."""
    sends: list[str] = []
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _answered("fast answer"))
    monkeypatch.setattr(
        remote_client, "deliver_to_caller", lambda identity, text: (sends.append(text) is None, "")
    )
    owner = str(tmp_path / "askd.json")

    class Session:
        def write_all(self, payload, timeout_s=None) -> int:
            self.last = proto.read_frame(io.BytesIO(payload))
            return len(payload)

        def read_frame(self, timeout_s=None) -> dict:
            if self.last["op"] == "hello":
                return _m2_hello()
            body = self.last["body"]
            if answer_before_ack:
                # The answer is stored (and notified) BEFORE the ack is read.
                assert body["request_id"] in remote_client.watched_receipts(owner)
                remote_client.poll_receipt(body["request_id"], notify=True)
            return {
                "ok": True, "op": "submit", "op_id": self.last["op_id"],
                "request_id": body["request_id"], "digest": body["digest"],
                "state": "delivered",
            }

        def close(self) -> None:
            pass

    result = _submit(
        tmp_path, Session(), reply_expected=True, intent="background",
        poll_owner=owner, caller_identity=dict(CALLER_IDENTITY),
    )
    assert result.ok
    if not answer_before_ack:
        remote_client.poll_receipt(result.request_id, notify=True)

    receipt = remote_client.read_receipt(result.request_id)
    out = io.StringIO()
    code = remote_client.remote_pend(result.request_id, out=out, err=io.StringIO())
    print("submit race:", "answer_before_ack=", answer_before_ack,
          "state=", receipt["state"], "sends=", len(sends))
    assert receipt["state"] == remote_client.STATE_NOTIFIED
    assert code == 0 and "fast answer" in out.getvalue()


def test_a_late_failure_cannot_erase_a_notification_attempt(fake_ssh, monkeypatch):
    """Crash window: the attempt marker is durable before the outcome is recorded."""
    request_id = _accepted_reply_receipt(monkeypatch)
    _set_receipt(request_id, notify_attempted_at="2026-11-10T11:41:00+00:00")

    record = remote_client.read_receipt(request_id)
    remote_client._transition(record, remote_client.STATE_NOT_DELIVERED, "late ack")

    receipt = remote_client.read_receipt(request_id)
    print("attempt marker:", receipt.get("notify_attempted_at"), "state=", receipt["state"])
    assert receipt["state"] == remote_client.STATE_ACCEPTED
    assert receipt.get("notify_attempted_at")


def test_a_late_status_mirror_cannot_downgrade_a_fetched_answer(tmp_path, fake_ssh, monkeypatch):
    request_id = _accepted_reply_receipt(monkeypatch)
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _answered("stored answer"))
    remote_client.poll_receipt(request_id, notify=False)
    before = _receipt(request_id)
    assert before["state"] == remote_client.STATE_FETCHED

    monkeypatch.setenv("CCB_FAKE_SSH_MODE", "status_delivered")
    monkeypatch.setenv("CCB_FAKE_SSH_STATE", "accepted-pending")
    remote_client.status(request_id, out=io.StringIO(), err=io.StringIO())

    after = _receipt(request_id)
    assert after["state"] == remote_client.STATE_FETCHED
    assert after["reply_text"] == "stored answer"


# --------------------------------------------------------------------------- #
# Caller pane process witness (notification)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "captured_pid", ["4242", ""], ids=["known-process-control", "missing-process-witness"]
)
def test_tmux_notification_requires_the_captured_pane_process(
    pane_backend, monkeypatch, captured_pid
):
    import peer_routing
    from types import SimpleNamespace

    monkeypatch.setattr(pane_backend, "respawn_pane", lambda *a, **kw: True, raising=False)
    monkeypatch.setenv("CCB_CALLER_PANE_ID", "%0")
    monkeypatch.setenv("CCB_CALLER_TERMINAL", "tmux")
    monkeypatch.setattr(
        peer_routing,
        "identify_sender",
        lambda *a, **kw: SimpleNamespace(candidate=SimpleNamespace(**CALLER_IDENTITY)),
    )
    if captured_pid:
        identity = remote_client.capture_caller_identity(
            caller="claude", work_dir=CALLER_IDENTITY["work_dir"]
        )
    else:
        # Capture fails at submit time; the pane is later respawned in place.
        with monkeypatch.context() as context:
            context.setattr(
                pane_backend, "pane_pid",
                lambda pane: (_ for _ in ()).throw(OSError("pid unreadable at submit")),
            )
            identity = remote_client.capture_caller_identity(
                caller="claude", work_dir=CALLER_IDENTITY["work_dir"]
            )
        assert identity["pane_pid"] == "" and identity["pane_title_marker"]
        pane_backend.pid = "9999"  # same pane id, new process

    ok, reason = remote_client.deliver_to_caller(identity, "private answer")
    print("caller witness:", repr(captured_pid), "accepted=", ok, "sends=", len(pane_backend.sent))
    if captured_pid:
        assert ok and len(pane_backend.sent) == 1
    else:
        assert not ok
        assert "process was not captured" in reason
        assert pane_backend.sent == []


def test_a_backend_without_in_place_respawn_needs_no_process_witness(
    pane_backend, monkeypatch
):
    """The witness rule is specific to backends where the pane id survives a respawn."""
    import peer_routing
    from types import SimpleNamespace

    monkeypatch.setenv("CCB_CALLER_PANE_ID", "9")
    monkeypatch.setenv("CCB_CALLER_TERMINAL", "wezterm")
    monkeypatch.setattr(
        peer_routing,
        "identify_sender",
        lambda *a, **kw: SimpleNamespace(candidate=SimpleNamespace(**CALLER_IDENTITY)),
    )

    class _NoRespawn:
        def __init__(self):
            self.sent = []

        def is_alive(self, pane_id):
            return True

        def pane_matches_cwd_strict(self, pane_id, work_dir):
            return True

        def find_pane_by_title_marker(self, marker, cwd_hint=""):
            return "9"

        def send_text(self, pane_id, text):
            self.sent.append((pane_id, text))

    backend = _NoRespawn()
    import terminal

    monkeypatch.setattr(terminal, "get_backend_for_session", lambda _rec: backend)
    identity = {
        "pane_id": "9", "terminal": "wezterm",
        "work_dir": CALLER_IDENTITY["work_dir"],
        "pane_title_marker": CALLER_IDENTITY["pane_title_marker"],
        "ccb_project_id": CALLER_IDENTITY["ccb_project_id"],
        "pane_pid": "",
    }

    ok, reason = remote_client.deliver_to_caller(identity, "answer")

    assert ok, reason
    assert len(backend.sent) == 1


# --------------------------------------------------------------------------- #
# Legacy caller marker fallback (a launch with no live inventory)
# --------------------------------------------------------------------------- #

LEGACY_PANE_ID = "%0"
LEGACY_MARKER = "CCB-Claude-f8cc696c"
LEGACY_PROJECT_ID = "proj-legacy-f8cc696c"


class _NoPaneCreationBackend(_PaneBackend):
    """Records any attempt to create or respawn a pane (never expected)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pane_ops: list[str] = []

    def ensure_pane(self, *args, **kwargs):
        self.pane_ops.append("ensure_pane")

    def respawn_pane(self, *args, **kwargs):
        self.pane_ops.append("respawn_pane")

    def split_pane(self, *args, **kwargs):
        self.pane_ops.append("split_pane")


def _legacy_record(
    work_dir,
    *,
    provider: str = "claude",
    pane_id: str = LEGACY_PANE_ID,
    marker: str = LEGACY_MARKER,
    project_id: str = LEGACY_PROJECT_ID,
):
    """A registry record shaped like a real legacy (no-inventory) launch."""
    from ccb_runtime_status import RegistryProviderRecord

    return RegistryProviderRecord(
        project_id=project_id,
        work_dir=str(work_dir),
        provider=provider,
        provider_entry={"pane_id": pane_id, "pane_title_marker": marker, "terminal": None},
        registry_record={},
        updated_at=0,
        timestamp_stale=False,
    )


def _spy_registry(monkeypatch, records):
    """Replace the legacy registry reader and record every call's kwargs."""
    import ccb_runtime_status

    calls: list[dict] = []

    def fake_iter(**kwargs):
        calls.append(kwargs)
        return list(records)

    monkeypatch.setattr(ccb_runtime_status, "iter_registry_provider_records", fake_iter)
    return calls


def _sender_resolution(monkeypatch, *, candidate=None, error: str = "", detail: str = ""):
    import peer_routing
    from types import SimpleNamespace

    resolution = SimpleNamespace(candidate=candidate, error=error, detail=detail)
    monkeypatch.setattr(peer_routing, "identify_sender", lambda *a, **kw: resolution)
    return resolution


def _capture_tmux_caller(monkeypatch, work_dir, *, caller: str = "claude"):
    monkeypatch.setenv("CCB_CALLER_PANE_ID", LEGACY_PANE_ID)
    monkeypatch.setenv("CCB_CALLER_TERMINAL", "tmux")
    return remote_client.capture_caller_identity(caller=caller, work_dir=str(work_dir))


def test_legacy_registry_entry_supplies_the_callers_marker(tmp_path, pane_backend, monkeypatch):
    import peer_routing
    from project_id import compute_ccb_project_id

    work_dir = tmp_path / "project"
    work_dir.mkdir()
    calls = _spy_registry(
        monkeypatch,
        [
            _legacy_record(work_dir),
            _legacy_record(work_dir, provider="pi", pane_id="%9", marker="CCB-Pi-f8cc696c"),
            _legacy_record(work_dir, provider="codex", pane_id="%10", marker="CCB-Codex-f8cc696c"),
        ],
    )
    _sender_resolution(
        monkeypatch, error=peer_routing.NO_CANDIDATES, detail="legacy project without live inventory"
    )

    identity = _capture_tmux_caller(monkeypatch, work_dir)

    print("legacy capture:", identity["pane_id"], identity["pane_title_marker"], identity["ccb_project_id"])
    assert identity["pane_title_marker"] == LEGACY_MARKER
    assert identity["ccb_project_id"] == LEGACY_PROJECT_ID
    # The lookup is scoped to this project and keeps the reader's default of
    # excluding stale records.
    assert calls == [{"project_id": compute_ccb_project_id(work_dir)}]


def test_legacy_lookup_never_asks_for_stale_records(tmp_path, pane_backend, monkeypatch):
    import peer_routing

    work_dir = tmp_path / "project"
    work_dir.mkdir()
    calls = _spy_registry(monkeypatch, [_legacy_record(work_dir)])
    _sender_resolution(monkeypatch, error=peer_routing.NO_CANDIDATES)

    identity = _capture_tmux_caller(monkeypatch, work_dir)

    assert identity["pane_title_marker"] == LEGACY_MARKER
    assert [call.get("include_stale") for call in calls] == [None]


def test_legacy_lookup_without_an_entry_for_the_caller_pane_stays_empty(
    tmp_path, pane_backend, monkeypatch
):
    import peer_routing

    work_dir = tmp_path / "project"
    work_dir.mkdir()
    _spy_registry(monkeypatch, [_legacy_record(work_dir, pane_id="%9")])
    _sender_resolution(monkeypatch, error=peer_routing.NO_CANDIDATES)

    identity = _capture_tmux_caller(monkeypatch, work_dir)

    assert identity["pane_title_marker"] == ""
    assert identity["ccb_project_id"] == ""


@pytest.mark.parametrize(
    "records_kwargs",
    [
        pytest.param([{"pane_id": "%9"}], id="wrong-pane"),
        pytest.param([{"provider": "codex"}], id="wrong-provider"),
    ],
)
def test_legacy_lookup_requires_an_exact_pane_and_provider_match(
    tmp_path, pane_backend, monkeypatch, records_kwargs
):
    import peer_routing

    work_dir = tmp_path / "project"
    work_dir.mkdir()
    _spy_registry(monkeypatch, [_legacy_record(work_dir, **records_kwargs[0])])
    _sender_resolution(monkeypatch, error=peer_routing.NO_CANDIDATES)

    identity = _capture_tmux_caller(monkeypatch, work_dir)

    assert identity["pane_title_marker"] == ""
    assert identity["ccb_project_id"] == ""


def test_legacy_lookup_ignores_a_record_from_another_work_dir(
    tmp_path, pane_backend, monkeypatch
):
    import peer_routing

    work_dir = tmp_path / "project"
    work_dir.mkdir()
    other = tmp_path / "other-project"
    other.mkdir()
    _spy_registry(monkeypatch, [_legacy_record(other)])
    _sender_resolution(monkeypatch, error=peer_routing.NO_CANDIDATES)

    identity = _capture_tmux_caller(monkeypatch, work_dir)

    assert identity["pane_title_marker"] == ""
    assert identity["ccb_project_id"] == ""


def test_legacy_lookup_with_two_matching_entries_stays_empty(
    tmp_path, pane_backend, monkeypatch
):
    import peer_routing

    work_dir = tmp_path / "project"
    work_dir.mkdir()
    _spy_registry(
        monkeypatch,
        [_legacy_record(work_dir), _legacy_record(work_dir, project_id="proj-other")],
    )
    _sender_resolution(monkeypatch, error=peer_routing.NO_CANDIDATES)

    identity = _capture_tmux_caller(monkeypatch, work_dir)

    assert identity["pane_title_marker"] == ""
    assert identity["ccb_project_id"] == ""


@pytest.mark.parametrize(
    "error",
    ["ambiguous", "invalid_inventory", "unavailable", "launcher_dead", "session_inactive"],
)
def test_only_no_candidates_may_fall_through_to_the_legacy_registry(
    tmp_path, pane_backend, monkeypatch, error
):
    """Every other outcome means the inventory WAS consulted and refused."""
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    calls = _spy_registry(monkeypatch, [_legacy_record(work_dir)])
    _sender_resolution(monkeypatch, error=error, detail="inventory said no")

    identity = _capture_tmux_caller(monkeypatch, work_dir)

    print("inventory error:", error, "registry_calls=", len(calls))
    assert identity["pane_title_marker"] == ""
    assert identity["ccb_project_id"] == ""
    assert calls == []


def test_a_live_candidate_wins_over_a_conflicting_legacy_entry(
    tmp_path, pane_backend, monkeypatch
):
    from types import SimpleNamespace

    work_dir = tmp_path / "project"
    work_dir.mkdir()
    calls = _spy_registry(monkeypatch, [_legacy_record(work_dir, marker="CCB-Claude-legacy")])
    _sender_resolution(
        monkeypatch,
        candidate=SimpleNamespace(
            pane_title_marker="CCB-Claude-live",
            ccb_project_id="proj-live",
            live_id="live-1",
            launch_id="launch-1",
            work_dir=str(work_dir),
        ),
    )

    identity = _capture_tmux_caller(monkeypatch, work_dir)

    assert identity["pane_title_marker"] == "CCB-Claude-live"
    assert identity["ccb_project_id"] == "proj-live"
    assert calls == []


def test_a_tmux_caller_without_a_marker_gets_an_explicit_refusal(
    tmp_path, monkeypatch
):
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    backend = _NoPaneCreationBackend()
    monkeypatch.setattr(terminal, "get_backend_for_session", lambda _rec: backend)
    identity = {
        "caller": "claude",
        "pane_id": LEGACY_PANE_ID,
        "terminal": "tmux",
        "work_dir": str(work_dir),
        "pane_title_marker": "",
        "ccb_project_id": "",
        "pane_pid": "4242",
    }

    ok, reason = remote_client.deliver_to_caller(identity, "private answer")

    print("marker refusal:", ok, reason, "sends=", len(backend.sent))
    assert not ok
    assert "destination has no saved pane marker" in reason
    assert backend.sent == []
    assert backend.pane_ops == []


def test_a_legacy_captured_caller_receives_the_answer(tmp_path, monkeypatch):
    import peer_routing

    work_dir = tmp_path / "project"
    work_dir.mkdir()
    backend = _NoPaneCreationBackend()
    monkeypatch.setattr(terminal, "get_backend_for_session", lambda _rec: backend)
    _spy_registry(monkeypatch, [_legacy_record(work_dir)])
    _sender_resolution(
        monkeypatch, error=peer_routing.NO_CANDIDATES, detail="legacy project without live inventory"
    )

    identity = _capture_tmux_caller(monkeypatch, work_dir)
    assert identity["pane_title_marker"] == LEGACY_MARKER
    assert identity["ccb_project_id"] == LEGACY_PROJECT_ID

    ok, reason = remote_client.deliver_to_caller(identity, "the answer")

    print("legacy notify:", ok, reason, "sends=", len(backend.sent))
    assert ok, reason
    assert backend.sent == [(LEGACY_PANE_ID, "the answer")]
    assert backend.pane_ops == []


# --------------------------------------------------------------------------- #
# fetch must be bound to this exact request
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "missing_field", ["", "digest", "request_id"],
    ids=["matching-identity-control", "missing-request-digest", "missing-request-id"],
)
def test_fetch_requires_the_request_identity_and_digest(fake_ssh, monkeypatch, missing_field):
    request_id = _accepted_reply_receipt(monkeypatch)
    receipt = _receipt(request_id)
    answer = "answer with no request binding"
    frame = {
        "ok": True, "op": "fetch", "op_id": f"fetch-{request_id}",
        "request_id": request_id, "state": "answered", "reply_text": answer,
        "reply_digest": proto.compute_reply_digest(answer), "digest": receipt["digest"],
    }
    if missing_field:
        del frame[missing_field]
    session = _ScriptedSession([_m2_hello(), frame])

    result = remote_client.fetch(request_id, session_factory=lambda _argv: session)

    print("request identity:", "missing=", missing_field, "answered=", result.answered,
          "error=", result.error_code)
    if not missing_field:
        assert result.answered and not result.error_code
    else:
        assert result.error_code == "untrusted_reply" and not result.answered


# --------------------------------------------------------------------------- #
# pend shows a stored answer without the host config or the destination
# --------------------------------------------------------------------------- #


def test_pend_shows_a_stored_answer_after_the_host_config_is_removed(fake_ssh, monkeypatch):
    request_id = _accepted_reply_receipt(monkeypatch)
    with monkeypatch.context() as context:
        context.setattr(remote_client, "fetch", lambda _rid: _answered("cached answer"))
        remote_client.poll_receipt(request_id)
        control = io.StringIO()
        assert remote_client.remote_pend(request_id, out=control, err=io.StringIO()) == 0
        assert "cached answer" in control.getvalue()

    remote_client.hosts_config_path().unlink()
    out, err = io.StringIO(), io.StringIO()
    code = remote_client.remote_pend(request_id, out=out, err=err)

    print("cached pend:", "exit=", code, "error=", err.getvalue().strip())
    assert code == 0 and "cached answer" in out.getvalue()
    assert _receipt(request_id)["state"] == remote_client.STATE_FETCHED


# --------------------------------------------------------------------------- #
# The source daemon's poller
# --------------------------------------------------------------------------- #


def test_watched_receipts_are_scoped_to_their_owner(tmp_path, fake_ssh, monkeypatch):
    owner = "/home/user/.cache/ccb/projects/0123456789abcdef/askd.json"
    other = "/home/user/.cache/ccb/projects/fedcba9876543210/askd.json"
    mine = _accepted_reply_receipt(monkeypatch, poll_owner=owner)
    theirs = _accepted_reply_receipt(monkeypatch, poll_owner=other)

    assert remote_client.watched_receipts(owner) == [mine]
    assert remote_client.watched_receipts(other) == [theirs]
    assert remote_client.watched_receipts("") == []


def test_the_watcher_polls_only_its_own_receipts_and_notifies_once(
    tmp_path, fake_ssh, monkeypatch, pane_backend
):
    monkeypatch.setattr(remote_client, "REPLY_BACKOFF_START_S", 0.01)
    monkeypatch.setattr(remote_client, "REPLY_BACKOFF_CAP_S", 0.02)
    owner = str(tmp_path / "askd.json")
    mine = _accepted_reply_receipt(monkeypatch, poll_owner=owner)
    other = _accepted_reply_receipt(
        monkeypatch, poll_owner="/home/user/.cache/ccb/projects/0123456789abcdef/askd.json"
    )
    monkeypatch.setattr(remote_client, "fetch", lambda _rid: _answered("watched answer"))

    stop, wake = threading.Event(), threading.Event()
    thread = threading.Thread(
        target=remote_client.watch_loop, args=(stop, wake), kwargs={"owner": owner}
    )
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _receipt(mine).get("state") == remote_client.STATE_NOTIFIED:
                break
            time.sleep(0.02)
        else:  # pragma: no cover - the watcher failed
            pytest.fail("the watcher did not notify in time")
        # Give a second cycle the chance to (wrongly) notify again.
        time.sleep(0.15)
    finally:
        stop.set()
        wake.set()
        thread.join(timeout=10)

    assert _receipt(mine)["state"] == remote_client.STATE_NOTIFIED
    assert len(pane_backend.sent) == 1
    assert _receipt(other)["state"] == remote_client.STATE_ACCEPTED
    assert remote_client.watched_receipts(owner) == []


def test_watchable_covers_every_source_state(tmp_path, fake_ssh, monkeypatch):
    base = {"reply_expected": True, "state": remote_client.STATE_ACCEPTED}
    assert remote_client.watchable(base) is True
    assert remote_client.watchable({**base, "state": remote_client.STATE_ACCEPTANCE_UNKNOWN}) is True
    assert remote_client.watchable({**base, "state": remote_client.STATE_FETCH_UNAVAILABLE}) is True
    assert remote_client.watchable({**base, "state": remote_client.STATE_ANSWER_AVAILABLE}) is True
    assert remote_client.watchable({**base, "state": remote_client.STATE_NOT_DELIVERED}) is False
    assert remote_client.watchable({**base, "state": remote_client.STATE_NOTIFIED}) is False
    assert remote_client.watchable({**base, "state": remote_client.STATE_FETCHED,
                                    "notify_attempted_at": "x"}) is False
    assert remote_client.watchable({"reply_expected": False, "state": "accepted"}) is False


def test_pend_repairs_or_refuses_cached_text_that_fails_its_digest(fake_ssh, monkeypatch):
    import io

    request_id = _accepted_reply_receipt(monkeypatch)
    original = "answer from the destination"
    monkeypatch.setattr(remote_client, "fetch", lambda rid: _answered(original))
    remote_client.poll_receipt(request_id)
    good = io.StringIO()
    assert remote_client.remote_pend(request_id, out=good, err=io.StringIO()) == 0
    assert original in good.getvalue()

    # Corrupt only the cached text; the destination still returns the verified original.
    remote_client.update_receipt(request_id, lambda r: r.update(reply_text="invalid cached text"))
    out, err = io.StringIO(), io.StringIO()
    assert remote_client.remote_pend(request_id, out=out, err=err) == 0
    assert "invalid cached text" not in out.getvalue()
    assert original in out.getvalue()
    assert remote_client.read_receipt(request_id)["reply_text"] == original

    # Corrupt again with the destination unreachable: refuse, never print the bad text.
    remote_client.update_receipt(request_id, lambda r: r.update(reply_text="invalid cached text"))

    def _down(_rid):
        raise remote_client.RemoteRefusal("destination unreachable")

    monkeypatch.setattr(remote_client, "fetch", _down)
    out, err = io.StringIO(), io.StringIO()
    code = remote_client.remote_pend(request_id, out=out, err=err)
    assert code != 0
    assert "invalid cached text" not in out.getvalue()
