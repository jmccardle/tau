# τ next — rewrite plan

> **Amendments (2026-10-25, Phase 0).** The plan below is kept as handed off.
> Where these notes conflict with it, these notes win.
>
> - **Release identity.** The rewrite ships as **τ 0.16.0, codename "Trusty"**,
>   not `ffwf-tau` 1.0. Development distribution: `ffwf-tau-next`, import
>   `tau_next` (D10). Final names at release are an open question for Phase 7.
> - **Python floor 3.12**, matching τ 0.15 (the oracle must import in the same
>   venv). PyO3 0.30.1 / maturin 1.15.
> - **"abi3 wheels"** holds only for GIL builds: one `cp312-abi3` wheel. Free-threaded
>   builds have no stable ABI before 3.15 (`abi3t`, PEP 803), so 3.14t ships a
>   version-specific `cp314t` wheel. τ 0.15 is tested on 3.14t/3.15t, so this
>   is not optional.
> - **0.15 import names** are `tau_llm`, `tau_agent_core`, `tau_coding_agent`,
>   `tau_jmfts`, `tau_meta`; there is no `tau` package to collide with.
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
- `tests/behavior/harness.py`: the `Harness` protocol. Session create/load, input, step, auto-step on/off, abort, open_cursor, tree snapshot, cursor snapshot, events.
- `FakeProvider` (scripted assistant nodes, including calls, errors, and stream deltas), `FakeTool`, a fake clock for cache-TTL tests.
- Scenarios mined from the as-built review §8/§9 and the 0.15 test suite (read for scenarios only). Each test is tagged `keep` (0.15 behavior to preserve), `change` (0.15 does X, new does Y; cite the review row), `drop`, or `new`.
- `tests/oracle/`: adapter over τ 0.15. `keep` tests must pass on it.
- Required scenario coverage: plain turn; tool batch; tool error; unknown tool; bad args; HTTP error; mid-stream error; abort during sample; abort during tool; crash during sample (resume samples again); crash after calls committed (resume writes `Interrupted`); hook edits a tool result; hook refuses input; hook drafts on scratch and retargets; two cursors at one node; branch across a config node (I6); detached-root one-off call; reload equals pre-save state (I7); provider-illegal draft rejected at commit (I4/I5).
- **Exit:** `keep` green on oracle; everything else collected and failing for the right reason. Sign-off.

### Phase 3: Rust core
- `tau-core`: types, `walk`, `next`, `validate`, `config_at` (nearest `Config` ancestor; snapshots), `commit` with I6, LCA.
- `tau-store`: JSONL node log + cursor-move log, versioned header, trait for JMFTS later.
- Proptest: I1–I7 over random op sequences.
- Transition table emitted as DOT; CI fails if the committed diagram differs.
- `tau-py`: bindings for the above. Python sees `Session`, `Cursor`, `Node`, plain functions.
- **Exit:** property tests green; ratchet on.

### Phase 4: Python driver, one provider, built-in tools
- `driver.py` step loop; `hooks.py` chain; events.
- OpenAI-compatible provider first (covers local llama.cpp). Typed request settings from `Config`; no pass-through dicts.
- Typed config file schema (pydantic). Unknown keys are errors.
- Built-in tools as one `Tool` type.
- **Exit:** behavior suite green on the new implementation except tests that need later phases. Sign-off.

### Phase 5: extensions that prove the model
- **Compaction:** warm-cache in-context summary on scratch under `at`; cold summary on a detached root with a cheap model; branch from the deepest verbatim prefix with copied tail; speculative candidate built on idle, committed at next input only if the cache has expired.
- **Prune:** post-expiry deterministic rewrite (old tool outputs, old thinking), branch from LCA.
- **Sub-agent:** cursor opened by a tool at a committed node; result returned as the tool result with `derived_from`.
- **Debug:** frontend with auto-step off. Shows walk, draft, scratch, and `next`.
- Provider gains `cache_state(walk) -> (warm_prefix_tokens, expires_at)`.
- **Exit:** each extension has behavior tests and no core change was needed. If one was, stop and write an ADR.

### Phase 6: frontends
Order: print/json, REPL, RPC, TUI, serve. One event projector per wire; all cursors visible on every wire.

### Phase 7: release
Anthropic and Google providers with `cache_state`; JMFTS store; one-way converter from 0.15 session files; docs site; `ffwf-tau` 1.0.

---

## 4. Decisions (recommended default in bold)

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
