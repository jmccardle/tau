"""Tests for ChatMessageData widget data contract.

Reference: PHASE-4-SUBPHASE-0.md — ChatMessageData Contract
Reference: SUBPHASE-0.0.md — AgentEvent.message field → widget data mapping
"""

import dataclasses
import pytest

from tau_coding_agent.widgets.chat_display import ChatMessageData


class TestChatMessageDataImport:
    """Test that ChatMessageData is importable."""

    def test_chat_message_data_is_importable(self):
        """ChatMessageData must be importable from widgets.chat_display."""
        from tau_coding_agent.widgets.chat_display import ChatMessageData
        assert ChatMessageData is not None

    def test_chat_message_data_in_widgets_init(self):
        """ChatMessageData must be re-exported from widgets.__init__."""
        from tau_coding_agent.widgets import ChatMessageData as C
        assert C is ChatMessageData


class TestChatMessageDataIsDataclass:
    """Test that ChatMessageData is a proper dataclass."""

    def test_is_dataclass(self):
        """ChatMessageData must be a dataclass."""
        assert dataclasses.is_dataclass(ChatMessageData)

    def test_has_all_required_fields(self):
        """ChatMessageData must have role and content fields."""
        field_names = {f.name for f in dataclasses.fields(ChatMessageData)}
        assert "role" in field_names
        assert "content" in field_names

    def test_has_all_optional_fields(self):
        """ChatMessageData must have timestamp, streaming, tool_name, tool_call_id, is_error."""
        field_names = {f.name for f in dataclasses.fields(ChatMessageData)}
        assert "timestamp" in field_names
        assert "streaming" in field_names
        assert "tool_name" in field_names
        assert "tool_call_id" in field_names
        assert "is_error" in field_names


class TestChatMessageDataConstruction:
    """Test ChatMessageData construction and defaults."""

    def test_minimal_construction(self):
        """ChatMessageData can be constructed with just role and content."""
        msg = ChatMessageData(role="user", content=[{"type": "text", "text": "hello"}])
        assert msg.role == "user"
        assert msg.content == [{"type": "text", "text": "hello"}]

    def test_defaults(self):
        """ChatMessageData optional fields have correct defaults."""
        msg = ChatMessageData(role="assistant", content=[{"type": "text", "text": "hi"}])
        assert msg.timestamp is None
        assert msg.streaming is False
        assert msg.tool_name is None
        assert msg.tool_call_id is None
        assert msg.is_error is False

    def test_full_construction(self):
        """ChatMessageData accepts all fields."""
        msg = ChatMessageData(
            role="toolResult",
            content=[{"type": "text", "text": "Tool output"}],
            timestamp=1234567890,
            streaming=True,
            tool_name="bash",
            tool_call_id="call_abc123",
            is_error=True,
        )
        assert msg.role == "toolResult"
        assert msg.timestamp == 1234567890
        assert msg.streaming is True
        assert msg.tool_name == "bash"
        assert msg.tool_call_id == "call_abc123"
        assert msg.is_error is True

    def test_user_role(self):
        """ChatMessageData accepts role='user'."""
        msg = ChatMessageData(role="user", content=[{"type": "text", "text": "prompt"}])
        assert msg.role == "user"

    def test_assistant_role(self):
        """ChatMessageData accepts role='assistant'."""
        msg = ChatMessageData(role="assistant", content=[{"type": "text", "text": "response"}])
        assert msg.role == "assistant"

    def test_toolresult_role(self):
        """ChatMessageData accepts role='toolResult'."""
        msg = ChatMessageData(role="toolResult", content=[])
        assert msg.role == "toolResult"


class TestChatMessageDataContent:
    """Test ChatMessageData.content field handling."""

    def test_content_accepts_text_block(self):
        """content accepts a text ContentBlock dict."""
        msg = ChatMessageData(
            role="assistant",
            content=[{"type": "text", "text": "Hello, world!"}]
        )
        assert len(msg.content) == 1
        assert msg.content[0]["type"] == "text"

    def test_content_accepts_multiple_blocks(self):
        """content accepts multiple ContentBlock dicts."""
        msg = ChatMessageData(
            role="assistant",
            content=[
                {"type": "text", "text": "Thinking..."},
                {"type": "toolCall", "id": "call_1", "name": "bash", "arguments": {"cmd": "ls"}},
                {"type": "text", "text": "Done."},
            ]
        )
        assert len(msg.content) == 3

    def test_content_accepts_thinking_block(self):
        """content accepts a thinking ContentBlock dict."""
        msg = ChatMessageData(
            role="assistant",
            content=[{"type": "thinking", "thinking": "Let me think...", "cached_tokens": 0}]
        )
        assert msg.content[0]["type"] == "thinking"

    def test_content_accepts_image_block(self):
        """content accepts an image ContentBlock dict."""
        msg = ChatMessageData(
            role="user",
            content=[
                {"type": "text", "text": "What is this?"},
                {"type": "image", "data": "base64data", "mime_type": "image/png"},
            ]
        )
        assert msg.content[1]["type"] == "image"

    def test_content_accepts_empty_list(self):
        """content accepts an empty list."""
        msg = ChatMessageData(role="assistant", content=[])
        assert msg.content == []

    def test_content_is_list(self):
        """content must be a list."""
        msg = ChatMessageData(role="assistant", content=[])
        assert isinstance(msg.content, list)


class TestChatMessageDataToolFields:
    """Test ChatMessageData tool-specific fields."""

    def test_tool_fields_on_assistant_message(self):
        """Assistant message with tool call has tool fields set."""
        msg = ChatMessageData(
            role="assistant",
            content=[{"type": "toolCall", "id": "call_x", "name": "bash", "arguments": {}}],
            tool_name="bash",
            tool_call_id="call_x",
        )
        assert msg.tool_name == "bash"
        assert msg.tool_call_id == "call_x"

    def test_tool_fields_none_for_non_tool_message(self):
        """Non-tool message has None tool fields."""
        msg = ChatMessageData(role="assistant", content=[{"type": "text", "text": "hello"}])
        assert msg.tool_name is None
        assert msg.tool_call_id is None

    def test_is_error_on_error_message(self):
        """is_error is True for error messages."""
        msg = ChatMessageData(
            role="assistant",
            content=[],
            is_error=True,
        )
        assert msg.is_error is True

    def test_is_error_false_for_normal_message(self):
        """is_error is False for normal messages."""
        msg = ChatMessageData(role="assistant", content=[{"type": "text", "text": "ok"}])
        assert msg.is_error is False


class TestChatMessageDataStreaming:
    """Test ChatMessageData streaming state."""

    def test_streaming_true_during_update(self):
        """streaming is True during message updates."""
        msg = ChatMessageData(
            role="assistant",
            content=[{"type": "text", "text": "partial"}],
            streaming=True,
        )
        assert msg.streaming is True

    def test_streaming_false_for_completed(self):
        """streaming is False for completed messages."""
        msg = ChatMessageData(
            role="assistant",
            content=[{"type": "text", "text": "complete"}],
            streaming=False,
        )
        assert msg.streaming is False


class TestChatMessageDataTimestamp:
    """Test ChatMessageData timestamp handling."""

    def test_timestamp_is_none_by_default(self):
        """timestamp defaults to None."""
        msg = ChatMessageData(role="user", content=[])
        assert msg.timestamp is None

    def test_timestamp_accepts_int(self):
        """timestamp accepts an integer (ms since epoch)."""
        msg = ChatMessageData(role="user", content=[], timestamp=1700000000000)
        assert msg.timestamp == 1700000000000
