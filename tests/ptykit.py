"""`tau` on a pseudo-terminal: a real process, its screen read back as text."""

import fcntl
import os
import pty
import re
import select
import struct
import sys
import termios
import time
from pathlib import Path

from world import World

ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


class Screen:
    """A `tau` process on a pseudo-terminal, and what it has drawn."""

    def __init__(self, world: World, *argv: str, cwd: Path) -> None:
        env = {**world.environ, "TAU_HOME": str(world.tau), "TERM": "xterm-256color"}
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(cwd)
            os.execve(sys.executable, [sys.executable, "-m", "tau", *argv], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        self.seen = b""

    def text(self) -> str:
        return ANSI.sub(b"", self.seen).decode("utf-8", "replace")

    def wait_for(self, text: str, seconds: float = 20) -> None:
        deadline = time.monotonic() + seconds
        while text not in self.text():
            assert time.monotonic() < deadline, f"{text!r} never drawn:\n{self.text()[-600:]}"
            if select.select([self.fd], [], [], 0.1)[0]:
                try:
                    self.seen += os.read(self.fd, 65536)
                except OSError:
                    break

    def type(self, keys: str) -> None:
        os.write(self.fd, keys.encode())

    def quit(self) -> int:
        self.type("\x11")  # Ctrl+Q
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                return os.waitstatus_to_exitcode(status)
            time.sleep(0.05)
        os.kill(self.pid, 9)
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
