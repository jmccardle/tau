"""`tau` itself, in a pseudo-terminal: the launch from `tau.cli.main` to the screen and back (D23
section 1). A real process, so the whole path runs: the terminal check, the theme, `open_client`,
`run_async` on the driver's loop, `Opened.close`. The mock model, a throwaway TAU_HOME."""

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

from world import World, tau

MOCK = {
    "default_model": "m",
    "models": {"m": {"model": "m", "provider": "mock", "options": {"delay_ms": 0}}},
}
OLD = "20261030-091502-1c2d3e"
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


def test_tau_opens_the_screen_answers_and_leaves(served: World) -> None:
    served.user_config(MOCK)
    work = served.home / "work"
    work.mkdir(exist_ok=True)
    screen = Screen(served, "-m", "m", cwd=work)
    screen.wait_for("idle")
    screen.type("hello there\r")
    screen.wait_for("mock: hello there")
    assert screen.quit() == 0


def test_tau_attaches_to_a_server_on_its_own_socket(served: World) -> None:
    served.user_config(MOCK)
    work = served.home / "work"
    work.mkdir(exist_ok=True)
    served.session(OLD, cwd=work)
    custom = served.home / "mine.sock"
    started = tau(served, "serve", "-d", "--session", OLD, "--socket", str(custom), cwd=work)
    assert started.code == 0, started.err
    screen = Screen(served, "--session", OLD, cwd=work)
    screen.wait_for(OLD)
    screen.type("over the wire\r")
    screen.wait_for("mock: over the wire")
    assert screen.quit() == 0
