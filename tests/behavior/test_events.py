"""`driver.events()` (D15): live deltas from the provider, a saved event after each answer."""

import itertools
from pathlib import Path

from fakes import Fail, FakeProvider, Reply
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
DELTAS = [
    ThinkingDelta("hm"),
    TextDelta("hel"),
    TextDelta("lo"),
]
# A part is yielded before the first delta of the next part.
SCRIPT = [DELTAS[0], Part.Thinking("hm"), DELTAS[1], DELTAS[2], Part.Text("hello")]


async def drain(h: Harness, *streams: EventStream) -> list[list[Event]]:
    """Everything each stream will deliver: the driver stops, which ends the streams."""
    await h.close()
    return [[event async for event in stream] for stream in streams]


@scenario
async def test_live_deltas_arrive_before_the_saved_event_that_holds_the_reply(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply(SCRIPT)]))
    stream = h.driver.events()
    await h.send("hi")
    seen: list[Event] = []
    async for event in stream:
        seen.append(event)
        if isinstance(event, Saved) and isinstance(h.bodies()[-1], Body.Assistant):
            break  # a saved event told us to look at the tree, and the reply is there
    lives = [e for e in seen if isinstance(e, Live)]
    assert [e.delta for e in lives] == DELTAS
    assert seen.index(lives[-1]) < len(seen) - 1, "the last delta came before the saved event"
    assert {e.cursor for e in seen} == {h.cursor}
    await stream.aclose()
    await h.close()


@scenario
async def test_a_cursor_filter_hears_only_that_cursor(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply(SCRIPT)]))
    other = h.driver.open(SYS, Author.Frontend("term"))
    mine, theirs, everyone = (h.driver.events(c) for c in (h.cursor, other, None))
    await h.send("hi")
    await h.settle()
    mine_seen, theirs_seen, everyone_seen = await drain(h, mine, theirs, everyone)
    assert {e.cursor for e in mine_seen} == {h.cursor}
    assert not any(isinstance(e, Live) for e in theirs_seen), "the other cursor sampled nothing"
    assert {e.cursor for e in theirs_seen} <= {other}
    lives = [e for e in everyone_seen if isinstance(e, Live)]
    assert lives == [e for e in mine_seen if isinstance(e, Live)] != []


@scenario
async def test_a_slow_subscriber_loses_live_events_but_never_a_saved_one(tmp_path: Path) -> None:
    calls = [CallNamed(0, "c1", "read"), CallArgs(0, '{"p'), CallArgs(0, 'ath"}')]
    batch = Reply([*SCRIPT, *calls, Part.Call("c1", "read", '{"path"}')], stop=Stop.Tools)
    h = await Harness.create(tmp_path, provider=FakeProvider([batch, Reply([Part.Text("done")])]))
    slow, fast = h.driver.events(buffer=2), h.driver.events()
    await h.send("hi")
    await h.settle()  # nothing read yet: the pump went on regardless
    kept, everything = await drain(h, slow, fast)
    live = [e for e in everything if isinstance(e, Live)]
    assert len(live) == len(DELTAS) + len(calls)
    assert len([e for e in kept if isinstance(e, Live)]) <= 2
    assert kept[-1] == Saved(h.cursor)
    assert [e for e in kept if isinstance(e, Saved)], "saved events survive a full buffer"
    assert not any(a == b and isinstance(a, Saved) for a, b in itertools.pairwise(kept))


@scenario
async def test_a_failed_sample_still_emits_saved(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Fail("boom", [DELTAS[1]])]))
    stream = h.driver.events()
    await h.send("hi")
    await h.settle()
    (events,) = await drain(h, stream)
    assert Live(h.cursor, DELTAS[1]) in events
    assert events[-1] == Saved(h.cursor)


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
async def test_text_that_never_became_a_part_is_saved_when_the_sample_fails(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Fail("boom", [*DELTAS[1:]])]))
    await h.send("hi")
    await h.settle()
    reply = next(b for b in h.bodies() if isinstance(b, Body.Assistant))
    assert reply.parts == [Part.Text("hello")]
    await h.close()
