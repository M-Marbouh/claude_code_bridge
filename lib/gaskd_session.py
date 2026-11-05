from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from ccb_config import apply_backend_env
from project_id import compute_ccb_project_id
from session_utils import find_project_session_file as _find_project_session_file, safe_write_session
from terminal import get_backend_for_session

apply_backend_env()


def find_project_session_file(work_dir: Path) -> Optional[Path]:
    return _find_project_session_file(work_dir, ".gemini-session")


def _read_json(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8-sig")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class GeminiProjectSession:
    session_file: Path
    data: dict

    @property
    def terminal(self) -> str:
        return (self.data.get("terminal") or "tmux").strip() or "tmux"

    @property
    def pane_id(self) -> str:
        v = self.data.get("pane_id")
        if not v and self.terminal == "tmux":
            v = self.data.get("tmux_session")
        return str(v or "").strip()

    @property
    def pane_title_marker(self) -> str:
        return str(self.data.get("pane_title_marker") or "").strip()

    @property
    def gemini_session_id(self) -> str:
        return str(self.data.get("gemini_session_id") or "").strip()

    @property
    def gemini_session_path(self) -> str:
        return str(self.data.get("gemini_session_path") or "").strip()

    @property
    def work_dir(self) -> str:
        return str(self.data.get("work_dir") or self.session_file.parent)

    @property
    def runtime_dir(self) -> Path:
        return Path(self.data.get("runtime_dir") or self.session_file.parent)

    @property
    def start_cmd(self) -> str:
        return str(self.data.get("start_cmd") or "").strip()

    def backend(self):
        return get_backend_for_session(self.data)

    def _attach_pane_log(self, backend: object, pane_id: str) -> None:
        ensure = getattr(backend, "ensure_pane_log", None)
        if callable(ensure):
            try:
                ensure(str(pane_id))
            except Exception:
                pass

    def ensure_pane(self) -> Tuple[bool, str]:
        backend = self.backend()
        if not backend:
            return False, "Terminal backend not available"

        pane_id = self.pane_id
        previous_pane = str(pane_id or "").strip()
        launch_id = str(self.data.get("session_id") or "").strip()
        marker = self.pane_title_marker
        resolver = getattr(backend, "find_pane_by_title_marker", None)

        if pane_id and backend.is_alive(pane_id):
            # WezTerm multi-window: verify the alive pane belongs to this project.
            # Also keeps PR #132 tmux-recycling detection in the inner block.
            cwd_check = getattr(backend, "pane_belongs_to_cwd", None)
            if not cwd_check or cwd_check(pane_id, self.work_dir):
                if marker and callable(resolver):
                    try:
                        resolved = resolver(marker, self.work_dir)
                        if resolved and str(resolved) != str(pane_id) and backend.is_alive(str(resolved)):
                            _invalidate_role_claims(launch_id, previous_pane)
                            self.data["pane_id"] = str(resolved)
                            self.data["updated_at"] = _now_str()
                            self._write_back()
                            self._attach_pane_log(backend, str(resolved))
                            return True, str(resolved)
                    except Exception:
                        pass
                self._attach_pane_log(backend, pane_id)
                return True, pane_id
            # else: pane alive but belongs to wrong project — fall through to marker resolution

        resolved: Optional[str] = None
        if marker and callable(resolver):
            resolved = resolver(marker, self.work_dir)
            if resolved and backend.is_alive(str(resolved)):
                _invalidate_role_claims(launch_id, previous_pane)
                self.data["pane_id"] = str(resolved)
                self.data["updated_at"] = _now_str()
                self._write_back()
                self._attach_pane_log(backend, str(resolved))
                return True, str(resolved)

        if self.terminal == "tmux":
            start_cmd = self.start_cmd
            respawn = getattr(backend, "respawn_pane", None)
            if start_cmd and callable(respawn):
                last_err: str | None = None
                is_pane_dead = getattr(backend, "is_pane_dead", None)
                candidates = [resolved]
                if pane_id and str(pane_id) != str(resolved or ""):
                    candidates.append(pane_id)
                for target in candidates:
                    if not target or not str(target).startswith("%") or not callable(is_pane_dead):
                        continue
                    try:
                        if not is_pane_dead(str(target)):
                            continue
                    except Exception:
                        continue
                    if str(target) != str(resolved or ""):
                        cwd_check = getattr(backend, "pane_belongs_to_cwd", None)
                        foreign_marker = getattr(backend, "pane_has_different_ccb_marker", None)
                        try:
                            if not callable(cwd_check) or not cwd_check(str(target), self.work_dir):
                                continue
                            if not callable(foreign_marker) or foreign_marker(str(target), marker):
                                continue
                        except Exception:
                            continue
                    try:
                        saver = getattr(backend, "save_crash_log", None)
                        if callable(saver):
                            try:
                                runtime = self.runtime_dir
                                runtime.mkdir(parents=True, exist_ok=True)
                                crash_log = runtime / f"pane-crash-{int(time.time())}.log"
                                saver(str(target), str(crash_log), lines=1000)
                            except Exception:
                                pass
                        _invalidate_role_claims(launch_id, str(target))
                        credential_prefix = _role_credential_prefix(
                            launch_id, str(target), str(self.session_file)
                        )
                        respawn(
                            str(target),
                            cmd=credential_prefix + start_cmd,
                            cwd=self.work_dir,
                            remain_on_exit=True,
                            kill_live=False,
                        )
                        if backend.is_alive(str(target)):
                            self.data["pane_id"] = str(target)
                            self.data["updated_at"] = _now_str()
                            self._write_back()
                            self._attach_pane_log(backend, str(target))
                            return True, str(target)
                        last_err = "respawn did not revive pane"
                    except Exception as exc:
                        last_err = f"{exc}"
                if last_err:
                    return False, f"Pane not alive and respawn failed: {last_err}"

        return False, f"Pane not alive: {pane_id}"

    def update_gemini_binding(self, *, session_path: Optional[Path], session_id: Optional[str]) -> None:
        old_path = str(self.data.get("gemini_session_path") or "").strip()
        old_id = str(self.data.get("gemini_session_id") or "").strip()
        updated = False
        if session_path:
            try:
                session_path_str = str(Path(session_path).expanduser())
            except Exception:
                session_path_str = str(session_path)
            if session_path_str and self.data.get("gemini_session_path") != session_path_str:
                self.data["gemini_session_path"] = session_path_str
                updated = True

            # Best-effort: store Gemini project hash for debugging/inspection.
            try:
                project_hash = Path(session_path_str).parent.parent.name
            except Exception:
                project_hash = ""
            if project_hash and self.data.get("gemini_project_hash") != project_hash:
                self.data["gemini_project_hash"] = project_hash
                updated = True

        if session_id and self.data.get("gemini_session_id") != session_id:
            self.data["gemini_session_id"] = session_id
            updated = True

        # Ensure ccb_project_id exists (best-effort).
        if not (self.data.get("ccb_project_id") or "").strip():
            try:
                self.data["ccb_project_id"] = compute_ccb_project_id(Path(self.work_dir))
                updated = True
            except Exception:
                pass

        if updated:
            new_id = str(session_id or "").strip()
            if not new_id and session_path:
                try:
                    new_id = Path(session_path).stem
                except Exception:
                    new_id = ""
            session_path_str = str(session_path) if session_path else ""
            if old_id and old_id != new_id:
                self.data["old_gemini_session_id"] = old_id
            if old_path and (old_path != session_path_str or (old_id and old_id != new_id)):
                self.data["old_gemini_session_path"] = old_path
            if old_path or old_id:
                self.data["old_updated_at"] = _now_str()
                try:
                    from ctx_transfer_utils import maybe_auto_transfer

                    old_path_obj = None
                    if old_path:
                        try:
                            old_path_obj = Path(old_path).expanduser()
                        except Exception:
                            old_path_obj = None
                    maybe_auto_transfer(
                        provider="gemini",
                        work_dir=Path(self.work_dir),
                        session_path=old_path_obj,
                        session_id=old_id or None,
                    )
                except Exception:
                    pass

            self.data["updated_at"] = _now_str()
            if self.data.get("active") is False:
                self.data["active"] = True
            self._write_back()

    def _write_back(self) -> None:
        payload = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        ok, _err = safe_write_session(self.session_file, payload)
        if not ok:
            return


def load_project_session(work_dir: Path) -> Optional[GeminiProjectSession]:
    session_file = find_project_session_file(work_dir)
    if not session_file:
        return None
    data = _read_json(session_file)
    if not data:
        return None
    return GeminiProjectSession(session_file=session_file, data=data)


def compute_session_key(session: GeminiProjectSession) -> str:
    """
    Compute the daemon routing/serialization key for this provider.

    Hard rule: include provider + ccb_project_id to isolate projects and providers.
    """
    pid = str(session.data.get("ccb_project_id") or "").strip()
    if not pid:
        try:
            pid = compute_ccb_project_id(Path(session.work_dir))
        except Exception:
            pid = ""
    return f"gemini:{pid}" if pid else "gemini:unknown"


def _role_credential_prefix(launch_id: str, pane_id: str, session_file: str) -> str:
    """Credential env for a respawned pane, re-derived from the launch record.

    The stored start command intentionally carries no token. If this lookup
    fails the pane still respawns -- it just cannot claim a role.
    """
    try:
        from live_roles import credential_env_prefix_for_pane

        return credential_env_prefix_for_pane(launch_id, pane_id, session_file)
    except Exception:
        return ""


def _invalidate_role_claims(launch_id: str, pane_id: str) -> None:
    """Drop a role claim recorded for a pane that just died or was replaced.

    Role assignments are per pane incarnation: a respawned or replacement
    pane never inherits one. Best-effort -- recovery must not depend on it.
    """
    launch_id = str(launch_id or "").strip()
    pane_id = str(pane_id or "").strip()
    if not launch_id or not pane_id:
        return
    # Best-effort tidy-up only. Recovery proceeds whatever this returns: the
    # stored pane process witness is what makes a respawned pane's old
    # assignment invalid, so a lock timeout or a failed write here changes
    # nothing about correctness.
    try:
        from live_roles import invalidate_pane_claims

        invalidate_pane_claims(launch_id, pane_id)
    except Exception:
        pass
