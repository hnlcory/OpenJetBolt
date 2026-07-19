# OpenJetBolt

A CLI tool that talks directly to a Jetson Bolt e-bike over Bluetooth
Low Energy (BLE), bypassing/replacing the official "Ride Jetson" iOS/Android app entirely. Reads live telemetry (battery, speed, brake/light state) and allows the bike's max speed limit to be set past the app restricted limit of 15mph.

Works with any Jetson Bolt. Connect a specific bike's BLE address and password once, 
and it remembers that as the default for future runs. The authentication is the same for every
Jetson Bolt (it comes from the shared app not the individual bike). Only the BLE address and the 6 digit password change per bike.

## Requirements

- Python 3.8+
- [bleak](https://github.com/hbldh/bleak), a cross-platform BLE library:

  ```bash
  pip install bleak
  ```
- A Bluetooth adapter supported by bleak (macOS, Linux, and Windows all work).
- The Jetson Bolt **cannot** be connected to the official Jetson app when running this script

## Quick Start

Power on a bike and run:

```bash
python3 OpenJetBolt.py pair
```
This scans for nearby devices with "Bolt" in their advertised name, lets you
pick one if more than one shows up, asks for the bike's 6-digit password
(press Enter to try the default `000000`), verifies the password against the
real bike, and (if successful) saves the address/password as your
default in `jetson_bolt_config.json` (created next to the script).

After that, every other command just works with no flags:

```bash
python3 OpenJetBolt.py info      # firmware info + current max speed
python3 OpenJetBolt.py set 30    # set max speed to 30 km/h
```

## How settings are resolved

Every bike facing command needs three things: an **address**, a **password**,
and a **name hint** (used only when scanning for an address). These resolve
in priority order:

1. `--address` / `--password` / `--name-hint` flags on the command line
2. `jetson_bolt_config.json` next to the script (written by `pair` or
   `config set`)
3. Built-in defaults - no fixed address (falls back to scanning for a device
   named "Bolt"), password `000000`

To keep a saved default and still run a one-off command for a different bike without changing your config:

```bash
python3 OpenJetBolt.py --address AA:BB:CC:DD:EE:FF --password 123456 info
```

### Global Flags

| Flag | Short | Description |
|---|---|---|
| `--address ADDRESS` | `-a` | BLE address (or macOS CoreBluetooth UUID) of the bike. Overrides the saved config. |
| `--password PASSWORD` | `-p` | 6-digit `CODE=` password for the bike. Overrides the saved config. Defaults to `000000` if nothing is saved or passed. |
| `--name-hint HINT` | `-n` | Substring to match against a device's advertised name when scanning. Only used when no address is known. Defaults to `"Bolt"`. |

## Commands

### `pair`

```bash
python3 OpenJetBolt.py pair
```

Interactive first time setup. Scans for BLE devices where name matches
`--name-hint` (default "Bolt"), lets you choose one if several are found,
prompts for the password, confirms it actually works against the real bike,
and saves address + password + name hint as the new default. 

### `config`

```bash
python3 OpenJetBolt.py config show

python3 OpenJetBolt.py config set [--address ADDRESS] [--password PASSWORD] [--name-hint HINT]

python3 OpenJetBolt.py config clear
```

View, edit, or delete the saved defaults directly, without doing any
BLE scanning. `config set` only updates the fields you pass and leaves the rest alone. `config clear` deletes the config file.

### `scan`

```bash
python3 OpenJetBolt.py scan
```

Lists every BLE device currently visible regardless of name. Useful if bike's advertised name doesn't contain "Bolt".

### `info`

```bash
python3 OpenJetBolt.py info
```

Connects, logs in, completes the mutual auth handshake, queries a batch of read only settings (firmware version, mode, units, lock, cruise, headlight), and prints the bike's current max speed limit.

### `get`

```bash
python3 OpenJetBolt.py get
```

Like `info`, but only prints the current max speed limit.

### `set <km/h>`

```bash
python3 OpenJetBolt.py set 30
```

Sends the "set max speed" command and waits for the bike's  confirmation. 3 possible outcomes:

- **confirmed** - the bike accepted exactly the value you sent

- **clamped** - the bike's firmware capped it lower than requested (the reported value is the actual new ceiling)

- **no confirmation** - the frame was sent but no telemetry read back arrived in time

### `monitor [seconds]`

```bash
python3 OpenJetBolt.py monitor        # 30 seconds (default)

python3 OpenJetBolt.py monitor 60
```

Connects, authenticates, and prints every decoded notification live for the
given duration (default 30s). While running, a summary bar stays
at the bottom of the terminal (see [Status Bar](#status-bar-while-monitoringlogging) below).

### `log [seconds] [path]`

```bash
python3 OpenJetBolt.py log                          # 30s -> jetson_telemetry.csv
python3 OpenJetBolt.py log 60 ride1.csv
```

Same as `monitor`, but also writes every telemetry frame as a row in a CSV file (default `jetson_telemetry.csv`) to correlate specific events (ex. "I squeezed the brake at this timestamp").

### `collect [count]`

```bash
python3 OpenJetBolt.py collect       # 20 pairs (default)
python3 OpenJetBolt.py collect 50
```

Developer/Debugging command: queries the bike's authentication oracle with `count` random values and prints each `(input, output)` pair in a form you can paste into the script's `KNOWN_VECTORS` test data. Not needed for normal use.

### `selftest`

```bash
python3 OpenJetBolt.py selftest
```

Validates the authentication math against known test vectors (no bike or Bluetooth connection needed). Useful to confirm the
script itself is working correctly before troubleshooting connection issue.

## Status Bar while monitoring/logging

During `monitor` and `log`, a summary is at the bottom of the
terminal while the decoded telemetry stream keeps scrolling above it:

```
---------------------------------------------------------------------
| BATTERY: 98% | SPEED_RAW: 152 | TOP_SPEED: 210 | MAX: 33 km/h (20.5 mph) | BRAKE: - | LIGHT: - | CRUISE: 0 |
```

- **BATTERY** - battery percentage
- **SPEED_RAW** - live speed/RPM reading
- **TOP_SPEED** - highest `SPEED_RAW` value seen this session
- **MAX** - currently active max speed limit
- **BRAKE** / **LIGHT** - `YES` if on, `-` otherwise
- **CRUISE** - cruise control setting

## Example Usage

```bash
# One-time setup for a new bike
python3 OpenJetBolt.py pair
#   scanning for devices matching 'Bolt' (12s)...
#   candidates:
#     [0] 11223344-5566-7788-99AA-BBCCDDEEFF00  rssi= -52  Bolt
#   6-digit password [000000]:
#   verifying login against 11223344-5566-7788-99AA-BBCCDDEEFF00...
#   saved default bike to jetson_bolt_config.json

# Check what it's currently set to
python3 OpenJetBolt.py get
#   max speed = 25 km/h (15.5 mph)

# Raise the limit
python3 OpenJetBolt.py set 32
#   -> set max speed 32 km/h (19.9 mph):  aa 06 06 20 ... bb
#   confirmed: controller accepted 32 km/h

# Watch the live summary data for 60 seconds
python3 OpenJetBolt.py monitor 60
```

## Notes

- On macOS bleak/CoreBluetooth hides the bike's real MAC address and
  substitutes a system-assigned UUID. Expected and works the same as
  a real address everywhere in this script
- Use this at your own risk
