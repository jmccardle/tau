"""What only Python can check about the base tools: a thread per call, memory held by the
binding, and asyncio cancellation. Everything else is tested in crates/tau-tools."""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from calls import call, err, ok

from tau_next import BashTool, GrepTool, Outcome

# Starts `sleep 13` in a session of its own, and goes on once it is there (a bare
# `setsid ... &` races with the end of the command), saving its pid.
LEAVE = (
    "setsid sleep 13 & p=$!; "
    'until [ "$(cut -d\' \' -f6 /proc/$p/stat)" = "$p" ]; do sleep 0.02; done; '
    "echo $p > pid;"
)


def end(pid: int) -> None:
    subprocess.run(["kill", str(pid)], check=False)


# bash: a process that left the group


def test_cancelling_while_a_process_outside_the_group_holds_the_pipe(tmp_path: Path) -> None:
    async def scenario() -> float:
        tool = BashTool(tmp_path)
        command = f"{LEAVE} sleep 30"
        running = asyncio.create_task(tool.call(json.dumps({"command": command})))
        while not (tmp_path / "pid").exists():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        started = time.monotonic()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 5
    end(int((tmp_path / "pid").read_text()))


def test_a_timeout_over_the_maximum_is_an_error_naming_it(tmp_path: Path) -> None:
    tool = BashTool(tmp_path, timeout_ms=500, max_timeout_ms=1000)
    assert err(call(tool, command="true", timeout=1001)) == (
        "`timeout` of 1001 ms is over the maximum of 1000 ms"
    )
    assert ok(call(tool, command="true", timeout=1000)) == ""
    assert "at most 1000" in tool.description
    with pytest.raises(ValueError, match="over the maximum"):
        BashTool(tmp_path, timeout_ms=2000, max_timeout_ms=1000)


def test_each_call_has_its_own_thread(tmp_path: Path) -> None:
    """Each command waits until 100 are running at once: with fewer threads than that, none
    could finish (the pool of asyncio's default executor is far smaller)."""
    barrier = (
        "mkdir -p here; touch here/$$; "
        "until f=(here/*); [ ${#f[@]} -ge 100 ]; do sleep 0.05; done; echo together"
    )

    async def scenario() -> list[Outcome]:
        tool = BashTool(tmp_path)
        calls = [tool.call(json.dumps({"command": barrier, "timeout": 60_000})) for _ in range(100)]
        return list(await asyncio.gather(*calls))

    assert asyncio.run(scenario()) == [Outcome.Ok("together\n")] * 100


MEASURE = """
import asyncio, json, resource, sys
from tau_next import ReadTool
tool = ReadTool(sys.argv[1])
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
out = asyncio.run(tool.call(json.dumps({"path": "one.txt"})))
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print((after - before) // 1024, "line 1 is cut at byte" in out.value)
"""


def test_a_file_of_one_enormous_line_is_not_held_in_memory(tmp_path: Path) -> None:
    """Measured in a fresh process, so the baseline is not the test run's own peak."""
    with (tmp_path / "one.txt").open("wb") as f:
        for _ in range(200):
            f.write(b"x" * 1_000_000)
    done = subprocess.run(
        [sys.executable, "-c", MEASURE, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    grown_mb, cut = done.stdout.split()
    assert int(grown_mb) < 50, done.stdout
    assert cut == "True"


def test_cancelling_a_grep_over_a_big_tree_returns_promptly(tmp_path: Path) -> None:
    for d in range(50):
        sub = tmp_path / f"d{d}"
        sub.mkdir()
        for f in range(200):
            (sub / f"f{f}.txt").write_text("nothing here\n" * 10)

    async def scenario() -> float:
        tool = GrepTool(tmp_path)
        running = asyncio.create_task(tool.call(json.dumps({"pattern": "zzz"})))
        await asyncio.sleep(0)
        started = time.monotonic()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 2


def running(marker: str) -> bool:
    """Whether a live process has `marker` in its command line."""
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdline = (proc / "cmdline").read_bytes()
            state = (proc / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            continue
        if state != "Z" and marker.encode() in cmdline:
            return True
    return False


def test_cancelling_right_after_the_call_starts_always_ends_the_command(tmp_path: Path) -> None:
    async def scenario() -> float:
        tool = BashTool(tmp_path)
        started = time.monotonic()
        for _ in range(20):
            task = asyncio.create_task(tool.call(json.dumps({"command": "sleep 31.9"})))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 30
    assert not running("sleep\x0031.9")
