"""Clients (D16 sections 3 and 5): the snapshot on connect, the order of events, watching, bounds,
questions between clients, notices, and a failed hook seen by a client that came late."""

import asyncio
import gc
from pathlib import Path
from typing import Literal

import pytest
from fakes import FakeProvider, FakeTool, Gate, Recorder, Reply
from harness import Harness, scenario

from tau import (
    Appended,
    CallArgs,
    CallNamed,
    Client,
    Cursor,
    CursorView,
    CutOff,
    Driver,
    Ending,
    Event,
    HookFailure,
    Labels,
    Live,
    LocalClient,
    Notice,
    Part,
    Stop,
    Subject,
    TextDelta,
    ThinkingDelta,
)

SYS = 1  # the system node's id: a new session is Config 0, System 1


async def _read_all(client: Client) -> list[Event]:
    return [event async for event in client]


async def first(client: Client, count: int) -> list[Event]:
    return [await asyncio.wait_for(anext(client), 1) for _ in range(count)]


# The snapshot on connect


@scenario
async def test_a_client_connecting_mid_sample_is_told_where_everything_stands(
    tmp_path: Path,
) -> None:
    gate = Gate()
    reply = Reply([Part.Text("hi")], gate=gate, gate_after=1)
    h = await Harness.create(tmp_path, provider=FakeProvider([reply]))
    other = await h.client.open(SYS)
    await h.client.set_label(h.cursor, "main")
    await h.set_auto(False, other)
    await h.send("go")
    await h.settle()  # the sample is in flight
    late = await h.connect("late")
    *views, labels = await first(late, 3)
    assert labels == Labels((("main", h.cursor),)), "after the cursors it names"
    assert [v.cursor for v in views if isinstance(v, CursorView)] == [h.cursor, other]
    mine = views[0]
    assert isinstance(mine, CursorView)
    assert (mine.busy, mine.auto, mine.at) == (True, True, 2)
    assert mine.items == (), "the input was taken by the step"
    await late.close()
    gate.release()
    await h.close()


@scenario
async def test_nodes_are_appended_before_the_state_that_points_at_them(tmp_path: Path) -> None:
    call = Reply([Part.Call("a", "ta", "{}")], Stop.Tools)
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([call, Reply([Part.Text("done")])]),
        tools=[FakeTool("ta", "ran")],
    )
    known = len(h.nodes())  # the nodes the client reads by call: the snapshot holds none
    client = await h.connect("watcher")
    await h.send("go")
    await h.settle()
    total = len(h.nodes())
    (events,) = await _drain(h, client)
    for event in events:
        if isinstance(event, Appended):
            assert event.nodes[0].id == known, "in order, with no gap"
            known += len(event.nodes)
        elif isinstance(event, CursorView):
            assert event.at < known, f"{event} points at a node the client was not sent"
    assert known == total


async def _drain(h: Harness, *clients: Client) -> list[list[Event]]:
    await h.close()
    return [[event async for event in client] for client in clients]


@scenario
async def test_a_state_that_replaces_an_earlier_copy_goes_to_the_end(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply([Part.Text("a")])]))
    other = await h.client.open(SYS)
    await h.set_auto(False, other)
    client = await h.connect("unread")
    await h.send("go")
    await h.settle()
    await h.set_auto(True, other)  # this cursor's state goes after the other's latest
    (events,) = await _drain(h, client)
    views = [e for e in events if isinstance(e, CursorView)]
    assert sorted(v.cursor for v in views) == [h.cursor, other], "one copy of each, no more"
    assert views[-1] == await _view_after(events, other)
    assert [type(e) for e in events].count(Labels) == 1


async def _view_after(events: list[Event], cursor: int) -> CursorView:
    return next(e for e in reversed(events) if isinstance(e, CursorView) and e.cursor == cursor)


# Watching


@scenario
async def test_watching_catches_up_on_the_reply_in_flight(tmp_path: Path) -> None:
    gate = Gate()
    script = [
        ThinkingDelta("h"),
        ThinkingDelta("m"),
        Part.Thinking("hm"),
        TextDelta("hel"),
        TextDelta("lo"),
        Part.Text("hello"),
        CallNamed(0, "c1", "t"),
        CallArgs(0, '{"a'),
    ]
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply(script, gate=gate, gate_after=len(script))])
    )
    await h.send("go")
    await h.settle()
    late = await h.connect("late")
    await first(late, 2)
    await late.watch([h.cursor])
    caught = await first(late, 4)
    assert caught == [
        Live(h.cursor, ThinkingDelta("hm")),
        Live(h.cursor, TextDelta("hello")),
        Live(h.cursor, CallNamed(0, "c1", "t")),
        Live(h.cursor, CallArgs(0, '{"a')),
    ], "the parts not in the tree yet, then the call still forming"
    await late.watch([h.cursor])  # already watching: nothing to catch up on
    await late.watch([])
    await late.watch([h.cursor])  # watching again after stopping: caught up again
    assert len(await first(late, 4)) == 4
    await late.close()
    await h.close()


@scenario
async def test_watching_a_cursor_that_is_not_open_is_refused(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    with pytest.raises(LookupError):
        await h.client.watch([7])
    await h.close()


# Bounds


@scenario
async def test_a_client_that_falls_behind_is_cut_off_and_reconnects_to_a_consistent_snapshot(
    tmp_path: Path,
) -> None:
    replies = [Reply([Part.Text(f"r{i}")]) for i in range(4)]
    h = await Harness.create(tmp_path, provider=FakeProvider(replies))
    slow = await h.connect("slow", buffer=2)
    for i in range(4):
        await h.send(f"m{i}")
        await h.settle()  # each is an Appended nobody reads
    with pytest.raises(ConnectionError) as cut:
        await asyncio.wait_for(_read_all(slow), 1)
    assert isinstance(cut.value, CutOff)
    again = await h.connect("slow", buffer=2)
    view, _labels = await first(again, 2)
    assert isinstance(view, CursorView)
    assert view.at == len(h.nodes()) - 1
    assert not view.busy
    await again.close()
    await h.close()


@scenario
async def test_a_client_cut_off_is_no_longer_connected(tmp_path: Path) -> None:
    asks = Asks("still there?", to="slow")
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("ok")])] * 4), extensions=[asks]
    )
    await h.connect("slow", buffer=1)
    asks.skip = True
    for i in range(3):
        await h.send(f"m{i}")
        await h.settle()
    asks.skip = False
    await h.send("last")
    await h.settle()
    assert [q.to for q in (await h.view()).questions] == [None], (
        "the stream was cut off: the question is for everyone"
    )
    await h.respond("yes")
    await h.settle()
    assert asks.answers == ["yes"]
    await h.close()


@scenario
async def test_a_question_open_when_its_client_is_cut_off_is_open_to_everyone(
    tmp_path: Path,
) -> None:
    asks = Asks("still there?", to="slow")
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("ok")])] * 4), extensions=[asks]
    )
    slow = await h.connect("slow", buffer=1)
    await h.send("hi")
    await h.settle()
    assert [q.to for q in (await h.view()).questions] == ["slow"]
    asks.skip = True
    other = await h.client.open(SYS)
    for i in range(3):  # more `Appended` than a buffer of one holds
        await h.send(f"m{i}", cursor=other)
        await h.settle()
    assert [q.to for q in (await h.view()).questions] == [None]
    with pytest.raises(ConnectionError):
        await first(slow, 5)
    await h.close()


# Questions between clients


class Asks:
    """A `pre_step` hook that asks `text` of `to` and records what came back."""

    name = "asks"

    def __init__(self, text: str, to: str | None = None) -> None:
        self.text, self.to = text, to
        self.skip = False
        self.answers: list[object] = []

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        if not self.skip:
            self.answers.append(await cursor.ask(self.text, Literal["yes", "no"], to=self.to))
        return None


async def asked(tmp_path: Path, asks: Asks, *clients: str) -> tuple[Harness, list[LocalClient]]:
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("ok")])] * 2), extensions=[asks]
    )
    connected = [await h.connect(c) for c in clients]
    await h.send("hi")
    await h.settle()
    return h, connected


@scenario
async def test_the_first_good_answer_wins_and_a_later_one_is_too_late(tmp_path: Path) -> None:
    asks = Asks("go on?")
    h, (alice, bob) = await asked(tmp_path, asks, "alice", "bob")
    (question,) = (await h.view()).questions
    assert (question.to, question.text) == (None, "go on?")
    assert question.schema == {"type": "string", "enum": ["yes", "no"]}
    with pytest.raises(ValueError, match="must be one of"):
        await bob.answer(question.id, "maybe")
    assert (await h.view()).questions == (question,), "a wrong answer leaves it open"
    await alice.answer(question.id, "yes")
    with pytest.raises(LookupError):
        await bob.answer(question.id, "no")
    await h.settle()
    assert asks.answers == ["yes"]
    with pytest.raises(LookupError):
        await alice.answer(question.id, "yes")
    await h.close()


@scenario
async def test_every_client_is_told_of_a_question_and_of_its_close(tmp_path: Path) -> None:
    asks = Asks("go on?", to="alice")
    h, (alice, bob) = await asked(tmp_path, asks, "alice", "bob")
    for client in (alice, bob):
        views = [e for e in await first(client, 2) if isinstance(e, CursorView)]
        assert [q.text for q in views[0].questions] == ["go on?"]
    (question,) = (await h.view()).questions
    await alice.answer(question.id, "no")
    for client in (alice, bob):
        closed = await _next_view(client)
        assert closed.questions == ()
    await h.close()


async def _next_view(client: Client) -> CursorView:
    async with asyncio.timeout(1):
        async for event in client:
            if isinstance(event, CursorView):
                return event
    raise AssertionError("the stream ended")


@scenario
async def test_a_question_for_one_client_cannot_be_answered_by_another(tmp_path: Path) -> None:
    asks = Asks("just you", to="alice")
    h, (alice, bob) = await asked(tmp_path, asks, "alice", "bob")
    (question,) = (await h.view()).questions
    with pytest.raises(PermissionError, match="alice"):
        await bob.answer(question.id, "yes")
    await alice.answer(question.id, "yes")
    await h.settle()
    assert asks.answers == ["yes"]
    await h.close()


@scenario
async def test_a_question_for_a_client_that_disconnects_is_open_to_everyone(
    tmp_path: Path,
) -> None:
    asks = Asks("still yours?", to="alice")
    h, (alice, bob) = await asked(tmp_path, asks, "alice", "bob")
    (question,) = (await h.view()).questions
    assert question.to == "alice"
    await first(bob, 2)  # the snapshot and the question's view
    await alice.close()
    pooled = await _next_view(bob)
    assert [(q.id, q.to) for q in pooled.questions] == [(question.id, None)], "bob may answer"
    assert asks.answers == [], "a disconnect declines nothing"
    again = await h.connect("alice")
    seen = [e for e in await first(again, 2) if isinstance(e, CursorView)]
    assert [(q.id, q.to) for q in seen[0].questions] == [(question.id, None)], (
        "it stays in the pool when its client returns"
    )
    await bob.answer(question.id, "yes")
    await h.settle()
    assert asks.answers == ["yes"]
    await h.close()


@scenario
async def test_asking_a_client_that_is_not_connected_asks_everyone(tmp_path: Path) -> None:
    asks = Asks("anyone?", to="alice")
    h, () = await asked(tmp_path, asks)
    assert asks.answers == [], "the cursor waits"
    (question,) = (await h.view()).questions
    assert question.to is None, "in the pool at once"
    assert (await h.view()).busy
    await h.respond("yes")
    await h.settle()
    assert asks.answers == ["yes"]
    await h.close()


@scenario
async def test_an_abandoned_client_is_no_longer_connected(tmp_path: Path) -> None:
    asks = Asks("anyone?", to="alice")
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("ok")])] * 2), extensions=[asks]
    )
    alice = await h.connect("alice")
    await h.send("one")
    await h.settle()
    (question,) = (await h.view()).questions
    assert question.to == "alice", "alice is connected"
    del alice  # never closed
    gc.collect()
    await h.send("two")  # the driver finds her gone
    await h.settle()
    assert [q.to for q in (await h.view()).questions] == [None], "and her question is open to all"
    await h.respond("yes")
    await h.settle()
    assert asks.answers == ["yes"]
    await h.close()


@scenario
async def test_a_question_for_an_abandoned_client_is_answered_before_anything_syncs(
    tmp_path: Path,
) -> None:
    asks = Asks("anyone?", to="alice")
    h, (alice, bob) = await asked(tmp_path, asks, "alice", "bob")
    (question,) = (await h.view()).questions
    assert question.to == "alice"
    del alice  # never closed; the cursor is idle but for this question, so nothing syncs
    gc.collect()
    await bob.answer(question.id, "yes")
    await h.settle()
    assert asks.answers == ["yes"]
    await h.close()


@scenario
async def test_abort_declines_the_questions_and_a_late_answer_is_too_late(tmp_path: Path) -> None:
    asks = Asks("go on?")
    h, (alice,) = await asked(tmp_path, asks, "alice")
    (question,) = (await h.view()).questions
    await alice.abort(h.cursor)
    await h.settle()
    assert asks.answers == [None]
    with pytest.raises(LookupError):
        await alice.answer(question.id, "yes")
    await h.close()


# A failed hook, and how a step ended


class Breaks:
    """An extension named `breaks` whose `pre_step` and `post_step` raise when told to."""

    name = "breaks"

    def __init__(self, *, pre: bool = False, post: bool = False) -> None:
        self.pre, self.post = pre, post

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        if self.pre:
            raise KeyError("x")
        return None

    async def post_step(self, cursor: Cursor, subject: Subject) -> str | None:
        if self.post:
            raise ValueError("late")
        return None


@scenario
async def test_a_failed_hook_is_in_the_state_a_client_connecting_later_is_told(
    tmp_path: Path,
) -> None:
    recorder = Recorder()
    h = await Harness.create(tmp_path, extensions=[Breaks(pre=True), recorder])
    await h.step()
    await h.settle()
    view = await h.view()
    assert view.hook_error == HookFailure("breaks", "pre_step", "KeyError: 'x'")
    assert view.ending == Ending.Failed("KeyError: 'x'")
    assert recorder.endings == [view.ending]
    await h.close()


@scenario
async def test_a_failed_post_step_hook_is_in_the_state_too(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Breaks(post=True)])
    await h.step()
    await h.settle()
    view = await h.view()
    assert view.hook_error == HookFailure("breaks", "post_step", "ValueError: late")
    assert view.ending == Ending.Done()
    await h.close()


@scenario
async def test_a_failed_hook_is_in_the_cursor_that_failed_only_and_ends_with_its_next_step(
    tmp_path: Path,
) -> None:
    breaks = Breaks(pre=True)
    h = await Harness.create(tmp_path, extensions=[breaks])
    other = await h.client.open(SYS)
    await h.settle()
    assert (await h.view(other)).hook_error is not None
    breaks.pre = False
    await h.step()
    await h.settle()
    assert (await h.view(h.cursor)).hook_error is None, "this cursor's step passed"
    assert (await h.view(other)).hook_error is not None, "the other cursor's did not run again"
    await h.step(other)
    await h.settle()
    assert (await h.view(other)).hook_error is None
    await h.close()


@scenario
async def test_a_failed_hook_writes_nothing_to_the_session(tmp_path: Path) -> None:
    passing = await Harness.create(tmp_path / "ok", extensions=[Breaks()])
    await passing.step()
    await passing.settle()
    failing = await Harness.create(tmp_path / "bad", extensions=[Breaks(pre=True)])
    await failing.step()
    await failing.settle()
    assert len(failing.nodes()) == len(passing.nodes())
    await passing.close()
    await failing.close()


@scenario
async def test_a_step_clears_how_the_last_one_ended_and_a_reload_does_not_restore_it(
    tmp_path: Path,
) -> None:
    gate = Gate()
    reply = Reply([Part.Text("hi")], gate=gate, gate_after=1)
    h = await Harness.create(tmp_path, provider=FakeProvider([reply]))
    await h.step()
    await h.settle()
    assert (await h.view()).ending == Ending.Done()
    await h.send("go")
    await h.settle()
    running = await h.view()
    assert (running.busy, running.ending) == (True, None)
    gate.release()
    await h.settle()
    assert (await h.view()).ending == Ending.Done()
    await h.close()
    again = await Harness.load(tmp_path)
    assert (await again.view()).hook_error is None
    await again.close()


# Notices


class Refuses:
    name = "refuses"

    async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
        return "no commands here"


@scenario
async def test_a_refused_input_is_a_notice_to_the_client_that_typed_it_alone(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Refuses()])
    bystander = await h.connect("bystander")
    await first(bystander, 2)
    await h.send("/oops")
    await h.settle()
    assert h.notices() == ["no commands here"]
    await bystander.close()
    heard = [e async for e in bystander]
    assert not [e for e in heard if isinstance(e, Notice)]
    await h.close()


class Tells:
    name = "tells"

    def __init__(self, to: str, text: str) -> None:
        self.to, self.text = to, text

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        cursor.tell(self.text, to=self.to)
        return None


@scenario
async def test_a_hook_tells_one_client(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Tells("alice", "build done")])
    alice = await h.connect("alice")
    bob = await h.connect("bob")
    await h.step()
    await h.settle()
    async with asyncio.timeout(1):
        notice = await anext(e async for e in alice if isinstance(e, Notice))
    assert notice == Notice("build done")
    (events,) = await _drain(h, bob)
    assert not [e for e in events if isinstance(e, Notice)]


@scenario
async def test_a_notice_for_a_client_that_is_not_connected_is_dropped(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Tells("alice", "build done")])
    await h.step()
    await h.settle()
    alice = await h.connect("alice")
    snapshot = await first(alice, 2)
    assert not [e for e in snapshot if isinstance(e, Notice)]
    await h.close()


@scenario
async def test_watching_catches_up_on_calls_under_the_providers_own_index(tmp_path: Path) -> None:
    gate = Gate()
    script = [
        TextDelta("t"),
        Part.Text("t"),
        CallNamed(1, "c1", "tool"),  # the provider numbers its blocks: the call is 1
        CallArgs(1, "{}"),
        Part.Call("c1", "tool", "{}"),
        CallNamed(2, "c2", "other"),
        CallArgs(2, '{"a'),
    ]
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply(script, gate=gate, gate_after=len(script))])
    )
    await h.send("go")
    await h.settle()
    late = await h.connect("late")
    await first(late, 2)
    await late.watch([h.cursor])
    caught = await first(late, 5)
    assert caught == [
        Live(h.cursor, TextDelta("t")),
        Live(h.cursor, CallNamed(1, "c1", "tool")),
        Live(h.cursor, CallArgs(1, "{}")),
        Live(h.cursor, CallNamed(2, "c2", "other")),
        Live(h.cursor, CallArgs(2, '{"a')),
    ]
    await late.close()
    await h.close()


@scenario
async def test_every_call_raises_the_same_way_once_the_driver_has_stopped(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    c, at = h.client, h.cursor
    await h.driver.stop()
    calls = [
        c.input(at, "x"),
        c.send(at, "x"),
        c.step(at),
        c.go_to(at, 1),
        c.set_auto(at, True),
        c.abort(at),
        c.open(1),
        c.set_label(at, "l"),
        c.remove_label("l"),
        c.watch([at]),
        c.answer(0, "x"),
        c.commands(),
        c.run(at, "nosuch", {}),
        c.options(at, "nosuch"),
        c.configure(at, "nosuch", {}),
        c.complete(at, "nope", ""),
        c.node(99),
        c.walk(99),
        c.children(99),
    ]
    for call in calls:
        with pytest.raises(RuntimeError, match="stopped"):
            await call
    h.session.close()


def test_the_drivers_public_surface_is_small() -> None:
    public = {name for name in dir(Driver) if not name.startswith("_")}
    assert public == {
        "start",
        "stop",
        "rest",
        "use_catalog",
        "reload",
        "state",
        "client",
        "add_item",
    }, "a frontend holds a Client; the driver's other calls are the Client's"
