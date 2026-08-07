"""
helpers.ui -- terminal presentation helpers. Currently just the pinned
bottom StatusBar used by cli.cmd_monitor()/cli.cmd_log() to keep a live
telemetry summary readable while decoded output scrolls above it.
"""

import shutil
import sys

# STATUS_BAR_FIELDS -- display order of fields in the pinned bottom status
#                      bar used by cli.cmd_monitor()/cli.cmd_log() (see
#                      class StatusBar below). These names must match the
#                      keys transport.Bolt._on_notify() passes to
#                      StatusBar.update().
STATUS_BAR_FIELDS = ["BATTERY", "SPEED_RAW", "TOP_SPEED", "MAX", "BRAKE", "LIGHT", "CRUISE"]


# class StatusBar
#   A pinned 2-line summary -- one dashed separator plus one
#   "| LABEL: value | LABEL: value |" line -- fixed at the very bottom of
#   the terminal, while normal print() output keeps scrolling above it.
#   Used by cli.cmd_monitor()/cli.cmd_log() so live telemetry stays readable
#   at a glance instead of scrolling past.
#
#   How it works: ANSI/VT100 escape codes. `\x1b[<top>;<bottom>r` (DECSTBM,
#   "set scrolling region") confines normal terminal scrolling to
#   everything above the reserved lines; update() jumps into the reserved
#   region to repaint it, then moves the cursor back to the pinned
#   scrolling anchor (the bottom row of the scroll region, column 1) --
#   exactly where an ordinary print() already leaves it between lines --
#   so scrolling output elsewhere is unaffected. (Deliberately NOT done via
#   ANSI "save/restore cursor", `\x1b[s`/`\x1b[u` -- that pair is a single,
#   ambiguous save slot whose behavior around scroll regions isn't
#   consistent across terminals, and was observed to occasionally leave
#   the cursor parked in the reserved rows, so the next scrolling print --
#   often a status7/0xA7 line, just by bad luck of arrival timing -- got
#   drawn on top of the pinned bar instead of above it.) If stdout isn't a
#   real terminal (piped to a file, redirected, not a TTY), the bar
#   silently does nothing at all, so redirected output never gets raw
#   escape codes mixed into it.
#
#   Known limitation: the terminal size is captured once, on entry: if the
#   window is resized mid-session the reserved lines will be in the wrong
#   place until the next run. Not handled, since it's a minor cosmetic
#   issue for a nice-to-have feature.
#
#   Usage:
#     with StatusBar(["BATTERY", "MAX"]) as bar:
#         bolt.status_bar = bar
#         ...normal scrolling prints happen here, e.g. via Bolt.verbose...
#         bar.update({"BATTERY": "98%"})   # redraws the pinned line(s)
#
#   Construction args:
#     field_order (list[str]) -- display order of fields in the bar; any
#       field not yet passed to update() shows as "--" so the layout never
#       shifts as data trickles in.
class StatusBar:
    RESERVED_LINES = 2   # 1 separator line + 1 data line, pinned at the bottom

    def __init__(self, field_order):
        self.field_order = field_order
        self.values = {name: "--" for name in field_order}
        self.enabled = sys.stdout.isatty()   # never touch the terminal when output isn't an interactive TTY
        self.total_rows = 0
        self.columns = 80
        self.scroll_bottom = 0

    # __enter__: reserve the bottom RESERVED_LINES terminal rows for the bar
    # and confine normal scrolling to everything above them.
    # Returns: self, so `with StatusBar(...) as bar:` works.
    def __enter__(self):
        if self.enabled:
            size = shutil.get_terminal_size(fallback=(80, 24))
            self.total_rows, self.columns = size.lines, size.columns
            self.scroll_bottom = self.total_rows - self.RESERVED_LINES
            if self.scroll_bottom < 1:
                self.enabled = False   # terminal too short to reserve space safely -- fall back to plain scrolling
        if self.enabled:
            sys.stdout.write("\x1b[?25l")                        # hide cursor (avoids flicker during redraws)
            sys.stdout.write(f"\x1b[1;{self.scroll_bottom}r")     # DECSTBM: scrolling region = rows 1..scroll_bottom
            sys.stdout.write(f"\x1b[{self.scroll_bottom};1H")     # park cursor at the bottom of the scroll region
            sys.stdout.flush()
            self._redraw()
        return self

    # __exit__: always restore the terminal to normal full-screen scrolling,
    # even if the caller raised inside the `with` block -- otherwise the
    # user's shell is left with a broken scroll region afterwards.
    def __exit__(self, *a):
        if self.enabled:
            sys.stdout.write("\x1b[r")                            # reset scrolling region to the whole screen
            sys.stdout.write(f"\x1b[{self.total_rows};1H\n")       # move past the old bar so the shell prompt lands cleanly
            sys.stdout.write("\x1b[?25h")                          # show cursor again
            sys.stdout.flush()

    # update(new_values)
    #   Usage: bar.update({"BATTERY": "98%", "SPEED_RAW": "152"})
    #     Merges `new_values` into the bar's known field values (fields not
    #     included keep their previous/last-known value) and repaints.
    #   Args:
    #     new_values (dict[str, str]) -- subset of field_order -> display
    #       text; a single telemetry frame usually only updates 1-2 fields.
    #   Returns: None. No-op if stdout isn't a terminal.
    def update(self, new_values):
        self.values.update(new_values)
        if self.enabled:
            self._redraw()

    # _redraw(): repaint the two reserved lines in place without disturbing
    # whatever the caller is scrolling above them.
    def _redraw(self):
        bar_text = "| " + " | ".join(f"{name}: {self.values[name]}" for name in self.field_order) + " |"
        bar_text = bar_text[:self.columns]      # never let an over-wide bar line auto-wrap onto a 3rd row
        separator = "-" * self.columns
        sys.stdout.write(f"\x1b[{self.scroll_bottom + 1};1H\x1b[2K{separator}")  # repaint separator line
        sys.stdout.write(f"\x1b[{self.scroll_bottom + 2};1H\x1b[2K{bar_text}")   # repaint data line
        # Return to the pinned scrolling anchor deterministically (see the
        # class docstring for why this replaced save/restore cursor).
        sys.stdout.write(f"\x1b[{self.scroll_bottom};1H")
        sys.stdout.flush()
