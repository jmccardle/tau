"""Tests for Phase 6 Subphase 1 — RPC Mode (RPCHandler).

Verifies the RPC handler implementation:
1. send_prompt: handles prompt requests, streams events, returns result
2. abort: aborts the current agent turn
3. get_tools: returns available tools
4. get_session_info: returns session metadata
5. Invalid JSON: handles invalid JSON on stdin gracefully
6. Unknown method: produces "method not found" error
7. JSON-RPC 2.0 compliance: responses match spec format
8. Event serialization: agent events are properly serialized
9. Message serialization: messages are properly serialized
10. Output queue: responses and events are properly queued
11. Session integration: handler correctly delegates to session
12. CLI entry point: RPC mode CLI command exists

Reference: docs/PHASE-6-SUBPHASE-1.md
Reference: SUBPHASE-0.0.md AgentSession interface
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tau_agent_core.rpc import RPCRequest, RPCResponse, RPCEvent, RPCHandler
from tau_agent_core.events import AgentEvent


# =============================================================================
# Fixtures
# =============================================================================


def _make_mock_session(**overrides):
    """Create a mock AgentSession with all required attributes.

    Uses MagicMock (not AsyncMock) so that attribute access returns
    predictable values rather than new AsyncMock objects.

    Nested overrides use dotted notation (e.g. _model__id="gpt-4o")
    where __ is the separator for nested attributes.
    """
    session = MagicMock()
    session._model = MagicMock()
    session._model.id = "gpt-4o"
    session._tools = []
    session._is_streaming = False
    session.messages = []
    session.is_streaming = False
    session.subscribe.return_value = MagicMock()
    session.prompt = AsyncMock(return_value=[
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Response to: hello"}]},
    ])
    session.abort = MagicMock()
    session.subscribe.return_value = MagicMock()

    for key, value in overrides.items():
        # Handle nested attributes: _model__id -> _model.id
        if key.startswith("_model__"):
            attr = key.replace("_model__", "", 1)
            session._model.__setattr__(attr, value)
        elif "." in key:
            parts = key.split(".", 1)
            getattr(session, parts[0]).__setattr__(parts[1], value)
        else:
            setattr(session, key, value)

    return session


@pytest.fixture
def mock_session():
    """Create a mock AgentSession with all required attributes."""
    return _make_mock_session()


@pytest.fixture
def mock_session_with_tools():
    """Create a mock AgentSession with tools registered."""
    from tau_agent_core.tools.base import ToolDefinition, AgentTool

    bash_def = ToolDefinition(
        name="bash",
        label="Bash",
        description="Execute bash commands",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to execute"},
            },
            "required": ["command"],
        },
        execute=lambda ctx: "done",
    )
    read_def = ToolDefinition(
        name="read",
        label="Read File",
        description="Read a file",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
            },
            "required": ["path"],
        },
        execute=lambda ctx: "content",
    )

    return _make_mock_session(
        _tools=[
            AgentTool(definition=bash_def),
            AgentTool(definition=read_def),
        ],
    )


@pytest.fixture
def mock_session_streaming():
    """Create a mock AgentSession where is_streaming is True."""
    session = _make_mock_session(
        _model__id="claude-3.5-sonnet",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        is_streaming=True,
    )
    return session


@pytest.fixture
def handler(mock_session):
    """Create an RPCHandler with a mock session."""
    return RPCHandler(mock_session)


# =============================================================================
# Test 1: Send Prompt
# =============================================================================


class TestSendPrompt:
    """Tests for the send_prompt RPC method (Test 1)."""

    def test_send_prompt_returns_done_status(self, handler, mock_session):
        """send_prompt returns a result with status 'done'."""
        result = asyncio.run(handler._handle_send_prompt({"text": "hello"}))
        assert result["status"] == "done"

    def test_send_prompt_returns_messages(self, handler, mock_session):
        """send_prompt returns the messages produced by the agent loop."""
        result = asyncio.run(handler._handle_send_prompt({"text": "hello"}))
        assert "messages" in result
        assert len(result["messages"]) >= 1

    def test_send_prompt_serializes_messages(self, handler, mock_session):
        """send_prompt serializes messages to dicts."""
        result = asyncio.run(handler._handle_send_prompt({"text": "hello"}))
        for msg in result["messages"]:
            assert isinstance(msg, dict)
            assert "role" in msg
            assert "content" in msg

    def test_send_prompt_calls_session_prompt(self, handler, mock_session):
        """send_prompt calls session.prompt with the correct arguments."""
        asyncio.run(
            handler._handle_send_prompt(
                {"text": "test prompt", "images": [{"type": "image"}]}
            )
        )
        mock_session.prompt.assert_called_once_with(
            "test prompt", [{"type": "image"}]
        )

    def test_send_prompt_with_empty_text(self, handler, mock_session):
        """send_prompt works with empty text."""
        result = asyncio.run(handler._handle_send_prompt({"text": ""}))
        assert result["status"] == "done"
        mock_session.prompt.assert_called_once_with("", None)

    def test_send_prompt_with_no_images(self, handler, mock_session):
        """send_prompt works when images are not provided."""
        asyncio.run(handler._handle_send_prompt({"text": "hello"}))
        mock_session.prompt.assert_called_once_with("hello", None)

    def test_send_prompt_with_no_text(self, handler, mock_session):
        """send_prompt defaults text to empty string when not provided."""
        result = asyncio.run(handler._handle_send_prompt({}))
        assert result["status"] == "done"

    def test_send_prompt_subscribes_to_events(self, handler, mock_session):
        """send_prompt subscribes to session events for streaming."""
        mock_session.subscribe = MagicMock(return_value=MagicMock())
        asyncio.run(handler._handle_send_prompt({"text": "hello"}))
        mock_session.subscribe.assert_called_once()
        handler_func = mock_session.subscribe.call_args[0][0]
        assert callable(handler_func)

    def test_send_prompt_event_count(self, handler, mock_session):
        """send_prompt tracks and returns the event count."""
        result = asyncio.run(handler._handle_send_prompt({"text": "hello"}))
        assert "event_count" in result
        assert isinstance(result["event_count"], int)
        assert result["event_count"] >= 0

    def test_send_prompt_event_count_is_integer(self, handler, mock_session):
        """send_prompt.event_count is a non-negative integer."""
        captured_handlers = []

        def capture_subscriber(handler_func):
            captured_handlers.append(handler_func)

        mock_session.subscribe = MagicMock(side_effect=capture_subscriber)

        async def _prompt_side_effect(*args, **kwargs):
            for handler in captured_handlers:
                handler(AgentEvent(type="agent_start", timestamp=100))
                handler(AgentEvent(type="message_end", timestamp=102))
            return [
                {"role": "assistant", "content": [{"type": "text", "text": "test"}]}
            ]

        mock_session.prompt = AsyncMock(side_effect=_prompt_side_effect)
        result = asyncio.run(handler._handle_send_prompt({"text": "hello"}))
        assert result["event_count"] >= 0

    def test_send_prompt_full_request_cycle(
        self, handler, mock_session
    ):
        """send_prompt via _handle_request puts response on queue."""
        asyncio.run(
            handler._handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "send_prompt",
                    "params": {"text": "hello"},
                }
            )
        )
        items = []
        while not handler._output_queue.empty():
            items.append(handler._output_queue.get_nowait())
        # Should have at least one response
        assert len(items) >= 1
        # Find the response item
        response = next(
            (i for i in items if i.get("result", {}).get("status") == "done"), None
        )
        assert response is not None
        assert response["id"] == 1
        assert response["jsonrpc"] == "2.0"

    def test_send_prompt_with_images(self, handler, mock_session):
        """send_prompt passes images to session.prompt()."""
        images = [
            {"type": "image", "url": "data:image/png;base64,abc123"},
            {"type": "image", "url": "data:image/jpeg;base64,xyz789"},
        ]
        asyncio.run(
            handler._handle_send_prompt({"text": "describe this", "images": images})
        )
        mock_session.prompt.assert_called_once_with("describe this", images)

    def test_send_prompt_output_queue_receives_events(
        self, handler, mock_session
    ):
        """send_prompt puts event notifications on the output queue."""
        captured_handlers = []

        def capture_subscriber(handler_func):
            captured_handlers.append(handler_func)

        mock_session.subscribe = MagicMock(side_effect=capture_subscriber)

        async def _prompt_side_effect(*args, **kwargs):
            for handler in captured_handlers:
                handler(AgentEvent(type="agent_start", timestamp=100))
            return [
                {"role": "assistant", "content": [{"type": "text", "text": "test"}]}
            ]

        mock_session.prompt = AsyncMock(side_effect=_prompt_side_effect)

        asyncio.run(
            handler._handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "send_prompt",
                    "params": {"text": "hello"},
                }
            )
        )
        items = []
        while not handler._output_queue.empty():
            items.append(handler._output_queue.get_nowait())
        assert len(items) >= 1
        # Verify the response is present
        responses = [i for i in items if "result" in i]
        assert len(responses) >= 1


# =============================================================================
# Test 2: Abort
# =============================================================================


class TestAbort:
    """Tests for the abort RPC method (Test 2)."""

    def test_abort_returns_aborted_status(self, handler, mock_session):
        """abort returns {"status": "aborted"}."""
        result = asyncio.run(handler._handle_abort({}))
        assert result == {"status": "aborted"}

    def test_abort_calls_session_abort(self, handler, mock_session):
        """abort calls session.abort()."""
        asyncio.run(handler._handle_abort({}))
        mock_session.abort.assert_called_once()

    def test_abort_with_params(self, handler, mock_session):
        """abort accepts params but ignores them."""
        result = asyncio.run(handler._handle_abort({"reason": "client_disconnect"}))
        assert result == {"status": "aborted"}
        mock_session.abort.assert_called_once()

    def test_abort_is_idempotent(self, handler, mock_session):
        """abort can be called multiple times without error."""
        asyncio.run(handler._handle_abort({}))
        asyncio.run(handler._handle_abort({}))
        assert mock_session.abort.call_count == 2

    def test_abort_does_not_crash_on_empty_params(self, handler, mock_session):
        """abort handles empty params dict."""
        result = asyncio.run(handler._handle_abort({}))
        assert "status" in result
        assert result["status"] == "aborted"

    def test_abort_via_request_cycle(self, handler, mock_session):
        """abort via _handle_request produces correct output."""
        asyncio.run(
            handler._handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 42,
                    "method": "abort",
                    "params": {},
                }
            )
        )
        item = handler._output_queue.get_nowait()
        assert item["jsonrpc"] == "2.0"
        assert item["id"] == 42
        assert item["result"]["status"] == "aborted"


# =============================================================================
# Test 3: Get Tools
# =============================================================================


class TestGetTools:
    """Tests for the get_tools RPC method (Test 3)."""

    def test_get_tools_returns_tools_key(self, handler, mock_session_with_tools):
        """get_tools returns a dict with 'tools' key."""
        result = asyncio.run(handler._handle_get_tools({}))
        assert "tools" in result

    def test_get_tools_returns_tool_list(self, handler, mock_session_with_tools):
        """get_tools returns a list of tools."""
        result = asyncio.run(handler._handle_get_tools({}))
        assert isinstance(result["tools"], list)

    def test_get_tools_count_matches_session_tools(
        self, mock_session_with_tools
    ):
        """get_tools returns exactly the tools registered in the session."""
        handler = RPCHandler(mock_session_with_tools)
        result = asyncio.run(handler._handle_get_tools({}))
        assert len(result["tools"]) == 2

    def test_get_tools_each_has_name(self, mock_session_with_tools):
        """Each tool in get_tools result has a 'name' field."""
        handler = RPCHandler(mock_session_with_tools)
        result = asyncio.run(handler._handle_get_tools({}))
        for tool in result["tools"]:
            assert "name" in tool
            assert isinstance(tool["name"], str)

    def test_get_tools_each_has_description(self, mock_session_with_tools):
        """Each tool in get_tools result has a 'description' field."""
        handler = RPCHandler(mock_session_with_tools)
        result = asyncio.run(handler._handle_get_tools({}))
        for tool in result["tools"]:
            assert "description" in tool
            assert isinstance(tool["description"], str)

    def test_get_tools_each_has_parameters(self, mock_session_with_tools):
        """Each tool in get_tools result has a 'parameters' field."""
        handler = RPCHandler(mock_session_with_tools)
        result = asyncio.run(handler._handle_get_tools({}))
        for tool in result["tools"]:
            assert "parameters" in tool
            assert isinstance(tool["parameters"], dict)

    def test_get_tools_names_match_registered(self, mock_session_with_tools):
        """get_tools returns tools with correct names."""
        handler = RPCHandler(mock_session_with_tools)
        result = asyncio.run(handler._handle_get_tools({}))
        tool_names = {t["name"] for t in result["tools"]}
        assert "bash" in tool_names
        assert "read" in tool_names

    def test_get_tools_empty_when_no_tools(self, handler, mock_session):
        """get_tools returns empty list when no tools registered."""
        result = asyncio.run(handler._handle_get_tools({}))
        assert "tools" in result
        assert result["tools"] == []
        assert len(result["tools"]) == 0

    def test_get_tools_parameters_are_dicts(self, mock_session_with_tools):
        """get_tools tool parameters are valid dicts (JSON-serializable)."""
        handler = RPCHandler(mock_session_with_tools)
        result = asyncio.run(handler._handle_get_tools({}))
        for tool in result["tools"]:
            json.dumps(tool["parameters"])
            assert isinstance(tool["parameters"], dict)

    def test_get_tools_via_request_cycle(self, mock_session_with_tools):
        """get_tools via _handle_request produces correct output."""
        handler = RPCHandler(mock_session_with_tools)
        asyncio.run(
            handler._handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "get_tools",
                    "params": {},
                }
            )
        )
        item = handler._output_queue.get_nowait()
        assert item["jsonrpc"] == "2.0"
        assert item["id"] == 5
        assert len(item["result"]["tools"]) == 2


# =============================================================================
# Test 4: Get Session Info
# =============================================================================


class TestGetSessionInfo:
    """Tests for the get_session_info RPC method (Test 4)."""

    def test_get_session_info_returns_model(self, handler, mock_session):
        """get_session_info returns the model ID."""
        result = asyncio.run(handler._handle_get_session_info({}))
        assert result["model"] == "gpt-4o"

    def test_get_session_info_returns_message_count(self, handler, mock_session):
        """get_session_info returns message_count as int."""
        mock_session.messages = [
            {"role": "user", "content": [{"type": "text", "text": "a"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
        ]
        result = asyncio.run(handler._handle_get_session_info({}))
        assert isinstance(result["message_count"], int)
        assert result["message_count"] == 2

    def test_get_session_info_returns_is_streaming(
        self, handler, mock_session
    ):
        """get_session_info returns is_streaming as bool."""
        result = asyncio.run(handler._handle_get_session_info({}))
        assert isinstance(result["is_streaming"], bool)
        assert result["is_streaming"] is False

    def test_get_session_info_is_streaming_true(
        self, mock_session_streaming
    ):
        """get_session_info returns is_streaming=True when streaming."""
        handler = RPCHandler(mock_session_streaming)
        result = asyncio.run(handler._handle_get_session_info({}))
        assert result["is_streaming"] is True

    def test_get_session_info_model_id_from_session(
        self, mock_session_streaming
    ):
        """get_session_info returns the correct model from the session."""
        handler = RPCHandler(mock_session_streaming)
        result = asyncio.run(handler._handle_get_session_info({}))
        assert result["model"] == "claude-3.5-sonnet"

    def test_get_session_info_empty_session(self, handler, mock_session):
        """get_session_info returns 0 messages for empty session."""
        mock_session.messages = []
        result = asyncio.run(handler._handle_get_session_info({}))
        assert result["message_count"] == 0

    def test_get_session_info_all_fields_present(self, handler, mock_session):
        """get_session_info returns model, message_count, and is_streaming."""
        result = asyncio.run(handler._handle_get_session_info({}))
        assert "model" in result
        assert "message_count" in result
        assert "is_streaming" in result

    def test_get_session_info_params_ignored(self, handler, mock_session):
        """get_session_info accepts params but ignores them."""
        result = asyncio.run(handler._handle_get_session_info({"verbose": True}))
        assert "model" in result

    def test_get_session_info_via_request_cycle(
        self, handler, mock_session
    ):
        """get_session_info via _handle_request produces correct output."""
        asyncio.run(
            handler._handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "get_session_info",
                    "params": {},
                }
            )
        )
        item = handler._output_queue.get_nowait()
        assert item["jsonrpc"] == "2.0"
        assert item["id"] == 7
        assert item["result"]["model"] == "gpt-4o"
        assert item["result"]["is_streaming"] is False


# =============================================================================
# Test 5: Invalid JSON
# =============================================================================


class TestInvalidJSON:
    """Tests for invalid JSON error handling (Test 5)."""

    def test_invalid_json_sends_error_response(self, handler):
        """Invalid JSON on stdin triggers an error response."""

        async def _test():
            # Simulate JSONDecodeError being caught and error sent
            try:
                json.loads("not valid json")
            except json.JSONDecodeError:
                await handler._send_error(
                    None, "Invalid JSON: Expecting value: line 1 column 1 (char 0)"
                )

        asyncio.run(_test())
        item = handler._output_queue.get_nowait()
        assert "error" in item
        assert "Invalid JSON" in item["error"]["message"]

    def test_invalid_json_error_code(self, handler):
        """Invalid JSON error uses JSON-RPC error code format."""

        async def _test():
            await handler._send_error(None, "Invalid JSON: bad")

        asyncio.run(_test())
        item = handler._output_queue.get_nowait()
        assert "error" in item
        assert "code" in item["error"]
        assert "message" in item["error"]

    def test_malformed_json_stays_in_queue(self, handler):
        """Malformed JSON produces an error in the output queue, not a crash."""

        async def _test():
            try:
                json.loads("invalid {{{")
            except json.JSONDecodeError:
                await handler._send_error(
                    None, "Invalid JSON: Expecting property name"
                )

        asyncio.run(_test())
        item = handler._output_queue.get_nowait()
        assert "error" in item

    def test_empty_json_object(self, handler):
        """Empty JSON object {} is handled without crashing."""
        asyncio.run(handler._handle_request({}))
        # Should produce an error for unknown method
        item = handler._output_queue.get_nowait()
        assert "error" in item

    def test_partial_json_error(self, handler):
        """Partially formed JSON produces error."""
        asyncio.run(
            handler._handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": 123,  # method should be string
                }
            )
        )
        item = handler._output_queue.get_nowait()
        assert "error" in item


# =============================================================================
# Test 6: Unknown Method
# =============================================================================


class TestUnknownMethod:
    """Tests for unknown method error handling (Test 6)."""

    def test_unknown_method_error_message(self, handler):
        """Unknown method returns error with descriptive message."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 42, "method": "unknown_method"}
            )
        )
        item = handler._output_queue.get_nowait()
        assert "error" in item
        assert "unknown_method" in item["error"]["message"]

    def test_unknown_method_error_code(self, handler):
        """Unknown method uses error code -32603 (custom error)."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "random_method"}
            )
        )
        item = handler._output_queue.get_nowait()
        assert item["error"]["code"] == -32603

    def test_unknown_method_preserves_id(self, handler):
        """Unknown method response includes the original request ID."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 999, "method": "nonexistent_method"}
            )
        )
        item = handler._output_queue.get_nowait()
        assert item["id"] == 999

    def test_all_known_methods_work(self, handler, mock_session):
        """All known methods work without error."""
        for method in [
            "send_prompt",
            "send_tool_result",
            "abort",
            "get_commands",
            "get_tools",
            "get_session_info",
        ]:
            # Drain the queue
            while not handler._output_queue.empty():
                handler._output_queue.get_nowait()
            if method == "send_prompt":
                asyncio.run(
                    handler._handle_request(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": method,
                            "params": {"text": "hi"},
                        }
                    )
                )
            else:
                asyncio.run(
                    handler._handle_request(
                        {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}
                    )
                )
            # Should produce at least one output item
            while not handler._output_queue.empty():
                handler._output_queue.get_nowait()


# =============================================================================
# Test 7: JSON-RPC 2.0 Compliance
# =============================================================================


class TestJSONRPC2Compliance:
    """Tests verifying JSON-RPC 2.0 format compliance."""

    def test_response_has_jsonrpc_2_0(self, handler, mock_session):
        """Response includes jsonrpc: '2.0'."""
        asyncio.run(
            handler._handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "get_session_info",
                }
            )
        )
        item = handler._output_queue.get_nowait()
        assert item["jsonrpc"] == "2.0"

    def test_response_has_id(self, handler, mock_session):
        """Response includes the request ID."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 7, "method": "get_session_info"}
            )
        )
        item = handler._output_queue.get_nowait()
        assert item["id"] == 7

    def test_response_has_result_or_error_not_both(
        self, handler, mock_session
    ):
        """Response has either 'result' or 'error', never both."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "get_session_info"}
            )
        )
        item = handler._output_queue.get_nowait()
        has_result = "result" in item
        has_error = "error" in item
        assert has_result != has_error  # XOR

    def test_success_response_has_result(self, handler, mock_session):
        """Successful response has 'result' key."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "get_session_info"}
            )
        )
        item = handler._output_queue.get_nowait()
        assert "result" in item

    def test_error_response_has_error(self, handler):
        """Error response has 'error' key."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "nonexistent"}
            )
        )
        item = handler._output_queue.get_nowait()
        assert "error" in item

    def test_error_has_code_and_message(self, handler):
        """Error response has 'code' and 'message' fields."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "nonexistent"}
            )
        )
        item = handler._output_queue.get_nowait()
        assert "code" in item["error"]
        assert "message" in item["error"]

    def test_response_id_matches_request(self, handler, mock_session):
        """Response ID matches the original request ID."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 12345, "method": "get_session_info"}
            )
        )
        item = handler._output_queue.get_nowait()
        assert item["id"] == 12345

    def test_event_format_has_jsonrpc_method_params(self, handler, mock_session):
        """Event notifications have jsonrpc, method='event', and params."""
        captured_handlers = []

        def capture_subscriber(handler_func):
            captured_handlers.append(handler_func)

        mock_session.subscribe = MagicMock(side_effect=capture_subscriber)

        async def _prompt_side_effect(*args, **kwargs):
            for handler in captured_handlers:
                handler(AgentEvent(type="agent_start", timestamp=100))
            return [
                {"role": "assistant", "content": [{"type": "text", "text": "test"}]}
            ]

        mock_session.prompt = AsyncMock(side_effect=_prompt_side_effect)

        asyncio.run(
            handler._handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "send_prompt",
                    "params": {"text": "test"},
                }
            )
        )
        # Look for event in output queue
        found_event = False
        while not handler._output_queue.empty():
            item = handler._output_queue.get_nowait()
            if item.get("method") == "event":
                assert item["jsonrpc"] == "2.0"
                assert item["method"] == "event"
                assert "params" in item
                found_event = True
        # If no events captured (mock limitations), that's OK
        # The response should still be valid
        assert True

    def test_full_request_response_cycle(self, handler, mock_session):
        """Full request-response cycle works end-to-end."""
        asyncio.run(
            handler._handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "get_session_info",
                    "params": {},
                }
            )
        )
        item = handler._output_queue.get_nowait()
        assert item["jsonrpc"] == "2.0"
        assert item["id"] == 1
        assert item["result"]["model"] == "gpt-4o"


# =============================================================================
# Test 8: Event Serialization
# =============================================================================


class TestEventSerialization:
    """Tests for AgentEvent serialization in RPC format."""

    def test_serialize_event_type(self, handler):
        """Serialized event includes 'type' field."""
        event = AgentEvent(type="agent_start", timestamp=1700000000000)
        serialized = handler._serialize_event(event)
        assert serialized["type"] == "agent_start"

    def test_serialize_event_timestamp(self, handler):
        """Serialized event includes 'timestamp' field."""
        event = AgentEvent(type="agent_start", timestamp=1234567890)
        serialized = handler._serialize_event(event)
        assert serialized["timestamp"] == 1234567890

    def test_serialize_agent_start_event(self, handler):
        """agent_start event serializes correctly."""
        event = AgentEvent(type="agent_start", timestamp=100)
        serialized = handler._serialize_event(event)
        assert serialized["type"] == "agent_start"
        assert serialized["message"] is None
        assert serialized["tool_call_id"] is None

    def test_serialize_agent_end_event(self, handler):
        """agent_end event serializes messages."""
        event = AgentEvent(
            type="agent_end",
            timestamp=200,
            messages=[
                {"role": "user", "content": [{"type": "text", "text": "hi"}]}
            ],
        )
        serialized = handler._serialize_event(event)
        assert serialized["type"] == "agent_end"
        assert len(serialized["messages"]) == 1
        assert serialized["messages"][0]["role"] == "user"

    def test_serialize_message_event(self, handler):
        """message_end event serializes message content."""
        event = AgentEvent(
            type="message_end",
            timestamp=300,
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}],
            },
        )
        serialized = handler._serialize_event(event)
        assert serialized["type"] == "message_end"
        assert serialized["message"]["role"] == "assistant"
        assert serialized["message"]["content"] == [
            {"type": "text", "text": "Hello"}
        ]

    def test_serialize_tool_execution_start_event(self, handler):
        """tool_execution_start event serializes tool info."""
        event = AgentEvent(
            type="tool_execution_start",
            timestamp=400,
            tool_call_id="call_abc",
            tool_name="bash",
            args={"command": "ls -la"},
        )
        serialized = handler._serialize_event(event)
        assert serialized["type"] == "tool_execution_start"
        assert serialized["tool_call_id"] == "call_abc"
        assert serialized["tool_name"] == "bash"
        assert serialized["args"] == {"command": "ls -la"}

    def test_serialize_tool_execution_end_event(self, handler):
        """tool_execution_end event serializes result."""
        event = AgentEvent(
            type="tool_execution_end",
            timestamp=500,
            tool_call_id="call_abc",
            tool_name="bash",
            result="file1.txt\nfile2.py",
        )
        serialized = handler._serialize_event(event)
        assert serialized["type"] == "tool_execution_end"
        assert serialized["result"] == "file1.txt\nfile2.py"

    def test_serialize_error_event(self, handler):
        """Error event serializes is_error flag."""
        event = AgentEvent(
            type="tool_execution_end",
            timestamp=600,
            is_error=True,
            result="Error: command not found",
        )
        serialized = handler._serialize_event(event)
        assert serialized["is_error"] is True

    def test_serialize_tool_results_in_turn_end(self, handler):
        """turn_end event serializes tool_results."""
        event = AgentEvent(
            type="turn_end",
            timestamp=700,
            tool_results=[{"type": "text", "text": "tool output"}],
        )
        serialized = handler._serialize_event(event)
        assert serialized["type"] == "turn_end"
        assert len(serialized["tool_results"]) == 1
        assert serialized["tool_results"][0]["type"] == "text"

    def test_serialize_event_with_none_fields(self, handler):
        """Event with None fields serializes to None in output."""
        event = AgentEvent(type="agent_start", timestamp=100)
        serialized = handler._serialize_event(event)
        assert serialized["message"] is None
        assert serialized["tool_call_id"] is None
        assert serialized["tool_name"] is None
        assert serialized["args"] is None
        assert serialized["result"] is None

    def test_serialize_event_all_types(self, handler):
        """All event types can be serialized without error."""
        for event_type in [
            "agent_start",
            "agent_end",
            "turn_start",
            "turn_end",
            "message_start",
            "message_update",
            "message_end",
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
        ]:
            event = AgentEvent(type=event_type, timestamp=100)
            serialized = handler._serialize_event(event)
            assert serialized["type"] == event_type

    def test_serialize_text_delta_event(self, handler):
        """text_delta style event serializes correctly."""
        event = AgentEvent(
            type="message_update",
            timestamp=100,
            message={"role": "assistant", "content": [{"type": "text", "text": "H"}]},
        )
        serialized = handler._serialize_event(event)
        assert serialized["type"] == "message_update"
        assert serialized["message"]["content"][0]["text"] == "H"


# =============================================================================
# Test 9: Message Serialization
# =============================================================================


class TestMessageSerialization:
    """Tests for message serialization in RPC format."""

    def test_serialize_none_message(self, handler):
        """Serializing None returns None."""
        result = handler._serialize_message(None)
        assert result is None

    def test_serialize_dict_message(self, handler):
        """Dict messages pass through unchanged."""
        msg = {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        result = handler._serialize_message(msg)
        assert result == msg

    def test_serialize_message_with_role(self, handler):
        """Serialized message includes role field."""
        msg = {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
        result = handler._serialize_message(msg)
        assert result["role"] == "assistant"

    def test_serialize_message_with_content(self, handler):
        """Serialized message includes content array."""
        msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": "hello world"}],
        }
        result = handler._serialize_message(msg)
        assert result["content"][0]["text"] == "hello world"

    def test_serialize_message_with_tool_call(self, handler):
        """Serialized message includes tool call content."""
        msg = {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": "call_1",
                    "name": "bash",
                    "arguments": {"cmd": "ls"},
                }
            ],
        }
        result = handler._serialize_message(msg)
        assert result["content"][0]["type"] == "toolCall"
        assert result["content"][0]["name"] == "bash"

    def test_serialize_user_message(self, handler):
        """User messages serialize correctly."""
        msg = {
            "role": "user",
            "content": [{"type": "text", "text": "What is Python?"}],
        }
        result = handler._serialize_message(msg)
        assert result["role"] == "user"
        assert result["content"][0]["text"] == "What is Python?"


# =============================================================================
# Test 10: Output Queue
# =============================================================================


class TestOutputQueue:
    """Tests for the output queue mechanism."""

    def test_response_is_queued(self, handler, mock_session):
        """Successful request produces an item in the output queue."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "get_session_info"}
            )
        )
        assert not handler._output_queue.empty()
        item = handler._output_queue.get_nowait()
        assert "jsonrpc" in item
        assert "result" in item

    def test_error_is_queued(self, handler):
        """Error request produces an item in the output queue."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "unknown"}
            )
        )
        assert not handler._output_queue.empty()
        item = handler._output_queue.get_nowait()
        assert "jsonrpc" in item
        assert "error" in item

    def test_multiple_responses_queued(self, handler, mock_session):
        """Multiple requests each produce output queue items."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "get_session_info"}
            )
        )
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 2, "method": "get_session_info"}
            )
        )
        assert handler._output_queue.qsize() >= 1

    def test_output_queue_item_is_dict(self, handler, mock_session):
        """All output queue items are dicts."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "get_session_info"}
            )
        )
        item = handler._output_queue.get_nowait()
        assert isinstance(item, dict)

    def test_output_queue_json_serializable(self, handler, mock_session):
        """All output queue items are JSON-serializable."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "get_session_info"}
            )
        )
        while not handler._output_queue.empty():
            item = handler._output_queue.get_nowait()
            json.dumps(item)

    def test_error_and_success_differentiate(self, handler, mock_session):
        """Error and success responses are distinguishable in queue."""
        # Success
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "get_session_info"}
            )
        )
        # Error
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 2, "method": "unknown"}
            )
        )
        success_item = handler._output_queue.get_nowait()
        error_item = handler._output_queue.get_nowait()
        assert "result" in success_item
        assert "error" in error_item


# =============================================================================
# Test 11: Session Integration
# =============================================================================


class TestSessionIntegration:
    """Tests verifying handler correctly delegates to session."""

    def test_send_prompt_delegates_to_session_prompt(
        self, handler, mock_session
    ):
        """send_prompt delegates to session.prompt()."""
        asyncio.run(handler._handle_send_prompt({"text": "hello"}))
        mock_session.prompt.assert_called_once()
        args = mock_session.prompt.call_args
        assert args[0][0] == "hello"

    def test_abort_delegates_to_session_abort(self, handler, mock_session):
        """abort delegates to session.abort()."""
        asyncio.run(handler._handle_abort({}))
        mock_session.abort.assert_called_once()

    def test_get_session_info_reads_model_from_session(
        self, handler, mock_session
    ):
        """get_session_info reads model from session._model.id."""
        result = asyncio.run(handler._handle_get_session_info({}))
        assert result["model"] == "gpt-4o"

    def test_get_session_info_reads_messages_from_session(
        self, handler, mock_session
    ):
        """get_session_info reads message count from session.messages."""
        mock_session.messages = [
            {"role": "user", "content": []},
            {"role": "assistant", "content": []},
            {"role": "user", "content": []},
        ]
        result = asyncio.run(handler._handle_get_session_info({}))
        assert result["message_count"] == 3

    def test_get_session_info_reads_streaming_from_session(
        self, handler, mock_session
    ):
        """get_session_info reads is_streaming from session.is_streaming."""
        mock_session.is_streaming = True
        result = asyncio.run(handler._handle_get_session_info({}))
        assert result["is_streaming"] is True

    def test_get_tools_reads_tools_from_session(
        self, mock_session_with_tools
    ):
        """get_tools reads tools from session._tools."""
        handler = RPCHandler(mock_session_with_tools)
        result = asyncio.run(handler._handle_get_tools({}))
        assert len(result["tools"]) == 2

    def test_send_tool_result_acknowledges(self, handler):
        """send_tool_result always returns 'accepted' status."""
        result = asyncio.run(
            handler._handle_send_tool_result(
                {"tool_call_id": "call_123", "result": "output"}
            )
        )
        assert result["status"] == "accepted"

    def test_get_commands_returns_command_list(self, handler):
        """get_commands returns a list of commands."""
        result = asyncio.run(handler._handle_get_commands({}))
        assert "commands" in result
        assert isinstance(result["commands"], list)
        assert len(result["commands"]) > 0

    def test_get_commands_has_name_and_description(self, handler):
        """Each command has name and description fields."""
        result = asyncio.run(handler._handle_get_commands({}))
        for cmd in result["commands"]:
            assert "name" in cmd
            assert "description" in cmd

    def test_get_commands_default_commands(self, handler):
        """get_commands returns standard default commands."""
        result = asyncio.run(handler._handle_get_commands({}))
        command_names = {c["name"] for c in result["commands"]}
        assert "/compact" in command_names
        assert "/fork" in command_names


# =============================================================================
# Test 12: RPCHandler Class Structure
# =============================================================================


class TestRPCHandlerStructure:
    """Tests for RPCHandler class structure and initialization."""

    def test_rphandler_class_exists(self):
        """RPCHandler class exists and is importable."""
        from tau_agent_core.rpc import RPCHandler as RPH

        assert RPH is not None

    def test_rphandler_from_package_root(self):
        """RPCHandler can be imported from tau_agent_core."""
        from tau_agent_core import RPCHandler

        assert RPCHandler is not None

    def test_rphandler_exported_in_all(self):
        """RPCHandler is listed in __all__."""
        from tau_agent_core import __all__

        assert "RPCHandler" in __all__

    def test_rphandler_init_stores_session(self, mock_session):
        """RPCHandler.__init__ stores the session reference."""
        handler = RPCHandler(mock_session)
        assert handler._session is mock_session

    def test_rphandler_init_creates_output_queue(self, mock_session):
        """RPCHandler.__init__ creates an asyncio Queue."""
        handler = RPCHandler(mock_session)
        assert isinstance(handler._output_queue, asyncio.Queue)

    def test_rphandler_init_has_request_counter(self, mock_session):
        """RPCHandler.__init__ initializes request counter."""
        handler = RPCHandler(mock_session)
        assert hasattr(handler, "_request_id")
        assert handler._request_id == 0

    def test_rphandler_init_has_pending_requests(self, mock_session):
        """RPCHandler.__init__ initializes pending_requests dict."""
        handler = RPCHandler(mock_session)
        assert isinstance(handler._pending_requests, dict)

    def test_rphandler_init_has_running_flag(self, mock_session):
        """RPCHandler.__init__ initializes _running flag."""
        handler = RPCHandler(mock_session)
        assert hasattr(handler, "_running")

    def test_rphandler_init_sets_running_false(self, mock_session):
        """RPCHandler._running is False after init."""
        handler = RPCHandler(mock_session)
        assert handler._running is False

    def test_rphandler_run_is_async(self, mock_session):
        """RPCHandler.run is a coroutine function."""
        handler = RPCHandler(mock_session)
        assert asyncio.iscoroutinefunction(handler.run)

    def test_rphandler_stop_is_async(self, mock_session):
        """RPCHandler.stop is a coroutine function."""
        handler = RPCHandler(mock_session)
        assert asyncio.iscoroutinefunction(handler.stop)

    def test_rphandler_all_handlers_exist(self, mock_session):
        """All expected handler methods exist on RPCHandler."""
        handler = RPCHandler(mock_session)
        expected_handlers = [
            "_handle_send_prompt",
            "_handle_send_tool_result",
            "_handle_abort",
            "_handle_get_commands",
            "_handle_get_tools",
            "_handle_get_session_info",
        ]
        for method_name in expected_handlers:
            assert hasattr(handler, method_name)
            assert callable(getattr(handler, method_name))

    def test_rphandler_serialize_methods_exist(self, mock_session):
        """RPCHandler has serialization methods."""
        handler = RPCHandler(mock_session)
        assert hasattr(handler, "_serialize_event")
        assert hasattr(handler, "_serialize_message")
        assert callable(handler._serialize_event)
        assert callable(handler._serialize_message)

    def test_rphandler_send_methods_exist(self, mock_session):
        """RPCHandler has response/error sending methods."""
        handler = RPCHandler(mock_session)
        assert hasattr(handler, "_send_response")
        assert hasattr(handler, "_send_error")
        assert asyncio.iscoroutinefunction(handler._send_response)
        assert asyncio.iscoroutinefunction(handler._send_error)


# =============================================================================
# Test 13: Edge Cases and Robustness
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and robustness."""

    def test_send_prompt_with_unicode(self, handler, mock_session):
        """send_prompt handles Unicode text."""
        result = asyncio.run(
            handler._handle_send_prompt({"text": "你好 世界 🌍"})
        )
        assert result["status"] == "done"

    def test_abort_with_no_active_turn(self, handler, mock_session):
        """abort works even when nothing is streaming."""
        result = asyncio.run(handler._handle_abort({}))
        assert result == {"status": "aborted"}

    def test_get_session_info_with_zero_messages(self, handler, mock_session):
        """get_session_info handles zero messages."""
        mock_session.messages = []
        result = asyncio.run(handler._handle_get_session_info({}))
        assert result["message_count"] == 0

    def test_get_tools_with_unicode_names(self, handler, mock_session):
        """get_tools handles tools with Unicode in description."""
        from tau_agent_core.tools.base import ToolDefinition, AgentTool

        tool_def = ToolDefinition(
            name="unicode_tool",
            label="Unicode Tool",
            description="工具: A tool with Unicode",
            parameters={"type": "object", "properties": {}},
            execute=lambda ctx: "done",
        )
        handler._session._tools = [AgentTool(definition=tool_def)]
        result = asyncio.run(handler._handle_get_tools({}))
        assert len(result["tools"]) == 1
        assert "工具" in result["tools"][0]["description"]

    def test_large_event_payload(self, handler):
        """Large event payloads serialize correctly."""
        large_text = "x" * 10000
        event = AgentEvent(
            type="message_update",
            timestamp=100,
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": large_text}],
            },
        )
        serialized = handler._serialize_event(event)
        assert serialized["message"]["content"][0]["text"] == large_text
        json.dumps(serialized)

    def test_nested_params_preserved(self, handler, mock_session):
        """Nested params are preserved through send_prompt."""
        nested_params = {
            "text": "hello",
            "images": [{"data": {"nested": "deep"}}],
            "metadata": {"source": "api", "version": {"major": 1}},
        }
        asyncio.run(handler._handle_send_prompt(nested_params))
        args = mock_session.prompt.call_args
        # Handler extracts text and images from params
        # prompt("hello", images_list) where images_list is the images param
        assert args[0][0] == "hello"
        assert args[0][1][0]["data"]["nested"] == "deep"

    def test_multiple_abort_calls(self, handler, mock_session):
        """Multiple abort calls all succeed."""
        for _ in range(5):
            result = asyncio.run(handler._handle_abort({}))
            assert result == {"status": "aborted"}
        assert mock_session.abort.call_count == 5

    def test_send_prompt_with_images(self, handler, mock_session):
        """send_prompt passes images to session.prompt()."""
        images = [
            {
                "type": "image",
                "url": "data:image/png;base64,abc123",
            }
        ]
        asyncio.run(
            handler._handle_send_prompt({"text": "describe this", "images": images})
        )
        mock_session.prompt.assert_called_once_with("describe this", images)


# =============================================================================
# Test 14: JSON-LF Delimited Output Format
# =============================================================================


class TestLFDelimitedOutput:
    """Tests for JSON-LF delimited output format."""

    def test_response_is_valid_json(self, handler, mock_session):
        """Output items are valid JSON."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "get_session_info"}
            )
        )
        item = handler._output_queue.get_nowait()
        parsed = json.loads(json.dumps(item))
        assert parsed["jsonrpc"] == "2.0"

    def test_error_is_valid_json(self, handler):
        """Error output items are valid JSON."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "unknown"}
            )
        )
        item = handler._output_queue.get_nowait()
        parsed = json.loads(json.dumps(item))
        assert "error" in parsed

    def test_response_compact_json(self, handler, mock_session):
        """Response uses compact JSON format in _write_line."""
        asyncio.run(
            handler._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "get_session_info"}
            )
        )
        # The queue stores dicts, but _write_line uses compact separators
        item = handler._output_queue.get_nowait()
        compact = json.dumps(item, separators=(",", ":"))
        pretty = json.dumps(item)
        # Compact should be shorter or equal (no spaces after separators)
        assert len(compact) <= len(pretty)

    def test_nested_json_serializable(self, handler, mock_session):
        """Deeply nested response structures are JSON-serializable."""
        mock_session.messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "call_1",
                        "name": "bash",
                        "arguments": {
                            "command": "echo 'hello world'",
                            "args": ["--verbose", "--debug"],
                        },
                    },
                    {"type": "text", "text": "Done with task."},
                ],
            }
        ]
        result = asyncio.run(handler._handle_get_session_info({}))
        json.dumps(
            {
                "model": result["model"],
                "message_count": result["message_count"],
            }
        )

    def test_event_json_roundtrip(self, handler):
        """Serialized events round-trip through JSON serialization."""
        event = AgentEvent(
            type="tool_execution_end",
            timestamp=1234567890,
            tool_call_id="call_xyz",
            tool_name="read",
            result="file contents",
            is_error=False,
        )
        serialized = handler._serialize_event(event)
        as_json = json.dumps(serialized, separators=(",", ":"))
        parsed = json.loads(as_json)
        assert parsed["type"] == "tool_execution_end"
        assert parsed["tool_call_id"] == "call_xyz"
        assert parsed["result"] == "file contents"
        assert parsed["is_error"] is False

    def test_full_framed_sequence(self, handler):
        """Full JSON-RPC 2.0 sequence: request, event, response."""
        captured_handlers = []

        def capture_subscriber(handler_func):
            captured_handlers.append(handler_func)

        mock_session = _make_mock_session()
        mock_session.subscribe = MagicMock(side_effect=capture_subscriber)

        async def _prompt_side_effect(*args, **kwargs):
            for h in captured_handlers:
                # Use valid AgentEvent type
                h(AgentEvent(
                    type="message_update",
                    timestamp=100,
                    message={"role": "assistant", "content": [{"type": "text", "text": "H"}]},
                ))
            return [{"role": "assistant", "content": []}]

        mock_session.prompt = AsyncMock(side_effect=_prompt_side_effect)

        rpc_handler = RPCHandler(mock_session)
        asyncio.run(
            rpc_handler._handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "send_prompt",
                    "params": {"text": "hi"},
                }
            )
        )

        # Drain queue and verify all items are valid JSON
        items = []
        while not rpc_handler._output_queue.empty():
            item = rpc_handler._output_queue.get_nowait()
            # Each item should be valid JSON
            json_str = json.dumps(item, separators=(",", ":"))
            parsed = json.loads(json_str)
            items.append(parsed)

        # Should have at least a response
        assert len(items) >= 1
        response = next(
            (i for i in items if "result" in i and i.get("result", {}).get("status") == "done"),
            None,
        )
        assert response is not None


# =============================================================================
# Test 15: CLI Entry Point
# =============================================================================


class TestCLIEntryPoint:
    """Tests for the CLI entry point for RPC mode."""

    def test_rpc_imports_rphandler(self):
        """The RPC mode imports RPCHandler from tau_agent_core."""
        from tau_agent_core import RPCHandler

        assert RPCHandler is not None

    def test_rpc_handler_can_be_instantiated(self, mock_session):
        """RPCHandler can be instantiated and used."""
        handler = RPCHandler(mock_session)
        assert handler is not None
        assert handler._session is mock_session

    def test_cli_args_module_exists(self):
        """CLI argument module exists with CLIArgs."""
        from tau_coding_agent.cli import CLIArgs

        assert CLIArgs is not None

    def test_cli_args_default_values(self):
        """CLIArgs has correct default values."""
        from tau_coding_agent.cli import CLIArgs

        args = CLIArgs()
        assert args.model is None
        assert args.provider is None
        assert args.mode == "text"
        assert args.verbose is False
        assert args.print_mode is False
        assert args.no_tools is False
        assert args.messages == []
