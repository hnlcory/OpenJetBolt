# Unit tests for helpers/auth.py -- the +PM auth transform.
#
# Pure/deterministic math, no I/O and nothing to mock: these tests are the
# strongest regression net on the crypto, since KNOWN_VECTORS were captured
# live from a real bike's +PA oracle.

import pytest

from helpers.auth import KNOWN_VECTORS, _bonding_key, pm_token


# For every one of the 12 real nonce/token pairs captured from a bike,
# pm_token() must reproduce the exact expected token. This is the main
# regression guard: if the crypto ever gets refactored/broken, this is what
# catches it.
@pytest.mark.parametrize("nonce_hex, expected_hex", KNOWN_VECTORS)
def test_pm_token_matches_known_vectors(nonce_hex, expected_hex):
    assert pm_token(nonce_hex) == int(expected_hex, 16)


# pm_token() is pure and stateless -- calling it twice with the same input
# must always give the same output (no hidden randomness/state).
def test_pm_token_is_deterministic():
    nonce_hex, _ = KNOWN_VECTORS[0]
    assert pm_token(nonce_hex) == pm_token(nonce_hex)


# The nonce hex string is parsed with int(..., 16), which accepts either
# case -- confirm an uppercase nonce still produces the correct token.
def test_pm_token_accepts_uppercase_hex():
    nonce_hex, expected_hex = KNOWN_VECTORS[0]
    assert pm_token(nonce_hex.upper()) == int(expected_hex, 16)


# The token is documented as a 32-bit big-endian int -- guard against a
# future change accidentally returning something out of that range (e.g. a
# sign flip or an extra byte).
def test_pm_token_returns_a_32_bit_int():
    for nonce_hex, _ in KNOWN_VECTORS:
        token = pm_token(nonce_hex)
        assert isinstance(token, int)
        assert 0 <= token <= 0xFFFFFFFF


# _bonding_key() takes no arguments and is only a function of the module's
# fixed constants, so it must return the same 16-byte list every time it's
# called, and every byte must be a valid byte value (0-255).
def test_bonding_key_shape_and_determinism():
    key_a = _bonding_key()
    key_b = _bonding_key()
    assert key_a == key_b
    assert len(key_a) == 16
    assert all(isinstance(byte_val, int) and 0 <= byte_val <= 255 for byte_val in key_a)
