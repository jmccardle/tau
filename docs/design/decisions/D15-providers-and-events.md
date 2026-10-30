# D15: Providers own their connections; frontends get saved and live events

Status: candidate (2026-10-29): the first slice is built (provider contract, settings,
live and saved events, mock provider); replay data, the OpenAI provider's own use of
the contract, and tool progress are not. Amends `model.md` §9 and §10.

"Live" replaces the first draft's "draft" event: `draft` already means the hook draft
(glossary).

## Context

One provider exists (OpenAI-compatible). A review for a second one found:

- the `provider` key is checked against a fixed list and then ignored: nothing
  picks an implementation by name;
- a part has nowhere to keep data a provider must get back byte for byte
  (Anthropic `thinking.signature`, `redacted_thinking`, Gemini
  `thoughtSignature` on text and calls);
- `ModelConfig` has no `max_tokens` (Anthropic requires it) and no place for
  settings only one provider reads;
- a new HTTP client per sample: every hosted call pays a TLS handshake;
- `SampleError` has no status, so a retry wrapper cannot tell what is worth
  retrying;
- `model.md` §10.1 says parts stream as `stream_delta`s; the code yields only
  whole parts, so a frontend sees nothing until a part is done.

Frontends need text from two places: the core (what was saved, where a cursor
moved) and the provider and tools (a reply streaming in, a tool's progress).

## Decision

**A provider is a long-lived object that owns its connections.**

```
Provider.name                        # what a model's `provider` setting calls it
Provider.check(model) -> problems    # its own settings; `tau config check` calls it
Provider.sample(walk, model, tools)  # async stream: live events and parts, then Finish
Provider.aclose()                    # closes its connections; the builder calls it
```

How it pools connections, retries, or authenticates is its own business.

**Settings.** A model has shared settings and provider options:

- shared: `provider`, `model`, `base_url` (optional; proxies make it useful to
  every provider, and one that has no use for it refuses it in `check`),
  `api_key` / `api_key_env`, `max_tokens`;
- `options`: an object only the named provider reads (thinking budget,
  `cache_control`, `cache_prompt`, safety settings). Layers merge it like any
  object. The config loader never looks inside; the provider's `check` does,
  so a misspelt option fails at `tau config check`.

**Selection.** The driver is given a map of provider name to provider. The
config check accepts exactly the names in that map. A provider refuses a model
whose `provider` is not its own.

**What comes back is shared.**

- Parts: `Text`, `Thinking`, `Call`, each with an optional `replay` slot:
  opaque JSON tagged with the provider name that wrote it. A provider sends a
  part's replay data back only when the tag is its own; otherwise it treats the
  part as it would an unsigned one (`model.md` §10.4).
- `Finish(stop, reason, meta)`: `meta` gains a normalized `usage` (input,
  output, cached tokens) beside the provider's raw fields.
- `SampleError(message, meta, status=None, retry_after=None)`. Retries are a
  wrapper provider, never core (cursor never spins).
- The walk-to-messages rules every provider shares (`model.md` §10.5–10.7:
  batches, results, partial calls, stop notes) live in one module.

**Events: saved and live, kept apart.**

- *Saved events* come from the core and mean "look at the tree": node added,
  cursor moved, label changed, item changed, busy or idle, question, notice.
  The tree is the truth; a frontend that falls behind catches up by reading
  it.
- *Live events* come from the provider and tools and are never saved: text
  and thinking arriving, a call forming (its name, then its arguments as they
  stream), a running tool's progress (bash output as it arrives). A frontend
  draws them as a layer over its cursor and drops the layer when the node they
  belong to is saved. A cancelled sample is saved as an `Interrupted` reply
  with what arrived (see "Amended while building"), so dropping or merging
  live events loses nothing, and a slow frontend may skip them.
- The provider's stream yields live events as they arrive and whole parts as
  they finish; the driver forwards the first and saves only the second.
- A tool may take a `progress` callback; the driver turns each call into a
  live event tagged with the call's id.
- Every event names its cursor. The driver offers one async stream
  (`driver.events()`) with an optional cursor filter.

## Amended while building

1. **Cancelled text was lost.** The first draft said a cancelled sample "is already
   saved as an `Interrupted` reply with what arrived". It is not: a provider yields a
   part when the part finishes, so the text of a part still open at the cancel is in
   no part. (The OpenAI provider loses it today.) A generator cannot yield while it is
   being cancelled, so the driver does it: it keeps the live text and thinking whose
   part has not arrived, and on cancel or failure saves them as one `Text` or
   `Thinking` part. This forces a rule on providers: a part is yielded before the
   first live event of the next part (a provider that breaks it fails the sample).
   A call still forming at the cancel is not saved (its arguments are not a part).
2. **Live events have no cursor at the provider.** A provider does not know which
   cursor it serves, so it yields cursor-less deltas (`TextDelta`, `ThinkingDelta`,
   `CallNamed`, `CallArgs`) and the driver wraps each as `Live(cursor, delta)`. "Every
   event names its cursor" holds for what `driver.events()` yields.
3. **Saved events start as one kind.** `Saved(cursor)` means "the tree changed for
   this cursor, look at it", emitted after the driver applies each answer to core. The
   fine-grained kinds (node added, cursor moved, label changed, item changed, busy or
   idle, question, notice) are a later slice.
4. **Subscribing is immediate.** `driver.events()` is a plain call that subscribes
   when called (an async generator would subscribe at its first `anext`, after
   events could be missed). The stream is an `EventStream`: its own bounded buffer,
   `aclose()` unsubscribes, an `async with` works. A full buffer drops `Live`; a
   `Saved` merges into an equal `Saved` just before it, or takes the place of the
   oldest `Live`; only a buffer of nothing but `Saved` grows past its bound.
   Stopping the driver ends every stream after what it holds.
5. **`Provider.close()` is `aclose()`**, and a provider has a `name`; the driver's map
   must key each provider by its own name (checked at construction).
6. **`check_config` takes the map as a required keyword**; `read_models` (no
   provider checks, as it never had) does not, so an unknown provider name found
   outside `tau config check` fails that sample, with the known names in the error.
7. **`builtin_providers(environ)`** builds the built-in providers (`openai`, `mock`);
   the CLI and examples use it.
8. **`max_tokens`** is sent by the OpenAI provider as `max_tokens` (its one change, so
   a setting is not silently ignored); `options` it refuses until it has some.

## Open

- Fine-grained saved events (item 3).
- A call forming at a cancel (item 1): is a partial `Call` part with partial
  `args_raw` right, given `partial_calls`?

- Whether frontends subscribe per driver or per cursor. Cursors can use
  different providers; that affects replay data, not events, which are the
  same shared types from every provider. The cursor filter leaves both open.
- Normalized `usage` fields beyond input, output, cached.
- Images (not needed for a text-only port).

## Alternatives

| option | why not |
|---|---|
| A provider is a function called per sample | Nowhere to keep a connection; every hosted call pays a handshake. |
| Every setting typed in `ModelConfig` | The core config grows with every provider's knobs. |
| Every setting in `options`, `base_url` included | Proxies make `base_url` common to all; one shared spelling. |
| Save live events | The tree already holds what arrived (`Interrupted`); saving deltas doubles the log for display only. |
| Retries in core or the driver | A cursor never spins; a wrapper provider retries where it knows the status. |

## Consequences

- Glossary: provider factory, provider options, replay data, saved event,
  live event, progress callback, normalized usage.
- Rust: `replay` on `Text`, `Thinking`, `Call` (serde default; old logs load).
- `config_layers` loses its fixed provider list; `check_config` takes the
  provider map.
- The OpenAI provider keeps one client per endpoint and maps `reasoning` /
  `reasoning_content` to `Thinking`.
- Tests: a part's replay data round-trips through the log; another provider's
  replay data is never sent; a misspelt option fails `tau config check`; a
  cancelled stream leaves the `Interrupted` reply equal to the live events
  seen; one client serves many samples.
