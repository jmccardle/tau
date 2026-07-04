# Extensions chain E0 → E4 — implementation spec

> **Status: PLAN (2026-07-03).** The buildable, per-phase spec for the extension
> beeline of `docs/EXTENSIONS-ORCHESTRATION-PLAN.md` (§3 API, §5 demos, §7
> phasing; open decisions RESOLVED in §8). pi is the source of truth for API
> *shape*; τ diverges on *who may trigger* orchestration (§8 decision 2). Evidence
> is cited `file:line` against the current tree and the pi checkout
> (`~/Development/pi`), gathered by a 6-probe research pass (2026-07-03) whose raw
> packets live in the session scratchpad. Maps onto ROADMAP Tier 11 M0→M3
> (session-lifecycle half) + the Tier 6/7 flag cherry-picks. Companion to the
> landed `docs/SESSION-TREE-IMPLEMENTATION.md` (E3 substrate).

Resolved maintainer decisions this spec builds on (plan §8): **(2)** extensions
may expose fork/compact/navigate/delegate as **model-callable tools** — safety is
the veto hook + budget guard + process boundary, so **E2 is a hard prerequisite
for the agent-facing mutation tools**; **(3)** mid-turn compact/fork **defer to
`turn_end`**; **(4)** ship the **optional per-model `cost` config** (Fail-Early, no
registry); **(5)** `send_user_message` ships `followUp`/`nextTurn` only but the
delivery-mode param stays **extensible** (future `steer` must be additive).

---

## 0. What already exists (build the gap, not the whole thing)

The extension skeleton is scaffolded but **wired to nothing**, and the E3 substrate
already delivered much of what E3-ctx needs. The reconciliation, verified 2026-07-03:

- **The central wiring fault (blocks everything).** `AgentSession._make_extension_api()`
  hands each extension a **bare `ExtensionAPI()`** — no session, no registry, and a
  **fresh orphan `EventBus`**, not the session's real bus (`agent_session.py:533-540,
  109`). Every handler subscribes to a dead bus; every registered tool lands in a
  registry no code path reads. E1 is "bind the API to the real session/bus/registry,"
  not a greenfield build.
- **Two contradictory loaders**, neither matching pi. `sdk.py` requires a named
  `extend(api)` (`sdk.py:187-203`); the **dead, unused** `extensions/loader.py`
  requires a named `register` and returns it *un-called* (`loader.py:114-117`); pi
  uses a default-export factory (`loader.ts:340-384`). `extensions/loader.py` is dead
  code (only re-exported, never on a live path) with a Fail-Early swallow (returns
  `None` on any load error, `loader.py:99-121`).
- **The two E2 seams exist as named stubs.** `_prepare_tool_call` is validation-only
  (`agent_loop.py:784-826`); `_apply_after_hooks` is an explicit no-op whose docstring
  says "extensions will use this in Phase 3" (`agent_loop.py:888-899`). E2 wires *into*
  these, reusing the existing `BlockedCall`/`ErrorCall` dataclasses (`agent_loop.py:51-59`)
  for the block path.
- **The EventBus is notify-only.** `emit` discards handler return values and swallows
  exceptions (`events.py:167-183`); 10 event types vs pi's ~24 (`events.py:46-57`).
  The return-value-driven hook model needs a **separate** return-collecting dispatcher
  (pi keeps `EventBus` and `ExtensionRunner` separate — `types.ts:1347`).
- **E3-ctx is much smaller than the plan assumed — these are DONE:** `summarize_branch`
  is already `complete_simple` + raise (`session_manager.py:705-758`); `AgentSession.compact`
  is already append-only via `ConversationTree.context_entries` + `append_compaction`
  (`agent_session.py:368-522`); the full navigate/branch_summary substrate +
  `TauBackend.navigate_tree` composite exists (`session_store.py:396-434`,
  `backends.py:159-206`); `entries()`/`context_for` cover `ctx.entries()`. E3-ctx is
  now **"expose the landed ops on `ExtensionContext`"** + the injection queue + deferral.
- **Fail-Early debts to clear while here (not new features):** `get_context_usage()`
  returns a hardcoded `{"total_tokens": 0}` (`extension_types.py:161-163`);
  `send_user_message`/`send_message` are `hasattr`-guarded **silent no-ops** against
  methods that don't exist on `AgentSession` (`extension_types.py:305-313`); the shipped
  `send_user_message(deliver_as="steer")` default **violates resolved decision 5**.
- **A cost field already exists, always empty.** `Usage.cost: dict` is defined but never
  populated (`tau-ai/.../types.py:84`); token usage is real end-to-end
  (`openai.py:183-203` → `agent_loop.py:501` → `backends.py:520-531` → `headless.py:279`).
  §6 is "compute into/beside the existing carrier," not "add + thread."

---

## E0 — loader + flags  (size S; ROADMAP M0 + Tier 6/7 flags)

**Goal:** one real loader that discovers, loads, and (E1) connects extensions, plus
the hermetic-spawn CLI vocabulary. No trust gate (Tier 8 — global + explicit only).

### E0.1 One loader, one verb

- **Verb: `register(api)`** (plan §3 fixes it). Delete the dead `extensions/loader.py`
  and the `sdk.py` `extend(api)` thunk path; the single loader imports each module by
  file path (`importlib.util.spec_from_file_location`, already used at `sdk.py:196-200`)
  and calls its module-level `register(api)`. **Keep file-path importlib** (pi-faithful,
  path-based); **do not** add `importlib.metadata` entry_points — pi has no analog and
  it needs installed packages (§7 decision E0-b).
- **Async factories:** `register` may be sync or async; await it before the first turn.
  Current `ext(self._make_extension_api())` at `agent_session.py:125-126` cannot await —
  E0 must (`await ext(api)` when a coroutine is returned). Guard the *invocation* in
  try/except so one throwing factory doesn't abort session construction (pi separates
  load-time from invoke-time errors).
- **Discovery (E0 scope = global + explicit only):** explicit `-e <path>` entries +
  the global dir `~/.tau/extensions`. **Project-local `<cwd>/.tau/extensions` stays OFF**
  until the Tier-8 trust gate (plan §3.4; pi gates it too, `project-trust.ts:45-95`).
  Grammar: a bare `*.py` file, or a package dir (subdir with `__init__.py`); **defer**
  pi's `package.json`-manifest rule (`PiManifest`, `loader.ts:445-463`). Dedupe by
  resolved path, first-wins.
- **Return a struct, not a bare list.** Port pi's `LoadExtensionsResult`
  (`types.ts:1590`): `{extensions: [...], errors: [{path, error}]}` so load diagnostics
  reach the caller/UI (τ drops them today — `sdk.py:152-184`).
- **Error policy (Fail-Early distinction):** a **discovered** extension that fails to
  load is collected into `errors[]` + logged to stderr and skipped (matches pi
  `loader.ts:413-443` and τ's current stderr-skip); an **explicit `-e`** extension that
  fails **raises** — the user named it, so silently skipping it is the anti-pattern
  (§7 decision E0-c).

### E0.2 CLI flags (`cli.py` `build_parser`)

Add, mapping pi `args.ts:104-153`: `--extension/-e` (repeatable path), `--no-extensions/-ne`
(disables *discovery* only — explicit `-e` still load, pi `args.ts:152-153`),
`--exclude-tools/-xt` (csv denylist), `--no-builtin-tools/-nbt`, and the missing
`--no-session` flag (the `Session.create_in_memory` seam is landed; the flag is not).
`--tools/-t` + `--no-tools/-nt` already exist (`cli.py:125-137`). Thread each into
headless (`headless.py`) and the spawn path. `-nbt` degenerates to `--no-tools` until
E1 lands registered tools in the loop — ship it and document the degeneracy, or gate
behind E1 (§7 decision E0-d). Deprioritize `--session-id`/`--session-dir` (Tier 7).
`--append-system-prompt` (Tier 6, needed by the delegate) is a small add — include it
here since E4 depends on it.

### E0.3 Tests
Loader: discovers a global-dir extension + an explicit `-e`; `--no-extensions` suppresses
the former, keeps the latter; a broken discovered ext → `errors[]` + others still load;
a broken **explicit** `-e` → raises; `register(api)` invoked (not returned un-called);
async `register` awaited. Flags: each parses and threads to the headless run config.

---

## E1 — connect the API  (size M; ROADMAP M1)

**Goal:** one `ExtensionAPI` per `AgentSession`, bound to the real bus/registry/session;
registered tools live in the loop; a return-collecting dispatcher exists for E2; real
`get_context_usage()`. No mutating hooks yet (E2), no session-control ops (E3-ctx).

### E1.1 Bind the API
`_make_extension_api()` constructs **one** `ExtensionAPI(session=self, event_bus=self._events,
registry=<session-owned>)` (`agent_session.py:533-540` → real refs; `self._events` is the
loop's bus, `agent_session.py:109`). Drop the legacy backward-compat mirrors (`_handlers`,
`_active_tools`, `_commands`, `_session_name`, `extension_types.py:226-231`) and update the
tests that leaned on them (§7 decision E1-c). `api.on(event, handler)` now subscribes to the
live bus, so the existing 10 notify events reach extensions immediately.

### E1.2 Registered tools reach the loop
`AgentLoop` is built per `prompt()`/`continue_conversation` (`agent_session.py:249-255,
333-339`) with `tools=self._tools` only; the registry is never read. E1 resolves the
registry's active extension tools into `AgentTool` instances and merges them into the
per-turn `tools` list. Because the loop is rebuilt each turn, **runtime registration is
live-next-turn for free** (matches pi). `register_tool` currently takes a raw dict
(`extension_types.py:254`); port pi's `ToolDefinition` shape (`types.ts:435-482`) —
**parameters as a JSON-schema dict** (τ already uses dict tool schemas; do not require
Pydantic/TypeBox) with `name`/`description`/`parameters`/`execute(tool_call_id, params,
signal, on_update, ctx)`.

### E1.3 The return-collecting dispatcher (the E2 substrate)
Build a **separate `ExtensionRunner`-equivalent** alongside the notify `EventBus` (pi keeps
them separate; the bus stays fire-and-forget for the 10 `AgentEvent`s). It dispatches the
**mutating** hook events as their own typed events (a **parallel typed dispatch**, *not* an
extension of the `AgentEvent` Literal — §7 decision E1-a), iterating extensions in load
order and handlers in registration order, awaiting each and threading the return value
forward. E1 lands the dispatcher + the wiring; E2 lands the four hook call-sites. Expose a
`has_handlers(event)` check for the no-extension fast path (pi `agent-session.ts:405-411`).

### E1.4 Real `get_context_usage()` + `send_user_message` de-fictionalized
Replace the `{"total_tokens": 0}` stub with pi's `ContextUsage` shape
`{tokens: int|None, context_window: int, percent: float|None}` (`types.ts:281-287`), reading
`estimate_context_tokens` (already computed for auto-compact, `agent_session.py:514`). Fix
the `send_user_message` default off `"steer"` → `"followUp"`, validate `deliver_as` ∈
`{followUp, nextTurn}` while leaving the param a plain string (extensible, decision 5); the
real queue lands in E3-ctx (E1 just corrects the signature + removes the silent `hasattr`
no-op → raise if the queue isn't present yet).

### E1.5 Tests (`fake_llm` through the full loop)
A registered fake tool becomes callable and executes; `api.on('tool_execution_end', …)`
receives a live event with real payload; `get_context_usage()` returns real non-zero
numbers over a seeded session; a second `register_tool` mid-session is live the next turn.

---

## E2 — mutating hooks  (size M; ROADMAP M2 subset)

**Goal:** the four return-value hooks — `tool_call`, `tool_result`, `before_agent_start`,
`context` — with pi chaining semantics, wired into the existing seams via E1's dispatcher.
Scope is exactly these four (plan §7); `message_end`/`before_provider_*`/`session_before_*`
are **out** (session lifecycle → E3-ctx).

| Hook | τ seam | Event → Result (pi shape) | Chaining / semantics |
|---|---|---|---|
| `tool_call` | `_prepare_tool_call` at the `PreparedToolCall` return (`agent_loop.py:804`) | `{type, tool_call_id, tool_name, input}` → `{block?, reason?}`; **mutate `input` in place** to patch args | first `block:true` short-circuits → convert to `BlockedCall` (error result text = `reason`); **exception = fail-CLOSED block** (pi `agent-session.ts:419-424`); **no re-validation** after mutation (pi parity, §7 decision E2-a) |
| `tool_result` | `_apply_after_hooks` (`agent_loop.py:888`, called `:637` seq / `:694` par) | `{…, content, is_error, details}` → `{content?, details?, is_error?}` partial patch | clone once, field-patch shared event across handlers (later sees earlier); whole-value replace, no deep merge; none set → pass through |
| `before_agent_start` | `AgentSession.prompt()` just before `loop.run()` (`agent_session.py:~247`) | `{prompt, images?, system_prompt}` → `{system_prompt?, message?}` | `system_prompt` **chains** (last wins, live to later handlers); `message`s **accumulate**, injected as custom messages |
| `context` | `_stream_response` before building the context dict (`agent_loop.py:376`), on a **deep copy** | `{type:'context', messages}` → `{messages?}` replaces | fires **before every LLM call** (not per-turn); structuredClone-equivalent first; the `<system-reminder>` seam |

**How the loop reaches the dispatcher (§7 decision E2-b):** the loop holds only the
fire-and-forget `emit` (`agent_loop.py:90`). `AgentSession` injects a **hook-dispatcher
callable** (returning results) into `AgentLoop.__init__`, called at the two tool seams and
the `context` seam; `before_agent_start` fires in `AgentSession.prompt()` (above the loop).
All four gate on `has_handlers` for the zero-extension fast path.

**Fail-Early note (not a violation to remove — a deliberate asymmetry):** `tool_call` is
fail-CLOSED (throw → block); every *other* hook swallows-and-continues but must surface the
error via an `emit_error`-equivalent, never silently drop (pi `runner.ts:754-763`).

**Tests:** veto blocks execution + error text = reason; in-place arg patch reaches the tool;
`tool_result` patch replaces content/is_error; `before_agent_start` chains two system-prompt
handlers + accumulates two messages; `context` injects a `<system-reminder>` visible on the
wire payload; a throwing `tool_call` handler blocks; two-extension load-order chain.

---

## E3-ctx — session-control surface  (size S/M post-substrate; ROADMAP M3 session half)

**Goal:** expose the landed E3 substrate ops on `ExtensionContext` as the model-callable
session-control surface, add the `send_user_message` queue + turn-end deferral, and route
seam-3 lifecycle events onto the bus. Needs E1 (bound API) + the merged substrate. Most of
the *algebra* is done (§0) — this is exposure + two small mechanisms.

### E3c.1 The `ExtensionContext` op surface (net-new methods; delegate to landed code)
Give `ExtensionContext` a handle to the `AgentSession`, then:

| `ctx` method | wraps (landed) | note |
|---|---|---|
| `compact(custom_instructions=None)` | `AgentSession.compact` (`agent_session.py:368`) | + deferred variant (E3c.3) |
| `entries()` | `SessionLog.entries()` / `ConversationTree` (`session_log.py:60`) | thin pass-through |
| `summarize_branch(from_entry, custom_instructions=None)` | module `summarize_branch` (`session_manager.py:705`, already raise-based) | via `subtree_text` → append_branch_summary, à la `navigate_tree` (`backends.py:159-206`) |
| `navigate(target_id, summarize=False, …)` | `append_navigate`/`append_branch_summary` (`session_store.py:396-434`) | in-place branch |
| `fork(entry_id=None)` | in-place = navigate+append; new-file = `Session.fork` (`session_store.py:347`) → path | two behaviors, one op with a mode, or two methods (§7 decision E3-b) |

pi keeps fork/navigate command-only (`types.ts:339-373`); τ exposes them on the base
(handler) context so agent **tools** can call them (decision 2). The gatekeeper veto (E2) is
the safety that makes this acceptable.

### E3c.2 The store-authority seam (the one real architectural knot — §7 decision E3-a)
`AgentSession.compact` mutates the session's **own** `_session_log`; on the **TUI live path**
that is a throwaway `InMemorySessionLog` (the scratch log from the E3 1d landing), while the
live `Session` is owned by the TUI and mutated by `TauBackend.navigate_tree` directly. So an
agent-tool `ctx.compact/fork/navigate` would mutate the **scratch** log, not the live session
— wrong on the TUI path. In **headless**, `AgentSession` owns the real session, so it is
correct there. Resolution options are a genuine decision (§7).

### E3c.3 Turn-end deferral + the injection queue
- **Deferral (decision 3):** a tool requesting compact/fork records intent and returns a
  normal result; drain it at the tail of `prompt()` — the **same site** as
  `_maybe_auto_compact()` (`agent_session.py:301`), i.e. **end-of-`prompt()`**, not
  per-inner-turn (`turn_end` fires per inner turn; the resolved decision means end-of-prompt).
  No loop reentrancy.
- **`send_user_message` queue:** add the `_queue_message` the API already calls
  (`extension_types.py:305`). `followUp` drains at end-of-`prompt()` (re-enters within the
  same call), `nextTurn` queues for the next `prompt()`. Both share the end-of-prompt drain
  with deferred compact/fork. `deliver_as` stays extensible for a future `steer`.

### E3c.4 seam-3 lifecycle events onto the bus
`subscribe_session_events` emits raw dicts `{type, session, **extra}` (`session_store.py:47-70`)
with no consumer. Route them onto the extension bus via a **separate string channel**
(`EventBus.emit_channel`), **not** by extending the `AgentEvent` Literal (which has no
session members — `events.py:46-70`; §7 decision E3-c). This gives `session_start`/
`session_before_fork`/`session_before_compact`/`session_shutdown` their first consumer.

**Tests:** each `ctx` op mutates the right log and re-renders `context_for`; deferred
`compact_now` applies exactly once at end-of-prompt, not mid-turn; `followUp` vs `nextTurn`
land at the defined points; a seam-3 event reaches an `api.on('session_before_compact', …)`
handler.

---

## E4 — demos + cost  (size M = 5×S; the payoff, in `examples/`)

Each demo is a runnable extension + a smoke test. Dependency-ordered (research Part C):

1. **`24_budget.py`** (E1 + cost) — accumulate `message_end`/`turn_end` usage; past a
   USD (or token) threshold inject a warning via `context` then `ctx.abort()` (exists,
   `extension_types.py`). *Note:* the warn-via-`context` version needs E2 — see §7 D1.
2. **`20_delegate.py`** (E0 flags + E1) — a `delegate` tool spawning
   `tau -p --mode json --no-session [--no-extensions] --model … --tools …
   --append-system-prompt <tmp> "Task: …"` children (pi `subagent/index.ts:288-324`);
   single / parallel-N (≤8, 4-concurrent, 50 KB/task cap) / chain (`{previous}`);
   per-child limits (`max_usd`/`max_seconds`/`max_turns`/stuck-detection) and stop_reason
   taxonomy from `pi_orchestration_patterns.md §2`. Parses **τ's** `kind`-schema JSONL
   (`headless.py:279`) behind one adapter fn (Tier-9 swap point). **Parallel children must
   be read-only-scoped** (§7 D-delegate). Rolls usage into `details`.
3. **`22_gatekeeper.py`** (E2) — `tool_call` veto: deny writes outside `.tau/scope.txt`
   prefixes; deny reads/bash touching `tests_heldout/`. The enforcement that makes the
   agent-callable mutation tools safe.
4. **`21_reminders.py`** (E2) — the four-rule bank from `pi_planning_implementing_evaluating.md
   §2` (tests-readonly / root-cause-after-2-failures / scope-guard / no-new-deps): track state
   on `tool_call`/`tool_result`, inject `<system-reminder>` via `context` with per-rule
   cooldowns (3/4/2/1). Read `event.input.path` (τ controls the field — drop pi's `args??input`
   dual-read).
5. **`23_context_surgeon.py`** (E3-ctx + E2 safety) — agent tools `compact_now`
   (turn-deferred), `summarize_history(from_entry)`, `fork_session(entry_id)` → returns the
   forked path + optionally spawns a delegate. Composes demos 2+3; lands last.

### E4.cost (§6)
Compute `cost_usd` from the optional per-model `cost:{input, output, cache_read, cache_write}`
(USD/M) block in `config.json`, carried by `model_config` (`headless.py:46-66`). Port pi's
`calculateCost` (`models.ts:39-48`), collapsed (τ has no `cacheWrite1h` field):
`sum(price[k]/1e6 * usage[k])`. **Emit `cost_usd` only when the block is present** — an
absent block yields tokens-only, never a fabricated `$0` (a real free model `cost:{…:0}` and
*unknown* cost must read differently — the one subtle Fail-Early trap). `cache_write_tokens`
is inert against today's provider (never populated) — real 0, comment it. Compute home is a
decision (§7 D2).

---

## 6. Sequencing & tests

Strict: **E0 → E1 → E2**; E3-ctx needs E1 + the (landed) substrate; E4 items land
incrementally. **E2 is the tallest pole** — 3 of 5 demos need it, and decision 2 makes it
the prerequisite for the agent-callable mutation tools. Suggested landing order, each a
green-gated commit (ruff + mypy + pytest): **E0 → E1 (+ cost) → 24_budget + 20_delegate →
E2 → 22_gatekeeper + 21_reminders → E3-ctx → 23_context_surgeon → walkthrough doc.**
Testing spine: E1/E2 via `fake_llm` through the full loop (registered fake tools, veto/patch
assertions, injected-context on the wire payload); E3-ctx via the property-style entry-tree
tests the substrate already uses; delegate smoke-tested by spawning `tau -p` against the fake
provider in-repo. Gate green per commit.

---

## 7. Open decisions

**Mechanical — RESOLVED in this spec** (recorded for audit; pi-faithful + Fail-Early):
E0-a `register(api)` verb, delete the dead loader + `extend` path. E0-b file-path importlib,
defer entry_points. E0-c explicit-`-e` failure is fatal, discovered failure is collected.
E0-d ship `-nbt` degenerate-to-`--no-tools` with a doc note. E1-a mutating hooks are a
parallel typed dispatch, not an `AgentEvent`-Literal extension; keep `EventBus` notify-only.
E1-c drop the legacy API mirrors, update tests. E2-a preserve pi's no-re-validation-after-
mutation (veto is the guard). E2-b `AgentSession` injects a hook-dispatcher into `AgentLoop`.
E3-b `fork` is one op with an in-place-vs-export mode. E3-c seam-3 events ride a separate bus
channel. `get_context_usage` adopts pi's `ContextUsage` shape. Discovery order is moot in E0
(no project dir until Tier 8).

**Genuinely yours — surfaced by the research, recommendation given:**

- **D1 — Budget demo dependency.** The plan says "E1 only," but the warn-then-abort behavior
  injects via the `context` hook (**E2**). *Recommend:* land `24_budget` **after E2** (full
  warn-then-abort); a minimal abort-only guard is possible in E1 if you want an early demo.
- **D2 — Where `cost_usd` computes.** (a) *Provider-home* — thread prices into the provider,
  fill the already-present frozen `Usage.cost`, giving **per-message** cost (pi-faithful; the
  budget guard can then abort mid-run on dollars) but re-plumbs the provider + frozen `Usage`.
  (b) *Emit-boundary* — compute in the backend/headless where `model_config` is in scope,
  **final-`done` only**, no provider changes, but the budget guard can only threshold on
  tokens mid-run. *Recommend (a)* if the budget demo should guard on dollars mid-run; else (b).
- **D3 — E3-ctx store authority on the TUI path.** An agent-tool `ctx.compact/fork` mutates
  `AgentSession`'s log, which on the TUI is the scratch `InMemorySessionLog` (headless is
  fine). Options: **(a)** bind `AgentSession` to the live `Session` now — i.e. do the deferred
  `self.messages → transcript_view` injection as part of E3-ctx (bigger, unifies the TUI);
  **(b)** ship agent-callable session-control for **headless first**, scope it out of the TUI
  until the transcript_view refactor. *Recommend (b)* — smaller, and the TUI refactor was
  already staged as a separate follow-up; E3-ctx's demo (`23_context_surgeon`) can be
  smoke-tested headless.
- **D-delegate — Tier-9 coupling + hermeticity.** τ's `--mode json` child emits a `kind`-schema
  with **no `stop_reason`/`model`/per-turn usage**, so the delegate's failure-detection
  degrades to exit-code + stderr, and its stuck/limit logic loses real signals. Also pi's own
  subagent omits `--no-extensions`. *Two sub-calls:* (i) accept degraded delegate failure
  signals for E4, or **pull pi-faithful `--mode json` (Tier 9) forward** as a delegate
  prerequisite? *Recommend:* accept degraded for E4 behind the one-adapter seam (Tier-9 swap
  touches one place). (ii) pass `--no-extensions` to children for true hermeticity?
  *Recommend:* yes (add the flag in E0, pass it) — τ wants the isolation guarantee.
- **D-parallel — read-only enforcement.** pi leaves "never parallelize writers" to author
  discipline. *Recommend:* **hard code-guard** in `20_delegate.py` — parallel mode forces a
  read-only `--tools` allowlist (Fail-Early; cheap; safety-relevant), with the write-tool
  classification a small constant list.
