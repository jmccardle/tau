# τ (tau-agent-core) Architecture

## Philosophy

τ is a **programmable agent harness**. The agent loop, tools, sessions, and extension system are the core library. The TUI (`tau-coding-agent`) is one consumer of this library. τ can also be used headlessly (SDK), via RPC, or embedded in other applications.

## Core Data Flow

```
User → τ-coding-agent (TUI)
          │
          ├─ prompt text → τ-agent-core.AgentSession.prompt()
          │                     │
          │                     ├─ before_agent_start → extensions
          │                     │
          │                     ├─ build_system_prompt()
          │                     │
          │                     ├─ agent_loop.run()
          │                     │     │
          │                     │     ├─ stream assistant response (tau_ai)
          │                     │     ├─ parse tool calls
          │                     │     ├─ tool_call events → extensions (blocking)
          │                     │     ├─ execute tools (built-in or extension)
          │                     │     │     ├─ beforeToolCall hooks
          │                     │     ├─ tool_result events → extensions (modifying)
          │                     │     │     └─ afterToolCall hooks
          │                     │     └─ loop until no more tool calls
          │                     │
          │                     ├─ agent_end → extensions
          │                     │
          │                     └─ append to session (SessionManager)
          │
          └─ emit events → TUI widgets update UI
```

## The Agent Loop (τ-agent-core/agent_loop.py)

The agent loop is the central execution engine. It mirrors pi's agent-loop.js architecture:

```python
class AgentLoop:
    """Core loop: prompt → LLM → tools → repeat."""

    async def run(
        self,
        prompts: list[AgentMessage],
        context: AgentContext,
        config: AgentLoopConfig,
        emit: EventSink,
        signal: AbortSignal | None = None,
    ) -> list[AgentMessage]:
        """Run the full agent loop for one or more prompts."""

        # 1. Emit agent_start, turn_start
        # 2. For each prompt: emit message_start/message_end
        # 3. Stream assistant response from LLM
        # 4. If assistant has tool calls:
        #    a. Emit tool_execution_start for each
        #    b. beforeToolCall hooks (can block)
        #    c. Execute tools (sequential or parallel)
        #    d. Emit tool_execution_update (streaming)
        #    e. afterToolCall hooks (can modify result)
        #    f. Emit tool_execution_end
        #    g. Emit tool result message (message_start/message_end)
        #    h. Add results to context
        # 5. Emit turn_end
        # 6. Check for steering messages (queued while agent worked)
        # 7. If steering: go to step 3
        # 8. Check for follow-up messages (queued for after agent finishes)
        # 9. If follow-up: go to step 3
        # 10. Emit agent_end

    async def run_continue(
        self,
        context: AgentContext,
        config: AgentLoopConfig,
        emit: EventSink,
        signal: AbortSignal | None = None,
    ) -> list[AgentMessage]:
        """Continue the loop without adding new messages (for retries)."""
```

### Tool Execution Modes

- **Sequential** (default): tools execute one at a time, in assistant source order
- **Parallel**: tools are prepared sequentially, then executed concurrently; results
  collected in assistant source order

### Tool Call Lifecycle

```
LLM responds with tool calls
  │
  ▼
tool_execution_start ──► (emit)
  │
  ▼
beforeToolCall hook ──► can return { block: True, reason: "..." }
  │                     (if blocked: emit error result, skip execution)
  ▼
validate tool arguments (pydantic)
  │
  ▼
tool.execute(tool_call_id, params, signal, on_update)
  │
  │  ┌── on_update() called during execution ──► tool_execution_update
  │  │
  ▼  ▼
tool result ──► afterToolCall hook ──► can modify content/details/terminate
  │
  ▼
tool_execution_end ──► (emit)
  │
  ▼
message_start (tool result)
  │
  ▼
message_end (tool result)
  │
  ▼
result appended to context → next LLM call
```

### Events Emitted by AgentLoop

| Event | When | Payload |
|-------|------|---------|
| `agent_start` | Loop begins | `{}` |
| `agent_end` | Loop finishes | `{messages: [...]}` |
| `turn_start` | Each LLM call | `{turn_index: int}` |
| `turn_end` | Each LLM call finishes | `{message, tool_results}` |
| `message_start` | New message enters context | `{message}` |
| `message_update` | Streaming text/tool deltas | `{message, event}` |
| `message_end` | Message fully received | `{message}` |
| `tool_execution_start` | Tool about to execute | `{tool_call_id, tool_name, args}` |
| `tool_execution_update` | Partial result during execution | `{tool_call_id, tool_name, args, partial_result}` |
| `tool_execution_end` | Tool finished | `{tool_call_id, tool_name, result, is_error}` |

## Session Manager (τ-agent-core/session_manager.py)

τ's sessions are JSONL files with a tree structure. This mirrors pi's approach:

```python
@dataclass
class SessionEntry:
    """Base entry in a session JSONL file."""
    id: str                          # 8-char short ID
    type: str                        # "session", "message", "tool_result", "custom_message",
                                     # "compaction", "branch_summary", "thinking_level_change",
                                     # "model_change"
    timestamp: int                   # epoch ms
    parent_id: str | None            # parent entry ID (for tree traversal)

@dataclass
class MessageEntry(SessionEntry):
    type = "message"
    message: Message                 # UserMessage, AssistantMessage, ToolResultMessage

@dataclass
class CustomMessageEntry(SessionEntry):
    type = "custom_message"
    custom_type: str                 # Extension-defined type
    content: str
    display: bool
    details: dict | None

@dataclass
class CompactionEntry(SessionEntry):
    type = "compaction"
    summary: str                     # Summary text
    tokens_before: int
    first_kept_entry_id: str         # First message kept after compaction

@dataclass
class BranchSummaryEntry(SessionEntry):
    type = "branch_summary"
    summary: str
    from_id: str                     # Entry ID where branch diverged
```

### Session File Format

```jsonl
{"version": 1, "type": "session", "id": "s001", "timestamp": 1718668800000, "cwd": "/home/user/project", "model_id": "gpt-4o", "thinking_level": "off"}
{"id": "s002", "type": "message", "timestamp": 1718668801000, "parent_id": "s001", "message": {"role": "user", "content": "List files", "timestamp": 1718668801000}}
{"id": "s003", "type": "message", "timestamp": 1718668802000, "parent_id": "s002", "message": {"role": "assistant", "content": [...], "timestamp": 1718668802000}}
{"id": "s004", "type": "tool_result", "timestamp": 1718668802500, "parent_id": "s003", "message": {...}}
...
```

### Key Operations

- `load(path)` → `SessionState` (all entries parsed)
- `save(state)` → writes JSONL
- `get_active_path(leaf_id)` → list of entries from root to leaf
- `append_entry(entry)` → append to file
- `list(cwd)` → list sessions for a working directory
- `list_all()` → list all sessions across directories
- `fork(from_session, entry_id)` → copy path up to entry_id into new file
- `migrate(entries)` → handle version migrations (v1→v2→v3)

## Extension System (τ-agent-core/extensions/)

This is τ's key differentiator. Python extensions loaded at runtime.

```python
# Example extension (user writes):
# ~/.tau/extensions/my_tool.py

from tau_agent_core import ExtensionAPI, define_tool
from pydantic import BaseModel

class GreetParams(BaseModel):
    name: str

greet_tool = define_tool({
    "name": "greet",
    "label": "Greet",
    "description": "Greet someone by name",
    "parameters": GreetParams.model_json_schema(),
    "execute": async def(tool_call_id, params, signal, on_update, ctx):
        return {
            "content": [{"type": "text", "text": f"Hello, {params.name}!"}],
            "details": {"greeted": params.name},
        }
})

def register(pi: ExtensionAPI):
    pi.register_tool(greet_tool)
    pi.on("tool_call", async def(event, ctx):
        if event.tool_name == "bash" and "rm -rf" in event.input.command:
            ok = await ctx.ui.confirm("Dangerous", "Allow rm -rf?")
            if not ok:
                return {"block": True, "reason": "Blocked by user"}
```

### Extension Discovery Paths

| Path | Scope |
|------|-------|
| `~/.tau/extensions/*.py` | Global |
| `~/.tau/extensions/*/__init__.py` | Global (directory) |
| `<cwd>/.tau/extensions/*.py` | Project-local |
| `<cwd>/.tau/extensions/*/__init__.py` | Project-local (directory) |

### Extension API Surface

```python
class ExtensionAPI:
    """Methods available to extensions."""

    # Event subscription
    def on(self, event: str, handler: Callable[[Event, ExtensionContext], ...]) -> None: ...

    # Tool registration
    def register_tool(self, definition: ToolDefinition) -> None: ...
    def get_all_tools(self) -> list[ToolInfo]: ...
    def set_active_tools(self, names: list[str]) -> None: ...

    # Command registration
    def register_command(self, name: str, command: CommandDefinition) -> None: ...

    # Session state
    def append_entry(self, custom_type: str, data: dict) -> None: ...
    def set_session_name(self, name: str) -> None: ...

    # Messaging
    def send_user_message(self, content: str, deliver_as: str = "steer") -> None: ...
    def send_message(self, message: dict, options: dict) -> None: ...

    # UI interaction (TUI-only)
    # ctx.ui.confirm(), .select(), .input(), .notify()

    # Flags
    def register_flag(self, name: str, options: dict) -> None: ...
    def get_flag(self, name: str) -> Any: ...
```

## Message Types

τ uses the same core message types as pi, adapted for Python:

```python
@dataclass
class UserMessage:
    role: str = "user"
    content: str | list[dict]   # String or [{"type": "text", "text": "..."}]
    timestamp: int

@dataclass
class AssistantMessage:
    role: str = "assistant"
    content: list[dict]         # [{"type": "text", "text": "..."},
                                 #        {"type": "thinking", "thinking": "..."},
                                 #        {"type": "toolCall", "id": "...",
                                 #         "name": "...", "arguments": {...}}]
    api: str                    # e.g., "openai-completions"
    provider: str               # e.g., "openai"
    model: str
    response_id: str | None
    usage: Usage
    stop_reason: str            # "stop" | "length" | "toolUse" | "error" | "aborted"
    error_message: str | None
    timestamp: int

@dataclass
class ToolResultMessage:
    role: str = "toolResult"
    tool_call_id: str
    tool_name: str
    content: list[dict]         # [{"type": "text", "text": "..."}]
    details: dict | None
    is_error: bool
    timestamp: int
```

## Context Building for LLM Calls

Before each LLM call, τ-agent-core:

1. Walks the session tree from current leaf to root
2. Applies compaction (summarizes older messages, keeps recent)
3. Collects `thinking_level_change` and `model_change` entries for current settings
4. Passes through any `transformContext` callbacks (extensions can prune/modify messages)
5. Converts `AgentMessage[]` → `Message[]` for the provider
6. Calls `convertToLlm(messages)` → `list[dict]` for OpenAI API

```python
def build_context_for_llm(session_entries, leaf_id, config):
    # Walk tree, apply compaction, collect messages
    # Apply transformContext hooks
    # Convert to OpenAI format
    return llm_messages, system_prompt, tools, thinking_level
```

## Compaction (τ-agent-core/compaction.py)

When context approaches the model's window:

```python
def should_compact(messages, model_context_window, margin=0.8) -> bool:
    """Check if context is approaching model limits."""

def prepare_compaction(entries, first_kept, custom_instructions=None) -> dict:
    """Prepare compaction: select messages to summarize, identify kept entries."""

async def compact(session, config, summary_callback=None) -> CompactionResult:
    """Run compaction: summarize older messages, write compaction entry."""
```

Compaction is lossy — full history remains in the JSONL file. The compaction entry contains the summary and `first_kept_entry_id` pointing to the first non-summarized message.
