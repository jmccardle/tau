# τ Implementation Plan

## Phase 0 — Monorepo Setup (1 day)

Set up the package structure, shared configuration, and development tooling.

- [ ] Create `tau-ai/` with `pyproject.toml`, `src/tau_ai/`
- [ ] Create `tau-agent-core/` with `pyproject.toml`, `src/tau_agent_core/`
- [ ] Create `tau-coding-agent/` with `pyproject.toml`, `src/tau_coding_agent/`
- [ ] Set up import resolution between packages (editable install / path-based)
- [ ] Configure `pytest` shared config
- [ ] Add `.gitignore` (Python-specific)
- [ ] Add `pyproject.toml` at root for workspace configuration (optional, uv/poetry workspace)

## Phase 1 — τ-ai (OpenAI Provider) (1 week)

**Goal**: A working OpenAI provider that can stream text and tool calls.

### 1.1 Core Types (1 day)
- [ ] `tau_ai/types.py` — All message, tool, model, usage types
- [ ] `tau_ai/tools.py` — `define_tool()`, `validate_tool_arguments()`
- [ ] `tau_ai/streaming.py` — `AssistantMessageEventStream` async iterator

### 1.2 OpenAI Provider (3 days)
- [ ] `tau_ai/providers/base.py` — Abstract `Provider` class
- [ ] `tau_ai/providers/openai.py` — `OpenAICompletionsProvider`
  - Message conversion (τ → OpenAI)
  - Tool call conversion (τ → OpenAI function format)
  - Streaming: text deltas, tool call deltas, usage
  - Error handling (API errors → `error` event)
- [ ] `tau_ai/providers/registry.py` — Provider registration

### 1.3 Streaming Integration (2 days)
- [ ] `tau_ai/client.py` — `stream_simple()` function (agent loop entry point)
  - Wraps provider streaming in τ's event protocol
  - Handles `stop_reason` detection
  - Tracks token usage
- [ ] Tests for streaming (mock OpenAI responses)

### 1.4 Edge Cases (1 day)
- [ ] Reasoning/thinking level → `reasoning_effort` mapping
- [ ] Tool call argument accumulation (multi-delta tool calls)
- [ ] Token counting (input, output, total)
- [ ] Error event encoding

### Deliverable:
```python
from tau_ai import stream_simple, Model

model = Model(id="gpt-4o", name="GPT-4o", api="openai-completions",
              provider="openai", base_url="https://api.openai.com/v1",
              context_window=128000, max_tokens=4096)

stream = await stream_simple(model, {"system_prompt": "...", "messages": [...]}, {})
async for event in stream:
    if event.type == "text_delta":
        print(event.delta, end="")
    elif event.type == "toolcall_delta":
        print(f"\nTool call: {event.tool_call.name}")
final = await stream.result()
print(f"\nDone! {final.usage.total_tokens} tokens")
```

## Phase 2 — τ-agent-core Core (2 weeks)

**Goal**: A working agent loop that can run end-to-end with OpenAI tool calling.

### 2.1 Session Manager (3 days)
- [ ] `tau_agent_core/session_manager.py`
  - JSONL file format (v1, forward-compatible)
  - Session CRUD (new, load, save, list)
  - Entry types (session, message, tool_result, custom_message, compaction, branch_summary)
  - Tree structure (parent_id, branch traversal)
  - Version migrations (v1→v2→v3)
  - `in_memory()` for testing

### 2.2 System Prompt Builder (2 days)
- [ ] `tau_agent_core/system_prompt.py`
  - `build_system_prompt()` — date, cwd, tools, context files
  - Context file loading (AGENTS.md, .tau/AGENTS.md)
  - Tool snippets and guidelines generation

### 2.3 Agent Loop (5 days)
- [ ] `tau_agent_core/agent_loop.py`
  - `AgentLoop.run()` — full loop with steering/follow-up
  - `AgentLoop.run_continue()` — retry without new messages
  - `stream_response()` — LLM call with context building
  - `execute_tool_calls()` — sequential/parallel modes
  - `prepare_tool_call()` — validation + before hooks
  - `execute_tool()` — tool execution with abort support
  - `finalize_tool_call()` — after hooks
  - Event emission at every step
  - Tool batch early termination

### 2.4 Built-in Tools (3 days)
- [ ] `tau_agent_core/tools/base.py` — `ToolDefinition`, `AgentTool` base
- [ ] `tau_agent_core/tools/read.py` — File reading with truncation, image support
- [ ] `tau_agent_core/tools/write.py` — File writing with atomic operations
- [ ] `tau_agent_core/tools/edit.py` — Search/replace with diff rendering
- [ ] `tau_agent_core/tools/bash.py` — Shell execution with output streaming, temp files
- [ ] `tau_agent_core/tools/grep.py` — grep with file/path filtering
- [ ] `tau_agent_core/tools/find.py` — find with glob/regex
- [ ] `tau_agent_core/tools/ls.py` — Directory listing
- [ ] `tau_agent_core/tools/truncate.py` — Line/byte truncation utilities
- [ ] `tau_agent_core/tools/__init__.py` — `create_all_tools(cwd)` factory

### 2.5 Agent Session (2 days)
- [ ] `tau_agent_core/agent_session.py` — High-level session API
  - `prompt(text)` → sends prompt, runs loop, returns messages
  - `messages` property → active path messages
  - `subscribe(handler)` → event subscription
  - `compact()` → manual compaction trigger
  - `abort()` → abort current turn
  - `state` property → read-only state access

### 2.6 SDK Entry Point (1 day)
- [ ] `tau_agent_core/sdk.py` — `create_agent_session()`
  - Discovers extensions, tools, context files
  - Loads settings from `~/.tau/settings.json`
  - Returns `AgentSession`
  - In-memory session support for testing

### Deliverable:
```python
from tau_agent_core import create_agent_session, SessionManager

session = create_agent_session(
    model="gpt-4o",
    tools=["read", "bash", "edit", "write"],
)

session.subscribe(lambda e: print(f"{e.type}: {e.message or e.tool_name}"))
await session.prompt("List the files in this directory and summarize them")

# Prints streaming text deltas, tool calls, results, until done
```

## Phase 3 — τ-agent-core Extensions (1 week)

**Goal**: Python extensions can register tools, intercept events, and persist state.

### 3.1 Event Bus (1 day)
- [ ] `tau_agent_core/extensions/events.py` — `EventBus`
  - Channel-based async event dispatch
  - Safe error handling (handler exceptions logged, not raised)
  - Unsubscribe support

### 3.2 Extension Loader (1 day)
- [ ] `tau_agent_core/extensions/loader.py` — `ExtensionLoader`
  - Discover extensions from `~/.tau/extensions/` and `<cwd>/.tau/extensions/`
  - Import Python modules dynamically
  - Call `register(pi)` factory function
  - Handle import errors gracefully

### 3.3 Extension Registry (2 days)
- [ ] `tau_agent_core/extensions/registry.py` — `ExtensionRegistry`
  - `register_tool(definition)` → add to tool set
  - `get_all_tools()` → return all tools (built-in + extension)
  - `set_active_tools(names)` → enable/disable
  - `register_command(name, defn)` → slash command
  - `register_flag(name, defn)` → CLI flag
  - Tool info (`ToolInfo` with name, description, schema, source)

### 3.4 Extension API (1 day)
- [ ] `tau_agent_core/extensions/types.py` — `ExtensionAPI`, `ExtensionContext`, `ExtensionUI`
  - Event subscription methods
  - Tool/command/flag registration
  - Session state access
  - Messaging methods
  - UI methods (TUI-only, no-ops in headless)

### 3.5 Integration Tests (2 days)
- [ ] Test: extension registers a tool → LLM calls it → result returned
- [ ] Test: extension intercepts tool_call → blocks execution
- [ ] Test: extension modifies tool_result
- [ ] Test: extension persists state across session reload
- [ ] Test: extension registers a command → TUI invokes it

### Deliverable:
```python
# ~/.tau/extensions/hello.py
from tau_agent_core import ExtensionAPI, define_tool
from pydantic import BaseModel

class HelloParams(BaseModel):
    name: str = "world"

def hello_execute(tool_call_id, params, signal, on_update, ctx):
    return {
        "content": [{"type": "text", "text": f"Hello, {params.name}!"}],
    }

hello_tool = define_tool({
    "name": "hello",
    "label": "Hello",
    "description": "Greet someone by name",
    "parameters": HelloParams.model_json_schema(),
    "execute": hello_execute,
})

def register(pi: ExtensionAPI):
    pi.register_tool(hello_tool)
    pi.on("agent_start", lambda: print("Agent starting!"))
```

## Phase 4 — τ-coding-agent TUI (1.5 weeks)

**Goal**: A working interactive TUI based on Parley, with agent-aware rendering.

### 4.1 Fork Parley (1 day)
- [ ] Copy `parley.py` → `tau_coding_agent/app.py`
- [ ] Copy `parley.tcss` → `tau_coding_agent/themes/catppuccin.tcss`
- [ ] Replace `backends.py` with τ-agent-core integration
- [ ] Update config system (`.tau/settings.json` path)
- [ ] Remove Anthropic/Gemini backend code (τ-ai only)

### 4.2 Agent-Aware Widgets (5 days)
- [ ] `tau_coding_agent/widgets/tool_call_widget.py` — Collapsible tool call display
- [ ] `tau_coding_agent/widgets/tool_result_widget.py` — Tool result rendering
- [ ] `tau_coding_agent/widgets/thinking_block.py` — Collapsible thinking blocks
- [ ] `tau_coding_agent/widgets/chat_display.py` — Updated for agent events
- [ ] `tau_coding_agent/widgets/footer.py` — Token/cost/context bar
- [ ] `tau_coding_agent/widgets/input_bar.py` — @ file ref, !bash, tab complete

### 4.3 Session Tree (2 days)
- [ ] `tau_coding_agent/widgets/session_tree.py` — Branch-aware sidebar
- [ ] Session fork/clone UI
- [ ] Compaction summary display
- [ ] Labels/bookmarks in tree

### 4.4 CLI Entry Point (1 day)
- [ ] `tau_coding_agent/cli.py` — `tau` command
  - `tau` — interactive mode
  - `tau -p "prompt"` — print mode
  - `tau --mode rpc` — RPC mode
  - `tau --model gpt-4o "prompt"` — specify model
  - `tau --extension ./my_ext.py` — load extension
  - `tau --tools read,bash` — specific tools
  - `tau --continue` — resume last session
  - `tau --fork <id>` — fork session
  - `tau --thinking high` — thinking level
  - `tau -v` — verbose

### 4.5 Print Mode (1 day)
- [ ] Print mode: single prompt → stream → exit
- [ ] JSON mode: stream all events as JSON lines
- [ ] Stdin pipe support: `cat file.py | tau -p "Review this"`

### 4.6 Integration (2 days)
- [ ] Connect TUI widgets to τ-agent-core events
- [ ] Streaming text → ChatDisplay incremental update (keep 30Hz throttle)
- [ ] Tool calls → ToolCallWidget creation/update
- [ ] Tool results → ToolResultWidget creation
- [ ] Agent end → re-enable input, update footer
- [ ] Abort (Ctrl+C) → abort current turn

### Deliverable:
```bash
tau --model gpt-4o "What files are in this directory?"
# Shows TUI with streaming response, tool calls, results
```

## Phase 5 — Compaction & Session Features (1 week)

**Goal**: Long sessions, context management, session tree operations.

### 5.1 Compaction Engine (4 days)
- [ ] `tau_agent_core/compaction.py`
  - `should_compact(messages, model_window, margin)`
  - `prepare_compaction(entries, first_kept, instructions)`
  - `compact(session, config, summary_callback)` — run compaction via LLM
  - Compaction entry format and tree integration
  - Auto-compaction on context overflow
  - Proactive compaction before hitting limit

### 5.2 Session Operations (2 days)
- [ ] Session fork (copy path up to entry → new file)
- [ ] Session clone (duplicate active path at entry → new file)
- [ ] Session navigation (`navigate(entry_id)`)
- [ ] Branch summarization (summarize abandoned branches)

### 5.3 TUI Session Tree (2 days)
- [ ] `/tree` command — navigate tree in place
- [ ] Session selector — browse past sessions
- [ ] `/fork` and `/clone` commands
- [ ] Compaction trigger (`/compact`)
- [ ] Label/bookmark support

### 5.4 Settings (1 day)
- [ ] `~/.tau/settings.json` — global settings
- [ ] `.tau/settings.json` — project-local override
- [ ] Settings: model, thinking level, compaction, extension paths
- [ ] `/settings` command — interactive settings editor

## Phase 6 — Polish & RPC (1 week)

**Goal**: Production-ready with RPC support.

### 6.1 RPC Mode (3 days)
- [ ] `tau_agent_core/rpc.py` — JSONL over stdin/stdout
  - LF-delimited JSON framing
  - `send_prompt` command → streaming events
  - Tool call interception via RPC
  - UI interaction via extension UI protocol
  - `get_commands` command
  - `get_tools` command

### 6.2 Export (1 day)
- [ ] Session export to HTML (markdown + code blocks)
- [ ] Session export to markdown
- [ ] Shareable URL generation

### 6.3 Performance (1 day)
- [ ] 30Hz throttle for TUI updates
- [ ] Streaming message buffer (accumulate deltas, batch render)
- [ ] Large file handling (lazy loading)
- [ ] Memory profile for long sessions

### 6.4 Error Handling (2 days)
- [ ] Provider errors → error messages in chat
- [ ] Tool errors → error results in tool output
- [ ] Extension errors → logged, don't crash agent
- [ ] Network timeout handling
- [ ] API key validation at startup

### 6.5 Documentation (2 days)
- [ ] `README.md` for each package
- [ ] Example extensions (5-10)
- [ ] SDK usage examples
- [ ] RPC protocol documentation
- [ ] Migration guide from parley

## Phase 7 — Future (Not in MVP)

These are intentionally OUT of scope for the MVP but documented for future work:

- [ ] Anthropic provider (add to τ-ai)
- [ ] Google provider (add to τ-ai)
- [ ] OAuth flows (login/logout)
- [ ] Pi packages (npm-equivalent distribution)
- [ ] Multiple model support in TUI
- [ ] Custom themes
- [ ] Image paste (Ctrl+V)
- [ ] Multi-line input with Shift+Enter
- [ ] Session cost tracking
- [ ] Telemetry
- [ ] Sub-agent support (via extensions)
- [ ] Plan mode (via extensions)
- [ ] Permission gate system (built-in)

## Effort Summary

| Phase | Duration | Key Deliverable |
|-------|----------|----------------|
| 0. Monorepo | 1 day | Working package structure |
| 1. τ-ai | 1 week | OpenAI streaming provider |
| 2. Agent core | 2 weeks | Agent loop + built-in tools |
| 3. Extensions | 1 week | Python extension system |
| 4. TUI | 1.5 weeks | Interactive τ-coding-agent |
| 5. Compaction | 1 week | Context management + sessions |
| 6. Polish | 1 week | RPC, export, error handling |
| **Total** | **~8.5 weeks** | **Working τ agent harness** |

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| τ-ai provider interface changes during core dev | Medium | Lock types in Phase 1, test early with Phase 2 |
| Agent loop complexity (steering, follow-up, parallel tools) | High | Start simple (sequential only), add complexity iteratively |
| Parley fork diverges too far from original | Low | Keep Parley as a separate branch; τ-coding-agent is a fork |
| Extension system is harder than expected (hot-reload, module isolation) | Medium | Start with static extension loading, add hot-reload later |
| Session tree operations (fork, clone, navigation) edge cases | Medium | Build session manager first, test with synthetic data |
| 30Hz throttle + streaming + tool rendering TUI performance | Medium | Profile early with large outputs |

## Decision Points

### τ-ai: Pydantic vs raw dicts for messages

**Decision**: Use **pydantic models** for τ message types internally, but convert to **raw dicts** at the API boundary (τ-ai → τ-agent-core boundary).

Reason: Pydantic gives us validation, serialization, and type safety. The OpenAI API expects raw dicts, so we convert at the edge.

### τ-agent-core: Sync vs async events

**Decision**: **Async events** (all event handlers are async).

Reason: Tool execution is async (file I/O, network calls), and the extension system needs async support. Making everything async avoids blocking the agent loop.

### TUI: Textual vs Rich vs custom

**Decision**: **Textual** (same as Parley).

Reason: Parley already uses Textual. The component system, layout engine, and event loop are well-suited for this kind of application. Rich would require building everything from scratch.

### Tool parameters: JSON Schema vs pydantic models

**Decision**: Extensions define tools with **JSON Schema** (via pydantic's `model_json_schema()`). The agent loop validates with **pydantic**.

Reason: JSON Schema is the universal format for LLM tool definitions. Pydantic models are the convenient definition format for Python developers. The bridge is `model_json_schema()` → `model_validate()`.
