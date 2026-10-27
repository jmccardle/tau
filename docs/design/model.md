# Model

Status: **for review (Phase 1), seventh revision.** Expands `docs/plan.md` §1.
On sign-off this file and `glossary.md` supersede the plan. Every term used
here is defined in `glossary.md`. D1–D12 are the decisions in
`decisions/`. Questions marked **Q*n*** are listed at the end, each with a
recommendation; the documents assume the recommendation.

## Decided in review

Revisions one to six (2026-10-26, 2026-10-27) are summarised where they still
hold:
- **Calls stay inside the `Assistant` node** that produced them. One sample
  produced them all at once, so no call is a node of its own.
- **A reply is stored as it streamed, with a stop reason.** An `Assistant`
  body is an ordered list of parts (text, thinking, calls), each call's
  arguments kept as the raw string the model produced. If the user saw
  tokens, they are stored. What a truncated call becomes on resubmission is a
  config option (`partial_calls`, Q27).
- **Only a `stop == tools` reply runs calls.** Calls in a reply that stopped
  for any other reason are not runnable, and the provider renders them as
  text.
- **Nobody passes `by`.** The driver fills in a move's `by` and a node's
  author from the caller.
- **Every write is followed by its move.** A cursor is `{id, at}`. There is
  no `target`, draft pointer, hold, or discard (D6).
- **Hooks act only on their own cursor.** Cursors talk to each other through
  the **scratchpad**: a persistent, modifiable list of **items** off the tree.
  Anyone may add, update, or remove any item, and adding one wakes the cursor
  it is addressed to. A node lists the items it consumed in **`delivers`**,
  and load drops every delivered item, so an item's content is stored once.
- **Abort cancels effects, never hooks.** Hooks run normally on what was
  written. A frontend's go-to on a busy cursor is an abort, then the move; it
  is refused while a hook runs or a question is open.
- **Questions.** `cursor.ask` asks a person and locks the cursor until it is
  answered or declined.
- **Frontends.** Every client has an id, and `by` names it:
  `frontend(client)`. Extensions show state to every frontend
  (`cursor.show`) or tell one client (`cursor.tell`). `tau serve` is built
  first and shapes the `Harness`.
- **Core decides; the driver performs** (D11), and **one record log plus a
  scratchpad file** (D12).

**Seventh revision** (2026-10-27). Fewer rules, one loop:
- **A step is one pass of a fixed loop**, from `pre_step` to `post_step`: an
  input phase, a sample phase, and a tools phase. Each phase has the same
  shape: pre hook on the effect, effect, pre hook on the draft, commit under
  `at`, post hook. Hooks change the machine's state; then the machine acts
  (§6). "Cycle" is retired: a step is the cycle.
- **Two hook shapes.** Cursor hooks get the cursor and may move it. Tool hooks
  (`pre_tool`, `pre_result`) get the batch's node and may not. **A hook
  returns `None` to carry on, or a string to stop**, and the string says why
  (§7).
- **Drafts are committed under wherever `at` is.** A hook that moves the
  cursor before a commit chooses where the node goes. I1 keeps only what a
  sample sends: exactly `walk(at)`.
- **One `Results` node per batch**, written when every call has finished,
  with one outcome per call. The provider expands it into one tool message
  per call. I4 holds by construction (D7, reversed back to the plan).
- **The journal.** Each finished call's outcome is a scratchpad item until the
  `Results` node delivers it, so a crash loses no finished call (§8).
- **The cursor never looks at children.** `next` reads only `walk(at)`.
  Fast-forward, `Session.rerun`, runs, batch states, and the claim are gone.
  Going to an `Assistant` node with calls and stepping runs them again (§5).
- **Config is the nearest `Config` on the walk.** I6 and the restatement are
  retired (D5).
- **Input is the one kind of item core knows.** An item from a frontend client
  is input. A step starts its input phase with the cursor's pending input,
  and the `User` node `delivers` it. The base extension, `deliver`, and
  `combine` are gone (§7, D2).
- **"Committed" and "uncommitted" are retired.** A node is on the tree once it
  is written. "Boundary" is retired too: a hook that holds the cursor may move
  it.
- **Labels** are a mutable map from unique strings to cursor ids, stored as
  records. A cursor may have several (§1).
- **Move history.** Hooks read move records: `cursor.moves()`, and
  `cursor.moves(since_step=True)` for the moves since this step's `pre_step`
  began.

The design rests on one idea: **the conversation is a tree of immutable
nodes, and everything else is either a pointer into that tree, a function of
it, or an item in the scratchpad.** τ 0.15 kept per-turn state in objects:
cursor fields, queues, per-turn state objects, pausing brokers. A reload had
to guess that state back into existence. Here nothing is state unless it is a
node, a move record, a label record, or a scratchpad item.

One way to picture it: **each cursor is a small Turing machine.** It reads
`walk(at)`. `next` is the transition function. The extensions listed in
`config_at(at)` are its state table. The session tree is the tape, except
that the tape is append-only, so the only things a machine changes are where
it is and what it appends. The scratchpad is how one machine leaves word for
another. **This is an analogy only.** None of its words (head, tape, state
table) is a term of this design or an identifier in its code.

Notation: `x?` marks a field that may be absent. `A | B` means one of.

## 1. Stored objects

Four things are stored. Nodes, move records, and label records are written
once and never changed. Scratchpad items may be updated and removed.

```
Node   { id, parent?, time, by, body, copied_from, derived_from, delivers }

body   = System    { text }
       | User      { text, images }
       | Assistant { parts, stop, stop_raw? }
       | Results   { outcomes }                 outcomes: [ { call, outcome } ], in call order
       | Config    { snapshot }
       | Custom    { kind, data }

part    = Text(text) | Thinking(text, signature?) | Call { id, tool, args_raw }
outcome = Ok(value) | Err(error) | Interrupted
stop    = end | tools | length | refused | interrupted | error
by      = frontend(client) | provider | tools | core | extension(name)

Move   { cursor, from?, to, by, time, opened_by? }
Label  { label, cursor? }                       no cursor: the label is removed

Item   { id, from: { frontend?, extension?, cursor }, to, data }
```

`copied_from`, `derived_from` and `delivers` are lists, empty for most nodes.
- `copied_from` lists the nodes whose content this node repeats verbatim, in
  order, such as a compaction copy. The store keeps an in-memory reverse
  index of it, built at load.
- `derived_from` lists the nodes whose walks this node's content was computed
  from, such as a summary.
- `delivers` lists the scratchpad items this node consumed: the inputs a
  `User` node holds, or the journal items a `Results` node holds. It holds
  ids, never content.

Parts are kept in the order they streamed. `args_raw` is the argument text
exactly as the model produced it, which may be truncated or malformed. It is
never parsed and re-serialised for storage. `stop` is the normalised reason
generation ended. `stop_raw` keeps the provider's own string for debugging.

| `stop` | meaning |
|---|---|
| `end` | the model chose to stop |
| `tools` | the model handed off to tool execution |
| `length` | the output limit was reached |
| `refused` | the provider's filter or refusal |
| `interrupted` | we cancelled it (abort) |
| `error` | the stream failed after output had arrived |

A **session** is the set of its nodes, moves, labels, and scratchpad items.

A **cursor** is `{id, at}`. `at` is the `to` of the cursor's latest move. A
move with no `from` opens a cursor (Q5) and assigns its id, the next
session-local integer (Q33). A move's `by` says who asked for it (Q28).
`opened_by` names the cursor that opened this one (Q6). The driver fills in
`by` and `opened_by` from the caller; no API caller supplies them.

A **label** is a name for a cursor. Labels map unique strings to cursor ids:
each label names one cursor, and a cursor may have any number of labels
(`"research-agent-3ca7"` and `"Web Search Agent (Rust Patterns)"` can both
name cursor 4). A `Label` record sets a label, moving it if another cursor
had it, or removes it. Load replays them. `Session.cursor(label)` looks one
up.

A **scratchpad item** is addressed to one cursor (`to`). `from` names the
cursor that added it, and the extension or the frontend client:
- **with a `frontend` client, it is input** (§7), whatever else `from`
  names. Its `data` is `{text, images}`. A frontend adds items only as
  input, through `Session.input`;
- **with an `extension`, it belongs to the extensions.** `data` is any JSON
  value;
- **with neither, it is a journal item**, which core writes (§6). Its `data`
  is `{batch, call, outcome}`.

Items have session-local integer ids that are never reused. No walk contains
an item, and no item reaches the model (I1).

Every root is a `Config` node (Q5), so every walk starts with one and
`config_at` is always defined.

The core is pure. The driver passes `time` in, and core assigns ids (Q8). No
function in `tau-core` reads a clock, a file, or the network.

## 2. Batches and results

The `Call` parts of an `Assistant` node with `stop == tools` and at least one
call form a **batch**. A `stop == tools` reply with no calls is an ordinary
reply.

| reply's `stop` | call's `args_raw` | in the tools phase |
|---|---|---|
| `tools` | parses | the tool runs, through `pre_tool` and `pre_result` |
| `tools` | doesn't parse | `Err("invalid arguments")`, without running. The model sees its own malformed output. |
| anything else | either | nothing runs. The calls are not runnable; the provider renders them as text (§10). A truncated plan is not half-run. |

A batch is answered by **one `Results` node**, written when every call has an
outcome: one outcome per call, in call order. When a walk is sent, the
provider's conversion expands it into one tool message per call.

```
u1  User          "fix the bug"
└ a1  Assistant   text, calls [c3, c5]        the batch
  └ r1  Results   c3: Ok(…), c5: Ok(…)        written at the join; next is Sample
```

Running a batch again is going back to its `Assistant` node and stepping: the
cursor does not look at children, so the tools phase runs and writes a
sibling `Results` (§5). Each `Results` node is final; another answer is
another node (Q10).

Until the join, each finished call's outcome waits as a **journal item** in
the cursor's scratchpad. The `Results` node delivers them. A crash before the
join loses no finished call (§8).

## 3. Derived views

None of these are stored. Each is a function of the stored objects.

| view | definition |
|---|---|
| `walk(n)` | the nodes root → … → `n`, following `parent`. Inclusive. |
| `LCA(a, b)` | the deepest node on both `walk(a)` and `walk(b)`; none if they are in different trees |
| `config_at(n)` | the body of the nearest `Config` node on `walk(n)` |
| messages | the provider's conversion of a walk's message-bearing nodes |
| `next(walk)` | §6 |
| a cursor's scratchpad | the items whose `to` is that cursor, in the order they were added |
| a cursor's pending input | the input items in its scratchpad, in the order they were added |
| a cursor's labels | the labels whose latest record names it |

There is no "committed" set. A node is on the tree once it is written.
Nodes that no cursor's walk passes through are ordinary nodes: side nodes an
extension appended, a detached root's work, `sample_error` records, a
sibling reply someone went back from. Any of them may be gone to.

One session, mid-step, two trees:

```
r    Config  (model: big)            (root)
└ s    System
  └ u1   User "fix the bug"
    └ a1   Assistant  calls [c3, c5]  ◀ A.at      (c3 still running)
d    Config  (model: cheap)          (detached root: an earlier step's hook made a
└ d1   User "summarise s..u1"         one-off call; its root records which model
  └ d2   Assistant "…summary…"        produced d2)

scratchpad:  item 4 { from: {frontend: laptop, cursor: A}, to: A,
                      data: {text: "also check b.rs"} }               input
             item 5 { from: {cursor: A}, to: A,
                      data: {batch: a1, call: c5, outcome: Ok(…)} }   journal
```

When `c3` finishes, the join writes `r1 Results` under `a1`, delivering item
5, and A moves onto it. The step ends with `post_step`. The next step's input
phase takes item 4: `u2 User` under `r1`, delivering it, then a sample. If
the process dies before the join, A loads at `a1` with item 5 still there,
and the next tools phase reuses `c5`'s outcome and answers `c3` (§8).

## 4. Invariants

Each invariant has a statement, a reason, and the test that holds the code to
it. Tests live in Phase 2 (`tests/behavior/`, Python, through `Harness`) and
Phase 3 (`crates/tau-core`, Rust unit tests and proptest). The numbers are
kept from earlier revisions; I6 is retired.

### I1 A sample sends its walk
**Statement.** A sample sends exactly `walk(at)` with `config_at(at)`, as
`at` stands when the sample starts, and nothing else. No request is spliced,
filtered, or added to outside the tree. Scratchpad items and `Custom` nodes
never reach the model (Q3).

A reply is committed under `at` as it stands at commit time. A `pre_reply`
hook that moves the cursor therefore chooses where the reply goes, and the
reply's parent need not be where it was sampled. That is a hook's choice,
recorded in the move records, and no rule in core.

Relations other than `parent` are recorded as `copied_from` (verbatim),
`derived_from` (computed), or `delivers` (consumed items).

**Why.** In 0.15 the model's view was a fold of the path with splices,
filters, and per-turn injections. "What did the model see?" had no direct
answer. Here it is the walk at the moment of the sample.

**Enforced by.**
- No API adds to a request outside the tree. A hook changes what is sent only
  by moving the cursor, or appending and moving onto what it appended.
- Behavior test: `FakeProvider` records each sample's messages and config;
  they equal the conversion of `walk(at)` and `config_at(at)` at the
  `sample` job.

### I2 Immutability
**Statement.** No node changes after it is appended. Pre hooks on a draft
(`pre_input`, `pre_reply`, `pre_result`) shape it before it is committed;
nothing edits it afterwards. Scratchpad items are not nodes, and changing
one changes no node.

**Why.** 0.15 wrote nodes in two parts and finalized them with a second
write. A process exit between the two left an orphan that nothing could
repair.

**Enforced by.**
- `Node` has no setters in Rust and is a frozen class in Python. A behavior
  test checks that assignment raises.
- The record log is append-only.
- Proptest: after any operation sequence, every node observed earlier still
  equals what was observed.

### I3 `at` is a node
**Statement.** A cursor's `at` is always a node of its session. Every walk is
valid, because every node passed its type's checks when it was written (I5),
so there is nothing else to check at a move.

**Why.** "Where is this cursor?" must never need a guess (0.15 guessed a
default position).

**Enforced by.**
- Unit test: a move to an id that is not in the session is refused.
- Proptest: after any operation sequence, every `cursor.at` is a node.

### I4 A batch's `Results` answers every call once
**Statement.** The tools phase writes one `Results` node per batch it runs,
with exactly one outcome per call of the batch, in call order. It holds by
construction: the join writes the node from one outcome per call, and
nothing writes half of it.

A hook may write a `Results` node of its own anywhere (a cache, a test). Core
checks only that its outcomes name distinct calls. Whether a walk is legal on
the wire is the provider's (I5, Q11).

**Why.** Every provider rejects a conversation with an unanswered call, so a
call that did not run still owes the model an outcome that says so.

**Enforced by.**
- Unit tests: the join writes one outcome per call, including `Interrupted`
  and `Err` outcomes, and core-written ones.
- Proptest: every `Results` node the tools phase wrote answers each call of
  its parent exactly once.
- Behavior scenarios: tool batch; unknown tool; bad arguments; `pre_tool`
  blocks one call of two; `pre_result` redacts an output; abort during tools.

### I5 A node is valid by its type
**Statement.** Core's only checks at append are the types themselves:
- **V1**: a node without a parent must be `Config`, so every walk starts with
  one;
- **V2**: a `Results` node's outcomes name distinct calls.

Every other transition is legal in the tree. `User` under an unanswered
batch, `Results` under a `User` node, two `User` nodes in a row: each is a
tree a person or a hook made on purpose. Making it legal on the wire is the
provider's conversion, which runs before every sample. A conversion error
fails that sample (§8, Q11). For example, a provider turns a `Results` node
whose parent is not an `Assistant` node into a `User` message.

**Why.** An illegal node should fail when it is made. But the tree is not a
wire format, and a rule that only some APIs need belongs to those APIs'
conversions.

**Enforced by.**
- Unit tests per rule.
- Behavior scenario: a provider-illegal walk fails at the next sample,
  records the error, and leaves `at` unchanged.

### I6 Retired
Config no longer follows the cursor. A cursor's config is `config_at(at)`:
the nearest `Config` on its walk. Going to an old line uses that line's
config. Frontends and extensions deal with the implications, for example by
showing the model a go-to switches to, or by appending a `Config` (D5).

### I7 Reload is replay
**Statement.** In-memory state is a pure function of the stored nodes, moves,
labels, and scratchpad items. No heuristics, no repair, no "latest wins".

**Why.** 0.15's loader kept the last line per id and guessed a reopen
position. A crash could therefore produce a state no sequence of operations
would have produced.

**Not persisted, by design:**
- The auto-step flag: every cursor loads with it off.
- The `max_steps` counter (Q20).
- Whether a cursor is busy. Every cursor loads at rest.
- Open questions, and what extensions show frontends (§9).
- When each cursor's last `pre_step` started, and the scratchpad as it was
  then.
- The reverse index of `copied_from`, which load rebuilds from the nodes.
- Events and stream deltas.

A cursor that was running a batch loads at its `Assistant` node, with the
journal items of the calls that finished. Input given to a busy cursor
survives a crash as an item. Whether an item was consumed is read off the
nodes' `delivers`, so a reload neither loses nor repeats it.

A step's commit and its move are one record (§11). A torn final record, cut
off by a crash, is a write that never completed. Load ignores it. That is not
repair: the result is exactly the state before that write began.

Load **reconciles** the scratchpad with one rule: an item listed in some
node's `delivers` is dropped. The rule is a fact about stored records, not a
guess.

**Enforced by.**
- Proptest: `state == load(save(state))` over random operation sequences,
  including multiple cursors, aborts, labels set and moved, and scratchpad
  items added, updated, removed, and delivered. `state` here means nodes,
  moves, labels, each cursor's `at`, and the scratchpad.
- Proptest: a crash injected between a delivering record and the items'
  removal loads with the items gone and the node present.
- Behavior scenarios: input to a busy cursor, then a crash before delivery;
  on reload the input is delivered exactly once. A crash mid-batch keeps
  every finished call's outcome.

## 5. Move

A move is the only way `at` changes. It comes from one of three places, and
its `by` records which (Q28). No caller supplies `by`.
- **a commit's move** onto the node a phase just committed (`by: core`, or
  the hook's extension for a hook's `cursor.write`);
- **a go-to**, `Session.move(cursor, to)`, from a frontend
  (`by: frontend(client)`, Q15);
- **a hook's move**, `cursor.move(to)`, from a cursor hook on its own cursor
  (`by: extension(name)`).

A move checks only that `to` is a node of the session (I3). It does not look
at what is below `to`. Going to an `Assistant` node whose batch already has a
`Results` child and stepping runs the batch again, and writes a sibling
`Results`: that is a **re-run**, and it needs no method of its own. A
frontend that does not want this goes to the `Results` node instead.

**A hook's move applies at once.** Only cursor hooks can move, and while one
runs, nothing else of its step is in flight: tool hooks have no cursor, and
the tools phase moves the cursor only at its join (§6). Later hooks at the
same point see the cursor where it now is. Each hook can see every move of
the step so far with `cursor.moves(since_step=True)`.

**A frontend's move of a busy cursor** is an abort, then the move:
- It is **refused** while any hook of the cursor is running, or while a
  question on it is open (§7). Nothing is queued: the frontend may try again,
  or abort first.
- Otherwise it aborts the step (§8). The effect in flight is cancelled, the
  draft is committed, and the post hooks and `post_step` run as usual. Post
  hooks may even move the cursor; the frontend's move then applies over
  theirs.
- Then the move applies to a cursor at rest.

The frontend's move wins, and no extension loses a hook it was owed. A move
whose `to` equals `at` writes nothing.

**A go-to turns auto-step off, then wakes the cursor.** Its step runs
`pre_step` and `post_step` and no phase, because auto-step is off (§6). An
extension's `pre_step` may turn auto-step on, and then the phases run.
Otherwise the cursor sits where the go-to put it. Browsing calls no provider
and runs no tool unless an extension or the frontend chooses to.

A **refused** move changes nothing and raises to whoever asked for it. A hook
that catches it has handled it; one that does not fails like any hook error
(§7).

**Branching** is a move whose `to` is neither on `walk(at)` nor below `at`:
the cursor leaves its line at the LCA. Moving to an ancestor is **going
back**. The branch forms when the next commit under that ancestor makes a
sibling, as when a person rejects a reply and steps again.

```
before                          after "go to" b2
u1 ─ a1 ─ r1   ◀ A.at           u1 ─ a1 ─ r1
 └ b1 ─ b2                       └ b1 ─ b2   ◀ A.at
LCA(at, b2) = u1                 config is config_at(b2), whatever b's line chose
```

## 6. Steps

### Next

`next(walk)` says what a walk needs done. It is a pure function of the walk,
and never looks below its last node. It skips `Config` and `Custom` nodes from
the end, then looks at the nearest message-bearing node:

| nearest message-bearing node | `next` |
|---|---|
| `User` or `Results` | `Sample` |
| `Assistant` with `stop == tools` and at least one call | `RunTools` (all its calls) |
| any other `Assistant`, `System`, or none | `Wait` |

### The machine

A cursor's step is a sans-IO state machine in `tau-core` (D11). Its state is:
- `at`, and `auto`, the auto-step flag;
- the **draft**: what the current phase will commit (input `User` nodes, a
  reply, or a batch's outcomes);
- the **effect**: what the current phase will do (sample `walk(at)`, or run
  the calls).

Hooks change that state through their handle: they move `at`, edit the
draft, set `auto`, or fill in the effect's result so it does not run. Then
the machine acts. A step is one pass of the same phase three times:

```
pre_step
for phase in [input, sample, tools]:                      if the phase applies (below)
    pre_<effect>     pre_sample | pre_tool                hooks may answer, so the effect is skipped
    effect           sample walk(at) | run the calls      input has no effect
    pre_<draft>      pre_input | pre_reply | pre_result   hooks edit the draft; in tools, per call as
                                                          each finishes, then its journal item
    commit           the draft under at, and the move onto it, as one record
                                                          (in tools, once, at the join)
    post_<draft>     post_reply | post_result             input has no post hook
post_step(ending)
```

| phase | applies when | pre hook on the effect | effect | pre hook on the draft | commits | post hook |
|---|---|---|---|---|---|---|
| input | there is pending input, and `next != RunTools` | — | — | `pre_input`, per item | one `User` node per item, each delivering it | — |
| sample | `next == Sample` | `pre_sample` | the provider request | `pre_reply` | one `Assistant` node | `post_reply` |
| tools | `next == RunTools` | `pre_tool`, per call | the calls, in parallel (Q21) | `pre_result`, per call, as each finishes; then a journal item | one `Results` node at the join, delivering the journal items | `post_result` |

A phase applies only if **auto-step is on, or a frontend asked for this step**
(`Session.step`), and its condition holds for `at` *at the start of the
phase*. A hook that moves the cursor in `post_reply` therefore decides where
the tools phase looks. The input phase waits while `next == RunTools`, so a
batch is answered before input goes under it (Q40).

**Ending.** `post_step` gets how the step ended:

| ending | when |
|---|---|
| `done` | the step ran its phases, or none applied |
| `halted(reason)` | a cursor hook returned a string |
| `failed(error)` | a sample failed, or a cursor hook raised (a raising `pre_input` leaves its input pending, Q43) |
| `aborted` | an abort, or a frontend's move of the busy cursor |

What the step did is in the move records: `cursor.moves(since_step=True)`.

**After `post_step`**, another step starts at once if auto-step is on and
`next(walk(at)) != Wait` or input is pending; or, throttled, if the cursor's
scratchpad changed since this step's `pre_step` started. Otherwise the cursor
**rests**. After an abort, a halt, or a failure, auto-step is off and the
cursor rests whatever the scratchpad holds. Journal items the step itself
wrote and delivered do not count as a change.

**Waking.** A cursor at rest starts a step when:
- an item is added to its scratchpad;
- it opens;
- its session loads (every cursor, in the lease holder only);
- a go-to lands it;
- a frontend steps it (`Session.step`) or turns its auto-step on.

A cursor is **busy** from the moment a step starts until it rests. Busy is
read off the machine, never stored or set. `Session.step` on a busy cursor is
refused; turning auto-step on for a busy cursor takes effect after its
`post_step`.

**Throttle.** A step that committed nothing starts the next at least 250 ms
after it started. If its `pre_step` took 80 ms and nothing applied, the next
waits 170 ms. A wake during the wait joins the step already scheduled.
Nothing polls: an item an extension leaves in place causes no further step
until the scratchpad changes again. An extension that must poll removes its
item and adds a new one.

**`max_steps`.** Each step that commits something counts. Only actions
`by: frontend(…)` reset the count. When it reaches `config_at(at).max_steps`,
auto-step turns off (Q20). A cursor a hook or tool opens inherits its
opener's count and auto-step; one a frontend opens starts at 0, auto-step
off.

### Where things are committed

Every commit is **under `at` as it stands at the commit**, and the move onto
the new node is in the same record. Input goes under wherever `pre_input`
hooks left the cursor, and each next item under the previous one. A reply
goes under wherever `pre_reply` hooks left it. The tools phase's `at` cannot
move during the phase, because tool hooks have no cursor, so its `Results`
node goes under the batch's `Assistant` node.

A `pre_input` hook that wants to put something before a person's message
appends a node and moves onto it; the input then goes under that node.
`pre_sample` shapes what is sent the same way: it moves to a `Config` node it
appended (a model choice, Q26), a reminder (Q30), or the end of a line it
built (compaction, Q25).

## 7. Hooks

### Two shapes

**Cursor hooks** — `pre_input`, `pre_step`, `pre_sample`, `pre_reply`,
`post_reply`, `post_result`, `post_step` — are

```python
async def hook(cursor: Cursor, subject) -> str | None
```

**Tool hooks** — `pre_tool`, `pre_result` — are

```python
async def hook(node: NodeHandle, call: CallDraft) -> str | None
```

- `cursor` is a handle to the cursor the hook runs for, bound to the hook's
  extension, which is how the driver fills in `by`. It may move the cursor.
- `node` is a handle to the batch's `Assistant` node, bound to the hook's
  extension and the cursor. It can do everything a cursor handle can except
  move the cursor or set its auto-step. Tool hooks run in parallel, one chain
  per call, so none of them may touch the cursor.
- `subject` is what the hook point is about (table below). Drafts are mutable:
  a hook edits one in place.
- At each hook point, hooks run in the order their extensions are listed in
  `config_at(at)`.

**The return value.** `None` carries on. **A string stops**, and says why:
- from a cursor hook, the step **halts**: later hooks at that point do not
  run, the draft of the current phase is discarded, auto-step turns off, and
  `post_step` runs with `halted(reason)`. In `pre_input`, the input is
  refused: its item is removed, and its client is sent a `notice` with the
  reason. Returning a string from `post_step` only turns auto-step off;
- from a tool hook, **that call's outcome is `Err(reason)`**: in `pre_tool`
  the tool does not run, and in `pre_result` the outcome is replaced. Later
  hooks of that call's chain do not run. The other calls carry on.

So a permission extension that is declined returns `"permission not
granted"` from `pre_tool`, in its own words. Core has no `Skipped`.

### Hook points

| hook point | shape | runs | subject |
|---|---|---|---|
| `pre_input` | cursor | for each pending input item, oldest first | the input draft: text and images, editable |
| `pre_step` | cursor | at the start of every step | none |
| `pre_sample` | cursor | before a sample | the sample draft: setting its `reply` answers it (a cache hit), and no request is made |
| `pre_reply` | cursor | before a reply is committed | the reply draft: its parts, editable; its `stop` is a fact about the sample and is not |
| `post_reply` | cursor | after the reply is committed and moved onto | the `Assistant` node |
| `pre_tool` | tool | before each call runs | the call draft: its arguments editable; setting its `outcome` answers it, and the tool does not run |
| `pre_result` | tool | as each call finishes | the call draft, with the call as it ran and its outcome, editable |
| `post_result` | cursor | after the `Results` node is committed and moved onto | the `Results` node |
| `post_step` | cursor | at the end of every step, aborted ones included | the ending |

An answer from `pre_sample` or `pre_tool` skips the effect. The draft's pre
hooks still run, so a cache hit is still redacted (Q18). `pre_result` may
change an outcome's content but not its kind, except to `Err` by returning a
string.

**Outcomes core writes** skip `pre_result`: `Err("invalid arguments")`,
`Interrupted`, and `Err("hook error")` have no tool output to rewrite.

### What a handle can do

Through its handle, any hook may:
- **read** the tree, every cursor's position, labels, and move records;
- **append** a node anywhere (side nodes, detached roots);
- **write scratchpads**: add an item to any cursor's scratchpad, update or
  remove any item;
- **ask** a person (`cursor.ask`), **show** frontends state (`cursor.show`),
  and **tell** one client something (`cursor.tell`) (§9);
- **open cursors**, and set and remove labels;
- **sample a walk and run a tool** outside any step (a cold summary on a
  detached root, for example).

A cursor handle may also act on its own cursor:
- `cursor.move(to)`;
- `cursor.write(body, delivers=[…])`: append under `at` and move onto it, as
  one record;
- `cursor.auto_step = …`.

An extension reads **its own cursor's scratchpad** in `pre_step` and
`post_step`. Frontends may read any scratchpad at any time (§9).

Nodes, moves, and items a hook writes trigger no hooks on its own cursor's
step. An item it adds may wake another cursor, whose step runs hooks as
usual.

**Hook errors.** An uncaught exception is a halt with the error:
- from a cursor hook, `post_step` runs with `failed(error)`, and the frontend
  is shown the error. In `pre_input`, the input stays pending;
- from a tool hook, that call's outcome is `Err("hook error")` (`by: core`).
  In `pre_result`, the raw output is withheld, because the hooks that would
  have redacted it did not finish.

### Input

`Session.input(cursor, input)` is called through a client handle, which
supplies the client id. It is refused at once if a question is open on the
cursor. Otherwise core adds an input item
`{from: {frontend: client, cursor}, to: cursor, data: {text, images}}` to the
cursor's scratchpad. Adding it wakes the cursor if it is at rest. On a busy
cursor, it waits for the next step's input phase.

The **input phase** takes the pending input in the order it was added. For
each item, `pre_input` runs on its draft, and then core commits a `User` node
under `at` with the draft's content, `delivers: [item]`, authored
`frontend(client)`, and moves onto it, as one record. The item is removed
after. Two inputs from two clients are two `User` nodes in a row.

A frontend's **send** is input plus auto-step on. Typing while the agent works
is the same call: the input waits, and the next step delivers it, after the
batch if one is running. **Interrupt and send** is abort, then send.

Pending input is visible to every frontend, which may edit or remove it like
any item. After an abort, it stays pending, and the cursor rests. An
extension may offer it back to its client in `post_step` when the ending is
`aborted` (`cursor.tell`, Q42).

Other input behaviors are extensions:
- **A command**: a `pre_input` hook that handles `/model fast` writes the
  `Config` node with `cursor.write` and returns `"handled: model fast"`, so
  the input is not written and the step halts.
- **A gate**: a `pre_input` hook that returns a reason until something is
  satisfied.

### Questions

A hook that needs a person calls **`await cursor.ask(target, question)`**.
`target` is one client or any client. The question goes out as an event, and
the call returns the answer, or `None` if it was declined. Core does not look
at either: the question's shape and its answers belong to the extension.

While a question is open, the cursor is **locked**:
- the frontend's input, go-to, and step on that cursor are refused, not
  queued;
- `Session.answer(cursor, question, answer)` answers it, and
  `Session.decline(cursor, question)` declines it;
- abort is allowed. It cancels the effects in flight (other calls of the
  batch, for example) and leaves the question open (§8).

A question for one client is declined if that client detaches. A question
for any client holds the cursor until some client answers or declines it,
and is replayed to every client that connects (§9). Questions are driver
memory: a crash loses them (§8).

### Between cursors

To ask another cursor for something, an extension adds an item to that
cursor's scratchpad. Adding wakes it, and its own extensions' `pre_step`
hooks decide what to do. `to` names a cursor, not an extension: any extension
may read the items its cursor receives, and any may update or remove any
item. An extension that wants only its own items checks `from`.

**A sub-agent** (Phase 5) is a tool that, through its handle:
1. opens a cursor (`opened_by` = the calling cursor), for example on a
   detached root, and may label it;
2. adds the task as an item to the new cursor's scratchpad;
3. waits until the new cursor rests at `next == Wait`, and returns a value
   whose `derived_from` lists the cursor's final node.

The new cursor wakes on opening, and an extension's `pre_step` there writes
the task as a `User` node with `cursor.write(…, delivers=[item])`. The tool
drives nothing: it only waits.

## 8. Abort, failure, reload

- **Abort.** `Session.abort(cursor)` turns auto-step off and cancels the
  effect in flight: the sample stream, or the running tools. **It never
  cancels a hook.** No later effect starts. The current phase's draft goes
  through its pre hooks and is committed as usual, its post hook runs, and
  `post_step` runs with `aborted`. Then the cursor rests.
  - **During a sample**, with no parts yet: there is no draft; nothing is
    committed.
  - **During a sample**, with parts: the reply draft has the parts so far and
    `stop: interrupted`. `pre_reply` runs, the reply is committed and moved
    onto, and `post_reply` runs. Its calls are not runnable (§2). The user
    saw those tokens, so the model sees them next time.
  - **During tools**: each running call is cancelled and its outcome is
    `Interrupted`, and so is each call not yet started. A call whose
    `pre_result` is running finishes it. A call whose `pre_tool` is running
    finishes it: a string or an outcome it sets is used; if it returns
    `None`, the call does not start, and its outcome is `Interrupted` (Q34).
    An open question keeps the join waiting until it is answered or
    declined. The join commits the `Results` node, and `post_result` runs.
  - **During a hook** with no effect in flight: the hook finishes; no later
    effect starts.
  - **At rest**, abort only turns auto-step off.
- **A sample fails.** HTTP errors, a stream that breaks partway, and the
  provider's conversion rejecting the walk. Auto-step goes off (Q4).
  - **Output had arrived:** the reply is committed like an aborted one, with
    `stop: error`. `stop_raw` and the error are kept. `post_reply` runs, then
    `post_step` with `failed(error)`.
  - **Nothing arrived** (a 401 or 429 before the first token): the driver
    appends a `Custom {kind: sample_error}` node under `at`, without moving.
    It is visible, and on no cursor's walk. `post_step` runs with
    `failed(error)`.
- **A tool fails.** That is an `Err` outcome like any other.
- **Length, refusal.** Ordinary replies with `stop: length` or
  `stop: refused`. `next` is `Wait`. Continuing after `length` is an
  extension's choice, for example a `post_reply` hook that writes "continue"
  with `cursor.write` (Q20).
- **Crash during a sample.** On reload `next` is `Sample`, so the next step
  samples again.
- **Crash during tools.** The cursor loads at the batch's `Assistant` node.
  Each call that finished before the crash has a journal item. The next
  tools phase, for each call:
  - with a journal item for this batch: uses its outcome; the call does not
    run again, and its `pre_result` already ran;
  - otherwise, if its tool is idempotent: runs it like any call;
  - otherwise: `Interrupted` (D4).

  The join delivers the journal items. A ten-minute `bash` that finished
  before the crash keeps its outcome.
- **Crash during a commit.** A commit and its move are one record (§11), so a
  crash leaves both or neither. A torn final record is ignored on load. A
  crash after a delivering record but before the items' removal leaves the
  items in the scratchpad file; load drops them.
- **Reload.** Every cursor loads at rest, with auto-step off, no open
  questions, and nothing shown. Then, in the lease holder only, every cursor
  wakes. With auto-step off, its step runs `pre_step` and `post_step` and no
  phase; pending input waits for the frontend to send or step.

## 9. Events and frontends

There is one `Event` type, and every event names its cursor:

| kind | when | carries |
|---|---|---|
| `node_appended` | any append | the node |
| `cursor_moved` | after a move record is written | `from`, `to`, `by` |
| `label_changed` | after a label record is written | the label, and its cursor or none |
| `stream_delta` | during a sample | a fragment of text, thinking, or a call |
| `item_changed` | an item is added, updated, or removed | the item, or its id when removed |
| `question` | a question is asked, answered, declined, or withdrawn | the question, its target, and its state |
| `status` | an extension shows state (`cursor.show`) | the extension and its state |
| `notice` | an extension tells one client something, or input is refused | the client and the message |

Observers cannot block the driver or change anything. Every frontend is a
projection of this stream. A `notice` goes only to its client; every other
event goes to every subscriber.

**Clients.** A frontend serves one or more **clients**: a terminal, or each
connection to `tau serve`. Every client has an id, and everything a client
does is recorded as `by: frontend(client)`. The id is recorded only, never
used for control flow, except that a question or a notice may be addressed
to one client.

**What extensions show.**
- **`cursor.show(state)`** sets the calling extension's state for its cursor,
  replacing what it showed before. It is driver memory: a reload clears it,
  and the extension rebuilds it when the cursor wakes. State that must
  survive a reload is the extension's to write as nodes or items.
- **`cursor.tell(client, message) → bool`** sends a message to one client. It
  returns whether the client was attached to receive it.

**`tau serve` comes first** among frontends. It multiplexes many clients over
the network onto the same sessions and cursors. When a client connects, it
sends that client every cursor's current `status`, the open questions it may
answer, and the scratchpads. A local frontend is the same model with one
client: it serves itself. The `Harness` (Phase 2) is shaped like serve.

**No cursor waits on a particular client.** Events go to whoever is
subscribed, possibly nobody. A client may switch to another cursor or
session at any time, and every active cursor keeps operating. A cursor waits
on a person only through a question.

## 10. Provider requirements

Core knows no provider. Each provider is a Python adapter for one API family,
and it owes the driver the following. The behavior suite holds every provider
to these through `FakeProvider` and a recorded-wire test per provider.

1. **Stream parts in order.** Text, thinking, and calls arrive as
   `stream_delta` events. The driver accumulates them into the reply draft,
   and each call's argument text into its `args_raw`.
2. **Normalise the stop reason** into `stop`, and keep the raw string in
   `stop_raw`. For example: OpenAI `tool_calls`, Anthropic `tool_use` →
   `tools`; `length`, `max_tokens`, `MAX_TOKENS` → `length`;
   `content_filter`, `refusal`, `SAFETY` → `refused`.
3. **Replay faithfully.** Converting a walk sends earlier parts back as they
   were produced: text verbatim, and `args_raw` byte-for-byte where the wire
   takes a string (OpenAI). Where it requires an object (Anthropic), parse it
   there and nowhere else. This is what lets a prefix cache reuse a partial
   reply.
4. **Reasoning follows the API's rules.** Send thinking back exactly as the
   API requires: signed blocks unmodified, encrypted reasoning as received.
   A cut-off thinking part with no signature is stored and shown, but never
   sent back.
5. **Expand `Results`.** A `Results` node under its batch becomes one tool
   message per call, in the order the API needs. Anywhere else, the
   conversion makes it legal, for example as a `User` message.
6. **Render what isn't a call as text**, using τ's markup (Q27): a `Call`
   part of a reply whose `stop` is not `tools`, or of a batch with no
   `Results` below it on the walk, according to `config.partial_calls`; and
   a stop note for `length`, `interrupted`, and `error`.
7. **Legality is the conversion's.** A walk the API would reject even after
   these rules is a conversion error, and the sample fails before any
   request is sent (Q11).
8. **Report cache use.** Each sample reports how many prompt tokens came from
   the provider's cache: llama.cpp `cache_n`, and the hosted APIs' usage
   fields. Phase 4's exit criteria include a llama.cpp test: interrupt a
   reply mid-text, send input, and assert that the cached count covers the
   old prompt plus the partial text.

## 11. Persistence and processes

- `tau-store` keeps, per session, one append-only **record log** and one
  **scratchpad file**, each with a versioned header (D12). Loading reads
  both, replays the log, and reconciles the scratchpad (I7).
- **Records.** Each record is one durable write: one or more nodes with at
  most one move, or one label change. Each is length-prefixed and
  checksummed, so a torn final record is detected exactly, and load ignores
  it.
- **Durability.** A node is durable before any move that names it. A commit
  and its move are one record, and so are a hook's `cursor.write` and its
  move.
- **The scratchpad file** holds the current items and the next item id. It is
  rewritten whole for each change, atomically (write a new file, sync it,
  rename it over the old one). A removed item is gone from disk. Adding an
  item is durable before the cursor it wakes is woken, and a journal item is
  durable before its call counts as finished.
- **Delivery order.** A delivering record is made durable first, and the
  items are removed after. Load drops any item a node delivers. An item
  consumed without a node is only removed; a crash before the removal leaves
  it in place, so such consumers must tolerate seeing an item twice.
- **Lease.** There is one `flock` per session, taken on the record log. The
  holder writes; every other process gets the session read-only. Only the
  holder runs steps and hooks, the wakes on load included. If the holder's
  process is dead, the lease is reclaimed.
- **Two cursors at one batch** each run it, and each writes its own
  `Results` node: the journal is per cursor (Q13). An extension that wants
  "run once" coordinates through the scratchpad.

## 12. Core and driver

Every rule in this document is decided in `tau-core`, in Rust (D11). Each
cursor's step is a sans-IO state machine: it never waits and does nothing
itself. It hands out **jobs**, and the Python driver performs them and feeds
back the results:

```
step = session.wake(cursor, reason)           Rust
loop:
    job = step.next_job()                     Rust: run a hook point | sample | run calls
                                              | persist a record | update the scratchpad
                                              | wait (the throttle) | rest
    result = await driver.perform(job)        Python: hooks, providers, tools, the store
    step.feed(result)                         Rust: applies hook changes, advances
frontend calls (abort, move, input, step, answer, decline) are fed to the same machine
```

A hook job's result is what the hook changed: moves, draft edits, the
auto-step flag, and its return value. A run-calls job holds several calls;
the driver feeds their completions back one at a time, in any order. Where a
job may wait (a question, a stream, a tool), an abort arrives as one more
input, and the machine says which jobs to cancel. Time comes in with each
result.

The phase loop in §6 is the machine's whole step function. Every rule is a
row of its tables: which phase applies, which hook point comes next, what a
string return does, what an abort cancels. All of it is reachable from
proptest without Python or a clock.

## Open questions

Each one has a recommendation, which is what the documents assume. A "no"
means a document changes before sign-off. Q14 and Q19 were folded into
earlier decisions, and Q29 into "only a `stop == tools` reply runs calls".
The seventh revision retires Q2, Q16, Q35, Q36 and Q38, settles Q13, Q33,
Q34 and Q37, and adds Q39–Q45.

| # | question | recommendation |
|---|---|---|
| Q1 | The plan names the node timestamp `at`, the same as `Cursor.at`. Rename? | **`time`.** One word, one meaning. |
| Q2 | *Retired (seventh revision).* "Committed" no longer exists: a node is on the tree once it is written, and any node may be gone to. | — |
| Q3 | Does a `Custom` node ever reach the model? | **No.** To show the model something, append a message-bearing node. The same holds for scratchpad items. |
| Q4 | Failure and abort. | **As in §8.** A sample that produced output is committed with its `stop` (`interrupted` or `error`). One that produced nothing leaves a `sample_error` side node. An aborted tools phase answers unfinished calls `Interrupted` at the join. Every abort happens in place: the draft is committed, hooks run on it, and only then does a frontend's move apply. |
| Q5 | Roots and opening. | **Every root is a `Config` node.** `Session.create(config, system)` appends the root `Config` and a `System` node under it (`by: core`), and opens the first cursor at the `System` node with an opening move. Detached roots are `Config` too. |
| Q6 | What does `opened_by` name? | **The opening cursor.** A cursor a tool or hook opens records the calling cursor; a frontend's is empty. |
| Q7 | What is the set of authors? | **`frontend(client)`, `provider`, `tools`, `core`, `extension(name)`.** A node's author is whoever produced its content as written. A hook's answer or rewrite makes its extension the author. A delivered input is the typing client's. Recorded only, never used for control flow. |
| Q8 | Node id format? | **A session-local integer assigned at append.** Deterministic, which suits I7 and tests. Item ids and cursor ids are session-local integers too, never reused. |
| Q9 | 0.15 had handler policies (timeouts on hooks). Keep any? | **Not in core.** A hook that needs a deadline uses `asyncio.timeout`. |
| Q10 | Is a written `Results` node final? | **Yes; another answer is another node.** Going back to the batch and stepping writes a sibling `Results`. |
| Q11 | Where does provider-specific legality live? | **In the provider's conversion**, which the driver runs before every sample. A rejection is a failed sample. |
| Q12 | Do cursors close? | **Not in core for now.** Cursors are cheap, and frontends hide finished ones. |
| Q13 | Two cursors at one batch both have `next == RunTools`. | **Settled (seventh revision): each runs it.** The claim is gone. The journal is per cursor, and each writes its own `Results`. "Run once" is an extension's, through the scratchpad. |
| Q15 | How does a person branch or go back? | **Go-to, step, and input.** A go-to (`Session.move`) turns auto-step off and wakes the cursor; no phase runs unless an extension turns auto-step on. On a busy cursor it aborts first, and it is refused while a hook runs or a question is open. `Session.step` runs one step's phases whatever auto-step is, so going to a batch and stepping re-runs it. `Harness` exposes all three. |
| Q16 | *Retired with I6.* | — |
| Q17 | Input arrives while `at` is a batch with no `Results`: a paused cursor, or after a reload. | **The batch runs first** (Q40). The input waits; a send turns auto-step on, the tools phase answers the batch (with the journal and D4 after a crash), and the next step delivers the input. To skip the batch, go to the reply's parent first. |
| Q18 | Which hooks run after a pre hook answers, and what may `pre_result` change? | **The draft's pre hooks run** (`pre_reply`, `pre_result`), so a cache hit still gets redacted. `pre_result` may change an outcome's content, not its kind, except to `Err` by returning a string. |
| Q20 | Where does "keep going" live, and what stops a runaway loop? | **In post hooks.** A `post_reply` hook writes a `User` node with `cursor.write`, and `next` becomes `Sample`. A runaway loop is stopped by `max_steps` in `Config`: the most committing steps a cursor takes without a frontend acting on it. Nothing is persisted, so a runaway loop cannot survive a reload. |
| Q21 | Do a batch's calls run in parallel? | **Yes, by default.** A tool can be `exclusive` and run alone. |
| Q22 | How does a person approve one call and deny another? | **`cursor.ask` from `pre_tool`** (through its node handle). Declined, the hook returns its own reason, which becomes the call's `Err`. Abort does not close the question. |
| Q23 | 0.15 had lifecycle hooks (`session_start`, `session_shutdown`, `session_before_switch`). | **None until a Phase 5 extension needs one.** The wake on load covers `session_start` for cursors. |
| Q24 | When `pre_tool` rewrites a call, is the executed call recorded? | **Not by core.** The `Assistant` node keeps the call as the model made it. A hook that wants an audit trail appends a `Custom` side node wherever it likes. |
| Q25 | May `pre_sample` change the request only through the tree? | **Yes.** It moves the cursor, so what is sent is `walk(at)` (I1). Reminders, model choices, and compaction are all visible in the tree. |
| Q26 | A hook that picks a cheaper model appends a `Config` node, and that choice persists on the line. Revert on its own? | **No automatic revert.** A routing hook that wants a one-off appends the restoring `Config` itself. |
| Q27 | What does a call that was never run become when the walk is sent again? | **`partial_calls: text` by default**, rendered as `[τ: interrupted while calling write_file; arguments so far: …]` after the reply's text. `partial_calls: omit` drops it. The exact markup is pinned in the Phase 4 provider ADR and tested byte for byte. |
| Q28 | Should a move record say who asked for it? | **Yes: `{cursor, from?, to, by, time, opened_by?}`**, with `by` one of `frontend(client)` (a go-to, an opening), `core` (a commit's move), `extension(name)` (a hook's move, write, or opening), or `tools` (an opening by a tool). Recorded only, never used for control flow. |
| Q30 | What body kind is a compaction summary or a `pre_sample` reminder? | **`User`, authored by the extension.** It is legal on every API's wire. |
| Q31 | When is input delivered, and what sees it? | **Settled (seventh revision): by core, in the input phase** (§7), through `pre_input`. An extension that must see every message watches `node_appended` or `pre_input`. |
| Q32 | Do opened cursors run hooks? | **Yes: opening wakes the cursor.** It is the new cursor's own step, not a hook on the opener's. A tool's handle may read, append, sample a walk, run a tool, open cursors, and write scratchpads, but it moves no cursor. |
| Q33 | What is a cursor id, and how is it named? | **Settled (seventh revision): a session-local integer, plus any number of labels.** Labels map unique strings to cursor ids, are set, moved, and removed by `Label` records, and are replayed on load. A label never names two cursors. |
| Q34 | An abort arrives while a call's `pre_tool` is still running. | **Settled (seventh revision): the hook finishes; no new effect starts.** A string it returns or an outcome it sets is used; if it returns `None`, the call's outcome is `Interrupted`. |
| Q35 | *Retired with "committed".* A cursor may open on any node. | — |
| Q36 | *Retired with `combine`.* Two clients' inputs are two `User` nodes. | — |
| Q37 | A question asked of one client: what if that client detaches? | **Settled (seventh revision): it is declined**, and `cursor.ask` returns `None`. A question for any client holds the cursor until some client answers. |
| Q38 | *Retired with "boundary".* A cursor hook may move its cursor; tool hooks cannot. | — |
| Q39 | Does a wake with auto-step off run any phase? | **No: only `pre_step` and `post_step`.** A go-to or a load browses; an extension that wants work done turns auto-step on in `pre_step`. `Session.step` is the frontend's way to run phases with auto-step off. |
| Q40 | Pending input at a batch with no `Results`: which goes first? | **The batch.** The input phase applies only when `next != RunTools`. Otherwise the input would go under the batch and leave the journal of a running or crashed batch with nothing to deliver it. |
| Q41 | A crash leaves journal items for a batch; the cursor then goes elsewhere, and later back. | **The journal is used** when a tools phase next runs that batch on that cursor: those calls ran, and their outcomes are true. A frontend that wants a clean re-run removes the items first; they are visible like any item. |
| Q42 | What happens to pending input on an abort? | **It stays pending, and the cursor rests.** Every frontend shows it and may edit or remove it. An extension may offer it back to its client in `post_step(aborted)`; none ships in the default config until a frontend needs it. |
| Q43 | How does a frontend learn that `pre_input` refused its input? | **A `notice` to that client with the reason**, and the item is removed. The step halts. A hook error leaves the input pending instead, because nothing decided against it. |
| Q44 | How does a hook see where other hooks moved the cursor? | **`cursor.moves(since_step=True)`**: the cursor's move records since this step's `pre_step` began. The first record's `from` is where the step started. No `prev` field. |
| Q45 | Should an `Assistant` node record which node it was sampled from (`sampled_from`), now that a reply may be committed elsewhere? | **No.** The move records already show every move between the sample and the commit. Add it if a debug frontend finds the records too indirect. |
