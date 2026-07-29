from __future__ import annotations

import json
import os
from pathlib import Path

import provider_log_reader as resolver
from claude_comm import ClaudeLogReader
from codex_comm import CodexLogReader


def _record(project: str, session: str, work_dir: Path, **providers: dict) -> dict:
    return {
        "ccb_project_id": project,
        "ccb_session_id": session,
        "work_dir": str(work_dir),
        "providers": providers,
    }


def test_strict_reader_is_exact_tab_bound_and_read_only(tmp_path: Path, monkeypatch) -> None:
    codex_log = tmp_path / "tab-a-codex.jsonl"
    codex_log.write_text("{}\n", encoding="utf-8")
    record = _record(
        "project-a",
        "tab-a",
        tmp_path,
        codex={"codex_session_path": str(codex_log), "codex_session_id": "codex-tab-a"},
    )
    monkeypatch.setattr(
        resolver,
        "load_registry_by_project_id",
        lambda *_args: (_ for _ in ()).throw(AssertionError("strict lookup must not fall back by project")),
    )
    monkeypatch.setattr(
        resolver,
        "upsert_registry",
        lambda *_args: (_ for _ in ()).throw(AssertionError("strict lookup must be read-only")),
    )

    reader = resolver.provider_log_reader(
        "codex",
        tmp_path,
        "project-a",
        strict_tab=True,
        ccb_session_id="tab-a",
        registry_record=record,
        update_registry=True,
    )

    assert isinstance(reader, CodexLogReader)
    assert reader.current_log_path() == codex_log.resolve()
    assert reader._session_id_filter == "codex-tab-a"
    assert reader._allow_stale_switch is False
    assert (
        resolver.provider_log_reader(
            "codex",
            tmp_path,
            "project-a",
            strict_tab=True,
            ccb_session_id="tab-b",
            registry_record=record,
        )
        is None
    )


def test_strict_claude_reader_is_pinned_to_recorded_log(tmp_path: Path) -> None:
    claude_log = tmp_path / "tab-a-claude.jsonl"
    claude_log.write_text("{}\n", encoding="utf-8")
    record = _record(
        "project-a",
        "tab-a",
        tmp_path,
        claude={"claude_session_path": str(claude_log)},
    )

    reader = resolver.provider_log_reader(
        "claude",
        tmp_path,
        "project-a",
        strict_tab=True,
        ccb_session_id="tab-a",
        registry_record=record,
    )

    assert isinstance(reader, ClaudeLogReader)
    assert reader._latest_session() == claude_log
    assert reader._allow_session_switch is False


def test_legacy_codex_resolution_preserves_env_and_top_level_session_filter(
    tmp_path: Path, monkeypatch
) -> None:
    codex_log = tmp_path / "legacy-codex.jsonl"
    codex_log.write_text("{}\n", encoding="utf-8")
    record = _record(
        "project-a",
        "tab-a",
        tmp_path,
        codex={"codex_session_path": str(codex_log), "codex_session_id": "nested-new"},
    )
    record["codex_session_id"] = "top-level-legacy"
    monkeypatch.setenv("CODEX_SESSION_ID", "tab-a")
    monkeypatch.setattr(resolver, "load_registry_by_session_id", lambda session: record if session == "tab-a" else None)
    monkeypatch.setattr(
        resolver,
        "load_registry_by_project_id",
        lambda *_args: (_ for _ in ()).throw(AssertionError("env resolution should win")),
    )

    reader = resolver.provider_log_reader("codex", tmp_path, "project-a")

    assert isinstance(reader, CodexLogReader)
    assert reader.current_log_path() == codex_log.resolve()
    assert reader._session_id_filter == "top-level-legacy"


def test_legacy_claude_resolution_keeps_freshness_and_registry_upsert(
    tmp_path: Path, monkeypatch
) -> None:
    registry_log = tmp_path / "registry.jsonl"
    session_log = tmp_path / "session.jsonl"
    registry_log.write_text("{}\n", encoding="utf-8")
    session_log.write_text("{}\n", encoding="utf-8")
    os.utime(registry_log, (10, 10))
    os.utime(session_log, (20, 20))
    session_file = tmp_path / ".claude-session"
    session_file.write_text(
        json.dumps({"claude_session_path": str(session_log), "claude_session_id": "claude-new"}),
        encoding="utf-8",
    )
    record = _record(
        "project-a",
        "tab-a",
        tmp_path,
        claude={"claude_session_path": str(registry_log)},
    )
    updates: list[dict] = []
    monkeypatch.setenv("CCB_SESSION_ID", "tab-a")
    monkeypatch.setattr(resolver, "load_registry_by_session_id", lambda session: record if session == "tab-a" else None)
    monkeypatch.setattr(resolver, "upsert_registry", lambda updated: updates.append(updated.copy()))

    reader = resolver.provider_log_reader(
        "claude",
        tmp_path,
        "project-a",
        explicit_session_file=session_file,
        update_registry=True,
    )

    assert isinstance(reader, ClaudeLogReader)
    assert reader._preferred_session == session_log
    assert reader._allow_session_switch is True
    assert record["providers"]["claude"]["claude_session_path"] == str(session_log)
    assert len(updates) == 1


def test_request_anchor_reconciliation_requires_a_user_anchor(tmp_path: Path) -> None:
    req_id = "20260729-210000-000-1"
    codex_log = tmp_path / "codex.jsonl"
    codex_log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": f"Quoted CCB_REQ_ID: {req_id}",
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": f"CCB_REQ_ID: {req_id}\nDelivered",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert resolver.provider_request_anchor_seen(
        "codex",
        tmp_path,
        "project-a",
        req_id,
        log_path=codex_log,
    )
    assert not resolver.provider_request_anchor_seen(
        "codex",
        tmp_path,
        "project-a",
        "different-task",
        log_path=codex_log,
    )


def test_claude_request_anchor_reconciliation_reads_explicit_log(
    tmp_path: Path,
) -> None:
    req_id = "20260729-210000-000-2"
    claude_log = tmp_path / "claude.jsonl"
    claude_log.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"CCB_REQ_ID: {req_id}\nDelivered",
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert resolver.provider_request_anchor_seen(
        "claude",
        tmp_path,
        "project-a",
        req_id,
        log_path=claude_log,
    )
