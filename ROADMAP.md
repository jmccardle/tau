# τ Roadmap

Living schedule of open work. Each item cites the evidence (file:line, doc, or
test) it came from so it can be audited against the source of truth (pi) and the
"Fail Early" rule. Phase-build work (the `docs/PHASE-*` plan) is **complete**;
this file tracks the post-build bug/feature backlog.

**State (2026-06-20):** branch `master`, clean tree. Suite: **1360 passed / 0
failed** (`pytest` from repo root) after closing Tier 1. mypy baseline unchanged
(58 pre-existing errors, none in the Tier 1 changes).

Last shipped (commits `4e20240`, `9cb472d`, `83efb1a`): thinking consolidation,
real usage via `stream_options`, multi-turn `_assistant_content_to_openai` fix,
reasoning round-trip (pi parity), the `prompt()`/`continue_conversation()`
return-only-this-turn duplication fix, TUI reload renderer, and global
reasoning/tool toggles.

---

## Tier 1 — Known bugs

### 1. Fake API-key default → 24 failing tests hit the live network — ✅ DONE (2026-06-20)
**Was:** `openai.py:248` did
`self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "sk-fake-key-for-testing")`,
so a missing key was silently sent to the **real** OpenAI API and every one of
24 tests raised `RuntimeError: HTTP 401: Incorrect API key provided: sk-fake-…`.
This was finding **#5** in `docs/CODE-QUALITY-NOTES.md` and a `CLAUDE.md`
Fail-Early violation.

**Fixed (one root cause, several coordinated changes):**
- **No fabricated key (`tau-ai/providers/openai.py`):** constructor no longer
  defaults to a fake key (`api_key or os.environ.get("OPENAI_API_KEY")`, may be
  None). `stream_chat` resolves `self.api_key or options["api_key"]` and **raises
  `ValueError("No API key for provider: …")`** when none is found (mirrors pi
  `openai-completions.ts:141`). Local servers pass the truthy `"not-needed"`
  sentinel, which satisfies the check.
- **Threaded the real key end-to-end** (it was previously dropped, so configuring
  a real OpenAI key never worked — only local did, because base_url routed
  locally): `AgentLoopConfig.api_key` → `_stream_response` adds it to
  `stream_simple`'s options → `client.py` builds the provider with it →
  `stream_chat` strips it from the request body. `AgentSession` accepts `api_key`;
  `create_agent_session` stopped ignoring its `api_key` arg; `backends.py` passes
  the configured key instead of stashing it in an unused field.
- **Tests:** new `fake_llm` fixture in `tau-agent-core/tests/conftest.py` patches
  the network boundary (`stream_simple`) with a canned reply so the **full agent
  loop still runs** (real events, message assembly) without a network call;
  applied via `@pytest.mark.usefixtures("fake_llm")` to the 7 affected classes.
  New provider tests assert the raise, env-var read, options-key path, and
  `not-needed` sentinel; new session tests assert the key is threaded into
  `stream_simple` options (and that None injects no override). The streaming
  provider tests that mock the transport now construct with `api_key="sk-test"`.

**Result:** suite **1360 passed / 0 failed**; mypy baseline unchanged.

---

## Tier 2 — Code quality (open findings, no behavior change)

### 2. Duplicated "join → parse → ToolCall" logic — DRY (`docs/CODE-QUALITY-NOTES.md` #7)
The ~8-line join/parse/finalize block is still repeated across `openai.py`'s
build methods. Centralize into one `_finalize_tool_call_args(parts) -> dict` so
the parsing contract lives in exactly one place. (#1–#4, #6, #8 of that doc are
already FIXED; this is the remaining parsing-related debt.)

### 3. Fragile prompt/context de-duplication — LOW (`docs/CODE-QUALITY-NOTES.md` #10)
`agent_loop.py:143-183` skips a "duplicate" prompt by stringifying + `.strip()`
-comparing text blocks (whitespace/multimodal-fragile). Related to the
session-layer duplication bug just fixed in `agent_session.py`. Add an explicit
dedupe key or a comment stating the exact invariant it protects.

---

## Tier 3 — Missing features (deferred from `docs/CLI-PLAN.md`, Fail-Early gated)

### 4. `--thinking` — blocked on a `reasoning_effort` send-path in tau-ai
**Verified still blocked:** `grep reasoning_effort tau-ai/src` → not sent
anywhere; `Model` (`tau-ai/src/tau_ai/types.py`) has no reasoning/thinking field
(only the `ThinkingContent` *block* carries `thinking_signature`). The recent
reasoning **round-trip** work replays *captured* reasoning back to the model — it
does **not** add a request-side `reasoning_effort` param, so `--thinking` is
still gated. Work: add a thinking level to `Model` + a send-path in `openai.py`
(pi `reasoning_effort`, levels `off..xhigh`, default `medium`). Until then the
flag must error, not stub (`docs/CLI-PLAN.md` "Deferred").

### 5. Headless session continuation — `--continue`/`-c`, `--resume`, `--session`, `--fork`, `--name`
Sessions already persist to `~/.tau/chats/` and resume **from the TUI**. The
CLI-side flags to resume *headlessly* still need: load-instead-of-`new_session()`
in `TauBackend.__init__` (`backends.py:90`), session→context wiring, and tests.
Confirm `SessionManager.fork()` exists before promising `--fork` (open question
in `docs/CLI-PLAN.md` §3).

---

## Tier 4 — Low priority / cleanup

### 6. Message-label placement — DESIGN NOTE (`docs/TUI-FOLLOWUPS.md` #3)
`MessageBox` uses `border_title` for the role label. Switching to an in-box
first-line header is a localized `MessageBox.compose` + `parley.tcss` change,
no behavioral impact. Only if the visual treatment is preferred.

### 7. Doc hygiene
- **`docs/TUI-FOLLOWUPS.md` removed (2026-06-20)** — it was a session-companion
  doc; all three items are resolved or captured here: item 1 (reload renderer)
  FIXED, item 2 (stray dir) already removed as CODE-QUALITY #9, item 3 (message
  label placement) lives at Tier 4 #6 above.
- **`docs/COMMAND_LINE.md`** still needs the 11 corrections enumerated in
  `docs/CLI-PLAN.md` §4 (invented flags/env vars, short-alias collisions,
  lossy thinking map). Correct, don't delete.

### 9. Large single-message render strategy — DESIGN NOTE (low priority)
A pathologically large *string* assistant message (the old 827 KB
`1781803484.json`) renders correctly but is slow because Markdown parsing scales
with size. If such messages recur in normal use, consider a display-only
lazy/plain-`Static` strategy for oversized content — but never silently truncate
assistant prose (Fail-Early). Carried over from the removed TUI-FOLLOWUPS #1.

### 8. Durable caveat (not a task) — pre-fix chats stay bloated on disk
Chats written before the thinking-consolidation fix keep hundreds of blocks per
message on disk. They render fine via the TUI reload normalizer
(`ChatDisplay.reload_messages`), but the files aren't rewritten. A load-and-resave
normalization was **deliberately not added** (Fail-Early: don't silently rewrite
the user's saved files). Left here so it isn't "rediscovered" as a bug.

---

## Suggested order

1. ~~**Tier 1 #1**~~ — ✅ done (2026-06-20).
2. **Tier 2 #2/#3** (cheap, localizes the parsing/dedup contracts) ← next.
3. **Tier 3 #4** (`reasoning_effort` send-path) — unblocks `--thinking` *and*
   completes the reasoning story the recent round-trip work started.
4. **Tier 3 #5** (headless resume), then Tier 4 cleanup.
