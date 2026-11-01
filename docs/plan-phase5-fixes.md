# Phase 5 fix plan: the extension and frontend contracts

Status: **Stage A built and signed off 2026-10-31**; **Stage B built and signed off 2026-10-31**; **C and D run as one stage, reviewed at its end** (owner, 2026-10-31); D17 accepted 2026-10-31. Sources: `examples/speculative/REPORT.md`
(seven extensions written against D16's surface), John's review of it (rulings
below), and a read of the code behind each item. Sizes are rough estimates of
lines changed, tests excluded unless noted.

## Rulings from the review

1. **Questions belong to the cursor, not to a client.** A question asked
   with no `to` goes to every client and blocks until one answers. With
   nobody connected it simply stays open: under `tau serve` the agent waits on
   that hook until someone answers. A lost client is the same as `None`: its
   questions go into the same pool. Decline and abort mean the same thing to
   the asker: do as little as possible and give up.
2. **Cursors run the same whether anyone is watching or not.** An extension that
   branches on `cursor.client is None` is a smell. Client matching exists to
   route a question: if client A disconnects, cursor A can still ask, and if
   client B switches to cursor A it can answer.
3. **Text an extension sends to a model is a legitimate `User` message.** The
   gamemaster *is* the user prompting the character. It is no more prompt
   injection than a skill or `@file`. What was wrong was the author, not the role.
4. **A command acts on a busy cursor**, and a question asked from its body is
   the same as a question asked from a busy cursor's hook.
5. **An abort leaves nothing answerable behind.**
6. **τ owns `config.json`** (D13): the program writes its own edits.
7. **A cursor is a name on a tip of the tree.** Closing one keeps only what
   restarting needs: its name and its last node.

## Corrections to REPORT.md

- **The scratchpad was used, not neglected.** The gamemaster used items as the inbox:
  `add_item` wakes the cursor and its `pre_step` delivers the item. That is
  model.md's own sub-agent pattern (model.md:394-398). Two pieces are missing:
  - a handle cannot wait for another cursor to rest, or abort it;
  - `send` cannot say an extension wrote the text.

  Tools have the same cross-cursor rights as hooks: `TreeHandle`'s set plus
  `ask`. Neither has `rest` or `abort`; only `Client` and `Driver` have them.
- **`ask` does not use `cursor.client`.** `to` is optional. Open questions
  already reach every client inside `CursorView`, including clients that
  connect later. A question to a named client survives that client
  disconnecting.

  The one defect is `_questions.py:62`: it declines at once when nobody
  matching `to` is connected. Theme 5b of the report ("raise when nobody is
  connected") is reversed: the question stays open instead.
- **A command body already runs as the cursor's `pre_step` job**, so the cursor
  is busy and `ask` goes through the same `Questions` path as a hook's.
  Ruling 4 holds in the code. The deadlock belongs to `chat.py`.
- **`cursor.client` is derived, never stored.** It is the command's runner,
  or else the nearest `User` node a frontend wrote. Its only consumers are
  `send` and `tell`, which raise without it, and two extensions.

## Stage A: subtractions and small fixes (no new concept)

One pass: Sonnet implementers, an Opus review. Everything here is Python
except A3.

| # | fix | size | deletes or enables |
|---|---|---|---|
| A1 | `Client.run` settles when the command returns, success or raise. Delete the parked failure path (`_failed`, `settle()`, the pump call). The step still ends `Failed` with `hook_error`. Fix the three docstrings and D16 §2 that say "once the step has ended". | ~30, mostly deleted | the three timings; busy races in 5 of 7 extensions |
| A2 | `chat.py` runs a command as a task once its arguments are complete. Empty lines pass through, so an empty answer declines. All open questions are shown, not just the first. | ~15 | the deadlock when a command asks |
| A3 | A `post_step` that raises turns auto-step off, as one that returns a string does. Add a row to model.md's ending table. | 1 Rust | a crash treated more gently than a stop |
| A4 | `ask` never declines because a client is absent. Delete `_connected` and the driver wiring. A question for a client that is not connected goes into the pool at once: `to` becomes `None`, and anyone may answer. A question for a client that later disconnects goes into the pool too; the `CursorView` that clears `to` tells the other clients they may now answer. It stays in the pool if that client returns. Rewrite D16 §3's "nobody connected" lines and two tests. | ~40 net | ruling 1 |
| A5 | A question asked on an aborted step returns `None` at once. `Questions` marks the cursor in `decline` and clears the mark when the next step begins. The question's removal is already a whole-state `CursorView`. | ~25 | ruling 5: the "busy until someone answers" hole |
| A6 | `tell` with no `to` goes to every client. Delete the typist derivation (`_typist`, `_need_client`) and `client` on `Cursor` and `NodeHandle`. `TreeHandle.client` went too (review): cursors are oblivious to clients, and a command taking `(tree, cursor)` is given the one `Cursor` for both, so `tree.client` was silently `None` there. A private runner stays on `TreeHandle` for `send` until stage B. | ~30, mostly deleted | ruling 2; report theme 4 (the `pre_input` client) dissolves; forest's progress reaches everyone |
| A7 | A `list[T]` option accepts a tuple default. `commandline.value` reads a JSON array when the text starts with `[`. | ~10 | workarounds in bash_guard, plan_mode, router |
| A8 | The last `str` parameter of a command takes the rest of the line. | ~10, library | `/plan add retry`, `/search 3 what is 2+2` |
| A9 | `NodeHandle.cursor`: a tool knows its cursor id. | ~3 | harness_sense's `attach` for its own id |
| A10 | Doc bugs. In D16 §2's table, a command's `TreeHandle` is not "the cursor's handle". In model.md §7, mark `handle.sample` and "run a tool outside any step" as not built, or strike them. `show` waits for D4. | docs | the wrong path gamemaster took |

`send` needed a client until B1; it is authored by the extension now.

## Stage B: the sub-agent primitive (the one blocker)

**ADR D17** covers B1, because it changes the scratchpad file. B2 needs no ADR:
it is `Driver.rest` and `Client.abort` in handle form, which model.md already
assumes.

| # | fix | size |
|---|---|---|
| B1 | **built.** **Input is authored.** `ItemData.Input { client, text }` becomes `Input { by: Author, text }`. `send` uses the handle's `by`, so text sent from an extension is written as `User` by `Extension(name)` and needs no client. The scratchpad file's version does not change (D17: versions mark released formats); there is no migration. The log is unchanged: `Author.Extension` already exists and is already recorded. A `pre_input` halt notifies only a `Frontend` author, as now. Add a glossary entry under **input**. | ~60–100, core + `tau-py` + Python, ~15 test sites |
| B2 | **built.** **`await tree.rest(cursor) -> Ending \| None`** (`None`: not known, e.g. not stepped since a reload) and **`tree.abort(cursor)`** on `TreeHandle`, so every handle has them: tool, hook and command alike. Both refuse the handle's own cursor (waiting on it deadlocks; aborting it cancels yourself). Add a hub accessor for the last ending. `abort` is safe from a tool's worker thread. | ~50–70 |
| B3 | **built.** **Acceptance:** rewrite gamemaster and forest on B1–B2. The gamemaster becomes `send`, `rest`, read the reply. An aborted gamemaster tool is cancelled, and in a `finally` it aborts its characters; forest does the same with its fan, so abort stops spending. That is extension code, not a core rule. Expect about −40 lines across the two, and the test-side `Waits` stand-in deleted. | examples |

**Hazard, left open on purpose:** a cycle where A's tool waits for B while B
waits for A is not detected. The user's abort is the way out, and B3 shows
the abort reaches the leaves. A cycle detector would be 1000-year-flood logic.

## Stage C: cursor lifecycle and app assembly

| # | fix | size |
|---|---|---|
| C1 | **built.** *Differs from the row:* `reopen` of an open cursor raises `ValueError("cursor N is open")`, and `NoSuchCursor` stays for an id never opened (the row is silent); forest's fan closes each branch once its reply is read (gamemaster's characters are long-lived and are not closed); `Machine::moves_since_step` was deleted (only a test used it) to stay under the method limit, and `tau-core::Session` and `tau-py::Session` carry `[justified]` entries in `scripts/ratchet-allow.toml` for the reviewer to confirm. Folded in from Stage B's review: `DriverState.waiting` became `parked` and counts an effect once whatever it waits on (a question and a `rest`); a handle used outside an effect now raises instead of counting under `None`; a worker thread's `abort` raises `RuntimeError` when the loop is closed (a loop that is merely not running makes it wait; the Harness bounds a scenario, so a hang fails the test); the worker-thread abort test runs in asyncio debug mode, which refuses a loop-owned operation made off the loop, so it fails if the abort does not hop. **Closing a cursor** (reverses Q12; ADR D17). Closing appends a `Close { cursor }` record. The cursor keeps its id: ids are dense (`at.len()`), and replay checks that. Its last node stays in `at`, and its name stays in its labels, which is the restart record of ruling 7. A closed cursor leaves `cursors()` and the clients' views, and is not woken on load. Restarting is `open(at=last node)` and moving the label; `reopen(cursor)` does both (D17). `close` is: abort, wait for rest, remove the pending items, then write `Close`. Abort turns auto-step off, cancels the sample and the calls, and declines open questions; with A5 it also declines any asked later. Two things abort does not do: it does not cancel a running hook, which is why `close` waits for rest, and it does not touch items, which is why `close` removes them. Without that, they would sit in the scratchpad, addressed to a cursor that can never take them. | ~150 Rust, ~80 Python, plus I7 |
| C2 | **built.** *Differs from the row:* the refusal is `Busy`, naming the cursors that are busy or have an effect out and how many effects run (a background command has no cursor; a command waiting for its step has its cursor busy and no effect out); the show text of removed extensions is dropped; the options and commands are mutated in place (`ExtensionOptions.reload`, `Commands.use`) so the handles and calls that hold them see the new set; all or nothing; clients are told nothing, as after `use_catalog`, and `commands()` is now "fixed until the driver reloads". A tool cannot reload the driver it runs in (its own call is an effect), so harness_sense's `reload` wish stays a wish.  **`driver.reload(extensions)`**, only when no effect is out, which is the moment every job's captured set is gone. It rebuilds options, tools, owners and commands, and clears the fold cache. A `Config` node an extension can no longer read fails when it is read (`ConfigError`), not at reload, because the tree is never rewritten. Precedent: `use_catalog`. | ~60–100 |
| C3 | **built.** *Differs from the row:* what a file must export is settled: the module itself if it has a `name` (plan_mode), else the instances in its module-level `extensions = [...]` (no class scan; anything else is an error naming the file and the rule); each module is keyed by its resolved path. The six class-based speculative examples gained an `extensions` line. `examples/common.py` takes `parents` parsers so chat.py's `--extension` is in `--help` and in the one parse.  **`chat.py --extension PATH`** (repeatable). Naming the file on the command line is the consent, so no trust is needed. The trusted loader for `tau` (hash-pinned like `.tau/config.json`) stays with the trust work carried from Phase 4. | ~30, example |

## Stage D: state a frontend can see, tool shaping, config writes

These add fields or concepts. Each needs a glossary entry.

| # | fix | size |
|---|---|---|
| D1 | **τ writes its own config.** A library writer sets `extensions.<name>.<field>` in the user layer: read, validate through the options check, write atomically (as `_trust` does), then `use_catalog`. It is exposed as `tau config set`, a frontend's `/config --save`, and `cursor.save_options(**changes)`. The project layer waits for a trust rule: an edit τ writes re-records the hash. A `Config` node means this branch; the file means every session. bash_guard's "always" belongs in the file. **Built;** `save_options` is also on `NodeHandle` (bash_guard's `always` is saved from `pre_tool`, so its `post_result` and memory are deleted). `/config` spelling: `--save`, a flag anywhere after the extension. `tau config set` knows the extensions `tau config check` knows (`StepLimit`) and checks the user layer alone. A new public `UserConfig` (`LoadedConfig.user`, `Driver(user_config=)`) carries the layers. *Review fixes:* a symlinked `config.json` is written through (the link stays), a dangling one is an error naming it (`user_config_path`); `cursor.saved_options()` (also on `NodeHandle`) reads the file's own values so bash_guard's `always` adds to the file's `allow` only; `LayerText` is exported (it types `UserConfig.project`). | ~100–140 |
| D2 | **built.** *Differs from the row:* an unknown name fails the step as a contract-breaking hook does (`ValueError` naming the unknown names and the tools there are, `hook_error` of the extension); a bare `str`, or `tools` set at a point other than `pre_sample`, fails too. An offer is not enforced: a model can still call a tool it was not offered, so plan_mode keeps its `pre_tool` refusal. Every `pre_sample` answer replaces the last offer (a halt or a draft that answers the sample leaves none), so an offer never reaches a later step. `cursor.tools()` (the driver's tool names) was added in review, and plan_mode offers the read-only names it shares with it, so it works on chat.py's driver (only `word_count`, so it offers none).  A `pre_sample` hook may set `subject.tools` (names; `None` means all). | ~10 + glossary |
| D3 | **built.** *Differs from the row:* `cursor.model()` and `cursor.models()` are on `Cursor` only (not `NodeHandle`). `CursorView.model` is `None` when no `Config` on the branch names a model or one is unreadable, so a malformed node cannot kill the driver in the middle of a publish; the sample and `cursor.model()` raise `ConfigError` with why. The fold now carries the model beside the options. router's two copies are gone (`model_on` stays for its skip-own-nodes use); harness_sense's `whereami` reads the view.  `CursorView.model`, `cursor.model()` (the fold the driver already computes) and `cursor.models()` (the catalog's names). | ~30 |
| D4 | **built.** *Differs from the row:* `cursor.show(text)` / `show(None)`, on `Cursor` only; `CursorView.shown` is `(extension, text)` pairs in name order. A client that has not read the last `CursorView` never sees a `show` that was replaced in between (state, not a message). a `show` from a sync command's worker thread hops onto the loop like `abort`; plan_mode shows "plan mode" at both ends of a step, ralph "iteration N of M" (its per-iteration `tell` is gone), forest the round (its `progress` parameter is gone); chat.py prints changes as `[extension: text]`.  **`show`**: one unpersisted string per extension per cursor, in `CursorView`, seen by every client. model.md already promises it; plan_mode, ralph and forest each wanted exactly this. | ~50 + glossary |

Left alone: question records and grouping (plan_mode's two questions in a row),
`StepLimit` counting steps rather than iterations, `/config` refused while a
loop runs, and `hook_error` being cleared when the next step starts.

## Decisions for John

1. ~~A named client is absent~~ **Decided 2026-10-31:** a lost client is the same
   as `None`. The question goes into the pool and blocks until someone
   answers. Disconnecting is not declining (A4).
2. ~~Pending items on close~~ **Decided 2026-10-31:** close aborts first (C1).
3. ~~`show`~~ **Decided 2026-10-31:** build it (D4).
4. ~~Config writes~~ **Decided 2026-10-31:** τ writes its own config: the
   library writer, `tau config set`, `/config --save` and `save_options` (D1).

## Order

Stage A, then B (ADR D17 first, written with C1, since both are scratchpad
or log changes), then C and D in whatever order the decisions allow. Each
stage ends with the gate and a stop for review.
