"""
helpers.config -- per-bike settings: BLE address, password, and
(optionally) a name-hint used for scanning when no address is known yet.
Written by `pair`/`config set` (see cli.cmd_pair()/cli.cmd_config()), read
by resolve_settings() on every bike-facing command.
"""

import json
from pathlib import Path

# DEFAULT_NAME_HINT -- substring matched (case-insensitive) against a
#                      scanned device's advertised name when no address is
#                      known yet.
# DEFAULT_PASSWORD  -- fallback CODE= password if none is saved/passed.
DEFAULT_NAME_HINT = "Bolt"
DEFAULT_PASSWORD = "000000"

# CONFIG_PATH -- where per-bike defaults (address/password/name hint) are
#                persisted, as a JSON file next to OpenJetBolt.py (the
#                entry point) at the repo root -- not next to this module.
#                This file lives at helpers/config.py, so
#                `.parent.parent` walks back up out of the helpers/
#                package to the repo root.
#                Do NOT shorten this to `.parent` -- that would silently
#                move every user's saved config into helpers/ and
#                orphan the real jetson_bolt_config.json already checked
#                into the repo root.
CONFIG_PATH = Path(__file__).resolve().parent.parent / "jetson_bolt_config.json"


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
#                                  as produced by cli.build_parser()).
#   Returns:
#     tuple(address: str | None, password: str, name_hint: str)
#       address may still be None here if never set anywhere -- resolve()
#       (in transport.py) is what falls back to scanning in that case.
def resolve_settings(args):
    config = load_config()
    address = args.address or config.get("address")
    password = args.password or config.get("password") or DEFAULT_PASSWORD
    name_hint = args.name_hint or config.get("name_hint") or DEFAULT_NAME_HINT
    return address, password, name_hint
