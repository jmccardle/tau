"""What the three hosted providers share (`_wire.Connection`, and the driver over any of them),
tested once and run through each: one long-lived client, the loop it belongs to, `ProviderClosed`,
the key, what follows a finish, and a cancel."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from harness import scenario
from wire import (
    ALIAS,
    DIALECTS,
    EACH,
    ENVIRON,
    KEY_ENV,
    SECRET,
    UNUSED,
    Dialect,
    KeepAliveServer,
    Server,
    bodies,
    collect,
    collect_all,
    cut,
    gated,
    hello_walk,
    running,
    sse_text,
)

from tau_next import (
    Body,
    Delta,
    Finish,
    Live,
    ModelConfig,
    Part,
    Provider,
    ProviderClosed,
    SampleError,
    Stop,
    TextDelta,
    ThinkingDelta,
    builtin_providers,
    model_config,
)


async def sample_all(
    provider: Provider, config: ModelConfig
) -> tuple[list[Part | Finish | Delta], SampleError | None]:
    """Everything `sample` yielded, and the `SampleError` it ended with, if any."""
    return await collect_all(provider, hello_walk(), config)


async def collect_rest(stream: AsyncIterator[object]) -> tuple[list[object], SampleError | None]:
    parts: list[object] = []
    try:
        async for item in stream:
            parts.append(item)
    except SampleError as e:
        return parts, e
    return parts, None


# The one client


@EACH
@scenario
async def test_samples_share_one_connection_until_aclose_which_may_be_repeated(
    dialect: Dialect,
) -> None:
    server = KeepAliveServer(dialect.reply("hi"))
    with server.running() as root:
        provider = dialect.make(None, ENVIRON)
        for _ in range(3):
            items, error = await sample_all(provider, dialect.model(root))
            assert error is None
            assert [i for i in items if isinstance(i, Part)] == [Part.Text("hi")]
            assert isinstance(items[-1], Finish)
        await provider.aclose()
        await provider.aclose()  # closing twice is not a failure
    assert server.connections == 1


@EACH
@scenario
async def test_a_sample_after_aclose_fails_as_closed_not_as_a_transient_error(
    dialect: Dialect,
) -> None:
    provider = dialect.serving(dialect.reply("hi"))
    await provider.aclose()
    items, error = await sample_all(provider, dialect.model())
    assert items == []
    assert isinstance(error, ProviderClosed)
    assert (error.status, error.retry_after) == (None, None)


@EACH
def test_a_program_that_runs_one_event_loop_per_command_keeps_working(dialect: Dialect) -> None:
    server = KeepAliveServer(dialect.reply("hi"))
    with server.running() as root:
        provider = dialect.make(None, ENVIRON)
        for _ in range(2):
            items, error = asyncio.run(sample_all(provider, dialect.model(root)))
            assert error is None
            assert Part.Text("hi") in items
        asyncio.run(provider.aclose())


@EACH
@scenario
async def test_aclose_cuts_a_sample_in_flight_as_closed(dialect: Dialect) -> None:
    server = KeepAliveServer(dialect.reply("hi"), stall=len(dialect.stalled))
    with server.running() as root:
        provider = dialect.make(None, ENVIRON)
        stream = provider.sample(hello_walk(), dialect.model(root), [])
        assert await anext(stream) == TextDelta("hi")
        rest = asyncio.ensure_future(collect_rest(stream))
        await asyncio.sleep(0.05)
        await provider.aclose()
        parts, error = await asyncio.wait_for(rest, 5)
    assert parts == [Part.Text("hi")], "the text that arrived is still yielded"
    assert isinstance(error, ProviderClosed)


# The key


@EACH
@scenario
async def test_the_key_is_read_at_sample_time_and_goes_in_its_header_only(
    dialect: Dialect,
) -> None:
    server = sse_text(dialect.reply("hi"))
    environ = {KEY_ENV: SECRET}
    provider = dialect.make(server.transport, environ)
    for key in (SECRET, "rotated"):
        environ[KEY_ENV] = key
        _, error = await sample_all(provider, dialect.model())
        assert error is None
        request = server.requests[-1]
        assert request.headers[dialect.key_header].endswith(key)
        assert key not in str(request.url)
    del environ[KEY_ENV]
    sent = len(server.requests)
    _, error = await sample_all(provider, dialect.model())
    assert error is not None
    assert KEY_ENV in str(error)
    assert len(server.requests) == sent, "nothing is sent without the key"


@EACH
@scenario
async def test_an_http_error_never_holds_the_key(dialect: Dialect) -> None:
    server = Server(httpx.Response(400, json={"error": {"code": 400, "message": "bad request"}}))
    _, error = await sample_all(dialect.over(server), dialect.model())
    assert error is not None
    assert SECRET not in str(error)
    assert SECRET not in str(server.requests[0].url)


@pytest.mark.parametrize(
    "key", ["sk-secret-0123\nrest", "sk-secret-0123\u201cquoted\u201d"], ids=["newline", "quote"]
)
@scenario
async def test_a_key_http_cannot_carry_fails_the_sample_without_quoting_it(key: str) -> None:
    dialect = DIALECTS["anthropic"]
    server = KeepAliveServer(dialect.reply("hi"))
    with server.running() as root:
        provider = dialect.make(None, {KEY_ENV: key})
        items, error = await sample_all(provider, dialect.model(root))
        await provider.aclose()
    assert items == []
    assert error is not None
    assert KEY_ENV in str(error)  # refused by `ModelConfig.api_key`, before any header
    assert "sk-secret" not in str(error)
    assert "sk-secret" not in repr(error.__cause__)


@EACH
@scenario
async def test_builtin_providers_send_the_key_from_their_environ(dialect: Dialect) -> None:
    server = KeepAliveServer(dialect.reply("hi"))
    providers = builtin_providers({KEY_ENV: SECRET})
    with server.running() as root:
        _, error = await sample_all(providers[dialect.name], dialect.model(root))
    for provider in providers.values():
        await provider.aclose()
    assert error is None
    assert (server.header(dialect.key_header) or "").endswith(SECRET)


# What goes wrong on the wire


@EACH
@pytest.mark.parametrize("trouble", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
@scenario
async def test_a_transport_error_is_a_sample_error_caused_by_it_and_not_a_closing(
    dialect: Dialect, trouble: httpx.TransportError
) -> None:
    server = Server(trouble)
    items, error = await sample_all(dialect.over(server), dialect.model())
    assert items == []
    assert type(error) is SampleError, "a timeout is trouble with one sample, not a closed provider"
    assert (error.status, error.retry_after) == (None, None)
    assert type(trouble).__name__ in str(error)
    assert error.__cause__ is trouble


@EACH
@scenario
async def test_a_nan_or_infinity_in_a_stream_event_is_a_sample_error(dialect: Dialect) -> None:
    for constant in ("NaN", "Infinity", "-Infinity"):
        provider = dialect.serving(f'data: {{"usage": {{"x": {constant}}}}}\n\n')
        items, error = await sample_all(provider, dialect.model())
        assert items == []
        assert error is not None
        assert "malformed SSE data" in str(error)


@EACH
@scenario
async def test_a_lone_surrogate_in_a_stream_event_is_a_sample_error(dialect: Dialect) -> None:
    items, error = await sample_all(dialect.serving('data: {"x": "\\ud800"}\n\n'), dialect.model())
    assert items == []
    assert error is not None
    assert "surrogate" in str(error)


@EACH
@scenario
async def test_a_paired_surrogate_escape_is_text_not_an_error(dialect: Dialect) -> None:
    provider = dialect.serving(dialect.reply("hi").replace("hi", "\\ud83d\\ude00"))
    items, error = await sample_all(provider, dialect.model())
    assert error is None
    assert Part.Text("\U0001f600") in items


@scenario
async def test_a_non_finite_number_in_the_request_is_a_sample_error() -> None:
    dialect = DIALECTS["openai"]
    config = dialect.model(extra_body={"x": float("nan")})
    items, error = await sample_all(dialect.serving(dialect.reply("hi")), config)
    assert items == []
    assert error is not None
    assert "can't carry" in str(error)


# What follows a finish


def after_the_finish(dialect: Dialect) -> dict[str, Server]:
    """The reply `dialect` says, with each kind of trouble after its finish."""
    whole = dialect.reply("hi")
    return {
        "nothing more": sse_text(whole),
        "late content": sse_text(whole + dialect.late),
        "an error event": sse_text(whole + dialect.trouble),
        "malformed data": sse_text(whole + "data: {not json\n\n"),
        "a chunk that is no object": sse_text(whole + "data: [1]\n\n"),
        "a connection reset": cut(whole),
    }


@EACH
@pytest.mark.parametrize("kind", list(after_the_finish(DIALECTS["openai"])))
@scenario
async def test_trouble_after_the_finish_costs_only_the_meta_not_the_reply(
    dialect: Dialect, kind: str
) -> None:
    server = after_the_finish(dialect)[kind]
    items, error = await collect(dialect.over(server), hello_walk())
    assert error is None
    assert items[:-1] == [Part.Text("hi")]
    assert isinstance(items[-1], Finish)
    assert items[-1].stop == Stop.End


# A cancel, under the driver


@EACH
@scenario
async def test_a_cancel_mid_thinking_leaves_an_interrupted_reply_holding_unsigned_thinking(
    dialect: Dialect, tmp_path: Path
) -> None:
    server, _ = gated(dialect.thinking_head)
    async with running(tmp_path / "s", dialect.over(server)) as (session, driver, cursor):
        seen = driver.events(cursor)
        driver.send(cursor, "test", "hi")
        async for item in seen:
            if isinstance(item, Live) and item.delta == ThinkingDelta("think"):
                break
        driver.abort(cursor)
        await driver.rest(cursor)
        reply = bodies(session, cursor)[-1]
        await seen.aclose()
    assert isinstance(reply, Body.Assistant)
    assert reply.stop == Stop.Interrupted
    assert reply.parts == [Part.Thinking("let me think")], "what arrived, with no replay data"


# A plain turn, under the driver


@EACH
@scenario
async def test_a_plain_turn_logs_the_model_name_and_neither_the_key_nor_the_url(
    dialect: Dialect, tmp_path: Path
) -> None:
    server = sse_text(dialect.reply("hi"))
    directory = tmp_path / "s"
    async with running(directory, dialect.over(server)) as (session, driver, cursor):
        driver.send(cursor, "test", "hello")
        await driver.rest(cursor)
        walk = bodies(session, cursor)
        assert walk[0] == Body.Config(model_config(ALIAS))
        assert walk[-2] == Body.User("hello")
        assert isinstance(walk[-1], Body.Assistant)
        assert (walk[-1].parts, walk[-1].stop) == ([Part.Text("hi")], Stop.End)
    assert "tools" not in server.bodies[0], "no tools offered, no tools key"
    for file in directory.rglob("*"):
        if file.is_file():
            data = file.read_bytes()
            for leak in (SECRET, UNUSED, "unused.test", "api_key", "base_url", KEY_ENV):
                assert leak.encode() not in data, f"{leak!r} is in {file}"
