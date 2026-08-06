#!/usr/bin/env python3
"""
Entry Point file. import and call main(). all argument parsing and command
implementations in helpers/cli.py.

    python3 OpenJetBolt.py <command>

Requires:  pip install bleak
"""

from helpers.cli import main

if __name__ == "__main__":
    main()
