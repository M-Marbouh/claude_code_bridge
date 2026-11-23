"""Cross-machine `share_all`: the opt-in "share every project" grant.

Plan and ratification: docs/plans/cross-machine-share-all-plan.md (A1-A6).

The tests are grouped the way the plan states the requirements:

* the grant model derives nothing unless asked (the negative control), and the
  eligibility rules (A4) decide what it may ever derive;
* `list` and `submit` authorize through ONE seam, so they cannot disagree;
* a malformed `share_all` keeps each verb's existing failure shape (A6);
* the pairing surface advertises the capability (A1), accepts zero alias picks
  (A3) and refuses to claim a grant it could not prove (A2).

Every path used here lives under `tmp_path`; the registry fixture is a
shape-copy of a real `ccb-session-*.json` with all ids and tokens replaced.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
import remote_client
import remote_discovery
import remote_journal
import remote_pairing
import remote_protocol as proto
from project_id import compute_ccb_project_id
from remote_delivery import Destination, RemoteDeliveryService
from remote_journal import (
    RemoteConfig,
    RemoteConfigError,
    RemoteJournal,
    load_config,
)

LOCAL_ID = "20261123-094910-540-475956"
REQUEST_ID = f"{LOCAL_ID}-habc1"

#: The destination's issuer token is a documented NON-SECRET routing label
#: (guide §4a): it namespaces request ids, it authorizes nothing, and the real
#: value is never secret. The literal is used inline below for that reason.

#: A copy of a real registry record's KEY SHAPE, with every id, token, marker
#: and path replaced. `providers` carries the three provider slots a real
#: launch writes; only claude/codex are remote providers.
SESSION_RECORD_SHAPE = {
    "ccb_session_id": "ai-0000000000-000000",
    "ccb_project_id": "p" * 64,
    "work_dir": "/srv/placeholder-project",
    "terminal": "tmux",
    "providers": {
        "claude": {
            "member_id": "member-placeholder-claude",
            "member_token": "token-placeholder-claude",
            "pane_id": "%1",
            "pane_title_marker": "CCB-claude-placeholder",
            "session_file": "/srv/placeholder-project/.claude-session",
        },
        "codex": {
            "member_id": "member-placeholder-codex",
            "member_token": "token-placeholder-codex",
            "pane_id": "%2",
            "pane_title_marker": "CCB-codex-placeholder",
            "session_file": "/srv/placeholder-project/.codex-session",
        },
        "pi": {
            "member_id": "member-placeholder-pi",
            "member_token": "token-placeholder-pi",
            "pane_id": "%3",
            "pane_title_marker": "CCB-pi-placeholder",
            "session_file": "/srv/placeholder-project/.pi-session",
        },
    },
    "role_policy": {"lead": "claude"},
    "terminal_panes": {"claude": "%1", "codex": "%2", "pi": "%3"},
    "updated_at": 1700000000,
}


def _make_project(root: Path, name: str = "demo", *, config_dir: str = ".ccb") -> Path:
    """A directory that looks like a real CCB project."""
    project = root / name
    project.mkdir(parents=True, exist_ok=True)
    (project / config_dir).mkdir(exist_ok=True)
    return project


def _write_registry(registry_dir: Path, records: list[dict]) -> Path:
    registry_dir.mkdir(parents=True, exist_ok=True)
    for index, record in enumerate(records):
        (registry_dir / f"ccb-session-ai-{index}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    return registry_dir


def _project_tree(tmp_path: Path) -> Path:
    """A directory OUTSIDE the suite's temp root, where projects may live.

    `conftest.py` points `tempfile.tempdir` at `tmp_path`, so anything under
    `tmp_path` is (correctly) excluded from a share-all grant. Tests that need
    an ELIGIBLE project therefore root it beside `tmp_path` instead.
    """
    tree = tmp_path.parent / f"{tmp_path.name}-projects"
    tree.mkdir(parents=True, exist_ok=True)
    return tree


def _registry_record(work_dir: Path) -> dict:
    record = json.loads(json.dumps(SESSION_RECORD_SHAPE))
    record["work_dir"] = str(work_dir)
    record["ccb_project_id"] = compute_ccb_project_id(work_dir)
    record["providers"]["claude"]["session_file"] = str(work_dir / ".claude-session")
    return record


# --------------------------------------------------------------------------- #
# The grant model
# --------------------------------------------------------------------------- #


def test_share_all_off_derives_nothing_even_when_projects_are_eligible():
    """Negative control: the DEFAULT must expose only what config names."""
    config = RemoteConfig(
        principals={
            "bob": remote_journal.Principal(
                name="bob", issuer_token="abc1", allowed_aliases=("demo",)
            )
        },
        aliases={
            "demo": remote_journal.Alias(name="demo", work_dir="/srv/demo", providers=("claude",))
        },
    )
    projects = [{"work_dir": "/srv/demo", "project_id": "a" * 64},
                {"work_dir": "/srv/other", "project_id": "b" * 64}]

    effective = config.effective_aliases("bob", projects)

    assert tuple(effective.aliases) == ("demo",)
    assert effective.derived_names() == ()
    assert effective.share_all is False
    assert effective.skipped == {}


def test_share_all_on_derives_every_eligible_project_under_its_folder_name():
    config = RemoteConfig(
        principals={
            "bob": remote_journal.Principal(
                name="bob", issuer_token="abc1", allowed_aliases=("demo",), share_all=True
            )
        },
        aliases={
            "demo": remote_journal.Alias(name="demo", work_dir="/srv/demo", providers=("claude",))
        },
    )
    projects = [
        {"work_dir": "/srv/demo", "project_id": "a" * 64},
        {"work_dir": "/srv/My_Other.Project", "project_id": "b" * 64},
    ]

    effective = config.effective_aliases("bob", projects)

    # The explicit alias stays; the same directory is NOT derived twice.
    assert set(effective.aliases) == {"demo", "my-other-project"}
    assert effective.derived_names() == ("my-other-project",)
    assert effective.aliases["my-other-project"].project_id == "b" * 64
    assert effective.aliases["my-other-project"].providers == ("claude", "codex")


def test_two_projects_normalising_to_one_alias_are_both_dropped_and_reported():
    config = RemoteConfig(
        principals={
            "bob": remote_journal.Principal(name="bob", issuer_token="abc1", share_all=True)
        }
    )
    projects = [
        {"work_dir": "/srv/My_Project", "project_id": "a" * 64},
        {"work_dir": "/opt/my-project", "project_id": "b" * 64},
        {"work_dir": "/srv/unique", "project_id": "c" * 64},
    ]

    effective = config.effective_aliases("bob", projects)

    # Ambiguity is refused, never resolved by picking one.
    assert "my-project" not in effective.aliases
    assert "unique" in effective.aliases
    assert "my-project" in effective.skipped
    assert "more than one project" in effective.skipped["my-project"]


def test_explicit_alias_wins_a_name_clash_over_a_derived_one():
    config = RemoteConfig(
        principals={
            "bob": remote_journal.Principal(
                name="bob", issuer_token="abc1", allowed_aliases=("my-project",), share_all=True
            )
        },
        aliases={
            "my-project": remote_journal.Alias(
                name="my-project", work_dir="/srv/elsewhere", providers=("claude",)
            )
        },
    )
    projects = [{"work_dir": "/srv/My_Project", "project_id": "a" * 64}]

    effective = config.effective_aliases("bob", projects)

    assert effective.aliases["my-project"].work_dir == "/srv/elsewhere"
    assert effective.aliases["my-project"].derived is False
    assert effective.derived_names() == ()


def test_an_explicit_alias_for_the_same_directory_suppresses_the_derived_name():
    config = RemoteConfig(
        principals={
            "bob": remote_journal.Principal(
                name="bob", issuer_token="abc1", allowed_aliases=("nickname",), share_all=True
            )
        },
        aliases={
            "nickname": remote_journal.Alias(
                name="nickname", work_dir="/srv/My_Project", providers=("claude",)
            )
        },
    )
    projects = [{"work_dir": "/srv/My_Project", "project_id": "a" * 64}]

    effective = config.effective_aliases("bob", projects)

    # One project, one name: the explicit one.
    assert set(effective.aliases) == {"nickname"}
    assert effective.derived_names() == ()


def test_share_all_providers_narrows_and_is_intersected_with_the_protocol_set():
    config = RemoteConfig(
        principals={
            "bob": remote_journal.Principal(
                name="bob",
                issuer_token="abc1",
                share_all=True,
                share_all_providers=("claude", "gemini"),
                share_all_providers_given=True,
            )
        }
    )
    projects = [{"work_dir": "/srv/demo", "project_id": "a" * 64}]

    effective = config.effective_aliases("bob", projects)

    # gemini is not a remote provider: it can never be granted.
    assert effective.aliases["demo"].providers == ("claude",)


def test_unknown_principal_derives_nothing():
    config = RemoteConfig()
    assert config.effective_aliases("ghost", [{"work_dir": "/srv/demo"}]).aliases == {}


# --------------------------------------------------------------------------- #
# Eligibility (A4)
# --------------------------------------------------------------------------- #


def test_eligible_projects_excludes_temp_missing_and_non_project_dirs(tmp_path):
    home = tmp_path / "home"
    projects = _project_tree(tmp_path)
    real = _make_project(projects, "demo")
    legacy = _make_project(projects, "legacy", config_dir=".ccb_config")
    plain = projects / "plain"
    plain.mkdir(parents=True)
    gone = projects / "gone"
    temp_project = _make_project(tmp_path, "inside-tmp")

    registry = _write_registry(
        home / ".ccb" / "run",
        [
            _registry_record(real),
            _registry_record(legacy),
            _registry_record(plain),
            _registry_record(gone),
            _registry_record(temp_project),
        ],
    )
    # tmp_path IS the system temp dir for this test (conftest sets tempfile.tempdir).
    # `temp_dir` is named explicitly so "under the temp dir" is provable here.
    eligible = remote_pairing.eligible_projects(registry, temp_dir=tmp_path)

    dirs = {entry["work_dir"] for entry in eligible}
    assert str(real.resolve()) in dirs
    # A4: the legacy config dir counts as a project anchor.
    assert str(legacy.resolve()) in dirs
    # No project config -> not a project.
    assert str(plain.resolve()) not in dirs
    # Registry entry whose directory no longer exists.
    assert str(gone.resolve()) not in dirs
    # Under the system temp dir -> never shared.
    assert str(temp_project.resolve()) not in dirs


def test_eligible_projects_dedups_a_symlink_with_its_target(tmp_path):
    home = tmp_path / "home"
    projects = _project_tree(tmp_path)
    target = _make_project(projects, "real-project")
    link = projects / "linked-project"
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable on this platform")
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")

    registry = _write_registry(
        home / ".ccb" / "run", [_registry_record(target), _registry_record(link)]
    )
    eligible = remote_pairing.eligible_projects(registry, temp_dir=tmp_path)

    # A4: canonicalised BEFORE dedup, so the symlink does not become a second
    # project (and cannot collide with its own target's derived alias).
    assert [entry["work_dir"] for entry in eligible] == [str(target.resolve())]
    assert eligible[0]["project_id"] == compute_ccb_project_id(target)


def test_eligible_projects_carries_the_project_id_the_resolver_would_use(tmp_path):
    home = tmp_path / "home"
    project = _make_project(_project_tree(tmp_path), "demo")
    registry = _write_registry(home / ".ccb" / "run", [_registry_record(project)])

    eligible = remote_pairing.eligible_projects(registry, temp_dir=tmp_path)

    assert eligible[0]["project_id"] == compute_ccb_project_id(project.resolve())
    assert set(eligible[0]) >= {"work_dir", "name", "providers", "project_id"}


# --------------------------------------------------------------------------- #
# Config loading: strict bool (A6)
# --------------------------------------------------------------------------- #


def _write_config(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.mark.parametrize("value", ["yes", 1, 0, None, [], {}])
def test_a_non_boolean_share_all_is_a_config_error(tmp_path, value):
    path = _write_config(
        tmp_path,
        {"principals": {"bob": {"issuer_token": "abc1", "share_all": value}}},
    )
    with pytest.raises(RemoteConfigError):
        load_config(path)


def test_a_boolean_share_all_loads_and_defaults_to_off(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "principals": {
                "bob": {"issuer_token": "abc1", "share_all": True},
                "eve": {"issuer_token": "xyz9"},
            },
            # Unknown fields stay ignored.
            "something_else": {"nonsense": True},
        },
    )
    config = load_config(path)
    assert config.principal("bob").share_all is True
    assert config.principal("eve").share_all is False


# --------------------------------------------------------------------------- #
# The service seam: list and submit agree
# --------------------------------------------------------------------------- #


def _destination(work_dir: str) -> Destination:
    return Destination(
        provider="claude",
        live_id="live-1",
        launch_id="ai-1",
        work_dir=work_dir,
        pane_id="%9",
        terminal="tmux",
        pane_title_marker="CCB-claude-1",
        ccb_project_id=compute_ccb_project_id(Path(work_dir)),
    )


class _Adapter:
    def __init__(self, key: str = "claude"):
        self.key = key


class _Registry:
    def __init__(self, *adapters):
        self._adapters = {adapter.key: adapter for adapter in adapters}

    def get(self, key):
        return self._adapters.get(key)


class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, object, str]] = []

    def __call__(self, provider, request, record_id):
        self.calls.append((provider, request, record_id))
        import threading

        from askd.adapters.base import QueuedTask

        return QueuedTask(
            request=request, created_ms=0, req_id=request.req_id or "", done_event=threading.Event()
        )


def _service(tmp_path: Path, *, projects: list[dict], principal_extra: dict | None = None):
    config_path = tmp_path / "remote" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    principal = {"issuer_token": "abc1", "allowed_aliases": ["named"]}
    principal.update(principal_extra or {})
    config_path.write_text(
        json.dumps(
            {
                "principals": {"bob": principal},
                "aliases": {
                    "named": {"work_dir": "/srv/named", "providers": ["claude", "codex"]}
                },
            }
        ),
        encoding="utf-8",
    )
    recorder = _Recorder()
    service = RemoteDeliveryService(
        registry=_Registry(_Adapter("claude"), _Adapter("codex")),
        enqueue=recorder,
        owner_daemon=str(tmp_path / "askd.json"),
        journal=RemoteJournal(tmp_path / "remote"),
        config_loader=lambda: load_config(config_path),
        resolver=lambda provider, work_dir, session: _destination(work_dir),
        sessions=lambda provider, work_dir: [],
    )
    # Substitute the registry read: the seam must use whatever `list` will show.
    service._share_all_projects = lambda: list(projects)  # type: ignore[assignment]
    return service, recorder


def _record(**overrides) -> dict:
    kwargs = {
        "dest_project": "named",
        "dest_provider": "claude",
        "intent": "notify",
        "text": "hello from the other machine",
        "request_id": REQUEST_ID,
        "source_claims": {},
    }
    kwargs.update(overrides)
    return proto.build_request_record(**kwargs)


def _submit_msg(record: dict, principal: str = "bob") -> dict:
    return {
        "type": "ask.request",
        "v": 1,
        "id": "op-1",
        "operation": "remote_submit",
        "principal": principal,
        "record": record,
    }


def _projects(*entries) -> list[dict]:
    return [
        {"work_dir": work_dir, "project_id": compute_ccb_project_id(Path(work_dir))}
        for work_dir in entries
    ]


def test_without_share_all_a_new_project_is_refused_unauthorized(tmp_path):
    """Negative control for the whole feature."""
    service, recorder = _service(
        tmp_path, projects=_projects(str(tmp_path / "newcomer"))
    )
    response = service.handle_submit(
        _submit_msg(_record(dest_project="newcomer"))
    )

    assert response["ok"] is False
    assert response["error"]["code"] == proto.ERR_UNAUTHORIZED
    assert recorder.calls == []
    assert service.journal.read(REQUEST_ID) is None
    # And `list` never advertised it either.
    listed = service.handle_list({"id": "op-list", "principal": "bob"})
    assert [entry["alias"] for entry in listed["remote"]["projects"]] == ["named"]
    assert "share_all" not in listed["remote"]


def test_with_share_all_a_project_created_after_the_config_is_listed_and_deliverable(tmp_path):
    newcomer = tmp_path / "newcomer"
    newcomer.mkdir()
    service, recorder = _service(
        tmp_path, projects=_projects(str(newcomer)), principal_extra={"share_all": True}
    )

    listed = service.handle_list({"id": "op-list", "principal": "bob"})
    assert listed["remote"]["share_all"] is True
    entries = {entry["alias"]: entry for entry in listed["remote"]["projects"]}
    assert set(entries) == {"named", "newcomer"}
    assert entries["newcomer"]["derived"] is True
    project_id = entries["newcomer"]["project_id"]
    assert project_id == compute_ccb_project_id(newcomer)

    response = service.handle_submit(
        _submit_msg(_record(dest_project="newcomer", dest_project_id=project_id))
    )
    assert response["ok"] is True, response
    assert len(recorder.calls) == 1
    stored = service.journal.read(REQUEST_ID)
    assert stored["work_dir"] == str(newcomer)
    assert stored["dest_project_id"] == project_id
    assert stored["dest_project_derived"] is True


def test_list_and_submit_agree_over_the_whole_fixture_registry(tmp_path):
    """Property-style: every listed alias is deliverable, nothing else is."""
    names = ["alpha", "beta", "My_Gamma", "delta.project", "epsilon"]
    projects = []
    for name in names:
        directory = tmp_path / name
        directory.mkdir()
        projects.append(
            {"work_dir": str(directory), "project_id": compute_ccb_project_id(directory)}
        )
    service, recorder = _service(
        tmp_path, projects=projects, principal_extra={"share_all": True}
    )
    listed = service.handle_list({"id": "op-list", "principal": "bob"})
    advertised = {entry["alias"]: entry for entry in listed["remote"]["projects"]}

    # beta and epsilon are simple names; My_Gamma and delta.project normalise.
    assert set(advertised) == {
        "named", "alpha", "beta", "my-gamma", "delta-project", "epsilon",
    }
    expected = 0
    for index, (alias, entry) in enumerate(sorted(advertised.items())):
        if alias == "named":
            # The configured alias keeps its old shape and needs no identity.
            assert "derived" not in entry and "project_id" not in entry
            record = _record(request_id=f"20261123-094910-{500 + index}-1-habc1")
        else:
            assert entry["derived"] is True
            record = _record(
                dest_project=alias,
                request_id=f"20261123-094910-{500 + index}-1-habc1",
                dest_project_id=entry["project_id"],
            )
        assert service.handle_submit(_submit_msg(record))["ok"] is True, alias
        expected += 1
    # Every listed alias really was accepted.
    assert len(recorder.calls) == expected

    # An alias the seam does not produce is refused by BOTH verbs.
    unlisted = "not-a-project"
    assert unlisted not in advertised
    refused = service.handle_submit(_submit_msg(_record(dest_project=unlisted)))
    assert refused["error"]["code"] == proto.ERR_UNAUTHORIZED


def test_a_derived_alias_sent_without_the_identity_is_refused_with_zero_writes(tmp_path):
    """A5: no echo of the listed project identity -> refused before anything durable."""
    newcomer = tmp_path / "newcomer"
    newcomer.mkdir()
    service, recorder = _service(
        tmp_path, projects=_projects(str(newcomer)), principal_extra={"share_all": True}
    )

    response = service.handle_submit(_submit_msg(_record(dest_project="newcomer")))

    assert response["ok"] is False
    assert response["error"]["code"] == proto.ERR_UNAUTHORIZED
    assert "project identity" in response["error"]["message"]
    assert recorder.calls == []
    assert service.journal.read(REQUEST_ID) is None


def test_a_derived_alias_sent_with_the_wrong_identity_is_refused_with_zero_writes(tmp_path):
    newcomer = tmp_path / "newcomer"
    newcomer.mkdir()
    service, recorder = _service(
        tmp_path, projects=_projects(str(newcomer)), principal_extra={"share_all": True}
    )

    response = service.handle_submit(
        _submit_msg(_record(dest_project="newcomer", dest_project_id="0" * 64))
    )

    assert response["ok"] is False
    assert response["error"]["code"] == proto.ERR_UNAUTHORIZED
    assert recorder.calls == []
    assert service.journal.read(REQUEST_ID) is None


def test_the_identity_is_checked_before_the_resolver_runs(tmp_path):
    """A5 ordering: a mismatched identity must not even resolve a destination."""
    newcomer = tmp_path / "newcomer"
    newcomer.mkdir()
    service, recorder = _service(
        tmp_path, projects=_projects(str(newcomer)), principal_extra={"share_all": True}
    )
    resolved: list[str] = []
    service._resolver = lambda provider, work_dir, session: (
        resolved.append(work_dir) or _destination(work_dir)
    )

    service.handle_submit(_submit_msg(_record(dest_project="newcomer", dest_project_id="0" * 64)))

    assert resolved == []
    assert recorder.calls == []


def test_an_explicit_alias_still_needs_no_project_identity(tmp_path):
    """The unchanged path: a configured alias behaves exactly as before."""
    service, recorder = _service(tmp_path, projects=[], principal_extra={"share_all": True})

    response = service.handle_submit(_submit_msg(_record()))

    assert response["ok"] is True, response
    assert len(recorder.calls) == 1
    assert service.journal.read(REQUEST_ID)["dest_project_derived"] is False


def test_an_ambiguous_derived_name_is_reported_as_skipped_by_list(tmp_path):
    first = tmp_path / "My_Project"
    second = tmp_path / "sub" / "my-project"
    first.mkdir()
    second.mkdir(parents=True)
    service, recorder = _service(
        tmp_path,
        projects=_projects(str(first), str(second)),
        principal_extra={"share_all": True},
    )

    listed = service.handle_list({"id": "op-list", "principal": "bob"})

    aliases = [entry["alias"] for entry in listed["remote"]["projects"]]
    assert "my-project" not in aliases
    skipped = {entry["alias"]: entry["reason"] for entry in listed["remote"].get("skipped") or []}
    assert "my-project" in skipped and "more than one project" in skipped["my-project"]
    # And submit agrees: neither ambiguous project is reachable.
    refused = service.handle_submit(_submit_msg(_record(dest_project="my-project")))
    assert refused["error"]["code"] == proto.ERR_UNAUTHORIZED
    assert recorder.calls == []


def test_the_project_set_is_re_derived_per_request_never_cached(tmp_path):
    late = tmp_path / "late"
    service, _recorder = _service(tmp_path, projects=[], principal_extra={"share_all": True})
    assert [e["alias"] for e in service.handle_list({"id": "l1", "principal": "bob"})["remote"]["projects"]] == ["named"]

    # Created after the first list: the next request must see it.
    late.mkdir()
    service._share_all_projects = lambda: _projects(str(late))  # type: ignore[assignment]
    listed = service.handle_list({"id": "l2", "principal": "bob"})
    assert "late" in [entry["alias"] for entry in listed["remote"]["projects"]]


# --------------------------------------------------------------------------- #
# A6: a malformed share_all keeps each verb's existing failure shape
# --------------------------------------------------------------------------- #


def test_a_malformed_share_all_refuses_list_and_submit_but_still_masks_status(tmp_path):
    config_path = tmp_path / "remote" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "principals": {
                    "bob": {"issuer_token": "abc1", "allowed_aliases": ["named"], "share_all": "yes"}
                },
                "aliases": {"named": {"work_dir": "/srv/named", "providers": ["claude"]}},
            }
        ),
        encoding="utf-8",
    )
    recorder = _Recorder()
    service = RemoteDeliveryService(
        registry=_Registry(_Adapter("claude"), _Adapter("codex")),
        enqueue=recorder,
        owner_daemon=str(tmp_path / "askd.json"),
        journal=RemoteJournal(tmp_path / "remote"),
        config_loader=lambda: load_config(config_path),
        resolver=lambda provider, work_dir, session: _destination(work_dir),
        sessions=lambda provider, work_dir: [],
    )

    listed = service.handle_list({"id": "op-list", "principal": "bob"})
    assert listed["ok"] is False
    assert listed["error"]["code"] == proto.ERR_CONFIG_INVALID

    submitted = service.handle_submit(_submit_msg(_record()))
    assert submitted["ok"] is False
    assert submitted["error"]["code"] == proto.ERR_CONFIG_INVALID

    # status/fetch mask as not_found, exactly as before (A6).
    for handler, verb in ((service.handle_status, "status"), (service.handle_fetch, "fetch")):
        masked = handler({"id": f"op-{verb}", "principal": "bob", "request_id": REQUEST_ID})
        assert masked["ok"] is False
        assert masked["error"]["code"] == proto.ERR_NOT_FOUND

    assert recorder.calls == []


# --------------------------------------------------------------------------- #
# A1/A3: the pairing surface
# --------------------------------------------------------------------------- #


def _pairing_home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = tmp_path / "gate"
    gate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gate.chmod(0o755)
    return home, gate


def _inspect(home: Path, gate: Path) -> dict:
    return remote_pairing.inspect_destination(
        home=home,
        gate_path=gate,
        host_key_dir=home / "hostkeys",
        registry_dir=home / ".ccb" / "run",
    )


PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIplaceholderplaceholderplaceholder1"


def test_inspect_advertises_share_all_and_what_it_would_cover(tmp_path):
    home, gate = _pairing_home(tmp_path)
    project = _make_project(_project_tree(tmp_path), "demo")
    _write_registry(home / ".ccb" / "run", [_registry_record(project)])

    # `inspect` derives under the real temp root; point it at an empty one so
    # this project is provably eligible.
    result = remote_pairing.inspect_destination(
        home=home,
        gate_path=gate,
        host_key_dir=home / "hostkeys",
        registry_dir=home / ".ccb" / "run",
        temp_dir=tmp_path / "empty-temp",
    )

    assert result["share_all_capability"] is True
    assert [entry["work_dir"] for entry in result["share_all_projects"]] == [
        str(project.resolve())
    ]


def test_apply_grants_share_all_with_zero_alias_picks(tmp_path):
    """A3: zero picks + share_all is a complete answer, not an empty one."""
    home, gate = _pairing_home(tmp_path)
    hashed = _inspect(home, gate)["state_hash"]

    applied = remote_pairing.apply_destination(
        {
            "verb": "apply",
            "principal": "laptop-a",
            "pubkey": PUBKEY,
            "issuer_token": "alice1",
            "aliases": [],
            "share_all": True,
            "expected_state_hash": hashed,
        },
        home=home,
        gate_path=gate,
    )

    assert applied["ok"] is True
    assert applied["share_all"] is True
    document = json.loads((home / ".ccb" / "remote" / "config.json").read_text())
    assert document["principals"]["laptop-a"]["share_all"] is True
    assert "aliases" not in document or document["aliases"] == {}


def test_apply_still_refuses_zero_alias_picks_without_a_share_all_decision(tmp_path):
    """The unchanged refusal for every existing caller."""
    home, gate = _pairing_home(tmp_path)
    hashed = _inspect(home, gate)["state_hash"]

    with pytest.raises(remote_pairing.PairingError) as raised:
        remote_pairing.apply_destination(
            {
                "verb": "apply",
                "principal": "laptop-a",
                "pubkey": PUBKEY,
                "issuer_token": "alice1",
                "aliases": [],
                "expected_state_hash": hashed,
            },
            home=home,
            gate_path=gate,
        )

    assert "at least one alias" in str(raised.value)
    assert not (home / ".ccb" / "remote" / "config.json").exists()


def test_apply_revokes_share_all_with_zero_picks_and_keeps_explicit_aliases(tmp_path):
    home, gate = _pairing_home(tmp_path)
    hashed = _inspect(home, gate)["state_hash"]
    request = {
        "verb": "apply",
        "principal": "laptop-a",
        "pubkey": PUBKEY,
        "issuer_token": "alice1",
        "aliases": [{"alias": "demo", "work_dir": "/srv/demo", "providers": ["claude"]}],
        "share_all": True,
        "expected_state_hash": hashed,
    }
    first = remote_pairing.apply_destination(request, home=home, gate_path=gate)
    assert first["share_all"] is True

    revoked = remote_pairing.apply_destination(
        {**request, "aliases": [], "share_all": False, "expected_state_hash": first["state_hash"]},
        home=home,
        gate_path=gate,
    )

    document = json.loads((home / ".ccb" / "remote" / "config.json").read_text())
    assert revoked["share_all"] is False
    assert "share_all" not in document["principals"]["laptop-a"]
    # Explicit shares survive a revocation.
    assert document["principals"]["laptop-a"]["allowed_aliases"] == ["demo"]
    assert "demo" in document["aliases"]


def test_apply_leaves_the_grant_untouched_when_share_all_is_omitted(tmp_path):
    home, gate = _pairing_home(tmp_path)
    hashed = _inspect(home, gate)["state_hash"]
    request = {
        "verb": "apply",
        "principal": "laptop-a",
        "pubkey": PUBKEY,
        "issuer_token": "alice1",
        "aliases": [{"alias": "demo", "work_dir": "/srv/demo", "providers": ["claude"]}],
        "share_all": True,
        "expected_state_hash": hashed,
    }
    first = remote_pairing.apply_destination(request, home=home, gate_path=gate)

    # A re-apply that says nothing about share_all must not turn it off.
    again = remote_pairing.apply_destination(
        {
            "verb": "apply",
            "principal": "laptop-a",
            "pubkey": PUBKEY,
            "issuer_token": "alice1",
            "aliases": [{"alias": "demo", "work_dir": "/srv/demo", "providers": ["claude"]}],
            "expected_state_hash": first["state_hash"],
        },
        home=home,
        gate_path=gate,
    )

    document = json.loads((home / ".ccb" / "remote" / "config.json").read_text())
    assert document["principals"]["laptop-a"]["share_all"] is True
    assert again["share_all_changed"] is False


@pytest.mark.parametrize("value", ["yes", 1, None, [], "true"])
def test_apply_refuses_a_malformed_share_all_before_writing_anything(tmp_path, value):
    home, gate = _pairing_home(tmp_path)
    hashed = _inspect(home, gate)["state_hash"]

    with pytest.raises(remote_pairing.PairingError) as raised:
        remote_pairing.apply_destination(
            {
                "verb": "apply",
                "principal": "laptop-a",
                "pubkey": PUBKEY,
                "issuer_token": "alice1",
                "aliases": [{"alias": "demo", "work_dir": "/srv/demo", "providers": ["claude"]}],
                "share_all": value,
                "expected_state_hash": hashed,
            },
            home=home,
            gate_path=gate,
        )

    assert "share_all must be true or false" in str(raised.value)
    assert raised.value.refused is True
    assert not (home / ".ccb" / "remote" / "config.json").exists()
    assert not (home / ".ssh" / "authorized_keys").exists()


# --------------------------------------------------------------------------- #
# A5/A2 wire shape
# --------------------------------------------------------------------------- #


def test_the_project_identity_travels_and_is_covered_by_the_digest():
    project_id = "a" * 64
    record = proto.build_request_record(
        dest_project="demo",
        dest_provider="claude",
        intent="notify",
        text="hello",
        request_id=REQUEST_ID,
        dest_project_id=project_id,
    )
    assert record["dest_project_id"] == project_id

    # Tampering with it invalidates the digest.
    tampered = dict(record, dest_project_id="b" * 64)
    with pytest.raises(proto.ProtocolError) as raised:
        proto.verify_digest(tampered)
    assert raised.value.code == proto.ERR_DIGEST_MISMATCH


@pytest.mark.parametrize("value", ["not-a-hash", "A" * 64, "", 5, ["a" * 64]])
def test_a_malformed_project_identity_is_refused(value):
    with pytest.raises(proto.ProtocolError) as raised:
        proto.build_request_record(
            dest_project="demo",
            dest_provider="claude",
            intent="notify",
            text="hello",
            request_id=REQUEST_ID,
            dest_project_id=value,
        )
    assert raised.value.code == proto.ERR_INVALID_REQUEST


def test_a_record_without_the_identity_keeps_its_exact_old_digest():
    """The unchanged wire: an explicit-alias record must hash exactly as before."""
    kwargs = {
        "dest_project": "demo",
        "dest_provider": "claude",
        "intent": "notify",
        "text": "hello",
        "request_id": REQUEST_ID,
    }
    current = proto.build_request_record(**kwargs)
    manual = proto.compute_digest(
        {
            "schema_version": proto.SCHEMA_VERSION,
            "dest_project": "demo",
            "dest_provider": "claude",
            "text": "hello",
            "reply_expected": False,
            "intent": "notify",
            "request_id": REQUEST_ID,
        }
    )
    assert current["digest"] == manual
    assert "dest_project_id" not in current


def test_hello_advertises_share_all():
    assert proto.capabilities()["share_all"] is True


# --------------------------------------------------------------------------- #
# The REAL sender: `remote_client.submit` against the REAL destination handlers
# --------------------------------------------------------------------------- #
#
# The tests above build records by hand, so they cannot catch a sender that
# never sends `dest_project_id`. These drive the actual code a user runs:
# `remote_client.submit` on A, `handle_list`/`handle_submit` on B, over one
# in-memory pipe. A hand-built record that omits the identity is the negative
# control: it must be refused, or the assertion below would prove nothing.


class _Pipe:
    """One `ssh` call: the sender's frames in, the destination's frames out.

    `deliver` receives the body of each request frame and returns the reply
    frame, so the destination side is the real `RemoteDeliveryService`.
    """

    def __init__(self, deliver, *, refuse_identity_first: bool = False):
        self.deliver = deliver
        self.requests: list[dict] = []
        self.bodies: list[dict] = []
        self.responses: list[dict] = []
        self.refuse_identity_first = refuse_identity_first
        self._pending: list[dict] = []
        self.closed = False

    # -- ssh session surface --------------------------------------------- #
    def write_all(self, payload: bytes, timeout_s: float | None = None) -> int:
        # `encode_frame` prefixes a 4-byte length, so the frame is read back
        # with the protocol's own reader rather than parsed as bare JSON.
        frame = proto.read_frame(io.BytesIO(payload))
        assert isinstance(frame, dict)
        self.requests.append(frame)
        if frame.get("op") == "hello":
            self._pending.append(
                {
                    "ok": True,
                    "op": "hello",
                    "op_id": frame.get("op_id"),
                    "capabilities": proto.capabilities(),
                }
            )
        else:
            body = frame.get("body") or {}
            self.bodies.append(body)
            self._pending.append(self.deliver(frame.get("op"), frame.get("op_id"), body))
        return len(payload)

    def read_frame(self, timeout_s: float | None = None) -> dict:
        return self._pending.pop(0)

    def close(self) -> None:
        self.closed = True


def _sender_env(tmp_path: Path, projects: list[dict], *, share_all: bool = True):
    """A sender-side hosts.json + cache, and the REAL destination service."""
    identity = tmp_path / "id_ed25519"
    identity.write_text("private", encoding="utf-8")
    known = tmp_path / "known_hosts"
    known.write_text("example.test ssh-ed25519 AAAA\n", encoding="utf-8")
    hosts_path = Path.home() / ".ccb" / "remote" / "hosts.json"
    hosts_path.parent.mkdir(parents=True, exist_ok=True)
    hosts_path.write_text(
        json.dumps(
            {
                "hosts": {
                    "server": {
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
    config_path = tmp_path / "dest" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "principals": {"laptop-a": {"issuer_token": "abc1", "share_all": share_all}},
                "aliases": {},
            }
        ),
        encoding="utf-8",
    )
    service = RemoteDeliveryService(
        registry=_Registry(_Adapter("claude")),
        enqueue=_Recorder(),
        owner_daemon=str(tmp_path / "askd.json"),
        journal=RemoteJournal(tmp_path / "dest"),
        config_loader=lambda: load_config(config_path),
        resolver=lambda provider, work_dir, session: _destination(work_dir),
        sessions=lambda provider, work_dir: [],
    )
    service._share_all_projects = lambda: list(projects)  # type: ignore[assignment]
    return service


def _deliver_with(service: RemoteDeliveryService, *, traps: list[str] | None = None):
    """A `_Pipe.deliver` that answers from the REAL handlers, recording bypasses."""
    traps = traps if traps is not None else []

    def deliver(op: str, op_id, body: dict) -> dict:
        if op == "list":
            response = service.handle_list({"id": op_id, "principal": "laptop-a"})
        else:
            response = service.handle_submit(
                {"id": op_id, "principal": "laptop-a", "record": body}
            )
        if response.get("ok") is False:
            error = response.get("error") or {}
            if "did not match the project identity" in str(error.get("message") or ""):
                traps.append("identity-mismatch")
        # Mirror `bin/ccb-remote-gate.dispatch_remote_operation`: it flattens the
        # service's `remote` payload into the top-level frame, which is the shape
        # the sender's acceptance checks read.
        frame = dict(response)
        remote = frame.pop("remote", None)
        if isinstance(remote, dict):
            frame.update(remote)
        frame["op"] = op
        frame["op_id"] = op_id
        return frame

    return deliver


def _cache_observation(projects: list[dict]) -> None:
    """Write the discovery cache exactly as `ccb-remote list` would."""
    host = remote_client.load_hosts()["server"]
    assert remote_discovery.write_success(host, projects) is True


def test_the_real_sender_delivers_to_a_derived_alias_from_the_cached_list(tmp_path):
    """End-to-end: cache the list, then submit through the real sender."""
    project = tmp_path / "proj" / "Real_Project"
    project.mkdir(parents=True)
    project_id = compute_ccb_project_id(project)
    service = _sender_env(
        tmp_path, [{"work_dir": str(project), "project_id": project_id}]
    )

    # `list` first, exactly as a user runs it: this is what populates the cache.
    pipe = _Pipe(_deliver_with(service))
    listed = remote_client.list_projects(
        "server", session_factory=lambda _argv: pipe, out=io.StringIO(), err=io.StringIO()
    )
    assert listed.exit_code == remote_client.EXIT_ACCEPTED
    derived = [entry for entry in listed.projects if entry.get("derived") is True]
    assert derived and derived[0]["alias"] == "real-project"
    # And the cache therefore knows the identity, with no extra round trip.
    assert (
        remote_client.cached_project_identity("server", "real-project")
        == derived[0]["project_id"]
    )

    # Now the real sender, with NO hand-built record.
    submit_pipe = _Pipe(_deliver_with(service))
    result = remote_client.submit(
        "server",
        "real-project",
        "claude",
        "hello from the real sender",
        session_factory=lambda _argv: submit_pipe,
        out=io.StringIO(),
        err=io.StringIO(),
    )

    assert result.exit_code == remote_client.EXIT_ACCEPTED, result.reason
    assert result.state == remote_client.STATE_ACCEPTED
    # The bytes that actually went out carried the identity.
    assert submit_pipe.bodies[0]["dest_project_id"] == derived[0]["project_id"]
    stored = service.journal.read(result.request_id)
    assert stored is not None and stored["dest_project"] == "real-project"


def test_an_observed_alias_that_now_names_another_project_is_refused_not_retargeted(tmp_path):
    """P1-a: an alias the sender HAS listed may never be silently re-pointed.

    The cache says X; the destination says Y. Automatically resending with Y
    would deliver the message to a project the user never chose, so the send
    must stop and make them list again.
    """
    project = tmp_path / "proj" / "Real_Project"
    project.mkdir(parents=True)
    real_id = compute_ccb_project_id(project)
    stale_id = "0" * 64
    service = _sender_env(
        tmp_path, [{"work_dir": str(project), "project_id": real_id}]
    )
    # Observed earlier, under an identity the destination no longer agrees with.
    _cache_observation(
        [{"alias": "real-project", "derived": True, "project_id": stale_id,
          "providers": [{"provider": "claude", "present": True}]}]
    )

    traps: list[str] = []
    pipe = _Pipe(_deliver_with(service, traps=traps))
    result = remote_client.submit(
        "server",
        "real-project",
        "claude",
        "this must not be retargeted",
        session_factory=lambda _argv: pipe,
        out=io.StringIO(),
        err=io.StringIO(),
    )

    assert result.exit_code == remote_client.EXIT_NOT_DELIVERED
    assert result.state == remote_client.STATE_NOT_DELIVERED
    assert "now points at a different project than when you last listed it" in result.reason
    assert "run `ccb-remote list server` and resend" in result.reason
    # Exactly ONE submit was written: no automatic resend, no list refresh.
    assert len(pipe.bodies) == 1
    assert not [op for op in pipe.requests if op.get("op") == "list"]
    # And nothing was accepted on the destination.
    assert service.journal.read(result.request_id) is None


def test_a_never_observed_alias_is_resolved_by_one_fresh_list_and_one_resend(tmp_path):
    """P1-a: the automatic repair is only for an alias never seen before."""
    project = tmp_path / "proj" / "Real_Project"
    project.mkdir(parents=True)
    real_id = compute_ccb_project_id(project)
    service = _sender_env(
        tmp_path, [{"work_dir": str(project), "project_id": real_id}]
    )
    # No cache at all: this alias has never been listed on this machine.
    assert remote_client.cached_alias("server", "real-project").observed is False

    pipe = _Pipe(_deliver_with(service))
    result = remote_client.submit(
        "server",
        "real-project",
        "claude",
        "first ever send",
        session_factory=lambda _argv: pipe,
        out=io.StringIO(),
        err=io.StringIO(),
    )

    assert result.exit_code == remote_client.EXIT_ACCEPTED, result.reason
    # One fresh list was taken, and the second submit carried the real id.
    assert [op for op in pipe.requests if op.get("op") == "list"]
    submits = [op for op in pipe.requests if op.get("op") == "submit"]
    assert len(submits) == 2
    assert submits[-1]["body"]["dest_project_id"] == real_id


def test_the_real_sender_never_resends_after_an_acceptance_unknown(tmp_path):
    """The retry is bounded to a refusal. An unknown outcome is never resent."""
    project = tmp_path / "proj" / "Real_Project"
    project.mkdir(parents=True)
    project_id = compute_ccb_project_id(project)
    service = _sender_env(
        tmp_path, [{"work_dir": str(project), "project_id": project_id}]
    )
    _cache_observation(
        [{"alias": "real-project", "derived": True, "project_id": "0" * 64,
          "providers": [{"provider": "claude", "present": True}]}]
    )

    class _Silent(_Pipe):
        """The submit is fully written, and then no acknowledgement ever comes.

        The raise is on the READ, not the write: the bytes demonstrably reached
        the destination, which is exactly the acceptance-unknown case that must
        never be resent.
        """

        def write_all(self, payload, timeout_s=None):
            frame = proto.read_frame(io.BytesIO(payload))
            self.requests.append(frame)
            if frame.get("op") == "hello":
                self._pending.append(
                    {
                        "ok": True,
                        "op": "hello",
                        "op_id": frame.get("op_id"),
                        "capabilities": proto.capabilities(),
                    }
                )
            else:
                self.bodies.append(frame.get("body") or {})
                self._pending.append(remote_client.SessionError(
                    "timeout", "no answer from the destination"
                ))
            return len(payload)

        def read_frame(self, timeout_s=None):
            item = self._pending.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    pipe = _Silent(_deliver_with(service))
    result = remote_client.submit(
        "server",
        "real-project",
        "claude",
        "hello into the void",
        session_factory=lambda _argv: pipe,
        out=io.StringIO(),
        err=io.StringIO(),
    )

    assert result.exit_code == remote_client.EXIT_ACCEPTANCE_UNKNOWN
    # ONE submit reached the wire: no list refresh, no resend.
    assert len([op for op in pipe.requests if op.get("op") == "submit"]) == 1
    assert not [op for op in pipe.requests if op.get("op") == "list"]


def test_the_real_sender_leaves_an_explicit_aliases_record_unchanged(tmp_path):
    """Without share_all the record carries no identity, and still delivers."""
    project = tmp_path / "proj" / "named"
    project.mkdir(parents=True)
    service = _sender_env(tmp_path, [], share_all=False)
    # An explicitly configured alias needs no derived identity at all.
    config_path = tmp_path / "dest" / "config.json"
    document = json.loads(config_path.read_text())
    document["principals"]["laptop-a"]["allowed_aliases"] = ["named"]
    document["aliases"] = {"named": {"work_dir": str(project), "providers": ["claude"]}}
    config_path.write_text(json.dumps(document), encoding="utf-8")

    pipe = _Pipe(_deliver_with(service))
    result = remote_client.submit(
        "server",
        "named",
        "claude",
        "explicit target",
        session_factory=lambda _argv: pipe,
        out=io.StringIO(),
        err=io.StringIO(),
    )

    assert result.exit_code == remote_client.EXIT_ACCEPTED, result.reason
    # Byte-identical: no identity field on the wire for a configured alias.
    assert "dest_project_id" not in pipe.bodies[0]


# --------------------------------------------------------------------------- #
# Fix 2 — ratifier findings
# --------------------------------------------------------------------------- #


def test_an_explicit_alias_rejects_an_identity_for_a_different_project(tmp_path):
    """P1-a: an identity minted for a derived alias cannot be re-aimed.

    When the alias becomes explicit (or is pointed somewhere else), a record
    still carrying the OLD identity must be refused rather than delivered to
    whatever the alias names today.
    """
    derived_project = tmp_path / "proj" / "Real_Project"
    derived_project.mkdir(parents=True)
    other_project = tmp_path / "proj" / "elsewhere"
    other_project.mkdir(parents=True)
    # The explicit alias points at a DIFFERENT directory than the identity.
    service, recorder = _service(
        tmp_path, projects=[], principal_extra={"share_all": False}
    )
    config_path = tmp_path / "remote" / "config.json"
    document = json.loads(config_path.read_text())
    document["principals"]["bob"]["allowed_aliases"] = ["named"]
    document["aliases"] = {
        "named": {"work_dir": str(other_project), "providers": ["claude"]}
    }
    config_path.write_text(json.dumps(document), encoding="utf-8")

    response = service.handle_submit(
        _submit_msg(
            _record(
                dest_project="named",
                dest_project_id=compute_ccb_project_id(derived_project),
            )
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == proto.ERR_UNAUTHORIZED
    assert "did not match the project identity" in response["error"]["message"]
    assert recorder.calls == []
    assert service.journal.read(REQUEST_ID) is None


def test_an_explicit_alias_accepts_its_own_identity(tmp_path):
    """P1-a positive control: the matching identity is accepted."""
    project = tmp_path / "proj" / "named"
    project.mkdir(parents=True)
    project_id = compute_ccb_project_id(project)
    service, recorder = _service(tmp_path, projects=[], principal_extra={"share_all": False})
    config_path = tmp_path / "remote" / "config.json"
    document = json.loads(config_path.read_text())
    document["principals"]["bob"]["allowed_aliases"] = ["named"]
    document["aliases"] = {"named": {"work_dir": str(project), "providers": ["claude"]}}
    config_path.write_text(json.dumps(document), encoding="utf-8")

    response = service.handle_submit(
        _submit_msg(_record(dest_project="named", dest_project_id=project_id))
    )

    assert response["ok"] is True, response
    assert len(recorder.calls) == 1


def test_an_explicit_alias_shadowing_a_derived_one_checks_the_identity(tmp_path):
    """P1-a: the explicit alias wins the name, and owns the identity check."""
    explicit_dir = tmp_path / "proj" / "kept"
    explicit_dir.mkdir(parents=True)
    derived_dir = tmp_path / "proj" / "my-project"
    derived_dir.mkdir(parents=True)
    service, recorder = _service(
        tmp_path,
        projects=[{"work_dir": str(derived_dir), "project_id": compute_ccb_project_id(derived_dir)}],
        principal_extra={"share_all": True},
    )
    config_path = tmp_path / "remote" / "config.json"
    document = json.loads(config_path.read_text())
    document["principals"]["bob"]["allowed_aliases"] = ["my-project"]
    document["aliases"] = {
        "my-project": {"work_dir": str(explicit_dir), "providers": ["claude"]}
    }
    config_path.write_text(json.dumps(document), encoding="utf-8")

    listed = service.handle_list({"id": "op-list", "principal": "bob"})
    # The explicit alias shadows the derived one, so it is NOT advertised as
    # derived and carries no derived identity.
    entry = listed["remote"]["projects"][0]
    assert entry["alias"] == "my-project"
    assert "derived" not in entry

    # An identity belonging to the SHADOWED project is refused.
    refused = service.handle_submit(
        _submit_msg(
            _record(
                dest_project="my-project",
                dest_project_id=compute_ccb_project_id(derived_dir),
            )
        )
    )
    assert refused["ok"] is False
    assert refused["error"]["code"] == proto.ERR_UNAUTHORIZED
    assert recorder.calls == []

    # The explicit alias's own identity is accepted.
    accepted = service.handle_submit(
        _submit_msg(
            _record(
                dest_project="my-project",
                dest_project_id=compute_ccb_project_id(explicit_dir),
            )
        )
    )
    assert accepted["ok"] is True, accepted


def test_an_omitted_providers_key_keeps_the_default_and_an_empty_list_derives_nothing(tmp_path):
    """P2-b: `share_all_providers: []` means NO providers, not the default."""
    project = tmp_path / "proj" / "demo"
    project.mkdir(parents=True)
    projects = [{"work_dir": str(project), "project_id": compute_ccb_project_id(project)}]

    def load(principals: dict) -> RemoteConfig:
        path = tmp_path / f"config-{len(principals)}-{id(principals)}.json"
        path.write_text(json.dumps({"principals": principals}))
        return load_config(path)

    omitted = load({"bob": {"issuer_token": "abc1", "share_all": True}})
    empty = load(
        {"bob": {"issuer_token": "abc1", "share_all": True, "share_all_providers": []}}
    )

    # Distinguishable all the way through the loader.
    assert omitted.principal("bob").share_all_providers_given is False
    assert empty.principal("bob").share_all_providers_given is True

    derived = omitted.effective_aliases("bob", projects)
    assert derived.aliases["demo"].providers == ("claude", "codex")

    # An explicit empty list derives nothing at all: the alias is not
    # advertised, so `list` omits it and `submit` refuses it.
    none_derived = empty.effective_aliases("bob", projects)
    assert none_derived.aliases == {}
    assert none_derived.share_all is True


def test_an_empty_share_all_providers_list_makes_derived_aliases_unaddressable(tmp_path):
    """P2-b: list omits them and submit refuses them — the two agree."""
    project = tmp_path / "proj" / "demo"
    project.mkdir(parents=True)
    service, recorder = _service(
        tmp_path,
        projects=[{"work_dir": str(project), "project_id": compute_ccb_project_id(project)}],
        principal_extra={"share_all": True, "share_all_providers": []},
    )

    listed = service.handle_list({"id": "op-list", "principal": "bob"})
    assert [entry["alias"] for entry in listed["remote"]["projects"]] == ["named"]

    refused = service.handle_submit(
        _submit_msg(
            _record(
                dest_project="demo",
                dest_project_id=compute_ccb_project_id(project),
            )
        )
    )
    assert refused["ok"] is False
    assert refused["error"]["code"] == proto.ERR_UNAUTHORIZED
    assert recorder.calls == []


def test_a_failed_receipt_write_blocks_the_resend(tmp_path, monkeypatch):
    """P1-c: the refreshed digest must be recorded BEFORE the resend goes out.

    If that receipt write fails, nothing is resent: a request that never reached
    the destination must never be described by a receipt that claims it did.
    """
    project = tmp_path / "proj" / "Real_Project"
    project.mkdir(parents=True)
    real_id = compute_ccb_project_id(project)
    service = _sender_env(
        tmp_path, [{"work_dir": str(project), "project_id": real_id}]
    )
    # Never observed, so this submit is allowed to resolve it with one `list`.
    assert remote_client.cached_alias("server", "real-project").observed is False

    # R2: the refreshed digest is now written through `update_receipt` (which
    # holds the receipt lock), NOT through a bare `save_receipt`. Fail THAT
    # write, and record that the locked path is the one used.
    locked_calls = {"n": 0}
    real_update = remote_client.update_receipt

    def flaky_update(request_id, mutate):
        locked_calls["n"] += 1
        # The FIRST locked write here is the refreshed digest (the submit-time
        # receipt was written earlier by `save_receipt`). Fail only that one, so
        # the later lifecycle transition is not disturbed.
        if locked_calls["n"] == 1:
            raise OSError("disk full")
        return real_update(request_id, mutate)

    monkeypatch.setattr(remote_client, "update_receipt", flaky_update)

    pipe = _Pipe(_deliver_with(service))
    result = remote_client.submit(
        "server",
        "real-project",
        "claude",
        "must not be resent",
        session_factory=lambda _argv: pipe,
        out=io.StringIO(),
        err=io.StringIO(),
    )

    assert result.exit_code == remote_client.EXIT_NOT_DELIVERED
    assert "could not be recorded in the outbound receipt" in result.reason
    # The digest write went through the LOCKED path, not a bare save.
    assert locked_calls["n"] >= 1
    # Exactly ONE submit went out (the refused first attempt): no resend.
    submits = [op for op in pipe.requests if op.get("op") == "submit"]
    assert len(submits) == 1
    assert service.journal.read(result.request_id) is None


# --------------------------------------------------------------------------- #
# Fix 3 — ratifier re-review findings
# --------------------------------------------------------------------------- #


def test_identity_is_rechecked_against_the_resolvers_own_result(tmp_path):
    """R1: a directory swapped between the check and resolution is refused.

    The alias and the record still agree, so only a comparison against what the
    RESOLVER actually resolved can catch it — and it must refuse before any
    journal or queue write.
    """
    project = tmp_path / "proj" / "Real"
    project.mkdir(parents=True)
    project_id = compute_ccb_project_id(project)
    other = tmp_path / "other" / "Real"
    other.mkdir(parents=True)
    other_id = compute_ccb_project_id(other)

    service, recorder = _service(
        tmp_path, projects=_projects(str(project)), principal_extra={"share_all": True}
    )
    # The resolver reports a DIFFERENT project than the alias proved.
    service._resolver = lambda provider, work_dir, session: Destination(
        provider=provider,
        live_id="live-1",
        launch_id="ai-1",
        work_dir=str(other),
        pane_id="%9",
        terminal="tmux",
        pane_title_marker="CCB-claude-1",
        ccb_project_id=other_id,
    )

    response = service.handle_submit(
        _submit_msg(_record(dest_project="real", dest_project_id=project_id))
    )

    assert response["ok"] is False
    assert response["error"]["code"] == proto.ERR_UNAUTHORIZED
    assert "did not match the project identity" in response["error"]["message"]
    assert recorder.calls == [], "nothing may be queued"
    assert service.journal.read(REQUEST_ID) is None, "nothing may be journalled"


def test_an_empty_resolved_identity_fails_closed(tmp_path):
    """Ratifier amendment: a bound request whose resolver reports no identity is refused."""
    project = tmp_path / "proj" / "Real"
    project.mkdir(parents=True)
    project_id = compute_ccb_project_id(project)

    service, recorder = _service(
        tmp_path, projects=_projects(str(project)), principal_extra={"share_all": True}
    )
    service._resolver = lambda provider, work_dir, session: Destination(
        provider=provider,
        live_id="live-1",
        launch_id="ai-1",
        work_dir=str(project),
        pane_id="%9",
        terminal="tmux",
        pane_title_marker="CCB-claude-1",
        ccb_project_id="",
    )

    response = service.handle_submit(
        _submit_msg(_record(dest_project="real", dest_project_id=project_id))
    )

    assert response["ok"] is False
    assert response["error"]["code"] == proto.ERR_UNAUTHORIZED
    assert recorder.calls == [], "nothing may be queued"
    assert service.journal.read(REQUEST_ID) is None, "nothing may be journalled"


def test_the_resolvers_matching_identity_is_accepted(tmp_path):
    """R1 positive control: agreement is not a refusal."""
    project = tmp_path / "proj" / "Real"
    project.mkdir(parents=True)
    project_id = compute_ccb_project_id(project)
    service, recorder = _service(
        tmp_path, projects=_projects(str(project)), principal_extra={"share_all": True}
    )

    response = service.handle_submit(
        _submit_msg(_record(dest_project="real", dest_project_id=project_id))
    )

    assert response["ok"] is True, response
    assert len(recorder.calls) == 1


def test_a_symlink_swap_between_list_and_submit_is_refused(tmp_path):
    """R1 end-to-end shape: the listed dir becomes a symlink to a same-named one."""
    original = tmp_path / "projects" / "Real"
    original.mkdir(parents=True)
    listed_id = compute_ccb_project_id(original)
    elsewhere = tmp_path / "elsewhere" / "Real"
    elsewhere.mkdir(parents=True)
    elsewhere_id = compute_ccb_project_id(elsewhere)
    assert listed_id != elsewhere_id

    service, recorder = _service(
        tmp_path, projects=_projects(str(original)), principal_extra={"share_all": True}
    )
    service._resolver = lambda provider, work_dir, session: Destination(
        provider=provider, live_id="l", launch_id="ai", work_dir=str(elsewhere),
        pane_id="%9", terminal="tmux", pane_title_marker="m",
        ccb_project_id=elsewhere_id,
    )

    refused = service.handle_submit(
        _submit_msg(_record(dest_project="real", dest_project_id=listed_id))
    )

    assert refused["error"]["code"] == proto.ERR_UNAUTHORIZED
    assert recorder.calls == []
    assert service.journal.read(REQUEST_ID) is None


def test_the_refreshed_digest_is_written_under_the_receipt_lock(tmp_path, monkeypatch):
    """R2: the digest update goes through `update_receipt`, never a bare save.

    A bare `save_receipt` would race the daemon poller: it rewrites the whole
    record from an in-memory copy and could undo a state that writer had already
    advanced. Only the `digest` field may change, under the shared lock.
    """
    project = tmp_path / "proj" / "Real_Project"
    project.mkdir(parents=True)
    real_id = compute_ccb_project_id(project)
    service = _sender_env(
        tmp_path, [{"work_dir": str(project), "project_id": real_id}]
    )
    assert remote_client.cached_alias("server", "real-project").observed is False

    seen = []
    real_update = remote_client.update_receipt
    bare_saves = []
    real_save = remote_client.save_receipt

    def spy_update(request_id, mutate):
        before = remote_client.read_receipt(request_id)
        result = real_update(request_id, mutate)
        after = remote_client.read_receipt(request_id)
        seen.append({"before": before, "after": after})
        return result

    def spy_save(record):
        bare_saves.append(record.get("request_id"))
        return real_save(record)

    monkeypatch.setattr(remote_client, "update_receipt", spy_update)
    monkeypatch.setattr(remote_client, "save_receipt", spy_save)

    pipe = _Pipe(_deliver_with(service))
    result = remote_client.submit(
        "server",
        "real-project",
        "claude",
        "digest must land under the lock",
        session_factory=lambda _argv: pipe,
        out=io.StringIO(),
        err=io.StringIO(),
    )

    assert result.exit_code == remote_client.EXIT_ACCEPTED, result.reason
    # At least one locked update carried a digest change...
    digest_updates = [
        item for item in seen if item["before"].get("digest") != item["after"].get("digest")
    ]
    assert digest_updates, "the refreshed digest must be written through update_receipt"
    # ...and the only bare save was the INITIAL receipt, before any submit.
    assert bare_saves == [result.request_id]


def test_a_derived_alias_keeps_the_path_and_identity_from_one_resolve(tmp_path):
    """R1: the derived alias must not pair a fresh path with an old hash."""
    project = tmp_path / "projects" / "Real"
    project.mkdir(parents=True)
    project_id = compute_ccb_project_id(project)
    service, _recorder = _service(
        tmp_path,
        projects=[{"work_dir": str(project), "project_id": project_id}],
        principal_extra={"share_all": True},
    )

    listed = service.handle_list({"id": "op-list", "principal": "bob"})
    entry = next(
        item for item in listed["remote"]["projects"] if item["alias"] == "real"
    )

    # The path the alias addresses and the identity it advertises describe the
    # SAME directory.
    assert entry["project_id"] == compute_ccb_project_id(Path(entry_path := project))
    assert entry_path.is_dir()
