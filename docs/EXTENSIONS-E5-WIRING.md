# E5 — extension wiring, the durable-hook invariant, and the live surface

> **Status: PLAN (2026-07-04). Design only — implementation and test-procedure
> execution deferred by the maintainer.** This is the buildable spec for the
> milestone that makes extensions *actually reach a running process* (CLI + TUI)
> and reworks the mutating-hook model onto τ's tree-as-truth architecture. It
> builds directly on the landed E0–E4 chain (`docs/EXTENSIONS-IMPLEMENTATION.md`,
> commits S1–S23) and the post-run **S24** bridge (`api.on` → `ExtensionRunner`).
> pi (`~/Development/pi`) is the source of truth for API *shape*; **E5 makes one
> deliberate, documented divergence from pi** — it removes pi's per-call `context`
> transform in favour of durable node edits (§1), justified by τ's tree-centric
> design (CLAUDE.md sanctions intentional divergence). Evidence is cited
> `file:line` against the current tree.

---

## 0. Reconciliation — the orphan chain (what blocks interactive use)

E0–E4 built every extension mechanism; S24 fixed the last internal seam
(`api.on` → runner). But **nothing loads an extension into a real process**, and
the mechanism surfaces are not connected to the CLI/TUI. Verified 2026-07-04:

| Seam | State (cited) | Consequence |
|---|---|---|
| `_load_extensions` (file-path loader) | complete, but **called only by tests** (`sdk.py:297`) | `-e` / discovery never runs in a live process |
| `create_agent_session(extensions=…)` | accepts **inline factories only**; never calls the loader (`sdk.py:520-538`) | SDK path cannot load a file |
| TUI `TauBackend.__init__` | builds `AgentSession(…)` with **no `extensions=`** (`backends.py:205`) | **the TUI loads zero extensions** |
| headless `resolve_model_config` | stashes `model_config["extensions"]` — **consumed by nobody** (`headless.py:129-132`) | `tau -p -e demo.py` is a no-op |
| threaded flags `-xt` / `-nbt` / `--append-system-prompt` | parsed + staged into `model_config` (S2) — **consumed by nobody** | tool-filter / prompt flags inert |
| registry `_commands` (`register_command`) | stored; **read by nobody** (`registry.py:99`) | extension slash-commands invisible |
| TUI palette `get_system_commands` | hardcoded models/clear/compact/tree; **never reads the registry** (`app.py:1406`) | no way to see or run extensions |
| `LoadExtensionsResult.errors` | populated; **surfaced nowhere** | a broken extension fails silently |
| `api.notify` / `ExtensionUI` | exists, gated on a `_tui_delegate` (`extension_types.py:26-86`) — **verify it is ever set** | demos' `api.notify(...)` likely dead in the TUI |

**Already conformant to the E5 hook model (§1) — do not touch:**
- `tool_result`: the patched result is applied *before* `all_results.append(result)`
  (`agent_loop.py:687-700`), and that appended result is the persisted tree node,
  the wire payload, and the TUI render — **one artifact**. This is the template.
- `tool_call` veto: a block becomes an `is_error` `toolResult` node
  (`agent_loop.py:240-254`) the model reacts to like a non-zero exit — already a
  real node on the active path.

---

## 1. Architecture decision — the durable-hook invariant (the centrepiece)

**Invariant.** For any LLM call, the model's input =
**(the system prompt, re-attached per call) + (the exact linear active path
through the session tree).** Nothing else. An extension may influence what the
model sees *only* by (a) editing/appending **durable tree nodes**, or (b)
contributing to the **system prompt** (already a per-call frame, attached
separately at `agent_loop.py:416`, after any hook, so it can't be clobbered).
There is **no ephemeral, out-of-band message channel.**

**The one rule for mutating hooks.** *A mutating hook's output is a durable
edit/append to the active path. The path is the single artifact — persisted,
rendered, and sent. There is no separate copy.* Multiple extensions chain
(handler 2 sees handler 1's edit — already the runner's behaviour,
`runner.ts` parity, `runner.py:200-320`).

**Why (honesty + reload fidelity).** A tree-as-truth system where "what the model
saw" can silently diverge from "the path shown in the interface" is a
reasoning/debugging hazard and forks history on reload (two histories). Under the
rule the TUI node, the on-disk node, and the wire bytes are the *same object*, so
they cannot diverge; reload replays the exact bytes the model saw (edits are baked
in, not recomputed — *more* deterministic, not less). The maintainer's framing
derives the split: **the TUI is an interface to examine and modify the session
tree; extensions are automation of that same concept** — so an extension action is
either a tree mutation (durable, visible, reloadable) or a system-prompt frame,
never a hidden third thing. (Recorded as the project memory
`tree-as-truth-model-input-invariant`.)

### 1.1 The hooks measured against the rule

| Hook | Today | Under the rule (E5) |
|---|---|---|
| `tool_result` | patched result **is** persisted+sent+shown (`agent_loop.py:687`) | ✅ already correct — the template |
| `tool_call` veto | becomes an `is_error` `toolResult` node (`agent_loop.py:240-254`) | ✅ already a real node (arg-patch likewise reflected in the executed+persisted call) |
| `before_agent_start` `message` | reaches the model, **not persisted** (`agent_session.py:419-421`) | ❌ → **persist as tree node(s)** (§3.1) — closes the reload fork |
| `context` | ephemeral list-replace on a **deep copy**, never persisted (`agent_loop.py:412`) | ❌ → **eliminated** (§3.2); its cases fold into durable `tool_result` edits + `before_agent_start` |

### 1.2 Why `context` can be eliminated (not merely redefined)

`context` fires *before every LLM round-trip* — the loop re-enters
`_stream_response` for each provider call (`agent_loop.py:197,309` → the hook at
`:412`), so within one `prompt()` a `model→tools→model→…` run fires it several
times. But every such round-trip is preceded by a **real node**: the first call by
`before_agent_start` (per prompt), every subsequent call by the tool results of the
previous round. So:

- **Reminders** ("after 2 failures, nudge") → edit the *triggering* `tool_result`
  content in place (durable; already the mechanism), and/or a `before_agent_start`
  durable message for the pre-first-call case.
- **Budget** ("over threshold → warn, then abort") → append a durable warning node
  (or edit the last node) before `ctx.abort()`.

The *only* capability lost is transforming the **whole history differently per
send** (e.g. per-call global redaction) — which is exactly the hidden divergence
E5 deletes. Redaction (model sees *less* than the tree) is the sole legitimate
message-list mutation; it is **not needed now** and is explicitly deferred as a
separate, opt-in feature rather than smuggled in via a general hook.

*(A thin per-round-trip "append durable node(s)" ergonomic hook was considered and
rejected for E5 — the rule stays absolute; revisit only if demo authorship proves
painful.)*

---

## 2. E5.1 — the wiring spine (loader → session → runner, both paths)

**Goal:** `-e demo.py` and `~/.tau/extensions` discovery load real extensions
whose hooks fire, on **both** the headless (`tau -p`) and TUI paths, in one
milestone (maintainer decision: both paths together — they share the
`AgentSession(extensions=…)` seam).

### 2.1 Split load from bind (resolves the chicken-and-egg)

The session's `ExtensionRunner` is created inside `AgentSession.__init__`
(`agent_session.py:159`); the S24 per-extension bucket loop already binds factories
correctly. So:

- The **loader** does discovery + `importlib` + validates a module-level
  `register`, and returns the **uninvoked** `register` callables paired with their
  resolved file paths (catching *import* errors → `LoadExtensionsResult.errors`).
  It no longer invokes `register` itself (drop the `api_factory` invocation path).
- The **session** invokes each `register` through the S24 bucket loop, labelling
  each bucket by the extension's **file path** (not `module:qualname`), so
  `has_handlers`/error attribution read the real source.
- **Error policy (pi-faithful, Fail-Early):** an *explicit* `-e` **import** failure
  raises (the user named it); a *discovered* import failure → `errors[]` + stderr,
  skip. *Invoke* (`register`) failures surface at session construction; the
  explicit-vs-discovered origin is threaded so an explicit `-e` invoke failure also
  raises (or is documented if origin cannot be threaded cleanly).

### 2.2 Wire both run paths

- **headless** (`headless.py`): from the staged `model_config["extensions"]` +
  discovery (respecting `-ne`), call the loader and pass the results into the
  session builder; print `LoadExtensionsResult.errors` to stderr.
- **TUI** (`backends.py`, `app.py`, `cli.py`): `TauBackend` accepts `extensions=`
  and forwards them to its `AgentSession`; the app loads at startup from the same
  CLI flags and hands them in; surface load errors as a startup notice (and in the
  `/extensions` palette, §5).
- **SDK** (`create_agent_session`): optionally accept `extension_paths=` and run
  the loader, so the file-path path is not TUI/headless-only.

### 2.3 Consume the other threaded flags (all CLI/TUI use cases)

Close the S2 "threaded but consumed by nobody" gaps: `-xt/--exclude-tools` filters
the resolved tool list; `-nbt/--no-builtin-tools` drops built-ins (extension tools
survive once E1 tool-merge is on the path); `--append-system-prompt` appends to the
turn system prompt; verify `--no-session` on both paths.

---

## 3. E5.2 — the durable-hook rework

### 3.1 `before_agent_start` messages become durable nodes
Persist injected messages as tree nodes carrying an **extension-origin role/type**
so the TUI renders them as extension-injected (not a literal user message), while
the wire serializes them to an LLM-acceptable role (pi's `custom`→`user`,
`messages.ts`). This closes the reload fork (`agent_session.py:419-421`) and gives
the maintainer's "a message type the LLM accepts that also appears in the
transcript." System-prompt chaining is unchanged (per-turn frame).

### 3.2 Eliminate the `context` hook
Remove the dispatch call-site (`agent_loop.py:412`), drop `"context"` from
`ExtensionRunner.HOOK_EVENTS` and its `emit_context` (`runner.py:298-320`), and
update the S24 `api.on` bridge so `api.on("context", …)` is an unknown-hook
**raise** (Fail-Early), not a silent bind. Retire `test_context_hook.py`. Audit
`23_context_surgeon` (uses `ctx` ops, not the `context` hook — expected clean).

### 3.3 Rework the two demos onto durable edits
- **`21_reminders.py`**: inject the `<system-reminder>` by editing the triggering
  `tool_result` content in place (durable) + `before_agent_start` for the
  pre-first-call rule; keep the cooldown/state logic (state is in-memory, resets on
  reload — acceptable; already-injected reminders persist in the nodes).
- **`24_budget.py`**: threshold trip appends a durable warning node (or edits the
  last node) then `ctx.abort()` (abort unchanged).

### 3.4 Guard: extension write-access to the tree
The rule gives extensions write access to real nodes — consistent with "extensions
automate modifying the tree," but scope it to **append + edit the node the hook is
about**; disallow arbitrary deletion/rewrite of prior path nodes.

---

## 4. E5.3 — visibility

- Wire `ExtensionUI._tui_delegate` so `api.notify(...)` paints in the TUI (confirm
  `set_ui_delegate` is actually called; wire it in `TauBackend`/`app`). Headless
  `notify` → stderr / a `--mode json` event.
- Cosmetic only: render `tool_call` vetoes as a visibly-blocked line (they are
  already `is_error` nodes — no new channel).

*(No "inspectable ephemeral frame" work — superseded by §1; there is no ephemeral
channel to inspect. The transcript already is the model input.)*

---

## 5. E5.4 — the command-palette surface

- **`/extensions`** command + palette entries reading the now-populated registry +
  `LoadExtensionsResult`: per extension its name, path, and registered
  tools/commands/hooks, plus any load errors. Read-only first (runtime
  enable/reload deferred).
- Surface `register_command` entries into `get_system_commands` (`app.py:1406`) and
  the slash dispatch (`app.py:1137-1150`) — the second orphan — so
  extension-registered commands are listed and runnable.

---

## 6. E5.5 — test strategy (execution deferred)

Documented now; **written/run later** per the maintainer.

- **Automated floor:** headless subprocess smoke (`tau -p -e <demo>.py` against the
  in-repo fake provider, asserting the hook's *durable node* appears in the emitted
  transcript/JSON); Textual `Pilot` (`run_test`) for the TUI load → `/extensions`
  listing → a veto rendering.
- **Manual live procedures (per demo):** e.g. gatekeeper — `tau -e
  examples/22_gatekeeper.py`, attempt a write outside `.tau/scope.txt` → see the
  blocked `is_error` node; reminders — drive two tool failures → see the reminder
  *appended to the failing tool_result* in the transcript; budget — watch running
  cost climb → durable warning node then abort; delegate — spawn a child.
- **Reload check (the invariant's proof):** run a demo that injects, reload the
  session, assert the model's context is byte-identical to the persisted path (no
  second history).

---

## 7. Resolved decisions

- **D-E5-1 — both paths in one milestone** (headless + TUI share the
  `AgentSession(extensions=…)` seam).
- **D-E5-2 — durable-hook invariant** (§1): model input = system prompt + exact
  tree path; hooks edit/append durable nodes; no ephemeral channel.
- **D-E5-3 — eliminate `context`** (not redefine); its cases fold into durable
  `tool_result` edits + `before_agent_start`. Intentional pi divergence.
- **D-E5-4 — `before_agent_start` messages persist** as extension-origin tree nodes.
- **D-E5-5 — redaction deferred** as a separate opt-in feature, never a hidden hook.
- **D-E5-6 — palette read-only first**; runtime enable/reload later.
- **D-E5-7 — load split from bind**: loader returns uninvoked `register` callables;
  the session's S24 bucket loop invokes them, bucket-labelled by file path.

---

## 8. Directly executable step plan (implementation deferred)

Continues the E0–E4 numbering (S1–S23 landed; **S24** = the `api.on`→runner bridge,
landed). Each step is one green-gated commit (ruff + ruff-format + mypy + `pytest`).
"Files" names the primary targets; "Verify" is the proving test. **Do not build yet
— this is the approved map for a later implement→review→fix pass.**

### E5.1 — wiring spine
- **S25 — loader returns uninvoked `register` callables.** Refactor
  `_load_extensions` to import + validate `register` + collect *import* errors, and
  return `[(path, register)]` + `errors` **without invoking**. Explicit-`-e` import
  failure raises; discovered → `errors[]`. Files: `sdk.py`. *Verify:* callables
  returned un-invoked; error policy per origin.
- **S26 — session binds file-path extensions.** Thread `(path, register)` into
  `AgentSession` so the S24 bucket loop invokes them, bucket-labelled by file path;
  invoke-error origin handling. Files: `agent_session.py`, `sdk.py`. *Verify:* a
  file-path extension's hook fires through a real fake-provider loop; bucket labelled
  by path.
- **S27 — wire both run paths + surface errors.** headless + TUI (`TauBackend`,
  `app`, `cli`) load from CLI flags (`-e`, discovery, `-ne`) and pass to the session;
  print/annunciate `LoadExtensionsResult.errors`. Files: `sdk.py`, `headless.py`,
  `backends.py`, `app.py`, `cli.py`. *Verify:* `tau -p -e demo.py` fires the demo's
  hook; TUI loads a discovered extension; `-ne` suppresses discovery, keeps `-e`.
- **S28 — consume threaded tool/prompt flags.** `-xt` exclude-tools filter, `-nbt`,
  `--append-system-prompt`, verify `--no-session` both paths. Files: `headless.py`,
  `backends.py`/`app.py`, `sdk.py`. *Verify:* excluded tool absent; appended prompt
  present; no-session writes no file.

### E5.2 — durable-hook rework
- **S29 — `before_agent_start` messages durable.** Persist as extension-origin tree
  nodes; TUI renders distinctly; wire serializes to an accepted role. Files:
  `agent_session.py`, node/type defs, `session_store.py`, render in `backends.py`/
  `app.py`. *Verify:* injected message in tree + transcript + survives reload; wire
  role accepted.
- **S30 — eliminate `context`.** Remove the call-site (`agent_loop.py:412`), drop
  `"context"` from `HOOK_EVENTS` + `emit_context`, make `api.on("context")` raise,
  retire `test_context_hook.py`. Files: `agent_loop.py`, `extensions/runner.py`,
  `extension_types.py`, tests. *Verify:* no context dispatch remains; unknown-hook
  raises.
- **S31 — reminders demo → durable.** Inject via `tool_result` content edit +
  `before_agent_start`; keep cooldowns. Files: `examples/21_reminders.py` + test.
  *Verify:* reminder is durable content on the triggering node, in tree+transcript.
- **S32 — budget demo → durable.** Durable warning node/edit before `ctx.abort()`.
  Files: `examples/24_budget.py` + test. *Verify:* warning durable + abort.

### E5.3 — visibility
- **S33 — `api.notify` + veto render.** Wire `ExtensionUI._tui_delegate`; headless
  notify → stderr/json; distinct veto line. Files: `extension_types.py`,
  `backends.py`, `app.py`, `headless.py`. *Verify:* notify shows in TUI; headless
  notify surfaces.

### E5.4 — palette
- **S34 — `/extensions` listing.** Command + palette entries from the registry +
  `LoadExtensionsResult` (name/path/tools/commands/hooks/errors). Files: `app.py`,
  a small extensions-info accessor. *Verify:* lists loaded + errors.
- **S35 — surface `register_command`.** Extension commands appear in
  `get_system_commands` and dispatch. Files: `app.py`, registry access. *Verify:* an
  extension-registered command runs from the palette.

### E5.5 — tests / procedures (deferred execution)
- **S36 — automated floor.** headless subprocess smoke + Textual `Pilot` +
  reload-invariant check. *(Write later.)*
- **S37 — live-procedures doc.** Per-demo manual checklist. *(Write later.)*

**Fast path:** S25–S28 (spine) · S29–S32 (durable hooks) · S33 (visibility) ·
S34–S35 (palette) · S36–S37 (tests, deferred). S26 is the keystone — nothing loads
into a live process until the session binds file-path extensions.
