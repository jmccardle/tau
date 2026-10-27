# Glossary

Status: **draft for review (Phase 1).** On sign-off this file and `model.md`
supersede `docs/plan.md` §1.

Rule 2: no type, field, or module-level concept exists without an entry here.
Each entry has one meaning. If a word needs a second meaning, it needs a
second word.

Identifiers are proposals until Phase 3 builds them. Rust names live in
`tau_core`, and Python names in `tau_next`. "Retires" lists the τ 0.15 terms
(from the as-built review §7) that the entry replaces. "New" means 0.15 had no
counterpart. The full retirement table is at the end.

Questions marked **Q*n*** are open and listed in `model.md`, Open questions.

---

## The tree

### node
**Rust** `Node`, `NodeId` · **Python** `Node`, ids are `int` (Q8) ·
**retires** entry, message entry, custom entry, side entry, finalize

One immutable record in a session:
`{id, parent?, time, by, body, copied_from?, derived_from?}`. It is written
once and never changed (I2). There is no "incomplete" node, and no second
write that completes one.

### body
**Rust** `Body` · **Python** `Node.body`, one class per kind ·
**retires** entry type, `customType`

What a node holds. A body is exactly one of six kinds:

| kind | holds | reaches the model? |
|---|---|---|
| `System` | the system prompt text | yes |
| `User` | text and images from a person, or written on their behalf | yes |
| `Assistant` | parts (text, thinking, calls) in the order they streamed, and a stop reason | yes |
| `Result` | one call's id and its outcome | yes |
| `Config` | a full snapshot of the config in force | no |
| `Custom` | `{kind, data}`, owned by an extension or the driver | no (Q3) |

The first four kinds are **message-bearing**. `Config` and `Custom` are
bookkeeping, and the provider never sees them.

Known `Custom` kinds are listed here as they are introduced:
- `sample_error`: written by the driver. It records, as scratch, a sample
  that failed before any output arrived (`model.md` §8).

### part
**Rust** `Part` · **Python** `Part` (one class per kind) · **retires** content
block (as a mutable list)

One piece of a reply, in the order it streamed:
- `Text(text)`;
- `Thinking(text, signature?)`;
- `Call{id, tool, args_raw}`.

Parts are stored as they arrived and never reorganised. Every structured view,
such as runnable calls, is derived from them.

### stop
**Rust** `Stop` · **Python** `Stop` · **retires** `stopReason`,
`AgentEndReason` (as a reply property)

Why a reply's generation ended, normalised across providers. The provider's
own string is kept as `stop_raw`.

| `stop` | means |
|---|---|
| `end` | the model chose to stop |
| `tools` | the model handed off to tool execution |
| `length` | the output limit was reached |
| `refused` | the provider's filter or refusal |
| `interrupted` | we cancelled it (abort) |
| `error` | the stream failed after output had arrived |

Only `tools` runs calls. On every other stop, core answers the reply's calls
`Skipped(<stop>)` and `next` is `Wait`.

### call
**Rust** `Call`, `CallId` · **Python** `Call` · **retires** tool call,
toolCall block, `BlockedCall`

A part of an `Assistant` body that requests one tool: `{id, tool, args_raw}`.
- `CallId` is the provider's id, unique within its node.
- **`args_raw`** is the argument text exactly as the model produced it, which
  may be incomplete or malformed. It is parsed when the call runs and never
  re-serialised for storage.
- A call is **runnable** when its reply's `stop` is `tools` and its `args_raw`
  parses.

A call is never a node of its own, because one sample produced all of a
reply's calls at once.

### batch
**Python**: none (a view) · new

The calls of one `Assistant` node. On a walk, a batch is in one of three
states:
- **unstarted**: no results yet;
- **partial**: some calls answered;
- **complete**: every call answered exactly once (I4).

A cursor's `at` is never at a partial batch (I3).

### run
**Python**: none (a view) · new

A batch's result nodes: one per call, each under the previous, in the order
the calls finished. A batch has at most one run (`model.md` §8). Within a run,
nothing branches on committed nodes, except at the result that completes it
(I4b).

### result
**Rust** `Body::Result` · **Python** `Result` (body class) · **retires** tool
result, toolResult message, `Results` (one node per batch, D7's old default)

A node answering one call: `{call, outcome}`. Its parent is the batch's
`Assistant` node (for the first result) or the previous result in the run.
Every call gets exactly one result before anything else may follow its batch,
even when the call is blocked or its output redacted.

### outcome
**Rust** `Outcome` · **Python** `Outcome` · **retires** `is_error`,
`BlockedCall`

What a result says:

| outcome | meaning |
|---|---|
| `Ok(value)` | the tool ran and returned a value, or a `pre_tool` hook supplied one (cache, mock) |
| `Err(error)` | the tool ran and failed, or could not run (unknown tool; `args_raw` doesn't parse) |
| `Skipped(reason)` | deliberately not run: a `pre_tool` hook declined it ("blocked by …"), or the reply stopped for a reason other than `tools` |
| `Interrupted` | no result was written: the call was aborted, or a crash or abort came before its result was written. The tool may or may not have run (D4) |

### author
**Rust** `Author` · **Python** `Node.by` · **retires** `source:"agent"`,
submitter, `SubmissionSource`

Who wrote a node:
- `frontend`: a person's input.
- `provider`: a sample.
- `tools`: a result from running a call.
- `core`: a root, I6's restated config, or an `Interrupted` result.
- `extension(name)`: a hook.

A node's author is whoever produced its content as written. A hook's answer or
rewrite makes that hook's extension the author, and so does a copy. The author
is recorded and never used for control flow (Q7).

### time
**Rust** `Node::time` · **Python** `Node.time` · **retires** `timestamp`

The wall-clock time a node was appended. The driver supplies it; core never
reads a clock. The plan calls this field `at`, which collides with
`Cursor.at` (Q1).

### copied_from
**Rust** `Node::copied_from` · **Python** `Node.copied_from` · new

The node whose body this node repeats *verbatim*, under a different parent.
Compaction's copied tail produces these. A copy has its own parent and
therefore its own context (I1).

### derived_from
**Rust** `Node::derived_from` · **Python** `Node.derived_from` · new

The node whose walk this node's content was *computed* from:
- a summary points at the last node it summarised;
- a prune rewrite points at the node it rewrote;
- a sub-agent's answer points at the sub-agent's final node.

One id is enough, because the walk to that id is the input.

### session
**Rust** `Session` · **Python** `Session` · **retires** `AgentSession`,
`SessionLog`, file `Session`, `SessionManager`, `AgentSessionRuntime`,
catalog, `ReplicaSession`

One conversation: a forest of nodes plus its cursors. Append-only. A session
is persisted as nodes and move records, and nothing else (I7). In 0.15 this
word had ten meanings; here it has one.

### tree
**Python**: none (a view) · new

A root and all its descendants. A session is a forest of one or more trees.

### root
**Python**: none (a view) · **retires** session header entry

A node with no parent. Every root is a `Config` node, so every walk starts
with the config in force (Q5). A session's first node is its first root.

### detached root
**Python**: none (a view) · **retires** side-completion sessions,
`_EphemeralConversationSession`

Any root after the first. It holds one-off work a hook does outside the
conversation, such as a cold summary from a cheap model. Its `Config` records
which model did the work. Its nodes are scratch unless a cursor commits onto
them.

### walk
**Rust** `Session::walk(id)` · **Python** `Session.walk(node_id)` ·
**retires** fold, path, `Cursor.context()`, compaction splice

`walk(n)` is the nodes root → … → `n`, following `parent`, inclusive. Nothing else: no splicing,
no filtering, no rendering. Every view the model gets is a walk.

### tip
The last node of a walk: `walk(n)`'s tip is `n`.

### line
A walk, seen as one path through the tree. To branch is to leave one
line for another.

### LCA
**Rust** `Session::lca(a, b)` · **Python** `Session.lca(a, b)` · new

Lowest common ancestor: the deepest node on both `walk(a)` and `walk(b)`. None
when `a` and `b` are in different trees.

---

## Position

### cursor
**Rust** `Cursor`, `CursorId` · **Python** `Cursor` · **retires** head cursor,
acting cursor, `TURN_CURSOR`, `TurnFrame`, `RemoteCursor`

An insertion point: `{id, at, target}`, plus the driver's in-memory
`auto_step` flag.
- Several cursors may share a session, and even a node.
- A cursor carries no settings: those come from the walk (`config_at`).
- A cursor carries no queues: those belong to frontends (D2).
- Cursors do not close (Q12).

### open_cursor
**Rust** `Session::open_cursor(at, opened_by?)` · **Python**
`Session.open_cursor(at, opened_by=None)` · **retires** `spawn`, `newest`,
cursor registration

Creates a cursor and appends its opening move. The node must be committed and
its walk must validate, so a cursor never opens partway through a batch (I3).

### at
**Rust** `Cursor::at` · **Python** `Cursor.at` · **retires** leaf,
`pre_turn_leaf`

The cursor's committed node. Always valid and committed (I3). This is where
the conversation *is*, for that cursor.

### target
**Rust** `Cursor::target` · **Python** `Cursor.target` · new (0.15 wrote at
the leaf directly)

The tip of the cursor's draft: where the conversation *will be* if the step
commits. It equals `at` between steps. Not persisted (D6).

### draft
**Python** `Session.draft(cursor)` (a view) · new

Derived, never stored. The uncommitted nodes on `walk(target)`, which are
exactly what a commit would make permanent. Most were appended in the current
step. A draft may also include older nodes on the way: the results a crash
left behind, or a compaction candidate built ahead of time.

### committed
**Python** `Session.is_committed(node_id)` (a view) · new

A node is committed if it lies on `walk(m.to)` for some move record `m`,
opening moves included. Committed is permanent: moving a cursor away does not
uncommit anything (Q2).

### scratch
**Python**: a view · **retires** side entry, orphaned suspension

Derived, never stored. Nodes that are neither committed nor in any cursor's
current draft. That covers:
- hook work that was not kept;
- detached-root calls;
- samples that failed before any output arrived;
- refused or discarded drafts;
- drafts orphaned by a crash (D6).

Scratch is persisted like any node and stays inspectable. It is just not on
anyone's conversation.

### append
**Rust** `Session::append(parent?, by, body, …)` · **Python**
`Session.append(...)` · **retires** `append_at`, `_CursorWriter`, write group

Writes one new node and emits `node_appended`. It checks two rules first:
- I4a: under an unstarted or partial batch, only a result answering one of its
  unanswered calls may be appended.
- V2: a parentless node must be `Config`.

Appending never moves a cursor.

### commit
**Rust** `Session::commit(cursor)` · **Python** `Session.commit(cursor)` · new
(0.15 had no single point where a write became final)

The only way `at` changes. It validates `walk(target)` (I4, I5), restates
config if I6 requires it, appends one move record, and sets `at := target`.
A refused commit leaves `at` unchanged, sets `target := at`, and turns
auto-step off (`model.md` §5). A tools commit is never refused: its effects
already happened.

### move record
**Rust** `Move` · **retires** navigate entry

The persisted fact of one commit, or of one cursor opening:
`{cursor, from?, to, time, opened_by?}`. `from` is absent on an opening move.
`opened_by` names the cursor that opened this one, such as a sub-agent's
parent (Q6).

### branch
**Rust/Python**: no type; a verb · **retires** fork (in-session),
`commit_branch`, `BranchPlan`, `branch_summary`, `spawn_branch`

To branch is to commit a target that is not a descendant of `at`. The cursor
leaves its old line at the LCA, or entirely if there is none. The old line
stays committed.

### fork
**retires** `ctx.fork`, catalog fork, runtime fork, `multitask_strategy="fork"`,
and eight other senses

A new session whose first nodes copy `walk(n)` from another session, each copy
with `copied_from`. Fork crosses sessions and never means anything inside one.
Nothing before Phase 6 needs it.

---

## Execution

### next
**Rust** `next(walk) -> Next` · **Python** `Session.next(cursor)` (a method;
a function named `next` would shadow a builtin) · **retires** agent loop
position, `LoopPosition`

What a cursor needs done, as a pure function of `walk(at)`. It skips `Config`
and `Custom` nodes from the end and decides on the nearest message-bearing
node:

| nearest message-bearing node | `Next` |
|---|---|
| `User`, or a result completing a batch whose reply had `stop == tools` | `Sample` |
| `Assistant` with `stop == tools`, or a result in a partial batch | `RunTools(unanswered ids)` |
| any other `Assistant` or completed batch, `System`, or none | `Wait` |

### step
**retires** loop turn, agent run, user turn, submission, turn (all four
senses, D1)

One cycle for one cursor (`model.md` §7). Its phases run in order:
1. pre hooks;
2. the **effect**: a provider request, tools, or nothing for input;
3. post hooks;
4. **write** the produced node(s) as draft;
5. **hold**;
6. **commit**.

There are three **step kinds**:
- `input`: a frontend gives input;
- `sample`: `next` is `Sample`;
- `tools`: `next` is `RunTools`. A tools step writes one result per call as
  each call finishes, and commits once.

A step either commits or ends with `target := at`.

### reply
**Python** `Assistant` body, before it is written · **retires**
`AssistantMessage` (as a mutable in-flight object)

What a sample returns, as `pre_sample` (when it answers) and `post_sample` see
it. A reply becomes an `Assistant` node when it is written.

### hold
The fifth phase of a step. If the cursor's auto-step is off, the driver waits
here for the frontend to **resume**, or to **discard** (not available in a
tools step).

### discard
Ends a held input or sample step without committing: `target := at`, and the
draft becomes scratch. A tools step cannot be discarded. Its tools have
already run, and discarding would leave `next == RunTools`, which would run
them again.

### auto-step
**Python** `Cursor.auto_step` (driver memory) · **retires** step mode,
suspension, `api.suspend`, `PausableDeadline`, responder

A per-cursor flag. When it is on, the driver commits each step without holding
and keeps stepping until `next` is `Wait`. When it is off, every step holds.
It goes off on its own when a step ends without committing (refused,
discarded, failed, aborted), so a failure never repeats unattended. Every
cursor loads with it off. To **pause** a cursor is to turn its auto-step off.

### resume
Continue a held step, or turn auto-step back on. At an unstarted or partial
batch, resume follows `model.md` §8:
1. Continue the batch's run, if a crash left one.
2. Re-run unanswered idempotent calls.
3. Answer the rest `Interrupted` (D4).
4. Commit.

### idempotent
**Python** `Tool.idempotent: bool` · new

A tool flag meaning that running the tool twice has the same effect as running
it once. Only idempotent calls are re-run on resume (D4).

### sample
**Rust**: none (an effect) · **Python** `Provider.sample` · **retires**
completion, stream (as a noun), model call

One provider request over a walk. It produces one `Assistant` node, appended
under that walk's tip. Parts arrive as `stream_delta` events and are never
written token by token. Whatever stops the sample (an end, a handoff, a limit,
an abort, an error), the accumulated parts are written once, provided any
arrived.

### context
**retires** `ExtensionContext`/`ctx`, the `context` hook, `context=` parameter

The walk a sample is drawn from, before provider conversion. That is
`walk(at)`, unless a `pre_sample` hook returned a different tip; then it is
`walk(tip)`. "Context window" is the provider's limit on it,
in tokens.

### message
**retires** message entry, `customMessage`, `AgentMessage`

The provider's wire unit. **messages** = the provider's conversion of a
context's message-bearing nodes. Never stored.

### config
**Rust** `Body::Config`, `config_at(n)` · **Python** `Config` (pydantic,
Phase 4) · **retires** config entry, `CONFIG_KEYS`, `TurnFrame`, settings
(`Settings`, `settings.json`), model entry

Everything that decides how a step runs: provider, model, request settings,
tools, and the hooks in force. A `Config` node stores it as a full
**snapshot**. `config_at(n)` is the body of the nearest `Config` node on
`walk(n)`. It is always defined, because every root is a `Config` node.
Partial updates are never folded together.

A **restating** `Config` is one core appends (I6) to keep a cursor's config
when its line changes. Every other `Config` node is an explicit change, and
it persists until another `Config` node replaces it (Q26).

Fields named so far:
- **`max_steps`** (Q20): the most `Assistant` nodes allowed on the walk since
  the last `User` node authored by `frontend`. The count is computed from the
  walk, not stored.
- **`partial_calls`** (Q27): `text` (the default) or `omit`. It sets what a
  call with no run (truncated or skipped) becomes when the walk is sent again:
  `text` renders it after the reply in τ's `[τ: …]` markup, so the model sees
  what it was writing, and `omit` drops it.

### validate
**Rust** `validate(walk)`

The structural check commit runs on `walk(target)`: rules V1 and V2 (I5).
Provider legality is checked separately, by the provider's conversion (Q11).

### hook
**Python** `async def hook(api, cursor, …)`, one signature per hook point ·
**retires** the 0.15 hooks (`input`, `before_agent_start`, `tool_call`,
`tool_result`, `turn_end`, `user_turn_end`, `annotate`, `entry_written`, …),
handler policy, suspension site, chain

A Python function an extension registers at one **hook point**. At each hook
point, hooks run in the order their extensions are listed in the config. Each
receives what the previous one returned:
- A **pre** hook sees a request before its effect runs. It may rewrite the
  request, or **answer** it: the effect and the remaining pre hooks are
  skipped, but the post hooks still run (Q18).
- A **post** hook sees a result before it is written, and may rewrite it.

Any hook may also use `api` to append nodes elsewhere, for example on a
detached root. Hooks do not move `target`.

### hook point
**Python** the registration name · new

One of:

| hook point | receives | returns |
|---|---|---|
| `pre_input` | the input (a `User` body) | a body to write: the input, possibly rewritten, or another body when a command is **handled** (`/model fast` returns a `Config`). Or nothing: **refused**, and nothing is written |
| `pre_sample` | the context tip | a context tip (the same, or a node it appended: a reminder, a `Config`, a compacted line), or a reply (answered) |
| `post_sample` | the reply | the reply, or nothing (refused, nothing written) |
| `pre_tool` | one call | the call (possibly rewritten), or an outcome (answered) |
| `post_tool` | the call as it ran, and its outcome | an outcome of the same kind, with its content possibly rewritten (Q18). It cannot drop one: every call is owed a result (I4) |
| `idle` (Q20) | the cursor, after a commit leaves `next == Wait` | nothing; it may give input |

### refuse
Ending a step from `pre_input` or `post_sample` by returning nothing. The
remaining hooks do not run, nothing is committed, and auto-step goes off.
`pre_tool` cannot refuse; it answers `Skipped` instead.

### handled
Input that `pre_input` turned into a different body, such as a command turned
into a `Config` node. That body is written and committed in place of a `User`
node.

### answer
A pre hook returning the effect's result itself, such as a cached reply or a
mocked or blocked outcome, so that the effect does not run.

### claim
**Python**: driver memory · new

The driver's in-memory mark that a cursor is running a batch. Any other
cursor that reaches the batch waits, then goes to the completed run, so tools
run once (Q13). The lease keeps claims within one process.

### event
**Rust** `Event` · **Python** `Event` · **retires** `AgentEvent`, `WireEvent`,
serve event, RPC event, bus channel, hook event dict

One type, tagged with a cursor, in three kinds: `node_appended`,
`cursor_moved`, `stream_delta`. Observers cannot block or change anything.
Every wire format is a projection of this one type.

### abort
**retires** `AbortSignal`, `AbortReason`, abort-tree, `rollback`

Cancel a cursor's in-flight sample or tools, then turn its auto-step off. An
aborted sample that had produced output is written with
`stop: interrupted`, its calls are `Skipped`, and it is committed; one that
had produced nothing writes nothing. Aborted tools keep the results already
written, answer each unanswered call `Interrupted`, and commit the batch,
which I4 requires (`model.md` §8, Q4).

---

## Effects and edges

### provider
**Python** `Provider` · **retires** backend (config key), `ProviderSpec`,
`api` (as a selector), client pool

The Python object that turns a context into a sample, for one API family
(OpenAI-compatible first). It also converts walks to messages, and that
conversion is where provider-specific legality lives (Q11). What every
provider owes the driver (stop mapping, faithful replay, reasoning rules,
cache reporting) is listed in `model.md` §11. In 0.15 this word had four
meanings; here it means only this adapter.

### model
**Python** `Config.model` · **retires** `Model` (pydantic endpoint
descriptor), model entry, `model_spec`

The name of the model a provider samples from, spelled as the provider's API
spells it. Behavior switches belong to the provider and its typed settings,
not to the model.

### tool
**Python** `Tool` · **retires** `AgentTool`, `ToolDefinition` (×2),
`ExtensionToolDefinition`, `ToolInfo`, `ToolDescription`

One type for every tool, built-in or extension. A tool has:
- a name;
- an argument schema;
- an `idempotent` flag;
- an `exclusive` flag: it runs alone, never concurrently with the batch's
  other calls (Q21);
- an async function from arguments to an outcome.

### extension
**retires** bucket, `ExtensionHandlers`, `ExtensionRunner`, `ExtensionState`,
`ExtensionAPI` (as a 42-method surface)

A Python module that registers hooks and tools through the public API.
Extensions have no special standing in core (D3, D8). The first three
(Phase 5) prove the model:
- **compaction**: summarises an old prefix and branches onto a shorter line
  made of the summary and copies of the recent nodes.
- **prune**: after the provider's cache expires, rewrites old tool outputs and
  thinking as derived nodes, then branches onto the rewrite.
- **sub-agent**: a tool that opens a cursor at a committed node, runs it, and
  answers the call with a value `derived_from` that cursor's final node.

### api
**Python**: the first argument to a hook

What a hook may do besides returning its value: read the session, append
nodes (for example on a detached root), sample a walk, run a tool, open a
cursor, and, at `idle`, give input. It cannot move `target`; the hook point's
return value is how a hook steers the step. The exact surface is a Phase 4
decision. It is not the 0.15 `ExtensionAPI`.

### frontend
**retires** head, backend (as `Backend`/`TauBackend`/`RemoteBackend`), head
label

A program that drives sessions and shows events: print/json, REPL, RPC, TUI,
serve.
- Input queues and "steer" behavior live here (D2).
- A frontend may give input under a named parent, or set `target` and commit,
  to branch or go back (Q15).
- Every frontend sees every cursor.

### driver
**Python** `tau_next.driver`

The asyncio loop that runs steps. It calls `next`, runs the hook points
around each effect, writes, then holds or commits. It also keeps the per-batch
claim, so tools run once (Q13). It is the only place that awaits.

### store
**Rust** `tau_store` · **retires** session store, `SessionCatalog`, JMFTS
session log (until Phase 7)

The only Rust code that touches the filesystem. It keeps an append-only node
log and move log per session, behind a trait.

### lease
**Rust** `tau_store` · **retires** `SessionWriteLock`, `WriteAccess`,
`LockHolder`, lock sidecar

One `flock` per session, taken on its node log. The holder may write; every
other process opens the session read-only. A lease whose holder process is
dead is reclaimed.

---

## Testing (Phase 2)

### Harness
**Python** `tests/behavior/harness.py` · **retires** session log contract
tests

The protocol every behavior test drives: create and load sessions, give input
(optionally under a named parent), step, toggle auto-step, abort, open
cursors, go to a node, snapshot the tree and cursors, and read events. It has
one adapter for `tau_next` (in `tests/`) and one for τ 0.15 (in `parity/`).

### FakeProvider, FakeTool
**Python** `tests/behavior/` · **retires** scripted fake model streams,
`unittest.mock` patches

These fakes sit at the effect boundary, and they are the only fakes allowed
(rule 4).
- `FakeProvider` replays scripted samples, including calls, errors, and
  stream deltas. It records each context it was sent (the I1 test).
- `FakeTool` returns scripted outcomes.

---

## Scaffold (deleted in Phase 3)

### probe
**Rust** `tau_core::scaffold::Probe` · **Python** `tau_next.Probe`,
`ProbeError`, `echo`

Phase 0's hello-world struct for testing the Rust/Python boundary. It is not
part of τ.

---

## Retired 0.15 terms

Banned terms (rule 3) are linted. The rest disappear because nothing here
needs them.

| 0.15 term | what replaces it |
|---|---|
| entry | node |
| side entry, `SIDE_KINDS` | scratch: a node not on a committed walk |
| incomplete / finalize | nothing: nodes are written once, complete |
| leaf, default leaf | `at`, persisted through move records; no guessing |
| head cursor, acting cursor, `TURN_CURSOR` | every cursor is explicit, and hooks receive theirs |
| turn frame, `TurnFrame` | the `Config` node on the walk |
| turn (four senses) | step (D1); frontends count steps since last input |
| submission, multitask strategy | input steps; queues and steering are a frontend's (D2) |
| suspension, responder, `api.suspend` | hold: auto-step off, the frontend resumes or discards |
| lane | nothing |
| head | frontend |
| backend | provider (model API) or frontend (driver of a session) |
| fork (in-session) | branch |
| compaction entry, splice | the compaction extension: a summary node plus a branch (D3) |
| extension lock, `request_user_action` | a `pre_input` hook that refuses input until satisfied |
| `input` hook | `pre_input` |
| `before_agent_start` hook | `pre_sample` (appends what it adds, I1) |
| `tool_call` hook | `pre_tool` |
| `tool_result` hook | `post_tool` |
| `turn_end`, `user_turn_end` hooks | `idle` (Q20) |
| `annotate`, `entry_written` hooks | event observers (`node_appended`) |
| session write lock | lease |
| `AgentSession`, runtime, catalog, `SessionManager` | session |
| `ExtensionContext` / `ctx` | the `api` and `cursor` hook arguments |
| handler policy (gating/advisory/human) | nothing in core (Q9) |
