"""Phase 3 role delivery gate: matrix, discovery, kinds, acknowledgement.

The launch record and claims fixtures are a real record copied from
`~/.ccb/run` of the launch this work was done in, with every owner
path, project/session id and pane title replaced by neutral values
(claude=lead %8, pi=implementer %13, codex=ratifier %14). Structure,
field names and provider/pane topology are unchanged; credential values
are deterministic test tokens, and each test relocates the record to its
own project/pid so discovery exercises real files.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import delivery_policy as dp
from askd.adapters.base import PeerDestination, ProviderRequest, QueuedTask, ResolvedRoute
from project_id import compute_ccb_project_id

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "role_gate"

CLAUDE_MEMBER = "11111111111111111111111111111111"  # lead
PI_MEMBER = "22222222222222222222222222222222"      # implementer
CODEX_MEMBER = "33333333333333333333333333333333"   # ratifier
MEMBER_BY_PROVIDER = {"claude": CLAUDE_MEMBER, "pi": PI_MEMBER, "codex": CODEX_MEMBER}
ROLE_BY_MEMBER = {CLAUDE_MEMBER: "lead", PI_MEMBER: "implementer", CODEX_MEMBER: "ratifier"}


def _run_dir() -> Path:
    return Path.home() / ".ccb" / "run"


def _install_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    role_policy: str | None = "enforce",
    probe_roles: bool = True,
) -> dict:
    record = json.loads((FIXTURES / "launch_record.json").read_text(encoding="utf-8"))
    claims_doc = json.loads((FIXTURES / "launch_claims.json").read_text(encoding="utf-8"))
    record["work_dir"] = str(tmp_path)
    record["ccb_project_id"] = compute_ccb_project_id(tmp_path)
    record["ccb_pid"] = os.getpid()
    record["updated_at"] = int(time.time())
    if role_policy is None:
        record.pop("role_policy", None)
    else:
        record["role_policy"] = role_policy
    run_dir = _run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    launch_id = record["ccb_session_id"]
    (run_dir / f"ccb-session-{launch_id}.json").write_text(json.dumps(record), encoding="utf-8")
    (run_dir / f"ccb-roles-{launch_id}.json").write_text(json.dumps(claims_doc), encoding="utf-8")
    if probe_roles:
        # Pane liveness cannot be true inside a test; the claims themselves
        # are the real fixture. Only the liveness probe is stubbed.
        monkeypatch.setattr(
            dp, "_current_roles", lambda _record: dict(ROLE_BY_MEMBER)
        )
    return record


def _claims_path(record: dict) -> Path:
    return _run_dir() / f"ccb-roles-{record['ccb_session_id']}.json"


def _route(record: dict, provider: str) -> ResolvedRoute:
    entry = record["providers"][provider]
    return ResolvedRoute(
        live_id=MEMBER_BY_PROVIDER[provider],
        launch_id=record["ccb_session_id"],
        pane_id=entry["pane_id"],
        terminal=record["terminal"],
        session_file=entry["session_file"],
        ccb_project_id=record["ccb_project_id"],
    )


def _request(
    record: dict,
    *,
    provider: str,
    sender: str,
    kind: str = "request",
    to_role: str | None = None,
    delivery_only: bool = False,
    peer: PeerDestination | None = None,
    reply_to: str = "",
    route: ResolvedRoute | None = None,
    sender_member_id: str | None = None,
    sender_member_token: str | None = None,
) -> ProviderRequest:
    if to_role is None:
        to_role = ROLE_BY_MEMBER[MEMBER_BY_PROVIDER[provider]]
    if sender_member_id is None:
        sender_member_id = MEMBER_BY_PROVIDER.get(sender, "")
    if sender_member_token is None:
        sender_member_token = f"test-token-{sender}" if sender in MEMBER_BY_PROVIDER else ""
    return ProviderRequest(
        client_id="test",
        work_dir=record["work_dir"],
        timeout_s=1.0,
        quiet=True,
        message="hello",
        caller=sender or "manual",
        delivery_only=delivery_only,
        route=route if route is not None else _route(record, provider),
        peer_destination=peer or PeerDestination(),
        kind=kind,
        to_role=to_role,
        sender_member_id=sender_member_id,
        sender_member_token=sender_member_token,
        reply_to=reply_to,
    )


# --- matrix -------------------------------------------------------------


@pytest.mark.parametrize(
    "sender,destination,allowed",
    [
        ("claude", "pi", True),      # lead -> implementer
        ("claude", "codex", True),   # lead -> ratifier
        ("pi", "claude", True),      # implementer -> lead
        ("codex", "claude", True),   # ratifier -> lead
        ("pi", "codex", False),      # implementer -/-> ratifier
        ("codex", "pi", False),      # ratifier -/-> implementer
    ],
)
def test_matrix(tmp_path, monkeypatch, sender, destination, allowed):
    record = _install_launch(tmp_path, monkeypatch)
    decision = dp.evaluate_request(_request(record, provider=destination, sender=sender), destination)
    assert decision.allowed is allowed, decision.reason


def test_refusal_names_roles_and_allowed_targets(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    decision = dp.evaluate_request(_request(record, provider="codex", sender="pi"), "codex")
    assert not decision.allowed
    assert "implementer" in decision.reason
    assert "ratifier" in decision.reason
    assert "allowed targets: lead" in decision.reason


def test_unassigned_sender_may_not_send(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    # The destination still holds its role; only the sender has none.
    monkeypatch.setattr(dp, "_current_roles", lambda _record: {CODEX_MEMBER: "ratifier"})
    decision = dp.evaluate_request(_request(record, provider="codex", sender="claude"), "codex")
    assert not decision.allowed
    assert "assign a role first" in decision.reason


def test_unassigned_destination_may_not_receive(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_current_roles", lambda _record: {PI_MEMBER: "implementer"})
    decision = dp.evaluate_request(
        _request(record, provider="codex", sender="pi", to_role=""), "codex"
    )
    assert not decision.allowed
    assert "assign a role first" in decision.reason


# --- sender identity ----------------------------------------------------


@pytest.mark.parametrize(
    "member_id,token",
    [
        ("", ""),
        (CODEX_MEMBER, "not-the-token"),
        (CODEX_MEMBER, "test-token-claude"),
        ("some-member-from-another-launch", "test-token-codex"),
    ],
)
def test_missing_forged_or_foreign_credential_is_external(tmp_path, monkeypatch, member_id, token):
    record = _install_launch(tmp_path, monkeypatch)
    external_to_lead = _request(
        record, provider="claude", sender="codex",
        sender_member_id=member_id, sender_member_token=token,
    )
    assert dp.evaluate_request(external_to_lead, "claude").allowed
    external_to_implementer = _request(
        record, provider="pi", sender="codex",
        sender_member_id=member_id, sender_member_token=token,
    )
    decision = dp.evaluate_request(external_to_implementer, "pi")
    assert not decision.allowed
    assert "external senders may only send to a session holding the lead role" in decision.reason


def test_external_destination_must_hold_lead(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    assert dp.evaluate_request(_request(record, provider="claude", sender="manual"), "claude").allowed
    for provider in ("pi", "codex"):
        decision = dp.evaluate_request(_request(record, provider=provider, sender="manual"), provider)
        assert not decision.allowed


# --- --to-role assertion ------------------------------------------------


def test_to_role_verified_with_policy_off(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch, role_policy="off")
    ok = dp.evaluate_request(
        _request(record, provider="pi", sender="claude", to_role="implementer"), "pi"
    )
    assert ok.allowed
    mismatch = dp.evaluate_request(
        _request(record, provider="pi", sender="claude", to_role="ratifier"), "pi"
    )
    assert not mismatch.allowed
    assert mismatch.reason == "target role mismatch: pi is implementer"


def test_to_role_mismatch_under_enforce(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    decision = dp.evaluate_request(
        _request(record, provider="pi", sender="claude", to_role="ratifier"), "pi"
    )
    assert decision.reason == "target role mismatch: pi is implementer"


def test_enforce_requires_to_role_from_member_senders(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    decision = dp.evaluate_request(
        _request(record, provider="pi", sender="claude", to_role=""), "pi"
    )
    assert not decision.allowed
    assert "--to-role" in decision.reason


def test_enforce_does_not_require_to_role_from_external_senders(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    decision = dp.evaluate_request(
        _request(record, provider="claude", sender="manual", to_role=""), "claude"
    )
    assert decision.allowed


def test_policy_off_is_unchanged_without_to_role(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch, role_policy="off")
    monkeypatch.setattr(dp, "_current_roles", lambda _record: {})
    decision = dp.evaluate_request(
        _request(record, provider="codex", sender="pi", kind="", to_role=""), "codex"
    )
    assert decision.allowed


def test_absent_policy_field_means_off(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch, role_policy=None)
    decision = dp.evaluate_request(_request(record, provider="codex", sender="pi", kind=""), "codex")
    assert decision.allowed


# --- kinds --------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,delivery_only,has_peer,consistent",
    [
        ("", False, False, True),
        ("request", False, False, True),
        ("request", True, True, True),
        ("request", True, False, False),
        ("notify", True, False, True),
        ("notify", False, False, False),
        ("reply", True, True, True),
        ("reply", True, False, False),
        ("reply", False, True, False),
        ("bogus", False, False, False),
    ],
)
def test_kind_transport_consistency(kind, delivery_only, has_peer, consistent):
    assert dp.kind_is_consistent(kind, delivery_only=delivery_only, has_peer=has_peer) is consistent


def test_inconsistent_kind_is_refused_before_the_matrix(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    destination = dp.destination_from_launch("pi", record["ccb_session_id"], PI_MEMBER)
    sender = dp.identify_sender(destination, CLAUDE_MEMBER, "test-token-claude")
    decision = dp.evaluate(
        destination=destination, sender=sender, kind="notify", delivery_only=False
    )
    assert not decision.allowed
    assert "inconsistent request kind" in decision.reason


def test_enforce_requires_an_explicit_kind(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    decision = dp.evaluate_request(_request(record, provider="pi", sender="claude", kind=""), "pi")
    assert not decision.allowed
    assert "explicit request kind" in decision.reason


# --- replies (E2) -------------------------------------------------------


def _write_peer_receipt(task_id: str, *, caller: str, pane_id: str, project_id: str, work_dir: str) -> None:
    from task_receipts import new_peer_receipt, receipt_path, write_receipt

    root = Path(str(dp.__file__)).parent.parent / "tmp-unused"  # unused, keeps import order clear
    del root
    receipt = new_peer_receipt(
        task_id=task_id,
        peer_provider="codex",
        caller=caller,
        intent="wait",
        work_dir=Path(work_dir),
        status_file=Path(work_dir) / "status",
        log_file=Path(work_dir) / "log",
        reply_file=Path(work_dir) / "reply",
        caller_pane_id=pane_id,
        caller_terminal="tmux",
        caller_pane_title_marker="CCB-Codex-marker",
    )
    receipt["ccb_project_id"] = project_id
    write_receipt(receipt_path(f"peer-codex", task_id), receipt)


def test_verified_reply_reaches_original_requester_whatever_its_role(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    # The original requester is pi (implementer): a peer reply to it is
    # exempt from the external->lead-only rule when it is correlated.
    _write_peer_receipt(
        "20261105-120000-000-1",
        caller="pi",
        pane_id=record["providers"]["pi"]["pane_id"],
        project_id=record["ccb_project_id"],
        work_dir=record["work_dir"],
    )
    peer = PeerDestination(
        pane_id=record["providers"]["pi"]["pane_id"],
        terminal=record["terminal"],
        work_dir=record["work_dir"],
        ccb_project_id=record["ccb_project_id"],
        pane_title_marker=record["providers"]["pi"]["pane_title_marker"],
    )
    request = _request(
        record, provider="pi", sender="codex", kind="reply",
        delivery_only=True, peer=peer, reply_to="20261105-120000-000-1",
        route=ResolvedRoute(),
    )
    assert dp.evaluate_request(request, "pi").allowed


def test_correlated_reply_to_a_different_pane_is_refused(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    _write_peer_receipt(
        "20261105-120001-000-1",
        caller="pi",
        pane_id=record["providers"]["claude"]["pane_id"],
        project_id=record["ccb_project_id"],
        work_dir=record["work_dir"],
    )
    peer = PeerDestination(
        pane_id=record["providers"]["pi"]["pane_id"],
        terminal=record["terminal"],
        work_dir=record["work_dir"],
        ccb_project_id=record["ccb_project_id"],
    )
    request = _request(
        record, provider="pi", sender="codex", kind="reply",
        delivery_only=True, peer=peer, reply_to="20261105-120001-000-1",
        route=ResolvedRoute(),
    )
    decision = dp.evaluate_request(request, "pi")
    assert not decision.allowed
    assert "original requester pane" in decision.reason


def test_reply_without_a_receipt_is_refused(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    peer = PeerDestination(
        pane_id=record["providers"]["pi"]["pane_id"],
        terminal=record["terminal"],
        work_dir=record["work_dir"],
        ccb_project_id=record["ccb_project_id"],
    )
    request = _request(
        record, provider="pi", sender="codex", kind="reply",
        delivery_only=True, peer=peer, reply_to="20261105-120002-000-1",
        route=ResolvedRoute(),
    )
    decision = dp.evaluate_request(request, "pi")
    assert not decision.allowed
    assert "no receipt" in decision.reason


# --- discovery (E1) -----------------------------------------------------


def test_unknown_launch_is_unmanaged(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "_current_roles", lambda _record: dict(ROLE_BY_MEMBER))
    destination = dp.destination_from_launch("pi", "ai-not-a-launch", PI_MEMBER)
    assert destination.mode == dp.MODE_UNMANAGED


def test_malformed_record_refuses(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    path = _run_dir() / f"ccb-session-{record['ccb_session_id']}.json"
    path.write_text("{not json", encoding="utf-8")
    destination = dp.destination_from_launch("pi", record["ccb_session_id"], PI_MEMBER)
    assert destination.mode == dp.MODE_ERROR
    decision = dp.evaluate_request(
        _request(record, provider="pi", sender="claude"), "pi", destination=destination
    )
    assert not decision.allowed
    assert "cannot be determined" in decision.reason


def test_unknown_policy_value_refuses(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch, role_policy="sometimes")
    destination = dp.destination_from_launch("pi", record["ccb_session_id"], PI_MEMBER)
    assert destination.mode == dp.MODE_ERROR


def test_ambiguous_project_ownership_refuses(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    clone = json.loads((_run_dir() / f"ccb-session-{record['ccb_session_id']}.json").read_text())
    clone["ccb_session_id"] = "ai-1-clone"
    clone["ccb_pid"] = os.getpid()
    clone["updated_at"] = int(time.time())
    (_run_dir() / "ccb-session-ai-1-clone.json").write_text(json.dumps(clone), encoding="utf-8")
    destination = dp.destination_from_work_dir("pi", tmp_path)
    assert destination.mode == dp.MODE_ERROR
    assert "more than one running launch" in destination.reason


def _clone_launch(record: dict, launch_id: str, role_policy: str | None) -> None:
    clone = json.loads(
        (_run_dir() / f"ccb-session-{record['ccb_session_id']}.json").read_text(encoding="utf-8")
    )
    clone["ccb_session_id"] = launch_id
    clone["ccb_pid"] = os.getpid()
    clone["updated_at"] = int(time.time())
    if role_policy is None:
        clone.pop("role_policy", None)
    else:
        clone["role_policy"] = role_policy
    (_run_dir() / f"ccb-session-{launch_id}.json").write_text(json.dumps(clone), encoding="utf-8")


def test_two_off_launches_share_a_project_without_breaking_asks(tmp_path, monkeypatch):
    """Two non-enforcing launches of one project are legitimate today."""
    record = _install_launch(tmp_path, monkeypatch, role_policy="off")
    _clone_launch(record, "ai-2-off", "off")

    destination = dp.destination_from_work_dir("pi", tmp_path)
    assert destination.mode == dp.MODE_OFF
    assert destination.ambiguous is True
    assert destination.member_id == ""

    # These requests carry no route: the ambiguity is a work-dir lookup.
    unrouted = dict(route=ResolvedRoute())
    # A plain ask keeps working exactly as it did before the gate existed.
    assert dp.evaluate_request(
        _request(record, provider="pi", sender="claude", to_role="", **unrouted), "pi"
    ).allowed
    # An explicit assertion cannot be verified, so it is refused rather than ignored.
    asserted = dp.evaluate_request(
        _request(record, provider="pi", sender="claude", to_role="implementer", **unrouted), "pi"
    )
    assert not asserted.allowed
    assert asserted.reason == (
        "cannot verify target role: more than one launch owns this destination"
    )

    # Negative control: the same shape with ONE enforcing owner must refuse,
    # so the allowed case above is not a vacuous assertion.
    _clone_launch(record, "ai-2-off", "enforce")
    refused = dp.evaluate_request(
        _request(record, provider="pi", sender="claude", to_role="", **unrouted), "pi"
    )
    assert not refused.allowed
    assert "more than one running launch" in refused.reason


def test_stray_corrupt_registry_file_does_not_refuse_an_unmanaged_destination(tmp_path, monkeypatch):
    run_dir = _run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "ccb-session-ai-corrupt.json").write_text("{not json", encoding="utf-8")

    destination = dp.destination_from_work_dir("pi", tmp_path)
    assert destination.mode == dp.MODE_UNMANAGED
    request = _request(
        record={"work_dir": str(tmp_path), "ccb_project_id": "", "providers": {}, "terminal": "tmux",
                "ccb_session_id": ""},
        provider="pi",
        sender="manual",
        route=ResolvedRoute(),
        to_role="",
    )
    assert dp.evaluate_request(request, "pi", destination=destination).allowed


def test_corrupt_record_for_a_known_launch_still_refuses(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    path = _run_dir() / f"ccb-session-{record['ccb_session_id']}.json"
    path.write_text("{not json", encoding="utf-8")

    destination = dp.destination_from_launch("pi", record["ccb_session_id"], PI_MEMBER)
    assert destination.mode == dp.MODE_ERROR
    decision = dp.evaluate_request(
        _request(record, provider="pi", sender="claude"), "pi", destination=destination
    )
    assert not decision.allowed


def test_unmanaged_destination_refuses_an_explicit_target_role(tmp_path, monkeypatch):
    destination = dp.destination_from_work_dir("pi", tmp_path)
    assert destination.mode == dp.MODE_UNMANAGED
    request = _request(
        record={"work_dir": str(tmp_path), "ccb_project_id": "", "providers": {}, "terminal": "tmux",
                "ccb_session_id": ""},
        provider="pi",
        sender="claude",
        route=ResolvedRoute(),
        to_role="implementer",
    )
    decision = dp.evaluate_request(request, "pi", destination=destination)
    assert not decision.allowed
    assert decision.reason == "cannot verify target role: the destination is not in a CCB launch"


def test_corrupt_claims_refuse(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch, probe_roles=False)
    _claims_path(record).write_text("{broken", encoding="utf-8")
    destination = dp.destination_from_launch("pi", record["ccb_session_id"], PI_MEMBER)
    # Real Phase 2 reader over a broken claims file yields no roles, so the
    # enforcing destination refuses instead of treating it as unassigned-but-ok.
    decision = dp.evaluate_request(_request(record, provider="pi", sender="claude"), "pi")
    assert destination.mode == dp.MODE_ENFORCE
    assert not decision.allowed


# --- recheck (E5) -------------------------------------------------------


def test_recheck_refuses_after_a_role_change(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    request = _request(record, provider="pi", sender="claude")
    assert dp.evaluate_request(request, "pi").allowed
    # The sender's role changed while the task sat queued: a ratifier may
    # not address the implementer, so the queued task must be refused.
    monkeypatch.setattr(
        dp, "_current_roles", lambda _record: {PI_MEMBER: "implementer", CLAUDE_MEMBER: "ratifier"}
    )
    decision = dp.evaluate_request(request, "pi", expected_pane=record["providers"]["pi"]["pane_id"])
    assert not decision.allowed
    assert "refused" in decision.reason


def test_recheck_detects_a_replaced_pane(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    request = _request(record, provider="pi", sender="claude")
    decision = dp.evaluate_request(request, "pi", expected_pane="%999")
    assert not decision.allowed
    assert "destination pane changed" in decision.reason


# --- acknowledgement (A7) ----------------------------------------------


class _FakeRegistry:
    def get(self, _provider):
        return object()


class _FakeTask:
    def __init__(self, req_id: str, result=None):
        self.req_id = req_id
        self.result = result
        self.done_event = __import__("threading").Event()
        self.cancelled = False
        self.cancel_event = None


class _RecordingPool:
    def __init__(self, task):
        self.task = task
        self.submitted = []

    def submit(self, provider, request):
        self.submitted.append((provider, request))
        return self.task


def _daemon_with(pool):
    from askd.daemon import UnifiedAskDaemon

    daemon = UnifiedAskDaemon.__new__(UnifiedAskDaemon)
    daemon.registry = _FakeRegistry()
    daemon.pool = pool
    daemon.token = "token"
    daemon.host = "127.0.0.1"
    daemon.port = 0
    daemon.work_dir = None
    return daemon


def _daemon_msg(record: dict, *, provider: str, sender: str, **overrides) -> dict:
    entry = record["providers"][provider]
    msg = {
        "type": "ask.request",
        "v": 1,
        "id": "client-1",
        "token": "token",
        "provider": provider,
        "work_dir": record["work_dir"],
        "timeout_s": 1.0,
        "message": "hello",
        "caller": sender,
        "route": {
            "live_id": MEMBER_BY_PROVIDER[provider],
            "launch_id": record["ccb_session_id"],
            "pane_id": entry["pane_id"],
            "terminal": record["terminal"],
            "session_file": entry["session_file"],
            "ccb_project_id": record["ccb_project_id"],
        },
        "kind": "request",
        "to_role": ROLE_BY_MEMBER[MEMBER_BY_PROVIDER[provider]],
        "sender_member_id": MEMBER_BY_PROVIDER.get(sender, ""),
        "sender_member_token": f"test-token-{sender}" if sender in MEMBER_BY_PROVIDER else "",
    }
    msg.update(overrides)
    return msg


def test_accept_refusal_never_enqueues(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    pool = _RecordingPool(_FakeTask("20261105-120003-000-1"))
    daemon = _daemon_with(pool)

    response = daemon._handle_request(_daemon_msg(record, provider="codex", sender="pi"))

    assert response["exit_code"] == 1
    assert "role policy refused" in response["reply"]
    assert pool.submitted == []  # E5: zero enqueue calls for an accept refusal


def test_async_submit_reports_a_preset_refusal(tmp_path, monkeypatch):
    from askd.adapters.base import ProviderResult

    record = _install_launch(tmp_path, monkeypatch)
    preset = ProviderResult(
        exit_code=1,
        reply="Routed destination is no longer available (gone).",
        req_id="20261105-120004-000-1",
        session_key="pi:launch:x:live:y",
        done_seen=False,
        status="failed",
    )
    pool = _RecordingPool(_FakeTask(preset.req_id, preset))
    daemon = _daemon_with(pool)

    response = daemon._handle_request(
        _daemon_msg(record, provider="pi", sender="claude", async_submit=True)
    )

    assert response["accepted"] is False
    assert response["exit_code"] == 1
    assert "no longer available" in response["reply"]


def test_check_delivery_op_is_read_only(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    pool = _RecordingPool(_FakeTask("x"))
    daemon = _daemon_with(pool)
    message = _daemon_msg(record, provider="codex", sender="pi")
    message["operation"] = "check_delivery"
    message["target_provider"] = message.pop("provider")

    refused = daemon._handle_request(message)
    allowed_message = _daemon_msg(record, provider="codex", sender="claude")
    allowed_message["operation"] = "check_delivery"
    allowed_message["target_provider"] = allowed_message.pop("provider")
    allowed = daemon._handle_request(allowed_message)

    assert refused["allowed"] is False and "role policy refused" in refused["reason"]
    assert allowed["allowed"] is True
    assert pool.submitted == []


# --- adapter recheck: zero send_text (E5) -------------------------------


class _FakeBackend:
    def __init__(self):
        self.sent = []

    def is_alive(self, _pane):
        return True

    def find_pane_by_title_marker(self, _marker, _cwd):
        return "%13"

    def get_pane_content(self, _pane, lines=40):
        return (Path(__file__).parent / "fixtures" / "pi" / "screen-idle.txt").read_text(
            encoding="utf-8"
        )

    def send_text(self, pane, text):
        self.sent.append((pane, text))


def test_pi_adapter_recheck_refuses_with_zero_sends(tmp_path, monkeypatch):
    from askd.adapters.pi import PiAdapter

    record = _install_launch(tmp_path, monkeypatch)
    binding = {
        "active": True,
        "work_dir": str(tmp_path),
        "start_dir": str(tmp_path),
        "session_id": record["ccb_session_id"],
        "pi_session_id": "pi-session-1",
        "pi_session_dir": str(tmp_path / "sessions"),
        "pane_id": record["providers"]["pi"]["pane_id"],
        "pane_title_marker": record["providers"]["pi"]["pane_title_marker"],
    }
    (tmp_path / "sessions").mkdir()
    backend = _FakeBackend()
    monkeypatch.setattr("askd.adapters.pi._load_destination", lambda _req: (tmp_path / ".pi-session", binding, ""))
    monkeypatch.setattr("askd.adapters.pi._check_pane", lambda *_args: (backend, ""))
    adapter = PiAdapter()
    monkeypatch.setattr(adapter, "_finish", lambda _task, result, **_kwargs: result)

    request = ProviderRequest(
        client_id="test",
        work_dir=str(tmp_path),
        timeout_s=1.0,
        quiet=True,
        message="hello",
        caller="pi",
        kind="request",
        sender_member_id=CODEX_MEMBER,
        sender_member_token="test-token-codex",
    )
    task = QueuedTask(
        request=request, created_ms=0, req_id="20261105-120006-000-1",
        done_event=__import__("threading").Event(),
    )
    result = adapter.handle_task(task)

    assert result.exit_code == 1
    assert "role policy refused" in result.reply
    assert backend.sent == []


# --- bridge direct reply (A6.iv / E2) ----------------------------------


def _load_bridge_module():
    path = ROOT / "bin" / "ccb-bridge-ask"
    loader = importlib.machinery.SourceFileLoader("ccb_bridge_ask_role_gate", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_bridge_direct_reply_allowed_only_when_correlated(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    bridge = _load_bridge_module()
    pane = record["providers"]["pi"]["pane_id"]

    uncorrelated = bridge._direct_reply_policy_refusal(
        provider="pi",
        pane_id=pane,
        work_dir=record["work_dir"],
        ccb_project_id=record["ccb_project_id"],
        reply_receipt=None,
        to_role="",
    )
    assert "external senders may only send to a session holding the lead role" in uncorrelated

    _write_peer_receipt(
        "20261105-120007-000-1",
        caller="pi",
        pane_id=pane,
        project_id=record["ccb_project_id"],
        work_dir=record["work_dir"],
    )
    receipt = {"task_id": "20261105-120007-000-1"}
    correlated = bridge._direct_reply_policy_refusal(
        provider="pi",
        pane_id=pane,
        work_dir=record["work_dir"],
        ccb_project_id=record["ccb_project_id"],
        reply_receipt=receipt,
        to_role="",
    )
    assert correlated == ""


# --- legacy fallback and email ingress ---------------------------------


def test_legacy_fallback_refused_under_enforce(tmp_path):
    record = json.loads((FIXTURES / "launch_record.json").read_text(encoding="utf-8"))
    home = tmp_path / "home"
    run_dir = home / ".ccb" / "run"
    run_dir.mkdir(parents=True)
    record["work_dir"] = str(tmp_path)
    record["ccb_project_id"] = compute_ccb_project_id(tmp_path)
    record["ccb_pid"] = os.getpid()
    record["updated_at"] = int(time.time())
    record["role_policy"] = "enforce"
    (run_dir / f"ccb-session-{record['ccb_session_id']}.json").write_text(json.dumps(record))
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "CCB_CALLER": "claude",
            "CCB_UNIFIED_ASKD": "0",
            "CCB_MEMBER_ID": CLAUDE_MEMBER,
            "CCB_MEMBER_TOKEN": "test-token-claude",
            "CCB_RUN_DIR": str(tmp_path / "run"),
        }
    )
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "ask"), "codex", "hello"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "role policy requires the unified daemon" in result.stderr


def test_email_ingress_is_always_external(tmp_path, monkeypatch):
    from mail import ask_handler as ask_handler_module

    captured: dict = {}

    class _Config:
        default_provider = "claude"
        default_work_dir = str(tmp_path)
        provider_prefix_pattern = r"^(\w+)"
        case_insensitive = True

    class _Msg:
        body = "claude hello"
        message_id = "<1@example.com>"
        from_addr = "user@example.com"
        subject = "[CCB] test"
        thread_id = "t1"
        references = []

    monkeypatch.setenv("CCB_MEMBER_ID", "leaked-id")
    monkeypatch.setenv("CCB_MEMBER_TOKEN", "leaked-token")
    monkeypatch.setenv("CCB_ROUTE_LAUNCH_ID", "leaked-launch")

    def fake_run(*_args, **kwargs):
        captured["env"] = kwargs.get("env") or {}

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(ask_handler_module.subprocess, "run", fake_run)
    handler = ask_handler_module.AskHandler(_Config())
    result = handler.handle_email(_Msg())

    assert result.success
    env = captured["env"]
    for leaked in ("CCB_MEMBER_ID", "CCB_MEMBER_TOKEN", "CCB_ROUTE_LAUNCH_ID"):
        assert leaked not in env
    assert env["CCB_CALLER"] == "email"


# --- async preflight (A6.iii / E4) -------------------------------------


def _load_ask_module():
    path = ROOT / "bin" / "ask"
    loader = importlib.machinery.SourceFileLoader("ask_role_gate", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_async_preflight_refusal_never_announces_a_submission(tmp_path, monkeypatch, capsys):
    ask = _load_ask_module()
    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, tmp_path)
    monkeypatch.delenv("CCB_UNIFIED_ASKD", raising=False)
    monkeypatch.setenv("CCB_CALLER", "pi")
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: True)
    monkeypatch.setattr(ask, "_resolve_unified_daemon_context", lambda: context)
    monkeypatch.setattr(ask, "_preflight_target", lambda _provider, **_kwargs: True)
    monkeypatch.setattr(ask, "_queued_notice", lambda *_a, **_k: "")
    monkeypatch.setattr(ask.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        ask,
        "_preflight_delivery",
        lambda *_a, **_k: (False, "role policy refused: implementer may not send to ratifier"),
    )
    spawned: list = []
    monkeypatch.setattr(ask.subprocess, "Popen", lambda *a, **k: spawned.append(a) or None)

    rc = ask.main(["ask", "codex", "--background", "hello"])

    out = capsys.readouterr()
    assert rc == ask.EXIT_ERROR
    assert "role policy refused" in out.err
    assert "CCB_ASYNC_SUBMITTED" not in out.out
    assert "CCB_BACKGROUND_SUBMITTED" not in out.out
    assert spawned == []


# --- opt-in storage (A1) ------------------------------------------------


def _load_launcher_module():
    path = ROOT / "ccb"
    loader = importlib.machinery.SourceFileLoader("ccb_role_gate", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_launcher_records_the_opt_in_before_panes_exist(tmp_path, monkeypatch):
    launcher_mod = _load_launcher_module()
    project = tmp_path / "project"
    (project / ".ccb").mkdir(parents=True)
    monkeypatch.chdir(project)
    launcher = launcher_mod.AILauncher(providers=["claude"], role_policy="enforce")
    launcher.terminal_type = "tmux"

    assert launcher._publish_role_policy() is True

    record = json.loads(
        (_run_dir() / f"ccb-session-{launcher.session_id}.json").read_text(encoding="utf-8")
    )
    assert record["role_policy"] == "enforce"
    assert record["ccb_project_id"] == launcher.project_id
    destination = dp.destination_from_launch("claude", launcher.session_id)
    assert destination.mode == dp.MODE_ENFORCE


def test_launcher_without_the_flag_publishes_no_policy(tmp_path, monkeypatch):
    launcher_mod = _load_launcher_module()
    project = tmp_path / "project"
    (project / ".ccb").mkdir(parents=True)
    monkeypatch.chdir(project)
    # No flag and no config key: the launch is not enforcing, and an
    # explicit "off" is still off.
    launcher = launcher_mod.AILauncher(providers=["claude"])
    launcher.terminal_type = "tmux"
    assert launcher._publish_role_policy() is True
    assert dp.destination_from_launch("claude", launcher.session_id).mode == dp.MODE_UNMANAGED


# --- G1: destination identity comes from the bound session, not a hash ---


class _BoundSession:
    """What an adapter's `load_session` returns: a `.data`-carrying object."""

    def __init__(self, data: dict):
        self.data = dict(data)


def _binding(record: dict, provider: str, *, work_dir: str) -> dict:
    entry = record["providers"][provider]
    return {
        "active": True,
        "work_dir": work_dir,
        "session_id": record["ccb_session_id"],
        "ccb_project_id": record["ccb_project_id"],
        "pane_id": entry["pane_id"],
        "pane_title_marker": entry["pane_title_marker"],
        "session_file": entry["session_file"],
    }


def test_subdirectory_request_is_judged_by_the_session_it_binds_to(tmp_path, monkeypatch):
    """The parent's session is the destination, so the parent's policy applies."""
    record = _install_launch(tmp_path, monkeypatch)
    subdir = tmp_path / "nested" / "deeper"
    subdir.mkdir(parents=True)
    binding = _binding(record, "codex", work_dir=str(tmp_path))

    # A subdirectory hash alone is a different project: the old discovery
    # called it unmanaged. The bound session says otherwise.
    assert dp.destination_from_work_dir("codex", subdir).mode == dp.MODE_UNMANAGED
    destination = dp.destination_for_session("codex", _BoundSession(binding))
    assert destination.mode == dp.MODE_ENFORCE
    assert destination.role == "ratifier"

    # implementer -> ratifier must be refused, which the old code allowed.
    request = _request(
        record, provider="codex", sender="pi", route=ResolvedRoute()
    )
    decision = dp.evaluate_request(
        request, "codex", session=_BoundSession(binding)
    )
    assert not decision.allowed
    assert "allowed targets: lead" in decision.reason


def test_stale_enforcing_record_refuses_through_the_binding(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    path = _run_dir() / f"ccb-session-{record['ccb_session_id']}.json"
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["updated_at"] = 1  # far outside the registry TTL
    path.write_text(json.dumps(stale), encoding="utf-8")

    destination = dp.destination_for_session(
        "codex", _BoundSession(_binding(record, "codex", work_dir=str(tmp_path)))
    )
    assert destination.mode == dp.MODE_ERROR
    assert "stale" in destination.reason


def test_corrupt_record_named_by_the_binding_refuses(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    (_run_dir() / f"ccb-session-{record['ccb_session_id']}.json").write_text(
        "{not json", encoding="utf-8"
    )
    destination = dp.destination_for_session(
        "codex", _BoundSession(_binding(record, "codex", work_dir=str(tmp_path)))
    )
    assert destination.mode == dp.MODE_ERROR


def test_unbound_destination_with_an_unrelated_corrupt_file_is_allowed(tmp_path, monkeypatch):
    run_dir = _run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "ccb-session-ai-1700000001-999.json").write_text("{broken", encoding="utf-8")
    binding = {
        "active": True,
        "work_dir": str(tmp_path),
        "session_id": "provider-session-not-a-launch",
        "pane_id": "%99",
        "pane_title_marker": "CCB-Codex-deadbeef",
    }
    destination = dp.destination_for_session("codex", _BoundSession(binding))
    assert destination.mode == dp.MODE_UNMANAGED


# --- G2: every legacy fallback runs the gate ---


def test_gemini_notify_with_to_role_refuses_before_the_legacy_command(
    tmp_path, monkeypatch, capsys
):
    ask = _load_ask_module()
    record = _install_launch(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CCB_CALLER", "claude")
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: True)
    monkeypatch.setattr(ask, "_preflight_target", lambda _provider, **_kwargs: True)
    monkeypatch.setattr(ask, "_resolve_unified_daemon_context", lambda: None)
    ran: list = []
    monkeypatch.setattr(ask.subprocess, "run", lambda *a, **k: ran.append(a) or type(
        "R", (), {"returncode": 0}
    )())

    rc = ask.main(["ask", "gemini", "--notify", "--to-role", "ratifier", "FYI"])

    err = capsys.readouterr().err
    assert rc == ask.EXIT_ERROR
    assert "cannot verify target role" in err or "target role mismatch" in err
    assert ran == []


def test_gemini_notify_policy_off_keeps_the_legacy_command(tmp_path, monkeypatch):
    ask = _load_ask_module()
    _install_launch(tmp_path, monkeypatch, role_policy="off")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CCB_CALLER", "claude")
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: True)
    monkeypatch.setattr(ask, "_preflight_target", lambda _provider, **_kwargs: True)
    monkeypatch.setattr(ask, "_resolve_unified_daemon_context", lambda: None)
    ran: list = []
    monkeypatch.setattr(
        ask.subprocess,
        "run",
        lambda *a, **k: ran.append(a) or type("R", (), {"returncode": 0})(),
    )

    rc = ask.main(["ask", "gemini", "--notify", "FYI"])

    assert rc == 0
    assert len(ran) == 1


# --- G3: mixed versions -------------------------------------------------


def _stub_daemon_without_check_delivery(monkeypatch, ask, response: dict | None = None):
    calls: list = []

    def fake_request(_state, request, **_kwargs):
        calls.append(request)
        return response if response is not None else {
            "type": "ask.response",
            "v": 1,
            "id": request.get("id"),
            "exit_code": 0,
            "reply": "",
            "queued": True,  # an old daemon answers the request normally
        }

    import askd_rpc

    monkeypatch.setattr(askd_rpc, "request_daemon", fake_request)
    return calls


def test_old_daemon_refuses_when_a_target_role_is_asserted(tmp_path, monkeypatch):
    ask = _load_ask_module()
    _install_launch(tmp_path, monkeypatch, role_policy="off")
    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, tmp_path)
    _stub_daemon_without_check_delivery(monkeypatch, ask)

    allowed, refusal = ask._preflight_delivery(
        "pi", daemon_context=context, work_dir=str(tmp_path), to_role="implementer"
    )

    assert allowed is False
    assert "cannot confirm the CCB daemon supports role policy" in refusal


def test_old_daemon_continues_when_nothing_needs_gating(tmp_path, monkeypatch):
    ask = _load_ask_module()
    _install_launch(tmp_path, monkeypatch, role_policy="off")
    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, tmp_path)
    _stub_daemon_without_check_delivery(monkeypatch, ask)

    allowed, refusal = ask._preflight_delivery(
        "pi", daemon_context=context, work_dir=str(tmp_path)
    )

    assert allowed is True
    assert refusal == ""


def test_old_daemon_refuses_an_enforcing_destination(tmp_path, monkeypatch):
    ask = _load_ask_module()
    _install_launch(tmp_path, monkeypatch)  # enforce
    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, tmp_path)
    _stub_daemon_without_check_delivery(monkeypatch, ask)

    allowed, refusal = ask._preflight_delivery(
        "pi", daemon_context=context, work_dir=str(tmp_path)
    )

    assert allowed is False
    assert "cannot confirm the CCB daemon supports role policy" in refusal


def test_old_client_without_credential_or_kind_reaches_an_enforcing_lead(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    legacy = _request(
        record,
        provider="claude",
        sender="manual",
        kind="",
        sender_member_id="",
        sender_member_token="",
        to_role="",
    )
    assert dp.evaluate_request(legacy, "claude").allowed

    to_ratifier = _request(
        record,
        provider="codex",
        sender="manual",
        kind="",
        sender_member_id="",
        sender_member_token="",
        to_role="",
    )
    decision = dp.evaluate_request(to_ratifier, "codex")
    assert not decision.allowed
    assert "external senders may only send to a session holding the lead role" in decision.reason


# --- G4: A7 reports only refusals --------------------------------------


def test_async_submit_acknowledges_a_real_completed_success(tmp_path, monkeypatch):
    from askd.adapters.base import ProviderResult
    from completion_hook import COMPLETION_STATUS_COMPLETED

    record = _install_launch(tmp_path, monkeypatch)
    preset = ProviderResult(
        exit_code=0,
        reply="done",
        req_id="20261105-120010-000-1",
        session_key="pi:launch:x:live:y",
        done_seen=True,
        status=COMPLETION_STATUS_COMPLETED,
    )
    daemon = _daemon_with(_RecordingPool(_FakeTask(preset.req_id, preset)))

    response = daemon._handle_request(
        _daemon_msg(record, provider="pi", sender="claude", async_submit=True)
    )

    assert response["accepted"] is True
    assert response["exit_code"] == 0


def test_async_submit_reports_a_failed_preset_as_a_refusal(tmp_path, monkeypatch):
    from askd.adapters.base import ProviderResult
    from completion_hook import COMPLETION_STATUS_FAILED

    record = _install_launch(tmp_path, monkeypatch)
    preset = ProviderResult(
        exit_code=1,
        reply="refused",
        req_id="20261105-120011-000-1",
        session_key="k",
        done_seen=False,
        status=COMPLETION_STATUS_FAILED,
    )
    daemon = _daemon_with(_RecordingPool(_FakeTask(preset.req_id, preset)))

    response = daemon._handle_request(
        _daemon_msg(record, provider="pi", sender="claude", async_submit=True)
    )

    assert response["accepted"] is False
    assert response["exit_code"] == 1
    assert response["reply"] == "refused"


# --- G5: a background correlated reply is judged as a reply ------------


def test_peer_preflight_kind_is_reply_for_a_correlated_reply():
    ask = _load_ask_module()
    assert ask._peer_preflight_kind("background", "20261105-120000-000-1") == "reply"
    assert ask._peer_preflight_kind("wait", "") == "request"
    assert ask._peer_preflight_kind("notify", "") == "notify"


def test_background_correlated_reply_is_preflighted_as_a_reply(tmp_path, monkeypatch):
    ask = _load_ask_module()
    seen: dict = {}
    monkeypatch.setattr(
        ask, "_resolve_unified_daemon_context", lambda: None
    )
    monkeypatch.setattr(
        ask,
        "_preflight_delivery",
        lambda *a, **k: seen.update(k) or (True, ""),
    )
    monkeypatch.setattr(
        ask, "_prepare_peer_task", lambda *a, **k: ask._PeerTaskFiles(
            "task-1", tmp_path / "l", tmp_path / "s", tmp_path / "m", tmp_path / "r"
        )
    )
    for name in ("l", "s", "m", "r"):
        (tmp_path / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(
        ask.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 1})()
    )
    monkeypatch.setattr(ask, "_append_task_status_line", lambda *a, **k: None)
    monkeypatch.setattr(ask, "_print_peer_task_submission", lambda *a, **k: None)

    rc = ask._run_peer_bridge_background(
        "/tmp/peer", "claude", 5.0, "reply body", "codex", "background",
        "20261105-120000-000-1", tmp_path, live_id="live-x",
    )

    assert rc == ask.EXIT_OK
    assert seen["kind"] == "reply"
    assert seen["reply_to"] == "20261105-120000-000-1"
    assert seen["live_id"] == "live-x"


def test_background_correlated_reply_refusal_stops_before_spawning(tmp_path, monkeypatch):
    ask = _load_ask_module()
    monkeypatch.setattr(ask, "_resolve_unified_daemon_context", lambda: None)
    monkeypatch.setattr(
        ask, "_preflight_delivery", lambda *a, **k: (False, "role policy refused: no receipt")
    )
    spawned: list = []
    monkeypatch.setattr(ask.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(ask, "_prepare_peer_task", lambda *a, **k: spawned.append(a))

    rc = ask._run_peer_bridge_background(
        "/tmp/peer", "claude", 5.0, "reply body", "codex", "background",
        "20261105-120099-000-1", tmp_path,
    )

    assert rc == ask.EXIT_ERROR
    assert spawned == []


def test_correlated_reply_reaches_a_ratifier_requester(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    _write_peer_receipt(
        "20261105-120012-000-1",
        caller="codex",  # the ratifier asked; the reply must reach it
        pane_id=record["providers"]["codex"]["pane_id"],
        project_id=record["ccb_project_id"],
        work_dir=record["work_dir"],
    )
    peer = PeerDestination(
        pane_id=record["providers"]["codex"]["pane_id"],
        terminal=record["terminal"],
        work_dir=record["work_dir"],
        ccb_project_id=record["ccb_project_id"],
    )
    request = _request(
        record, provider="codex", sender="pi", kind="reply",
        delivery_only=True, peer=peer, reply_to="20261105-120012-000-1",
        route=ResolvedRoute(),
    )
    assert dp.evaluate_request(request, "codex").allowed


# --- G6: peer recheck at the injection point ---------------------------


def test_peer_recheck_refuses_after_a_role_change_with_zero_sends(tmp_path, monkeypatch):
    import peer_routing
    from askd.adapters.base import PeerDestination as PD, ProviderRequest as PR, QueuedTask as QT
    from peer_routing import DestinationResolution

    record = _install_launch(tmp_path, monkeypatch)
    backend = _FakeBackend()
    monkeypatch.setattr(peer_routing, "revalidate_peer_destination", lambda _dest: DestinationResolution())
    monkeypatch.setattr(
        "terminal.get_backend_for_session", lambda _data: backend, raising=False
    )
    # The role disappears between accept and injection.
    monkeypatch.setattr(dp, "_current_roles", lambda _record: {})

    peer = PD(
        pane_id=record["providers"]["codex"]["pane_id"],
        terminal=record["terminal"],
        work_dir=record["work_dir"],
        ccb_project_id=record["ccb_project_id"],
    )
    request = PR(
        client_id="t", work_dir=record["work_dir"], timeout_s=1.0, quiet=True,
        message="hello", caller="codex", delivery_only=True, peer_destination=peer,
    )
    task = QT(request=request, created_ms=0, req_id="20261105-120013-000-1",
              done_event=__import__("threading").Event())

    result = peer_routing.send_peer_message(task, "codex", "codex:peer:%99")

    assert result.exit_code == 1
    assert "role policy refused" in result.reply
    assert result.status != "completed"
    assert backend.sent == []


# --- H0/H1: one discovery path -----------------------------------------


def _write_launch(record: dict, *, launch_id: str, role_policy: str | None,
                  updated_at: int | None = None, providers: bool = True) -> dict:
    """Publish a launch record the way the launcher really does."""
    out = {k: v for k, v in record.items() if k not in ("providers", "live_sessions")}
    out["ccb_session_id"] = launch_id
    out["ccb_pid"] = os.getpid()
    out["updated_at"] = int(time.time()) if updated_at is None else updated_at
    if providers:
        out["providers"] = record["providers"]
    if role_policy is None:
        out.pop("role_policy", None)
    else:
        out["role_policy"] = role_policy
    (_run_dir() / f"ccb-session-{launch_id}.json").write_text(json.dumps(out), encoding="utf-8")
    return out


def test_stale_enforcing_peer_destination_refuses(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    _write_launch(record, launch_id=record["ccb_session_id"], role_policy="enforce", updated_at=1)

    destination = dp.destination_from_pane(
        "codex",
        record["providers"]["codex"]["pane_id"],
        ccb_project_id=record["ccb_project_id"],
    )

    assert destination.mode == dp.MODE_ERROR
    assert "stale" in destination.reason


def test_inventory_only_codex_pair_member_is_found_and_judged(tmp_path, monkeypatch):
    """The launcher publishes a pair through `live_sessions`, not providers."""
    monkeypatch.setattr(dp, "_current_roles", lambda _record: {"cx1": "ratifier"})
    pair_project = tmp_path / "pair-only"
    pair_project.mkdir()
    pair = {
        "ccb_session_id": "ai-1700000002-1000000",
        "ccb_project_id": compute_ccb_project_id(pair_project),
        "work_dir": str(pair_project),
        "terminal": "tmux",
        "ccb_pid": os.getpid(),
        "updated_at": int(time.time()),
        "role_policy": "enforce",
        # No `providers` key at all: members exist only in the inventory.
        "live_sessions": [
            {
                "live_id": "cx1",
                "provider": "codex",
                "pane_id": "%71",
                "pane_title_marker": "CCB-Codex-cx1",
                "terminal": "tmux",
                "session_file": str(pair_project / ".ccb" / ".codex-session"),
                "auth_token": "tok-cx1",
                "active": True,
            }
        ],
    }
    _run_dir().mkdir(parents=True, exist_ok=True)
    (_run_dir() / "ccb-session-ai-1700000002-1000000.json").write_text(
        json.dumps(pair), encoding="utf-8"
    )

    destination = dp.destination_from_pane(
        "codex", "%71", ccb_project_id=compute_ccb_project_id(pair_project)
    )

    # Found through the INVENTORY (providers alone would miss it) and read as
    # enforcing, for the right member.
    assert destination.mode in (dp.MODE_ENFORCE, dp.MODE_ERROR)
    assert destination.launch_id == "ai-1700000002-1000000"
    assert destination.member_id == "cx1"


# --- H2: client guards follow the session binding ---------------------


def _install_gemini_binding(record: dict, tmp_path: Path, *, subdir: Path) -> None:
    cfg = tmp_path / ".ccb"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / ".gemini-session").write_text(
        json.dumps(
            {
                "active": True,
                "work_dir": str(tmp_path),
                "session_id": record["ccb_session_id"],
                "ccb_project_id": record["ccb_project_id"],
                "pane_id": "%44",
                "pane_title_marker": "CCB-Gemini-0f0f0f0f",
                "gemini_session_path": str(cfg / "gemini.json"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch_wd = subdir
    return monkeypatch_wd


def test_client_guard_follows_the_binding_from_a_subdirectory(tmp_path, monkeypatch, capsys):
    ask = _load_ask_module()
    record = _install_launch(tmp_path, monkeypatch)
    subdir = tmp_path / "nested"
    subdir.mkdir()
    _install_gemini_binding(record, tmp_path, subdir=subdir)
    monkeypatch.chdir(subdir)
    monkeypatch.setenv("CCB_CALLER", "claude")
    monkeypatch.setattr(ask, "_use_unified_daemon", lambda: True)
    monkeypatch.setattr(ask, "_preflight_target", lambda _provider, **_kwargs: True)
    monkeypatch.setattr(ask, "_resolve_unified_daemon_context", lambda: None)
    ran: list = []
    monkeypatch.setattr(
        ask.subprocess, "run",
        lambda *a, **k: ran.append(a) or type("R", (), {"returncode": 0})(),
    )

    rc = ask.main(["ask", "gemini", "--notify", "FYI"])

    err = capsys.readouterr().err
    assert rc == ask.EXIT_ERROR
    assert "role policy requires the unified daemon" in err
    assert ran == []


def test_old_daemon_preflight_follows_the_binding_from_a_subdirectory(tmp_path, monkeypatch):
    ask = _load_ask_module()
    record = _install_launch(tmp_path, monkeypatch)
    subdir = tmp_path / "nested"
    subdir.mkdir()
    _install_gemini_binding(record, tmp_path, subdir=subdir)
    monkeypatch.chdir(subdir)
    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, subdir)
    _stub_daemon_without_check_delivery(monkeypatch, ask)

    allowed, refusal = ask._preflight_delivery(
        "gemini", daemon_context=context, work_dir=str(subdir)
    )

    assert allowed is False
    assert "cannot confirm the CCB daemon supports role policy" in refusal


# --- H3: mixed versions fail closed when the gate is required ----------


def test_bridge_refuses_an_old_daemon_when_a_target_role_is_asserted(monkeypatch, tmp_path):
    bridge = _load_bridge_module()
    sent: list = []

    monkeypatch.setattr(bridge.askd_rpc, "read_state", lambda _path: {"port": 1, "token": "tok"})
    monkeypatch.setattr(
        bridge, "find_running_state_file", lambda *_a, **_k: Path("/tmp/askd.json")
    )

    def fake_request(_state, request, **_kwargs):
        sent.append(request)
        return {"exit_code": 0, "reply": ""}  # an old daemon: no "allowed"

    monkeypatch.setattr(bridge.askd_rpc, "request_daemon", fake_request)

    with pytest.raises(RuntimeError, match="cannot confirm the CCB daemon supports role policy"):
        bridge._send_to_daemon(
            {"work_dir": str(tmp_path), "ccb_project_id": "abcd1234"},
            "hello",
            5.0,
            "pane-1",
            "tmux",
            str(tmp_path),
            "wait",
            "task-9",
            "",
            provider="claude",
            caller="claude",
            to_role="lead",
        )

    # Only the capability probe went out; nothing was submitted.
    assert len(sent) == 1
    assert sent[0]["operation"] == "check_delivery"


def test_preflight_raising_refuses_when_a_target_role_is_asserted(tmp_path, monkeypatch):
    ask = _load_ask_module()
    _install_launch(tmp_path, monkeypatch, role_policy="off")
    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, tmp_path)

    def boom(*_a, **_k):
        raise OSError("connection refused")

    import askd_rpc

    monkeypatch.setattr(askd_rpc, "request_daemon", boom)

    allowed, refusal = ask._preflight_delivery(
        "pi", daemon_context=context, work_dir=str(tmp_path), to_role="implementer"
    )

    assert allowed is False
    assert "cannot confirm the CCB daemon supports role policy" in refusal


def test_preflight_raising_continues_when_nothing_needs_gating(tmp_path, monkeypatch):
    ask = _load_ask_module()
    _install_launch(tmp_path, monkeypatch, role_policy="off")
    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, tmp_path)

    def boom(*_a, **_k):
        raise OSError("connection refused")

    import askd_rpc

    monkeypatch.setattr(askd_rpc, "request_daemon", boom)

    allowed, refusal = ask._preflight_delivery(
        "pi", daemon_context=context, work_dir=str(tmp_path)
    )

    assert allowed is True
    assert refusal == ""


# --- H4: live-id selector honoured for path targets --------------------


def test_path_target_honours_the_live_id_selector(tmp_path, monkeypatch):
    record = _install_launch(tmp_path, monkeypatch)
    pair_project = tmp_path / "pair"
    pair_project.mkdir()
    pair_id = compute_ccb_project_id(pair_project)
    pair = {
        "ccb_session_id": "ai-1700000003-1000000",
        "ccb_project_id": pair_id,
        "work_dir": str(pair_project),
        "terminal": "tmux",
        "ccb_pid": os.getpid(),
        "updated_at": int(time.time()),
        "role_policy": "enforce",
        "live_sessions": [
            {"live_id": "cx1", "provider": "codex", "pane_id": "%71",
             "pane_title_marker": "C1", "terminal": "tmux",
             "session_file": str(pair_project / ".ccb" / ".codex-session"),
             "auth_token": "t1", "active": True},
            {"live_id": "cx2", "provider": "codex", "pane_id": "%72",
             "pane_title_marker": "C2", "terminal": "tmux",
             "session_file": str(pair_project / "run" / "cx2" / ".codex-session"),
             "auth_token": "t2", "active": True},
        ],
    }
    (_run_dir() / "ccb-session-ai-1700000003-1000000.json").write_text(
        json.dumps(pair), encoding="utf-8"
    )

    by_path = dp.destination_from_project_hint("codex", str(pair_project), member_id="cx2")
    by_prefix = dp.destination_from_project_hint("codex", pair_id[:8], member_id="cx2")

    assert by_path.member_id == "cx2"
    assert by_path.pane_id == "%72"
    assert by_prefix.member_id == "cx2"


# --- J1: exact member within a launch, never the first one -------------


def _pair_project(tmp_path: Path, monkeypatch, *, binding_pane: str = "", policy: str = "enforce"):
    """A Codex pair project: cx1 on %71, cx2 on %72, one launch record."""
    monkeypatch.setattr(dp, "_current_roles", lambda _r: {"cx1": "ratifier", "cx2": "implementer"})
    project = tmp_path / "pair"
    (project / ".ccb").mkdir(parents=True)
    project_id = compute_ccb_project_id(project)
    launch = "ai-1700000004-1000000"
    _run_dir().mkdir(parents=True, exist_ok=True)
    (_run_dir() / f"ccb-session-{launch}.json").write_text(
        json.dumps(
            {
                "ccb_session_id": launch,
                "ccb_project_id": project_id,
                "work_dir": str(project),
                "terminal": "tmux",
                "ccb_pid": os.getpid(),
                "updated_at": int(time.time()),
                "role_policy": policy,
                "live_sessions": [
                    {"live_id": "cx1", "provider": "codex", "pane_id": "%71",
                     "pane_title_marker": "C1", "terminal": "tmux",
                     "session_file": str(project / ".ccb" / ".codex-session"),
                     "auth_token": "t1", "active": True},
                    {"live_id": "cx2", "provider": "codex", "pane_id": "%72",
                     "pane_title_marker": "C2", "terminal": "tmux",
                     "session_file": str(project / "run" / "cx2" / ".codex-session"),
                     "auth_token": "t2", "active": True},
                ],
            }
        ),
        encoding="utf-8",
    )
    if binding_pane:
        (project / ".ccb" / ".codex-session").write_text(
            json.dumps(
                {
                    "active": True,
                    "work_dir": str(project),
                    "session_id": launch,
                    "ccb_project_id": project_id,
                    "pane_id": binding_pane,
                }
            ),
            encoding="utf-8",
        )
    return project, project_id, launch


def test_path_target_with_a_project_binding_still_honours_the_selector(tmp_path, monkeypatch):
    project, project_id, _launch = _pair_project(tmp_path, monkeypatch, binding_pane="%71")

    destination = dp.destination_from_project_hint("codex", str(project), member_id="cx2")

    assert destination.member_id == "cx2"
    assert destination.pane_id == "%72"


def test_prefix_target_honours_the_selector(tmp_path, monkeypatch):
    project, project_id, _launch = _pair_project(tmp_path, monkeypatch, binding_pane="%71")

    destination = dp.destination_from_project_hint("codex", project_id[:8], member_id="cx2")

    assert destination.member_id == "cx2"
    assert destination.pane_id == "%72"


def test_session_bound_to_cx2_is_judged_as_cx2_without_a_selector(tmp_path, monkeypatch):
    project, _project_id, _launch = _pair_project(tmp_path, monkeypatch, binding_pane="%72")

    destination = dp.destination_from_work_dir("codex", project)

    assert destination.member_id == "cx2"
    assert destination.pane_id == "%72"


def test_a_pair_without_a_selector_refuses_under_enforce(tmp_path, monkeypatch):
    project, _project_id, launch = _pair_project(tmp_path, monkeypatch, binding_pane="")

    destination = dp.destination_from_launch("codex", launch)

    assert destination.mode == dp.MODE_ERROR
    assert "cannot identify the destination session" in destination.reason


# --- J2: the bridge probe judges the real request ----------------------


def test_bridge_probe_allows_a_correlated_reply_to_a_ratifier(tmp_path, monkeypatch):
    bridge = _load_bridge_module()
    calls: list = []
    monkeypatch.setattr(bridge.askd_rpc, "read_state", lambda _p: {"port": 1, "token": "tok"})
    monkeypatch.setattr(
        bridge, "find_running_state_file", lambda *_a, **_k: Path("/tmp/askd.json")
    )
    _install_launch(tmp_path, monkeypatch)  # enforcing destination

    def fake_request(_state, request, **_kwargs):
        calls.append(request)
        if request.get("operation") == "check_delivery":
            return {"allowed": True, "reason": ""}
        return {"exit_code": 0, "reply": "delivered", "meta": {"status": "completed"}}

    monkeypatch.setattr(bridge.askd_rpc, "request_daemon", fake_request)

    exit_code, reply, _meta = bridge._send_to_daemon(
        {
            "work_dir": str(tmp_path),
            "ccb_project_id": compute_ccb_project_id(tmp_path),
            "providers": {"codex": {"pane_id": record_pane(tmp_path)}},
        },
        "reply body",
        5.0,
        "pane-1",
        "tmux",
        str(tmp_path),
        "wait",
        "task-j2",
        "20261105-120020-000-1",
        provider="codex",
        caller="pi",
    )

    assert exit_code == 0
    assert reply == "delivered"
    probe = calls[0]
    assert probe["operation"] == "check_delivery"
    assert probe["kind"] == "reply"
    assert probe["reply_to"] == "20261105-120020-000-1"
    assert calls[1].get("operation") is None


def test_bridge_probe_reports_a_correlation_refusal_as_policy(tmp_path, monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(bridge.askd_rpc, "read_state", lambda _p: {"port": 1, "token": "tok"})
    monkeypatch.setattr(
        bridge, "find_running_session", lambda *_a, **_k: Path("/tmp/askd.json"),
        raising=False,
    )
    monkeypatch.setattr(
        bridge, "find_running_state_file", lambda *_a, **_k: Path("/tmp/askd.json")
    )
    _install_launch(tmp_path, monkeypatch)
    reason = "role policy refused: reply does not return to the original requester pane"
    monkeypatch.setattr(
        bridge.askd_rpc,
        "request_daemon",
        lambda _state, request, **_k: {"allowed": False, "reason": reason},
    )

    with pytest.raises(RuntimeError, match="does not return to the original requester pane"):
        bridge._send_to_daemon(
            {
                "work_dir": str(tmp_path),
                "ccb_project_id": compute_ccb_project_id(tmp_path),
                "providers": {"codex": {"pane_id": record_pane(tmp_path)}},
            },
            "reply body",
            5.0,
            "pane-1",
            "tmux",
            str(tmp_path),
            "wait",
            "task-j2b",
            "20261105-120021-000-1",
            provider="codex",
            caller="pi",
        )


def record_pane(tmp_path: Path) -> str:
    """The pane the fixture's codex member holds (survives relocation)."""
    return "%14"


# --- J3: conflicting ownership is a discovery, not ignorance -----------


def test_two_enforcing_owners_require_the_gate_and_refuse_an_old_daemon(tmp_path, monkeypatch):
    ask = _load_ask_module()
    record = _install_launch(tmp_path, monkeypatch)
    _write_launch(record, launch_id="ai-1700000005-1000000", role_policy="enforce")

    assert ask.gate_required("pi", work_dir=str(tmp_path)) is True

    context = ask._UnifiedDaemonContext(Path("/tmp/askd.json"), {"token": "tok"}, tmp_path)
    _stub_daemon_without_check_delivery(monkeypatch, ask)
    allowed, refusal = ask._preflight_delivery(
        "pi", daemon_context=context, work_dir=str(tmp_path)
    )
    assert allowed is False
    assert "cannot confirm the CCB daemon supports role policy" in refusal
