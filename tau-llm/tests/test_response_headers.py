"""Response headers reach the caller on success and on refusal (docs/RESPONSE-HEADERS.md)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tau_llm import ProviderHttpError, stream_simple
from tau_llm.providers.anthropic import AnthropicMessagesProvider
from tau_llm.providers.google import GoogleGenerativeAIProvider
from tau_llm.streaming import DoneEvent
from tau_llm.types import Model

RATE_HEADERS = {"X-RateLimit-Remaining-Requests": "99", "Retry-After": "7"}
LOWERED = {"x-ratelimit-remaining-requests": "99", "retry-after": "7"}


def _model(**overrides: Any) -> Model:
    fields: dict[str, Any] = {
        "id": "m",
        "name": "m",
        "api": "openai-completions",
        "provider": "local",
        "base_url": "http://llm.test/v1",
        "context_window": 8192,
        "max_tokens": 256,
    }
    fields.update(overrides)
    return Model(**fields)


def _route_openai(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    real_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", client)


def _openai_ok(request: httpx.Request) -> httpx.Response:
    if json.loads(request.content).get("stream"):
        chunk = {
            "id": "c1",
            "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}],
        }
        body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx.Response(
            200, headers={**RATE_HEADERS, "Content-Type": "text/event-stream"}, text=body
        )
    message = {"role": "assistant", "content": "hi"}
    return httpx.Response(
        200,
        headers=RATE_HEADERS,
        json={"id": "c1", "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]},
    )


@pytest.mark.parametrize("stream", [True, False])
async def test_openai_done_event_carries_lowercased_headers(monkeypatch, stream):
    _route_openai(monkeypatch, httpx.MockTransport(_openai_ok))

    events = await stream_simple(
        _model(stream=stream),
        {"messages": [{"role": "user", "content": "hi"}]},
        {"api_key": "not-needed"},
    )
    done = [e async for e in events if isinstance(e, DoneEvent)]

    assert done[0].response_headers is not None
    assert LOWERED.items() <= done[0].response_headers.items()
    assert events.response_headers == done[0].response_headers


@pytest.mark.parametrize("stream", [True, False])
async def test_openai_refusal_carries_retry_after(monkeypatch, stream):
    refusing = httpx.MockTransport(
        lambda request: httpx.Response(
            429, headers=RATE_HEADERS, json={"error": {"message": "slow down"}}
        )
    )
    _route_openai(monkeypatch, refusing)

    events = await stream_simple(
        _model(stream=stream),
        {"messages": [{"role": "user", "content": "hi"}]},
        {"api_key": "not-needed"},
    )
    with pytest.raises(ProviderHttpError) as excinfo:
        await events.result()

    assert excinfo.value.status_code == 429
    assert excinfo.value.headers is not None
    assert excinfo.value.headers["retry-after"] == "7"


def _anthropic_model() -> Model:
    return _model(
        id="claude-opus-5",
        api="anthropic-messages",
        provider="anthropic",
        base_url="https://api.anthropic.com",
    )


async def test_anthropic_done_event_carries_the_stream_response_headers():
    final = SimpleNamespace(
        id="msg_01",
        model="claude-opus-5",
        content=[SimpleNamespace(type="text", text="hi")],
        stop_reason="end_turn",
        stop_details=None,
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )

    class _Stream:
        response = SimpleNamespace(headers={"Anthropic-RateLimit-Tokens-Remaining": "1000"})

        def __aiter__(self) -> Any:
            async def gen() -> Any:
                yield SimpleNamespace(type="text", text="hi")
                yield SimpleNamespace(type="message_stop")

            return gen()

        async def get_final_message(self) -> Any:
            return final

    class _Manager:
        async def __aenter__(self) -> Any:
            return _Stream()

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    class _Client:
        class messages:  # noqa: N801 — mirrors the SDK's attribute name
            @staticmethod
            def stream(**kwargs: Any) -> Any:
                return _Manager()

    provider = AnthropicMessagesProvider(api_key="sk-ant-test")
    provider._client = _Client()

    stream = await provider.stream_chat(_anthropic_model(), [{"role": "user", "content": "hi"}])
    done = [e async for e in stream if isinstance(e, DoneEvent)]

    assert done[0].response_headers == {"anthropic-ratelimit-tokens-remaining": "1000"}


async def test_anthropic_refusal_carries_retry_after():
    anthropic = pytest.importorskip("anthropic")
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    response = httpx.Response(
        429,
        headers=RATE_HEADERS,
        json=body,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    sdk_error = anthropic.RateLimitError("Error code: 429", response=response, body=body)

    class _Refusing:
        class messages:  # noqa: N801 — mirrors the SDK's attribute name
            @staticmethod
            def stream(**kwargs: Any) -> Any:
                raise sdk_error

    provider = AnthropicMessagesProvider(api_key="sk-ant-test")
    provider._client = _Refusing()

    stream = await provider.stream_chat(_anthropic_model(), [{"role": "user", "content": "hi"}])
    with pytest.raises(ProviderHttpError) as excinfo:
        async for _ in stream:
            pass

    assert excinfo.value.headers is not None
    assert excinfo.value.headers["retry-after"] == "7"


def _google_provider(monkeypatch: pytest.MonkeyPatch, models: Any) -> GoogleGenerativeAIProvider:
    class _Client:
        aio = type("A", (), {"models": models})()

    provider = GoogleGenerativeAIProvider(api_key="k")
    monkeypatch.setattr(provider, "_get_client", lambda: _Client())
    return provider


def _google_model() -> Model:
    return _model(id="gemini-x", api="google-generative-ai", provider="gemini")


async def test_google_done_event_carries_the_first_chunk_http_response_headers(monkeypatch):
    part = SimpleNamespace(text="hi", thought=False, function_call=None, thought_signature=None)
    candidate = SimpleNamespace(finish_reason="STOP", content=SimpleNamespace(parts=[part]))
    chunk = SimpleNamespace(
        usage_metadata=None,
        candidates=[candidate],
        sdk_http_response=SimpleNamespace(headers={"X-Goog-Quota": "5"}),
    )

    async def chunks() -> Any:
        yield chunk

    class _Models:
        async def generate_content_stream(self, **kwargs: Any) -> Any:
            return chunks()

    provider = _google_provider(monkeypatch, _Models())
    stream = await provider.stream_chat(_google_model(), [{"role": "user", "content": "hi"}])
    done = [e async for e in stream if isinstance(e, DoneEvent)]

    assert done[0].response_headers == {"x-goog-quota": "5"}


async def test_google_refusal_carries_retry_after(monkeypatch):
    errors = pytest.importorskip("google.genai.errors")
    payload = {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}
    sdk_error = errors.APIError(
        429, payload, response=httpx.Response(429, headers=RATE_HEADERS, json=payload)
    )

    class _Models:
        async def generate_content_stream(self, **kwargs: Any) -> Any:
            raise sdk_error

    provider = _google_provider(monkeypatch, _Models())
    stream = await provider.stream_chat(_google_model(), [{"role": "user", "content": "hi"}])
    with pytest.raises(ProviderHttpError) as excinfo:
        async for _ in stream:
            pass

    assert excinfo.value.headers is not None
    assert excinfo.value.headers["retry-after"] == "7"


def test_an_error_built_without_a_response_says_its_headers_are_unknown():
    err = ProviderHttpError(503, message="", body="", model_id="q", base_url="u")
    assert err.headers is None
