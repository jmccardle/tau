"""`bash`: output, exit codes, timeout, and a real abort of the whole process group."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from calls import call, err, ok

from tau_next import BashTool, Outcome, base_tools


def test_the_tool_describes_itself() -> None:
    tool = BashTool()
    assert tool.name == "bash"
    assert tool.parameters["required"] == ["command"]
    assert "30000" in tool.description
    assert "terminated" in tool.description
    assert "no process outlives a call" in tool.description


@pytest.mark.parametrize("name", ["read", "write", "grep"])
def test_the_file_tools_say_the_working_directory_is_a_default_not_a_sandbox(name: str) -> None:
    tool = {t.name: t for t in base_tools()}[name]
    assert "not a sandbox" in tool.description


def test_output_arrives_interleaved_in_the_working_directory(tmp_path: Path) -> None:
    tool = BashTool(tmp_path)
    out = ok(call(tool, command="echo out1; echo err1 >&2; echo out2; pwd"))
    assert out == f"out1\nerr1\nout2\n{tmp_path.resolve()}\n"


def test_stdin_is_closed(tmp_path: Path) -> None:
    assert ok(call(BashTool(tmp_path), command="cat | wc -c")).strip() == "0"


def test_a_nonzero_exit_is_an_error_that_ends_with_the_code(tmp_path: Path) -> None:
    out = call(BashTool(tmp_path), command="echo about to fail; exit 7")
    assert out == Outcome.Err("about to fail\n\ncommand exited with code 7")
    assert err(call(BashTool(tmp_path), command="exit 3")) == "command exited with code 3"


def test_a_command_killed_by_a_signal_names_it(tmp_path: Path) -> None:
    assert err(call(BashTool(tmp_path), command="kill -TERM $$")) == "command killed by signal 15"


def test_bad_calls_are_outcomes(tmp_path: Path) -> None:
    tool = BashTool(tmp_path)
    assert "missing field `command`" in err(call(tool))
    assert "must not be empty" in err(call(tool, command="   "))
    assert "`timeout` must be at least 1 ms" in err(call(tool, command="true", timeout=0))
    assert "unknown field" in err(call(tool, command="true", cwd="/"))


def test_constructor_arguments_are_checked(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        BashTool(tmp_path / "nope")
    with pytest.raises(ValueError, match="timeout"):
        BashTool(tmp_path, timeout_ms=0)
    with pytest.raises(ValueError, match="output limit"):
        BashTool(tmp_path, max_output=0)


def test_a_timeout_kills_the_command_and_what_it_started(tmp_path: Path) -> None:
    tool = BashTool(tmp_path)
    started = time.monotonic()
    out = call(tool, command="echo before; sleep 300 & echo $! > child.pid; wait", timeout=300)
    assert time.monotonic() - started < 10
    assert out == Outcome.Err("before\n\ncommand timed out after 300 ms and was terminated")
    assert gone(int((tmp_path / "child.pid").read_text()))


def test_the_constructor_sets_the_default_timeout(tmp_path: Path) -> None:
    tool = BashTool(tmp_path, timeout_ms=200)
    assert "default 200" in tool.description
    assert "timed out after 200 ms" in err(call(tool, command="sleep 300"))


def test_what_the_shell_leaves_running_is_killed_when_it_ends(tmp_path: Path) -> None:
    ok(call(BashTool(tmp_path), command="sleep 300 & echo $! > child.pid"))
    assert gone(int((tmp_path / "child.pid").read_text()))


def test_cancelling_the_call_kills_the_process_group(tmp_path: Path) -> None:
    """The abort: a long `sleep` (and the shell and a background child) is gone afterwards."""
    pids = tmp_path / "pids"

    async def scenario() -> tuple[float, list[int]]:
        tool = BashTool(tmp_path)
        command = f"echo $$ > {pids}; sleep 300 & echo $! >> {pids}; sleep 300; echo unreachable"
        running = asyncio.create_task(tool.call(json.dumps({"command": command})))
        deadline = time.monotonic() + 10
        while not pids.exists() or len(pids.read_text().split()) < 2:
            assert time.monotonic() < deadline, "the command never started"
            await asyncio.sleep(0.01)
        pid_list = [int(p) for p in pids.read_text().split()]
        assert all(alive(p) for p in pid_list)
        started = time.monotonic()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        return time.monotonic() - started, pid_list

    elapsed, pid_list = asyncio.run(scenario())
    assert elapsed < 5, "cancel waited for the sleep instead of killing it"
    assert len(pid_list) == 2
    assert all(gone(p) for p in pid_list), "a process of the command survived the cancel"


def test_calls_run_in_parallel_and_do_not_block_the_event_loop(tmp_path: Path) -> None:
    async def scenario() -> tuple[float, list[Outcome]]:
        tool = BashTool(tmp_path)
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        watcher = asyncio.create_task(ticker())
        started = time.monotonic()
        outcomes = await asyncio.gather(
            *(tool.call(json.dumps({"command": "sleep 0.5; echo done"})) for _ in range(8))
        )
        elapsed = time.monotonic() - started
        watcher.cancel()
        assert ticks > 10, "the event loop was blocked"
        return elapsed, list(outcomes)

    elapsed, outcomes = asyncio.run(scenario())
    assert outcomes == [Outcome.Ok("done\n")] * 8
    assert elapsed < 2.5, "eight half-second commands ran one after another"


def alive(pid: int) -> bool:
    """Whether the process runs. A zombie awaiting its parent's wait is not running."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def gone(pid: int, within: float = 3.0) -> bool:
    deadline = time.monotonic() + within
    while alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

DIES_WITH_THE_SIGNAL = """
import asyncio, json, sys
sys.path.insert(0, sys.argv[1])
from common import run_until_signalled
from tau_next import BashTool

async def main():
    command = json.dumps({"command": sys.argv[4] + "sleep " + sys.argv[2], "timeout": 600_000})
    await BashTool(sys.argv[3], kill_grace_ms=1500).call(command)

run_until_signalled(main())
"""


def running(argument: str) -> list[int]:
    """The live `sleep <argument>` processes, found by their command line."""
    want = f"sleep\0{argument}\0".encode()
    found = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                if proc.joinpath("cmdline").read_bytes() == want and alive(int(proc.name)):
                    found.append(int(proc.name))
            except OSError:
                continue
    return found


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_a_program_killed_by_sigterm_or_sighup_leaves_no_command_running(
    tmp_path: Path, number: signal.Signals
) -> None:
    """The program installs the handlers `run_until_signalled` installs: the signal cancels
    the call, which kills the command's session. The sleep's duration is unique to this run."""
    duration = f"{1000 + number}.{os.getpid()}"
    program = subprocess.Popen(
        [sys.executable, "-c", DIES_WITH_THE_SIGNAL, str(EXAMPLES), duration, str(tmp_path), ""]
    )
    try:
        deadline = time.monotonic() + 10
        while not running(duration):
            assert time.monotonic() < deadline, "the command never started"
            time.sleep(0.02)
        program.send_signal(number)
        assert program.wait(timeout=10) == 128 + number
        deadline = time.monotonic() + 5
        while running(duration):
            assert time.monotonic() < deadline, "the sleep outlived the program"
            time.sleep(0.02)
    finally:
        program.kill()
        for pid in running(duration):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize(
    ("first", "second"), [(signal.SIGTERM, signal.SIGTERM), (signal.SIGHUP, signal.SIGTERM)]
)
def test_a_second_signal_in_the_grace_period_still_leaves_no_command_running(
    tmp_path: Path, first: signal.Signals, second: signal.Signals
) -> None:
    """A command that ignores SIGTERM lives until the grace period ends and SIGKILL; a second
    cancellation arriving meanwhile does not end the wait."""
    duration = f"{2000 + first}.{os.getpid()}"
    program = subprocess.Popen(
        [
            sys.executable,
            "-c",
            DIES_WITH_THE_SIGNAL,
            str(EXAMPLES),
            duration,
            str(tmp_path),
            "trap '' TERM; ",
        ]
    )
    try:
        deadline = time.monotonic() + 10
        while not running(duration):
            assert time.monotonic() < deadline, "the command never started"
            time.sleep(0.02)
        program.send_signal(first)
        time.sleep(0.4)
        assert running(duration), "the command should still be inside its grace period"
        program.send_signal(second)
        assert program.wait(timeout=10) == 128 + first
        assert not running(duration), "the program left before the command was dead"
    finally:
        program.kill()
        for pid in running(duration):
            os.kill(pid, signal.SIGKILL)
