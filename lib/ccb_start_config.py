from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Optional, Tuple

from session_utils import legacy_project_config_dir, project_config_dir, safe_write_session


CONFIG_FILENAME = "ccb.config"
DEFAULT_PROVIDERS = ["codex", "gemini", "opencode", "claude"]


@dataclass
class StartConfig:
    data: dict
    path: Optional[Path] = None


_ALLOWED_PROVIDERS = {"codex", "gemini", "opencode", "claude"}


def _parse_tokens(raw: str) -> list[str]:
    if not raw:
        return []
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line
        if "//" in stripped:
            stripped = stripped.split("//", 1)[0]
        if "#" in stripped:
            stripped = stripped.split("#", 1)[0]
        lines.append(stripped)
    cleaned = " ".join(lines)
    cleaned = re.sub(r"[\[\]\{\}\"']", " ", cleaned)
    parts = re.split(r"[,\s]+", cleaned)
    return [p for p in (part.strip() for part in parts) if p]


def normalize_provider_tokens(tokens: list[str]) -> tuple[list[str], bool]:
    providers: list[str] = []
    seen_providers: set[str] = set()
    cmd_enabled = False

    def add_provider(provider: str) -> None:
        if provider in seen_providers:
            return
        seen_providers.add(provider)
        providers.append(provider)

    for raw in tokens:
        token = str(raw).strip().lower()
        if not token:
            continue
        if token == "cmd":
            cmd_enabled = True
            continue
        if token not in _ALLOWED_PROVIDERS:
            continue
        add_provider(token)
    return providers, cmd_enabled


def _normalize_providers(tokens: list[str]) -> tuple[list[str], bool]:
    return normalize_provider_tokens(tokens)


def normalize_start_config_data(data: dict) -> dict:
    out = dict(data)
    raw_providers = out.get("providers")
    tokens: list[str] = []
    if isinstance(raw_providers, str):
        tokens = _parse_tokens(raw_providers)
    elif isinstance(raw_providers, list):
        tokens = [str(p) for p in raw_providers if p is not None]
    elif raw_providers is not None:
        tokens = [str(raw_providers)]

    if tokens:
        providers, cmd_enabled = normalize_provider_tokens(tokens)
        out["providers"] = providers
        if cmd_enabled and "cmd" not in out:
            out["cmd"] = True
    out.pop("provider_instances", None)
    out.pop("instances", None)
    return out


def _parse_config_obj(obj: object) -> dict:
    if isinstance(obj, dict):
        return normalize_start_config_data(dict(obj))

    if isinstance(obj, list):
        tokens = [str(p) for p in obj if p is not None]
        providers, cmd_enabled = normalize_provider_tokens(tokens)
        data: dict = {"providers": providers}
        if cmd_enabled:
            data["cmd"] = True
        return data

    if isinstance(obj, str):
        tokens = _parse_tokens(obj)
        providers, cmd_enabled = normalize_provider_tokens(tokens)
        data = {"providers": providers}
        if cmd_enabled:
            data["cmd"] = True
        return data

    return {}


def _read_config(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        obj = None
    if obj is None:
        tokens = _parse_tokens(raw)
        providers, cmd_enabled = normalize_provider_tokens(tokens)
        data: dict = {"providers": providers}
        if cmd_enabled:
            data["cmd"] = True
        return data
    return _parse_config_obj(obj)


def _config_paths(work_dir: Path) -> Tuple[Path, Path, Path]:
    primary = project_config_dir(work_dir) / CONFIG_FILENAME
    legacy = legacy_project_config_dir(work_dir) / CONFIG_FILENAME
    global_path = Path.home() / ".ccb" / CONFIG_FILENAME
    return primary, legacy, global_path


def load_start_config(work_dir: Path) -> StartConfig:
    primary, legacy, global_path = _config_paths(work_dir)
    if primary.exists():
        return StartConfig(data=_read_config(primary), path=primary)
    if legacy.exists():
        return StartConfig(data=_read_config(legacy), path=legacy)
    if global_path.exists():
        return StartConfig(data=_read_config(global_path), path=global_path)
    return StartConfig(data={}, path=None)


def ensure_default_start_config(work_dir: Path) -> Tuple[Optional[Path], bool]:
    primary, legacy, _global_path = _config_paths(work_dir)
    if primary.exists():
        return primary, False
    if legacy.exists():
        return legacy, False
    target = primary
    if not primary.parent.exists() and legacy.parent.is_dir():
        target = legacy
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = ",".join(DEFAULT_PROVIDERS) + "\n"
        ok, _error = safe_write_session(target, payload)
        if not ok:
            return None, False
        return target, True
    except Exception:
        return None, False
