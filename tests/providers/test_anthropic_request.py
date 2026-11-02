"""The Anthropic provider's request side: the walk as `system` and `messages`, the options, the
settings `check` demands, and `tau config check`. Walks come from Harness sessions."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeProvider, FakeTool, Reply
from harness import Harness, scenario
from wire import (
    ANTHROPIC,
    ANTHROPIC_MODEL,
    GEMINI,
    KEY_ENV,
    SECRET,
    SIGNATURE,
    collect,
    fresh_walk,
    hello_walk,
    message,
    recorded,
    sent_request,
    text,
    thinking,
    walk_of,
)
from world import World, tau

from tau import AnthropicProvider, Body, ModelConfig, Node, Part, Replay, Stop

RESOLVED = ANTHROPIC.model(None)  # the provider's own URL
SIGNED = Replay("anthropic", json.dumps({"type": "thinking", "signature": SIGNATURE}))
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
    return (await sent_request(ANTHROPIC, walk, model, tools))[2]


def with_options(**options: object) -> ModelConfig:
    return replace(RESOLVED, options=options)


@scenario
async def test_system_is_top_level_and_the_request_names_the_endpoint_and_key(
    tmp_path: Path,
) -> None:
    h, walk = await walk_after(tmp_path, [Reply([Part.Text("hi")])], "hello")
    url, headers, body = await sent_request(ANTHROPIC, walk[:-1], RESOLVED)
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == SECRET
    assert headers["anthropic-version"] == "2023-06-01"
    assert body == {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4096,
        "stream": True,
        "system": [{"type": "text", "text": "be brief", "cache_control": CACHE}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello", "cache_control": CACHE}]}
        ],
    }
    proxy = replace(RESOLVED, base_url="http://proxy.test:8080/")
    assert (await sent_request(ANTHROPIC, walk, proxy))[0] == "http://proxy.test:8080/v1/messages"
    await h.close()


@scenario
async def test_foreign_and_unsigned_thinking_is_not_sent_and_signed_thinking_is() -> None:
    """A reply Gemini made (thinking with no signature, text signed for Gemini) and a reply
    Claude made (signed thinking), both read from the providers' own streams."""
    gemini, _ = await collect(GEMINI.serving(recorded("gemini_text_thinking")), hello_walk())
    claude, _ = await collect(
        ANTHROPIC.serving(message(thinking(0, ["mine"]), text(1, ["answer"]))), fresh_walk()
    )
    foreign = [p for p in gemini if isinstance(p, Part)]
    mine = [p for p in claude if isinstance(p, Part)]
    assert any(isinstance(p, Part.Thinking) for p in foreign)
    walk = walk_of(
        Body.User("q1"),
        Body.Assistant(foreign, Stop.End, None),
        Body.User("q2"),
        Body.Assistant(mine, Stop.End, None),
        Body.User("q3"),
    )
    messages = (await request(walk, with_options(prompt_caching=False)))["messages"]
    foreign_text = next(p for p in foreign if isinstance(p, Part.Text))
    assert messages[1] == {  # type: ignore[index]
        "role": "assistant",
        "content": [{"type": "text", "text": foreign_text.text}],
    }
    assert messages[3] == {  # type: ignore[index]
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "mine", "signature": SIGNATURE},
            {"type": "text", "text": "answer"},
        ],
    }


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
async def test_options_reach_the_request_with_extra_body_beside_them(tmp_path: Path) -> None:
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
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "high"}
    assert (body["temperature"], body["top_p"], body["top_k"]) == (0.5, 0.9, 40)
    assert body["metadata"] == {"user_id": "u1"}
    await h.close()


# check


def test_check_demands_a_key_and_max_tokens_and_a_root_base_url() -> None:
    anthropic = AnthropicProvider(environ={})
    assert anthropic.check(RESOLVED) == []
    (no_max,) = anthropic.check(replace(RESOLVED, max_tokens=None))
    assert "max_tokens" in no_max
    (no_key,) = anthropic.check(replace(RESOLVED, api_key_env=None))
    assert "API key" in no_key
    (versioned,) = anthropic.check(replace(RESOLVED, base_url="http://p.test/v1"))
    assert "/v1/messages" in versioned


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
    for mode in ({"type": "adaptive"}, {"type": "disabled"}, {"type": "between_tools"}):
        assert anthropic.check(with_options(thinking=mode)) == []


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"seed": 1}, "unknown option 'seed'"),
        ({"temperature": "hot"}, "'temperature' must be a number"),
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
        ({"extra_body": {"max_tokens": 9}}, "may not set 'max_tokens'"),
        ({"extra_body": {"top_k": 3}, "top_k": 4}, "already set"),
        ({"extra_body": {"output_config": {}}, "effort": "low"}, "already set"),
        ({"extra_body": {"x": float("nan")}}, "not JSON"),
    ],
)
def test_check_refuses_anything_else(options: dict[str, object], message: str) -> None:
    problems = AnthropicProvider(environ={}).check(with_options(**options))
    assert any(message in p for p in problems), problems


def test_tau_config_check_names_a_missing_max_tokens(world: World) -> None:
    settings = {"provider": "anthropic", "model": ANTHROPIC_MODEL, "api_key_env": KEY_ENV}
    world.user_config({"models": {"claude": settings}})
    result = tau(world, "config", "check")
    assert result.code == 1
    assert "max_tokens" in result.out
    world.user_config({"models": {"claude": {**settings, "max_tokens": 4096}}})
    assert tau(world, "config", "check").code == 0
