from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import remote_delivery
import remote_protocol as proto
from askd.adapters.base import ProviderResult, QueuedTask
from remote_delivery import Destination, RemoteDeliveryService, RemoteError, deliver_remote_task
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
EVE_REQUEST_ID = f"{LOCAL_ID}-hxyz9"
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


def _config_document() -> dict:
    return {
        "principals": {
            "bob": {"issuer_token": "abc1", "allowed_aliases": ["example-project"]},
            "eve": {"issuer_token": "xyz9", "allowed_aliases": ["other-project"]},
            "nobody": {"issuer_token": "noac", "allowed_aliases": []},
        },
        "aliases": {
            "example-project": {"work_dir": "/tmp/example-project", "providers": ["claude", "codex"]},
            "other-project": {"work_dir": "/tmp/other-project", "providers": ["claude"]},
            "claude-only": {"work_dir": "/tmp/claude-only", "providers": ["claude"]},
        },
    }


class _Adapter:
    def __init__(self, key: str = "claude"):
        self.key = key


class _Registry:
    def __init__(self, *adapters):
        self._adapters = {adapter.key: adapter for adapter in adapters}

    def get(self, key):
        return self._adapters.get(key)


class _Recorder:
    """Collects enqueued requests; can preset a refusal result."""

    def __init__(self):
        self.calls: list[tuple[str, object, str]] = []
        self.preset: ProviderResult | None = None
        self.refused_before_queue = False
        self.returns_none = False

    def __call__(self, provider, request, record_id):
        self.calls.append((provider, request, record_id))
        if self.returns_none:
            return None
        task = QueuedTask(request=request, created_ms=0, req_id=request.req_id or "", done_event=threading.Event())
        task.remote_record_id = record_id
        task.refused_before_queue = self.refused_before_queue
        if self.preset is not None:
            task.result = self.preset
            task.done_event.set()
        return task


def _record(**overrides) -> dict:
    kwargs = dict(
        dest_project="example-project",
        dest_provider="claude",
        intent="notify",
        text="hello from the other machine",
        request_id=REQUEST_ID,
        source_claims={"sender": "host-a", "note": "unverified"},
    )
    kwargs.update(overrides)
    return proto.build_request_record(**kwargs)


def _mutated(record: dict, **changes) -> dict:
    """Change a built record and re-sign it, to exercise the service's own refusals."""
    base = {k: v for k, v in record.items() if k != "digest"}
    base.update(changes)
    base["digest"] = proto.compute_digest(base)
    return base


def _service(
    tmp_path: Path, *, recorder=None, resolver=None, sessions=None, journal=None, owner=OWNER, config=None
):
    config_path = tmp_path / "remote" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config if config is not None else _config_document()), encoding="utf-8")
    recorder = recorder or _Recorder()
    service = RemoteDeliveryService(
        registry=_Registry(_Adapter("claude")),
        enqueue=recorder,
        owner_daemon=owner,
        journal=journal or RemoteJournal(tmp_path / "remote"),
        config_loader=lambda: load_config(config_path),
        resolver=resolver or (lambda provider, work_dir, session: DESTINATION),
        # Session choices are read from the host's own inventory; a unit test
        # supplies them explicitly instead of depending on the real registry.
        sessions=sessions or (lambda provider, work_dir: []),
    )
    return service, recorder


def _submit_msg(record: dict, principal: str = "bob", msg_id: str = "op-1") -> dict:
    return {"type": "ask.request", "v": 1, "id": msg_id, "operation": "remote_submit", "principal": principal, "record": record}


# --------------------------------------------------------------------------- #
# Acceptance
# --------------------------------------------------------------------------- #


def test_submit_accepts_records_and_enqueues_exactly_once(tmp_path):
    service, recorder = _service(tmp_path)
    response = service.handle_submit(_submit_msg(_record()))

    assert response["ok"] is True
    assert response["remote"]["state"] == STATE_ACCEPTED
    assert response["remote"]["request_id"] == REQUEST_ID
    assert len(recorder.calls) == 1

    provider, request, record_id = recorder.calls[0]
    assert provider == "claude"
    assert record_id == REQUEST_ID
    assert request.req_id == REQUEST_ID
    assert request.delivery_only is True
    assert request.suppress_completion_hook is True
    assert request.kind == "notify"
    assert request.peer_destination.pane_id == "%9"
    assert request.peer_destination.terminal == "tmux"
    assert f"CCB_REQ_ID: {REQUEST_ID}" not in request.message  # wrapper is applied at send time
    assert "CCB_REPLY_EXPECTED: no" in request.message
    assert "CCB_REMOTE_SOURCE_HOST: abc1" in request.message
    assert "unverified" in request.message
    assert request.message.rstrip().endswith("hello from the other machine")

    stored = service.journal.read(REQUEST_ID)
    assert stored["state"] == STATE_ACCEPTED
    assert stored["principal"] == "bob"
    assert stored["issuer_token"] == "abc1"
    assert stored["pane_id"] == "%9"
    assert stored["owner_daemon"] == OWNER
    assert stored["reply_expected"] is False
    assert stored["source_claims"] == {"sender": "host-a", "note": "unverified"}
    assert [entry["state"] for entry in stored["history"]] == [STATE_ACCEPTED]


def test_ack_does_not_wait_for_or_require_delivery(tmp_path):
    service, recorder = _service(tmp_path)
    began = time.monotonic()
    response = service.handle_submit(_submit_msg(_record()))
    assert time.monotonic() - began < 2.0
    assert response["ok"] is True
    # The delivery has not happened yet; the ack still stands.
    assert service.journal.read(REQUEST_ID)["state"] == STATE_ACCEPTED


def test_delivery_only_and_reply_expected_are_enforced_before_acceptance(tmp_path):
    service, recorder = _service(tmp_path)
    response = service.handle_submit(_submit_msg(_mutated(_record(), reply_expected=True)))
    assert response["ok"] is False
    assert response["error"]["code"] == proto.ERR_REPLY_NOT_SUPPORTED
    assert recorder.calls == []
    assert service.journal.read(REQUEST_ID) is None


@pytest.mark.parametrize("provider", ["gemini", "hermes", "pi"])
def test_unsupported_provider_refused_before_acceptance(tmp_path, provider):
    service, recorder = _service(tmp_path)
    response = service.handle_submit(_submit_msg(_mutated(_record(), dest_provider=provider)))
    assert response["error"]["code"] == proto.ERR_UNSUPPORTED_PROVIDER
    assert recorder.calls == []
    assert service.journal.read(REQUEST_ID) is None


def test_tampered_record_is_refused_by_its_digest(tmp_path):
    service, recorder = _service(tmp_path)
    record = _record()
    record["text"] = "swapped after signing"
    response = service.handle_submit(_submit_msg(record))
    assert response["error"]["code"] == proto.ERR_DIGEST_MISMATCH
    assert recorder.calls == []


def test_missing_digest_is_refused(tmp_path):
    service, recorder = _service(tmp_path)
    record = _record()
    record.pop("digest")
    response = service.handle_submit(_submit_msg(record))
    assert response["error"]["code"] == proto.ERR_INVALID_REQUEST
    assert recorder.calls == []


# --------------------------------------------------------------------------- #
# Authorization: no record, no ack, no dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "principal,record_overrides",
    [
        ("ghost", {}),  # unknown principal
        ("bob", {}),  # known principal, but the id names eve's issuer token
        ("nobody", {}),  # known principal with an empty ACL
        ("eve", {}),  # valid principal, alias not in its ACL
    ],
)
def test_unauthorized_submits_are_refused_without_side_effects(tmp_path, principal, record_overrides):
    service, recorder = _service(tmp_path)
    request_id = REQUEST_ID if principal != "bob" else EVE_REQUEST_ID
    record = _record(request_id=request_id, **record_overrides)
    if principal == "bob":
        # bob's own token is abc1; signing with xyz9 must not authorize.
        record = _record(request_id=EVE_REQUEST_ID)
    response = service.handle_submit(_submit_msg(record, principal=principal))
    assert response["ok"] is False
    assert response["error"]["code"] in {proto.ERR_UNAUTHORIZED, proto.ERR_NOT_FOUND}
    assert recorder.calls == []
    assert service.journal.read(str(record["request_id"])) is None


def test_provider_not_allowed_for_the_alias_is_refused(tmp_path):
    config = _config_document()
    config["principals"]["bob"]["allowed_aliases"] = ["claude-only"]
    service, recorder = _service(tmp_path, config=config)
    response = service.handle_submit(
        _submit_msg(_record(dest_project="claude-only", dest_provider="codex"))
    )
    assert response["error"]["code"] == proto.ERR_UNSUPPORTED_PROVIDER
    assert recorder.calls == []


def test_ambiguous_or_absent_destination_is_refused_before_acceptance(tmp_path):
    def ambiguous(provider, work_dir, session):
        raise RemoteError(proto.ERR_DESTINATION_UNAVAILABLE, "more than one live claude session")

    service, recorder = _service(tmp_path, resolver=ambiguous)
    response = service.handle_submit(_submit_msg(_record()))
    assert response["error"]["code"] == proto.ERR_DESTINATION_UNAVAILABLE
    assert recorder.calls == []
    assert service.journal.read(REQUEST_ID) is None


# --------------------------------------------------------------------------- #
# Durability
# --------------------------------------------------------------------------- #


def test_journal_write_failure_means_no_ack_and_zero_dispatch(tmp_path, monkeypatch):
    service, recorder = _service(tmp_path)

    def boom(*_args, **_kwargs):
        raise JournalError("disk full")

    monkeypatch.setattr(service.journal, "create", boom)
    response = service.handle_submit(_submit_msg(_record()))

    assert response["ok"] is False
    assert response["error"]["code"] == proto.ERR_JOURNAL_FAILURE
    assert recorder.calls == []


def test_journal_is_written_before_the_ack_and_before_enqueue(tmp_path):
    order: list[str] = []
    service, recorder = _service(tmp_path)
    original_create = service.journal.create

    def create(record):
        order.append("journal")
        original_create(record)

    def enqueue(provider, request, record_id):
        order.append("enqueue")
        return recorder(provider, request, record_id)

    service.journal.create = create  # type: ignore[assignment]
    service.enqueue = enqueue
    assert service.handle_submit(_submit_msg(_record()))["ok"] is True
    assert order == ["journal", "enqueue"]


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_same_id_same_digest_returns_state_without_a_second_dispatch(tmp_path):
    service, recorder = _service(tmp_path)
    first = service.handle_submit(_submit_msg(_record()))
    second = service.handle_submit(_submit_msg(_record()))
    assert first["ok"] is True and second["ok"] is True
    assert second["remote"]["state"] == STATE_ACCEPTED
    assert len(recorder.calls) == 1


def test_same_id_different_payload_or_target_is_refused(tmp_path):
    config = _config_document()
    config["principals"]["bob"]["allowed_aliases"] = ["example-project", "claude-only"]
    service, recorder = _service(tmp_path, config=config)
    assert service.handle_submit(_submit_msg(_record()))["ok"] is True

    changed_text = service.handle_submit(_submit_msg(_record(text="a different message")))
    assert changed_text["error"]["code"] == proto.ERR_IDEMPOTENCY_CONFLICT

    changed_target = service.handle_submit(_submit_msg(_record(dest_project="claude-only")))
    assert changed_target["error"]["code"] == proto.ERR_IDEMPOTENCY_CONFLICT
    assert len(recorder.calls) == 1


def test_same_local_id_under_two_issuer_tokens_is_two_records(tmp_path):
    service, recorder = _service(tmp_path)
    bob = service.handle_submit(_submit_msg(_record()))
    eve_record = _record(request_id=EVE_REQUEST_ID, dest_project="other-project")
    eve = service.handle_submit(_submit_msg(eve_record, principal="eve"))
    assert bob["ok"] is True and eve["ok"] is True
    assert len(recorder.calls) == 2
    assert len(service.journal.iter_records()) == 2


def test_enqueue_time_refusal_marks_failed_and_is_not_acknowledged(tmp_path):
    recorder = _Recorder()
    recorder.preset = ProviderResult(
        exit_code=1,
        reply="Peer destination is no longer available (unavailable).",
        req_id=REQUEST_ID,
        session_key="peer:x",
        done_seen=False,
        status="failed",
    )
    recorder.refused_before_queue = True
    service, _ = _service(tmp_path, recorder=recorder)
    response = service.handle_submit(_submit_msg(_record()))
    assert response["ok"] is False
    assert response["error"]["code"] == proto.ERR_DESTINATION_UNAVAILABLE
    assert service.journal.read(REQUEST_ID)["state"] == STATE_FAILED


def test_no_worker_for_the_provider_marks_failed(tmp_path):
    recorder = _Recorder()
    recorder.returns_none = True
    service, _ = _service(tmp_path, recorder=recorder)
    response = service.handle_submit(_submit_msg(_record()))
    assert response["ok"] is False
    assert response["error"]["code"] == proto.ERR_UNSUPPORTED_PROVIDER
    assert service.journal.read(REQUEST_ID)["state"] == STATE_FAILED


# --------------------------------------------------------------------------- #
# list / status scoping
# --------------------------------------------------------------------------- #


def test_list_shows_only_the_principals_aliases_with_presence(tmp_path):
    def resolver(provider, work_dir, session):
        if provider == "codex" and work_dir == "/tmp/example-project":
            raise RemoteError(proto.ERR_DESTINATION_UNAVAILABLE, "no live codex session")
        return DESTINATION

    service, _ = _service(tmp_path, resolver=resolver)
    bob = service.handle_list({"id": "op-1", "principal": "bob"})
    assert bob["ok"] is True
    assert bob["remote"] == {
        "projects": [
            {
                "alias": "example-project",
                "providers": [
                    {
                        "provider": "claude",
                        "present": True,
                        "state": "present",
                        "reason": "",
                        "sessions": [],
                    },
                    {
                        "provider": "codex",
                        "present": False,
                        "state": "absent",
                        "reason": "no live codex session",
                        "sessions": [],
                    },
                ],
            }
        ]
    }
    eve = service.handle_list({"id": "op-2", "principal": "eve"})
    assert [project["alias"] for project in eve["remote"]["projects"]] == ["other-project"]
    for principal in ("nobody", "ghost"):
        response = service.handle_list({"id": "op-3", "principal": principal})
        assert response["ok"] is True
        assert response["remote"]["projects"] == []


# --------------------------------------------------------------------------- #
# list detail (M3 `list_sessions`): availability + exact session choices
# --------------------------------------------------------------------------- #


def _list_providers(service: RemoteDeliveryService) -> dict:
    response = service.handle_list({"id": "op-list", "principal": "bob"})
    assert response["ok"] is True
    providers = response["remote"]["projects"][0]["providers"]
    return {entry["provider"]: entry for entry in providers}


def test_list_reports_present_with_the_exact_session_choices(tmp_path):
    service, _ = _service(
        tmp_path, sessions=lambda provider, work_dir: [{"live_id": "live-1", "active": True}]
    )

    claude = _list_providers(service)["claude"]

    assert claude == {
        "provider": "claude",
        "present": True,
        "state": "present",
        "reason": "",
        # The id a sender would pass as --live-id, exactly as the inventory names it.
        "sessions": [{"live_id": "live-1", "active": True}],
    }


def test_list_reports_absent_with_the_resolver_reason(tmp_path):
    def resolver(provider, work_dir, session):
        raise RemoteError(
            proto.ERR_DESTINATION_UNAVAILABLE,
            "no live claude session for the destination project",
            verdict="absent",
        )

    service, _ = _service(tmp_path, resolver=resolver)

    claude = _list_providers(service)["claude"]

    assert claude["present"] is False
    assert claude["state"] == "absent"
    assert claude["reason"] == "no live claude session for the destination project"
    assert claude["sessions"] == []


def test_list_reports_ambiguous_when_two_live_sessions_exist(tmp_path):
    def resolver(provider, work_dir, session):
        raise RemoteError(
            proto.ERR_DESTINATION_UNAVAILABLE,
            "more than one live claude session for the destination project; "
            "select one with dest_session",
            verdict="ambiguous",
        )

    sessions = lambda provider, work_dir: [
        {"live_id": "live-a", "active": True},
        {"live_id": "live-b", "active": True},
    ]
    service, _ = _service(tmp_path, resolver=resolver, sessions=sessions)

    claude = _list_providers(service)["claude"]

    # `present` keeps its old meaning: an ambiguous provider is NOT present.
    assert claude["present"] is False
    assert claude["state"] == "ambiguous"
    assert [session["live_id"] for session in claude["sessions"]] == ["live-a", "live-b"]


def test_list_reports_ambiguity_even_with_no_usable_session_ids(tmp_path):
    """The legacy ambiguity case: the verdict decides, not the id count.

    A launch with no real live ids advertises no session choice at all, yet two
    live sessions still make the provider ambiguous. Counting the surviving ids
    would have said "absent" here.
    """

    def resolver(provider, work_dir, session):
        raise RemoteError(
            proto.ERR_DESTINATION_UNAVAILABLE,
            "more than one live claude session for the destination project; "
            "select one with dest_session",
            verdict="ambiguous",
        )

    service, _ = _service(tmp_path, resolver=resolver, sessions=lambda provider, work_dir: [])

    claude = _list_providers(service)["claude"]

    assert claude["present"] is False
    assert claude["state"] == "ambiguous"
    assert claude["sessions"] == []


def test_a_markerless_refusal_is_never_read_as_ambiguity(tmp_path):
    """No verdict means no structured answer -- never an inferred one."""

    def resolver(provider, work_dir, session):
        raise RemoteError(proto.ERR_DESTINATION_UNAVAILABLE, "destination refused")

    sessions = lambda provider, work_dir: [
        {"live_id": "live-a", "active": True},
        {"live_id": "live-b", "active": True},
    ]
    service, _ = _service(tmp_path, resolver=resolver, sessions=sessions)

    claude = _list_providers(service)["claude"]

    assert claude["state"] == "absent"
    assert claude["reason"] == "destination refused"


def test_list_reports_a_legacy_launch_as_present_without_choices(tmp_path):
    # A legacy launch has no real live ids, so the resolver accepts its one
    # pane while there is no session choice to offer.
    service, _ = _service(tmp_path, sessions=lambda provider, work_dir: [])

    claude = _list_providers(service)["claude"]

    assert claude["present"] is True
    assert claude["state"] == "present"
    assert claude["sessions"] == []


def test_list_reports_an_invalid_inventory_as_unavailable(tmp_path):
    def resolver(provider, work_dir, session):
        raise RemoteError(
            proto.ERR_DESTINATION_UNAVAILABLE,
            "destination live-session inventory is invalid; refusing to guess",
            verdict="invalid_inventory",
        )

    def sessions(provider, work_dir):
        raise RemoteError(
            proto.ERR_DESTINATION_UNAVAILABLE,
            "destination live-session inventory is invalid",
            verdict="invalid_inventory",
        )

    service, _ = _service(tmp_path, resolver=resolver, sessions=sessions)

    claude = _list_providers(service)["claude"]

    assert claude["present"] is False
    assert claude["state"] == "unavailable"
    assert claude["reason"] == "destination live-session inventory is invalid"
    assert claude["sessions"] == []


def test_a_session_probe_crash_is_reported_not_raised(tmp_path):
    def sessions(provider, work_dir):
        raise RuntimeError(f"boom {work_dir}")

    service, _ = _service(tmp_path, sessions=sessions)

    claude = _list_providers(service)["claude"]

    assert claude["state"] == "unavailable"
    # Never a raw exception or a path: the reason is a fixed sentence.
    assert claude["reason"] == "the destination sessions could not be listed"


def test_describe_sessions_offers_only_real_live_ids(monkeypatch):
    import peer_routing
    from types import SimpleNamespace

    legacy = SimpleNamespace(live_id="legacy:ai-1:claude", active=True)
    live = SimpleNamespace(live_id="live-1", active=True)
    calls = []

    def fake_entries(project_id, provider):
        calls.append((project_id, provider))
        return [(None, legacy), (None, live)], True

    monkeypatch.setattr(peer_routing, "_live_entries_for_project", fake_entries)

    assert remote_delivery.describe_sessions("claude", "/tmp/example-project") == [
        {"live_id": "live-1", "active": True}
    ]
    # Same inventory reader the submit path uses, scoped to that one project.
    assert calls and calls[0][1] == "claude"


LONG_PREFIX = "s" * 128
LONG_ID_A = f"{LONG_PREFIX}-a"
LONG_ID_B = f"{LONG_PREFIX}-b"


def _live_session(live_id: str, pane_id: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        live_id=live_id,
        provider="claude",
        active=True,
        pane_id=pane_id,
        terminal="tmux",
        work_dir="/tmp/example-project",
        launch_id="ai-1",
        pane_title_marker=f"CCB-Claude-{pane_id.strip('%')}",
        ccb_project_id="proj-1",
    )


def test_describe_sessions_keeps_ids_that_share_a_long_prefix(monkeypatch):
    import peer_routing

    first = _live_session(LONG_ID_A, "%1")
    second = _live_session(LONG_ID_B, "%2")
    monkeypatch.setattr(
        peer_routing,
        "_live_entries_for_project",
        lambda project_id, provider: ([(None, first), (None, second)], True),
    )

    sessions = remote_delivery.describe_sessions("claude", "/tmp/example-project")

    assert [session["live_id"] for session in sessions] == [LONG_ID_A, LONG_ID_B]


def test_describe_sessions_drops_an_oversized_id(monkeypatch):
    import peer_routing

    oversized = _live_session("x" * (proto.MAX_SESSION_ID_LENGTH + 1), "%3")
    monkeypatch.setattr(
        peer_routing,
        "_live_entries_for_project",
        lambda project_id, provider: ([(None, oversized)], True),
    )

    assert remote_delivery.describe_sessions("claude", "/tmp/example-project") == []


def test_resolve_destination_selects_the_session_named_by_a_long_id(monkeypatch):
    """The id a sender was shown addresses exactly that pane, never a prefix."""
    import peer_routing

    first = _live_session(LONG_ID_A, "%1")
    second = _live_session(LONG_ID_B, "%2")
    monkeypatch.setattr(
        peer_routing,
        "_live_entries_for_project",
        lambda project_id, provider: ([(None, first), (None, second)], True),
    )
    monkeypatch.setattr(remote_delivery, "_pane_pid", lambda terminal, pane_id: "")

    destination = remote_delivery.resolve_destination(
        "claude", "/tmp/example-project", LONG_ID_B
    )

    assert destination.pane_id == "%2"
    assert destination.live_id == LONG_ID_B


def test_describe_sessions_refuses_a_broken_inventory(monkeypatch):
    import peer_routing

    def broken(project_id, provider):
        raise peer_routing.InvalidInventoryError("broken")

    monkeypatch.setattr(peer_routing, "_live_entries_for_project", broken)

    with pytest.raises(RemoteError) as excinfo:
        remote_delivery.describe_sessions("claude", "/tmp/example-project")

    assert "inventory is invalid" in str(excinfo.value)


def test_status_returns_state_without_message_content(tmp_path):
    service, _ = _service(tmp_path)
    service.handle_submit(_submit_msg(_record()))
    response = service.handle_status({"id": "op-1", "principal": "bob", "request_id": REQUEST_ID})
    assert response["ok"] is True
    payload = response["remote"]
    assert payload["state"] == STATE_ACCEPTED
    assert payload["digest"] == _record()["digest"]
    assert "text" not in payload
    assert "source_claims" not in payload
    assert "intent" not in payload


def test_status_for_another_principal_is_identical_to_a_missing_id(tmp_path):
    service, _ = _service(tmp_path)
    service.handle_submit(_submit_msg(_record()))

    def answer(principal: str, request_id: str) -> dict:
        return service.handle_status({"id": "op-1", "principal": principal, "request_id": request_id})

    other_principal = answer("eve", REQUEST_ID)
    missing = answer("bob", f"{LOCAL_ID}-habc1".replace("abc1", "zzz9"))
    unknown_id = answer("bob", f"{LOCAL_ID}-hqqqq")
    assert other_principal == missing == unknown_id
    assert other_principal["error"]["code"] == proto.ERR_NOT_FOUND
    # A malformed id is the same answer again: no existence leak either way.
    assert answer("bob", "not-an-id") == missing


def test_status_survives_a_corrupt_record(tmp_path):
    service, _ = _service(tmp_path)
    service.handle_submit(_submit_msg(_record()))
    service.journal.record_path(REQUEST_ID).write_text("{broken", encoding="utf-8")
    response = service.handle_status({"id": "op-1", "principal": "bob", "request_id": REQUEST_ID})
    assert response["ok"] is False
    assert response["error"]["code"] == proto.ERR_JOURNAL_FAILURE


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #


def _seed(journal: RemoteJournal, request_id: str = REQUEST_ID, **overrides) -> dict:
    record = {
        "request_id": request_id,
        "digest": "a" * 64,
        "principal": "bob",
        "dest_project": "example-project",
        "dest_provider": "claude",
        "dest_session": "",
        "work_dir": "/tmp/example-project",
        "pane_id": "%9",
        "terminal": "tmux",
        "pane_title_marker": "CCB-claude-1",
        "ccb_project_id": "proj-1",
        # The binding captured at acceptance; recovery must reproduce it exactly.
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


def test_recovery_turns_dispatch_in_progress_into_outcome_unknown_without_resend(tmp_path):
    service, recorder = _service(tmp_path)
    _seed(service.journal, state=STATE_DISPATCHING)
    summary = service.recover()
    assert summary["unknown"] == 1
    assert recorder.calls == []
    assert service.journal.read(REQUEST_ID)["state"] == STATE_UNKNOWN


def test_recovery_requeues_only_this_daemons_accepted_pending_records(tmp_path):
    service, recorder = _service(tmp_path)
    _seed(service.journal, REQUEST_ID)
    _seed(service.journal, EVE_REQUEST_ID, owner_daemon=OTHER_OWNER)
    summary = service.recover()
    assert summary["requeued"] == 1
    assert summary["skipped"] == 1
    assert [call[2] for call in recorder.calls] == [REQUEST_ID]
    # Still queued, not falsely marked delivered.
    assert service.journal.read(REQUEST_ID)["state"] == STATE_ACCEPTED
    assert service.journal.read(EVE_REQUEST_ID)["state"] == STATE_ACCEPTED


def test_recovery_never_touches_terminal_states(tmp_path):
    service, recorder = _service(tmp_path)
    _seed(service.journal, REQUEST_ID, state=STATE_DELIVERED)
    _seed(service.journal, EVE_REQUEST_ID, state=STATE_FAILED)
    summary = service.recover()
    assert summary == {"unknown": 0, "requeued": 0, "skipped": 2, "failed": 0}
    assert recorder.calls == []


def test_recovery_marks_a_gone_destination_failed(tmp_path):
    def gone(provider, work_dir, session):
        raise RemoteError(proto.ERR_DESTINATION_UNAVAILABLE, "no live claude session")

    service, recorder = _service(tmp_path, resolver=gone)
    _seed(service.journal, REQUEST_ID)
    summary = service.recover()
    assert summary["failed"] == 1
    assert recorder.calls == []
    assert service.journal.read(REQUEST_ID)["state"] == STATE_FAILED


# --------------------------------------------------------------------------- #
# deliver_remote_task: the honest state machine around send_text
# --------------------------------------------------------------------------- #


class _RecordingBackend:
    def __init__(self, *, fail: bool = False):
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def send_text(self, pane_id: str, text: str) -> None:
        self.sent.append((pane_id, text))
        if self.fail:
            raise OSError("pane vanished mid-paste")


def _delivery_task():
    from askd.adapters.base import PeerDestination, ProviderRequest

    request = ProviderRequest(
        client_id=REQUEST_ID,
        work_dir="/tmp/example-project",
        timeout_s=-1.0,
        quiet=True,
        message="body",
        caller="remote:bob",
        req_id=REQUEST_ID,
        delivery_only=True,
        suppress_completion_hook=True,
        peer_destination=PeerDestination(
            pane_id="%9", terminal="tmux", work_dir="/tmp/example-project"
        ),
        kind="notify",
    )
    task = QueuedTask(request=request, created_ms=0, req_id=REQUEST_ID, done_event=threading.Event())
    task.remote_record_id = REQUEST_ID
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


def _seed_dispatching(journal_root: Path | None = None):
    journal = RemoteJournal(journal_root)
    return journal, _seed(journal, state=STATE_ACCEPTED)


def test_successful_send_records_delivered(tmp_path, monkeypatch):
    journal = RemoteJournal()
    _seed(journal)
    backend = _RecordingBackend()
    _patch_send(monkeypatch, backend)

    result = deliver_remote_task(_delivery_task(), "claude", "peer:key")

    assert result.status == "completed"
    assert len(backend.sent) == 1
    assert f"CCB_REQ_ID: {REQUEST_ID}" in backend.sent[0][1]
    record = journal.read(REQUEST_ID)
    assert record["state"] == STATE_DELIVERED
    assert [entry["state"] for entry in record["history"]][-2:] == [STATE_DISPATCHING, STATE_DELIVERED]


def test_send_exception_after_dispatch_is_outcome_unknown_not_failed(tmp_path, monkeypatch):
    journal = RemoteJournal()
    _seed(journal)
    backend = _RecordingBackend(fail=True)
    _patch_send(monkeypatch, backend)

    result = deliver_remote_task(_delivery_task(), "claude", "peer:key")

    assert result.exit_code == 1
    assert "unknown" in result.reply.lower()
    record = journal.read(REQUEST_ID)
    assert record["state"] == STATE_UNKNOWN
    assert STATE_FAILED not in [entry["state"] for entry in record["history"]]


def test_refusal_before_dispatch_is_failed(tmp_path, monkeypatch):
    journal = RemoteJournal()
    _seed(journal)
    backend = _RecordingBackend()
    _patch_send(monkeypatch, backend, revalidate_ok=False)

    result = deliver_remote_task(_delivery_task(), "claude", "peer:key")

    assert result.status == "failed"
    assert backend.sent == []
    assert journal.read(REQUEST_ID)["state"] == STATE_FAILED


def test_role_refusal_is_failed_and_sends_nothing(tmp_path, monkeypatch):
    journal = RemoteJournal()
    _seed(journal)
    backend = _RecordingBackend()
    _patch_send(monkeypatch, backend, refusal="role policy refused: external senders may only send to lead")

    result = deliver_remote_task(_delivery_task(), "claude", "peer:key")

    assert result.status == "failed"
    assert backend.sent == []
    assert journal.read(REQUEST_ID)["state"] == STATE_FAILED


def test_lock_timeout_is_failed_and_sends_nothing(tmp_path, monkeypatch):
    journal = RemoteJournal()
    _seed(journal)
    backend = _RecordingBackend()
    _patch_send(monkeypatch, backend)
    monkeypatch.setenv("CCB_PANE_LOCK_TIMEOUT_S", "0.1")

    import pane_lock

    holder_key = pane_lock.lock_key(backend, "%9")
    release = threading.Event()
    started = threading.Event()

    def hold() -> None:
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
    assert holder_key == pane_lock.lock_key(backend, "%9")


def test_dispatch_commit_failure_is_failed_not_unknown(tmp_path, monkeypatch):
    journal = RemoteJournal()
    _seed(journal)
    backend = _RecordingBackend()
    _patch_send(monkeypatch, backend)

    def boom(*_args, **_kwargs):
        raise JournalError("disk full")

    monkeypatch.setattr(RemoteJournal, "transition_if", boom)
    result = deliver_remote_task(_delivery_task(), "claude", "peer:key")

    assert result.status == "failed"
    assert backend.sent == []
