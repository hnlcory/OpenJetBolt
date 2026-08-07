# Unit tests for helpers/transport.py -- the async BLE session (class Bolt)
# and resolve(). bleak's BleakClient/BleakScanner are fully mocked; no real
# BLE hardware access happens anywhere in this file.

import asyncio
import io

import pytest
from unittest.mock import AsyncMock, MagicMock

from helpers import transport
from helpers.protocol import NOTIFY_CHAR, TYPE_SET_SPEED, WRITE_CHAR, build_frame


# Helper (not a test itself): builds a Bolt with BleakClient replaced by a
# fully-mocked async client, so every test below can construct a Bolt
# without ever touching real BLE hardware.
def make_bolt(mocker, address="AA:BB:CC:DD:EE:FF", password="000000", verbose=False):
    mock_client = MagicMock(name="BleakClient")
    mock_client.connect = AsyncMock()
    mock_client.start_notify = AsyncMock()
    mock_client.stop_notify = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_client.write_gatt_char = AsyncMock()
    mocker.patch.object(transport, "BleakClient", return_value=mock_client)
    bolt = transport.Bolt(address, password, verbose=verbose)
    return bolt, mock_client


# ---------------------------------------------------------------------------
# __aenter__ / __aexit__
# ---------------------------------------------------------------------------

# Entering the `async with Bolt(...) as bolt:` context must connect first,
# then subscribe to notifications on the right characteristic with the
# instance's own _on_notify callback -- skipping either step means the bike
# would never actually respond to anything.
async def test_aenter_connects_and_subscribes_to_notifications(mocker):
    bolt, mock_client = make_bolt(mocker)
    result = await bolt.__aenter__()
    assert result is bolt
    mock_client.connect.assert_awaited_once()
    mock_client.start_notify.assert_awaited_once_with(NOTIFY_CHAR, bolt._on_notify)


# __aexit__ must always disconnect, even if the best-effort stop_notify()
# call raises (e.g. the bike already dropped the link) -- an unsubscribe
# failure should never leave the connection dangling.
async def test_aexit_disconnects_even_if_stop_notify_raises(mocker):
    bolt, mock_client = make_bolt(mocker)
    mock_client.stop_notify.side_effect = RuntimeError("already gone")
    await bolt.__aexit__(None, None, None)
    mock_client.stop_notify.assert_awaited_once_with(NOTIFY_CHAR)
    mock_client.disconnect.assert_awaited_once()


# ---------------------------------------------------------------------------
# _on_notify
# ---------------------------------------------------------------------------

# An 0xA3 telemetry frame is the bike's own read-back of its max speed --
# _on_notify must cache that value so get_max_speed() can pick it up later.
def test_on_notify_a3_frame_updates_last_max_speed(mocker):
    bolt, _ = make_bolt(mocker)
    bolt._on_notify(None, build_frame(0xA3, bytes([25])))
    assert bolt.last_max_speed == 25


# High-frequency telemetry frames (A1/A2/A4/A7) stream constantly and would
# drown out request/response matching in _await() if queued -- confirm none
# of them end up on rx_queue.
def test_on_notify_high_frequency_frames_are_not_queued(mocker):
    bolt, _ = make_bolt(mocker)
    for frame_type in (0xA1, 0xA2, 0xA4, 0xA7):
        bolt._on_notify(None, build_frame(frame_type, bytes([0, 0, 0, 0, 0])))
    assert bolt.rx_queue.empty()


# By contrast, the A3 max-speed confirmation and plain ASCII replies ARE
# needed by _await()/at(), so they must be queued.
def test_on_notify_queues_a3_and_ascii_lines(mocker):
    bolt, _ = make_bolt(mocker)
    bolt._on_notify(None, build_frame(0xA3, bytes([25])))
    assert bolt.rx_queue.get_nowait() is not None

    bolt._on_notify(None, b"CODE_OK\r\n")
    assert bolt.rx_queue.get_nowait() == "[ascii] CODE_OK"
    assert bolt.rx_queue.empty()


# When csv_file is set (cli.cmd_log), an 0xA1 frame must append one
# correctly-formatted row (battery/speed_raw/cap in the right columns) so
# recorded rides can be analyzed afterwards.
def test_on_notify_writes_csv_row_for_a1_frame(mocker):
    bolt, _ = make_bolt(mocker)
    bolt.csv_file = io.StringIO()
    payload = bytes([80, 0x01, 0x2C, 0, 25])   # battery=80, speed_raw=300, cap=25
    bolt._on_notify(None, build_frame(0xA1, payload))
    row = bolt.csv_file.getvalue().strip()
    fields = row.split(",")
    # epoch,type,battery,speed_raw,cap,brake,light,cruise,raw_hex
    assert fields[1] == "status"
    assert fields[2] == "80"
    assert fields[3] == "300"
    assert fields[4] == "25"


# Regression test for bug 1.2's real-world trigger point: _on_notify()
# calls parse_a1()/parse_a4() unconditionally on every binary frame
# whenever csv_file or status_bar is set (see helpers/transport.py). Before
# the fix, a truncated 9-byte 0xA1 frame arriving mid-session -- entirely
# plausible on a flaky BLE link -- raised IndexError from inside this
# callback (bleak's notify callback, where an uncaught exception means
# silently stalled telemetry rather than a clean traceback). Feeding one in
# directly must not raise, and the CSV row should just show blank values
# for the fields parse_a1() couldn't extract.
def test_on_notify_does_not_crash_on_truncated_a1_frame(mocker):
    bolt, _ = make_bolt(mocker)
    bolt.csv_file = io.StringIO()
    bolt.status_bar = MagicMock()
    truncated_frame = bytes([0xAA, 0xA1, 0x09, 1, 2, 3, 4, 0x00, 0xBB])
    assert len(truncated_frame) == 9

    bolt._on_notify(None, truncated_frame)   # must not raise

    row = bolt.csv_file.getvalue().strip()
    fields = row.split(",")
    assert fields[2] == ""   # battery -- parse_a1() returned None, so blank rather than a value


# When status_bar is set (cli.cmd_monitor/cmd_log), an 0xA1 frame should
# push BATTERY/SPEED_RAW/TOP_SPEED into the bar, and TOP_SPEED must track
# the session-high reading -- a later, lower reading must not lower it.
def test_on_notify_updates_status_bar_and_tracks_session_top_speed(mocker):
    bolt, _ = make_bolt(mocker)
    bolt.status_bar = MagicMock()
    high_speed_payload = bytes([80, 0x01, 0x2C, 0, 25])   # speed_raw = 300
    low_speed_payload = bytes([80, 0x00, 0x64, 0, 25])    # speed_raw = 100

    bolt._on_notify(None, build_frame(0xA1, high_speed_payload))
    bolt._on_notify(None, build_frame(0xA1, low_speed_payload))

    assert bolt.top_speed_raw == 300   # the lower second reading doesn't lower it
    last_fields = bolt.status_bar.update.call_args_list[-1].args[0]
    assert last_fields["TOP_SPEED"] == "300"
    assert last_fields["SPEED_RAW"] == "100"


# ---------------------------------------------------------------------------
# _write / _await / at
# ---------------------------------------------------------------------------

# _write() is the one place raw bytes actually go out over BLE -- confirm
# it targets the write characteristic with response=True (a GATT Write
# Request, matching what the real app does).
async def test_write_calls_write_gatt_char_with_response_true(mocker):
    bolt, mock_client = make_bolt(mocker)
    await bolt._write(b"hello")
    mock_client.write_gatt_char.assert_awaited_once_with(WRITE_CHAR, b"hello", response=True)


# _await() should skip over unrelated queued lines and return the first one
# that actually contains the substring being waited for, having drained
# (and discarded) everything before it.
async def test_await_returns_first_matching_line_and_drains_others(mocker):
    bolt, _ = make_bolt(mocker)
    await bolt.rx_queue.put("[ascii] noise")
    await bolt.rx_queue.put("[ascii] CODE_OK")
    result = await bolt._await("CODE_OK", timeout=1.0)
    assert result == "[ascii] CODE_OK"
    assert bolt.rx_queue.empty()


# If nothing matching ever arrives, _await() must give up after `timeout`
# and return None rather than hanging forever.
async def test_await_returns_none_on_timeout(mocker):
    bolt, _ = make_bolt(mocker)
    result = await bolt._await("NEVER", timeout=0.1)
    assert result is None


# at() must append the required \r\n terminator to the command text before
# writing it, then delegate reply-matching to _await().
async def test_at_writes_command_with_crlf_terminator(mocker):
    bolt, _ = make_bolt(mocker)
    mocker.patch.object(bolt, "_write", AsyncMock())
    mocker.patch.object(bolt, "_await", AsyncMock(return_value="[ascii] CODE_OK"))
    result = await bolt.at("CODE=000000", expect="CODE_OK", timeout=1.0, echo=False)
    bolt._write.assert_awaited_once_with(b"CODE=000000\r\n")
    assert result == "[ascii] CODE_OK"


# ---------------------------------------------------------------------------
# oracle
# ---------------------------------------------------------------------------

# A well-formed "+PA><hex>" reply should be parsed into the equivalent int.
async def test_oracle_parses_hex_reply(mocker):
    bolt, _ = make_bolt(mocker)
    mocker.patch.object(bolt, "at", AsyncMock(return_value="[ascii] +PA>1a2b3c4d"))
    assert await bolt.oracle("aabbccddeeff") == 0x1A2B3C4D


# No reply at all (bike never answered) must return None, not raise.
async def test_oracle_returns_none_when_no_reply(mocker):
    bolt, _ = make_bolt(mocker)
    mocker.patch.object(bolt, "at", AsyncMock(return_value=None))
    assert await bolt.oracle("aabbccddeeff") is None


# A reply containing "+PA>" but with unparseable hex after it must return
# None (the ValueError from int(..., 16) is caught internally) rather than
# propagating an exception to the caller.
async def test_oracle_returns_none_on_malformed_hex(mocker):
    bolt, _ = make_bolt(mocker)
    mocker.patch.object(bolt, "at", AsyncMock(return_value="[ascii] +PA>not-hex"))
    assert await bolt.oracle("aabbccddeeff") is None


# ---------------------------------------------------------------------------
# login / handshake / pair
# ---------------------------------------------------------------------------

# A GETDEVID + CODE_OK reply pair means the password was accepted.
async def test_login_success(mocker):
    bolt, _ = make_bolt(mocker)
    mocker.patch.object(bolt, "at", AsyncMock(side_effect=["[ascii] imoogoo", "[ascii] CODE_OK"]))
    assert await bolt.login() is True


# If CODE_OK never comes back, login() must report failure (password
# rejected) rather than assuming success.
async def test_login_failure_when_no_code_ok(mocker):
    bolt, _ = make_bolt(mocker)
    mocker.patch.object(bolt, "at", AsyncMock(side_effect=["[ascii] imoogoo", None]))
    assert await bolt.login() is False


# By default handshake() should run login() and then pair() -- both steps
# needed for a full mutual-auth session.
async def test_handshake_calls_login_then_pair_by_default(mocker):
    bolt, _ = make_bolt(mocker)
    mocker.patch.object(bolt, "login", AsyncMock(return_value=True))
    mocker.patch.object(bolt, "pair", AsyncMock(return_value=True))
    assert await bolt.handshake() is True
    bolt.pair.assert_awaited_once()


# handshake(pair=False) is used by commands that only need login() (e.g.
# collect) -- pair() must not be called in that mode.
async def test_handshake_skips_pair_when_disabled(mocker):
    bolt, _ = make_bolt(mocker)
    mocker.patch.object(bolt, "login", AsyncMock(return_value=True))
    mocker.patch.object(bolt, "pair", AsyncMock(return_value=True))
    assert await bolt.handshake(pair=False) is True
    bolt.pair.assert_not_awaited()


# If login() fails, handshake() must short-circuit and never attempt pair()
# -- there's no point pairing with an unauthenticated session.
async def test_handshake_short_circuits_when_login_fails(mocker):
    bolt, _ = make_bolt(mocker)
    mocker.patch.object(bolt, "login", AsyncMock(return_value=False))
    mocker.patch.object(bolt, "pair", AsyncMock(return_value=True))
    assert await bolt.handshake() is False
    bolt.pair.assert_not_awaited()


# pair() must compute the token purely locally (via pm_token) from the
# lower-cased nonce the bike sent, then succeed only once the bike answers
# "+PM>OK" -- confirms both the nonce normalization and the success path.
async def test_pair_success_computes_token_locally_from_lowercased_nonce(mocker):
    bolt, _ = make_bolt(mocker)
    mocker.patch.object(
        bolt, "at", AsyncMock(side_effect=["[ascii] +PM>14CC500D98BC", "[ascii] +PM>OK"])
    )
    spy = mocker.patch("helpers.transport.pm_token", wraps=transport.pm_token)
    assert await bolt.pair() is True
    spy.assert_called_once_with("14cc500d98bc")


# If the bike never issues a "+PM>" challenge, pair() must fail cleanly
# rather than trying to compute a token from nothing.
async def test_pair_returns_false_when_no_challenge_arrives(mocker):
    bolt, _ = make_bolt(mocker)
    mocker.patch.object(bolt, "at", AsyncMock(return_value=None))
    assert await bolt.pair() is False


# If a challenge arrives but the computed token is rejected (no "+PM>OK"),
# pair() must report failure.
async def test_pair_returns_false_when_token_rejected(mocker):
    bolt, _ = make_bolt(mocker)
    mocker.patch.object(bolt, "at", AsyncMock(side_effect=["[ascii] +PM>14cc500d98bc", None]))
    assert await bolt.pair() is False


# ---------------------------------------------------------------------------
# get_max_speed / set_max_speed
# ---------------------------------------------------------------------------

# get_max_speed() must reset the cache to None up front (so a call can't
# return a stale value left over from before it was invoked), and if
# nothing new arrives before `timeout`, it must return None.
async def test_get_max_speed_clears_stale_cache_and_times_out(mocker):
    bolt, _ = make_bolt(mocker)
    bolt.last_max_speed = 99   # stale value from a previous call
    result = await bolt.get_max_speed(timeout=0.2)
    assert result is None


# If a fresh 0xA3 frame arrives (via _on_notify, simulated here directly on
# last_max_speed) while get_max_speed() is polling, it should be picked up
# well before the timeout elapses.
async def test_get_max_speed_picks_up_value_set_while_waiting(mocker):
    bolt, _ = make_bolt(mocker)

    async def set_speed_later():
        await asyncio.sleep(0.05)
        bolt.last_max_speed = 30

    task = asyncio.ensure_future(set_speed_later())
    result = await bolt.get_max_speed(timeout=2.0)
    await task
    assert result == 30


# set_max_speed() must build the correct binary frame for the requested
# km/h value, write it out, and return whatever get_max_speed() reports
# back as the bike's actual confirmed value.
async def test_set_max_speed_sends_frame_and_returns_confirmation(mocker):
    bolt, mock_client = make_bolt(mocker)
    mocker.patch.object(bolt, "get_max_speed", AsyncMock(return_value=25))
    result = await bolt.set_max_speed(25)
    assert result == 25
    expected_frame = build_frame(TYPE_SET_SPEED, bytes([25]))
    mock_client.write_gatt_char.assert_awaited_once_with(WRITE_CHAR, expected_frame, response=True)


# Regression test for bug 1.4: set_max_speed() used to build the payload
# byte with `kmh & 0xFF`, silently wrapping an out-of-range request into a
# different, valid-looking speed (300 -> 44 km/h, -5 -> 251 km/h) instead
# of rejecting it -- the wrong default for a command that controls how fast
# a vehicle goes. It must now raise ValueError for anything outside 0-255,
# and must never reach _write() (no frame should go out over BLE at all).
@pytest.mark.parametrize("bad_kmh", [300, -5, 256, -1])
async def test_set_max_speed_rejects_out_of_range_values(mocker, bad_kmh):
    bolt, mock_client = make_bolt(mocker)
    with pytest.raises(ValueError):
        await bolt.set_max_speed(bad_kmh)
    mock_client.write_gatt_char.assert_not_awaited()


# The valid range's edges (0 and 255) must still be accepted -- the fix
# should reject values OUTSIDE 0-255, not narrow the existing valid range.
@pytest.mark.parametrize("boundary_kmh", [0, 255])
async def test_set_max_speed_accepts_boundary_values(mocker, boundary_kmh):
    bolt, mock_client = make_bolt(mocker)
    mocker.patch.object(bolt, "get_max_speed", AsyncMock(return_value=boundary_kmh))
    result = await bolt.set_max_speed(boundary_kmh)
    assert result == boundary_kmh
    expected_frame = build_frame(TYPE_SET_SPEED, bytes([boundary_kmh]))
    mock_client.write_gatt_char.assert_awaited_once_with(WRITE_CHAR, expected_frame, response=True)


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

# A truthy address should be returned immediately with no BLE scan at all
# -- scanning is only a fallback when nothing is already known.
async def test_resolve_returns_address_immediately_without_scanning(mocker):
    find_mock = mocker.patch.object(transport.BleakScanner, "find_device_by_filter", AsyncMock())
    result = await transport.resolve("AA:BB:CC:DD:EE:FF", "Bolt")
    assert result == "AA:BB:CC:DD:EE:FF"
    find_mock.assert_not_called()


# With no address given, resolve() should scan and return the matched
# device's address.
async def test_resolve_scans_and_returns_matched_device_address(mocker):
    device = MagicMock()
    device.address = "11:22:33:44:55:66"
    device.name = "My Bolt"
    mocker.patch.object(transport.BleakScanner, "find_device_by_filter", AsyncMock(return_value=device))
    result = await transport.resolve(None, "Bolt")
    assert result == "11:22:33:44:55:66"


# If the scan finds nothing matching the name hint, resolve() must raise
# SystemExit (a clean, expected CLI-level failure) rather than returning
# None and letting a caller silently try to connect to nothing.
async def test_resolve_raises_system_exit_when_nothing_found(mocker):
    mocker.patch.object(transport.BleakScanner, "find_device_by_filter", AsyncMock(return_value=None))
    with pytest.raises(SystemExit):
        await transport.resolve(None, "Bolt")
