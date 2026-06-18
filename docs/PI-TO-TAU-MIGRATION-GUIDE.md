# Migration Guide: Parley → τ (tau-agent-core)

> A step-by-step guide for migrating existing Parley-based code to τ.

## What Is τ?

τ (tau-agent-core) is the Python rewrite of the Parley AI agent system. It provides the same core functionality — conversational AI with tool execution, session management, and extension support — but in Python using the [Textual](https://textual.textualize.io/) TUI framework.

This guide helps you migrate from Parley (TypeScript/Node.js) to τ (Python).

---

## Table of Contents

1. [Quick Summary: What Changes, What Stays the Same](#1-quick-summary-what-changes-what-stays-the-same)
2. [Package Structure Comparison](#2-package-structure-comparison)
3. [Migration Steps](#3-migration-steps)
   - Step 1: Create a τ Session
   - Step 2: Migrate Tool Definitions
   - Step 3: Migrate Extensions
   - Step 4: Handle Sessions
   - Step 5: Event Subscriptions
   - Step 6: Error Handling
4. [Code Examples](#4-code-examples)
5. [FAQ](#5-faq)

---

## 1. Quick Summary: What Changes, What Stays the Same

### What Changes

| Area | Parley (TypeScript) | τ (Python) |
|------|---------------------|------------|
| Language | TypeScript / Node.js | Python 3.11+ |
| TUI | Built-in TUI | Textual-based TUI |
| AI Provider | 14+ providers (OpenAI, Anthropic, Google, etc.) | OpenAI only (MVP) |
| Streaming | `streamChat()` generator | `stream_simple()` async generator |
| Provider Registry | `ProviderRegistry` | `tau_ai.providers.registry.ProviderRegistry` |
| Model Types | `Model` interface | `tau_ai.types.Model` (pydantic) |
| Abort | AbortController | `AbortSignal` |
| Session Format | JSONL files (compatible!) | JSONL files (same format) |
| Extension API | `ExtensionAPI` (TypeScript) | `tau_agent_core.ExtensionAPI` (Python) |

### What Stays the Same

| Concept | Details |
|---------|---------|
| Session JSONL format | **Compatible** — τ reads and writes the same JSONL format as Parley |
| Message structure | `role` + `content[]` (TextContent, ToolCall, ImageContent, etc.) |
| Agent event types | `agent_start`, `agent_end`, `turn_start`, `turn_end`, `message_*`, `tool_execution_*` |
| Tool definition shape | `name`, `label`, `description`, `parameters`, `execute`, `prompt_snippet` |
| Extension lifecycle | `extend(api)` → register tools, subscribe to events |
| Session operations | `new()`, `fork()`, `clone()`, `list()`, `compact()` |
| Package boundaries | τ-ai (providers) → τ-agent-core (agent loop) → τ-coding-agent (TUI) |
| Abort semantics | `abort()` is idempotent, `is_aborted()` is thread-safe |

---

## 2. Package Structure Comparison

### Parley Package Layout

```
parley/
├── pi-ai/             # AI providers
├── pi-agent-core/     # Agent loop, tools, sessions
├── pi-coding-agent/   # TUI
├── pi-tui/            # UI components
└── pi/                # CLI entry point
```

### τ Package Layout

```
tau-agent-core/        # Root of τ monorepo
├── tau-ai/            # AI providers (replaces pi-ai)
│   └── src/tau_ai/
│       ├── types.py       # Model, Message, Tool types
│       ├── client.py      # stream_simple()
│       ├── streaming.py   # TextDeltaEvent, DoneEvent, ErrorEvent
│       ├── abort.py       # AbortSignal
│       ├── tools.py       # define_tool(), validate_tool_arguments()
│       └── providers/     # OpenAI, OpenAI Responses
├── tau-agent-core/    # Agent orchestration (replaces pi-agent-core)
│   └── src/tau_agent_core/
│       ├── sdk.py           # create_agent_session() — main entry point
│       ├── agent_session.py # AgentSession — public API
│       ├── agent_loop.py    # AgentLoop — conversation loop
│       ├── session_manager.py # SessionManager — JSONL persistence
│       ├── events.py        # EventBus, AgentEvent
│       ├── extension_types.py # ExtensionAPI, ExtensionContext
│       ├── tools/           # read, write, edit, bash, ls, grep, find
│       ├── extensions/      # Extension loader
│       ├── rpc.py           # RPC server/client
│       ├── compaction.py    # Context compaction
│       └── system_prompt.py # System prompt builder
└── tau-coding-agent/  # TUI (replaces pi-coding-agent)
    └── src/tau_coding_agent/
        ├── app.py           # Main Textual app
        ├── cli.py           # CLI arguments
        ├── config.py        # Configuration
        └── widgets/         # Chat display, tool widgets, session tree
```

---

## 3. Migration Steps

### Step 1: Create a τ Session

#### Parley (TypeScript)

```typescript
import { createAgentLoop, createSessionManager } from "pi-agent-core";
import { createAllTools } from "pi-ai";

const sessionManager = createSessionManager({ cwd: process.cwd() });
const tools = createAllTools({ cwd: process.cwd() });
const agentLoop = createAgentLoop({
  model: "gpt-4o",
  tools,
  sessionManager,
});

// Run the agent
await agentLoop.prompt("Write a hello world function");
```

#### τ (Python)

```python
from tau_agent_core.sdk import create_agent_session

# One-liner: create_agent_session handles model resolution,
# tool discovery, and system prompt building
session = create_agent_session(
    model="gpt-4o",
    tools=["read", "write", "bash"],
)

# Run the agent
messages = await session.prompt("Write a hello world function")
```

### Step 2: Migrate Tool Definitions

#### Parley (TypeScript)

```typescript
import { createTool } from "pi-agent-core";

const myTool = createTool({
  name: "my_tool",
  label: "My Tool",
  description: "A custom tool",
  parameters: {
    type: "object",
    properties: {
      name: { type: "string", description: "Name to greet" },
    },
    required: ["name"],
  },
  execute: async (args) => {
    return `Hello, ${args.name}!`;
  },
  promptSnippet: "my_tool: A custom tool",
  promptGuidelines: ["Use this tool to greet someone."],
  executionMode: "sequential",
});
```

#### τ (Python)

```python
from tau_agent_core.tools.base import ToolDefinition, AgentTool

def my_tool_execute(args):
    return f"Hello, {args['name']}!"

my_tool = AgentTool(
    definition=ToolDefinition(
        name="my_tool",
        label="My Tool",
        description="A custom tool",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name to greet"}
            },
            "required": ["name"],
        },
        execute=my_tool_execute,
        prompt_snippet="my_tool: A custom tool",
        prompt_guidelines=["Use this tool to greet someone."],
        execution_mode="sequential",
    )
)
```

### Step 3: Migrate Extensions

#### Parley (TypeScript)

```typescript
// my_extension.ts
export function extend(api) {
  api.on("tool_call", (event) => {
    console.log(`Tool called: ${event.toolName}`);
  });

  api.registerCommand({
    name: "/mycommand",
    description: "My command",
    execute: async (session, args) => {
      console.log("My command executed");
    },
  });
}
```

#### τ (Python)

```python
# my_extension.py
def my_extension(api):
    api.on("tool_execution_start", lambda event:
        print(f"Tool called: {event.tool_name}"))

    # For commands, register via the API
    api.register_command("/mycommand", {
        "description": "My command",
    })
```

Usage in session:

```python
session = create_agent_session(
    model="gpt-4o",
    extensions=[my_extension],
)
```

### Step 4: Handle Sessions

#### Parley (TypeScript)

```typescript
const sm = createSessionManager({ cwd: "." });
const sessionPath = sm.newSession();
const entries = sm.getEntries(sessionPath);
const forked = sm.fork(sessionPath);
```

#### τ (Python)

```python
from tau_agent_core.session_manager import SessionManager

sm = SessionManager(cwd=".")
session_path = sm.new_session()
entries = sm.get_active_messages()  # Active path messages
forked = sm.fork(session_path)

# List all sessions
for info in sm.list():
    print(info.session_path, info.message_count)
```

### Step 5: Event Subscriptions

#### Parley (TypeScript)

```typescript
agentLoop.on("messageUpdate", (event) => {
  console.log(`Message: ${event.delta}`);
});

agentLoop.on("toolCall", (event) => {
  console.log(`Tool: ${event.toolName}`);
});
```

#### τ (Python)

```python
session = create_agent_session(model="gpt-4o")

def on_event(event):
    print(f"Event: {event.type}, tool={event.tool_name}, error={event.is_error}")

unsub = session.subscribe(on_event)

# Use the session...
await session.prompt("Hello")

# Unsubscribe when done
unsub()
```

### Step 6: Error Handling

#### Parley (TypeScript)

```typescript
try {
  await agentLoop.prompt("Write code");
} catch (error) {
  console.error(`Agent error: ${error.message}`);
}
```

#### τ (Python)

```python
from tau_ai.streaming import ErrorEvent

# In τ, errors are handled as events:
# - Provider errors → ErrorEvent → shown in chat
# - Tool errors → ToolResultMessage with is_error=True
# - Extension errors → logged, agent loop continues

# You can still catch exceptions at the top level:
try:
    messages = await session.prompt("Write code")
except Exception as e:
    print(f"Agent error: {e}")

# Or listen for error events:
def on_error(event):
    if event.is_error:
        print(f"Error event: {event.message}")

session.subscribe(on_error)
```

---

## 4. Code Examples

### Complete Migration Example

#### Before: Parley

```typescript
import { createAgentLoop, createSessionManager } from "pi-agent-core";
import { createAllTools } from "pi-ai";

async function main() {
  const sm = createSessionManager({ cwd: process.cwd() });
  const tools = createAllTools({ cwd: process.cwd() });

  const loop = createAgentLoop({
    model: "gpt-4o",
    tools,
    sessionManager: sm,
    systemPrompt: "You are a helpful assistant.",
  });

  loop.on("agentStart", () => console.log("Agent starting"));
  loop.on("agentEnd", () => console.log("Agent done"));

  await loop.prompt("Write a hello world");
}
```

#### After: τ

```python
from tau_agent_core.sdk import create_agent_session

async def main():
    session = create_agent_session(
        model="gpt-4o",
        tools=["read", "write", "bash"],
        system_prompt="You are a helpful assistant.",
    )

    session.subscribe(lambda e: print(f"Event: {e.type}"))

    messages = await session.prompt("Write a hello world")

    print(f"Done! {len(messages)} messages")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

### In-Memory Mode (Testing)

#### Before: Parley

```typescript
import { createInMemorySessionManager } from "pi-agent-core";
```

#### After: τ

```python
from tau_agent_core.session_manager import SessionManager
from tau_agent_core.sdk import create_agent_session

mgr = SessionManager.in_memory()
session = create_agent_session(
    model="gpt-4o",
    session_manager=mgr,
)
```

### RPC Mode (Headless)

#### Before: Parley

```typescript
// Parley has built-in RPC via stdio
```

#### After: τ

```bash
# Start τ in RPC mode
tau --rpc --port 8080

# Connect via the τ RPC protocol (JSON-RPC 2.0 over stdin/stdout)
# See docs/RPC-PROTOCOL.md for full details
```

---

## 5. FAQ

### Q: Are τ session files compatible with Parley?
**A:** Yes. τ uses the same JSONL file format as Parley. You can load Parley sessions in τ and vice versa.

### Q: Do I need to rewrite my tools?
**A:** Tool definitions are structurally the same (name, description, parameters, execute). You need to rewrite the `execute` function from TypeScript to Python, but the tool definition format is compatible.

### Q: Can I use the same extensions?
**A:** Extensions need to be rewritten from TypeScript to Python, but the extension API (`extend(api)`, `api.on()`, `api.register_tool()`) is designed to be similar.

### Q: What providers are available?
**A:** τ MVP supports only OpenAI (gpt-4o, gpt-4, gpt-4-turbo). Other providers (Anthropic, Google, etc.) are planned for Phase 7.

### Q: How do I migrate my session data?
**A:** No migration needed. τ reads and writes the same JSONL format as Parley. Just point τ to your Parley session directory.

### Q: Can I use τ alongside Parley?
**A:** Yes. They use the same session format, so you can switch between them seamlessly.

---

## Useful Links

| Resource | Link |
|----------|------|
| τ README (tau-agent-core) | `/tau-agent-core/README.md` |
| τ README (tau-ai) | `/tau-ai/README.md` |
| τ README (tau-coding-agent) | `/tau-coding-agent/README.md` |
| RPC Protocol | `/docs/RPC-PROTOCOL.md` |
| Example Extensions | `/examples/` |
| SDK Examples | `/examples/10_sdk_create_session.py` |
| Compatibility Map | `/docs/PI-TO-TAU-COMPATIBILITY.md` |

---

## License

MIT
