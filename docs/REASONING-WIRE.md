# Reasoning on the wire: levels, artifacts, replay, counts

Decision record (2026-10-17), nothing built yet. Resolves forgejo #10, #22 and #23
together, because all three are one question: what τ sends, keeps and counts for
a model's reasoning on each wire.

## 1. What was wrong, measured

All figures are from `docs/probe-results/reasoning-wire-2026-10-17.md`: raw HTTP,
no SDK, no τ, one run per arm, run on 2026-10-17.

1. **τ's level names are not the wires' level names.** τ sent each level under its
   own name. `effort: "minimal"` is a 400 on `claude-haiku-5-5` ("Input should be
   'low', 'medium', 'high', 'xhigh' or 'max'"), `reasoning_effort: "minimal"` is a
   400 on `gpt-5.6-luna` ("Supported values are: 'none', 'low', 'medium', 'high',
   and 'xhigh'"), and `thinkingLevel: "xhigh"` is a 400 on `gemini-3.5-flash`.
2. **Google ignored the level.** Every level except `off` sent
   `{"include_thoughts": true}`, so `minimal` and `high` were one request. The wire
   has a knob that matters: `thinkingLevel` minimal/low/medium/high gave 0, 248,
   552 and 519 thought tokens.
3. **"No level" is not "no reasoning".** Sending no reasoning control, each
   provider's current model reasons anyway: 75 thinking tokens on Anthropic, 505
   on Gemini, 52 on OpenAI. τ turned `--thinking off` into "send nothing" in
   three places (`sdk.py`, `TauBackend` twice), and `_thinking_params` dropped a
   level on any entry without `reasoning: true`. So `off` reached no wire.
4. **τ deleted reasoning the user paid for.** Anthropic's `_convert_messages`
   dropped every thinking block before the last user message, on a docstring
   claim that the API discards them. Measured (#23 comment, 2026-10-16): replaying
   a 77-token prior-turn block raised turn-2 `input_tokens` from 80 to 159, so the
   API reads it. The current docs say the 5.5 models keep all prior turns.
5. **Google's signature on a plain answer was thrown away.** Gemini returns a
   `thoughtSignature` on a no-tool-call answer too, streamed as a trailing
   empty-text part. τ kept a signature only when a function call followed it.
   Replayed on turn 2, it raised `promptTokenCount` from 41 to 268, which is
   turn 1's 227 thought tokens. The model then recalled the secret number it had
   picked (737 in both placements). Without it, the model guessed 496 and spent
   635 thought tokens rebuilding its reasoning.
6. **Thinking was billed and hidden.** Anthropic's 5.5 models default to
   `display: "omitted"`. τ never asked for text, so a thinking block persisted as
   `""` with only a signature.
7. **Reasoning tokens were discarded.** All three wires report them
   (`output_tokens_details.thinking_tokens`,
   `completion_tokens_details.reasoning_tokens`, `thoughtsTokenCount`). τ dropped
   two of the three, and folded Google's into output with no split.
8. **#10:** `_map_finish_reason` mapped `content_filter` and any unknown value to
   `"stop"`, so τ reported an answer it could not vouch for as complete.

## 2. The level vocabulary stays common; each wire translates it

**Decided: one ordinal vocabulary (`off`, `minimal`, `low`, `medium`, `high`,
`xhigh`) for every head and the tree. Each provider owns a default translation
table, and `Model.thinking_level_map` overrides it per model.**

Alternatives:

* *Per-model declared vocabularies.* Loses, for two reasons. First,
  `set_reasoning` would be valid or invalid depending on which model is in force
  at the next request, and `set_model` can change that between the set and the
  use. Second, `/thinking` completion and the RPC params schema would need a
  live enumeration where a fixed domain does today.
* *A numeric budget.* Loses as a vocabulary. Anthropic rejects `budget_tokens`
  on every current model, OpenAI never had one, and only Gemini 2.5 still takes
  `thinking_budget`. A budget stays reachable per model as a map fragment
  (§2.2).

### 2.1 Default translation tables

A level a wire has no name for is **unsupported by default**, not renamed. The
clamp in `clamp_thinking_level` (upward first) picks the next level up, and §3
reports which one was sent. Renaming `minimal` to `low` would make two levels
one request, which is the defect §1.2 records on Google.

| Level | `anthropic-messages` | `google-generative-ai` | `openai-completions` |
|---|---|---|---|
| `off` | `thinking: {type: disabled}` | `thinking_config: {thinking_budget: 0, include_thoughts: false}` | `reasoning_effort: "none"` (see §4) |
| `minimal` | unsupported → clamps to `low` | `thinking_level: "minimal"` | `reasoning_effort: "minimal"` |
| `low` | `thinking: {type: adaptive, display: summarized}`, `output_config: {effort: low}` | `thinking_level: "low"` | `reasoning_effort: "low"` |
| `medium` | … `effort: medium` | `thinking_level: "medium"` | `reasoning_effort: "medium"` |
| `high` | … `effort: high` | `thinking_level: "high"` | `reasoning_effort: "high"` |
| `xhigh` | only when mapped (Anthropic's `xhigh` and `max` exist on some models only) | only when mapped (400 on 3.5-flash) | only when mapped |

Google sends `include_thoughts: true` with every level except `off`. Without it,
Gemini still thinks but returns no thought text (`no thinkingConfig` arm: 505
thought tokens, 0 chars).

The OpenAI-compatible table stays the identity because the wire's own enum is
the one τ inherited. A model whose server rejects a value (`gpt-5.6-luna`
rejects `minimal`) nulls it in the map: `"thinking_level_map": {"minimal": null}`.

### 2.2 What a map entry means on each wire

* **A string** is the wire's native level name: `effort` on Anthropic,
  `thinking_level` on Google, `reasoning_effort` on OpenAI-compatible. So
  `{"xhigh": "max"}` reaches Anthropic's `max`.
* **A dict** is a request fragment, merged in place of the default for that
  level. On OpenAI-compatible it merges into the body, as before. On Anthropic
  it merges into the request: `{"off": {"thinking": {"type": "between_tools"}}}`
  is how Sonnet 5.5, which rejects `disabled`, gets its lowest setting. On Google
  it merges into `thinking_config`: `{"high": {"thinking_budget": 8192}}` serves
  a Gemini 2.5 model. Google rejects a request carrying both a budget and a level
  (measured), so a dict replaces the default level key and does not join it.
* **`null`** marks the level unsupported, as before.

## 3. The level actually sent is reported, on the message

**Decided: `AssistantMessage.reasoning_level` holds the τ level whose wire form
this completion was sent with, after clamping. It is `None` when τ sent no
reasoning control at all.**

It lives on the message because the message is what every head already has:
extensions' `message_end` hooks, `get_messages`, the persisted tree, and the TUI.
The RPC `message_end` excludes `message` for size, so it also gets a lifted
`reasoning_level` field, the way `stop_reason` already is
(`TRUNCATED-TOOL-CALLS.md` §3.1). The field is a closed six-value enum, so it is
not unbounded content.

The τ level is reported, not the native value. The tables in §2 are static, so
the native value follows from the τ level and the model's map. A requested
`xhigh` reported as `high` is the clamp made visible. A requested `high`
reported as `None` is a model that does not declare reasoning (§4).

## 4. `None` and `"off"` stay two values, with two meanings

**Decided: keep both, and stop the heads collapsing one into the other.**

* **`None`** means τ sends no reasoning control, and the provider's own default
  applies. On every current model in §1.3 that default is to think.
* **`"off"`** means: send this wire's off form, or fail. It is sent whatever
  `Model.reasoning` says, because the flag says whether a model accepts
  *levels*, and turning reasoning off is not a level. #23's live case was a
  haiku entry without `reasoning: true` that thought while τ believed it was off.

Merging them fails both ways. If the one value means "send nothing", it is the
#23 bug. If it means "always send an off form", every request to every endpoint
carries a reasoning field. `gpt-4o` and llama.cpp each reject or ignore that
field, and τ cannot tell which.

What `"off"` sends when nothing is mapped:

| Wire | `reasoning: true` | `reasoning: false` |
|---|---|---|
| Anthropic | `disabled` | `disabled` |
| Google | `thinking_budget: 0` | `thinking_budget: 0` |
| OpenAI-compatible | `reasoning_effort: "none"` | **raises**, naming `thinking_level_map.off` |

The OpenAI-compatible `false` column raises because τ cannot know the off form
on that endpoint. On a llama.cpp chat template it is
`{"chat_template_kwargs": {"enable_thinking": false}}`. On OpenAI it is `"none"`.
On a model that never reasons it is `{}`, an empty fragment that says "nothing to
send". Guessing would be the silent drop this record exists to remove. A model
whose server rejects `disabled` (Opus 5.5) gets a 400 that names the cause, and
τ does not quietly substitute adaptive thinking.

A level other than `off`, on a model that does not declare reasoning, is still
not sent. `reasoning_level` then reads `None`, so the drop is visible.
`build_model_from_config` already warns at construction.

The heads that turned `"off"` into `None` stop doing so: `sdk.py`, both sites in
`TauBackend`, the `tau -p` resume path, and the TUI resume path. Those two resume
paths also turned a recorded `None` into `"off"`. A resumed session now keeps the
value it recorded. `create_agent_session(thinking_level=…)` defaults to `None`.
Its old default of `"off"` would now send `disabled` on every SDK session.

### 4.1 Compaction and branch summaries

**Decided: the summarizer gets the session's value unchanged.** `None` stays
`None` and `off` stays `off`. The provider clamps it like any other request.
Before, a summary got the level only when the model declared reasoning and the
level was not `off`. So a session that asked for `off` had its summaries think
by the provider default.

## 5. Opaque reasoning artifacts are kept verbatim and replayed unchanged

**One rule for every wire.** Whatever a provider returns that it needs back (a
signature, a redacted payload, an encrypted blob) is persisted in the tree as it
came. `ThinkingContent.thinking_signature` carries it, namespaced by provider. It
is sent back unchanged whenever the replay scope (§6) includes that message.

| Wire | What comes back | What τ stores | What τ sends back |
|---|---|---|---|
| Anthropic | `thinking` block: summary text (empty under `omitted`) + `signature` (the encrypted full thinking); `redacted_thinking`: `data` | `ThinkingContent(thinking=text, thinking_signature={"anthropic": {"signature": …}})`, or `{"anthropic": {"redacted": true, "data": …}}` | the same block, unchanged |
| Google | thought parts (`thought: true`, summary text) when `include_thoughts`; a `thoughtSignature` on the first `functionCall` of a step, **or on the answer's last part** (streamed: a trailing empty-text part) | function-call signature: `ToolCall.provider_signature` (unchanged); answer signature: `ThinkingContent.thinking_signature={"google": {"thought_signature": b64}}` (**new**) | thought parts as `{text, thought: true}`; the answer signature on the last text part, or on a trailing empty part when there is no text |
| OpenAI-compatible | plaintext under `reasoning_content` / `reasoning` / `reasoning_text` | the text, and the field name as `thinking_signature` | the text under that field name |

**OpenAI's own wire has no artifact to keep.** On `gpt-5.6-luna` the Chat
Completions message carries only `annotations`, `content`, `refusal` and `role`.
Its reasoning is not returned, so there is nothing to replay. Replayable
reasoning on OpenAI lives in the Responses API (`encrypted_content`), and τ does
not speak it.

**Google placement was measured both ways.** A signature on the text part and a
signature on a trailing empty part were each accepted, and each was read: 268
prompt tokens, the same number recalled. τ sends it on the last text part, as
non-streamed responses deliver it.

A block that carries an artifact is never dropped for having empty text. Under
`omitted`, an Anthropic block's text is always empty and the signature is the
reasoning. The Anthropic docs name a serializer that skips empty blocks as the
way this goes wrong.

## 6. Replay scope becomes a per-provider default

**Decided: `Model.reasoning_replay` defaults to `None`, meaning the provider's
default. Anthropic replays `"all"`, Google replays `"all"`, and OpenAI-compatible
keeps `"turn"`.**

* **Anthropic replays all.** The docs (thinking, "Thinking block preservation by
  model") say to pass every block back. The API filters them per model, keeps
  what the model reads, and bills only those. Opus 4.5+, Sonnet 4.6+ and Haiku
  5.5 keep all prior turns; earlier models have the API strip them. Deleting them
  in τ edits the history and changes the request prefix at every user turn,
  which costs cache hits on the tool-loop span (`PROMPT-CACHING.md` §8). The old
  docstring claiming "the API discards prior-turn thinking" is removed. It was
  true of the 4.5-era models and is false now.
* **An explicit `"turn"` is honoured on Anthropic.** The preserved-thinking docs
  allow removing blocks from the start of the history. τ removes the prior
  turns' blocks and keeps the current run, so the request stays valid. It is a
  legitimate way to save input tokens. An explicit `"off"` is still refused:
  warned and treated as `"turn"`, or raised under `strict_reasoning_formats`.
  Stripping the current tool loop's blocks discards the reasoning behind the
  calls being answered.
* **An unsigned block from a prior turn is dropped, not sent as text.** It is a
  foreign provider's thinking, or an aborted partial. Under `"all"` it would
  otherwise reach the model as visible prose. Inside the current turn, S2's
  text conversion stands (`ANTHROPIC-GOOGLE-CLIENTS.md`).
* **Local OpenAI-compatible servers keep `"turn"`.** Measured on Qwen3: replaying
  everything brought the payload to 72% thinking. The plaintext is re-tokenized
  as prompt, which is a different trade from an encrypted block that the server
  filters.
* **Google keeps `"all"`.** That is its behaviour today (`!= "off"`), and §1.5
  measures what the answer signature is worth.

## 7. Reasoning tokens are counted separately

**Decided: `Usage.reasoning_tokens: int | None`, the part of `output_tokens`
that was reasoning. `None` means the server reported no split.**

| Wire | Source | Note |
|---|---|---|
| Anthropic | `usage.output_tokens_details.thinking_tokens` | streamed on the final `message_delta` only; always ≤ `output_tokens` |
| OpenAI-compatible | `usage.completion_tokens_details.reasoning_tokens` | absent on llama.cpp → `None` |
| Google | `usage_metadata.thoughts_token_count` | Google omits zero-valued fields, so an absent count with usage present is `0`; still added into `output_tokens`, as before, because Google bills it as output |

`output_tokens` stays the inclusive, billed total on every wire, so pricing is
unchanged. The field is not a fifth priced bucket. The truncation notice quotes
it when the provider reported it, because "the cap was hit" means more when it
says how much of the cap went to thinking (#23 point 4).

## 8. Display: summaries are requested, because they are free

John's condition: "If there's a charge for the summary of thinking, I'd default
to not asking for it."

**Confirmed from the docs, not measured.** The pricing section of "Steering
thinking" (platform.claude.com, read 2026-10-17) has a table whose
"Summary generation" row reads **"No charge"** for `display: "summarized"`. It
also says: "What you're billed for is the same regardless of the `display`
setting; only what you see changes." The live runs cannot confirm it. Thinking
tokens vary from run to run by more than any difference between arms (39–109
tokens at effort medium to xhigh, one run each).

**Decided: τ sends `display: "summarized"` with every adaptive level.** The map
overrides it per level, for an operator who wants `omitted`'s faster first text
token:
`{"medium": {"thinking": {"type": "adaptive", "display": "omitted"}, "output_config": {"effort": "medium"}}}`.
`display` is not sent with `disabled`, which rejects it.

A request with no level (`None`) sends no `thinking` object, so it gets the
API default, which is `omitted` on the 5.5 models. Sending `adaptive` there to
attach `display` would turn thinking on for 4.6-era models, where no field means
off.

## 9. Stop reasons on the OpenAI-compatible wire (#10)

**Decided: an explicit table. Anything not in it is an `"error"` whose message
names the raw value.**

| `finish_reason` | τ `stop_reason` |
|---|---|
| `stop` | `stop` |
| `length`, `model_length` (Mistral: the context window) | `length` |
| `tool_calls` | `toolUse` |
| `content_filter` | `error` |
| `function_call` | `error`: the legacy field τ does not read, so the call would be lost |
| `error` | `error` |
| anything else | `error`, "unhandled finish_reason …" |

This is the rule Google's and Anthropic's providers already follow.
`error_message` is now set on this wire too.

## 10. The heads get a setter

* **RPC `set_reasoning(level)`**, a mutation in the capability registry. It
  calls `AgentSession.set_reasoning` and answers `{level, leaf}`. Like
  `set_auto_compaction`, it appends nothing itself: the next request records
  `thinking` ahead of itself (`TURN-SETTINGS.md` §3). So it answers on an
  unpersisted session too, and `leaf` is the unchanged tip.
  `cycle_thinking_level` stays declined, and its reason now points here.
* **`/thinking <level>`**, a flow over a new `thinking_level` domain. The TUI and
  the REPL perform flows generically, through the backend method named by the
  mutation, so the same declaration gives both heads the command. `/thinking`
  with no argument lists the six levels.
* All three call `AgentSession.set_reasoning`. `TauBackend` and the serve
  `RemoteBackend` each gain a `set_reasoning` that returns a `Performed`.

## 11. What is absent

* **OpenRouter `reasoning_details`.** Its docs specify item types and replay
  ("pass back unmodified"), but not how streamed fragments of one item merge:
  "built by concatenating all chunks in order". No endpoint in reach emits it.
  unorouter returned plaintext `reasoning_content`. A guessed merge would send a
  sequence the server may reject or misread. The vehicle is ready
  (`thinking_signature` namespaced per wire); the merge rule is what is missing.
* **The Responses API.** OpenAI's replayable reasoning lives there. τ speaks Chat
  Completions only. That is a new wire, not a fix.
* **A way back to `None` from the heads.** `/thinking` and `set_reasoning` take
  the six levels. Once a session has set `off`, it cannot return to "provider
  default" without a restart. A seventh token would be a vocabulary change for
  every head, and nothing measured asks for it yet.
* **Anthropic per-message effort** (beta `mid-conversation-output-config`). It
  keeps the prompt cache across an effort change. τ changes the top-level effort,
  which restarts the cache, as the docs say.
* **`xhigh`/`max` without a map.** Anthropic's support varies by model (Opus 4.6
  and Sonnet 4.6 lack `xhigh`), and τ keeps no model-name table
  (`ANTHROPIC-GOOGLE-CLIENTS.md` O2).
* **Showing `reasoning_level` in the TUI.** It is on the message and on the wire;
  rendering it is a TUI display decision, left to the TUI's owner.
