# τ-coding-agent Design — TUI (Fork of Parley)

## Scope

τ-coding-agent is the interactive terminal interface. It's built on **Parley's foundation** (Textual app, 30Hz throttle, incremental mounting, catppuccin theme) but extended with τ's agent-specific features.

## What Parley Gives Us (Reused As-Is)

| Component | Lines | Status |
|-----------|-------|--------|
| Textual App shell | ~500 | ✅ Direct port of parley.py |
| 30Hz streaming throttle | ~10 | ✅ Direct port |
| Chat sidebar with history | ~100 | ✅ Keep, enhance with tree |
| Command palette (Ctrl+P) | ~20 | ✅ Textual built-in |
| Catppuccin-mocha theme | ~150 | ✅ Direct port |
| Config system | ~30 | ✅ Keep, enhance |
| Chat persistence (JSON) | ~60 | ✅ Keep, extend to JSONL |

## What Changes from Parley

### 1. Backend → τ-agent-core

**Parley**: `backends.py` — `Backend` ABC with `chat()` and `stream_chat()`
**τ**: No more `Backend`. Instead, τ-coding-agent uses `AgentSession` from τ-agent-core.

```python
# OLD (Parley)
backend = create_backend(config)
content, usage = await backend.stream_chat(messages, display.update_streaming_message)

# NEW (τ)
session = AgentSession(...)
session.subscribe(lambda event: display.handle_event(event))
await session.prompt(user_input)
```

### 2. Message Rendering → Agent-Aware Widgets

**Parley**: Single `ChatMessage` widget (Markdown border for all messages)
**τ**: Distinct widgets for each message type

```
τ-coding-agent/src/tau_coding_agent/widgets/
├── __init__.py
├── chat_display.py         # ChatDisplay (scrollable message container)
├── user_message_widget.py  # Render user messages
├── assistant_message.py    # Render assistant text + thinking blocks
├── tool_call_widget.py     # NEW: Collapsible tool call display
├── tool_result_widget.py   # NEW: Tool result rendering (diff, output, file)
├── thinking_block.py       # NEW: Collapsible thinking/reasoning blocks
├── session_tree.py         # NEW: Tree sidebar (vs Parley's flat list)
├── footer.py               # NEW: Token/cost/context usage bar
├── input_bar.py            # Enhanced input (file references @, !bash)
└── model_bar.py            # Model selector in footer
```

### 3. Session Model → JSONL with Tree

**Parley**: `Chat` dataclass with flat message list, saved as single JSON
**τ**: JSONL file with tree structure (entries with parent_id)

### 4. Input Bar → Agent Input

**Parley**: TextArea with Ctrl+Enter, up/down history
**τ**: Same, plus:
- `@` fuzzy file reference (search project files)
- `!command` runs bash, sends output to LLM
- `!!command` runs bash silently
- Tab completion for file paths
- Multi-line Shift+Enter

### 5. Sidebar → Session Tree

**Parley**: Flat list of chats grouped by date
**τ**: Tree-structured navigation with:
- Branch indicators
- Labels/bookmarks on entries
- Compaction summary display
- Fork/clone capability
- Collapse/expand branches

### 6. Footer → Token/Cost Bar

**Parley**: Simple Footer widget
**τ**: Custom footer showing:
- Current model name
- Token count (input/output/total)
- Context usage percentage
- Session name
- Thinking level indicator

## Widget Designs

### ToolCallWidget (NEW)

```python
class ToolCallWidget(Container):
    """Display a tool call with status and arguments preview."""

    def __init__(self, tool_call, status="pending"):
        super().__init__()
        self.tool_call = tool_call
        self.status = status  # "pending", "running", "done", "error"

    def compose(self):
        # Header: tool name + status icon
        yield Label(f"🔧 {self.tool_call.name}", classes="tool-call-header")

        # Collapsible argument preview
        args_text = json.dumps(self.tool_call.arguments, indent=2)
        yield Collapsible(
            Markdown(args_text[:500]),
            title="Arguments",
            expanded=False,
        )

        # Status indicator
        if self.status == "running":
            yield Loader()
        elif self.status == "error":
            yield Label("❌ Failed", classes="tool-error")

    def update_status(self, status, result=None):
        self.status = status
        self.refresh()
```

### ToolResultWidget (NEW)

```python
class ToolResultWidget(Container):
    """Display tool execution results."""

    def render_tool_result(self, result, tool_name):
        if tool_name == "read":
            return self._render_read_result(result)
        elif tool_name == "write":
            return self._render_write_result(result)
        elif tool_name == "edit":
            return self._render_edit_result(result)
        elif tool_name == "bash":
            return self._render_bash_result(result)
        else:
            return Markdown(result)

    def _render_bash_result(self, result):
        """Bash output with tail truncation display."""
        output = result.get("content", "")
        truncation = result.get("details", {}).get("truncation")

        if truncation and truncation.get("truncated"):
            # Show last N lines
            lines = output.split("\n")
            preview_lines = lines[-5:]
            return Markdown("```\n" + "\n".join(preview_lines) + "\n```\n\n[... truncated]")
        return Markdown("```\n" + output + "\n```")

    def _render_edit_result(self, result):
        """File edit result with diff preview."""
        # Could integrate with difflib for unified diff display
        ...
```

### ThinkingBlockWidget (NEW)

```python
class ThinkingBlockWidget(Container):
    """Collapsible thinking/reasoning block."""

    def __init__(self, thinking_text):
        super().__init__()
        self.thinking_text = thinking_text
        self._collapsed = True

    def compose(self):
        yield Label(
            "💭 [click to expand]",
            classes="thinking-header",
            id="thinking-toggle",
        )
        self._content = Markdown(self.thinking_text)
        self._content.styles.display = "none"

    def on_click(self):
        self._collapsed = not self._collapsed
        self._content.styles.display = "block" if not self._collapsed else "none"
        self.query_one("#thinking-toggle").update(
            "💭 [click to collapse]" if not self._collapsed else "💭 [click to expand]"
        )
```

### SessionTreeWidget (NEW — replaces flat list)

```python
class SessionTreeWidget(Tree):
    """Tree-based session navigation."""

    def __init__(self, session_manager):
        super().__init__()
        self.session_manager = session_manager
        self._refresh()

    def _refresh(self):
        """Load sessions and build tree."""
        self.clear()
        for session in self.session_manager.list():
            root = self.add_root(f"📁 {session.cwd}")
            for entry in session.entries:
                if entry.type == "message" and entry.message.role == "user":
                    label = entry.message.content[:40]
                    self.add_child(root, label, data=entry)

    def on_tree_selected(self, node):
        """Navigate to selected entry."""
        entry = node.data
        self.app.navigate_to_entry(entry.id)
```

### FooterWidget (NEW)

```python
class FooterWidget(Static):
    """Session info bar with model, tokens, context usage."""

    def update_session_info(self, model, usage, context_percent, thinking_level, session_name):
        parts = [
            f"🤖 {model}",
            f"📊 {context_percent}% context",
            f"💭 {thinking_level}",
            f"📝 {session_name or 'unnamed'}",
        ]
        if usage:
            parts.append(f"🔢 {usage.total_tokens} tokens")
        self.update(" | ".join(parts))
```

## TUI Event Flow

```
User types in ChatInput (TextArea)
         │
         ├─ Enter      → emit Submitted(text)
         ├─ Ctrl+Enter  → emit MultiLineSubmitted(text)
         ├─ @           → trigger fuzzy file search → show completions
         ├─ !command    → execute bash → send output to LLM
         │
         └─ Submit(text)
               │
               ▼
ParleyApp.on_input_submitted(text)
               │
               ├─ session.prompt(text)
               │     │
               │     └─ τ-agent-core agent loop
               │           │
               │           └─ emits AgentEvents
               │
               └─ session.subscribe(event_handler)
                     │
                     ├─ message_update → ChatDisplay.update_streaming_message(delta)
                     ├─ tool_execution_start → ToolCallWidget(status="running")
                     ├─ tool_execution_end → ToolCallWidget(status="done", result=...)
                     └─ agent_end → update footer, re-enable input
```

## Key Differences from Parley — Summary

| Aspect | Parley | τ |
|--------|--------|---|
| Backend | `Backend` ABC (chat/stream_chat) | τ-agent-core `AgentSession` |
| Messages | Simple text dict | Structured (text, thinking, tool calls, tool results) |
| Persistence | Single JSON per chat | JSONL tree with session manager |
| Session nav | Flat list by date | Tree with branches, compaction |
| Input | TextArea | TextArea + @ file ref + !bash + tab complete |
| Rendering | Markdown widget | Typed widgets (user/assistant/tool/thinking) |
| Footer | Textual Footer | Custom: model, tokens, context, session |
| Tools | None | Full built-in tool suite |
| Extensions | None | Python extension system |
| Modes | Only interactive | Interactive + print + RPC |

## Dependencies

- `textual >= 0.47.0`
- `tau-agent-core` (local dependency)
- `typer` or `argparse` (CLI)
- Standard library only
