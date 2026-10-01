from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import hermes_comm
from hermes_comm import HermesStateError, _SUMMARY_END_MARKER, hermes_home_from_env, read_exchange, session_db_path
from askd.adapters.base import ProviderRequest, QueuedTask
import askd.adapters.hermes as hermes_adapter_module
from ccb_protocol import append_trailing_notice, split_done_text

_check_pane = hermes_adapter_module._check_pane


def _db(path: Path, *, version: int = 30) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    schema_31 = """
        , auto_archived INTEGER DEFAULT 0
    """ if version >= 31 else ""
    message_identity_columns = """
            , message_uid TEXT, absorbed_message_uids TEXT, tool_call_uids TEXT, tool_call_uid TEXT
    """ if version >= 31 else ""
    conn.executescript(
        f"""
        CREATE TABLE schema_version(version INTEGER NOT NULL);
        CREATE TABLE sessions(
            id TEXT PRIMARY KEY, cwd TEXT, ended_at TEXT, end_reason TEXT, parent_session_id TEXT
            {schema_31}
        );
        CREATE TABLE messages(
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
            tool_calls TEXT, tool_name TEXT, timestamp TEXT, finish_reason TEXT, active INTEGER,
            compacted INTEGER, display_kind TEXT, display_metadata TEXT, display_order INTEGER,
            _compressed_summary INTEGER DEFAULT 0
            {message_identity_columns}
        );
        """
    )
    conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
    return conn


def _session(conn, session_id: str, cwd: Path, *, end_reason: str = "", parent: str = "") -> None:
    conn.execute(
        "INSERT INTO sessions(id,cwd,ended_at,end_reason,parent_session_id) VALUES (?,?,NULL,?,?)",
        (session_id, str(cwd), end_reason, parent),
    )


def _message(
    conn, session_id: str, role: str, content: str, *, timestamp: str, order: int,
    finish: str = "", tool_calls: str = "", active: int = 1, compacted: int = 0,
    display_kind: str = "", metadata: str = "",
    compressed_summary: bool = False,
) -> int:
    cur = conn.execute(
        """INSERT INTO messages(
            session_id,role,content,tool_call_id,tool_calls,tool_name,timestamp,finish_reason,
            active,compacted,display_kind,display_metadata,display_order,_compressed_summary
        ) VALUES (?,?,?,NULL,?,'',?,?,?,?,?,?,?,?)""",
        (session_id, role, content, tool_calls, timestamp, finish, active, compacted,
         display_kind, metadata, order, int(compressed_summary)),
    )
    return int(cur.lastrowid)


def _anchor(conn, session_id: str, req_id: str, *, timestamp: str = "t1", order: int = 1, content: str | None = None) -> int:
    return _message(
        conn, session_id, "user", content or f"CCB_REQ_ID: {req_id}\nDo the small task",
        timestamp=timestamp, order=order,
    )


def _done(
    conn, session_id: str, req_id: str, *, text: str | None = None, finish: str = "stop",
    tool_calls: str = "", order: int = 2, active: int = 1, compacted: int = 0, metadata: str = "",
) -> int:
    return _message(
        conn, session_id, "assistant", text or f"Answer\nCCB_DONE: {req_id}",
        timestamp=f"t{order}", order=order, finish=finish, tool_calls=tool_calls,
        active=active, compacted=compacted, metadata=metadata,
    )


def test_read_exchange_requires_exact_whole_line_anchor_and_done_id(tmp_path: Path) -> None:
    req_id = "20260930-232849-001-1"
    conn = _db(tmp_path / "state.db")
    _session(conn, "s1", tmp_path)
    _message(conn, "s1", "user", f"prefix CCB_REQ_ID: {req_id}\nBody", timestamp="t0", order=1)
    conn.commit()
    conn.close()
    db = tmp_path / "state.db"

    assert read_exchange(db, tmp_path, req_id) is None  # negative: embedded marker is not an anchor

    conn = sqlite3.connect(db)
    _anchor(conn, "s1", req_id)
    _done(conn, "s1", "different-id", order=3)
    conn.commit()
    conn.close()
    exchange = read_exchange(db, tmp_path, req_id)
    assert exchange is not None
    assert exchange.reply(req_id) is None  # negative: a DONE for another request cannot complete this one


@pytest.mark.parametrize(
    ("reply_text", "finish", "tool_calls"),
    [
        ("Interim\nCCB_DONE: req", "", ""),
        ("Partial\nCCB_DONE: req", "length", ""),
        ("Tool result\nCCB_DONE: req", "stop", '[{"id":"call"}]'),
        ("Wrong request\nCCB_DONE: other", "stop", ""),
        ("No marker", "stop", ""),
    ],
)
def test_nonterminal_partial_tool_and_wrong_id_rows_never_complete(tmp_path: Path, reply_text, finish, tool_calls) -> None:
    conn = _db(tmp_path / "state.db")
    _session(conn, "s1", tmp_path)
    _anchor(conn, "s1", "req")
    _done(conn, "s1", "req", text=reply_text, finish=finish, tool_calls=tool_calls)
    conn.commit()
    conn.close()
    exchange = read_exchange(tmp_path / "state.db", tmp_path, "req")
    assert exchange is not None
    assert exchange.reply("req") is None  # negative: each non-terminal shape fails its completion check


def test_request_id_completion_crosses_later_user_rows(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "target", tmp_path)
    _session(conn, "other", tmp_path)
    _anchor(conn, "target", "req")
    _message(conn, "target", "assistant", "working", timestamp="t2", order=2, finish="")
    _message(conn, "target", "user", "later real user turn", timestamp="t3", order=3)
    _done(conn, "target", "req", text="Correlated answer\nCCB_DONE: req", order=4)
    _done(conn, "other", "elsewhere", text="Wrong session\nCCB_DONE: req", order=2)
    conn.commit()
    conn.close()

    exchange = read_exchange(db, tmp_path, "req")
    assert exchange is not None
    assert exchange.reply("req") == "Correlated answer"


def test_same_request_anchor_in_unrelated_sessions_is_ambiguous(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "first", tmp_path)
    _session(conn, "second", tmp_path)
    _anchor(conn, "first", "duplicate")
    _anchor(conn, "second", "duplicate")
    conn.commit()
    conn.close()

    with pytest.raises(HermesStateError, match="Ambiguous"):
        read_exchange(db, tmp_path, "duplicate")


def test_summary_only_carrier_does_not_hide_request_id_completion(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "s1", tmp_path)
    _anchor(conn, "s1", "old")
    _message(conn, "s1", "assistant", "old request still working", timestamp="t2", order=2)
    carrier = "[CONTEXT SUMMARY]: copied CCB_REQ_ID: synthetic\nOlder request data"
    _message(
        conn, "s1", "user", carrier, timestamp="t3", order=3, display_kind="hidden",
        compressed_summary=True,
    )
    _done(conn, "s1", "old", text="The old request answer\nCCB_DONE: old", order=4)
    conn.commit()
    conn.close()

    exchange = read_exchange(db, tmp_path, "old")
    assert exchange is not None
    assert [row["role"] for row in exchange.rows] == ["assistant", "user", "assistant"]
    assert exchange.reply("old") == "The old request answer"


def test_hidden_compaction_summary_request_id_is_not_an_anchor(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "s1", tmp_path)
    carrier = "[CONTEXT SUMMARY]: copied old request\nCCB_REQ_ID: synthetic\nOld request details"
    _message(
        conn, "s1", "user", carrier, timestamp="t1", order=1, display_kind="hidden",
        compressed_summary=True,
    )
    _done(conn, "s1", "synthetic")
    conn.commit()
    conn.close()

    assert read_exchange(db, tmp_path, "synthetic") is None


_CARRIER_CASES = [
    (
        "string",
        f"[CONTEXT SUMMARY]: archived request\n{_SUMMARY_END_MARKER}\nCCB_REQ_ID: live\nAsk",
        False,
        "CCB_REQ_ID: live\nAsk",
        "",
    ),
    (
        "json-prefixed",
        "\x00json:" + json.dumps(
            f"[CONTEXT SUMMARY]: archived request\n{_SUMMARY_END_MARKER}\nCCB_REQ_ID: live\nAsk"
        ),
        False,
        "CCB_REQ_ID: live\nAsk",
        "",
    ),
    (
        "multipart-summary-and-live",
        [
            {"type": "text", "text": "[CONTEXT SUMMARY]: copied CCB_REQ_ID: stale"},
            {"type": "text", "text": f"{_SUMMARY_END_MARKER}\nCCB_REQ_ID: live"},
            {"type": "image", "url": "image-data"},
        ],
        False,
        [{"type": "text", "text": "CCB_REQ_ID: live"}, {"type": "image", "url": "image-data"}],
        "",
    ),
    (
        "summary-only",
        "[CONTEXT SUMMARY]: copied old request\nCCB_REQ_ID: stale",
        False,
        None,
        "",
    ),
    (
        "hidden-with-live",
        f"[CONTEXT SUMMARY]: archived request\n{_SUMMARY_END_MARKER}\nCCB_REQ_ID: live\nAsk",
        False,
        "CCB_REQ_ID: live\nAsk",
        "hidden",
    ),
    (
        "end-marker-only-summary",
        f"archived request\nCCB_REQ_ID: stale\n{_SUMMARY_END_MARKER}",
        True,
        None,
        "hidden",
    ),
    ("plain-user", "CCB_REQ_ID: live\nAsk", False, "CCB_REQ_ID: live\nAsk", ""),
]


@pytest.mark.parametrize(
    ("_name", "content", "is_summary", "expected", "_display_kind"),
    _CARRIER_CASES,
    ids=[case[0] for case in _CARRIER_CASES],
)
def test_live_user_projection_hard_coded_carrier_expectations(
    _name: str, content, is_summary: bool, expected, _display_kind: str,
) -> None:
    assert hermes_comm._live_user_projection(content, is_summary=is_summary) == expected


def test_live_user_projection_matches_hermes_context_compressor() -> None:
    hermes_root = Path.home() / ".hermes" / "hermes-agent"
    if not hermes_root.is_dir():
        pytest.skip("Hermes source tree is not installed")
    sys.path.insert(0, str(hermes_root))
    try:
        from agent.context_compressor import ContextCompressor
    except Exception as exc:
        pytest.skip(f"Hermes ContextCompressor is not importable: {exc}")

    for name, content, is_summary, _expected, display_kind in _CARRIER_CASES:
        hermes_content = content
        if isinstance(content, str) and content.startswith("\x00json:"):
            hermes_content = json.loads(content[len("\x00json:"):])
        message = {"role": "user", "content": hermes_content, "display_kind": display_kind}
        if is_summary:
            message["_compressed_summary"] = True
        hermes_projection = ContextCompressor._strip_context_summary_handoff_message(message)
        expected = hermes_projection.get("content") if hermes_projection is not None else None
        assert hermes_comm._live_user_projection(content, is_summary=is_summary) == expected, name


def test_compacted_archived_anchor_remains_retrievable(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "s1", tmp_path)
    _message(conn, "s1", "user", "CCB_REQ_ID: archived\nOld task", timestamp="old", order=1, active=0, compacted=1)
    _done(conn, "s1", "archived", active=0, compacted=1)
    conn.commit()
    conn.close()

    exchange = read_exchange(db, tmp_path, "archived")
    assert exchange is not None and exchange.reply("archived") == "Answer"


def test_withdrawn_terminal_row_does_not_complete(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "s1", tmp_path)
    _anchor(conn, "s1", "withdrawn")
    _done(conn, "s1", "withdrawn", active=0, compacted=0)
    conn.commit()
    conn.close()

    exchange = read_exchange(db, tmp_path, "withdrawn")
    assert exchange is not None and exchange.reply("withdrawn") is None


def test_model_only_terminal_row_does_not_complete(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "s1", tmp_path)
    _anchor(conn, "s1", "model-only")
    _done(conn, "s1", "model-only", metadata='{"model_only":true}')
    conn.commit()
    conn.close()

    exchange = read_exchange(db, tmp_path, "model-only")
    assert exchange is not None and exchange.reply("model-only") is None


def test_schema_31_uidless_compaction_replay_completes(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db, version=31)
    _session(conn, "s1", tmp_path)
    earliest = _message(
        conn, "s1", "user", "CCB_REQ_ID: replay\nDo the small task", timestamp="t1", order=1,
        active=0, compacted=1,
    )
    _anchor(conn, "s1", "replay", timestamp="t2", order=2)
    _done(conn, "s1", "replay", text="Replay answer\nCCB_DONE: replay", order=3)
    conn.commit()
    conn.close()

    exchange = read_exchange(db, tmp_path, "replay")
    assert exchange is not None and exchange.anchor_id == earliest
    assert exchange.reply("replay") == "Replay answer"


def test_json_content_anchor_and_synthetic_user_rows(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "s1", tmp_path)
    json_content = "\x00json:" + json.dumps([{"type": "text", "text": "CCB_REQ_ID: json-anchor\nBody"}])
    _message(conn, "s1", "user", json_content, timestamp="t1", order=1)
    _message(
        conn, "s1", "user", "internal notification", timestamp="t2", order=2,
        display_kind="async_delegation_complete",
    )
    _message(
        conn, "s1", "user", "model generated row", timestamp="t3", order=3,
        metadata='{"model_only":true}',
    )
    _done(conn, "s1", "json-anchor", order=4)
    conn.commit()
    conn.close()

    exchange = read_exchange(db, tmp_path, "json-anchor")
    assert exchange is not None
    assert exchange.reply("json-anchor") == "Answer"
    assert len(exchange.rows) == 3  # negative: the synthetic user row cannot truncate this exchange


def test_wrong_workdir_anchor_and_unsupported_schema_fail_closed(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "elsewhere", tmp_path / "other")
    _anchor(conn, "elsewhere", "req")
    conn.commit()
    conn.close()
    assert read_exchange(db, tmp_path, "req") is None  # negative: another cwd is outside this lookup

    bad_db = tmp_path / "old.db"
    conn = _db(bad_db, version=29)
    conn.commit()
    conn.close()
    with pytest.raises(HermesStateError, match="schema version"):
        read_exchange(bad_db, tmp_path, "req")  # negative: unsupported schema is an error, not no-reply

    newer_db = tmp_path / "newer.db"
    conn = _db(newer_db, version=32)
    conn.commit()
    conn.close()
    with pytest.raises(HermesStateError, match="schema version 32") as error:
        read_exchange(newer_db, tmp_path, "req")
    assert error.value.kind == "incompatible"


def test_queued_prompt_anchor_survives_accept_and_drain_replacement(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db, version=31)
    _session(conn, "s1", tmp_path)
    early_id = _message(
        conn, "s1", "user", "CCB_REQ_ID: queued\nDo this", timestamp="t2", order=2,
        active=0, compacted=0, metadata=json.dumps({"_queued_prompt": True}),
    )
    _done(conn, "s1", "previous", text="Previous answer\nCCB_DONE: previous", order=3)
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db)
    replacement_id = _message(
        conn, "s1", "user", "CCB_REQ_ID: queued\nDo this", timestamp="t4", order=4,
    )
    _done(conn, "s1", "queued", text="Queued answer\nCCB_DONE: queued", order=5)
    conn.commit()
    conn.close()

    drained = read_exchange(db, tmp_path, "queued")
    assert drained is not None and drained.anchor_id == early_id
    assert replacement_id in [row["id"] for row in drained.rows]
    assert drained.reply("queued") == "Queued answer"


def test_session_cwd_filter_compares_realpaths(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    recorded_cwd = tmp_path / "uncreated" / ".."
    conn = _db(db)
    _session(conn, "same-project", recorded_cwd)
    _anchor(conn, "same-project", "realpath")
    conn.commit()
    conn.close()

    exchange = read_exchange(db, tmp_path, "realpath")
    assert exchange is not None and exchange.session_id == "same-project"


def test_session_home_binding_and_exact_pend_recovery(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "hermes-home"
    assert hermes_home_from_env({"HERMES_HOME": str(home)}).resolve() == home.resolve()
    monkeypatch.setenv("HOME", str(tmp_path))
    assert hermes_home_from_env({}).resolve() == (tmp_path / ".hermes").resolve()
    assert session_db_path({"hermes_home": str(home)}) == home / "state.db"
    with pytest.raises(HermesStateError, match="does not match"):
        session_db_path({"hermes_home": str(home), "state_db_path": str(tmp_path / "other.db")})

    db = home / "state.db"
    home.mkdir()
    conn = _db(db)
    _session(conn, "bound", tmp_path)
    _session(conn, "other", tmp_path)
    _anchor(conn, "bound", "task-id")
    _done(conn, "bound", "task-id")
    _anchor(conn, "other", "other-task")
    _done(conn, "other", "other-task", text="leaked\nCCB_DONE: task-id")
    conn.commit()
    conn.close()

    source = Path(__file__).resolve().parents[1] / "bin" / "pend"
    loader = importlib.machinery.SourceFileLoader("pend_for_hermes_test", str(source))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    pend = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(pend)
    receipt = {
        "task_id": "task-id", "work_dir": str(tmp_path), "destination_database_path": str(db),
        "destination_conversation_id": "bound",
    }
    assert pend._recover_from_recorded_conversation(receipt, "hermes") == "Answer"
    assert pend._recover_from_recorded_conversation({**receipt, "destination_conversation_id": "other"}, "hermes") is None


def test_database_is_opened_read_only_and_missing_db_is_not_no_reply(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "s1", tmp_path)
    _anchor(conn, "s1", "req")
    conn.commit()
    conn.close()
    read_exchange(db, tmp_path, "req")
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(db.as_uri() + "?mode=ro", uri=True).execute("DELETE FROM messages")

    with pytest.raises(HermesStateError) as error:
        read_exchange(tmp_path / "missing.db", tmp_path, "req")
    assert error.value.kind == "unavailable"  # negative: missing DB is distinguishable from no anchor


def test_busy_database_is_retried_and_reported_as_busy(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "s1", tmp_path)
    conn.commit()
    monkeypatch.setattr(hermes_comm, "BUSY_RETRIES", 2)
    monkeypatch.setattr(hermes_comm, "BUSY_RETRY_DELAY_S", 0)
    conn.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(HermesStateError) as error:
            read_exchange(db, tmp_path, "req")
        assert error.value.kind == "busy"  # negative: contention cannot be mistaken for an absent anchor
    finally:
        conn.rollback()
        conn.close()


def _adapter_task(work_dir: Path, req_id: str, *, timeout: float = 0.05) -> QueuedTask:
    request = ProviderRequest(
        client_id="test", work_dir=str(work_dir), timeout_s=timeout, quiet=True, message="tiny",
        caller="claude", suppress_completion_hook=True, timeout_explicit=True,
    )
    return QueuedTask(request=request, created_ms=0, req_id=req_id, done_event=threading.Event())


def _configure_adapter(monkeypatch, work_dir: Path, db: Path, backend) -> None:
    session = {
        "active": True, "work_dir": str(work_dir), "pane_id": "%1", "pane_title_marker": "CCB-Hermes-test",
        "terminal": "tmux", "ccb_project_id": "project",
    }
    monkeypatch.setattr(hermes_adapter_module, "_load_destination", lambda _req: (work_dir / ".hermes-session", session, ""))
    monkeypatch.setattr(hermes_adapter_module, "_check_pane", lambda *_args: (backend, ""))
    monkeypatch.setattr(hermes_adapter_module, "session_db_path", lambda _session: db)
    monkeypatch.setattr(hermes_adapter_module, "get_backend_for_session", lambda _session: backend)
    monkeypatch.setattr(hermes_adapter_module.HermesAdapter, "_finish", lambda self, task, result, **_kw: result)


@pytest.mark.parametrize(("active", "compacted", "has_done"), [(0, 0, False), (0, 1, True)])
def test_duplicate_stale_anchor_is_refused_before_send(tmp_path: Path, monkeypatch, active, compacted, has_done) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "old", tmp_path)
    _message(
        conn, "old", "user", "CCB_REQ_ID: stale\nOld task", timestamp="t1", order=1,
        active=active, compacted=compacted,
    )
    if has_done:
        _done(conn, "old", "stale", order=2, active=0, compacted=1)
    conn.commit()
    conn.close()
    backend = SimpleNamespace(send_text=lambda *_args: pytest.fail("stale request was sent"))
    _configure_adapter(monkeypatch, tmp_path, db, backend)

    result = hermes_adapter_module.HermesAdapter().handle_task(_adapter_task(tmp_path, "stale"))
    assert result.status == "failed"
    assert "anchor already exists" in result.reply  # negative: a stale task id cannot be submitted again


def test_rotation_during_request_is_incomplete_and_never_reads_child_reply(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "parent", tmp_path, end_reason="compression")
    _session(conn, "child", tmp_path, parent="parent")
    conn.commit()
    conn.close()

    def send(_pane, _text):
        conn = sqlite3.connect(db)
        _anchor(conn, "parent", "rotating", timestamp="clone", order=5)
        _anchor(conn, "child", "rotating", timestamp="clone", order=1)
        _done(conn, "child", "rotating", order=2)
        conn.commit()
        conn.close()

    backend = SimpleNamespace(send_text=send, pane_liveness=lambda _pane: "alive", find_pane_by_title_marker=lambda *_args: "%1")
    _configure_adapter(monkeypatch, tmp_path, db, backend)
    monkeypatch.setattr(hermes_adapter_module, "probe_pane_liveness", lambda *_args: "alive")

    result = hermes_adapter_module.HermesAdapter().handle_task(_adapter_task(tmp_path, "rotating"))
    assert result.status == "incomplete"
    assert "session rotated" in result.reply
    assert not result.done_seen  # negative: child session DONE is outside the anchor session


def test_compression_child_anchor_never_reads_child_done(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "parent", tmp_path, end_reason="compression")
    _session(conn, "child", tmp_path, parent="parent")
    _anchor(conn, "child", "child-only")
    _done(conn, "child", "child-only")
    conn.commit()
    conn.close()

    with pytest.raises(HermesStateError, match="compression child") as error:
        read_exchange(db, tmp_path, "child-only")
    assert error.value.kind == "rotated"


def test_pane_death_and_replacement_are_negative_controls(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "s1", tmp_path)
    conn.commit()
    conn.close()

    def send(_pane, _text):
        conn = sqlite3.connect(db)
        _anchor(conn, "s1", "dead")
        conn.commit()
        conn.close()

    backend = SimpleNamespace(send_text=send, pane_liveness=lambda _pane: "gone", find_pane_by_title_marker=lambda *_args: "%1")
    _configure_adapter(monkeypatch, tmp_path, db, backend)
    monkeypatch.setattr(hermes_adapter_module, "probe_pane_liveness", lambda *_args: "gone")
    monkeypatch.setattr(hermes_adapter_module, "PaneDeathTracker", lambda: SimpleNamespace(observe=lambda _status: True))
    dead = hermes_adapter_module.HermesAdapter().handle_task(_adapter_task(tmp_path, "dead"))
    assert dead.status == "incomplete" and "pane died" in dead.reply

    class ReplacedBackend:
        def is_alive(self, _pane):
            return True

        def find_pane_by_title_marker(self, *_args):
            return "%replacement"

    session = {"pane_id": "%1", "pane_title_marker": "CCB-Hermes-test", "terminal": "tmux"}
    monkeypatch.setitem(_check_pane.__globals__, "get_backend_for_session", lambda _session: ReplacedBackend())
    _backend, error = _check_pane(session, tmp_path)
    assert error and "identity changed" in error  # negative: pane-title replacement cannot pass validation


def test_completion_delivery_carries_hermes_identity(monkeypatch, tmp_path: Path) -> None:
    adapter = hermes_adapter_module.HermesAdapter()
    task = _adapter_task(tmp_path, "delivery")
    task.request.suppress_completion_hook = False
    notifications = []
    monkeypatch.setattr(hermes_adapter_module, "persist_proven_result", lambda *_args, **_kwargs: "no_receipt")
    monkeypatch.setattr(hermes_adapter_module, "notify_completion", lambda **kwargs: notifications.append(kwargs))
    result = hermes_adapter_module.ProviderResult(
        exit_code=0, reply="answer", req_id="delivery", session_key="hermes:project", done_seen=True,
        status="completed",
    )

    adapter._finish(task, result, database_path=str(tmp_path / "state.db"), conversation_id="session")
    assert len(notifications) == 1
    assert notifications[0]["provider"] == "hermes" and notifications[0]["req_id"] == "delivery"
    assert notifications[0]["done_seen"] is True

    task.request.suppress_completion_hook = True
    adapter._finish(task, result, database_path=str(tmp_path / "state.db"), conversation_id="session")
    assert len(notifications) == 1  # negative: suppressed completion cannot notify the caller


def test_incompatible_database_early_failure_is_persisted_and_notified(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    original_finish = hermes_adapter_module.HermesAdapter._finish
    backend = SimpleNamespace(send_text=lambda *_args: pytest.fail("incompatible DB request was sent"))
    _configure_adapter(monkeypatch, tmp_path, db, backend)
    monkeypatch.setattr(
        hermes_adapter_module, "read_exchange",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HermesStateError("incompatible", "schema 31")),
    )
    adapter = hermes_adapter_module.HermesAdapter()
    finish_calls = []
    notifications = []
    monkeypatch.setattr(hermes_adapter_module, "persist_proven_result", lambda *_args, **_kwargs: "no_receipt")
    monkeypatch.setattr(hermes_adapter_module, "notify_completion", lambda **kwargs: notifications.append(kwargs))
    monkeypatch.setattr(
        hermes_adapter_module.HermesAdapter, "_finish",
        lambda self, task, result, **kwargs: (finish_calls.append(result), original_finish(self, task, result, **kwargs))[1],
    )
    task = _adapter_task(tmp_path, "incompatible")
    task.request.suppress_completion_hook = False

    result = adapter.handle_task(task)

    assert result.status == "failed"
    assert len(finish_calls) == 1 and finish_calls[0] is result
    assert len(notifications) == 1 and notifications[0]["status"] == "failed"


def test_pend_surfaces_hermes_state_error_instead_of_pending(tmp_path: Path, monkeypatch, capsys) -> None:
    source = Path(__file__).resolve().parents[1] / "bin" / "pend"
    loader = importlib.machinery.SourceFileLoader("pend_hermes_error_test", str(source))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    pend = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(pend)
    monkeypatch.setattr(
        hermes_comm, "read_exchange",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HermesStateError("incompatible", "schema version 31")),
    )
    receipt = {
        "task_id": "broken", "provider": "hermes", "work_dir": str(tmp_path),
        "destination_database_path": str(tmp_path / "state.db"), "destination_conversation_id": "s1",
        "status_file": str(tmp_path / "missing.status"), "log_file": str(tmp_path / "missing.log"),
    }

    result = pend._show_receipt(receipt)

    assert result == pend.EXIT_ERROR
    assert "Hermes state DB incompatible: schema version 31" in capsys.readouterr().err


def test_reply_stops_at_done_line_and_drops_following_prose(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "s1", tmp_path)
    _anchor(conn, "s1", "trailing")
    _done(conn, "s1", "trailing", text="The answer\nCCB_DONE: trailing\npost-marker prose")
    conn.commit()
    conn.close()

    exchange = read_exchange(db, tmp_path, "trailing")
    assert exchange is not None
    answer, trailing = split_done_text("The answer\nCCB_DONE: trailing\npost-marker prose", "trailing")
    assert exchange.reply("trailing") == append_trailing_notice(answer, trailing)


def test_done_before_rotation_still_completes_from_parent(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _db(db)
    _session(conn, "parent", tmp_path, end_reason="compression")
    _session(conn, "child", tmp_path, parent="parent")
    _anchor(conn, "parent", "answered")
    _done(conn, "parent", "answered")
    conn.commit()
    conn.close()

    exchange = read_exchange(db, tmp_path, "answered")
    assert exchange is not None and exchange.session_id == "parent"
    assert exchange.reply("answered") == "Answer"


def test_check_pane_uses_the_real_wezterm_backend(tmp_path: Path, monkeypatch) -> None:
    from terminal import WeztermBackend

    panes = [{"pane_id": 11, "title": "CCB-Hermes-test", "cwd": f"file://{tmp_path}"}]
    monkeypatch.setattr(WeztermBackend, "_list_panes", lambda self: panes)
    monkeypatch.setitem(_check_pane.__globals__, "get_backend_for_session", lambda _session: WeztermBackend())
    session = {"pane_id": "11", "pane_title_marker": "CCB-Hermes-test", "terminal": "wezterm"}

    backend, error = _check_pane(session, tmp_path)
    assert error == "" and isinstance(backend, WeztermBackend)
    _backend, error = _check_pane(dict(session, pane_id="12"), tmp_path)
    assert error == "Hermes pane is not available."  # negative: a pane that is not listed fails
