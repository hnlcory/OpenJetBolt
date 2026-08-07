"""
Module map:
    auth.py       -- the +PM authentication transform (pm_token) + its
                      offline test vectors
    protocol.py   -- GATT UUIDs, the binary AA..BB frame format, and
                      parse/format helpers
    transport.py  -- class Bolt, the async BLE session, and resolve()
    ui.py         -- class StatusBar, the pinned live telemetry summary
    config.py     -- saved per-bike defaults (address/password/name hint)
    cli.py        -- argument parsing, the cmd_* command implementations,
                      and main() (actual entry point OpenJetBolt.py calls)
"""
