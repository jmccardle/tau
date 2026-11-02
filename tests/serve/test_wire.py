"""The wire (D21 sections 3, 5, 6) as a client sees it: the handshake, bad messages, the order of
events and responses, each way a stream ends, and what a disconnect does. The behavior suite run
remote is the test of the whole `Client`; these are the things only a wire has."""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Literal

import pytest
from fakes import FakeProvider, FakeTool, Gate, Reply
from harness import Harness, scenario
from websockets.asyncio.client import ClientConnection, unix_connect
from websockets.asyncio.server import ServerConnection, unix_serve
from websockets.exceptions import ConnectionClosed

from tau import (
    PROTOCOL,
    Client,
    ConfigError,
    Cursor,
    CursorClosed,
    CursorView,
    Labels,
    NoSuchCursor,
    NoSuchLabel,
    NoSuchNode,
    Notice,
    Part,
    ProtocolMismatch,
    RemoteClient,
    Server,
    Stop,
)
from tau.commandline import UnknownCommand

pytestmark = pytest.mark.usefixtures("remote")

SYS = 1  # the system node's id: a new session is Config 0, System 1


async def dial(h: Harness) -> ClientConnection:
    assert h.socket is not None
    return await unix_connect(h.socket, max_size=None)


async def say(ws: ClientConnection, request_id: int | None, method: str, **params: object) -> None:
    message: dict[str, object] = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        message["id"] = request_id
    await ws.send(json.dumps(message))


async def hear(ws: ClientConnection) -> dict[str, Any]:
    return json.loads(await asyncio.wait_for(ws.recv(), 5))


async def hello(ws: ClientConnection, client: str = "test:raw", **extra: object) -> list[dict]:
    """Say `hello` and return every message up to and including its response."""
    await say(ws, 0, "hello", protocol=PROTOCOL, client=client, **extra)
    heard = [await hear(ws)]
    while "id" not in heard[-1]:
        heard.append(await hear(ws))
    return heard


async def closed(ws: ClientConnection) -> None:
    """The server closes the connection: the next receive says so."""
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(ws.recv(), 5)


def error_of(message: dict[str, object]) -> dict[str, object]:
    found = message["error"]
    assert isinstance(found, dict)
    return found


# The handshake


@scenario
async def test_hello_is_answered_with_the_session_after_the_snapshot(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    ws = await dial(h)
    heard = await hello(ws, "john@laptop:vscode")
    assert [m.get("params", {}).get("kind") for m in heard[:-1]] == ["CursorView", "Labels"]
    assert all(m["method"] == "event" for m in heard[:-1])
    welcome = heard[-1]["result"]
    assert isinstance(welcome, dict)
    assert welcome["protocol"] == PROTOCOL == 1
    assert welcome["session"] == h.directory.name
    assert welcome["cwd"] == str(h.directory)
    assert welcome["pid"] == os.getpid()
    assert isinstance(welcome["version"], str)
    await ws.close()
    await h.close()


@scenario
async def test_another_protocol_is_refused_with_the_servers_and_the_connection_closes(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    ws = await dial(h)
    await say(ws, 0, "hello", protocol=PROTOCOL + 1, client="test:raw")
    refused = error_of(await hear(ws))
    assert refused["code"] == -32006
    assert refused["data"] == {"server": PROTOCOL}
    await closed(ws)
    await h.close()


@scenario
async def test_a_client_is_told_a_server_speaks_another_protocol_as_protocol_mismatch(
    tmp_path: Path,
) -> None:
    """The error object of a server whose protocol is not ours, which only a fake can send."""

    async def other(socket: ServerConnection) -> None:
        request = json.loads(await socket.recv())
        error = {"code": -32006, "message": "this server speaks 2", "data": {"server": 2}}
        await socket.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": error}))

    where = tmp_path / "s"
    async with unix_serve(other, where):
        with pytest.raises(ProtocolMismatch) as refused:
            await RemoteClient.connect(where, "test:old")
    assert refused.value.server == 2
    assert not isinstance(refused.value, ConnectionError), "a reconnect loop must not retry it"


@scenario
@pytest.mark.parametrize(
    "first",
    [
        ("step", {"cursor": 0}),
        ("hello", {"protocol": PROTOCOL}),
        ("hello", {"protocol": "one", "client": "c"}),
        ("hello", {"protocol": PROTOCOL, "client": "c", "buffer": 0}),
    ],
)
async def test_anything_but_a_good_hello_first_closes_the_connection(
    tmp_path: Path, first: tuple[str, dict[str, object]]
) -> None:
    h = await Harness.create(tmp_path)
    ws = await dial(h)
    await say(ws, 7, first[0], **first[1])
    refused = await hear(ws)
    assert refused["id"] == 7
    assert error_of(refused)["code"] in (-32600, -32602, -32009)
    await closed(ws)
    await h.close()


@scenario
async def test_a_second_hello_closes_the_connection(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    ws = await dial(h)
    await hello(ws)
    await say(ws, 1, "hello", protocol=PROTOCOL, client="again")
    assert error_of(await hear(ws))["code"] == -32600
    await closed(ws)
    await h.close()


# Bad messages after hello leave the connection open


@scenario
async def test_bad_messages_are_answered_and_the_connection_stays_open(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    ws = await dial(h)
    await hello(ws)
    await ws.send("{not json")
    assert error_of(await hear(ws))["code"] == -32700
    await ws.send(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "node", "params": {"id": 0}}]))
    assert error_of(await hear(ws))["code"] == -32600, "no batches"
    await say(ws, None, "step", cursor=0)
    assert error_of(await hear(ws))["code"] == -32600, "a client notification"
    await say(ws, 2, "teleport")
    unknown = await hear(ws)
    assert (unknown["id"], error_of(unknown)["code"]) == (2, -32601)
    await say(ws, 3, "close")
    assert error_of(await hear(ws))["code"] == -32601, "closing the connection is how one leaves"
    for id, params in ((4, {"cursr": 0}), (5, {}), (6, {"cursor": "zero"}), (7, {"cursor": True})):
        await say(ws, id, "step", **params)
        bad = await hear(ws)
        assert (bad["id"], error_of(bad)["code"]) == (id, -32602), params
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 8, "method": "step", "params": [0]}))
    assert error_of(await hear(ws))["code"] == -32602, "params are by name"
    await say(ws, 9, "node", id=0)
    good = await hear(ws)
    assert good["id"] == 9
    assert isinstance(good["result"], dict)
    await ws.close()
    await h.close()


@scenario
async def test_the_errors_a_call_raises_are_the_tables_with_the_data_a_row_needs(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    ws = await dial(h)
    await hello(ws)
    asked = [
        ("step", {"cursor": 99}, -32002),
        ("node", {"id": 99}, -32004),
        ("remove_label", {"label": "nope"}, -32005),
        ("answer", {"question": 5, "value": None}, -32007),
        ("nodes", {"start": -1, "limit": 1}, -32009),
        ("options", {"cursor": 0, "extension": "nope"}, -32012),
    ]
    for id, (method, params, code) in enumerate(asked, 1):
        await say(ws, id, method, **params)
        assert error_of(await hear(ws))["code"] == code, method
    await say(ws, 10, "parse_line", cursor=0, line="/zz")
    unknown = error_of(await hear(ws))
    assert (unknown["code"], unknown["data"]) == (-32008, {"name": "zz"})
    await ws.close()
    await h.close()


@scenario
async def test_a_remote_client_raises_the_class_the_code_names(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    other = await h.client.open(SYS)
    await h.client.close_cursor(other)
    with pytest.raises(NoSuchCursor):
        await h.client.step(99)
    with pytest.raises(NoSuchCursor) as gone:  # a closed cursor is caught as a missing one
        await h.client.step(other)
    assert isinstance(gone.value, CursorClosed)
    with pytest.raises(NoSuchNode):
        await h.client.node(99)
    with pytest.raises(NoSuchLabel):
        await h.client.remove_label("nope")
    with pytest.raises(ValueError, match="start"):
        await h.client.nodes(-1, 1)
    with pytest.raises(UnknownCommand) as unknown:
        await h.client.parse_line(h.cursor, "/zz")
    assert unknown.value.name == "zz"
    with pytest.raises(ConfigError):
        await h.client.options(h.cursor, "nope")
    with pytest.raises(TypeError):  # not JSON: refused before anything is sent
        await h.client.answer(0, Path("x"))
    await h.close()


# Order


@scenario
async def test_a_response_arrives_after_the_events_its_call_caused(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    ws = await dial(h)
    await hello(ws)
    await say(ws, 1, "open", at=SYS)
    heard = [await hear(ws)]
    while "id" not in heard[-1]:
        heard.append(await hear(ws))
    cursor = heard[-1]["result"]
    views = [m["params"] for m in heard[:-1] if m["params"]["kind"] == "CursorView"]
    assert [v["cursor"] for v in views] == [cursor], "its view came first"
    await ws.close()
    await h.close()


@scenario
async def test_calls_begin_in_the_order_they_arrive(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    other = await h.client.open(SYS)
    ws = await dial(h)
    await hello(ws)
    await say(ws, 1, "close_cursor", cursor=other)
    await say(ws, 2, "reopen", cursor=other)  # needs the first to have begun, and to have closed
    done: dict[int, dict[str, object]] = {}
    while len(done) < 2:
        message = await hear(ws)
        if "id" in message:
            done[int(str(message["id"]))] = message
    assert done[1]["result"] is None
    assert "error" not in done[2], done[2]
    await ws.close()
    await h.close()


class Asker:
    """A command that asks a question and returns the answer."""

    name = "asker"

    async def ask(self, cursor: Cursor) -> str:
        """Ask."""
        return await cursor.ask("ok?", Literal["yes", "no"])

    def __init__(self) -> None:
        self.commands = (self.ask,)


@scenario
async def test_a_call_that_waits_does_not_hold_back_the_answer_it_needs(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Asker()])
    ws = await dial(h)
    await hello(ws)
    await say(ws, 1, "run", cursor=h.cursor, command="ask", args={})
    question = None
    while question is None:  # the open question arrives as a state event
        message = await hear(ws)
        params = message.get("params", {})
        if params.get("kind") == "CursorView" and params["questions"]:
            question = params["questions"][0]["id"]
    await say(ws, 2, "answer", question=question, value="yes")
    done: dict[int, dict[str, object]] = {}
    while len(done) < 2:
        message = await hear(ws)
        if "id" in message:
            done[int(str(message["id"]))] = message
    assert done[1]["result"] == "yes"
    assert done[2]["result"] is None, "nothing returned is null"
    await ws.close()
    await h.close()


# The end


@scenario
async def test_a_stopped_driver_ends_every_stream_with_a_null_error_and_closes(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    ws = await dial(h)
    await hello(ws)
    client = await h.connect("watcher")
    await h.driver.stop()
    ended = await hear(ws)
    assert ended == {"jsonrpc": "2.0", "method": "end", "params": {"error": None}}
    await closed(ws)
    assert [e async for e in client]  # what it held, then it ends
    with pytest.raises(ConnectionError, match="the driver is stopped"):
        await client.node(0)
    await h.close()


@scenario
async def test_a_failed_driver_ends_every_stream_with_stopped_and_what_failed(
    tmp_path: Path,
) -> None:
    gate = Gate()
    reply = Reply([Part.Call("a", "wait", "{}")], Stop.Tools)
    h = await Harness.create(
        tmp_path, provider=FakeProvider([reply]), tools=[FakeTool("wait", "ok", gate)]
    )
    ws = await dial(h)
    await hello(ws)
    await h.send("hi")
    await h.settle()
    h.session.close()  # the next answer meets a closed session: the pump dies of it
    gate.release()
    while True:
        message = await hear(ws)
        if message.get("method") == "end":
            break
    error = error_of(message["params"])  # type: ignore[arg-type]
    assert (error["code"], error["message"]) == (-32013, "Closed: the session is closed")
    await closed(ws)
    with pytest.raises(ConnectionError, match="Closed: the session is closed"):
        await h.client.node(0)
    await h.driver.stop()


class Shouts:
    """A step that tells every connected client something too big for a socket to hold."""

    name = "shouts"

    def __init__(self, size: int) -> None:
        self.text = "x" * size

    async def pre_step(self, cursor: Cursor, subject: object) -> None:
        for _ in range(12):
            cursor.tell(self.text)


@scenario
async def test_a_client_that_does_not_read_is_cut_off_and_told_so_in_end(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Shouts(300_000)])
    ws = await dial(h)
    await hello(ws, buffer=2)
    await h.send("hi")  # nobody reads `ws`: the socket fills, then the stream's backlog passes 2
    await h.settle()
    last: dict[str, object] = {}
    while last.get("method") != "end":
        last = await hear(ws)
    error = error_of(last["params"])  # type: ignore[arg-type]
    assert error["code"] == -32014
    await closed(ws)
    await h.close()


# Disconnects


class Asks:
    """A `pre_step` hook asking `to` one question, once."""

    name = "asks"

    def __init__(self, to: str) -> None:
        self.to = to
        self.answers: list[object] = []

    async def pre_step(self, cursor: Cursor, subject: object) -> None:
        if not self.answers:
            self.answers.append(await cursor.ask("still yours?", Literal["yes"], to=self.to))


async def asked_to(h: Harness) -> str | None:
    (question,) = (await h.view()).questions
    return question.to


@scenario
async def test_a_dropped_connection_loses_its_client_and_its_question_goes_to_everyone(
    tmp_path: Path,
) -> None:
    asks = Asks("alice")
    h = await Harness.create(tmp_path, provider=FakeProvider([]), extensions=[asks])
    ws = await dial(h)
    await hello(ws, "alice")
    await h.send("hi")
    await h.settle()
    assert await asked_to(h) == "alice"
    ws.transport.abort()  # no closing handshake: the process died
    await asyncio.wait_for(ws.wait_closed(), 5)
    await h.until(lambda: h.driver.state().open_questions == 1)
    for _ in range(100):  # the server notices a closed socket on its own time
        if await asked_to(h) is None:
            break
        await asyncio.sleep(0.01)
    assert await asked_to(h) is None
    await h.respond("yes")
    await h.settle()
    assert asks.answers == ["yes"]
    await h.close()


@scenario
async def test_connections_may_share_an_id_and_the_client_is_lost_with_the_last(
    tmp_path: Path,
) -> None:
    asks = Asks("alice")
    h = await Harness.create(tmp_path, provider=FakeProvider([]), extensions=[asks])
    first = await h.connect("alice")
    second = await h.connect("alice")
    await h.send("hi")
    await h.settle()
    await first.close()
    await h.settle()
    assert await asked_to(h) == "alice", "the other connection is still alice"
    await second.close()
    await h.settle()
    for _ in range(100):
        if await asked_to(h) is None:
            break
        await asyncio.sleep(0.01)
    assert await asked_to(h) is None
    await h.close()


@scenario
async def test_a_command_a_client_began_runs_on_after_it_disconnects(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Asker()])
    caller = await h.connect("caller")
    running = asyncio.ensure_future(caller.run(h.cursor, "ask", {}))
    await h.until(lambda: h.driver.state().open_questions == 1)
    await caller.close()
    with pytest.raises((ConnectionError, asyncio.CancelledError)):
        await running
    assert h.driver.state().open_questions == 1, "the question waits, with nobody connected"
    await h.respond("yes")
    await h.settle()
    assert not h.session.cursor(h.cursor).busy
    await h.close()


# The client is a Client


async def connected(h: Harness) -> Client:
    """A `RemoteClient`, held to the `Client` protocol by the type checker."""
    assert h.socket is not None
    return await RemoteClient.connect(h.socket, "test:typed")


@scenario
async def test_a_remote_client_is_a_client_and_its_stream_opens_with_the_snapshot(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    client = await connected(h)
    assert client.id == "test:typed"
    view, labels = (
        await asyncio.wait_for(anext(client), 5),
        await asyncio.wait_for(anext(client), 5),
    )
    assert isinstance(view, CursorView)
    assert isinstance(labels, Labels)
    await client.close()
    assert [e async for e in client] == [], "closing ends the stream"
    with pytest.raises(ConnectionError):
        await client.node(0)
    await h.close()


@scenario
async def test_what_a_remote_client_hears_equals_what_a_local_one_does(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply([Part.Text("hello")])]))
    local = h.driver.client("local")
    remote = await h.connect("remote")
    await h.send("hi")
    await h.settle()
    await h.driver.stop()
    heard_local = [e async for e in local]
    heard_remote = [e async for e in remote]
    assert heard_remote == heard_local
    assert any(isinstance(e, Notice) for e in heard_remote) is False
    await h.close()


# The server rests (D21 Open 3)


@scenario
async def test_a_server_with_an_idle_limit_returns_when_unused_that_long(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    assert h.socket is not None
    quiet = await Server.start(
        h.driver, h.socket.with_name("idle"), session="s", cwd=h.directory, idle=0.2
    )
    client = await RemoteClient.connect(h.socket.with_name("idle"), "visitor")
    waiting = asyncio.ensure_future(quiet.wait())
    await asyncio.sleep(0.5)
    assert not waiting.done(), "a connected client keeps it up"
    await client.close()
    assert await asyncio.wait_for(waiting, 5) is None, "and when it leaves, the limit runs"
    await h.driver.stop()
    await quiet.close()
    await h.close()


@scenario
async def test_a_server_without_an_idle_limit_returns_only_for_a_failed_driver(
    tmp_path: Path,
) -> None:
    gate = Gate()
    reply = Reply([Part.Call("a", "wait", "{}")], Stop.Tools)
    h = await Harness.create(
        tmp_path, provider=FakeProvider([reply]), tools=[FakeTool("wait", "ok", gate)]
    )
    assert h.socket is not None
    watched = await Server.start(h.driver, h.socket.with_name("w"), session="s", cwd=h.directory)
    waiting = asyncio.ensure_future(watched.wait())
    await asyncio.sleep(0.5)
    assert not waiting.done(), "never rests"
    await h.send("hi")
    await h.settle()
    h.session.close()
    gate.release()
    failure = await asyncio.wait_for(waiting, 5)
    assert failure is not None
    assert str(failure) == "the session is closed"
    await h.driver.stop()
    await watched.close()


# What a client does when things go wrong


@scenario
async def test_cancelling_one_call_leaves_the_other_calls_waiting_for_their_answers(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Asker()])
    other = await h.client.open(SYS)
    caller = await h.connect("caller")
    first = asyncio.ensure_future(caller.run(h.cursor, "ask", {}))
    await h.until(lambda: h.driver.state().open_questions == 1)
    second = asyncio.ensure_future(caller.run(other, "ask", {}))
    await h.until(lambda: h.driver.state().open_questions == 2)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    (question,) = (await h.view(other)).questions
    await h.client.answer(question.id, "yes")
    assert await asyncio.wait_for(second, 5) == "yes", "its answer was not dropped"
    await caller.close()
    await h.close()


@scenario
async def test_an_event_that_does_not_decode_ends_the_stream_and_fails_the_calls(
    tmp_path: Path,
) -> None:
    """A server whose event is not a node: the client must not hang on it."""

    async def broken(socket: ServerConnection) -> None:
        request = json.loads(await socket.recv())
        welcome = {"protocol": PROTOCOL, "version": "x", "session": "s", "cwd": "/", "pid": 1}
        await socket.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": welcome}))
        event = {"kind": "Appended", "nodes": [{"id": "not a node"}]}
        await socket.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": event}))
        async for _ in socket:
            pass

    where = tmp_path / "s"
    async with unix_serve(broken, where):
        client = await RemoteClient.connect(where, "test:broken")
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(anext(client), 5)
        with pytest.raises(ConnectionError, match="protocol"):
            await asyncio.wait_for(client.node(0), 5)
        await asyncio.wait_for(client.close(), 5)
