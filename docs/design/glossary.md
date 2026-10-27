# Glossary

Status: **for review (Phase 1), sixth revision.** On sign-off this file and `model.md`
supersede `docs/plan.md` §1.

Rule 2: no type, field, or module-level concept exists without a definition here.
Each term has one meaning. If a word needs a second meaning, it needs a
second word.

Identifiers are proposals until Phase 3 builds them. Rust names live in
`tau_core`, and Python names in `tau_next`. "Retires" lists the τ 0.15 terms
(from the as-built review §7) that the term replaces. "New" means 0.15 had no
counterpart. The full retirement table is at the end.

Questions marked **Q*n*** are open and listed in `model.md`, Open questions.

---

## The tree

### node
**Rust** `Node`, `NodeId` · **Python** `Node`, ids are `int` (Q8) ·
**retires** entry, message entry, custom entry, side entry, finalize

One immutable record in a session:
`{id, parent?, time, by, body, copied_from, derived_from, delivers}`. It is
written once and never changed (I2). There is no node written in two parts,
and no second write that completes one.

### body
**Rust** `Body` · **Python** `Node.body`, one class per kind ·
**retires** entry type, `customType`

What a node holds. A body is exactly one of six kinds:

| kind | holds | reaches the model? |
|---|---|---|
| `System` | the system prompt text | yes |
| `User` | text and images from a person, or written on their behalf | yes |
| `Assistant` | parts (text, thinking, calls) in the order they streamed, and a stop reason | yes |
| `Result` | one call's id and its outcome | yes |
| `Config` | a full snapshot of the config in force | no |
| `Custom` | `{kind, data}`, owned by an extension or core | no (Q3) |

The first four kinds are **message-bearing**. `Config` and `Custom` are
bookkeeping, and the provider never sees them.

Known `Custom` kinds are listed here as they are introduced:
- `sample_error`: written by core (`by: core`). It records, uncommitted, a
  sample that failed before any output arrived (`model.md` §8).

### part
**Rust** `Part` · **Python** `Part` (one class per kind) · **retires** content
block (as a mutable list)

One piece of a reply, in the order it streamed:
- `Text(text)`;
- `Thinking(text, signature?)`;
- `Call{id, tool, args_raw}`.

Parts are stored as they arrived and never reorganised. Every structured view,
such as runnable calls, is derived from them.

### stop
**Rust** `Stop` · **Python** `Stop` · **retires** `stopReason`,
`AgentEndReason` (as a reply property)

Why a reply's generation ended, normalised across providers. The provider's
own string is kept as `stop_raw`.

| `stop` | means |
|---|---|
| `end` | the model chose to stop |
| `tools` | the model handed off to tool execution |
| `length` | the output limit was reached |
| `refused` | the provider's filter or refusal |
| `interrupted` | we cancelled it (abort) |
| `error` | the stream failed after output had arrived |

Only `tools` runs calls. On every other stop, the reply's calls are not
runnable and owe no result: core writes nothing for them, the cursor rests on
the reply, and `next` is `Wait`. The provider renders them as text, per
`partial_calls`.

### call
**Rust** `Call`, `CallId` · **Python** `Call` · **retires** tool call,
toolCall block, `BlockedCall`

A part of an `Assistant` body that requests one tool: `{id, tool, args_raw}`.
- `CallId` is the provider's id, unique within its node.
- **`args_raw`** is the argument text exactly as the model produced it, which
  may be truncated or malformed. It is parsed when the call runs and never
  re-serialised for storage.
- A call is **runnable** when its reply's `stop` is `tools` and its `args_raw`
  parses.

A call is never a node of its own, because one sample produced all of a
reply's calls at once.

### batch
**Python**: none (a view) · new

The calls of one `Assistant` node with `stop == tools` and at least one call.
A `stop == tools` reply with no calls is an ordinary reply, not a batch:
`next` is `Wait`. On a walk, a batch is in one of three states:
- **unstarted**: no results yet;
- **partial**: some calls answered;
- **complete**: every call answered exactly once (I4).

A cursor's `at` may be the end of a partial run, but never a node above the
end of a run, except where a re-run placed it (I3). A move onto the batch or
into a run fast-forwards to the run's current end; only a re-run lands inside
on purpose. A copied batch that is not complete on the walk cannot be landed
on: the driver never runs a copied call.

### run
**Python**: none (a view) · new

A batch's result nodes on one line: one per call, each under the previous, in
the order the calls finished. A batch may have several runs, and a run may
branch partway; each line answers each call once (I4). A second run only ever
comes from a deliberate **re-run** (`Session.rerun`).

### result
**Rust** `Body::Result` · **Python** `Result` (body class) · **retires** tool
result, toolResult message, `Results` (one node per batch, D7's old default)

A node answering one call: `{call, outcome}`. Its parent is the batch's
`Assistant` node (for the first result) or the previous result in the run. It
must answer a call of the batch on `walk(parent)` that is not yet answered on
that walk (I4, checked at append).
On every line, every call in a batch gets exactly one result before anything
else may follow the batch, even when the call is blocked or its output
redacted.

### outcome
**Rust** `Outcome` · **Python** `Outcome` · **retires** `is_error`,
`BlockedCall`

What a result says:

| outcome | meaning |
|---|---|
| `Ok(value)` | the tool ran and returned a value, or a `pre_tool` hook supplied one (cache, mock) |
| `Err(error)` | the tool ran and failed, or could not run (unknown tool; `args_raw` doesn't parse: `Err("invalid arguments")`), or a hook raised: `Err("hook error")`, with any raw output withheld (`model.md` §7) |
| `Skipped(reason)` | deliberately not run: a `pre_tool` hook declined it ("blocked by …", "permission not granted") |
| `Interrupted` | no result was written: an abort stopped the call, or stopped it before it started, or a crash came before its result was written. The tool may or may not have run (D4) |

### author
**Rust** `Author` · **Python** `Node.by` · **retires** `source:"agent"`,
submitter, `SubmissionSource`

Who wrote a node:
- `frontend(client)`: a person's input, named by the client that sent it.
  A delivered input is its typing client's, because its content is that
  person's text.
- `provider`: a sample.
- `tools`: a result from running a call, and anything a tool appends.
- `core`: the session's first root and its `System` node (`Session.create`),
  I6's restated config, a `sample_error` record, and every result core writes
  itself: `Interrupted`, `Err("invalid arguments")`, `Err("hook error")`.
- `extension(name)`: a hook, including a root a hook creates (a detached
  root), and a node a hook rewrote or wrote itself.

A node's author is whoever produced its content as written. A hook's answer or
rewrite makes that hook's extension the author, and so does a copy. The author
is recorded and never used for control flow (Q7). No API caller supplies it:
the driver fills it in from the caller, because hooks get a cursor handle
bound to their extension and frontends call through a client handle. A move
record's `by` works the same way.

### time
**Rust** `Node::time` · **Python** `Node.time` · **retires** `timestamp`

The wall-clock time a node was appended. The driver supplies it; core never
reads a clock. The plan calls this field `at`, which collides with
`Cursor.at` (Q1).

### copied_from
**Rust** `Node::copied_from` · **Python** `Node.copied_from`,
`Session.copies_of(node_id)` · new

A list of the nodes whose content this node repeats *verbatim*, in order,
under a different parent. Most nodes have none. A copy has its own parent and
therefore its own context (I1).
- Compaction's copied tail lists one node per copy.
- The store keeps an in-memory **reverse index**, built at load and never
  stored, so `Session.copies_of(n)` is one lookup.
- The tail copies `Config` nodes too, verbatim. A copied `Config` counts as
  explicit under I6, so the copy keeps the config it records.
- A copied batch (an `Assistant` node with calls) must be followed by copies
  of its results. A move onto a copied batch that is not complete on the walk
  is refused, so the driver never runs a copied call (I3). A hook that
  crashes partway through a copy leaves an unlanded, uncommitted copy.

### derived_from
**Rust** `Node::derived_from` · **Python** `Node.derived_from` · new

A list of the nodes whose walks this node's content was *computed* from. Most
nodes have none.
- A summary points at the last node of the range it summarised (the range is
  that node's walk below the summary's own parent), and at the detached-root
  reply that produced it, if one did.
- A prune rewrite points at the node it rewrote.
- A sub-agent's answer points at the sub-agent's final node.

One id per input is enough, because the walk to each id is that input.

### delivers
**Rust** `Node::delivers` · **Python** `Node.delivers` · new

A list of the scratchpad item ids a node consumed. Most nodes have none. It
holds ids, never content. `cursor.deliver` sets it, and a hook's
`cursor.write` may. It is the only record that an item was consumed: load
drops any item a node delivers (I7), so a crash between the delivering write
and the item's removal neither loses nor repeats it, and the content is
stored once, in the node.

### session
**Rust** `Session` · **Python** `Session` · **retires** `AgentSession`,
`SessionLog`, file `Session`, `SessionManager`, `AgentSessionRuntime`,
catalog, `ReplicaSession`

One conversation: a forest of nodes, its cursors, and its scratchpad. A
session is persisted as its record log (nodes and move records) and its
scratchpad file, and nothing else (I7).

**`Session.create(config, system)`** appends the root `Config` and a `System`
node under it (both `by: core`), and opens the first cursor at the `System`
node (Q5). In 0.15 this word had ten meanings; here it has one.

### tree
**Python**: none (a view) · new

A root and all its descendants. A session is a forest of one or more trees.

### root
**Python**: none (a view) · **retires** session header entry

A node with no parent. Every root is a `Config` node, so every walk starts
with the config in force (Q5). A session's first node is its first root,
written by `Session.create` (`by: core`). A root a hook creates is
`by: extension(name)`.

### detached root
**Python**: none (a view) · **retires** side-completion sessions,
`_EphemeralConversationSession`

Any root after the first. It holds one-off work done outside the
conversation, such as a cold summary from a cheap model, or a sub-agent's
line. Its `Config` records which model did the work. Its nodes are
uncommitted unless a cursor moves onto them or opens there.

### walk
**Rust** `Session::walk(id)` · **Python** `Session.walk(node_id)` ·
**retires** fold, path, `Cursor.context()`, compaction splice

`walk(n)` is the nodes root → … → `n`, following `parent`, inclusive. Nothing else: no splicing,
no filtering, no rendering. Every view the model gets is a walk.

### tip
The last node of a walk: `walk(n)`'s tip is `n`.

### line
A walk, seen as one path through the tree. To branch is to leave one
line for another.

### LCA
**Rust** `Session::lca(a, b)` · **Python** `Session.lca(a, b)` · new

Lowest common ancestor: the deepest node on both `walk(a)` and `walk(b)`. None
when `a` and `b` are in different trees.

---

## The scratchpad

### scratchpad
**Rust** `Scratchpad` · **Python** `Session.scratchpad`,
`Session.scratchpad_of(cursor)` · **retires** submission queue, steering
queue, follow-up queue

A session's persistent, modifiable list of **items**, off the tree. A
**cursor's scratchpad** is the items addressed to it, in the order they were
added. It is how input waits for a busy cursor and how one cursor leaves word
for another.
- **Anyone may add, update, or remove any item**: any hook, through its
  cursor handle, on any cursor's scratchpad; a tool, through its handle; a
  frontend, through its client handle.
- **An extension reads its own cursor's scratchpad only in `pre_step` and
  `post_step`.** Frontends read any scratchpad at any time.
- **Adding an item wakes the cursor it is addressed to** (see wake).
- Nothing in it reaches the model (I1). To act on an item, an extension
  writes a node.
- It is stored as its own file, rewritten whole and atomically on each
  change, holding only the current items (`model.md` §10, D12).

### item
**Rust** `Item`, `ItemId` · **Python** `Item` · new

One entry in the scratchpad: `{id, from: {frontend?, extension, cursor}, to,
data}`.
- `id`: a session-local integer, never reused.
- `from`: the extension and cursor that added it, and the frontend client
  when a person's input started it.
- `to`: the cursor it is addressed to. It names no extension: extensions on
  that cursor decide among themselves, checking `from` where it matters.
- `data`: any JSON value. Its meaning belongs to the extensions that read it.
  An item `cursor.deliver` can deliver holds `{text, images?}`.

An item is **open** until it is removed. Load drops any item some node
`delivers`.

### pending input
**Python**: none (a base-extension item) · new

An open base-extension item: input a person gave a cursor that the base
extension has neither delivered nor returned. It is pending in the scratchpad
until `pre_step` delivers it, or until an abort returns it.

### delivery
**Python** `cursor.deliver(items)` · new

Writing items' input onto the cursor's line. `cursor.deliver` writes one
`User` node per run of consecutive items from one client (or one per item,
with `combine: false`), each with `delivers` listing its items, moves the
cursor onto the last, as one durable write, and then removes the items. The
node's author is the typing client; an item with no client is authored by
the delivering extension. Allowed only at a boundary. `delivers` is the only
delivery record, so a crash cannot deliver an input twice.

### returned input
**Python**: none (a base-extension item, updated) · new

A pending input the base extension hands back after an abort, so the person
can send it again or edit it. Its `post_step` tells the typing client
(`cursor.tell`); if the client got it, the item is removed; otherwise the
item is updated to `returned` and stays, and every frontend can show it with
send and edit buttons. `pre_step` does not deliver returned items.

---

## Position

### cursor
**Rust** `Cursor`, `CursorId` · **Python** `Cursor` · **retires** head cursor,
acting cursor, `TURN_CURSOR`, `TurnFrame`, `RemoteCursor`

An insertion point: `{id, at}`, plus driver memory: its auto-step flag,
whether it is busy, its `max_steps` count, and its open questions. Every
write a step makes is followed by a move of its cursor onto it.
- `CursorId` is a session-local integer, assigned by the cursor's opening
  move (Q33). A cursor may also have a label.
- Several cursors may share a session, and even a node.
- A cursor carries no settings: those come from the walk (`config_at`).
- A cursor carries no queue. Input for a busy cursor waits in its scratchpad
  (D2).
- Cursors do not close (Q12).

### label
**Rust** `Move::label` · **Python** `Cursor.label`, `Session.cursor(label)` ·
new

An optional name for a cursor, set on its opening move: unique in the
session, and never changed (Q33). Load builds the map from label to cursor.

### busy
**Python** `Cursor.busy` (driver memory) · new

A cursor is busy from the moment it wakes until it comes to rest: through
its `pre_step`, every step it takes, and every `post_step` (see cycle).
Otherwise it is **at rest**. Never stored: every cursor loads at rest.
- Input hooks run whether the cursor is busy or not.
- A frontend's `Session.step` on a busy cursor is refused.
- A frontend's go-to or re-run on a busy cursor is refused while one of its
  hooks is running, and otherwise aborts its step first.

### locked
**Python**: driver memory · new

A cursor with an open question (see question). While locked, a frontend's
input, go-to, step, and re-run on it are refused, not queued. Abort is
allowed. Answering or declining the question unlocks it.

### open_cursor
**Rust** `Session::open_cursor(at, label?, opened_by?)` (internal) · **Python**
`Session.open_cursor(at, label=None)` · **retires** `spawn`, `newest`, cursor
registration

Creates a cursor and appends its opening move. The driver fills in
`opened_by` from the caller, as it fills in `by`: a hook on cursor A that
opens a cursor records A, and so does a tool running for cursor A (a
sub-agent); a frontend leaves it empty. The node may be committed or not, but
its walk must validate; the opening move commits it (Q35). A node on a batch
or inside one of its runs fast-forwards to the end of the run, as a move does
(I3). Opening never takes a claim. **Opening wakes the new cursor**: its
`pre_step` runs, whoever opened it (Q32). A cursor a hook or tool opens
inherits its opener's `max_steps` count and auto-step setting; one a
frontend opens starts at 0, with auto-step off.

### at
**Rust** `Cursor::at` · **Python** `Cursor.at` · **retires** leaf,
`pre_turn_leaf`

The cursor's committed node. Always valid and committed (I3). This is where
the conversation *is*, for that cursor.

### committed
**Python** `Session.is_committed(node_id)` (a view) · new

A node is committed if it lies on `walk(m.to)` for some move record `m`,
opening moves included. Committed is permanent: moving a cursor away does not
uncommit anything (Q2).

### uncommitted
**Python**: a view · **retires** side entry, orphaned suspension, and this
glossary's own earlier word "scratch"

Derived, never stored. Every node that is not committed: no move has landed on
it or passed through it. That covers:
- side nodes an extension appends off a line, or anywhere else;
- detached-root work;
- the nodes a `pre_sample` hook built for a sample that produced nothing;
- `sample_error` records;
- an I6 restatement for a sample that produced nothing (reused next time);
- a copy a hook left unfinished.

An uncommitted node is persisted like any node and stays inspectable. It is
just not on anyone's conversation, until a move reaches it. Apart from an
unreached I6 restatement, which is reused rather than duplicated, core never
writes a node and then abandons it, so an uncommitted node is otherwise an
extension's work or a record of a failure. The word was "scratch" until the
sixth revision; it changed so that it could not be confused with the
scratchpad, which is not part of the tree.

### side node
**Python**: none (an ordinary node) · new

A node a hook appends off the cursor's line: a `Custom` note under a written
node, an alternative, or work on a detached root. It is uncommitted unless a
move later reaches it. Nothing marks it; "side" describes where it sits.

### append
**Rust** `Session::append(parent?, by, body, …)` · **Python**
`Session.append(parent, body, …)` · **retires** `append_at`, `_CursorWriter`, write group

Writes one new node and emits `node_appended`. It checks the node first, so
nothing invalid is written (I5):
- I4: a `Result` must answer a call of the batch on `walk(parent)` that is
  not yet answered on that walk; under an unstarted or partial batch, nothing
  else may be appended.
- V2: a parentless node must be `Config`.

Appending never moves a cursor. A step moves its cursor onto what it appended
straight afterwards. The node's author is filled in by the driver from the
caller (see author); Rust's `by` argument is internal, never taken from user
input.

### move
**Rust** `Session::move_cursor(cursor, to, by)` · **Python**
`Session.move(cursor, to)` · new (0.15 had no single point where a write
became final)

The only way `at` changes. A move does every check first, then writes
(`model.md` §5):
1. resolve `to`: a destination on a batch or inside one of its runs
   **fast-forwards** (a re-run keeps `to`);
2. refuse a move onto a copied batch that is not complete on the walk, a
   re-run with nothing to run, a hook's move outside a boundary or of
   another cursor, and a frontend's move while a hook of the cursor runs or
   a question is open;
3. if the resolved `to == at`, write nothing (a re-run skips this check);
4. apply I6 for a go-to, a hook's move, or a re-run; at an unstarted or
   partial batch the restatement is deferred to the completing result;
5. if the cursor is busy (a frontend's move), abort its step, wait for it to
   end, and resolve `to` again; then append any new restating `Config` and
   the move record together, set `at := to`, and emit `cursor_moved`;
6. a go-to then turns auto-step off and wakes the cursor; a re-run runs its
   tools step.

A move never takes a claim, and is never refused because of one.

A move comes from a step (onto what it wrote, or a fast-forward the step makes
when it re-reads a run), a frontend's go-to, a hook on its own cursor at a
boundary, or a re-run. Rust's `by` argument is internal: no caller supplies
it.

A refused move changes nothing, aborts nothing, and raises to whoever
asked. A hook that catches its own refused move has handled it, and auto-step
is unchanged. A step's own move is never refused and never skipped.

### move record
**Rust** `Move` · **retires** navigate entry

The persisted fact of one move, or of one cursor opening:
`{cursor, from?, to, by, time, opened_by?, label?}`. `from` is absent on an
opening move, and only an opening move carries a `label`. `by` says who
asked: `frontend(client)`, `core` (a step's own move), `extension(name)` (a
hook's move, write, delivery, re-run, or opening), or `tools` (a tool's
opening) (Q28). `opened_by` names the cursor that opened this one: a hook's
cursor, or the cursor a tool runs for, such as a sub-agent's parent (Q6).

### fast-forward
**Python**: none (behavior of `Session.move` and `Session.open_cursor`) · new

What a move or `open_cursor` always does when its destination is a batch's
`Assistant` node or one of its results: it lands at the end of the existing
run instead. It follows only `Result` children answering calls of that same
batch, taking the most recently appended where there are several, and stops
at the result that completes the batch. It never passes onto a later reply.

If the run is partial, the cursor lands at its current end and sits there; it
takes no claim. When it is next stepped, the step takes the claim, re-reads
the run, and continues it (resume). A step's own re-read is a fast-forward
too (`by: core`). This is what keeps a cursor from landing above the end of a
run by accident and running calls a second time (I3). Only a re-run lands
inside a batch on purpose.

### re-run
**Rust** `Session::rerun(cursor, node)` · **Python**
`Session.rerun(cursor, node)` · new

A deliberate second run of a batch: a move that lands exactly on `node` (the
batch's `Assistant` node, or a result partway through a run), followed at once
by a tools step for every call still unanswered on its new line, through
`pre_tool` as usual. The step takes the claim, waiting if another step holds
it. The result is a sibling run, or one that branches partway (keep `r5`, run
`c3` again). Afterwards the cursor continues per its auto-step.
- It skips the `to == at` stop: `rerun(a1)` while `at == a1` runs the batch
  again.
- It is refused when nothing is unanswered on the new line, for example on
  the result that completes the batch.
- Frontends call it directly; a hook may re-run its own cursor at a boundary.
  A frontend's re-run of a busy cursor is a move like a go-to: refused while
  a hook runs, otherwise an abort first. A hook's re-run does not nest: the
  call returns once the move is written, and the tools step is the cycle's
  next step. It counts toward `max_steps`. Tools cannot re-run.

Resume after a crash never re-runs on purpose: a re-run that crashed before
its first result fast-forwards on reload, and the user asks again
(`model.md` §5, §8).

### go to
**Python** `Session.move(cursor, to)` from a frontend · **retires**
`navigate_tree`

A move a frontend asks for, with no new input (Q15). It is how a person
browses, branches, **goes back** (to an ancestor on `walk(at)`, which is not
a branch), or rejects a reply (go back to its parent, then step: the step's
write makes the sibling, and that is the branch).
- It turns auto-step off and wakes the cursor where it lands, so `pre_step`
  runs there. A step follows only if an extension turns auto-step on, for
  example after delivering input. Otherwise the cursor sits where it landed.
- On a busy cursor it is refused while a hook runs or a question is open.
  Otherwise it aborts the step first: the step's records are written and
  moved onto, their hooks run through `post_step`, and then the go-to
  applies over any move those hooks made.
- At most it writes one restating `Config` (I6). To run a batch again, use a
  re-run.

### branch
**Rust/Python**: no type; a verb · **retires** fork (in-session),
`commit_branch`, `BranchPlan`, `branch_summary`, `spawn_branch`

To branch is to move to a node that is neither on `walk(at)` nor below `at`.
The cursor leaves its old line at the LCA, or entirely if there is none. The
old line stays committed. Moving to an ancestor is going back, not a branch;
a branch forms there only when the next write makes a sibling. A re-run is a
branch inside a batch's runs.

### fork
**retires** `ctx.fork`, catalog fork, runtime fork, `multitask_strategy="fork"`,
and eight other senses

A new session whose first nodes copy `walk(n)` from another session, each copy
with `copied_from`. Fork crosses sessions and never means anything inside one.
Nothing before Phase 6 needs it.

---

## Execution

### next
**Rust** `next(walk) -> Next` · **Python** `Session.next(cursor)` (a method;
a function named `next` would shadow a builtin) · **retires** agent loop
position, `LoopPosition`

What a cursor needs done, as a pure function of `walk(at)`. It skips `Config`
and `Custom` nodes from the end and decides on the nearest message-bearing
node:

| nearest message-bearing node | `Next` |
|---|---|
| `User`, or a result completing a batch | `Sample` |
| `Assistant` with `stop == tools` and at least one call, or a result in a partial batch | `RunTools(unanswered ids)` |
| any other `Assistant` (a `stop == tools` reply with no calls included), `System`, or none | `Wait` |

### wake
**Python**: driver behavior · new

What starts a cursor's cycle. A cursor at rest wakes when an item is added to
its scratchpad, when it opens, when its session loads (every cursor, in the
lease holder only), when a go-to lands it, and when a frontend steps it,
re-runs a batch on it, or turns its auto-step on. A wake while a `pre_step`
is already scheduled (see throttle) joins it.

### cycle
**Rust** `Cycle` (the per-cursor state machine, D11) · new

What a cursor does from the moment it wakes until it comes to rest
(`model.md` §7):
1. `pre_step`;
2. evaluate `next`;
3. if `next` is not `Wait`, and auto-step is on or the cycle owes a step
   (`Session.step`, a re-run): take one step, then `post_step`; after an
   aborted step, rest; with auto-step on and `next` not `Wait`, go to 1;
4. if the scratchpad changed since the last `pre_step` started, and the cycle
   was not aborted, go to 1, throttled;
5. rest.

The cursor is busy for the whole cycle.

### throttle
**Python**: driver behavior · new

A `pre_step` that no step separates from the previous one starts at least
250 ms after the previous one started. The cursor is at rest while it waits.
Nothing polls: `pre_step` runs again only when the scratchpad changes, a step
follows, or the cursor is woken.

### boundary
**Python**: none (a property of a hook point) · new

A hook point where none of the step's effects is in flight: `pre_step`,
`post_reply`, `post_result` once its batch is complete on the line, and
`post_step`. Only at a boundary may a hook write under its cursor's `at`
(`cursor.write`, `cursor.deliver`), move its cursor, or re-run on it (Q38).

### input
**Python** `Session.input(cursor, input)`, through a client handle · new
(0.15's submission is retired under step)

What a frontend gives a cursor, busy or at rest. It is refused at once if the
cursor is locked. Otherwise core does nothing with it but run the cursor's
`pre_input` hooks (`model.md` §7), outside the cycle, one input chain at a
time per cursor. One hook handles it (typically by adding an item, which
wakes the cursor), or one refuses it, or every hook passes and it is refused
("no extension handled this input"). A refused input changes nothing, and the
frontend keeps the text. Core has no default input behavior and no queue
(D2): the base extension supplies both. To give input elsewhere, go there
first. Input resets the cursor's `max_steps` count. A frontend's **send** is
input plus auto-step on.

### base extension
**Python** `tau_next` default config · new

The extension that ships in the default config and gives input its everyday
behavior. A config may replace it. Its hooks:
- `pre_input`: adds the input as an item (`from.frontend` = the client,
  `data: {text, images}`) to the cursor's scratchpad, busy or at rest, and
  returns handled.
- `pre_step`: delivers the cursor's pending inputs, in order, when `at` can
  take a `User` node and its `deliver` setting allows (see delivery). At an
  unstarted or partial batch it does nothing (Q17).
- `post_step` on an `aborted` ending: returns the cursor's pending inputs
  (see returned input).

Its settings `deliver` and `combine` are in `Config` (see config).

### step
**Python** `Session.step(cursor)` · **retires** loop turn, agent run, user
turn, submission, turn (all four senses, D1)

One sample or one tools run for one cursor (`model.md` §7). `Session.step`
wakes a cursor at rest and takes one step, whatever its auto-step, and resets
its `max_steps` count; on a busy cursor it is refused. A step's phases run
in order:
1. pre hooks on the effect (`pre_sample`, `pre_tool`);
2. the **effect**: a provider request, or tools;
3. pre hooks on the node (`pre_reply`, `pre_result`);
4. **write** the node and **move** the cursor onto it;
5. post hooks on the written node (`post_reply`, `post_result`);
6. `post_step`, once, with the step's ending.

There are two **step kinds**:
- `sample`: `next` is `Sample`;
- `tools`: `next` is `RunTools`. A tools step writes one result per call as
  each call finishes, and moves onto each one.

Each node a step writes goes under the node it was produced from (a reply
under its context tip, a result under its run), and its own move lands on
it. A step either moves its cursor onto what it wrote; or (refused, or a
sample that failed with no output) leaves `at` where it was and turns
auto-step off; or is aborted, in which case it writes its records, moves
onto them, and runs their hooks.

### ending
**Rust** `Ending` · **Python** `Ending` · new

How a step ended, the subject of `post_step`: `wrote` (with the last node
written), `refused`, `failed` (with the error), or `aborted` (with the last
node written, if any).

### reply
**Python** `Assistant` body, before it is written · **retires**
`AssistantMessage` (as a mutable in-flight object)

What a sample returns, as `pre_sample` (when it answers) and `pre_reply` see
it. A reply becomes an `Assistant` node when it is written. `pre_reply` may
rewrite its parts but not its `stop`, which is a fact about the sample.

### auto-step
**Python** `Cursor.auto_step` (driver memory), set with
`Session.set_auto_step(cursor, on)` · **retires** step mode,
suspension, `api.suspend`, `PausableDeadline`, responder

A per-cursor flag. When it is on, the cycle keeps stepping until `next` is
`Wait`. When it is off, the cursor **pauses** after each step, already moved,
until the frontend resumes it. It goes off on its own when a step ends without
moving its cursor (refused, failed), on abort, on a go-to, on a hook error,
and when the cursor has taken `max_steps` steps without the frontend acting
on it, so neither a failure nor a runaway loop repeats unattended. Every
cursor loads with it off. Turning it on resets the `max_steps` count and
wakes a cursor at rest. To **pause** a cursor is to turn its auto-step off.
A hook may set its own cursor's auto-step.

### resume
Step a paused cursor, or turn its auto-step back on. A cursor may sit at the
end of a partial run (after a reload, an abort, a pause, or a fast-forward).
When it is next stepped, the step follows `model.md` §8:
1. Take the batch's claim. If another step holds it, that step is running the
   batch: wait for it.
2. Re-read the run, and fast-forward onto its end if results lie below `at`
   (a step's own move, `by: core`; not in the step a re-run starts, which runs
   the calls unanswered where it landed).
3. Run unanswered idempotent calls again, like any call: `pre_tool`, the
   tool, `pre_result`, `post_result`.
4. Answer the rest `Interrupted` (D4), which skips `pre_result`; its
   `post_result` hooks run.

Each result is written and moved onto as usual.

### idempotent
**Python** `Tool.idempotent: bool` · new

A tool flag meaning that running the tool twice has the same effect as running
it once. Only idempotent calls run again on resume (D4).

### sample
**Rust**: none (an effect) · **Python** `Provider.sample` · **retires**
completion, stream (as a noun), model call

One provider request over a walk. It produces one `Assistant` node, appended
under that walk's tip, or under I6's restating `Config` when the driver
appended one under the tip. Parts arrive as `stream_delta` events and are never
written token by token. Whatever stops the sample (an end, a handoff, a limit,
an abort, an error), the accumulated parts are written once, provided any
arrived.

### effect
**Python**: none (a step phase) · new

The outside work a step exists for: the provider request in a sample step,
or running a tool in a tools step. An abort cancels effects in flight and
nothing else. A tree write or a move is not an effect.

### context
**retires** `ExtensionContext`/`ctx`, the `context` hook, `context=` parameter

The walk a sample is drawn from, before provider conversion. That is
`walk(at)`, unless a `pre_sample` hook returned a different tip; then it is
`walk(tip)`. "Context window" is the provider's limit on it,
in tokens.

### message
**retires** message entry, `customMessage`, `AgentMessage`

The provider's wire unit. **messages** = the provider's conversion of a
context's message-bearing nodes. Never stored.

### config
**Rust** `Body::Config`, `config_at(n)` · **Python** `Config` (pydantic,
Phase 4) · **retires** config entry, `CONFIG_KEYS`, `TurnFrame`, settings
(`Settings`, `settings.json`), model entry

Everything that decides how a step runs: provider, model, request settings,
tools, and the hooks in force. A `Config` node stores it as a full
**snapshot**. `config_at(n)` is the body of the nearest `Config` node on
`walk(n)`. It is always defined, because every root is a `Config` node.
Partial updates are never folded together.

A **restating** `Config` is one core appends (I6) to keep a cursor's config
when its line changes. If the node already has a restating child with the
same body, core reuses it instead of appending another. Every other `Config`
node is **explicit**, copies included, and it persists until another `Config`
node replaces it (Q26). I6 restates only when no explicit `Config` lies in
the range (LCA, to]: below `LCA(at, to)`, excluding the LCA and including
`to` (the context tip, at a sample), or all of `walk(to)` when there is no
LCA. When `to` is an unstarted or partial batch, the restatement is
**deferred**: the run uses its own walk's config, and the restatement is
written under the result that completes the batch, with the step's move onto
it (Q16). What is owed is read off the move log.

Fields named so far:
- **`max_steps`** (Q20): the most steps a cursor takes without the frontend
  acting on it. The driver counts in memory, per cursor, and only actions
  `by: frontend(…)` reset the count: giving input, stepping the cursor,
  re-running a batch, turning auto-step on. A cursor a hook or tool opens
  inherits its opener's count; one a frontend opens starts at 0. When the
  count reaches `config_at(at).max_steps`, auto-step turns off and the cursor
  pauses. `next` does not look at it, and nothing is stored.
- **`deliver`**, the base extension's: `after_batch` (the default) delivers
  pending inputs at any `pre_step` where `at` can take a `User` node, so
  after a batch completes and before the next sample; `after_stop` delivers
  only at a `pre_step` where `next` is `Wait`. Never mid-batch.
- **`combine`**, the base extension's: `true` (the default) delivers each run
  of consecutive pending inputs from one client in one `User` node, texts
  joined by a blank line; `false` writes one `User` node per input, in
  order.
- **`partial_calls`** (Q27): `text` (the default) or `omit`. It sets what a
  call that was never run (its reply's `stop` was not `tools`, whether the
  call is complete or truncated) becomes when the walk is sent again:
  `text` renders it after the reply in τ's `[τ: …]` markup, so the model sees
  what it was writing, and `omit` drops it.

### validate
**Rust** `validate(walk)`

The structural checks (I5). `append` checks each new node (V1: I4;
V2: a parentless node is `Config`), so every walk is valid by construction.
A move checks where it lands (I3, I6). `validate(walk)` re-checks a whole walk,
for tests. Provider legality is checked separately, by the provider's
conversion (Q11).

### hook
**Python** `async def hook(cursor, subject)`, at every hook point ·
**retires** the 0.15 hooks (`input`, `before_agent_start`, `tool_call`,
`tool_result`, `turn_end`, `user_turn_end`, `annotate`, `entry_written`, …),
handler policy, suspension site, chain

A Python function an extension registers at one **hook point**. Every hook
has one shape, `hook(cursor, subject) → outcome`: `cursor` is the cursor
handle, bound to the hook's extension, and `subject` is what the hook point
is about (the input, the context tip, the reply, the call, the call and its
outcome, the written node, or the step's ending). At each hook point, hooks
run in the order their extensions are listed in the config, and each
receives what the previous one returned.

What a hook returns steers the hook point:
- An **input hook** (`pre_input`) handles, passes, or refuses the input.
- A **pre hook on an effect** (`pre_sample`, `pre_tool`) may rewrite the
  request, or **answer** it: the effect and the remaining hooks at that point
  are skipped, but the node's pre hooks still run (Q18).
- A **pre hook on a node** (`pre_reply`, `pre_result`) sees a node before it
  is written, and may rewrite it. `pre_reply` may also refuse it.
- A **post hook** (`post_reply`, `post_result`) sees the node after it is
  written and the cursor has moved onto it.
- **`pre_step`** and **`post_step`** take in what has arrived and react to
  how a step ended.

A hook acts only on its own cursor, and on its cursor's line only at a
boundary (see cursor handle). The tree's own rules decide the result:
append and move checks apply. A hook's move applies at once; later hooks at
the same point see the cursor where it now is; two hooks that both move make
two real moves, in order. When the hook point finishes, the cycle carries on
from wherever the cursor is.

Nodes and moves written by hooks trigger no hooks, but a step a hook causes
(after a "continue", or a re-run's tools step) runs hooks as usual, and a
cursor a hook opens or adds an item to wakes and runs its own cycle.

Post hooks run for every node a step writes and moves onto, aborted steps
included. Results core writes (`Err("invalid arguments")`, `Interrupted`,
`Err("hook error")`) skip `pre_result`, since there is no tool output to
rewrite, but their `post_result` hooks run.

In a tools step, each call's `pre_result` runs as soon as its tool finishes,
concurrently. Only write, move, and `post_result` are serialised per cursor,
in completion order.

An uncaught exception from a hook ends its hook point, turns auto-step off,
and is shown to the frontend. If the effect has not run, it does not. A call
whose hook raised is answered `Err("hook error")` by core, with any raw
output withheld (`model.md` §7). An input hook's error refuses the input.

Abort never cancels a hook (see abort).

### hook point
**Python** the registration name · new

One of nine:

| hook point | runs | subject | outcome |
|---|---|---|---|
| `pre_input` | on `Session.input`, busy or at rest, outside the cycle | the input | **handled**, **pass** (the input, possibly rewritten), or **refuse** (a reason) |
| `pre_step` | when the cursor wakes, before every step, and after `post_step` when the scratchpad changed | nothing | nothing |
| `pre_sample` | before a sample | the context tip | a context tip (the same, or a node it appended: a reminder, a `Config`, a compacted line), or a reply (answered) |
| `pre_reply` | before a reply is written | the reply | the reply, its parts possibly rewritten (never its `stop`), or nothing (refused, nothing written) |
| `post_reply` | after a sample step's move | the written `Assistant` node | nothing |
| `pre_tool` | before each call runs | one call | the call (possibly rewritten), or an outcome (answered) |
| `pre_result` | as each call finishes | the call as it ran, and its outcome | an outcome of the same kind, with its content possibly rewritten (Q18). It cannot drop one: every call is owed a result (I4) |
| `post_result` | after each result's move | the written result node | nothing |
| `post_step` | after every step, aborted ones included | the ending | nothing |

Every hook also receives the cursor handle.

### pre_step
**Python** hook point name · new

The hook point where an extension takes in what has arrived. It runs when a
cursor wakes, before every step, and after `post_step` if the scratchpad
changed since the last `pre_step` started (unless the step was aborted),
throttled (see throttle). It is a boundary, and one of the two hook points
where an extension may read its cursor's scratchpad. The base extension
delivers pending inputs here. It is not a step and does not count toward
`max_steps`.

### post_step
**Python** hook point name · new

The hook point that runs once after every step, with its ending: `wrote`,
`refused`, `failed`, or `aborted`. It is a boundary, and one of the two hook
points where an extension may read its cursor's scratchpad. After an aborted
step it is the last hook of the cycle: the base extension returns pending
inputs here.

### refuse
Declining what a hook point is about:
- an input hook returns **refuse** with a reason, or every input hook passes
  ("no extension handled this input"). Nothing is written, nothing moves,
  and `Session.input` raises the refusal to the frontend, which keeps the
  text;
- `pre_reply` returns nothing. The step ends: the remaining hooks do not run
  except `post_step`, nothing is written, the cursor does not move, and
  auto-step goes off.

`pre_tool` cannot refuse; it answers `Skipped` instead.

### handled
An input hook's outcome saying the input is dealt with, which ends the chain.
It carries no body: what the input causes on the line happens later, in
`pre_step`, typically because the hook added an item.

### pass
An input hook's outcome handing the input, possibly rewritten, to the next
input hook. If every hook passes, the input is refused.

### answer
A pre hook returning the effect's result itself, such as a cached reply or a
mocked or blocked outcome, so that the effect does not run.

### question
**Python** `await cursor.ask(target, question)`, `Session.answer`,
`Session.decline` · **retires** `request_user_action`, suspension prompt

A hook asking a person something and waiting for **an answer** or
**declined**. `target` is one client or any client. While a question is
open, its cursor is locked. Abort does not close it. Core does not look at
the question or the answer: both belong to the extension. A question for one
client is declined if that client detaches (Q37); a question for any client
is replayed to every client that connects. Questions are driver memory: a
crash loses them, and resume answers the waiting call `Interrupted` (Q22).

### claim
**Python**: driver memory · new

The driver's in-memory mark that a step is running a batch's calls: a tools
step, a resume, or a re-run's tools step. It is held only while that step
runs. The step takes it before running any call, then re-reads the run. A
step that finds it held waits (the holder is by definition running), then
re-reads and fast-forwards instead of running the calls again, so tools run
once (Q13). A move or `open_cursor` never takes a claim and is never refused
because of one. An aborted step releases its claim once its `Interrupted`
results are written. The lease keeps claims within one process.

### event
**Rust** `Event` · **Python** `Event` · **retires** `AgentEvent`, `WireEvent`,
serve event, RPC event, bus channel, hook event dict

One type, tagged with a cursor, in seven kinds: `node_appended`,
`cursor_moved`, `stream_delta`, `item_changed`, `question`, `status`,
`notice` (`model.md` §9). Observers cannot block or change anything. Every
wire format is a projection of this one type. Events go to whoever is
subscribed, possibly nobody; a `notice` goes only to its client. No cursor
waits on a particular client, except for a question asked of one.

### abort
**Python** `Session.abort(cursor)` · **retires** `AbortSignal`, `AbortReason`,
abort-tree, `rollback`

Esc. It turns the cursor's auto-step off and cancels the effects of its step
that are in flight: the sample stream, or the running tools. **It never
cancels a hook**, and no new effect starts in that step. The step then writes
its records and moves onto them as usual, and their hooks run: an aborted
sample that had produced output is written with `stop: interrupted` (after
`pre_reply`), its calls owe no result, and the cursor moves onto it; one that
had produced nothing writes nothing. Aborted tools keep the results already
written, answer each running or unstarted call `Interrupted`, moving onto
each, which I4 requires, and run `post_result` for each. A call whose
`pre_tool` or `pre_result` is running finishes normally (Q34). Then
`post_step` runs with ending `aborted`, and the cursor rests. On a cursor at
rest, abort only turns auto-step off. A frontend's go-to or re-run of a busy
cursor is an abort, then the move (`model.md` §8, Q4).

---

## Effects and edges

### provider
**Python** `Provider` · **retires** backend (config key), `ProviderSpec`,
`api` (as a selector), client pool

The Python object that turns a context into a sample, for one API family
(OpenAI-compatible first). It also converts walks to messages, and that
conversion is where provider-specific legality lives (Q11). What every
provider owes the driver (stop mapping, faithful replay, reasoning rules,
cache reporting) is listed in `model.md` §11. In 0.15 this word had four
meanings; here it means only this adapter.

### model
**Python** `Config.model` · **retires** `Model` (pydantic endpoint
descriptor), model entry, `model_spec`

The name of the model a provider samples from, spelled as the provider's API
spells it. Behavior switches belong to the provider and its typed settings,
not to the model.

### tool
**Python** `Tool` · **retires** `AgentTool`, `ToolDefinition` (×2),
`ExtensionToolDefinition`, `ToolInfo`, `ToolDescription`

One type for every tool, built-in or extension. A tool has:
- a name;
- an argument schema;
- an `idempotent` flag;
- an `exclusive` flag: it runs alone, never concurrently with the batch's
  other calls (Q21);
- an async function from arguments to an outcome. It may also receive a
  handle bound to the calling cursor (see cursor handle): whatever it appends
  is authored `tools`, and a cursor it opens has `opened_by` = the calling
  cursor. It acts on no cursor's line, so it cannot touch the step running
  it (Q32).

### extension
**retires** bucket, `ExtensionHandlers`, `ExtensionRunner`, `ExtensionState`,
`ExtensionAPI` (as a 42-method surface)

A Python module that registers hooks and tools through the public API.
Extensions have no special standing in core (D3, D8). The base extension
ships in the default config and handles input. Extensions on one cursor, or
on different cursors, work together through the scratchpad. The first three
built in Phase 5 prove the model:
- **compaction**: summarises an old prefix and branches onto a shorter line
  made of the summary (a `User` node by the extension, Q30) and copies of the
  recent nodes, `Config` nodes included.
- **prune**: after the provider's cache expires, rewrites old tool outputs and
  thinking as derived nodes, then branches onto the rewrite.
- **sub-agent**: a tool that, through its handle, opens a cursor
  (`opened_by` = the calling cursor) on a line outside the parent's batch,
  for example a detached root, adds the task as an item to its scratchpad,
  waits until it rests at `next == Wait`, and answers the call with a value
  whose `derived_from` lists the cursor's final node. Details are Phase 5.

### cursor handle
**Python** the first argument to every hook; a narrower one is handed to a
tool · new (the fourth revision called it `api`)

The cursor a hook runs for, bound to the hook's extension, so everything it
writes is authored `extension(name)` and every move it makes is
`by: extension(name)` without the hook saying so. Through it a hook may, at
any hook point:
- read: `cursor.at`, `cursor.next`, `cursor.busy`, `cursor.auto_step`, and
  the session (`cursor.session`), including other cursors' positions;
- append side nodes; add, update, or remove items in any cursor's
  scratchpad; ask (`cursor.ask`), show (`cursor.show`), and tell
  (`cursor.tell`); set its own cursor's auto-step; open cursors; sample a
  walk and run a tool outside any step (compaction's cold summary on a
  detached root).

At a boundary only, it may also act on its own cursor's line:
`cursor.write(body, …)`, which appends under `at` and moves onto it as one
durable write; `cursor.deliver(items)`; and move or re-run its own cursor. It
never acts on another cursor's line. It reads its own cursor's scratchpad
only in `pre_step` and `post_step`.

A tool's handle is bound to the calling cursor instead, and is narrower: it
may read, append, sample a walk, run a tool, open cursors, write
scratchpads, and wait on a cursor it opened, but it acts on no cursor's line
(Q32). The exact surface is a Phase 4 decision. It is not the 0.15
`ExtensionAPI`.

### frontend
**retires** head, backend (as `Backend`/`TauBackend`/`RemoteBackend`), head
label

A program that drives sessions and shows events: `tau serve` first, then
print/json, REPL, RPC, TUI. A frontend serves one or more clients.
- A frontend keeps no queue. It hands input to `Session.input` at once, busy
  or not, and keeps the text only if the input is refused (D2). Queueing is
  the base extension's.
- A frontend may give input (`Session.input`), step a cursor
  (`Session.step`), go to any node (`Session.move`), re-run a batch
  (`Session.rerun`), set auto-step (`Session.set_auto_step`), abort
  (`Session.abort`), answer or decline a question, and add, update, or remove
  scratchpad items (Q15). Giving input, stepping, re-running, and turning
  auto-step on reset the cursor's `max_steps` count.
- Every frontend sees every cursor and every scratchpad. No cursor waits on a
  particular client: a client may switch cursors or sessions, or detach, and
  every active cursor keeps operating.

### client
**Python** the client handle a frontend calls through · new

One connection to a frontend: a terminal, or each connection to `tau serve`.
Every client has an id, and everything it does is `by: frontend(client)`. The
id is recorded, never used for control flow, except that a question, a
notice, or a returned input may be addressed to one client.

### status
**Python** `cursor.show(state)` · new

The state an extension shows every frontend for its cursor, such as "2
inputs returned". Each call replaces the extension's previous state. Driver
memory: a reload clears it, and `tau serve` replays the current state to each
client that connects. Emitted as a `status` event.

### notice
**Python** `cursor.tell(client, message) → bool` · new

A message from an extension to one client, such as "put this text back in
your input box". It returns whether the client was attached to receive it.
Emitted as a `notice` event to that client only.

### driver
**Python** `tau_next.driver`

The Python code that performs what core's cycles ask for (D11). It runs hook
points, provider samples, and tools, awaits them, cancels what core says to
cancel, passes records to the store, sleeps for the throttle, and feeds every
result back to core with the time. It decides nothing: hook order, when
`pre_step` runs, boundaries, aborts, fast-forward, the claim, and `max_steps`
are core's. It is the only place that awaits.

### job
**Rust** `Job` · new

One thing a cycle asks the driver to do: run a hook point, sample, run a set
of calls, persist a record, wait (for a claim, or the throttle), or rest. The
driver performs it and feeds back its result, or each call's completion in
any order. A job never decides anything (D11).

### store
**Rust** `tau_store` · **retires** session store, `SessionCatalog`, JMFTS
session log (until Phase 7)

The only Rust code that touches the filesystem. It keeps, per session, one
append-only record log and one scratchpad file, behind a trait (D12).

### record
**Rust** `tau_store::Record` · new

One durable write in a session's record log: one or more nodes and at most
one move, length-prefixed and checksummed. A step's write and its move are
one record, so a crash leaves both or neither. A torn final record is ignored
on load (I7).

### lease
**Rust** `tau_store` · **retires** `SessionWriteLock`, `WriteAccess`,
`LockHolder`, lock sidecar

One `flock` per session, taken on its record log. The holder may write, and
only the holder runs cycles and hooks (the wakes on load included); every
other process opens the session read-only and never runs a hook. A lease
whose holder process is dead is reclaimed.

---

## Testing (Phase 2)

### Harness
**Python** `tests/behavior/harness.py` · **retires** session log contract
tests

The protocol every behavior test drives, shaped like `tau serve`: a test
attaches one or more clients to a session and drives it through them. It
mirrors the frontend operations: create (`Session.create`) and load
sessions, give input (`Session.input`), step (`Session.step`), set auto-step
(`Session.set_auto_step`), abort (`Session.abort`), open cursors
(`Session.open_cursor`), go to a node (`Session.move`), re-run a batch
(`Session.rerun`), answer or decline questions, read and change scratchpad
items, snapshot the tree, cursors and scratchpad, and read events, which are
the only feed. It has one adapter for `tau_next` (in `tests/`) and one for
τ 0.15 (in `parity/`).

### FakeProvider, FakeTool
**Python** `tests/behavior/` · **retires** scripted fake model streams,
`unittest.mock` patches

These fakes sit at the effect boundary, and they are the only fakes allowed
(rule 4).
- `FakeProvider` replays scripted samples, including calls, errors, and
  stream deltas. It records each context it was sent (the I1 test).
- `FakeTool` returns scripted outcomes.

---

## Scaffold (deleted in Phase 3)

### probe
**Rust** `tau_core::scaffold::Probe` · **Python** `tau_next.Probe`,
`ProbeError`, `echo`

Phase 0's hello-world struct for testing the Rust/Python boundary. It is not
part of τ.

---

## Retired 0.15 terms

Banned terms (rule 3) are linted. The rest disappear because nothing here
needs them.

| 0.15 term | what replaces it |
|---|---|
| entry | node |
| side entry, `SIDE_KINDS` | uncommitted: a node not on a committed walk |
| incomplete / finalize | nothing: nodes are written once, complete |
| leaf, default leaf | `at`, persisted through move records; no guessing |
| head cursor, acting cursor, `TURN_CURSOR` | every cursor is explicit, and hooks receive theirs |
| turn frame, `TurnFrame` | the `Config` node on the walk |
| turn (four senses) | step (D1); frontends count steps since last input |
| submission, multitask strategy | input hooks; queueing is the base extension's pending inputs in the scratchpad (D2) |
| suspension, responder, `api.suspend` | auto-step off: the cursor pauses between steps. A hook that needs a person asks a question |
| lane | nothing |
| head | frontend |
| backend | provider (model API) or frontend (driver of a session) |
| fork (in-session) | branch |
| compaction entry, splice | the compaction extension: a summary node plus a branch (D3) |
| extension lock, `request_user_action` | a `pre_input` hook that refuses input until satisfied, or a question |
| `input` hook | `pre_input`, plus `pre_step` for what the input causes |
| `before_agent_start` hook | `pre_sample` (appends what it adds, I1) |
| `tool_call` hook | `pre_tool` |
| `tool_result` hook | `pre_result` |
| `turn_end`, `user_turn_end` hooks | `post_reply` (Q20), or `post_step` |
| `annotate`, `entry_written` hooks | event observers (`node_appended`) |
| session write lock | lease |
| `AgentSession`, runtime, catalog, `SessionManager` | session |
| `ExtensionContext` / `ctx` | the cursor handle, every hook's first argument |
| handler policy (gating/advisory/human) | nothing in core (Q9) |

Terms this glossary retired in its own earlier revisions: `post_open` (now a
wake and `pre_step`), `post_input` and the input step (input causes writes
only in `pre_step`), interrupt (now abort, and a frontend's move of a busy
cursor), and scratch (now uncommitted).
