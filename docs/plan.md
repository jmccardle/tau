# τ next — plan

τ's agent harness rewritten as a sans-IO Rust core under a Python driver.
It ships as **τ 0.16.0, codename Trusty**: the τ repository absorbs this one,
`tests/` replaces the 0.15 suite, and `parity/` is deleted. Development
distribution: `ffwf-tau-next`, import `tau_next` (D10).

The design lives in `docs/design/` (`model.md`, `glossary.md`, `decisions/`).
This file holds only the phases and where we are. The original hand-off plan,
with its model sketch (§1) and decisions table (§4), is in git history
(`b07efb9` and earlier).

## Status (2026-10-27)

| phase | state |
|---|---|
| 0 scaffold | **done.** Workspace, maturin, `cp312-abi3` + `cp314t` wheels, CI on forge.goblincorps.com, rule lint. |
| 1 design docs | **done** (signed off 2026-10-27). `docs/design/` is accepted and aligned to the spike. |
| 2 behavior spec | **folded into 3** (order below). No `Harness`, no fakes yet; 8 required scenarios exist as Rust tests (`crates/tau-core/tests/step.rs`). |
| 3 Rust core | **in progress**, growing the spike. |
| 4–7 | not started. |

Rulings since revision 7 (recorded in `model.md` and the ADRs):
- writes happen inside the Rust call; persistence is not a job (D11);
- JMFTS is a replica fed from the local log (D12);
- the input phase applies whenever input is pending; queueing is an extension's;
- no crash recovery for calls in core: rest at the unanswered batch, input
  goes under it, a step re-runs it (D4);
- post hooks run on synced data: one sync before each post hook and when the
  step ends, not one per write (D11);
- `Results` is `by: Tools`, and each outcome carries its own `by` (D7).

**The step spike** (`crates/tau-core/src/{tree,step}.rs`, ~700 lines, plus
~400 lines of tests) is the phase loop of D11 as a sans-IO machine:
`next_job` / `feed`, the cursor hooks, one `Results` node per batch, the
throttle, abort, reload with auto-step off. A turn costs ~15–30 µs at any
session depth. It covers part of 3a (`Node`, `Body`, `Move`, `Item`, `walk`,
`next`, V1, V2) and the core of 3c. Missing: labels, `config_at`, LCA,
questions, go-to on a busy cursor, `max_steps`, proptests, the store (3b),
the bindings, and the clippy gate (pedantic lints; deferred to the Phase 3
closeout).

### Order from here (approved 2026-10-27; bends rule 5)

Phase 2 as written specifies everything through the Python `Harness` before
any implementation. The spike shows the machine is small and cheap to test
directly, so:

1. Phase 1 is signed off. Change `model.md` only when code forces it.
2. **Grow the spike into 3a + 3c** with a Rust scenario test per item in the
   Phase 2 coverage list, written before the code that passes it. These are
   the behavior spec at the core level.
3. **3b store**, then **`tau-py` + a minimal driver**, then the `Harness`,
   `FakeProvider`, `FakeTool` (Phase 2's Python half), and port the scenarios
   to `tests/behavior/`, which is what carries into τ 0.16.
4. `parity/` keep-tests over τ 0.15 can be written any time; they block
   nothing.

## Rules

`CLAUDE.md` holds them. Rule 5: each phase ends with a stop for review.

## Phases

### Phase 0: scaffold — done
Cargo workspace, maturin, wheels (Linux x86_64 only; aarch64 and macOS need
runners we don't have — revisit before release), clippy, rustfmt, ruff,
pyright strict, proptest. Exit met: the wheel imports next to `ffwf-tau` 0.15,
including on free-threaded 3.14t without re-enabling the GIL.

### Phase 1: design docs — done
`glossary.md`, `model.md` (numbered invariants, each with its enforcing
test), `decisions/` D1–D12, hand-drawn diagrams (the last ones; rule 6).

### Phase 2: behavior spec
- `tests/behavior/harness.py`: the `Harness` protocol, shaped as an
  in-process `tau serve` (model.md): session create/load, `crash()` (drop
  memory, keep disk), `attach(client)` with input, go-to, step, abort,
  auto-step, answer, decline, and the client's events; tree, cursor, label,
  and scratchpad snapshots; `open_cursor`.
- `settle()`: run until every cursor rests or holds only jobs a fake has not
  released. No test sleeps.
- `FakeProvider` and `FakeTool` with **gates** (hold a sample or call open,
  act, release), and a fake clock for cache TTLs and the throttle.
- Scenarios mined from the as-built review §8/§9 and the 0.15 suite (for
  scenarios only), each tagged `keep`, `change`, `drop`, or `new`.
- `parity/`: a `Harness` adapter over τ 0.15 for the `keep` tests (plain
  turn, tool batch, tool error, unknown tool, bad args, HTTP error). An API
  diff, not the spec; run by `scripts/parity.sh`, never by `check.sh`.
- Required coverage (✓ = covered in `crates/tau-core/tests/step.rs`):
  - ✓ plain turn; ✓ tool batch (one `Results`, call order); tool error;
    unknown tool; bad args; HTTP error; mid-stream error;
  - ✓ abort during sample with parts; abort during sample without parts;
    abort during tools (running calls `Interrupted`, `post_result` once);
  - crash during sample (the next step samples again); ✓ crash mid-batch:
    rests at the batch, input goes under it, a step re-runs it; crash between
    a delivering record and the items' removal (delivered once);
  - input while busy is delivered by the next step, after the batch; input at
    rest; ✓ refused input (notice, item removed); abort leaves pending input
    pending; abort then send;
  - a frontend's go-to refused while a hook runs or a question is open; go-to
    on a busy cursor aborts, runs post hooks and `post_step`, then moves; a
    go-to wakes with no phase;
  - a question locks the cursor; esc does not close it; a single-client
    question is declined when that client detaches; abort during a `pre_tool`
    awaiting approval, then approval, gives `Interrupted`;
  - hook strings: `pre_tool` → `Err(reason)`; a cursor hook halts the step;
    ✓ `pre_input` refuses;
  - a `pre_sample` hook moves the cursor; ✓ a `pre_reply` move chooses where
    the reply lands; a `post_reply` move changes where tools look;
  - `cursor.moves(since_step=True)` shows other hooks' moves;
  - ✓ the throttle, and an item left in place does not spin;
  - agent-to-agent items wake an idle cursor; a sub-agent tool waits for its
    cursor to rest;
  - two cursors at one batch both run it; re-run from the `Assistant` node
    writes a sibling `Results`;
  - branch across a config node; detached-root one-off call; labels across a
    reload; `max_steps` stops a "continue" loop; reload equals pre-save state
    (I7); a provider-illegal walk fails at the next sample (I5).
- **Exit:** `keep` green through `parity/`; everything else collected and
  failing for the right reason. Sign-off.

### Phase 3: Rust core
- **3a, types and views.** `Node`, `Body`, `Results`, `Move`, `Label`, `Item`,
  `walk`, `next`, `config_at`, LCA, the append checks (V1, V2). Proptest I1–I5.
- **3b, store.** `tau-store` per D12: one record log (length-prefixed,
  checksummed), the scratchpad file rewritten atomically, the lease, load with
  the `delivers` reconciliation, a trait for the JMFTS replica. Proptest I7,
  including torn tails. Sync at `Record::Sync` only (D11).
- **3c, the step machine.** The phase loop as a sans-IO machine (D11).
  Proptests over scripted results and frontend calls. Transition tables
  emitted as DOT, checked in CI. `tau-py` bindings: `Session`, `Cursor`,
  `Node`, handles, plain functions.
- **Exit:** property tests green; clippy gate green; ratchet on (rule 7).

### Phase 4: Python driver, one provider, built-in tools
- The driver performs jobs: hook calls, samples, calls (each call's
  `pre_tool`, tool, `pre_result`), waits. It cancels what the machine says to.
- OpenAI-compatible provider first (covers llama.cpp), including `Results`
  expansion and rendering unanswered calls as text. Typed request settings
  from `Config`.
- Typed config file schema (pydantic); unknown keys are errors.
- Built-in tools as one `Tool` type. Rust tools ship as their own PyO3
  wheels, called by the driver with the GIL released; core has no plugin ABI.
- **Exit:** behavior suite green except tests that need later phases.

### Phase 5: extensions that prove the model
Compaction (warm in-context summary; cold summary on a detached root; branch
from the deepest verbatim prefix), prune, sub-agent, permissions (`pre_tool`
asks through `cursor.ask`), debug frontend (auto-step off), and optionally
keeping batch outcomes across a crash (D4). The provider gains
`cache_state(walk)`. **Exit:** each has behavior tests and needed no core
change; if one did, stop and write an ADR.

### Phase 6: frontends
`tau serve` first (many clients, sessions and cursors multiplexed, questions
and scratchpads replayed on connect), then print/json, REPL, RPC, TUI, each a
client of the same model. One event projector per wire.

### Phase 7: release
Anthropic and Google providers with `cache_state`; JMFTS replica; one-way
converter from 0.15 sessions (D9); docs; absorption into the τ repository as
0.16.0.
