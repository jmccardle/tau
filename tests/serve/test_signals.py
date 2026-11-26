"""SIGTERM, SIGHUP and a second SIGINT end each program that holds a session (`tau -p`, the
terminal UI, `tau serve`) through the shutdown a normal exit runs: the driver stops, and every
extension's `stop` ends the processes it started (`tau._signals`). Real processes, with the two
built-ins that start some: `mcp` (the test server, which starts a `sleep` of its own) and `hooks`
(a `UserPromptSubmit` command that never ends: `sleep` in the background and in the foreground).
Each test asserts that nothing is left in either one's process group."""

import sys

import pytest

if sys.platform == "win32":
    pytest.skip(
        "Windows: no SIGTERM to catch and no SIGHUP (tau._signals)", allow_module_level=True
    )

import contextlib
import json
import os
import shlex
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

from ptykit import Screen
from world import World

SERVER = Path(__file__).resolve().parents[1] / "mcp_server.py"
CONFIG = {
    "default_model": "m",
    "models": {"m": {"model": "m", "provider": "mock", "options": {"delay_ms": 0}}},
    "load": {"mcp": True, "hooks": True},
}


def _group_of(pid: int) -> int | None:
    """The process group of `pid`; `None` if it is gone or a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    state, _ppid, group = stat.rsplit(") ", 1)[1].split()[:3]
    return None if state == "Z" else int(group)


def _members(group: int) -> list[int]:
    """The live processes of process group `group`."""
    return [int(n) for n in os.listdir("/proc") if n.isdigit() and _group_of(int(n)) == group]


class Rig:
    """A TAU_HOME whose session starts an MCP server (with a child) and, on each message, a hook
    command that never ends; the process groups of both, once they run."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.log = world.home / "mcp.log"
        self.hook_pid = world.home / "hook.pid"
        world.user_config(CONFIG)
        server = {
            "command": sys.executable,
            "args": [str(SERVER)],
            "env": {"MCP_LOG": str(self.log), "SPAWN_CHILD": "1"},
        }
        (world.tau / "mcp.json").write_text(json.dumps({"mcpServers": {"t": server}}))
        hook = f"echo $$ > {shlex.quote(str(self.hook_pid))}; sleep 7772 & sleep 7772"
        handler = {"type": "command", "command": hook}
        hooks = {"hooks": {"UserPromptSubmit": [{"hooks": [handler]}]}}
        (world.tau / "hooks.json").write_text(json.dumps(hooks))
        self.groups: list[int] = []

    @property
    def environ(self) -> dict[str, str]:
        return {"HOME": str(self.world.home), "PATH": os.environ["PATH"]}

    def wait(self, *, hook: bool) -> None:
        """Until the MCP server's child runs (and with `hook`, the hook command): their groups."""
        deadline = time.monotonic() + 30
        while True:
            lines = (
                [json.loads(line) for line in self.log.read_text().splitlines()]
                if self.log.exists()
                else []
            )
            child = next((int(e["child"]) for e in lines if "child" in e), None)
            started = not hook or (self.hook_pid.exists() and self.hook_pid.read_text().strip())
            if child is not None and started:
                break
            assert time.monotonic() < deadline, "the MCP server's child or the hook never ran"
            time.sleep(0.05)
        group = _group_of(child)
        assert group is not None
        self.groups = [group]
        if hook:
            self.groups.append(int(self.hook_pid.read_text()))  # it leads a session of its own
        time.sleep(0.3)  # the hook's `sleep`s are started

    def survivors(self, seconds: float = 10) -> list[int]:
        """What is left in the groups after `seconds` at most (an orphan is reaped by init)."""
        deadline = time.monotonic() + seconds
        while (left := [p for g in self.groups for p in _members(g)]) and (
            time.monotonic() < deadline
        ):
            time.sleep(0.05)
        return left

    def clean_up(self) -> None:
        for group in self.groups:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signal.SIGKILL)


@pytest.fixture
def rig(served: World) -> Iterator[Rig]:
    made = Rig(served)
    try:
        yield made
    finally:
        made.clean_up()


def _print(rig: Rig) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "tau", "-p", "--no-session", "go"],
        env=rig.environ,
        cwd=rig.world.home / "work",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _ended(process: subprocess.Popen[bytes]) -> tuple[int, str]:
    try:
        _, err = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    return process.returncode, err.decode()


# -- tau -p


@pytest.mark.parametrize(
    ("number", "said"),
    [(signal.SIGTERM, "tau: stopped by SIGTERM"), (signal.SIGHUP, "tau: stopped by SIGHUP")],
)
def test_p_ends_its_extensions_processes_on_a_signal(
    rig: Rig, number: signal.Signals, said: str
) -> None:
    process = _print(rig)
    rig.wait(hook=True)
    process.send_signal(number)
    code, err = _ended(process)
    assert code == 128 + number, err
    assert err.splitlines() == [said]
    assert rig.survivors() == []


def test_p_ends_its_extensions_processes_on_a_second_interrupt(rig: Rig) -> None:
    """The first Ctrl+C waits for the hook (an abort never cancels one); the second closes the
    session, which ends the hook's command and the MCP server, within `LEAVE_SECONDS`."""
    process = _print(rig)
    rig.wait(hook=True)
    process.send_signal(signal.SIGINT)
    time.sleep(1)  # the first is heard, and the abort is waiting for the hook
    began = time.monotonic()
    process.send_signal(signal.SIGINT)
    code, err = _ended(process)
    assert time.monotonic() - began < 5
    assert code == 130
    assert err == "tau: interrupted\n"
    assert rig.survivors() == []


# -- the terminal UI


def _screen(rig: Rig) -> Screen:
    screen = Screen(rig.world, cwd=rig.world.home / "work", env={"PATH": os.environ["PATH"]})
    screen.wait_for("idle")
    screen.type("go\r")
    rig.wait(hook=True)
    return screen


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_the_tui_ends_its_extensions_processes_on_a_signal(
    rig: Rig, number: signal.Signals
) -> None:
    screen = _screen(rig)
    screen.process.send_signal(number)
    try:
        assert screen.exit_code() == 128 + number
    finally:
        screen.close()
    assert rig.survivors() == []


def test_the_tui_ends_its_extensions_processes_when_its_terminal_closes(rig: Rig) -> None:
    """The terminal itself goes away: the kernel sends SIGHUP, and nothing can be drawn."""
    screen = _screen(rig)
    os.close(screen.fd)
    try:
        assert screen.process.wait(timeout=30) == 128 + signal.SIGHUP
    finally:
        if screen.process.poll() is None:
            screen.process.kill()
            screen.process.wait()
    assert rig.survivors() == []


# -- tau serve


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_serve_ends_its_extensions_processes_on_a_signal(rig: Rig, number: signal.Signals) -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "tau", "serve"],
        env=rig.environ,
        cwd=rig.world.home / "work",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    rig.wait(hook=False)
    assert process.stdout is not None
    process.stdout.readline()  # the banner: the record is written, the handlers are in place
    process.send_signal(number)
    code, err = _ended(process)
    assert code == 0, err
    assert rig.survivors() == []
