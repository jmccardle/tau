# PR: do not apply a user grammar inside the reasoning block

Upstream: [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp)
Branch: `pr/lazy-grammar-thinking` → https://github.com/jmccardle/llama.cpp/tree/pr/lazy-grammar-thinking
Commit: `17985b8d` (base: `99f3dc32`)
Refs: [#20345](https://github.com/ggml-org/llama.cpp/issues/20345) (open), [#12276](https://github.com/ggml-org/llama.cpp/issues/12276) (closed as stale)

Diff: `common/sampling.cpp` **+21/−4** (mostly comment), plus one server test and a
three-line test-harness knob.

---

## Title

`server : do not apply a user grammar inside the reasoning block`

## Body (paste into the PR)

### The bug

A user-supplied grammar (the `grammar` API field) is applied from the **first generated
token**. On a chat template that force-opens its thinking tag, that token is already
*inside* the reasoning block. So the grammar constrains the model's **reasoning** instead
of its answer: the constrained output is emitted into the reasoning channel, and `content`
comes back empty.

```bash
curl http://localhost:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "messages": [{"role": "user", "content": "Is a doc on expired TLS certs relevant to a gateway cert outage? Answer include or exclude."}],
  "max_tokens": 600,
  "grammar": "root ::= \"include\" | \"exclude\""
}'
```

```json
{"choices": [{"message": {"content": "", "reasoning_content": "include"}}]}
```

The grammar held perfectly. The answer just is not where anybody reads it. To a client this
looks like the server *dropped* the constraint, which is the exact opposite of what
happened — so it is easy to misdiagnose and hard to notice: nothing errors.

Reproduced on `master` (`99f3dc32`) with Qwen3.5-2B and Qwen3.5-35B, on both GBNF and
llguidance grammars.

**There is no client-side workaround.** On the OAI path, `oaicompat_chat_params_parse`
overwrites `grammar_lazy` and `grammar_triggers` with the template-derived values
(`server-common.cpp:1100-1106`), so a caller cannot defer the grammar itself.

This is the still-open half of #20345. #20223 fixed the analogous problem for
`response_format` / `json_schema` — those grammars are built by the chat template and now
model the reasoning block explicitly, so they work (verified: 3,661 chars of free reasoning,
then a constrained answer). A user grammar is never built by the template, so it never got
that treatment. #12276 requested exactly this ("model free to think, strict with an answer
format") and was closed as stale.

### The fix

llama.cpp **already has the mechanism.** `grammar_should_apply()` suppresses the grammar
while the reasoning block is open, and `common_sampler_init()` builds the reasoning-state
tracker (`rbudget`) it depends on.

Both were gated on `grammar_lazy`, so a plain user grammar got neither: no tracker, and
therefore `grammar_should_apply()` returned `true` from token 0.

This extends both to `COMMON_GRAMMAR_TYPE_USER` — a type the codebase already maintains.

Grammars built by the chat template (`json_schema`, tool calls) are **deliberately left
alone**: they model the reasoning block themselves and are prefilled with the generation
prompt, so they are already reasoning-aware and must keep seeing every token. Suppressing
their accepts would desync them and break the path #20223 just fixed.

Because the suppression lives in `common_sampler`, *above* the grammar backend, this fixes
**llguidance** grammars as well — they take a separate sampler that never sees
`grammar_lazy` / `grammar_triggers`, so a trigger-based fix could not have reached them.

### After

```json
{"choices": [{"message": {
  "content": "include",
  "reasoning_content": "Thinking Process:\n\n1. **Analyze the Request:** ..."
}}]}
```

Free reasoning, then an answer that obeys the grammar. Same result with an llguidance
grammar (`%llguidance` + lark).

### Scope / known limitation

Thinking templates come in two styles, and this fixes one of them:

| style | example | before | after |
|---|---|---|---|
| **force-open** (prompt ends with `<think>`) | Qwen3.5 | **broken** — answer trapped in `reasoning_content`, `content` empty | **fixed** — reasons freely, then obeys the grammar |
| **model-initiated** (model emits `<think>` itself) | Qwen3-0.6B | grammar clamps at token 0, so the model never opens the block: a valid constrained answer, no reasoning | unchanged |

The model-initiated case is not broken today — you get a correct constrained answer, you
just don't get reasoning — and this PR does not change it. Letting such a model *both*
reason and be constrained requires composing the reasoning block into the grammar (what
#20223 does for `json_schema`), which is not possible for an opaque GBNF or llguidance
grammar supplied by the user. That is a separate feature.

One honest failure mode: if a model never closes its reasoning block, the grammar never
engages and output stays unconstrained (I watched a 2B ramble 12k characters). This
composes with `--reasoning-budget`, which force-closes the block.

### Testing

New: `tools/server/tests/unit/test_chat_completion.py::test_completion_with_grammar_and_thinking`
(`@pytest.mark.slow`, Qwen3.5-2B — needs a model whose tokenizer actually has the thinking
tags, and whose template force-opens them). A `reasoning_budget` knob was added to the test
harness to bound how long the model may think, keeping it quick.

Verified the test has teeth — with `common/sampling.cpp` reverted to `master` it fails with
exactly the bug:

```
AssertionError: {'content': '', 'reasoning_content': 'aaaaa', 'role': 'assistant'}
```

Suites run on this branch (all pass):

- server: `test_chat_completion.py` (53), `test_tool_call.py`, `test_completion.py` (38), `test_template.py` (35)
- C++: `test-chat`, `test-chat-auto-parser`, `test-chat-peg-parser`, `test-grammar-integration`, `test-sampling`, `test-json-schema-to-grammar`, `test-grammar-parser`

Manual regression matrix on Qwen3.5-2B — `json_schema` + thinking still works (701 chars
reasoning + `"exclude"`), tool calls + thinking still work (`get_weather {"city":"Paris"}`),
grammar with thinking disabled still works, plain chat unaffected.

---

## Separate bug found while investigating (NOT in this PR)

`reasoning_format=none` + `response_format` → **HTTP 400**, `Failed to initialize samplers`.

Root cause: with `reasoning_format=none` the PEG parser omits the reasoning block from the
generated grammar (`root ::= "<|im_start|>assistant\n" space space (json…)`), but the
prompt still force-opens `<think>`, and template-derived grammars are prefilled with the
generation prompt (`common/sampling.cpp:284`). The prefill feeds `<think>` into a grammar
that does not allow it, and throws.

The underlying conflation: `ctx.extracting_reasoning` drives *both* what the grammar lets
the model **emit** and how the reasoning is **reported**. `reasoning_format` should only
control reporting.

Contributor `aldehir` already noted on #20345 that the #20223 fix "does not work when
reasoning format = none". This is why. Different subsystem (the autoparser), so it belongs
in its own PR — worth posting on #20345.

---

## Why this matters to τ

τ disables thinking on every constrained call (`tau_llm/providers/openai.py`,
`_apply_constraints` sets `chat_template_kwargs: {"enable_thinking": False}`) purely to
route around this bug. With this fix, that workaround can go for raw grammars and `choices`.

τ's `local-llm` uses the **llguidance** dialect, which is why the "suppress above the
grammar backend" placement matters: a lazy-trigger fix in the GBNF sampler would not have
helped τ at all.

Independently of upstream: τ's `enable_thinking: False` is set *before* the `json_schema`
branch, which already goes out as `response_format` — the path that works with thinking on
stock llama.cpp today. That blanket is over-broad and costs reasoning we could already have.

See [`REASONING-VS-CONSTRAINED-DECODING.md`](./REASONING-VS-CONSTRAINED-DECODING.md), whose
original diagnosis (grammar_lazy clobbering) was **superseded** by the findings here.
