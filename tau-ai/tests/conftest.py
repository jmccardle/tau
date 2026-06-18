"""tau-ai test fixtures.

Provides shared fixtures for tau-ai package tests:
- mock_openai_client: Mock AsyncOpenAI client
- sample_messages: List of UserMessage/AssistantMessage/ToolResultMessage
- sample_tool_call: A ToolCall with valid arguments
- Provider auto-registration for stream_simple tests

Reference: SUBPHASE-0.0.md lines 260-340
"""

import pytest

from tau_ai.types import (
    UserMessage,
    AssistantMessage,
    ToolResultMessage,
    ToolCall,
    TextContent,
    Usage,
)


@pytest.fixture
def mock_openai_client():
    """Fixture providing a mock AsyncOpenAI client.

    Returns a mock object with the interface expected by tau_ai.client:
    - client.chat.completions.create() -> async generator
    - client.chat.completions.acreate() -> async generator

    Usage in tests:
        async def test_streaming(mock_openai_client):
            # Configure mock
            mock_openai_client.chat.completions.create = mock_async_gen([
                {"choices": [{"delta": {"content": "Hello"}}]}
            ])
    """
    import unittest.mock as mock

    class MockAsyncIterator:
        def __init__(self, items):
            self._items = items
            self._index = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._index >= len(self._items):
                raise StopAsyncIteration
            item = self._items[self._index]
            self._index += 1
            return item

    client_mock = mock.MagicMock()
    client_mock.chat.completions.create.return_value = MockAsyncIterator([])

    # Also need the responses API mock
    client_mock.responses = mock.MagicMock()

    return client_mock


@pytest.fixture
def sample_messages():
    """Fixture providing a list of sample messages.

    Returns:
        list[UserMessage | AssistantMessage | ToolResultMessage]:
        A realistic conversation with one user message,
        one assistant message with tool calls,
        and one tool result message.
    """
    user_msg = UserMessage(
        role="user",
        content="What files are in the current directory?",
        timestamp=1700000000000,
    )

    assistant_msg = AssistantMessage(
        role="assistant",
        content=[
            TextContent(type="text", text="Let me check the directory."),
            ToolCall(
                type="toolCall",
                id="call_abc123",
                name="ls",
                arguments={"path": "."},
            ),
        ],
        api="openai-completions",
        provider="openai",
        model="gpt-4",
        response_id="resp_abc123",
        usage=Usage(
            input_tokens=50,
            output_tokens=20,
            total_tokens=70,
        ),
        stop_reason="toolUse",
        timestamp=1700000001000,
    )

    tool_result_msg = ToolResultMessage(
        role="toolResult",
        tool_call_id="call_abc123",
        tool_name="ls",
        content=[
            TextContent(type="text", text="file1.txt\nfile2.py\ndir1/"),
        ],
        details={"exit_code": 0},
        is_error=False,
        timestamp=1700000002000,
    )

    return [user_msg, assistant_msg, tool_result_msg]


@pytest.fixture
def sample_tool_call():
    """Fixture providing a sample ToolCall.

    Returns:
        ToolCall: A valid tool call with id, name, and arguments.
    """
    return ToolCall(
        type="toolCall",
        id="call_test_001",
        name="read_file",
        arguments={"path": "src/main.py", "offset": 0, "limit": 100},
    )
