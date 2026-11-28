"""A stdio MCP server's process (D24 section 12), and how it is signalled; from tau-acp's
`child.py`.

A pid is only safe to signal while the process has not been reaped: after that the number can
name another process, and asyncio reaps a child as soon as it exits. Where Linux offers them we
therefore hold a pidfd for the server, opened when it starts, and signal through it. The server
leads its own process group (`start_new_session`), so what it starts (`npx` starts `node`) is
signalled with it; the other members of the group are pinned by pidfds before it is stopped, so
they can be signalled after it has exited. Elsewhere (macOS) it is signalled while asyncio has
not seen it exit, and what is left in its group once it has exited is signalled by the group's
id, which cannot be reused while the group has members (`_terminate_left_group`). On Windows it
is terminated alone.

When τ is killed in a way it cannot clean up after, a server sees its stdin close, which the MCP
specification makes its signal to exit.

A server does not inherit τ's environment: only `INHERITED_ENVIRONMENT`, and what its `env` names.
"""

import asyncio
import contextlib
import os
import select
import signal
import sys
import time
from collections.abc import Mapping
from pathlib import Path

_POSIX = sys.platform != "win32"
_POLL = 0.02  # seconds between looks at whether it has exited

INHERITED_ENVIRONMENT = (
    (
        "APPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "USERNAME",
        "USERPROFILE",
    )
    if sys.platform == "win32"
    else ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")
)
"""The variables of τ's environment a stdio server inherits; anything else it gets only by naming
it in its `env`, so a key τ can read reaches only the servers given it. The MCP Python SDK's
`DEFAULT_INHERITED_ENV_VARS` (mcp 2.3.0, `mcp/client/stdio.py`): a server written against the
SDK's clients needs nothing more."""


def environment(env: Mapping[str, str]) -> dict[str, str]:
    """A stdio server's whole environment: the `INHERITED_ENVIRONMENT` variables τ has, then
    `env` over them. A value that starts with `()` is a shell function, not a setting, and is not
    inherited (as the SDK's `get_default_environment` skips it)."""
    inherited = {
        name: value
        for name in INHERITED_ENVIRONMENT
        if (value := os.environ.get(name)) is not None and not value.startswith("()")
    }
    return {**inherited, **env}


class Child:
    """A process τ started and owns, signalled only while it runs."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.pid = process.pid
        self._fd: int | None = None
        if hasattr(os, "pidfd_open") and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):  # it has exited already
                self._fd = os.pidfd_open(process.pid)

    def running(self) -> bool:
        """Whether it has not exited. While this is true and the caller does not yield to the
        event loop, its pid and its process group id name it and its group, and nothing else."""
        if self.process.returncode is not None:
            return False
        if self._fd is None:  # no pidfds here; or there are, and it had exited when it started
            return not hasattr(os, "pidfd_open")
        return not select.select([self._fd], [], [], 0)[0]  # readable once it exits

    def terminate(self, kill: bool = False) -> None:
        """SIGTERM (`kill`: SIGKILL) to its process group, unless it has exited; on Windows,
        terminate it."""
        if not self.running():
            return
        if not _POSIX:
            self.process.kill() if kill else self.process.terminate()
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pid, signal.SIGKILL if kill else signal.SIGTERM)

    def group(self) -> list[int]:
        """Pidfds for the other processes in its group now, for `terminate_all` once it has
        exited. Empty where there is no `/proc` or no pidfd, or once it has exited."""
        if not self.running() or not hasattr(os, "pidfd_open"):
            return []
        fds: list[int] = []
        for pid in _group_members(self.pid):
            with contextlib.suppress(OSError):  # gone since the listing
                fds.append(os.pidfd_open(pid))
        if self.running():  # it ran throughout: each pid had our group id, so each is ours
            return fds
        close_all(fds)
        return []

    def close(self) -> None:
        """Release the pidfd."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def terminate_all(fds: list[int]) -> None:
    """SIGTERM to each process of a `Child.group` that is still running."""
    for fd in fds:
        with contextlib.suppress(ProcessLookupError):
            signal.pidfd_send_signal(fd, signal.SIGTERM)


def close_all(fds: list[int]) -> None:
    """Release the pidfds of a `Child.group`."""
    for fd in fds:
        os.close(fd)


def _group_members(pgid: int) -> list[int]:
    """The pids whose process group is `pgid`, other than `pgid` itself, from `/proc`."""
    members: list[int] = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        with contextlib.suppress(OSError, ValueError, IndexError):
            fields = stat.read_text().rsplit(")", 1)[1].split()  # after "pid (comm)"
            pid = int(stat.parent.name)
            if pid != pgid and int(fields[2]) == pgid:
                members.append(pid)
    return members


def _terminate_left_group(pgid: int) -> None:
    """SIGTERM to what is left in the process group `pgid` led by a server that has exited,
    where there are no pidfds to pin its members with (macOS).

    The group's id is the server's pid, and the kernel never gives a new process a pid that is
    the id of a process group that still has members (POSIX; Linux and XNU both check), so while
    one member remains, `pgid` names this group and nothing else. Once none remains, `killpg`
    finds no group and fails (`ProcessLookupError`), and nothing is signalled. A process that
    has since been given the number as its pid is in its parent's group, not in a group `pgid`,
    unless it made one of its own in the instant between the last member's exit and this call."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)


async def spawn(
    command: str, args: tuple[str, ...], env: dict[str, str], cwd: Path, limit: int
) -> asyncio.subprocess.Process:
    """Start a server with pipes on stdin, stdout and stderr, leading its own process group
    (POSIX). `OSError` as `create_subprocess_exec` raises it."""
    return await asyncio.create_subprocess_exec(
        command,
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=limit,
        start_new_session=_POSIX,
    )


async def stop(child: Child, grace: float) -> None:
    """Close its stdin, wait `grace`, SIGTERM its group, wait, SIGKILL. What was in its group
    when the stop began also gets SIGTERM once it has exited (where there are no pidfds, what is
    in its group then). A server that exited before the
    stop began is not signalled, nor anything it left: its ids may name someone else's by now.

    Its exit is watched, not `wait`ed for: `Process.wait` also waits for its pipes to close, and
    what it started (`npx`'s `node`) holds them until it is signalled in turn."""
    process = child.process
    ran = child.running()
    group = child.group()
    try:
        if process.stdin is not None:  # also once it has exited: its transport waits for it
            with contextlib.suppress(ConnectionError, RuntimeError):
                process.stdin.close()
        if not await _exits(child, grace):
            child.terminate()
            if not await _exits(child, grace):
                child.terminate(kill=True)
                await _exits(child, grace)
        terminate_all(group)
        if ran and _POSIX and not hasattr(os, "pidfd_open"):
            _terminate_left_group(child.pid)
    finally:
        close_all(group)
        child.close()


async def _exits(child: Child, seconds: float) -> bool:
    """Whether it exits within `seconds`."""
    deadline = time.monotonic() + seconds
    while child.running():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(_POLL)
    return True
