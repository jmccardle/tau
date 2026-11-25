"""How each provider sends a tool result's images (D30 section 7), against the request body it
posts to a local server: no paid call. The result is mixed, text then an image, as `read` and a
function tool return it."""

import base64
from dataclasses import replace
from typing import Any

from fakes import FakeTool
from harness import scenario
from pngs import png
from wire import ANTHROPIC, GEMINI, OPENAI, SIGNATURE, posted, sent, walk_of

from tau import Author, Body, Image, Outcome, Part, Replay, Stop

PICTURE = png(3, 2)
B64 = base64.b64encode(PICTURE).decode("ascii")
TOOLS = [FakeTool("shot", "unused")]


def _walk(*outcomes: Outcome) -> list:
    calls = [Part.Call(f"c{i}", "shot", "{}") for i in range(1, len(outcomes) + 1)]
    results = [(c.id, o, Author.Tools()) for c, o in zip(calls, outcomes, strict=True)]
    return walk_of(
        Body.User("take a screenshot"),
        Body.Assistant(calls, Stop.Tools, None),
        Body.Results(results),
    )


def _body(server: object) -> Any:
    """The JSON body of the one request `server` was sent."""
    return getattr(server, "bodies")[0]  # noqa: B009


MIXED = Outcome.Ok("the screen at noon", [Image(PICTURE)])


@scenario
async def test_openai_keeps_the_tool_message_text_and_follows_it_with_a_user_message() -> None:
    """A tool message holds only text (OpenAI, llama.cpp, vLLM): the images go in one `user`
    message after the batch's tool messages, as `data:` URLs."""
    server = await posted(OPENAI, _walk(MIXED, Outcome.Ok("no image")), tools=TOOLS)
    messages = sent(server.bodies[0])
    assert messages[2:] == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "shot", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "shot", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "the screen at noon"},
        {"role": "tool", "tool_call_id": "c2", "content": "no image"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "[images from tool results: c1]"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}},
            ],
        },
    ]


@scenario
async def test_openai_a_result_with_no_batch_carries_its_image_in_the_user_message() -> None:
    walk = walk_of(Body.User("go"), Body.Results([("zz", MIXED, Author.Tools())]))
    messages = sent((await posted(OPENAI, walk)).bodies[0])
    assert messages[-1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "[tool result zz: the screen at noon]"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}},
        ],
    }


@scenario
async def test_anthropic_puts_the_text_and_the_image_inside_the_tool_result() -> None:
    server = await posted(ANTHROPIC, _walk(MIXED, Outcome.Err("broken")), tools=TOOLS)
    user = _body(server)["messages"][-1]
    assert user["role"] == "user"
    blocks = [{k: v for k, v in b.items() if k != "cache_control"} for b in user["content"]]
    assert blocks == [
        {
            "type": "tool_result",
            "tool_use_id": "c1",
            "content": [
                {"type": "text", "text": "the screen at noon"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": B64},
                },
            ],
            "is_error": False,
        },
        # Only an `Ok` holds images: an error result stays text (the API refuses images there).
        {"type": "tool_result", "tool_use_id": "c2", "content": "Error: broken", "is_error": True},
    ]


@scenario
async def test_anthropic_an_image_alone_sends_no_empty_text_block() -> None:
    server = await posted(ANTHROPIC, _walk(Outcome.Ok("", [Image(PICTURE)])), tools=TOOLS)
    result = _body(server)["messages"][-1]["content"][0]
    assert [b["type"] for b in result["content"]] == ["image"]


@scenario
async def test_gemini_follows_the_function_responses_with_inline_data() -> None:
    server = await posted(GEMINI, _walk(MIXED), tools=TOOLS)
    last = _body(server)["contents"][-1]
    assert last == {
        "role": "user",
        "parts": [
            {"functionResponse": {"name": "shot", "response": {"result": "the screen at noon"}}},
            {"inlineData": {"mimeType": "image/png", "data": B64}},
        ],
    }


# A user's images (D31): after the user's text, in the user's own message.

SEEN = Body.User("what is this?", [Image(PICTURE)])


@scenario
async def test_openai_a_user_message_is_its_text_then_its_images() -> None:
    messages = sent((await posted(OPENAI, walk_of(SEEN))).bodies[0])
    assert messages[-1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}},
        ],
    }
    alone = sent((await posted(OPENAI, walk_of(Body.User("", [Image(PICTURE)])))).bodies[0])
    assert alone[-1]["content"] == [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}}
    ], "an image alone sends no empty text"


@scenario
async def test_anthropic_a_user_message_is_its_text_then_its_images() -> None:
    def blocks(walk: list) -> list:
        return [{k: v for k, v in b.items() if k != "cache_control"} for b in walk]

    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": B64}}
    server = await posted(ANTHROPIC, walk_of(SEEN))
    user = _body(server)["messages"][-1]
    assert user["role"] == "user"
    assert blocks(user["content"]) == [{"type": "text", "text": "what is this?"}, image]
    alone = await posted(ANTHROPIC, walk_of(Body.User("  ", [Image(PICTURE)])))
    assert blocks(_body(alone)["messages"][-1]["content"]) == [image], "no blank text block"


@scenario
async def test_gemini_a_user_message_is_its_text_then_inline_data() -> None:
    server = await posted(GEMINI, walk_of(SEEN))
    assert _body(server)["contents"][-1] == {
        "role": "user",
        "parts": [
            {"text": "what is this?"},
            {"inlineData": {"mimeType": "image/png", "data": B64}},
        ],
    }
    alone = await posted(GEMINI, walk_of(Body.User("", [Image(PICTURE)])))
    assert _body(alone)["contents"][-1]["parts"] == [
        {"inlineData": {"mimeType": "image/png", "data": B64}}
    ]


@scenario
async def test_anthropic_an_image_alone_opens_a_new_loop_under_manual_thinking() -> None:
    """Under manual thinking the loop in progress starts after the last user message that says
    something; an image says something, so a loop it opened with signed thinking keeps its
    `tool_use`, though the loop before it opened without thinking (counted from there, the
    signed batch would go as text)."""
    signed = Replay("anthropic", '{"type": "thinking", "signature": "' + SIGNATURE + '"}')
    walk = walk_of(
        Body.User("go"),
        Body.Assistant([Part.Call("a", "shot", "{}")], Stop.Tools, None),
        Body.Results([("a", Outcome.Ok("A"), Author.Tools())]),
        Body.Assistant([Part.Text("done")], Stop.End, None),
        Body.User("", [Image(PICTURE)]),
        Body.Assistant([Part.Thinking("mine", signed), Part.Call("b", "shot", "{}")], Stop.Tools),
        Body.Results([("b", Outcome.Ok("B"), Author.Tools())]),
    )
    model = replace(
        ANTHROPIC.model(None),
        options={"prompt_caching": False, "thinking": {"type": "enabled", "budget_tokens": 2048}},
    )
    server = await posted(ANTHROPIC, walk, model, TOOLS)
    kinds = [[b["type"] for b in m["content"]] for m in _body(server)["messages"]]
    assert kinds[-3:] == [["image"], ["thinking", "tool_use"], ["tool_result"]]
