# Unit tests for helpers/cli.py -- argparse wiring plus every cmd_* function.
#
# Full mock coverage: bleak (BleakScanner), Bolt, builtins.input, and
# asyncio.sleep are all mocked so nothing here touches real BLE hardware,
# real stdin, or the real clock/filesystem outside of tmp_path/isolated_config.

import sys
import types

import pytest
from unittest.mock import AsyncMock, MagicMock

from helpers import cli


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------

# Bare `selftest` should parse with no extra arguments needed.
def test_parser_selftest():
    args = cli.build_parser().parse_args(["selftest"])
    assert args.command == "selftest"


# `collect` takes an optional positional count, defaulting to 20 when
# omitted and using whatever's given otherwise.
def test_parser_collect_default_and_override_count():
    parser = cli.build_parser()
    assert parser.parse_args(["collect"]).count == 20
    assert parser.parse_args(["collect", "5"]).count == 5


# `scan` and `pair` are argument-free subcommands -- just confirm they
# parse and set args.command correctly.
def test_parser_scan_and_pair():
    parser = cli.build_parser()
    assert parser.parse_args(["scan"]).command == "scan"
    assert parser.parse_args(["pair"]).command == "pair"


# `config` has its own nested sub-subparsers (show/set/clear); `config set`
# also has its own -a/-p/-n flags separate from the top-level ones.
def test_parser_config_show_set_clear():
    parser = cli.build_parser()
    assert parser.parse_args(["config", "show"]).config_action == "show"
    assert parser.parse_args(["config", "clear"]).config_action == "clear"
    set_args = parser.parse_args(["config", "set", "-a", "AA:BB", "-p", "123456", "-n", "MyBolt"])
    assert set_args.config_action == "set"
    assert set_args.address == "AA:BB"
    assert set_args.password == "123456"
    assert set_args.name_hint == "MyBolt"


# `monitor` takes an optional positional seconds, defaulting to 30.
def test_parser_monitor_default_and_override_seconds():
    parser = cli.build_parser()
    assert parser.parse_args(["monitor"]).seconds == 30
    assert parser.parse_args(["monitor", "10"]).seconds == 10


# `log` takes two optional positionals (seconds, path) with documented
# defaults (30s, jetson_telemetry.csv).
def test_parser_log_defaults_and_overrides():
    parser = cli.build_parser()
    args = parser.parse_args(["log"])
    assert args.seconds == 30
    assert args.path == "jetson_telemetry.csv"
    args = parser.parse_args(["log", "5", "out.csv"])
    assert args.seconds == 5
    assert args.path == "out.csv"


# `info` and `get` are argument-free subcommands.
def test_parser_info_and_get():
    parser = cli.build_parser()
    assert parser.parse_args(["info"]).command == "info"
    assert parser.parse_args(["get"]).command == "get"


# `set` requires a kmh positional -- there's no sensible default target
# speed, so omitting it must be a parse error (argparse raises SystemExit).
def test_parser_set_requires_kmh():
    parser = cli.build_parser()
    assert parser.parse_args(["set", "25"]).kmh == 25
    with pytest.raises(SystemExit):
        parser.parse_args(["set"])   # kmh is a required positional


# The global --address/--password/--name-hint flags (before the subcommand
# name) should populate the shared namespace regardless of which subcommand
# follows.
def test_parser_global_overrides_before_subcommand():
    parser = cli.build_parser()
    args = parser.parse_args(["--address", "AA:BB", "--password", "123456", "--name-hint", "MyBolt", "get"])
    assert args.address == "AA:BB"
    assert args.password == "123456"
    assert args.name_hint == "MyBolt"


# ---------------------------------------------------------------------------
# cmd_selftest
# ---------------------------------------------------------------------------

# cmd_selftest() runs entirely offline against KNOWN_VECTORS -- since
# pm_token() is correct (see test_auth.py), it must report every vector as
# OK and print the "ALL VECTORS PASS" summary line, with no FAIL anywhere.
def test_cmd_selftest_reports_all_vectors_pass(capsys):
    cli.cmd_selftest()
    out = capsys.readouterr().out
    assert "ALL VECTORS PASS" in out
    assert "FAIL" not in out


# ---------------------------------------------------------------------------
# cmd_collect
# ---------------------------------------------------------------------------

# cmd_collect() should query the oracle exactly `count` times after a
# successful login -- one oracle() call per requested random sample.
async def test_cmd_collect_queries_oracle_count_times(mocker, make_mock_bolt):
    bolt_class, bolt = make_mock_bolt()
    bolt.oracle = AsyncMock(return_value=0x1234)
    mocker.patch.object(cli, "Bolt", bolt_class)
    mocker.patch.object(cli, "resolve", AsyncMock(return_value="AA:BB"))
    mocker.patch.object(cli.random, "randrange", return_value=0)

    await cli.cmd_collect(None, "000000", "Bolt", 3)

    assert bolt.oracle.await_count == 3


# If login() fails, cmd_collect() must return immediately -- querying the
# oracle without a successful login makes no sense and shouldn't happen.
async def test_cmd_collect_returns_early_when_login_fails(mocker, make_mock_bolt):
    bolt_class, bolt = make_mock_bolt()
    bolt.login = AsyncMock(return_value=False)
    bolt.oracle = AsyncMock()
    mocker.patch.object(cli, "Bolt", bolt_class)
    mocker.patch.object(cli, "resolve", AsyncMock(return_value="AA:BB"))

    await cli.cmd_collect(None, "000000", "Bolt", 5)

    bolt.oracle.assert_not_awaited()


# ---------------------------------------------------------------------------
# cmd_scan
# ---------------------------------------------------------------------------

# cmd_scan() should print one line per discovered device, including its
# address, RSSI, and advertised name.
async def test_cmd_scan_prints_discovered_devices(mocker, capsys):
    device = MagicMock()
    device.name = "MyBolt"
    advertisement = MagicMock()
    advertisement.rssi = -55
    advertisement.local_name = "MyBolt"
    mocker.patch.object(
        cli.BleakScanner, "discover", AsyncMock(return_value={"AA:BB": (device, advertisement)})
    )

    await cli.cmd_scan()

    out = capsys.readouterr().out
    assert "AA:BB" in out
    assert "-55" in out
    assert "MyBolt" in out


# ---------------------------------------------------------------------------
# cmd_pair
# ---------------------------------------------------------------------------

# Helper (not a test itself): builds a (device, advertisement) mock pair
# for a scan result, with the given name and RSSI.
def _make_candidate(name, rssi):
    device = MagicMock()
    device.name = name
    advertisement = MagicMock()
    advertisement.rssi = rssi
    advertisement.local_name = name
    return device, advertisement


# If the scan finds no device matching the name hint, cmd_pair() must raise
# SystemExit rather than trying to proceed with nothing to pair.
async def test_cmd_pair_raises_when_no_candidates_match(mocker):
    mocker.patch.object(cli.BleakScanner, "discover", AsyncMock(return_value={}))
    with pytest.raises(SystemExit):
        await cli.cmd_pair("Bolt")


# With exactly one matching candidate, cmd_pair() should skip the
# device-selection prompt (only prompting for the password), verify the
# password against the bike, and save address/password/name_hint on success.
async def test_cmd_pair_single_candidate_skips_selection_prompt_and_saves(mocker, isolated_config, make_mock_bolt):
    discovered = {"AA:BB": _make_candidate("MyBolt", -40)}
    mocker.patch.object(cli.BleakScanner, "discover", AsyncMock(return_value=discovered))
    bolt_class, bolt = make_mock_bolt()
    mocker.patch.object(cli, "Bolt", bolt_class)
    input_mock = mocker.patch("builtins.input", return_value="")   # accept default password

    await cli.cmd_pair("Bolt")

    input_mock.assert_called_once()   # only the password prompt, no device-selection prompt
    saved = cli.load_config()
    assert saved["address"] == "AA:BB"
    assert saved["password"] == cli.DEFAULT_PASSWORD
    assert saved["name_hint"] == "Bolt"


# With multiple matching candidates, cmd_pair() must prompt for a selection
# index and use whichever one the user picked (here index 1, "CC:DD").
async def test_cmd_pair_multiple_candidates_prompts_for_selection(mocker, isolated_config, make_mock_bolt):
    discovered = {
        "AA:BB": _make_candidate("Bolt-1", -40),
        "CC:DD": _make_candidate("Bolt-2", -45),
    }
    mocker.patch.object(cli.BleakScanner, "discover", AsyncMock(return_value=discovered))
    bolt_class, bolt = make_mock_bolt()
    mocker.patch.object(cli, "Bolt", bolt_class)
    input_mock = mocker.patch("builtins.input", side_effect=["1", "123456"])

    await cli.cmd_pair("Bolt")

    assert input_mock.call_count == 2
    saved = cli.load_config()
    assert saved["address"] == "CC:DD"
    assert saved["password"] == "123456"


# If the entered password is rejected by the bike, cmd_pair() must raise
# SystemExit and must NOT save anything -- a rejected password should never
# become the new saved default.
async def test_cmd_pair_rejected_password_raises_and_does_not_save(mocker, isolated_config, make_mock_bolt):
    discovered = {"AA:BB": _make_candidate("MyBolt", -40)}
    mocker.patch.object(cli.BleakScanner, "discover", AsyncMock(return_value=discovered))
    bolt_class, bolt = make_mock_bolt()
    bolt.login = AsyncMock(return_value=False)
    mocker.patch.object(cli, "Bolt", bolt_class)
    mocker.patch("builtins.input", return_value="000000")

    with pytest.raises(SystemExit):
        await cli.cmd_pair("Bolt")

    assert cli.load_config() == {}


# ---------------------------------------------------------------------------
# cmd_config
# ---------------------------------------------------------------------------

# With no saved config, `config show` should say so rather than printing
# nothing or raising.
def test_cmd_config_show_with_no_saved_config(isolated_config, capsys):
    cli.cmd_config(types.SimpleNamespace(config_action="show"))
    assert "no saved config" in capsys.readouterr().out


# `config show` with an existing config should print every saved key/value.
def test_cmd_config_show_prints_each_saved_key(isolated_config, capsys):
    cli.save_config({"address": "AA:BB", "password": "123456"})
    cli.cmd_config(types.SimpleNamespace(config_action="show"))
    out = capsys.readouterr().out
    assert "address = AA:BB" in out
    assert "password = 123456" in out


# `config set` should only touch the fields explicitly passed, leaving any
# other previously-saved keys (like an untouched name_hint) intact.
def test_cmd_config_set_merges_without_clobbering_unrelated_keys(isolated_config):
    cli.save_config({"address": "AA:BB", "name_hint": "Bolt"})
    cli.cmd_config(types.SimpleNamespace(config_action="set", address=None, password="999999", name_hint=None))
    saved = cli.load_config()
    assert saved == {"address": "AA:BB", "name_hint": "Bolt", "password": "999999"}


# `config clear` should delete an existing config file and report that it
# did so.
def test_cmd_config_clear_removes_existing_file(isolated_config, capsys):
    cli.save_config({"address": "AA:BB"})
    cli.cmd_config(types.SimpleNamespace(config_action="clear"))
    assert not isolated_config.exists()
    assert "removed" in capsys.readouterr().out


# `config clear` with no file present must not raise (nothing to delete),
# and should say so instead.
def test_cmd_config_clear_when_no_file_present(isolated_config, capsys):
    cli.cmd_config(types.SimpleNamespace(config_action="clear"))
    assert "no config file to remove" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_monitor
# ---------------------------------------------------------------------------

# Helper (not a test itself): a StatusBar replacement usable as
# `with StatusBar(...) as bar:` without touching a real terminal.
def _mock_status_bar(mocker):
    instance = MagicMock()
    instance.__enter__ = MagicMock(return_value=instance)
    instance.__exit__ = MagicMock(return_value=False)
    mocker.patch.object(cli, "StatusBar", return_value=instance)
    return instance


# cmd_monitor() should complete the handshake, wire the StatusBar instance
# onto the Bolt (so telemetry updates reach it), and print a note when no
# binary telemetry was ever seen during the session.
async def test_cmd_monitor_handshakes_and_reports_no_telemetry(mocker, make_mock_bolt, capsys):
    bolt_class, bolt = make_mock_bolt()
    bolt.saw_telemetry = False
    mocker.patch.object(cli, "Bolt", bolt_class)
    mocker.patch.object(cli, "resolve", AsyncMock(return_value="AA:BB"))
    status_bar = _mock_status_bar(mocker)
    mocker.patch("asyncio.sleep", AsyncMock())

    await cli.cmd_monitor(None, "000000", "Bolt", 5)

    bolt.handshake.assert_awaited_once()
    assert bolt.status_bar is status_bar
    assert "no binary telemetry" in capsys.readouterr().out


# When telemetry WAS seen, the "no binary telemetry" note must not appear
# -- it should only fire in the no-data case above.
async def test_cmd_monitor_says_nothing_extra_when_telemetry_seen(mocker, make_mock_bolt, capsys):
    bolt_class, bolt = make_mock_bolt()
    bolt.saw_telemetry = True
    mocker.patch.object(cli, "Bolt", bolt_class)
    mocker.patch.object(cli, "resolve", AsyncMock(return_value="AA:BB"))
    _mock_status_bar(mocker)
    mocker.patch("asyncio.sleep", AsyncMock())

    await cli.cmd_monitor(None, "000000", "Bolt", 5)

    assert "no binary telemetry" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_log
# ---------------------------------------------------------------------------

# On a successful handshake, cmd_log() should write the CSV header, assign
# the open file handle onto bolt.csv_file (so _on_notify starts logging
# rows), and print the final "wrote <path>" confirmation.
async def test_cmd_log_writes_csv_header_and_confirms_on_success(mocker, tmp_path, make_mock_bolt, capsys):
    bolt_class, bolt = make_mock_bolt()
    mocker.patch.object(cli, "Bolt", bolt_class)
    mocker.patch.object(cli, "resolve", AsyncMock(return_value="AA:BB"))
    _mock_status_bar(mocker)
    mocker.patch("asyncio.sleep", AsyncMock())
    path = tmp_path / "out.csv"

    await cli.cmd_log(None, "000000", "Bolt", 5, str(path))

    content = path.read_text()
    assert content.startswith("epoch,type,battery,speed_raw,cap,brake,light,cruise,raw_hex\n")
    assert bolt.csv_file is not None
    assert f"wrote {path}" in capsys.readouterr().out


# If the handshake fails, cmd_log() returns before the final print -- the
# CSV file still gets created (the header is written before the handshake
# is attempted) but the "wrote ..." confirmation must NOT appear.
async def test_cmd_log_returns_early_when_handshake_fails(mocker, tmp_path, make_mock_bolt, capsys):
    bolt_class, bolt = make_mock_bolt()
    bolt.handshake = AsyncMock(return_value=False)
    mocker.patch.object(cli, "Bolt", bolt_class)
    mocker.patch.object(cli, "resolve", AsyncMock(return_value="AA:BB"))
    _mock_status_bar(mocker)
    mocker.patch("asyncio.sleep", AsyncMock())
    path = tmp_path / "out.csv"

    await cli.cmd_log(None, "000000", "Bolt", 5, str(path))

    assert path.exists()   # header was written before the handshake attempt
    assert "wrote" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_info / cmd_get
# ---------------------------------------------------------------------------

# cmd_info() should fire exactly the documented batch of read-only AT
# queries, in order, after a successful handshake.
async def test_cmd_info_queries_expected_at_commands(mocker, make_mock_bolt):
    bolt_class, bolt = make_mock_bolt()
    bolt.get_max_speed = AsyncMock(return_value=25)
    mocker.patch.object(cli, "Bolt", bolt_class)
    mocker.patch.object(cli, "resolve", AsyncMock(return_value="AA:BB"))

    await cli.cmd_info(None, "000000", "Bolt")

    called_cmds = [call.args[0] for call in bolt.at.await_args_list]
    assert called_cmds == ["+VER?", "+MODE=?", "+UNIT=?", "+LOCK=?", "+CRZE=?", "HLGT=?"]


# cmd_get() should print the current max speed (in km/h and converted mph)
# when a value is available.
async def test_cmd_get_prints_max_speed(mocker, capsys, make_mock_bolt):
    bolt_class, bolt = make_mock_bolt()
    bolt.get_max_speed = AsyncMock(return_value=25)
    mocker.patch.object(cli, "Bolt", bolt_class)
    mocker.patch.object(cli, "resolve", AsyncMock(return_value="AA:BB"))

    await cli.cmd_get(None, "000000", "Bolt")

    assert "max speed = 25 km/h" in capsys.readouterr().out


# cmd_get() should print a "no read-back" message instead of crashing on
# `None * 0.6214` when no 0xA3 frame arrived at all.
async def test_cmd_get_prints_no_readback_when_none(mocker, capsys, make_mock_bolt):
    bolt_class, bolt = make_mock_bolt()
    bolt.get_max_speed = AsyncMock(return_value=None)
    mocker.patch.object(cli, "Bolt", bolt_class)
    mocker.patch.object(cli, "resolve", AsyncMock(return_value="AA:BB"))

    await cli.cmd_get(None, "000000", "Bolt")

    assert "no read-back" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_set
# ---------------------------------------------------------------------------

# When the bike confirms exactly the requested speed, cmd_set() should
# print the "confirmed" outcome.
async def test_cmd_set_prints_confirmed_when_accepted_as_requested(mocker, capsys, make_mock_bolt):
    bolt_class, bolt = make_mock_bolt()
    bolt.set_max_speed = AsyncMock(return_value=25)
    mocker.patch.object(cli, "Bolt", bolt_class)
    mocker.patch.object(cli, "resolve", AsyncMock(return_value="AA:BB"))

    await cli.cmd_set(None, "000000", "Bolt", 25)

    assert "confirmed" in capsys.readouterr().out


# When the bike's firmware caps the value lower than requested, cmd_set()
# should print the "CLAMPED" outcome, not "confirmed".
async def test_cmd_set_prints_clamped_when_value_differs(mocker, capsys, make_mock_bolt):
    bolt_class, bolt = make_mock_bolt()
    bolt.set_max_speed = AsyncMock(return_value=20)
    mocker.patch.object(cli, "Bolt", bolt_class)
    mocker.patch.object(cli, "resolve", AsyncMock(return_value="AA:BB"))

    await cli.cmd_set(None, "000000", "Bolt", 25)

    assert "CLAMPED" in capsys.readouterr().out


# When no 0xA3 confirmation ever arrives, cmd_set() should say so rather
# than reporting a false confirmation or clamp.
async def test_cmd_set_prints_no_confirmation_when_none(mocker, capsys, make_mock_bolt):
    bolt_class, bolt = make_mock_bolt()
    bolt.set_max_speed = AsyncMock(return_value=None)
    mocker.patch.object(cli, "Bolt", bolt_class)
    mocker.patch.object(cli, "resolve", AsyncMock(return_value="AA:BB"))

    await cli.cmd_set(None, "000000", "Bolt", 25)

    assert "no 0xA3 confirmation" in capsys.readouterr().out


# If the handshake fails, cmd_set() must not attempt to send the
# set-max-speed frame at all.
async def test_cmd_set_skips_when_handshake_fails(mocker, make_mock_bolt):
    bolt_class, bolt = make_mock_bolt()
    bolt.handshake = AsyncMock(return_value=False)
    mocker.patch.object(cli, "Bolt", bolt_class)
    mocker.patch.object(cli, "resolve", AsyncMock(return_value="AA:BB"))

    await cli.cmd_set(None, "000000", "Bolt", 25)

    bolt.set_max_speed.assert_not_awaited()


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------

# Running with no arguments at all should default to `selftest` (the
# documented bare-invocation behavior), not raise an argparse error.
def test_main_defaults_to_selftest_with_no_args(mocker, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["OpenJetBolt.py"])
    selftest_mock = mocker.patch.object(cli, "cmd_selftest")
    cli.main()
    selftest_mock.assert_called_once()


# `config` is dispatched synchronously (cmd_config(args) directly), unlike
# every other subcommand which goes through asyncio.run() -- confirm
# asyncio.run is never invoked for it.
def test_main_dispatches_config_synchronously(mocker, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["OpenJetBolt.py", "config", "show"])
    config_mock = mocker.patch.object(cli, "cmd_config")
    run_mock = mocker.patch("asyncio.run")
    cli.main()
    config_mock.assert_called_once()
    run_mock.assert_not_called()


# Every other subcommand should call its matching cmd_*() coroutine with
# exactly the arguments main() is documented to pass, and hand the
# resulting coroutine to asyncio.run() to actually execute it.
@pytest.mark.parametrize(
    "argv, cmd_name, expected_call",
    [
        (["scan"], "cmd_scan", ()),
        (["pair"], "cmd_pair", ("Bolt",)),
        (["collect", "5"], "cmd_collect", (None, "000000", "Bolt", 5)),
        (["monitor", "10"], "cmd_monitor", (None, "000000", "Bolt", 10)),
        (["log", "10", "out.csv"], "cmd_log", (None, "000000", "Bolt", 10, "out.csv")),
        (["info"], "cmd_info", (None, "000000", "Bolt")),
        (["get"], "cmd_get", (None, "000000", "Bolt")),
        (["set", "25"], "cmd_set", (None, "000000", "Bolt", 25)),
    ],
)
def test_main_dispatches_async_commands_via_asyncio_run(mocker, monkeypatch, argv, cmd_name, expected_call):
    monkeypatch.setattr(sys, "argv", ["OpenJetBolt.py"] + argv)
    mocker.patch.object(cli, "resolve_settings", return_value=(None, "000000", "Bolt"))
    run_mock = mocker.patch("asyncio.run")
    # cmd_name is an `async def` in cli.py, so mocker.patch.object auto-detects
    # that and swaps in an AsyncMock -- calling it returns a coroutine object
    # (not `return_value` directly), which is what gets handed to asyncio.run().
    cmd_mock = mocker.patch.object(cli, cmd_name)

    cli.main()

    cmd_mock.assert_called_once_with(*expected_call)
    run_mock.assert_called_once()
    run_mock.call_args.args[0].close()   # avoid a "coroutine was never awaited" warning
