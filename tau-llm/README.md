# ffwf-tau-llm

The provider layer of **Tau**, a programmable coding agent harness. `tau_llm`
speaks to OpenAI-compatible chat endpoints and turns the response stream into
typed events. It knows nothing about agents.

Tau began as a Python port of the TypeScript project
[pi-mono](https://github.com/badlogic/pi-mono), which is still read as the
reference implementation when porting or debugging; it now diverges from pi
deliberately in several places.

## What is in it

- **Message and tool types** — `UserMessage`, `AssistantMessage`,
  `ToolResultMessage`, and the content blocks they carry (`TextContent`,
  `ThinkingContent`, `ImageContent`, `ToolCall`). All pydantic models.
- **`Model`** — one endpoint's configuration. The usual fields (`id`,
  `provider`, `base_url`, `context_window`, `max_tokens`) plus the τ-specific
  ones: `reasoning`, `thinking_level_map`, `reasoning_replay`,
  `grammar_dialect`, `extra_body`, `server_features`.
- **A streaming client** — `stream_simple()` returns an
  `AssistantMessageEventStream` you can iterate for deltas and `await` for the
  final message. `complete_simple()` is the non-streaming spelling of the same
  call.
- **Constrained decoding** — `DecodeConstraints` carries a grammar, a JSON
  Schema, or a list of choices; the `grammar` helpers build one.
- **`AbortSignal`** — cooperative cancellation for an in-flight completion.

## Why it is a separate package

The dependencies are `pydantic` and `httpx`, and that is the whole list. The
`openai` SDK is deliberately not among them: τ talks to the `/chat/completions`
wire format directly, which is what makes a local OpenAI-compatible server
(vLLM, llama.cpp, Ollama) a first-class case rather than an afterthought.

Install it alone if you want the streaming client and the message types without
an agent loop attached.

## Install

```bash
pip install ffwf-tau-llm
```

Python 3.11 or newer. Note the `ffwf-` prefix: `tau-llm` on PyPI is an unrelated
project.

## Example

```python
import asyncio
from tau_llm import Model, stream_simple

model = Model(
    id="gpt-4o",
    name="gpt-4o",
    api="openai-completions",
    provider="openai",
    base_url="https://api.openai.com/v1",
    context_window=128000,
    max_tokens=4096,
)


async def main():
    stream = await stream_simple(
        model,
        {"messages": [{"role": "user", "content": "Say hello."}]},
        {"api_key": "sk-..."},
    )
    async for event in stream:
        if event.type == "text_delta":
            print(event.delta, end="", flush=True)
    final = await stream.result()
    print("\n", final.usage)


asyncio.run(main())
```

Point `base_url` at `http://localhost:8080/v1` and the same code runs against a
local llama.cpp.

**A missing API key raises, it does not improvise.** `stream_simple` fails with
`No API key for provider: …` rather than sending a fabricated one. That is the
house rule throughout Tau: refuse loudly instead of producing a plausible wrong
answer.

## Streaming events

`tau_llm.streaming` defines what the stream yields: `TextDeltaEvent`,
`ThinkingDeltaEvent`, `ToolCallDeltaEvent`, `DoneEvent`, `ErrorEvent`.

The terminal `DoneEvent.final` is the authoritative `AssistantMessage` — its
`ToolCall` blocks carry the fully accumulated `arguments`. OpenAI streams tool
call arguments as incremental fragments, one piece per chunk, which the provider
concatenates; a consumer that reads any single delta as a complete payload will
corrupt the JSON.

## Providers

**Providers are pooled, not registered.** `Provider` (in
`tau_llm.providers.base`) is an abstract interface with a single concrete
implementation today: `OpenAICompletionsProvider`, covering the OpenAI Chat
Completions API and OpenAI-compatible servers. There is no registry;
`stream_simple()` resolves and caches provider instances itself, keyed on
provider name, base URL, and a hash of the API key — so a second model on a
different endpoint can never be served by the first model's provider.

### Endpoint quirks (`tau_llm.compat`)

"OpenAI-compatible" is a family, not a specification. Two of the fields its
members disagree about cannot be reached through `Model.extra_body`, so they
live on `Model.compat`:

- `max_tokens_field` — OpenAI's o-series and gpt-5 family reject `max_tokens`
  and want `max_completion_tokens`; llama.cpp, vLLM and the classic Chat
  Completions API want `max_tokens`.
- `supports_usage_in_streaming` — whether `stream_options` may be sent.

Both are inferred from the base URL when unset (`detect_compat`), and a stated
field wins field by field (`resolve_compat`). Detection is deliberately narrow:
an unrecognised endpoint keeps `max_tokens`, because an unrecognised endpoint is
far more often a local server than a proxy in front of OpenAI. `Model.provider`
is not consulted — τ's config seam defaults it to `"openai"` for any entry that
names no backend, so it usually means "unstated".

This is adapted from pi's `detectCompat`/`getCompat`, cut to the fields that
have a live consumer. `tau_llm.compat`'s module docstring lists all 24 pi fields
that did not port, and which τ field already says each one.

## Model facts (`tau_llm.catalog`)

τ ships no model catalog. `python -m tau_llm.catalog` reads one on demand from
[models.dev](https://models.dev) and prints a `~/.tau/config.json` entry:

```bash
python -m tau_llm.catalog providers                  # provider ids and their env vars
python -m tau_llm.catalog search kimi                # provider/model pairs
python -m tau_llm.catalog show openai/gpt-5.1        # the raw catalog record
python -m tau_llm.catalog config openai/gpt-5.1 \
    --base-url https://api.openai.com/v1             # a config entry, on stdout
```

It fills `context_window`, `max_tokens`, `reasoning` and `thinking_level_map`.
It refuses rather than guessing: a record with no context limit, or a model the
catalog marks as unable to call tools, raises `CatalogError` instead of
producing an entry with an invented number in it.

`--base-url` is required, because models.dev carries provider `id`, `name`,
`doc`, `npm` and `env` but **no endpoint** — one model id is served by many
gateways. `--catalog api.json` reads a local copy instead of fetching.

## Tools

Build a tool with `define_tool()`, which returns a validated `ToolDefinition`:

```python
from tau_llm import define_tool

word_count = define_tool(
    name="word_count",
    label="Word count",
    description="Count the words in a string.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    execute=lambda text: {"words": len(text.split())},
)
```

A single mapping may also be passed positionally — `define_tool({...})`.
Passing both forms, or neither, is an error. `define_tool` raises rather than
patching a malformed definition into a working one: `label` is required and is
never derived from `name`, an unknown field is rejected instead of being
dropped, `execute` must be callable, `name` must be usable on the wire
(`[A-Za-z0-9_-]{1,64}`), and `parameters` must be a JSON Schema *object* schema
with a `properties` key — a tool that takes no arguments writes
`{"type": "object", "properties": {}}`.

Tool argument validation at call time is hand-rolled against that schema
(`validate_tool_arguments`). It checks only top-level `type` and `required`, so
a keyword like `minLength` is accepted and then silently unenforced.

`define_tool` is **not** the shape `tau_agent_core`'s
`ExtensionAPI.register_tool()` takes — that one is a plain dict whose `execute`
has the five-argument extension signature. See `docs/extensions.md`.

## Docs

- `docs/tau-llm.md` — design notes for this package.
- `docs/TOOL-CALL-PIPELINE.md` — how a tool call travels from HTTP bytes to a
  rendered widget.
- `docs/REASONING-VS-CONSTRAINED-DECODING.md` — why τ disables thinking on
  constrained calls.

## Credits

Two MIT-licensed projects, neither a dependency — both read and ported, so the
attribution is here rather than in a lock file.

- **[pi-mono](https://github.com/badlogic/pi-mono)**, Copyright (c) 2025 Mario
  Zechner. The implementation this package was ported from: the streaming event
  vocabulary, the OpenAI-completions provider, thinking-level clamping, and
  `tau_llm.compat` (pi's `detectCompat`/`getCompat`).
- **[models.dev](https://github.com/sst/models.dev)**, the SST project. The
  model facts `tau_llm.catalog` reads, served from
  <https://models.dev/api.json>. Fetched when asked for; never vendored here.

Repository: <https://github.com/jmccardle/tau>

## The rest of Tau

| Distribution | Imports as | What it adds |
|---|---|---|
| `ffwf-tau-agent-core` | `tau_agent_core` | the agent loop, tools, sessions, extensions |
| `ffwf-tau-coding-agent` | `tau_coding_agent` | the `tau` command and the Textual TUI |
| `ffwf-tau-jmfts` | `tau_jmfts` | a JMFTS-backed session store |

MIT © Fight Fire with Fire Robotics, LLC
