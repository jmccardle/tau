"""`examples/ratatui`: a client of `tau serve` written in Rust, driven in a pseudo-terminal against
a real server (docs/plan.md, Phase 8D). The binary is built once per test run from the example's
own package; the server has the mock model, a throwaway TAU_HOME, and dies with the test. The
screen is read back through a terminal emulator (the program draws only what changed), the session
through its log after the server has stopped."""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from ptykit import Screen
from websockets.asyncio.server import ServerConnection, unix_serve
from world import World, tau

from tau import PROTOCOL, RemoteClient, Session

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "examples" / "ratatui" / "Cargo.toml"
SCHEMA = REPO / "docs" / "protocol.schema.json"
CTRL_C = "\x03"
CTRL_D = "\x04"
ESC = "\x1b"
DOWN = "\x1b[B"
OLD = "20261030-091502-1c2d3e"
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
            "options": {"delay_ms": 400, "reply": "one two three four five six seven eight"},
        }
    },
}
GATE = """
from typing import Literal

class Gate:
    name = "gate"

    async def pre_sample(self, cursor, subject):
        verdict = await cursor.ask("run the thing?", Literal["yes", "no"])
        return None if verdict == "yes" else f"verdict: {verdict}"

extensions = [Gate()]
"""
COUNT = """
class Count:
    name = "count"

    async def pre_sample(self, cursor, subject):
        n = await cursor.ask("how many?", int)
        return None if n is None else f"count: {n}"

extensions = [Count()]
"""
FLOOD = '''
from tau import Cursor

class Flood:
    name = "flood"

    def __init__(self):
        self.commands = (self.flood,)

    async def flood(self, cursor: Cursor, count: int, text: str) -> str:
        """Tell every client `text`, `count` times."""
        for _ in range(count):
            cursor.tell(text)
        return "flooded"

extensions = [Flood()]
'''


@pytest.fixture(scope="session")
def binary() -> Path:
    """The example, built once. Without cargo the test is skipped locally and fails in CI, where
    a skipped test would hide that the example does not build."""
    cargo = shutil.which("cargo", path=f"{os.path.expanduser('~/.cargo/bin')}:{os.environ['PATH']}")
    if cargo is None:
        if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("cargo is not installed in CI, so examples/ratatui cannot be built")
        pytest.skip("cargo is not installed, so examples/ratatui cannot be built")
    built = subprocess.run(
        [cargo, "build", "--locked", "--manifest-path", str(MANIFEST)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    return MANIFEST.parent / "target" / "debug" / "tau-ratatui"


@pytest.fixture
def home(served: World) -> World:
    served.user_config(MOCK)
    return served


def work(world: World) -> Path:
    return world.home / "work"


def serving(world: World, *argv: str) -> dict[str, object]:
    """`tau serve -d` for the session OLD; its record."""
    if not (world.tau / "sessions" / OLD).exists():
        world.session(OLD, cwd=work(world))
    result = tau(world, "serve", "-d", "--json", "--session", OLD, *argv, cwd=work(world))
    assert result.code == 0, result.err
    return json.loads(result.out)


def extension(world: World, name: str, source: str) -> str:
    path = world.home / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return str(path)


OPEN: list[Screen] = []


@pytest.fixture(autouse=True)
def close_screens() -> Iterator[None]:
    """Whatever a test opened is closed (killed, if still running) however the test ended: no
    `tau-ratatui` outlives its test."""
    yield
    while OPEN:
        OPEN.pop().close()


def client(world: World, binary: Path, *argv: str, **kwargs: str) -> Screen:
    """The example on a terminal, quitting with Ctrl+C."""
    screen = Screen(
        world,
        *argv,
        cwd=work(world),
        program=binary,
        quit_key=CTRL_C,
        env={"USER": "tester", **kwargs},
    )
    OPEN.append(screen)
    return screen


def idle(screen: Screen) -> None:
    screen.wait_for("idle | cursor 0", on_screen=True)


def kill_server(pid: int) -> None:
    """SIGKILL a server, as a crash does, and wait until it no longer runs (a process nobody
    has reaped yet is a zombie, which is gone for our purposes)."""
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        if stat.rpartition(")")[2].split()[0] == "Z":
            return
        time.sleep(0.02)
    raise AssertionError(f"server {pid} did not die")


def log_of(world: World) -> list[str]:
    """The text of the session's nodes, once its server has stopped (a held session cannot be
    read)."""
    stopped = tau(world, "serve", "--stop", "--session", OLD, cwd=work(world))
    assert stopped.code == 0, stopped.err
    session = Session.read(world.tau / "sessions" / OLD)
    try:
        return [repr(n.body) for n in session.tree.walk(session.cursor(0).at)]
    finally:
        session.close()


# -- a chat


def test_type_a_line_see_the_reply_and_leave_with_the_terminal_restored(
    home: World, binary: Path
) -> None:
    serving(home)
    screen = client(home, binary)  # no flags: the one server of this directory
    idle(screen)
    assert f"tau {OLD}" in screen.display().splitlines()[0]
    assert "tester@" in screen.display().splitlines()[0]
    assert screen.display().splitlines()[0].rstrip().endswith(":ratatui")
    screen.type("hello there\r")
    screen.wait_for("assistant: mock: hello there", on_screen=True)
    assert "you: hello there" in screen.display()
    assert screen.quit() == 0
    assert b"\x1b[?1049h" in screen.seen, "it took the alternate screen"
    assert screen.seen.rindex(b"\x1b[?1049l") > screen.seen.rindex(b"hello there"), (
        "and gave it back"
    )
    assert screen.seen.rindex(b"\x1b[?25h") > screen.seen.rindex(b"hello there"), "and the cursor"
    assert any("mock: hello there" in body for body in log_of(home))


def test_ctrl_d_leaves_too_and_the_walk_of_an_old_session_is_shown(
    home: World, binary: Path
) -> None:
    home.session(OLD, cwd=work(home), says=["first thing", "second thing"])
    serving(home)
    screen = client(home, binary, "--session", OLD)
    screen.wait_for("t: second thing", on_screen=True)
    assert "t: first thing" in screen.display()
    screen.type(CTRL_D)
    assert screen.exit_code() == 0


def test_the_socket_flag_needs_no_record(home: World, binary: Path) -> None:
    record = serving(home)
    screen = client(home, binary, "--socket", str(record["socket"]))
    idle(screen)
    assert screen.quit() == 0


def test_a_slash_line_runs_a_command_and_an_unknown_one_goes_as_text(
    home: World, binary: Path
) -> None:
    serving(home)
    screen = client(home, binary)
    idle(screen)
    screen.type("/nosuchthing at all\r")
    screen.wait_for("no command /nosuchthing: sent as text", on_screen=True)
    screen.wait_for("assistant: mock: /nosuchthing at all", on_screen=True)
    screen.type("/model\r")  # a command whose argument is missing: this client has no forms
    screen.wait_for("needs model", on_screen=True)
    screen.wait_for("> /model", on_screen=True)  # and the line is back in the editor
    assert screen.quit() == 0


def test_what_another_client_says_is_shown_under_its_name(home: World, binary: Path) -> None:
    record = serving(home)
    screen = client(home, binary)
    idle(screen)

    async def other_says() -> None:
        async with await RemoteClient.connect(str(record["socket"]), "ann@elsewhere:web") as ann:
            await ann.send(0, "hello from ann")
            deadline = time.monotonic() + 10
            async for event in ann:
                if time.monotonic() > deadline or "mock: hello from ann" in repr(event):
                    break

    asyncio.run(other_says())
    screen.wait_for("ann@elsewhere:web: hello from ann", on_screen=True)
    screen.wait_for("assistant: mock: hello from ann", on_screen=True)
    assert screen.quit() == 0


# -- esc


def test_esc_aborts_a_busy_cursor(home: World, binary: Path) -> None:
    home.user_config(SLOW)
    serving(home)
    screen = client(home, binary)
    idle(screen)
    screen.type("go\r")
    screen.wait_for("busy", on_screen=True)
    screen.wait_for("assistant: one", on_screen=True)
    screen.type(ESC)
    idle(screen)
    assert "eight" not in screen.display(), "the reply was cut short"
    assert screen.quit() == 0
    assert any("Interrupted" in body for body in log_of(home))


# -- questions


def test_a_question_is_answered_from_its_choices(home: World, binary: Path) -> None:
    serving(home, "-e", extension(home, "gate", GATE))
    screen = client(home, binary)
    idle(screen)
    screen.type("go\r")
    screen.wait_for("gate asks: run the thing?", on_screen=True)
    assert "> yes" in screen.display()
    screen.type(DOWN + "\x1b[A")  # down, then back up: still yes
    screen.type("\r")
    screen.wait_for("assistant: mock: go", on_screen=True)
    assert "asks:" not in screen.display(), "the question closed"
    assert screen.quit() == 0
    assert any("mock: go" in body for body in log_of(home))


def test_a_question_is_declined_with_esc(home: World, binary: Path) -> None:
    serving(home, "-e", extension(home, "gate", GATE))
    screen = client(home, binary)
    idle(screen)
    screen.type("go\r")
    screen.wait_for("gate asks: run the thing?", on_screen=True)
    screen.type(ESC)
    screen.wait_for("the step was halted: verdict: None", on_screen=True)
    assert "asks:" not in screen.display()
    assert screen.quit() == 0
    assert not any("mock: go" in body for body in log_of(home)), "declined: no reply was sampled"


def test_a_choice_other_than_the_first_is_answered(home: World, binary: Path) -> None:
    serving(home, "-e", extension(home, "gate", GATE))
    screen = client(home, binary)
    idle(screen)
    screen.type("go\r")
    screen.wait_for("gate asks: run the thing?", on_screen=True)
    screen.type(DOWN + "\r")
    screen.wait_for("the step was halted: verdict: no", on_screen=True)
    assert screen.quit() == 0


def test_a_typed_answer_is_checked_before_it_is_sent(home: World, binary: Path) -> None:
    serving(home, "-e", extension(home, "count", COUNT))
    screen = client(home, binary)
    idle(screen)
    screen.type("go\r")
    screen.wait_for("count asks: how many?", on_screen=True)
    screen.type("lots\r")
    screen.wait_for("is not an integer", on_screen=True)
    assert "asks: how many?" in screen.display(), "still asked"
    screen.type("\x15" + "7\r")  # Ctrl+U clears the field
    screen.wait_for("the step was halted: count: 7", on_screen=True)
    assert screen.quit() == 0


# -- the end


def test_the_server_stopping_on_purpose_ends_the_client_cleanly(home: World, binary: Path) -> None:
    serving(home)
    screen = client(home, binary)
    idle(screen)
    stopped = tau(home, "serve", "--stop", "--session", OLD, cwd=work(home))
    assert stopped.code == 0, stopped.err
    assert screen.exit_code() == 0
    assert "the server stopped" in screen.text()
    assert screen.seen.rindex(b"\x1b[?1049l") > screen.seen.rindex(b"idle"), "terminal restored"


def test_a_cut_off_client_says_hello_again_and_reads_the_walk_again(
    home: World, binary: Path
) -> None:
    record = serving(home, "-e", extension(home, "flood", FLOOD))
    screen = client(home, binary)
    idle(screen)
    screen.type("before\r")
    screen.wait_for("assistant: mock: before", on_screen=True)
    screen.type("draft")

    async def flood(count: int, text: str, *, stopped: bool) -> None:
        if stopped:
            os.kill(screen.pid, signal.SIGSTOP)
        try:
            async with await RemoteClient.connect(
                str(record["socket"]), "ann@elsewhere:web"
            ) as ann:
                await ann.run(0, "flood", {"count": count, "text": text})
        finally:
            if stopped:
                os.kill(screen.pid, signal.SIGCONT)

    asyncio.run(flood(1, "a notice", stopped=False))
    screen.wait_for("a notice", on_screen=True)  # the status line holds the last notice
    # While the client is stopped the server has more for it than its stream holds.
    asyncio.run(flood(400, "x" * 20000, stopped=True))
    deadline = time.monotonic() + 20
    while "a notice" in screen.display() or "x" * 40 in screen.display():
        assert time.monotonic() < deadline, "the client never started over"
        screen.pump()  # a client that started over has a model with no notices
    screen.wait_for("you: before", on_screen=True)
    assert screen.display().count("you: before") == 1, "the walk was read again, not added to"
    screen.wait_for("> draft", on_screen=True)
    screen.type("\x15after\r")
    screen.wait_for("assistant: mock: after", on_screen=True)
    assert screen.quit() == 0


def test_a_cut_off_client_whose_server_is_gone_leaves_with_a_reason(
    home: World, binary: Path
) -> None:
    record = serving(home, "-e", extension(home, "flood", FLOOD))
    screen = client(home, binary)
    idle(screen)

    async def flood_then_die() -> None:
        os.kill(screen.pid, signal.SIGSTOP)
        try:
            async with await RemoteClient.connect(
                str(record["socket"]), "ann@elsewhere:web"
            ) as ann:
                await ann.run(0, "flood", {"count": 400, "text": "x" * 20000})
            os.kill(int(str(record["pid"])), signal.SIGKILL)
        finally:
            os.kill(screen.pid, signal.SIGCONT)

    asyncio.run(flood_then_die())
    assert screen.exit_code() == 1
    assert "cut off by the server, and could not reconnect" in " ".join(screen.text().split())


# -- failures to connect


def test_nothing_to_connect_to_names_tau_serve(home: World, binary: Path) -> None:
    screen = client(home, binary)
    assert screen.exit_code() == 1
    assert "tau serve -d" in screen.text()


def test_a_crashed_servers_record_is_not_a_server(home: World, binary: Path) -> None:
    other = "20261030-091503-1c2d3f"
    pids = []
    for session in (OLD, other):
        home.session(session, cwd=work(home))
        started = tau(home, "serve", "-d", "--json", "--session", session, cwd=work(home))
        assert started.code == 0, started.err
        pids.append(int(json.loads(started.out)["pid"]))
    kill_server(pids[0])
    assert (home.tau / "serve" / f"{OLD}.json").exists(), "its record is left behind"
    screen = client(home, binary)  # two records for this directory, one of them dead
    idle(screen)
    assert f"tau {other}" in screen.display().splitlines()[0]
    assert screen.quit() == 0
    assert (home.tau / "serve" / f"{OLD}.json").exists(), "and this client does not delete it"


def test_with_only_a_dead_record_there_is_no_server(home: World, binary: Path) -> None:
    started = serving(home)
    kill_server(int(str(started["pid"])))
    screen = client(home, binary)
    assert screen.exit_code() == 1
    shown = " ".join(screen.text().split())
    assert "1 stale" in shown
    assert "tau serve -d" in shown


def test_a_stale_socket_is_reported_with_its_path(home: World, binary: Path) -> None:
    gone = home.home / "gone.sock"
    screen = client(home, binary, "--socket", str(gone))
    assert screen.exit_code() == 1
    assert "cannot connect to" in screen.text()
    assert "gone.sock" in screen.text()


@pytest.fixture
def refusing(home: World) -> Iterator[tuple[Path, str]]:
    """A server that answers `hello` as a server of another protocol does, on a socket of its own:
    its path and the message it answers with."""
    names = json.loads(SCHEMA.read_text(encoding="utf-8"))["$defs"]["ErrorCode"]
    code = dict(zip(names["x-names"], names["enum"], strict=True))["ProtocolMismatch"]
    path = home.home / "other.sock"
    message = f"this server speaks protocol {PROTOCOL + 1}, not {PROTOCOL}"

    async def refuse(connection: ServerConnection) -> None:
        request = json.loads(await connection.recv())
        assert request["method"] == "hello"
        assert request["params"]["protocol"] == PROTOCOL
        error = {"code": code, "message": message}
        await connection.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": error}))
        await connection.close()

    loop = asyncio.new_event_loop()
    stop: asyncio.Future[None] = loop.create_future()
    ready = threading.Event()

    async def serve() -> None:
        async with unix_serve(refuse, str(path)):
            ready.set()
            await stop

    thread = threading.Thread(target=loop.run_until_complete, args=(serve(),))
    thread.start()
    assert ready.wait(10)
    try:
        yield path, message
    finally:
        loop.call_soon_threadsafe(stop.set_result, None)
        thread.join(10)
        loop.close()


def test_a_protocol_mismatch_exits_with_the_servers_message(
    home: World, binary: Path, refusing: tuple[Path, str]
) -> None:
    path, message = refusing
    screen = client(home, binary, "--socket", str(path))
    assert screen.exit_code() == 1
    shown = " ".join(screen.text().split())
    assert "protocol mismatch" in shown
    assert message in shown
