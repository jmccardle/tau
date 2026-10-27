# Model

Status: **for review (Phase 1), fourth revision.** Expands `docs/plan.md` §1.
On sign-off this file and `glossary.md` supersede the plan. Every term used
here has an entry in `glossary.md`. D1–D10 are the decisions in `plan.md` §4.
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
- **Every write is followed by its move** (fourth revision). A cursor is
  `{id, at}`. The plan's `target`, draft, hold, and discard are gone, and D6
  becomes "a cursor is `{id, at}`; every write is followed by its move".
  - Each step ends with one move onto what it wrote. A tools step moves onto
    each result as it is written.
  - Validation happens at append, so nothing invalid is written, and at move,
    for where a cursor lands.
  - To reject a reply, go to its parent and sample again. The old reply stays
    in the tree as a committed sibling (Q2).
  - Auto-step off pauses a cursor *between* steps.
  - Core never writes a node and then abandons it. A node no move reaches is
    an extension's side node, a detached root's work, or a record of a
    failure.
- **Hook points sit around writes.** A `pre_<node>` hook rewrites a node
  before it is written; a `pre_<effect>` hook shapes an effect; a
  `post_<node>` hook runs after the move and before `next` is evaluated.
  - The points are `pre_input`, `post_input`, `pre_sample`, `pre_reply`,
    `post_reply`, `pre_tool`, `pre_result`, `post_result` (§7).
  - Pre hooks change what is written. Post hooks change what happens around
    a written node: they may append side nodes, or move the cursor.
  - This retires the plan's Phase 2 scenario "hook drafts on scratch and
    retargets". Its replacements are "`pre_sample` builds the context it
    returns" (a reminder, a model choice, or a compacted line) and "a post
    hook moves the cursor" (a synthetic sibling, or "continue").

The whole design rests on one idea: **the conversation is a tree of immutable
nodes, and everything else is either a pointer into that tree or a function of
it.** τ 0.15 kept per-turn state in objects: cursor fields, queues, frames,
suspension brokers. A reload had to guess that state back into existence.
Here nothing is state unless it is a node or a move record.

Notation: `x?` marks a field that may be absent. `A | B` means one of.

## 1. Stored objects

Only two things are ever written: nodes and move records.

```
Node  { id, parent?, time, by, body, copied_from?, derived_from? }

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

Parts are kept in the order they streamed. `args_raw` is the argument text
exactly as the model produced it, which may be incomplete or malformed. It is
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
cursor, not a node: the one that opened this one (Q6).

Every root is a `Config` node (Q5), so every walk starts with one and
`config_at` is always defined.

The core is pure. The driver passes `time` in, and core assigns ids at append
(Q8). No function in `tau-core` reads a clock, a file, or the network.

## 2. Batches

An `Assistant` node's `Call` parts form a **batch**. The batch's results
follow it in a **run**: one result per call, each under the previous, in the
order the calls finished.

Every call gets a result. Only a reply that handed off runs anything:

| reply's `stop` | call's `args_raw` | result |
|---|---|---|
| `tools` | parses | the tool runs, through `pre_tool` and `pre_result` |
| `tools` | doesn't parse | `Err("invalid arguments")`, without running. The model sees its own malformed output. |
| anything else | either | `Skipped(<stop>)`, written by core together with the reply (Q29). A truncated plan is not half-run. |

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
- side nodes an extension appends off the line, or anywhere else;
- work done on a detached root;
- the nodes a `pre_sample` hook built for a sample that then produced nothing;
- `sample_error` records (§8).

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
  the reply under it.
- Behavior test: `FakeProvider` records each sample's context and config. For
  every node with `by == provider`, the recorded context equals
  `walk(node.parent)`, and the recorded config equals `config_at(node.parent)`.
  A node a hook produced or rewrote has that hook's extension as author (Q7).

### I2 Immutability
**Statement.** No node changes after it is appended. Pre hooks (`pre_input`,
`pre_reply`, `pre_result`) edit a node *before* it is written; nothing edits
it afterwards. Post hooks see only written nodes.

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
validates. Nothing *lands* partway through a run. A go-to, a hook's move, or
`open_cursor` whose destination is an `Assistant` node with calls, or a node
inside its run, **fast-forwards** to the end of the existing run:
- if the run is complete (or there is none yet), the cursor lands there;
- if the run is partial, the landing cursor takes the batch's claim and
  resumes it (§8);
- if another cursor holds that claim, the move is refused.

So only the cursor holding a batch's claim sits partway through its run, and
it moves onto each result as it is written. The session's first cursor opens
at the root it creates (Q5).

**Why.** "Where is this cursor?" must never need a guess (0.15's
`default_leaf` was a heuristic). And a second cursor landing mid-run would see
`next == RunTools` and run the remaining calls a second time.

**Enforced by.**
- Unit tests: `open_cursor` and go-to onto a batch or into its run
  fast-forward to the run's end, and are refused while another cursor holds
  the batch's claim.
- Proptest: after any operation sequence, every `cursor.at` is committed,
  `validate(walk(at))` passes, and `at` is partway through a run only for the
  cursor holding that batch's claim, or for any cursor straight after a
  reload.

### I4 Every call gets exactly one result
**Statement.** On any walk, a batch is followed only by its own results, one
per call, until the batch is complete. Two checks enforce this at append, and
both are local: they read only the batch and its run.
- **I4a.** A result may be appended only at the end of its batch's run: under
  the `Assistant` node if the batch is unstarted, otherwise under the latest
  result, and only for a call that has no result yet on that walk. Under an
  unstarted or partial batch, no other kind of node may be appended.
- **I4b.** Nothing branches inside a run. A batch's `Assistant` node, and
  every result except the one that completes the batch, has at most one
  child. The completing result is an ordinary node, and any number of cursors
  may continue from it.

**Why.** Every provider rejects a conversation with an unanswered call, so a
blocked call still owes the model a result that says so. And running one
batch twice on one line of the conversation is never what anyone meant.

**Consequence (Q10).** A written result is final. To change history above
it, branch from higher up, using copies or derived nodes, as prune does.

**Enforced by.**
- Unit tests for each rejection.
- Proptest: no node in a run, other than the completing result, has two
  children.
- Behavior scenarios: tool batch; unknown tool; bad arguments; `pre_tool`
  blocks one call of two; `pre_result` redacts an output; two cursors at one
  batch (Q13); two cursors continuing from one completed batch.

### I5 Validation at append and at move
**Statement.** `append` checks a node against its walk before writing it, and
a move checks where it lands before writing the move record. Nothing invalid
is written, and no cursor lands anywhere invalid. A failed check refuses the
operation and changes nothing.

**Why.** An illegal conversation should fail when it is made, not on the next
provider request, possibly days later.

**Rules.** Core's checks are structural.
- At append:
  - **V1**: I4a and I4b.
  - **V2**: a node without a parent must be `Config`, so every walk starts
    with one.
- At a move (§5): I3, and I6 for a go-to or a hook's move.

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
There are two places a cursor's line can change:
- **At a sample:** `pre_sample` returns a context tip on another line, for
  example after compaction. If `config_at(tip) != config_at(at)`, and no node
  between `LCA(at, tip)` and the tip is a `Config` written by someone other
  than core, the driver appends `Config(config_at(at))` under the tip
  (`by: core`) before sampling. Then the sample runs with the cursor's own
  config. A `Config` the hook appended on purpose (a model choice, Q26) is
  explicit, so it stands and is not undone.
- **At a go-to or a hook's move** (§5): if `config_at(to) != config_at(at)`
  and none of the nodes the move commits is a `Config` node, the move appends
  the restating `Config` under `to` and lands on that node instead. `next`
  looks past it (§6). If `to` is an unstarted batch, I4a forbids appending
  under it, so the move is refused (Q16).

A config change that a hook or a frontend appends on purpose is explicit, and
it persists until another `Config` node replaces it (Q26).

**Why.** Branching to an old line must not silently switch the model,
provider, or tools to whatever was in force back then (D5).

**Enforced by.**
- Proptest: `config_at(at)` changes only across a move that commits a `Config`
  node something other than I6 appended.
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
- Events and stream deltas.

A cursor that was running a batch loads at the last result it moved onto, and
resume continues from there (§8).

A node is durable before any move that names it, and a step's write and its
move are durable together (§10). A torn final record, cut off by a crash, is
a write that never completed. Load ignores it. That is not repair: no state
is guessed, and the result is exactly the state before that write began.

Anything a later rule counts is computed from the walk, such as `max_steps`
(Q20).

**Enforced by.** Proptest: `state == load(save(state))` over random operation
sequences, including multiple cursors, partial batches, scratch, and detached
roots. `state` here means nodes, moves, the committed set, and each cursor's
`at`.

## 5. Move

A move is the only way `at` changes. Every move comes from one of three
places, and its `by` records which (Q28):
- **a step's own move** onto what the step just wrote (`by: core`);
- **a go-to** from a frontend (`by: frontend`, Q15);
- **a post hook's move** (`by: extension(name)`, §7).

In order:

1. If `to == at`, stop: there is nothing to do, and nothing is written.
2. I3: if `to` is an `Assistant` node with calls, or a node inside its run,
   and this cursor does not hold that batch's claim, fast-forward `to` to the
   end of the existing run. If the run is partial, take the batch's claim, so
   that the cursor resumes it after landing (§8). If another cursor holds the
   claim, refuse.
3. For a go-to or a hook's move, apply I6 (§4). Refuse if `to` is an unstarted
   batch and I6 would restate.
4. Append the move `{cursor, from: at, to, by, time}`, set `at := to`, and
   emit `cursor_moved`.

A **refused** move changes nothing and raises to whoever asked for it: the
frontend, or the hook. A hook that does not catch it fails like any hook
error (§7).

**A step's own move is never refused.** Its node passed append's checks, the
cursor is the one writing the run, and I6 does not apply. A tools step in
particular always moves: its tools have already run, and their effects are
real.

Branching is a move whose `to` is not below `at`:

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
| any node where `next` would be `Sample`, once `max_steps` is reached on the walk (Q20) | `Wait` |
| `User` | `Sample` |
| `Assistant` with `stop == tools` (unstarted batch) | `RunTools` (all its calls) |
| a result, batch partial | `RunTools` (the calls not yet answered on the walk) |
| a result, batch complete, and the batch's reply had `stop == tools` | `Sample` |
| a result, batch complete, any other `stop` (the calls were `Skipped`, Q29) | `Wait` |
| any other `Assistant`, `System`, or none | `Wait` |

Two cursors at the same node always agree on `next`. The "partial" row arises
only at the `at` of the cursor running or resuming that batch (I3), including
after a reload.

## 7. Steps and hook points

A step is one cycle for one cursor. It writes a node and moves onto it, with
hook points before the effect, before the write, and after the move. At each
hook point, hooks run in the order their extensions are listed in
`config_at(at)`, and each receives what the previous one returned.

| step | starts when | pre hook (effect) | effect | pre hook (node) | writes, then moves onto | post hook |
|---|---|---|---|---|---|---|
| input | a frontend gives input | none | none | `pre_input(user)` → a body to write, or nothing | that body | `post_input(node)` |
| sample | `next == Sample` | `pre_sample(tip)` → a context tip, or a reply | provider request | `pre_reply(reply)` → a reply, or nothing | one `Assistant` node, plus `Skipped` results for its calls if `stop != tools`; the move lands on the last (Q29) | `post_reply(node)` |
| tools | `next == RunTools` | per call: `pre_tool(call)` → a call, or an outcome | run the tool | per call: `pre_result(call, outcome)` → an outcome | one result per call, each moved onto as it is written | per result: `post_result(node)` |

**Phases of a step**, in order:
1. **pre hooks on the effect** (`pre_sample`, `pre_tool`);
2. **effect**;
3. **pre hooks on the node** (`pre_input`, `pre_reply`, `pre_result`);
4. **write and move**: append the node and move onto it (§5). In a tools step,
   calls run concurrently unless a tool is `exclusive` (Q21), but writing is
   serialised per cursor: a result is written and moved onto, and its
   `post_result` hooks finish, before the next completed result is written.
   Completed outcomes wait their turn in completion order;
5. **post hooks** (`post_input`, `post_reply`, `post_result`) on the written
   node.

Then the driver evaluates `next` on `walk(at)`. With auto-step on, it keeps
stepping until `next` is `Wait`. With auto-step off, the cursor pauses after
each step until the frontend resumes it.

What each pre hook may do:
- **`pre_input`** may rewrite the input, or return a different body to write
  instead. That is how a command is **handled**: `/model fast` returns a
  `Config` body, which is written and moved onto like input. Returning nothing
  **refuses** the input, and nothing is written.
- **`pre_sample`** may return a different context tip: a node it appended under
  the tip (a reminder), a `Config` node (a model choice, which persists, Q26),
  or the end of a new line it built (compaction, Q25). The driver applies I6
  and samples `walk(tip)` with `config_at(tip)`. Before calling the provider,
  the driver checks that a reply could be appended under the tip (I4a); if
  not, it refuses the step. The step's move onto the reply commits those
  nodes too. The hook may instead **answer** with a reply
  (a cache hit), and then the provider is not called.
- **`pre_reply`** may rewrite the reply (a guard, redaction), or return nothing
  to **refuse** it. A refused reply, even one with calls, writes nothing; its
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

What a post hook may do. It receives the written node after the cursor has
moved onto it, before `next` is evaluated.
- It may **append side nodes** anywhere the append rules allow: a `Custom`
  note under the node, or work on a detached root. These are scratch unless a
  move later reaches them.
- It may **move the cursor**, through `api`. The move is checked like a go-to
  (§5). Two examples:
  - `post_input` writes a sibling `u1'` of the typed input `u1`
    (`derived_from: u1`, `by: extension(name)`) and moves onto it. The typed
    original stays in the tree, committed. Use `pre_input` to change what is
    written; use `post_input` to keep what was typed and continue from
    something else.
  - `post_reply` appends `User "continue"` under the reply and moves onto it,
    so `next` becomes `Sample` (Q20, Q29).
- `post_result` may move only on the result that completes the batch, because
  nothing branches inside a run (I4b).
- A move by a post hook ends that node's post hooks: the later ones do not
  run.

Nodes and moves written by hooks trigger no hooks. Post hooks do not run after
an abort, and an abort while a post hook is running cancels it and skips the
remaining post hooks. They do run after a failure that wrote a reply
(`stop: error`).

**Hook errors.** An uncaught exception from any hook ends that hook point:
the remaining hooks there do not run. Auto-step goes off, and the frontend
is shown the error.
- In a pre hook on an effect, the effect does not run. In a sample step,
  nothing is written. In a tools step, the call is answered
  `Err("hook error")` (`by: core`).
- In `pre_input` or `pre_reply`, nothing is written.
- In `pre_result`, the tool has run. The call is answered `Err("hook error")`
  (`by: core`), and the raw output is withheld, because the hooks that would
  have redacted it did not finish.
- In a post hook, the node is already written and moved onto. A refused move
  raises into the hook; uncaught, it is a hook error like any other.

A step that ends without moving its cursor (a refusal, or a sample that failed
before any output) **turns auto-step off**. Without that rule, a refused or
failed sample would repeat forever: `walk(at)` hasn't changed, so `next`
hasn't either.

**Input when the cursor is not at rest.** Input lands only where
`next != RunTools`.
- If a step is in progress, the driver keeps the input until the cursor
  reaches such a node, then starts an input step. Under D2 the queue is the
  frontend's; core offers none.
- If the cursor's `at` is an unstarted or partial batch (a paused cursor, or
  after a reload), the driver first answers the batch, then takes the input
  (Q17).

**Going somewhere without new input** (Q15). A frontend may give input under a
named parent, or go to any node: a move, which runs no hooks. This is how a
person branches, returns to an older line, or rejects a reply (go to its
parent and step again).

## 8. Abort, failure, reload

- **Abort during a sample.** Cancel the request. Then:
  - **If no parts had arrived:** write nothing, leave `at` where it was, and
    turn auto-step off. `next` is still `Sample`. Nodes `pre_sample` built for
    this sample are scratch.
  - **Otherwise:** write the reply with the parts so far and
    `stop: interrupted`, after `pre_reply` (redaction still applies). Core
    answers any calls `Skipped(interrupted)`, including a call cut off
    mid-arguments (Q29). Move onto it, run no post hooks, and turn auto-step off.
    `next` is `Wait`.

  The user saw those tokens, so they are stored and the model sees them next
  time (§11).
- **Abort during tools.** Cancel the running calls and their hooks. Results
  already written stay. Each call without a written result gets an
  `Interrupted` result (`by: core`), written and moved onto like any result.
  That includes a call whose tool finished while its `pre_result` was still
  running: its raw outcome is never written unredacted. No post hooks run, and
  auto-step goes off (Q4).
- **A sample fails.** This covers HTTP errors, a stream that breaks partway,
  and the provider's conversion rejecting the walk. Auto-step goes off either
  way (Q4).
  - **Output had arrived:** the reply is written and moved onto like an
    aborted one, but with `stop: error`. `stop_raw` and the error text are
    kept. Post hooks run.
  - **Nothing arrived** (for example a 401 or 429 before the first token):
    there is no reply. The driver records the error as a `Custom
    {kind: sample_error}` node under the context tip. It is persisted and
    visible, but it is scratch, not part of the conversation, and the cursor
    does not move.
- **Length, refusal.** These are ordinary replies with `stop: length` or
  `stop: refused`. Their calls are `Skipped` and `next` is `Wait` (Q29). Continuing
  automatically after `length` is an extension's choice, for example a
  `post_reply` hook that appends "continue" and moves onto it (Q20).
- **Resume at a batch.** This happens after a reload, before taking input at
  a batch a paused cursor sits on, and after a move fast-forwarded onto a
  partial run (I3).
  0. Take the batch's claim (§10). If another cursor holds it, wait for the
     batch to complete, then fast-forward to the end of its run (I3); there
     is nothing left to resume.
  1. With the claim held, re-read the run: if the tree holds results of this
     batch below `at`, move onto the end of that run. A batch has at most one
     run: I4b allows each node in it one child, and the claim stops a second
     runner. The driver does this step before running any tool, whether or
     not it is resuming.
  2. For each call still unanswered on that walk:
     - if its tool is idempotent, run it, with its pre hooks, the tool, and
       `pre_result`;
     - otherwise answer it `Interrupted` (D4).

     Each result is written and moved onto as usual, so the batch completes.

  This is why each call gets its own result. A ten-minute `bash` that finished
  is not marked `Interrupted` because its sibling was still running.
- **Crash during a write.** A step's node and its move are durable together
  (§10), so a crash leaves both or neither. A torn final record is ignored on
  load.
- **Crash during a sample.** On reload `next` is `Sample`, so resume samples
  again.

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

## 10. Persistence and processes

- `tau-store` keeps, per session, an append-only node log and a move log, each
  with a versioned header. Loading reads both and replays them (I7). The
  on-disk layout is a Phase 3 ADR.
- **Durability.** A node is durable before any move that names it. A step's
  write and its move are durable together: a crash leaves both or neither.
  The Phase 3 store ADR picks how, for example one combined log, or one
  record holding both. A torn final record is a write that never completed,
  and load ignores it (I7).
- **Lease.** There is one `flock` per session, taken on the node log. The
  holder writes; every other process gets the session read-only. If the
  holder's process is dead, the lease is reclaimed. 0.15 left a stale holder
  record that nothing checked.
- **Claim.** While a cursor runs or resumes a batch, the driver holds an
  in-memory claim on the batch's `Assistant` node. A cursor takes the claim
  before it runs any call, and re-reads the run once it has it (§8 resume).
  Any other cursor that reaches the batch at rest, or resumes it, waits for
  the batch to complete, then fast-forwards to the end of the run instead of
  running the calls again (Q13). A go-to, hook move, or `open_cursor` that
  would land partway through a run while another cursor holds the claim is
  refused (I3). The lease keeps every claim inside one process.

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
   - a `Call` part that is not answered by a run (a truncated or skipped call),
     rendered according to `config.partial_calls`;
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
review decisions and Q20.

| # | question | recommendation |
|---|---|---|
| Q1 | The plan names the node timestamp `at`, the same as `Cursor.at`. Rename? | **`time`.** One word, one meaning. |
| Q2 | Is "committed" permanent, or does a node stop being committed once every cursor has moved off it? May a move land on old scratch, such as an extension's side line? | **Permanent, and yes.** Committed is derivable from moves, and cursors can reopen on old lines. A rejected reply stays committed; the line simply moved away from it. Validation still guards every move. |
| Q3 | Does a `Custom` node ever reach the model? | **No.** To show the model something, append a message-bearing node. That keeps `walk` honest (I1) and the provider conversion small. |
| Q4 | Failure and abort. The plan discards a failed step, but abort during tools must still answer every call or I4 breaks, and tokens the user saw should not vanish. | **As in §8.** A sample that produced output is written with its `stop` (`interrupted` or `error`) and moved onto. A sample that produced nothing leaves at most a scratch `sample_error` node. Abort during tools answers unwritten calls `Interrupted` and moves onto them. |
| Q5 | Roots and opening. I3 says cursors open at committed nodes, but an empty session has none. | **Every root is a `Config` node.** Creating a session appends the root, and an opening move (a move with no `from`, which the plan doesn't have) commits it. Detached roots are `Config` too, so each records the model it used. |
| Q6 | What does `opened_by` name? | **The opening cursor.** A sub-agent's answer carries `derived_from`. |
| Q7 | What is the set of authors? | **`frontend`, `provider`, `tools`, `core`, `extension(name)`.** A node's author is whoever produced its content as written. A hook's answer or rewrite makes its extension the author, and so does a copy. Authors are recorded only, never used for control flow. |
| Q8 | Node id format? | **A session-local integer assigned at append.** It is deterministic, which suits I7 and tests. JMFTS (Phase 7) maps its own ids. ADR in Phase 3. |
| Q9 | 0.15 had handler policies (timeouts on hooks). Keep any? | **Not in core.** A hook that needs a deadline uses `asyncio.timeout`. |
| Q10 | A written result is final (I4b). | **Yes.** Otherwise two answers to one call could each be "the" answer. Change history by branching from higher up. |
| Q11 | Where does provider-specific legality live? | **In the provider's conversion**, which the driver runs before every sample. A rejection is a failed sample. Core stays provider-agnostic. |
| Q12 | Do cursors close? | **Not in core for now.** Cursors are cheap, and frontends hide finished ones. Closing would need a new record and an ADR. |
| Q13 | Two cursors at one batch both have `next == RunTools`. | **Run once**: an in-memory claim per batch (§10), and the second cursor moves onto the completed run. This becomes a Phase 2 scenario. |
| Q15 | How does a person branch or go back? | **Two frontend operations.** Input under a named parent runs an input step there. "Go to" is a move, with no hooks. `Harness` exposes both. |
| Q16 | A go-to across a config change restates the `Config` *under* the node it goes to. A go-to to an unstarted batch is then refused, because I4a forbids appending there. | **As written.** No other placement keeps both I4 and I6, and going to an unanswered batch on another line is rare. |
| Q17 | Input arrives while `at` is an unstarted or partial batch: a paused cursor, or after a reload. | **Answer the batch first, then take the input.** Calls the driver knows never started are answered `Skipped("superseded by input")`. Calls that might have started (after a reload) follow resume: idempotent calls re-run, the rest are `Interrupted`. Refusing the input would raise an error the user didn't need. |
| Q18 | Which hooks run after a pre hook answers, and what may `pre_result` change? | **The node's pre hooks run** (`pre_reply`, `pre_result`), so a cache hit still gets redacted. `pre_result` may change an outcome's content but not its kind: it cannot turn `Skipped` into `Ok`. The driver enforces that. |
| Q20 | Where does "keep going" live (a hook that sends more input after the model stops), and what stops a runaway loop? | **In post hooks.** A `post_reply` hook may append a `User` node (`by: extension(name)`) under the reply and move onto it, and `next` becomes `Sample`. As a hook's write, it runs no input hooks. A `max_steps` field in `Config` limits the number of `Assistant` nodes on the walk since the last node an input step wrote (whoever its author); once reached, `next` is `Wait` where it would be `Sample` (§6). The count is computed from the walk, so it survives a reload (I7). |
| Q21 | Do a batch's calls run in parallel? | **Yes, by default.** A tool can be `exclusive` and run alone, for example two edits to one file. |
| Q22 | How does a person approve one call and deny another? | **A `pre_tool` hook awaits an answer from the frontend.** Nothing is written while it waits. Abort or a crash leaves the call unanswered, and resume answers it `Interrupted`. This needs a fourth event kind (a question to the frontend) and an `api` call to answer it. The same mechanism lets a `pre_reply` hook show a reply to a person before it is written. 0.15's suspensions tried to do this, so it gets its own ADR. |
| Q23 | 0.15 had lifecycle hooks (`session_start`, `session_shutdown`, `session_before_switch`). | **None until a Phase 5 extension needs one.** None of the three does. |
| Q24 | When `pre_tool` rewrites a call (a sandbox, say), is the executed call recorded? | **Not by core.** The `Assistant` node keeps the call as the model made it, and the result answers it. A hook that wants an audit trail writes a `Custom` node on a detached root, because nothing but results may hang off a batch (I4a). |
| Q25 | May `pre_sample` change the request only by returning a context tip, never by editing the request invisibly? | **Yes.** That is I1's guarantee. Reminders, model choices, and compaction are all visible in the tree. |
| Q26 | A `pre_sample` hook that picks a cheaper model appends a `Config` node, and that choice persists on the line. Should one-off choices revert on their own? | **No automatic revert.** A routing hook that wants a one-off appends the restoring `Config` at its next `pre_sample`. The tree then shows exactly which model produced each reply, and nothing reverts behind anyone's back. |
| Q27 | What does a truncated or skipped call become when the walk is sent again, and what does τ's markup look like? | **`partial_calls: text` by default**, rendered as `[τ: interrupted while calling write_file; arguments so far: …]` after the reply's text. `partial_calls: omit` drops it. Stop notes use the same `[τ: …]` form. The exact markup is pinned in the Phase 4 provider ADR and tested byte for byte, because changing it later invalidates every cached prefix that contains it. |
| Q28 | Hooks can now move a cursor. Should a move record say who asked for it? | **Yes: `{cursor, from?, to, by, time, opened_by?}`**, with `by` one of `frontend` (a go-to, an opening), `core` (a step's own move), or `extension(name)` (a hook's move). Like a node's author, it is recorded only, never used for control flow. |
| Q29 | A reply with `stop != tools` gets `Skipped` results written under it, and the step moves onto the last. Then `post_reply`'s "the cursor has moved onto it" is false; a `User "continue"` appended under the reply is refused by I4b when the reply had a call; and whether `post_result` runs for core-written `Skipped` results is unspecified. | **Calls in a reply with `stop != tools` are not runnable and owe no result.** Core writes no `Skipped` nodes for them, the step moves onto the reply, and `next` is `Wait`. The provider renders them per `partial_calls` (§11). I4 applies only to replies with `stop == tools`. Passages marked (Q29) still describe the `Skipped` results and change if this is accepted. |
