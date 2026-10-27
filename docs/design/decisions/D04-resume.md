# D4: Resume at calls without results

Status: accepted (Phase 1, 2026-10-26)

## Context

A process dies while a batch runs. On reload, some calls have results and some
do not. The tool for an unanswered call may have run, or even finished. Every
provider rejects a conversation with an unanswered call (I4, Why), so each one
must be answered before the next sample.

0.15 guessed what to do. Here a reload is a replay of nodes and moves, with
no heuristics (I7).

## Decision

On resume (model.md §8), for each call still unanswered on the cursor's walk:

| the call's tool | what resume does |
|---|---|
| `idempotent` | run it again like any call: `pre_tool`, the tool, `pre_result`, `post_result` |
| anything else | answer it `Interrupted`, a core-written result: no `pre_result`, but `post_result` runs |

- `idempotent` is a flag on `Tool` (glossary): running twice has the same
  effect as once.
- Resume happens when a cursor at the end of a partial run is next stepped.
  The step first takes the batch's claim (waiting if another step holds it)
  and re-reads the run, fast-forwarding as its own move, so calls that
  already have results are not run again (I3, model.md §8, §10). A move
  never takes a claim.
- Each result is written and moved onto as usual, so the batch completes.
- The same rule serves input at a partial batch after a reload: the base
  extension holds the input pending and steps the cursor, and resume
  answers the batch first (Q17).
- Interrupting a resume (an abort, or a move of the busy cursor) answers its
  remaining calls `Interrupted`, like any interrupted tools step (model.md
  §8). The resume's fast-forward is the step's own move, so it is
  compare-and-set: a move made while the step waited for the claim wins.
- A call whose tool finished but whose result was not yet written is
  `Interrupted`, unless idempotent. The raw outcome is never written
  unredacted (model.md §8, abort during tools).
- Resume never re-runs on purpose. A deliberate second run is
  `Session.rerun`, a separate operation.

## Alternatives

| option | why not |
|---|---|
| Always re-run (plan option 2) | Repeats side effects: a second `bash` deploy, a duplicate write. |
| Write one `Interrupted` for the whole batch | The plan's old shape. A ten-minute call that finished would be marked `Interrupted` because a sibling was still running. Per-call results (D7) avoid it. |
| Guess from output or timing | A heuristic; I7 forbids it. |

## Consequences

- A finished call survives a crash: its result is already on the line.
- The model sees `Interrupted` and can decide to retry.
- Cost: a tool author must set `idempotent` correctly. The default is not
  idempotent, so a missing flag fails safe.
- Enforced by: behavior scenario "crash after calls committed (resume writes
  `Interrupted`)" and the batch scenarios under I4. Proptest I7 covers
  partial batches. The idempotent re-run path has no scenario named in
  model.md (gap).
