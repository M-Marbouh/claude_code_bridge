from __future__ import annotations

import io
import json
import re
import struct
from pathlib import Path

import pytest

import remote_protocol as proto

REAL_LOCAL_ID_ASK = "20261109-182536-922-2626995"
REAL_LOCAL_ID_REQ_ID = "20260125-143000-123-12345-0"

#: Real message text sent through ``ask`` on 2026-11-09; kept byte-for-byte
#: (em-dash, "->", newlines, quotes) as the brief requires.
REAL_MESSAGE = (
    "ratifier: Plan ratification request — Cross-machine CCB Milestone 1 (one-way delivery).\n\n"
    "Claim: the frozen Oct-3 design (docs/plans/cross-machine-ccb-proposal.md) can start with an "
    "even thinner first milestone than the one the user named: A->B delivery-only "
    "(reply_expected=no, terminal state 'delivered')"
)


def _envelope(op: str = "hello", **overrides) -> dict:
    envelope = {
        "proto": {"major": 1, "minor": 0},
        "op": op,
        "op_id": "op-1",
        "body": {},
    }
    envelope.update(overrides)
    return envelope


def _frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def _read(data: bytes):
    return proto.read_frame(io.BytesIO(data))


def _refused(data: bytes, code: str) -> proto.ProtocolError:
    with pytest.raises(proto.ProtocolError) as excinfo:
        _read(data)
    assert excinfo.value.code == code
    return excinfo.value


def _record(**overrides) -> dict:
    kwargs = dict(
        dest_project="example-project",
        dest_provider="claude",
        intent="notify",
        text="hello there",
        request_id=proto.make_remote_id(REAL_LOCAL_ID_ASK, "abc1"),
    )
    kwargs.update(overrides)
    return proto.build_request_record(**kwargs)


def _padded_envelope(size: int) -> bytes:
    """A valid hello envelope whose JSON payload is exactly ``size`` bytes."""
    base = _envelope(pad="")
    empty = proto.encode_frame(base)
    pad = "x" * (size - (len(empty) - proto.HEADER_SIZE))
    encoded = proto.encode_frame(_envelope(pad=pad))
    assert len(encoded) == proto.HEADER_SIZE + size
    return encoded


# --------------------------------------------------------------------------- #
# Framing
# --------------------------------------------------------------------------- #


def test_frame_round_trip_preserves_text_and_unicode():
    envelope = _envelope(body={"text": REAL_MESSAGE})
    stream = io.BytesIO()
    proto.write_frame(stream, envelope)
    stream.seek(0)
    assert proto.read_frame(stream) == envelope
    assert proto.read_frame(stream) is None  # clean EOF


def test_encode_frame_is_length_prefixed_compact_utf8():
    encoded = proto.encode_frame(_envelope(body={"text": "é"}))
    length = struct.unpack(">I", encoded[:4])[0]
    payload = encoded[4:]
    assert length == len(payload)
    assert payload == '{"proto":{"major":1,"minor":0},"op":"hello","op_id":"op-1","body":{"text":"é"}}'.encode(
        "utf-8"
    )


def test_exact_max_frame_is_accepted_and_one_more_byte_is_refused():
    accepted = _read(_padded_envelope(proto.MAX_FRAME))
    assert isinstance(accepted, dict)
    assert len(accepted["pad"]) > 0
    _refused(struct.pack(">I", proto.MAX_FRAME + 1), proto.ERR_FRAME_TOO_LARGE)


def test_short_length_prefix_refused_with_positive_control():
    good = proto.encode_frame(_envelope())
    _refused(good[:2], proto.ERR_MALFORMED_FRAME)
    assert _read(good) == _envelope()


def test_zero_length_refused_with_positive_control():
    _refused(b"\x00\x00\x00\x00", proto.ERR_EMPTY_FRAME)
    assert _read(proto.encode_frame(_envelope())) == _envelope()


def test_truncated_body_refused_with_positive_control():
    good = proto.encode_frame(_envelope(body={"text": REAL_MESSAGE}))
    _refused(good[:-3], proto.ERR_MALFORMED_FRAME)
    assert _read(good)["body"]["text"] == REAL_MESSAGE


def test_invalid_utf8_refused_with_positive_control():
    _refused(_frame(b"\xff\xfe\xfd"), proto.ERR_INVALID_UTF8)
    assert _read(_frame(b'{"a":1}')) == {"a": 1}


def test_json_array_refused_with_positive_control():
    _refused(_frame(b"[1, 2]"), proto.ERR_NOT_OBJECT)
    assert _read(_frame(b'{"a":1}')) == {"a": 1}


def test_invalid_json_refused_with_positive_control():
    _refused(_frame(b'{"a":}'), proto.ERR_INVALID_JSON)
    assert _read(_frame(b'{"a":1}')) == {"a": 1}


def test_duplicate_key_refused_at_any_depth_with_positive_control():
    _refused(_frame(b'{"a":1,"a":2}'), proto.ERR_DUPLICATE_KEY)
    _refused(_frame(b'{"a":{"b":1,"b":2}}'), proto.ERR_DUPLICATE_KEY)
    assert _read(_frame(b'{"a":{"b":1}}')) == {"a": {"b": 1}}


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #


def test_major_mismatch_refused_with_positive_control():
    bad = _envelope(proto={"major": 2, "minor": 0})
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.validate_envelope(bad)
    assert excinfo.value.code == proto.ERR_PROTOCOL_VERSION
    assert proto.validate_envelope(_envelope()) is not None


def test_higher_minor_and_unknown_optional_fields_are_accepted():
    envelope = _envelope(
        proto={"major": 1, "minor": 7},
        future_field={"anything": [1, 2, 3]},
        body={"text": REAL_MESSAGE},
    )
    assert proto.validate_envelope(envelope) is envelope


def test_unknown_mandatory_field_refused_with_positive_control():
    bad = _envelope(future_field=True, mandatory=["future_field"])
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.validate_envelope(bad)
    assert excinfo.value.code == proto.ERR_UNKNOWN_MANDATORY_FIELD
    # Control: the same frame with a field this build understands in the list.
    assert proto.validate_envelope(_envelope(mandatory=["proto", "body"])) is not None
    assert proto.validate_envelope(_envelope()) is not None


def test_unknown_op_refused_and_known_verbs_accepted():
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.validate_envelope(_envelope(op="cancel"))
    assert excinfo.value.code == proto.ERR_UNKNOWN_OP
    for verb in proto.VERBS:
        assert proto.validate_envelope(_envelope(op=verb)) is not None
    # M2 adds the pull-based fetch verb; it is envelope-valid like the rest.
    assert "fetch" in proto.VERBS


def test_bad_op_id_and_non_object_body_refused():
    with pytest.raises(proto.ProtocolError):
        proto.validate_envelope(_envelope(op_id=""))
    with pytest.raises(proto.ProtocolError):
        proto.validate_envelope(_envelope(body=[1]))
    assert proto.validate_envelope(_envelope(body={"a": 1})) is not None


def test_hello_advertises_delivery_only_and_explicit_reply():
    response = proto.hello_response("op-42")
    assert response["ok"] is True
    assert response["op"] == "hello"
    assert response["op_id"] == "op-42"
    assert response["capabilities"] == {
        "verbs": ["hello", "list", "submit", "status", "fetch"],
        "reply_modes": {
            "claude": ["delivery-only", "explicit-reply"],
            "codex": ["delivery-only", "explicit-reply"],
        },
        "max_frame": 256 * 1024,
        "proto": {"major": 1, "minor": 1},
        # M3: `list` replies also carry per-provider availability and the
        # live-session choices; a sender that predates this ignores the key.
        "list_sessions": True,
    }
    # No provider beyond Claude/Codex may be advertised.
    assert set(response["capabilities"]["reply_modes"]) == {"claude", "codex"}


def test_m1_delivery_only_is_still_advertised_with_positive_control():
    # The shipped M1 source refuses to submit unless `delivery-only` is
    # advertised for the provider; M2 must keep advertising it.
    capabilities = proto.capabilities()
    for provider in ("claude", "codex"):
        assert proto.advertises(capabilities, provider, proto.DELIVERY_ONLY_MODE)
        assert proto.advertises(capabilities, provider, proto.EXPLICIT_REPLY_MODE)
    # Negative control: capability tables are provider-scoped, never global.
    assert not proto.advertises(capabilities, "gemini", proto.DELIVERY_ONLY_MODE)
    assert not proto.advertises({"reply_modes": {}}, "claude", proto.DELIVERY_ONLY_MODE)
    assert proto.advertises(
        {"reply_modes": {"claude": ["delivery-only"]}},
        "claude",
        proto.DELIVERY_ONLY_MODE,
    )


def test_error_frames_use_enumerated_codes():
    frame = proto.error_frame(proto.ERR_UNSUPPORTED, "nope", op_id="op-1", op="list")
    assert frame == {
        "ok": False,
        "error": {"code": "unsupported", "message": "nope"},
        "op": "list",
        "op_id": "op-1",
    }
    for code in proto.ERROR_CODES:
        assert proto.error_frame(code, "x")["error"]["code"] == code
    with pytest.raises(proto.ProtocolError):
        proto.error_frame("not_a_code", "x")


# --------------------------------------------------------------------------- #
# Host-qualified ids
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("local_id", [REAL_LOCAL_ID_ASK, REAL_LOCAL_ID_REQ_ID])
def test_real_local_ids_round_trip(local_id: str):
    remote = proto.make_remote_id(local_id, "abc1")
    assert remote == f"{local_id}-habc1"
    assert proto.parse_remote_id(remote) == (local_id, "abc1")


def test_parse_remote_id_from_brief_fixture():
    assert proto.parse_remote_id("20261109-182536-922-2626995-habc1") == (
        "20261109-182536-922-2626995",
        "abc1",
    )


def test_at_most_one_separator_and_never_a_greedy_local_part():
    # ``parse_remote_id`` splits on the LAST "-h". Under strict validation of
    # both halves this is equivalent to splitting on the first one: a second
    # separator cannot land in the local part (digits only) or in the token,
    # so the id shape is unambiguous and a greedy local part never matches.
    with pytest.raises(proto.ProtocolError):
        proto.parse_remote_id("20261109-182536-922-2626995-habc1-hdef2")
    assert proto.parse_remote_id("20261109-182536-922-2626995-habc1")[1] == "abc1"


def test_bad_token_chars_refused_with_positive_control():
    for token in ("ABC1", "abc", "abc 1", "abc1!", "a" * 33, ""):
        with pytest.raises(proto.ProtocolError):
            proto.make_remote_id(REAL_LOCAL_ID_ASK, token)
    assert proto.make_remote_id(REAL_LOCAL_ID_ASK, "abc1").endswith("-habc1")


def test_id_with_trailing_garbage_refused_with_positive_control():
    for bad in (
        "20261109-182536-922-2626995-habc1 ",
        "20261109-182536-922-2626995-habc1!",
        "x20261109-182536-922-2626995-habc1",
        "20261109-182536-922-2626995-",
        "20261109-182536-922-2626995",
    ):
        with pytest.raises(proto.ProtocolError):
            proto.parse_remote_id(bad)
    assert proto.parse_remote_id("20261109-182536-922-2626995-habc1")[1] == "abc1"


def test_local_only_id_is_not_a_remote_id():
    with pytest.raises(proto.ProtocolError):
        proto.parse_remote_id(REAL_LOCAL_ID_REQ_ID)
    assert proto.parse_remote_id(proto.make_remote_id(REAL_LOCAL_ID_REQ_ID, "tok9")) == (
        REAL_LOCAL_ID_REQ_ID,
        "tok9",
    )


# --------------------------------------------------------------------------- #
# Submit record + digest
# --------------------------------------------------------------------------- #


def test_build_request_record_shape_and_digest():
    record = _record()
    assert record["schema_version"] == proto.SCHEMA_VERSION == 1
    assert record["reply_expected"] is False
    assert record["dest_provider"] == "claude"
    assert "dest_session" not in record
    assert proto._DIGEST_RE.match(record["digest"])
    proto.verify_digest(record)
    # Round-trips through the wire without changing the digest.
    decoded = proto.read_frame(io.BytesIO(proto.encode_frame(record)))
    assert proto.compute_digest(decoded) == record["digest"]


def test_digest_stable_across_key_order():
    first = _record(text=REAL_MESSAGE)
    reordered = {key: first[key] for key in reversed(list(first))}
    assert list(first) != list(reordered)
    assert proto.compute_digest(first) == proto.compute_digest(reordered)
    assert proto.canonical_json(reordered) == proto.canonical_json(first)


def test_digest_ignores_source_claims_and_op_id():
    first = _record(source_claims={"sender": "a", "note": "one"})
    second = _record(source_claims={"sender": "b"})
    assert first["digest"] == second["digest"]
    with_op_id = dict(first, op_id="op-17")
    assert proto.compute_digest(with_op_id) == first["digest"]


def test_digest_changes_when_text_target_or_session_changes():
    base = _record()
    assert _record(text="hello there!")["digest"] != base["digest"]
    assert _record(dest_project="other-project")["digest"] != base["digest"]
    assert _record(dest_session="sess-1")["digest"] != base["digest"]
    assert _record(dest_provider="codex")["digest"] != base["digest"]
    assert _record(intent="other")["digest"] != base["digest"]


def test_digest_mismatch_is_refused_with_positive_control():
    record = _record()
    proto.verify_digest(record)
    tampered = dict(record, text="hello there, but tampered")
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.verify_digest(tampered)
    assert excinfo.value.code == proto.ERR_DIGEST_MISMATCH
    assert proto.verify_digest(_record()) is None


def test_canonical_json_sorted_compact_and_unicode_preserved():
    assert proto.canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'


def test_reply_expected_true_builds_schema_v2_and_v1_still_refuses_it():
    record = _record(reply_expected=True)
    assert record["schema_version"] == proto.SCHEMA_VERSION_REPLY == 2
    assert record["reply_expected"] is True
    proto.verify_digest(record)
    # A v1 record that asks for a reply is still refused exactly as M1 did: the
    # delivery-only schema keeps one meaning.
    v1 = dict(record, schema_version=1)
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.validate_request_record(v1)
    assert excinfo.value.code == proto.ERR_REPLY_NOT_SUPPORTED
    assert _record(reply_expected=False)["reply_expected"] is False
    assert _record(reply_expected=False)["schema_version"] == 1


def test_schema_v2_requires_reply_expected_true_and_nothing_else_changes():
    record = _record(reply_expected=True)
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.validate_request_record(dict(record, reply_expected=False))
    assert excinfo.value.code == proto.ERR_INVALID_REQUEST
    with pytest.raises(proto.ProtocolError):
        proto.validate_request_record(dict(record, reply_expected="yes"))
    # Everything else a v2 record may carry is validated by the v1 rules.
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.validate_request_record(dict(record, dest_provider="gemini"))
    assert excinfo.value.code == proto.ERR_UNSUPPORTED_PROVIDER
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.validate_request_record(dict(record, text="x" * (proto.MAX_TEXT_BYTES + 1)))
    assert excinfo.value.code == proto.ERR_INVALID_REQUEST
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.validate_request_record(dict(record, new_field=True))
    assert excinfo.value.code == proto.ERR_INVALID_REQUEST


def test_v1_and_v2_digests_differ_for_the_same_payload():
    v1 = _record()
    v2 = _record(reply_expected=True)
    assert v1["request_id"] == v2["request_id"]
    assert v1["digest"] != v2["digest"]
    # A v2 record cannot be passed off by a v1 digest.
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.verify_digest(dict(v2, digest=v1["digest"]))
    assert excinfo.value.code == proto.ERR_DIGEST_MISMATCH


def test_unsupported_provider_refused_with_positive_control():
    for provider in ("gemini", "hermes", "opencode", "pi"):
        with pytest.raises(proto.ProtocolError) as excinfo:
            _record(dest_provider=provider)
        assert excinfo.value.code == proto.ERR_UNSUPPORTED_PROVIDER
    assert _record(dest_provider="claude")["dest_provider"] == "claude"
    assert _record(dest_provider="codex")["dest_provider"] == "codex"


def test_provider_other_than_the_capability_table_is_never_accepted():
    assert set(proto.REMOTE_PROVIDERS) == set(proto.capabilities()["reply_modes"])


def test_text_must_be_non_empty_and_within_the_cap():
    with pytest.raises(proto.ProtocolError):
        _record(text="")
    with pytest.raises(proto.ProtocolError):
        _record(text=123)
    boundary = "x" * proto.MAX_TEXT_BYTES
    assert _record(text=boundary)["text"] == boundary
    with pytest.raises(proto.ProtocolError):
        _record(text=boundary + "x")
    # The cap is counted in UTF-8 bytes, not code points.
    multibyte = "é" * (proto.MAX_TEXT_BYTES // 2)
    assert len(multibyte) == proto.MAX_TEXT_BYTES // 2
    assert _record(text=multibyte)["text"] == multibyte
    with pytest.raises(proto.ProtocolError):
        _record(text=multibyte + "é")


def test_request_id_must_be_host_qualified():
    with pytest.raises(proto.ProtocolError):
        _record(request_id=REAL_LOCAL_ID_ASK)
    with pytest.raises(proto.ProtocolError):
        _record(request_id=f"{REAL_LOCAL_ID_ASK}-hABC1")
    assert _record(request_id=f"{REAL_LOCAL_ID_ASK}-habc1")["request_id"].endswith("-habc1")


def test_dest_project_alias_shape_is_bounded():
    for alias in ("", "../etc", "a b", "a/b", "-leading", "x" * 65):
        with pytest.raises(proto.ProtocolError):
            _record(dest_project=alias)
    assert _record(dest_project="example-project_1.2")["dest_project"] == "example-project_1.2"


def test_dest_session_must_be_short_printable():
    for session in ("", "s" * 257, "bad\nsession", "bad\x00"):
        with pytest.raises(proto.ProtocolError):
            _record(dest_session=session)
    assert _record(dest_session="2c8ab895-d0ab-49c6-86c1-0f7ff9fb6e8e")["dest_session"] == (
        "2c8ab895-d0ab-49c6-86c1-0f7ff9fb6e8e"
    )


def test_unknown_record_field_refused_with_positive_control():
    record = _record()
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.validate_request_record(dict(record, replyExpect=True))
    assert excinfo.value.code == proto.ERR_INVALID_REQUEST
    assert proto.validate_request_record(record)["request_id"] == record["request_id"]


def test_wrong_schema_version_refused_with_positive_control():
    record = _record()
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.validate_request_record(dict(record, schema_version=3))
    assert excinfo.value.code == proto.ERR_UNSUPPORTED_SCHEMA
    with pytest.raises(proto.ProtocolError):
        proto.validate_request_record(dict(record, schema_version=True))
    assert proto.validate_request_record(record)["schema_version"] == 1


def test_source_claims_must_be_an_object_and_are_audit_only():
    with pytest.raises(proto.ProtocolError):
        _record(source_claims=["audit"])
    record = _record(source_claims={"sender": "host-a", "nested": {"k": [1, 2]}})
    assert record["source_claims"] == {"sender": "host-a", "nested": {"k": [1, 2]}}
    assert "source_claims" not in proto.digest_payload(record)


def test_intent_must_be_a_short_non_empty_string():
    with pytest.raises(proto.ProtocolError):
        _record(intent="")
    with pytest.raises(proto.ProtocolError):
        _record(intent="i" * (proto.MAX_INTENT_CHARS + 1))
    assert _record(intent="notify")["intent"] == "notify"


# --------------------------------------------------------------------------- #
# Parser-hardening regressions (found by probing the gate as a subprocess)
# --------------------------------------------------------------------------- #


def test_deeply_nested_json_refused_with_positive_control():
    payload = b"[" * 200_000
    assert len(payload) < proto.MAX_FRAME
    framed = struct.pack(">I", len(payload)) + payload
    error = _refused(framed, proto.ERR_INVALID_JSON)
    assert isinstance(error, proto.ProtocolError)  # never a RecursionError
    # Nested-but-shallow JSON with an object at the root is still accepted.
    nested = b'{"a":' + b"[" * 50 + b"1" + b"]" * 50 + b"}"
    assert isinstance(_read(struct.pack(">I", len(nested)) + nested), dict)
    assert _read(proto.encode_frame(_envelope())) == _envelope()


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_non_standard_json_constants_refused_at_parse_with_positive_control(constant: bytes):
    for payload in (
        b'{"a":' + constant + b"}",
        b'{"a":{"b":[' + constant + b"]}}",
    ):
        error = _refused(struct.pack(">I", len(payload)) + payload, proto.ERR_INVALID_JSON)
        # Refused by the parser's constant hook, not merely by the later scan.
        assert "non-standard JSON constant" in error.message
    # Control: the same frame with a standard number parses, and re-encodes.
    control = b'{"a":1.0}'
    assert _read(struct.pack(">I", len(control)) + control) == {"a": 1.0}
    assert proto.encode_frame({"a": 1.0})  # must stay encodable


def test_accepted_frames_are_always_re_encodable():
    for payload in (b'{"a":1.0}', b'{"a":-0.0}', b'{"a":1e308}', b'{"a":123456789012345678901234567890}'):
        decoded = _read(struct.pack(">I", len(payload)) + payload)
        assert proto.encode_frame(decoded)
    # A standard JSON literal that overflows a float is refused: it would
    # parse to infinity and could never be re-encoded (allow_nan=False).
    for payload in (b'{"a":1e400}', b'{"a":-1e400}'):
        error = _refused(struct.pack(">I", len(payload)) + payload, proto.ERR_INVALID_JSON)
        # A standard literal can only be caught by the post-parse finiteness scan.
        assert "non-finite number" in error.message


# --------------------------------------------------------------------------- #
# Review fix: `^...$` anchors miss a trailing newline; shape checks must be
# whole-string (re.fullmatch) and the envelope body must be explicit.
# --------------------------------------------------------------------------- #


def test_local_id_with_trailing_newline_is_refused_with_positive_control():
    good = REAL_LOCAL_ID_ASK
    assert proto.parse_remote_id(f"{good}-habc1") == (good, "abc1")
    for bad in (f"{good}\n", f"{good} \n"):
        with pytest.raises(proto.ProtocolError):
            proto.make_remote_id(bad, "abc1")
    for bad in (f"{good}\n-habc1", f"{good}-habc1\n"):
        with pytest.raises(proto.ProtocolError):
            proto.parse_remote_id(bad)
    # Positive control: the same id without the newline is accepted end to end.
    assert proto.make_remote_id(good, "abc1") == f"{good}-habc1"


def test_host_token_with_trailing_newline_is_refused_with_positive_control():
    for bad in ("abc1\n", "\nabc1", "abc1\nx"):
        with pytest.raises(proto.ProtocolError):
            proto.make_remote_id(REAL_LOCAL_ID_ASK, bad)
        with pytest.raises(proto.ProtocolError):
            proto.parse_remote_id(f"{REAL_LOCAL_ID_ASK}-h{bad}")
    assert proto.parse_remote_id(f"{REAL_LOCAL_ID_ASK}-habc1") == (REAL_LOCAL_ID_ASK, "abc1")


def test_project_alias_with_trailing_newline_is_refused_with_positive_control():
    for bad in ("example-project\n", "example-project \n", "example\n"):
        with pytest.raises(proto.ProtocolError):
            _record(dest_project=bad)
    assert _record(dest_project="example-project")["dest_project"] == "example-project"


def test_digest_with_trailing_newline_is_refused_with_positive_control():
    record = _record()
    with pytest.raises(proto.ProtocolError):
        proto.verify_digest(dict(record, digest=record["digest"] + "\n"))
    with pytest.raises(proto.ProtocolError):
        proto.validate_request_record(dict(record, digest=record["digest"] + "\n"))
    assert proto.verify_digest(record) is None


def test_session_with_trailing_newline_is_refused_with_positive_control():
    with pytest.raises(proto.ProtocolError):
        _record(dest_session="2c8ab895-d0ab-49c6-86c1-0f7ff9fb6e8e\n")
    with pytest.raises(proto.ProtocolError):
        _record(dest_session="sess\n\n")
    assert _record(dest_session="2c8ab895-d0ab-49c6-86c1-0f7ff9fb6e8e")["dest_session"].endswith("e8e")


def test_intent_with_trailing_newline_is_refused_with_positive_control():
    with pytest.raises(proto.ProtocolError):
        _record(intent="notify\n")
    assert _record(intent="notify")["intent"] == "notify"


def test_envelope_body_must_be_an_explicit_object():
    for envelope in (
        {"proto": {"major": 1, "minor": 0}, "op": "hello", "op_id": "op-1"},
        {"proto": {"major": 1, "minor": 0}, "op": "hello", "op_id": "op-1", "body": None},
        _envelope(body=None),
    ):
        with pytest.raises(proto.ProtocolError) as excinfo:
            proto.validate_envelope(envelope)
        assert excinfo.value.code == proto.ERR_INVALID_REQUEST
    # Positive control: an explicit empty object is a valid body.
    assert proto.validate_envelope(_envelope(body={})) is not None


def _anchor_violations(source: str) -> list[str]:
    """Shapes that would accept a trailing newline: `$`-anchored patterns or `.match(`."""
    problems: list[str] = []
    for compiled in re.finditer(r"re\.compile\(\s*r?([\"'])(.+?)\1\s*\)", source, re.S):
        pattern = compiled.group(2)
        if pattern.startswith("^") or pattern.endswith("$"):
            problems.append(f"anchored pattern: {pattern!r}")
    if ".match(" in source:
        problems.append(".match(")
    return problems


def test_no_dollar_anchored_or_prefix_matching_shape_checks():
    repo = Path(__file__).resolve().parents[1]
    for path in (repo / "lib" / "remote_protocol.py", repo / "bin" / "ccb-remote-gate"):
        assert _anchor_violations(path.read_text(encoding="utf-8")) == [], path.name


def test_the_anchor_guard_detects_a_violation():
    """Positive control for the static guard above."""
    assert _anchor_violations('PATTERN = re.compile(r"^[a-z]+$")\nPATTERN.match(value)\n')
    assert _anchor_violations('PATTERN = re.compile(r"[a-z]+")\nPATTERN.fullmatch(value)\n') == []
