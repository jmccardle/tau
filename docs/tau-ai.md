# τ-ai Design — OpenAI-First Provider Library

## Scope

τ-ai is a thin, well-tested provider library. For the MVP, it supports only OpenAI (completions + responses APIs). The architecture is designed for easy addition of Anthropic, Google, and other providers later.

## Design Goals

1. **OpenAI-first, not OpenAI-only** — Provider interface is generic; OpenAI is the first implementation
2. **Event-driven streaming** — Same protocol regardless of provider
3. **Tool calling as first-class** — Structured tool calls in the stream
4. **Minimal dependencies** — Only `openai` SDK, `pydantic`, and standard library

## Package: `tau_ai`

```
src/tau_ai/
├── __init__.py
├── types.py           # Core data types (shared across providers)
├── client.py          # AsyncOpenAI wrapper
├── streaming.py       # Event stream protocol
├── tools.py           # Tool definitions, parameter validation
├── providers/
│   ├── __init__.py
│   ├── base.py        # Abstract Provider class
│   ├── openai.py      # OpenAI Completions API provider
│   ├── openai_responses.py
│   └── registry.py    # Provider registration
└── utils/
    ├── overflow.py    # Context window overflow detection
    └── validation.py  # Tool argument validation
```

## Core Types (`tau_ai/types.py`)

```python
# API and Provider identifiers (mirrors pi's KnownApi / KnownProvider)
KnownApi = Literal["openai-completions", "openai-responses"]
KnownProvider = Literal["openai"]

class Api:
    """API type identifier for a provider."""
    pass

class Provider:
    """Provider identifier (e.g., 'openai', 'anthropic')."""
    pass

# --- Message Types ---

class TextContent(BaseModel):
    type: Literal["text"] = "text"
    text: str

class ThinkingContent(BaseModel):
    type: Literal["thinking"] = "thinking"
    thinking: str

class ImageContent(BaseModel):
    type: Literal["image"] = "image"
    data: str  # base64
    mime_type: str

class ToolCall(BaseModel):
    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, Any]

class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost: dict[str, float] = {}

class UserMessage(BaseModel):
    role: Literal["user"] = "user"
    content: str | list[TextContent | ImageContent]
    timestamp: int

class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: list[TextContent | ThinkingContent | ToolCall]
    api: KnownApi
    provider: KnownProvider
    model: str
    response_id: str | None = None
    usage: Usage
    stop_reason: StopReason
    error_message: str | None = None
    timestamp: int

class ToolResultMessage(BaseModel):
    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str
    tool_name: str
    content: list[TextContent | ImageContent]
    details: dict[str, Any] | None = None
    is_error: bool
    timestamp: int

Message = UserMessage | AssistantMessage | ToolResultMessage

# --- Tool Types ---

class Tool(BaseModel):
    """Tool definition for the LLM API."""
    name: str
    description: str
    parameters: dict  # JSON Schema (OpenAI format)

class AgentTool(Generic[T]):
    """Tool definition for the agent loop (wraps LLM Tool)."""
    name: str
    label: str  # Human-readable
    description: str
    parameters: TSchema  # pydantic model or JSON Schema
    execute: Callable[[str, object, AbortSignal | None, UpdateCallback | None], Coroutine[Any, Any, AgentToolResult]]
    execution_mode: Literal["sequential", "parallel"] = "parallel"

class AgentToolResult(BaseModel):
    content: list[TextContent | ImageContent]
    details: dict[str, Any]
    terminate: bool = False

# --- Provider Types ---

class Model(BaseModel):
    id: str
    name: str
    api: KnownApi
    provider: KnownProvider
    base_url: str
    reasoning: bool = False
    input: list[Literal["text", "image"]]
    cost: dict[str, float] = {}
    context_window: int
    max_tokens: int

class Context(BaseModel):
    system_prompt: str | None = None
    messages: list[Message]
    tools: list[Tool] | None = None

# --- Streaming Protocol ---

StopReason = Literal["stop", "length", "toolUse", "error", "aborted"]

AssistantMessageEvent = Union[
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallDeltaEvent,
    DoneEvent,
    ErrorEvent,
]

class AssistantMessageEventStream:
    """Async iterator yielding AssistantMessageEvents."""
    def __aiter__(self) -> AsyncIterator[AssistantMessageEvent]: ...
    async def result(self) -> AssistantMessage:
        """Return the final assistant message after stream completes."""
        ...
```

## Provider Interface (`tau_ai/providers/base.py`)

```python
class Provider(ABC):
    """Abstract provider interface. Each provider implements this."""

    @abstractmethod
    async def stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions,
    ) -> AssistantMessageEventStream:
        """Stream an assistant response."""

    @abstractmethod
    async def stream_simple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions,
    ) -> AssistantMessageEventStream:
        """Stream with simplified options (no provider-specific settings)."""

    @property
    @abstractmethod
    def api(self) -> KnownApi:
        """Which API this provider implements."""

    @property
    @abstractmethod
    def name(self) -> KnownProvider:
        """Provider name identifier."""
```

## OpenAI Provider (`tau_ai/providers/openai.py`)

```python
class OpenAICompletionsProvider(Provider):
    """OpenAI Chat Completions API provider.

    This handles:
    - Message conversion (τ types → OpenAI format)
    - Tool calling (parallel tool calls, function format)
    - Thinking/reasoning (mapped to `reasoning_effort`)
    - Streaming deltas (text, tool calls)
    - Token usage tracking
    """

    api = "openai-completions"
    name = "openai"

    def __init__(self, client: AsyncOpenAI):
        self._client = client

    async def stream(self, model, context, options):
        # 1. Convert τ messages to OpenAI format
        openai_messages = self._convert_messages(context.messages, context.system_prompt)

        # 2. Convert τ tools to OpenAI format
        openai_tools = [self._convert_tool(t) for t in context.tools] if context.tools else None

        # 3. Build completion request
        kwargs = {
            "model": model.id,
            "messages": openai_messages,
            "tools": openai_tools,
            "stream": True,
            "max_tokens": options.max_tokens,
            "temperature": options.temperature,
            "stream_options": {"include_usage": True},
        }

        # 4. Handle reasoning
        if model.reasoning and options.reasoning:
            kwargs["reasoning_effort"] = self._map_reasoning_level(options.reasoning)

        # 5. Stream response
        stream = await self._client.chat.completions.create(**kwargs)

        # 6. Yield events
        async for event in self._stream_events(stream, model, context):
            yield event

    def _convert_messages(self, messages, system_prompt):
        """Convert τ Message[] to OpenAI message format."""
        openai_msgs = []
        if system_prompt:
            openai_msgs.append({"role": "system", "content": system_prompt})

        for msg in messages:
            if isinstance(msg, UserMessage):
                openai_msgs.append({"role": "user", "content": msg.content})
            elif isinstance(msg, AssistantMessage):
                openai_msgs.append(self._assistant_to_openai(msg))
            elif isinstance(msg, ToolResultMessage):
                openai_msgs.append(self._tool_result_to_openai(msg))

        return openai_msgs

    def _assistant_to_openai(self, msg):
        """Convert τ assistant message to OpenAI format."""
        content_parts = []
        tool_calls = []

        for part in msg.content:
            if isinstance(part, TextContent):
                content_parts.append(part.text)
            elif isinstance(part, ThinkingContent):
                # OpenAI doesn't have thinking blocks — store as text
                # with a prefix so τ can detect them on receive
                content_parts.append(f"[THINKING]\n{part.thinking}\n[/THINKING]")
            elif isinstance(part, ToolCall):
                tool_calls.append({
                    "id": part.id,
                    "type": "function",
                    "function": {
                        "name": part.name,
                        "arguments": json.dumps(part.arguments),
                    },
                })

        result = {"role": "assistant"}
        if content_parts:
            result["content"] = "\n".join(content_parts)
        if tool_calls:
            result["tool_calls"] = tool_calls
        return result

    def _tool_result_to_openai(self, msg):
        """Convert τ tool result to OpenAI format."""
        text_parts = [
            c.text for c in msg.content if isinstance(c, TextContent)
        ]
        return {
            "role": "tool",
            "tool_call_id": msg.tool_call_id,
            "content": "\n".join(text_parts) if text_parts else "",
        }

    async def _stream_events(self, stream, model, context):
        """Convert OpenAI streaming chunks to τ events."""
        partial = AssistantMessage(
            role="assistant",
            content=[],
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage=Usage(),
            stop_reason="stop",
            timestamp=int(time.time() * 1000),
        )

        for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta

                # Text delta
                if delta.content:
                    yield TextDeltaEvent(
                        type="text_delta",
                        content_index=0,
                        delta=delta.content,
                        partial=partial,
                    )
                    partial.content[0] = TextContent(text=partial.content[0].text + delta.content)

                # Tool call delta
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        yield ToolCallDeltaEvent(...)
                        # Accumulate tool call arguments

        # Yield done event with final message
        yield DoneEvent(type="done", reason=..., message=partial)
```

## Provider Registry (`tau_ai/providers/registry.py`)

```python
_provider_registry: dict[str, Provider] = {}

def register_provider(provider: Provider) -> None:
    _provider_registry[provider.api] = provider

def get_provider(api: str) -> Provider | None:
    return _provider_registry.get(api)

def get_default_api_key(provider: str) -> str | None:
    """Get API key from env vars or config."""
    env_key = os.environ.get(f"{provider.upper()}_API_KEY")
    if env_key:
        return env_key
    return None
```

## Parameter Validation (`tau_ai/tools.py`)

```python
def validate_tool_arguments(tool: AgentTool, tool_call: ToolCall) -> object:
    """Validate tool call arguments against tool schema.

    Returns validated parameters or raises ValueError.
    Uses pydantic for validation.
    """
    schema = tool.parameters
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_validate(tool_call.arguments)
    elif isinstance(schema, dict):
        # Fallback: JSON Schema validation via jsonschema package
        try:
            import jsonschema
            jsonschema.validate(tool_call.arguments, schema)
            return tool_call.arguments
        except jsonschema.ValidationError as e:
            raise ValueError(str(e)) from e
    return tool_call.arguments
```

## Dependencies

- `openai >= 1.12.0`
- `pydantic >= 2.0`
- Standard library only (asyncio, json, time, dataclasses)

## Future Provider Additions

To add a new provider (e.g., Anthropic):

1. Create `tau_ai/providers/anthropic.py`
2. Implement `Provider` interface
3. Register with `register_provider(AnthropicProvider())`
4. Add provider identifier to `KnownProvider`

No changes needed in τ-agent-core or τ-coding-agent.
