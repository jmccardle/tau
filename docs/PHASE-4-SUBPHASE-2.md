# Phase 4 Subphase 2 — Agent-Aware Widgets

> **Topic**: Implement typed widgets for user messages, assistant messages, tool calls, tool results, thinking blocks, and footer.

## Scope

This subphase implements all the agent-specific rendering widgets that replace Parley's single `ChatMessage` widget. These widgets render different message types with appropriate formatting:

| Widget | Purpose | Key Feature |
|--------|---------|-------------|
| `UserMessageWidget` | User prompts | Markdown border, timestamp |
| `AssistantMessageWidget` | Assistant responses | Text + thinking blocks + tool calls |
| `ToolCallWidget` | Pending/running tool calls | Collapsible, status indicator, loader |
| `ToolResultWidget` | Tool execution results | Diff display, output preview, truncation |
| `ThinkingBlockWidget` | Thinking/reasoning content | Collapsible |
| `FooterWidget` | Session info bar | Model, tokens, context, session name |

## Reference

- `SUBPHASE-4-SUBPHASE-0.md`: widget data contracts
- `docs/tau-coding-agent.md` lines 100-160: widget designs
- `docs/tau-coding-agent.md` lines 160-220: TUI event flow
- parley.py: how Parley renders messages (to replace)
- `docs/textual-headless-testing.md`: headless Textual testing patterns and fixtures

## Implementation Outline

### ChatDisplay

```python
class ChatDisplay(Container):
    """Scrollable container for chat messages."""

    def __init__(self):
        self._messages: list[Widget] = []
        self._streaming_message: AssistantMessageWidget | None = None

    def append_message(self, data: ChatMessageData):
        """Append a new message widget."""
        if data.role == "user":
            widget = UserMessageWidget(data)
        elif data.role == "assistant":
            widget = AssistantMessageWidget(data)
        elif data.role == "toolResult":
            widget = ToolResultWidget(data)
        self._messages.append(widget)
        self.mount(widget)

    def update_streaming_message(self, delta: str):
        """Update the current streaming assistant message."""
        if not self._streaming_message:
            self._streaming_message = AssistantMessageWidget(...)
            self.append_message(ChatMessageData(
                role="assistant",
                content=[],
                streaming=True,
            ))
        self._streaming_message.append_text(delta)
        self.scroll_end()

    def finalize_streaming_message(self):
        """Convert the streaming message to a final message."""
        if self._streaming_message:
            self._streaming_message.is_streaming = False
            self._streaming_message = None
```

### UserMessageWidget

```python
class UserMessageWidget(Container):
    def __init__(self, data: ChatMessageData):
        super().__init__()
        text = " ".join(
            c.get("text", "") for c in data.content
            if c.get("type") == "text"
        )
        self._content = Markdown(text)
        self._timestamp = Label(
            f"{datetime.fromtimestamp(data.timestamp / 1000).strftime('%H:%M')}"
            if data.timestamp else "",
            classes="timestamp",
        )

    def compose(self):
        yield self._content
        yield self._timestamp
```

### AssistantMessageWidget

```python
class AssistantMessageWidget(Container):
    def __init__(self, data: ChatMessageData):
        super().__init__()
        self._is_streaming = data.streaming
        self._text_parts: list[str] = []
        self._widgets: list[Widget] = []

    def compose(self):
        yield Markdown("".join(self._text_parts))
        for w in self._widgets:
            yield w

    def append_text(self, text: str):
        self._text_parts.append(text)
        self.refresh()

    def append_thinking(self, text: str):
        w = ThinkingBlockWidget(text)
        self._widgets.append(w)
        self.mount(w)

    def append_tool_call(self, data: ToolCallData):
        w = ToolCallWidget(data)
        self._widgets.append(w)
        self.mount(w)
```

### ToolCallWidget

```python
class ToolCallWidget(Container):
    def __init__(self, data: ToolCallData):
        super().__init__()
        self._status = data.status
        self._tool_name = data.tool_name
        self._args = data.arguments

    def compose(self):
        # Status icon + tool name
        icon = {"pending": "⏳", "running": "🔄", "done": "✅", "error": "❌"}[self._status]
        yield Label(f"{icon} {self._tool_name}", classes="tool-header")

        # Collapsible arguments
        args_text = json.dumps(self._args, indent=2)
        yield Collapsible(
            Markdown(args_text[:500]),
            title="Arguments",
            expanded=False,
        )

        if self._status == "running":
            yield Loader()
```

### ToolResultWidget

```python
class ToolResultWidget(Container):
    def __init__(self, data: ChatMessageData):
        super().__init__()
        self._tool_name = data.tool_name or ""
        self._is_error = data.is_error
        self._content = self._render_content(data)

    def _render_content(self, data: ChatMessageData) -> Widget:
        """Render tool result based on tool type."""
        text = " ".join(c.get("text", "") for c in data.content)

        if self._tool_name == "bash":
            return self._render_bash(text, data)
        elif self._tool_name == "edit":
            return self._render_edit(text, data)
        elif self._tool_name == "read":
            return self._render_read(text, data)
        else:
            return Markdown(text)

    def _render_bash(self, text: str, data: ChatMessageData) -> Widget:
        """Bash output with truncation."""
        details = {}
        for c in data.content:
            if c.get("type") == "text":
                try:
                    details = json.loads(c.get("details", "{}"))
                except:
                    pass
        truncation = details.get("truncation")
        if truncation and truncation.get("truncated"):
            lines = text.split("\n")
            preview = "\n".join(lines[-5:])
            return Column(
                Markdown("```\n" + preview + "\n```\n\n[... truncated]"),
            )
        return Markdown("```\n" + text + "\n```")
```

### ThinkingBlockWidget

```python
class ThinkingBlockWidget(Container):
    def __init__(self, text: str):
        super().__init__()
        self._collapsed = True
        self._content = Markdown(text)

    def compose(self):
        yield Label("💭 [click to expand]", classes="thinking-header", id="thinking-toggle")
        self._content.styles.display = "none"
        yield self._content

    def on_click(self):
        self._collapsed = not self._collapsed
        self._content.styles.display = "block" if not self._collapsed else "none"
        self.query_one("#thinking-toggle").update(
            "💭 [click to collapse]" if not self._collapsed else "💭 [click to expand]"
        )
```

### FooterWidget

```python
class FooterWidget(Static):
    def __init__(self):
        super().__init__()
        self._data = FooterData(model="unknown")

    def update(self, data: FooterData):
        self._data = data
        parts = [
            f"🤖 {data.model}",
        ]
        if data.tokens:
            parts.append(f"🔢 {data.tokens} tokens")
        if data.context_percent is not None:
            parts.append(f"📊 {data.context_percent}%")
        if data.session_name:
            parts.append(f"📝 {data.session_name}")
        self.update(" | ".join(parts))
```

## Event Handler Wiring

The `ParleyApp._handle_event()` method dispatches events to widgets:

```python
def _handle_event(self, event: AgentEvent):
    match event.type:
        case "message_start":
            self._chat_display.append_message(
                ChatMessageData(
                    role=event.message.role,
                    content=...serialize...,
                )
            )
        case "message_update":
            self._chat_display.update_streaming_message(event)
        case "message_end":
            self._chat_display.finalize_streaming_message()
        case "tool_execution_start":
            self._chat_display.append_tool_call(
                ToolCallData(
                    tool_name=event.tool_name,
                    tool_call_id=event.tool_call_id,
                    arguments=event.args or {},
                    status="running",
                )
            )
        case "tool_execution_update":
            # Update existing tool call widget
            ...
        case "tool_execution_end":
            # Update tool call widget with result
            self._chat_display.update_tool_result(
                ToolResultData(
                    tool_name=event.tool_name,
                    content=event.result.get("content", []),
                    is_error=event.is_error,
                )
            )
        case "agent_end":
            self._footer.update(FooterData(
                model=self._session._model.id,
                tokens=event.messages and ...,
                session_name=self._session._session_name,
            ))
```

## Done Criteria

- `ChatDisplay` renders user messages, assistant messages, and tool results correctly
- `UserMessageWidget` renders markdown with a timestamp
- `AssistantMessageWidget` renders text + thinking blocks + tool calls
- `ToolCallWidget` shows status icon, tool name, collapsible arguments, and loader
- `ToolResultWidget` renders bash output with truncation, edit diffs, etc.
- `ThinkingBlockWidget` is collapsible (click to expand/collapse)
- `FooterWidget` shows model, tokens, context usage, and session name
- All widgets mount/unmount correctly in the chat display
- Agent events are dispatched to the correct widgets

## Testing Strategy

### Test 1: ChatDisplay appends messages

```python
async def test_chat_display_appends():
    display = ChatDisplay()
    data = ChatMessageData(role="user", content=[{"type": "text", "text": "hello"}])
    display.append_message(data)
    assert len(display._messages) == 1
    assert isinstance(display._messages[0], UserMessageWidget)
```

### Test 2: Streaming message accumulation

```python
async def test_streaming_message():
    display = ChatDisplay()
    display.update_streaming_message(
        AgentEvent(type="message_update", message=AssistantMessage(content=[TextContent(text="h")]))
    )
    display.update_streaming_message(
        AgentEvent(type="message_update", message=AssistantMessage(content=[TextContent(text="i")]))
    )
    # Should have accumulated "hi"
    assert display._streaming_message
    assert "hi" in "".join(display._streaming_message._text_parts)
```

### Test 3: Tool call widget creation

```python
async def test_tool_call_widget():
    display = ChatDisplay()
    data = ToolCallData(
        tool_name="bash",
        tool_call_id="tc1",
        arguments={"command": "ls"},
        status="running",
    )
    display.append_tool_call(data)
    assert len(display._widgets) == 1
    assert isinstance(display._widgets[0], ToolCallWidget)
```

### Test 4: Tool result rendering

```python
async def test_bash_result_rendering():
    widget = ToolResultWidget(ChatMessageData(
        role="toolResult",
        tool_name="bash",
        content=[{"type": "text", "text": "file1\nfile2\nfile3\nfile4\nfile5\nfile6\nfile7"}],
    ))
    # Should render with Markdown wrapper
    assert isinstance(widget._content, (Markdown, Column))
```

### Test 5: Thinking block collapsibility

```python
async def test_thinking_block_click():
    widget = ThinkingBlockWidget("Let me think...")
    # Initially collapsed
    assert widget._collapsed is True
    # Simulate click
    widget.on_click()
    assert widget._collapsed is False
    assert widget._content.styles.display == "block"
```

### Test 6: Footer update

```python
async def test_footer_update():
    footer = FooterWidget()
    footer.update(FooterData(model="gpt-4o", tokens=1500, context_percent=45, session_name="test"))
    expected = "🤖 gpt-4o | 🔢 1500 tokens | 📊 45% | 📝 test"
    assert expected in footer.renderable
```

### Test 7: Event dispatch

```python
async def test_event_dispatch(app):
    events_received = []

    async def mock_handle(e):
        events_received.append(e.type)

    app._handle_event = mock_handle
    await app._handle_event(AgentEvent(type="message_start"))
    assert "message_start" in events_received
```

## Success Signal

All 7 test categories pass. The TUI correctly renders all message types. Streaming text is accumulated and displayed at 30Hz. Tool calls show status indicators. Tool results render differently based on tool type. Thinking blocks are collapsible. The footer shows session info. Agent events are dispatched to the correct widgets.
