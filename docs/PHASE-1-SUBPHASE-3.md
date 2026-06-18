# Phase 1 Subphase 3 — Streaming Protocol and Client

> **Topic**: Implement the streaming event protocol (`AssistantMessageEventStream`) and the `stream_simple()` client function.

## Scope

This subphase connects the provider's raw OpenAI streaming to τ's typed event protocol. It produces:
1. `AssistantMessageEventStream` — async iterator that yields typed `StreamEvent` objects
2. `stream_simple()` — the convenience function that τ-agent-core calls

## Reference

- `SUBPHASE-0.0.md` lines 180-210: streaming event protocol
- `docs/tau-ai.md` lines 120-180: streaming design
- `docs/tau-agent-core.md` lines 160-220: how agent loop consumes streaming
- pi's `pi-ai/dist/streaming/assistant-message-event-stream.js` (reference)

## Implementation Outline

### `tau_ai/streaming.py`

```python
class AssistantMessageEventStream:
    """Async iterator over streaming events from the LLM.

    Yields: TextDeltaEvent, ToolCallDeltaEvent, DoneEvent, ErrorEvent

    Usage:
        stream = await stream_simple(model, context, options)
        async for event in stream:
            if event.type == "text_delta":
                print(event.delta, end="")
            elif event.type == "toolcall_delta":
                print(f"\nTool: {event.tool_call.name}")
        final = await stream.result()
    """

    def __init__(self, provider_stream, model, context):
        self._provider_stream = provider_stream
        self._model = model
        self._context = context
        self._partial = AssistantMessage(content=[])
        self._done = False
        self._final = None
        self._usage = Usage(input_tokens=0, output_tokens=0, total_tokens=0)
        self._error = None
        self._event_queue = asyncio.Queue()
        self._collector_task = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._done and self._event_queue.empty():
            raise StopAsyncIteration
        event = await self._event_queue.get()
        return event

    async def _collect(self):
        """Background coroutine that processes provider stream."""
        try:
            async for chunk in self._provider_stream:
                await self._process_chunk(chunk)
            # Emit done event
            self._final = self._partial
            await self._event_queue.put(DoneEvent(
                type="done",
                final=self._final,
                usage=self._usage,
            ))
        except Exception as e:
            await self._event_queue.put(ErrorEvent(
                type="error",
                message=str(e),
                is_error=True,
            ))

    async def _process_chunk(self, chunk: dict):
        """Process a single OpenAPI streaming chunk."""
        delta = chunk.get("delta", {})

        if "content" in delta and delta["content"]:
            await self._event_queue.put(TextDeltaEvent(
                type="text_delta",
                delta=delta["content"],
                partial=self._partial,
            ))
            self._partial.content.append(TextContent(text=delta["content"]))

        if "tool_calls" in delta:
            for tc_delta in delta["tool_calls"]:
                idx = tc_delta.get("index", 0)
                await self._handle_tool_call_delta(tc_delta, idx)

    async def _handle_tool_call_delta(self, delta, index):
        """Handle a tool call delta, accumulating arguments."""
        ...  # accumulate into ToolCall objects on _partial

    async def result(self) -> AssistantMessage:
        """Wait for the stream to complete and return the final message."""
        if not self._done:
            await self._wait_for_done()
        if self._error:
            raise Exception(self._error)
        return self._final

    async def _wait_for_done(self):
        """Wait for the DoneEvent."""
        while not self._done:
            event = await self._event_queue.get()
            if event.type == "done":
                self._done = True
                self._final = event.final
                self._usage = event.usage
                return

    def abort(self):
        """Abort the stream."""
        if hasattr(self._provider_stream, 'abort'):
            self._provider_stream.abort()
```

### `tau_ai/client.py`

```python
async def stream_simple(
    model: Model,
    context: dict,  # {"system_prompt": str, "messages": list[Message], "tools": list[ToolDefinition] | None}
    options: dict | None = None,  # {"api_key": str, "reasoning": str, "max_tokens": int, "signal": AbortSignal}
) -> AssistantMessageEventStream:
    """Simple streaming client for the agent loop.

    This is the ONLY entry point that τ-agent-core uses to talk to τ-ai.
    """
    provider_name = model.provider
    registry = get_registry()
    provider = registry.get(provider_name)

    messages = context.get("messages", [])
    tools = context.get("tools", None)

    stream = await provider.stream_chat(
        model=model,
        messages=messages,
        tools=tools,
        options=options,
    )

    return AssistantMessageEventStream(
        provider_stream=stream,
        model=model,
        context=context,
    )
```

### Provider ABC

```python
from abc import ABC, abstractmethod

class Provider(ABC):
    """Abstract provider interface."""

    @abstractmethod
    async def stream_chat(
        self,
        model: Model,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        options: dict | None = None,
    ) -> AssistantMessageEventStream:
        """Stream a chat completion. Returns an async iterable."""
        ...
```

### Provider Registry

```python
class Registry:
    """Singleton registry of providers."""
    _instance = None
    _providers: dict[str, Provider] = {}

    def register(self, name: str, provider: Provider):
        self._providers[name] = provider

    def get(self, name: str) -> Provider:
        return self._providers[name]

    def list_all(self) -> list[str]:
        return list(self._providers.keys())
```

## Done Criteria

- `stream_simple()` is callable and returns an `AssistantMessageEventStream`
- The stream yields events in the correct order:
  0+ `TextDeltaEvent`s
  0+ `ToolCallDeltaEvent`s (interleaved with text deltas)
  1 `DoneEvent` (always)
- `await stream.result()` returns the fully accumulated `AssistantMessage`
- `await stream.result()` raises on `ErrorEvent`
- The `_collect` background task runs independently of the iterator
- Token usage is tracked (input/output/total)
- `abort()` propagates to the underlying provider

### Event Ordering Guarantee

For a response that includes text and a tool call:
```
TextDeltaEvent("Let me ")
TextDeltaEvent("check")
ToolCallDeltaEvent(id="call_1", name="bash", delta="")
ToolCallDeltaEvent(id="call_1", name="", delta="{'command':")
ToolCallDeltaEvent(id="call_1", name="", delta="'ls'}")
DoneEvent(final=AssistantMessage(content=[TextContent(...), ToolCall(...)]), usage=...)
```

For a pure text response:
```
TextDeltaEvent("Hello")
DoneEvent(final=AssistantMessage(content=[TextContent(text="Hello")]), usage=...)
```

For an error:
```
ErrorEvent(type="error", message="Invalid API key", is_error=True)
```

## Testing Strategy

### Test 1: Stream simple returns event stream

```python
async def test_stream_simple_returns_stream(mock_openai, mock_provider):
    stream = await stream_simple(
        model=Model(id="gpt-4o", ...),
        context={"messages": [UserMessage(content=[TextContent(text="hi")])]},
    )
    assert isinstance(stream, AssistantMessageEventStream)
```

### Test 2: Text-only stream produces correct events

```python
async def test_text_only_stream(mock_openai, mock_provider):
    # mock_provider returns: [{"delta": {"content": "Hello"}}] then finish_reason="stop"
    stream = await stream_simple(...)
    events = [e async for e in stream]

    assert len(events) == 2  # 1 text_delta + 1 done
    assert events[0].type == "text_delta"
    assert events[0].delta == "Hello"
    assert events[1].type == "done"
    assert events[1].final.content[0].text == "Hello"
    assert events[1].usage.total_tokens > 0
```

### Test 3: Tool call stream accumulates arguments

```python
async def test_tool_call_stream_accumulates(mock_openai, mock_provider):
    # mock_provider returns tool call deltas with partial arguments
    stream = await stream_simple(...)
    final = await stream.result()

    tool_calls = [c for c in final.content if hasattr(c, 'type') and c.type == "toolCall"]
    assert len(tool_calls) == 1
    assert tool_calls[0].arguments == '{"command":"ls"}'
```

### Test 4: Error event on API error

```python
async def test_error_event(mock_openai, mock_provider):
    mock_provider.stream_chat.side_effect = Exception("API Error")
    stream = await stream_simple(...)
    events = [e async for e in stream]
    assert any(e.type == "error" for e in events)
```

### Test 5: stream.result() blocks until done

```python
async def test_result_blocks_until_done(mock_openai, mock_provider):
    stream = await stream_simple(...)
    # Start iterating
    task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0.1)  # let it start

    # result() should block until the stream completes
    final = await stream.result()
    assert isinstance(final, AssistantMessage)
```

### Test 6: Abort propagates

```python
async def test_abort_propagates(mock_openai, mock_provider):
    stream = await stream_simple(...)
    stream.abort()
    assert stream._partial is not None  # partial state preserved
```

## Success Signal

The streaming protocol works end-to-end: `stream_simple()` → provider → event stream → typed events. An agent can use the stream pattern:
```python
stream = await stream_simple(model, context, options)
async for event in stream:
    ...
final = await stream.result()
```
And it works for all three response types (text, tool calls, error). The `_collect` background task ensures the iterator is non-blocking.
