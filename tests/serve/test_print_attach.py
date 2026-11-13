"""`tau -p` on a session a `tau serve` holds (D22 section 8): it attaches instead of
failing, refuses what a running server would ignore, and answers or declines only the questions of
its own cursor. The servers are real processes with a mock model, started in a throwaway TAU_HOME
and killed when the test is over."""

import sys

import pytest

if sys.platform == "win32":
    pytest.skip(
        "Windows: tau serve does not run there (a Unix domain socket, D21; not ported)",
        allow_module_level=True,
    )

import argparse
import asyncio
import json
import os
import signal
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
from proctable import alive
from world import Result, World, tau

from tau import (
    PROTOCOL,
    Author,
    ConfigError,
    CursorView,
    Ending,
    Labels,
    RemoteClient,
    Session,
    list_sessions,
    open_client,
)
from tau.cli import extension_flags, session_flags

MOCK = {
    "default_model": "m",
    "models": {"m": {"model": "m", "provider": "mock", "options": {"delay_ms": 0}}},
}
OLD = "20261030-091502-1c2d3e"
GATE = """
from typing import Literal

class Gate:
    name = "gate"

    async def pre_sample(self, cursor, subject):
        verdict = await cursor.ask("run the thing?", Literal["yes", "no"])
        return None if verdict == "yes" else f"verdict: {verdict}"

extensions = [Gate()]
"""
ASKER = '''
from typing import Literal

from tau import Cursor

class Asker:
    name = "asker"

    def __init__(self):
        self.commands = (self.ask,)

    async def ask(self, cursor: Cursor) -> str:
        """Ask."""
        return await cursor.ask("ok?", Literal["yes", "no"])

extensions = [Asker()]
'''


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


def p(world: World, *argv: str) -> Result:
    return tau(world, "-p", *argv, cwd=work(world))


def extension(world: World, name: str, source: str) -> str:
    path = world.home / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return str(path)


def nodes_of(world: World) -> list[str]:
    """The text of the session's nodes, read after its server is gone (a held session cannot
    be read for a lease, only copied)."""
    session = Session.read(world.tau / "sessions" / OLD)
    try:
        return [repr(n.body) for n in session.tree.walk(session.cursor(0).at)]
    finally:
        session.close()


# -- attach


def test_p_on_a_served_session_talks_to_the_server(home: World) -> None:
    record = serving(home)
    result = p(home, "--session", OLD, "hello")
    assert result == Result(0, "mock: hello\n", "")
    stopped = tau(home, "serve", "--stop", "--session", OLD, cwd=work(home))
    assert stopped.code == 0
    assert any("mock: hello" in body for body in nodes_of(home)), "the server wrote the nodes"
    assert record["started"] is True


def test_continue_attaches_too(home: World) -> None:
    serving(home)
    assert p(home, "-c", "hello").out == "mock: hello\n"


def test_json_welcome_is_the_servers(home: World) -> None:
    record = serving(home)
    first = json.loads(p(home, "--session", OLD, "--json", "hello").out.splitlines()[0])
    assert first["kind"] == "Welcome"
    assert (first["pid"], first["session"], first["protocol"]) == (record["pid"], OLD, PROTOCOL)


def test_verbose_says_attached(home: World) -> None:
    record = serving(home)
    err = p(home, "--session", OLD, "--verbose", "hello").err
    assert f"tau: session {OLD} (attached to pid {record['pid']})" in err


@pytest.mark.parametrize(
    ("flags", "named"),
    [
        (["-e", "x.py"], "-e"),
        (["--no-extensions"], "--no-extensions"),
        (["--set", "step_limit.limit=2"], "--set"),
        (["-m", "m"], "--model"),
    ],
)
def test_what_a_running_server_would_ignore_is_refused(
    home: World, flags: list[str], named: str
) -> None:
    serving(home)
    result = p(home, "--session", OLD, *flags, "hello")
    assert result.code == 1
    assert result.out == ""
    assert named in result.err


def test_a_name_is_refused_for_a_session_a_server_holds(home: World) -> None:
    serving(home)
    result = p(home, "--session", OLD, "-n", "the one", "hello")
    assert result.code == 1
    assert "-n" in result.err
    assert [s.name for s in list_sessions(home.tau / "sessions")] == [None]


def test_a_session_held_by_a_process_that_is_no_server_is_leased(home: World) -> None:
    held = Session.load(home.session(OLD, cwd=work(home)))
    try:
        result = p(home, "--session", OLD, "hello")
    finally:
        held.close()
    assert result.code == 1
    assert f"tau --fork {OLD}" in result.err


def test_a_fork_never_attaches(home: World) -> None:
    serving(home)
    result = p(home, "--fork", OLD, "hello")
    assert result.code == 0, result.err
    assert len(list_sessions(home.tau / "sessions")) == 2


def test_a_record_whose_server_is_dead_is_stale_and_the_session_opens_here(home: World) -> None:
    pid = serving(home)["pid"]
    assert isinstance(pid, int)
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    result = p(home, "--session", OLD, "hello")
    assert result.code == 0, result.err
    assert result.out == "mock: hello\n"


# -- questions: its own cursor only


def test_a_question_on_its_own_cursor_is_answered_by_a_rule(home: World) -> None:
    serving(home, "-e", extension(home, "gate", GATE))
    result = p(home, "--session", OLD, "--answer", "gate=yes", "go")
    assert result.code == 0, result.err
    assert result.out == "mock: go\n"
    assert 'answered question 0 from gate with yes: "run the thing?"' in result.err


def test_a_question_on_its_own_cursor_with_no_rule_stays_open_and_p_stops(home: World) -> None:
    serving(home, "-e", extension(home, "gate", GATE))
    result = p(home, "--session", OLD, "go")
    assert result.code == 3
    assert 'question 0 from gate on cursor 0 has no rule: "run the thing?"' in result.err
    assert "declined" not in result.err
    again = p(home, "--session", OLD, "--answer", "gate=yes", "and then")
    assert again.code == 0, again.err
    assert "answered question 0 from gate with yes" in again.err, "the next -p answers it"


def test_a_decline_rule_declines_a_question_on_its_own_cursor(home: World) -> None:
    serving(home, "-e", extension(home, "gate", GATE))
    result = p(home, "--session", OLD, "--decline", "gate", "go")
    assert result.code == 3, "the gate halts on a declined question"
    assert "declined question 0 from gate on cursor 0 (--decline gate)" in result.err


def test_a_question_on_another_cursor_is_left_to_the_servers_other_clients(home: World) -> None:
    record = serving(home, "-e", extension(home, "asker", ASKER))

    async def scenario() -> None:
        async with await RemoteClient.connect(str(record["socket"]), "test:person") as person:
            snapshot = [await anext(person) for _ in range(2)]
            assert isinstance(snapshot[0], CursorView)
            other = await person.open(snapshot[0].at)
            asking = asyncio.create_task(person.run(other, "ask", {}))
            question = None
            while question is None:
                event = await asyncio.wait_for(anext(person), 5)
                if isinstance(event, CursorView) and event.cursor == other and event.questions:
                    question = event.questions[0]
            result = await asyncio.to_thread(
                p, home, "--session", OLD, "--answer", "*=yes", "--verbose", "go"
            )
            assert result.code == 0, result.err
            assert result.out == "mock: go\n"
            assert f"question {question.id} from asker on cursor {other}" in result.err
            assert "left to the server's other clients" in result.err
            await person.answer(question.id, "no")  # still open: -p did not take it
            assert await asking == "no"

    asyncio.run(scenario())


def slow(world: World, seconds: int) -> None:
    """The mock model takes `seconds` to answer, so a test can act while -p waits."""
    options = {"delay_ms": seconds * 1000}
    world.user_config(
        {**MOCK, "models": {"m": {"model": "m", "provider": "mock", "options": options}}}
    )


def test_a_cursor_another_client_opens_after_p_attached_is_still_left_to_it(home: World) -> None:
    """What -p answers is what its own run opened, not whatever is new since it attached."""
    slow(home, 6)
    record = serving(home, "-e", extension(home, "asker", ASKER))

    async def scenario() -> None:
        async with await RemoteClient.connect(str(record["socket"]), "test:person") as person:
            snapshot = [await anext(person) for _ in range(2)]
            assert isinstance(snapshot[0], CursorView)
            running = asyncio.create_task(
                asyncio.to_thread(p, home, "--session", OLD, "--answer", "*=yes", "--verbose", "go")
            )
            await asyncio.sleep(3)  # -p has attached and sent
            other = await person.open(snapshot[0].at)
            asking = asyncio.create_task(person.run(other, "ask", {}))
            question = None
            while question is None:
                event = await asyncio.wait_for(anext(person), 5)
                if isinstance(event, CursorView) and event.cursor == other and event.questions:
                    question = event.questions[0]
            result = await running
            assert result.code == 0, result.err
            assert f"question {question.id} from asker on cursor {other}" in result.err
            assert "left to the server's other clients" in result.err
            assert "answered question" not in result.err
            await person.answer(question.id, "no")  # still open: -p did not take it
            assert await asking == "no"

    asyncio.run(scenario())


def test_p_prints_the_reply_to_its_own_message_not_another_clients(home: World) -> None:
    """Both write to the one cursor; the other client's message waits behind -p's, and its
    reply comes last."""
    slow(home, 5)
    record = serving(home)

    async def scenario() -> None:
        async with await RemoteClient.connect(str(record["socket"]), "test:person") as person:
            snapshot = [await anext(person) for _ in range(2)]
            assert isinstance(snapshot[0], CursorView)
            running = asyncio.create_task(asyncio.to_thread(p, home, "--session", OLD, "mine"))
            await asyncio.sleep(3)  # -p has attached and sent; the model is answering it
            await person.send(snapshot[0].cursor, "theirs")
            result = await running
            assert result.out == "mock: mine\n", result.err

    asyncio.run(scenario())


# -- open_client


def args(*argv: str) -> argparse.Namespace:
    return argparse.ArgumentParser(parents=[extension_flags(), session_flags()]).parse_args(
        list(argv)
    )


def test_open_client_attaches_to_the_server_that_holds_the_session(home: World) -> None:
    record = serving(home)

    async def opened() -> None:
        found = await open_client(
            args("--session", OLD), client="test:open", environ=home.environ, cwd=work(home)
        )
        try:
            assert isinstance(found.client, RemoteClient)
            assert found.started is None
            assert found.welcome is not None
            assert (found.welcome.pid, found.welcome.session) == (record["pid"], OLD)
            assert found.cursor == 0
            assert isinstance(found.snapshot[0], CursorView)
            assert isinstance(found.snapshot[-1], Labels), "every event up to the first Labels"
        finally:
            await found.close()

    asyncio.run(opened())


class Person:
    """A person's client on a served session (the TUI, say), with a cursor of their own or the
    shared first one, whose message waits on the gate's question."""

    def __init__(self, client: RemoteClient) -> None:
        self.client = client
        self.cursor = 0
        self.question = -1

    async def ask(self, *, shared: bool) -> None:
        first = [await anext(self.client) for _ in range(2)]
        assert isinstance(first[0], CursorView)
        self.cursor = 0 if shared else await self.client.open(first[0].at)
        await self.client.send(self.cursor, "person says")
        while self.question < 0:
            event = await asyncio.wait_for(anext(self.client), 10)
            if isinstance(event, CursorView) and event.cursor == self.cursor and event.questions:
                self.question = event.questions[0].id

    async def rested(self) -> CursorView:
        while True:
            event = await asyncio.wait_for(anext(self.client), 10)
            if isinstance(event, CursorView) and event.cursor == self.cursor and not event.busy:
                return event


def test_a_refused_value_aborts_only_the_cursor_p_sent_to(home: World) -> None:
    record = serving(home, "-e", extension(home, "gate", GATE))

    async def scenario() -> None:
        async with await RemoteClient.connect(str(record["socket"]), "test:person") as client:
            person = Person(client)
            await person.ask(shared=False)
            result = await asyncio.to_thread(
                p, home, "--session", OLD, "--answer", "gate=maybe", "go"
            )
            assert result.code == 1
            assert "--answer gate=maybe" in result.err
            await client.answer(person.question, "yes")  # still open: nobody aborted it
            view = await person.rested()
            assert view.ending == Ending.Done()

    asyncio.run(scenario())


def test_p_answers_by_its_rules_a_question_already_open_on_its_cursor(home: World) -> None:
    record = serving(home, "-e", extension(home, "gate", GATE))

    async def scenario() -> None:
        async with await RemoteClient.connect(str(record["socket"]), "test:person") as client:
            person = Person(client)
            await person.ask(shared=True)  # the first cursor, which -p attaches to as well
            result = await asyncio.to_thread(
                p, home, "--session", OLD, "--answer", "gate=yes", "go"
            )
            assert result.code == 0, result.err
            assert f"answered question {person.question} from gate with yes" in result.err
            assert result.out == "mock: go\n"

    asyncio.run(scenario())


def test_a_question_open_on_its_cursor_with_no_rule_stops_p_before_it_sends(home: World) -> None:
    record = serving(home, "-e", extension(home, "gate", GATE))

    async def scenario() -> None:
        async with await RemoteClient.connect(str(record["socket"]), "test:person") as client:
            person = Person(client)
            await person.ask(shared=True)
            result = await asyncio.to_thread(p, home, "--session", OLD, "go")
            assert result.code == 3
            assert (
                f'question {person.question} from gate on cursor 0 has no rule: "run the thing?"'
                in result.err
            )
            assert "it stays open" in result.err
            assert result.out == "", "it stopped before it sent its message"
            assert "declined" not in result.err
            await client.answer(person.question, "yes")  # still open: the person answers it
            view = await person.rested()
            assert view.ending == Ending.Done()

    asyncio.run(scenario())


def test_open_client_refuses_own_extensions_when_it_attaches(home: World) -> None:
    serving(home)

    class Mine:
        name = "mine"

    async def opened() -> None:
        with pytest.raises(ConfigError, match="own= extensions"):
            await open_client(
                args("--session", OLD),
                client="test:open",
                environ=home.environ,
                cwd=work(home),
                own=[Mine()],
            )

    asyncio.run(opened())


# -- questions of the cursors its own run opens (a sub-agent's)


class _Delegating(BaseHTTPRequestHandler):
    """The parent calls `agent("sub")`; the agent calls `bash("touch ran")`; both then say so."""

    ARGS: ClassVar[dict[str, str]] = {"task": "sub"}

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body["messages"]
        task = self.ARGS["task"]
        child = any(m["role"] == "user" and m["content"] == task for m in messages)
        if messages[-1]["role"] == "tool":
            delta, finish = (
                {"role": "assistant", "content": "child done" if child else "done"},
                "stop",
            )
        else:
            name, arguments = (
                ("bash", {"command": "touch ran"}) if child else ("agent", dict(self.ARGS))
            )
            call = {
                "index": 0,
                "id": "c0",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            delta, finish = {"tool_calls": [call]}, "tool_calls"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {
            "id": "x",
            "object": "chat.completion.chunk",
            "model": "f",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


class _Resuming(_Delegating):
    """As `_Delegating`, but the parent continues the agent `agent:1` the session already has."""

    ARGS: ClassVar[dict[str, str]] = {"task": "more", "to": "agent:1"}


def _serving_model(home: World, handler: type[BaseHTTPRequestHandler]) -> Iterator[World]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    home.user_config(
        {
            "default_model": "m",
            "models": {"m": {"model": "m", "base_url": url}},
            "load": {"agents": True},
            "extensions": {"permissions": {"ask": ["bash"]}},
        }
    )
    work(home).mkdir(exist_ok=True)
    try:
        yield home
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def delegating(home: World) -> Iterator[World]:
    yield from _serving_model(home, _Delegating)


@pytest.fixture
def resuming(home: World) -> Iterator[World]:
    yield from _serving_model(home, _Resuming)


def test_a_sub_agents_question_is_answered_by_the_attached_run_not_left_hanging(
    delegating: World,
) -> None:
    serving(delegating)
    result = p(delegating, "--session", OLD, "--answer", "permissions=yes", "go")
    assert result.code == 0, result.err
    assert result.out == "done\n"
    assert "answered question" in result.err
    assert (work(delegating) / "ran").exists(), "the agent's bash call ran"


def test_without_a_rule_the_attached_run_stops_at_a_sub_agents_question(delegating: World) -> None:
    serving(delegating)
    result = p(delegating, "--session", OLD, "go")
    assert result.code == 3
    assert "has no rule" in result.err
    assert "declined" not in result.err
    assert not (work(delegating) / "ran").exists(), "the call has not run"


def test_a_decline_rule_declines_a_sub_agents_question(delegating: World) -> None:
    serving(delegating)
    result = p(delegating, "--session", OLD, "--decline", "permissions", "go")
    assert result.code == 0, result.err
    assert result.out == "done\n"
    assert "declined question" in result.err
    assert not (work(delegating) / "ran").exists(), "the call was refused"


def test_an_agent_the_session_already_had_open_is_the_attached_runs_when_it_is_sent_a_task(
    resuming: World,
) -> None:
    """A crash left `agent:1` open at rest, so it is in the attach snapshot; the run's model sends
    it a follow-up, and its question is the run's to answer: no other client is there to."""

    def leftover(made: Session, tip: int) -> int:
        made.set_label("agent:1", made.open(tip, Author.Core(), 0))
        return tip

    resuming.session(OLD, cwd=work(resuming), build=leftover)
    serving(resuming)
    result = p(resuming, "--session", OLD, "--answer", "permissions=yes", "go")
    assert result.code == 0, result.err
    assert result.out == "done\n"
    assert "left to the server's other clients" not in result.err
    assert (work(resuming) / "ran").exists(), "the agent's bash call ran"
