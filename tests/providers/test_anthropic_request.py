"""The Anthropic provider's request side: the walk as `system` and `messages`, the options, the
settings `check` demands, and `tau config check`. Walks come from Harness sessions."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from anthropic_streams import (
    KEY_ENV,
    MODEL,
    RESOLVED,
    SECRET,
    SIGNATURE,
    message,
    provider,
    running,
    sent_request,
    stream,
    text,
)
from fakes import FakeProvider, FakeTool, Reply
from harness import Harness, scenario
from world import World, tau

from tau_next import ModelConfig, Node, Part, Replay, SampleError, Stop
from tau_next.providers.anthropic import AnthropicProvider

SIGNED = Replay("anthropic", json.dumps({"type": "thinking", "signature": SIGNATURE}))
FOREIGN = Replay("gemini", json.dumps({"thoughtSignature": "abc"}))
CACHE = {"type": "ephemeral"}


async def walk_after(
    tmp_path: Path, script: list[Reply], *inputs: str
) -> tuple[Harness, list[Node]]:
    """A session that has run `script` for each input, and its walk."""
    h = await Harness.create(
        tmp_path, system="be brief", provider=FakeProvider(script), tools=[FakeTool("ta", "A")]
    )
    for line in inputs:
        await h.send(line)
        await h.settle()
    return h, h.session.tree.walk(h.session.cursor(h.cursor).at)


async def request(
    walk: list[Node], model: ModelConfig = RESOLVED, tools: tuple[()] = ()
) -> dict[str, object]:
    return (await sent_request(walk, model, tools))[2]


def with_options(**options: object) -> ModelConfig:
    return replace(RESOLVED, options=options)


@scenario
async def test_system_is_top_level_and_the_request_names_the_endpoint_and_key(
    tmp_path: Path,
) -> None:
    h, walk = await walk_after(tmp_path, [Reply([Part.Text("hi")])], "hello")
    url, headers, body = await sent_request(walk[:-1])
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == SECRET
    assert headers["anthropic-version"] == "2023-06-01"
    assert body == {
        "model": MODEL,
        "max_tokens": 4096,
        "stream": True,
        "system": [{"type": "text", "text": "be brief", "cache_control": CACHE}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello", "cache_control": CACHE}]}
        ],
    }
    proxy = replace(RESOLVED, base_url="http://proxy.test:8080/")
    assert (await sent_request(walk, proxy))[0] == "http://proxy.test:8080/v1/messages"
    await h.close()


@scenario
async def test_the_key_is_read_at_sample_time_and_a_missing_one_fails_before_any_request(
    tmp_path: Path,
) -> None:
    h, walk = await walk_after(tmp_path, [Reply([Part.Text("hi")])], "hello")
    with pytest.raises(SampleError, match=KEY_ENV):
        await sent_request(walk, environ={})
    await h.close()


@scenario
async def test_foreign_and_unsigned_thinking_is_not_sent_and_signed_thinking_is(
    tmp_path: Path,
) -> None:
    script = [
        Reply(
            [
                Part.Thinking("from gemini", FOREIGN),
                Part.Thinking("cut off"),
                Part.Thinking("mine", SIGNED),
                Part.Text("answer"),
            ]
        ),
        Reply([Part.Text("next")]),
    ]
    h, walk = await walk_after(tmp_path, script, "q1", "q2")
    body = await request(walk[:-1], with_options(prompt_caching=False))
    assert body["messages"][1] == {  # type: ignore[index]
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "mine", "signature": SIGNATURE},
            {"type": "text", "text": "answer"},
        ],
    }
    await h.close()


@scenario
async def test_a_reply_with_nothing_to_send_is_left_out_and_its_neighbours_merge(
    tmp_path: Path,
) -> None:
    h, walk = await walk_after(
        tmp_path, [Reply([Part.Thinking("cut off")]), Reply([Part.Text("x")])], "first", "second"
    )
    body = await request(walk[:-1], with_options(prompt_caching=False))
    assert body["messages"] == [  # type: ignore[comparison-overlap]
        {
            "role": "user",
            "content": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
        }
    ]
    await h.close()


@scenario
async def test_a_call_that_cannot_run_is_text_with_the_stop_note(tmp_path: Path) -> None:
    script = [Reply([Part.Text("looking"), Part.Call("a", "ta", '{"p"')], Stop.Length)]
    h, walk = await walk_after(tmp_path, script, "look")
    body = await request(walk, with_options(prompt_caching=False))
    assert body["messages"][1] == {  # type: ignore[index]
        "role": "assistant",
        "content": [
            {"type": "text", "text": "looking"},
            {"type": "text", "text": '[tool call a: ta {"p"]'},
            {"type": "text", "text": "[reply stopped: length]"},
        ],
    }
    await h.close()


@scenario
async def test_prompt_caching_marks_the_system_text_and_the_last_message_only(
    tmp_path: Path,
) -> None:
    h, walk = await walk_after(
        tmp_path, [Reply([Part.Text("a")]), Reply([Part.Text("b")])], "q1", "q2"
    )
    walk = walk[:-1]
    body = await request(walk)
    assert body["system"] == [{"type": "text", "text": "be brief", "cache_control": CACHE}]
    marks = [
        "cache_control" in block
        for m in body["messages"]  # type: ignore[attr-defined]
        for block in m["content"]
    ]
    assert marks == [False, False, True]
    off = await request(walk, with_options(prompt_caching=False))
    assert off["system"] == "be brief"
    assert "cache_control" not in json.dumps(off["messages"])
    await h.close()


@scenario
async def test_options_reach_the_request_and_extra_body_goes_last(tmp_path: Path) -> None:
    h, walk = await walk_after(tmp_path, [Reply([Part.Text("a")])], "q")
    options = {
        "thinking": {"type": "adaptive", "display": "summarized"},
        "effort": "high",
        "temperature": 0.5,
        "top_p": 0.9,
        "top_k": 40,
        "extra_body": {"metadata": {"user_id": "u1"}},
    }
    body = await request(walk[:-1], with_options(**options))
    assert list(body)[-6:] == [
        "thinking",
        "output_config",
        "temperature",
        "top_p",
        "top_k",
        "metadata",
    ]
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "high"}
    assert body["metadata"] == {"user_id": "u1"}
    await h.close()


# check


def test_check_demands_a_key_and_max_tokens_and_a_root_base_url() -> None:
    anthropic = AnthropicProvider(environ={})
    assert anthropic.check(RESOLVED) == []
    assert anthropic.check(replace(RESOLVED, max_tokens=None)) == [
        "needs max_tokens: the Messages API requires it and there is no default"
    ]
    assert "needs an API key" in anthropic.check(replace(RESOLVED, api_key_env=None))[0]
    assert (
        "/v1/messages itself" in anthropic.check(replace(RESOLVED, base_url="http://p.test/v1"))[0]
    )


def test_check_leaves_the_values_to_the_api() -> None:
    anthropic = AnthropicProvider(environ={})
    options = {"effort": "extreme", "thinking": {"type": "on", "display": "full"}}
    assert anthropic.check(with_options(**options)) == []


def test_check_accepts_the_listed_options() -> None:
    anthropic = AnthropicProvider(environ={})
    options: dict[str, object] = {
        "thinking": {"type": "enabled", "budget_tokens": 2048, "display": "omitted"},
        "effort": "xhigh",
        "temperature": 1,
        "top_p": 0.99,
        "top_k": 5,
        "prompt_caching": False,
        "extra_body": {"service_tier": "auto"},
    }
    assert anthropic.check(with_options(**options)) == []
    for thinking in ({"type": "adaptive"}, {"type": "disabled"}, {"type": "between_tools"}):
        assert anthropic.check(with_options(thinking=thinking)) == []


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"seed": 1}, "unknown option 'seed'"),
        ({"temperature": "hot"}, "'temperature' must be a number"),
        ({"top_p": True}, "'top_p' must be a number"),
        ({"top_k": 1.5}, "'top_k' must be a non-negative integer"),
        ({"effort": 3}, "'effort' must be a non-empty string"),
        ({"prompt_caching": "yes"}, "'prompt_caching' must be true or false"),
        ({"thinking": "adaptive"}, "'thinking' must be an object"),
        ({"thinking": {"type": 3}}, "needs a 'type'"),
        ({"thinking": {"type": "enabled"}}, "needs budget_tokens >= 1024"),
        ({"thinking": {"type": "enabled", "budget_tokens": 100}}, "needs budget_tokens >= 1024"),
        ({"thinking": {"type": "enabled", "budget_tokens": 4096}}, "less than max_tokens"),
        ({"thinking": {"type": "adaptive", "budget_tokens": "lots"}}, "budget_tokens must be"),
        ({"thinking": {"type": "adaptive", "display": 3}}, "display must be a string"),
        ({"thinking": {"type": "adaptive", "display": "updates"}}, "needs a beta header"),
        ({"thinking": {"type": "adaptive", "block_binding": {}}}, "block_binding needs a beta"),
        ({"extra_body": {"thinking": {"type": "adaptive"}}}, "may not set 'thinking'"),
        ({"extra_body": {"context_management": {}}}, "needs a beta header"),
        ({"extra_body": []}, "'extra_body' must be an object"),
        ({"extra_body": {"messages": []}}, "may not set 'messages'"),
        ({"extra_body": {"max_tokens": 9}}, "may not set 'max_tokens'"),
        ({"extra_body": {"top_k": 3}, "top_k": 4}, "already set"),
        ({"extra_body": {"output_config": {}}, "effort": "low"}, "already set"),
        ({"extra_body": {"x": float("nan")}}, "not JSON"),
    ],
)
def test_check_refuses_anything_else(options: dict[str, object], message: str) -> None:
    problems = AnthropicProvider(environ={}).check(with_options(**options))
    assert any(message in p for p in problems), problems


@scenario
async def test_a_driver_refuses_a_bad_option_before_any_request(tmp_path: Path) -> None:
    server = stream(message(text(0, ["x"])))
    async with running(tmp_path / "s", provider(server), options={"seed": 1}) as (_, driver, c):
        driver.send(c, "test", "hi")
        await driver.rest(c)
    assert server.requests == []


def test_tau_config_check_names_a_missing_max_tokens(world: World) -> None:
    settings = {"provider": "anthropic", "model": MODEL, "api_key_env": KEY_ENV}
    world.user_config({"models": {"claude": settings}})
    result = tau(world, "config", "check")
    assert result.code == 1
    assert "max_tokens" in result.out
    world.user_config({"models": {"claude": {**settings, "max_tokens": 4096}}})
    assert tau(world, "config", "check").code == 0
