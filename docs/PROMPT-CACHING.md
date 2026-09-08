# Prompt caching

**Built 2026-09-06.** τ sent no cache breakpoint on either wire, so a session
against an Anthropic model was billed full input price on every request —
reported from a real session as 0.0% cached input and roughly 5× the average
cost per message. Every number below was measured on 2026-09-06 against live
endpoints; the probe scripts are named per section.

## 1. What was wrong

Anthropic's prompt cache is opt-in per request. Nothing is cached unless the
request carries a `cache_control` breakpoint, and a request without one performs
no lookup either, so `cache_read_input_tokens` is structurally 0. Grepping the
five `src` trees for `cache_control` on 2026-09-05 found it in no source file.

That is the whole defect. It is not specific to a model, and it is not specific
to the transport: the reporting session ran over a LiteLLM proxy on the OpenAI
wire, and a direct `anthropic-messages` call had the same hole.

OpenAI is unaffected because its caching is automatic — no marker exists to
send.

## 2. The measured matrix

Two calls per cell with an identical 13.4K-token prefix, the second within
seconds of the first. `write` is `cache_creation_input_tokens`, `read` is
`cache_read_input_tokens`.

| Endpoint | Wire | Marker | Call 1 | Call 2 |
|---|---|---|---:|---:|
| api.anthropic.com | `/v1/messages` | none | 0 / 0 | 0 / 0 |
| api.anthropic.com | `/v1/messages` | block `cache_control` | write 13447 | read 13447 |
| api.anthropic.com | `/v1/messages` | top-level `cache_control` | write 13458 | read 13458 |
| localhost:4000 (LiteLLM → Haiku 4.5) | `/chat/completions` | none | 0 / 0 | 0 / 0 |
| localhost:4000 (LiteLLM → Haiku 4.5) | `/chat/completions` | block `cache_control` | write 13447 | read 13447 |
| localhost:4000 (LiteLLM → Haiku 4.5) | `/v1/messages` | block `cache_control` | write 13447 | read 13447 |
| localhost:4000 (LiteLLM → Haiku 4.5) | `/v1/messages` | top-level `cache_control` | write 13458 | read 13458 |
| api.openai.com (GPT-5.6 Luna) | `/chat/completions` | none | write 11781 | read 11781 |
| api.openai.com (GPT-5.6 Luna) | `/chat/completions` | block `cache_control` | write 11781 | read 11781 |

Three readings:

1. **The bug reproduces on both wires**, and only where the marker is absent.
2. **LiteLLM translates an OpenAI-format `cache_control` into Anthropic's native
   caching.** The token counts are identical to the direct call, so nothing is
   lost in the proxy.
3. **OpenAI needs no marker and is unharmed by one.** Its two rows are identical
   to the token.

Script: `stage2_cache.py`.

### Nothing rejects the key

Acceptance was checked separately with short prompts (`stage1_accept.py`). Five
OpenAI-compatible servers were sent a `cache_control` key inside a content block
and a system message as a block list:

| Server | Result |
|---|---|
| api.openai.com | 200, `prompt_tokens` identical to the plain request (27) |
| localhost:4000 (LiteLLM) | 200, translated |
| llama.cpp (Qwen3.8-Flash-Next) | 200, `prompt_tokens` identical (104) |
| api.unorouter.com (nemotron-3-super) | 200, `prompt_tokens` identical (25) |
| api.anthropic.com | 200, native |

Identical `prompt_tokens` is the part that matters: the key is dropped during
parsing rather than serialized into the prompt text. **No endpoint returned an
error.** §5 says why that is still not enough to make markers a default.

## 3. Usage field names

`prompt_tokens` on the OpenAI wire **includes** both the cached and the written
span; Anthropic's `input_tokens` excludes both. Measured on one LiteLLM cold
write and the warm read that followed:

| Field | Cold write | Warm read |
|---|---:|---:|
| `prompt_tokens` | 14116 | 13461 |
| `prompt_tokens_details.cached_tokens` | 0 | 13447 |
| `prompt_tokens_details.cache_write_tokens` | 14113 | 0 |
| `prompt_tokens_details.text_tokens` | 3 | 14 |

`prompt_tokens - cached_tokens - cache_write_tokens` equals the server's own
`text_tokens` in both rows, which is what licenses `_usage_from_openai`'s
subtraction. api.openai.com reports `cache_write_tokens` under the same name;
llama.cpp reports neither, so the reading there is unchanged.

LiteLLM also mirrors the Anthropic names (`cache_creation_input_tokens`,
`cache_read_input_tokens`) at the top level of `usage`. τ reads the nested
OpenAI names because api.openai.com populates only those.

## 4. Where the breakpoints go

`tools` → `system` → `messages` is the render order, so a breakpoint caches the
entire prefix before it. τ places two of the four available, both at the default
5-minute TTL.

The tail marker is the one that matters in an agent loop, and the tail of a τ
loop is a `role: "tool"` message. Measured on one 21058-token request through
LiteLLM to Haiku 4.5, sent twice with only the marker set changing:

| Breakpoints | write | read |
|---|---:|---:|
| system only | 0 | 10736 |
| system + tail (`role: "tool"`) | 3331 | 17721 |
| system + tail, repeated | 0 | 21052 |

So a marker on a tool-result message does survive LiteLLM's translation, and
without it roughly half the prefix is re-billed at full input price every
request. Script: `stage5_toolmark.py`.

A four-request growing conversation shows the steady state: each request read
the whole previous prefix and wrote only the ~1510-token delta appended since.

**The 5-minute TTL is the right one here** and the 1-hour TTL is not. A read
refreshes the entry's timer for free, and requests inside a τ tool loop start
seconds apart, so the 5-minute entry stays warm indefinitely while the 1-hour
TTL costs 2× on every write instead of 1.25×.

**Haiku 4.5's minimum cacheable prefix is 4096 tokens** — the highest of any
current model, and non-monotonic across generations. A prefix below the minimum
caches silently not at all: no error, `cache_creation_input_tokens: 0`.

## 5. Two keys, and why only one wire needs the second

**`prompt_cache` is a boolean and defaults to `true` on every wire.** It asks
for caching. **`prompt_cache_dialect` is `"anthropic"` or unset.** It says how an
OpenAI-compatible endpoint has to be asked, and `Model` already spelled this
concept once — `grammar_dialect` — for the same reason.

The first design put both questions in one key, with values `"anthropic"` and
`"off"`. That mixed a dialect with a state, so `null` meant "on" on one wire and
"off" on the other, and a reader could not tell from the value which question
they were answering. `build_model_from_config` refuses a string now and names
the replacement, because `"off"` coerces to `False` and `"anthropic"` to a
truthy nothing — both of which read as working while the marker goes unsent.

`anthropic-messages` reads only the boolean. There `cache_control` is a
top-level parameter of the one API that module speaks and the SDK declares it,
so there is no dialect to state and nothing is written into the body.

`openai-completions` reads both, and sends nothing without the dialect. That
asymmetry is about where the key has to travel, not about the risk measured in
§2: on this wire there is no top-level field, so the marker must go **inside
`messages`** — and `messages` is in `_RESERVED_BODY_KEYS`, which means an
operator who lands on a server that does reject it has no config-level way to
take it back out. Four servers were measured to ignore it; the population of
OpenAI-compatible servers is not four.

Detecting the dialect from the provider id or the model id was rejected. That is
the failure in <https://github.com/can1357/oh-my-pi/issues/1845>: the same model
id is served by gateways that speak different caching dialects, and the base URL
does not say which. The operator declares it.

## 6. Configuration

Anthropic backend — caching is already on; the only entry needed is the opt-out:

```json
"haiku-4.5": {
  "backend": "anthropic",
  "model": "claude-haiku-4-5-20251001",
  "api_key": "sk-ant-…"
}
```

The same model behind a LiteLLM gateway on the OpenAI wire — the dialect is
required, because nothing about this entry tells τ that `localhost:4000` routes
to Anthropic:

```json
"litellm-haiku": {
  "backend": "openai",
  "model": "claude-haiku-4-5-20251001",
  "base_url": "http://localhost:4000/v1",
  "api_key": "sk-…",
  "prompt_cache_dialect": "anthropic"
}
```

An OpenAI model, through the same gateway or directly — **leave the dialect
unset.** OpenAI caches automatically above 1024 tokens and a marker buys
nothing:

```json
"litellm-luna": {
  "backend": "openai",
  "model": "gpt-5.6-luna",
  "base_url": "http://localhost:4000/v1",
  "api_key": "sk-…"
}
```

## 7. Reading the result

The TUI's token row shows `R` (cache read) and `W` (cache write) per completion.
Before this change `W` was always 0 on the OpenAI wire because
`_usage_from_openai` never set it, so a session paying the write premium and
never reading it back looked free. That is the one losing regime — writing a
prefix that is never read — and it is now visible rather than silent.

`R` at 0 across a long session on a model that should be caching means the
prefix is being rewritten between requests. §8 lists the invalidators.

### The notice

τ now says so rather than leaving it to be noticed on a bill. `cache_miss_reason`
(`tau_agent_core/prompt_cache.py`) reads each turn's completions and returns a
sentence, or None:

| Condition | Why it is a finding |
|---|---|
| Two or more calls in the turn, every call after the first read 0 | The prefix demonstrably did not change between them; this cannot be a false positive |
| The first call read 0, less than 300s after the previous turn's last completion | Inside the 5-minute entry's life, so it should have been there |

Both require the last call's prompt to exceed 4096 tokens, which is the highest
minimum cacheable prefix of any current model (§4) — below it nothing caches and
a notice would be noise.

### A 0 is not a miss unless the server counts

Both conditions also require every completion in the turn to carry
`Usage.cache_reported`: the server accounted for a prompt cache at all. **A 0 in
`cache_read_tokens` says two different things** — "this server has a cache and
read nothing from it", which is the finding, and "this server has no cache",
which is not. §3 measured llama.cpp reporting neither `cached_tokens` nor
`cache_write_tokens`, and τ's default model is `local-llm` pointing at exactly
such a server, so without the gate the first local turn over 4096 tokens with
two tool calls produces a notice telling its reader to configure a gateway they
are not using.

The distinction has to be made in the provider, because by the time a `Usage`
reaches the head both cases are the integer 0. `_usage_from_openai` sets the flag
from whether either key was **present** in `prompt_tokens_details`;
`anthropic-messages` and `google-generative-ai` do the same against their own
field names. The default is `False`, so a provider that says nothing produces no
notice — absence of evidence never manufactures a finding.

This is also what lets the notice say something definite. It no longer has to
offer "or your server may have no cache" as an alternative reading, because that
reading is now excluded before the sentence is written.

**It is gated on the result, never on the configuration.** Gating on
`models.<name>.prompt_cache` was the first design and it is backwards: a model
that declares the dialect is a model someone already configured correctly, while
the case worth reporting — an OpenAI-compatible gateway that drops
`cache_control` on its way to the Anthropic model it proxies — has nothing
declared to gate on. The reported session that started this work would have
produced no notice under that gate.

### One read closes the question

`PromptCacheObserver` holds the two things a pure function cannot, and the first
is a latch: **one `cache_read_tokens` above 0, anywhere in the session, silences
the observer permanently.** A read proves caching is enabled, and being off is
the misconfiguration this reports; a later cold turn is then an expired entry,
which is ordinary.

The latch subsumes most of the cross-turn condition, and that is the right
reading rather than a redundancy. If a later call in the turn read, caching is on
and there is nothing to report; if none did, the within-turn condition already
fires. What the cross-turn condition still carries alone is the **one call per
turn** shape — a chat-shaped session, where the within-turn condition can never
have two calls to compare.

### The clock is per prefix

The second is the gap the cross-turn condition needs, and it used to be one
`_last_completion_ms` attribute on `RenderRouter`, written by every lane. A
sub-agent branch closing 9s before a user turn therefore supplied that turn's
gap, though the two share no prompt prefix — measured by driving a synthetic
router: the conversation's own previous completion was 609s back (outside the
TTL, so a cold read is expected) and the notice fired on the branch's 9s.

A caller now names the prefix a turn belongs to. `RenderRouter` names
`CONVERSATION_PREFIX` for a submission lane and nothing at all for a
`branch:` lane, which neither reads nor writes a clock — a sub-agent's lane
exists for exactly one span, so there is no next turn on its prefix to compare
against. `PromptCacheObserver.feed_event` applies the same rule by a second
route: a turn whose events carry no `submission_id` is an LLM-backed compaction
or a `continue_conversation()` resume, running a loop on a prompt that is not the
conversation's, so it leaves no clock behind either.

### Three deliveries, one verdict

The reading is head-agnostic; only the delivery is not.

| Head | Delivery |
|---|---|
| TUI | `ChatDisplay.add_message("system", …)` — the display-only chrome `/extensions` uses: not a conversation node, not persisted, not sent to the model. **Once per model per session**, because a line repeated every turn is one a reader stops seeing |
| `tau -p` | Two lines on **stderr**, in both `--mode text` and `--mode json`. stdout is a transcript in one mode and JSONL in the other, and both are routinely redirected, so a diagnostic there would be corruption. One process is one turn, so only the within-turn condition can fire and there is nothing to suppress |
| `tau --mode rpc` | `WireEvent.cache_notice`, a string on `agent_end` beside `message_count`. `null` is the normal case. A host renders it however it likes; the handler owns one observer for the connection, so the latch holds across the session the same way |

The once-per-model rule is the TUI's alone, and deliberately: it is a display
policy, not a reading. The evidence gates are the observer's and are identical in
all three.

## 8. What is NOT fixed

**`reasoning_replay: "turn"` rewrites the message prefix at every user turn.**
`_convert_messages_to_openai` keeps thinking blocks while `index > last_user_idx`
and drops them once a newer user message arrives; tool results do not advance
`last_user_idx`, so the whole tool loop replays its reasoning and then the next
user message removes it, changing the rendered bytes from the first assistant
message onward. `inferred` from the code at `openai.py:751-756`, not measured on
the wire.

The cost depends entirely on session shape. On the reported session — ~200
requests across ~5 user messages, so ~40 requests per turn — it invalidates the
cache 5 times out of 200 and is not worth changing. On a chat-shaped session it
would invalidate every request. Re-measure before changing the default;
`docs/` records the 72%→28% payload reduction that made `"turn"` the default in
the first place, and that trade has not been re-run against caching.

**Not measured:** Bedrock, Vertex, Azure, or any OpenAI-compatible server beyond
the four in §2. `tool_use`/`tool_result` block-level markers on the
`anthropic-messages` wire (τ uses the automatic top-level breakpoint there
instead). Whether unorouter's `cached_tokens` field ever becomes non-zero.
