# harness_sense: friction log

Files: `examples/speculative/harness_sense.py` (383 lines, of which about 60 are the helper
`gist`/`describe_extension` and WISH comments), `tests/examples/speculative/test_harness_sense.py`
(15 tests, passing). Written against HEAD ab490dd, from the position of the model that has to
live in the harness.

## Does

Seven tools with the names a model would guess, none needing documentation to call:
`whereami()` (cursor, node, depth, labels, siblings, model, other cursors, each extension with its
source file, tools, hook points and current options, tools offered, notes), `tree(depth)` (the
branch around you, from the last branch point, with `<- you` and `@cursor` marks), `context_budget()`
(reported and estimated tokens, window if configured, biggest nodes, truncated tool outputs with
the file holding the whole), `hook_errors()` (halts, hook failures with the hook named, calls an
extension refused), `note(text)` (a side node, shown by `whereami` on that branch),
`probe(prompt, cursor=None)` (a scratch cursor, one step, the reply, tools refused and reported),
`check_extension(path)` (load an edited file and build a throwaway driver on it). `reload` is a
WISH (lines 375-383).

## Easy

- **Walk, tree, own node: all there** (lines 156-190, 201-233). `node.walk()`, `node.tree.children`,
  `node.node`, `node.cursors()`, `node.cursor(c)` are enough for `whereami`, `tree` and
  `context_budget` without the driver. `tree` is 30 lines including finding the branch point.
- **Plain functions and bound methods are tools** (lines 112-120): `self.tools = [self.whereami,
  ...]`, docstrings are the descriptions, `Annotated[int, "levels above and below"]` is the
  parameter help, `NodeHandle` is injected. A model can call all seven without a doc page.
- **Notes as side nodes** (`note`, line 310; `_side`, line 192): `model.md` §3 already names
  "an extension's notes" as side nodes, `node.append` writes one without moving anything, and they
  are branch-scoped, persisted (I7), and visible in `Appended` and `tree`. Nine lines, no
  scratchpad item (an item would have woken the cursor and never reached the model).
- **`context_budget` has real data**: the provider already normalizes `usage` into
  `Assistant.meta` (`{"usage": {"input", "output", "cached"}}`) and the base tools already put
  a `[truncated: ... is in <path>]` note in the output, so "what is this costing and where is the
  rest" is a read of the walk (lines 237-275).
- **A throwaway driver is a validator** (`check_extension`, lines 354-373): `Driver(session, {},
  catalog, extensions=[module])` raises `DefinitionError` naming the extension, function and
  parameter, and needs no provider. "Does my edit load?" is eight lines and the errors are already
  in the language a model needs ("no docstring to describe it").
- **Failure history written as a side node** (`post_step`, lines 279-291): `Ending.Failed/Halted`
  plus a snapshot gives the hook that failed; two lines make it durable and branch-scoped.
- **`probe` is built from public calls** (lines 322-350): `client.open`, `input`, `step`,
  `driver.rest`, `abort`, `walk`; `pre_tool` (316-320) refuses tools on a probe cursor, so a probe
  is a safe dry run. The label `probe-1` and a busy cursor 1 reach every frontend with no code.

## Fought

### (a) Expressing the behaviour

1. **The extension needs the driver** → an extension is built *before* the driver that holds it;
   handles expose the tree, cursors, labels and options but not the driver, the extension list,
   the tool list, the catalog or a client. → `attach(driver, extensions, tools)` (line 122), which
   the app must call, passing the same lists the driver was built from; fails loud if forgotten
   (`an_unattached_sense_says_so`). → Approximation, WISH at line 126. **Severity: awkward**
   (a footgun by construction, and the app duplicates its own wiring). Answers to "can a tool
   see ...": walk yes, its node's id yes, other cursors' `CursorState` yes; catalog no, extension
   list no, tool list no, its own cursor no.
2. **Which cursor am I?** → `NodeHandle` has `id` but no cursor; `ask` knows it privately.
   → busy cursors whose `at` is the batch (line 149), ambiguous when two cursors share a batch
   (Q13) and re-done in `pre_tool` (line 318). **Severity: awkward** for something the driver holds
   in a `partial`.
3. **What model, how big a window?** → `Config` nodes are readable (`json.loads(config.data)`), but
   the fold is private (`model_name`), the catalog is the driver's, and `ModelConfig` has no
   context window at all. → Fold the walk myself (lines 165-167, duplicating core), and make the
   window an option the person sets (`/config harness_sense context_window=N`), reporting "window
   unknown" otherwise instead of guessing a default. → Open, WISH comment at line 59.
   **Severity: awkward.**
4. **Truncated outputs are prose** → `Outcome.Ok(value)` has no metadata, so the tool-output cut
   is found by regex over the text (`TRUNCATED`). → Works for the base tools' wording only.
   **Severity: cosmetic** now, brittle if the note changes.
5. **Reload without restarting** → blocker. `Driver` fixes extensions, tools and commands at
   construction (`check_extensions`, `assemble_tools`, `assemble_commands` once); there is no
   loader either, and an extension in a file is a module with a `name` (a class-based extension has
   no way to be found in its file). → `check_extension` validates and describes, and says "not
   loaded"; the edit-validate half of the loop, none of the swap. → Gap left open, WISH sketch at
   375. **Severity: blocker** for the question as posed ("hot-reload into the running driver").
6. **Try the modified harness on a scratch branch** → `model.md` §7 says a handle can "sample a
   walk or run a tool outside any step"; `handles.py` has neither, so a probe cannot just ask the
   model. → A whole cursor: `client.open`, `input`, `step`. A handle *can* start it (`open`, `send`)
   but only the driver can wait for it (`Driver.rest`); that is why probe needs `attach`.
   → Implemented. **Severity: awkward.** Gotcha found on the way: `CursorId` can be a tool
   parameter, but its default `"new"` is refused and `"new"` arrives as an int
   indistinguishable from a cursor the model named (it could name its own!), so `probe` takes
   `int | None` and checks the id against the probes it opened (lines 327-328).
7. **Probes must not act** → every cursor has the same tools. → `pre_tool` refuses on cursors in
   `self._probes` (lines 316-320) and the reply's calls are reported. It works only because
   `pre_tool` runs *every* hook: a permissions extension listed alongside still asks the person
   about a call that is refused anyway. **Severity: awkward**, and the same missing
   per-cursor tool filter as `plan_mode` wish 2.
8. **`probe` gets one step**, so the model's answer is its first reply or its first tool calls
   (reported, never run). A multi-step probe ("does it finish the task?") has no honest form
   without auto-step and real tools. **Severity: cosmetic** (a limit, not a bug).
9. **Source files** → `inspect.getsourcefile(type(ext))` goes through `sys.modules`; a file loaded
   by `spec_from_file_location` without registering the module (as a future extension loader
   might, and as `tests/examples/test_chat.py` does) raises `TypeError`. **Severity: cosmetic**;
   a loader should register, or the extension should be told its path.

### (b) Showing state to the user

10. **`CursorView.hook_error` and `ending` are `None` from the start of the next step**, and a model
    can only ask in a step. → `post_step` writes the failure as a side node (line 279). A halt
    caused by the same hook shows only `Ending.Halted(reason)`. The tool-hook failure story is
    different again: it is a `Results` `Err` authored by the extension, no hook point.
    **Severity: awkward** (three places for "what stopped me": the view, the ending, the
    outcome). Fine as an extension fix, so see Wish 2.
11. **Handles see `CursorState`, clients see `CursorView`**: questions, `ending`, `hook_error` and
    pending-question counts are on the view only. → `whereami` and `post_step` open a transient
    client for a snapshot (lines 138-148); the stream's snapshot order (a `CursorView` per cursor,
    then `Labels`) made that safe to read in 10 lines. **Severity: awkward** (a hook opening a
    client to read the world is a layering inversion; also see 14).
12. **The human cannot see what the model was told.** `whereami` and friends answer through a
    `Results` node; the chat client draws `[calling probe]` and the reply text but no tool results,
    no `Labels`, no other cursors (asserted in the last test). A person and a model each need the
    same facts, and there are now two renderings of them. **Severity: awkward.**

### (c) Input from the user

13. None of these tools asks anything. The interplay that matters: **a client held open inside a
    tool counts as "someone is connected"** for the "ask anyone declines at once when nobody is
    connected" rule. With the app headless, the main cursor's own question is declined, but while
    a probe runs its `harness_sense:probe` client is connected, so the probe cursor's questions
    stay open for an answer only a person could give: three of them (its wake, the extra wake its
    input causes, its step), and `probe` waits until `probe_timeout`
    (`a_probes_own_client_makes_a_headless_question_wait_for_nobody`). → Not fixed.
    **Severity: awkward** (a quiet deadlock; the timeout, line 338, is what bounds it).

### (d) Timing and concurrency

14. **One cursor, three steps for a probe**: opening wakes it (a hook-only step), the arriving input
    is "news" and wakes it again, and `step` is the third. `driver.rest(cursor)` between the
    input and the `step` call (line 335) is what makes the ordering safe. Every `pre_step` hook ran
    three times on the probe cursor (`Nosy` asked three times). **Severity: awkward** (correct per
    `model.md` §6, surprising when a hook is expensive).
15. **`Harness.settle()` cannot settle on a tool that awaits another cursor**: quiet means every
    unit is parked at a gate or question, and the waiting tool is a unit that is neither. The
    headless test has to spin on `open_questions` by hand. **Severity: cosmetic** (test
    infrastructure), and a sign: no `Driver` call says "this unit is waiting on cursor N".
16. **Timeouts and `asyncio.timeout` are real time** (line 338); `a_probe_that_gets_no_answer_...`
    needs a real `sleep(0.15)`. The FakeClock cannot drive it. **Severity: cosmetic.**
17. **Abort while a probe runs**: the main cursor's abort cancels the tool call; the `async with`
    closes the client; the probe cursor itself is left running unattended (nothing aborts it).
    Not tested. **Severity: awkward** (an orphaned scratch cursor with auto-step off, so it rests
    quickly, but it keeps its label and any open question).

## Frontend view

**Chat line client today**: a call shows `[calling whereami]` and the model's later text. The
tool's answer, the `probe-1` label, cursor 1 going busy, notes (Appended `Custom` nodes) and the
side-node stop records are all invisible; the client watches one cursor and draws only deltas,
notices, sample errors and hook errors.

**A TUI or web frontend** should render, with no new events:
- tool results (`Appended` `Results` nodes) as collapsible blocks, so a person can read what
  `whereami` told the model;
- other cursors from the `CursorView`s it already receives (probe cursor busy, `Labels` names it),
  as tabs or a list with a busy marker;
- `Custom` nodes by `kind` (`harness_sense.note`, `harness_sense.stopped`) as margin notes in the
  tree view;
- the `CursorView.questions` of *any* cursor, since a probe's question can arrive from a cursor
  the person is not watching (D16 already sends every cursor's state).
What it cannot render today: the extensions with their files and tools offered, and the current
options of every extension. The client protocol has `commands()` and `options(cursor, ext)` but
no read for the tools or the extensions. It reconstructs nothing else.

## Since stage D

`whereami` reads the model from the cursor's `CursorView` (`CursorView.model`) instead of folding
the `Config` nodes itself. `Driver.reload` exists, but only when no effect is out and a tool call is
an effect, so the `reload` tool in the last WISH still cannot be written inside the driver it would
reload; the app that owns the driver reloads between steps.

## Wish

Smallest first.

1. **`NodeHandle.cursor` (the batch's cursor id).** Deletes `_mine` (lines 149-152) and the guess in
   `pre_tool` (line 318); `ask` already needs it.
2. **Leave a failure's record to an extension, which is what we did.** `CursorView.hook_error`
   being `None` from the next step's start is right for a banner. The honest answer to friction 10
   is "an extension that cares writes a side node", as `post_step` does (lines 279-291). Core
   writes a `sample_error` side node already; if hook failures were one too (`Custom{kind:
   "hook_error"}`), `post_step` and its snapshot (11 lines) would not exist. Prefer the extension
   answer until a second extension wants the record.
3. **`Outcome.Ok(value, meta=None)`** or a structured `truncated` marker on tool output. Deletes the
   `TRUNCATED` regex and makes "where is the full output" a field a TUI can link.
4. **`ModelConfig.context_window`** (in the model config file, read by `read_models`). Deletes the
   option at line 59 and the "window unknown" branch; a frontend can show a fill gauge too.
5. **A read-only description of the harness, once**: `Driver.describe()` / `Client.harness()` /
   `NodeHandle.harness()` returning a frozen value: extensions (name, source file, tools,
   commands, hook points, options), tools offered, models. One value for the model (`whereami`),
   the person (a `/harness` panel), and `tau agents` over a network. Deletes `attach`, the
   duplicated lists, `describe_extension`, and lets `RemoteClient` offer it for free.
6. **`handle.sample(walk, prompt) -> reply`** as `model.md` §7 already promises. Makes `probe` a
   pure read that opens no cursor, runs no hook three times, holds no client, and cannot reach a
   tool. The probe's `pre_tool` refusal (316-320), the label, `_probes`, the three-step ordering
   and friction 13 and 17 all disappear. Keep the cursor-based probe only if someone wants a probe
   that can run tools.
7. **Per-branch tool filter** (the same wish as `plan_mode` 2: `pre_sample` may set
   `subject.tools`). A tool-running probe could then be told to use `read` and `grep` only.
8. **`Driver.replace_extension(name, new)`** at the next hook job: refused while a hook of the old
   one is out; `DefinitionError` (nothing swapped) as at build; tools and commands diffed; a
   cursor's options carried over. This is the whole of `reload`, and it is also the only blocker
   in this file. Cost: one more thing a `Driver` can change after it is built, next to
   `use_catalog`. Without it, the honest answer is "restart the driver", with `check_extension`
   to find the mistakes first.
9. **An extension loader that registers the module and records the path** (needed by trust, by
   `reload`, and by `whereami`'s source file). Not core: a function in `tau`, used by the
   program that builds the driver.
10. **A way to write the model's instructions**: a model learns these tools exist only from their
    names and docstrings; `whereami`'s "Call this first" is the only onboarding channel. An
    extension-contributed system-prompt fragment would let `harness_sense` say "you have a
    harness; here is how to ask about it" once. Today a system prompt is a `System` node the app
    wrote. Leave to an extension if the prompt is composed by the program, not by core.
