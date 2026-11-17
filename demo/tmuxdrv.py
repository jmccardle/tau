"""A private tmux server (its own socket, no user config) and the few things the races ask of it."""

from __future__ import annotations

import os
import shlex
import subprocess
import time


class Tmux:
    def __init__(self, name: str | None = None) -> None:
        self.socket = name or f"tau-demo-{os.getpid()}"

    def run(self, *args: str, check: bool = True) -> str:
        r = subprocess.run(
            ["tmux", "-L", self.socket, "-f", "/dev/null", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if check and r.returncode:
            raise RuntimeError(f"tmux {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout

    def capture(self, target: str) -> str:
        return self.run("capture-pane", "-p", "-t", target, check=False)

    def kill(self) -> None:
        self.run("kill-server", check=False)

    def wait_for(self, target: str, text: str, timeout: float, poll: float = 0.01) -> float:
        """Block until `text` is on screen in `target`; return the monotonic time it was seen."""
        deadline = time.perf_counter() + timeout
        while True:
            seen = time.perf_counter()
            if text in self.capture(target):
                return seen
            if seen > deadline:
                raise TimeoutError(
                    f"{text!r} never appeared in {target} within {timeout:.0f}s:\n{self.capture(target)}"
                )
            time.sleep(poll)


def shell_line(env: dict[str, str], argv: list[str], cwd: str) -> str:
    """One command line that runs `argv` in `cwd` under exactly `env`."""
    pairs = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    return f"cd {shlex.quote(cwd)} && exec env -i {pairs} " + " ".join(shlex.quote(a) for a in argv)


def choose_session(tmux: Tmux, target: str, version: str, timeout: float = 120.0) -> None:
    """0.15's TUI lists sessions on `--resume`: take the newest (the only one) as soon as it shows."""
    if version == "15":
        tmux.wait_for(target, "Resume a session", timeout)
        tmux.run("send-keys", "-t", target, "Enter")
