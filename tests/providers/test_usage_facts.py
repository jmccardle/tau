"""The usage facts of D26 section 2: what each provider's normalizer makes of a server's report.

**Every body here is synthetic**: written by hand from the providers' published response formats,
not recorded from a server (the one exception is `plain_usage.sse`, whose chunks llama.cpp sent).
They prove the normalizers do what D26 says with a body of that shape; they do not prove a server
sends that shape. The recorded fixtures (`test_provider_fixtures.py`) are what prove that, and
they are the owner's to record (`scripts/record_provider.py`)."""

import json
from collections.abc import Mapping

import httpx
import pytest
from harness import scenario
from wire import (
    ANTHROPIC,
    DONE_LINE,
    GEMINI,
    OPENAI,
    SSE,
    Dialect,
    Server,
    chunk,
    collect,
    event,
    finish,
    fresh_walk,
    gemini_chunk,
    hello_walk,
    recorded,
    start,
    text,
)

from tau import Finish, ModelConfig, OpenAIProvider, Stop


async def meta_of(dialect: Dialect, stream: str) -> dict[str, object]:
    items, error = await collect(dialect.serving(stream), hello_walk())
    assert error is None
    done = items[-1]
    assert isinstance(done, Finish)
    assert done.meta is not None
    return json.loads(done.meta)


def anthropic(first: Mapping[str, object], last: Mapping[str, object]) -> str:
    """A reply whose `message_start` carries the usage `first` and whose `message_delta` `last`."""
    return start(first) + text(0, ["ok"]) + finish("end_turn", last) + event("message_stop")


def openai(usage: Mapping[str, object]) -> str:
    report = json.dumps({"id": "c1", "model": "m", "choices": [], "usage": usage})
    return chunk({"content": "x"}, "stop") + f"data: {report}\n\n" + DONE_LINE


# Anthropic


@scenario
async def test_anthropic_reports_thinking_and_hour_writes_and_what_the_bill_says_besides() -> None:
    first = {
        "input_tokens": 12,
        "cache_read_input_tokens": 2000,
        "cache_creation_input_tokens": 300,
        "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200},
        "output_tokens": 1,
        "service_tier": "standard",
        "inference_geo": "us",
    }
    last = {
        "output_tokens": 420,
        "output_tokens_details": {"thinking_tokens": 256},
        "server_tool_use": {"web_search_requests": 1},
        "speed": "fast",
    }
    meta = await meta_of(ANTHROPIC, anthropic(first, last))
    assert meta["usage"] == {
        "input": 2312,
        "output": 420,
        "cached": 2000,
        "written": 300,
        "written_1h": 200,
        "reasoning": 256,
    }
    assert meta["service_tier"] == "standard"
    assert meta["inference_geo"] == "us"
    assert meta["speed"] == "fast"
    assert meta["server_tool_use"] == {"web_search_requests": 1}


@scenario
async def test_anthropic_leaves_out_what_the_server_left_out() -> None:
    meta = await meta_of(
        ANTHROPIC, anthropic({"input_tokens": 25, "output_tokens": 1}, {"output_tokens": 9})
    )
    assert meta["usage"] == {"input": 25, "output": 9}
    assert not {"service_tier", "speed", "inference_geo", "server_tool_use"} & set(meta)


@pytest.mark.parametrize(
    "bad",
    [
        {"output_tokens_details": {"thinking_tokens": -1}},
        {"output_tokens_details": {"thinking_tokens": "many"}},
        {"output_tokens_details": 7},
        {"cache_creation": {"ephemeral_1h_input_tokens": 1.5}},
        {"cache_creation": {"ephemeral_1h_input_tokens": 301}},  # more than was written at all
    ],
)
@scenario
async def test_a_malformed_anthropic_count_still_drops_the_usage(bad: dict[str, object]) -> None:
    first = {"input_tokens": 12, "cache_creation_input_tokens": 300, "output_tokens": 1}
    meta = await meta_of(ANTHROPIC, anthropic(first, {"output_tokens": 9, **bad}))
    assert "usage" not in meta
    assert meta["usage_raw"], "what the server said is kept"


@scenario
async def test_a_window_that_ran_out_is_a_length_stop() -> None:
    stream = start() + text(0, ["ok"]) + finish("model_context_window_exceeded")
    items, error = await collect(ANTHROPIC.serving(stream + event("message_stop")), hello_walk())
    assert error is None
    done = items[-1]
    assert isinstance(done, Finish)
    assert (done.stop, done.reason) == (Stop.Length, "model_context_window_exceeded")


# OpenAI


@scenario
async def test_openai_reports_reasoning_and_prediction_tokens() -> None:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 64},
        "completion_tokens_details": {
            "reasoning_tokens": 32,
            "accepted_prediction_tokens": 5,
            "rejected_prediction_tokens": 2,
        },
    }
    meta = await meta_of(OPENAI, openai(usage))
    assert meta["usage"] == {"input": 100, "output": 50, "cached": 64, "reasoning": 32}
    assert meta["prediction"] == {"accepted": 5, "rejected": 2}


@pytest.mark.parametrize(
    "details", [{"reasoning_tokens": None}, {"reasoning_tokens": -3}, "none", ["x"]]
)
@scenario
async def test_a_malformed_reasoning_count_still_drops_the_usage(details: object) -> None:
    usage = {"prompt_tokens": 3, "completion_tokens": 1, "completion_tokens_details": details}
    meta = await meta_of(OPENAI, openai(usage))
    assert "usage" not in meta
    assert meta["usage_raw"] == usage


# Gemini


@scenario
async def test_gemini_counts_a_built_in_tools_prompt_as_input_and_thoughts_as_reasoning() -> None:
    usage = {
        "promptTokenCount": 100,
        "toolUsePromptTokenCount": 40,
        "candidatesTokenCount": 20,
        "thoughtsTokenCount": 7,
        "cachedContentTokenCount": 64,
    }
    stream = gemini_chunk([{"text": "ok"}]) + gemini_chunk([], "STOP", usage)
    meta = await meta_of(GEMINI, stream)
    assert meta["usage"] == {
        "input": 140,
        "output": 27,
        "cached": 64,
        "reasoning": 7,
        "tool_prompt": 40,
    }


@scenario
async def test_gemini_leaves_out_the_counts_it_did_not_report() -> None:
    usage = {"promptTokenCount": 9, "candidatesTokenCount": 2}
    stream = gemini_chunk([{"text": "ok"}]) + gemini_chunk([], "STOP", usage)
    meta = await meta_of(GEMINI, stream)
    assert meta["usage"] == {"input": 9, "output": 2, "cached": 0}


@scenario
async def test_a_malformed_gemini_tool_prompt_count_still_drops_the_usage() -> None:
    usage = {"promptTokenCount": 9, "candidatesTokenCount": 2, "toolUsePromptTokenCount": "x"}
    stream = gemini_chunk([{"text": "ok"}]) + gemini_chunk([], "STOP", usage)
    assert "usage" not in await meta_of(GEMINI, stream)


# llama.cpp

PROPS = {
    "default_generation_settings": {"id": 0, "n_ctx": 32768, "params": {"n_predict": -1}},
    "total_slots": 1,
    "model_path": "/models/qwen3.gguf",
    "chat_template": "{%- for message in messages %}<|im_start|>{{ message.role }}{% endfor %}",
    "build_info": "b7965-abcdef0",
}
MODELS = {
    "object": "list",
    "data": [
        {
            "id": "qwen3",
            "object": "model",
            "owned_by": "llamacpp",
            "meta": {"n_vocab": 151936, "n_ctx_train": 40960, "n_params": 8190735360, "size": 5027},
        }
    ],
}


def llama(pages: Mapping[str, object] | None = None) -> Server:
    """A server that streams llama.cpp's recorded reply with its usage chunk, and answers
    `GET`s from `pages`."""
    reply = httpx.Response(200, headers=SSE, text=recorded("plain_usage"))
    return Server(reply, pages)


async def sampled(provider: object, server_model: str = "qwen3") -> dict[str, object]:
    assert isinstance(provider, OpenAIProvider)
    model = ModelConfig(server_model, "http://llama.test:8100/v1")
    items, error = await collect(provider, fresh_walk(), model)
    assert error is None
    done = items[-1]
    assert isinstance(done, Finish)
    assert done.meta is not None
    return json.loads(done.meta)


@scenario
async def test_llama_cpp_is_asked_about_itself_once_and_its_markers_join_every_meta() -> None:
    server = llama({"/props": PROPS, "/v1/models": MODELS})
    provider = OPENAI.over(server)
    first, second = await sampled(provider), await sampled(provider)
    assert [r.url.path for r in server.asked] == ["/props", "/v1/models"], "once, not per sample"
    for meta in (first, second):
        said = meta["server"]
        assert isinstance(said, dict)
        template = said.pop("template_sha256")
        assert isinstance(template, str)
        assert len(template) == 64
        assert said == {
            "build_info": "b7965-abcdef0",
            "n_ctx": 32768,
            "model_size": 5027,
            "n_params": 8190735360,
            "n_vocab": 151936,
        }
    assert isinstance(provider, OpenAIProvider)
    assert provider.chat_template("http://llama.test:8100/v1") == PROPS["chat_template"]
    assert provider.chat_template("http://other.test/v1") is None


@scenario
async def test_a_server_with_no_props_page_is_not_llama_cpp_and_is_not_asked_again() -> None:
    server = llama()
    provider = OPENAI.over(server)
    first, second = await sampled(provider), await sampled(provider)
    assert [r.url.path for r in server.asked] == ["/props"]
    assert "server" not in first
    assert "server" not in second
    assert len(server.requests) == 2, "both samples went out"


@scenario
async def test_a_props_page_that_is_not_llama_cpps_adds_nothing() -> None:
    server = llama({"/props": {"hello": "world"}})
    meta = await sampled(OPENAI.over(server))
    assert "server" not in meta
    assert [r.url.path for r in server.asked] == ["/props"], "its models are not asked for"


@scenario
async def test_a_server_that_could_not_be_reached_is_asked_again_by_the_next_sample() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            return httpx.Response(200, headers=SSE, text=recorded("plain_usage"))
        if seen.count("GET /props") == 1:
            raise httpx.ConnectError("refused")
        pages = {"/props": PROPS, "/v1/models": MODELS}
        return httpx.Response(200, json=pages[request.url.path])

    provider = OpenAIProvider(httpx.MockTransport(handle), {})
    first, second = await sampled(provider), await sampled(provider)
    assert "server" not in first
    assert isinstance(second["server"], dict)
    assert seen == [
        "GET /props",
        "POST /v1/chat/completions",
        "GET /props",
        "GET /v1/models",
        "POST /v1/chat/completions",
    ]


@scenario
async def test_llama_cpps_cache_count_is_the_cached_of_a_usage_that_has_none() -> None:
    """The recorded chunk: `usage` with no cache count, `timings.cache_n` 0."""
    meta = await sampled(OPENAI.over(llama()))
    assert meta["usage"] == {"input": 20, "output": 7, "cached": 0}
    timings = meta["timings"]
    assert isinstance(timings, dict)
    assert (timings["prompt_n"], timings["cache_n"]) == (20, 0)


@scenario
async def test_a_cache_count_in_the_usage_wins_over_the_timings() -> None:
    usage = {
        "prompt_tokens": 20,
        "completion_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 5},
    }
    timed = json.dumps(
        {"id": "c1", "model": "m", "choices": [], "usage": usage, "timings": {"cache_n": 9}}
    )
    stream = chunk({"content": "x"}, "stop") + f"data: {timed}\n\n" + DONE_LINE
    meta = await meta_of(OPENAI, stream)
    assert meta["usage"] == {"input": 20, "output": 7, "cached": 5}


@scenario
async def test_a_cache_count_larger_than_the_prompt_is_not_taken_from_the_timings() -> None:
    usage = {"prompt_tokens": 20, "completion_tokens": 7}
    timed = json.dumps(
        {"id": "c1", "model": "m", "choices": [], "usage": usage, "timings": {"cache_n": 21}}
    )
    stream = chunk({"content": "x"}, "stop") + f"data: {timed}\n\n" + DONE_LINE
    meta = await meta_of(OPENAI, stream)
    assert meta["usage"] == {"input": 20, "output": 7}
