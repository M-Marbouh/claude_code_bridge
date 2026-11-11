"""M3 W3 — destination-side `ccb-remote pairing inspect|apply|revoke`.

Everything here is exercised against a temporary HOME, so no test reads or
writes the machine's real `~/.ssh` or `~/.ccb`. The values use the same generic
names as the shipped guide (`laptop-a`, `server`, `demo`, `alice`).
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import remote_pairing


GATE = "/opt/ccb/bin/ccb-remote-gate"
PUBKEY_ONE = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIoneoneoneoneoneoneoneoneoneoneoneone1 laptop-a"
PUBKEY_TWO = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItwotwotwotwotwotwotwotwotwotwotwotwo2 other-b"
THIS_BLOB = "dGhpcy1ibG9iLTAxMjM0NTY3ODktYWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo="
OTHER_BLOB = "b3RoZXItYmxvYi0wMTIzNDU2Nzg5LWFiY2RlZmdoaWprbG1ub3BxcnN0dXZ3eHk="
PRINCIPAL = "laptop-a"
TOKEN = "alice1"


def _paths(home: Path) -> dict:
    return remote_pairing.destination_paths(home)


def _hostkey(blob: str = "AAAAC3NzaC1lZDI1NTE5AAAAIhosthosthosthosthosthosthosthosthostho1") -> dict:
    return {"type": "ssh-ed25519", "key": blob}


def _apply_request(
    state_hash: str,
    *,
    principal: str = PRINCIPAL,
    pubkey: str = PUBKEY_ONE,
    token: str = TOKEN,
    aliases: list[dict] | None = None,
) -> dict:
    return {
        "verb": "apply",
        "principal": principal,
        "pubkey": pubkey,
        "issuer_token": token,
        "aliases": aliases
        or [{"alias": "demo", "work_dir": "/srv/demo", "providers": ["codex", "claude"]}],
        "expected_state_hash": state_hash,
    }


def _inspect(
    home: Path,
    *,
    gate: Path | None = None,
    lookup_principal: str = "",
    lookup_pubkey: str = "",
) -> dict:
    return remote_pairing.inspect_destination(
        home=home,
        gate_path=gate or Path(GATE),
        host_key_dir=_write_host_key_dir(home),
        registry_dir=home / ".ccb" / "run",
        lookup_principal=lookup_principal,
        lookup_pubkey=lookup_pubkey,
    )


def _write_host_key_dir(home: Path) -> Path:
    directory = home / "etc-ssh"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ssh_host_ed25519_key.pub").write_text(
        f"ssh-ed25519 {_hostkey()['key']} root@server\n", encoding="utf-8"
    )
    (directory / "ssh_host_rsa_key.pub").write_text(
        "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQrsa root@server\n", encoding="utf-8"
    )
    return directory


def _make_gate(tmp_path: Path) -> Path:
    gate = tmp_path / "ccb-remote-gate"
    gate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gate.chmod(0o755)
    return gate


def _seed_other_pairing(home: Path) -> None:
    """A different principal's key line + a shared alias already on B."""
    ssh_dir = _paths(home)["ssh_dir"]
    ssh_dir.mkdir(parents=True, exist_ok=True)
    (ssh_dir / "authorized_keys").write_text(
        'restrict,command="/opt/ccb/bin/ccb-remote-gate --principal other-b" '
        f"{PUBKEY_TWO}\n",
        encoding="utf-8",
    )
    (ssh_dir / "authorized_keys").chmod(0o600)
    remote_root = _paths(home)["remote_root"]
    remote_root.mkdir(parents=True, exist_ok=True)
    (remote_root / "config.json").write_text(
        json.dumps(
            {
                "principals": {
                    "other-b": {"issuer_token": "other1", "allowed_aliases": ["demo"]}
                },
                "aliases": {"demo": {"work_dir": "/srv/demo", "providers": ["codex", "claude"]}},
            }
        ),
        encoding="utf-8",
    )
    (remote_root / "config.json").chmod(0o600)


def _bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


# --------------------------------------------------------------------------- #
# inspect
# --------------------------------------------------------------------------- #


def test_inspect_reports_what_a_pairing_would_find(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    registry = home / ".ccb" / "run"
    registry.mkdir(parents=True)
    (registry / "ccb-session-ai-1.json").write_text(
        json.dumps(
            {
                "work_dir": "/srv/demo",
                "providers": {"codex": {"pane_id": "%1"}, "claude": {}, "gemini": {}},
            }
        ),
        encoding="utf-8",
    )
    _seed_other_pairing(home)

    result = remote_pairing.inspect_destination(
        home=home,
        gate_path=gate,
        host_key_dir=_write_host_key_dir(home),
        registry_dir=registry,
        lookup_pubkey=PUBKEY_TWO,
    )

    assert result["ok"] is True
    assert result["gate"] == str(gate)
    assert result["gate_present"] is True
    assert {item["type"] for item in result["host_keys"]} == {"ssh-ed25519", "ssh-rsa"}
    # Another principal's token is NEVER part of the answer.
    assert "issuer_token" not in json.dumps(result)
    assert result["principals"] == {"other-b": {"allowed_aliases": ["demo"]}}
    assert result["aliases"]["demo"]["work_dir"] == "/srv/demo"
    assert [project["work_dir"] for project in result["projects"]] == ["/srv/demo"]
    assert result["projects"][0]["providers"] == ["codex", "claude"]
    assert result["bound_principals"] == ["other-b"]
    assert len(result["state_hash"]) == 64


def test_inspect_reports_a_missing_gate_without_writing(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)

    result = remote_pairing.inspect_destination(
        home=home,
        gate_path=tmp_path / "missing-gate",
        host_key_dir=_write_host_key_dir(home),
        registry_dir=home / ".ccb" / "run",
    )

    assert result["gate_present"] is False
    assert not _paths(home)["authorized_keys"].exists()
    assert not _paths(home)["config"].exists()


def test_inspect_refuses_an_unusable_existing_config(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    config = _paths(home)["config"]
    config.parent.mkdir(parents=True)
    config.write_text("{not json", encoding="utf-8")

    with pytest.raises(remote_pairing.PairingError):
        _inspect(home, gate=_make_gate(tmp_path))


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #


def test_apply_installs_one_restricted_line_and_the_grants(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)

    applied = remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )

    assert applied["ok"] is True
    assert applied["key_line_added"] is True
    keys = _paths(home)["authorized_keys"].read_text()
    assert keys == (
        f'restrict,command="{gate} --principal {PRINCIPAL}" {PUBKEY_ONE}\n'
    )
    document = json.loads(_paths(home)["config"].read_text())
    grant = dict(document["principals"][PRINCIPAL])
    assert grant.pop("pubkey_sha256")
    assert grant == {
        "issuer_token": TOKEN,
        "allowed_aliases": ["demo"],
    }
    assert document["aliases"]["demo"] == {
        "work_dir": "/srv/demo",
        # Normalised to the protocol's provider order, so re-pairing is a no-op.
        "providers": ["claude", "codex"],
    }
    # Private files, private directory.
    assert stat.S_IMODE(_paths(home)["authorized_keys"].stat().st_mode) == 0o600
    assert stat.S_IMODE(_paths(home)["config"].stat().st_mode) == 0o600
    assert stat.S_IMODE(_paths(home)["remote_root"].stat().st_mode) == 0o700


def test_apply_is_idempotent_and_keeps_the_same_state(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    first = remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )
    before = _paths(home)["authorized_keys"].read_bytes(), _paths(home)["config"].read_bytes()

    second = remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )

    assert first["state_hash"] == second["state_hash"]
    assert second["key_line_added"] is False and second["config_changed"] is False
    assert (_paths(home)["authorized_keys"].read_bytes(), _paths(home)["config"].read_bytes()) == before


def test_apply_preserves_another_pairing_and_its_shared_alias(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    _seed_other_pairing(home)

    remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )

    keys = _paths(home)["authorized_keys"].read_text()
    assert "other-b" in keys and PRINCIPAL in keys
    document = json.loads(_paths(home)["config"].read_text())
    assert document["principals"]["other-b"]["issuer_token"] == "other1"
    assert document["principals"][PRINCIPAL]["issuer_token"] == TOKEN


@pytest.mark.parametrize(
    "reason",
    ["state_hash", "principal_token", "principal_key", "pubkey_other_principal", "alias_dir", "alias_providers"],
)
def test_apply_refuses_conflicts_with_zero_writes(tmp_path, reason):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    _seed_other_pairing(home)
    request = _apply_request(_inspect(home, gate=gate)["state_hash"])
    if reason == "state_hash":
        request["expected_state_hash"] = "0" * 64
    elif reason == "principal_token":
        # Same principal name, different token: only reachable by re-pairing.
        remote_pairing.apply_destination(request, home=home, gate_path=gate)
        request["expected_state_hash"] = _inspect(home, gate=gate)["state_hash"]
        request["issuer_token"] = "other9"
    elif reason == "principal_key":
        request["pubkey"] = PUBKEY_TWO
        request["expected_state_hash"] = _inspect(home, gate=gate)["state_hash"]
        request["principal"] = "other-b"
    elif reason == "pubkey_other_principal":
        request["pubkey"] = PUBKEY_TWO
        request["expected_state_hash"] = _inspect(home, gate=gate)["state_hash"]
    elif reason == "alias_dir":
        request["aliases"] = [
            {"alias": "demo", "work_dir": "/srv/other", "providers": ["codex", "claude"]}
        ]
    else:
        request["aliases"] = [
            {"alias": "demo", "work_dir": "/srv/demo", "providers": ["claude"]}
        ]
    before = (
        _bytes(_paths(home)["authorized_keys"]),
        _bytes(_paths(home)["config"]),
        sorted(path.name for path in home.rglob("*.bak-*")),
    )

    with pytest.raises(remote_pairing.PairingError):
        remote_pairing.apply_destination(request, home=home, gate_path=gate)

    after = (
        _bytes(_paths(home)["authorized_keys"]),
        _bytes(_paths(home)["config"]),
        sorted(path.name for path in home.rglob("*.bak-*")),
    )
    assert after == before
    if reason != "principal_token":
        # (that case legitimately created the pairing first, then changed its token)
        assert f"--principal {PRINCIPAL}" not in _paths(home)["authorized_keys"].read_text()


def test_apply_refuses_without_a_gate_on_this_machine(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    request = _apply_request(_inspect(home)["state_hash"])

    with pytest.raises(remote_pairing.PairingError) as excinfo:
        remote_pairing.apply_destination(request, home=home, gate_path=tmp_path / "nope")

    assert "gate" in str(excinfo.value)
    assert not _paths(home)["authorized_keys"].exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"principal": "Laptop-A"},
        {"principal": "laptop a"},
        {"issuer_token": "bad token"},
        {"pubkey": "ssh-ed25519 !!!not-base64"},
        {"aliases": [{"alias": "../escape", "work_dir": "/srv/demo", "providers": ["codex"]}]},
        {"aliases": [{"alias": "demo", "work_dir": "relative/path", "providers": ["codex"]}]},
        {"aliases": [{"alias": "demo", "work_dir": "/srv/demo", "providers": ["gemini"]}]},
        {"aliases": [{"alias": "demo", "work_dir": "/srv/de\nmo", "providers": ["codex"]}]},
        {"aliases": []},
    ],
)
def test_apply_refuses_hostile_or_malformed_values(tmp_path, overrides):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    request = _apply_request(_inspect(home, gate=gate)["state_hash"])
    request.update(overrides)

    with pytest.raises(remote_pairing.PairingError):
        remote_pairing.apply_destination(request, home=home, gate_path=gate)

    assert not _paths(home)["authorized_keys"].exists()
    assert not _paths(home)["config"].exists()


def test_values_with_shell_metacharacters_are_data_never_commands(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    marker = tmp_path / "pwned"
    work_dir = f"/srv/demo $(touch {marker}) `touch {marker}`"

    applied = remote_pairing.apply_destination(
        _apply_request(
            _inspect(home, gate=gate)["state_hash"],
            aliases=[{"alias": "demo", "work_dir": work_dir, "providers": ["codex"]}],
        ),
        home=home,
        gate_path=gate,
    )

    assert applied["ok"] is True
    # Stored verbatim in the JSON document, never executed and never mangled.
    document = json.loads(_paths(home)["config"].read_text())
    assert document["aliases"]["demo"]["work_dir"] == work_dir
    assert not marker.exists()


def test_a_key_line_with_quotes_cannot_be_installed(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    hostile_gate = tmp_path / 'ga"te'
    hostile_gate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hostile_gate.chmod(0o755)

    with pytest.raises(remote_pairing.PairingError):
        remote_pairing.apply_destination(
            _apply_request(_inspect(home)["state_hash"]), home=home, gate_path=hostile_gate
        )

    assert not _paths(home)["authorized_keys"].exists()


# --------------------------------------------------------------------------- #
# revoke
# --------------------------------------------------------------------------- #


def test_revoke_removes_only_this_pairings_line_and_grant(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    _seed_other_pairing(home)
    remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )

    revoked = remote_pairing.revoke_destination(
        {
            "verb": "revoke",
            "principal": PRINCIPAL,
            "pubkey": PUBKEY_ONE,
            "issuer_token": TOKEN,
            "expected_state_hash": _inspect(home, gate=gate)["state_hash"],
        },
        home=home,
    )

    assert revoked["key_lines_removed"] == 1
    assert revoked["principal_removed"] is True
    keys = _paths(home)["authorized_keys"].read_text()
    assert "other-b" in keys
    assert f"--principal {PRINCIPAL}" not in keys
    document = json.loads(_paths(home)["config"].read_text())
    assert "other-b" in document["principals"] and PRINCIPAL not in document["principals"]
    # The shared alias stays: it may serve the other principal.
    assert document["aliases"]["demo"]["work_dir"] == "/srv/demo"
    assert revoked["aliases_kept"] == ["demo"]


def test_revoke_refuses_when_the_principal_holds_a_different_key(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    _seed_other_pairing(home)  # "other-b" holds PUBKEY_TWO

    before = (
        _bytes(_paths(home)["authorized_keys"]),
        _bytes(_paths(home)["config"]),
    )
    with pytest.raises(remote_pairing.PairingError):
        # Wrong key for that principal: removing it anyway would revoke a
        # pairing this machine never installed.
        remote_pairing.revoke_destination(
            {
                "verb": "revoke",
                "principal": "other-b",
                "pubkey": PUBKEY_ONE,
                "issuer_token": "other1",
                "expected_state_hash": _inspect(home, gate=_make_gate(tmp_path))["state_hash"],
            },
            home=home,
        )
    after = (
        _bytes(_paths(home)["authorized_keys"]),
        _bytes(_paths(home)["config"]),
    )
    assert after == before


def test_revoke_is_idempotent_after_a_successful_removal(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )
    request = {
        "verb": "revoke",
        "principal": PRINCIPAL,
        "pubkey": PUBKEY_ONE,
        "issuer_token": TOKEN,
        "expected_state_hash": _inspect(home, gate=gate)["state_hash"],
    }
    remote_pairing.revoke_destination(dict(request), home=home)

    twice = dict(request, expected_state_hash=_inspect(home, gate=gate)["state_hash"])
    again = remote_pairing.revoke_destination(twice, home=home)

    assert again["key_lines_removed"] == 0
    assert again["principal_removed"] is False


def test_state_hash_distinguishes_absent_from_empty(tmp_path):
    assert remote_pairing.state_hash(b"", b"") != remote_pairing.state_hash(b"\n", b"")
    assert remote_pairing.state_hash(b"", b"{}") == remote_pairing.state_hash(b"", b"{}")


# --------------------------------------------------------------------------- #
# B4: a two-file transaction that commits the grant before the key line
# --------------------------------------------------------------------------- #


def test_apply_commits_the_grant_before_the_key_line(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    request = _apply_request(_inspect(home, gate=gate)["state_hash"])
    seen: list[str] = []
    real = remote_pairing.atomic_write

    def record(path, data, **kwargs):
        seen.append(path.name)
        return real(path, data, **kwargs)

    monkeypatch.setattr(remote_pairing, "atomic_write", record)

    remote_pairing.apply_destination(request, home=home, gate_path=gate)

    assert seen.index("config.json") < seen.index("authorized_keys")


def test_a_fault_between_the_two_renames_is_completed_by_a_retry(tmp_path, monkeypatch):
    """The grant is durable before the key line; a retry finishes the pair."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    real = remote_pairing.atomic_write
    calls = {"n": 0}

    def fail_second(path, data, **kwargs):
        calls["n"] += 1
        if path.name == "authorized_keys":
            raise OSError("simulated power loss between the two renames")
        return real(path, data, **kwargs)

    monkeypatch.setattr(remote_pairing, "atomic_write", fail_second)
    with pytest.raises(OSError):
        remote_pairing.apply_destination(
            _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
        )
    monkeypatch.setattr(remote_pairing, "atomic_write", real)

    # Intermediate state: the grant exists, the key line does not (fail closed).
    document = json.loads(_paths(home)["config"].read_text())
    assert document["principals"][PRINCIPAL]["issuer_token"] == TOKEN
    assert _bytes(_paths(home)["authorized_keys"]) == b""

    resumed = remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )

    assert resumed["ok"] is True
    keys = _paths(home)["authorized_keys"].read_text()
    assert keys.count(f"--principal {PRINCIPAL}") == 1
    assert json.loads(_paths(home)["config"].read_text())["principals"][PRINCIPAL][
        "allowed_aliases"
    ] == ["demo"]


def test_a_lost_apply_response_is_idempotent_on_retry(tmp_path):
    """B applied, A never saw the answer: re-running must change nothing."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    first = remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )
    keys, config = _bytes(_paths(home)["authorized_keys"]), _bytes(_paths(home)["config"])

    retried = remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )

    assert retried["key_line_added"] is False and retried["config_changed"] is False
    assert retried["state_hash"] == first["state_hash"]
    assert (_bytes(_paths(home)["authorized_keys"]), _bytes(_paths(home)["config"])) == (
        keys,
        config,
    )


def test_apply_requires_the_state_hash_it_was_inspected_with(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    request = _apply_request("")

    with pytest.raises(remote_pairing.PairingError):
        remote_pairing.apply_destination(request, home=home, gate_path=gate)

    assert not _paths(home)["authorized_keys"].exists()
    assert not _paths(home)["config"].exists()


# --------------------------------------------------------------------------- #
# B5: revoke ownership (token, hash, principal-only leftovers)
# --------------------------------------------------------------------------- #


def test_revoke_refuses_a_token_from_a_different_pairing(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    _seed_other_pairing(home)
    remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )
    before = (_bytes(_paths(home)["authorized_keys"]), _bytes(_paths(home)["config"]))

    with pytest.raises(remote_pairing.PairingError):
        remote_pairing.revoke_destination(
            {
                "principal": PRINCIPAL,
                "pubkey": PUBKEY_ONE,
                "issuer_token": "other1",
                "expected_state_hash": _inspect(home, gate=gate)["state_hash"],
            },
            home=home,
        )

    assert (_bytes(_paths(home)["authorized_keys"]), _bytes(_paths(home)["config"])) == before


def test_revoke_refuses_a_stale_state_hash(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )
    stale = _inspect(home, gate=gate)["state_hash"]
    # Something changes elsewhere on the machine after the inspection.
    (home / ".ssh" / "authorized_keys").write_text(
        _paths(home)["authorized_keys"].read_text() + "# another pairing touched this file\n",
        encoding="utf-8",
    )

    with pytest.raises(remote_pairing.PairingError):
        remote_pairing.revoke_destination(
            {
                "principal": PRINCIPAL,
                "pubkey": PUBKEY_ONE,
                "issuer_token": TOKEN,
                "expected_state_hash": stale,
            },
            home=home,
        )
    assert f"--principal {PRINCIPAL}" in _paths(home)["authorized_keys"].read_text()


def test_revoke_removes_the_grant_before_the_key_line(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )
    seen: list[str] = []
    real = remote_pairing.atomic_write

    def record(path, data, **kwargs):
        seen.append(path.name)
        return real(path, data, **kwargs)

    monkeypatch.setattr(remote_pairing, "atomic_write", record)
    remote_pairing.revoke_destination(
        {
            "principal": PRINCIPAL,
            "pubkey": PUBKEY_ONE,
            "issuer_token": TOKEN,
            "expected_state_hash": _inspect(home, gate=gate)["state_hash"],
        },
        home=home,
    )

    assert seen.index("config.json") < seen.index("authorized_keys")


def test_revoke_refuses_a_line_pairing_did_not_write(tmp_path):
    """A line carrying our key with other options is not ours to remove."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    _paths(home)["ssh_dir"].mkdir(parents=True, exist_ok=True)
    _paths(home)["authorized_keys"].write_text(
        f'from="10.0.0.5",restrict,command="{gate} --principal {PRINCIPAL}" {PUBKEY_ONE}\n',
        encoding="utf-8",
    )

    with pytest.raises(remote_pairing.PairingError) as excinfo:
        remote_pairing.revoke_destination(
            {
                "principal": PRINCIPAL,
                "pubkey": PUBKEY_ONE,
                "issuer_token": TOKEN,
                "expected_state_hash": _inspect(home, gate=gate)["state_hash"],
            },
            home=home,
        )
    assert "did not write" in str(excinfo.value)
    assert f"--principal {PRINCIPAL}" in _paths(home)["authorized_keys"].read_text()


# --------------------------------------------------------------------------- #
# B6: shared local files (pin merge, rotation, comma-separated names)
# --------------------------------------------------------------------------- #


def test_known_hosts_merge_requires_rotation_for_a_different_pin():
    host, port = "server.example.test", 2222
    existing = f"[{host}]:{port} ssh-ed25519 {OTHER_BLOB}\n"
    verified = [{"type": "ssh-ed25519", "key": THIS_BLOB}]

    with pytest.raises(remote_pairing.PairingError) as excinfo:
        remote_pairing.known_hosts_merge(existing, host, port, verified)

    assert "--rotate-pin" in str(excinfo.value)
    assert remote_pairing.known_hosts_merge(
        existing, host, port, verified, rotate=True
    ) == f"[{host}]:{port} ssh-ed25519 {THIS_BLOB}\n"


def test_known_hosts_merge_keeps_the_other_names_on_a_shared_line():
    host, port = "server.example.test", 2222
    existing = (
        f"other.example.test,[{host}]:{port} ssh-ed25519 {THIS_BLOB}\n"
        "third.example.test ssh-ed25519 AAAAthird\n"
    )

    merged = remote_pairing.known_hosts_merge(
        existing, host, port, [{"type": "ssh-ed25519", "key": THIS_BLOB}]
    )

    lines = [line for line in merged.splitlines() if line.strip()]
    # The shared line keeps its other name; exactly one line names the endpoint.
    assert "other.example.test ssh-ed25519" in lines[0]
    assert any(line.startswith("third.example.test ") for line in lines)
    assert sum(1 for line in lines if f"[{host}]:{port}" in line) == 1


def test_known_hosts_remove_keeps_a_shared_line_for_the_other_name():
    host, port = "server.example.test", 2222
    existing = f"other.example.test,[{host}]:{port} ssh-ed25519 {THIS_BLOB}\n"

    merged, removed = remote_pairing.known_hosts_remove(existing, host, port)

    assert removed == 1
    assert merged.startswith("other.example.test ssh-ed25519")
    assert f"[{host}]:{port}" not in merged


def test_known_hosts_merge_keeps_hashed_entries_for_other_hosts():
    import base64
    import hashlib
    import hmac

    def hashed(name: str) -> str:
        salt = b"0123456789abcdefghij"
        digest = hmac.new(salt, name.encode(), hashlib.sha1).digest()
        return "|1|" + base64.b64encode(salt).decode() + "|" + base64.b64encode(digest).decode()

    host, port = "server.example.test", 22
    other = hashed("other.example.test")
    ours = hashed(host)
    existing = f"{other} ssh-ed25519 AAAAother\n{ours} ssh-ed25519 {OTHER_BLOB}\n"

    merged = remote_pairing.known_hosts_merge(
        existing, host, port, [{"type": "ssh-ed25519", "key": THIS_BLOB}], rotate=True
    )

    lines = [line for line in merged.splitlines() if line.strip()]
    assert any(line.startswith(other) for line in lines)
    assert not any(line.startswith(ours) for line in lines)
    assert f"{host} ssh-ed25519 {THIS_BLOB}" in lines


# --------------------------------------------------------------------------- #
# A's side: a failed `pairing` call has to say what is actually wrong
# --------------------------------------------------------------------------- #

#: The verbatim USAGE a B whose installed CCB predates W3 dumps after its stderr
#: line (copied from that install's `bin/ccb-remote` USAGE constant).
OLD_PEER_USAGE = """Usage:
  ccb-remote status <host-qualified-id>   Ask the destination for this request's state
  ccb-remote list <host> [--json]         List the projects this key may address on <host>
  ccb-remote reply <host-qualified-id>    Answer a delivered request (answer on stdin)
  ccb-remote -h                           Show this help

`reply` runs on the machine that received the request, from the pane that
received it, and sends exactly one answer:

    ccb-remote reply <host-qualified-id> <<'CCB_REPLY'
    the answer
    CCB_REPLY

Cross-machine delivery is pull-based: the source fetches the answer; the
destination never connects back. `status` is the recovery path for a delivery
whose acknowledgement was lost.
"""

PAIR_ARGV = [
    "ssh",
    "-T",
    "laptop-a",
    '"$HOME"/.local/bin/ccb-remote',
    "pairing",
    "inspect",
]


def _fake_ssh(monkeypatch, *, returncode: int, stdout: str = "", stderr: str = "") -> list[list[str]]:
    """Stand in for one ssh run; records the argv it was handed."""
    seen: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(remote_pairing.subprocess, "run", runner)
    return seen


def _failure(monkeypatch, *, returncode: int, stdout: str = "", stderr: str = "", **kwargs) -> str:
    _fake_ssh(monkeypatch, returncode=returncode, stdout=stdout, stderr=stderr)
    with pytest.raises(remote_pairing.PairingError) as excinfo:
        remote_pairing.run_pairing(PAIR_ARGV, {"verb": "inspect"}, **kwargs)
    return str(excinfo.value)


def test_a_b_too_old_to_pair_says_to_update_it(monkeypatch):
    message = _failure(
        monkeypatch,
        returncode=1,
        stderr="[ERROR] Unknown ccb-remote command: pairing\n" + OLD_PEER_USAGE,
    )

    assert "too old to pair" in message
    assert "update CCB there" in message
    # B's usage dump is never the message.
    assert "Usage:" not in message
    assert "ccb-remote status" not in message
    assert "CCB_REPLY" not in message


def test_a_missing_ccb_remote_names_the_path_it_looked_for(monkeypatch):
    path = "/home/x/.local/bin/ccb-remote"
    message = _failure(
        monkeypatch,
        returncode=127,
        stderr=f"bash: line 1: {path}: No such file or directory\n",
        remote_ccb=path,
    )

    assert "was not found on the other machine" in message
    assert path in message
    assert "--remote-ccb" in message
    assert "$HOME" not in message


def test_a_missing_ccb_remote_names_the_path_even_without_an_exit_code(monkeypatch):
    # Exit 1 with the shell's own words, not 127: still the not-found message.
    message = _failure(
        monkeypatch,
        returncode=1,
        stderr="sh: /opt/ccb/bin/ccb-remote: No such file or directory\n",
        remote_ccb="/opt/ccb/bin/ccb-remote",
    )

    assert "/opt/ccb/bin/ccb-remote" in message
    assert "was not found on the other machine" in message


def test_the_default_remote_path_is_named_unquoted(monkeypatch):
    message = _failure(monkeypatch, returncode=127, stderr="bash: line 1: ccb-remote: command not found\n")

    assert "~/.local/bin/ccb-remote" in message
    assert "$HOME" not in message


def test_the_path_the_user_named_reaches_the_failure_message(monkeypatch):
    import remote_pair_cli

    path = "~/bin/ccb-remote"
    _fake_ssh(
        monkeypatch,
        returncode=127,
        stderr="bash: line 1: /home/x/bin/ccb-remote: No such file or directory\n",
    )

    channel = remote_pair_cli._Channel(destination="laptop-a", port=22, remote_ccb=path)
    with pytest.raises(remote_pairing.PairingError) as excinfo:
        channel.call("inspect", {"verb": "inspect"})

    # The message names the user's own path, never the quoted wire form.
    assert path in str(excinfo.value)
    assert "$HOME" not in str(excinfo.value)
    # The wire form is unchanged: ssh still gets the shell-expanded one.
    assert '"$HOME"/bin/ccb-remote' in channel.argv("inspect")


def test_ssh_itself_failing_keeps_ssh_text(monkeypatch):
    message = _failure(
        monkeypatch,
        returncode=255,
        stderr="ssh: connect to host laptop-a port 22: Connection refused\n",
    )

    assert "the pairing connection failed" in message
    assert "Connection refused" in message


def test_a_generic_failure_uses_the_error_line_not_the_usage_tail(monkeypatch):
    message = _failure(
        monkeypatch,
        returncode=1,
        stderr=(
            "warning: something harmless\n"
            "[ERROR] the state hash does not match; nothing was written\n"
            + OLD_PEER_USAGE
        ),
    )

    assert "the state hash does not match" in message
    assert "Usage:" not in message
    assert "ccb-remote list" not in message


def test_a_plain_failure_still_reports_the_stderr_tail(monkeypatch):
    message = _failure(monkeypatch, returncode=1, stderr="something went wrong on B\n")

    assert "the pairing connection failed" in message
    assert "something went wrong on B" in message


def test_a_json_refusal_with_a_nonzero_exit_is_unchanged(monkeypatch):
    refusal = {"ok": False, "error": "the state hash does not match"}
    seen = _fake_ssh(
        monkeypatch,
        returncode=1,
        stdout=json.dumps(refusal) + "\n",
        stderr="noise that must not matter\n",
    )

    result = remote_pairing.run_pairing(PAIR_ARGV, {"verb": "apply"})

    assert result == refusal
    assert len(seen) == 1  # no retry, no second remote command


# --------------------------------------------------------------------------- #
# Review fixes (task 20261111-180019): presence by key blob, ownership
# evidence, definitive refusals, and one consistent read
# --------------------------------------------------------------------------- #

REPO = Path(__file__).resolve().parents[1]
CCB_REMOTE = REPO / "bin" / "ccb-remote"

#: The finding-2 fixture: somebody else's line carrying the same key blob.
FOREIGN_LINE = (
    f'from="10.0.0.5",restrict,command="{GATE} --principal {PRINCIPAL}" '
    f"{PUBKEY_ONE} ccb-remote main"
)


def _seed(home: Path, *, keys: str = "", config: dict | None = None) -> None:
    paths = _paths(home)
    paths["ssh_dir"].mkdir(parents=True, exist_ok=True)
    if keys:
        paths["authorized_keys"].write_text(keys, encoding="utf-8")
    paths["remote_root"].mkdir(parents=True, exist_ok=True)
    if config is not None:
        paths["config"].write_text(json.dumps(config), encoding="utf-8")


def _revoke_request(home: Path, gate: Path, *, pubkey: str = PUBKEY_ONE, token: str = TOKEN) -> dict:
    return {
        "verb": "revoke",
        "principal": PRINCIPAL,
        "pubkey": pubkey,
        "issuer_token": token,
        "expected_state_hash": _inspect(home, gate=gate)["state_hash"],
    }


def test_apply_refuses_a_second_line_for_a_key_already_present(tmp_path):
    """Fix 2: any line with this blob is a conflict unless it is exactly ours."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    _seed(home, keys=FOREIGN_LINE + "\n")
    paths = _paths(home)
    before = (_bytes(paths["authorized_keys"]), _bytes(paths["config"]))

    with pytest.raises(remote_pairing.PairingError) as excinfo:
        remote_pairing.apply_destination(
            _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
        )

    assert "already appears in another authorized_keys line" in str(excinfo.value)
    assert excinfo.value.refused is True
    assert (_bytes(paths["authorized_keys"]), _bytes(paths["config"])) == before


def test_apply_still_completes_when_the_exact_line_is_already_there(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    request = _apply_request(_inspect(home, gate=gate)["state_hash"])
    remote_pairing.apply_destination(request, home=home, gate_path=gate)

    again = remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )

    assert again["key_line_added"] is False
    assert _paths(home)["authorized_keys"].read_text().count("ssh-ed25519") == 1


def test_inspect_reports_a_foreign_line_as_not_the_installed_one(tmp_path):
    """Fix 2: inspect's report uses the same \"exact line\" rule as apply."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    _seed(home, keys=FOREIGN_LINE + "\n")

    result = remote_pairing.inspect_destination(
        home=home,
        gate_path=gate,
        lookup_principal=PRINCIPAL,
        lookup_pubkey=PUBKEY_ONE,
        host_key_dir=_write_host_key_dir(home),
        registry_dir=home / ".ccb" / "run",
    )

    assert result["key_blob_present"] is True
    assert result["existing_key_line"] is False
    assert result["key_line_exact_principals"] == []
    # The foreign line is not a shape `pairing` writes, so it is not reported
    # as a pairing at all -- only as "this key is already here".
    assert result["key_line_principals"] == []


def test_revoke_finishes_after_the_grant_was_already_removed(tmp_path, monkeypatch):
    """Fix 7: a revoke cut in half (grant gone, key line left) must finish."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )
    real = remote_pairing.atomic_write

    def fail_on_keys(path, data, **kwargs):
        if path.name == "authorized_keys":
            raise OSError("simulated power loss before the key line went")
        return real(path, data, **kwargs)

    monkeypatch.setattr(remote_pairing, "atomic_write", fail_on_keys)
    with pytest.raises(OSError):
        remote_pairing.revoke_destination(_revoke_request(home, gate), home=home)
    monkeypatch.setattr(remote_pairing, "atomic_write", real)

    document = json.loads(_paths(home)["config"].read_text())
    assert PRINCIPAL not in document["principals"]  # the grant is gone
    assert f"--principal {PRINCIPAL}" in _paths(home)["authorized_keys"].read_text()

    finished = remote_pairing.revoke_destination(_revoke_request(home, gate), home=home)

    assert finished["key_lines_removed"] == 1
    assert finished["principal_removed"] is False
    assert f"--principal {PRINCIPAL}" not in _paths(home)["authorized_keys"].read_text()


def test_revoke_removes_a_grant_whose_key_line_never_got_written(tmp_path, monkeypatch):
    """Fix 7: an interrupted apply left a grant with the recorded key."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    real = remote_pairing.atomic_write

    def fail_on_keys(path, data, **kwargs):
        if path.name == "authorized_keys":
            raise OSError("simulated power loss before the key line went")
        return real(path, data, **kwargs)

    monkeypatch.setattr(remote_pairing, "atomic_write", fail_on_keys)
    with pytest.raises(OSError):
        remote_pairing.apply_destination(
            _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
        )
    monkeypatch.setattr(remote_pairing, "atomic_write", real)

    document = json.loads(_paths(home)["config"].read_text())
    assert document["principals"][PRINCIPAL]["pubkey_sha256"]
    assert _bytes(_paths(home)["authorized_keys"]) == b""

    revoked = remote_pairing.revoke_destination(_revoke_request(home, gate), home=home)

    assert revoked["principal_removed"] is True
    assert revoked["key_lines_removed"] == 0
    assert PRINCIPAL not in json.loads(_paths(home)["config"].read_text())["principals"]


def test_revoke_refuses_a_grant_recorded_against_a_different_key(tmp_path, monkeypatch):
    """Fix 7: revoke only removes a half whose recorded key matches."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    real = remote_pairing.atomic_write

    def fail_on_keys(path, data, **kwargs):
        if path.name == "authorized_keys":
            raise OSError("simulated power loss before the key line went")
        return real(path, data, **kwargs)

    monkeypatch.setattr(remote_pairing, "atomic_write", fail_on_keys)
    with pytest.raises(OSError):
        remote_pairing.apply_destination(
            _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
        )
    monkeypatch.setattr(remote_pairing, "atomic_write", real)
    before = _bytes(_paths(home)["config"])

    with pytest.raises(remote_pairing.PairingError) as excinfo:
        remote_pairing.revoke_destination(
            _revoke_request(home, gate, pubkey=PUBKEY_TWO), home=home
        )

    assert "different key" in str(excinfo.value)
    assert _bytes(_paths(home)["config"]) == before


def test_a_definitive_refusal_is_marked_and_an_after_write_failure_is_not(
    tmp_path, monkeypatch
):
    """Fix 6: `refused` means \"provably nothing was written\"."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)

    with pytest.raises(remote_pairing.PairingError) as refused:
        remote_pairing.apply_destination(_apply_request("not-the-hash"), home=home, gate_path=gate)
    assert refused.value.refused is True

    approved = _inspect(home, gate=gate)["state_hash"]
    real = remote_pairing._dest_state
    calls = {"n": 0}

    def fail_after_the_writes(paths):
        calls["n"] += 1
        if calls["n"] > 1:  # the read-back that follows the two writes
            raise remote_pairing.PairingError("nothing is readable any more")
        return real(paths)

    monkeypatch.setattr(remote_pairing, "_dest_state", fail_after_the_writes)
    with pytest.raises(remote_pairing.PairingError) as after:
        remote_pairing.apply_destination(
            _apply_request(approved), home=home, gate_path=gate
        )
    assert after.value.refused is False


def test_apply_refuses_a_state_that_changed_since_the_user_approved(tmp_path):
    """Fix 8: the approved hash is the only one apply accepts, and says so."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    approved = _inspect(home, gate=gate)["state_hash"]
    _seed(home, keys="ssh-ed25519 AAAAsomebodyelse someone@elsewhere\n")

    with pytest.raises(remote_pairing.PairingStale) as excinfo:
        remote_pairing.apply_destination(
            _apply_request(approved), home=home, gate_path=gate
        )

    assert "changed since you approved" in str(excinfo.value)
    assert excinfo.value.refused is True and excinfo.value.stale is True
    assert not _paths(home)["config"].exists()


def test_inspect_reads_both_files_under_the_apply_lock(tmp_path, monkeypatch):
    """Fix 8: no torn read of a transaction someone else is committing."""
    import contextlib

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    entered: list[str] = []
    real = remote_pairing.pairing_lock

    @contextlib.contextmanager
    def watching(root, **kwargs):
        entered.append(str(root))
        with real(root, **kwargs):
            yield

    monkeypatch.setattr(remote_pairing, "pairing_lock", watching)

    _inspect(home, gate=gate)

    assert entered == [str(_paths(home)["remote_root"])]


def test_the_real_handler_reports_a_definitive_refusal_as_json(tmp_path):
    """Fix 6: through `bin/ccb-remote pairing apply`, not a substituted dict."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    _seed(
        home,
        config={"principals": {PRINCIPAL: {"issuer_token": TOKEN, "allowed_aliases": ["demo"]}}},
    )
    request = _apply_request(_inspect(home, gate=gate)["state_hash"], token="other9")

    completed = subprocess.run(
        [sys.executable, str(CCB_REMOTE), "pairing", "apply"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        env=dict(os.environ, HOME=str(home)),
    )

    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["refused"] is True
    assert "issuer token" in payload["error"]
    assert completed.returncode != 0
    assert _bytes(_paths(home)["authorized_keys"]) == b""


def test_the_real_handler_never_claims_a_refusal_after_a_bug(tmp_path, monkeypatch):
    """Fix 6: the generic-exception path carries no \"nothing was written\"."""
    import importlib.util
    import io

    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("ccb_remote_script", str(CCB_REMOTE))
    spec = importlib.util.spec_from_loader("ccb_remote_script", loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    import remote_pairing as pairing_module

    def explode(*_args, **_kwargs):
        raise RuntimeError("a bug, not a refusal")

    monkeypatch.setattr(pairing_module, "apply_destination", explode)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_apply_request("x"))))
    printed = io.StringIO()
    monkeypatch.setattr(sys, "stdout", printed)

    code = module._cmd_pairing(["apply"])

    payload = json.loads(printed.getvalue().strip().splitlines()[-1])
    assert code != 0
    assert payload["ok"] is False
    assert "refused" not in payload and "stale" not in payload


def test_removing_a_pin_for_another_port_leaves_the_default_port_line():
    """Fix 3: port 2222 never unpins what port 22 owns."""
    host = "server.example.test"
    existing = (
        f"{host} ssh-ed25519 {THIS_BLOB}\n"
        f"[{host}]:2222 ssh-ed25519 {OTHER_BLOB}\n"
    )

    merged, removed = remote_pairing.known_hosts_remove(existing, host, 2222)

    assert removed == 1
    assert f"{host} ssh-ed25519 {THIS_BLOB}" in merged
    assert f"[{host}]:2222" not in merged


def test_merging_a_pin_for_another_port_leaves_the_default_port_line():
    host = "server.example.test"
    existing = (
        f"{host} ssh-ed25519 {THIS_BLOB}\n"
        f"[{host}]:2222 ssh-ed25519 {OTHER_BLOB}\n"
    )

    merged = remote_pairing.known_hosts_merge(
        existing, host, 2222, [{"type": "ssh-ed25519", "key": THIS_BLOB}], rotate=True
    )

    lines = [line for line in merged.splitlines() if line.strip()]
    assert f"{host} ssh-ed25519 {THIS_BLOB}" in lines
    assert f"[{host}]:2222 ssh-ed25519 {THIS_BLOB}" in lines
    assert f"[{host}]:2222 ssh-ed25519 {OTHER_BLOB}" not in lines


# --------------------------------------------------------------------------- #
# Re-review fixes (task 20261111-190934, destination side)
# --------------------------------------------------------------------------- #

OTHER_GATE = "/opt/evil/bin/ccb-remote-gate"


def test_apply_never_rewrites_a_grant_recorded_for_another_key(tmp_path, monkeypatch):
    """Review2 item 2: the recorded ownership evidence is not overwritten."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    real = remote_pairing.atomic_write

    def fail_on_keys(path, data, **kwargs):
        if path.name == "authorized_keys":
            raise OSError("simulated power loss before the key line went")
        return real(path, data, **kwargs)

    # An interrupted apply: the grant (with its key fingerprint) exists, the
    # key line does not.
    monkeypatch.setattr(remote_pairing, "atomic_write", fail_on_keys)
    with pytest.raises(OSError):
        remote_pairing.apply_destination(
            _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
        )
    monkeypatch.setattr(remote_pairing, "atomic_write", real)
    before = _bytes(_paths(home)["config"])
    recorded = json.loads(before)["principals"][PRINCIPAL]["pubkey_sha256"]

    with pytest.raises(remote_pairing.PairingError) as excinfo:
        remote_pairing.apply_destination(
            _apply_request(
                _inspect(home, gate=gate)["state_hash"], pubkey=PUBKEY_TWO
            ),
            home=home,
            gate_path=gate,
        )

    assert "recorded against a different key" in str(excinfo.value)
    assert excinfo.value.refused is True
    assert _bytes(_paths(home)["config"]) == before
    assert json.loads(_paths(home)["config"].read_text())["principals"][PRINCIPAL][
        "pubkey_sha256"
    ] == recorded
    assert _bytes(_paths(home)["authorized_keys"]) == b""


def test_a_key_type_inside_a_quoted_option_is_not_the_key():
    """Review2 item 4: authorized_keys is read the way sshd reads it."""
    line = (
        f'command="echo ssh-ed25519 {OTHER_BLOB}",restrict '
        f"ssh-ed25519 {THIS_BLOB} ccb-remote main"
    )

    assert remote_pairing.key_line_blob(line) == ("ssh-ed25519", THIS_BLOB)
    # A plain line and a normal options line still parse.
    assert remote_pairing.key_line_blob(f"ssh-ed25519 {THIS_BLOB} comment") == (
        "ssh-ed25519",
        THIS_BLOB,
    )
    assert remote_pairing.key_line_blob(
        f'from="10.0.0.5",restrict,command="/x --principal p" ssh-ed25519 {THIS_BLOB}'
    ) == ("ssh-ed25519", THIS_BLOB)
    assert remote_pairing.key_line_blob("# ssh-ed25519 AAAAcomment") is None


def test_backslashes_in_options_are_read_exactly_as_sshd_reads_them():
    """Review3 item 1: OpenSSH skips a backslash ONLY directly before a quote.

    In `\\\\\\"` (two backslashes, then backslash-quote) the quote is escaped, so
    the quoted command does NOT end there. A parser that lets every backslash
    escape the next character ends the options early and takes command text
    for the key.
    """
    bs = "\\"
    line = (
        f'command="echo {bs}{bs}{bs}" ssh-ed25519 {OTHER_BLOB}",restrict '
        f"ssh-ed25519 {THIS_BLOB} ccb-remote main"
    )
    assert remote_pairing.key_line_blob(line) == ("ssh-ed25519", THIS_BLOB)
    # sshd skips backslash-quote even outside quotes; a lone backslash is text.
    assert remote_pairing.split_options_field(f'a{bs}"b c') == (f'a{bs}"b', "c")
    assert remote_pairing.split_options_field(f"x{bs}y z") == (f"x{bs}y", "z")


def test_apply_sees_the_real_blob_behind_a_quoted_option(tmp_path):
    """Review2 item 4: the spoken blob decides "already present"."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    blob = PUBKEY_ONE.split(" ")[1]
    _seed(
        home,
        keys=f'command="echo ssh-ed25519 {OTHER_BLOB}",restrict ssh-ed25519 {blob} c\n',
    )
    before = _bytes(_paths(home)["authorized_keys"])

    with pytest.raises(remote_pairing.PairingError) as excinfo:
        remote_pairing.apply_destination(
            _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
        )

    assert "already appears in another authorized_keys line" in str(excinfo.value)
    assert _bytes(_paths(home)["authorized_keys"]) == before


def test_a_same_shape_line_with_another_command_is_not_ours(tmp_path):
    """Review2 item 5: exact means B's own gate, not just the same shape."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    line = (
        f'restrict,command="{OTHER_GATE} --principal {PRINCIPAL}" {PUBKEY_ONE}'
    )
    _seed(home, keys=line + "\n")

    inspected = _inspect(
        home, gate=gate, lookup_principal=PRINCIPAL, lookup_pubkey=PUBKEY_ONE
    )

    assert inspected["key_blob_present"] is True
    assert inspected["key_line_foreign_blob"] is True
    assert inspected["existing_key_line"] is False
    assert inspected["key_line_exact_principals"] == []

    with pytest.raises(remote_pairing.PairingError) as excinfo:
        remote_pairing.revoke_destination(
            _revoke_request(home, gate), home=home, gate_path=gate
        )
    assert "did not write" in str(excinfo.value)
    assert _paths(home)["authorized_keys"].read_text() == line + "\n"


def test_revoke_refuses_a_foreign_line_even_when_the_grant_matches(tmp_path, monkeypatch):
    """Review2 item 6: the foreign-line refusal comes before the grant branch."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    remote_pairing.apply_destination(
        _apply_request(_inspect(home, gate=gate)["state_hash"]), home=home, gate_path=gate
    )
    # Somebody else also authorized this key, with their own options.
    foreign = f'from="10.0.0.5",restrict,command="{OTHER_GATE}" {PUBKEY_ONE}'
    with _paths(home)["authorized_keys"].open("a", encoding="utf-8") as handle:
        handle.write(foreign + "\n")
    config_before = _bytes(_paths(home)["config"])
    keys_before = _bytes(_paths(home)["authorized_keys"])

    with pytest.raises(remote_pairing.PairingError) as excinfo:
        remote_pairing.revoke_destination(
            _revoke_request(home, gate), home=home, gate_path=gate
        )

    assert "did not write" in str(excinfo.value)
    assert excinfo.value.refused is True
    assert _bytes(_paths(home)["config"]) == config_before
    assert _bytes(_paths(home)["authorized_keys"]) == keys_before


def test_the_real_revoke_handler_knows_this_machines_gate(tmp_path):
    """Review2 item 5: the handler passes B's gate, so shape is not enough."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    gate = _make_gate(tmp_path)
    line = f'restrict,command="{OTHER_GATE} --principal {PRINCIPAL}" {PUBKEY_ONE}'
    _seed(home, keys=line + "\n")
    request = _revoke_request(home, gate)

    completed = subprocess.run(
        [sys.executable, str(CCB_REMOTE), "pairing", "revoke"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        env=dict(os.environ, HOME=str(home)),
    )

    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False and payload["refused"] is True
    assert "did not write" in payload["error"]
    assert _paths(home)["authorized_keys"].read_text() == line + "\n"
