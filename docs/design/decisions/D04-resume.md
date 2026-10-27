# D4: Resume at a batch with no `Results`

Status: accepted (Phase 1, 2026-10-26; updated in the sixth and seventh
revisions, and reversed to "no recovery in core" after the step spike)

## Context

A process dies while a batch runs. On reload, some calls had finished and
some had not. The tool for an unfinished call may have run, or even
finished. 0.15 guessed what to do; here a reload is a replay of the stored
records, with no heuristics (I7).

The seventh revision kept each finished outcome as a scratchpad journal item
and re-ran only `idempotent` tools. Writing it showed that the rule also
needs a record of which calls had *started*, and every finished call costs a
scratchpad rewrite. That is plumbing for a power cut. A person can say "the
power went out, check the files" instead.

## Decision

- **Core keeps no crash recovery for calls.** A batch's outcomes are held in
  the step until the join, which writes one `Results` node. Nothing about a
  batch in flight is stored.
- After a crash, the cursor loads at the batch's `Assistant` node, at rest,
  with auto-step off (I7). Every outcome of that batch is lost.
- **Input goes under the unanswered batch.** The input phase applies
  whenever input is pending. The provider renders calls with no `Results`
  below them on the walk as text (model.md §10, rule 6), so the model sees
  them as interrupted and can check what happened.
- **A step without input re-runs the whole batch**, like any re-run (model.md
  §5). That is the person's choice.
- **Keeping finished outcomes is an extension**, with the hooks that already
  exist: `pre_result` saves each outcome as a scratchpad item, `pre_tool`
  answers a call from a saved item so the tool does not run, and
  `post_result` removes the items.
- Aborting the tools phase is unchanged: running and unstarted calls are
  `Interrupted` at the join (model.md §8).

## Alternatives

| option | why not |
|---|---|
| A journal of outcomes, re-running only `idempotent` tools (seventh revision) | Needs a started marker per call, a flag every tool author must get right, and a scratchpad rewrite per call. Core plumbing for a rare event that a person handles better. |
| Write `Interrupted` for the batch on load | A write on load is repair, which I7 forbids, and it decides for the person. |
| Guess from output or timing | A heuristic; I7 forbids it. |

## Consequences

- No per-call writes during a batch; the tools phase writes once, at the join.
- A finished ten-minute call is lost in a crash unless an extension keeps it.
- No `idempotent` flag on `Tool`.
- Enforced by: Phase 2 scenarios "crash mid-batch: rests at the batch; input
  goes under it; a step re-runs it", already spiked in
  `crates/tau-core/tests/step.rs`.
