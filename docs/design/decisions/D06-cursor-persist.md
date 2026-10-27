# D6: A cursor is `{id, at}`; every write is followed by its move

Status: accepted, revised in review (Phase 1, 2026-10-26)

## Context

The plan gave a cursor two positions: `at` (committed) and `target` (the
draft tip). A step wrote nodes toward `target`, hooks could retarget it, and
commit validated and set `at := target`. The question D6 asked was whether to
persist `target`.

That design needed a draft, a commit, and a rule for what a crash mid-draft
left behind. 0.15's equivalent was a nest of in-memory state objects and guessed
reopen positions.

## Decision

- A cursor is `{id, at}`. There is no `target`, draft, hold, or discard.
- **Every write is followed by its move** (model.md §5, I3).
  - Each step ends with one move onto what it wrote (`by: core`). A tools
    step moves onto each result as it is written.
  - Validation is at append (V1, V2) and at move (I3, I6) (I5), so nothing
    invalid is written.
  - A step's own move is never refused and never skipped. A move of a busy
    cursor from outside its step interrupts the step in place: its records
    are written and moved onto, and then the move applies (model.md §5, §8).
- Core never writes a node and then abandons it, except an unreached I6
  restatement. A node no move reaches is one of those, an extension's side
  node, a detached root's work, or a record of a failure (`sample_error`).
  Those are scratch.
- `at` is the `to` of the cursor's latest move, so the only persisted
  position is the move log (I7). Nothing guesses a reopen point.
- To reject a reply, go to its parent and step again. The old reply stays
  committed (Q2).
- Auto-step and busy (the step lock) are not persisted: every cursor loads
  at rest with auto-step off.
- A cursor carries no queue. Input for a busy cursor is held as nodes by the
  base extension (D2).

## Alternatives

| option | why not |
|---|---|
| Do not persist `target` (plan default): a crash leaves the draft as scratch | Needs a draft and a commit step to exist at all. Review removed both, so the crash case cannot arise. |
| Persist `target` | Two persisted positions per cursor, and a rule for resuming from a draft. |
| Hooks retarget a draft | Replaced by pre hooks that change what is written and hooks that move the cursor (model.md §7). |

## Consequences

- One position per cursor, one record per move, so I7 is a plain replay.
- A crash after a write but before its move cannot leave a half-state:
  node and move are durable together (model.md §10; the on-disk form is open
  until the Phase 3 store ADR).
- Cost: a reply cannot be inspected and then discarded before it is
  committed. It is written, then the person goes elsewhere. Rejected replies
  remain in the tree.
- Cost: the plan's scenario "hook drafts on scratch and retargets" is
  retired. Its replacements are "`pre_sample` builds the context it returns"
  and "a hook moves the cursor".
- Enforced by: I3 and I7 proptests (every `cursor.at` is committed and
  validates; `state == load(save(state))`) and I5's unit tests.

## History

Plan default: no `target` persisted; crash mid-draft leaves the draft as
scratch. Review (fourth revision) removed `target` entirely. The question
this settled is in the "Decided in review" list of model.md; Q2 (committed is
permanent) and Q28 (move records carry `by`) follow from it. The fifth
revision let any hook move a busy cursor, interrupting its step in place,
instead of rules about when a hook may move. Its first draft made the step's
own move compare-and-set, leaving an interrupted step's records as scratch;
review replaced that with in-place interrupts, so every write a step makes
is still followed by its move.
