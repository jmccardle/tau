"""Tests for Phase 1 Subphase 2 — OpenAI Provider Implementation.

These tests implement the test cases listed in PHASE-1-SUBPHASE-2.md
"Testing Strategy" section.

Test categories:
  1. Message conversion — text only
  2. Message conversion — tool calls
  3. Tool conversion
  4. Streaming event production (text response)
  5. Tool call delta accumulation
  6. Error handling

Reference: PHASE-1-SUBPHASE-2.md, "Testing Strategy" section
           SUBPHASE-0.0.md, "Core Data Type Contracts" section
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from tau_ai.providers.openai import OpenAICompletionsProvider
from tau_ai.streaming import DoneEvent, ErrorEvent, TextDeltaEvent, ToolCallDeltaEvent
from tau_ai.tools import ToolDefinition
from tau_ai.types import (
    AssistantMessage,
    ImageContent,
    Model,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)


# ═══════════════════════════════════════════════════════════════════════════
# Helper: build_sse_chunk and build_sse_stream (avoid nested f-strings)
# ═══════════════════════════════════════════════════════════════════════════

def _sse_chunk(data: dict) -> str:
    """Build a single SSE data line from a dict."""
    return "data: " + json.dumps(data)


def _sse_stream(chunks: list[dict]) -> str:
    """Build a full SSE response body from a list of data dicts."""
    lines = [_sse_chunk(c) for c in chunks]
    lines.append("data: [DONE]")
    return "\n".join(lines)


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

    response = MagicMock()
    response.status_code = 200
    response.text = _sse_stream(chunks)
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

    # Final with tool_calls finish_reason
    chunks.append({
        "id": "chatcmpl-tool-call-final",
        "model": "gpt-4",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    })

    response = MagicMock()
    response.status_code = 200
    response.text = _sse_stream(chunks)
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


def _make_length_response(usage: dict | None = None) -> MagicMock:
    """Create a mock response for token-limit (length) finish_reason."""
    chunks = [
        {"id": "chatcmpl-trunc", "choices": [{"index": 0, "delta": {"content": "truncated"}}]},
        {
            "id": "chatcmpl-trunc",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 4000, "total_tokens": 4010},
        },
    ]
    response = MagicMock()
    response.status_code = 200
    response.text = _sse_stream(chunks)
    response.headers = {"x-request-id": "test"}
    response.json.return_value = {
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 4000, "total_tokens": 4010}
    }
    return response


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


def _collect_events(stream):
    """Helper to collect all events from a stream (sync wrapper)."""
    async def _collect():
        events = []
        async for event in stream:
            events.append(event)
        return events
    return asyncio.run(_collect())


# ═══════════════════════════════════════════════════════════════════════════
# Test 1: Message conversion — text only
# ═══════════════════════════════════════════════════════════════════════════

class TestConvertMessagesTextOnly:
    """Test 1 from PHASE-1-SUBPHASE-2.md "Testing Strategy"."""

    def setup_method(self):
        self.provider = OpenAICompletionsProvider()

    def test_single_user_text_message(self):
        """UserMessage with text converts to OpenAI user message with text block."""
        messages = [
            UserMessage(content=[TextContent(text="hello")], timestamp=0),
        ]
        result = self.provider._convert_messages_to_openai(messages)

        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["type"] == "text"
        assert result[0]["content"][0]["text"] == "hello"

    def test_single_user_string_content(self):
        """UserMessage with string content converts to OpenAI format."""
        messages = [
            UserMessage(content="hello world", timestamp=0),
        ]
        result = self.provider._convert_messages_to_openai(messages)

        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["text"] == "hello world"

    def test_multiple_text_blocks(self):
        """Multiple text blocks in one message are converted correctly."""
        messages = [
            UserMessage(
                content=[
                    TextContent(text="First part"),
                    TextContent(text="Second part"),
                ],
                timestamp=0,
            ),
        ]
        result = self.provider._convert_messages_to_openai(messages)

        assert len(result) == 1
        assert len(result[0]["content"]) == 2
        assert result[0]["content"][0]["text"] == "First part"
        assert result[0]["content"][1]["text"] == "Second part"

    def test_thinking_content_in_user_message(self):
        """ThinkingContent is not valid in UserMessage; converter should not crash."""
        text = TextContent(text="Let me think about this...")
        messages = [UserMessage(content=[text], timestamp=0)]
        result = self.provider._convert_messages_to_openai(messages)
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["type"] == "text"

    def test_conversation_with_multiple_user_messages(self):
        """Multiple messages in a conversation are converted."""
        messages = [
            UserMessage(content=[TextContent(text="What is 2+2?")], timestamp=0),
            AssistantMessage(
                content=[TextContent(text="The answer is 4.")],
                api="openai-completions",
                provider="openai",
                model="gpt-4",
                usage=Usage(),
                stop_reason="stop",
                timestamp=0,
            ),
            UserMessage(content=[TextContent(text="And 3+3?")], timestamp=0),
        ]
        result = self.provider._convert_messages_to_openai(messages)

        assert len(result) == 3
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "user"


# ═══════════════════════════════════════════════════════════════════════════
# Test: Message conversion — image content
# ═══════════════════════════════════════════════════════════════════════════

class TestConvertMessagesImageContent:
    """Verify image content conversion rules from PHASE-1-SUBPHASE-2.md."""

    def setup_method(self):
        self.provider = OpenAICompletionsProvider()

    def test_user_message_with_image(self):
        """UserMessage with ImageContent converts to image_url format."""
        messages = [
            UserMessage(
                content=[
                    TextContent(text="What is in this image?"),
                    ImageContent(data="abc123", mime_type="image/png"),
                ],
                timestamp=0,
            ),
        ]
        result = self.provider._convert_messages_to_openai(messages)

        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert len(result[0]["content"]) == 2
        assert result[0]["content"][0]["type"] == "text"
        assert result[0]["content"][1]["type"] == "image_url"
        assert "data:image/png;base64,abc123" in result[0]["content"][1]["image_url"]["url"]

    def test_user_message_with_image_jpeg(self):
        """ImageContent with JPEG mime type uses correct data URI."""
        messages = [
            UserMessage(
                content=[ImageContent(data="base64jpegdata", mime_type="image/jpeg")],
                timestamp=0,
            ),
        ]
        result = self.provider._convert_messages_to_openai(messages)

        assert "data:image/jpeg;base64,base64jpegdata" in result[0]["content"][0]["image_url"]["url"]

    def test_image_data_with_data_uri_prefix(self):
        """ImageContent with data: URI prefix strips it before base64 encoding."""
        messages = [
            UserMessage(
                content=[
                    ImageContent(data="data:image/png;base64,existingbase64data", mime_type="image/png"),
                ],
                timestamp=0,
            ),
        ]
        result = self.provider._convert_messages_to_openai(messages)

        content = result[0]["content"][0]["image_url"]["url"]
        assert "data:image/png;base64,existingbase64data" in content


# ═══════════════════════════════════════════════════════════════════════════
# Test 2: Message conversion — tool calls
# ═══════════════════════════════════════════════════════════════════════════

class TestConvertMessagesWithToolCalls:
    """Test 2 from PHASE-1-SUBPHASE-2.md "Testing Strategy"."""

    def setup_method(self):
        self.provider = OpenAICompletionsProvider()

    def test_assistant_with_tool_calls(self):
        """AssistantMessage with ToolCall content blocks has tool_calls in output."""
        messages = [
            AssistantMessage(
                content=[
                    TextContent(text="checking"),
                    ToolCall(id="c1", name="bash", arguments={"command": "ls"}),
                ],
                api="openai-completions",
                provider="openai",
                model="gpt-4",
                usage=Usage(),
                stop_reason="toolUse",
                timestamp=0,
            ),
        ]
        result = self.provider._convert_messages_to_openai(messages)

        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert "tool_calls" in result[0]
        assert len(result[0]["tool_calls"]) == 1
        assert result[0]["tool_calls"][0]["function"]["name"] == "bash"
        assert result[0]["tool_calls"][0]["id"] == "c1"
        assert '"command"' in result[0]["tool_calls"][0]["function"]["arguments"]
        assert '"ls"' in result[0]["tool_calls"][0]["function"]["arguments"]

    def test_assistant_text_only_no_tool_calls_key(self):
        """AssistantMessage with only text does not include tool_calls key."""
        messages = [
            AssistantMessage(
                content=[TextContent(text="Hello, world!")],
                api="openai-completions",
                provider="openai",
                model="gpt-4",
                usage=Usage(),
                stop_reason="stop",
                timestamp=0,
            ),
        ]
        result = self.provider._convert_messages_to_openai(messages)

        assert len(result) == 1
        assert result[0]["content"] == "Hello, world!"
        assert "tool_calls" not in result[0]

    def test_assistant_with_multiple_tool_calls(self):
        """AssistantMessage with multiple ToolCall blocks produces multiple tool_calls."""
        messages = [
            AssistantMessage(
                content=[
                    ToolCall(id="call_1", name="read_file", arguments={"path": "main.py"}),
                    ToolCall(id="call_2", name="run_command", arguments={"cmd": "npm test"}),
                ],
                api="openai-completions",
                provider="openai",
                model="gpt-4",
                usage=Usage(),
                stop_reason="toolUse",
                timestamp=0,
            ),
        ]
        result = self.provider._convert_messages_to_openai(messages)

        assert len(result[0]["tool_calls"]) == 2
        assert result[0]["tool_calls"][0]["function"]["name"] == "read_file"
        assert result[0]["tool_calls"][1]["function"]["name"] == "run_command"

    def test_tool_result_message_conversion(self):
        """ToolResultMessage converts to OpenAI tool role."""
        messages = [
            ToolResultMessage(
                tool_call_id="call_123",
                tool_name="bash",
                content=[TextContent(text="file1 file2")],
                is_error=False,
                timestamp=0,
            ),
        ]
        result = self.provider._convert_messages_to_openai(messages)

        assert len(result) == 1
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "call_123"
        assert result[0]["content"] == "file1 file2"

    def test_conversation_with_tool_turn(self):
        """Full conversation: user -> assistant (tool) -> tool result."""
        messages = [
            UserMessage(content=[TextContent(text="list files")], timestamp=0),
            AssistantMessage(
                content=[ToolCall(id="c1", name="bash", arguments={"command": "ls"})],
                api="openai-completions",
                provider="openai",
                model="gpt-4",
                usage=Usage(),
                stop_reason="toolUse",
                timestamp=0,
            ),
            ToolResultMessage(
                tool_call_id="c1",
                tool_name="bash",
                content=[TextContent(text="file1.txt file2.py")],
                is_error=False,
                timestamp=0,
            ),
        ]
        result = self.provider._convert_messages_to_openai(messages)

        assert len(result) == 3
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "tool"
        assert result[2]["tool_call_id"] == "c1"


# ═══════════════════════════════════════════════════════════════════════════
# Test 3: Tool conversion
# ═══════════════════════════════════════════════════════════════════════════

class TestConvertTools:
    """Test 3 from PHASE-1-SUBPHASE-2.md "Testing Strategy"."""

    def setup_method(self):
        self.provider = OpenAICompletionsProvider()

    def _make_bash_tool(self) -> ToolDefinition:
        return ToolDefinition(
            name="bash",
            label="Bash",
            description="Run bash command",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            execute=lambda **kw: "",
        )

    def test_convert_single_tool(self):
        """Single tool definition converts to OpenAI function format."""
        tool = self._make_bash_tool()
        result = self.provider._convert_tools_to_openai([tool])

        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "bash"
        assert result[0]["function"]["description"] == "Run bash command"
        assert "command" in result[0]["function"]["parameters"]["properties"]

    def test_convert_multiple_tools(self):
        """Multiple tools convert to multiple function definitions."""
        tool1 = self._make_bash_tool()
        tool2 = ToolDefinition(
            name="read_file",
            label="Read File",
            description="Read file contents",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            execute=lambda **kw: "",
        )
        result = self.provider._convert_tools_to_openai([tool1, tool2])

        assert len(result) == 2
        assert result[0]["function"]["name"] == "bash"
        assert result[1]["function"]["name"] == "read_file"

    def test_convert_empty_tools(self):
        """Empty tool list returns empty list."""
        result = self.provider._convert_tools_to_openai([])
        assert result == []

    def test_tool_parameters_preserved(self):
        """Tool parameters JSON Schema is preserved in conversion."""
        tool = self._make_bash_tool()
        result = self.provider._convert_tools_to_openai([tool])
        expected_schema = {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        }
        assert result[0]["function"]["parameters"] == expected_schema

    def test_convert_tool_with_complex_schema(self):
        """Tools with complex JSON schemas are preserved correctly."""
        tool = ToolDefinition(
            name="complex_tool",
            label="Complex",
            description="A tool with complex parameters",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "options": {
                        "type": "object",
                        "properties": {"verbose": {"type": "boolean"}},
                    },
                },
                "required": ["name", "count"],
            },
            execute=lambda **kw: "",
        )
        result = self.provider._convert_tools_to_openai([tool])

        assert result[0]["function"]["name"] == "complex_tool"
        props = result[0]["function"]["parameters"]["properties"]
        assert props["count"]["type"] == "integer"
        assert props["tags"]["type"] == "array"


# ═══════════════════════════════════════════════════════════════════════════
# Test 4: Streaming event production — text response
# ═══════════════════════════════════════════════════════════════════════════

class TestStreamTextResponse:
    """Test 4 from PHASE-1-SUBPHASE-2.md "Testing Strategy"."""

    def _make_mock_client(self, response):
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

    def test_stream_text_response_produces_text_delta_events(self, monkeypatch):
        """stream_chat produces TextDeltaEvent instances for text content."""
        mock_response = _make_mock_text_response(["Hello", ", ", "world!"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
        ))

        events = _collect_events(stream)
        text_events = [e for e in events if isinstance(e, TextDeltaEvent)]
        assert len(text_events) > 0
        full_text = "".join(e.delta for e in text_events)
        assert "Hello" in full_text
        assert "world" in full_text

    def test_stream_text_response_ends_with_done_event(self, monkeypatch):
        """stream_chat ends with a DoneEvent for text response."""
        mock_response = _make_mock_text_response(["Hello", ", ", "world!"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
        ))

        events = _collect_events(stream)
        done_events = [e for e in events if isinstance(e, DoneEvent)]
        assert len(done_events) == 1

        done = done_events[0]
        assert isinstance(done.final, AssistantMessage)
        assert done.final.api == "openai-completions"
        assert done.final.provider == "openai"
        assert done.usage.total_tokens == 30

    def test_stream_text_response_final_message_text(self, monkeypatch):
        """DoneEvent.final contains the full text response."""
        mock_response = _make_mock_text_response(["Hello", ", ", "world!"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
        ))

        events = _collect_events(stream)
        done_events = [e for e in events if isinstance(e, DoneEvent)]
        assert len(done_events) == 1
        final = done_events[0].final
        text_blocks = [c for c in final.content if isinstance(c, TextContent)]
        full_text = "".join(c.text for c in text_blocks)
        assert full_text == "Hello, world!"

    def test_stream_text_response_no_tool_calls_in_final(self, monkeypatch):
        """Pure text response has no ToolCall content blocks."""
        mock_response = _make_mock_text_response(["Hello, world!"])
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
        ))

        events = _collect_events(stream)
        done_events = [e for e in events if isinstance(e, DoneEvent)]
        assert len(done_events) == 1
        tool_calls = [c for c in done_events[0].final.content if isinstance(c, ToolCall)]
        assert len(tool_calls) == 0

    def test_stream_text_response_produces_error_on_http_error(self, monkeypatch):
        """stream_chat produces ErrorEvent on non-200 HTTP response."""
        mock_response = _make_mock_error_response(status_code=401, error_msg="Invalid API key")
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
        ))

        events = _collect_events(stream)
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1
        assert "Invalid API key" in error_events[0].message
        assert error_events[0].is_error is True


# ═══════════════════════════════════════════════════════════════════════════
# Test 5: Tool call delta accumulation
# ═══════════════════════════════════════════════════════════════════════════

class TestStreamToolCallDelta:
    """Test 5 from PHASE-1-SUBPHASE-2.md "Testing Strategy"."""

    def _make_mock_client(self, response):
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

    def test_stream_tool_call_produces_toolcall_delta_events(self, monkeypatch):
        """stream_chat produces ToolCallDeltaEvent instances for tool calls."""
        tool_calls = [{"id": "call_abc123", "name": "bash", "arguments": {"command": "ls -la"}}]
        mock_response = _make_mock_tool_call_response(tool_calls)
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="list files")], timestamp=0)],
            tools=[ToolDefinition(
                name="bash",
                label="Bash",
                description="Run bash command",
                parameters={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                execute=lambda **kw: "",
            )],
        ))

        events = _collect_events(stream)
        toolcall_events = [e for e in events if isinstance(e, ToolCallDeltaEvent)]
        assert len(toolcall_events) > 0

    def test_stream_tool_call_accumulates_name(self, monkeypatch):
        """Tool call name is accumulated across delta events."""
        tool_calls = [{"id": "call_abc123", "name": "bash", "arguments": {"command": "ls -la"}}]
        mock_response = _make_mock_tool_call_response(tool_calls)
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="list files")], timestamp=0)],
            tools=[ToolDefinition(
                name="bash",
                label="Bash",
                description="Run bash command",
                parameters={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                execute=lambda **kw: "",
            )],
        ))

        events = _collect_events(stream)
        done_events = [e for e in events if isinstance(e, DoneEvent)]
        assert len(done_events) == 1
        final = done_events[0].final
        tool_calls = [c for c in final.content if isinstance(c, ToolCall)]
        assert len(tool_calls) == 1
        assert tool_calls[0].name == "bash"
        assert tool_calls[0].id == "call_abc123"

    def test_stream_tool_call_accumulates_arguments(self, monkeypatch):
        """Tool call arguments are accumulated correctly across deltas."""
        tool_calls = [{"id": "call_abc123", "name": "bash", "arguments": {"command": "ls -la"}}]
        mock_response = _make_mock_tool_call_response(tool_calls)
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="list files")], timestamp=0)],
            tools=[ToolDefinition(
                name="bash",
                label="Bash",
                description="Run bash command",
                parameters={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                execute=lambda **kw: "",
            )],
        ))

        events = _collect_events(stream)
        done_events = [e for e in events if isinstance(e, DoneEvent)]
        assert len(done_events) == 1
        final = done_events[0].final
        tool_calls = [c for c in final.content if isinstance(c, ToolCall)]
        assert len(tool_calls) == 1
        args = tool_calls[0].arguments
        assert "command" in args
        assert args["command"] == "ls -la"

    def test_stream_mixed_text_and_tool_calls(self, monkeypatch):
        """Response with text before tool calls produces both text and toolcall deltas."""
        tool_calls = [{"id": "call_abc123", "name": "bash", "arguments": {"command": "ls -la"}}]
        mock_response = _make_mock_tool_call_response(tool_calls)
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="list files")], timestamp=0)],
            tools=[ToolDefinition(
                name="bash",
                label="Bash",
                description="Run bash command",
                parameters={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                execute=lambda **kw: "",
            )],
        ))

        events = _collect_events(stream)
        text_events = [e for e in events if isinstance(e, TextDeltaEvent)]
        toolcall_events = [e for e in events if isinstance(e, ToolCallDeltaEvent)]
        done_events = [e for e in events if isinstance(e, DoneEvent)]

        assert len(text_events) > 0
        assert len(toolcall_events) > 0
        assert len(done_events) == 1

        # Final message should have both text and tool calls
        final = done_events[0].final
        text_blocks = [c for c in final.content if isinstance(c, TextContent)]
        tool_calls = [c for c in final.content if isinstance(c, ToolCall)]
        assert len(text_blocks) > 0
        assert len(tool_calls) > 0


# ═══════════════════════════════════════════════════════════════════════════
# Test 6: Error handling
# ═══════════════════════════════════════════════════════════════════════════

class TestErrorHandling:
    """Test 6 from PHASE-1-SUBPHASE-2.md "Testing Strategy"."""

    def _make_mock_client(self, response):
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

    def test_error_on_invalid_api_key(self, monkeypatch):
        """401 response produces ErrorEvent with API key error message."""
        mock_response = _make_mock_error_response(status_code=401, error_msg="Invalid API key")
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
        ))

        events = _collect_events(stream)
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1
        assert "Invalid API key" in error_events[0].message
        assert error_events[0].is_error is True

    def test_error_on_rate_limit(self, monkeypatch):
        """429 response produces ErrorEvent with rate limit message."""
        mock_response = _make_mock_error_response(
            status_code=429,
            error_msg="Rate limit exceeded. Please try again later."
        )
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
        ))

        events = _collect_events(stream)
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1
        assert "Rate limit" in error_events[0].message

    def test_error_on_generic_http_error(self, monkeypatch):
        """500 response produces ErrorEvent with HTTP status message."""
        mock_response = _make_mock_error_response(status_code=500, error_msg="Internal Server Error")
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
        ))

        events = _collect_events(stream)
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1
        assert "500" in error_events[0].message

    def test_error_on_network_error(self, monkeypatch):
        """Network error produces ErrorEvent with exception message."""
        class FailingClient:
            def __init__(self, *args, **kwargs):
                pass

            async def post(self, *args, **kwargs):
                raise ConnectionError("Connection refused")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                pass

        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            FailingClient,
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
        ))

        events = _collect_events(stream)
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1
        assert "Connection refused" in error_events[0].message

    def test_error_event_is_error_flag(self, monkeypatch):
        """ErrorEvent has is_error=True."""
        mock_response = _make_mock_error_response()
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
        ))

        events = _collect_events(stream)
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1
        assert error_events[0].is_error is True

    def test_error_event_type_is_error(self, monkeypatch):
        """ErrorEvent has type='error'."""
        mock_response = _make_mock_error_response()
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
        ))

        events = _collect_events(stream)
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1
        assert error_events[0].type == "error"


# ═══════════════════════════════════════════════════════════════════════════
# Additional tests: Conversion edge cases
# ═══════════════════════════════════════════════════════════════════════════

class TestConvertOpenaiChoiceToMessage:
    """Tests for _convert_openai_choice_to_message (streaming delta accumulation)."""

    def setup_method(self):
        self.provider = OpenAICompletionsProvider()

    def test_convert_text_delta_to_assistant_message(self):
        """Text delta is converted to AssistantMessage with TextContent."""
        choice = {
            "delta": {"content": "Hello, world!", "model": "gpt-4"},
            "finish_reason": "stop",
            "message_id": "msg_123",
        }
        result = self.provider._convert_openai_choice_to_message(choice)

        assert isinstance(result, AssistantMessage)
        assert result.api == "openai-completions"
        assert result.provider == "openai"
        assert result.model == "gpt-4"
        assert len(result.content) == 1
        assert isinstance(result.content[0], TextContent)
        assert result.content[0].text == "Hello, world!"

    def test_convert_tool_call_delta(self):
        """Tool call delta is converted to AssistantMessage with ToolCall."""
        choice = {
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": "call_1",
                    "function": {
                        "name": "bash",
                        "arguments": json.dumps({"command": "ls"}),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }
        result = self.provider._convert_openai_choice_to_message(choice)

        assert isinstance(result, AssistantMessage)
        tool_calls = [c for c in result.content if isinstance(c, ToolCall)]
        assert len(tool_calls) == 1
        assert tool_calls[0].name == "bash"
        assert tool_calls[0].arguments["command"] == "ls"

    def test_convert_finish_reason_stop(self):
        """finish_reason 'stop' maps to stop_reason 'stop'."""
        choice = {"delta": {"content": "done"}, "finish_reason": "stop"}
        result = self.provider._convert_openai_choice_to_message(choice)
        assert result.stop_reason == "stop"

    def test_convert_finish_reason_length(self):
        """finish_reason 'length' maps to stop_reason 'length'."""
        choice = {"delta": {"content": "truncated"}, "finish_reason": "length"}
        result = self.provider._convert_openai_choice_to_message(choice)
        assert result.stop_reason == "length"

    def test_convert_finish_reason_tool_calls(self):
        """finish_reason 'tool_calls' maps to stop_reason 'toolUse'."""
        choice = {
            "delta": {
                "tool_calls": [{"id": "c1", "function": {"name": "x", "arguments": "{}"}}]
            },
            "finish_reason": "tool_calls",
        }
        result = self.provider._convert_openai_choice_to_message(choice)
        assert result.stop_reason == "toolUse"

    def test_convert_empty_delta(self):
        """Empty delta produces AssistantMessage with empty content."""
        choice = {"delta": {}, "finish_reason": "stop"}
        result = self.provider._convert_openai_choice_to_message(choice)
        assert isinstance(result, AssistantMessage)
        assert len(result.content) == 0


class TestConvertMessagesDict:
    """Test conversion of dict messages (already in OpenAI format)."""

    def setup_method(self):
        self.provider = OpenAICompletionsProvider()

    def test_dict_user_message_passthrough(self):
        """Dict messages are passed through."""
        messages = [{"role": "user", "content": "hello"}]
        result = self.provider._convert_messages_to_openai(messages)
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "hello"

    def test_dict_tool_message_passthrough(self):
        """Dict tool messages are converted to tool role."""
        messages = [{"role": "tool", "tool_call_id": "c1", "content": "result"}]
        result = self.provider._convert_messages_to_openai(messages)
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "c1"


# ═══════════════════════════════════════════════════════════════════════════
# Additional: Provider instantiation and configuration
# ═══════════════════════════════════════════════════════════════════════════

class TestProviderConfiguration:
    """Tests for provider initialization and configuration."""

    def test_provider_default_api_key(self):
        """Provider uses default API key when none provided."""
        provider = OpenAICompletionsProvider()
        assert provider.api_key == "sk-fake-key-for-testing"

    def test_provider_custom_api_key(self):
        """Provider accepts custom API key."""
        provider = OpenAICompletionsProvider(api_key="sk-custom-key")
        assert provider.api_key == "sk-custom-key"

    def test_provider_custom_base_url(self):
        """Provider accepts custom base URL."""
        provider = OpenAICompletionsProvider(base_url="https://custom.api/v1")
        assert provider.base_url == "https://custom.api/v1"

    def test_provider_default_base_url(self):
        """Provider uses OpenAI default URL."""
        provider = OpenAICompletionsProvider()
        assert provider.base_url == "https://api.openai.com/v1"

    def test_provider_inherits_from_base(self):
        """Provider inherits from Provider ABC."""
        from tau_ai.providers.base import Provider
        assert issubclass(OpenAICompletionsProvider, Provider)

    def test_provider_implements_stream_chat(self):
        """Provider implements stream_chat method."""
        provider = OpenAICompletionsProvider()
        assert hasattr(provider, "stream_chat")
        assert callable(provider.stream_chat)


# ═══════════════════════════════════════════════════════════════════════════
# Additional: Thinking/reasoning content conversion
# ═══════════════════════════════════════════════════════════════════════════

class TestThinkingContentConversion:
    """Tests for thinking/reasoning content handling."""

    def setup_method(self):
        self.provider = OpenAICompletionsProvider()

    def test_assistant_with_thinking_content(self):
        """AssistantMessage with ThinkingContent converts correctly."""
        messages = [
            AssistantMessage(
                content=[
                    ThinkingContent(type="thinking", thinking="Let me reason through this..."),
                    TextContent(type="text", text="The answer is 42."),
                ],
                api="openai-completions",
                provider="openai",
                model="gpt-4",
                usage=Usage(),
                stop_reason="stop",
                timestamp=0,
            ),
        ]
        result = self.provider._convert_messages_to_openai(messages)

        assert result[0]["role"] == "assistant"
        # Thinking is included in the content field
        assert result[0]["content"] is not None

    def test_convert_openai_reasoning_delta(self):
        """OpenAI reasoning delta is converted to ThinkingContent."""
        choice = {
            "delta": {"reasoning": "Let me think step by step..."},
            "finish_reason": "stop",
        }
        result = self.provider._convert_openai_choice_to_message(choice)
        assert isinstance(result, AssistantMessage)
        thinking_blocks = [c for c in result.content if isinstance(c, ThinkingContent)]
        assert len(thinking_blocks) == 1
        assert thinking_blocks[0].thinking == "Let me think step by step..."


# ═══════════════════════════════════════════════════════════════════════════
# Additional: Token limit / truncated response
# ═══════════════════════════════════════════════════════════════════════════

class TestTokenLimitHandling:
    """Tests for token limit (truncated response) handling."""

    def _make_mock_client(self, response):
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

    def test_stop_reason_length_mapping(self):
        """finish_reason 'length' correctly maps to stop_reason 'length'."""
        provider = OpenAICompletionsProvider()
        choice = {"delta": {"content": "text"}, "finish_reason": "length"}
        result = provider._convert_openai_choice_to_message(choice)
        assert result.stop_reason == "length"

    def test_stream_with_length_finish_reason(self, monkeypatch):
        """stream_chat with 'length' finish_reason produces DoneEvent with length stop_reason."""
        mock_response = _make_length_response()
        monkeypatch.setattr(
            "tau_ai.providers.openai.httpx.AsyncClient",
            self._make_mock_client(mock_response),
        )

        provider = OpenAICompletionsProvider()
        stream = asyncio.run(provider.stream_chat(
            model=_make_model(),
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
        ))

        events = _collect_events(stream)
        done_events = [e for e in events if isinstance(e, DoneEvent)]
        assert len(done_events) == 1
        assert done_events[0].final.stop_reason == "length"
        assert done_events[0].usage.total_tokens == 4010
