# Model

Status: **draft for review (Phase 1), third revision.** Expands `docs/plan.md`
§1. On sign-off this file and `glossary.md` supersede the plan. Every term used
here has an entry in `glossary.md`. D1–D10 are the decisions in `plan.md` §4.
Questions marked **Q*n*** are listed at the end, each with a recommendation;
the documents assume the recommendation.

**Decided in review** (2026-10-26). These supersede the plan:
- **Calls stay inside the `Assistant` node** that produced them. One sample
  produced them all at once, so no call is a node of its own.
- **One result node per call** (this reverses D7's default). Each result is
  written as soon as its call finishes. The batch commits once, when every
  call is answered.
- **Five hook points: `pre_input`, `pre_sample`, `post_sample`, `pre_tool`,
  `post_tool`.** A pre hook rewrites a request before its effect runs, or
  answers it itself. A post hook rewrites a result before it is written. Hooks
  never move `target`. (A sixth point, `idle`, is proposed in Q20.)
  - This retires the plan's Phase 2 scenario "hook drafts on scratch and
    retargets". Its replacement is "`pre_sample` builds the context it
    returns": a reminder, a model choice, or a compacted line.
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

Move  { cursor, from?, to, time, opened_by? }
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

A **session** is the set of its nodes and moves. A **cursor** is
`{id, at, target}`. `at` is the `to` of the cursor's latest move. `target`
exists only in memory (D6). A move with no `from` opens a cursor (Q5).
`opened_by` names a cursor, not a node: the one that opened this one (Q6).

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
| `tools` | parses | the tool runs, through `pre_tool` and `post_tool` |
| `tools` | doesn't parse | `Err("invalid arguments")`, without running. The model sees its own malformed output. |
| anything else | either | `Skipped(<stop>)`, written by core together with the reply. A truncated plan is not half-run. |

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
| draft (of a cursor) | the uncommitted nodes on `walk(target)`, which is exactly what a commit would make permanent |
| scratch | nodes that are neither committed nor in any cursor's draft |
| `config_at(n)` | the body of the nearest `Config` node on `walk(n)` |
| messages | the provider's conversion of a walk's message-bearing nodes |
| `next(walk)` | §6 |

A draft may contain nodes from earlier steps. Two examples: the results a
crash left behind, which resume targets, and a compaction candidate built ahead
of time. Their inclusion is deliberate. Whatever is on `walk(target)` and not
yet committed is what the commit is about.

One session, mid-step, two trees:

```
r    Config  (model: big)            committed  (root)
└ s    System                        committed
  └ u1   User "fix the bug"          committed
    └ a1   Assistant  calls [c3, c5] committed   ◀ A.at
      └ r5   Result c5: Ok           draft       ◀ A.target   (c3 still running)
d    Config  (model: cheap)          scratch     (detached root: an earlier step's
└ d1   User "summarise s..u1"        scratch      hook made a one-off call; its root
  └ d2   Assistant "…summary…"       scratch      records which model produced d2)
```

When `c3` finishes, `r3` is appended under `r5` and the batch commits:
`A.at = r3`. If the process dies first, `r5` is scratch on reload, and resume
continues the run from it, so `c5` does not run again (§8).

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
**Statement.** No node changes after it is appended. Post hooks edit a result
*before* it is written; nothing edits it afterwards.

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
validates. In particular, `at` is never partway through a batch. Cursors open
only at such nodes. The one exception is the session's first cursor, which
opens at the root it creates (Q5).

**Why.** "Where is this cursor?" must never need a guess (0.15's
`default_leaf` was a heuristic).

**Enforced by.**
- Unit tests: `open_cursor` fails at a scratch node, and at a committed result
  partway through its run.
- Proptest: after any operation sequence, every `cursor.at` is committed and
  `validate(walk(at))` passes.

### I4 Every call gets exactly one result
**Statement.** On any walk, a batch is followed only by its own results, one
per call, until the batch is complete. Two checks enforce this, and both are
local: they read only the batch and its run.
- **I4a, at append.** A result may be appended only at the end of its batch's
  run: under the `Assistant` node if the batch is unstarted, otherwise under
  the latest result, and only for a call that has no result yet on that walk.
  Under an unstarted or partial batch, no other kind of node may be appended.
- **I4b, at commit.** A commit may not leave a batch partial: `walk(target)`
  ends either at an unstarted batch (a sample step) or after a complete one.
  Within a run, nothing branches on committed nodes: a batch's `Assistant`
  node, and every result except the one that completes the batch, has at most
  one committed child. The completing result is an ordinary node, and any
  number of cursors may continue from it.

**Why.** Every provider rejects a conversation with an unanswered call, so a
blocked call still owes the model a result that says so. And running one
batch twice on one line of the conversation is never what anyone meant.

**Consequence (Q10).** A committed result is final. To change history above
it, branch from higher up, using copies or derived nodes, as prune does.

**Enforced by.**
- Unit tests for each rejection.
- Proptest: no committed walk ends partway through a batch. No node in a run,
  other than the completing result, has two committed children.
- Behavior scenarios: tool batch; unknown tool; bad arguments; `pre_tool`
  blocks one call of two; `post_tool` redacts an output; two cursors at one
  batch (Q13); two cursors continuing from one completed batch.

### I5 Commit validates
**Statement.** `commit` checks `walk(target)` before `at := target`. If the
check fails, the commit is refused (§5).

**Why.** An illegal conversation should fail when it is made, not on the next
provider request, possibly days later.

**Rules.** `validate` in core is structural:
- **V1**: I4a and I4b hold along the walk.
- **V2**: the walk's first node is a `Config` node. This is also checked at
  append: a node without a parent must be `Config`.

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
  example after compaction. If `config_at(tip) != config_at(at)`, the driver
  appends `Config(config_at(at))` under the tip (`by: core`) before sampling.
  Then the sample runs with the cursor's own config.
- **At a "go to" commit** from a frontend (§7): if
  `config_at(target) != config_at(at)` and the draft contains no `Config`
  node, commit appends the restating `Config` under `target` and commits that
  node instead. `next` looks past it (§6). If `target` is an unstarted batch,
  I4a forbids appending under it, so the commit is refused (Q16).

A config change that a hook or a frontend appends on purpose is explicit, and
it persists until another `Config` node replaces it (Q26).

**Why.** Branching to an old line must not silently switch the model,
provider, or tools to whatever was in force back then (D5).

**Enforced by.**
- Proptest: `config_at(at)` changes only across a commit whose draft holds a
  `Config` node that something other than I6 appended.
- Behavior scenarios: branch across a config node; compaction across a model
  change.

### I7 Reload is replay
**Statement.** In-memory state is a pure function of the stored nodes and
moves. No heuristics, no repair, no "latest wins".

**Why.** 0.15's loader kept the last line per id and guessed a reopen
position. A crash could therefore produce a state no sequence of operations
would have produced.

**Not persisted, by design:**
- `target`: after a load, `target == at` for every cursor. An unfinished draft
  is scratch, apart from a batch's run, which resume continues (§8).
- The auto-step flag: every cursor loads with it off.
- Events and stream deltas.

Anything a later rule counts is computed from the walk, such as `max_steps`
(Q20).

**Enforced by.** Proptest: `state == load(save(state))` over random operation
sequences, including multiple cursors, drafts, partial batches, scratch, and
detached roots. `state` here means nodes, moves, the committed set, and each
cursor's `at`.

## 5. Commit

In order:

1. If `target == at`, stop: there is nothing to do, and nothing is written.
2. Validate `walk(target)` (I4, I5). On failure, refuse.
3. For a "go to" commit, apply I6 (§4). Refuse if `target` is an unstarted
   batch.
4. Append the move `{cursor, from: at, to: target, time}`, set
   `at := target`, and emit `cursor_moved`.

A **refused** commit always ends the same way: nothing is committed;
`target := at`, so the draft becomes scratch; auto-step goes off; and the
frontend sees the reason.

**A tools commit is never refused and never discarded.** The tools have
already run, and their effects are real. A complete run under a committed
`Assistant` always passes V1 and V2. I6 cannot apply, because no `Config`
node can sit inside a run. Holding a tools step therefore offers only resume.
Without this rule, a discard would leave `next == RunTools`, and the next step
would run a `bash` command a second time.

A commit may make an existing scratch node committed. For example, a frontend
can return to an alternative it rejected (Q2). Validation applies as usual.

Branching is a commit whose target is not below `at`:

```
before                          after "go to" b2
u1 ─ a1 ─ r1   ◀ A.at           u1 ─ a1 ─ r1                    still committed
 └ b1 ─ b2     ◀ A.target        └ b1 ─ b2 ─ c   ◀ A.at         c = restated Config, only
LCA(at, target) = u1                                            if b's line had different config (I6)
```

## 6. Next

`next(walk)` says what a walk needs done. It is a pure function. The driver
applies it to `walk(at)`; resume applies it to the end of a run (§8). It skips
`Config` and `Custom` nodes from the end, then looks at the nearest
message-bearing node:

| nearest message-bearing node | `next` |
|---|---|
| `User` | `Sample` |
| `Assistant` with `stop == tools` (unstarted batch) | `RunTools` (all its calls) |
| a result, batch partial | `RunTools` (the calls not yet answered on the walk) |
| a result, batch complete, and the batch's reply had `stop == tools` | `Sample` |
| a result, batch complete, any other `stop` (the calls were `Skipped`) | `Wait` |
| any other `Assistant`, `System`, or none | `Wait` |

Two cursors at the same node always agree on `next`. The "partial" row never
arises at a cursor's `at` (I3). It arises only when resume continues a run.

## 7. Steps and hook points

A step is one cycle for one cursor. It has an effect with hook points on each
side. At each hook point, hooks run in the order their extensions are listed in
`config_at(at)`, and each receives what the previous one returned.

| step | starts when | pre hook | effect | post hook | writes |
|---|---|---|---|---|---|
| input | a frontend gives input | `pre_input(user)` → a body to write, or nothing | none | none | that body |
| sample | `next == Sample` | `pre_sample(tip)` → a context tip, or a reply | provider request | `post_sample(reply)` → a reply, or nothing | one `Assistant` node, plus `Skipped` results for its calls if `stop != tools` |
| tools | `next == RunTools` | per call: `pre_tool(call)` → a call, or an outcome | run the tool | per call: `post_tool(call, outcome)` → an outcome | one result per call |

What each hook point may do:
- **`pre_input`** may rewrite the input, or return a different body to write
  instead. That is how a command is **handled**: `/model fast` returns a
  `Config` body, which is written and committed like input. Returning nothing
  **refuses** the input, and nothing is written.
- **`pre_sample`** may return a different context tip: a node it appended under
  the tip (a reminder), a `Config` node (a model choice, which persists, Q26),
  or the end of a new line it built (compaction, Q25). The driver applies I6
  and samples `walk(tip)` with `config_at(tip)`. The hook may instead
  **answer** with a reply (a cache hit), and then the provider is not called.
- **`post_sample`** may rewrite the reply (a guard, redaction), or return
  nothing to **refuse** it. A refused reply, even one with calls, writes
  nothing; its calls never existed in the tree.
- **`pre_tool`** may rewrite the call (a sandbox, a remote host). It may also
  answer with an outcome and skip the tool: a cached or mocked `Ok`, or
  `Skipped("blocked by …")`.
- **`post_tool`** receives the call as it ran (after `pre_tool`'s rewrites) and
  its outcome. It may rewrite the outcome's content but not its kind (Q18).
  It must return an outcome, because every call is owed a result (I4).

An answer from a pre hook skips the remaining pre hooks and the effect. The
post hooks still run (Q18). A refusal ends the step, and no further hooks run.

**Phases of a step**, in order:
1. **pre hooks**;
2. **effect**;
3. **post hooks**;
4. **write**: append the produced node and set `target` to it. In a tools step,
   each result is written as soon as its call's `post_tool` returns, at the end
   of the run, and `target` follows. Calls run concurrently unless a tool is
   `exclusive` (Q21).
5. **hold**: if auto-step is off, wait for the frontend to resume or, except in
   a tools step, discard (`target := at`);
6. **commit** (§5). A tools step commits once, when the batch is complete.

A step that ends with `target == at` (refused, discarded, or failed) leaves its
nodes as scratch and **turns auto-step off**. Without that rule, a refused or
failed sample would repeat forever: `walk(at)` hasn't changed, so `next`
hasn't either. With auto-step on, the driver keeps stepping until `next` is
`Wait`. What happens then is Q20.

**Input when the cursor is not at rest.** Input lands only where
`next != RunTools`.
- If a step is in progress, the driver holds the input until the cursor reaches
  such a node, then starts an input step. Under D2 the queue is the frontend's;
  core offers none.
- If the cursor's `at` is an unstarted batch (a held sample, or after a
  reload), the driver first answers the batch, then takes the input (Q17).

**Going somewhere without new input** (Q15). A frontend may give input under a
named parent, or set `target` to any node and commit ("go to"). A go-to runs
no hooks. This is how a person branches, or returns to an older line.

## 8. Abort, failure, reload

- **Abort during a sample.** Cancel the request. Then:
  - **If no parts had arrived:** write nothing, set `target := at`, and turn
    auto-step off. `next` is still `Sample`.
  - **Otherwise:** write the reply with the parts so far and
    `stop: interrupted`, after `post_sample` (redaction still applies). Core
    answers any calls `Skipped(interrupted)`, including a call cut off
    mid-arguments. Commit at once, without holding, and turn auto-step off.
    `next` is `Wait`.

  The user saw those tokens, so they are stored and the model sees them next
  time (§11).
- **Abort during tools.** Cancel the running calls and their hooks. Results
  already written stay. Each call without a written result gets an
  `Interrupted` result (`by: core`). That includes a call whose tool finished
  while its `post_tool` was still running: its raw outcome is never written
  unredacted. The batch commits, and auto-step goes off (Q4).
- **A sample fails.** This covers HTTP errors, a stream that breaks partway,
  and the provider's conversion rejecting the walk. Auto-step goes off either
  way (Q4).
  - **Output had arrived:** the reply is written and committed like an
    aborted one, but with `stop: error`. `stop_raw` and the error text are
    kept.
  - **Nothing arrived** (for example a 401 or 429 before the first token):
    there is no reply. The driver records the error as a scratch
    `Custom {kind: sample_error}` node under the context tip. It is persisted
    and visible, but it is not part of the conversation, and nothing is
    committed.
- **Length, refusal.** These are ordinary replies with `stop: length` or
  `stop: refused`. Their calls are `Skipped` and `next` is `Wait`. Continuing
  automatically after `length` is an extension's choice, for example an
  `idle` hook that answers "continue" (Q20).
- **Resume at a batch.** This happens after a reload, or before taking input
  at an unstarted batch.
  1. If a run exists under the batch's `Assistant` node, target its end. A
     batch has at most one run: the claim (§10) stops a second runner, a tools
     step is never discarded, and resume extends the run rather than starting
     a new one.
  2. For each call still unanswered on that walk:
     - if its tool is idempotent, run it, with pre hooks, the tool, and post
       hooks;
     - otherwise answer it `Interrupted` (D4).
  3. Commit the batch.

  This is why each call gets its own result. A ten-minute `bash` that finished
  is not marked `Interrupted` because its sibling was still running.
- **Crash during a sample.** On reload `next` is `Sample`, so resume samples
  again.

## 9. Events

There is one `Event` type, and every event names its cursor:

| kind | when | carries |
|---|---|---|
| `node_appended` | any append, including draft and scratch | the node |
| `cursor_moved` | after a commit's move is written | `from`, `to` |
| `stream_delta` | during a sample | a fragment of text, thinking, or a call |

Observers cannot block the driver or change anything. Print/json, RPC, serve,
and the TUI are each a projection of this stream. Per-call approval may need a
fourth kind (Q22).

## 10. Persistence and processes

- `tau-store` keeps, per session, an append-only node log and a move log, each
  with a versioned header. Loading reads both and replays them (I7). The
  on-disk layout is a Phase 3 ADR.
- **Lease.** There is one `flock` per session, taken on the node log. The
  holder writes; every other process gets the session read-only. If the
  holder's process is dead, the lease is reclaimed. 0.15 left a stale holder
  record that nothing checked.
- **Claim.** While a cursor runs a batch, the driver holds an in-memory claim
  on the batch's `Assistant` node. Any other cursor that reaches that node
  waits for the batch to commit, then goes to the completed run instead of
  running the calls again (Q13). The lease keeps every claim inside one
  process.

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
| Q2 | Is "committed" permanent, or does a node stop being committed once every cursor has moved off it? May a commit pick up old scratch, such as a rejected alternative? | **Permanent, and yes.** Committed is derivable from moves, cursors can reopen on old lines, and I4b depends on permanence. Validation still guards every commit. |
| Q3 | Does a `Custom` node ever reach the model? | **No.** To show the model something, append a message-bearing node. That keeps `walk` honest (I1) and the provider conversion small. |
| Q4 | Failure and abort. The plan says "`target := at`", but abort during tools must commit or I4 breaks, and tokens the user saw should not vanish. | **As in §8.** A sample that produced output is written with its `stop` (`interrupted` or `error`) and committed. A sample that produced nothing leaves at most a scratch `sample_error` node. Abort during tools answers unwritten calls `Interrupted` and commits. |
| Q5 | Roots and opening. I3 says cursors open at committed nodes, but an empty session has none. | **Every root is a `Config` node.** Creating a session appends the root, and an opening move (a move with no `from`, which the plan doesn't have) commits it. Detached roots are `Config` too, so each records the model it used. |
| Q6 | What does `opened_by` name? | **The opening cursor.** A sub-agent's answer carries `derived_from`. |
| Q7 | What is the set of authors? | **`frontend`, `provider`, `tools`, `core`, `extension(name)`.** A node's author is whoever produced its content as written. A hook's answer or rewrite makes its extension the author, and so does a copy. Authors are recorded only, never used for control flow. |
| Q8 | Node id format? | **A session-local integer assigned at append.** It is deterministic, which suits I7 and tests. JMFTS (Phase 7) maps its own ids. ADR in Phase 3. |
| Q9 | 0.15 had handler policies (timeouts on hooks). Keep any? | **Not in core.** A hook that needs a deadline uses `asyncio.timeout`. |
| Q10 | A committed result is final (I4b). | **Yes.** Otherwise two answers to one call could each be "the" answer. Change history by branching from higher up. |
| Q11 | Where does provider-specific legality live? | **In the provider's conversion**, which the driver runs before every sample. A rejection is a failed sample. Core stays provider-agnostic. |
| Q12 | Do cursors close? | **Not in core for now.** Cursors are cheap, and frontends hide finished ones. Closing would need a new record and an ADR. |
| Q13 | Two cursors at one batch both have `next == RunTools`. | **Run once**: an in-memory claim per batch (§10), and the second cursor goes to the completed run. This becomes a Phase 2 scenario. |
| Q15 | How does a person branch or go back? | **Two frontend operations.** Input under a named parent runs an input step there. "Go to" sets `target` and commits, with no hooks. `Harness` exposes both. |
| Q16 | A go-to across a config change restates the `Config` *under* `target`. A go-to whose target is an unstarted batch is then refused, because I4a forbids appending there. | **As written.** No other placement keeps both I4 and I6, and going to an unanswered batch on another line is rare. |
| Q17 | Input arrives while `at` is an unstarted batch: a held sample, or a reload. | **Answer the batch first, then take the input.** Calls the driver knows never started are answered `Skipped("superseded by input")`. Calls that might have started (after a reload) follow resume: idempotent calls re-run, the rest are `Interrupted`. Refusing the input would raise an error the user didn't need. |
| Q18 | Which hooks run after a pre hook answers, and what may `post_tool` change? | **The post hooks run**, so a cache hit still gets redacted. `post_tool` may change an outcome's content but not its kind: it cannot turn `Skipped` into `Ok`. The driver enforces that. |
| Q20 | Where does "keep going" live (a hook that sends more input after the model stops), and what stops a runaway loop? | **A sixth hook point, `idle`.** It runs when a commit leaves `next == Wait`, and its hooks may give input, which passes through `pre_input` like any input. A `max_steps` field in `Config` limits the number of `Assistant` nodes on the walk since the last `User` node authored by `frontend`. The count is computed from the walk, so it survives a reload (I7). |
| Q21 | Do a batch's calls run in parallel? | **Yes, by default.** A tool can be `exclusive` and run alone, for example two edits to one file. |
| Q22 | How does a person approve one call and deny another? | **A `pre_tool` hook awaits an answer from the frontend.** Nothing is written while it waits. Abort or a crash leaves the call unanswered, and resume answers it `Interrupted`. This needs a fourth event kind (a question to the frontend) and an `api` call to answer it. 0.15's suspensions tried to do this, so it gets its own ADR. |
| Q23 | 0.15 had lifecycle hooks (`session_start`, `session_shutdown`, `session_before_switch`). | **None until a Phase 5 extension needs one.** None of the three does. |
| Q24 | When `pre_tool` rewrites a call (a sandbox, say), is the executed call recorded? | **Not by core.** The `Assistant` node keeps the call as the model made it, and the result answers it. A hook that wants an audit trail writes a `Custom` node on a detached root, because nothing but results may hang off a batch (I4a). |
| Q25 | May `pre_sample` change the request only by returning a context tip, never by editing the request invisibly? | **Yes.** That is I1's guarantee. Reminders, model choices, and compaction are all visible in the tree. |
| Q26 | A `pre_sample` hook that picks a cheaper model appends a `Config` node, and that choice persists on the line. Should one-off choices revert on their own? | **No automatic revert.** A routing hook that wants a one-off appends the restoring `Config` at its next `pre_sample`. The tree then shows exactly which model produced each reply, and nothing reverts behind anyone's back. |
| Q27 | What does a truncated or skipped call become when the walk is sent again, and what does τ's markup look like? | **`partial_calls: text` by default**, rendered as `[τ: interrupted while calling write_file; arguments so far: …]` after the reply's text. `partial_calls: omit` drops it. Stop notes use the same `[τ: …]` form. The exact markup is pinned in the Phase 4 provider ADR and tested byte for byte, because changing it later invalidates every cached prefix that contains it. |
