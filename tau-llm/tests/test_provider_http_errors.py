"""A non-2xx reaches every caller as ProviderHttpError with its status (docs/PROVIDER-HTTP-ERRORS.md)."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
import pytest

from tau_llm import ProviderHttpError, complete_simple
from tau_llm.errors import MAX_ERROR_BODY_CHARS
from tau_llm.providers.anthropic import AnthropicMessagesProvider
from tau_llm.providers.google import GoogleGenerativeAIProvider
from tau_llm.streaming import ErrorEvent
from tau_llm.types import Model


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


def _openai_refusing(status: int, body: dict[str, Any]) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, json=body))


@pytest.mark.parametrize("stream", [True, False])
def test_complete_simple_raises_the_status_of_an_over_context_400(monkeypatch, stream):
    """The JMFTS path: complete_simple, a 400, both transports."""
    transport = _openai_refusing(400, {"error": {"message": "exceeds the context size"}})
    real_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", client)

    async def drive() -> None:
        await complete_simple(
            _model(stream=stream),
            {"messages": [{"role": "user", "content": "hi"}]},
            {"api_key": "not-needed"},
        )

    with pytest.raises(ProviderHttpError) as excinfo:
        asyncio.run(drive())

    err = excinfo.value
    assert (err.status_code, err.message, err.model_id) == (400, "exceeds the context size", "m")
    assert err.base_url == "http://llm.test/v1"
    assert json.loads(err.body) == {"error": {"message": "exceeds the context size"}}


def test_the_message_keeps_the_0_12_prefix_that_callers_already_parse():
    """jmfts_core.llm_client matches ``^HTTP (\\d{3}) from model `` on 0.12.0's text."""
    err = ProviderHttpError(503, message="loading", body="", model_id="q", base_url="http://x")
    assert re.match(r"^HTTP (\d{3}) from model ", str(err)).group(1) == "503"
    assert str(err) == "HTTP 503 from model 'q' at 'http://x': loading"


def test_an_empty_message_says_so_and_an_oversized_body_is_bounded():
    err = ProviderHttpError(
        502, message="", body="x" * (MAX_ERROR_BODY_CHARS + 10), model_id="q", base_url="u"
    )
    assert str(err).endswith(": (empty response body)")
    assert err.body.endswith("... [truncated 10 chars]")


def test_anthropic_status_errors_become_provider_http_errors():
    anthropic = pytest.importorskip("anthropic")
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": "too long"}}
    response = httpx.Response(
        400,
        json=body,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    sdk_error = anthropic.BadRequestError("Error code: 400", response=response, body=body)
    provider = AnthropicMessagesProvider(api_key="sk-ant-test")

    class _Refusing:
        class messages:  # noqa: N801 — mirrors the SDK's attribute name
            @staticmethod
            def stream(**kwargs: Any) -> Any:
                raise sdk_error

    provider._client = _Refusing()
    model = _model(
        id="claude-opus-5",
        api="anthropic-messages",
        provider="anthropic",
        base_url="https://api.anthropic.com",
    )

    async def drive() -> None:
        stream = await provider.stream_chat(model, [{"role": "user", "content": "hi"}])
        async for _ in stream:
            pass

    with pytest.raises(ProviderHttpError) as excinfo:
        asyncio.run(drive())

    err = excinfo.value
    assert (err.status_code, err.message, err.model_id) == (400, "too long", "claude-opus-5")
    assert err.__cause__ is sdk_error
    assert json.loads(err.body) == body


async def test_google_api_errors_become_provider_http_errors(monkeypatch):
    errors = pytest.importorskip("google.genai.errors")
    payload = {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}
    sdk_error = errors.APIError(429, payload)

    class _Models:
        async def generate_content_stream(self, **kwargs: Any) -> Any:
            raise sdk_error

    class _Client:
        aio = type("A", (), {"models": _Models()})()

    provider = GoogleGenerativeAIProvider(api_key="k")
    monkeypatch.setattr(provider, "_get_client", lambda: _Client())
    model = _model(
        id="gemini-x",
        api="google-generative-ai",
        provider="gemini",
        base_url="https://generativelanguage.googleapis.com",
    )

    stream = await provider.stream_chat(model, [{"role": "user", "content": "hi"}])
    with pytest.raises(ProviderHttpError) as excinfo:
        async for _ in stream:
            pass

    err = excinfo.value
    assert (err.status_code, err.message) == (429, "quota")
    assert err.__cause__ is sdk_error
    assert json.loads(err.body) == payload


async def test_a_non_status_sdk_failure_stays_an_in_band_error_event(monkeypatch):
    """Only an HTTP status is promoted; a dropped connection is reported as before."""

    class _Models:
        async def generate_content_stream(self, **kwargs: Any) -> Any:
            raise RuntimeError("connection reset")

    class _Client:
        aio = type("A", (), {"models": _Models()})()

    provider = GoogleGenerativeAIProvider(api_key="k")
    monkeypatch.setattr(provider, "_get_client", lambda: _Client())
    model = _model(id="gemini-x", api="google-generative-ai", provider="gemini")

    stream = await provider.stream_chat(model, [{"role": "user", "content": "hi"}])
    events = [event async for event in stream]

    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].exception is None
    assert "connection reset" in events[-1].message
