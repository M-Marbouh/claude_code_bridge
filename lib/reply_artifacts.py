from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Callable

from askd_runtime import run_dir
from project_id import compute_ccb_project_id


HARD_INLINE_MAX_BYTES = 64 * 1024
DEFAULT_PREVIEW_BYTES = 16 * 1024
DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MAX_ARTIFACTS = 100


def _env_positive_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _inline_limit() -> int:
    configured = _env_positive_int(
        "CCB_COMPLETION_INLINE_MAX_BYTES", HARD_INLINE_MAX_BYTES
    )
    return min(configured, HARD_INLINE_MAX_BYTES)


def _artifact_stem(req_id: str, digest: str) -> str:
    safe = "".join(c for c in (req_id or "") if c.isalnum() or c in "-_")[:120]
    return safe or f"reply-{digest[:20]}"


def _cleanup_artifacts(completion_dir: Path, keep: Path) -> None:
    ttl = _env_positive_int(
        "CCB_COMPLETION_ARTIFACT_TTL_SECONDS", DEFAULT_RETENTION_SECONDS
    )
    max_artifacts = _env_positive_int(
        "CCB_COMPLETION_ARTIFACT_MAX_FILES", DEFAULT_MAX_ARTIFACTS
    )
    cutoff = time.time() - ttl
    candidates: list[tuple[float, Path]] = []
    try:
        paths = list(completion_dir.glob("*.md"))
    except OSError:
        return

    for path in paths:
        if path == keep or path.is_symlink():
            continue
        try:
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
            if mtime < cutoff:
                path.unlink()
                continue
            candidates.append((mtime, path))
        except OSError:
            continue

    candidates.sort(reverse=True)
    retained_other_count = max(0, max_artifacts - 1)
    for _mtime, path in candidates[retained_other_count:]:
        try:
            path.unlink()
        except OSError:
            pass


def _completion_dirs(
    project: str | Path | None,
    *,
    all_projects: bool,
) -> list[Path]:
    current = run_dir()
    if all_projects:
        if current.parent.name == "projects":
            base = current.parent.parent
        else:
            base = current
        candidates = [
            current / "completions",
            base / "completions",
            *(base / "projects").glob("*/completions"),
        ]
    elif project is not None and current.parent.name == "projects":
        project_id = compute_ccb_project_id(Path(project).expanduser())
        candidates = [
            current.parent / project_id[:16] / "completions",
        ]
    elif project is not None and not (os.environ.get("CCB_RUN_DIR") or "").strip():
        project_id = compute_ccb_project_id(Path(project).expanduser())
        candidates = [
            current / "projects" / project_id[:16] / "completions",
        ]
    else:
        candidates = [current / "completions"]
    return list(dict.fromkeys(candidates))


def cleanup_reply_artifacts(
    *,
    project: str | Path | None = None,
    all_projects: bool = False,
    older_than_seconds: int = DEFAULT_RETENTION_SECONDS,
    dry_run: bool = False,
    now: float | None = None,
) -> int:
    """Delete expired reply artifacts and return the matching/deleted count."""
    cutoff = (time.time() if now is None else float(now)) - max(
        0, int(older_than_seconds)
    )
    affected = 0
    for completion_dir in _completion_dirs(project, all_projects=all_projects):
        try:
            paths = list(completion_dir.glob("*.md"))
        except OSError:
            continue
        for path in paths:
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_mtime >= cutoff:
                    continue
                if not dry_run:
                    path.unlink()
                affected += 1
            except OSError:
                continue
    return affected


def prepare_agent_visible_reply(
    reply_content: str,
    req_id: str,
    *,
    debug_log: Callable[[str], None] | None = None,
) -> str:
    """Keep agent-visible reply output bounded while preserving oversized results."""
    encoded = (reply_content or "").encode("utf-8")
    inline_limit = _inline_limit()
    if len(encoded) <= inline_limit:
        return reply_content

    digest = hashlib.sha256(encoded).hexdigest()
    completion_dir = run_dir() / "completions"
    final_path = completion_dir / f"{_artifact_stem(req_id, digest)}.md"
    temp_path: Path | None = None
    try:
        completion_dir.mkdir(parents=True, exist_ok=True)
        try:
            completion_dir.chmod(0o700)
        except OSError:
            pass
        fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{final_path.stem}-", dir=completion_dir
        )
        temp_path = Path(raw_temp_path)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temp_path, final_path)
        temp_path = None
        _cleanup_artifacts(completion_dir, final_path)
    except OSError as exc:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        preview_size = min(DEFAULT_PREVIEW_BYTES, max(1, inline_limit // 2))
        preview = encoded[:preview_size].decode("utf-8", errors="ignore").rstrip()
        if debug_log:
            debug_log(f"oversized reply spill failed req_id={req_id!r} error={exc}")
        return (
            "[CCB_RESULT_WITHHELD]\n"
            f"Result size: {len(encoded)} bytes\n"
            f"Inline limit: {inline_limit} bytes\n"
            "The full result could not be persisted; inspect the provider task log.\n\n"
            f"Preview:\n{preview}"
        )

    preview_size = min(DEFAULT_PREVIEW_BYTES, max(1, inline_limit // 2))
    preview = encoded[:preview_size].decode("utf-8", errors="ignore").rstrip()
    if debug_log:
        debug_log(
            f"oversized reply spilled req_id={req_id!r} "
            f"bytes={len(encoded)} path={final_path}"
        )
    return (
        "[CCB_RESULT_SPILLED]\n"
        f"Result size: {len(encoded)} bytes\n"
        f"Inline limit: {inline_limit} bytes\n"
        f"Full result: {final_path}\n"
        f"SHA-256: {digest}\n"
        "The exact result is preserved. Read only the needed sections, "
        "or read the full file if required.\n\n"
        f"Preview (first {preview_size} bytes):\n{preview}"
    )
