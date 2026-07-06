# Phase 1 Subphase 1 — Core Types Implementation

> **Topic**: Implement `tau_ai.types`, `tau_ai.tools`, and `tau_ai.abort` with full pydantic models, validation, and serialization.

## Scope

This subphase implements the **data layer** of τ-ai: message types, tool definitions, abort signal, and parameter validation. No provider, no HTTP, no streaming — just pure data.

## Reference

- `SUBPHASE-0.0.md` lines 120-180: type contracts
- `docs/tau-ai.md` lines 1-60: type design rationale
- `docs/tau-agent-core.md` lines 1-60: how agent loop consumes types
- `MONOREPO-STRUCTURE.md` lines 15-20: file layout

## Implementation Outline

### 1. `tau_ai/types.py`

```python
# Dataclasses or Pydantic BaseModel for all message types
# ContentBlock is a discriminated union
# Use model_validator to handle content block type dispatch
```

Key types:
- `UserMessage`, `AssistantMessage`, `ToolResultMessage`
- `TextContent`, `ThinkingContent`, `ImageContent`, `ToolCall` (as ContentBlock variants)
- `Model` (id, name, provider, base_url, context_window, max_tokens)
- `Usage` (input_tokens, output_tokens, total_tokens)
- `AbortSignal`
- `Message` (Union type alias)

### 2. `tau_ai/tools.py`

```python
# define_tool() — creates a ToolDefinition dict
# validate_tool_arguments() — validates tool call args against JSON schema
# validate_tool_parameters() — validates ToolDefinition has required fields
```

### 3. `tau_ai/abort.py`

```python
class AbortSignal:
    # threading.Lock for thread safety
    # _aborted: bool
```

## Done Criteria

- All types import from `tau_ai.types`, `tau_ai.tools`, `tau_ai.abort`
- `UserMessage`, `AssistantMessage`, `ToolResultMessage` can be instantiated and serialized to/from dicts
- `ContentBlock` discriminates on `type` field automatically
- `validate_tool_arguments()` raises `ValueError` on invalid args
- `validate_tool_arguments()` returns a dict on valid args
- `AbortSignal.abort()` makes subsequent `is_aborted()` return True (thread-safe)
- `Model` serializes to OpenAI-compatible dict format
- `Usage` is a frozen dataclass (immutable)

## Testing Strategy

### Test 1: Message round-trip serialization

```python
async def test_message_roundtrip():
    msg = UserMessage(content=[TextContent(text="hello")])
    d = msg.model_dump()
    assert d["role"] == "user"
    assert d["content"][0]["type"] == "text"
    assert d["content"][0]["text"] == "hello"

    recovered = UserMessage.model_validate(d)
    assert recovered.content[0].text == "hello"
```

### Test 2: ContentBlock type discrimination

```python
async def test_content_block_discrimination():
    # Text
    tb = TextContent(text="hello")
    assert tb.type == "text"
    assert tb.text == "hello"

    # Image
    ib = ImageContent(data="base64string", mime="image/png")
    assert ib.type == "image"

    # ToolCall
    tbc = ToolCall(id="call_123", name="bash", arguments={"command": "ls"})
    assert tbc.type == "toolCall"
    assert tbc.name == "bash"
```

### Test 3: Tool argument validation

```python
async def test_validate_tool_arguments_valid():
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    args = validate_tool_arguments({"name": "schema"}, {"name": "world"})
    assert args == {"name": "world"}

async def test_validate_tool_arguments_invalid():
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    with pytest.raises(ValueError):
        validate_tool_arguments({"name": "schema"}, {"wrong_key": "world"})
```

### Test 4: AbortSignal thread safety

```python
async def test_abort_signal_thread_safety():
    signal = AbortSignal()
    results = []

    def background_abort():
        time.sleep(0.01)
        signal.abort()

    threading.Thread(target=background_abort).start()
    while not signal.is_aborted():
        await asyncio.sleep(0.001)
        results.append(True)

    assert len(results) > 0
    assert signal.is_aborted()
    signal.abort()  # idempotent
    assert signal.is_aborted()
```

### Test 5: Model serialization to OpenAI format

```python
async def test_model_to_openai_format():
    model = Model(
        id="gpt-4o", name="GPT-4o", api="openai-completions",
        provider="openai", base_url="https://api.openai.com/v1",
        context_window=128000, max_tokens=4096
    )
    # Verify model attributes
    assert model.id == "gpt-4o"
    assert model.base_url == "https://api.openai.com/v1"
```

## Success Signal

All 5 test categories pass. An agent can read `tau_ai/types.py` and use every exported type without reading any other file. The types are pure Python with no external dependencies (except pydantic).
