# D11: Core decides; the driver performs

Status: accepted (Phase 1); spiked in `crates/tau-core/src/step.rs`

## Context

The original plan put the step loop in Python and left `tau-core` with types
and `walk`. Most rules are about *when* (which phase applies, what a string
does, what an abort cancels), interleaved with waits on providers, tools, and
people. Providers, tools, and hooks are Python, so Python must await. The
question is where the decisions live.

## Decision

- **Each cursor's step is a sans-IO state machine in `tau-core`**
  (`step::Step`). It never waits. It hands out **jobs** (`Job`) and takes
  results (`Fed`); the Python driver performs one and feeds the other.
- **Only waits are jobs**: run a hook point, sample, run calls, wait (the
  throttle), rest. `pre_tool`, the tool, and `pre_result` run inside the
  driver's per-call unit; core holds outcomes and writes `Results` at the
  join.
- **Writes are not jobs.** Every append, move, label change, and item change
  happens inside the Rust call that makes it. `tau-core` queues the
  `Record`s (`Session::outbox`); a Rust layer composing core with `tau-store`
  hands them to the store inside the call. `tau-py` binds that layer.
- **Post hooks run on synced data.** The store may buffer writes; the step
  queues a `Sync` before each post hook (`post_reply`, `post_result`,
  `post_step`) and when the step ends. A phase's writes, pre hooks'
  included, share one sync. Calls outside a step sync on return.
- A hook job's result is only the edited draft and the return value.
- Frontend calls (input, go-to, step, abort, auto-step) are inputs to the
  session machine (`machine::Machine`), which holds every cursor's step and
  refuses a call only where it has no edge, with a typed error (`Busy`).
- No async runtime in Rust.

## Alternatives

| option | why not |
|---|---|
| A Python driver that decides | Every rule becomes asyncio with locks, testable only with real waits. |
| One asyncio task per cursor | The rules still live in Python. |
| Rust awaits Python through an async bridge | A runtime in the bindings; cancellation across the FFI. |

## Consequences

- Every rule is testable in Rust with no Python and no clock
  (`crates/tau-core/tests/step.rs`). A turn costs ~15–30 µs in the spike.
- The driver is small: perform jobs, cancel what abort says.
- One fsync (~0.3 ms on the dev container) per phase, not per write: a
  plain turn syncs twice. A crash loses at most the writes since the last
  sync; a lost pre hook write is lost with the commit it came with.
- Cost: `Session` is shared mutable state on free-threaded builds; its
  locking needs an ADR in Phase 3.
