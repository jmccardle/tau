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
- **Runaway loops are an extension's** (leave it to an extension). Core keeps
  no count and has no `max_steps`. `StepLimit(n)` (`tau_next.StepLimit`) is a
  `post_step` hook that turns auto-step off after `n` steps that moved the
  cursor, counted since a new input item reached it. It restarts when it stops
  the cursor, so whoever resumes gets another `n`. It sees only what a hook can
  see, so a frontend's `set_auto`, `step`, or go-to does not restart the count
  by itself; a new input does.
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
- Enforced by `tests/behavior/test_step_limit.py`: a "continue" hook stops at
  the limit; a new input or a resume after the stop gives another.
