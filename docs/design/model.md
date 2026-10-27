# Model

Status: **for review (Phase 1), fifth revision.** Expands `docs/plan.md` §1.
On sign-off this file and `glossary.md` supersede the plan. Every term used
here is defined in `glossary.md`. D1–D10 are the decisions in `plan.md` §4.
Questions marked **Q*n*** are listed at the end, each with a recommendation;
the documents assume the recommendation.

**Decided in review** (2026-10-26). These supersede the plan:
- **Calls stay inside the `Assistant` node** that produced them. One sample
  produced them all at once, so no call is a node of its own.
- **One result node per call** (this reverses D7's default). Each result is
  written as soon as its call finishes, and the cursor moves onto it.
- **A reply is stored as it streamed, with a stop reason.** An `Assistant`
  body is an ordered list of parts (text, thinking, calls) with each call's
  arguments kept as the raw string the model produced. Runnable calls are a
  view, never a reorganisation. The plan's "streamed partials are never nodes"
  becomes: partials are never written token by token, but a sample's
  accumulated parts are written once, whatever stopped it. If the user saw
  tokens, they are stored.
  - What a truncated call becomes on resubmission is a config option
    (`partial_calls`).
  - Replaying parts faithfully is a provider requirement (§11).
- **Only a `stop == tools` reply is owed results**. Calls in a reply
  that stopped for any other reason are not runnable; core writes nothing for
  them, the cursor rests on the reply, and the provider renders them as text.
- **Re-running a batch is a branch.** The tree may hold several runs under one
  `Assistant` node, and a run may branch partway. The invariant is per line:
  each walk answers each call once (I4). Avoiding an accidental second run is
  the driver's job: landing on a batch fast-forwards to the end of the
  existing run, and the claim stops two steps running one batch at once. A
  deliberate re-run is its own operation, `Session.rerun` (§5).
- **Browsing runs nothing.** `Session.move` is browsing history: a go-to never
  starts a step, even with auto-step on. `Session.rerun` is a move plus a
  tools step. After a hook's move, the driver carries on per auto-step (§5).
- **Nobody passes `by`.** The driver fills in a move's `by` and a node's
  author from the caller: hooks get a cursor handle bound to their
  extension, and frontends call through the frontend API.
- **Every write is followed by its move** (fourth revision). A cursor is
  `{id, at}`. The plan's `target`, draft, hold, and discard are gone, and D6
  becomes "a cursor is `{id, at}`; every write is followed by its move".
  - Each step ends with one move onto what it wrote. A tools step moves onto
    each result as it is written.
  - Validation happens at append, so nothing invalid is written, and at move,
    for where a cursor lands.
  - To reject a reply, go to its parent and step again. The old reply stays
    in the tree as a committed sibling (Q2).
  - Auto-step off pauses a cursor *between* steps.
  - Core never writes a node and then abandons it, with two exceptions: an I6
    restatement for a sample that then produced nothing (reused rather than
    duplicated, I6), and the records an interrupted step writes after its
    cursor was moved elsewhere (§8). Any other node no move reaches is an
    extension's side node, a detached root's work, or a record of a failure.
- **Hook points sit around writes.** A `pre_<node>` hook rewrites a node
  before it is written; a `pre_<effect>` hook shapes an effect; a
  `post_<node>` hook runs after the move and before `next` is evaluated.
  - The points are `pre_input`, `post_input`, `post_open`, `pre_sample`,
    `pre_reply`, `post_reply`, `pre_tool`, `pre_result`, `post_result` (§7).
  - Pre hooks change what is written. Post hooks change what happens around
    a written node.
  - This retires the plan's Phase 2 scenario "hook drafts on scratch and
    retargets". Its replacements are "`pre_sample` builds the context it
    returns" (a reminder, a model choice, or a compacted line) and "a hook
    moves the cursor" (a synthetic sibling, or "continue").

**Decided in review, fifth revision** (2026-10-27). A bulletproof data
structure, and small extensions on top, instead of rules about which hook may
do what:
- **Every hook gets the cursor.** Each hook is `hook(cursor, subject) →
  outcome`. The cursor handle is bound to the hook's extension, which is how
  the driver fills in `by`. Through it any hook may read anything and do
  anything a frontend can: append, move, re-run, set auto-step, open cursors.
  There is no table of what a hook point may do (§7).
- **A step remembers where it started; moving a busy cursor interrupts its
  step.** A step's own move happens only if its cursor is still where the
  step left it. Any other move of a busy cursor interrupts the step, by the
  abort rules, and that move wins. `Session.abort` is an interrupt without a
  move (§8).
- **Input is hook-driven.** `Session.input` runs the cursor's input hooks,
  busy or at rest. One of them handles the input, or it is refused. Core has
  no default input behavior and no queue (§7, D2).
- **The base extension** ships in the default config and does what core used
  to: it writes input at rest, and holds input to a busy cursor as a pending
  input in the session's inbox until its post hooks deliver it (§7).
- **`post_open`** runs when a cursor opens, and for every cursor when a
  session loads. The base extension uses it to deliver inputs still pending
  from before a restart.

The whole design rests on one idea: **the conversation is a tree of immutable
nodes, and everything else is either a pointer into that tree or a function of
it.** τ 0.15 kept per-turn state in objects: cursor fields, queues, frames,
suspension brokers. A reload had to guess that state back into existence.
Here nothing is state unless it is a node or a move record.

One way to picture it: **each cursor is a small Turing machine.** Its head
reads `walk(at)`. `next` is the transition function. The extensions listed in
`config_at(at)` are its state table. The session tree is the tape, except that
the tape is append-only, so the only things a machine changes are where its
own head is and what it appends. Hooks therefore belong to a cursor: two
cursors whose lines carry different config run different extensions. Each
hook is handed its machine, the cursor, and acts through it. This is why nodes
and moves a hook writes trigger no hooks: they are the machine writing, not a
new input to it.

Notation: `x?` marks a field that may be absent. `A | B` means one of.

## 1. Stored objects

Only two things are ever written: nodes and move records.

```
Node  { id, parent?, time, by, body, copied_from, derived_from }

body  = System    { text }
      | User      { text, images }
      | Assistant { parts, stop, stop_raw? }
      | Result    { call, outcome }              outcome = Ok(value) | Err(error)
                                                         | Skipped(reason) | Interrupted
      | Config    { snapshot }
      | Custom    { kind, data }

part  = Text(text) | Thinking(text, signature?) | Call { id, tool, args_raw }
stop  = end | tools | length | refused | interrupted | error

Move  { cursor, from?, to, by, time, opened_by? }
```

`copied_from` and `derived_from` are lists of node ids, empty for most nodes.
`copied_from` lists the nodes whose content this node repeats verbatim, in
order: one for a compaction copy, one or more for a delivered input (§7). The
store keeps an in-memory reverse index of `copied_from`, built at load, so
"what copies this node?" is one lookup.

Parts are kept in the order they streamed. `args_raw` is the argument text
exactly as the model produced it, which may be truncated or malformed. It is
never parsed and re-serialised for storage. `stop` is the normalised reason
generation ended. `stop_raw` keeps the provider's own string (`end_turn`,
`length`, …) for debugging.

| `stop` | meaning |
|---|---|
| `end` | the model chose to stop |
| `tools` | the model handed off to tool execution |
| `length` | the output limit was reached |
| `refused` | the provider's filter or refusal |
| `interrupted` | we cancelled it (abort) |
| `error` | the stream failed after output had arrived |

A **session** is the set of its nodes and moves. A **cursor** is `{id, at}`.
`at` is the `to` of the cursor's latest move. A move with no `from` opens a
cursor (Q5). A move's `by` says who asked for it (Q28). `opened_by` names a
cursor, not a node: the one that opened this one (Q6). The driver fills in
both from the caller; no API caller supplies them.

Every root is a `Config` node (Q5), so every walk starts with one and
`config_at` is always defined.

The core is pure. The driver passes `time` in, and core assigns ids at append
(Q8). No function in `tau-core` reads a clock, a file, or the network.

## 2. Batches

The `Call` parts of an `Assistant` node with `stop == tools` and at least one
call form a **batch**. A `stop == tools` reply with no calls is an ordinary
reply: nothing runs, and `next` is `Wait`. The batch's results follow it in a **run**: one result per call,
each under the previous, in the order the calls finished.

Only a reply that handed off runs anything, and only its calls are owed
results:

| reply's `stop` | call's `args_raw` | result |
|---|---|---|
| `tools` | parses | the tool runs, through `pre_tool` and `pre_result` |
| `tools` | doesn't parse | `Err("invalid arguments")`, without running. The model sees its own malformed output. |
| anything else | either | none. The calls are not runnable and owe nothing; the cursor rests on the reply, and the provider renders them as text (§11). A truncated plan is not half-run. |

```
u1  User          "fix the bug"
└ a1  Assistant   text, calls [c3, c5]        the batch: c3 and c5
  └ r5  Result    c5: Ok(…)                   c5 finished first
    └ r3  Result    c3: Ok(…)                 the batch is complete: next is Sample
```

On a given walk, a batch is in one of three states:
- **unstarted**: no results yet;
- **partial**: some calls answered, some not;
- **complete**: every call answered exactly once.

A batch may have more than one run. Running it again is a deliberate branch
(a **re-run**, `Session.rerun`, §5): a sibling run under the `Assistant`
node, or one that branches partway, keeping `r5` and running `c3` again. Each run is its own
line, and each line answers each call once (I4).

Tree order is completion order. It is true, and it is what lets a finished call
survive a crash. The provider's conversion restores call order for the next
request, so the model never sees completion order.

## 3. Derived views

None of these are stored. Each is a function of the stored objects.

| view | definition |
|---|---|
| `walk(n)` | the nodes root → … → `n`, following `parent`. Inclusive. |
| `LCA(a, b)` | the deepest node on both `walk(a)` and `walk(b)`; none if they are in different trees |
| committed | every node on `walk(m.to)` for every move `m`, opening moves included. Permanent (Q2). |
| scratch | every node that is not committed: no move has landed on it or passed through it |
| `config_at(n)` | the body of the nearest `Config` node on `walk(n)` |
| messages | the provider's conversion of a walk's message-bearing nodes |
| `next(walk)` | §6 |

Scratch is not a kind of node, and nothing marks it. It is what is left over:
- side nodes an extension appends off the line, or anywhere else, such as
  the base extension's inbox and its pending inputs (§7);
- work done on a detached root;
- the nodes a `pre_sample` hook built for a sample that then produced nothing;
- `sample_error` records (§8);
- an I6 restatement for a sample that produced nothing, which no move
  reached (it is reused next time, I6);
- what an interrupted step wrote after its cursor was moved elsewhere: a
  reply with `stop: interrupted`, or `Interrupted` results (§8);
- a copy a hook left unfinished, for example by crashing partway through a
  compaction tail.

A later move can make a scratch node committed, for example a frontend going
to an extension's side line.

One session, mid-step, two trees:

```
r    Config  (model: big)            committed  (root)
└ s    System                        committed
  └ u1   User "fix the bug"          committed
    └ a1   Assistant  calls [c3, c5] committed
      └ r5   Result c5: Ok           committed   ◀ A.at   (c3 still running)
d    Config  (model: cheap)          scratch     (detached root: an earlier step's
└ d1   User "summarise s..u1"        scratch      hook made a one-off call; its root
  └ d2   Assistant "…summary…"       scratch      records which model produced d2)
```

Cursor A's moves so far: … → `u1`, → `a1`, → `r5`. When `c3` finishes, `r3`
is appended under `r5` and A moves onto it. The batch is then complete, and
`next` is `Sample`. If the process dies first, A loads at `r5`, `next` is
`RunTools(c3)`, and resume answers `c3` (§8). `c5` does not run again.

## 4. Invariants

Each invariant has a statement, a reason, and the test that holds the code to
it. Tests live in Phase 2 (`tests/behavior/`, Python, through `Harness`) and
Phase 3 (`crates/tau-core`, Rust unit tests and proptest).

### I1 Parent is context
**Statement.** A node's `walk(parent)` contains everything that produced it,
and nothing that bypassed the tree.
- For a sampled `Assistant` node, `walk(parent)` *is* the context the sample
  was drawn from, together with the config in force (`config_at(parent)`).
- A result was produced by its call, which is on `walk(parent)`. Sibling
  results that finished earlier sit above it in the run but played no part.

Any other relation is recorded as `copied_from` (verbatim) or `derived_from`
(computed), never as a parent.

**Why.** In 0.15 the model's view was a fold of the path with splices
(compaction), filters, and per-turn injections. "What did the model see?" had
no direct answer. Here it is `walk(parent)`.

**Enforced by.**
- No API re-parents, splices, or adds to a request outside the tree. A
  `pre_sample` hook changes the request only by returning a context tip, which
  is a node (Q25). The driver samples with `config_at` of that tip and appends
  the reply under it, or under I6's restating `Config` when there is one.
- Behavior test: `FakeProvider` records each sample's context and config. For
  every node with `by == provider`, the recorded context equals
  `walk(node.parent)`, and the recorded config equals `config_at(node.parent)`.
  A node a hook produced or rewrote has that hook's extension as author (Q7).
  A reply an interrupted step wrote is held to the same check: its parent is
  the tip it was sampled from, wherever the cursor went.

### I2 Immutability
**Statement.** No node changes after it is appended. Input hooks and pre
hooks (`pre_reply`, `pre_result`) shape a node *before* it is written; nothing
edits it afterwards. Post hooks see only written nodes.

**Why.** 0.15 wrote nodes "incomplete" and finalized them with a second write.
A process exit between the two left an orphan that nothing could repair.

**Enforced by.**
- `Node` has no setters in Rust and is a frozen class in Python. A behavior
  test checks that assignment raises.
- The store is append-only.
- Proptest: after any operation sequence, every node observed earlier still
  equals what was observed.

### I3 Valid `at`
**Statement.** A cursor's `at` is always a committed node whose walk
validates. A cursor's `at` may be the end of a partial run. It is never a node
above the end of a run (a batch's `Assistant` node, or a result, that already
has a result of the same batch below it), except where `Session.rerun` placed
it. `Session.create` opens the session's first cursor at the `System` node it
writes (Q5).

Nothing lands *inside* a run by accident. A go-to, a hook's move, or
`open_cursor` whose destination is a batch's `Assistant` node, or one of its
results, **fast-forwards** to the end of the existing run:
- it follows only `Result` children answering calls of that same batch,
  taking the most recently appended where there are several (an earlier
  re-run);
- it stops at the result that completes the batch, and never passes onto a
  later reply;
- if the run is partial, the cursor lands at its current end and sits there.
  When it is next stepped, the step takes the claim and continues the run
  (§8). A move never takes a claim.

A move onto a copied batch (an `Assistant` node with `copied_from` and calls)
that is not complete on the walk is refused: the driver never runs a copied
call (§5).

Only `Session.rerun` lands where it was sent inside a batch. That is a
deliberate **re-run** (§5): the move is followed at once by a tools step for
every call still unanswered on its new line.

A move that interrupts a step (§8) resolves `to` only after the interrupted
step has written its records, so it never lands above results the
interrupted step is about to write.

**Why.** "Where is this cursor?" must never need a guess (0.15's
`default_leaf` was a heuristic). And a cursor that landed above the end of a
run by accident would see `next == RunTools` and run calls a second time,
which nobody chose.

**Enforced by.**
- Unit tests: `open_cursor` and go-to onto a batch or into its run
  fast-forward to the run's current end, stop at the completing result, and
  never pass onto a later reply; a move never takes a claim; a move onto a
  copied batch that is not complete is refused; `Session.rerun` lands where
  it was sent; a go-to onto the batch a busy cursor is running interrupts the
  step and lands at the end of the run its `Interrupted` results complete.
- Proptest: after any operation sequence, every `cursor.at` is committed and
  `validate(walk(at))` passes. `at` may be the end of a partial run, and is
  never a node above the end of a run, except where `Session.rerun` placed
  it.

### I4 Every call gets exactly one result
**Statement.** On every walk, a batch (an `Assistant` node with
`stop == tools` and at least one call) is followed only by its own results,
one per call, until the batch is complete. Other replies owe no results (§2).

The check is local, at append: a `Result` must answer a call of the batch on
`walk(parent)` (its parent is the batch's `Assistant` node or one of its
results) that is not yet answered on that walk. Under an unstarted or partial
batch, no other kind of node may be appended.

The tree may hold several runs under one `Assistant` node, and a run may
branch partway. Each is its own line, and each line answers each call once.
Preventing an *accidental* second run is the driver's job, not the tree's:
landing fast-forwards (I3), and the claim stops two steps running one batch at
once (§10).

**Why.** Every provider rejects a conversation with an unanswered call, so a
blocked call still owes the model a result that says so. And two answers to
one call on one line could each be "the" answer.

**Consequence (Q10).** A result is final on its line. Another answer is
another line: a re-run (§5), or a branch from higher up, using copies or
derived nodes, as prune does.

**Enforced by.**
- Unit tests for each rejection.
- Proptest: no walk holds two results for one call, or a non-result node
  after a batch that is not complete.
- Behavior scenarios: tool batch; unknown tool; bad arguments; `pre_tool`
  blocks one call of two; `pre_result` redacts an output; two cursors at one
  batch (Q13); two cursors continuing from one completed batch; a deliberate
  re-run from `a1`; a re-run that keeps `r5` and runs `c3` again; a
  `post_result` hook moves the cursor mid-batch, and the calls still running
  are answered `Interrupted` under the run.

### I5 Validation at append and at move
**Statement.** `append` checks a node against its walk before writing it, and
a move checks where it lands before writing the move record. Nothing invalid
is written, and no cursor lands anywhere invalid. A failed check refuses the
operation and changes nothing.

**Why.** An illegal conversation should fail when it is made, not on the next
provider request, possibly days later.

**Rules.** Core's checks are structural.
- At append:
  - **V1**: I4, on the new node's walk.
  - **V2**: a node without a parent must be `Config`, so every walk starts
    with one.
- At a move (§5): I3, and I6 for a go-to, a hook's move, or a re-run.

Because every node passed V1 and V2 when it was written, every walk is valid,
and `validate(walk)` is a check the tests run, not a step the driver needs.

Provider-specific legality (system prompt placement, consecutive `User` nodes,
image support) belongs to the provider. The driver checks it by converting the
walk to messages before every sample. A conversion error fails that sample
(§8) (Q11).

**Enforced by.**
- Unit tests per rule.
- Behavior scenario: a provider-illegal walk fails at the next sample, records
  the error, and leaves `at` unchanged.

### I6 Config follows the cursor
**Statement.** Moving a cursor never changes its effective config implicitly.
The range checked is **(LCA, to]**: the nodes on `walk(to)` below
`LCA(at, to)`, excluding the LCA and including `to`. With no LCA (another
tree), it is all of `walk(to)`. There are two places a cursor's line can
change:
- **At a sample:** `pre_sample` returns a context tip on another line, for
  example after compaction. If `config_at(tip) != config_at(at)`, and no node
  in (LCA(at, tip), tip] is an explicit `Config`, the driver restates
  `Config(config_at(at))` under the tip (`by: core`) before sampling. The
  sample then runs with the cursor's own config, and the reply is appended
  under that restating `Config`. A `Config` the hook appended on purpose (a
  model choice, Q26) is explicit, so it stands and is not undone.
- **At a go-to, a hook's move, or a re-run** (§5): the same reading. If
  `config_at(to) != config_at(at)`, and no node in (LCA(at, to), to] is an
  explicit `Config`, the move restates the cursor's config under `to` and
  lands on that node instead. `next` looks past it (§6). If `to` is an
  unstarted or partial batch, I4 forbids appending under it, so the move is
  refused (Q16).

**Restating** means: if `to` (or the tip) already has a child that is a
restating `Config` core wrote with the same body, reuse it and land on it;
otherwise append one. Going back and forth between two lines therefore adds
at most one restatement per place.

An **explicit** `Config` is any `Config` node except a restatement core wrote
under this rule. That includes a copied `Config`: a compaction tail copies
`Config` nodes verbatim, with `copied_from`, and the copy counts as explicit.

A config change that a hook or a frontend appends on purpose is explicit, and
it persists until another `Config` node replaces it (Q26).

**Why.** Branching to an old line must not silently switch the model,
provider, or tools to whatever was in force back then (D5).

**Enforced by.**
- Proptest: `config_at(at)` changes only across a move whose (LCA, to]
  contains an explicit `Config`, or which appends or reuses a restatement.
- Unit test: going back and forth between two lines with different config
  writes one restatement per place, then reuses it.
- Behavior scenarios: branch across a config node; compaction across a model
  change.

### I7 Reload is replay
**Statement.** In-memory state is a pure function of the stored nodes and
moves. No heuristics, no repair, no "latest wins".

**Why.** 0.15's loader kept the last line per id and guessed a reopen
position. A crash could therefore produce a state no sequence of operations
would have produced.

**Not persisted, by design:**
- The auto-step flag: every cursor loads with it off.
- The claim (§10).
- The `max_steps` counter (Q20). A loop it would stop cannot survive a
  reload anyway, because auto-step loads off.
- Whether a cursor is busy, and where its step started. Every cursor loads
  at rest.
- The reverse index of `copied_from`, which load rebuilds from the nodes.
- Events and stream deltas.

A cursor that was running a batch loads at the last result it moved onto, and
resume continues from there (§8). Input given to a busy cursor survives a
crash because it is a node, a pending input (§7); whether it was delivered is
read off the nodes too, so a reload neither loses nor repeats it.

A node is durable before any move that names it, and a step's write and its
move are durable together (§10). A torn final record, cut off by a crash, is
a write that never completed. Load ignores it. That is not repair: no state
is guessed, and the result is exactly the state before that write began.

**Enforced by.**
- Proptest: `state == load(save(state))` over random operation sequences,
  including multiple cursors, partial batches, scratch, detached roots,
  interrupts, and pending inputs. `state` here means nodes, moves, the
  committed set, each cursor's `at`, and the set of pending inputs.
- Behavior scenario: input to a busy cursor, then a crash before delivery;
  on reload, `post_open` delivers it exactly once. A crash after delivery
  delivers nothing again.

## 5. Move

A move is the only way `at` changes. Every move comes from one of four
places, and its `by` records which (Q28). No caller supplies `by`: the driver
fills it in from who is calling. Hooks get a cursor handle bound to their
extension, and frontends call through the frontend API.
- **a step's own move** onto what the step just wrote, including the
  fast-forwards a step makes when it re-reads a run on resume or after
  waiting for a claim (`by: core`). I6 cannot apply to these: no `Config` sits
  inside a run;
- **a go-to**, `Session.move(cursor, to)`, from a frontend (`by: frontend`,
  Q15);
- **a hook's move**, the same call through a cursor handle
  (`by: extension(name)`, §7), from any hook point. It may move the hook's own
  cursor or another one;
- **a re-run**, `Session.rerun(cursor, node)`, from a frontend or any hook.

**A step's own move is compare-and-set.** A step records the `at` it started
from. Its own move happens only if the cursor is still where the step left
it: where it started, or where the step's previous own move put it. **Any
other move of a busy cursor interrupts its step** (§8): a go-to, a hook's
move or re-run from any hook (the step's own, an input hook, another
cursor's), all alike. The interrupting move wins. A move whose resolved `to`
equals `at` writes nothing and interrupts nothing.

**A go-to never starts a step**, even with auto-step on. Browsing runs
nothing: no step, no hook, no provider call. At most it writes one restating
`Config` (I6). A cursor that lands on a `User` node or an unstarted batch sits
there until the frontend steps it or gives input. A hook's move is different:
when the hook that made it returns, the driver evaluates `next` for the moved
cursor and continues per auto-step. A re-run is a move plus a step.

A move does every check first, then writes. In order:

1. **Resolve `to`.** A move onto a batch's `Assistant` node or one of its
   results fast-forwards to the end of the existing run (I3). A re-run keeps
   `to` as given.
2. **Refuse** a move onto a copied batch that is not complete on the walk
   (I3). Refuse a re-run whose `to` leaves no call unanswered on its new line
   (for example, the completing result): there is nothing to run.
3. If the resolved `to == at`, stop: nothing is written. A re-run skips this
   check: `rerun(a1)` while `at == a1` runs the batch again.
4. For a go-to, a hook's move, or a re-run, apply I6 (§4). Refuse if a
   restatement would be needed under an unstarted or partial batch.
5. Write: if the cursor is busy, interrupt its step first, and resolve `to`
   again once the step's records are written (§8). Then append the restating
   `Config` if I6 needs a new one, and the move `{cursor, from: at, to, by,
   time}`, together (§10). Set `at := to`, and emit `cursor_moved`.
6. For a re-run only: run a tools step at once for every call still
   unanswered on the new line, without fast-forwarding. It makes a sibling
   run, or a run that branches partway. Afterwards the cursor continues per
   its auto-step.

A move never takes a claim. A step does, when it runs calls (§10).

A **refused** move changes nothing and raises to whoever asked for it: the
frontend, or the hook. It interrupts nothing, because every check runs before
any write. A hook that catches it has handled it, and auto-step stays as it
was. A hook that does not catch it fails like any hook error (§7).

A re-run is always explicit: `Session.rerun`, available to frontends and to
every hook through its cursor handle. Every call it runs passes through
`pre_tool` as usual, so a hook may still block one, or ask a person first
(Q22). A hook's re-run does not nest: the call returns once the move is
written, and the re-run's tools step runs when the hook returns. Resume after
a crash never re-runs on purpose (§8).

```
u1 ─ a1 ─ r5 ─ r3        the first run
      │    └ r3'         rerun(r5): keep c5's result, run c3 again
      └ r3'' ─ r5''      rerun(a1): run both calls again
```

**A step's own move is never refused.** Its node passed append's checks, and
I6 does not apply. It is only ever skipped, when the cursor was moved away
(compare-and-set, above); its node then stays where it was written, as a
record.

**Branching** is a move whose `to` is neither on `walk(at)` nor below `at`:
the cursor leaves its line at the LCA. Moving to an ancestor (a node on
`walk(at)`) is **going back**, not a branch. The branch forms only when the
next write under that ancestor makes a sibling, as when a person rejects a
reply and steps again.

```
before                          after "go to" b2
u1 ─ a1 ─ r1   ◀ A.at           u1 ─ a1 ─ r1                    still committed
 └ b1 ─ b2                       └ b1 ─ b2 ─ c   ◀ A.at         c = restated Config, only
LCA(at, b2) = u1                                                if b's line had different config (I6)
```

## 6. Next

`next(walk)` says what a walk needs done. It is a pure function. The driver
applies it to `walk(at)`. It skips `Config` and `Custom` nodes from the end,
then looks at the nearest message-bearing node:

| nearest message-bearing node | `next` |
|---|---|
| `User` | `Sample` |
| `Assistant` with `stop == tools` and at least one call (unstarted batch) | `RunTools` (all its calls) |
| a result, batch partial | `RunTools` (the calls not yet answered on the walk) |
| a result, batch complete | `Sample` |
| any other `Assistant` (its calls, if any, are not runnable; a `stop == tools` reply with no calls is one, §2), `System`, or none | `Wait` |

Two cursors at the same node always agree on `next`. The "partial" row arises
at the end of a partial run: at the cursor running it, at a cursor that
landed there, or after a reload (I3).

## 7. Steps and hook points

A step is one cycle for one cursor. It writes a node and moves onto it, with
hook points before the effect, before the write, and after the move. A cursor
is **busy** while a step runs on it, from the step's start (its first pre
hook, or its wait for a claim) until its last own move, or until it is
interrupted. Otherwise it is **at rest**. Busy is driver memory, never stored.
A step is finished once its last own move has happened; the post hooks on
that node still run, on a cursor at rest.

### Hooks

Every hook has one shape: **`hook(cursor, subject) → outcome`**.
- `cursor` is a handle to the cursor the hook runs for, bound to the hook's
  extension. That binding is how the driver fills in `by` on whatever the
  hook writes.
- `subject` is what the hook point is about: the input, the context tip, the
  reply, the call, the call and its outcome, or the written node.
- At each hook point, hooks run in the order their extensions are listed in
  `config_at(at)`, and each receives what the previous one returned.

Through its cursor handle, any hook may:
- **read** anything: the cursor's `at`, its `next`, whether it is `busy`, its
  auto-step, and the whole session;
- **act** as a frontend can: append side nodes anywhere the append rules
  allow; write a node under `at` and move onto it as one durable write
  (`cursor.write`, §10); move or re-run this cursor or another; set
  auto-step; step a cursor; abort; open cursors.

No hook point forbids any of these. What an action does follows from the tree
and the cursor, the same for a hook as for a frontend:
- append's checks (I4) and a move's checks (I3, I6) apply as usual;
- a move of a busy cursor interrupts its step, whoever makes it (§5, §8). A
  move never interrupts the post hooks of a node already written and moved
  onto: they all run;
- after a hook's move, once the hook returns, the driver evaluates `next` for
  the moved cursor and carries on per auto-step;
- nodes and moves a hook writes trigger no hooks. A *step* a hook causes (the
  one after a "continue", or a re-run's tools step) runs hooks as usual.
  `post_open` is the one hook point a hook's write can trigger: it belongs to
  the new cursor, not to the one that opened it.

### Hook points

| hook point | runs | subject | outcome |
|---|---|---|---|
| `pre_input` | on `Session.input`, busy or at rest | the input | handled (optionally with a body), pass (the input, possibly rewritten), or refuse (a reason) |
| `post_input` | after an input step's move | the written node | nothing |
| `post_open` | after a cursor's opening move; for every cursor after a session loads | the node the cursor is at | nothing |
| `pre_sample` | before a sample | the context tip | a context tip, or a reply (answered) |
| `pre_reply` | before a reply is written | the reply | the reply, its parts possibly rewritten, or nothing (refused) |
| `post_reply` | after a sample step's move | the written `Assistant` node | nothing |
| `pre_tool` | before each call runs | the call | the call, possibly rewritten, or an outcome (answered) |
| `pre_result` | as each call finishes | the call as it ran, and its outcome | an outcome of the same kind |
| `post_result` | after each result's move | the written result node | nothing |

### Steps

| step | starts when | pre hook (effect) | effect | pre hook (node) | writes, then moves onto | post hook |
|---|---|---|---|---|---|---|
| input | an input hook handles input with a body (below) | none | none | none | that body | `post_input` |
| sample | `next == Sample` | `pre_sample` | provider request | `pre_reply` | one `Assistant` node | `post_reply` |
| tools | `next == RunTools` | per call: `pre_tool` | run the tool | per call: `pre_result` | one result per call, each moved onto as it is written | per result: `post_result` |

**Phases of a step**, in order:
1. **pre hooks on the effect** (`pre_sample`, `pre_tool`);
2. **effect**;
3. **pre hooks on the node** (`pre_reply`, `pre_result`);
4. **write and move**: append the node and move onto it (§5). In a tools step,
   calls run concurrently unless a tool is `exclusive` (Q21), and each call's
   `pre_result` runs as soon as its tool finishes, concurrently too. Only
   write, move, and `post_result` are serialised per cursor: a result is
   written and moved onto, and its `post_result` hooks finish, before the
   next result is written. Results wait their turn in completion order;
5. **post hooks** (`post_input`, `post_reply`, `post_result`) on the written
   node.

**A step remembers where it started.** It records the `at` it started from.
Each node it writes goes under the node it was produced from: a reply under
the context tip it was sampled from (or I6's restating `Config`), a result
under the previous node of its own run. So I1 holds wherever the cursor has
gone. The step's own move onto each node is compare-and-set: it happens only
if the cursor is still where the step left it (§5).

Then the driver evaluates `next` on `walk(at)`. With auto-step on, it keeps
stepping until `next` is `Wait`. With auto-step off, the cursor pauses after
each step until the frontend resumes it. Each step without the frontend
acting counts toward `max_steps`; when the count reaches
`config_at(at).max_steps`, auto-step turns off (Q20).

What each pre hook returns:
- **`pre_sample`** may return a different context tip: a node it appended under
  the tip (a reminder, Q30), a `Config` node (a model choice, which persists, Q26),
  or the end of a new line it built (compaction, Q25). The driver applies I6
  and samples `walk(tip)` with `config_at(tip)`, appending the reply under the
  tip, or under I6's restating `Config` if there is one. Before calling the
  provider, the driver checks that a reply could be appended under the tip
  (I4); if not, it refuses the step. The step's move onto the reply commits those
  nodes too. The hook may instead **answer** with a reply
  (a cache hit), and then the provider is not called.
- **`pre_reply`** may rewrite the reply's parts (a guard, redaction), but not
  its `stop`, which is a fact about the sample. Or it may return nothing to
  **refuse** it. A refused reply, even one with calls, writes nothing; its
  calls never existed in the tree.
- **`pre_tool`** may rewrite the call (a sandbox, a remote host). It may also
  answer with an outcome and skip the tool: a cached or mocked `Ok`, or
  `Skipped("blocked by …")`.
- **`pre_result`** receives the call as it ran (after `pre_tool`'s rewrites)
  and its outcome. It may rewrite the outcome's content but not its kind
  (Q18). It must return an outcome, because every call is owed a result (I4).

An answer from `pre_sample` or `pre_tool` skips the remaining hooks at that
point and the effect. The node's pre hooks still run, so a cache hit is still
redacted (Q18). A refusal ends the step, and no further hooks run.

**Results core writes skip `pre_result`.** `Err("invalid arguments")`,
`Interrupted`, and `Err("hook error")` have no tool output to rewrite, so no
`pre_result` runs for them. They are written and moved onto, and their
`post_result` hooks run like any result's. **Post hooks run for every node a
step writes and moves onto**, whoever authored it. The one exception is an
interrupt: the records it writes run no hooks (§8).

**Post hooks** receive the written node after the cursor has moved onto it,
before `next` is evaluated. Two examples of what one may do through its
cursor:
- `post_input` writes a sibling `u1'` of the typed input `u1`
  (`derived_from: [u1]`, `by: extension(name)`) and moves onto it. The typed
  original stays in the tree, committed. Use an input hook to change what is
  written; use `post_input` to keep what was typed and continue from
  something else.
- `post_reply` writes `User "continue"` under the reply with `cursor.write`,
  so `next` becomes `Sample` (Q20). That works for any reply that is not a
  batch: any `stop` except `tools`, or `tools` with no calls. Under a batch,
  only results may be appended (I4).

**A post hook's move does not interrupt the other post hooks of the same
node.** Every post hook at a point runs on the node that was written, in
config order. A later hook sees the cursor wherever earlier hooks left it:
its `cursor.at` may no longer be the node it was given. If two post hooks
both move, each move is a real move, in order. A post hook that moves after
the step's last own move interrupts nothing, because the step is finished.
In a tools step, a `post_result` hook that moves before the batch is complete
interrupts the calls still running (§8), but the remaining `post_result`
hooks on its own node still run.

When the post hooks return, the driver evaluates `next` on `walk(at)`,
wherever the cursor now is, and carries on per auto-step, unless a go-to
moved the cursor meanwhile: a go-to never starts a step.

Post hooks do not run on the records an interrupt writes (§8). They do run
after a failure that wrote a reply (`stop: error`).

**Hook errors.** An uncaught exception from any hook ends that hook point:
the remaining hooks there do not run. Auto-step goes off, and the frontend
is shown the error.
- In a pre hook on an effect, the effect does not run. In a sample step,
  nothing is written. In a tools step, the call is answered
  `Err("hook error")` (`by: core`).
- In `pre_input`, the input is refused with the error. In `pre_reply`,
  nothing is written.
- In `pre_result`, the tool has run. The call is answered `Err("hook error")`
  (`by: core`), and the raw output is withheld, because the hooks that would
  have redacted it did not finish.
- In a post hook or `post_open`, the node is already written and moved onto.

A refused move or append raises into the hook that asked for it. Caught, it is
handled, and auto-step is unchanged; uncaught, it is a hook error like any
other.

A step that ends without moving its cursor (a refusal, or a sample that failed
before any output) **turns auto-step off**. Without that rule, a refused or
failed sample would repeat forever: `walk(at)` hasn't changed, so `next`
hasn't either. An interrupted step is not one of these: the move that
interrupted it decides what happens next (§8).

### Input

`Session.input(cursor, text)` runs that cursor's `pre_input` hooks, whether
the cursor is busy or at rest. Nothing else in core looks at input: core has
no default input behavior and no queue. Each hook returns one of:
- **handled**, optionally with a body. Without a body, the hook has done all
  it meant to, for example writing a pending input (below). With a body, the
  driver writes it under `at` and moves onto it in an **input step**, then
  runs `post_input`. On a busy cursor, a body is a deliberate interrupt: the
  step is interrupted in place, as by an abort but leaving auto-step alone,
  and the body is written under the `at` the step left (§8).
- **pass**, with the input, possibly rewritten, to the next hook;
- **refuse**, with a reason.

The chain stops at the first hook that handles or refuses. If every hook
passes, the input is refused: "no extension handled this input". A refused
input changes nothing. `Session.input` raises the refusal to the frontend,
which keeps the text.

A handled body is authored `frontend` when it is a `User` body holding the
input as given. Otherwise its author is the extension whose hook produced or
rewrote it. A command is handled with another body: `/model fast` returns a
`Config`, which is written and moved onto like input. The input step's move
is `by: core`, like any step's own move.

**The base extension** ships in the default config, and a config may replace
it. Its `pre_input` handles every input:
- **At rest**, it returns handled with a `User` body holding the input. If
  inputs are still pending for this cursor, it writes the new input as a
  pending input too and delivers them all at once, in order, so nothing typed
  earlier lands later.
- **Busy**, it appends a **pending input**,
  `Custom{kind: pending_input, data: {cursor, text, images}}`, under the
  session's **inbox**, and returns handled without a body. The inbox is a
  `Custom{kind: inbox}` side node under the session's first root, which the
  base extension appends the first time it needs one. Both are scratch.
- **At rest at an unstarted or partial batch**, where no `User` node may be
  written (I4), it appends a pending input and steps the cursor. The tools
  step answers the batch, by the resume rules for a partial run (§8), and the
  input is delivered after it (Q17).

It **delivers** from its post hooks, as its settings in `Config` say:
- `deliver: after_batch` (the default): on `post_result` for the result that
  completes a batch, or on `post_reply` for a reply that is not a batch,
  whichever comes first. Never mid-batch: every call needs its result first.
- `deliver: after_stop`: only on `post_reply` for a reply that is not a batch.
- `combine: true` (the default) delivers all the cursor's pending inputs in
  one `User` node, their texts joined by a blank line. `combine: false`
  writes one `User` node per input, in order.

Delivery is `cursor.write(User{…}, copied_from: [p1, …])`: the `User` node and
the move onto it are one durable write (§10). **That `copied_from` list is the
only record that `p1` was delivered.** A pending input is one that no node
copies from, and the store's reverse index answers that in one lookup (§1).
There is no separate delivered marker: a second write could be lost in a
crash, and the input delivered twice. Delivery is a hook's write, so it runs
no input hooks (Q31). It moves the cursor after the step's own move, so it
interrupts nothing, and other extensions' post hooks on the same node run
whatever their order in the config.

Its `post_open` delivers any inputs still pending for the cursor from before
a restart, if `at` can take a `User` node. Otherwise its post hooks deliver
them once the batch is answered. Every cursor loads with auto-step off, so a
delivered input then waits for the frontend to step or resume the cursor.

Other input behaviors are extensions too:
- **Interrupt and send**: an input hook that, on a busy cursor, returns
  handled with a `User` body. The step is interrupted in place, its partial
  reply or `Interrupted` results are written and moved onto, and the input is
  written after them.
- **A command**: a hook that handles `/model fast` with a `Config` body, and
  passes anything else.
- **A gate**: a hook that refuses input until something is satisfied.

**Going somewhere without new input** (Q15). A frontend may go to any node
with `Session.move`: a move, which runs no hooks and **never starts a step**,
even with auto-step on. This is how a person branches, browses or returns to
an older line, or rejects a reply (go to its parent, then step). To give input
elsewhere, go there first. A cursor that lands on a `User` node or an
unstarted batch sits there until the frontend steps it or gives input.
`Session.rerun` is the one frontend move that starts a step (§5).

### What still holds

Moving a busy cursor is new; the rules that keep a batch's tools running once
are unchanged.
- **Fast-forward.** A hook's move resolves `to` like a go-to (I3). An
  interrupting move resolves `to` only after the interrupted step's records
  are written, so it lands at the end of the run those records complete, never
  above them.
- **Claim.** Only a step running calls holds one (§10). An interrupt cancels
  the calls, writes their `Interrupted` results, then releases the claim. A
  step that is interrupted while it waits for a claim has written nothing and
  simply ends.
- **Resume.** A cursor at the end of a partial run takes the claim when it is
  stepped, re-reads the run, and fast-forwards as its own move (§8). That
  move is compare-and-set too, so a move made while the step waited wins.
  Interrupting a resume answers its remaining calls `Interrupted`.
- **`max_steps`.** Every step counts, including one a hook's move or re-run
  causes. An interrupt is not a step. The frontend's actions reset the count
  as before (Q20). A delivered input is the base extension acting, but the
  frontend's `Session.input` that produced it already reset the count.

## 8. Interrupt, abort, failure, reload

- **Interrupt.** A step is interrupted when its busy cursor is moved by
  anything but the step's own move (§5), when an input hook handles input to
  it with a body (§7), or by `Session.abort`, which is an interrupt without a
  move. The driver cancels the running parts of the step, except the hook
  that asked for the interrupt. Then, in one go, the step writes its records,
  each under the node it was produced from:
  - **During a sample**, if no parts had arrived: nothing is written. Nodes
    `pre_sample` built for this sample are scratch.
  - **During a sample**, if parts had arrived: the reply is written with the
    parts so far and `stop: interrupted`, after `pre_reply` (redaction still
    applies; this is the one hook point an interrupted step still runs, so
    that no reply is written unredacted). Its calls, including one cut off
    mid-arguments, are not runnable and owe no result (§2). The user saw
    those tokens, so they are stored and the model sees them next time
    (§11).
  - **During tools**: results already written stay. Every call without a
    written result gets an `Interrupted` result (`by: core`) under its run.
    That includes a call whose tool finished while its `pre_result` was
    still running: its raw outcome is never written unredacted (Q4).
  - **Moves.** The step's own move onto each record is compare-and-set (§5).
    In place (an abort, or handled input with a body), the cursor is still
    where the step left it, so it moves onto each record as usual: onto the
    interrupted reply (`next` is `Wait`), or onto each `Interrupted` result
    until the batch is complete. After an interrupting move it is not, so
    the records stay where they were written, as scratch, and the
    interrupting move then lands (I3).
  - **Hooks.** No further hook of that step runs, apart from `pre_reply` on
    an interrupted reply: no remaining pre hook, and no post hook on the
    records. A hook that was running is cancelled, unless it is the one that
    asked, or a post hook on a node already written and moved onto: those
    finish. After a step's last own move there is nothing left to interrupt,
    so a move then is just a move, and an abort only turns auto-step off.
  - **Auto-step.** `Session.abort` turns it off: stopping is what abort is
    for. Any other interrupt leaves it alone, and what follows is the
    interrupter's: a go-to sits; after a hook's move, the driver carries on
    per auto-step; a re-run runs its tools step; handled input runs its
    input step.
- **A sample fails.** This covers HTTP errors, a stream that breaks partway,
  and the provider's conversion rejecting the walk. Auto-step goes off either
  way (Q4).
  - **Output had arrived:** the reply is written and moved onto like an
    interrupted one, but with `stop: error`. `stop_raw` and the error text are
    kept. Post hooks run.
  - **Nothing arrived** (for example a 401 or 429 before the first token):
    there is no reply. The driver records the error as a `Custom
    {kind: sample_error}` node under the context tip. It is persisted and
    visible, but it is scratch, not part of the conversation, and the cursor
    does not move.
- **Length, refusal.** These are ordinary replies with `stop: length` or
  `stop: refused`. Their calls owe no result, and `next` is `Wait`. Continuing
  automatically after `length` is an extension's choice, for example a
  `post_reply` hook that writes "continue" with `cursor.write` (Q20).
- **Resume at a batch.** A cursor may sit at the end of a partial run: after
  a reload, after an abort or a paused cursor, or because a move
  fast-forwarded there (I3). When it is next stepped, for example by the
  base extension taking input there (Q17):
  0. Take the batch's claim (§10). If another cursor's step holds it, that
     step is running the batch: wait for it to finish.
  1. With the claim held, re-read the run: if the tree holds results of this
     batch below `at`, fast-forward onto the end of that run (I3). That is
     the step's own move (`by: core`, §5). The driver does this before
     running any call, except in the tools step a `Session.rerun` starts,
     which runs the calls unanswered where it landed. A re-run's intent lives
     only in that step, so a re-run that crashed before its first result
     fast-forwards onto the existing run on reload. Nobody re-chose it; the
     user asks again.
  2. For each call still unanswered on that walk:
     - if its tool is idempotent, run it again like any call: `pre_tool`, the
       tool, `pre_result`, and `post_result`;
     - otherwise answer it `Interrupted` (D4), a core-written result: it
       skips `pre_result`, and its `post_result` hooks run.

     Each result is written and moved onto as usual, so the batch completes.

  This is why each call gets its own result. A ten-minute `bash` that finished
  is not marked `Interrupted` because its sibling was still running.
- **Crash during a write.** A step's node and its move are durable together
  (§10), and so are a hook's `cursor.write` and its move, so a crash leaves
  both or neither. A torn final record is ignored on load.
- **Crash during a sample.** On reload `next` is `Sample`, so resume samples
  again.
- **Reload.** Every cursor loads at rest, with auto-step off, and nothing
  runs until something acts on it. Then `post_open` runs for every cursor
  (§7). That is how the base extension delivers input that was still pending
  when the process died.

## 9. Events

There is one `Event` type, and every event names its cursor:

| kind | when | carries |
|---|---|---|
| `node_appended` | any append, including scratch | the node |
| `cursor_moved` | after a move record is written | `from`, `to`, `by` |
| `stream_delta` | during a sample | a fragment of text, thinking, or a call |

Observers cannot block the driver or change anything. Print/json, RPC, serve,
and the TUI are each a projection of this stream. Per-call approval may need a
fourth kind (Q22).

**No cursor waits on a particular frontend connection.** Events go to
whoever is subscribed, possibly nobody. Under `tau serve` a frontend may
switch to another cursor or another session at any time, and every active
cursor keeps operating: input for a busy cursor is held as a node (§7), not
in a frontend, and nothing a step does blocks on an observer. A cursor *may*
wait on a persisted pending record, such as an approval a hook needs (Q22):
any frontend can answer it, including one that attaches later or runs on
another device.

## 10. Persistence and processes

- `tau-store` keeps, per session, an append-only node log and a move log, each
  with a versioned header. Loading reads both and replays them (I7). The
  on-disk layout is a Phase 3 ADR.
- **Durability.** A node is durable before any move that names it. A step's
  write and its move are durable together, and so are a hook's
  `cursor.write` and its move: a crash leaves both or neither. Delivering a
  pending input relies on this (§7).
  The Phase 3 store ADR picks how, for example one combined log, or one
  record holding both. A torn final record is a write that never completed,
  and load ignores it (I7).
- **Lease.** There is one `flock` per session, taken on the node log. The
  holder writes; every other process gets the session read-only. If the
  holder's process is dead, the lease is reclaimed. 0.15 left a stale holder
  record that nothing checked.
- **Claim.** The driver holds an in-memory claim on a batch's `Assistant`
  node only while a step is running that batch's calls: a tools step, a
  resume, or a re-run's tools step. The step takes the claim before it runs
  any call and re-reads the run once it has it (§8 resume). A move never
  takes a claim, and no move is refused because of one. A step that finds
  the claim held waits, because the holder is by definition running; it then
  re-reads the run and fast-forwards instead of running the calls again
  (Q13). So two steps never run one batch at once, a re-run included. An
  interrupted step releases its claim once its `Interrupted` results are
  written (§8). The lease keeps every claim inside one process.

## 11. Provider requirements

Core knows no provider. Each provider is a Python adapter for one API family,
and it owes the driver the following. The behavior suite holds every provider
to these through `FakeProvider` and a recorded-wire test per provider.

1. **Stream parts in order.** Text, thinking, and calls arrive as `stream_delta`
   events. The driver accumulates them into the reply's parts, and each
   call's argument text into its `args_raw`.
2. **Normalise the stop reason** into `stop`, and keep the raw string in
   `stop_raw`. For example: OpenAI `tool_calls`, Anthropic `tool_use` → `tools`;
   `length`, `max_tokens`, `MAX_TOKENS` → `length`; `content_filter`,
   `refusal`, `SAFETY` → `refused`.
3. **Replay faithfully.** Converting a walk sends earlier parts back as they
   were produced:
   - text verbatim;
   - `args_raw` byte-for-byte where the wire takes a string (OpenAI). Where it
     requires an object (Anthropic), parse it there and nowhere else.

   This is what lets a provider's prefix cache reuse a partial reply.
4. **Reasoning follows the API's rules, not ours.** Send thinking back exactly
   as the API requires: signed blocks unmodified, encrypted reasoning as it
   was received. Omit it where the API or the server's chat template drops it.
   A cut-off thinking part with no signature is stored and shown, but it is
   never sent back.
5. **Render what isn't a call as text.** Two things are rendered after the
   reply's text, using τ's markup (Q27):
   - a `Call` part of a reply whose `stop` is not `tools`, complete or
     truncated, which was never run and has no result (§2), rendered
     according to `config.partial_calls`, for example
     `[τ: call not run: interrupted]`;
   - a stop note for `length`, `interrupted`, and `error`.
6. **Legality is the conversion's.** A walk the API would reject is a
   conversion error, and the sample fails before any request is sent (Q11).
7. **Report cache use.** Each sample reports how many prompt tokens came from
   the provider's cache: llama.cpp `cache_n`, and the hosted APIs' usage
   fields. Phase 4's exit criteria include a llama.cpp test: interrupt a reply
   mid-text, send input, and assert that the cached count covers the old
   prompt plus the partial text. Phase 5's `cache_state` builds on the same
   numbers.

## Open questions

Each one has a recommendation, which is what the documents assume. A "no"
means a document changes before sign-off. Q14 and Q19 were folded into the
review decisions and Q20, and Q29 into the decision that only a
`stop == tools` reply is owed results. Q31 and Q32 are new in the fifth
revision.

| # | question | recommendation |
|---|---|---|
| Q1 | The plan names the node timestamp `at`, the same as `Cursor.at`. Rename? | **`time`.** One word, one meaning. |
| Q2 | Is "committed" permanent, or does a node stop being committed once every cursor has moved off it? May a move land on old scratch, such as an extension's side line? | **Permanent, and yes.** Committed is derivable from moves, and cursors can reopen on old lines. A rejected reply stays committed; the line simply moved away from it. Validation still guards every move. |
| Q3 | Does a `Custom` node ever reach the model? | **No.** To show the model something, append a message-bearing node. That keeps `walk` honest (I1) and the provider conversion small. |
| Q4 | Failure and abort. The plan discards a failed step, but abort during tools must still answer every call or I4 breaks, and tokens the user saw should not vanish. | **As in §8.** A sample that produced output is written with its `stop` (`interrupted` or `error`). A sample that produced nothing leaves at most a scratch `sample_error` node. An interrupted tools step answers unwritten calls `Interrupted`. After an abort (an interrupt in place) the cursor moves onto what was written; after an interrupting move those records are scratch. |
| Q5 | Roots and opening. I3 says cursors open at committed nodes, but an empty session has none. | **Every root is a `Config` node.** `Session.create(config, system)` appends the root `Config` and a `System` node under it (both `by: core`), and opens the first cursor at the `System` node with an opening move (a move with no `from`, which the plan doesn't have). Detached roots are `Config` too, so each records the model it used; a root a hook creates is authored `extension(name)`, filled in by the driver like any append. |
| Q6 | What does `opened_by` name? | **The opening cursor.** A tool receives a handle bound to the calling cursor, so a cursor a tool opens (a sub-agent) has `opened_by` = the calling cursor. The sub-agent opens on a line outside the parent's batch, for example a `User` task under a detached root or under a node above the batch; details are Phase 5. Its answer carries `derived_from`. |
| Q7 | What is the set of authors? | **`frontend`, `provider`, `tools`, `core`, `extension(name)`.** A node's author is whoever produced its content as written. A hook's answer or rewrite makes its extension the author, and so does a copy. No caller supplies the author: the driver fills it in from the caller (§5). Authors are recorded only, never used for control flow. |
| Q8 | Node id format? | **A session-local integer assigned at append.** It is deterministic, which suits I7 and tests. JMFTS (Phase 7) maps its own ids. ADR in Phase 3. |
| Q9 | 0.15 had handler policies (timeouts on hooks). Keep any? | **Not in core.** A hook that needs a deadline uses `asyncio.timeout`. |
| Q10 | Is a written result final? | **Final on its line; another answer is another line.** Two answers to one call on one line could each be "the" answer, so I4 forbids it. A deliberate re-run (§5) or a branch from higher up gives the call another answer on another line. |
| Q11 | Where does provider-specific legality live? | **In the provider's conversion**, which the driver runs before every sample. A rejection is a failed sample. Core stays provider-agnostic. |
| Q12 | Do cursors close? | **Not in core for now.** Cursors are cheap, and frontends hide finished ones. Closing would need a new record and an ADR. |
| Q13 | Two cursors at one batch both have `next == RunTools`. | **Run once**: an in-memory claim per batch, held while a step runs its calls (§10). The second cursor's step waits, then re-reads the run and fast-forwards onto it. This becomes a Phase 2 scenario. |
| Q15 | How does a person branch or go back? | **Three frontend operations.** "Go to" (`Session.move`) is a move with no hooks, and it never starts a step. A re-run (`Session.rerun`) is a move plus a tools step. Input (`Session.input`) goes to the cursor wherever it is, busy or at rest, and its input hooks decide what happens; to give input elsewhere, go there first. `Harness` exposes all three. |
| Q16 | A go-to across a config change restates the `Config` *under* the node it goes to. A go-to to an unstarted or partial batch is then refused, because I4 forbids appending there. | **As written.** No other placement keeps both I4 and I6, and going to an unanswered batch on another line is rare. |
| Q17 | Input arrives while `at` is an unstarted or partial batch: a paused cursor, or after a reload. | **The base extension's call, not core's.** No `User` node may be written there (I4), so the base extension holds the input as a pending input and steps the cursor. The tools step answers the batch (a partial run by the resume rules: idempotent calls run again, the rest are `Interrupted`), and the input is delivered after it. This retires `Skipped("superseded by input")`: an extension cannot tell whether a call started before a reload, and the tree cannot either. Cost: a person who paused at an unstarted batch and then types sees the batch run first. To skip it, they go back to the reply's parent and step again. |
| Q18 | Which hooks run after a pre hook answers, and what may `pre_result` change? | **The node's pre hooks run** (`pre_reply`, `pre_result`), so a cache hit still gets redacted. `pre_result` may change an outcome's content but not its kind: it cannot turn `Skipped` into `Ok`. The driver enforces that. |
| Q20 | Where does "keep going" live (a hook that sends more input after the model stops), and what stops a runaway loop? | **In post hooks.** A `post_reply` hook writes a `User` node (`by: extension(name)`) under any reply that is not a batch (any `stop` except `tools`, or `tools` with no calls) with `cursor.write`, and `next` becomes `Sample`. As a hook's write, it runs no input hooks. A runaway loop is stopped by `max_steps` in `Config`: the most steps a cursor takes without the frontend acting on it. The driver keeps an in-memory counter per cursor, reset whenever the frontend acts on that cursor (gives input, steps it, re-runs, turns auto-step on). When it reaches `config_at(at).max_steps`, auto-step turns off and the cursor pauses. `next` stays a pure function of the walk. Nothing is persisted: every cursor loads with auto-step off (I7), so a runaway loop cannot survive a reload. |
| Q21 | Do a batch's calls run in parallel? | **Yes, by default.** A tool can be `exclusive` and run alone, for example two edits to one file. |
| Q22 | How does a person approve one call and deny another? | **A `pre_tool` hook awaits an answer from the frontend.** Nothing is written while it waits. Abort or a crash leaves the call unanswered, and resume answers it `Interrupted`. This needs a fourth event kind (a question to the frontend) and a handle call to answer it. The same mechanism lets a `pre_reply` hook show a reply to a person before it is written. 0.15's suspensions tried to do this, so it gets its own ADR. **Note (fifth revision):** the wait should be on a persisted pending record in the session, not on one frontend's connection (§9): under `tau serve` the person may have switched away, attach later, or answer from another device. Like input to a busy cursor (D2), the question is a node any frontend can answer, and the answer is written as a node. The record's shape is left open for the ADR. |
| Q23 | 0.15 had lifecycle hooks (`session_start`, `session_shutdown`, `session_before_switch`). | **None until a Phase 5 extension needs one.** None of the three does. |
| Q24 | When `pre_tool` rewrites a call (a sandbox, say), is the executed call recorded? | **Not by core.** The `Assistant` node keeps the call as the model made it, and the result answers it. A hook that wants an audit trail writes a `Custom` node on a detached root, because nothing but results may hang off a batch that is not complete (I4). |
| Q25 | May `pre_sample` change the request only by returning a context tip, never by editing the request invisibly? | **Yes.** That is I1's guarantee. Reminders, model choices, and compaction are all visible in the tree. |
| Q26 | A `pre_sample` hook that picks a cheaper model appends a `Config` node, and that choice persists on the line. Should one-off choices revert on their own? | **No automatic revert.** A routing hook that wants a one-off appends the restoring `Config` at its next `pre_sample`. The tree then shows exactly which model produced each reply, and nothing reverts behind anyone's back. |
| Q27 | What does a call that was never run (its reply's `stop` was not `tools`) become when the walk is sent again, and what does τ's markup look like? | **`partial_calls: text` by default**, rendered as `[τ: interrupted while calling write_file; arguments so far: …]` after the reply's text. `partial_calls: omit` drops it. Stop notes use the same `[τ: …]` form. The exact markup is pinned in the Phase 4 provider ADR and tested byte for byte, because changing it later invalidates every cached prefix that contains it. |
| Q28 | Hooks can now move a cursor. Should a move record say who asked for it? | **Yes: `{cursor, from?, to, by, time, opened_by?}`**, with `by` one of `frontend` (a go-to, a re-run, an opening), `core` (a step's own move, an input step's move), or `extension(name)` (a hook's move, write, or re-run). No API caller supplies it: the driver fills it in from the caller, as it does a node's author. Like a node's author, it is recorded only, never used for control flow. |
| Q30 | What body kind is a compaction summary or a `pre_sample` reminder? | **`User`, authored by the extension** (`by: extension(name)`). It is legal on every API's wire, where `System` is legal only at the start of the conversation for several APIs. `Custom` never reaches the model (Q3). |
| Q31 | When does the base extension deliver, and what sees a delivered input? | **`deliver: after_batch`, `combine: true` by default** (§7): the earliest point the old "one kept input" rule allowed, in one `User` node. One seam, accepted: a delivered input is a hook's write, so `post_input` does not run for it (an extension that must see every message a person sent watches `node_appended`, or reads pending inputs). A batch completed by a result core writes still runs `post_result` (§7), so `after_batch` delivery does not miss it. |
| Q32 | Does `post_open` run for a cursor a hook or tool opens? Nodes and moves a hook writes otherwise trigger no hooks. | **Yes, for every opening.** `post_open` is the new cursor's own first hook point, not a hook on the opener's machine, so it does not re-enter the hook that opened it. A tool's handle stays as before: it may read, append, and open cursors, but not move or re-run, so a tool cannot interrupt the step that runs it. |
