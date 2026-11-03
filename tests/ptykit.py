"""`tau` on a pseudo-terminal: a real process, its screen read back as text."""

import contextlib
import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

from world import World

ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


_TAKE_THE_TERMINAL = (
    "import fcntl, os, sys, termios\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "os.execv(sys.argv[1], sys.argv[1:])\n"
)


class Screen:
    """A `tau` process on a pseudo-terminal, and what it has drawn."""

    def __init__(self, world: World, *argv: str, cwd: Path) -> None:
        env = {
            **world.environ,
            "TAU_HOME": str(world.tau),
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
        }
        self.fd, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        # No fork of this (threaded) process: a new session whose first act is to take the
        # terminal as its controlling one, then become `tau`.
        self.process = subprocess.Popen(
            [sys.executable, "-c", _TAKE_THE_TERMINAL, sys.executable, "-m", "tau", *argv],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        os.close(slave)
        self.pid = self.process.pid
        self.seen = b""

    def text(self) -> str:
        return ANSI.sub(b"", self.seen).decode("utf-8", "replace")

    def wait_for(self, text: str, seconds: float = 20) -> None:
        deadline = time.monotonic() + seconds
        while text not in self.text():
            if time.monotonic() >= deadline:
                shown = self.text()[-600:]
                self.close()
                raise AssertionError(f"{text!r} never drawn:\n{shown}")
            if not self._drain():
                break

    def _drain(self, seconds: float = 0.1) -> bool:
        """Take what the terminal has to say, waiting up to `seconds`; False once it is closed."""
        if select.select([self.fd], [], [], seconds)[0]:
            try:
                self.seen += os.read(self.fd, 65536)
            except OSError:
                return False
        return True

    def close(self) -> None:
        """End the process if it is still running, reap it, and close the terminal."""
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        with contextlib.suppress(OSError):
            os.close(self.fd)  # closed already, perhaps

    def type(self, keys: str) -> None:
        os.write(self.fd, keys.encode())

    def quit(self) -> int:
        self.type("\x11")  # Ctrl+Q
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            code = self.process.poll()
            if code is not None:
                self.close()
                return code
            self._drain(0.05)  # a terminal nobody reads fills up, and `tau` blocks writing to it
        self.close()
        raise AssertionError("tau did not leave on Ctrl+Q")


def lock_holder(log: Path) -> int | None:
    """The process holding the lease (an exclusive `flock`) on the session log `log`, from
    `/proc/locks`."""
    inode = log.stat().st_ino
    for line in Path("/proc/locks").read_text().splitlines():
        fields = line.split()
        if "FLOCK" in fields and fields[5].endswith(f":{inode}"):
            return int(fields[4])
    return None
