# D1: No "turn"; the step, and `max_steps`

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
- **`max_steps`** in `Config` bounds committing steps taken without a
  frontend acting. The driver counts in memory; only frontend actions
  (input, step, auto-step on) reset it. A cursor a hook or tool opens
  inherits its opener's count. At the limit, auto-step turns off.
- `next` stays a pure function of the walk; `max_steps` never changes it.
- Nothing is stored: every cursor loads with auto-step off, so a runaway loop
  cannot survive a reload.

## Alternatives

| option | why not |
|---|---|
| "Turn" as the steps from input to `Wait` | `Wait` is not an end; a `post_reply` hook may continue. It needs a stored marker. |
| A count computed from the walk | The walk cannot tell a delivered input from a hook's "continue" node. |
| `max_steps` makes `next` return `Wait` | `next` would depend on more than the walk. |
| A persisted counter | State outside the records (I7), and useless: auto-step loads off. |

## Consequences

- One word, `step`; "cycle" is retired.
- Two cursors at one node agree on `next`; their counts may differ.
- Not yet spiked. Enforced by a Phase 2 scenario: a "continue" hook stops at
  `max_steps`; a frontend action resets the count.
