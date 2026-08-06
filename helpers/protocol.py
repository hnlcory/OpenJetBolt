"""
helpers.protocol -- the Jetson Bolt BLE wire protocol: GATT
characteristic UUIDs, the binary AA..BB frame format, and pure parse/format
helpers on top of it. No networking or session state here -- see
transport.py for the async BLE session built on top of this module.
"""

# WRITE_CHAR        -- GATT characteristic UUID the client writes AT
#                      commands and binary frames to (vendor UART-style
#                      service, discovered via GATT enumeration).
# NOTIFY_CHAR       -- GATT characteristic UUID the bike pushes replies
#                      and telemetry through; subscribing to this is what
#                      triggers bleak's implicit CCCD ("enable
#                      notifications") write.
# START, END        -- binary frame delimiter bytes (0xAA .. 0xBB).
# TYPE_SET_SPEED    -- binary frame TYPE byte for the "set max speed"
#                      command.
# TELEMETRY         -- binary frame TYPE byte -> human-readable label, for
#                      the five telemetry frame kinds the bike streams.
WRITE_CHAR  = "1a764871-c42d-11e5-953d-0002a5d5c51b"
NOTIFY_CHAR = "1a764874-c42d-11e5-953d-0002a5d5c51b"
START, END = 0xAA, 0xBB
TYPE_SET_SPEED = 0x06
TELEMETRY = {0xA1: "status", 0xA2: "status2", 0xA3: "max_speed", 0xA4: "status4", 0xA7: "status7"}


# build_frame(msg_type, payload)
#   Usage: frame = build_frame(0x06, bytes([25]))
#     Builds one binary command frame: AA <TYPE> <LEN> <PAYLOAD...> <CKSUM> BB.
#   Args:
#     msg_type (int)   -- one-byte frame type (e.g. TYPE_SET_SPEED).
#     payload (bytes)  -- zero or more payload bytes (default: none).
#   Returns:
#     bytes -- the complete, checksummed, ready-to-write frame.
def build_frame(msg_type, payload=b""):
    # LEN counts the WHOLE frame (start+type+len+payload+cksum+end), which
    # is why it's "5 fixed bytes + payload length" rather than just
    # len(payload).
    header = bytes([START, msg_type, 5 + len(payload)]) + payload
    checksum = 0
    for byte_val in header:
        checksum ^= byte_val                          # CKSUM = XOR of every byte from START through the last payload byte
    return header + bytes([checksum, END])


# verify_frame(frame)
#   Usage: is_valid = verify_frame(frame)
#     Recomputes a received binary frame's checksum and compares it to the
#     checksum byte the frame actually carries.
#   Args:
#     frame (bytes) -- a complete AA...BB frame, checksum and end byte
#                       included.
#   Returns:
#     bool -- True if the frame is at least 5 bytes, starts with START and
#             ends with END, and the XOR checksum matches; False otherwise.
def verify_frame(frame):
    if len(frame) < 5 or frame[0] != START or frame[-1] != END:
        return False
    checksum = 0
    for byte_val in frame[:-2]:                        # everything except the trailing [CKSUM, END] pair
        checksum ^= byte_val
    return checksum == frame[-2]


# decode_notify(data)
#   Usage: text = decode_notify(data)
#     Turns one raw notification payload from the bike into a human-readable
#     log line, for both binary telemetry frames and plain ASCII replies.
#     Purely presentational -- does not affect control flow anywhere.
#   Args:
#     data (bytes) -- raw bytes as delivered by the BLE notify callback.
#   Returns:
#     str -- one printable line describing the frame/reply.
def decode_notify(data):
    if data[:1] == bytes([START]) and data[-1:] == bytes([END]):
        checksum_status = "ok" if verify_frame(data) else "BAD-CKSUM"
        frame_type = data[1]                          # frame TYPE byte, e.g. 0xA3
        body = data[3:-2]                              # payload only: strip START/TYPE/LEN and CKSUM/END
        extra = ""
        if frame_type == 0xA3 and body:
            extra = f"  active_max={body[0]} km/h ({body[0]*0.6214:.1f} mph)"   # body[0] = active speed limit
        elif frame_type == 0xA1 and len(body) >= 5:
            speed = (body[1] << 8) | body[2]          # body[1:3] = big-endian 16-bit live speed/RPM reading
            extra = f"  batt={body[0]}%  speed_raw={speed}  lim={body[4]}"       # body[0]=battery%, body[4]=current limit
        elif frame_type == 0xA4 and len(body) >= 5:
            flags = []
            if body[3] & 0x01:                         # bit 0 of body[3] = brake lever engaged
                flags.append("BRAKE")
            if body[4] & 0x10:                          # bit 4 of body[4] = headlight on
                flags.append("LIGHT")
            extra = f"  {'+'.join(flags) if flags else '-'}  vbat_raw={body[1]}"  # body[1] = analog voltage-ish reading
        elif frame_type == 0xA2 and len(body) >= 5:
            extra = f"  cruise={body[1]}"              # body[1] = cruise-control setting
        return f"[bin {TELEMETRY.get(frame_type, f'type_0x{frame_type:02x}')} {checksum_status}] {data.hex(' ')}{extra}"
    try:
        return f"[ascii] {data.decode('ascii').strip()}"
    except UnicodeDecodeError:
        return f"[raw] {data.hex(' ')}"                # neither a valid AA..BB frame nor ASCII -- dump raw hex


# parse_a1(data)
#   Usage: result = parse_a1(data)
#     Extract the fields carried by an 0xA1 "status" telemetry frame.
#   Args:
#     data (bytes) -- one raw notification payload (any frame type; this
#                     function checks the type itself).
#   Returns:
#     tuple(battery: int, speed_raw: int, cap: int) if `data` is a
#     well-formed 0xA1 frame, else None.
#       battery   -- battery percentage, 0-100
#       speed_raw -- live 16-bit speed/RPM reading (big-endian)
#       cap       -- currently active max-speed limit, km/h
def parse_a1(data):
    if len(data) >= 9 and data[0] == START and data[1] == 0xA1:
        payload = data[3:-2]                           # payload bytes only
        return (payload[0], (payload[1] << 8) | payload[2], payload[4])
    return None


# parse_a4(data)
#   Usage: result = parse_a4(data)
#     Extract the fields carried by an 0xA4 "status4" telemetry frame.
#   Args:
#     data (bytes) -- one raw notification payload (any frame type; this
#                     function checks the type itself).
#   Returns:
#     tuple(brake: int, light: int, analog: int) if `data` is a well-formed
#     0xA4 frame, else None.
#       brake  -- 1 if the brake lever is engaged, else 0
#       light  -- 1 if the headlight is on, else 0
#       analog -- raw analog reading (voltage/temperature-ish, undecoded)
def parse_a4(data):
    if len(data) >= 9 and data[0] == START and data[1] == 0xA4:
        payload = data[3:-2]                           # payload bytes only
        return (1 if payload[3] & 0x01 else 0, 1 if payload[4] & 0x10 else 0, payload[1])
    return None
