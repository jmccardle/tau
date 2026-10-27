# D2: Input to a busy cursor; queues belong to frontends

Status: accepted, revised in review (Phase 1, 2026-10-26)

## Context

A person types while a cursor is sampling or running tools. 0.15 answered
with submissions and a multitask strategy: queues, steering, and per-turn
state held in objects, which a reload had to guess back (glossary,
"Retired 0.15 terms").

Here a cursor is `{id, at}` and nothing else (D6). It has no place to keep a
queue, and a queue would be state that is neither a node nor a move (I7).

## Decision

- **Core offers no queue.** A cursor carries no queues; they belong to
  frontends (glossary, cursor and frontend). "Steer" behavior is a frontend's.
- Input lands only where `next != RunTools` (model.md §7).
  - If a step is in progress, the driver keeps **one** input until the cursor
    reaches such a node, then starts an `input` step. A second input while
    one is kept is refused: anything longer than one slot is a queue, and the
    queue is the frontend's.
  - If `at` is an unstarted or partial batch (a paused cursor, or after a
    reload), the driver answers the batch first, then takes the input (Q17).
    Calls it knows never started get `Skipped("superseded by input")`. Calls
    that might have started follow resume: idempotent calls run again, the
    rest are `Interrupted` (D4).
- To work in parallel with a busy cursor, open another cursor with
  `open_cursor` (glossary). Input may also be given under a named parent,
  which runs an input step there (Q15).
- A frontend that wants to cancel work first calls abort (model.md §8).

## Alternatives

| option | why not |
|---|---|
| Core `append` that injects at commit (plan default) | There is no commit. `append` writes a node and never moves a cursor (glossary), and a node under an unstarted or partial batch is refused by I4 anyway. |
| A core-side input queue per cursor | State outside nodes and moves; a reload could not restore it (I7). Each frontend wants different steering anyway. |
| Refuse all input at a busy cursor | An error the person did not need (Q17). Core keeps one input; only a second is refused. |

## Consequences

- Core stays small: the only rules are "input waits for `next != RunTools`"
  and "one input waits; a second is refused".
- Every frontend sees every cursor (glossary, frontend), so a second cursor
  is visible on every wire.
- Cost: each frontend implements its own queue and steering, and must handle
  the refusal of a second input. A buffered input is lost if the frontend
  dies; that is a frontend property, not the session's. The one kept input is
  driver memory and is lost on a crash too.
- Enforced by: behavior scenarios "two cursors at one batch" and the I4
  list in model.md §4. The driver tests for Q17 (input at a partial batch)
  are named in Q17 but not in any invariant's "Enforced by" list.

## History

Plan default: core offers `append` (inject at commit) and `open_cursor`;
queueing is a frontend buffer. Review removed commit (D6), so "inject at
commit" has no meaning. What remains is `open_cursor`, the driver's rule that
input waits for `next != RunTools`, and Q17 for input at a batch.
