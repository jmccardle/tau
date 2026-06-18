# Pi → τ Compatibility Map

This document maps every major pi subsystem to its τ equivalent, noting what is:
- **DIRECT** — Direct port (same behavior, different language)
- **SIMPLIFIED** — Feature is reduced in scope for MVP
- **EXTENSION** — Can be built by users via the extension system
- **NOT PLANNED** — Intentionally not in scope for MVP

## Core Architecture

| Pi Subsystem | τ Equivalent | Strategy |
|-------------|-------------|----------|
| `@mariozechner/pi-coding-agent` | `tau-coding-agent` | **DIRECT** (fork of Parley) |
| `@mariozechner/pi-agent-core` | `tau-agent-core` | **DIRECT** (agent loop, tools, sessions) |
| `@mariozechner/pi-ai` | `tau-ai` | **DIRECT** (provider abstraction) |
| `@mariozechner/pi-tui` | Textual widgets in τ-coding-agent | **DIRECT** (τ-coding-agent includes its own TUI) |
| `@mariozechner/jiti` | Python `importlib` | **DIRECT** (simpler — Python loads modules natively) |

## Providers

| Pi Provider | τ Equivalent | Strategy |
|------------|-------------|----------|
| All 14+ providers | OpenAI only (MVP) | **SIMPLIFIED** — Add Anthropic/Google in Phase 7 |
| Provider registry | `tau_ai.providers.registry` | **DIRECT** |
| Model types | `tau_ai.types.Model` | **DIRECT** (simplified — no compat overrides) |
| Stream options | `tau_ai.types.StreamOptions` | **DIRECT** (core fields only) |
| OAuth flows | Not planned | **NOT PLANNED** — API keys via env vars |
| Proxy support | Via OpenAI SDK | **DIRECT** (OpenAI SDK handles proxy) |
| Retry logic | Via OpenAI SDK | **DIRECT** (SDK handles retries) |
| Transport (SSE/WS) | Via OpenAI SDK | **DIRECT** (SDK handles transport) |

## Agent Loop

| Pi Feature | τ Equivalent | Strategy |
|-----------|-------------|----------|
| `agentLoop()` / `agentLoopContinue()` | `AgentLoop.run()` / `run_continue()` | **DIRECT** |
| Tool execution (sequential/parallel) | Same modes | **DIRECT** |
| `beforeToolCall` / `afterToolCall` hooks | Extension system events | **DIRECT** |
| Context transform (`transformContext`) | Extension `context` event | **DIRECT** |
| Steering messages | `getSteeringMessages` / `deliverAs: steer` | **DIRECT** |
| Follow-up messages | `getFollowUpMessages` / `deliverAs: followUp` | **DIRECT** |
| Abort signal | `AbortSignal` / `session.abort()` | **DIRECT** |
| Thinking/reasoning levels | Mapping to `reasoning_effort` | **SIMPLIFIED** (only OpenAI levels) |
| Token counting | Via OpenAI usage in stream | **DIRECT** |
| Early termination (`terminate`) | `AgentToolResult.terminate` | **DIRECT** |

## Built-in Tools

| Pi Tool | τ Equivalent | Strategy |
|---------|-------------|----------|
| `read` | `tau_agent_core.tools.read` | **DIRECT** (truncation, images, offset/limit) |
| `write` | `tau_agent_core.tools.write` | **DIRECT** (atomic writes) |
| `edit` | `tau_agent_core.tools.edit` | **DIRECT** (search/replace, diff rendering) |
| `bash` | `tau_agent_core.tools.bash` | **DIRECT** (streaming output, temp files, timeout) |
| `grep` | `tau_agent_core.tools.grep` | **DIRECT** |
| `find` | `tau_agent_core.tools.find` | **DIRECT** |
| `ls` | `tau_agent_core.tools.ls` | **DIRECT** |
| Tool definitions | `create_all_tools(cwd)` | **DIRECT** |
| Custom tool rendering (`renderCall`, `renderResult`) | Widget system | **DIRECT** (via τ-coding-agent widgets) |

## Sessions

| Pi Feature | τ Equivalent | Strategy |
|-----------|-------------|----------|
| JSONL file format | JSONL file format | **DIRECT** (same format, compatible) |
| Tree structure (parent_id) | Same | **DIRECT** |
| Session migrations (v1→v2→v3) | Same | **DIRECT** |
| Branch traversal | `build_session_context()` | **DIRECT** |
| Session list | `SessionManager.list()` | **DIRECT** |
| Session fork | `SessionManager.fork()` | **DIRECT** |
| Session clone | `SessionManager.clone()` | **DIRECT** |
| Compaction entry | `CompactionEntry` | **DIRECT** |
| Branch summary entry | `BranchSummaryEntry` | **DIRECT** |
| Custom message entry | `CustomMessageEntry` | **DIRECT** |
| Thinking level change entry | `ThinkingLevelEntry` | **DIRECT** |
| Model change entry | `ModelChangeEntry` | **DIRECT** |

## Compaction

| Pi Feature | τ Equivalent | Strategy |
|-----------|-------------|----------|
| Auto-compaction on overflow | `should_compact()` + auto-trigger | **DIRECT** |
| Proactive compaction | Margin-based triggering | **DIRECT** |
| Manual compaction | `/compact` command | **DIRECT** |
| Custom compaction instructions | `compact(instructions=...)` | **DIRECT** |
| Compaction via LLM | Via extension or direct call | **DIRECT** |
| Branch summarization | `summarize_branch()` | **DIRECT** |
| Token estimation | Token counting heuristics | **DIRECT** |

## Extension System

| Pi Feature | τ Equivalent | Strategy |
|-----------|-------------|----------|
| TypeScript extension loading | Python extension loading | **DIRECT** (simpler with importlib) |
| Event subscription (`pi.on()`) | `ExtensionAPI.on()` | **DIRECT** |
| Tool registration (`pi.registerTool()`) | `ExtensionAPI.register_tool()` | **DIRECT** |
| Command registration (`pi.registerCommand()`) | `ExtensionAPI.register_command()` | **DIRECT** |
| Shortcut registration (`pi.registerShortcut()`) | TBD (TUI-level) | **SIMPLIFIED** (TUI keybindings) |
| CLI flag registration (`pi.registerFlag()`) | `ExtensionAPI.register_flag()` | **DIRECT** |
| Session persistence (`pi.appendEntry()`) | `ExtensionAPI.append_entry()` | **DIRECT** |
| Messaging (`pi.sendUserMessage()`) | `ExtensionAPI.send_user_message()` | **DIRECT** |
| UI interaction (`ctx.ui.*`) | `ExtensionContext.ui.*` | **DIRECT** (no-ops in headless) |
| Extension hot-reload (`/reload`) | `importlib.reload()` | **DIRECT** (simpler than jiti) |
| Extension locations | Same paths (`~/.tau/`, `<cwd>/.tau/`) | **DIRECT** |
| Pi packages | Not planned | **NOT PLANNED** — pip install for shared extensions |
| `isToolCallEventType()` | `is_tool_call(event, "tool_name")` | **DIRECT** |
| `createLocalBashOperations()` | Same utility | **DIRECT** |
| `wrapRegisteredTools()` | Same wrapper | **DIRECT** |

## Interactive Mode (TUI)

| Pi Feature | τ Equivalent | Strategy |
|-----------|-------------|----------|
| Main app layout | Fork of Parley app.py | **DIRECT** |
| Catppuccin theme | Catppuccin-mocha TCSS | **DIRECT** |
| Chat messages (user/assistant) | Typed widgets | **DIRECT** |
| Tool call display | **NEW** — ToolCallWidget | **DIRECT** (not in Parley, needed for τ) |
| Tool result display | **NEW** — ToolResultWidget | **DIRECT** (not in Parley, needed for τ) |
| Thinking blocks | **NEW** — ThinkingBlockWidget | **DIRECT** (not in Parley, needed for τ) |
| File diff display | **NEW** — DiffWidget | **SIMPLIFIED** (basic diff, no syntax highlighting) |
| Bash output display | **NEW** — BashOutputWidget | **DIRECT** (tail truncation) |
| Image display | **SIMPLIFIED** (basic image widget) | **SIMPLIFIED** — text representation only for MVP |
| Session sidebar | **NEW** — SessionTreeWidget | **DIRECT** (tree, not flat list) |
| Model selector | Model bar in footer | **SIMPLIFIED** (text display, no picker) |
| Thinking level selector | Footer indicator | **SIMPLIFIED** (text display only) |
| Settings selector | `/settings` command | **SIMPLIFIED** (flat list, no nested) |
| Session picker | Sidebar + `/resume` | **DIRECT** |
| Tree navigation | **NEW** — SessionTreeWidget | **DIRECT** |
| Footer info | **NEW** — FooterWidget | **DIRECT** (model, tokens, context) |
| Input bar | Enhanced TextArea | **DIRECT** (add @ file ref, !bash) |
| Command palette | Textual built-in | **DIRECT** |
| Keyboard shortcuts | Textual bindings | **SIMPLIFIED** (core shortcuts only) |
| Message queue (steer/followUp) | Queue in agent session | **DIRECT** |

## Print Mode

| Pi Feature | τ Equivalent | Strategy |
|-----------|-------------|----------|
| Print mode (`tau -p`) | Print mode | **DIRECT** |
| JSON mode (`tau --mode json`) | JSON mode | **DIRECT** |
| Stdin pipe support | Stdin pipe | **DIRECT** |

## RPC Mode

| Pi Feature | τ Equivalent | Strategy |
|-----------|-------------|----------|
| JSONL over stdin/stdout | JSONL over stdin/stdout | **DIRECT** |
| LF-delimited framing | LF-delimited framing | **DIRECT** |
| Extension UI protocol | Extension UI protocol | **DIRECT** |
| `send_prompt` command | `send_prompt` | **DIRECT** |
| `get_commands` command | `get_commands` | **DIRECT** |
| `get_tools` command | `get_tools` | **DIRECT** |

## System Prompt

| Pi Feature | τ Equivalent | Strategy |
|-----------|-------------|----------|
| Date + cwd in prompt | Same | **DIRECT** |
| Context files (AGENTS.md) | Same discovery | **DIRECT** |
| System prompt file (.tau/SYSTEM.md) | Same | **DIRECT** |
| Tool snippets in prompt | Same | **DIRECT** |
| Guidelines in prompt | Same | **DIRECT** |
| Custom system prompt | `--system-prompt` | **DIRECT** |
| Append system prompt | `--append-system-prompt` | **DIRECT** |

## What's Simplified or Omitted (MVP)

| Feature | Status | Reason |
|---------|--------|--------|
| Multiple providers (Anthropic, Google, etc.) | **SIMPLIFIED** — OpenAI only | OpenAI covers 90% of use cases; others can be added later |
| OAuth flows (`/login`, `/logout`) | **NOT PLANNED** | API keys via env vars are sufficient for MVP |
| Pi packages (npm distribution) | **NOT PLANNED** — Use pip install | Extensions shared via git/npm for MVP |
| Image paste (Ctrl+V) in TUI | **SIMPLIFIED** — Text only | Image rendering requires more TUI work |
| Sub-agents | **NOT PLANNED** | Extension system can build this |
| Plan mode | **NOT PLANNED** | Extension system can build this |
| Permission popups (built-in) | **SIMPLIFIED** — via extensions | Users build their own permission gates |
| To-do lists (built-in) | **NOT PLANNED** | Extension system |
| Background bash | **NOT PLANNED** — use tmux | Same philosophy as pi |
| Custom TUI themes | **SIMPLIFIED** — catppuccin only | Theme system can be added later |
| Export to HTML | **SIMPLIFIED** — Markdown only | HTML export is non-trivial |
| Telemetry | **NOT PLANNED** | Opt-in for now |
| Model cycling (Ctrl+P) | **SIMPLIFIED** — text display only | Full picker can be added later |
| Session sharing (GitHub gist) | **NOT PLANNED** | Can be built as an extension |

## What's Easier in τ Than in pi

| Feature | Why It's Easier |
|---------|----------------|
| Module loading | Python `importlib` vs jiti TypeScript transpiler |
| Hot reload | `importlib.reload()` vs file watch + cache busting |
| Type validation | pydantic is more mature for runtime validation |
| Async runtime | `asyncio` is more battle-tested for I/O-bound work |
| Extension packaging | pip handles dependencies natively |
| Testing | pytest + asyncio test support |
| Documentation | `--help`, `help()` built into Python |
| Development iteration | No compilation step (vs TypeScript) |

## What's Harder in τ Than in pi

| Feature | Why It's Harder |
|---------|----------------|
| Provider breadth | pi has 14+ providers; τ starts with 1 |
| TUI components | pi-tui has 20+ components; τ-coding-agent builds from scratch |
| Tree navigation | pi has sophisticated tree with labels, collapse, search |
| Session branching UI | pi has in-place tree view; τ needs to build this |
| Command palette richness | pi has many built-in commands; τ starts with basics |
