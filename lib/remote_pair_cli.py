"""Guided `ccb-remote pair` / `unpair` (M3 W3, source side).

The interaction is here; the values, validation, B-side verbs and SSH channel
are in `remote_pairing`. Order of operations is the ratified one:

    verify the machine's identity FIRST (the user's own ssh/known_hosts, or an
    independently supplied fingerprint) -> read-only inspect -> pick projects ->
    show the concrete diff for BOTH machines -> typed approval -> write
    (A key, B apply, A pin merge, A hosts.json) -> verify with `list`.

Every step before the approval writes nothing. Every step after it is
idempotent and reports exactly what is done and what is left, because the user
may interrupt at any point and re-run `pair` to resume (A2 recovery).

The pairing SSH channel deliberately uses the user's NORMAL ssh login: the
dedicated restricted key does not exist on B until `apply` has run, and it can
only ever run the gate afterwards.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import remote_client
import remote_discovery
from remote_pairing import (
    PairingError,
    atomic_write,
    ccb_key_line,
    fingerprint,
    known_hosts_merge,
    known_hosts_remove,
    matching_host_keys,
    known_hosts_entries,
    normalize_fingerprint,
    pairing_remote_ccb_default,
    pairing_lock,
    pin_rotation_problem,
    probe_presented_host_keys,
    verified_endpoint,
    ssh_pairing_argv,
    pairing_ssh_target_parts,
    pin_file_from_keys,
    read_text_or_empty,
    run_pairing,
    sanitized_principal,
    split_pubkey,
    generated_token,
    user_known_host_keys,
    validate_alias,
    validate_port,
    validate_principal,
    validate_providers,
    validate_pubkey,
    validate_ssh_target,
    validate_token,
    validate_work_dir,
)

EXIT_OK = 0
EXIT_REFUSED = 1
#: Distinct from a refusal: the pairing is still in place, only revocation of
#: the remote grant is outstanding (A3). Local deletion alone never says "done".
EXIT_REMOTE_PENDING = 2

PROMPT = "> "


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #


@dataclass
class PairOptions:
    alias: str = ""
    destination: str = ""
    port: int = 22
    principal: str = ""
    token: str = ""
    key_path: str = ""
    fingerprint: str = ""
    remote_ccb: str = ""
    known_hosts: str = ""
    identity: str = ""
    providers: tuple[str, ...] = ("claude", "codex")
    shares: dict[str, dict] = field(default_factory=dict)
    share_specs: list[str] = field(default_factory=list)
    shares_json: str = ""
    yes: bool = False
    print_remote_steps: bool = False
    rotate_pin: bool = False


@dataclass
class UnpairOptions:
    alias: str = ""
    local_only: bool = False
    principal: str = ""
    destination: str = ""
    port: int = 22
    remote_ccb: str = ""
    known_hosts: str = ""
    identity: str = ""
    yes: bool = False


def _ask_value(prompt: str, default: str, *, input_fn: Callable[[str], str]) -> str:
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    answer = (input_fn(shown) or "").strip()
    return answer or default


def _confirm(input_fn: Callable[[str], str], out) -> bool:
    print("Type 'yes' to apply these changes, anything else to stop.", file=out)
    try:
        answer = (input_fn(PROMPT) or "").strip()
    except EOFError:
        return False
    return answer.lower() == "yes"


# --------------------------------------------------------------------------- #
# Paths and local state
# --------------------------------------------------------------------------- #


def _remote_root() -> Path:
    return remote_client.remote_root()


def _pin_path() -> Path:
    return remote_client.remote_root() / "known_hosts"


def _keys_dir() -> Path:
    return remote_client.remote_root() / "keys"


def _pairing_records_dir() -> Path:
    return remote_client.remote_root() / "pairings"


def _pairing_record_path(alias: str) -> Path:
    return _pairing_records_dir() / f"{alias}.json"


def _read_pairing_record(alias: str) -> dict:
    try:
        data = json.loads(_pairing_record_path(alias).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_pairing_record(alias: str, record: dict) -> None:
    directory = _pairing_records_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    atomic_write(
        _pairing_record_path(alias),
        (json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        default_mode=0o600,
    )


def _existing_hosts() -> dict:
    try:
        return remote_client.load_hosts()
    except remote_client.RemoteConfigError:
        return {}


def _resume_conflict(flag: str, recorded, alias: str) -> str:
    """Refuse a value that would silently change a half-done pairing."""
    return (
        f"{flag} does not match the pairing already in progress for {alias} (recorded: {recorded}). "
        "A retry finishes that pairing with the scope that was approved, so its values cannot be "
        f"changed under it; finish it as recorded, or remove ~/.ccb/remote/pairings/{alias}.json "
        "and start again."
    )


def _resume_key_mismatch(record: dict, pubkey_now: str, key_path: Path) -> str:
    """Why the key on disk is not the key this pairing approved, or ""."""
    recorded = str(record.get("pubkey") or "").strip()
    recorded_fp = str(record.get("pubkey_fingerprint") or "").strip()
    if not recorded and not recorded_fp:
        return ""
    recorded_blob = (split_pubkey(recorded) or ("", ""))[1] if recorded else ""
    blob_now = (split_pubkey(pubkey_now) or ("", ""))[1] if pubkey_now else ""
    same = bool(recorded_blob) and recorded_blob == blob_now
    if recorded_fp:
        try:
            same = bool(blob_now) and normalize_fingerprint(
                fingerprint(blob_now)
            ) == normalize_fingerprint(recorded_fp)
        except PairingError:
            # A key this build cannot fingerprint is not provably the recorded
            # one; the blob comparison decides.
            same = bool(recorded_blob) and recorded_blob == blob_now
    elif not recorded_blob:
        return ""
    if same:
        return ""
    return (
        f"the key this pairing approved ({recorded_fp or 'recorded earlier'}) is not the key at "
        f"{key_path} now, so the pairing cannot be resumed under a different key. Restore that "
        f"key, or remove ~/.ccb/remote/pairings/{record.get('alias') or ''}.json and pair again. "
        "Nothing was written."
    )


def _recorded_shares(record: dict) -> dict[str, dict]:
    """The shares a recorded pairing approved, in the shape `apply` takes."""
    shares: dict[str, dict] = {}
    for entry in record.get("shares") or []:
        if not isinstance(entry, dict):
            continue
        try:
            alias = validate_alias(entry.get("alias"), "project alias")
            shares[alias] = {
                "alias": alias,
                "work_dir": validate_work_dir(entry.get("work_dir")),
                "providers": list(validate_providers(entry.get("providers"))),
            }
        except PairingError:
            continue
    return shares


def _recorded_pin(record: dict) -> list[dict]:
    """The host keys a recorded pairing approved, as `{type, key}` pairs."""
    pins: list[dict] = []
    for item in record.get("pin") or []:
        if not isinstance(item, dict):
            continue
        key_type = str(item.get("type") or "").strip()
        key = str(item.get("key") or "").strip()
        if not key_type.startswith(("ssh-", "ecdsa-", "sk-")) or not key:
            continue
        if any(character.isspace() for character in key):
            continue
        pins.append({"type": key_type, "key": key})
    return pins


# --------------------------------------------------------------------------- #
# Key material
# --------------------------------------------------------------------------- #


def _keygen_binary() -> str:
    found = shutil.which("ssh-keygen")
    if not found:
        raise PairingError("ssh-keygen was not found on this machine")
    return found


def public_key_text(private: Path) -> str:
    """The public key line for an existing key pair, or "" when there is none."""
    public = private.with_name(private.name + ".pub")
    try:
        text = public.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        try:
            return validate_pubkey(text)
        except PairingError:
            text = ""
    if not private.is_file():
        return ""
    try:
        completed = subprocess.run(
            [_keygen_binary(), "-y", "-f", str(private)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    try:
        return validate_pubkey((completed.stdout or "").strip())
    except PairingError:
        return ""


def generate_key(private: Path, *, comment: str) -> str:
    """Create an ed25519 key with no passphrase and return its public line."""
    private.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(private.parent, 0o700)
    except OSError:
        pass
    completed = subprocess.run(
        [
            _keygen_binary(),
            "-q",
            "-t", "ed25519",
            "-N", "",
            "-C", comment,
            "-f", str(private),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise PairingError(
            "the pairing key could not be created: "
            + " ".join((completed.stderr or "").strip().split())[-200:]
        )
    try:
        os.chmod(private, 0o600)
    except OSError:
        pass
    public = public_key_text(private)
    if not public:
        raise PairingError("the pairing key was created but its public half is unreadable")
    return public


# --------------------------------------------------------------------------- #
# hosts.json (validated before it replaces the real file)
# --------------------------------------------------------------------------- #


def _hosts_document() -> dict:
    path = remote_client.hosts_config_path()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"hosts": {}}
    except (OSError, ValueError) as exc:
        raise PairingError(f"hosts.json is unreadable or invalid: {exc}") from None
    if not isinstance(document, dict) or not isinstance(document.get("hosts", {}), dict):
        raise PairingError("hosts.json must contain a hosts object")
    document.setdefault("hosts", {})
    return document


def _write_hosts_document(document: dict) -> None:
    path = remote_client.hosts_config_path()
    os.makedirs(path.parent, mode=0o700, exist_ok=True)
    payload = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    # Validate through the real loader BEFORE the file is replaced, using a
    # sibling temp file with the same name shape (the loader only reads it).
    probe = path.with_name(f".{path.name}.validate")
    try:
        atomic_write(probe, payload, default_mode=0o600)
        remote_client.load_hosts(probe)
    except remote_client.RemoteConfigError as exc:
        try:
            probe.unlink()
        except OSError:
            pass
        raise PairingError(f"the host entry would be refused by CCB: {exc}") from None
    except BaseException:
        try:
            probe.unlink()
        except OSError:
            pass
        raise
    try:
        probe.unlink()
    except OSError:
        pass
    atomic_write(path, payload, default_mode=0o600)


def _hosts_entry(
    ssh_target: str, port: int, identity_file: Path, issuer_token: str
) -> dict:
    return {
        "ssh_target": ssh_target,
        "port": port,
        "identity_file": str(identity_file),
        "known_hosts": str(_pin_path()),
        "issuer_token": issuer_token,
    }


# --------------------------------------------------------------------------- #
# Shares
# --------------------------------------------------------------------------- #


def _share_from_spec(spec: str, *, projects: list[dict], default_providers) -> tuple[str, dict]:
    """`ALIAS` (project by name) or `ALIAS=WORK_DIR` (explicit directory)."""
    alias_part, _, work_dir = spec.partition("=")
    alias = validate_alias(alias_part.strip(), "project alias")
    work_dir = work_dir.strip()
    if not work_dir:
        matches = [
            project
            for project in projects
            if project.get("name") == alias or Path(str(project.get("work_dir"))).name == alias
        ]
        if len(matches) != 1:
            raise PairingError(
                f"'{alias}' does not name exactly one project on the other machine; "
                "use ALIAS=WORK_DIR or pick from the list"
            )
        work_dir = str(matches[0]["work_dir"])
        providers = tuple(matches[0].get("providers") or ()) or default_providers
    else:
        providers = default_providers
    return alias, {
        "alias": alias,
        "work_dir": validate_work_dir(work_dir),
        "providers": list(validate_providers(providers)),
    }


def resolve_shares(
    options: PairOptions, projects: list[dict]
) -> dict[str, dict]:
    shares: dict[str, dict] = {}
    if options.shares_json:
        try:
            document = json.loads(options.shares_json)
        except ValueError as exc:
            raise PairingError(f"--shares-json is not valid JSON: {exc}") from None
        if not isinstance(document, dict) or not document:
            raise PairingError("--shares-json must be an object of alias -> share")
        for name, entry in document.items():
            if not isinstance(entry, dict):
                raise PairingError("each share must be an object")
            alias = validate_alias(name, "project alias")
            shares[alias] = {
                "alias": alias,
                "work_dir": validate_work_dir(entry.get("work_dir")),
                "providers": list(
                    validate_providers(
                        entry.get("providers") or list(options.providers)
                    )
                ),
            }
    for alias, share in options.shares.items():
        # Explicit shares are validated like any other value: this path is used
        # by flags and by callers, so nothing may reach a write unvalidated.
        entry_alias = validate_alias(alias, "project alias")
        shares[entry_alias] = {
            "alias": entry_alias,
            "work_dir": validate_work_dir(share.get("work_dir")),
            "providers": list(validate_providers(share.get("providers") or options.providers)),
        }
    for spec in options.share_specs:
        alias, share = _share_from_spec(
            spec, projects=projects, default_providers=options.providers
        )
        shares[alias] = share
    return shares


def _inherit_project_providers(shares: dict[str, dict], projects: list[dict]) -> None:
    by_dir = {str(project.get("work_dir")): project for project in projects}
    for share in shares.values():
        project = by_dir.get(share["work_dir"])
        if not project:
            continue
        available = [name for name in project.get("providers") or [] if name in ("claude", "codex")]
        if available and not set(share["providers"]) & set(available):
            share["providers"] = list(validate_providers(available))


# --------------------------------------------------------------------------- #
# Pairing channel
# --------------------------------------------------------------------------- #


@dataclass
class _Channel:
    destination: str
    port: int
    remote_ccb: str
    known_hosts: Optional[Path] = None
    accept_new: bool = False
    identity: str = ""

    def argv(self, verb: str) -> list[str]:
        return ssh_pairing_argv(
            self.destination,
            self.port,
            self.remote_ccb,
            verb,
            known_hosts=self.known_hosts,
            accept_new=self.accept_new,
            identity=self.identity or None,
        )

    def call(self, verb: str, request: dict) -> dict:
        # The user's own path, not the `"$HOME"`-quoted form the wire needs:
        # a failure message has to name a file they can look for.
        return run_pairing(self.argv(verb), request, remote_ccb=self.remote_ccb)


def _bootstrap_trust(
    channel: _Channel,
    *,
    destination: str,
    port: int,
    supplied_fingerprint: str,
    inspect_request: dict,
    user_known_hosts_file: Optional[Path],
    recorded_pin: list[dict] | None = None,
    out,
) -> tuple[dict, list[dict], Path]:
    """Verify who we are talking to BEFORE anything is written (A1).

    Three acceptable ways to know the key, and no fourth:

    * the user supplied `--fingerprint`: it is compared to the key the machine
      presents over a connection that authenticated nothing
      (`PreferredAuthentications=none`) -- and, when the user's own ssh already
      knows the endpoint, the selected key must be one their ssh accepts too;
    * a pairing record from an earlier run: the host key IT approved is the
      anchor (a retry must not quietly adopt a different machine);
    * the user's own ssh has exactly one key for this endpoint.

    Anything else stops here with zero writes. Exactly ONE key is selected, and
    the authenticated connection that follows is pinned to THAT key alone (not
    to the user's whole known_hosts), so the machine that answers is provably
    the machine whose key was selected. Keys the user's ssh accepts but that the
    answering machine does not report are a refusal, not a second pin.
    """
    host, _user = pairing_ssh_target_parts(destination)
    user_keys = user_known_host_keys(host, port, path=user_known_hosts_file)
    supplied = supplied_fingerprint.strip()

    if supplied:
        # An explicit fingerprint is the strongest statement the user makes, so
        # it is checked even when their known_hosts already knows the machine
        # (a stale or wrong entry must never silently outvote it).
        expected = normalize_fingerprint(supplied)
        scratch = pin_file_from_keys([], host=host, port=port)
        try:
            probe_presented_host_keys(destination, port, known_hosts=scratch)
            presented = [
                {"type": entry["type"], "key": entry["key"]}
                for entry in known_hosts_entries(read_text_or_empty(scratch), host, port)
            ]
            if not presented:
                raise PairingError(
                    "that machine's host key could not be read without authenticating, so the "
                    "supplied fingerprint cannot be checked; nothing was written"
                )
            matched = [
                item
                for item in presented
                if normalize_fingerprint(fingerprint(item["key"])) == expected
            ]
            if not matched:
                raise PairingError(
                    "the fingerprint you supplied does not match the key that machine presented; "
                    "nothing was written"
                )
            if user_keys:
                accepted = matching_host_keys(matched, user_keys)
                if not accepted:
                    raise PairingError(
                        "the fingerprint you supplied names a key your own ssh would not accept for "
                        f"{host}:{port} (your known_hosts pins "
                        f"{', '.join(sorted(fingerprint(item['key']) for item in user_keys))}); "
                        "stop and check which machine (and which key) you are pairing. Nothing was "
                        "written"
                    )
                matched = accepted
            verified = _one_key(matched, destination=destination, port=port)
        finally:
            try:
                scratch.unlink()
            except OSError:
                pass
        print(f"trust: the supplied fingerprint matches {destination}:{port}", file=out)
        pin_file = pin_file_from_keys(verified, host=host, port=port)
        return (
            _inspect_with_selected_pin(
                channel,
                verified=verified,
                pin_file=pin_file,
                inspect_request=inspect_request,
                destination=destination,
                port=port,
            ),
            verified,
            pin_file,
        )

    if recorded_pin:
        # B3 resume: the approved host key is part of the approved scope, so a
        # retry verifies against it instead of starting a new trust decision.
        if user_keys:
            verified = matching_host_keys(recorded_pin, user_keys)
            if not verified:
                raise PairingError(
                    "the host key this pairing approved earlier is no longer accepted by your ssh "
                    "for that machine; nothing was written -- unpair it and pair again if the "
                    "machine was deliberately re-keyed"
                )
        else:
            verified = [
                {"type": item["type"], "key": item["key"]} for item in recorded_pin
            ]
        verified = _one_key(verified, destination=destination, port=port)
        print(
            f"trust: resuming with the host key approved earlier for {destination}:{port} "
            f"({fingerprint(verified[0]['key'])})",
            file=out,
        )
        pin_file = pin_file_from_keys(verified, host=host, port=port)
        return (
            _inspect_with_selected_pin(
                channel,
                verified=verified,
                pin_file=pin_file,
                inspect_request=inspect_request,
                destination=destination,
                port=port,
            ),
            verified,
            pin_file,
        )

    if user_keys:
        verified = _one_key(user_keys, destination=destination, port=port)
        print(
            f"trust: {destination}:{port} is verified in your ssh known_hosts "
            f"({fingerprint(verified[0]['key'])})",
            file=out,
        )
        pin_file = pin_file_from_keys(verified, host=host, port=port)
        return (
            _inspect_with_selected_pin(
                channel,
                verified=verified,
                pin_file=pin_file,
                inspect_request=inspect_request,
                destination=destination,
                port=port,
            ),
            verified,
            pin_file,
        )

    raise PairingError(
        f"this machine has no verified host key for {host}:{port}. Connect once with plain ssh and "
        "compare the fingerprint out of band, then run pairing again; or pass "
        "--fingerprint SHA256:... with a value you verified yourself. Nothing was written."
    )


def _inspect_with_selected_pin(
    channel: _Channel,
    *,
    verified: list[dict],
    pin_file: Path,
    inspect_request: dict,
    destination: str,
    port: int,
) -> dict:
    """The read-only inspect, over the SELECTED key only (strict).

    The connection may only use the pin this run selected. Anything else -- the
    user's whole known_hosts, an accept-new scratch file -- would let a machine
    answer while ssh verified a key pairing did not select.

    The caller only learns `pin_file` when this returns, so on ANY failure or
    interruption the temporary pin is removed here.
    """
    try:
        channel.known_hosts = pin_file
        channel.accept_new = False
        result = channel.call("inspect", inspect_request)
        if not matching_host_keys(result.get("host_keys") or [], verified):
            raise PairingError(
                "the host key that machine reports does not match the key selected for this "
                f"pairing ({fingerprint(verified[0]['key'])}); stop and check the machine before "
                "pairing, nothing was written"
            )
        return result
    except BaseException:
        try:
            pin_file.unlink()
        except OSError:
            pass
        raise

def _one_key(keys: list[dict], *, destination: str, port: int) -> list[dict]:
    """Exactly one host key, or a refusal that says how to name the right one.

    Pinning several keys for one endpoint would claim ssh verifies any of them;
    without a fingerprint this machine cannot say WHICH one the connection used
    (ssh's own negotiation is not part of its public interface here), so the
    honest answer is to ask for the value the user verified out of band.
    """
    unique: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in keys:
        marker = (item["type"], item["key"])
        if marker in seen:
            continue
        seen.add(marker)
        unique.append({"type": item["type"], "key": item["key"]})
    if len(unique) > 1:
        shown = ", ".join(sorted(fingerprint(item["key"]) for item in unique))
        raise PairingError(
            f"your ssh accepts {len(unique)} host keys for {destination}:{port} ({shown}), and "
            "pairing pins exactly one; pass --fingerprint SHA256:... with the value you verified "
            "out of band. Nothing was written."
        )
    return unique


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _render_projects(projects: list[dict], out) -> None:
    if not projects:
        print("  (no CCB projects found in that machine's registry)", file=out)
        return
    for index, project in enumerate(projects, start=1):
        providers = ",".join(project.get("providers") or []) or "none"
        print(f"  [{index}] {project.get('work_dir')}  ({providers})", file=out)


def _render_diff(
    *,
    alias: str,
    destination: str,
    port: int,
    gate: str,
    principal: str,
    token: str,
    pubkey: str,
    shares: dict[str, dict],
    key_path: Path,
    key_present: bool,
    pin_keys: list[dict],
    host,
    out,
) -> None:
    print("", file=out)
    print(f"On {alias} ({destination}:{port}):", file=out)
    if pubkey:
        line = ccb_key_line(gate, principal, pubkey)
    else:
        # The key is created at write time; render the same shape without
        # pretending a placeholder key is a real one.
        line = f'restrict,command="{gate} --principal {principal}" <the new public key>'
    print(f"  authorized_keys += {line}", file=out)
    print(f"  config.json principals[{principal}] = token {token}", file=out)
    for share in shares.values():
        providers = ",".join(share["providers"])
        print(
            f"  config.json aliases[{share['alias']}] = {share['work_dir']} ({providers})",
            file=out,
        )
    print("On this machine:", file=out)
    if key_present:
        print(f"  reuse key {key_path}", file=out)
    else:
        print(f"  create key {key_path}", file=out)
    for item in pin_keys:
        print(f"  pin {known_hosts_line_for_display(host, port, item)}", file=out)
    print(
        f"  hosts.json hosts[{alias}] = {destination}:{port}, token {token}",
        file=out,
    )
    print("", file=out)


def known_hosts_line_for_display(host: str, port: int, item: dict) -> str:
    from remote_pairing import known_hosts_line

    return known_hosts_line(host, port, item["type"], item["key"])


def _progress(out, done: list[str], pending: list[str], resume_hint: str) -> None:
    if done:
        print("done: " + ", ".join(done), file=out)
    if pending:
        print("not done: " + ", ".join(pending), file=out)
        print(resume_hint, file=out)


# --------------------------------------------------------------------------- #
# pair
# --------------------------------------------------------------------------- #


def pair(
    options: PairOptions,
    *,
    out=sys.stdout,
    err=sys.stderr,
    input_fn: Callable[[str], str] = input,
) -> int:
    try:
        return _pair(options, out=out, err=err, input_fn=input_fn)
    except PairingError as exc:
        print(f"[ERROR] {exc}", file=err)
        return EXIT_REFUSED


def _pair(options: PairOptions, *, out, err, input_fn) -> int:
    interactive = not options.yes
    alias = options.alias.strip()
    if not alias:
        if not interactive:
            raise PairingError("pair needs --alias in non-interactive mode")
        alias = _ask_value("Name for that machine (the host alias)", "", input_fn=input_fn)
    alias = validate_alias(alias, "machine name")

    # B3: the record of an unfinished pairing is the authority for everything
    # already approved, so it is read BEFORE the destination (and the rest) is
    # resolved. `pair <alias>` alone therefore resumes instead of starting a
    # new transaction with a fresh identity.
    record = _read_pairing_record(alias)
    resuming = bool(record) and not record.get("applied_at")
    # What the record looked like BEFORE this run touched it, so a definitive
    # refusal can put an earlier run's record back instead of deleting it.
    record_path = _pairing_record_path(alias)
    record_before = record_path.read_bytes() if record_path.is_file() else None

    existing_hosts = _existing_hosts()
    existing_entry = (
        existing_hosts[alias].__dict__ if alias in existing_hosts else {}
    )

    explicit_destination = options.destination.strip()
    recorded_destination = str(record.get("ssh_target") or "") if resuming else ""
    if explicit_destination and recorded_destination and explicit_destination != recorded_destination:
        raise PairingError(_resume_conflict("--destination", recorded_destination, alias))
    destination = explicit_destination or recorded_destination or (
        str(existing_entry.get("ssh_target") or "") if existing_entry else ""
    )
    if not destination:
        if not interactive:
            raise PairingError("pair needs --destination [user@]host")
        destination = _ask_value("SSH destination of that machine (user@host)", "", input_fn=input_fn)
    destination = validate_ssh_target(destination)

    port_given = options.port != 22
    explicit_port = options.port if port_given else 0
    recorded_port = (
        int(record["port"]) if str(record.get("port") or "").isdigit() else 0
    ) if resuming else 0
    if explicit_port and recorded_port and explicit_port != recorded_port:
        raise PairingError(_resume_conflict("--port", recorded_port, alias))
    port = explicit_port or recorded_port or (
        int(existing_entry["port"])
        if existing_entry and str(existing_entry.get("port") or "").isdigit()
        else 22
    )
    if interactive and not port_given and not recorded_port and not existing_entry:
        port = validate_port(_ask_value("SSH port", "22", input_fn=input_fn))
    port = validate_port(port)

    if existing_entry and str(existing_entry.get("ssh_target") or "") not in ("", destination):
        raise PairingError(
            f"host alias {alias} is already configured for a different machine; run "
            f"`ccb-remote unpair {alias}` first"
        )

    if resuming:
        recorded_principal = str(record.get("principal") or "")
        if options.principal.strip() and recorded_principal and options.principal.strip() != recorded_principal:
            raise PairingError(_resume_conflict("--principal", recorded_principal, alias))
        recorded_token = str(record.get("issuer_token") or "")
        if options.token.strip() and recorded_token and options.token.strip() != recorded_token:
            raise PairingError(_resume_conflict("--token", "(recorded)", alias))

    remote_ccb = options.remote_ccb.strip() or str(record.get("remote_ccb") or "") or pairing_remote_ccb_default()
    # B1: the endpoint the pinned key belongs to must be the one ssh will use.
    verified_endpoint(destination, port)

    key_path = (
        Path(options.key_path).expanduser()
        if options.key_path
        else Path(str(record.get("key_path") or "")) if record.get("key_path") else
        _keys_dir() / alias
    )
    if not options.key_path and existing_entry.get("identity_file"):
        key_path = Path(str(existing_entry["identity_file"])).expanduser()

    pubkey_existing = public_key_text(key_path)
    if resuming:
        # B3 + key identity: the recorded key IS the pairing's identity, so a
        # retry whose key file was replaced (or points elsewhere) must stop
        # instead of silently re-applying under a different key.
        mismatch = _resume_key_mismatch(record, pubkey_existing, key_path)
        if mismatch:
            raise PairingError(mismatch)
    inspect_request = {
        "verb": "inspect",
        # On a resume the recorded principal is the one this pairing will use,
        # so ask about THAT (the flags may be empty on a retry).
        "lookup_principal": options.principal.strip()
        or (str(record.get("principal") or "") if resuming else ""),
        "lookup_pubkey": pubkey_existing,
    }

    user_known_hosts_file = (
        Path(options.known_hosts).expanduser() if options.known_hosts.strip() else None
    )
    channel = _Channel(
        destination=destination,
        port=port,
        remote_ccb=remote_ccb,
        identity=options.identity.strip(),
    )
    pin_file: Path | None = None
    try:
        inspect_result, verified_keys, pin_file = _bootstrap_trust(
            channel,
            destination=destination,
            port=port,
            supplied_fingerprint=options.fingerprint.strip(),
            inspect_request=inspect_request,
            user_known_hosts_file=user_known_hosts_file,
            recorded_pin=_recorded_pin(record) if resuming else None,
            out=out,
        )
        channel.known_hosts = pin_file
        gate = str(inspect_result.get("gate") or "")
        if not inspect_result.get("gate_present") or not gate:
            raise PairingError(
                "CCB's remote gate was not found on that machine (is CCB installed there?); "
                "nothing was written"
            )

        # Prefer an explicit value, then what the existing pairing/hosts file
        # says (a re-run must reuse the same principal, token and key).
        principal = options.principal.strip()
        if not principal:
            bound = list(inspect_result.get("bound_principals") or [])
            if resuming and record.get("principal"):
                principal = str(record["principal"])
            elif len(bound) == 1:
                principal = str(bound[0])
            elif record.get("principal"):
                principal = str(record["principal"])
            else:
                principal = ""
        if not principal:
            default_principal = ""
            try:
                default_principal = sanitized_principal(os.uname().nodename)
            except Exception:
                default_principal = ""
            if interactive:
                principal = _ask_value(
                    "Principal name for this machine on the other side",
                    default_principal,
                    input_fn=input_fn,
                )
            else:
                principal = default_principal
        principal = validate_principal(principal)

        token = (
            options.token.strip()
            or (str(record.get("issuer_token") or "") if resuming else "")
            or str(existing_entry.get("issuer_token") or "")
            or str(record.get("issuer_token") or "")
        )
        if not token:
            token = generated_token()
        token = validate_token(token)

        # Shares: explicit values win, then the projects found on that machine.
        shares = resolve_shares(options, inspect_result.get("projects") or [])
        shares_from_record = False
        if resuming:
            approved = _recorded_shares(record)
            if shares and approved and shares != approved:
                raise PairingError(
                    _resume_conflict(
                        "--share",
                        ", ".join(sorted(approved)) or "(none)",
                        alias,
                    )
                )
            if approved:
                shares = approved
                shares_from_record = True
        if not shares:
            if not interactive:
                raise PairingError("no sharing select took place; pass --share ALIAS[=WORK_DIR]")
            projects = inspect_result.get("projects") or []
            print("", file=out)
            print("Projects on that machine (choose what this machine may address):", file=out)
            _render_projects(projects, out)
            raw = _ask_value(
                "Numbers to share, comma separated (for example 1,3)",
                ",".join(str(index) for index, _p in enumerate(projects, start=1)),
                input_fn=input_fn,
            )
            chosen = [part.strip() for part in raw.split(",") if part.strip()]
            for item in chosen:
                if not item.isdigit() or not 1 <= int(item) <= len(projects):
                    raise PairingError(f"'{item}' is not one of the listed projects")
                project = projects[int(item) - 1]
                share_alias = validate_alias(
                    _ask_value(
                        f"Alias this machine will type for {project.get('work_dir')}",
                        re.sub(r"[^a-z0-9-]+", "-", str(project.get("name") or "").lower()).strip("-")
                        or f"project{int(item)}",
                        input_fn=input_fn,
                    ),
                    "project alias",
                )
                providers_raw = _ask_value(
                    f"Providers for {share_alias} (comma separated)",
                    ",".join(project.get("providers") or []) or ",".join(options.providers),
                    input_fn=input_fn,
                )
                shares[share_alias] = {
                    "alias": share_alias,
                    "work_dir": validate_work_dir(project.get("work_dir")),
                    "providers": list(validate_providers(providers_raw)),
                }
        if not shares:
            raise PairingError("nothing would be shared; pairing would be useless")
        if not shares_from_record:
            _inherit_project_providers(shares, inspect_result.get("projects") or [])

        # Fail before the first write if the other side would refuse.
        conflicts = _precheck_conflicts(
            inspect_result, principal=principal, token=token, pubkey=pubkey_existing, shares=shares
        )
        if conflicts:
            raise PairingError(conflicts)

        rotation = pin_rotation_problem(
            read_text_or_empty(_pin_path()),
            pairing_ssh_target_parts(destination)[0],
            port,
            verified_keys,
        )
        if rotation and not options.rotate_pin:
            raise PairingError(rotation)

        pubkey_preview = pubkey_existing
        _render_diff(
            alias=alias,
            destination=destination,
            port=port,
            gate=gate,
            principal=principal,
            token=token,
            pubkey=pubkey_preview,
            shares=shares,
            key_path=key_path,
            key_present=bool(pubkey_existing),
            pin_keys=verified_keys,
            host=pairing_ssh_target_parts(destination)[0],
            out=out,
        )

        if not options.yes and not _confirm(input_fn, out):
            print("stopped: nothing was written.", file=out)
            return EXIT_REFUSED

        # ---- writes start here: key -> metadata -> B apply -> pin -> hosts ----
        done: list[str] = []
        created_key = not pubkey_existing
        pubkey = pubkey_existing or generate_key(key_path, comment=f"ccb-{alias}")
        done.append(f"key {key_path}" if created_key else f"reused key {key_path}")
        share_list = [
            {
                "alias": share["alias"],
                "work_dir": share["work_dir"],
                "providers": list(share["providers"]),
            }
            for share in shares.values()
        ]
        host, _user = pairing_ssh_target_parts(destination)
        # B3: the approved scope is durable BEFORE B is touched, so an
        # interrupted pairing resumes with the same identity and grants.
        _write_pairing_record(
            alias,
            {
                "alias": alias,
                "principal": principal,
                "issuer_token": token,
                "pubkey": pubkey,
                "pubkey_fingerprint": fingerprint(pubkey.split(" ")[1]),
                "key_path": str(key_path),
                "ssh_target": destination,
                "host": host,
                "port": port,
                "remote_ccb": remote_ccb,
                "pin": [
                    {"type": item["type"], "key": item["key"], "fingerprint": fingerprint(item["key"])}
                    for item in verified_keys
                ],
                "shares": share_list,
                "approved_at": _now(),
            },
        )
        done.append("pairing metadata")

        if created_key:
            # Zero-write rule (B4): if B definitively refuses below, the key and
            # metadata THIS run created are removed again. "Created" is captured
            # here so a pre-existing key is never deleted.
            pass

        if options.print_remote_steps:
            # The local half is written so this machine is ready the moment the
            # user runs the printed command there; the remote grant is theirs.
            # (`pubkey` is already generated above when a key was needed.)
            _write_local_half(
                alias=alias,
                destination=destination,
                port=port,
                key_path=key_path,
                token=token,
                principal=principal,
                pubkey=pubkey,
                remote_ccb=remote_ccb,
                pin_keys=verified_keys,
                shares=shares,
                done=done,
            )
            _print_remote_steps(
                alias=alias,
                destination=destination,
                port=port,
                remote_ccb=remote_ccb,
                gate=gate,
                principal=principal,
                token=token,
                pubkey=pubkey,
                key_path=key_path,
                shares=shares,
                inspect_hash=str(inspect_result.get("state_hash") or ""),
                out=out,
            )
            _progress(
                out,
                done,
                ["the remote apply (run the command above on that machine)", "verification"],
                "Then run `ccb-remote list %s` to confirm the connection." % alias,
            )
            # B7: completion is only claimed after hello + list succeed, which
            # a manual run cannot have done yet.
            return EXIT_REMOTE_PENDING

        # B4/B7: apply uses the hash the user APPROVED (the state the diff was
        # rendered from). Re-reading it here would approve a machine the user
        # never saw; if B changed since, its own check refuses with zero writes.
        apply_request = {
            "verb": "apply",
            "principal": principal,
            "pubkey": pubkey,
            "issuer_token": token,
            "aliases": [
                {
                    "alias": share["alias"],
                    "work_dir": share["work_dir"],
                    "providers": list(share["providers"]),
                }
                for share in shares.values()
            ],
            "expected_state_hash": str(inspect_result.get("state_hash") or ""),
        }
        try:
            applied = channel.call("apply", apply_request)
        except PairingError as exc:
            # The connection or the answer was lost: B MAY have written. Keep
            # everything so a retry can finish idempotently.
            _progress(
                err,
                done,
                ["the remote grant", "the local host key pin", "the local host entry"],
                f"Re-run `ccb-remote pair {alias}` once the problem is fixed; it resumes here.",
            )
            raise PairingError(f"the other machine refused or failed the grant: {exc}") from None
        if applied.get("ok") is not True:
            if applied.get("refused") is True and applied.get("stale") is not True:
                # A definitive refusal from B about THIS request, which wrote
                # nothing: do not leave this run's key/metadata behind. A
                # "the machine changed" refusal is different -- the approved
                # scope is still valid, so everything is kept for the retry.
                _discard_this_runs_writes(
                    key_path, created_key, alias, record_before=record_before
                )
                _progress(
                    err,
                    [],
                    ["the remote grant", "the local host key pin", "the local host entry"],
                    f"Nothing was left behind. Fix the problem and run `ccb-remote pair {alias}` again.",
                )
                raise PairingError(
                    f"the other machine refused the grant: {applied.get('error') or 'no reason given'}"
                )
            _progress(
                err,
                done,
                ["the remote grant", "the local host key pin", "the local host entry"],
                f"Re-run `ccb-remote pair {alias}` once the problem is fixed; it resumes here.",
            )
            raise PairingError(
                f"the other machine did not confirm the grant: {applied.get('error') or 'no reason given'}"
            )
        done.append("remote grant")

        _write_local_half(
            alias=alias,
            destination=destination,
            port=port,
            key_path=key_path,
            token=token,
            principal=principal,
            pubkey=pubkey,
            remote_ccb=remote_ccb,
            pin_keys=verified_keys,
            shares=shares,
            done=done,
        )

        # B7: "paired" is only claimed once that machine answers hello + list.
        verified = _verify_and_report(alias, out=out, err=err, example_project=sorted(shares)[0])
        if not verified:
            _progress(
                err,
                done,
                ["verification (hello + list)"],
                f"Start CCB on that machine, then run `ccb-remote list {alias}`.",
            )
            return EXIT_REFUSED
        print("paired.", file=out)
        _progress(out, done, [], "")
        return EXIT_OK
    finally:
        if pin_file is not None:
            try:
                pin_file.unlink()
            except OSError:
                pass


def _precheck_conflicts(
    inspect_result: dict,
    *,
    principal: str,
    token: str,
    pubkey: str,
    shares: dict[str, dict],
) -> str:
    """What `apply` would refuse, checked BEFORE the first local write.

    The remote never reveals another principal's token, so the checks here use
    only what it does say about keys: which principals hold a line at all, and
    which principal(s) the offered key is bound to.
    """
    principals = inspect_result.get("principals") or {}
    bound = [str(name) for name in inspect_result.get("bound_principals") or []]
    line_principals = {str(name) for name in inspect_result.get("key_line_principals") or []}
    if inspect_result.get("key_line_foreign_blob") or (
        inspect_result.get("key_blob_present")
        and principal
        not in {str(name) for name in inspect_result.get("key_line_exact_principals") or []}
    ):
        # This key already lives in a line pairing did not write: `apply` would
        # refuse it, so say it now -- before the first local write.
        return (
            "this key already appears in an authorized_keys line on that machine whose text differs "
            "from what pairing writes; remove that line there first, or use a different key"
        )
    if principal in principals:
        if bound and principal not in bound:
            return (
                f"this key is already bound to another principal ({bound[0]}) on that machine; "
                "use that principal or a different key"
            )
        if principal in line_principals and principal not in bound:
            return (
                f"principal {principal} already holds a different key on that machine; "
                "choose another --principal or remove the old pairing there first"
            )
    elif bound:
        return (
            f"this key is already bound to another principal ({bound[0]}) on that machine; "
            "use that principal or a different key"
        )
    aliases = inspect_result.get("aliases") or {}
    for share in shares.values():
        current = aliases.get(share["alias"])
        if not isinstance(current, dict):
            continue
        if str(current.get("work_dir") or "") != share["work_dir"]:
            return (
                f"alias {share['alias']} already points at a different directory on that machine; "
                "choose another alias"
            )
        current_providers = [str(item) for item in current.get("providers") or []]
        if current_providers and set(current_providers) != set(share["providers"]):
            return (
                f"alias {share['alias']} already exists with different providers on that machine; "
                "match them or choose another alias"
            )
    return ""


def _discard_this_runs_writes(
    key_path: Path,
    created_key: bool,
    alias: str,
    *,
    record_before: bytes | None = None,
) -> None:
    """Remove what THIS run created after a proven B refusal.

    The key and the record are only removed when this run created them. A
    record left by an earlier run is the approved scope a retry resumes from,
    so it is restored to its pre-run content (left alone when this run did not
    change it) rather than deleted.
    """
    if created_key:
        for path in (key_path, key_path.with_name(key_path.name + ".pub")):
            try:
                path.unlink()
            except OSError:
                pass
    record_path = _pairing_record_path(alias)
    if record_before is None:
        try:
            record_path.unlink()
        except OSError:
            pass
        return
    current = record_path.read_bytes() if record_path.is_file() else None
    if current != record_before:
        atomic_write(record_path, record_before, default_mode=0o600)


def _write_local_half(
    *,
    alias: str,
    destination: str,
    port: int,
    key_path: Path,
    token: str,
    principal: str,
    pubkey: str,
    remote_ccb: str,
    pin_keys: list[dict],
    shares: dict[str, dict],
    done: list[str],
) -> None:
    """The local steps, in the ratified order: pin merge, hosts.json, record.

    Both shared files are merged under the pairing lock (B6): another pairing
    may be adding its own pin or host entry at the same moment, and neither
    file may lose the other's work.
    """
    host = pairing_ssh_target_parts(destination)[0]
    known_dir = _pin_path().parent
    os.makedirs(known_dir, mode=0o700, exist_ok=True)
    with pairing_lock(_remote_root()):
        merged = known_hosts_merge(
            read_text_or_empty(_pin_path()), host, port, pin_keys, rotate=True
        )
        atomic_write(_pin_path(), merged.encode("utf-8"), default_mode=0o600)
        done.append(f"pin {_pin_path()}")

        document = _hosts_document()
        document["hosts"][alias] = _hosts_entry(destination, port, key_path, token)
        _write_hosts_document(document)
        done.append(f"hosts.json entry {alias}")

        record = _read_pairing_record(alias)
        record.update(
            {
                "alias": alias,
                "principal": principal,
                "pubkey": pubkey,
                "pubkey_fingerprint": fingerprint(pubkey.split(" ")[1]),
                "key_path": str(key_path),
                "ssh_target": destination,
                "host": host,
                "port": port,
                "remote_ccb": remote_ccb,
                "issuer_token": token,
                "shares": [
                    {
                        "alias": share["alias"],
                        "work_dir": share["work_dir"],
                        "providers": list(share["providers"]),
                    }
                    for share in shares.values()
                ],
                "applied_at": _now(),
            }
        )
        _write_pairing_record(alias, record)
        done.append("pairing record")


def _print_remote_steps(
    *,
    alias: str,
    destination: str,
    port: int,
    remote_ccb: str,
    gate: str,
    principal: str,
    token: str,
    pubkey: str,
    key_path: Path,
    shares: dict[str, dict],
    inspect_hash: str,
    out,
) -> None:
    """The exact data and command to run on B by hand, when A cannot reach it."""
    payload = {
        "verb": "apply",
        "principal": principal,
        "pubkey": pubkey or "<the public key of %s>" % key_path,
        "issuer_token": token,
        "aliases": [
            {
                "alias": share["alias"],
                "work_dir": share["work_dir"],
                "providers": list(share["providers"]),
            }
            for share in shares.values()
        ],
        "expected_state_hash": inspect_hash,
    }
    print("", file=out)
    print("Run this ON the other machine (it applies the same grant):", file=out)
    print("", file=out)
    print(f"  {remote_ccb} pairing apply <<'CCB_PAIRING'", file=out)
    print(json.dumps(payload, indent=2), file=out)
    print("CCB_PAIRING", file=out)
    print("", file=out)
    print(
        f"(the gate there is {gate}; the principal is {principal})", file=out
    )
    print(
        "If that command refuses because the machine's state changed, re-run this "
        "pairing command to print a fresh payload -- nothing is lost.",
        file=out,
    )
    print("", file=out)


def _verify_and_report(
    alias: str, *, out, err, example_project: str = "PROJECT"
) -> bool:
    """Run the real delivery check; True only when that machine answered hello + list."""
    import io

    captured = io.StringIO()
    try:
        result = remote_client.list_projects(alias, out=captured, err=captured)
    except Exception as exc:  # never fail a completed pairing on the check
        print(f"warning: the connection check could not run: {exc}", file=err)
        return False
    if result.exit_code != remote_client.EXIT_ACCEPTED:
        print(
            "warning: the pairing is written, but the other machine did not answer `list` yet "
            f"({result.reason or 'no reason given'}). Start CCB there, then run "
            f"`ccb-remote list {alias}`.",
            file=err,
        )
        return False
    print("", file=out)
    print("That machine answers as:", file=out)
    print(captured.getvalue().rstrip(), file=out)
    # The same section `ccb-list` shows, rendered by the same code, so the user
    # sees exactly what their agents will see.
    view = remote_discovery.cache_view(alias)
    print("", file=out)
    for line in remote_discovery.section_lines([view]):
        print(line, file=out)
    print("", file=out)
    print("Send a message with, for example:", file=out)
    print(f'  ask codex --peer "{alias}::{example_project}" --wait "hello from here"', file=out)
    return True


# --------------------------------------------------------------------------- #
# unpair
# --------------------------------------------------------------------------- #


def unpair(
    options: UnpairOptions,
    *,
    out=sys.stdout,
    err=sys.stderr,
    input_fn: Callable[[str], str] = input,
) -> int:
    try:
        return _unpair(options, out=out, err=err, input_fn=input_fn)
    except PairingError as exc:
        print(f"[ERROR] {exc}", file=err)
        return EXIT_REFUSED


def _unpair(options: UnpairOptions, *, out, err, input_fn) -> int:
    alias = options.alias.strip()
    if not alias:
        raise PairingError("unpair needs the host alias to remove")
    alias = validate_alias(alias, "machine name")
    try:
        hosts = remote_client.load_hosts()
    except remote_client.RemoteConfigError as exc:
        raise PairingError(str(exc)) from None
    entry = hosts.get(alias)
    record = _read_pairing_record(alias)
    if entry is None and not record:
        raise PairingError(f"{alias} is not configured on this machine")

    identity_file = Path(str(record.get("key_path") or "")).expanduser() if record.get("key_path") else (
        Path(str(entry.identity_file)).expanduser() if entry is not None else Path()
    )
    pubkey = public_key_text(identity_file) if identity_file else ""
    principal = options.principal.strip() or str(record.get("principal") or "")
    if not principal:
        raise PairingError(
            "this machine has no record of the principal used for that pairing; pass --principal "
            "so exactly that grant is revoked"
        )
    principal = validate_principal(principal)
    if not pubkey:
        raise PairingError(
            "this machine no longer has that pairing's public key, so its line cannot be matched; "
            "remove the entry on the other machine by hand (see the command below)"
        )
    destination = options.destination.strip() or str(
        record.get("ssh_target") or (entry.ssh_target if entry is not None else "")
    )
    if not destination:
        raise PairingError("no recorded ssh destination for that pairing; pass --destination")
    destination = validate_ssh_target(destination)
    port = validate_port(options.port if options.port != 22 else int(
        record.get("port") or (entry.port if entry is not None else 22)
    ))
    remote_ccb = options.remote_ccb.strip() or str(record.get("remote_ccb") or "") or pairing_remote_ccb_default()

    if not options.yes and not _confirm_for_unpair(alias, input_fn, out):
        print("stopped: nothing was removed.", file=out)
        return EXIT_REFUSED

    explicit_known = (
        Path(options.known_hosts).expanduser() if options.known_hosts.strip() else None
    )
    token = str(record.get("issuer_token") or "") or (
        str(entry.issuer_token) if entry is not None else ""
    )
    if not token:
        raise PairingError(
            "this machine has no record of the issuer token used for that pairing; pass nothing is "
            "safe here -- run the manual revoke below on the other machine instead"
        )
    token = validate_token(token)

    channel = _Channel(
        destination=destination,
        port=port,
        remote_ccb=remote_ccb,
        identity=options.identity.strip(),
    )
    pin = _pin_path()
    channel.known_hosts = pin if pin.is_file() else explicit_known
    revoked: dict = {}
    failure = ""
    try:
        # B5: revoke against the state we just read, and prove ownership by
        # naming the same token; B refuses a principal that belongs elsewhere.
        fresh = channel.call(
            "inspect", {"verb": "inspect", "lookup_principal": principal, "lookup_pubkey": pubkey}
        )
        revoke_request = {
            "verb": "revoke",
            "principal": principal,
            "pubkey": pubkey,
            "issuer_token": token,
            "expected_state_hash": str(fresh.get("state_hash") or ""),
        }
        revoked = channel.call("revoke", revoke_request)
        if revoked.get("ok") is not True:
            failure = str(revoked.get("error") or "the other machine did not confirm the revocation")
    except PairingError as exc:
        failure = str(exc)
        revoke_request = {
            "verb": "revoke",
            "principal": principal,
            "pubkey": pubkey,
            "issuer_token": token,
            "expected_state_hash": "<the hash `pairing inspect` reported>",
        }

    if failure:
        print("remote revocation PENDING:", failure, file=err)
        print("", file=err)
        print("The grant on the other machine is still in place. Remove it with:", file=err)
        print("", file=err)
        print(f"  {remote_ccb} pairing revoke <<'CCB_PAIRING'", file=err)
        print(json.dumps(revoke_request, indent=2), file=err)
        print("CCB_PAIRING", file=err)
        print("", file=err)
        if not options.local_only:
            print(
                "Nothing local was removed, so this machine cannot reach the machine again either. "
                "Re-run with --local-only to forget it here while the remote grant is still pending.",
                file=err,
            )
            return EXIT_REMOTE_PENDING

    removed = _remove_local_pairing(alias, entry, identity_file)
    if failure:
        print("local pairing removed: " + ", ".join(removed), file=out)
        print(
            "remote revocation is still PENDING; run the command above on that machine.",
            file=err,
        )
        return EXIT_REMOTE_PENDING
    print("unpaired: " + ", ".join(removed), file=out)
    if revoked.get("aliases_kept"):
        print(
            "kept on that machine (shared aliases): " + ", ".join(revoked["aliases_kept"]),
            file=out,
        )
    return EXIT_OK


def _confirm_for_unpair(alias: str, input_fn, out) -> bool:
    print(f"Remove the pairing with {alias} on THIS machine and revoke its grant on that machine?", file=out)
    return _confirm(input_fn, out)


def _resolved_path(value) -> str:
    """A comparable absolute path for a value that may be relative or `~/...`."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except OSError:
        return text


def _pin_endpoint(entry: dict) -> tuple[str, int, str]:
    """(host, port, pin file) for a host entry -- NOT the ssh user.

    One user@host and another@host reach the same pinned key, so the endpoint
    is the machine, not the account used to log in.
    """
    raw = str(entry.get("ssh_target") or "")
    try:
        host = pairing_ssh_target_parts(raw)[0] if raw else ""
    except PairingError:
        host = ""
    try:
        port = int(entry.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    return host, port, _resolved_path(entry.get("known_hosts") or _pin_path())


def _remove_local_pairing(alias: str, entry, identity_file: Path) -> list[str]:
    """Forget this pairing locally: host entry, pin lines, key, cache, record.

    Two things are shared in practice and are therefore kept: a key file that
    another host entry still points at (one key may serve several aliases), and
    a pinned host key for an endpoint that another entry still uses.

    Both shared files are read-modified-written in ONE critical section under
    the same lock `pair` takes (B6), re-reading them inside it, so a concurrent
    pairing can never lose this removal (or its own write to ours).
    """
    removed: list[str] = []
    pin_path = _pin_path()
    key_wanted = _resolved_path(identity_file)
    with pairing_lock(_remote_root()):
        document = _hosts_document()
        hosts = document.get("hosts") if isinstance(document.get("hosts"), dict) else {}
        others = {
            name: value
            for name, value in hosts.items()
            if name != alias and isinstance(value, dict)
        }
        if alias in hosts:
            hosts.pop(alias)
            _write_hosts_document(document)
            removed.append(f"hosts.json entry {alias}")

        # Read the record INSIDE the lock: `pair` may be writing it right now.
        record = _read_pairing_record(alias)
        destination = str(entry.ssh_target) if entry is not None else ""
        port = int(entry.port) if entry is not None else 22
        if record:
            destination = str(record.get("ssh_target") or destination)
            port = int(record.get("port") or port)

        key_in_use = bool(key_wanted) and any(
            _resolved_path(value.get("identity_file")) == key_wanted
            for value in others.values()
        )
        if destination:
            host = pairing_ssh_target_parts(destination)[0]
            endpoint = (host, port, _resolved_path(pin_path))
            # A pin is shared only by another entry for the SAME machine, on
            # the SAME port, reading the SAME pin file. How that entry logs in
            # is irrelevant, and a key someone else uses says nothing about it.
            endpoint_in_use = any(
                _pin_endpoint(value) == endpoint for value in others.values()
            )
            if not endpoint_in_use:
                text = read_text_or_empty(pin_path)
                merged, count = known_hosts_remove(text, host, port)
                if count:
                    atomic_write(pin_path, merged.encode("utf-8"), default_mode=0o600)
                    removed.append(f"{count} pinned host key line(s) for {host}")

        if key_in_use:
            removed.append(f"(kept {identity_file}: another host entry uses it)")
        else:
            for path in (identity_file, identity_file.with_name(identity_file.name + ".pub")):
                if path and path.is_file():
                    try:
                        path.unlink()
                        removed.append(str(path))
                    except OSError:
                        pass

            cache = remote_discovery.cache_path(alias)
            if cache.is_file():
                try:
                    cache.unlink()
                    removed.append(str(cache))
                except OSError:
                    pass

        record_path = _pairing_record_path(alias)
        if record_path.is_file():
            try:
                record_path.unlink()
                removed.append(str(record_path))
            except OSError:
                pass
    return removed


__all__ = [
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_REMOTE_PENDING",
    "PairOptions",
    "UnpairOptions",
    "pair",
    "unpair",
    "resolve_shares",
    "public_key_text",
    "generate_key",
]
