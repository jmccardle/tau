# Provider coverage: what τ speaks, what it should, and how to prove it

Research record (2026-10-12), partly built. Nothing in `tau_llm/providers/` was
changed. What exists is a prototype of §5's contract suite: `tau-llm/tests/test_provider_contract.py`
and twelve **synthetic** cassettes under `tau-llm/tests/cassettes/`. Every claim
below is marked **ran**, **read** (with `path:line`), **inferred** or **assumed**.
Vendor facts cite a URL and the date it was read.

## 1. What is wrong, measured

τ speaks three wire protocols: `openai-completions`, `anthropic-messages` and
`google-generative-ai` (`tau-llm/src/tau_llm/providers/__init__.py:133-135`, read).
pi's checkout has 44 provider factories (`packages/ai/src/providers/all.ts:5-46`)
over ten chat protocols (§2.1). That gap matters less than the count suggests,
because most of pi's providers are OpenAI-compatible endpoints that τ reaches with
config alone (§2.2).

The real defects are inside the three wires τ already ships. Two come from the
vendors' current docs (§4); both are inferred, because no paid call was made.

- **First-party OpenAI.** τ speaks Chat Completions there, and OpenAI's current
  reasoning flagships do not support tool calling on that API (§4.1). τ cannot
  run its tool loop against them.
- **Anthropic 5.x models.** These default `thinking.display` to `omitted`. τ does
  not set it, so they return empty thinking text (§4.2).

The rest were found by the prototype suite in §5, which runs one contract against
all three providers through each provider's real transport.

- **The three providers disagree about an unknown stop reason.**
  - Anthropic and Google map an unmapped value to `error`
    (`providers/anthropic.py:798-803`, `providers/google.py:665-678`, read).
  - OpenAI maps `content_filter`, and **any** value it has not seen, to `stop`
    (`providers/openai.py:1248-1258`, read).
  - Ran: a `content_filter` cassette comes back `stop_reason == "stop"`, so a
    filtered answer is presented as complete. Mistral's `model_length` (§3.3)
    takes the same path, so a truncated answer reads as finished.
- **One of the three providers retries behind the caller's back.**
  - **Resolved 2026-10-14** by `docs/PROVIDER-RETRY.md`: `AsyncAnthropic` is now
    built with `max_retries=0` and τ's own retry layer decides. The contract
    suite's 429 clause passes on all three wires. The record below is as measured.
  - `docs/PROVIDER-HTTP-ERRORS.md` §6 says "No retry policy. τ reports the status."
  - `AsyncAnthropic` is built without `max_retries` (`providers/anthropic.py:212-216`,
    read), so it keeps the SDK default of 2 (ran: `anthropic._constants.DEFAULT_MAX_RETRIES == 2`
    on anthropic 1.0.0).
  - The module docstring accepted this when the client was built ("a second HTTP
    stack, with its own pooling and retries", `providers/anthropic.py:22-23`,
    read). The later error record did not notice it, so the two documents now
    disagree.
  - Ran: a 429 cassette produces **3 requests** for one call before
    `ProviderHttpError` surfaces.
  - google-genai's default is one attempt (ran: `retry_args(None)` returns
    `stop_after_attempt(1)` on google-genai 2.19.0). The OpenAI provider has no
    retry. So the same 429 costs 1, 3 or 1 requests depending on the wire.
- **No wire reports reasoning tokens.**
  - `Usage` has no such field (`tau-llm/src/tau_llm/types.py:88-97`, read).
  - OpenAI's `completion_tokens_details.reasoning_tokens` is never read
    (`providers/openai.py:577-600`, read).
  - Google's `thoughts_token_count` is folded into `output_tokens`
    (`providers/google.py:752-760`, read). The bill is right, but the split is lost.
- **Gemini ignores the thinking level.**
  - Every level other than `off` sends the same `{"include_thoughts": true}`
    (`providers/google.py:443-455`, read).
  - `thinking_level_map` is not consulted on this wire, so `low` and `high` are the
    same request.
- **A Mistral reasoning model crashes τ.** Mistral streams its thinking as a
  *list* in `delta.content`, and τ's OpenAI parser assumes a string
  (`providers/openai.py:1764`, read). Ran, against a synthetic chunk in Mistral's
  documented shape: `TypeError: sequence item 0: expected str instance, list found`.
  It fails loudly, so nothing is wrong silently, but the model is unusable.
- **The per-provider tests never run the vendor SDK's own parser.**
  - `test_anthropic_provider.py` installs a `_FakeClient` that yields SDK-shaped
    event objects (`tau-llm/tests/test_anthropic_provider.py:200-218`, read).
  - The SDK's SSE decoding, its accumulation and its retry layer are therefore
    untested against τ's consumer.
  - The prototype drives `anthropic` 1.0.0 and `google-genai` 2.19.0 end to end
    over a mock transport, and that is how the retry finding surfaced.

## 2. Inventory: pi's providers against τ

### 2.1 Distinct wire protocols

These are pi's protocol implementations, from `packages/ai/src/api/` (read). Each
one is either a separate protocol or a quirk set on top of one.

| pi `api` | lines | What it is | τ |
|---|---|---|---|
| `openai-completions` | 1669 | Chat Completions plus pi's compat flag vocabulary | **native** (`openai.py`) |
| `anthropic-messages` | 1391 | Messages API | **native** (`anthropic.py`, via the SDK) |
| `google-generative-ai` | 526 + 452 shared | Gemini API | **native** (`google.py`, via the SDK) |
| `google-vertex` | 598 + shared | Same body; Vertex endpoint, ADC/OAuth or API key, project + location | **absent**: separate auth, same payload (§4.3) |
| `openai-responses` | 376 + 792 shared | OpenAI Responses API | **absent**: separate protocol (§4.1) |
| `azure-openai-responses` | 338 + shared | Responses on Azure deployments, `api-key` header, `api-version` | **absent** |
| `openai-codex-responses` | 1650 | Responses over ChatGPT OAuth, WebSocket with SSE fallback | **absent**: subscription auth |
| `bedrock-converse-stream` | 1325 | AWS Converse, SigV4 or bearer token | **absent**: separate protocol and auth |
| `mistral-conversations` | 936 | Mistral chat completions via `@mistralai/mistralai` | **absent**; OpenAI-compatible with quirks (§3) |
| `pi-messages` | 433 | pi's own Radius service | n/a |
| `openrouter-images` | 196 | Image generation | n/a (not a chat wire) |

### 2.2 Every pi provider, classified

Classes:

- **a**: τ reaches it natively.
- **b**: τ reaches it through `openai-completions` with config only (`base_url`,
  `api_key`, and `thinking_level_map` fragments or `extra_body`).
- **c**: τ reaches it through `openai-completions`, but loses a quirk pi handles in code.
- **d**: not reachable.

The provider rows are read from `pi/packages/ai/src/providers/*.ts` and
`api/openai-completions.ts`. pi's per-model compat values come from
`scripts/generate-models.ts` (`G:`) because the generated `data/*.json` is not in
the checkout.

| pi provider | pi `api` | Auth | τ | What is missing |
|---|---|---|---|---|
| openai | openai-responses | `OPENAI_API_KEY` | **a** (Chat Completions) | Responses-only features (§4.1) |
| anthropic | anthropic-messages | API key; `ANTHROPIC_AUTH_TOKEN` bearer; Pro/Max OAuth (`anthropic-messages.ts:874,928-936`) | **a** for the API key | Bearer token and OAuth: **d** |
| google | google-generative-ai | `GEMINI_API_KEY` | **a** | Thinking level ignored (§4.3) |
| google-vertex | google-vertex | ADC, or `GOOGLE_CLOUD_API_KEY` + project/location (`google-vertex.ts:72-95`) | **d** | google-genai's `vertexai=True` mode; never wired |
| amazon-bedrock | bedrock-converse-stream | SigV4 / profile / bearer (`amazon-bedrock.ts:56-77`) | **d** | The whole protocol |
| azure-openai-responses | azure-openai-responses | `AZURE_OPENAI_API_KEY`, resource, api-version (`:227-246`) | **d** | Responses wire, `api-key` header (inferred), deployment map |
| openai-codex | openai-codex-responses | ChatGPT OAuth only | **d** | OAuth + Responses + WebSocket |
| github-copilot | completions / responses / anthropic, per model | Copilot token or OAuth | **d** | Editor headers (G:145-148), OAuth, one `api` per provider id |
| xai | openai-responses | `XAI_API_KEY` | **c** | Encrypted reasoning replay (`openai-responses.ts:338`) |
| mistral | mistral-conversations | `MISTRAL_API_KEY` | **c** | 9-char tool-call ids, thinking chunks, `model_length` (§3) |
| openrouter | openai-completions | `OPENROUTER_API_KEY` | **c** | `reasoning_details` round trip (C:1235-1307); `provider` routing is reachable by `extra_body` |
| deepseek | openai-completions | `DEEPSEEK_API_KEY` | **c** | `requiresReasoningContentOnAssistantMessages` (C:1309-1315) against τ's default `reasoning_replay="turn"` (inferred: a 400 on tool turns) |
| xiaomi ×4 | openai-completions | `XIAOMI_*` | **c** | Same as deepseek (G:2187) |
| moonshotai ×2 | openai-completions | `MOONSHOT_API_KEY` | **b**; **c** for kimi-k3 | kimi-k3 reasoning_content + deferred-tools mode (G:2154-2159) |
| nvidia | openai-completions | `NVIDIA_API_KEY` | **c** | `NVCF-POLL-SECONDS` header (G:206): τ has no header knob |
| fireworks | anthropic-messages + completions | `FIREWORKS_API_KEY` | **c** | Session-affinity header; two provider ids by hand |
| minimax ×2 | anthropic-messages | `MINIMAX_API_KEY` | **c** (inferred) | τ sends only `thinking: adaptive` (`anthropic.py:563`); a third-party Messages server may reject it |
| kimi-coding | anthropic-messages | `KIMI_API_KEY` / OAuth | **b** for the key | OAuth |
| vercel-ai-gateway | anthropic-messages | `AI_GATEWAY_API_KEY` | **b** | — |
| opencode, opencode-go | anthropic / google / completions / responses, per model | `OPENCODE_API_KEY` | **c** | One `api` per provider id (`client.py:83-89`); responses models **d** |
| cloudflare-workers-ai | openai-completions | key + account id | **b** | Account id written into `base_url` |
| cloudflare-ai-gateway | anthropic / completions / responses | `cf-aig-authorization` header (`cloudflare-auth.ts:85-93`) | **d** | Custom auth header |
| groq, cerebras, together, baseten, huggingface, ant-ling, zai ×2, qwen ×3 | openai-completions | bearer key | **b** | Each one's thinking switch is a static `thinking_level_map` fragment |
| radius | pi-messages | Radius key / OAuth | **d** | pi's own service |
| openrouter-images | images | — | **d** | Not a chat wire |
| faux | faux | none | n/a | A scripted test provider; §5's cassettes do the same job |

Totals over the 44: a = 3, b ≈ 17, c ≈ 15, d ≈ 9. This is inferred from the
table; several "b" rows have never been pointed at the real endpoint.

### 2.3 What closes the most rows

Ranked by rows reached per unit of work. Inferred from §2.2.

1. **Per-model `headers`, plus a configurable auth header.**
   - τ hardcodes `Authorization: Bearer` (`providers/openai.py:762-765`, read) and
     `Model` has no headers field (`types.py:163-192`, read).
   - This reaches nvidia, Cloudflare AI Gateway, the Fireworks/Cloudflare affinity
     headers, Azure's `api-key`, and `ANTHROPIC_AUTH_TOKEN`.
2. **A `reasoning_content` replay requirement.** pi calls it
   `requiresReasoningContentOnAssistantMessages`. It covers DeepSeek, Xiaomi, Kimi
   K3, and some OpenRouter models.
3. **Vertex for Gemini.** It is the same SDK τ already uses. Cost is in §6.
4. **The OpenAI Responses wire.** One protocol: first-party OpenAI reasoning
   models, xAI, and the Responses half of opencode and Copilot.
5. **Bedrock and OAuth subscriptions.** Each is a separate auth world. ROADMAP's
   "M4 registerProvider" stance (provider work is tau-llm's) applies.

Two things the inventory surfaced in passing (read, not part of the brief):

- `tau_coding_agent/backends.py:1023` silently falls back to `https://api.openai.com/v1` when a
  config entry has no `base_url`.
- `tau_coding_agent/backends.py:1084` still defaults `context_window` to 128000, which
  `catalog.py:290-295` names as the failure class it exists to avoid.

Both are Fail-Early violations of the `catalog.py` kind. The `catalog.py:278`
docstring is also stale: it still says "openai-completions only".

## 3. The Mistral branch

### 3.1 It could not be read from here

- The brief says the private Forgejo repo carries a branch for Mistral support.
- No local ref names one: `git branch -a` and `git log --all | grep -i mistral`
  (ran) find nothing.
- `origin` is reachable only over ssh, and ssh is blocked in this sandbox (ran:
  `Could not read from remote repository`).
- HTTPS needs credentials (ran: `could not read Username`).
- The Forgejo MCP tool exposes issues, PRs, releases, wiki pages, labels,
  milestones, action tasks and repository metadata, but **no branch, commit or
  file resource** (ran: `gitea_manual(action="get")` lists `issue`,
  `pull_request`, `repository`, `wiki_page`).
- The repository has 0 PRs and 0 action tasks (ran), so no PR diff exists to read
  either.
- I did not pull the MCP server's token out of local config to call the REST API
  directly. That is a credential the session was not handed.

So there is **no evaluation of the branch's diff in this record**. §3.2–§3.4 are
what any such branch has to get right, which is the checklist to review it against.

### 3.2 Verdict on the question the branch answers

**OpenAI-compatible with quirks, not its own provider.** Inferred from pi's
implementation and Mistral's docs (§4.5).

- Mistral's endpoint is `POST /v1/chat/completions`: bearer auth, SSE, OpenAI
  `messages`/`tools`/`tool_calls`.
- pi gave it a separate `api` only because it used Mistral's SDK
  (`mistral-conversations.ts`, 936 lines). Almost all of the file is
  re-implementing what `openai-completions` already does.
- The quirks pi handles in code are each small and each belongs in τ's existing
  quirk machinery (`compat.py`, `Model`).

### 3.3 The quirks any Mistral change must handle

1. **Tool-call ids are 9 alphanumeric characters.**
   - pi rewrites every replayed id through a collision-checked hash map
     (`mistral-conversations.ts:26,232-258`, read).
   - Ids τ mints for other providers (`call_…`, `toolu_…`) are rejected by
     Mistral. Inferred from pi's code; the vendor docs say `tool_call_id` must
     match an id Mistral generated (§4.5).
   - This matters to τ specifically, because a session switched to Mistral
     mid-conversation replays foreign ids. It belongs at the message-conversion
     boundary as a `Compat` field (`tool_call_id_format: "mistral9"`), with the
     mapping deterministic per session so the prefix cache survives.
2. **Thinking arrives as content chunks.**
   - `delta.content` is a list of `{"type": "thinking", "thinking": [{"type": "text", …}]}`
     (`mistral-conversations.ts:43,644-649`, read).
   - τ crashes on it (§1, ran).
   - Replaying it requires the same chunk shape back, which no τ
     `reasoning_replay` path writes today (inferred).
3. **`finish_reason: "model_length"`** means the context window ran out
   (`mistral-conversations.ts:927`, read). τ maps it to `stop` (§1).
4. **Reasoning switches differ per model.**
   - pi sends `prompt_mode: "reasoning"` to Magistral and `reasoning_effort` to
     three named models (`mistral-conversations.ts:893-899`, read).
   - Mistral has since deprecated `magistral-*` and documents `reasoning_effort`
     generally (§4.5).
   - Either is a string or a `thinking_level_map` fragment today. No code is needed.
5. **No `stream_options`** in Mistral's request schema (§4.5).
   - τ sends it by default (`compat.py:139`). Whether Mistral rejects it or ignores
     it is not documented; that is unverified.
   - `compat.supports_usage_in_streaming: false` exists for exactly this case.

Cached-token usage arrives under six possible spellings, and pi reads all of them
(`:537-555`). τ reads `prompt_tokens_details.cached_tokens` only (`openai.py:580`).
Inferred: low impact.

### 3.4 Mergeability against current master

Master now carries `e8a9bd9` (`ProviderHttpError`). A branch that predates it and
adds a provider class will conflict at the error path. Assumed; the diff was not
seen. Specifically:

- every non-2xx must raise `ProviderHttpError`, and must not yield an `ErrorEvent`
  (`docs/PROVIDER-HTTP-ERRORS.md` §2);
- a class built on `mistralai`'s SDK needs a `http_error_from_mistral` beside the
  two in `tau_llm/errors.py`, plus a `retries` decision (§1);
- a branch that instead extends `compat.py` inherits all of this from
  `openai.py:1643-1663` for free. That is one more reason for §3.2's verdict.

What to check when the branch is reviewed:

- the quirks of §3.3, each with a cassette under the contract suite (§5);
- `model_length` mapping to `length`;
- ids that survive a cross-provider replay;
- the `ProviderHttpError` path.

## 4. The three official APIs, feature by feature

All vendor docs were read on 2026-10-12. Where the fetch tool returned a summary
of a page too long to read whole, the claim says *(summary)*. Model ids come from
the docs and not from memory, because memory was wrong:

- OpenAI: `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-luna` (https://developers.openai.com/api/docs/models).
- Anthropic: `claude-opus-5-5`, `claude-fable-5-1`, `claude-sonnet-5-5`, `claude-haiku-5-5` (https://platform.claude.com/docs/en/about-claude/models/overview).
- Gemini stable: `gemini-3.8-flash` and `gemini-3.6-flash`; preview: `gemini-3.1-pro-preview` (https://ai.google.dev/gemini-api/docs/models, page dated 2026-10-09).

### 4.1 OpenAI, and the Responses question

**This is the headline of the record.** τ speaks Chat Completions to
api.openai.com. OpenAI now says:

- "Starting with GPT-5.4, Chat Completions does not support tool calling with
  `reasoning_effort` values other than `none`" *(summary)*.
- "GPT-6 Astra and GPT-6.1 Sol require the Responses API for tool calling"
  (https://developers.openai.com/api/docs/guides/function-calling).
- Chat Completions "remains supported, Responses is recommended for all new
  projects". It is not on the deprecations page
  (https://developers.openai.com/api/docs/guides/migrate-to-responses,
  https://developers.openai.com/api/docs/deprecations).

So **τ cannot run a tool loop on OpenAI's current reasoning flagships**. This is
inferred from the docs; no paid call verified it. τ's OpenAI support is really
OpenAI-*compatible* support, which is what τ's default local model needs, and
that is why nobody noticed.

| Feature | Vendor (2026-10-12) | τ today |
|---|---|---|
| Streaming | Chat: `delta.{content,refusal,tool_calls}`. Usage arrives on the last chunk only with `stream_options.include_usage`. Responses: semantic events (`response.output_text.delta`, `response.function_call_arguments.delta`, …) | Chat SSE parsed; `include_usage` sent when compat allows (`openai.py:1519-1520`) |
| Tool calling | `parallel_tool_calls`, `strict: true` (all fields required, `additionalProperties: false`), `tool_choice` incl. `allowed_tools` | Plain `function` tools, no `strict` (`openai.py:1214-1238`); `tool_choice` only through decode constraints (`openai.py:330`) |
| Reasoning | Chat `reasoning_effort` / Responses `reasoning.effort` ∈ `none…max`, per model; summaries; `encrypted_content` on reasoning items when `store: false` | `reasoning_effort` string or a body fragment (`openai.py:1529-1540`); no encrypted reasoning (no Responses) |
| Prompt caching | Automatic at ≥1024 tokens. GPT-5.6+: `prompt_cache_options.ttl: "30m"` and writes billed 1.25×. Older models: `prompt_cache_retention`, and `prompt_cache_key` affects routing (https://developers.openai.com/api/docs/guides/prompt-caching) | Nothing sent, which is correct for automatic caching; `cached_tokens` and `cache_write_tokens` read (`openai.py:577-582`). No `prompt_cache_key`, so a tree of sibling sessions cannot share a routing key (inferred) |
| Structured output | `response_format: json_schema` (Chat), `text.format` (Responses) | `response_format` sent only for llama.cpp-dialect constraints; refused unless `grammar_dialect` is set (`openai.py:341-345`), so OpenAI's strict JSON schema is unreachable |
| Images / PDF | `image_url` data URIs; PDF as a `file` part (Chat) or `input_file` (Responses) | Images as data URIs (`openai.py:682,902`); no PDF/document block type in τ (`types.py`, read) |
| Token counting | `POST /v1/responses/input_tokens` (https://developers.openai.com/api/docs/guides/token-counting) | None; R2 plans `/tokenize` for llama.cpp only (ROADMAP) |
| Usage | `prompt_tokens_details.cached_tokens`, `completion_tokens_details.reasoning_tokens` | cached yes, reasoning **no** (§1) |
| Stop reasons | `stop`, `length`, `tool_calls`, `content_filter`, `function_call` (deprecated) | `content_filter` and unknown → `stop` (§1) |
| Errors / rate limits | `error.{message,type,param,code}`; `x-ratelimit-*`, `Retry-After` (https://developers.openai.com/api/docs/guides/rate-limits) | `ProviderHttpError` keeps status, message and body (`openai.py:1643-1663`); **no headers kept**, so `Retry-After` is lost |

### 4.2 Anthropic Messages

τ uses the official SDK (`docs/ANTHROPIC-GOOGLE-CLIENTS.md` §5). That buys SSE
parsing and block accumulation, but not the request decisions. Three of those
decisions are now wrong for the current models:

- **`thinking.display` defaults to `omitted` on the 5.x models.** Thinking blocks
  then arrive with empty text and only a `signature`
  (https://platform.claude.com/docs/en/build-with-claude/thinking).
  - τ sends `{"type": "adaptive"}` with no `display` (`anthropic.py:563`, read).
  - Its module docstring even names the default change (`anthropic.py:18`).
  - Inferred: a τ user on Opus 5.5 sees no reasoning text at all. The signature is
    still captured, so replay keeps working.
  - Fix: one key, `display: "summarized"`, or a `Model` knob for it.
- **`"off"` sends `thinking: disabled`, which Opus 5.5 and Fable 5.1 reject.**
  Sonnet 5.5 wants `between_tools`.
  - τ chose to send it and accept the 400 (`anthropic.py:539-543`, read). That is
    Fail-Early-correct.
  - But "off" is now unreachable on three of the four flagships, and the 400 is
    the only thing that says so.
- **Prior-turn thinking is no longer discarded by the API.** Opus 4.5+, Sonnet
  4.6+ and the 5.x models keep every prior turn.
  - τ's `_replay_scope` overrides any `reasoning_replay` to `"turn"` on the stated
    grounds that "blocks from prior turns are discarded by the API regardless"
    (`anthropic.py:231-262`, read). That premise is now false.
  - Inferred, not measured: dropping those blocks rewrites the prefix at every user
    turn and costs a cache read each time. That is `PROMPT-CACHING.md` §8's open
    item, now with a mechanism.
  - Docs: "binding" makes replayed blocks valid only while the prefix is
    unchanged, and new accounts get a 400 on a mismatch.

| Feature | Vendor (2026-10-12) | τ today |
|---|---|---|
| Streaming | `message_start` → blocks (`text_delta`, `input_json_delta`, `thinking_delta`, `signature_delta`) → `message_delta` (cumulative usage) → `message_stop`; `ping`; in-stream `error` | Via `client.messages.stream()` (`anthropic.py:651-669`); the contract suite drives the real SDK (§5) |
| Tool calling | `tool_choice` `auto` / `any` / `tool` / `none`, `disable_parallel_tool_use` inside it; `any`/`tool` → 400 on Opus 5.5, Sonnet 5.5, Fable 5.1; `strict: true` GA | Tools converted with `input_schema` only (`anthropic.py:512-525`); no `strict`, no `tool_choice` (constraints refused, `anthropic.py:591-596`) |
| Thinking | `adaptive` + `output_config.effort` `low…max`; `budget_tokens` → 400 on 4.7+; `display`; signatures; `redacted_thinking` | adaptive + effort (`anthropic.py:529-563`); signatures and `redacted_thinking` round-trip (`anthropic.py:430-456`); no `display` (above) |
| Prompt caching | Top-level `cache_control` = automatic caching (uses 1 of 4 breakpoints); `ttl` `5m`/`1h`; minimum 512 tokens on 5.x | Top-level ephemeral on every request (`anthropic.py:621-625`); no `1h` TTL knob except `extra_body` |
| Structured output | `output_config.format` json_schema, GA | Refused: "no decode-constraint parameter" (`anthropic.py:591-596`), **stale** — the parameter now exists |
| Images / PDF | base64 / URL image blocks; `document` blocks for PDF | Images (`anthropic.py:846`); no PDF |
| Token counting | `POST /v1/messages/count_tokens`, free, separate RPM | None (R2) |
| Usage | `input_tokens` excludes cache; `cache_creation_input_tokens`, `cache_read_input_tokens`, per-TTL split | Partitioned correctly (`anthropic.py:818-842`); TTL split dropped |
| Stop reasons | `end_turn`, `max_tokens`, `stop_sequence`, `tool_use`, `pause_turn`, `refusal`, `model_context_window_exceeded` | First five mapped; `refusal` → error with `stop_details` (`anthropic.py:791-797`); others → error (`:798-803`). `model_context_window_exceeded` arguably wants `length`, and `pause_turn` only arises with server tools τ does not send |
| Errors / rate limits | `{"type":"error","error":{type,message},"request_id"}`; 529 `overloaded_error`; `retry-after`, `anthropic-ratelimit-*`; a spend-cap 429 has no `retry-after` | `ProviderHttpError` (`errors.py:60`); the SDK retries 2× first (§1); headers and `request_id` not kept |

### 4.3 Google: Gemini API and Vertex

Google now leads its docs with an **Interactions API** (`/interactions`, beta).
`generateContent` "remains fully supported"
(https://ai.google.dev/gemini-api/docs/migrate-to-interactions.md.txt), and τ
uses `generateContent` through the SDK. Vertex AI is renamed Gemini Enterprise
Agent Platform. Its host is still `aiplatform.googleapis.com`, with OAuth bearer
auth from ADC, and API keys only in Express Mode
(https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations).

| Feature | Vendor (2026-10-12) | τ today |
|---|---|---|
| Streaming | `:streamGenerateContent?alt=sse`; chunks carry candidates; signatures may arrive on empty-text parts | SDK `generate_content_stream` (`google.py:523-527`); signatures captured per part (`google.py:604-622`) |
| Tool calling | `functionCallingConfig.mode` `AUTO` / `ANY` / `NONE` / `VALIDATED`, `allowed_function_names`; no parallel flag; `streamFunctionCallArguments` (preview) | Declarations only, SDK auto-calling disabled (`google.py:431-441,498`); no `tool_config` |
| Thinking | Gemini 3+: `thinkingLevel` `MINIMAL…HIGH` per model; `thinkingBudget` deprecated there, and **setting both is an error**; `includeThoughts` | `include_thoughts` only; `off` sends `thinking_budget: 0` (`google.py:453-455`). Inferred: on 3.1 Pro (minimum `LOW`) "off" cannot be honoured, and every on-level is the same request |
| Thought signatures | Required on function calls (first part of a parallel set; every part of a sequential one); 400 / `MISSING_THOUGHT_SIGNATURE` when missing | Placed on the first call of a step and persisted base64 (`google.py:308-376`, `:94-117`). A sequential call is its own model step, so "first call of each step" matches the doc's rule (inferred); no cassette checks it yet (§5.4 item 9) |
| Prompt caching | Implicit on 2.5+ (minimum 4096 on 3.x; Vertex says 6144 for 3.7/3.8 Flash and 3.1 Pro); explicit `cachedContents` API (not in Interactions) | Implicit only; `cached_content_token_count` read (`google.py:755-761`); no explicit caching |
| Structured output | `responseMimeType` + `responseJsonSchema` | Refused (S6, `google.py:27`) |
| Images / PDF | `inline_data` up to 20 MB per request; PDF 1000 pages | Images as `inline_data` (`google.py:302-304`); no PDF |
| Token counting | `models/{m}:countTokens` | None (R2) |
| Usage | `promptTokenCount` (includes cached), `thoughtsTokenCount`, `toolUsePromptTokenCount`, … | Partitioned; thoughts folded into output (§1); `toolUsePromptTokenCount` ignored |
| Stop reasons | 18+ values incl. `MISSING_THOUGHT_SIGNATURE`, `TOO_MANY_TOOL_CALLS`, `MALFORMED_RESPONSE` | 8 mapped; any other → `error` with the raw name (`google.py:78-87,665-678`), which is correct |
| Errors | 429 `RESOURCE_EXHAUSTED` (also spend limits); `RetryInfo` in details (forum/GitHub only) | `ProviderHttpError` (`errors.py:83`); details kept only inside `body` |
| Vertex | Project + location + ADC; same payload | **Absent**. google-genai has `vertexai=True, project=, location=` (assumed from the SDK; not exercised); `_get_client` passes only `api_key` (`google.py:160-162`) |

### 4.4 Cross-vendor gaps, in one place

- **Rate-limit headers.** No wire keeps response headers on `ProviderHttpError`
  (`errors.py:40-53`, read). `Retry-After` / `retry-after` is the one header a
  caller deciding retries needs. `PROVIDER-HTTP-ERRORS.md` §6 gives retry policy
  to the caller, and the caller then cannot see the server's answer.
- **Structured output is refused on all three official wires**, and two of the
  refusals cite a premise that has since changed: Anthropic now has
  `output_config.format`, and OpenAI's `json_schema` is native there. The refusals
  are honest, because the call raises rather than running unconstrained. They are
  stale, not dangerous.
- **No PDF anywhere.** τ's content vocabulary is text and image only.
- **Token counting.** Anthropic and Google both offer server-side counts, and
  OpenAI now does too (Responses only). R2's plan names the first two.

### 4.5 Mistral, for §3

Sources: https://docs.mistral.ai/capabilities/reasoning/,
https://docs.mistral.ai/capabilities/function_calling/,
https://github.com/mistralai/client-python/blob/main/docs/models/chatcompletionchoicefinishreason.md.

- `max_tokens` only, and no `stream_options`. τ's compat default already sends
  `max_tokens` off api.openai.com (`compat.py:129-141`), but it also sends
  `stream_options` unless told not to.
- `reasoning_effort` ∈ `none…xhigh`. `magistral-*` is deprecated.
- With reasoning on, `content` is a list carrying a `thinking` chunk, and the docs
  say to replay it.
- Tool-call ids are `^[a-zA-Z0-9]{9}$`. That rule is confirmed only via the
  Vercel AI SDK issue (https://github.com/vercel/ai/issues/11802) and vLLM's
  parser, not by Mistral's own pages.
- `finish_reason` ∈ `stop`, `length`, `model_length`, `error`, `tool_calls`.

## 5. Test plan

### 5.1 Three tiers, and what each one may cost

| Tier | Runs | Network | Proves |
|---|---|---|---|
| **Contract** (cassette replay) | every `pytest` | none | τ's consumer handles what the vendor sends, through the vendor SDK's own parser |
| **Request shape** (golden bodies) | every `pytest` | none | τ sends what the vendor documents: field names, placement, absence of a field |
| **Live conformance** (opt-in marker per vendor) | `pytest -m live_openai`, etc. | real endpoint, a key | the endpoint still behaves the way the cassette recorded |

The live tier follows `test_llama_conformance.py`'s rule exactly: **its assertions
encode measurements, not wishes**. A failure means the endpoint changed, and the
change may be the vendor fixing something. A transport failure skips; a wrong
answer fails. It is the only tier that spends money, so it is deselected in
`addopts` the way `llama` is (`pyproject.toml:22`), and it needs an explicit key
variable with no fallback.

### 5.2 Cassettes: format and recording

Prototype (built, ran): `tau-llm/tests/cassettes/<api>/<feature>.json`.

- **`meta`**: `provenance` (`synthetic` | `recorded`), `written` or captured date,
  `source` URL, and the model id when recorded.
- **`expect`**: what the response showed, such as the text, `stop_reason`, prompt
  tokens and cached tokens. The test reads expectations from the cassette, so a
  recorded replacement states what it saw.
- **`response`**: status, headers, and SSE events as `{event?, data}`.

Replay injects an `httpx.MockTransport` into each provider's real client:

- `OpenAICompletionsProvider._client` for OpenAI;
- `AsyncAnthropic(http_client=…)` for Anthropic. That one must be an `httpx2`
  transport: ran, anthropic 1.0.0 rejects an `httpx.AsyncClient`;
- `genai.Client(http_options={"httpx_async_client": …})` for Google.

Recording, not built:

- `scripts/record_cassette.py --api … --feature …` sends one request per
  `(api, feature)` through the same providers with a recording transport.
- It strips `authorization`, `x-api-key`, `x-goog-api-key`, and any header in a
  deny list.
- It writes `provenance: recorded`.
- It is run by John, with his keys, once per vendor-visible change. It is never
  run in CI.
- The twelve synthetic cassettes are then replaced. Until they are, they prove
  only that τ agrees with the vendor's *documentation*.

Request-shape goldens use the same mock transport. `_Replay.requests` already
keeps the request, so a test asserts on the JSON τ sent.

### 5.3 The contract suite

`ProviderContract` (prototype, `tau-llm/tests/test_provider_contract.py`) is a
base class. Each wire subclasses it by naming its `api` and building its provider
on a transport, the same shape as `tau_agent_core.testing`'s
`session_log_contract.py`. A known gap is marked `xfail(strict=True)` on the one
subclass that has it, so the fix turns the suite red until the marker comes off.

Ran 2026-10-12: **16 passed, 4 xfailed, 1 skipped.**

| Test (built) | Asserts | OpenAI | Anthropic | Google |
|---|---|---|---|---|
| `test_text_deltas_concatenate_to_the_final_text` | Σ deltas == final text == `expect.text`; `stop` | pass | pass | pass |
| `test_usage_partitions_the_prompt` | input + cache_read + cache_write == prompt; `cache_reported` | pass | pass | pass |
| `test_reasoning_tokens_are_reported_where_the_server_reports_them` | `usage.reasoning_tokens` == server's count | **xfail** | skip (no separate count) | **xfail** |
| `test_tool_call_arguments_decode_and_stop_is_tool_use` | one call, decoded args, `toolUse` | pass | pass | pass |
| `test_output_cap_maps_to_length` | `length` | pass | pass | pass |
| `test_an_unmapped_stop_reason_is_an_error_not_a_stop` | `content_filter` / `pause_turn` / unknown → `error` | **xfail** | pass | pass |
| `test_a_429_raises_provider_http_error_with_its_status` | `ProviderHttpError(429)` after exactly **one** request | pass | **xfail** (3 requests) | pass |

### 5.4 Proposed tests, in priority order

Each entry gives its tier and the clause it asserts.

**P0: defects already found.** Each one closes an xfail above.

1. `test_an_unmapped_stop_reason_is_an_error_not_a_stop` is built. The fix is
   `openai.py` `_map_finish_reason`: unknown values map to `error`, plus an
   explicit `content_filter` (and `model_length` → `length`).
2. `test_a_429_raises_provider_http_error_with_its_status` is built. The fix is
   `max_retries=0` in `anthropic.py:212`, or a stated decision to retry, written
   into `PROVIDER-HTTP-ERRORS.md` §6.
3. `test_reasoning_tokens_are_reported_where_the_server_reports_them` is built.
   The fix is a `Usage.reasoning_tokens` field.

**P1: the request side.** Request-shape goldens, no network.

4. `test_openai_sends_stream_options_include_usage`: the body has
   `stream_options.include_usage` when compat allows it.
5. `test_anthropic_cache_control_is_top_level_and_once`: one top-level
   `cache_control`, and none on tools.
6. `test_anthropic_thinking_sends_adaptive_and_effort`, plus
   `test_anthropic_thinking_display_is_explicit`. See §4.2 on `display`.
7. `test_google_thinking_level_reaches_the_wire`: `low` and `high` produce
   different bodies. This is xfail today (§1).
8. `test_tool_result_round_trip_keeps_the_id`: per wire, a tool result names the
   call id the assistant turn carried. Mistral's 9-char rewrite gets its own case.
9. `test_signature_round_trip`: an Anthropic thinking signature, a Gemini
   `thoughtSignature` on the first `functionCall` only, and an OpenAI-compatible
   `reasoning_content` field name, each replayed byte-identical.
10. `test_image_input_shape`: an `image_url` data URI, an Anthropic base64
    `source`, and Gemini `inline_data`.

**P2: response shapes still missing a cassette.**

11. `thinking_stream` per wire: thinking deltas, signature capture, and
    `redacted_thinking`.
12. `parallel_tool_calls`: two calls in one turn, on separate indices, both decoded.
13. `in_band_error_after_200`: an SSE error frame. It must be an `ErrorEvent` with
    `exception=None` (`PROVIDER-HTTP-ERRORS.md` §6).
14. `overloaded_529` (Anthropic) and `resource_exhausted_429` (Gemini, with
    `RetryInfo`): the status is kept, and the body is kept within the truncation
    bound.
15. `refusal` (Anthropic) with `stop_details`: `error_message` names the category
    (`anthropic.py:791-797`).
16. `mistral_thinking_chunks` and `mistral_model_length`: these turn §3.3 into
    executable checks.

**P3: live tier.** Opt-in, John's keys, priced per run.

17. `live_<vendor>_text_and_usage`: one short completion. Assert that the
    cassette's `expect` keys hold, which keeps the cassettes honest.
18. `live_<vendor>_cache_read_on_second_call`: two identical calls over the
    minimum cacheable prefix, and the second reads more than 0. This is the
    measurement `PROMPT-CACHING.md` §2 made by hand.
19. `live_<vendor>_count_tokens_equals_billed_input`: the R2 equality, server
    count against `usage`.

Cost is assumed: a few cents per vendor per run at small prompts, plus the
caching test's prefix, which needs at least ~1–4k tokens per vendor (§4).

## 6. Recommendations, each with its cost

Costs are estimates (inferred) in lines of production code plus tests.

| # | Change | Cost | Why first |
|---|---|---|---|
| 1 | OpenAI stop-reason mapping matches the other two: unknown → `error`, `content_filter` → `error`, `model_length` → `length` | ~15 lines; the xfail comes off | A truncated or filtered answer is shown as complete today |
| 2 | **Done (`PROVIDER-RETRY.md`).** Decide Anthropic retries. Either `max_retries=0` (PROVIDER-HTTP-ERRORS §6 stays true), or keep the SDK's 2 and say so in both docs | 1 line + 1 doc sentence | Today the docs disagree, and a caller budgeting retries pays them up to 3× |
| 3 | Anthropic `thinking.display: "summarized"` when reasoning is on (or a `Model` knob) | ~5 lines + 1 golden | 5.x models return empty thinking text today (§4.2) |
| 4 | Land the contract suite and record real cassettes (`scripts/record_cassette.py`) | ~200 lines + one recording session with keys | Every later provider change is checked against vendor bytes, not docs |
| 5 | `Usage.reasoning_tokens`, read on all three wires | ~30 lines; touches persisted usage (additive) | Cost accounting and the R2 work both want the split |
| 6 | OpenAI Responses wire | ~800–1200 lines (pi's is 1168 across two files) + cassettes | **The only way to run tools on OpenAI's current flagships** (§4.1); also encrypted reasoning replay and xAI |
| 7 | Gemini thinking level: map τ levels to `thinking_level` (Gemini 3) or `thinking_budget` (2.5), never both | ~40 lines | `low` and `high` are the same request today; "off" is unrepresentable on 3.1 Pro |
| 8 | Keep response headers (at least `retry-after`, `request-id`) on `ProviderHttpError` | ~20 lines across three wires | A caller owning retry policy needs the server's wait (§4.4) |
| 9 | Re-check Anthropic `_replay_scope`'s "prior-turn thinking is discarded" premise against 5.x, and measure the cache cost | a measurement first, then ~10 lines | The premise is false per current docs (§4.2); the cost is inferred |
| 10 | Per-model `headers` + auth-header name on `openai-completions` | ~40 lines | Moves ~6 pi providers from c/d to b (§2.3) |
| 11 | Mistral via `Compat`: id format, list-content thinking, `model_length` | ~120 lines + 3 cassettes | Smaller than a provider, and inherits `ProviderHttpError` (§3) |
| 12 | `count_tokens` on Anthropic and Google (ROADMAP R2) | ~80 lines | Free, exact, server-side counts (§4) |
| 13 | Vertex mode on the Google provider (`vertexai=True`, project, location, ADC) | ~60 lines; ADC cannot be tested without GCP | Enterprise Gemini users |

## 7. Open questions for John

1. **Where is the Mistral branch?** Name it, or push it somewhere this sandbox can
   read, and §3 becomes a diff review instead of a checklist.
2. **Retries.** Should τ retry at all? If not, `max_retries=0` on Anthropic. If
   yes, should all three wires share one policy?
3. **First-party OpenAI.** Is it a goal? If it is, the Responses wire (item 6) is
   not optional: per §4.1, tools on OpenAI's current reasoning flagships need it.
   If it is not, the `openai` vendor spec should say "OpenAI-compatible", so nobody
   reads it as working against `gpt-6-*`.
4. **Cassettes are public.** Recorded responses go to GitHub with the commit.
   Recording needs a prompt you are content to publish, plus header scrubbing; the
   model ids and the response ids would be visible. Acceptable?
5. **Live-tier budget.** Is there a per-run spend ceiling you want encoded, for
   example refusing to run without `TAU_LIVE_BUDGET_OK=1`?
6. **Third-party Anthropic-shaped servers.** Should a model be able to say "budget
   thinking" instead of "adaptive" on `anthropic-messages` (MiniMax, Fireworks)?
   Or are those endpoints out of scope?

## 8. What is absent

- **No change to any provider.** Every defect in §1 is unfixed. The xfails mark them.
- **No recorded cassette.** All twelve are synthetic. They prove agreement with
  the vendor's documented shapes, and nothing about the bytes a vendor actually sends.
- **No paid call was made.** The live tier is designed, not written.
- **No Mistral branch review** (§3.1).
- **No recorder script.** §5.2 specifies it.
- **No Bedrock, OAuth or Responses design** beyond the cost line. Each is its own
  record if chosen.
