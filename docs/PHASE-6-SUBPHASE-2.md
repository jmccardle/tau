# Phase 6 Subphase 2 — Session Export

> **Topic**: Implement session export to markdown and HTML formats.

## Scope

This subphase implements `tau_agent_core.export.export_session()` — exporting a session's messages to a human-readable format. This is used for:
1. Sharing sessions (markdown export)
2. Archiving sessions (HTML export)

## Reference

- `SUBPHASE-6-SUBPHASE-0.md`: export config types
- `docs/IMPLEMENTATION-PLAN.md` lines 500-530: export spec
- pi's `export.js` (reference — markdown and HTML export)

## Implementation Outline

### `tau_agent_core/export.py`

```python
from typing import Protocol

class Exporter(Protocol):
    def export(self, messages: list[Message], config: ExportConfig) -> str: ...

def export_session(
    messages: list[Message],
    config: ExportConfig = ExportConfig(format="markdown"),
) -> str:
    """Export a session's messages to a string."""
    exporters: dict[str, Exporter] = {
        "markdown": MarkdownExporter(),
        "html": HTMLExporter(),
    }
    exporter = exporters.get(config.format)
    if not exporter:
        raise ValueError(f"Unknown export format: {config.format}")
    return exporter.export(messages, config)

class MarkdownExporter:
    def export(self, messages: list[Message], config: ExportConfig) -> str:
        lines = ["# Session Export", ""]
        for msg in messages:
            lines.append(self._export_message(msg, config))
            lines.append("")
        return "\n".join(lines)

    def _export_message(self, msg: Message, config: ExportConfig) -> str:
        if msg.role == "user":
            return f"### User\n\n{self._extract_text(msg.content)}"
        elif msg.role == "assistant":
            parts = []
            for block in msg.content:
                if block.type == "thinking" and config.include_thinking:
                    parts.append(f"> 💭 {block.text}\n")
                elif block.type == "text":
                    parts.append(block.text)
                elif block.type == "toolCall":
                    parts.append(f"\n```tool\n{block.name}: {json.dumps(block.arguments)}\n```\n")
            return f"### Assistant\n\n{''.join(parts)}"
        elif msg.role == "toolResult":
            content = self._extract_text(msg.content)
            if config.include_tool_calls:
                return f"### Tool: {msg.tool_name}\n\n```\n{content}\n```\n"
            return ""
        return ""

    def _extract_text(self, content: list[dict]) -> str:
        return " ".join(c.get("text", "") for c in content if c.get("type") == "text")

class HTMLExporter:
    def export(self, messages: list[Message], config: ExportConfig) -> str:
        html = [
            "<!DOCTYPE html>",
            "<html>",
            "<head><title>τ Session Export</title>",
            "<style>",
            "  .message { margin: 1em 0; padding: 1em; border-radius: 8px; }",
            "  .user { background: #e8f5e9; }",
            "  .assistant { background: #e3f2fd; }",
            "  .tool { background: #fff3e0; }",
            "  pre { background: #f5f5f5; padding: 1em; border-radius: 4px; overflow-x: auto; }",
            "</style>",
            "</head>",
            "<body>",
            "<h1>Session Export</h1>",
        ]
        for msg in messages:
            html.append(self._export_message(msg, config))
        html.extend(["</body>", "</html>"])
        return "\n".join(html)

    def _export_message(self, msg: Message, config: ExportConfig) -> str:
        role_class = msg.role.replace("Result", "Tool") if msg.role != "assistant" else "assistant"
        if msg.role == "toolResult":
            role_class = "tool"
        content = self._extract_html_content(msg.content, config)
        return f'<div class="message {role_class}"><strong>{msg.role}</strong>\n{content}\n</div>'
```

## Done Criteria

- `export_session()` exports messages to markdown and HTML formats
- Markdown export has sections for user, assistant, and tool messages
- Thinking blocks are included in markdown export when `include_thinking=True`
- Tool calls are rendered as code blocks
- HTML export has CSS styling
- HTML export has sections for user, assistant, and tool messages
- `include_tool_calls=False` omits tool messages
- `include_timestamps=True` adds timestamps

## Testing Strategy

### Test 1: Markdown export — user and assistant

```python
async def test_markdown_export():
    messages = [
        UserMessage(content=[TextContent(text="hello")]),
        AssistantMessage(content=[TextContent(text="hi there!")]),
    ]
    result = export_session(messages, ExportConfig(format="markdown"))
    assert "### User" in result
    assert "hello" in result
    assert "### Assistant" in result
    assert "hi there!" in result
```

### Test 2: Markdown export — tool call

```python
async def test_markdown_export_tool():
    messages = [
        UserMessage(content=[TextContent(text="run ls")]),
        AssistantMessage(content=[ToolCall(id="c1", name="bash", arguments={"command": "ls"})]),
        ToolResultMessage(tool_call_id="c1", tool_name="bash", content=[TextContent(text="file1\nfile2")]),
    ]
    result = export_session(messages, ExportConfig(format="markdown", include_tool_calls=True))
    assert "bash" in result
    assert "file1" in result
```

### Test 3: Markdown export — no tool calls

```python
async def test_markdown_export_no_tools():
    messages = [
        AssistantMessage(content=[ToolCall(id="c1", name="bash", arguments={"command": "ls"})]),
        ToolResultMessage(tool_call_id="c1", tool_name="bash", content=[TextContent(text="out")]),
    ]
    result = export_session(messages, ExportConfig(format="markdown", include_tool_calls=False))
    assert "bash" not in result
    assert "out" not in result
```

### Test 4: HTML export

```python
async def test_html_export():
    messages = [
        UserMessage(content=[TextContent(text="hello")]),
    ]
    result = export_session(messages, ExportConfig(format="html"))
    assert "<!DOCTYPE html>" in result
    assert 'class="user"' in result
    assert "hello" in result
```

### Test 5: HTML export — styling

```python
async def test_html_styling():
    messages = [UserMessage(content=[TextContent(text="hi")])]
    result = export_session(messages, ExportConfig(format="html"))
    assert ".user" in result
    assert ".assistant" in result
    assert ".tool" in result
```

### Test 6: Unknown format

```python
async def test_unknown_format():
    with pytest.raises(ValueError):
        export_session([], ExportConfig(format="unknown"))
```

## Success Signal

All 6 test categories pass. Export produces valid markdown and HTML. Tool calls are formatted as code blocks. Thinking blocks are included when configured. Unknown formats raise ValueError.
