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

import pyte
from world import World

ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


_TAKE_THE_TERMINAL = (
    "import fcntl, os, sys, termios\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "os.execv(sys.argv[1], sys.argv[1:])\n"
)


class Screen:
    """A program on a pseudo-terminal, and what it has drawn: `tau` with `argv`, or, given
    `program`, that program (and `quit_key` is what leaves it)."""

    def __init__(
        self,
        world: World,
        *argv: str,
        cwd: Path,
        program: Path | None = None,
        quit_key: str = "\x11",
        env: dict[str, str] | None = None,
    ) -> None:
        self.quit_key = quit_key
        command = [sys.executable, "-m", "tau"] if program is None else [str(program)]
        environ = {
            **world.environ,
            "TAU_HOME": str(world.tau),
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            **(env or {}),
        }
        self.fd, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        # No fork of this (threaded) process: a new session whose first act is to take the
        # terminal as its controlling one, then become `tau`.
        self.process = subprocess.Popen(
            [sys.executable, "-c", _TAKE_THE_TERMINAL, *command, *argv],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=environ,
            start_new_session=True,
        )
        os.close(slave)
        self.pid = self.process.pid
        self.seen = b""
        self._terminal = pyte.Screen(120, 40)
        self._stream = pyte.ByteStream(self._terminal)
        self._fed = 0

    def text(self) -> str:
        return ANSI.sub(b"", self.seen).decode("utf-8", "replace")

    def display(self) -> str:
        """The screen as it looks now, one line per row: for a program that draws by moving the
        cursor and rewriting only what changed, whose output is no text (`text` is for `tau`)."""
        self._stream.feed(self.seen[self._fed :])
        self._fed = len(self.seen)
        return "\n".join(line.rstrip() for line in self._terminal.display)

    def wait_for(self, text: str, seconds: float = 20, *, on_screen: bool = False) -> None:
        """Wait until `text` has been drawn; with `on_screen`, until it is on the screen now."""
        deadline = time.monotonic() + seconds
        while text not in (self.display() if on_screen else self.text()):
            if time.monotonic() >= deadline:
                shown = (self.display() if on_screen else self.text())[-600:]
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

    def pump(self, seconds: float = 0.1) -> None:
        """Read what the program draws for up to `seconds`."""
        self._drain(seconds)

    def close(self) -> None:
        """End the process if it is still running, close the terminal, and reap it. The terminal
        is closed first: on macOS a process exits only once its terminal's unread output has
        drained, so with output left and nobody reading, a killed process would never be reaped.
        Reaping has a deadline, so a process that still will not go fails the test, not hangs it."""
        if self.process.poll() is None:
            self.process.kill()
        with contextlib.suppress(OSError):
            os.close(self.fd)  # closed already, perhaps
        self.process.wait(timeout=30)

    def type(self, keys: str) -> None:
        os.write(self.fd, keys.encode())

    def quit(self) -> int:
        self.type(self.quit_key)
        try:
            return self.exit_code()
        except AssertionError:
            raise AssertionError(f"the program did not leave on {self.quit_key!r}") from None

    def exit_code(self, seconds: float = 20) -> int:
        """Wait for the program to exit, reading what it draws meanwhile; its exit code."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            code = self.process.poll()
            if code is not None:
                self._drain(0.05)  # what it wrote last
                self.close()
                return code
            self._drain(0.05)  # a terminal nobody reads fills up, and the program blocks writing
        self.close()
        raise AssertionError("the program did not exit")


def lock_holder(log: Path) -> int | None:
    """The process holding the lease (an exclusive `flock`) on the session log `log`. Linux lists
    the locks in `/proc/locks`. macOS has no such list: a shared `flock` that is refused says
    some process holds it, and `lsof` names the one that has the file open (the lease holder is
    the only process with a session log open, so exactly one is expected)."""
    if sys.platform != "darwin":
        inode = log.stat().st_ino
        for line in Path("/proc/locks").read_text().splitlines():
            fields = line.split()
            if "FLOCK" in fields and fields[5].endswith(f":{inode}"):
                return int(fields[4])
        return None
    with log.open("rb") as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            return None  # nobody holds it exclusively; our probe lock goes with `probe`
    done = subprocess.run(
        ["lsof", "-t", "--", str(log)], capture_output=True, text=True, check=False
    )
    holders = [int(p) for p in done.stdout.split() if int(p) != os.getpid()]
    assert len(holders) == 1, f"{log} is locked; the processes with it open are {holders}"
    return holders[0]
