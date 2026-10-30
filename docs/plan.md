# τ next — plan

τ's agent harness rewritten as a sans-IO Rust core under a Python driver.
It ships as **τ 0.16.0, codename Trusty**: the τ repository absorbs this one,
`tests/` replaces the 0.15 suite, and `parity/` is deleted. Development
distribution: `ffwf-tau-next`, import `tau_next` (D10).

The design lives in `docs/design/` (`model.md`, `glossary.md`, `decisions/`).
This file holds only the phases and where we are. The original hand-off plan,
with its model sketch (§1) and decisions table (§4), is in git history
(`b07efb9` and earlier).

## Status (2026-10-29)

| phase | state |
|---|---|
| 0 scaffold | **done.** Workspace, maturin, `cp312-abi3` + `cp314t` wheels, CI on forge.goblincorps.com, rule lint. |
| 1 design docs | **done** (signed off 2026-10-27). `docs/design/` is accepted and aligned to the spike. |
| 2 behavior spec | **folded into 3** (order below). The `Harness` and fakes exist (`tests/behavior/`); the Phase 2 coverage list is not yet ported in full. |
| 3 Rust core | **done** (signed off 2026-10-29): core, store, bindings; 3d as-built review in `docs/as-built/`, ratchet on with an empty allowlist. |
| 4 driver, provider, tools | **mostly built** ahead of 3d by the roadmap below: driver, OpenAI provider, `@tool`, base tools. D13 config built (2026-10-29). D15 (providers and events) is a candidate: first slice built 2026-10-29 (provider contract, `max_tokens`/`options`, provider map, live and saved events, mock provider); tool progress built 2026-10-30; replay data not yet. Missing: the 4 exit review. |
| 5–7 | not started. |

Rulings since revision 7 (recorded in `model.md` and the ADRs):
- writes happen inside the Rust call; persistence is not a job (D11);
- JMFTS is a replica fed from the local log (D12);
- the input phase applies whenever input is pending; queueing is an extension's;
- no crash recovery for calls in core: rest at the unanswered batch, input
  goes under it, a step re-runs it (D4);
- post hooks run on synced data: one sync before each post hook and when the
  step ends, not one per write (D11);
- `Results` is `by: Tools`, and each outcome carries its own `by` (D7).

**The core so far** (`crates/tau-core/src/{tree,step,machine}.rs`, ~1,100
lines, plus ~1,400 lines of tests): the tree with labels, `config_at`, and
LCA (3a); one cursor's step as a sans-IO machine (D11); and
the session machine over every cursor, taking frontend calls (go-to on a busy
cursor, `Session.step`, wakes). The I1–I5
proptests (`tests/invariants.rs`) drive it with random frontend calls and
random answers to every job, hooks that move the cursor included. A turn costs
~15–30 µs at any session depth. Replay (`tree::load`) and the I7
proptests are in. The store (3b, `crates/tau-store`, ~270 lines) writes the
log and the scratchpad file, holds the lease, and cuts torn tails; a stored
turn costs ~3 ms (fsyncs). The clippy gate (pedantic, `-D warnings`) is
green. `docs/benchmarks.md` records the access-pattern benchmarks and how
to rerun them. Missing: the JMFTS replica trait (nothing feeds it before Phase 7) and
the bindings.

### Order from here (approved 2026-10-27; bends rule 5)

Phase 2 as written specifies everything through the Python `Harness` before
any implementation. The spike shows the machine is small and cheap to test
directly, so:

1. Phase 1 is signed off. Change `model.md` only when code forces it.
2. **Grow the spike into 3a + 3c** with a Rust scenario test per item in the
   Phase 2 coverage list, written before the code that passes it. These are
   the behavior spec at the core level.
3. **3b store** (done), **`tau-py`** (done), then **a minimal driver**. D14
   (loading a subset of a long history) is deferred: every body stays in memory.

### Roadmap to the first LLM call (2026-10-28)

| # | step | done when |
|---|---|---|
| 1 | `tau-py` bindings | done: a turn driven by hand from Python (`tests/core/`) |
| 2 | minimal driver | performs every job, cancels on abort; provider, tool, hook protocols |
| 3 | `Harness`, `FakeProvider`, `FakeTool` | the first Rust scenarios pass as Python tests (`tests/behavior/`); review starts here |
| 4 | minimal config | done, then replaced by D13: a `Config` names its model; the driver resolves it from the layered config |
| 5 | OpenAI-compatible provider | done: walk to messages, streamed parts, stop normalized and kept raw (`finish_reason`), tools array, HTTP errors as failures |
| 6 | first call | done (`examples/first_call.py`): a Python import example against local llama.cpp (`192.168.1.100:8100`), not a CLI |
| 7 | first tool round-trip | done (`examples/first_tool.py`): `@tool` makes a `Tool` from a function; tools are the driver's, handed to each `sample`, never recorded; the example defines its own `read_file` (no built-in toolset yet) |
| 8 | cursor and tree bindings | done (2026-10-29): a cursor hook's `move`, `write`, `set_auto` (checked by the hook job's serial); `append`, `open`, `add_item`, `remove_item`, labels, and the reads `children`, `node_count`, `all_labels` from both handles and the driver; `tests/behavior/test_tree.py`. **Deferred:** update-an-item (an in-place update must not bump `changes`, as only an added item does); `show`/`tell` wait for the events of model.md §9; `opened_by` on `open` is not spiked |
| 9 | base tools | done: `read`, `write`, `grep`, `bash` in Rust (`crates/tau-tools`), as `Tool`s (`tau_next.base_tools`); output limit with overflow file; `bash` kills its process group on cancel and timeout (`tests/tools/`, `tests/behavior/test_base_tools.py`). Bound by `tau-py` rather than shipped as a separate wheel (Phase 4 says otherwise): one wheel for now; revisit if the tools grow heavy dependencies |

**Deferred (2026-10-29):**
- Overflow-file pruning: a user config or an option of the base tools
  extension, by age and/or total size (e.g. last 60 days, last 0.25 GB).
- A macOS implementation of the base tools (`bash` finds leftover processes
  through `/proc`, so it is Linux-only), and a `powershell` tool for Windows.

4. **Config overhaul** (approved 2026-10-27; built 2026-10-29), in Python: the config file
   parser, project trust, and `tau config check`. Written up as
   `decisions/D13-config.md` (accepted 2026-10-29), which supersedes the
   points agreed here earlier: model names are qualified by group
   (`group:name`), not unique file-wide; a `Config` node names its model
   instead of storing a snapshot; no schema version before 1.0.
   Build (2026-10-29; `tau` and `~/.tau` are ours now, shadowing 0.15):
   - wave 1: **A** `tau_next.config` (layers, groups, qualified names,
     merge, lookup, resolve with sources, problems); **B** `meta` on
     `Assistant` (opaque to core; the provider records what a response
     carries: model, id, fingerprint, usage);
   - wave 2: **C** a `Config` node names its model, the driver resolves it at
     sample time; **D** trust (`trusted.json` with a content hash, upward
     search, owner/700/`.gitignore`, a live-terminal prompt, scripts refused)
     and our own `.env` reader;
   - wave 3: **E** the `tau` command (`config check [--show]`, `trust`); the
     examples take a model name from a sample config in `examples/`.
5. The `Harness`, `FakeProvider`, `FakeTool` (Phase 2's Python half), and the
   scenarios ported to `tests/behavior/`, which is what carries into τ 0.16.
6. `parity/` keep-tests over τ 0.15 can be written any time; they block
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
  act, release), and a fake clock for cache TTLs.
- Scenarios mined from the as-built review §8/§9 and the 0.15 suite (for
  scenarios only), each tagged `keep`, `change`, `drop`, or `new`.
- `parity/`: a `Harness` adapter over τ 0.15 for the `keep` tests (plain
  turn, tool batch, tool error, unknown tool, bad args, HTTP error). An API
  diff, not the spec; run by `scripts/parity.sh`, never by `check.sh`.
- Required coverage (✓ = covered in `crates/tau-core/tests/`):
  - ✓ plain turn; ✓ tool batch (one `Results`, call order); ✓ tool error;
    unknown tool; bad args; ✓ HTTP error; ✓ mid-stream error;
  - ✓ abort during sample with parts; ✓ abort during sample without parts;
    ✓ abort during tools (running calls `Interrupted`, `post_result` once);
  - ✓ crash during sample (the input is redelivered once); ✓ crash mid-batch:
    rests at the batch, input goes under it, a step re-runs it; ✓ crash between
    a delivering record and the items' removal (delivered once);
  - ✓ input while busy is delivered by the next step, after the batch; input at
    rest; ✓ refused input (notice, item removed); abort leaves pending input
    pending (✓); abort then send (✓);
  - ✓ a frontend's go-to on a busy cursor, a hook running included, aborts,
    runs post hooks and `post_step`, then moves; ✓ a go-to wakes with no phase;
  - ✓ a question holds its hook's step (the cursor is busy); ✓ esc and go-to
    decline it; a single-client
    question is declined when that client detaches; abort during a `pre_tool`
    awaiting approval, then approval, gives `Interrupted`;
  - hook strings: `pre_tool` → `Err(reason)`; ✓ a cursor hook halts the step;
    ✓ `pre_input` refuses;
  - ✓ a `pre_sample` hook moves the cursor; ✓ a `pre_reply` move chooses where
    the reply lands; ✓ a `post_reply` move changes where tools look;
  - ✓ `cursor.moves(since_step=True)` shows other hooks' moves;
  - ✓ a cursor that does not move rests with auto-step off (a hook that
    declines every input, a `pre_reply` that discards the reply); an item added
    while busy causes one more step, and an item left in place does not spin;
  - ✓ agent-to-agent items wake an idle cursor; a sub-agent tool waits for its
    cursor to rest;
  - ✓ two cursors at one batch both run it; ✓ re-run from the `Assistant` node
    writes a sibling `Results`;
  - branch across a config node; detached-root one-off call; ✓ labels across a
    reload; ✓ `StepLimit` stops a "continue" loop and a model that calls tools forever; ✓ reload equals pre-save state
    (I7); a provider-illegal walk fails at the next sample (I5).
- **Exit:** `keep` green through `parity/`; everything else collected and
  failing for the right reason. Sign-off.

### Phase 3: Rust core
- **3a, types and views.** `Node`, `Body`, `Results`, `Move`, `Label`, `Item`,
  `walk`, `next`, `config_at`, LCA, the append check (V2). Proptest I1–I5.
- **3b, store.** `tau-store` per D12: one record log (length-prefixed,
  checksummed), the scratchpad file rewritten atomically, the lease, load with
  the `delivers` reconciliation, a trait for the JMFTS replica. Proptest I7,
  including torn tails. Sync at `Record::Sync` only (D11).
- **3c, the step machine.** The phase loop as a sans-IO machine (D11).
  Proptests over scripted results and frontend calls. Transition tables
  emitted as DOT, checked in CI. `tau-py` bindings: `Session`, `Cursor`,
  `Node`, handles, plain functions.
- **3d, as-built review of the core.** The same review, tools, and plots
  as the τ 0.15 as-built review, on `crates/`, so the numbers compare:
  - metrics: `rust-code-analysis-cli` (CC, cognitive, LOC; for radon),
    `cargo llvm-cov` (for coverage.py), dead code denied plus `cargo machete`
    (for vulture), `cargo-modules` and `cargo depgraph` (for grimp/`deps.py`),
    `guppy` for "tau-core depends on no IO" as a check;
  - generated diagrams, diffed in CI (rule 6): the step's position graph
    and the frontend-call refusals, recorded by driving the scenario tests
    (not `next`, which is a three-row table); `Body`/`Node`/`Session`/`Machine`
    from a small `syn` script (for `uml.py`);
  - traces: `tracing` spans around `write`, `append`, and `answer`, exported
    with `tracing-chrome`.
  The rule 7 thresholds are set from this review.
- **Exit:** property tests green; clippy gate green; ratchet on (rule 7); the
  3d review signed off.

### Phase 4: Python driver, one provider, built-in tools
- The driver performs jobs: hook calls, samples, calls (each call's
  `pre_tool`, tool, `pre_result`), waits. It cancels what the machine says to.
- OpenAI-compatible provider first (covers llama.cpp), including `Results`
  expansion and rendering unanswered calls as text. Typed request settings
  from `Config`.
- Typed config file schema (pydantic); unknown keys are errors. The parser
  and checker land earlier (order step 4); here the provider's keys fill in.
- Built-in tools as one `Tool` type. Rust tools ship as their own PyO3
  wheels, called by the driver with the GIL released; core has no plugin ABI.
- **Exit:** behavior suite green except tests that need later phases. The
  as-built review again, on `python/` with the 0.15 tools as they are (radon,
  coverage.py, grimp, `trace_turn.py`), plus a traced turn across both
  languages.

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
Anthropic and Google providers with `cache_state`; JMFTS replica; docs (no
0.15 session converter: D9 superseded); absorption into the τ repository as
0.16.0.
