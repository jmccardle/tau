#!/usr/bin/env python3
"""Report what your terminal sends for Enter, and what Textual calls it.

Reference: docs/ENTER-KEY.md §6. Answers the one question that document cannot
answer for you: whether YOUR terminal can tell an application that Shift was held
down when you pressed Enter.

Run it from a real terminal (not through a pipe):

    venv/bin/python scripts/keyprobe.py

Press Enter, Shift+Enter, Ctrl+Enter, Alt+Enter, Ctrl+J. Press Ctrl+C to stop.
The terminal is restored on the way out.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import tty

from textual import events
from textual._xterm_parser import XTermParser

KITTY_QUERY = "\x1b[?u"
# 1 | 8 | 16 — disambiguate, report-all-keys, report-associated-text. Exactly the
# flags textual/drivers/linux_driver.py writes on startup.
KITTY_ENABLE = "\x1b[>25u"
KITTY_DISABLE = "\x1b[<u"


def read_available(fd: int, timeout: float) -> str:
    """Read whatever is on the tty within timeout seconds. May be empty."""
    if not select.select([fd], [], [], timeout)[0]:
        return ""
    data = os.read(fd, 1024)
    while select.select([fd], [], [], 0.02)[0]:
        data += os.read(fd, 1024)
    return data.decode("utf-8", errors="replace")


def probe_kitty(fd: int) -> str:
    """Ask the terminal whether it speaks the kitty keyboard protocol."""
    os.write(fd, KITTY_QUERY.encode())
    reply = read_available(fd, 0.3)
    if not reply:
        return "NO REPLY -> this terminal does not speak the kitty protocol"
    return f"replied {reply!r} -> kitty protocol supported"


def textual_keys(parser: XTermParser, data: str) -> list[str]:
    """The key names Textual would produce for these bytes."""
    return [ev.key for ev in parser.feed(data) if isinstance(ev, events.Key)]


def flush_pending(parser: XTermParser) -> list[str]:
    """Resolve a sequence the parser is still holding, the way the driver does.

    A lone ESC and an ESC-prefixed sequence are ambiguous until either more bytes
    arrive or ``constants.ESCAPE_DELAY`` elapses, so the parser buffers and waits.
    Textual's driver settles that with ``Parser.tick()`` on a timer; without the
    same call here the probe reported an EMPTY key list for Alt+Enter, which reads
    as "nothing arrived" when what actually happens is that Textual resolves the
    sequence and then discards the modifier.
    """
    return [ev.key for ev in parser.tick() if isinstance(ev, events.Key)]


def main() -> int:
    if not sys.stdin.isatty():
        print("stdin is not a tty. Run this directly in your terminal.")
        return 1

    fd = sys.stdin.fileno()
    before = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        print(f"TERM={os.environ.get('TERM', '')!r}\r")
        print(f"kitty query: {probe_kitty(fd)}\r")
        print("\r")
        print("Enabling the kitty protocol, exactly as Textual 8.2.7 does.\r")
        os.write(fd, KITTY_ENABLE.encode())
        read_available(fd, 0.1)
        print("\r")
        print("Press keys. Ctrl+C stops.\r")
        print("\r")

        parser = XTermParser()
        pending = ""
        while True:
            # Shorter than ESCAPE_DELAY, so an ESC-prefixed sequence gets its
            # timeout driven on the next pass rather than waiting for a keypress.
            data = read_available(fd, 0.05)
            if not data:
                late = flush_pending(parser)
                if late:
                    print(f"bytes {pending!r:24} textual {late}\r")
                    pending = ""
                continue
            if data == "\x03":
                break
            keys = textual_keys(parser, data)
            if keys:
                print(f"bytes {data!r:24} textual {keys}\r")
                pending = ""
            else:
                # Held by the parser. ``flush_pending`` prints it a beat later.
                pending += data
    finally:
        os.write(fd, KITTY_DISABLE.encode())
        termios.tcsetattr(fd, termios.TCSADRAIN, before)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
