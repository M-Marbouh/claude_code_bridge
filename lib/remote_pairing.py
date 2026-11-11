"""Guided pairing between two machines (M3 W3): validation, B-side verbs, A-side helpers.

Two halves live here, both non-interactive so they can be tested directly:

* the DESTINATION half (`inspect_destination`, `apply_destination`,
  `revoke_destination`) -- what B's installed `ccb-remote pairing <verb>` runs
  over the user's NORMAL SSH login, reading one JSON request on stdin and
  writing one JSON result on stdout. It edits exactly two files, under a lock:
  ``~/.ssh/authorized_keys`` (one restricted line per pairing) and
  ``~/.ccb/remote/config.json`` (the principal grant and its aliases).
* the SOURCE-side helpers (`user_known_host_keys`, `known_hosts_merge`,
  `ssh_pairing_argv`, `run_pairing`, `fingerprint`) -- pure enough to test
  without a network, and shared by `pair` and `unpair`.

Rules that every function here obeys, because this writes `authorized_keys`:

* user values are DATA. They are validated by a closed pattern and inserted
  into a JSON document or a fixed line template; nothing is ever passed to a
  shell, and no value may contain a newline, NUL or quote-breaking character.
* every write is temp + fsync + rename + directory fsync, keeps 0700/0600, and
  is preceded (once) by a timestamped backup of the file it changes.
* unrelated keys, principals and aliases are preserved verbatim, and a
  conflicting existing mapping is REFUSED rather than overwritten.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

#: Providers a remote message may target (same set the protocol advertises).
REMOTE_PROVIDERS = ("claude", "codex")

#: The same shapes the two loaders enforce; pairing validates BEFORE writing so
#: nothing is ever written that its own loader would later refuse.
HOST_ALIAS_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
PRINCIPAL_RE = re.compile(r"[a-z0-9_-]{1,64}")
ISSUER_TOKEN_RE = re.compile(r"[a-z0-9]{4,32}")
SSH_TARGET_RE = re.compile(r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._-]*")

#: Public key types pairing will install. Deliberately a closed list: this
#: string ends up inside a restricted authorized_keys line.
PUBKEY_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)
_PUBKEY_BLOB_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_MAX_WORK_DIR = 4096
_MAX_PUBKEY = 8192


class PairingError(RuntimeError):
    """A refusal or expected failure; the caller decides how to report it.

    `refused` is set when the failure provably happened BEFORE any write, so
    the caller may promise "nothing was written" and discard its own half. A
    failure after the first write started is never flagged: it may have left
    state behind, and claiming otherwise would be a lie.

    `stale` marks the one refusal that is about the MACHINE, not about the
    request: the state changed under this approval. The approved scope is still
    valid, so the caller must keep its own half for the retry instead of
    throwing the pairing away.
    """

    refused = False
    stale = False


class PairingStale(PairingError):
    """The machine changed since the user approved; retrying resumes."""

    refused = True
    stale = True


# --------------------------------------------------------------------------- #
# Validation (values are data, never shell input)
# --------------------------------------------------------------------------- #


def _refuse_forbidden(value: object, field: str) -> str:
    text = value if isinstance(value, str) else ""
    if not text:
        raise PairingError(f"{field} must be a non-empty string")
    if len(text) > 8192:
        raise PairingError(f"{field} is too long")
    forbidden = [char for char in ("\x00", "\r", "\n") if char in text]
    if forbidden:
        raise PairingError(f"{field} may not contain control characters")
    return text


def validate_alias(value: object, field: str = "alias") -> str:
    text = _refuse_forbidden(value, field)
    if not HOST_ALIAS_RE.fullmatch(text):
        raise PairingError(
            f"{field} must be 1-32 characters: lowercase letters, digits and '-' (starting with a letter or digit)"
        )
    return text


def validate_principal(value: object, field: str = "principal") -> str:
    text = _refuse_forbidden(value, field)
    if not PRINCIPAL_RE.fullmatch(text):
        raise PairingError(
            f"{field} must be 1-64 characters: lowercase letters, digits, '-' and '_'"
        )
    return text


def validate_token(value: object, field: str = "issuer_token") -> str:
    text = _refuse_forbidden(value, field)
    if not ISSUER_TOKEN_RE.fullmatch(text):
        raise PairingError(f"{field} must be 4-32 lowercase letters or digits")
    return text


def validate_ssh_target(value: object) -> str:
    text = _refuse_forbidden(value, "ssh destination")
    if text.startswith("-") or not SSH_TARGET_RE.fullmatch(text):
        raise PairingError("ssh destination must be [user@]host")
    return text


def validate_port(value: object) -> int:
    if isinstance(value, bool):
        raise PairingError("port must be a number between 1 and 65535")
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise PairingError("port must be a number between 1 and 65535") from None
    if not 1 <= port <= 65535:
        raise PairingError("port must be a number between 1 and 65535")
    return port


def validate_work_dir(value: object, field: str = "work_dir") -> str:
    text = _refuse_forbidden(value, field)
    if len(text) > _MAX_WORK_DIR:
        raise PairingError(f"{field} is too long")
    if not (text.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", text)):
        raise PairingError(f"{field} must be an absolute path")
    if text != text.strip():
        raise PairingError(f"{field} may not start or end with whitespace")
    return text


def validate_providers(value: object, field: str = "providers") -> tuple[str, ...]:
    if isinstance(value, str):
        items: Iterable[object] = [item for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise PairingError(f"{field} must be a list of provider names")
    providers: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise PairingError(f"{field} must contain provider names")
        name = item.strip().lower()
        if name not in REMOTE_PROVIDERS:
            raise PairingError(
                f"{field} may only contain {', '.join(REMOTE_PROVIDERS)} (got {name or 'an empty value'})"
            )
        if name not in providers:
            providers.append(name)
    if not providers:
        raise PairingError(f"{field} must name at least one provider")
    # A stable order keeps a re-pairing a no-op regardless of flag order.
    return tuple(name for name in REMOTE_PROVIDERS if name in providers)


def validate_pubkey(value: object, field: str = "public key") -> str:
    """The exact public-key line to install, without a trailing newline."""
    text = _refuse_forbidden(value, field)
    text = text.strip()
    if len(text) > _MAX_PUBKEY:
        raise PairingError(f"{field} is too long")
    parts = text.split(" ", 2)
    if len(parts) < 2 or parts[0] not in PUBKEY_TYPES or not _PUBKEY_BLOB_RE.fullmatch(parts[1]):
        raise PairingError(f"{field} is not an SSH public key this build can install")
    return text


# --------------------------------------------------------------------------- #
# Small durable-write helpers
# --------------------------------------------------------------------------- #


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ensure_dir(path: Path, mode: int = 0o700) -> Path:
    os.makedirs(path, mode=mode, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_text_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise PairingError(f"{path} is unreadable: {exc}") from exc


def file_mode(path: Path, default: int) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return default


def atomic_write(path: Path, data: bytes, *, default_mode: int = 0o600) -> None:
    """Same-directory temp file, fsync, rename, directory fsync; keeps the mode."""
    directory = _ensure_dir(path.parent)
    mode = file_mode(path, default_mode)
    handle = tempfile.NamedTemporaryFile(
        dir=str(directory), prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def backup_once(path: Path) -> Path | None:
    """A timestamped copy of a file about to change; None when it does not exist."""
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.bak-{utc_stamp()}")
    index = 1
    while target.exists():
        index += 1
        target = path.with_name(f"{path.name}.bak-{utc_stamp()}-{index}")
    data = path.read_bytes()
    atomic_write(target, data, default_mode=file_mode(path, 0o600))
    return target


@contextmanager
def pairing_lock(remote_root: Path, *, timeout_s: float = 30.0):
    """Serialise pairing writes on this machine (one lock file, flock)."""
    import fcntl

    _ensure_dir(remote_root)
    path = remote_root / "pairing.lock"
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise PairingError(
                        "another pairing is running on this machine; finish it and retry"
                    ) from None
                time.sleep(0.1)
        yield path
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# State hash: what `apply` expects to still be true
# --------------------------------------------------------------------------- #


def state_hash(authorized_keys: bytes, config: bytes) -> str:
    """A digest of BOTH files, distinguishing "absent" from "empty"."""
    digest = hashlib.sha256()
    for label, data in ((b"authorized_keys", authorized_keys), (b"config.json", config)):
        digest.update(label)
        digest.update(b"\0present\0" if data else b"\0absent\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# The restricted key line
# --------------------------------------------------------------------------- #


def ccb_key_line(gate: str, principal: str, pubkey: str) -> str:
    """The one authorized_keys line a pairing installs (no trailing newline)."""
    gate_abs = validate_work_dir(gate, "gate path")
    validate_principal(principal)
    key = validate_pubkey(pubkey)
    if '"' in gate_abs or '"' in key:
        # A quote would end the options= string; refuse instead of escaping,
        # because an escaped path here changes what ssh runs.
        raise PairingError("the gate path or key may not contain a double quote")
    if " " in gate_abs or "\t" in gate_abs:
        raise PairingError("the gate path may not contain whitespace")
    return f'restrict,command="{gate_abs} --principal {principal}" {key}'


def split_pubkey(value: str) -> tuple[str, str] | None:
    parts = str(value or "").strip().split(" ", 2)
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def pubkey_sha256(key_blob: str) -> str:
    """Ownership evidence: a stable SHA-256 name for a key blob's text.

    Deliberately not `fingerprint()`: that decodes the blob as base64 and adds
    a failure mode on a value `validate_pubkey` already accepted, while this
    only has to answer "is this the same key text?"
    """
    return hashlib.sha256(str(key_blob or "").encode("utf-8")).hexdigest()


def parse_ccb_key_line(line: str) -> dict | None:
    """The pairing a restricted CCB line names, or None for any other line."""
    match = re.match(r'^restrict,command="([^"]*)" (\S+ .*)$', line.strip())
    if not match:
        return None
    command, key = match.group(1), match.group(2)
    principal_match = re.search(r"--principal (\S+)$", command.strip())
    gate = command.strip()[: principal_match.start()].strip() if principal_match else command.strip()
    blob = split_pubkey(key)
    if principal_match is None or blob is None:
        return None
    return {
        "principal": principal_match.group(1),
        "gate": gate,
        "key_type": blob[0],
        "pubkey": blob[1],
        "line": line.strip(),
    }


def split_options_field(line: str) -> tuple[str, str]:
    """Split a line into (first field, rest) the way sshd reads options.

    Options are one comma-separated field that may contain double-quoted
    strings, and a space inside a quoted string is part of the value rather
    than a separator. That matters because `command="echo ssh-ed25519 AAAA..."`
    looks like a key line to a naive split but is only an option.

    Mirrors OpenSSH's `sshkey_advance_past_options` exactly: a backslash is
    special ONLY directly before a double quote (both are skipped, quoted or
    not); any other backslash is an ordinary character.
    """
    text = line.strip()
    quoted = False
    skip_next = False
    for index, char in enumerate(text):
        if skip_next:
            skip_next = False
            continue
        if char == "\\" and text[index + 1:index + 2] == '"':
            skip_next = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if char in " \t" and not quoted:
            return text[:index], text[index + 1:]
    return text, ""


def key_line_blob(line: str) -> tuple[str, str] | None:
    """(type, blob) of ANY authorized_keys line, whatever options it carries.

    `parse_ccb_key_line` only recognises the exact shape pairing writes; this
    finds the key in a line someone else wrote (`from="...",restrict,...`),
    which is what "this key is already present" has to mean. Read the way sshd
    does: an optional options field comes FIRST, so a key-type word inside a
    quoted option is never the key.
    """
    first, rest = split_options_field(line)
    if not first:
        return None
    if first in PUBKEY_TYPES:
        fields = rest.split()
        return (first, fields[0]) if fields else None
    if first.startswith("#"):
        return None
    fields = rest.split()
    if len(fields) >= 2 and fields[0] in PUBKEY_TYPES:
        return fields[0], fields[1]
    return None


def canonical_ccb_line(parsed: dict) -> str:
    """Everything pairing writes before the key blob's optional comment."""
    return (
        f'restrict,command="{parsed["gate"]} --principal {parsed["principal"]}" '
        f'{parsed["key_type"]} {parsed["pubkey"]}'
    )


def is_exact_ccb_line(line: str, gate: str = "") -> bool:
    """Is this line exactly what pairing writes NOW for these values?

    `gate` is this machine's own gate path -- the one `apply` would write. A
    line of the same shape that runs a DIFFERENT command (another gate path,
    another program) is somebody else's and must never be reported as
    installed or removed as ours. The key blob may be followed by a comment
    (ssh allows nothing else), so that is the only tolerated suffix.
    """
    parsed = parse_ccb_key_line(line)
    if parsed is None:
        return False
    if gate:
        # The line has to be what pairing would write with THIS machine's gate,
        # so the prefix is rebuilt from it: a same-shaped line that runs another
        # command never matches.
        prefix = (
            f'restrict,command="{gate} --principal {parsed["principal"]}" '
            f'{parsed["key_type"]} {parsed["pubkey"]}'
        )
    else:
        prefix = canonical_ccb_line(parsed)
    text = line.strip()
    if not text.startswith(prefix):
        return False
    rest = text[len(prefix):]
    return rest == "" or (rest[0] in " \t" and bool(rest.strip()))


# --------------------------------------------------------------------------- #
# Destination half: files, inspection, apply, revoke
# --------------------------------------------------------------------------- #


def destination_paths(home: Path | None = None) -> dict:
    home = Path(home) if home is not None else Path.home()
    remote_root = home / ".ccb" / "remote"
    return {
        "home": home,
        "ssh_dir": home / ".ssh",
        "authorized_keys": home / ".ssh" / "authorized_keys",
        "remote_root": remote_root,
        "config": remote_root / "config.json",
    }


def _dest_state(paths: dict) -> tuple[bytes, bytes, str]:
    try:
        keys = paths["authorized_keys"].read_bytes()
    except FileNotFoundError:
        keys = b""
    except OSError as exc:
        raise PairingError(f"authorized_keys is unreadable: {exc}") from exc
    try:
        config = paths["config"].read_bytes()
    except FileNotFoundError:
        config = b""
    except OSError as exc:
        raise PairingError(f"config.json is unreadable: {exc}") from exc
    return keys, config, state_hash(keys, config)


def _load_config_document(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PairingError(f"config.json is not valid JSON: {exc}") from None
    if not isinstance(document, dict):
        raise PairingError("config.json must be a JSON object")
    # Validate it exactly as the delivery path will, so pairing never writes a
    # file its own loader refuses.
    from remote_journal import RemoteConfigError, load_config

    path = None
    try:
        with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as handle:
            handle.write(raw)
            path = Path(handle.name)
        load_config(path)
    except RemoteConfigError as exc:
        raise PairingError(f"config.json is not usable: {exc}") from None
    finally:
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass
    return document


def host_public_keys(host_key_dir: Path | str = "/etc/ssh") -> list[dict]:
    """B's own SSH host public keys, as `{type, key}` pairs."""
    keys: list[dict] = []
    try:
        candidates = sorted(Path(host_key_dir).glob("ssh_host_*_key.pub"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            parts = path.read_text(encoding="utf-8").strip().split(" ", 2)
        except OSError:
            continue
        if len(parts) >= 2 and parts[0] in PUBKEY_TYPES and _PUBKEY_BLOB_RE.fullmatch(parts[1]):
            if not any(item["key"] == parts[1] for item in keys):
                keys.append({"type": parts[0], "key": parts[1]})
    return keys


def known_projects(registry_dir: Path | None = None) -> list[dict]:
    """CCB projects this machine knows: registry work_dirs and their providers."""
    directory = Path(registry_dir) if registry_dir is not None else Path.home() / ".ccb" / "run"
    projects: dict[str, dict] = {}
    try:
        paths = sorted(directory.glob("ccb-session-*.json"))
    except OSError:
        paths = []
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        work_dir = str(record.get("work_dir") or "").strip()
        if not work_dir:
            continue
        providers = record.get("providers")
        names = [
            name
            for name in (providers.keys() if isinstance(providers, dict) else [])
            if name in REMOTE_PROVIDERS
        ]
        entry = projects.setdefault(
            work_dir,
            {"work_dir": work_dir, "name": Path(work_dir).name, "providers": []},
        )
        for name in names:
            if name not in entry["providers"]:
                entry["providers"].append(name)
    return sorted(projects.values(), key=lambda item: item["work_dir"])


def inspect_destination(
    *,
    home: Path | None = None,
    gate_path: Path | None = None,
    host_key_dir: Path | str = "/etc/ssh",
    registry_dir: Path | None = None,
    lookup_principal: str = "",
    lookup_pubkey: str = "",
) -> dict:
    """Read-only description of what a pairing would find on this machine.

    The two files are read under the SAME lock `apply` takes (B7), so the hash
    handed back really is one consistent pair of files rather than a torn read
    of a transaction someone else is committing.
    """
    paths = destination_paths(home)
    with pairing_lock(paths["remote_root"]):
        keys, config, digest = _dest_state(paths)
        document = _load_config_document(config)
    principals = document.get("principals") if isinstance(document.get("principals"), dict) else {}
    aliases = document.get("aliases") if isinstance(document.get("aliases"), dict) else {}

    gate = str(gate_path) if gate_path is not None else ""
    gate_present = bool(gate) and Path(gate).is_file() and os.access(gate, os.X_OK)

    lines = [line for line in keys.decode("utf-8", "replace").splitlines() if line.strip()]
    ccb_lines = [parsed for parsed in (parse_ccb_key_line(line) for line in lines) if parsed]
    lookup_blob = split_pubkey(lookup_pubkey) if lookup_pubkey else None
    result = {
        "ok": True,
        "gate": gate,
        "gate_present": gate_present,
        "host_keys": host_public_keys(host_key_dir),
        # Never another principal's token: only what this side is allowed to say.
        "principals": {
            name: {
                "allowed_aliases": list(
                    (entry.get("allowed_aliases") if isinstance(entry, dict) else []) or []
                )
            }
            for name, entry in principals.items()
            if isinstance(name, str)
        },
        "aliases": {
            name: {
                "work_dir": str((entry or {}).get("work_dir") or "")
                if isinstance(entry, dict)
                else "",
                "providers": list((entry or {}).get("providers") or [])
                if isinstance(entry, dict)
                else [],
            }
            for name, entry in aliases.items()
            if isinstance(name, str)
        },
        "projects": known_projects(registry_dir),
        "state_hash": digest,
        "existing_key_line": False,
        "key_blob_present": False,
        "key_line_foreign_blob": False,
        "key_line_exact_principals": [],
        "bound_principals": [],
        "key_line_principals": sorted({item["principal"] for item in ccb_lines}),
    }
    own_gate = str(Path(gate).resolve()) if gate else ""
    if lookup_principal:
        wanted = ""
        if lookup_pubkey and own_gate:
            try:
                wanted = ccb_key_line(own_gate, str(lookup_principal), lookup_pubkey)
            except PairingError:
                wanted = ""
        # Only the EXACT line counts as "already installed": a line carrying
        # this key with different options is a conflict, not an install.
        result["existing_key_line"] = bool(wanted) and any(
            line.strip() == wanted for line in lines
        )
    if lookup_blob is not None:
        result["key_blob_present"] = any(key_line_blob(line) == lookup_blob for line in lines)
        result["bound_principals"] = sorted(
            {
                item["principal"]
                for item in ccb_lines
                if (item["key_type"], item["pubkey"]) == lookup_blob
            }
        )
        # A line that carries this key but is not the shape pairing writes is
        # a conflict wherever it sits -- even next to a correct line.
        result["key_line_foreign_blob"] = any(
            key_line_blob(line) == lookup_blob
            and not is_exact_ccb_line(line, gate=own_gate)
            for line in lines
        )
        # Which principals already hold a line that is byte-for-byte the one
        # pairing writes for this key (the only shape that is not a conflict).
        result["key_line_exact_principals"] = sorted(
            {
                item["principal"]
                for item in ccb_lines
                if (item["key_type"], item["pubkey"]) == lookup_blob
                and is_exact_ccb_line(item["line"], gate=own_gate)
            }
        )
    return result


def _authorized_keys_without_newline_dup(text: str, line: str) -> str:
    lines = text.splitlines()
    if any(existing.strip() == line for existing in lines):
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


def _requested_aliases(request: dict) -> list[dict]:
    raw = request.get("aliases")
    if not isinstance(raw, list) or not raw:
        raise PairingError("apply needs at least one alias to share")
    aliases: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise PairingError("each alias must be an object")
        aliases.append(
            {
                "alias": validate_alias(entry.get("alias"), "project alias"),
                "work_dir": validate_work_dir(entry.get("work_dir")),
                "providers": list(validate_providers(entry.get("providers"))),
            }
        )
    if len({entry["alias"] for entry in aliases}) != len(aliases):
        raise PairingError("an alias was requested twice")
    return aliases


def apply_destination(
    request: dict,
    *,
    home: Path | None = None,
    gate_path: Path | None = None,
) -> dict:
    """Install this pairing's key line and grants, or refuse with nothing written."""
    state = {"written": False}
    try:
        return _apply_destination(request, home=home, gate_path=gate_path, state=state)
    except PairingError as exc:
        # Only a failure BEFORE the first write may be reported as "nothing was
        # written"; anything after it is an unfinished transaction.
        if not state["written"]:
            exc.refused = True
        raise


def _apply_destination(
    request: dict,
    *,
    home: Path | None,
    gate_path: Path | None,
    state: dict,
) -> dict:
    if not isinstance(request, dict):
        raise PairingError("apply needs a JSON object")
    principal = validate_principal(request.get("principal"))
    pubkey = validate_pubkey(request.get("pubkey"))
    token = validate_token(request.get("issuer_token"))
    aliases = _requested_aliases(request)
    expected = str(request.get("expected_state_hash") or "").strip()
    if not expected:
        raise PairingError("apply needs the state hash it was inspected with; nothing was written")

    paths = destination_paths(home)
    gate = Path(gate_path) if gate_path is not None else None
    if gate is None or not gate.is_file() or not os.access(gate, os.X_OK):
        raise PairingError("the CCB gate was not found next to ccb-remote on this machine")
    line = ccb_key_line(str(gate.resolve()), principal, pubkey)

    with pairing_lock(paths["remote_root"]):
        keys_bytes, config_bytes, digest = _dest_state(paths)
        if expected and expected != digest:
            raise PairingStale(
                "the other machine changed since you approved; re-run pair "
                "(nothing was written)"
            )
        document = _load_config_document(config_bytes)
        principals = document.setdefault("principals", {})
        if not isinstance(principals, dict):
            raise PairingError("config.json principals must be an object")
        aliases_document = document.setdefault("aliases", {})
        if not isinstance(aliases_document, dict):
            raise PairingError("config.json aliases must be an object")

        keys_text = keys_bytes.decode("utf-8", "replace")
        parsed_lines = [
            parsed
            for parsed in (
                parse_ccb_key_line(line_text)
                for line_text in keys_text.splitlines()
                if line_text.strip()
            )
            if parsed
        ]
        blob = split_pubkey(pubkey)
        assert blob is not None

        # ANY line carrying this key is a conflict unless it is byte-for-byte
        # the line pairing writes: appending a second line for one key would
        # make the key addressable under two option sets at once.
        for line_text in keys_text.splitlines():
            if not line_text.strip() or key_line_blob(line_text) != blob:
                continue
            if line_text.strip() != line:
                raise PairingError(
                    "this key already appears in another authorized_keys line on that machine "
                    "(its options or text differ from what pairing writes); remove that line there "
                    "first, then pair again -- nothing was written"
                )

        for item in parsed_lines:
            if item["principal"] == principal and (item["key_type"], item["pubkey"]) != blob:
                raise PairingError(
                    f"principle {principal} is already bound to a different key on this machine"
                )
            if (item["key_type"], item["pubkey"]) == blob and item["principal"] != principal:
                raise PairingError(
                    f"this key is already bound to another principal ({item['principal']})"
                )
        existing_principal = principals.get(principal)
        if isinstance(existing_principal, dict):
            existing_token = str(existing_principal.get("issuer_token") or "")
            if existing_token and existing_token != token:
                raise PairingError(
                    f"principal {principal} already exists with a different issuer token"
                )
            recorded_sha = str(existing_principal.get("pubkey_sha256") or "")
            if recorded_sha and recorded_sha != pubkey_sha256(blob[1]):
                # The grant records WHICH key it was made for. A request naming
                # a different key is a different pairing, and re-applying must
                # never rewrite that record to match itself.
                raise PairingError(
                    f"principal {principal} on that machine is already recorded against a "
                    "different key; nothing was written"
                )
        for entry in aliases:
            existing_alias = aliases_document.get(entry["alias"])
            if not isinstance(existing_alias, dict):
                continue
            existing_work_dir = str(existing_alias.get("work_dir") or "")
            if existing_work_dir != entry["work_dir"]:
                raise PairingError(
                    f"alias {entry['alias']} already points at a different directory on this machine"
                )
            existing_providers = [str(item) for item in existing_alias.get("providers") or []]
            if existing_providers and set(existing_providers) != set(entry["providers"]):
                # Order is not part of the grant: the SET is what a message may
                # address, so only a real difference is a conflict.
                raise PairingError(
                    f"alias {entry['alias']} already exists with different providers on this machine"
                )

        new_keys_text = _authorized_keys_without_newline_dup(keys_text, line)
        keys_changed = new_keys_text != keys_text

        new_principals = dict(principals)
        requested_alias_names = [entry["alias"] for entry in aliases]
        merged = dict(existing_principal) if isinstance(existing_principal, dict) else {}
        merged_current = [str(item) for item in merged.get("allowed_aliases") or []]
        merged["issuer_token"] = token
        merged["allowed_aliases"] = merged_current + [
            name for name in requested_alias_names if name not in merged_current
        ]
        # Ownership evidence (B5): the grant records WHICH key it was made for,
        # so a revoke can prove the half it is removing is its own.
        merged["pubkey_sha256"] = pubkey_sha256(blob[1])
        new_principals[principal] = merged

        new_aliases = dict(aliases_document)
        aliases_added = []
        for entry in aliases:
            if entry["alias"] not in new_aliases:
                aliases_added.append(entry["alias"])
            current = new_aliases.get(entry["alias"])
            base = dict(current) if isinstance(current, dict) else {}
            base["work_dir"] = entry["work_dir"]
            existing_list = [str(item) for item in base.get("providers") or []]
            # Keep an existing equivalent list verbatim, so a re-apply does not
            # rewrite the file just because the flags were ordered differently.
            base["providers"] = (
                existing_list if existing_list and set(existing_list) == set(entry["providers"]) else entry["providers"]
            )
            new_aliases[entry["alias"]] = base

        new_document = dict(document)
        new_document["principals"] = new_principals
        new_document["aliases"] = new_aliases
        new_config_bytes = (
            json.dumps(new_document, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        config_changed = new_config_bytes != config_bytes

        # Two files, one intent: the GRANT is committed first, the key line
        # second. An interruption between the two renames therefore leaves a
        # principal that cannot authenticate yet (fail closed, and a retry
        # completes it) rather than a key with no grant (which would silently
        # authorize a principal the operator never approved).
        backups: list[str] = []
        state["written"] = True
        if config_changed:
            backup = backup_once(paths["config"])
            if backup is not None:
                backups.append(str(backup))
            atomic_write(paths["config"], new_config_bytes, default_mode=0o600)
        if keys_changed:
            backup = backup_once(paths["authorized_keys"])
            if backup is not None:
                backups.append(str(backup))
            atomic_write(
                paths["authorized_keys"], new_keys_text.encode("utf-8"), default_mode=0o600
            )

        keys_after, config_after, digest_after = _dest_state(paths)
        return {
            "ok": True,
            "principal": principal,
            "key_line_added": keys_changed,
            "config_changed": config_changed,
            "principals_added": [] if isinstance(existing_principal, dict) else [principal],
            "aliases_added": aliases_added,
            "backups": backups,
            "state_hash": digest_after,
        }


def revoke_destination(
    request: dict, *, home: Path | None = None, gate_path: Path | None = None
) -> dict:
    """Remove exactly this pairing's key line and principal grant.

    A shared alias is deliberately kept: it may serve another principal, and
    deleting it would revoke more than this pairing installed. `gate_path` is
    this machine's own gate (the one `apply` writes); without it a same-shaped
    line cannot be told apart from ours, so the caller always passes it.
    """
    state = {"written": False}
    try:
        return _revoke_destination(
            request, home=home, gate_path=gate_path, state=state
        )
    except PairingError as exc:
        if not state["written"]:
            exc.refused = True
        raise


def _revoke_destination(
    request: dict, *, home: Path | None, gate_path: Path | None, state: dict
) -> dict:
    if not isinstance(request, dict):
        raise PairingError("revoke needs a JSON object")
    principal = validate_principal(request.get("principal"))
    pubkey = validate_pubkey(request.get("pubkey"))
    token = validate_token(request.get("issuer_token"))
    expected = str(request.get("expected_state_hash") or "").strip()
    if not expected:
        raise PairingError("revoke needs the state hash it was inspected with; nothing was removed")
    blob = split_pubkey(pubkey)
    assert blob is not None

    paths = destination_paths(home)
    with pairing_lock(paths["remote_root"]):
        keys_bytes, config_bytes, digest = _dest_state(paths)
        if expected != digest:
            raise PairingStale(
                "this machine's ssh/config state changed since it was inspected; inspect again "
                "before revoking anything"
            )
        keys_text = keys_bytes.decode("utf-8", "replace")
        lines = keys_text.splitlines()

        # Which lines are ours to remove, and which merely carry our key:
        #   exact      -- byte-for-byte the line pairing writes for this
        #                 principal and this key (the only line revoke removes)
        #   foreign    -- carries this key with other options/text (never ours)
        #   other_key  -- a CCB line for this principal with a different key
        own_gate = str(Path(gate_path).resolve()) if gate_path is not None else ""
        exact: list[int] = []
        foreign: list[int] = []
        other_key_for_principal = False
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            parsed = parse_ccb_key_line(line)
            if parsed is not None and parsed["principal"] == principal:
                if (parsed["key_type"], parsed["pubkey"]) != blob:
                    other_key_for_principal = True
                elif is_exact_ccb_line(line, gate=own_gate):
                    exact.append(index)
                else:
                    foreign.append(index)
            elif key_line_blob(line) == blob:
                foreign.append(index)

        # A line carrying this key that pairing would not write NOW (other
        # options, another command) is not ours: refuse before anything is
        # removed, even when a matching grant also exists.
        if foreign:
            raise PairingError(
                "this key already appears in an authorized_keys line on that machine that pairing "
                "did not write (its options, command or text differ); inspect it by hand -- "
                "nothing was removed"
            )

        document = _load_config_document(config_bytes)
        principals = document.get("principals")
        existing_principal = (
            principals.get(principal) if isinstance(principals, dict) else None
        )
        if isinstance(existing_principal, dict):
            stored_token = str(existing_principal.get("issuer_token") or "")
            if stored_token != token:
                # Ownership: this name now belongs to a different pairing. Its
                # grant must never be removed by a stale unpair.
                raise PairingError(
                    f"principal {principal} on that machine belongs to a different pairing "
                    "(issuer token mismatch); nothing was removed"
                )
            stored_sha = str(existing_principal.get("pubkey_sha256") or "")
            if stored_sha and stored_sha != pubkey_sha256(blob[1]):
                raise PairingError(
                    f"principal {principal} on that machine is recorded against a different "
                    "key; nothing was removed"
                )
            if not stored_sha and not exact:
                # A bare grant whose key we cannot prove (an interrupted apply
                # from a build that recorded no fingerprint) is not removable
                # on evidence that does not exist.
                raise PairingError(
                    f"principal {principal} has no key line and no recorded key fingerprint on "
                    "that machine, so ownership cannot be proved; inspect it by hand before "
                    "removing anything"
                )
        elif exact:
            # An interrupted REVOKE left exactly this: the grant is gone and
            # our key line remains. B holds no token once the grant is removed,
            # so the evidence is the line itself: only the holder of this key
            # can ask for it by its blob, and only for this principal.
            pass
        elif other_key_for_principal:
            # Handled below by the same message as before: the principal is
            # bound to a key that is not the one this request names.
            pass

        if not exact and not isinstance(existing_principal, dict) and other_key_for_principal:
            raise PairingError(
                f"principal {principal} is bound to a different key on this machine; nothing was removed"
            )

        matching = exact

        kept = [line for index, line in enumerate(lines) if index not in set(matching)]
        new_keys_text = "\n".join(kept) + ("\n" if kept else "")

        principal_removed = False
        if isinstance(principals, dict) and principal in principals:
            principals = dict(principals)
            principals.pop(principal)
            document["principals"] = principals
            principal_removed = True
        aliases = document.get("aliases")
        aliases_kept = sorted(aliases) if isinstance(aliases, dict) else []
        new_config_bytes = (
            json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")

        # Mirror of apply: the GRANT goes first, the key line second. An
        # interruption leaves a key line with no grant (denied, and a retry
        # finishes) rather than a grant that outlives the revocation.
        backups: list[str] = []
        state["written"] = True
        if principal_removed:
            backup = backup_once(paths["config"])
            if backup is not None:
                backups.append(str(backup))
            atomic_write(paths["config"], new_config_bytes, default_mode=0o600)
        if matching:
            backup = backup_once(paths["authorized_keys"])
            if backup is not None:
                backups.append(str(backup))
            atomic_write(
                paths["authorized_keys"], new_keys_text.encode("utf-8"), default_mode=0o600
            )

        _keys_after, _config_after, digest_after = _dest_state(paths)
        return {
            "ok": True,
            "principal": principal,
            "key_lines_removed": len(matching),
            "principal_removed": principal_removed,
            "aliases_kept": aliases_kept,
            "backups": backups,
            "state_hash": digest_after,
        }


# --------------------------------------------------------------------------- #
# Source half: known_hosts parsing/merging, pin, fingerprints
# --------------------------------------------------------------------------- #


def endpoint_names(host: str, port: int) -> list[str]:
    """The names `ssh` would look up, in its own order of preference."""
    return [host] if int(port) == 22 else [f"[{host}]:{port}", host]


def pin_names(host: str, port: int) -> list[str]:
    """The ONLY names a pin write or removal may touch for this endpoint.

    ssh looks `[host]:port` up and (in its own order) may also consider a bare
    `host` line, but a bare `host` line describes port 22: touching it while
    unpairing port 2222 would unpin a different connection.
    """
    return [host] if int(port) == 22 else [f"[{host}]:{port}"]


def _hashed_match(pattern: str, name: str) -> bool:
    """Does a `|1|salt|hash` known_hosts pattern name this host? (HMAC-SHA1)"""
    parts = pattern.split("|")
    if len(parts) != 4 or parts[1] != "1":
        return False
    try:
        salt = base64.b64decode(parts[2], validate=True)
        expected = base64.b64decode(parts[3], validate=True)
    except (ValueError, TypeError):
        return False
    digest = hmac.new(salt, name.encode("utf-8"), hashlib.sha1).digest()
    return hmac.compare_digest(digest, expected)


def known_hosts_entries(
    text: str, host: str, port: int, *, names: list[str] | None = None
) -> list[dict]:
    """Every pinned key line that names this endpoint, hashed or plain."""
    wanted = list(names) if names is not None else endpoint_names(host, port)
    found: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 3:
            continue
        patterns = fields[0].split(",")
        matched = any(
            pattern in wanted
            or (pattern.startswith("|") and any(_hashed_match(pattern, name) for name in wanted))
            for pattern in patterns
        )
        if not matched:
            continue
        if fields[1] not in PUBKEY_TYPES:
            continue
        found.append({"type": fields[1], "key": fields[2], "line": line})
    return found


def user_known_hosts_paths(home: Path | None = None) -> list[Path]:
    home = Path(home) if home is not None else Path.home()
    return [home / ".ssh" / "known_hosts", home / ".ssh" / "known_hosts2"]


def user_known_host_keys(
    host: str, port: int, *, home: Path | None = None, path: Path | None = None
) -> list[dict]:
    """Verified key entries for this endpoint.

    `path` is an explicit known_hosts file (the user's, if it is not the one
    their ssh reads by default); without it, the usual ssh locations are read.
    """
    paths = [Path(path)] if path is not None else user_known_hosts_paths(home)
    found: list[dict] = []
    for candidate in paths:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.extend(known_hosts_entries(text, host, port))
    return found


def known_hosts_line(host: str, port: int, key_type: str, key: str) -> str:
    name = f"[{host}]:{port}" if int(port) != 22 else host
    return f"{name} {key_type} {key}"


def _prune_endpoint(patterns: list[str], host: str, port: int) -> tuple[list[str], bool]:
    """Split one line's pattern list into (other names, named this endpoint?)."""
    names = set(pin_names(host, port))
    kept: list[str] = []
    removed = False
    for pattern in patterns:
        if pattern in names or (
            pattern.startswith("|") and any(_hashed_match(pattern, name) for name in names)
        ):
            removed = True
        else:
            kept.append(pattern)
    return kept, removed


def pin_rotation_problem(existing: str, host: str, port: int, keys: list[dict]) -> str:
    """Why an existing pin for this endpoint would have to be rotated, or "".

    A pin that is not the key we just verified means one of two things: the
    machine was re-keyed (rotate deliberately), or something is wrong (stop).
    Pairing never overwrites it silently.
    """
    verified = {(item["type"], item["key"]) for item in keys}
    stale = [
        entry
        for entry in known_hosts_entries(
            existing, host, port, names=pin_names(host, port)
        )
        if (entry["type"], entry["key"]) not in verified
    ]
    if not stale:
        return ""
    return (
        f"a different host key is already pinned for this machine ({fingerprint(stale[0]['key'])}). "
        "If that machine was deliberately re-keyed, re-run with --rotate-pin after verifying the new "
        "fingerprint out of band; otherwise stop and check the machine. Nothing was written."
    )


def known_hosts_merge(
    existing: str,
    host: str,
    port: int,
    keys: list[dict],
    *,
    rotate: bool = False,
) -> str:
    """Add/replace ONLY this endpoint's names; every other pin is preserved.

    A comma-separated line that also names other hosts keeps those names; only
    this endpoint's name is removed from it. An existing pin for this endpoint
    that is NOT one of the verified keys requires `rotate=True`.
    """
    kept: list[str] = []
    stale: list[dict] = []
    for raw in existing.splitlines():
        line = raw.strip()
        if not line:
            continue
        fields = line.split()
        patterns = fields[0].split(",") if fields else []
        remaining, removed = _prune_endpoint(patterns, host, port)
        if not removed:
            kept.append(line)
            continue
        if len(fields) >= 3:
            stale.append({"type": fields[1], "key": fields[2]})
        if remaining:
            # Other names shared this line: keep them, drop only ours.
            kept.append(",".join(remaining) + " " + " ".join(fields[1:]))
    if stale and not rotate:
        verified = {(item["type"], item["key"]) for item in keys}
        if any((entry["type"], entry["key"]) not in verified for entry in stale):
            raise PairingError(pin_rotation_problem(existing, host, port, keys))
    for item in keys:
        kept.append(known_hosts_line(host, port, item["type"], item["key"]))
    return "\n".join(kept) + ("\n" if kept else "")


def known_hosts_remove(existing: str, host: str, port: int) -> tuple[str, int]:
    """Remove this endpoint's names; return the new text and how many lines went.

    A line shared with other names is rewritten without ours instead of being
    dropped, so unpairing here never unpins an unrelated machine.
    """
    kept: list[str] = []
    removed = 0
    for raw in existing.splitlines():
        line = raw.strip()
        if not line:
            continue
        fields = line.split()
        patterns = fields[0].split(",") if fields else []
        remaining, matched = _prune_endpoint(patterns, host, port)
        if not matched:
            kept.append(line)
            continue
        removed += 1
        if remaining and len(fields) >= 2:
            kept.append(",".join(remaining) + " " + " ".join(fields[1:]))
    return "\n".join(kept) + ("\n" if kept else ""), removed


def fingerprint(key_blob: str) -> str:
    """`SHA256:…` for a base64 SSH key blob (the form `ssh-keygen -lf` prints)."""
    try:
        raw = base64.b64decode(key_blob, validate=True)
    except (ValueError, TypeError):
        raise PairingError("that host key is not valid base64") from None
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest}"


def normalize_fingerprint(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PairingError("the fingerprint is empty")
    return text if text.startswith("SHA256:") else f"SHA256:{text}"


def matching_host_keys(
    presented: list[dict], accepted: list[dict]
) -> list[dict]:
    """The presented keys the user's own ssh (or a fingerprint) vouched for."""
    accepted_set = {(item["type"], item["key"]) for item in accepted}
    return [item for item in presented if (item["type"], item["key"]) in accepted_set]


# --------------------------------------------------------------------------- #
# Source half: the pairing SSH channel
# --------------------------------------------------------------------------- #

PAIRING_SSH_ENV = "CCB_PAIRING_SSH_BIN"
PAIRING_TIMEOUT_ENV = "CCB_PAIRING_TIMEOUT_S"
DEFAULT_PAIRING_TIMEOUT_S = 60.0


def pairing_timeout_s() -> float:
    raw = (os.environ.get(PAIRING_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_PAIRING_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_PAIRING_TIMEOUT_S
    return value if value > 0 else DEFAULT_PAIRING_TIMEOUT_S


def pairing_ssh_bin() -> str:
    return (os.environ.get(PAIRING_SSH_ENV) or "").strip() or (shutil.which("ssh") or "ssh")


#: Transports that would make "the key we verified" ambiguous or let an existing
#: connection skip host-key checks entirely. Pairing fails on any of them rather
#: than pretending the pinned key was what the user's ssh actually checked.
_REFUSED_SSH_TRANSPORTS = (
    ("proxycommand", "none"),
    ("proxyjump", "none"),
    ("controlpath", "none"),
    ("controlmaster", "false"),
)


def _ssh_effective(destination: str, port: int, *, ssh_bin: str | None = None) -> dict:
    """`ssh -G`: what ssh would really connect to, without connecting."""
    argv = [ssh_bin or pairing_ssh_bin(), "-G", "-p", str(validate_port(port)), validate_ssh_target(destination)]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PairingError(f"could not read your ssh configuration: {exc}") from None
    if completed.returncode != 0:
        detail = " ".join((completed.stderr or "").strip().split())[-200:]
        raise PairingError(
            "could not read your ssh configuration"
            + (f" ({detail})" if detail else "")
        )
    config: dict[str, str] = {}
    for line in (completed.stdout or "").splitlines():
        key, _, value = line.partition(" ")
        config[key.strip().lower()] = value.strip()
    return config


def verified_endpoint(
    destination: str, port: int, *, ssh_bin: str | None = None
) -> tuple[str, int]:
    """The endpoint the verified host key belongs to, or a clear refusal.

    A ProxyJump/ProxyCommand, a ControlMaster socket or a HostName rewrite all
    change WHICH machine (or which key check) is in play. Pairing needs one
    unambiguous answer, so any of them stops it with an explanation instead of
    silently pinning a key that may not be the one ssh will use.
    """
    config = _ssh_effective(destination, port, ssh_bin=ssh_bin)
    for field, inert in _REFUSED_SSH_TRANSPORTS:
        value = str(config.get(field) or "").strip().lower()
        if value and value != inert:
            raise PairingError(
                f"your ssh configuration sets {field}={value} for this machine, so the host key "
                "cannot be verified unambiguously; pair it with a direct ssh connection (or do the "
                "manual steps) and try again. Nothing was written."
            )
    host, _user = pairing_ssh_target_parts(destination)
    effective_host = str(config.get("hostname") or host).strip()
    try:
        effective_port = int(str(config.get("port") or port).strip())
    except ValueError:
        effective_port = port
    if effective_host != host:
        raise PairingError(
            f"your ssh configuration connects to {effective_host} for {host}; use the real host name "
            "so the key that gets pinned is the key that will be checked. Nothing was written."
        )
    if effective_port != int(port):
        raise PairingError(
            f"your ssh configuration uses port {effective_port} for {host}:{port}; pass that port "
            "explicitly so the pinned key and the connection agree. Nothing was written."
        )
    return effective_host, effective_port


def probe_presented_host_keys(
    destination: str,
    port: int,
    *,
    known_hosts: Path,
    ssh_bin: str | None = None,
) -> list[dict]:
    """Learn the presented host key WITHOUT authenticating, then read it back.

    `PreferredAuthentications=none` makes ssh complete the key exchange (which
    is where host-key verification happens) and then stop: no key is offered,
    no command runs. With `StrictHostKeyChecking=accept-new` into a file THIS
    process owns, the accepted line is the presented key -- so a supplied
    fingerprint can be compared before anything authenticated happens.
    """
    argv = [
        ssh_bin or pairing_ssh_bin(),
        "-T",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "PreferredAuthentications=none",
        "-p", str(validate_port(port)),
        validate_ssh_target(destination),
        "true",
    ]
    try:
        subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PairingError(f"could not reach that machine to read its host key: {exc}") from None
    host, _user = pairing_ssh_target_parts(destination)
    try:
        text = known_hosts.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries = known_hosts_entries(text, host, port)
    # Only real key entries count; duplicates are collapsed by type+key.
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for entry in entries:
        marker = (entry["type"], entry["key"])
        if marker in seen:
            continue
        seen.add(marker)
        unique.append({"type": entry["type"], "key": entry["key"]})
    return unique


def ssh_pairing_argv(
    destination: str,
    port: int,
    remote_ccb: str,
    verb: str,
    *,
    known_hosts: Path | None = None,
    accept_new: bool = False,
    identity: str | Path | None = None,
    ssh_bin: str | None = None,
) -> list[str]:
    """ssh argv for one `pairing` call over the user's NORMAL login.

    Deliberately NOT remote_client's delivery argv: pairing has to use the
    user's own ssh (their config, their agent, their jump hosts), because the
    dedicated key does not exist yet. The known-hosts source is still explicit:
    either the user's own file (bootstrapping trust) or a file this process
    wrote containing exactly the verified key.
    """
    argv = [
        ssh_bin or pairing_ssh_bin(),
        "-T",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", f"StrictHostKeyChecking={'accept-new' if accept_new else 'yes'}",
    ]
    if known_hosts is not None:
        argv.extend(
            [
                "-o", f"UserKnownHostsFile={known_hosts}",
                "-o", "GlobalKnownHostsFile=/dev/null",
            ]
        )
    if identity:
        # An explicit login identity for the pairing channel; the user's config
        # is still honoured, so a jump host or option there keeps working.
        argv.extend(["-i", str(identity), "-o", "IdentitiesOnly=yes"])
    argv.extend(["-p", str(validate_port(port)), validate_ssh_target(destination)])
    argv.extend([remote_command(remote_ccb), "pairing", verb])
    return argv


#: What a B whose installed CCB predates W3 prints when the `pairing` verb is
#: handed to it (`bin/ccb-remote` main), always as its first stderr line.
_OLD_PEER_MARKER = "Unknown ccb-remote command: pairing"

#: The remote shell's own words for "the command word is not a file I can run".
_MISSING_COMMAND_RE = re.compile(r"command not found|no such file or directory", re.IGNORECASE)

#: What ssh itself exits with when the connection (not the remote command) died.
_SSH_OWN_FAILURE = 255

#: What a POSIX shell exits with when the command word does not exist.
_COMMAND_NOT_RUNNABLE = 127


def _first_error_line(stderr: str) -> str:
    """The first `[ERROR] ...` line of a remote CCB's stderr, collapsed."""
    for line in stderr.splitlines():
        candidate = " ".join(line.split())
        if candidate.startswith("[ERROR]"):
            return candidate
    return ""


def _pairing_failure_message(returncode: int, stderr: str, remote_ccb: str) -> str:
    """Say WHY the other machine returned no JSON, never quoting its usage dump.

    A machine running a CCB older than W3 answers `pairing` with a usage dump,
    and a machine without ccb-remote answers with the shell's own complaint;
    both used to surface as "the pairing connection failed (<tail>)", which
    tells the user nothing they can act on.
    """
    if _OLD_PEER_MARKER in stderr:
        return (
            "the other machine's CCB is too old to pair; update CCB there, "
            "then run pair again"
        )
    # ssh's own failure (255) is not a statement about the remote command, so
    # its text -- the text the user already acts on -- stays exactly as before.
    if returncode != _SSH_OWN_FAILURE:
        if returncode == _COMMAND_NOT_RUNNABLE or _MISSING_COMMAND_RE.search(stderr):
            return (
                f"ccb-remote was not found on the other machine at {remote_ccb} "
                "(is CCB installed there? use --remote-ccb PATH if it lives elsewhere)"
            )
    # Otherwise today's behaviour, except that a remote CCB's own error line is
    # preferred over a character-tail that a usage dump can dominate.
    detail = "" if returncode == _SSH_OWN_FAILURE else _first_error_line(stderr)
    if not detail:
        detail = " ".join(stderr.strip().split())[-300:]
    if not detail:
        return f"the pairing connection failed (ssh exited {returncode})"
    return f"the pairing connection failed ({detail})"


def run_pairing(
    argv: list[str],
    request: dict,
    *,
    timeout_s: float | None = None,
    remote_ccb: str | None = None,
) -> dict:
    """Run one `pairing <verb>` call and return its JSON result.

    The request travels on stdin and the result is the ONLY thing on stdout;
    anything else (a shell banner, an rc file's echo) is reported as an
    unusable answer instead of being parsed as structure. `remote_ccb` is the
    path the USER named (never the `"$HOME"`-quoted form on the wire), so a
    failure can name the file the other machine should have.
    """
    payload = json.dumps(request)
    try:
        completed = subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout_s if timeout_s is not None else pairing_timeout_s(),
        )
    except FileNotFoundError:
        raise PairingError(f"ssh was not found ({argv[0]})") from None
    except subprocess.TimeoutExpired:
        raise PairingError("the other machine did not answer in time") from None
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    result = _last_json_object(stdout)
    if result is None:
        if completed.returncode != 0:
            raise PairingError(
                _pairing_failure_message(
                    completed.returncode,
                    stderr,
                    remote_ccb or pairing_remote_ccb_default(),
                )
            )
        raise PairingError("the other machine returned no usable answer")
    if completed.returncode != 0 and result.get("ok") is not False:
        # A non-zero exit with an ok answer is still an answer ssh may have
        # muddled; treat only an explicit refusal as trustworthy.
        raise PairingError(
            f"the pairing command failed (ssh exited {completed.returncode})"
            + (f": {' '.join(stderr.strip().split())[-200:]}" if stderr.strip() else "")
        )
    return result


def _last_json_object(text: str) -> dict | None:
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def pairing_remote_ccb_default() -> str:
    """B's ccb-remote path: `~/.local/bin/ccb-remote` (remote PATH is often bare)."""
    return "~/.local/bin/ccb-remote"


#: The ONLY characters a remote ccb-remote path may contain. ssh joins the
#: remote command's argv with spaces and hands it to B's login shell, so a
#: locally safe argv proves nothing: the path itself has to be inert.
REMOTE_CCB_RE = re.compile(r"^(?:~/|/)[A-Za-z0-9._+/-]*$")


def validate_remote_ccb(path: object, field: str = "the remote ccb-remote path") -> str:
    """An absolute or `~/`-relative path made only of inert characters."""
    text = str(path or "").strip()
    if not text:
        raise PairingError(f"{field} is empty")
    if len(text) > 512:
        raise PairingError(f"{field} is too long")
    if text.startswith("-") or not REMOTE_CCB_RE.fullmatch(text):
        raise PairingError(
            f"{field} may only contain letters, digits, '.', '_', '+', '-' and '/' "
            "(and must be absolute or start with ~/)"
        )
    if any(segment == ".." for segment in text.split("/")):
        raise PairingError(f"{field} may not contain '..'")
    if text in ("/", "~/"):
        raise PairingError(f"{field} names a directory, not a command")
    return text


def remote_command(path: object) -> str:
    """The single remote command word, safe against ssh's space-joined argv.

    A `~/` path is NOT sent as a quoted `~` (a quoted tilde never expands).
    Instead the home prefix is written as `"$HOME"` -- a literal this module
    controls -- and the rest is a validated path suffix, so the only thing the
    remote shell ever expands is `$HOME` itself.
    """
    text = validate_remote_ccb(path)
    if text.startswith("~/"):
        return '"$HOME"/' + text[2:]
    return text


def pin_file_from_keys(
    keys: list[dict], *, host: str, port: int, home: Path | None = None
) -> Path:
    """A private temp known_hosts holding exactly the verified key(s)."""
    home = Path(home) if home is not None else Path.home()
    directory = home / ".ccb" / "remote"
    _ensure_dir(directory)
    handle = tempfile.NamedTemporaryFile(
        "w", dir=str(directory), prefix=".pairing-pin.", suffix=".tmp", delete=False, encoding="utf-8"
    )
    with handle:
        for item in keys:
            handle.write(known_hosts_line(host, port, item["type"], item["key"]) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    path = Path(handle.name)
    os.chmod(path, 0o600)
    return path


def pairing_ssh_target_parts(destination: str) -> tuple[str, str]:
    """Split `[user@]host` into (host, user) for known_hosts lookups."""
    user, _, host = validate_ssh_target(destination).rpartition("@")
    return host, user


def sanitized_principal(value: str) -> str:
    """A hostname-shaped default principal, validated like any other value."""
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text).strip("-_")
    if not text or not PRINCIPAL_RE.fullmatch(text):
        raise PairingError("could not derive a principal name; pass --principal")
    return text


def generated_token() -> str:
    import secrets

    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))
