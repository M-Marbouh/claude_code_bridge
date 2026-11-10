from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

import remote_protocol as proto

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "bin" / "ccb-remote-gate"

#: A run dir with no askd state, so "no destination daemon" is deterministic.
GATE_TEST_RUN_DIR = Path(tempfile.mkdtemp(prefix="ccb-gate-run-"))


def _envelope(op: str = "hello", **overrides) -> dict:
    envelope = {
        "proto": {"major": 1, "minor": 0},
        "op": op,
        "op_id": "op-1",
        "body": {},
    }
    envelope.update(overrides)
    return envelope


def _frame(value: dict) -> bytes:
    return proto.encode_frame(value)


def _env(**overrides) -> dict:
    env = {key: value for key, value in os.environ.items() if key != "SSH_ORIGINAL_COMMAND"}
    env.update(overrides)
    return env


def _run(args: list[str], payload: bytes = b"", *, env: dict | None = None):
    return subprocess.run(
        [sys.executable, str(GATE), *args],
        input=payload,
        capture_output=True,
        env=env if env is not None else _env(),
        timeout=60,
    )


#: Saved at import time so a monkeypatched ``proto.read_frame`` cannot break
#: the test helper that decodes the gate's own replies.
_READ_FRAME = proto.read_frame


def _responses(stdout: bytes) -> list[dict]:
    stream = io.BytesIO(stdout)
    frames = []
    while True:
        frame = _READ_FRAME(stream)
        if frame is None:
            return frames
        frames.append(frame)


def _run_without_writing_stdin(args: list[str], *, env: dict):
    """Start the gate, keep stdin open, and wait for it to exit on its own.

    A gate that reads stdin before refusing would block here and time out, so
    success proves the refusal happens before any stdin read.
    """
    proc = subprocess.Popen(
        [sys.executable, str(GATE), *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        code = proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    stdout = proc.stdout.read()
    stderr = proc.stderr.read()
    proc.stdout.close()
    proc.stderr.close()
    try:
        proc.stdin.close()
    except Exception:
        pass
    return code, stdout, stderr


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_hello_answered_locally_then_clean_eof():
    result = _run(["--principal", "bob"], _frame(_envelope()))
    assert result.returncode == 0, result.stderr
    frames = _responses(result.stdout)
    assert len(frames) == 1
    assert frames[0]["ok"] is True
    assert frames[0]["op"] == "hello"
    assert frames[0]["op_id"] == "op-1"
    assert frames[0]["capabilities"] == proto.capabilities()
    assert b"principal=bob" in result.stderr
    assert b"op=hello" in result.stderr
    assert b"outcome=ok" in result.stderr


def test_gate_loops_over_frames_and_stops_on_eof():
    payload = _frame(_envelope()) + _frame(_envelope(op="list", op_id="op-2"))
    result = _run(["--principal", "bob"], payload, env=_env(CCB_RUN_DIR=str(GATE_TEST_RUN_DIR)))
    assert result.returncode == 0, result.stderr
    frames = _responses(result.stdout)
    assert [frame["op_id"] for frame in frames] == ["op-1", "op-2"]
    assert frames[0]["ok"] is True
    # No destination daemon is running in this isolated run dir.
    assert frames[1]["error"]["code"] == proto.ERR_DAEMON_UNAVAILABLE


@pytest.mark.parametrize("verb", ["list", "submit", "status", "fetch"])
def test_forwarded_verbs_without_a_daemon_refuse_honestly(verb: str):
    result = _run(["--principal", "bob"], _frame(_envelope(op=verb)), env=_env(CCB_RUN_DIR=str(GATE_TEST_RUN_DIR)))
    assert result.returncode == 0, result.stderr
    frames = _responses(result.stdout)
    assert len(frames) == 1
    assert frames[0]["ok"] is False
    assert frames[0]["error"]["code"] == proto.ERR_DAEMON_UNAVAILABLE
    assert frames[0]["op"] == verb


def test_empty_ssh_original_command_is_allowed():
    result = _run(["--principal", "bob"], _frame(_envelope()), env=_env(SSH_ORIGINAL_COMMAND=""))
    assert result.returncode == 0, result.stderr
    assert _responses(result.stdout)[0]["ok"] is True


# --------------------------------------------------------------------------- #
# Refusals before reading stdin
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--principal"],
        ["--principal", "bob", "extra"],
        ["--principal", "bob", "--principal", "eve"],
        ["--principals", "bob"],
        ["--principal", "Bob"],
        ["--principal", ""],
        ["--principal", "b" * 65],
        ["--principal", "bo b"],
        ["--principal", "bob", "-h"],
    ],
)
def test_bad_argv_refused_before_reading_stdin(args: list[str]):
    code, stdout, stderr = _run_without_writing_stdin(args, env=_env())
    assert code == 2
    assert stdout == b""
    assert b"usage" in stderr


def test_ssh_original_command_refused_before_reading_stdin():
    for command in ("ls", "sh -c id", "scp -t /tmp/x", " "):
        code, stdout, stderr = _run_without_writing_stdin(
            ["--principal", "bob"], env=_env(SSH_ORIGINAL_COMMAND=command)
        )
        assert code == 2
        assert stdout == b""
        assert b"refused-original-command" in stderr
        if command.strip():
            assert command.strip().encode() not in stderr


def test_good_argv_within_the_same_env_does_answer():
    """Positive control for the argv/SSH_ORIGINAL_COMMAND refusals above."""
    result = _run(["--principal", "bob"], _frame(_envelope()))
    assert result.returncode == 0, result.stderr
    assert _responses(result.stdout)[0]["ok"] is True


# --------------------------------------------------------------------------- #
# Frame-level refusals write an error frame
# --------------------------------------------------------------------------- #


def test_truncated_body_refused_with_error_frame_and_positive_control():
    good = _frame(_envelope(body={"text": "x" * 40}))
    result = _run(["--principal", "bob"], good[:-5])
    assert result.returncode == 2
    frames = _responses(result.stdout)
    assert frames[-1]["ok"] is False
    assert frames[-1]["error"]["code"] == proto.ERR_MALFORMED_FRAME
    control = _run(["--principal", "bob"], good)
    assert control.returncode == 0
    assert _responses(control.stdout)[0]["ok"] is True


def test_zero_length_frame_refused_with_positive_control():
    result = _run(["--principal", "bob"], b"\x00\x00\x00\x00")
    assert result.returncode == 2
    assert _responses(result.stdout)[-1]["error"]["code"] == proto.ERR_EMPTY_FRAME
    assert _run(["--principal", "bob"], _frame(_envelope())).returncode == 0


def test_oversized_length_prefix_refused_with_positive_control():
    result = _run(["--principal", "bob"], struct.pack(">I", proto.MAX_FRAME + 1))
    assert result.returncode == 2
    assert _responses(result.stdout)[-1]["error"]["code"] == proto.ERR_FRAME_TOO_LARGE
    control = _run(["--principal", "bob"], _frame(_envelope()))
    assert control.returncode == 0
    assert _responses(control.stdout)[0]["ok"] is True


def test_invalid_json_refused_with_positive_control():
    payload = b'{"a":}'
    payload = struct.pack(">I", len(payload)) + payload
    result = _run(["--principal", "bob"], payload)
    assert result.returncode == 2
    assert _responses(result.stdout)[-1]["error"]["code"] == proto.ERR_INVALID_JSON
    assert _run(["--principal", "bob"], _frame(_envelope())).returncode == 0


def test_duplicate_key_refused_with_positive_control():
    payload = b'{"proto":{"major":1,"minor":0},"op":"hello","op_id":"op-1","op":"hello"}'
    result = _run(["--principal", "bob"], struct.pack(">I", len(payload)) + payload)
    assert result.returncode == 2
    assert _responses(result.stdout)[-1]["error"]["code"] == proto.ERR_DUPLICATE_KEY
    assert _run(["--principal", "bob"], _frame(_envelope())).returncode == 0


def test_json_array_refused_with_positive_control():
    payload = b"[1,2]"
    result = _run(["--principal", "bob"], struct.pack(">I", len(payload)) + payload)
    assert result.returncode == 2
    assert _responses(result.stdout)[-1]["error"]["code"] == proto.ERR_NOT_OBJECT
    assert _run(["--principal", "bob"], _frame(_envelope())).returncode == 0


# --------------------------------------------------------------------------- #
# Envelope-level refusals keep the loop alive
# --------------------------------------------------------------------------- #


def test_major_mismatch_refused_with_error_frame_and_positive_control():
    bad = _frame(_envelope(proto={"major": 2, "minor": 0}))
    result = _run(["--principal", "bob"], bad + _frame(_envelope(op="hello", op_id="op-2")))
    assert result.returncode == 0, result.stderr
    frames = _responses(result.stdout)
    assert frames[0]["error"]["code"] == proto.ERR_PROTOCOL_VERSION
    assert frames[1]["ok"] is True  # stream stayed usable; the control proves it


def test_unknown_mandatory_field_refused_with_positive_control():
    bad = _frame(_envelope(future_field=1, mandatory=["future_field"]))
    result = _run(["--principal", "bob"], bad)
    assert result.returncode == 0, result.stderr
    frames = _responses(result.stdout)
    assert frames[0]["error"]["code"] == proto.ERR_UNKNOWN_MANDATORY_FIELD
    control = _run(["--principal", "bob"], _frame(_envelope(mandatory=["proto"])))
    assert _responses(control.stdout)[0]["ok"] is True


# --------------------------------------------------------------------------- #
# The gate is not a shell and does not log message text
# --------------------------------------------------------------------------- #


def test_message_text_is_never_logged():
    secret = "SECRET-MARKER-1234"
    intent = "INTENT-MARKER-5678"
    request = {
        "proto": {"major": 1, "minor": 0},
        "op": "submit",
        "op_id": "op-9",
        "body": {"text": f"{secret}\nsecond line", "intent": intent},
    }
    result = _run(["--principal", "bob"], _frame(request), env=_env(CCB_RUN_DIR=str(GATE_TEST_RUN_DIR)))
    assert result.returncode == 0, result.stderr
    assert _responses(result.stdout)[0]["error"]["code"] == proto.ERR_DAEMON_UNAVAILABLE
    # Positive control: the log line for this op exists, so the absence of the
    # text below is meaningful rather than a silent no-op.
    assert b"op=submit" in result.stderr
    assert b"op_id=op-9" in result.stderr
    combined = result.stdout + result.stderr
    assert secret.encode() not in combined
    assert intent.encode() not in combined


def test_gate_source_never_spawns_or_evaluates():
    source = GATE.read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "os.system" not in source
    assert "popen" not in source.lower()
    assert not re.search(r"\beval\(", source)
    assert not re.search(r"\bexec\(", source)
    assert not re.search(r"(?<![\w.])compile\(", source)
    assert "shell=True" not in source


def test_gate_is_an_executable_python_script():
    assert GATE.exists()
    assert os.access(GATE, os.X_OK)
    first_line = GATE.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "#!/usr/bin/env python3"


def test_gate_imports_only_stdlib_and_the_protocol_module():
    source = GATE.read_text(encoding="utf-8")
    imports = re.findall(r"^(?:from|import)\s+([A-Za-z0-9_.]+)", source, re.MULTILINE)
    allowed = {"os", "re", "sys", "pathlib", "remote_protocol", "__future__"}
    assert set(imports) <= allowed, imports


# --------------------------------------------------------------------------- #
# Parser-hardening regressions found by probing the gate as a subprocess
# --------------------------------------------------------------------------- #


def _no_traceback(stderr: bytes) -> None:
    text = stderr.decode("utf-8", "replace")
    assert "Traceback" not in text
    assert 'File "' not in text
    assert "RecursionError" not in text
    assert "internal error" not in text.replace("outcome=internal-error", "")


def test_deeply_nested_frame_refused_without_traceback():
    payload = b"[" * 200_000
    assert len(payload) < proto.MAX_FRAME
    result = _run(["--principal", "bob"], struct.pack(">I", len(payload)) + payload)
    assert result.returncode == 2
    frames = _responses(result.stdout)
    assert len(frames) == 1
    assert frames[0] == {
        "ok": False,
        "error": {"code": proto.ERR_INVALID_JSON, "message": frames[0]["error"]["message"]},
    }
    _no_traceback(result.stderr)
    # Control: shallow nesting inside an object is accepted by the same gate.
    nested = b'{"proto":{"major":1,"minor":0},"op":"hello","op_id":"op-1","body":{"a":' + b"[" * 50 + b"1" + b"]" * 50 + b"}}"
    control = _run(["--principal", "bob"], struct.pack(">I", len(nested)) + nested)
    assert control.returncode == 0, control.stderr
    assert _responses(control.stdout)[0]["ok"] is True


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_standard_constant_refused_without_traceback(constant: str):
    body = ('{"proto":{"major":1,"minor":0},"op":"hello","op_id":"op-1","body":{"a":'
            + constant
            + "}}").encode()
    result = _run(["--principal", "bob"], struct.pack(">I", len(body)) + body)
    assert result.returncode == 2
    assert _responses(result.stdout)[-1]["error"]["code"] == proto.ERR_INVALID_JSON
    _no_traceback(result.stderr)
    # Control: the same frame with a standard number is answered as hello.
    control_body = body.replace(constant.encode(), b"1.0")
    control = _run(["--principal", "bob"], struct.pack(">I", len(control_body)) + control_body)
    assert control.returncode == 0, control.stderr
    assert _responses(control.stdout)[0]["ok"] is True


def _gate_module():
    loader = importlib.machinery.SourceFileLoader("ccb_remote_gate_under_test", str(GATE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _FakeIn:
    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)


class _FakeOut:
    def __init__(self):
        self.buffer = io.BytesIO()


def test_injected_dispatch_exception_yields_internal_error_without_traceback(
    monkeypatch, capsys
):
    gate = _gate_module()
    secret = "SECRET-INTERNAL-DETAIL"

    def boom(envelope, *, principal):
        raise RuntimeError(f"{secret} at /home/someone/ccb")

    monkeypatch.setattr(gate, "handle_envelope", boom)
    stdin = _FakeIn(_frame(_envelope(op="list", op_id="op-7")))
    stdout = _FakeOut()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    code = gate.main(["ccb-remote-gate", "--principal", "bob"])

    assert code == 2
    frames = _responses(stdout.buffer.getvalue())
    assert len(frames) == 1
    assert frames[0]["ok"] is False
    assert frames[0]["error"]["code"] == proto.ERR_INTERNAL_ERROR
    assert frames[0]["error"]["message"] == "internal error"
    assert proto.ERR_INTERNAL_ERROR in proto.ERROR_CODES

    captured = capsys.readouterr()
    assert "outcome=internal-error" in captured.err
    assert "principal=bob" in captured.err
    assert "op=list" in captured.err
    assert "op_id=op-7" in captured.err
    assert secret not in captured.err
    assert "Traceback" not in captured.err
    assert "/home/someone" not in captured.err


def test_injected_reader_exception_yields_internal_error_without_traceback(monkeypatch, capsys):
    gate = _gate_module()
    secret = "SECRET-READER-DETAIL"

    def boom(stream):
        raise RuntimeError(secret)

    monkeypatch.setattr(gate.proto, "read_frame", boom)
    stdout = _FakeOut()
    monkeypatch.setattr(sys, "stdin", _FakeIn(b""))
    monkeypatch.setattr(sys, "stdout", stdout)

    assert gate.main(["ccb-remote-gate", "--principal", "bob"]) == 2
    frames = _responses(stdout.buffer.getvalue())
    assert frames[0]["error"]["code"] == proto.ERR_INTERNAL_ERROR
    captured = capsys.readouterr()
    assert "outcome=internal-error" in captured.err
    assert secret not in captured.err
    assert "Traceback" not in captured.err


def test_dispatch_guard_keeps_healthy_frames_working():
    """Positive control for the internal-error guard: normal dispatch still answers."""
    result = _run(
        ["--principal", "bob"],
        _frame(_envelope()) + _frame(_envelope(op="submit")),
        env=_env(CCB_RUN_DIR=str(GATE_TEST_RUN_DIR)),
    )
    assert result.returncode == 0, result.stderr
    frames = _responses(result.stdout)
    assert frames[0]["ok"] is True
    assert frames[1]["error"]["code"] == proto.ERR_DAEMON_UNAVAILABLE
    assert b"internal-error" not in result.stderr


# --------------------------------------------------------------------------- #
# Review fix: whole-string principal check; an explicit JSON object body
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("principal", ["bob\n", "bob\r\n", "\nbob", "bob\t"])
def test_principal_with_a_trailing_newline_is_refused_before_reading_stdin(principal: str):
    code, stdout, stderr = _run_without_writing_stdin(
        ["--principal", principal], env=_env()
    )
    assert code == 2
    assert stdout == b""
    assert b"usage" in stderr
    # Positive control: the clean name on the same path still answers hello.
    result = _run(["--principal", "bob"], _frame(_envelope()))
    assert result.returncode == 0, result.stderr
    assert _responses(result.stdout)[0]["ok"] is True


def test_envelope_body_missing_or_null_is_refused_without_dispatch(monkeypatch):
    gate = _gate_module()
    dispatched: list[dict] = []

    def fake_dispatch(envelope, *, principal):
        dispatched.append(envelope)
        return {"ok": True, "op": envelope.get("op"), "op_id": envelope.get("op_id")}

    monkeypatch.setattr(gate, "dispatch_remote_operation", fake_dispatch)

    def run(payload: bytes) -> tuple[int, list[dict]]:
        stdout = _FakeOut()
        monkeypatch.setattr(sys, "stdin", _FakeIn(payload))
        monkeypatch.setattr(sys, "stdout", stdout)
        code = gate.main(["ccb-remote-gate", "--principal", "bob"])
        return code, _responses(stdout.buffer.getvalue())

    for envelope in (
        {"proto": {"major": 1, "minor": 0}, "op": "list", "op_id": "op-1"},
        {"proto": {"major": 1, "minor": 0}, "op": "list", "op_id": "op-1", "body": None},
    ):
        code, frames = run(_frame(envelope))
        assert code == 0  # envelope-level refusal, then clean EOF
        assert len(frames) == 1
        assert frames[0]["error"]["code"] == proto.ERR_INVALID_REQUEST
        assert frames[0]["op"] == "list"
    assert dispatched == [], "a body-less frame must never reach dispatch"

    # Positive control: an explicit empty object does dispatch.
    code, frames = run(_frame(_envelope(op="list")))
    assert code == 0
    assert frames[0]["ok"] is True
    assert len(dispatched) == 1


def test_body_less_frame_is_refused_by_the_real_gate_subprocess():
    envelope = {"proto": {"major": 1, "minor": 0}, "op": "list", "op_id": "op-1"}
    result = _run(["--principal", "bob"], _frame(envelope), env=_env(CCB_RUN_DIR=str(GATE_TEST_RUN_DIR)))
    assert result.returncode == 0, result.stderr
    [frame] = _responses(result.stdout)
    assert frame["error"]["code"] == proto.ERR_INVALID_REQUEST
