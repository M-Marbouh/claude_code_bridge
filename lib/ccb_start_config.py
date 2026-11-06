from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Optional, Tuple

from live_roles import ROLE_NAME_RE
from session_utils import legacy_project_config_dir, project_config_dir, safe_write_session


CONFIG_FILENAME = "ccb.config"
DEFAULT_PROVIDERS = ["codex", "gemini", "opencode", "claude"]


@dataclass
class StartConfig:
    data: dict
    path: Optional[Path] = None


_ALLOWED_PROVIDERS = {"codex", "gemini", "opencode", "claude", "hermes", "pi"}


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


def _split_provider_tokens(
    tokens: list[str],
) -> tuple[list[str], bool, dict[str, list[str]], str]:
    """Tokenise a provider list, keeping `provider=role` annotations.

    Returns `(providers, cmd_enabled, roles, error)`. `roles` maps each provider
    to one entry per occurrence, in the tokens' original order, where "" means
    that occurrence is unassigned. A token without `=` parses exactly as it
    always has: `cmd` enables the command pane and an unknown provider is
    ignored. A malformed annotation is an error, never a silent drop.
    """
    providers: list[str] = []
    cmd_enabled = False
    roles: dict[str, list[str]] = {}

    for raw in tokens:
        token = str(raw).strip().lower()
        if not token:
            continue
        if "=" in token:
            name, _sep, role = token.partition("=")
            name = name.strip()
            role = role.strip()
            if not name:
                return providers, cmd_enabled, roles, (
                    f"provider annotation '{token}' has no provider name before '='"
                )
            if name == "cmd":
                return providers, cmd_enabled, roles, (
                    "the cmd pane cannot be given a role"
                )
            if name not in _ALLOWED_PROVIDERS:
                return providers, cmd_enabled, roles, (
                    f"provider annotation '{token}' names unknown provider '{name}'"
                )
            if not role:
                return providers, cmd_enabled, roles, (
                    f"provider annotation '{token}' has no role after '='"
                )
            if not ROLE_NAME_RE.match(role):
                return providers, cmd_enabled, roles, (
                    f"provider annotation '{token}' has an invalid role name '{role}'"
                )
            providers.append(name)
            roles.setdefault(name, []).append(role)
            continue
        if token == "cmd":
            cmd_enabled = True
            continue
        if token not in _ALLOWED_PROVIDERS:
            continue
        providers.append(token)
        roles.setdefault(token, []).append("")
    return providers, cmd_enabled, roles, ""


def normalize_provider_tokens(tokens: list[str]) -> tuple[list[str], bool]:
    providers, cmd_enabled, _roles, _error = _split_provider_tokens(tokens)
    return providers, cmd_enabled


def _normalize_config_roles(raw: object) -> tuple[dict[str, list[str]], str]:
    """Validate a JSON `roles` object into one entry per declared occurrence."""
    if not isinstance(raw, dict):
        return {}, "roles must map providers to role names in ccb.config"
    plan: dict[str, list[str]] = {}
    for raw_provider, raw_value in raw.items():
        provider = str(raw_provider).strip().lower()
        if provider not in _ALLOWED_PROVIDERS:
            return {}, f"roles names unknown provider '{raw_provider}' in ccb.config"
        # A provider mapped to null/"" expresses no assignment at all.
        if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
            continue
        entries = raw_value if isinstance(raw_value, list) else [raw_value]
        assigned: list[str] = []
        for entry in entries:
            if entry is None:
                assigned.append("")
                continue
            if not isinstance(entry, str):
                return {}, (
                    f"roles for '{provider}' must be role names or null in ccb.config"
                )
            role = entry.strip().lower()
            if not role:
                assigned.append("")
                continue
            if not ROLE_NAME_RE.match(role):
                return {}, (
                    f"roles for '{provider}' has an invalid role name '{entry}' in ccb.config"
                )
            assigned.append(role)
        plan[provider] = assigned
    return plan, ""


def _has_assigned_role(plan: dict[str, list[str]]) -> bool:
    return any(any(role for role in roles) for roles in plan.values())


def _data_from_tokens(tokens: list[str]) -> dict:
    providers, cmd_enabled, roles, error = _split_provider_tokens(tokens)
    data: dict = {"providers": providers}
    if cmd_enabled:
        data["cmd"] = True
    if error:
        data["roles_error"] = error
    elif _has_assigned_role(roles):
        data["roles"] = roles
    return data


def resolve_roles_for_providers(data: dict, providers: list[str]) -> tuple[list[str], str]:
    """Map ccb.config roles onto the launch's effective providers.

    Returns `(assignments, error)`: one role (or "") per provider occurrence in
    the launch's original provider order, and a config error when the roles
    cannot be honoured. Never guesses: a role naming a provider this launch
    does not start, a role count that does not match the provider's occurrence
    count, a role assigned to two sessions, or per-occurrence roles for a
    duplicate provider without occurrence identities are all refused.
    """
    error = str(data.get("roles_error") or "")
    if error:
        return [], error
    raw = data.get("roles")
    assignments = [""] * len(providers)
    if raw is None:
        return assignments, ""
    if not isinstance(raw, dict):
        return [], "roles must map providers to role names in ccb.config"

    for provider, roles in raw.items():
        if not isinstance(roles, list):
            return [], f"roles for '{provider}' must be a role name or a list in ccb.config"
        if not any(roles):
            # A provider whose entries are all blank assigns nothing, so it
            # constrains nothing: plain-text tokens add such entries for every
            # unannotated provider, and a CLI override must be able to drop one.
            continue
        if provider not in providers:
            return [], (
                f"roles assigns a role to '{provider}', which this launch does not start"
            )
        count = providers.count(provider)
        if len(roles) != count:
            return [], (
                f"roles for '{provider}' must name one role per {provider} session "
                f"({count}), got {len(roles)}"
            )
        if count > 1 and providers.count("codex") != 2:
            # Per-occurrence credentials exist only for the Codex pair.
            return [], f"per-session roles for duplicate {provider} are not supported"

    seen: dict[str, int] = {}
    for index, provider in enumerate(providers):
        occurrence = seen.get(provider, 0)
        seen[provider] = occurrence + 1
        roles = raw.get(provider)
        # Blank-only entries were skipped by validation, so they must not be
        # indexed here either: they say nothing about this occurrence.
        if roles and any(roles):
            assignments[index] = roles[occurrence]

    duplicated = sorted({role for role in assignments if role and assignments.count(role) > 1})
    if duplicated:
        return [], (
            f"role '{duplicated[0]}' is assigned to more than one session in ccb.config"
        )
    return assignments, ""


def provider_pairing_error(providers: list[str], *, resume: bool = False) -> str:
    for provider in set(providers):
        count = providers.count(provider)
        if count > (2 if provider == "codex" else 1):
            return "Only two Codex sessions may share a provider; other providers must be unique."
    if resume and providers.count("codex") == 2:
        return "CCB resume is unsupported for a Codex pair. Start fresh, then use native resume inside each pane."
    return ""


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

    token_roles: dict[str, list[str]] = {}
    token_error = ""
    if tokens:
        providers, cmd_enabled, token_roles, token_error = _split_provider_tokens(tokens)
        out["providers"] = providers
        if cmd_enabled and "cmd" not in out:
            out["cmd"] = True
    out.pop("provider_instances", None)
    out.pop("instances", None)

    # An explicit `roles` object and `provider=role` provider tokens describe
    # the same thing; accepting both would silently ignore one of them.
    explicit_roles = "roles" in out and out.get("roles") is not None
    if token_error:
        out.pop("roles", None)
        out["roles_error"] = token_error
    elif explicit_roles and _has_assigned_role(token_roles):
        out.pop("roles", None)
        out["roles_error"] = "roles given both in providers and in roles; use one"
    elif explicit_roles:
        plan, error = _normalize_config_roles(out["roles"])
        if error:
            out.pop("roles", None)
            out["roles_error"] = error
        else:
            out["roles"] = plan
    elif _has_assigned_role(token_roles):
        out["roles"] = token_roles
    else:
        out.pop("roles", None)
    return out


def _parse_config_obj(obj: object) -> dict:
    if isinstance(obj, dict):
        return normalize_start_config_data(dict(obj))

    if isinstance(obj, list):
        tokens = [str(p) for p in obj if p is not None]
        return _data_from_tokens(tokens)

    if isinstance(obj, str):
        return _data_from_tokens(_parse_tokens(obj))

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
        return _data_from_tokens(_parse_tokens(raw))
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
