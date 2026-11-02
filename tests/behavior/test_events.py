"""What a client hears (D15, D16): live deltas, state sent whole, and what becomes of its stream
when the driver fails or stops."""

import asyncio
import json
import math
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from itertools import pairwise
from pathlib import Path

import pytest
from fakes import Fail, FakeProvider, FakeTool, Gate, Recorder, Reply
from harness import Harness, scenario

from tau import (
    Appended,
    Author,
    Body,
    CallArgs,
    CallNamed,
    Client,
    Closed,
    CursorView,
    Delta,
    Ending,
    Event,
    Finish,
    ItemData,
    Labels,
    Live,
    ModelConfig,
    Node,
    Outcome,
    Part,
    SampleError,
    Stop,
    TextDelta,
    ThinkingDelta,
    Tool,
)

SYS = 1  # the system node's id: a new session is Config 0, System 1
DELTAS = [ThinkingDelta("hm"), TextDelta("hel"), TextDelta("lo")]
# A part is yielded before the first delta of the next part.
SCRIPT = [DELTAS[0], Part.Thinking("hm"), DELTAS[1], DELTAS[2], Part.Text("hello")]


async def failing_harness(tmp_path: Path) -> tuple[Harness, Gate]:
    """A harness with one call parked at the returned gate. Close the session, then release
    the gate: the call's answer meets a closed session, which the pump dies of."""
    gate = Gate()
    reply = Reply([Part.Call("a", "wait", "{}")], Stop.Tools)
    h = await Harness.create(
        tmp_path, provider=FakeProvider([reply]), tools=[FakeTool("wait", "ok", gate)]
    )
    return h, gate


def held(gate: Gate) -> Reply:
    """A reply that streams SCRIPT and waits at `gate` before its `Finish`."""
    return Reply(SCRIPT, gate=gate, gate_after=len(SCRIPT))


async def drain(h: Harness, *clients: Client) -> list[list[Event]]:
    """Everything each client will hear: the driver stops, which ends the streams."""
    await h.close()
    return [[event async for event in client] for client in clients]


async def read_all(client: Client) -> list[Event]:
    return [event async for event in client]


async def snapshot(client: Client, cursors: int = 1) -> list[Event]:
    """The snapshot a client opens with: a state per cursor, then `Labels`."""
    return [await asyncio.wait_for(anext(client), 1) for _ in range(1 + cursors)]


async def next_view(client: Client, cursor: int) -> CursorView:
    """The next state of `cursor` the client is sent."""
    async with asyncio.timeout(1):
        async for event in client:
            if isinstance(event, CursorView) and event.cursor == cursor:
                return event
    raise AssertionError("the stream ended")


def lives(events: list[Event]) -> list[Live]:
    return [e for e in events if isinstance(e, Live)]


def appended(events: list[Event]) -> list[Node]:
    return [n for e in events if isinstance(e, Appended) for n in e.nodes]


@scenario
async def test_live_deltas_are_heard_as_they_arrive_and_the_reply_is_appended_after(
    tmp_path: Path,
) -> None:
    gate = Gate()
    h = await Harness.create(tmp_path, provider=FakeProvider([held(gate)]))
    client = await h.connect("watcher", watching=[h.cursor])
    await h.send("hi")
    await h.settle()  # parked at the gate with every delta streamed
    arrived: list[Event] = []
    while len(lives(arrived)) < len(DELTAS):
        arrived.append(await asyncio.wait_for(anext(client), 1))
    assert [e.delta for e in lives(arrived)] == DELTAS
    gate.release()
    await h.settle()
    (rest,) = await drain(h, client)
    reply = appended(rest)[-1]
    assert isinstance(reply.body, Body.Assistant)
    assert [type(e) for e in rest[-2:]] == [Appended, CursorView], "the state follows its nodes"
    assert isinstance(rest[-1], CursorView)
    assert rest[-1].at == reply.id


@scenario
async def test_a_client_hears_the_live_deltas_of_the_cursors_it_watches_only(
    tmp_path: Path,
) -> None:
    gate = Gate()
    h = await Harness.create(tmp_path, provider=FakeProvider([held(gate)]))
    other = await h.client.open(SYS)
    mine = await h.connect("mine", watching=[h.cursor])
    theirs = await h.connect("theirs", watching=[other])
    neither = await h.connect("neither")
    both = await h.connect("both", watching=[h.cursor, other])
    await h.send("hi")
    await h.settle()
    mine_seen, theirs_seen, neither_seen, both_seen = await drain(h, mine, theirs, neither, both)
    assert [e.delta for e in lives(mine_seen)] == DELTAS
    assert lives(both_seen) == lives(mine_seen)
    assert not lives(theirs_seen), "the other cursor sampled nothing"
    assert not lives(neither_seen)
    for seen in (mine_seen, theirs_seen, neither_seen):
        assert {e.cursor for e in seen if isinstance(e, CursorView)} == {h.cursor, other}, (
            "every client hears every cursor's state"
        )


@scenario
async def test_a_slow_client_loses_live_events_and_never_slows_the_pump(tmp_path: Path) -> None:
    gate = Gate()
    calls = [CallNamed(0, "c1", "read"), CallArgs(0, '{"p'), CallArgs(0, 'ath"}')]
    batch = Reply(
        [*SCRIPT, *calls, Part.Call("c1", "read", '{"path"}')],
        stop=Stop.Tools,
        gate=gate,
        gate_after=len(SCRIPT) + 4,
    )
    h = await Harness.create(tmp_path, provider=FakeProvider([batch, Reply([Part.Text("done")])]))
    slow = await h.connect("slow", buffer=2, watching=[h.cursor])
    fast = await h.connect("fast", watching=[h.cursor])
    await h.send("hi")
    await h.settle()  # parked at the gate: the pump ran with nobody reading
    slow_seen, fast_seen = await drain(h, slow, fast)
    assert len(lives(fast_seen)) == len(DELTAS) + len(calls)
    assert len(lives(slow_seen)) == 2, "the buffer held its capacity and no more"


@scenario
async def test_a_failed_sample_is_appended_and_the_state_says_how_it_ended(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Fail("boom", [DELTAS[1]])]))
    client = await h.connect("watcher")
    await h.send("hi")
    await h.settle()
    (events,) = await drain(h, client)
    reply = appended(events)[-1]
    assert isinstance(reply.body, Body.Assistant)
    assert reply.body.stop == Stop.Error
    last = [e for e in events if isinstance(e, CursorView)][-1]
    assert (last.at, last.busy) == (reply.id, False)
    assert isinstance(last.ending, Ending.Failed)


@scenario
async def test_a_call_that_changes_a_cursor_is_heard_by_another_client(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    client = await h.connect("watcher")
    await snapshot(client)
    await h.input("hello")
    view = await asyncio.wait_for(anext(client), 1)
    assert isinstance(view, CursorView)
    assert [i.data for i in view.items] == [ItemData.Input(Author.Frontend("term"), "hello")]
    opened = await h.client.open(SYS)
    assert (await next_view(client, opened)).at == SYS
    await h.client.go_to(opened, 0)
    while (view := await next_view(client, opened)).at != 0:  # the busy step is aborted first
        assert view.busy
    await client.close()
    await h.close()


@scenario
async def test_a_call_that_changes_nothing_is_not_announced(tmp_path: Path) -> None:
    """An abort on a resting cursor, a `set_auto` to the value it has: nothing a client could
    see changed, so nothing is sent."""
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    client = await h.connect("watcher")
    await snapshot(client)
    waiting = asyncio.ensure_future(anext(client))
    assert not await h.abort()
    await h.set_auto(False)
    await h.settle()
    assert not waiting.done()
    await h.set_auto(True)  # this does change the cursor
    event = await waiting
    assert isinstance(event, CursorView)
    assert event.auto
    await client.close()
    await h.close()


@scenario
async def test_closing_a_client_ends_its_stream_and_closing_twice_is_fine(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply(SCRIPT)]))
    async with await h.connect("one") as one:
        assert one.id == "one"
    client = await h.connect("two")
    await client.close()
    await client.close()
    assert [e async for e in client] == []
    await h.send("hi")
    await h.settle()
    await h.close()


@scenario
async def test_a_second_consumer_of_one_stream_is_refused(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    client = await h.connect("watcher")
    await snapshot(client)
    first = asyncio.ensure_future(anext(client))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="one consumer"):
        await anext(client)
    await h.input("x")
    assert isinstance(await first, CursorView)
    await h.close()


@scenario
async def test_a_driver_that_fails_ends_every_stream_with_the_failure(tmp_path: Path) -> None:
    h, gate = await failing_harness(tmp_path)
    client = await h.connect("watcher")
    await h.send("hi")
    await h.settle()
    h.session.close()  # the next answer meets a closed session: the pump dies of it
    gate.release()
    heard: list[Event] = []

    async def read() -> None:
        async for event in client:
            heard.append(event)

    with pytest.raises(ConnectionError, match=r"^Closed: the session is closed$") as ended:
        await asyncio.wait_for(read(), 1)  # buffered events are delivered first, then the failure
    if not h.remote:  # a wire carries "Type: msg", not the exception
        assert isinstance(ended.value.__cause__, Closed), "the failure, chained"
    assert heard, "what was buffered came first"
    await h.driver.stop()


@scenario
async def test_stopping_a_failed_driver_does_not_turn_the_failure_into_a_clean_end(
    tmp_path: Path,
) -> None:
    h, gate = await failing_harness(tmp_path)
    client = await h.connect("watcher")
    await h.send("hi")
    await h.settle()
    h.session.close()
    gate.release()
    with pytest.raises(ConnectionError, match="Closed"):
        await read_all(client)
    await h.driver.stop()  # the natural cleanup
    with pytest.raises(ConnectionError, match="Closed"):
        await read_all(client)  # the client still learns the driver died


@scenario
async def test_a_driver_failure_cancels_the_effects_still_running(tmp_path: Path) -> None:
    """A sibling call keeps running (and billing) until `stop` unless the failure cancels it."""
    cancelled: list[str] = []
    never = Gate()

    class Slow:
        name = "slow"
        description = "Waits until cancelled."
        parameters: Mapping[str, object] = {"type": "object", "properties": {}}

        async def call(self, args_raw: str) -> Outcome:
            try:
                await never.wait()
            except asyncio.CancelledError:
                cancelled.append("slow")
                raise
            return Outcome.Ok("never")

    gate = Gate()
    reply = Reply([Part.Call("a", "fast", "{}"), Part.Call("b", "slow", "{}")], Stop.Tools)
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([reply]),
        tools=[FakeTool("fast", "ok", gate), Slow()],
        gates=[never],
    )
    client = await h.connect("watcher")
    await h.send("go")
    await h.settle()
    h.session.close()
    gate.release()  # the fast call answers into a closed session
    with pytest.raises(ConnectionError, match="Closed"):
        await read_all(client)
    for _ in range(5):
        await asyncio.sleep(0)
    assert cancelled == ["slow"], "cancelled by the failure, not left for `stop`"
    await h.driver.stop()


@scenario
async def test_text_that_never_became_a_part_is_saved_when_the_sample_fails(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Fail("boom", [*DELTAS[1:]])]))
    await h.send("hi")
    await h.settle()
    reply = next(b for b in h.bodies() if isinstance(b, Body.Assistant))
    assert reply.parts == [Part.Text("hello")]
    await h.close()


@scenario
async def test_a_failed_reply_says_why_after_a_reload(tmp_path: Path) -> None:
    # A Gemini 503 inside the stream, after a thought: the reply holds the thought and the
    # error, so a reader of that one node learns why it failed.
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Fail("boom", [Part.Thinking("hm")], status=503, retry_after=2.5)]),
    )
    await h.send("hi")
    await h.settle()
    await h.crash()
    reply = next(b for b in h.bodies() if isinstance(b, Body.Assistant))
    assert reply.stop == Stop.Error
    assert reply.parts == [Part.Thinking("hm")]
    assert reply.meta is not None
    assert json.loads(reply.meta) == {
        "error": {"message": "boom", "status": 503, "retry_after": 2.5}
    }
    await h.close()


@scenario
async def test_a_failure_before_any_part_stays_a_sample_error_node(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Fail("boom", status=503)]))
    await h.send("hi")
    await h.settle()
    assert not any(isinstance(b, Body.Assistant) for b in h.bodies())
    assert Body.Custom("sample_error", "boom") in [n.body for n in h.nodes()]
    await h.close()


VIOLATIONS = {
    "finish with text still open": [TextDelta("x")],
    "a thinking part after text deltas": [TextDelta("x"), Part.Thinking("x")],
    "a call part after text deltas": [TextDelta("x"), Part.Call("c", "t", "x")],
    "a text part that is not the deltas": [TextDelta("x"), Part.Text("y")],
    "a text delta while thinking": [ThinkingDelta("x"), TextDelta("y")],
    "a call part with other arguments": [
        CallNamed(0, "c", "t"),
        CallArgs(0, "{"),
        Part.Call("c", "t", "{}"),
    ],
    "a call part for another call": [CallNamed(0, "c", "t"), Part.Call("d", "t", "")],
    "a second call before the first part": [CallNamed(0, "c", "t"), CallNamed(1, "d", "t")],
    "call arguments with no call named": [CallArgs(0, "{")],
    "call arguments for another call": [CallNamed(0, "c", "t"), CallArgs(1, "{")],
}


@pytest.mark.parametrize("script", VIOLATIONS.values(), ids=VIOLATIONS.keys())
@scenario
async def test_a_provider_that_breaks_the_ordering_rule_fails_the_sample_and_keeps_the_text(
    tmp_path: Path, script: list[Part | TextDelta]
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply(script)]))
    await h.send("hi")
    await h.settle()
    h.check()
    bodies = [n.body for n in h.nodes()]
    replies = [b for b in bodies if isinstance(b, Body.Assistant)]
    notes = [b for b in bodies if isinstance(b, Body.Custom) and b.kind == "sample_error"]
    assert len(notes) + len([r for r in replies if r.stop == Stop.Error]) == 1, "the sample failed"
    if script[0] == TextDelta("x"):
        assert [r.parts for r in replies] == [[Part.Text("x")]], "the streamed text is kept"
    await h.close()


@scenario
async def test_a_cancelled_sample_keeps_the_text_streamed_so_far(
    tmp_path: Path,
) -> None:
    gate = Gate()
    reply = Reply([TextDelta("x")], gate=gate, gate_after=1)
    h = await Harness.create(tmp_path, provider=FakeProvider([reply]))
    await h.send("hi")
    await h.settle()
    await h.abort()
    await h.settle()
    assert next(b for b in h.bodies() if isinstance(b, Body.Assistant)).parts == [Part.Text("x")]
    await h.close()


# Provider failures end one sample, not the driver


class Rogue(FakeProvider):
    """A provider whose `sample` is `script`, called with the sample's number, and whose `check`
    raises `check_error` if given."""

    def __init__(
        self,
        script: Callable[[int], AsyncGenerator[Part | Finish | Delta, None]],
        check_error: Exception | None = None,
    ) -> None:
        super().__init__([])
        self._rogue = script
        self._check_error = check_error
        self.samples = 0

    @property
    def gates(self) -> list[Gate]:
        return [HELD]

    def check(self, model: ModelConfig) -> list[str]:
        if self._check_error is not None:
            raise self._check_error
        return []

    async def sample(  # type: ignore[override]
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> AsyncGenerator[Part | Finish | Delta, None]:
        self.samples += 1
        async for item in self._rogue(self.samples):
            yield item


def replies(h: Harness, cursor: int | None = None) -> list[Body.Assistant]:
    return [b for b in h.bodies(cursor) if isinstance(b, Body.Assistant)]


@scenario
async def test_a_provider_that_raises_mid_stream_fails_that_sample_and_no_other(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def script(n: int) -> AsyncGenerator[Part | Finish | Delta, None]:
        if n == 1:
            yield ThinkingDelta("hm")
            yield Part.Thinking("hm")
            yield TextDelta("he")
            raise RuntimeError("bug")
        yield Part.Text("fine")
        yield Finish(Stop.End)

    h = await Harness.create(tmp_path, provider=Rogue(script))
    other = await h.client.open(SYS)
    await h.send("hi")
    await h.settle()
    (reply,) = replies(h)
    assert reply.stop == Stop.Error
    assert reply.parts == [Part.Thinking("hm"), Part.Text("he")], "the parts are kept"
    assert reply.meta is not None
    assert json.loads(reply.meta) == {"error": {"message": "RuntimeError: bug", "internal": True}}
    assert "RuntimeError: bug" in capsys.readouterr().err, "the traceback goes to stderr"
    assert "Traceback" not in reply.meta
    await h.client.send(other, "hello")
    await h.settle()
    assert [r.parts for r in replies(h, other)] == [[Part.Text("fine")]], "the driver lives on"
    await h.close()


@scenario
async def test_a_check_that_raises_fails_the_sample_and_not_the_driver(tmp_path: Path) -> None:
    async def script(n: int) -> AsyncGenerator[Part | Finish | Delta, None]:
        yield Finish(Stop.End)

    h = await Harness.create(tmp_path, provider=Rogue(script, RuntimeError("check bug")))
    await h.send("hi")
    await h.settle()
    h.check()
    (error,) = [n.body for n in h.nodes() if isinstance(n.body, Body.Custom)]
    assert error.kind == "sample_error"
    assert "RuntimeError" in error.data
    assert "check bug" in error.data
    await h.close()


HELD = Gate()  # a provider parked here never gets past it


async def resets(n: int) -> AsyncGenerator[Part | Finish | Delta, None]:
    yield TextDelta("x")
    try:
        await HELD.wait()
    except asyncio.CancelledError:
        raise SampleError("connection reset") from None
    yield Finish(Stop.End)  # pragma: no cover


async def leaks(n: int) -> AsyncGenerator[Part | Finish | Delta, None]:
    yield TextDelta("x")
    try:
        await HELD.wait()
    finally:
        raise OSError("cleanup failed")
    yield Finish(Stop.End)  # pragma: no cover


@pytest.mark.parametrize("script", [resets, leaks], ids=["connection reset", "cleanup fails"])
@scenario
async def test_an_abort_is_an_abort_whatever_the_provider_raises_while_cancelled(
    tmp_path: Path, script: Callable[[int], AsyncGenerator[Part | Finish | Delta, None]]
) -> None:
    h = await Harness.create(tmp_path, provider=Rogue(script))
    await h.send("hi")
    await h.settle()
    await h.abort()
    await h.settle()
    h.check()
    assert replies(h) == [Body.Assistant([Part.Text("x")], Stop.Interrupted)]
    await h.close()


@scenario
async def test_a_failed_reply_is_strict_json_and_keeps_the_providers_own_error(
    tmp_path: Path,
) -> None:
    said = json.dumps({"error": {"type": "overloaded", "code": 529}})

    async def script(n: int) -> AsyncGenerator[Part | Finish | Delta, None]:
        yield Part.Text("t")
        if n == 1:
            raise SampleError("slow down", said, status=529, retry_after=math.inf)
        raise SampleError("again", retry_after=math.nan)

    h = await Harness.create(tmp_path, provider=Rogue(script))
    await h.send("hi")
    await h.settle()
    await h.send("again")
    await h.settle()

    def strict(constant: str) -> float:
        raise AssertionError(f"meta holds {constant}, which is not JSON")

    metas = [json.loads(r.meta or "", parse_constant=strict) for r in replies(h)]
    assert metas[0] == {
        "error": {
            "message": "slow down",
            "status": 529,
            "provider": {"type": "overloaded", "code": 529},
        }
    }, "no infinite retry_after, and the provider's error is kept"
    assert metas[1] == {"error": {"message": "again"}}
    await h.close()


@scenario
async def test_a_hook_answer_that_changed_nothing_is_not_announced(tmp_path: Path) -> None:
    rec = Recorder("rec")
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("x")])]), extensions=[rec]
    )
    client = await h.connect("watcher")
    seen: list[Event] = []

    async def read() -> None:
        async for event in client:
            seen.append(event)

    reader = asyncio.ensure_future(read())
    await h.send("hi")
    await h.settle()
    await h.close()
    await reader
    assert len(rec.points) == 6, "six hooks answered"
    views = [e for e in seen if isinstance(e, CursorView)]
    assert all(a != b for a, b in pairwise(views)), "nothing said twice"


@scenario
async def test_a_client_connected_after_the_driver_failed_ends_with_the_failure(
    tmp_path: Path,
) -> None:
    h, gate = await failing_harness(tmp_path)
    await h.send("hi")
    await h.settle()
    h.session.close()
    gate.release()
    await h.dies()
    late = h.driver.client("late")
    with pytest.raises(ConnectionError, match="Closed"):
        await asyncio.wait_for(anext(late), 1)
    await h.driver.stop()
    # stopping does not turn it into a clean end
    with pytest.raises(ConnectionError, match="Closed"):
        await asyncio.wait_for(anext(h.driver.client("later")), 1)


@scenario
async def test_a_client_connected_after_a_stop_ends_cleanly(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    await h.driver.stop()
    assert [e async for e in h.driver.client("late")] == []
    h.session.close()


@scenario
async def test_calls_on_a_stopped_driver_raise_and_change_nothing(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    await h.driver.stop()
    before = h.snapshot()
    c, client = h.cursor, h.client
    calls: list[Callable[[], Awaitable[object]]] = [
        lambda: client.send(c, "anyone?"),
        lambda: client.input(c, "anyone?"),
        lambda: client.step(c),
        lambda: client.go_to(c, SYS),
        lambda: client.set_auto(c, True),
        lambda: client.abort(c),
        lambda: client.open(SYS),
        lambda: client.set_label(c, "main"),
        lambda: client.remove_label("no-such-label"),
        lambda: client.watch([c]),
        lambda: client.answer(0, "yes"),
    ]
    for call in calls:
        with pytest.raises(ConnectionError, match="the driver is stopped"):
            await call()
    with pytest.raises(ConnectionError, match="the driver is stopped"):
        h.driver.add_item(c, ItemData.Extension("x", "y"))
    assert h.snapshot() == before, "no cursor was left busy with nothing to run it"
    h.session.close()


@scenario
async def test_calls_on_a_failed_driver_raise_connection_error_chained_to_the_failure(
    tmp_path: Path,
) -> None:
    h, gate = await failing_harness(tmp_path)
    await h.send("hi")
    await h.settle()
    h.session.close()
    gate.release()
    await h.dies()
    with pytest.raises(ConnectionError, match=r"^Closed: ") as ended:
        await h.client.send(h.cursor, "again")
    if not h.remote:  # a wire carries "Type: msg", not the exception
        assert isinstance(ended.value.__cause__, Closed), "the failure, chained"
    with pytest.raises(ConnectionError, match="Closed"):
        h.driver.add_item(h.cursor, ItemData.Extension("x", "y"))
    with pytest.raises(ConnectionError, match="Closed"):
        await h.client.answer(0, "yes")
    with pytest.raises(ConnectionError, match="Closed"):
        await h.client.remove_label("no-such-label")
    await h.driver.stop()


@scenario
async def test_an_item_added_from_outside_wakes_the_cursor_and_is_announced(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply([Part.Text("hi")])]))
    client = await h.connect("watcher")
    await snapshot(client)
    item = h.driver.add_item(h.cursor, ItemData.Extension("timer", "tick"))
    view = await asyncio.wait_for(anext(client), 1)
    assert isinstance(view, CursorView)
    assert [i.id for i in view.items] == [item]
    with pytest.raises(TypeError, match="input"):
        h.driver.add_item(h.cursor, ItemData.Input(Author.Frontend("term"), "typed"))
    await client.close()
    await h.close()


@scenario
async def test_labels_a_client_writes_are_sent_whole(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    other = await h.client.open(SYS)
    await h.client.set_label(h.cursor, "main")
    client = await h.connect("watcher")
    await snapshot(client, cursors=2)
    await h.client.set_label(other, "main")  # moves from h.cursor to other
    assert await asyncio.wait_for(anext(client), 1) == Labels((("main", other),))
    await h.client.remove_label("main")
    assert await asyncio.wait_for(anext(client), 1) == Labels(())
    await client.close()
    await h.close()
