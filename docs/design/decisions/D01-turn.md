# D1: No "turn"; the step, and `StepLimit`

Status: accepted (Phase 1)

## Context

τ 0.15 used "turn" in four senses (loop turn, agent run, user turn,
submission), each with its own state and hooks, and a reload had to guess
where a turn stood. Core only needs to know what a cursor does next, which is
`next(walk)`. Something must still stop a runaway loop, such as a hook that
keeps writing "continue" (Q20).

## Decision

- No turn in core. The unit is the **step**: one pass of the phase loop,
  `pre_step` to `post_step` (`step::Step`, model.md §6).
- Frontends that show "turns" count steps since the last input.
- **A cursor that does not move rests** (core, model.md §6): a step that ends
  `Done` with work left but no move record since it woke turns auto-step off.
  Nothing spins in place, so no extension is needed for that case.
- **Runaway loops where the cursor does move are an extension's** (leave it to
  an extension). Core keeps no count and has no `max_steps`. `StepLimit(n)`
  (`tau_next.StepLimit`) keeps its own per-cursor count: `pre_input` sets it to
  1, `pre_step` adds one (auto-step on), and `post_step` turns auto-step off at
  `n` and starts over, so whoever resumes gets another `n`.
- `next` stays a pure function of the walk; no limit changes it.
- Nothing is stored: every cursor loads with auto-step off, so a runaway loop
  cannot survive a reload.

## Alternatives

| option | why not |
|---|---|
| "Turn" as the steps from input to `Wait` | `Wait` is not an end; a `post_reply` hook may continue. It needs a stored marker. |
| A count computed from the walk | The walk cannot tell a delivered input from a hook's "continue" node. |
| `max_steps` makes `next` return `Wait` | `next` would depend on more than the walk. |
| `max_steps` in `Config`, counted by core | Core state (`Session::taken`) and a reset on every frontend call for an optional policy. An extension can stop a loop from what hooks see. |
| A persisted counter | State outside the records (I7), and useless: auto-step loads off. |

## Consequences

- One word, `step`; "cycle" is retired.
- Two cursors at one node agree on `next`; their counts may differ.
- Enforced by `tests/behavior/test_step_limit.py`: a "continue" hook and a
  model that calls tools forever stop at the limit; a new input or a resume
  after the stop gives another. A hook that leaves an input pending while it
  commits is offered the input again every step, so `pre_input` restarts the
  count each time and the limit does not bound it.
