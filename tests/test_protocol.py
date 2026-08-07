# Unit tests for helpers/protocol.py -- the binary AA..BB frame format and
# its pure parse/format helpers. No networking or state here, so no mocking
# is required.

from helpers.protocol import (
    END,
    START,
    build_frame,
    decode_notify,
    parse_a1,
    parse_a4,
    verify_frame,
)


# ---------------------------------------------------------------------------
# build_frame / verify_frame
# ---------------------------------------------------------------------------

# With no payload, the frame should be exactly 5 bytes: START, TYPE,
# LEN(=5), CKSUM, END, with CKSUM being the XOR of the first 3 bytes.
def test_build_frame_with_no_payload():
    frame = build_frame(0x06)
    assert frame[0] == START
    assert frame[1] == 0x06
    assert frame[2] == 5
    assert frame[-1] == END
    checksum = START ^ 0x06 ^ 5
    assert frame[3] == checksum
    assert len(frame) == 5


# LEN must count the whole frame (5 fixed bytes + payload), the payload
# must be carried through unchanged, and the checksum must XOR every byte
# from START through the last payload byte (not just the header).
def test_build_frame_with_payload():
    payload = bytes([0x01, 0x02, 0x03])
    frame = build_frame(0x06, payload)
    assert frame[2] == 5 + len(payload)
    header = bytes([START, 0x06, 5 + len(payload)]) + payload
    expected_checksum = 0
    for byte_val in header:
        expected_checksum ^= byte_val
    assert frame[-2] == expected_checksum
    assert frame[-1] == END
    assert frame[3:-2] == payload


# Anything build_frame() produces should be accepted by verify_frame() --
# the two functions must agree on what a valid frame looks like.
def test_build_frame_round_trips_through_verify_frame():
    for msg_type, payload in [(0x06, b""), (0x06, bytes([25])), (0xA1, bytes(range(6)))]:
        assert verify_frame(build_frame(msg_type, payload)) is True


# Frames shorter than 5 bytes can't contain a valid header + checksum + end,
# so they must always fail verification regardless of content.
def test_verify_frame_rejects_too_short():
    assert verify_frame(bytes([START, 0x06, END])) is False


# A frame that doesn't begin with the START byte is not a valid frame, even
# if everything else about it (length, checksum) looks right.
def test_verify_frame_rejects_wrong_start_byte():
    frame = bytearray(build_frame(0x06, bytes([1])))
    frame[0] = 0x00
    assert verify_frame(bytes(frame)) is False


# Same idea for the END byte -- a truncated/corrupted frame that's missing
# its terminator must be rejected.
def test_verify_frame_rejects_wrong_end_byte():
    frame = bytearray(build_frame(0x06, bytes([1])))
    frame[-1] = 0x00
    assert verify_frame(bytes(frame)) is False


# Flipping the checksum byte while leaving everything else intact must be
# caught -- this is the whole point of having a checksum.
def test_verify_frame_rejects_tampered_checksum():
    frame = bytearray(build_frame(0x06, bytes([1])))
    frame[-2] ^= 0xFF
    assert verify_frame(bytes(frame)) is False


# ---------------------------------------------------------------------------
# decode_notify
# ---------------------------------------------------------------------------

# Plain text replies (not AA..BB frames) should be labeled "[ascii]" with
# the trailing \r\n stripped for readability.
def test_decode_notify_ascii_reply():
    assert decode_notify(b"CODE_OK\r\n") == "[ascii] CODE_OK"


# Bytes that are neither a valid AA..BB frame nor decodable ASCII must fall
# back to a raw hex dump rather than raising an exception.
def test_decode_notify_raw_hex_fallback_for_undecodable_bytes():
    data = bytes([0x81, 0x82, 0x83])
    assert decode_notify(data) == f"[raw] {data.hex(' ')}"


# An 0xA1 "status" frame should surface battery %, live speed, and the
# current speed limit in the decoded line, plus report checksum "ok".
def test_decode_notify_a1_status_frame():
    payload = bytes([80, 0x01, 0x2C, 0, 25])   # battery=80, speed_raw=0x012C=300, lim=25
    frame = build_frame(0xA1, payload)
    line = decode_notify(frame)
    assert "[bin status ok]" in line
    assert "batt=80" in line
    assert "speed_raw=300" in line
    assert "lim=25" in line


# A frame with a tampered checksum should still be decoded (best-effort),
# but flagged as BAD-CKSUM instead of "ok" so a bad read is visible.
def test_decode_notify_flags_bad_checksum():
    frame = bytearray(build_frame(0xA1, bytes([80, 0, 0, 0, 25])))
    frame[-2] ^= 0xFF   # tamper the checksum, keep START/END and body intact
    line = decode_notify(bytes(frame))
    assert "BAD-CKSUM" in line


# An 0xA2 "status2" frame should surface the cruise-control setting.
def test_decode_notify_a2_status2_frame():
    payload = bytes([0, 42, 0, 0, 0])   # cruise = body[1] = 42
    line = decode_notify(build_frame(0xA2, payload))
    assert "[bin status2 ok]" in line
    assert "cruise=42" in line


# An 0xA3 "max_speed" frame should surface the active limit in both km/h
# and the converted mph value.
def test_decode_notify_a3_max_speed_frame():
    payload = bytes([30])   # active_max = 30 km/h
    line = decode_notify(build_frame(0xA3, payload))
    assert "[bin max_speed ok]" in line
    assert "active_max=30 km/h (18.6 mph)" in line


# An 0xA4 "status4" frame encodes brake/light as individual bits -- check
# all four combinations (neither/brake-only/light-only/both) decode to the
# expected flag text, alongside the raw analog reading.
def test_decode_notify_a4_status4_frame_flag_combinations():
    cases = [
        (0x00, 0x00, "-"),
        (0x01, 0x00, "BRAKE"),
        (0x00, 0x10, "LIGHT"),
        (0x01, 0x10, "BRAKE+LIGHT"),
    ]
    for brake_byte, light_byte, expected in cases:
        payload = bytes([0, 150, 0, brake_byte, light_byte])   # vbat_raw = body[1] = 150
        line = decode_notify(build_frame(0xA4, payload))
        assert f"  {expected}  vbat_raw=150" in line


# A frame type not in the TELEMETRY dict should still decode (as a
# generically-labeled "type_0xNN" frame) rather than raising a KeyError.
def test_decode_notify_unknown_frame_type_falls_back_to_hex_label():
    line = decode_notify(build_frame(0x99))
    assert "[bin type_0x99 ok]" in line


# ---------------------------------------------------------------------------
# parse_a1 / parse_a4
# ---------------------------------------------------------------------------

# A well-formed 0xA1 frame should extract to (battery, speed_raw, cap).
def test_parse_a1_well_formed_frame():
    payload = bytes([80, 0x01, 0x2C, 0, 25])
    frame = build_frame(0xA1, payload)
    assert parse_a1(frame) == (80, 300, 25)


# Data shorter than the minimum 0xA1 frame length must return None rather
# than raising an IndexError.
def test_parse_a1_too_short_returns_none():
    assert parse_a1(bytes([START, 0xA1, 0, 0, 0])) is None


# A frame of the right shape but the wrong TYPE byte is not an 0xA1 frame
# and must be rejected (returns None), not mis-parsed.
def test_parse_a1_wrong_type_returns_none():
    payload = bytes([80, 0x01, 0x2C, 0, 25])
    frame = build_frame(0xA2, payload)   # same shape, different type byte
    assert parse_a1(frame) is None


# A well-formed 0xA4 frame should extract to (brake, light, analog) -- check
# all four brake/light bit combinations map to the correct 0/1 values.
def test_parse_a4_well_formed_frame_all_flag_combinations():
    cases = [
        (0x00, 0x00, 0, 0),
        (0x01, 0x00, 1, 0),
        (0x00, 0x10, 0, 1),
        (0x01, 0x10, 1, 1),
    ]
    for brake_byte, light_byte, expected_brake, expected_light in cases:
        payload = bytes([0, 150, 0, brake_byte, light_byte])
        frame = build_frame(0xA4, payload)
        assert parse_a4(frame) == (expected_brake, expected_light, 150)


# Same too-short guard as parse_a1, for parse_a4.
def test_parse_a4_too_short_returns_none():
    assert parse_a4(bytes([START, 0xA4, 0, 0, 0])) is None


# Same wrong-type guard as parse_a1, for parse_a4.
def test_parse_a4_wrong_type_returns_none():
    payload = bytes([0, 150, 0, 1, 0x10])
    frame = build_frame(0xA1, payload)
    assert parse_a4(frame) is None
