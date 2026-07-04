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

### E3c.2 The store-authority seam — RESOLVED: bind the live Session now (D3)
`AgentSession.compact` mutates the session's own `_session_log`; on the **TUI live path** that
is a throwaway `InMemorySessionLog` (the E3-1d scratch log), while the live `Session` is owned
by the TUI. E3-ctx **retires that split** by pulling the deferred `self.messages →
transcript_view` refactor forward (plan §4.5 endgame):

1. The TUI's `AgentSession` is constructed with the **live `Session`** as its `SessionLog`
   (drop the scratch `InMemorySessionLog`, `backends.py` E3-1d wiring).
2. `AgentSession` becomes the **sole persister** — remove `app.py`'s own `append_message`
   writes (`app.py:1145,1239`), resolving the E3-1d double-write tension that forced the
   scratch log in the first place.
3. The TUI transcript render becomes a **view over `ConversationTree`** — `self.messages`
   rebuilt from `session.context` at structural points (the `Session.context` seam already
   landed, `session_store.py`), matching pi's `rebuildChatFromMessages` (`interactive-mode.ts`).
   Incremental streaming render is unchanged; full rebuild only on turn-end / compact / navigate.

Then agent-tool `ctx.compact/fork/navigate` mutate the **one authoritative session** on both
the TUI and headless paths — identical semantics everywhere. This is the load-bearing reason
D3 chose "bind now": it is the only way the agent-callable session tools are correct in the
TUI, and it lands the view-discipline refactor that was E3's last deferred piece. **Sequence
this sub-step first within E3-ctx** (it is a refactor of landed code; the `ctx` op surface in
E3c.1 sits on top of it).

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

1. **`24_budget.py`** (E2 + cost; lands **after** E2 per D1) — accumulate `message_end`/
   `turn_end` usage, computing running `$` from tokens × the model's `cost` block; past a
   USD (or token) threshold inject a warning via the `context` hook then `ctx.abort()`
   (`abort` already exists, `extension_types.py`).
2. **E-json** (Tier 9 json, pulled forward — D-delegate) then **`20_delegate.py`** (E0 flags
   + E1 + E-json) — first land pi-faithful `--mode json` (per-message `message_end` carrying
   `usage`/`model`/`stop_reason`, `type` discriminator, header line first) so children emit
   real limit/failure signals. Then the `delegate` tool spawns
   `tau -p --mode json --no-session --no-extensions --model … --tools …
   --append-system-prompt <tmp> "Task: …"` children (pi `subagent/index.ts:288-324`);
   single / parallel-N (≤8, 4-concurrent, 50 KB/task cap) / chain (`{previous}`); per-child
   limits (`max_usd`/`max_seconds`/`max_turns`/stuck-detection) + stop_reason taxonomy from
   `pi_orchestration_patterns.md §2`, now reading real child signals. **Parallel children
   forced read-only** (hard guard, D-parallel). Rolls usage into `details`.
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
is inert against today's provider (never populated) — real 0, comment it. **Compute at the
emit boundary** (backend/headless, where `model_config` is in scope; final `done`/`agent_end`
only) — D2 resolved; the frozen `Usage` is untouched and `24_budget` computes its own running
`$` from per-message tokens × config prices.

---

## 6. Sequencing & tests

Strict: **E0 → E1 → E2**; E3-ctx needs E1 + the (landed) substrate; E4 items land
incrementally. **E2 is the tallest pole** — 3 of 5 demos need it, and decision 2 makes it
the prerequisite for the agent-callable mutation tools. Two resolved decisions reshape the
order: **E-json** (pi-faithful `--mode json`, Tier 9 pulled forward per D-delegate) slots
**between E1 and the delegate demo**; and **E3-ctx is now M/L** because it carries the
live-Session bind + transcript_view refactor (D3). Suggested landing order, each a
green-gated commit (ruff + mypy + pytest): **E0 → E1 (+ cost at the emit boundary) →
E-json (Tier-9 json) → 20_delegate → E2 → 22_gatekeeper + 21_reminders + 24_budget →
E3-ctx (live-Session bind first, then the `ctx` op surface) → 23_context_surgeon →
walkthrough doc.** (`24_budget` lands **after** E2, not before — its warn-then-abort needs
the `context` hook, D1.) The full step-by-step is §8.
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

**Genuinely yours — RESOLVED 2026-07-03:**

- **D1 — Budget demo dependency → land `24_budget` AFTER E2** (full warn-then-abort via the
  `context` hook). A minimal abort-only guard in E1 is optional and not planned.
- **D2 — Cost home → EMIT-BOUNDARY, final-only.** Compute `cost_usd` in the backend/headless
  where `model_config` (prices) is in scope; attach it to the `done`/`agent_end` event only.
  No provider re-plumbing; the frozen `Usage` is untouched. The `24_budget` demo computes its
  own running `$` from per-message tokens × config prices, so it still thresholds mid-run.
- **D3 — E3-ctx store authority → BIND THE LIVE SESSION NOW.** E3-ctx **pulls the deferred
  `self.messages → transcript_view` TUI refactor forward**: retire the scratch
  `InMemorySessionLog` on the TUI live path, give the TUI's `AgentSession` the live `Session`
  as its `SessionLog`, and drop `app.py`'s own `append_message` writes (AgentSession becomes
  the sole persister — resolving the E3-1d double-write tension). Agent-tool `ctx.compact/fork/
  navigate` then mutate the one authoritative session on **both** paths. This makes E3-ctx
  **M/L, not S/M** (see revised E3c.2). Consequence: the TUI transcript render becomes a view
  over `ConversationTree` (pi's `rebuildChatFromMessages` pattern), landing the §4.5 endgame.
- **D-delegate — PULL TIER 9 (pi-faithful `--mode json`) FORWARD** as a delegate prerequisite.
  Before `20_delegate`, implement the pi-faithful JSON event schema (per-message `message_end`
  carrying `usage`/`model`/`stop_reason`, `type` discriminator, header line first — plan
  Tier 9 json half) so the delegate's stuck-detection and per-child limits have real child
  signals. This inserts an **E-json** step between E1 and the delegate demo (see §6). The child
  is also spawned with **`--no-extensions`** (added in E0) for true hermeticity.
- **D-parallel — HARD CODE-GUARD** in `20_delegate.py`: parallel mode forces a read-only
  `--tools` allowlist (Fail-Early), write-tool classification a small constant list.

---

## 8. Directly executable step plan

Each step is one landable, green-gated commit (ruff + mypy + `pytest`). "Files" names the
primary targets; "Verify" is the test that proves it. Steps within a phase are ordered; phases
are strictly ordered except where noted. Commit prefix `feat(ext):` unless noted. This is the
list a builder (or the implement→review→fix workflow) executes top to bottom.

### E0 — loader + flags
- **S1 — one loader.** Delete `tau_agent_core/extensions/loader.py` (dead) and its
  `extensions/__init__.py` re-export; rewrite `sdk._load_extensions` as the single loader:
  verb `register(api)`, file-path importlib, `await` async factories, discovery = global
  `~/.tau/extensions` + explicit paths only, dedup by resolved path. Return
  `LoadExtensionsResult{extensions, errors:[{path,error}]}`. Explicit-`-e` load failure
  **raises**; discovered failure → `errors[]` + stderr, continue. Files: `sdk.py`,
  `extensions/__init__.py`, new `extensions/loader.py` (canonical) or fold into `sdk.py`.
  *Verify:* `test_extension_loader.py` — discovered vs explicit, `-ne`, broken-discovered
  collected, broken-explicit raises, async `register` awaited.
- **S2 — CLI flags.** Add `-e/--extension` (append), `-ne/--no-extensions`, `-xt/--exclude-tools`,
  `-nbt/--no-builtin-tools`, `--no-session`, `--append-system-prompt` to `cli.py build_parser`
  + `CLIArgs`; thread into `headless.py`. `-nbt` degenerates to `--no-tools` (doc note).
  Use `parse_known_args` to preserve unknown `--flags` for future `registerFlag` (or defer,
  noting it). Files: `cli.py`, `headless.py`. *Verify:* flags parse + reach the run config;
  `--no-extensions` keeps `-e`.

### E1 — connect the API (+ cost)
- **S3 — bind the API.** `_make_extension_api()` → one `ExtensionAPI(session=self,
  event_bus=self._events, registry=<session-owned>)`; drop legacy mirrors
  (`_handlers`/`_active_tools`/`_commands`/`_session_name`) and update dependent tests.
  Files: `agent_session.py:533-540`, `extension_types.py`. *Verify:* `api.on('tool_execution_end')`
  receives a live event over a `fake_llm` turn.
- **S4 — registered tools reach the loop.** Resolve the registry's active extension tools →
  `AgentTool` and merge into the per-turn `tools` (`agent_session.py:249-255,333-339`); port
  `register_tool` to pi's `ToolDefinition` shape (dict params). Files: `agent_session.py`,
  `extensions/registry.py`, `extension_types.py`. *Verify:* a registered fake tool executes;
  a mid-session registration is live next turn.
- **S5 — return-collecting dispatcher.** New `ExtensionRunner`-equivalent beside the notify
  `EventBus`: typed hook events, load-order/registration-order chaining, `has_handlers`.
  Files: new `extensions/runner.py` (or `events.py` sibling). *Verify:* unit — collect + chain
  + short-circuit; no-handler fast path.
- **S6 — real usage + de-fictionalize injection.** `get_context_usage()` → pi `ContextUsage`
  `{tokens, context_window, percent}` via `estimate_context_tokens`; `send_user_message`
  default → `followUp`, validate `∈{followUp,nextTurn}`, **raise** (not silent no-op) until the
  E3-ctx queue exists. Files: `extension_types.py`. *Verify:* real non-zero usage; bad
  `deliver_as` raises.
- **S7 — cost at the emit boundary.** Optional per-model `cost:{input,output,cache_read,
  cache_write}` (USD/M) in `config.json`; compute `cost_usd` in `backends.py` (`usage_totals`,
  `:520-531`) + `headless.py` `done` (`:279`) from `model_config`; **emit only when present**
  (absent ≠ `$0`). Files: `backends.py`, `headless.py`, config docs. *Verify:* cost present with
  config, absent without; free `cost:{…:0}` ≠ absent.

### E-json — pi-faithful `--mode json` (Tier 9, pulled forward; before the delegate)
- **S8 — pi-faithful json schema.** Add a `tau_event → pi AgentSessionEvent` serializer sourced
  from the `AgentEvent` bus: `type` discriminator (not `kind`), per-message `message_end`
  carrying `usage`/`model`/`stop_reason`, session header line first. Behind `--mode json`
  (gate the old `kind` schema behind a flag or replace — decide at build time; a demo adapter
  reads whichever ships). Files: `backends.py`, `headless.py`. *Verify:* json stream carries
  `message_end` with usage/model/stop_reason; header first.

### E-demo-1 — delegate (needs E0 + E1 + E-json)
- **S9 — `examples/20_delegate.py`.** `delegate` tool spawning `tau -p --mode json --no-session
  --no-extensions --model … --tools … --append-system-prompt <tmp> "Task: …"`; single /
  parallel-N (≤8, 4-concurrent, 50 KB/task) / chain (`{previous}`); per-child `max_usd`/
  `max_seconds`/`max_turns`/stuck-detection + stop_reason taxonomy reading E-json signals;
  usage → `details`; **parallel forces read-only `--tools`**. *Verify:* smoke test spawning
  `tau -p` against the fake provider (single + parallel + a forced-read-only assertion).

### E2 — mutating hooks (inject dispatcher into AgentLoop)
- **S10 — thread the dispatcher.** `AgentSession` injects the hook-dispatcher into
  `AgentLoop.__init__`; `has_handlers` fast path. Files: `agent_session.py`, `agent_loop.py`.
- **S11 — `tool_call`** veto/patch at `_prepare_tool_call` (`agent_loop.py:804`): `{block,reason}`
  → `BlockedCall`; in-place `input` patch; **exception = block**; no re-validation. *Verify:*
  veto blocks + error text = reason; patch reaches tool; throwing handler blocks.
- **S12 — `tool_result`** patch at `_apply_after_hooks` (`agent_loop.py:888`): `{content?,
  details?,is_error?}` chained. *Verify:* patch replaces content/is_error.
- **S13 — `before_agent_start`** in `prompt()` before `loop.run()`: `system_prompt` chains,
  `message`s accumulate. *Verify:* two handlers chain + accumulate.
- **S14 — `context`** in `_stream_response` (`agent_loop.py:376`) on a deep copy, before every
  LLM call: `{messages?}` replaces. *Verify:* injected `<system-reminder>` on the wire payload.

### E-demo-2 — E2 demos (any order; after E2)
- **S15 — `examples/22_gatekeeper.py`** — `tool_call` veto: deny writes outside `.tau/scope.txt`
  prefixes; deny reads/bash touching `tests_heldout/`. *Verify:* out-of-scope write blocked;
  held-out read blocked.
- **S16 — `examples/21_reminders.py`** — the four-rule bank (tests-readonly / root-cause-after-2 /
  scope-guard / no-new-deps) with cooldowns 3/4/2/1, state via `tool_call`/`tool_result`,
  inject via `context`; read `event.input.*`. *Verify:* each rule fires once then cools down.
- **S17 — `examples/24_budget.py`** — accumulate usage → running `$` (tokens × config cost);
  past threshold inject warning via `context` then `ctx.abort()`. *Verify:* aborts past
  threshold; warning injected.

### E3-ctx — session-control surface (bind live first)
- **S18 — bind the live Session (D3 refactor).** TUI `AgentSession` takes the live `Session` as
  its `SessionLog` (retire the scratch `InMemorySessionLog`); remove `app.py`'s own
  `append_message` writes (`:1145,1239`) → AgentSession sole persister; TUI transcript becomes a
  `ConversationTree`/`session.context` view rebuilt at structural points. Files: `backends.py`,
  `app.py`, `agent_session.py`. *Verify:* one write path (no double-persist); TUI resume/render
  unchanged; existing suite green.
- **S19 — `ctx` op surface.** Add `compact`/`entries`/`summarize_branch`/`navigate`/`fork` to
  `ExtensionContext`, delegating to the landed methods (§E3c.1). *Verify:* each op mutates the
  one session + re-renders `context_for`.
- **S20 — deferral + injection queue.** Turn-end drain at the `_maybe_auto_compact` site
  (`agent_session.py:301`); `_queue_message` with `followUp`(end-of-prompt) / `nextTurn`(next
  prompt). *Verify:* deferred `compact_now` applies once at end-of-prompt; followUp/nextTurn land.
- **S21 — seam-3 onto the bus.** Route `subscribe_session_events` dicts onto a separate
  `EventBus` channel. *Verify:* `api.on('session_before_compact')` fires.

### E-demo-3 + capstone
- **S22 — `examples/23_context_surgeon.py`** — `compact_now` (deferred), `summarize_history`,
  `fork_session` (+ optional delegate spawn). *Verify:* headless smoke test of each tool.
- **S23 — walkthrough doc** — `docs/` composed run (gatekeeper + reminders + budget wrapping a
  delegate-driven plan→implement→evaluate). No code; ties the demos together.

**Fast path for a builder:** S1–S2 (E0) · S3–S7 (E1+cost) · S8 (E-json) · S9 (delegate) ·
S10–S14 (E2) · S15–S17 (demos) · S18–S21 (E3-ctx) · S22–S23. Each block is independently
green-gatable; S3 is the keystone (nothing observable until the API is bound).
