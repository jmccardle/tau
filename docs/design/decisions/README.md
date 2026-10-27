# Decisions

One ADR per decision in `docs/plan.md` §4 (D1–D10), plus the decisions review
added (D11, D12). Where an ADR and the plan differ, the ADR wins (the Phase 1
design docs supersede the plan on sign-off).

| id | title | status | decision |
|---|---|---|---|
| [D1](D01-turn.md) | No "turn" | accepted, revised in review | Drop `turn`; the unit is the step, one pass of the phase loop from `pre_step` to `post_step`. `max_steps` in `Config` bounds committing steps taken without a frontend acting; reaching it turns auto-step off. |
| [D2](D02-busy-input.md) | Input to a busy cursor | accepted, revised in review four times | Input is the one item kind core knows. It waits in the cursor's scratchpad, and the next step's input phase commits a `User` node that `delivers` it, after any running batch. |
| [D3](D03-compaction.md) | Compaction | accepted | An extension. `walk` stays a plain chain; compaction builds a line and `pre_sample` moves the cursor onto it. |
| [D4](D04-resume.md) | Resume at a batch with no `Results` | accepted, updated in review | Finished calls are journal items; the next tools phase reuses them, runs idempotent calls again, and answers the rest `Interrupted`. |
| [D5](D05-config-branch.md) | Config on a branch | accepted, revised in review twice | A cursor's config is the nearest `Config` on its walk, as full snapshots. "Config follows the cursor" (I6) is retired. |
| [D6](D06-cursor-persist.md) | Cursor persistence | accepted, revised in review | A cursor is `{id, at}` plus labels; every write is followed by its move. No `target`, no fast-forward, no `rerun`. |
| [D7](D07-call-result.md) | Tool call and result | accepted, revised in review twice | Calls stay in the `Assistant` node; one `Results` node per batch at the join (I4 by construction); the journal keeps finished calls across a crash. |
| [D8](D08-extension-compat.md) | 0.15 extension compatibility | accepted | None. The glossary's hook mapping replaces a shim. |
| [D9](D09-session-compat.md) | 0.15 sessions | accepted | One-way converter in Phase 7. Details open. |
| [D10](D10-package-name.md) | Package name | accepted, revised in review | `tau_next` (`ffwf-tau-next`) in development; ships as τ 0.16.0, final names set at absorption. |
| [D11](D11-core-decides.md) | Where the rules live | proposed | Each cursor's step is a sans-IO state machine in `tau-core`, the phase loop; the Python driver performs its jobs and feeds back results. |
| [D12](D12-store-layout.md) | Store layout | proposed | One record log (nodes with at most one move, or a label change, per record) plus an atomically rewritten scratchpad file holding input and journal items; load drops delivered items. |
