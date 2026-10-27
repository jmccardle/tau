# τ next — rewrite plan

> **Amendments (2026-10-25, Phase 0).** The plan below is kept as handed off.
> Where these notes conflict with it, these notes win.
>
> - **Destiny.** The rewrite ships as **τ 0.16.0, codename "Trusty"**, not
>   `ffwf-tau` 1.0, and the τ repository absorbs this one rather than being
>   archived (§0). At that point `tests/` replaces the 0.15 suite wholesale and
>   `parity/` is deleted, its purpose complete. Development distribution:
>   `ffwf-tau-next`, import `tau_next` (D10); final names are settled at absorption.
> - **Parity is not the test suite** (supersedes `tests/oracle/` in §0 and
>   Phase 2). 0.16 is incompatible on purpose; the oracle tests are an API diff
>   (keep / change / drop / new), not a spec. They live in top-level `parity/`
>   with the `parity` dependency group and `scripts/parity.sh`. `tests/` must
>   pass with no τ 0.15 installed. The `Harness` protocol and the `tau_next`
>   adapter live in `tests/behavior/` (they carry over); the 0.15 adapter lives
>   in `parity/`.
> - **Python floor 3.12**, matching τ 0.15 (parity/ imports both in one venv). PyO3 0.30.1 / maturin 1.15.
> - **"abi3 wheels"** holds only for GIL builds: one `cp312-abi3` wheel. Free-threaded
>   builds have no stable ABI before 3.15 (`abi3t`, PEP 803), so 3.14t ships a
>   version-specific `cp314t` wheel. τ 0.15 is tested on 3.14t/3.15t, so this
>   is not optional.
> - **CI is Linux x86_64 only** (supersedes the Phase 0 matrix). It runs on
>   forge.goblincorps.com's single Ubuntu runner (`.forgejo/workflows/ci.yml`);
>   dev.ffwf.net has no runner. Wheels are manylinux2014 (glibc ≥ 2.17), built
>   with zig as the linker. Nothing builds or tests aarch64 or macOS, so we
>   claim neither. Revisit before release: those need runners we don't have
>   (or a release-time GitHub mirror).
> - **0.15 import names** are `tau_llm`, `tau_agent_core`, `tau_coding_agent`,
>   `tau_meta` (what `ffwf-tau==0.15.0` installs), plus the optional `tau_jmfts`;
>   there is no `tau` package to collide with.
> - **The as-built review** (§7–§11 cited below) is at the 0.15 tag
>   (`v0.15.0`, which the review's merge commit landed on).

Hand-off plan for Claude Code. Source of truth for design decisions until `docs/design/` exists in the new repo; after Phase 1 those files supersede this one.

**Inputs to read before starting**

- The τ 0.15 as-built review (`test/merge-all-2026-10-24 @ dde8cdeb`): §7 glossary and collision table, §8 scenarios, §9 traced turn and its 11 variants, §10 config/provider audit, §11 docs-vs-code. It describes what exists and what went wrong. It is not a spec.
- The τ 0.15 source tree, **read-only, for behavior**. Copy no code from it.

---

## 0. Repository

New repo, not an overhaul branch.

- **Different build system.** maturin/Cargo workspace vs the current five pure-Python distributions. A branch would carry both toolchains and the old layout's sediment.
- **Side-by-side install.** The old and new packages must import in the same venv so the behavior suite can run both (Phase 2). Different import names (`tau` stays 0.15; new package `tau_next` until release, renamed at 1.0) make this trivial. A branch makes it impossible.
- **0.15.x stays fixable** in the old repo without merge traffic.
- **Clean history.** The old repo keeps its history; the new one starts from the design docs.

At release, the new code ships as `ffwf-tau` 1.0 on PyPI and the old repo is archived with a pointer.

```
tau-next/
  Cargo.toml                 # workspace
  crates/
    tau-core/                # pure Rust: Node, Session, Cursor, walk, next, commit, validate, config_at
    tau-store/               # append-only node log + cursor-move log (file; JMFTS later), behind a trait
    tau-py/                  # PyO3 bindings (cdylib), abi3
  python/tau_next/
    driver.py                # asyncio step driver
    hooks.py                 # hook chain runner
    providers/               # openai-compatible first
    tools/                   # read, write, edit, bash, grep, find, ls
    ext/                     # compaction, subagent, prune (Phase 5)
    frontends/               # print/json first (Phase 6)
  docs/design/
    glossary.md
    model.md                 # data model, invariants, step algorithm
    decisions/               # one ADR per decision below
    diagrams/                # generated from code once Phase 3 lands
  tests/
    behavior/                # Python, through the Harness protocol only
    oracle/                  # Harness adapter over τ 0.15
  pyproject.toml             # maturin
  CLAUDE.md                  # the rules in §2, verbatim
```

---

## 1. Model

> Superseded by `docs/design/model.md` and `glossary.md` on Phase 1 sign-off. Kept as written for history.

### Objects

| object | definition |
|---|---|
| `Node` | `{id, parent: Option<NodeId>, at: Timestamp, by: Author, body: Body, copied_from?, derived_from?}`. Immutable once written. |
| `Session` | Forest of nodes for one conversation. Append-only. Multiple roots allowed (detached one-off calls). |
| `Cursor` | `{id, at: NodeId, target: NodeId}`. Insertion point. `at` is committed; `target` is the draft tip. |
| `walk(n)` | Root-to-`n` parent chain. Nothing else: no splicing, no filtering. |

`Body` kinds: `System`, `User`, `Assistant` (text, thinking, optional `calls`), `Results` (map `call_id → Ok | Err | Skipped | Interrupted`), `Config` (full snapshot), `Custom { kind, data }`.

Derived, never stored: **draft** = nodes on `walk(target)` below `LCA(at, target)` created this step; **scratch** = nodes on no cursor's committed walk; **messages** = provider conversion of `walk(at)`.

Persisted: nodes, and one cursor-move record per commit `{cursor, from, to, at, opened_by?}`.

Every additional type needs a glossary entry and an ADR making the case. Default answer: no.

### Invariants

- **I1 Parent is context.** A node's parent chain is exactly what produced it. Any other relation is `copied_from` / `derived_from`.
- **I2 Immutability.** No node is modified after write. Edit = append sibling + retarget.
- **I3 Committed `at`.** A cursor's `at` is always a validated, committed node. Cursors open only at committed nodes.
- **I4 Calls are answered.** A node with `calls` has at most one committed child, and it is the `Results` node answering every call. Checked locally at append.
- **I5 Commit validates.** `validate(walk(target))` checks provider legality before `at := target`.
- **I6 Config follows the cursor.** A commit never changes effective config implicitly. If `config_at(target) != config_at(at)` and the draft contains no `Config` node, core appends a restating `Config` node (`by: core`).
- **I7 Reload is replay.** In-memory state is a pure function of persisted facts. No heuristics. Property test: `state == load(save(state))` over random op sequences, including multiple cursors, drafts, scratch, and detached roots.

### Step

```rust
enum Next { Wait, Sample, RunTools(Vec<CallId>) }

fn next(walk: &[Node]) -> Next {
    match walk.last().map(|n| &n.body) {
        Some(Body::User(_)) | Some(Body::Results(_))     => Next::Sample,
        Some(Body::Assistant(a)) if !a.calls.is_empty() => Next::RunTools(a.call_ids()),
        _                                               => Next::Wait,
    }
}
```

(Config/Custom at the tip: `next` looks at the nearest message-bearing ancestor. Pin this in model.md.)

Python driver, one step:

1. **Produce:** user input → append `User` under `at`; `Sample` → sample `walk(at)`, append `Assistant`; `RunTools` → run, append one `Results`. Set `target`.
2. **Hooks:** the chain for this step kind (`input`, `sample`, `tools`) runs in order. Each is `async (api, cursor) -> None` and may append anywhere (including detached roots), move `target`, or set `target = at` (refuse/abort/handled).
3. **Pause** if the frontend is not auto-stepping (debug, and every reload).
4. **Commit** (I5, I6), persist the move.

Abort = cancel the in-flight sample/tool, write `Interrupted`/error results as needed for I4, `target := at`.

Core is sans-IO except `tau-store`. No tokio in core; Python's asyncio owns all waiting.

### Events

One type, cursor-tagged: `node_appended`, `cursor_moved`, `stream_delta`. Observers are non-blocking. Every wire format (json lines, RPC, serve, TUI render) is a projection of this type. Streaming partials are never nodes.

### Cross-process

One `flock` lease per session file. Second process is read-only. Keep 0.15's lease semantics, minus the stale-holder record ambiguity: holder liveness is checked.

---

## 2. Rules for CLAUDE.md

1. Copy no code from τ 0.15. Read it only to learn behavior, then write the behavior as a test.
2. No new type, field, or module-level concept without a glossary entry; anything not in the glossary is suspect.
3. Banned identifiers and docstring words (lint in CI): `lane`, `suspension`, `suspend`, `responder`, `side_entry`, `incomplete`, `default_leaf`, `acting_cursor`, `head` (as a frontend), `frame`, `entry` (use `node`), `backend` (use `provider` or `frontend`), `fork` for anything in-session (use `branch`).
4. Tests go through the `Harness` protocol or the public Python API. No `unittest.mock.patch`, no `_private` access from tests (both linted). Fakes live at the effect boundary only: `FakeProvider` (scripted samples), `FakeTool`.
5. Each phase ends with a stop for review. Do not start the next phase without sign-off.
6. Diagrams are generated from code after Phase 3. Hand-drawn diagrams exist only in Phase 1.
7. Ratchet in CI from Phase 3: max function CC ≤ 15, no class over 25 methods, no file over 800 lines, banned-term count 0, I7 property test green.

---

## 3. Phases

### Phase 0: scaffold (no design content)
- Cargo workspace, maturin `pyproject.toml`, abi3 wheels. CI matrix: linux x86_64/aarch64, macOS arm64. Choose the Python floor after checking current PyO3 support for 3.14 and free-threaded builds.
- Hello-world: Rust function called from Python, round-trip a struct.
- clippy, rustfmt, ruff, pyright strict on `python/`, proptest wired.
- **Exit:** wheel installs into a clean venv next to `ffwf-tau` 0.15 and both import.

### Phase 1: design docs (no implementation)
- `glossary.md`: final term table from the design discussion (node, message, session, walk, context, cursor, at, target, draft, scratch, commit, step, next, sample, hook, event, abort, branch, fork, extension, tool, model, provider, frontend, lease). Each term: definition, Rust/Python identifier, and the 0.15 terms it retires.
- `model.md`: §1 of this plan expanded. Every invariant gets a numbered statement and the test that will enforce it.
- Hand-drawn diagrams: node/body kinds, step sequence, cursor commit with LCA, the scratch and detached-root examples, compaction branch-from-prefix.
- `decisions/`: one ADR per item in §4, with the recommended default.
- **Exit:** sign-off.

### Phase 2: behavior spec (no implementation)
- `tests/behavior/harness.py`: the `Harness` protocol, shaped as an in-process `tau serve` (model.md §9): a local frontend is serve with one client. Session create/load and `crash()` (drop memory, keep disk); `attach(client)` returns a client handle with input, go-to, step, abort, auto-step on/off, answer, decline, and that client's events; tree, cursor, label, and scratchpad snapshots; `open_cursor`.
- **`settle()`**: run until every cursor rests or holds only jobs a fake has not released. D11 makes this well defined, so no test sleeps.
- `FakeProvider` (scripted replies, including calls, errors, and stream deltas) and `FakeTool`, both with **gates**: a test holds a sample or a tool open, acts (esc, go-to, input), then releases it. A fake clock for cache TTLs and the 250 ms throttle.
- Scenarios mined from the as-built review §8/§9 and the 0.15 test suite (read for scenarios only; 0.15's design is not a template). Each test is tagged `keep`, `change` (cite the review row), `drop`, or `new`.
- `tests/oracle/`: adapter over τ 0.15, limited to `keep` tests: plain turn, tool batch, tool error, unknown tool, bad args, HTTP error. τ 0.15 is read from a read-only clone of `jmccardle/tau` at `v0.15.0`.
- Required scenario coverage:
  - plain turn; tool batch (one `Results` node, outcomes in call order); tool error; unknown tool; bad args; HTTP error; mid-stream error;
  - abort during sample (with and without parts); abort during tools (running calls `Interrupted`, `post_result` once);
  - crash during sample (the next step samples again); crash mid-batch (journal outcomes kept, finished calls not run again; idempotent unfinished calls run, others `Interrupted`); crash between a delivering record and the items' removal (delivered once);
  - input while busy waits as an item and is delivered by the next step, after the batch; input at rest; refused input (notice, item removed); abort leaves pending input pending; abort then send;
  - a frontend's go-to refused while a hook runs or a question is open; go-to on a busy cursor aborts, runs post hooks and `post_step`, then moves; a go-to wakes with no phase (auto-step off);
  - a question locks the cursor (input, go-to, step refused); esc does not close it; a single-client question is declined when that client detaches; abort during a `pre_tool` waiting for approval, then approval, gives `Interrupted`;
  - hook string returns: `pre_tool` → `Err(reason)`; a cursor hook halts the step; `pre_input` refuses;
  - a `pre_sample` hook moves the cursor (reminder, model choice); a `pre_reply` hook moves the cursor and the reply lands there; a `post_reply` hook moves the cursor and the tools phase looks there;
  - `cursor.moves(since_step=True)` shows other hooks' moves;
  - the throttle; an extension leaving its item in place does not spin;
  - agent-to-agent items wake an idle cursor; a sub-agent tool waits for its cursor to rest;
  - two cursors at one batch both run it; re-run by going to the `Assistant` node and stepping writes a sibling `Results`;
  - branch across a config node (the destination line's config applies); detached-root one-off call; labels set, moved, and removed across a reload; `max_steps` stops a "continue" loop; reload equals pre-save state (I7); a provider-illegal walk fails at the next sample (I5).
- **Exit:** `keep` green on oracle; everything else collected and failing for the right reason. Sign-off.

### Phase 3: Rust core
Three parts, each ending with a review stop.
- **3a, types and views.** `tau-core` types (`Node`, `Body`, `Results`, `Move`, `Label`, `Item`), `walk`, `next`, `config_at`, LCA, the append checks (V1, V2). Proptest I1–I5 over random op sequences.
- **3b, store.** `tau-store` per D12: one record log (length-prefixed, checksummed records), the scratchpad file rewritten atomically, the lease, load with the `delivers` reconciliation, a trait for JMFTS later. Proptest I7, including torn tails and crashes between a delivering record and removal.
- **3c, the step machine.** The phase loop as a sans-IO state machine (D11): jobs out, results in, frontend calls as inputs. Proptests over scripted results and frontend calls. Transition tables emitted as DOT; CI fails if the committed diagram differs. `tau-py` bindings: Python sees `Session`, `Cursor`, `Node`, handles, plain functions.
- **Exit:** property tests green; ratchet on.

### Phase 4: Python driver, one provider, built-in tools
- The driver performs the machine's jobs: hook calls, samples, tool runs, store writes, waits; it cancels what the machine says to cancel. `hooks.py` registers hooks; events.
- OpenAI-compatible provider first (covers local llama.cpp), including `Results` expansion and `partial_calls` rendering. Typed request settings from `Config`; no pass-through dicts.
- Typed config file schema (pydantic). Unknown keys are errors.
- Built-in tools as one `Tool` type.
- **Exit:** behavior suite green on the new implementation except tests that need later phases. Sign-off.

### Phase 5: extensions that prove the model
- **Compaction:** warm-cache in-context summary; cold summary on a detached root with a cheap model; branch from the deepest verbatim prefix with copied tail, `pre_sample` moving the cursor onto it; speculative candidate built on idle, used at the next input only if the cache has expired.
- **Prune:** post-expiry deterministic rewrite (old tool outputs, old thinking), branch from LCA.
- **Sub-agent:** a tool opens a cursor, adds the task as an item, waits for it to rest; the outcome carries `derived_from`.
- **Permissions:** `pre_tool` asks through `cursor.ask`; declined returns its own reason.
- **Debug:** frontend with auto-step off, stepping with `Session.step`. Shows walk, drafts, the scratchpad, and `next`.
- Provider gains `cache_state(walk) -> (warm_prefix_tokens, expires_at)`.
- **Exit:** each extension has behavior tests and no core change was needed. If one was, stop and write an ADR.

### Phase 6: frontends
`tau serve` first: many clients over the network, sessions and cursors multiplexed, status, questions, and scratchpads replayed on connect. Then print/json, REPL, RPC, TUI, each a client of the same model (a local frontend serves one client). One event projector per wire; all cursors visible on every wire.

### Phase 7: release
Anthropic and Google providers with `cache_state`; JMFTS store; one-way converter from 0.15 session files; docs site; `ffwf-tau` 1.0.

---

## 4. Decisions (recommended default in bold)

> Superseded by `docs/design/decisions/` (D1–D12) on Phase 1 sign-off. Kept as written for history.

| # | decision | options |
|---|---|---|
| D1 | `turn` as a concept | **drop; frontends count steps since last input** / keep as "steps from input to Wait" |
| D2 | input to a busy cursor | **core offers `append` (inject at commit) and `open_cursor`; queueing is a frontend buffer** |
| D3 | compaction | **extension; `walk` is a plain chain** / core node kind |
| D4 | resume at committed calls without results | **write `Interrupted` for all; per-tool `idempotent` flag allows re-run** / always re-run |
| D5 | config on branch | **follows the cursor (I6), full snapshots** / follows the branch |
| D6 | persist `target` | **no; crash mid-draft leaves draft as scratch** / yes |
| D7 | tool call + result | **calls on `Assistant`, one `Results` node per batch, I4** / single merged node |
| D8 | 0.15 extension compatibility | **none; document hook mapping** / shim |
| D9 | 0.15 sessions | **one-way converter** / native read |
| D10 | package name during development | **`tau_next`, renamed at 1.0** |
