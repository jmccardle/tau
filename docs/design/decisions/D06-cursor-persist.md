# D6: A cursor is `{id, at}`; every write is followed by its move

Status: accepted (Phase 1)

## Context

The original plan gave a cursor two positions, `at` and a draft `target`,
with a commit that moved one to the other, and asked whether to persist
`target`. That needed a draft pointer, a commit step, and a crash rule for a
half-written draft.

## Decision

- A cursor is `{id, at}`, plus labels. No `target`, draft pointer, hold, or
  discard.
- **Every write is followed by its move**, in one record
  (`Session::write`, `Record::Write { node, mv }`).
- A move checks only that `to` is a node (I3) and never looks below it. Going
  to a batch and stepping re-runs it; there is no fast-forward or
  `Session.rerun`.
- A cursor hook may move its own cursor; tool hooks cannot.
- `at` is the latest move's `to`. Hooks read history with `cursor.moves()`;
  there is no `prev` field.
- Labels map unique strings to cursors, as records (Q33).
- Auto-step and busy are not persisted.

## Alternatives

| option | why not |
|---|---|
| Keep `target`, unpersisted | Needs a draft pointer and a commit to exist at all. |
| Persist `target` | Two positions and a resume rule. |
| A `prev` pointer | State beside the move records. |
| An immutable label on the opening move | A name could never change. |

## Consequences

- One position, one record per move; I7 is a plain replay.
- Cost: a reply cannot be looked at and discarded before commit, except by a
  `pre_reply` halt.
- Spiked: `Session::write`, `Session::move_to`, and
  `pre_reply_move_chooses_where_the_reply_lands`. Labels and `cursor.moves`
  are not yet spiked.
