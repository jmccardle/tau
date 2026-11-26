"""The signals that end τ as leaving does: SIGTERM (`kill`, a service manager) and SIGHUP (the
terminal closed). Each program that holds a session (`tau -p`, the terminal UI, `tau serve`) turns
them into cancellation, so the same shutdown runs as on a normal exit: the driver stops, and with
it every extension's `stop`, which ends the processes it started (hook commands, MCP servers).
A process that died of either at once would leave those running, as `tau._base_tools` says of a
`bash` command.

Windows has no SIGHUP and its event loop has no `add_signal_handler`; a process ended there with
`TerminateProcess` cannot run anything, so `ENDING` is empty on Windows and these do nothing.
"""

import asyncio
import signal
import sys
from collections.abc import Callable
from typing import Final

ENDING: Final[tuple[signal.Signals, ...]] = (
    () if sys.platform == "win32" else (signal.SIGTERM, signal.SIGHUP)
)
"""The signals that end a program cleanly, beyond SIGINT: none on Windows."""
HANGUP: Final[int | None] = None if sys.platform == "win32" else signal.SIGHUP
"""SIGHUP, after which the terminal may be gone; `None` on Windows, which has none."""


def status(number: int) -> int:
    """The exit status of a program a signal ended: 128 + its number, as a shell reports it."""
    return 128 + number


def name(number: int) -> str:
    """`SIGTERM` for 15."""
    return signal.Signals(number).name


def on_ending(loop: asyncio.AbstractEventLoop, then: Callable[[int], None]) -> None:
    """Run `then(number)` in `loop` on each of `ENDING`, in place of any handler before it."""
    for number in ENDING:
        loop.add_signal_handler(number, then, number)


def off_ending(loop: asyncio.AbstractEventLoop) -> None:
    """`ENDING` back to the default (the process dies of it)."""
    for number in ENDING:
        loop.remove_signal_handler(number)
