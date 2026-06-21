# τ Roadmap

Living schedule of open work. Each item cites the evidence (file:line, doc, or
test) it came from so it can be audited against the source of truth (pi) and the
"Fail Early" rule. Phase-build work (the `docs/PHASE-*` plan) is **complete**;
this file tracks the post-build bug/feature backlog.

**State (2026-06-21):** branch `master`. Suite: **1385 passed / 0 failed**
(`pytest` from repo root) after closing Tier 1, Tier 2, and Tier 3 #4. mypy:
**57** errors (unchanged; the reasoning_effort work added no new errors).

Last shipped (commits `4e20240`, `9cb472d`, `83efb1a`, `29869fe`): thinking
consolidation, real usage via `stream_options`, multi-turn
`_assistant_content_to_openai` fix, reasoning round-trip (pi parity), the
`prompt()`/`continue_conversation()` return-only-this-turn duplication fix, TUI
reload renderer, global reasoning/tool toggles, pi-parity prompt threading, and
the `reasoning_effort` send-path + `--thinking` flag (Tier 3 #4).

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

## Tier 2 — Code quality

### 2. Duplicated "join → parse → ToolCall" logic — CLOSED / WONTFIX (`docs/CODE-QUALITY-NOTES.md` #7)
Re-audited 2026-06-20: the tool-call fix already collapsed this from five sites
to **two**, and the two are *intentionally divergent* — `_build_partial_message`
parses leniently (`parse_streaming_json`, best-effort for display) while
`_build_final_message` parses strictly and **raises** (`parse_json_with_repair`
+ dict-check, the authoritative path). The done-event yield loop and
`agent_loop.py` no longer parse at all (they consume already-built `ToolCall`s).
The only shared lines are `"".join(parts)` and the `ToolCall(...)` construction;
a shared helper would have to re-introduce a `strict` flag to thread the
deliberate difference, netting ~nothing. Left as-is.

### 3. Fragile prompt/context de-duplication — ✅ DONE (2026-06-20) (`docs/CODE-QUALITY-NOTES.md` #10)
Re-audit found the loop-level dedup (`agent_loop.py`) was worse than "fragile":
(a) a **latent `UnboundLocalError`** — `prev_text` was bound only when the
context tail was a user message but referenced unconditionally; (b) **load-bearing
redundancy** — `prompt()` deliberately put the user message in *both* the context
and `prompts=[user_msg]`, relying on this strip-compare to collapse it, while the
session layer already did the same compare; (c) **multimodal-blind** — only text
blocks compared, so same-text/different-image prompts were silently dropped.
**Fixed by restoring pi parity:** `runAgentLoop` simply concatenates
`context + prompts` (agent-loop.ts:103-106), so the loop-level dedup is removed
and `run()` now does `messages = [*context, *prompts]`. `prompt()` threads the
user message exactly once (via `prompts`) and drops the duplicate the caller
supplies through an explicit `_ends_with_user_text(messages, text)` helper. Net
mypy errors −1; suite 1360/0.

---

## Tier 3 — Missing features (deferred from `docs/CLI-PLAN.md`, Fail-Early gated)

### 4. `--thinking` — ✅ DONE (2026-06-21)
**Shipped the full `reasoning_effort` send-path + CLI flag:**
- **τ-ai (`types.py`):** `Model` gained `reasoning: bool` (capability) and
  `thinking_level_map` (per-model level→value remap), mirroring pi `Model`
  (`types.ts:585,589`). New `tau_ai/models.py` ports pi's `clampThinkingLevel`/
  `getSupportedThinkingLevels` (`models.ts:51-84`) + `EXTENDED_THINKING_LEVELS`
  (`off..xhigh`) and `is_valid_thinking_level`.
- **τ-ai (`openai.py`):** `stream_chat` reads the τ-internal `reasoning` option,
  clamps it, and emits `payload["reasoning_effort"]` per pi's default "openai"
  `thinkingFormat` branch (`openai-completions.ts:620-628`); gated on
  `Model.reasoning` (Fail-Early: never sent to a non-reasoning model). The
  `reasoning` key is stripped from the body (never leaks).
- **τ-agent-core:** `AgentLoopConfig.reasoning`, `_stream_response` adds
  `options["reasoning"]`, `AgentSession(reasoning=…)` threads it into both
  `prompt()` and `continue_conversation()`, and `create_agent_session` now
  *uses* its (previously inert) `thinking_level` arg (non-"off" → `reasoning=True`
  on the model, level forwarded).
- **τ-coding-agent:** `--thinking {off..xhigh}` flag (argparse `choices`,
  validated), plus the `--model x:high` suffix (`resolve_model_config` parses it
  instead of raising). `TauBackend` derives `Model.reasoning` from a config
  `thinking` level (or explicit `reasoning: true`) and threads the level.
  Honored in both the TUI (`_launch_tui` overrides) and headless paths.
- A non-"off" level marks the model reasoning-capable (pi
  `model-resolver.ts:496`). xhigh needs a `thinking_level_map` entry, else it
  clamps to high (pi parity). Tests: provider send/clamp/gate, level helpers,
  CLI parse/resolve, backend wiring, session threading. Suite 1385/0; mypy 57.

**Test-rig caveat (not a bug — verified 2026-06-21):** against the local
llama.cpp + Qwen3 GGUF server, `reasoning_effort` is a **silent no-op**. τ puts
it on the wire correctly, but llama.cpp accepts it with HTTP 200, never
validates it (even `reasoning_effort: bogus` → 200), and the jinja template has
no reference to it — so it's parsed off the request and dropped before
templating. Deterministic A/B (temp=0, seed=42): output is byte-identical across
`high`/`minimal`/`bogus`/absent (same reasoning SHA). The *only* working thinking
toggle on this rig is `chat_template_kwargs: {enable_thinking: false}` (reasoning
590→0 chars). Implication: `--thinking <level>` against `local-llm` exercises the
full τ send-path (and the unit tests assert the *payload*, so they stay valid)
but has no server-side effect. Graded *or* on/off control of local Qwen would
require porting pi's `"qwen"` thinkingFormat (a `chat_template_kwargs.enable_thinking`
send-path) — out of scope until local thinking control is wanted.

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
2. ~~**Tier 2 #2/#3**~~ — #3 ✅ done (pi-parity dedup removal, 2026-06-20);
   #2 closed as WONTFIX (down to 2 intentionally-divergent sites).
3. ~~**Tier 3 #4**~~ — ✅ done (`reasoning_effort` send-path + `--thinking`,
   2026-06-21).
4. **Tier 3 #5** (headless resume), then Tier 4 cleanup. ← next.
