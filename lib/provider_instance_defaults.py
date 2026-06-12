from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from providers import make_qualified_key, parse_qualified_provider


@dataclass(frozen=True)
class InstancePolicy:
    provider: str
    instance: str | None
    model: str | None = None
    effort: str | None = None
    sandbox: str | None = None
    launch_args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)


_DEFAULT_POLICIES: dict[str, dict[str, object]] = {
    "codex": {
        "model": "gpt-5.5",
        "effort": "xhigh",
        "sandbox": "workspace-write",
    },
    "codex:worker": {
        "model": "gpt-5.4-mini",
        "effort": "xhigh",
        "sandbox": "workspace-write",
    },
    "claude:worker": {
        "model": "haiku",
    },
}


def qualified_key(provider: str, instance: str | None) -> str:
    return make_qualified_key(provider, instance)


def normalize_instance_overrides(raw: object) -> dict[str, dict]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key or "").strip().lower()
        if not key:
            continue
        base, instance = parse_qualified_provider(key)
        if not base:
            continue
        normalized_key = make_qualified_key(base, instance)
        if isinstance(raw_value, dict):
            normalized[normalized_key] = dict(raw_value)
    return normalized


def _tuple_launch_args(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part for part in value.split() if part)
    if isinstance(value, (list, tuple)):
        return tuple(str(part) for part in value if str(part).strip())
    return ()


def _string_env(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    env: dict[str, str] = {}
    for key, val in value.items():
        name = str(key or "").strip()
        if name:
            env[name] = str(val)
    return env


def resolve_instance_policy(
    provider: str,
    instance: str | None,
    overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> InstancePolicy:
    base = str(provider or "").strip().lower()
    inst = str(instance or "").strip().lower() or None
    key = make_qualified_key(base, inst)

    data: dict[str, object] = {}
    data.update(_DEFAULT_POLICIES.get(key, {}))
    if overrides:
        override = overrides.get(key)
        if isinstance(override, Mapping):
            for name in ("model", "effort", "sandbox", "launch_args", "env"):
                if name in override:
                    data[name] = override[name]

    model = data.get("model")
    effort = data.get("effort")
    sandbox = data.get("sandbox")
    return InstancePolicy(
        provider=base,
        instance=inst,
        model=str(model).strip() if model is not None and str(model).strip() else None,
        effort=str(effort).strip() if effort is not None and str(effort).strip() else None,
        sandbox=str(sandbox).strip() if sandbox is not None and str(sandbox).strip() else None,
        launch_args=_tuple_launch_args(data.get("launch_args")),
        env=_string_env(data.get("env")),
    )
