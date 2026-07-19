#!/usr/bin/env python3
"""
OpenJetBolt.py -- talk to ANY Jetson Bolt E-Bike BLE controller directly without the official app.

Requires:  pip install bleak
Usage:
    python OpenJetBolt.py selftest                   # validate pm_token offline (no bike)
    python OpenJetBolt.py scan                       # list nearby BLE devices
    python OpenJetBolt.py pair                       # discover a bike & save it as the default
    python OpenJetBolt.py config show|set|clear      # manage saved defaults
    python OpenJetBolt.py collect [count]
    python OpenJetBolt.py monitor [seconds]
    python OpenJetBolt.py log [seconds] [path]
    python OpenJetBolt.py info | get | set <km/h>

  Any bike-facing command accepts overrides, e.g.:
    python OpenJetBolt.py --address AA:BB:CC:DD:EE:FF --password 123456 info

Per-bike settings resolve in this order (highest priority first):
  1. --address / --password / --name-hint flags on the command line
  2. jetson_bolt_config.json next to this script (see the `config` command)
  3. built-in defaults (no fixed address -> scan by name hint; password 000000)

Note: The +PM auth transform (pm_token / _SBOX / _KEY / _INV) comes from the shared "Ride
Jetson" app binary so it is the same for every Jetson Bolt - what differs
per bike is the BLE address and potentially the 6-digit CODE= password.
"""

import argparse
import asyncio
import json
import random
import shutil
import sys
from pathlib import Path

# bleak: cross-platform (macOS/Linux/Windows) BLE client library and the
# only third-party dependency (`pip install bleak`). It scans, connects,
# writes GATT characteristics, and performs the CCCD "enable notifications"
# write for us whenever we call client.start_notify().
from bleak import BleakScanner, BleakClient

# ---------------------------------------------------------------------------
# +PM auth transform, ported verbatim from BleEncryption.encryptionStringOfValue()
# in the Jetson app. Validated against 12 captured nonce->token pairs (all pass).
# Shared by every Jetson Bolt -- it's a property of the app/firmware, not a
# per-device secret.

# _SBOX: the standard AES substitution box (256-entry byte -> byte lookup
# table). Recognizable by its first row (63 7c 77 7b f2 6b 6f c5 ...); reused
# here purely as a fixed scrambling table, not as part of real AES.
_SBOX = [
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
]
# _KEY: 16-byte secret key, extracted as a literal constant from the
# decompiled app (BleEncryption.key). Same value on every install of the app,
# so it authenticates the *app*, not an individual bike or user. Kept under
# this name to match the decompiled Java field it was read from.
_KEY = [130,224,59,247,81,196,183,128,219,59,213,52,194,80,95,23]
# _INV: second 16-byte constant (BleEncryption.inv) XORed into the state
# during key mixing, alongside _KEY. Purpose unknown beyond "extra scrambling
# constant"; treated as an opaque literal, same as _KEY. Also kept under its
# original decompiled field name for traceability back to the app source.
_INV = [240,173,249,9,177,51,187,250,113,220,19,117,32,49,32,93]


# _bonding_key()
#   Usage: substituted_key = _bonding_key()
#     Derives the 16-byte working subkey used to mix _KEY into the nonce
#     buffer inside pm_token(). Pure function of the constants above --
#     takes no arguments and returns the same value every call.
#   Args: (none)
#   Returns:
#     list[int] (len 16) -- _KEY substituted through the AES S-box, then
#                            rotated left by 4 bytes (i.e. byte 4 moves to
#                            position 0, ..., and the original first 4 bytes
#                            wrap around to the end).
def _bonding_key():
    substituted_key = [_SBOX[_KEY[byte_idx] & 255] for byte_idx in range(16)]  # sub_bytes(copy(key)): substitute each key byte through the AES S-box
    first_four_bytes = substituted_key[0:4]              # save the first 4 substituted bytes before they get overwritten
    for pos in range(12):
        substituted_key[pos] = substituted_key[pos + 4]  # shift bytes 4..15 down to positions 0..11 (rotate left by 4)
    substituted_key[12:16] = first_four_bytes             # the saved original first 4 bytes become the new last 4 (wrap-around)
    return substituted_key


# pm_token(nonce_hex)
#   Usage: token = pm_token(nonce_hex)
#     Computes the bike's expected +PM authentication response for a given
#     challenge nonce -- this is the reverse-engineered equivalent of the
#     app's BleEncryption.encryptionStringOfValue(). Must be called with NO
#     other BLE traffic to the bike between receiving the "+PM>NONCE"
#     challenge and sending the "+PM<token" answer, because a stray "+PA"
#     oracle call in between invalidates the pending nonce server-side
#     (see Bolt.pair() and Bolt.oracle() below).
#   Args:
#     nonce_hex (str) -- 12 hex characters (6 raw bytes), taken verbatim
#                        from the "+PM>NONCE" line the bike sends.
#   Returns:
#     int -- 32-bit token. Format as 8 lowercase hex chars (f"{token:08x}")
#            and send back as "+PM<<8 hex chars>" to complete the handshake.
def pm_token(nonce_hex: str) -> int:
    nonce_bytes = [int(nonce_hex[byte_idx * 2:byte_idx * 2 + 2], 16) for byte_idx in range(6)]  # nonce hex string -> 6 raw bytes

    # Expand the 6-byte nonce into a 32-byte working buffer `expanded` by
    # repeating and partially re-repeating it, per the decompiled algorithm:
    #   expanded[0:6]   = nonce                (bytes 0-5)
    #   expanded[6:12]  = nonce again          (bytes 6-11)
    #   expanded[12:16] = first 4 nonce bytes  (bytes 12-15)
    expanded = [0] * 32
    expanded[0:6] = nonce_bytes[0:6]
    expanded[6:12] = nonce_bytes[0:6]
    expanded[12:16] = nonce_bytes[0:4]

    # arraycopy(ne, 1, ne, 16, 15): copy expanded[1:16] (15 bytes) into
    # expanded[16:31], i.e. the second half mirrors the first half shifted
    # left by one byte.
    mirror_source = expanded[1:16]
    for offset in range(15):
        expanded[16 + offset] = mirror_source[offset]
    expanded[31] = expanded[0]  # last byte wraps back to expanded[0], closing the buffer

    # Key mixing: XOR every byte of both 16-byte halves with the derived
    # subkey and the _INV constant. This is where the secret key actually
    # enters the computation.
    subkey = _bonding_key()
    for idx in range(16):
        expanded[idx] = (expanded[idx] ^ subkey[idx]) ^ _INV[idx]
        expanded[idx + 16] = (expanded[idx + 16] ^ subkey[idx]) ^ _INV[idx]

    # Diffusion: a running XOR chain down each 16-byte half, so every byte
    # from index idx onward carries the influence of all bytes before it
    # (expanded[idx] ^= expanded[idx-1] cascades left-to-right).
    for idx in range(1, 16):
        expanded[idx] ^= expanded[idx - 1]
        expanded[idx + 16] ^= expanded[idx + 16 - 1]

    # Fold the 32-byte buffer down to 4 output bytes. Note the ONE integer
    # ADDITION (not XOR) at `expanded[out_idx+12] + expanded[out_idx+16]` --
    # per walkthrough.md Part VII, this single `+` is what makes the whole
    # transform non-linear over GF(2), which is why it can't be
    # reconstructed from oracle samples the way a CRC or affine function
    # could.
    output_bytes = [0] * 4
    for out_idx in range(4):
        folded = expanded[out_idx] ^ expanded[out_idx + 4] ^ expanded[out_idx + 8]
        folded ^= (expanded[out_idx + 12] + expanded[out_idx + 16])   # integer addition, not xor -- breaks GF(2) linearity
        folded ^= expanded[out_idx + 20] ^ expanded[out_idx + 24] ^ expanded[out_idx + 28]
        output_bytes[out_idx] = folded & 255                          # truncate back to a byte (the add above can carry past 0xFF)
    # Pack the 4 output bytes into a big-endian 32-bit integer.
    return (output_bytes[0] << 24) | (output_bytes[1] << 16) | (output_bytes[2] << 8) | output_bytes[3]


# KNOWN_VECTORS: (nonce_hex, expected_token_hex) pairs captured live from the
# bike's +PA oracle. Used only by cmd_selftest() to validate pm_token()
# offline, with no bike required -- run `python OpenJetBolt.py
# selftest` any time you touch the crypto above.
KNOWN_VECTORS = [
    ("14cc500d98bc", "71f470f0"), ("ff767f0e785b", "eb79a1f1"), ("34e3fe06412f", "2d77f243"),
    ("eabcb14c4c8d", "f9c6378d"), ("a8793d864a2c", "9a0a5587"), ("d6d763743161", "d52fb65c"),
    ("5b8a48c7b7a3", "ba00d303"), ("15cc485d698f", "0c0421d9"), ("8b60bb9a0f41", "d99a2100"),
    ("cfddf4df3f67", "4810b6db"), ("28bd031cd632", "16024b35"), ("bb818d7e3559", "27b6bc38"),
]

# ---------------------------------------------------------------------------
# Module-wide constants.
#   DEFAULT_NAME_HINT -- substring matched (case-insensitive) against a
#                        scanned device's advertised name when no address
#                        is known yet.
#   DEFAULT_PASSWORD  -- fallback CODE= password if none is saved/passed.
#   WRITE_CHAR        -- GATT characteristic UUID the client writes AT
#                        commands and binary frames to (vendor UART-style
#                        service, discovered via GATT enumeration).
#   NOTIFY_CHAR       -- GATT characteristic UUID the bike pushes replies
#                        and telemetry through; subscribing to this is what
#                        triggers bleak's implicit CCCD ("enable
#                        notifications") write.
#   START, END        -- binary frame delimiter bytes (0xAA .. 0xBB).
#   TYPE_SET_SPEED    -- binary frame TYPE byte for the "set max speed"
#                        command.
#   TELEMETRY         -- binary frame TYPE byte -> human-readable label, for
#                        the five telemetry frame kinds the bike streams.
#   STATUS_BAR_FIELDS -- display order of fields in the pinned bottom
#                        status bar used by cmd_monitor()/cmd_log() (see
#                        class StatusBar).
#   CONFIG_PATH       -- where per-bike defaults (address/password/name
#                        hint) are persisted, as a JSON file next to this
#                        script.
DEFAULT_NAME_HINT = "Bolt"
DEFAULT_PASSWORD = "000000"
WRITE_CHAR  = "1a764871-c42d-11e5-953d-0002a5d5c51b"
NOTIFY_CHAR = "1a764874-c42d-11e5-953d-0002a5d5c51b"
START, END = 0xAA, 0xBB
TYPE_SET_SPEED = 0x06
TELEMETRY = {0xA1: "status", 0xA2: "status2", 0xA3: "max_speed", 0xA4: "status4", 0xA7: "status7"}
STATUS_BAR_FIELDS = ["BATTERY", "SPEED_RAW", "TOP_SPEED", "MAX", "BRAKE", "LIGHT", "CRUISE"]

CONFIG_PATH = Path(__file__).resolve().parent / "jetson_bolt_config.json"


# ---------------------------------------------------------------------------
# Per-bike config: BLE address, password, and (optionally) a name-hint used
# for scanning when no address is known yet. Written by `pair`/`config set`,
# read by resolve_settings() on every bike-facing command.

# load_config()
#   Usage: config = load_config()
#     Reads the saved-defaults JSON file from disk, if present.
#   Args: (none)
#   Returns:
#     dict -- possibly empty; may contain "address"/"password"/"name_hint"
#             keys (all optional). A missing or corrupt file is treated the
#             same as "no config yet" (returns {}) rather than raising, so a
#             bad file never crashes an unrelated command.
def load_config():
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# save_config(config)
#   Usage: save_config(config)
#     Overwrites CONFIG_PATH with `config` as pretty-printed JSON.
#   Args:
#     config (dict) -- full config to persist (callers merge with
#                      load_config() first if they want to update rather
#                      than replace).
#   Returns: None (side effect: writes CONFIG_PATH).
def save_config(config):
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")


# resolve_settings(args)
#   Usage: address, password, name_hint = resolve_settings(args)
#     Merge CLI overrides > saved config > built-in defaults, in that
#     priority order, for the three per-bike settings every bike-facing
#     command needs.
#   Args:
#     args (argparse.Namespace) -- must have .address, .password, and
#                                  .name_hint attributes (each str or None,
#                                  as produced by build_parser()).
#   Returns:
#     tuple(address: str | None, password: str, name_hint: str)
#       address may still be None here if never set anywhere -- resolve()
#       (below) is what falls back to scanning in that case.
def resolve_settings(args):
    config = load_config()
    address = args.address or config.get("address")
    password = args.password or config.get("password") or DEFAULT_PASSWORD
    name_hint = args.name_hint or config.get("name_hint") or DEFAULT_NAME_HINT
    return address, password, name_hint


# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# class StatusBar
#   A pinned 2-line summary -- one dashed separator plus one
#   "| LABEL: value | LABEL: value |" line -- fixed at the very bottom of
#   the terminal, while normal print() output keeps scrolling above it.
#   Used by cmd_monitor()/cmd_log() so live telemetry stays readable at a
#   glance instead of scrolling past.
#
#   How it works: ANSI/VT100 escape codes. `\x1b[<top>;<bottom>r` (DECSTBM,
#   "set scrolling region") confines normal terminal scrolling to
#   everything above the reserved lines; update() jumps into the reserved
#   region to repaint it, then moves the cursor back to the pinned
#   scrolling anchor (the bottom row of the scroll region, column 1) --
#   exactly where an ordinary print() already leaves it between lines --
#   so scrolling output elsewhere is unaffected. (Deliberately NOT done via
#   ANSI "save/restore cursor", `\x1b[s`/`\x1b[u` -- that pair is a single,
#   ambiguous save slot whose behavior around scroll regions isn't
#   consistent across terminals, and was observed to occasionally leave
#   the cursor parked in the reserved rows, so the next scrolling print --
#   often a status7/0xA7 line, just by bad luck of arrival timing -- got
#   drawn on top of the pinned bar instead of above it.) If stdout isn't a
#   real terminal (piped to a file, redirected, not a TTY), the bar
#   silently does nothing at all, so redirected output never gets raw
#   escape codes mixed into it.
#
#   Known limitation: the terminal size is captured once, on entry: if the
#   window is resized mid-session the reserved lines will be in the wrong
#   place until the next run. Not handled, since it's a minor cosmetic
#   issue for a nice-to-have feature.
#
#   Usage:
#     with StatusBar(["BATTERY", "MAX"]) as bar:
#         bolt.status_bar = bar
#         ...normal scrolling prints happen here, e.g. via Bolt.verbose...
#         bar.update({"BATTERY": "98%"})   # redraws the pinned line(s)
#
#   Construction args:
#     field_order (list[str]) -- display order of fields in the bar; any
#       field not yet passed to update() shows as "--" so the layout never
#       shifts as data trickles in.
class StatusBar:
    RESERVED_LINES = 2   # 1 separator line + 1 data line, pinned at the bottom

    def __init__(self, field_order):
        self.field_order = field_order
        self.values = {name: "--" for name in field_order}
        self.enabled = sys.stdout.isatty()   # never touch the terminal when output isn't an interactive TTY
        self.total_rows = 0
        self.columns = 80
        self.scroll_bottom = 0

    # __enter__: reserve the bottom RESERVED_LINES terminal rows for the bar
    # and confine normal scrolling to everything above them.
    # Returns: self, so `with StatusBar(...) as bar:` works.
    def __enter__(self):
        if self.enabled:
            size = shutil.get_terminal_size(fallback=(80, 24))
            self.total_rows, self.columns = size.lines, size.columns
            self.scroll_bottom = self.total_rows - self.RESERVED_LINES
            if self.scroll_bottom < 1:
                self.enabled = False   # terminal too short to reserve space safely -- fall back to plain scrolling
        if self.enabled:
            sys.stdout.write("\x1b[?25l")                        # hide cursor (avoids flicker during redraws)
            sys.stdout.write(f"\x1b[1;{self.scroll_bottom}r")     # DECSTBM: scrolling region = rows 1..scroll_bottom
            sys.stdout.write(f"\x1b[{self.scroll_bottom};1H")     # park cursor at the bottom of the scroll region
            sys.stdout.flush()
            self._redraw()
        return self

    # __exit__: always restore the terminal to normal full-screen scrolling,
    # even if the caller raised inside the `with` block -- otherwise the
    # user's shell is left with a broken scroll region afterwards.
    def __exit__(self, *a):
        if self.enabled:
            sys.stdout.write("\x1b[r")                            # reset scrolling region to the whole screen
            sys.stdout.write(f"\x1b[{self.total_rows};1H\n")       # move past the old bar so the shell prompt lands cleanly
            sys.stdout.write("\x1b[?25h")                          # show cursor again
            sys.stdout.flush()

    # update(new_values)
    #   Usage: bar.update({"BATTERY": "98%", "SPEED_RAW": "152"})
    #     Merges `new_values` into the bar's known field values (fields not
    #     included keep their previous/last-known value) and repaints.
    #   Args:
    #     new_values (dict[str, str]) -- subset of field_order -> display
    #       text; a single telemetry frame usually only updates 1-2 fields.
    #   Returns: None. No-op if stdout isn't a terminal.
    def update(self, new_values):
        self.values.update(new_values)
        if self.enabled:
            self._redraw()

    # _redraw(): repaint the two reserved lines in place without disturbing
    # whatever the caller is scrolling above them.
    def _redraw(self):
        bar_text = "| " + " | ".join(f"{name}: {self.values[name]}" for name in self.field_order) + " |"
        bar_text = bar_text[:self.columns]      # never let an over-wide bar line auto-wrap onto a 3rd row
        separator = "-" * self.columns
        sys.stdout.write(f"\x1b[{self.scroll_bottom + 1};1H\x1b[2K{separator}")  # repaint separator line
        sys.stdout.write(f"\x1b[{self.scroll_bottom + 2};1H\x1b[2K{bar_text}")   # repaint data line
        # Return to the pinned scrolling anchor deterministically (see the
        # class docstring for why this replaced save/restore cursor).
        sys.stdout.write(f"\x1b[{self.scroll_bottom};1H")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# class Bolt
#   One BLE session with a single Jetson Bolt bike. Wraps a BleakClient plus
#   the AT-command/binary-frame protocol on top of it: login, mutual-auth
#   pairing, telemetry parsing, and the set-max-speed command.
#
#   Usage:
#     async with Bolt(address, password) as bolt:
#         await bolt.handshake()
#         await bolt.set_max_speed(30)
#
#   Construction args:
#     address  (str)  -- BLE address (or macOS CoreBluetooth UUID) to connect to.
#     password (str)  -- 6-digit CODE= password for this bike (default "000000").
#     verbose  (bool) -- if True, print every decoded notification as it
#                        arrives (from the notify callback); if False, only
#                        lines consumed by _await() are printed, at the point
#                        they're consumed.
class Bolt:
    def __init__(self, address, password=DEFAULT_PASSWORD, verbose=False):
        self.client = BleakClient(address)
        self.password = password
        self.rx_queue = asyncio.Queue()    # decoded notification lines waiting to be matched by _await()
        self.last_max_speed = None         # most recent km/h value seen in an 0xA3 telemetry frame
        self.top_speed_raw = None          # highest live speed_raw (0xA1) seen this session -- feeds the bar's TOP_SPEED field
        self.saw_telemetry = False         # whether ANY binary AA..BB frame has arrived yet this session
        self.verbose = verbose
        self.csv_file = None               # optional open file handle for CSV logging (set by cmd_log)
        self.status_bar = None             # optional StatusBar instance for a pinned bottom summary line (set by cmd_monitor/cmd_log)

    # __aenter__: async context-manager entry. Connects and subscribes to
    # notifications. Returns self, per the standard `async with X() as x`
    # protocol. NOTE: client.start_notify() is what performs the CCCD
    # ("enable notifications") descriptor write behind the scenes -- skip
    # this step and the bike will silently never reply to anything.
    async def __aenter__(self):
        await self.client.connect()
        await self.client.start_notify(NOTIFY_CHAR, self._on_notify)
        return self

    # __aexit__: async context-manager exit. Best-effort unsubscribe (a
    # bike that already dropped the link will raise here, which we ignore)
    # followed by disconnect. Args are the standard (exc_type, exc, tb)
    # triple, unused because we don't want to suppress exceptions.
    async def __aexit__(self, *a):
        try:
            await self.client.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass
        await self.client.disconnect()

    # _on_notify(_sender, data)
    #   Usage: registered with bleak as the notify callback; bleak calls
    #     this itself whenever the bike pushes data on NOTIFY_CHAR. Not
    #     meant to be called directly.
    #   Args:
    #     _sender -- the characteristic/sender bleak associates with the
    #               notification; unused (there's only one NOTIFY_CHAR).
    #     data (bytes-like) -- the raw notification payload.
    #   Returns: None. Side effects: updates
    #     last_max_speed/top_speed_raw/saw_telemetry, optionally appends a
    #     CSV row, optionally redraws the pinned status bar, optionally
    #     prints, and enqueues the decoded line onto self.rx_queue for
    #     _await() to consume.
    def _on_notify(self, _sender, data):
        data = bytes(data)
        is_binary_frame = data[:1] == bytes([START]) and data[-1:] == bytes([END])
        if is_binary_frame:
            self.saw_telemetry = True
            if data[1:2] == bytes([0xA3]) and len(data) > 3:
                self.last_max_speed = data[3]          # cache the read-back value for get_max_speed()
            if self.csv_file or self.status_bar:
                parsed_a1 = parse_a1(data)
                parsed_a4 = parse_a4(data)
                battery, speed_raw, speed_limit = parsed_a1 if parsed_a1 else ("", "", "")
                brake, light = (parsed_a4[0], parsed_a4[1]) if parsed_a4 else ("", "")
                cruise = data[4] if (data[1] == 0xA2 and len(data) >= 9) else ""
                if self.csv_file:
                    import time
                    self.csv_file.write(f"{time.time():.3f},{TELEMETRY.get(data[1], hex(data[1]))},"
                                         f"{battery},{speed_raw},{speed_limit},{brake},{light},{cruise},{data.hex()}\n")
                    self.csv_file.flush()
                if self.status_bar:
                    # Only include fields THIS frame actually carries --
                    # the bar itself remembers the last known value for
                    # every other field (see StatusBar.update()).
                    bar_fields = {}
                    if parsed_a1:
                        bar_fields["BATTERY"] = f"{battery}%"
                        bar_fields["SPEED_RAW"] = str(speed_raw)
                        if self.top_speed_raw is None or speed_raw > self.top_speed_raw:
                            self.top_speed_raw = speed_raw          # track the session-high live speed reading
                        bar_fields["TOP_SPEED"] = str(self.top_speed_raw)
                    if data[1] == 0xA3 and len(data) > 3:
                        bar_fields["MAX"] = f"{data[3]} km/h ({data[3]*0.6214:.1f} mph)"
                    if parsed_a4:
                        bar_fields["BRAKE"] = "YES" if brake else "-"
                        bar_fields["LIGHT"] = "YES" if light else "-"
                    if data[1] == 0xA2 and len(data) >= 9:
                        bar_fields["CRUISE"] = str(cruise)
                    if bar_fields:
                        self.status_bar.update(bar_fields)
        decoded_line = decode_notify(data)
        if self.verbose:
            print("  <-", decoded_line)
        # High-frequency telemetry (A1/A2/A4/A7) is deliberately NOT queued
        # to self.rx_queue -- it streams constantly and would drown out the
        # request/response matching _await() does for AT commands. The A3
        # max-speed confirmation and all ASCII replies still get queued.
        if not (is_binary_frame and data[1] in (0xA1, 0xA2, 0xA4, 0xA7)):
            self.rx_queue.put_nowait(decoded_line)

    # _write(payload)
    #   Usage: await self._write(b"...")
    #     Low-level write to the command characteristic.
    #   Args:
    #     payload (bytes) -- raw bytes to send (an AT command line or a
    #                        binary AA..BB frame).
    #   Returns: None. response=True means this is a GATT Write Request
    #     (acknowledged), matching what the real app does.
    async def _write(self, payload):
        await self.client.write_gatt_char(WRITE_CHAR, payload, response=True)

    # _await(contains, timeout)
    #   Usage: line = await self._await("CODE_OK")
    #     Waits for a decoded notification line containing `contains`,
    #     draining and discarding any non-matching lines it sees along the
    #     way, until `timeout` seconds have elapsed IN TOTAL (the deadline
    #     is computed once up front, not reset by each unrelated line).
    #   Args:
    #     contains (str)  -- substring to look for in each queued line.
    #     timeout (float) -- total seconds to wait before giving up.
    #   Returns:
    #     str  -- the first matching line, or
    #     None -- if the timeout elapses with no match.
    async def _await(self, contains, timeout=3.0):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                queued_line = await asyncio.wait_for(self.rx_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if not self.verbose:
                print("  <-", queued_line)              # verbose mode already printed it in _on_notify
            if contains in queued_line:
                return queued_line

    # at(cmd, expect, timeout, echo)
    #   Usage: reply = await bolt.at("GETDEVID", expect="imoogoo")
    #     Sends one AT-style text command and waits for a matching reply.
    #   Args:
    #     cmd (str)      -- command text, WITHOUT the \r\n terminator (added
    #                       here, since the protocol requires it on every
    #                       text command).
    #     expect (str)   -- substring the reply line must contain.
    #     timeout (float)-- seconds to wait for a matching reply.
    #     echo (bool)    -- if True, print the outgoing command.
    #   Returns: str | None -- see _await().
    async def at(self, cmd, expect="[ascii]", timeout=3.0, echo=True):
        if echo:
            print("  ->", cmd)
        await self._write(cmd.encode() + b"\r\n")
        return await self._await(expect, timeout)

    # oracle(hex_value, echo)
    #   Usage: token = await bolt.oracle("14cc500d98bc")
    #     Asks the bike to compute f(hex_value) via the "+PA<" command --
    #     the same transform pm_token() implements locally.
    #   WARNING: calling this invalidates any pending "+PM" nonce (see
    #     pair()/pm_token()). Only call it BEFORE a +PM challenge has been
    #     issued, or well after a handshake has completed -- never in
    #     between receiving a "+PM>NONCE" and sending "+PM<token".
    #   Args:
    #     hex_value (str) -- hex string to query the oracle with.
    #     echo (bool)     -- passed through to at(); default False to keep
    #                       bulk-collection output quiet (see cmd_collect).
    #   Returns:
    #     int  -- the oracle's answer, parsed from "+PA><hex>", or
    #     None -- if the bike didn't answer with a parseable "+PA>" line.
    async def oracle(self, hex_value, echo=False):
        reply = await self.at("+PA<" + hex_value, expect="+PA>", timeout=3.0, echo=echo)
        if not reply or "+PA>" not in reply:
            return None
        try:
            return int(reply.split("+PA>", 1)[1].strip(), 16)
        except ValueError:
            return None

    # login()
    #   Usage: ok = await bolt.login()
    #     Runs the plaintext identity/password step every connection needs:
    #     GETDEVID followed by CODE=<password>. Does NOT perform the +PM
    #     mutual-auth pairing -- see handshake()/pair() for that.
    #   Args: (none; uses self.password)
    #   Returns: bool -- True if the bike replied CODE_OK, else False (and
    #     prints a diagnostic).
    async def login(self):
        await self.at("GETDEVID", expect="imoogoo", timeout=3.0)
        if not await self.at("CODE=" + self.password, expect="CODE_OK", timeout=3.0):
            print("  !! no CODE_OK -- password auth failed")
            return False
        return True

    # handshake(pair)
    #   Usage: ok = await bolt.handshake()
    #     Convenience wrapper: login(), then pair() unless disabled.
    #   Args:
    #     pair (bool) -- if False, skip the +PM mutual-auth step and return
    #                    True right after a successful login() (useful for
    #                    commands like `collect` that need the oracle to
    #                    stay usable, which a completed +PM pairing may not
    #                    require, but which don't need +PM at all).
    #   Returns: bool -- True if all requested steps succeeded.
    async def handshake(self, pair=True):
        if not await self.login():
            return False
        return await self.pair() if pair else True

    # pair()
    #   Usage: ok = await bolt.pair()
    #     Performs the "+PM" mutual-authentication challenge/response:
    #     request a nonce, compute the answer LOCALLY via pm_token() (no
    #     other bike traffic in between -- see pm_token()'s docstring-
    #     comment for why), and send it back.
    #   Args: (none)
    #   Returns: bool -- True if the bike accepted the token ("+PM>OK"),
    #     False if no challenge arrived or the token was rejected.
    async def pair(self):
        challenge_reply = await self.at("+PM?", expect="+PM>", timeout=3.0)
        if not challenge_reply or "+PM>" not in challenge_reply:
            print("  !! no +PM challenge")
            return False
        nonce = challenge_reply.split("+PM>", 1)[1].strip().lower()
        token = pm_token(nonce)                          # computed offline -- no BLE round-trip, so the nonce can't go stale
        confirmation = await self.at("+PM<" + f"{token:08x}", expect="+PM>OK", timeout=3.0)
        if confirmation:
            print("  ** paired: +PM answered from local transform")
            return True
        print("  !! +PM rejected the token")
        return False

    # get_max_speed(timeout)
    #   Usage: kmh = await bolt.get_max_speed()
    #     Does NOT send any request -- the bike streams 0xA3 telemetry on
    #     its own schedule, and _on_notify() caches the latest value in
    #     self.last_max_speed as frames arrive. This just polls that cache.
    #   Args:
    #     timeout (float) -- max seconds to wait for a fresh 0xA3 frame.
    #   Returns: int | None -- current max speed in km/h, or None if no
    #     0xA3 frame arrived within `timeout`. Resets the cache to None
    #     first, so a call can't return a value left over from before it
    #     was invoked.
    async def get_max_speed(self, timeout=5.0):
        self.last_max_speed = None
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while self.last_max_speed is None and loop.time() < deadline:
            await asyncio.sleep(0.1)
        return self.last_max_speed

    # set_max_speed(kmh)
    #   Usage: confirmed_kmh = await bolt.set_max_speed(30)
    #     Sends the binary "set max speed" command, then waits for the
    #     bike's own 0xA3 read-back to see what it actually accepted.
    #   Args:
    #     kmh (int) -- desired max speed in km/h (0-255; the protocol's
    #                  payload is a single byte, masked with & 0xFF).
    #   Returns: int | None -- the km/h value the bike reports back via
    #     0xA3 (may be lower than requested if firmware clamps it), or None
    #     if no confirmation frame arrived at all.
    async def set_max_speed(self, kmh):
        frame = build_frame(TYPE_SET_SPEED, bytes([kmh & 0xFF]))
        print(f"  -> set max speed {kmh} km/h ({kmh*0.6214:.1f} mph):  {frame.hex(' ')}")
        await self._write(frame)
        return await self.get_max_speed()


# ---------------------------------------------------------------------------
# resolve(address, name_hint)
#   Usage: addr = await resolve(address, name_hint)
#     Turns "what the user/config told us" into a concrete address to
#     connect to, scanning as a last resort.
#   Args:
#     address (str | None)  -- already-known address; if truthy, returned
#                              immediately with no BLE scan performed.
#     name_hint (str)       -- substring matched case-insensitively against
#                              each scanned device's advertised name, used
#                              only when `address` is falsy.
#   Returns: str -- a connectable BLE address.
#   Raises: SystemExit -- if no address was given and no matching device is
#     found within the scan timeout.
async def resolve(address, name_hint):
    if address:
        return address
    print(f"no saved address -- scanning for '{name_hint}'...")
    device = await BleakScanner.find_device_by_filter(
        lambda candidate, _advertisement: name_hint.lower() in (candidate.name or "").lower(), timeout=15.0)
    if not device:
        raise SystemExit(
            f"device not found. Run `pair` to discover and save a bike, "
            f"or pass --address / --name-hint explicitly.")
    print(f"found {device.name} -> {device.address}")
    return device.address


# cmd_selftest()
#   Usage: cmd_selftest()            (invoked by `python OpenJetBolt.py selftest`)
#     Validates pm_token() against KNOWN_VECTORS. No bike connection is
#     made or needed -- pure offline math check.
#   Args: (none)
#   Returns: None. Side effect: prints one line per vector plus a summary.
def cmd_selftest():
    all_passed = True
    for nonce_hex, expected_hex in KNOWN_VECTORS:
        computed = pm_token(nonce_hex)
        passed = (computed == int(expected_hex, 16))
        all_passed &= passed
        print(f"  {nonce_hex} -> {computed:08x} exp {expected_hex} {'OK' if passed else 'FAIL'}")
    print("  ALL VECTORS PASS -- pm_token is correct" if all_passed else "  some FAILED")


# cmd_collect(address, password, name_hint, count)
#   Usage: invoked by `python OpenJetBolt.py collect [count]`
#     Connects, logs in (no +PM pairing needed), and queries the "+PA"
#     oracle with `count` random 6-byte values, printing each pair in a
#     form that can be pasted straight into KNOWN_VECTORS for future
#     selftest coverage.
#   Args:
#     address, password, name_hint -- resolved bike settings (see
#       resolve_settings()).
#     count (int) -- number of random oracle queries to make.
#   Returns: None. Side effect: prints one Python tuple literal per pair.
async def cmd_collect(address, password, name_hint, count):
    async with Bolt(await resolve(address, name_hint), password) as bolt:
        if not await bolt.login():
            return
        print(f"  collecting {count} oracle pairs...")
        for _ in range(count):
            random_hex = bytes(random.randrange(256) for _ in range(6)).hex()
            token = await bolt.oracle(random_hex)
            print(f'    ("{random_hex}", "{token:08x}"),' if token is not None else f"    {random_hex} -> NK")


# cmd_scan()
#   Usage: invoked by `python OpenJetBolt.py scan`
#     Lists every BLE device visible right now, regardless of name --
#     useful for finding a Bolt's advertised name if it doesn't contain
#     "Bolt", or for general BLE debugging.
#   Args: (none)
#   Returns: None. Side effect: prints one line per discovered device.
async def cmd_scan():
    for address, (device, advertisement) in (await BleakScanner.discover(timeout=12.0, return_adv=True)).items():
        print(f"  {address}  rssi={advertisement.rssi:>4}  {device.name or advertisement.local_name or '(no name)'}")


# cmd_pair(name_hint)
#   Usage: invoked by `python OpenJetBolt.py pair`
#     Interactive first-time setup for a new bike: scan for devices whose
#     name matches `name_hint`, let the user pick one if there are several,
#     prompt for the password, VERIFY it against the real bike with
#     login() before saving anything, then persist address+password+
#     name_hint to CONFIG_PATH as the new default.
#   Args:
#     name_hint (str) -- substring to match against advertised device names.
#   Returns: None. Side effects: prints, reads from stdin (input()), writes
#     CONFIG_PATH on success.
#   Raises: SystemExit -- if no device matches, or if the entered password
#     is rejected by the bike (nothing is saved in that case).
async def cmd_pair(name_hint):
    print(f"scanning for devices matching '{name_hint}' (12s)...")
    found = await BleakScanner.discover(timeout=12.0, return_adv=True)
    candidates = [
        (address, device, advertisement) for address, (device, advertisement) in found.items()
        if name_hint.lower() in ((device.name or advertisement.local_name or "").lower())
    ]
    if not candidates:
        print(f"  no devices matched '{name_hint}'. All devices seen:")
        for address, (device, advertisement) in found.items():
            print(f"    {address}  rssi={advertisement.rssi:>4}  {device.name or advertisement.local_name or '(no name)'}")
        raise SystemExit("no matching bike found -- try --name-hint or run `scan`")

    print("  candidates:")
    for choice_idx, (candidate_address, device, advertisement) in enumerate(candidates):
        print(f"    [{choice_idx}] {candidate_address}  rssi={advertisement.rssi:>4}  {device.name or advertisement.local_name}")
    if len(candidates) == 1:
        selected_idx = 0                                # only one match -- no need to prompt
    else:
        selected_idx = int(input(f"  select a device [0-{len(candidates)-1}]: ").strip())
    address = candidates[selected_idx][0]

    password = input(f"  6-digit password [{DEFAULT_PASSWORD}]: ").strip() or DEFAULT_PASSWORD

    print(f"  verifying login against {address}...")
    async with Bolt(address, password) as bolt:
        if not await bolt.login():
            raise SystemExit("  password rejected -- not saving. Re-run `pair` with the correct code.")

    # Only reached if login() above succeeded -- merge into (not replace)
    # any existing config so unrelated saved keys survive.
    config = load_config()
    config.update({"address": address, "password": password, "name_hint": name_hint})
    save_config(config)
    print(f"  saved default bike to {CONFIG_PATH}")


# cmd_config(args)
#   Usage: invoked by `python OpenJetBolt.py config show|set|clear`
#     Inspect or hand-edit the saved defaults without going through BLE
#     discovery (unlike `pair`, this never talks to a bike).
#   Args:
#     args (argparse.Namespace) -- must have .config_action ("show"/"set"/
#       "clear") and, for "set", optional .address/.password/.name_hint.
#   Returns: None. Side effects: prints (show), writes CONFIG_PATH (set),
#     or deletes CONFIG_PATH (clear).
def cmd_config(args):
    if args.config_action == "show":
        config = load_config()
        if not config:
            print(f"  no saved config at {CONFIG_PATH}")
            return
        for key, value in config.items():
            print(f"  {key} = {value}")
    elif args.config_action == "set":
        config = load_config()
        if args.address:
            config["address"] = args.address
        if args.password:
            config["password"] = args.password
        if args.name_hint:
            config["name_hint"] = args.name_hint
        save_config(config)
        print(f"  saved to {CONFIG_PATH}")
    elif args.config_action == "clear":
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
            print(f"  removed {CONFIG_PATH}")
        else:
            print("  no config file to remove")


# cmd_monitor(address, password, name_hint, seconds)
#   Usage: invoked by `python OpenJetBolt.py monitor [seconds]`
#     Connects, completes the full handshake (login + +PM pairing), then
#     prints every decoded notification (verbose=True) for `seconds`, with
#     a pinned battery/speed/max-speed/brake/light/cruise summary line kept
#     fixed at the bottom of the terminal (see class StatusBar) while the
#     decoded stream keeps scrolling above it.
#   Args:
#     address, password, name_hint -- resolved bike settings.
#     seconds (int) -- how long to sit and watch telemetry.
#   Returns: None. Side effect: prints a live decoded stream.
async def cmd_monitor(address, password, name_hint, seconds):
    async with Bolt(await resolve(address, name_hint), password, verbose=True) as bolt:
        await bolt.handshake()
        print(f"monitoring {seconds}s...")
        with StatusBar(STATUS_BAR_FIELDS) as bar:
            bolt.status_bar = bar
            await asyncio.sleep(seconds)
        if not bolt.saw_telemetry:
            print("  (no binary telemetry)")


# cmd_log(address, password, name_hint, seconds, path)
#   Usage: invoked by `python OpenJetBolt.py log [seconds] [path]`
#     Like cmd_monitor(), but also writes every telemetry frame as a CSV
#     row (via Bolt.csv_file / _on_notify) so specific events (e.g. "I
#     squeezed the brake at this timestamp") can be correlated after the
#     fact, and shows the same pinned bottom summary line as cmd_monitor()
#     (see class StatusBar). The caller is expected to perform ONE labeled
#     action at a time while this runs.
#   Args:
#     address, password, name_hint -- resolved bike settings.
#     seconds (int) -- recording duration.
#     path (str)    -- output CSV file path (overwritten if it exists).
#   Returns: None. Side effects: creates/overwrites `path`; prints status.
async def cmd_log(address, password, name_hint, seconds, path):
    with open(path, "w") as csv_file:
        csv_file.write("epoch,type,battery,speed_raw,cap,brake,light,cruise,raw_hex\n")
        async with Bolt(await resolve(address, name_hint), password, verbose=True) as bolt:
            if not await bolt.handshake():
                return
            bolt.csv_file = csv_file                    # from here on, _on_notify() appends a row per telemetry frame
            print(f"logging {seconds}s to {path} -- perform ONE labeled action at a time")
            with StatusBar(STATUS_BAR_FIELDS) as bar:
                bolt.status_bar = bar
                await asyncio.sleep(seconds)
    print(f"  wrote {path}")


# cmd_info(address, password, name_hint)
#   Usage: invoked by `python OpenJetBolt.py info`
#     Connects, handshakes, fires a batch of read-only AT queries to
#     populate a human-readable snapshot (firmware version, mode, units,
#     lock, cruise, headlight), then prints the current max speed.
#   Args: address, password, name_hint -- resolved bike settings.
#   Returns: None. Side effect: prints the queries' replies and a summary.
async def cmd_info(address, password, name_hint):
    async with Bolt(await resolve(address, name_hint), password) as bolt:
        if not await bolt.handshake():
            return
        for query_cmd in ("+VER?", "+MODE=?", "+UNIT=?", "+LOCK=?", "+CRZE=?", "HLGT=?"):
            await bolt.at(query_cmd, timeout=2.0)
        max_speed = await bolt.get_max_speed()
        print(f"\n  current max speed = {max_speed} km/h ({max_speed*0.6214:.1f} mph)" if max_speed is not None
              else "\n  no 0xA3 telemetry")


# cmd_get(address, password, name_hint)
#   Usage: invoked by `python OpenJetBolt.py get`
#     Connects, handshakes, and prints just the current max speed -- a
#     quieter version of cmd_info() for scripting.
#   Args: address, password, name_hint -- resolved bike settings.
#   Returns: None. Side effect: prints the current max speed (or "no
#     read-back" if no 0xA3 frame arrived).
async def cmd_get(address, password, name_hint):
    async with Bolt(await resolve(address, name_hint), password) as bolt:
        await bolt.handshake()
        max_speed = await bolt.get_max_speed()
        print(f"max speed = {max_speed} km/h ({max_speed*0.6214:.1f} mph)" if max_speed is not None else "no read-back")


# cmd_set(address, password, name_hint, kmh)
#   Usage: invoked by `python OpenJetBolt.py set <km/h>`
#     Connects, handshakes, sends the set-max-speed command, and reports
#     whether the bike confirmed, clamped, or didn't respond.
#   Args:
#     address, password, name_hint -- resolved bike settings.
#     kmh (int) -- requested max speed in km/h.
#   Returns: None. Side effect: prints the outcome.
async def cmd_set(address, password, name_hint, kmh):
    async with Bolt(await resolve(address, name_hint), password) as bolt:
        if not await bolt.handshake():
            return
        confirmed_speed = await bolt.set_max_speed(kmh)
        if confirmed_speed is None:
            print("  frame sent, but no 0xA3 confirmation arrived.")
        elif confirmed_speed == kmh:
            print(f"  confirmed: controller accepted {confirmed_speed} km/h")
        else:
            print(f"  controller CLAMPED {kmh} -> {confirmed_speed} km/h (firmware ceiling = {confirmed_speed})")


# ---------------------------------------------------------------------------
# build_parser()
#   Usage: parser = build_parser()
#     Builds the full argparse CLI: global --address/--password/--name-hint
#     overrides (available before the subcommand name), plus one
#     subparser per command. `config` gets its own nested sub-subparsers
#     (show/set/clear).
#   Args: (none)
#   Returns: argparse.ArgumentParser -- ready to call .parse_args() on.
def build_parser():
    # description=__doc__ reuses the module docstring at the top of this
    # file as the --help text; RawDescriptionHelpFormatter preserves its
    # manual line breaks instead of re-wrapping them.
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address", "-a", help="BLE address/UUID of the bike (overrides saved config)")
    parser.add_argument("--password", "-p", help="6-digit CODE= password (overrides saved config, default 000000)")
    parser.add_argument("--name-hint", "-n", help="substring to match in the device name when scanning (default 'Bolt')")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("selftest", help="validate pm_token() offline (no bike)")

    collect_parser = subparsers.add_parser("collect", help="dump n oracle pairs")
    collect_parser.add_argument("count", nargs="?", type=int, default=20)   # positional, optional -- defaults to 20 pairs

    subparsers.add_parser("scan", help="list nearby BLE devices")
    subparsers.add_parser("pair", help="discover a bike, verify its password, and save it as the default")

    # `config` has its own nested subcommands (show/set/clear), separate
    # from the top-level --address/--password/--name-hint flags above --
    # these `-a`/`-p`/`-n` only apply to `config set`, writing straight to
    # CONFIG_PATH rather than overriding a single command's behavior.
    config_parser = subparsers.add_parser("config", help="view or edit saved defaults")
    config_subparsers = config_parser.add_subparsers(dest="config_action", required=True)
    config_subparsers.add_parser("show")
    config_set_parser = config_subparsers.add_parser("set")
    config_set_parser.add_argument("--address", "-a")
    config_set_parser.add_argument("--password", "-p")
    config_set_parser.add_argument("--name-hint", "-n")
    config_subparsers.add_parser("clear")

    monitor_parser = subparsers.add_parser("monitor", help="print live telemetry")
    monitor_parser.add_argument("seconds", nargs="?", type=int, default=30)

    log_parser = subparsers.add_parser("log", help="record telemetry to CSV")
    log_parser.add_argument("seconds", nargs="?", type=int, default=30)
    log_parser.add_argument("path", nargs="?", default="jetson_telemetry.csv")

    subparsers.add_parser("info", help="print firmware/config info and current max speed")
    subparsers.add_parser("get", help="print current max speed")

    set_parser = subparsers.add_parser("set", help="set max speed (km/h)")
    set_parser.add_argument("kmh", type=int)                          # required positional -- no sensible default for a target speed

    return parser


# main()
#   Usage: entry point, called from `if __name__ == "__main__"` below.
#     Parses argv, then dispatches to the matching cmd_*() function.
#     Commands that don't need a bike (selftest/scan/config) are handled
#     before resolve_settings() runs, so they work with zero saved config
#     and never attempt a BLE scan. Every other command resolves
#     address/password/name_hint first and passes them through.
#   Args: (none; reads sys.argv)
#   Returns: None.
def main():
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] or ["selftest"])  # bare `python OpenJetBolt.py` -> selftest

    if args.command == "selftest":
        cmd_selftest()
        return
    if args.command == "scan":
        asyncio.run(cmd_scan())
        return
    if args.command == "config":
        cmd_config(args)
        return

    address, password, name_hint = resolve_settings(args)

    if args.command == "pair":
        asyncio.run(cmd_pair(name_hint))
    elif args.command == "collect":
        asyncio.run(cmd_collect(address, password, name_hint, args.count))
    elif args.command == "monitor":
        asyncio.run(cmd_monitor(address, password, name_hint, args.seconds))
    elif args.command == "log":
        asyncio.run(cmd_log(address, password, name_hint, args.seconds, args.path))
    elif args.command == "info":
        asyncio.run(cmd_info(address, password, name_hint))
    elif args.command == "get":
        asyncio.run(cmd_get(address, password, name_hint))
    elif args.command == "set":
        asyncio.run(cmd_set(address, password, name_hint, args.kmh))


if __name__ == "__main__":
    main()
