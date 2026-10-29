"""The OpenAI provider under the driver: a session on disk, a turn, and what the log holds."""

import os
from pathlib import Path

import httpx
import pytest
from harness import scenario
from transcripts import (
    BASE_URL,
    MODEL,
    CutStream,
    Server,
    chunk,
    recorded_error,
    running,
    sse,
)

from tau_next import Body, ModelConfig, Node, Part, Session, Stop
from tau_next.providers.openai import OpenAIProvider

KEY_ENV = "TAU_TEST_KEY"
SECRET = "sk-test-0123456789"


def bodies(session: Session, cursor: int) -> list[Body]:
    return [n.body for n in session.walk(session.at(cursor))]


@scenario
async def test_a_plain_turn_through_the_driver(tmp_path: Path) -> None:
    server = sse("plain")
    async with running(tmp_path / "s", OpenAIProvider(server.transport, {})) as (s, driver, c):
        driver.send(c, "test", "hello")
        await driver.rest(c)
        assert bodies(s, c)[2:] == [
            Body.User("hello"),
            Body.Assistant([Part.Text("Hello, how are you?")], Stop.End),
        ]
    assert server.bodies[0]["messages"][-1] == {"role": "user", "content": "hello"}  # type: ignore[index]


@scenario
async def test_reasoning_is_kept_in_the_reply_and_not_sent_back(tmp_path: Path) -> None:
    server = sse("reasoning")
    async with running(tmp_path / "s", OpenAIProvider(server.transport, {})) as (s, driver, c):
        driver.send(c, "test", "17 * 3?")
        await driver.rest(c)
        reply = bodies(s, c)[-1]
        assert isinstance(reply, Body.Assistant)
        assert [type(p) for p in reply.parts] == [Part.Thinking, Part.Text]
        driver.send(c, "test", "again?")
        await driver.rest(c)
    roles = [m["role"] for m in server.bodies[1]["messages"]]  # type: ignore[index]
    assert roles == ["system", "user", "assistant", "user"]
    assert "Need compute" not in str(server.bodies[1])


@scenario
async def test_an_http_error_fails_the_step_and_records_a_sample_error(tmp_path: Path) -> None:
    async with running(tmp_path / "s", OpenAIProvider(recorded_error().transport, {})) as (
        s,
        driver,
        c,
    ):
        driver.send(c, "test", "hello")
        await driver.rest(c)
        assert not s.auto(c), "a failed step turns auto-step off"
        records = [n for n in nodes(s) if isinstance(n.body, Body.Custom)]
        assert [r.body.kind for r in records if isinstance(r.body, Body.Custom)] == ["sample_error"]
        assert "HTTP 500" in custom_data(records[0])


@scenario
async def test_a_stream_cut_mid_reply_commits_the_partial_reply_as_an_error(
    tmp_path: Path,
) -> None:
    head = (chunk({"content": "par"})).encode()
    server = Server(httpx.Response(200, stream=CutStream(head)))
    async with running(tmp_path / "s", OpenAIProvider(server.transport, {})) as (s, driver, c):
        driver.send(c, "test", "hello")
        await driver.rest(c)
        assert bodies(s, c)[-1] == Body.Assistant([Part.Text("par")], Stop.Error)


@scenario
async def test_the_api_key_is_sent_but_never_stored(tmp_path: Path) -> None:
    config = ModelConfig(MODEL, BASE_URL, KEY_ENV).to_config()
    server = sse("plain")
    directory = tmp_path / "s"
    provider = OpenAIProvider(server.transport, {KEY_ENV: SECRET})
    async with running(directory, provider, config) as (s, driver, c):
        driver.send(c, "test", "hello")
        await driver.rest(c)
        assert bodies(s, c)[-1] == Body.Assistant([Part.Text("Hello, how are you?")], Stop.End)
    assert server.requests[0].headers["authorization"] == f"Bearer {SECRET}"
    assert KEY_ENV in repr(config), "the variable's name is in the Config"
    for file in directory.rglob("*"):
        if file.is_file():
            assert SECRET.encode() not in file.read_bytes(), f"the key is in {file}"
    loaded = Session.load(directory, 0)
    try:
        for node in nodes(loaded):
            assert SECRET not in repr(node)
    finally:
        loaded.close()


@scenario
async def test_an_unset_key_variable_fails_the_sample_before_any_request(
    tmp_path: Path,
) -> None:
    config = ModelConfig(MODEL, BASE_URL, KEY_ENV).to_config()
    server = sse("plain")
    async with running(tmp_path / "s", OpenAIProvider(server.transport, {}), config) as (
        s,
        driver,
        c,
    ):
        driver.send(c, "test", "hello")
        await driver.rest(c)
        records = [n for n in nodes(s) if isinstance(n.body, Body.Custom)]
        assert KEY_ENV in custom_data(records[0])
    assert server.requests == []


def nodes(session: Session) -> list[Node]:
    out: list[Node] = []
    while True:
        try:
            out.append(session.node(len(out)))
        except LookupError:
            return out


def custom_data(node: Node) -> str:
    assert isinstance(node.body, Body.Custom)
    return node.body.data


@pytest.mark.skipif("TAU_LIVE_BASE_URL" not in os.environ, reason="set TAU_LIVE_BASE_URL")
@scenario
async def test_live_a_plain_turn(tmp_path: Path) -> None:
    """Against a real server: `TAU_LIVE_BASE_URL` and `TAU_LIVE_MODEL` (the id from /v1/models)."""
    model = os.environ.get("TAU_LIVE_MODEL")
    assert model, "TAU_LIVE_MODEL must name the model when TAU_LIVE_BASE_URL is set"
    config = ModelConfig(model, os.environ["TAU_LIVE_BASE_URL"]).to_config()
    async with running(tmp_path / "s", OpenAIProvider(), config) as (s, driver, c):
        driver.send(c, "test", "Say hello in five words or fewer.")
        await driver.rest(c)
        reply = bodies(s, c)[-1]
        assert isinstance(reply, Body.Assistant)
        assert reply.stop == Stop.End
        assert any(isinstance(p, Part.Text) and p.text.strip() for p in reply.parts)
