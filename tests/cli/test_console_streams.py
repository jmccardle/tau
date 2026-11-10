"""`tau -p` as a well-mannered console program: every kind of standard input (a terminal, a
pipe, a file, /dev/null, an open pipe that never speaks, a closed descriptor) and every kind of
standard output (a pipe, a closed descriptor, a reader that went away). Real descriptors, so
these start the command in a subprocess or hand `main` a pipe."""

import sys

import pytest

if sys.platform == "win32":
    pytest.skip(
        "Windows: no pseudo-terminals (the pty, fcntl and termios modules are Unix only)",
        allow_module_level=True,
    )

import io
import os
import pty
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from world import World, tau

from tau.cli import main

MOCK = {
    "default_model": "m",
    "models": {"m": {"model": "m", "provider": "mock", "options": {"delay_ms": 0}}},
}
SLOW = {
    "default_model": "m",
    "models": {
        "m": {
            "model": "m",
            "provider": "mock",
            "options": {"reply": " ".join(["slow"] * 40), "delay_ms": 100},
        }
    },
}
SILENT_WAIT = 3  # tau._launch.STDIN_WAIT, which tests can only observe


@pytest.fixture
def configured(world: World) -> World:
    world.user_config(MOCK)
    return world


def run(
    world: World, *argv: str, shell: str | None = None, **streams: object
) -> subprocess.CompletedProcess[bytes]:
    """`tau -p ARGV` as a process of its own. `shell` is a prefix such as `exec <&-;` that sets
    its descriptors up before `python -m tau` starts. Streams default to /dev/null in, pipes out."""
    command: list[str] = [sys.executable, "-m", "tau", "-p", "--no-session", *argv]
    settings: dict[str, object] = {
        "env": {"HOME": str(world.home), "PATH": os.environ["PATH"]},
        "cwd": world.home,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": 60,
    }
    settings.update(streams)
    if shell is not None:
        command = ["sh", "-c", shell + ' exec "$@"', "sh", *command]
    return subprocess.run(command, check=False, **settings)  # type: ignore[call-overload]


# -- standard input


def test_a_file_on_stdin_is_read(configured: World) -> None:
    path = configured.home / "diff.txt"
    path.write_text("the diff\n", encoding="utf-8")
    with path.open("rb") as stdin:
        done = run(configured, "review", stdin=stdin)
    assert done.stdout == b"mock: review\n\nthe diff\n"
    assert done.stderr == b""


def test_a_pipe_that_has_sent_its_data_and_closed_is_read(configured: World) -> None:
    done = run(configured, "review", stdin=None, input=b"piped\n")
    assert done.stdout == b"mock: review\n\npiped\n"
    assert done.stderr == b""


def test_dev_null_is_no_input_and_the_words_go_alone(configured: World) -> None:
    done = run(configured, "just", "words")
    assert (done.returncode, done.stdout, done.stderr) == (0, b"mock: just words\n", b"")


def test_dev_null_and_no_words_is_a_usage_error(configured: World) -> None:
    done = run(configured)
    assert done.returncode == 2
    assert done.stdout == b""
    assert b"nothing to send" in done.stderr
    assert b"Traceback" not in done.stderr


def test_a_terminal_is_never_read(configured: World) -> None:
    master, slave = pty.openpty()
    try:
        with os.fdopen(slave, "rb", closefd=False) as stdin:
            done = run(configured, "no", "wait", stdin=stdin)
    finally:
        os.close(master)
        os.close(slave)
    assert (done.returncode, done.stdout, done.stderr) == (0, b"mock: no wait\n", b"")


def test_a_closed_stdin_is_no_input(configured: World) -> None:
    done = run(configured, "alone", shell="exec <&-;")
    assert (done.returncode, done.stdout, done.stderr) == (0, b"mock: alone\n", b"")


def test_a_closed_stdin_and_no_words_is_a_usage_error(configured: World) -> None:
    done = run(configured, shell="exec <&-;")
    assert done.returncode == 2
    assert b"nothing to send" in done.stderr
    assert b"Traceback" not in done.stderr


def test_a_stdin_that_stays_open_and_silent_is_given_up_on_with_a_message(
    configured: World,
) -> None:
    """cron, ssh and CI steps leave stdin open. The words go alone, and stderr says why it took
    a few seconds."""
    process = subprocess.Popen(
        [sys.executable, "-m", "tau", "-p", "--no-session", "the", "words"],
        env={"HOME": str(configured.home), "PATH": os.environ["PATH"]},
        cwd=configured.home,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None  # held open: never written to, never closed
    try:
        started = time.monotonic()
        process.wait(timeout=60)
        waited = time.monotonic() - started
        assert process.stdout is not None
        assert process.stderr is not None
        out, err = process.stdout.read(), process.stderr.read()
    finally:
        process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    assert waited < SILENT_WAIT + 30
    assert process.returncode == 0
    assert out == b"mock: the words\n"
    assert (
        err
        == (
            f"tau: no standard input within {SILENT_WAIT} s: going on without it "
            "(`-` as the first word waits for it)\n"
        ).encode()
    )


def piped(world: World, *argv: str, delay: float, data: bytes) -> tuple[int, str, str]:
    """`main` with a pipe on stdin that sends `data` after `delay` seconds and then closes."""
    read, write = os.pipe()
    out, err = io.StringIO(), io.StringIO()

    def later() -> None:
        time.sleep(delay)
        os.write(write, data)
        os.close(write)

    writer = threading.Thread(target=later)
    writer.start()
    with os.fdopen(read, encoding="utf-8") as stdin:
        code = main(
            ["-p", "--no-session", *argv],
            stdin=stdin,
            stdout=out,
            stderr=err,
            environ=world.environ,
            cwd=world.home,
        )
    writer.join()
    return code, out.getvalue(), err.getvalue()


def test_a_pipe_that_speaks_within_the_wait_is_read_to_its_end(configured: World) -> None:
    code, out, err = piped(configured, "review", delay=1, data=b"late diff\n")
    assert (code, out, err) == (0, "mock: review\n\nlate diff\n", "")


def test_a_lone_dash_waits_for_stdin_as_long_as_it_takes_and_says_so_once(
    configured: World,
) -> None:
    code, out, err = piped(configured, "-", "review", delay=4, data=b"slow diff\n")
    assert code == 0
    assert out == "mock: review\n\nslow diff\n"
    assert err == ("tau: still reading standard input: it has not ended (`</dev/null` skips it)\n")


def test_a_dash_alone_makes_stdin_the_message(configured: World) -> None:
    assert tau(configured, "-p", "-", stdin="from the pipe").out == "mock: from the pipe\n"


def test_a_first_dash_is_not_part_of_the_words(configured: World) -> None:
    result = tau(configured, "-p", "-", "review", "this", stdin="data")
    assert result.out == "mock: review this\n\ndata\n"


def test_a_dash_on_a_dev_null_stdin_and_no_words_is_nothing_to_send(configured: World) -> None:
    done = run(configured, "-")
    assert done.returncode == 2
    assert b"nothing to send" in done.stderr


def test_stdin_that_is_not_text_is_a_usage_error_not_a_traceback(configured: World) -> None:
    path = configured.home / "binary"
    path.write_bytes(b"\xff\xfe\x00 not text")
    with path.open("rb") as stdin:
        done = run(configured, "look", stdin=stdin)
    assert done.returncode == 2
    assert b"standard input is not UTF-8 text" in done.stderr
    assert b"Traceback" not in done.stderr


# -- standard output


def test_a_closed_stdout_is_an_error_not_a_traceback(configured: World) -> None:
    done = run(configured, "hi", shell="exec >&-;")
    assert done.returncode == 1
    assert done.stderr == b"tau: standard output is closed\n"


def test_a_full_stdout_is_one_line_and_status_1(configured: World) -> None:
    with Path("/dev/full").open("wb") as stdout:
        done = run(configured, "hi", stdout=stdout)
    assert done.returncode == 1
    assert b"Traceback" not in done.stderr
    assert done.stderr.startswith(b"tau: ")


class _Gone(io.StringIO):
    """A standard output whose reader went away."""

    def __init__(self, *, writes: bool) -> None:
        super().__init__()
        self.writes = writes

    def write(self, s: str, /) -> int:
        if self.writes:
            raise BrokenPipeError(32, "Broken pipe")
        return super().write(s)

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


@pytest.mark.parametrize("writes", [True, False], ids=["at the write", "at the flush"])
@pytest.mark.parametrize("argv", [["--version"], ["sessions"], ["-p", "hi"]])
def test_any_command_whose_reader_went_away_is_a_quiet_141(
    configured: World, argv: list[str], writes: bool
) -> None:
    err = io.StringIO()
    code = main(
        argv,
        stdin=io.StringIO(""),
        stdout=_Gone(writes=writes),
        stderr=err,
        environ=configured.environ,
        cwd=configured.home,
    )
    assert code == 141
    assert err.getvalue() == ""


def test_head_closing_a_pipe_on_a_plain_command_leaves_quietly(configured: World) -> None:
    """`tau sessions --json | head -0`-like: the reader is gone before the first write."""
    read, write = os.pipe()
    os.close(read)
    with os.fdopen(write, "wb") as stdout:
        done = run(configured, "-v", stdout=stdout)
    assert done.stderr == b""
    assert done.returncode in (0, 141)


def test_no_words_waits_for_stdin_like_a_dash_and_says_so(configured: World) -> None:
    code, out, err = piped(configured, delay=4, data=b"make output\n")
    assert code == 0
    assert out == "mock: make output\n"
    assert err == ("tau: still reading standard input: it has not ended (`</dev/null` skips it)\n")


def test_a_dash_after_the_first_word_is_a_word(configured: World) -> None:
    assert tau(configured, "-p", "what", "is", "5", "-", "3", stdin="x").out == (
        "mock: what is 5 - 3\n\nx\n"
    )


def test_a_dash_on_a_terminal_is_a_word(configured: World) -> None:
    master, slave = pty.openpty()
    try:
        with os.fdopen(slave, "rb", closefd=False) as stdin:
            done = run(configured, "what", "is", "5", "-", "3", stdin=stdin)
    finally:
        os.close(master)
        os.close(slave)
    assert (done.returncode, done.stdout, done.stderr) == (0, b"mock: what is 5 - 3\n", b"")


def test_data_that_arrived_but_has_not_ended_is_not_called_nothing(configured: World) -> None:
    read, write = os.pipe()
    os.write(write, b"hi\n")
    err = io.StringIO()
    out = io.StringIO()
    closer = threading.Timer(3.5, os.close, (write,))
    closer.start()
    with os.fdopen(read, encoding="utf-8") as stdin:
        code = main(
            ["-p", "--no-session", "words"],
            stdin=stdin,
            stdout=out,
            stderr=err,
            environ=configured.environ,
            cwd=configured.home,
        )
    closer.join()
    assert code == 0
    assert out.getvalue() == "mock: words\n\nhi\n"
    assert (
        err.getvalue()
        == "tau: still reading standard input: it has not ended (`</dev/null` skips it)\n"
    )


class _BrokenErr(io.StringIO):
    def write(self, s: str, /) -> int:
        raise BrokenPipeError(32, "Broken pipe")


def test_a_broken_stderr_does_not_discard_what_stdout_holds(configured: World) -> None:
    """Standard output is a file with text still buffered; the usage error cannot be written to
    the broken standard error. The buffered text must still reach the file."""
    path = configured.home / "out.txt"
    with path.open("w", encoding="utf-8", buffering=1 << 16) as out:
        out.write("earlier output\n")
        code = main(
            ["-p"],
            stdin=io.StringIO(""),
            stdout=out,
            stderr=_BrokenErr(),
            environ=configured.environ,
            cwd=configured.home,
        )
    assert code == 141
    assert path.read_text(encoding="utf-8") == "earlier output\n"
