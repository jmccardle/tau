# τ-agent-core

> The agent orchestration core of the τ (tau-agent-core) system.

## Overview

`tau-agent-core` is the central orchestration layer of τ. It provides:

- **AgentSession**: High-level session API combining agent loop, session manager, and events
- **AgentLoop**: Core loop that drives the conversation (LLM calls → tool execution → streaming)
- **SessionManager**: Persistent session storage (JSONL-based)
- **EventBus**: Event dispatch for TUI widgets and extensions
- **Extension System**: Plugin architecture for custom behavior
- **Built-in Tools**: `read`, `write`, `edit`, `bash`, `ls`, `grep`, `find`
- **RPC Protocol**: Remote procedure calls for headless operation
- **SDK**: `create_agent_session()` factory for easy setup

## Quick Start

```python
from tau_agent_core.sdk import create_agent_session

# Create a session
session = create_agent_session(
    model="gpt-4o",
    tools=["read", "write", "bash"],
)

# Send a prompt
messages = await session.prompt("Write a Python function that sorts a list")

# Subscribe to events
session.subscribe(lambda event: print(event.type))

# Abort if needed
session.abort()
```

## Architecture

```
tau-agent-core/
├── src/tau_agent_core/
│   ├── __init__.py          # Public exports
│   ├── sdk.py               # create_agent_session() — main SDK entry
│   ├── agent_session.py     # AgentSession — public session API
│   ├── agent_loop.py        # AgentLoop — conversation loop
│   ├── agent_loop_types.py  # AgentLoop type definitions
│   ├── session.py           # SessionState
│   ├── session_manager.py   # SessionManager — JSONL persistence
│   ├── events.py            # EventBus, AgentEvent
│   ├── extension_types.py   # ExtensionAPI, ExtensionContext, ExtensionUI
│   ├── extensions/          # Extension loader and registry
│   │   ├── __init__.py
│   │   ├── loader.py
│   │   ├── registry.py
│   │   └── events.py
│   ├── tools/               # Built-in tools
│   │   ├── __init__.py
│   │   ├── base.py          # AgentTool, ToolDefinition, ToolBatchResult
│   │   ├── read.py          # ReadTool
│   │   ├── write.py         # WriteTool
│   │   ├── edit.py          # EditTool
│   │   ├── bash.py          # BashTool
│   │   ├── ls.py            # LsTool
│   │   ├── grep.py          # GrepTool
│   │   └── find.py          # FindTool
│   ├── rpc.py               # RPC server/client protocol
│   ├── compaction.py        # Context compaction
│   ├── system_prompt.py     # System prompt builder
│   └── settings.py          # Settings management
```

## Package Boundaries

- **τ-coding-agent** imports from τ-agent-core:
  - `tau_agent_core.AgentSession`
  - `tau_agent_core.SessionManager`
  - `tau_agent_core.AgentEvent`
- **τ-coding-agent** does NOT import from τ-agent-core:
  - `tau_agent_core.agent_loop.*` — internal implementation
  - `tau_agent_core.tools.*` — tools are registered via AgentSession
  - `tau_agent_core.extensions.*` — loaded via AgentSession
  - `tau_agent_core.compaction.*` — triggered via AgentSession

## Writing an Extension

```python
# my_extension.py
def my_extension(api):
    """Register extension handlers."""

    # Listen to agent events
    def on_agent_start(event):
        print(f"Agent starting, turn {event.turn_index}")

    api.on("agent_start", on_agent_start)

    # Register a custom tool
    api.register_tool({
        "name": "greet",
        "label": "Greet",
        "description": "Greet someone by name",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name to greet"}
            },
            "required": ["name"],
        },
    })

    # Use the TUI API (in TUI mode)
    async def on_confirm(event):
        confirmed = await api.ui.confirm(
            "Confirm deletion", "Are you sure?"
        )
        return confirmed
```

## Session Manager

```python
from tau_agent_core.session_manager import SessionManager

# Create a file-based session manager
mgr = SessionManager(cwd="/path/to/project")

# Create a new session
session_path = mgr.new_session()

# Append entries
mgr.append_entry({
    "session_id": session_path,
    "type": "message",
    "message": {"role": "user", "content": [{"type": "text", "text": "Hello"}]}
})

# List all sessions
for session_info in mgr.list():
    print(session_info.session_path, session_info.message_count)

# Fork a session
fork_path = mgr.fork(session_path)

# Clone a session
clone_path = mgr.clone(session_path)
```

## In-Memory Mode (for testing)

```python
from tau_agent_core.session_manager import SessionManager

# In-memory session manager for tests
mgr = SessionManager.in_memory()

from tau_agent_core.sdk import create_agent_session
session = create_agent_session(
    model="gpt-4o",
    session_manager=mgr,
)

await session.prompt("Hello, world!")
```

## Error Handling

- **Provider errors**: Converted to `ErrorEvent`, shown in chat
- **Tool errors**: Wrapped in `ToolResultMessage` with `is_error=True`
- **Extension errors**: Logged by EventBus, agent loop continues
- **Network errors**: Retried by the SDK, `ErrorEvent` on failure

## Performance

- **30Hz throttle**: Chat display updates at most 30 times/second
- **Large file handling**: Files >1MB are read with offset/limit
- **Memory efficient**: Lazy loading for sessions with >1000 messages

## License

MIT
