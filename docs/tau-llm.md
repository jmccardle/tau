# τ-llm Design — the provider layer

## Scope

`tau_llm` is the streaming/provider layer: message and tool types, a
`Provider` abstraction, and three concrete implementations — one per wire
protocol τ speaks.

| `Model.api` | Client | Built on | Vendor τ registers |
|---|---|---|---|
| `openai-completions` | `OpenAICompletionsProvider` | `httpx` directly | `openai` |
| `anthropic-messages` | `AnthropicMessagesProvider` | the `anthropic` SDK | `anthropic` |
| `google-generative-ai` | `GoogleGenerativeAIProvider` | the `google-genai` SDK | `gemini` |

The two vendor SDKs are optional extras (`tau-llm[anthropic]`,
`tau-llm[google]`) imported lazily on first request, so `import tau_llm` works
without either and the whole test suite passes with both imports blocked.

The OpenAI path also serves OpenAI-compatible local servers (vLLM,
llama.cpp) as a first-class case rather than an afterthought.

## Package layout

```
src/tau_llm/
├── types.py             # Message/Tool/Model/Usage types (pydantic)
├── client.py            # stream_simple() + the provider connection pool
├── streaming.py         # streaming events + AssistantMessageEventStream
├── tools.py             # define_tool(), validate_tool_arguments()
├── models.py            # clamp_thinking_level, thinking-level plumbing
├── compat.py            # the two fields detected from a base URL
├── catalog.py           # `python -m tau_llm.catalog` — models.dev lookup
├── abort.py, constraints.py, grammar.py, json_parse.py
└── providers/
    ├── base.py          # Provider ABC + the api and vendor registries
    ├── __init__.py      # τ's own register_api / register_provider calls
    ├── openai.py        # openai-completions
    ├── anthropic.py     # anthropic-messages
    └── google.py        # google-generative-ai
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
"gbnf"] | None`, `extra_body: dict[str, Any]`, `server_features: list[str]`,
`stream: bool`, `request_timeout: float | None`, `temperature: float | None`,
`strict_reasoning_formats: bool`, `requires_tool_call_id: bool`,
`supports_multimodal_function_response: bool`, and `compat: Compat | None`.

`temperature` defaults to `None`, and `None` means τ sends no temperature at
all — the endpoint applies its own. This is pi's position
(`simple-options.ts:32` forwards `options?.temperature`, which is undefined
unless a caller sets one) and it is the only correct default across the three
endpoint families τ speaks to: llama.cpp defaults to 0.8, the OpenAI wire to
1.0, and the Anthropic Messages API removed the parameter outright on Opus 5,
Opus 4.8, Opus 4.7, Sonnet 5 and Fable 5, where any value is a 400.

`Model.api`, `AssistantMessage.api` and `AssistantMessage.provider` are `str`.
They were `Literal["openai"]` until `6e1dfbe`, which was a latent defect rather
than a constraint: `streaming.py` copies the Model's vendor onto the message,
so a legal `Model` naming anyone else raised `ValidationError` there.

A `ToolCall` and a `ThinkingContent` each carry a `provider_signature: dict`,
namespaced by vendor. See "Reasoning signatures" below.

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

`StreamEventStream` is a structural `Protocol` (`__aiter__` only). There is no
non-streaming entry point on the interface: `Model.stream = False` changes how
τ talks to the server, not what the caller sees, because the buffered path
fills the same accumulator and yields the same event sequence.

`client.py`'s `stream_simple()` is the thin wrapper everything else calls: it
resolves a provider instance, calls `stream_chat`, and wraps the result once
in `AssistantMessageEventStream` (`streaming.py`) — the one stream type with
queue buffering and the terminal `async def result() -> AssistantMessage`.

### Two registries, and a pool on top

Three questions, deliberately answered in three places.

* **Which client class serves this `Model.api`?** The *api registry* —
  `register_api(api, factory)` in `providers/base.py`. An unregistered `api`
  raises, naming what was asked for and what is registered; it does not fall
  back to the OpenAI class, because that fallback was the bug that let a model
  declaring `openai-responses` be served over the completions wire in silence.
* **Where does this `Model.provider` point, and which environment variables
  hold its key?** The *vendor registry* — `register_provider(ProviderSpec(...))`.
  τ registers one vendor per protocol it implements and ships no model
  catalogs; anyone else's vendor is six lines in their own code at import time.
* **Which client INSTANCE serves this call?** The pool in `client.py`, keyed on
  `(provider_id, api, base_url, sha256(api_key))` and held in a
  `WeakKeyDictionary` per event loop. `api` is in the key because two apis are
  two classes. The credential is hashed, never stored raw, including the
  absent case. Teardown is `aclose_providers()`; the TUI, headless and RPC
  entry points all call it.

An earlier `ProviderRegistry` class was deleted as dead code — `stream_simple`
built a fresh, empty one on every call, so every lookup raised `KeyError` and a
new provider and `httpx.AsyncClient` got constructed per completion: +42ms per
call, no HTTP keep-alive. `docs/PROVIDER-LIFETIME.md` has the forensics. The
registries above are the opposite case: `client.py` cannot construct a provider
without them.

A missing key raises `No API key for provider: …` rather than falling back to a
fabricated one (Fail Early).

### Reasoning signatures

Anthropic's thinking blocks and Gemini 3's function calls both carry a
signature the vendor VALIDATES on replay, so both are namespaced by vendor and
the OpenAI writer refuses a foreign one — raising under
`Model.strict_reasoning_formats`, otherwise warning once per payload shape and
dropping the token while the tool call replays intact.

The rule that is easy to get backwards: a **functionCall** signature is
replayed on every `reasoning_replay` setting including `"off"`, because it is
protocol rather than chain-of-thought and Gemini 3 answers 400 without it.
Signatures on **text and thinking** parts do follow the knob. Full argument:
`docs/ANTHROPIC-GOOGLE-CLIENTS.md` O4.

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

## Extending

**An OpenAI-compatible vendor needs no τ change at all** — no fork, no new
file, no core edit. Register a spec at import time in your own code:

```python
from tau_llm.providers import ProviderSpec, register_provider

register_provider(ProviderSpec(
    id="groq", name="Groq",
    api="openai-completions",
    base_url="https://api.groq.com/openai/v1",
    api_key_env=("GROQ_API_KEY",),
))
```

then point a model at it with `provider="groq"`. This is pi's largest provider
bucket — roughly 25 of its 39 providers are thin configurations over one
OpenAI-compatible client.

**A genuinely new wire protocol** is a new `tau_llm/providers/<name>.py`
implementing `Provider.stream_chat`, plus a `register_api` call. Nothing in
`client.py` or `types.py` changes: dispatch already reads `model.api` through
the registry, and the type annotations are already `str`. The Anthropic and
Google clients were both added this way.

## Dependencies

`pydantic>=2.0` and `httpx>=0.27`. Standard library otherwise. The `anthropic`
and `google-genai` SDKs are optional extras, imported lazily on first request
by their own provider modules — nothing at module scope pulls them in.
