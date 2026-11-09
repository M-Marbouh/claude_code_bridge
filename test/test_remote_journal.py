from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import remote_journal
from remote_journal import (
    STATE_ACCEPTED,
    STATE_DISPATCHING,
    STATE_DELIVERED,
    STATE_FAILED,
    STATE_UNKNOWN,
    JournalError,
    RemoteConfigError,
    RemoteJournal,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUEST_ID = "20261109-182536-922-2626995-habc1"
OTHER_ID = "20261109-182536-922-2626995-hxyz9"


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
        },
    }


def _write_config(tmp_path: Path, document: dict | None = None) -> Path:
    root = tmp_path / "remote"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.json"
    path.write_text(json.dumps(document if document is not None else _config_document()), encoding="utf-8")
    return path


def _record(request_id: str = REQUEST_ID, **overrides) -> dict:
    record = {
        "request_id": request_id,
        "schema_version": 1,
        "digest": "a" * 64,
        "principal": "bob",
        "issuer_token": "abc1",
        "source_claims": {"host": "a"},
        "dest_project": "example-project",
        "dest_provider": "claude",
        "dest_session": "",
        "work_dir": "/tmp/example-project",
        "pane_id": "%9",
        "terminal": "tmux",
        "pane_title_marker": "CCB-claude-1",
        "ccb_project_id": "proj-1",
        "reply_expected": False,
        "intent": "notify",
        "text": "hello",
        "owner_daemon": "/tmp/askd.json",
        "owner_pid": os.getpid(),
        "state": STATE_ACCEPTED,
    }
    record.update(overrides)
    return record


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_missing_config_is_deny_all_not_an_error(tmp_path):
    config = load_config(tmp_path / "absent.json")
    assert config.principals == {} and config.aliases == {}
    assert config.may_use("bob", "example-project") is False
    assert config.allowed_aliases("bob") == ()


def test_config_loads_principals_aliases_and_acl(tmp_path):
    config = load_config(_write_config(tmp_path))
    assert config.principal("bob").issuer_token == "abc1"
    assert config.may_use("bob", "example-project") is True
    assert config.may_use("eve", "example-project") is False  # not in eve's ACL
    assert config.may_use("nobody", "example-project") is False
    assert config.may_use("ghost", "example-project") is False
    assert config.allowed_aliases("bob") == ("example-project",)
    assert config.providers_for("bob", "example-project") == ("claude", "codex")
    # An alias listed in an ACL but absent from the alias table is not offered.
    document = _config_document()
    document["principals"]["bob"]["allowed_aliases"].append("ghost-project")
    config = load_config(_write_config(tmp_path, document))
    assert config.allowed_aliases("bob") == ("example-project",)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.update({"principals": []}),
        lambda doc: doc["principals"].update({"bob": {"allowed_aliases": []}}),
        lambda doc: doc["aliases"].update({"example-project": {"providers": ["claude"]}}),
        lambda doc: doc["principals"].update({"bob": {"issuer_token": "", "allowed_aliases": []}}),
        lambda doc: doc["principals"].update({"bob": {"issuer_token": "abc1", "allowed_aliases": "all"}}),
    ],
)
def test_malformed_config_fails_closed(tmp_path, mutate):
    document = _config_document()
    mutate(document)
    with pytest.raises(RemoteConfigError):
        load_config(_write_config(tmp_path, document))


def test_unparseable_config_fails_closed(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RemoteConfigError):
        load_config(path)


# --------------------------------------------------------------------------- #
# Journal records
# --------------------------------------------------------------------------- #


def test_record_round_trip_and_history(tmp_path):
    journal = RemoteJournal(tmp_path / "remote")
    journal.create(_record())
    record = journal.read(REQUEST_ID)
    assert record["state"] == STATE_ACCEPTED
    assert record["history"][0]["state"] == STATE_ACCEPTED
    assert "at" in record["history"][0]

    journal.transition(REQUEST_ID, STATE_DISPATCHING, "about to send")
    record = journal.read(REQUEST_ID)
    assert record["state"] == STATE_DISPATCHING
    assert [entry["state"] for entry in record["history"]] == [STATE_ACCEPTED, STATE_DISPATCHING]
    path = journal.record_path(REQUEST_ID)
    assert oct(path.stat().st_mode & 0o777) == oct(0o600)
    assert oct(journal.journal_dir.stat().st_mode & 0o777) == oct(0o700)


def test_record_files_are_durable_writes_with_no_temp_left_behind(tmp_path):
    journal = RemoteJournal(tmp_path / "remote")
    journal.create(_record())
    journal.transition(REQUEST_ID, STATE_DELIVERED)
    leftovers = [p.name for p in journal.journal_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert json.loads(journal.record_path(REQUEST_ID).read_text())["state"] == STATE_DELIVERED


def test_ids_that_are_not_host_qualified_are_refused(tmp_path):
    journal = RemoteJournal(tmp_path / "remote")
    for bad in ("../../etc/passwd", "20261109-182536-922-2626995", REQUEST_ID + "!"):
        with pytest.raises(Exception):
            journal.record_path(bad)


def test_write_failure_raises_journal_error(tmp_path, monkeypatch):
    journal = RemoteJournal(tmp_path / "remote")
    journal.ensure()

    def boom(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(remote_journal.os, "replace", boom)
    with pytest.raises(JournalError):
        journal.create(_record())
    # Nothing partially written, so a reader sees no record at all.
    assert journal.read(REQUEST_ID) is None


def test_concurrent_duplicate_acceptance_across_processes(tmp_path):
    """Two processes accept the same id: exactly one may dispatch."""
    root = tmp_path / "remote"
    journal = RemoteJournal(root)
    journal.ensure()
    dispatch_log = tmp_path / "dispatched.txt"
    start_file = tmp_path / "start"

    script = tmp_path / "accept_child.py"
    script.write_text(
        f"""
import os, sys, time
sys.path.insert(0, {str(REPO_ROOT / 'lib')!r})
from remote_journal import RemoteJournal, STATE_ACCEPTED

root, request_id, dispatch_log, start_file = sys.argv[1:5]
journal = RemoteJournal(root)
while not os.path.exists(start_file):
    time.sleep(0.002)
with journal.locked(request_id):
    existing = journal.read(request_id)
    if existing is None:
        journal.create({{
            "request_id": request_id, "digest": "a" * 64, "state": STATE_ACCEPTED,
            "principal": "bob", "dest_project": "example-project", "dest_provider": "claude",
        }})
        with open(dispatch_log, "a", encoding="utf-8") as handle:
            handle.write(request_id + "\\n")
""",
        encoding="utf-8",
    )

    procs = [
        subprocess.Popen([sys.executable, str(script), str(root), REQUEST_ID, str(dispatch_log), str(start_file)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    time.sleep(0.2)
    start_file.write_text("go")
    for proc in procs:
        _out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err

    assert dispatch_log.read_text().splitlines() == [REQUEST_ID]
    assert journal.read(REQUEST_ID) is not None


def test_concurrent_readers_never_see_a_partial_record(tmp_path):
    journal = RemoteJournal(tmp_path / "remote")
    journal.create(_record())
    for _ in range(20):
        assert journal.read(REQUEST_ID)["request_id"] == REQUEST_ID


def test_per_id_locks_are_independent(tmp_path):
    journal = RemoteJournal(tmp_path / "remote")
    journal.create(_record())
    journal.create(_record(OTHER_ID))
    with journal.locked(REQUEST_ID):
        # A different id must not wait for this one.
        with journal.locked(OTHER_ID, timeout_s=0.5):
            pass


def test_same_local_id_under_two_issuer_tokens_is_two_records(tmp_path):
    journal = RemoteJournal(tmp_path / "remote")
    first = _record(REQUEST_ID)
    second = _record(OTHER_ID, issuer_token="xyz9", principal="eve")
    journal.create(first)
    journal.create(second)
    assert journal.record_path(REQUEST_ID) != journal.record_path(OTHER_ID)
    assert len(journal.iter_records()) == 2


def test_transition_to_an_unknown_state_is_refused(tmp_path):
    journal = RemoteJournal(tmp_path / "remote")
    journal.create(_record())
    with pytest.raises(JournalError):
        journal.transition(REQUEST_ID, "not-a-state")
    with pytest.raises(JournalError):
        journal.transition(OTHER_ID, STATE_FAILED)  # no such record
