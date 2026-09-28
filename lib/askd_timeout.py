"""Timeout settings shared by the ask client, daemon, and Codex adapter."""
from __future__ import annotations

import math
import os
from typing import Any


DEFAULT_CODEX_IDLE_TIMEOUT_S = 1800.0
DEFAULT_CODEX_MAX_WAIT_S = 21600.0


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
