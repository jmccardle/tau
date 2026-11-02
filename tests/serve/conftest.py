"""Fixtures for the wire's tests (D21): a `Harness` that serves, and a TAU_HOME for `tau serve`
processes that are always killed when the test is over."""

import contextlib
import os
import shutil
import signal
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from harness import Harness
from world import World


@pytest.fixture(autouse=True)
def keep_cwd() -> Iterator[None]:
    """`tau serve` in this process changes into the session's working directory (the shared
    start, D18); give it back."""
    before = os.getcwd()
    yield
    os.chdir(before)


@pytest.fixture
def remote() -> Iterator[None]:
    """The `Harness` serves its driver and its clients are remote."""
    Harness.mode = "remote"
    yield
    Harness.mode = "local"


def _processes_for(base: Path) -> list[int]:
    """Every process whose command line names `base`: a `tau serve -d` of the test, which has
    its TAU_HOME's pending-log path in its arguments."""
    found: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            command = (proc / "cmdline").read_bytes()
        except OSError:
            continue
        if str(base).encode() in command:
            found.append(int(proc.name))
    return found


@pytest.fixture
def served() -> Iterator[World]:
    """A throwaway TAU_HOME and project, under a short directory (a socket path has a limit).
    Whatever `tau serve` the test left running is killed, even when the test failed."""
    base = Path(tempfile.mkdtemp(prefix="tau"))
    home = base / "h"
    home.mkdir()
    world = World(home, home / ".tau", {"HOME": str(home)})
    (home / "work").mkdir()
    try:
        yield world
    finally:
        for pid in _processes_for(base):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        shutil.rmtree(base, ignore_errors=True)
