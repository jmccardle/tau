# EXTENSIONS-DEMO-ROADMAP — E6–E11: the demo-extension arc

Status: **PLANNED** (no implementation yet). Successor to `EXTENSIONS-E5-WIRING.md`
(E0–E5 landed through S37). Step numbering continues at S38.

Grounded in a 2026-07-04 survey of three surfaces: the τ extension API as built
(`extension_types.py`, `extensions/runner.py`, `sdk.py`, `agent_session.py`),
the pi example-extension catalog (~60 examples at
`~/Development/pi/packages/coding-agent/examples/extensions/`, API contract in
`src/core/extensions/types.ts`), and the τ frontends (`app.py`, `headless.py`).
Claims below about "what exists" cite file:line anchors from that survey.

---

## §0 Where we stand

**Works today (E0–E5):** three mutating hooks (`tool_call` veto/arg-patch,
`tool_result` durable edit, `before_agent_start` durable customMessage nodes +
system-prompt chain), ten notify events (`agent_start/end`, `turn_start/end`,
`message_*`, `tool_execution_*`, `"all"`), `register_tool` (sync/async execute,
prompt snippets, execution_mode), `register_command` (async, raw-string args),
`send_user_message` (followUp/nextTurn), `set_session_name`, the ctx tree ops
(`entries/compact/summarize_branch/navigate/fork`), `ctx.abort/shutdown/
get_context_usage`, load via `-e` + `~/.tau/extensions` discovery, `/extensions`
read-only palette, `api.ui.notify` → TUI toast / headless stderr.

**Verified gaps** (each is either an E6–E10 work item or an explicit non-goal):

| # | Gap | Anchor | Disposition |
|---|---|---|---|
| G1 | No session lifecycle hooks (`session_start`/`session_shutdown`) | `runner.py:109` | E6 |
| G2 | No `input` hook (transform the user prompt pre-node) | — | E6 |
| G3 | Notify handlers: return ignored, **exceptions swallowed silently** | `events.py:167-183` | E6 (surface, don't swallow) |
| G4 | `api.append_entry` is RAM-only — lost on restart | registry `_entry_store` | E6 (persist as durable entries) |
| G5 | `api.send_message` inert (calls nonexistent `_append_custom_message`) | `extension_types.py` | E6 (implement or delete — Fail-Early forbids the silent no-op) |
| G6 | `register_flag`/`get_flag` dead (`value` never populated) | — | E6 (replace with per-extension config; delete flags) |
| G7 | Command handler return value discarded — commands can only toast | `agent_session.py:1069-1072` | E7 |
| G8 | TUI `confirm/select/input` delegates raise `NotImplementedError` | `app.py:88-101` | E7 |
| G9 | Headless dialogs silently auto-resolve (confirm→True, select→first) | `extension_types.py:58-89` | E7 (policy decision D-E6-2) |
| G10 | Extension activity invisible in `--mode json` (notify → stderr) | `headless.py` | E7 |
| G11 | Vetoes render as generic errored tool result (no "blocked" styling) | E5 §4 leftover | E7 |
| G12 | `ExtensionRunner.on_error` unwired — hook errors go to raw stderr | no callers | E6 |
| G13 | No per-extension config object | — | E6 |
| G14 | No model get/set from extensions (only private `ctx._session._model`) | — | E6 |
| G15 | No pricing/cost API — `24_budget` hand-parses `~/.tau/config.json` | `examples/24_budget.py` | E8 (extension-side kit, not harness) |
| G16 | No timer/scheduler/idle facility | — | E8 (extension-side; pi also has none — its examples use raw `setInterval`) |
| G17 | No custom widgets/panels/status line (pi's `ctx.ui` surface) | — | E10 |
| G18 | Tool/command names globally flat; collisions overwrite w/ warning | `registry.py` | D-E6-5 |
| G19 | No runtime enable/disable/reload (palette read-only, per D-E5-6) | — | E10 |
| G20 | No whole-history transform/redaction (context hook retired) | D-E5-5 | non-goal here (unchanged) |
| G21 | Examples `01`/`02` stale — call nonexistent `api.notify`, `01` "blocks" via a notify event that cannot block | `examples/01,02` | E6 (fix immediately; a broken safety demo is worse than none) |

**pi porting economics** (from the catalog): ~9 pi examples port with zero new
API; ~15 more are one small primitive away (G1, G2, G7, G8); the rest are
blocked on rich UI (G17), deep lifecycle (compaction/fork/tree events),
`user_bash`, or `registerProvider`. Roughly half of pi's examples exist mainly
to demo its `ctx.ui` — we deliberately do NOT chase that surface 1:1 (§6).

---

## §1 Design frame

### 1.1 The five atoms, mapped onto τ

The research catalog reduces every harness pattern to five primitives. τ has a
native answer for each — and one structural advantage:

| Atom | τ construct | Status |
|---|---|---|
| **Isolated agent** | `tau -p --mode json` subprocess (the `20_delegate` pattern) | works; needs a clean kit wrapper (E8) |
| **Event stream** | `--mode json` JSONL out; `EventBus` in-process | works; extension activity missing from the stream (G10) |
| **Gate** | `tool_call` veto + durable `is_error` node; kit gate-runner for external checks | works; kit helper in E8 |
| **Backplane** | **the session tree itself** + durable `append_entry` (G4) | the τ differentiator — see below |
| **Budget/ledger** | usage fold off `message_end` + pricing kit helper (G15) | hand-rolled today; kit in E8 |

**The tree-as-backplane thesis.** In pi, extension state lives in RAM and gets
reconstructed by replaying entries on `session_start`/`session_tree`. In τ, the
tree-as-truth invariant (E5 §1) means anything an extension persists onto the
path is *automatically* durable, rendered, reload-safe, and (if it's a message
node) model-visible. The demo arc should showcase this: τ extensions don't
need a side database for conversation-scoped state — the tree IS the typed
canon store from the ideation catalog. Cross-session state (ledgers, red-team
findings) uses a small file-store kit helper instead (§4).

### 1.2 What belongs harness-side vs extension-side

Rule of thumb, applied throughout: **harness-side** = new *places to stand*
(hook points, UI reach, visibility channels, config plumbing) — things an
extension cannot build for itself without private-attr reaching. **Extension-
side (the kit, §4)** = *behaviors* composable from public API — spawning,
gating, metering, watching. pi validates this split: it ships no timer API and
its examples are fine; τ's kit makes those recurring behaviors importable
instead of copy-pasted.

### 1.3 Invariant discipline for every new hook

Any new mutating hook must produce **durable path effects only** (append or
edit-the-node-in-question), same as E5. Specifically:

- `input` (G2) transforms the prompt **before the user node exists** — the
  transformed text is what gets persisted, rendered, and sent. No invariant
  violation, because there is never a second copy. (Same reasoning that made
  `before_agent_start` legal.)
- `turn_end` (new) may return `{message}` → appended as a durable customMessage
  node before the next turn, exactly like `before_agent_start`. It may NOT
  rewrite prior nodes.
- `session_start`/`session_shutdown` are **notify-grade** (no return effect)
  but with E6's error surfacing — they exist for setup/teardown side effects
  (watchers, state reconstruction from `ctx.entries()`, exit commits).

---

## §2 E6 — Platform integrity + lifecycle completion (harness)

Make the existing surface honest, then add the four hook points the demo arc
needs. Nothing here is speculative; every item unblocks named demos in §5–§7.

| Step | Work | Unblocks |
|---|---|---|
| S38 | **Kill or fix the inert API** (Fail-Early): implement `api.send_message` as a durable customMessage append via a real `AgentSession._append_custom_message` (display-only by default, `visible_to_model` opt-in is D-E6-1); **delete** `register_flag`/`get_flag` (superseded by S40). Fix examples `01` (real `tool_call` veto) and `02` (`api.ui.notify`). | G5, G6, G21; pi `send-user-message`, `file-trigger` |
| S39 | **Durable `append_entry`** — persist `{customType, data}` entries into the session log as non-message entries (new `customEntry` kind, excluded from `convert_to_llm`), readable back through `ctx.entries()`. Reload-invariant test à la S29. | G4; todo demo, bookmarks, any tree-backplane state |
| S40 | **Per-extension config**: `api.config` dict sourced from `~/.tau/config.json` `"extensions": {"<name>": {...}}` + per-run `--ext-config name.key=value` overrides. | G13; every configurable demo (budget ceiling, gate rules) |
| S41 | **`session_start` / `session_shutdown` hooks** (notify-grade, error-surfaced; shutdown fires on TUI quit, headless completion, SIGINT/SIGTERM). | G1; pi `auto-commit-on-exit`, `file-trigger`, state reconstruction |
| S42 | **`input` hook** (mutating): `{prompt, images}` → `{prompt?, handled?}`; transform pre-node (§1.3), `handled:true` consumes the input without starting a turn (command-like extensions). | G2; pi `inline-bash`, `input-transform`; slash-adjacent UX |
| S43 | **`turn_end` mutating hook**: `{turn_index, usage, messages}` → `{message?}` durable append. Notify `turn_end` remains for pure observers. | trigger-compact, checkpoint-annotate, status |
| S44 | **Error visibility**: wire `ExtensionRunner.on_error` → TUI warning notice + headless structured stderr/JSON line; notify-bus handler exceptions surfaced through the same path instead of swallowed (`events.py:167-183`). | G3, G12 |
| S45 | **Model + usage access**: `ctx.get_model() -> {id, provider, context_window}`, `ctx.set_model(name)` (next-turn effect, mirrors pi `setModel`), and a public per-completion usage accessor so `24_budget` stops digging through `event.message`. | G14; presets, router demos |

---

## §3 E7 — Visibility & interaction, tier 1 (frontends)

The smallest set that makes extensions *feel present* in both frontends and
unblocks the Medium pi ports. No custom widgets yet (that's E10).

| Step | Work | Unblocks |
|---|---|---|
| S46 | **Command output channel**: `run_extension_command` returns the handler's value; TUI renders a string/renderable result as a display-only system box (same chrome as `/extensions`); headless prints it (text) / emits it (json). | G7; every report-style command (`/todos`, `/ledger`, `/findings`) |
| S47 | **Wire TUI dialogs**: implement `confirm/select/input` delegates on the existing `ModalScreen` templates (`app.py:104-244` are the pattern; the stubs at `app.py:88-101` name this). Optional timeout param (pi's `timed-confirm`) deferred. | G8; `permission-gate` proper, any human-gate demo |
| S48 | **Headless dialog policy** (D-E6-2 resolved first): default **Fail-Early raise** when a dialog fires headless with no policy; `--ui-defaults confirm=yes,select=first` (or config) restores the old auto-answer *explicitly*. Silent auto-approve of a permission gate is exactly the fallback anti-pattern. | G9 |
| S49 | **Extension events in `--mode json`**: add a parallel JSONL record type `{"type":"extension", "kind": "notify"\|"veto"\|"error"\|"custom", "extension": name, ...}` emitted alongside the closed `AgentEvent` set (the AgentEvent Literal stays closed; this is a separate record family, like the session header line). `api.ui.notify` headless routes here instead of bare stderr. | G10; orchestrators reading the stream (the event-stream atom) |
| S50 | **Blocked-call rendering**: distinct "⛔ blocked by <ext>: <reason>" presentation for vetoed tool calls in TUI + a `blocked:true` marker in the JSON record. | G11 |
| S51 | **Palette args**: commands may declare `"args": "<placeholder>"`; palette entry then opens the S47 input modal before dispatch. | palette parity with typed `/cmd args` |
| S52 | **Inter-extension channels**: bless `EventBus` custom channels under `ext:<name>:<topic>` via `api.emit(topic, payload)` / `api.on("ext:...")` (the latent `emit_channel` facility, `events.py:185-199`). In-RAM, fire-and-forget, never model-visible — explicitly NOT a backplane. | pi `event-bus`; kit coordination |

---

## §4 E8 — `tau_ext_kit`: agentic extension primitives (extension-side)

A small importable library shipped alongside the examples (location decision
D-E6-3: `examples/ext_kit/` vs a fourth package). **Not part of the harness**
— everything here composes public API only, and each helper is the distilled
form of a pattern from the research catalog. One module per atom:

| Step | Module | Contents |
|---|---|---|
| S53 | `ext_kit.spawn` — *isolated agent* | `spawn_tau(prompt, model=None, tools=None, cwd=None, timeout=None) -> ChildResult` wrapping `tau -p --mode json` subprocess; streamed event iterator; usage/cost rollup; abort propagation from `ctx.signal`; bounded `WorkerPool(n)`. (Extracted + hardened from `20_delegate`.) |
| S54 | `ext_kit.stream` — *event stream* | JSONL event reader for child streams; `StuckDetector` (N identical consecutive tool calls → flag/kill); `ProgressWatchdog` (no event for T seconds → flag); turn/tool counters. |
| S55 | `ext_kit.gate` — *gate* | `run_gate(cmd, parse=...) -> GateResult` (exit-code or regex verdict); `verdict_node(result)` → durable customMessage text block; `revert_and_recheck(paths, gate)` (anti-cheat: stash, re-run, restore). |
| S56 | `ext_kit.state` — *backplane* | `TreeStore(ctx, custom_type)` — typed records over durable `append_entry` (S39) with `load()` reconstruction from `ctx.entries()` (conversation-scoped, reload-safe); `FileStore(name)` — atomic JSON under `~/.tau/ext-state/<name>.json` (cross-session). |
| S57 | `ext_kit.ledger` — *budget/ledger* | `Pricing.from_config()` (formalizes what `24_budget` hand-rolls); `UsageMeter` folding `message_end`/S45 usage; `CostLedger` (JSONL append, `$/outcome` queries); `Ceiling(limit, on_warn, on_stop)` bang-bang controller. |
| S58 | `ext_kit.steer` — in-loop steering | `ReminderBank` (generalized `21_reminders`: rule → threshold → durable `<system-reminder>` edit); `TurnDebouncer`; `wrap_tool(name, before=None, after=None)` — shadow a built-in tool with pre/post logic (the pi `tool-override` pattern as a helper). |

Refactor `20_delegate`, `21_reminders`, `24_budget` onto the kit as the proof
that the abstractions are the right ones (S59).

---

## §5 E9 — Base demo extensions (broad appeal)

Numbered `examples/3x_*.py`. Each entry: what it shows → what it uses.
Ordered so each demo introduces at most one new concept.

| Step | Demo | Shows | Uses |
|---|---|---|---|
| S60 | `30_permission_gate.py` (pi port, replaces broken `01`) | human-in-the-loop veto | `tool_call` veto + S47 `confirm`; headless policy S48 |
| S60 | `31_protected_paths.py` (pi port) | pure policy gate, zero UI | `tool_call` veto; S40 config for the path list |
| S60 | `32_pirate.py` + `33_claude_rules.py` (pi ports) | system-prompt chain; rules dir → prompt | `before_agent_start` `system_prompt` |
| S61 | `34_desktop_notify.py` (pi `notify` port) | terminal OSC 777 ping on completion | notify `agent_end` (works today) |
| S61 | `35_auto_commit_on_exit.py` (pi port) | exit-time side effect | S41 `session_shutdown` |
| S61 | `36_file_trigger.py` (pi port) | external world → conversation | S41 watcher + `send_user_message` |
| S62 | `37_inline_bash.py` (pi port) | `!{cmd}` expansion in prompts | S42 `input` hook |
| S62 | `38_todo.py` (pi port, τ-flavored) | **tree-backplane state** + command output | `register_tool` + `TreeStore` (S56) + S46 `/todos` report |
| S63 | `39_trigger_compact.py` (pi port) | self-managing context | S43 `turn_end` + `ctx.get_context_usage` + `ctx.compact(defer=True)` |
| S63 | `40_handoff.py` (**τ-native flagship** — pi needs `newSession`+custom UI for this; τ's tree ops make it two calls) | focused continuation session | `ctx.summarize_branch` + `ctx.fork(mode="export")` + S46 output |
| S64 | `41_bookmarks.py` | labeled tree waypoints | `TreeStore` + `ctx.navigate` + S46 listing |
| S64 | `42_session_autoname.py` (pi `session-name` port) | ambient metadata | notify `message_end` + `set_session_name` |
| S65 | `43_budget_ledger.py` (upgrade of `24`) | metered ceiling with a report | `ext_kit.ledger` + S46 `/ledger` + durable warn node |

---

## §6 E10 — Rich control surfaces (TUI panels + CLI parity)

### 6.1 When a slash command is not enough

A slash command (even with S46 output) is right for **fire-and-forget verbs and
point-in-time reports**. You need a richer interface exactly when one of three
things is true:

1. **The decision needs structure** — multi-field input, pick-N-of-M, ranked
   choice. A raw `args: str` forces the user to memorize syntax. → *forms*.
2. **The state evolves while the agent runs** and the user must steer mid-run —
   a delegate fleet's children starting/finishing/costing, a budget burning
   down. A toast is gone in 3 seconds; a system box is stale on arrival. →
   *live status/panels*.
3. **The output is a workspace, not a message** — triaging 20 review findings
   (keep/drop each), browsing a ledger, editing a rule bank. Scrolling
   transcript boxes can't carry selection state. → *interactive panels*.

### 6.2 The τ approach: declarative specs, not widget factories

pi hands extensions a widget factory (`ctx.ui.custom`) — maximal power, but it
couples extensions to the TUI toolkit, is untestable headless, and half of
pi's example corpus exists just to QA it. τ takes the other fork (D-E6-4):
extensions describe **what** to show/ask via plain-data specs; each frontend
renders them its own way. Headless degrades honestly (JSON records + S48
policy) instead of silently. A raw-Textual escape hatch can come later if a
real demo hits the spec ceiling.

| Step | Work |
|---|---|
| S66 | **`ctx.ui.form(spec) -> dict`** — declarative form (fields: text/select/multiselect/confirm/number), rendered as one generic `ExtensionFormScreen` (ModalScreen); headless: JSON record + `--ui-defaults`/raise per S48. Covers pi's `question`/`questionnaire` without custom components. |
| S67 | **`ctx.ui.set_status(key, text)`** — keyed slots in a one-line extension status bar (footer strip); headless: status records in the JSON stream. Covers pi `status-line`/`model-status` use cases. |
| S68 | **`ctx.ui.panel(key, spec)`** — persistent side/top panel from a spec of `{title, table|list|text, actions}`; actions dispatch back into the extension as command calls. Live-updatable (re-call with same key). The fleet dashboard primitive. |
| S69 | **`register_shortcut(key, command)`** — extension key bindings (guarded namespace, e.g. `ctrl+e` prefix chords), palette-discoverable. |
| S70 | **Runtime extension management**: `/extensions` gains enable/disable/reload actions (lifts D-E5-6 read-only), using the S41 shutdown hook for clean teardown. |

### 6.3 Worked examples (how/when/why, one per mechanism)

- **Budget** (S67): ceiling proximity is ambient state — `set_status("budget",
  "$1.42/2.00")` beats both toast spam and silence.
- **Review swarm triage** (S68 + S66): findings land in a panel table; the user
  multiselects keepers; only those become a durable findings node. The
  *decision* is the user's; the *record* is the tree's.
- **Delegate fleet** (S68): live table of children (status/turns/cost/last
  tool) with per-row abort actions — unreadable as transcript, natural as a
  panel.
- **Permission gate** (S47, already tier-1): single yes/no stays a modal
  confirm; escalating it to a panel would be ceremony.
- **CLI rule**: every S66–S68 surface MUST have a JSON-stream representation
  and a non-interactive policy; a demo that only works in the TUI is rejected
  in review. This keeps the isolated-agent atom intact — a τ child process
  running these same extensions stays orchestratable.

---

## §7 E11 — Advanced / bespoke demos

Composed showcases, each promoting one output to an input (the catalog's
"four dials"). Kept to five; each gets a design note before its step lands.

| Step | Demo | Composition | The dial it turns |
|---|---|---|---|
| S71 | `50_review_swarm.py` — `/review` fans out read-only children over the diff (security/perf/correctness lenses), dedupes, adversarially re-checks survivors, panel triage → durable findings node | `spawn.WorkerPool` + `gate` + S68 panel + `TreeStore` | none (the static baseline the others build on) |
| S72 | `51_delegate_fleet.py` — `20_delegate` v2: pool + stuck-detector + per-child budget + live dashboard + ledger | `spawn` + `stream` + `ledger` + S68 | event stream → steering (kill/re-route stuck children) |
| S73 | `52_red_team_memory.py` — review findings that survive refutation accrete in a cross-session `FileStore`; future `/review` runs seed their adversaries with the corpus | S71 + `FileStore` | backplane accretes across sessions |
| S74 | `53_router_ledger.py` — cost ledger per (task-tag, model) + `/route` report recommending reassignments; applies via `ctx.set_model` only on user confirm | `ledger` + S45 + S47 confirm | ledger → routing (human on the ratify gate) |
| S75 | `54_consequence_engine.py` — `/what-if <change>`: worktree-per-hypothesis children carry it to consequences, gates score survivors, report names what breaks | `spawn` (worktree cwd) + `gate` + S46 report | composition generated per task (bounded composer-lite) |

---

## §8 Open decisions

| ID | Decision | Recommendation |
|---|---|---|
| D-E6-1 | `send_message` durable nodes: display-only always, or `visible_to_model` opt-in? | Opt-in flag, default display-only — model-visible injection already has `before_agent_start`/`send_user_message`; don't create a third default channel. |
| D-E6-2 | Headless dialog policy | Fail-Early raise by default; explicit `--ui-defaults` to restore auto-answer (S48). The current silent auto-approve violates the standing rule. |
| D-E6-3 | Kit location | `examples/ext_kit/` first (import-path via extension dir); promote to a `tau-ext-kit` package only if third parties want it. |
| D-E6-4 | Panel model: declarative spec vs Textual widget factory | Spec-first (§6.2). Revisit only if an E11 demo hits the ceiling. |
| D-E6-5 | Tool/command namespacing on collision | Keep flat names + loud warning; add `--ext-priority` ordering only if a real collision shows up in the demo corpus. Namespacing everything is ceremony the demos don't need. |
| D-E6-6 | `turn_end` mutating vs notify-only | Mutating with append-only power (§1.3); the notify variant stays for observers. |

---

## §9 Sequence summary

```
E6  S38–S45  platform integrity + lifecycle hooks      (harness, tau-agent-core)
E7  S46–S52  visibility & interaction tier 1           (frontends + json stream)
E8  S53–S59  tau_ext_kit primitives + refactor 20/21/24 (extension-side)
E9  S60–S65  base demos (13 extensions)                 (examples/)
E10 S66–S70  rich control surfaces + runtime mgmt       (frontends)
E11 S71–S75  advanced composed demos (5 extensions)     (examples/)
```

Dependency spine: S39→S56→S62(todo)/S73; S41→S61; S42→S62(inline-bash);
S45→S57→S65/S74; S46 gates every report demo; S47/S48 gate S60; S49 gates all
E11 (children must be observable); S68 gates S71/S72. E8 can start as soon as
S39/S45 land (rest of E6/E7 parallel-safe). Every step lands under the Tier-5
gate + full suite, one commit per step, same regime as E5.

Non-goals for this arc: `registerProvider` (provider work is tau-ai's, not an
extension's), `user_bash`/PTY interception (no `!` shell feature in τ yet),
compaction-strategy hooks (`ctx.compact` + S43 cover the demo needs), theme
API, custom editors/overlays/games (fun in pi, but they demo the widget
factory we chose not to build — D-E6-4), redaction (D-E5-5, unchanged).
