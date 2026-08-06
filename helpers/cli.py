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
import random
import sys

from bleak import BleakScanner

from .auth import KNOWN_VECTORS, pm_token
from .config import CONFIG_PATH, DEFAULT_PASSWORD, load_config, resolve_settings, save_config
from .transport import Bolt, resolve
from .ui import STATUS_BAR_FIELDS, StatusBar


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
#       config.resolve_settings()).
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
#     fixed at the bottom of the terminal (see ui.StatusBar) while the
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
#     row (via Bolt.csv_file / Bolt._on_notify) so specific events (e.g. "I
#     squeezed the brake at this timestamp") can be correlated after the
#     fact, and shows the same pinned bottom summary line as cmd_monitor()
#     (see ui.StatusBar). The caller is expected to perform ONE labeled
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
#   Usage: the actual entry point -- OpenJetBolt.py at the repo root just
#     does `from helpers.cli import main; main()`. Parses argv, then
#     dispatches to the matching cmd_*() function.
#     Commands that don't need a bike (selftest/scan/config) are handled
#     before config.resolve_settings() runs, so they work with zero saved
#     config and never attempt a BLE scan. Every other command resolves
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
