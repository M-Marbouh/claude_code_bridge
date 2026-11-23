"""Timeout settings shared by the ask client, daemon, and the Codex/Pi adapters.

Two families live here, deliberately separate:

* the per-provider IDLE window and MAX WAIT cap used by progress mode, and
* the plain positive-number coercion every caller already used.

The environment names are per provider and frozen: ``CCB_CODEX_*`` keeps
working exactly as it always has, and Pi gets its own pair. No generic rename
happens here, because an operator's existing ``CCB_CODEX_IDLE_TIMEOUT_S`` must
never start meaning something else.
"""
from __future__ import annotations

import math
import os
from typing import Any


DEFAULT_CODEX_IDLE_TIMEOUT_S = 1800.0
DEFAULT_CODEX_MAX_WAIT_S = 21600.0

#: Pi shares Codex's defaults: a turn that keeps producing output stays alive
#: for half an hour of silence at most, and no turn may outlive six hours.
DEFAULT_PI_IDLE_TIMEOUT_S = 1800.0
DEFAULT_PI_MAX_WAIT_S = 21600.0


def positive_timeout_s(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed <= 0:
        return default
    return parsed


def _positive_timeout(name: str, default: float) -> float:
    return positive_timeout_s(os.environ.get(name, str(default)), default)


def codex_idle_timeout_s() -> float:
    return _positive_timeout("CCB_CODEX_IDLE_TIMEOUT_S", DEFAULT_CODEX_IDLE_TIMEOUT_S)


def codex_max_wait_s() -> float:
    return _positive_timeout("CCB_CODEX_MAX_WAIT_S", DEFAULT_CODEX_MAX_WAIT_S)


def pi_idle_timeout_s() -> float:
    """How long a Pi turn may stay SILENT before it is reported incomplete."""
    return _positive_timeout("CCB_PI_IDLE_TIMEOUT_S", DEFAULT_PI_IDLE_TIMEOUT_S)


def pi_max_wait_s() -> float:
    """The absolute cap on one Pi turn, however busy it looks (B1)."""
    return _positive_timeout("CCB_PI_MAX_WAIT_S", DEFAULT_PI_MAX_WAIT_S)


#: The provider-keyed view callers use when they must not spell out a branch.
IDLE_TIMEOUT_ENV = {
    "codex": ("CCB_CODEX_IDLE_TIMEOUT_S", DEFAULT_CODEX_IDLE_TIMEOUT_S),
    "pi": ("CCB_PI_IDLE_TIMEOUT_S", DEFAULT_PI_IDLE_TIMEOUT_S),
}
MAX_WAIT_ENV = {
    "codex": ("CCB_CODEX_MAX_WAIT_S", DEFAULT_CODEX_MAX_WAIT_S),
    "pi": ("CCB_PI_MAX_WAIT_S", DEFAULT_PI_MAX_WAIT_S),
}


def idle_timeout_s(provider: str) -> float:
    """The idle window for ``provider``; Codex's value for anything unlisted.

    Only Codex and Pi have progress mode today, so an unlisted provider keeps
    the long-standing Codex behaviour rather than inventing a third default.
    """
    name, default = IDLE_TIMEOUT_ENV.get(provider, IDLE_TIMEOUT_ENV["codex"])
    return _positive_timeout(name, default)


def max_wait_s(provider: str) -> float:
    """The absolute wait cap for ``provider``; Codex's value for anything unlisted."""
    name, default = MAX_WAIT_ENV.get(provider, MAX_WAIT_ENV["codex"])
    return _positive_timeout(name, default)
