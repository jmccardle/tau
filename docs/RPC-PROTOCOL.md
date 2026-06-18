# τ RPC Protocol

> JSON-RPC 2.0 protocol for headless τ-agent-core operation.

## Overview

The τ RPC protocol allows external tools, CI/CD pipelines, and custom UIs to interact with the τ agent without a terminal. It uses **JSON-RPC 2.0** over **stdin/stdout** with LF-delimited framing.

## Transport

- **Input**: JSON-RPC requests read from stdin, one per line
- **Output**: JSON-RPC responses and events written to stdout, one per line
- **Framing**: Each line is a complete JSON-RPC message, terminated by a newline (`\n`)
- **Encoding**: UTF-8

### Message Format

All messages follow the JSON-RPC 2.0 specification:

```
{"jsonrpc":"2.0","id":1,"method":"send_prompt","params":{"text":"hello"}}
{"jsonrpc":"2.0","id":1,"result":{"status":"done","messages":[...],"event_count":5}}
{"jsonrpc":"2.0","method":"event","params":{"type":"agent_start","timestamp":1234567890}}
{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"Error message"}}
```

### Message Types

| Type | Description |
|------|-------------|
| `RPCRequest` | Client request to the server (has `id` and `method`) |
| `RPCResponse` | Server response to a request (has `id`, either `result` or `error`) |
| `RPCEvent` | Server event notification (no `id`, method is always `"event"`) |

## Available Methods

### `send_prompt`

Send a user prompt and run the agent loop. The server responds with the result after the agent completes the turn.

**Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "send_prompt",
  "params": {
    "text": "Write a Python function to sort a list",
    "images": null
  }
}
```

**Success Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "status": "done",
    "messages": [
      {
        "role": "user",
        "content": [{"type": "text", "text": "Write a Python function to sort a list"}]
      },
      {
        "role": "assistant",
        "content": [{"type": "text", "text": "Response to: Write a Python function to sort a list"}]
      }
    ],
    "event_count": 5
  }
}
```

**Error Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32603,
    "message": "Error description"
  }
}
```

### `send_tool_result`

Send a tool execution result. Useful when external tools execute commands.

**Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "send_tool_result",
  "params": {
    "tool_call_id": "call_001",
    "result": "file1.txt\nfile2.txt"
  }
}
```

**Success Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "status": "accepted"
  }
}
```

### `abort`

Abort the current agent turn.

**Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "abort",
  "params": {}
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "status": "aborted"
  }
}
```

### `get_commands`

Return the list of available commands.

**Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "get_commands",
  "params": {}
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "commands": [
      {"name": "/compact", "description": "Compact the session"},
      {"name": "/fork", "description": "Fork the session"},
      {"name": "/clone", "description": "Clone the session"},
      {"name": "/list", "description": "List sessions"},
      {"name": "/exit", "description": "Exit the agent"}
    ]
  }
}
```

### `get_tools`

Return the list of available tools.

**Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "get_tools",
  "params": {}
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "tools": [
      {
        "name": "read",
        "description": "Read file contents",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}
      }
    ]
  }
}
```

### `get_session_info`

Return session metadata (model, message count, streaming state).

**Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "method": "get_session_info",
  "params": {}
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "result": {
    "model": "gpt-4o",
    "message_count": 4,
    "is_streaming": false
  }
}
```

## Event Notifications

Events are sent from server to client at any time (not tied to a specific request ID).

### Event Types

| Event Type | Description |
|------------|-------------|
| `agent_start` | Agent loop has started |
| `agent_end` | Agent loop has finished |
| `turn_start` | New agent turn started |
| `turn_end` | Agent turn finished |
| `message_start` | New message being created |
| `message_update` | Message content updated |
| `message_end` | Message creation complete |
| `tool_execution_start` | Tool execution started |
| `tool_execution_update` | Tool execution progress update |
| `tool_execution_end` | Tool execution finished |

### Event Format

```json
{
  "jsonrpc": "2.0",
  "method": "event",
  "params": {
    "type": "message_update",
    "timestamp": 1718668800000,
    "message": {
      "role": "assistant",
      "content": [{"type": "text", "text": "Hello, world!"}]
    },
    "is_error": false
  }
}
```

### Event Fields

All events include:
- `type` (str): Event type (see table above)
- `timestamp` (int): Millisecond epoch
- `is_error` (bool): Whether this event indicates an error

Conditional fields by event type:

| Field | Event Types | Description |
|-------|-------------|-------------|
| `message` | message_*, agent_* | The message being modified |
| `turn_index` | turn_* | Turn number (0-based) |
| `tool_name` | tool_execution_* | Name of the tool |
| `tool_call_id` | tool_execution_* | Unique tool call ID |
| `args` | tool_execution_start | Tool execution arguments |
| `result` | tool_execution_end | Tool execution result |
| `tool_results` | turn_end | List of tool result messages |
| `messages` | agent_end | Final list of messages |

## Example: Full Client

### Python Client

```python
import asyncio
import json
import sys

class TauRPCClient:
    """RPC client for τ-agent-core."""

    def __init__(self):
        self._pending = {}
        self._next_id = 0

    async def run(self):
        """Run the client, reading from stdin and writing to stdout."""
        # Start a task to read stdout
        read_task = asyncio.create_task(self._read_output())

        # Send a prompt
        result = await self.send_prompt("Write a Python function to sort a list")
        print("Result:", json.dumps(result, indent=2))

        # Get session info
        info = await self.get_session_info()
        print("Session info:", json.dumps(info, indent=2))

        # Get tools
        tools = await self.get_tools()
        print("Tools:", json.dumps(tools, indent=2))

        # Abort
        await self.abort()

        read_task.cancel()

    async def _next_id(self):
        self._next_id += 1
        return self._next_id

    async def send_prompt(self, text: str, images: list | None = None) -> dict:
        """Send a prompt to the agent."""
        msg_id = await self._next_id()
        request = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "send_prompt",
            "params": {"text": text, "images": images}
        }
        sys.stdout.write(json.dumps(request) + "\n")
        sys.stdout.flush()
        # Wait for response (in production, use a proper request/response queue)
        return {}  # Simplified

    async def abort(self) -> dict:
        msg_id = await self._next_id()
        request = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "abort",
            "params": {}
        }
        sys.stdout.write(json.dumps(request) + "\n")
        sys.stdout.flush()
        return {}

    async def get_session_info(self) -> dict:
        msg_id = await self._next_id()
        request = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "get_session_info",
            "params": {}
        }
        sys.stdout.write(json.dumps(request) + "\n")
        sys.stdout.flush()
        return {}

    async def get_tools(self) -> dict:
        msg_id = await self._next_id()
        request = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "get_tools",
            "params": {}
        }
        sys.stdout.write(json.dumps(request) + "\n")
        sys.stdout.flush()
        return {}

    async def _read_output(self):
        """Read responses from stdout."""
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            print(f"Response: {json.dumps(data, indent=2)}")


if __name__ == "__main__":
    client = TauRPCClient()
    asyncio.run(client.run())
```

### Shell Usage

```bash
# Start τ in RPC mode and interact via stdin
tau --rpc --port 0

# Or use a pipe for simple queries
echo '{"jsonrpc":"2.0","id":1,"method":"get_session_info","params":{}}' | tau --rpc --port 0
```

## Error Codes

| Code | Description |
|------|-------------|
| `-32700` | Parse error (invalid JSON) |
| `-32600` | Invalid Request |
| `-32601` | Method not found |
| `-32602` | Invalid params |
| `-32603` | Internal error |
| `-32000` to `-32099` | Server error (reserved) |

## Message Schema

### RPCRequest
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "send_prompt",
  "params": {...}
}
```

### RPCResponse (Success)
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {...}
}
```

### RPCResponse (Error)
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32603,
    "message": "Error message"
  }
}
```

### RPCEvent
```json
{
  "jsonrpc": "2.0",
  "method": "event",
  "params": {
    "type": "message_update",
    "timestamp": 1718668800000
  }
}
```

## License

MIT
