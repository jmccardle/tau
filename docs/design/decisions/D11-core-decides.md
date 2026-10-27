# D11: Core decides; the driver performs

Status: proposed (Phase 1); spiked in `crates/tau-core/src/step.rs`

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
  writes them before returning. `tau-py` binds that layer. A hook's
  `cursor.write` is durable when it returns.
- A hook job's result is only the edited draft and the return value.
- Frontend calls (input, go-to, step, abort, auto-step, answer) are inputs
  to the same machine.
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
- Cost: a write blocks its thread for one fsync (~0.3 ms on the dev
  container). Whether a sync per write or per phase is the durability unit is
  open.
- Cost: `Session` is shared mutable state on free-threaded builds; its
  locking needs an ADR in Phase 3.
