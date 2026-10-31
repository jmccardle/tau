"""Closing a cursor (D17 section 2): a client's `close_cursor`, a handle's, what is left of the
cursor, what every other call on it says, what clients are sent, and `reopen`."""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from fakes import FakeProvider, Gate, Reply
from harness import Harness, say, scenario

from tau import (
    Body,
    Cursor,
    CursorClosed,
    CursorGone,
    CursorView,
    Ending,
    Labels,
    LocalClient,
    NodeHandle,
    NoSuchCursor,
    Outcome,
    Part,
    Stop,
    Subject,
    TreeHandle,
)

SYS = 1  # the system node's id: a new session is Config 0, System 1


def calls(tool: str) -> Reply:
    return Reply([Part.Call("c", tool, "{}")], Stop.Tools)


def held() -> tuple[Gate, Reply]:
    """A reply that stops at its gate after its first part."""
    gate = Gate()
    return gate, Reply([Part.Text("hel"), Part.Text("lo")], Stop.End, gate, gate_after=1)


async def with_other(tmp_path: Path, **kwargs: object) -> tuple[Harness, int]:
    """A harness and a second cursor, open at the same node, labelled `scout`."""
    h = await Harness.create(tmp_path, **kwargs)  # pyright: ignore[reportArgumentType]
    other = await h.client.open(SYS)
    await h.client.set_label(other, "scout")
    await h.settle()
    return h, other


async def gone(h: Harness) -> list[CursorGone]:
    await h.settle()
    return [e for e in h.heard if isinstance(e, CursorGone)]


# What a close leaves


@scenario
async def test_closing_a_resting_cursor_takes_it_out_of_cursors_and_keeps_its_name(
    tmp_path: Path,
) -> None:
    h, other = await with_other(tmp_path, provider=FakeProvider([say("hi")]))
    await h.send("hello", cursor=other)
    await h.settle()
    last = h.session.cursor(other).at
    await h.client.close_cursor(other)
    assert h.session.cursors() == [h.cursor]
    assert h.session.closed() == [(other, last)], "its last node, not where it started"
    assert h.session.label("scout") == other, "the label is the restart record"
    assert await gone(h) == [CursorGone(other, last)]
    assert Labels((("scout", other),)) in h.heard
    await h.close()


@scenario
async def test_ids_stay_dense_so_the_next_cursor_is_not_the_closed_one(tmp_path: Path) -> None:
    h, other = await with_other(tmp_path)
    await h.client.close_cursor(other)
    assert await h.client.open(SYS) == other + 1
    await h.close()


@scenario
async def test_closing_a_busy_cursor_aborts_it_and_drops_its_pending_input(tmp_path: Path) -> None:
    gate, reply = held()
    h, other = await with_other(tmp_path, provider=FakeProvider([reply]), gates=[gate])
    await h.send("first", cursor=other)
    await h.settle()
    assert gate.held == 1
    await h.input("later", cursor=other)  # waits for the next input phase
    assert b"later" in (h.directory / "scratchpad").read_bytes(), "pending, on disk"
    await h.client.close_cursor(other)
    assert h.session.cursors() == [h.cursor]
    assert [e.cursor for e in await gone(h)] == [other]
    assert b"later" not in (h.directory / "scratchpad").read_bytes(), "gone with the cursor"
    await h.close()


class Asker:
    """A cursor hook that asks before every step and keeps what it was told."""

    name = "asker"

    def __init__(self) -> None:
        self.answers: list[object] = []

    async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
        self.answers.append(await cursor.ask("go on?", bool))


@scenario
async def test_closing_a_cursor_declines_its_open_question(tmp_path: Path) -> None:
    asker = Asker()
    h, other = await with_other(tmp_path, extensions=[asker])  # opening wakes it: it asks
    assert [q.text for q in (await h.view(other)).questions] == ["go on?"]
    await h.client.close_cursor(other)
    assert asker.answers == [None], "declined, as an abort declines it"
    assert h.driver.state().open_questions == 0
    assert h.session.closed()[0][0] == other
    await h.close()


class Watcher:
    """A tool that waits for cursor 1 to rest."""

    name = "watcher"

    def __init__(self) -> None:
        self.seen: list[Ending | None] = []
        self.tools = (self.look,)

    async def look(self, node: NodeHandle) -> str:
        """Wait for cursor 1 to rest."""
        self.seen.append(await node.rest(1))
        return "looked"


@scenario
async def test_a_rest_waiting_on_a_cursor_that_is_closed_sees_the_abort(tmp_path: Path) -> None:
    watcher = Watcher()
    gate, reply = held()
    h, other = await with_other(
        tmp_path,
        provider=FakeProvider([reply, calls("look"), say("done")]),
        extensions=[watcher],
        gates=[gate],
    )
    await h.send("first", cursor=other)
    await h.settle()
    await h.send("go")
    await h.settle()  # the tool waits on the cursor that waits at its gate
    assert watcher.seen == []
    await h.client.close_cursor(other)
    await h.settle()
    assert watcher.seen == [Ending.Aborted()]
    assert h.session.closed()[0][0] == other
    await h.close()


# Every other call on it


def after_close() -> dict[str, Callable[[LocalClient, int], Awaitable[object]]]:
    return {
        "input": lambda c, k: c.input(k, "x"),
        "send": lambda c, k: c.send(k, "x"),
        "step": lambda c, k: c.step(k),
        "go_to": lambda c, k: c.go_to(k, SYS),
        "set_auto": lambda c, k: c.set_auto(k, True),
        "abort": lambda c, k: c.abort(k),
        "set_label": lambda c, k: c.set_label(k, "other"),
        "watch": lambda c, k: c.watch([k]),
        "close_cursor": lambda c, k: c.close_cursor(k),
        "run": lambda c, k: c.run(k, "model", {}),
    }


@pytest.mark.parametrize("call", list(after_close()))
@scenario
async def test_every_client_call_on_a_closed_cursor_raises_cursor_closed(
    tmp_path: Path, call: str
) -> None:
    h, other = await with_other(tmp_path)
    await h.client.close_cursor(other)
    with pytest.raises(CursorClosed, match=f"cursor {other} is closed") as raised:
        await after_close()[call](h.client, other)
    assert isinstance(raised.value, NoSuchCursor), "code that catches NoSuchCursor still works"
    await h.close()


@scenario
async def test_the_name_of_a_closed_cursor_can_move_or_go_but_not_be_put_on_it(
    tmp_path: Path,
) -> None:
    h, other = await with_other(tmp_path)
    await h.client.close_cursor(other)
    await h.client.set_label(h.cursor, "scout")  # the restart: the name moves
    assert h.session.label("scout") == h.cursor
    await h.client.set_label(h.cursor, "main")
    await h.client.remove_label("scout")
    with pytest.raises(CursorClosed):
        await h.client.set_label(other, "main")
    await h.close()


class Probe:
    """Commands that try a tree handle's calls on a cursor and keep what each raised."""

    name = "probe"

    def __init__(self) -> None:
        self.raised: dict[str, type[BaseException]] = {}
        self.commands = (self.try_all,)

    async def try_all(self, tree: TreeHandle, cursor: int) -> None:
        """Try each call on the cursor."""
        calls: dict[str, Callable[[], Awaitable[object] | object]] = {
            "send": lambda: tree.send(cursor, "x"),
            "add_item": lambda: tree.add_item(cursor, "x"),
            "set_label": lambda: tree.set_label("x", cursor),
            "abort": lambda: tree.abort(cursor),
            "rest": lambda: tree.rest(cursor),
            "close_cursor": lambda: tree.close_cursor(cursor),
            "cursor": lambda: tree.cursor(cursor),
        }
        for name, call in calls.items():
            try:
                result = call()
                if isinstance(result, Awaitable):
                    await result  # pyright: ignore[reportUnknownVariableType]
            except Exception as e:
                self.raised[name] = type(e)


@scenario
async def test_every_handle_call_on_a_closed_cursor_raises_cursor_closed(tmp_path: Path) -> None:
    probe = Probe()
    h, other = await with_other(tmp_path, extensions=[probe])
    await h.client.close_cursor(other)
    await h.run("try_all", {"cursor": other})
    assert probe.raised == dict.fromkeys(
        ["send", "add_item", "set_label", "abort", "rest", "close_cursor", "cursor"], CursorClosed
    )
    await h.close()


# What clients are sent


@scenario
async def test_a_client_connecting_after_a_close_is_told_the_cursor_is_gone_before_the_labels(
    tmp_path: Path,
) -> None:
    h, other = await with_other(tmp_path)
    at = h.session.cursor(other).at
    await h.client.close_cursor(other)
    late = await h.connect("late")
    events = [await asyncio.wait_for(anext(late), 1) for _ in range(3)]
    assert isinstance(events[0], CursorView)
    assert events[0].cursor == h.cursor, "the open cursor's view"
    assert events[1:] == [CursorGone(other, at), Labels((("scout", other),))]
    await late.close()
    await h.close()


@scenario
async def test_a_gone_event_replaces_the_cursors_view_in_a_buffer_nobody_reads(
    tmp_path: Path,
) -> None:
    h, other = await with_other(tmp_path)
    at = h.session.cursor(other).at
    slow = await h.connect("slow")  # its snapshot holds the view of `other`
    await h.client.close_cursor(other)
    events = [await asyncio.wait_for(anext(slow), 1) for _ in range(3)]
    assert [type(e) for e in events] == [CursorView, Labels, CursorGone]
    assert events[2] == CursorGone(other, at)
    assert all(not (isinstance(e, CursorView) and e.cursor == other) for e in events)
    await slow.close()
    await h.close()


@scenario
async def test_a_reloaded_session_keeps_the_closed_cursor_closed(tmp_path: Path) -> None:
    h, other = await with_other(tmp_path, provider=FakeProvider([say("hi")]))
    await h.send("hello", cursor=other)
    await h.settle()
    at = h.session.cursor(other).at
    await h.client.close_cursor(other)
    await h.crash()
    await h.settle()
    assert h.session.cursors() == [h.cursor], "not woken, not listed"
    assert h.session.closed() == [(other, at)]
    assert h.session.label("scout") == other
    assert await gone(h) == [CursorGone(other, at)], "the snapshot says so"
    await h.close()


# Reopening


@scenario
async def test_reopen_starts_a_new_cursor_at_the_last_node_and_moves_the_labels(
    tmp_path: Path,
) -> None:
    h, other = await with_other(tmp_path, provider=FakeProvider([say("hi")]))
    await h.client.set_label(other, "again")
    await h.send("hello", cursor=other)
    await h.settle()
    at = h.session.cursor(other).at
    await h.client.close_cursor(other)
    new = await h.client.reopen(other)
    assert new == other + 1
    assert h.session.cursor(new).at == at
    assert h.session.label("scout") == new
    assert h.session.label("again") == new
    assert h.session.closed() == [(other, at)], "the old id stays closed"
    with pytest.raises(NoSuchCursor, match="not closed"):
        await h.client.reopen(new)
    with pytest.raises(NoSuchCursor, match="not closed"):
        await h.client.reopen(99)
    await h.close()


class Foreman:
    """Tools that close a cursor, reopen it, and try to close their own."""

    name = "foreman"

    def __init__(self) -> None:
        self.reopened: list[int] = []
        self.refused: list[str] = []
        self.tools = (self.dismiss, self.rehire, self.quit)

    async def dismiss(self, node: NodeHandle) -> str:
        """Close cursor 1."""
        await node.close_cursor(1)
        return "dismissed"

    def rehire(self, node: NodeHandle) -> str:
        """Reopen cursor 1."""
        self.reopened.append(node.reopen(1))
        return "rehired"

    async def quit(self, node: NodeHandle) -> str:
        """Close the cursor this call is a step of."""
        try:
            await node.close_cursor(node.cursor_id)
        except ValueError as e:
            self.refused.append(str(e))
        return "tried"


@scenario
async def test_a_tool_closes_a_cursor_it_waited_on_and_reopens_it(tmp_path: Path) -> None:
    foreman = Foreman()
    gate, reply = held()
    h, other = await with_other(
        tmp_path,
        provider=FakeProvider([reply, calls("dismiss"), calls("rehire"), calls("quit"), say("ok")]),
        extensions=[foreman],
        gates=[gate],
    )
    await h.send("first", cursor=other)
    await h.settle()
    await h.send("go")  # the tool closes the cursor whose sample is held at its gate
    await h.settle()
    assert h.session.closed()[0][0] == other
    results = [b for b in h.bodies() if isinstance(b, Body.Results)]
    assert [r.outcomes[0][1] for r in results] == [
        Outcome.Ok("dismissed"),
        Outcome.Ok("rehired"),
        Outcome.Ok("tried"),
    ]
    assert foreman.reopened == [other + 1]
    assert h.session.label("scout") == other + 1, "reopen moved the label"
    assert len(foreman.refused) == 1
    assert f"cursor {h.cursor} is this handle's own" in foreman.refused[0]
    assert h.session.cursors() == [h.cursor, other + 1], "nothing of its own was closed"
    await h.close()


# A fan of effects waiting on a cursor, counted for `settle`


class Both:
    """A tool that waits on cursor 1 and asks a question at the same time."""

    name = "both"

    def __init__(self) -> None:
        self.answers: list[object] = []
        self.tools = (self.wait_and_ask,)

    async def wait_and_ask(self, node: NodeHandle) -> str:
        """Wait for cursor 1 to rest and ask, at once."""
        _, answer = await asyncio.gather(node.rest(1), node.ask("go on?", bool))
        self.answers.append(answer)
        return "both"


@scenario
async def test_an_effect_with_a_rest_and_a_question_out_is_one_parked_effect(
    tmp_path: Path,
) -> None:
    """`settle` compares effects with parked effects: one with two waits out counts once."""
    both = Both()
    gate, reply = held()
    h, other = await with_other(
        tmp_path,
        provider=FakeProvider([reply, calls("wait_and_ask"), say("done")]),
        extensions=[both],
        gates=[gate],
    )
    await h.send("first", cursor=other)
    await h.settle()
    await h.send("go")
    await h.settle()  # a sample at its gate, and the tool: asking and waiting
    state = h.driver.state()
    assert (state.units, state.open_questions, state.parked) == (2, 1, 1), state
    assert h.parked() == state.units, "the gate's sample and the tool: two effects, two parked"
    await h.respond(True)
    await h.abort(other)
    await h.settle()
    assert both.answers == [True]
    await h.close()


# The loop thread


class Stray:
    """A sync tool that waits to be told, then aborts cursor 1 from its worker thread."""

    name = "stray"

    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started, self.release = started, release
        self.returned = threading.Event()
        self.raised: list[BaseException] = []
        self.tools = (self.cut,)

    def cut(self, node: NodeHandle) -> str:
        """Abort cursor 1, once released."""
        self.started.set()
        self.release.wait()
        try:
            node.abort(1)
        except BaseException as e:
            self.raised.append(e)
        self.returned.set()
        return "cut"


def test_a_worker_thread_abort_fails_when_the_event_loop_is_not_running(tmp_path: Path) -> None:
    """The abort hops onto the loop and waits for it: with no running loop to do it, it raises
    instead of waiting for ever."""
    started, release = threading.Event(), threading.Event()
    stray = Stray(started, release)

    async def start() -> Harness:
        h = await Harness.create(
            tmp_path, provider=FakeProvider([calls("cut")]), extensions=[stray]
        )
        await h.client.open(SYS)
        await h.send("go")
        while not started.is_set():
            await asyncio.sleep(0)
        return h  # the loop stops here, with the call out and the driver running

    loop = asyncio.new_event_loop()
    h = loop.run_until_complete(start())
    release.set()
    assert stray.returned.wait(10), "it waited for a loop that will never run it"
    assert len(stray.raised) == 1
    assert isinstance(stray.raised[0], RuntimeError)
    assert "event loop is not running" in str(stray.raised[0])
    loop.run_until_complete(h.close())
    loop.close()
