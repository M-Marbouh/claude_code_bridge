from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from test_ccb_tmux_split import _load_ccb_module


ROOT = Path(__file__).resolve().parents[1]
ON = ROOT / "config" / "ccb-tmux-on.sh"
OFF = ROOT / "config" / "ccb-tmux-off.sh"
# Golden comparisons run against the last commit before the lifecycle rewrite,
# so they keep meaning after this change is committed.
BASELINE = "9d07c4d"
RECORDED_STATUS_RIGHT = '#{?window_bigger,[#{window_offset_x}#,#{window_offset_y}] ,}"#{=21:pane_title}" %H:%M %d-%b-%y  #(python3 /tmp/fixture-widget.py --unicode)'


@pytest.fixture
def tmux_server(tmp_path):
    binary = shutil.which("tmux")
    if not binary:
        pytest.skip("tmux is not installed")
    socket = "t-" + uuid.uuid4().hex[:16]
    socket_root = ROOT / (".t-" + uuid.uuid4().hex[:6])
    socket_root.mkdir()
    server_env = {**os.environ, "TMUX_TMPDIR": str(socket_root)}
    def run(*args, check=True, **kwargs):
        return subprocess.run([binary, "-L", socket, *map(str, args)], check=check,
                              capture_output=True, text=True, env=server_env, **kwargs)
    started = run("-f", "/dev/null", "new-session", "-d", "-x", "200", "-y", "50", "-s", "fixture", "-c", "/tmp", check=False)
    if started.returncode:
        shutil.rmtree(socket_root, ignore_errors=True)
        pytest.skip("sandbox cannot create a disposable tmux server")
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "tmux"
    shim.write_text(f"#!/bin/sh\nexec {binary} -L {socket} \"$@\"\n", encoding="utf-8")
    shim.chmod(0o755)
    pane = run("display-message", "-p", "#{pane_id}").stdout.strip()
    session = run("display-message", "-p", "#{session_id}").stdout.strip()
    window = run("display-message", "-p", "#{window_id}").stdout.strip()
    env = {**server_env, "PATH": f"{shim_dir}:{os.environ['PATH']}", "TMUX": f"isolated,{os.getpid()},0",
           "TMUX_PANE": pane, "CCB_THEME": "dark", "CCB_RUNTIME_TMP": str(tmp_path)}
    try:
        yield run, env, session, window
    finally:
        run("kill-server", check=False)
        shutil.rmtree(socket_root, ignore_errors=True)


def value(run, scope, target, option):
    flags = "-qv" if scope == "session" else "-wqv"
    result = run("show-options", flags, "-t", target, option, check=False)
    return result.stdout.rstrip("\n") if result.returncode == 0 else ""


def test_inherited_theme_round_trip_keeps_status_rows(tmux_server):
    run, env, session, window = tmux_server
    run("set-hook", "-g", "after-select-pane", 'display-message "global inherited"')
    global_hook = run("show-hooks", "-g", "after-select-pane").stdout
    before = {key: value(run, "session", session, key) for key in
              ("status", "status-position", "status-left", "status-right", "status-format[0]", "status-format[1]")}
    rendered_before = run("display-message", "-p", "-t", session, "#{T:status-format[0]}").stdout.rstrip("\n")
    assert subprocess.run([str(ON), "--session", session, "--window", window, "--owner", "owner"], env=env).returncode == 0
    assert subprocess.run([str(OFF), "--session", session, "--window", window, "--owner", "owner"], env=env).returncode == 0
    after = {key: value(run, "session", session, key) for key in before}
    assert after == before
    assert not run("show-options", "-q", "-t", session, "status-format").stdout
    rendered_after = run("display-message", "-p", "-t", session, "#{T:status-format[0]}").stdout.rstrip("\n")
    assert rendered_after == rendered_before
    assert not run("show-hooks", "-t", session, "after-select-pane").stdout
    assert run("show-hooks", "-g", "after-select-pane").stdout == global_hook
    assert run("show-options", "-gv", "status-format[0]").stdout.strip()
    assert run("show-options", "-gv", "status-format[1]").stdout.strip()


def test_idempotence_wrong_owner_and_off_without_snapshot(tmux_server):
    run, env, session, window = tmux_server
    assert subprocess.run([str(OFF), "--session", session], env=env).returncode == 0
    on = [str(ON), "--session", session, "--window", window, "--owner", "owner"]
    off = [str(OFF), "--session", session, "--window", window, "--owner", "owner"]
    assert subprocess.run(on, env=env).returncode == 0
    assert subprocess.run(on, env=env).returncode == 3
    assert subprocess.run([str(OFF), "--session", session, "--owner", "foreign"], env=env).returncode == 0
    assert run("show-options", "-qv", "-t", session, "@ccb_active").stdout.strip() == "owner"
    assert subprocess.run(off, env=env).returncode == 0
    assert subprocess.run(off, env=env).returncode == 0


def test_local_values_and_hooks_round_trip_on_captured_window(tmux_server):
    run, env, session, window = tmux_server
    run("set-option", "-t", session, "status-right", RECORDED_STATUS_RIGHT)
    run("set-hook", "-g", "after-select-pane", 'display-message "global stays"')
    run("set-hook", "-t", session, "after-select-pane", 'display-message "first # \\"')
    run("set-hook", "-ag", "-t", session, "after-select-pane", 'display-message "second \\"')
    run("set-window-option", "-t", window, "pane-border-format", 'local "quoted" # value')
    before = (value(run, "session", session, "status-right"),
              run("show-hooks", "-t", session, "after-select-pane").stdout,
              value(run, "window", window, "pane-border-format"),
              run("show-hooks", "-g", "after-select-pane").stdout)
    args = ["--session", session, "--window", window, "--owner", "owner"]
    assert subprocess.run([str(ON), *args], env=env).returncode == 0
    assert subprocess.run([str(OFF), *args], env=env).returncode == 0
    after = (value(run, "session", session, "status-right"),
             run("show-hooks", "-t", session, "after-select-pane").stdout,
             value(run, "window", window, "pane-border-format"),
             run("show-hooks", "-g", "after-select-pane").stdout)
    assert after == before


def test_theme_targets_captured_window(tmux_server):
    run, env, session, first = tmux_server
    second = run("new-window", "-t", session, "-P", "-F", "#{window_id}").stdout.strip()
    run("set-window-option", "-t", second, "pane-border-format", "other-window")
    args = ["--session", session, "--window", first, "--owner", "owner"]
    assert subprocess.run([str(ON), *args], env=env).returncode == 0
    run("select-window", "-t", second)
    other_pane = run("display-message", "-p", "#{pane_id}").stdout.strip()
    border = ROOT / "config" / "ccb-border.sh"
    assert subprocess.run([str(border), other_pane], env=env).returncode == 0
    assert run("show-options", "-wqv", "-t", second, "@ccb_prev_pabs_set").stdout.strip() == "1"
    assert subprocess.run([str(OFF), *args], env=env).returncode == 0
    assert value(run, "window", second, "pane-border-format").endswith("other-window")
    assert not run("show-options", "-wq", "-t", second, "pane-active-border-style").stdout
    assert not run("show-options", "-wq", "-t", second, "@ccb_prev_pabs_set").stdout


def test_concurrent_activation_has_single_owner(tmux_server):
    run, env, session, window = tmux_server
    original = value(run, "session", session, "status-right")
    args = [str(ON), "--session", session, "--window", window, "--owner", "owner"]
    first = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    second = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    codes = sorted((first.wait(), second.wait()))
    assert codes == [0, 3]
    assert run("show-options", "-qv", "-t", session, "@ccb_active").stdout.strip() == "owner"
    off = [str(OFF), "--session", session, "--window", window, "--owner", "owner"]
    assert subprocess.run(off, env=env).returncode == 0
    assert value(run, "session", session, "status-right") == original


def test_midlifecycle_failure_rolls_back_and_allows_retry(tmux_server, tmp_path):
    run, env, session, window = tmux_server
    original = value(run, "session", session, "status-right")
    marker = tmp_path / "failed-once"
    fail_dir = tmp_path / "no-flock-bin"
    fail_dir.mkdir()
    tmux_shim = Path(env["PATH"].split(":", 1)[0]) / "tmux"
    shim = fail_dir / "tmux"
    shim.write_text(
        f'#!/bin/sh\ncase "$*" in *" status-right "*) if [ ! -e "{marker}" ]; then touch "{marker}"; exit 41; fi;; esac\nexec "{tmux_shim}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    env = {**env, "PATH": f"{fail_dir}:{env['PATH']}"}
    args = ["--session", session, "--window", window, "--owner", "owner"]
    failed = subprocess.run([str(ON), *args], env=env, capture_output=True, text=True)
    assert failed.returncode != 0
    assert value(run, "session", session, "status-right") == original
    assert subprocess.run([str(ON), *args], env={**env, "PATH": env["PATH"].split(":", 1)[1]}).returncode == 0
    assert subprocess.run([str(OFF), *args], env=env).returncode == 0


def test_fallback_lock_without_flock(tmux_server, tmp_path):
    run, env, session, window = tmux_server
    fake_path = tmp_path / "without-flock"
    fake_path.mkdir()
    shim = Path(env["PATH"].split(":", 1)[0]) / "tmux"
    for command in ("bash", "base64", "tr", "mkdir", "cat", "kill", "sleep", "dirname", "date", "stat", "rm", "grep"):
        source = shutil.which(command)
        if source:
            (fake_path / command).symlink_to(source)
    (fake_path / "tmux").symlink_to(shim)
    fallback_env = {**env, "PATH": str(fake_path)}
    assert not (fake_path / "flock").exists()
    assert not run("show-options", "-q", "-t", session, "status").stdout
    args = ["--session", session, "--window", window, "--owner", "owner"]
    assert subprocess.run([str(ON), *args], env=fallback_env).returncode == 0
    # A silent no-op activation must fail here: ownership and theme must both be applied.
    assert run("show-options", "-qv", "-t", session, "@ccb_active").stdout.strip() == "owner"
    assert value(run, "session", session, "status") == "2"
    assert not list((Path(env["CCB_RUNTIME_TMP"]) / "ccb-tmux-locks").glob("*.d"))
    assert subprocess.run([str(OFF), *args], env=fallback_env).returncode == 0
    assert not run("show-options", "-qv", "-t", session, "@ccb_active").stdout.strip()
    assert not run("show-options", "-q", "-t", session, "status").stdout


def test_status_right_extension_is_appended_once(tmux_server, tmp_path):
    run, env, session, window = tmux_server
    env = {**env, "PATH": f"{env['PATH']}:{shutil.which('tmux') or ''}"}
    args = ["--session", session, "--window", window, "--owner", "owner"]
    assert subprocess.run([str(ON), *args], env=env).returncode == 0
    current = run("show-options", "-qv", "-t", session, "status-right").stdout.rstrip("\n")
    expected = ('#[fg=#f38ba8,bg=#1e1e2e]#[fg=#1e1e2e,bg=#f38ba8,bold] #{?#{@ccb_agent},#{@ccb_agent},-} '
                '#[fg=#cba6f7,bg=#f38ba8]#[fg=#1e1e2e,bg=#cba6f7,bold] CCB:#{@ccb_version} '
                f'#[fg=#89b4fa,bg=#cba6f7]#[fg=#cdd6f4,bg=#89b4fa] #({ROOT / "config" / "ccb-status.sh"} modern) '
                '#[fg=#fab387,bg=#89b4fa]#[fg=#1e1e2e,bg=#fab387,bold] %m/%d %a %H:%M #[default]'
                '#{T:@ccb_status_right_extra}')
    assert current == expected
    assert current.endswith("#[default]#{T:@ccb_status_right_extra}")
    assert current.count("#{T:@ccb_status_right_extra}") == 1
    run("set-option", "-g", "@ccb_status_right_extra", "GLOBAL_WIDGET")
    assert run("display-message", "-p", "#{T:status-right}").stdout.strip().endswith("GLOBAL_WIDGET")
    run("set-option", "-t", session, "@ccb_status_right_extra", "SESSION_WIDGET")
    assert run("display-message", "-p", "#{T:status-right}").stdout.strip().endswith("SESSION_WIDGET")
    conf = tmp_path / "reload.conf"
    conf.write_text("set -g @ccb_status_right_extra RELOADED_WIDGET\n", encoding="utf-8")
    run("source-file", conf)
    assert run("display-message", "-p", "#{T:status-right}").stdout.strip().endswith("SESSION_WIDGET")
    run("set-option", "-u", "-t", session, "@ccb_status_right_extra")
    assert run("display-message", "-p", "#{T:status-right}").stdout.strip().endswith("RELOADED_WIDGET")
    assert subprocess.run([str(OFF), *args], env=env).returncode == 0


def test_widget_command_renders_for_attached_client(tmux_server, tmp_path):
    run, env, session, window = tmux_server
    if not shutil.which("script") or not shutil.which("timeout"):
        pytest.skip("script and timeout are required to capture an attached tmux client")
    widget = tmp_path / "fixture-widget.py"
    widget.write_text("#!/usr/bin/env python3\nprint('WIDGET_RENDERED')\n", encoding="utf-8")
    widget.chmod(0o755)
    run("set-option", "-g", "@ccb_status_right_extra", f" #({widget}) ")
    args = ["--session", session, "--window", window, "--owner", "owner"]
    assert subprocess.run([str(ON), *args], env=env).returncode == 0
    captured = subprocess.run(
        # Keep the client's stdin open (an EOF stdin detaches it before the status job renders).
        [shutil.which("timeout"), "5", "sh", "-c", "sleep 6 | script -q -c 'tmux attach-session -t fixture' /dev/null"],
        env={**{k: v for k, v in env.items() if k != "TMUX"}, "TERM": "xterm-256color"},
        capture_output=True, text=True, check=False,
    )
    screen = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()=>][0-9A-Za-z]?", "", captured.stdout)
    assert "WIDGET_RENDERED" in screen
    assert subprocess.run([str(OFF), *args], env=env).returncode == 0


def test_launcher_only_cleans_up_owned_theme(monkeypatch, tmp_path):
    ccb = _load_ccb_module()
    home_bin = tmp_path / ".local" / "bin"
    home_bin.mkdir(parents=True)
    (home_bin / "ccb-tmux-on.sh").touch()
    (home_bin / "ccb-tmux-off.sh").touch()
    monkeypatch.setattr(ccb.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("TMUX", "fixture")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "display-message" in args:
            return type("Result", (), {"stdout": "$0 @0", "returncode": 0})()
        return type("Result", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr(ccb.subprocess, "run", fake_run)
    launcher = type("Launcher", (), {"terminal_type": "tmux", "session_id": "launch-token"})()
    launcher._set_tmux_ui_active = ccb.AILauncher._set_tmux_ui_active.__get__(launcher)
    launcher._tmux_ui_owned = False
    launcher._set_tmux_ui_active(False)
    assert calls == []
    launcher._tmux_ui_owned = True
    launcher._tmux_ui_target = ["$0", "@0"]
    launcher._set_tmux_ui_active(False)
    assert calls[-1] == [str(home_bin / "ccb-tmux-off.sh"), "--session", "$0", "--window", "@0", "--owner", "launch-token"]


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_theme_matches_head_except_status_extra(tmux_server, tmp_path, theme):
    if not _baseline_available():
        pytest.skip(f"baseline commit {BASELINE} not in this clone")
    run, env, session, window = tmux_server
    env = {**env, "CCB_THEME": theme}
    binary = shutil.which("tmux")
    assert binary
    old_socket = "h-" + uuid.uuid4().hex[:12]
    old_socket_root = ROOT / (".h-" + uuid.uuid4().hex[:6])
    old_socket_root.mkdir()
    old_server_env = {**os.environ, "TMUX_TMPDIR": str(old_socket_root)}

    def old_run(*args, check=True):
        return subprocess.run([binary, "-L", old_socket, *map(str, args)], env=old_server_env,
                              check=check, capture_output=True, text=True)

    started = old_run("-f", "/dev/null", "new-session", "-d", "-x", "200", "-y", "50",
                      "-s", "head-fixture", "-c", "/tmp", check=False)
    if started.returncode:
        shutil.rmtree(old_socket_root, ignore_errors=True)
        pytest.skip("sandbox cannot create the second disposable tmux server")
    second_session = old_run("display-message", "-p", "#{session_id}").stdout.strip()
    second_window = old_run("display-message", "-p", "-t", second_session, "#{window_id}").stdout.strip()
    current_pane = run("display-message", "-p", "-t", window, "#{pane_id}").stdout.strip()
    old_pane = old_run("display-message", "-p", "-t", second_window, "#{pane_id}").stdout.strip()
    run("set-option", "-p", "-t", current_pane, "@ccb_agent", "Gemini")
    old_run("set-option", "-p", "-t", old_pane, "@ccb_agent", "Gemini")

    original_dir = tmp_path / "head-config"
    original_dir.mkdir()
    for name in ("ccb-tmux-on.sh", "ccb-border.sh", "ccb-status.sh", "ccb-git.sh"):
        source = "config/" + name
        data = subprocess.run(["git", "show", f"{BASELINE}:" + source], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout
        target = original_dir / name
        target.write_text(data, encoding="utf-8")
        target.chmod(0o755)
    common_bin = tmp_path / "common-bin"
    common_bin.mkdir()
    (common_bin / "ccb").write_text("#!/bin/sh\necho vtest\n", encoding="utf-8")
    (common_bin / "ccb").chmod(0o755)
    old_tmux_shim = original_dir / "tmux"
    old_tmux_shim.write_text(f'#!/bin/sh\nexec "{binary}" -L "{old_socket}" "$@"\n', encoding="utf-8")
    old_tmux_shim.chmod(0o755)
    head_env = {**old_server_env, "TMUX": f"isolated,{os.getpid()},0", "TMUX_PANE": old_pane,
                "CCB_THEME": theme, "PATH": f"{original_dir}:{common_bin}:{os.environ['PATH']}",
                "CCB_RUNTIME_TMP": str(tmp_path)}
    try:
        result = subprocess.run([str(original_dir / "ccb-tmux-on.sh")], env=head_env,
                                capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr

        current_dir = tmp_path / "current-config"
        current_dir.mkdir()
        for name in ("ccb-tmux-on.sh", "ccb-tmux-off.sh", "ccb-border.sh", "ccb-status.sh", "ccb-git.sh"):
            target = current_dir / name
            target.write_text((ROOT / "config" / name).read_text(encoding="utf-8"), encoding="utf-8")
            target.chmod(0o755)
        current_env = {**env, "TMUX_PANE": current_pane, "PATH": f"{common_bin}:{env['PATH']}"}
        result = subprocess.run([str(current_dir / "ccb-tmux-on.sh"), "--session", session, "--window", window, "--owner", "golden"],
                                env=current_env, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr

        session_opts = ("status", "status-position", "status-justify", "status-interval", "status-style",
                        "status-format[0]", "status-format[1]", "status-left", "status-left-length", "status-right",
                        "status-right-length")
        window_opts = ("pane-border-status", "pane-border-format", "pane-border-style",
                       "pane-active-border-style", "window-status-format", "window-status-current-format",
                       "window-status-separator")

        def options(execute, target_session, target_window, pane):
            vals = {key: execute("show-options", "-qv", "-t", target_session, key, check=False).stdout.rstrip("\n")
                    for key in session_opts}
            vals.update({key: execute("show-options", "-wqv", "-t", target_window, key,
                                      check=False).stdout.rstrip("\n") for key in window_opts})
            vals["pane-active-border-style@pane"] = execute("show-options", "-qv", "-p", "-t", pane,
                                                             "pane-active-border-style", check=False).stdout.rstrip("\n")
            vals["hook"] = execute("show-hooks", "-t", target_session, "after-select-pane", check=False).stdout.rstrip("\n")
            vals["status-left"] = vals["status-left"].replace(str(current_dir), "<config>").replace(str(original_dir), "<config>")
            vals["hook"] = vals["hook"].replace(str(current_dir), "<config>").replace(str(original_dir), "<config>")
            return vals

        expected = options(old_run, second_session, second_window, old_pane)
        actual = options(run, session, window, current_pane)
        expected_right = expected.pop("status-right", "").replace(str(original_dir), "<config>")
        actual_right = actual.pop("status-right", "").replace(str(current_dir), "<config>")
        assert actual == expected
        assert actual_right == expected_right + "#{T:@ccb_status_right_extra}"
    finally:
        old_run("kill-server", check=False)
        shutil.rmtree(old_socket_root, ignore_errors=True)


def test_border_styles_match_head_for_agent_and_title_cases(tmux_server, tmp_path):
    if not _baseline_available():
        pytest.skip(f"baseline commit {BASELINE} not in this clone")
    run, env, session, _window = tmux_server
    original_dir = tmp_path / "head-config"
    original_dir.mkdir()
    source = subprocess.run(["git", "show", f"{BASELINE}:config/ccb-border.sh"], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout
    original = original_dir / "ccb-border.sh"
    original.write_text(source, encoding="utf-8")
    original.chmod(0o755)
    cases = (("Codex", ""), ("Gemini", ""), ("Claude", ""), ("OpenCode", ""), ("Cmd", ""),
             ("Other", "CCB-Codex-x"), ("Other", "CCB-Gemini-x"), ("Other", "Claude-x"),
             ("Other", "CCB-OpenCode-x"), ("Other", "CCB-Cmd-x"), ("Other", "plain"))
    def effective_style(script, agent, title):
        pane = run("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", session).stdout.strip()
        run("set-option", "-p", "-t", pane, "@ccb_agent", agent)
        run("select-pane", "-T", title, "-t", pane)
        result = subprocess.run([str(script), pane], env=env, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        return run("display-message", "-p", "-t", pane, "#{pane-active-border-style}").stdout.rstrip("\n")

    outcomes = []
    for agent, title in cases:
        head_style = effective_style(original, agent, title)
        current_style = effective_style(ROOT / "config" / "ccb-border.sh", agent, title)
        assert current_style == head_style, (agent, title, head_style, current_style)
        outcomes.append(current_style)
    assert outcomes == ["fg=#ff9e64,bold", "fg=#7dcfff,bold", "fg=#bb9af7,bold", "fg=#9ece6a,bold",
                        "fg=#7dcfff,bold", "fg=#ff9e64,bold", "fg=#7dcfff,bold", "fg=#bb9af7,bold",
                        "fg=#9ece6a,bold", "fg=#7dcfff,bold", "fg=#7aa2f7,bold"]


def _baseline_available():
    return subprocess.run(["git", "cat-file", "-e", f"{BASELINE}^{{commit}}"], cwd=ROOT,
                          capture_output=True).returncode == 0


def test_border_restore_keeps_original_window_styles(tmux_server):
    """pane-active-border-style is window-scoped: border.sh must not re-theme on off."""
    run, env, session, first = tmux_server
    border = ROOT / "config" / "ccb-border.sh"
    run("set-option", "-w", "-t", first, "pane-active-border-style", "fg=yellow")
    local_win = run("new-window", "-d", "-t", session, "-P", "-F", "#{window_id}").stdout.strip()
    run("set-option", "-w", "-t", local_win, "pane-active-border-style", "fg=cyan")
    inherited_win = run("new-window", "-d", "-t", session, "-P", "-F", "#{window_id}").stdout.strip()
    args = ["--session", session, "--window", first, "--owner", "owner"]
    assert subprocess.run([str(ON), *args], env=env).returncode == 0
    for win in (first, local_win, inherited_win):
        pane = run("display-message", "-p", "-t", win, "#{pane_id}").stdout.strip()
        assert subprocess.run([str(border), pane], env=env).returncode == 0
        assert subprocess.run([str(border), pane], env=env).returncode == 0
    assert subprocess.run([str(OFF), *args], env=env).returncode == 0
    assert value(run, "window", first, "pane-active-border-style") == "fg=yellow"
    assert value(run, "window", local_win, "pane-active-border-style") == "fg=cyan"
    assert not run("show-options", "-wq", "-t", inherited_win, "pane-active-border-style").stdout


def test_off_by_session_name_shares_activation_lock_and_works_outside_tmux(tmux_server):
    run, env, session, window = tmux_server
    if not shutil.which("flock"):
        pytest.skip("flock required to hold the lock from the test")
    original = value(run, "session", session, "status-right")
    args = ["--session", session, "--window", window, "--owner", "owner"]
    assert subprocess.run([str(ON), *args], env=env).returncode == 0
    name = run("display-message", "-p", "-t", session, "#{session_name}").stdout.strip()
    server_pid = run("display-message", "-p", "#{pid}").stdout.strip()
    lock = Path(env["CCB_RUNTIME_TMP"]) / "ccb-tmux-locks" / f"{server_pid}_{session.replace('$', '_')}.lock"
    holder = subprocess.Popen(["flock", str(lock), "sleep", "3"])
    try:
        import time
        time.sleep(0.3)
        blocked = subprocess.run(["timeout", "1", str(OFF), name], env=env)
        assert blocked.returncode == 124  # waited on the canonical lock instead of racing
        assert run("show-options", "-qv", "-t", session, "@ccb_active").stdout.strip() == "owner"
    finally:
        holder.wait()
    outside = {k: v for k, v in env.items() if k not in ("TMUX", "TMUX_PANE")}
    assert subprocess.run([str(OFF), name], env=outside).returncode == 0
    assert not run("show-options", "-qv", "-t", session, "@ccb_active").stdout.strip()
    assert value(run, "session", session, "status-right") == original


def test_off_finishes_cleanup_when_launch_window_closed(tmux_server):
    run, env, session, first = tmux_server
    run("set-hook", "-t", session, "after-select-pane", 'display-message "mine"')
    keep = run("new-window", "-d", "-t", session, "-P", "-F", "#{window_id}").stdout.strip()
    args = ["--session", session, "--window", first, "--owner", "owner"]
    assert subprocess.run([str(ON), *args], env=env).returncode == 0
    run("kill-window", "-t", first)
    assert run("display-message", "-p", "-t", keep, "#{window_id}").stdout.strip() == keep
    assert subprocess.run([str(OFF), *args], env=env).returncode == 0
    assert not run("show-options", "-qv", "-t", session, "@ccb_active").stdout.strip()
    assert not run("show-options", "-qv", "-t", session, "@ccb_snapshot_complete").stdout.strip()
    hooks = run("show-hooks", "-t", session, "after-select-pane").stdout
    assert "mine" in hooks and "ccb-border" not in hooks
