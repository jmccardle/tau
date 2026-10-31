# D4: No crash recovery for calls in core

Status: accepted (Phase 1, reversed after the step spike)

## Context

A process dies while a batch runs. Some calls finished; an unfinished one may
have run. A reload is a replay with no heuristics (I7). Keeping finished
outcomes needs a journal, a started marker per call, and a flag every tool
author must get right: plumbing for a power cut that a person can explain in
one message.

## Decision

- A batch's outcomes are held in the step until the join, which writes one
  `Results` node. Nothing about a batch in flight is stored.
- After a crash the cursor loads at the batch's `Assistant` node, at rest,
  auto-step off (`Session::reconcile`). Every outcome is lost.
- **Input goes under the unanswered batch.** The provider renders its calls
  as text (model.md §10 rule 6), so the model sees them as interrupted.
- **A step without input re-runs the whole batch.**
- Keeping outcomes is an extension: `pre_result` saves each as an item,
  `pre_tool` answers from a saved item, `post_result` removes them.
- Abort is unchanged: unfinished calls are `Interrupted` at the join.

## Alternatives

| option | why not |
|---|---|
| A journal of outcomes; re-run only `idempotent` tools | A started marker per call, a flag per tool, a scratchpad rewrite per call. |
| Write `Interrupted` for the batch on load | A write on load is repair (I7), and it decides for the person. |
| Guess from output or timing | A heuristic. |

## Consequences

- The tools phase writes once, at the join.
- A finished ten-minute call is lost in a crash unless an extension keeps it.
- No `idempotent` flag on `Tool`.
- Spiked: `after_a_crash_mid_batch_input_goes_under_the_unanswered_batch`,
  `after_a_crash_mid_batch_a_step_reruns_the_whole_batch`.
