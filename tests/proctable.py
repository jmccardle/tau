"""The process table, as the tests look at it: whether a process is alive and which processes run
with which arguments, and how a test kills one. Linux reads `/proc`; macOS has none and asks `ps`;
Windows opens the process to read its exit code, and asks PowerShell for the command lines."""

import os
import signal
import subprocess
import sys

# Starts `sleep 13` in a session of its own (it leaves the command's session), goes on once it
# is there (a bare `setsid ... &` races with the end of the command), and saves its pid in `pid`.
# macOS has no `setsid(1)`: perl calls setsid(2), and `s` in `ps`'s state marks a session leader.
if sys.platform == "darwin":
    LEAVE = (
        "perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV' sleep 13 & p=$!; "
        "until ps -o stat= -p $p | grep -q s; do sleep 0.02; done; "
        "echo $p > pid;"
    )
else:
    LEAVE = (
        "setsid sleep 13 & p=$!; "
        'until [ "$(cut -d\' \' -f6 /proc/$p/stat)" = "$p" ]; do sleep 0.02; done; '
        "echo $p > pid;"
    )


def _ps(*args: str) -> list[str]:
    done = subprocess.run(["ps", *args], capture_output=True, text=True, check=False)
    return done.stdout.splitlines()


def alive(pid: int) -> bool:
    """Whether process `pid` runs. A zombie, awaiting its parent's wait, does not."""
    if sys.platform == "win32":
        return _alive_windows(pid)
    if sys.platform == "darwin":
        state = "".join(_ps("-o", "stat=", "-p", str(pid))).strip()
        return state != "" and not state.startswith("Z")
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def commands() -> dict[int, list[str]]:
    """The arguments of every live process but this one, by pid. On macOS `ps` joins them with
    spaces, so there an argument holding a space reads as two."""
    found: dict[int, list[str]] = {}
    if sys.platform == "win32":
        found = _commands_windows()
    elif sys.platform == "darwin":
        for line in _ps("-axo", "pid=,stat=,args="):
            pid, state, *args = line.split()
            if not state.startswith("Z"):
                found[int(pid)] = args
    else:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/cmdline", "rb") as cmdline:
                    raw = cmdline.read()
            except OSError:
                continue  # gone since the listing
            if alive(int(name)):
                found[int(name)] = raw.decode(errors="replace").split("\0")[:-1]
    found.pop(os.getpid(), None)
    return found


def with_argument(text: str) -> list[int]:
    """The live processes (not this one) one of whose arguments contains `text`."""
    return [pid for pid, args in commands().items() if any(text in arg for arg in args)]


def interpreter(environ: dict[str, str]) -> tuple[str, dict[str, str]]:
    """The program and environment to run this interpreter as a child whose pid is the
    interpreter's own. On Windows a venv's `python.exe` is a launcher that starts the base
    interpreter as a second process; that one is run directly, told its venv by
    `__PYVENV_LAUNCHER__`, as `multiprocessing` (and `tau serve -d`) do."""
    base = getattr(sys, "_base_executable", sys.executable)
    if sys.platform != "win32" or os.path.normcase(base) == os.path.normcase(sys.executable):
        return sys.executable, dict(environ)
    return base, {**environ, "__PYVENV_LAUNCHER__": sys.executable}


def kill(pid: int) -> None:
    """Kill process `pid` at once, as a crash would end it (SIGKILL; on Windows TerminateProcess,
    which is what `os.kill` does there). `ProcessLookupError` if there is none."""
    os.kill(pid, signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL)


def _alive_windows(pid: int) -> bool:
    if sys.platform != "win32":
        raise AssertionError("Windows only")
    import _winapi  # noqa: PLC0415 - Windows only

    try:
        handle = _winapi.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        return _winapi.GetExitCodeProcess(handle) == _winapi.STILL_ACTIVE
    finally:
        _winapi.CloseHandle(handle)


_LIST = (
    "Get-CimInstance Win32_Process | ForEach-Object { "
    '[string]$_.ProcessId + "`t" + [string]$_.CommandLine }'
)


def _commands_windows() -> dict[int, list[str]]:
    """Each process's command line, as one argument: Windows keeps a command line as one string,
    and only the program reading it splits it."""
    done = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _LIST],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    found: dict[int, list[str]] = {}
    for line in done.stdout.splitlines():
        pid, _, line_of_args = line.partition("\t")
        if pid.strip().isdigit() and line_of_args:
            found[int(pid)] = [line_of_args]
    return found
