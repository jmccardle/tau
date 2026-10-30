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


@scenario
async def test_the_reply_streams_as_several_deltas_then_is_saved_whole() -> None:
    async with mock_session(options={"delay_ms": 0}) as (session, driver, cursor):
        stream = driver.events(cursor)
        driver.send(cursor, "term", "hello brave new world")
        await driver.rest(cursor)
        (reply,) = replies(session)
        await driver.stop()  # ends the stream after what it holds
        events: list[Event] = [e async for e in stream]
    deltas = [
        e.delta.text for e in events if isinstance(e, Live) and isinstance(e.delta, TextDelta)
    ]
    assert len(deltas) == 5
    assert "".join(deltas) == "mock: hello brave new world"
    assert reply.parts == [Part.Text("mock: hello brave new world")]
    assert isinstance(events[-1], Saved)


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
async def test_an_aborted_sample_leaves_an_interrupted_reply_equal_to_the_deltas_seen() -> None:
    async with mock_session(options={"delay_ms": 0, "reply": WORDS}) as (session, driver, cursor):
        stream = driver.events(cursor)
        seen: list[Event] = []
        heard_two = asyncio.Event()

        async def listen() -> None:
            async for event in stream:
                seen.append(event)
                if sum(isinstance(e, Live) for e in seen) == 2:
                    heard_two.set()

        listener = asyncio.ensure_future(listen())
        driver.send(cursor, "term", "go")
        await heard_two.wait()
        driver.abort(cursor)
        await driver.rest(cursor)
        (reply,) = replies(session)
        await driver.stop()
        await listener
    streamed = "".join(
        e.delta.text for e in seen if isinstance(e, Live) and isinstance(e.delta, TextDelta)
    )
    assert reply.stop == Stop.Interrupted
    assert reply.parts == [Part.Text(streamed)]
    assert 0 < len(streamed) < len(WORDS)
