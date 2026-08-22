# Removing branch lanes

Status: **built, 2026-08-21.** §1–§5 as designed; §6's three open questions are
answered in §8 with what the code actually did.

Revised the same day it was written. The first revision designed a *durable
cursor* to replace the lane tag; §7 records why that was dropped, because the
reason is itself the decision.

## 1. What a lane is today

`LANE_KEY = "branchOf"` (`session_log.py:150`) is stamped on every entry a
branch sub-agent writes. It is created by one call — `open_branch()` — whose only
production caller is `ctx.spawn_branch` (`extension_types.py:1374`). Nothing
else makes one: not `ctx.fork` in either mode, not the TUI's branching, not a
rollback.

So τ's operative definition of "sub-agent" is **"started by an extension calling
`spawn_branch`"**. There is no semantic definition and no structural one — a
sub-agent branch and a user's alternative branch are the same shape. Measured
from one fork point:

```
A) three-way fork        messages=4  lane-tagged=0
B) three sub-agents      messages=4  lane-tagged=3
```

Same tree, same entry count, opposite treatment.

## 2. The decision that shrinks this: crash consistency is not bought

The tag exists to repair one inference. `resolve_cursor` (`session_log.py:179`)
picks the leaf by log order — last entry wins — and a second concurrent writer
makes log order meaningless, so the tag filters that writer back out. Its
docstring states the failure it prevents: a sub-agent's append landing last
before a crash makes the next load resume inside the branch.

**τ is not going to pay for that guarantee.** τ is an interactive agent, not a
service that must survive `pkill` at an arbitrary instant with perfect
consistency. If a crash lands you on the leaf of `B` instead of where you meant
to be, the transcript is visible, the tree browser moves you, and nothing is
corrupted.

That is the requirement being dropped, deliberately and on the record.

### Why "last written" is usually the right answer anyway

The shape this design was argued from, which is the ordinary case rather than a
corner:

1. ~20 messages of research. The leaf is **P**.
2. **A**, **B**, **C** are children of P, running concurrently, ~20 messages
   each, one task apiece from the first half of the conversation.
3. Each branch is summarized: **A'**, **B'**, **C'**. P now has six children.
4. An extension writes a seventh child, **ABC'**, concatenating the three
   summaries. (Conversation nodes cannot have multiple parents; extensions can
   make a node that stands for several.)

`ABC'` is the last write **and** the intended continuation. "Last entry wins"
gets it right. The lane filter is neutral here at best.

This is one agent moving forwards, backwards and sideways through its own
history — not a main agent with helpers that must be gathered back to it. The
tag encodes the second model, and the second model is wrong.

## 3. Why the tag is the wrong tool regardless

### 3.1 It is not what isolates the model's context

`BranchView`'s own docstring (`session_log.py:379-385`):

> **Isolation is mutual, and it is structural rather than enforced.**
> `context_for` walks leaf→root, so a branch's entries are never *ancestors* of
> the primary leaf and cannot leak into the primary context — whatever their
> kind, and even though `entries()` returns them. **Nothing filters them out; the
> tree shape means they are never on the path.**

A leaf→root walk cannot wander: every node has exactly one `parentId`. That is
what keeps a branch out of another branch's context, and it survives the tag's
deletion untouched.

### 3.2 Three of its four consumers ask the wrong question

| Consumer | Uses the tag for | Correct? |
|---|---|---|
| `resolve_cursor` (`session_log.py:179`) | which entries may define the cursor | moot after §2 |
| `subtree_text` (`conversation_tree.py:656`) | containment when summarizing | no |
| `Session.messages` (`session_store.py:353`) | "messages of this conversation" | no |
| `SessionInfo` reader (`session_store.py:766`) | `message_count`, `first_message` → title | no |

The bottom three want *"does this entry belong to the conversation being looked
at?"*. The answer is **ancestry from the cursor**, not write provenance. For a
sub-agent the two coincide. For a fork they diverge, and the code silently gives
the wrong answer: `Session.messages` is a flat filter with no ancestry check at
all, so a three-way fork returns three mutually exclusive alternatives
concatenated as one conversation.

## 4. The change

1. **Delete the tag.** `session_log.py:150,179,351`; the `lane=` parameter
   through the Protocol and the three stores (`session_log.py:122,338,431`,
   `store.py:575`, `catalog.py:211`, `session_store.py:673`); the stamps at
   `session_log.py:351`, `session_store.py:694`, `store.py:590`.
2. **`resolve_cursor` returns to pi's rule** — last entry, no filter. This is
   parity restored, not a divergence introduced.
3. **Move the three consumers to ancestry** (`session_store.py:353,766`,
   `conversation_tree.py:656`). This is **not optional**: delete the tag without
   it and sub-agent turns start appearing in `messages`, inflating
   `message_count` and able to become a session's display title.
4. **`BranchView` stays**, intact. It is a second cursor over one log, it still
   owns its own leaf, and it simply stops stamping what it writes.

Fixing (3) also removes the fork/sub-agent asymmetry in §1, as a side effect
rather than as separate work.

## 5. What this does NOT touch

`lane` is two unrelated things in this codebase, and only one is the tag.

The other is a **live UI routing key**: `RenderRouter.on_branch_event` /
`_route` / `_close` (`backends.py:412,463,496,507`) and
`ChatDisplay.open_lane` (`app.py:265`), which render concurrent branch output in
separate visual lanes. Neither file imports `LANE_KEY`; they take a `lane`
string as an argument, which `BranchView.lane` supplies in memory.

That accounts for **165 of the ~215 test references to "lane"** —
`test_tui_multi_lane_render.py` (88) and `test_render_router.py` (77) — and none
of it moves. Display needs a per-branch identity at runtime; it never needed the
entry stamped.

Scale, then: roughly 20 source edits and ~50 test references, concentrated in
four files.

## 6. Open for the implementer

### 6.1 The contract suite is the real ripple

`tau_agent_core.testing.session_log_contract` (~25 lane references) is
**published API**. It exists so a downstream store implementor can prove
conformance. Removing its lane-discipline tests changes what a third-party store
is required to implement, which is a compatibility decision rather than a
cleanup, and it wants a release note.

**The owner has accepted that API-change cost** if nothing there can be
salvaged. Before taking it, work out what those tests were actually trying to
guarantee — they may be the clearest surviving statement of an intent worth
keeping under a different mechanism.

### 6.2 A hypothesis to test, not a conclusion

> Lane discipline is the opposite of context engineering.

Two readings, and the implementer should decide which holds:

- **An antipattern**, where leaf-to-shared-root ancestry is the right primitive
  and the tag was an inferior stand-in for it.
- **A real need pointing the wrong way** — something extensions want *laterality*
  to deliberately ignore, i.e. the ability to reach sideways across branches on
  purpose (which is exactly what `ABC'` in §2 does). If so, the mechanism should
  be an explicit lateral read, not an implicit containment nobody asked for.

The `ABC'` node is evidence for the second reading: an extension deliberately
building a node from three sibling branches is lateral movement, and a
containment rule that fights it is working against the user.

### 6.3 Measure, do not guess

`SessionInfo` reads entries without building a tree, because the picker reads
many sessions at once. Ancestry needs the parent chain. Whether that costs
anything at picker scale is the one question here that a reading cannot answer.

### 6.4 Do not re-derive §1–§5

Re-verify citations — this repo has watched line numbers drift within hours —
but the argument above was worked out against the code and does not need
relitigating.

## 7. Rejected: the durable cursor

The first revision of this document proposed recording the primary leaf durably
so `resolve_cursor` would never infer, then deleting the tag as unnecessary. For
the file stores that meant a new `cursor` entry kind written by the primary
writer whenever a branch was open; for JMFTS it was nearly free, since a
conversation root is already a mutable document carrying
`structured_content={"tau": header}` and already patched by `update_document`
(`store.py:332,517`).

It was dropped because it buys the §2 guarantee, and τ is not buying it. It also
kept "primary" as a structural privilege — one cursor that matters and others
that do not — which is the same wrong model as the tag, in a new mechanism.

Recorded rather than deleted for two reasons. If a future τ ever does need
crash-exact resume, this is the shape and JMFTS is the cheap case. And the
observation underneath it stands on its own: nothing today can resume at a
chosen leaf. `Session.__init__` (`session_store.py:291`) takes no leaf argument
and `resolve_ref` resolves a *session*, never an entry, so three sub-agents in
flight have three leaves and a load can reach exactly one of them. Making load
accept a leaf is additive, independent of everything above, and would let a
caller say which of §2's seven children it wants.

## 8. What was built

### 8.1 §6.3 — ancestry is affordable, measured

Benchmarked the streaming O(1)-memory scan in `read_session_info` against a
buffer-then-walk one, twice and independently. Real corpus
(`~/.tau/sessions`, 44 files, 524 entries): **3.50 ms → 3.88 ms, 1.11x**.
Synthetic 200 files × 500 entries (one fork every 50): **203 ms → 225 ms,
1.11x**. The prior measurement agreed: +0.94 ms / 1.11x real; 50×200 +2 ms;
200×500 1.14x; 500×1000 1.21x; 1000×2000 1.23x. A constant factor, not a
complexity change — both passes are dominated by `json.loads`, and the absolute
delta only matters at sizes where the *existing* scan already takes 265 ms+.
The picker reads in a `@work(thread=True)` worker. `SessionInfo` needed no
design change: `read_session_info` buffers, resolves the cursor with the shared
`resolve_cursor`, and walks it with `ConversationTree.path()`. `name` and
`modified` stay whole-file — a name is session-level metadata and any write did
genuinely modify the file.

### 8.2 §6.2 — both readings hold, split by consumer

Reading 1 (the tag was an inferior stand-in for ancestry) for `Session.messages`
and the `SessionInfo` reader: both now walk from the cursor.

Reading 2 (a real need pointing the wrong way) for `subtree_text`: it is now
bounded by **descendants of `from_id`** — a structural question about the
subtree the caller named — and reaches down but never sideways or up. That is
not the lane filter renamed: the old rule asked *who wrote this entry* and
refused to descend from a primary entry into a sub-agent branch hanging under
it, so it silently returned a different region than the one asked for. It also
stops fighting §2's `ABC'` case: an extension building a node out of three
sibling summaries has named what it wants.

### 8.3 §6.1 — the contract suite, salvaged rather than cut

Of `tau_agent_core.testing.session_log_contract`'s lane tests, exactly one was
about the tag and it asserted the guarantee §2 drops. It is **replaced, not
deleted**, by `test_the_cursor_is_the_last_entry_whoever_wrote_it`, which pins
the opposite rule and tells an implementor not to reintroduce a filter (two
stores disagreeing about which entry is the cursor is a divergence no fold can
repair). Its neighbour survives as
`test_branch_entries_never_reach_another_cursors_context` with a rewritten
rationale: the property is real and now has exactly one mechanism behind it —
the single-`parentId` leaf→root walk — so the test proves the structural
property instead of a filter. The `context_for(L)` invariance test needed no
change; it uses `open_branch`, which survives.

**Release note — SessionLog implementors (behaviour change).**

> `SessionLog.append_at()` loses its `lane=` keyword, and the `branchOf` entry
> marker is gone. A store must no longer write it, and must not filter on it.
>
> - `resolve_cursor` is pi's rule again: **the last entry wins, whoever wrote
>   it.** Filtering entries out of that decision is now a conformance failure.
>   What you lose is crash-exactness: if the process dies immediately after a
>   sub-agent's write, the next load resumes at that branch's leaf instead of
>   the primary one. τ does not buy that guarantee (§2) — the transcript is
>   visible and the tree browser moves you.
> - Branch isolation is unchanged and is now *entirely* structural. It rests on
>   your store reconstructing `parentId` exactly on reload; get that wrong and
>   you break context isolation itself, not merely an ordering.
> - Readers that asked "does this entry belong to this conversation?" must ask
>   ancestry from their own cursor. A `branchOf` filter never answered it for a
>   forked session anyway (§3.2).
> - Existing logs with `branchOf` on them still load. The key is ignored; the
>   tree shape it accompanied is what carried the meaning.
>
> Contract-suite delta: `test_a_lane_tagged_entry_landing_last_does_not_capture_the_primary_cursor`
> → `test_the_cursor_is_the_last_entry_whoever_wrote_it` (inverted assertion);
> `test_lane_entries_never_reach_the_primary_context` →
> `test_branch_entries_never_reach_another_cursors_context` (same property, no
> `lane=` argument).

### 8.4 The asymmetry, as a test

`test_a_three_way_fork_and_three_sub_agents_are_INDISTINGUISHABLE`
(`tau-agent-core/tests/test_branch_view.py`) builds §1's two logs and asserts
the message trees are equal modulo generated ids, then that `resolve_cursor`,
`context_for` and `subtree_text` all give identical answers for both.

The `Session.messages` bug §3.2 predicted was real and is reproduced before
being fixed: a three-way fork returned
`["shared question", "ALTERNATIVE ONE", "ALTERNATIVE TWO", "ALTERNATIVE THREE"]`
as one conversation, and the picker reported `message_count == 4` for a
two-message conversation.
