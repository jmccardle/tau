# τ-coding-agent

> The terminal UI for τ — a coding agent with real-time streaming, tool execution, and session management.

## Overview

`tau-coding-agent` is the terminal user interface (TUI) for the τ system. Built on [Textual](https://textual.textualize.io/), it provides:

- **Real-time chat**: Streamed responses from the LLM, displayed token by token
- **Tool execution display**: Visual representation of tool calls and results
- **Session tree**: Browse, fork, clone, and switch between sessions
- **Input bar**: Command-line style input with history navigation
- **Footer**: Status indicators for streaming, tool calls, and session info
- **Extension integration**: Extensions can hook into the event bus
- **RPC mode**: Headless operation via RPC protocol

## Quick Start

```bash
# Run the τ coding agent
tau
```

```python
# Use programmatically
from tau_agent_core.sdk import create_agent_session

session = create_agent_session(
    model="gpt-4o",
    tools=["read", "write", "bash"],
)
messages = await session.prompt("Write a Python function")
```

## Architecture

```
tau-coding-agent/
├── src/tau_coding_agent/
│   ├── __init__.py          # Package init
│   ├── app.py               # Main Textual application
│   ├── cli.py               # CLI argument parser
│   ├── config.py            # Configuration management
│   └── widgets/
│       ├── __init__.py
│       ├── chat_display.py       # Chat message display
│       ├── chat_display_data.py  # Message data structures
│       ├── tool_call_widget.py   # Tool call display
│       ├── tool_result_widget.py # Tool result display
│       ├── session_tree.py       # Session browser
│       ├── input_bar.py          # Input field with history
│       └── footer.py             # Status bar
```

## Package Boundaries

- **τ-coding-agent** imports from τ-agent-core:
  - `tau_agent_core.AgentSession` — the ONLY import for core logic
  - `tau_agent_core.SessionManager` — session management
  - `tau_agent_core.AgentEvent` — event types for subscription
- **τ-coding-agent** does NOT import from τ-agent-core:
  - `tau_agent_core.agent_loop.*` — internal implementation
  - `tau_agent_core.tools.*` — tools registered via AgentSession
  - `tau_agent_core.extensions.*` — loaded via AgentSession

## Widgets

### Chat Display

The primary widget for displaying conversation. Supports:
- Token-by-token streaming updates
- Tool call blocks with expand/collapse
- Tool result blocks with syntax highlighting
- Clear and reset functionality

```python
from tau_coding_agent.widgets.chat_display import ChatDisplay
from tau_coding_agent.widgets.chat_display_data import ChatMessageData

display = ChatDisplay()
display.append_message(ChatMessageData(
    role="user",
    content=[{"type": "text", "text": "Hello"}]
))
display.append_message(ChatMessageData(
    role="assistant",
    content=[{"type": "text", "text": "Hi there!"}]
))
```

### Session Tree

Browse and manage sessions:
- List all sessions
- Fork/clone sessions
- Delete sessions
- Switch between sessions

### Input Bar

Command-line style input:
- Arrow key history navigation
- Tab completion
- Custom command support

## Writing a TUI Extension

```python
# my_tui_extension.py
def my_tui_extension(api):
    """Register extension handlers for the TUI."""

    # Listen to agent events
    def on_tool_call(event):
        print(f"Tool called: {event.tool_name}")

    api.on("tool_execution_start", on_tool_call)

    # Register a custom tool
    api.register_tool({
        "name": "my_tool",
        "label": "My Tool",
        "description": "A custom tool",
        "parameters": {"type": "object", "properties": {}},
    })
```

## CLI Options

```bash
tau --model gpt-4o --tools read,write,bash --cwd /path/to/project
tau --rpc --port 8080          # Start RPC server
tau --headless --prompt "Hello"  # Run single prompt and exit
```

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `↑/↓` | Navigate session tree |
| `Enter` | Confirm selection |
| `Ctrl+C` | Abort current prompt |
| `/` | Focus input bar |
| `q` | Quit |
| `n` | New session |
| `f` | Fork session |
| `c` | Clone session |
| `d` | Delete session |

## Error Handling

- Provider errors are displayed in the chat
- Tool errors show with an error indicator
- Extension errors are logged, UI continues
- Network errors are retried automatically

## License

MIT
