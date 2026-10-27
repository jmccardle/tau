# Glossary

Status: **for review (Phase 1), fourth revision.** On sign-off this file and `model.md`
supersede `docs/plan.md` §1.

Rule 2: no type, field, or module-level concept exists without an entry here.
Each entry has one meaning. If a word needs a second meaning, it needs a
second word.

Identifiers are proposals until Phase 3 builds them. Rust names live in
`tau_core`, and Python names in `tau_next`. "Retires" lists the τ 0.15 terms
(from the as-built review §7) that the entry replaces. "New" means 0.15 had no
counterpart. The full retirement table is at the end.

Questions marked **Q*n*** are open and listed in `model.md`, Open questions.

---

## The tree

### node
**Rust** `Node`, `NodeId` · **Python** `Node`, ids are `int` (Q8) ·
**retires** entry, message entry, custom entry, side entry, finalize

One immutable record in a session:
`{id, parent?, time, by, body, copied_from?, derived_from}`. It is written
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
| `Skipped(reason)` | deliberately not run: a `pre_tool` hook declined it ("blocked by …"); or input arrived at an unstarted batch and the call never started (`Skipped("superseded by input")`, Q17) |
| `Interrupted` | no result was written: the call was aborted, or a crash or abort came before its result was written. The tool may or may not have run (D4) |

### author
**Rust** `Author` · **Python** `Node.by` · **retires** `source:"agent"`,
submitter, `SubmissionSource`

Who wrote a node:
- `frontend`: a person's input.
- `provider`: a sample.
- `tools`: a result from running a call.
- `core`: the session's first root and its `System` node (`Session.create`),
  I6's restated config, a `sample_error` record, and every result core writes
  itself: `Skipped("superseded by input")`, `Interrupted`,
  `Err("invalid arguments")`, `Err("hook error")`.
- `extension(name)`: a hook, including a root a hook creates (a detached
  root).

A node's author is whoever produced its content as written. A hook's answer or
rewrite makes that hook's extension the author, and so does a copy. The author
is recorded and never used for control flow (Q7). No API caller supplies it:
the driver fills it in from the caller, because hooks get a session handle
bound to their extension and frontends call through the frontend API. A move
record's `by` works the same way.

### time
**Rust** `Node::time` · **Python** `Node.time` · **retires** `timestamp`

The wall-clock time a node was appended. The driver supplies it; core never
reads a clock. The plan calls this field `at`, which collides with
`Cursor.at` (Q1).

### copied_from
**Rust** `Node::copied_from` · **Python** `Node.copied_from` · new

The node whose body this node repeats *verbatim*, under a different parent.
Compaction's copied tail produces these. A copy has its own parent and
therefore its own context (I1).
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

An insertion point: `{id, at}`, plus the driver's in-memory `auto_step` flag.
Every write a step makes is followed by a move of its cursor onto it.
- Several cursors may share a session, and even a node.
- A cursor carries no settings: those come from the walk (`config_at`).
- A cursor carries no queues: those belong to frontends (D2).
- Cursors do not close (Q12).

### open_cursor
**Rust** `Session::open_cursor(at, opened_by?)` (internal) · **Python**
`Session.open_cursor(at)` · **retires** `spawn`, `newest`, cursor registration

Creates a cursor and appends its opening move. The driver fills in
`opened_by` from the caller, as it fills in `by`: a hook on cursor A that
opens a cursor records A, and so does a tool running for cursor A (a
sub-agent); a frontend leaves it empty. The node must be committed and its
walk must validate. A node on a batch or inside one of its runs fast-forwards
to the end of the run, as a move does (I3). Opening a cursor never starts a
step and never takes a claim.

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
- a copy a hook left unfinished.

Scratch is persisted like any node and stays inspectable. It is just not on
anyone's conversation, until a move reaches it. Apart from an unreached I6
restatement, which is reused rather than duplicated, core never writes a node
and then abandons it, so scratch is otherwise an extension's work or a record
of a failure.

### side node
**Python**: none (an ordinary node) · new

A node a hook appends off the cursor's line: a `Custom` note under a written
node, an alternative, or work on a detached root. It is scratch unless a move
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
4. apply I6 for a go-to, a hook's move, or a re-run, and refuse if a
   restatement would be needed under an unstarted or partial batch;
5. append any new restating `Config` and the move record together, set
   `at := to`, and emit `cursor_moved`.

A move never takes a claim, and is never refused because of one.

A move comes from a step (onto what it wrote, or a fast-forward the step makes
when it re-reads a run), a frontend's go-to, a post hook, or a re-run. Rust's `by` argument is internal: no caller supplies it.
The driver fills it in from the caller, because hooks get a session handle
bound to their extension and frontends call through the frontend API.

A refused move changes nothing and raises to whoever asked. A hook that
catches its own refused move has handled it, and auto-step is unchanged. A
step's own move is never refused.

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
- Frontends call it directly, and post hooks through `api`. A post hook's
  re-run does not nest: like a hook's move, it takes effect when the post
  hooks finish, and the driver then runs the tools step, which counts toward
  `max_steps`. Pre hooks and tools cannot re-run.

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
frontend steps it or gives input. Input under a named parent is a go-to to
that parent followed by an input step. A hook's move, by contrast, happens inside a step, and the driver
carries on after it per auto-step. To run a batch again, use a re-run.

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
**Python** `Session.input(cursor, text, parent=None)` · new (0.15's
submission is retired under step)

What a frontend gives a cursor. With `parent`, it is a go-to to that parent
(fast-forward, I6, and the same refusals) followed by an input step. Input
lands only where `next != RunTools`; if a step is running, the driver keeps
one input and refuses a second (D2). It resets the cursor's `max_steps`
count.

### step
**Python** `Session.step(cursor)` · **retires** loop turn, agent run, user
turn, submission, turn (all four senses, D1)

`Session.step` runs one step on a cursor, whatever its auto-step, and resets
its `max_steps` count. One cycle for one cursor (`model.md` §7). Its phases run in order:
1. pre hooks on the effect (`pre_sample`, `pre_tool`);
2. the **effect**: a provider request, tools, or nothing for input;
3. pre hooks on the node (`pre_input`, `pre_reply`, `pre_result`);
4. **write** the node and **move** the cursor onto it;
5. post hooks on the written node (`post_input`, `post_reply`,
   `post_result`).

Then the driver evaluates `next` again.

There are three **step kinds**:
- `input`: a frontend gives input (`Session.input`);
- `sample`: `next` is `Sample`;
- `tools`: `next` is `RunTools`. A tools step writes one result per call as
  each call finishes, and moves onto each one.

A step either moves its cursor onto what it wrote, or (refused, or a sample
that failed with no output) leaves `at` where it was and turns auto-step off.

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
moving its cursor (refused, failed), on abort, and when the cursor has taken
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
4. Answer the rest `Interrupted` (D4), which runs no hooks.

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
LCA.

Fields named so far:
- **`max_steps`** (Q20): the most steps a cursor takes without the frontend
  acting on it. The driver counts in memory, per cursor, and resets the count
  whenever the frontend gives input, steps the cursor, re-runs a batch, or
  turns auto-step on. When the count reaches `config_at(at).max_steps`,
  auto-step turns off and the cursor pauses. `next` does not look at it, and
  nothing is stored.
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
**Python** `async def hook(api, cursor, …)`, one signature per hook point ·
**retires** the 0.15 hooks (`input`, `before_agent_start`, `tool_call`,
`tool_result`, `turn_end`, `user_turn_end`, `annotate`, `entry_written`, …),
handler policy, suspension site, chain

A Python function an extension registers at one **hook point**. At each hook
point, hooks run in the order their extensions are listed in the config. Each
receives what the previous one returned. There are three sorts:
- A **pre hook on an effect** (`pre_sample`, `pre_tool`) shapes a request
  before the effect runs. It may rewrite the request, or **answer** it: the
  effect and the remaining hooks at that point are skipped, but the node's pre
  hooks still run (Q18).
- A **pre hook on a node** (`pre_input`, `pre_reply`, `pre_result`) sees a
  node before it is written, and may rewrite it. `pre_input` and `pre_reply`
  may also refuse it.
- A **post hook** (`post_input`, `post_reply`, `post_result`) sees the node
  after it is written and the cursor has moved onto it, before `next` is
  evaluated. It may append side nodes, and it may move the cursor (checked
  like a go-to) or re-run a batch. Either ends that node's post hooks and
  takes effect when they finish: the driver then evaluates `next` and carries
  on per auto-step, unlike a go-to; for a re-run, it runs the re-run's tools
  step. `post_result` may move only on the result that completes its batch,
  because the step holds the batch's claim until then. In a tools step, post hooks run one result at a time per
  cursor. Post hooks do not run after an
  abort, and an abort during one cancels it and skips the rest.

Any hook may also use `api` to append nodes elsewhere, for example on a
detached root. Nodes and moves written by hooks trigger no hooks, but a step
a hook causes (after a "continue", or a re-run's tools step) runs hooks as
usual.

Results core writes (`Err("invalid arguments")`, `Interrupted`,
`Err("hook error")`, `Skipped("superseded by input")`) run no hooks either:
there is no tool output to rewrite. They are written and moved onto; when one
completes a batch, the driver evaluates `next` directly.

In a tools step, each call's `pre_result` runs as soon as its tool finishes,
concurrently. Only write, move, and `post_result` are serialised per cursor,
in completion order.

An uncaught exception from a hook ends its hook point, turns auto-step off,
and is shown to the frontend. If the effect has not run, it does not. A call
whose hook raised is answered `Err("hook error")` by core, with any raw
output withheld (`model.md` §7).

### hook point
**Python** the registration name · new

One of:

| hook point | receives | returns |
|---|---|---|
| `pre_input` | the input (a `User` body) | a body to write: the input, possibly rewritten, or another body when a command is **handled** (`/model fast` returns a `Config`). Or nothing: **refused**, and nothing is written |
| `post_input` | the written input node | nothing; it may append, move the cursor (a synthetic sibling), or re-run a batch |
| `pre_sample` | the context tip | a context tip (the same, or a node it appended: a reminder, a `Config`, a compacted line), or a reply (answered) |
| `pre_reply` | the reply | the reply, its parts possibly rewritten (never its `stop`), or nothing (refused, nothing written) |
| `post_reply` | the written `Assistant` node | nothing; it may append, move the cursor ("continue" under any reply that is not a batch, Q20), or re-run a batch |
| `pre_tool` | one call | the call (possibly rewritten), or an outcome (answered) |
| `pre_result` | the call as it ran, and its outcome | an outcome of the same kind, with its content possibly rewritten (Q18). It cannot drop one: every call is owed a result (I4) |
| `post_result` | the written result node | nothing; it may append, or, on the completing result only, move the cursor or re-run a batch |

### refuse
Ending a step from `pre_input` or `pre_reply` by returning nothing. The
remaining hooks do not run, nothing is written, the cursor does not move, and
auto-step goes off. `pre_tool` cannot refuse; it answers `Skipped` instead.

### handled
Input that `pre_input` turned into a different body, such as a command turned
into a `Config` node. That body is written and moved onto in place of a `User`
node.

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
because of one. The lease keeps claims within one process.

### event
**Rust** `Event` · **Python** `Event` · **retires** `AgentEvent`, `WireEvent`,
serve event, RPC event, bus channel, hook event dict

One type, tagged with a cursor, in three kinds: `node_appended`,
`cursor_moved`, `stream_delta`. Observers cannot block or change anything.
Every wire format is a projection of this one type.

### abort
**Python** `Session.abort(cursor)` · **retires** `AbortSignal`, `AbortReason`,
abort-tree, `rollback`

Cancel a cursor's in-flight sample or tools, then turn its auto-step off. An
aborted sample that had produced output is written with
`stop: interrupted`, its calls owe no result, and the cursor moves onto it;
one that had produced nothing writes nothing. Aborted tools keep the
results already written, and answer each unanswered call `Interrupted`,
moving onto each, which I4 requires. No post hooks run after an abort, and an
abort while a post hook is running cancels it and skips the remaining post
hooks (`model.md` §8, Q4).

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
- an async function from arguments to an outcome. It may also receive an
  `api` handle bound to the calling cursor: whatever it appends is authored
  `tools`, and a cursor it opens has `opened_by` = the calling cursor. It
  cannot move that cursor or re-run.

### extension
**retires** bucket, `ExtensionHandlers`, `ExtensionRunner`, `ExtensionState`,
`ExtensionAPI` (as a 42-method surface)

A Python module that registers hooks and tools through the public API.
Extensions have no special standing in core (D3, D8). The first three
(Phase 5) prove the model:
- **compaction**: summarises an old prefix and branches onto a shorter line
  made of the summary (a `User` node by the extension, Q30) and copies of the
  recent nodes, `Config` nodes included.
- **prune**: after the provider's cache expires, rewrites old tool outputs and
  thinking as derived nodes, then branches onto the rewrite.
- **sub-agent**: a tool that, through its `api` handle, opens a cursor
  (`opened_by` = the calling cursor) on a line outside the parent's batch,
  for example a `User` task under a detached root or under a node above the
  batch. It runs that cursor and answers the call with a value whose
  `derived_from` lists the cursor's final node. Details are Phase 5.

### api
**Python**: the first argument to a hook; also handed to a tool

What a hook may do besides returning its value: read the session, append
nodes (for example on a detached root), sample a walk, run a tool, open a
cursor, and, from a post hook, move its cursor or re-run a batch. The handle
is bound to the hook's extension, so everything it writes is authored
`extension(name)` without the hook saying so. A pre hook steers only through
its return value. A tool's handle is bound to the calling cursor instead: it
may read, append, and open cursors, but not move or re-run. The exact
surface is a Phase 4 decision. It is not the 0.15 `ExtensionAPI`.

### frontend
**retires** head, backend (as `Backend`/`TauBackend`/`RemoteBackend`), head
label

A program that drives sessions and shows events: print/json, REPL, RPC, TUI,
serve.
- Input queues and "steer" behavior live here (D2).
- A frontend may give input (`Session.input`, optionally under a named
  parent), step a cursor (`Session.step`), go to any node (`Session.move`,
  which never starts a step), re-run a batch (`Session.rerun`), set auto-step
  (`Session.set_auto_step`), or abort (`Session.abort`) (Q15). Each of these
  except a go-to and abort resets the cursor's `max_steps` count.
- Every frontend sees every cursor.

### driver
**Python** `tau_next.driver`

The asyncio loop that runs steps. It calls `next`, runs the hook points
around each effect, writes and moves, runs the post hooks, then steps again or
pauses. It also keeps the per-batch
claim, so tools run once (Q13). It is the only place that awaits.

### store
**Rust** `tau_store` · **retires** session store, `SessionCatalog`, JMFTS
session log (until Phase 7)

The only Rust code that touches the filesystem. It keeps an append-only node
log and move log per session, behind a trait.

### lease
**Rust** `tau_store` · **retires** `SessionWriteLock`, `WriteAccess`,
`LockHolder`, lock sidecar

One `flock` per session, taken on its node log. The holder may write; every
other process opens the session read-only. A lease whose holder process is
dead is reclaimed.

---

## Testing (Phase 2)

### Harness
**Python** `tests/behavior/harness.py` · **retires** session log contract
tests

The protocol every behavior test drives. It mirrors the frontend operations:
create (`Session.create`) and load sessions, give input (`Session.input`,
optionally under a named parent), step (`Session.step`), set auto-step
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
| submission, multitask strategy | input steps; queues and steering are a frontend's (D2) |
| suspension, responder, `api.suspend` | auto-step off: the cursor pauses between steps. A hook that needs a person awaits the frontend (Q22) |
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
| `ExtensionContext` / `ctx` | the `api` and `cursor` hook arguments |
| handler policy (gating/advisory/human) | nothing in core (Q9) |
