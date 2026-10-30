"""The request the Gemini provider builds, and what it sends back of a saved reply."""

import json
from pathlib import Path

import pytest
from fakes import FakeTool
from harness import scenario
from wire import (
    ANTHROPIC,
    ENVIRON,
    GEMINI,
    GEMINI_BARE,
    GEMINI_MODEL,
    KEY_ENV,
    call,
    collect,
    fresh_walk,
    gemini_chunk,
    hello_walk,
    message,
    recorded,
    running,
    sent_request,
    sse_texts,
    text,
    thinking,
    walk_of,
)

from tau_next import Author, Body, ModelConfig, Outcome, Part, Replay, SampleError, Stop
from tau_next.providers.gemini import GeminiProvider

DEFAULT_URL = "https://generativelanguage.googleapis.com/v1beta"


def model(**options: object) -> ModelConfig:
    return ModelConfig(GEMINI_MODEL, provider="gemini", api_key_env=KEY_ENV, options=options)


def replay(**data: object) -> Replay:
    return Replay("gemini", json.dumps(data))


async def request_body(*bodies: Body, system: str = "sys") -> dict[str, object]:
    walk = walk_of(*bodies, system=system)
    return (await sent_request(GEMINI, walk, GEMINI_BARE, [FakeTool("t", "x")]))[2]  # tools offered


def ok(call_id: str, value: str) -> tuple[str, Outcome, Author]:
    return (call_id, Outcome.Ok(value), Author.Tools())


@scenario
async def test_the_request_names_the_model_and_the_endpoint_and_the_options_make_the_config() -> (
    None
):
    config = ModelConfig(GEMINI_MODEL, provider="gemini", api_key_env=KEY_ENV, max_tokens=99)
    walk = walk_of(Body.User("hello"), system="be brief")
    url, _, body = await sent_request(GEMINI, walk, config)
    assert url == f"{DEFAULT_URL}/models/{GEMINI_MODEL}:streamGenerateContent?alt=sse"
    assert body == {
        "systemInstruction": {"parts": [{"text": "be brief"}]},
        "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
        "generationConfig": {"maxOutputTokens": 99},
    }
    proxy = ModelConfig(GEMINI_MODEL, "http://proxy.test:9/v1beta/", KEY_ENV, provider="gemini")
    url, _, _ = await sent_request(GEMINI, hello_walk(), proxy)
    assert url == f"http://proxy.test:9/v1beta/models/{GEMINI_MODEL}:streamGenerateContent?alt=sse"


@scenario
async def test_tools_are_one_declaration_list_with_their_json_schema_as_given() -> None:
    tool = FakeTool("read", "x", description="Read a file.")
    _, _, body = await sent_request(GEMINI, hello_walk(), GEMINI_BARE, [tool])
    assert body["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "read",
                    "description": "Read a file.",
                    "parametersJsonSchema": {"type": "object", "properties": {}},
                }
            ]
        }
    ]


@scenario
async def test_a_signature_on_a_call_round_trips_byte_identical(tmp_path: Path) -> None:
    """Recorded call (id and signature from the API) in, tool run, the next request sends the
    same `functionCall` part back with the signature, and the response with the same id."""
    server = sse_texts(recorded("gemini_call"), gemini_chunk([{"text": "done"}], "STOP"))
    async with running(tmp_path / "s", GEMINI.over(server), tools=[FakeTool("read", "hi")]) as (
        _,
        driver,
        cursor,
    ):
        driver.send(cursor, "test", "read it")
        await driver.rest(cursor)
    recorded_part = next(
        part
        for line in recorded("gemini_call").splitlines()
        if line.startswith("data:")
        for part in json.loads(line[5:])["candidates"][0]["content"]["parts"]
        if "functionCall" in part
    )
    model_turn, answer = server.bodies[1]["contents"][1:]  # type: ignore[index]
    assert model_turn == {"role": "model", "parts": [recorded_part]}
    assert recorded_part["thoughtSignature"].encode() in server.requests[1].content
    assert answer == {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "name": "read",
                    "response": {"result": "hi"},
                    "id": "call_698416",
                }
            }
        ],
    }


@scenario
async def test_a_signature_on_text_round_trips_and_unsigned_thinking_is_not_sent(
    tmp_path: Path,
) -> None:
    server = sse_texts(recorded("gemini_text_thinking"), gemini_chunk([{"text": "ok"}], "STOP"))
    async with running(tmp_path / "s", GEMINI.over(server)) as (session, driver, cursor):
        driver.send(cursor, "test", "tide pools")
        await driver.rest(cursor)
        saved = session.tree.node(session.cursor(cursor).at).body
        driver.send(cursor, "test", "more")
        await driver.rest(cursor)
    assert isinstance(saved, Body.Assistant)
    thinking_part, text_part = saved.parts
    assert isinstance(thinking_part, Part.Thinking)
    assert isinstance(text_part, Part.Text)
    assert text_part.replay is not None
    signature = json.loads(text_part.replay.data)["thoughtSignature"]
    contents = server.bodies[1]["contents"]
    assert contents[1] == {  # type: ignore[index]
        "role": "model",
        "parts": [{"text": text_part.text, "thoughtSignature": signature}],
    }
    assert thinking_part.text not in server.requests[1].content.decode()


@scenario
async def test_another_providers_signatures_and_ids_are_not_sent() -> None:
    """A reply Claude made (signed thinking, text, a call), read from its own stream."""
    stream = message(
        thinking(0, ["mine"]),
        text(1, ["t"]),
        call(2, "toolu_9", "read", ["{}"]),
        reason="tool_use",
    )
    items, _ = await collect(ANTHROPIC.serving(stream), fresh_walk())
    parts = [i for i in items if isinstance(i, Part)]
    body = await request_body(
        Body.User("go"), Body.Assistant(parts, Stop.Tools, None), Body.Results([ok("toolu_9", "v")])
    )
    assert body["contents"][1:] == [  # type: ignore[index]
        {
            "role": "model",
            "parts": [{"text": "t"}, {"functionCall": {"name": "read", "args": {}}}],
        },
        {
            "role": "user",
            "parts": [{"functionResponse": {"name": "read", "response": {"result": "v"}}}],
        },
    ]


@scenario
async def test_data_that_cannot_be_kept_as_replay_fails_the_sample_not_core() -> None:
    chunk = gemini_chunk([{"text": "x", "thoughtSignature": "\ud800"}], "STOP")
    _, error = await collect(GEMINI.serving(chunk), hello_walk())
    assert error is not None
    assert "surrogate" in str(error)


@scenario
async def test_replay_this_version_cannot_parse_counts_as_unsigned() -> None:
    odd = replay(thoughtSignature=7, id=["x"])
    body = await request_body(
        Body.User("go"),
        Body.Assistant([Part.Call("c1", "read", "{}", odd)], Stop.Tools, None),
        Body.Results([ok("c1", "one")]),
    )
    call_part = body["contents"][1]["parts"][0]  # type: ignore[index]
    assert call_part == {"functionCall": {"name": "read", "args": {}}}


# Options


@pytest.mark.parametrize(
    ("options", "problem"),
    [
        ({"tempreature": 1}, "unknown option 'tempreature'"),
        ({"temperature": "hot"}, "'temperature' must be a number"),
        ({"top_k": True}, "'top_k' must be an integer"),
        ({"top_k": 40.5}, "'top_k' must be an integer"),
        ({"extra_body": {"generationConfig": {"candidateCount": 2}}}, "candidateCount"),
        ({"thinking": 3}, "'thinking' must be an object"),
        ({"thinking": {"budget": -2}}, "'budget' must be an integer >= -1"),
        ({"thinking": {"level": "low", "budget": 0}}, "both 'budget' and 'level'"),
        ({"thinking": {"include": True}}, "unknown key 'include'"),
        ({"safety_settings": [{"category": "A"}]}, "exactly {category, threshold}"),
        ({"extra_body": {"contents": []}}, "may not set 'contents'"),
        ({"extra_body": {"generationConfig": {"topK": 3}}, "top_k": 5}, "generationConfig.topK"),
        ({"extra_body": {"x": float("nan")}}, "not JSON"),
    ],
)
def test_options_are_checked_and_refused_when_unknown_or_ill_shaped(
    options: dict[str, object], problem: str
) -> None:
    problems = GeminiProvider(environ=ENVIRON).check(model(**options))
    assert any(problem in p for p in problems), problems


def test_a_model_naming_no_key_is_a_problem() -> None:
    provider = GeminiProvider(environ={})
    (no_key,) = provider.check(ModelConfig(GEMINI_MODEL, provider="gemini"))
    assert "api_key_env" in no_key


@scenario
async def test_options_become_generation_config_safety_settings_and_the_extra_body() -> None:
    config = ModelConfig(
        GEMINI_MODEL,
        provider="gemini",
        api_key_env=KEY_ENV,
        max_tokens=50,
        options={
            "temperature": 0.5,
            "top_p": 0.9,
            "top_k": 20,
            "thinking": {"include_thoughts": True, "budget": 1024},
            "safety_settings": [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"}],
            "extra_body": {"generationConfig": {"stopSequences": ["END"]}, "cachedContent": "c/1"},
        },
    )
    _, _, body = await sent_request(GEMINI, hello_walk(), config)
    assert body["generationConfig"] == {
        "maxOutputTokens": 50,
        "temperature": 0.5,
        "topP": 0.9,
        "topK": 20,
        "thinkingConfig": {"includeThoughts": True, "thinkingBudget": 1024},
        "stopSequences": ["END"],
    }
    assert body["safetySettings"] == [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"}]
    assert body["cachedContent"] == "c/1"
    _, _, plain = await sent_request(GEMINI, hello_walk(), GEMINI_BARE)
    assert "generationConfig" not in plain, "thoughts are asked for only when an option says so"


@scenario
async def test_a_walk_ending_in_a_model_turn_fails_before_any_request() -> None:
    walk = walk_of(Body.User("go"), Body.Assistant([Part.Text("hi")], Stop.End, None))
    with pytest.raises(SampleError, match="ends with a model turn"):
        await sent_request(GEMINI, walk, GEMINI_BARE)


@scenario
async def test_blank_user_and_system_turns_are_sent_as_nothing() -> None:
    body = await request_body(Body.User(""), Body.User("hi"), Body.User("  "), system=" ")
    assert "systemInstruction" not in body
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


@scenario
async def test_the_model_is_quoted_in_the_path() -> None:
    config = ModelConfig("x?alt=json#", provider="gemini", api_key_env=KEY_ENV)
    url, _, _ = await sent_request(GEMINI, hello_walk(), config)
    assert url == f"{DEFAULT_URL}/models/x%3Falt%3Djson%23:streamGenerateContent?alt=sse"


def test_check_refuses_a_model_that_carries_the_models_prefix() -> None:
    config = ModelConfig("models/gemini-test", provider="gemini", api_key_env=KEY_ENV)
    (problem,) = GeminiProvider(environ=ENVIRON).check(config)
    assert "'gemini-test'" in problem
