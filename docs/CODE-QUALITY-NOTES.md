# Code Quality Notes

Observations gathered while diagnosing the tool-call parsing failure (2026-06-19). Ordered by severity. These are notes, not yet changes.

## The recurring theme: silent fallbacks hide bugs ("Fail Early")

The repo owner's standing rule is that fallbacks/placeholders/dummy data are an anti-pattern — a correct-but-late result beats a finished-but-fake one. The tool-call bug is a textbook case: the actual defect was a wrong accumulation, but what made it *hard to find* (and let it reach tool execution as corrupted data instead of crashing at the source) was a chain of silent fallbacks. Several findings below are instances of this.

> **Status (2026-06-19):** findings **#1, #2, #3, #4, #6, #8 are FIXED** (see `docs/TOOL-CALL-PARSING-BUG.md` → Resolution). Still open: **#5** (fake API key), **#7** (parsing now centralized in `json_parse.py` but the provider's build methods still duplicate the join/parse loop), **#9** (stray dir), **#10** (dedup), and new **#11** (streaming-test harness). Full suite: 1674 passed / 99 failed; the 99 are pre-existing and unrelated to the tool-call fix.

---

### 1. Tool-call argument corruption — CRITICAL ✱
`tau-ai/.../providers/openai.py:680-688`. The provider assumes each streaming chunk holds the complete cumulative arguments string and suffix-slices "the new portion"; OpenAI streams incremental fragments that must be concatenated. Result: malformed JSON → `{"raw": ...}` / `{}` reaches the tool. Full write-up + reproduction + fix in `docs/TOOL-CALL-PARSING-BUG.md`. This is the headline issue; everything else is secondary.

### 2. `{"raw": <string>}` fallback fabricates arguments on parse failure — HIGH ✱
Four copies: `openai.py:472-476` (partial build), `:508-513` (final build), `:709-711` (done-event yield), `:393-395` (`_convert_openai_choice_to_message`); plus a fifth in `agent_loop.py:499-503`. On a JSON error these invent `{"raw": "..."}`, which is not a valid argument object and silently corrupts the call. For a *complete* payload that fails to parse, this should **raise**; for a *partial* mid-stream payload, use a partial-JSON parser and return `{}` (see #4). This fallback is what turned a parse error into silent data corruption.

### 3. Parallel tool calls keyed by chunk position, not `index` — HIGH
`openai.py:660` computes `tc_index = tc_delta.get("index", i)` but never uses it; accumulation keys on the enumerate index `i` (`tool_call_index[i]`, `:665-672`). OpenAI sends `id`+`name` only on a call's first delta; later argument fragments carry only `index`. Position-keying mis-routes fragments across concurrent calls, so multi/parallel tool calls break even after fixing #1. pi keys by `index` then `id`.

### 4. Strict `json.loads` used on partial streaming JSON — MEDIUM
The live/partial paths (`_build_partial_message`, and `agent_loop.py:495-504` which parses a *single raw delta fragment*) call strict `json.loads`, which by definition fails on incomplete JSON and trips the `{"raw": ...}` branch on every mid-stream delta. pi uses `parseStreamingJson` (partial-tolerant) for partials and strict parsing only at finalize. τ has no equivalent; porting `~/Development/pi/packages/ai/src/utils/json-parse.ts` would remove a whole class of noise.

### 5. Fake API-key default — MEDIUM ✱
`openai.py:124`: `os.environ.get("OPENAI_API_KEY", "sk-fake-key-for-testing")`. A missing key for a real provider should fail loudly, not silently send a bogus key and surface as a confusing upstream `401`. Local-server semantics (`api_key == "not-needed"`) can stay, but the default-to-fake masks misconfiguration.

### 6. Debug `print()` scaffolding in the working tree — MEDIUM (cleanup)
Uncommitted changes add `[DEBUG …]` prints across `openai.py`, `agent_loop.py`, `backends.py`, `app.py`. Useful for the current hunt; remove once the fix lands (they also call `_build_partial_message` / re-`json.dumps` on every delta, adding per-chunk overhead). Consider replacing with `logging` at DEBUG level rather than `print`.

### 7. Duplicated "join → parse → fallback → ToolCall" logic — MEDIUM (DRY)
The same ~8-line block is repeated three times within `openai.py` alone (`_build_partial_message`, `_build_final_message`, the done-event yield loop) and partially again in `agent_loop.py`. One helper (`_finalize_tool_call_args(parts) -> dict`) would localize the parsing contract so a fix happens in exactly one place — directly relevant to why this bug had five sites to chase.

### 8. Redundant accumulation in the agent loop — LOW
`agent_loop.py:479-515` re-derives tool-call arguments from each raw `ToolCallDeltaEvent.delta` instead of reading the already-accumulated value off `event.partial` (the provider-built partial `AssistantMessage`). The provider already owns accumulation; the loop should consume its result, not redo it (incorrectly).

### 9. Stray duplicate package dir — LOW
Both `tau-coding-agent/` (the real `src/`-layout package) and a top-level `tau_coding_agent/` (containing `widgets/chat_display.py`) exist. The latter looks like a pre-restructure remnant not covered by `[tool.setuptools.packages.find]`. Confirm it's dead and remove, or fold it in — two import roots with the same name invite confusion.

### 10. Fragile prompt/context de-duplication — LOW
`agent_loop.py:143-183` compares the incoming prompt against the last context message by stringifying and `.strip()`-comparing text blocks to skip a "duplicate." This is brittle (whitespace/multimodal-sensitive) and easy to break silently; worth a comment explaining the exact invariant it protects, or a more explicit dedupe key.

### 11. Streaming tests never feed `aiter_lines` — HIGH (test harness)
The provider reads SSE via `response.aiter_lines()`, but the `MagicMock` responses in `test_subphase3.py` and `test_openai_provider.py` only set `.text` (the full SSE body) and never implement `aiter_lines`. A default `MagicMock().aiter_lines()` async-yields **zero** lines, so the SSE parser is never exercised: the stream produces a `DoneEvent` with `final=None`, and the ~27 streaming tests fail on `'NoneType' object has no attribute 'content'` — regardless of whether the parsing logic is correct. This is why the tool-call streaming tests were red even before (and after) the fix. Real httpx supplies `aiter_lines`, so this is purely a mock gap. Giving each mock response an `aiter_lines` that yields the lines of its body (as `tau-ai/tests/test_tool_call_streaming_fix.py` does) would turn those tests into genuine validations. The deeper ~66 `test_agent_session.py` / `test_phase6_subphase3_integration.py` failures are a *separate*, higher-level issue (session/loop wiring still in progress) and are out of scope for the tool-call work.

---

## Cross-cutting suggestions

- **Add a streaming-fragment regression test.** A faux provider that emits arguments across several chunks (like pi's `faux-provider`) would have caught #1/#3 immediately. The existing `tau-ai/tests/test_openai_provider.py` evidently uses single-chunk fixtures. pi keeps such cases under dedicated provider tests — mirror that.
- **Parse the tool-call contract in one place.** Most findings (#1, #2, #4, #7, #8) stem from argument parsing being re-implemented at every layer. Centralizing it in the provider — fragments in, validated `dict` out, raise on genuine failure — collapses the surface area.
- **Honor the spec citations.** Files reference `PHASE-*` docs; when behavior is ported from pi, cite the pi file/line too, so future divergences are auditable against the source of truth.
