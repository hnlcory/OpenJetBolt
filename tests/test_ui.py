# Unit tests for helpers/ui.py -- the pinned bottom StatusBar.
#
# StatusBar reads sys.stdout.isatty() at construction time and, if enabled,
# writes raw ANSI/VT100 escape codes to sys.stdout. Under plain pytest,
# stdout isn't a real TTY, so the "disabled" path is exercised by default;
# the "enabled" path is exercised by monkeypatching sys.stdout to a fake
# object whose isatty() returns True.

import io
import os
import shutil
import sys

from helpers.ui import STATUS_BAR_FIELDS, StatusBar


# A StringIO that claims to be a real terminal, so StatusBar's
# `sys.stdout.isatty()` check can be forced True in tests.
class _FakeTTY(io.StringIO):
    def isatty(self):
        return True


# Under plain pytest, stdout is not a TTY, so StatusBar should be disabled
# by construction and __enter__/update/__exit__ must all be safe no-ops --
# this is the default, most common runtime scenario (piped/redirected output).
def test_status_bar_disabled_by_default_under_pytest():
    bar = StatusBar(["BATTERY"])
    assert bar.enabled is False
    with bar:
        bar.update({"BATTERY": "50%"})
    # No exception, and (since disabled) nothing was written to real stdout
    # beyond whatever pytest itself captures -- nothing to assert on stdout
    # content here, the important thing is __enter__/update/__exit__ are all
    # safe no-ops when not a TTY.


# Guard against the display-order constant drifting out of sync with the
# field names transport.Bolt._on_notify() actually passes to update() --
# a mismatch here would silently break the live summary bar.
def test_status_bar_field_order_constant_matches_expected_shape():
    assert STATUS_BAR_FIELDS == ["BATTERY", "SPEED_RAW", "TOP_SPEED", "MAX", "BRAKE", "LIGHT", "CRUISE"]


# With stdout forced to look like a real terminal, __enter__ should hide the
# cursor, set the scroll region (DECSTBM) to reserve the bottom rows, and do
# an initial redraw showing every field's placeholder ("--"). update()
# should then merge in new values and repaint, leaving untouched fields at
# their last-known value. __exit__ should restore the terminal afterwards.
def test_status_bar_enabled_writes_escapes_and_redraws(monkeypatch):
    fake_out = _FakeTTY()
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((80, 24)))

    bar = StatusBar(["BATTERY", "MAX"])
    assert bar.enabled is True

    with bar:
        assert bar.scroll_bottom == 24 - StatusBar.RESERVED_LINES
        enter_output = fake_out.getvalue()
        assert "\x1b[?25l" in enter_output                       # hide cursor
        assert f"\x1b[1;{bar.scroll_bottom}r" in enter_output    # DECSTBM scroll region
        assert "BATTERY: --" in enter_output                     # initial redraw, unset fields show "--"
        assert "MAX: --" in enter_output

        fake_out.truncate(0)
        fake_out.seek(0)
        bar.update({"BATTERY": "50%"})
        update_output = fake_out.getvalue()
        assert "BATTERY: 50%" in update_output
        assert "MAX: --" in update_output   # untouched field keeps its last value

    exit_output = fake_out.getvalue()
    assert "\x1b[r" in exit_output           # reset scroll region
    assert "\x1b[?25h" in exit_output        # show cursor


# _redraw() truncates the bar line to the terminal's column count so an
# over-wide bar never auto-wraps onto a third row -- force a narrow
# 20-column terminal with many fields and confirm the line is cut short.
def test_status_bar_truncates_line_to_terminal_width(monkeypatch):
    fake_out = _FakeTTY()
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((20, 24)))

    fields = ["BATTERY", "SPEED_RAW", "TOP_SPEED", "MAX", "BRAKE", "LIGHT", "CRUISE"]
    with StatusBar(fields):
        full_text = "| " + " | ".join(f"{name}: --" for name in fields) + " |"
        truncated_text = full_text[:20]
        output = fake_out.getvalue()
        assert truncated_text in output
        assert full_text not in output


# If the terminal is too short to safely reserve RESERVED_LINES rows,
# __enter__ must downgrade `enabled` back to False and write nothing at all
# -- falling back to plain scrolling rather than corrupting the display.
def test_status_bar_disables_itself_when_terminal_too_short(monkeypatch):
    fake_out = _FakeTTY()
    monkeypatch.setattr(sys, "stdout", fake_out)
    # 2 total rows - RESERVED_LINES(2) = 0 scroll_bottom -> too short to reserve space
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((80, 2)))

    bar = StatusBar(["BATTERY"])
    assert bar.enabled is True   # isatty() was True at construction time
    with bar:
        assert bar.enabled is False   # __enter__ downgrades it once it sees the terminal is too short
    assert fake_out.getvalue() == ""   # no escapes written at all
