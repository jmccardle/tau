# Model

Status: **draft for review (Phase 1).** Expands `docs/plan.md` §1. On sign-off
this file and `glossary.md` supersede it. Every term used here has an entry in
`glossary.md`. Questions marked **Q*n*** are listed at the end with a
recommended answer each. The documents assume the recommendation.

The whole design rests on one idea: **the conversation is a tree of immutable
nodes, and everything else is either a pointer into that tree or a function of
it.** τ 0.15 kept per-turn state in objects (cursor fields, queues, frames,
suspension brokers), which a reload then had to guess back into existence.
Here nothing is state unless it is a node or a move record.

Notation: `x?` marks a field that may be absent. `A | B` means one of.

## 1. Stored objects

Only two things are ever written: nodes and move records.

```
Node  { id, parent?, time, by, body, copied_from?, derived_from? }

body  = System   { text }
      | User     { text, images }
      | Assistant{ text, thinking, calls }        each call = { id, tool, args }
      | Results  { one outcome per call id }      outcome = Ok(value) | Err(error)
                                                          | Skipped | Interrupted
      | Config   { snapshot }
      | Custom   { kind, data }

Move  { cursor, from?, to, time, opened_by? }
```

A **session** is the set of its nodes and moves. A **cursor** is
`{id, at, target}`:
- `at` is the `to` of the cursor's latest move.
- `target` exists only in memory (D6).

A move with no `from` opens a cursor. The plan has only commit moves; opening
moves are added here (Q5).

Every root is a `Config` node (Q5). So every walk starts with a `Config` node,
and `config_at` is always defined.

The core is pure. The driver passes `time` in. Core assigns ids at append (Q8).
No function in `tau-core` reads a clock, a file, or the network.

## 2. Derived views

None of these are stored. Each is a function of the stored objects.

| view | definition |
|---|---|
| `walk(n)` | root → … → `n`, following `parent`. Inclusive. |
| `LCA(a, b)` | the deepest node on both `walk(a)` and `walk(b)`; none if they are in different trees |
| committed | every node on `walk(m.to)`, for every move `m`, opening moves included. Permanent (Q2). |
| draft (of a cursor) | the nodes on `walk(target)` that are not on `walk(at)` and were appended during the current step |
| scratch | nodes that are neither committed nor in any cursor's draft |
| `config_at(n)` | the body of the nearest `Config` node on `walk(n)` |
| messages | the provider's conversion of a walk's message-bearing nodes |
| `next(walk)` | §5 |

The draft is defined without the LCA, so it also covers a target in another
tree, where no LCA exists.

One session, mid-step, two trees:

```
r    Config  (model: big)            committed  (root)
└ s    System                        committed
  └ u1   User "fix the bug"          committed
    ├ a1   Assistant  calls [c1]     committed
    │ └ x1   Results {c1: Ok}        committed   ◀ A.at
    │   └ a2   Assistant "done"      draft       ◀ A.target
    └ a1'  Assistant  calls [c9]     scratch     (a hook rejected this sample
                                                  and retargeted: a1 replaced it)
d    Config  (model: cheap)          scratch     (detached root: a hook's
└ d1   User "summarise u1..x1"       scratch      one-off call; its root records
  └ d2   Assistant "…summary…"       scratch      which model produced d2)
```

When cursor A commits, `a2` becomes committed and `A.at = a2`. If the step is
discarded instead, `A.target := x1` and `a2` becomes scratch. Either way `a2`
is never deleted or edited.

## 3. Invariants

Each invariant has a statement, a reason, and the test that holds the code to
it. Tests are in Phase 2 (`tests/behavior/`, Python, through `Harness`) and
Phase 3 (`crates/tau-core`, Rust unit tests and proptest).

### I1 Parent is context
**Statement.** A node's `walk(parent)` is exactly what produced it. For an
`Assistant` node written by a provider, that walk is the context its sample
was drawn from. For a `Results` node, its parent holds the calls it answers.
Any other relation is recorded as `copied_from` (verbatim) or `derived_from`
(computed), never as a parent.

**Why.** In 0.15 the model's view was a fold of the path with splices
(compaction) and filters, so "what did the model see?" had no direct answer.
Here it is always `walk(parent)`.

**Enforced by.**
- No API re-parents or splices. `append` takes the parent explicitly, and the
  driver appends a sample under the tip of the walk it sent.
- Behavior test: `FakeProvider` records the context of every sample. For
  every node with `by == provider`, the recorded context equals
  `walk(node.parent)`. Copies are not exempt by accident: a copy's `by` is
  whoever made the copy, never `provider` (Q7).

### I2 Immutability
**Statement.** No node changes after it is appended. To edit, append a sibling
and retarget.

**Why.** 0.15 wrote nodes "incomplete" and finalized them with a second write.
A process exit between the two left an orphan that nothing could repair.

**Enforced by.**
- `Node` has no setters in Rust and is a frozen class in Python; a behavior
  test checks that assignment raises.
- The store is append-only.
- Proptest: after any operation sequence, every node observed earlier still
  equals what was observed.

### I3 Committed `at`
**Statement.** A cursor's `at` is always a committed node whose walk passed
validation. Cursors open only at committed nodes, with one exception: the
session's first cursor opens at the root it creates (Q5).

**Why.** "Where is this cursor?" must never need a guess. 0.15's `default_leaf`
was a heuristic.

**Enforced by.**
- Unit test: `open_cursor` at a scratch node is an error.
- Proptest: after any operation sequence, every `cursor.at` is committed and
  `validate(walk(at))` passes.

### I4 Calls are answered
**Statement.** A node with calls has at most one committed child, and that
child is the `Results` node answering every call. This is checked twice:
- **I4a, at append.** Any child of a calls node must be a `Results` node whose
  keys are exactly its parent's call ids. "Any" includes scratch, so a hook
  cannot hang a `Custom` node off a calls node (Q10).
- **I4b, at commit.** A calls node never gains a second committed child.

I4a needs only the parent. I4b needs the parent's children and the committed
set.

**Why.** Every provider rejects a conversation with an unanswered call, and
running one batch of calls twice on one line of the conversation is never what
anyone meant.

**Consequence (Q10).** A committed answer is final. To change history above
it, branch from higher up, using copies or derived nodes, as prune does.

**Enforced by.**
- Unit tests for each rejection.
- Proptest: no calls node ever has two committed children.
- Behavior scenarios:
  - tool batch;
  - unknown tool;
  - bad arguments;
  - hook edits a tool result (an uncommitted sibling, retargeted before commit);
  - two cursors at one node (Q13).

### I5 Commit validates
**Statement.** `commit` checks `walk(target)` before `at := target`. If the
check fails, the commit is refused (§4).

**Why.** An illegal conversation should fail when it is made, not on the next
provider request, possibly days later.

**Rules.** `validate` in core is structural:
- **V1**: I4a and I4b hold along the walk.
- **V2**: the walk's first node is a `Config` node. This is also checked at
  append: a node without a parent must be `Config`.

Provider-specific legality is the provider's: system prompt placement,
consecutive `User` nodes, image support. At commit, the driver converts
`walk(target)` to messages, and a conversion error refuses the commit (Q11).

**Enforced by.**
- Unit tests per rule.
- Behavior scenario: a provider-illegal draft is rejected at commit, and `at`
  is unchanged.

### I6 Config follows the cursor
**Statement.** A commit never changes the effective config implicitly. If
`config_at(target) != config_at(at)` and the draft contains no `Config` node,
commit appends `Config(config_at(at))` under `target` (`by: core`). That new
node becomes the committed `at`. This is harmless: `next` looks past `Config`
nodes (§5). If `target` is a calls node, I4a forbids appending under it, so
the commit is refused (Q16).

**Why.** Branching to an old line must not silently switch the model,
provider, or tools to whatever was in force back then (D5).

**Enforced by.**
- Proptest: `config_at(at)` changes only across commits whose draft contains a
  `Config` node.
- Behavior scenario: branch across a config node.

### I7 Reload is replay
**Statement.** In-memory state is a pure function of the stored nodes and
moves: no heuristics, no repair, no "latest wins".

**Why.** 0.15's loader kept the last line per id and guessed a reopen
position. A crash could therefore produce a state that no sequence of
operations would have produced.

**Not persisted, by design:**
- `target`: after a load, `target == at` for every cursor, and any draft in
  progress is scratch (D6).
- the auto-step flag: every cursor loads with auto-step off.
- events and stream deltas.

**Enforced by.** Proptest: `state == load(save(state))` over random operation
sequences, including multiple cursors, drafts, scratch, and detached roots.
`state` means nodes, moves, the committed set, and each cursor's `at`.

## 4. Commit

In order:

1. If `target == at`, stop: nothing to do, nothing written.
2. Validate `walk(target)` (I4, I5). On failure, refuse.
3. If the config would change implicitly (I6):
   - if `target` has calls, refuse;
   - otherwise append the restating `Config` under `target` and make that node
     the new `target`.
4. Append the move record `{cursor, from: at, to: target, time}` and set
   `at := target`. Emit `cursor_moved`.

**Refused** always ends the same way:
- nothing is committed;
- `target := at`, so the draft becomes scratch;
- auto-step goes off;
- the frontend sees the reason.

The driver runs the provider's conversion check (Q11) just before step 2.

A commit may make an existing scratch node committed. For example, a frontend
can go back to the rejected `a1'` in §2 by targeting it (Q2). Validation
applies as usual.

Branching is a commit whose target is not below `at`:

```
before                          after commit of A (target was b2)
u1 ─ a1 ─ x1   ◀ A.at           u1 ─ a1 ─ x1                    still committed
 └ b1 ─ b2     ◀ A.target        └ b1 ─ b2 ─ c   ◀ A.at         c = restated Config, only
LCA(at, target) = u1                                            if b's line had different config (I6)
```

## 5. Next

What a cursor needs done is a pure function of `walk(at)`. Skip `Config` and
`Custom` nodes from the end, then decide on the nearest message-bearing node:

| nearest message-bearing node | `next` |
|---|---|
| `User` or `Results` | `Sample` |
| `Assistant` with calls | `RunTools` (those calls) |
| `Assistant` without calls, `System`, or none | `Wait` |

`next` reads only the walk, so two cursors at the same node always agree.

## 6. Step

A step is one cycle for one cursor. There are three kinds, named for what
produces the draft.

| kind | starts when | produce | hook chain |
|---|---|---|---|
| input | a frontend gives input | append `User` under `at`, or under a parent the frontend names (Q15) | `input` |
| sample | `next == Sample` | sample `walk(at)`, append the `Assistant` node under `at` | `sample` |
| tools | `next == RunTools` | run the calls (Q13), append one `Results` node under `at` | `tools` |

Each kind runs the same four phases:

1. **Produce** (per the table) and set `target` to the new node.
2. **Hooks.** The chain runs in order. Each hook may append anywhere,
   including on detached roots, and may move `target`. On the `input` and
   `sample` chains a hook may also set `target := at`, which refuses the step.
   On the `tools` chain it may not, because that would leave the calls
   unanswered (I4). A hook that declines calls appends a `Results` sibling
   with `Skipped` outcomes and targets it (Q14).
3. **Hold.** If auto-step is off, wait until the frontend resumes or discards
   the step. Discard is `target := at`.
4. **Commit** (§4).

If a step ends with `target == at` (refused, discarded, or failed), nothing is
committed, its nodes are scratch, and **auto-step goes off**. That stops a
refused or failed step from repeating forever: `walk(at)` hasn't changed, so
`next` hasn't either. With auto-step on, the driver keeps stepping until
`next` is `Wait`.

**Input when the cursor is not at rest.** Input lands only where
`next != RunTools`:
- If a step is in progress, the driver holds the input and starts an `input`
  step once the cursor reaches such a node (D2: the queue belongs to the
  frontend; core offers no queue).
- If the cursor is already at a calls node (for example after a reload), the
  driver resolves the calls first, by the resume rule in §7, then takes the
  input (Q17).

Opening another cursor is always available instead.

**Going somewhere without new input** (Q15). A frontend may set `target` to
any node and commit. No produce happens and no chain runs. This is how a
person returns to an older line.

## 7. Abort, failure, reload

- **Abort during a sample.** Cancel the request. Partial output is discarded
  (streamed text was only ever events). `target := at`, auto-step off. `next`
  is still `Sample`, so the frontend may retry, or the user may type.
- **Abort during tools.** Cancel the running calls. The driver appends and
  commits a `Results` node: finished calls keep their outcomes, the rest are
  `Interrupted`. Auto-step off. The commit is required; without it the cursor
  stays on an unanswered calls node (Q4).
- **A sample fails** (HTTP error, mid-stream error). The driver records the
  error as a scratch `Custom {kind: sample_error}` node under `at`, so the
  failure is persisted and visible but not part of the conversation. Nothing
  is committed. Auto-step off (Q4).
- **Resume at a calls node** (after a reload, or before taking input there):
  - If a complete `Results` child already exists as scratch (the tools
    finished but the commit never happened), target it. The `tools` chain
    runs, then the commit.
  - Otherwise run the idempotent calls again, write `Interrupted` for the
    rest, and commit one `Results` node (D4).
- **Crash during a sample.** On reload `next` is `Sample`. Resuming samples
  again.

## 8. Events

There is one `Event` type, and every event names its cursor:

| kind | when | carries |
|---|---|---|
| `node_appended` | any append, including draft and scratch | the node |
| `cursor_moved` | after a commit's move record is written | `from`, `to` |
| `stream_delta` | during a sample | a fragment of text, thinking, or a call |

Observers cannot block the driver or change anything. Print/json, RPC, serve,
and the TUI are each one projection of this stream.

## 9. Persistence and processes

- `tau-store` keeps, per session, an append-only node log and a move log, each
  with a versioned header. Loading reads both and replays them (I7). The
  on-disk layout is a Phase 3 ADR.
- **Lease.** There is one `flock` per session, taken on the node log. The
  holder writes; every other process gets the session read-only. If the
  holder's process is dead, the lease is reclaimed. 0.15 left a stale holder
  record that nothing checked.
- **Tools run once.** While a cursor runs a calls node's tools, the driver
  holds an in-memory claim on that node. Any other cursor that reaches it waits
  for the `Results` instead of running the calls again (Q13). The lease keeps
  the claim to one process.

## Open questions

Each one has a recommendation, which is what the documents assume. A "no"
means a document changes before sign-off.

| # | question | recommendation |
|---|---|---|
| Q1 | The plan names the node timestamp `at`, the same as `Cursor.at`. Rename it? | **`time`.** One word, one meaning. |
| Q2 | Is "committed" permanent, or does a node stop being committed when every cursor moves off it? And may a commit pick up old scratch, such as a rejected alternative? | **Permanent, and yes.** Committed is derivable from moves. Cursors can reopen on old lines. I4b depends on permanence. Validation still guards every commit. |
| Q3 | Does a `Custom` node ever reach the model? | **No.** To show the model something, append a message-bearing node. This keeps `walk` honest (I1) and the provider conversion small. |
| Q4 | Failure and abort. The plan says "`target := at`", but abort during tools must commit `Results` or I4 breaks. Agree with §7? | **Yes.** A failed sample leaves a scratch `Custom` node. Abort during tools commits `Interrupted` outcomes. |
| Q5 | Roots and opening. I3 says cursors open at committed nodes, but an empty session has none. | **Every root is a `Config` node.** Creating a session appends the root, and an opening move (a move with no `from`, which the plan doesn't have) commits it. Detached roots are `Config` too, so they record the model they used. |
| Q6 | What does `opened_by` name? | **The opening cursor.** A sub-agent's answer carries `derived_from`. |
| Q7 | What is the set of `Author`s? | **`frontend`, `provider`, `tools`, `core`, `extension(name)`.** A copy's author is whoever made the copy. Recorded only; never used for control flow. |
| Q8 | Node id format? | **A session-local integer assigned at append.** It is deterministic, which suits I7 and tests. JMFTS (Phase 7) maps its own ids. ADR in Phase 3. |
| Q9 | 0.15 had handler policies (hook timeouts). Keep any? | **Not in core.** A hook that needs a deadline uses `asyncio.timeout`. |
| Q10 | I4a is stricter than the plan: no child of any kind except `Results` under a calls node. I4b makes a committed answer final. | **Both, as written.** Otherwise two answers to one call could each be "the" answer. |
| Q11 | Where does provider-specific legality live? | **In the provider's conversion**, run by the driver at commit. Core stays provider-agnostic. |
| Q12 | Do cursors close? | **Not in core for now.** Cursors are cheap; frontends hide finished ones. Closing would need a new record type and an ADR. |
| Q13 | Two cursors at one calls node both have `next == RunTools`. | **Run once.** An in-memory claim per calls node (§9). The other cursor targets the resulting `Results`. This becomes a Phase 2 scenario. |
| Q14 | May a `tools` hook refuse the whole step? | **No.** It declines calls by answering them `Skipped`. A whole-step refusal would strand the calls. |
| Q15 | How does a person branch or go back (the plan's "edit = append sibling + retarget")? | **Two frontend operations.** Input with a named parent: an input step under that parent, then commit. Go to a node: set `target` and commit, with no chain. `Harness` exposes both. |
| Q16 | I6's restating `Config` goes *under* `target` and becomes `at`, and a commit to a calls node across a config change is refused. The plan says neither. | **As written.** No other place keeps both I4 and I6. |
| Q17 | Input arrives while the cursor is at a calls node. | **Resolve the calls first** (§7 resume rule), then take the input. Refusing the input instead would be an error the user didn't need. |
