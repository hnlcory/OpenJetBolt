# Unit tests for helpers/config.py -- per-bike saved defaults.
#
# Every test here uses the `isolated_config` fixture (see conftest.py) so
# CONFIG_PATH points at a throwaway file under tmp_path, never the real
# jetson_bolt_config.json at the repo root.

import json

from helpers.config import DEFAULT_NAME_HINT, DEFAULT_PASSWORD, load_config, resolve_settings, save_config


# A missing config file is a normal/expected state (first run, or after
# `config clear`) -- load_config() must return {} rather than raising.
def test_load_config_missing_file_returns_empty_dict(isolated_config):
    assert not isolated_config.exists()
    assert load_config() == {}


# A valid JSON file on disk should be parsed back into the equivalent dict.
def test_load_config_reads_valid_json(isolated_config):
    isolated_config.write_text(json.dumps({"address": "AA:BB:CC:DD:EE:FF", "password": "123456"}))
    assert load_config() == {"address": "AA:BB:CC:DD:EE:FF", "password": "123456"}


# A corrupted/non-JSON file must not crash unrelated commands -- load_config()
# is documented to treat a bad file the same as "no config yet" ({}).
def test_load_config_returns_empty_dict_on_corrupt_json(isolated_config):
    isolated_config.write_text("{not valid json")
    assert load_config() == {}


# save_config() should write pretty-printed JSON with a trailing newline
# (matches the documented `json.dumps(..., indent=2) + "\n"` behavior).
def test_save_config_writes_pretty_json_with_trailing_newline(isolated_config):
    save_config({"address": "AA:BB"})
    text = isolated_config.read_text()
    assert text.endswith("\n")
    assert json.loads(text) == {"address": "AA:BB"}


# Whatever save_config() writes, load_config() must be able to read back
# exactly -- the two functions need to agree on the file format.
def test_save_config_round_trips_with_load_config(isolated_config):
    config = {"address": "11:22:33:44:55:66", "password": "654321", "name_hint": "MyBolt"}
    save_config(config)
    assert load_config() == config


# ---------------------------------------------------------------------------
# resolve_settings: CLI arg > saved config > built-in default, per field.
# ---------------------------------------------------------------------------

# When both a CLI arg and a saved config value exist, the CLI arg (highest
# priority) must win for every field.
def test_resolve_settings_arg_overrides_saved_config(isolated_config, make_args):
    save_config({"address": "CONFIG:ADDR", "password": "111111", "name_hint": "ConfigHint"})
    args = make_args(address="ARG:ADDR", password="222222", name_hint="ArgHint")
    assert resolve_settings(args) == ("ARG:ADDR", "222222", "ArgHint")


# With no CLI overrides, the saved config values (second priority) should
# be used instead of falling straight through to the built-in defaults.
def test_resolve_settings_falls_back_to_saved_config(isolated_config, make_args):
    save_config({"address": "CONFIG:ADDR", "password": "111111", "name_hint": "ConfigHint"})
    args = make_args()   # no CLI overrides
    assert resolve_settings(args) == ("CONFIG:ADDR", "111111", "ConfigHint")


# With nothing saved and no CLI overrides, address should stay None (there
# is no built-in default address -- resolve() in transport.py scans
# instead), while password/name_hint fall back to their DEFAULT_* constants.
def test_resolve_settings_falls_back_to_defaults_when_nothing_saved(isolated_config, make_args):
    args = make_args()
    address, password, name_hint = resolve_settings(args)
    assert address is None
    assert password == DEFAULT_PASSWORD
    assert name_hint == DEFAULT_NAME_HINT


# The three-level priority is resolved independently per field, not
# all-or-nothing -- mix sources across the three fields in one call to
# confirm each field picks its own highest-priority source correctly.
def test_resolve_settings_mixes_sources_per_field(isolated_config, make_args):
    # address only in config, password only as CLI arg, name_hint falls back to default
    save_config({"address": "CONFIG:ADDR"})
    args = make_args(password="999999")
    assert resolve_settings(args) == ("CONFIG:ADDR", "999999", DEFAULT_NAME_HINT)
