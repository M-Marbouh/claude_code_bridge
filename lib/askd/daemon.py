"""
Unified Ask Daemon - Single daemon for all AI providers.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

from askd.adapters.base import BaseProviderAdapter, ProviderRequest, ProviderResult, QueuedTask
from askd.registry import ProviderRegistry
from askd_runtime import log_path, random_token, state_file_path, write_log
from ccb_protocol import make_req_id
from providers import ProviderDaemonSpec
from worker_pool import BaseSessionWorker, PerSessionWorkerPool


ASKD_SPEC = ProviderDaemonSpec(
    daemon_key="askd",
    protocol_prefix="ask",
    state_file_name="askd.json",
    log_file_name="askd.log",
    idle_timeout_env="CCB_ASKD_IDLE_TIMEOUT_S",
    lock_name="askd",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write_log(line: str) -> None:
    write_log(log_path(ASKD_SPEC.log_file_name), line)


def _request_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        raw = value.strip().lower()
        if not raw:
            return False
        return raw in {"1", "true", "yes", "on"}
    return bool(value)


class _SessionWorker(BaseSessionWorker[QueuedTask, ProviderResult]):
    """Worker thread for processing tasks for a specific session."""

    def __init__(self, session_key: str, adapter: BaseProviderAdapter):
        super().__init__(session_key)
        self.adapter = adapter

    def _handle_task(self, task: QueuedTask) -> ProviderResult:
        return self.adapter.handle_task(task)

    def _handle_exception(self, exc: Exception, task: QueuedTask) -> ProviderResult:
        _write_log(f"[ERROR] provider={self.adapter.key} session={self.session_key} req_id={task.req_id} {exc}")
        return self.adapter.handle_exception(exc, task)


class _UnifiedWorkerPool:
    """Worker pool that routes tasks to provider-specific workers."""

    def __init__(self, registry: ProviderRegistry):
        self._registry = registry
        self._pools: Dict[str, PerSessionWorkerPool[_SessionWorker]] = {}
        self._lock = threading.Lock()

    def _get_pool(self, provider_key: str) -> PerSessionWorkerPool[_SessionWorker]:
        with self._lock:
            if provider_key not in self._pools:
                self._pools[provider_key] = PerSessionWorkerPool[_SessionWorker]()
            return self._pools[provider_key]

    def submit(self, provider: str, request: ProviderRequest) -> Optional[QueuedTask]:
        adapter = self._registry.get(provider)
        if not adapter:
            return None

        req_id = request.req_id or make_req_id()
        cancel_event = threading.Event()
        task = QueuedTask(
            request=request,
            created_ms=_now_ms(),
            req_id=req_id,
            done_event=threading.Event(),
            cancelled=False,
            cancel_event=cancel_event,
        )

        session = adapter.load_session(Path(request.work_dir))
        session_key = adapter.compute_session_key(session) if session else f"{provider}:unknown"

        pool = self._get_pool(provider)
        worker = pool.get_or_create(
            session_key,
            lambda sk: _SessionWorker(sk, adapter),
        )
        worker.enqueue(task)
        return task


class UnifiedAskDaemon:
    """
    Unified daemon server for all AI providers.

    Handles requests for Claude, Codex, Gemini, and OpenCode
    in a single process with per-provider worker pools.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        state_file: Optional[Path] = None,
        registry: Optional[ProviderRegistry] = None,
        work_dir: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.state_file = state_file or state_file_path(ASKD_SPEC.state_file_name)
        self.token = random_token()
        self.registry = registry or ProviderRegistry()
        self.pool = _UnifiedWorkerPool(self.registry)
        self.work_dir = work_dir

    def _handle_request(self, msg: dict) -> dict:
        """Handle an incoming request."""
        operation = str(msg.get("operation") or "").strip().lower()
        if operation == "list_projects":
            return self._handle_list_projects(msg)
        if operation == "runtime_status":
            return self._handle_runtime_status(msg)

        provider = str(msg.get("provider") or "").strip().lower()
        if not provider:
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": "Missing 'provider' field",
            }

        if ":" in provider:
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": f"Provider instances are no longer supported: {provider}",
            }

        adapter = self.registry.get(provider)
        if not adapter:
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": f"Unknown provider: {provider}",
            }

        caller = str(msg.get("caller") or "").strip()
        if not caller:
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": "Missing 'caller' field (required).",
            }

        try:
            request = ProviderRequest(
                client_id=str(msg.get("id") or ""),
                work_dir=str(msg.get("work_dir") or ""),
                timeout_s=float(msg.get("timeout_s") or 300.0),
                quiet=bool(msg.get("quiet") or False),
                message=str(msg.get("message") or ""),
                caller=caller,
                output_path=str(msg.get("output_path")) if msg.get("output_path") else None,
                req_id=str(msg.get("req_id")) if msg.get("req_id") else None,
                no_wrap=bool(msg.get("no_wrap") or False),
                show_tier=_request_bool(msg.get("show_tier")),
                delivery_only=bool(msg.get("delivery_only") or msg.get("no_reply_wait") or False),
                suppress_completion_hook=bool(msg.get("suppress_completion_hook") or False),
                email_req_id=str(msg.get("email_req_id") or ""),
                email_msg_id=str(msg.get("email_msg_id") or ""),
                email_from=str(msg.get("email_from") or ""),
                caller_pane_id=str(msg.get("caller_pane_id") or ""),
                caller_terminal=str(msg.get("caller_terminal") or ""),
                caller_work_dir=str(msg.get("caller_work_dir") or ""),
            )
        except Exception as exc:
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": f"Bad request: {exc}",
            }

        task = self.pool.submit(provider, request)
        if not task:
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": f"Failed to submit task for provider: {provider}",
            }

        wait_timeout = None if float(request.timeout_s) < 0.0 else (float(request.timeout_s) + 5.0)
        task.done_event.wait(timeout=wait_timeout)
        result = task.result

        # If timeout occurred and task is still running, mark it as cancelled
        if not result and not task.done_event.is_set():
            _write_log(f"[WARN] Task timeout, marking as cancelled: provider={provider} req_id={task.req_id}")
            task.cancelled = True
            if task.cancel_event:
                task.cancel_event.set()

        if not result:
            return {
                "type": "ask.response",
                "v": 1,
                "id": request.client_id,
                "exit_code": 2,
                "reply": "",
            }

        return {
            "type": "ask.response",
            "v": 1,
            "id": request.client_id,
            "req_id": result.req_id,
            "exit_code": result.exit_code,
            "reply": result.reply,
            "provider": provider,
            "meta": {
                "session_key": result.session_key,
                "status": result.status,
                "done_seen": result.done_seen,
                "done_ms": result.done_ms,
                "anchor_seen": result.anchor_seen,
                "anchor_ms": result.anchor_ms,
                "fallback_scan": result.fallback_scan,
                "log_path": result.log_path,
                "confirmation": (result.extra or {}).get("confirmation", ""),
            },
        }

    def _handle_list_projects(self, msg: dict) -> dict:
        """Run terminal-aware project discovery outside a managed client sandbox."""
        ccb_list = Path(__file__).resolve().parents[2] / "bin" / "ccb-list"
        argv = [sys.executable, str(ccb_list), "--json", "--direct"]
        if _request_bool(msg.get("include_stale")):
            argv.append("--stale")
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception as exc:
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": f"Project discovery failed: {exc}",
            }
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "ccb-list failed").strip()
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": f"Project discovery failed: {detail}",
            }
        try:
            entries = json.loads(result.stdout)
        except Exception as exc:
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": f"Project discovery returned invalid JSON: {exc}",
            }
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": "Project discovery returned an invalid result",
            }
        return {
            "type": "ask.response",
            "v": 1,
            "id": msg.get("id"),
            "exit_code": 0,
            "reply": "",
            "entries": entries,
        }

    def _handle_runtime_status(self, msg: dict) -> dict:
        """Resolve terminal-aware provider status outside a managed client sandbox."""
        raw_work_dir = msg.get("work_dir")
        if not isinstance(raw_work_dir, str) or not raw_work_dir.strip():
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": "Runtime status requires a work_dir",
            }
        try:
            from ccb_runtime_status import resolve_project_runtime_status
            from project_id import compute_ccb_project_id

            check_daemon = _request_bool(msg.get("check_daemon", True))
            daemon_work_dir = self.work_dir or os.getcwd()
            same_project = compute_ccb_project_id(Path(daemon_work_dir)) == compute_ccb_project_id(
                Path(raw_work_dir)
            )
            project = resolve_project_runtime_status(
                raw_work_dir,
                include_stale=_request_bool(msg.get("include_stale")),
                check_daemon=check_daemon,
                _allow_daemon_proxy=False,
                _daemon_online_override=(True if same_project else None) if check_daemon else False,
            )
        except Exception as exc:
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": f"Runtime status failed: {exc}",
            }
        return {
            "type": "ask.response",
            "v": 1,
            "id": msg.get("id"),
            "exit_code": 0,
            "reply": "",
            "project": project.to_dict(),
        }

    def serve_forever(self) -> int:
        """Start the daemon and serve requests."""
        from askd_server import AskDaemonServer
        import askd_rpc

        self.registry.start_all()

        def _on_stop() -> None:
            self.registry.stop_all()
            self._cleanup_state_file()

        server = AskDaemonServer(
            spec=ASKD_SPEC,
            host=self.host,
            port=self.port,
            token=self.token,
            state_file=self.state_file,
            request_handler=self._handle_request,
            request_queue_size=128,
            on_stop=_on_stop,
            work_dir=self.work_dir,
        )
        return server.serve_forever()

    def _cleanup_state_file(self) -> None:
        import askd_rpc
        try:
            st = askd_rpc.read_state(self.state_file)
        except Exception:
            st = None
        try:
            if isinstance(st, dict) and int(st.get("pid") or 0) == os.getpid():
                self.state_file.unlink(missing_ok=True)
        except TypeError:
            try:
                if isinstance(st, dict) and int(st.get("pid") or 0) == os.getpid():
                    if self.state_file.exists():
                        self.state_file.unlink()
            except Exception:
                pass
        except Exception:
            pass


def read_state(state_file: Optional[Path] = None) -> Optional[dict]:
    import askd_rpc
    state_file = state_file or state_file_path(ASKD_SPEC.state_file_name)
    return askd_rpc.read_state(state_file)


def ping_daemon(timeout_s: float = 0.5, state_file: Optional[Path] = None) -> bool:
    import askd_rpc
    state_file = state_file or state_file_path(ASKD_SPEC.state_file_name)
    return askd_rpc.ping_daemon("ask", timeout_s, state_file)


def shutdown_daemon(timeout_s: float = 1.0, state_file: Optional[Path] = None) -> bool:
    import askd_rpc
    state_file = state_file or state_file_path(ASKD_SPEC.state_file_name)
    return askd_rpc.shutdown_daemon("ask", timeout_s, state_file)
