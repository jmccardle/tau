# τ Roadmap

Living schedule of open work. Each item cites the evidence (file:line, doc, or
test) it came from so it can be audited against the source of truth (pi) and the
"Fail Early" rule.

**State (2026-06-23):** branch `master`. Suite **1397 passed / 0 failed**.
Static checks: **ruff clean** (0 issues), **mypy 0** (was 55; the Tier-5 gate is
green and now enforced by a blocking pre-commit hook — commits `5fd4c4f`,
`ac6236c`). The phase-build (`docs/PHASE-*`) and the post-build bug/quality
backlog (former Tiers 1–4, summarized below) are **complete**. Forward work is
Tiers 5–12, sequenced around the committed **`docs/SESSION-UX-REDESIGN.md`**
sprint — whose **Phase A (storage layer) is now landed** (see below). Scope/
complexity for Tiers 6–12 was established by a five-agent research pass
(2026-06-22); each tier cites the pi parity targets it rests on.

---

## Shipped (compressed — former Tiers 1–4)

- **API key (Tier 1):** no fabricated `sk-fake-…` default; key threaded
  end-to-end (`AgentLoopConfig.api_key` → provider), raises
  `No API key for provider: …` when absent. `fake_llm` fixture patches the
  network boundary so the full loop still runs in tests.
- **Loop/prompt quality (Tier 2/3):** restored pi-parity prompt threading
  (`runAgentLoop` concatenates `context + prompts`); removed the fragile,
  multimodal-blind, crash-prone loop-level dedup (`_ends_with_user_text` helper).
  Tool-call join/parse collapsed to two intentionally-divergent sites (WONTFIX).
- **Thinking (Tier 3 #4):** full `reasoning_effort` send-path —
  `Model.reasoning`/`thinking_level_map`, `tau_ai/models.py` (`clampThinkingLevel`),
  `openai.py` emits `reasoning_effort` (clamped, gated on `Model.reasoning`);
  `--thinking {off…xhigh}` + `--model x:high`. *Caveat:* on the local llama.cpp
  rig `reasoning_effort` is a silent no-op (tests assert the wire payload; the
  server ignores it — the real local toggle is `chat_template_kwargs.enable_thinking`).
- **Headless session continuation (Tier 3 #5):** `--continue`/`-c`,
  `--session REF`, `--fork REF`, `--name`/`-n` over the **`Chat` store**;
  `--resume`/`-r` deferred (interactive-only). **Superseded by the session
  sprint**, which moves all of this onto the new JSONL `Session` store.
- **Docs/cleanup (Tier 4):** `COMMAND_LINE.md` corrected (11 fixes); border-title
  message label kept; large-message render reviewed (no action).

**Durable caveat (not a task):** chats written before the thinking-consolidation
fix keep hundreds of blocks/message on disk; they render fine via the reload
normalizer but are not rewritten (Fail-Early: don't silently rewrite saved files).
The session sprint abandons `~/.tau/chats` entirely (no migration), so this
retires itself.

---

## The path forward (Tiers 5–12)

### Session UX sprint — Phase A: storage layer (DONE) — *landed 2026-06-23*

The append-only JSONL session store (`docs/SESSION-UX-REDESIGN.md` §5/§9 Phase A)
replaced the chat-web `Chat` blob. Landed interface (`session_store.py`):

- **`Session`** (wraps one `.jsonl`): `messages`/`model`/`backend`/`name`/`header`
  reconstructed views + raw `entries()` (seam 2); `append_message` /
  `append_model_change` / `append_thinking_change` / `append_session_info` /
  `append_compaction` (append-on-message, flush per line); `create(cwd, model,
  backend, *, system_prompt, name, id=None, base_dir=None)`, `create_in_memory`,
  `load`, `fork` (header `parent` = source id, copies entries, source untouched).
- **`SessionInfo.read(path)`** — streaming picker reader (count / first / last /
  `modified` from last entry); `None` on parse error (skip at the list edge).
- **`session_dir_for_cwd` / `list_sessions(cwd|None) / most_recent`** — cwd
  partitioning via pi's `--<dashed-cwd>--` slug; `base_dir` override (seam 1).
- **Seam 3** lifecycle events (`session_start`/`before_fork`/`before_compact`/
  `shutdown`) via `subscribe_session_events` — emit points baked in, no consumer
  yet (→ Tier 11).
- **Consumers migrated:** `headless.py` (`--continue`/`--session`/`--fork`/`--name`
  now cwd-scoped, id-based selectors; `_persist_session` + the `+1.0s` collision
  hack deleted) and `app.py` (sidebar → `SessionInfo`; TUI keeps a live working
  `self.messages` list + the active `Session` as an append sink; clear starts a
  fresh session; compact stays a runtime context op — the file keeps full
  history, no rewrite). New `test_session_store.py` (15) + rewritten
  `test_headless_resume.py`; suite 1397/0, gate green. **No migration of
  `~/.tau/chats`** (abandoned, decision 1). **Next: Phase B** (picker modal),
  **Phase C** (command unification + sidebar-closed default).

### Tier 5 — Quality gate (DONE) — *shipped 2026-06-22; compaction landed 2026-06-22*

A tracked `.githooks/pre-commit` (`core.hooksPath .githooks`) running **ruff
check + ruff format --check + mypy** over the three `src` trees, hard-gating
commits ("clear debt first, then hard-gate", maintainer 2026-06-22). No new
dependency; Fail-Early (requires the in-repo venv tools, no PATH fallback).

- **ruff: DONE** (commit `5fd4c4f`). 31→0; `[tool.ruff]` in `pyproject.toml`:
  `line-length = 100`, `target-version = "py311"`, exclude `venv`, default lint
  rules; import-sorting (`I`) deferred.
- **mypy: 55 → 0, DONE** (commit `5fd4c4f`, **no blanket `# type: ignore`**).
  Notable fixes: renamed `SessionManager.list()` → `list_sessions()` (it
  shadowed builtin `list` in this module's annotations — 17 of 21 errors);
  updated the stale `Provider` ABC to the real contract (`Model`/`ToolDefinition`
  params + a `StreamEventStream` Protocol return both stream impls satisfy);
  removed a dead, unreachable `resolve_model()` registry branch that called a
  nonexistent `Provider.resolve_model()`.
- **`sdk._build_system_prompt` — KEPT (decision 2026-06-22).** It is *not* dead:
  it's the **only** `AGENTS.md`/`.tau/SYSTEM.md` loader in τ, reached via the
  public `create_agent_session` (+3 tests, `test_agent_session.py:1305-1333`).
  It is off the live TUI/headless path (which take a literal
  `config["system_prompt"]`, default `"You are a helpful assistant."`), so it's
  a **stranded precursor of pi's live-path `resource-loader.ts`** — **Tier 8 is
  its real port, not deletion**. Keep it as the working reference until Tier 8
  supersedes it.
- **Extension-load errors: DONE** (commit `ac6236c`). `_load_extensions_from_dir`
  no longer swallows failures (`except (ImportError, OSError): pass`) — each
  broken extension is logged to stderr and skipped; `_make_ext_factory` raises on
  a missing spec/`extend()` instead of fabricating a silent no-op.
- **Blocking hook: DONE** (commit `5fd4c4f`). Activate per-clone with
  `git config core.hooksPath .githooks`.
- **LLM-backed compaction: DONE** — faithful port of pi's
  `packages/agent/.../compaction/compaction.ts`. The fabricated-summary
  placeholder is gone; `compaction.py` is a full port (Usage-based token
  estimation, structured summarization prompts incl. the iterative `UPDATE`
  prompt, split-turn handling, file-op tracking) operating on τ's active-path
  entry dicts. Supporting changes:
    - `tau-ai`: new `complete_simple(model, context, options)` (port of pi's
      `completeSimple`, stream.ts:67) — the non-streaming primitive the summary
      call uses.
    - new `compaction_utils.py` (port of pi's `utils.ts`):
      `serialize_conversation`, file-op extraction, `format_file_operations`.
    - `SessionManager.apply_compaction` — splices the summary entry at the
      boundary (re-parents `first_kept` onto it) so the compacted prefix drops
      out of the active path; `_build_active_path` now anchors on the **last**
      compaction (pi `buildSessionContext` parity) so iterative compaction
      actually prunes.
    - `AgentSession.compact()` runs the real pipeline (manual `/compact`) and
      `prompt()` auto-compacts after a turn via `should_compact`
      (`compaction_settings`, gated so a window ≤ reserve never trips it).
    - Errors raise `CompactionError` (Pythonic translation of pi's
      `Result<T, CompactionError>`); **no fabricated fallback summary** — Fail-Early.
  Replaced the placeholder-era `compaction.py` API (`CompactionConfig`,
  `compact_session`, `build_compaction_prompt`, …) and rewrote
  `test_phase5_subphase1.py` against the new engine. **Tier 5 is now fully
  closed.**
- **Follow-up (separate concern, NOT compaction):** `session_manager.summarize_branch`
  still falls back to truncated raw text on an LLM error (lines ~730-734) — the
  same anti-pattern compaction just shed. It should be refactored onto
  `complete_simple` + raise. Tracked here so it isn't forgotten.

### Tier 6 — CLI parity quick-wins + json doc-fix — *pre/parallel to the session sprint*

No session-layer dependency; small, over existing plumbing (`cli.py
build_parser`, `headless.py`, `backends.py`).

- `--append-system-prompt` (repeatable; pi `args.ts:95`) — concat after the base
  prompt.
- `--exclude-tools`/`-xt` (denylist; pi `args.ts:125`) — `.filter` over the
  active tool set.
- `--no-builtin-tools`/`-nbt` (pi `args.ts:118`) — ≡ `--no-tools` in τ until an
  extension-tool subsystem exists (Tier 11); document the equivalence, don't fake
  a difference.
- `--list-models [search]` (pi `args.ts:171`) — over the **`config.json` models
  map**, *not* a bundled registry (τ has none; **do not fabricate one** —
  Fail-Early). Reuse `textual.fuzzy.Matcher`.
- **json doc-fix.** The `--mode json` claim in `CLI-PLAN.md §4 #11` /
  `COMMAND_LINE.md:126` is **false**: τ emits the backend's flat `{"kind":…}`
  events (`headless.py` / `backends.py:210-214`), not the `AgentEvent`
  vocabulary. Correct the docs to describe reality now; the actual re-emit to
  pi's schema is Tier 9. **Decision (locked 2026-06-22):** τ will emit **pi's**
  json schema.

### Tier 7 — Post-session CLI flags — *after session Phase A*

Ride the Phase-A seams (below). pi `args.ts:104,108,112`.

- `--session-dir` — threads `base_dir` through the new helpers (seam 1).
- `--session-id` — `Session.create(id=…)` + exact-id lookup in the cwd dir (seam 1).
- `--no-session` — `Session.create_in_memory` ephemeral mode (seam 1).

### Tier 8 — Context files + trust — *security-ordered*

- **Context-file discovery (S/M, low-risk, high-value).** Port pi's
  `loadProjectContextFiles` (`resource-loader.ts:61-117`): candidate set
  `AGENTS.md`/`CLAUDE.md` (±uppercase), global + cwd→root walk, dedupe,
  `<project_context>` / `<project_instructions path=…>` injection
  (`system-prompt.ts:154-161`). Unify onto the live `backends.py`/headless path,
  **superseding** the SDK-only `sdk._build_system_prompt` stub (kept in Tier 5 as
  the working reference — see Tier 5). Fold its `.tau/SYSTEM.md` loading in here
  too (pi `resource-loader.ts:952-966`: project/global `SYSTEM.md` +
  `APPEND_SYSTEM.md`). Add `--no-context-files`/`-nc`.
- **Trust gate (M/L, security-sensitive).** Port pi's `trust.json`
  (`~/.tau/trust.json`, cwd-canonical keys, ancestor inheritance;
  `trust-manager.ts:27-35,42-57`), `resolve_project_trusted`
  (`project-trust.ts:45-95`), `--approve`/`-a`/`--no-approve`/`-na`. UX: a
  **Textual `ModalScreen`** (consistent with the session picker) registered as a
  `trust`/`untrust` **command** in the session registry (seam 4 — one handler,
  three surfaces). The trust store stays **separate** from the session dir
  (different keying: raw abspath + inheritance vs. dashed slug).
- **HARD CONSTRAINT (Fail-Early / security).** Context files are inert text and
  may ship ungated (pi-faithful). But τ **must not** auto-load *executable*
  project-local resources (extensions, `.tau/SYSTEM.md`) before the trust gate
  exists. Trust (this tier) **precedes** any project-local extension/SYSTEM.md
  loading (Tier 11).

### Tier 9 — Export + json reconciliation — *after session Phase A*

- **`--export` HTML (M).** Port pi's `exportFromFile` (`export-html/index.ts:288`)
  onto the new `Session.entries()`/`header` (seam 2); a self-contained
  `template.html`+css+js. τ **owns the template look** (personality); only the
  embedded `SessionData` contract must match pi's exporter.
- **pi-faithful `--mode json` (M).** Re-emit pi's `AgentSessionEvent` schema:
  `type` discriminator (not `kind`), camelCase `toolCallId`/`toolName`, the
  session **header line first** (`print-mode.ts:114-119`). A `tau_event → pi-json`
  serializer behind `--mode json`, sourced from the `AgentEvent` bus
  (`events.py`), not the backend `kind` stream. Finalize the Tier-6 doc.

### Tier 10 — Themes / templates / skills — *after the session command registry*

- **Shared resource loader (M).** Frontmatter parser + `~/.tau/<kind>/` &
  `.tau/<kind>/` discovery + `--no-X`-keeps-explicit-paths. Build once; all three
  reuse it.
- **Themes (S) — Tau's identity divergence; ship early.** Adopt **Textual-native**
  theming (`App.theme`/`register_theme` + a `$variable` refactor of
  `parley.tcss`), **not** pi's 51-slot ANSI-baked JSON (tied to pi's custom
  renderer). The one subsystem where diverging on *format* is correct; offer a
  thin pi-theme import only if demand appears. `--theme`/`--no-themes`.
- **Prompt templates (S/M).** Keep pi's flat-`.md`+frontmatter + `$ARGUMENTS`/`$1`
  substitution (`prompt-templates.ts`) so pi templates port; route `/<name>`
  through the session **command registry** (seam 4) + palette.
- **Skills (M/L).** Match pi/Claude-Code **`SKILL.md`** exactly (ecosystem
  interop; `~/.agents/skills/` cross-harness dir): two-tier progressive disclosure
  (`<available_skills>` gated on `read`), `/skill:name` body inlining. Defer
  `disable-model-invocation`/`allowed-tools` (experimental in pi).

### Tier 11 — Extensions epic — *multi-sprint*

The biggest frontier. τ has a **half-wired skeleton** (`extension_types.py`,
`extensions/{loader,registry}.py`, `sdk._load_extensions`, and
`agent_session.py:104-106` actually invokes factories) but: **no runner**
(registered tools/commands/flags are never read back), loop hooks are no-ops
(`agent_loop.py:898`), **two contradictory loaders** (`sdk.py` calls
`mod.extend(api)` vs `extensions/loader.py` calls `mod.register(api)`), **no CLI
surface** (`--extension` absent), and load errors are silently swallowed
(`sdk.py:226` — Fail-Early).

Milestones: **M0** reconcile the two loaders (→ `register(api)`, importlib +
`importlib.metadata` entry points, Fail-Early on errors) + surface
`--extension`/`-e`, `--no-extensions`/`-ne` → **M1** runner + tool registration
(registered tools become live `AgentTool`s) → **M2** hooks/interceptors (the ~33
events, return-value mutation) → **M3** session-lifecycle (consume the Phase-A
emit seam 3) + Textual UI registration (`registerShortcut`/`registerMessageRenderer`)
→ **M4** `registerProvider` → **M5** package manager (lean on `pip`/entry points;
`list`/`config` over `settings.json` first; defer git/npm fetching).

Faithful: factory shape, single `ExtensionAPI`, event names/semantics, interceptor
pattern, registration verbs, discovery locations. Personality: importlib + entry
points (no jiti), `Protocol` API, Pydantic tool schemas, Textual bindings/widgets.

### Tier 12 — RPC mode — *deferred, narrow audience*

`--mode rpc` (pi `args.ts:80`; pi's `modes/rpc/`: 28 command verbs, LF-only JSONL,
extension-UI round-trips). XL, embedding-only audience → lowest priority. Gate on
the session **command registry** (seam 4) as the dispatch table. *Note:*
tau-agent-core already has a partial `rpc.py` (JSON-RPC 2.0 types + `RPCHandler`
skeleton) — **distinct** from pi's `RpcCommand` protocol; reconcile here.

---

## Cross-cutting: the 4 Phase-A seams (approved 2026-06-22)

`docs/SESSION-UX-REDESIGN.md` Phase A now bakes in four small forward-compat seams
— cheap to add during the rewrite, expensive to retrofit — each unlocking a later
tier with near-zero rework:

1. **Session API parameter slots** — `base_dir: Path|None=None` on
   `session_dir_for_cwd`/`list_sessions`/`most_recent`/`Session.create`/`fork`;
   `id: str|None=None` on `Session.create`; a `Session.create_in_memory` ephemeral
   mode. → **Tier 7** (`--session-dir`/`--session-id`/`--no-session`).
2. **Raw `entries()`/`header` accessor** on `Session` (not just the folded
   `messages`). → **Tier 9** (`--export`, pi-faithful json).
3. **Session-lifecycle event emission** —
   `Session.create/load/fork/append_compaction` emit
   `session_start`/`before_fork`/`before_compact`/`shutdown` (no consumer yet).
   → **Tier 11** (extension hooks, no loop retrofit).
4. **Generic/dynamic command registry** — register at runtime (not a fixed
   `resume/new/fork` enum); one slash-parser with "unknown `/x` → pass through";
   a "register palette entries from a list" seam. → **Tier 10** (templates/themes),
   **Tier 8** (trust commands), **Tier 12** (rpc dispatch).

---

## Suggested order

Tier 6 + the session sprint (with the 4 seams) in parallel → Tier 7 → Tier 8 →
Tier 9 → Tier 10 → Tier 11 (epic) → Tier 12. Tier 5 is fully closed (mypy gate +
LLM-backed compaction); the only loose thread it leaves is the `summarize_branch`
Fail-Early follow-up noted under Tier 5, which can fill any gap.
