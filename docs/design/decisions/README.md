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
| [D10](D10-package-name.md) | Package name | accepted | `tau` until absorption into τ 0.16.0. |
| [D11](D11-core-decides.md) | Where rules live | accepted, spiked | A sans-IO step machine in `tau-core`; the driver performs jobs. |
| [D12](D12-store-layout.md) | Store layout | accepted | One record log plus a scratchpad file; JMFTS a replica. |
| [D13](D13-config.md) | Configuration | accepted | One owned file plus trusted project files; a session names its model (`group:name`), never copies it. |
| [D14](D14-residency.md) | Loading a subset of a long history | deferred | Every body stays in memory; a later version must not widen the core/driver protocol or evict a line being built. |
| [D15](D15-providers-and-events.md) | Providers and events | accepted | A provider is a long-lived object with shared settings plus its own `options`; parts carry tagged replay data; frontends get saved events (look at the tree) and live events (never saved). Built: contract, settings, events, mock, OpenAI, Anthropic and Gemini providers, tool progress; layered review and live checks 2026-10-30 recorded in the ADR. |
| [D16](D16-clients-and-asking.md) | Clients and asking | draft | A frontend holds a `Client` (local or remote, one contract); small state goes whole to every client, growth as `Appended`; questions (extension asks) and forms (commands, extension config) share `@tool`'s schema; completion from fixed values and named formats, no callbacks. |
| [D17](D17-authored-input-and-closing.md) | Authored input; closing cursors | accepted, built | Input records its `Author`, so an extension's text is its own; a cursor closes with a `Close` record that keeps its id, last node and name, and `reopen` restarts it. |
| [D18](D18-session-catalog.md) | Session catalog | accepted | A session is a directory with a time-sortable id and an `info.json` (cwd, name, `forked_from`); listing scans; `--fork REF` copies one walk; one shared start for `tau`, `tau -p` and `tau serve`. |
| [D19](D19-extension-loading.md) | Extension loading | accepted | Five sources, one export rule (C3's); `load: {key: bool}` in the config; a failing source stops `tau`; `/ext:cmd` when two extensions share a command. |
| [D20](D20-context-assembly.md) | Context assembly | accepted | The system prompt is assembled once at session creation by a startup step (`tau.system_prompt`) and stored as the `System` node. |
| [D21](D21-the-wire.md) | The wire | accepted | `tau serve` serves one session per process, JSON-RPC over WebSocket on a Unix socket in `$TAU_HOME/serve/`; `PROTOCOL` checked at `hello`; the behavior suite runs against a local and a remote client. Companion: [D27, tau-code's changes](D27-tau-code-changes.md). |
| [D22](D22-print-mode.md) | Print mode | accepted | `tau -p`: one message to the first cursor, final reply on stdout (or the wire's events as JSON lines), `--answer` rules answer questions or decline; exit 3 when it stopped before finishing; attaches to a running `tau serve` that holds the session. |
| [D23](D23-the-tui.md) | The TUI | accepted | `tau` runs a Textual app (public module `tau.tui`) written only against `Client` (local or remote); 0.15's widgets, CSS and themes carry over; questions inline above the editor; the theme lives in a `tui` config key; tested with Textual's pilot through the `Harness`. |
| [D24](D24-standard-extensions.md) | Standard extensions | accepted | Compaction, prune (deterministic, by turns), usage, `@file`, skills and templates, permissions and sub-agent (off by default) as built-ins, and retry as a provider wrapper with a `load: {retry: false}` switch; `--no-extensions` affects no built-in; `context_window` and prices on `ModelConfig`. |
| [D26](D26-token-counting.md) | Token counting | accepted, T1 built | A count carries its provenance (`reported`, `rendered`, `calibrated`, `heuristic`, `ceiling`) and no estimate is made from characters; compaction measures the cut, the checks and the next bill in the reported unit, with a byte ceiling for what is not sent yet; tokenizers, calibration and count calls are optional layers (T2 to T7). |
| [D27](D27-tau-code-changes.md) | tau-code's changes for D21 | accepted | The table of what tau-code changes to speak D21's wire (moved out of D21 §10; was `D21-tau-code-changes`). |
