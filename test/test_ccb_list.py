from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _write_fake_tmux(bin_dir: Path, alive_panes: list[str]) -> None:
    panes = "\n".join(alive_panes)
    script = bin_dir / "tmux"
    script.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"list-panes\" ]; then\n"
        f"  printf '%s\\n' {json.dumps(panes)}\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _run_ccb_list(tmp_path: Path, *args: str) -> list[dict]:
    env = os.environ.copy()
    env["CCB_RUN_DIR"] = str(tmp_path / "run")
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
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_tmux(fake_bin, ["%1"])

    (run_dir / "ccb-session-ai-test.json").write_text(
        json.dumps(
            {
                "work_dir": str(work_dir),
                "terminal": "tmux",
                "updated_at": 42,
                "providers": {
                    "claude": {"pane_id": "%1", "pane_title_marker": "CCB-Claude"},
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
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_tmux(fake_bin, ["%1"])

    (run_dir / "ccb-session-ai-test.json").write_text(
        json.dumps(
            {
                "work_dir": str(work_dir),
                "terminal": "tmux",
                "updated_at": 42,
                "providers": {"claude": {"pane_id": "%9"}},
            }
        ),
        encoding="utf-8",
    )

    assert _run_ccb_list(tmp_path) == []
    assert len(_run_ccb_list(tmp_path, "--stale")) == 1
