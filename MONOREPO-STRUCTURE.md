# Monorepo Structure — τ (tau)

```
agent-harness-py/
├── tau-ai/                    # Unified LLM provider abstraction (OpenAI-first)
│   ├── pyproject.toml
│   ├── src/tau_ai/
│   │   ├── __init__.py
│   │   ├── client.py          # AsyncOpenAI wrapper
│   │   ├── streaming.py       # Event-driven streaming protocol
│   │   ├── types.py           # Message/Tool/Model types
│   │   ├── tools.py           # Tool definitions, parameter validation
│   │   └── providers/
│   │       ├── __init__.py
│   │       ├── openai.py      # OpenAI provider (primary)
│   │       ├── openai_responses.py
│   │       └── registry.py    # Provider registration system
│   └── tests/
│
├── tau-agent-core/            # Agent runtime, loop, tools, sessions
│   ├── pyproject.toml
│   ├── src/tau_agent_core/
│   │   ├── __init__.py
│   │   ├── agent_loop.py      # Core turn loop
│   │   ├── agent_session.py   # Session state + event subscription
│   │   ├── session_manager.py # JSONL persistence, tree, fork, branch
│   │   ├── compaction.py      # Context window management
│   │   ├── system_prompt.py   # Prompt builder
│   │   ├── tools/             # Built-in tool implementations
│   │   │   ├── __init__.py
│   │   │   ├── read.py
│   │   │   ├── write.py
│   │   │   ├── edit.py
│   │   │   ├── bash.py
│   │   │   ├── grep.py
│   │   │   ├── find.py
│   │   │   └── ls.py
│   │   └── extensions/        # Extension system
│   │       ├── __init__.py
│   │       ├── loader.py      # Discover/load Python extension modules
│   │       ├── registry.py    # Tool/command/event registration
│   │       ├── events.py      # Async event bus
│   │       └── types.py       # Extension API surface
│   └── tests/
│
├── tau-coding-agent/          # TUI (fork of Parley) + CLI
│   ├── pyproject.toml
│   ├── src/tau_coding_agent/
│   │   ├── __init__.py
│   │   ├── cli.py             # CLI entry point (typer/argparse)
│   │   ├── app.py             # Textual App (fork of parley.py)
│   │   ├── widgets/           # TUI components
│   │   │   ├── __init__.py
│   │   │   ├── chat_display.py
│   │   │   ├── tool_call_widget.py    # NEW: render tool calls
│   │   │   ├── tool_result_widget.py  # NEW: render tool results
│   │   │   ├── thinking_block.py      # NEW: collapsible thinking
│   │   │   ├── session_tree.py        # NEW: branch navigation
│   │   │   └── footer.py              # NEW: token/cost context
│   │   ├── extensions/        # Builtin example extensions
│   │   └── themes/            # TUI themes (catppuccin, etc.)
│   └── tests/
│
└── docs/
    ├── ARCHITECTURE.md
    ├── EXTENSIONS.md
    ├── IMPLEMENTATION-PLAN.md
    └── ...
```

## Package Dependencies

```
tau-coding-agent  ──depends on──▶  tau-agent-core  ──depends on──▶  tau-ai
         │                              │
         │ (TUI only)                   │ (headless/programmatic)
         ▼                              ▼
   (Textual, openai-sdk)        (pydantic, asyncstdlib)
```

## Cross-Package Interfaces

### tau-agent-core → tau-ai

τ-agent-core imports from τ-ai:
- `Message`, `AssistantMessage`, `ToolResultMessage`, `ToolCall` data types
- `Provider` abstract class (streaming protocol)
- `stream_chat()` — the core streaming function

τ-agent-core does NOT import τ-coding-agent. It is TUI-agnostic.

### tau-coding-agent → tau-agent-core

τ-coding-agent imports from τ-agent-core:
- `AgentSession` — the main session/loop API
- `SessionManager` — for session persistence
- Built-in tool definitions
- Extension discovery paths

### tau-coding-agent → tau-ai

τ-coding-agent imports from τ-ai only for:
- Model type definitions (for display in TUI)
- Provider configuration
- NOT for streaming (that goes through τ-agent-core)

## Naming Conventions

| Concept | τ Name | pi Equivalent |
|---------|--------|---------------|
| LLM provider library | `tau_ai` | `pi-ai` |
| Agent runtime | `tau_agent_core` | `pi-agent-core` |
| Interactive CLI | `tau_coding_agent` | `pi-coding-agent` |
| Session | `AgentSession` | `AgentSession` |
| Message types | `UserMessage`, `AssistantMessage`, `ToolResultMessage` | Same |
| Tool definition | `ToolDefinition` | `AgentTool` |
| Event bus | `EventBus` | `EventBus` |
| Extension system | `ExtensionRegistry` | `ExtensionRunner` |
| Compaction | `compact_session()` | `compact()` |
