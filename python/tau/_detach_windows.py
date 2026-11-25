"""`tau serve -d`'s child on Windows (D29 section 10): started with no console, in its own
process group, and out of its starter's job when the job allows it.

- **No console** (`DETACHED_PROCESS`): the server has no window, and closing the starter's
  console does not end it.
- **Its own process group** (`CREATE_NEW_PROCESS_GROUP`): a Ctrl+C typed in the starter's console
  is not delivered to it.
- **Out of the job** (`CREATE_BREAKAWAY_FROM_JOB`): an editor, a terminal or a CI runner may run
  its children in a job that is killed when it closes, which would end the server with its
  starter. A job that forbids breakaway refuses the flag; the child is then started in the job,
  and `spawn` says so (`broke_away` false) for `-d` to report.
- **The log** is opened sharing delete, so the child can rename it to `<session id>.log` while
  it writes to it, as on POSIX.
- **The interpreter itself**, not a venv's `python.exe`: that one is a launcher that starts the
  base interpreter as a second process, whose pid would not be the child's. The base interpreter
  is started with `__PYVENV_LAUNCHER__`, as `multiprocessing` does (bpo-35797), so it still runs
  in the venv.
"""

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_BREAKAWAY: Final = 0x01000000
"""`CREATE_BREAKAWAY_FROM_JOB`."""
_SHARE_ALL: Final = 0x7
"""`FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`."""
_CREATE_ALWAYS: Final = 2


@dataclass(frozen=True)
class Spawned:
    """A started child, and whether it left its starter's job (`False`: the job forbade it, and
    the child ends when that job is closed)."""

    process: "subprocess.Popen[bytes]"
    broke_away: bool


def spawn(command: Sequence[str], log: Path, cwd: Path, environ: Mapping[str, str]) -> Spawned:
    """Start `command` (an interpreter, then its arguments) as the detached server, its output to
    `log` (created, or emptied). `OSError` if it cannot be started."""
    if sys.platform != "win32":
        raise AssertionError("Windows only")
    import _winapi  # noqa: PLC0415 - Windows only
    import msvcrt  # noqa: PLC0415 - Windows only

    argv, env = _interpreter(command, environ)
    handle = _winapi.CreateFile(
        str(log), _winapi.GENERIC_WRITE, _SHARE_ALL, 0, _CREATE_ALWAYS, 0, 0
    )
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(msvcrt.open_osfhandle(handle, os.O_WRONLY), "wb") as out:

        def start(creation: int) -> "subprocess.Popen[bytes]":
            return subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                cwd=cwd,
                env=env,
                creationflags=creation,
            )

        try:
            return Spawned(start(flags | _BREAKAWAY), broke_away=True)
        except PermissionError:  # ERROR_ACCESS_DENIED: the job forbids breakaway
            return Spawned(start(flags), broke_away=False)


def _interpreter(
    command: Sequence[str], environ: Mapping[str, str]
) -> tuple[list[str], dict[str, str]]:
    """`command` and its environment, with a venv's launcher replaced by the base interpreter."""
    argv, env = list(command), dict(environ)
    base = getattr(sys, "_base_executable", sys.executable)
    launcher = os.path.normcase(sys.executable)
    if argv and os.path.normcase(argv[0]) == launcher and os.path.normcase(base) != launcher:
        argv[0] = base
        env["__PYVENV_LAUNCHER__"] = sys.executable
    return argv, env
