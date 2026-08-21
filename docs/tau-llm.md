# τ-llm Design — OpenAI-compatible provider library

## Scope

`tau_llm` is the streaming/provider layer: message and tool types, a
`Provider` abstraction, and one concrete implementation
(`OpenAICompletionsProvider`) for the OpenAI Chat Completions API and
OpenAI-compatible local servers (vLLM, llama.cpp). `Provider` is written as
an abstract interface so a second provider is *possible*, but as of this
writing none exists and the abstraction has never been exercised by a
second implementation — see "Extending with a new provider" below for what
that would actually require today.

## Package layout

```
src/tau_llm/
├── types.py            # Message/Tool/Model/Usage/streaming-event types (pydantic)
├── client.py            # stream_simple() + the provider connection pool
├── streaming.py         # AssistantMessageEventStream (queue buffering + result())
├── tools.py             # define_tool(), validate_tool_arguments()
├── models.py             # clampThinkingLevel-equivalent, thinking-level plumbing
├── abort.py, constraints.py, grammar.py, json_parse.py
└── providers/
    ├── base.py          # Provider ABC, StreamEventStream Protocol
    └── openai.py        # OpenAICompletionsProvider — the only concrete provider
```

## Core types (`tau_llm/types.py`)

All pydantic `BaseModel`s. `Message = UserMessage | AssistantMessage |
ToolResultMessage`; `AssistantMessage.content` is a list of
`TextContent | ThinkingContent | ToolCall` blocks. `Usage` is frozen and
carries the expected token counts plus an `extra: dict[str, Any]` field for
server-reported, non-portable telemetry (llama.cpp's `timings` block,
τ's own tool-arg JSON-repair count).

`Model` carries more than a plain OpenAI wrapper needs, because τ talks to
local OpenAI-compatible servers as a first-class case, not an afterthought:
`reasoning: bool`, `thinking_level_map`, `reasoning_replay: Literal["all",
"turn", "off"]` (τ defaults to `"turn"`, not pi's `"all"` — see README's
"Where τ diverges from pi"), `grammar_dialect: Literal["llguidance",
"gbnf"] | None`, `extra_body: dict[str, Any]`, `server_features:
list[str]`. `Model.provider` and `AssistantMessage.provider` are typed
`Literal["openai"]` today — there is no separate `KnownProvider`/`KnownApi`
named type to extend; a second provider means adding a second literal
value at each call site.

## Provider interface (`tau_llm/providers/base.py`)

```python
class Provider(ABC):
    @abstractmethod
    async def stream_chat(
        self, model: Model, messages: list[Any],
        tools: list[ToolDefinition] | None = None,
        options: dict[str, Any] | None = None,
    ) -> StreamEventStream: ...
```

`StreamEventStream` is a structural `Protocol` (`__aiter__` only, no
`api`/`name` properties on the class). `client.py`'s `stream_simple()` is
the thin wrapper everything else calls: it resolves a provider instance,
calls `stream_chat`, and wraps the result once in
`AssistantMessageEventStream` (`streaming.py`) — the one stream type with
queue buffering and the terminal `async def result() -> AssistantMessage`.

### Provider lifecycle: a pool, not a registry

An earlier `ProviderRegistry` class existed and was deleted as dead code —
`stream_simple` built a fresh, empty registry on every single call, so
every lookup raised `KeyError`, and a new `OpenAICompletionsProvider` (and
a new `httpx.AsyncClient`) got constructed per completion: measured at
+42ms/call, no HTTP keep-alive. See `docs/PROVIDER-LIFETIME.md` for the
forensics.

The real mechanism is a connection pool in `client.py`:
`_get_or_create_provider(provider_name, base_url, api_key)` looks up or
builds a provider cached by `(provider_name, resolved_base_url,
sha256(api_key))`, held in a `WeakKeyDictionary` keyed per event loop, with
explicit teardown via `aclose_providers()` — call it on process shutdown
(the TUI, headless, and RPC entry points all do). API key resolution is not
a generic `get_default_api_key(provider)` helper; it's inlined per provider
(`OpenAICompletionsProvider.__init__` reads `OPENAI_API_KEY` directly) and
raises `No API key for provider: …` rather than falling back to a
fabricated key (Fail-Early).

## Tools (`tau_llm/tools.py`)

`define_tool(**fields)` is the constructor for a `ToolDefinition`. It takes
the eight model fields as keywords, or a single mapping positionally
(`define_tool({...})`, the shape the function originally advertised);
both forms at once, or neither, is a `TypeError`.

It validates rather than normalises — nothing malformed is patched into
something that works:

| Rule | What it prevents |
|---|---|
| the five required fields are present | a pydantic error from a call site that reads fine |
| unknown field names are rejected | `ToolDefinition` is `extra="ignore"`, so a typo'd `prompt_snipet` would be dropped in silence |
| `execute` is callable | a failure the first time the *model* calls the tool, many turns after the mistake |
| `name` matches `[A-Za-z0-9_-]{1,64}` | OpenAI rejects the whole request for one bad tool name, with a 400 that doesn't say which tool |
| `parameters` is an object schema with `properties` | `_validate_json_schema` reads `required`/`properties` regardless of `type`, so a non-object schema validates every call vacuously |
| every `required` name appears in `properties` | the model is never told about the field, so it can never send it, and every call fails forever |
| `label` and `description` are non-empty | a blank chip in the TUI, and a tool the model is given no reason to call |

`label` is **required and never derived from `name`.** Inventing it would be
exactly the fabricated default the repo's Fail-Early rule forbids, and the
failure mode is invisible: the TUI would show a developer identifier as a
human label and everything would "work". (`ExtensionAPI.register_tool()` in
`tau_agent_core` *does* default `label` to `name` — that is a separate,
pi-compatible contract, not an inconsistency to harmonise away.)

Not checked, deliberately: nested schema validity, `$ref`, and format
keywords. `validate_tool_arguments` ignores all of it at call time, so
checking it here would advertise an enforcement that does not exist — and
would reject `pydantic.model_json_schema()` output, which is the documented
way to build `parameters`.

`validate_tool_arguments(tool, tool_call)` does **not** use the
`jsonschema` package or a pydantic-model path — it's a hand-rolled
validator (`_validate_json_schema`) that checks `required` fields and does
ad-hoc type-checking per JSON Schema primitive type, raising `ValueError`
with a collected error message on failure. A schema keyword it does not
know (`minLength`, `enum`, `pattern`) looks enforced and is silently
ignored; that is a known limitation, not something `define_tool` closes.

**`define_tool`'s output is not accepted by `tau_agent_core.AgentTool`.**
`tau_agent_core.tools.base` defines its own structurally identical
`ToolDefinition`, and pydantic rejects a foreign model instance
(`Input should be a valid dictionary or instance of ToolDefinition`).
`AgentTool(definition=tool.model_dump())` bridges them. Extensions are a
third shape again — `api.register_tool()` takes a plain dict whose
`execute` is `execute(tool_call_id, params, signal, on_update, ctx)` (see
`docs/extensions.md`).

## Extending with a new provider

There is no `register_provider()` call to make — that function doesn't
exist. Adding a second provider today means:

1. Implement `Provider.stream_chat` in a new `tau_llm/providers/<name>.py`.
2. Wire it into `client.py`'s `_get_or_create_provider`/`_pool_key`, which
   currently only knows how to build an `OpenAICompletionsProvider`.
3. Extend the `Literal["openai"]` annotations on `Model.provider` and
   `AssistantMessage.provider` in `types.py`.

That's real, scoped work inside `tau_llm`, not a drop-in extension point —
correcting an earlier claim that no core changes were needed.

## Dependencies

`openai` SDK, `pydantic>=2.0`. Standard library otherwise (asyncio, json,
time).
