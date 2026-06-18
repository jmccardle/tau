# Subphase 0.0 — Cross-Phase Data Contracts

> **Purpose**: Define data types, interfaces, and protocols that cross-cut multiple phases. No phase can proceed in isolation without these contracts.

## Why This Is Separate

τ is a 3-package monorepo. Each phase touches different packages:
- Phase 1 owns `tau-ai/`
- Phase 2 owns `tau-agent-core/`
- Phase 3 owns `tau-agent-core/extensions/`
- Phase 4 owns `tau-coding-agent/`
- Phase 5-6 touch `tau-agent-core/` and `tau-coding-agent/`

The **inter-package boundaries** must be locked before implementation begins, because:
1. Phase 2 (agent loop) imports types from Phase 1 (provider types)
2. Phase 4 (TUI) imports types from Phase 2 (agent events)
3. Phase 3 (extensions) needs types from Phase 2 (tool definitions)

If Phase 1 changes the message type mid-stream, Phase 2 breaks. If Phase 2 changes the event format, Phase 4 breaks. These contracts are the **immutable interface** between phases.

## Contract Document Index

| Contract | Defined In | Consumed By |
|----------|-----------|-------------|
| Message types (`UserMessage`, `AssistantMessage`, `ToolResultMessage`, etc.) | Phase 1, subphase 1.1 | All phases |
| Tool definitions (`ToolDefinition`, `ToolCall`, `validate_tool_arguments`) | Phase 1, subphase 1.1 | Phase 2, Phase 3 |
| `Model` type + provider config | Phase 1, subphase 1.1 | Phase 2, Phase 4 |
| `AbortSignal` | Phase 1, subphase 1.1 | Phase 2, Phase 3, Phase 6 |
| Streaming event protocol (`TextDeltaEvent`, `ToolCallDeltaEvent`, `DoneEvent`, `ErrorEvent`) | Phase 1, subphase 1.3 | Phase 2 |
| `AgentEvent` enum and data fields | Phase 2, subphase 2.1 | Phase 3, Phase 4 |
| `AgentTool` and `AgentToolResult` | Phase 2, subphase 2.1 | Phase 3 |
| Tool batch result (`ToolBatchResult`) | Phase 2, subphase 2.1 | Phase 2 (internal) |
| Session entry JSON schema | Phase 2, subphase 2.2 | Phase 2, Phase 5 |
| `SessionManager` interface | Phase 2, subphase 2.2 | Phase 2, Phase 3, Phase 4, Phase 5, Phase 6 |
| `AgentSession` interface | Phase 2, subphase 2.4 | Phase 4 |
| Extension API surface (`ExtensionAPI`, `ExtensionContext`, `ExtensionUI`) | Phase 3, subphase 3.3 | Phase 4 (UI methods), all extension writers |
| `EventBus` interface | Phase 3, subphase 3.1 | Phase 2 (agent loop), Phase 4 |
| CLI argument contract | Phase 4, subphase 4.3 | Phase 6 (RPC uses same argument parsing) |
| RPC message format | Phase 6, subphase 6.1 | Phase 6 (self-contained) |

## Core Data Type Contracts

These are the types that must be defined early and referenced across phases. The actual code is written in Phase 1 subphase 1.1, but the contracts are defined here.

### 1. Messages (tau-ai)

```python
# Every message has:
#   role: str ("user" | "assistant" | "toolResult")
#   content: list[ContentBlock]
#   timestamp: int (ms since epoch, optional for toolResult)
#
# ContentBlock is a discriminated union:
#   - TextContent(type="text", text: str)
#   - ThinkingContent(type="thinking", text: str, cached_tokens: int)
#   - ImageContent(type="image", data: str, mime: str)
#   - ToolCall(type="toolCall", id: str, name: str, arguments: Any)
```

**Reference**: `MONOREPO-STRUCTURE.md`, lines 26-32; `docs/tau-ai.md`, lines 1-40; `docs/tau-agent-core.md`, lines 1-60.

**Rationale**: This mirrors pi's message types (see `@mariozechner/pi-agent-core/dist/types.d.ts`), but uses a flat content block list instead of a single text field. This makes tool calls and thinking blocks first-class citizens.

**Constraint**: Messages are **immutable** once created. All mutation happens via `AssistantMessageEventStream` which produces partial messages that are combined into a final `AssistantMessage`.

### 2. Tool Definitions (tau-ai + tau-agent-core)

```python
# Phase 1 (tau-ai) defines the raw tool definition:
ToolDefinition = {
    "name": str,           # unique, snake_case
    "label": str,          # human-readable, for TUI
    "description": str,    # sent to LLM
    "parameters": dict,    # JSON Schema (pydantic.model_json_schema())
    "execute": Callable,   # async function
    "prompt_snippet": str | None,   # one-line summary for system prompt
    "prompt_guidelines": list[str] | None,  # guidelines for LLM
    "execution_mode": Literal["sequential", "parallel"]  # default "parallel"
}

# Phase 2 (tau-agent-core) wraps it:
AgentTool = {
    "definition": ToolDefinition,
    "name": str,           # alias: definition.name
    "execute": Callable,   # alias: definition.execute
}

# Tool call parameters are validated:
def validate_tool_arguments(tool: AgentTool, tool_call: ToolCall) -> dict:
    """Returns validated parameter dict (pydantic.model_validate result).
    Raises ValueError if arguments don't match schema.
    """
```

**Reference**: `docs/tau-agent-core.md`, lines 61-120; `docs/extensions.md`, lines 1-60.

**Rationale**: pi uses `AgentTool` directly (no intermediate `ToolDefinition`). We separate them because extensions register with `ToolDefinition` (raw, unvalidated), but the agent loop works with `AgentTool` (validated, wrapped).

**Constraint**: Tool names must be **globally unique** across all sources (built-in + extensions). The extension registry enforces this at registration time.

### 3. AbortSignal (tau-ai)

```python
class AbortSignal:
    """Signal that can be checked during async operations.

    When abort() is called, all subsequent is_aborted() checks return True.
    Async operations should check this periodically (e.g., every 100ms)
    and raise asyncio.CancelledError if aborted.
    """
    def is_aborted(self) -> bool: ...
    def abort(self) -> None: ...
```

**Reference**: `docs/tau-ai.md`, lines 140-180; `docs/tau-agent-core.md`, lines 120-160.

**Rationale**: pi uses AbortController (from Web API). Python has no equivalent, so we build a minimal one. This is needed for:
- Tool execution (long-running bash commands)
- LLM streaming (cancel mid-stream)
- RPC mode (client disconnect)

**Constraint**: `abort()` is idempotent. `is_aborted()` is thread-safe (use `threading.Lock` internally).

### 4. Streaming Events (tau-ai)

```python
# All streaming events are produced by stream_simple():
class StreamEvent(Protocol):
    type: str  # "text_delta" | "toolcall_delta" | "done" | "error"

class TextDeltaEvent(StreamEvent):
    type: Literal["text_delta"]
    delta: str
    partial: AssistantMessage  # accumulating message

class ToolCallDeltaEvent(StreamEvent):
    type: Literal["toolcall_delta"]
    delta: dict  # OpenAI-style tool call delta
    partial: AssistantMessage

class DoneEvent(StreamEvent):
    type: Literal["done"]
    final: AssistantMessage  # fully accumulated message
    usage: Usage  # {input_tokens, output_tokens, total_tokens}

class ErrorEvent(StreamEvent):
    type: Literal["error"]
    message: str
    is_error: Literal[True]
```

**Reference**: `docs/tau-ai.md`, lines 80-140; `docs/tau-agent-core.md`, lines 160-220.

**Rationale**: pi's `streamChat()` returns a generator of `AssistantMessageEvent`. We replicate this with typed events instead of a single union type, so consumers can pattern-match on event type without inspecting the message.

**Constraint**: Events are **ordered** and **non-retriable**. If the network drops, the consumer must restart the stream.

### 5. Agent Events (tau-agent-core)

```python
# All agent events are emitted by AgentLoop.run():
class AgentEvent:
    type: Literal[
        "agent_start", "agent_end",
        "turn_start", "turn_end",
        "message_start", "message_update", "message_end",
        "tool_execution_start", "tool_execution_update", "tool_execution_end",
    ]
    # Common fields:
    timestamp: int  # ms since epoch
    # Conditional fields by type:
    message: Message | None      # agent_start/end, message_*
    turn_index: int | None       # turn_*
    tool_call_id: str | None     # tool_*
    tool_name: str | None        # tool_*
    args: dict | None            # tool_execution_start
    result: Any | None           # tool_execution_*
    is_error: bool = False       # all types
    tool_results: list[ToolResultMessage] | None  # turn_end
    messages: list[Message] | None  # agent_end
```

**Reference**: `docs/tau-agent-core.md`, lines 220-300; `docs/tau-coding-agent.md`, lines 100-160.

**Rationale**: This is the **central event bus** of τ. Both the TUI (Phase 4) and extensions (Phase 3) listen to these events. The agent loop is the producer; everything else is a consumer.

**Constraint**: Events are **fire-and-forget**. The event bus does not buffer or queue. If a handler is slow, it does not delay the agent loop. Handlers are called synchronously for performance; async handlers are awaited but do not block other handlers.

### 6. Session Entry JSON Schema (tau-agent-core)

```json
{
  "$schema": "http://json-schema.org/draft/2020-12/schema#",
  "description": "A single entry in a session JSONL file",
  "oneOf": [
    {
      "title": "SessionEntry",
      "properties": {
        "id": { "type": "string" },
        "type": { "const": "session" },
        "timestamp": { "type": "integer" },
        "parent_id": { "type": "string" },
        "model": { "type": "string" },
        "model_name": { "type": "string" },
        "cwd": { "type": "string" },
        "system_prompt": { "type": "string" },
        "session_name": { "type": "string" }
      },
      "required": ["id", "type", "timestamp"]
    },
    {
      "title": "MessageEntry",
      "properties": {
        "id": { "type": "string" },
        "type": { "const": "message" },
        "timestamp": { "type": "integer" },
        "parent_id": { "type": "string" },
        "message": { "$ref": "#/definitions/Message" }
      },
      "required": ["id", "type", "timestamp", "message"]
    },
    {
      "title": "ToolResultEntry",
      "properties": {
        "id": { "type": "string" },
        "type": { "const": "toolResult" },
        "timestamp": { "type": "integer" },
        "parent_id": { "type": "string" },
        "tool_call_id": { "type": "string" },
        "tool_name": { "type": "string" },
        "content": { "type": "array" },
        "is_error": { "type": "boolean" }
      },
      "required": ["id", "type", "timestamp", "tool_call_id", "tool_name", "content"]
    },
    {
      "title": "CustomMessageEntry",
      "properties": {
        "id": { "type": "string" },
        "type": { "const": "customMessage" },
        "timestamp": { "type": "integer" },
        "parent_id": { "type": "string" },
        "custom_type": { "type": "string" },
        "message": { "type": "object" }
      },
      "required": ["id", "type", "timestamp", "custom_type", "message"]
    },
    {
      "title": "CompactionEntry",
      "properties": {
        "id": { "type": "string" },
        "type": { "const": "compaction" },
        "timestamp": { "type": "integer" },
        "parent_id": { "type": "string" },
        "first_kept_id": { "type": "string" },
        "summary": { "type": "string" },
        "tokens_saved": { "type": "integer" },
        "compacted_entries": { "type": "array", "items": { "type": "string" } }
      },
      "required": ["id", "type", "timestamp", "first_kept_id", "summary"]
    }
  ]
}
```

**Reference**: `docs/tau-agent-core.md`, lines 300-400; `docs/IMPLEMENTATION-PLAN.md`, lines 100-180; pi's `session-manager.js` and `agent-session.js`.

**Rationale**: This is a **forward-compatible** JSON schema. New entry types can be added without breaking old readers (they'll just skip unknown types). Each entry has an `id` (UUID), `parent_id` (tree structure), `timestamp` (ms), and `type` (discriminated union).

**Constraint**: The JSONL format is **append-only**. No in-place edits. Sessions are rebuilt by replaying entries. This matches pi's approach and ensures crash safety.

### 7. AgentSession Interface (tau-agent-core → tau-coding-agent boundary)

```python
class AgentSession:
    """Public API for agent sessions.

    This is the ONLY interface that τ-coding-agent uses to interact
    with τ-agent-core. Everything in τ-agent-core is accessible
    through this object or through subscription.
    """
    # Properties
    messages: list[Message]              # Current active path
    state: SessionState                  # Read-only session state
    is_streaming: bool                   # True during agent loop

    # Methods
    def subscribe(self, handler: Callable[[AgentEvent], Any]) -> Callable[[], None]:
        """Subscribe to agent events. Returns unsubscribe function."""

    async def prompt(self, text: str, images: list[dict] | None = None) -> list[Message]:
        """Send a prompt and run the agent loop. Returns messages produced."""

    async def continue_conversation(self) -> list[Message]:
        """Run another agent turn without adding a new prompt."""

    async def compact(self, custom_instructions: str | None = None):
        """Trigger manual compaction."""

    def abort(self) -> None:
        """Abort the current agent turn."""
```

**Reference**: `docs/tau-agent-core.md`, lines 400-500; `docs/tau-coding-agent.md`, lines 160-220.

**Rationale**: This is the **public API surface** of τ-agent-core. It wraps the agent loop, session manager, and extension system into a single object. The TUI only interacts with this object — it never calls `AgentLoop` directly, never calls `SessionManager` directly, never calls the extension loader directly.

**Constraint**: `AgentSession` is the **boundary** between τ-agent-core and τ-coding-agent. τ-coding-agent must not import anything from τ-agent-core other than `AgentSession` and `SessionManager`. All other internal details are private.

### 8. Extension API Surface (tau-agent-core → extension boundary)

```python
class ExtensionAPI:
    """Public API exposed to extension modules."""
    def on(self, event: str, handler: Callable) -> None: ...
    def register_tool(self, definition: dict) -> None: ...
    def get_all_tools(self) -> list[ToolInfo]: ...
    def set_active_tools(self, names: list[str]) -> None: ...
    def register_command(self, name: str, command: dict) -> None: ...
    def append_entry(self, custom_type: str, data: dict) -> None: ...
    def set_session_name(self, name: str) -> None: ...
    def send_user_message(self, content: str, deliver_as: str = "steer") -> None: ...
    def send_message(self, message: dict, options: dict) -> None: ...
    def register_flag(self, name: str, options: dict) -> None: ...
    def get_flag(self, name: str) -> Any: ...

    @property
    def ui(self) -> ExtensionUI: ...

class ExtensionContext:
    """Context passed to extension event handlers and tools."""
    @property
    def cwd(self) -> str: ...
    @property
    def session_manager(self) -> SessionManager: ...
    @property
    def signal(self) -> AbortSignal | None: ...
    @property
    def is_idle(self) -> bool: ...
    def abort(self) -> None: ...
    def shutdown(self) -> None: ...
    def get_context_usage(self) -> dict: ...

class ExtensionUI:
    """User interaction methods (TUI only)."""
    async def confirm(self, title: str, message: str) -> bool: ...
    async def select(self, title: str, items: list[str]) -> str | None: ...
    async def input(self, title: str, default: str = "") -> str: ...
    def notify(self, message: str, level: str = "info") -> None: ...
```

**Reference**: `docs/tau-agent-core.md`, lines 500-600; `docs/extensions.md`, lines 60-180.

**Rationale**: This is the **only API** extension modules use. Extensions are loaded as plain Python modules and receive an `ExtensionAPI` instance (plus an `ExtensionContext` for event handlers). They must not import τ-agent-core internals.

**Constraint**: The `ui` property is a **no-op** in headless mode (RPC, SDK). The TUI implements the real UI methods. This means the same extension can run in any context.

## Package Boundary Enforcement

### τ-ai → τ-agent-core boundary

τ-agent-core imports:
- `tau_ai.types.*` — all message, tool, model types
- `tau_ai.tools.*` — `define_tool()`, `validate_tool_arguments()`
- `tau_ai.client.*` — `stream_simple()`

τ-agent-core does NOT import:
- `tau_ai.providers.*` — the agent loop doesn't care about providers
- `tau_ai.streaming.*` — only uses `stream_simple()` return value

### τ-agent-core → τ-coding-agent boundary

τ-coding-agent imports:
- `tau_agent_core.AgentSession`
- `tau_agent_core.SessionManager`
- `tau_agent_core.AgentEvent`

τ-coding-agent does NOT import:
- `tau_agent_core.agent_loop.*` — internal implementation detail
- `tau_agent_core.tools.*` — tools are registered via AgentSession
- `tau_agent_core.compaction.*` — compaction is triggered via AgentSession
- `tau_agent_core.extensions.*` — extensions are loaded via AgentSession

### Extension → τ-agent-core boundary

Extensions import:
- `tau_agent_core.ExtensionAPI`
- `tau_agent_core.ExtensionContext`
- `tau_agent_core.define_tool`

Extensions do NOT import:
- `tau_agent_core.agent_loop`
- `tau_agent_core.session_manager`
- `tau_ai.*`

## Data Flow Summary

```
User → τ-coding-agent (TUI)
       │
       ├─ User types prompt
       │     │
       │     └─ AgentSession.prompt(text)
       │           │
       │           ├─ AgentLoop.run()  ◄── Phase 2
       │           │     │
       │           │     ├─ τ-ai.stream_simple()  ◄── Phase 1
       │           │     │
       │           │     └─ EventBus.emit()  ◄── Phase 3
       │           │           │
       │           │           └─ TUI widgets + extensions
       │           │
       │           └─ SessionManager.append_entry()  ◄── Phase 2/5
       │
       └─ AgentSession.subscribe(handler)
             │
             └─ EventBus → TUI widgets  ◄── Phase 4
```

The key insight: **τ-coding-agent never calls τ-ai directly**. All LLM interaction flows through `AgentSession` → `AgentLoop` → `stream_simple()`. This keeps the TUI decoupled from provider implementation details.
