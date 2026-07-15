from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from project_id import compute_ccb_project_id


ROOT = Path(__file__).resolve().parents[1]


def _write_fake_tmux(bin_dir: Path, panes: list[dict[str, str]]) -> None:
    lines = "\n".join(
        "\t".join(
            [
                pane["pane_id"],
                pane.get("title", ""),
                pane.get("cwd", ""),
                pane.get("dead", "0"),
                pane.get("session_id", "$1"),
                pane.get("window_id", "@1"),
            ]
        )
        for pane in panes
    )
    script = bin_dir / "tmux"
    script.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"list-panes\" ]; then\n"
        f"  printf '%b\\n' {json.dumps(lines)}\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _write_fake_wezterm(bin_dir: Path, panes: list[dict[str, str]]) -> None:
    script = bin_dir / "wezterm"
    script.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"cli\" ] && [ \"$2\" = \"list\" ]; then\n"
        f"  printf '%s\\n' {json.dumps(json.dumps(panes))}\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _run_ccb_list(tmp_path: Path, *args: str) -> list[dict]:
    env = os.environ.copy()
    env.pop("CCB_RUN_DIR", None)
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "ccb-list"), "--json", *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return json.loads(result.stdout)


def test_ccb_list_reports_provider_liveness(tmp_path: Path) -> None:
    run_dir = tmp_path / ".ccb" / "run"
    run_dir.mkdir(parents=True)
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_tmux(
        fake_bin,
        [
            {"pane_id": "%1", "title": "CCB-Claude-test", "cwd": str(work_dir), "dead": "0"},
            {"pane_id": "%2", "title": "other", "cwd": str(work_dir), "dead": "0"},
        ],
    )

    (run_dir / "ccb-session-ai-test.json").write_text(
        json.dumps(
            {
                "work_dir": str(work_dir),
                "terminal": "tmux",
                "updated_at": int(time.time()),
                "providers": {
                    "claude": {"pane_id": "%1", "pane_title_marker": "CCB-Claude-test"},
                    "codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex"},
                },
            }
        ),
        encoding="utf-8",
    )

    entries = _run_ccb_list(tmp_path)

    assert len(entries) == 1
    assert entries[0]["index"] == 1
    assert entries[0]["work_dir"] == str(work_dir)
    assert entries[0]["providers"]["claude"]["alive"] is True
    assert entries[0]["providers"]["codex"]["alive"] is True
    assert entries[0]["session_count"] == 1
    assert entries[0]["peer_capable"] is False
    assert len(entries[0]["sessions"]) == 1


def test_ccb_list_preserves_wezterm_pane_zero(tmp_path: Path) -> None:
    run_dir = tmp_path / ".ccb" / "run"
    run_dir.mkdir(parents=True)
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_wezterm(
        fake_bin,
        [{"pane_id": 0, "cwd": f"file://{work_dir}", "title": "CCB-Claude-test", "tab_id": 7}],
    )
    (run_dir / "ccb-session-zero.json").write_text(
        json.dumps(
            {
                "ccb_session_id": "zero",
                "work_dir": str(work_dir),
                "terminal": "wezterm",
                "updated_at": int(time.time()),
                "providers": {"claude": {"pane_id": 0, "pane_title_marker": "CCB-Claude-test"}},
            }
        ),
        encoding="utf-8",
    )

    entries = _run_ccb_list(tmp_path)

    assert len(entries) == 1
    assert entries[0]["providers"]["claude"]["pane_id"] == "0"
    assert entries[0]["providers"]["claude"]["alive"] is True


def test_ccb_list_omits_stale_only_projects_by_default(tmp_path: Path) -> None:
    run_dir = tmp_path / ".ccb" / "run"
    run_dir.mkdir(parents=True)
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_tmux(fake_bin, [{"pane_id": "%1", "title": "CCB-Claude", "cwd": str(work_dir), "dead": "0"}])

    (run_dir / "ccb-session-ai-test.json").write_text(
        json.dumps(
            {
                "work_dir": str(work_dir),
                "terminal": "tmux",
                "updated_at": int(time.time()),
                "providers": {"claude": {"pane_id": "%9"}},
            }
        ),
        encoding="utf-8",
    )

    assert _run_ccb_list(tmp_path) == []
    assert len(_run_ccb_list(tmp_path, "--stale")) == 1


def test_ccb_list_includes_codex_only_project_by_default(tmp_path: Path) -> None:
    run_dir = tmp_path / ".ccb" / "run"
    run_dir.mkdir(parents=True)
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_tmux(fake_bin, [{"pane_id": "%2", "title": "CCB-Codex-test", "cwd": str(work_dir), "dead": "0"}])

    (run_dir / "ccb-session-ai-test.json").write_text(
        json.dumps(
            {
                "work_dir": str(work_dir),
                "terminal": "tmux",
                "updated_at": int(time.time()),
                "providers": {
                    "claude": {"pane_id": "%1", "pane_title_marker": "CCB-Claude-test"},
                    "codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"},
                },
            }
        ),
        encoding="utf-8",
    )

    entries = _run_ccb_list(tmp_path)
    assert len(entries) == 1
    assert entries[0]["providers"]["codex"]["alive"] is True
    assert "claude" not in entries[0]["providers"]
    assert entries[0]["peer_capable"] is False


def test_ccb_list_rejects_reused_tmux_pane_id_with_wrong_marker(tmp_path: Path) -> None:
    run_dir = tmp_path / ".ccb" / "run"
    run_dir.mkdir(parents=True)
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_tmux(fake_bin, [{"pane_id": "%4", "title": "regular shell", "cwd": str(work_dir), "dead": "0"}])

    (run_dir / "ccb-session-ai-test.json").write_text(
        json.dumps(
            {
                "work_dir": str(work_dir),
                "terminal": "tmux",
                "updated_at": int(time.time()),
                "providers": {"claude": {"pane_id": "%4", "pane_title_marker": "CCB-Claude-test"}},
            }
        ),
        encoding="utf-8",
    )

    assert _run_ccb_list(tmp_path) == []
    stale = _run_ccb_list(tmp_path, "--stale")
    assert stale[0]["providers"]["claude"]["alive"] is False


def test_ccb_list_rejects_reused_wezterm_pane_id_with_wrong_cwd(tmp_path: Path) -> None:
    run_dir = tmp_path / ".ccb" / "run"
    run_dir.mkdir(parents=True)
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_wezterm(fake_bin, [{"pane_id": 23, "cwd": f"file://{other_dir}", "title": "CCB-Claude-test"}])

    (run_dir / "ccb-session-ai-test.json").write_text(
        json.dumps(
            {
                "work_dir": str(work_dir),
                "terminal": "wezterm",
                "updated_at": int(time.time()),
                "providers": {"claude": {"pane_id": "23", "pane_title_marker": "CCB-Claude-test"}},
            }
        ),
        encoding="utf-8",
    )

    assert _run_ccb_list(tmp_path) == []
    stale = _run_ccb_list(tmp_path, "--stale")
    assert stale[0]["providers"]["claude"]["alive"] is False


def test_ccb_list_keeps_verified_live_window_past_registry_ttl(tmp_path: Path) -> None:
    run_dir = tmp_path / ".ccb" / "run"
    run_dir.mkdir(parents=True)
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_tmux(fake_bin, [{"pane_id": "%1", "title": "CCB-Claude-test", "cwd": str(work_dir), "dead": "0"}])

    (run_dir / "ccb-session-ai-test.json").write_text(
        json.dumps(
            {
                "work_dir": str(work_dir),
                "terminal": "tmux",
                "updated_at": int(time.time()) - (8 * 24 * 60 * 60),
                "providers": {"claude": {"pane_id": "%1", "pane_title_marker": "CCB-Claude-test"}},
            }
        ),
        encoding="utf-8",
    )

    entries = _run_ccb_list(tmp_path)
    assert len(entries) == 1
    assert entries[0]["providers"]["claude"]["alive"] is True
    assert entries[0]["providers"]["claude"]["timestamp_stale"] is True


def test_ccb_list_keeps_same_project_wezterm_tabs_as_separate_windows(tmp_path: Path) -> None:
    run_dir = tmp_path / ".ccb" / "run"
    run_dir.mkdir(parents=True)
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_wezterm(
        fake_bin,
        [
            {"pane_id": 1, "cwd": f"file://{work_dir}", "title": "Claude", "tab_id": 20},
            {"pane_id": 2, "cwd": f"file://{work_dir}", "title": "Codex", "tab_id": 20},
            {"pane_id": 3, "cwd": f"file://{work_dir}", "title": "Claude", "tab_id": 21},
            {"pane_id": 4, "cwd": f"file://{work_dir}", "title": "Codex", "tab_id": 21},
        ],
    )
    project_id = compute_ccb_project_id(work_dir)
    for session_id, claude_pane, codex_pane, updated_at in (
        ("first", "1", "2", int(time.time()) - 10),
        ("second", "3", "4", int(time.time())),
    ):
        (run_dir / f"ccb-session-{session_id}.json").write_text(
            json.dumps(
                {
                    "ccb_session_id": session_id,
                    "ccb_project_id": project_id,
                    "work_dir": str(work_dir),
                    "terminal": "wezterm",
                    "updated_at": updated_at,
                    "providers": {
                        "claude": {"pane_id": claude_pane, "pane_title_marker": "CCB-Claude-test"},
                        "codex": {"pane_id": codex_pane, "pane_title_marker": "CCB-Codex-test"},
                    },
                }
            ),
            encoding="utf-8",
        )

    entries = _run_ccb_list(tmp_path)

    assert len(entries) == 1
    assert entries[0]["session_count"] == 2
    assert {session["window_id"] for session in entries[0]["sessions"]} == {"tab:20", "tab:21"}
    assert entries[0]["peer_capable"] is False


def test_ccb_list_uses_cwd_and_window_cohort_when_tmux_titles_change(tmp_path: Path) -> None:
    run_dir = tmp_path / ".ccb" / "run"
    run_dir.mkdir(parents=True)
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_tmux(
        fake_bin,
        [
            {"pane_id": "%1", "title": "Claude Code", "cwd": str(work_dir), "window_id": "@8"},
            {"pane_id": "%2", "title": "Codex", "cwd": str(work_dir), "window_id": "@8"},
        ],
    )
    (run_dir / "ccb-session-live.json").write_text(
        json.dumps(
            {
                "ccb_session_id": "live",
                "work_dir": str(work_dir),
                "terminal": "tmux",
                "updated_at": int(time.time()),
                "providers": {
                    "claude": {"pane_id": "%1", "pane_title_marker": "CCB-Claude-test"},
                    "codex": {"pane_id": "%2", "pane_title_marker": "CCB-Codex-test"},
                },
            }
        ),
        encoding="utf-8",
    )

    entries = _run_ccb_list(tmp_path)

    assert len(entries) == 1
    assert entries[0]["session_count"] == 1
    assert set(entries[0]["providers"]) == {"claude", "codex"}
    assert all(status["alive"] for status in entries[0]["providers"].values())


def test_ccb_list_hides_inactive_unconfigured_legacy_providers(tmp_path: Path) -> None:
    run_dir = tmp_path / ".ccb" / "run"
    run_dir.mkdir(parents=True)
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    config_dir = work_dir / ".ccb"
    config_dir.mkdir()
    (config_dir / "ccb.config").write_text("codex, claude\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_wezterm(
        fake_bin,
        [
            {"pane_id": 2, "cwd": f"file://{work_dir}", "title": "CCB-Gemini", "tab_id": 31},
            {"pane_id": 4, "cwd": f"file://{work_dir}", "title": "CCB-Opencode", "tab_id": 32},
            {"pane_id": 10, "cwd": f"file://{work_dir}", "title": "Claude", "tab_id": 30},
            {"pane_id": 11, "cwd": f"file://{work_dir}", "title": "Codex", "tab_id": 30},
        ],
    )
    project_id = compute_ccb_project_id(work_dir)
    (run_dir / "ccb-session-current.json").write_text(
        json.dumps(
            {
                "ccb_session_id": "current",
                "ccb_project_id": project_id,
                "work_dir": str(work_dir),
                "terminal": "wezterm",
                "updated_at": int(time.time()),
                "providers": {
                    "claude": {"pane_id": "10", "pane_title_marker": "CCB-Claude-test"},
                    "codex": {"pane_id": "11", "pane_title_marker": "CCB-Codex-test"},
                },
            }
        ),
        encoding="utf-8",
    )
    for provider, pane_id in (("gemini", "2"), ("opencode", "4")):
        (run_dir / f"ccb-session-old-{provider}.json").write_text(
            json.dumps(
                {
                    "ccb_session_id": f"old-{provider}",
                    "ccb_project_id": project_id,
                    "work_dir": str(work_dir),
                    "terminal": "wezterm",
                    "updated_at": int(time.time()) - 100,
                    "providers": {provider: {"pane_id": pane_id, "pane_title_marker": f"CCB-{provider}"}},
                }
            ),
            encoding="utf-8",
        )

    entries = _run_ccb_list(tmp_path)

    assert len(entries) == 1
    assert set(entries[0]["providers"]) == {"claude", "codex"}
    assert all(set(session["providers"]) <= {"claude", "codex"} for session in entries[0]["sessions"])


def test_ccb_list_marks_inactive_session_file_unbound(tmp_path: Path) -> None:
    run_dir = tmp_path / ".ccb" / "run"
    run_dir.mkdir(parents=True)
    work_dir = tmp_path / "project"
    session_dir = work_dir / ".ccb"
    session_dir.mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_wezterm(
        fake_bin,
        [{"pane_id": 7, "cwd": f"file://{work_dir}", "title": "CCB-Claude-test", "tab_id": 40}],
    )
    project_id = compute_ccb_project_id(work_dir)
    session_file = session_dir / ".claude-session"
    session_file.write_text(
        json.dumps(
            {
                "active": False,
                "provider": "claude",
                "pane_id": "7",
                "ccb_project_id": project_id,
                "session_id": "live",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "ccb-session-live.json").write_text(
        json.dumps(
            {
                "ccb_session_id": "live",
                "ccb_project_id": project_id,
                "work_dir": str(work_dir),
                "terminal": "wezterm",
                "updated_at": int(time.time()),
                "providers": {
                    "claude": {
                        "pane_id": "7",
                        "pane_title_marker": "CCB-Claude-test",
                        "session_file": str(session_file),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    entries = _run_ccb_list(tmp_path)

    assert len(entries) == 1
    claude = entries[0]["providers"]["claude"]
    assert claude["alive"] is True
    assert claude["session_bound"] is False
    assert claude["mounted"] is False
    assert claude["reason"] == "session_unbound"
    assert entries[0]["peer_capable"] is False
