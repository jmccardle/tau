# D17: Input says who wrote it; cursors close

Status: proposed (2026-10-31). Phase 5 fix plan items B1 and C1
(`docs/plan-phase5-fixes.md`), under rulings 3 and 7. Reverses `model.md` Q12.
Amends D2 (input), D6 (what a cursor is), D12 (file version) and D16 §5 (client
events).

Phase 5's exit rule is that a core change needs an ADR. Both changes here touch the
session files: B1 changes the scratchpad file, and C1 adds a log record. They are
decided together so that the files change once.

## Context

**Input has the wrong author.** An input item is `Input { client, text }`. The input
phase commits it as a `User` node by `Frontend(client)`. A frontend's `send` is
correct. An extension's `send` (`TreeHandle.send`) has no client of its own, so it
borrows the last person who typed on that branch and writes the text as theirs. That
is a silent lie: the tree says John typed what the gamemaster wrote. With nobody
typing, `send` raises, so an extension can't drive a cursor that no person has
touched.

Ruling 3: text an extension sends to a model is a real `User` message. The
gamemaster *is* the user prompting a character, no more an injection than a skill
or an `@file`. The role is right. The author is wrong.

**Cursors never go away.** Q12 said cursors don't close "for now". A forest or a
gamemaster opens a cursor per sub-agent or character. After the work is done, each
one stays in `cursors()`, in every client's snapshot, and is woken on every load.
Ruling 7: a cursor is a name on a tip of the tree, and closing one keeps only what
restarting needs, its name and its last node. The owner also ruled that close aborts
first (Decision 2 of the plan).

**Facts this decision relies on, checked in the code on 2026-10-31:**

- Cursor ids are dense. `Session::open` takes `at.len()` as the new id
  (`tree.rs:252`), and replay refuses an opening move whose cursor is not
  `at.len()` (`replay.rs:58`).
- The log holds four kinds of record: `Write { node, mv }`, `Move`,
  `Label { label, cursor }`, `Sync { pad }` (`tree.rs:123-141`). The store always
  writes `Sync { pad: None }` to the log, and the pad goes only to the scratchpad
  file (`tau-store/src/lib.rs` `save`). So **the log never holds items**, and
  changing `ItemData` changes no log bytes.
- One `VERSION` constant (`tau-store/src/lib.rs:24`) is written into, and checked
  against, the headers of both files (`header`, `check_header`). A file of any
  other version is refused with `Version`.
- Auto-step and busy are not persisted (D6). `reconcile` turns every cursor's
  auto-step off and drops delivered items. `Machine::loaded` wakes every cursor in
  `auto` (`machine.rs:33-39`). The Python `Session.cursors()` is the same key set
  (`tau-py/src/session.rs:402`).
- Abort (`Machine::abort`, `machine.rs:162`; `Step::abort`, `step.rs:507`) turns
  auto-step off and makes the ending `Aborted`. It returns true only while the step
  is in `Sample` or `Calls`, which tells the driver to cancel that effect. A hook
  that is already running finishes, and `post_step` still runs. Abort does not
  touch items. The driver's abort also stops a running command and declines open
  questions (`_calls.py` `_left`).
- Every frontend, extension and driver call on a cursor first checks
  `Session::at(cursor)`. Those calls are `input`, `step`, `go_to`, `set_auto`,
  `abort`, `add_item`, `set_label`, the hook calls, `next_job` and `answer`.
  `Session::add_item` itself does not check; `Machine::add_item` does.
- The halt notice: a `pre_input` halt removes the offered item and pushes a notice
  only when the draft's author is `Frontend(client)` (`step.rs`, `on_hook_stop`).
- Sync Python tools and commands run on worker threads (`_functions.py:147`), and
  their handle calls go straight into Rust. So a handle call can reach the session
  between two calls on the event loop.
- Python already has a `Closed` exception and `close()` methods with other meanings:
  `Session.close()` (the session) and `Client.close()` / `Feed.close()` (a stream).

## Decision

### 1. Input is authored (B1)

- `ItemData::Input { client: String, text }` becomes **`Input { by: Author, text }`**.
  The input phase drafts the `User` node with that `by`.
- `Machine::input(cursor, by, text)` (Python `Session.input(cursor, by, text)`)
  takes the author:
  - A client's `input` and `send` pass `Frontend(client)`, as now.
  - **`TreeHandle.send(to, text)`** passes the handle's own `by`, which is
    `Extension(<its extension>)`. It never needs a client, so `send`'s
    `ValueError` for "no client" goes away. `send` still turns `to`'s auto-step on.
  - `Driver.add_item` still refuses an `Input`: an input comes in through `input`
    or `send`.
- Core does not check which author an input has. By Q7, authors are recorded
  only. The handle fills in `by`, so an extension cannot claim to be a frontend.
- **`pre_input` hooks** see the draft's `by` (`Draft.by`, already exposed). A gate
  can tell typed text (`Frontend`) from sent text (`Extension`) and treat them
  differently. Gates that don't look at `by` treat both the same, which is the
  point of ruling 3.
- **A `pre_input` halt** works as it does today: the item is removed and the step
  ends `Halted(reason)`. A notice goes out only when the author is a `Frontend`. An
  extension has no stream to receive a notice. It learns the reason from the ending:
  `await tree.rest(cursor)` (B2, below) returns `Halted(reason)`. Every client sees
  the same reason in `CursorView.ending`.
- **How a frontend shows it.** The node is a `User` node whose `by` is
  `Extension("gamemaster")`. A frontend shows it as a user message under the
  author's name ("gamemaster:"), not as the person's own line. The same goes for a
  pending item in `CursorView.items`, which now carries `by`. This is frontend
  code, not a rule in core or the driver. `examples/chat.py` prints `Appended`
  `User` nodes that its own client didn't write, labelled with the author. That is
  part of B3.
- **Files.** The log records are unchanged: `Author::Extension` is already
  recorded on nodes and moves, and items never reach the log. The scratchpad file
  stores `Input` items, so its format changes. The file version goes **1 → 2**.
  Because one `VERSION` covers both files, the log's header changes with it. No
  migration, since we're before 1.0 (and `~/.tau` was started fresh on 2026-10-29).
  A version-1 session is refused with `Version`.

**Why not a new body kind** (`Body::Message { from, text }` or similar): every
provider would have to convert it to the user role anyway. Q11 (wire legality),
compaction and anything that reads a walk would each learn a kind that means
`User`. Q30 already set the precedent that a summary or reminder an extension writes
is `User` by that extension. The author field already carries the difference.

**Why not keep delivering `Extension` items from `pre_step`** (the sub-agent idiom,
`model.md` §7 "Between cursors"): it only works if the target cursor runs an
extension that recognises the item and writes it. It skips the target's `pre_input`
gates, so a filter that every person's input passes through never sees the
gamemaster's. It also leaves auto-step to the sender. Input already has a phase
that delivers it (Q31). The idiom stays valid, and is the right tool, for data
that isn't a user message: a task description the receiving extension turns into
something else, a timer tick, a relayed webhook.

**Why not `Input { client }` with the extension's name as the client**: that lies
the other way. It claims a frontend named `gamemaster` typed it, and a halt notice
would go to a client that doesn't exist.

### 2. Cursors close (C1)

**The record.** Closing appends **`Close { cursor }`** to the log, a tombstone.
Like a label record, it has no `by` or `time`.

**What stays.** The cursor keeps its id, so ids stay dense and the `open` rule
(`at.len()`) is unchanged. Its last node stays in `at`. Its labels stay where they
are: those labels, plus the last node, are the restart record of ruling 7. Its moves
stay in the history.

**What goes.**

- It leaves `cursors()` and the clients' views.
- It is not woken on load.
- Its pending items are removed, in the same sync as the `Close` record. Otherwise
  they would sit in the scratchpad addressed to a cursor that can never take them.
- Its auto-step is gone.

The Python `Session.cursor(c)` raises for it (see the error below). The new read
**`closed()`** (on `Session` and every tree handle) lists `(cursor, last node)` for
each closed cursor, in id order.

**Restarting** is `open(at=last node)`, then `set_label(label, new)` for each label
to keep. A new id with the old name: the label moves. There is no reopen call in
core; see Open.

**Every other call on a closed cursor fails early** with a new core error,
`Error::Closed(cursor)` ("cursor 3 is closed"). In Python it is **`CursorClosed`**,
a subclass of `NoSuchCursor`. `NoSuchCursor` already means "a cursor id that is not
open", so code that catches it keeps working. The specific name and message tell a
person the id is real, and that its last node is in `closed()`. (The name is not
`Closed`, which is taken by `Session.close`.) The check sits in one place,
`Session::at`, which every call on a cursor already goes through, so no call needs a
guard of its own:

| call on a closed cursor | result |
|---|---|
| `move` / `go_to`, a hook's `move` or `write` | `CursorClosed` |
| `step`, `set_auto` | `CursorClosed` |
| `input`, `send` (frontend or extension), `add_item` to it | `CursorClosed`: the sender learns the target is gone and nothing waits in the scratchpad |
| `set_label(label, closed)`, putting a label onto it | `CursorClosed` |
| `set_label(label, open)` for a label now on it; `remove_label` | allowed: this is how a restart moves the name, or how the restart record is forgotten |
| `label(l)` for a label on it | returns its id; `closed()` gives the node |
| `abort` | `CursorClosed`. A cleanup that aborts what it opened must not abort what it already closed. |
| `close` a second time | `CursorClosed`, as a second `Session.close()` raises `Closed` |
| `cursor(c)`, `watch`, B2's `rest` | `CursorClosed` |
| `next_job`, `answer` (the driver) | `CursorClosed`: a driver bug, not a state |

Replay applies the same rule. A `Move`, a `Write` that moves, a `Label` onto the
cursor, or a second `Close` after its `Close` is `BadRecord`. A log can't contain
anything that live calls would refuse.

**Where the work happens.**

- Core: **`Machine::close(cursor)`** is refused with `Busy` unless the cursor is at
  rest. It removes the cursor's items, records `Close` and syncs. It does not abort
  anything.
- Driver: abort, wait for rest, close, as three steps. Waiting is driver work, so
  the waiting lives in the driver, as it does for `Driver.rest`. Core refusing a
  busy cursor reuses the existing `Busy` path instead of adding a "closing" state.
- The rejected alternative copies `go_to`'s `going`: hold the close and apply it
  when the aborted step rests. Then every external call would need its own "closing"
  check, and the step's own commits would run on a cursor that is half-closed.
- **Python `close_cursor(cursor)`** on `Client` and on every tree handle. It is
  async because it waits. The name is not `close` because `Client.close()` and
  `Session.close()` already mean something else. It runs as:
  1. Abort, which stops the command, cancels the effect and declines open questions.
  2. Await rest, which lets a running hook and `post_step` finish.
  3. Call `Session.close_cursor`.
- If the cursor woke between its rest and the close, core refuses with `Busy`, and
  the driver goes round again. This can happen when a sync tool on a worker thread
  adds an item to the cursor. Each round aborts whatever woke it, and each round
  waits for a rest, so the loop never spins.
- A tree handle refuses its own cursor, for the same reason B2's `rest` and
  `abort` do: closing it would wait on itself. A command that takes `Cursor` runs as
  that cursor's `pre_step`, so it can't close its own cursor either.
- Anyone waiting on the cursor sees the abort, not the close. A B2 `rest` already
  waiting returns `Aborted`, and a question asked during the close is declined (A5).
- Driver memory for the cursor is dropped when the close lands: its view, its last
  ending and hook error, and A5's decline mark.

**What a client sees.** One new whole-state event, **`CursorGone(cursor, at)`**:
the cursor closed, so the client drops its view, and `at` is its last node, for
restarting.

- Each cursor has exactly one state event at a time: its `CursorView` while open,
  its `CursorGone` after it closes. In a `Feed` buffer, `CursorGone` replaces that
  cursor's `CursorView`, so the per-cursor buffer bound holds.
- The connect snapshot sends a `CursorGone` for each closed cursor, before
  `Labels`. So the existing rule holds unchanged: a label never names a cursor the
  client has not been sent, including labels on closed cursors.
- No `Closed` or `Removed` delta is added to `CursorView`. D16 rejects
  fine-grained state events, and this is the per-cursor state, whole.
- `Hub.sync` compares `closed()` with what it last published, the same way it
  compares views.

**Load.** `Machine::loaded` wakes only open cursors. `reconcile` also drops items
addressed to a closed cursor. That covers a crash after the log sync that made
`Close` durable but before the scratchpad rewrite. It is the same situation as a
delivered item, and the same fix, so it adds no heuristic. `model.md` I7 becomes:
"state is a pure function of nodes, moves, labels, closes, and items".

**Close and the file version.** `Close` is a new record kind, so it changes what a
build writes. The rule: every change to what a build writes bumps `VERSION`, and
before 1.0 there is no migration. If B1's and C1's format changes land in one commit,
both take `VERSION` to 2. If B1 lands on `master` first, C1 takes it to 3. Sessions
made between the two would then be refused (see Open).

**An empty session is allowed.** Closing the last open cursor is allowed, with no
guard. A frontend that opens a session with no open cursor restarts one, or opens
one at the `System` node. `examples/chat.py` (`session.cursors()[0]`) must handle
this.

### 3. B2, related but not decided here

B2 adds **`await tree.rest(cursor) -> Ending`** and **`tree.abort(cursor)`** to
`TreeHandle`, so tools, hooks and commands all have them. Both refuse the handle's
own cursor. `rest` reads the ending of the last step from a new hub accessor.
`abort` is safe to call from a tool's worker thread. Cycles (A's tool waits on B
while B waits on A) are not detected: the user's abort is the way out, and B3 shows
that it reaches the leaves.

B2 needs no ADR. These are `Driver.rest` and `Client.abort` in handle form, which
`model.md` already assumes. They're listed here because the acceptance test (B3:
gamemaster and forest) needs B1, B2 and C1 together:

1. `send` the text (B1).
2. `rest` and read the reply (B2).
3. On abort, `abort` the children in a `finally` (B2).
4. `close_cursor` the children that are finished (C1).

## Alternatives

| option | why not |
|---|---|
| A new `Body` kind for an extension's message | Every provider maps it to the user role anyway; Q30 already makes an extension's summary `User`. The author says who. |
| Keep the pre_step idiom as the only way to talk to another cursor | Needs the target's cooperation, skips its `pre_input` gates, leaves auto-step to the sender. Kept for data that is not a user message. |
| `send` keeps a client, borrowed or invented | The silent lie this decision removes. |
| Close removes the cursor's `at` and renumbers | Ids must stay dense for `open` and replay; renumbering would rewrite history. |
| Close keeps nothing (no last node) | Ruling 7: restart needs its name and its last node. |
| Calls on a closed cursor are no-ops (an idempotent `abort` or `close`) | Hides a mistake: a `send` to a closed character would vanish. Fail early. |
| Reuse `NoSuchCursor` as is | "no cursor 3" when labels still name cursor 3 is a confusing message. A subclass keeps the old catch working. |
| Close as a pending state in core (like `go_to`'s `going`) | Every external call would need a "closing" check, and the aborted step's own commits would run on a half-closed cursor. The `Busy` refusal plus a driver loop needs no new state. |
| Core `close` aborts and removes items while the cursor is busy | A running hook can't be cancelled (§8), so the close would land under a hook that is still writing. |
| One `Closed` set event resent whole on each close | The set only grows, so each close costs O(all closed). A per-cursor state event is the shape `CursorView` already has. |
| Refuse to close the last open cursor | A guard against a state that is harmless. A frontend opens or restarts one. |
| Separate log and scratchpad versions | Nothing gained: with no migration, an old scratchpad makes the session unreadable either way. |

## Consequences

- **Core** (`tau-core`):
  - `ItemData::Input { by, text }`; `Machine::input` takes `by`.
  - `Record::Close { cursor }`; `Error::Closed`; a closed set beside `at`.
  - `Session::at` refuses closed cursors; the open-cursor read and `loaded` skip
    them; `Machine::close`; `closed()`.
  - Replay and `reconcile` follow the rules above.
- **Store**: `VERSION` bump(s), nothing else. A `Close` record is encoded like any
  other.
- **`tau-py`**:
  - `Session.input(cursor, by, text)`, `ItemData.Input(by, text)`.
  - `Session.close_cursor`, `Session.closed()`.
  - `CursorClosed(NoSuchCursor)`.
  - `_core.pyi`, held to the built module by stubtest.
- **Python**:
  - `TreeHandle.send` uses `by`, and `_need_client` goes from it (with A6, it goes
    altogether).
  - `close_cursor` on `Client`, `LocalClient` and tree handles.
  - `closed()` on tree handles.
  - `CursorGone` in `events.py` and the snapshot; the hub's diff and cleanup.
- **I7.** The replay property tests need:
  - a `Close` op that picks among open cursors. The tests' `cursors()` helper
    counts ids with `at(c).is_ok()` until the first failure, so it must count every
    id and pick among the open ones. `invariants.rs` has the same helper.
  - equality that includes the closed set. `Session` derives `PartialEq`, so a new
    field is compared automatically.
  - a crash case: `Close` synced, scratchpad not rewritten, and load does not bring
    back the closed cursor's items. This is the closing counterpart of
    `an_older_scratchpad_never_redelivers`.
  - a unit test that replay refuses any record naming a closed cursor, with its
    position.
  - a unit test that `loaded` wakes only open cursors.
- **Behavior tests** through `Harness`:
  - extension `send` is authored `Extension`, needs no client, and its `pre_input`
    halt is visible in `rest`'s ending;
  - close of a busy cursor aborts, waits, and removes its items;
  - each call in the table raises `CursorClosed`;
  - restart by label;
  - `CursorGone` in the stream and the snapshot;
  - a session reloaded after a close.
- **Docs**:
  - `model.md`: Q12 becomes "yes (D17)". §7 Input and "Between cursors", and I7's
    statement and `reconcile`.
  - Glossary changes:
    - **input**: "what gives a cursor a `User` message: a frontend's line, or an
      extension's `send`; its author becomes the node's `by`".
    - **item**: `Input { by, text }`.
    - **author**: an input's author is its node's; a halt notifies only a
      `Frontend` author.
    - **cursor**: "Cursors do not close (Q12)" is replaced.
    - **label**: stays on a closed cursor as its restart record; can't be set onto
      one.
    - **scratchpad**: a close removes the cursor's items.
    - **record**: adds `Close`.
    - **machine**: adds `close`.
    - **tree handle**: `send`, `close_cursor`, `closed`.
    - New: **close (a cursor)**, **closed cursor**, **`CursorClosed`**,
      **`CursorGone`**.
    - The heading **close, Closed** becomes the session's explicitly.
  - D16 §5 lists `CursorGone` among the state events.
- **Size** (rough, tests excluded):

  | item | core | `tau-py` | Python | tests |
  |---|---|---|---|---|
  | B1 | ~25 | ~15 | ~20 | ~15 sites touched (the plan's figure) |
  | C1 | ~90 | ~30 | ~90 | ~120 Rust (I7 op, crash case, replay and load units) and ~80 behavior |

  This is close to the plan's ~150 Rust and ~80 Python for C1.

## Open

- **One version bump or two.** Land the format halves of B1 and C1 in one commit to
  spend one bump? Otherwise sessions made on `master` between them become
  unreadable.
- **A reopen call.** Restart is open plus moving each label, about four lines. If
  frontends and extensions all write the same four lines, a driver-level
  `reopen(cursor) -> int` (open at the last node, move every label) would earn its
  place. Not added until a second user appears.
- **B2's `rest` after a reload.** The hub keeps no ending across a reload, so `rest`
  on a cursor that hasn't stepped since then has no `Ending` to return. This is
  B2's to settle (`Ending | None`, or `Done`). It is noted here only because C1's
  close waits through the same path.

**Not verified, inferred:**

- That PyO3's `create_exception!` can subclass another created exception
  (`CursorClosed(NoSuchCursor)`). If it can't, `CursorClosed` subclasses
  `LookupError` directly.
- The test helpers in `invariants.rs` beyond the one `cursors()` function read.
- The line counts.
