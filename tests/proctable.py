"""The process table, as the tests look at it: whether a process is alive and which processes run
with which arguments. Linux reads `/proc`; macOS has none and asks `ps`. Windows has neither, and
the tests that need one are skipped there (the `bash` tool and `tau serve` do not run on it)."""

import os
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
    if sys.platform == "darwin":
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
