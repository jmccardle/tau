# τ API Reference

## τ-ai Public API

### `tau_ai.types`

```python
from tau_ai.types import (
    # Messages
    UserMessage,
    AssistantMessage,
    ToolResultMessage,
    Message,  # Union type

    # Content blocks
    TextContent,
    ThinkingContent,
    ImageContent,
    ToolCall,

    # Agent tool
    AgentTool,
    AgentToolResult,
    AgentToolUpdateCallback,

    # Model/Provider
    Model,
    Context,

    # Streaming
    AssistantMessageEventStream,
    StopReason,
    TextDeltaEvent,
    ToolCallDeltaEvent,
    DoneEvent,
    ErrorEvent,

    # Tool definitions
    Tool,
    Usage,
)
```

### `tau_ai`

```python
# Stream a response
from tau_ai import stream_simple

stream = await stream_simple(
    model: Model,
    context: dict,  # {"system_prompt": ..., "messages": [...], "tools": [...]}
    options: dict,  # {"api_key": ..., "reasoning": ..., "max_tokens": ...}
)

async for event in stream:
    if event.type == "text_delta":
        print(event.delta, end="")
    elif event.type == "toolcall_delta":
        print(f"\nTool: {event.tool_call.name}")

final = await stream.result()  # AssistantMessage
```

### `tau_ai.tools`

```python
from tau_ai.tools import define_tool, validate_tool_arguments

tool = define_tool({
    "name": "my_tool",
    "label": "My Tool",
    "description": "Does something",
    "parameters": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
    "execute": my_tool_execute,
})
```

## τ-agent-core Public API

### `tau_agent_core`

```python
from tau_agent_core import (
    # Session management
    SessionManager,
    SessionInfo,
    SessionState,

    # Agent session (main API)
    AgentSession,

    # Agent loop
    AgentLoop,
    AgentLoopConfig,
    AgentEvent,
    EventSink,

    # Tools
    create_all_tools,
    create_coding_tools,
    create_read_only_tools,
    AgentTool,

    # Extensions
    ExtensionAPI,
    ExtensionContext,
    ExtensionLoader,
    ExtensionRegistry,

    # SDK
    create_agent_session,

    # Compaction
    compact_session,
    should_compact,
)
```

### `create_agent_session()` — SDK Entry Point

```python
from tau_agent_core import create_agent_session, SessionManager

# Minimal: all defaults
session = create_agent_session()

# With options
session = create_agent_session(
    model="gpt-4o",                    # Model ID or name
    provider="openai",                 # Provider
    base_url="https://api.openai.com/v1",  # Optional override
    api_key="sk-...",                  # Optional (env var preferred)
    tools=["read", "bash", "edit", "write"],  # Tool names
    session_manager=SessionManager.in_memory(),  # For testing
    extensions=[my_extension],          # List of extension factory functions
    system_prompt="You are a helpful assistant.",  # Optional custom prompt
    thinking_level="high",             # off, minimal, low, medium, high, xhigh
    cwd="/path/to/project",            # Working directory
    settings=None,                     # Settings dict
)
```

### `AgentSession` API

```python
# Subscribe to events
def handler(event: AgentEvent):
    if event.type == "text_delta":
        print(event.delta, end="")
    elif event.type == "toolcall_end":
        print(f"\n[Tool: {event.tool_name}]")

unsubscribe = session.subscribe(handler)

# Send a prompt (runs agent loop)
messages = await session.prompt("What files are in this directory?")

# Access current messages
for msg in session.messages:
    print(msg.role, msg.content)

# Abort current turn
session.abort()

# Manual compaction
await session.compact(custom_instructions="Focus on recent changes")
```

### `AgentEvent` Types

```python
# All events have: type: str
# Plus type-specific fields

# agent_start / agent_end
AgentEvent(type="agent_start")
AgentEvent(type="agent_end", messages=[...])

# turn_start / turn_end
AgentEvent(type="turn_start", turn_index=0)
AgentEvent(type="turn_end", turn_index=0, message=..., tool_results=[...])

# message_start / message_update / message_end
AgentEvent(type="message_start", message=...)
AgentEvent(type="message_update", message=..., event=...)
AgentEvent(type="message_end", message=...)

# tool_execution_start / update / end
AgentEvent(type="tool_execution_start", tool_call_id=..., tool_name=..., args=...)
AgentEvent(type="tool_execution_update", tool_call_id=..., tool_name=..., args=..., result=...)
AgentEvent(type="tool_execution_end", tool_call_id=..., tool_name=..., result=..., is_error=...)
```

### `SessionManager` API

```python
from tau_agent_core import SessionManager

# File-based session manager
mgr = SessionManager(cwd="/path/to/project")

# Create new session
session_path = mgr.new_session(model_id="gpt-4o")

# Load session
state = mgr.load(session_path)

# Append entry
mgr.append_entry({
    "id": "abc12345",
    "type": "message",
    "timestamp": 1718668800000,
    "parent_id": "prev_id",
    "message": {...}
})

# Get active messages
messages = mgr.get_active_messages()

# List sessions
sessions = mgr.list()  # Current cwd
sessions = mgr.list_all()  # All directories

# Fork a session
new_path = mgr.fork(from_entry_id="abc12345")

# Clone a session
clone_path = mgr.clone(from_entry_id="abc12345")
```

## τ-coding-agent CLI

```bash
tau [options] [prompt...]

Options:
  -p, --print              Print response and exit
  --mode json              Output events as JSON lines
  --mode rpc               RPC mode (stdin/stdout)
  --model MODEL            Model ID (e.g., gpt-4o)
  --thinking LEVEL         Thinking level: off|minimal|low|medium|high
  --tools NAMES            Comma-separated tool names
  --no-tools               Disable all tools
  --no-builtin-tools       Disable built-in tools
  --extension PATH         Load extension from path (repeatable)
  --no-extensions          Disable extension discovery
  --continue, -c           Continue most recent session
  --resume, -r             Browse past sessions
  --fork ENTRY             Fork session from entry ID
  --session PATH|ID        Use specific session
  --no-session             Ephemeral mode
  --system-prompt TEXT     Custom system prompt
  --append-system-prompt   Append to system prompt
  --verbose, -v            Verbose output
  -h, --help               Show help
  -v, --version            Show version

Examples:
  tau "What files are in this directory?"
  tau -p "Summarize this codebase"
  cat README.md | tau -p "Review this file"
  tau --model gpt-4o --thinking high "Solve this problem"
  tau -e ./my_extension.py --tools read,bash "Help me with X"
  tau --continue
  tau --fork abc12345
```

## Extension API Quick Reference

```python
from tau_agent_core import ExtensionAPI

def register(pi: ExtensionAPI):
    # Event subscription
    pi.on("agent_start", handler)
    pi.on("tool_call", handler)
    pi.on("tool_result", handler)

    # Tool registration
    pi.register_tool(tool_def)
    pi.set_active_tools(["read", "bash"])

    # Commands
    pi.register_command("mycmd", CommandDef(...))

    # State
    pi.append_entry("my_state", {"key": "value"})
    pi.set_session_name("My Session")

    # Messaging
    pi.send_user_message("message")
    pi.send_message({"custom": True})

    # CLI flags
    pi.register_flag("my-flag", {"type": "boolean", "default": False})

    # UI (TUI-only)
    ctx.ui.confirm("Title", "Message")
    ctx.ui.select("Title", ["A", "B", "C"])
    ctx.ui.input("Title", "default")
    ctx.ui.notify("Notification")
```
