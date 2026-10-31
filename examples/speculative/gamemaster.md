# gamemaster: friction log

Files: `examples/speculative/gamemaster.py` (175 lines, about 55 of them cross-cursor plumbing),
`tests/examples/speculative/test_gamemaster.py` (9 tests, through `Harness`). Line numbers below
are the extension's unless a test is named.

## Does

- The main cursor is the GM's conversation with the player. A character is a cursor labelled
  `char:<name>` that branches off the **same root `Config`** as the GM, with its own `System`
  node holding the persona (L45-L78). In that branch the GM is the user and the model plays the
  character. `/character Mira "a wary innkeeper" model=mira` (a command) and `introduce(name,
  persona, model)` (a tool) create one. The cast is only labels, so it survives a reload.
- The GM model has `roll("2d6+1")`, `introduce`, `present` (who is here, who is answering) and
  `ask_characters(who, message)`. The last sends `message` to each named character **in
  parallel**, waits for every reply, and returns the lines as the tool result plus
  `(not asked: Cass)`. The GM's next sample is the summary of the scene. Characters not named
  are never sampled and their histories do not grow. There is no automatic "while you were
  away" note: the GM's message is the briefing (the tool says "they know nothing else").
- The player talks to a character by sending to that cursor from the frontend; the character
  answers, the GM never hears of it (`test_the_player_can_watch_a_character_think...`).

## Easy

- A character is a branch + a label: `enlist` is 8 lines (L70-L78). No registry, no reload
  code; `cast` (L52-L59) derives the cast from labels, so I7 holds for free.
- `Model | None` as a parameter (L109, L114) gives the frontend a model picker and the GM a
  per-character model (a cheap model for extras) with no code. Same for `list[str]` + `Annotated`
  help text on `ask_characters` (L121-L122): the schema the GM sees is the documentation.
- `present` (L62) is one function in both lists (L94-L95): a tool the GM calls and a `/present`
  the player types.
- Parallelism is free: cursors are independent steps, so two characters were mid-sample at
  once (`test_a_scene_asks_only_those_present...` holds both provider gates at 1 together).
- `Progress` (L123, L140) makes the scene visible on the GM's cursor as it fills in:
  `Live(ToolProgress("Mira has answered"))`.
- `roll` (L97-L106) is ordinary Python; the rng is injected so the test computes the expected
  roll from a twin `random.Random(7)`.
- A failed character (`Fail("overloaded")`) fails the call with an `Err`, the GM sees it
  (`RuntimeError: the character did not answer: ...`), with no extra code beyond L174.

## Fought

### (a) Expressing the behaviour

1. **A tool cannot ask another cursor and get its reply.** Intent: `reply = await
   node.say(mira, "Borin walks in")`. Offered: `send(to, text)` (an input; returns an item id)
   and `add_item`; nothing returns the reply and no handle can await a cursor's rest (only
   `Driver.rest`, which an extension does not have). Did: item + `pre_step` hook that turns it
   into a `User` node (L153-L163) + `post_step` hook that resolves a `Future` the tool awaits
   (L165-L175) + a `waiting` dict (L93). About 35 lines of plumbing for a one-line idea. The
   hooks of one extension instance see every cursor, which is the only reason it can be done.
   Severity: **awkward** (expressible, but the shape is a trick; also the thing the whole
   extension is about).
2. **Author of the GM's message.** Intent: a `User` node in Mira's history authored by the GM.
   Offered: `TreeHandle.send` writes the input as `client`, the nearest human typist on the walk
   (`NodeHandle.client`), so Mira's history would say the *player* wrote the GM's line;
   `append` is authored `Extension` but does not move the cursor. Did: the item/`pre_step`
   route above, which gives `by = Extension("gamemaster")` (asserted in the scene test). The
   `# WISH:` block at L132-L134 is the line I wanted. Severity: **awkward**; the wrong-author
   version is one line shorter and silently wrong.
3. **A wake with auto-step off does not sample** (Q39), so the delivered message sat there.
   Did: `cursor.set_auto(True)` (L162), found by reading the model doc, not the handle's.
   Severity: **cosmetic** (one line, but a trap for the next author). It also leaves the
   character on auto-step forever.
4. **Tool and command need different handles.** `introduce` takes `NodeHandle` (so it can
   `walk()` to the GM's root), `character` takes `Cursor`; `TreeHandle` knows no walk, so one
   function cannot be both (L108-L116). Annotating a tool `TreeHandle` also hides `walk` at the
   type level though the object has it. Consequence of `Cursor`: `/character` is a step of the
   GM cursor, so it is `Busy` while a scene is running. Severity: **awkward**.
5. **No system-prompt contribution.** The GM needs to be told it is a GM; an extension cannot
   add to the session's system node, so `GM_SYSTEM` (L37-L42) is a constant the program must
   pass to `Session.create`. Severity: **cosmetic** (tool descriptions carry most of it).
6. **No read of "all labels".** `cast` scans every cursor's labels (L52-L59), and `set_label`
   silently moves an existing label, so `enlist` must check first (L72). Severity: **cosmetic**.
7. Known sharp edge left in: two `ask_characters` calls for one character in the same batch
   overwrite `waiting[cursor]` (L136). Not guarded (a queue of waiters would be the guard).

### (b) Showing state to the user

- A character is a cursor; whether it is "in the scene" is not state anywhere. The user can see
  the *cost* (below) but "who is in the scene" lives only in the GM's tool-call arguments.
- The raw lines the characters said are in the GM's `Results` node and in each character's own
  walk, but the player's frontend must know to look. Severity: **awkward**, a frontend problem
  (see Frontend view), not an API gap.
- Cost is visible, indirectly: `CursorView.busy` went true for cursors 0, 1, 2 and never for 3
  (asserted in `test_a_frontend_sees_every_cursor...`); the provider saw 2 GM + 1 + 1 + 0
  samples; the tool result ends `(not asked: Cass)`. There is no per-cursor usage/token
  number in any event. Severity: **cosmetic** unless the player wants money, then a gap.
- After a reload every cursor is `busy` for its empty wake step, so `/present` says everyone is
  "answering" until the loop settles (`test_the_cast_survives_a_reload...`). **Cosmetic.**

### (c) Input from the user

Nothing needed a question. `/character` is a form for free (`name`, `persona`, and `model` with
the model picker); a bare `/character` would ask each missing argument. If the player should
approve each scene's spend, that is a `pre_tool` hook with `node.ask(..., Literal["go",
"skip"])` as in D16 section 6; not written, because it is the existing Permissions case
unchanged. No friction.

### (d) Timing and concurrency

1. **Abort does not reach the characters.** Intent: aborting the GM mid-scene stops the cast.
   Offered: abort cancels the tool call (Interrupted outcome, the `finally` at L145-L147 drops
   the waiters) but a handle cannot abort another cursor (`abort` is `Client` only) and hooks
   act only on their own cursor. Result in `test_aborting_the_gm_mid_scene...`: GM `Results`
   is `[Ok, Interrupted]`, Mira's sample is still in flight and her reply lands in her history
   afterwards, unheard; there is no GM summary. The player can abort Mira from the frontend
   (she shows `busy`). Not worked around (a `pre_sample` flag would stop samples not yet
   started, not the one in flight). Severity: **blocker** for "abort stops the scene", awkward
   for everything else.
2. **`Client.run` returns before the cursor is at rest.** `/character` three times in a row
   raised `Busy` on the second until the test settled between them (test L109). The doc
   says "once its step has ended"; the cursor still counts as busy for a beat. A line frontend
   would race itself. Severity: **awkward**.
3. **The Harness cannot settle over a tool parked on another cursor.** `settle` counts only
   gates and open questions, so a tool awaiting Mira looked like an unparked unit and settle
   spun out. Did: a `Waits(Gate)` subclass reporting 1 while any reply is pending, passed in
   `gates=` (about 10 lines of test). Severity: **cosmetic** but any "cursor drives cursor"
   extension will hit it.
4. **One `FakeProvider` cannot script several cursors independently.** Its script is a single
   queue consumed in the order samples *start*, so with parallel characters the replies would
   go to whoever sampled first. Did: `ByModel(FakeProvider)` in the test (test L46-L66), routing by
   `ModelConfig.model` to one `FakeProvider` per model, which needs a per-character model
   (the `model=` parameter above). `Harness` builds `{provider.name: provider}` from the one
   provider it is given, so the router is the only way. Severity: **awkward** for tests;
   an unscripted cursor (Cass) raising `AssertionError` if sampled is a good "was not called"
   assertion.
5. Failure of one of N: the first error fails the call; the other characters still answer into
   their own histories and the GM sees none of it. Chosen over swallowing (fail early); the
   cost is lost lines. **Cosmetic.**

## Frontend view

**Today** (`examples/chat.py` unchanged): shows one cursor only, prints text deltas and
`[calling X]`. On the GM's cursor the player sees `[calling roll]`, `[calling ask_characters]`,
the GM's summary. It prints neither the characters' lines nor the tool results, and no
`ToolProgress`. Pointed at `char:Mira`'s cursor (`client.send(mira, ...)`) it works as a
plain chat with her, with the player standing in the GM's seat (author `Frontend`, test
`...talk_to_it_directly`).

**A TUI/web view needs**, all of it already in the events:
- `Labels`: the only way to know which cursors are characters (`char:` prefix is a convention
  between extension and frontend; nothing else carries cursor metadata). Tabs from labels.
- `CursorView` of every cursor, always sent: a busy spinner per tab, `at` per tab. This is how
  "2 of 5 were called" is shown, with no extension help.
- `Appended` is **not attributed to a cursor.** The frontend must keep a `node -> cursor`
  map itself: a node belongs to the cursor whose `at` is it or whose walk passes through it,
  which it learns from the next `CursorView`, or by `walk(at)` when a tab is opened. The test
  does exactly this (`walk_ids`). Costs a round trip per tab switch.
- `watch(cursors)` replaces the set: to see the GM's `ToolProgress` and a character's live
  tokens at once it must `watch([main, tab])` and re-call on every tab switch. Characters not
  watched cost nothing but the `CursorView`.
- The `Results` text of `ask_characters` is where the scene's raw lines live for the GM; a
  nice view would render that node as a transcript pane. Nothing about it is marked.

## Wish

Smallest first; the first two together delete L93, L130-L149's waiter code and all of L153-L175
(about 40 of the 55 plumbing lines) and make the `# WISH` block real.

1. **`TreeHandle.say(to, text)`**: like `send`, but the `User` node is authored
   `Extension(name)` and the cursor wakes with auto-step on. Enables L132-L134; removes L153-L163
   and the need for item + `set_auto`. (Or let `send` take `by_extension=True`.) Not a new
   concept: `send` with the author the caller already is.
2. **`await tree.rest(cursor)`** on a handle: returns when that cursor is at rest, raises if its
   last step did not end `Done`. It is `Driver.rest` for an extension. Removes L93, L136-L142,
   L165-L175; the tool becomes `say` to each, `gather(rest)`, read `walk(at)[-1]`.
3. **`NodeHandle.abort(cursor)`**: so a tool's `except CancelledError` can stop what it
   started. Two lines; closes (d)1. Argument against, from the model doc: "hooks act only on
   their own cursor". `send`/`add_item` already cross; abort is the same kind of act.
4. **`Client.run` resolves after the cursor is at rest** (or the doc says `Busy` is expected
   and tests settle). Deletes the settle at test L109; fixes a race any frontend has.
5. **`TreeHandle.labels()`** and `set_label` refusing to move an existing label unless asked.
   Replaces `cast` (8 lines) and the check at L72. Cosmetic.
6. **Test side:** a `FakeProvider` keyed by model (or `Harness.create(providers=...)`) deletes
   `ByModel`; `settle` that treats "a tool awaiting a busy cursor" as parked deletes `Waits`.
7. **Honestly fine, no change:** characters as labelled cursors (the label convention is the
   right registry; core should not grow a "character" notion); `Appended` unattributed (a
   frontend rule: attribute by `at`, worth one sentence in D16 section 5); system prompt as a
   program constant; no cost/usage event (a frontend can count `busy` and samples).
