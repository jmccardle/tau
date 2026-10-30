# Model

Status: **accepted (Phase 1 closed 2026-10-27).** This file and `glossary.md`
supersede the original plan's model section (git history). Change it when
code forces a change, not before. Terms are defined
in `glossary.md`; D1–D12 are in `decisions/`; **Q*n*** are at the end.

**The spike is the reference.** `crates/tau-core/src/tree.rs` (the tree) and
`step.rs` (the step machine), driven by `crates/tau-core/tests/step.rs`,
implement §1–§8 below. Where this document and the spike differ, the spike is
right until a review says otherwise. Anything marked **not yet spiked** is
specified here and not built.

The idea: **the conversation is a tree of immutable nodes; everything else is
a pointer into it, a function of it, or an item in the scratchpad.** Nothing
is state unless it is a node, a move record, a label record, or a scratchpad
item, so a reload is a replay (I7).

## Rulings

- **Leave it to an extension.** Core's defaults are simplicity and
  predictability. Behavior that someone might want differently (resuming
  after a crash, queueing input, retries) is an extension's, and core only
  exposes the state an extension needs to decide. Loading wakes every cursor
  with auto-step off, so only `pre_step` and `post_step` run (Q39); a
  crash-resume extension turns its own cursor's auto-step back on.
- **No hook edit limits.** Core checks the state hooks leave, not what they
  changed: after a point's hooks run, it acts on the draft and cursor as they
  are, refusing only what would make the tree invalid (V2). A `pre_input`
  draft that delivers nothing writes its `User` node and leaves the input
  pending; the input phase offers each input once, so it cannot spin, and
  `after`'s rules (§6) decide whether another step runs: it moved the cursor,
  so it does; the next one offers nothing and rests. A draft a hook leaves as
  `None` commits nothing.
- **A step is one pass of a fixed loop**: `pre_step`, then the input, sample,
  and tools phases (each if it applies), then `post_step` (§6).
- **Every phase has one shape**: pre hook on the effect, the effect, pre hook
  on the draft, commit under `at` with its move as one record, post hook.
- **Two hook shapes.** Cursor hooks `(cursor, subject) -> str | None` may move
  their cursor. Tool hooks `(node, call) -> str | None` run per call in the
  driver and may not. `None` carries on; a string halts the step, or gives
  that call `Err(reason)` (§7).
- **Calls stay inside the `Assistant` node; one `Results` node per batch**,
  written at the join, in call order (D7).
- **`next` reads only `walk(at)`.** The cursor never looks at children; going
  to a batch and stepping re-runs it (§5).
- **Config is the nearest `Config` on the walk** (D5).
- **Input is the one item kind core knows.** The input phase applies whenever
  input is pending, even at an unanswered batch; holding input back is an
  extension's (D2).
- **No crash recovery for calls in core.** A batch's outcomes are in memory
  until the join. After a crash the cursor rests at the unanswered batch:
  input goes under it, a step re-runs it (D4).
- **Persistence is not a job.** Writes happen inside the Rust call that makes
  them (D11). **Post hooks run on synced data:** a phase's writes, its pre
  hooks' included, share one sync before its post hook; `post_step`'s writes
  are synced before the step rests or restarts. JMFTS is a replica (D12).
- **Hooks act only on their own cursor.** Cursors talk through the
  scratchpad.
- **Abort cancels effects, never hooks** (§8).
- **Nobody passes `by`.** The driver fills it in from the caller.

## 1. Stored objects

```
Node   { id, parent?, time, by, body, delivers, copied_from*, derived_from* }

body   = System(text)
       | User(text, images*)
       | Assistant { parts, stop, finish_reason?, meta? }
       | Results(outcomes)                  [(call id, outcome, by)], in call order
       | Config { data }                  data: opaque to core
       | Custom { kind, data }

part    = Text(text) | Thinking(text, signature*) | Call { id, tool, args_raw }
outcome = Ok(value) | Err(error) | Interrupted
stop    = End | Tools | Length | Refused | Interrupted | Error
by      = Frontend(client) | Provider | Tools | Core | Extension(name)

Move   { cursor, from?, to, by, time, opened_by* }
Label  { label, cursor? }                    no cursor: the label is removed
Item   { id, to, data }   data = Input { client, text } | Extension { name, data }
```

`*` marks what is **not yet spiked**. `x?` is optional.

- A node is written once (I2). `delivers` lists the item ids it consumed; a write
  may deliver only items pending for its own cursor (`NotPending` otherwise).
  `copied_from` lists nodes it repeats verbatim; `derived_from` lists nodes
  whose walks its content was computed from.
- `args_raw` is the argument text exactly as the model produced it, never
  re-serialised.
- A **cursor** is `{id, at}`; `at` is the `to` of its latest move. A move with
  no `from` opens a cursor. Ids (node, cursor, item) are session-local
  integers, never reused (Q8).
- A **label** maps a unique string to one cursor; a cursor may have several.
  Label records set, move, and remove them; load replays them.
- An **item** is addressed to one cursor (`to`). Input items come from a
  frontend client; every other item belongs to the extensions. No item
  reaches the model (I1).
- Every root is a `Config` node, so `config_at` is always defined: a root is
  made only by `append_root`, which takes a `Config` (no refusal to have).
- Core is pure: the driver passes `time` in; core assigns ids.

## 2. Batches

The `Call` parts of an `Assistant` node with `stop == Tools` and at least one
call are a **batch** (`tree::calls`). Calls in any other reply are not
runnable; the provider renders them as text (§10).

- A batch is answered by one `Results` node, written at the join when every
  call has an outcome, under the batch, in call order.
- The tool, or a `pre_tool` hook, answers an `args_raw` it cannot use; core
  and the driver do not parse it (D7, 2026-10-29).
- Each `Results` node is final. Going back to the batch and stepping writes a
  sibling `Results` (Q10). Two cursors at one batch each run it (Q13).

## 3. Derived views

| view | definition |
|---|---|
| `walk(n)` | root → … → `n` by `parent`, inclusive |
| `LCA(a, b)` | the deepest node on both walks; none across trees (`Tree::lca`) |
| `config_at(n)` | the body of the nearest `Config` on `walk(n)` (`Tree::config_at`) |
| `next` | §6 |
| a cursor's scratchpad | its items, in the order added (`Session::items`) |
| pending input | the input items in it |

Nodes no cursor's walk passes through are **side nodes**: an extension's
notes, a detached root's work, `sample_error` records, a reply someone went
back from. Any node may be gone to.

## 4. Invariants

| # | statement | enforced by |
|---|---|---|
| I1 | A sample sends exactly `walk(at)` with `config_at(at)`, as `at` stands when the sample starts. Items and `Custom` nodes never reach the model. A reply is committed under `at` as it stands at the commit. | `FakeProvider` records each request (Phase 2); `pre_reply_move_chooses_where_the_reply_lands`; proptest `invariants_hold_under_any_driver` (the sample phase starts only where `next` is `Sample`; a reply lands under `at` at its commit) |
| I2 | No node changes after it is appended. Pre hooks shape a draft before its commit. | no setters; frozen pyclass; proptest `invariants_hold_under_any_driver` (no node changes; the queued `Write`s equal the nodes) |
| I3 | `at` is always a node of its session. | unit test: a move to an unknown id is refused (`Error::NoSuchNode`); proptest `invariants_hold_under_any_driver` (go-to, hook moves, and opens to unknown ids are refused and change nothing) |
| I4 | The tools phase writes one `Results` per batch it runs, one outcome per call, in call order. Holds by construction. | `batch_answers_in_call_order_then_samples_again`; proptest `invariants_hold_under_any_driver` (outcomes answered in any order, aborts mid-batch; every batch run is answered once) |
| I5 | Append checks only types: **V2** a `Results` node's outcomes, and an `Assistant` node's calls, name distinct call ids. A root is made by `append_root` (a `Config` by construction), so there is no V1; replay refuses a logged root that is not a `Config` as `BadRecord`. Wire legality is the provider's conversion (Q11). | `Error::DuplicateCall`; unit tests; proptest `appends_check_only_types` (refused only for V2, any other shape under any node taken; a refusal changes nothing) |
| I6 | Retired (D5). | — |
| I7 | Reload is replay: state is a pure function of nodes, moves, labels, and items. No heuristics, no repair. | proptests `reload_at_any_sync_equals_the_state_at_that_sync`, `an_older_scratchpad_never_redelivers`, `a_torn_tail_opens_as_the_last_sync_before_it`; `tree::load` |

**Not persisted (I7):** auto-step (every cursor loads with it off), busy (every
cursor loads at rest), open questions (driver memory), shown state, a
step's in-memory batch outcomes, events. Load drops every item some node
`delivers`; anything after the log's last good `Sync` (a torn tail) never
happened. That is the
whole of `reconcile`.

## 5. Move

A move is the only way `at` changes, and it checks only that `to` is a node
(I3). It never looks below `to`. A move to `at` writes nothing.

| source | API | `by` |
|---|---|---|
| a commit | `Session::write` (append under `at` + move, one record) | `Core`, or the hook's extension for `cursor.write` |
| a cursor hook | `cursor.move(to)` → `Session::move_to` | `Extension(name)` |
| a frontend's go-to | `Session.move(cursor, to)` | `Frontend(client)` |

- **A hook's move applies at once**; later hooks see the new `at`. Each hook
  sees the step's moves with `cursor.moves(since_step=True)` (`Step::moves`).
- **A go-to** turns auto-step off and wakes the cursor, whose step then runs
  only `pre_step` and `post_step` (Q39). On a busy cursor it aborts the step
  and applies after `post_step` (`Machine::go_to`), whatever is out: an
  effect is cancelled, a hook (cursor or tool) finishes (Q34), and an open
  question is declined (§7). It is refused only for an unknown cursor or
  node.
- **Re-run** is go-to the batch's `Assistant` node, then step. **Branching**
  is moving off `walk(at)` and not below it; the branch forms at the next
  commit. Going to an ancestor is going back.

## 6. Steps

### `next`

`tree::next` skips `Config` and `Custom` nodes from the end of the walk, then:

| last message-bearing node | `Next` |
|---|---|
| `User` or `Results` | `Sample` |
| an `Assistant` with a batch | `RunTools` |
| anything else, or none | `Wait` |

`Session::next(cursor)` scans back from `at` without building the walk.

### The machine

`step::Step` is one cursor's step, sans-IO (D11). The driver loops:

```
step = Step::wake(session, cursor, asked, now)     asked: a frontend's Session.step
loop:
    job = step.next_job(session)                   Hooks | Sample | RunCalls | Pending | Rest
    perform it, then step.answer(session, answer, now)  Hooks | HookFailed | Sampled | CallDone
abort: step.abort(session) -> must the driver cancel the effect in flight?
```

Positions, in order (`Pos`): `PreStep`, `Input`, `PreSample`, `Sample`,
`PreReply`, `PostReply`, `Tools`, `Calls`, `PostResult`, `PostStep`, then
`Rest`.

| phase | applies when | jobs, in order | commits |
|---|---|---|---|
| input | input is pending | `Hooks(PreInput, draft)` per item, oldest first; the items pending when the phase begins, each offered once | one `User` per item, `delivers: [item]`, by `Frontend(client)`; an item a hook already delivered is skipped, and a hook that delivers the offered one itself must also leave the draft `None` (below): otherwise the commit's `delivers` names an item that is gone, `write` refuses it (`NotPending`), and the step fails |
| sample | `next == Sample` | `Hooks(PreSample)`, `Sample`, `Hooks(PreReply, draft)`, `Hooks(PostReply)` | one `Assistant` |
| tools | `next == RunTools` | `RunCalls(calls)`, `Pending` until every `CallDone`, `Hooks(PostResult)` | one `Results` at the join, by `Tools`; each outcome has its own `by` |

- **A phase applies** only if the step was not aborted, auto-step is on or
  the step was `asked`, and its condition holds for `at` at the start of the
  phase. A `post_reply` hook that moves the cursor decides where the tools
  phase looks.
- **Drafts.** A `Hooks` job carries the phase's `Draft {body, delivers, by}`;
  `Answer::Hooks { draft }` returns the edited one, and core writes it under
  `at` with `Session::write`. A draft of `None` (a hook cleared it) commits
  nothing and the step carries on; it is not a failure. From `pre_sample`, a
  draft is a reply that **answers** the sample: no `Sample` job, straight to
  `pre_reply`. A step *committed* only through core's own writes (a phase's
  draft, the join) or through a hook's `cursor.write`; either moves the cursor,
  and the move is what `after` looks at (§6).
- **`RunCalls`** hands the driver the batch. For each call the driver runs
  `pre_tool`, the tool, and `pre_result`, and answers `CallDone` in any order.
  The step holds each outcome in its slot; when every slot is filled, the
  join writes `Results` in call order.

### Ending

`Hooks(PostStep)` carries the `Ending`:

| `Ending` | when | auto-step |
|---|---|---|
| `Done` | the phases ran, or none applied | unchanged |
| `Halted(reason)` | a cursor hook returned a string | off |
| `Failed(error)` | a sample failed, or a cursor hook raised | off |
| `Aborted` | `Step::abort` | off |

A string from `post_step` only turns auto-step off.

### After `post_step`

John's ruling, the rule the section below implements:

> "If auto_step is on, and the cursor doesn't move, turn auto_step off. Hooks'
> interaction with the draft is for that core behavior: sample the LLM (the
> LLM's output is the draft and becomes the new node and the cursor's `at`) or
> execute the tools (the tool results are the drafts and become the new node
> and the cursor's `at`). The cursor should never spin in place — the mental
> model of the cursor should be a state machine, and if its state results in
> not moving then we should model that it's never going to move. … We can't
> govern arbitrary behavior extensions are granted, so the best defense for
> the cursor is to not try more than once."

A step that ended `Done` has **work** if auto-step is on and `next != Wait`, or
an input is pending. It **moved** if its cursor has a move record since the
step woke (`Step::moves`, the same records as `cursor.moves(since_step=True)`:
a commit, the join, and a hook's `cursor.move` or `cursor.write` all leave one;
a draft a hook threw away leaves none). Core keeps no other flag; the question
is answered from the move records alone.

| ending | work | moved | after `post_step` |
|---|---|---|---|
| `Done` | yes, auto-step on | yes | the next step starts at once |
| `Done` | yes, auto-step on | no | auto-step goes off; `Rest` |
| anything else | | | `Rest` |

A cursor never tries the same thing twice. A hook that declines every input,
or throws every reply away, gets one step: the cursor does not move, auto-step
goes off, and the input stays pending. Auto-step turned off this way is the
same state as any other turn-off (`Session::auto`, read by a frontend, or by
an extension through `cursor.auto`); `post_step` itself runs before core
decides, so it still sees auto-step on.

An item added to the cursor's scratchpad since this step woke
(`Session::changes`; removing or delivering an item is not a change) is news
from outside, not spinning: with a `Done` ending it starts a step even when
the table says rest. That step follows the same rule: if it does not move the
cursor either, it rests, with auto-step off if it was on and there was work.
There is no delay and no timer.

An input left pending after a step that moved is work: with auto-step on the
next step offers it again. A hook that keeps leaving input pending while it
commits each time moves the cursor every step, so only an extension such as
`StepLimit` bounds it.

**Wakes:** an item added to its scratchpad, opening, load (lease holder
only), a go-to, `Session.step`, auto-step turned on. Busy means a step exists
and is not resting. `machine::Machine` holds every cursor's step and starts
one on a wake; a wake while busy joins the step already there.
`Session.step` on a busy cursor is refused (`Error::Busy`).

**Runaway loops** where the cursor does move every step (a model that calls
tools forever) are an extension's (D1): `StepLimit` counts steps in `pre_input`
and `pre_step` and turns auto-step off in `post_step`. Core keeps no count.

## 7. Hooks

| hook point | shape | job | subject |
|---|---|---|---|
| `pre_step` | cursor | `Hooks(PreStep)` | none |
| `pre_input` | cursor | `Hooks(PreInput, draft)`, per item | the input draft |
| `pre_sample` | cursor | `Hooks(PreSample)` | the sample draft; a reply answers it |
| `pre_reply` | cursor | `Hooks(PreReply, draft)` | the reply draft |
| `post_reply` | cursor | `Hooks(PostReply)` | the `Assistant` node |
| `pre_tool` | tool | inside `RunCalls` | the call draft; setting an outcome answers it |
| `pre_result` | tool | inside `RunCalls` | the call draft with its outcome |
| `post_result` | cursor | `Hooks(PostResult)` | the `Results` node |
| `post_step` | cursor | `Hooks(PostStep, ending)` | the `Ending` |

A `Hooks` job runs every hook at that point, in the order their extensions
are listed in `config_at(at)`; the driver answers with one `Answer::Hooks` (the
first string, if any, and the final draft).

**A string from a cursor hook** halts: later hooks at that point don't run,
the draft is discarded, auto-step goes off, `post_step` gets
`Halted(reason)`. From `pre_input`, the item is also removed and its client
gets a notice with the string (`Session::notices`): a refusal, or a
command's reply (`pre_input_string_refuses_with_a_notice`). Inputs already
committed stay.

**A string from a tool hook** makes that call's outcome `Err(reason)`; in
`pre_tool` the tool does not run. The other calls carry on. Core has no
`Skipped`.

**Errors.** A cursor hook that raises is `Answer::HookFailed`: the draft is
discarded, `post_step` gets `Failed(error)`, and an input stays pending. A
tool hook that raises gives its call `Err("hook error")`, withholding any raw
output. Outcomes core writes (`Interrupted`, `Err("hook error")`) skip `pre_result`.

**Handles.** A cursor handle may read the tree, cursors, labels, and move
records; append anywhere; add, update, or remove any item; open cursors; set
labels; ask, show, and tell (§9); sample a walk or run a tool outside any
step. On its own cursor, and only while its hook job is out, it may `move`, `write(body, delivers)`, and set
`auto_step`. A node handle (tool hooks) is the same minus anything on the
cursor. Every handle call goes into Rust at once (§11). An extension reads
its own cursor's scratchpad in `pre_step` and `post_step`. Writes a hook
makes trigger no hooks on its own step. A handle lives exactly as long as its
hook coroutine: once the hook returns (or its job is answered), the handle is
**retired**. A retired handle still reads the tree; every write, `ask`, and
own-cursor call raises `NotInHook`. Core has no handle identity: it allows
own-cursor calls while a cursor hook job is out for that cursor, and the
retired handle is the driver's. The exact surface is Phase 4's.

### Input

`Session.input(cursor, text)` adds an input item and wakes the cursor (the
spike's `Session::add_item`). A **send** is input, then auto-step on: two calls, which the driver's `send` makes. On a
busy cursor the item waits for the next step's input phase. After an abort it
stays pending (Q42). Commands and gates are `pre_input` hooks.

### Questions (the driver's; core has none)

`await cursor.ask(target, question)` returns the response, or `None` if
declined. A question is a hook awaiting a future the driver holds
(`Driver.ask`, `respond`, `questions`); core knows nothing of it. The hook's
job stays out while it waits, so the cursor is **busy**, and core's rules
for a busy cursor apply unchanged: input is accepted and waits for the next
step's input phase, `Session.step` is `Busy`, auto-step on or off only sets
the flag (no second step starts under the question). Abort and go-to take
the abort path: the step is aborted, the driver declines the cursor's open
questions, and the hook finishes with `None`. A question for one client is
declined if it detaches (not built).

### Between cursors

An extension adds an item to another cursor's scratchpad; that cursor wakes,
and its own `pre_step` acts on it, typically with `cursor.write(…,
delivers=[item])`. A **sub-agent** (Phase 5) is a tool that opens a cursor,
adds the task as an item, and waits until the cursor rests at `Wait`.

## 8. Abort, failure, crash

- **Abort** (`Step::abort`) turns auto-step off and ends the step `Aborted`, so
  no later phase applies. It returns true when the driver must cancel the
  effect in flight (`Sample` or `Calls`) and answer what it got. Hooks are never cancelled; post hooks and `post_step`
  run as usual. The driver declines the cursor's open questions (§7), so a
  hook awaiting one finishes.
  - During a sample with parts: the driver answers the partial reply with
    `stop: Interrupted`; it goes through `pre_reply`, is committed, and
    `post_reply` runs (`abort_during_sample_commits_the_partial_reply`).
    With no parts: `Sampled { reply: None, error: None }`, nothing is
    committed.
  - During tools: the driver answers `Interrupted` for every call that had not
    finished, and the join writes `Results`. A running `pre_tool` or
    `pre_result` finishes (Q34).
- **A sample fails.** With output: the reply is committed with `stop: Error`,
  then `post_step` gets `Failed`. With none: core appends a
  `Custom { kind: "sample_error" }` under `at` without moving, and
  `post_step` gets `Failed`. A conversion error is a failed sample (Q11).
- **A tool fails**: an `Err` outcome.
- **`Length`, `Refused`**: ordinary replies; `next` is `Wait`.
- **Crash during a sample**: the input's commit was not synced yet, so its
  item is pending again; the next step delivers it once and samples
  (`crash_during_a_sample_*`).
- **Crash during tools**: the cursor loads at the batch, at rest; every
  outcome is lost (D4). Input goes under the unanswered batch and the
  provider renders its calls as text; a step re-runs the whole batch
  (`after_a_crash_mid_batch_*`). Keeping outcomes across a crash is an
  extension built from `pre_result`, `pre_tool`, and items.
- **Crash during a commit**: node and move are one record; a torn tail is cut
  back to the last `Sync` on open.
- **A save fails** (disk full, permissions changed): it is a crash. The call
  raises the OS error and the session closes itself, as `Session.close`
  leaves it: every later call raises `Closed`. Continuing is reopening from
  disk, which is the crash recovery above; nothing in memory is trusted.

## 9. Events and frontends (not yet spiked)

One `Event` type, tagged with a cursor: `node_appended`, `cursor_moved`,
`label_changed`, `stream_delta`, `item_changed`, `question`, `status`
(`cursor.show`), `notice` (`cursor.tell`, or a refused input). Observers
cannot block or change anything. A `notice` goes only to its client; the rest
go to every subscriber.

Every client has an id, and its actions are `by: Frontend(client)`. `tau
serve` comes first and shapes the `Harness`. No cursor waits on a particular
client; a cursor waits on a person only through a question. On connect,
serve sends every cursor's status, the open questions, and the scratchpads.

## 10. Provider requirements (not yet spiked)

Each provider is a Python adapter for one API family. It owes the driver:

1. Parts streamed in order as `stream_delta`s; the driver builds the reply
   draft and each call's `args_raw`.
2. `stop` normalised, the provider's own string kept as `finish_reason`
   (`Finish`); the sample is handed the driver's tools, which are never recorded.
3. Faithful replay: text verbatim, `args_raw` byte for byte where the wire
   takes a string; parsed only where the wire needs an object.
4. Thinking sent back as the API requires; an unsigned cut-off thinking part
   is stored but never sent.
5. A `Results` node under its batch becomes one tool message per call;
   anywhere else, the conversion makes it legal (e.g. a `User` message).
6. Calls that aren't runnable, or a batch with no `Results` below it on the
   walk, are rendered as text in τ's markup per `partial_calls` (Q27), with a
   stop note for `Length`, `Interrupted`, and `Error`.
7. A walk the API would still reject is a conversion error: the sample fails
   before any request (Q11).
8. Cache use reported per sample (llama.cpp `cache_n`, hosted usage fields).

## 11. Persistence

Core queues what must be made durable in `Session::outbox` as `Record`s
(`Write { node, mv }`, `Move`, `Label`, `Sync { pad }`). A Rust layer
composing core with `tau-store` hands them to the store inside the call
(D11). The store may buffer up to a `Sync`; at a `Sync` it syncs the log,
then, if `pad` is set, rewrites the scratchpad file with it. The log never
holds items. Load is `tree::load(records, scratchpad)`. The step queues a `Sync` before each post hook and when the
step ends; calls outside a step (a frontend's input, go-to) sync on return.
A crash loses at most the writes since the last sync, and never a post hook's
view. A failed save is a crash (§8): `Store::save` consumes the store, and
the Python session closes. Layout (D12): one append-only record log per session
(length-prefixed, checksummed records) plus a scratchpad file rewritten
atomically. A delivering record is durable before its items are removed;
load drops delivered items. Open cuts the log back to its last good `Sync`
(a torn tail) and keeps the cut bytes aside. One `flock` lease per session:
only the holder writes, runs steps, and runs hooks. JMFTS is a replica fed
from the log. Built in `crates/tau-store` (`Store::create`, `open`, `save`).

## Open questions

Each row is the recommendation the documents assume. Retired: Q2, Q14, Q16,
Q19, Q29, Q35, Q36, Q38, Q40, Q41.

| # | question | answer assumed |
|---|---|---|
| Q1 | Name of a node's timestamp | `time` |
| Q3 | Does `Custom` reach the model? | No; nor do items |
| Q4 | Failure and abort | §8 |
| Q5 | Roots | every root is `Config`; `Session::create` writes `Config`, `System`, opens a cursor |
| Q6 | `opened_by` | the opening cursor; empty for a frontend |
| Q7 | Authors | `Frontend(client)`, `Provider`, `Tools`, `Core`, `Extension(name)`; recorded only |
| Q8 | Ids | session-local integers |
| Q9 | Hook timeouts | not core's; `asyncio.timeout` |
| Q10 | Is `Results` final? | yes; another answer is a sibling |
| Q11 | Wire legality | the provider's conversion; a rejection fails the sample |
| Q12 | Do cursors close? | not in core for now |
| Q13 | Two cursors at one batch | each runs it |
| Q15 | How a person branches | go-to, step, input |
| Q17 | Input at an unanswered batch | goes under it (D4) |
| Q18 | After a pre hook answers | the draft's pre hooks still run |
| Q20 | "Keep going", runaway loops | a `post_reply` hook writes a `User` node; `StepLimit` |
| Q21 | Parallel calls | yes; an `exclusive` tool runs alone (not built yet: `Tool` has no `exclusive`) |
| Q22 | Approve one call, deny another | `cursor.ask` from `pre_tool`; declined returns a reason |
| Q23 | Lifecycle hooks | none until Phase 5 needs one |
| Q24 | Record a rewritten call? | not by core |
| Q25 | `pre_sample` changes the request only through the tree? | yes, by moving the cursor |
| Q26 | Revert a one-off `Config`? | no |
| Q27 | A never-run call on resend | `partial_calls: text` (markup pinned in Phase 4) |
| Q28 | Moves record `by` | yes |
| Q30 | Kind of a summary or reminder | `User`, by the extension |
| Q31 | Who delivers input | core, in the input phase |
| Q32 | Do opened cursors run hooks? | yes, opening wakes them |
| Q33 | Cursor names | integers plus any number of labels |
| Q34 | Abort while `pre_tool` runs | it finishes; `None` means `Interrupted` |
| Q37 | A one-client question's client detaches | declined |
| Q39 | A wake with auto-step off | `pre_step` and `post_step` only |
| Q42 | Pending input on abort | stays pending |
| Q43 | How a frontend learns `pre_input` refused | a notice; the item is removed |
| Q44 | Seeing other hooks' moves | `cursor.moves(since_step=True)` |
| Q45 | `sampled_from` on `Assistant` | no; the move records show it |
