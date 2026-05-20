from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _write_fake_tmux(bin_dir: Path, panes: list[dict[str, str]]) -> None:
    lines = "\n".join(
        "\t".join(
            [
                pane["pane_id"],
                pane.get("title", ""),
                pane.get("cwd", ""),
                pane.get("dead", "0"),
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
    assert entries[0]["providers"]["codex"]["alive"] is False


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


def test_ccb_list_requires_alive_claude_pane_by_default(tmp_path: Path) -> None:
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

    assert _run_ccb_list(tmp_path) == []
    stale = _run_ccb_list(tmp_path, "--stale")
    assert len(stale) == 1
    assert stale[0]["providers"]["claude"]["alive"] is False
    assert stale[0]["providers"]["codex"]["alive"] is True


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


def test_ccb_list_omits_expired_registry_even_if_pane_matches(tmp_path: Path) -> None:
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

    assert _run_ccb_list(tmp_path) == []
    assert _run_ccb_list(tmp_path, "--stale") == []
