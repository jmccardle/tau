# JMFTS as a τ Session Backend — Integration Plan

Status: **PARTIALLY DELIVERED** (last audited 2026-07-14). The τ-side core is shipped:
**Phase 2** (`tau-jmfts` package — `JmftsClient`, `JmftsSessionLog`, config/CLI),
**Phase 3** (catalog seam + TUI + importer), **Phase 4** (enrich), **Phase 5** (agent
tools), and both core primitives **C1** (`ctx.complete()`) and **C2** (branch
sub-agents) are all committed on `master`. **Not done:** **Phase 1** is partial
(CR-6, **CR-4 auth/CORS**, and **CR-1 position** landed; only CR-2 structured
filter remains), and **Phase 6** (ontology, CR-5 path summarization,
CR-4 enablement) has not started. C1 landed *additive-only* — three internal callers
still bypass its model resolver (see the crosswalk's debt list).
Companion repos: this one (τ) and `~/Development/jmfts` (JMFTS). **We own both**, so
this plan includes change requests against JMFTS where the clean fix belongs there.

> **Status index:** delivery state of every JMFTS/W/G/fork item and the outstanding
> debts (incl. the JMFTS-side CRs) live in one place:
> **`docs/WORKSTREAM-CROSSWALK.md`** — authoritative for *status*; this doc stays
> authoritative for *design*. The four KNOWN-DEFECTS silent-failure bugs are now
> **resolved server-side** (jmfts `280d650`) and τ's client-side workarounds reverted;
> `docs/KNOWN-DEFECTS.md` is kept as historical record.

## 0. Objective

Make JMFTS (John McCardle's Fusion Tree Search) an optional, first-class backing
store for τ conversations, and expose JMFTS capabilities (search, ingest, ontology)
to extensions and agent tools. A conversation stops being a JSONL file and becomes
a JMFTS document subtree: a root node with `usetype="tau:conversation"` carrying
hostname/cwd in `structured_content`, with every τ tree entry a descendant document.

Non-goals / constraints:

- **Optional.** τ with no JMFTS configured behaves exactly as today (file `Session`
  / `InMemorySessionLog`). No JMFTS import at module scope on the default path.
- **Fail-Early.** No silent fallback to file storage when JMFTS is configured but
  unreachable — a failed append fails the turn, loudly.
- **Root discipline, foreign-node tolerance.** τ only *opens* conversations whose
  root is `tau:conversation`, but the subtree may contain documents of arbitrary
  usetypes (RAPTOR summaries, extension-ingested references, future JMFTS features).
  τ walks through them without crashing and without leaking them into model input.
- **Tree-as-truth is untouched.** The JMFTS backend is a different durability layer
  under the same append-only entry algebra; `ConversationTree` remains the single
  read-time fold (SESSION-TREE-IMPLEMENTATION.md §4.5: same entries → same tree).

## 1. What research established (2026-07-12)

### τ side

- The seam already exists: **`SessionLog`** (`tau-agent-core/src/tau_agent_core/session_log.py:38`),
  a `runtime_checkable` structural Protocol: `id`, `cursor`, `entries()`,
  `append_message`, `append_custom_message`, `append_custom_entry`,
  `append_compaction`, `append_navigate`, `append_branch_summary`. `AgentSession`
  depends only on it; `TauBackend.bind_session_log` (`backends.py:295`) injects it.
  SESSION-TREE-IMPLEMENTATION.md §4 "Decision 4 option B" explicitly designates
  this as the DB seam — a database-backed store satisfies the same surface.
- Entries are plain dicts `{type, id, parentId, timestamp, ...payload}`; the tree
  is encoded purely by `parentId` chaining; branching = navigate + append; the
  cursor is itself persisted as `navigate` entries; **nothing is ever rewritten**
  (compaction is an appended splice anchor, not tree surgery).
- **Unknown entry types are already tolerated**: `Session.load` does no schema
  validation; `ConversationTree.context_for` (`conversation_tree.py:159-177`) is a
  whitelist (`message`, `customMessage`, `compaction`, `branch_summary` produce
  messages; everything else is walked through silently); `tree()` renders every
  kind. The only hard requirements per entry: `id` (str) and `parentId` (str|None).
- Model/thinking/name changes (`append_model_change` etc.) are deliberately off
  the Protocol — the TUI/headless call them on the concrete `Session`. A JMFTS log
  must provide them too to be a drop-in on the live path.
- Discovery/resume is currently file-path-shaped (`list_sessions`, `SessionInfo`,
  `Session.load(path)`, headless `_resolve_session_ref`) — this is the one place
  that needs a real abstraction added (§5, Phase 3).

### JMFTS side

- Documents: `id` (serial int), `parent_id` (FK), `title`, `content`,
  **`structured_content` JSONB (GIN-indexed)**, `usetype` (free-form VARCHAR(100) —
  `tau:*` needs no registration for `POST /documents`), materialized `path` (JSONB
  ancestor array, GIN-indexed) + `depth`, timestamps, `content_hash`, `embed`
  vector. (`jmfts: schema.sql:12-36`, `models/document.py`)
- **No sibling-order column** — children sort by `created_at` (`repositories/document.py:210`).
  See CR-1.
- `POST /documents` accepts `parent_id` + `structured_content` + `auto_embed`
  (default true → **synchronous local transformer inference per write**; pass
  `false` and embed later via `POST /documents/{id}/embed`). `PATCH` cannot change
  `parent_id` (no reparent API — irrelevant for τ, which never reparents). `DELETE`
  cascades over the subtree via the ORM. No batch create (CR-3). No auth (CR-4).
  No optimistic locking — single writer per conversation is a hard rule.
- `GET /documents?usetype=&parent_id=&title_prefix=&limit=&offset=` exists →
  conversation discovery works today; `structured_content` filtering is client-side
  until CR-2. `GET /documents/{id}/subtree?max_depth=` (nullable → unbounded)
  fetches an arbitrarily deep subtree in one `path @>` GIN query — a 1000-entry
  message chain loads in one round trip.
- `POST /conversations/ingest` is a **batch pipeline** (root + flat children,
  hardcoded `usetype="conversation"`/`"chunk"`, optional RAPTOR + fact extraction).
  Useful as a reference and for one-shot analysis passes; **not** the live write
  path and not τ's mapping.
- Links (`link_type` edges), triples (predicates + temporal validity +
  invalidate/supersede), search scoping (`parent_id` = whole-subtree containment,
  `usetype` filters, named indexes with roots, search-contexts presets,
  `/search/auto`, `/search/synthesize`), RAPTOR/segment/chunk/extract-facts all
  operate on document ids — every τ node is automatically addressable by all of it.

## 2. The mapping: τ entry tree ↔ JMFTS document subtree

**Topology is mirrored 1:1.** τ's `parentId` chain becomes JMFTS `parent_id`. The
conversation root document is the session header; every entry is a document whose
parent is its τ parent (first entry parents to the root document itself).

We considered and **rejected** the `/conversations/ingest` shape (flat children
under the root, τ topology stashed in metadata): it reads better to JMFTS's
summarizer, but it hides branch structure from the graph/links/subtree machinery,
makes "extend from any point as a new subtree" unnatural, and creates two
topologies to keep consistent. Mirroring means the JMFTS tree *is* the τ tree:
scoping a search to a branch node searches exactly that branch's descendants, an
extension can hang a reference document off any message, and a τ conversation can
itself be planted under any existing JMFTS document (`parent_id` of the root).
The cost — JMFTS parent/child reads as "follows" rather than "contains" inside a
conversation — is addressed by CR-5 (path summarization) and the enrichment
extension (§6), not by contorting the storage shape.

### 2.1 Per-document mapping

| JMFTS field | Conversation root | Entry document |
|---|---|---|
| `usetype` | `tau:conversation` | `"tau:" + entry type` (`tau:message`, `tau:customMessage`, `tau:customEntry`, `tau:compaction`, `tau:branch_summary`, `tau:navigate`, `tau:model_change`, `tau:thinking_change`, `tau:session_info`) |
| `parent_id` | `null` or any chosen host document | τ `parentId` (root doc id for first entry) |
| `title` | session display title (mutable via `session_info`) | short label, e.g. `"user — 0007"`, `"assistant — 0008"`, `"compaction — 0042"` |
| `content` | empty (or lazily-maintained transcript projection — enrichment's job, not the hot path's) | **searchable text projection** of the entry: concatenated text blocks of the message, the compaction/branch summary text, empty for navigate/config kinds |
| `structured_content` | `{"tau": {header…}}` see §2.2 | `{"tau": {entry…}, "seq": n}` see §2.3 |
| `auto_embed` | `false` | `false` (enrichment embeds later, §6) |

**`structured_content.tau` is authoritative; `title`/`content` are projections.**
The round-trip contract: `entries()` returns exactly
`{**doc.structured_content["tau"], "id": str(doc.id), "parentId": str(doc.parent_id) or None}` —
byte-shape-identical to a file `Session` entry, so `ConversationTree` folds it
unchanged. If a projection and `structured_content.tau` ever disagree, `tau` wins.

### 2.2 Root document (the header)

```jsonc
{
  "tau": {
    "type": "session", "version": 1,
    "id": "<uuid4-hex>",            // stable τ session identity (SessionLog.id)
    "timestamp": "<iso>",
    "cwd": "/home/john/Development/agent-harness-py",
    "hostname": "<socket.gethostname()>",   // NEW field, per the objective
    "parent": null                   // fork-source session uuid, as today
  }
}
```

Two identities coexist deliberately: the **JMFTS doc id** (locator within one
JMFTS instance; what `--session` resolves against, what search returns) and the
**τ session uuid** (portable identity that survives export/import/fork lineage).
`JmftsSessionLog.id` returns the uuid, honoring the Protocol's "never a path" rule.

### 2.3 Entry ids, ordering, and the cursor

- **Entry `id` = `str(jmfts_doc_id)`.** The append POSTs first and adopts the
  returned id. Entry ids are opaque strings everywhere in τ (`ConversationTree`
  walks by dict lookup), so numeric strings are fine, and `navigate.targetId` /
  `compaction.firstKeptId` references are automatically JMFTS-resolvable — every
  cross-reference in the τ tree is a real document id that links, triples, and
  search results can point at with no translation table.
- **Append order** (`entries()` "load order", cursor resolution): ordered by
  `structured_content.seq`, a per-conversation monotone counter maintained by the
  writer (initialized to `max(seq)+1` on load). Doc-id order agrees with it under
  the single-writer rule and serves as an integrity cross-check — load fails
  loudly (Fail-Early) if they disagree, since that means a second writer touched
  the tree. Sibling display order additionally gets a real column via CR-1.
- **Cursor** stays exactly what it is today: resolved from the last entry
  (latest-wins `navigate`), per `Session._resolve_cursor` semantics. No mutable
  "cursor" field on the root — that would reintroduce last-write-wins races and
  break append-only.
  - **…but resolved over τ's OWN entries only** — foreign documents (§2.4) are excluded.
    Discovered while implementing W11; this bullet and §2.4 as originally written
    contradict each other, and the naive reading is a live bug. A foreign document can
    be created **out-of-band by a different actor** (enrichment attaching a RAPTOR
    summary, an extension hanging a reference off a message), which in the common case
    gives it the *highest doc id in the subtree* — it is written *after* the entry it
    annotates. Feeding the combined list to `resolve_cursor` would then silently adopt
    that foreign node as the cursor on the next `load`: a foreign write moving τ's own
    tip, with no error, changing what the model sees on the next turn. Filtering foreign
    entries out before resolving keeps the real invariant — **the cursor moves only on a
    τ append** — true across a reload, exactly as it already is on the live path (a
    foreign `POST /documents` never goes through τ's leaf at all). Regression-tested in
    `tau-jmfts/tests`.

### 2.4 Foreign documents inside the conversation subtree

On load, any document in the subtree whose usetype is not `tau:*` (or whose
`structured_content.tau` is absent) is surfaced as a synthesized entry:

```jsonc
{ "type": "jmfts:document", "id": "<doc id>", "parentId": "<parent doc id>",
  "timestamp": "<created_at>", "usetype": "<original usetype>", "title": "<title>" }
```

Consequences, all of which fall out of existing τ behavior (verified):

- `ConversationTree` **walks through it** (`parentId` traversal is kind-agnostic)
  and the `context_for` whitelist **drops it from model input** — a RAPTOR summary
  node or an extension-attached reference sitting on the active path costs nothing
  and breaks nothing.
- It appears in `tree()` (the TUI tree browser) as a node with its usetype as kind.
- τ can `append_navigate` to it and extend from it — "extend from any point as a
  new subtree of agent-generated content" is just the existing branch mechanism.
- To make a JMFTS document **model-visible**, an extension injects it explicitly
  (durable `customMessage` carrying the doc's content/reference, or a
  `before_agent_start` injection). The core whitelist is *not* widened — model
  input stays an inspectable fold of explicit entry kinds (tree-as-truth: no
  hidden channels).

τ never opens an arbitrary document as a conversation: `load(ref)` verifies the
root's usetype is `tau:conversation` and its `structured_content.tau` header is
well-formed, else raises.

## 3. Architecture: a new optional package, `tau-jmfts`

```
tau-jmfts/                       # fourth monorepo package, src-layout
  src/tau_jmfts/
    client.py        # JmftsClient: thin sync httpx wrapper over the REST API
                     #   (documents CRUD, subtree, search/*, indexes, triples, embed)
    store.py         # JmftsSessionLog: SessionLog Protocol + the concrete-Session
                     #   surface the TUI touches (append_model_change/thinking_change/
                     #   session_info, name, context, messages, fork)
    catalog.py       # discovery: list/most-recent/resolve-ref → SessionInfo-shaped
    importer.py      # JSONL session file ⇄ JMFTS subtree (lossless round-trip)
    ext/
      tools.py       # extension: agent-facing jmfts_search / jmfts_read / jmfts_ingest
      enrich.py      # extension: deferred embedding, indexing, presentations
```

- Depends on `tau-agent-core` (Protocol, `ConversationTree`) and `httpx`. Neither
  `tau-agent-core` nor `tau-coding-agent` depends on it — τ resolves the backend
  lazily by import at session-creation time and raises `tau-jmfts not installed`
  if configured but absent (Fail-Early, no degraded mode).
- **Why not "just an extension"?** Extensions cannot swap persistence (verified:
  no such hook; they only append through the API). The *store* must be injected at
  the `SessionLog` seam by cli/headless/TUI wiring; the *capabilities* (tools,
  hooks, enrichment) are ordinary extensions shipped in the same package.
- Sync HTTP inside the async loop matches the existing blocking-file-write
  pattern (`_persist_entry` does blocking `open/write` today). LAN-local
  single-row inserts with `auto_embed=false` are low-millisecond. Timeouts are
  short and errors propagate — a dead JMFTS fails the append, which fails the
  turn, which is the correct behavior for a configured-but-broken store.

### 3.1 Configuration & CLI

`~/.tau/config.json` (the reserved-but-unwired seam from SESSION-TREE-IMPLEMENTATION.md §4.6 —
"no store config until a real backend exists"; it now does):

```jsonc
{
  "session_store": {
    "backend": "jmfts",                       // default when absent: "file"
    "url": "http://localhost:8100",           // or $JMFTS_API_URL
    "token": null,                            // shared-bearer token (CR-4); or $JMFTS_API_TOKEN. No default — omit only for an unauth'd server
    "parent_id": null,                        // optional: host doc for new conversation roots
    "index": "tau"                            // optional: BM25 index to register roots into (enrichment)
  }
}
```

The server (CR-4) gates every endpoint behind an `Authorization: Bearer <token>`
shared secret. Set `session_store.token` (or the `$JMFTS_API_TOKEN` environment
variable) to that secret; the client sends it on every request. There is **no
default token** — against an auth'd server a missing token 401s loudly at the
startup health check (a clear "set a token" `StoreError`), never a silent
degraded mode.

CLI: `--store file|jmfts` per-run override; `--session <ref>` accepts a JMFTS doc
id or a τ uuid(-prefix) when the jmfts backend is active. Startup performs the
health check (`GET /`) once and fails loudly if the configured store is unreachable.

**Scoped to runs that will write, since Tectum's prototyping report.** `tau -p
--no-session` exited 2 at startup against an unreachable store it had already
been told not to write to — a run refused over a dependency it does not have.
`--no-session` (in `--print` and in `--mode rpc` alike) now builds the same
configured catalog without the `GET /`: every configuration error still raises
(missing package, no URL, non-string URL/token, unknown backend name), and only
the network contact is deferred. `create_ephemeral` is in-memory under both
stores, so an ephemeral run never reaches the server at all. `--mode rpc` keeps
`list_sessions`/`switch_session`/`fork`/`new_session {"persist": true}`
reachable over the wire under the flag, and those meet an unreachable store as
a loud failure on that request rather than at startup. Nothing is retried or
degraded; the store is simply not consulted until something needs it.

### 3.2 Write path (hot)

Per `append_*`: build the entry dict (same algebra as `InMemorySessionLog._append`),
`POST /documents` with `auto_embed=false`, `usetype="tau:<kind>"`,
`parent_id=<cursor doc id>`, `structured_content={"tau": entry_sans_ids, "seq": n}`,
adopt the returned id, advance the local cursor. One insert per entry, exactly the
granularity of today's per-message-completion file appends (streaming deltas were
never persisted per-delta). `navigate`/config kinds carry empty `content` (JMFTS
skips embedding ≤10 chars anyway). Single-writer per conversation tree is a
documented invariant; the seq/doc-id agreement check (§2.3) enforces it at load.

### 3.3 Read path

`load(ref)`: resolve root (doc id, or uuid → CR-2 / client-side scan), verify
usetype + header, `GET /documents/{root}/subtree?max_depth=null` (one query),
partition into τ entries (reconstructed verbatim from `structured_content.tau`)
and foreign nodes (synthesized `jmfts:document` entries), sort by `seq`
(foreign nodes interleave by `created_at` against their neighbors — their exact
list position is irrelevant to the fold, which walks by `parentId`), resolve the
cursor, hand the list to `ConversationTree`. From that point τ is unchanged.

### 3.4 Fork, compaction, deletion

- **Fork** = create a new root (header `parent` = source uuid) and bulk-copy the
  source entries as new documents preserving topology — semantics identical to
  the file `Session.fork` full-copy. Client-side loop initially; CR-3 makes it one
  request. (A zero-copy fork that shares the prefix subtree is rejected: it would
  make two conversations' trees overlap, breaking the root-discipline rule and
  delete semantics.)
- **Compaction / branch summaries** append documents like everything else — and
  because their `content` projection is the summary text, τ's own summaries become
  first-class searchable JMFTS nodes for free. This is the first concrete payoff
  of the integration: compaction summaries, embedded and indexed, are exactly the
  "hierarchical summary above the leaves" JMFTS trees want.
- **Delete conversation** = `DELETE /documents/{root}` (ORM cascade covers the
  subtree, links, token embeddings). Surfaced in the TUI session picker only after
  Phase 3.

## 4. Change requests against JMFTS (we own both projects)

Ranked; CR-1/CR-2 unblock polish, none block Phase 2's core.

- **CR-1 — Explicit sibling ordering (requested).** Add nullable
  `position INTEGER` to `documents`; ordering contract everywhere children/siblings
  are listed becomes `ORDER BY position ASC NULLS LAST, created_at ASC, id ASC`;
  accepted on `POST /documents` and `PATCH`. `created_at` sorting has no tiebreak
  and sub-millisecond inserts can tie — this fixes ordering for *all* JMFTS use
  cases (document sections included), not just τ. τ sets `position` = birth order
  among siblings (branch creation order). Migration: backfill NULL (legacy order
  preserved by the NULLS LAST + created_at fallback).
- **CR-2 — `structured_content` containment filter on `GET /documents`**, e.g.
  `?structured_filter={"tau":{"cwd":"/home/john/..."}}` compiled to JSONB `@>`
  (the GIN index already exists, `schema.sql:202`). Gives server-side
  cwd/hostname-scoped conversation discovery; until then the catalog filters
  client-side over `?usetype=tau:conversation`.
- **CR-3 — Batch create**: `POST /documents/batch` taking
  `[{ref, parent_ref|parent_id, ...DocumentCreate}]` with intra-batch `ref`
  linking, one transaction, `auto_embed=false`-friendly. Wanted by the importer
  (thousand-entry sessions = one request instead of a thousand) and fork.
- **CR-4 — Optional bearer-token auth** (single shared token via env var, honored
  by a FastAPI dependency; CORS tightened). τ conversations carry source code and
  secrets; the current API is unauthenticated with `allow_origins=["*"]`. Should
  land before real conversations are stored on a shared-network instance.
- **CR-5 — Path/branch summarization** (later, aligns "branch summarization with
  JMFTS' provided capabilities"): RAPTOR's unit is *children of a node*, which in
  a mirrored conversation is a chain, not a cluster. Add an operation that
  summarizes a **root→node walk** (or accept an explicit doc-id list), producing a
  `summary` document linked (`link_type="summarizes"`) to the span — the
  server-side twin of τ's `summarize_branch`. Until then, τ keeps its own
  LLM-backed branch summarization and JMFTS gets the *result* as a node (§3.4).
- **CR-6 — `tau:*` usetype presentations** (seed data, trivial): `tau:conversation`
  → `transcript` renderer with collapsed children; `tau:message` → transcript;
  `tau:customEntry`/`tau:navigate` → hidden-ish plain. Makes `GET /view/{id}` of a
  conversation pleasant with zero τ-side work.

## 5. τ-side refactors required (small, and worth it regardless)

1. **Catalog seam.** Discovery/resume is file-path-shaped today. Introduce a small
   protocol next to `SessionLog` — `list(cwd) -> list[SessionInfo]`,
   `most_recent(cwd)`, `load(ref) -> SessionLog + metadata` — extract the current
   file implementation into it, wire the TUI picker (`app.py` session sidebar) and
   headless `_resolve_session_ref` through it. This is the §4.4 `SessionStore`
   idea delivered at the discovery altitude instead of the byte altitude (the byte
   seam is unnecessary: `JmftsSessionLog` implements `SessionLog` directly and
   never touches `Session`'s `_persist_*` funnels).
2. **Concrete-surface audit.** Enumerate every attribute the TUI/headless touch on
   the concrete `Session` beyond the Protocol (`append_model_change`,
   `append_thinking_change`, `append_session_info`, `name`, `context`, `messages`,
   `fork`, header fields) and either add them to a widened secondary Protocol or
   implement them on `JmftsSessionLog` — decided by the audit, enforced by the
   contract tests.
3. **`hostname` in the file header too** (one line in `_build_header`), so file
   and JMFTS sessions carry the same identity metadata and future import/discovery
   is uniform.

## 6. Extension capability layer (the "larger vision" surface)

Shipped as ordinary extensions in `tau_jmfts.ext`, loadable with `-e`, configured
via the existing `"extensions": {"<stem>": {...}}` slice; they use `JmftsClient`
directly and work **regardless of which session store is active** (a file-backed
session can still search JMFTS; enrichment naturally requires the jmfts store).

- **`enrich.py`** — the deferred work the hot path skips: on `turn_end`/
  `session_shutdown`, embed un-embedded `tau:*` documents (`POST /documents/{id}/embed`;
  policy: embed message/summary text, skip or truncate giant tool results — JMFTS
  embedding quality degrades past ~4k chars), register the conversation root into
  the configured BM25 index (`/indexes/{name}/index-document`), maintain the root's
  transcript `content` projection if enabled. All idempotent and resumable.
- **`tools.py`** — agent-facing tools via `api.register_tool`:
  `jmfts_search(query, scope=db|conversation|subtree(doc_id)|index, method, usetype)`
  → hybrid search with `parent_id` containment scoping; `jmfts_read(doc_id, depth)`
  → document + children (leaves recover verbatim text, ancestors give the summary
  ladder); `jmfts_explore(doc_id)` → links + triples neighborhood;
  `jmfts_ingest(content, parent_id, usetype)` → let the agent file documents.
- **Context-injection hooks** — `before_agent_start` search-and-inject (persisted
  as durable `customMessage` nodes, honoring the no-hidden-channels invariant),
  e.g. "retrieve top-k from the project's knowledge index for the incoming prompt".
- **Ontology (later phase)** — predicates like `tau:produced`, `tau:discussed`,
  `tau:continues`; triples linking conversation nodes to ingested code/docs;
  `/triples/path` as a "how do these two artifacts relate" tool; τ's
  `extract-facts` pass over finished conversations feeding the knowledge graph.

Extension stories that need *concurrent LLM work* (e.g. the retrieval-review
pattern) additionally rely on two τ-core primitives that are independent of JMFTS
entirely — see §9.

## 7. Phases

| Phase | Where | Deliverable | Exit criterion |
|---|---|---|---|
| 1 | jmfts | CR-1 (position), CR-2 (structured filter), CR-6 (presentations); CR-3/CR-4 scheduled | migrations applied; ordering contract tested |
| 2 | τ | `tau-jmfts` package: `JmftsClient`, `JmftsSessionLog`, config/CLI wiring, headless first (`tau -p --store jmfts`) | contract test suite green over `InMemorySessionLog` / file `Session` / `JmftsSessionLog`; a headless multi-turn session with branches + compaction round-trips |
| 3 | τ | Catalog seam refactor; TUI picker/resume/fork/delete on JMFTS; `importer.py` (JSONL ⇄ JMFTS, lossless) | existing file sessions importable; TUI parity checklist passes on both stores |
| 4 | both | `enrich.py` (deferred embedding, indexing, projections) + CR-3 batch (importer/fork perf) | conversation content searchable in JMFTS minutes after a session ends |
| 5 | τ | `tools.py` agent tools + context-injection hooks | agent can retrieve from a scoped subtree and cite doc ids |
| 6 | both | Ontology integration; CR-5 path summarization; CR-4 auth enabled | triples over conversations; branch summaries server-aligned |
| C1 | τ core | `ctx.complete()` — direct completions through the model registry (§9.1); parallel track, no JMFTS dependency; wants to land before/with Phase 5 | an extension runs N concurrent completions on a config-named model |
| C2 | τ core | Branch sub-agents / multi-cursor entry log (§9.2); parallel track, no JMFTS dependency. ~~Gated on `docs/PROVIDER-LIFETIME.md`~~ — **gate cleared 2026-07-12** (provider pool landed; see that doc's §8) | a tool-using sub-agent's turns are recorded as a real branch while the primary loop's cursor survives reload |

**Testing spine (Phase 2, the keystone):** one parameterized contract suite
asserting the `SessionLog` algebra (parentId chaining, navigate/cursor semantics,
compaction splice, branch summary re-pointing, custom entries, unknown-kind
tolerance, fork) over all three implementations. JMFTS runs are integration tests
gated by `JMFTS_TEST_URL` against a disposable instance (marker `-m jmfts`),
creating and deleting their own root documents.

## 8. Decision points (defaults chosen, flag to revisit)

1. **Outage policy: hard-fail (chosen).** JMFTS configured + unreachable ⇒ the
   append raises and the turn fails; no shadow JSONL journal, no auto-fallback to
   file (Fail-Early; a dual-write "mirror" is deliberately rejected as a
   fallback-shaped source of split-brain truth).
2. **Topology mirroring over flat-children (chosen)** — rationale in §2; revisit
   only if RAPTOR-over-conversations becomes the dominant use and CR-5 proves
   insufficient.
3. **Sync writes on the hot path (chosen)** — matches today's blocking file I/O;
   revisit (background flusher with a bounded queue + hard join at turn end) only
   if measured latency says so.
4. **Numeric entry ids when JMFTS-backed (chosen)** — ids are opaque strings to τ;
   the payoff (every τ node natively addressable by JMFTS search/links/triples)
   outweighs the cosmetic divergence from 8-hex.
5. **Tool-using sub-agents record real in-tree branches (chosen, §9.2)** — not
   ephemeral side-sessions grafted back as a blob. The session tree stays the
   single truth for everything the agent did; ephemeral remains the right shape
   only for tree-less single completions (§9.1).

## 9. Core track: sub-model calls and branch sub-agents

Two τ-core primitives motivated by JMFTS user stories but deliberately
**independent of JMFTS** — they work identically over `InMemorySessionLog`, the
file `Session`, and `JmftsSessionLog`, and become *native* (searchable, linkable,
summarizable subtrees) when the jmfts store is active.

Motivating story (retrieval review): a query fans out into N JMFTS search
results attached as children of a message; each result becomes an agent branch
answering "include this to answer the query?" — or, multi-level, "include as-is,
or examine the documents it summarizes individually?", recursing; each branch
folds to a verdict; the whole review subtree is then reformatted into a single
retrieval call/response message pair that the primary agent responds to. Dozens
of documents, multiple granularities, concurrent — impractical as sequential
turns, and painful as multiple τ processes.

### 9.1 C1 — `ctx.complete()`: direct completions through the model registry

A single LLM request-response with no tree writes and no agent loop:

```python
msg = await ctx.complete(messages, model="local-llm")  # -> tau_llm AssistantMessage
```

- **Model routing is the point**: `model` is a name resolved through the same
  config `models` registry the TUI picks from (default: the session's current
  model). An extension's model choice is then configured inline with the agent's,
  in its existing config slice — e.g.
  `"extensions": {"retrieval_review": {"model": "local-llm-small"}}` — with no
  extension-private client plumbing.
- Semantics: stateless and session-free — touches neither the entry log nor the
  cursor; safe under `asyncio.gather` at any fan-out; errors propagate to the
  caller (Fail-Early, no retry policy hidden inside). Optionally mirrored as an
  `ext:` observability event, never as tree state.
- Home: `ExtensionContext` in tau-agent-core. Precedent already in-tree:
  `summarize_branch` makes internal LLM calls; C1 is that capability handed to
  extensions with the model made explicit.
- Covers every "classify / extract / draft" story where the result, not the
  process, matters. When an audit trail *is* wanted without a full sub-agent, the
  extension writes the exchange to JMFTS itself as foreign nodes (§2.4) via
  `JmftsClient` — inspectable in the tree browser, invisible to model input.

### 9.2 C2 — branch sub-agents: multi-cursor over one entry log

> **PREREQUISITE — provider/HTTP-client lifetime: RESOLVED (2026-07-12). C2 is unblocked.**
> See **`docs/PROVIDER-LIFETIME.md` §8**.
>
> The prerequisite was that `stream_simple` built a fresh provider + `httpx.AsyncClient` on
> **every completion** (+42 ms/call, 51 % slower on a LAN; C2 multiplies that by the fan-out) —
> and, decisively, that the obvious fix would have silently routed a second model's completion
> **and its API key** to the first model's server, HTTP 200, no error. C2 is precisely the
> feature that puts more than one model in a run and would have unmasked it.
>
> It is now a provider pool keyed on `(provider_name, base_url, sha256(api_key))`, per event
> loop, with explicit teardown; `ProviderRegistry` is deleted. **Do not re-key the pool on
> `provider_name` alone** — that is the cross-routing bug, and it fails silently.

```python
result = await ctx.spawn_branch(parent_id, prompt, tools=["jmfts_read"], model=..., max_turns=...)
```

Two properties fall out of the existing fold for free, which is why this is
tractable at all:

- **Context isolation both ways.** `context_for` walks leaf→root, so a
  sub-branch's entries are never ancestors of the primary leaf (they cannot leak
  into the primary context, whatever their kind) — and the sub-agent's own
  context is the walk from *its* leaf up through the branch point, i.e. exactly
  the shared conversation prefix plus its own work. Choosing `parent_id` chooses
  the sub-agent's inherited context.
- **The persisted format already tolerates it.** Entries carry explicit
  `parentId`; only the *writer* convenience (chaining off a single `_leaf_id`)
  assumes one cursor. Interleaved appends from many branches are valid files/rows
  today.

The actual design work, enumerated:

1. **Explicit-parent appends** — a `BranchCursor` handle (own leaf id, same
   underlying log) or `parent_id=` parameters on the `append_*` surface; an
   additive `SessionLog` change, format unchanged.
2. **Primary-cursor discipline** — `_resolve_cursor` reads "last entry,
   latest-wins navigate"; if a sub-branch entry lands last before a crash, reload
   would resume inside the branch. Branch entries therefore carry a lane marker
   (e.g. `branchOf: <branch-root id>`) that cursor resolution ignores; the
   primary cursor only ever moves by primary appends/navigates.
3. **Append serialization** — one in-process lock held across seq assignment +
   the write, so `seq` (and, on JMFTS, doc-id order) stays monotone and the §2.3
   integrity check keeps holding. This serializes only the millisecond-scale
   appends; the concurrency that matters (N LLM streams) is untouched. The
   single-writer invariant refines to: **one process, many cursors**.
4. **Branch-tagged events** — `AgentEvent`s carry no run identity today; the bus
   gains a branch id so the TUI can render the primary stream plus branch
   progress (full parallel stream rendering can come later).
5. **Failure containment** — a sub-agent error marks its branch with an error
   entry and returns the failure to the spawner; it never aborts the primary loop.
6. **Tool scoping** — sub-agents share the process and cwd; `tools=[...]` is a
   hard allowlist per spawn (retrieval evaluators get `jmfts_read`, not
   `write`/`bash`).

**Fold step**: the spawner reads its branches' verdicts (leaf entries or branch
summaries), then makes exactly one primary-cursor append — the distilled
"retrieval call + response" pair. With the jmfts store active, the finished
review branches are already real JMFTS subtrees: enrichment can embed them,
CR-5 can summarize them, and future conversations can search them. On the file
store the same branches are just interleaved JSONL lines — fully functional,
merely less queryable.

**Ordering**: C1 first — it is C2 minus tools, tree writes, and events; it
covers most stories at a fraction of the cost, and its model-resolution plumbing
is exactly what C2 reuses.
