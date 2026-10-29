# Glossary

Status: **accepted (Phase 1 closed 2026-10-27).** Rule 2: no type, field, or module-level
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

A node answering a batch: one `(call id, outcome, by)` per call, in call
order, written at the join, `by: Tools`. Each outcome's `by` is `Tools` if the
tool ran unedited, else the extension whose `pre_tool` answered it or whose
`pre_result` last edited it (fed in `CallDone`). Final; another answer is a
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
Spike fields: `nodes`, `configs` (per node, its nearest `Config`), `at`
(cursor → node), `auto`, `items`, `next_item`, `changes`, `moves`, `outbox`,
`notices`. `Session::create(config, system,
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
*spike* `Session::lca` · new

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

Core's refusals: `NoSuchNode`, `NoSuchCursor`, `NoSuchLabel`,
`RootNotConfig` (V1), `DuplicateCall` (V2), `BadRecord` (load); a driver's: `WrongFeed`
(a feed that answers no job: none is out, it is of another kind, or it
names a call that is not in the batch or already answered; nothing changes); and a frontend call's: `Busy`
(step on a busy cursor), `Locked` (input, go-to, or step with a question
open), `HookRunning` (go-to while a cursor hook runs). Each becomes a typed Python exception of the same name in `tau-py`, and so does
each `tau_store::Error` variant except `Io` (the builtin `OSError`) and `Core` (the
inner error). Each subclasses the closest builtin (`_core.pyi` has them all);
there is no common base class.

## The scratchpad

### scratchpad
*spike* `tree::Scratchpad { items, next_item }`, `Session::scratchpad`,
`Session::items` · retires submission queue, steering queue, follow-up queue

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
*spike* `Session::moves_of`, `Step::moves` · new

A cursor's move records; with `since_step=True` (`Step::moves`), only those
since the step woke (Q44).

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
*spike* `Session::set_label`, `remove_label`, `label`, `labels_of`,
`Record::Label` · new

A unique string naming one cursor; a cursor may have several. Set, moved,
and removed by **label records** `{label, cursor?}`; setting a label already
on that cursor writes nothing.

### machine
*spike* `machine::Machine` · new

The session plus every cursor's step: the frontend calls (`input`, `send`,
`step`, `go_to`, `set_auto`, `abort`), the extension calls that wake a
cursor (`open`, `add_item`), the question lock (`ask`, `answered`), and
`next_job`/`feed` per cursor. `open` and `add_item` sync on return unless a
cursor hook is running (then the phase's sync covers them). `abort`, `ask`,
`answered`, `next_job`, `feed` check the cursor: `NoSuchCursor`. Holds a busy go-to until its aborted step
rests (`going`). Each frontend call resets `max_steps` and syncs on return.
What `tau-py` exposes; `Session`'s own mutators are for core and tests.
Also `set_label` and `remove_label` (which sync on return), `Machine::loaded(session,
now)` (a machine for a session just opened from a store: load is a wake, so
every cursor starts a step), and `Session::take_notices` (drain `notices`).

### Python session
`tau_next.Session` (`tau-py`) · new

The machine plus its optional store behind one `Mutex`, so the class is
`frozen` and free-threading safe. `Session.create(config, system, now,
directory=None)` makes one (in memory, or in a directory with a store);
`Session.load(directory, now)` opens one. Every call takes the lock with the
GIL released; every call that changes the session then gives the store its
queued records before it returns, so Python never saves. An in-memory
session drops them. After a failed save (`Store::failed`) every call that
changes the session is refused up front with `Failed`. Cursor ids are checked
by core: an unknown one is `NoSuchCursor`. The value classes have a `__repr__`
(the Rust `Debug` text).
`cursors()` lists the open cursors; `take_notices()` drains the notices.

### close, Closed
`Session.close`, `tau_next.Closed` · new

Drops the store and releases the lease without writing what is not yet
synced, as a crash would. Every later call, a second `close` included,
raises `Closed`.

### Poisoned
`tau_next.Poisoned` · new

A call panicked inside the session's lock; the session raises it from then on.
Not core's: a binding-level guard so a core bug (a panic) is not followed by
more calls on possibly torn state.

### busy, at rest
*spike* `Machine::busy`, `Step::resting` · new

A cursor is busy while a `Step` exists for it that is not `resting()`.
Never stored; every cursor loads at rest.

### locked
*spike* `Machine::locked`, `ask`, `answered` · new

A cursor with an open question. Input, go-to, and step on it are refused
(`Error::Locked`); abort is allowed and leaves the question open. Core
counts open questions per cursor; the driver holds their content.

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
`Sampled { reply, error }`, `CallDone { call, outcome }`, `Waited`. Each is accepted only while the job it answers is out; any other feed is `WrongFeed`.

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
`Session.step`, auto-step turned on. Load: `Machine::loaded` wakes every
cursor of the loaded session (auto-step is off, so `pre_step` and `post_step`
only); `Session.create` does not wake its first cursor.

### auto-step
*spike* `Session::auto` · retires step mode, suspension, `api.suspend`,
responder

A cursor's only flag. On: phases run and steps follow one another while
there is work. Off: a woken cursor runs only `pre_step` and `post_step`. Goes
off on abort, halt, failure, go-to, `max_steps`, and load.

### max_steps
*spike* `Config::max_steps`; the count is `Session::taken` · `Config` field

The most committing steps a cursor takes without a frontend acting. Reaching
it turns auto-step off (D1).

### partial_calls
not yet spiked · `Config` field

`text` (default) or `omit`: how a never-run call is rendered on resend (Q27).

### abort
*spike* `Step::abort` · retires `AbortSignal`, abort-tree, `rollback`

Esc. Auto-step off, no later phase; returns whether the driver must cancel
the effect in flight, or its sleeper in the throttle (the restart is cancelled,
so a later `Waited` is a `WrongFeed`). Never cancels a hook.


### in_hook
*spike* `Step::in_hook` · new

True while a cursor hook job is out and not yet fed. Core cannot see tool
hooks inside `RunCalls`; a go-to then aborts and the hook finishes (Q34).
### resume
What a crash mid-batch leaves: the cursor at rest at the unanswered batch,
every outcome lost (D4). Input goes under it; a step re-runs it.

### reconcile
*spike* `Session::reconcile` · new

What load does after replay: every cursor's auto-step off, and every
delivered item dropped (I7).

### load
*spike* `Session::load(records, scratchpad)` · new

Reload (I7): replay the log's records in order, take the scratchpad file,
`reconcile`. Pure; the store reads the files. A record that cannot apply (a
node id or cursor out of sequence, a move from a node its cursor is not at)
is `Error::BadRecord(position)`.

### sample, context, message
retires completion, model call, `ExtensionContext`/`ctx`, the `context`
hook, message entry, `AgentMessage`

A **sample** is one provider request over the **context**, `walk(at)`, with
`config_at(at)` (I1). A **message** is the provider's wire unit, never stored.

### config
*spike* `tree::Config`, `Body::Config`, `Session::config_at` · retires config
entry, `CONFIG_KEYS`, `Settings`, model entry

A full snapshot of how a step runs. `config_at(n)` is the nearest `Config`
on `walk(n)` (D5), recorded per node when it is added (`Session::configs`),
so it costs the same at any depth. Core reads only the typed fields it needs
(`max_steps`); `data` holds the rest, opaque to core and typed by the
driver.

## Hooks

### hook
retires the 0.15 hooks, handler policy, chain

A Python function an extension registers at one hook point. `None` carries
on; a string stops. Hooks at a point run in `config_at(at)` order.

### hook point
*spike* `Hook`, Python `tau_next.Hook` (the seven cursor points) · new

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
the lock is *spike* (`Machine::ask`); the rest is the driver's · retires
`request_user_action`

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
Python `tau_next.driver.Driver` · new

Performs jobs and feeds results back with the time. Decides nothing. The only
place that awaits. Built from a session, a provider, tools, extensions, and a
clock. Its frontend calls (`input`, `send`, `step`, `go_to`, `set_auto`,
`abort`, `open`, `add_item`, `answer`) run on the loop thread, call the
session, then poke the pump. Extensions are a list given to the constructor,
in hook order; how `config_at` will name them is open.

### pump
`Driver._pump` · new

The one coroutine per session that calls `next_job` and `feed` (D11 rejects a
task per cursor). Loop: for each cursor with no effect out that is busy or
due a poll, take its next job and start it as a unit; then await the queue and
feed what arrives. `next_job` is not idempotent, so it is never called while a
cursor has an effect out, except after a `CallDone` (expecting `Pending`).

### unit
`Driver` (`units`) · new

One task performing one effect: a hooks job, a sample, one call of a batch, or
a throttle sleeper. It reports `(cursor, Fed)` to the pump's queue when done.
A cancelled unit reports nothing.

### Outstanding
`tau_next.Outstanding` · new

A cursor's effect in flight: its units, and what abort needs to cancel them
(the sample, the calls, whether it is the throttle). At most one per cursor.

### poke, poll
`Driver._poke`, `Driver._poll` · new

A **poke** wakes the pump after a frontend call. A **poll** is one extra
`next_job` for a cursor whose effect just fed, even if it now rests: a go-to
waiting on its aborted step applies inside that `next_job`, and `busy` does
not show it.

### Subject
`tau_next.Subject` · new

The mutable holder a cursor hook receives: `draft` (`pre_input`,
`pre_sample`, `pre_reply`), `node` (`post_reply`, `post_result`), `ending`
(`post_step`). The hook's edits to `draft` are what is fed back.

### CallDraft
`tau_next.CallDraft` · new

The call draft a tool hook receives: `{id, tool, args_raw, outcome?}`. Setting
`outcome` in `pre_tool` answers the call; in `pre_result` it edits the outcome.
The hook that changed the outcome is its `by`.

### SampleError
`tau_next.SampleError` · new

What a provider raises to fail a sample. Parts it yielded first are kept: the
reply is committed with `stop: Error` and `Sampled.error` set. With none,
`Sampled { reply: None, error }`. Any other exception from a provider or the
driver is a bug and surfaces through `Driver.check`.

### Clock, SystemClock
`tau_next.Clock`, `tau_next.SystemClock` · new

`now() -> int` (milliseconds) and `async sleep_until(t)`. The driver reads time
only through it, so tests move time themselves.

### Question
`tau_next.Question` · new

An open question held by the driver: `{target, text, future}`. `answer` fills
the oldest one for a cursor and calls `Session.answered`; `None` declines.

### record
*spike* `tree::Record` · new

What must be made durable, in order, queued in `Session::outbox`:
`Write { node, mv }` (a node and its move, one record), `Move`, `Label`,
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
scratchpad file), `Failed` (an earlier write failed; open again), `Core`
(the log does not replay). **JMFTS** (Phase 7) is a replica of the log,
never the store of record.

### torn tail, dropped
*spike* `Store::dropped`

Whatever follows the log's last good `Sync` on open: a record cut short,
one failing its checksum, records never synced, damage. Open cuts it (so a
session goes on from its last sync) and keeps the bytes as
`dropped-<offset>` in the session directory; `dropped` is how many.

### lease
*spike* in `Store` · retires `SessionWriteLock`

An exclusive `flock` on the log, held while a `Store` lives. Only the holder
writes and runs steps; a second open is `Error::Leased`.

## Testing

### Harness
`tests/behavior/harness.py`

What every behavior test drives, shaped like `tau serve`: a session in a
directory with a `Driver` over the fakes. `Harness.create`/`load`; a client's
calls (`input`, `send`, `step`, `abort`, `go_to`, `set_auto`, `answer`, as
client `term`); `settle`; `crash`; snapshots (`snapshot`, `bodies`, `nodes`,
`notices`).

### settle
`Harness.settle` · new

Yield to the loop until the driver is quiet (no report or poke queued, no busy
cursor without an effect out) and every unit is **parked**, on two passes in a
row. Never sleeps and never advances the clock; re-raises what a unit died of.
Gives up with an error after 10,000 passes.

### parked
A unit waiting on something only a test releases: a held `Gate`, a
`FakeClock` sleeper, or an open question. `settle` compares the driver's unit
count with the parked count.

### crash
`Harness.crash` · new

Cancel every task without feeding, close the session unsynced, reopen it from
disk, start a new driver on the same fakes.

### Gate
`tests/behavior/fakes.py` · new

A latch a fake waits at. `held` counts waiters; `release()` lets them and all
later arrivals through.

### FakeProvider, FakeTool, FakeClock
The only fakes allowed (rule 4): scripted samples (`Reply`, `Fail`; a `Gate` after
`gate_after` parts; each request's walk ids and config recorded), scripted
outcomes (calls recorded), and time that moves only on `advance`.

### Recorder
`tests/behavior/fakes.py` · new

Not a fake: a real extension that records the hooks it is run at (`points`,
`subjects`, `endings`) and returns the string a test gives it for a point.

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
