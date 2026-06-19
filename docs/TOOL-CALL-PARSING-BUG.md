# Tool-Call Argument Parsing — Root-Cause Diagnosis

**Status:** ✅ RESOLVED (2026-06-19) — see "Resolution" at the bottom.
**Severity:** critical — all multi-chunk tool calls executed with empty or garbage arguments.
**One-line cause:** the OpenAI provider assumed each streaming chunk carried the *complete cumulative* tool-call argument string, when OpenAI in fact streams *incremental fragments* that must be concatenated.

> The diagnosis below describes the original (pre-fix) code and line numbers. The fix has been applied; jump to **Resolution** for what changed and how it was verified.

---

## Symptom

When the model emits a tool call, the executed tool receives `{}` or `{"raw": "<garbled>"}` instead of the real arguments. The debug instrumentation shows the `{"raw": ...}` fallback firing in `openai.py` (`PARSE FAILED`), and the corrupted value then propagates unchanged through every downstream hop (`agent_loop` → `backend` → `app`).

## Root cause

`tau-ai/src/tau_ai/providers/openai.py`, in `stream_chat`'s `event_generator`, lines **680–688**:

```python
if tc_args and tc_id and tc_id in accum.tool_calls:
    # OpenAI streaming sends the complete accumulated argument
    # string on every chunk, not just the delta. Reconstruct
    # by tracking the last accumulated length and only appending
    # the new portion.
    tc_acc = accum.tool_calls[tc_id]
    last_args = "".join(tc_acc.arguments_parts)
    if len(tc_args) > len(last_args):
        tc_acc.arguments_parts.append(tc_args[len(last_args):])
```

**The premise in the comment is false.** The OpenAI Chat Completions streaming API — and every OpenAI-compatible server (vLLM, Ollama, llama.cpp, …) — sends `delta.tool_calls[].function.arguments` as an **incremental fragment** in each chunk. The correct reconstruction is plain concatenation. This code instead assumes a cumulative string and tries to append "only the new portion," which produces two compounding failures when fed real fragments:

1. **Suffix-slicing corrupts each fragment.** `tc_args[len(last_args):]` slices the *fragment* at an offset equal to the *total accumulated length*, dropping the fragment's leading characters.
2. **The length gate silently drops fragments.** Once `last_args` is longer than an incoming fragment (true almost immediately, and permanently thereafter), `len(tc_args) > len(last_args)` is `False` and the fragment is discarded entirely.

The joined `arguments_parts` is therefore malformed/truncated JSON; `json.loads` raises; and the code falls back to `{"raw": args_str}` (or `{}`).

### Why it sometimes appears to work

If a server returns the whole arguments object in a **single** chunk (short args, or a server that doesn't fragment arguments), then on that one chunk `last_args == ""`, the slice `tc_args[0:]` is the entire string, and parsing succeeds. Unit tests built from single-chunk fixtures pass, masking the defect. The default config targets a `local-llm` server, which fragments aggressively — so real use breaks.

## Reproduction

`/tmp/repro_toolcall_bug.py` runs τ's exact accumulation logic and pi's, on the canonical fragment stream for `{"location": "San Francisco", "unit": "celsius"}`:

```
fragments = ['{"', 'location', '": "', 'San Fran', 'cisco", ', '"unit": ', '"celsius"', '}']

tau (buggy):   accumulated='{"cation"'
               json.loads FAILED (Expecting ':' delimiter: line 1 column 10) -> falls back to {'raw': ...}
pi  (correct): accumulated='{"location": "San Francisco", "unit": "celsius"}'
               json.loads OK -> {'location': 'San Francisco', 'unit': 'celsius'}
```

Trace of how τ produces `{"cation"`:

| chunk fragment | `last` len | `len(frag) > len(last)`? | appended (`frag[len(last):]`) | accumulator |
|---|---|---|---|---|
| `{"`        | 0 | yes | `{"`     | `{"` |
| `location` | 2 | yes | `cation` (drops `lo`) | `{"cation` |
| `": "`     | 8 | no  | — dropped | `{"cation` |
| `San Fran` | 8 | no  | — dropped | `{"cation` |
| `cisco", ` | 8 | no  | — dropped | `{"cation` |
| `"unit": ` | 8 | no  | — dropped | `{"cation` |
| `"celsius"`| 8 | yes | `"` (drops `"celsius`) | `{"cation"` |
| `}`        | 9 | no  | — dropped | `{"cation"` |

## The reference (pi) behaviour

`~/Development/pi/packages/ai/src/providers/openai-completions.ts:360-364`:

```ts
let delta = "";
if (toolCall.function?.arguments) {
    delta = toolCall.function.arguments;
    block.partialArgs = (block.partialArgs ?? "") + toolCall.function.arguments; // concatenate
    block.arguments = parseStreamingJson(block.partialArgs);
}
```

Two things τ is missing:

- **Concatenation** of fragments (the fix below).
- **`parseStreamingJson`** (`pi .../utils/json-parse.ts`) — a partial-JSON-tolerant parser used for the *live* value during streaming, so an incomplete-but-growing buffer yields a best-effort object instead of an exception. pi reserves a strict parse for the finalized buffer. τ uses strict `json.loads` everywhere, which is why every mid-stream partial also trips the `{"raw": ...}` branch.

## The fix

### Minimal (resolves the corruption)

Replace `openai.py:680-688` with a plain append:

```python
if tc_args and tc_id and tc_id in accum.tool_calls:
    accum.tool_calls[tc_id].arguments_parts.append(tc_args)
```

With this, `arguments_parts` joins to the exact concatenation of the fragments, `json.loads` succeeds at finalize, and the real arguments reach the tool. (Confirmed by the reproduction: the same fragments then parse to `{'location': 'San Francisco', 'unit': 'celsius'}`.)

### Complete (matches pi, removes the masking)

1. **Concatenate** as above.
2. **Stop fabricating data on parse failure.** Remove the `{"raw": args_str}` fallbacks. For a *partial* (mid-stream) buffer, use a partial-JSON parser (port `parseStreamingJson`) and return `{}` until it parses. For the *final* buffer, a parse failure is a real error — raise / emit an `ErrorEvent` rather than inventing `{"raw": ...}` (see "Fail Early" in `docs/CODE-QUALITY-NOTES.md`).
3. **Key tool calls by `index`, not enumerate position.** `tc_index = tc_delta.get("index", i)` is computed at `openai.py:660` but never used; accumulation falls back to the chunk-local position `i`. For **parallel** tool calls, follow-up argument fragments arrive with only `index` (no `id`), so position-keying mis-routes them. Key by the OpenAI `index` field first, then `id` (pi does this via `toolCallBlocksByIndex` / `toolCallBlocksById`). Without this, parallel/multi tool calls stay broken even after the concatenation fix.

## Sites touched by the same defect family

| File | Lines | Issue |
|---|---|---|
| `tau-ai/.../providers/openai.py` | 680–688 | **root cause** — cumulative-string assumption |
| `tau-ai/.../providers/openai.py` | 660, 665–672 | `index` computed but unused; position-keyed accumulation (parallel-call bug) |
| `tau-ai/.../providers/openai.py` | 472–476, 508–513, 709–711, 393–395 | four copies of the `{"raw": ...}` fallback |
| `tau-agent-core/.../agent_loop.py` | 495–504 | partial display re-parses a *single* raw delta fragment with strict `json.loads` (always fails mid-stream) |

> Note: the working tree previously had heavy `[DEBUG …]` `print()` instrumentation across `openai.py`, `agent_loop.py`, `backends.py`, and `app.py`. That scaffolding has been removed.

---

## Resolution

The complete fix (all five points above) was applied:

1. **`tau-ai/src/tau_ai/json_parse.py`** (new) — Python port of pi's `json-parse.ts`: `repair_json`, `parse_json_with_repair` (strict, raises), `parse_streaming_json` (best-effort partial parse, returns `{}`).
2. **`providers/openai.py`** — fragments are now **concatenated**; tool calls are accumulated in an ordered list keyed by stream **`index`** (then `id`) via `_resolve_tool_call_block`, so parallel calls route correctly; the *partial* (display) path uses `parse_streaming_json`, the *final* path uses `parse_json_with_repair` and **raises** on a complete-but-invalid payload (surfaced as an `ErrorEvent`); all four `{"raw": …}` fallbacks removed; dead `tool_call_index` removed.
3. **`agent_loop.py`** — the `ToolCallDeltaEvent` handler now consumes the provider's accumulated `event.partial` instead of re-parsing a single raw fragment; its `{"raw": …}` fallback removed.
4. **Debug scaffolding** removed from `openai.py`, `agent_loop.py`, `backends.py`, `app.py`.

### Verification

`tau-ai/tests/test_tool_call_streaming_fix.py` (new, 13 tests, all passing) drives the **real** provider with a fragmenting SSE feed (the mock implements `aiter_lines`, unlike the older fixtures):

- fragmented arguments concatenate to the correct dict;
- parallel tool calls whose follow-up fragments carry only `index` route correctly;
- empty arguments → `{}`;
- a complete-but-invalid final payload produces an `ErrorEvent` (no `DoneEvent`, no `{"raw": …}`);
- `json_parse` unit cases (repair, raise-on-incomplete, best-effort partials).

Full suite after the fix: **1674 passed / 99 failed** — the 99 are all pre-existing and unrelated (no regressions, +13 new passes; baseline was 1661/99).

### Known follow-up (separate issue)

The project's *existing* streaming tests (`test_subphase3.py`, `test_openai_provider.py`) still fail — but for a **test-harness** reason, not the parsing logic: their `MagicMock` responses set `.text` but never implement `aiter_lines`, which the provider reads. So `aiter_lines()` yields zero lines and the SSE parser is never exercised (the final message comes back `None`). Wiring `aiter_lines` into those mock builders would turn ~27 of them into real validations of this fix. Tracked as finding #11 in `CODE-QUALITY-NOTES.md`.
