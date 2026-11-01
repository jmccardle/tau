# forest: friction log

> **B1–B2 resolved (Stage B, 2026-10-31).** Fought (a)1 (polling `busy` and a `post_step` hook
> to learn each child's ending) is gone: `_fan` is `open`, `send`, `await rest` on each, and a
> `finally` aborts the branches, so aborting the search stops the samples and the judges in
> flight. 268 lines to 254. The log below is the history of the first version and its line
> numbers no longer match.

Files: `examples/speculative/forest.py` (266 lines; about 100 are the search maths, `scores`,
`backed_up`, `select`, `render`; the API-facing part is `Forest`, lines 160-262),
`tests/examples/speculative/test_forest.py` (12 tests, all green).

## Does

`/search N question` branches N cursors from the current node, one sample each, in parallel. A
judge branch (a cursor opened at each candidate and sent a judge persona) scores every candidate
`SCORE: 0..1`. The score is a `Custom` node (`forest/score`) under the candidate; visits and
backed-up means (MCTS) are computed from those nodes, never stored. UCB1 picks the node to expand
(N refinements of it) until `max_samples` would be passed. The main cursor then moves to the best
reply and the command returns the explored tree with scores, visits and sample/token counts. The
judge is searchable: `judge_depth=1` rates each verdict with half as many judges. `steer=true` asks
the person, after each round, whether to go on. `/forest_tree <node>` re-renders from the log.

## Easy

- **Where scores live** (L222, one line): `cursor.append(reply, Body.Custom(SCORE, data))`. A node
  is persisted, replayed on load, and sent to every client as an `Appended`: survives a restart
  and is visible to a frontend with no new event kind (tests: `..._survive_a_restart...`,
  `..._frontend_sees_cursors_scores_and_progress`).
- **Derived, not stored** (L74-122): visits, means, UCB, spent samples and tokens are functions of
  the tree (I7). Samples = `Assistant` nodes under the start (L89-98); tokens from `meta.usage`.
  There is no search state to lose, resume or corrupt.
- **Branching N parallel cursors** is two lines (L245-248): `cursor.open(at)`, `cursor.send(k, text)`.
  The test holds all three samples at one gate: three requests in flight together.
- **Moving the main cursor to the winner** is `cursor.move(best.reply)` (L210); the command's step
  then ends on a reply, so nothing samples (D16's "a command that should not sample" never bit).
- **Commands and options**: `commands = (self.search, forest_tree)` (L166); `n: int, question: str`
  give the form, the check and `Annotated` help; `Progress` is injected (L202); options are a frozen
  dataclass (L44-56) read with `cursor.options()` and set per branch by `/config forest ...`.
- **A typed question** is three lines (L204-208): `await cursor.ask(text, Literal["yes", "no"])`;
  declined (abort, no client) is `None`, which I read as "stop spending".
- **Recursion** (L224-241) is an ordinary recursive coroutine over `_fan`; no API was needed.
- `forest_tree` is a `TreeHandle` command: it runs at once, whatever the main cursor is doing.

## Fought

### (a) Expressing the behaviour

1. **Wait for N cursors to come to rest.**
   Intent: `await` the children. Offered: `cursor.cursor(k).busy` (a read) and hooks. Did: poll
   `busy` every 10 ms (L250-251) plus a `post_step` hook that records each child's `Ending`
   (L168-171, a handle cannot read how another cursor's step ended; a hook can). A `post_step`
   hook alone cannot say "rest": core decides rest after `post_step` (auto-step, the no-move rule,
   `StepLimit` turning auto-step off), so a hook that sets an event on `Next.Wait` deadlocks when a
   cursor rests mid-loop. Not done: an extension holding its own `driver.client()` and reading
   `CursorView.busy` events; it would be a connected client, so `ask(to=None)` stops declining at
   once when nobody is there (D16 §3) and it receives `Notice`s. **Awkward** (works; a timer in
   an extension, 22 lines for what should be one `await`).
2. **Start a step on another cursor.**
   Intent: N replies as siblings under one `User` node. Offered: `open` wakes with auto-step off
   (only `pre_step`/`post_step` run); `set_auto` is own-cursor only; `send` is input plus auto-on.
   Did: `send`, so the question is a new `User` node per branch (`at -> U_i -> R_i`, N copies of
   the question). Probe: `open` then `send` costs two steps per child, since the input arrived
   after the wake and the news rule starts a second, empty step (hooks run twice; `StepLimit`
   counts 2). Gap left open. **Cosmetic**.
3. **Judge a reply without a cursor.**
   `model.md` §7 says a handle may "sample a walk or run a tool outside any step"; `handles.py`
   has no such method. Did: each judge sample is a cursor with its own two nodes. This is also
   the brief's "judge is a branch", but 12 samples is 12 cursors. **Awkward**.
4. **Cursors never close** (Q12). Test asserts `1 + 12` cursors after a 12-sample search, all at
   rest, forever; every client's snapshot carries 13 `CursorView`s from now on. **Awkward** (grows
   with every search; the real cost of N-way fans in `tau agents`). **Resolved by C1 (D17):**
   `_fan` calls `close_cursor` on each branch once its reply is read; the test now asserts one
   open cursor and twelve closed.
5. **Judge persona.** Only a `User` message in the same walk (the judge sees the candidate and the
   question in its context). A fresh persona means `append_root` + `System` + copying the text
   + the start problem of (2). Did the former. **Cosmetic**.
6. **Annotating a node.** Nodes are immutable and labels name cursors, so a score is a child
   `Custom` node. Consequence: `tree.children(candidate)` now mixes the score, the judge branches
   and the refinement branches; the renderer tells judge from refinement only by the score node's
   `verdicts` field (L74-86, `Scored`). `derived_from` (model.md §1, not yet spiked) would say it
   in the tree. **Cosmetic**.
7. **Free text as a command argument.** `/search 3 what is 2+2` fails with `/search takes 2
   arguments`; the question must be quoted (test `..._wants_the_question_quoted`). **Cosmetic**.
8. Fail early held without effort: a judge with no `SCORE:` raises, the step is `Failed`
   (`hook_error` names `pre_step` of `forest`), and the candidates stay in the tree unscored.

### (b) Showing state to the user

- **Progress** (L202) is a `Notice` to the client that ran the command only. A second client (the
  TUI) gets none (test asserts this); it has the nodes: the `forest/score` `Appended` per
  candidate. **Awkward**: a frontend that did not type `/search` cannot tell a search is running
  except that the main cursor is `busy`, and has nothing that says why.
- **Which cursors are the search's children?** `CursorView` has `cursor`, `at`, `busy`, `auto`
  and nothing about who opened it (`opened_by`, Q6, is specified and not spiked). The test's
  "tui" finds them as "busy, not the main cursor", a guess. I could label them
  (`cursor.set_label`), which would leak 12 labels as well. **Awkward**.
- **Cost**: sample and token counts are exact and derived (L89-98), shown per round and at the
  end. Not live: a frontend sums `Appended` `Assistant` nodes itself. **Cosmetic**.
- **The explored tree** is a text block in a `str` result (L132-148). A rich frontend should not
  parse it; it re-derives from nodes with the same two functions, which means shipping the
  extension's `scores()` to the frontend. Fine for a namespaced `Custom` kind.
- `Client.children(id)` is one call per node, so a remote frontend reading a search subtree is
  one round trip per node (the extension's own `below()` is the same walk, L67-71). **Awkward**
  over a network.

### (c) Getting input from the user

- The steering question works (test `..._person_steers_between_rounds...`): it is in the main
  cursor's `CursorView.questions`, `respond("no")` ends the search at round 1, and the search
  goes to the best so far.
- **A line client deadlocks on it.** `examples/chat.py` `Chat.line` awaits `command()`, which
  awaits `client.run(...)` for as long as the command runs; the question is shown
  (`? round 1: ... Expand #3? [yes/no]`) but the prompt loop that would read the answer is the
  thing blocked (probe: `line()` still blocked after 1 s, `chat.question` set). The API is
  consistent (`run` is a call that ends when the command ends; questions come on the stream), but
  nothing says a frontend must run commands as tasks. **Blocker** for chat.py; **cosmetic** for
  the API if documented.
- A declined question (abort, nobody connected) is `None`; I stop spending on `None`. That is a
  decision the extension has to make for itself each time.

### (d) Timing and concurrency

- **Abort across N cursors.** `abort` on the main cursor cancels the command (`CancelledError`
  at the poll, L251): no further round, no judge (test `test_abort_stops_the_search_but_not...`).
  The three candidate samples already in flight run on: a handle has no `abort`, and the
  extension's only other hook point (`pre_step` of a child, returning a string) fires before the
  sample, when nothing is yet out to stop. They finish, are billed, and sit in the tree unscored.
  A frontend may abort one child (`Client.abort(child)`) and the search carries on without it
  (test `..._aborts_one_branch...`). Gap left open. **Blocker** for real money: abort must stop
  spending.
- **`Client.run` returns when the command returns, not when the step ends** (probe: cursor still
  `busy`). The `Client.run` docstring reads as "once its step has ended" for failures only. A
  program that stops the driver right after `run` returns loses the step's unsynced writes (probe:
  after reload, zero score nodes and the main cursor still at the start, though the caller was
  handed the tree); the test settles first, with a comment. `/config` then `/search` back to back
  is `Busy`. **Awkward**; see Wish 5.
- **Parallelism is free**: nothing in the extension serialises; the pump runs the N samples
  together.
- **Test infrastructure**: `Harness.settle()` counts effects against gates, and an extension
  waiting on other cursors is neither, so mid-search states are reached with `until(...)`.
  `FakeProvider` answers in call order, so judge scripts rely on cursor id order; the tests assert
  per-candidate scores, which fails loudly if that order ever changes. **Cosmetic**.
- `StepLimit` counts per cursor, so N children each get their own count; the main cursor's
  command is one step. No interaction found.

## Frontend view

**Chat line client today** (`examples/chat.py` over a `LocalClient`): sees the progress `Notice`
(`[round 1: [0.2, 0.9, 0.5], 6/12 samples]`), the final tree as `[result]`, and the steering
question as `? ... [yes/no]`, which it cannot answer (see (c)). It watches one cursor, so the N
streams are invisible and the 12 extra `CursorView`s are ignored. Nothing is wrong, nothing is
shown.

**A TUI or web frontend** needs: every `CursorView` (to show N busy panes) and a reason they exist
(opened-by, which it must guess); `watch(children)` for their `Live` (works, with catch-up: test
`..._frontend_sees_cursors...` receives both `TextDelta`s); the `Appended` score nodes to annotate
a tree view; the parent links, read with `node`/`children` (one call per node); the extension's
`scores()` and `backed_up()` to turn nodes into n/mean, since nothing in core says what a
`forest/score` means; and its own rule for hiding judge branches from the tree (the score's
`verdicts`). Joining mid-search is fine: the snapshot has the busy children, the nodes are readable.

## Since stage D

**(b) Progress** is now `cursor.show`: the search shows `round N: sampling (previous: <scores>)` on
its cursor for every client and clears it when the command ends (a `finally`). The `Progress`
parameter and its `Notice` to the client that ran the command are gone. A `show` replaced before a
client reads it is never seen, so each round makes one `show`, at its start.

## Wish

Smallest first; each says what it deletes or enables.

1. **`await tree.rest(cursors) -> {cursor: Ending}`** on `TreeHandle`. The driver already has
   rest waiters (`_Waiters`) and endings. Deletes L165, L168-171, L247, L250-261 (the `_ended`
   dict, the `post_step` hook, the poll and its 10 ms timer, ~15 lines) and makes `_fan` about
   six lines.
2. **`tree.abort(cursor)`**, the handle form of `Client.abort`, by `Extension(name)`. Enables
   `except CancelledError: for k in kids: tree.abort(k); raise` (+3 lines) and fixes the one
   blocker: abort stops spending.
3. **`tree.sample(at, text)`**, the thing `model.md` §7 already promises ("sample a walk outside
   any step"): appends a `User`/`Assistant` pair under `at` and returns the reply, owned by the
   calling task (so cancelling the command cancels the sample). Judges would use it and open no
   cursor: deletes 6 of the 12 cursors in the two-round test, and wish 2 for them. Candidates stay cursors when
   they should run tools. Honest alternative if not wanted: **`tree.close(cursor)`**.
4. **`opened_by` in `CursorView`** (Q6 is already in `model.md`): a frontend folds children under
   the main cursor without guessing; no extension code changes.
5. **`Client.run` / `configure` return when a `Cursor` command's step has ended** (or say plainly
   in `client.py` that they do not, and give a frontend `Client.rest(cursor)`). Deletes the
   `settle()` and the Busy race in callers; changes nothing in the extension.
6. `commandline.parse`: a final `str` parameter takes the rest of the line, so `/search 3 what is
   2+2` works. Pure frontend library.
7. `FakeProvider` replies keyed by the last node's text (test fake): the judge scripts stop
   depending on cursor id order.

Fine as it is, and I would not add: search state in core, a `score` on nodes, node annotations
other than `Custom` (score-as-node is the best part of this), UCB or MCTS anywhere outside the
extension, a progress channel other than `Notice` plus nodes. Chat.py should run commands as
tasks; that is its fix, not the API's.
