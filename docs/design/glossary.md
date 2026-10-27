# Glossary

Status: **for review (Phase 1), seventh revision.** On sign-off this file and `model.md`
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
and no second write that completes one. A node is on the tree once it is
written.

### body
**Rust** `Body` · **Python** `Node.body`, one class per kind ·
**retires** entry type, `customType`

What a node holds. A body is exactly one of six kinds:

| kind | holds | reaches the model? |
|---|---|---|
| `System` | the system prompt text | yes |
| `User` | text and images from a person, or written on their behalf | yes |
| `Assistant` | parts (text, thinking, calls) in the order they streamed, and a stop reason | yes |
| `Results` | one outcome per call of a batch, in call order | yes, as one tool message per call |
| `Config` | a full snapshot of the config in force | no |
| `Custom` | `{kind, data}`, owned by an extension or core | no (Q3) |

The first four kinds are **message-bearing**. `Config` and `Custom` are
bookkeeping, and the provider never sees them.

Known `Custom` kinds are listed here as they are introduced:
- `sample_error`: written by core (`by: core`) under `at`, without a move. It
  records a sample that failed before any output arrived (`model.md` §8).

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
runnable, `next` is `Wait`, and the provider renders them as text, per
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
  parses. A call whose `args_raw` doesn't parse gets
  `Err("invalid arguments")` without running.

A call is never a node of its own, because one sample produced all of a
reply's calls at once.

### batch
**Python**: none (a view) · new

The calls of one `Assistant` node with `stop == tools` and at least one call.
A `stop == tools` reply with no calls is an ordinary reply, not a batch:
`next` is `Wait`. A batch is answered by one `Results` node, written by the
tools phase at the join. There are no batch states: `next` reads only the
walk, so a cursor at a batch's `Assistant` node runs it whatever lies below.

### Results
**Rust** `Body::Results` · **Python** `Results` (body class) · **retires**
tool result, toolResult message, `Result` (one node per call, this
glossary's own earlier revisions)

A node answering a batch: `{outcomes: [{call, outcome}]}`, one outcome per
call, in call order. The tools phase writes it at the join, under the batch's
`Assistant` node, delivering the batch's journal items, and moves the cursor
onto it. Every call of the batch has exactly one outcome (I4), even when it
did not run or its output was redacted. A written `Results` node is final;
another answer is a sibling `Results` (Q10).

A hook may write a `Results` node of its own anywhere; core checks only that
its outcomes name distinct calls (V2). When the walk is sent, the provider
expands a `Results` node under its batch into one tool message per call, and
makes one anywhere else legal (for example as a `User` message).

### outcome
**Rust** `Outcome` · **Python** `Outcome` · **retires** `is_error`,
`BlockedCall`, `Skipped` (this glossary's own earlier revisions)

What a `Results` node says for one call:

| outcome | meaning |
|---|---|
| `Ok(value)` | the tool ran and returned a value, or a `pre_tool` hook set one (cache, mock) |
| `Err(error)` | the tool ran and failed, or could not run (unknown tool; `Err("invalid arguments")`), or a tool hook returned a string (the string is the error, in the extension's words), or a hook raised: `Err("hook error")`, with any raw output withheld |
| `Interrupted` | an abort stopped the call or kept it from starting, or a crash came before it finished and its tool is not idempotent. The tool may or may not have run (D4) |

### author
**Rust** `Author` · **Python** `Node.by` · **retires** `source:"agent"`,
submitter, `SubmissionSource`

Who wrote a node:
- `frontend(client)`: a person's input, named by the client that sent it. A
  delivered input is its typing client's, because its content is that
  person's text.
- `provider`: a sample.
- `tools`: a `Results` node from running calls, and anything a tool appends.
- `core`: the session's first root and its `System` node (`Session.create`),
  a `sample_error` record, and every outcome core writes itself:
  `Interrupted`, `Err("invalid arguments")`, `Err("hook error")`.
- `extension(name)`: a hook, including a root a hook creates (a detached
  root), and a node a hook rewrote, answered, or wrote itself.

A node's author is whoever produced its content as written. A hook's answer or
rewrite makes that hook's extension the author, and so does a copy. The author
is recorded and never used for control flow (Q7). No API caller supplies it:
the driver fills it in from the caller, because hooks get a handle bound to
their extension and frontends call through a client handle. A move record's
`by` works the same way.

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
therefore its own walk.
- Compaction's copied tail lists one node per copy, `Config` nodes included.
- The store keeps an in-memory **reverse index**, built at load and never
  stored, so `Session.copies_of(n)` is one lookup.
- A copied batch is followed by a copy of its `Results`. A cursor that goes to
  a copied `Assistant` node with calls and steps runs them, like any batch: a
  frontend that does not want that goes to the copied `Results`.

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
holds ids, never content. Core sets it on a delivered input's `User` node and
on a `Results` node (its journal items); a hook's `cursor.write` may set it.
It is the only record that an item was consumed: load drops any item a node
delivers (I7), so a crash between the delivering record and the item's
removal neither loses nor repeats it, and the content is stored once, in the
node.

### session
**Rust** `Session` · **Python** `Session` · **retires** `AgentSession`,
`SessionLog`, file `Session`, `SessionManager`, `AgentSessionRuntime`,
catalog, `ReplicaSession`

One conversation: a forest of nodes, its cursors and their labels, and its
scratchpad. A session is persisted as its record log (nodes, move records,
label records) and its scratchpad file, and nothing else (I7).

**`Session.create(config, system)`** appends the root `Config` and a `System`
node under it (both `by: core`), and opens the first cursor at the `System`
node (Q5). In 0.15 this word had ten meanings; here it has one.

### tree
**Python**: none (a view) · new

A root and all its descendants. A session is a forest of one or more trees.

### root
**Python**: none (a view) · **retires** session header entry

A node with no parent. Every root is a `Config` node (V1), so every walk
starts with the config in force (Q5). A session's first node is its first
root, written by `Session.create` (`by: core`). A root a hook creates is
`by: extension(name)`.

### detached root
**Python**: none (a view) · **retires** side-completion sessions,
`_EphemeralConversationSession`

Any root after the first. It holds one-off work done outside the
conversation, such as a cold summary from a cheap model, or a sub-agent's
line. Its `Config` records which model did the work. A cursor may open on or
go to any of its nodes.

### side node
**Python**: none (an ordinary node) · new

A node that no cursor's walk passes through: a `Custom` note a hook appended,
an alternative, a `sample_error` record, a reply someone went back from, work
on a detached root. Nothing marks it; "side" describes where it sits, and a
later move may put it on a cursor's walk.

### walk
**Rust** `Session::walk(id)` · **Python** `Session.walk(node_id)` ·
**retires** fold, path, `Cursor.context()`, compaction splice

`walk(n)` is the nodes root → … → `n`, following `parent`, inclusive. Nothing
else: no splicing, no filtering, no rendering. Every view the model gets is a
walk (I1).

### tip
The last node of a walk: `walk(n)`'s tip is `n`.

### line
A walk, seen as one path through the tree. To branch is to leave one line for
another.

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
added. It is where input waits, where a batch's finished outcomes wait for
the join (the journal), and how one cursor leaves word for another.
- **Anyone may add, update, or remove any item**: any hook, through its
  handle, on any cursor's scratchpad; a tool, through its handle; a frontend,
  through its client handle.
- **An extension reads its own cursor's scratchpad only in `pre_step` and
  `post_step`.** Frontends read any scratchpad at any time.
- **Adding an item wakes the cursor it is addressed to** (see wake).
- Nothing in it reaches the model (I1). To act on an item, an extension
  writes a node.
- It is stored as its own file, rewritten whole and atomically on each
  change, holding only the current items (`model.md` §11, D12).

### item
**Rust** `Item`, `ItemId` · **Python** `Item` · new

One element of the scratchpad: `{id, from: {frontend?, extension?, cursor},
to, data}`.
- `id`: a session-local integer, never reused.
- `from`: the cursor that added it, and who added it. What `from` names
  decides the item's kind:
  - a `frontend` client: **input**, `data: {text, images}`;
  - an `extension`: the extensions', `data` any JSON value;
  - neither: a **journal item**, which core writes,
    `data: {batch, call, outcome}`.
- `to`: the cursor it is addressed to. It names no extension: extensions on
  that cursor decide among themselves, checking `from` where it matters.

An item is **open** until it is removed. Load drops any item some node
`delivers`.

### input
**Python** `Session.input(cursor, input)`, through a client handle · new
(0.15's submission is retired under step)

What a frontend gives a cursor, busy or at rest, and the one kind of item
core knows. `Session.input` is refused at once if the cursor is locked.
Otherwise core adds an input item
`{from: {frontend: client, cursor}, to: cursor, data: {text, images}}`, which
wakes the cursor if it is at rest. Core has no queue in frontends or cursors
(D2): input waits in the scratchpad until the next input phase. Input resets
the cursor's `max_steps` count. A frontend's **send** is input plus auto-step
on. To give input elsewhere, go there first.

### pending input
**Python** `Session.pending_input(cursor)` (a view) · new

A cursor's input items, in the order they were added: input given to it and
not yet delivered or removed. Every frontend shows it and may edit or remove
it like any item. After an abort it stays pending (Q42).

### delivery
**Python**: core behavior, in the input phase · new

Writing an input onto the cursor's line. For each pending input item, oldest
first, the input phase runs `pre_input` on its input draft, then commits a
`User` node under `at` with the draft's content, `delivers: [item]`, authored
`frontend(client)`, and moves onto it, as one record. The item is removed
after. Two inputs from two clients are two `User` nodes in a row. `delivers`
is the only delivery record, so a crash cannot deliver an input twice.

### journal
**Python**: none (core behavior) · new

The cursor's journal items for a batch: each call's outcome, kept in the
scratchpad as soon as its `pre_result` hooks finish, until the join commits
the `Results` node that delivers them. A crash before the join loses no
finished call: the next tools phase at that batch on that cursor uses the
journal's outcomes and runs only the rest (Q41, D4). The journal is per
cursor, so two cursors at one batch each run it (Q13). Journal items a step
wrote and delivered do not count as a scratchpad change for re-stepping.

### journal item
**Rust** `Item` (with neither `frontend` nor `extension` in `from`) · new

One call's outcome in the journal: `{from: {cursor}, to: cursor, data:
{batch, call, outcome}}`. Core writes it, and a call counts as finished once
it is durable. A frontend may remove one, for a clean re-run.

---

## Position

### cursor
**Rust** `Cursor`, `CursorId` · **Python** `Cursor` · **retires** head cursor,
acting cursor, `TURN_CURSOR`, `TurnFrame`, `RemoteCursor`

An insertion point: `{id, at}`. Its only flag is auto-step; busy and locked
are read off its machine, and its `max_steps` count and open questions are
driver memory. Every commit is followed by a move of its cursor onto it.
- `CursorId` is a session-local integer, assigned by the cursor's opening
  move (Q33). A cursor may have any number of labels.
- Several cursors may share a session, and even a node.
- A cursor carries no settings: those come from the walk (`config_at`).
- A cursor carries no queue. Input for a busy cursor waits in its scratchpad
  (D2).
- Cursors do not close (Q12).

### label
**Rust** `Label` (a record) · **Python** `Cursor.labels`,
`Session.cursor(label)`, `cursor.set_label(label)`,
`cursor.remove_label(label)` · new

A name for a cursor. Labels map unique strings to cursor ids: each label
names one cursor, and a cursor may have any number of labels
(`"research-agent-3ca7"` and `"Web Search Agent (Rust Patterns)"` can both
name cursor 4). There is no label naming several cursors. Setting a label
another cursor holds moves it. Labels are mutable, and stored as label
records (Q33).

### label record
**Rust** `Label` · new

The persisted fact of one label change: `{label, cursor?}`. With a cursor, it
sets the label to that cursor; without one, it removes the label. Load
replays them in order. Emitted as `label_changed`.

### busy
**Python** `Cursor.busy` (read off the machine) · new

A cursor is busy from the moment a step starts until it rests. Otherwise it
is **at rest**. Never stored or set: every cursor loads at rest.
- A frontend's `Session.step` on a busy cursor is refused.
- A frontend's go-to on a busy cursor is refused while one of its hooks is
  running or a question is open, and otherwise aborts its step first.
- Input to a busy cursor waits as an item for the next step's input phase.

### locked
**Python**: read off the machine · new

A cursor with an open question (see question). While locked, a frontend's
input, go-to, and step on it are refused, not queued. Abort is allowed.
Answering or declining the question unlocks it.

### open_cursor
**Rust** `Session::open_cursor(at, opened_by?)` (internal) · **Python**
`Session.open_cursor(at)` · **retires** `spawn`, `newest`, cursor
registration

Creates a cursor and appends its opening move. `at` may be any node. The
driver fills in `opened_by` from the caller, as it fills in `by`: a hook on
cursor A that opens a cursor records A, and so does a tool running for
cursor A (a sub-agent); a frontend leaves it empty. **Opening wakes the new
cursor** (Q32). A cursor a hook or tool opens inherits its opener's
`max_steps` count and auto-step setting; one a frontend opens starts at 0,
with auto-step off. Labels are set afterwards, by label records.

### at
**Rust** `Cursor::at` · **Python** `Cursor.at` · **retires** leaf,
`pre_turn_leaf`

The node the cursor is on: the `to` of its latest move. Always a node of its
session (I3). This is where the conversation *is*, for that cursor.

### append
**Rust** `Session::append(parent?, by, body, …)` · **Python**
`Session.append(parent, body, …)` · **retires** `append_at`, `_CursorWriter`, write group

Writes one new node and emits `node_appended`. It checks only the type
(I5): V1, a parentless node is `Config`; V2, a `Results` node's outcomes name
distinct calls. Every other transition is legal in the tree. Appending never
moves a cursor. The node's author is filled in by the driver from the caller
(see author); Rust's `by` argument is internal.

### commit
**Rust**: a phase of the step machine · **Python**: none · **retires** the
plan's `commit` (a move with checks, drafts and LCA)

The phase step that writes the current draft under `at`, as it stands at that
moment, and the move onto it, as one record (`by: core`). Input commits one
`User` node per item, each under the previous; a sample commits the reply;
the tools phase commits the `Results` node at the join. A hook's
`cursor.write` is the same write, authored by the hook's extension.

### move
**Rust** `Session::move_cursor(cursor, to, by)` · **Python**
`Session.move(cursor, to)`, `cursor.move(to)` · new (0.15 had no single
point where a write became final)

The only way `at` changes (`model.md` §5). A move checks only that `to` is a
node of the session (I3), and never looks at what is below `to`. It comes
from a commit (`by: core`, or the extension for `cursor.write`), a frontend's
go-to, or a cursor hook on its own cursor. A move whose `to` equals `at`
writes nothing. A refused move changes nothing and raises to whoever asked.

A hook's move applies at once; later hooks at the same point see the cursor
where it now is. A frontend's move of a busy cursor is refused while a hook
runs or a question is open, and is otherwise an abort, then the move.

### move record
**Rust** `Move` · **retires** navigate entry

The persisted fact of one move, or of one cursor opening:
`{cursor, from?, to, by, time, opened_by?}`. `from` is absent on an opening
move. `by` says who asked: `frontend(client)`, `core` (a commit's move),
`extension(name)` (a hook's move, write, or opening), or `tools` (a tool's
opening) (Q28). `opened_by` names the cursor that opened this one. Hooks read
them with `cursor.moves()`.

### cursor.moves
**Python** `cursor.moves(since_step=False)` · new

A cursor's move records, oldest first. With `since_step=True`, only the moves
since the current step's `pre_step` began: the first record's `from` is where
the step started, and each `by` says who moved it (Q44). There is no `prev`
field.

### go to
**Python** `Session.move(cursor, to)` from a frontend · **retires**
`navigate_tree`, `Session.rerun` (this glossary's own earlier revisions)

A move a frontend asks for, with no new input (Q15). It is how a person
browses, branches, **goes back** (to an ancestor on `walk(at)`, which is not
a branch), or rejects a reply (go back to its parent, then step: the commit
makes the sibling, and that is the branch).
- It turns auto-step off and wakes the cursor where it lands. With auto-step
  off, that step runs `pre_step` and `post_step` and no phase (Q39), unless
  an extension's `pre_step` turns auto-step on.
- On a busy cursor it is refused while a hook runs or a question is open.
  Otherwise it aborts the step first: the draft is committed, its post hooks
  and `post_step` run, and then the go-to applies over any move those hooks
  made.
- The cursor's config is then `config_at(to)`, whatever that line chose.

### re-run
**Python**: none (go to, then step) · new

Running a batch again: go to its `Assistant` node and step. The cursor never
looks at children, so the tools phase runs every call and writes a sibling
`Results`. It needs no method. Journal items left by a crash at that batch on
that cursor are used (Q41); a frontend that wants a clean re-run removes
them first.

### branch
**Rust/Python**: no type; a verb · **retires** fork (in-session),
`commit_branch`, `BranchPlan`, `branch_summary`, `spawn_branch`

To branch is to move to a node that is neither on `walk(at)` nor below `at`.
The cursor leaves its old line at the LCA, or entirely if there is none.
Moving to an ancestor is going back, not a branch; a branch forms there only
when the next commit makes a sibling.

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

What a cursor needs done, as a pure function of `walk(at)`. It never looks
below `at`. It skips `Config` and `Custom` nodes from the end and decides on
the nearest message-bearing node:

| nearest message-bearing node | `Next` |
|---|---|
| `User` or `Results` | `Sample` |
| `Assistant` with `stop == tools` and at least one call | `RunTools` (all its calls) |
| any other `Assistant`, `System`, or none | `Wait` |

### step
**Rust** the step machine (D11) · **Python** `Session.step(cursor)` ·
**retires** loop turn, agent run, user turn, submission, turn (all four
senses, D1), and "cycle" (this glossary's own sixth revision)

One pass of the phase loop for one cursor, from `pre_step` to `post_step`
(`model.md` §6):

```
pre_step
for phase in [input, sample, tools]:     if the phase applies
    pre_<effect>, effect, pre_<draft>, commit under at, post_<draft>
post_step(ending)
```

A phase applies only if auto-step is on or a frontend asked for this step,
and its condition holds for `at` at the start of the phase. After
`post_step`, another step starts if auto-step is on and `next` is not `Wait`
or input is pending, or, throttled, if the cursor's scratchpad changed since
this step's `pre_step` started. Otherwise the cursor rests. After an abort,
a halt, or a failure it rests regardless.

`Session.step` runs one step's phases whatever the cursor's auto-step, and
resets its `max_steps` count; on a busy cursor it is refused. Each step that
commits something counts toward `max_steps`.

### phase
**Rust** `Phase` · new

One of the three parts of a step, each with the same shape: pre hook on the
effect, effect, pre hook on the draft, commit under `at`, post hook.

| phase | applies when | effect | pre hooks | commits | post hook |
|---|---|---|---|---|---|
| input | there is pending input, and `next != RunTools` | none | `pre_input`, per item | one `User` node per item, each delivering it | none |
| sample | `next == Sample` | the provider request | `pre_sample`, then `pre_reply` | one `Assistant` node | `post_reply` |
| tools | `next == RunTools` | the calls, in parallel (Q21) | per call: `pre_tool`, then `pre_result`, then a journal item | one `Results` node at the join | `post_result` |

The input phase waits while `next == RunTools`, so a batch is answered
before input goes under it (Q40).

### draft
**Rust** `Draft` · **Python** the drafts below · **retires** the plan's
`draft` (a node held under `target` before commit), this glossary's own
earlier sense

What the current phase will commit: input `User` nodes, a reply, or a
batch's outcomes. It is part of the step machine's state, not stored. Pre
hooks on the draft edit it in place. A halt discards it; an abort commits it.

### input draft
**Python** `InputDraft` · new

The subject of `pre_input`: one pending input's text and images, editable.
What it holds when `pre_input` finishes is what the `User` node holds.

### sample draft
**Python** `SampleDraft` · new

The subject of `pre_sample`. Setting its `reply` **answers** the sample (a
cache hit): no request is made, and the reply goes on to `pre_reply`.

### reply
**Python** `ReplyDraft` · **retires** `AssistantMessage` (as a mutable
in-flight object)

The subject of `pre_reply`: what a sample returned, before it is committed.
Its parts are editable; its `stop` is a fact about the sample and is not. It
becomes an `Assistant` node at the commit, under `at` as `pre_reply` hooks
left it.

### call draft
**Python** `CallDraft` · new

The subject of `pre_tool` and `pre_result`, one per call. In `pre_tool` its
arguments are editable, and setting its `outcome` **answers** the call: the
tool does not run. In `pre_result` it holds the call as it ran and its
outcome, whose content is editable but not its kind (Q18).

### effect
**Python**: none (a phase part) · new

The outside work a phase exists for: the provider request in the sample
phase, or running the calls in the tools phase. The input phase has none. An
abort cancels the effect in flight and nothing else. A commit or a move is not
an effect.

### ending
**Rust** `Ending` · **Python** `Ending` · new

How a step ended, the subject of `post_step`:

| ending | when |
|---|---|
| `done` | the step ran its phases, or none applied |
| `halted(reason)` | a cursor hook returned a string |
| `failed(error)` | a sample failed, or a cursor hook raised |
| `aborted` | an abort, or a frontend's move of the busy cursor |

What the step did is in `cursor.moves(since_step=True)`.

### halt
**Python**: a cursor hook returning a string · new

What a cursor hook does by returning a string instead of `None`. Later hooks
at that point do not run, the current phase's draft is discarded, auto-step
turns off, and `post_step` runs with `halted(reason)`. In `pre_input`, the
input is refused: its item is removed, and its client gets a `notice` with
the reason (Q43). From `post_step`, a string only turns auto-step off. A tool
hook's string is not a halt: it becomes that call's `Err(reason)`.

### wake
**Python**: driver behavior · new

What starts a step on a cursor at rest: an item added to its scratchpad, its
opening, its session loading (every cursor, in the lease holder only), a
go-to landing it, a frontend stepping it (`Session.step`) or turning its
auto-step on. A wake during the throttle joins the step already scheduled.

### throttle
**Python**: driver behavior · new

A step that committed nothing starts the next at least 250 ms after it
started. The cursor is at rest while it waits. Nothing polls: a further step
starts only when the scratchpad changes, something is committed, or the
cursor is woken.

### auto-step
**Python** `Cursor.auto_step`, set with `Session.set_auto_step(cursor, on)`
or `cursor.auto_step = …` · **retires** step mode, suspension,
`api.suspend`, `PausableDeadline`, responder

A cursor's only flag. When it is on, phases run and steps follow one another
until `next` is `Wait` and no input is pending. When it is off, a woken
cursor runs only `pre_step` and `post_step`; `Session.step` still runs a
step's phases. It goes off on abort, on a halt, on a failure, on a go-to, and
when the cursor has taken `max_steps` committing steps without the frontend
acting on it, so neither a failure nor a runaway loop repeats unattended.
Every cursor loads with it off. Turning it on resets the `max_steps` count
and wakes a cursor at rest. To **pause** a cursor is to turn its auto-step
off.

### resume
Continuing a batch after a crash. The cursor loads at the batch's
`Assistant` node, so the next tools phase runs it: each call with a journal
item uses that outcome; each other call runs again if its tool is idempotent,
and is otherwise `Interrupted` (D4). The join delivers the journal items.

### idempotent
**Python** `Tool.idempotent: bool` · new

A tool flag meaning that running the tool twice has the same effect as running
it once. Only idempotent calls without a journal item run again on resume
(D4).

### sample
**Rust**: none (an effect) · **Python** `Provider.sample` · **retires**
completion, stream (as a noun), model call

One provider request over `walk(at)` with `config_at(at)`, as `at` stands
when the sample starts (I1). Parts arrive as `stream_delta` events and are
never written token by token. Whatever stops the sample (an end, a handoff, a
limit, an abort, an error), the accumulated parts become the reply draft,
provided any arrived.

### context
**retires** `ExtensionContext`/`ctx`, the `context` hook, `context=` parameter

The walk a sample is drawn from, before provider conversion: `walk(at)` when
the sample starts. A `pre_sample` hook shapes it by moving the cursor (Q25).
"Context window" is the provider's limit on it, in tokens.

### message
**retires** message entry, `customMessage`, `AgentMessage`

The provider's wire unit. **messages** = the provider's conversion of a
context's message-bearing nodes, with each `Results` node expanded into one
tool message per call. Never stored.

### config
**Rust** `Body::Config`, `config_at(n)` · **Python** `Config` (pydantic,
Phase 4) · **retires** config entry, `CONFIG_KEYS`, `TurnFrame`, settings
(`Settings`, `settings.json`), model entry, restating `Config` (this
glossary's own earlier revisions, with I6)

Everything that decides how a step runs: provider, model, request settings,
tools, and the hooks in force. A `Config` node stores it as a full
**snapshot**. `config_at(n)` is the body of the nearest `Config` node on
`walk(n)`; it is always defined, because every root is a `Config` node.
Partial updates are never folded together. Going to another line uses that
line's config: nothing restates it (D5). A `Config` node persists on its
line until another replaces it (Q26).

Fields named so far:
- **`max_steps`** (Q20): the most committing steps a cursor takes without
  the frontend acting on it. The driver counts in memory, per cursor, and
  only actions `by: frontend(…)` reset the count: giving input, stepping the
  cursor, turning auto-step on. A cursor a hook or tool opens inherits its
  opener's count; one a frontend opens starts at 0. When the count reaches
  `config_at(at).max_steps`, auto-step turns off. Nothing is stored.
- **`partial_calls`** (Q27): `text` (the default) or `omit`. It sets what a
  call that was never run becomes when the walk is sent again: `text`
  renders it after the reply in τ's `[τ: …]` markup, and `omit` drops it.

### validate
**Rust** `validate(walk)`

The structural checks (I5): V1, a parentless node is `Config`; V2, a
`Results` node's outcomes name distinct calls. `append` checks each new node,
so every walk is valid by construction. `validate(walk)` re-checks a whole
walk, for tests. Provider legality is checked separately, by the provider's
conversion (Q11).

### hook
**Python** a cursor hook or a tool hook · **retires** the 0.15 hooks
(`input`, `before_agent_start`, `tool_call`, `tool_result`, `turn_end`,
`user_turn_end`, `annotate`, `entry_written`, …), handler policy, suspension
site, chain, and this glossary's own earlier outcomes (handled, pass,
refuse)

A Python function an extension registers at one **hook point**. There are
two shapes (cursor hook, tool hook), and one return rule: **`None` carries
on; a string stops, and says why.** At each hook point, hooks run in the
order their extensions are listed in `config_at(at)`. Drafts are edited in
place, not returned.

Nodes, moves, and items a hook writes trigger no hooks on its own cursor's
step. An item it adds may wake another cursor, whose step runs hooks as
usual.

An uncaught exception from a cursor hook is a halt with `failed(error)`; in
`pre_input` the input stays pending. From a tool hook, the call's outcome is
`Err("hook error")` (`by: core`), and in `pre_result` the raw output is
withheld. Abort never cancels a hook.

### cursor hook
**Python** `async def hook(cursor: Cursor, subject) -> str | None` · new

The shape of `pre_input`, `pre_step`, `pre_sample`, `pre_reply`,
`post_reply`, `post_result`, and `post_step`. It gets the cursor handle, so
it may move its cursor, write under `at`, and set auto-step. A string return
halts the step.

### tool hook
**Python** `async def hook(node: NodeHandle, call: CallDraft) -> str | None` ·
new

The shape of `pre_tool` and `pre_result`. Tool hooks run in parallel, one
chain per call, so they get a node handle instead of the cursor and cannot
move it. A string return makes that call's outcome `Err(reason)`: in
`pre_tool` the tool does not run, and in `pre_result` the outcome is
replaced. Later hooks of that call's chain do not run; the other calls carry
on.

### hook point
**Python** the registration name · new

One of nine:

| hook point | shape | runs | subject |
|---|---|---|---|
| `pre_input` | cursor | for each pending input item, oldest first | the input draft |
| `pre_step` | cursor | at the start of every step | none |
| `pre_sample` | cursor | before a sample | the sample draft |
| `pre_reply` | cursor | before a reply is committed | the reply draft |
| `post_reply` | cursor | after the reply is committed and moved onto | the `Assistant` node |
| `pre_tool` | tool | before each call runs | the call draft |
| `pre_result` | tool | as each call finishes | the call draft, with its outcome |
| `post_result` | cursor | after the `Results` node is committed and moved onto | the `Results` node |
| `post_step` | cursor | at the end of every step, aborted ones included | the ending |

An answer from `pre_sample` or `pre_tool` skips the effect, but the draft's
pre hooks still run (Q18). Outcomes core writes (`Err("invalid arguments")`,
`Interrupted`, `Err("hook error")`) skip `pre_result`.

### pre_step
**Python** hook point name · new

The cursor hook point at the start of every step, where an extension takes in
what has arrived. One of the two hook points where an extension may read its
cursor's scratchpad. With auto-step off, a woken cursor runs only `pre_step`
and `post_step`; turning auto-step on here lets the phases run.

### post_step
**Python** hook point name · new

The cursor hook point at the end of every step, with its ending. One of the
two hook points where an extension may read its cursor's scratchpad. After
an aborted step, an extension may offer pending input back to its client
here (Q42).

### answer
Setting a draft's result in a pre hook on an effect, so the effect does not
run: a sample draft's `reply` in `pre_sample` (a cached reply), or a call
draft's `outcome` in `pre_tool` (a mocked or cached outcome). Not to be
confused with answering a question.

### node handle
**Python** `NodeHandle`, the first argument to a tool hook · new

A handle to a batch's `Assistant` node, bound to the hook's extension and the
cursor. It can do everything a cursor handle can (read, append, write
scratchpads, ask, show, tell, open cursors, set labels, sample a walk, run a
tool) except move the cursor, write under `at`, or set auto-step.

### cursor handle
**Python** `Cursor`, the first argument to a cursor hook; a narrower one is
handed to a tool · new (the fourth revision called it `api`)

The cursor a hook runs for, bound to the hook's extension, so everything it
writes is authored `extension(name)` and every move it makes is
`by: extension(name)`. Through it a hook may:
- read: `cursor.at`, `cursor.next`, `cursor.busy`, `cursor.auto_step`,
  `cursor.labels`, `cursor.moves()`, and the session (`cursor.session`),
  including other cursors' positions;
- append nodes anywhere; add, update, or remove items in any cursor's
  scratchpad; ask (`cursor.ask`), show (`cursor.show`), and tell
  (`cursor.tell`); open cursors; set and remove labels; sample a walk and run
  a tool outside any step (compaction's cold summary on a detached root);
- act on its own cursor: `cursor.move(to)`; `cursor.write(body,
  delivers=[…])`, which appends under `at` and moves onto it as one record;
  `cursor.auto_step = …`.

It never acts on another cursor. It reads its own cursor's scratchpad only in
`pre_step` and `post_step`.

A tool's handle is bound to the calling cursor, and is narrower: it may read,
append, sample a walk, run a tool, open cursors, write scratchpads, and wait
on a cursor it opened, but it moves no cursor (Q32). The exact surface is a
Phase 4 decision. It is not the 0.15 `ExtensionAPI`.

### cursor.write
**Python** `cursor.write(body, delivers=[])` · new

A cursor hook's commit: append `body` under `at` and move onto it, as one
record, authored by the hook's extension. With `delivers`, it consumes those
items, as a sub-agent's `pre_step` does with its task.

### question
**Python** `await cursor.ask(target, question)`, `Session.answer`,
`Session.decline` · **retires** `request_user_action`, suspension prompt

A hook asking a person something. `target` is one client or any client.
`cursor.ask` returns the answer, or `None` if it was declined. While a
question is open, its cursor is locked; abort does not close it. Core does
not look at the question or the answer: both belong to the extension. A
question for one client is declined if that client detaches (Q37); a
question for any client holds the cursor until some client answers, and is
replayed to every client that connects. Questions are driver memory: a crash
loses them.

### event
**Rust** `Event` · **Python** `Event` · **retires** `AgentEvent`, `WireEvent`,
serve event, RPC event, bus channel, hook event dict

One type, tagged with a cursor, in eight kinds: `node_appended`,
`cursor_moved`, `label_changed`, `stream_delta`, `item_changed`, `question`,
`status`, `notice` (`model.md` §9). Observers cannot block or change
anything. Every wire format is a projection of this one type. Events go to
whoever is subscribed, possibly nobody; a `notice` goes only to its client.

### abort
**Python** `Session.abort(cursor)` · **retires** `AbortSignal`, `AbortReason`,
abort-tree, `rollback`

Esc. It turns the cursor's auto-step off and cancels the effect in flight:
the sample stream, or the running tools. **It never cancels a hook**, and no
later effect starts. The current phase's draft goes through its pre hooks and
is committed as usual, its post hook runs, and `post_step` runs with
`aborted`; then the cursor rests. An aborted sample with output is committed
with `stop: interrupted`; one with none commits nothing. Aborted tools give
each running or unstarted call `Interrupted`, and the join commits the
`Results`. A call whose `pre_tool` is running finishes it; if it returns
`None`, the call does not start and is `Interrupted` (Q34). On a cursor at
rest, abort only turns auto-step off. A frontend's go-to of a busy cursor is
an abort, then the move (`model.md` §8, Q4).

---

## Effects and edges

### provider
**Python** `Provider` · **retires** backend (config key), `ProviderSpec`,
`api` (as a selector), client pool

The Python object that turns a context into a sample, for one API family
(OpenAI-compatible first). It also converts walks to messages, expanding
`Results` nodes, and that conversion is where provider-specific legality
lives (Q11). What every provider owes the driver is listed in `model.md`
§10. In 0.15 this word had four meanings; here it means only this adapter.

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
  cursor. It moves no cursor (Q32).

### extension
**retires** bucket, `ExtensionHandlers`, `ExtensionRunner`, `ExtensionState`,
`ExtensionAPI` (as a 42-method surface)

A Python module that registers hooks and tools through the public API.
Extensions have no special standing in core (D3, D8). Extensions on one
cursor, or on different cursors, work together through the scratchpad. The
first three built in Phase 5 prove the model:
- **compaction**: summarises an old prefix and moves onto a shorter line
  made of the summary (a `User` node by the extension, Q30) and copies of the
  recent nodes, `Config` nodes included.
- **prune**: after the provider's cache expires, rewrites old tool outputs and
  thinking as derived nodes, then moves onto the rewrite.
- **sub-agent**: a tool that, through its handle, opens a cursor
  (`opened_by` = the calling cursor), for example on a detached root, adds
  the task as an item to its scratchpad, waits until it rests at
  `next == Wait`, and returns a value whose `derived_from` lists the cursor's
  final node. Details are Phase 5.

### frontend
**retires** head, backend (as `Backend`/`TauBackend`/`RemoteBackend`), head
label

A program that drives sessions and shows events: `tau serve` first, then
print/json, REPL, RPC, TUI. A frontend serves one or more clients; a local
frontend is the same model with one client, serving itself.
- A frontend keeps no queue. It hands input to `Session.input` at once, busy
  or not (D2).
- A frontend may give input (`Session.input`), step a cursor
  (`Session.step`), go to any node (`Session.move`), set auto-step
  (`Session.set_auto_step`), abort (`Session.abort`), answer or decline a
  question, set and remove labels, and add, update, or remove scratchpad
  items (Q15). Giving input, stepping, and turning auto-step on reset the
  cursor's `max_steps` count.
- Every frontend sees every cursor and every scratchpad. No cursor waits on a
  particular client: a client may switch cursors or sessions, or detach, and
  every active cursor keeps operating.

### client
**Python** the client handle a frontend calls through · new

One connection to a frontend: a terminal, or each connection to `tau serve`.
Every client has an id, and everything it does is `by: frontend(client)`. The
id is recorded, never used for control flow, except that a question or a
notice may be addressed to one client.

### status
**Python** `cursor.show(state)` · new

The state an extension shows every frontend for its cursor. Each call
replaces the extension's previous state. Driver memory: a reload clears it,
and `tau serve` replays the current state to each client that connects.
Emitted as a `status` event.

### notice
**Python** `cursor.tell(client, message) → bool` · new

A message to one client: from an extension, such as "put this text back in
your input box", or from core when `pre_input` refuses that client's input.
`cursor.tell` returns whether the client was attached to receive it. Emitted
as a `notice` event to that client only.

### driver
**Python** `tau_next.driver`

The Python code that performs what core's step machines ask for (D11). It
runs hook points, provider samples, and tools, awaits them, cancels what core
says to cancel, passes records and scratchpad changes to the store, sleeps
for the throttle, and feeds every result back to core with the time. It
decides nothing: which phase applies, hook order, halts, aborts, and
`max_steps` are core's. It is the only place that awaits.

### job
**Rust** `Job` · new

One thing a step machine asks the driver to do: run a hook point, sample,
run a set of calls, persist a record, update the scratchpad, wait (the
throttle), or rest. The driver performs it and feeds back its result, or each
call's completion in any order. A hook job's result is what the hook changed
and what it returned. A job never decides anything (D11).

### store
**Rust** `tau_store` · **retires** session store, `SessionCatalog`, JMFTS
session log (until Phase 7)

The only Rust code that touches the filesystem. It keeps, per session, one
append-only record log and one scratchpad file, behind a trait (D12).

### record
**Rust** `tau_store::Record` · new

One durable write in a session's record log: one or more nodes with at most
one move, or one label change. Length-prefixed and checksummed. A commit and
its move are one record, so a crash leaves both or neither. A torn final
record is ignored on load (I7).

### lease
**Rust** `tau_store` · **retires** `SessionWriteLock`, `WriteAccess`,
`LockHolder`, lock sidecar

One `flock` per session, taken on its record log. The holder may write, and
only the holder runs steps and hooks (the wakes on load included); every
other process opens the session read-only and never runs a hook. A lease
whose holder process is dead is reclaimed.

---

## Testing (Phase 2)

### Harness
**Python** `tests/behavior/harness.py` · **retires** session log contract
tests

The protocol every behavior test drives, shaped like `tau serve`: a test
attaches one or more clients to a session and drives it through them, the
same model a local frontend uses with its one client. It mirrors the
frontend operations: create (`Session.create`) and load sessions, give input
(`Session.input`), step (`Session.step`), set auto-step
(`Session.set_auto_step`), abort (`Session.abort`), open cursors
(`Session.open_cursor`), go to a node (`Session.move`), set labels, answer or
decline questions, read and change scratchpad items, snapshot the tree,
cursors and scratchpad, and read events, which are the only feed. It has one
adapter for `tau_next` (in `tests/`) and one for τ 0.15 (in `parity/`).

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
| side entry, `SIDE_KINDS` | side node: an ordinary node no cursor's walk passes through |
| incomplete / finalize | nothing: nodes are written once, complete |
| leaf, default leaf | `at`, persisted through move records; no guessing |
| head cursor, acting cursor, `TURN_CURSOR` | every cursor is explicit, and hooks receive theirs |
| turn frame, `TurnFrame` | the `Config` node on the walk |
| turn (four senses) | step (D1); frontends count steps since last input |
| submission, multitask strategy | input items in the scratchpad, delivered by the input phase (D2) |
| suspension, responder, `api.suspend` | auto-step off: the cursor rests between steps. A hook that needs a person asks a question |
| lane | nothing |
| head | frontend |
| backend | provider (model API) or frontend (driver of a session) |
| fork (in-session) | branch |
| compaction entry, splice | the compaction extension: a summary node plus a move (D3) |
| extension lock, `request_user_action` | a `pre_input` hook that returns a reason until satisfied, or a question |
| `input` hook | `pre_input` |
| `before_agent_start` hook | `pre_sample` (moves onto what it adds, I1) |
| `tool_call` hook | `pre_tool` |
| `tool_result` hook | `pre_result` |
| `turn_end`, `user_turn_end` hooks | `post_reply` (Q20), or `post_step` |
| `annotate`, `entry_written` hooks | event observers (`node_appended`) |
| session write lock | lease |
| `AgentSession`, runtime, catalog, `SessionManager` | session |
| `ExtensionContext` / `ctx` | the cursor handle, a cursor hook's first argument |
| handler policy (gating/advisory/human) | nothing in core (Q9) |

Terms this glossary retired in its own earlier revisions:
- sixth revision and before: `post_open` (now a wake and `pre_step`),
  `post_input` and the input step (input is now the input phase), interrupt
  (now abort, and a frontend's move of a busy cursor), scratch (renamed
  uncommitted, itself now retired).
- seventh revision:
  - **committed**, **uncommitted**: a node is on the tree once it is
    written; "side node" covers nodes no cursor's walk passes through.
  - **boundary**: a cursor hook may move its cursor at any of its hook
    points; tool hooks cannot.
  - **cycle**: a step now runs from `pre_step` to `post_step`.
  - **base extension**, its `deliver` and `combine` settings, and
    `cursor.deliver`: core delivers input in the input phase.
  - **returned input**: pending input stays pending after an abort (Q42).
  - **run**, and the batch states **unstarted**, **partial**, **complete**:
    one `Results` node per batch, and the journal holds what finished.
  - **`Result`** (one node per call): replaced by `Results`.
  - **fast-forward**: a move never looks below `to`.
  - **claim**: two cursors at one batch each run it (Q13).
  - **`Session.rerun`**: go to the batch and step.
  - **`Skipped`**: a tool hook returns a string, which becomes `Err(reason)`.
  - **refuse**, **handled**, **pass** (hook outcomes): `None` or a string.
  - **restating `Config`** and I6: config is the nearest `Config` on the
    walk (D5).
  - **label on the move record**: labels are mutable label records.
