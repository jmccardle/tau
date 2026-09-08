"""RPC wire envelope: the JSON-RPC 2.0 request/response/event dataclasses.

Block [2] Dialect, per docs/REMOTE-CONTROL.md section 3/4. Split out of the
former rpc.py (docs/REMOTE-CONTROL.md section 7.3, requirement X1) — this
module has no transport or dispatch concerns, only the envelope shapes.

Reference: docs/PHASE-6-SUBPHASE-0.md
Reference: docs/SUBPHASE-0.0.md lines 260-340
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from tau_llm.docs import agent_facing

PARSE_ERROR = -32700  # the client sent bytes that are not valid JSON
INVALID_REQUEST = -32600  # valid JSON, but not a valid Request object (no `method`)
METHOD_NOT_FOUND = -32601  # `method` names no table entry, or a declined one
INVALID_PARAMS = -32602  # `params` fails the method's params_schema
INTERNAL_ERROR = -32603  # the handler raised something it did not raise deliberately

SUBMISSION_REJECTED = -32000
COMMAND_NOT_SUPPORTED = -32001
TURN_STILL_RUNNING = -32002
REQUEST_TOO_LARGE = -32003
SESSION_NOT_PERSISTED = -32004


@agent_facing(topic="rpc")
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


@agent_facing(topic="rpc")
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


@agent_facing(topic="rpc")
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
