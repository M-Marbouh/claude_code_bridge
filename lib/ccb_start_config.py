from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping, Optional, Tuple

from providers import make_qualified_key, normalize_instance_name, parse_qualified_provider
from session_utils import legacy_project_config_dir, project_config_dir


CONFIG_FILENAME = "ccb.config"
DEFAULT_PROVIDERS = ["codex", "gemini", "opencode", "claude"]


@dataclass
class StartConfig:
    data: dict
    path: Optional[Path] = None


@dataclass(frozen=True)
class ProviderInstanceSpec:
    provider: str
    instance: str
    key: str


_ALLOWED_PROVIDERS = {"codex", "gemini", "opencode", "claude", "droid"}


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


def normalize_provider_tokens(tokens: list[str]) -> tuple[list[str], list[dict], bool]:
    providers: list[str] = []
    instances: list[dict] = []
    seen_providers: set[str] = set()
    seen_instances: set[str] = set()
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
        base, instance = parse_qualified_provider(token)
        if base not in _ALLOWED_PROVIDERS:
            continue
        if instance is None:
            add_provider(base)
            continue
        normalized_instance = normalize_instance_name(instance)
        if not normalized_instance:
            continue
        add_provider(base)
        key = make_qualified_key(base, normalized_instance)
        if key in seen_instances:
            continue
        seen_instances.add(key)
        instances.append({"provider": base, "instance": normalized_instance, "key": key})
    return providers, instances, cmd_enabled


def _normalize_providers(tokens: list[str]) -> tuple[list[str], bool]:
    providers, _instances, cmd_enabled = normalize_provider_tokens(tokens)
    return providers, cmd_enabled


def instance_specs_from_config(data: Mapping[str, object]) -> list[ProviderInstanceSpec]:
    raw = data.get("provider_instances") if isinstance(data, Mapping) else None
    if not isinstance(raw, list):
        return []
    specs: list[ProviderInstanceSpec] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        provider = str(item.get("provider") or "").strip().lower()
        instance = normalize_instance_name(str(item.get("instance") or ""))
        if provider not in _ALLOWED_PROVIDERS or not instance:
            continue
        key = make_qualified_key(provider, instance)
        if key in seen:
            continue
        seen.add(key)
        specs.append(ProviderInstanceSpec(provider=provider, instance=instance, key=key))
    return specs


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
        providers, instances, cmd_enabled = normalize_provider_tokens(tokens)
        out["providers"] = providers
        out["provider_instances"] = instances
        if cmd_enabled and "cmd" not in out:
            out["cmd"] = True
    elif "provider_instances" not in out:
        out["provider_instances"] = []

    raw_instances = out.get("instances")
    if isinstance(raw_instances, dict):
        out["instances"] = dict(raw_instances)
    return out


def _parse_config_obj(obj: object) -> dict:
    if isinstance(obj, dict):
        return normalize_start_config_data(dict(obj))

    if isinstance(obj, list):
        tokens = [str(p) for p in obj if p is not None]
        providers, instances, cmd_enabled = normalize_provider_tokens(tokens)
        data: dict = {"providers": providers, "provider_instances": instances}
        if cmd_enabled:
            data["cmd"] = True
        return data

    if isinstance(obj, str):
        tokens = _parse_tokens(obj)
        providers, instances, cmd_enabled = normalize_provider_tokens(tokens)
        data = {"providers": providers, "provider_instances": instances}
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
        providers, instances, cmd_enabled = normalize_provider_tokens(tokens)
        data: dict = {"providers": providers, "provider_instances": instances}
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
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = ",".join(DEFAULT_PROVIDERS) + "\n"
        target.write_text(payload, encoding="utf-8")
        return target, True
    except Exception:
        return None, False
