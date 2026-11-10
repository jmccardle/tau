# τ 0.16 — plan

τ's agent harness rewritten as a sans-IO Rust core under a Python driver,
shipping as **τ 0.16.0, codename Trusty**. The τ repository absorbed
Trusty's repository on 2026-10-31 (merge commit `rewrite Tau in Rust`):
`tests/` replaces the 0.15 suite, and `parity/` is deleted. Distribution
`ffwf-tau`, import `tau` (D10).

The design lives in `docs/design/` (`model.md`, `glossary.md`, `decisions/`).
This file holds only the phases and where we are. The original hand-off plan,
with its model sketch (§1) and decisions table (§4), is in git history
(`b07efb9` and earlier).

## Status (2026-10-31)

| phase | state |
|---|---|
| 0 scaffold | **done.** Workspace, maturin, `cp312-abi3` + `cp314t` wheels, CI on forge.goblincorps.com, rule lint. |
| 1 design docs | **done** (signed off 2026-10-27). `docs/design/` is accepted and aligned to the spike. |
| 2 behavior spec | **folded into 3** (order below). The `Harness` and fakes exist (`tests/behavior/`); the Phase 2 coverage list is not yet ported in full. |
| 3 Rust core | **done** (signed off 2026-10-29): core, store, bindings; 3d as-built review in `docs/as-built/`, ratchet on with an empty allowlist. |
| 4 driver, provider, tools | **done** (signed off 2026-10-30): driver, `@tool`, base tools, D13 config, D15 providers and events. Exit review `docs/as-built/phase4/`; the ratchet covers `python/`. |
| 5 extensions | **done** (2026-10-31): D16 stages 1 (values, functions as tools), 2 (`Client`, events, questions with ids) and 3 (commands, options, completion, `commandline`, `examples/chat.py`) built; stage 4 (`tau serve`, `RemoteClient`) is Phase 7. Fix list `plan-phase5-fixes.md`: stages A and B built and signed off 2026-10-31; C and D built as one stage and signed off 2026-10-31 (D17 accepted 2026-10-31). |
| 6 absorption | **done** (2026-10-31): history merged into the τ repository, renamed to `tau` / `ffwf-tau`, `parity/` deleted, branch `0.15.x` for 0.15 hotfixes. |
| 7 foundations and the wire | **in progress**: ADRs D18–D21 accepted 2026-11-01. Stage 7A (D19, D18, D20: the shared start) built, reviewed and signed off 2026-11-01. Stage 7B (D21, the wire) built, reviewed and signed off 2026-11-01. Stage 7C (API docs: surface, reference, docstrings, guides) built, reviewed and signed off 2026-11-02. Exit gap: tau-search's own extension cannot load until its port after 0.16.0; the entry point is tested with fixture packages. |
| 8 frontends, tools, standard extensions | **in progress** (2026-11-02). Stages: **8A** `base_tools` built-in extension, `edit`, `find`; **8B** `tau -p`, `--answer`, `--version`, `--verbose`; **8C** the Textual TUI; **8D** the ratatui example; **8E** standard extensions. 8B, 8C and 8E get ADRs (D22–D24) first. D22–D24 accepted 2026-11-02; 8A signed off 2026-11-02; 8B built and reviewed 2026-11-03; 8C built and reviewed 2026-11-03 in three cuts (8C-1 `tau` opens the TUI: transcript, editor, commands, forms, questions, themes; 8C-2 cursor bar and switching, Ctrl+Z, `Client.remove_item`, the tree screen, the session picker, re-exec; 8C-3 `/config` forms with save, `/model` picker, saved theme, render window, snapshots, TUI guide page), signed off 2026-11-03; 8D built and reviewed 2026-11-03 (the ratatui example: a Rust client of `tau serve`, wire types generated from the schema; the schema gained `minimum: 0` on every id and an integer `ErrorCode`), stopped for review. Fixed 2026-11-03: the hub's `Feed.offer` moved a replacing `CursorView` to the end of the buffer, past the snapshot's `Labels` or `Live` deltas; a state now replaces its copy only where nothing but state follows it (D16 Order, `tests/behavior/test_event_order.py`). TUI follow-ups after 8D (owner, 2026-11-03): a tree view that follows the active cursor beside the other branches and cursors (built and reviewed 2026-11-03: the Ctrl+G screen reworked, the viewed walk straight with other branches as folded stubs, closed cursors dimmed, following; D23 "Built: tree view"; stopped for review); extension and tool lists with load/unload written as config; compaction and pruning (8E) checked through that tree view. **8E-4 built** (2026-11-03): permissions (`ask`, `allow`, `deny` rules, `yes | always | no`, asking nothing until configured; `tau -p --answer`) and the `agents` built-in, off by default (`agent(task, to=None)` opens a closed-when-done cursor `agent:N`), M10 (D24 "Built: 8E-4"); the ratatui example draws a question of several lines; reviewed twice 2026-11-03 (bash and path rule bypasses closed; the driver refuses a tool the sample was not offered); 8E complete, stopped for the owner's sign-off. **8E-3 built** (2026-11-03): `copied_from` and `derived_from` (M1), `/prune`, compaction (`/compact`, the proactive trigger on `context_window`, a summarizer cursor), a hook's `rest` ending `Aborted` when its own cursor is aborted (M7), the tree screen's copy and derived marks and `p` (D24 "Built: 8E-3"), reviewed twice 2026-11-03; soft and hard limits (Ruling 14, owner); core: an abort before sampling or during `pre_input` ends the step without sampling or committing more input. **8E-2 built** (2026-11-03): usage, `context_window`, `price`, `resolved_model`, `model(at=)`, `usage.written` (D24 "Built: 8E-2"), reviewed 2026-11-03; **8E-1 built** (2026-11-03): retry (`RetryProvider` in `tau`, `Retrying` delta, `load: {retry: false}`), `@file` attachments, prompt templates and skills (pinned by trust), built-ins built with the `LoadedConfig` (D24 "Built: 8E-1"), reviewed 2026-11-03; 8E is reviewed per cut and signed off as a whole. The ratatui example gained the tree browser and `/config` forms (2026-11-03, reviewed; matching D23's tree view). |
| 9 | not started; see Phases. |

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
to rerun them. Missing: the JMFTS replica trait (nothing feeds it before Phase 7). The bindings (`tau-py`) are built.

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
| 6 | first call | done (`examples/first_call.py`): a Python import example against a local llama.cpp server, not a CLI |
| 7 | first tool round-trip | done (`examples/first_tool.py`): `@tool` makes a `Tool` from a function; tools are the driver's, handed to each `sample`, never recorded; the example defines its own `read_file` (no built-in toolset yet) |
| 8 | cursor and tree bindings | done (2026-10-29): a cursor hook's `move`, `write`, `set_auto` (checked by the hook job's serial); `append`, `open`, `add_item`, `remove_item`, labels from both handles, and `add_item`, `set_label`, `remove_label` from the driver; reads through `tree` (`node`, `walk`, `children`, `config_at`), `cursor`, `cursors`, `label` on both handles and `driver.session`; `tests/behavior/test_tree.py`. **Deferred:** update-an-item (an in-place update must not bump `changes`, as only an added item does); `show`/`tell` wait for the events of model.md §9; `opened_by` on `open` is not spiked |
| 9 | base tools | done: `read`, `write`, `grep`, `bash` in Rust (`crates/tau-tools`), as `Tool`s (`tau.base_tools`); output limit with overflow file; `bash` kills its process group on cancel and timeout (`tests/tools/`, `tests/behavior/test_base_tools.py`). Bound by `tau-py` rather than shipped as a separate wheel (Phase 4 says otherwise): one wheel for now; revisit if the tools grow heavy dependencies |

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
   - wave 1: **A** `tau.config` (layers, groups, qualified names,
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
- Required coverage (✓ = covered in `crates/tau-core/tests/`, or in the Python
  suite where a test location is named):
  - ✓ plain turn; ✓ tool batch (one `Results`, call order); ✓ tool error;
    ✓ unknown tool; ✓ bad args (`tests/behavior/test_scenarios.py`,
    driver-level); ✓ HTTP error; ✓ mid-stream error;
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
    question is declined when that client detaches (Phase 6: no client
    attach/detach until `tau serve`); abort during a `pre_tool` gives
    `Interrupted` once the hook finishes (✓ `test_scenarios.py`; the
    awaiting-approval form is Phase 5's permissions extension: a tool
    hook has no `ask` yet, `NodeHandle` lacks it);
  - hook strings: ✓ `pre_tool` → `Err(reason)` (`test_scenarios.py`); ✓ a cursor hook halts the step;
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
  - ✓ branch across a config node (`test_scenarios.py`); ✓ detached-root
    one-off call (`test_scenarios.py`); ✓ labels across a
    reload; ✓ `StepLimit` stops a "continue" loop and a model that calls tools forever; ✓ reload equals pre-save state
    (I7); ✓ a provider-illegal walk fails at the next sample (I5)
    (`tests/providers/test_illegal_walk.py`).
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

### Phase 4: Python driver, providers, built-in tools
- The driver performs jobs: hook calls, samples, calls (each call's
  `pre_tool`, tool, `pre_result`), waits. It cancels what the machine says to.
- Providers: OpenAI-compatible (covers llama.cpp), Anthropic and Gemini are built
  (D15), all sharing one HTTP module and one walk, with `Results` expansion and
  unanswered calls rendered as text. Typed request settings from `Config`; a
  provider's own settings in `options`.
- Config: D13's own parser and checker (built; no pydantic). Each provider
  checks its own `options` (D15).
- Base tools (`read`, `write`, `grep`, `bash`) in Rust (`crates/tau-tools`),
  bound by `tau-py` into the one wheel (roadmap step 9); core has no plugin ABI.
- **Exit:** behavior suite green except tests that need later phases. The
  as-built review again, on `python/` with the 0.15 tools as they are (radon,
  coverage.py, grimp, `trace_turn.py`), plus a traced turn across both
  languages.

### Phase 5: extensions that prove the model
**First, D16** (`decisions/D16-clients-and-asking.md`): the `Client` protocol (local and
remote, one contract), whole cursor state to every client with a snapshot on connect,
`Appended` instead of `Saved`, questions with ids, targets and schemas, `@command` and
extension config as forms, completion from fixed values and named formats. Built in its
order (Consequences), then the extensions below use it: permissions is the first typed
question, compaction and prune the first commands. A small chat example is the hands-on
client. Carried from Phase 4: trust's hash scope once `.tau/` holds hooks.

Compaction (warm in-context summary; cold summary on a detached root; branch
from the deepest verbatim prefix), prune, sub-agent, permissions (`pre_tool`
asks through `cursor.ask`), debug frontend (auto-step off), and optionally
keeping batch outcomes across a crash (D4). The provider gains
`cache_state(walk)`. **Exit:** each has behavior tests and needed no core
change; if one did, stop and write an ADR.

**Re-scoped 2026-10-31:** Phase 5 closes with `plan-phase5-fixes.md` (stages
A–D). The extensions listed above are not built yet. They become Phase 8's
standard-extension work, built next to the frontends that need them. The
speculative extensions (`examples/speculative/`) are their prototypes: bash_guard
for permissions, forest and gamemaster for sub-agents.

### Phase 6: absorption — done (2026-10-31)
The τ repository merged Trusty's history with `--no-ff` (merge commit `rewrite Tau
in Rust`), taking Trusty's tree whole. Then: renamed to `tau` / `ffwf-tau`
(D10), deleted `parity/`, rewrote `CLAUDE.md` for τ (rule 1 allows code from
0.15 to be carried over selectively, never harness code), and created branch
`0.15.x` from 0.15's last commit for its hotfixes.

Phases 7–9 (2026-10-31) place every group of capabilities that 0.15 had and
0.16 neither built nor planned (`docs/from-0.15.md` §4). Order: finish the fix plan
(B–D), then 7, 8, 9. Syncing tau-code and tau-search comes after 0.16.0: the
items in these plans take precedence.

### Phase 7: the `tau` program's foundations, and the wire
Every frontend needs these before it can start a session.
- **Session catalog** (ADR):
  - where sessions live (`$TAU_HOME/sessions`, `--session-dir`);
  - listing them by working directory, naming, opening and continuing them;
  - `--fork REF`, a new session copying a walk from another (glossary
    `copied_from`, not yet spiked);
  - export to Markdown.

  Built 2026-11-01 (D18): `examples/chat.py` uses the shared start (`start_session`).
- **Extension loading** (ADR): the trusted loader carried from Phase 4.
  - Sources: the built-ins, the `tau.extensions` entry point (tau-search's
    contract), `~/.tau/extensions/`, the project's `.tau/extensions/` under
    trust, and `-e PATH`.
  - Turning them on and off is written to the config (fix plan D1), along with
    `--no-extensions` and `tau ext list`.
  - Command namespacing (`/ext:cmd`) for two extensions with the same command.
- **Context assembly:**
  - the system prompt from the config and from files (`AGENTS.md`,
    `CLAUDE.md`, `.tau/SYSTEM.md`);
  - `--system-prompt`, `--append-system-prompt`, `--no-context-files`;
  - the ADR decides whether this is a startup step of `tau` or a standard
    extension.
- **`tau serve` and `RemoteClient`** (D16 stage 4). The wire is the `Client`
  contract serialized: JSON-RPC over WebSocket. A remote client and a local
  one see the same behavior. From day one it has:
  - `tau serve --schema`, so clients can generate their types (tau-code does);
  - a protocol version checked at `hello`;
  - daemon discovery with `serve -d --json`.

  The server parses and completes a typed command line, so a client outside
  Python does not reimplement `commandline`.
- **The API documentation:**
  - **Surface manifest.** A generated, checked-in list of every public name
    and signature, compared in CI the way the diagrams are, so any API change
    is a readable diff in review. What is public (decided in 7C-1, reasons in
    `docs/api/README.md`): `effects`, `events`, `handles`, `protocols`, `clock`
    and the rest are internal modules (underscore names) whose public contents
    `tau` exports; `tau.cli`, `tau.commandline` and `tau.trace` are the only
    public modules (`commandline` is public because its `value` and `values`
    are how a frontend reads an answer); the three hosted provider classes are
    exported, `MockProvider` is not (`builtin_providers()["mock"]`). A public
    signature names only public tau types, the standard library's, and an
    allow list (`httpx`).
  - **Reference.** Generated from docstrings, with a ratchet check that every
    public name has one. The native types' docs live in Rust, so `help()`
    works at runtime, and the stub is checked against them. Today 2 of 224
    stub entries have docstrings.
  - **Guides**, user-facing and separate from `docs/design/`: quickstart; the
    model in ten minutes; writing an extension; writing a frontend;
    configuration; the wire protocol, generated from the schema.
  - **7C-1 built** (2026-11-01): `docs/api/` (groups, README, `surface.txt`), `docs/reference/`,
    `scripts/api_docs.py` and `scripts/stub_docs.py`, and the docstring count in the ratchet (report
    only until 7C-2 sets the bar to 0).
- **Exit:**
  - a remote client passes the same behavior tests as a local one;
  - `tau-search`'s extension loads through the entry point (`-e` for now);
  - the manifest and the reference are checked in CI.

### Phase 8: frontends, tools and standard extensions
**Frontends**, each a client of the same model:
- `tau` launches the Textual TUI. It reuses 0.15's core widgets (message,
  tool and reasoning boxes, the stream strip, themes and `tau.tcss`, dialogs,
  the session picker, the tree widget), with the app around them written
  against `Client`.
- `tau -p`, with text or JSON output. The `-p` client is the one that
  answers questions: with `--answer` rules (0.15's `--ui-defaults`), or it
  declines. Ruling 1 holds, because someone does answer. On a session held by
  a running `tau serve`, `-p` connects to that server (D22 §8); attached, it
  answers or declines only questions on its own cursor.
- `--version` and `--verbose`.
- A ratatui example TUI, a Rust client of `tau serve` in `examples/ratatui/`
  (its own Cargo package, outside the workspace and the ratchet). It is the
  first client outside Python, and proof that the schema is enough. Decided 2026-11-03:
  - scope is a usable chat client: find the session from `$TAU_HOME/serve/*.json` or
    `--socket`; the first cursor's walk (messages, thinking, tools) with live text; send, Esc
    abort, questions answered or declined inline, `/` lines through the server's parser,
    reconnect after `CutOff`. Not the tree, picker, cursor switching, forms, themes or Ctrl+Z;
  - its wire types are generated from `docs/protocol.schema.json`, not hand-written; where the
    schema is not enough, the schema is fixed;
  - one end-to-end test drives the built binary in a pty against a `tau serve` (pytest,
    `tests/ptykit.py`), and a CI job builds, lints and runs it;
  - it speaks one protocol number exactly; a mismatch exits with the server's error.

**Base tools:** the shared start (`tau.start_session`, D18 §5) gets them from a built-in
extension, `base_tools`, loaded like `step_limit` and turned off with `load: {base_tools: false}`
or `tau ext disable base_tools` (decided 2026-11-01). `--no-extensions` skips the discovered
sources only and leaves every built-in as `load` says (ruling 2026-11-02, as D19 §5). Then `edit` (search and replace, reporting a diff; in
Rust beside `write`), and `find` (glob).

**Dropped or deferred from 0.15** (decided 2026-11-01): the `ls` tool (`bash` covers it), the
REPL frontend (`--mode repl`), and image input until after 0.16.0 (D15 Deferred).

**Standard extensions**, shipped built in:
- compaction, which needs `context_window` on `ModelConfig` and a token
  estimate;
- permissions, from the bash_guard prototype;
- sub-agent, from forest and gamemaster; off by default, and the first cut if 8E runs long;
- prune, in with the design of D24 §9 (by turns, no model); `/prune` ships, automatic pruning
  waits for a way to know the cache's state;
- retry on provider errors (today only `examples/retry.py`): a provider wrapper, on by
  default, turned off with `load: {retry: false}` (D24 §4);
- skills and prompt templates (commands that insert text);
- `@file` attachments;
- usage: per-turn cost (price fields on `ModelConfig`), session stats, and the
  notice when the cache is not being read.

### Phase 9: release 0.16.0
- Wheels beyond Linux x86_64. aarch64 and macOS need runners; the base tools
  are Linux-only.
- The PyPI publish workflow on GitHub. It is rewritten for maturin but keeps
  `publish.yml` as its name, because the trusted publisher is bound to that
  name.
- The docs site, and release notes written from `docs/from-0.15.md`
  (what changed, what was dropped, what comes later). There is no 0.15
  session converter (D9 superseded), and no 0.15 extension shim.
- **The JMFTS replica comes after 0.16.0** (decided 2026-10-31). Its absence
  is an accepted regression.

### After 0.16.0
- **tau-code:** its protocol package is regenerated from `--schema`, and its
  React components carry over. 0.15 commands with no counterpart here
  (elide, commit-branch, paste-subtree, compare, steer and enqueue) become
  commands or extensions, or are dropped. Its web client replaces 0.15's
  `tau serve --web-root`.
- **tau-search:** a small port. Its `tau_agent_core` imports
  (`tool_from_function`, `ToolError`) become a plain function and `tau`'s
  `ToolError`, and its dependency becomes `ffwf-tau`.
- **JMFTS replica and Tectum**, in one pass (decided 2026-10-31: very
  important, but no rush). Tectum needs the same protocol over stdio, for
  programs that run `tau` as a subprocess (0.15's `--mode rpc`), and its
  `--no-tools` contract.
- **Later, each extension-shaped or tooling:** image input, constrained
  decoding, a models.dev catalog generator (`tau config` subcommand),
  background tools, code mode, the NATS bus, HTML export, the llama.cpp
  telemetry strip, `serve --tail`, the Docker image, and a TUI screenshot
  harness.
- **Dropped:** the REPL (the TUI and `examples/chat.py` cover it), `ls`
  (bash), `--fun`.
