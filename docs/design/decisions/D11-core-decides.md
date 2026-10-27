# D11: Core decides; the driver performs

Status: proposed (Phase 1, sixth revision, 2026-10-27; updated in the seventh)

## Context

The plan put the step loop in Python (`driver.py`, an asyncio step driver)
and left `tau-core` with types, `walk`, `next`, validation and `config_at`.
CLAUDE.md says behavior lives in `tau-core`.

Most of model.md is rules about *when*: which phase of a step applies, which
hook point comes next, what a string return does, when `pre_step` runs again
and is throttled, what an abort cancels and what it lets finish, when a
frontend's move is refused or aborts a step, `max_steps`. These rules
interleave with waits: provider streams, tools running concurrently, hooks
that ask a person.

Something has to await. Providers, tools and hooks are Python, so the awaiting
is Python's. The question is where the decisions live.

## Decision

- **Each cursor's step is a sans-IO state machine in `tau-core`** (glossary,
  step). Its state is `at`, auto-step, the draft, and the effect, and its
  step function is the phase loop of model.md §6: for each of the input,
  sample, and tools phases that applies, pre hook on the effect, effect, pre
  hook on the draft, commit under `at`, post hook; then `post_step`. Every
  rule is a row of the machine's tables.
- It never waits and performs nothing. It hands out **jobs**: run a hook
  point, sample, run a set of calls, persist a record, update the
  scratchpad (an input's removal, a journal item), wait (the throttle),
  rest.
- **The Python driver performs jobs and feeds back results**, with the time.
  A hook job's result is what the hook changed (its moves, draft edits, the
  auto-step flag) and its return value, `None` or a string. For a set of
  calls the driver feeds back each completion as it happens, in any order;
  core journals each and commits the `Results` node at the join.
- **Frontend calls are inputs to the same machine**: input, go-to, step,
  abort, set auto-step, answer, decline. The machine decides whether each is
  refused, aborts a step, or applies, and which running jobs the driver must
  cancel.
- `tau-py` exposes the machine as plain calls, with no async runtime in Rust
  (no tokio, no `pyo3-async-runtimes`).
- In-memory driver state (busy, auto-step, the `max_steps` count, open
  questions, shown state, the throttle) is held by core's machine too, and is
  still never stored (I7). There is no claim (Q13).

## Alternatives

| option | why not |
|---|---|
| A Python driver that decides (plan default) | Every rule above becomes asyncio code with locks. It can only be tested through Python with real waits, and proptest cannot reach it. |
| One asyncio task per cursor with a mailbox | Cleaner locking, but the rules still live in Python. |
| Rust drives, awaiting Python through an async bridge | Puts an async runtime into the bindings, makes Python cancellation cross the FFI, and breaks "no async runtime in core". |

## Consequences

- Every rule is reachable from Rust unit tests and proptest, with no Python
  and no clock: feed a script of results and frontend calls, check the jobs
  and records that come out. The I3, I4 and I7 proptests drive the machine
  directly.
- The Python driver is small: a loop that performs jobs, and the cancellation
  plumbing for streams and tools.
- The behavior suite still runs through Python (`Harness`), so the same rules
  are held from both sides.
- Cost: one FFI crossing per job and per result. A stream's deltas are
  events the driver emits without feeding each one back; only the sample's
  end is a result.
- Cost: the machine is mutable state shared across threads on a
  free-threaded build. `Session` is a `frozen` pyclass holding a `Mutex`;
  that needs its own glossary term and an ADR in Phase 3, per CLAUDE.md.
- Rule 7's ratchet (CC ≤ 15, file ≤ 800 lines) applies to the machine like
  any Rust code; the step function is table-driven, one table per phase,
  to stay inside it.
- Enforced by: Phase 3 proptests over the machine, and the Phase 2 behavior
  suite through `Harness`.
