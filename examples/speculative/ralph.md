# ralph: friction log

Files: `examples/speculative/ralph.py` (96 lines, about 25 of them the loop),
`tests/examples/speculative/test_ralph.py` (12 tests, all green).

## Does

`/ralph PROMPT.md [root]` writes the file's text as a `User` node under `root` (default: where the
cursor is, e.g. the `System` node) and turns auto-step on. Each time the model finishes (a reply
that asks for no tools), `post_step` moves the cursor back to `root` and writes the file again,
re-read, so the model can rewrite its own prompt. Each iteration is a new branch from `root` with a
fresh context; all state that matters is in files the model edits (the test has it write `DONE`
and a new `PROMPT.md` through the real `write` tool). It stops when `DONE` exists beside the prompt,
at `max_iterations` (an option), or when anything else ends the run (abort, failed step, a client
or `StepLimit` turning auto-step off). It says so with a `Notice` per iteration and at the stop.

## Easy

- **A hook can move the cursor between steps, and it just works** (L63-66, L90): `cursor.move(root)`
  then `cursor.write(Body.User(text))` in `post_step`. The write is one record (node + move),
  synced before the step restarts, and the next step starts at once.
- **"Cursor never spins" does not fight a loop that returns to the same node.** The rule asks
  whether the step moved the cursor: every iteration commits a reply and then a new `User` node,
  so both are move records; with auto-step on and `next == Sample`, `Done` plus work plus moved is
  "start the next step". Test `..._never_spins...`: six iterations, auto-step on and the cursor busy
  throughout, six `User` children of one root. Each iteration is a fresh branch, not a revisit.
  What would fight it is a restart that only moves to a node where `next == Wait` (an `Assistant`
  reply): no work, so it rests quietly. A move to a `User` node would work too (re-sampling a
  sibling), but then the file is not re-read.
- **Iteration count is in the tree**: the `User` children of `root` authored by `Extension("ralph")`
  (test `..._frontend_counts_iterations...`). The loop's memory (L49) is only `root`, the path, the
  starting client and a counter; a reload empties it, which is wanted.
- **Stop conditions are three short checks** (L73-87): `subject.ending` for abort, failure and
  halt; `state.auto` for outside stops; the `DONE` file and `max_iterations` for the loop's own.
- **Options and types**: `Options` (L26-33) gives `/config ralph max_iterations=3`;
  `root: NodeId | None` gives a `[root]` argument that completes to the cursor's walk (test
  `..._command_line_is_a_path_and_an_optional_node`); `prompt: Path` completes files.
- **Crash and reload**: auto-step is off after a load, so a reloaded session does not loop; a stale
  loop entry is dropped by the wake step's `post_step` (test `..._reload_does_not_loop...`).

## Fought

### (a) Expressing the behaviour

1. **"The model finished" has no name.** The iteration boundary is "this step ended `Done` and
   `next == Wait`" (L79); core decides rest after `post_step`, so a hook cannot ask "will you rest?",
   only compute core's own rule. Works; one line; it also fires for `Length` (a truncated reply
   starts a fresh iteration, which is what Ralph wants). **Cosmetic**.
2. **The handle forgets who typed.** A handle's client is the nearest frontend `User` node on the
   walk (D16 fix pass B). Back at a fixed root the walk has none (the loop's own nodes are by
   `Extension("ralph")`), so `cursor.client` is `None` in every hook of the loop (test
   `..._handle_forgets_who_typed`) and a bare `cursor.tell(...)` raises. Did: the command's
   `cursor.client` is remembered in `Loop.client` (L40, L57) and passed as `to=`. **Awkward** but
   correct: the extension should just remember it; one field.
3. **Hook order is an implicit contract with `StepLimit`.** Listed after `StepLimit`, ralph sees
   auto-step already off and stops on the finished reply; listed before it, ralph has already
   written the next prompt, `StepLimit` then switches auto-step off, and the cursor rests on an
   unsent `User` node (tests `..._step_limit_counts_steps...` and `..._listed_before_step_limit...`).
   Both are defensible; nothing in the API says which a user gets. **Awkward** (the `StepLimit`
   docstring warns of the same thing for hooks that turn auto-step on).
4. **Where the loop's state lives.** Not in options: `NodeId` is refused in options (stored options
   are read without a session), and a `Config` node lives on one branch, which the loop leaves
   every iteration. So: extension memory (L49). Fine; that is "leave it to an extension".
5. **A hook that fails after half its work.** A missing prompt file raises in the command and
   nothing is written (test `..._missing_prompt_file...`). One that vanishes mid-loop raises in
   `post_step`: the `CursorView` carries `hook_error` (`ralph`, `post_step`), but the step's
   `ending` was decided before `post_step` ran, so it is still `Done` and auto-step stays on; the
   cursor rests only because `next == Wait` is no work (test `..._vanishes_mid_loop...`). My first
   version called `cursor.move(root)` before reading the file and so left the cursor half
   restarted at the root (found by probe); the fix is to read first, and to keep the loop
   registered only while it goes on (L69, L80, L91). Core does not roll back a failed hook's
   writes (no hook edit limits), so this ordering is the extension's. **Cosmetic**, but a
   `post_step` failure that leaves auto-step on is a surprise worth a sentence in the docs.
6. The command's result arrives as soon as the command returns (`/ralph` answers in no time while
   the loop runs on). Intended, but see (d).

### (b) Showing state to the user

- **Iteration count and stop reason are `Notice`s to the client that typed `/ralph`** (L82, L96).
  Any other client sees only nodes: it must count `Appended` `User` nodes by
  `Extension("ralph")` (test asserts it can, and that it is sent no `Notice`).
  `CursorView` has no slot an extension can fill ("iteration 7 of 25"); `busy` stays `True`
  and `auto` stays `True` through the whole run, with no blink at an iteration boundary.
  **Awkward**. Not done: abusing `set_label("ralph/7", cursor)` as a status line (it works with
  today's API and shows up in every client's `Labels`, but a label is a name for a cursor).
- **How many branches pile up**: one per iteration, each a subtree (a typical iteration with tools
  is 4-6 nodes), forever; the log is append-only. 25 iterations is about 100 nodes, no cap and no
  prune. A tree view must collapse siblings of the current walk. **Cosmetic** until someone runs
  `max_iterations=10000`.
- After the stop the cursor rests on the last finished reply (`DONE` or `max_iterations`), or on an
  unsent prompt (see Fought 3). It is not told apart in the `CursorView` except by `next`.

### (c) Getting input from the user

Nothing is asked, by design. Two near misses:
- **A gentle stop.** `Client.set_auto(cursor, False)` stops the loop at the next step boundary
  (probe: the sample in flight finishes, the next step does not start, the loop is dropped). It is
  a step boundary, not an iteration boundary: a run stopped mid-tools leaves a `Results` node as
  the last thing. "Stop after this iteration" would be a command setting a flag in the extension.
  Not added. **Cosmetic**.
- Bare `/ralph` is a form: `prompt` is required, `root` optional with completion. Free.

### (d) Timing and concurrency

- **A loop that never rests is a cursor that cannot take a `Cursor` command.** `/config` and
  `/ralph` both run as a step of the cursor and are `Busy` unless it is at rest (test
  `..._config_before_the_loop_holds...`). So the iteration cap, `StepLimit`'s limit and anything
  else configurable cannot be tuned mid-run; the choices are abort (loses the sample in flight)
  or wait. This is D16's "commands do not queue", deliberately; for a loop that is by design
  always busy, it means the only live controls are files (`DONE`), `set_auto(False)` and abort.
  **Awkward**. Even if `/config` were allowed mid-run, it would write a `Config` node on the
  branch the loop is about to leave, so the next iteration would not see it; options set before
  `/ralph` are on `root`'s walk and hold for every iteration (the test sets
  `max_iterations=2` first and the loop stops after two).
- **`configure`/`run` return when the command returns, not when its step ends**, so `/config`
  followed at once by `/ralph` is `Busy` (the test asserts it, then settles). Real users never
  type that fast; a script does. **Awkward** (same as forest).
- **Is `StepLimit` the right stop?** As an outer guard, yes; as the stop, no. It counts steps, an
  iteration with one tool call is two (test: limit 4 gives 2 iterations); it is never reset between
  iterations because the loop writes nodes instead of input items (`StepLimit` resets on
  `pre_input`), so the count is "unattended steps in a row", exactly right for a run of the
  loop; and its default of 50 ends a run after at most 50 steps, which will surprise anyone who
  expects "forever" (`/config step_limit limit=100000` before `/ralph`). The iteration cap is its
  own option because the user thinks in iterations.
- **Abort** ends the loop (`Aborted` in `post_step`, tell, drop); a later `set_auto(True)` sends
  the unanswered prompt once and rests (test `..._abort_ends_the_loop...`).
- **Spin**: none found. Every restart commits a node.

## Frontend view

**Chat line client today**: `/` lists `/ralph <prompt> [root]`; running it prints
`[ralph: PROMPT.md from node 1]`, then `[ralph: iteration 1 finished]` per iteration and a final
`[ralph: stopped ...]` line, while the watched cursor streams each iteration's text one after the
other with nothing marking where one ends and the next begins. That is enough to follow it.

**A TUI or web frontend** needs: the cursor's walk as the main pane (it changes `at` to a new branch
every iteration, and the D15 rule "drop the reply layer when `at` changes" already does the right
thing); a count it must derive (`Extension("ralph")` `User` children of the root, with the root
found as the parent of the walk's first `Extension("ralph")` `User` node); a tree view that folds old iterations; and
`busy`/`auto` (which do not blink). It would like a status string from the extension; today the
only channel is a `Notice` to the client that started it, so a second client (a phone, say) sees
the tree grow and nothing else.

## Since stage D

**Wish 3, a status per cursor:** built as `cursor.show(text)`. The loop shows `iteration N of M` on
its cursor and clears it when it stops, so a second client sees the count in `CursorView.shown`
without counting nodes. The `Notice` per iteration is gone; the stop reason is still a `Notice`
(it is a message, not a state). The line client prints `[ralph: iteration 2 of 25]`.

## Wish

1. **Nothing in core for the loop.** It is 25 lines of extension on `post_step`, `move`, `write`
   and `set_auto`, and each of them did what the docs said. The honest answer to "can a hook
   move the cursor between steps?" is yes; to "does never-spins fight it?" is no.
2. **Document, or fix, that `Client.run`/`configure` return before a `Cursor` command's step has
   ended** (same as forest Wish 5). Removes the Busy race for scripts; the extension is unchanged.
3. **A status an extension can set per cursor**, only if a second client matters: smallest form is
   a `CursorView.status: str` set by `cursor.set_status(text)`, sent whole like the rest of the
   state. Would replace the `Notice` per iteration (L82) with one line and give every client the
   same text. The honest alternative is "this is fine": nodes plus `Notice` are enough, and a
   frontend that wants an iteration counter can count them. I would not add it for ralph alone.
4. **Say which `post_step` runs first** (extension order is the driver's list order; `model.md`
   "does not yet say how `config_at` names them"). Resolves Fought 3 in prose, no code.
5. Not wished: queueing `/config` during a run (D16 rejected it for good reason), a core iteration
   concept, pruning old branches, or `StepLimit` counting iterations. A loop whose controls are
   files and `StepLimit`, and whose tuning is "abort, `/config`, `/ralph`", is the Ralph way.
