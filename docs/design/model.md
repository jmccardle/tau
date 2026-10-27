# Model

Status: **for review (Phase 1), sixth revision.** Expands `docs/plan.md` §1.
On sign-off this file and `glossary.md` supersede the plan. Every term used
here is defined in `glossary.md`. D1–D12 are the decisions in
`decisions/`. Questions marked **Q*n*** are listed at the end, each with a
recommendation; the documents assume the recommendation.

## Decided in review

These supersede the plan. Revisions one to five (2026-10-26, 2026-10-27):
- **Calls stay inside the `Assistant` node** that produced them. One sample
  produced them all at once, so no call is a node of its own.
- **One result node per call** (this reverses D7's default). Each result is
  written as soon as its call finishes, and the cursor moves onto it.
- **A reply is stored as it streamed, with a stop reason.** An `Assistant`
  body is an ordered list of parts (text, thinking, calls) with each call's
  arguments kept as the raw string the model produced. Runnable calls are a
  view, never a reorganisation. Partials are never written token by token,
  but a sample's accumulated parts are written once, whatever stopped it. If
  the user saw tokens, they are stored.
  - What a truncated call becomes on resubmission is a config option
    (`partial_calls`).
  - Replaying parts faithfully is a provider requirement (§11).
- **Only a `stop == tools` reply is owed results.** Calls in a reply that
  stopped for any other reason are not runnable; core writes nothing for
  them, the cursor rests on the reply, and the provider renders them as text.
- **Re-running a batch is a branch.** The tree may hold several runs under one
  `Assistant` node, and a run may branch partway. The invariant is per line:
  each walk answers each call once (I4). Landing on a batch fast-forwards to
  the end of the existing run, and the claim stops two steps running one
  batch at once. A deliberate re-run is its own operation, `Session.rerun`
  (§5).
- **Nobody passes `by`.** The driver fills in a move's `by` and a node's
  author from the caller: hooks get a cursor handle bound to their
  extension, and frontends call through a client handle.
- **Every write is followed by its move.** A cursor is `{id, at}`. The plan's
  `target`, draft, hold, and discard are gone (D6). Validation happens at
  append, so nothing invalid is written, and at move, for where a cursor
  lands. To reject a reply, go to its parent and step again; the old reply
  stays in the tree as a committed sibling (Q2).
- **Hook points sit around writes.** A pre hook on a node rewrites it before
  it is written; a pre hook on an effect shapes the effect; a post hook runs
  after the move.
- **Every hook gets the cursor.** Each hook is `hook(cursor, subject) →
  outcome`, with a cursor handle bound to its extension.

**Sixth revision** (2026-10-27). A small core, and extensions that talk
through one shared place:
- **Hooks act only on their own cursor.** A hook may move, re-run, write
  under `at`, or set auto-step only on the cursor it runs for, and it may
  move or write under `at` only at a **boundary**: a hook point where none of
  the step's effects is in flight (§7). Cursors talk to each other through
  the scratchpad.
- **The scratchpad.** A persistent, modifiable list of **items**, off the
  tree: `{id, from: {frontend?, extension, cursor}, to: cursor, data}`.
  Anyone may add, update, or remove any item. Adding an item wakes the
  cursor it is addressed to. An extension reads its cursor's scratchpad only
  in `pre_step` and `post_step`. It replaces the fifth revision's pending
  input nodes (§1, §7, D2).
- **`delivers`.** A node that consumes items lists their ids. On load, an
  item some node delivers is dropped, so an item is consumed at most once and
  its content is stored once (§10).
- **`pre_step` and `post_step`.** `pre_step` runs when a cursor wakes and
  before every step; it is where an extension takes in what has arrived.
  `post_step` runs after every step, aborts included, with how the step
  ended. They replace `post_open` and `post_input` (§7).
- **Abort cancels effects, never hooks.** Esc stops the sample stream or the
  running tools. Hooks run normally on what was written. A go-to on a busy
  cursor is an abort, then the move; it is refused while a hook is running
  (§5, §8).
- **Questions.** `cursor.ask` asks a person through any frontend or one
  client, and locks the cursor until it is answered or declined. Abort does
  not close a question (§7).
- **Frontends.** Every client of a frontend has an id, and `by` names it:
  `frontend(client)`. Extensions show state to every frontend
  (`cursor.show`) or tell one client (`cursor.tell`). `tau serve` is built
  first among frontends and shapes the `Harness` (§9).
- **Cursor ids** are session-local integers, with an optional immutable
  label set when the cursor opens (Q33).
- **"Scratch" is now "uncommitted"**, so that it cannot be confused with the
  scratchpad.
- **Core decides; the driver performs** (D11). A cursor's cycle is a sans-IO
  state machine in `tau-core`. The Python driver runs the jobs it hands out
  (hooks, samples, tools, persistence, waits) and feeds back the results.
- **One record log, plus the scratchpad file** (D12).

The whole design rests on one idea: **the conversation is a tree of immutable
nodes, and everything else is either a pointer into that tree, a function of
it, or an item in the scratchpad.** τ 0.15 kept per-turn state in objects:
cursor fields, queues, per-turn state objects, pausing brokers. A reload had
to guess that state back into existence. Here nothing is state unless it is a
node, a move record, or a scratchpad item.

One way to picture it: **each cursor is a small Turing machine.** It reads
`walk(at)`. `next` is the transition function. The extensions listed in
`config_at(at)` are its state table. The session tree is the tape, except
that the tape is append-only, so the only things a machine changes are where
it is and what it appends. Hooks therefore belong to a cursor: two cursors
whose lines carry different config run different extensions. Each hook is
handed its machine, the cursor, and acts only on that machine. This is why
nodes and moves a hook writes trigger no hooks: they are the machine writing,
not a new input to it. The scratchpad is how one machine leaves word for
another. **This is an analogy only.** None of its words (head, tape, state
table) is a term of this design or an identifier in its code.

Notation: `x?` marks a field that may be absent. `A | B` means one of.

## 1. Stored objects

Three things are stored. Nodes and move records are written once and never
changed. Scratchpad items may be updated and removed.

```
Node  { id, parent?, time, by, body, copied_from, derived_from, delivers }

body  = System    { text }
      | User      { text, images }
      | Assistant { parts, stop, stop_raw? }
      | Result    { call, outcome }              outcome = Ok(value) | Err(error)
                                                         | Skipped(reason) | Interrupted
      | Config    { snapshot }
      | Custom    { kind, data }

part  = Text(text) | Thinking(text, signature?) | Call { id, tool, args_raw }
stop  = end | tools | length | refused | interrupted | error
by    = frontend(client) | provider | tools | core | extension(name)

Move  { cursor, from?, to, by, time, opened_by?, label? }

Item  { id, from: { frontend?, extension, cursor }, to, data }
```

`copied_from`, `derived_from` and `delivers` are lists, empty for most nodes.
- `copied_from` lists the nodes whose content this node repeats verbatim, in
  order, such as a compaction copy. The store keeps an in-memory reverse
  index of it, built at load, so "what copies this node?" is one lookup.
- `derived_from` lists the nodes whose walks this node's content was computed
  from, such as a summary.
- `delivers` lists the scratchpad items this node consumed, such as the
  inputs a delivered `User` node holds (§7). It holds ids, never content.

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

A **session** is the set of its nodes, moves, and scratchpad items. A
**cursor** is `{id, at}`. `at` is the `to` of the cursor's latest move. A move
with no `from` opens a cursor (Q5); it assigns the cursor its id, the next
session-local integer, and may carry a `label`, a name that is unique in the
session and never changes (Q33). A move's `by` says who asked for it (Q28).
`opened_by` names a cursor, not a node: the one that opened this one (Q6). The
driver fills in `by` and `opened_by` from the caller; no API caller supplies
them.

A **scratchpad item** is addressed to one cursor (`to`). `from` names the
extension and cursor that added it and, when a person's input started it,
the frontend client. `data` is any JSON value; its meaning belongs to the
extensions that read it. Items have session-local integer ids that are never
reused. The scratchpad is not part of the tree: no walk contains an item, and
no item reaches the model (I1).

Every root is a `Config` node (Q5), so every walk starts with one and
`config_at` is always defined.

The core is pure. The driver passes `time` in, and core assigns ids (Q8). No
function in `tau-core` reads a clock, a file, or the network.

## 2. Batches

The `Call` parts of an `Assistant` node with `stop == tools` and at least one
call form a **batch**. A `stop == tools` reply with no calls is an ordinary
reply: nothing runs, and `next` is `Wait`. The batch's results follow it in a
**run**: one result per call, each under the previous, in the order the calls
finished.

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
node, or one that branches partway, keeping `r5` and running `c3` again. Each
run is its own line, and each line answers each call once (I4).

Tree order is completion order. It is true, and it is what lets a finished
call survive a crash. The provider's conversion restores call order for the
next request, so the model never sees completion order.

## 3. Derived views

None of these are stored. Each is a function of the stored objects.

| view | definition |
|---|---|
| `walk(n)` | the nodes root → … → `n`, following `parent`. Inclusive. |
| `LCA(a, b)` | the deepest node on both `walk(a)` and `walk(b)`; none if they are in different trees |
| committed | every node on `walk(m.to)` for every move `m`, opening moves included. Permanent (Q2). |
| uncommitted | every node that is not committed: no move has landed on it or passed through it |
| `config_at(n)` | the body of the nearest `Config` node on `walk(n)` |
| messages | the provider's conversion of a walk's message-bearing nodes |
| `next(walk)` | §6 |
| a cursor's scratchpad | the items whose `to` is that cursor, in the order they were added |

Uncommitted is not a kind of node, and nothing marks it. It is what is left
over:
- side nodes an extension appends off the line, or anywhere else;
- work done on a detached root;
- the nodes a `pre_sample` hook built for a sample that then produced nothing;
- `sample_error` records (§8);
- an I6 restatement for a sample that produced nothing, which no move
  reached (it is reused next time, I6);
- a copy a hook left unfinished, for example by crashing partway through a
  compaction tail.

A later move can make an uncommitted node committed, for example a frontend
going to an extension's side line, or a cursor opening on a detached root.

One session, mid-step, two trees:

```
r    Config  (model: big)            committed    (root)
└ s    System                        committed
  └ u1   User "fix the bug"          committed
    └ a1   Assistant  calls [c3, c5] committed
      └ r5   Result c5: Ok           committed    ◀ A.at   (c3 still running)
d    Config  (model: cheap)          uncommitted  (detached root: an earlier step's
└ d1   User "summarise s..u1"        uncommitted   hook made a one-off call; its root
  └ d2   Assistant "…summary…"       uncommitted   records which model produced d2)

scratchpad:  item 4 { from: {frontend: laptop, extension: base, cursor: A},
                      to: A, data: {text: "also check b.rs"} }
```

Cursor A's moves so far: … → `u1`, → `a1`, → `r5`. When `c3` finishes, `r3`
is appended under `r5` and A moves onto it. The batch is then complete, and
`next` is `Sample`. Before the sample, A's `pre_step` runs, and the base
extension delivers item 4 as a `User` node under `r3` (§7). If the process
dies first, A loads at `r5`, `next` is `RunTools(c3)`, and resume answers
`c3` (§8). `c5` does not run again. Item 4 is still in the scratchpad.

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

Any other relation is recorded as `copied_from` (verbatim), `derived_from`
(computed), or `delivers` (consumed items), never as a parent. Scratchpad
items never reach the model: to show the model an item's content, an
extension writes a node.

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
  A reply an aborted step wrote is held to the same check.

### I2 Immutability
**Statement.** No node changes after it is appended. Pre hooks on a node
(`pre_reply`, `pre_result`) shape it *before* it is written; nothing edits it
afterwards. Post hooks see only written nodes. Scratchpad items are not nodes,
and changing one changes no node.

**Why.** 0.15 wrote nodes in two parts and finalized them with a second
write. A process exit between the two left an orphan that nothing could
repair.

**Enforced by.**
- `Node` has no setters in Rust and is a frozen class in Python. A behavior
  test checks that assignment raises.
- The record log is append-only.
- Proptest: after any operation sequence, every node observed earlier still
  equals what was observed.

### I3 Valid `at`
**Statement.** A cursor's `at` is always a committed node whose walk
validates. A cursor's `at` may be the end of a partial run. It is never a node
above the end of a run (a batch's `Assistant` node, or a result, that already
has a result of the same batch below it), except where `Session.rerun` placed
it. `Session.create` opens the session's first cursor at the `System` node it
writes (Q5). `open_cursor` may open a cursor on any node whose walk
validates, committed or not; its opening move commits that node's walk (Q35).

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

A go-to on a busy cursor resolves `to` only after the abort it causes has
written the step's records and moved onto them (§5), so it never lands above
results the aborted step was about to write.

**Why.** "Where is this cursor?" must never need a guess (0.15 guessed a
default position). And a cursor that landed above the end of a run by
accident would see `next == RunTools` and run calls a second time, which
nobody chose.

**Enforced by.**
- Unit tests: `open_cursor` and go-to onto a batch or into its run
  fast-forward to the run's current end, stop at the completing result, and
  never pass onto a later reply; a move never takes a claim; a move onto a
  copied batch that is not complete is refused; `Session.rerun` lands where
  it was sent; a go-to onto the batch a busy cursor is running resolves to
  the run's current end, which is `at`, so it writes nothing and aborts
  nothing (§5 step 3).
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
  `post_result` hook's move before the batch is complete is refused (§7);
  abort during tools answers every running call `Interrupted`, and their
  `post_result` hooks run.

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
  lands on that node instead. `next` looks past it (§6).
- **Deferred, at a batch.** If `to` is an unstarted or partial batch, I4
  forbids appending under it. The move lands on the batch (fast-forwarded,
  or where a re-run sent it) without a restatement, and the run uses
  `config_at` of its own walk, as every run does. The restatement is owed,
  and is written at the first legal place: when a step's own move lands the
  cursor on the result that completes the batch, it appends the restating
  `Config` under that result (or reuses one) and moves onto it, in the same
  durable write (Q16). What is owed is read off the move log, not kept in
  memory: the cursor's latest move not `by: core` landed in that batch, and
  `config_at(from)` of that move differs from the batch's. A later move
  elsewhere cancels it. So it survives a reload (I7).

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
  contains an explicit `Config`, or which appends or reuses a restatement,
  or which lands in an unstarted or partial batch; in the last case the
  cursor's config is restored by the restatement under the completing
  result.
- Behavior scenario: go-to an unstarted batch on a line with another config;
  the batch runs with that line's config, and the cursor lands on a
  restating `Config` under the completing result, also after a crash
  mid-batch.
- Unit test: going back and forth between two lines with different config
  writes one restatement per place, then reuses it.
- Behavior scenarios: branch across a config node; compaction across a model
  change.

### I7 Reload is replay
**Statement.** In-memory state is a pure function of the stored nodes, moves,
and scratchpad items. No heuristics, no repair, no "latest wins".

**Why.** 0.15's loader kept the last line per id and guessed a reopen
position. A crash could therefore produce a state no sequence of operations
would have produced.

**Not persisted, by design:**
- The auto-step flag: every cursor loads with it off.
- The claim (§10).
- The `max_steps` counter (Q20). A loop it would stop cannot survive a
  reload anyway, because auto-step loads off.
- Whether a cursor is busy. Every cursor loads at rest.
- Open questions, and what extensions show frontends (§9). A question lost
  in a crash leaves its call unanswered, and resume answers it (§8).
- When each cursor last ran `pre_step`, and the scratchpad as it was then.
- The reverse index of `copied_from`, which load rebuilds from the nodes.
- Events and stream deltas.

A deferred I6 restatement is not memory: it is read off the move log.

A cursor that was running a batch loads at the last result it moved onto, and
resume continues from there (§8). Input given to a busy cursor survives a
crash because it is a scratchpad item; whether it was delivered is read off
the nodes' `delivers`, so a reload neither loses nor repeats it.

A node is durable before any move that names it, and a step's write and its
move are durable together (§10). A torn final record, cut off by a crash, is
a write that never completed. Load ignores it. That is not repair: no state
is guessed, and the result is exactly the state before that write began.

Load **reconciles** the scratchpad with one rule: an item listed in some
node's `delivers` is dropped. A crash between a delivering write and the
item's removal therefore cannot deliver the item twice (§10). The rule is a
fact about stored records, not a guess.

**Enforced by.**
- Proptest: `state == load(save(state))` over random operation sequences,
  including multiple cursors, partial batches, uncommitted nodes, detached
  roots, aborts, and scratchpad items added, updated, removed, and
  delivered. `state` here means nodes, moves, the committed set, each
  cursor's `at` and label, and the scratchpad.
- Proptest: a crash injected between a delivering write and the item's
  removal loads with the item gone and the node present.
- Behavior scenario: input to a busy cursor, then a crash before delivery;
  on reload, the cursor wakes and `pre_step` delivers it exactly once. A
  crash after delivery delivers nothing again.

## 5. Move

A move is the only way `at` changes. Every move comes from one of four
places, and its `by` records which (Q28). No caller supplies `by`: the driver
fills it in from who is calling. Hooks get a cursor handle bound to their
extension, and frontends call through a client handle.
- **a step's own move** onto what the step just wrote, including the
  fast-forwards a step makes when it re-reads a run on resume or after
  waiting for a claim (`by: core`). I6 cannot apply to these: no `Config` sits
  inside a run;
- **a go-to**, `Session.move(cursor, to)`, from a frontend
  (`by: frontend(client)`, Q15);
- **a hook's move**, the same call through a cursor handle, on the hook's own
  cursor, at a boundary (`by: extension(name)`, §7);
- **a re-run**, `Session.rerun(cursor, node)`, from a frontend, or from a
  hook on its own cursor at a boundary.

**A hook's move never meets running work.** A hook may move only its own
cursor, and only at a boundary, where no effect of its step is in flight
(§7). The move applies at once. Later hooks at the same point see the cursor
where it now is, and when the hook point finishes, the cycle carries on from
there.

**A frontend's move of a busy cursor** (a go-to or a re-run) is an abort,
then the move:
- It is **refused** while any hook of the cursor is running, other than an
  input hook, or while a question on it is open (§7). Nothing is queued: the
  frontend may try again, or abort first.
- Otherwise it aborts the step (§8): the effects in flight are cancelled, the
  step's records are written and moved onto, and their hooks run as usual,
  through `post_step`. Post hooks may act on the aborted records, and may
  even move the cursor; the frontend's move then applies over theirs.
- Then the move resolves `to` again and applies to a cursor at rest.

The frontend's move wins, and no extension loses a hook it was owed. A move
whose resolved `to` equals `at` writes nothing and aborts nothing (step 3).

**A go-to turns auto-step off, then wakes the cursor at its destination.**
`pre_step` runs there (§7). A step follows only if an extension's `pre_step`
turns auto-step on, for example after delivering input. Otherwise the cursor
sits where the go-to put it. Browsing calls no provider and runs no tool
unless an extension chooses to. A re-run is a move plus a tools step.

A move does every check first, then writes. In order:

1. **Resolve `to`.** A move onto a batch's `Assistant` node or one of its
   results fast-forwards to the end of the existing run (I3). A re-run keeps
   `to` as given.
2. **Refuse** a move onto a copied batch that is not complete on the walk
   (I3). Refuse a re-run whose `to` leaves no call unanswered on its new line
   (for example, the completing result): there is nothing to run. Refuse a
   hook's move outside a boundary, or of another cursor. Refuse a frontend's
   move while a hook of the cursor is running or a question is open.
3. If the resolved `to == at`, stop: nothing is written. A re-run skips this
   check: `rerun(a1)` while `at == a1` runs the batch again.
4. For a go-to, a hook's move, or a re-run, apply I6 (§4). If `to` is an
   unstarted or partial batch, the restatement is deferred to the batch's
   completing result instead of written now.
5. Write: if the cursor is busy (a frontend's move), abort the step, wait for
   it to end, and resolve `to` again; if it now equals `at`, stop. Then
   append the restating `Config` if I6 needs a new one, and the move
   `{cursor, from: at, to, by, time}`, together (§10). Set `at := to`, and
   emit `cursor_moved`.
6. Afterwards:
   - a go-to turns auto-step off and wakes the cursor (`pre_step`, §7);
   - a re-run runs a tools step at once for every call still unanswered on
     the new line, without fast-forwarding. It makes a sibling run, or a run
     that branches partway. `pre_step` runs before it, as before every step.
     Afterwards the cursor continues per its auto-step;
   - a hook's move lets its hook point finish, and the cycle carries on.

A move never takes a claim. A step does, when it runs calls (§10).

A **refused** move changes nothing and raises to whoever asked for it: the
frontend, or the hook. It aborts nothing, because every check runs before
any write. A hook that catches it has handled it, and auto-step stays as it
was. A hook that does not catch it fails like any hook error (§7).

A re-run is always explicit. Every call it runs passes through `pre_tool` as
usual, so a hook may still block one, or ask a person first (§7). A hook's
re-run does not nest: the call returns once the move is written, and the
re-run's tools step is the cycle's next step. Resume after a crash never
re-runs on purpose (§8).

```
u1 ─ a1 ─ r5 ─ r3        the first run
      │    └ r3'         rerun(r5): keep c5's result, run c3 again
      └ r3'' ─ r5''      rerun(a1): run both calls again
```

**A step's own move is never refused.** Its node passed append's checks, and
I6 does not apply, except that a deferred restatement is written with the
step's move onto a completing result (I6). It is never skipped either: an
abort writes the step's records and moves onto them before anything else
moves the cursor.

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

## 7. Cycles, steps, and hooks

A **step** is one sample or one tools run for one cursor. It writes and moves
onto what it wrote, with hook points before the effect, before the write, and
after the move. A **cycle** is what a cursor does from the moment it wakes
until it comes to rest: `pre_step`, then the steps it takes, each followed by
`post_step`.

### The cycle

A cursor at rest **wakes** when:
- an item is added to its scratchpad;
- it opens, whoever opened it;
- its session loads (every cursor, in the lease holder only);
- a go-to lands it;
- a frontend steps it (`Session.step`), re-runs a batch on it, or turns its
  auto-step on.

A woken cursor runs its cycle:

```
1  pre_step
2  evaluate next(walk(at))
3  if next != Wait, and auto-step is on or the cycle owes a step
   (Session.step, a re-run): take one step, then post_step(ending), and
     if the step was aborted:                            rest
     if auto-step is on and next(walk(at)) != Wait:      go to 1
4  if the scratchpad changed since the last pre_step started,
   and the cycle was not aborted:                        go to 1 (throttled)
5  rest
```

A cursor is **busy** from the moment it wakes until it rests. Busy is driver
memory, never stored. A frontend's `Session.step` on a busy cursor is refused:
it is already stepping, and nothing is queued. `Session.rerun` on a busy
cursor is a move like a go-to (§5): refused while a hook runs, otherwise an
abort first. Turning auto-step on for a busy cursor takes effect at its next
check (step 3).

**Throttle.** A `pre_step` that no step separates from the previous one
starts at least 250 ms after the previous one started. If an extension's
`pre_step` took 80 ms and left the cursor at rest, the next one waits 170 ms.
The cursor is at rest while it waits; a wake during the wait joins the
`pre_step` already scheduled. Nothing polls: an item an extension leaves in
place causes no further `pre_step` until the scratchpad changes again. An
extension that must poll removes its item and adds a new one.

### Hooks

Every hook has one shape: **`hook(cursor, subject) → outcome`**.
- `cursor` is a handle to the cursor the hook runs for, bound to the hook's
  extension. That binding is how the driver fills in `by` on whatever the
  hook writes.
- `subject` is what the hook point is about: the input, the context tip, the
  reply, the call, the call and its outcome, the written node, or how the
  step ended.
- At each hook point, hooks run in the order their extensions are listed in
  `config_at(at)`, and each receives what the previous one returned.

Through its cursor handle, any hook may, at any hook point:
- **read** the tree and every cursor's position: the cursor's `at`, its
  `next`, whether it is `busy`, its auto-step, and the whole session;
- **append** side nodes anywhere the append rules allow, including detached
  roots;
- **write scratchpads**: add an item to any cursor's scratchpad, and update
  or remove any item;
- **ask** a person (`cursor.ask`), **show** frontends state
  (`cursor.show`), and **tell** one client something (`cursor.tell`)
  (§9);
- set its own cursor's auto-step, and open cursors;
- sample a walk and run a tool outside any step (a cold summary on a
  detached root, for example).

At a **boundary** it may also act on its own cursor's line:
- `cursor.write(body, …)`: append under `at` and move onto it, as one durable
  write (§10);
- `cursor.deliver(items)`: write the input items hold as `User` nodes under
  `at` and move onto the last (below);
- move or re-run its own cursor.

The boundaries are the hook points where none of the step's effects is in
flight: `pre_step`, `post_reply`, `post_result` once its batch is complete on
the line, and `post_step`. Elsewhere these calls are refused.

An extension **reads its own cursor's scratchpad** only in `pre_step` and
`post_step`. Frontends may read any scratchpad at any time (§9).

What an action does follows from the tree and the cursor, the same for a
hook as for a frontend: append's checks (I4) and a move's checks (I3, I6)
apply as usual. Nodes and moves a hook writes trigger no hooks. A *step* a
hook causes (the one after a "continue", or a re-run's tools step) runs
hooks as usual, and so does the cycle of a cursor its item wakes.

### Hook points

| hook point | runs | subject | outcome |
|---|---|---|---|
| `pre_input` | on `Session.input`, busy or at rest, outside the cycle | the input | handled, pass (the input, possibly rewritten), or refuse (a reason) |
| `pre_step` | when the cursor wakes, before every step, and when its scratchpad changed (above) | nothing | nothing |
| `pre_sample` | before a sample | the context tip | a context tip, or a reply (answered) |
| `pre_reply` | before a reply is written | the reply | the reply, its parts possibly rewritten, or nothing (refused) |
| `post_reply` | after a sample step's move | the written `Assistant` node | nothing |
| `pre_tool` | before each call runs | the call | the call, possibly rewritten, or an outcome (answered) |
| `pre_result` | as each call finishes | the call as it ran, and its outcome | an outcome of the same kind |
| `post_result` | after each result's move | the written result node | nothing |
| `post_step` | after every step, aborted ones included | how the step ended | nothing |

`post_step`'s subject, the **ending**, is one of:

| ending | when | carries |
|---|---|---|
| `wrote` | the step wrote and moved onto its node: a reply (any `stop`), or the result that completed its batch | the last node it wrote |
| `refused` | `pre_reply` refused the reply, or no reply could be appended under the tip | nothing |
| `failed` | the sample failed before any output, or a hook error ended the step | the error |
| `aborted` | an abort, or a frontend's move of the busy cursor, stopped it (§8) | the last node it wrote, if any |

### Steps

| step | starts when | pre hook (effect) | effect | pre hook (node) | writes, then moves onto | post hook |
|---|---|---|---|---|---|---|
| sample | `next == Sample` | `pre_sample` | provider request | `pre_reply` | one `Assistant` node | `post_reply` |
| tools | `next == RunTools` | per call: `pre_tool` | run the tool | per call: `pre_result` | one result per call, each moved onto as it is written | per result: `post_result` |

Every step is followed by `post_step`.

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
5. **post hooks** (`post_reply`, `post_result`) on the written node;
6. **`post_step`**, once, with the step's ending.

**Where a step writes.** Each node a step writes goes under the node it was
produced from: a reply under the context tip it was sampled from (or I6's
restating `Config`), a result under the previous node of its own run. Its own
move then lands on it. An abort writes the step's records the same way and
lands on them too (§8), so they are on the line and committed.

Each step counts toward `max_steps`, and only actions `by: frontend(…)`
reset the count; when it reaches `config_at(at).max_steps`, auto-step turns
off (Q20). `pre_step` is not a step and does not count.

What each pre hook returns:
- **`pre_sample`** may return a different context tip: a node it appended under
  the tip (a reminder, Q30), a `Config` node (a model choice, which persists,
  Q26), or the end of a new line it built (compaction, Q25). The driver
  applies I6 and samples `walk(tip)` with `config_at(tip)`, appending the
  reply under the tip, or under I6's restating `Config` if there is one.
  Before calling the provider, the driver checks that a reply could be
  appended under the tip (I4); if not, it refuses the step. The step's move
  onto the reply commits those nodes too. The hook may instead **answer**
  with a reply (a cache hit), and then the provider is not called.
- **`pre_reply`** may rewrite the reply's parts (a guard, redaction), but not
  its `stop`, which is a fact about the sample. Or it may return nothing to
  **refuse** it. A refused reply, even one with calls, writes nothing; its
  calls never existed in the tree.
- **`pre_tool`** may rewrite the call (a sandbox, a remote host). It may also
  answer with an outcome and skip the tool: a cached or mocked `Ok`, or
  `Skipped("blocked by …")`. It may ask a person first (`cursor.ask`).
- **`pre_result`** receives the call as it ran (after `pre_tool`'s rewrites)
  and its outcome. It may rewrite the outcome's content but not its kind
  (Q18). It must return an outcome, because every call is owed a result (I4).

An answer from `pre_sample` or `pre_tool` skips the remaining hooks at that
point and the effect. The node's pre hooks still run, so a cache hit is still
redacted (Q18). A refusal ends the step, and no further hooks of that step
run except `post_step`.

**Results core writes skip `pre_result`.** `Err("invalid arguments")`,
`Interrupted`, and `Err("hook error")` have no tool output to rewrite, so no
`pre_result` runs for them. They are written and moved onto, and their
`post_result` hooks run like any result's. **Post hooks run for every node a
step writes and moves onto**, whoever authored it, aborted steps included.

**Post hooks** receive the written node after the cursor has moved onto it.
Every post hook at a point runs on the node that was written, in config
order. A later hook sees the cursor wherever earlier hooks left it: its
`cursor.at` may no longer be the node it was given. If two post hooks both
move, each move is a real move, in order. For example, a `post_reply` hook
writes `User "continue"` under the reply with `cursor.write`, so `next`
becomes `Sample` (Q20). That works for any reply that is not a batch: any
`stop` except `tools`, or `tools` with no calls. Under a batch, only results
may be appended (I4). A `post_result` hook before its batch is complete is
not at a boundary: calls are still running, so it may not move or write
under `at`.

When `post_step` returns, the cycle continues as above, from wherever the
cursor now is.

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
- In a post hook, `pre_step` or `post_step`, any node is already written and
  moved onto; the cycle carries on with auto-step off.

A refused move or append raises into the hook that asked for it. Caught, it is
handled, and auto-step is unchanged; uncaught, it is a hook error like any
other.

A step that ends without moving its cursor (a refusal, or a sample that failed
before any output) **turns auto-step off**. Without that rule, a refused or
failed sample would repeat forever: `walk(at)` hasn't changed, so `next`
hasn't either.

### Between cursors

A hook acts only on its own cursor. To ask another cursor for something, an
extension adds an item to that cursor's scratchpad. Adding wakes it, and its
own extensions' `pre_step` hooks decide what to do. If the receiving cursor
is busy, its next `pre_step` sees the item: before its next step, or after
its `post_step`, because the scratchpad changed. Nothing an extension does
reaches into another cursor's step.

`to` names a cursor, not an extension. Extensions work with each other
through items without permissions: any extension may read the items its
cursor receives (in `pre_step` and `post_step`), and any may update or remove
any item. An extension that wants only its own items checks `from`.

**A sub-agent** (Phase 5) is a tool that, through its handle:
1. opens a cursor (`opened_by` = the calling cursor), for example on a
   detached root;
2. adds the task as an item to the new cursor's scratchpad;
3. waits until the new cursor rests at `next == Wait`, and answers the call
   with a value whose `derived_from` lists the cursor's final node.

The new cursor inherits its opener's auto-step and `max_steps` count, wakes
on opening, and an extension's `pre_step` there delivers the task. The tool
drives nothing: it only waits. Details are Phase 5.

### Input

`Session.input(cursor, input)` is called through a client handle, which
supplies the client id. It is refused at once if a question is open on the
cursor (below). Otherwise it runs that cursor's `pre_input` hooks, busy or at
rest. Input chains for one cursor run one at a time, in the order they
arrived, and outside the cycle: no other hook waits for them, and they wait
for no other hook. Nothing else in core looks at input: core has no default
input behavior. Each hook returns one of:
- **handled**: the hook has done what it meant to, typically adding an item
  to the cursor's scratchpad, which wakes the cursor;
- **pass**, with the input, possibly rewritten, to the next hook;
- **refuse**, with a reason.

The chain stops at the first hook that handles or refuses. If every hook
passes, the input is refused: "no extension handled this input". A refused
input changes nothing. `Session.input` raises the refusal to the frontend,
which keeps the text.

`pre_input` is not a boundary: it may not write under `at` or move the
cursor. What input causes on the line happens in `pre_step`, so it always
happens between steps, never inside one.

A frontend's **send** is input plus auto-step on. Typing while the agent
works is the same call: the input becomes an item, and `pre_step` delivers it
at the next point the line can take it. **Interrupt and send** is two
frontend actions: abort (§8), then send. The input lands under what the
abort left. To put it elsewhere, go there first.

**The base extension** ships in the default config, and a config may replace
it. It gives input its everyday behavior:
- **`pre_input`** adds the input as an item, `{from: {frontend: client,
  extension: base, cursor}, to: cursor, data: {text, images}}`, and returns
  handled. It does the same whether the cursor is busy or at rest.
- **`pre_step`** delivers the cursor's open base items, in order, when `at`
  can take a `User` node and its `deliver` setting allows. It does nothing at
  an unstarted or partial batch, where no `User` node may be written (I4).
- **`post_step`**, when the ending is `aborted`, **returns** its open items
  to the people who typed them: for each, it tells the item's client
  (`cursor.tell`). If the client got it, the item is removed. Otherwise the
  item is updated to `returned` and stays, so every frontend can show it with
  send and edit buttons. `pre_step` skips returned items.

Its settings, in `Config`:
- `deliver: after_batch` (the default) delivers at any `pre_step` where `at`
  can take a `User` node. After a tools step, that is the `pre_step` before
  the next sample, once the batch is complete. Never mid-batch.
- `deliver: after_stop` delivers only at a `pre_step` where
  `next(walk(at)) == Wait`.
- `combine: true` (the default) writes one `User` node per run of consecutive
  items from one client, texts joined by a blank line. `combine: false` writes
  one `User` node per item. Either way the nodes are one durable write, and
  the cursor moves onto the last.

**Delivery** is `cursor.deliver(items)`. It writes each `User` node with
`delivers` listing the items it holds, moves the cursor onto the last, as one
durable write, and then removes the items from the scratchpad. A delivered
node's author is `frontend(client)`, the client that typed it, because its
content is that person's text; an item with no client is authored by the
delivering extension. **`delivers` is the record of delivery.** An item that
some node delivers is gone, even if a crash came before its removal (§10).
Delivery is a hook's write, so it runs no input hooks. A cursor at rest
woken by the input runs its cycle as usual: with auto-step on (a send), the
delivered `User` node is sampled at once; with auto-step off, it waits.

**Input at a batch** (Q17). A cursor may rest at an unstarted or partial
batch: paused, or after a reload. Input there becomes an item, and the base
extension delivers nothing until the batch is answered. A send turns
auto-step on, so the tools step answers the batch (a partial run by the
resume rules, §8), and the `pre_step` before the next sample delivers the
input.

Other input behaviors are extensions too:
- **A command**: a hook that handles `/model fast` by adding an item for its
  own extension, passing anything else. Its `pre_step` writes the `Config`
  node with `cursor.write` at the next point the line can take one.
- **A gate**: a hook that refuses input until something is satisfied.

**Going somewhere without new input** (Q15). A frontend may go to any node
with `Session.move`. This is how a person branches, browses or returns to an
older line, or rejects a reply (go to its parent, then step). The go-to turns
auto-step off and wakes the cursor where it lands; a step follows only if an
extension starts one. `Session.rerun` is the one frontend move that always
starts a step (§5).

### Questions

A hook that needs a person calls **`await cursor.ask(target, question)`**.
`target` is one client, or any client. The question goes out as an event,
and the hook waits for **an answer** or **declined**. Core does not look at
either: the question's shape and its answers belong to the extension. A
tool-permission extension's `pre_tool`, for example, asks "run `bash rm -rf
build`?" and, if declined, answers the call `Skipped("permission not
granted")`.

While a question is open, the cursor is **locked**:
- the frontend's input, go-to, step, and re-run on that cursor are refused,
  not queued;
- `Session.answer(cursor, question, answer)` answers it, and
  `Session.decline(cursor, question)` declines it;
- abort is allowed. It cancels the effects in flight (other calls of the
  batch, for example), whose results are written as usual, and leaves the
  question open (§8).

A question for one client is declined if that client detaches. A question for
any client is replayed to every client that connects (§9). Questions are
driver memory: a crash loses them, and resume answers the waiting call
`Interrupted` (§8, D4).

### What still holds

- **Fast-forward.** A hook's move resolves `to` like a go-to (I3). A
  frontend's move of a busy cursor resolves `to` only after the abort's
  records are written and moved onto, so it lands at the end of the run
  those records complete, never above them.
- **Claim.** Only a step running calls holds one (§10). An abort cancels the
  calls, writes their `Interrupted` results, then releases the claim. A step
  that is aborted while it waits for a claim has written nothing and simply
  ends.
- **Resume.** A cursor at the end of a partial run takes the claim when it is
  stepped, re-reads the run, and fast-forwards as its own move (§8).
  Aborting a resume answers its remaining running calls `Interrupted`.
- **`max_steps`.** Every step counts, including one a hook's move or re-run
  causes. An abort is not a step. Only actions `by: frontend(…)` reset the
  count (Q20); a delivered input is the base extension acting, but the
  frontend's `Session.input` that produced it already reset the count. A
  cursor a hook or tool opens inherits its opener's count and auto-step
  setting, so a sub-agent cannot reset a runaway loop by opening a cursor. A
  cursor a frontend opens starts at 0, with auto-step off.

## 8. Abort, failure, reload

- **Abort.** `Session.abort(cursor)` turns the cursor's auto-step off and
  cancels the effects of its step that are in flight: the sample stream, or
  the tools that are running. **It never cancels a hook.** No new effect
  starts in that step. Then the step writes its records, each under the node
  it was produced from, **its own move lands on them as usual**, and their
  hooks run as they would for any write.
  - **During a sample**, if no parts had arrived: nothing is written, and
    the cursor does not move. Nodes `pre_sample` built for this sample are
    uncommitted.
  - **During a sample**, if parts had arrived: `pre_reply` runs, the reply is
    written with the parts so far and `stop: interrupted`, and moved onto;
    `post_reply` runs; `next` is `Wait`. Its calls, including one cut off
    mid-arguments, are not runnable and owe no result (§2). The user saw
    those tokens, so they are stored and the model sees them next time
    (§11).
  - **While `pre_sample` runs**: the hook finishes, and the sample is not
    started. Nothing is written for the reply.
  - **While `pre_reply` runs**: the hook finishes, and the reply it returns
    is written with its sampled `stop` (the sample had finished) and moved
    onto.
  - **During tools**: results already written stay. Each call whose tool is
    running is cancelled and answered `Interrupted` (`by: core`), and so is
    each call that had not started. A call whose `pre_result` is running is
    not cancelled: its hooks finish and its outcome is written, redacted as
    usual. A call whose `pre_tool` is running is not cancelled either: if the
    hook answers with an outcome (`Skipped` after a declined question, say),
    that outcome is written; if it returns the call to run, the call is not
    started and is answered `Interrupted` (Q34). Each result is written and
    moved onto in turn, and its `post_result` hooks run. An open question
    keeps the step from ending until it is answered or declined.
  - **While waiting for a claim**: the step ends, having written nothing.
  - **Then**: `post_step` runs with ending `aborted`. The cycle does not run
    `pre_step` again; the cursor rests (§7). The base extension's
    `post_step` returns pending input to the people who typed it.
  - **At rest**, abort only turns auto-step off.
- **A frontend's move of a busy cursor** is an abort, then the move, then
  what follows the move: a go-to wakes the cursor at its destination, and a
  re-run runs its tools step (§5).
- **A sample fails.** This covers HTTP errors, a stream that breaks partway,
  and the provider's conversion rejecting the walk. Auto-step goes off either
  way (Q4). Hooks run as for any write.
  - **Output had arrived:** the reply is written and moved onto like an
    aborted one, but with `stop: error`. `stop_raw` and the error text are
    kept. `post_reply` runs, and `post_step` with ending `wrote`.
  - **Nothing arrived** (for example a 401 or 429 before the first token):
    there is no reply. The driver records the error as a `Custom
    {kind: sample_error}` node under the context tip. It is persisted and
    visible, but it is uncommitted, not part of the conversation, and the
    cursor does not move. `post_step` runs with ending `failed`.
- **A tool fails.** That is an `Err` outcome, written like any result. It is
  not an abort.
- **Length, refusal.** These are ordinary replies with `stop: length` or
  `stop: refused`. Their calls owe no result, and `next` is `Wait`. Continuing
  automatically after `length` is an extension's choice, for example a
  `post_reply` hook that writes "continue" with `cursor.write` (Q20).
- **Resume at a batch.** A cursor may sit at the end of a partial run: after
  a reload, after an abort or a paused cursor, or because a move
  fast-forwarded there (I3). When it is next stepped:
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
  (§10), and so are a hook's `cursor.write` or `cursor.deliver` and its move,
  so a crash leaves both or neither. A torn final record is ignored on load.
  A crash after a delivering write but before the items' removal leaves the
  items in the scratchpad file; load drops them, because a node delivers them.
- **Crash during a sample.** On reload `next` is `Sample`, so resume samples
  again.
- **Reload.** Every cursor loads at rest, with auto-step off, no open
  questions, and nothing shown to frontends. Then, in the lease holder only,
  every cursor wakes, and `pre_step` runs (§7). That is how the base
  extension delivers input that was still pending when the process died, if
  `at` can take a `User` node; the cursor then waits, auto-step off, for the
  frontend.

## 9. Events and frontends

There is one `Event` type, and every event names its cursor:

| kind | when | carries |
|---|---|---|
| `node_appended` | any append, including uncommitted nodes | the node |
| `cursor_moved` | after a move record is written | `from`, `to`, `by` |
| `stream_delta` | during a sample | a fragment of text, thinking, or a call |
| `item_changed` | an item is added, updated, or removed | the item, or its id when removed |
| `question` | a question is asked, answered, declined, or withdrawn | the question, its target, and its state |
| `status` | an extension shows state (`cursor.show`) | the extension and its state |
| `notice` | an extension tells one client something (`cursor.tell`) | the client and the message |

Observers cannot block the driver or change anything. Print/json, RPC, serve,
and the TUI are each a projection of this stream. A `notice` goes only to its
client; every other event goes to every subscriber.

**Clients.** A frontend serves one or more **clients**: a terminal, or each
connection to `tau serve`. Every client has an id, and everything a client
does is recorded as `by: frontend(client)`. The id is recorded only, never
used for control flow, except that a question or a notice may be addressed to
one client.

**What extensions show.**
- **`cursor.show(state)`** sets the calling extension's state for its cursor,
  replacing what it showed before. Every frontend renders it, for example
  "2 inputs returned". It is driver memory: a reload clears it, and the
  extension rebuilds it when the cursor wakes. State that must survive a
  reload is the extension's to write into the session, as nodes or items.
- **`cursor.tell(client, message) → bool`** sends a message to one client,
  for example "put this text back in your input box". It returns whether the
  client was attached to receive it, so the extension can fall back to
  `show` or an item.

**`tau serve` comes first** among frontends. It multiplexes many clients over
the network onto the same sessions and cursors. When a client connects, it
sends that client every cursor's current `status`, the open questions it may
answer, and the scratchpads, so a client that attaches late sees what one
attached early saw. No `on_connect` hook exists until an extension needs
per-client setup (Q23). The `Harness` (Phase 2) is shaped like serve: several
clients per session, and events as the only feed.

**No cursor waits on a particular client.** Events go to whoever is
subscribed, possibly nobody. A client may switch to another cursor or another
session at any time, and every active cursor keeps operating: input for a
busy cursor is an item, not a frontend buffer, and nothing a step does blocks
on an observer. A cursor waits on a person only through a question, which any
client may answer unless it was asked of one.

## 10. Persistence and processes

- `tau-store` keeps, per session, one append-only **record log** and one
  **scratchpad file**, each with a versioned header (D12). Loading reads both,
  replays the log, and reconciles the scratchpad (I7).
- **Records.** Each record in the log is one durable write: one or more nodes
  and at most one move. Each is length-prefixed and checksummed, so a torn
  final record is detected exactly, and load ignores it (I7).
- **Durability.** A node is durable before any move that names it. A step's
  write and its move are one record, and so are a hook's `cursor.write` or
  `cursor.deliver` and its move, and an I6 restatement and the move onto it:
  a crash leaves both or neither.
- **The scratchpad file** holds the current items and the next item id. It is
  rewritten whole for each change, atomically (write a new file, sync it,
  rename it over the old one), so it is never torn. It is not a log: a
  removed item is gone from disk. Adding an item is durable before the cursor
  it wakes is woken.
- **Delivery order.** A delivering write (its nodes, `delivers`, and the
  move) is made durable first, and the items are removed after. Load drops
  any item a node delivers, so a crash between the two neither loses nor
  repeats an input, and the input's content is stored once, in its node. An
  item consumed without a node (a discarded returned input, a message an
  extension acted on without writing) is only removed; a crash before the
  removal leaves it in place, so such consumers must tolerate seeing an item
  twice.
- **Lease.** There is one `flock` per session, taken on the record log. The
  holder writes; every other process gets the session read-only. Only the
  holder runs cycles and hooks, the wakes on load included: a read-only
  process never runs a hook. If the holder's process is dead, the lease is
  reclaimed. 0.15 left a stale holder record that nothing checked.
- **Claim.** The driver holds an in-memory claim on a batch's `Assistant`
  node only while a step is running that batch's calls: a tools step, a
  resume, or a re-run's tools step. The step takes the claim before it runs
  any call and re-reads the run once it has it (§8 resume). A move never
  takes a claim, and no move is refused because of one. A step that finds
  the claim held waits, because the holder is by definition running; it then
  re-reads the run and fast-forwards instead of running the calls again
  (Q13). So two steps never run one batch at once, a re-run included. An
  aborted step releases its claim once its `Interrupted` results are
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

## 12. Core and driver

Every rule in this document is decided in `tau-core`, in Rust (D11). Each
cursor's cycle is a sans-IO state machine: it never waits, and it does
nothing itself. It hands out **jobs**, and the Python driver performs them
and feeds back the results:

```
cycle = session.wake(cursor, reason)          Rust
loop:
    job = cycle.next_job()                    Rust: run a hook point | sample | run tools
                                              | persist a record | wait (a claim, the throttle)
                                              | rest
    result = await driver.perform(job)        Python: hooks, providers, tools, the store
    cycle.feed(result)                        Rust: validates, decides, advances
frontend calls (abort, move, input, answer)   fed to the same state machine
```

A tools job holds several calls; the driver feeds their completions back one
at a time, in any order, and core serialises the writes. Where a job may
wait (a hook asking a person, a stream, a tool), an abort arrives as one more
input to the machine, which decides what is cancelled. Time comes in with
each result.

So the driver is small: it runs Python hooks, providers and tools, awaits
them, and cancels the jobs core says to cancel. Hook order, when `pre_step`
runs, the throttle, boundaries, I3, I4, I6, fast-forward, the claim, abort,
the result order, and `max_steps` are all Rust, and all reachable from
proptest without Python or a clock.

## Open questions

Each one has a recommendation, which is what the documents assume. A "no"
means a document changes before sign-off. Q14 and Q19 were folded into the
review decisions and Q20, and Q29 into the decision that only a
`stop == tools` reply is owed results. The sixth revision settles Q22 and
Q31 and adds Q33–Q38.

| # | question | recommendation |
|---|---|---|
| Q1 | The plan names the node timestamp `at`, the same as `Cursor.at`. Rename? | **`time`.** One word, one meaning. |
| Q2 | Is "committed" permanent, or does a node stop being committed once every cursor has moved off it? May a move land on an old uncommitted node, such as an extension's side line? | **Permanent, and yes.** Committed is derivable from moves, and cursors can reopen on old lines. A rejected reply stays committed; the line simply moved away from it. Validation still guards every move. |
| Q3 | Does a `Custom` node ever reach the model? | **No.** To show the model something, append a message-bearing node. That keeps `walk` honest (I1) and the provider conversion small. The same holds for scratchpad items. |
| Q4 | Failure and abort. The plan discards a failed step, but abort during tools must still answer every call or I4 breaks, and tokens the user saw should not vanish. | **As in §8.** A sample that produced output is written with its `stop` (`interrupted` or `error`). A sample that produced nothing leaves at most an uncommitted `sample_error` node. An aborted tools step answers unwritten calls `Interrupted`. Every abort happens in place: the cursor moves onto what was written, hooks run on it, and only then does a frontend's move apply. |
| Q5 | Roots and opening. I3 says cursors open at committed nodes, but an empty session has none. | **Every root is a `Config` node.** `Session.create(config, system)` appends the root `Config` and a `System` node under it (both `by: core`), and opens the first cursor at the `System` node with an opening move (a move with no `from`, which the plan doesn't have). Detached roots are `Config` too, so each records the model it used; a root a hook creates is authored `extension(name)`, filled in by the driver like any append. |
| Q6 | What does `opened_by` name? | **The opening cursor.** A tool receives a handle bound to the calling cursor, so a cursor a tool opens (a sub-agent) has `opened_by` = the calling cursor; a hook's records its own cursor; a frontend's is empty. The sub-agent opens on a line outside the parent's batch, for example a detached root (§7). Its answer carries `derived_from`. |
| Q7 | What is the set of authors? | **`frontend(client)`, `provider`, `tools`, `core`, `extension(name)`.** A node's author is whoever produced its content as written. A hook's answer or rewrite makes its extension the author, and so does a copy. A delivered input is the typing client's. No caller supplies the author: the driver fills it in from the caller (§5). Authors are recorded only, never used for control flow. |
| Q8 | Node id format? | **A session-local integer assigned at append.** It is deterministic, which suits I7 and tests. JMFTS (Phase 7) maps its own ids. Item ids are session-local integers too, never reused. |
| Q9 | 0.15 had handler policies (timeouts on hooks). Keep any? | **Not in core.** A hook that needs a deadline uses `asyncio.timeout`. |
| Q10 | Is a written result final? | **Final on its line; another answer is another line.** Two answers to one call on one line could each be "the" answer, so I4 forbids it. A deliberate re-run (§5) or a branch from higher up gives the call another answer on another line. |
| Q11 | Where does provider-specific legality live? | **In the provider's conversion**, which the driver runs before every sample. A rejection is a failed sample. Core stays provider-agnostic. |
| Q12 | Do cursors close? | **Not in core for now.** Cursors are cheap, and frontends hide finished ones. Closing would need a new record and an ADR. |
| Q13 | Two cursors at one batch both have `next == RunTools`. | **Run once**: an in-memory claim per batch, held while a step runs its calls (§10). The second cursor's step waits, then re-reads the run and fast-forwards onto it. This becomes a Phase 2 scenario. |
| Q15 | How does a person branch or go back? | **Three frontend operations.** "Go to" (`Session.move`) is a move; it turns auto-step off and wakes the cursor where it lands, so a step follows only if an extension starts one. On a busy cursor it aborts the step first, and it is refused while a hook runs or a question is open. A re-run (`Session.rerun`) is a move plus a tools step. Input (`Session.input`) goes to the cursor wherever it is, busy or at rest, and its input hooks decide what happens; to give input elsewhere, go there first. `Harness` exposes all three. |
| Q16 | A go-to across a config change restates the `Config` *under* the node it goes to. Where does the restatement go when `to` is an unstarted or partial batch, where I4 forbids appending? | **Deferred to the first legal place** (I6). The move lands on the batch without a restatement, and the run uses `config_at` of its own walk, as every run does. When a step's own move lands the cursor on the result that completes the batch, it writes the restating `Config` under that result (or reuses one) and moves onto it, in one durable write. What is owed is read off the move log, so it survives a reload. Cost: the batch's calls run under the destination line's config (its tools, its hooks), which is what that line chose. |
| Q17 | Input arrives while `at` is an unstarted or partial batch: a paused cursor, or after a reload. | **The base extension holds it until the batch is answered.** The input is an item either way; `pre_step` delivers nothing while no `User` node may be written (I4). A send turns auto-step on, so the batch runs (a partial run by the resume rules: idempotent calls run again, the rest are `Interrupted`), and the input is delivered before the next sample. This retires `Skipped("superseded by input")`: an extension cannot tell whether a call started before a reload, and the tree cannot either. Cost: a person who paused at an unstarted batch and then sends sees the batch run first. To skip it, they go back to the reply's parent and step again. |
| Q18 | Which hooks run after a pre hook answers, and what may `pre_result` change? | **The node's pre hooks run** (`pre_reply`, `pre_result`), so a cache hit still gets redacted. `pre_result` may change an outcome's content but not its kind: it cannot turn `Skipped` into `Ok`. The driver enforces that. |
| Q20 | Where does "keep going" live (a hook that sends more input after the model stops), and what stops a runaway loop? | **In post hooks.** A `post_reply` hook writes a `User` node (`by: extension(name)`) under any reply that is not a batch (any `stop` except `tools`, or `tools` with no calls) with `cursor.write`, and `next` becomes `Sample`. As a hook's write, it runs no input hooks. A runaway loop is stopped by `max_steps` in `Config`: the most steps a cursor takes without the frontend acting on it. The driver keeps an in-memory counter per cursor, reset only by actions `by: frontend(…)` on that cursor (giving input, stepping it, re-running, turning auto-step on). A cursor a hook or tool opens inherits its opener's count and auto-step setting; one a frontend opens starts at 0 with auto-step off. When it reaches `config_at(at).max_steps`, auto-step turns off and the cursor pauses. `next` stays a pure function of the walk. Nothing is persisted: every cursor loads with auto-step off (I7), so a runaway loop cannot survive a reload. Two cursors trading items forever are bounded the same way, because each delivery leads to steps that count. |
| Q21 | Do a batch's calls run in parallel? | **Yes, by default.** A tool can be `exclusive` and run alone, for example two edits to one file. |
| Q22 | How does a person approve one call and deny another? | **Settled (sixth revision): `cursor.ask`.** A `pre_tool` hook asks, and the cursor is locked until the question is answered or declined (§7). Abort does not close it. The question is driver memory, replayed by `tau serve` to clients that connect; a crash loses it, and resume answers the call `Interrupted`. The same mechanism lets a `pre_reply` hook show a reply to a person before it is written. |
| Q23 | 0.15 had lifecycle hooks (`session_start`, `session_shutdown`, `session_before_switch`). | **None until a Phase 5 extension needs one.** None of the three does. `pre_step` on load covers what `session_start` did for cursors. An `on_connect` hook for per-client setup waits for an extension that needs it; `tau serve` replays shown state on connect without one (§9). |
| Q24 | When `pre_tool` rewrites a call (a sandbox, say), is the executed call recorded? | **Not by core.** The `Assistant` node keeps the call as the model made it, and the result answers it. A hook that wants an audit trail writes a `Custom` node on a detached root, because nothing but results may hang off a batch that is not complete (I4). |
| Q25 | May `pre_sample` change the request only by returning a context tip, never by editing the request invisibly? | **Yes.** That is I1's guarantee. Reminders, model choices, and compaction are all visible in the tree. |
| Q26 | A `pre_sample` hook that picks a cheaper model appends a `Config` node, and that choice persists on the line. Should one-off choices revert on their own? | **No automatic revert.** A routing hook that wants a one-off appends the restoring `Config` at its next `pre_sample`. The tree then shows exactly which model produced each reply, and nothing reverts behind anyone's back. |
| Q27 | What does a call that was never run (its reply's `stop` was not `tools`) become when the walk is sent again, and what does τ's markup look like? | **`partial_calls: text` by default**, rendered as `[τ: interrupted while calling write_file; arguments so far: …]` after the reply's text. `partial_calls: omit` drops it. Stop notes use the same `[τ: …]` form. The exact markup is pinned in the Phase 4 provider ADR and tested byte for byte, because changing it later invalidates every cached prefix that contains it. |
| Q28 | Hooks can move a cursor. Should a move record say who asked for it? | **Yes: `{cursor, from?, to, by, time, opened_by?, label?}`**, with `by` one of `frontend(client)` (a go-to, a re-run, an opening), `core` (a step's own move), `extension(name)` (a hook's move, write, delivery, re-run, or opening), or `tools` (an opening by a tool). No API caller supplies it: the driver fills it in from the caller, as it does a node's author. Like a node's author, it is recorded only, never used for control flow. |
| Q30 | What body kind is a compaction summary or a `pre_sample` reminder? | **`User`, authored by the extension** (`by: extension(name)`). It is legal on every API's wire, where `System` is legal only at the start of the conversation for several APIs. `Custom` never reaches the model (Q3). |
| Q31 | When does the base extension deliver, and what sees a delivered input? | **Settled (sixth revision): in `pre_step`**, per `deliver` and `combine` (§7). Every input goes through the scratchpad, busy or at rest, so there is one delivery path. `post_input` no longer exists; an extension that must see every message a person sent watches `node_appended`, or reads the cursor's items in `pre_step`. |
| Q32 | Do opened cursors run hooks? Nodes and moves a hook writes otherwise trigger no hooks. | **Yes: opening wakes the cursor**, and its `pre_step` runs, in the lease holder. It is the new cursor's own cycle, not a hook on the opener's machine, so it does not re-enter the hook that opened it. A tool's handle stays narrower than a hook's: it may read, append, sample a walk, run a tool, open cursors, and write scratchpads, but it acts on no cursor's line, so a tool cannot touch the step that runs it. |
| Q33 | What is a cursor id? | **A session-local integer, assigned by the opening move, plus an optional `label`** on that move. A label is unique in the session and never changes; `Session.cursor(label)` looks it up through a map load builds. Renaming would need a new record and an ADR. Integers match node ids, keep I7 deterministic, and are cheap to store in items and move records; UUIDs pay off only when ids cross writers, and the lease means there is one. |
| Q34 | An abort arrives while a call's `pre_tool` is still running, for example waiting on a question. What happens to that call? | **The hook finishes; no new effect starts.** If `pre_tool` answers (a `Skipped` after a declined question), the answer is written. If it returns the call to run, the call is not started, and core answers it `Interrupted`. The person stopped the step; approving afterwards does not restart it. |
| Q35 | May a cursor open on an uncommitted node, such as a detached root a sub-agent's tool just wrote? | **Yes, if its walk validates.** The opening move commits the walk, as any move does. I3 still holds: a cursor's `at` is committed the moment it is set. |
| Q36 | Who authors a delivered input when items from two clients are combined? | **Each client's own node.** `combine` joins only consecutive items from one client, so every delivered `User` node has one author, `frontend(client)`. |
| Q37 | A question asked of one client: what if that client detaches? | **It is declined.** The extension's decline path decides what follows. A question for any client is not declined by any one client leaving. |
| Q38 | Where may a hook move its cursor or write under `at`? | **Only at boundaries**: `pre_step`, `post_reply`, `post_result` once its batch is complete, and `post_step`. Elsewhere an effect is in flight, and a move there would need deferred moves and partial interrupts, which the fifth revision had and this one removes. A pre hook that wants another outcome returns one (rewrite, answer, refuse); a post hook moves after its step's work is written. |
