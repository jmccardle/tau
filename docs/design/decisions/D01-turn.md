# D1: No "turn"; steps, and `max_steps` as a pause

Status: accepted, revised in review (Phase 1, 2026-10-26; sixth revision 2026-10-27)

## Context

τ 0.15 used "turn" in four senses: loop turn, agent run, user turn, and
submission. Each had its own state and its own hooks (`turn_end`,
`user_turn_end`). A reload had to guess where a turn stood (glossary,
"Retired 0.15 terms").

Core needs one question answered: what does this cursor do next? That is
`next(walk)` (model.md §6), a pure function. It does not need to know what
a "turn" is. But something must stop a runaway loop, for example a hook that
keeps sending "continue" (Q20).

## Decision

- There is no turn in core. The unit is the **step** (model.md §7): one
  sample or one tools run for one cursor. Every step counts toward
  `max_steps`, including one a hook's move or re-run causes; an abort is not
  a step, and neither is `pre_step`.
- Frontends that want to show "turns" count steps since the last input. That
  is a display choice, not core state.
- Runaway loops are bounded by the `max_steps` field of `Config`: the most
  steps a cursor takes without the frontend acting on it (Q20). The driver
  keeps an in-memory counter per cursor, reset only by actions
  `by: frontend(client)`: giving input, stepping the cursor, re-running a batch,
  turning auto-step on. A cursor a hook or tool opens inherits its opener's
  count and auto-step setting; a cursor a frontend opens starts at 0 with
  auto-step off. When the
  count reaches `config_at(at).max_steps`, auto-step turns off and the
  cursor pauses.
- `next` stays a pure function of the walk; `max_steps` never changes it.
- Nothing is stored. Every cursor loads with auto-step off (I7), so a
  runaway loop cannot survive a reload, and a fresh count after a reload
  loses nothing.
- "Keep going" lives in a `post_reply` hook that writes a `User` node and
  moves onto it with `cursor.write` (model.md §7). That is the cursor acting on its own, not the
  frontend, so it does not reset the count.

## Alternatives

| option | why not |
|---|---|
| Keep "turn" as the steps from input to `Wait` (plan option 2) | `Wait` is not an end: a `post_reply` hook may continue past it. A turn would need either a stored marker or a second definition of where it ends. 0.15 shows where that leads. |
| A count computed from the walk (an earlier revision) | The walk cannot tell a delivered input's node from a hook's "continue" `User` node: both may be authored by an extension. The boundary would need a stored marker. |
| `max_steps` reached makes `next` return `Wait` | `next` would depend on more than the walk. Pausing the cursor says the same thing without that. |
| A persisted counter | State outside nodes and moves (I7), and pointless: auto-step loads off, so nothing runs away across a reload. |

## Consequences

- One word, one meaning: `step`. The banned-term table maps all four turn
  senses to it.
- `max_steps` needs no persistence: a reload turns auto-step off, which is
  what reaching `max_steps` would do.
- Two cursors at the same node always agree on `next`. Their counters may
  differ; that only decides when each pauses.
- Cost: a frontend's "turn" count may differ from another frontend's. Core
  does not care.
- Enforced by: model.md names no behavior scenario for `max_steps` yet (gap,
  see the Phase 2 list). The I7 proptest covers that the counter is not part
  of the stored state.

## History

Plan default: drop "turn"; frontends count steps since last input. Review
kept the drop and added a core-side bound, `max_steps` (Q20). It was first
computed from the walk; review found the walk could not mark where input
began, so it became an in-memory count that pauses the cursor. The plan had no runaway guard once "continue" became a hook's job.
