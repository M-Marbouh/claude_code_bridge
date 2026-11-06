from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ACTIVITY = ROOT / "config" / "ccb-activity.py"

PROJECT = "proj-current"
OTHER_PROJECT = "proj-foreign"
LAUNCH = "ai-100-1"


def _load_activity_module():
    loader = importlib.machinery.SourceFileLoader("ccb_activity_under_test", str(ACTIVITY))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _write_record(home: Path, project_id: str, launch_id: str, work_dir: Path) -> None:
    run_dir = home / ".ccb" / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"ccb-session-{launch_id}.json").write_text(
        json.dumps(
            {
                "ccb_session_id": launch_id,
                "ccb_project_id": project_id,
                "work_dir": str(work_dir),
                "terminal": "tmux",
                "updated_at": int(time.time()),
                "providers": {"codex": {"pane_id": "%1"}},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def activity_env(tmp_path, monkeypatch):
    # `test/conftest.py` already isolates HOME to this directory.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CCB_RUN_DIR", raising=False)
    runtime_tmp = tmp_path / "runtime"
    runtime_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(runtime_tmp))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    project = tmp_path / "project"
    (project / ".ccb").mkdir(parents=True)
    _write_record(home, PROJECT, LAUNCH, project)
    other = tmp_path / "foreign"
    (other / ".ccb").mkdir(parents=True)
    _write_record(home, OTHER_PROJECT, "ai-200-2", other)
    return {"home": home, "project": project, "other": other, "tmp": tmp_path}


def _queues(project_id: str = PROJECT, launch_id: str = LAUNCH) -> list[dict]:
    return [
        {
            "provider": "claude",
            "session_key": f"claude:{project_id}",
            "in_flight": {"req_id": "r1", "running_s": 1.0, "progress": {}},
            "waiting": 2,
        },
        # Duplicate Codex: two routed workers of the same launch both count.
        {
            "provider": "codex",
            "session_key": f"codex:launch:{launch_id}:live:a",
            "in_flight": {"req_id": "r2", "running_s": 2.0, "progress": {}},
            "waiting": 1,
        },
        {
            "provider": "codex",
            "session_key": f"codex:launch:{launch_id}:live:b",
            "in_flight": None,
            "waiting": 2,
        },
        # Foreign project, foreign launch, peer and unknown keys: excluded.
        {
            "provider": "codex",
            "session_key": f"codex:{OTHER_PROJECT}",
            "in_flight": {"req_id": "r3"},
            "waiting": 9,
        },
        {
            "provider": "claude",
            "session_key": "claude:launch:ai-200-2:live:x",
            "in_flight": {"req_id": "r4"},
            "waiting": 9,
        },
        {
            "provider": "codex",
            "session_key": 'peer:["codex","tmux","%1","marker"]',
            "in_flight": {"req_id": "r5"},
            "waiting": 9,
        },
        {
            "provider": "codex",
            "session_key": "codex:unknown",
            "in_flight": {"req_id": "r6"},
            "waiting": 9,
        },
    ]


FAKE_STATE_FILE = Path("/fake/askd.json")


def _install_fake_daemon(
    monkeypatch,
    queues=None,
    error=None,
    requests=None,
    discoveries=None,
    delay=0.0,
    state_file=None,
    state=None,
):
    """Fake the daemon selection and queue RPC at the `askd_rpc` seam."""
    import askd_rpc
    import askd_runtime

    selected = state_file if state_file is not None else FAKE_STATE_FILE
    fake_state = dict(state or {"token": "t", "pid": 1, "port": 2})

    def _find(*_args, **_kwargs):
        if discoveries is not None:
            discoveries.append(1)
        return selected

    def _request(_state, request, **_kwargs):
        if requests is not None:
            requests.append(request)
        if delay:
            time.sleep(delay)
        if error is not None:
            raise error
        return {
            "type": "ask.response",
            "id": request.get("id"),
            "exit_code": 0,
            "queues": list(queues or []),
        }

    monkeypatch.setattr(askd_runtime, "find_running_state_file", _find)
    monkeypatch.setattr(askd_rpc, "read_state", lambda _path: dict(fake_state))
    monkeypatch.setattr(askd_rpc, "request_daemon", _request)


def test_activity_aggregates_only_the_focused_projects_queues(activity_env, monkeypatch, capsys):
    module = _load_activity_module()
    _install_fake_daemon(monkeypatch, _queues())

    assert module.main(["%1", str(activity_env["project"])]) == 0

    assert capsys.readouterr().out == "C 1▶2  X 1▶3\n"


def test_activity_scopes_to_the_panes_own_project(activity_env, monkeypatch, capsys):
    module = _load_activity_module()
    _install_fake_daemon(monkeypatch, _queues())

    # The other project's pane sees only its own two queues, never the five
    # belonging to the current project.
    assert module.main(["%9", str(activity_env["other"])]) == 0
    assert capsys.readouterr().out == "C 1▶9  X 1▶9\n"


def test_a_subdirectory_maps_to_the_project_and_is_never_hashed(activity_env, monkeypatch, capsys):
    module = _load_activity_module()
    _install_fake_daemon(monkeypatch, _queues())
    nested = activity_env["project"] / "src" / "deep"
    nested.mkdir(parents=True)
    # The nested path would hash to something else if it were trusted alone.
    from project_id import compute_ccb_project_id

    assert compute_ccb_project_id(nested) != PROJECT

    assert module.main(["%1", str(nested)]) == 0
    assert capsys.readouterr().out == "C 1▶2  X 1▶3\n"


def test_a_path_outside_any_recorded_project_renders_nothing(activity_env, monkeypatch, capsys):
    module = _load_activity_module()
    discoveries: list[int] = []
    _install_fake_daemon(monkeypatch, _queues(), discoveries=discoveries)
    stray = activity_env["tmp"] / "not-a-project"
    stray.mkdir()

    assert module.main(["%1", str(stray)]) == 0
    assert capsys.readouterr().out == ""
    assert discoveries == []


def test_inherited_run_dir_is_not_trusted(activity_env, monkeypatch, capsys):
    module = _load_activity_module()
    discoveries: list[int] = []
    _install_fake_daemon(monkeypatch, _queues(), discoveries=discoveries)
    bogus = activity_env["tmp"] / "bogus-run"
    bogus.mkdir()
    monkeypatch.setenv("CCB_RUN_DIR", str(bogus))

    assert module.main(["%1", str(activity_env["project"])]) == 0

    assert capsys.readouterr().out == "C 1▶2  X 1▶3\n"
    assert discoveries == [1]
    # The helper restores the variable for its in-process caller.
    assert os.environ.get("CCB_RUN_DIR") == str(bogus)


def test_idle_providers_render_nothing(activity_env, monkeypatch, capsys):
    module = _load_activity_module()
    _install_fake_daemon(
        monkeypatch,
        [
            {"provider": "codex", "session_key": f"codex:{PROJECT}", "in_flight": None, "waiting": 0},
        ],
    )

    assert module.main(["%1", str(activity_env["project"])]) == 0
    assert capsys.readouterr().out == ""


def test_cache_serves_results_and_empty_results(activity_env, monkeypatch, capsys):
    module = _load_activity_module()
    requests: list[dict] = []
    monkeypatch.setenv("CCB_ACTIVITY_CACHE_S", "60")
    _install_fake_daemon(monkeypatch, [], requests=requests)

    assert module.main(["%1", str(activity_env["project"])]) == 0
    assert capsys.readouterr().out == ""
    assert len(requests) == 1

    # Activity appears, but the cached empty result is still fresh.
    _install_fake_daemon(monkeypatch, _queues(), requests=requests)
    assert module.main(["%1", str(activity_env["project"])]) == 0
    assert capsys.readouterr().out == ""
    assert len(requests) == 1


def test_cache_is_keyed_by_project_and_daemon_identity(activity_env, monkeypatch, capsys):
    module = _load_activity_module()
    monkeypatch.setenv("CCB_ACTIVITY_CACHE_S", "60")
    _install_fake_daemon(monkeypatch, _queues())

    assert module.main(["%1", str(activity_env["project"])]) == 0
    capsys.readouterr()
    assert module._cache_path(PROJECT, "daemon-a") != module._cache_path(PROJECT, "daemon-b")
    assert module._cache_path(PROJECT, "daemon-a") != module._cache_path(OTHER_PROJECT, "daemon-a")


def test_an_error_clears_stale_activity(activity_env, monkeypatch, capsys):
    module = _load_activity_module()
    monkeypatch.setenv("CCB_ACTIVITY_CACHE_S", "60")
    _install_fake_daemon(monkeypatch, _queues())

    assert module.main(["%1", str(activity_env["project"])]) == 0
    assert "X 1▶3" in capsys.readouterr().out
    cache_path = module._cache_path(PROJECT, module._identity(FAKE_STATE_FILE, {"pid": 1, "port": 2}))
    assert cache_path is not None and cache_path.is_file()

    # Expire the entry, then make the daemon fail: the old counts must not linger.
    cache_path.write_text(f"{int(time.time()) - 120}\nC 1▶2  X 1▶3\n", encoding="utf-8")
    _install_fake_daemon(monkeypatch, error=RuntimeError("daemon gone"))

    assert module.main(["%2", str(activity_env["project"])]) == 0
    assert capsys.readouterr().out == ""
    stored = cache_path.read_text(encoding="utf-8").split("\n", 1)
    assert stored[1].strip() == ""


def test_dead_or_unreachable_daemon_renders_nothing(activity_env, monkeypatch, capsys):
    module = _load_activity_module()
    _install_fake_daemon(monkeypatch, error=RuntimeError("Unified askd daemon state is unavailable"))

    started = time.monotonic()
    assert module.main(["%1", str(activity_env["project"])]) == 0
    elapsed = time.monotonic() - started

    assert capsys.readouterr().out == ""
    assert elapsed < 1.0


def test_a_slow_daemon_is_bounded_by_the_deadline(activity_env, monkeypatch, capsys):
    module = _load_activity_module()
    monkeypatch.setenv("CCB_ACTIVITY_DEADLINE_S", "0.2")
    _install_fake_daemon(monkeypatch, _queues(), delay=2.0)

    started = time.monotonic()
    assert module.main(["%1", str(activity_env["project"])]) == 0
    elapsed = time.monotonic() - started

    assert capsys.readouterr().out == ""
    assert elapsed < 1.0


def _fake_daemon_server() -> tuple[socket.socket, threading.Thread, int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]

    def serve() -> None:
        while True:
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                try:
                    request = json.loads(data.split(b"\n")[0])
                except Exception:
                    continue
                if str(request.get("type", "")).endswith(".ping"):
                    response = {"type": "ask.pong", "v": 1, "id": request.get("id"), "exit_code": 0}
                else:
                    response = {
                        "type": "ask.response",
                        "v": 1,
                        "id": request.get("id"),
                        "exit_code": 0,
                        "reply": "",
                        "queues": _queues(),
                    }
                conn.sendall((json.dumps(response) + "\n").encode("utf-8"))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return server, thread, port


def test_runs_from_the_installed_layout(activity_env, tmp_path):
    """The helper must work from ~/.local/bin + ~/.local/share/codex-dual/lib."""
    home = activity_env["home"]
    prefix = home / ".local" / "share" / "codex-dual"
    shutil.copytree(
        ROOT / "lib",
        prefix / "lib",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    installed_bin = home / ".local" / "bin"
    installed_bin.mkdir(parents=True)
    installed_helper = installed_bin / "ccb-activity.py"
    shutil.copy2(ACTIVITY, installed_helper)
    installed_helper.chmod(0o755)

    server, _thread, port = _fake_daemon_server()
    cache_root = home / ".cache" / "ccb"
    cache_root.mkdir(parents=True)
    (cache_root / "askd.json").write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": port,
                "token": "daemon-token",
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TMPDIR": str(activity_env["tmp"] / "runtime"),
        "TERM": "xterm-256color",
    }
    try:
        result = subprocess.run(
            [sys.executable, str(installed_helper), "%1", str(activity_env["project"])],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        server.close()

    assert result.returncode == 0, result.stderr
    assert result.stdout == "C 1▶2  X 1▶3\n"


# --- C2 corrections ----------------------------------------------------------

@pytest.mark.parametrize("pane_path", ["", "nonexistent"])
def test_a_missing_or_nonexistent_pane_path_never_queries_a_daemon(
    activity_env, monkeypatch, capsys, pane_path
):
    module = _load_activity_module()
    discoveries: list[int] = []
    requests: list[dict] = []
    _install_fake_daemon(monkeypatch, _queues(), discoveries=discoveries, requests=requests)
    path = "" if pane_path == "" else str(activity_env["tmp"] / "does-not-exist")

    assert module.main(["%1", path]) == 0

    assert capsys.readouterr().out == ""
    # Even with a recorded active project present, an unusable pane path must
    # stop before discovery or any queue RPC.
    assert discoveries == []
    assert requests == []


def test_the_deadline_empties_output_when_discovery_is_slow(activity_env, monkeypatch, capsys):
    module = _load_activity_module()
    import askd_rpc
    from project_id import compute_ccb_project_id

    monkeypatch.setenv("CCB_ACTIVITY_DEADLINE_S", "0.1")
    project = activity_env["project"]
    project_key = compute_ccb_project_id(project)[:16] or "unknown"
    cache_root = activity_env["home"] / ".cache" / "ccb"
    project_state = cache_root / "projects" / project_key / "askd.json"
    legacy_state = cache_root / "askd.json"
    project_state.parent.mkdir(parents=True, exist_ok=True)
    project_state.write_text(
        json.dumps({"host": "127.0.0.1", "port": 111, "token": "a", "pid": 11}), encoding="utf-8"
    )
    legacy_state.write_text(
        json.dumps({"host": "127.0.0.1", "port": 222, "token": "b", "pid": 22}), encoding="utf-8"
    )

    # Real `ping_daemon` (which catches `Exception` and turns a failure into
    # "not reachable") driving a slow transport: only the LAST candidate
    # answers. On the old code the swallowed deadline let discovery continue
    # past the budget, find that daemon and print counts after expiry.
    def _slow_request(state, request, **_kwargs):
        time.sleep(0.3)
        if str(request.get("type", "")).endswith(".ping"):
            if state.get("port") == 222:
                return {"type": "ask.pong", "id": request.get("id"), "exit_code": 0}
            raise OSError("connection refused")
        return {
            "type": "ask.response",
            "id": request.get("id"),
            "exit_code": 0,
            "queues": _queues(),
        }

    monkeypatch.setattr(askd_rpc, "request_daemon", _slow_request)

    started = time.monotonic()
    assert module.main(["%1", str(project)]) == 0
    elapsed = time.monotonic() - started

    assert capsys.readouterr().out == ""
    assert elapsed < 0.5


def test_cache_follows_the_reachable_daemon_not_the_first_state_file(
    activity_env, monkeypatch, capsys
):
    module = _load_activity_module()
    import askd_rpc
    from project_id import compute_ccb_project_id

    project = activity_env["project"]
    project_key = compute_ccb_project_id(project)[:16] or "unknown"
    cache_root = activity_env["home"] / ".cache" / "ccb"
    project_state = cache_root / "projects" / project_key / "askd.json"
    legacy_state = cache_root / "askd.json"
    project_state.parent.mkdir(parents=True, exist_ok=True)
    project_state.write_text(
        json.dumps({"host": "127.0.0.1", "port": 111, "token": "a", "pid": 11}), encoding="utf-8"
    )
    legacy_state.write_text(
        json.dumps({"host": "127.0.0.1", "port": 222, "token": "b", "pid": 22}), encoding="utf-8"
    )

    reachable = {"project": False}

    def _ping(protocol_prefix, timeout_s, state_file):
        if Path(str(state_file)) == project_state:
            return reachable["project"]
        return Path(str(state_file)) == legacy_state

    monkeypatch.setattr(askd_rpc, "ping_daemon", _ping)

    def _request(state, request, **_kwargs):
        waiting = 5 if state.get("port") == 222 else 7
        return {
            "type": "ask.response",
            "id": request.get("id"),
            "exit_code": 0,
            "queues": [
                {
                    "provider": "codex",
                    "session_key": f"codex:{PROJECT}",
                    "in_flight": None,
                    "waiting": waiting,
                }
            ],
        }

    monkeypatch.setattr(askd_rpc, "request_daemon", _request)
    monkeypatch.setenv("CCB_ACTIVITY_CACHE_S", "60")

    # Project-scoped state file exists but is dead; the legacy daemon answers.
    assert module.main(["%1", str(project)]) == 0
    assert capsys.readouterr().out == "X 0▶5\n"
    legacy_identity = module._identity(legacy_state, {"pid": 22, "port": 222})
    project_identity = module._identity(project_state, {"pid": 11, "port": 111})
    legacy_cache = module._cache_path(PROJECT, legacy_identity)
    project_cache = module._cache_path(PROJECT, project_identity)
    assert legacy_cache is not None and legacy_cache.is_file()
    assert project_cache is not None and not project_cache.exists()

    # The project-scoped daemon comes up. The entry cached under the other
    # daemon must never be served for it.
    reachable["project"] = True
    monkeypatch.setenv("CCB_ACTIVITY_CACHE_S", "0")
    assert module.main(["%1", str(project)]) == 0
    assert capsys.readouterr().out == "X 0▶7\n"
