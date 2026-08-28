# Anthropic and Google clients — settled decisions and open questions

Status: **sequencing steps 1–4 built (2026-08-22) — the Anthropic client is in.
Steps 5–6 (Google) remain blocked on O1/O2.** Opened 2026-08-21.

`PLAN-0.9.3.md` §4.4 step 5 is "pluggable auth, a model resolver, distinct
clients — last, and only on real demand for a specific non-OpenAI-shaped
vendor." That demand arrived: Anthropic and Google are the last two major wire
protocols τ cannot speak.

Steps 1–4 already landed, so the seam is free. `Provider` is an ABC with one
abstract method, `stream_chat`, plus `aclose` (`providers/base.py:80-149`); the
API and vendor registries exist; `client.py:199` dispatches through
`get_api_factory`. Adding a wire protocol is "write a class and register it."

The cost is not the seam. It is four collisions between these two APIs and
machinery τ built for the OpenAI path. This document records which of them are
settled and which are still open.

pi citations are at the local checkout, `~/Development/pi`. τ claims were
verified directly against the files named.

## 1. The governing principle

**Keep the pipeline OpenAI-shaped. Overload minimally for Anthropic and Google
peculiarities.** τ's internal message model, its accumulator, its finalize path
and its event vocabulary stay as they are. A second wire protocol adapts itself
to them, not the reverse.

This is a deliberate cost. It means the Anthropic and Google clients carry more
translation than pi's do, because pi's internal model was designed with all
three in view from the start. τ's was not, and rebuilding it now would touch
every provider, the session store, and the persisted JSONL.

## 2. Settled

### S1 — `reasoning_replay` on Anthropic: warn, then do the right thing

τ's `Model.reasoning_replay` (`types.py:226`) takes `"all"`, `"turn"`, `"off"`,
default `"turn"`.

`"turn"` replays a message's chain-of-thought only when the message sits after
the last user message (`openai.py:718`). In a tool-use loop the whole
assistant/tool sequence sits after the last user message, so `"turn"` replays
exactly the current turn's thinking and drops prior turns.

**That is already what Anthropic requires.** Thinking blocks from the current
tool-use sequence must travel back with their signatures; blocks from prior
turns are discarded by the API. τ's default needs no change.

The other two settings do:

* `"off"` would strip signatures Anthropic requires *inside* the current tool
  loop.
* `"all"` sends prior-turn thinking that Anthropic discards. Wasteful, not
  wrong.

**Decision: warn and use the correct behaviour anyway.** Choosing Anthropic is
an express act; setting `reasoning_replay="off"` alongside it is a mistake, not
an instruction to honour. The provider logs what it overrode and why, once per
model per process, and proceeds.

This is the one place in τ where a stated config value is deliberately not
obeyed. It is recorded here so it is not later mistaken for a bug.

### S2 — an unsigned thinking block becomes text, with a warning

A thinking block can reach the Anthropic converter with no signature: an aborted
stream, a block persisted before signatures were captured, a message synthesised
by an extension, or a session that changed models mid-conversation.

pi converts such a block to a plain `text` block
(`api/anthropic-messages.ts:1240`), or preserves it with an empty signature for
models flagged `allowEmptySignature`. τ has the same shape of fallback on the
OpenAI path already (`openai.py:849`).

**Decision: convert to text and warn. Do not raise.**

Raising is the wrong reflex here. The cause is almost always mixing models or an
extension-authored message — both legitimate — and the data structure stays
compatible with returning to the model that produced it. Crashing the program
over a provider quirk costs the user the session; converting costs a signature
the provider was going to reject anyway.

### S3 — `strict_reasoning_formats`, default `False`

A new `Model` field. `False` (default) means a reasoning-format quirk warns and
degrades as described in S1 and S2. `True` turns the same conditions into a
raise.

This exists so an operator who *is* running a single-model, signature-clean
pipeline can find out when that stops being true, without imposing a crash on
everyone mixing models.

It governs S1's override, S2's text conversion, and — see O1 — the duplicate
tool-name case on Google. One flag, one meaning: "a provider quirk I was willing
to work around is now an error."

### S4 — `thinking_signature` becomes `str | dict`, and the OpenAI writer refuses a dict

The word "signature" means four different things across the three APIs:

| Where | What it holds |
|---|---|
| τ `ThinkingContent.thinking_signature` (`types.py:46`) | the **field name** the reasoning arrived on (`reasoning_content`, `reasoning`, `reasoning_text`) |
| Anthropic `thinking.signature` (`anthropic-messages.ts:624`) | an opaque cryptographic signature over the thinking text |
| Anthropic `redacted_thinking.data` (`anthropic-messages.ts:633`) | an opaque encrypted payload, on a **different block type** |
| Google `part.thoughtSignature` (`google-generative-ai.ts:200`) | an opaque blob on a **functionCall part**, not a thinking block |

pi overloads one field for the first three. τ cannot copy that unchanged,
because τ uses the value **as a dictionary key**:

```python
result[thinking_signature] = thinking   # openai.py:870
```

An Anthropic signature through that line writes a cryptographic blob as a JSON
field name. It would not raise. It would send a valid-looking request that means
nothing.

**Decision: widen the field to `str | dict[str, Any]`, do not split it.**

* `str` keeps today's meaning exactly — the field name to replay under. Every
  existing config and every persisted session is unaffected.
* `dict` carries a provider-peculiar payload, e.g.
  `{"anthropic": {"signature": "...", "redacted": false}}`.

The OpenAI writer at `openai.py:869` guards on `isinstance(..., str)`. A dict
reaching it means the block came from another provider — model mixing — so it
takes S2's path: warn, keep the thinking as text content, do not use it as a
key. Under `strict_reasoning_formats` it raises instead.

This also gives `redacted_thinking` a home without a new block type.

**Sequencing: this lands before either client.** It is a `tau-llm` types change
that touches persisted session JSONL, and doing it once beats doing it after two
clients depend on the old shape.

### S5 — Anthropic first, Google second

pi's Anthropic client is 1391 lines in one file. Its Google path is ~978 across
two (`google-generative-ai.ts` parses, `google-shared.ts` converts), before
Vertex.

Anthropic's block model maps onto τ's, it is one file, and it needs no per-model
capability predicates. Google needs both, and its open question (O1) is
undecided. Anthropic proves the seam; Google follows.

### S6 — constrained decoding needs no work, and must not get any

`Model.grammar_dialect` defaults to `None`, which already means "raise on a
constraint-carrying call" (`openai.py:369`). A new provider that simply never
sets it inherits the correct refusal for free.

Do not add a grammar path to either client. Neither API exposes one, and the
failure mode τ built this gate against — a silently-ignored constraint returned
as a constrained generation — is exactly what a best-effort implementation would
reintroduce.

### S7 — `detect_compat` must not run off the OpenAI path — and already does not

`tau_llm.compat` reasons about `max_tokens` versus `max_completion_tokens` and
`stream_options` support. Both are OpenAI-path questions. It must not run for
`anthropic-messages` or `google-generative-ai`.

**Correction (2026-08-22): this needs no gate, because the gate is structural.**
`detect_compat` is reachable only through `resolve_compat`, and `resolve_compat`
has exactly one non-test caller: `openai.py:1262`, inside
`OpenAICompletionsProvider` itself. A new client that never calls it inherits the
correct behaviour for free — the same shape as S6, where a provider that never
sets `grammar_dialect` inherits the correct constraint refusal for free.

Gating on `model.api` would have added a branch that can only ever take one
value, and would have implied the call sits somewhere shared. It does not.

What this step actually cost was a test: `tau-llm/tests/test_compat_is_openai_only.py`
walks the `tau_llm` AST and asserts that nothing outside `compat.py` and
`providers/openai.py` calls either function. A structural invariant needs a
structural test — it fails the day someone hoists the compat call into
`client.py`'s dispatch or `providers/base.py`, where every wire protocol would
start paying for an OpenAI-only question.

### S8 — `ToolCall` must carry a provider signature, and the OpenAI writer must refuse it

Forced by O4's resolution, and discovered only because O4 was researched rather
than built around. `ToolCall` today is:

```python
type: Literal["toolCall"] = "toolCall"
id: str
name: str
arguments: dict[str, Any]
```

There is nowhere to put `thought_signature`. Since Gemini 3 rejects a
`functionCall` replayed without it, the Google client cannot be written until
there is.

Decided, reusing S4's convention rather than inventing a second one:

```python
provider_signature: dict[str, Any] = {}   # {"google": {"thought_signature": "..."}}
```

A namespaced dict, not a bare string. S4 widened `thinking_signature` to
`str | dict` only because that field had a pre-existing string meaning to keep;
`ToolCall` has none, so it starts at the shape S4 had to grow into. The namespace
is what stops a signature minted by one vendor from being replayed to another —
the same hazard `resolveThoughtSignature` guards in pi.

Two obligations come with it:

* **The OpenAI writer must refuse it**, exactly as S4 made it refuse a dict
  `thinking_signature`: drop the field, warn once per shape, raise under
  `strict_reasoning_formats`. A signature is meaningless off the wire that minted
  it, and silently forwarding one is the failure S4 exists to prevent.
* **It is persisted.** `ToolCall` round-trips through `model_dump()` into session
  JSONL, so a resumed session must still hold the signature or its next turn
  400s. This is the same durability requirement O1 already records for
  synthesised tool call ids, and for the same reason.

## 3. Open

### O1 — Google pairs tool results by name, and τ executes tools in parallel

`google-shared.ts:249` builds a tool result as:

```ts
functionResponse: {
  name: msg.toolName,                                  // matched by NAME
  response: msg.isError ? {error: v} : {output: v},
  ...(requiresToolCallId(model.id) ? { id: msg.toolCallId } : {}),
}
```

Three facts shape the question.

**Fact 1: τ already preserves call order end to end.** The parallel path uses
`asyncio.gather` (`agent_loop.py:1111`), which returns results in *input* order
regardless of completion order, and `all_results` is built by iterating
`enumerate(results)` aligned against `prepared_calls[i]`. `_build_batch_result`
(`agent_loop.py:1177`) then preserves that order into `result_messages`. So the
common worry — "parallel calls do not finish in the order they started" — is
true of completion and irrelevant to τ's data path.

**Fact 2: modern Gemini carries ids.** pi's `requiresToolCallId`
(`google-shared.ts:105`) returns true for Gemini 3+, `claude-*` and `gpt-oss-*`
behind Google endpoints. The by-name-only case is the legacy one. Ids sent to
Google are sanitised to `[a-zA-Z0-9_-]` and truncated to 64 characters
(`google-shared.ts:133-136`).

**Fact 3: the ambiguity is narrow.** It exists only when one assistant turn
contains **two calls to the same tool name** *and* the model does not carry ids.
A turn calling `read` and `grep` together is unambiguous by name. A turn calling
`read` twice is not.

Options:

| | Approach | Size | Drawback |
|---|---|---|---|
| A | Send the id when the model accepts it, positional otherwise (pi's approach) | small — one conditional plus id sanitisation | legacy models are silently positional |
| B | Detect the narrow case: duplicate tool names in one batch on a model without ids → warn, or raise under `strict_reasoning_formats` | ~20 lines on top of A | refuses (or warns about) a case that usually works |
| C | Force sequential tool execution on Google | small | **does not solve the problem** — sequential governs *execution*, not how many calls the model emits in one turn. Two `read` calls in one assistant turn stay two calls. Rejected. |
| D | Positional matching, documented, no guard | smallest | a mis-pairing is undetectable and silent |

Recommendation, not yet decided: **A + B**. Send the id wherever the model takes
it, and guard exactly the intersection where it is genuinely ambiguous. That
matches the warn-by-default / strict-flag shape already settled in S2 and S3,
rather than degrading parallelism globally for a case that is rare.

**MEASURED 2026-08-22 — take A, and B loses most of its reason to exist.**

Two facts from the O2 run, same record
(`docs/probe-results/README-gemini-2026-08-22.md`):

1. Sending the id is accepted everywhere measured, including on a model pi says
   does not take one. So A's "positional otherwise" branch is not where the
   common case lands — τ sends the id, and the ambiguity B guards does not arise.
2. The ambiguity was measured directly and did not reproduce. Two calls to
   `get_temperature` in one turn, answered by name only with payloads that never
   name the city — so only position could disambiguate them — paired **correctly**
   on all three models.

B was costed at ~20 lines to guard a mis-pairing this probe could not produce,
in a case τ's own default now avoids by sending the id. Build A. Leave B
unbuilt, with fact 2 recorded as the reason, so it is re-proposed only if a
mis-pairing is actually observed.

The persisted-synthetic-id requirement below is unaffected and still applies.

Option C is recorded as rejected with its reason so it is not re-proposed.

Also open under O1: a synthesised tool call id is **persisted** into the session
JSONL. It must be generated once at parse time and never re-derived on reload,
or resuming a session breaks the tool-result linkage.

### O2 — per-model Google capabilities: vendored table or `Model` fields

Google's converter needs two per-model answers: `requiresToolCallId` and
`supportsMultimodalFunctionResponse`.

pi's are not a table. They are prefix matching and a regex on the model id
string (`google-shared.ts:105-126`) — `startsWith("claude-")`,
`startsWith("gpt-oss-")`, `/^gemini(?:-live)?-(\d+)/` with a `>= 3` test.

That has the same polarity problem τ already rejected once. `PLAN-0.9.3.md`
§4.5 records it: pi lists the servers wanting `max_tokens` and gives everyone
else `max_completion_tokens`, so an unrecognised endpoint gets a spelling it
rejects; τ inverted the detection so the unknown case lands on the safe branch.
A regex that tests `gemini-(\d+) >= 3` is wrong the day Gemini 4 ships.

`providers/__init__.py` also states τ's standing position: τ ships no vendor
list, "every URL and environment-variable name in one is a claim τ would have to
keep true as vendors move them."

A capability table is a different kind of claim than a URL, but it ages the same
way.

Proposed synthesis rather than a choice — **a vendored table as the default, a
`Model` field that overrides it, and the unknown case picking the safe branch.**
The table saves the operator from configuring the common models; the field means
a wrong table is never load-bearing; the polarity keeps a new model from
inheriting a wrong answer silently.

Still to settle: what "safe" means per capability. For
`supports_multimodal_function_response` the safe branch is clearly the
conservative one (send a separate image turn). For `requires_tool_call_id` it is
not obvious — sending an id to a model that does not expect one may itself be
rejected. That needs measuring before it is decided.

**MEASURED 2026-08-22 — O2 is settled.** Full record:
`docs/probe-results/README-gemini-2026-08-22.md`.

Sending a tool-call `id` on a `functionResponse` was **accepted** by every model
measured, with the no-id control at 200 in each case: `gemini-3-flash-preview`,
`gemini-3.6-flash`, and — the load-bearing one — `gemma-4-26b-a4b-it`, which pi's
`requiresToolCallId` answers **false** for. So the case the question named ("an id
sent to a model that does not expect one may itself be rejected") does not occur
on this endpoint.

Decided, in the shape §2 already uses:

* **`requires_tool_call_id` defaults to TRUE, and the unknown model sends the
  id.** The safe branch is the permissive one, which is what the measurement
  found and the opposite of what the question assumed. A `Model` field overrides
  it.
* **No vendored table for this capability.** O2 proposed one as the default layer;
  if sending is always accepted, a table can only ever be wrong, never useful. τ
  keeps its standing position of shipping no vendor list, and gains a capability
  it does not have to keep true as Google moves.
* **`supports_multimodal_function_response` defaults to FALSE** — the separate
  image turn, which O2 already called clearly safe. Only `gemma-4-26b-a4b-it` was
  measured accepting a nested image, and one permissive data point does not earn
  a permissive default when the conservative branch always works.

Two limits on the sample, both recorded rather than smoothed over. No Gemini below
major version 3 is callable on a new key (2.5 Flash and 2.5 Flash Lite both 404
with "no longer available to new users"), so the legacy case pi's rule protects
could not be reached directly — Gemma stands in for it, which is a weaker
argument than an old Gemini would have been. And every verdict is scoped to
`generativelanguage.googleapis.com/v1beta`; a locally-served Gemma reaches τ over
the OpenAI wire and shares none of it.

**The instrument: `scripts/gemini_capability_probe.py`.** It asks Google
both questions directly, on one model per major Gemini version so the sample
straddles pi's `>= 3` boundary rather than assuming either side of it. It also
measures the failure O1's option B would guard, so that guard is costed before it
is written rather than after.

Every check that can report "rejected" carries a control — the same request with
only the field under test removed — and raises instead of returning a verdict if
the control does not come back 200. Without that, "Google refused this field" and
"the probe built the body wrong" are the same observation, and a wrong answer to
O2 would be indistinguishable from a right one.

The probe is written and lint/type clean; it has NOT been run. It needs a key
(`GEMINI_API_KEY`), and the free tier is enough. Nothing under O2 is decided
until a run is recorded in `docs/probe-results/`, at which point the pytest
wrapper follows the `llama` marker's shape: assertions that encode the
measurement, not the wish.

### O3 — constraining tool *arguments* is a real gap, and is not this work

Raised while checking S6. `_apply_constraints` refuses a decode constraint
alongside a declared tools array (`openai.py:377`) unless
`tool_choice="none"`, for a live-verified reason: llama-server 400s on
`grammar` + tools, and — worse — accepts `json_schema` + tools with a 200 while
**silently disabling tool calling**, so the model invents a schema-shaped answer
instead of calling the tool.

So "constrain the turn" and "let the model call tools" are mutually exclusive
today, deliberately.

That is not the same thing as **constraining a tool call's arguments to the
tool's own schema**, which τ has no path for at all. The backend probe already
found the motivating case: llama3.2:1b emitted a `properties` key alongside
`city`, outside the declared schema, and τ transcribed it faithfully
(`ce84321`).

This is worth its own design pass. It is orthogonal to the Anthropic and Google
work and must not be folded into it.

### O4 — what `reasoning_replay` means on Google

Google's `thoughtSignature` rides on a `functionCall` part, not on a thinking
block. τ's knob is phrased entirely in terms of replaying thinking blocks, so
its vocabulary does not extend cleanly.

~~Undecided. It does not block Anthropic.~~

**RESOLVED 2026-08-22 by research, and the answer is that `reasoning_replay` must
not govern it at all.** The premise above — that this is a replay *preference* —
is wrong. On Gemini 3 it is a protocol requirement:

> The first `functionCall` part in **each step** of the current turn **must**
> include its `thought_signature`.

Omitting it fails the request with 400: *"Function call `FC1` in the `1.` content
block is missing a `thought_signature`."* It was optional on Gemini 2.5 and
became mandatory on Gemini 3. Seven independent framework bug reports describe
exactly this breakage (LangChain4j #4097, goose #5792, adk-js #149, LiteLLM via
ai-helm #75, opencode #4832, cc-switch #2813, openai-agents-js #718).

So the decision:

* **A `functionCall`'s `thought_signature` is a field of the TOOL CALL, not
  reasoning content, and is always replayed** — regardless of `reasoning_replay`,
  including under `"off"`. It is reasoning-*derived*, which is what made it look
  like this knob's business; it is not reasoning the model reads back as
  thinking, it is a token the API validates. Letting `reasoning_replay="turn"`
  (τ's default) drop it would 400 every multi-turn tool conversation on Gemini 3.
* **Signatures on text and thinking parts DO follow `reasoning_replay`.** Google
  calls returning those "recommended", with no validation error, which is exactly
  the discretionary case the knob was designed for.

That split is the honest reading of O4's own observation. τ's knob means "how
much prior chain-of-thought do I resend", and one of these two things is not
chain-of-thought.

Three consequences for the client, all from the same source:

1. **Parallel calls take ONE signature.** Only the first `functionCall` part in a
   step carries it; the rest omit it. τ merges parallel calls into one assistant
   turn, so the converter must attach the signature to the first block only —
   copying it onto each would be inventing signatures.
2. **Never reconstruct a signature-bearing turn; replay its parts as they came.**
   pi states it directly (`google-shared.ts:60`): signatures must be preserved
   as-is, never merged or moved across parts. This is why
   `scripts/gemini_capability_probe.py` replays the assistant turn verbatim
   rather than rebuilding it — a client that rebuilds drops the field and hits
   the 400 above.
3. **Streaming may deliver the signature on the first delta only.** pi keeps
   `retainThoughtSignature` for this: a later delta's absent signature must not
   overwrite one already seen for that part.

**Documentation trap, recorded because it points the wrong way.** Google's newer
Interactions API guide says signatures "never appear on user inputs, model
outputs, or standard function calls" — the opposite of the rule above. τ speaks
`generateContent`, so the legacy `generate-content/thought-signatures` page
governs. Do not "fix" this code from the Interactions page.

Still open, and smaller: pi additionally validates that a signature is base64
(`TYPE_BYTES`) and drops one that came from a different provider or model
(`resolveThoughtSignature`). τ has the same cross-model replay hazard the moment
a session switches models mid-conversation. That is the same shape as S2's
unsigned-thinking problem and should reuse `strict_reasoning_formats`.

## 4. Sequencing

1. ✅ **S4** — widen `thinking_signature` to `str | dict`, guard the OpenAI writer.
   Types change, lands alone, before either client.
2. ✅ **S3** — add `strict_reasoning_formats`. Small, and S1/S2/O1 all reference it.
3. ✅ **S7** — ~~gate `detect_compat` on `model.api`~~ — no gate needed; a test
   pins the structural invariant instead. See the correction in S7.
4. ✅ **Anthropic client** — S1, S2, S5, S6 apply. O5 was decided first (the
   official SDK, as an optional extra) and then it landed.
5. ✅ **Decide O2, then O1.** Both decided from measurement on 2026-08-22, not
   from discussion. O2: send the id, no vendored table, conservative on nested
   images. O1: option A; option B left unbuilt because the mis-pairing it guards
   did not reproduce. Record: `docs/probe-results/README-gemini-2026-08-22.md`.
6. ✅ **S8** — widen `ToolCall` with `provider_signature`, guard the OpenAI writer.
   Types change, landed alone, before the Google client. Same position in the
   order S4 held before the Anthropic one, and for the same reason.
7. ✅ **Google client** — `tau_llm/providers/google.py`, registered as the
   `google-generative-ai` api and the `gemini` vendor.

### What steps 6–7 landed

O6, opened and decided while building step 7, mirrors O5: the client is built on
the official `google-genai` SDK as the optional extra `tau-llm[google]`,
imported lazily. O5 argued drift as a prediction; here it was already measured —
`thought_signature` went from optional to validated between Gemini 2.5 and 3, and
Google's two documentation pages presently disagree about whether the field
exists on function calls at all.

The vendor is registered as **`gemini`**, not `google`. That is the `backend`
value `~/.tau/config.json` entries have carried since before this client existed,
including the one in the shipped template; registering `google` would have left
every one of them resolving to the OpenAI wire, which is exactly the defect step
4 fixed.

The two O2 capabilities are `Model` fields — `requires_tool_call_id` (default
`True`) and `supports_multimodal_function_response` (default `False`) — and
nothing in the module matches on a model id. That is the point: O2 proposed a
vendored table as the default layer, and the measurement removed the need for
one.

Not verified against the live API. The provider suite stubs the SDK's streaming
surface, and the whole suite passes with the SDK import blocked, which is what
keeps the extra genuinely optional. The probe exercised the *wire* against real
models, but the probe is not this client.

O3 is scheduled separately and is not part of this work.

### What steps 1–3 landed

* `ThinkingContent.thinking_signature` is `str | dict[str, Any]`
  (`tau-llm/src/tau_llm/types.py`). The `str` meaning is byte-for-byte
  unchanged, so no persisted session and no config moves.
* The OpenAI writer branches on the type. A `dict` never reaches
  `result[thinking_signature] = thinking`; it takes S2's path — the reasoning
  stays as text content, and `_on_foreign_thinking_signature` warns once per
  payload shape per process. `strict_reasoning_formats` turns that warning into
  a raise.
* `Model.strict_reasoning_formats` exists, defaults `False`, and is reachable
  from `~/.tau/config.json` as `models.<name>.strict_reasoning_formats`
  (`backends.build_model_from_config` validates the type — that seam is an
  explicit constructor, not a passthrough, so a field not added there is a field
  no config user has).
* Tests: `tau-llm/tests/test_foreign_thinking_signature.py` (13),
  `tau-llm/tests/test_compat_is_openai_only.py` (2).

S1's warn-and-override is **not** built. It belongs to the Anthropic client,
which is the only thing that can know Anthropic's replay requirement, and it
lands with step 4.

## 5. Opened while building steps 1–3

### O5 — what is the Anthropic client built on: the official SDK, or the wire?

**This blocks step 4, and §1's governing principle does not answer it.** §1
governs τ's *internal* model. This is a question about the *outbound* half.

pi uses the official vendor SDKs for both providers: `@anthropic-ai/sdk` at
`anthropic-messages.ts:1` and `openai` at `6.40.0` in `packages/ai/package.json`.

τ does neither. `tau-llm` depends on `pydantic` and `httpx` and nothing else,
and `OpenAICompletionsProvider` posts to `/chat/completions` and parses the SSE
frames itself. So "port pi's client" and "match τ's existing provider" point at
different substrates, and the doc's framing — "adding a wire protocol is write a
class and register it" — quietly assumes the second without arguing for it.

| | Approach | Cost | Drawback |
|---|---|---|---|
| A | Official `anthropic` Python SDK, as a `tau-llm[anthropic]` extra | smallest client; the SDK owns SSE parsing, block types, signature round-tripping, and tracks the wire as it drifts | a second HTTP stack inside a package that has exactly one; its own pooling and retries under `aclose_providers`; a vendor dependency in a package whose registry docstring says τ ships no vendor claims |
| B | Implement the Messages wire over `httpx`, like the OpenAI provider | no new dependency; one streaming/pooling/abort story; consistent with `Provider` as "one wire protocol per class" | τ owns the drift — thinking `adaptive` vs `budget_tokens`, `output_config.effort`, beta headers, `stop_reason: "refusal"` — and that surface moved repeatedly across 2025–2026 |

The drift under B is not hypothetical. Anthropic removed `budget_tokens` on the
current models (it now 400s), moved effort into `output_config`, and changed the
default of `thinking.display` to `"omitted"`. Each is a silent behaviour change
for a hand-rolled client that does not track it.

**Decided 2026-08-22: A — the official SDK, as the optional extra
`tau-llm[anthropic]`.** The drift argument won. The extra rather than a hard
dependency keeps an install that only ever speaks the OpenAI wire from paying
for a second HTTP stack; `providers/__init__.py` imports the provider module
lazily, and the provider imports the SDK later still, on first request, so
`import tau_llm` works without the extra and the missing-extra error names the
extra instead of surfacing as a bare `ModuleNotFoundError`.

### What step 4 landed

`tau-llm/src/tau_llm/providers/anthropic.py`, registered as the
`anthropic-messages` wire protocol, plus an `anthropic` vendor spec
(`https://api.anthropic.com`, `ANTHROPIC_API_KEY`). Bedrock, Vertex and Foundry
are deliberately absent — each speaks an Anthropic-shaped protocol behind
different auth, and each is a `register_provider` call in the embedding
application, exactly as `providers/__init__.py` describes.

Registering the vendor is narrower than the vendor list that docstring refuses.
The refusal is about vendors τ does **not** implement, whose URLs τ would then
have to keep true. `anthropic-messages` is a wire protocol named after its
author, so the api registration is already the claim; declining to also state
the endpoint would only mean every user retyping the same URL.

Decisions inside the client, beyond what §2 settled:

* **Tool results merge into one user message.** Anthropic has no `tool` role.
  Splitting the results of a parallel tool call across several user messages
  trains the model to stop making parallel calls, so consecutive results join a
  preceding run of results — and never a real user turn.
* **A prior turn's thinking is dropped, not downgraded to text.** S2's
  convert-to-text applies to a block τ *would* replay but cannot sign. A block
  the replay scope excludes is discarded by the API anyway, and sending it as
  text would inject the model's private reasoning into the visible conversation.
* **`refusal` maps to τ's `error` stop_reason**, with the category on
  `error_message`. A refusal is HTTP 200 with little or no content; reporting it
  as `stop` would hand the caller an empty successful answer and
  `ctx.complete()` would not raise.
* **An unmapped `stop_reason` is also an error.** `pause_turn` means the turn is
  resumable, not finished. Guessing "stop" would return a truncated answer that
  looks complete.
* **Thinking levels map to `output_config.effort` with adaptive thinking.**
  `budget_tokens` is removed on the current models and returns a 400, so τ never
  sends one. `"off"` sends `thinking: {"type": "disabled"}` rather than being
  silently upgraded to adaptive — some models reject that pairing at the highest
  effort settings, and a 400 naming it beats τ running a mode nobody asked for.
  **This mapping is stated, not settled** — τ's level enum is OpenAI-shaped and
  predates this API. It is the first thing to revisit if the client misbehaves.

**Not verified against the live API.** Every test stubs the SDK's streaming
surface; the contract this module owns is "τ messages in, τ streaming events
out", and the SDK owns the bytes. A first real call is still worth making.

### The config seam could not name a non-OpenAI wire — fixed with step 4

`backends.build_model_from_config` hardcoded `api="openai-completions"`, so the
api registry existed, `client._resolve_request` dispatched through it, and no
`~/.tau/config.json` user could reach any of it. It also defaulted every model's
`base_url` to OpenAI's, which would have pointed the Anthropic client at the
wrong server.

Both are now resolved in order — a stated value, then the registered vendor's
own, then the historical default — and the api resolution inherits §4.5's
polarity rule: an unrecognised value raises against `registered_apis()` rather
than falling through to the OpenAI wire, because a model silently served over
the wrong protocol is the failure `providers/__init__.py` already removed once
for `openai-responses`. `backend: "anthropic"` alone is now a working config.

Covered by `tau-coding-agent/tests/test_config_names_a_wire.py`, whose first
class asserts that every config shape that worked before builds the same Model.

### The first real call failed — and the tests could not have caught it

The section above closes with "Not verified against the live API… A first real
call is still worth making." It was made, against an `anthropic-messages`
gateway, and it did not reach the network:

```
TypeError: AsyncMessages.stream() got an unexpected keyword argument 'temperature'
```

Three separate things had to be true for a provider this heavily tested to fail
on every real request.

**1. The agent loop sent a temperature nobody chose.** `AgentLoopConfig`
defaulted `temperature` to `0.7` and `AgentLoop._build_options` put it in
`options` unconditionally. The value was unreachable from config: `Model` had no
`temperature` field, so `agent_session`'s `getattr(self._model, "temperature",
0.7)` always fell through to the literal, and a `temperature` key in
`~/.tau/config.json` was dropped by pydantic without a word. pi does not do this
— `simple-options.ts:32` forwards `options?.temperature`, which is undefined
unless a caller sets one, and pi's agent never sets one.

**2. This provider splatted options into a typed Python method.** The OpenAI
provider builds a JSON body and posts it, so a key it does not recognise is the
server's business and the server answers. This one calls
`client.messages.stream(**request)`. `anthropic` 1.0.0 removed `temperature`,
`top_p` and `top_k` from that signature — the Messages API returns 400 for them
on Opus 5, Opus 4.8, Opus 4.7, Sonnet 5 and Fable 5 — and declares no
`**kwargs`. `Model.extra_body` was splatted the same way, so the operator escape
hatch this module documents was equally broken on this wire.

**3. The stub accepted anything.** `_FakeClient`'s `def stream(self, **kwargs)`
takes every keyword the real SDK rejects. Seventy-odd tests drove it and none of
them could see the defect. This is the general shape: a stub that is more
permissive than the thing it stands for does not test the boundary, it hides it.

What changed:

* `Model.temperature: float | None = None`, read by `agent_session` and carried
  by `build_model_from_config`. `None` means τ sends none and the endpoint
  applies its own — pi parity, and the only default that is right for llama.cpp
  (0.8), the OpenAI wire (1.0) and a Messages model that removed the parameter.
  `AgentLoopConfig.temperature` and `Settings.temperature` default to `None` too.
* `_accepted_stream_params` asks the INSTALLED SDK what
  `client.messages.stream` declares. `Model.extra_body` is then split rather
  than splatted: a key the SDK declares rides as that keyword argument, which
  keeps per-call options able to override it, and a key it does not declare goes
  into `extra_body`, where it lands in the JSON body and the server answers.
  A per-call option the SDK does not declare raises, names itself, and names
  `models.<name>.extra_body` as the way to send it anyway — τ does not guess
  that an undeclared keyword argument belongs in the body.
* The stub's `stream` is generated from `SDK_STREAM_PARAMS`, so it rejects what
  the SDK rejects, and one test compares that tuple against the real signature
  (skipped when the optional extra is absent). That test is what keeps the rest
  of the file honest.
