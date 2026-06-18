"""RPC protocol message types for τ-agent-core.

Phase 6 Subphase 0: Finalize the RPC protocol and export types.

All messages are LF-delimited JSON (JSON-RPC 2.0 format):
    {"jsonrpc": "2.0", "id": 1, "method": "send_prompt", "params": {"text": "hello"}}
    {"jsonrpc": "2.0", "id": null, "method": "event", "params": {"type": "text_delta", "delta": "H"}}
    {"jsonrpc": "2.0", "id": 1, "result": {"status": "done", "messages": [...]}}

Reference: docs/PHASE-6-SUBPHASE-0.md
Reference: docs/SUBPHASE-0.0.md lines 260-340
Reference: docs/tau-coding-agent.md lines 220-280
Reference: docs/IMPLEMENTATION-PLAN.md lines 460-500
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class RPCRequest:
    """A JSON-RPC 2.0 request message.

    Attributes:
        jsonrpc: JSON-RPC protocol version (always "2.0").
        id: Request ID (int) for matching responses. None for notifications.
        method: RPC method name ("send_prompt", "send_tool_result",
                "get_commands", etc.).
        params: Method-specific parameters, or None.
    """
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | None = None
    method: str = ""
    params: dict[str, Any] | None = None

    def to_json_line(self) -> str:
        """Serialize to a single LF-delimited JSON line.

        Returns:
            JSON string suitable for LF-delimited framing.
        """
        import json
        return json.dumps(self.__dict__, separators=(",", ":"))

    @classmethod
    def from_json_line(cls, line: str) -> RPCRequest:
        """Deserialize from a LF-delimited JSON line.

        Args:
            line: A single LF-delimited JSON string.

        Returns:
            An RPCRequest instance.
        """
        import json
        data = json.loads(line)
        return cls(**data)


@dataclass
class RPCResponse:
    """A JSON-RPC 2.0 response message.

    Either `result` or `error` must be set (never both).
    For error responses, `result` is None and `error` is an error dict.
    For success responses, `error` is None and `result` contains the result.

    Attributes:
        jsonrpc: JSON-RPC protocol version (always "2.0").
        id: Request ID matching the original request. None for notifications.
        result: The response result, or None on error.
        error: The error dict on failure, or None on success.
    """
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def to_json_line(self) -> str:
        """Serialize to a single LF-delimited JSON line.

        Returns:
            JSON string suitable for LF-delimited framing.
        """
        import json
        return json.dumps(self.__dict__, separators=(",", ":"))

    @classmethod
    def from_json_line(cls, line: str) -> RPCResponse:
        """Deserialize from a LF-delimited JSON line.

        Args:
            line: A single LF-delimited JSON string.

        Returns:
            An RPCResponse instance.
        """
        import json
        data = json.loads(line)
        return cls(**data)

    def is_error(self) -> bool:
        """Check if this response represents an error."""
        return self.error is not None


@dataclass
class RPCEvent:
    """A JSON-RPC 2.0 event notification (fire-and-forget).

    Events use method="event" and carry an AgentEvent as params.
    They have no request ID and expect no response.

    Attributes:
        jsonrpc: JSON-RPC protocol version (always "2.0").
        method: Always "event" for notifications.
        params: The event payload (AgentEvent serialized as dict).
    """
    jsonrpc: Literal["2.0"] = "2.0"
    method: Literal["event"] = "event"
    params: dict[str, Any] = field(default_factory=dict)

    def to_json_line(self) -> str:
        """Serialize to a single LF-delimited JSON line.

        Returns:
            JSON string suitable for LF-delimited framing.
        """
        import json
        return json.dumps(self.__dict__, separators=(",", ":"))

    @classmethod
    def from_json_line(cls, line: str) -> RPCEvent:
        """Deserialize from a LF-delimited JSON line.

        Args:
            line: A single LF-delimited JSON string.

        Returns:
            An RPCEvent instance.
        """
        import json
        data = json.loads(line)
        return cls(**data)
