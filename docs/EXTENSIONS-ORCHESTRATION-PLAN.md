# Extensions beeline — orchestration & context-navigation as extensions

> **Status: PROPOSAL (2026-07-03); §4 session substrate DECIDED
> (maintainer, 2026-07-03).** Evaluation of the codebase against
> `docs/pi_orchestration_patterns.md` and
> `docs/pi_planning_implementing_evaluating.md`, and a plan to beeline through
> the extension-API work (ROADMAP Tier 11) that lets those patterns ship as
> **demo extensions**, not core capabilities. pi remains the source of truth
> for the API *shape*; τ deliberately diverges on *who may trigger*
> orchestration (see §2) and on persisting the tree cursor (see §4.3).
> Evidence cited as `file:line` against the current tree and the local pi
> checkout (`~/Development/pi`).

## 1. What the research established

**pi ships no subagents — and neither will τ.** In pi, subagents exist only as
an example extension (`packages/coding-agent/examples/extensions/subagent/`,
~1000 lines) built entirely on public API: `registerTool` + spawning `pi`
subprocesses, with agents defined as Markdown files. What pi puts in *core* is
the enabling surface:

- **Return-value-driven mutating hooks** (`core/extensions/types.ts:985-1115`):
  `tool_call` → `{block, reason}` (+ in-place arg mutation), `tool_result` →
  patched result, `before_agent_start` → `{systemPrompt, message}`, `context` →
  `{messages}` (rewrite the message list before each LLM call),
  `session_before_compact` → cancel or supply a custom `CompactionResult`.
  Handlers chain in load order; there is no imperative `veto()`/`setSystemPrompt()`.
- **Session navigation on the command context** (`types.ts:339-373`):
  `fork(entryId)`, `newSession()`, `navigateTree()`, `switchSession()`,
  `compact()` — powers `/fork`, `/tree` etc. Available to *commands*
  (user-triggered), **not** to the model as tools.
- **Hermetic spawn flags** (`cli/args.ts`): `--no-extensions`, `--no-session`,
  `--tools`, `--mode json` — what makes a subprocess subagent scoped and
  observable.

τ's current state (verified 2026-07-03):

| Capability the patterns need | τ today |
|---|---|
| Extensions load & connect | Two contradictory loaders (`sdk.py:186-202` wants `extend(api)`; `extensions/loader.py:114` wants `register`); `AgentSession._make_extension_api()` (`agent_session.py:533-540`) hands each extension a **bare API** — fresh `EventBus`, orphan registry, no session ref. Extensions are inert. TUI loads none. No `--extension` flag. |
| Registered tools reach the loop | Never read back — `AgentLoop` is built from the constructor `tools` list only. Write-only registry (`extensions/registry.py:51-121`). |
| `tool_call` veto / arg patch | Absent. `_prepare_tool_call` (`agent_loop.py:784`) is validation-only; `_apply_after_hooks` (`agent_loop.py:888`) is an explicit no-op stub. |
| Message / reminder injection | Absent. `send_user_message`/`_queue_message` referenced by `ExtensionAPI` don't exist on `AgentSession` (all `hasattr` guards fail). No `context`/`before_agent_start` seam; system prompt prepended once in `_stream_response` (`agent_loop.py:373-386`). |
| Events carry information back | `EventBus` is fire-and-forget; handler return values are discarded (`events.py:169`). 10 event types vs pi's ~30. |
| Fork / branch / summarize | **Exists twice.** Rich tree ops in agent-core `SessionManager` — `fork` (`session_manager.py:386`), `clone` (`:458`), `navigate` (`:494`), `summarize_branch` (`:742`), compaction splice (`apply_compaction`, `:223`) — but that store is vestigial on the live path. The live store, `session_store.Session`, has flat `fork` + a lifecycle-event seam with no consumer (`session_store.py:37-67`) and reserved `branch_summary`/`custom` kinds. |
| Hermetic spawn | `--tools`/`--no-tools` exist (`cli.py:76`); `--no-session`, `--no-extensions`, `--exclude-tools` don't. Seam for `--no-session` is landed (`Session.create_in_memory`). |
| Usage / cost | Real token usage flows to `message_end` (`agent_loop.py:494-501`) and headless json's final `done` event (`headless.py:265-278`). No `$` anywhere; `get_context_usage()` returns hardcoded zeros (`extension_types.py:161`). |
| RPC fork/clone | Advertised strings with no handler (`rpc.py:394-397` vs `:297-303`). |

## 2. The philosophy: where τ diverges from pi

pi's two-plane doctrine (`pi_orchestration_patterns.md`): orchestration lives
*outside* the agent in a Python driver; the running agent gets only thin shims.
τ **keeps** the halves of that doctrine that earn their keep and drops the rest:

- **Kept — subagents are subprocesses.** A delegate is still a
  `tau -p --mode json` child with its own `--model`/`--tools`/`--no-session`.
  Isolation, metering, and kill-ability are properties of the process boundary;
  no in-process "nested loop" subagents.
- **Kept — nothing is baked into core.** Every pattern below ships as an
  extension in `examples/`; core grows only *API*.
- **Diverged — the agent may drive its own orchestration.** pi reserves
  fork/navigate/compact for user-triggered commands. τ will let extensions
  expose them **as agent tools** (fork-and-explore, summarize-my-history,
  compact-and-continue, delegate). Rationale: τ's stated goal is agents with a
  strong capacity to navigate their own context and history; the safety story
  is not "the model can't", it's "the veto hook, budget guard, and process
  boundary say no" — capability gated by policy, per the harness's own
  feedback-control doc (`pi_planning_implementing_evaluating.md`).

## 3. Target extension API (the feature set)

The minimal core surface that makes all five demo extensions (§5) writable.
Verb: **`register(api)`** (per ROADMAP M0 and `docs/extensions.md`); pi-style
factory semantics otherwise (sync or async, awaited before first turn).

### 3.1 Connected ExtensionAPI (fixes the wiring fault)

One `ExtensionAPI` per `AgentSession`, bound to the session's real `EventBus`,
a session-owned registry, and the session itself. Registered tools become live
`AgentTool`s in every subsequently-built `AgentLoop` (constructor `tools` +
active extension tools, resolved per `prompt()` since τ builds a fresh loop per
turn — `agent_session.py:234`, which conveniently makes runtime registration
"live immediately", matching pi).

### 3.2 Mutating hooks (return-value driven, pi semantics)

Chained in load order; later handlers see earlier mutations; a raised exception
in `tool_call` blocks (fail-safe). The subset that orchestration needs — not
all ~30 pi events yet:

| Hook | Fires | Handler may return |
|---|---|---|
| `tool_call` | after arg validation in `_prepare_tool_call`, before execution | `{"block": True, "reason": str}`; may mutate `event.input` in place |
| `tool_result` | in `_apply_after_hooks` | `{"content"?, "details"?, "is_error"?}` partial patch |
| `before_agent_start` | once per `prompt()`, before the first LLM call | `{"system_prompt"?, "message"?}` (system-prompt chained; message injected into context) |
| `context` | before **every** LLM call, on a deep copy of messages | `{"messages"?}` — the system-reminder seam |
| existing 10 `AgentEvent`s | as today | nothing (notify-only), but handlers are awaited with real payloads |

`session_before_compact` → cancel/replace comes for free once the
`session_store` lifecycle seam (seam 3) is routed onto the same bus.

### 3.3 Session-control surface (the divergence, delivered as API)

On `ExtensionContext` (event handlers) and a command-context superset later:

- `compact(custom_instructions=None)` — the real pipeline
  (`AgentSession.compact`, `agent_session.py:373`), plus a
  **turn-boundary-deferred** variant so an agent *tool* can request compaction
  mid-turn without reentering the loop.
- `fork(entry_id=None)` / `entries()` / `summarize_branch(...)` — exposed as
  `ConversationTree` operations (§4): append a summary/`navigate` node + move
  the cursor. `summarize_branch`'s truncated-raw-text
  fallback (`session_manager.py:~730-734`) is refactored to
  `complete_simple` + raise while being exposed (the ROADMAP Tier-5 loose
  thread — don't hand extensions an API with a known Fail-Early violation).
- `send_user_message(content, deliver_as="followUp"|"nextTurn")` — minimal
  injection queue on `AgentSession` (the currently-fictional methods made
  real). Full mid-stream `steer` is deferred; reminders don't need it
  (`context` covers them) and delegation doesn't either.
- `get_context_usage()` returning real numbers (compaction's
  `estimate_context_tokens` already computes them).

### 3.4 Hermetic-spawn CLI flags

`--extension/-e` (repeatable) + `--no-extensions` (Tier 11 M0),
`--no-session` (Tier 7 seam, landed), `--exclude-tools/-xt` (Tier 6 quick-win).
Together with the existing `--tools`/`--no-tools`/`--model`/`--thinking`, this
is the full capability-scoping vocabulary the orchestration doc's `run_pi`
needs — pointed at `tau` instead of `pi`.

**Trust constraint honored (ROADMAP Tier 8):** discovery loads **global**
(`~/.tau/extensions/`) + **explicit `-e`** only. Project-local
`<cwd>/.tau/extensions/` stays OFF until the trust gate ships. (pi gates
project extensions on trust the same way — `loader.ts:557-605`.)

### 3.5 Explicitly deferred

TUI command registry + UI affordances (`registerShortcut`, message renderers,
`ctx.ui` beyond the existing headless no-ops) → Tier 11 M3 in ROADMAP order.
`registerProvider` → M4. Package manager → M5. pi-faithful `--mode json` →
Tier 9 (the delegate demo parses today's `kind` schema behind one thin
function, so the Tier-9 swap touches one place). Cost-in-dollars → §6.

## 4. The session substrate: tree-as-truth (DECIDED 2026-07-03)

> **Concrete build:** the E3 slice below (persist the tree, TUI tree-browser
> with three-mode subtree compaction, and the external-store seam) is spec'd
> step-by-step in `docs/SESSION-TREE-IMPLEMENTATION.md`.

A fork/summarize tool must operate on the store that is *authoritative*, and
today that's split: agent-core `SessionManager` owns context assembly + tree
surgery; `session_store.Session` owns live persistence (TUI + headless), with
the TUI additionally keeping its own working `messages` list and `TauBackend`
constructing a throwaway `SessionManager` (`backends.py:85,127`).

**Decision (maintainer, 2026-07-03): the branching, summarizable conversation
*tree* is the genuine data structure; the linear message list is a view of
it. The persisted artifact is an append-only log of tree nodes — including
the cursor (target location) — and everything else is a read-time fold.**
pi already embodies most of this: `buildSessionContext(entries, leafId)`
walks leaf→root and *interprets* compaction and `branch_summary` entries as
splices during the walk (`session-manager.ts:320-411`); compaction is a plain
appended entry (`appendCompaction`, `:990`); branching is just moving a leaf
pointer so the next append's `parentId` forms the branch (`branch()`,
`:1241-1245`). Nothing is ever re-parented or rewritten.

### 4.1 The three objects

| Object | Role | Provenance |
|---|---|---|
| **`SessionLog`** | Persistence only: append-only JSONL writer/reader + header. Knows nothing about trees. **Never rewrites.** | `session_store.Session`'s file discipline, kept (flush-per-line, cwd partitioning, lifecycle-event seam). |
| **`ConversationTree`** | The genuine structure, pure and I/O-free: `by_id`/children index + the **cursor** (leaf pointer); `append(entry)` (parent = cursor), `navigate(id)`, `path(leaf)`, and the interpretive fold `context_for(leaf)` implementing the splice rules. **Invariant: every structural change is an appended entry — there is no other mutation.** | `SessionManager`'s algebra (`_build_active_path`, `session_manager.py:544`; `_extract_branch_messages`, `:627`) extracted and made side-effect-free over `Session.entries()`. |
| **Views** | `context_view` (what the LLM sees: path + splices) and `transcript_view` (what the message-log widget renders: same path, annotated with branch points). Derived, never persisted. | Replaces the three materialized copies: `SessionManager` active path → `AgentSession.messages`, the TUI's hand-maintained `self.messages`, and the `session_store` linear fold. |

Under this model, fork is not an operation — it's what happens when you
append after navigating to an interior node. And **compaction and
branch-summarization become the same operation** ("replace a subpath with a
summary node at read time"), differing only in entry kind: one splice
mechanism, two kinds. The §5 context-surgeon tools all reduce to *append a
node + move the cursor* — exactly the small, safe API to hand an agent.

### 4.2 Correction toward pi: append-only compaction

τ's `apply_compaction` (`session_manager.py:223-302`) physically re-parents
`first_kept` onto the compaction entry and **rewrites the whole file**
(`_persist_entries` opens `"w"`); its own docstring concedes the tension.
This was τ's divergence, not pi's design — pi appends the compaction entry
and splices at read time. E3 replaces the rewrite with pi's semantics:
appended `compaction` entry + read-time splice in `context_for`. This
restores the append-only invariant, crash-safety (a torn rewrite can destroy
the session), and keeps the pre-compaction history *addressable* — navigating
behind the boundary and continuing ("un-compact and explore") comes for free.

### 4.3 Deliberate divergence from pi: persist the cursor

pi's `leafId` is in-memory only; on load, context assembly falls back to the
**last entry in the file** (`session-manager.ts:348,768`), so a pure
`branch()` with no subsequent append evaporates on quit. For agents that
navigate their own history, the cursor is first-class state. τ adds a tiny
**`navigate` entry kind** — `{"type": "navigate", "target_id": <id|null>}` —
appended whenever the tip moves without new content; on load, cursor =
resolved from the last entry (a `navigate` entry points at its target,
`null` meaning before-first-entry; any other entry points at itself).
Latest-wins, matching how `model_change`/`session_info` already behave.
Navigation itself becomes part of the historical record. Compatibility is
asymmetric and accepted: τ reading a pi file (no `navigate` entries → cursor
= last entry) behaves identically to pi; pi reading a τ file would hit an
unknown entry kind — flagged, not blocking.

### 4.4 Fork across files, demoted

In-place branching (navigate + append) is the structural fork. New-file fork
— `session_store.Session.fork`'s copy (`session_store.py:~300`) and pi's
`createBranchedSession` (`session-manager.ts:1286`) — is retained but
reframed as **exporting a materialized path as a new pickable session**. The
picker lists sessions; branches live inside one.

### 4.5 Endgame for the old objects

`SessionManager`'s persistence retires; its algebra becomes
`ConversationTree`; `AgentSession` reads and appends through the same objects
the TUI/headless persist through — one write path, and the extension API's
`fork`/`entries` mean the same thing everywhere. The TUI's `self.messages`
copy dies last (view-discipline refactor, stageable after E3 — see §7).
Alternative (rejected): teach `SessionManager` to read/write `session_store`
files — two classes for one format, perpetuating the double-write.

## 5. Demo extensions (the payoff, in `examples/`)

Each is a runnable demo + a smoke test; together they exercise every §3 API.

1. **`20_delegate.py` — subagent tool.** `delegate` tool spawning
   `tau -p --mode json --no-session --no-extensions` children; modes single /
   parallel-N / chain (pi's example shapes); per-child `model`, `tools`
   allowlist, wall/turn/`stop_reason` limits and stuck-detection from
   `pi_orchestration_patterns.md §2`; streams progress via `on_update`; rolls
   up usage into `details`. **Parallel delegates must be read-only-scoped**
   (the doc's warning #4: never parallelize writers).
2. **`21_reminders.py` — feedback-control steering.** Port of the
   `pi_planning_implementing_evaluating.md §2` bank: tests-readonly,
   root-cause-after-2-failures, scope-guard, no-new-deps — state tracked via
   `tool_call`/`tool_result` events, correction injected via the `context`
   hook as `<system-reminder>` text with cooldowns.
3. **`22_gatekeeper.py` — hard interlock.** `tool_call` veto: path protection
   (deny writes outside `.tau/scope.txt` prefixes, deny `tests_heldout/`
   reads), the enforcement the reminders only ask for.
4. **`23_context_surgeon.py` — self-navigation tools.** Agent-facing
   `compact_now` (turn-boundary deferred), `summarize_history(from_entry)`,
   `fork_session(entry_id)` → returns the forked file path +
   (optionally) spawns a delegate from the fork — fork-and-explore without
   losing the mainline.
5. **`24_budget.py` — in-agent budget guard.** Accumulates `message_end`
   usage; past threshold injects a warning via `context`, then
   `ctx.abort()` — the orchestration doc's budget-guard sketch, made real.

A `docs/` walkthrough shows the composed system: gatekeeper + reminders +
budget wrapping a delegate-driven plan→implement→evaluate run — the §5
pipeline of the planning doc, with τ as both orchestrator and worker.

## 6. Cost tracking (small, optional, Fail-Early)

Token usage is real end-to-end; dollars are not. Proposal: an **optional**
`cost: {input, output, cache_read, cache_write}` (USD/M tokens) per model in
`~/.tau/config.json`. When present, `done`/`agent_end` usage includes
`cost_usd`; when absent, tokens only — **no fabricated prices, no bundled
price registry** (Fail-Early; mirrors the ROADMAP's `--list-models` stance).
This is what puts local models on the same axis as API ones for the ledger in
`pi_orchestration_patterns.md §4`. The ledger itself stays in the demo layer.

## 7. Phased plan and ROADMAP reconciliation

The beeline = **Tier 11 M0→M2 pulled forward**, plus three S-sized flag items
cherry-picked from Tiers 6/7, plus the store unification, with M3–M5 and
Tiers 8/9/10 left in ROADMAP order. No tier is skipped; two are partially
front-run and say so.

| Phase | Size | Contents | ROADMAP mapping |
|---|---|---|---|
| **E0 — loader + flags** | S | One loader (`register(api)`), importlib + entry points, Fail-Early on load errors; `--extension/-e`, `--no-extensions`; discovery = global + explicit only (trust-gated project dir deferred); `--exclude-tools`; `--no-session` | Tier 11 M0 · Tier 6 item 2 · Tier 7 item 3 |
| **E1 — connect the API** | M | Session-bound `ExtensionAPI` (real bus/registry/session); registered tools live in the loop; awaited handlers with real payloads; real `get_context_usage()`; delete `extensions/events.py` stub | Tier 11 M1 |
| **E2 — mutating hooks** | M | `tool_call` veto/patch in `_prepare_tool_call`; `tool_result` in `_apply_after_hooks`; `before_agent_start`; `context`; chain semantics + tests | Tier 11 M2 (scoped subset) |
| **E3 — tree-as-truth substrate + session control** | M/L | *(full spec: `docs/SESSION-TREE-IMPLEMENTATION.md`)* §4: extract `ConversationTree` over `Session.entries()` (pure; splice fold ported from `_build_active_path`); `navigate` entry kind (§4.3); append-only compaction replacing the `apply_compaction` rewrite (§4.2); retire `SessionManager` persistence; then expose `compact`/`fork`/`entries`/`summarize_branch` (refactored to raise) on `ExtensionContext`; turn-boundary deferral; minimal `send_user_message` queue; route seam-3 lifecycle events onto the extension bus | Tier 11 M3 (session-lifecycle half) · reworks Tier 5's landed `apply_compaction` (structural half only — the summarization engine is untouched) · Tier 5 loose thread · seam 2+3 consumer |
| **E4 — demos + cost** | M (5×S) | Extensions 20–24, walkthrough doc, optional per-model `cost` config | the point of the exercise |
| **later (unchanged order)** | — | TUI view-discipline refactor (`self.messages` → subscribed `transcript_view`, §4.5 — natural companion to session-sprint Phase B/C picker & command work), TUI command registry & UI registration (M3 UI half, seam 4), `registerProvider` (M4), packages (M5), trust-gated project extensions (Tier 8), pi-faithful json swap in the delegate parser (Tier 9) | session sprint Phases B/C · ROADMAP as written |

Dependencies: E0→E1→E2 strictly ordered; E3's substrate work is independent of
E0–E2 and can start in parallel (it touches `session_store`/`session_manager`,
not the extension plumbing); E3's `ExtensionContext` surface needs both E1 and
the substrate; E4 items land incrementally (budget needs only E1; gatekeeper
needs E2; context-surgeon needs E3; delegate needs E0 flags + E1).

**ROADMAP fit of the §4 substrate specifically:** it is the concrete design
for what ROADMAP already gestures at — SESSION-UX-REDESIGN §5.3 kept
`parentId` on every entry and reserved `branch_summary`/`custom` kinds
precisely so "in-session branching becomes a UI feature, not a format
migration"; seam 2 (`entries()`/`header`) becomes `ConversationTree`'s input;
seam 3's lifecycle events get their first consumer. It **reworks one landed
piece**: Tier 5's `apply_compaction` file-rewrite is replaced by pi-parity
read-time splicing (§4.2) — the compaction *engine* (prompts, token
estimation, `complete_simple`) is untouched. The `navigate` entry kind is a
format addition (version stays 1; unknown-kind tolerance already exists at
the picker edge). Tier 9 (`--export`, pi-faithful json) reads the same
`entries()` and is unaffected. Session-sprint Phases B/C proceed as designed;
the TUI's working-list retirement rides with them, after E3.

**Testing.** E1/E2 via `fake_llm` through the full loop (registered fake tools,
veto/patch assertions, injected-context assertions on the wire payload). E3
store surgery gets property-style tests over entry trees (fork/compact/path
rebuild). Delegate demo smoke-tested by spawning `tau -p` against the fake
provider in-repo; the local-llm rig remains the aggressive-streaming
integration check. Gate (ruff/mypy/pre-commit) green per commit, as usual.

## 8. Maintainer decisions

1. **Session substrate** — **RESOLVED 2026-07-03**: tree-as-truth (§4).
   Tree ops move onto `session_store` entries via `ConversationTree`;
   `SessionManager` persistence retires; compaction goes append-only (§4.2);
   the cursor is persisted via a `navigate` entry kind (§4.3, deliberate
   pi divergence); new-file fork demoted to path export (§4.4).
2. **Agent-triggered fork/compact/delegate** (§2 divergence) — this plan
   assumes yes (it is the stated intent); recording it here makes it auditable.
3. **Turn-boundary deferral** for mid-turn self-ops (compact/fork requested by
   a tool apply at `turn_end`) — recommended over loop reentrancy. *(Open.)*
4. **Optional per-model `cost` config** (§6) — tokens-only otherwise. *(Open.)*
5. **Steering scope** — ship `followUp`/`nextTurn` only; defer mid-stream
   `steer` until a demo actually needs it. *(Open.)*
