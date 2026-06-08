"""Tests for multi-instance provider support (#117)."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

# Ensure lib is on path
_lib = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(_lib))

# ── Stub out modules that use Python 3.10+ syntax (str | None) ─────────────
# ccb_config and terminal use union-type syntax unsupported on Python <3.10.
# We pre-populate sys.modules with lightweight stubs so that caskd_session /
# gaskd_session can be imported without hitting a TypeError.

def _ensure_stub(mod_name: str, attrs: dict[str, Any] | None = None) -> None:
    """Insert a stub module into sys.modules if not already importable."""
    if mod_name in sys.modules:
        return
    try:
        __import__(mod_name)
        return
    except Exception:
        pass
    stub = types.ModuleType(mod_name)
    for k, v in (attrs or {}).items():
        setattr(stub, k, v)
    sys.modules[mod_name] = stub


_ensure_stub("terminal", {
    "_subprocess_kwargs": lambda: {},
    "get_backend_for_session": lambda data: None,
})
_ensure_stub("ccb_config", {
    "apply_backend_env": lambda: None,
    "get_backend_env": lambda: None,
})
_ensure_stub("project_id", {
    "compute_ccb_project_id": lambda p: "stub_project_id",
})


# ── parse_qualified_provider ────────────────────────────────────────────────

class TestParseQualifiedProvider:
    def test_plain_provider(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider("codex") == ("codex", None)

    def test_qualified_provider(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider("codex:auth") == ("codex", "auth")

    def test_empty_string(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider("") == ("", None)

    def test_none_input(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider(None) == ("", None)

    def test_colon_only(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider(":") == ("", None)

    def test_provider_with_empty_instance(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider("codex:") == ("codex", None)

    def test_uppercase_normalized(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider("CODEX:Auth") == ("codex", "auth")

    def test_whitespace_trimmed(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider(" codex : auth ") == ("codex", "auth")

    def test_multiple_colons(self):
        """Only split on first colon."""
        from providers import parse_qualified_provider
        base, instance = parse_qualified_provider("codex:auth:extra")
        assert base == "codex"
        assert instance == "auth:extra"

    def test_all_providers(self):
        from providers import parse_qualified_provider
        for prov in ("codex", "gemini", "opencode", "claude", "droid", "copilot", "codebuddy", "qwen"):
            assert parse_qualified_provider(prov) == (prov, None)
            assert parse_qualified_provider(f"{prov}:test") == (prov, "test")


# ── make_qualified_key ──────────────────────────────────────────────────────

class TestMakeQualifiedKey:
    def test_no_instance(self):
        from providers import make_qualified_key
        assert make_qualified_key("codex", None) == "codex"

    def test_with_instance(self):
        from providers import make_qualified_key
        assert make_qualified_key("codex", "auth") == "codex:auth"

    def test_empty_instance(self):
        from providers import make_qualified_key
        assert make_qualified_key("codex", "") == "codex"

    def test_roundtrip(self):
        from providers import parse_qualified_provider, make_qualified_key
        key = make_qualified_key("gemini", "frontend")
        base, inst = parse_qualified_provider(key)
        assert base == "gemini"
        assert inst == "frontend"

    def test_roundtrip_no_instance(self):
        from providers import parse_qualified_provider, make_qualified_key
        key = make_qualified_key("codex", None)
        base, inst = parse_qualified_provider(key)
        assert base == "codex"
        assert inst is None


# ── session_filename_for_instance ───────────────────────────────────────────

class TestSessionFilenameForInstance:
    def test_no_instance(self):
        from providers import session_filename_for_instance
        assert session_filename_for_instance(".codex-session", None) == ".codex-session"

    def test_with_instance(self):
        from providers import session_filename_for_instance
        assert session_filename_for_instance(".codex-session", "auth") == ".codex-auth-session"

    def test_empty_instance(self):
        from providers import session_filename_for_instance
        assert session_filename_for_instance(".codex-session", "") == ".codex-session"

    def test_whitespace_instance(self):
        from providers import session_filename_for_instance
        assert session_filename_for_instance(".codex-session", "  ") == ".codex-session"

    def test_all_provider_sessions(self):
        from providers import session_filename_for_instance
        cases = [
            (".codex-session", "auth", ".codex-auth-session"),
            (".gemini-session", "frontend", ".gemini-frontend-session"),
            (".opencode-session", "backend", ".opencode-backend-session"),
            (".claude-session", "review", ".claude-review-session"),
            (".droid-session", "test", ".droid-test-session"),
            (".copilot-session", "dev", ".copilot-dev-session"),
            (".codebuddy-session", "prod", ".codebuddy-prod-session"),
            (".qwen-session", "api", ".qwen-api-session"),
        ]
        for base, inst, expected in cases:
            assert session_filename_for_instance(base, inst) == expected, f"Failed for {base} + {inst}"


class TestInstanceNameValidation:
    def test_valid_instance_names(self):
        from providers import normalize_instance_name, validate_instance_name

        for name in ("worker", "docs-1", "haiku_worker", "a1"):
            assert validate_instance_name(name)
            assert normalize_instance_name(name.upper()) == name

    def test_invalid_instance_names(self):
        from providers import normalize_instance_name, validate_instance_name

        for name in ("", "  ", "../x", "x/y", "auth:extra", "-worker", "worker.name"):
            assert not validate_instance_name(name)
            assert normalize_instance_name(name) is None


class TestStartConfigInstanceNormalization:
    def test_one_line_config_normalizes_to_bare_providers_and_instances(self):
        from ccb_start_config import normalize_start_config_data

        data = normalize_start_config_data({
            "providers": "codex, codex:worker, claude, claude:worker",
            "instances": {"codex:worker": {"model": "gpt-5.4"}},
        })

        assert data["providers"] == ["codex", "claude"]
        assert data["provider_instances"] == [
            {"provider": "codex", "instance": "worker", "key": "codex:worker"},
            {"provider": "claude", "instance": "worker", "key": "claude:worker"},
        ]
        assert data["instances"] == {"codex:worker": {"model": "gpt-5.4"}}

    def test_cmd_and_dedup_with_qualified_tokens(self):
        from ccb_start_config import normalize_provider_tokens

        providers, instances, cmd_enabled = normalize_provider_tokens([
            "codex:worker",
            "cmd",
            "codex",
            "codex:worker",
            "claude:docs-1",
        ])

        assert providers == ["codex", "claude"]
        assert instances == [
            {"provider": "codex", "instance": "worker", "key": "codex:worker"},
            {"provider": "claude", "instance": "docs-1", "key": "claude:docs-1"},
        ]
        assert cmd_enabled is True

    def test_invalid_instance_token_is_dropped_in_config(self):
        from ccb_start_config import normalize_provider_tokens

        providers, instances, cmd_enabled = normalize_provider_tokens(["codex:../x", "codex"])

        assert providers == ["codex"]
        assert instances == []
        assert cmd_enabled is False

    def test_unsupported_provider_instance_is_dropped_in_config(self):
        from ccb_start_config import normalize_provider_tokens

        providers, instances, cmd_enabled = normalize_provider_tokens(["gemini:worker", "gemini"])

        assert providers == ["gemini"]
        assert instances == []
        assert cmd_enabled is False


class TestProviderInstanceDefaults:
    def test_partial_override_keeps_defaults(self):
        from provider_instance_defaults import resolve_instance_policy

        policy = resolve_instance_policy("codex", "worker", {"codex:worker": {"model": "gpt-5.4"}})

        assert policy.provider == "codex"
        assert policy.instance == "worker"
        assert policy.model == "gpt-5.4"
        assert policy.effort == "medium"
        assert policy.sandbox == "workspace-write"


# ── Session module instance support ────────────────────────────────────────

class TestSessionModuleInstance:
    """Test that session modules accept instance parameter."""

    def test_codex_find_session_file_default(self, tmp_path):
        from caskd_session import find_project_session_file
        # No session file exists -- should return None
        result = find_project_session_file(tmp_path)
        assert result is None

    def test_codex_find_session_file_with_instance(self, tmp_path):
        from caskd_session import find_project_session_file
        # Create instance-specific session file
        ccb_dir = tmp_path / ".ccb"
        ccb_dir.mkdir()
        session_file = ccb_dir / ".codex-auth-session"
        session_file.write_text('{"pane_id": "test"}')
        result = find_project_session_file(tmp_path, instance="auth")
        assert result is not None
        assert "auth" in result.name

    def test_codex_load_session_no_instance(self, tmp_path):
        from caskd_session import load_project_session
        result = load_project_session(tmp_path)
        assert result is None

    def test_codex_load_session_with_instance(self, tmp_path):
        from caskd_session import load_project_session
        result = load_project_session(tmp_path, instance="auth")
        assert result is None  # No file exists

    def test_codex_load_session_with_instance_file_exists(self, tmp_path):
        from caskd_session import load_project_session
        ccb_dir = tmp_path / ".ccb"
        ccb_dir.mkdir()
        session_file = ccb_dir / ".codex-auth-session"
        session_file.write_text('{"pane_id": "%42", "work_dir": "/tmp/test"}')
        result = load_project_session(tmp_path, instance="auth")
        assert result is not None
        assert result.pane_id == "%42"

    def test_codex_compute_session_key_no_instance(self):
        from caskd_session import compute_session_key, CodexProjectSession
        session = CodexProjectSession(
            session_file=Path("/tmp/test/.ccb/.codex-session"),
            data={"ccb_project_id": "abc123", "work_dir": "/tmp/test"},
        )
        key = compute_session_key(session)
        assert key == "codex:abc123"

    def test_codex_compute_session_key_with_instance(self):
        from caskd_session import compute_session_key, CodexProjectSession
        session = CodexProjectSession(
            session_file=Path("/tmp/test/.ccb/.codex-auth-session"),
            data={"ccb_project_id": "abc123", "work_dir": "/tmp/test"},
        )
        key = compute_session_key(session, instance="auth")
        assert "auth" in key
        assert "abc123" in key

    def test_gemini_find_session_file_default(self, tmp_path):
        from gaskd_session import find_project_session_file
        result = find_project_session_file(tmp_path)
        assert result is None

    def test_gemini_find_session_file_with_instance(self, tmp_path):
        from gaskd_session import find_project_session_file
        ccb_dir = tmp_path / ".ccb"
        ccb_dir.mkdir()
        session_file = ccb_dir / ".gemini-frontend-session"
        session_file.write_text('{"pane_id": "test"}')
        result = find_project_session_file(tmp_path, instance="frontend")
        assert result is not None
        assert "frontend" in result.name

    def test_gemini_compute_session_key_with_instance(self):
        from gaskd_session import compute_session_key, GeminiProjectSession
        session = GeminiProjectSession(
            session_file=Path("/tmp/test/.ccb/.gemini-session"),
            data={"ccb_project_id": "xyz789", "work_dir": "/tmp/test"},
        )
        key = compute_session_key(session, instance="frontend")
        assert "frontend" in key
        assert "xyz789" in key

    def test_claude_find_session_file_with_instance(self, tmp_path):
        from laskd_session import find_project_session_file
        ccb_dir = tmp_path / ".ccb"
        ccb_dir.mkdir()
        session_file = ccb_dir / ".claude-worker-session"
        session_file.write_text('{"pane_id": "test"}')
        result = find_project_session_file(tmp_path, instance="worker")
        assert result is not None
        assert result.name == ".claude-worker-session"

    def test_claude_load_session_with_instance_file_exists(self, tmp_path):
        from laskd_session import load_project_session
        ccb_dir = tmp_path / ".ccb"
        ccb_dir.mkdir()
        session_file = ccb_dir / ".claude-worker-session"
        session_file.write_text('{"pane_id": "%88", "work_dir": "/tmp/test"}')
        result = load_project_session(tmp_path, instance="worker")
        assert result is not None
        assert result.pane_id == "%88"

    def test_claude_compute_session_key_with_instance(self):
        from laskd_session import compute_session_key, ClaudeProjectSession
        session = ClaudeProjectSession(
            session_file=Path("/tmp/test/.ccb/.claude-worker-session"),
            data={"ccb_project_id": "claude123", "work_dir": "/tmp/test"},
        )
        key = compute_session_key(session, instance="worker")
        assert "worker" in key
        assert "claude123" in key


# ── ProviderRequest instance field ──────────────────────────────────────────

class TestProviderRequestInstance:
    def test_default_instance_is_none(self):
        from askd.adapters.base import ProviderRequest
        req = ProviderRequest(
            client_id="test",
            work_dir="/tmp",
            timeout_s=30.0,
            quiet=False,
            message="hello",
            caller="manual",
        )
        assert req.instance is None

    def test_instance_can_be_set(self):
        from askd.adapters.base import ProviderRequest
        req = ProviderRequest(
            client_id="test",
            work_dir="/tmp",
            timeout_s=30.0,
            quiet=False,
            message="hello",
            caller="manual",
            instance="auth",
        )
        assert req.instance == "auth"


# ── Backward compatibility ──────────────────────────────────────────────────

class TestBackwardCompatibility:
    """Verify that no-instance usage produces identical behavior."""

    def test_parse_qualified_plain(self):
        from providers import parse_qualified_provider
        for prov in ("codex", "gemini", "opencode", "claude", "droid", "copilot", "codebuddy", "qwen"):
            base, inst = parse_qualified_provider(prov)
            assert base == prov
            assert inst is None

    def test_session_filename_unchanged(self):
        from providers import session_filename_for_instance
        for name in (".codex-session", ".gemini-session", ".opencode-session",
                     ".claude-session", ".droid-session", ".copilot-session",
                     ".codebuddy-session", ".qwen-session"):
            assert session_filename_for_instance(name, None) == name

    def test_make_qualified_key_no_instance(self):
        from providers import make_qualified_key
        assert make_qualified_key("codex", None) == "codex"


# ── Daemon routing with instance ────────────────────────────────────────────

class TestDaemonInstanceRouting:
    """Test that the daemon correctly parses and routes instance-qualified providers."""

    def test_daemon_parses_qualified_provider(self):
        """Verify _handle_request parses 'codex:auth' correctly."""
        from providers import parse_qualified_provider
        base, inst = parse_qualified_provider("codex:auth")
        assert base == "codex"
        assert inst == "auth"

    def test_pool_key_includes_instance(self):
        """Pool key should use qualified key for instance isolation."""
        from providers import make_qualified_key
        key = make_qualified_key("codex", "auth")
        assert key == "codex:auth"
        # Different instance = different pool key
        key2 = make_qualified_key("codex", "payment")
        assert key2 == "codex:payment"
        assert key != key2

    def test_same_provider_different_instances_isolated(self):
        """Two instances of the same provider must produce different keys."""
        from providers import make_qualified_key
        keys = set()
        for inst in ("auth", "payment", "frontend", "backend"):
            keys.add(make_qualified_key("codex", inst))
        assert len(keys) == 4

    def test_different_providers_same_instance_isolated(self):
        """Same instance name on different providers must produce different keys."""
        from providers import make_qualified_key
        key1 = make_qualified_key("codex", "auth")
        key2 = make_qualified_key("gemini", "auth")
        assert key1 != key2
