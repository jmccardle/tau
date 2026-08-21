# Workstream crosswalk: W / G / JMFTS-phase / llama.cpp-fork

Status snapshot: **2026-07-15** (debts #1–#4 and JMFTS CR-4 discharged; see the debt list).

The constrained-decoding + JMFTS-backing-store effort is tracked under **four
different naming vocabularies**, spread across four documents in three repos. This
was a real source of confusion ("what are the W and G series? weren't the llama.cpp
targets in turboquant?"). This doc is the single crosswalk: what each series is,
where its source-of-truth doc lives, how they map onto each other, and — audited
against the code on 2026-07-14 — what is actually built.

Rule: **this file is a pointer + status index, not a spec.** Each series' own doc
stays authoritative for *content*; this file only reconciles *names and status*.
When status here disagrees with a plan doc's header, this file is newer (the plan
headers are known-stale — see "Doc drift" below).

---

## The four vocabularies at a glance

| Series | What it is | Source-of-truth doc | Repo |
|---|---|---|---|
| **W0–W15** | The **merged execution schedule** — the G-series and the JMFTS phases interleaved into one dependency-ordered line. This is the list we actually built against. | **this doc** (the W-table below). The original merged plan `goofy-wobbling-knuth.md` was delivered in full and **culled 2026-07-14**; its schedule now lives here. | tau |
| **G0–G7** | Constrained-decoding & KV-branch-readiness targets (τ-side). | `docs/CONSTRAINED-GEN-AND-BRANCHING-PLAN.md` (appendix table) | tau |
| **JMFTS Phases 1–6 + C1/C2** | JMFTS-as-session-backend integration; C1/C2 are two τ-core primitives that ride along. | `docs/JMFTS-INTEGRATION-PLAN.md` §7 | tau |
| **Fork Phases A–D** (+ KV paths 0–3) | The **llama.cpp server enhancement** work (jump-forward decoding; KV branching). Prerequisite for G6/G7 only. | `JUMP_FORWARD_ROADMAP.md`, `GRAMMAR_DECODING_RECON.md`, `KV_BRANCHING_RECON.md` | turboquant_experiments |

**Reading order:** G and the JMFTS phases are the two *source* backlogs. **W is the
unified schedule that merges them** (the merge exists because G3 secretly depends on
JMFTS's C1 — neither source doc owned that edge). The fork phases are a separate,
server-side track that only two τ targets (G6, G7) depend on.

---

## W-series — the schedule we built (all committed)

Every W item is committed on `master`. Crosswalk + audited status:

| W | maps to | commit | status |
|---|---|---|---|
| W0 | config consolidation | `63f686a` | ✅ done |
| W1 | live grammar spike (no τ code) | `5ae3297` | ✅ done (`scripts/w1_grammar_spike.py`) |
| W2 | **G0** | `a589e45` | ✅ done |
| W3 | **G1** | `d9fe6a2` | ✅ done |
| W4 | **G2** | `309c5be` | ✅ done |
| W5 | SessionLog contract suite + shared cursor algebra | `c7e917e` | ✅ done |
| W6 | **C1** (`ctx.complete()`) | `11ca571` | ✅ consolidated (Piece A, `completion.py`) — debt #1 |
| W7 | **G3** (the demo) | `11ca571` | ✅ done (`examples/60_retrieval_review.py`) |
| W8 | **G4** telemetry | `2a2702a` | ✅ capture + readout (TUI / `--mode json` / describe echo) |
| W9 | **G5** prefix-stability tests | `2a2702a` | ✅ done |
| W10 | catalog seam | `2a2702a` | ✅ done |
| W11 | **JMFTS Phase 2** (store) | `65f17e9` | ✅ done |
| W12 | **JMFTS Phase 3** (`--store` wiring, TUI/importer) | `b359404` | ✅ done |
| W13 | **JMFTS Phase 4** (enrich) | `b865a42` | ✅ done |
| W14 | **C2** branch sub-agents | `b359404` | ✅ done |
| W15 | **JMFTS Phase 5** (jmfts tools) | `b865a42` | ✅ done |

(hardening pass `2fdc92a` sits between W7 and W8: closed 4 constraint bypasses + 2
silent-override hazards + the unrun demo.)

---

## G-series — constrained decoding (τ-side)

Source table: `docs/CONSTRAINED-GEN-AND-BRANCHING-PLAN.md:590-597`.

| G | target | built via | status (2026-07-14) |
|---|---|---|---|
| G0 | `Model.grammar_dialect` / `extra_body` / `server_features` | W2 | ✅ |
| G1 | `DecodeConstraints` + provider wire mapping + gating | W3 | ✅ |
| G2 | `tau_llm/grammar.py` + `ConstraintViolation` + verification | W4 | ✅ |
| G3 | constrained `ctx.complete()` (N concurrent verified verdicts) | W7 | ✅ |
| G4 | telemetry: `Usage.extra ← timings`, repair counter, **TUI/json readout** | W8 | ✅ capture (`types.py:92`, `openai.py:1163/953`) + readout: TUI exchange summary via `format_telemetry`, `--mode json` `usage.extra` (test-locked), and `describe()` echoed from `ctx.complete()` |
| G6 | integration bench vs stock **+ fork** servers | — | ❌ blocked on the fork (needs `n_ff_total`) |
| G7 | branch hints / LMTP provider | — | ❌ reserved (needs the server API to exist) |

**Everything G0–G5 runs against stock llama.cpp master.** Only G6/G7 need the fork
(plan doc line 599).

---

## JMFTS phases + C1/C2

Source table: `docs/JMFTS-INTEGRATION-PLAN.md:381-390`.

| Phase | where | deliverable | built via | status |
|---|---|---|---|---|
| 1 | jmfts | CR-1 (position), CR-2 (structured filter), CR-6 (presentations) | — | ⚠️ **partial** — CR-1 + CR-6 + **CR-4 (auth)** done; CR-2 not (see JMFTS-side below) |
| 2 | τ | `tau-jmfts` package (`JmftsClient`, `JmftsSessionLog`, config/CLI) | W11 | ✅ (contract suite green over all 3 stores) |
| 3 | τ | catalog seam; TUI picker/resume/fork; JSONL⇄JMFTS importer | W10 + W12 | ✅ |
| 4 | both | `enrich.py` (deferred embedding/indexing/projections) | W13 | ✅ (τ side) |
| 5 | τ | `tools.py` agent tools + context-injection hooks | W15 | ✅ |
| 6 | both | ontology integration; CR-5 path summarization; **CR-4 auth enabled** | — | ❌ not started |
| C1 | τ core | `ctx.complete()` through the model registry | W6 | ✅ consolidated (Piece A); Piece B (cheap-model routing) tracked — debt #1 |
| C2 | τ core | branch sub-agents / multi-cursor entry log | W14 | ✅ (provider-pool gate cleared, `docs/PROVIDER-LIFETIME.md` §8) |

---

## llama.cpp server track (turboquant_experiments)

**Not W or G.** This is the server-fork work; it has its own phase letters and gates
only G6/G7. Nothing here is started — the docs are recon + a verified roadmap.

- **`JUMP_FORWARD_ROADMAP.md`** — jump-forward decoding in llama-server, verified
  against `repos/upstream-llama-cpp` master. Four phases:
  - **Phase A** — expose forced (ff) tokens through the sampler stack (~40 LOC)
  - **Phase B** — C++ test for Phase A before touching the server (~60 LOC)
  - **Phase C** — server integration (~100–150 LOC, `server-context.cpp`)
  - **Phase D** — metrics & demo instrumentation (~30 LOC) → emits `n_ff_total`
  Phase D's forced-token count is the **same number G4 needs** and the same number
  the silent-override upstream report proposes as a grammar-agnostic integrity
  check. This is the load-bearing dependency: until Phase D lands, G4 can capture
  `timings` but never `n_ff_total` (confirmed empty on stock in the W1 spike).
- **`GRAMMAR_DECODING_RECON.md`** — the recon Phase A–D is a companion to (build
  state, effort assessment, LMQL/LMTP evaluation, demo sequence).
- **`KV_BRANCHING_RECON.md`** — KV-cache prefix-reuse paths **0** (free today) / **1**
  (under-the-hood, API-preserving, ~150–250 LOC) / **2** (explicit fork API or LMTP)
  / **3** (radix-tree, skip). This is what G5's prefix-stability work prepares τ for.

### llama.cpp bug/PR docs (these live in the **tau** repo, not turboquant)

- **`docs/lazy-grammar-thinking-pr.md`** — a complete, tested upstream PR (branch
  `pr/lazy-grammar-thinking`, `common/sampling.cpp` +21/−4) that *fixes* the
  thinking-vs-grammar bug τ currently works around with `enable_thinking: False`.
  **Untracked as of 2026-07-14** — needs a commit decision. Supersedes the diagnosis
  in `REASONING-VS-CONSTRAINED-DECODING.md`.
- **`docs/UPSTREAM-LLAMACPP-SILENT-OVERRIDES.md`** — draft issue for the two
  silent-override bugs (`response_format`+`tools` → 200 with tools dropped;
  `grammar`+`response_format` → 200 with grammar discarded). Not yet filed.
- **`docs/REASONING-VS-CONSTRAINED-DECODING.md`** — the (now superseded) grammar_lazy
  diagnosis.

---

## The two dependency edges that cross vocabularies

1. **G3 → C1.** Constrained generation is delivered *through* `ctx.complete()`. That
   is why W merges the two backlogs and why W6 (C1) precedes W7 (G3).
2. **G6, G7 → Fork Phases A–D.** The only τ targets gated on the turboquant server
   work. Everything else ships on stock llama.cpp master.

---

## Debts left along the way (audited 2026-07-14)

The W0–W15 line is complete, but these were stepped around:

1. ~~**C1 is additive, not the consolidation the plan wanted.**~~ **RESOLVED (Piece A).**
   Extracted a single resolver-routed primitive `resolved_complete()` in
   `tau_agent_core/completion.py`; `ctx.complete()`, `summarize_branch`, and both
   compaction summaries now sit on it — no more `complete_simple` bypass to private
   `session._model`/`_api_key`. Behavior-preserving (model still defaults to the
   session's own). **Piece B remains open:** actually *routing* compaction to a
   cheap/constrained model needs a config key for the compaction model — a τ-only
   addition with no pi precedent (pi reuses the agent model). Tracked, not scheduled.
2. ~~**G4 observability consumer-side is empty.**~~ **RESOLVED.** The TUI exchange
   summary now renders t/s / repairs / forced-share (shared `format_telemetry`), the
   `--mode json` stream's `usage.extra` carry is locked by test, and
   `DecodeConstraints.describe()` is wired into `ctx.complete()` via
   `ExtensionUI.emit_constraints` (its first non-test caller). Two §3.3 sub-items stay
   blocked-not-stubbed: the main loop applies no constraints (an echo there would be a
   `{"kind":"none"}` placeholder), and constrained completions don't persist an entry.
3. ~~**Doc drift.**~~ **RESOLVED.** Both plan-doc headers are reconciled against this
   file: `CONSTRAINED-GEN-AND-BRANCHING-PLAN.md` now reflects G4 as fully built, and
   `JMFTS-INTEGRATION-PLAN.md` reads PARTIALLY DELIVERED (fixed 2026-07-14).
4. ~~**`lazy-grammar-thinking-pr.md` is uncommitted**, and τ's `enable_thinking: False`
   blanket in `_apply_constraints` is set *before* the `json_schema` branch.~~
   **RESOLVED.** The PR was committed (`b880826`); the blanket was then narrowed so it
   applies only to the grammar/choices paths — a `json_schema`/`response_format` call
   now keeps thinking on (the constraint is a template-built, reasoning-aware grammar,
   upstream #20223). Pinned by `TestConstrainedCallsDoNotThink`. The grammar-path
   suppression stays load-bearing until the upstream PR lands server-side.

### JMFTS-side (other repo, HEAD `17180d9`)

- ~~**CR-4 (auth + CORS) — NOT done.**~~ **DONE (both sides).** Server: a shared-bearer
  app-level dependency + CORS locked to `JMFTS_CORS_ORIGINS` (jmfts `17180d9`); a
  missing token generates and prints an ephemeral one at startup (never allow-all).
  Client: the τ `JmftsClient` already sent `Bearer`; `build_jmfts_client` /
  `ext/tools.py` now pass a token from `session_store.token` / `$JMFTS_API_TOKEN`, and
  a 401 raises `StoreError` (no silent allow). The **operational cutover** — generate
  the secret, put it in every τ config, flip server + clients together — is a
  deployment step, not code.
- **CR-1 (position column) — DONE** (jmfts `d70cc57`, τ client `6fe4c5c`).
  Sparse nullable `position INTEGER`; sibling/child listings now order by
  `position ASC NULLS LAST, created_at ASC, id ASC` (`get_children`,
  `get_siblings`, the `/view` child bundle). Opt-in via `sequential` on
  `create()`/`POST /documents`: `None` inherits the parent's ordered-ness
  (propagates down, off by default), `True` auto-numbers birth order, `False`
  forces NULL; sequential on a root → HTTP 400. Migration 004 applied on midlife;
  contract test (9) + live smoke green. **Deferred to CR-1b:** depth-first
  tree-traversal order for `get_subtree`/`depth=-1`, and PATCH reordering.
- **CR-2 (structured_content search filter) — NOT done.** CR-6 (presentations) done.
- **KNOWN-DEFECTS.md: all four silent-failure bugs RESOLVED** (jmfts `280d650`):
  embed now 400s on over-window text instead of truncating; `chunk_text` caps every
  strategy and bounds the merge; `index_document` is idempotent (safe incremental
  reindex); the chunker hard-splits whitespace-free words. `tau_jmfts/ext/enrich.py`'s
  client-side workarounds have been **reverted** — enrich now indexes incrementally via
  the idempotent `index-document`, drops the strategy/merge pins, and no longer reports
  "unembeddable" chunks; `jmfts_ingest` gates over-window content through the chunker.
