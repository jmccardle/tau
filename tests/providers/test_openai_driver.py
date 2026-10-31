"""The OpenAI provider under the driver: what a tool turn and a reasoning turn put on the wire
and in the log. (A plain turn, a cancel, a failure: `test_connection.py`, `test_events.py`.)"""

import json
import os
from pathlib import Path
from typing import Annotated

import pytest
from harness import scenario
from wire import (
    ALIAS,
    OPENAI,
    bodies,
    catalog,
    running,
    sent,
    sse,
    sse_sequence,
)

from tau_next import (
    Body,
    Driver,
    FunctionTool,
    LocalClient,
    Part,
    Session,
    StepLimit,
    Stop,
    SystemClock,
    model_config,
    tool,
)
from tau_next.providers.openai import OpenAIProvider

# What the recorded `plain` stream says about itself; it carried no usage.
PLAIN_META = json.dumps(
    {
        "model": OPENAI.model_id,
        "id": "chatcmpl-PDYpQXnYl6gO6gM1pARqC6R7yqmMGiDs",
        "system_fingerprint": "b1637-9c7a7553",
    }
)


@scenario
async def test_reasoning_is_kept_in_the_reply_and_not_sent_back(tmp_path: Path) -> None:
    server = sse("reasoning")
    async with running(tmp_path / "s", OPENAI.over(server)) as (s, driver, c):
        await LocalClient(driver, "test").send(c, "17 * 3?")
        await driver.rest(c)
        reply = bodies(s, c)[-1]
        assert isinstance(reply, Body.Assistant)
        assert [type(p) for p in reply.parts] == [Part.Thinking, Part.Text]
        await LocalClient(driver, "test").send(c, "again?")
        await driver.rest(c)
    roles = [m["role"] for m in server.bodies[1]["messages"]]  # type: ignore[index]
    assert roles == ["system", "user", "assistant", "user"]
    assert "Need compute" not in str(server.bodies[1])


@pytest.mark.skipif("TAU_LIVE_BASE_URL" not in os.environ, reason="set TAU_LIVE_BASE_URL")
@scenario
async def test_live_a_plain_turn(tmp_path: Path) -> None:
    """Against a real server: `TAU_LIVE_BASE_URL` and `TAU_LIVE_MODEL` (the id from /v1/models)."""
    model = os.environ.get("TAU_LIVE_MODEL")
    assert model, "TAU_LIVE_MODEL must name the model when TAU_LIVE_BASE_URL is set"
    live = catalog(ALIAS, model=model, base_url=os.environ["TAU_LIVE_BASE_URL"])
    async with running(tmp_path / "s", OpenAIProvider(), live) as (s, driver, c):
        await LocalClient(driver, "test").send(c, "Say hello in five words or fewer.")
        await driver.rest(c)
        reply = bodies(s, c)[-1]
        assert isinstance(reply, Body.Assistant)
        assert reply.stop == Stop.End
        assert reply.finish_reason == "stop"
        assert any(isinstance(p, Part.Text) and p.text.strip() for p in reply.parts)


def get_weather_tool() -> FunctionTool:
    @tool
    def get_weather(city: Annotated[str, "city name"]) -> str:
        """Current weather in a city."""
        return f"sunny in {city}"

    return get_weather


@scenario
async def test_a_tool_round_trip_renders_the_tools_array_and_the_tool_message(
    tmp_path: Path,
) -> None:
    """The recorded `tool_call` response, run, and the follow-up request that answers it."""
    server = sse_sequence("tool_call", "plain")
    weather = get_weather_tool()
    async with running(tmp_path / "s", OPENAI.over(server), tools=[weather]) as (
        session,
        driver,
        c,
    ):
        await LocalClient(driver, "test").send(c, "weather in Paris?")
        await driver.rest(c)
        first, second = server.bodies
        expected_tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Current weather in a city.",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string", "description": "city name"}},
                        "additionalProperties": False,
                        "required": ["city"],
                    },
                },
            }
        ]
        assert first["tools"] == expected_tools
        assert second["tools"] == expected_tools
        assert "tool_choice" not in first
        assert "parallel_tool_calls" not in first
        assert sent(second)[-2:] == [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "y23a03Z82TN0yjGdHOY18qoUF5gVGfNV",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "y23a03Z82TN0yjGdHOY18qoUF5gVGfNV",
                "content": "sunny in Paris",
            },
        ]
        walk = bodies(session, c)
        assert isinstance(walk[-3], Body.Assistant)
        assert walk[-3].finish_reason == "tool_calls"
        assert walk[-1] == Body.Assistant(
            [Part.Text("Hello, how are you?")], Stop.End, "stop", PLAIN_META
        )
        for body in walk:
            if isinstance(body, Body.Config | Body.System | Body.User | Body.Custom):
                assert "get_weather" not in repr(body), "tools are not recorded"


@pytest.mark.skipif("TAU_LIVE_BASE_URL" not in os.environ, reason="set TAU_LIVE_BASE_URL")
@scenario
async def test_live_a_tool_round_trip(tmp_path: Path) -> None:
    """Against a real server (see `test_live_a_plain_turn`): the model reads a secret by tool."""
    model = os.environ.get("TAU_LIVE_MODEL")
    assert model, "TAU_LIVE_MODEL must name the model when TAU_LIVE_BASE_URL is set"
    secret = tmp_path / "secret.txt"
    secret.write_text("the phrase is: violet-anchor-42\n")

    @tool
    def read_file(path: Annotated[str, "path of the file to read"]) -> str:
        """Read a text file and return its contents."""
        return Path(path).read_text()

    live = catalog(ALIAS, model=model, base_url=os.environ["TAU_LIVE_BASE_URL"])
    session = Session.create(
        model_config(ALIAS, {"step_limit": {"limit": 6}}),
        "Use tools when needed.",
        SystemClock().now(),
        tmp_path / "s",
    )
    # StepLimit bounds the loop of sample, tools, sample: a model that keeps asking stops.
    driver = Driver(
        session,
        {"openai": OpenAIProvider()},
        live,
        tools=[read_file],
        extensions=[StepLimit()],
    )
    driver.start()
    c = session.cursors()[0]
    try:
        await LocalClient(driver, "test").send(c, f"Read {secret} and tell me the phrase in it.")
        await driver.rest(c)
        walk = bodies(session, c)
        assert any(isinstance(b, Body.Results) for b in walk)
        reply = walk[-1]
        assert isinstance(reply, Body.Assistant)
        assert any(isinstance(p, Part.Text) and "violet-anchor-42" in p.text for p in reply.parts)
    finally:
        await driver.stop()
        session.close()
