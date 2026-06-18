# τ-ai

> AI provider abstractions for the τ (tau-agent-core) system.

## Overview

`tau-ai` is the AI provider layer of the τ monorepo. It provides:

- **Model types**: `Model`, `ToolDefinition`, `TextContent`, `ToolCall`, `ImageContent`, `ThinkingContent`, `ToolResultMessage`, etc.
- **Provider implementations**: `OpenAIProvider`, `OpenAIResponsesProvider` (via `providers/`)
- **Tool definitions**: `define_tool()`, `validate_tool_arguments()`
- **Streaming events**: `TextDeltaEvent`, `ToolCallDeltaEvent`, `DoneEvent`, `ErrorEvent`
- **Client API**: `stream_simple()` — unified streaming interface
- **Abort signals**: `AbortSignal` — cancel long-running operations

## Quick Start

```python
from tau_ai.types import Model
from tau_ai.client import Client

# Create a client
client = Client(model="gpt-4o")

# Stream a response
async for event in client.stream_simple("Say hello!"):
    print(event)
```

## Architecture

```
tau-ai/
├── src/tau_ai/
│   ├── __init__.py          # Public re-exports
│   ├── types.py             # Message, tool, model types
│   ├── client.py            # Client API (stream_simple)
│   ├── streaming.py         # Stream event types
│   ├── abort.py             # AbortSignal
│   ├── tools.py             # Tool definition utilities
│   └── providers/
│       ├── base.py          # Provider base class
│       ├── openai.py        # OpenAI Chat Completions
│       ├── openai_responses.py  # OpenAI Responses API
│       └── registry.py      # Provider registry
```

## Package Boundaries

- **τ-agent-core** imports from τ-ai:
  - `tau_ai.types.*` — message, tool, model types
  - `tau_ai.tools.*` — `define_tool()`, `validate_tool_arguments()`
  - `tau_ai.client.*` — `stream_simple()`
- **τ-agent-core** does NOT import from τ-ai:
  - `tau_ai.providers.*` — provider internals
  - `tau_ai.streaming.*` — streaming event types

## Usage Patterns

### Custom System Prompt

```python
from tau_ai.client import Client

client = Client(
    model="gpt-4o",
    system_prompt="You are a helpful coding assistant.",
)
```

### Streaming Events

```python
from tau_ai.streaming import TextDeltaEvent, DoneEvent, ErrorEvent

async for event in client.stream_simple("Hello"):
    if isinstance(event, TextDeltaEvent):
        print(event.delta, end="")
    elif isinstance(event, DoneEvent):
        print(f"\nTokens: {event.usage}")
    elif isinstance(event, ErrorEvent):
        print(f"Error: {event.message}")
```

### Abort a Long Operation

```python
from tau_ai.abort import AbortSignal
from tau_ai.client import Client

signal = AbortSignal()
client = Client(model="gpt-4o", abort_signal=signal)

# Cancel from another thread/task
async def cancel_after(delay):
    import asyncio
    await asyncio.sleep(delay)
    signal.abort()

asyncio.create_task(cancel_after(5.0))
```

## License

MIT
