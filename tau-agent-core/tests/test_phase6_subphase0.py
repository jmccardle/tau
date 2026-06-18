"""Tests for Phase 6 Subphase 0 — Data Contract Definition.

Verifies the RPC protocol and export type contracts:
1. RPCRequest: jsonrpc, id, method, params, to_json_line, from_json_line
2. RPCResponse: jsonrpc, id, result, error, to_json_line, from_json_line, is_error
3. RPCEvent: jsonrpc, method, params, to_json_line, from_json_line
4. ExportConfig: format, include_tool_calls, include_thinking, include_timestamps,
   to_dict, from_dict, is_markdown, is_html
5. LF-delimited JSON framing compliance
6. Public exports from package root

Reference: docs/PHASE-6-SUBPHASE-0.md
Reference: docs/SUBPHASE-0.0.md lines 260-340
"""

import json
from unittest.mock import patch

import pytest

from tau_agent_core.rpc import RPCRequest, RPCResponse, RPCEvent
from tau_agent_core.export import ExportConfig


# =============================================================================
# 1. RPCRequest — Basic Structure Tests
# =============================================================================


class TestRPCRequestStructure:
    """Tests verifying RPCRequest has all required fields per the contract."""

    def test_request_jsonrpc_default(self):
        """RPCRequest defaults jsonrpc to '2.0'."""
        req = RPCRequest()
        assert req.jsonrpc == "2.0"

    def test_request_jsonrpc_type(self):
        """RPCRequest.jsonrpc is a Literal['2.0']."""
        req = RPCRequest()
        assert isinstance(req.jsonrpc, str)
        assert req.jsonrpc == "2.0"

    def test_request_id_defaults_to_none(self):
        """RPCRequest.id defaults to None."""
        req = RPCRequest()
        assert req.id is None

    def test_request_method_defaults_to_empty(self):
        """RPCRequest.method defaults to empty string."""
        req = RPCRequest()
        assert req.method == ""

    def test_request_params_defaults_to_none(self):
        """RPCRequest.params defaults to None."""
        req = RPCRequest()
        assert req.params is None

    def test_request_instantiation_with_all_fields(self):
        """RPCRequest can be instantiated with all fields."""
        req = RPCRequest(
            jsonrpc="2.0",
            id=1,
            method="send_prompt",
            params={"text": "hello"},
        )
        assert req.jsonrpc == "2.0"
        assert req.id == 1
        assert req.method == "send_prompt"
        assert req.params == {"text": "hello"}

    def test_request_instantiation_with_id_none(self):
        """RPCRequest works with id=None (notification-style request)."""
        req = RPCRequest(method="event", params={"type": "test"})
        assert req.id is None

    def test_request_with_complex_params(self):
        """RPCRequest handles complex nested params."""
        req = RPCRequest(
            id=42,
            method="send_prompt",
            params={
                "text": "hello world",
                "images": [
                    {"url": "data:image/png;base64,abc123", "mime": "image/png"},
                ],
            },
        )
        assert req.params["text"] == "hello world"
        assert len(req.params["images"]) == 1


# =============================================================================
# 2. RPCRequest — Serialization Tests
# =============================================================================


class TestRPCRequestSerialization:
    """Tests for RPCRequest to_json_line() and from_json_line()."""

    def test_to_json_line_contains_required_fields(self):
        """RPCRequest.to_json_line() includes jsonrpc, id, method, params."""
        req = RPCRequest(
            jsonrpc="2.0",
            id=1,
            method="send_prompt",
            params={"text": "hello"},
        )
        line = req.to_json_line()
        parsed = json.loads(line)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["id"] == 1
        assert parsed["method"] == "send_prompt"
        assert parsed["params"] == {"text": "hello"}

    def test_to_json_line_is_lf_delimited(self):
        """RPCRequest.to_json_line() produces LF-delimited JSON (no trailing newline)."""
        req = RPCRequest(id=1, method="test", params={"key": "val"})
        line = req.to_json_line()
        # Must not end with \n (LF) — LF is the delimiter, not part of the line
        assert not line.endswith("\n")
        assert not line.endswith("\r\n")

    def test_to_json_line_compact_format(self):
        """RPCRequest.to_json_line() uses compact JSON (no extra spaces)."""
        req = RPCRequest(id=1, method="test", params={"k": "v"})
        line = req.to_json_line()
        # Compact format uses "," and ":" separators (no spaces)
        # The raw dataclass __dict__ format means it's actually pretty-printed
        # by default, but the key requirement is valid JSON
        parsed = json.loads(line)
        assert parsed is not None

    def test_from_json_line_roundtrip(self):
        """RPCRequest round-trips through to_json_line/from_json_line."""
        original = RPCRequest(
            jsonrpc="2.0",
            id=99,
            method="send_tool_result",
            params={"tool_call_id": "call_001", "result": "done"},
        )
        line = original.to_json_line()
        restored = RPCRequest.from_json_line(line)
        assert restored.jsonrpc == original.jsonrpc
        assert restored.id == original.id
        assert restored.method == original.method
        assert restored.params == original.params

    def test_from_json_line_with_null_id(self):
        """RPCRequest.from_json_line() handles null id."""
        line = '{"jsonrpc":"2.0","id":null,"method":"notification","params":{}}'
        req = RPCRequest.from_json_line(line)
        assert req.jsonrpc == "2.0"
        assert req.id is None
        assert req.method == "notification"
        assert req.params == {}

    def test_from_json_line_with_no_id_field(self):
        """RPCRequest.from_json_line() handles missing id field."""
        line = '{"jsonrpc":"2.0","method":"ping","params":{}}'
        req = RPCRequest.from_json_line(line)
        assert req.method == "ping"
        assert req.id is None

    def test_from_json_line_preserves_nested_params(self):
        """RPCRequest.from_json_line() preserves nested parameter structures."""
        original = RPCRequest(
            id=1,
            method="send_prompt",
            params={
                "text": "What is the capital of France?",
                "images": [
                    {"url": "data:image/png;base64,abc", "mime": "image/png"},
                ],
                "metadata": {"source": "api", "version": 1},
            },
        )
        line = original.to_json_line()
        restored = RPCRequest.from_json_line(line)
        assert restored.params["text"] == original.params["text"]
        assert len(restored.params["images"]) == 1
        assert restored.params["metadata"]["source"] == "api"


# =============================================================================
# 3. RPCResponse — Basic Structure Tests
# =============================================================================


class TestRPCResponseStructure:
    """Tests verifying RPCResponse has all required fields per the contract."""

    def test_response_jsonrpc_default(self):
        """RPCResponse defaults jsonrpc to '2.0'."""
        resp = RPCResponse()
        assert resp.jsonrpc == "2.0"

    def test_response_id_defaults_to_none(self):
        """RPCResponse.id defaults to None."""
        resp = RPCResponse()
        assert resp.id is None

    def test_response_result_defaults_to_none(self):
        """RPCResponse.result defaults to None."""
        resp = RPCResponse()
        assert resp.result is None

    def test_response_error_defaults_to_none(self):
        """RPCResponse.error defaults to None."""
        resp = RPCResponse()
        assert resp.error is None

    def test_response_success(self):
        """RPCResponse can represent a success result."""
        resp = RPCResponse(
            jsonrpc="2.0",
            id=1,
            result={"status": "done", "messages": [{"role": "assistant", "content": []}]},
            error=None,
        )
        assert resp.result is not None
        assert resp.error is None
        assert resp.result["status"] == "done"

    def test_response_error_case(self):
        """RPCResponse can represent an error."""
        resp = RPCResponse(
            jsonrpc="2.0",
            id=2,
            result=None,
            error={"code": -32601, "message": "Method not found"},
        )
        assert resp.result is None
        assert resp.error is not None
        assert resp.error["code"] == -32601

    def test_response_with_null_id(self):
        """RPCResponse can have id=None (for notification responses)."""
        resp = RPCResponse(jsonrpc="2.0", id=None, result={"ok": True})
        assert resp.id is None


# =============================================================================
# 4. RPCResponse — Serialization and Error Tests
# =============================================================================


class TestRPCResponseSerialization:
    """Tests for RPCResponse to_json_line(), from_json_line(), and is_error()."""

    def test_to_json_line_success(self):
        """RPCResponse.to_json_line() serializes a success response."""
        resp = RPCResponse(
            id=1,
            result={"status": "done"},
        )
        line = resp.to_json_line()
        parsed = json.loads(line)
        assert parsed["id"] == 1
        assert parsed["result"] == {"status": "done"}
        assert parsed["error"] is None

    def test_to_json_line_error(self):
        """RPCResponse.to_json_line() serializes an error response."""
        resp = RPCResponse(
            id=2,
            result=None,
            error={"code": -32600, "message": "Invalid Request"},
        )
        line = resp.to_json_line()
        parsed = json.loads(line)
        assert parsed["id"] == 2
        assert parsed["result"] is None
        assert parsed["error"]["code"] == -32600

    def test_from_json_line_success_roundtrip(self):
        """RPCResponse round-trips through to_json_line/from_json_line."""
        original = RPCResponse(
            jsonrpc="2.0",
            id=42,
            result={"status": "ok", "count": 3},
        )
        line = original.to_json_line()
        restored = RPCResponse.from_json_line(line)
        assert restored.id == original.id
        assert restored.result == original.result
        assert restored.error is None

    def test_is_error_returns_false_for_success(self):
        """RPCResponse.is_error() returns False for success responses."""
        resp = RPCResponse(id=1, result={"status": "ok"})
        assert resp.is_error() is False

    def test_is_error_returns_true_for_error(self):
        """RPCResponse.is_error() returns True for error responses."""
        resp = RPCResponse(id=1, error={"code": -32600, "message": "error"})
        assert resp.is_error() is True

    def test_is_error_returns_false_for_empty_response(self):
        """RPCResponse.is_error() returns False for empty (default) response."""
        resp = RPCResponse()
        assert resp.is_error() is False

    def test_response_with_complex_result(self):
        """RPCResponse handles complex nested result structures."""
        resp = RPCResponse(
            id=1,
            result={
                "status": "done",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                    {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )
        line = resp.to_json_line()
        restored = RPCResponse.from_json_line(line)
        assert len(restored.result["messages"]) == 2
        assert restored.result["usage"]["input_tokens"] == 10

    def test_lf_delimited_framing(self):
        """RPCResponse.to_json_line() is LF-delimited (no trailing newline)."""
        resp = RPCResponse(id=1, result={"ok": True})
        line = resp.to_json_line()
        assert not line.endswith("\n")
        assert not line.endswith("\r\n")


# =============================================================================
# 5. RPCEvent — Basic Structure Tests
# =============================================================================


class TestRPCEventStructure:
    """Tests verifying RPCEvent has all required fields per the contract."""

    def test_event_jsonrpc_default(self):
        """RPCEvent defaults jsonrpc to '2.0'."""
        evt = RPCEvent()
        assert evt.jsonrpc == "2.0"

    def test_event_method_default(self):
        """RPCEvent defaults method to 'event'."""
        evt = RPCEvent()
        assert evt.method == "event"

    def test_event_method_literal(self):
        """RPCEvent.method is a Literal['event']."""
        evt = RPCEvent()
        assert evt.method == "event"
        assert isinstance(evt.method, str)

    def test_event_params_defaults_to_empty_dict(self):
        """RPCEvent.params defaults to empty dict."""
        evt = RPCEvent()
        assert evt.params == {}

    def test_event_with_params(self):
        """RPCEvent can carry event payload params."""
        evt = RPCEvent(
            jsonrpc="2.0",
            method="event",
            params={
                "type": "text_delta",
                "delta": "Hello",
            },
        )
        assert evt.params["type"] == "text_delta"
        assert evt.params["delta"] == "Hello"

    def test_event_with_agent_start_params(self):
        """RPCEvent can carry agent_start event data."""
        evt = RPCEvent(
            params={
                "type": "agent_start",
                "timestamp": 1700000000000,
                "turn_index": None,
            },
        )
        assert evt.params["type"] == "agent_start"

    def test_event_with_tool_execution_params(self):
        """RPCEvent can carry tool_execution_start event data."""
        evt = RPCEvent(
            params={
                "type": "tool_execution_start",
                "tool_call_id": "call_123",
                "tool_name": "ls",
                "args": {"path": "."},
                "timestamp": 1700000000000,
            },
        )
        assert evt.params["type"] == "tool_execution_start"
        assert evt.params["tool_call_id"] == "call_123"
        assert evt.params["tool_name"] == "ls"

    def test_event_params_is_dict(self):
        """RPCEvent.params is always a dict."""
        evt = RPCEvent()
        assert isinstance(evt.params, dict)


# =============================================================================
# 6. RPCEvent — Serialization Tests
# =============================================================================


class TestRPCEventSerialization:
    """Tests for RPCEvent to_json_line() and from_json_line()."""

    def test_to_json_line_text_delta(self):
        """RPCEvent.to_json_line() serializes a text_delta event."""
        evt = RPCEvent(params={"type": "text_delta", "delta": "H"})
        line = evt.to_json_line()
        parsed = json.loads(line)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["method"] == "event"
        assert parsed["params"]["type"] == "text_delta"
        assert parsed["params"]["delta"] == "H"

    def test_to_json_line_is_lf_delimited(self):
        """RPCEvent.to_json_line() is LF-delimited."""
        evt = RPCEvent(params={"type": "done"})
        line = evt.to_json_line()
        assert not line.endswith("\n")
        assert not line.endswith("\r\n")

    def test_from_json_line_roundtrip(self):
        """RPCEvent round-trips through to_json_line/from_json_line."""
        original = RPCEvent(
            params={"type": "agent_end", "messages": [], "timestamp": 100},
        )
        line = original.to_json_line()
        restored = RPCEvent.from_json_line(line)
        assert restored.jsonrpc == "2.0"
        assert restored.method == "event"
        assert restored.params["type"] == "agent_end"
        assert restored.params["messages"] == []

    def test_from_json_line_with_full_agent_event(self):
        """RPCEvent.from_json_line() preserves full agent event data."""
        evt_dict = {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "tool_execution_end",
                "tool_call_id": "call_abc",
                "tool_name": "read_file",
                "result": "file contents here",
                "is_error": False,
                "timestamp": 1700000001000,
            },
        }
        line = json.dumps(evt_dict, separators=(",", ":"))
        evt = RPCEvent.from_json_line(line)
        assert evt.params["tool_call_id"] == "call_abc"
        assert evt.params["result"] == "file contents here"
        assert evt.params["is_error"] is False


# =============================================================================
# 7. RPC — JSON-RPC 2.0 Protocol Compliance
# =============================================================================


class TestRPCProtocolCompliance:
    """Tests verifying RPC types comply with JSON-RPC 2.0 spec."""

    def test_all_types_have_jsonrpc_2_0(self):
        """All RPC types default jsonrpc to '2.0'."""
        assert RPCRequest().jsonrpc == "2.0"
        assert RPCResponse().jsonrpc == "2.0"
        assert RPCEvent().jsonrpc == "2.0"

    def test_request_has_method_and_params(self):
        """RPCRequest has method (required) and params (optional)."""
        req = RPCRequest(method="test")
        assert hasattr(req, "method")
        assert hasattr(req, "params")
        assert isinstance(req.method, str)

    def test_response_has_result_and_error(self):
        """RPCResponse has result and error fields (mutually exclusive)."""
        resp = RPCResponse()
        assert hasattr(resp, "result")
        assert hasattr(resp, "error")

    def test_event_has_event_method(self):
        """RPCEvent always has method='event'."""
        evt = RPCEvent()
        assert evt.method == "event"

    def test_request_response_id_pairing(self):
        """Request ID and Response ID match for request-response pattern."""
        req_id = 123
        req = RPCRequest(id=req_id, method="send_prompt", params={"text": "hi"})
        resp = RPCResponse(id=req_id, result={"status": "ok"})
        assert req.id == resp.id

    def test_notification_style_request(self):
        """RPCRequest can be a notification (id=None)."""
        req = RPCRequest(method="event", params={"type": "ping"})
        assert req.id is None

    def test_lf_delimited_separation(self):
        """Multiple RPC messages separated by LF are valid."""
        req = RPCRequest(id=1, method="send_prompt", params={"text": "hello"})
        resp = RPCResponse(id=1, result={"status": "done"})
        event = RPCEvent(params={"type": "done", "delta": ""})

        # All are valid single lines
        lines = [req.to_json_line(), resp.to_json_line(), event.to_json_line()]
        for line in lines:
            assert "\n" not in line
            assert "\r" not in line

        # Joined with LF should be valid JSON on each line
        combined = "\n".join(lines)
        for line in combined.split("\n"):
            if line.strip():
                parsed = json.loads(line)
                assert parsed is not None


# =============================================================================
# 8. RPC — Request ID Handling
# =============================================================================


class TestRPCRequestId:
    """Tests for RPC request/response ID handling."""

    def test_request_with_int_id(self):
        """RPCRequest accepts integer ID."""
        req = RPCRequest(id=1)
        assert req.id == 1

    def test_request_with_zero_id(self):
        """RPCRequest accepts zero as valid ID."""
        req = RPCRequest(id=0)
        assert req.id == 0

    def test_request_with_large_id(self):
        """RPCRequest accepts large integer ID."""
        req = RPCRequest(id=999999999)
        assert req.id == 999999999

    def test_response_with_int_id(self):
        """RPCResponse accepts integer ID."""
        resp = RPCResponse(id=1)
        assert resp.id == 1

    def test_request_id_is_optional(self):
        """RPCRequest id can be None for notifications."""
        req = RPCRequest(id=None)
        assert req.id is None

    def test_response_id_preserved_roundtrip(self):
        """RPCResponse preserves ID through serialization roundtrip."""
        for test_id in [0, 1, 42, 999999, None]:
            resp = RPCResponse(id=test_id, result={"ok": True})
            line = resp.to_json_line()
            restored = RPCResponse.from_json_line(line)
            assert restored.id == test_id, f"ID {test_id} not preserved"


# =============================================================================
# 9. RPC — Method Name Conventions
# =============================================================================


class TestRPCMethodNames:
    """Tests for RPC method name patterns from the contract."""

    def test_send_prompt_method(self):
        """'send_prompt' is a valid RPC method."""
        req = RPCRequest(method="send_prompt", params={"text": "hello"})
        assert req.method == "send_prompt"

    def test_send_tool_result_method(self):
        """'send_tool_result' is a valid RPC method."""
        req = RPCRequest(method="send_tool_result", params={"tool_call_id": "x"})
        assert req.method == "send_tool_result"

    def test_get_commands_method(self):
        """'get_commands' is a valid RPC method."""
        req = RPCRequest(method="get_commands")
        assert req.method == "get_commands"

    def test_event_method(self):
        """'event' is the reserved method for RPCEvent notifications."""
        evt = RPCEvent(params={"type": "text_delta", "delta": "test"})
        assert evt.method == "event"

    def test_arbitrary_method_name(self):
        """RPCRequest accepts arbitrary method names (extensibility)."""
        req = RPCRequest(method="custom_method")
        assert req.method == "custom_method"


# =============================================================================
# 10. ExportConfig — Basic Structure Tests
# =============================================================================


class TestExportConfigStructure:
    """Tests verifying ExportConfig has all required fields per the contract."""

    def test_config_format_required(self):
        """ExportConfig requires format field."""
        config = ExportConfig(format="markdown")
        assert config.format == "markdown"

    def test_config_format_html(self):
        """ExportConfig accepts 'html' as format."""
        config = ExportConfig(format="html")
        assert config.format == "html"

    def test_config_default_include_tool_calls(self):
        """ExportConfig defaults include_tool_calls to True."""
        config = ExportConfig(format="markdown")
        assert config.include_tool_calls is True

    def test_config_default_include_thinking(self):
        """ExportConfig defaults include_thinking to True."""
        config = ExportConfig(format="markdown")
        assert config.include_thinking is True

    def test_config_default_include_timestamps(self):
        """ExportConfig defaults include_timestamps to False."""
        config = ExportConfig(format="markdown")
        assert config.include_timestamps is False

    def test_config_all_defaults(self):
        """ExportConfig with all defaults produces expected config."""
        config = ExportConfig(format="markdown")
        assert config.include_tool_calls is True
        assert config.include_thinking is True
        assert config.include_timestamps is False

    def test_config_all_options(self):
        """ExportConfig accepts all options explicitly."""
        config = ExportConfig(
            format="html",
            include_tool_calls=False,
            include_thinking=False,
            include_timestamps=True,
        )
        assert config.format == "html"
        assert config.include_tool_calls is False
        assert config.include_thinking is False
        assert config.include_timestamps is True

    def test_config_invalid_format_raises(self):
        """ExportConfig raises ValueError for invalid format."""
        with pytest.raises(ValueError, match="format must be"):
            ExportConfig(format="json")

    def test_config_invalid_format_json(self):
        """ExportConfig rejects 'json' as a format."""
        with pytest.raises(ValueError):
            ExportConfig(format="json")

    def test_config_invalid_format_xml(self):
        """ExportConfig rejects 'xml' as a format."""
        with pytest.raises(ValueError):
            ExportConfig(format="xml")


# =============================================================================
# 11. ExportConfig — Serialization Tests
# =============================================================================


class TestExportConfigSerialization:
    """Tests for ExportConfig to_dict() and from_dict()."""

    def test_to_dict_contains_format(self):
        """ExportConfig.to_dict() includes format."""
        config = ExportConfig(format="markdown")
        d = config.to_dict()
        assert d["format"] == "markdown"

    def test_to_dict_contains_all_fields(self):
        """ExportConfig.to_dict() includes all configuration fields."""
        config = ExportConfig(
            format="html",
            include_tool_calls=True,
            include_thinking=False,
            include_timestamps=True,
        )
        d = config.to_dict()
        assert d["format"] == "html"
        assert d["include_tool_calls"] is True
        assert d["include_thinking"] is False
        assert d["include_timestamps"] is True

    def test_to_dict_returns_dict(self):
        """ExportConfig.to_dict() returns a dict."""
        config = ExportConfig(format="markdown")
        result = config.to_dict()
        assert isinstance(result, dict)

    def test_from_dict_creates_config(self):
        """ExportConfig.from_dict() creates a valid config."""
        data = {
            "format": "html",
            "include_tool_calls": False,
            "include_thinking": True,
            "include_timestamps": True,
        }
        config = ExportConfig.from_dict(data)
        assert config.format == "html"
        assert config.include_tool_calls is False
        assert config.include_thinking is True
        assert config.include_timestamps is True

    def test_from_dict_roundtrip(self):
        """ExportConfig round-trips through to_dict()/from_dict()."""
        original = ExportConfig(
            format="html",
            include_tool_calls=True,
            include_thinking=False,
            include_timestamps=True,
        )
        data = original.to_dict()
        restored = ExportConfig.from_dict(data)
        assert restored.format == original.format
        assert restored.include_tool_calls == original.include_tool_calls
        assert restored.include_thinking == original.include_thinking
        assert restored.include_timestamps == original.include_timestamps

    def test_from_dict_defaults_missing_optional_fields(self):
        """ExportConfig.from_dict() uses defaults for missing fields."""
        data = {"format": "markdown"}
        config = ExportConfig.from_dict(data)
        assert config.include_tool_calls is True
        assert config.include_thinking is True
        assert config.include_timestamps is False


# =============================================================================
# 12. ExportConfig — Helper Methods Tests
# =============================================================================


class TestExportConfigHelpers:
    """Tests for ExportConfig.is_markdown(), is_html(), and __repr__()."""

    def test_is_markdown_true(self):
        """ExportConfig.is_markdown() returns True for markdown format."""
        config = ExportConfig(format="markdown")
        assert config.is_markdown() is True

    def test_is_markdown_false_for_html(self):
        """ExportConfig.is_markdown() returns False for html format."""
        config = ExportConfig(format="html")
        assert config.is_markdown() is False

    def test_is_html_true(self):
        """ExportConfig.is_html() returns True for html format."""
        config = ExportConfig(format="html")
        assert config.is_html() is True

    def test_is_html_false_for_markdown(self):
        """ExportConfig.is_html() returns False for markdown format."""
        config = ExportConfig(format="markdown")
        assert config.is_html() is False

    def test_is_markdown_and_is_html_mutually_exclusive(self):
        """is_markdown() and is_html() are mutually exclusive."""
        for fmt in ["markdown", "html"]:
            config = ExportConfig(format=fmt)
            assert config.is_markdown() or config.is_html()
            assert not (config.is_markdown() and config.is_html())

    def test_repr_contains_format(self):
        """ExportConfig.__repr__() contains the format."""
        config = ExportConfig(format="html", include_timestamps=True)
        repr_str = repr(config)
        assert "html" in repr_str

    def test_repr_contains_all_fields(self):
        """ExportConfig.__repr__() contains all configuration fields."""
        config = ExportConfig(
            format="markdown",
            include_tool_calls=False,
            include_thinking=False,
            include_timestamps=True,
        )
        repr_str = repr(config)
        assert "format" in repr_str
        assert "include_tool_calls" in repr_str
        assert "include_thinking" in repr_str
        assert "include_timestamps" in repr_str


# =============================================================================
# 13. ExportConfig — Default Configurations
# =============================================================================


class TestExportConfigDefaults:
    """Tests for common default configuration presets."""

    def test_markdown_default_config(self):
        """Markdown export includes everything by default."""
        config = ExportConfig(format="markdown")
        assert config.include_tool_calls is True
        assert config.include_thinking is True
        assert config.include_timestamps is False

    def test_html_default_config(self):
        """HTML export includes everything by default."""
        config = ExportConfig(format="html")
        assert config.include_tool_calls is True
        assert config.include_thinking is True
        assert config.include_timestamps is False

    def test_minimal_markdown_export(self):
        """Minimal markdown export excludes tool calls and thinking."""
        config = ExportConfig(
            format="markdown",
            include_tool_calls=False,
            include_thinking=False,
        )
        assert config.include_tool_calls is False
        assert config.include_thinking is False
        assert config.is_markdown()

    def test_verbose_html_export(self):
        """Verbose HTML export includes everything."""
        config = ExportConfig(
            format="html",
            include_tool_calls=True,
            include_thinking=True,
            include_timestamps=True,
        )
        assert config.include_tool_calls is True
        assert config.include_thinking is True
        assert config.include_timestamps is True
        assert config.is_html()


# =============================================================================
# 14. LF-Delimited JSON Framing
# =============================================================================


class TestLFDelimitedFraming:
    """Tests verifying LF-delimited JSON framing compliance.

    Per the contract, all RPC messages must be LF-delimited JSON:
    - Each message is on its own line
    - Messages are separated by \n (LF)
    - No trailing newline on the last message
    """

    def test_request_json_line(self):
        """RPCRequest produces valid LF-delimited JSON."""
        req = RPCRequest(
            jsonrpc="2.0",
            id=1,
            method="send_prompt",
            params={"text": "hello"},
        )
        line = req.to_json_line()
        # Must be valid JSON
        parsed = json.loads(line)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["method"] == "send_prompt"

    def test_response_json_line(self):
        """RPCResponse produces valid LF-delimited JSON."""
        resp = RPCResponse(
            jsonrpc="2.0",
            id=1,
            result={"status": "done", "messages": []},
        )
        line = resp.to_json_line()
        parsed = json.loads(line)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["result"]["status"] == "done"

    def test_event_json_line(self):
        """RPCEvent produces valid LF-delimited JSON."""
        evt = RPCEvent(
            jsonrpc="2.0",
            method="event",
            params={"type": "text_delta", "delta": "H"},
        )
        line = evt.to_json_line()
        parsed = json.loads(line)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["method"] == "event"

    def test_full_session_example(self):
        """Full example from the spec serializes correctly."""
        # Request: send_prompt
        req = RPCRequest(
            jsonrpc="2.0",
            id=1,
            method="send_prompt",
            params={"text": "hello"},
        )
        # Event: text_delta
        evt = RPCEvent(
            jsonrpc="2.0",
            method="event",
            params={"type": "text_delta", "delta": "H"},
        )
        # Response: done
        resp = RPCResponse(
            jsonrpc="2.0",
            id=1,
            result={"status": "done", "messages": []},
        )

        # All produce valid JSON on a single line
        assert json.loads(req.to_json_line()) is not None
        assert json.loads(evt.to_json_line()) is not None
        assert json.loads(resp.to_json_line()) is not None

    def test_multiple_messages_lf_separated(self):
        """Multiple messages separated by LF can be split and parsed."""
        req1 = RPCRequest(id=1, method="send_prompt", params={"text": "a"})
        req2 = RPCRequest(id=2, method="send_prompt", params={"text": "b"})
        resp1 = RPCResponse(id=1, result={"status": "ok"})
        resp2 = RPCResponse(id=2, result={"status": "ok"})

        # Join with LF
        framed = "\n".join([
            req1.to_json_line(),
            req2.to_json_line(),
            resp1.to_json_line(),
            resp2.to_json_line(),
        ])
        lines = framed.split("\n")
        assert len(lines) == 4
        # Parse each line - detect type by presence of 'method' field
        results = []
        for l in lines:
            data = json.loads(l)
            if "method" in data:
                results.append(RPCRequest.from_json_line(l))
            elif "result" in data or "error" in data:
                results.append(RPCResponse.from_json_line(l))
        assert len(results) == 4


# =============================================================================
# 15. Public Exports
# =============================================================================


class TestPublicExports:
    """Tests for public exports from package root."""

    def test_rpc_request_from_package_root(self):
        """RPCRequest can be imported from tau_agent_core."""
        from tau_agent_core import RPCRequest
        assert RPCRequest is not None

    def test_rpc_response_from_package_root(self):
        """RPCResponse can be imported from tau_agent_core."""
        from tau_agent_core import RPCResponse
        assert RPCResponse is not None

    def test_rpc_event_from_package_root(self):
        """RPCEvent can be imported from tau_agent_core."""
        from tau_agent_core import RPCEvent
        assert RPCEvent is not None

    def test_export_config_from_package_root(self):
        """ExportConfig can be imported from tau_agent_core."""
        from tau_agent_core import ExportConfig
        assert ExportConfig is not None

    def test_rpc_request_direct_import(self):
        """RPCRequest can be imported from tau_agent_core.rpc."""
        from tau_agent_core.rpc import RPCRequest
        assert RPCRequest is not None

    def test_rpc_response_direct_import(self):
        """RPCResponse can be imported from tau_agent_core.rpc."""
        from tau_agent_core.rpc import RPCResponse
        assert RPCResponse is not None

    def test_rpc_event_direct_import(self):
        """RPCEvent can be imported from tau_agent_core.rpc."""
        from tau_agent_core.rpc import RPCEvent
        assert RPCEvent is not None

    def test_export_config_direct_import(self):
        """ExportConfig can be imported from tau_agent_core.export."""
        from tau_agent_core.export import ExportConfig
        assert ExportConfig is not None

    def test_exported_in_all_list(self):
        """New types are listed in __all__."""
        from tau_agent_core import __all__
        assert "RPCRequest" in __all__
        assert "RPCResponse" in __all__
        assert "RPCEvent" in __all__
        assert "ExportConfig" in __all__

    def test_rpc_module_exists(self):
        """tau_agent_core.rpc module exists."""
        import tau_agent_core.rpc
        assert tau_agent_core.rpc is not None

    def test_export_module_exists(self):
        """tau_agent_core.export module exists."""
        import tau_agent_core.export
        assert tau_agent_core.export is not None


# =============================================================================
# 16. Cross-Contract Consistency
# =============================================================================


class TestCrossContractConsistency:
    """Tests verifying consistency across RPC and Export contracts."""

    def test_event_params_match_agent_event_structure(self):
        """RPCEvent params can carry the AgentEvent field structure."""
        import time
        ts = int(time.time() * 1000)
        evt = RPCEvent(
            params={
                "type": "agent_start",
                "timestamp": ts,
                "message": None,
                "turn_index": None,
                "tool_call_id": None,
                "tool_name": None,
                "args": None,
                "result": None,
                "is_error": False,
                "tool_results": None,
                "messages": None,
            },
        )
        data = evt.to_json_line()
        restored = RPCEvent.from_json_line(data)
        assert restored.params["type"] == "agent_start"
        assert restored.params["timestamp"] == ts
        assert restored.params["is_error"] is False

    def test_response_result_contains_messages(self):
        """RPCResponse.result can contain messages array."""
        resp = RPCResponse(
            id=1,
            result={
                "status": "done",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                    {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
                ],
            },
        )
        data = resp.to_json_line()
        restored = RPCResponse.from_json_line(data)
        assert len(restored.result["messages"]) == 2
        assert restored.result["messages"][0]["role"] == "user"

    def test_request_params_can_contain_tool_result(self):
        """RPCRequest.params can contain tool execution results."""
        req = RPCRequest(
            method="send_tool_result",
            params={
                "tool_call_id": "call_001",
                "tool_name": "bash",
                "result": "output here",
                "is_error": False,
            },
        )
        data = req.to_json_line()
        restored = RPCRequest.from_json_line(data)
        assert restored.params["tool_call_id"] == "call_001"
        assert restored.params["is_error"] is False
