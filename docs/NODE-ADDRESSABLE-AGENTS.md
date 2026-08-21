# Spec: node-addressable agents — the tree is the context, the invoker is the agent

**Status:** shipped 2026-07-30 → 08-01. `agent_spec` is a real event type,
written by `_record_agent_spec()` (`agent_session.py:558`) as a
`customEntry`/`customType: "agent_spec"`, with a reload-invariance contract
test (`session_log_contract.py:634`). Rollback
(`multitask_strategy="rollback"`) navigates back to `_pre_turn_leaf`, bound
to `ctrl+z` in the TUI (`app.py:2160`, `RollbackPromptModal` at `app.py:612`).
Fork reuses `ctx.spawn_branch`/`BranchView`. The invariants below are the
record of what was verified before building on them, not an open design —
see `ROADMAP.md`'s "Node-addressable agents" entry for the compressed
shipped-state summary. Depends on nothing; `SUBMISSION-LIFECYCLE.md`'s
`fork` strategy depends on §2 and §5 of this document.

**Relationship to existing docs.** `SESSION-TREE-IMPLEMENTATION.md` records how the
tree was built. This document states what the built tree *guarantees*, and what
follows for running more than one agent over it. `JMFTS-INTEGRATION-PLAN.md` §7 owns
the conformance suite this document adds obligations to.

---

## The problem

τ can already point a second agent at a point in a conversation — `BranchView`
(`session_log.py:310`) plus `ctx.spawn_branch` (`extension_types.py:1134`) do exactly
that. What is missing is a written statement of *why that is safe*, and consequently
nobody can tell which further operations are safe. Concretely, three questions have no
documented answer:

1. If agent A is running at leaf `L`, can anything agent B appends change what A sees?
2. Does any of this depend on the JSONL store, or does it hold on JMFTS too?
3. What, exactly, is "an agent at a node"? Is it a pointer, or does it need a spec?

These are answerable from the code, and the answers are strong enough to build on.
They are also easy to get wrong later by accident, which is what a spec is for.

---

## The invariants

### I1 — a leaf's context is immutable once the leaf exists

`ConversationTree._active_path_entries` (`conversation_tree.py:196`) reads **only the
ancestor chain of the leaf**: `_walk` follows `parentId` upward, and the compaction
anchor is chosen from within that walk (`for idx, entry in enumerate(path)`). It never
consults siblings, load order outside the path, or the entry list as a whole.

No code path rewrites an entry: `parentId` is written once at append, and there is no
delete, no in-place mutation, and no file rewrite in `session_log.py`,
`conversation_tree.py`, or `session_store.py`. Appends therefore only ever create new
*leaves* — nothing can insert itself into an existing node's ancestor chain.

> **I1.** `context_for(L)` is a pure function of `L`'s ancestors. Since `L`'s ancestors
> are fixed at the moment `L` is appended, `context_for(L)` is fixed for all time,
> regardless of what is appended anywhere else in the tree.

This is not a convention agents must respect. It is a consequence of the fold's shape,
and it is why `BranchView` needed no context plumbing at all (`session_log.py:321-336`).

**Corollary — isolation is structural, not enforced.** A branch's entries are never
ancestors of the primary leaf, so nothing filters them and nothing *can* leak them.
`BranchView.entries()` deliberately returns the whole shared list and this still holds.

### I2 — model input is a tree path plus an ephemeral frame

`system_prompt` never becomes a node. It is inserted at request-build time
(`agent_loop.py:556-565`) from `config.system_prompt`. Model and tools are likewise
constructor arguments read per call, not entries on the path.

> **I2.** Model input per call = (the exact linear tree path to the cursor) + (a frame:
> system prompt, tool set, model, reasoning level). The path is node-owned, durable and
> immutable. The frame is invoker-owned, ephemeral and unpersisted. There is no third
> channel.

The frame's absence from the tree is deliberate and is **not** a gap to be closed. See
§5.

---

## Consequences

**Deletion is expressible as an append.** Three shapes, only one of which needs to copy
anything:

| shape | mechanism | copies? |
|---|---|---|
| drop a suffix | move the cursor and append — the old suffix becomes a sibling and falls off the `parentId` walk (`ctx.fork(mode="in_place")`; `append_branch_summary`'s re-parent, `session_log.py:262`) | no |
| drop a middle span | a splice anchor whose boundary names the resume point — what `compaction`'s `firstKeptId` already does | no |
| drop a middle span that a live branch points *into* | duplicate the retained subtree under a new parent | **yes** |

The third is the only case requiring duplication, and it is the expensive one — not in
bytes but in **referential integrity**. Copying mints new ids, and ids are load-bearing:
`tool_call_id` pairing, a branch's `cursor`, a compaction's `firstKeptId`, a JMFTS index
over subtree ids. Reach for it last.

**Prefer tree shape over per-node markers for exclusion.** Shape-based exclusion cannot
leak by omission: a new exporter, indexer or renderer that has never heard of branches
still cannot see them, because they are not ancestors of anything it walks. Marker-based
exclusion must be taught to every walker, and one that was not taught silently includes
what should have been excluded.

τ already has the scar. `LANE_KEY` is the system's one marker, and it had to be
retrofitted into `resolve_cursor` (`session_log.py:126-138` — or a crash resumes inside
a sub-agent's lane) and *separately* into `subtree_text` (`conversation_tree.py:326-341`
— or summarizing an abandoned branch vacuums up sub-agent transcripts). Two walkers, two
independent teachings, one of them a context leak. A third walker would need a third.

**The cost of never deleting.** Logs grow monotonically and nothing reclaims. `entries()`
deep-copies the whole log per call (`session_log.py:194`), and `BranchView._ids()`
invokes it on every navigate and compaction check. Accept it; do not paper over it.

**The one operation that is not an append.** True erasure — a credential in a tool
result — cannot be expressed as a marker. It requires a real rewrite. Name it as a
deliberate exception rather than pretending a flag covers it.

---

## Storage neutrality

Everything above lives **above** the `SessionLog` seam (`session_log.py:40`).
`ConversationTree` is pure and I/O-free over `(entries, cursor)` dicts and does not know
which store produced them. `BranchView` lives in `session_log.py` **once** rather than
per-store, for the reason given at `:94-101`. `resolve_cursor` is module-level because
*"the cursor is part of the entry algebra, not of any one durability layer."*

This is enforced, not merely intended: all three stores subclass the same conformance
suite — `test_contract_inmemory.py:9`, `test_contract_file_session.py:20/27`,
`tau-jmfts/tests/test_contract_jmfts.py:61` (the last implementing `reload()` against a
live server).

**Three things a store does not get for free:**

1. **`ctx.fork(mode="export")` is file-only** and raises otherwise
   (`extension_types.py:1341-1354`). Correct as-is — "copy the session to a new file"
   has no JMFTS meaning. Keep it Fail-Early; do not emulate it. Read alongside decision
   6 it is more than an escape hatch: exporting is the operation that turns a lane into
   a *process*, and it is file-only because processes are files.
2. **Load order is an obligation, not a property.** `resolve_cursor` takes the last
   primary entry and `tree()` keeps load order for roots. A database must *reproduce*
   insertion order. The JMFTS store sorts by doc id and cross-checks against its own
   `seq`, raising when they disagree (`store.py:400-405` — *"a second writer touched the
   tree"*). That check is the model to copy in any future store.
3. **One conversation has one writing process.** See decision 6 for the statement and
   for why the hazard is the leaf pointer rather than `_generate_entry_id`.

---

## The agent-at-a-node model

### What is node-addressable today

Context, completely. `BranchView(log, parent_id, …)` is a constructor taking a node id
and returning a runnable `SessionLog`; `AgentSession(session_log=branch, …)` runs on it
unchanged. With I1, "an agent is a pointer, and every history operation is *append nodes,
move the pointer*" is literally true.

### What is not, and deliberately stays that way

The frame. `AgentSession.__init__` takes fifteen parameters; exactly one
(`session_log`) is node-addressable. The rest — model, system prompt, tools, extensions,
api_key, reasoning, compaction settings, resolver, turn cap, execution mode — are
supplied by the invoker.

Some of it *is* written down (the file header carries model/system prompt/cwd,
`session.py:89-97`; there are `append_model_change` / `append_thinking_change` /
`append_session_info` entries, `session_store.py:418-424`) but nothing reads it back:
those three are deliberately excluded from the `SessionLog` Protocol
(`session_log.py:44-48`) and `context_for` emits nothing for them. They are a **record,
not a reconstruction**, and that is the correct status.

> **Position.** Faithful agent reconstruction from the tree is **not a goal**. A tool or
> extension that is absent makes an agent materially different, and guaranteeing its
> presence is the invoking environment's responsibility, not the log's. The tree owns
> what was said; the invoker owns who speaks next.

### The filesystem is frame, not path

`cwd` and everything under it is shared mutable state that no tree operation forks. That
is a statement about filesystem modify semantics, not about agent semantics, and the
harness does not sugar-coat it or paper over it.

It is also the position above applied to a second resource. The invoker arranges the
filesystem the way it arranges tools: read-only forks, a git worktree per agent, a
designated output path per instance, or nothing at all because the forks only read. The
harness's job is to make that arrangement **expressible** and **recorded**, not to
enforce it:

- **Expressible** — already true. `spawn_branch` takes a required, non-defaulted tool
  allowlist (`extension_types.py:1159-1163`), so a read-only fork is `tools=["read",
  "grep", "glob"]` today. Its docstring already states the reality plainly: *"Sub-agents
  share the process and cwd."* That is the correct shape — name the condition, refuse to
  default around it.
- **Recorded** — W2. The `agent_spec` node carries `cwd` alongside the rest of the frame,
  so a transcript says which directory a span of turns ran against.

What the harness must **not** do is imply isolation it does not provide. No `isolated=`
flag, no automatic per-branch temp directory, no copy-on-write emulation. A fork whose
tool allowlist includes `write` and `bash` shares one working directory with its parent,
and that is the operating system's answer, correctly surfaced.

### What this buys

"Swap execution to a different agent at a point in context" is not a feature to build.
It is what falls out of I1 + I2. Three recipes, all available today:

```python
# 1. Stop-and-resume on the same line with a new spec.
#    Picks up at the log's cursor; nothing else is required.
reviewer = create_agent_session(
    session_log=log, model="…", system_prompt=REVIEW_PROMPT, tools=["read", "grep"]
)

# 2. Fork at an arbitrary node with a different spec.
branch = open_branch(log, node_id, label="adversarial reviewer")
critic = AgentSession(session_log=branch, model=other, system_prompt=CRITIC, tools=scoped)

# 3. The packaged form — already varies model, tools and turn cap per branch.
await ctx.spawn_branch(parent_id, prompt, tools=[...], model=..., max_turns=...)
```

---

## Work items

Each is small and independently landable. Sizes are estimates against the cited sites.

**W1 — `spawn_branch` accepts a system prompt.** It hardcodes
`system_prompt=session._system_prompt` (`extension_types.py:1196`) and offers no
override, which is the one concrete blocker on "fork with a different spec." One
parameter. *Trivial.*

**W2 — `agent_spec` provenance node.** Because the frame is invoker-owned and
unpersisted, a transcript read back cannot tell which spec produced which turns. Turns
1–5 from a read-only reviewer and 6–10 from a full-tool builder are indistinguishable.
This is a legibility loss, not a correctness one — but it is the loss you feel the first
time you debug why the agent did not run the tests.

Write a **non-authoritative** `agent_spec` `customEntry` at each construction/swap —
model, system prompt digest, tool names, extension names, `cwd`.
`append_custom_entry` already provides a durable node kind that `ConversationTree`
excludes from the fold *by kind* (`conversation_tree.py:178-181`), so it never reaches
the model and never affects context. It records what was invoked; it does not promise
reinvocation. *Small.* **Must not** grow a reader that reconstructs from it — that would
contradict the §5 position and turn a record into a contract.

**W3 — generalize the splice anchor into a summary-less `elide` node.** The compaction
anchor is already a node on the path meaning "the fold skips from here to `firstKeptId`",
and every path-folding walker already traverses it. Generalizing it gives arbitrary
structured exclusion with nothing erased, no per-node flags, no new walkers to teach, and
no effect on branches whose paths do not contain the anchor. ~20 lines in
`_active_path_entries` plus an entry kind, plus contract tests. *Small.*

**W4 — extension inheritance across a spec swap.** Today a swapped-in or forked session
gets no extensions (`spawn_branch` passes none; `load_extensions` is async and
per-session). Consistent with §5, but it must be *stated* rather than discovered. Decide
and document: forks start extension-free, and the invoker loads what it wants.
*Documentation + one docstring.* Note that inheriting instead would make the hook runner
re-entrant across two concurrent turns, which is a materially larger change.

**W5 — custom tools through the SDK.** `create_agent_session(tools=[...])` resolves
built-in names only and raises on anything else (`sdk.py:191`). Custom `AgentTool`
objects must go through the `AgentSession` constructor or arrive via extensions.
Document; do not add a fallback. *Documentation.*

**W6 — write the single-writer precondition down.** Decision 6 belongs on the
`SessionLog` Protocol docstring (`session_log.py:40`), in its positive form: concurrency
*inside* a conversation is lanes, concurrency *across* processes is
`fork(mode="export")` to a separate conversation. No guard, no stat check, no id change.
*Documentation.*

---

## Test obligations

This repo's idiom is that a contract is executable (`testing/session_log_contract.py`
exists precisely because `runtime_checkable` checks method *names* only). These
invariants should be tests, not paragraphs.

**T1 — I1 as a conformance test.** Add to `SessionLogContractTests`: capture
`context_for(L)`; append an unrelated subtree elsewhere (a lane, a sibling, a compaction
on another path); assert `context_for(L)` is byte-identical. Every store must pass. This
is the single most valuable test in this document — it is the property every concurrent
reader depends on, and no store currently proves it.

**T2 — no-mutation property.** Assert that after any append, every pre-existing entry
dict is unchanged (id, `parentId`, payload). Cheap, and it pins the premise T1 rests on.

**T3 — `elide` fold cases** (with W3): anchor with no summary; anchor whose boundary is
the root; a branch rooted inside an elided span is unaffected.

**T4 — reload-invariance of `agent_spec`** (with W2): the node survives `reload()` and
still contributes nothing to `context_for`.

**T5 — `entries()` is total** (decision 7). After every exclusion operation the system
has — open a lane, re-parent via `append_branch_summary`, compact, elide — assert that
every id ever returned by an append still appears in `entries()`. This is the audit
guarantee stated as a test, and it is what makes W3 safe to land: `elide` may hide a span
from *a fold*, never from the log.

---

## Decisions taken

1. **Reconstruction is not a goal.** §5. The invoking environment owns capability.
2. **Exclusion is encoded in tree shape, not per-node flags.** §3, with `LANE_KEY`'s
   retrofit history as the evidence.
3. **`agent_spec` is a record, never a contract.** W2.
4. **Forks start extension-free.** W4.
5. **`fork(mode="export")` stays file-only and raises elsewhere.** §4.

6. **A conversation has exactly one writing process.** Concurrency *inside* a
   conversation is lanes (`BranchView`); concurrency *across* processes is
   `fork(mode="export")` to a separate conversation. Documented as a precondition (W6),
   not guarded.

   The hazard is **not** id collision, and an earlier draft of this document said
   otherwise. `_generate_entry_id` retries against the log's own id set
   (`session_store.py:129`), so a same-process collision merely redraws; across two
   processes the collision window is only the entries the other writer added since the
   last load, which is negligible. The real hazard is that `_leaf_id` is process-local
   memory nothing re-reads (`session_store.py:557-560`), so two writers both parent off
   the same node and the conversation silently becomes a fork instead of a line —
   `resolve_cursor` then picks one writer's turns on reload and orphans the other's. The
   JMFTS store already refuses to load a tree showing evidence of this (`store.py:400-405`);
   the file store does not, and will not.

   This costs nothing that matters, because the concurrency the rest of this document
   depends on is lane-shaped, and `Session.fork` (`session_store.py:365`) already ships
   the process-shaped alternative — a verbatim entry copy into a new file with
   `header.parent = source.id`, "self-contained — no cross-file chaining." Two accepted
   consequences: ids are copied verbatim, so an entry id is unique within a file but is
   not a global handle; and export pays a full copy per process where a lane pays zero.
   Right price at process scale, wrong price at turn scale — which is the argument for
   keeping `fork` in-process, not against export.

7. **`entries()` is total, and that is a guarantee.** Every filtering mechanism —
   `context_for`, `subtree_text`, lanes, compaction, and `elide` — is a *fold over*
   `entries()`, and no fold may be the only way to reach an entry. T5. Stating it is what
   licenses W3: `elide` earns the right to hide a span from a view precisely because
   there is a view it cannot hide from.

8. **Growth is deferred behind a measurement, not a change.** Nothing reclaims and
   `entries()` deep-copies per call. The dominant cost is identified — `BranchView._ids()`
   deep-copies the whole log to compute a set of ids, on every navigate and compaction
   check — and the fix is a Protocol widening (an `ids()` accessor across three stores
   plus the contract suite) that we are not making on speculation. The threshold is
   store-dependent: JMFTS's `entries()` is a round-trip, a different curve entirely.
   Measure before touching this.
