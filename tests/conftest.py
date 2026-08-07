# Shared pytest fixtures for the helpers/* unit test suite.
#
# The single most important fixture here is `isolated_config`: several
# helpers.config / helpers.cli functions read and write a real JSON file at
# the repo root (jetson_bolt_config.json) by default. Any test that exercises
# load_config/save_config/resolve_settings/cmd_config/cmd_pair MUST use this
# fixture (or otherwise redirect CONFIG_PATH) so the test suite never touches
# that real file.

import types
from unittest.mock import AsyncMock, MagicMock

import pytest


# isolated_config(monkeypatch, tmp_path)
#   Redirects CONFIG_PATH to a throwaway file under tmp_path so tests never
#   read/write/delete the real jetson_bolt_config.json at the repo root.
#   helpers/cli.py does `from .config import CONFIG_PATH, ...`, which binds
#   its own module-level name independently of helpers.config.CONFIG_PATH --
#   patching only one of them would leave the other pointing at the real
#   file, so both are patched here.
@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    from helpers import cli as cli_module
    from helpers import config as config_module

    fake_path = tmp_path / "jetson_bolt_config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", fake_path)
    monkeypatch.setattr(cli_module, "CONFIG_PATH", fake_path)
    return fake_path


# make_args()
#   Factory fixture for argparse.Namespace-like objects, for tests that call
#   resolve_settings(args) directly without going through real argument
#   parsing. Defaults every field to None so a test only needs to pass the
#   overrides it cares about.
@pytest.fixture
def make_args():
    def _make(**overrides):
        defaults = {"address": None, "password": None, "name_hint": None}
        defaults.update(overrides)
        return types.SimpleNamespace(**defaults)

    return _make


# make_mock_bolt()
#   Factory fixture returning (bolt_class_mock, bolt_instance_mock).
#   bolt_class_mock is a drop-in replacement for helpers.transport.Bolt (or
#   the name imported into helpers.cli): calling it with any args/kwargs
#   returns bolt_instance_mock, which supports `async with ... as bolt:` and
#   has AsyncMock stand-ins for every async method real code calls on a Bolt.
#   Used everywhere a test needs a fake bike without touching real BLE.
@pytest.fixture
def make_mock_bolt():
    def _make():
        bolt = MagicMock(name="BoltInstance")
        bolt.login = AsyncMock(return_value=True)
        bolt.handshake = AsyncMock(return_value=True)
        bolt.pair = AsyncMock(return_value=True)
        bolt.at = AsyncMock(return_value=None)
        bolt.oracle = AsyncMock(return_value=None)
        bolt.get_max_speed = AsyncMock(return_value=None)
        bolt.set_max_speed = AsyncMock(return_value=None)
        bolt.saw_telemetry = False
        bolt.csv_file = None
        bolt.status_bar = None
        # MagicMock specially supports configuring magic methods by direct
        # attribute assignment -- this is what makes `async with bolt:` work.
        bolt.__aenter__ = AsyncMock(return_value=bolt)
        bolt.__aexit__ = AsyncMock(return_value=False)

        bolt_class = MagicMock(name="BoltClass", return_value=bolt)
        return bolt_class, bolt

    return _make
