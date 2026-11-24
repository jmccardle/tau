"""How each provider sends a tool result's images (D30 section 7), against the request body it
posts to a local server: no paid call. The result is mixed, text then an image, as `read` and a
function tool return it."""

import base64
from typing import Any

from fakes import FakeTool
from harness import scenario
from pngs import png
from wire import ANTHROPIC, GEMINI, OPENAI, posted, sent, walk_of

from tau import Author, Body, Image, Outcome, Part, Stop

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
