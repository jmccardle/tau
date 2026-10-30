"""`driver.events()` (D15): live deltas from the provider, a saved event after each answer."""

import asyncio
import gc
from pathlib import Path

import pytest
from fakes import Fail, FakeProvider, Gate, Reply
from harness import Harness, scenario

from tau_next import (
    Author,
    Body,
    CallArgs,
    CallNamed,
    Event,
    EventStream,
    Live,
    Part,
    Saved,
    Stop,
    TextDelta,
    ThinkingDelta,
)

SYS = 1  # the system node's id: a new session is Config 0, System 1
DELTAS = [ThinkingDelta("hm"), TextDelta("hel"), TextDelta("lo")]
# A part is yielded before the first delta of the next part.
SCRIPT = [DELTAS[0], Part.Thinking("hm"), DELTAS[1], DELTAS[2], Part.Text("hello")]


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
    assert saved == Saved(h.cursor)
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
    assert events == [Saved(h.cursor)], "nothing was read: one saved event is all there is to say"


@scenario
async def test_a_cursor_filter_hears_only_that_cursor(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(tmp_path, provider=FakeProvider([held(gate)]))
    other = h.driver.open(SYS, Author.Frontend("term"))
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
    second = h.driver.open(SYS, Author.Frontend("term"))
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
    assert events == [Saved(h.cursor)]


@scenario
async def test_a_frontend_call_that_changes_a_cursor_is_heard_by_another_frontend(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    stream = h.driver.events()
    await h.input("hello")
    assert await anext(stream) == Saved(h.cursor)
    opened = h.driver.open(SYS, Author.Frontend("term"))
    assert await anext(stream) == Saved(opened)
    h.driver.go_to(opened, SYS, "term")
    assert await anext(stream) == Saved(opened)
    await stream.aclose()
    await h.close()


@scenario
async def test_closing_the_stream_unsubscribes(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply(SCRIPT)]))
    assert h.driver.subscribers == 0
    stream = h.driver.events()
    async with h.driver.events() as other:
        assert h.driver.subscribers == 2
        assert other is not stream
    assert h.driver.subscribers == 1
    await stream.aclose()
    await stream.aclose()  # closing twice is fine
    assert h.driver.subscribers == 0
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
    assert h.driver.subscribers == 0
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
    class Boom(Exception):
        pass

    # A provider that raises something other than SampleError is the crash path.
    class Crashing(FakeProvider):
        async def sample(self, walk, model, tools):  # type: ignore[no-untyped-def,override]
            raise Boom("provider bug")
            yield  # pragma: no cover

    h = await Harness.create(tmp_path, provider=Crashing([]))
    stream = h.driver.events()
    waiting = asyncio.ensure_future(anext(stream))
    await h.send("hi")
    with pytest.raises(Boom):
        await h.driver.rest(h.cursor)
    # buffered events are delivered first, then the failure
    assert await waiting == Saved(h.cursor)  # what was buffered comes first
    with pytest.raises(Boom):
        await read_all(stream)
    await h.close()


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
}


@pytest.mark.parametrize("script", VIOLATIONS.values(), ids=VIOLATIONS.keys())
@scenario
async def test_a_provider_that_breaks_the_ordering_rule_fails_the_sample_and_keeps_the_text(
    tmp_path: Path, script: list[Part | TextDelta]
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply(script)]))
    await h.send("hi")
    await h.settle()
    h.driver.check()
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
