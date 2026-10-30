"""The mock provider, streaming through a real driver (D15)."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from harness import scenario

from tau_next import (
    Body,
    Driver,
    Event,
    EventStream,
    Live,
    Part,
    Saved,
    Session,
    Stop,
    SystemClock,
    TextDelta,
    builtin_providers,
    model_config,
    read_models,
)

WORDS = " ".join(f"w{i}" for i in range(200))


@asynccontextmanager
async def mock_session(**settings: object) -> AsyncIterator[tuple[Session, Driver, int]]:
    """A session whose model is the mock provider with `settings`, and a started driver."""
    models = read_models(cli={"models": {"m": {"model": "m", "provider": "mock", **settings}}})
    providers = builtin_providers({})
    session = Session.create(model_config("m"), "sys", SystemClock().now())
    driver = Driver(session, providers, models)
    driver.start()
    try:
        yield session, driver, session.cursors()[0]
    finally:
        await driver.stop()
        session.close()
        for provider in providers.values():
            await provider.aclose()


def replies(session: Session) -> list[Body.Assistant]:
    return [
        n.body
        for n in session.tree.walk(session.cursor(0).at)
        if isinstance(n.body, Body.Assistant)
    ]


async def listen(stream: EventStream, seen: list[Event], heard: asyncio.Event, n: int) -> None:
    """Collect the stream; set `heard` at the `n`th live event. A reader that keeps up sees
    every live event: a `Saved` clears the ones it has not read."""
    async for event in stream:
        seen.append(event)
        if sum(isinstance(e, Live) for e in seen) == n:
            heard.set()


@scenario
async def test_the_reply_streams_as_several_deltas_then_is_saved_whole() -> None:
    async with mock_session(options={"delay_ms": 1}) as (session, driver, cursor):
        seen: list[Event] = []
        listener = asyncio.ensure_future(listen(driver.events(cursor), seen, asyncio.Event(), 0))
        driver.send(cursor, "term", "hello brave new world")
        await driver.rest(cursor)
        (reply,) = replies(session)
        await driver.stop()  # ends the stream after what it holds
        await listener
    deltas = [e.delta.text for e in seen if isinstance(e, Live) and isinstance(e.delta, TextDelta)]
    assert len(deltas) == 5
    assert "".join(deltas) == "mock: hello brave new world"
    assert reply.parts == [Part.Text("mock: hello brave new world")]
    assert seen.index(Saved(cursor)) < len(seen), "a Saved follows"
    assert isinstance(seen[-1], Saved)


@scenario
async def test_the_saved_reply_is_the_text_streamed_with_usage_and_a_normal_stop() -> None:
    async with mock_session(options={"delay_ms": 0, "reply": "one two"}) as (
        session,
        driver,
        cursor,
    ):
        driver.send(cursor, "term", "a b c")
        await driver.rest(cursor)
        (reply,) = replies(session)
        assert (reply.parts, reply.stop, reply.finish_reason) == (
            [Part.Text("one two")],
            Stop.End,
            "stop",
        )
        assert json.loads(reply.meta or "") == {"usage": {"input": 3, "output": 2, "cached": 0}}


@scenario
async def test_max_tokens_caps_the_words_and_ends_with_the_length_stop() -> None:
    settings = {"max_tokens": 2, "options": {"delay_ms": 0, "reply": "one two three four"}}
    async with mock_session(**settings) as (session, driver, cursor):
        driver.send(cursor, "term", "go")
        await driver.rest(cursor)
        (reply,) = replies(session)
        assert (reply.parts, reply.stop) == ([Part.Text("one two ")], Stop.Length)


@scenario
async def test_an_aborted_sample_leaves_an_interrupted_reply_holding_the_text_streamed() -> None:
    async with mock_session(options={"delay_ms": 1, "reply": WORDS}) as (session, driver, cursor):
        seen: list[Event] = []
        heard_two = asyncio.Event()
        listener = asyncio.ensure_future(listen(driver.events(cursor), seen, heard_two, 2))
        driver.send(cursor, "term", "go")
        await heard_two.wait()
        driver.abort(cursor)
        await driver.rest(cursor)
        (reply,) = replies(session)
        await driver.stop()
        await listener
    seen_text = "".join(
        e.delta.text for e in seen if isinstance(e, Live) and isinstance(e.delta, TextDelta)
    )
    assert reply.stop == Stop.Interrupted
    (part,) = reply.parts
    assert isinstance(part, Part.Text)
    # Everything seen is in the reply (a delta not yet read is cleared by the Saved, but
    # the reply holds it), and the reply ends on a word the mock had streamed.
    assert part.text.startswith(seen_text)
    assert seen_text
    assert WORDS.startswith(part.text)
    assert part.text.endswith(" ")
    assert len(part.text) < len(WORDS)


@scenario
async def test_a_bad_option_fails_that_sample_and_the_cursor_survives() -> None:
    for bad in ({"delay_ms": "fast"}, {"reply": 5}, {"dealy_ms": 1}, {"reply": ""}):
        async with mock_session(options=bad) as (session, driver, cursor):
            driver.send(cursor, "term", "go")
            await driver.rest(cursor)  # raises if the driver died
            driver.check()
            notes = [n.body for n in (session.tree.node(i) for i in range(len(session.tree)))]
            errors = [b.data for b in notes if isinstance(b, Body.Custom)]
            assert len(errors) == 1
            assert "(mock)" in errors[0]
            assert not any(isinstance(b, Body.Assistant) and b.parts for b in notes)
