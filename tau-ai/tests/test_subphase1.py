"""Tests for Phase 1 Subphase 1 — Core Types Implementation.

These tests implement the exact test cases listed in PHASE-1-SUBPHASE-1.md
"Testing Strategy" section.

Test categories:
  1. Message round-trip serialization
  2. ContentBlock type discrimination
  3. Tool argument validation
  4. AbortSignal thread safety (async)
  5. Model serialization to OpenAI format

Reference: PHASE-1-SUBPHASE-1.md, "Testing Strategy" section
           SUBPHASE-0.0.md, "Core Data Type Contracts" section
"""

import asyncio
import threading
import time

import pytest

from tau_ai import Model, Usage, UserMessage, AssistantMessage
from tau_ai.types import (
    TextContent,
    ThinkingContent,
    ImageContent,
    ToolCall,
    ToolResultMessage,
)
from tau_ai.tools import ToolDefinition, validate_tool_arguments
from tau_ai.abort import AbortSignal


# ═══════════════════════════════════════════════════════════════════════════
# Test 1: Message round-trip serialization
# ═══════════════════════════════════════════════════════════════════════════

class TestMessageRoundtrip:
    """Test 1 from PHASE-1-SUBPHASE-1.md "Testing Strategy".

    Verify messages serialize to dicts and can be deserialized back.
    """

    def test_user_message_roundtrip(self):
        """UserMessage serializes and deserializes correctly."""
        msg = UserMessage(content=[TextContent(text="hello")], timestamp=1700000000000)
        d = msg.model_dump()

        assert d["role"] == "user"
        assert d["content"][0]["type"] == "text"
        assert d["content"][0]["text"] == "hello"

        recovered = UserMessage.model_validate(d)
        assert recovered.content[0].text == "hello"

    def test_user_message_string_content_roundtrip(self):
        """UserMessage with string content round-trips correctly."""
        msg = UserMessage(content="hello world", timestamp=0)
        d = msg.model_dump()

        assert d["role"] == "user"
        assert d["content"] == "hello world"

        recovered = UserMessage.model_validate(d)
        assert recovered.content == "hello world"

    def test_user_message_image_content_roundtrip(self):
        """UserMessage with image content round-trips correctly."""
        msg = UserMessage(
            content=[
                TextContent(text="see this image"),
                ImageContent(data="base64encodedimage", mime_type="image/png"),
            ],
            timestamp=0,
        )
        d = msg.model_dump()
        recovered = UserMessage.model_validate(d)
        assert len(recovered.content) == 2
        assert recovered.content[0].type == "text"
        assert recovered.content[1].type == "image"
        assert recovered.content[1].data == "base64encodedimage"

    def test_assistant_message_roundtrip(self):
        """AssistantMessage serializes and deserializes correctly."""
        msg = AssistantMessage(
            content=[
                TextContent(type="text", text="I'll check that for you."),
                ToolCall(
                    type="toolCall",
                    id="call_abc123",
                    name="read_file",
                    arguments={"path": "src/main.py"},
                ),
            ],
            api="openai-completions",
            provider="openai",
            model="gpt-4",
            usage=Usage(input_tokens=100, output_tokens=50),
            stop_reason="toolUse",
            timestamp=1700000000000,
        )
        d = msg.model_dump()

        assert d["role"] == "assistant"
        assert d["api"] == "openai-completions"
        assert d["provider"] == "openai"
        assert d["model"] == "gpt-4"
        assert d["stop_reason"] == "toolUse"
        assert len(d["content"]) == 2

        recovered = AssistantMessage.model_validate(d)
        assert recovered.content[0].text == "I'll check that for you."
        assert recovered.content[1].name == "read_file"
        assert recovered.content[1].arguments == {"path": "src/main.py"}

    def test_tool_result_message_roundtrip(self):
        """ToolResultMessage serializes and deserializes correctly."""
        msg = ToolResultMessage(
            tool_call_id="call_123",
            tool_name="ls",
            content=[TextContent(text="file1.txt\nfile2.py")],
            details={"exit_code": 0},
            is_error=False,
            timestamp=1700000000000,
        )
        d = msg.model_dump()
        recovered = ToolResultMessage.model_validate(d)

        assert recovered.role == "toolResult"
        assert recovered.tool_call_id == "call_123"
        assert recovered.tool_name == "ls"
        assert not recovered.is_error

    def test_thinking_content_roundtrip(self):
        """ThinkingContent round-trips correctly."""
        msg = AssistantMessage(
            content=[
                ThinkingContent(
                    type="thinking",
                    thinking="Let me think about the best way to do this.",
                    cached_tokens=50,
                ),
                TextContent(type="text", text="Here's my answer."),
            ],
            api="openai-completions",
            provider="openai",
            model="gpt-4",
            usage=Usage(),
            stop_reason="stop",
            timestamp=0,
        )
        d = msg.model_dump()
        recovered = AssistantMessage.model_validate(d)
        assert recovered.content[0].type == "thinking"
        assert recovered.content[0].thinking == "Let me think about the best way to do this."
        assert recovered.content[0].cached_tokens == 50


# ═══════════════════════════════════════════════════════════════════════════
# Test 2: ContentBlock type discrimination
# ═══════════════════════════════════════════════════════════════════════════

class TestContentBlockDiscrimination:
    """Test 2 from PHASE-1-SUBPHASE-1.md "Testing Strategy".

    Verify each ContentBlock variant has the correct type discriminator.
    """

    def test_text_content_type(self):
        """TextContent.type is 'text'."""
        tb = TextContent(text="hello")
        assert tb.type == "text"
        assert tb.text == "hello"

    def test_image_content_type(self):
        """ImageContent.type is 'image' with data and mime_type."""
        ib = ImageContent(data="base64string", mime_type="image/png")
        assert ib.type == "image"
        assert ib.data == "base64string"
        assert ib.mime_type == "image/png"

    def test_image_content_jpeg_mime(self):
        """ImageContent works with image/jpeg."""
        ib = ImageContent(data="base64jpegdata", mime_type="image/jpeg")
        assert ib.type == "image"
        assert ib.mime_type == "image/jpeg"

    def test_tool_call_type(self):
        """ToolCall.type is 'toolCall' with id, name, arguments."""
        tbc = ToolCall(id="call_123", name="bash", arguments={"command": "ls"})
        assert tbc.type == "toolCall"
        assert tbc.name == "bash"
        assert tbc.arguments == {"command": "ls"}

    def test_thinking_content_type(self):
        """ThinkingContent.type is 'thinking'."""
        tc = ThinkingContent(thinking="I need to reason about this")
        assert tc.type == "thinking"
        assert tc.thinking == "I need to reason about this"
        assert tc.cached_tokens == 0  # default

    def test_thinking_content_with_cached_tokens(self):
        """ThinkingContent stores cached_tokens when provided."""
        tc = ThinkingContent(thinking="reasoning", cached_tokens=100)
        assert tc.type == "thinking"
        assert tc.cached_tokens == 100

    def test_discriminated_union_in_assistant_message(self):
        """AssistantMessage content list correctly holds mixed ContentBlock types."""
        msg = AssistantMessage(
            content=[
                TextContent(type="text", text="Let me think..."),
                ThinkingContent(type="thinking", thinking="reasoning steps..."),
                ToolCall(
                    type="toolCall",
                    id="call_1",
                    name="ls",
                    arguments={"path": "."},
                ),
                TextContent(type="text", text="Here's the result."),
            ],
            api="openai-completions",
            provider="openai",
            model="gpt-4",
            usage=Usage(),
            stop_reason="toolUse",
            timestamp=0,
        )
        assert len(msg.content) == 4
        assert msg.content[0].type == "text"
        assert msg.content[1].type == "thinking"
        assert msg.content[2].type == "toolCall"
        assert msg.content[3].type == "text"

    def test_discriminated_union_in_user_message(self):
        """UserMessage content list correctly holds TextContent and ImageContent."""
        msg = UserMessage(
            content=[
                TextContent(type="text", text="Look at this image:"),
                ImageContent(data="base64data", mime_type="image/png"),
            ],
            timestamp=0,
        )
        assert len(msg.content) == 2
        assert msg.content[0].type == "text"
        assert msg.content[1].type == "image"


# ═══════════════════════════════════════════════════════════════════════════
# Test 3: Tool argument validation
# ═══════════════════════════════════════════════════════════════════════════

class TestValidateToolArgumentsValid:
    """Test 3a from PHASE-1-SUBPHASE-1.md "Testing Strategy".

    Verify valid arguments pass validation.
    """

    @pytest.fixture
    def tool_with_schema(self):
        """A tool with a JSON schema requiring a 'name' field."""

        class MockTool:
            parameters = {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            }

        return MockTool()

    @pytest.fixture
    def tool_with_multiple_fields(self):
        """A tool with multiple required fields."""

        class MockTool:
            parameters = {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer"},
                    "active": {"type": "boolean"},
                },
                "required": ["name", "count"],
            }

        return MockTool()

    def test_valid_single_field(self, tool_with_schema):
        """Valid args with required field passes."""
        from tau_ai.types import ToolCall
        tc = ToolCall(id="call_1", name="test", arguments={"name": "world"})
        result = validate_tool_arguments(tool_with_schema, tc)
        assert result == {"name": "world"}

    def test_valid_args_as_dict(self, tool_with_schema):
        """Valid args passed as dict passes."""
        result = validate_tool_arguments(tool_with_schema, {"name": "world"})
        assert result == {"name": "world"}

    def test_valid_multiple_fields(self, tool_with_multiple_fields):
        """Valid args with all required fields passes."""
        from tau_ai.types import ToolCall
        tc = ToolCall(
            id="call_1",
            name="test",
            arguments={"name": "world", "count": 42, "active": True},
        )
        result = validate_tool_arguments(tool_with_multiple_fields, tc)
        assert result == {"name": "world", "count": 42, "active": True}

    def test_extra_fields_allowed(self, tool_with_schema):
        """Extra fields beyond schema don't cause failure."""
        result = validate_tool_arguments(tool_with_schema, {"name": "world", "extra": "ignored"})
        assert result["name"] == "world"

    def test_empty_args_with_no_required(self):
        """Empty args passes when no required fields."""

        class MockTool:
            parameters = {"type": "object"}

        result = validate_tool_arguments(MockTool(), {})
        assert result == {}


class TestValidateToolArgumentsInvalid:
    """Test 3b from PHASE-1-SUBPHASE-1.md "Testing Strategy".

    Verify invalid arguments raise ValueError.
    """

    @pytest.fixture
    def tool_with_schema(self):
        """A tool with a JSON schema requiring a 'name' field."""

        class MockTool:
            parameters = {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            }

        return MockTool()

    def test_missing_required_field(self, tool_with_schema):
        """Missing required field raises ValueError."""
        with pytest.raises(ValueError, match="Missing required field"):
            validate_tool_arguments(tool_with_schema, {"wrong_key": "world"})

    def test_wrong_type_string(self, tool_with_schema):
        """Wrong type (int instead of string) raises ValueError."""
        with pytest.raises(ValueError):
            validate_tool_arguments(tool_with_schema, {"name": 123})

    def test_wrong_type_integer(self):
        """Wrong type (string instead of integer) raises ValueError."""

        class MockTool:
            parameters = {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            }

        with pytest.raises(ValueError, match="expected integer"):
            validate_tool_arguments(MockTool(), {"count": "not a number"})

    def test_wrong_type_boolean(self):
        """Wrong type (string instead of boolean) raises ValueError."""

        class MockTool:
            parameters = {
                "type": "object",
                "properties": {"flag": {"type": "boolean"}},
                "required": ["flag"],
            }

        with pytest.raises(ValueError, match="expected boolean"):
            validate_tool_arguments(MockTool(), {"flag": "yes"})

    def test_wrong_type_object(self):
        """Wrong type (list instead of object) raises ValueError."""

        class MockTool:
            parameters = {
                "type": "object",
                "properties": {"config": {"type": "object"}},
                "required": ["config"],
            }

        with pytest.raises(ValueError, match="expected object"):
            validate_tool_arguments(MockTool(), {"config": [1, 2, 3]})

    def test_wrong_type_array(self):
        """Wrong type (dict instead of array) raises ValueError."""

        class MockTool:
            parameters = {
                "type": "object",
                "properties": {"tags": {"type": "array"}},
                "required": ["tags"],
            }

        with pytest.raises(ValueError, match="expected array"):
            validate_tool_arguments(MockTool(), {"tags": {"key": "value"}})

    def test_multiple_missing_fields(self):
        """Multiple missing required fields all reported."""

        class MockTool:
            parameters = {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                "required": ["a", "b"],
            }

        with pytest.raises(ValueError, match="Missing required field") as exc_info:
            validate_tool_arguments(MockTool(), {})

        error_msg = str(exc_info.value)
        assert "a" in error_msg
        assert "b" in error_msg


# ═══════════════════════════════════════════════════════════════════════════
# Test 4: AbortSignal thread safety (async version)
# ═══════════════════════════════════════════════════════════════════════════

class TestAbortSignalThreadSafety:
    """Test 4 from PHASE-1-SUBPHASE-1.md "Testing Strategy".

    Verify AbortSignal is thread-safe and idempotent.
    Uses asyncio for the async version of the subphase test.
    """

    def test_abort_signal_thread_safety(self):
        """AbortSignal works correctly across threads with asyncio polling."""
        signal = AbortSignal()
        results = []

        def background_abort():
            time.sleep(0.01)
            signal.abort()

        threading.Thread(target=background_abort).start()

        async def poll_abort():
            while not signal.is_aborted():
                results.append(True)
                await asyncio.sleep(0.001)

        asyncio.run(poll_abort())

        assert len(results) > 0, "Should have polled at least once before abort"
        assert signal.is_aborted()

    def test_abort_is_idempotent(self):
        """Calling abort() multiple times is safe."""
        signal = AbortSignal()
        signal.abort()
        assert signal.is_aborted()
        signal.abort()  # Should not raise
        assert signal.is_aborted()
        signal.abort()  # Should not raise
        assert signal.is_aborted()

    def test_abort_then_check(self):
        """After abort(), is_aborted() consistently returns True."""
        signal = AbortSignal()
        signal.abort()
        for _ in range(100):
            assert signal.is_aborted() is True

    def test_is_aborted_false_before_abort(self):
        """is_aborted() returns False for a new AbortSignal."""
        signal = AbortSignal()
        assert signal.is_aborted() is False

    def test_abort_multiple_threads(self):
        """Multiple threads calling abort() simultaneously is safe."""
        signal = AbortSignal()

        def abort_worker():
            for _ in range(100):
                signal.abort()

        threads = [threading.Thread(target=abort_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert signal.is_aborted()

    def test_abort_concurrent_read_write(self):
        """Concurrent is_aborted() reads during abort() don't crash."""
        signal = AbortSignal()
        errors = []
        stop_flag = threading.Event()

        def reader():
            try:
                for _ in range(200):
                    signal.is_aborted()
                    if stop_flag.is_set():
                        break
            except Exception as e:
                errors.append(e)

        def writer():
            time.sleep(0.01)
            signal.abort()
            stop_flag.set()

        threads = []
        for _ in range(5):
            threads.append(threading.Thread(target=reader))
        threads.append(threading.Thread(target=writer))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0, f"Race condition errors: {errors}"
        assert signal.is_aborted()

    def test_abort_signal_in_async_poll(self):
        """Async polling detects abort quickly after background abort."""
        signal = AbortSignal()
        polls_before_abort = [0]

        def background_abort():
            time.sleep(0.01)
            signal.abort()

        threading.Thread(target=background_abort).start()

        async def poll():
            polls_before_abort[0] += 1
            while not signal.is_aborted():
                polls_before_abort[0] += 1
                await asyncio.sleep(0.0005)

        asyncio.run(poll())
        assert polls_before_abort[0] > 0, "Should have polled at least once"
        assert signal.is_aborted()


# ═══════════════════════════════════════════════════════════════════════════
# Test 5: Model serialization to OpenAI format
# ═══════════════════════════════════════════════════════════════════════════

class TestModelSerialization:
    """Test 5 from PHASE-1-SUBPHASE-1.md "Testing Strategy".

    Verify Model serializes correctly to OpenAI-compatible dict format.
    """

    def test_model_attributes(self):
        """Model attributes are accessible."""
        model = Model(
            id="gpt-4o",
            name="GPT-4o",
            api="openai-completions",
            provider="openai",
            base_url="https://api.openai.com/v1",
            context_window=128000,
            max_tokens=4096,
        )
        assert model.id == "gpt-4o"
        assert model.name == "GPT-4o"
        assert model.base_url == "https://api.openai.com/v1"

    def test_model_to_openai_format(self):
        """Model.to_openai_format() returns OpenAI-compatible dict."""
        model = Model(
            id="gpt-4o",
            name="GPT-4o",
            api="openai-completions",
            provider="openai",
            base_url="https://api.openai.com/v1",
            context_window=128000,
            max_tokens=4096,
        )
        result = model.to_openai_format()

        assert result["id"] == "gpt-4o"
        assert result["name"] == "GPT-4o"
        assert result["provider"] == "openai"
        assert result["base_url"] == "https://api.openai.com/v1"
        assert result["max_completion_tokens"] == 4096
        assert result["context_window"] == 128000

    def test_model_to_openai_format_responses_api(self):
        """Model with openai-responses API serializes correctly."""
        model = Model(
            id="o1-mini",
            name="o1-mini",
            api="openai-responses",
            provider="openai",
            base_url="https://api.openai.com/v1",
            context_window=128000,
            max_tokens=32768,
        )
        result = model.to_openai_format()
        assert result["id"] == "o1-mini"
        assert result["max_completion_tokens"] == 32768

    def test_model_serializes_to_dict(self):
        """Model.model_dump() returns a dict."""
        model = Model(
            id="gpt-4o",
            name="GPT-4o",
            api="openai-completions",
            provider="openai",
            base_url="https://api.openai.com/v1",
            context_window=128000,
            max_tokens=4096,
        )
        d = model.model_dump()
        assert isinstance(d, dict)
        assert d["id"] == "gpt-4o"
        assert d["name"] == "GPT-4o"
        assert d["context_window"] == 128000
        assert d["max_tokens"] == 4096

    def test_model_roundtrip(self):
        """Model serializes and deserializes correctly."""
        model = Model(
            id="gpt-4o",
            name="GPT-4o",
            api="openai-completions",
            provider="openai",
            base_url="https://api.openai.com/v1",
            context_window=128000,
            max_tokens=4096,
        )
        d = model.model_dump()
        recovered = Model.model_validate(d)
        assert recovered.id == model.id
        assert recovered.name == model.name
        assert recovered.base_url == model.base_url

    def test_model_importable_from_top_level(self):
        """Model is importable from tau_ai top-level."""
        from tau_ai import Model
        m = Model(
            id="gpt-4o",
            name="GPT-4o",
            api="openai-completions",
            provider="openai",
            base_url="https://api.openai.com/v1",
            context_window=128000,
            max_tokens=4096,
        )
        assert m.id == "gpt-4o"


# ═══════════════════════════════════════════════════════════════════════════
# Additional: Usage frozen immutability
# ═══════════════════════════════════════════════════════════════════════════

class TestUsageFrozen:
    """Test Usage is frozen (immutable) as required by the subphase."""

    def test_usage_is_frozen(self):
        """Usage cannot be mutated after creation."""
        u = Usage(input_tokens=100, output_tokens=50)
        with pytest.raises(Exception):  # Pydantic ValidationError
            u.input_tokens = 200

    def test_usage_default_is_frozen(self):
        """Default Usage is also frozen."""
        u = Usage()
        with pytest.raises(Exception):
            u.output_tokens = 100

    def test_usage_copy_returns_new_instance(self):
        """model_copy() returns a new instance, not same object."""
        u = Usage(input_tokens=100)
        u2 = u.model_copy()
        assert u2.input_tokens == 100
        # u2 is a separate instance (though frozen)
        assert u is not u2

    def test_usage_serialization(self):
        """Usage serializes to a dict with all fields."""
        u = Usage(
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=80,
            cache_write_tokens=20,
            total_tokens=150,
            cost={"input": 0.01, "output": 0.02},
        )
        d = u.model_dump()
        assert d["input_tokens"] == 100
        assert d["output_tokens"] == 50
        assert d["cache_read_tokens"] == 80
        assert d["cache_write_tokens"] == 20
        assert d["total_tokens"] == 150
        assert d["cost"]["input"] == 0.01
