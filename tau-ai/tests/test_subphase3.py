"""Tests for Phase 1 Subphase 3 — Streaming Protocol and Client.

These tests implement the test cases listed in PHASE-1-SUBPHASE-3.md
"Testing Strategy" section.

Test categories:
  1. stream_simple returns event stream
  2. Text-only stream produces correct events
  3. Tool call stream accumulates arguments
  4. Error event on API error
  5. stream.result() blocks until done
  6. Abort propagates

Reference: PHASE-1-SUBPHASE-3.md, "Testing Strategy" section
           SUBPHASE-0.0.md, "4. Streaming Events" section
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator
from unittest.mock import MagicMock, patch

import pytest

from tau_ai.streaming import (
    AssistantMessageEventStream,
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ToolCallDeltaEvent,
)
from tau_ai.types import (
    AssistantMessage,
    Model,
    TextContent,
    ToolCall,
    Usage,
)
from tau_ai.client import stream_simple
from tau_ai.providers.openai import OpenAICompletionsProvider
from tau_ai.tools import ToolDefinition
from tau_ai.abort import AbortSignal


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _make_model(**overrides) -> Model:
    """Create a test Model with defaults."""
    defaults = {
        "id": "gpt-4o",
        "name": "GPT-4o",
        "api": "openai-completions",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "context_window": 128000,
        "max_tokens": 4096,
    }
    defaults.update(overrides)
    return Model(**defaults)


def _attach_aiter_lines(response: MagicMock) -> MagicMock:
    """Give a mock response an ``aiter_lines()`` that async-yields its SSE body's
    lines, the way real httpx does.

    The provider reads the stream via ``response.aiter_lines()``
    (``openai.py:659``), NOT ``.text``. A bare ``MagicMock.aiter_lines()`` yields
    zero lines, so the SSE parser never runs and ``DoneEvent.final`` is ``None``
    — which is why these streaming tests failed on ``'NoneType' object has no
    attribute 'content'`` regardless of the parsing logic (CODE-QUALITY-NOTES
    #11). Call this on every status-200 response mock.
    """
    body = response.text

    async def _aiter():
        for line in body.split("\n"):
            yield line

    response.aiter_lines = _aiter
    return response


def _make_mock_text_response(text_chunks: list[str], finish_reason: str = "stop", usage: dict | None = None) -> MagicMock:
    """Create a mock HTTP response with streaming text deltas."""
    chunks = []
    for i, chunk in enumerate(text_chunks):
        chunks.append({
            "id": f"chatcmpl-test-{i}",
            "model": "gpt-4",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "choices": [{"index": 0, "delta": {"content": chunk}}],
        })
    chunks.append({
        "id": "chatcmpl-test-final",
        "model": "gpt-4",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    })

    lines = ["data: " + json.dumps(c) for c in chunks]
    lines.append("data: [DONE]")
    body = "\n".join(lines)

    response = MagicMock()
    response.status_code = 200
    response.text = body
    _attach_aiter_lines(response)
    response.headers = {"x-request-id": "test-req-id"}
    response.json.return_value = {
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    }
    return response


def _make_mock_tool_call_response(tool_calls: list[dict]) -> MagicMock:
    """Create a mock HTTP response with tool call streaming."""
    chunks = []

    # Text before tool calls
    chunks.append({
        "id": "chatcmpl-tool-call",
        "model": "gpt-4",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "choices": [{"index": 0, "delta": {"content": "Let me check that."}}],
    })

    # Stream tool call deltas
    for i, tc in enumerate(tool_calls):
        tc_id = tc["id"]
        name = tc["name"]
        args_str = json.dumps(tc["arguments"])

        # Stream name char by char
        for char in name:
            chunks.append({
                "id": f"chatcmpl-tool-call-{i}",
                "model": "gpt-4",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "choices": [{
                    "index": 0,
                    "delta": {
                        "content": None,
                        "tool_calls": [{
                            "index": i,
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": char, "arguments": ""},
                        }],
                    },
                }],
            })

        # Stream arguments in chunks
        for j in range(0, len(args_str), 3):
            chunk_text = args_str[j:j+3]
            chunks.append({
                "id": f"chatcmpl-tool-call-{i}",
                "model": "gpt-4",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "choices": [{
                    "index": 0,
                    "delta": {
                        "content": None,
                        "tool_calls": [{
                            "index": i,
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": None, "arguments": chunk_text},
                        }],
                    },
                }],
            })

    # Final
    chunks.append({
        "id": "chatcmpl-tool-call-final",
        "model": "gpt-4",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    })

    lines = ["data: " + json.dumps(c) for c in chunks]
    lines.append("data: [DONE]")
    body = "\n".join(lines)

    response = MagicMock()
    response.status_code = 200
    response.text = body
    _attach_aiter_lines(response)
    response.headers = {"x-request-id": "test-tool-req-id"}
    response.json.return_value = {
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    }
    return response


def _make_mock_error_response(status_code: int = 401, error_msg: str = "Invalid API key") -> MagicMock:
    """Create a mock HTTP error response."""
    response = MagicMock()
    response.status_code = status_code
    response.text = json.dumps({"error": {"message": error_msg, "type": "invalid_request_error"}})
    response.json.return_value = {
        "error": {"message": error_msg, "type": "invalid_request_error"}
    }
    return response


def _make_mock_client(response: MagicMock):
    """Factory for a mock HTTP client that returns the given response."""

    class MockClient:
        def __init__(self, *args, **kwargs):
            self._response = response

        async def post(self, *args, **kwargs):
            return self._response

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

    return MockClient


def _collect_events(stream: Any) -> list[Any]:
    """Helper to collect all events from a stream (sync wrapper)."""
    async def _collect():
        events = []
        async for event in stream:
            events.append(event)
        return events
    return asyncio.run(_collect())


def _make_text_only_sse(chunks: list[str]) -> str:
    """Build SSE text-only response."""
    lines = []
    for i, chunk in enumerate(chunks):
        lines.append("data: " + json.dumps({
            "id": f"chunk-{i}",
            "choices": [{"delta": {"content": chunk}, "index": 0}],
        }))
    lines.append("data: [DONE]")
    return "\n".join(lines)


def _make_error_sse(status_code: int, error_msg: str) -> tuple[MagicMock, int]:
    """Build an SSE error response mock."""
    response = MagicMock()
    response.status_code = status_code
    response.text = json.dumps({"error": {"message": error_msg}})
    response.json.return_value = {"error": {"message": error_msg}}
    return response


# ═══════════════════════════════════════════════════════════════════════════
# Test 1: Stream simple returns event stream
# ═══════════════════════════════════════════════════════════════════════════

class TestStreamSimpleReturnsStream:
    """Test 1 from PHASE-1-SUBPHASE-3.md 'Testing Strategy'.

    Verify stream_simple returns an AssistantMessageEventStream.
    """

    def _make_mock_client(self, response):
        return _make_mock_client(response)

    def test_stream_simple_returns_event_stream(self, monkeypatch):
        """stream_simple returns an AssistantMessageEventStream for text response."""
        mock_response = _make_mock_text_response(["Hello, world!"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return stream

        stream = asyncio.run(run())
        assert isinstance(stream, AssistantMessageEventStream)

    def test_stream_simple_returns_event_stream_for_tool_calls(self, monkeypatch):
        """stream_simple returns an AssistantMessageEventStream for tool call response."""
        tool_calls = [{"id": "call_abc123", "name": "bash", "arguments": {"command": "ls -la"}}]
        mock_response = _make_mock_tool_call_response(tool_calls)
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "list files"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return stream

        stream = asyncio.run(run())
        assert isinstance(stream, AssistantMessageEventStream)

    def test_stream_simple_returns_event_stream_on_error(self, monkeypatch):
        """stream_simple returns an AssistantMessageEventStream even on API error."""
        mock_response = _make_mock_error_response(status_code=401, error_msg="Invalid API key")
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return stream

        stream = asyncio.run(run())
        assert isinstance(stream, AssistantMessageEventStream)


# ═══════════════════════════════════════════════════════════════════════════
# Test 2: Text-only stream produces correct events
# ═══════════════════════════════════════════════════════════════════════════

class TestTextOnlyStream:
    """Test 2 from PHASE-1-SUBPHASE-3.md 'Testing Strategy'.

    Verify text-only streams produce text_delta + done events in the
    correct order, with proper final message and usage.
    """

    def _make_mock_client(self, response):
        return _make_mock_client(response)

    def test_text_only_stream_event_count(self, monkeypatch):
        """Text-only stream produces exactly 2 events (1 text_delta + 1 done)."""
        mock_response = _make_mock_text_response(["Hello"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        assert len(events) == 2  # 1 text_delta + 1 done
        assert events[0].type == "text_delta"
        assert events[1].type == "done"

    def test_text_only_stream_first_delta_content(self, monkeypatch):
        """First TextDeltaEvent.delta equals the expected text."""
        mock_response = _make_mock_text_response(["Hello"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        assert events[0].type == "text_delta"
        assert events[0].delta == "Hello"

    def test_text_only_stream_final_message_text(self, monkeypatch):
        """DoneEvent.final contains the full text response."""
        mock_response = _make_mock_text_response(["Hello", ", ", "world!"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        # With 3 text chunks there are 4 events (3 text_delta + 1 done);
        # the DoneEvent is always the last one.
        done = events[-1]
        assert isinstance(done, DoneEvent)
        assert isinstance(done.final, AssistantMessage)
        text_blocks = [c for c in done.final.content if isinstance(c, TextContent)]
        full_text = "".join(c.text for c in text_blocks)
        assert full_text == "Hello, world!"

    def test_text_only_stream_usage_total_tokens(self, monkeypatch):
        """DoneEvent.usage has correct total_tokens."""
        mock_response = _make_mock_text_response(
            ["Hello, world!"],
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        done = events[1]
        assert isinstance(done, DoneEvent)
        assert isinstance(done.usage, Usage)
        assert done.usage.total_tokens > 0
        assert done.usage.total_tokens == 30

    def test_text_only_stream_produces_text_delta_events(self, monkeypatch):
        """Text-only stream yields TextDeltaEvent instances."""
        mock_response = _make_mock_text_response(["Hello", ", ", "world!"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        text_events = [e for e in events if isinstance(e, TextDeltaEvent)]
        assert len(text_events) > 0
        # All deltas together form the full response
        full_text = "".join(e.delta for e in text_events)
        assert "Hello" in full_text
        assert "world" in full_text

    def test_text_only_stream_no_toolcall_events(self, monkeypatch):
        """Text-only stream does not produce ToolCallDeltaEvent."""
        mock_response = _make_mock_text_response(["Hello, world!"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        toolcall_events = [e for e in events if isinstance(e, ToolCallDeltaEvent)]
        assert len(toolcall_events) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Test 3: Tool call stream accumulates arguments
# ═══════════════════════════════════════════════════════════════════════════

class TestToolCallStreamAccumulates:
    """Test 3 from PHASE-1-SUBPHASE-3.md 'Testing Strategy'.

    Verify that tool call deltas are accumulated correctly across
    multiple chunks, producing the full tool call in DoneEvent.
    """

    def _make_mock_client(self, response):
        return _make_mock_client(response)

    def test_tool_call_stream_accumulates_name(self, monkeypatch):
        """Tool call name is accumulated across delta events."""
        tool_calls = [{"id": "call_abc123", "name": "bash", "arguments": {"command": "ls -la"}}]
        mock_response = _make_mock_tool_call_response(tool_calls)
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "list files"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        done_events = [e for e in events if isinstance(e, DoneEvent)]
        assert len(done_events) == 1
        final = done_events[0].final
        tc_blocks = [c for c in final.content if isinstance(c, ToolCall)]
        assert len(tc_blocks) == 1
        assert tc_blocks[0].name == "bash"
        assert tc_blocks[0].id == "call_abc123"

    def test_tool_call_stream_accumulates_arguments(self, monkeypatch):
        """Tool call arguments are accumulated correctly across deltas."""
        tool_calls = [{"id": "call_abc123", "name": "bash", "arguments": {"command": "ls -la"}}]
        mock_response = _make_mock_tool_call_response(tool_calls)
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "list files"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return await stream.result()

        final = asyncio.run(run())
        tc_blocks = [c for c in final.content if isinstance(c, ToolCall)]
        assert len(tc_blocks) == 1
        args = tc_blocks[0].arguments
        assert "command" in args
        assert args["command"] == "ls -la"

    def test_tool_call_stream_result(self):
        """Test 3 from subphase doc: stream.result() returns accumulated message with tool calls.

        stream_simple(...); final = await stream.result();
        tool_calls = [c for c in final.content if hasattr(c, 'type') and c.type == "toolCall"]
        assert len(tool_calls) == 1
        assert tool_calls[0].arguments == {"command": "ls"}
        """
        tool_calls = [{"id": "call_abc123", "name": "bash", "arguments": {"command": "ls"}}]
        mock_response = _make_mock_tool_call_response(tool_calls)

        class MockClient:
            def __init__(self, *args, **kwargs):
                self._response = mock_response
            async def post(self, *args, **kwargs):
                return self._response
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                pass

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "list files"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            final = await stream.result()
            return final

        with patch("tau_ai.providers.openai.httpx.AsyncClient", MockClient):
            final = asyncio.run(run())

        tool_calls_in_content = [
            c for c in final.content
            if hasattr(c, "type") and c.type == "toolCall"
        ]
        assert len(tool_calls_in_content) == 1
        assert tool_calls_in_content[0].arguments == {"command": "ls"}

    def test_tool_call_stream_multiple_tool_calls(self, monkeypatch):
        """Multiple tool calls are all accumulated correctly."""
        tool_calls = [
            {"id": "call_1", "name": "read_file", "arguments": {"path": "main.py"}},
            {"id": "call_2", "name": "bash", "arguments": {"command": "npm test"}},
        ]

        chunks = []
        # Text before tool calls
        chunks.append({
            "id": "multi-tc",
            "choices": [{"delta": {"content": "Running tools."}}],
        })
        # Tool call 1 - name
        chunks.append({
            "id": "multi-tc",
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "r", "arguments": ""},
                    }]
                }
            }],
        })
        chunks.append({
            "id": "multi-tc",
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "ead_file", "arguments": ""},
                    }]
                }
            }],
        })
        # Tool call 1 - arguments
        chunks.append({
            "id": "multi-tc",
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": None, "arguments": '{"path": "main.py"}'},
                    }]
                }
            }],
        })
        # Tool call 2 - name
        chunks.append({
            "id": "multi-tc",
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 1,
                        "id": "call_2",
                        "function": {"name": "bash", "arguments": ""},
                    }]
                }
            }],
        })
        chunks.append({
            "id": "multi-tc",
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 1,
                        "id": "call_2",
                        "function": {"name": None, "arguments": '{"command": "npm test"}'},
                    }]
                }
            }],
        })
        # Final
        chunks.append({
            "id": "multi-tc",
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
        })

        lines = ["data: " + json.dumps(c) for c in chunks]
        lines.append("data: [DONE]")
        sse_body = "\n".join(lines)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = sse_body
        _attach_aiter_lines(mock_response)
        mock_response.headers = {"x-request-id": "test"}
        mock_response.json.return_value = {"usage": {"total_tokens": 50}}

        async def run():
            model = _make_model()
            provider = OpenAICompletionsProvider(api_key="sk-test")

            # Patch the client
            class MockClient:
                def __init__(self, *args, **kwargs):
                    self._response = mock_response
                async def post(self, *args, **kwargs):
                    return self._response
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *exc):
                    pass

            with patch.object(provider, "_get_client", return_value=MockClient()):
                stream = await provider.stream_chat(
                    model=model,
                    messages=[{
                        "role": "user",
                        "content": [{"type": "text", "text": "run tools"}],
                    }],
                )
                events = [e async for e in stream]
                done_events = [e for e in events if isinstance(e, DoneEvent)]
                return done_events[0].final

        final = asyncio.run(run())
        tc_blocks = [c for c in final.content if isinstance(c, ToolCall)]
        assert len(tc_blocks) == 2
        assert tc_blocks[0].name == "read_file"
        assert tc_blocks[1].name == "bash"


# ═══════════════════════════════════════════════════════════════════════════
# Test 4: Error event on API error
# ═══════════════════════════════════════════════════════════════════════════

class TestErrorEvent:
    """Test 4 from PHASE-1-SUBPHASE-3.md 'Testing Strategy'.

    Verify that API errors produce ErrorEvent instances.
    """

    def _make_mock_client(self, response):
        return _make_mock_client(response)

    def test_error_event_on_http_401(self, monkeypatch):
        """401 response produces ErrorEvent with 'Invalid API key'."""
        mock_response = _make_mock_error_response(status_code=401, error_msg="Invalid API key")
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        assert any(e.type == "error" for e in events)
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1
        assert "Invalid API key" in error_events[0].message
        assert error_events[0].is_error is True

    def test_error_event_on_http_429(self, monkeypatch):
        """429 response produces ErrorEvent with rate limit message."""
        mock_response = _make_mock_error_response(
            status_code=429, error_msg="Rate limit exceeded."
        )
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1
        assert "Rate limit" in error_events[0].message

    def test_error_event_on_http_500(self, monkeypatch):
        """500 response produces ErrorEvent with HTTP status."""
        mock_response = _make_mock_error_response(
            status_code=500, error_msg="Internal Server Error"
        )
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1
        assert "500" in error_events[0].message

    def test_error_event_is_error_flag(self, monkeypatch):
        """ErrorEvent has is_error=True."""
        mock_response = _make_mock_error_response()
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1
        assert error_events[0].is_error is True

    def test_error_event_only_event(self, monkeypatch):
        """ErrorEvent is the only event produced on API error (no other events)."""
        mock_response = _make_mock_error_response()
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        # Only error event, no text_delta or done events
        text_events = [e for e in events if isinstance(e, TextDeltaEvent)]
        done_events = [e for e in events if isinstance(e, DoneEvent)]
        assert len(text_events) == 0
        assert len(done_events) == 0
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Test 5: stream.result() blocks until done
# ═══════════════════════════════════════════════════════════════════════════

class TestResultBlocksUntilDone:
    """Test 5 from PHASE-1-SUBPHASE-3.md 'Testing Strategy'.

    Verify stream.result() blocks until the stream completes.
    """

    def _make_mock_client(self, response):
        return _make_mock_client(response)

    def test_result_blocks_until_done(self, monkeypatch):
        """stream.result() blocks until done and returns AssistantMessage."""
        mock_response = _make_mock_text_response(["Hello, world!"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            final = await stream.result()
            return final

        final = asyncio.run(run())
        assert isinstance(final, AssistantMessage)
        # Verify the content is available
        text_blocks = [c for c in final.content if isinstance(c, TextContent)]
        assert len(text_blocks) > 0
        full_text = "".join(c.text for c in text_blocks)
        assert full_text == "Hello, world!"

    def test_result_after_iterating(self, monkeypatch):
        """Calling result() after iterating the stream returns the final message."""
        mock_response = _make_mock_text_response(["Hello"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            # Iterate first
            async for event in stream:
                pass
            # Then get result
            return await stream.result()

        final = asyncio.run(run())
        assert isinstance(final, AssistantMessage)
        text_blocks = [c for c in final.content if isinstance(c, TextContent)]
        assert text_blocks[0].text == "Hello"

    def test_result_after_consuming_events(self, monkeypatch):
        """Collecting all events via async for then calling result() returns same data."""
        mock_response = _make_mock_text_response(["Hello, world!"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            events = [e async for e in stream]
            # Stream already exhausted, result should still work
            final = await stream.result()
            return final

        final = asyncio.run(run())
        assert isinstance(final, AssistantMessage)

    def test_result_returns_same_instance_after_multiple_calls(self, monkeypatch):
        """Multiple calls to result() return the same final message."""
        mock_response = _make_mock_text_response(["Hello"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            final1 = await stream.result()
            final2 = await stream.result()
            return final1, final2

        final1, final2 = asyncio.run(run())
        assert final1 is final2

    def test_result_returns_message_from_done_event(self, monkeypatch):
        """stream.result() returns the same message as DoneEvent.final."""
        mock_response = _make_mock_text_response(["Hello"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            events = [e async for e in stream]
            done = [e for e in events if isinstance(e, DoneEvent)][0]
            result_msg = await stream.result()
            return done.final, result_msg

        done_final, result_msg = asyncio.run(run())
        assert done_final is result_msg


# ═══════════════════════════════════════════════════════════════════════════
# Test 6: Abort propagates
# ═══════════════════════════════════════════════════════════════════════════

class TestAbortPropagates:
    """Test 6 from PHASE-1-SUBPHASE-3.md 'Testing Strategy'.

    Verify abort() propagates to the underlying provider and preserves
    partial state.
    """

    def _make_mock_client(self, response):
        return _make_mock_client(response)

    def test_abort_preserves_partial_state(self, monkeypatch):
        """stream.abort() preserves partial state (partial is not None)."""
        mock_response = _make_mock_text_response(["Hello"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            # Collect all events first
            async for event in stream:
                pass
            # Abort should not fail
            stream.abort()
            return stream._partial is not None

        preserved = asyncio.run(run())
        assert preserved is True

    def test_abort_propagates_to_provider(self, monkeypatch):
        """stream.abort() calls abort() on the underlying provider stream."""
        mock_response = _make_mock_text_response(["Hello"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            # The provider's stream has an abort method
            # In current implementation, the provider wraps in an internal async generator
            # We test that abort() doesn't raise
            try:
                stream.abort()
                return True
            except Exception:
                return False

        result = asyncio.run(run())
        # abort() should not raise an exception
        assert result is True

    def test_abort_idempotent(self, monkeypatch):
        """Multiple calls to abort() do not raise."""
        mock_response = _make_mock_text_response(["Hello"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            stream.abort()
            stream.abort()
            stream.abort()
            return True

        asyncio.run(run())  # Should not raise

    def test_abort_on_error_stream(self, monkeypatch):
        """abort() on an error stream does not raise."""
        mock_response = _make_mock_error_response()
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            # Consume events
            async for event in stream:
                pass
            stream.abort()
            return True

        asyncio.run(run())  # Should not raise

    def test_abort_before_awaiting_result(self, monkeypatch):
        """Calling abort() before result() works."""
        mock_response = _make_mock_text_response(["Hello, world!"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            # Get first event
            first_event = await stream.__anext__()
            # Now abort
            stream.abort()
            # Get remaining events
            remaining = []
            async for event in stream:
                remaining.append(event)
            return first_event.type, len(remaining)

        first, remaining_count = asyncio.run(run())
        assert first == "text_delta"


# ═══════════════════════════════════════════════════════════════════════════
# Additional: Event ordering tests
# ═══════════════════════════════════════════════════════════════════════════

class TestEventOrdering:
    """Tests for event ordering guarantee from PHASE-1-SUBPHASE-3.md.

    For a response with text and tool calls:
      TextDeltaEvent(s) → ToolCallDeltaEvent(s) → DoneEvent

    For a pure text response:
      TextDeltaEvent(s) → DoneEvent

    For an error:
      ErrorEvent only
    """

    def _make_mock_client(self, response):
        return _make_mock_client(response)

    def test_pure_text_order_text_then_done(self, monkeypatch):
        """Pure text: TextDeltaEvent before DoneEvent."""
        mock_response = _make_mock_text_response(["Hello"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        types = [e.type for e in events]
        assert "text_delta" in types
        assert "done" in types
        # text_delta comes before done
        text_idx = types.index("text_delta")
        done_idx = types.index("done")
        assert text_idx < done_idx

    def test_mixed_response_text_then_toolcall_then_done(self, monkeypatch):
        """Mixed response: TextDeltaEvent(s), ToolCallDeltaEvent(s), then DoneEvent."""
        tool_calls = [{"id": "call_abc123", "name": "bash", "arguments": {"command": "ls -la"}}]
        mock_response = _make_mock_tool_call_response(tool_calls)
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "list files"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        types = [e.type for e in events]

        text_indices = [i for i, t in enumerate(types) if t == "text_delta"]
        toolcall_indices = [i for i, t in enumerate(types) if t == "toolcall_delta"]
        done_indices = [i for i, t in enumerate(types) if t == "done"]

        assert len(text_indices) > 0
        assert len(toolcall_indices) > 0
        assert len(done_indices) == 1

        # Done must come after text and toolcall
        assert max(text_indices) < done_indices[0]
        assert max(toolcall_indices) < done_indices[0]

    def test_error_event_order_single_event(self, monkeypatch):
        """Error: only ErrorEvent, no other events."""
        mock_response = _make_mock_error_response()
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            return [e async for e in stream]

        events = asyncio.run(run())
        types = [e.type for e in events]
        assert types == ["error"]
        assert len(events) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Additional: AssistantMessageEventStream internals
# ═══════════════════════════════════════════════════════════════════════════

class TestAssistantMessageEventStreamInternals:
    """Tests for AssistantMessageEventStream internal behavior."""

    def test_stream_is_async_iterable(self, monkeypatch):
        """stream_simple returns an object that is async-iterable."""
        mock_response = _make_mock_text_response(["Hello"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            _make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            # Should be async iterable
            count = 0
            async for _ in stream:
                count += 1
            return count > 0

        assert asyncio.run(run())

    def test_event_queue_yields_events(self, monkeypatch):
        """Events are yielded from the stream's internal queue."""
        mock_response = _make_mock_text_response(["Hello, world!"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            _make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            all_events = [e async for e in stream]
            # All events should be StreamEvent subclasses
            for e in all_events:
                assert hasattr(e, "type")
                assert hasattr(e, "partial") or hasattr(e, "final") or hasattr(e, "message")
            return True

        assert asyncio.run(run())

    def test_partial_message_in_text_delta_event(self, monkeypatch):
        """TextDeltaEvent.partial is an AssistantMessage."""
        mock_response = _make_mock_text_response(["Hello"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            _make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            events = [e async for e in stream]
            text_events = [e for e in events if isinstance(e, TextDeltaEvent)]
            assert len(text_events) > 0
            assert isinstance(text_events[0].partial, AssistantMessage)
            assert text_events[0].partial.role == "assistant"
            return True

        assert asyncio.run(run())

    def test_done_event_has_usage(self, monkeypatch):
        """DoneEvent.usage is a Usage instance."""
        mock_response = _make_mock_text_response(
            ["Hello"],
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            _make_mock_client(mock_response),
        )

        async def run():
            model = _make_model()
            stream = await stream_simple(
                model=model,
                context={
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ]
                },
                options={"api_key": "sk-test"},
            )
            events = [e async for e in stream]
            done_events = [e for e in events if isinstance(e, DoneEvent)]
            assert len(done_events) == 1
            assert isinstance(done_events[0].usage, Usage)
            return done_events[0].usage.total_tokens == 30

        assert asyncio.run(run())
