from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import reply_artifacts


def test_configured_limit_cannot_raise_hard_64k_cap(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CCB_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("CCB_COMPLETION_INLINE_MAX_BYTES", str(1024 * 1024))
    reply = "x" * (64 * 1024 + 1)

    prepared = reply_artifacts.prepare_agent_visible_reply(reply, "req-hard-cap")

    result_path = tmp_path / "completions" / "req-hard-cap.md"
    assert result_path.read_text(encoding="utf-8") == reply
    assert "[CCB_RESULT_SPILLED]" in prepared
    assert "Inline limit: 65536 bytes" in prepared
    assert hashlib.sha256(reply.encode()).hexdigest() in prepared


def test_unknown_request_ids_use_content_derived_artifact_names(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CCB_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("CCB_COMPLETION_INLINE_MAX_BYTES", "8")

    first = reply_artifacts.prepare_agent_visible_reply("first result", "")
    second = reply_artifacts.prepare_agent_visible_reply("second result", "")

    paths = sorted((tmp_path / "completions").glob("reply-*.md"))
    assert len(paths) == 2
    assert paths[0] != paths[1]
    assert str(paths[0]) in first or str(paths[0]) in second
    assert str(paths[1]) in first or str(paths[1]) in second


def test_artifact_cleanup_removes_only_expired_markdown(
    monkeypatch, tmp_path: Path
) -> None:
    completion_dir = tmp_path / "completions"
    completion_dir.mkdir()
    expired = completion_dir / "expired.md"
    unrelated = completion_dir / "keep.txt"
    expired.write_text("old", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    old = time.time() - 60
    os.utime(expired, (old, old))
    os.utime(unrelated, (old, old))
    monkeypatch.setenv("CCB_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("CCB_COMPLETION_INLINE_MAX_BYTES", "8")
    monkeypatch.setenv("CCB_COMPLETION_ARTIFACT_TTL_SECONDS", "1")

    reply_artifacts.prepare_agent_visible_reply("new result", "req-new")

    assert not expired.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert (completion_dir / "req-new.md").read_text(encoding="utf-8") == "new result"


def test_spill_failure_withholds_full_oversized_result(
    monkeypatch, tmp_path: Path
) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied", encoding="utf-8")
    monkeypatch.setattr(reply_artifacts, "run_dir", lambda: blocked)
    monkeypatch.setenv("CCB_COMPLETION_INLINE_MAX_BYTES", "8")
    reply = "secret-tail-" * 100

    prepared = reply_artifacts.prepare_agent_visible_reply(reply, "req-fail")

    assert "[CCB_RESULT_WITHHELD]" in prepared
    assert "The full result could not be persisted" in prepared
    assert len(prepared.encode("utf-8")) < len(reply.encode("utf-8"))


def test_explicit_artifact_cleanup_supports_dry_run(
    monkeypatch, tmp_path: Path
) -> None:
    completion_dir = tmp_path / "completions"
    completion_dir.mkdir()
    expired = completion_dir / "expired.md"
    fresh = completion_dir / "fresh.md"
    unrelated = completion_dir / "keep.txt"
    expired.write_text("old", encoding="utf-8")
    fresh.write_text("new", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    now = time.time()
    os.utime(expired, (now - 60, now - 60))
    monkeypatch.setenv("CCB_RUN_DIR", str(tmp_path))

    assert (
        reply_artifacts.cleanup_reply_artifacts(
            older_than_seconds=30, dry_run=True, now=now
        )
        == 1
    )
    assert expired.exists()
    assert (
        reply_artifacts.cleanup_reply_artifacts(
            older_than_seconds=30, now=now
        )
        == 1
    )
    assert not expired.exists()
    assert fresh.exists()
    assert unrelated.exists()


def test_explicit_project_cleanup_does_not_use_current_project_override(
    monkeypatch, tmp_path: Path
) -> None:
    projects_dir = tmp_path / "projects"
    current_dir = projects_dir / "current"
    other_project = tmp_path / "other-project"
    other_project.mkdir()
    other_id = reply_artifacts.compute_ccb_project_id(other_project)[:16]
    other_completion_dir = projects_dir / other_id / "completions"
    current_completion_dir = current_dir / "completions"
    other_completion_dir.mkdir(parents=True)
    current_completion_dir.mkdir(parents=True)
    other_artifact = other_completion_dir / "other.md"
    current_artifact = current_completion_dir / "current.md"
    other_artifact.write_text("other", encoding="utf-8")
    current_artifact.write_text("current", encoding="utf-8")
    monkeypatch.setenv("CCB_RUN_DIR", str(current_dir))

    assert (
        reply_artifacts.cleanup_reply_artifacts(
            project=other_project,
            older_than_seconds=0,
            now=time.time() + 1,
        )
        == 1
    )
    assert not other_artifact.exists()
    assert current_artifact.exists()
