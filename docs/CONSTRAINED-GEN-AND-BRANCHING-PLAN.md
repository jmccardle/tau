# τ × llama-server: Constrained Decoding & KV-Branch Readiness Plan

Status: **PARTIALLY DELIVERED** (last audited 2026-07-14).
G0 (`Model.grammar_dialect`/`extra_body`/`server_features`), G1 (`DecodeConstraints` +
provider wire mapping + gating), G2 (`tau_llm/grammar.py` + `ConstraintViolation` +
verification), G3 (constrained `ctx.complete()`, demoed by
`examples/60_retrieval_review.py`) and G5 (prefix-stability contract tests, §7.1.1)
are implemented and hardened.
**G4 (telemetry) is built:** capture (W8 — `Usage.extra` carries the server's
`timings` block; a JSON-repair counter rides `message_end`) and the consumer side
now both ship — the TUI exchange summary renders t/s / repairs / forced-share (shared
`format_telemetry`), the `--mode json` stream carries `usage.extra` (test-locked), and
§3.3's constraint echo (`DecodeConstraints.describe()`) is emitted from `ctx.complete()`
via `ExtensionUI.emit_constraints`. (Two §3.3 sub-items stay blocked-not-stubbed: the
main loop applies no constraints, and constrained completions don't persist an entry.)
**Not built:** G6 (integration bench) and G7 (LMTP), both blocked on the
server-fork track (they need the forced-token count `n_ff_total`, which stock
llama.cpp does not report).

> **Status index:** the delivery state of every G/W/JMFTS/fork item — and the debts
> left along the way — is tracked in one place: **`docs/WORKSTREAM-CROSSWALK.md`**.
> That file is authoritative for *status*; this doc stays authoritative for *design*.

Companion documents:

- `~/Development/turboquant_experiments/GRAMMAR_DECODING_RECON.md` — llguidance
  grammars on llama.cpp, verified working; jump-forward effort assessment.
- `~/Development/turboquant_experiments/JUMP_FORWARD_ROADMAP.md` — the server-side
  implementation plan (Phases A–D) for forced-token injection in llama-server.
- `~/Development/turboquant_experiments/KV_BRANCHING_RECON.md` — zero-copy KV
  forking in llama.cpp's unified cache; paths 0/1/2.
- `docs/JMFTS-INTEGRATION-PLAN.md` §9 — the C1 (`ctx.complete()`) and C2 (branch
  sub-agents / multi-cursor) core tracks this plan's consumers live on.

## 0. Objective

The server side is becoming a stronger generation platform: llguidance grammars
already work on llama-server (`"grammar": "%llguidance {}\n..."` on the standard
REST API), jump-forward decoding will make grammar-forced spans nearly free, and
the unified KV cache can zero-copy-fork a shared prefix into N branches. **None of
that is reachable from τ today**: τ cannot attach a grammar to a completion, cannot
see the server's timing/attribution metrics, and gives no guarantee that two
related requests share a byte-identical prompt prefix.

This plan defines what τ must **expose** (config, extension API, tool surface) and
**develop** (plumbing, gating, verification, telemetry) so that:

1. An extension or tool can constrain any completion with a grammar / JSON schema
   — the retrieval-review fan-out (JMFTS §9) runs verdict generations where
   nearly every token is grammar-forced.
2. The main agent loop's tool calls benefit from server-side constrained decoding
   and jump-forward with **measurable** effect (repair-free tool JSON, forced-token
   share, effective t/s).
3. C2 multi-cursor branch sub-agents present the server with byte-stable shared
   prefixes, so KV-branching (RadixAttention-subset) turns N-way fan-out prefill
   from O(N·context) into O(1) — and τ is shaped to adopt an explicit fork API
   (LMTP path 2) when it exists.

Non-goals / constraints:

- **Stock-server compatible.** Everything in Phases G0–G4 works against llama.cpp
  master today (grammars are already accepted; jump-forward only changes *speed*,
  not the request contract). Nothing in τ may hard-require the private fork.
- **Fail-Early.** A constraint requested against a model that doesn't declare
  grammar support raises; a constraint the server silently dropped is detected
  and raised (see §5.3 — the SPM silent-death failure mode is real and observed).
  No constraint is ever "best effort".
- **Tree-as-truth is untouched.** A decode constraint is part of the per-call
  *ephemeral frame* (sampling parameters), never a hidden content channel. Model
  input remains the system prompt + the linear tree path; constraints are
  inspectable (recorded on the result and observable as events), and they never
  inject or reorder messages.

## 0.1 W1 spike results — VERIFIED LIVE (2026-07-12)

Run against stock llama.cpp master (`repos/llama-master`, CUDA + `-DLLAMA_LLGUIDANCE=ON`),
Qwen3-8B-Q4_K_M (BPE), `--jinja`, on the OpenAI-compat endpoint τ actually uses
(`/v1/chat/completions` — the recon had only ever tested the native `/completion`).

| # | Request | Result |
|---|---|---|
| 1 | `grammar` (llguidance), no tools | **200, constrained** → `"include"` |
| 2 | `response_format: json_schema` | **200, constrained** → `{"verdict": "include"}` |
| 3 | `grammar` + tools, `tool_choice=auto` | **400** `Cannot use custom grammar constraints with tools.` |
| 4 | `grammar` + tools, **`tool_choice="none"`** | **200, grammar applies** — the exemption is real |
| 5 | `json_schema` + tools, `tool_choice=auto` | **200 — and tool calling is SILENTLY DEAD** (see below) |
| 6 | `grammar` + `json_schema` together | **500** `Cannot use both json_schema and grammar` |
| 7 | streaming final chunk | carries **both** `usage` and `timings`, same chunk |

**The finding that sets policy (#5).** With an identical prompt explicitly ordering a
tool call:

- tools alone → `tool_calls: read{"file_path": "/etc/hostname"}`
- tools + `response_format` → **no tool call**, and the model fabricated
  `{"verdict": "success"}` to satisfy the schema.

The server rejects `grammar` + tools (a 400 τ can surface) but **accepts
`json_schema` + tools with a 200 while silently disabling tool calling** — the
schema grammar wins and the model invents a schema-shaped answer. This is the
fabricated-data failure mode, returned as success.

⇒ **Decision (was §9's open question): τ raises on ANY constraint + tools when
`tool_choice != "none"`** — `grammar` *and* `json_schema` alike. The server only
protects us from one of the two.

Two further notes for implementers:

- `timings` keys on stock master: `prompt_ms`, `prompt_n`, `prompt_per_second`,
  `prompt_per_token_ms`, `predicted_ms`, `predicted_n`, `predicted_per_second`,
  `predicted_per_token_ms`, `cache_n`. **No `n_ff_total`** — that arrives with the
  jump-forward fork (roadmap Phase D), which is exactly why `Usage.extra` is
  untyped (§6, decision 6).
- The `grammar` + `json_schema` conflict returns **500**, not 400. τ raises
  client-side before the request anyway (§3.1), so this only matters for error
  attribution.

## 0.3 A constrained call cannot THINK — see `docs/REASONING-VS-CONSTRAINED-DECODING.md`

**(2026-07-12, found when the rewired retrieval-review demo returned nothing at all.)**

A decode constraint applies from the FIRST generated token, and a reasoning model's first
token belongs to its thinking. Qwen's template force-opens `<think>`, so llama-server
forces the constrained answer into the REASONING channel and returns
`{"content": "", "reasoning_content": "include"}`. The grammar held perfectly; the answer
just is not where anyone reads it. τ reads `content`, sees `""`, and raises
`ConstraintViolation` — which claims *the server dropped the grammar*, the exact opposite
of what happened. **Every constrained call against a thinking model produced an empty
verdict this way.**

τ now sends `chat_template_kwargs: {"enable_thinking": false}` on constrained requests.
That is a **workaround, and it costs the model its reasoning** — a trade τ should not have
to make. llama.cpp already has the mechanism to avoid it (lazy grammars + a trigger on the
reasoning delimiter, which is how it does tool calls with reasoning models); it is simply
not reachable from `/v1/chat/completions`, which overwrites `grammar_lazy` /
`grammar_triggers` unconditionally (`server-common.cpp:1099-1105`). The upstream patch and
its reproduction are written up in `docs/REASONING-VS-CONSTRAINED-DECODING.md`.

## 0.2 Hardening pass — two SILENT-OVERRIDE hazards (2026-07-12, Qwen3.6-35B)

The W1 spike was re-run against a second, much larger model (`qwen36-35B-IQ4_XS`, 8
slots, 256k ctx) with a proper control, and the constraint layer was attacked
adversarially. Everything in §0.1 reproduced. Two *new* hazards surfaced, both of which
had live, working bypasses through τ.

**Reproduce:** `venv/bin/python scripts/w1_grammar_spike.py [base_url]` — the script now
asserts and exits non-zero. (The version originally committed sent `max_tokens: 50` with
thinking enabled, so the whole budget was spent inside `<think>` and every `content` came
back empty; it never reproduced its own recorded findings.)

### Hazard 1 — `json_schema` silently disables tool calling (confirmed, with a control)

| request | result |
|---|---|
| `tools` alone | `read({"file_path": "/etc/hostname"})` — the model wants the tool |
| `tools` + `response_format` | **HTTP 200, no tool call**, fabricated `{"verdict": "exclude"}` |
| `grammar` + `tools` (`tool_choice != none`) | HTTP 400 — loud, guarded |

The server refuses `grammar` + tools explicitly but has **no equivalent check** for
`json_schema` + tools. τ is the only line of defence, hence the raise covering *both*
constraint kinds (`_apply_constraints`, gate 2).

### Hazard 2 — `response_format` SILENTLY OVERRIDES `grammar`

| request | result |
|---|---|
| `grammar` + top-level `json_schema` | HTTP 500 `"Cannot use both json_schema and grammar"` |
| `grammar` + `response_format` | **HTTP 200 — the grammar is discarded, JSON returned** |
| `grammar` alone | `'include'` |

llama.cpp's mutual-exclusion check is keyed on the top-level `json_schema` field;
`response_format` parses on a different path and wins without a word. A real
`DecodeConstraints(grammar=include|exclude)` came back as `{"verdict": "REJECT"}`.

**Both hazards are llama.cpp bugs** (a silent precedence choice where the server has
enough information to refuse). See `docs/UPSTREAM-LLAMACPP-SILENT-OVERRIDES.md`.

### What this forced in τ

1. **`grammar` / `json_schema` / `response_format` are now reserved body keys**
   (`_CONSTRAINT_BODY_KEYS`), rejected from `Model.extra_body`, per-call body options,
   and `DecodeConstraints.extra_body` alike. `DecodeConstraints` is the only door,
   because it is the only door with gates. Four live bypasses existed before this:
   a `grammar` in `extra_body` skipped both gates entirely (the capability gate returns
   early when `constraints is None`); a smuggled `response_format` silently beat a real
   grammar; `tools` passed as a *body option* left `has_tools=False` so the tools gate
   never fired; and `DecodeConstraints(json_schema=…, extra_body={"grammar": …})`
   satisfied the exactly-one-of validator while shipping both.
2. **Raw-grammar verification was a placeholder.** It asserted only that the output was
   non-empty — so with the grammar sabotaged, `choices` correctly raised, `json_schema`
   was self-protecting, and a **raw grammar returned `{"verdict": "no"}` and PASSED**.
   `tau_llm.grammar` helpers now return a `Grammar` (a `str` subclass carrying a checker
   for its own output), and `DecodeConstraints(grammar=…)` **raises at construction** if
   it can neither check the grammar nor was given a `verify=`. τ does not pretend.
   This also closes a sharp edge: `grammar.choice("a","b")` and `choices=["a","b"]`
   compile to the *identical* wire grammar and are now verified identically.
3. **Whitespace in a grammar is significant.** `fixed("yes ")` really does force the
   trailing space, so `_message_text` no longer strips before verifying — stripping made
   a *correctly* constrained output fail its own check and blamed the server for damage
   τ did.
4. **`stop_reason == "length"` now raises.** Verification used to be skipped on a
   truncated generation, and nothing downstream inspects `stop_reason`, so a partial
   `{"verdict": "include", "confidence": 0.` was handed back as a successful constrained
   result. A truncated constrained answer is a prefix, not an answer.

### Who should verify — and why it is not the server

The end-to-end argument settles it: **you cannot delegate the check to the component
whose silent failure is the threat.** The documented failure mode is llguidance dying
mid-generation, logging server-side, and letting the model run free
(`GRAMMAR_DECODING_RECON.md:36`) — a server in that state will cheerfully report success.

But τ must never reimplement the *constraint engine* either: client-side constraint
evaluation is precisely the LMQL mistake the LMTP evaluation calls "the aging part"
(`GRAMMAR_DECODING_RECON.md:113-116`). So no llguidance Python bindings. Three tiers:

| tier | who checks | how |
|---|---|---|
| stock llama.cpp | τ, but only soundly | `choices` → membership; `json_schema` → parse; helper-built grammar → its own checker; hand-written grammar → **caller must supply `verify=`** |
| our fork | the server, honestly | llg parser death → hard error instead of "allow everything"; the two silent overrides above → 400 |
| LMTP / jump-forward | the protocol | **forced-token attribution** (`token_source: "forced"\|"sampled"`, `n_ff_total`) makes verification *grammar-agnostic*: a constraint that forced **zero** tokens was not applied. O(1), total, reimplements nothing. |

That third row is the real answer, and it is the same number as G4's telemetry and the
jump-forward demo metric (forced tokens per forward pass). One signal, three uses — which
promotes G4 from a small parallel task to the foundation of trustworthy verification.

## 1. What exists today (verified, file:line)

The audit found the transport is nearly ready and the policy layer is entirely
missing:

- **Arbitrary body passthrough already works.** `OpenAICompletionsProvider.stream_chat`
  splats every option it doesn't recognize into the request JSON
  (`tau-llm/src/tau_llm/providers/openai.py:821-829` — `body_options`). A
  `{"grammar": "..."}` option would reach llama-server *today*. What's missing is
  any legitimate way to put it there.
- **Options are rebuilt from static config on every call.**
  `AgentLoop._stream_one_completion` constructs `options` from
  `AgentLoopConfig.temperature/api_key/reasoning` + the abort signal
  (`tau-agent-core/src/tau_agent_core/agent_loop.py:578-591`). There is no
  per-call or per-turn override channel; extensions cannot influence the next
  completion's decode parameters at all.
- **`Model` declares no decode capabilities** (`tau-llm/src/tau_llm/types.py:124`):
  nothing says "this endpoint accepts llguidance grammars" or "this endpoint has
  a fork API". `build_model_from_config`
  (`tau-coding-agent/src/tau_coding_agent/backends.py:88`) is the single
  config→`Model` seam where new fields enter.
- **Server metrics are dropped.** `_usage_from_openai` (openai.py:183) maps
  prompt/completion/cached token counts and discards everything else; `DoneEvent`
  carries only `final` + `usage` (`tau-llm/src/tau_llm/streaming.py:100`); `Usage`
  is a frozen 5-field model (types.py:69). llama-server's `timings` block — which
  the jump-forward roadmap Phase D extends with `n_ff_total` (forced-token count)
  — never reaches τ.
- **Tool schemas are already JSON Schema.** `ToolDefinition.parameters`
  (`tau-llm/src/tau_llm/tools.py`) is the raw material for constraint grammars, but
  note §5.1: for the main loop, the tool-call *wire format* is owned by the
  server's chat template, not by τ.
- **Tool-argument repair is un-instrumented.** `parse_json_with_repair`
  (`tau-llm/src/tau_llm/json_parse.py`) silently absorbs malformed streamed JSON.
  Under server-side constrained tool calls its invocation count should drop to
  zero — but nothing counts it, so nothing can prove it.
- **C1/C2 do not exist yet** (JMFTS plan §9). C1 (`ctx.complete()`) is the natural
  first consumer of per-call constraints; C2 (branch sub-agents) is the consumer
  of KV branching. This plan feeds requirements into both rather than duplicating
  them.

## 2. Capability model: `Model` learns what its endpoint can do

New optional fields on `tau_llm.types.Model`, populated from the config entry by
`build_model_from_config` (and settable on ad-hoc `--model` dicts):

```jsonc
// ~/.tau/config.json models entry
"local-llm": {
  "backend": "openai",
  "base_url": "http://127.0.0.1:8080/v1",
  "model": "gpt-oss-20b",
  "grammar": "llguidance",          // null (default) | "llguidance" | "gbnf"
  "extra_body": {"cache_prompt": true},   // static per-model request params
  "server_features": ["jump_forward", "slot_fork"]   // informational tags
}
```

- **`Model.grammar_dialect: Literal["llguidance","gbnf"] | None = None`** — the
  single gate for constrained requests. `None` means every constraint-carrying
  call **raises** (`Model 'x' declares no grammar support; set models.<name>.grammar`)
  rather than shipping a param OpenAI-the-company would 400 on and other servers
  would silently ignore (silent-ignore is the worse failure — an unconstrained
  generation masquerading as constrained; Fail-Early).
- **`Model.extra_body: dict[str, Any] = {}`** — static request-body params merged
  into the payload before per-call options (per-call wins). Standalone value even
  without grammars: llama-server knobs like `cache_prompt`, `min_p`, samplers —
  today these are unreachable without editing τ source. This is the config-side
  half of the passthrough that openai.py:821 already implements on the wire side.
- **`server_features: list[str]`** — advisory tags for telemetry/UX (e.g. the TUI
  can show "ff" in the footer when `jump_forward` is declared and `n_ff_total`
  arrives). Deliberately *not* used for gating — the gate for grammar is
  `grammar_dialect`, and fork-API gating arrives with the fork API (§7.3).

Dialect note: τ standardizes on **llguidance Lark-style grammars** (the
`%llguidance {}\n` prefix dispatched at llama.cpp `common/sampling.cpp:201`)
because that is what jump-forward accelerates. `"gbnf"` is declared-but-second-class:
accepted, passed through as-is, no τ-side helpers.

## 3. The decode-constraints channel

One new value type in `tau_llm` (next to `Model`):

```python
class DecodeConstraints(BaseModel):
    grammar: str | None = None            # raw grammar text, dialect per Model
    json_schema: dict | None = None       # compiled to response_format/grammar
    choices: list[str] | None = None      # sugar: compiled to a choice grammar
    tool_choice: str | dict | None = None # OpenAI-compat passthrough
    extra_body: dict[str, Any] = {}       # per-call body params (highest precedence)
```

Exactly **one** of `grammar` / `json_schema` / `choices` may be set (a server
takes one constraint per request; two is a caller bug — raise, don't pick).

### 3.1 Wire mapping (provider)

`stream_chat` gains explicit handling (a new recognized option key,
`options["constraints"]`, stripped from `body_options` like `api_key`/`reasoning`):

- `grammar` → payload `"grammar"`, prefixed with `%llguidance {}\n` when the
  model dialect is llguidance and the text doesn't already carry a `%llguidance`
  header (never double-prefix; never prefix a gbnf model's grammar).
- `choices=["include","exclude"]` → generated llguidance grammar
  `start: "include" | "exclude"` (τ-side compilation, §4.2).
- `json_schema` → llama-server accepts OpenAI-style
  `response_format: {"type":"json_schema", ...}`; pass that form so the server
  does its own schema→grammar conversion (llguidance consumes JSON Schema
  natively — τ does not reimplement that compiler).
- `tool_choice`, `extra_body` → merged into the payload, per-call over
  `Model.extra_body` over τ defaults.
- **Gating**: any of `grammar`/`json_schema`/`choices` present with
  `model.grammar_dialect is None` → raise before the request is built.
- **Conflict**: constraints + a non-empty `tools` list **and `tool_choice != "none"`**
  → raise (§5.1 — one grammar per request; the server's own tool grammar collides).
  Refined by the W1 spike (§0.1), which corrected this rule in both directions:
  - `tool_choice="none"` + a constraint is **legal** and works (tools declared but
    suppressed) — the blanket "non-empty tools → raise" was too strict.
  - The raise must cover **`json_schema` as well as `grammar`**. The server 400s on
    `grammar` + tools but returns **200 on `json_schema` + tools while silently
    disabling tool calling**, and the model fabricates a schema-shaped answer. Only
    a τ-side raise catches that one.

### 3.2 Where constraints enter (and where they don't)

- **`stream_simple` / `complete_simple`** (`tau-llm/src/tau_llm/client.py:31,91`):
  accept `options["constraints"]` — this is the SDK-level surface, done first.
- **C1 `ctx.complete(messages, model=..., constraints=...)`** — the primary
  consumer (JMFTS §9.1). The constraint parameter lands in C1's signature from
  day one; C1's model-registry resolution + this plan's gating compose with no
  extra work.
- **`AgentSession.prompt(..., constraints=...)`** — a per-*prompt* one-shot
  applying to every completion of that turn. Niche for the main loop (whose
  completions need free text + tool calls) but nearly free once the loop threads
  options, and it is what a future headless structured-output mode
  (`tau -p --json-schema out.json "extract ..."`) rides on.
- **Deliberately NOT a new hook.** No `before_model_call` mutating hook in this
  plan. Constraints are explicit arguments at call sites that own the call
  (extension code, C1, prompt). A hook that silently rewrites decode parameters
  for calls it doesn't own is a hidden channel with veto-ordering questions —
  YAGNI until a concrete story demands it, and the tree-as-truth invariant says
  automation should be *inspectable modification*, not interception.

### 3.3 Observability (tree-as-truth)

- The applied constraints are echoed on the result: `AssistantMessage` gains no
  new field, but the loop's `message_end` AgentEvent and C1's optional `ext:`
  observability record carry `{"constraints": {...}}` — what constrained a
  generation is inspectable after the fact.
- Persisted assistant entries produced under constraints get the constraint
  summary in the entry payload (`details`-style, display-only) so the TUI tree
  view can mark them. The context fold ignores it (not a message; never replayed).

## 4. Grammar tooling in τ (`tau_llm/grammar.py`, new module)

Extensions shouldn't hand-write Lark for the common cases. A small, dependency-free
composer — **string generation only**, no parsing, no llguidance reimplementation:

### 4.1 Helpers

```python
choice(*alternatives)             # start: "a" | "b" | "c"
fixed(text)                       # start: "exact string"
json_object(schema: dict)         # → prefer response_format passthrough (§3.1);
                                  #   provided for symmetry when a raw grammar is needed
regex(pattern)                    # start: /pattern/   (llguidance lark supports regex terminals)
sequence(*parts)                  # concatenation of the above
```

Each returns grammar *text* (no `%llguidance` header — the provider owns
prefixing). Escaping rules (quotes, backslashes) are the actual work here and get
exhaustive unit tests; a mis-escaped grammar is a silently-wrong constraint.

### 4.2 The verdict pattern (why this exists)

The retrieval-review story (JMFTS §9): N concurrent `ctx.complete()` calls, each
asked "include this document?" with
`constraints=DecodeConstraints(choices=["include", "exclude", "examine-children"])`.
Under jump-forward, each verdict costs ~1 forward pass regardless of the token
length of the chosen alternative — the grammar forces everything but the decision
point. This is the "short structured responses where half (here: nearly all) the
tokens are grammar-generated" capability, and it's the demo that matters for
multi-cursor throughput.

### 4.3 Constraint verification (the SPM lesson)

The recon found the failure mode where llguidance dies mid-generation, logs
server-side, and **generation continues unconstrained** — invisible to the
client. τ never trusts a constraint blindly:

- `choices` → assert the returned text is exactly one of the alternatives.
- `json_schema` → assert the returned text parses as JSON (full schema
  validation optional; parse failure alone catches constraint death).
- `grammar` (raw) → no general check possible; caller may pass
  `verify: Callable[[str], bool]` — absent that, τ at minimum asserts non-empty
  output and documents the residual risk.

A failed verification **raises** (`ConstraintViolation`, carrying the output) —
an unconstrained result returned as constrained is fabricated data.

## 5. Main-loop tool calls: server-side constraint, τ-side proof

### 5.1 Division of labor (important negative decision)

τ does **not** compile its registered tools into a grammar for the main loop.
The tool-call wire format on `/chat/completions` (which special tokens open a
call, how the JSON is framed) is owned by the model's chat template; llama-server
with `--jinja` already builds the lazy tool grammar from the same `tools` array τ
sends, per model family. Jump-forward then accelerates those forced spans (the
`{"file_path": "` scaffolding of a `read` call) with **zero τ changes**. τ
hand-building a competing grammar would fight the template and break on every
model family. τ's leverage is instead:

1. **Send-side**: pass `tool_choice` through (`"required"` for dispatch-only
   turns; an extension forcing "you must answer with a tool call" composes with
   the server grammar), via `DecodeConstraints.tool_choice` on `prompt()`.
2. **Proof-side**: instrument what the server claims and what τ observes (§5.2).

### 5.2 Repair-counter instrumentation

`parse_json_with_repair` gains a counter surfaced per completion (how many
tool-arg payloads needed repair) → `message_end` event + session totals. The
hypothesis "a grammar-constrained server produces repair-free tool JSON" becomes
a measured fact per model/server pairing, and a regression alarm: repairs
reappearing under a `--jinja` server means the constraint pipeline broke.

### 5.3 Integration bench (uses §6 telemetry)

A `tests/integration` bench, gated by `LLAMA_TEST_URL` (mirrors the JMFTS-plan
`JMFTS_TEST_URL` pattern): a fixed multi-tool transcript replayed against
(a) stock llama-server, (b) the jump-forward fork; assert byte-compatible tool
behavior and report forced-token share + effective t/s deltas. This is τ's half
of the roadmap's §5.8 demo measurement.

## 6. Telemetry: stop dropping the server's numbers

- **`Usage` gains `extra: dict[str, Any] = {}`** (stays frozen; populated at
  construction). `_usage_from_openai` stashes the raw `timings` object from the
  final SSE chunk when present (llama-server sends `prompt_ms`,
  `predicted_per_second`, draft stats; the fork adds `n_ff_total`). Unknown keys
  ride along untyped — τ doesn't chase the server's schema.
- **`DoneEvent` unchanged** (usage already rides it); the agent loop copies
  `usage.extra` onto the `message_end` AgentEvent it already emits, so backends,
  extensions (`ctx.get_usage()`), and the headless `--mode json` stream all see it.
- **TUI**: footer/status shows effective t/s and, when `n_ff_total` is present,
  forced-token share for the last completion (e.g. `⚡ 41.2 t/s · 68% forced`).
  Cheap, and it's the live readout of the whole platform story.
- **Token-source attribution** (`token_source: "forced"|"sampled"` per streamed
  token) is an LMTP-protocol concept, not available over `/chat/completions` SSE.
  Reserved: τ streaming events get no per-delta source field until the LMTP
  backend exists (§7.3). Do not fake it from timings.

## 7. KV-branch readiness

The server recon's conclusion: fork-at-tip works everywhere (including
Kimi-Linear); path 1 makes the stock REST API transparently prefix-sharing when
an incoming request has a long LCP against a live slot. **τ's obligation in both
cases is the same: byte-stable prompt prefixes.** The fork API (path 2) adds an
explicit handle later.

### 7.1 The prefix-stability contract (new invariant + tests)

Invariant: for consecutive completions in one agent turn, and for a turn N+1
following turn N, the serialized request `messages` array of the later call must
have the earlier call's serialization as an **exact prefix** (modulo the appended
suffix), except across explicitly prefix-breaking operations (compaction,
navigate, model change).

Known threats, from the audit — each gets a contract test over captured payloads
(fake-SSE-server harness, no real model needed):

1. **`reasoning_replay="turn"`** (the deliberate pi divergence): prior-turn
   thinking blocks are stripped when the next user message arrives, so turn N+1's
   serialization diverges from turn N's **at turn N's first thinking-bearing
   assistant message**. Cost shape: one turn's worth of re-prefill per turn —
   everything older is already thinking-stripped and stable. `"off"` is fully
   prefix-stable; `"all"` is stable but pays the payload bloat the setting exists
   to avoid. Action: measure, document the three-way tradeoff in the
   `reasoning_replay` docstring, and leave `"turn"` the default — one turn of
   re-prefill against a warm cache is cheap; a wrong default that bloats every
   context is not. Revisit if branch fan-out measurements say otherwise.
2. **Within-turn stability** (completion k vs k+1 while tool-calling): must be
   exact — same system prompt bytes, `convert_to_llm` determinism, stable dict
   key order (Python dicts preserve insertion order; the test pins it against
   regressions), no timestamps or counters leaking into serialized content.
3. **C2 fan-out identity**: N sub-agents branched at entry X must serialize the
   shared prefix (system prompt + path-to-X) **byte-identically** across all N
   requests. This is the property that makes path-1 slot-LCP forking (and any
   future explicit fork) actually fire. Test: spawn N mock branches, capture N
   payloads, assert common prefix == the branch-point serialization.

### 7.1.1 G5 delivered: the contract tests (W9, 2026-07-12)

All three threats above now have a contract test, driving the REAL
`OpenAICompletionsProvider` / `ExtensionContext.complete()` against a captured
in-memory HTTP payload (no live model) — no behavior changed, tests only.

- `tau-llm/tests/test_prefix_stability.py`
  - `test_within_turn_stability_across_tool_call_round_trips` (threat 2): drives
    a 3-request tool-calling turn (assistant → tool call → tool result →
    assistant, twice in a row) through one provider instance and asserts each
    request's serialized `messages` is a strict **byte prefix** of the next's.
  - `test_reasoning_replay_turn_breaks_prefix_at_the_dropped_thinking_block` /
    `test_reasoning_replay_all_keeps_the_same_turn_boundary_prefix_stable`
    (threat 1): **measures and pins** the deliberate pi divergence. With
    `reasoning_replay="turn"` (τ's default), the assistant message that carried
    the previous turn's thinking is proven to be **rewritten in place** —
    not merely superseded — the moment a new user message opens the next turn:
    every other shared message (the user message before it, the tool result
    after it) stays byte-identical, but that one message's serialized dict
    loses its `reasoning_content` key. `reasoning_replay="all"` is proven to
    keep the exact same message byte-identical across the same turn boundary.
    **This is confirmed working as designed, not a bug** — see the
    `Model.reasoning_replay` docstring (`tau_llm/types.py:~153`) and §9 decision
    5 above. Do not "fix" the default to pi parity; these tests exist so a
    future change to that default has to consciously break them.
- `tau-agent-core/tests/test_prefix_stability_fanout.py` (threat 3, using the
  REAL C1 primitive — `ExtensionContext.complete()` — since C2 branch spawning
  doesn't exist yet; `ctx.complete()` is the fan-out primitive that does):
  `test_n_way_fan_out_shares_a_byte_identical_prefix` fans N=8 concurrent
  `ctx.complete()` calls sharing a 3-message prefix out through a real
  `AgentSession`, and asserts the captured `messages[:3]` serializes to the
  identical compact-JSON string across all 8 requests, while the one
  per-branch suffix message is genuinely distinct per branch (so the shared
  prefix assertion isn't vacuous). `test_fan_out_prefix_matches_the_branch_point_serialization`
  additionally checks the shared prefix bytes match serializing the branch
  point in isolation — fanning out doesn't itself perturb the shared bytes.

**Byte-level, not dict-level, on purpose.** Every assertion compares
`json.dumps(messages, separators=(",", ":"))` strings, not parsed
lists-of-dicts — a structural-equality check would silently pass a reordered
key or a re-serialized float, either of which still changes the bytes a
server tokenizes and would still break its prefix match. The shared
`_assert_byte_prefix` helper (replicated in both test files — tau-agent-core
may depend on tau-llm, never the reverse, so it can't be imported once) trims
the earlier request's closing `]` and requires the later request to start
with exactly that string, so "prefix" means what it must mean for KV-cache
reuse: the later request only ever *appends* array elements.

No unexpected prefix breaks were found — the three threats identified by the
audit (§7.1) are the only three that fired against the tested shapes.

### 7.2 Concurrency plumbing that follows from the recon

- Branches are sequence ids bounded by the server's `-np`; τ's C2 spawner needs a
  configurable fan-out cap (`models.<name>.max_parallel` or a C2 spawn arg) so a
  50-way review against `-np 8` queues instead of erroring — surfaced, not
  hidden: the cap is config the user set, and hitting it is logged.
- `Model.extra_body: {"cache_prompt": true}` (default-on server-side, but pin it
  explicitly in the shipped `local-llm` template) plus a config recipe in docs
  for `--kv-unified` servers, so the τ-side story is reproducible.

### 7.3 Explicit fork API (blocked on server path 2 / LMTP — shape reserved only)

When `POST /slots/{id}/fork` or LMTP `GENERATE {branch_id, fork_from}` exists:

- `DecodeConstraints` (or a sibling `BranchHints`) gains
  `branch_of: str | None` — a τ session-tree **entry id**; the provider layer
  maps entry ids → server branch handles (the mapping table lives in the
  provider/backend, never in the tree).
- Natural correspondence to build on: τ fork points *are* session-tree branch
  points (C2 `spawn_branch(parent_id, ...)`), and τ owns them explicitly — the
  recon's argument for path 2 ("the harness knows its fork points, explicit
  beats heuristic") is precisely τ's shape.
- An LMTP backend would be a new `tau_llm` provider (`api: "lmtp"`) implementing
  the same streaming-event contract, at which point per-token `token_source`
  attribution (§6) becomes real. **No code in this plan** — the wire protocol
  doesn't exist yet; the reservation is: nothing in G0–G5 may assume
  one-request-per-completion-with-no-session-affinity in a way that would make a
  branch-handle field impossible to thread later. (Today's `options` dict + the
  provider registry already satisfy this.)

## 8. Phases

| Phase | Deliverable | Depends on | Exit criterion |
|---|---|---|---|
| G0 | `Model.grammar_dialect` / `Model.extra_body` / `server_features`; config + `build_model_from_config` plumbing | — | `extra_body` params observed in captured request payloads; unknown dialect raises |
| G1 | `DecodeConstraints` + provider wire mapping + gating/conflict raises; fake-SSE payload tests | G0 | grammar/choices/json_schema each land correctly in the payload; constraints+tools raises; no-dialect raises |
| G2 | `tau_llm/grammar.py` helpers + constraint verification (`ConstraintViolation`) | G1 | escaping unit tests green; a choices-constrained live call against local llama-server returns a verified verdict |
| G3 | C1 `ctx.complete(..., constraints=...)` integration (lands with/inside JMFTS C1) | G1, JMFTS C1 | an extension runs N concurrent verdict completions, all constraint-verified |
| G4 | Telemetry: `Usage.extra` ← `timings`; `message_end` carries it; repair counter; TUI readout | — (parallel) | forced-share/t/s visible in TUI + `--mode json` stream against the jump-forward fork |
| G5 | Prefix-stability contract tests (§7.1, §7.1.1) + `reasoning_replay` tradeoff docs. **Tests+docs DELIVERED** (W9, 2026-07-12); the §7.2 fan-out cap plumbing is NOT part of this and remains open. | — (parallel) | within-turn byte-prefix test green ✅; N-way fan-out identity test green, via C1 `ctx.complete()` since C2 branch spawning doesn't exist yet ✅; divergence points documented ✅ |
| G6 | Integration bench vs stock + fork servers (§5.3) | G4, server jump-forward | measured forced-token share + t/s delta on the fixed transcript; repairs == 0 under `--jinja` |
| G7 | Branch hints / LMTP provider | server path 2 | reserved; re-plan when the server API exists |

Phases G0–G5 are unblocked **now** and require only stock llama.cpp master.
The headless structured-output CLI (`tau -p --json-schema …`) is an optional
follow-on to G1+G2, scheduled with the CLI plan (docs/CLI-PLAN.md), not here.

## 9. Decision points (defaults chosen, flag to revisit)

1. **No `before_model_call` hook (chosen).** Constraints are explicit call-site
   arguments (C1, `prompt()`, SDK options). A decode-parameter interception hook
   is deferred until a story needs constraints on calls the caller doesn't own.
2. **τ does not build tool-call grammars for the main loop (chosen).** The chat
   template owns the wire format; the server owns the tool grammar; τ proves the
   effect instead (§5). Revisit only for a server/model pair that demonstrably
   lacks `--jinja` tool constraint and matters to us.
3. **llguidance-lark is the first-class dialect (chosen);** gbnf is passthrough-only.
4. **Constraint verification failures raise (chosen)** — an unverifiable
   constrained result is treated as fabricated data, per Fail-Early. The escape
   hatch is the caller-supplied `verify` (including an explicit "don't check")
   on raw grammars only.
5. **`reasoning_replay="turn"` stays the default (chosen)** despite its
   one-turn-deep prefix break (§7.1.1); measurement in G5/G6 can overturn this
   per-model via the existing config knob, not by a code change.
6. **`Usage.extra` is untyped (chosen).** The server's timing schema is moving
   (fork adds fields); τ carries it opaquely and types only what it renders.
   Confirmed by W1 (§0.1): stock master has no `n_ff_total`; the fork adds it.
7. **Constraint + tools raises whenever `tool_choice != "none"` (RESOLVED by W1,
   §0.1).** Covers `json_schema` as well as `grammar` — the server silently kills
   tool calling for the former rather than erroring, so τ is the only line of
   defence. `tool_choice="none"` + a constraint is explicitly permitted.
