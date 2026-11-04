"""`tau -p` (D22): the one-shot client, through `tau.cli.main` with the mock model. What it prints,
what it answers, how it ends. A scripted model is an extension in a file (`-e`): the mock model
only echoes."""

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from websockets.asyncio.server import ServerConnection, unix_serve
from world import Result, World, tau

from tau import (
    PROTOCOL,
    Appended,
    Body,
    CursorView,
    Ending,
    Event,
    Labels,
    RemoteClient,
    Welcome,
    __version__,
    list_sessions,
)
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

LOOP_FOREVER = """
from tau import Body

name = "loop"

async def post_reply(cursor, subject):
    cursor.write(Body.User("continue"))
    return None
"""
GATE = """
from typing import Literal

class Gate:
    name = "gate"

    async def pre_sample(self, cursor, subject):
        verdict = await cursor.ask("run the thing?", Literal["yes", "no"])
        return None if verdict == "yes" else f"verdict: {verdict}"

extensions = [Gate()]
"""
TWO_LINES = """
from typing import Literal

class Gate:
    name = "gate"

    async def pre_sample(self, cursor, subject):
        verdict = await cursor.ask("run the thing?\\nand the second line", Literal["yes", "no"])
        return None if verdict == "yes" else f"verdict: {verdict}"

extensions = [Gate()]
"""
COUNT = """
class Count:
    name = "count"

    async def pre_sample(self, cursor, subject):
        got = await cursor.ask("how many?", int)
        return None if got == 3 else f"got {got}"

extensions = [Count()]
"""
BOOM = """
class Boom:
    name = "boom"

    async def pre_sample(self, cursor, subject):
        raise RuntimeError("it broke")

extensions = [Boom()]
"""
REFUSE = """
class Refuse:
    name = "refuse"

    async def pre_input(self, cursor, subject):
        return "no input today"

extensions = [Refuse()]
"""
COMMANDS = '''
from typing import Literal

from tau import Cursor

class Cmds:
    name = "cmds"

    def __init__(self):
        self.commands = (self.ping, self.shout, self.boom, self.permit, self.wake)

    async def ping(self, cursor: Cursor) -> str:
        """Pong."""
        return "pong"

    async def shout(self, cursor: Cursor, word: str) -> str:
        """Say the word in capitals."""
        return word.upper()

    async def boom(self, cursor: Cursor) -> str:
        """Fail."""
        raise RuntimeError("it broke")

    async def permit(self, cursor: Cursor) -> str:
        """Ask first."""
        return await cursor.ask("really?", Literal["yes", "no"])

    async def wake(self, cursor: Cursor) -> str:
        """Write to this cursor, which then answers."""
        cursor.send(cursor.id, "woken")
        return "sent"

extensions = [Cmds()]
'''


STUBBORN = """
import asyncio, pathlib

MARKS = pathlib.Path(MARKS_DIRECTORY)

class Stubborn:
    name = "stubborn"

    async def pre_sample(self, cursor, subject):
        (MARKS / "started").write_text("x")
        await asyncio.sleep(60)  # hooks are never cancelled: an abort waits for this

extensions = [Stubborn()]
"""


@pytest.fixture(autouse=True)
def keep_cwd() -> Iterator[None]:
    """The shared start changes the process's working directory; give it back."""
    before = os.getcwd()
    yield
    os.chdir(before)


@pytest.fixture
def configured(world: World) -> World:
    world.user_config(MOCK)
    return world


def ext(world: World, name: str, source: str) -> str:
    """An extension file in the home directory; the `-e` value for it."""
    path = world.home / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return str(path)


def p(world: World, *argv: str, stdin: str = "") -> Result:
    return tau(world, "-p", *argv, stdin=stdin)


def sessions(world: World) -> list[str]:
    directory = world.tau / "sessions"
    return sorted(d.name for d in directory.iterdir()) if directory.exists() else []


def objects(result: Result) -> list[dict[str, object]]:
    return [json.loads(line) for line in result.out.splitlines()]


# -- --version


@pytest.mark.parametrize("flag", ["-v", "--version"])
def test_version_prints_tau_and_the_protocol_it_speaks(world: World, flag: str) -> None:
    result = tau(world, flag)
    assert result == Result(0, f"tau {__version__} (protocol {PROTOCOL})\n", "")


# -- the message


def test_the_answer_is_the_reply_on_stdout_and_nothing_else_is_said(configured: World) -> None:
    result = p(configured, "hello")
    assert result == Result(0, "mock: hello\n", "")


def test_the_words_are_joined_with_single_spaces(configured: World) -> None:
    assert p(configured, "fix", "the", "test").out == "mock: fix the test\n"


def test_a_message_that_is_all_stdin_is_stdin(configured: World) -> None:
    assert p(configured, stdin="from a pipe").out == "mock: from a pipe\n"


def test_stdin_follows_the_words_after_a_blank_line(configured: World) -> None:
    result = p(configured, "review", "this", stdin="the diff\n")
    assert result.out == "mock: review this\n\nthe diff\n"


def test_nothing_to_send_is_a_usage_error_and_creates_no_session(configured: World) -> None:
    for stdin in ("", "  \n"):
        result = p(configured, stdin=stdin)
        assert result.code == 2
        assert result.out == ""
        assert "nothing to send" in result.err
    assert sessions(configured) == []


def test_the_new_session_is_kept_and_no_session_keeps_none(configured: World) -> None:
    p(configured, "one")
    (kept,) = list_sessions(configured.tau / "sessions")
    assert kept.first == "one"
    p(configured, "--no-session", "two")
    assert sessions(configured) == [kept.id]


def test_continue_adds_to_the_newest_session(configured: World) -> None:
    p(configured, "one")
    result = p(configured, "-c", "two")
    assert result.out == "mock: two\n"
    (only,) = list_sessions(configured.tau / "sessions")
    assert only.id == sessions(configured)[0]


def test_resume_is_a_usage_error_for_print(configured: World) -> None:
    result = p(configured, "-r", "hello")
    assert result.code == 2
    assert "-r" in result.err
    assert sessions(configured) == []


def test_a_word_that_names_a_subcommand_is_a_message_after_p(configured: World) -> None:
    assert p(configured, "sessions").out == "mock: sessions\n"


def test_a_start_error_is_one_message_and_exit_1(world: World) -> None:
    world.user_config({"models": {"m": {"model": "m", "provider": "mock"}}})
    result = p(world, "hello")
    assert result.code == 1
    assert result.out == ""
    assert result.err.startswith("tau: no model chosen")
    assert sessions(world) == []


def test_model_picks_the_model_of_a_new_session(world: World) -> None:
    world.user_config(
        {
            "models": {
                "a": {"model": "a", "provider": "mock", "options": {"reply": "from a"}},
                "b": {"model": "b", "provider": "mock", "options": {"reply": "from b"}},
            }
        }
    )
    assert p(world, "-m", "b", "hi").out == "from b\n"
    assert p(world, "--model", "a", "hi").out == "from a\n"
    assert p(world, "hi").code == 1, "no default_model: the start says so"


# -- exit statuses


def test_a_failed_step_is_exit_1_with_what_failed(configured: World) -> None:
    result = p(configured, "-e", ext(configured, "boom", BOOM), "go")
    assert result.code == 1
    assert result.out == ""
    assert "tau: hook error: pre_sample of boom: RuntimeError: it broke" in result.err
    assert "tau: failed: " in result.err
    assert "it broke" in result.err


def test_a_halt_is_exit_3_and_says_why(configured: World) -> None:
    result = p(configured, "-e", ext(configured, "refuse", REFUSE), "go")
    assert result.code == 3
    assert result.out == ""
    assert "no input today" in result.err
    assert "tau: stopped before finishing: halted: no input today" in result.err


def test_auto_step_turned_off_is_exit_3_and_says_so(configured: World) -> None:
    result = p(configured, "--set", "step_limit.limit=1", "go")
    assert result.code == 3
    assert result.out == "mock: go\n", "the reply is whole; the stop is still told"
    assert "tau: stopped before finishing: auto-step is off" in result.err


def test_a_run_that_finishes_is_exit_0_whatever_the_limit(configured: World) -> None:
    assert p(configured, "--set", "step_limit.limit=50", "go").code == 0


def test_interrupt_aborts_the_step_and_exits_130(world: World) -> None:
    world.user_config(SLOW)
    timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    try:
        result = p(world, "go")
    finally:
        timer.cancel()
    assert result.code == 130
    assert result.out == ""
    assert "tau: interrupted" in result.err
    assert "Traceback" not in result.err
    (kept,) = list_sessions(world.tau / "sessions")
    assert kept.error is None, "the session is left whole and listed"


# -- --set


def test_set_is_for_this_run_only_and_changes_no_file(configured: World) -> None:
    before = (configured.tau / "config.json").read_text(encoding="utf-8")
    assert p(configured, "--set", "step_limit.limit=1", "go").code == 3
    assert (configured.tau / "config.json").read_text(encoding="utf-8") == before
    assert p(configured, "go").code == 0


@pytest.mark.parametrize(
    ("assignment", "complaint"),
    [
        ("nope.limit=1", "extension 'nope' has no options"),
        ("step_limit.nope=1", "has no option 'nope'"),
        ("step_limit.limit=ten", "step_limit.limit"),
        ("step_limit.limit=0", "at least 1"),
    ],
)
def test_a_bad_set_is_a_config_error_before_any_session(
    configured: World, assignment: str, complaint: str
) -> None:
    result = p(configured, "--set", assignment, "go")
    assert result.code == 1
    assert result.out == ""
    assert complaint in result.err
    assert sessions(configured) == []


def test_set_without_a_value_is_a_usage_error(configured: World) -> None:
    with pytest.raises(SystemExit) as stop:
        p(configured, "--set", "step_limit.limit", "go")
    assert stop.value.code == 2


# -- questions and --answer


def test_an_answer_text_globs_the_first_line_of_the_question_only(configured: World) -> None:
    gate = ext(configured, "gate", TWO_LINES)
    hit = p(configured, "-e", gate, "--answer", "gate:run the thing?=yes", "go")
    assert hit.code == 0, hit.err
    assert "answered question 0 from gate with yes" in hit.err
    miss = p(configured, "-e", gate, "--answer", "gate:*second line=yes", "go")
    assert "declined question 0 from gate" in miss.err, "the second line is not in the text"


def test_a_rule_answers_the_question_and_the_run_goes_on(configured: World) -> None:
    result = p(configured, "-e", ext(configured, "gate", GATE), "--answer", "gate=yes", "go")
    assert result.code == 0, result.err
    assert result.out == "mock: go\n"
    assert 'tau: answered question 0 from gate with yes: "run the thing?"' in result.err


def test_no_rule_declines_and_says_so(configured: World) -> None:
    result = p(configured, "-e", ext(configured, "gate", GATE), "go")
    assert result.code == 3
    assert result.out == ""
    assert 'tau: declined question 0 from gate: "run the thing?"' in result.err


def test_the_first_matching_rule_wins(configured: World) -> None:
    gate = ext(configured, "gate", GATE)
    result = p(
        configured,
        "-e",
        gate,
        "--answer",
        "step_limit=yes",
        "--answer",
        "gate:something else*=yes",
        "--answer",
        "gate:run the*=no",
        "--answer",
        "gate=yes",
        "go",
    )
    assert result.code == 3
    assert "tau: stopped before finishing: halted: verdict: no" in result.err


def test_a_star_asks_any_extension(configured: World) -> None:
    result = p(configured, "-e", ext(configured, "gate", GATE), "--answer", "*=yes", "go")
    assert result.code == 0


def test_the_value_is_read_by_the_questions_schema(configured: World) -> None:
    result = p(configured, "-e", ext(configured, "count", COUNT), "--answer", "count=3", "go")
    assert result.code == 0, result.err
    assert (
        p(configured, "-e", str(configured.home / "count.py"), "--answer", "count=4", "go").code
        == 3
    )


def test_a_value_the_schema_refuses_aborts_and_exits_1(configured: World) -> None:
    result = p(configured, "-e", ext(configured, "gate", GATE), "--answer", "gate=maybe", "go")
    assert result.code == 1
    assert result.out == ""
    assert "--answer gate=maybe" in result.err
    assert "question 0 from gate" in result.err
    assert "run the thing?" in result.err
    assert "is not one of" in result.err
    (kept,) = list_sessions(configured.tau / "sessions")
    assert kept.error is None


@pytest.mark.parametrize("rule", ["gate", "=yes", ":x=yes"])
def test_a_rule_needs_an_extension_and_a_value(configured: World, rule: str) -> None:
    with pytest.raises(SystemExit) as stop:
        p(configured, "--answer", rule, "go")
    assert stop.value.code == 2


# -- command lines


def test_a_command_line_runs_the_command_and_prints_its_result(configured: World) -> None:
    cmds = ext(configured, "cmds", COMMANDS)
    assert p(configured, "-e", cmds, "/ping") == Result(0, "pong\n", "")
    assert p(configured, "-e", cmds, "/shout", "hello").out == "HELLO\n"


def test_missing_arguments_are_a_usage_error_naming_them(configured: World) -> None:
    result = p(configured, "-e", ext(configured, "cmds", COMMANDS), "/shout")
    assert result.code == 2
    assert result.out == ""
    assert "word" in result.err
    assert sessions(configured) == [], "the session was never used"


def test_a_command_that_fails_is_exit_1(configured: World) -> None:
    result = p(configured, "-e", ext(configured, "cmds", COMMANDS), "/boom")
    assert result.code == 1
    assert "it broke" in result.err


def test_a_command_may_ask_and_a_rule_answers(configured: World) -> None:
    cmds = ext(configured, "cmds", COMMANDS)
    result = p(configured, "-e", cmds, "--answer", "cmds=yes", "/permit")
    assert result == Result(0, "yes\n", 'tau: answered question 0 from cmds with yes: "really?"\n')


def test_a_command_that_wakes_the_cursor_is_waited_for_until_the_model_finishes(
    configured: World,
) -> None:
    result = p(configured, "-e", ext(configured, "cmds", COMMANDS), "--verbose", "/wake")
    assert result.code == 0, result.err
    assert result.out == "sent\nmock: woken\n", "the command's result, then the reply it caused"
    assert 'node 2 User: "woken"' in result.err
    assert 'Assistant: "mock: woken"' in result.err, "the run ended after the model's answer"


def template(world: World, name: str, text: str) -> None:
    (world.tau / "prompts").mkdir(parents=True, exist_ok=True)
    (world.tau / "prompts" / f"{name}.md").write_text(text, encoding="utf-8")


def test_a_template_command_prints_the_models_reply(configured: World) -> None:
    template(configured, "review", "Review $1 carefully")
    assert p(configured, "/review", "main.rs") == Result(0, "mock: Review main.rs carefully\n", "")


def test_a_template_command_under_json_prints_events_not_the_reply(configured: World) -> None:
    template(configured, "review", "Review $1 carefully")
    result = p(configured, "--json", "/review", "main.rs")
    assert result.code == 0
    assert "Review main.rs carefully" in result.out
    assert not any(line == "mock: Review main.rs carefully" for line in result.out.splitlines())


def test_a_template_named_like_a_built_in_leaves_the_built_in_its_name(configured: World) -> None:
    template(configured, "model", "Pick a model")
    assert p(configured, "/model", "m").code == 0
    assert p(configured, "/skills:model").out == "mock: Pick a model\n"


def test_a_template_command_that_stops_before_the_model_answers_is_exit_3(
    configured: World,
) -> None:
    template(configured, "review", "Review $1")
    configured.user_config({**MOCK, "extensions": {"step_limit": {"limit": 1}}})
    result = p(configured, "-e", ext(configured, "loop", LOOP_FOREVER), "/review", "x")
    assert result.code == 3, result.err


def test_an_unknown_command_is_sent_as_text_with_a_warning(configured: World) -> None:
    result = p(configured, "/nonsense", "now")
    assert result.code == 0
    assert result.out == "mock: /nonsense now\n"
    assert "tau: no command /nonsense: sent as text" in result.err


def test_a_slash_alone_is_a_usage_error(configured: World) -> None:
    assert p(configured, "/").code == 2


# -- --json


class _Replay:
    """The lines `-p --json` printed, read by a `RemoteClient` of the wire (D21): a fake server that
    says `hello` and sends each line as an `event`."""

    def __init__(self, lines: list[dict[str, object]], where: Path) -> None:
        self.lines, self.where = lines, where

    async def serve(self, socket: ServerConnection) -> None:
        request = json.loads(await socket.recv())
        welcome = {"protocol": PROTOCOL, "version": "x", "session": "s", "cwd": "/", "pid": 1}
        await socket.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": welcome}))
        for line in self.lines:
            await socket.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": line}))
        await socket.send(
            json.dumps({"jsonrpc": "2.0", "method": "end", "params": {"error": None}})
        )
        await socket.wait_closed()

    async def read(self) -> list[Event]:
        async with (
            unix_serve(self.serve, self.where),
            await RemoteClient.connect(self.where, "test:replay") as client,
        ):
            return [e async for e in client]


def decoded(lines: list[dict[str, object]]) -> list[Event]:
    """What a client of the wire makes of the lines (a socket path has a length limit: a short
    directory)."""
    with tempfile.TemporaryDirectory(prefix="tau") as where:
        return asyncio.run(_Replay(lines, Path(where) / "s").read())


def test_json_is_a_welcome_then_the_events_the_wire_would_send(configured: World) -> None:
    result = p(configured, "--json", "hi")
    assert result.code == 0, result.err
    assert result.err == ""
    first, *events = objects(result)
    (kept,) = sessions(configured)
    welcome = Welcome(**{k: v for k, v in first.items() if k != "kind"})  # type: ignore[arg-type]
    assert first["kind"] == "Welcome"
    assert (welcome.session, welcome.protocol) == (kept, PROTOCOL)
    assert welcome.pid == os.getpid()
    assert welcome.cwd == str(configured.home.resolve())
    heard = decoded(events)
    # A client's stream keeps one view per cursor and drops the Live it did not ask for: what it
    # decoded is the events' end state, and every line fit the schema or it would have raised.
    assert isinstance(heard[0], Labels | CursorView)
    appended = [n for e in heard if isinstance(e, Appended) for n in e.nodes]
    replies = [n.body for n in appended if isinstance(n.body, Body.Assistant)]
    assert [part.text for part in replies[-1].parts] == ["mock: hi"]  # type: ignore[attr-defined]
    kinds = [event["kind"] for event in events]
    assert kinds[0] == "CursorView", "the snapshot first"
    assert "Live" in kinds, "the watched cursor's Live"
    last = [e for e in heard if isinstance(e, CursorView)][-1]
    assert not last.busy
    assert last.ending == Ending.Done()


def test_json_with_no_session_has_no_welcome_line(configured: World) -> None:
    result = p(configured, "--json", "--no-session", "hi")
    assert result.code == 0, result.err
    assert all(line["kind"] != "Welcome" for line in objects(result))
    assert objects(result)[0]["kind"] == "CursorView"
    assert sessions(configured) == []


def test_json_failure_ends_with_an_error_line_and_stderr_still_tells(configured: World) -> None:
    result = p(configured, "--json", "-e", ext(configured, "boom", BOOM), "go")
    assert result.code == 1
    *_, last = objects(result)
    assert list(last) == ["error"]
    error = last["error"]
    assert isinstance(error, dict)
    assert error["type"] == "Failed"
    assert "it broke" in error["message"]
    assert "tau: failed: " in result.err


def test_json_start_error_is_only_the_error_line(world: World) -> None:
    world.user_config({"models": {"m": {"model": "m", "provider": "mock"}}})
    result = p(world, "--json", "hi")
    assert result.code == 1
    (line,) = objects(result)
    assert line["error"]["type"] == "ConfigError"  # type: ignore[index]


def test_json_stderr_carries_notices_and_questions_stdout_stays_json(configured: World) -> None:
    result = p(configured, "--json", "-e", ext(configured, "gate", GATE), "go")
    assert result.code == 3
    assert 'tau: declined question 0 from gate: "run the thing?"' in result.err
    for line in result.out.splitlines():
        json.loads(line)


# -- --verbose


def test_verbose_reports_the_start_before_the_run_on_stderr(configured: World) -> None:
    result = p(configured, "--verbose", "hi")
    assert result.code == 0
    assert result.out == "mock: hi\n", "stdout is the answer, as ever"
    (kept,) = sessions(configured)
    err = result.err
    assert f"tau: session {kept} (new)" in err
    assert f"tau: working directory {configured.home.resolve()}" in err
    assert "tau: model m" in err
    assert f"tau: config {configured.tau / 'config.json'}" in err
    assert "tau: extension base_tools" in err
    assert "tau: extension step_limit" in err
    assert "tau: system prompt " in err
    assert err.index("session") < err.index("system prompt") < err.index("node ")


def test_verbose_follows_the_run_node_by_node(configured: World) -> None:
    err = p(configured, "--verbose", "hi").err
    assert 'tau: node 2 User: "hi"' in err
    assert 'tau: node 3 Assistant: "mock: hi"' in err
    assert "tau: step ended: Done" in err


SHOWS = """
class Shows:
    name = "shows"

    async def post_reply(self, cursor, subject):
        cursor.show("plan mode on")
        return None

extensions = [Shows()]
"""


def test_verbose_prints_what_an_extension_shows_when_it_changes(configured: World) -> None:
    err = p(configured, "--verbose", "-e", ext(configured, "shows", SHOWS), "hi").err
    assert "tau: shows: plan mode on\n" in err
    assert err.count("tau: shows: ") == 1, "a line is printed when it changes, not on every view"
    assert "tau: usage: 1 in · 0% cached · 2 out\n" in err, "any extension's, usage included"
    assert err.index("node 3 Assistant") < err.index("tau: usage: ")


def test_without_verbose_a_shown_line_is_not_printed(configured: World) -> None:
    assert p(configured, "hi").err == ""


def test_verbose_shows_a_question_as_it_opens(configured: World) -> None:
    err = p(
        configured, "--verbose", "-e", ext(configured, "gate", GATE), "--answer", "*=yes", "go"
    ).err
    assert 'tau: question 0 from gate: "run the thing?"' in err


def test_verbose_under_no_session_says_in_memory(configured: World) -> None:
    err = p(configured, "--verbose", "--no-session", "hi").err
    assert "tau: session in memory (new)" in err


def test_verbose_with_json_keeps_stdout_to_objects(configured: World) -> None:
    result = p(configured, "--verbose", "--json", "hi")
    assert "tau: session " in result.err
    assert len(objects(result)) > 3


def test_without_verbose_stderr_is_empty(configured: World) -> None:
    assert p(configured, "hi").err == ""


# -- notices


def test_a_notice_is_a_line_on_stderr(configured: World) -> None:
    result = p(configured, "-e", ext(configured, "refuse", REFUSE), "go")
    assert "tau: no input today" in result.err.splitlines()


# -- the second SIGINT leaves at once


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + 30
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never happened"
        time.sleep(0.02)


def test_a_second_interrupt_leaves_at_once_though_a_hook_never_returns(
    configured: World,
) -> None:
    marks = configured.home / "marks"
    marks.mkdir()
    source = STUBBORN.replace("MARKS_DIRECTORY", repr(str(marks)))
    command = [
        sys.executable,
        "-m",
        "tau",
        "-p",
        "--no-session",
        "-e",
        ext(configured, "stubborn", source),
        "go",
    ]
    process = subprocess.Popen(
        command,
        env={"HOME": str(configured.home), "PATH": os.environ["PATH"]},
        cwd=configured.home,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        wait_for(marks / "started")
        process.send_signal(signal.SIGINT)
        time.sleep(1)  # the first is heard, and the abort is waiting for the hook
        process.send_signal(signal.SIGINT)
        out, err = process.communicate(timeout=10)
    finally:
        process.kill()
        process.wait()
    assert process.returncode == 130
    assert out == b""
    assert err == b"tau: interrupted\n"


# -- a closed stdout


class _Closed(io.StringIO):
    """A standard output whose reader went away."""

    def write(self, s: str, /) -> int:
        raise BrokenPipeError(32, "Broken pipe")


def test_a_closed_stdout_is_a_quiet_exit_141(configured: World) -> None:
    err = io.StringIO()
    code = main(
        ["-p", "--json", "hi"],
        stdin=io.StringIO(""),
        stdout=_Closed(),
        stderr=err,
        environ=configured.environ,
        cwd=configured.home,
    )
    assert code == 141
    assert err.getvalue() == ""


def test_head_closing_the_pipe_ends_the_process_quietly(world: World) -> None:
    world.user_config(SLOW)
    process = subprocess.Popen(
        [sys.executable, "-m", "tau", "-p", "--json", "hi"],
        env={"HOME": str(world.home), "PATH": os.environ["PATH"]},
        cwd=world.home,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert process.stderr is not None
        assert json.loads(process.stdout.readline())["kind"] == "Welcome"
        process.stdout.close()  # `| head -1`
        err = process.stderr.read()
        process.wait(timeout=30)
    finally:
        process.kill()
        process.wait()
    assert process.returncode == 141
    assert err == b""


# -- failures on stderr


def test_json_usage_error_after_the_start_ends_with_an_error_line(configured: World) -> None:
    result = p(configured, "--json", "-e", ext(configured, "cmds", COMMANDS), "/shout")
    assert result.code == 2
    *_, last = objects(result)
    assert list(last) == ["error"]
    error = last["error"]
    assert isinstance(error, dict)
    assert error["type"] == "UsageError"
    assert "word" in error["message"]


def test_json_nothing_to_send_is_an_error_line_too(configured: World) -> None:
    result = p(configured, "--json")
    assert result.code == 2
    (line,) = objects(result)
    assert line["error"]["type"] == "UsageError"  # type: ignore[index]


def test_a_failing_command_is_one_line_and_no_traceback(
    configured: World, capsys: pytest.CaptureFixture[str]
) -> None:
    result = p(configured, "-e", ext(configured, "cmds", COMMANDS), "/boom")
    assert result.code == 1
    assert result.err.splitlines()[-1] == "tau: RuntimeError: it broke"
    assert all(line.startswith("tau: ") for line in result.err.splitlines())
    assert capsys.readouterr().err == "", "the traceback is for --verbose"


def test_verbose_shows_the_traceback_on_stderr(
    configured: World, capsys: pytest.CaptureFixture[str]
) -> None:
    result = p(configured, "--verbose", "-e", ext(configured, "cmds", COMMANDS), "/boom")
    assert result.code == 1
    assert "Traceback (most recent call last)" in result.err
    assert "RuntimeError: it broke" in result.err
    assert capsys.readouterr().err == ""


def test_a_failing_hook_is_no_traceback_either(
    configured: World, capsys: pytest.CaptureFixture[str]
) -> None:
    result = p(configured, "-e", ext(configured, "boom", BOOM), "go")
    assert result.code == 1
    assert "Traceback" not in result.err
    assert capsys.readouterr().err == ""


# -- a rule that can match nothing


def test_a_rule_for_an_extension_that_is_not_loaded_is_a_usage_error(configured: World) -> None:
    result = p(configured, "-e", ext(configured, "gate", GATE), "--answer", "gat=yes", "go")
    assert result.code == 2
    assert result.out == ""
    assert "--answer gat=yes" in result.err
    assert "gat" in result.err
    assert "gate" in result.err, "the loaded extensions are named"
    assert "step_limit" in result.err
    assert sessions(configured) == [], "nothing was sent"


def test_a_star_or_a_loaded_extension_is_no_usage_error(configured: World) -> None:
    gate = ext(configured, "gate", GATE)
    assert p(configured, "-e", gate, "--answer", "*=yes", "go").code == 0
    assert p(configured, "-e", gate, "--answer", "gate=yes", "go").code == 0


# -- the report


def test_verbose_start_report_has_no_nested_parentheses(configured: World) -> None:
    err = p(configured, "--verbose", "hi").err
    assert "tau: extension session_name (built-in, added by the startup)" in err
    assert "((" not in err


class _OneCall(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint whose model calls `bash` once and then answers."""

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        after_tool = body["messages"][-1]["role"] == "tool"
        if after_tool:
            deltas = [({"role": "assistant", "content": "done"}, "stop")]
        else:
            call = {
                "index": 0,
                "id": "c0",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "true"})},
            }
            deltas = [({"tool_calls": [call]}, "tool_calls")]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta, finish in deltas:
            chunk = {
                "id": "x",
                "object": "chat.completion.chunk",
                "model": "f",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


def test_verbose_says_one_call_not_1_calls(world: World) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OneCall)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1"
        world.user_config({"default_model": "f", "models": {"f": {"model": "f", "base_url": url}}})
        result = p(world, "--verbose", "go")
    finally:
        server.shutdown()
        server.server_close()
    assert result.code == 0, result.err
    assert "tau: node 3 Assistant: 1 call: bash" in result.err
    assert "calls" not in result.err
