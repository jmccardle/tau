# Phase 1 Subphase 2 — OpenAI Provider Implementation

> **Topic**: Implement the OpenAI-completions provider that converts τ types to OpenAI API format and back.

## Scope

This subphase implements `tau_ai.providers.openai.OpenAICompletionsProvider`, the **only** concrete provider. It:
1. Converts τ `Message` list to OpenAI API format
2. Converts τ `ToolDefinition` to OpenAI `function_call` format
3. Converts OpenAI API responses back to τ `AssistantMessage`
4. Handles all error cases

## Reference

- `SUBPHASE-0.0.md` lines 120-160: message type contracts
- `docs/tau-ai.md` lines 60-120: provider design
- `docs/tau-agent-core.md` lines 160-220: how agent loop consumes provider
- pi's `pi-ai/dist/providers/openai-completions-provider.js` (reference implementation)

## Implementation Outline

### `tau_ai/providers/openai.py`

```python
class OpenAICompletionsProvider(Provider):
    """Provider for OpenAI-compatible APIs (OpenAI, Ollama, vLLM, etc.)."""

    async def stream_chat(
        self,
        model: Model,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        options: dict | None = None,
    ) -> AssistantMessageEventStream:
        ...

    def _convert_messages_to_openai(self, messages: list[Message]) -> list[dict]:
        """Convert τ messages to OpenAI API message format."""
        ...

    def _convert_tools_to_openai(self, tools: list[ToolDefinition]) -> list[dict]:
        """Convert τ tool definitions to OpenAI function format."""
        ...

    def _convert_openai_choice_to_message(self, choice: dict) -> AssistantMessage:
        """Convert OpenAPI choice (non-streaming) to τ AssistantMessage."""
        ...
```

### Key Conversion Rules

**Messages → OpenAI**:
- `UserMessage` → `{"role": "user", "content": [...]}`
  - `TextContent` → `{"type": "text", "text": ...}`
  - `ImageContent` → `{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}`
- `AssistantMessage` → `{"role": "assistant", "content": ..., "tool_calls": ...}`
  - Text-only content → `{"content": "..."}`
  - Tool calls in content → `{"tool_calls": [...]}`
- `ToolResultMessage` → `{"role": "tool", "tool_call_id": ..., "content": ...}`
- `ThinkingContent` → included in `content` field (OpenAI handles as text)

**Tools → OpenAI**:
- `ToolDefinition.parameters` → `functions[].parameters` (JSON Schema)
- `ToolDefinition.description` → `functions[].description`
- `ToolDefinition.name` → `functions[].name`

**OpenAI Choice → τ Message** (streaming):
- `delta.content` → `TextDeltaEvent(delta=delta.content, partial=...)`
- `delta.tool_calls[].function.name` → `ToolCallDeltaEvent(delta=..., partial=...)`
- `delta.tool_calls[].function.arguments` → accumulated into `ToolCall.arguments`
- `finish_reason == "tool_calls"` → `ToolCallDeltaEvent` for each tool call
- `finish_reason == "stop"` → `DoneEvent(final=message, usage=...)`
- `finish_reason == "length"` → `DoneEvent(final=message, usage=...)` (token limit)

## Done Criteria

- `OpenAICompletionsProvider.stream_chat()` produces the correct event stream for:
  - Pure text response (no tool calls)
  - Tool call response (single and multiple tool calls)
  - Mixed text + tool calls
  - Error response (invalid API key, rate limit, etc.)
  - Truncated response (token limit hit)
  - Reasoning/thinking content
- `_convert_messages_to_openai()` correctly handles all message types
- `_convert_tools_to_openai()` produces valid OpenAI function format
- `_convert_openai_choice_to_message()` handles streaming delta accumulation

### Specific Conversions to Test

```python
# User message with text
UserMessage(content=[TextContent(text="hello")])
→ {"role": "user", "content": [{"type": "text", "text": "hello"}]}

# User message with image
UserMessage(content=[ImageContent(data="abc123", mime="image/png")])
→ {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}}]}

# Assistant message with tool calls
AssistantMessage(content=[
    TextContent(text="Let me check."),
    ToolCall(id="call_1", name="bash", arguments={"command": "ls"})
])
→ {"role": "assistant", "content": "Let me check.", "tool_calls": [{"id": "call_1", "name": "bash", "arguments": "..."}]}

# Tool result
ToolResultMessage(tool_call_id="call_1", tool_name="bash", content=[TextContent(text="file1 file2")])
→ {"role": "tool", "tool_call_id": "call_1", "content": "file1 file2"}

# Tool definition
ToolDefinition(name="bash", label="Bash", description="Run bash command",
               parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]})
→ {"type": "function", "function": {"name": "bash", "description": "Run bash command", "parameters": {...}}}
```

## Testing Strategy

### Test 1: Message conversion — text only

```python
async def test_convert_messages_text_only(mock_openai):
    messages = [
        UserMessage(content=[TextContent(text="hello")]),
    ]
    provider = OpenAICompletionsProvider()
    openai_messages = provider._convert_messages_to_openai(messages)
    assert len(openai_messages) == 1
    assert openai_messages[0]["role"] == "user"
    assert openai_messages[0]["content"][0]["text"] == "hello"
```

### Test 2: Message conversion — tool calls

```python
async def test_convert_messages_with_tool_calls(mock_openai):
    messages = [
        AssistantMessage(content=[
            TextContent(text="checking"),
            ToolCall(id="c1", name="bash", arguments={"command": "ls"})
        ])
    ]
    result = provider._convert_messages_to_openai(messages)
    assert "tool_calls" in result[0]
    assert len(result[0]["tool_calls"]) == 1
    assert result[0]["tool_calls"][0]["name"] == "bash"
```

### Test 3: Tool conversion

```python
async def test_convert_tools():
    tool = ToolDefinition(
        name="bash", label="Bash", description="Run bash",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        execute=mock_execute
    )
    openai_tools = provider._convert_tools_to_openai([tool])
    assert len(openai_tools) == 1
    assert openai_tools[0]["function"]["name"] == "bash"
    assert "command" in openai_tools[0]["function"]["parameters"]["properties"]
```

### Test 4: Streaming event production

```python
async def test_stream_text_response(mock_openai):
    provider = OpenAICompletionsProvider()
    stream = await provider.stream_chat(
        model=Model(id="gpt-4o", name="GPT-4o", ...),
        messages=[UserMessage(content=[TextContent(text="hi")])],
    )
    events = []
    async for event in stream:
        events.append(event)

    assert any(e.type == "text_delta" for e in events)
    done = next(e for e in events if e.type == "done")
    assert isinstance(done, DoneEvent)
    assert isinstance(done.final, AssistantMessage)
    assert done.usage.total_tokens > 0
```

### Test 5: Tool call delta accumulation

```python
async def test_stream_tool_call_delta(mock_openai):
    # Simulate OpenAI streaming: delta arrives in parts
    provider = OpenAICompletionsProvider()
    stream = await provider.stream_chat(
        model=Model(...),
        messages=[...],
        tools=[bash_tool_def]
    )
    events = [e async for e in stream]
    toolcall_events = [e for e in events if e.type == "toolcall_delta"]
    assert len(toolcall_events) > 0
    # Verify arguments accumulated correctly
    final = await stream.result()
    assert any(c.type == "toolCall" for c in final.content)
```

### Test 6: Error handling

```python
async def test_error_event_on_api_error(mock_openai):
    mock_openai.api_key_auth = Mock(side_effect=Exception("Invalid API key"))
    stream = await provider.stream_chat(model=Model(...), messages=[...])
    events = [e async for e in stream]
    assert any(e.type == "error" for e in events)
```

## Success Signal

The OpenAI provider correctly converts between τ types and OpenAI API format. Streaming events match the protocol defined in `SUBPHASE-0.0.md`. Error cases produce the right events. An agent can swap `OpenAICompletionsProvider` with a mock provider and all tests still pass (provider is called via the `Provider` ABC).
