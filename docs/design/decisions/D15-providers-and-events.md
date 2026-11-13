# D15: Providers own their connections; frontends get saved and live events

Status: accepted (2026-10-30), after the live checks below. Built: the provider contract,
settings, live and saved events and the mock provider (items 1-8); the OpenAI provider
(10-13); replay data and the shared walk (14); the Anthropic provider (15); the Gemini
provider (16); failed replies that say why (17); one HTTP module and one walk for the
three hosted providers (18); tool progress (19, 20). A layered review of the providers
(2026-10-30) amended items 1, 4, 9, 14, 17, 18 and 20 and settled most of what was Open;
see "Review 2026-10-30". Amends `model.md` §9 and §10.
Its frontend rules for events (1-5, below) and the `Saved`, `HookError` and `EventStream` they
describe are superseded by D16 §5 (built in D16 stage 2); `Live` and the deltas stand.

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
  part as it would an unsigned one (`model.md` §10.4). Replay data its own provider
  wrote but this version cannot understand counts as unsigned too (Review).
- `Finish(stop, reason, meta)`: `meta` gains a normalized `usage` (input,
  output, cached tokens) beside the provider's raw fields.
- `SampleError(message, meta, status=None, retry_after=None)`. The core never retries
  (cursor never spins); a retry is a wrapper provider, and only an example
  (`examples/retry.py`) ships (Review, D3).
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
  belong to is saved (the exact rules are "Frontend rules for events", below). A cancelled sample is saved as an `Interrupted` reply
  with what arrived (see "Amended while building"), so dropping or merging
  live events loses nothing, and a slow frontend may skip them.
- The provider's stream yields live events as they arrive and whole parts as
  they finish; the driver forwards the first and saves only the second.
- A tool may take a `progress` callback; the driver turns each call into a
  live event, `ToolProgress(node, call_id, text)`, tagged with the call (item 19).
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
   first live event of the next part, a part yielded while deltas are open must be the
   part they were building (same kind and text; a call: same id, tool, arguments), and
   `Finish` may not come with deltas open (a provider that breaks any of these fails
   the sample, and the open text is saved).
   A call still forming at the cancel is not saved (its arguments are not a part). A call
   cut off by any failure is dropped the same way, in every provider (Review, D1).
2. **Live events have no cursor at the provider.** A provider does not know which
   cursor it serves, so it yields cursor-less deltas (`TextDelta`, `ThinkingDelta`,
   `CallNamed`, `CallArgs`) and the driver wraps each as `Live(cursor, delta)`. "Every
   event names its cursor" holds for what `driver.events()` yields.
3. **Saved events start as one kind.** `Saved(cursor)` means "the tree changed for
   this cursor, look at it", emitted after the driver applies each answer to core that
   changed something (Review, D2) and
   by the frontend calls that change a cursor with no answer (`input`, `go_to`, `open`,
   `add_item`, `set_label`, ...), so a second frontend hears of them. The
   fine-grained kinds (node added, cursor moved, label changed, item changed, busy or
   idle, question, notice) are a later slice.
4. **Subscribing is immediate.** `driver.events()` is a plain call that subscribes
   when called (an async generator would subscribe at its first `anext`, after
   events could be missed). The stream is an `EventStream`: its own bounded buffer,
   `aclose()` unsubscribes, an `async with` works, and the driver holds streams weakly so
   an abandoned one is collected. One consumer per stream. A buffer holding `capacity`
   events drops further `Live`; a `Saved(c)` removes the buffered `Live`s of `c` and
   any earlier `Saved(c)` (the frontend drops that layer at `Saved(c)` anyway) and joins
   the end (superseded by Review, D2: only a `Saved` with `reply` removes the `Live`s), so a buffer never exceeds capacity plus one `Saved` per cursor. Stopping the
   driver ends every stream after what it holds; a driver that fails (pump or effect)
   ends them by raising the failure from `anext`.
5. **`Provider.close()` is `aclose()`**, and a provider has a `name`; the driver's map
   must key each provider by its own name (checked at construction).
6. **`check_config` takes the map as a required keyword**; `read_models` (no
   provider checks, as it never had) does not, so an unknown provider name found
   outside `tau config check` fails that sample, with the known names in the error.
7. **`builtin_providers(environ)`** builds the built-in providers (`openai`, `anthropic`,
   `gemini`, `mock`); the CLI and examples use it.
8. **Settings are checked at run time too.** `tau config check` is not the only caller
   of `Provider.check`: the driver calls it before every sample and fails that sample
   with the problems listed, so an ill-typed option never reaches `sample`.
   `ModelConfig.options` is a deep read-only copy.
9. **`max_tokens`** is sent by the OpenAI provider as `max_tokens` (its one change, so
   a setting is not silently ignored); `options` it refuses until it has some
   (superseded by item 10, which gave it options).

10. **The OpenAI provider is built to the contract.** One `httpx.AsyncClient` (httpx pools
   per host, so not one per `base_url`), made on the first sample; `aclose()` closes it and a
   later sample fails. It streams `TextDelta`, `ThinkingDelta` (from `reasoning_content` or
   `reasoning`), `CallNamed`, `CallArgs`, and yields each part before the next kind's first
   delta. Options: `temperature`, `top_p`, `reasoning_effort`, `include_usage`,
   `extra_body` (merged last; `model`, `messages`, `stream`, `tools` refused). `SampleError`
   has keyword-only `status` and `retry_after`.
11. **`usage` is normalized; the raw object moves to `usage_raw`.** Before, the OpenAI
   provider kept the server's `usage` under `usage`; the contract gives that key to the
   normalized `{input, output, cached}` (the mock's shape), so the raw object is
   `usage_raw`. Servers that report no cached tokens get `cached: 0`; a server that
   gives no prompt and completion counts gets no `usage`.
12. **Call ids and names are read until the call is announced.** `CallNamed` needs both, so
   the provider yields it when both have arrived (arguments before that are held), and
   later id or name fields of that call are not read.

13. **Review fixes to item 10.** The body is read to its end after `[DONE]` so the
   connection is pooled. A client belongs to the loop that made it. A sample after
   `aclose()`, or cut by it, raises `ProviderClosed` (a `SampleError`). `extra_body` may
   not set `n` or a key the model's settings set; `check` also refuses non-finite numbers
   in it, and the config loader refuses `NaN`, `Infinity` and out-of-range numbers as
   invalid JSON. A choice other than index 0 fails the sample (a chunk with more than one
   choice is refused outright, Review). A call that changes its id
   or name after being announced (no `index`, a split name) fails the sample rather than
   merging. `Retry-After` and the status are taken before an error body is read.
   A malformed cached count leaves `usage` out.

14. **Replay and the shared walk are built** (slice 3). `Replay(provider, data)` is a frozen
   class, not loose JSON: `data` is the text of a JSON object with no repeated key and
   `provider` a visible name, checked in Rust at construction (`BadReplay`, a `ValueError`) and
   when a log loads (without building a parse tree; a refusal there says why). A `BadReplay`
   escaping a provider's `sample` fails that sample like a `SampleError` (superseded by
   Review: every exception from `check` or `sample` fails one sample, and the separate
   `BadReplay` branch is gone). A part's `replay` is
   `None` by default; a part without one is logged in the old spelling, and one with it as
   `{"Text": {"text", "replay"}}`, so old logs load. `tau.replay_for(part, provider)`
   returns the parsed data only for the part's own provider; the exact text is
   `part.replay.data` (parsing keeps string values but not necessarily number or escape
   spelling). The ordering rule already
   compared only text, tool and arguments, so a final part may carry replay its deltas lacked.
   The walk rules live in the private `_providers/_walk.py` (`turns(walk)`), which the OpenAI
   provider uses with byte-identical requests; it is private until a third-party provider
   needs it. A `ToolResults` result also carries the call's `tool` (Gemini names it) and
   `error` (Anthropic flags it). A signed part cut off at a cancel is saved without its
   signature, so it counts as unsigned and is never sent.

15. **The Anthropic provider** (`_providers/anthropic.py`, request half in
   `_providers/_anthropic_request.py`) keeps to the contract. What the docs showed that
   shaped it: the API requires `max_tokens`, so `check` requires it (no default); newer
   models take `thinking.type` `adaptive` with `output_config.effort`, and `enabled` with
   `budget_tokens` is refused by 4.7 and later, so `thinking` is passed through as an object
   and the API judges the model; `display` defaults to `omitted` on newer models, which
   streams an empty `thinking_delta` and a signature, so a signed `Thinking("")` is normal;
   thinking blocks must come back unmodified, `redacted_thinking` too, and under manual
   `enabled` thinking the last assistant turn must open with one. Choices: replay data is
   `{"type": "thinking", "signature"}` or `{"type": "redacted_thinking", "data"}`; usage
   `input` counts every prompt token (`input_tokens` + cache reads + cache writes) to match
   OpenAI's `prompt_tokens`, `cached` is reads only; `pause_turn` and
   `model_context_window_exceeded` map to `Length` (no server tools are offered, so
   `pause_turn` is not expected), `stop_sequence` to `End`; a stream `error` event takes the
   status its `type` stands for. Walks that would make the API refuse are sent as text
   instead of failing every later sample (§10.7): a runnable batch whose `args_raw` is not a
   JSON object, any batch when the sample offers no tools (the API refuses `tool_use` blocks
   without `tools`), or a batch of a loop that did not open with thinking under manual thinking
   (a tool loop is one assistant turn, and the final turn of a request must start with a
   thinking block; checked only when the walk ends in tool results, on the loop's first
   assistant message), goes as call markup with its results
   as `[tool result ...]` user text; empty assistant turns are dropped and neighbours merge;
   a `System` node after the start becomes user text `[system note: ...]`; ids the API refuses
   are mapped by a pure function (bad characters to `_`, plus a hash). Replay that cannot
   become a `Replay` fails the sample. Caching marks the system text and the last message
   (default on); a turn that adds 20 or more blocks past the last mark misses the cache
   (the API's lookback window). The provider sends no `anthropic-beta` header, so `check`
   refuses the options that need one. It keeps `anthropic-version: 2023-06-01`:
   `2026-10-21` removes `temperature`, `top_p` and `top_k`. `Part.Thinking` text is the summary, so a round trip sends
   the signature and the summary together as received.

16. **The Gemini provider** (`_providers/gemini.py`) is built to the contract, and building it
   showed:
   - *Signatures arrive on the last chunk of the text they sign*, often one with no text (live,
     `gemini-3.5-flash`: three text chunks, then `{"text": "", "thoughtSignature": ...}` with the
     finish reason; a short reply carries it on the one text part). A signature therefore ends the
     saved `Text`, and replay is `{"thoughtSignature": ...}` on that part. On a call it is a
     sibling of `functionCall` in the same part. Thought summaries carried none in any capture.
   - *A tool call in the current turn without its signature is a 400* on Gemini 3 ("Function call
     is missing a thought_signature"). A call from another provider, or from a model that sent
     none, therefore fails the sample with the API's message; there is no fallback signature (the
     documented dummy value is a workaround and is not used).
   - *Signatures crossed models* in one check (a `gemini-3.5-flash` call signature accepted by
     `gemini-3.5-flash-lite`), so replay is tagged with the provider and not the model.
   - *Ids are optional.* This API gave `call_<n>` ids on calls. A call without one gets a
     random UUID id (`gemini-<hex>`), unique in any session, and its replay marks it
     `synthesized_id`; only an API id (replay `id`) goes back, on the `functionCall` and its
     `functionResponse`. Without ids Gemini matches responses by name and order, so the batch
     keeps call order.
   - *Errors can arrive inside a 200 stream* (`{"error": {"code": 503, ...}}`), after parts.
   - *Thoughts are counted separately*: `thoughtsTokenCount` is not in `candidatesTokenCount`.
     Normalized `output` is their sum.
   - Rules the review of the walk module asked of a provider, now in `_walk` beside Anthropic's
     (item 18): a batch whose call arguments are not a JSON object goes as text with its
     results; model turns left empty (unsigned thinking only) are dropped and neighbouring
     contents merge; a `System` node after the first message is a `[system note: ...]` user
     turn. Data core refuses as replay fails the sample, which stays in the provider.
17. **A failed reply says why.** A sample that failed after parts arrived saved an
   `Error` reply, and the error text went only to `post_step` (`Ending::Failed`): a
   Gemini 503 inside the stream left a lone `Thinking` part and no reason in the tree.
   Chosen: the failed reply carries the error in its `meta`, as
   `error: {message, status?, retry_after?}` (amended by Review: also `internal?` and
   `provider?`), added by the driver (`_effects.py`) from
   `SampleError`. Rejected: a core-written `sample_error` node after the reply. It would
   hang a side child under the cursor's node, so a frontend reading the reply would
   still not learn why, and a second place to look. Core keeps `meta` opaque, so no
   Rust change. The no-parts case is unchanged (a `sample_error` node holding the
   text; the sample left no reply to carry it). An old log loads: its failed replies
   just have no `error` entry.

18. **One HTTP module and one walk for the three hosted providers.** `_wire.Connection` holds
   the client (per loop, `ProviderClosed`, `aclose`) and the sample loop that all three had
   copied; each provider keeps its request, its `_Stream`, and how its error bodies read
   (Anthropic leads with the error `type`; Google's `RetryInfo` is read in `gemini.py`, since no
   other API sends it). `_walk.turns` takes the rules that were local: `object_args`
   (Anthropic and Gemini; a blank `args_raw` is `{}` for both, which Gemini used to send as text;
   superseded by Review: the option is deleted and the rule is always on),
   `structured` (Anthropic sends no `tool_use` without `tools`; nothing says Gemini refuses
   `functionCall` parts without `tools`, so it did not ask; superseded by Review: all three
   pass `structured=bool(tools)`), and the provider hook `text_from`
   (only Anthropic's manual thinking depends on what a provider would send). `Messages` does
   the merge and drops what a provider rendered empty; what counts as empty stays the
   provider's. OpenAI asked for `object_args` only (now always on): llama.cpp answers HTTP 500 "Failed to parse tool call
   arguments as JSON" to a history call whose `arguments` is `{"p"` (probed by hand), and an empty
   `arguments` goes as `{}`. It offered tools whenever it had any, so it had no use for `structured`
   (superseded: Review). The system note was said to be `[system note: ...]` everywhere, but OpenAI
   sent `role: system` until the Review (D11) made it true. A negative token count is malformed
   for all three.

19. **Tool progress (built).** A tool opts in by giving `call` a parameter named
   `progress`, detected once when the driver is built (`tool_keywords`; D15 amended 2026-11-12, it was `takes_progress`); the driver then
   passes a `Progress` (`Callable[[str], None]`) by keyword. A tool without it is called with
   `args_raw` alone. Chosen over a context-local `tau.progress(text)` because it is
   explicit in the signature (a bad `progress` fails when the driver is built), needs no
   hidden state that a Rust thread or another language's tool cannot see, and "any object
   with these attributes works" stays true: the attribute is the parameter. `progress` is
   callable from any thread; each call is moved onto the loop and becomes
   `Live(cursor, ToolProgress(node, call_id, text))` while the tool runs. `node` is the
   batch's node (the `Assistant` node holding the calls): servers may reuse call ids across
   batches (some local ones send `"0"` every time) and core rejects no duplicates, so the id
   alone does not say which call; `(node, call_id)` does. After the call has
   finished, been aborted or cancelled, it does nothing and raises nothing. `text` is new
   output since the last event, not the whole so far. Empty text is dropped; a non-`str`
   raises `TypeError` to the tool. Progress is never saved: the `Outcome` the tool returns is
   what `Results` holds. A provider cannot yield a `ToolProgress`: `Delta` is the provider's
   four kinds, `Live.delta` is the wider `LiveDelta`, pyright refuses it, and at run time a
   sample that yields anything but a part, a delta or a `Finish` fails. `@tool` functions
   take a parameter named `progress` the same way; it is not an argument, so it is not in
   `parameters`. A `progress` parameter annotated as anything but `Progress` (an argument
   that happens to share the name) is a `ToolDefinitionError`, not silently taken over.
   **Amended 2026-11-12: the call id.** A tool that runs somewhere else (a remote executor)
   needs its call's id, so `call` may also name a parameter `call_id`: the driver passes the
   `Part.Call`'s `id` by keyword, found by the same one pass as `progress` (`tool_keywords`,
   which replaced `takes_progress`; a positional-only keyword is a `DefinitionError`). Only
   `Tool` objects take it. A `@tool` function takes `Progress` by annotation, and `call_id` has
   no annotation: a new injected type would be a new public concept shared by commands (which
   run no call), for a need that a `Tool` object already meets.
   `bash` passes its output to `progress` as it arrives. The reader thread (Rust, no GIL)
   only copies bytes into a buffer of at most 64 KiB; a thread of its own takes the GIL for
   each call and passes the buffer on at most once per 100 ms (the first bytes after a quiet
   spell at once). Text is cut only between UTF-8 characters: only a half-arrived character
   in the last one to three bytes is held back, an invalid byte is shown at once and does not
   hold up what follows, and dropping the oldest bytes skips to a character boundary. What a
   slow consumer holds up: the command and the reader never wait on it (the buffer drops its
   oldest bytes), but the answer does, because `wait` joins the emitter thread: it waits for
   a callback already running and for the final flush, one more call. (With 1 s callbacks,
   `echo a; sleep 0.2; echo b` answers at 2 s.) Bounding that would need interrupting a
   callback in flight; under the driver the callback is only `call_soon_threadsafe`, so it
   does not arise. The answer (10 kB tail, overflow file) is the same with or without
   progress. The emitter attaches with `try_attach` after checking a flag that an `atexit`
   callback clears: under abi3-py312 PyO3 cannot ask whether the interpreter is finalizing,
   and attaching then is undefined behavior on 3.12 and 3.13, so no call is made once exit
   begins. `read`, `write`, `grep` take none.

   *Found while building:* `Saved(c)` was emitted after every answer, and a batch answers
   once per call, so it fired while other calls of the batch still ran, and wiped that
   cursor's buffered `Live` events, tool progress included. Fixed in item 20.

20. **`Saved` names the calls it settled; progress outlives other saves.** (Amended by
   Review, D2: a `Saved` purges the deltas only when `reply` is true, and is emitted only
   when something changed; the `calls` rule stands.) A call answered
   before its batch is whole only fills a slot in core: no node is added, so the driver now
   emits no `Saved` for it (a `Saved` means the tree changed). The batch's `Results` node
   is the save that answers its calls, and that `Saved` carries `calls`, the calls it
   answered as `(node, call_id)` pairs: `Saved(cursor, calls=())`. The buffer rule: `Saved(c)` still removes
   earlier `Saved(c)` and, if it has `reply`, the provider deltas (text, thinking, call)
   buffered for `c`; buffered
   `ToolProgress` of `c` survives every `Saved` except for the calls in its `calls`. The
   `calls` of a `Saved(c)` it replaces move onto it, so a subscriber that fell behind still
   hears which calls settled. The frontend rule matches: draw a call's progress as a layer
   keyed by `(node, call_id)` and drop it when a `Saved` names the call (its result is in the tree),
   not at every `Saved`. The bound is unchanged: a full buffer drops any `Live`, progress
   included, so the buffer holds at most `capacity` events plus one `Saved` per cursor;
   progress surviving a save does not enlarge it (a flood fills the buffer and the rest is
   dropped, oldest kept). Other `Saved`s (frontend calls such as `set_auto`, hook answers)
   can still arrive mid-batch with `calls=()`; progress survives them.

   A hook's write is announced when it happens. A handle's writes (`append`, `append_root`,
   `open`, `add_item`, `remove_item`, `set_label`, `remove_label`, and a cursor hook's
   `move`, `write`, `set_auto`) each emit `Saved` for the cursor they changed (the hook's or
   batch's own when they name none), through a `changed` callback beside `poke`. Before, only
   the answer was announced, so a `pre_result` hook that wrote on a fast call went unseen
   until the slow call of the batch ended. A call answer that changed nothing still emits
   nothing.

21. **`HookError`: a failed cursor hook is a live event, not a saved one (ruling 2026-10-30).**
   When a cursor hook (`pre_input`, `pre_step`, `pre_sample`, `pre_reply`, `post_reply`,
   `post_result`, `post_step`) raises or breaks its contract, the failure is not written to
   the session: core ends the step with `Ending.Failed`, and the driver emits
   `HookError(cursor, point, extension, error)` to the frontends, `error` being the same
   `"Type: msg"` text `post_step` hooks see (traceback still on stderr). It is emitted for
   every cursor-hook failure, a failing `post_step` included (that hook's `Ending` is the one
   it fails *after*, so nothing else would tell). It names the cursor the hook ran for, so
   `driver.events(cursor)` filters it like the rest. It is not replayed on reload: a frontend
   that was not subscribed never learns it, and an extension that wants the record keeps it
   (a logging extension can write it to its own file, or to a node with `cursor.append`).
   A tool hook's failure is not a `HookError`: it is the `Err` in the call's `Results` node.
   *Delivery:* a `HookError` is never dropped by a full buffer. It is the only record of why
   a step stopped and is not in the tree to be re-read, so rule 1 (read the tree) cannot make
   up for losing it. To keep the stream bounded, a `HookError(c)` replaces the buffered
   `HookError(c)` before it (the newest failure explains where the cursor stands, as one
   `Saved(c)` stands for many), and no `Saved` removes one. The bound becomes: at most
   `capacity` events, plus one `Saved` and one `HookError` per cursor.

## Review 2026-10-30

A layered review of the providers and the events (driver boundary, shared wire code, each
provider, config, tests) found bugs, over-promising docs and code worth subtracting. All of
it is merged except what is Open below. Decisions, with the reason in a line:

- **D1. Cut-off calls are dropped everywhere.** A call that did not finish is never saved
  half-built; its raw text goes in `meta.error` (`a call was cut off and dropped: ...`).
  One rule instead of three providers' three ideas of "partial", and a half-built call is
  not a call. `Connection.sample` lost its re-raise block.
- **D2. `Saved.reply`.** It is true only for a sample's answer (`Answer.Sampled`); live
  deltas are purged only by a `Saved` with `reply`. Before, any save (a `set_auto`, a hook
  write) wiped the streaming layer. `Saved` is also emitted only when the tree, the labels,
  the cursor's place, state or items changed, so "the tree changed" is true. Coalescing
  ORs `reply`.
- **D3. Retries live in an example only** (`examples/retry.py`, `RetryProvider`): neither
  core nor any provider retries (cursor never spins). The example retries a whole sample
  only if nothing was yielded yet; on 408, 429, 502, 503, 529, a 500 with `retry_after`,
  and a transport error (the `__cause__`); never a plain 500, which llama.cpp uses for
  deterministic errors.
- **D4. No value lists.** `check` keeps type checks and the API judges values, so a new
  `effort` level or `thinking.type` needs no release here. It still refuses what can never
  work here: options needing a beta header (`thinking.display: "updates"`,
  `thinking.block_binding`, `extra_body.context_management`), and the Anthropic budget
  check (`budget_tokens` at least 1024 and below `max_tokens`).
- **D5. `request()` is private** (`_request`), without its re-check: the driver runs
  `check` first, and `Provider.sample` assumes it passed. A one-off call needs no new API;
  the recipe is in the glossary (`examples/one_off.py`).
- **D6. Superseded (2026-10-30):** `Refused` was removed; see the amendment below.
- **D7. No timeout option yet.** The defaults stand (10 s to connect, 300 s between bytes).
  `return_progress` is documented below. Closed (2026-10-30): whoever hits the defaults adds
  a provider option.
- **D8. No flag for dropped live events.** A slow subscriber's drops are not marked; the
  frontend rules below make that safe, and the tree is the truth.
- **D10. A client belongs to the event loop that made it.** A sample on another loop fails
  with a `SampleError` saying to build one provider per event loop (amended 2026-10-30; it
  used to drop the client and make a new one, which leaked a socket per loop).
- **D11. `[system note: ...]` everywhere** after the first non-`System` turn, OpenAI
  included. Leading `System` nodes stay `role: system`. One rule for every provider.
- **D12. An empty send is allowed.** Gemini renders blank turns as nothing (they are
  dropped); the API judges the rest.
- **`structured=bool(tools)` for every provider.** A sample that offers no tools never
  sends `tool_use` or `functionCall` blocks; they go as text.
- **Replay that is not understood counts as unsigned.** Data we cannot read never fails a
  branch.
- **Provider errors fail one sample, with `internal: true`.** Any `Exception` from `check`
  or `sample` that is not a `SampleError` keeps the parts, saves `meta.error` as
  `"Type: msg"` with `internal: true`, and prints the traceback to stderr, never the log.
  Driver and core invariant breaks still kill the driver. An abort saves `Cancelled`
  for a sample it cuts off, with the parts so far; a sample that had already finished (its
  `Finish` yielded) before the abort reached it is answered as the complete reply it is, and
  the abort still ends the step `Aborted`. A hook or tool that breaks its contract costs one
  step or one call the same way (`Type: msg`, traceback on stderr).
- **Strict JSON.** Server events and call arguments with `NaN` or infinity are invalid (a
  NaN argument counts as "not an object" and goes as text); `meta` is written with
  `allow_nan=False`; `retry_after` is `None` when not finite; a lone surrogate in an event
  is a `SampleError`.
- **Keys safe in headers.** `ModelConfig.api_key` refuses any character outside
  `0x21-0x7E`, naming the variable or the literal, never the key. A header an HTTP client
  cannot encode is a `SampleError` that never contains the header value.

Also carried out: `Connection` ignores content after the reply is done (only `meta` is
read); `CallArgs` needs an open `CallNamed` with the same index; a provider's `check` does
not verify its own name (the driver's name-keyed map does); `_typed_body(model)` is each
provider's one source of request keys and `extra_body` conflicts; Gemini refuses non-list
`parts`, parts with more than one data key, a non-bool `thought` and a `models/` prefix on
the model name, ignores chunks after a `blockReason` and metadata part keys, quotes the
model in the URL, and drops blank turns; OpenAI refuses a chunk with more than one choice
and a `base_url` ending in `/chat/completions`, and turns `delta.refusal` into text; Anthropic refuses `extra_body.thinking`, checks the block index, skips null
usage values, and gives a non-object error event status 500; the config loader bounds JSON
integers to 64 bits; `tau config check` turns a raising provider check into a problem
line, and a bad literal key is an error while a bad key inside a variable is a warning
(the variable is read at sample time).

Amended 2026-10-30: `Refused` was removed from `Stop`; refusals end as `End` with the raw
reason kept in `Finish.reason` (a `refusal` delta is still text). A call forming at a cancel
is saved as it arrived, the same rule as text.

Amended 2026-11-12 (reverses the removal above): **`Stop.Refused` is back.** As an `End`, a
refusal could not be told from an answer without knowing every provider's reason strings, so
`tau -p` printed nothing and exited 0 on one (found in E1). Anthropic's `refusal`, OpenAI's
`content_filter` and a `refusal` delta, Gemini's safety, recitation and blocklist reasons and
a blocked prompt (`blockReason`) end as `Refused`; the raw reason is still `Finish.reason`, and
the parts are still text and still replayed (D6 holds again). The question that was Open:
a refusal replaces only an `End`; a `Length` or `Tools` stop is kept (a `refusal` delta then
`finish_reason: length` is `Length`). Gemini's `LANGUAGE` and `ESCALATION` stay `End`: they
are not documented as a refusal. Logs written before name no `Refused` and load as they were.

**Declined:**

- *Capping meta strings.* Meta records what the server said; a cap would hide it.
- *A content-type check on responses.* Servers and proxies disagree about it, and the SSE
  reader already fails on what is not SSE.
- *A total deadline.* A long stream is legitimate and abort exists; the wait between bytes
  is bounded. Revisit with the timeout option.
- *A startup check* (probing a provider when the driver is built). It would make startup
  need the network; `tau config check` and the per-sample `check` suffice.

### Live checks 2026-10-30

Every hosted provider ran against the real API, directly and through a LiteLLM proxy, with
`examples/cache_rate.py` (four turns on a ~7k-token system prompt). Cached share of the
prompt, turns 2-4:

| route | cached |
|---|---|
| Anthropic direct, and through LiteLLM's `/anthropic` passthrough | 99-100% |
| Anthropic through LiteLLM's OpenAI endpoint (our `openai` provider) | 0%: nothing marks the prefix; 100% when LiteLLM injects the marks (`cache_control_injection_points`) |
| Gemini direct, passthrough, or LiteLLM's OpenAI endpoint | 66% (Gemini's implicit caching covers ~4k of 6.1k) |
| Mistral direct (`openai` provider) | 99%, 0%, 99% (a miss on Mistral's side) |
| Mistral through LiteLLM (`openai` or `anthropic` provider) | 99-100% |
| llama.cpp (`openai` provider) | 100% |

- The `anthropic` provider drives non-Anthropic models through LiteLLM's unified
  `/v1/messages`: Mistral caches at 100%. Gemini fails on a free-tier key: LiteLLM turns our
  cache marks into Gemini's explicit cached content, which the free tier allows none of
  (`TotalCachedContentStorageTokensPerModelFreeTier ... limit=0`). The failure is a sample
  error with the server's message, as it should be.
- A reasoning model behind an OpenAI-compatible endpoint (Paper Instruments
  `ultramarine-1-flash`) streams `reasoning_content`; it is kept as a `Thinking` part.
- *The Anthropic empty reply after tool results* (was Open; option A, change nothing, stands).
  A `Results` node followed by a `User` node goes out as one user message, tool results then
  text, which Anthropic's docs say can give an empty `end_turn`. 80 samples (Haiku and
  Sonnet, 10 each of four shapes: a follow-up question, a bare "thanks", "go on" mid-task, a
  new instruction mid-task) gave no empty reply: answers, or the next tool call. No
  placeholder and no unmerging.
- *Anthropic tool-id length* (was Open). Ids of 40 to 1024 characters are accepted; ids are
  mapped for their characters only, never shortened, and that is enough.
- OpenAI proper is not exercised (no key or credits).

### Frontend rules for events

**Superseded by D16 §5** (`Saved`, `HookError` and `EventStream` are gone; see `model.md` §9 and
the glossary for the client rules). Kept as history.

1. Live events are advisory until the next `Saved`. A subscriber that fell behind lost
   some; read the tree.
2. Ignore a `CallArgs` whose `CallNamed` you never saw (the `Live` before it may have been
   dropped).
3. Drop a reply's streamed layer (text, thinking, call deltas) at `Saved(reply=True)`. No
   other `Saved` clears it.
4. Drop a call's progress layer, keyed by `(node, call_id)`, when a `Saved.calls` names it.
5. Show a `HookError` as why the cursor's step stopped. It is not in the tree and not replayed:
   keep it as long as the cursor's status is on screen, and expect none for a failure that
   happened before you subscribed.

### Provider settings worth knowing

- **llama.cpp `return_progress`.** `options.extra_body: {"return_progress": true}` asks the
  server to send progress while it processes a long prompt, which keeps the read alive
  (the read timeout is 300 s between bytes). Exercised against llama.cpp b1637 with
  `examples/config.json`'s `local:qwen` (2026-10-30): the reply and usage arrive as without it.
- **OpenAI proper and `max_completion_tokens`.** Newer OpenAI models refuse `max_tokens`.
  Leave `max_tokens` unset and send `options.extra_body: {"max_completion_tokens": N}`.
- **Gemini 3 and switching mid tool loop.** A call from another provider carries no
  signature, so switching to a Gemini 3 model in the middle of a tool loop fails with a 400
  ("Function call is missing a thought_signature") until the next user text starts a new
  loop. There is no fallback signature (Fail Early).

## Open

- A provider-check problem points at the model's path even when the option came from
  a group or another layer.
- Moved to D16 (clients and asking).

## Deferred until something needs them

- Normalized `usage` fields beyond input, output, cached.
- Images (not needed for a text-only port).
- `tau config check` and `tau trust` know only the built-in providers, so a third-party
  provider cannot be checked from the CLI yet.

## Alternatives

| option | why not |
|---|---|
| A provider is a function called per sample | Nowhere to keep a connection; every hosted call pays a handshake. |
| Every setting typed in `ModelConfig` | The core config grows with every provider's knobs. |
| Every setting in `options`, `base_url` included | Proxies make `base_url` common to all; one shared spelling. |
| Save live events | The tree already holds what arrived (`Interrupted`); saving deltas doubles the log for display only. |
| Retries in core or the driver | A cursor never spins; a wrapper provider retries where it knows the status (an example, not built in). |

## Consequences

- Glossary: provider factory, provider options, replay data, saved event,
  live event, progress callback, normalized usage.
- Rust: `replay` on `Text`, `Thinking`, `Call` (serde default; old logs load).
- `config_layers` loses its fixed provider list; `check_config` takes the
  provider map.
- The OpenAI provider keeps one client per provider, bound to the event loop that made it, and maps `reasoning` /
  `reasoning_content` to `Thinking`.
- Tests: a part's replay data round-trips through the log; another provider's
  replay data is never sent; a misspelt option fails `tau config check`; a
  cancelled stream leaves the `Interrupted` reply equal to the live events
  seen; one client serves many samples.

## Amended (D24 8E-2, 2026-11-03): normalized usage

Normalized `usage` is `{input, output}` plus `cached` and `written` **when the server reports
them**; `input` still counts every prompt token. Item 11's "servers that report no cached tokens get
`cached: 0`" is withdrawn: an absent `cached` means the server did not say, and `cached: 0` means
it said it read nothing, which is what the cache notice (D24 section 3) needs to tell apart.

- **`openai`**: `cached` is `prompt_tokens_details.cached_tokens` and `written` is
  `prompt_tokens_details.cache_write_tokens`, each present only if the chunk carries it. A malformed
  count still leaves `usage` out. What LiteLLM sends was checked in its source (1.106.6,
  `types/utils.py` `Usage.__init__` and `llms/anthropic/chat/transformation.py`
  `calculate_usage`): for an Anthropic model its OpenAI endpoint always sends
  `prompt_tokens_details` with an integer `cached_tokens` (0 on a miss) and `cache_write_tokens`
  (also mirrored as `cache_creation_tokens`, and at the top level as
  `cache_creation_input_tokens` and `cache_read_input_tokens`); `prompt_tokens` already counts both.
  For a model whose own API reports no cache counts (llama.cpp, many local servers) it sends
  none, and then `cached` is absent. Only `cache_write_tokens` is read for writes: the
  other spellings are the same number.
- **`anthropic`**: `cached` is `cache_read_input_tokens` and `written` is
  `cache_creation_input_tokens`, each only if the response has it (Anthropic sends both, 0 on a
  miss).
- **`gemini`**: `cached` is `cachedContentTokenCount`, `0` when absent (Gemini omits zeros, so
  absent is a real zero); there is no `written`.
- **`mock`**: reports `cached: 0`, as before.
