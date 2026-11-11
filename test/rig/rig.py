"""Docker "machine B" rig for the cross-machine acceptance matrix (M1).

One disposable container plays the destination host:

    sshd (key only, user `b`) + tmux + a real CCB installation for `b`
    + a real `ccb` launch running stub `codex`/`claude` panes that record
    everything they receive.

The source side is this working tree, run on the host under a throw-away HOME,
so the user's real `~/.ccb`, `~/.ssh` and agent are never touched.

Nothing here is product code: the rig only drives the shipped CLIs
(`ask`, `ccb-remote`, `ccb-remote-gate`, `pend`) and reads destination-side
state (journal records, stub logs) as evidence.

M2 rows additionally use an isolated SOURCE side on the host: its own tmux
server, a fake launch registry (so the caller pane can be captured and
revalidated) and a real `askd` for `--background` polling.

Run through `test/test_docker_rig.py` with `CCB_DOCKER_RIG=1`.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RIG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "lib"))
IMAGE = "ccb-m1-rig:latest"
GATE_IN_CONTAINER = "/home/b/.local/share/codex-dual/bin/ccb-remote-gate"
ASK_IN_CONTAINER = "/home/b/.local/share/codex-dual/bin/askd"
DEST_PROJECT = "/srv/demo"
STUB_LOG = "/srv/demo/.stub-agent.log"
DEST_STUB_CONFIG = "/srv/demo/.stub-reply.json"
SOURCE_PROVIDER = "codex"
SOURCE_MARKER = "CCB-Codex-RIGA"
SOURCE_LAUNCH = "ai-rigsource-1"

PRINCIPALS = {
    # name: (issuer token, allowed aliases)
    "rig-a": ("riga1", ["demo"]),
    "rig-x": ("rigx1", []),
    "rig-b2": ("rigb2", ["demo"]),  # row 7 only: a granted, distinct issuer token
}

#: Strip the ambient CCB/tmux identity so the source side is isolated.
_STRIP_ENV = (
    "TMUX", "TMUX_PANE", "WEZTERM_PANE", "CCB_SESSION_ID", "CCB_CALLER",
    "CCB_CALLER_PANE_ID", "CCB_CALLER_TERMINAL", "CCB_MEMBER_ID", "CCB_MEMBER_TOKEN",
    "CCB_LIVE_ID", "CCB_LIVE_TOKEN", "CCB_WORK_DIR", "CCB_RUN_DIR", "CCB_ROUTE_LAUNCH_ID",
    "CCB_ROUTE_LIVE_ID", "CCB_ROUTE_TASK_ID", "CCB_SSH_BIN", "CCB_PANE_LOCK_DISABLE",
    "CCB_PANE_LOCK_TIMEOUT_S", "CCB_REMOTE_OP_TIMEOUT_S",
)


class RigError(RuntimeError):
    pass


def _run(argv: list[str], *, env: dict | None = None, timeout: float = 180, check: bool = True,
         input_text: str | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        argv, capture_output=True, text=True, env=env, timeout=timeout, input=input_text, cwd=cwd
    )
    if check and proc.returncode != 0:
        raise RigError(f"command failed ({proc.returncode}): {' '.join(argv[:4])}…\n{proc.stdout}\n{proc.stderr}")
    return proc


class Rig:
    def __init__(self, *, name_suffix: str = "", source_ref: str = "") -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccb-rig-"))
        self.container = f"ccb-m1-rig{name_suffix}-{os.getpid()}"
        #: Empty = this working tree; a git ref = that tree (M1/M2 compatibility).
        self.source_ref = source_ref
        self.port = 0
        self.pane_id = ""
        self.marker = ""
        self.live_id = ""
        self._started = False
        self._source_side: "SourceSide | None" = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> "Rig":
        self._build()
        self._run_container()
        self._pin_host_key()
        self._make_keys()
        self._copy_repo_and_install()
        self._write_authorized_keys()
        self._write_destination_config()
        self._write_source_home()
        self.launch_destination()
        self._started = True
        return self

    def stop(self) -> None:
        self.stop_source_side()
        # CCB_RIG_KEEP=1 leaves the container and temp dir behind for debugging.
        if os.environ.get("CCB_RIG_KEEP") != "1":
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, timeout=120)
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _build(self) -> None:
        if os.environ.get("CCB_RIG_SKIP_BUILD") == "1":
            return
        _run(
            ["docker", "build", "-q", "-t", IMAGE, "-f", str(RIG_DIR / "Dockerfile"), str(RIG_DIR)],
            timeout=900,
        )

    def _run_container(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, timeout=120)
        _run(
            ["docker", "run", "-d", "--name", self.container, "-p", "127.0.0.1::22", IMAGE],
            timeout=180,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            out = _run(["docker", "port", self.container, "22"], check=False)
            text = (out.stdout or "").strip()
            if text:
                self.port = int(text.rsplit(":", 1)[-1])
                break
            time.sleep(0.2)
        if not self.port:
            raise RigError("container did not publish a port")

    def _pin_host_key(self) -> None:
        pub = _run(["docker", "exec", self.container, "cat", "/etc/ssh/ssh_host_ed25519_key.pub"]).stdout
        key = " ".join(pub.split()[:2])
        (self.tmp / "known_hosts").write_text(f"[127.0.0.1]:{self.port} {key}\n", encoding="utf-8")

    def _make_keys(self) -> None:
        for name in ("a", "x", "b2", "bad", "login"):
            _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.tmp / f"id_{name}")])

    def _copy_repo_and_install(self) -> None:
        tarball = self.tmp / "src.tar"
        if self.source_ref:
            # Another commit's tree: the compatibility rows need a real M1 host.
            _run(
                ["git", "-C", str(REPO), "archive", "--format=tar",
                 "--output=" + str(tarball), self.source_ref],
                timeout=300,
            )
        else:
            _run(
                [
                    "tar", "-cf", str(tarball),
                    "--exclude=.git", "--exclude=.ccb", "--exclude=__pycache__",
                    "--exclude=:memory:.ses", "--exclude=.pytest_cache", "-C", str(REPO), ".",
                ],
                timeout=300,
            )
        _run(["docker", "cp", str(tarball), f"{self.container}:/tmp/ccb-src.tar"], timeout=300)
        self.dest_root(
            "mkdir -p /opt/ccb-src && tar -C /opt/ccb-src -xf /tmp/ccb-src.tar "
            "&& chown -R b:b /opt/ccb-src",
            timeout=300,
        )
        # The normal installer, as `b`: proves it ships the gate and the CLIs.
        _run(
            ["docker", "exec", "-u", "b", self.container, "bash", "-lc",
             "cd /opt/ccb-src && ./install.sh install"],
            timeout=900,
        )
        self.dest("test -x " + GATE_IN_CONTAINER + " && test -x " + ASK_IN_CONTAINER)

    def _write_authorized_keys(self) -> None:
        # One unrestricted line: pairing logs in with the USER'S NORMAL ssh,
        # before (and without) the restricted key it installs.
        lines = [(self.tmp / "id_login.pub").read_text().strip()]
        for principal in PRINCIPALS:
            key = (self.tmp / f"id_{'a' if principal == 'rig-a' else principal.split('-')[1]}.pub").read_text().strip()
            command = f"{GATE_IN_CONTAINER} --principal {principal}"
            lines.append(f'restrict,command="{command}" {key}')
        self.dest("mkdir -p /home/b/.ssh && chmod 700 /home/b/.ssh")
        (self.tmp / "authorized_keys").write_text("\n".join(lines) + "\n", encoding="utf-8")
        _run(["docker", "cp", str(self.tmp / "authorized_keys"), f"{self.container}:/home/b/.ssh/authorized_keys"])
        self.dest("chown b:b /home/b/.ssh/authorized_keys && chmod 600 /home/b/.ssh/authorized_keys")

    def _write_destination_config(self, *, extra: dict | None = None) -> None:
        principals = {
            name: {"issuer_token": token, "allowed_aliases": list(aliases)}
            for name, (token, aliases) in PRINCIPALS.items()
        }
        config = {
            "principals": principals,
            "aliases": {"demo": {"work_dir": DEST_PROJECT, "providers": ["codex", "claude"]}},
        }
        if extra:
            config.update(extra)
        path = self.tmp / "dest-config.json"
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        self.dest("mkdir -p /home/b/.ccb/remote")
        _run(["docker", "cp", str(path), f"{self.container}:/home/b/.ccb/remote/config.json"])
        self.dest("chown b:b /home/b/.ccb/remote/config.json && chmod 600 /home/b/.ccb/remote/config.json")

    def _write_source_home(self) -> None:
        home = self.tmp / "home"
        (home / ".ccb" / "remote").mkdir(parents=True, exist_ok=True)
        hosts = {
            "hosts": {
                "rig": self._host_entry("a", "riga1"),
                "rigx": self._host_entry("x", "rigx1"),
                "rigb": self._host_entry("b2", "rigb2"),
                "badkey": self._host_entry("bad", "riga1"),
            }
        }
        (home / ".ccb" / "remote" / "hosts.json").write_text(json.dumps(hosts, indent=2), encoding="utf-8")
        (self.tmp / "tmp").mkdir(exist_ok=True)

    def _host_entry(self, key_name: str, token: str) -> dict:
        return {
            "ssh_target": "b@127.0.0.1",
            "port": self.port,
            "identity_file": str(self.tmp / f"id_{key_name}"),
            "known_hosts": str(self.tmp / "known_hosts"),
            "issuer_token": token,
        }

    # ------------------------------------------------------------------ #
    # Destination
    # ------------------------------------------------------------------ #

    def dest(self, script: str, *, timeout: float = 180, check: bool = True) -> subprocess.CompletedProcess:
        return _run(
            ["docker", "exec", "-u", "b", self.container, "bash", "-lc", script],
            timeout=timeout, check=check,
        )

    def dest_root(self, script: str, *, timeout: float = 180, check: bool = True) -> subprocess.CompletedProcess:
        return _run(["docker", "exec", self.container, "bash", "-c", script], timeout=timeout, check=check)

    def launch_destination(
        self, *, extra_env: str = "", role_policy: str = "", providers: str = "codex"
    ) -> None:
        if role_policy:
            self.dest(f"mkdir -p {DEST_PROJECT}/.ccb && printf '%s\\n' '{role_policy}' > {DEST_PROJECT}/.ccb/ccb.config")
        self.dest(
            f"cd {DEST_PROJECT} && {extra_env} tmux new-session -d -s launch 'ccb {providers}'",
            timeout=120,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            record = self.registry()
            if record and record.get("providers", {}).get("codex", {}).get("pane_id"):
                state = self.dest("ls /home/b/.cache/ccb/projects/*/askd.json 2>/dev/null", check=False).stdout.strip()
                if state:
                    self.pane_id = record["providers"]["codex"]["pane_id"]
                    self.marker = record["providers"]["codex"].get("pane_title_marker", "")
                    self.live_id = record.get("ccb_session_id", "")
                    return
            time.sleep(0.3)
        raise RigError("destination launch did not produce a live codex session")

    def reset_destination(
        self, *, extra_env: str = "", role_policy: str = "", providers: str = "codex"
    ) -> None:
        """Fresh tmux server, fresh journal, fresh stub log."""
        self.dest("tmux kill-server 2>/dev/null || true", check=False)
        self.dest("pkill -f 'bin/askd' 2>/dev/null || true", check=False)
        time.sleep(0.5)
        self.dest(
            "rm -rf /home/b/.ccb/remote/journal /home/b/.cache/ccb/projects /home/b/.ccb/run; "
            f": > {STUB_LOG} 2>/dev/null || true",
            check=False,
        )
        self.dest(f"rm -rf {DEST_PROJECT}/.ccb")
        self.launch_destination(extra_env=extra_env, role_policy=role_policy, providers=providers)

    def set_stub_config(self, **config) -> None:
        """Script the destination stub agents for the next delivered reply prompt."""
        path = self.tmp / "stub-reply.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        _run(["docker", "cp", str(path), f"{self.container}:{DEST_STUB_CONFIG}"])
        self.dest(f"chown b:b {DEST_STUB_CONFIG}")

    def clear_stub_config(self) -> None:
        self.dest(f"rm -f {DEST_STUB_CONFIG}", check=False)

    def registry_credential(self, provider: str = "codex") -> tuple[str, str]:
        """The live destination pane's own member credential (never logged)."""
        entry = (self.registry().get("providers") or {}).get(provider) or {}
        member_id = str(entry.get("member_id") or "")
        member_token = str(entry.get("member_token") or "")
        if not member_id or not member_token:
            raise RigError(f"no member credential for {provider}")
        return member_id, member_token

    def registry(self) -> dict:
        """The launch record for the destination project (pretty-printed files)."""
        out = self.dest(
            "python3 - <<'PY'\n"
            "import glob, json\n"
            "records = []\n"
            "for path in sorted(glob.glob('/home/b/.ccb/run/ccb-session-*.json')):\n"
            "    try:\n"
            "        records.append(json.load(open(path)))\n"
            "    except Exception:\n"
            "        pass\n"
            "print(json.dumps(records))\n"
            "PY",
            check=False,
        ).stdout.strip()
        try:
            records = json.loads(out or "[]")
        except ValueError:
            return {}
        for record in records:
            if record.get("work_dir") == DEST_PROJECT:
                return record
        return records[0] if records else {}

    def patch_registry(self, **fields) -> None:
        """Destination-side state only: roles / role_policy live on the launch record."""
        payload = json.dumps(fields)
        self.dest(
            "python3 - <<'PY'\n"
            "import glob, json\n"
            f"patch = json.loads({payload!r})\n"
            "for path in glob.glob('/home/b/.ccb/run/ccb-session-*.json'):\n"
            "    record = json.load(open(path))\n"
            "    record.update(patch)\n"
            "    json.dump(record, open(path, 'w'))\n"
            "PY"
        )

    def journal(self) -> list[dict]:
        out = self.dest(
            "python3 - <<'PY'\n"
            "import glob, json\n"
            "records = []\n"
            "for path in sorted(glob.glob('/home/b/.ccb/remote/journal/*.json')):\n"
            "    try:\n"
            "        records.append(json.load(open(path)))\n"
            "    except Exception:\n"
            "        pass\n"
            "print(json.dumps(records))\n"
            "PY",
            check=False,
        ).stdout.strip()
        try:
            return json.loads(out or "[]")
        except ValueError:
            return []

    def journal_record(self, request_id: str) -> dict | None:
        for record in self.journal():
            if record.get("request_id") == request_id:
                return record
        return None

    def journal_count(self) -> int:
        return len(self.journal())

    def stub_log(self) -> str:
        return self.dest(f"cat {STUB_LOG} 2>/dev/null", check=False).stdout

    def clear_stub_log(self) -> None:
        self.dest(f": > {STUB_LOG}")

    def askd_restart(self) -> None:
        """Restart the destination askd only (not the launch or the pane)."""
        self.dest("pkill -f 'bin/askd' 2>/dev/null || true", check=False)
        time.sleep(0.8)
        self.dest(
            "nohup /home/b/.local/share/codex-dual/bin/askd --state-file "
            "$(ls /home/b/.cache/ccb/projects/*/askd.json 2>/dev/null | head -1) "
            f"--work-dir {DEST_PROJECT} --providers codex >/tmp/askd-restart.log 2>&1 &",
            check=False,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            out = self.dest("pgrep -f 'bin/askd' | wc -l", check=False).stdout.strip()
            if out and int(out) > 0:
                time.sleep(0.5)
                return
            time.sleep(0.2)
        raise RigError("destination askd did not restart")

    def hold_pane_lock(self, seconds: float = 8.0) -> subprocess.Popen:
        """Hold the destination pane's injection lock from a separate process."""
        script = (
            "import sys, time; sys.path.insert(0, '/opt/ccb-src/lib')\n"
            "from pane_lock import pane_injection_lock\n"
            "from terminal import TmuxBackend\n"
            "backend = TmuxBackend()\n"
            f"with pane_injection_lock(backend, {self.pane_id!r}, 30.0):\n"
            "    print('held', flush=True)\n"
            f"    time.sleep({seconds})\n"
        )
        return subprocess.Popen(
            ["docker", "exec", "-u", "b", self.container, "python3", "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    # ------------------------------------------------------------------ #
    # M2: a disposable SOURCE host side
    # ------------------------------------------------------------------ #

    def extract_ref(self, ref: str) -> Path:
        """Extract another commit's tree on the host (for compatibility rows)."""
        target = self.tmp / f"ref-{ref[:12]}"
        if (target / "bin" / "ask").exists():
            return target
        target.mkdir(parents=True, exist_ok=True)
        archive = self.tmp / f"ref-{ref[:12]}.tar"
        _run(
            ["git", "-C", str(REPO), "archive", "--format=tar",
             "--output=" + str(archive), ref],
            timeout=300,
        )
        _run(["tar", "-xf", str(archive), "-C", str(target)], timeout=300)
        return target

    def start_source_side(
        self, *, provider: str = SOURCE_PROVIDER, with_askd: bool = False,
        capture_name: str = "pane-capture.txt",
    ) -> SourceSide:
        """A fresh host-side A: own HOME, tmux server, fake launch and (optionally) askd."""
        self.stop_source_side()
        base = self.tmp / "source"
        base.mkdir(parents=True, exist_ok=True)
        project = base / "project"
        project.mkdir(exist_ok=True)
        home = base / "home"
        (home / ".ccb" / "remote").mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            self.tmp / "home" / ".ccb" / "remote" / "hosts.json",
            home / ".ccb" / "remote" / "hosts.json",
        )
        (base / "tmp").mkdir(exist_ok=True)
        capture = base / capture_name
        capture.write_text("", encoding="utf-8")
        socket_root = base / "tmux-sockets"
        socket_root.mkdir(exist_ok=True)
        socket_name = f"ccb-rig-src-{os.getpid()}"
        server_env = {**os.environ, "TMUX_TMPDIR": str(socket_root)}
        _run(
            ["tmux", "-L", socket_name, "new-session", "-d", "-s", "riga",
             "-c", str(project), f"cat >> {shlex.quote(str(capture))}"],
            env=server_env, timeout=60,
        )
        _run(["tmux", "-L", socket_name, "select-pane", "-t", "%0", "-T", SOURCE_MARKER],
             env=server_env, timeout=60)
        _run(["tmux", "-L", socket_name, "set-option", "-p", "-t", "%0",
              "@ccb_marker", SOURCE_MARKER], env=server_env, timeout=60)
        pane_id = _run(
            ["tmux", "-L", socket_name, "display-message", "-p", "#{pane_id}"],
            env=server_env, timeout=60,
        ).stdout.strip()
        socket_path = _run(
            ["tmux", "-L", socket_name, "display-message", "-p", "#{socket_path}"],
            env=server_env, timeout=60,
        ).stdout.strip()
        _write_source_launch(home, project, pane_id=pane_id, marker=SOURCE_MARKER,
                            provider=provider)
        env = self.source_env()
        env.update({
            "HOME": str(home),
            "TMPDIR": str(base / "tmp"),
            "TMUX": f"{socket_path},1,0",
            "TMUX_PANE": pane_id,
            "CCB_CALLER": provider,
            "CCB_WORK_DIR": str(project),
        })
        side = SourceSide(
            rig=self, home=home, project=project, capture=capture,
            socket_name=socket_name, socket_root=socket_root,
            pane_id=pane_id, marker=SOURCE_MARKER, env=env,
        )
        self._source_side = side
        if with_askd:
            side.start_askd(provider)
        return side

    def stop_source_side(self) -> None:
        if self._source_side is not None:
            self._source_side.stop()
            self._source_side = None

    # ------------------------------------------------------------------ #
    # Source side (this working tree, isolated HOME)
    # ------------------------------------------------------------------ #

    def source_env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env["HOME"] = str(self.tmp / "home")
        env["TMPDIR"] = str(self.tmp / "tmp")
        env["CCB_CALLER"] = "codex"
        env["PATH"] = f"{REPO / 'bin'}:{env.get('PATH', '')}"
        env.update(extra)
        return env

    def ask(self, *args: str, timeout: float = 180, **extra_env: str) -> subprocess.CompletedProcess:
        return _run(
            [sys.executable, str(REPO / "bin" / "ask"), *args],
            env=self.source_env(**extra_env), timeout=timeout, check=False, cwd=str(self.tmp),
        )

    def remote(self, *args: str, timeout: float = 180, **extra_env: str) -> subprocess.CompletedProcess:
        return _run(
            [sys.executable, str(REPO / "bin" / "ccb-remote"), *args],
            env=self.source_env(**extra_env), timeout=timeout, check=False, cwd=str(self.tmp),
        )

    def remote_home(self) -> Path:
        """A-side HOME for the default (isolated) source environment."""
        return self.tmp / "home"

    def home_file(self, relative: str) -> Path:
        return self.remote_home() / relative

    #: The pairing rows pass these two explicitly: ssh expands `~` from the
    #: passwd entry, not from $HOME, so an isolated HOME cannot supply them.
    def pairing_known_hosts(self) -> str:
        return str(self.tmp / "known_hosts")

    def pairing_identity(self) -> str:
        return str(self.tmp / "id_login")

    def dest_file(self, path: str) -> str:
        return self.dest(f"cat {path} 2>/dev/null", check=False).stdout

    def dest_bytes(self, path: str) -> bytes:
        out = subprocess.run(
            ["docker", "exec", "-u", "b", self.container, "cat", path],
            capture_output=True, timeout=120,
        )
        return out.stdout

    def probe(self, *args: str, timeout: float = 180, **extra_env: str) -> dict:
        proc = _run(
            [sys.executable, str(RIG_DIR / "protocol_probe.py"), *args],
            env=self.source_env(**extra_env), timeout=timeout, check=False,
        )
        for line in reversed(proc.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        return {"ok": False, "stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode}

    def receipts(self) -> list[dict]:
        directory = self.tmp / "home" / ".ccb" / "remote" / "outbound"
        records = []
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except ValueError:
                pass
        return records

    def latest_receipt(self) -> dict:
        records = self.receipts()
        return records[-1] if records else {}

    def ssh_argv(
        self,
        options: "list[str] | tuple[str, ...]" = (),
        *,
        command: "list[str] | None" = None,
        identity: str = "a",
        destination: str | None = None,
    ) -> list[str]:
        """ssh argv in ssh(1)'s operand order: OPTIONS -> destination -> command.

        The destination must be the last option operand: anything placed after
        the caller's last option becomes the hostname, which silently turns a
        hardening probe into a client-side failure (e.g. "hostname contains
        invalid characters") instead of a server-side refusal. The remote
        command, when there is one, comes after the destination.
        """
        argv = [
            "ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none", "-o", "ControlPath=none", "-o", "ControlMaster=no",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.tmp / 'known_hosts'}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-i", str(self.tmp / f"id_{identity}"), "-p", str(self.port),
            *options,
            destination or "b@127.0.0.1",
        ]
        if command:
            argv.extend(command)
        return argv

    def raw_ssh(
        self,
        *options: str,
        command: "list[str] | None" = None,
        identity: str = "a",
        timeout: float = 120,
        destination: str | None = None,
    ) -> subprocess.CompletedProcess:
        argv = self.ssh_argv(options, command=command, identity=identity, destination=destination)
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input="")

    def raw_ssh_with_pty(
        self,
        *options: str,
        command: "list[str] | None" = None,
        identity: str = "a",
        timeout: float = 120,
        destination: str | None = None,
    ) -> subprocess.CompletedProcess:
        """The same call from a real terminal, so ssh actually REQUESTS a pty.

        ssh only requests a pty when ITS OWN stdin is a terminal, so a piped
        run proves nothing (`-t` just prints "Pseudo-terminal will not be
        allocated"). `script` supplies a terminal; the request is then made,
        sshd refuses it, and the forced command still runs -- which is the pair
        of facts the PTY probe has to show.
        """
        argv = self.ssh_argv(options, command=command, identity=identity, destination=destination)
        quoted = " ".join(shlex.quote(part) for part in argv)
        return subprocess.run(
            ["script", "-qec", quoted, "/dev/null"],
            capture_output=True, text=True, timeout=timeout, input="",
        )

    def sshd_log(self) -> str:
        return _run(["docker", "logs", self.container], check=False).stderr or ""


# --------------------------------------------------------------------------- #
# Isolated SOURCE side ("machine A" on the host)
# --------------------------------------------------------------------------- #

class SourceSide:
    """One disposable host-side "A": its own HOME, tmux server and fake launch.

    The fake launch registry is test state, not product code: it gives
    `capture_caller_identity` the same evidence a real `ccb` launch publishes,
    so the notification path revalidates a real pane exactly as it would in
    production.
    """

    def __init__(self, *, rig: "Rig", home: Path, project: Path, capture: Path,
                 socket_name: str, socket_root: Path, pane_id: str, marker: str,
                 env: dict) -> None:
        self.rig = rig
        self.home = home
        self.project = project
        self.capture = capture
        self.socket_name = socket_name
        self.socket_root = socket_root
        self.pane_id = pane_id
        self.marker = marker
        self.env = env
        self.state_file = Path("")
        self.askd: subprocess.Popen | None = None
        self.askd_log = home.parent / "askd.log"

    # -- tmux ----------------------------------------------------------- #

    def _tmux_env(self) -> dict:
        return {**os.environ, "TMUX_TMPDIR": str(self.socket_root)}

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return _run(["tmux", "-L", self.socket_name, *args],
                    env=self._tmux_env(), check=check, timeout=60)

    def pane_pid(self) -> str:
        return self.tmux("display-message", "-p", "-t", self.pane_id, "#{pane_pid}").stdout.strip()

    def capture_text(self) -> str:
        try:
            return self.capture.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def kill_caller_pane(self) -> None:
        self.tmux("kill-pane", "-t", self.pane_id, check=False)
        time.sleep(0.2)

    # -- CLIs ------------------------------------------------------------ #

    def ask(self, *args: str, timeout: float = 300) -> subprocess.CompletedProcess:
        return _run([sys.executable, str(REPO / "bin" / "ask"), *args],
                    env=self.env, timeout=timeout, check=False, cwd=str(self.project))

    def remote(self, *args: str, timeout: float = 180) -> subprocess.CompletedProcess:
        return _run([sys.executable, str(REPO / "bin" / "ccb-remote"), *args],
                    env=self.env, timeout=timeout, check=False, cwd=str(self.project))

    def pend(self, request_id: str, timeout: float = 180) -> subprocess.CompletedProcess:
        return _run([sys.executable, str(REPO / "bin" / "pend"), request_id],
                    env=self.env, timeout=timeout, check=False, cwd=str(self.project))

    def receipt(self, request_id: str) -> dict:
        path = self.home / ".ccb" / "remote" / "outbound" / f"{request_id}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def receipts(self) -> list[dict]:
        directory = self.home / ".ccb" / "remote" / "outbound"
        records = []
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except ValueError:
                pass
        return records

    def latest_receipt(self) -> dict:
        records = self.receipts()
        return records[-1] if records else {}

    # -- askd ------------------------------------------------------------ #

    def start_askd(self, provider: str = SOURCE_PROVIDER) -> Path:
        from askd_rpc import ping_daemon
        from project_id import compute_ccb_project_id

        project_id = compute_ccb_project_id(self.project)
        self.state_file = (
            self.home / ".cache" / "ccb" / "projects" / project_id[:16] / "askd.json"
        )
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.askd_log, "a", encoding="utf-8")
        self.askd = subprocess.Popen(
            [sys.executable, str(REPO / "bin" / "askd"),
             "--state-file", str(self.state_file),
             "--work-dir", str(self.project),
             "--providers", provider],
            env=self.env, stdout=handle, stderr=handle, start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if ping_daemon("ask", 0.3, self.state_file):
                return self.state_file
            if self.askd.poll() is not None:
                raise RigError(f"source askd exited: {self.read_askd_log()[-1500:]}")
            time.sleep(0.1)
        raise RigError(f"source askd did not become reachable: {self.read_askd_log()[-1500:]}")

    def read_askd_log(self) -> str:
        try:
            return self.askd_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def stop(self) -> None:
        if self.askd is not None and self.askd.poll() is None:
            self.askd.terminate()
            try:
                self.askd.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.askd.kill()
        self.tmux("kill-server", check=False)


def _write_source_launch(home: Path, project: Path, *, pane_id: str, marker: str,
                        provider: str = SOURCE_PROVIDER) -> None:
    """Publish exactly what a real launch would: one live member for one pane."""
    from project_id import compute_ccb_project_id

    project_id = compute_ccb_project_id(project)
    run_dir = home / ".ccb" / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    session_file = run_dir / f"{SOURCE_LAUNCH}-{provider}-session.json"
    session_file.write_text(
        json.dumps({
            "session_id": SOURCE_LAUNCH,
            "ccb_session_id": SOURCE_LAUNCH,
            "active": True,
            "provider": provider,
            "ccb_project_id": project_id,
            "pane_id": pane_id,
            "terminal": "tmux",
        }),
        encoding="utf-8",
    )
    (run_dir / f"ccb-session-{SOURCE_LAUNCH}.json").write_text(
        json.dumps({
            "ccb_session_id": SOURCE_LAUNCH,
            "ccb_project_id": project_id,
            "work_dir": str(project),
            "terminal": "tmux",
            "updated_at": int(time.time()),
            "providers": {},
            "live_sessions": [{
                "live_id": f"live-{SOURCE_LAUNCH}",
                "provider": provider,
                "pane_id": pane_id,
                "pane_title_marker": marker,
                "terminal": "tmux",
                "session_file": str(session_file),
                "auth_token": "rig-source-token",
                "active": True,
            }],
        }),
        encoding="utf-8",
    )


def wait_until(predicate, timeout: float = 30.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last
