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

import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from tau_agent_core.agent_session import AgentSession
    from tau_agent_core.events import AgentEvent


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


class RPCHandler:
    """JSON-RPC 2.0 server over stdin/stdout.

    Implements the RPC protocol for τ-agent-core. External tools,
    custom UIs, and CI/CD pipelines can connect via this handler.

    Supported methods:
    - send_prompt: Send a user prompt and run the agent loop
    - send_tool_result: Send a tool execution result
    - abort: Abort the current agent turn
    - get_commands: Return available commands
    - get_tools: Return available tools
    - get_session_info: Return session metadata

    Reference: docs/PHASE-6-SUBPHASE-1.md
    Reference: SUBPHASE-0.0.md AgentSession interface
    """

    def __init__(self, session: "AgentSession") -> None:
        """Initialize the RPC handler.

        Args:
            session: The AgentSession to manage.
        """
        self._session = session  # type: ignore[assignment]
        self._request_id = 0
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._stdin_task = None  # type: asyncio.Task | None
        self._stdout_task = None  # type: asyncio.Task | None
        self._output_queue: asyncio.Queue = asyncio.Queue()
        self._running = False

    async def run(self) -> None:
        """Run the RPC server.

        Reads JSON-RPC requests from stdin and writes responses/events
        to stdout. Runs until stdin is closed.
        """
        self._running = True
        self._stdin_task = asyncio.create_task(self._read_stdin())
        self._stdout_task = asyncio.create_task(self._write_stdout())
        try:
            await asyncio.gather(self._stdin_task, self._stdout_task)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    async def stop(self) -> None:
        """Stop the RPC server gracefully."""
        self._running = False
        if self._stdin_task:
            self._stdin_task.cancel()
            try:
                await self._stdin_task
            except asyncio.CancelledError:
                pass
        if self._stdout_task:
            self._stdout_task.cancel()
            try:
                await self._stdout_task
            except asyncio.CancelledError:
                pass

    async def _read_stdin(self) -> None:
        """Read JSON-RPC requests from stdin asynchronously."""
        buffer = ""
        async for line in self._read_lines():
            buffer += line
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                    await self._handle_request(request)
                except json.JSONDecodeError as e:
                    await self._send_error(None, f"Invalid JSON: {e}")

    async def _read_lines(self):
        """Read lines from stdin asynchronously.

        Yields:
            Lines from stdin as strings.
        """
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    break
                yield line
            except (OSError, ValueError):
                break

    async def _write_stdout(self) -> None:
        """Write responses and events to stdout asynchronously."""
        loop = asyncio.get_event_loop()
        while self._running or not self._output_queue.empty():
            try:
                item = await asyncio.wait_for(self._output_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if not self._running:
                    break
                continue
            try:
                line = json.dumps(item, separators=(",", ":"))
                loop.run_in_executor(None, self._write_line, line)
            except Exception:
                pass

    def _write_line(self, data: str) -> None:
        """Write a single line to stdout (runs in executor).

        Args:
            data: The JSON string to write.
        """
        sys.stdout.write(data + "\n")
        sys.stdout.flush()

    async def _handle_request(self, request: dict) -> None:
        """Route a JSON-RPC request to the appropriate handler.

        Args:
            request: The parsed JSON-RPC request dict.
        """
        method = request.get("method", "")
        params = request.get("params", {}) or {}
        msg_id = request.get("id")

        handlers = {
            "send_prompt": self._handle_send_prompt,
            "send_tool_result": self._handle_send_tool_result,
            "abort": self._handle_abort,
            "get_commands": self._handle_get_commands,
            "get_tools": self._handle_get_tools,
            "get_session_info": self._handle_get_session_info,
        }

        handler = handlers.get(method)
        if handler:
            try:
                result = await handler(params)
                await self._send_response(msg_id, result)
            except Exception as e:
                await self._send_error(msg_id, str(e))
        else:
            await self._send_error(msg_id, f"Unknown method: {method}")

    async def _handle_send_prompt(self, params: dict) -> dict:
        """Handle a prompt request.

        Subscribes to session events, runs the prompt, and returns
        the result.

        Args:
            params: Request params with 'text' and optional 'images'.

        Returns:
            Result dict with status, messages, and event count.
        """
        text = params.get("text", "")
        images = params.get("images")

        # Capture events during prompt
        event_count = [0]

        def capture_event(event):
            """Capture events and send them to output queue."""
            event_count[0] += 1
            event_data = self._serialize_event(event)
            self._output_queue.put_nowait(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": event_data,
                }
            )

        # Subscribe to events before running prompt
        self._session.subscribe(capture_event)

        # Run the prompt
        messages = await self._session.prompt(text, images)

        # Send final response
        return {
            "status": "done",
            "messages": [self._serialize_message(m) for m in messages],
            "event_count": event_count[0],
        }

    async def _handle_send_tool_result(self, params: dict) -> dict:
        """Send a tool result (for external tool execution).

        Args:
            params: Tool result params with tool_call_id and result.

        Returns:
            Result dict with status.
        """
        # For now, just acknowledge receipt
        # Full implementation would integrate with the agent loop
        return {"status": "accepted"}

    async def _handle_abort(self, params: dict) -> dict:
        """Abort the current agent turn.

        Args:
            params: Request params (unused, included for protocol consistency).

        Returns:
            Result dict with status 'aborted'.
        """
        self._session.abort()
        return {"status": "aborted"}

    async def _handle_get_commands(self, params: dict) -> dict:
        """Return available commands.

        Args:
            params: Request params (unused).

        Returns:
            Dict with 'commands' list.
        """
        return {
            "commands": [
                {"name": "/compact", "description": "Compact the session"},
                {"name": "/fork", "description": "Fork the session"},
                {"name": "/clone", "description": "Clone the session"},
                {"name": "/list", "description": "List sessions"},
                {"name": "/exit", "description": "Exit the agent"},
            ]
        }

    async def _handle_get_tools(self, params: dict) -> dict:
        """Return available tools.

        Args:
            params: Request params (unused).

        Returns:
            Dict with 'tools' list.
        """
        tools = self._session._tools
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.definition.description,
                    "parameters": t.definition.parameters,
                }
                for t in tools
            ]
        }

    async def _handle_get_session_info(self, params: dict) -> dict:
        """Return session information.

        Args:
            params: Request params (unused).

        Returns:
            Dict with model, message_count, and is_streaming.
        """
        return {
            "model": self._session._model.id,
            "message_count": len(self._session.messages),
            "is_streaming": self._session.is_streaming,
        }

    async def _send_response(self, id: int | None, result: dict) -> None:
        """Send a JSON-RPC success response.

        Args:
            id: The request ID to match.
            result: The response result dict.
        """
        await self._output_queue.put(
            {
                "jsonrpc": "2.0",
                "id": id,
                "result": result,
            }
        )

    async def _send_error(self, id: int | None, message: str) -> None:
        """Send a JSON-RPC error response.

        Args:
            id: The request ID to match, or None for notifications.
            message: The error message.
        """
        await self._output_queue.put(
            {
                "jsonrpc": "2.0",
                "id": id,
                "error": {"code": -32603, "message": message},
            }
        )

    def _serialize_event(self, event: "AgentEvent") -> dict:
        """Serialize an AgentEvent to a dict for RPC.

        Args:
            event: The AgentEvent to serialize.

        Returns:
            Dict representation of the event.
        """
        return {
            "type": event.type,
            "timestamp": event.timestamp,
            "message": self._serialize_message(event.message) if event.message else None,
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "args": event.args,
            "result": event.result,
            "is_error": event.is_error,
            "tool_results": [self._serialize_message(m) for m in (event.tool_results or [])],
            "messages": [self._serialize_message(m) for m in (event.messages or [])],
        }

    def _serialize_message(self, message) -> dict | None:
        """Serialize a Message to a dict for RPC.

        Args:
            message: A Message-like object (dict, pydantic model, etc.).

        Returns:
            Dict representation of the message, or None.
        """
        if message is None:
            return None
        if hasattr(message, "model_dump"):
            dumped: dict[Any, Any] = message.model_dump()
            return dumped
        if isinstance(message, dict):
            return message
        return dict(message)
