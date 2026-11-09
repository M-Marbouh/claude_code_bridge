from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import remote_delivery
import remote_protocol as proto
from askd.daemon import UnifiedAskDaemon
from remote_delivery import Destination, RemoteError
from remote_journal import (
    STATE_ACCEPTED,
    STATE_DELIVERED,
    STATE_DISPATCHING,
    STATE_FAILED,
    STATE_UNKNOWN,
    RemoteJournal,
)

LOCAL_ID = "20261109-182536-922-2626995"
REQUEST_ID = f"{LOCAL_ID}-habc1"

DESTINATION = Destination(
    provider="claude",
    live_id="live-1",
    launch_id="ai-1",
    work_dir="/tmp/example-project",
    pane_id="%9",
    terminal="tmux",
    pane_title_marker="CCB-claude-1",
    ccb_project_id="proj-1",
)


class _Adapter:
    def __init__(self, key: str = "claude"):
        self.key = key

    def load_session(self, _path):  # pragma: no cover - peer path never binds a session
        return None

    def handle_exception(self, exc, task):  # pragma: no cover - only on worker failure
        from askd.adapters.base import ProviderResult

        return ProviderResult(
            exit_code=1, reply=str(exc), req_id=task.req_id, session_key=self.key,
            done_seen=False, status="failed",
        )


class _Registry:
    def __init__(self, *adapters):
        self._adapters = {adapter.key: adapter for adapter in adapters}

    def get(self, key):
        return self._adapters.get(key)


def _write_home_config() -> None:
    root = Path.home() / ".ccb" / "remote"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(
        json.dumps(
            {
                "principals": {
                    "bob": {"issuer_token": "abc1", "allowed_aliases": ["example-project"]},
                    "eve": {"issuer_token": "xyz9", "allowed_aliases": ["other-project"]},
                },
                "aliases": {
                    "example-project": {
                        "work_dir": "/tmp/example-project",
                        "providers": ["claude", "codex"],
                    },
                    "other-project": {"work_dir": "/tmp/other-project", "providers": ["claude"]},
                },
            }
        ),
        encoding="utf-8",
    )


def _daemon(tmp_path: Path) -> UnifiedAskDaemon:
    return UnifiedAskDaemon(registry=_Registry(_Adapter("claude")), state_file=tmp_path / "askd.json")


def _record(**overrides) -> dict:
    kwargs = dict(
        dest_project="example-project",
        dest_provider="claude",
        intent="notify",
        text="hello from another machine",
        request_id=REQUEST_ID,
        source_claims={"sender": "host-a"},
    )
    kwargs.update(overrides)
    return proto.build_request_record(**kwargs)


def _remote_submit(daemon, record, principal="bob", msg_id="op-1") -> dict:
    return daemon._handle_request(
        {
            "type": "ask.request",
            "v": 1,
            "id": msg_id,
            "operation": "remote_submit",
            "principal": principal,
            "record": record,
        }
    )


def _patch_delivery(monkeypatch, backend, *, refusal=""):
    import delivery_policy
    import peer_routing
    import terminal

    monkeypatch.setattr(remote_delivery, "resolve_destination", lambda p, w, s: DESTINATION)
    monkeypatch.setattr(terminal, "get_backend_for_session", lambda _data: backend)
    monkeypatch.setattr(
        peer_routing,
        "revalidate_peer_destination",
        lambda _destination: peer_routing.DestinationResolution(),
    )
    monkeypatch.setattr(delivery_policy, "refusal_for_request", lambda *a, **k: refusal)


class _RecordingBackend:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, pane_id: str, text: str) -> None:
        self.sent.append((pane_id, text))


def _wait_for_state(journal: RemoteJournal, request_id: str, state: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    record = journal.read(request_id)
    while time.monotonic() < deadline:
        record = journal.read(request_id)
        if record is not None and record.get("state") == state:
            return record
        time.sleep(0.02)
    return record or {}


def test_remote_list_is_served_by_the_daemon(tmp_path):
    _write_home_config()
    daemon = _daemon(tmp_path)
    response = daemon._handle_request({"operation": "remote_list", "id": "op-1", "principal": "bob"})
    assert response["ok"] is True
    assert [project["alias"] for project in response["remote"]["projects"]] == ["example-project"]


def test_remote_ops_never_fall_through_to_provider_dispatch(tmp_path):
    _write_home_config()
    daemon = _daemon(tmp_path)
    for operation, extra in (
        ("remote_status", {"request_id": REQUEST_ID}),
        ("remote_status", {"request_id": "not-an-id"}),
        ("remote_submit", {"record": {"bogus": True}}),
    ):
        response = daemon._handle_request({"operation": operation, "id": "op", "principal": "bob", **extra})
        assert response["ok"] is False
        assert "Missing 'provider' field" not in str(response.get("reply"))


def test_full_submit_path_accepts_then_delivers_once(tmp_path, monkeypatch):
    _write_home_config()
    daemon = _daemon(tmp_path)
    backend = _RecordingBackend()
    _patch_delivery(monkeypatch, backend)

    response = _remote_submit(daemon, _record())

    assert response["ok"] is True, response
    assert response["remote"]["state"] == STATE_ACCEPTED
    record = _wait_for_state(RemoteJournal(), REQUEST_ID, STATE_DELIVERED)
    assert record["state"] == STATE_DELIVERED
    assert [entry["state"] for entry in record["history"]] == [
        STATE_ACCEPTED,
        STATE_DISPATCHING,
        STATE_DELIVERED,
    ]
    assert len(backend.sent) == 1
    pane, prompt = backend.sent[0]
    assert pane == "%9"
    assert f"CCB_REQ_ID: {REQUEST_ID}" in prompt
    assert "relayed from another machine" in prompt
    assert "CCB_REPLY_EXPECTED: no" in prompt


def test_submit_to_a_gone_pane_is_refused_and_recorded_failed(tmp_path, monkeypatch):
    _write_home_config()
    daemon = _daemon(tmp_path)
    _patch_delivery(monkeypatch, _RecordingBackend())

    import peer_routing

    monkeypatch.setattr(
        peer_routing,
        "revalidate_peer_destination",
        lambda _destination: peer_routing.DestinationResolution(
            error="unavailable", detail="destination pane is no longer alive"
        ),
    )
    response = _remote_submit(daemon, _record())

    assert response["ok"] is False
    assert response["error"]["code"] == proto.ERR_DESTINATION_UNAVAILABLE
    assert RemoteJournal().read(REQUEST_ID)["state"] == STATE_FAILED


def test_daemon_startup_recovery_requeues_own_records_and_never_resends(tmp_path, monkeypatch):
    _write_home_config()
    daemon = _daemon(tmp_path)
    backend = _RecordingBackend()
    _patch_delivery(monkeypatch, backend)

    journal = RemoteJournal()
    template = {
        "request_id": REQUEST_ID,
        "digest": "a" * 64,
        "principal": "bob",
        "issuer_token": "abc1",
        "dest_project": "example-project",
        "dest_provider": "claude",
        "dest_session": "",
        "work_dir": "/tmp/example-project",
        "pane_id": "%9",
        "terminal": "tmux",
        "pane_title_marker": "CCB-claude-1",
        "ccb_project_id": "proj-1",
        "live_id": DESTINATION.live_id,
        "launch_id": DESTINATION.launch_id,
        "intent": "notify",
        "text": "hello",
        "reply_expected": False,
        "owner_daemon": str(daemon.state_file),
        "state": STATE_ACCEPTED,
    }
    unknown_id = f"{LOCAL_ID}-hzzz9"
    journal.create(dict(template))
    journal.create(dict(template, request_id=unknown_id, state=STATE_DISPATCHING))

    daemon._recover_remote_records()

    assert journal.read(unknown_id)["state"] == STATE_UNKNOWN
    record = _wait_for_state(journal, REQUEST_ID, STATE_DELIVERED)
    assert record["state"] == STATE_DELIVERED
    assert [entry["state"] for entry in record["history"]].count(STATE_DELIVERED) == 1
    assert len(backend.sent) == 1


def test_records_owned_by_another_daemon_are_left_alone(tmp_path, monkeypatch):
    _write_home_config()
    daemon = _daemon(tmp_path)
    backend = _RecordingBackend()
    _patch_delivery(monkeypatch, backend)

    journal = RemoteJournal()
    journal.create(
        {
            "request_id": REQUEST_ID,
            "digest": "a" * 64,
            "principal": "bob",
            "dest_project": "example-project",
            "dest_provider": "claude",
            "work_dir": "/tmp/example-project",
            "pane_id": "%9",
            "terminal": "tmux",
            "owner_daemon": "/some/other/project/askd.json",
            "state": STATE_ACCEPTED,
        }
    )
    daemon._recover_remote_records()
    time.sleep(0.1)
    assert backend.sent == []
    assert journal.read(REQUEST_ID)["state"] == STATE_ACCEPTED


def test_resolution_failure_is_reported_as_destination_unavailable(tmp_path, monkeypatch):
    _write_home_config()
    daemon = _daemon(tmp_path)

    def ambiguous(provider, work_dir, session):
        raise RemoteError(proto.ERR_DESTINATION_UNAVAILABLE, "more than one live claude session")

    monkeypatch.setattr(remote_delivery, "resolve_destination", ambiguous)
    response = _remote_submit(daemon, _record())
    assert response["error"]["code"] == proto.ERR_DESTINATION_UNAVAILABLE
    assert RemoteJournal().read(REQUEST_ID) is None


def test_worker_checkpoint_refusal_is_recorded_failed(tmp_path, monkeypatch):
    """The destination can disappear between acceptance and the send."""
    _write_home_config()
    daemon = _daemon(tmp_path)
    backend = _RecordingBackend()
    _patch_delivery(monkeypatch, backend)

    import peer_routing

    calls = {"n": 0}

    def flaky(_destination):
        calls["n"] += 1
        if calls["n"] == 1:
            return peer_routing.DestinationResolution()  # enqueue-time check passes
        return peer_routing.DestinationResolution(error="unavailable", detail="pane replaced")

    monkeypatch.setattr(peer_routing, "revalidate_peer_destination", flaky)
    response = _remote_submit(daemon, _record())

    assert response["ok"] is True  # accepted: the refusal came later
    record = _wait_for_state(RemoteJournal(), REQUEST_ID, STATE_FAILED)
    assert record["state"] == STATE_FAILED
    assert backend.sent == []


def test_unexpected_worker_exception_is_outcome_unknown(tmp_path, monkeypatch):
    _write_home_config()
    daemon = _daemon(tmp_path)
    backend = _RecordingBackend()
    _patch_delivery(monkeypatch, backend)

    import remote_delivery

    def boom(*_args, **_kwargs):
        raise RuntimeError("worker blew up")

    monkeypatch.setattr(remote_delivery, "deliver_remote_task", boom)
    assert _remote_submit(daemon, _record())["ok"] is True

    record = _wait_for_state(RemoteJournal(), REQUEST_ID, STATE_UNKNOWN)
    assert record["state"] == STATE_UNKNOWN
    assert STATE_FAILED not in [entry["state"] for entry in record["history"]]
