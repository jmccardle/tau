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
`finish_reason`.

### finish_reason
*spike* `Body::Assistant.finish_reason`, `Option<String>` · retires `stop_raw`

The provider's own finish string for a reply, verbatim (`"stop"`,
`"tool_calls"`, ...), kept beside the normalised `stop`. `None` when the
provider sent none: an interrupted reply, a failed sample, a fake.

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
*spike* `tree::Tree`, `Session::tree`; Python `tau_next.Tree` (`Session.tree`) · new

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

Core's refusals: `NoSuchNode`, `NoSuchCursor`, `NoSuchLabel`,
`DuplicateCall` (V2: a `Results` or `Assistant` node naming a call id twice; checked
on every append and write), `BadRecord` (load); a driver's: `WrongAnswer`
(an answer to no job: none is out, it is of another kind, or it
names a call that is not in the batch or already answered; nothing changes); and a frontend call's: `Busy`
(step on a busy cursor); a hook's: `NotInHook` (a hook's
move, write, or auto-step change on a cursor with no cursor hook job out), and
`NoSuchItem` (removing an item not in the scratchpad), and `NotPending` (a
write that delivers an item not pending for that cursor). Each becomes a typed Python exception of the same name in `tau-py`, and so does
each `tau_store::Error` variant except `Io` (the builtin `OSError`) and `Core` (the
inner error). Each subclasses the closest builtin (`_core.pyi` has them all);
there is no common base class.

## The scratchpad

### scratchpad
*spike* `tree::Scratchpad { items, next_item }`, `Session::scratchpad`,
`Session::items`; Python `CursorState.items` · retires submission queue, steering queue, follow-up queue

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

Per cursor, a counter bumped by `add_item` and nothing else: removing an item
(`remove_item`) or delivering one (`write`) is not a change, and wakes nothing. A
step compares it with the value at its wake (`Step::seen`) to tell whether work
was added to the scratchpad.

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
on that cursor writes nothing.

### machine
*spike* `machine::Machine` · new

The session plus every cursor's step: the frontend calls (`input`,
`step`, `go_to`, `set_auto`, `abort`), the extension calls (`open`,
`add_item`, `remove_item`, `append`, `set_label`, `remove_label`), a hook's
own-cursor calls (`hook_move`, `hook_write`, `hook_set_auto`), and
`next_job`/`answer` per cursor. Each extension call takes `hook: Option<CursorId>`,
the cursor whose hook makes it: it syncs on return unless a cursor hook job
is out for that cursor (then the phase's sync covers it); a frontend or the
driver passes `None`. `abort`, `next_job`, `answer` check the cursor:
`NoSuchCursor`. Holds a busy go-to until its aborted step
rests (`going`). Each frontend call syncs on return.
What `tau-py` exposes; `Session`'s own mutators are for core and tests.
Also `set_label` and `remove_label` (which sync on return), `Machine::loaded(session,
now)` (a machine for a session just opened from a store: load is a wake, so
every cursor starts a step), and the `Session::notices` the Python `take_notices` drains.

### Python session
`tau_next.Session` (`tau-py`) · new

The machine plus its optional store behind one `Mutex`, so the class is
`frozen` and free-threading safe. `Session.create(config, system, now,
directory=None)` makes one (in memory, or in a directory with a store);
`Session.load(directory)` opens one. Every call takes the lock with the
GIL released; every call that changes the session then gives the store its
queued records before it returns, so Python never saves. An in-memory
session drops them. A failed save is a crash: the call raises the OS error
and the session closes itself (see **close**). Cursor ids are checked
by core: an unknown one is `NoSuchCursor`. The value classes have a `__repr__`
(the Rust `Debug` text).
The calls are the machine's (**machine**) with these differences. `send` is
not one: it was `input` plus auto-step on, which a frontend does itself
(`input`, then `set_auto(cursor, True)`; `Driver.send` does both). The reads
are `tree` (a **Tree**, the nodes), `cursor(c)` (a **CursorState**, one
cursor), `cursors()` (the open cursors), `label(l)` and `labels()` (the labels),
and `take_notices()` (drains the notices). There is no whole-scratchpad read
(`scratchpad()` repeated every cursor's `items`) and no `dropped()`.

### CursorState
Python `tau_next.CursorState` (`Session.cursor(c)`) · new

A frozen snapshot of one cursor, all read under one lock: `at`, `auto`,
`busy`, `next`, `items`, `labels`, `moves`, `moves_since_step`. Later changes
do not reach it, so `busy` and `at` cannot disagree. It replaces the separate
per-cursor reads (`at`, `auto`, `busy`, `next`, `items`, `labels_of`,
`moves_of`, `moves_since_step`). The lists are Python objects only when read.
A snapshot costs a copy of the cursor's lists, so it grows with its
move count. Not the hook's `Cursor` handle, which acts; this only reads.

### close, Closed
`Session.close`, `tau_next.Closed` · new

Drops the store and releases the lease without writing what is not yet
synced, as a crash would. Every later call, a second `close` included,
raises `Closed`. A failed save leaves the session in the same state: the
store is gone with its lease, and reopening from disk is the recovery.

### Poisoned
`tau_next.Poisoned` · new

A call panicked inside the session's lock; the session raises it from then on.
Not core's: a binding-level guard so a core bug (a panic) is not followed by
more calls on possibly torn state.

### busy, at rest
*spike* `Machine::busy`, `Step::resting` · new

A cursor is busy while a `Step` exists for it that is not `resting()`.
Never stored; every cursor loads at rest.

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

What the step asks the driver to do: `Hooks { point, draft, ending }`,
`Sample`, `RunCalls { node, calls }` (`node`: the `Assistant` node holding the
batch; the driver refuses an empty one), `Pending` (calls outstanding),
`Rest`. Only waits are jobs; writes never are.

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
`Sampled { reply, error }`, `CallDone { call, outcome }`. Each is accepted only while the job it answers is out; any other answer is `WrongAnswer`.

### RunCall
*spike* `RunCall` · new

One call handed out in `Job::RunCalls`: `{id, tool, args_raw}`.

### draft
*spike* `Draft` · retires the plan's draft under `target`

What the current phase will commit: `{body, delivers, by}`. Carried in a
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
`tau_next.StepLimit` (`step_limit.py`) · extension, retires `Config::max_steps`
and `Session::taken`

`StepLimit(limit)`: an extension that guards loops where the cursor does move
each step (D1). It keeps its own count per cursor (core keeps none), read
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

What load does after replay: every cursor's auto-step off, and every
delivered item dropped (I7).

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

How a step runs; under D13 its data names a model by qualified
name instead of holding a full snapshot. `config_at(n)` is the nearest `Config`
on `walk(n)` (D5), recorded per node when it is added (`Session::configs`),
so it costs the same at any depth. Core reads nothing in it: `data` is opaque
to core and typed by the driver.

### ModelConfig, ConfigError
`tau_next.ModelConfig`, `tau_next.ConfigError` (`config.py`) · new

The typed reading of a `Config` node's `data`, minimal subset of the planned
config overhaul (plan.md, item 4): `model` (the name as its API spells it),
`base_url` (an http or https URL), and optional `api_key_env`, the *name* of
the environment variable holding the API key. `data` is a JSON object with
exactly those keys. `ModelConfig.parse(config)` validates; unknown keys, wrong
types, and missing keys raise `ConfigError` (a `ValueError`). `to_config()`
builds the `Config`. `api_key(environ)` reads the key at sample time; it is
never a field, so it reaches no node and no log (tested). It also carries
`provider` (default `"openai"`, the only one) and, for a literal `api_key` in
a file, `literal_api_key`, which `to_config` never writes. Groups, merging, and
files are `read_models`.

### api_key_env
`ModelConfig.api_key_env` · `Config` data key

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
`tau_next.config_layers` · D13 · new

`read_models(user, project, cli)` reads the layers (each may be absent; `cli`
is a mapping in the file's JSON shape), merges their raw trees (objects by
key; lists and scalars replace; `null` drops, including a key a group passes
down), then resolves every model. It raises `ConfigError` listing every
error; it finds and trusts no file. It returns a **`ModelCatalog`**: `models`
(qualified name to **`ResolvedModel`**: the `ModelConfig` and one **`Origin`**
per key, the layer, file path or `"cli"`, and the group that set it, `None`
for the model itself), `default_model`, `warnings`. `find(name)` returns the
qualified names matching a qualified or bare name (0, 1, or many: the caller
decides), `resolve(qualified)` the `ModelConfig`, `explain(qualified)` the
origins. `check_config(user, project, cli, environ)` returns every
**`ConfigProblem`** (severity `error` or `warning`; file, dotted path, and
for JSON syntax errors line and column) instead of raising: syntax errors,
duplicate keys, unknown keys with a suggestion, `:` in a name, a missing
`model` or `base_url`, wrong types, an unknown `provider`, a `default_model`
not in the config, 0.15's `backend` key, and as warnings a literal `api_key`
and (given `environ`) an unset `api_key_env` variable. A literal `api_key`
is held only in `ModelConfig.literal_api_key` (hidden from `repr`) and never
in a problem, origin, or catalog text.

### trusted directory, trusted.json
D13 · new

A project directory whose `.tau/` the user approved, listed in
`$TAU_HOME/trusted.json` with a hash of its `.tau/config.json`; a changed
hash is untrusted again. The project is the nearest `.tau/` upward from the
working directory, stopping at `$HOME`. Only a trusted directory's project config and `.env`
are read. Approval also makes `.tau/` the user's, mode `700`, with a
`.gitignore` of `*`.

### tau config check, tau trust
D13 · new

The first `tau` commands. `tau config check` validates the layers and reports
where each problem is; `--show <model>` prints each resolved value and its
source. `tau trust <dir>` approves a project directory, as the prompt does.

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

May move its cursor, write under `at`, set auto-step. Core allows these only
while a cursor hook job is out for that cursor (`in_hook`):
`Machine::hook_move`, `hook_write`, `hook_set_auto` raise `NotInHook` otherwise.
A handle kept from an earlier job is retired (see **retired handle**). They apply at once and sync
nothing; the phase's sync covers them. `set_auto(True)` in a step that is then
aborted leaves the cursor at rest: it wakes nothing.
Core checks no more than the tree's own rules (no hook edit limits).

### tool hook
`async def hook(node, call) -> str | None` · new

Runs per call, in parallel. A string makes that call `Err(reason)`.

### cursor handle, node handle, tree handle
Python `Cursor`, `NodeHandle`, `TreeHandle` · retires `ExtensionAPI`

What a hook acts through, bound to its extension (which fills in `by`). A
node handle is a cursor handle without the cursor's own operations. Every
call goes into Rust at once. Both are a **tree handle**, which holds what they
share. Reads: `get_node`, `lca`, `cursors`, `cursor_at`, `items_of`,
`moves_of`, `labels_of`, `label`, `all_labels`, `children`, `node_count`
(`Tree::children`, `node_count`, `labels`: children in append order; ids are
`0..node_count`). Writes that belong to no cursor: `append`
(`Machine::append`: a node under any parent, moving nothing), `open`,
`add_item` (an `ItemData.Extension` of this extension), `remove_item`
(`Machine::remove_item`: any item, wakes nothing), `set_label`,
`remove_label`. A cursor handle adds its own cursor's `at`, `auto`, `walk`,
`config`, `items`, `labels`, `moves(since_step=)`, `move(to)`,
`write(body, delivers)`, `set_auto(on)`, and `ask`. A handle's writes that
belong to no cursor pass the hook's cursor to core (`hook`), so they skip their
sync only while a cursor hook job is out for it. Not built: updating an item
in place, `show`, `tell` (`docs/plan.md`, roadmap row 8).

### Poke, Now
Python aliases in `handles.py` · new

`Poke = Callable[[], None]`: the driver's `_poke`, which tells the pump a handle
call (`open`, `add_item`) may have woken a cursor. `Now = Callable[[], int]`:
the driver's clock in milliseconds, so a handle fills in `time`. `Asker` is the
driver's question call (see **question**).

### question
`Driver.ask`, `respond`, `questions`, `Question` (driver memory; not core) ·
retires `request_user_action`

`await cursor.ask(target, question)` returns the response, or `None` if
declined. The hook awaiting it keeps its job out, so the cursor is busy and
core's busy rules apply (model.md §7). `Driver.abort` and `Driver.go_to`
decline the cursor's open questions.


## Effects and edges

### provider, model
retires backend (config key), `ProviderSpec`, `Model` descriptor,
`model_spec`

The **provider** is the Python adapter for one API family; it converts walks
to messages (`model.md` §10). The **model** is a name, as its API spells it.

### OpenAIProvider
`tau_next.providers.openai.OpenAIProvider` · new

The provider for OpenAI-compatible `/chat/completions` (llama.cpp, vLLM, OpenAI).
Reads `ModelConfig` from the sample's `config`. The request carries `model`,
`messages`, `stream: true`, and `tools` when the driver offers any (no
`tool_choice`, no `parallel_tool_calls`); the config has no sampling settings.
`tools` is `[{"type": "function", "function": {name, description, parameters}}]`,
built from each `Tool` for that sample only and recorded nowhere. Conversion: `System`
and `User` map to their roles; an `Assistant` to `assistant` with its text and
`tool_calls` (`args_raw` sent as the `arguments` string); a `Results` that
follows its batch to one `tool` message per outcome (`Err(e)` is `Error: e`,
`Interrupted` is `Interrupted`); a `Results` that answers no batch to one
`user` message of `[tool result <id>: <outcome>]` lines. `Thinking` parts are
not sent back (this API takes none). Calls that are not runnable, or a batch
with no `Results` after it, are `[tool call <id>: <tool> <args_raw>]` lines of
text (the `partial_calls: text` markup), followed by `[reply stopped: <why>]`
for `Length`, `Interrupted`, and `Error`. A `Results` whose ids differ from
its batch's is a conversion error (Q11). The response is read as SSE;
`reasoning_content` deltas become `Thinking`, `content` `Text`, `tool_calls`
`Call`s; each part is yielded once the next kind of part begins or the stream
finishes. `finish_reason` maps `stop` to `End`, `tool_calls` to `Tools`,
`length` to `Length`, `content_filter` to `Refused`; any other is an error. The
raw string is kept as `finish_reason` (via `Finish`). Fails with `SampleError`: a
non-200 status (the server's `error.message`), a transport error, malformed
SSE, an error object in the stream, a stream ending without `finish_reason`.
Parts begun before a failure are yielded first; a call cut off with no id or
no name is not a part, so its raw argument text and whatever id and name arrived
are appended to the `SampleError` message. Constructor: `transport`
(a test's `httpx.MockTransport`), `environ` (where keys are looked up),
`timeout`. `request(walk, config, tools)` returns the URL, headers, and body.

### tool, Tool
`tau_next.Tool` (protocol, `protocols.py`) · retires `AgentTool`, `ToolDefinition`, `ToolInfo`

The contract the driver and providers depend on: `name`, `description`,
`parameters` (a JSON Schema object for the arguments), and
`async call(args_raw: str) -> Outcome`. The tool gets the call's raw JSON text
and validates it itself, answering `Ok(text)` or `Err(message)`; an exception it
raises is `Err(str(e))` for that call. Breaking the return contract (answering
`Interrupted`, a `str`, `None`) is a programmer error, not a call's failure: the
driver raises `TypeError` and stops, surfacing through `Driver.check`. A malformed
`args_raw` is the tool's to answer (or a `pre_tool` hook's, which may rewrite
`call.args_raw` before the tool runs; D7); neither the driver nor core parses it.
The driver checks each tool when it is built (`ToolContractError`).
Any object with these attributes is a tool, including one written in another
language (a PyO3 class); `@tool` is one way to build one, and a test drives a
hand-written class through the `Harness`. Tools are the driver's, not the
history's: each sample is handed the driver's tools, and no `Config` or node
records them.

### tool (decorator), FunctionTool
`tau_next.tool`, `tau_next.FunctionTool` (`tools.py`) · new

`@tool` or `@tool(describe={...})` turns a sync or async function into a
`FunctionTool`, a `Tool`. Name: the function's `__name__`. Description: its
docstring, `inspect.cleandoc`'d. `parameters`: an object schema from the type
hints (`str`, `int`, `float`, `bool`, `list[T]`, `Literal` of all `str` or all
`int`, `T | None`); a parameter with a default is not `required`;
`additionalProperties` is false. `call` parses `args_raw`, checks the top-level
keys and each value's JSON type against the annotation, binds by keyword, runs
the function, and requires a `str` back. A sync function runs in a worker
thread, so it never blocks the loop; aborting the call stops waiting for it,
not the thread, which finishes and is discarded. A function that `functools.wraps`
an async one counts as async (`inspect.unwrap`). `FunctionTool.function` is the original function.

### describe
`tool(describe={"param": "text"})` · new

Parameter descriptions for the schema (`"description"` on that property). It
may cover some parameters or all; a name that is not a parameter is a
`ToolDefinitionError`. Docstrings are not parsed for parameter docs.

### ToolDefinitionError, ToolError
`tau_next.ToolDefinitionError` (a `ValueError`), `tau_next.ToolError` · new

`ToolDefinitionError`, at decoration, naming the function and parameter: no
docstring, a missing or unsupported annotation, `*args`/`**kwargs`,
positional-only parameters, a bad `describe`, an annotation that cannot
be resolved, and a default that does not fit its annotation (`s: str = None`
says to annotate `str | None`). `ToolError` is how `FunctionTool`
fails a call (malformed JSON, missing, extra, or wrongly typed arguments, an
exception from the function, a non-`str` return); `call` answers it as
`Err(message)`, so the model reads it, and it does not escape `call`. A tool
function may raise `ToolError("msg")` itself: the call answers `Err("msg")`
exactly, with no type prefix (any other exception is `Err("Type: msg")`).

### ToolContractError
`tau_next.ToolContractError` (a `TypeError`), `check_tools` (`protocols.py`) · new

Raised by `Driver.__init__` for an object that is not a `Tool`, naming it: a
missing or non-`str` `name` or `description`, `parameters` that is not a
`Mapping`, a `call` that is not callable, or two tools with one name.

### base tools: ReadTool, WriteTool, GrepTool, BashTool, base_tools
`tau_next.ReadTool` and the others, `tau_next.base_tools(cwd, ...)` (`base_tools.py`) over `crates/tau-tools`, bound by `tau-py/src/tools.rs` · new

Four `Tool`s whose logic is Rust (`tau-tools`: filesystem and process IO, no
PyO3) and whose `call` runs it on a worker thread with the GIL released, so
calls run in parallel (also on a free-threaded build) and never block the
loop. `base_tools(cwd)` returns all four. `cwd` (default: the current
directory, which must exist: `NotADirectoryError`) is what relative paths and
the shell's working directory are relative to. An expected failure (bad
arguments, a missing file, a nonzero exit, an overflow file that cannot be
written) is `Err(message)`; nothing is raised out of `call`. The arguments
are strict: an unknown or missing field is an `Err`.

| tool | parameters | does |
|---|---|---|
| `read` | `path`, `offset` (1-based line, default 1), `limit` (lines, default 4096) | A text file, verbatim, scanned in buffer-sized pieces (a file of one huge line costs the output window, not the file). `Err` for a missing file, a directory, a pipe, device or socket, an offset past the end, a binary file (NUL bytes; images are not supported). When `limit` or the byte limit stops it short of the end, a last line `[showing lines A-B; offset=N continues]` is appended; after a byte cut, `B` is the last kept line. If not even the first line fits, it is shown in part and the last line says the rest of it is only in the overflow file and `offset=N+1` continues after it. Opened with `O_NONBLOCK` and judged by `fstat` of the open file. Bytes that are not UTF-8 become U+FFFD and a line says so. |
| `write` | `path`, `content` | The whole file, UTF-8, atomically (a temp file beside it, then a rename), creating parent directories, following a symlink (dangling or not). Answers `Wrote N lines (B bytes) to <path>` (`1 line` in the singular). The new file gets the mode of the one it replaces before any content is written; the file and then the directory are synced. Hard links to the old file, its owner, group and ACLs are not kept. |
| `grep` | `pattern`, `path` (file or directory, default `cwd`), `files` (list; overrides `path`; searched as named), `ignore_case`, `glob` (a filter of a directory walk: the file name, or the path below the searched directory if it has a `/`; ignore rules still apply; not with `files` or a file `path`) | The Rust regex engine of ripgrep over a parallel, ignore-aware walk: `.gitignore` (in any tree, git repository or not), `.ignore`, hidden files and binary files are skipped. One `path:line:text` per match, path relative to the tool's working directory (not the searched path), sorted by path then line; a line is cut at 500 bytes (before decoding) with ` [+N bytes]`; a file with a line over 16 MiB (the searcher's heap limit) is not searched and is named in the could-not-search note. The search stops once the matches held reach the overflow cap, and a note says that some files were not searched. No match: `No matches found in N file(s)`. Notes add last lines: `[skipped N binary file(s)]` (the names, for files named explicitly), `[matching in F stopped at a NUL byte at offset N; ...]` (binary detection works a buffer at a time), an invalid-UTF-8 note, `[N paths could not be searched; first: ...]`. |
| `bash` | `command`, `timeout` (ms) | `bash -c command` in `cwd` as a session leader (no controlling terminal; `setsid` runs in the child before `exec`, so the session exists when the call starts), stdin closed, stdout and stderr on one pipe so they arrive in the order written. Exit 0: `Ok(output)`. Otherwise `Err(output + "\n\n" + "command exited with code N")` (or `killed by signal N`, `timed out after N ms and was terminated`). A `timeout` over the tool's maximum is an `Err` stating the maximum. The one `unsafe` block of the workspace is here (`pre_exec`); the workspace lint is `deny`, not `forbid`, for that. |

`write` has no output limit: its answer is one short line. The other three
share the *output limit*.

### output limit, max_output
`max_output=` on `ReadTool`, `GrepTool`, `BashTool`, `base_tools` · new

The most bytes of text the model gets from `read`, `grep` or `bash`: 10_000 by
default, at least 1. It bounds the *kept text*; the truncation note is extra.
Output at or under the limit is returned whole and writes no file.

### kept end
`Keep::Head`, `Keep::Tail` (`tau-tools/src/limit.rs`) · new

Which end of an over-limit output the model keeps. **`read` and `grep` keep the
head** (the start of a file, the first matches); **`bash` keeps the tail**
(errors and the result of a command land at the end). A judgment call of the
implementation, for John to overrule: it is one argument to `Sink::new` per
tool. Cuts fall on line boundaries and never inside a UTF-8 character. If not
one whole line fits (a single line longer than the limit), the cut is on a
character boundary instead and the note says so.

### overflow file, overflow_dir, max_overflow
`overflow_dir=` (default `<temp dir>/tau-<euid>`, from `std::env::temp_dir` and the effective uid, e.g. `/tmp/tau-1000`), `max_overflow=` (default 64 MiB) · new

When output passes the limit, the output (everything the tool would have
answered, not just what it kept) is written to `<overflow_dir>/<id>.out`, `id`
being 8 random characters from `a-z0-9`. The directory is created `0700` if
missing. It must be a directory (not a symlink), owned by the effective uid,
with `mode & 0o077 == 0`; otherwise the call is an `Err` naming the problem.
This is checked at construction (`lstat`; a directory that does not exist yet
is fine, `ValueError` otherwise) and again at use: the directory is opened
with `O_DIRECTORY|O_NOFOLLOW`, checked with `fstat` on that fd, and the file
is created relative to the fd with `openat`, so a path swapped in between
cannot redirect it. There is no fallback directory. Files are created
`0600` with `create_new`, so an existing file is never touched. For `read` the
output is the lines selected by `offset` and `limit`; for `grep`, all matches
(without a trailing newline); for `bash`, stdout and stderr interleaved,
without the exit-code trailer. The file holds at most `max_overflow` bytes
(at least `max_output`); bytes and lines past it are still counted, and the
note says the file holds only the first part. If the directory or file cannot
be created or written, the call is an `Err` saying so, and `bash` terminates its
command. Files are never deleted by the tool.

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
`tau_next._core.BashRun` · new

A started `bash` command. `Bash.start(args_raw)` spawns it as a session leader
(so its process group id is its pid) and never raises (a bad call is a
`BashRun` whose `wait` answers `Err`); `wait()` blocks, GIL released, and answers
the `Outcome`; `kill()` does not block and may be called from any thread, any
number of times. Ending a command, by `kill()` or by the timeout, is: SIGTERM to
the process group and every other process of the session, then SIGKILL after the grace period (`kill_grace_ms=`, default
2000), so the shell, its pipeline and anything it backgrounded die together.
The shell is waited for without being reaped (`waitid` with `WNOWAIT`, so its
pid cannot be reused), the rest of its group is killed, and only then is it
reaped; the SIGKILL is repeated, scanning `/proc/*/stat` for the session id (Linux only), until no live process of the session is left. A process that has called `setsid` itself leaves the session and is out of reach. A `killpg`
failure other than "no such process" is an `Err`, never a panic. `timeout_ms=`
is the timeout of a call that gives none (30_000); `max_timeout_ms=` (600_000)
the most a call may ask for. `BashTool.call` kills when it is cancelled and waits
for the command to be gone before the `CancelledError` goes on, and gives each
call a thread of its own. A process of a session of its own that holds
the output pipe open is not killed: the pipe is read for 250 ms after the shell
exits and then closed, and the answer says "a process outside the command's
group still holds the output open" (an `Ok` with a note after a normal exit).
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

What `read`, `grep` and `bash` share: `max_bytes` (the output limit),
`dir` (the overflow directory) and `max_overflow` (the cap of the overflow
file). Built by `Limits::new` (default directory) or `Limits::in_dir`; refuses
a zero limit, a cap below the limit, and an existing directory that is a
symlink, not ours, or open to others.

### Finished
`tau_tools::limit::Finished` · new

What a `Sink` answers: `text` (the kept text with its notes), `kept_lines`
(lines shown; a cut-short line counts as one), `truncated` (the output passed
the limit) and `cut_line` (the one line shown is only the start of a line). Used
by `read` to word its continuation hint.

### Cancel
`tau_next._core.Cancel` · new

A flag. `Read.run` and `Grep.run` take one; `ReadTool.call` and `GrepTool.call`
set it when they are cancelled, and the read loop and the grep walk (and its
searches, per match) check it and stop with an `Err`.

### Read, Write, Grep, Bash (`_core`)
`tau_next._core.Read` and the rest · new

The native engines under the base tools: `__new__(cwd, max_output, overflow_dir, max_overflow)`
(`Write`: `cwd` only; `Bash`: also `timeout_ms`, `max_timeout_ms`, `kill_grace_ms`), `name`, `description`,
`parameters_json`, and `run(args_raw, cancel) -> Outcome` (`Write`: no `cancel`; `Bash`: `start`). A bad
constructor argument raises `ValueError`. Python code uses the `Tool` classes
above instead.

### Finish
`tau_next.Finish` (`protocols.py`) · new

What a provider yields last: `Finish(stop, reason)`, the normalised `Stop` and
the provider's own string (`None` if it has none). The driver builds the reply
draft with both.

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

Performs jobs and answers each with its result and the time. Decides nothing. The only
place that awaits. Built from a session, a provider, tools, extensions, and a
clock. Its frontend calls (`input`, `send`, `step`, `go_to`, `set_auto`,
`abort`, `open`, `add_item`, `respond`) run on the loop thread, call the
session, then poke the pump. Extensions are a list given to the constructor,
in hook order; how `config_at` will name them is open.

### pump
`Driver._pump` · new

The one coroutine per session that calls `next_job` and `answer` (D11 rejects a
task per cursor). Loop: for each cursor with no effect out that is busy or
due a poll, take its next job and start it as a unit; then await the queue and
answer with what arrives. `next_job` is not idempotent, so it is never called while a
cursor has an effect out, except after a `CallDone` (expecting `Pending`).

### unit
`Driver` (`units`) · new

One task performing one effect: a hooks job, a sample, or one call of a batch.
It reports `(cursor, Answer)` to the pump's queue when done.
A cancelled unit reports nothing.

### Outstanding
`tau_next.Outstanding` · new

A cursor's effect in flight: its units, and what abort needs to cancel them
(the sample, the calls). At most one per cursor.

### poke, poll
`Driver._poke`, `Driver._poll` · new

A **poke** wakes the pump after a frontend call. A **poll** is one extra
`next_job` for a cursor whose effect just ended, even if it now rests: a go-to
waiting on its aborted step applies inside that `next_job`, and `busy` does
not show it.

### Subject
`tau_next.Subject` · new

The mutable holder a cursor hook receives: `draft` (`pre_input`,
`pre_sample`, `pre_reply`), `node` (`post_reply`, `post_result`), `ending`
(`post_step`). The hook's edits to `draft` are what the hook answers with.

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

### rest
`Driver.rest(cursor)` · new

Await until the cursor is at rest: not busy, no effect out, no poll due.
Re-raises what an effect or the pump died of. A cursor held by an open
question or a gate does not rest. The pump wakes its waiters after each pass.

### Clock, SystemClock
`tau_next.Clock`, `tau_next.SystemClock` · new

`now() -> int` (milliseconds). The driver reads time only through it, so tests
move time themselves.

### Question
`tau_next.Question` · new

An open question held by the driver: `{target, text, future}`. `respond` fills
the oldest one for a cursor with the response; `None` declines.

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
scratchpad file), `Core` (the log does not replay). `save` consumes the
store and returns it on success: after an `Io` error it is gone with its
lease, as after a crash, and the session is opened again. **JMFTS** (Phase 7) is a replica of the log,
never the store of record.

### torn tail, dropped
*spike* `Store::dropped`

Whatever follows the log's last good `Sync` on open: a record cut short,
one failing its checksum, records never synced, damage. Open cuts it (so a
session goes on from its last sync) and keeps the bytes as
`dropped-<offset>` in the session directory; `dropped` is how many. Rust only:
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
directory with a `Driver` over the fakes. `Harness.create`/`load`; a client's
calls (`input`, `send`, `step`, `abort`, `go_to`, `set_auto`, `respond`, as
client `term`); `settle`; `crash`; snapshots (`snapshot`, `bodies`, `nodes`,
`notices`).

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
`gate_after` parts; each request's walk ids and config recorded), scripted
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
`tau_next.trace` · new

One recording of spans, written as a Chrome trace file (Perfetto,
chrome://tracing) by `tracing-chrome`. `trace.start(path)`, `trace.stop()`,
and `trace.capture(path)` (both as a context manager). At most one runs per
process. The driver's own spans, `trace.span(kind, name)` (`hook`, `sample`,
`tool`, `driver`), are timed on the native clock and merged in by `stop`, one
track per asyncio task; they are no-ops when no trace runs.

### trace_to, stop_trace, trace_clock
`tau_next._core` · new

The native half of a trace: `trace_to(path)` installs the global subscriber
the first time and starts recording, `stop_trace()` returns when the file is
complete, `trace_clock()` is microseconds on the spans' own clock. Not for
callers other than `tau_next.trace`.

### TraceError
`tau_next.TraceError` (`tau_next._core.TraceError`, a `RuntimeError`) · new

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

Retired during design review: committed, uncommitted, boundary, cycle, base
extension, `deliver`, `combine`, returned input, run, batch states,
journal, journal item, `idempotent`, `Result`, fast-forward, claim,
`Session.rerun`, `Skipped`, refuse/handled/pass, restating `Config`, label on
the move record, `post_open`, `post_input`, interrupt, scratch, the throttle
(`THROTTLE`, `Pos::Throttle`, `Job::Wait`, `Answer::Waited`, `Clock.sleep_until`).
