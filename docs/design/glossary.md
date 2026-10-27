# Glossary

Status: **for review (Phase 1).** Rule 2: no type, field, or module-level
concept exists without an entry here. One term, one meaning.

Each entry gives the identifier (Rust in `tau_core`, Python in `tau_next`),
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
`Call { id, tool, args_raw }`.

### stop
*spike* `Stop` · retires `stopReason`, `AgentEndReason`

Why a reply ended: `End`, `Tools`, `Length`, `Refused`, `Interrupted`,
`Error`. Only `Tools` makes calls runnable. The provider's own string is
`stop_raw` (not yet spiked).

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

A node answering a batch: one `(call id, outcome)` per call, in call order,
written at the join. Final; another answer is a sibling (Q10).

### outcome
*spike* `Outcome` · retires `is_error`, `Skipped`

`Ok(value)`, `Err(error)`, or `Interrupted` (an abort stopped the call or
kept it from starting; the tool may or may not have run).

### author
*spike* `Author` · retires `source`, `SubmissionSource`

Who wrote a node, or who asked for a move: `Frontend(client)`, `Provider`,
`Tools`, `Core`, `Extension(name)`. Filled in by the driver from the caller,
recorded only, never used for control flow (Q7).

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
Spike fields: `nodes`, `at` (cursor → node), `auto`, `items`, `next_item`,
`changes`, `moves`, `outbox`, `notices`. `Session::create(config, system,
time)` writes the root `Config` and a `System` node and opens the first
cursor (Q5).

### root, detached root
a view · retires session header, side-completion sessions

A node with no parent; always `Config` (V1). Any root after the first is
**detached**: one-off work outside the conversation.

### side node
a view · new

A node no cursor's walk passes through. Nothing marks it.

### walk
*spike* `Session::walk` · retires fold, path, splice

`walk(n)`: the nodes root → … → `n`, inclusive. Nothing spliced or filtered.
Its last node is its **tip**; seen as a path through the tree, it is a
**line**.

### LCA
not yet spiked · new

Lowest common ancestor of two nodes; none across trees.

### append
*spike* `Session::append` · retires `append_at`, write group

Writes a node under any parent (or none) without moving any cursor. Checks
V1 and V2 only (I5).

### validate
not yet spiked

V1 (a root is `Config`) and V2 (a `Results` node names distinct calls) over a
whole walk, for tests. `append` checks each node (`Session::check`).

### Error
*spike* `tree::Error`

Core's refusals: `NoSuchNode`, `NoSuchCursor`, `RootNotConfig` (V1),
`DuplicateCall` (V2). Each becomes a typed Python exception in `tau-py`.

## The scratchpad

### scratchpad
*spike* `Session::items` (field and method) · retires submission queue,
steering queue, follow-up queue

A session's persistent, modifiable list of items, off the tree. A
**cursor's scratchpad** is the items addressed to it, oldest first. Anyone
may add, update, or remove any item; adding one wakes its cursor.

### item
*spike* `Item`, `ItemId`, `ItemData` · new

`{id, to, data}`. `data` is `Input { client, text }` or
`Extension { name, data }`. Ids are never reused (`next_item`).

### input
*spike* `ItemData::Input` · new

What a frontend gives a cursor: the one item kind core knows. The input
phase commits it as a `User` node that delivers it.

### pending input
a view · new

A cursor's input items not yet delivered or removed.

### changes
*spike* `Session::changes` · new

Per cursor, a counter bumped by `add_item` and `remove_item`. A step compares
it with the value at its wake (`Step::seen`) to tell whether the scratchpad
changed.

### notice
*spike* `Session::notices` · new

A message to one client: `(client, text)`. Core adds one when `pre_input`
refuses an input; extensions send them with `cursor.tell`.

## Position

### cursor
*spike* `CursorId`; `{id, at}` · retires head cursor, acting cursor,
`TURN_CURSOR`, `TurnFrame`

An insertion point. Its only flag is auto-step. It carries no settings and
no queue. Cursors do not close (Q12).

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
items. Every commit, and a hook's `cursor.write`.

### commit
*spike* (in `Step::feed`) · retires the plan's commit

A phase writing its draft with `Session::write`.

### cursor.moves
not yet spiked

A cursor's move records; with `since_step=True`, only those since this
step's `pre_step` began (Q44).

### go to
not yet spiked · retires `navigate_tree`, `Session.rerun`

A frontend's move. Turns auto-step off and wakes the cursor (Q39). On a busy
cursor: refused while a hook runs or a question is open, else abort then
move.

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
not yet spiked · new

A unique string naming one cursor; a cursor may have several. Set, moved,
and removed by **label records** `{label, cursor?}`.

### busy, at rest
a view · new

A cursor is busy while a `Step` exists for it that is not `resting()`.
Never stored; every cursor loads at rest.

### locked
not yet spiked · new

A cursor with an open question. Input, go-to, and step on it are refused.

## The step

### step
*spike* `Step` · retires turn (four senses), loop turn, agent run,
submission, cycle

One pass of the phase loop for one cursor, `pre_step` to `post_step`.
`Step::wake(session, cursor, asked, now)` starts one; `next_job` and `feed`
drive it; `abort` and `resting` are its other calls. Private state:
`cursor`, `pos`, `asked`, `aborted`, `committed`, `started`, `seen`,
`draft`, `error`, `ending`, `batch`.

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
`PostReply`, `Tools`, `Calls`, `PostResult`, `PostStep`, `Throttle`, `Rest`.

### next
*spike* `tree::next`, `Session::next`, `Next` · retires `LoopPosition`

What a walk needs: `Sample` (last message-bearing node is `User` or
`Results`), `RunTools` (a batch), or `Wait`. Never looks below `at`.

### job
*spike* `Job` · new

What the step asks the driver to do: `Hooks { point, draft, ending }`,
`Sample`, `RunCalls(calls)`, `Pending` (calls outstanding), `Wait { until }`
(the throttle), `Rest`. Only waits are jobs; writes never are.

### Fed
*spike* `Fed` · new

What the driver feeds back: `Hooks { stop, draft }`, `HookFailed(error)`,
`Sampled { reply, error }`, `CallDone { call, outcome }`, `Waited`.

### RunCall
*spike* `RunCall` · new

One call handed out in `Job::RunCalls`: `{id, tool, args_raw}`.

### draft
*spike* `Draft` · retires the plan's draft under `target`

What the current phase will commit: `{body, delivers, by}`. Carried in a
`Hooks` job; the edited one comes back in `Fed::Hooks`. A halt discards it;
an abort commits it. Python subjects: input draft, sample draft, reply
draft, call draft (Phase 4).

### answer
A pre hook supplying the effect's result so it does not run: a reply from
`pre_sample`, an outcome from `pre_tool`. Not a question's answer.

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

### throttle
*spike* `THROTTLE` (250 ms), `Job::Wait` · new

A step that committed nothing starts the next no sooner than 250 ms after it
started.

### wake
*spike* `Step::wake` · new

Start a step on a cursor at rest: an item added, opening, load, a go-to,
`Session.step`, auto-step turned on.

### auto-step
*spike* `Session::auto` · retires step mode, suspension, `api.suspend`,
responder

A cursor's only flag. On: phases run and steps follow one another while
there is work. Off: a woken cursor runs only `pre_step` and `post_step`. Goes
off on abort, halt, failure, go-to, `max_steps`, and load.

### max_steps
not yet spiked · `Config` field

The most committing steps a cursor takes without a frontend acting. Reaching
it turns auto-step off (D1).

### partial_calls
not yet spiked · `Config` field

`text` (default) or `omit`: how a never-run call is rendered on resend (Q27).

### abort
*spike* `Step::abort` · retires `AbortSignal`, abort-tree, `rollback`

Esc. Auto-step off, no later phase; returns whether the driver must cancel
the effect in flight. Never cancels a hook.

### resume
What a crash mid-batch leaves: the cursor at rest at the unanswered batch,
every outcome lost (D4). Input goes under it; a step re-runs it.

### reconcile
*spike* `Session::reconcile` · new

What load does after replay: every cursor's auto-step off, and every
delivered item dropped (I7).

### sample, context, message
retires completion, model call, `ExtensionContext`/`ctx`, the `context`
hook, message entry, `AgentMessage`

A **sample** is one provider request over the **context**, `walk(at)`, with
`config_at(at)` (I1). A **message** is the provider's wire unit, never stored.

### config
`Body::Config`, `config_at` · retires config entry, `CONFIG_KEYS`,
`Settings`, model entry

A full snapshot of how a step runs. `config_at(n)` is the nearest `Config`
on `walk(n)` (D5).

## Hooks

### hook
retires the 0.15 hooks, handler policy, chain

A Python function an extension registers at one hook point. `None` carries
on; a string stops. Hooks at a point run in `config_at(at)` order.

### hook point
*spike* `Hook` (the seven cursor points) · new

`pre_step`, `pre_input`, `pre_sample`, `pre_reply`, `post_reply`,
`post_result`, `post_step` (cursor hooks, each a `Job::Hooks`), and
`pre_tool`, `pre_result` (tool hooks, run by the driver inside `RunCalls`).

### cursor hook
`async def hook(cursor, subject) -> str | None` · new

May move its cursor, write under `at`, set auto-step.

### tool hook
`async def hook(node, call) -> str | None` · new

Runs per call, in parallel. A string makes that call `Err(reason)`.

### cursor handle, node handle
Python `Cursor`, `NodeHandle` · retires `ExtensionAPI`

What a hook acts through, bound to its extension (which fills in `by`). A
node handle is a cursor handle without the cursor's own operations. Every
call goes into Rust at once. Surface is Phase 4's.

### question
not yet spiked · retires `request_user_action`

`await cursor.ask(target, question)`; the cursor is locked until it is
answered or declined.


## Effects and edges

### provider, model
retires backend (config key), `ProviderSpec`, `Model` descriptor,
`model_spec`

The **provider** is the Python adapter for one API family; it converts walks
to messages (`model.md` §10). The **model** is a name, as its API spells it.

### tool
retires `AgentTool`, `ToolDefinition`, `ToolInfo`

A name, an argument schema, an `exclusive` flag, and an async function to an
outcome.

### extension
retires `ExtensionRunner`, `ExtensionState`, bucket

A Python module registering hooks and tools. No special standing in core.

### frontend, client
retires head, `Backend`, `RemoteBackend`

A program that drives sessions and shows events (`tau serve` first). A
**client** is one connection to it, with an id.

### event, status
not yet spiked · retires `AgentEvent`, `WireEvent`

One type in eight kinds (`model.md` §9); observers cannot change anything.
**Status** is the state an extension shows every frontend (`cursor.show`),
driver memory.

### driver
Python `tau_next.driver` · new

Performs jobs and feeds results back with the time. Decides nothing. The only
place that awaits.

### record
*spike* `tree::Record` · new

What must be made durable, in order, queued in `Session::outbox`:
`Write { node, mv }` (a node and its move, one record), `Move`, or
`Scratchpad` (rewrite the scratchpad file).
Queued in the **outbox** (`Session::outbox`) until the store writes them.

### store
`tau_store` · retires session store, `SessionCatalog`

The only Rust code that touches the filesystem: one record log and one
scratchpad file per session (D12). **JMFTS** (Phase 7) is a replica of the
log, never the store of record.

### lease
retires `SessionWriteLock`

One `flock` per session. Only the holder writes and runs steps.

## Testing and scaffold

### Harness
`tests/behavior/harness.py`

The protocol every behavior test drives, shaped like `tau serve`.

### FakeProvider, FakeTool
The only fakes allowed (rule 4): scripted samples, scripted outcomes.

### probe
`tau_core::scaffold::Probe`, `tau_next.Probe`, `ProbeError`, `echo`

Phase 0's boundary probe. Deleted in Phase 3.

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

Retired during design review: committed, uncommitted, boundary, cycle, base
extension, `deliver`, `combine`, returned input, run, batch states,
journal, journal item, `idempotent`, `Result`, fast-forward, claim,
`Session.rerun`, `Skipped`, refuse/handled/pass, restating `Config`, label on
the move record, `post_open`, `post_input`, interrupt, scratch.
