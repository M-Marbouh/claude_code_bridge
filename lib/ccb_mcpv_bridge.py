from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import stat
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any


POLICY_MODULE_NAME = "ccb_mcpv_local.py"
STATUS_FILENAME = ".mcpv-launch-status.json"


class DecisionKind(str, Enum):
    PLAIN = "plain"
    WRAP = "wrap"
    ERROR = "error"


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    argv_prefix: tuple[str, ...] = ()
    reason_code: str = ""
    policy_present: bool = False

    @classmethod
    def plain(cls, *, policy_present: bool = False) -> "Decision":
        return cls(DecisionKind.PLAIN, policy_present=policy_present)

    @classmethod
    def wrap(cls, argv_prefix: tuple[str, ...]) -> "Decision":
        if not argv_prefix:
            raise ValueError("a WRAP decision requires an argv prefix")
        return cls(DecisionKind.WRAP, argv_prefix=tuple(argv_prefix), policy_present=True)

    @classmethod
    def error(cls, reason_code: str) -> "Decision":
        return cls(
            DecisionKind.ERROR,
            reason_code=_sanitize_reason_code(reason_code),
            policy_present=True,
        )


class PolicyDecisionError(RuntimeError):
    def __init__(self, decision: Decision):
        self.decision = decision
        super().__init__(safe_error_message(decision.reason_code))


_SAFE_MESSAGES = {
    "manifest_ambiguous_root": "multiple manifest projects resolve to this project root",
    "manifest_invalid": "the MCP manifest is missing, unreadable, or invalid",
    "mcpctl_missing": "mcpctl is unavailable for a project that requires vault-backed MCP values",
    "policy_decision_failed": "the private MCP launch policy could not determine a safe decision",
    "policy_import_failed": "the private MCP launch policy could not be loaded",
    "policy_insecure": "the private MCP launch policy path has unsafe ownership or permissions",
    "policy_invalid_result": "the private MCP launch policy returned an invalid decision",
}

_POLICY_CACHE: dict[str, tuple[tuple[int, int, int], ModuleType]] = {}
_REASON_RE = re.compile(r"[^a-z0-9_]+")


def _sanitize_reason_code(value: object) -> str:
    code = _REASON_RE.sub("_", str(value or "policy_decision_failed").strip().lower()).strip("_")
    return code[:80] or "policy_decision_failed"


def safe_error_message(reason_code: str) -> str:
    code = _sanitize_reason_code(reason_code)
    return _SAFE_MESSAGES.get(code, _SAFE_MESSAGES["policy_decision_failed"])


def policy_path() -> Path:
    return Path.home() / ".ccb" / "local" / POLICY_MODULE_NAME


def _open_verified(path: Path, *, expect_dir: bool) -> os.stat_result:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NOCTTY", 0)
    if expect_dir:
        flags |= getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        expected = stat.S_ISDIR(info.st_mode) if expect_dir else stat.S_ISREG(info.st_mode)
        if not expected:
            raise PermissionError("unexpected policy path type")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise PermissionError("policy path is not owned by the current user")
        if info.st_mode & 0o022:
            raise PermissionError("policy path is group/world writable")
        return info
    finally:
        os.close(fd)


def _load_policy_module() -> tuple[ModuleType | None, Decision | None]:
    path = policy_path()
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, Decision.error("policy_import_failed")

    try:
        _open_verified(path.parent.parent, expect_dir=True)
        _open_verified(path.parent, expect_dir=True)
        info = _open_verified(path, expect_dir=False)
    except (OSError, PermissionError):
        return None, Decision.error("policy_insecure")

    cache_key = str(path)
    signature = (info.st_ino, info.st_mtime_ns, info.st_size)
    cached = _POLICY_CACHE.get(cache_key)
    if cached and cached[0] == signature:
        return cached[1], None

    module_name = "_ccb_private_mcpv_policy"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None, Decision.error("policy_import_failed")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
    except BaseException:
        return None, Decision.error("policy_import_failed")

    if not callable(getattr(module, "decide_wrap", None)):
        return None, Decision.error("policy_invalid_result")
    _POLICY_CACHE[cache_key] = (signature, module)
    return module, None


def reset_policy_cache() -> None:
    """Test hook: discard loaded private policy modules."""
    _POLICY_CACHE.clear()
    sys.modules.pop("_ccb_private_mcpv_policy", None)


def decide(provider: str, project_root: str | Path, managed: bool, caller: str) -> Decision:
    module, load_error = _load_policy_module()
    if load_error is not None:
        return load_error
    if module is None:
        return Decision.plain(policy_present=False)
    try:
        result = module.decide_wrap(
            provider=str(provider or "").strip().lower(),
            project_root=str(Path(project_root).expanduser()),
            managed=bool(managed),
            caller=str(caller or "").strip().lower(),
        )
    except BaseException:
        return Decision.error("policy_decision_failed")
    if not isinstance(result, Decision):
        return Decision.error("policy_invalid_result")
    if result.kind is DecisionKind.WRAP and not result.argv_prefix:
        return Decision.error("policy_invalid_result")
    if result.kind is DecisionKind.ERROR:
        return Decision.error(result.reason_code)
    return result


def render_shell_prefix(decision: Decision, *, shell_type: str) -> str:
    if decision.kind is DecisionKind.ERROR:
        raise PolicyDecisionError(decision)
    if decision.kind is DecisionKind.PLAIN:
        return ""
    if shell_type == "powershell":
        rendered = "& " + " ".join(
            "'" + token.replace("'", "''") + "'" for token in decision.argv_prefix
        )
    else:
        rendered = shlex.join(decision.argv_prefix)
    return rendered + " "


def _status_path(project_root: str | Path) -> Path:
    root = Path(project_root).expanduser()
    primary = root / ".ccb"
    legacy = root / ".ccb_config"
    parent = primary if primary.is_dir() or not legacy.is_dir() else legacy
    return parent / STATUS_FILENAME


def _read_status(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return {}
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                return {}
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                value = json.load(handle)
        finally:
            if fd >= 0:
                os.close(fd)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def record_decision_status(project_root: str | Path, provider: str, decision: Decision) -> bool:
    from session_utils import safe_write_session

    path = _status_path(project_root)
    provider_key = str(provider or "").strip().lower()
    if decision.kind is not DecisionKind.ERROR and not path.exists():
        return True
    data = _read_status(path) if path.exists() else {}
    providers = data.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    if decision.kind is DecisionKind.ERROR:
        code = _sanitize_reason_code(decision.reason_code)
        providers[provider_key] = {
            "provider": provider_key,
            "reason_code": code,
            "message": safe_error_message(code),
            "project_root": str(Path(project_root).expanduser()),
            "timestamp": int(time.time()),
        }
    else:
        providers.pop(provider_key, None)
    data = {"version": 1, "providers": providers}
    if not path.parent.is_dir():
        return False
    ok, _err = safe_write_session(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return bool(ok)


def read_degraded_states(project_root: str | Path) -> dict[str, dict[str, Any]]:
    path = _status_path(project_root)
    data = _read_status(path)
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for provider, value in providers.items():
        if isinstance(provider, str) and isinstance(value, dict):
            result[provider.strip().lower()] = dict(value)
    return result
