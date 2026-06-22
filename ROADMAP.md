# τ Roadmap

Living schedule of open work. Each item cites the evidence (file:line, doc, or
test) it came from so it can be audited against the source of truth (pi) and the
"Fail Early" rule.

**State (2026-06-22):** branch `master`. Suite **1403 passed / 0 failed**.
Static checks: **ruff clean** (0 issues), **mypy 55** (down from 57; the Tier-5
gate-blocker). The phase-build (`docs/PHASE-*`) and the post-build bug/quality
backlog (former Tiers 1–4, summarized below) are **complete**. Forward work is
Tiers 5–12, sequenced around the committed **`docs/SESSION-UX-REDESIGN.md`**
sprint. Scope/complexity for Tiers 6–12 was established by a five-agent research
pass (2026-06-22); each tier cites the pi parity targets it rests on.

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

### Tier 5 — Quality gate (IN PROGRESS) — *now*

A no-new-dependency `.git/hooks/pre-commit` running **ruff check + ruff format
+ mypy** over the three `src` trees, hard-gating once green ("clear debt first,
then hard-gate", maintainer 2026-06-22).

- **ruff: DONE.** 31→0 (22 auto-fixed, 24 files reformatted, 8 hand-fixed).
  `[tool.ruff]` in `pyproject.toml`: `line-length = 100`, `target-version =
  "py311"`, default lint rules; import-sorting (`I`) deferred. Fixing the
  `rpc.py` forward-refs (`TYPE_CHECKING`) also cleared 2 mypy `name-defined`
  errors. **Held uncommitted** to land with mypy as one lint/format overhaul
  commit (incl. the hook).
- **mypy: 55 → 0 (remaining; the blocker).** Histogram: `attr-defined` 12,
  `no-any-return` 11, `union-attr` 8, `valid-type` 6, `arg-type` 6, `assignment`
  4, `override` 3, then `return-value`/`index`/`misc`. **Fail-Early: no blanket
  `# type: ignore`** — fix the types.
- **During the mypy pass — remove dead code:** `sdk.py:_build_system_prompt`
  reads `AGENTS.md`/`.tau/SYSTEM.md` but is **off the live path**
  (`backends.py:64,105-112` builds the system prompt from `config` only), so τ
  injects no context files today. Delete it (its real replacement is Tier 8);
  also stop silently swallowing extension-load errors (`sdk.py:226 except: pass`).
- **Then install the blocking hook** (ruff check + `ruff format --check` + mypy).
- **New item — LLM-backed compaction.** `compaction.py:151-153` is a marked
  placeholder that builds the compaction prompt, discards it, and **fabricates**
  `summary = config.system_prompt + " - Compacted N entries"` — a standing
  Fail-Early violation, currently `# noqa: F841` with a pointer. pi's compaction
  is the reference. Address alongside or shortly after the gate.

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
  (`system-prompt.ts:154-161`). Unify onto the live `backends.py` path (the dead
  `sdk._build_system_prompt` was removed in Tier 5). Add `--no-context-files`/`-nc`.
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

Tier 5 (now) → Tier 6 + the session sprint (with the 4 seams) in parallel →
Tier 7 → Tier 8 → Tier 9 → Tier 10 → Tier 11 (epic) → Tier 12. Tier 5's mypy
cleanup and the dead-code / `compaction` items are independent of everything else
and can fill any gap.
