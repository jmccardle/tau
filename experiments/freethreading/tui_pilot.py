"""Launch the real `tau` TUI in a pty against the fake server, type a prompt, check the answer renders.

usage: python3 tui_pilot.py <tau-binary> <home> <cwd>
"""
import os
import pty
import select
import sys
import time

tau, home, cwd = sys.argv[1:4]
env = dict(os.environ, HOME=home, TERM="xterm-256color", COLUMNS="160", LINES="50", PYTHON_GIL="0")
pid, fd = pty.fork()
if pid == 0:
    os.chdir(cwd)
    os.execve(tau, [tau, "--no-session", "-nc", "--no-extensions"], env)

buf = bytearray()


def pump(secs):
    end = time.time() + secs
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                buf.extend(os.read(fd, 65536))
            except OSError:
                return


pump(6)
os.write(fd, b"list the toml files")
pump(1)
os.write(fd, b"\n")  # ctrl+j sends
pump(12)
import re
text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\x1b[=>]", "", buf.decode("utf-8", "replace"))
open(home + "/tui-screen.txt", "w").write(text)
print("rendered FAKE-DONE:", "FAKE-DONE" in text, "| saw 4 tool results:", "4 tool results" in text, "| tool names:", [t for t in ("find", "grep", "read", "ls") if t in text], "| saw 'Traceback':", "Traceback" in text, "| bytes:", len(buf))
os.write(fd, b"\x11")  # ctrl+q
pump(3)
try:
    wpid, status = os.waitpid(pid, os.WNOHANG)
    if wpid == 0:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        print("exit: killed after ctrl+q timeout")
    else:
        print("exit status:", os.waitstatus_to_exitcode(status))
except ChildProcessError:
    pass
