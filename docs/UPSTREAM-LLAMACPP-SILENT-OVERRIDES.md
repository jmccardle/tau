# Upstream bug report: two silent constraint overrides in llama-server's OpenAI-compat endpoint

Draft for a llama.cpp issue. Both are reproduced live; both are cases where the server
has enough information to refuse and instead silently discards what the client asked for.

**Verified against:** llama.cpp master (`repos/llama-master`, `2da6686`+), built with
`-DGGML_CUDA=ON -DLLAMA_LLGUIDANCE=ON`. Reproduced on **two** models —
`Qwen3-8B-Q4_K_M` and `Qwen3.6-35B-A3B (IQ4_XS)`, both BPE. Endpoint:
`POST /v1/chat/completions`.

Both reproductions suppress thinking (`chat_template_kwargs: {"enable_thinking": false}`)
and allow generous `max_tokens`. Without that, a thinking model spends the whole budget
inside `<think>` and returns an empty `content`, which masks the actual behaviour.

---

## Bug 1 — `response_format` + `tools`: HTTP 200, tool calling silently disabled

The server explicitly refuses a raw `grammar` alongside `tools`:

> `tools/server/server-common.cpp:1066-1069` →
> `"Cannot use custom grammar constraints with tools."` (HTTP 400)

but there is **no equivalent check for `response_format` / `json_schema`**. The schema
grammar wins, tool calling is silently turned off, and the model invents a
schema-shaped answer instead of calling the tool the caller declared.

### Reproduction

Control — tools alone. The model clearly wants the tool:

```bash
curl -s localhost:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "q", "max_tokens": 400,
  "chat_template_kwargs": {"enable_thinking": false},
  "messages": [{"role":"user","content":"Read the file /etc/hostname and tell me what it says."}],
  "tools": [{"type":"function","function":{"name":"read","description":"Read a file from disk",
    "parameters":{"type":"object","properties":{"file_path":{"type":"string"}},"required":["file_path"]}}}]
}' | jq '.choices[0].message | {content, tool_calls}'
```

```json
{ "content": "", "tool_calls": [ { "function": { "name": "read",
    "arguments": "{\"file_path\":\"/etc/hostname\"}" } } ] }
```

Now add `response_format` — nothing else changes:

```bash
curl -s localhost:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "q", "max_tokens": 400,
  "chat_template_kwargs": {"enable_thinking": false},
  "messages": [{"role":"user","content":"Read the file /etc/hostname and tell me what it says."}],
  "tools": [{"type":"function","function":{"name":"read","description":"Read a file from disk",
    "parameters":{"type":"object","properties":{"file_path":{"type":"string"}},"required":["file_path"]}}}],
  "response_format": {"type":"json_schema","json_schema":{"name":"verdict","schema":
    {"type":"object","properties":{"verdict":{"type":"string"}},"required":["verdict"]}}}
}' | jq '.choices[0].message | {content, tool_calls}'
```

```json
{ "content": "{\"verdict\": \"exclude\"}", "tool_calls": null }
```

**HTTP 200. No warning. The tool call is gone**, replaced by a fabricated,
schema-shaped answer that looks entirely plausible to a client.

### Expected

Either a 400 (consistent with the existing `grammar` + `tools` check), or — if the
combination is intended to be legal — documented precedence and, ideally, the tool
grammar composed with the schema rather than replaced by it.

Note `tool_choice: "none"` + a constraint **is** legitimately useful and already works
(the client declares tools but suppresses them for one constrained turn); any new check
should exempt it, exactly as the existing `grammar` + `tools` check does.

---

## Bug 2 — `grammar` + `response_format`: the grammar is silently discarded

The mutual-exclusion check is keyed on the **top-level `json_schema`** field:

> `tools/server/server-common.cpp:929-932` — throws `"Cannot use both json_schema and grammar"`

But `response_format` is parsed on a **different path** (`:936`), so the same semantic
collision — a JSON-schema constraint and a grammar constraint in one request — is not
caught. `response_format` wins and the grammar is dropped without a word.

### Reproduction

```bash
G='%llguidance {}\nstart: "include" | "exclude"'
RF='{"type":"json_schema","json_schema":{"name":"v","schema":{"type":"object","properties":{"verdict":{"type":"string"}},"required":["verdict"]}}}'
BASE='"model":"q","max_tokens":300,"chat_template_kwargs":{"enable_thinking":false},"messages":[{"role":"user","content":"Should this document be included? Answer with the verdict."}]'
```

| request body | status | `content` |
|---|---|---|
| `{$BASE, "grammar": $G}` | 200 | `"include"` |
| `{$BASE, "grammar": $G, "json_schema": {...}}` | **500** | `Cannot use both json_schema and grammar` |
| `{$BASE, "grammar": $G, "response_format": $RF}` | **200** | **`{"verdict": "no"}`** ← grammar ignored |

The third row is the bug: a grammar restricted to `include|exclude` produced
`{"verdict": "no"}`, a string the grammar cannot generate.

### Expected

`response_format` + `grammar` should hit the same guard as `json_schema` + `grammar`
(and that guard should arguably be a **400**, not a 500 — it is a client error, not a
server fault).

---

## Why silent overrides are the worst possible failure here

The whole point of a decode constraint is that the client can *rely* on the shape of the
output. A 400 is fine — the client retries or fixes the request. A **200 with the
constraint quietly dropped** is not a degraded result, it is a **wrong result that is
indistinguishable from a right one**: an unconstrained generation returned as a
constrained one. Downstream, that becomes fabricated data.

This is the same class as the already-known llguidance failure noted in our recon:
on SPM-tokenizer models (e.g. Hermes-2-Pro-Mistral-7B) the llguidance parser dies
mid-generation, logs the error **server-side**, and then **allows everything** — the
client sees a 200 and plausible prose. Same shape: the server knows the constraint is
not in force, and does not say so.

**Suggested principle:** whenever llama-server determines that a requested constraint
will not be applied — because it collides with another, or because the parser failed — it
should fail the request rather than fulfil it unconstrained. A client cannot detect this
without re-implementing the grammar engine, which defeats the purpose of server-side
constrained decoding.

**A cheap, general fix that would also serve jump-forward:** report the number of
grammar-**forced** tokens on the response (the `timings` block is a natural home; the
LMTP protocol sketch already wants `token_source: "forced"|"sampled"` per token). A
client can then verify *that a constraint was in force at all* without knowing anything
about the grammar: a constrained request that forced **zero** tokens did not have its
constraint applied. That single number is a total, grammar-agnostic integrity check —
and it is the same number the jump-forward work needs anyway.

---

## Third, minor: SPM tokenizer + llguidance

Already known to us, filed here for completeness — worth a separate issue with a minimal
repro. `Hermes-2-Pro-Mistral-7B` (SentencePiece): llguidance errors mid-generation
(`forced bytes: got ' '; applying '"'`), the constraint dies, and generation continues
**unconstrained**. Detokenized output also shows mangled spacing (`"transaction _id "`),
pointing at the `▁`-space token-bytes mapping. BPE models (Qwen, gpt-oss, Kimi) are
unaffected.
