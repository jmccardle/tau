# Phase 6 Subphase 1 — RPC Mode

> **Topic**: Implement JSONL over stdin/stdout RPC for external integration.

## Scope

This subphase implements `tau_agent_core.rpc.RPCHandler` — a JSON-RPC 2.0 server that runs over stdin/stdout. This enables:
1. External tools to invoke the τ agent
2. Custom UIs to connect to τ
3. CI/CD pipelines to use τ as a coding agent
4. Pi's RPC integration

## Reference

- `SUBPHASE-6-SUBPHASE-0.md`: RPC message types
- `SUBPHASE-0.0.md` lines 200-220: AgentEvent contract (for event serialization)
- `docs/tau-coding-agent.md` lines 220-280: RPC mode design
- `docs/IMPLEMENTATION-PLAN.md` lines 460-500: RPC spec
- pi's `rpc.js` (reference — same JSONL over stdin/stdout pattern)

## Implementation Outline

### `tau_agent_core/rpc.py`

```python
import asyncio
import json
import sys
from typing import Any

class RPCHandler:
    """JSON-RPC 2.0 handler over stdin/stdout."""

    def __init__(self, session: AgentSession):
        self._session = session
        self._request_id = 0
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._stdin_task = None
        self._stdout_task = None
        self._output_queue = asyncio.Queue()

    async def run(self):
        """Run the RPC server."""
        self._stdin_task = asyncio.create_task(self._read_stdin())
        self._stdout_task = asyncio.create_task(self._write_stdout())
        try:
            await asyncio.gather(self._stdin_task, self._stdout_task)
        except asyncio.CancelledError:
            pass

    async def _read_stdin(self):
        """Read JSON-RPC requests from stdin."""
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
        """Read lines from stdin asynchronously."""
        while True:
            line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            yield line

    async def _write_stdout(self):
        """Write responses and events to stdout."""
        while True:
            item = await self._output_queue.get()
            await asyncio.get_event_loop().run_in_executor(
                None, self._write_line, item
            )

    def _write_line(self, data: dict):
        sys.stdout.write(json.dumps(data) + "\n")
        sys.stdout.flush()

    async def _handle_request(self, request: dict):
        """Route a request to the appropriate handler."""
        method = request.get("method", "")
        params = request.get("params", {})
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
        """Handle a prompt request."""
        text = params.get("text", "")
        images = params.get("images")

        # Subscribe to events
        events = []
        self._session.subscribe(lambda e: self._output_queue.put_nowait({
            "jsonrpc": "2.0",
            "method": "event",
            "params": self._serialize_event(e),
        }))

        # Run the prompt
        messages = await self._session.prompt(text, images)

        # Send final response
        return {
            "status": "done",
            "messages": [self._serialize_message(m) for m in messages],
            "event_count": len(events),
        }

    async def _handle_send_tool_result(self, params: dict) -> dict:
        """Send a tool result (for external tool execution)."""
        # ... handle tool result
        return {"status": "accepted"}

    async def _handle_abort(self, params: dict) -> dict:
        """Abort the current agent turn."""
        self._session.abort()
        return {"status": "aborted"}

    async def _handle_get_commands(self, params: dict) -> dict:
        """Return available commands."""
        return {
            "commands": [
                {"name": "/compact", "description": "Compact the session"},
                {"name": "/fork", "description": "Fork the session"},
                # ...
            ]
        }

    async def _handle_get_tools(self, params: dict) -> dict:
        """Return available tools."""
        tools = self._session._registry.get_all_tools()
        return {
            "tools": [
                {"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in tools
            ]
        }

    async def _handle_get_session_info(self, params: dict) -> dict:
        """Return session information."""
        return {
            "model": self._session._model.id,
            "message_count": len(self._session.messages),
            "is_streaming": self._session.is_streaming,
        }

    async def _send_response(self, id: int | None, result: dict):
        """Send a JSON-RPC response."""
        await self._output_queue.put({
            "jsonrpc": "2.0",
            "id": id,
            "result": result,
        })

    async def _send_error(self, id: int | None, message: str):
        """Send a JSON-RPC error response."""
        await self._output_queue.put({
            "jsonrpc": "2.0",
            "id": id,
            "error": {"code": -32603, "message": message},
        })

    def _serialize_event(self, event: AgentEvent) -> dict:
        """Serialize an AgentEvent to a dict for RPC."""
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

    def _serialize_message(self, message: Message | None) -> dict | None:
        """Serialize a Message to a dict for RPC."""
        if message is None:
            return None
        return message.model_dump() if hasattr(message, "model_dump") else dict(message)
```

### CLI Entry Point

```python
# tau_coding_agent/cli.py
@app.command()
def rpc():
    """Run in RPC mode (JSONL over stdin/stdout)."""
    session = create_agent_session(...)
    handler = RPCHandler(session)
    asyncio.run(handler.run())
```

## Done Criteria

- `RPCHandler.run()` reads from stdin and writes to stdout
- `send_prompt` request streams events and returns final result
- `send_tool_result` request accepts tool results
- `abort` request aborts the current turn
- `get_commands` returns available commands
- `get_tools` returns available tools
- `get_session_info` returns session metadata
- Events are serialized as JSON and sent immediately
- Responses are JSON-RPC 2.0 compliant
- Invalid JSON on stdin produces an error response
- Unknown methods produce a "method not found" error

## Testing Strategy

### Test 1: Send prompt

```python
async def test_send_prompt(tmp_path, mock_openai, capsys):
    mgr = SessionManager.in_memory()
    session = create_agent_session(
        model="gpt-4o",
        session_manager=mgr,
    )
    handler = RPCHandler(session)

    # Simulate stdin
    stdin_data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "send_prompt", "params": {"text": "hello"}})

    # Capture stdout
    stdout_capture = []
    handler._write_line = lambda d: stdout_capture.append(json.dumps(d))

    # Run handler with mocked stdin
    ...

    # Check output
    assert any("jsonrpc" in str(d) and "event" in str(d) for d in stdout_capture)
```

### Test 2: Abort

```python
async def test_abort(mock_openai):
    mgr = SessionManager.in_memory()
    session = create_agent_session(
        model="gpt-4o",
        session_manager=mgr,
    )
    handler = RPCHandler(session)

    result = await handler._handle_abort({})
    assert result == {"status": "aborted"}
    assert session.is_streaming is False
```

### Test 3: Get tools

```python
async def test_get_tools(mock_openai):
    mgr = SessionManager.in_memory()
    session = create_agent_session(
        model="gpt-4o",
        session_manager=mgr,
        tools=["read", "bash"],
    )
    handler = RPCHandler(session)

    result = await handler._handle_get_tools({})
    assert "tools" in result
    assert len(result["tools"]) == 2
```

### Test 4: Get session info

```python
async def test_get_session_info(mock_openai):
    mgr = SessionManager.in_memory()
    session = create_agent_session(
        model="gpt-4o",
        session_manager=mgr,
    )
    handler = RPCHandler(session)

    result = await handler._handle_get_session_info({})
    assert result["model"] == "gpt-4o"
    assert isinstance(result["message_count"], int)
    assert isinstance(result["is_streaming"], bool)
```

### Test 5: Invalid JSON

```python
async def test_invalid_json(mock_openai, tmp_path):
    handler = RPCHandler(create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
    ))
    # Simulate invalid JSON on stdin
    # Should send error response
    ...
```

### Test 6: Unknown method

```python
async def test_unknown_method(mock_openai):
    mgr = SessionManager.in_memory()
    handler = RPCHandler(create_agent_session(
        model="gpt-4o",
        session_manager=mgr,
    ))
    result = await handler._handle_request({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "unknown_method",
    })
    # Should send error response
    ...
```

## Success Signal

All 6 test categories pass. The RPC handler correctly reads from stdin, processes requests, sends events, and writes responses to stdout. It handles invalid JSON, unknown methods, and errors gracefully. The JSON-RPC 2.0 format is correct.
