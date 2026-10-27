# Decisions

One ADR per decision in `docs/plan.md` §4. Where an ADR and the plan differ,
the ADR wins (the Phase 1 design docs supersede the plan on sign-off).

| id | title | status | decision |
|---|---|---|---|
| [D1](D01-turn.md) | No "turn" | accepted, revised in review | Drop `turn`; the unit is the step. `max_steps` in `Config` bounds steps taken without the frontend acting; reaching it turns auto-step off. |
| [D2](D02-busy-input.md) | Input to a busy cursor | accepted, revised in review twice | Input is hook-driven; core has no queue. The base extension holds busy input as pending-input nodes under the first root and delivers it from its post hooks. |
| [D3](D03-compaction.md) | Compaction | accepted | An extension. `walk` stays a plain chain; compaction builds a line and a `pre_sample` tip. |
| [D4](D04-resume.md) | Resume at calls without results | accepted | Idempotent calls run again; every other unanswered call is `Interrupted`. |
| [D5](D05-config-branch.md) | Config on a branch | accepted, revised in review | Config follows the cursor (I6), as full snapshots; a restatement at a batch is deferred to its completing result. |
| [D6](D06-cursor-persist.md) | Cursor persistence | accepted, revised in review | A cursor is `{id, at}`; every write is followed by its move. No `target`. |
| [D7](D07-call-result.md) | Tool call and result | accepted, revised in review | Calls stay in the `Assistant` node; one `Result` node per call, moved onto as written (I4). |
| [D8](D08-extension-compat.md) | 0.15 extension compatibility | accepted | None. The glossary's hook mapping replaces a shim. |
| [D9](D09-session-compat.md) | 0.15 sessions | accepted | One-way converter in Phase 7. Details open. |
| [D10](D10-package-name.md) | Package name | accepted, revised in review | `tau_next` (`ffwf-tau-next`) in development; ships as τ 0.16.0, final names set at absorption. |
