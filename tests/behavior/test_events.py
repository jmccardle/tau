"""`driver.events()` (D15): live deltas from the provider, a saved event after each answer."""

import asyncio
import gc
import json
import math
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from pathlib import Path

import pytest
from fakes import Fail, FakeProvider, FakeTool, Gate, Recorder, Reply
from harness import Harness, scenario

from tau_next import (
    Body,
    CallArgs,
    CallNamed,
    Closed,
    Delta,
    Event,
    EventStream,
    Finish,
    ItemData,
    Live,
    ModelConfig,
    Node,
    Outcome,
    Part,
    SampleError,
    Saved,
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


async def died(h: Harness) -> None:
    """Wait until the driver has recorded what it died of (a stream subscribed first sees it)."""
    with pytest.raises(Closed):
        await asyncio.wait_for(read_all(h.driver.events()), 1)


def held(gate: Gate) -> Reply:
    """A reply that streams SCRIPT and waits at `gate` before its `Finish`."""
    return Reply(SCRIPT, gate=gate, gate_after=len(SCRIPT))


async def drain(h: Harness, *streams: EventStream) -> list[list[Event]]:
    """Everything each stream will deliver: the driver stops, which ends the streams."""
    await h.close()
    return [[event async for event in stream] for stream in streams]


async def read_all(stream: EventStream) -> list[Event]:
    return [event async for event in stream]


def lives(events: list[Event]) -> list[Live]:
    return [e for e in events if isinstance(e, Live)]


@scenario
async def test_live_deltas_are_read_as_they_arrive_and_the_saved_event_follows(
    tmp_path: Path,
) -> None:
    gate = Gate()
    h = await Harness.create(tmp_path, provider=FakeProvider([held(gate)]))
    stream = h.driver.events()
    await h.send("hi")
    await h.settle()  # parked at the gate with every delta streamed
    arrived: list[Event] = []
    while len(lives(arrived)) < len(DELTAS):
        arrived.append(await anext(stream))
    assert [e.delta for e in lives(arrived)] == DELTAS
    gate.release()
    await h.settle()
    saved = await anext(stream)
    assert saved == Saved(h.cursor, reply=True)
    assert isinstance(h.bodies()[-1], Body.Assistant), "Saved: the reply is in the tree"
    await stream.aclose()
    await h.close()


@scenario
async def test_a_saved_event_clears_that_cursors_live_layer_and_earlier_saved(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply(SCRIPT)]))
    stream = h.driver.events()
    await h.send("hi")
    await h.settle()
    (events,) = await drain(h, stream)
    assert events == [Saved(h.cursor, reply=True)], "nothing read: one saved event says it all"


@scenario
async def test_a_cursor_filter_hears_only_that_cursor(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(tmp_path, provider=FakeProvider([held(gate)]))
    other = h.driver.open(SYS, "term")
    mine, theirs, everyone = (h.driver.events(c) for c in (h.cursor, other, None))
    await h.send("hi")
    await h.settle()
    mine_seen, theirs_seen, everyone_seen = await drain(h, mine, theirs, everyone)
    assert {e.cursor for e in mine_seen} == {h.cursor}
    assert lives(mine_seen) == lives(everyone_seen) != []
    assert not lives(theirs_seen), "the other cursor sampled nothing"
    assert {e.cursor for e in theirs_seen} == {other}


@scenario
async def test_a_slow_subscriber_loses_live_events_and_never_slows_the_pump(
    tmp_path: Path,
) -> None:
    gate = Gate()
    calls = [CallNamed(0, "c1", "read"), CallArgs(0, '{"p'), CallArgs(0, 'ath"}')]
    batch = Reply(
        [*SCRIPT, *calls, Part.Call("c1", "read", '{"path"}')],
        stop=Stop.Tools,
        gate=gate,
        gate_after=len(SCRIPT) + 4,
    )
    h = await Harness.create(tmp_path, provider=FakeProvider([batch, Reply([Part.Text("done")])]))
    slow, fast = h.driver.events(buffer=2), h.driver.events()
    await h.send("hi")
    await h.settle()  # parked at the gate: the pump ran with nobody reading
    slow_seen, fast_seen = await drain(h, slow, fast)
    assert len(lives(fast_seen)) == len(DELTAS) + len(calls)
    assert len(slow_seen) == 2, "the buffer held its capacity and no more"
    assert len(lives(slow_seen)) < len(lives(fast_seen))


@scenario
async def test_buffers_are_bounded_by_capacity_plus_one_saved_per_cursor(tmp_path: Path) -> None:
    replies = [Reply([Part.Text(f"r{i}")]) for i in range(6)]
    h = await Harness.create(tmp_path, provider=FakeProvider(replies))
    second = h.driver.open(SYS, "term")
    stream = h.driver.events(buffer=4)
    for _ in range(3):
        await h.send("a")
        await h.send("b", second)
        await h.settle()
    (seen,) = await drain(h, stream)
    assert len(seen) <= 4 + 2
    assert sorted(e.cursor for e in seen if isinstance(e, Saved)) == sorted([h.cursor, second])


@scenario
async def test_a_failed_sample_still_emits_saved(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Fail("boom", [DELTAS[1]])]))
    stream = h.driver.events()
    await h.send("hi")
    await h.settle()
    (events,) = await drain(h, stream)
    assert events == [Saved(h.cursor, reply=True)]


@scenario
async def test_a_frontend_call_that_changes_a_cursor_is_heard_by_another_frontend(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    stream = h.driver.events()
    await h.input("hello")
    assert await anext(stream) == Saved(h.cursor)
    opened = h.driver.open(SYS, "term")
    assert await anext(stream) == Saved(opened)
    mine = h.driver.events(opened)  # the cursor's own steps are not the subject here
    h.driver.go_to(opened, 0, "term")
    assert await anext(mine) == Saved(opened)
    await mine.aclose()
    await stream.aclose()
    await h.close()


@scenario
async def test_a_frontend_call_that_changes_nothing_is_not_announced(tmp_path: Path) -> None:
    """An abort on a resting cursor, a `set_auto` to the value it has: nothing a frontend could
    see changed, so no `Saved`."""
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    stream = h.driver.events()
    waiting = asyncio.ensure_future(anext(stream))
    assert not await h.abort()
    await h.set_auto(False)
    await h.settle()
    assert not waiting.done()
    await h.set_auto(True)  # this does change the cursor
    assert await waiting == Saved(h.cursor)
    await stream.aclose()
    await h.close()


@scenario
async def test_closing_the_stream_unsubscribes(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply(SCRIPT)]))
    assert h.driver.state().subscribers == 0
    stream = h.driver.events()
    async with h.driver.events() as other:
        assert h.driver.state().subscribers == 2
        assert other is not stream
    assert h.driver.state().subscribers == 1
    await stream.aclose()
    await stream.aclose()  # closing twice is fine
    assert h.driver.state().subscribers == 0
    assert [e async for e in stream] == []
    await h.send("hi")
    await h.settle()
    await h.close()


@scenario
async def test_an_abandoned_stream_is_collected(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    stream = h.driver.events()
    await h.input("x")
    async for _ in stream:
        break  # never closed
    del stream
    gc.collect()
    assert h.driver.state().subscribers == 0
    await h.close()


@scenario
async def test_a_second_consumer_of_one_stream_is_refused(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    stream = h.driver.events()
    first = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="one consumer"):
        await anext(stream)
    await h.input("x")
    assert await first == Saved(h.cursor)
    await h.close()


@scenario
async def test_a_driver_that_fails_ends_every_stream_with_the_failure(tmp_path: Path) -> None:
    h, gate = await failing_harness(tmp_path)
    stream = h.driver.events()
    await h.send("hi")
    await h.settle()
    h.session.close()  # the next answer meets a closed session: the pump dies of it
    gate.release()
    # buffered events are delivered first, then the failure
    assert await anext(stream) == Saved(h.cursor, reply=True)
    with pytest.raises(Closed):
        await anext(stream)
    await h.driver.stop()


@scenario
async def test_stopping_a_failed_driver_does_not_turn_the_failure_into_a_clean_end(
    tmp_path: Path,
) -> None:
    h, gate = await failing_harness(tmp_path)
    stream = h.driver.events()
    await h.send("hi")
    await h.settle()
    h.session.close()
    gate.release()
    with pytest.raises(Closed):
        await read_all(stream)
    await h.driver.stop()  # the natural cleanup
    with pytest.raises(Closed):
        await read_all(stream)  # the frontend still learns the driver died


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
    stream = h.driver.events()
    await h.send("go")
    await h.settle()
    h.session.close()
    gate.release()  # the fast call answers into a closed session
    with pytest.raises(Closed):
        await read_all(stream)
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
    other = h.driver.open(SYS, "term")
    await h.send("hi")
    await h.settle()
    (reply,) = replies(h)
    assert reply.stop == Stop.Error
    assert reply.parts == [Part.Thinking("hm"), Part.Text("he")], "the parts are kept"
    assert reply.meta is not None
    assert json.loads(reply.meta) == {"error": {"message": "RuntimeError: bug", "internal": True}}
    assert "RuntimeError: bug" in capsys.readouterr().err, "the traceback goes to stderr"
    assert "Traceback" not in reply.meta
    h.driver.send(other, "term", "hello")
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
async def test_typing_ahead_mid_stream_keeps_the_unread_deltas(tmp_path: Path) -> None:
    gate = Gate()
    script = [CallNamed(0, "c", "t"), CallArgs(0, "{}"), Part.Call("c", "t", "{}")]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply(script, Stop.Tools, gate=gate, gate_after=len(script))]),
    )
    stream = h.driver.events()
    await h.send("hi")
    await h.settle()  # streamed, parked at the gate
    await h.send("and also")  # a Saved that is not the reply
    events: list[Event] = []
    async with asyncio.timeout(1):
        while len(events) < 3:
            events.append(await anext(stream))
    assert [e.delta for e in lives(events)] == script[:2]
    assert events[2] == Saved(h.cursor), "the unread CallNamed and CallArgs are still there"
    await stream.aclose()
    gate.release()
    await h.close()


@scenario
async def test_a_hook_answer_that_changed_nothing_is_not_announced(tmp_path: Path) -> None:
    rec = Recorder("rec")
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("x")])]), extensions=[rec]
    )
    stream = h.driver.events()
    seen: list[Event] = []

    async def read() -> None:
        async for event in stream:
            seen.append(event)

    reader = asyncio.ensure_future(read())
    await h.send("hi")
    await h.settle()
    await h.close()
    await reader
    assert len(rec.points) == 6, "six hooks answered"
    saved = [e for e in seen if isinstance(e, Saved)]
    assert [s.reply for s in saved].count(True) == 1
    assert len(saved) == 5, "the send, the input node, the reply, and two moves; not each hook"


@scenario
async def test_a_stream_subscribed_after_the_driver_failed_ends_with_the_failure(
    tmp_path: Path,
) -> None:
    h, gate = await failing_harness(tmp_path)
    await h.send("hi")
    await h.settle()
    h.session.close()
    gate.release()
    await died(h)
    late = h.driver.events()
    with pytest.raises(Closed):
        await asyncio.wait_for(anext(late), 1)
    assert h.driver.state().subscribers == 0
    await h.driver.stop()
    with pytest.raises(Closed):  # stopping does not turn it into a clean end
        await asyncio.wait_for(anext(h.driver.events()), 1)


@scenario
async def test_a_stream_subscribed_after_a_stop_ends_cleanly(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    await h.driver.stop()
    assert [e async for e in h.driver.events()] == []
    assert h.driver.state().subscribers == 0
    h.session.close()


@scenario
async def test_frontend_calls_on_a_stopped_driver_raise_and_change_nothing(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    await h.driver.stop()
    before = h.snapshot()
    calls: list[Callable[[], object]] = [
        lambda: h.driver.send(h.cursor, "term", "anyone?"),
        lambda: h.driver.input(h.cursor, "term", "anyone?"),
        lambda: h.driver.step(h.cursor),
        lambda: h.driver.go_to(h.cursor, SYS, "term"),
        lambda: h.driver.set_auto(h.cursor, True),
        lambda: h.driver.abort(h.cursor),
        lambda: h.driver.open(SYS, "term"),
        lambda: h.driver.add_item(h.cursor, ItemData.Extension("x", "y")),
        lambda: h.driver.set_label("main", h.cursor),
    ]
    for call in calls:
        with pytest.raises(RuntimeError, match="stopped"):
            call()
    assert h.snapshot() == before, "no cursor was left busy with nothing to run it"
    h.session.close()


@scenario
async def test_frontend_calls_on_a_failed_driver_raise_the_failure(tmp_path: Path) -> None:
    h, gate = await failing_harness(tmp_path)
    await h.send("hi")
    await h.settle()
    h.session.close()
    gate.release()
    await died(h)
    with pytest.raises(Closed):
        h.driver.send(h.cursor, "term", "again")
    with pytest.raises(Closed):
        h.driver.add_item(h.cursor, ItemData.Extension("x", "y"))
    await h.driver.stop()


@scenario
async def test_a_frontend_adds_an_item_and_the_cursor_wakes_and_is_announced(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply([Part.Text("hi")])]))
    stream = h.driver.events()
    item = h.driver.add_item(h.cursor, ItemData.Extension("timer", "tick"))
    assert await asyncio.wait_for(anext(stream), 1) == Saved(h.cursor)
    assert [i.id for i in h.session.cursor(h.cursor).items] == [item]
    with pytest.raises(TypeError, match="input"):
        h.driver.add_item(h.cursor, ItemData.Input("term", "typed"))
    await stream.aclose()
    await h.close()


@scenario
async def test_labels_a_frontend_writes_are_announced_for_each_cursor_they_change(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    other = h.driver.open(SYS, "term")
    h.driver.set_label("main", h.cursor)
    stream = h.driver.events()
    h.driver.set_label("main", other)  # moves from h.cursor to other
    assert {await anext(stream), await anext(stream)} == {Saved(h.cursor), Saved(other)}
    h.driver.remove_label("main")
    assert await anext(stream) == Saved(other)
    await stream.aclose()
    await h.close()
