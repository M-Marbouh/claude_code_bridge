"""Contract tests for CCB's Pi state extension (lib/pi_extension/ccb-state.ts).

The extension is TypeScript that Pi loads itself, so these tests run it through Node's type
stripping against fake Pi runtimes and read the snapshot it actually writes. They cover the
writer-side guarantees the reader relies on: one publisher per process, child and retired
instances never write, a session change inside the launched Pi keeps publishing, and the file is
replaced atomically with mode 0600.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EXTENSION = REPO / "lib" / "pi_extension" / "ccb-state.ts"
PROBE = Path(__file__).parent / "fixtures" / "pi" / "extension_probe.mjs"
NODE = shutil.which("node")
#: A per-run launch token, derived rather than written out: it stands in for the random value
#: CCB generates for one launch.
TOKEN = "probe-" + uuid.uuid4().hex

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required to run Pi's TypeScript extension")


def _node_command() -> list[str]:
    """Node with type stripping; older/newer releases differ on the flag, so pick what works."""
    base = [str(NODE)]
    for flags in (["--experimental-strip-types"], []):
        probe = subprocess.run(base + flags + ["-e", "process.exit(0)"], capture_output=True, text=True)
        if probe.returncode == 0:
            return base + flags
    pytest.skip("this node cannot run TypeScript modules")


def _pi_dist() -> Path:
    """The installed Pi's `dist/`, so the probe loads the extension through Pi's own loader."""
    candidates: list[Path] = []
    pi = shutil.which("pi")
    if pi:
        # <prefix>/bin/pi -> <prefix>/lib/node_modules/@earendil-works/pi-coding-agent/dist
        candidates.append(Path(pi).resolve().parent.parent / "lib/node_modules/@earendil-works/pi-coding-agent/dist")
        candidates.append(Path(pi).resolve().parent.parent / "dist")
    candidates.append(Path.home() / ".local/lib/node_modules/@earendil-works/pi-coding-agent/dist")
    for candidate in candidates:
        if (candidate / "core/extensions/loader.js").is_file():
            return candidate
    pytest.skip("the installed Pi package (dist/core/extensions/loader.js) is required to drive the real loader")


def _run(tmp_path: Path, steps: list[dict], *, token: str | None = TOKEN,
         state_channel: bool = True) -> tuple[list[dict], Path | None]:
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"steps": steps}), encoding="utf-8")
    runtime_dir = tmp_path / "rt"
    runtime_dir.mkdir(mode=0o700, exist_ok=True)
    state_path = runtime_dir / "pi-state.json"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    if state_channel:
        env["CCB_PI_STATE_FILE"] = str(state_path)
        if token is not None:
            env["CCB_PI_LAUNCH_TOKEN"] = token
    completed = subprocess.run(
        [*_node_command(), str(PROBE), str(scenario), str(EXTENSION), str(_pi_dist())],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    results = json.loads(completed.stdout)
    return results, state_path if state_channel else None


def _tui(tmp_path: Path, name: str = "main", **overrides) -> dict:
    """A runtime as Pi binds it for the interactive pane: mode "tui" with a UI."""
    return {"op": "runtime", "cwd": str(tmp_path / name), "mode": "tui", **overrides}


def _headless(tmp_path: Path, name: str) -> dict:
    """A child runtime as Pi binds it in-process: print mode, no UI bound."""
    return {"op": "runtime", "cwd": str(tmp_path / name), "mode": "print"}


START = {"event": "session_start", "reason": "startup"}


def test_a_started_session_publishes_a_private_ready_snapshot(tmp_path: Path) -> None:
    results, state_path = _run(tmp_path, [_tui(tmp_path), START])
    assert state_path is not None
    mode = stat.S_IMODE(state_path.stat().st_mode)
    assert mode == 0o600
    state = results[0]["state"]
    assert state["v"] == 1
    assert state["launch_token"] == TOKEN
    assert state["mode"] == "tui" and state["has_ui"] is True
    assert state["idle"] is True and state["editor_empty"] is True and state["prompt_open"] is False
    assert state["pid"] > 0
    # Atomic replace leaves no temporary file behind.
    assert list(state_path.parent.glob("*.tmp")) == []


def test_editor_text_and_agent_lifecycle_are_reported(tmp_path: Path) -> None:
    results, _path = _run(tmp_path, [
        _tui(tmp_path),
        START,
        {"event": "input", "editor": "a draft line"},
        {"event": "agent_start", "idle": False},
        {"event": "agent_settled", "idle": True},
        {"event": "ui_prompt_start", "idle": True},
        {"event": "ui_prompt_end", "idle": True},
    ])
    _start, draft, busy, settled, prompting, resumed = (item["state"] for item in results)
    assert draft["editor_empty"] is False and draft["idle"] is True
    assert busy["idle"] is False
    assert settled["idle"] is True and settled["editor_empty"] is True
    assert prompting["prompt_open"] is True
    assert resumed["prompt_open"] is False
    # Every write advances the sequence the reader confirms against.
    assert [draft["seq"], busy["seq"], settled["seq"], prompting["seq"], resumed["seq"]] == sorted(
        {draft["seq"], busy["seq"], settled["seq"], prompting["seq"], resumed["seq"]}
    )


def test_a_whitespace_only_editor_counts_as_empty(tmp_path: Path) -> None:
    # Spaces in the editor cannot be concatenated into anything, so they are not a draft.
    results, _path = _run(tmp_path, [
        _tui(tmp_path),
        START,
        {"event": "input", "editor": "   \n  "},
    ])
    assert results[1]["state"]["editor_empty"] is True


def test_a_headless_child_in_another_cwd_never_writes(tmp_path: Path) -> None:
    # Pi's extension cache is keyed by cwd, so a runtime with another cwd gets a fresh module
    # instance (a real child/subagent). It runs headless, so it must never claim the pane.
    results, _path = _run(tmp_path, [
        _tui(tmp_path),
        START,
        _headless(tmp_path, "child-elsewhere"),
        {"event": "session_start", "runtime": 1, "reason": "startup", "session": "child", "file": "/s/child.jsonl"},
        {"event": "agent_settled", "runtime": 1, "session": "child", "file": "/s/child.jsonl"},
    ])
    published = results[0]["state"]
    assert published is not None
    assert results[1]["state"] == published
    assert results[2]["state"] == published


def test_a_headless_child_in_the_same_cwd_never_writes(tmp_path: Path) -> None:
    results, _path = _run(tmp_path, [
        _tui(tmp_path),
        START,
        _headless(tmp_path, "main"),
        {"event": "session_start", "runtime": 1, "reason": "startup", "session": "child", "file": "/s/child.jsonl"},
        {"event": "agent_settled", "runtime": 1, "session": "child", "file": "/s/child.jsonl"},
    ])
    published = results[0]["state"]
    assert results[1]["state"] == published
    assert results[2]["state"] == published


def test_a_child_cannot_claim_after_a_non_quit_shutdown_and_the_replacement_tui_publishes(tmp_path: Path) -> None:
    results, _path = _run(tmp_path, [
        _tui(tmp_path),
        START,
        _headless(tmp_path, "child-elsewhere"),
        {"event": "session_shutdown", "runtime": 0, "reason": "reload"},
        # The paused main must not hand the pane to a headless runtime.
        {"event": "session_start", "runtime": 1, "reason": "startup", "session": "child"},
        # A reload starts a new TUI runtime with a fresh module instance in the same process.
        _tui(tmp_path, "main", reload=True),
        {"event": "session_start", "runtime": 2, "reason": "reload"},
    ])
    published = results[0]["state"]
    assert results[1]["state"] == published, "the child claimed the paused main's pane"
    assert results[2]["state"] == published
    replacement = results[3]["state"]
    assert replacement is not None
    assert replacement["runtime_instance_id"] != published["runtime_instance_id"]
    assert replacement["seq"] == 1 and replacement["mode"] == "tui"


def test_the_replaced_publisher_never_writes_after_handover(tmp_path: Path) -> None:
    results, state_path = _run(tmp_path, [
        _tui(tmp_path),
        START,
        _tui(tmp_path, "main", reload=True),
        {"event": "session_start", "runtime": 1, "reason": "reload"},
        # The retired instance still receives events; none of them may touch the file.
        {"event": "agent_settled", "runtime": 0, "idle": False},
        {"event": "input", "runtime": 0, "editor": "stale draft"},
    ])
    replacement = results[1]["state"]
    assert replacement is not None
    assert replacement["runtime_instance_id"] != results[0]["state"]["runtime_instance_id"]
    assert results[2]["state"] == replacement
    assert results[3]["state"] == replacement
    assert state_path is not None and json.loads(state_path.read_text(encoding="utf-8")) == replacement


def test_a_session_change_inside_the_launched_pi_keeps_publishing(tmp_path: Path) -> None:
    # A /resume (or /new, /fork) inside the launched Pi moves the conversation. The launch token
    # still proves this is CCB's Pi, so publishing continues - which session it is in is a
    # transcript question, not a readiness one. A previous file it never reported is irrelevant.
    results, _path = _run(tmp_path, [
        _tui(tmp_path),
        START,
        {"event": "session_start", "reason": "resume", "session": "older", "file": "/s/older.jsonl",
         "previous": "/s/never-reported.jsonl"},
    ])
    published, resumed = results[0]["state"], results[1]["state"]
    assert resumed is not None
    assert resumed["launch_token"] == TOKEN and resumed["runtime_instance_id"] == published["runtime_instance_id"]
    assert resumed["seq"] > published["seq"]
    assert resumed["idle"] is True


def test_a_runtime_without_a_session_identity_still_publishes(tmp_path: Path) -> None:
    results, _path = _run(tmp_path, [
        _tui(tmp_path),
        {**START, "session": "", "file": ""},
    ])
    state = results[0]["state"]
    assert state is not None and state["launch_token"] == TOKEN


def test_quit_retires_the_publisher(tmp_path: Path) -> None:
    results, _path = _run(tmp_path, [
        _tui(tmp_path),
        START,
        {"event": "session_shutdown", "reason": "quit"},
        {"event": "agent_settled"},
        {"event": "session_start", "reason": "startup"},
    ])
    published = results[0]["state"]
    assert results[1]["state"] == published
    assert results[2]["state"] == published
    assert results[3]["state"] == published


def test_without_ccbs_state_channel_the_extension_stays_inert(tmp_path: Path) -> None:
    results, _path = _run(tmp_path, [_tui(tmp_path), START], state_channel=False)
    assert not (tmp_path / "rt" / "pi-state.json").exists()
    assert results[0]["state"] is None


def test_a_missing_token_leaves_it_inert(tmp_path: Path) -> None:
    results, _path = _run(tmp_path, [_tui(tmp_path), START], token=None)
    assert not (tmp_path / "rt" / "pi-state.json").exists()
    assert results[0]["state"] is None


def test_a_non_tui_runtime_never_publishes(tmp_path: Path) -> None:
    # Print and rpc runtimes have no pane terminal: they must not claim, whatever ccbs reader
    # says about a snapshot that claims a non-tui mode (that stays a refusal on the reader side).
    results, _path = _run(tmp_path, [
        {"op": "runtime", "cwd": str(tmp_path / "print"), "mode": "print"},
        START,
        {"op": "runtime", "cwd": str(tmp_path / "rpc"), "mode": "rpc"},
        {"event": "session_start", "runtime": 1, "reason": "startup"},
    ])
    assert results[0]["state"] is None
    assert results[1]["state"] is None
