# Reasoning vs. constrained decoding

**Status:** τ currently disables thinking on any constrained call. That is a real
capability loss, it is a workaround, and it is fixable upstream in llama.cpp with a
small patch. This documents the finding, the mechanism, and the work.

Found 2026-07-12, when the rewired `examples/60_retrieval_review.py` returned nothing at
all against a reasoning model.

---

## 1. The finding

Send a grammar constraint to llama-server with a reasoning model (Qwen3-35B, `b1061-2da6686`)
and thinking enabled, and you get:

```json
{"choices": [{"message": {"content": "", "reasoning_content": "include"}}]}
```

**The grammar held perfectly. The verdict is correct. It just is not where anybody reads
it.**

A decode constraint applies from the *first generated token*. A reasoning model's first
generated token belongs to its thinking — Qwen's template force-opens `<think>` in the
assistant prefix, so generation begins *inside* the reasoning block. llama-server
therefore forces the constrained answer into the reasoning channel, emits EOS, and
returns an empty `content`.

τ reads `content`, sees `""`, and raises `ConstraintViolation` — whose message says *"the
server most likely dropped the constraint and ran free"*, which is the **exact opposite**
of what happened. The constraint was honoured too well.

**Consequence: every constrained call against a thinking model silently produced an empty
verdict.** `ctx.complete(constraints=…)` — the whole C1 primitive — was unusable on
reasoning models, and the failure looked like a server bug.

## 2. What τ does today (the workaround)

`tau_llm/providers/openai.py`, `_apply_constraints`: a constrained request sets

```python
payload["chat_template_kwargs"] = {"enable_thinking": False}
```

unless the caller supplied their own `chat_template_kwargs`.

This is correct as far as it goes, and for most constrained calls it is *also fine on the
merits*: a classifier, an extractor, or a relevance judge is choosing between two forced
tokens, and chain-of-thought rarely changes which one. The retrieval-review demo judges
correctly without it.

But it is a **trade we should not have to make**. Some constrained tasks genuinely want
reasoning first — a nontrivial routing decision, a difficult schema extraction, a judgement
that hinges on a subtle distinction. "You may have structure or you may have reasoning,
pick one" is not a real answer, and τ silently picking one for you is worse.

Pinned by `tau-llm/tests/test_decode_constraints.py::TestConstrainedCallsDoNotThink`,
including the guarantee that an *un*constrained call is left alone (this must never become
a global "τ turns thinking off" regression) — and that a `json_schema` →
`response_format` call is *also* left alone: its grammar is template-built and
reasoning-aware (llama.cpp upstream #20223), so only the grammar/choices paths, where a
user grammar binds from token 0, actually force thinking off.

## 3. The mechanism already exists in llama.cpp — it just isn't reachable

llama.cpp supports **lazy grammars**: a grammar that stays dormant until a trigger string
appears, then clamps on. It is exactly how tool calling works with reasoning models — let
the model think freely, then force the tool-call syntax.

Verified against the **native** `/completion` endpoint on the live box:

```json
{
  "grammar": "root ::= \"</think>\" [\\n]* (\"include\" | \"exclude\")",
  "grammar_lazy": true,
  "grammar_triggers": [{"type": 2, "value": "</think>"}]
}
```

The model thought freely for **3,742 unconstrained characters** — the grammar stayed
dormant — exactly the behaviour we want.

Two details learned the hard way:

- **The grammar must consume the trigger text.** With `root ::= "include" | "exclude"` and
  a `</think>` trigger, the server dies with
  `got exception: Unexpected empty grammar stack after accepting piece: </think>`. The
  trigger string is fed *into* the grammar when it fires, so the grammar has to start by
  matching it.
- **Trigger `type` is the integer enum** (`2` = `COMMON_GRAMMAR_TRIGGER_TYPE_WORD`), not a
  string.

### Why it doesn't work on `/v1/chat/completions`

`tools/server/server-common.cpp`, `oaicompat_chat_params_parse`:

```cpp
auto grammar = json_value(body, "grammar", std::string());   // :933  user grammar IS read
...
auto chat_params = common_chat_templates_apply(opt.tmpls.get(), inputs);   // :1091
...
llama_params["grammar_lazy"] = chat_params.grammar_lazy;     // :1099  UNCONDITIONAL
auto grammar_triggers = json::array();
for (const auto & trigger : chat_params.grammar_triggers) {  // :1101
    grammar_triggers.push_back(server_grammar_trigger(trigger).to_json());
}
llama_params["grammar_triggers"] = grammar_triggers;         // :1105  UNCONDITIONAL
```

The user's `grammar` survives, but `grammar_lazy` and `grammar_triggers` are **overwritten
unconditionally** with the template-derived values — which are `false` and `[]` unless
tools are in play. A client's lazy-grammar fields are silently clobbered.

(The native `/completion` path reads them from the request via `server-schema.cpp:282`,
`:348` — which is why it works there and not here.)

τ cannot simply switch endpoints: it needs `/v1/chat/completions` for chat templating.

## 4. The work

### Option A (preferred) — llama.cpp applies the trigger itself

When a request carries a user grammar **and** thinking is enabled, the server should make
the grammar lazy with a trigger on the reasoning-close delimiter, automatically.

**It is the only party that can do this correctly.** `</think>` is Qwen's convention;
DeepSeek, GLM, and others differ. A *client* that hard-codes a delimiter is encoding one
model family's chat template into a harness that claims to be model-agnostic. llama.cpp
already applied the template, already knows the delimiters, and already tracks
`thinking_forced_open` in `common_chat_params`.

Sketch, in `oaicompat_chat_params_parse` after the template is applied:

- if `!grammar.empty() && inputs.enable_thinking && chat_params.thinking_forced_open`
- wrap the user grammar so it consumes the delimiter first
  (`root ::= "</think>" ws user_root`), and
- set `grammar_lazy = true` with a `COMMON_GRAMMAR_TRIGGER_TYPE_WORD` trigger on the
  template's reasoning-close string.

Then τ removes the `enable_thinking: False` workaround entirely and gets thinking **and** a
verified constrained answer in one call, on any reasoning model, without knowing a single
thing about `<think>`.

### Option B (fallback) — just stop clobbering

Honour user-supplied `grammar_lazy` / `grammar_triggers` on the OAI path when the template
produced no tool-call grammar of its own. Smaller patch, but it pushes the delimiter
knowledge onto every client — precisely the coupling worth avoiding. Worth having anyway,
as an escape hatch.

### Option C (no upstream dependency) — two-phase completion in τ

One unconstrained call to think, a second constrained call carrying that reasoning as
context. Works on **any** provider, including hosted APIs where we will never patch the
server; costs two round trips. Worth building regardless of A/B, because A/B only ever fix
llama.cpp.

## 5. Reproduction

Source checkout: `~/Development/turboquant_experiments/repos/upstream-llama-cpp`
(`2da6686` — matches the running server's `system_fingerprint: b1061-2da6686`).

```bash
# Broken: OAI-compat silently ignores grammar_lazy/grammar_triggers.
curl -s $LLAMA/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model":"local-llm","max_tokens":300,
  "messages":[{"role":"user","content":"Is a doc on expired TLS certs relevant to a gateway cert outage?"}],
  "grammar":"%llguidance {}\nstart: \"include\" | \"exclude\"",
  "grammar_lazy": true,
  "grammar_triggers": [{"type": 2, "value": "</think>"}]
}'
# -> {"content": "", "reasoning_content": "include"}

# Works: the native endpoint honours them, and the model thinks freely first.
curl -s $LLAMA/completion -H 'Content-Type: application/json' -d '{
  "prompt":"<|im_start|>user\nIs a doc on expired TLS certs relevant to a gateway cert outage?<|im_end|>\n<|im_start|>assistant\n<think>\n",
  "n_predict":800,
  "grammar":"root ::= \"</think>\" [\\n]* (\"include\" | \"exclude\")",
  "grammar_lazy": true,
  "grammar_triggers": [{"type": 2, "value": "</think>"}]
}'
```
