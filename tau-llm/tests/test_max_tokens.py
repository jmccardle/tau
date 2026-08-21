"""Tests for the ``Model.max_tokens`` send-path.

``max_tokens`` is a REQUIRED field on every :class:`~tau_llm.types.Model`, and it
was never placed on the request body — declared and not consulted. The symptom
was silent and expensive rather than loud: verified against llama-server, a
Model declaring ``max_tokens=512`` produced a slot reporting ``n_predict = -1``,
so generation ran unbounded against an ``n_ctx`` of 262144 and a single turn
decoded ~120k tokens before anything stopped it.

These pin the fix, including the two ways a caller takes control of the cap
itself (``Model.extra_body`` and per-call options) and the alternate spelling
OpenAI's o-series requires.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from tau_llm.providers.openai import OpenAICompletionsProvider
from tau_llm.types import Model, TextContent, UserMessage


def _sse_stream(chunks: list[dict]) -> str:
    lines = ["data: " + json.dumps(c) for c in chunks]
    lines.append("data: [DONE]")
    return "\n".join(lines)


def _ok_response() -> MagicMock:
    chunks = [
        {"id": "chatcmpl-1", "model": "m", "choices": [{"index": 0, "delta": {"content": "ok"}}]},
        {
            "id": "chatcmpl-1",
            "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    body = _sse_stream(chunks)
    response = MagicMock()
    response.status_code = 200
    response.text = body
    response.headers = {"x-request-id": "rid"}

    async def _aiter():
        for line in body.split("\n"):
            yield line

    response.aiter_lines = _aiter
    return response


class _StreamCM:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _CapturingClient:
    """A fake httpx.AsyncClient that records the JSON payload of the request."""

    last_payload: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def post(self, *args, **kwargs):
        _CapturingClient.last_payload = kwargs.get("json")
        return _ok_response()

    def stream(self, *args, **kwargs):
        _CapturingClient.last_payload = kwargs.get("json")
        return _StreamCM(_ok_response())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass


def _model(**overrides) -> Model:
    defaults: dict = {
        "id": "m",
        "name": "m",
        "api": "openai-completions",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "context_window": 1000,
        "max_tokens": 100,
    }
    defaults.update(overrides)
    return Model(**defaults)


def _run(model: Model, options: dict | None = None) -> dict:
    _CapturingClient.last_payload = None
    provider = OpenAICompletionsProvider(api_key="sk-test")

    async def _go():
        stream = await provider.stream_chat(
            model=model,
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
            options=options,
        )
        async for _ in stream:
            pass

    asyncio.run(_go())
    assert _CapturingClient.last_payload is not None
    return _CapturingClient.last_payload


def _patch_client(monkeypatch) -> None:
    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", _CapturingClient)


def test_model_max_tokens_reaches_the_request_body(monkeypatch):
    """The regression this file exists for: it used to be absent entirely."""
    _patch_client(monkeypatch)
    payload = _run(_model(max_tokens=512))
    assert payload["max_tokens"] == 512


def test_extra_body_max_tokens_wins_over_the_model_default(monkeypatch):
    """A per-model override in config takes precedence over the declared field."""
    _patch_client(monkeypatch)
    payload = _run(_model(max_tokens=100, extra_body={"max_tokens": 4096}))
    assert payload["max_tokens"] == 4096


def test_per_call_max_tokens_wins(monkeypatch):
    """Per-call options beat the model's static default, as for every body field."""
    _patch_client(monkeypatch)
    payload = _run(_model(max_tokens=100), {"max_tokens": 77})
    assert payload["max_tokens"] == 77


def test_max_completion_tokens_suppresses_the_classic_key(monkeypatch):
    """OpenAI's o-series rejects ``max_tokens`` and wants ``max_completion_tokens``.

    A caller naming the cap under that spelling has taken control; sending both
    would be two conflicting caps and a 400 on exactly the models that need the
    alternate key.
    """
    _patch_client(monkeypatch)
    payload = _run(_model(max_tokens=100, extra_body={"max_completion_tokens": 2048}))
    assert payload["max_completion_tokens"] == 2048
    assert "max_tokens" not in payload


def test_max_completion_tokens_via_per_call_options_also_suppresses(monkeypatch):
    _patch_client(monkeypatch)
    payload = _run(_model(max_tokens=100), {"max_completion_tokens": 2048})
    assert payload["max_completion_tokens"] == 2048
    assert "max_tokens" not in payload
