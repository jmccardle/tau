# D6: A cursor is `{id, at}`; every write is followed by its move

Status: accepted, revised in review (Phase 1, 2026-10-26; seventh revision 2026-10-27)

## Context

The plan gave a cursor two positions: `at` (committed) and `target` (the
draft tip). A step wrote nodes toward `target`, hooks could retarget it, and
commit validated and set `at := target`. The question D6 asked was whether to
persist `target`.

That design needed a draft pointer, a commit, and a rule for what a crash
mid-draft left behind. 0.15's equivalent was a nest of in-memory state
objects and guessed reopen positions.

## Decision

- A cursor is `{id, at}`, plus the **labels** that name it. There is no
  `target`, draft pointer, hold, or discard.
- **Every write is followed by its move** (model.md §5, §6).
  - Each phase commits its draft under `at` and moves onto it in the same
    record (`by: core`). A hook's `cursor.write` does the same.
  - A move checks only that `to` is a node (I3). It does not look below
    `to`: there is no fast-forward, and going to an `Assistant` node with
    calls and stepping runs them again, which is how a batch is re-run. There
    is no `Session.rerun`.
  - A cursor hook may move its own cursor at any of its hook points; tool
    hooks cannot move it. A frontend's move of a busy cursor aborts the step
    first: the draft is committed, its hooks run, and then the move applies
    (model.md §5, §8).
- `at` is the `to` of the cursor's latest move, so the only persisted
  position is the move records (I7). Nothing guesses a reopen point.
- Hooks read the cursor's history from its move records:
  `cursor.moves()`, and `cursor.moves(since_step=True)` for the moves since
  this step's `pre_step` began (Q44). There is no `prev` field.
- **Labels** map unique strings to cursor ids. A cursor may have any number;
  a label names one cursor at a time. `Label` records set, move, and remove
  them, and load replays them (Q33).
- To reject a reply, go to its parent and step again. The old reply stays in
  the tree as a sibling.
- Auto-step and busy are not persisted: every cursor loads at rest with
  auto-step off.
- A cursor carries no queue. Input for a busy cursor waits in its
  scratchpad (D2).

## Alternatives

| option | why not |
|---|---|
| Do not persist `target` (plan default): a crash leaves the draft as scratch nodes | Needs a draft pointer and a commit step to exist at all. Review removed both, so the crash case cannot arise. |
| Persist `target` | Two persisted positions per cursor, and a rule for resuming from a draft. |
| Hooks retarget a draft | Replaced by pre hooks that edit the draft and cursor hooks that move the cursor before the commit (model.md §6, §7). |
| An immutable label on the opening move (sixth revision) | A cursor's name could never change, and it had only one. |
| A `prev` pointer or `cursor.last_move` | New state beside the move records, which already say where the cursor was and who moved it. |

## Consequences

- One position per cursor, one record per move, so I7 is a plain replay.
- A crash after a write but before its move cannot leave a half-state:
  node and move are one record (model.md §11, D12).
- Cost: a reply cannot be inspected and then discarded before it is
  committed, except by a `pre_reply` hook that halts. Otherwise it is
  written, and the person goes elsewhere.
- Cost: going to a batch's `Assistant` node and stepping runs its calls
  again. A frontend that does not want that goes to the `Results` node.
- Enforced by: I3 and I7 proptests (every `cursor.at` is a node;
  `state == load(save(state))`, labels included) and I5's unit tests.

## History

Plan default: no `target` persisted; crash mid-draft leaves the draft as
scratch. Review (fourth revision) removed `target` entirely; Q28 (move
records carry `by`) follows from it. The fifth revision let any hook move a
busy cursor, interrupting its step in place. The sixth revision limited
hooks to their own cursor, at boundaries, and made a frontend's move of a
busy cursor an abort then the move. The seventh revision dropped
"committed", boundaries, fast-forward, and `Session.rerun`, and made labels
mutable records.
