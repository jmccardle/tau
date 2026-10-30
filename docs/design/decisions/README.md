# Decisions

One ADR per design decision. Where an ADR and `model.md` differ, the ADR
wins; where either differs from the step spike, the spike wins until review.

| id | title | status | decision |
|---|---|---|---|
| [D1](D01-turn.md) | No "turn" | accepted | The unit is the step; `StepLimit` (an extension) bounds unattended steps. |
| [D2](D02-busy-input.md) | Input | accepted | Input is a scratchpad item; the input phase delivers it whenever pending. |
| [D3](D03-compaction.md) | Compaction | accepted | An extension: a new line plus a `pre_sample` move. |
| [D4](D04-resume.md) | Crash mid-batch | accepted | No recovery in core: rest at the batch; input goes under it; a step re-runs it. |
| [D5](D05-config-branch.md) | Config on a branch | accepted | The nearest `Config` on the walk. |
| [D6](D06-cursor-persist.md) | Cursor persistence | accepted | `{id, at}`; every write is followed by its move. |
| [D7](D07-call-result.md) | Calls and results | accepted | Calls in the `Assistant` node; one `Results` per batch at the join. |
| [D8](D08-extension-compat.md) | 0.15 extensions | accepted | No compatibility; a mapping table. |
| [D9](D09-session-compat.md) | 0.15 sessions | superseded | No converter: τ 0.16 sessions live in `$TAU_HOME/sessions` beside 0.15's files, which it ignores. |
| [D10](D10-package-name.md) | Package name | accepted | `tau_next` until absorption into τ 0.16.0. |
| [D11](D11-core-decides.md) | Where rules live | accepted, spiked | A sans-IO step machine in `tau-core`; the driver performs jobs. |
| [D12](D12-store-layout.md) | Store layout | accepted | One record log plus a scratchpad file; JMFTS a replica. |
| [D13](D13-config.md) | Configuration | accepted | One owned file plus trusted project files; a session names its model (`group:name`), never copies it. |
| [D14](D14-residency.md) | Loading a subset of a long history | deferred | Every body stays in memory; a later version must not widen the core/driver protocol or evict a line being built. |
| [D15](D15-providers-and-events.md) | Providers and events | candidate | A provider is a long-lived object with shared settings plus its own `options`; parts carry tagged replay data; frontends get saved events (look at the tree) and live events (never saved). First slice built: contract, settings, events, mock provider. |
