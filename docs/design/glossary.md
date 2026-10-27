# Glossary

Status: **for review (Phase 1), fifth revision.** On sign-off this file and `model.md`
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
`{id, parent?, time, by, body, copied_from, derived_from}`. It is written
once and never changed (I2). There is no "incomplete" node, and no second
write that completes one.

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
- `sample_error`: written by core (`by: core`). It records, as scratch, a sample
  that failed before any output arrived (`model.md` §8).
- `pending_input`: written by the base extension directly under the
  session's first root: `{cursor, text, images}`, input held for a cursor
  (see pending input).

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
| `Skipped(reason)` | deliberately not run: a `pre_tool` hook declined it ("blocked by …") |
| `Interrupted` | no result was written: the call's step was interrupted (an abort, or a move of its busy cursor), or a crash came before its result was written. The tool may or may not have run (D4) |

### author
**Rust** `Author` · **Python** `Node.by` · **retires** `source:"agent"`,
submitter, `SubmissionSource`

Who wrote a node:
- `frontend`: a person's input.
- `provider`: a sample.
- `tools`: a result from running a call.
- `core`: the session's first root and its `System` node (`Session.create`),
  I6's restated config, a `sample_error` record, and every result core writes
  itself: `Interrupted`, `Err("invalid arguments")`, `Err("hook error")`.
- `extension(name)`: a hook, including a root a hook creates (a detached
  root), and a node a hook handled input with, unless it is the input as
  given. The base extension's pending inputs, and inputs it delivers from a
  post hook, are `extension(base)`.

A node's author is whoever produced its content as written. A hook's answer or
rewrite makes that hook's extension the author, and so does a copy. The author
is recorded and never used for control flow (Q7). No API caller supplies it:
the driver fills it in from the caller, because hooks get a cursor handle
bound to their extension and frontends call through the frontend API. A move
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
- A delivered input lists the pending inputs it delivers, one or several
  (joined by a blank line). That list is the only record of delivery: a
  pending input is one no node copies from.
- The store keeps an in-memory **reverse index**, built at load and never
  stored, so `Session.copies_of(n)` is one lookup.
- The tail copies `Config` nodes too, verbatim. A copied `Config` counts as
  explicit under I6, so the copy keeps the config it records.
- A copied batch (an `Assistant` node with calls) must be followed by copies
  of its results. A move onto a copied batch that is not complete on the walk
  is refused, so the driver never runs a copied call (I3). A hook that
  crashes partway through a copy leaves an unlanded scratch copy.

### derived_from
**Rust** `Node::derived_from` · **Python** `Node.derived_from` · new

A list of the nodes whose walks this node's content was *computed* from. Most
nodes have none.
- A summary points at the last node of the range it summarised (the range is
  that node's walk below the summary's own parent), and at the detached-root
  reply that produced it, if one did.
- A prune rewrite points at the node it rewrote.
- A sub-agent's answer points at the sub-agent's final node.
- A synthetic sibling a `post_input` hook writes points at the typed input.

One id per input is enough, because the walk to each id is that input.

### session
**Rust** `Session` · **Python** `Session` · **retires** `AgentSession`,
`SessionLog`, file `Session`, `SessionManager`, `AgentSessionRuntime`,
catalog, `ReplicaSession`

One conversation: a forest of nodes plus its cursors. Append-only. A session
is persisted as nodes and move records, and nothing else (I7).

**`Session.create(config, system)`** appends the root `Config` and a `System`
node under it (both `by: core`), and opens the first cursor at the `System`
node (Q5). In 0.15 this
word had ten meanings; here it has one.

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

Any root after the first. It holds one-off work a hook does outside the
conversation, such as a cold summary from a cheap model. Its `Config` records
which model did the work. Its nodes are scratch unless a cursor moves onto
them.

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

## Position

### cursor
**Rust** `Cursor`, `CursorId` · **Python** `Cursor` · **retires** head cursor,
acting cursor, `TURN_CURSOR`, `TurnFrame`, `RemoteCursor`

An insertion point: `{id, at}`, plus the driver's in-memory `auto_step` flag
and whether it is busy. Every write a step makes is followed by a move of its
cursor onto it.
- Several cursors may share a session, and even a node.
- A cursor carries no settings: those come from the walk (`config_at`).
- A cursor carries no queues. Input for a busy cursor is held as nodes, by
  the base extension (D2).
- Cursors do not close (Q12).

### busy
**Python** `Cursor.busy` (driver memory) · new

A cursor is busy while a step holds its **step lock**: from the step's start
(its first pre hook, or its wait for a claim) through its post hooks and the
evaluation of `next` that follows, or until it is interrupted. Otherwise it
is **at rest**. A frontend's `Session.step` and an input step on a cursor at
rest wait for the lock. A cursor's input hooks and post hooks never run
concurrently, and input hooks read `busy` under that ordering. Never stored:
every cursor loads at rest. A move of a busy cursor from outside its step
interrupts the step (see interrupt).

### open_cursor
**Rust** `Session::open_cursor(at, opened_by?)` (internal) · **Python**
`Session.open_cursor(at)` · **retires** `spawn`, `newest`, cursor registration

Creates a cursor and appends its opening move. The driver fills in
`opened_by` from the caller, as it fills in `by`: a hook on cursor A that
opens a cursor records A, and so does a tool running for cursor A (a
sub-agent); a frontend leaves it empty. The node must be committed and its
walk must validate. A node on a batch or inside one of its runs fast-forwards
to the end of the run, as a move does (I3). Opening a cursor never starts a
step and never takes a claim. The new cursor's `post_open` hooks run after
its opening move, whoever opened it (Q32). A cursor a hook or tool opens
inherits its opener's `max_steps` count and auto-step setting; one a frontend
opens starts at 0, with auto-step off.

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

### scratch
**Python**: a view · **retires** side entry, orphaned suspension

Derived, never stored. Every node that is not committed: no move has landed on
it or passed through it. That covers:
- side nodes an extension appends off a line, or anywhere else;
- detached-root work;
- the nodes a `pre_sample` hook built for a sample that produced nothing;
- `sample_error` records;
- an I6 restatement for a sample that produced nothing (reused next time);
- pending inputs;
- a copy a hook left unfinished.

Scratch is persisted like any node and stays inspectable. It is just not on
anyone's conversation, until a move reaches it. Apart from an unreached I6
restatement, which is reused rather than duplicated, core never writes a node
and then abandons it, so scratch is otherwise an extension's work or a record
of a failure.

### side node
**Python**: none (an ordinary node) · new

A node a hook appends off the cursor's line: a `Custom` note under a written
node, an alternative, a pending input under the first root, or work on a
detached root. It is scratch unless a move
later reaches it. Nothing marks it; "side" describes where it sits.

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
2. refuse a move onto a copied batch that is not complete on the walk;
3. if the resolved `to == at`, write nothing (a re-run skips this check);
4. apply I6 for a go-to, a hook's move, or a re-run; at an unstarted or
   partial batch the restatement is deferred to the completing result;
5. if the move interrupts a step, wait for the interrupt and resolve `to`
   again; then append any new restating `Config` and the move record
   together, set `at := to`, and emit `cursor_moved`.

A move never takes a claim, and is never refused because of one.

A move comes from a step (onto what it wrote, or a fast-forward the step makes
when it re-reads a run), a frontend's go-to, any hook, or a re-run. Rust's
`by` argument is internal: no caller supplies it. The driver fills it in from
the caller, because hooks get a cursor handle bound to their extension and
frontends call through the frontend API.

A move of a busy cursor from outside its step interrupts the step in place,
waits for it, and then applies; that move wins. A hook of the step moving its
own cursor has the move applied when the hook returns, after the same
interrupt. A post hook's move after its step's own move happens inside the
step and interrupts nothing. After a hook's move, once the hook returns, the
driver carries on per auto-step; a go-to never starts a step.

A refused move changes nothing, interrupts nothing, and raises to whoever
asked. A hook that catches its own refused move has handled it, and auto-step
is unchanged. A step's own move is never refused and never skipped.

### move record
**Rust** `Move` · **retires** navigate entry

The persisted fact of one move, or of one cursor opening:
`{cursor, from?, to, by, time, opened_by?}`. `from` is absent on an opening
move. `by` says who asked: `frontend`, `core` (a step's own move), or
`extension(name)` (a hook's move or re-run) (Q28). `opened_by` names the
cursor that opened this one: a hook's cursor, or the cursor a tool runs for,
such as a sub-agent's parent (Q6).

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
- Frontends call it directly, and any hook through its cursor handle. A
  re-run of a busy cursor interrupts its step, like any move. A hook's re-run
  does not nest: the call returns once the move is written, and the tools
  step runs when the hook returns. It counts toward `max_steps`. Tools cannot
  re-run.

Resume after a crash never re-runs on purpose: a re-run that crashed before
its first result fast-forwards on reload, and the user asks again
(`model.md` §5, §8).

### go to
**Python** `Session.move(cursor, to)` from a frontend · **retires**
`navigate_tree`

A move a frontend asks for, with no new input and no hooks (Q15). It is how a
person browses, branches, **goes back** (to an ancestor on `walk(at)`, which
is not a branch), or rejects a reply (go back to its parent, then step: the
step's write makes the sibling, and that is the branch). **A go-to never
starts a step**, even with auto-step on: browsing runs nothing (no step, no
hook, no provider call), and at most writes one restating `Config` (I6). A
cursor that lands on a `User` node or an unstarted batch sits there until the
frontend steps it or gives input. A go-to on a busy cursor interrupts its
step, then the cursor sits where it landed. After a hook's move, by contrast,
the driver carries on per auto-step. To run a batch again, use a re-run.

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

### input
**Python** `Session.input(cursor, text)` · new (0.15's submission is retired
under step)

What a frontend gives a cursor, busy or at rest. Core does nothing with it
but run the cursor's `pre_input` hooks (`model.md` §7). One of them handles
it, optionally with a body that an **input step** writes under `at` and moves
onto; or one refuses it; or every hook passes and it is refused ("no
extension handled this input"). A refused input changes nothing, and the
frontend keeps the text. Core has no default input behavior and no queue
(D2): the base extension supplies both. To give input elsewhere, go there
first. Input resets the cursor's `max_steps` count.

### base extension
**Python** `tau_next` default config · new

The extension that ships in the default config and gives input its everyday
behavior. A config may replace it, and its place in the config does not
matter. Its hooks:
- `pre_input`, **at rest**: handles the input with a `User` body. If inputs
  are still pending for the cursor, it appends this one as pending too and
  handles the input with one `User` body for all of them, `copied_from`
  listing them, so nothing typed earlier lands later (delivery at rest).
- `pre_input`, **busy**: appends a pending input and handles the input
  without a body. Input hooks never run while the cursor's post hooks do, so
  the post hooks that follow see it.
- `pre_input`, **at rest at an unstarted or partial batch**: appends a
  pending input and steps the cursor, so the batch is answered and the input
  delivered after it (Q17).
- `post_result` and `post_reply` deliver, per its settings `deliver` and
  `combine` in `Config` (see config).
- `post_open` delivers inputs still pending from before a restart.

**Interrupt and send** is a different input hook, not the base extension: on
a busy cursor it handles input with a `User` body, which interrupts the step
in place.

### pending input
**Python**: none (a `Custom` node) · new

`Custom{kind: pending_input, data: {cursor, text, images}}`, appended
directly under the session's first root: input the base extension holds for a
cursor until it is delivered. An input is pending while no node lists it in
`copied_from`; there is no other marker. Because it is a node, it survives a
crash, and the reverse index of `copied_from` makes "is it delivered?" one
lookup. Scratch, like any side node.

### delivery
**Python**: none (base extension behavior) · new

Writing a cursor's pending inputs onto its line as a `User` node whose
`copied_from` lists them. From a post hook it is
`cursor.write(User{…}, copied_from: [p1, …])`: the node and its move are one
durable write, so a crash leaves both or neither, and an input is never
delivered twice. At rest it is a handled body carrying `copied_from`, written
by an input step. The `copied_from` list is the only delivery record.

### step
**Python** `Session.step(cursor)` · **retires** loop turn, agent run, user
turn, submission, turn (all four senses, D1)

`Session.step` runs one step on a cursor, whatever its auto-step, and resets
its `max_steps` count. One cycle for one cursor (`model.md` §7). Its phases run in order:
1. pre hooks on the effect (`pre_sample`, `pre_tool`);
2. the **effect**: a provider request, tools, or nothing for input;
3. pre hooks on the node (`pre_reply`, `pre_result`);
4. **write** the node and **move** the cursor onto it;
5. post hooks on the written node (`post_input`, `post_reply`,
   `post_result`).

Then the driver evaluates `next` again.

There are three **step kinds**:
- `input`: an input hook handled input with a body (`Session.input`);
- `sample`: `next` is `Sample`;
- `tools`: `next` is `RunTools`. A tools step writes one result per call as
  each call finishes, and moves onto each one.

A step holds its cursor's step lock (see busy) from its start through its
post hooks and the evaluation of `next`. Each node it writes goes under the
node it was produced from (a reply under its context tip, a result under its
run), and its own move lands on it. A step either moves its cursor onto what
it wrote; or (refused, or a sample that failed with no output) leaves `at`
where it was and turns auto-step off; or is interrupted, in which case it
writes its records, moves onto them, and ends.

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

A per-cursor flag. When it is on, the driver keeps stepping until `next` is
`Wait`. When it is off, the cursor **pauses** after each step, already moved,
until the frontend resumes it. It goes off on its own when a step ends without
moving its cursor (refused, failed), on abort (but not on other interrupts),
and when the cursor has taken
`max_steps` steps without the frontend acting on it, so neither a failure
nor a runaway loop repeats unattended. Every cursor loads with it off.
Turning it on resets the `max_steps` count. To **pause** a cursor is to turn
its auto-step off.

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
  `by: frontend` reset the count: giving input, stepping the cursor,
  re-running a batch, turning auto-step on. A cursor a hook or tool opens
  inherits its opener's count; one a frontend opens starts at 0. When the count reaches `config_at(at).max_steps`,
  auto-step turns off and the cursor pauses. `next` does not look at it, and
  nothing is stored.
- **`deliver`**, the base extension's: `after_batch` (the default) delivers
  pending inputs on the result that completes a batch, or on a reply that is
  not a batch, whichever comes first; `after_stop` waits for a reply that is
  not a batch. Never mid-batch.
- **`combine`**, the base extension's: `true` (the default) delivers all of
  a cursor's pending inputs in one `User` node, texts joined by a blank line;
  `false` writes one `User` node per input, in order. Delivery at rest is
  always one node.
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
outcome, or the written node). At each hook point, hooks run in the order
their extensions are listed in the config, and each receives what the
previous one returned.

What a hook returns steers the hook point:
- An **input hook** (`pre_input`) handles, passes, or refuses the input.
- A **pre hook on an effect** (`pre_sample`, `pre_tool`) may rewrite the
  request, or **answer** it: the effect and the remaining hooks at that point
  are skipped, but the node's pre hooks still run (Q18).
- A **pre hook on a node** (`pre_reply`, `pre_result`) sees a node before it
  is written, and may rewrite it. `pre_reply` may also refuse it.
- A **post hook** (`post_input`, `post_reply`, `post_result`) sees the node
  after it is written and the cursor has moved onto it, before `next` is
  evaluated. `post_open` sees a cursor that has just opened or loaded.

What a hook does through its cursor handle is not limited by its hook point
(see cursor handle). The tree's own rules decide the result: append and move
checks apply. A move of a busy cursor from outside its step interrupts the
step in place. A hook moving its own cursor during its step has the move
applied when it returns, after the step is interrupted in place. A post
hook's move after its step's own move happens inside the step and interrupts
nothing: every post hook at a point runs on the node that was written, and a
later one sees the cursor wherever earlier ones left it (`cursor.at` may have
changed). Two post hooks that both move make two real moves, in order. After
a hook's move, once the hook returns, the driver carries on per auto-step.

Nodes and moves written by hooks trigger no hooks, but a step a hook causes
(after a "continue", or a re-run's tools step) runs hooks as usual, and a
cursor a hook opens runs its own `post_open`.

Post hooks run for every node a step writes and moves onto. Results core
writes (`Err("invalid arguments")`, `Interrupted`, `Err("hook error")`) skip
`pre_result`, since there is no tool output to rewrite, but their
`post_result` hooks run. Only the records an interrupt writes run no hooks.

In a tools step, each call's `pre_result` runs as soon as its tool finishes,
concurrently. Only write, move, and `post_result` are serialised per cursor,
in completion order. Post hooks do not run after an interrupt.

An uncaught exception from a hook ends its hook point, turns auto-step off,
and is shown to the frontend. If the effect has not run, it does not. A call
whose hook raised is answered `Err("hook error")` by core, with any raw
output withheld (`model.md` §7). An input hook's error refuses the input.

### hook point
**Python** the registration name · new

One of nine:

| hook point | runs | subject | outcome |
|---|---|---|---|
| `pre_input` | on `Session.input`, busy or at rest | the input | **handled** (optionally with a body to write, and its `copied_from`), **pass** (the input, possibly rewritten), or **refuse** (a reason) |
| `post_input` | after an input step's move | the written node | nothing |
| `post_open` | after a cursor's opening move, and for every cursor after a session loads | the node the cursor is at | nothing |
| `pre_sample` | before a sample | the context tip | a context tip (the same, or a node it appended: a reminder, a `Config`, a compacted line), or a reply (answered) |
| `pre_reply` | before a reply is written | the reply | the reply, its parts possibly rewritten (never its `stop`), or nothing (refused, nothing written) |
| `post_reply` | after a sample step's move | the written `Assistant` node | nothing |
| `pre_tool` | before each call runs | one call | the call (possibly rewritten), or an outcome (answered) |
| `pre_result` | as each call finishes | the call as it ran, and its outcome | an outcome of the same kind, with its content possibly rewritten (Q18). It cannot drop one: every call is owed a result (I4) |
| `post_result` | after each result's move | the written result node | nothing |

Every hook also receives the cursor handle, and may act through it at any
hook point.

### post_open
**Python** hook point name · new

The hook point that runs when a cursor's opening move is written (whoever
opened it, Q32), and for every cursor after a session loads, when each is at
rest with auto-step off. Only the lease holder runs it, as it runs every
hook. The base extension uses it to deliver inputs still
pending from before a restart.

### refuse
Declining what a hook point is about:
- an input hook returns **refuse** with a reason, or every input hook passes
  ("no extension handled this input"). Nothing is written, nothing moves,
  and `Session.input` raises the refusal to the frontend, which keeps the
  text;
- `pre_reply` returns nothing. The step ends: the remaining hooks do not run,
  nothing is written, the cursor does not move, and auto-step goes off.

`pre_tool` cannot refuse; it answers `Skipped` instead.

### handled
An input hook's outcome saying the input is dealt with, which ends the chain.
It may carry a body, with `copied_from` if the body repeats other nodes (the
base extension's delivery at rest): the driver writes it under `at` and moves onto it in an
input step, interrupting the step in place if the cursor is busy. A command
is handled with a `Config` body (`/model fast`). Without a body, the hook has
done all it meant to, such as holding a pending input.

### pass
An input hook's outcome handing the input, possibly rewritten, to the next
input hook. If every hook passes, the input is refused.

### answer
A pre hook returning the effect's result itself, such as a cached reply or a
mocked or blocked outcome, so that the effect does not run.

### claim
**Python**: driver memory · new

The driver's in-memory mark that a step is running a batch's calls: a tools
step, a resume, or a re-run's tools step. It is held only while that step
runs. The step takes it before running any call, then re-reads the run. A
step that finds it held waits (the holder is by definition running), then
re-reads and fast-forwards instead of running the calls again, so tools run
once (Q13). A move or `open_cursor` never takes a claim and is never refused
because of one. An interrupted step releases its claim once its `Interrupted`
results are written. The lease keeps claims within one process.

### event
**Rust** `Event` · **Python** `Event` · **retires** `AgentEvent`, `WireEvent`,
serve event, RPC event, bus channel, hook event dict

One type, tagged with a cursor, in three kinds: `node_appended`,
`cursor_moved`, `stream_delta`. Observers cannot block or change anything.
Every wire format is a projection of this one type. Events go to whoever is
subscribed, possibly nobody: no cursor waits on a particular frontend
connection. A cursor may wait on a persisted pending record that any
frontend can answer (Q22).

### abort
**Python** `Session.abort(cursor)` · **retires** `AbortSignal`, `AbortReason`,
abort-tree, `rollback`

An interrupt without a move, then auto-step off. The step moves onto its
records as usual: an aborted sample that
had produced output is written with `stop: interrupted`, its calls owe no
result, and the cursor moves onto it; one that had produced nothing writes
nothing. Aborted tools keep the results already written, and answer each
unanswered call `Interrupted`, moving onto each, which I4 requires. No post
hooks run after an abort (`model.md` §8, Q4). On a cursor at rest, abort only
turns auto-step off.

### interrupt
**Python**: none (driver behavior) · new

An abort that does not turn auto-step off. It happens to a busy cursor's step
when the cursor is moved from outside the step, when a hook of the step that
asked to move its own cursor returns, when an input hook handles input to it
with a body, or on abort. The driver cancels the running parts of the step
that have not asked to move, and writes the step's records under the nodes
they came from: a reply with the parts so far and `stop: interrupted` (after
`pre_reply`, so redaction holds), or an `Interrupted` result for every call
without one (not waiting behind post hooks). **The step's own move lands on
them as usual**, so they are on the line and committed. Only then does the
interrupter's move or body apply. An interrupt while `pre_reply` runs
cancels it and writes nothing, withholding the unredacted text. No other hook
of the step runs: a post hook already running finishes, and the rest do not
start. Only abort turns auto-step off (`model.md` §8).

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
  cursor. It cannot move a cursor or re-run, so it cannot interrupt the step
  running it (Q32).

### extension
**retires** bucket, `ExtensionHandlers`, `ExtensionRunner`, `ExtensionState`,
`ExtensionAPI` (as a 42-method surface)

A Python module that registers hooks and tools through the public API.
Extensions have no special standing in core (D3, D8). The base extension
ships in the default config and handles input. The first three built in
Phase 5 prove the model:
- **compaction**: summarises an old prefix and branches onto a shorter line
  made of the summary (a `User` node by the extension, Q30) and copies of the
  recent nodes, `Config` nodes included.
- **prune**: after the provider's cache expires, rewrites old tool outputs and
  thinking as derived nodes, then branches onto the rewrite.
- **sub-agent**: a tool that, through its handle, opens a cursor
  (`opened_by` = the calling cursor) on a line outside the parent's batch,
  for example a `User` task under a detached root or under a node above the
  batch. It runs that cursor and answers the call with a value whose
  `derived_from` lists the cursor's final node. Details are Phase 5.

### cursor handle
**Python** the first argument to every hook; a narrower one is handed to a
tool · new (the fourth revision called it `api`)

The cursor a hook runs for, bound to the hook's extension, so everything it
writes is authored `extension(name)` and every move it makes is
`by: extension(name)` without the hook saying so. Through it a hook may:
- read: `cursor.at`, `cursor.next`, `cursor.busy`, `cursor.auto_step`, and
  the session (`cursor.session`), including other cursors;
- act: append side nodes; `cursor.write(body, copied_from=…)`, which appends
  under `at` and moves onto it as one durable write; move or re-run this
  cursor or another; set auto-step; step; abort; open cursors; sample a walk
  and run a tool outside any step (compaction's cold summary on a detached
  root).

No hook point restricts it. A tool's handle is bound to the calling cursor
instead, and is narrower: it may read, append, sample a walk, open cursors,
and step or set auto-step on cursors it opened (a sub-agent), but not move or
re-run (Q32). The
exact surface is a Phase 4 decision. It is not the 0.15 `ExtensionAPI`.

### frontend
**retires** head, backend (as `Backend`/`TauBackend`/`RemoteBackend`), head
label

A program that drives sessions and shows events: print/json, REPL, RPC, TUI,
serve.
- A frontend keeps no queue. It hands input to `Session.input` at once, busy
  or not, and keeps the text only if the input is refused (D2). Queueing and
  "steer" behavior are extensions (the base extension, interrupt and send).
- A frontend may give input (`Session.input`), step a cursor
  (`Session.step`), go to any node (`Session.move`, which never starts a
  step), re-run a batch (`Session.rerun`), set auto-step
  (`Session.set_auto_step`), or abort (`Session.abort`) (Q15). Each of these
  except a go-to and abort resets the cursor's `max_steps` count.
- Every frontend sees every cursor. No cursor waits on a particular
  frontend connection: a frontend may switch cursors or sessions, or detach,
  and every active cursor keeps operating. A pending record a cursor waits on
  can be answered by any frontend, later, or from another device.

### driver
**Python** `tau_next.driver`

The asyncio loop that runs steps. It calls `next`, runs the hook points
around each effect, writes and moves, runs the post hooks, then steps again or
pauses. It runs input hooks on `Session.input`, `post_open` when a cursor
opens or loads, and interrupts a busy cursor's step when it is moved. It also
keeps the per-batch claim, so tools run once (Q13), and which cursors are
busy. It is the only place that awaits.

### store
**Rust** `tau_store` · **retires** session store, `SessionCatalog`, JMFTS
session log (until Phase 7)

The only Rust code that touches the filesystem. It keeps an append-only node
log and move log per session, behind a trait.

### lease
**Rust** `tau_store` · **retires** `SessionWriteLock`, `WriteAccess`,
`LockHolder`, lock sidecar

One `flock` per session, taken on its node log. The holder may write, and
only the holder runs cursors and hooks (`post_open` on load included); every
other process opens the session read-only and never runs a hook. A lease whose holder process is
dead is reclaimed.

---

## Testing (Phase 2)

### Harness
**Python** `tests/behavior/harness.py` · **retires** session log contract
tests

The protocol every behavior test drives. It mirrors the frontend operations:
create (`Session.create`) and load sessions, give input (`Session.input`),
step (`Session.step`), set auto-step
(`Session.set_auto_step`), abort (`Session.abort`), open cursors
(`Session.open_cursor`), go to a node (`Session.move`), re-run a batch
(`Session.rerun`), snapshot the tree and cursors, and read events. It has
one adapter for `tau_next` (in `tests/`) and one for τ 0.15 (in `parity/`).

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
| side entry, `SIDE_KINDS` | scratch: a node not on a committed walk |
| incomplete / finalize | nothing: nodes are written once, complete |
| leaf, default leaf | `at`, persisted through move records; no guessing |
| head cursor, acting cursor, `TURN_CURSOR` | every cursor is explicit, and hooks receive theirs |
| turn frame, `TurnFrame` | the `Config` node on the walk |
| turn (four senses) | step (D1); frontends count steps since last input |
| submission, multitask strategy | input hooks; queueing is the base extension's pending inputs, steering is an input hook (D2) |
| suspension, responder, `api.suspend` | auto-step off: the cursor pauses between steps. A hook that needs a person: open (Q22) |
| lane | nothing |
| head | frontend |
| backend | provider (model API) or frontend (driver of a session) |
| fork (in-session) | branch |
| compaction entry, splice | the compaction extension: a summary node plus a branch (D3) |
| extension lock, `request_user_action` | a `pre_input` hook that refuses input until satisfied |
| `input` hook | `pre_input` |
| `before_agent_start` hook | `pre_sample` (appends what it adds, I1) |
| `tool_call` hook | `pre_tool` |
| `tool_result` hook | `pre_result` |
| `turn_end`, `user_turn_end` hooks | `post_reply` (Q20) |
| `annotate`, `entry_written` hooks | event observers (`node_appended`) |
| session write lock | lease |
| `AgentSession`, runtime, catalog, `SessionManager` | session |
| `ExtensionContext` / `ctx` | the cursor handle, every hook's first argument |
| handler policy (gating/advisory/human) | nothing in core (Q9) |
