# D4: Resume at a batch with no `Results`

Status: accepted (Phase 1, 2026-10-26; updated in the sixth and seventh revisions)

## Context

A process dies while a batch runs. On reload, some calls had finished and
some had not. The tool for an unfinished call may have run, or even
finished. Every provider rejects a conversation with an unanswered call (I4,
Why), so each one must be answered before the next sample.

0.15 guessed what to do. Here a reload is a replay of the stored records,
with no heuristics (I7).

## Decision

- **The journal.** As each call finishes, after its `pre_result` hooks, its
  outcome is added to the cursor's scratchpad as a **journal item**
  (`from: {cursor}`, `data: {batch, call, outcome}`). It is durable before
  the call counts as finished. The `Results` node written at the join
  delivers every journal item of the batch (model.md §2, §11).
- After a crash, the cursor loads at the batch's `Assistant` node. The next
  tools phase there (model.md §8), for each call of the batch:

| the call | what the tools phase does |
|---|---|
| has a journal item for this batch | uses its outcome; the call does not run again, and its `pre_result` already ran |
| its tool is `idempotent` | runs it again like any call: `pre_tool`, the tool, `pre_result` |
| anything else | answers it `Interrupted`, a core-written outcome with no `pre_result` |

- The join commits one `Results` node with every outcome and delivers the
  journal items; `post_result` runs once on it.
- `idempotent` is a flag on `Tool` (glossary): running twice has the same
  effect as once.
- Input at such a batch waits: the input phase applies only when
  `next != RunTools`, so a send answers the batch first and the next step
  delivers the input (Q17, Q40).
- Aborting the tools phase (esc, or a frontend's move of the busy cursor)
  answers its running and unstarted calls `Interrupted` at the join, like
  any aborted tools phase (model.md §8).
- A call whose tool finished but whose `pre_result` had not finished is
  `Interrupted`, unless idempotent. Its raw outcome was never journalled, so
  nothing unredacted reaches the tree or the scratchpad.
- A question a `pre_tool` hook was waiting on is lost in a crash; its call
  is treated like any unfinished call (Q22).
- A crash's journal items stay for that batch on that cursor. If the cursor
  goes elsewhere and later back to the batch, the next tools phase uses them
  (Q41); a frontend that wants a clean re-run removes them first.

## Alternatives

| option | why not |
|---|---|
| Always re-run (plan option 2) | Repeats side effects: a second `bash` deploy, a duplicate write. |
| Write one `Interrupted` for the whole batch, with nothing saved before the join | The plan's crash cost: a ten-minute call that finished would be lost because a sibling was still running. The journal keeps it. |
| One `Result` node per call, moved onto as each finishes (revisions one to six) | Kept finished calls, but needed runs, batch states, fast-forward, a claim, and per-line I4 checks. The journal keeps them with none of that (D7). |
| Guess from output or timing | A heuristic; I7 forbids it. |

## Consequences

- A finished call survives a crash: its outcome is a journal item.
- The model sees `Interrupted` and can decide to retry.
- Cost: one scratchpad rewrite per finished call (D12).
- Cost: a tool author must set `idempotent` correctly. The default is not
  idempotent, so a missing flag fails safe.
- Enforced by: Phase 2 scenarios "crash mid-batch keeps journal outcomes"
  (the finished call does not run again; the unfinished one is
  `Interrupted`, or runs again if idempotent) and the batch scenarios under
  I4. The I7 proptests cover journal items across a crash.
