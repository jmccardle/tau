"""The request the Gemini provider builds, and what it sends back of a saved reply."""

import json
from pathlib import Path
from typing import cast

import httpx
import pytest
from fakes import FakeTool
from gemini_support import (
    ENVIRON,
    KEY,
    MODEL,
    bodies_sent,
    collect,
    gemini_chunk,
    gemini_walk,
    model,
    provider_for,
    provider_sequence,
    recorded,
    running,
    sent_request,
    walk_of,
)
from harness import scenario

from tau_next import Author, Body, ModelConfig, Outcome, Part, Replay, SampleError, Stop
from tau_next.providers.gemini import GeminiProvider

DEFAULT_URL = "https://generativelanguage.googleapis.com/v1beta"
CALL = Part.Call("c1", "read", '{"path": "/tmp/x"}')


def replay(**data: object) -> Replay:
    return Replay("gemini", json.dumps(data))


async def request_body(
    *bodies: Body, config: ModelConfig | None = None, system: str = "sys"
) -> dict[str, object]:
    walk = walk_of(*bodies, system=system)
    return (await sent_request(walk, config or model(), [FakeTool("t", "x")]))[2]  # tools offered


def ok(call_id: str, value: str) -> tuple[str, Outcome, Author]:
    return (call_id, Outcome.Ok(value), Author.Tools())


def batch(*calls: Part.Call) -> Body:
    return Body.Assistant(list(calls), Stop.Tools, None)


@scenario
async def test_the_request_names_the_model_and_carries_the_key_in_a_header_only() -> None:
    config = ModelConfig(MODEL, provider="gemini", api_key_env="K", max_tokens=99)
    url, headers, body = await sent_request(walk_of(Body.User("hello"), system="be brief"), config)
    assert url == f"{DEFAULT_URL}/models/{MODEL}:streamGenerateContent?alt=sse"
    assert headers["x-goog-api-key"] == KEY
    assert "key=" not in url
    assert body == {
        "systemInstruction": {"parts": [{"text": "be brief"}]},
        "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
        "generationConfig": {"maxOutputTokens": 99},
    }
    proxy = ModelConfig(MODEL, "http://proxy.test:9/v1beta/", "K", provider="gemini")
    url, _, _ = await sent_request(gemini_walk(), proxy)
    assert url == f"http://proxy.test:9/v1beta/models/{MODEL}:streamGenerateContent?alt=sse"


@scenario
async def test_an_http_error_message_never_holds_the_key() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, json={"error": {"code": 400, "message": "bad request"}})

    provider = GeminiProvider(httpx.MockTransport(handle), ENVIRON)
    _, error = await collect(provider, gemini_walk())
    assert error is not None
    assert KEY not in str(error)
    assert KEY not in str(seen[0].url)


@scenario
async def test_tools_are_one_declaration_list_with_their_json_schema_as_given() -> None:
    tool = FakeTool("read", "x", description="Read a file.")
    _, _, body = await sent_request(gemini_walk(), model(), [tool])
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
    provider, seen = provider_sequence(
        recorded("gemini_call"), gemini_chunk([{"text": "done"}], "STOP")
    )
    tool = FakeTool("read", "hi")
    async with running(tmp_path / "s", provider, [tool]) as (_, driver, cursor):
        driver.send(cursor, "test", "read it")
        await driver.rest(cursor)
    recorded_part = next(
        part
        for line in recorded("gemini_call").splitlines()
        if line.startswith("data:")
        for part in json.loads(line[5:])["candidates"][0]["content"]["parts"]
        if "functionCall" in part
    )
    model_turn, answer = bodies_sent(seen)[1]["contents"][1:]  # type: ignore[index]
    assert model_turn == {"role": "model", "parts": [recorded_part]}
    assert recorded_part["thoughtSignature"].encode() in seen[1].content
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
    provider, seen = provider_sequence(
        recorded("gemini_text_thinking"), gemini_chunk([{"text": "ok"}], "STOP")
    )
    async with running(tmp_path / "s", provider) as (session, driver, cursor):
        driver.send(cursor, "test", "tide pools")
        await driver.rest(cursor)
        saved = session.tree.node(session.cursor(cursor).at).body
        driver.send(cursor, "test", "more")
        await driver.rest(cursor)
    assert isinstance(saved, Body.Assistant)
    thinking, text = saved.parts
    assert isinstance(thinking, Part.Thinking)
    assert isinstance(text, Part.Text)
    assert text.replay is not None
    signature = json.loads(text.replay.data)["thoughtSignature"]
    contents = bodies_sent(seen)[1]["contents"]
    assert contents[1] == {  # type: ignore[index]
        "role": "model",
        "parts": [{"text": text.text, "thoughtSignature": signature}],
    }
    assert thinking.text not in seen[1].content.decode()


@scenario
async def test_another_providers_signatures_and_ids_are_not_sent() -> None:
    foreign = Replay("anthropic", '{"signature": "sig", "thoughtSignature": "nope", "id": "x"}')
    body = await request_body(
        Body.User("go"),
        Body.Assistant(
            [Part.Text("t", foreign), Part.Call("call_9", "read", "{}", foreign)],
            Stop.Tools,
            None,
        ),
        Body.Results([ok("call_9", "v")]),
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
async def test_a_batch_is_answered_in_call_order_by_id_where_given_and_by_name_else() -> None:
    api = replay(id="api-1", thoughtSignature="S")
    made_up = replay(synthesized_id=True)
    body = await request_body(
        Body.User("go"),
        batch(
            Part.Call("api-1", "ta", '{"x": 1}', api), Part.Call("gemini-2", "tb", "{}", made_up)
        ),
        Body.Results(
            [
                ok("api-1", "A"),
                ("gemini-2", Outcome.Err("boom"), Author.Tools()),
            ]
        ),
    )
    calls, answers = body["contents"][1:]  # type: ignore[index]
    assert calls["parts"] == [  # the signature only where the API put one
        {"functionCall": {"name": "ta", "args": {"x": 1}, "id": "api-1"}, "thoughtSignature": "S"},
        {"functionCall": {"name": "tb", "args": {}}},
    ]
    assert answers["parts"] == [
        {"functionResponse": {"name": "ta", "response": {"result": "A"}, "id": "api-1"}},
        {"functionResponse": {"name": "tb", "response": {"error": "Error: boom"}}},
    ]


@scenario
async def test_a_batch_whose_arguments_are_not_an_object_goes_as_text_with_its_results() -> None:
    body = await request_body(
        Body.User("go"),
        batch(Part.Call("c1", "read", '{"p"'), Part.Call("c2", "read", "{}")),
        Body.Results([ok("c1", "one"), ("c2", Outcome.Err("bad"), Author.Tools())]),
    )
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "go"}]},
        {
            "role": "model",
            "parts": [{"text": '[tool call c1: read {"p"]'}, {"text": "[tool call c2: read {}]"}],
        },
        {
            "role": "user",
            "parts": [{"text": "[tool result c1: one]\n[tool result c2: Error: bad]"}],
        },
    ]


@scenario
async def test_a_call_with_no_arguments_goes_as_a_call_with_an_empty_object() -> None:
    body = await request_body(
        Body.User("go"), batch(Part.Call("c1", "read", "")), Body.Results([ok("c1", "one")])
    )
    contents = cast("list[dict[str, list[dict[str, dict[str, object]]]]]", body["contents"])
    assert contents[1]["parts"][0]["functionCall"] == {"name": "read", "args": {}}


@scenario
async def test_a_reply_with_nothing_to_send_is_dropped_and_its_neighbours_merge() -> None:
    body = await request_body(
        Body.User("one"),
        Body.Assistant([Part.Thinking("hm", Replay("anthropic", "{}"))], Stop.Interrupted, None),
        Body.User("two"),
    )
    assert body["contents"] == [{"role": "user", "parts": [{"text": "one"}, {"text": "two"}]}]


@scenario
async def test_only_the_leading_system_nodes_are_the_instruction_a_later_one_is_a_note() -> None:
    body = await request_body(
        Body.User("a"), Body.System("now be loud"), Body.User("b"), system="calm"
    )
    assert body["systemInstruction"] == {"parts": [{"text": "calm"}]}
    assert body["contents"] == [
        {
            "role": "user",
            "parts": [{"text": "a"}, {"text": "[system note: now be loud]"}, {"text": "b"}],
        }
    ]


@scenario
async def test_signed_and_unsigned_text_parts_stay_separate_parts() -> None:
    body = await request_body(
        Body.User("go"),
        Body.Assistant(
            [Part.Text("a", replay(thoughtSignature="S")), Part.Text("b"), Part.Text("c")],
            Stop.End,
            None,
        ),
        Body.User("next"),
    )
    assert body["contents"][1]["parts"] == [  # type: ignore[index]
        {"text": "a", "thoughtSignature": "S"},
        {"text": "b"},
        {"text": "c"},
    ]


@scenario
async def test_data_that_cannot_be_kept_as_replay_fails_the_sample_not_core() -> None:
    chunk = gemini_chunk([{"text": "x", "thoughtSignature": "\ud800"}], "STOP")
    _, error = await collect(provider_for(chunk), gemini_walk())
    assert error is not None
    assert "surrogate" in str(error)


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
    provider = GeminiProvider(environ=ENVIRON)
    problems = provider.check(model(**options))
    assert any(problem in p for p in problems), problems


def test_a_model_naming_no_key_and_a_foreign_provider_name_are_problems() -> None:
    provider = GeminiProvider(environ={})
    assert provider.check(ModelConfig(MODEL, provider="gemini")) == [
        "needs an API key: set api_key_env (or api_key)"
    ]
    assert provider.check(ModelConfig(MODEL, api_key_env="K", provider="openai"))[0].startswith(
        "provider is 'openai'"
    )


@scenario
async def test_a_named_variable_that_is_unset_fails_the_sample_saying_so() -> None:
    with pytest.raises(SampleError, match="environment variable K is not set"):
        await sent_request(gemini_walk(), model(), environ={})


@scenario
async def test_options_become_generation_config_safety_settings_and_the_extra_body() -> None:
    config = ModelConfig(
        MODEL,
        provider="gemini",
        api_key_env="K",
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
    _, _, body = await sent_request(gemini_walk(), config)
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
    _, _, plain = await sent_request(gemini_walk(), model())
    assert "generationConfig" not in plain, "thoughts are asked for only when an option says so"


@scenario
async def test_a_walk_ending_in_a_model_turn_fails_before_any_request() -> None:
    walk = walk_of(Body.User("go"), Body.Assistant([Part.Text("hi")], Stop.End, None))
    with pytest.raises(SampleError, match="ends with a model turn"):
        await sent_request(walk, model())


@scenario
async def test_blank_user_and_system_turns_are_sent_as_nothing() -> None:
    body = await request_body(Body.User(""), Body.User("hi"), Body.User("  "), system=" ")
    assert "systemInstruction" not in body
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


@scenario
async def test_the_model_is_quoted_in_the_path() -> None:
    config = ModelConfig("x?alt=json#", provider="gemini", api_key_env="K")
    url, _, _ = await sent_request(gemini_walk(), config)
    assert url == f"{DEFAULT_URL}/models/x%3Falt%3Djson%23:streamGenerateContent?alt=sse"


def test_check_refuses_a_model_that_carries_the_models_prefix() -> None:
    config = ModelConfig("models/gemini-test", provider="gemini", api_key_env="K")
    (problem,) = GeminiProvider(environ=ENVIRON).check(config)
    assert "'gemini-test'" in problem


@scenario
async def test_calls_in_history_go_as_text_when_the_sample_offers_no_tools() -> None:
    walk = walk_of(
        Body.User("go"), batch(Part.Call("c1", "read", "{}")), Body.Results([ok("c1", "one")])
    )
    _, _, body = await sent_request(walk, model())
    assert body["contents"][1] == {"role": "model", "parts": [{"text": "[tool call c1: read {}]"}]}  # type: ignore[index]
    _, _, body = await sent_request(walk, model(), [FakeTool("read", "x")])
    assert "functionCall" in body["contents"][1]["parts"][0]  # type: ignore[index]


@scenario
async def test_replay_this_version_cannot_parse_counts_as_unsigned() -> None:
    odd = replay(thoughtSignature=7, id=["x"])
    walk = walk_of(
        Body.User("go"),
        batch(Part.Call("c1", "read", "{}", odd)),
        Body.Results([ok("c1", "one")]),
    )
    _, _, body = await sent_request(walk, model(), [FakeTool("read", "x")])
    call = body["contents"][1]["parts"][0]  # type: ignore[index]
    assert call == {"functionCall": {"name": "read", "args": {}}}
