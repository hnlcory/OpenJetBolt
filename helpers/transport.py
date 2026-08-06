"""
helpers.transport -- the async BLE session. class Bolt wraps a
BleakClient plus the AT-command/binary-frame protocol from protocol.py:
login, mutual-auth pairing, telemetry parsing, and the set-max-speed
command. resolve() turns a saved/typed address (or a name-hint scan) into
a concrete address to hand to Bolt.
"""

import asyncio
import time

from bleak import BleakClient, BleakScanner

from .auth import pm_token
from .config import DEFAULT_PASSWORD
from .protocol import (
    END,
    NOTIFY_CHAR,
    START,
    TELEMETRY,
    TYPE_SET_SPEED,
    WRITE_CHAR,
    build_frame,
    decode_notify,
    parse_a1,
    parse_a4,
)


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
        self.csv_file = None               # optional open file handle for CSV logging (set by cli.cmd_log)
        self.status_bar = None             # optional StatusBar instance for a pinned bottom summary line (set by cli.cmd_monitor/cli.cmd_log)

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
    #     the same transform auth.pm_token() implements locally.
    #   WARNING: calling this invalidates any pending "+PM" nonce (see
    #     pair(), below, and auth.pm_token()). Only call it BEFORE a +PM
    #     challenge has been issued, or well after a handshake has
    #     completed -- never in between receiving a "+PM>NONCE" and
    #     sending "+PM<token".
    #   Args:
    #     hex_value (str) -- hex string to query the oracle with.
    #     echo (bool)     -- passed through to at(); default False to keep
    #                       bulk-collection output quiet (see cli.cmd_collect).
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
    #     request a nonce, compute the answer LOCALLY via auth.pm_token()
    #     (no other bike traffic in between -- see pm_token()'s
    #     docstring-comment for why), and send it back.
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
