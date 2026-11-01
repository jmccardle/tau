# Glossary

Status: **accepted (Phase 1 closed 2026-10-27).** Rule 2: no type, field, or module-level
concept exists without an entry here. One term, one meaning.

Each entry gives the identifier (Rust in `tau_core`, Python in `tau`),
the τ 0.15 terms it **retires**, and a short definition. Rust names marked
*spike* exist in `crates/tau-core/src/{tree,step}.rs`; others are proposals.
Behavior is specified in `model.md`; this file only says what words mean.

## The tree

### node
*spike* `Node`, `NodeId` · retires entry, side entry, finalize

One immutable record: `{id, parent?, time, by, body, delivers}`, plus
`copied_from` and `derived_from` (not yet spiked). Written once (I2).

### body
*spike* `Body` · retires entry type, `customType`

What a node holds, one of: `System`, `User`, `Assistant { parts, stop }`,
`Results`, `Config`, `Custom { kind, data }`. The first four are
**message-bearing**; `Config` and `Custom` never reach the model (Q3).
Known `Custom` kinds: `sample_error` (core, a sample that failed with no
output).

### part
*spike* `Part` · retires content block

One piece of a reply in stream order: `Text`, `Thinking`, or
`Call { id, tool, args_raw }`. Each takes an optional `replay` (see **replay**).
A `Thinking` part may have no text when it has replay data (a redacted block).

### replay
`tau.Replay(provider, data)` · D15 · new

Data only the provider that wrote a part can send back: Anthropic's
`thinking.signature` and `redacted_thinking` data, Gemini's `thoughtSignature` (on text and
on calls). `provider` is the name of the provider that wrote it; `data` is the text of a
JSON object, kept exactly as given: `part.replay.data` is the exact text. Core checks both on
construction and when a log loads (`BadReplay`, a `ValueError`: a provider name that is empty,
blank, or holds a control character; `data` not a strict JSON object, or with a repeated key
at any depth; `NaN` and `Infinity` are not JSON). A logged `replay: null` is refused, as is
any other non-replay, with the reason in the `Undecodable` message. A part's `replay` is `None` unless the provider sets it; logs written before
replay existed load with `None`, and a part without replay is logged exactly as before. The
driver's ordering rule (see **provider, model**) ignores it: a provider learns a signature
only at the end of a block, so it is on the part and on none of its deltas.

### replay_for
`tau.replay_for(part, provider)` (`protocols.py`) · D15 · new

The replay data a provider may send back for a part, parsed: the JSON object in
`part.replay.data` when the part's replay is tagged with `provider`, else `None`. String
values parse back exactly; numbers and escapes may not re-serialize to the same bytes, so a
provider that needs the exact text reads `part.replay.data`. A non-`Part` raises `TypeError`. A part with another provider's replay is
treated as unsigned (`model.md` §10.4), so no provider repeats the tag check. Data tagged with
the provider's own name that this version cannot understand counts as unsigned too.

### turns (the walk module)
`tau.providers._walk.turns(walk, ...)` · D15 · new

The rules every provider shares (`model.md` §10.5 to §10.7), applied once to a walk:
which nodes count (`Config` and `Custom` do not), which replies are runnable (a batch
with a `Results` right below it), a `Results` that answers no batch becoming a user turn,
a `Results` that does not answer its batch call for call failing the sample, and a stop
note (`length`, `interrupted`, `error`) for calls that cannot run. It returns
`SystemTurn`, `UserTurn`, `AssistantTurn(parts, runnable, note)` and
`ToolResults(results)`, where each `CallResult` is `(id, tool, text, error)` in call
order; a provider maps them to its own wire shapes. `call_markup`, `outcome_markup` and
`stop_note` are the one text markup, and `system_note` (`[system note: <text>]`) is the one
spelling of a `System` node after the walk's start, for a provider whose single system
instruction cannot hold it. Two options cover the rules only some APIs need, so each
provider says whether it asks: `structured` (every provider passes `bool(tools)`: a request that
offers no tools has no batch that can go as calls), and `text_from` (a provider's own rule, given
the turns, naming the index from which batches go as text; Anthropic's manual thinking). A
batch either of them demotes, or one whose call has `args_raw` that is not a JSON object (always
checked; no arguments counting as `{}`; `call_object`, strict JSON, refuses NaN, infinity and lone
surrogates), goes as text (`runnable` false), and its `Results` becomes a
`UserTurn` of `[tool result <id>: <text>]` lines, so a walk the API would refuse never fails
every later sample. `Messages(items_key)` builds the wire messages from the turns: neighbouring
turns of one role merge, a turn with nothing to send leaves none, and `System` turns before any
message are the instruction. The module is private to the package: its shapes may change with
the next provider.

### stop
*spike* `Stop` · retires `stopReason`, `AgentEndReason`

Why a reply ended: `End`, `Tools`, `Length`, `Interrupted`,
`Error`. A refusal is an `End` with the provider's raw reason kept. Only `Tools` makes calls runnable. The provider's own string is
`finish_reason`. Python's `Stop`, `Next`, and `Hook` are not integers:
`Stop.Tools != 1`, because equal-to-an-int with a different hash breaks sets.

### finish_reason
*spike* `Body::Assistant.finish_reason`, `Option<String>` · retires `stop_raw`

The provider's own finish string for a reply, verbatim (`"stop"`,
`"tool_calls"`, ...), kept beside the normalised `stop`. `None` when the
provider sent none: an interrupted reply, a failed sample, a fake.

### meta
*spike* `Body::Assistant.meta`, `Option<String>` · new

What a response carried besides its parts, as the text of a JSON object: for
the OpenAI provider the `model` the server says answered, the response `id`,
`system_fingerprint`, and `usage` (token counts), whichever the stream sent; every hosted
provider and the mock report the normalized `usage` `{input, output, cached}` (D15), beside
its raw fields (`usage_raw`). Opaque to core, like `Config.data`; persisted and replayed
verbatim. On an `Error` reply it holds `error: {message, status?, retry_after?, internal?,
provider?}`, the failed sample's `SampleError`, put there by the driver: `retry_after` only
when finite; `internal: true` and a message of `"<Type>: <msg>"` when the provider raised
something other than a `SampleError`; `provider` is an `error` value the provider's own `meta`
held. A call cut off by the failure is not saved; its raw text is in the message (D15, D1). `None`
when the provider had nothing to report, or the reply was interrupted. The
driver refuses a `Finish` whose `meta` is not a JSON object.

### call
*spike* `Part::Call` · retires toolCall block, `BlockedCall`

A part requesting one tool. `id` is the provider's, unique in its node.
`args_raw` is the argument text as produced, never re-serialised. Never a
node of its own.

### batch
a view (`tree::calls`) · new

The calls of an `Assistant` node with `stop == Tools` and at least one call.
Answered by one `Results` node.

### Results
*spike* `Body::Results` · retires toolResult, `Result` (per-call node)

A node answering a batch: one `(call id, outcome, by)` per call, in call
order, written at the join, `by: Tools`. Each outcome's `by` is `Tools` if the
tool ran unedited, else the extension whose `pre_tool` answered it or whose
`pre_result` last edited it (answered in `Answer::CallDone`). Final; another answer is a
sibling (Q10).

### outcome
*spike* `Outcome` · retires `is_error`, `Skipped`

`Ok(value)`, `Err(error)`, or `Interrupted` (an abort stopped the call or
kept it from starting; the tool may or may not have run).

### author
*spike* `Author` · retires `source`, `SubmissionSource`

Who wrote a node, or who asked for a move: `Frontend(client)`, `Provider`,
`Tools`, `Core`, `Extension(name)`. Filled in by the driver from the caller,
recorded only, never used for control flow (Q7). A pre hook that edits a draft
leaves its `by` as is; who edited a node is not recorded (accepted gap).

### time
*spike* `Node::time`, `Move::time` · retires `timestamp`

Wall-clock time the driver passed in. Core never reads a clock.

### delivers
*spike* `Node::delivers`, `Draft::delivers` · new

The item ids a node consumed. The only record of consumption: load drops any
item some node delivers.

### copied_from, derived_from
not yet spiked · new

Lists of nodes this node repeats verbatim (a compaction copy), or whose walks
its content was computed from (a summary, a sub-agent's answer).

### session
*spike* `Session` · retires `AgentSession`, `SessionManager`, catalog

One conversation: a forest of nodes, its cursors, labels, and scratchpad.
Spike fields: `tree`, `at` (cursor → node), `auto`, `items`, `next_item`,
`changes`, `moves`, `outbox`, `notices`. `Session::create(config, system,
time)` writes the root `Config` and a `System` node and opens the first
cursor (Q5). The nodes live in its `Tree`.

### Tree
*spike* `tree::Tree`, `Session::tree`; Python `tau.Tree` (`Session.tree`) · new

The append-only forest of nodes and the reads over it: `node`, `walk`,
`config_at`, `lca`, `children`, `node_count`, `deciding`, plus the private
`push` and `check` (V2) that `Session::append` and replay share. Spike fields:
`nodes`, `configs` (per node, its nearest `Config`), `kids` (per node, its
children). It knows no cursor, label, or scratchpad; `Session::next` and
`Session::deciding` take a cursor and ask `Tree::deciding` about its node.
The Python `Session.tree` is a frozen view that shares the session's lock:
`node`, `walk`, `children`, `len()`, `config_at`, `lca` (the reads that were
on `Session`).
Replay is the free functions `tree::load` and `tree::reconcile`, which
rebuild a `Session` and so stand where `Session::load` and `Session::reconcile` were.

### root, detached root
a view · retires session header, side-completion sessions

A node with no parent; always a `Config`, because it is made only by
`append_root`. Any root after the first is **detached**: one-off work outside
the conversation. Replay refuses a logged root that is not a `Config`
(`BadRecord`).

### side node
a view · new

A node no cursor's walk passes through. Nothing marks it.

### walk
*spike* `Tree::walk` · retires fold, path, splice

`walk(n)`: the nodes root → … → `n`, inclusive. Nothing spliced or filtered.
Its last node is its **tip**; seen as a path through the tree, it is a
**line**.

### LCA
*spike* `Tree::lca` · new

Lowest common ancestor of two nodes; none across trees.

### append
*spike* `Session::append` · retires `append_at`, write group

Writes a node under any parent without moving any cursor. Checks V2 only (I5).

### append_root
*spike* `Session::append_root`, `Machine::append_root`, `Session.append_root`,
`TreeHandle.append_root` · new

Writes a root `Config` (no parent) without moving any cursor. Takes a `Config`,
not a `Body`, so it has nothing to refuse.

### validate
not yet spiked

V2 (a `Results` node's outcomes, and an `Assistant` node's calls, name distinct
call ids) over a whole walk, for tests. `append` checks each node (`Tree::check`).

### Error
*spike* `tree::Error`

Core's refusals: `NoSuchNode`, `NoSuchCursor`, `Closed` (a call on a closed cursor; Python
`CursorClosed`, a subclass of `NoSuchCursor`), `NoSuchLabel`,
`DuplicateCall` (V2: a `Results` or `Assistant` node naming a call id twice; checked
on every append and write), `BadRecord` (load); a driver's: `WrongAnswer`
(an answer to no job: none is out, it is of another kind, or it
names a call that is not in the batch or already answered; nothing changes); and a frontend call's: `Busy`
(step on a busy cursor); a hook's: `NotInHook` (a hook's
move, write, or auto-step change on a cursor with no cursor hook job out), and
`NoSuchItem` (removing an item not in the scratchpad), and `NotPending` (a
write that delivers an item not pending for that cursor). Each becomes a typed Python exception of the same name in `tau-py`, and so does
each `tau_store::Error` variant except `Io` (the builtin `OSError`, with `errno` and
`strerror` set so CPython picks its subclass, and `filename` the session directory
for `create` and `load`) and `Core` (the
inner error). Each subclasses the closest builtin (`_core.pyi` has them all);
there is no common base class.

## The scratchpad

### scratchpad
*spike* `tree::Scratchpad { items, next_item }`, `Session::scratchpad`,
`Session::items`; Python `CursorState.items` · retires submission queue, steering queue, follow-up queue

A session's persistent, modifiable list of items, off the tree. A
**cursor's scratchpad** is the items addressed to it, oldest first. Anyone
may add, update, or remove any item; adding one wakes its cursor. Closing a cursor removes
its items, in the same sync as the `Close` record.

### item
*spike* `Item`, `ItemId`, `ItemData` · new

`{id, to, data}`. `data` is `Input { by, text }` (`by` an *author*) or
`Extension { name, data }`. Ids are never reused (`next_item`).

### input
*spike* `ItemData::Input` · new

What gives a cursor a `User` message: a frontend's line (`by` is `Frontend(client)`), or an
extension's `send` (`by` is `Extension(name)`, needing no client). The one item kind core knows.
The input phase commits it as a `User` node that delivers it, `by` the input's author, and
`pre_input` hooks see that author in `Draft.by`. Core does not check it: authors are recorded only
(Q7), and the handle fills in its own, so an extension cannot claim to be a frontend. A `pre_input`
halt removes the input and notifies only a `Frontend` author; an extension reads the reason from
the step's ending (`await tree.rest(cursor)` returns `Halted(reason)`). D17 §1.

### pending input
a view · new

A cursor's input items not yet delivered or removed.

### changes
*spike* `Session::changes` · new

Per cursor, a counter bumped by `add_item` and nothing else: removing an item
(`remove_item`) or delivering one (`write`) is not a change, and wakes nothing. A
step compares it with the value at its wake (`Step::seen`) to tell whether work
was added to the scratchpad.

### notice
*spike* `Session::notices` · new

A message to one client: `(client, text)`. Core adds one when `pre_input`
refuses an input. The driver drains them after every change and sends each as a **Notice** to the
client it names. (`cursor.tell` does not go through core: it sends its **Notice** through the
driver, to one client or to all.)

## Position

### cursor
*spike* `CursorId`; `{id, at}` · retires head cursor, acting cursor,
`TURN_CURSOR`, `TurnFrame`

An insertion point. Its only flag is auto-step. It carries no settings and
no queue. A cursor can be **closed** (D17; it was Q12): it keeps its id and its last node, and
leaves `cursors()`.

### at
*spike* `Session::at` · retires leaf, `pre_turn_leaf`

The node a cursor is on: the `to` of its latest move. Always a node (I3).

### open
*spike* `Session::open` · retires `spawn`, `newest`

Creates a cursor at a node with an opening move (no `from`). Opening wakes
it (Q32). `opened_by` is not yet spiked.

### move
*spike* `Move`, `Session::move_to`, `Session::moves` · retires navigate entry

The only way `at` changes, and its record `{cursor, from?, to, by, time}`.
Checks only that `to` is a node. Never looks below `to`.

### write
*spike* `Session::write` · new

Append under `at` and move onto it, as one record; then drop the delivered
items. Every commit, and a hook's `cursor.write`. Core checks the resulting
state: every `delivers` id must be pending for that cursor (else `NotPending`,
nothing written), so a write cannot take another cursor's item. The input
phase's commit delivers what the draft names: a hook that delivers the
offered item itself must also leave the draft `None`, else the commit names a
gone item and `write` refuses it (`NotPending`), failing the step.

### commit
*spike* (in `Step::answer`) · retires the plan's commit

A phase writing its draft with `Session::write`; a draft of `None` (cleared by a
hook) writes nothing, and the step moved no further than the hooks moved it.

### cursor.moves
*spike* `Session::moves_of`, `Step::moves`; Python `CursorState.moves`,
`CursorState.moves_since_step` · new

A cursor's move records; with `since_step=True` (`Step::moves`), only those
since the step woke (Q44).

### go to
not yet spiked · retires `navigate_tree`, `Session.rerun`

A frontend's move. Turns auto-step off and wakes the cursor (Q39). On a busy
cursor: abort, then move once the step rests; a hook that is running
finishes, and an open question is declined.

### re-run
Go to a batch's `Assistant` node and step: the tools phase runs it again and
writes a sibling `Results`.

### branch
a verb · retires fork (in-session), `commit_branch`

Move to a node neither on `walk(at)` nor below it. Going to an ancestor is
**going back**.

### fork
retires `ctx.fork` and other senses

A new session copying `walk(n)` from another. Never inside one session.

### label
*spike* `Session::set_label`, `remove_label`, `label`, `labels_of`,
`Record::Label` · new

A unique string naming one cursor; a cursor may have several. Set, moved,
and removed by **label records** `{label, cursor?}`; setting a label already
on that cursor writes nothing. A label stays on a **closed cursor** as its restart record; it
can be moved off or removed, but not set onto one (`CursorClosed`).

### machine
*spike* `machine::Machine` · new

The session plus every cursor's step: the frontend calls (`input`,
`step`, `go_to`, `set_auto`, `abort`, `close`, which is refused with `Busy` unless the cursor is at rest (D17)), the extension calls (`open`,
`add_item`, `remove_item`, `append`, `set_label`, `remove_label`), a hook's
own-cursor calls (`hook_move`, `hook_write`, `hook_set_auto`), and
`next_job`/`answer` per cursor. Each extension call takes `hook: Option<CursorId>`,
the cursor whose hook makes it: it syncs on return unless a cursor hook job
is out for that cursor (then the phase's sync covers it); a frontend or the
driver passes `None`. `abort`, `next_job`, `answer` check the cursor:
`NoSuchCursor`, or `Closed` for a closed one. Holds a busy go-to until its aborted step
rests (`going`). Each frontend call syncs on return.
What `tau-py` exposes; `Session`'s own mutators are for core and tests.
Also `set_label` and `remove_label` (which sync on return), `Machine::loaded(session,
now)` (a machine for a session just opened from a store: load is a wake, so
every cursor starts a step), and the `Session::notices` the Python `take_notices` drains.

### Python session
`tau.Session` (`tau-py`) · new

The machine plus its optional store behind one `Mutex`, so the class is
`frozen` and free-threading safe. `Session.create(config, system, now,
directory=None)` makes one (in memory, or in a directory with a store);
`Session.load(directory)` opens one. Every call takes the lock with the
GIL released; every call that changes the session then gives the store its
queued records before it returns, so Python never saves. An in-memory
session drops them. A failed save is a crash: the call raises the OS error
and the session closes itself (see **close**). Cursor ids are checked
by core: an unknown one is `NoSuchCursor`, a closed one `CursorClosed`. `close_cursor(cursor)` is
the core call (`Busy` unless the cursor is at rest); the waiting is the driver's. The value classes have a `__repr__`
(the Rust `Debug` text).
The calls are the machine's (**machine**) with these differences. `send` is
not one: it was `input` plus auto-step on, which a frontend does itself
(`input`, then `set_auto(cursor, True)`; `Driver.send` does both). The reads
are `tree` (a **Tree**, the nodes), `cursor(c)` (a **CursorState**, one
cursor), `cursors()` (the open cursors), `closed()` (each closed cursor and its last node, in id order), `label(l)` and `labels()` (the labels),
and `take_notices()` (drains the notices). There is no whole-scratchpad read
(`scratchpad()` repeated every cursor's `items`) and no `dropped()`.

### CursorState
Python `tau.CursorState` (`Session.cursor(c)`) · new

A frozen snapshot of one cursor, all read under one lock: `at`, `auto`,
`busy`, `next`, `items`, `labels`, `moves`, `moves_since_step`. Later changes
do not reach it, so `busy` and `at` cannot disagree. It replaces the separate
per-cursor reads (`at`, `auto`, `busy`, `next`, `items`, `labels_of`,
`moves_of`, `moves_since_step`). The lists are Python objects only when read.
A snapshot copies the cursor's items and labels, not its moves: `moves` and
`moves_since_step` are read from the session when asked (a move list is
append-only, so the snapshot keeps where it ended), which raises `Closed` once
the session is closed, and a move made after the snapshot is not in them.
Its `repr` shows the cursor, `at`, `auto`, `busy`, `next`, and the item and
label counts, never the session behind it. Not the hook's `Cursor` handle,
which acts; this only reads.

### close, Closed (the session's)
`Session.close`, `tau.Closed` · new

(Closing a *cursor* is **close (a cursor)**.)

Drops the store and releases the lease without writing what is not yet
synced, as a crash would. Every later call, a second `close` included,
raises `Closed`. A failed save leaves the session in the same state: the
store is gone with its lease, and reopening from disk is the recovery.

### Poisoned
`tau.Poisoned` · new

A call panicked inside the session's lock; the session raises it from then on.
Not core's: a binding-level guard so a core bug (a panic) is not followed by
more calls on possibly torn state. The call that panicked raises
`PanicException` (pyo3's, a `BaseException`); every call after it raises
`Poisoned`. Load the session again.

### close (a cursor), closed cursor, Close
*spike* `Session::close`, `Machine::close`, `Session::closed`, `Record::Close { cursor }`,
`Error::Closed` · D17 · new

Closing a cursor appends a **`Close { cursor }` record** to the log (a tombstone, with no `by` or
`time`) and takes the cursor out of play. A **closed cursor** keeps its id (ids stay dense:
`open` takes `at.len()`), its last node (still in `at`), its labels, and its moves. It leaves
`cursors()` and the clients' views, is not woken on load, and has no auto-step. Its pending items
are removed in the same sync, so none sits in the scratchpad addressed to a cursor that can
never take it. `Machine::close` is refused with `Busy` unless the cursor is at rest, and aborts
nothing: the driver aborts and waits first (**close_cursor**). `Session::closed()` lists
`(cursor, last node)` for each closed cursor, in id order. Every other call on a closed cursor
fails early with `Error::Closed(cursor)` ("cursor 3 is closed"), checked once in `Session::at`
(`input`, `send`, `add_item`, `step`, `set_auto`, `go_to`, `abort`, the hook calls, `next_job`,
`answer`, `cursor(c)`, `watch`, `rest`, a second `close`, and `set_label` onto it); allowed are
`label(l)` for a label on it, moving that label off, and `remove_label`. Replay applies the same
rule: a record after the `Close` that names the cursor, a second `Close`, or a `Close` of a
cursor never opened is `BadRecord`. `reconcile` drops items addressed to a closed cursor, which
covers a crash between the log's sync and the scratchpad's rewrite. The file `VERSION` is
unchanged: `Close` is an additive record. Closing the last open cursor is allowed.

### CursorClosed
`tau.CursorClosed(NoSuchCursor)` · D17 · new

The Python form of `Error::Closed`. A subclass of `NoSuchCursor`, so code that catches that
still works; the name and message ("cursor 3 is closed") say the id is real and its last node is
in `closed()`. Not `Closed`, which is the session's.

### close_cursor
`Client.close_cursor(cursor)`, `TreeHandle.close_cursor(cursor)` (async), `Session.close_cursor`
(the core call) · D17 · new

Abort the cursor's step as `abort` does (cancel the effect, stop its command, decline open
questions), wait for it to rest, then `Session.close_cursor`. If the cursor woke in between
(a sync tool on a worker thread added an item), core's `Busy` sends the driver round again; each
round aborts what woke it and waits for a rest, so it never spins. Anyone waiting on the cursor
sees the abort: a `rest` already waiting returns `Aborted`. A tree handle refuses its own
cursor (`ValueError`), as `rest` and `abort` do: closing it would wait on itself. Driver memory
for the cursor is dropped when the close lands (its view, last ending and hook error, and the
question decline mark). The name is not `close` because `Client.close()` and `Session.close()`
mean something else.

### reopen
`Client.reopen(cursor)`, `TreeHandle.reopen(cursor)` -> the new cursor id · D17 · new

`open(at=` the closed cursor's last node `)` plus moving each of its labels onto the new cursor.
Composed in the driver, not core. `ValueError` ("cursor 3 is open") if the cursor is open, `NoSuchCursor` if it was never
opened. The new cursor is opened by the client, or by the handle's extension.

### CursorGone
`tau.CursorGone(cursor, at)` (`events.py`) · D17 · new

The state event for a closed cursor: the client drops its `CursorView`, and `at` is the last node,
for restarting. A cursor has one state event at a time: its `CursorView` while open, its
`CursorGone` after. In a **Feed** buffer it replaces that cursor's `CursorView`. The connect
snapshot sends one per closed cursor, before `Labels`, so a label never names a cursor the
client has not been sent. There is no `Closed` or `Removed` delta on `CursorView`.

### busy, at rest
*spike* `Machine::busy`, `Step::resting` · new

A cursor is busy while a `Step` exists for it that is not `resting()`, or
while a go-to waits for its aborted step to rest (the go-to applies in the
next `next_job`). So a busy cursor always needs `next_job`, and a driver needs
no separate poll. Never stored; every cursor loads at rest.

## The step

### step
*spike* `Step` · retires turn (four senses), loop turn, agent run,
submission, cycle

One pass of the phase loop for one cursor, `pre_step` to `post_step`.
`Step::wake(session, cursor, asked)` starts one; `next_job` and `answer`
drive it; `abort` and `resting` are its other calls. Private state:
`cursor`, `pos`, `asked`, `seen`, `first_move`, `out` (a job is
handed out and not yet answered; which kind is `pos`'s), `draft`, `error`,
`ending` (no longer `Done` once aborted, so no later phase applies), `batch`.

### asked
*spike* `Step::asked` · new

The step was a frontend's `Session.step`: its phases run with auto-step off.

### phase
*spike* (positions of `Pos`) · new

Input, sample, or tools. Each: pre hook on the effect, effect, pre hook on
the draft, commit, post hook. Applies only if not aborted, auto-step is on
or the step was asked, and its condition holds at its start.

### Pos
*spike* `step::Pos` (private) · new

Where a step is: `PreStep`, `Input`, `PreSample`, `Sample`, `PreReply`,
`PostReply`, `Tools`, `Calls`, `PostResult`, `PostStep`, `Rest`.

### transition log
*spike* `tau_core::transitions`, cargo feature `transitions` · new

Off by default and absent from every wheel. With it on, each position change
of a step (`Pos` to `Pos`, the job handed out or the answer that caused it) is
written once per scenario to stderr as a `TAU-TRANSITION` line. It exists so
`scripts/diagrams.sh` can draw the step's position graph from what the tests
actually did (`docs/as-built/diagrams/`). It changes no behavior.

### next
*spike* `tree::next`, `Session::next`, `Next` · retires `LoopPosition`

What a walk needs: `Sample` (last message-bearing node is `User` or
`Results`), `RunTools` (a batch), or `Wait`. Never looks below `at`.
`Session::deciding` returns it with the node that decided it (the last node
that is not `Config` or `Custom`); a batch is read from that node, not from
`at`, which may be a `Custom` node written after the reply.

### job
*spike* `Job` · new

What the step asks the driver to do: `Hooks { point, draft, node, ending }` (the
subject is whichever of the three the point is about),
`Sample`, `RunCalls { node, calls }` (`node`: the `Assistant` node holding the
batch; the driver refuses an empty one), `Pending` (calls outstanding),
`Rest`. Only waits are jobs; writes never are. `Job`, `Answer`, `SampleEnd`,
`RunCall`, and `Hook` are exported from `tau`: `Session.next_job` and
`answer` are the job loop a driver runs, and a frontend of another kind can
run it too.

### retired handle
Python `TreeHandle.retire` · new

Python retires a handle when its hook coroutine returns, before the job is
answered. A retired handle can still read (`walk`, `items`, `children`, ...); it
cannot write at all: every write, `ask`, and own-cursor call raises
`NotInHook`. A node handle is retired when its tool hook returns. A handle
from an earlier job, step, or cursor is retired by then. This is the only
check of handle identity: core knows only whether a cursor hook job is out
for a cursor (`in_hook`).

### Answer
*spike* `Answer` · new

What the driver answers with: `Hooks { stop, draft }`, `HookFailed(error)`,
`Sampled { parts, end }`, `CallDone { call, outcome }`. Each is accepted only while the job it answers is out; any other answer is `WrongAnswer`.

### SampleEnd
*spike* `SampleEnd` · new

How a sample ended, as the driver saw it: `Finished { stop, reason, meta }`
(the provider's finish), `Failed { error, meta }`, or `Cancelled` (the cursor
was aborted). Answered with the parts streamed so far in `Answer::Sampled`.
The driver reports what happened; core decides what the tree records: a
finish is the reply, a cancel with parts an `Interrupted` reply, a failure
with parts an `Error` reply (and the step fails), and a cancel or failure
with no parts writes no reply (a failure leaves a `sample_error` side node).
With parts, the driver puts the failure into the `Failed` `meta` as
`{"error": {"message", "status"?, "retry_after"?}}` (over any `error` the
response `meta` had), so the `Error` reply says why. Core still treats `meta`
as opaque.

### RunCall
*spike* `RunCall` · new

One call handed out in `Job::RunCalls`: `{id, tool, args_raw}`.

### draft
*spike* `Draft` · retires the plan's draft under `target`

What the current phase will commit: `{body, delivers, by}` (the constructor
takes them in that order). Carried in a
`Hooks` job; the edited one comes back in `Answer::Hooks`, and a hook that leaves
none (`None`) means commit nothing. A halt discards it; an abort commits it. Python subjects: input draft, sample draft, reply
draft, call draft (Phase 4).

### answer
A pre hook supplying the effect's result so it does not run: a reply from
`pre_sample`, an outcome from `pre_tool`. The same relation as `Answer` to a
job, one step earlier: the result stands in for the effect. (A question's reply
is a *response*.)

### effect
The outside work of a phase: the sample, or running the calls. Abort
cancels only effects.

### join
*spike* (`Step::join`, private) · new

When every call of the batch has an outcome: core writes the `Results` node
in call order and moves onto it.

### ending
*spike* `Ending` · new

How a step ended, the subject of `post_step`: `Done`, `Halted(reason)`,
`Failed(error)`, `Aborted`.

### halt
A cursor hook returning a string: later hooks at that point skip, the draft
is discarded, auto-step goes off, `post_step` gets `Halted`. In `pre_input`
the item is removed and its client gets a notice.

### rest rule
*spike* `Step::after` (private) · new

After `post_step` with a `Done` ending and work left (auto-step on and
`next != Wait`, or input pending): the next step starts at once if the cursor
moved since the step woke (`cursor.moves(since_step=True)`), else auto-step
goes off and the cursor rests. A cursor never tries the same thing twice
(model.md §6, with John's ruling). An item added since the step woke starts a
step regardless; that step answers to the same rule.

### wake
*spike* `Step::wake` · new

Start a step on a cursor at rest: an item added, opening, load, a go-to,
`Session.step`, auto-step turned on. Load: `Machine::loaded` wakes every
cursor of the loaded session (auto-step is off, so `pre_step` and `post_step`
only); `Session.create` does not wake its first cursor.

### auto-step
*spike* `Session::auto` · retires step mode, suspension, `api.suspend`,
responder

A cursor's only flag. On: phases run and steps follow one another while
there is work. Off: a woken cursor runs only `pre_step` and `post_step`. Goes
off on abort, halt, failure, go-to, `StepLimit`, a step that did not move the
cursor (the **rest rule**), and load. Any reader sees the change the same way:
`Session.auto(cursor)`, `cursor.auto` in a hook.

### StepLimit
`tau.StepLimit` (`step_limit.py`) · extension, retires `Config::max_steps`
and `Session::taken`

`StepLimit()`: an extension that guards loops where the cursor does move
each step (D1). Its `limit` is its option (`step_limit.Options`, `limit` default 50, at least 1;
D16 §6): `extensions.step_limit.limit` in the config files, or `/config step_limit` for a
branch; a bad one fails the step at `pre_step`. It keeps its own count per cursor (core keeps none), read
and changed at three hook points with `cursor.auto`: `pre_input` sets it to 1
(the step that takes an input is the first; there is no `post_input`),
`pre_step` adds one while auto-step is on, and `post_step` turns auto-step off
once the count reaches `limit`, then starts the count over, as it also does on
seeing auto-step off. Whoever resumes the cursor gets another `limit`.

### partial_calls
not yet spiked · `Config` field

`text` (default) or `omit`: how a never-run call is rendered on resend (Q27).
The `text` markup is `[tool call <id>: <tool> <args_raw>]` (see `OpenAIProvider`).

### abort
*spike* `Step::abort` · retires `AbortSignal`, abort-tree, `rollback`

Esc. Auto-step off, no later phase; returns whether the driver must cancel
the effect in flight. Never cancels a hook.


### in_hook
*spike* `Step::in_hook` · new

True while a cursor hook job is out and not yet answered. A hook's own-cursor
calls (`Machine::hook_*`) are allowed only then, and a call made with
`hook: Some(cursor)` skips its sync only then; a frontend's or the driver's
call always syncs. A go-to or abort during a hook, cursor or tool, aborts
the step and the hook finishes (Q34).
### resume
What a crash mid-batch leaves: the cursor at rest at the unanswered batch,
every outcome lost (D4). Input goes under it; a step re-runs it.

### reconcile
*spike* `Session::reconcile` · new

What load does after replay: every open cursor's auto-step off, and every
delivered item dropped, and every item addressed to a closed cursor (I7).

### load
*spike* `tree::load(records, scratchpad)` · new

Reload (I7): replay the log's records in order, take the scratchpad file,
`reconcile`. Pure; the store reads the files. A record that cannot apply (a
node id or cursor out of sequence, a move from a node its cursor is not at)
is `Error::BadRecord(position)`; so is a logged root that is not a `Config`.

### sample, context, message
retires completion, model call, `ExtensionContext`/`ctx`, the `context`
hook, message entry, `AgentMessage`

A **sample** is one provider request over the **context**, `walk(at)`, with
`config_at(at)` (I1). A **message** is the provider's wire unit, never stored.

### config
*spike* `tree::Config`, `Body::Config`, `Tree::config_at` · retires config
entry, `CONFIG_KEYS`, `Settings`, model entry

How a step runs. Its data is a JSON object with at most two keys: `"model"`, a qualified
name, and `"extensions"`, `{"<extension>": {"<option>": value}}` (D16 §4). It names a model
and holds nothing resolved from the config files (no URL, no key variable, no settings;
D13). A node holds just what it changes: `Cursor.set_model(model)`, `Cursor.set_options(**changes)`
and `model_config(model=None, extensions=None)` (`tau._config`, the one builder) build it, and `config_fields(config)` reads it back, raising `ConfigError` for any other shape (a
key other than those two, a `model` that is not a non-empty string, `extensions` that are not
an object of objects). `config_at(n)` is the nearest `Config` on `walk(n)` (D5), recorded per
node when it is added (`Session::configs`), so it costs the same at any depth; core reads
nothing in it. The driver therefore **folds the walk** itself: the model is the last one
named on it (`ExtensionOptions.model(node)`; `ConfigError`, so a failed sample, if none names one), and
an option is the last one set on it (`ExtensionOptions`), each node adding to those before
it. The model resolves in the `ModelCatalog` at each sample.

### ModelConfig, ConfigError
`tau.ModelConfig`, `tau.ConfigError` (module `tau._config`) · new

The **resolved model**, built by the driver at each sample from the
`ModelCatalog` and the name in `config_at(at)`; no node holds it. Fields:
`model` (the name as its API spells it), `base_url` (optional; an http or https
URL; a provider that needs one says so in its `check`), optional `api_key_env`,
the *name* of the environment variable holding the API
key, `provider` (default `"openai"`; a name in the driver's provider map), for a
literal `api_key` in a file `literal_api_key` (hidden from `repr`), `max_tokens`
(optional positive integer) and `options` (see *options*; stored as a deep read-only
copy: objects become read-only mappings, lists tuples). Invalid values raise
`ConfigError` (a `ValueError`), which also reports a `Config` that is not a
model name and a name the catalog lacks (`model 'x' is not in your config`);
the driver turns those into a failed sample. `api_key(environ)` reads the key
at sample time (the provider's environ); it is never a field, so it reaches no
node and no log (tested). Groups, merging, and files are `read_models`.

### options
`ModelConfig.options`, the model key `options` · D15 · new

A JSON object only the named provider reads (a thinking budget, `delay_ms` for the
mock). Layers merge it like any object, and a group's passes down to its models
and merges with theirs; a `null` inside drops that key. The loader checks only that
it is an object; the provider's `check` checks what is inside, so a misspelt option
fails `tau config check`. The driver also calls `check` before every sample and fails the
sample with the problems listed, so a bad option never reaches `sample`. An `options` that
is not an object is a problem at its key, whatever the group above it holds.

### api_key_env
`ModelConfig.api_key_env` · a model's key in the config files

The name of the environment variable that holds the API key. Unset or empty
when named is a `ConfigError`, which the provider reports as a failed sample.
No variable named: no `Authorization` header.

### user config, project config, TAU_HOME
D13 · retires 0.15's `~/.tau/config.json` reader

The **user config** is `$TAU_HOME/config.json` (`TAU_HOME` defaults to
`~/.tau`); tau owns it and may rewrite it. A **project config** is
`.tau/config.json` in a trusted directory. Layers, nearest wins: user,
project, CLI flags.

### group, qualified name
D13 · new

A **group** is a config object with a `models` object; its other keys apply to
every model inside, and groups nest. A model's **qualified name** is the path
of group names and its own name joined by `:` (`free_tokens:gpt-6-sol`).
Names may not contain `:`. A `Config` node stores a qualified name; a bare
name is only a search term.

### read_models, check_config, ModelCatalog, ResolvedModel, Origin, ConfigProblem
module `tau._config_layers`; all but `Origin` exported · D13 · new

`read_models` is the catalog without files or trust: how a program that embeds
the driver builds a `ModelCatalog` from a mapping (`read_models(cli=...)`). A
frontend that reads files calls `load_config`. `Origin` is internal: it is only
the item type of `explain`, read by attribute (`key`, `source`, `group`).

`read_models(user, project, cli)` reads the layers (each may be absent; `cli`
is a mapping in the file's JSON shape), merges their raw trees (objects by
key; lists and scalars replace; `null` drops, including a key a group passes
down), then resolves every model. It raises `ConfigError` listing every
error; it finds and trusts no file. It returns a **`ModelCatalog`**: `models`
(qualified name to **`ResolvedModel`**: the `ModelConfig` and one **`Origin`**
per key, the layer, file path or `"cli"`, and the group that set it, `None`
for the model itself), `default_model`, `warnings`, and `extensions` (the merged `extensions.<name>` objects of the
layers, per option; checked by `check_config`'s `extensions`, not by the loader: it does not know the
extensions), `load` (the merged top-level `load`: source key to `true` or `false`, a `null` dropping an
inherited key) and `load_sources` (the layer, file path, that set each `load` key; see *load*).
`find(name)` returns the
qualified names matching a qualified or bare name (0, 1, or many: the caller
decides), `resolve(qualified)` the `ModelConfig`, `explain(qualified)` the
origins. `check_config(user, project, cli, environ, *, providers, extensions=())` (`providers`: the
driver's map of name to provider, required) accepts exactly those provider names and
adds each provider's `check(model)` problems for every model naming it (at the
model's path, as `model 'x' (mock): ...`). It returns every
**`ConfigProblem`** (severity `error` or `warning`; file, dotted path, and
for JSON syntax errors line and column) instead of raising: syntax errors,
duplicate keys, unknown keys with a suggestion, `:` in a name, a missing
`model`, wrong types, an unknown `provider`, a bad `max_tokens` or `options`, a `default_model`
not in the config, 0.15's `backend` key, and as warnings a literal `api_key`
and (given `environ`) an unset `api_key_env` variable. A literal `api_key`
is held only in `ModelConfig.literal_api_key` (hidden from `repr`) and never
in a problem, origin, or catalog text. `api_key` and `api_key_env` are one
setting: the nearer one wins and drops the other. The first layer to define a name sets whether it is a model or a group; a later layer
giving `models` to a model is an error, and otherwise only tunes it. a `base_url` that is not an http(s) URL with a host (including one `urlparse` rejects) is an error at that key. `provider` unset shows in `explain`
with source `"default"`. The top level takes `models`, `default_model`, `extensions` and `load`;
frontend settings will widen it in a later part.

### trusted directory, trusted.json
D13 · new

A project directory whose `.tau/` the user approved, listed in
`$TAU_HOME/trusted.json` with a hash of its `.tau/config.json`, of its `.env`, of every file under
`.tau/extensions/` (`extensions_sha256`) and of `.tau/SYSTEM.md` (`system_sha256`) (D19 §4);
a changed hash is untrusted again, and so is adding the first extension file or a `SYSTEM.md`. The
project is the nearest `.tau/` upward from the
working directory, stopping at `$HOME`. Only a trusted directory's project config, `.env`,
extension code and `SYSTEM.md` are used, and what is used is the bytes that were approved. Approval also makes `.tau/` the user's, mode `700`, with a
`.gitignore` of `*`.

### safe_block, safe_text, user_config_path
module `tau._trust`; only `safe_block` is exported · D13 · new

`safe_block` is exported for a frontend that prints an exception from config
loading (the examples do): the exceptions carry file text unescaped. `safe_text(text)` (internal) writes every control, format (such as U+202E), line/paragraph separator and surrogate character as an escape (`\x1b`, `\u202e`; a byte that was not UTF-8 in a name as `\xff`), so output always encodes; `safe_block(text)` does it line by line, splitting only on `\n`; everything printed from config
data goes through it (the trust report, `tau config check`, `--show`, suggestions, the pick menu). Model
and group names may not contain control characters (the checker errors at that path). `user_config_path(home)` is
`<home>/config.json` or `None`.

### tau_home, find_project, ProjectSearch, refuse_special, create_project, project_config_path, ensure_trusted, Trusted, UntrustedProject, TrustError, is_interactive, terminal_ask, project_environment, LayerText
module `tau._trust` (`LayerText`: `tau._config_layers`); only `Trusted`, `UntrustedProject`, `TrustError` and `LayerText` are exported · D13 · new

Everything here but those four names is internal: the outside reaches it
through `load_config` (finding, trusting, `.env`), `tau trust` (approval and the
report), and `tau config check`. Tests do the same.

- `tau_home(environ)`: `$TAU_HOME` or `~/.tau` (`~` from `environ["HOME"]`).
- `find_project(start, environ)` returns a **`ProjectSearch`** (`root`, `skipped`): the
  nearest directory upward whose `.tau/` is a real directory owned by you, or `root` of
  `None` and in `skipped` why the nearest `.tau` the one walk passed over is not a
  project (a symlink, or not owned by you); `tau trust` says that instead of making a
  project when the `.tau` is `DIR/.tau` itself, and makes it as usual when it is further
  up (and removes the `.tau/` it made if approval fails). A symlinked or foreign `.tau` is skipped and the search goes on.
  `$HOME` itself and the directory whose `.tau` is `TAU_HOME` are never a project; the
  search stops at `$HOME` (outside it, runs to `/`). `refuse_special(path, environ)` is
  `TrustError` for `$HOME`, `TAU_HOME`, anything inside it, and the parent of a `TAU_HOME`
  named `.tau`. `create_project(root)` makes `<root>/.tau/` (mode `700`).
  `project_config_path(root)` is `<root>/.tau/config.json`.
- `trusted.json` (in `TAU_HOME`, written atomically) holds per project the sha256 of the
  approved config bytes and of the approved `.env` bytes (each behind a marker byte, so an
  absent file differs from an empty one), which decide, and a redacted copy of the config
  (every `api_key` value replaced by `(secret)`, canonical JSON) for diffs. D19 §4 adds
  `extensions_sha256` (the digest of the (path, sha256) list of every regular file under
  `.tau/extensions/` except `__pycache__/`, with the marker for a missing directory), `extensions`
  (the per-file map, for diffs) and `system_sha256` (`.tau/SYSTEM.md`, with the marker).
- `ensure_trusted(root, home, *, ask)` returns a frozen **`Trusted`** (`root`,
  `config_text`, `env_text`, `extension_files` (the approved files under `.tau/extensions/`,
  relative path to bytes; `None`: no such directory), `system_md` (the approved bytes of
  `.tau/SYSTEM.md`, `None`: none; what D20's assembly reads, never the file), `layer()` as a `LayerText`, and `read_models(user=, cli=)`
  returning the catalog from the approved text; callers use these, never
  `project_config_path`, which is for display) if both hashes match. Otherwise it builds
  the report, the text a user reads before agreeing: the project config itself with
  `api_key` values blanked, the variable names (never values) its `.env` sets, the `.tau/`
  checks (mode, `.gitignore`), the files under `.tau/extensions/` with their sizes and the size of
  `SYSTEM.md` (never any code), and for a changed project a unified diff of the redacted
  configs ("a secret value changed" when only a secret did), ".env changed since you
  approved it", and which extension files were added, removed or changed and whether `SYSTEM.md`
  changed. With no `ask` (no live terminal), or on a no, it raises
  **`UntrustedProject`** (message: the report and `tau trust <root>`, the path
  shell-quoted, the command left out when the config or `.env` has errors to fix first;
  `layer`: the config it holds, to show its problems). On a yes it sets
  `.tau/` to `700`, writes `.tau/.gitignore` (`*`, mode `644`) if missing, and records the
  hashes. With an `ask`, a config or `.env` with errors cannot be approved: **`TrustError`**
  (a `PermissionError`, with `where`: the path it is about, if any) is every other refusal.
  `terminal_ask(stdin, stdout)` and `is_interactive(stdin, stdout)` are the terminal side.
  `project_environment(trusted, environ)` overlays `environ` on the approved `.env` of a
  `Trusted` project (`None`: just `environ`). A **`LayerText`** is a config layer already
  read (source and text), accepted where a layer takes a `Path` (`read_models`, `check_config`); it is exported because `UserConfig.project` and `Trusted.layer()` are one.

Rules. (A) The config and `.env` are read once per `ensure_trusted`; report, question, and
approval use those bytes, and approval refuses ("changed while you were
reading; run again") if either differs. (B) Everything read inside a project
goes through one helper that refuses symlinks and non-regular files (naming the
path, never the content): it opens with `O_NOFOLLOW` and checks the descriptor, with no
earlier look to race. (C) As above: hash decides, redacted copy shows. (D) A config
with errors (or a malformed `.env`) cannot be approved; the report lists them.
(E) `.env` is content-pinned like the config; content decides, so neither file has an
owner check. (F) A foreign `.tau` directory is not a project. (G) `TAU_HOME` or `trusted.json`
writable by group or others refuses all trust operations with the `chmod` to
run; tau never changes them. (I) Every project-supplied string in a report or
trust error is shown with control characters (C0, DEL, C1) as `\xNN` escapes. (J) An `OSError` in trust's file
access (a mode-000 `config.json`, `.tau/` or `TAU_HOME`, a read-only `TAU_HOME` that cannot
take the approval) is a `TrustError` naming the path, never a traceback. (K) A record in
`trusted.json` from before `.env`, `.tau/extensions/` or `.tau/SYSTEM.md` was pinned (no
`env_sha256`, `extensions_sha256` or `system_sha256`) is not an approval: the project is untrusted,
and the next approval replaces the record. (L) A symlink, FIFO or oversize file under
`.tau/extensions/`, or a symlinked `SYSTEM.md`, is a `TrustError` naming it (rule B).
(J) `config.json` and `.env` over 1 MiB are an error naming file and size; files are
opened non-blocking, so a swapped-in FIFO cannot hang. (H) A `.gitignore` without a line of exactly `*`,
or with a `!` line, is a `[warn]` in the report, neither blocked nor rewritten.

### parse_dotenv, DotenvError
module `tau._dotenv`; only `DotenvError` is exported · D13 · new

Our own `.env` reader: `KEY=value` lines, optional `export `, `#` comments (a
`#` starts one only at line start or after whitespace, so `K=#x` is `#x`),
single-quoted values literal, double-quoted values with `\n`, `\"`, `\\`,
unquoted values trimmed of a trailing ` # comment`; a BOM is tolerated. Only `\n` (or `\r\n`)
ends a line, so a quoted value may hold `\x0b` or U+2028. A
malformed line is a `DotenvError` naming the file and line, never the value.

### load_config, LoadedConfig
module `tau._startup` · D13 · new

What a frontend calls before its first sample. `load_config(cwd, environ, *, ask,
cli=None)`: `tau_home(environ)`, the user config `<home>/config.json` (absent is
fine; `user_config_path(home)`, in `tau._trust`, is that path or `None`), `find_project(cwd)`, and
for a project `ensure_trusted(root, home, ask=ask)` (no `ask`, no live terminal:
`UntrustedProject`), then the catalog from the approved project text and the
user file (`cli` overrides on top), and the environment from
`project_environment`. Any `ConfigError` or `TrustError` propagates. It returns a
frozen **`LoadedConfig`**: `catalog`, `environ` (the shell environment over the
trusted project's `.env`; the shell wins; pass it to the provider so keys come
from it), `project` (the `Trusted`, or `None`), `home`, and `user`, the **`UserConfig`**
for `Driver(user_config=)` (see *UserConfig*).

### UserConfig
`tau._user_config` (exported) · D1 · new

The one config file τ writes: the user's `<TAU_HOME>/config.json` (decision 4: τ owns it, D13).
A frozen record of the layers a frontend loaded (`home`, the approved `project` layer as a
`LayerText` or `None`, the `cli` overrides), so that after a write it can read them again.
Two operations write. `save_load(key, on)` (D19 §3) sets `load.<key>` in the user's file the same
way (read, merge, atomic write; the file's other content stays) and returns the catalog, whose
`load` shows whether a nearer layer overrules it; it does not know the sources, so whether `key`
names one is the caller's (`tau ext enable`/`disable`). `save_options(changes, check)` sets `extensions.<name>.<field>` for each
field of `changes` (extension, then field, to JSON value) in the user's file: it reads the file
(`ConfigError`, in the loader's words, if the loader would refuse it; an absent file is `{}`),
sets the fields, merges the layers (`read_models`), calls `check(catalog)` (which raises
`ConfigError` for options the extension's options type refuses), and only then writes
atomically (`tau._trust.write_atomically`, shared with `trusted.json`: a scratch file in the
same directory, flushed, renamed over the file; a new file and `TAU_HOME` are private, `600`
and `700`). It returns the new catalog. Any failure before the rename leaves the file as it
was. The file is re-serialised as JSON with two-space indent: every key other than the ones
written is kept with its value and order, whitespace is not. The project layer has no write:
its path is not a parameter of anything, and an edit τ made would have to re-record the
trust hash, which waits for a trust rule. A `Config` node means this branch; the file means
every session (a node's value still wins on its branch). `UserConfig` is the whole of how τ
reaches the file; the ways in are `tau config set`, `tau ext enable`/`disable`,
`/config <extension> ... --save` and `Cursor.save_options` (see *tau config set*, *tau ext*,
*commandline*, *Handles*).

### select_model, Pick, terminal_pick, Prompts, prompts
module `tau._startup`; `terminal_pick` is internal (reach it as `prompts(...).pick`) · D13 · new

`select_model(catalog, requested=None, *, pick)` returns one qualified name:
`requested` (`--model`), else `catalog.default_model`, else a `ConfigError` that
says to pass `--model` or set `default_model`. A name with `:` is exact; a bare
name with one match is that match; with several, `pick` (a **`Pick`**: a callable
from the qualified names to one of them) chooses, and with no `pick` the
`ConfigError` lists them; with none, the `ConfigError` suggests close names
(`difflib`) and lists what exists. `terminal_pick(stdin, stdout)` numbers the
names and reads a number or a name; anything else is a `ConfigError`.
**`Prompts`** (`ask`, `pick`) is what a person can be asked; `prompts(stdin,
stdout)` returns both as `None` unless both streams are terminals.

### tau config check, tau config set, tau trust
`tau.cli` (`main`; the `tau` console script) · D13 · new

`main(argv, *, stdin, stdout, stderr, environ, cwd)` is the `tau` command; each
argument defaults to the process's. Exit status 0 success, 1 a problem, 2 usage
(`tau` alone prints help to stderr, `tau config` alone the help for `config`), 130 Ctrl-C
(no traceback). Anything printed from config data is escaped with `safe_text`. A `ConfigError`, `TrustError`, or
`DotenvError` is one `tau: message` line on stderr and exit 1, no traceback.

`tau config set EXTENSION.FIELD=VALUE ...` writes each into the user's file through
`UserConfig.save_options`, all or none: a value is read as `/config` reads it (`commandline.value`
by the option's schema), an unknown extension or field is a `ConfigError`, and every
extension's options are built with the changes before the file is written. It knows the
extensions a start would load (`load_extensions`: the built-ins, packages and drop-ins, and a
trusted project's own; a failed source is its start error), and checks the user layer by itself
(the project is never written, so it works from any directory, trusted project or not).
It prints `set <assignments> in <path>`, exit 0.

`tau config check` runs `check_config` (with `builtin_providers`) over the user config
and the project config and prints each problem as `file:line:col: severity: message` (the
dotted path before the message when there is no line). The project's problems
are reported even if it is not trusted, each tagged `[untrusted project: ...]`; an untrusted
project is never used, and it is itself an error (startup would refuse it), even with no
config file. A refused file (symlink, not a regular file) is reported under its own path;
`tau trust` shows a malformed `.env`. A trusted one is checked from the approved
text, and its `.env` counts toward unset-variable warnings. Exit 1 if any error,
0 otherwise (warnings print). It then loads the extensions as a start would (`load_extensions`; a
failed source is an error printing the start's message, and a `load` key no source has is a warning
when `false`) and checks the `extensions` options of those it loaded (`option_problems`, so
`extensions.step_limit.limit: 0` is an error).
`--show MODEL` (after a check with no errors; warnings are fine) selects as
`select_model` does and prints the qualified name, then each resolved key, its
value, and `[file or cli or default, group G]`. A literal `api_key` shows as
`(set)`; no key value is ever printed.

`tau trust [DIR]` finds the project by walking up from DIR (a relative DIR is
taken from the working directory; default: the working directory), as startup
does. It refuses `$HOME`, `TAU_HOME`, anything inside `TAU_HOME`, and (when `TAU_HOME`
is named `.tau`) its parent, as the start directory. If no `.tau/` is found it makes
`DIR/.tau/` (mode `700`) and trusts that; a symlinked or foreign `.tau` passed over is
a one-line "no project: ..." error instead. One read: it goes through `ensure_trusted`
with an `ask` that prints the report and says yes. An already trusted, unchanged project
prints `already trusted: <root>` and exits 0; otherwise it prints the report and
`trusted <root>`; it is the explicit command, so there is no y/N prompt. A
config with errors cannot be approved: the report and the refusal go to stderr,
exit 1.

### Extension loading: source, key, drop-in, shadowed, load
module `tau._extensions` (private; the names below are exported from `tau`) · D19 · new

A **source** is one place extension code comes from; its **key** is the name `load` and `-e` use
for it, and it obeys the extension-name rule (letters, digits, `_`, `-`). There are five kinds, in
**load order**: a **built-in** (a class in `tau`, fresh instances at each load: `step_limit`, key
its name); an installed package's registration in the `tau.extensions` group (key the registration's
name; it must name a module, not `module:attr`); a user **drop-in** (`$TAU_HOME/extensions/NAME.py`
or `NAME/`); a project drop-in (`.tau/extensions/NAME.py` or `NAME/`, imported from the bytes trust
approved); and `-e`. Within a row, sorted by key; names starting with `.` or `_` and anything that
is neither a `.py` file nor a package directory are ignored. Every source but a built-in is read by
one rule (C3's): a module with a `str` `name` is the extension, otherwise its `extensions` is a
non-empty list or tuple of extension objects, added in order; anything else is an
`ExtensionLoadError` naming the source and the rule. A file is imported under a private module name
derived from its resolved path; a package's module under its real name. **Shadowed**: for one key
the nearest row wins and the others are never imported (the project's `plan_mode.py` over the
user's; `-e ./search.py` over the `search` package); two sources of one key in the winning row are
an error naming both.

**`load`** is the top-level config key, an object from source key to `true` or `false`, merged like
every object (nearest layer wins, `null` drops). It lives in the files only (a `Config` node
cannot set it). A source is on unless `load` says off (or, for packages and drop-ins,
`--no-extensions`); `"KEY": true` where no source has the key stops the start, `false` there is
allowed and `tau config check` warns.

`load_extensions(loaded, *, explicit=(), discover=True) -> list[Extension]` is the one entry point
every frontend calls after `load_config` and before opening a session: the built-ins `load`
leaves on and, with `discover`, the packages and drop-ins it leaves on (the project's only if
trusted), then `explicit` (`-e` values: a path if it has a `/` or ends in `.py`, else the key of
a discovered source, which loads whatever `load` says; naming one is the consent and needs no
trust). `absolute_explicit(values, cwd)` makes the paths absolute against `cwd` (a program that
changes directory calls it first). Each source is imported and given the driver's definition
checks (extension names, tools, commands, options types) so a failure names its source; then the
checks across sources (one extension name, one tool name) and the `load` keys. **Every** failure
is reported, as one **`ExtensionLoadError`** (an `ImportError`; `sources` the failed
`ExtensionSource`s, the first underlying exception `__cause__`) whose message is, per failure,
`extension "KEY" failed to load; tau did not start.`, the `source:`, the traceback lines in the
source's own files, the exception, and the one command that gets past it (`tau ext disable KEY`,
or "fix it or drop the flag" for `-e`). The program exits 1 before it opens or writes a session
(a failed discovered source stops the start: 0.15 only noticed). The driver adds `tau` first; a
program adds `session_name` (D18) after `load_extensions` returns.

`list_extensions(cwd, environ, *, explicit=(), discover=True) -> list[ExtensionSource]` imports the
same and returns one **`ExtensionSource`** per source in load order, `tau` first: `key`, `state`
(`always`, `on`, `off`, `shadowed`, `failed`, `untrusted`: a project that is not trusted is never
asked, and imported not at all), `kind` (`built-in`, `package`, `user`, `project`, `explicit`, or
`load` for a `load` key no source has), `origin`, `detail` (why not on), and for a source that is on
the `extensions`, qualified `commands` and `tools` it provides. It is not served over the wire.

### tau ext, -e, --no-extensions
`tau.cli` · D19 · new

`tau ext list [-e VALUE] [--no-extensions] [--json]` prints a row per `ExtensionSource` (key, state,
origin) then indented lines: the reason, or `commands: ...; tools: ...` (a command bare where one
source has it, else `ext:name`); `--json` is one JSON object per source per line (the dataclass's
fields). Exit 1 if any source failed. `tau ext enable KEY` / `tau ext disable KEY` write
`load.KEY` into the user's file (`UserConfig.save_load`) and say so; a key no source has is a
`ConfigError` listing the keys and writes nothing; a project layer that sets the key the other way
is said to win, and its file is never written. **`-e VALUE`** (also `--extension`, repeatable) and
**`--no-extensions`** are `extension_flags()`, a parser a program passes as `parents=` and reads as
`load_extensions(loaded, explicit=absolute_explicit(args.extension, cwd), discover=not
args.no_extensions)`: `-e` names a file, a package directory or a discovered key and loads it even
if `load` or `--no-extensions` turns it off; `--no-extensions` turns off packages and drop-ins but
not built-ins; `-e x.pyc` is refused.

### approved files (`tau._project_code`)
module `tau._project_code` (private) · D19 · new

`ApprovedFiles` is the import finder of one project source: it serves the module's source from the
bytes `Trusted.extension_files` holds (compiled under the file's real path, so a traceback names it)
and refuses a module not among them, even if a file by that name has appeared. A file changed after
approval cannot slip in; relative imports work; nothing writes `__pycache__` into the project. Data
files an extension reads at run time are pinned at start, not re-checked on each read.

## Hooks

### hook
retires the 0.15 hooks, handler policy, chain

A Python function an extension registers at one hook point. `None` carries
on; a string stops. Hooks at a point run in `config_at(at)` order.

### hook point
*spike* `Hook` (the seven cursor points, exported from `tau` as `Hook`) · new

`pre_step`, `pre_input`, `pre_sample`, `pre_reply`, `post_reply`,
`post_result`, `post_step` (cursor hooks, each a `Job::Hooks`), and
`pre_tool`, `pre_result` (tool hooks, run by the driver inside `RunCalls`). Python
names them as strings: `protocols.CURSOR_POINTS` and `protocols.TOOL_POINTS`. An
extension attribute named `pre_*` or `post_*` that is not one of these is refused when the
driver is built (see **check_extensions**), so a misspelt hook cannot sit silent. The
rule is on the name, not the kind: a dataclass field `pre_prompt` is refused too. Name such a
field for what it holds (`prompt_prefix`), not with those prefixes.

### cursor hook, CursorHook
`async def hook(cursor, subject) -> str | None`; `tau.CursorHook` is its `Protocol` · new

A string halts the later hooks of the point. An exception, or a return that is not a `str`
or `None`, or a `subject.draft` left as something that is not a `Draft`, fails the step:
`HookFailed("Type: msg")`, the traceback on stderr (never the log). The driver lives.

May move its cursor, write under `at`, set auto-step. Core allows these only
while a cursor hook job is out for that cursor (`in_hook`):
`Machine::hook_move`, `hook_write`, `hook_set_auto` raise `NotInHook` otherwise.
A handle kept from an earlier job is retired (see **retired handle**). They apply at once and sync
nothing; the phase's sync covers them. `set_auto(True)` in a step that is then
aborted leaves the cursor at rest: it wakes nothing.
Core checks no more than the tree's own rules (no hook edit limits).

### tool hook, ToolHook
`async def hook(node, call) -> str | None`; `tau.ToolHook` is its `Protocol` · new

Runs per call, in parallel. A string makes that call `Err(reason)` and halts the later
hooks of the point. Every `pre_tool` hook runs, even after one has set an outcome (the later
hook sees it and may change it). An exception, a non-`str` return, a `call.outcome` that is
not an `Outcome`, or a non-`str` `call.args_raw` makes the call `Err("Type: msg")`, blamed
on that extension, the traceback on stderr (never the log); the driver lives.

### cursor handle, node handle, tree handle
Python `Cursor`, `NodeHandle`, `TreeHandle` · retires `ExtensionAPI`

What a hook acts through, bound to its extension (which fills in `by`). A
node handle is a cursor handle without the cursor's own operations. Every
call goes into Rust at once. Both are a **tree handle**, which holds what they
share. Reads: `tree` (the `Tree` view: `node`, `walk`, `children`, `config_at`,
`lca`, `len`) and `cursor(c)` (a `CursorState`), plus `cursors` and `label`;
no flat copies of what those two already give. Writes that belong to no cursor: `append`
(`Machine::append`: a node under any parent, moving nothing), `open`,
`add_item` (an `ItemData.Extension` of this extension), `remove_item`
(`Machine::remove_item`: any item, wakes nothing), `set_label`,
`remove_label`, `closed()` (each closed cursor and its last node), `reopen(cursor)` (see
**reopen**), and `send(to, text)` (an *input* for cursor `to` authored by this extension,
`Extension(name)`, with `to`'s auto-step turned on, as `Client.send` does: the other cursor's
model is given the text as a `User` message; it needs no client). A cursor is oblivious to its
clients: no handle has a **client**. Each write tells the driver (`Changed`), which publishes the difference to its
clients. The tree handle also waits on and stops another cursor (D17 §3): `await rest(cursor)`
returns the *ending* of the cursor's last step once it is at rest with no effect out (after a
`send` to a cursor at rest, the step the send caused; to a cursor mid-step, the input waits and
`rest` may return that earlier step's ending with the input still pending, so `rest` before
`send` to be sure), or `None` if that is not known (it has not stepped since the session was loaded; endings are not
persisted), and `abort(cursor)` aborts its step as a client's `abort` does (cancels the sample
or calls, stops a command, declines open questions), safe from a sync tool's worker thread: it hops onto the loop and waits for it (for as long as the loop is
not run; `RuntimeError` if it is closed). `close_cursor(cursor)` (see
**close_cursor**) closes a cursor once it is aborted and at rest. `rest` and `close_cursor` are async only, so a sync
tool cannot call them. All three refuse (`ValueError`) the handle's own cursor (a cursor handle's `id`,
a node handle's `cursor_id`): waiting on or closing itself would never end, and aborting itself would cancel
the call; a tree handle with no cursor of its own refuses none. A cycle (A's tool waits on B
while B waits on A) is not detected: the user's abort is the way out. A node handle adds `ask` (to its batch's cursor) and `cursor_id`, the id of the cursor
whose batch it is (`id` is the batch's node; `TreeHandle.cursor(c)` already reads a cursor's
state, so the attribute is not `cursor`). A cursor handle adds `node`, `walk`,
`options()` (also on a node handle, at its node), `set_model(model)` (the model must be in the
catalog) and `set_options(**changes)` (the calling extension's own options, which folded with the
walk's must fit their type; see *config*; nothing is written otherwise),
`save_options(**changes)` (the same options written into the user's config file for every session
and read by the running driver at once, no node; also on a node handle, so a tool hook can save;
see *UserConfig*), `saved_options()` (the fields the user's config file itself sets of the
calling extension's options, as JSON values, and none of the project's, the overrides' or a node's;
also on a node handle; what an extension reads to add to a list option it saves, since `options()`
folds the layers and nodes in), `model()` (the qualified name of the model the cursor's branch is on, as its
next sample resolves it; `ConfigError` if no `Config` on the walk names one or one is
malformed), `models()` (the catalog's qualified names, in file order, a tuple: what `set_model`
accepts), `tools()` (the names of the driver's tools, the extensions' included, in the order a
sample is offered them, a tuple: what `Subject.tools` accepts), `show(text)` (see **show**), `move(to)`, `write(body, delivers)`,
`set_auto(on)`, `tell(text, *, to=None)` (a `Notice` to client `to`, by default to every
connected client; dropped if `to` is not connected), and `ask`; its own `at`, `auto`,
`items`, `labels`, and `moves` are `cursor(handle.id)`. A handle's writes that
belong to no cursor pass the hook's cursor to core, so they skip their sync
only while a cursor hook job is out for it. They do it through a
`HandleWrites`; the public `Session.open`, `add_item`, `remove_item`,
`append`, `append_root`, `set_label`, `remove_label` take no `hook`, so a
frontend cannot name a cursor and skip the sync.

### HandleWrites
`tau._core.HandleWrites` (`HandleWrites(session, hook)`) · new

The same seven extension writes as `Session` (`open`, `add_item`,
`remove_item`, `append`, `append_root`, `set_label`, `remove_label`), bound to
the cursor whose hook makes them (`None` for a tool hook). While that cursor's
hook job is out they ride the phase's sync instead of syncing. Only the
handles hold one; it is not exported from `tau`, and it is a type apart
so the public `Session` cannot be given a hook to skip a sync with. It stays constructible from
Python (a `#[new]` taking `(session, hook)`) because `Cursor` builds it with its hook id; the
only caller is `handles.py`, and nothing public takes a hook: `TreeHandle` (exported) has no
`hook` parameter and always binds `None`, which syncs, and `_core` is a private module (rule 4
keeps tests out of it). Not built: updating an item
in place, and a handle's `sample` of a
walk and running a tool outside any step (model.md §7).

### Poke, Now, Changed
Python aliases in `handles.py` · new

`Poke = Callable[[], None]`: the driver's poke, which tells the pump a handle
call (`open`, `add_item`) may have woken a cursor. `Now = Callable[[], int]`:
the driver's clock in milliseconds, so a handle fills in `time`. `Asker` is the
driver's question call for one cursor (see **question**): `(text, expect, to, new_cursor)`. `Changed = Callable[[], None]` is the
driver's publish for a handle: a handle write calls it, and `Hub.sync` sends whatever changed.

### question
`tau._questions.Questions`, `OpenQuestion` (driver memory; not core) · retires
`request_user_action`, `Driver.respond`, `Driver.questions`, `Question`

`await handle.ask(text, expect=str, *, to=None)` on a cursor handle or a node handle (a tool
hook asks on its batch's cursor) returns the answer checked against `expect` and converted,
or `None` if declined. `expect` is any hint of the type language (**values**). A question has
an **id** (an integer, unique in the driver's life, never reused), its cursor, `to` (`None`:
anyone, the **pool**; or a client id), its text and the JSON Schema of its answer; clients see it as an
`OpenQuestion` in the cursor's `CursorView` until it closes. `Client.answer(question, value)`
answers it: the driver checks the value (a wrong one is a `ValueError` naming the problem to
the answering client, and the question stays open), the first good answer wins (a later one,
or an unknown id, is a `LookupError`), and a question `to` another client is a
`PermissionError` (while that client is connected). `None` declines. A question belongs to its
cursor, not to a client: it never declines for want of an answerer, and with no client connected
it stays open and blocks until someone answers. `to` says whom it is first for, only while that
client is **connected**: a question to a client that is not connected goes into the **pool** at
once (`to` becomes `None`, anyone may answer), and so does one whose client disconnects while it
is open (the cursor's `CursorView` is sent again, so the other clients see they may answer); it
stays in the pool if the client returns. Abort and go-to decline the cursor's open questions, and
any it asks later in that step: an abort leaves nothing answerable behind (the cursor may ask
again once its next step begins). The hook awaiting a question keeps its job out, so the cursor is busy
and core's busy rules apply (model.md §7). A question lives only while its `ask` waits: a hook
that gives up (`asyncio.timeout`, model.md Q9) or is cancelled removes it.

### OpenQuestion
`tau.OpenQuestion(id, to, text, schema)` (`events.py`) · D16 · new

What a client sees of an open question (see **question**): `schema` is the JSON Schema of the
answer with the named types' `"format"`. A plain frozen value.


## Effects and edges

### provider, model
retires backend (config key), `ProviderSpec`, `Model` descriptor,
`model_spec`

The **provider** is the Python adapter for one API family; it converts walks
to messages (`model.md` §10). It is long-lived (D15): the program that builds it
closes it. Its contract (`tau.Provider`): `name` (what a model's `provider`
calls it); `check(model) -> list[str]` (problems with the model's settings as this
provider reads them, empty if fine; a model whose `provider` is not its own is
one); `sample(walk, model, tools)` (an async stream of live deltas and whole parts,
then one `Finish`; `model` is the `ModelConfig` the driver resolved for this
sample, and the provider reads no node data); `aclose()`. Before each sample the driver
calls `check` and fails the sample (the cursor survives) if it returns problems; it closes
the `sample` generator when it stops reading. A part yielded while deltas are open must be
the part they were building (same kind, same text; a call: same id, tool and arguments),
and `Finish` may not come with deltas open; a violation fails the sample, and the open
text is saved. A part is yielded before
the first delta of the next part. If a sample is cancelled or fails, the driver
saves the text or thinking whose deltas arrived without a part.
Any exception from `check` or `sample` that is not a `SampleError` fails that one sample
(parts kept, `meta.error.internal` true, traceback to stderr); `sample` assumes `check` passed.
The **model** is a name, as its API spells it.
API keys are read from the provider's environ. A key holds only visible ASCII (`0x21` to `0x7E`);
the error names the variable or the literal, never the key.

**One-off call (a recipe, no API).** To ask a model one thing with no driver and no saved
session: `Session.create(config, system, now)` with no directory (in memory); append a `User`
node under the root with `session.append`; run `provider.check(model)` as the driver does, then
`async for item in provider.sample(session.tree.walk(node), model, []): ...` (`[]` offers no
tools). `model` is `catalog.resolve(name)` and `provider` is `builtin_providers(environ)[model.provider]`.
A runnable version is `examples/one_off.py`. The providers' request builders are private; this
is the supported path.

**Retries.** Neither the driver nor any provider retries (a cursor never spins). A wrapper
provider that does is `examples/retry.py` (`RetryProvider`); it reads `SampleError.status`,
`retry_after` and the `httpx` error in `__cause__`.

### builtin_providers
`tau.builtin_providers(environ)` (module `tau.providers`) · D15 · new

The built-in providers by name, `{"openai": OpenAIProvider, "anthropic": AnthropicProvider,
"gemini": GeminiProvider, "mock": MockProvider}`: what the CLI and the examples give `Driver` and
`check_config`. `environ` is where the hosted providers look up keys. The caller closes them.

### OpenAIProvider
`tau.providers.openai.OpenAIProvider` · new

The provider for OpenAI-compatible `/chat/completions` (llama.cpp, vLLM, OpenAI).
Is handed the resolved `ModelConfig` as `sample(walk, model, tools)`; it parses no node data. The request carries `model`,
`messages`, `stream: true`, and `tools` when the driver offers any (no
`tool_choice`, no `parallel_tool_calls`); then `max_tokens` and the options below.
`tools` is `[{"type": "function", "function": {name, description, parameters}}]`,
built from each `Tool` for that sample only and recorded nowhere. Conversion: `System`
and `User` map to their roles (a `System` after the first non-`System` turn is a `user`
message `[system note: ...]`); an `Assistant` to `assistant` with its text and
`tool_calls` (`args_raw` sent as the `arguments` string); a `Results` that
follows its batch to one `tool` message per outcome (`Err(e)` is `Error: e`,
`Interrupted` is `Interrupted`); a `Results` that answers no batch to one
`user` message of `[tool result <id>: <outcome>]` lines. `Thinking` parts are
not sent back (this API takes none). Calls that are not runnable, or a batch
with no `Results` after it, are `[tool call <id>: <tool> <args_raw>]` lines of
text (the `partial_calls: text` markup), followed by `[reply stopped: <why>]`
for `Length`, `Interrupted`, and `Error`. A `Results` whose ids differ from
its batch's is a conversion error (Q11). The response is read as SSE;
`reasoning_content` or `reasoning` deltas (vLLM sends both, equal; two different texts is an error)
become `ThinkingDelta`s and then a `Thinking`, `content` `TextDelta`s and a `Text`,
`tool_calls` a `CallNamed` (once the id and name have arrived; arguments that came
first are held until then; before that a repeated id or name replaces the earlier one,
after it a repeat of the same is ignored and a different one is an error, which is what a
server that omits `index` or splits a name causes), `CallArgs`, and a `Call`. A choice with
`index` other than 0 is an error, and a chunk with more than one choice is refused: one choice is
read. A `delta.refusal` is text. Each delta is yielded as it
arrives; each part is yielded before the first delta of the next kind of part, or
when the stream finishes. `finish_reason` maps `stop` to `End`, `tool_calls` to `Tools`,
`length` to `Length`, `content_filter` to `End`; any other is an error. The
raw string is kept as `finish_reason` (via `Finish`). The request sets
`stream_options.include_usage` unless the option says not to, and the stream is read to
its end (usage comes after the finish) to fill `meta` with `model`, `id`,
`system_fingerprint`, `usage_raw` (the server's `usage` object as sent) and the
normalized `usage` `{"input", "output", "cached"}` from `prompt_tokens`,
`completion_tokens` and `prompt_tokens_details.cached_tokens` (`cached` is 0 for a server
that reports none; no `usage` at all if the server gave no prompt and completion counts,
or a cached count that is present but not an integer). Once `finish_reason` arrives the reply is done: later content deltas are ignored and
any later failure (reset, error chunk, malformed SSE, no `[DONE]`) costs only the
meta not yet received. A `SampleError` carries the `meta` that had arrived. Fails with `SampleError`: a
non-200 status (the server's `error.message`), a transport error, malformed
SSE, an error object in the stream, a stream ending without `finish_reason`.
Parts begun before a failure are yielded first; a call cut off (no id or name yet, or arguments
unfinished) is not a part and is dropped, so its raw argument text is appended to the `SampleError`
message (D1). A non-200 response fails with `SampleError`
(`status` and `retry_after` are taken before the body is read, so a body that fails to
read keeps them) with `status` set and `retry_after` from the `Retry-After` header (whole seconds, or an
HTTP-date as seconds from now, 0 if past; anything else `None`); a connection error has
`status` `None`. No retry happens here. `name` is `"openai"`; its `check` requires a
`base_url` and reads exactly these options, refusing any other: `temperature`, `top_p`
(numbers), `reasoning_effort` (non-empty string), `include_usage` (boolean, default true;
some servers reject `stream_options`), `extra_body` (an object merged into the request body
last, for server-specific parameters such as llama.cpp's `cache_prompt`; `check` refuses
`model`, `messages`, `stream`, `tools`, `n`, any key the model's own settings already set
(`max_tokens`, `temperature`, `top_p`, `reasoning_effort`, and `stream_options` while
`include_usage` is true), and NaN or infinity anywhere inside it). `base_url` is the API root (such as
`http://host:8100/v1`): one ending in `/chat/completions` is a `check` problem. For llama.cpp,
`extra_body: {"return_progress": true}` makes the server report progress during a long prompt,
which keeps the read alive past the 300 s between-bytes timeout. For OpenAI proper, whose newer
models refuse `max_tokens`, leave `max_tokens` unset and set `extra_body: {"max_completion_tokens": N}`.
The `[system note: ...]` form is used for every `System` node after the first non-`System` turn. One `httpx.AsyncClient` serves every sample,
made on the first and closed by `aclose()`; a sample after `aclose()`, or one it cuts off,
fails with `ProviderClosed`. A client belongs to the event loop that made it: a sample
on another loop fails with a `SampleError`: build one provider per event loop (D15 D10). The response body is read to its
end, so the connection returns to the pool. Constructor: `transport`
(a test's `httpx.MockTransport`), `environ` (where keys are looked up),
`timeout`. The request is built inside `sample` and is not public.

### AnthropicProvider
`tau.providers.anthropic.AnthropicProvider`, name `"anthropic"` · D15 · new

The provider for the Anthropic Messages API (`POST {base_url}/v1/messages`, SSE; `base_url`
is the API root, `https://api.anthropic.com` when unset; one ending in `/v1` is a `check`
problem). Headers `x-api-key` and `anthropic-version: 2023-06-01`. `check` requires
`max_tokens` (the API requires it; there is no default) and a key (`api_key_env`), and
reads exactly these options: `thinking` (an object as the API takes it: `type` a string,
`budget_tokens` a count, `display` a string; `check` checks types and judges no values, which
are the API's, but `type` `enabled` needs `budget_tokens` of at least 1024 and below
`max_tokens`, and `display` `updates` and `block_binding` are refused: they need a beta header
this provider cannot send), `effort` (a non-empty string, sent as `output_config.effort`), `temperature`, `top_p`, `top_k` (newer models refuse these), `prompt_caching`
(boolean, default true: `cache_control: ephemeral` on the system text and on the last block of
the last message), and `extra_body` (merged last; may not set `model`, `messages`, `system`,
`stream`, `tools`, `max_tokens`, `thinking`, or a key the model's settings set; `context_management`
is refused too: it needs a beta header). Request: the first
`System` nodes are top-level `system`, one later is user text `[system note: ...]`; turns of
one role merge; `Thinking` parts are sent only with Anthropic replay data (`replay_for`), so
unsigned, cut-off and foreign thinking never go; a reply with nothing left to send is dropped;
a runnable batch is `tool_use` blocks (`input` the parsed `args_raw`, `{}` for none) and
`tool_result` blocks with `is_error`, but goes as text, with its results as `[tool result
<id>: <text>]` lines, when a call's `args_raw` is not a JSON object or, under `thinking.type`
`enabled`, the last assistant turn has no thinking to open with. A call id the API would refuse
is sent with bad characters replaced by `_` and a hash of the original added, alike in the
call and its result (ids are never shortened; whether the API limits their length is unverified).
A `User` node right after a `Results` merges into the same user message; the docs say that can
give an empty `end_turn`, which the provider drops, and the loop can repeat (D15 Open). Replay data: `{"type": "thinking", "signature": s}` on a `Thinking`
part (text is the block's `thinking`), `{"type": "redacted_thinking", "data": d}` on a
`Thinking("")`. Response: `thinking_delta`, `text_delta` as deltas; `tool_use` as `CallNamed`
then `CallArgs` (no arguments is `{}`); `stop_reason` maps `end_turn` and `stop_sequence` to
`End`, `tool_use` to `Tools`, `max_tokens`, `model_context_window_exceeded` and `pause_turn` to
`Length`, `refusal` to `End`; any other fails the sample. `meta`: `id`, `model`, `usage`
`{"input", "output", "cached"}` (`input` is `input_tokens` plus cache reads plus
`cache_creation_input_tokens`, `cached` the reads alone), `usage_raw` (the cumulative usage
as sent), `stop_details` when sent, and the `request-id` response header as `request_id` (on
failures too). The provider sends no `anthropic-beta` header, so `check` refuses what needs
one: `thinking.display` `updates`, `thinking.block_binding`, and `extra_body.context_management`. Errors: an HTTP error or `error` event is a `SampleError`
whose `status` is the response's, or for an event the status its `type` stands for (529 for
`overloaded_error`), with `retry_after` from the header. Client handling is `Connection`'s.

### _anthropic_request (the Anthropic request module)
`tau.providers._anthropic_request` · D15 · new

The walk as an Anthropic request, split from `providers/anthropic.py` (which reads the
response) to stay under the file-size ratchet: `conversation(walk, manual_thinking, has_tools)`
gives `system` and `messages`, `tools_array`, and `breakpoints` the cache marks. It
applies `turns` and holds every rule above that turns a walk into text instead of failing it.
Private: the shapes may change with the next provider.

### _wire (the provider helpers module)
`tau.providers._wire` · D15 · new

What every hosted provider shares over HTTP: `retry_after` (a `Retry-After` header as
seconds, `None` when not finite), `sse_data` and `events` (the SSE events as JSON objects, reading the body to its
end), `error_text` (`HTTP <status>: <message>` from an `error` object, `typed` for
`<type>: <message>`), and the JSON and number checks `plain`, `non_finite`, `is_number`,
`is_count`, `count`. `Connection` holds the one `httpx.AsyncClient` a provider keeps (made on the
first sample, bound to the event loop that made it (a sample on another loop is a `SampleError`), closed by `aclose()`, after
which a sample raises `ProviderClosed`) and runs the sample loop: it POSTs, reads each event
into a `ReplyReader`, maps an `httpx` error or a bad status to a `SampleError` with `status` and
`retry_after`, and yields the parts left at a failure. A provider gives it only how its error
bodies read (`typed_errors`, `body_wait` for a wait inside the body, `meta_headers`) and a
`ReplyReader`: the one-response reader (`feed`, `done`, `stop`, `close`, `meta`) that each provider's
`_Stream` is. `close` never raises and never returns a partial call: a call cut off is dropped and its
raw text goes in `meta["cut_off_call"]`, which `Connection` moves into the error message. Once a reply
is done, `Connection` ignores what `feed` returns and reads only `meta`. Events are strict JSON (NaN
and infinity are invalid; a lone surrogate is a `SampleError`); `SampleError`s from a header or body
never quote the header; the `httpx` error is the `__cause__`. Private to the package.

### GeminiProvider
`tau.providers.gemini.GeminiProvider`, name `"gemini"` · D15 · new

The provider for Google's `streamGenerateContent?alt=sse`. Built to the same contract as
`OpenAIProvider` (`Connection`: one client per loop, `ProviderClosed`, `status` and `retry_after`, the
ordering rule, `usage_raw`). `base_url` defaults to `https://generativelanguage.googleapis.com/v1beta`
(a proxy sets it); a key must be named (`api_key_env` or `api_key`; `check` says so; for an unset variable `tau config check` warns and sampling fails) and travels in the `x-goog-api-key`
header, never the URL. The URL is `<base>/models/<model>:streamGenerateContent?alt=sse`, the model
quoted for a URL; a model name that starts with `models/` is a `check` problem (the provider adds it).
Request: `systemInstruction` (the `System` nodes before any other node), `contents`
(`user` and `model`), `tools` (one `functionDeclarations` list, each tool's schema as
`parametersJsonSchema`), `generationConfig` (`maxOutputTokens` from `max_tokens`, then the
options), `safetySettings`, then `extra_body`. A `System` node later in the walk is a user
turn `[system note: <text>]` (`system_note`); a user or system turn with no text is dropped. A `Results` that follows its batch is one `user` content of
`functionResponse` parts, in call order, `{"result": text}` or `{"error": text}`, with the call's
`id` where the API gave one. A batch goes as text (the call markup, and its results as
`[tool result <id>: <text>]` user text) when a call's `args_raw` is not a JSON object, so a
cut-off call from another provider never fails the branch. A reply with nothing to send is
dropped and neighbouring same-role contents merge; text parts are never merged with each
other. `Thinking` is sent back only when Gemini signed it. A response `parts` that is not a list, a
part with more than one data key, or a `thought` that is not a bool fails the sample; metadata part
keys are ignored, and so are candidates after a `blockReason`. Switching to a Gemini 3 model in the
middle of a tool loop fails with a 400 (a call without a signature) until the next user text starts a
new loop; there is no fallback signature.

Response: a part with `thought: true` is `ThinkingDelta`s and one `Thinking`; other text is
`TextDelta`s and one `Text`; a `functionCall` arrives whole (`CallNamed`, one `CallArgs`, the
part). A `thoughtSignature` is the part's replay data `{"thoughtSignature": ...}`; it rides the
last chunk of the text it signs, often one with no text, and ends the part, so text after it
is a new part. On a call it sits on the `functionCall` part and the replay also holds the
`id` the API gave (`{"id": ...}`). A call with no id gets `gemini-` and a random UUID, and its
replay is `{"synthesized_id": true}`: a made-up id is never sent. A signature is sent back
only on the part it came from and only when `replay_for(part, "gemini")` returns data.
Replay that core cannot hold fails the sample. `finishReason`: `STOP` is `End` (`Tools` when
calls were made), `MAX_TOKENS` and `CONTINUATION` `Length`; `SAFETY`, `RECITATION`, `LANGUAGE`, `BLOCKLIST`,
`PROHIBITED_CONTENT`, `SPII`, `ESCALATION` and the image refusals `End`; any other fails the sample. A
`promptFeedback.blockReason` ends as `End` with the reason as `finish_reason` and the
block in `meta.promptFeedback`. `meta` holds `model` (`modelVersion`), `id` (`responseId`),
`finishMessage`, `usage_raw` and the normalized `usage`: `input` is `promptTokenCount`,
`output` is `candidatesTokenCount` plus `thoughtsTokenCount` (thinking is billed as output),
`cached` is `cachedContentTokenCount`; an absent count is 0. An error event in the stream
(`{"error": {...}}`) fails the sample with its `code` as `status`. A non-200 response is a
`SampleError` with `status` and `retry_after` (the `Retry-After` header, else the body's
`RetryInfo.retryDelay`). Options (`check` refuses any other): `temperature`, `top_p`, `top_k`
(`top_k` an integer); `thinking`, an object of `include_thoughts` (bool; thought summaries are requested
only when true), `budget` (integer >= -1; Gemini 2.5) or `level` (a string; Gemini 3), not
both; `safety_settings` (a list of `{category, threshold}`, one per category); `extra_body`
(merged last; `contents`, `systemInstruction`, `tools`, and any key the model's settings set
are refused; its `generationConfig` merges into ours, and may not set `candidateCount`).
A walk ending in a `model` turn fails the sample before any request.

### MockProvider
`tau.providers.mock.MockProvider`, name `"mock"` · D15 · new

Hits no network and needs no key. It streams a reply as `TextDelta`s, a word and the
space after it each, with a delay before each; then yields the whole `Text` and a
`Finish` (`End`, finish reason `"stop"`) with normalized `usage` (word counts) in
`meta`. `check` refuses `base_url` and any option but `reply` (a string: the text to
stream, non-empty; default `mock: ` and the walk's last user text) and `delay_ms` (a
non-negative integer, default 30). `max_tokens` caps the chunks streamed and ends the
reply with the `Length` stop. Cancelling it mid-stream leaves an `Interrupted` reply
holding exactly the text streamed. It fails the sample if the walk has no user text
and there is no `reply`.

### tool, Tool
`tau.Tool` (protocol, `protocols.py`) · retires `AgentTool`, `ToolDefinition`, `ToolInfo`

The contract the driver and providers depend on: `name`, `description`,
`parameters` (a JSON Schema object for the arguments), and
`async call(args_raw: str) -> Outcome`. The tool gets the call's raw JSON text
and validates it itself, answering `Ok(text)` or `Err(message)`; an exception it
raises is `Err(str(e))` for that call. Breaking the return contract (answering
`Interrupted`, a `str`, `None`) is a programmer error, not a call's failure, but a bounded one: the
driver turns it into that call's `Err("TypeError: tool 'x' answered ..., not an Ok or Err")`
(blamed on the tools, explanation on stderr); it costs one call, never the driver. A malformed
`args_raw` is the tool's to answer (or a `pre_tool` hook's, which may rewrite
`call.args_raw` before the tool runs; D7); neither the driver nor core parses it.
The driver checks each tool when it is built (`DefinitionError`).
Any object with these attributes is a tool, including one written in another
language (a PyO3 class); a plain function the driver describes is the other way
(see *function tool*), and a test drives a hand-written class through the `Harness`. Tools are the driver's, not the
history's: each sample is handed the driver's tools, and no `Config` or node
records them.

### function tool, ToolError, DefinitionError
`tau.ToolError`, `tau.DefinitionError` (a `TypeError`); `FunctionTool`,
`assemble_tools` (`_tools.py`, private) · D16 · new

The driver's `tools=` and an extension's `tools` attribute list plain sync or async functions as
well as `Tool` objects (an item with a `call` is a `Tool`; a function is described); the driver
makes a private `FunctionTool` of each when it is built (`assemble_tools`; D16 §2), so there is
no decorator and nothing to export. Name: the function's `__name__`. Description: its
docstring, `inspect.cleandoc`'d. `parameters`: an object schema from the type hints (the type
language, **values**), as a model is shown it (no `"format"`, no `"default"`); a parameter with a
default is not `required`; `additionalProperties` is false. A parameter is described with
`Annotated[T, "text"]`. `call` parses `args_raw`, checks the top-level keys and each value
against the annotation, then converts them all (so a `CursorId` of `"new"` opens its cursor only
for a call whose every argument passed), binds by keyword with the **injected parameters**, runs
the function, and requires a `str` back. The driver is the only caller. A sync function runs in a
worker thread, so it never blocks the loop; aborting the call stops waiting for it, not the
thread, which finishes and is discarded. A function that `functools.wraps` an async one counts as
async (`inspect.unwrap`).

`ToolError` is how a call fails (malformed JSON, missing, extra, or wrongly typed arguments, an
exception from the function, a non-`str` return); the call answers it as `Err(message)`, so the
model reads it. A tool function may raise `ToolError("msg")` itself: the call answers
`Err("msg")` exactly, with no type prefix (any other exception is `Err("Type: msg")`).

`DefinitionError` is the one error of building: a tool, command, extension, options type or
question that cannot be built. Its message names the extension, function and parameter: no
docstring, a missing or unsupported annotation, `*args`/`**kwargs`, positional-only parameters, an
`Annotated` with other than one `str`, an annotation that cannot be resolved, a default that does
not fit its annotation (`s: str = None` says to annotate `str | None`), an injected parameter the
function cannot have (a `Cursor` in a tool, a `NodeHandle` in a command), an object that is not a
`Tool` or an `Extension` (a missing or non-`str` `name`, `description` or `parameters`, a `call`
that is not callable, a misspelt `pre_*`/`post_*` attribute), two tools, commands or extensions
with one name, a function that takes a handle given as a driver tool, a `tools` or `commands`
that is not a sequence, an options type that is not a frozen dataclass of the language, and
`ask(expect=...)` of a type outside the language. `check_tools` and `check_extensions`
(`protocols.py`) make the contract checks and return the tools by name (with the names of those
that take progress) and the extensions.

### values, Kind, Context, Field, Form
`tau._values` (private), `Kind`, `Context`, `Field`, `kind_of`, `field_of`; `tau.Form` · D16 · new

The one type language: a Python type hint becomes a `Kind` holding its `schema` (for a
client, named types with `"format"`), its `model_schema` (without the format, which no
provider API accepts), `check(value, context)` (the problem as words, or `None`) and
`convert(value, context)` (what the function receives). A `Context` is what the named types
are checked against: the model catalog, and for the types looked up in the session, the
session and `new_cursor`, which opens the cursor a `CursorId` of `"new"` means (options are
checked with the catalog alone). A `Kind` is `session_only` if its values are looked up in the
session; `check_default` checks a Python default by shape only. A `Field` is a name, a `Kind`, its
default (if it `has_default`), and `nullable` (a `T | None` with no default: not required, `None`
when left out); `field_of(name, hint, default, has_default=)` makes one, and
`object_schema` / `check_object` / `convert_object` treat a tuple of them as an object: a
function's parameters, or an options dataclass's fields. `object_schema` carries each default as
`"default"` (a `Path` as its string) in the client schema, or, for an options form, the value now;
the model's schema has none. A `Form(name, description, schema, extension=None)` is what a client knows of
something it fills in: a command (`extension` is its owner) or an extension's options (the `name` is the
extension, `extension` `None`, as for `/config`; the
defaults are the values now); frozen, and not hashable, the schema being a dict. Its `qualified`
is `extension:name` for a command, the **qualified command name** nothing can take (D19 §7). A hint outside the
language, a default that does not fit it, or an `Annotated` carrying other than one `str` is a
`DefinitionError`. Supported: `str`, `int`, `float`, `bool`, `Literal` of `str` or `int`, `list[T]`,
`T | None`, `Annotated[T, "text"]` (the text is the schema's `"description"`),
`pathlib.Path` (a leading `~` is the home directory), and the named types. A `list[T]` default
may be a tuple, which a dataclass needs (it refuses a list); an options instance holds a list
either way, set or not. A JSON integer
given to a `float` is converted to one. A `CursorId` default of `"new"` is a `DefinitionError`: only a
caller may ask for a new cursor.

### named type, Model, CursorId, LabelName, NodeId
`tau.Model` (`str`), `CursorId` (`int`), `LabelName` (`str`), `NodeId` (`int`); `NewType`s · D16 · new

A closed set of hints checked in a context: a `Model` is in the catalog (by qualified
name; the only one an option may be, since it needs the catalog alone), a `CursorId` is an open cursor or `"new"`, a `LabelName` is an existing label, a
`NodeId` is an existing node. Each carries `"format"` `"model"`, `"cursor"`, `"label"`,
`"node"` in the client schema; `pathlib.Path` carries `"path"` (a string, converted to a `Path`,
not checked against the session). `"new"` is the only value with an effect: converting it opens a
cursor at the asker's node (the batch's node for a tool, the cursor's `at` for a question).

### injected parameter, Described, describe
`describe(function, what=, extension=, refused=)`, `Described` (`_functions.py`, private) · D16 · new

A parameter of a function that the driver fills and the model is never shown, found by
annotation alone (a parameter's name says nothing): `Progress`, `TreeHandle` or `NodeHandle` (the
batch's `NodeHandle`, bound to the extension that lists the function; only for tools listed on an
extension), and `Cursor` (refused for a tool; a command's). `describe` reads a function once into
a `Described`: name, docstring, the `Field`s of its arguments, the injected parameters by name and
kind, and whether it is async; commands use it with their own `refused` set (`NodeHandle`).
`invoke(function, described, arguments, progress=, handle=)` is the one place a described
function is called, for a tool and for a command: it fills the injected parameters and awaits an
async function or runs a sync one on a worker thread. What a handle tells the driver (`poke`,
`changed`) is routed back to the loop's thread by the driver, so a sync function may write through
its handle.

### command, CommandFailed
`tau.CommandFailed` (a `RuntimeError`), `Client.commands()` (a tuple of `Form`),
`Client.run(cursor, command, args)` · D16 · new

A plain function an extension lists in its `commands` attribute, offered to a person as `/name`;
the same function may be in `tools`. Described when the driver is built by
`describe(fn, what="command")`: injected `Progress` (each call is a `Notice`), `TreeHandle`, and
`Cursor`; `NodeHandle` is refused (`DefinitionError`). Its handles have no client (a command that takes both a `TreeHandle` and a `Cursor` is given the
one `Cursor` for both). Names are unique within an extension (`DefinitionError`) and `config` is taken; two extensions
may share one (D19 §7): every command has a **qualified name** `/<extension>:<command>` that nothing
can take, and the bare `/name` works while exactly one extension has it. `Client.run` takes either;
a bare name two extensions share raises `LookupError` (`/note is two commands: /a:note, /b:note`),
as does `commandline.parse` before any round trip (an unknown `/x:y` is `UnknownCommand`).
Load order never decides what `/note` does, and `tau` is not special: an extension adding `/model`
makes the bare `/model` ambiguous and `/tau:model` still works. Completion after `/` offers the bare
name of each unique command and the qualified names of shared ones, and after `/<extension>:` that
extension's commands. `Client.run` returns the command's result (`str | None`) when the command
has finished, and raises `CommandFailed`, whose message is the failure as `"Type: msg"`: as soon as
the command raises (see *Cursor command*), and `"Aborted: ..."` for one the cursor's abort or go-to cancelled. A sync function runs on a
worker thread, as a tool does. A `Form` (**values**) is what a client knows of one: name,
docstring, and the JSON Schema of its arguments with the named types' `"format"` and each
default; fixed until the driver reloads. `run` checks `args` as values of the language (with the
session; `"new"` after the rest) and raises `LookupError` (unknown command), `ValueError`
(arguments), or `Busy`; nothing is opened or started for a refused call. Not the same as the
`pre_input` refusal of a `/…` input: a client that does not know a command sends the line as text.

### Cursor command
`Commands.before` (`_commands.py`) · D16 · new

A command that takes a `Cursor`. It runs as a step of that cursor: `run` keeps it in memory and
calls `Session.step`, so it is `Busy` unless the cursor is at rest (commands do not queue); the
driver gives it to the step's `pre_step` job, where it runs with the cursor's handle (also its
`TreeHandle`) before the extensions' `pre_step` hooks. The rest of the step follows the usual
rules: a command that leaves the cursor on a `User` node gets a sample, one that leaves it on a
reply gets none. `abort` and `go_to` cancel a running command (and drop one not yet started),
whose job then answers without running the hooks (their `run` raises `CommandFailed`); a crash
loses a command that has not finished, and it is never re-run. A command that raises ends the step
`Failed` exactly as a failing hook does: the cursor's `hook_error` names `pre_step` and the
extension that lists the command, and no hook runs after it; its `run` raises `CommandFailed` at once, before the step has ended. A command with no `Cursor` runs at once as a task on the
loop, whatever the cursor is doing; with a `TreeHandle` bound to its extension.

### tau (extension), /model, /config
`tau._builtin.Tau` · D16 · new

The built-in extension, always first in the driver's list (its name is taken: a user extension
named `tau` is a `DefinitionError`; extension names are letters, digits, `_` and `-`, so `:`, `.`
and spaces cannot break `/ext:cmd`, `tau config set EXT.FIELD=V` or a command line). `/model <Model>` is a `Cursor` command that switches the
branch to a configured model (`Cursor.set_model`). `/config <extension> name=value ...` changes
an extension's options on the branch: its values are a mapping, which the type language has no
hint for, so it is not a function. `Client.commands()` lists a `config` `Form` (its one parameter
the extension, a `Literal` of those that have options; absent if none has any) so a frontend
learns of it, and `Client.configure(cursor, extension, values)` is the call: it checks the values
against the extension's options folded at the cursor's `at` (`ConfigError`, nothing written), then
writes a `Config` node as a step of the cursor, like a `Cursor` command (`Busy` unless at rest).
With `save=True` it first writes the values into the user's file too (`ExtensionOptions.save`;
see *UserConfig*), after the same checks, so a refusal writes neither the node nor the file.
`Client.run(cursor, "config", ...)` is a `ValueError`. A frontend reads the options of an extension
with `Client.options(cursor, extension)`: the `Form` whose defaults are the values now; a bare
`/config <extension>` shows it, and `commandline.parse` reads `/config <extension> limit=10`
by it (see *commandline*). The user's file is written by `save` (see *UserConfig*), never by a
`Config` node.

### options (extension)
`tau._options` (`ExtensionOptions`); `ConfigError` · D16 · new

An extension's `options` attribute names a **frozen dataclass**; each field is a hint of the
values language with a plain default (no `default_factory`; a `Model` is allowed and is checked
against the catalog, but not a `CursorId`, `LabelName` or `NodeId`, which are looked up in a
session that stored options do not have: use `int` or `str`). A field with no default must be
configured (a missing one is a `ConfigError`, and `check_config` reports it). Anything else is a
`DefinitionError` when the driver is built. A field's value at a node is the last one set: the
layers' `extensions.<name>` (`ModelCatalog.extensions`), then the `Config` nodes on the node's
walk, root to node, each setting only the fields it names (so a node can change `size` and leave
`mode`); there is no way to unset a field back to its default. `handle.options()` returns the
dataclass instance at the handle's node (`ConfigError` if it does not fit; the type's own
`__post_init__` may refuse). `ExtensionOptions.save(extension, changes)` writes `changes` into
the user's file (`save_options`, which builds every extension's options from the layers with the
change) and gives the driver the catalog read again (`use_catalog`); `ConfigError` if the
extension has no options or the driver was given no `UserConfig`. `set_model`, `set_options`, and `configure` check the same before
writing: the options folded at the cursor's `at` with the new values must build. The fold of a
node (its `Config` nodes' last model and options) is computed once and shared by its descendants
that add no `Config`; `ExtensionOptions.model(node)` reads the model from it, for `Cursor.model()`
and `CursorView.model`. `ExtensionOptions.reload(extensions)` reads other extensions' options
types and forgets every fold (see **driver**). `Client.options(cursor,
extension)` returns a `Form(extension, description, schema)`: the schema of the options object,
each default the value now (a required option nobody set has none). `check_config(...,
extensions=)` is the check `tau config check` makes for the extensions it knows, and a program
that loads others can pass them.

### Handles
`tau.handles.Handles` · D16 · new

What the driver wires into every handle it makes (session, question call, clock, `poke`,
`changed`, options), with `cursor(id, extension, runner=None)` (`runner`: the client of the
command it runs), `node(cursor, id, extension)` and `tree(extension, client)`: one place that
builds them, for hooks, tools and commands alike.

### complete, named type completion
`Client.complete(cursor, named, prefix)` · D16 · new

The values of a named type that start with `prefix`, as strings: `"path"` (the driver's
machine, relative to its working directory, a leading `~` the home directory as typed; a directory ends with `/`; a hidden name only if
the prefix starts with `.`; a missing or unreadable directory has none), `"model"` (qualified
names), `"cursor"` (open ids, then `"new"`), `"label"`, `"node"` (the cursor's walk, newest
first). Any other name is a `ValueError`. No extension code runs, so completion cannot hang or
raise on an extension's account.

### commandline
`tau.commandline`: `parse`, `completions`, `value`, `values`, `Parsed`, `UnknownCommand` · D16 · new

The pure parts every line-based frontend shares. `parse(line, commands, options)` reads a line
that starts with `/` as shell words: `Parsed(command, args, missing, extension, save)`. A `name=value`
word sets the parameter of that name; the other words fill the remaining parameters in order,
except that the last parameter, when it is a plain `str` or a `str | None` (not a `Literal` or a
named type), takes the rest of the line from the word that would fill it, as typed, quotes
included; a list takes comma-separated items, or a JSON array when the text starts with `[`; each word is converted by the parameter's schema (`int`,
`float`, `true`/`false`, a `Literal`'s values; a named type is a string or integer). `missing`
lists the required parameters not given, for the frontend to ask. `UnknownCommand` (a
`LookupError`) names a command there is not, so a frontend can warn and send the line as text;
a bare name that two extensions' commands share is a plain `LookupError` ("/note is two
commands: /a:note, /b:note"), which a frontend shows, not sends; `/ext:name` names one exactly;
`ValueError` for anything unreadable. `/config <extension> name=value ...` is the one line that is
not a command's: the words after the extension are read by that extension's options `Form`, which
the frontend passes as `options` (by extension name; the `config` form lists the names, and
`Client.options` gives each). Then `command` is the options form, `args` the options to change,
and `extension` its name; with no `args` the line asks for the form. The flag `--save`, anywhere
after the extension, sets `save`: the call is `Client.configure(..., save=True)`, which writes the
values into the user's config file for every session as well as the branch (`--save` with nothing to
save is a `ValueError`; it is typed, not completed). `Parsed.run(client, cursor)`
makes the call, `Client.run` or `Client.configure`, and returns the result.
`value(text, schema)` reads one value of any schema, and `async values(schema, prefix, client,
cursor)` says what could finish it; both serve a command's argument and a question's answer
typed as text alike (`new` for a `CursorId` question). `async completions(line, commands, client,
cursor, options)` says what could finish the word being typed, each a whole word (quoted if
needed): command names after `/` (bare for a command only one extension has, qualified for a
shared one; after `/ext:`, that extension's), then `values` of the parameter (a `Literal`'s values,
`client.complete` for a named type), the extensions after `/config `, and their options. The
driver's check stays the authority; this only spares a round trip.

### assemble_tools
`tau._tools.assemble_tools(tools, extensions)` · D16 · new

The driver's `tools=` followed by each extension's `tools` attribute (a sequence of plain
functions, made `FunctionTool`s, and `Tool` objects), with the extension that lists each; the
driver then runs `check_tools` over the whole.

### check_extensions
`check_extensions` (`protocols.py`) · new

Raised as a `DefinitionError` by `Driver.__init__` for an object that is not an `Extension`,
naming it: a missing, non-`str` or empty `name`, a `name` that is not letters, digits, `_` and `-`
(`protocols.EXTENSION_NAME`; D19 §1), two extensions with one name, or any attribute
(method, field, class attribute) named `pre_*` or `post_*` that is not a hook point or is not
callable. A module with a `name` is an extension (D16 §6).

### ProgressTool
`protocols.ProgressTool` · D15 · new

The `Protocol` of the `call` of a `Tool` that opted in to progress (see **progress
callback**): `async call(args_raw, *, progress) -> Outcome`. The driver casts to it for the
tools `check_tools` found taking progress.

### check_meta
`protocols.check_meta(meta)` · new

What `Finish` and `SampleError` check `meta` with: `None`, or the text of a strict JSON
object. `TypeError` for a non-`str`, `ValueError` for text that is not a JSON object
(`NaN` and `Infinity` are not JSON).

### base tools: ReadTool, WriteTool, GrepTool, BashTool, base_tools
`tau.ReadTool` and the others, `tau.base_tools(cwd, ...)` (`base_tools.py`) over `crates/tau-tools`, bound by `tau-py/src/tools.rs` · new

Four `Tool`s whose logic is Rust (`tau-tools`: filesystem and process IO, no
PyO3) and whose `call` runs it on a worker thread with the GIL released, so
calls run in parallel (also on a free-threaded build) and never block the
loop. `base_tools(cwd)` returns all four. `cwd` (default: the current
directory, which must exist: `NotADirectoryError`) is what relative paths and
the shell's working directory are relative to. An expected failure (bad
arguments, a missing file, a nonzero exit) is `Err(message)`; nothing is raised out of `call`. The arguments
are strict: an unknown or missing field is an `Err`.

| tool | parameters | does |
|---|---|---|
| `read` | `path`, `offset` (1-based line, default 1), `limit` (lines, default: as many as fit) | A text file, verbatim, scanned in buffer-sized pieces (a file of one huge line costs the output window, not the file). `Err` for a missing file, a directory, a pipe, device or socket, an offset past the end, a binary file (NUL bytes; images are not supported). When `limit` or the byte limit stops it short of the end, a last line `[showing lines A-B; offset=N continues]` is appended; after a byte cut, `B` is the last kept line. It stops reading as soon as the limit is passed and writes no overflow file: the file it reads is already on disk. If not even the first line fits, its start is shown and the last line says `[line N is cut at byte B; read cannot show the rest (use bash: cut -c or tail -c); offset=N+1 continues after it]`: `read` has no way to reach past the first `B` bytes of one line. Opened with `O_NONBLOCK` and judged by `fstat` of the open file. Bytes that are not UTF-8 become U+FFFD and a line says so. |
| `write` | `path`, `content` | The whole file, UTF-8, atomically (a temp file beside it, then a rename), creating parent directories, following a symlink (dangling or not). Answers `Wrote N lines (B bytes) to <path>` (`1 line` in the singular). The new file gets the mode of the one it replaces before any content is written; the file and then the directory are synced. Hard links to the old file, its owner, group and ACLs are not kept. |
| `grep` | `pattern`, `path` (file or directory, default `cwd`), `files` (list; overrides `path`; searched as named), `ignore_case`, `glob` (a filter of a directory walk: the file name, or the path below the searched directory if it has a `/`; ignore rules still apply; not with `files` or a file `path`) | The Rust regex engine of ripgrep over a parallel, ignore-aware walk: `.gitignore` (in any tree, git repository or not), `.ignore`, hidden files and binary files are skipped. One `path:line:text` per match, path relative to the tool's working directory (not the searched path), sorted by path then line; a line is cut at 500 bytes (before decoding) with ` [+N bytes]`; a file with a line over 16 MiB (the searcher's heap limit) is not searched and is named in the could-not-search note. The search stops once the matches held reach the overflow cap, and a note says that some files were not searched. No match: `No matches found in N file(s)`. Notes add last lines: `[skipped N binary file(s)]` (the names, for files named explicitly), `[matching in F stopped at a NUL byte at offset N; ...]` (binary detection works a buffer at a time), an invalid-UTF-8 note, `[N paths could not be searched; first: ...]`. |
| `bash` | `command`, `timeout` (ms) | `bash -c command` in `cwd` as a session leader (no controlling terminal; `setsid` runs in the child before `exec`, so the session exists when the call starts), stdin closed, stdout and stderr on one pipe so they arrive in the order written. Exit 0: `Ok(output)`. Otherwise `Err(output + "\n\n" + "command exited with code N")` (or `killed by signal N`, `timed out after N ms and was terminated`). A `timeout` over the tool's maximum is an `Err` stating the maximum. The one `unsafe` block of the workspace is here (`pre_exec`); the workspace lint is `deny`, not `forbid`, for that. |

`write` has no output limit: its answer is one short line. The other three
share the *output limit*.

### output limit, max_output
`max_output=` on `ReadTool`, `GrepTool`, `BashTool`, `base_tools` · new

The most bytes of text the model gets from `read`, `grep` or `bash` (`read` only stops; it saves nothing): 10_000 by
default, at least 1. It bounds the *kept text*; the truncation note is extra.
Output at or under the limit is returned whole and writes no file.

### kept end
`Keep::Head`, `Keep::Tail` (`tau-tools/src/limit.rs`) · new

Which end of an over-limit output the model keeps. **`read` and `grep` keep the
head** (`read` through `Sink::unspilled`, a sink that saves no file and adds no note) (the start of a file, the first matches); **`bash` keeps the tail**
(errors and the result of a command land at the end). A judgment call of the
implementation, for John to overrule: it is one argument to `Sink::new` per
tool. Cuts fall on line boundaries and never inside a UTF-8 character. If not
one whole line fits (a single line longer than the limit), the cut is on a
character boundary instead and the note says so.

### overflow file, overflow_dir, max_overflow
`overflow_dir=` (default `$XDG_RUNTIME_DIR/tau` when that is set, else `<temp dir>/tau-<euid>`, e.g. `/tmp/tau-1000`; chosen by `Limits::default_dir`), `max_overflow=` (default 64 MiB) · new

When `grep` or `bash` output passes the limit, the output (everything the tool would
have answered, not just what it kept) is written to `<overflow_dir>/<id>.out`, `id`
being 8 random characters from `a-z0-9`. `read` writes none (see `read`). The
directory is created `0700` if missing. It must be a directory (not a symlink), owned
by the effective uid, with `mode & 0o077 == 0`. Nothing is checked at construction: a
directory that fails is found when the first file is needed, by opening it with
`O_DIRECTORY|O_NOFOLLOW`, checking it with `fstat` on that fd, and creating the file
relative to the fd with `openat`, so a path swapped in between cannot redirect it, and
another user's squatted `/tmp/tau-<uid>` costs the overflow file and never startup. A
directory that cannot be used, or a file that cannot be written (disk full), does not fail
the call: the answer keeps its text and its exit code, and the note says `the full output
(N bytes) was not saved: <reason>` (`was not saved after B bytes` when the file held `B`
bytes when the write failed); the command of a `bash` call runs on. Files are created `0600` with `O_EXCL`, so an existing file is
never touched. Whenever the directory is opened, files in it older than a day whose names are
the ones we generate (`<id>.out`) are removed (best effort). For `grep`, the file holds all matches (without a trailing newline);
for `bash`, stdout and stderr interleaved, without the exit-code trailer. The file holds
at most `max_overflow` bytes (at least `max_output`); bytes and lines past it are still
counted, and the note says the file holds only the first part.

### truncation note
`limit.rs` · new

One bracketed line telling the model what it did not get, after the kept text
(head) or before it (tail):

```
<kept lines>
[truncated: 2 lines omitted after these; full output (20 bytes) is in /tmp/tau-1000/k3x9a0bz.out]

[truncated: 2 lines omitted before these; full output (20 bytes) is in /tmp/tau-1000/k3x9a0bz.out]
<kept lines>
```

`1 line` in the singular. When the file was capped, the part after the line count reads `; the output is N bytes but <path> holds only the first 64 MiB]` (`N bytes` if not a whole number of MiB). When a single long line was cut short, `, and one
long line cut short` follows `these`. Lines are counted as newlines plus one
for a last line without a newline. Kept text that had bytes that are not UTF-8 shows them as U+FFFD and gets one more line, `[invalid UTF-8 in the output was replaced with U+FFFD]`, on the same side as the note.

### BashRun, kill
`tau._core.BashRun` · new

A started `bash` command. `Bash.start(args_raw)` spawns it as a session leader
(so its process group id is its pid) and never raises (a bad call is a
`BashRun` whose `wait` answers `Err`); `wait()` blocks, GIL released, and answers
the `Outcome`; `kill()` does not block and may be called from any thread, any
number of times. Ending a command, by `kill()` or by the timeout, is: SIGTERM to
the process group and every other process of the session, then SIGKILL after the grace period (`kill_grace_ms=`, default
2000), so the shell, its pipeline and anything it backgrounded die together.
The shell is waited for without being reaped (`waitid` with `WNOWAIT`, so its
pid cannot be reused), the rest of its group is killed, and only then is it
reaped; the SIGKILL is repeated, scanning `/proc/*/stat` for the session id (Linux only), until no live process of the session is left. A process that has called `setsid` itself leaves the session and is out of reach. Every member of the session is signalled even when one signal fails (EPERM, say); the
first failure other than "no such process" is appended to the answer as a trailer, which
keeps the output and the exit code, and is never a panic. `timeout_ms=`
is the timeout of a call that gives none (30_000); `max_timeout_ms=` (600_000)
the most a call may ask for. `BashTool.call` kills when it is cancelled and waits
for the command to be gone before the `CancelledError` goes on, and gives each
call a thread of its own. Kill and timeout run on threads of the Python process, so a
program that runs base tools must turn SIGTERM and SIGHUP into the same cancellation as
SIGINT (`loop.add_signal_handler` cancelling the main task; `run_until_signalled` in
`examples/common.py`): a process that simply dies leaves the command running in its own
session with no timeout. Building `BashTool` off Linux raises `OSError` (it needs `/proc`).
The description tells the model that background jobs (`&`, `nohup`) are killed when the
command ends and no process outlives a call; those of `read`, `write` and `grep` that
the working directory is a default, not a sandbox. A process of a session of its own that holds
the output pipe open is not killed: the pipe is read for 250 ms after the shell
exits and then closed, and the answer says "a process outside the command's
session still holds the output open" (an `Ok` with a note after a normal exit).
A `BashRun` dropped without `wait` kills its session.

### BashConfig
`tau_tools::bash::BashConfig` · new

The three times of the `bash` tool, in milliseconds: `default_timeout_ms` (a
call that gives no `timeout`; 30_000), `max_timeout_ms` (the most a call may
ask for; 600_000) and `kill_grace_ms` (SIGTERM to SIGKILL; 2_000). All at least
1, the default not over the maximum. Python: `timeout_ms=`, `max_timeout_ms=`,
`kill_grace_ms=`.

### Limits
`tau_tools::Limits` · new

What `grep` and `bash` share: `max_bytes` (the output limit), `dir` (the overflow
directory) and `max_overflow` (the cap of the overflow file). Built by
`Limits::new(max_bytes, max_overflow, dir)`; refuses a zero limit and a cap below the
limit, and checks nothing about `dir` (see the overflow file). `Limits::default_dir()` is
the directory when the caller names none. `read` has no `Limits`, only a byte limit.

### Finished
`tau_tools::limit::Finished` · new

What a `Sink` answers: `text` (the kept text with its notes), `kept_lines`
(lines shown; a cut-short line counts as one), `kept_bytes` (bytes of text shown, without notes), `truncated` (the output passed
the limit) and `cut_line` (the one line shown is only the start of a line). Used
by `read` to word its continuation hint. A `Sink` also answers `is_over` (more than
the limit pushed; `read` stops reading then) and `total` (bytes pushed).

### Cancel
`tau._core.Cancel` · new

A flag. `Read.run` and `Grep.run` take one; `ReadTool.call` and `GrepTool.call`
set it when they are cancelled, and the read loop and the grep walk (and its
searches, per match) check it and stop with an `Err`.

### Read, Write, Grep, Bash (`_core`)
`tau._core.Read` and the rest · new

The native engines under the base tools: `__new__(cwd, max_output, overflow_dir, max_overflow)`
(`Read`: `cwd, max_output`; `Write`: `cwd` only; `Bash`: also `timeout_ms`, `max_timeout_ms`, `kill_grace_ms`); every argument is
required, the defaults being the Python classes', `name`, `description`,
`parameters_json`, and `run(args_raw, cancel) -> Outcome` (`Write`: no `cancel`; `Bash`: `start`). A bad
constructor argument raises `ValueError`. Python code uses the `Tool` classes
above instead.

### Finish
`tau.Finish` (`protocols.py`) · new

What a provider yields last: `Finish(stop, reason, meta)`, the normalised `Stop`,
the provider's own string (`None` if it has none), and the response `meta`
(`None` if none; otherwise strict JSON object text, checked on construction:
`TypeError` for a non-`str`). The
driver builds the reply draft with all three. A provider that reports usage puts the
normalized `usage` in `meta` as `{"input", "output", "cached"}` token counts, beside its
own fields (D15; the mock and all three hosted providers do so; the hosted providers keep the raw
`usage` object as `usage_raw`).

### extension
retires `ExtensionRunner`, `ExtensionState`, bucket

A Python module registering hooks and tools. No special standing in core. It has a
non-empty `name`, unique among a driver's extensions, and hooks that are methods named for
their hook points (`tau.Extension` is the `Protocol`).

### frontend, client
retires head, `Backend`, `RemoteBackend`

A program that drives sessions and shows events (`tau serve` first). A **client** is what such a
program holds: a `Client`, with an id.

### Client, LocalClient
`tau.Client` (a `Protocol`), `tau.LocalClient` (made by `Driver.client(id, *,
buffer=256)`; `client.py`) · D16 · new

A frontend's whole view of a session; it never touches `Driver` or `Session`. Async calls that
act as the client (`input`, `send`, `step`, `go_to`, `set_auto`, `abort`, `open`,
`set_label(cursor, label)`, `remove_label`, `close_cursor`, `reopen`, each with the client's id as `by: Frontend(id)` where
the driver takes one; `go_to`, `abort` and `close_cursor` return nothing), `watch`, `answer`, `commands`, `run`
(returns the command's result), `options`, `configure`, `complete` (see *command*, *options
(extension)*, */config*, *complete*), the reads `node`, `walk`, `children`, `close`, and one event
stream: the client itself is an async iterator of `Event`. Each call is documented once, on
`Client`. `id` is stable across reconnects (`john@laptop:tui`); a connection is not a client.
Every argument and event is a plain frozen value. For a remote client (Phase 6) the values of
the protocol that are not plain (the core's `Node`, `Body`, `Author`, `Item`, `ItemData`,
`Ending`) need one codec each; events and the rest travel as they are. `LocalClient` is the
in-process implementation: creating one connects, taking the snapshot and subscribing in one
synchronous act on the loop. A client is **connected** while it has a stream open; `close()`,
a cut-off, or dropping the `LocalClient` (the hub holds streams weakly) disconnects it. Every
call raises once the driver has failed or stopped. A stream that ends in error raises
`ConnectionError`: **CutOff**, or one whose message is the `"Type: msg"` of the failure that ended
the driver, chained to it; after a plain stop the stream ends cleanly, and a client connecting
after either gets a stream already ended. A frontend's one rule is `except ConnectionError`,
then reconnect: a new client with the same id, since the log is the truth and there is no
resume. Its constructor is the driver's: `LocalClient` takes a private `Calls`, which holds the
driver's side of each call (`_calls.py`) so `Driver`'s public surface is `start`, `stop`, `rest`,
`use_catalog`, `reload`, `state`, `client` and `add_item`.

### Calls
`tau._calls.Calls`, `frontend` (private) · D16 · new

The driver's side of each call of the `Client` protocol: it holds the session, hub, questions and
commands, and implements `connect`, `watch`, `input`, `send`, `step`, `go_to`, `abort`,
`set_auto`, `open`, `add_item`, `set_label`, `remove_label`, `answer`, `forms`, `run`, `configure`,
`options` and `complete`, and `close_cursor` and `reopen` (D17). `LocalClient` calls it; the calls are documented once, on `Client`.
`frontend(check, hub, poke)` is the wrapper a call that changes the session runs in: check the
driver is running, change, publish what changed, wake the pump. It exists so that `Driver` has no
call a frontend makes.

### event
`tau.Event` (`events.py`) · D15, D16 · retires `AgentEvent`, `WireEvent`, status

What a client hears: `Live | CursorView | CursorGone | Labels | Appended | Notice`. Small state
is sent whole whenever it changes, to every client, for every cursor; `Live` goes to clients
watching the cursor and `Notice` to one client. Observers cannot change anything. One ordered
stream per client: a change's `Appended` comes before the `CursorView` that points at its
nodes, and a `CursorView` or `Labels` that replaces an earlier copy removes it
and goes to the end. A stream opens with a `CursorView` per open cursor, a `CursorGone` per closed one, and `Labels`; the nodes are
not in it (a node never changes, so a client reads the ones it needs by id with `walk`, and
ignores an `Appended` node it has already read). Events are compared by value; a `CursorView`
holds schemas (dicts), so it is not hashable. What an extension shows (`cursor.show`) rides in the `CursorView`.

### CursorView
`tau.CursorView(cursor, at, busy, auto, questions, items, ending, hook_error, model, shown)`
(`events.py`) · D16 · new (the event D16 calls `CursorState`; that name is the read type of
`Session.cursor`)

One cursor as a client sees it, whole: `at`, `busy`, `auto`, its open `questions`
(`OpenQuestion`, oldest first), its pending `items` (whole), how its last step ended (`ending`,
an `Ending`) and the `hook_error` (`HookFailure`) of a cursor hook that failed in it. The two
are `None` from the start of a step until it ends, and after a reload: core keeps neither,
the driver notes them from the `post_step` job and the failure. Not in the tree and not
replayed, but in every snapshot, so a late client sees why a step stopped. `model` is the
qualified name of the model the cursor's branch is on (the last one a `Config` on the walk to
`at` names: what its next sample uses), read from the options' fold of that walk, so it costs
nothing per event; `None` if no `Config` on the walk names one or one is unreadable (a sample
fails with why; the view never raises). `shown` is what each extension shows on the cursor
(see **show**), as `(extension, text)` pairs in extension-name order.

### show
`Cursor.show(text)`; `CursorView.shown`; `Hub.show` · Phase 5 stage D · new

**show** is one line of text per extension per cursor that every client is sent, for what the
person cannot otherwise see of an extension's state: plan mode's mode, a loop's iteration, a
search's round. `cursor.show(text)` (on a cursor handle, so from a cursor hook or a command that
takes a `Cursor`; a retired handle raises `NotInHook`) sets this extension's line on the handle's
own cursor and replaces what it showed; `cursor.show(None)` shows nothing. `TypeError` for
anything else. It is the driver's memory, like a step's ending: not in the log or the
scratchpad, so it is gone when the session is reloaded (a hook shows it again), and it is
dropped when the cursor closes (`reopen` starts with none) and when a `reload` removes the
extension. From a sync command's worker thread the write hops onto the loop and waits for it, as
`abort` does, so the hub is only ever written there. Each change is sent whole, as a
`CursorView`, which a client that has not read the last one never sees (the stream keeps one
`CursorView` per cursor): `show` is state, not a message; use `tell` for a message. A line client
prints a change as `[extension: text]` and a removal as `[extension: cleared]`. A tool hook's
handle has no `show` yet.

### HookFailure
`tau.HookFailure(extension, point, error)` (`events.py`) · D16 · new, retires `HookError`

A cursor hook of `extension` raised, or broke its contract, at hook `point`; `error` is
`"Type: msg"`, the text a `post_step` hook sees as `Ending.Failed`. Never for a tool hook (that
is the `Err` in `Results`).

### Labels
`tau.Labels(labels)` (`events.py`) · D16 · new

Every label and its cursor, `((label, cursor), ...)` in label order.

### Appended
`tau.Appended(nodes)` (`events.py`) · D16 · new, retires `Saved`

Nodes added to the tree, in id order, bodies included. A client drops a call's `ToolProgress`
layer when the `Results` node answering it arrives, and a cursor's reply layer (text, thinking
and call deltas) when its `CursorView` has a different `at` or is not busy. A call answered
before its batch is whole adds no node, so nothing is sent.

### Notice
`tau.Notice(text)` (`events.py`) · D16 · new

A message for one client, or for every client: `cursor.tell` (without `to`: all), an input a hook
refused. Sent only to the clients with that id; dropped if there is none connected.

### CutOff
`tau.CutOff` (`events.py`; a `ConnectionError`) · D16 · new

What a client's stream ends with when its `Appended` and `Notice` backlog passed its buffer.
Nothing buffered is delivered. The client is disconnected; it reconnects to a fresh snapshot.

### watch
`Client.watch(cursors)` · D16 · new

Choose which cursors' `Live` the client hears: exactly `cursors`, replacing the last call
(`NoSuchCursor` for one that is not open). For each cursor newly watched the stream starts with a
catch-up taken in the same act: the parts of the sample in flight that are not in the tree
yet (as `TextDelta`, `ThinkingDelta`, and `CallNamed` with `CallArgs`, calls numbered in
order) and the text of the open part (`SampleRun.catch_up`). Tool progress has no catch-up;
the result arrives as `Appended`.

### hub, Feed
`tau._hub.Hub`, `Feed` · D16 · new (private)

The driver's clients. `Hub.sync` is the one place state events are made: it compares the
session, the driver's questions, and the steps' endings with what it last published and sends
the difference (new nodes, then each changed `CursorView`, then a `CursorGone` for each cursor closed since,
then `Labels`, then notices). The
driver calls it after each client call, each pump pass, each handle write, and each question
opened or closed. `Hub.connect` publishes what is pending, then builds the snapshot from what
was published, so subscribing and the snapshot are one act. A **Feed** is one client's stream:
its buffer holds at most `buffer` `Live` and `buffer` of `Appended` and `Notice`, plus one state
event per cursor (its `CursorView`, or the `CursorGone` that replaces it) and one `Labels`; a `Live` that finds its share full is dropped,
and an `Appended` or `Notice` that finds its share full cuts the client off. One consumer: a
second concurrent `anext` raises `RuntimeError`.

### Delta, TextDelta, ThinkingDelta, CallNamed, CallArgs
`tau.events` (exported) · D15 · new

What a provider's `sample` yields while a part is still arriving, with no cursor
(the provider does not know it): `TextDelta(text)`, `ThinkingDelta(text)`,
`CallNamed(index, id, tool)` (a call begins forming), `CallArgs(index, text)` (more
of its `args_raw`). `Delta` is their union: what a provider may yield. Never saved.

### tool progress, ToolProgress
`tau.ToolProgress(node, call_id, text)` (`events.py`) · D15 · new

What a running tool shows through its progress callback: `text` is the output that arrived
since the previous `ToolProgress` of the same call, not the whole so far. The call is `node`
(the batch's node, the `Assistant` node holding the call) and `call_id`: servers may reuse ids
across batches, so the id alone is ambiguous. Not a `Delta`: a provider cannot yield one. The
driver wraps it as `Live(cursor, ToolProgress(...))` while the call runs. Never saved: `Results`
holds only the `Outcome` the tool returns. Obeys the buffer rules of any `Live` (a full
buffer drops it).

### LiveDelta
`tau.LiveDelta` (`events.py`) · D15 · new

`Delta | ToolProgress`: what a `Live` carries. A provider's `sample` yields only `Delta`.

### progress callback, Progress
`tau.Progress` = `Callable[[str], None]` (`protocols.py`) · D15 · new

What a tool is given to show partial output: `progress(text)`. A hand-written `Tool` opts in by
giving `call` a parameter named `progress`, detected once when the driver is built
(`takes_progress`, once, in `check_tools`); a `progress` that cannot be passed by keyword
is a `DefinitionError`.
Callable from any thread. After the call is over (returned, aborted, cancelled) it does
nothing and raises nothing; empty text is dropped; a non-`str` raises `TypeError`. A described
function takes it as a parameter annotated `Progress`, whatever its name, an **injected
parameter** that is not one of the tool's `parameters`; a parameter named `progress` with another
annotation is an ordinary argument.
`bash` calls it with its output, at most once per 100 ms; the command never waits on it, the
answer waits for a call already running and the final flush.

### live event, Live
`tau.Live(cursor, delta)` · D15, D16 · new (the ADR's first draft said "draft event")

A `LiveDelta` on the cursor whose sample (or tool call, for `ToolProgress`) produced it. Never
in the tree. Heard only by clients watching the cursor (**watch**); a client whose share of the
buffer is full loses it with no mark: live events are advisory. Client rules: ignore a
`CallArgs` whose `CallNamed` you never saw; draw the stream as a layer over the cursor and drop it
when the cursor's `CursorView` has another `at` or is not busy; drop a call's `ToolProgress`
layer, keyed by `(node, call_id)`, when a `Results` node of that batch answering the call arrives
in an `Appended`.

### driver
Python `tau.driver.Driver` · new

Performs jobs and answers each with its result and the time. Decides nothing. The only
place that awaits. `Driver(session, providers, catalog, *, tools, extensions, clock, user_config)`:
built from a session, a mapping of provider name to provider (each keyed by its own
`name`, else `ValueError`; a sample whose model names another provider fails with the
known names in the `sample_error`), a `ModelCatalog` (where it resolves the model name in
`config_at(at)` at each sample), tools, extensions, and a clock. `use_catalog(catalog)`
replaces the catalog for samples dispatched after it (a frontend that reloads the config
files calls it); a sample already running keeps the model it resolved. `reload(extensions)`
runs other extensions in place of the driver's (the driver's own `tools` stay) from the next job
it dispatches: it rebuilds the options, the tools and their owners, and the commands, and forgets
every folded `Config`, all or nothing (`DefinitionError`, nothing changed, as at construction). It
is refused with `Busy`, naming the cursors, while any cursor is busy or any effect is out (a
command waiting for its step has no effect out, but its cursor is busy), because every job holds
the set it was dispatched with (a tool call is an effect, so a tool cannot reload the driver it
runs in); a failed or stopped driver raises as every call does. The tree is not
rewritten: a `Config` node that sets options of an extension no longer there, or that no longer
fit its options type, fails when read (`ConfigError` from `options()`), not at the reload. What the
removed extensions showed (`show`) is dropped, and the clients are sent the views without it. Apart
from that, like `use_catalog` it tells no client anything: a client asks `commands()` and `options()` again, so
what those return changes from the call after the reload. Its public surface is
`start`, `stop`, `rest`, `use_catalog`, `reload`, `state`, `client(id)` (how a frontend gets a
`LocalClient`) and `add_item`; the calls a client makes are implemented by a private `Calls`
(`_calls.py`), documented on `Client`: they run on the loop thread, call the session, publish what
changed (`Hub.sync`), then poke the pump. `add_item(to, data)` is for whoever has something to
tell a resting cursor from outside a hook (a timer extension, `tau serve` relaying a webhook): any
`ItemData` but `Input`, which only `input` and `send` add (they say who wrote it).
Once the driver has failed or stopped, every client call raises (the failure, or `RuntimeError`),
and a stream connected then ends at once (with the failure, or cleanly after a plain stop).
Extensions are a list given to the constructor, in hook order, checked there
(**check_extensions**); how `config_at` will name them is open.
`stop()` ends the clients' streams. A failure of the pump or an effect cancels the effects
still running, ends the streams with it, and makes `rest` raise it; after `stop()`, `rest` raises
`RuntimeError`, also for a caller already waiting. A hook adds its extension's item through
its handle. `state()` is the one read a harness needs (**DriverState**).

### pump
`Driver._pump` · new

The one coroutine per session that calls `next_job` and `answer` (D11 rejects a
task per cursor). Loop: for each cursor with no effect out that is busy,
take its next job and start it as a unit; then await the queue and
answer with what arrives. `next_job` is not idempotent, so it is never called while a
cursor has an effect out, except after a `CallDone` (expecting `Pending`).

### unit
`Driver` (`DriverState.units`) · new

One task performing one effect: a hooks job, a sample, or one call of a batch.
It reports `(cursor, Answer)` to the pump's queue when done.
A cancelled unit reports nothing.

### DriverState
`tau.driver.DriverState` (not exported), from `Driver.state()` · new

A frozen snapshot for a harness telling quiet from busy: `units` (effects running),
`queued` (the pump has a report or poke to take, or a busy cursor with no effect out),
`open_questions`, `parked` (effects parked in the driver: one with a question open, or a handle's
`rest` or `close_cursor` out, waits for an answer or another cursor, and counts once however
many of those it has out, so a harness counts it beside a held gate), and `failure` (what the pump or an effect died of, else `None`). Replaces the separate reads and `Driver.check`; reading it
needs the session open.

### poke
`Driver._poke` · new

A **poke** wakes the pump after a frontend call.

### Subject
`tau.Subject` · new

The mutable holder a cursor hook receives: `draft` (`pre_input`,
`pre_sample`, `pre_reply`), `node` (`post_reply`, `post_result`), `ending`
(`post_step`). The hook's edits to `draft` are what the hook answers with.

`tools` is for `pre_sample` alone: the names of the tools the sample about to be asked is
offered, or `None` (every hook starts with it) for all of them. What the hooks leave there when
the point is done is the offer; the sample is given the driver's tools of those names, in the
driver's order, and the next sample starts at `None` again. `[]` offers none. A name that is not
one of the driver's tools (extensions' included) fails the step as a hook that breaks its
contract does, `ValueError` naming the unknown names and the tools there are; so does a `str`
(`TypeError`) or `tools` set at any other point (`ValueError`: nothing else uses it). An offer is
not a wall: a model may call a tool it was not offered, and the call runs unless a `pre_tool`
hook refuses it. An extension reads the driver's tool names with `Cursor.tools()`, and offers
those of its own choosing that are among them (plan mode's `read`, `grep`, `bash`); naming one the
driver lacks fails the step. Every `pre_sample` answer replaces the last offer: the next sample
starts at `None` again, also after a step that was halted, answered by a draft, aborted or closed.

### CallDraft
`tau.CallDraft` (`protocols.py`) · new

The call draft a tool hook receives: `{id, tool, args_raw, outcome?}`. `id` and `tool` are
read-only (an edit would be ignored, so setting one raises `AttributeError`); `args_raw` and
`outcome` may be set. Setting `outcome` in `pre_tool` answers the call (later `pre_tool`
hooks still run); in `pre_result` it edits the outcome. The hook that changed the outcome is
its `by`.

### SampleError
`tau.SampleError` · new

What a provider raises to fail a sample. Parts it yielded first are kept: the
reply is committed with `stop: Error` and the error's `meta`. The driver
answers `Sampled { parts, end: Failed { error, meta } }`; core decides which. Any other
exception from a provider fails that one sample (D15); an invariant break in the driver
stops it, surfacing through `DriverState.failure` and the event streams.

`SampleError(message, meta, *, status=None, retry_after=None)`. `status` is the HTTP status
of an error response (`None` when there was no response); `retry_after` is the seconds the
server asked to wait (`None` if it did not say). A retrying wrapper provider reads both
(D15); nothing in core or the driver does.

### ProviderClosed
`tau.ProviderClosed` (a `SampleError`; `protocols.py`) · D15 · new

What a sample raises when its provider was closed (`aclose()`) before it started, or while
it ran. Not transient: a retry cannot succeed, so a retrying wrapper provider passes it on
instead of retrying as it would a connection error.

### rest
`Driver.rest(cursor)` · new

Await until the cursor is at rest: not busy, no effect out.
Re-raises what an effect or the pump died of. A cursor held by an open
question or a gate does not rest. The pump wakes its waiters after each pass.

### Clock, SystemClock
`tau.Clock`, `tau.SystemClock` · new

`now() -> int` (milliseconds). The driver reads time only through it, so tests
move time themselves.

### record
*spike* `tree::Record` · new

What must be made durable, in order, queued in `Session::outbox`:
`Write { node, mv }` (a node and its move, one record), `Move`, `Label`,
`Close { cursor }` (a cursor is closed; see **close (a cursor)**; additive, so `VERSION` is unchanged),
or `Sync { pad }` (a sync point; `pad` is the scratchpad as it stands there,
if it changed since the last sync, for the store's scratchpad file — never
written to the log).
Queued in the **outbox** (`Session::outbox`) until the store writes them.

### sync point
*spike* `Session::sync`, `Session::synced`, `Record::Sync` · new

Where queued records must reach disk: before each post hook and when a step
ends (`post_reply`, `post_result`, `post_step`, then rest or restart). A
phase's writes, its pre hooks' included, share one sync. `synced()` is true
when nothing changed since the last `Sync`.

### store
`tau_store::Store` · retires session store, `SessionCatalog`

The only Rust code that touches the filesystem: one session per directory,
holding the **log** (`log`, append-only records) and the **scratchpad file**
(`scratchpad`, rewritten by rename) (D12). Each file starts with a magic and
a version (`VERSION`); a record on disk is its length, a CRC-32, and the
record as JSON. `Store::create(dir, session)` writes a new session whole and
links it into place; `Store::open(dir)` takes the lease and loads;
`Store::save(session)` takes the outbox, writing and syncing the log at each
`Sync`, then the scratchpad file. Its errors (`tau_store::Error`): `Io`,
`Leased`, `Exists`, `NotASession`, `Version`, `Corrupt` (a damaged
scratchpad file), `Undecodable` (an intact log record that does not decode),
`Core` (the log does not replay), `TooLarge` (a record of 4 GiB or more, refused
while encoding: nothing of that `save` is written, and the session closes as
after any failed save). `save` consumes the
store and returns it on success: after an `Io` error it is gone with its
lease, as after a crash, and the session is opened again. **JMFTS** (Phase 7) is a replica of the log,
never the store of record.

### TooLarge
`tau.TooLarge` (`tau_store::Error::TooLarge`, a `ValueError`) · new

A record whose encoding does not fit the log's `u32` length (4 GiB or more).
It was a panic that poisoned the session; it is an ordinary failed save now.
The record is refused before anything of that save reaches the log, so the
session closes and reloads as last synced.

### torn tail, dropped
*spike* `Store::dropped`

Whatever follows the log's last good `Sync` on open: a record cut short,
one failing its checksum, records never synced, damage. Open cuts it (so a
session goes on from its last sync) and keeps the bytes as
`dropped-<offset>` in the session directory; `dropped` is how many. Only a
record that is short or fails its checksum is torn. A record whose length and
checksum agree but whose JSON does not decode is intact, not torn: open fails
with `Undecodable` (Python `tau.Undecodable`, a `ValueError`; names the
log file, the record's position from 0, and its byte offset) and cuts nothing,
wherever in the log it is. Rust only:
the Python `Session.dropped` was removed, because nothing but a test asserting
`0` ever read it.

### lease
*spike* in `Store` · retires `SessionWriteLock`

An exclusive `flock` on the log, held while a `Store` lives. Only the holder
writes and runs steps; a second open is `Error::Leased`.

## Testing

### Harness
`tests/behavior/harness.py`

What every behavior test drives, shaped like `tau serve`: a session in a
directory with a `Driver` over the fakes, and a client (`Driver.client`) for client `term`.
`Harness.create`/`load`; the client's calls (`input`, `send`, `step`, `abort`, `go_to`,
`set_auto`, `respond`: answer the cursor's oldest open question); `connect(id)` for another
client and `view(cursor)` for a cursor as a client connecting now is told of it; `settle`;
`crash`; `dies` (wait for the driver's failure); snapshots (`snapshot`, `bodies`, `nodes`,
`notices`: those the `term` client was sent).

### settle
`Harness.settle` · new

Yield to the loop until the driver is quiet (no report or poke queued, no busy
cursor without an effect out) and every unit is **parked**, on two passes in a
row. Never sleeps and never advances the clock; re-raises what a unit died of.
Gives up with an error after 10,000 passes.

### parked
A unit waiting on something only a test releases: a held `Gate` or an open
question. `settle` compares the driver's unit
count with the parked count.

### crash
`Harness.crash` · new

Cancel every task without answering, close the session unsynced, reopen it from
disk, start a new driver on the same fakes.

### Gate
`tests/behavior/fakes.py` · new

A latch a fake waits at. `held` counts waiters; `release()` lets them and all
later arrivals through.

### FakeProvider, FakeTool, FakeClock
The only fakes allowed (rule 4): scripted samples (`Reply`, `Fail`; a `Gate` after
`gate_after` parts; each request's walk ids and resolved model recorded; `fake_catalog` and `FAKE_CONFIG` give the Harness its one model), scripted
outcomes (calls recorded), and time that moves only on `advance`.

### Transcript
`tests/providers/fixtures/`, `tests/providers/transcripts.py` · new

A real server's response recorded as raw bytes (`*.sse`, or `http_error.http`
with its status line and headers) and replayed through `httpx.MockTransport`
(`Server`, with `GatedStream` and `CutStream` for streams a test holds or
breaks). Recorded from llama.cpp serving Qwen3; never edited.

### Recorder
`tests/behavior/fakes.py` · new

Not a fake: a real extension that records the hooks it is run at (`points`,
`subjects`, `endings`) and returns the string a test gives it for a point.

## Tracing

Observability only: none of these changes what a session does, and none is
in the model. Findings are in `docs/as-built/traces/`.

### span
`tracing` crate spans in `tau-core`, `tau-store`, and `tau-py` · new

A named interval in the code, made with the `tracing` facade, which costs a
static check and nothing else while no subscriber is installed. Names are
`core.*` (the machine: `write`, `append`, `answer`, `next_job`), `store.*`
(`save`, `append`, `fsync_log`, `write_pad`, `fsync_pad`, `fsync_dir`), and
`py.*` (a `Session` method's whole body, with `py.lock`, `py.machine`, and
`py.save` inside it). `tau-core` only makes spans: it installs no subscriber
and does no IO.

### trace
`tau.trace` · new

One recording of spans, written as a Chrome trace file (Perfetto,
chrome://tracing) by `tracing-chrome`. `trace.start(path)`, `trace.stop()`,
and `trace.capture(path)` (both as a context manager). At most one runs per
process. The driver's own spans, `trace.span(kind, name)` (`hook`, `sample`,
`tool`, `driver`), are timed on the native clock and merged in by `stop`, one
track per asyncio task; they are no-ops when no trace runs.

### trace_to, stop_trace, trace_clock
`tau._core` · new

The native half of a trace: `trace_to(path)` installs the global subscriber
the first time and starts recording, `stop_trace()` returns when the file is
complete, `trace_clock()` is microseconds on the spans' own clock. Not for
callers other than `tau.trace`.

### TraceError
`tau.TraceError` (`tau._core.TraceError`, a `RuntimeError`) · new

A trace started while one runs, stopped while none runs, or that cannot be
installed because another global tracing subscriber is.

## Retired terms

| 0.15 term | replaced by |
|---|---|
| entry, side entry | node, side node |
| incomplete, finalize | nothing: nodes are written once |
| leaf, default leaf | `at` |
| head cursor, acting cursor | every cursor is explicit |
| `TurnFrame` | the `Config` on the walk |
| turn (four senses) | step (D1) |
| submission, multitask strategy | input items (D2) |
| suspension, responder | auto-step off; a question |
| lane | nothing |
| head | frontend |
| backend | provider or frontend |
| fork (in-session) | branch |
| compaction entry, splice | the compaction extension (D3) |
| `input` hook | `pre_input` |
| `before_agent_start` | `pre_sample` |
| `tool_call`, `tool_result` | `pre_tool`, `pre_result` |
| `turn_end`, `user_turn_end` | `post_reply` or `post_step` |
| `annotate`, node-written hooks | event observers |
| session write lock | lease |
| handler policy | nothing (Q9) |
| `@tool(describe={...})` | `Annotated[T, "text"]` on the parameter; no decorator, the driver describes the function (D16) |
| `Saved` | `Appended`, and the `CursorView` that points at its nodes (D16) |
| `HookError` (event) | `CursorView.hook_error`, a `HookFailure` (D16) |
| `EventStream`, `Driver.events` | the `Client`, which is its own stream (D16) |
| `Driver.questions`, `Driver.respond`, `Question` | `CursorView.questions`, `Client.answer`, `OpenQuestion` (D16) |
| `CallKey` | nothing: a `Results` node in `Appended` names the calls it answers |

Retired during design review: committed, uncommitted, boundary, cycle, base
extension, `deliver`, `combine`, returned input, run, batch states,
journal, journal item, `idempotent`, `Result`, fast-forward, claim,
`Session.rerun`, `Skipped`, refuse/handled/pass, restating `Config`, label on
the move record, `post_open`, `post_input`, interrupt, scratch, the throttle
(`THROTTLE`, `Pos::Throttle`, `Job::Wait`, `Answer::Waited`, `Clock.sleep_until`).
