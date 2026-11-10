"""Regressions for the seven T2 review findings (ratifier CONTEST, lead-verified)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

import remote_delivery
import remote_journal
import remote_protocol as proto
from askd.adapters.base import PeerDestination, ProviderRequest, ProviderResult, QueuedTask
from remote_delivery import (
    Destination,
    RemoteDeliveryService,
    deliver_remote_task,
    record_outcome_unknown,
    record_pre_dispatch_failure,
)
from remote_journal import (
    STATE_ACCEPTED,
    STATE_DELIVERED,
    STATE_DISPATCHING,
    STATE_FAILED,
    STATE_UNKNOWN,
    JournalError,
    RemoteJournal,
    load_config,
)

LOCAL_ID = "20261109-182536-922-2626995"
REQUEST_ID = f"{LOCAL_ID}-habc1"
OWNER = "/tmp/home/.cache/ccb/projects/p1/askd.json"
OTHER_OWNER = "/tmp/home/.cache/ccb/projects/p2/askd.json"

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


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def _config_document(*, shared_token: bool = False) -> dict:
    eve_token = "abc1" if shared_token else "xyz9"
    return {
        "principals": {
            "bob": {"issuer_token": "abc1", "allowed_aliases": ["example-project"]},
            "eve": {"issuer_token": eve_token, "allowed_aliases": ["example-project"]},
        },
        "aliases": {
            "example-project": {"work_dir": "/tmp/example-project", "providers": ["claude", "codex"]},
        },
    }


class _Adapter:
    def __init__(self, key: str = "claude"):
        self.key = key

    def load_session(self, _path):
        return None

    def handle_exception(self, exc, task):
        return ProviderResult(
            exit_code=1, reply=str(exc), req_id=task.req_id, session_key=self.key,
            done_seen=False, status="failed",
        )


class _Registry:
    def __init__(self, *adapters):
        self._adapters = {a.key: a for a in adapters}

    def get(self, key):
        return self._adapters.get(key)


class _Recorder:
    def __init__(self):
        self.calls: list[tuple] = []
        self.preset: ProviderResult | None = None
        self.refused_before_queue = False
        self.returns_none = False
        self.on_enqueue = None

    def __call__(self, provider, request, record_id):
        self.calls.append((provider, request, record_id))
        if self.on_enqueue is not None:
            return self.on_enqueue(provider, request, record_id)
        if self.returns_none:
            return None
        task = QueuedTask(request=request, created_ms=0, req_id=request.req_id or "", done_event=threading.Event())
        task.remote_record_id = record_id
        task.remote_owner = OWNER
        task.refused_before_queue = self.refused_before_queue
        if self.preset is not None:
            task.result = self.preset
            task.done_event.set()
        return task


def _service(tmp_path: Path, *, recorder=None, resolver=None, owner=OWNER, config=None, journal=None):
    config_path = tmp_path / "remote" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config or _config_document()), encoding="utf-8")
    recorder = recorder or _Recorder()
    service = RemoteDeliveryService(
        registry=_Registry(_Adapter("claude")),
        enqueue=recorder,
        owner_daemon=owner,
        journal=journal or RemoteJournal(tmp_path / "remote"),
        config_loader=lambda: load_config(config_path),
        resolver=resolver or (lambda provider, work_dir, session: DESTINATION),
    )
    return service, recorder


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


def _submit_msg(record: dict, principal: str = "bob", msg_id: str = "op-1") -> dict:
    return {
        "type": "ask.request",
        "v": 1,
        "id": msg_id,
        "operation": "remote_submit",
        "principal": principal,
        "record": record,
    }


class _Backend:
    def __init__(self, *, fail_after_write: bool = False, fail_with=None):
        self.sent: list[tuple[str, str]] = []
        self.fail_after_write = fail_after_write
        self.fail_with = fail_with

    def send_text(self, pane_id: str, text: str) -> None:
        self.sent.append((pane_id, text))
        if self.fail_with is not None:
            raise self.fail_with
        if self.fail_after_write:
            raise OSError("pane vanished mid-paste")


def _delivery_task(request_id: str = REQUEST_ID, owner: str = OWNER) -> QueuedTask:
    request = ProviderRequest(
        client_id=request_id,
        work_dir="/tmp/example-project",
        timeout_s=-1.0,
        quiet=True,
        message="body",
        caller="remote:bob",
        req_id=request_id,
        delivery_only=True,
        suppress_completion_hook=True,
        peer_destination=PeerDestination(
            pane_id="%9", terminal="tmux", work_dir="/tmp/example-project"
        ),
        kind="notify",
    )
    task = QueuedTask(request=request, created_ms=0, req_id=request_id, done_event=threading.Event())
    task.remote_record_id = request_id
    task.remote_owner = owner
    return task


def _patch_send(monkeypatch, backend, *, revalidate_ok=True, refusal=""):
    import delivery_policy
    import peer_routing
    import terminal

    monkeypatch.setattr(terminal, "get_backend_for_session", lambda _data: backend)
    monkeypatch.setattr(
        peer_routing,
        "revalidate_peer_destination",
        lambda _destination: peer_routing.DestinationResolution()
        if revalidate_ok
        else peer_routing.DestinationResolution(error="unavailable", detail="pane replaced"),
    )
    monkeypatch.setattr(delivery_policy, "refusal_for_request", lambda *a, **k: refusal)


def _seed(journal: RemoteJournal, request_id: str = REQUEST_ID, **overrides) -> dict:
    record = {
        "request_id": request_id,
        "digest": "a" * 64,
        "principal": "bob",
        "issuer_token": "abc1",
        "dest_project": "example-project",
        "dest_provider": "claude",
        "dest_session": "",
        "work_dir": "/tmp/example-project",
        "pane_id": DESTINATION.pane_id,
        "terminal": DESTINATION.terminal,
        "pane_title_marker": DESTINATION.pane_title_marker,
        "ccb_project_id": DESTINATION.ccb_project_id,
        "live_id": DESTINATION.live_id,
        "launch_id": DESTINATION.launch_id,
        "intent": "notify",
        "text": "hello",
        "reply_expected": False,
        "owner_daemon": OWNER,
        "state": STATE_ACCEPTED,
    }
    record.update(overrides)
    journal.create(record)
    return record


# --------------------------------------------------------------------------- #
# Fix 1: post-dispatch failures are never `failed`, and never overwritten
# --------------------------------------------------------------------------- #


def test_synchronous_partial_send_is_never_overwritten_to_failed(tmp_path, monkeypatch):
    """The ratifier's probe: a worker that runs inside `enqueue` and partially sends."""
    _recorder = _Recorder()
    service, recorder = _service(
        tmp_path,
        recorder=_recorder,
        journal=RemoteJournal(Path.home() / ".ccb" / "remote"),
    )
    backend = _Backend(fail_after_write=True)
    _patch_send(monkeypatch, backend)

    def run_synchronously(provider, request, record_id):
        task = _delivery_task(record_id)
        task.request = request
        result = deliver_remote_task(task, provider, "peer:key")
        task.result = result
        return task

    recorder.on_enqueue = run_synchronously

    response = service.handle_submit(_submit_msg(_record()))

    # The request WAS accepted (durably) and the send may have happened: the
    # acknowledgement reports that, and the record keeps its honest state.
    assert response["ok"] is True
    assert response["remote"]["state"] == STATE_UNKNOWN
    record = service.journal.read(REQUEST_ID)
    assert record["state"] == STATE_UNKNOWN
    assert STATE_FAILED not in [entry["state"] for entry in record["history"]]


def test_synchronous_successful_send_is_acknowledged(tmp_path, monkeypatch):
    """Positive control for the probe above."""
    recorder = _Recorder()
    service, _ = _service(
        tmp_path, recorder=recorder, journal=RemoteJournal(Path.home() / ".ccb" / "remote")
    )
    backend = _Backend()
    _patch_send(monkeypatch, backend)

    def run_synchronously(provider, request, record_id):
        task = _delivery_task(record_id)
        task.request = request
        task.result = deliver_remote_task(task, provider, "peer:key")
        return task

    recorder.on_enqueue = run_synchronously
    response = service.handle_submit(_submit_msg(_record()))

    assert response["ok"] is True
    assert response["remote"]["state"] == STATE_DELIVERED
    assert service.journal.read(REQUEST_ID)["state"] == STATE_DELIVERED


def test_unmarked_preset_failure_is_not_a_proven_refusal(tmp_path):
    recorder = _Recorder()
    recorder.preset = ProviderResult(
        exit_code=1, reply="some failure", req_id=REQUEST_ID, session_key="peer:x",
        done_seen=False, status="failed",
    )
    service, _ = _service(tmp_path, recorder=recorder)

    response = service.handle_submit(_submit_msg(_record()))

    # Accepted and queued, but nothing may claim it was dispatched: the
    # unmarked result is ignored rather than trusted as a refusal.
    assert response["ok"] is True
    record = service.journal.read(REQUEST_ID)
    assert record["state"] == STATE_ACCEPTED
    assert STATE_FAILED not in [entry["state"] for entry in record["history"]]


def test_marked_enqueue_refusal_is_a_proven_refusal(tmp_path):
    """Positive control: with the explicit marker it IS pre-dispatch."""
    recorder = _Recorder()
    recorder.refused_before_queue = True
    recorder.preset = ProviderResult(
        exit_code=1, reply="Peer destination is no longer available (unavailable).",
        req_id=REQUEST_ID, session_key="peer:x", done_seen=False, status="failed",
    )
    service, _ = _service(tmp_path, recorder=recorder)

    response = service.handle_submit(_submit_msg(_record()))

    assert response["error"]["code"] == proto.ERR_DESTINATION_UNAVAILABLE
    assert service.journal.read(REQUEST_ID)["state"] == STATE_FAILED


def test_journal_read_failure_after_enqueue_is_acceptance_unknown(tmp_path, monkeypatch):
    service, recorder = _service(tmp_path)
    original_read = service.journal.read

    def read_or_fail(request_id):
        if service.journal.record_path(request_id).exists():
            raise JournalError("read after enqueue failed")
        return original_read(request_id)

    monkeypatch.setattr(service.journal, "read", read_or_fail)
    response = service.handle_submit(_submit_msg(_record()))

    assert response["ok"] is False
    assert response["error"]["code"] == proto.ERR_ACCEPTANCE_UNKNOWN
    assert "nothing was accepted" not in json.dumps(response)
    assert len(recorder.calls) == 1  # it really was enqueued


def test_post_dispatch_state_cannot_be_overwritten_by_the_handler(tmp_path):
    """A state that moved on must never be rewritten to `failed`."""
    service, recorder = _service(tmp_path)
    journal = service.journal

    def enqueue_and_advance(provider, request, record_id):
        # Another actor (here: the worker) already committed dispatch intent.
        journal.transition_if(record_id, STATE_ACCEPTED, STATE_DISPATCHING, "worker")
        task = QueuedTask(request=request, created_ms=0, req_id=record_id, done_event=threading.Event())
        task.remote_record_id = record_id
        task.remote_owner = OWNER
        task.refused_before_queue = True
        task.result = ProviderResult(exit_code=1, reply="refused", req_id=record_id,
                                     session_key="peer:x", done_seen=False, status="failed")
        return task

    recorder.on_enqueue = enqueue_and_advance
    response = service.handle_submit(_submit_msg(_record()))

    assert response["error"]["code"] == proto.ERR_ACCEPTANCE_UNKNOWN
    assert service.journal.read(REQUEST_ID)["state"] == STATE_DISPATCHING


def test_pane_lock_error_after_the_dispatch_hook_is_outcome_unknown(tmp_path, monkeypatch):
    from pane_lock import PaneLockTimeout

    journal = RemoteJournal()
    _seed(journal)
    backend = _Backend(fail_with=PaneLockTimeout("held by another sender"))
    _patch_send(monkeypatch, backend)

    result = deliver_remote_task(_delivery_task(), "claude", "peer:key")

    assert result.status == "failed"  # the ProviderResult label is unchanged
    record = journal.read(REQUEST_ID)
    assert record["state"] == STATE_UNKNOWN, "a post-dispatch lock error is not a proven non-delivery"
    assert STATE_FAILED not in [entry["state"] for entry in record["history"]]


def test_lock_acquisition_failure_before_the_hook_is_failed(tmp_path, monkeypatch):
    """Positive control: failing to ACQUIRE (nothing sent) is a proven refusal."""
    import pane_lock

    journal = RemoteJournal()
    _seed(journal)
    backend = _Backend()
    _patch_send(monkeypatch, backend)
    monkeypatch.setenv("CCB_PANE_LOCK_TIMEOUT_S", "0.1")

    release = threading.Event()
    started = threading.Event()

    def hold():
        with pane_lock.pane_injection_lock(backend, "%9", 5.0):
            started.set()
            release.wait(5.0)

    thread = threading.Thread(target=hold)
    thread.start()
    assert started.wait(5.0)
    try:
        result = deliver_remote_task(_delivery_task(), "claude", "peer:key")
    finally:
        release.set()
        thread.join(5.0)

    assert result.status == "failed"
    assert backend.sent == []
    assert journal.read(REQUEST_ID)["state"] == STATE_FAILED


# --------------------------------------------------------------------------- #
# Fix 2: fsync failures propagate, and are classified honestly
# --------------------------------------------------------------------------- #


def test_dir_fsync_failure_after_replace_is_acceptance_unknown(tmp_path, monkeypatch):
    journal = RemoteJournal(tmp_path / "remote")
    service, recorder = _service(tmp_path, journal=journal)

    def boom(_path):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(remote_journal, "_fsync_dir", boom)
    response = service.handle_submit(_submit_msg(_record()))

    assert response["error"]["code"] == proto.ERR_ACCEPTANCE_UNKNOWN
    assert recorder.calls == [], "an unconfirmed write must never be enqueued"


def test_dir_fsync_failure_marks_the_error_as_may_exist(tmp_path, monkeypatch):
    journal = RemoteJournal(tmp_path / "remote")
    monkeypatch.setattr(remote_journal, "_fsync_dir", lambda _p: (_ for _ in ()).throw(OSError("EIO")))
    with pytest.raises(JournalError) as excinfo:
        journal.create(_seed.__wrapped__({"request_id": REQUEST_ID, "state": STATE_ACCEPTED}) if hasattr(_seed, "__wrapped__") else {"request_id": REQUEST_ID, "state": STATE_ACCEPTED})
    assert excinfo.value.may_exist is True


def test_file_fsync_failure_is_nothing_accepted(tmp_path, monkeypatch):
    service, recorder = _service(tmp_path)

    def boom(_fd):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(remote_journal.os, "fsync", boom)
    response = service.handle_submit(_submit_msg(_record()))

    assert response["error"]["code"] == proto.ERR_JOURNAL_FAILURE
    assert "nothing was accepted" in response["error"]["message"]
    assert recorder.calls == []
    assert service.journal.read(REQUEST_ID) is None


def test_dir_fsync_propagates_in_plain_journal_use(tmp_path, monkeypatch):
    """Positive control for the two tests above: the same write succeeds unpatched."""
    journal = RemoteJournal(tmp_path / "remote")
    journal.create({"request_id": REQUEST_ID, "state": STATE_ACCEPTED, "digest": "a" * 64})
    assert journal.read(REQUEST_ID)["state"] == STATE_ACCEPTED


# --------------------------------------------------------------------------- #
# Fix 3: recovery never adopts a replacement destination
# --------------------------------------------------------------------------- #


def test_recovery_refuses_a_replaced_pane(tmp_path):
    replacement = Destination(
        provider="claude", live_id="live-9", launch_id="ai-1",
        work_dir="/tmp/example-project", pane_id="%99", terminal="tmux",
        pane_title_marker="CCB-claude-9", ccb_project_id="proj-1",
    )
    service, recorder = _service(tmp_path, resolver=lambda p, w, s: replacement)
    _seed(service.journal)

    summary = service.recover()

    assert summary["failed"] == 1 and summary["requeued"] == 0
    assert recorder.calls == []
    record = service.journal.read(REQUEST_ID)
    assert record["state"] == STATE_FAILED
    assert "destination replaced" in record["history"][-1]["reason"]


def test_recovery_refuses_a_replacement_when_dest_session_was_omitted(tmp_path):
    replacement = Destination(
        provider="claude", live_id="live-9", launch_id="ai-1",
        work_dir="/tmp/example-project", pane_id="%9", terminal="tmux",
        pane_title_marker="CCB-claude-1", ccb_project_id="proj-1",
    )
    service, recorder = _service(tmp_path, resolver=lambda p, w, s: replacement)
    _seed(service.journal)  # dest_session is empty: the binding still decides

    summary = service.recover()

    assert summary["failed"] == 1
    assert recorder.calls == []
    assert "destination replaced" in service.journal.read(REQUEST_ID)["history"][-1]["reason"]


def test_recovery_requeues_the_bound_destination(tmp_path):
    """Positive control: the SAME destination is requeued, not refused."""
    service, recorder = _service(tmp_path)
    _seed(service.journal)

    summary = service.recover()

    assert summary["requeued"] == 1
    assert [call[2] for call in recorder.calls] == [REQUEST_ID]
    assert service.journal.read(REQUEST_ID)["state"] == STATE_ACCEPTED


# --------------------------------------------------------------------------- #
# Fix 4: ownership, idempotent recovery, recovery after ownership
# --------------------------------------------------------------------------- #


def test_recovery_leaves_another_daemons_dispatch_in_progress_alone(tmp_path):
    service, recorder = _service(tmp_path)
    _seed(service.journal, state=STATE_DISPATCHING, owner_daemon=OTHER_OWNER)

    summary = service.recover()

    assert summary["unknown"] == 0 and summary["skipped"] == 1
    record = service.journal.read(REQUEST_ID)
    assert record["state"] == STATE_DISPATCHING
    assert recorder.calls == []


def test_second_recovery_call_does_not_requeue_again(tmp_path):
    service, recorder = _service(tmp_path)
    _seed(service.journal)

    first = service.recover()
    second = service.recover()

    assert first["requeued"] == 1
    assert second["requeued"] == 0 and second["skipped"] == 1
    assert len(recorder.calls) == 1


def test_transition_if_refuses_a_foreign_owner(tmp_path):
    journal = RemoteJournal(tmp_path / "remote")
    _seed(journal)

    assert journal.transition_if(REQUEST_ID, STATE_ACCEPTED, STATE_FAILED, "x", owner=OTHER_OWNER) is None
    assert journal.read(REQUEST_ID)["state"] == STATE_ACCEPTED
    # Positive control: the owner may transition.
    assert journal.transition_if(REQUEST_ID, STATE_ACCEPTED, STATE_FAILED, "x", owner=OWNER) is not None
    assert journal.read(REQUEST_ID)["state"] == STATE_FAILED


def test_transition_if_never_overwrites_an_unexpected_state(tmp_path):
    journal = RemoteJournal(tmp_path / "remote")
    _seed(journal, state=STATE_DELIVERED)
    assert journal.transition_if(REQUEST_ID, STATE_ACCEPTED, STATE_FAILED, "x") is None
    assert journal.read(REQUEST_ID)["state"] == STATE_DELIVERED


def test_worker_bookkeeping_is_owner_scoped(tmp_path):
    journal = RemoteJournal()
    _seed(journal, owner_daemon=OTHER_OWNER)

    record_pre_dispatch_failure(REQUEST_ID, "x", owner=OWNER)
    record_outcome_unknown(REQUEST_ID, "y", owner=OWNER)

    assert journal.read(REQUEST_ID)["state"] == STATE_ACCEPTED


def test_recovery_runs_only_after_ownership(tmp_path, monkeypatch):
    import askd_server
    from providers import ProviderDaemonSpec

    calls: list[str] = []

    def make_server(*, acquired: bool):
        class FakeLock:
            def __init__(self, *args, **kwargs):
                pass

            def try_acquire(self):
                calls.append("acquire")
                return acquired

        monkeypatch.setattr(askd_server, "ProviderLock", FakeLock)
        return askd_server.AskDaemonServer(
            spec=ProviderDaemonSpec(
                daemon_key="askd", protocol_prefix="ask", state_file_name="askd.json",
                log_file_name="askd.log", idle_timeout_env="X", lock_name="askd",
            ),
            token="t",
            state_file=tmp_path / "askd.json",
            request_handler=lambda msg: {},
            on_owned=lambda: calls.append("recover"),
        )

    denied = make_server(acquired=False)
    assert denied._take_ownership() is False
    assert calls == ["acquire"], "without ownership the hook must not run"

    calls.clear()
    owned = make_server(acquired=True)
    assert owned._take_ownership() is True
    assert calls == ["acquire", "recover"]


def test_daemon_wires_recovery_through_the_ownership_hook():
    source = Path("lib/askd/daemon.py").read_text(encoding="utf-8")
    assert "on_owned=self._on_owned" in source
    # `serve_forever` may only ACQUIRE ownership; it must neither call the hook
    # itself nor recover directly (that would run before ownership is held).
    serve = source.split("def serve_forever(self) -> int:", 1)[1]
    # Ownership is taken inside AskDaemonServer.serve_forever(); this daemon only
    # hands the hook over, so nothing here can recover before that.
    assert "on_owned=self._on_owned" in serve
    assert "self._recover_remote_records()" not in serve
    # The hook runs recovery first, then starts the M2 reply watcher.
    hook = source.split("def _on_owned(self) -> None:", 1)[1].split("def ", 1)[0]
    assert hook.index("self._recover_remote_records()") < hook.index("self._start_reply_watcher()")


def test_askd_server_runs_the_hook_after_acquiring_the_lock():
    source = Path("lib/askd_server.py").read_text(encoding="utf-8")
    take = source.split("def _take_ownership", 1)[1].split("def serve_forever", 1)[0]
    assert take.index("try_acquire()") < take.index("self.on_owned(")
    assert take.index("return False") < take.index("self.on_owned(")

    # serve_forever must go THROUGH the ownership gate, and must not fire the
    # hook on its own (that is how recovery ended up running before ownership).
    serve = source.split("def serve_forever(self) -> int:", 1)[1]
    assert "_take_ownership()" in serve
    assert "self.on_owned" not in serve
    assert "if not self._take_ownership():" in serve


# --------------------------------------------------------------------------- #
# Fix 7: duplicate submit is scoped to the submitting principal
# --------------------------------------------------------------------------- #


def test_duplicate_submit_for_another_principal_looks_missing(tmp_path):
    config = _config_document(shared_token=True)
    service, recorder = _service(tmp_path, config=config)

    first = service.handle_submit(_submit_msg(_record()))  # bob
    assert first["ok"] is True

    # eve shares the trace token and the alias, so she reaches the duplicate
    # check -- and must get exactly the answer `status` gives her.
    as_eve = service.handle_submit(_submit_msg(_record(), principal="eve", msg_id="op-2"))
    missing = service.handle_status(
        {"id": "op-3", "principal": "eve", "request_id": f"{LOCAL_ID}-hzzz9"}
    )

    assert as_eve["error"] == missing["error"]
    assert as_eve["error"]["code"] == proto.ERR_NOT_FOUND
    assert "already used" not in json.dumps(as_eve)
    assert len(recorder.calls) == 1

    # Positive control: the same principal still gets its own record back.
    again = service.handle_submit(_submit_msg(_record(), msg_id="op-4"))
    assert again["ok"] is True
    assert again["remote"]["request_id"] == REQUEST_ID
    assert len(recorder.calls) == 1
