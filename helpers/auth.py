"""
helpers.auth -- the +PM auth transform, ported verbatim from
BleEncryption.encryptionStringOfValue() in the Jetson app. Validated against
12 captured nonce->token pairs (all pass, see KNOWN_VECTORS / cli.cmd_selftest()).
Shared by every Jetson Bolt -- it's a property of the app/firmware, not a
per-device secret.
"""

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
#     (see Bolt.pair() and Bolt.oracle() in transport.py).
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
# bike's +PA oracle. Used only by cli.cmd_selftest() to validate pm_token()
# offline, with no bike required -- run `python OpenJetBolt.py selftest` any
# time you touch the crypto above.
KNOWN_VECTORS = [
    ("14cc500d98bc", "71f470f0"), ("ff767f0e785b", "eb79a1f1"), ("34e3fe06412f", "2d77f243"),
    ("eabcb14c4c8d", "f9c6378d"), ("a8793d864a2c", "9a0a5587"), ("d6d763743161", "d52fb65c"),
    ("5b8a48c7b7a3", "ba00d303"), ("15cc485d698f", "0c0421d9"), ("8b60bb9a0f41", "d99a2100"),
    ("cfddf4df3f67", "4810b6db"), ("28bd031cd632", "16024b35"), ("bb818d7e3559", "27b6bc38"),
]
