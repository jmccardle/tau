# Code Quality Notes

Observations gathered while diagnosing the tool-call parsing failure (2026-06-19). Ordered by severity. These are notes, not yet changes.

## The recurring theme: silent fallbacks hide bugs ("Fail Early")

The repo owner's standing rule is that fallbacks/placeholders/dummy data are an anti-pattern — a correct-but-late result beats a finished-but-fake one. The tool-call bug is a textbook case: the actual defect was a wrong accumulation, but what made it *hard to find* (and let it reach tool execution as corrupted data instead of crashing at the source) was a chain of silent fallbacks. Several findings below are instances of this.

> **Status (2026-06-20):** **#1, #2, #3, #4, #6, #8 FIXED** (tool-call work — see `docs/TOOL-CALL-PARSING-BUG.md` → Resolution), then **#9 FIXED** (stray dir `git rm`'d), **#11 FIXED** (streaming-test mocks now feed `aiter_lines`), **#5 FIXED** (no fabricated key; raise on missing; key threaded end-to-end — see below), **#10 FIXED** (loop-level prompt dedup removed for pi parity — see below), and **#7 CLOSED/WONTFIX** (down to two intentionally-divergent parse sites). Full suite now **1360 passed / 0 failed**; mypy 57 (was 58). **All findings in this doc are now resolved or closed.**

---

### 1. Tool-call argument corruption — CRITICAL ✱
`tau-ai/.../providers/openai.py:680-688`. The provider assumes each streaming chunk holds the complete cumulative arguments string and suffix-slices "the new portion"; OpenAI streams incremental fragments that must be concatenated. Result: malformed JSON → `{"raw": ...}` / `{}` reaches the tool. Full write-up + reproduction + fix in `docs/TOOL-CALL-PARSING-BUG.md`. This is the headline issue; everything else is secondary.

### 2. `{"raw": <string>}` fallback fabricates arguments on parse failure — HIGH ✱
Four copies: `openai.py:472-476` (partial build), `:508-513` (final build), `:709-711` (done-event yield), `:393-395` (`_convert_openai_choice_to_message`); plus a fifth in `agent_loop.py:499-503`. On a JSON error these invent `{"raw": "..."}`, which is not a valid argument object and silently corrupts the call. For a *complete* payload that fails to parse, this should **raise**; for a *partial* mid-stream payload, use a partial-JSON parser and return `{}` (see #4). This fallback is what turned a parse error into silent data corruption.

### 3. Parallel tool calls keyed by chunk position, not `index` — HIGH
`openai.py:660` computes `tc_index = tc_delta.get("index", i)` but never uses it; accumulation keys on the enumerate index `i` (`tool_call_index[i]`, `:665-672`). OpenAI sends `id`+`name` only on a call's first delta; later argument fragments carry only `index`. Position-keying mis-routes fragments across concurrent calls, so multi/parallel tool calls break even after fixing #1. pi keys by `index` then `id`.

### 4. Strict `json.loads` used on partial streaming JSON — MEDIUM
The live/partial paths (`_build_partial_message`, and `agent_loop.py:495-504` which parses a *single raw delta fragment*) call strict `json.loads`, which by definition fails on incomplete JSON and trips the `{"raw": ...}` branch on every mid-stream delta. pi uses `parseStreamingJson` (partial-tolerant) for partials and strict parsing only at finalize. τ has no equivalent; porting `~/Development/pi/packages/ai/src/utils/json-parse.ts` would remove a whole class of noise.

### 5. Fake API-key default — FIXED ✓ (2026-06-20)
~~`openai.py`: `os.environ.get("OPENAI_API_KEY", "sk-fake-key-for-testing")`. A missing key for a real provider should fail loudly, not silently send a bogus key and surface as a confusing upstream `401`.~~ The constructor no longer fabricates a key (`api_key or os.environ.get("OPENAI_API_KEY")`, may be None), and `stream_chat` raises `ValueError("No API key for provider: …")` when none is resolved (from `self.api_key` or `options["api_key"]`) — mirrors pi `openai-completions.ts:141`. Local-server semantics stay: the truthy `"not-needed"` sentinel passes. Fixing this also surfaced that the key was never *threaded* to the provider (configuring a real OpenAI key silently did nothing), so the key now flows `AgentSession.api_key → AgentLoopConfig → stream_simple options → provider` and is stripped from the request body; `create_agent_session` and `backends.py` stopped dropping it. The 24 tests that depended on a live key now run offline via the `fake_llm` fixture (`tau-agent-core/tests/conftest.py`).

### 6. Debug `print()` scaffolding in the working tree — MEDIUM (cleanup)
Uncommitted changes add `[DEBUG …]` prints across `openai.py`, `agent_loop.py`, `backends.py`, `app.py`. Useful for the current hunt; remove once the fix lands (they also call `_build_partial_message` / re-`json.dumps` on every delta, adding per-chunk overhead). Consider replacing with `logging` at DEBUG level rather than `print`.

### 7. Duplicated "join → parse → fallback → ToolCall" logic — CLOSED / WONTFIX ✓ (2026-06-20)
~~The same ~8-line block is repeated three times within `openai.py` alone and partially again in `agent_loop.py`.~~ Re-audited after the tool-call fix: it's now **two** sites, and they are *intentionally divergent* — `_build_partial_message` (`openai.py:660`) parses leniently via `parse_streaming_json` (best-effort display, `{}` until enough arrives) while `_build_final_message` (`openai.py:691`) parses strictly via `parse_json_with_repair` and **raises** on a complete-but-unparseable payload (the authoritative path). The done-event yield loop (`:917`) and `agent_loop.py` (`:496`) no longer parse — they consume already-built `ToolCall` blocks. The only shared lines are `"".join(parts)` and the `ToolCall(...)` construction; a `_finalize_tool_call_args(parts, *, strict)` helper would just re-encode the deliberate lenient-vs-strict split it was meant to remove. Not worth it — left as-is.

### 8. Redundant accumulation in the agent loop — LOW
`agent_loop.py:479-515` re-derives tool-call arguments from each raw `ToolCallDeltaEvent.delta` instead of reading the already-accumulated value off `event.partial` (the provider-built partial `AssistantMessage`). The provider already owns accumulation; the loop should consume its result, not redo it (incorrectly).

### 9. Stray duplicate package dir — FIXED ✓
~~Both `tau-coding-agent/` (the real `src/`-layout package) and a top-level `tau_coding_agent/` (containing `widgets/chat_display.py`) exist.~~ Confirmed dead (the only `ChatMessageData` definition, no importers) and removed via `git rm tau_coding_agent/widgets/chat_display.py`. The duplicate import root is gone.

### 10. Fragile prompt/context de-duplication — FIXED ✓ (2026-06-20)
~~`agent_loop.py:143-183` compares the incoming prompt against the last context message by stringifying and `.strip()`-comparing text blocks to skip a "duplicate."~~ Re-audit found three concrete problems, not just brittleness: (a) a **latent `UnboundLocalError`** — `prev_text` was bound only inside `if last_role == "user":` but referenced unconditionally at the compare, so any context ending in a non-user message would crash `run()` (unreachable only because the sole caller guaranteed a user tail); (b) **load-bearing redundancy** — `AgentSession.prompt()` deliberately inserted the user message into *both* the context and `prompts=[user_msg]`, relying on this strip-compare to collapse it, duplicating the session-layer check at `agent_session.py`; (c) **multimodal-blind** — only text blocks were compared, so a prompt with the same text but a different image was silently dropped (a Fail-Early violation). **Fix:** restore pi parity — `runAgentLoop` concatenates `context + prompts` with no dedup (`agent-loop.ts:103-106`). The loop-level dedup is removed (`run()` now does `messages = [*context, *prompts]`); `prompt()` threads the user message exactly once via `prompts` and drops any duplicate the caller supplied, using an explicit `_ends_with_user_text(messages, text)` helper that names the invariant. Suite 1360/0; mypy −1 error.

### 11. Streaming tests never feed `aiter_lines` — FIXED ✓
~~The provider reads SSE via `response.aiter_lines()`, but the `MagicMock` responses in `test_subphase3.py` and `test_openai_provider.py` only set `.text` and never implement `aiter_lines`~~, so the SSE parser was never exercised: the stream produced a `DoneEvent` with `final=None` and ~27 tests failed on `'NoneType' object has no attribute 'content'` regardless of the parsing logic. Fixed by adding a small `_attach_aiter_lines(response)` helper in both files (mirroring `tau-ai/tests/test_tool_call_streaming_fix.py`) that async-yields the SSE body's lines, wired into every status-200 response mock (the error makers don't read the stream). All **27 now pass and genuinely exercise the parser** — no real bug was hiding behind the mock gap. The remaining 24 `test_agent_session.py` / `test_phase6_subphase3_errors.py` failures are the *separate*, higher-level session/loop-wiring issue (still in progress), out of scope here.

---

## Cross-cutting suggestions

- **Add a streaming-fragment regression test.** A faux provider that emits arguments across several chunks (like pi's `faux-provider`) would have caught #1/#3 immediately. The existing `tau-ai/tests/test_openai_provider.py` evidently uses single-chunk fixtures. pi keeps such cases under dedicated provider tests — mirror that.
- **Parse the tool-call contract in one place.** Most findings (#1, #2, #4, #7, #8) stem from argument parsing being re-implemented at every layer. Centralizing it in the provider — fragments in, validated `dict` out, raise on genuine failure — collapses the surface area.
- **Honor the spec citations.** Files reference `PHASE-*` docs; when behavior is ported from pi, cite the pi file/line too, so future divergences are auditable against the source of truth.
