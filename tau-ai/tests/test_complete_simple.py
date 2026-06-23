"""Tests for complete_simple() — the non-streaming completion primitive.

complete_simple is a faithful port of pi's completeSimple (stream.ts:67):
``stream_simple(...).result()``. It is the primitive compaction uses to generate
a summary without a streaming UI. These tests drive the real provider with a
fragmenting SSE feed (reusing the harness from test_tool_call_streaming_fix) so
the accumulation + result() path is exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tau_ai.client import complete_simple
from tau_ai.providers.openai import OpenAICompletionsProvider
from tau_ai.types import AssistantMessage, Model, TextContent


def _model() -> Model:
    return Model(
        id="test-model",
        name="test-model",
        api="openai-completions",
        provider="openai",
        base_url="http://localhost/v1",
        context_window=8192,
        max_tokens=1024,
    )


def _sse(chunks: list[dict]) -> list[str]:
    return ["data: " + json.dumps(c) for c in chunks] + ["data: [DONE]"]


def _text_chunks(text: str, *, fragment: int = 4) -> list[dict]:
    """Stream `text` back in small content fragments, then finish with stop."""
    chunks: list[dict] = []
    for k in range(0, len(text), fragment):
        chunks.append({"id": "c", "choices": [{"delta": {"content": text[k : k + fragment]}}]})
    chunks.append({"id": "c", "choices": [{"delta": {}, "finish_reason": "stop"}]})
    return chunks


class _FakeResponse:
    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self.status_code = status_code
        self._lines = lines
        self.headers = {"x-request-id": "test-req"}
        self.text = "\n".join(lines)

    def json(self) -> dict:
        return {"usage": {"total_tokens": 11}}

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _RecordingClient:
    """Fake httpx client that records the last request payload."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_payload: dict | None = None

    async def post(self, *args, **kwargs):
        self.last_payload = kwargs.get("json")
        return self._response


def test_complete_simple_returns_accumulated_message(monkeypatch):
    """complete_simple drives the stream and returns the final AssistantMessage."""
    client = _RecordingClient(_FakeResponse(_sse(_text_chunks("hello world"))))
    monkeypatch.setattr(OpenAICompletionsProvider, "_get_client", lambda self: client)

    async def go() -> AssistantMessage:
        return await complete_simple(
            _model(),
            {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                ]
            },
            {"api_key": "sk-test", "max_tokens": 256},
        )

    final = asyncio.run(go())
    assert isinstance(final, AssistantMessage)
    text = "".join(c.text for c in final.content if isinstance(c, TextContent))
    assert text == "hello world"


def test_complete_simple_forwards_max_tokens_and_system_message(monkeypatch):
    """options['max_tokens'] reaches the request body; api_key is stripped; the
    leading system message survives conversion (the summary call relies on both)."""
    client = _RecordingClient(_FakeResponse(_sse(_text_chunks("ok"))))
    monkeypatch.setattr(OpenAICompletionsProvider, "_get_client", lambda self: client)

    async def go() -> None:
        await complete_simple(
            _model(),
            {
                "messages": [
                    {"role": "system", "content": "you summarize"},
                    {"role": "user", "content": [{"type": "text", "text": "go"}]},
                ]
            },
            {"api_key": "sk-test", "max_tokens": 4096},
        )

    asyncio.run(go())
    assert client.last_payload is not None
    assert client.last_payload["max_tokens"] == 4096
    assert "api_key" not in client.last_payload
    assert client.last_payload["messages"][0] == {"role": "system", "content": "you summarize"}


def test_complete_simple_raises_on_stream_error(monkeypatch):
    """An ErrorEvent (e.g. HTTP 500) surfaces as a raised exception — Fail-Early,
    no fabricated fallback message."""
    client = _RecordingClient(_FakeResponse(["whatever"], status_code=500))
    monkeypatch.setattr(OpenAICompletionsProvider, "_get_client", lambda self: client)

    async def go() -> AssistantMessage:
        return await complete_simple(
            _model(),
            {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]},
            {"api_key": "sk-test"},
        )

    with pytest.raises(Exception):
        asyncio.run(go())
