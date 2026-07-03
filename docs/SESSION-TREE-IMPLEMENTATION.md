# Session tree persistence + TUI tree-browser — implementation slice

> **Status: PLAN (2026-07-03).** The concrete, buildable slice of
> `docs/EXTENSIONS-ORCHESTRATION-PLAN.md` §4 ("tree-as-truth, DECIDED") and
> the deferred branching UI of `docs/SESSION-UX-REDESIGN.md` §10. Three parts:
> **(1)** make the persisted session a real tree with a persisted cursor and
> read-time splices; **(2)** a TUI tree-browser with three-mode subtree
> compaction; **(3)** the seam a fork would replace to persist to a database
> by UUID instead of a file — **documented, not built** (Fail-Early: no
> speculative DB code). pi is the source of truth for shape; evidence cited as
> `file:line` against the current tree and the local pi checkout
> (`~/Development/pi`). Maps onto ROADMAP Tier 11 **E3**.

---

## 0. What already exists (so we build the gap, not the whole thing)

The research (2026-07-03) established that most primitives are already on disk;
the gap is the *fold*, the *cursor*, and all of the *UI*.

**τ has two session stores, and they must converge (plan §4):**

- **System B — `session_store.Session`** (`tau-coding-agent/.../session_store.py`)
  is the **authoritative** live store: the TUI (`app.py`) and headless
  (`headless.py`) read/write/resume through it. It is *already a parentId
  tree*: `_append` stamps `parentId = self._leaf_id` and advances the leaf
  (`session_store.py:379-391`), it has a `fork` (`:299`) and an
  `append_compaction` (`:339`), and a `_leaf_id` cursor. But it is used
  **linearly** — nothing ever moves `_leaf_id` backward, `messages` folds by
  `type` and *ignores* `parentId` (`:185`), the cursor is **not persisted**,
  and compaction is a marker no reader splices on.
- **System A — `SessionManager`** (`tau-agent-core/.../session_manager.py`)
  owns the real **tree algebra** — `_build_active_path` (leaf→root walk +
  compaction splice, `:544-625`), `_extract_branch_messages` (subtree collect,
  `:627-702`), `navigate` (`:494`), `fork` (`:386`), `summarize_branch`
  (`:742`) — but it is **vestigial on the live path** (built by `TauBackend`
  then bypassed, `backends.py:85,110-115`) and it **violates append-only**:
  `apply_compaction` re-parents `first_kept` and `_persist_entries` rewrites
  the whole file in `"w"` mode (`session_manager.py:223-302`, `:300`).

**pi already ships the entire target feature** — the port is a translation, not
a design:

| pi (source of truth) | file:line | τ target |
|---|---|---|
| `buildSessionContext(entries, leafId)` — leaf→root walk, splices compaction/`branch_summary` at read time | `session-manager.ts:325-379` | `ConversationTree.context_for(leaf)` (§2.1) |
| `leafId` cursor (in-memory, falls back to last entry on load) | `:768, 859, 940` | persisted cursor via `navigate` entry (§2.2) |
| `appendCompaction` — plain appended entry, spliced at read time | `:990-1010` | append-only compaction (§2.3) |
| `branch(id)` — just moves the cursor | `:1241-1246` | `ConversationTree.navigate(id)` (§2.2) |
| `branchWithSummary(id, summary)` — append a `branch_summary`, move cursor | `:1262-1279` | second splice kind, unified with compaction (§2.4) |
| `getTree(): SessionTreeNode[]` — parent/child nodes for the UI | `:1191` | `ConversationTree.tree()` (§2.5) |
| `showTreeSelector` → "No summary / Summarize / Summarize with custom prompt" → `navigateTree` | `interactive-mode.ts:4446, 4479-4483, 4526`; `agent-session.ts:2708` | `SessionTreeModal` + `backend.navigate_tree` (§3) |

So: **Part 1 makes the fold real and the cursor persistent; Part 2 is the
Textual UI over it; Part 3 documents the storage boundary.**

---

## Part 1 — Session redesign: persist the tree

The decision (plan §4): the branching, summarizable conversation *tree* is the
genuine data structure; the linear message list is a read-time view. The
persisted artifact is an append-only log of tree nodes **including the cursor**.
Three objects (plan §4.1): `SessionLog` (persistence, kept from
`session_store.Session`'s file discipline), `ConversationTree` (pure, I/O-free
structure + cursor), Views (derived). Part 1 builds `ConversationTree` and
retires System A.

### 2.1 `ConversationTree` — the pure structure (port `_build_active_path`)

A new I/O-free class in **`tau-agent-core`** (the algebra belongs with the loop,
not the TUI), operating over the raw entry dicts that `Session.entries()`
already yields (`session_store.py:214`). No filesystem, no `Session`, no
`asyncio` — a pure function of `(entries, cursor)`.

```python
# tau_agent_core/conversation_tree.py  (new)
class ConversationTree:
    def __init__(self, entries: list[dict], cursor: str | None) -> None:
        self._entries = entries                        # append-only, load order
        self._by_id  = {e["id"]: e for e in entries}   # id → entry
        self._order  = {e["id"]: i for i, e in enumerate(entries)}
        self._children: dict[str | None, list[str]] = {}
        for e in entries:
            self._children.setdefault(e.get("parentId"), []).append(e["id"])
        self._cursor = cursor                          # leaf pointer (None = pre-root)

    # --- navigation (cursor only; nothing is deleted or rewritten) ---
    def navigate(self, entry_id: str | None) -> None: ...   # pi branch(): move cursor
    def path(self, leaf: str | None = None) -> list[dict]:  # leaf→root, reversed
        ...

    # --- the interpretive fold (port of _build_active_path:544-625) ---
    def context_for(self, leaf: str | None = None) -> list[dict]:
        """Root→leaf path with compaction/branch_summary splices applied.
        This is exactly _build_active_path's compaction logic (session_manager.py:597-622):
        anchor on the LAST compaction/branch_summary entry in the path, keep the
        summary node, drop path entries whose linear order precedes firstKeptId."""
        ...

    # --- UI + subtree ops ---
    def tree(self) -> list["TreeNode"]: ...     # pi getTree():1191 — for the browser
    def subtree_text(self, from_id: str) -> str:  # port _extract_branch_messages:627-702
        ...
```

**Provenance is exact.** `context_for` is `_build_active_path`
(`session_manager.py:544-625`) made side-effect-free — the same leaf→root
`parentId` walk with a cycle guard, the same "anchor on the *last* compaction
in the path" rule (`:597-600`, the iterative-compaction correctness note), the
same `linear_order`-vs-`firstKeptId` filter (`:602-622`). `subtree_text` is
`_extract_branch_messages` (`:627-702`) verbatim — the BFS descendant collector
that "summarize branch" feeds to the LLM. Both already exist and are tested in
System A; we are *moving* them onto the live entries, not writing them.

**Key name reconciliation** (the agent flagged this): System B entries use
camelCase `parentId`/`firstKeptId`/`tokensBefore` (`session_store.py:339,383`);
System A's algebra reads snake_case `parent_id`/`first_kept_id`. Since
`ConversationTree` lives over System B's on-disk format, it reads camelCase.
The port renames the field accesses — a mechanical diff, called out here so it
is not missed.

### 2.2 The persisted cursor — a `navigate` entry kind (plan §4.3)

pi's `leafId` is in-memory only; on load it falls back to the last file entry
(`session-manager.ts:349, 859`), so a pure `branch()` with no subsequent append
evaporates on quit. For agents (and a human tree-browser) that move the cursor
*without* adding content, the cursor is first-class state. τ diverges: a new
entry kind, appended whenever the tip moves without new content —

```json
{"type":"navigate","id":"<8-hex>","parentId":"<prev leaf>","timestamp":"<iso>","targetId":"<id|null>"}
```

On load, `cursor =` resolved from the **last** entry: a `navigate` entry points
at its `targetId` (`null` = before-first-entry); any other kind points at
itself. Latest-wins, exactly how `model_change`/`session_info` already resolve
(`SESSION-UX-REDESIGN.md:178-179`). This is a reserved-kind addition — format
version stays 1; `branch_summary`/`custom` were already reserved
(`SESSION-UX-REDESIGN.md:182-184`). New method on `Session`:

```python
def append_navigate(self, target_id: str | None) -> str:   # session_store.py
    return self._append("navigate", targetId=target_id)
```

**Compatibility is asymmetric and accepted** (plan §4.3): τ reading a pi file
(no `navigate` entries → cursor = last entry) behaves identically to pi; pi
reading a τ file hits an unknown kind — flagged, not blocking, and τ is the
consumer here.

### 2.3 Append-only compaction — retire the file rewrite (plan §4.2)

Replace System A's `apply_compaction` re-parent-and-rewrite
(`session_manager.py:223-302`, `_persist_entries` opens `"w"` at `:300`) with
pi's semantics (`appendCompaction`, `session-manager.ts:990`): **append** a
`compaction` entry (`session_store.py:339` already writes the right shape —
`summary`/`firstKeptId`/`tokensBefore`), leave every prior entry untouched, and
let `ConversationTree.context_for` splice at read time (§2.1). This:

- restores the append-only invariant and crash-safety (a torn `"w"` rewrite can
  destroy a session; the docstring at `session_manager.py:223` already concedes
  the tension);
- keeps pre-compaction history **addressable** — navigating behind the boundary
  and continuing ("un-compact and explore") falls out for free, because nothing
  was deleted;
- deletes `_persist_entries` (`:291`) and the re-parenting block (`:284-286`).

The compaction *engine* is untouched — prompts, token estimation,
`complete_simple`, `_perform_compaction` (`agent_session.py:470`) and the
`compact_messages` count-based path the TUI uses (`agent_session.py:400`) keep
working; only *where the boundary is recorded* changes (append, not rewrite).
This is the "reworks Tier 5's landed `apply_compaction`, structural half only"
line from the plan (§7).

### 2.4 `branch_summary` — the second splice kind (one mechanism, two kinds)

pi's `branchWithSummary` (`session-manager.ts:1262`) and `appendCompaction`
(`:990`) are the *same* operation — "replace a subpath with a summary node at
read time" — differing only in entry `type`. τ mirrors this: `context_for`'s
splice treats `compaction` and `branch_summary` identically (both are summary
anchors with a `firstKeptId`/`fromId`). "Summarize branch" (§3) appends a
`branch_summary`; auto-compaction appends a `compaction`; one splice path reads
both. New method:

```python
def append_branch_summary(self, summary: str, from_id: str | None) -> str:
    return self._append("branch_summary", summary=summary, fromId=from_id)
```

### 2.5 `tree()` for the browser UI (port `getTree`)

The Textual tree-browser (§3) needs parent/child nodes with display text, a
one-line summary, and the real-leaf marker — pi's `getTree(): SessionTreeNode[]`
(`session-manager.ts:1191`), children sorted by timestamp. Pure, derived from
the same index `ConversationTree` already builds:

```python
@dataclass
class TreeNode:
    id: str
    parent_id: str | None
    kind: str                 # message | compaction | branch_summary | navigate
    role: str | None          # for message nodes
    preview: str              # first line of text (browser row)
    is_leaf: bool             # == current cursor
    children: list["TreeNode"]
```

### 2.6 Wiring — one write path, System A retires (plan §4.5)

- `AgentSession.messages` (`agent_session.py:122`) stops calling
  `SessionManager.get_active_messages()` and instead builds
  `ConversationTree(session.entries(), cursor).context_for()`. The per-turn
  persistence that currently appends through `SessionManager.append_entry`
  (`agent_session.py:266-272, 285-291`) appends through the same `Session` the
  TUI/headless already persist through.
- `TauBackend` stops constructing a throwaway `SessionManager`
  (`backends.py:85`); it holds the live `Session` and a `ConversationTree` view
  over it.
- `SessionManager`'s **persistence** retires; its **algebra** is now
  `ConversationTree`. (Rejected alternative, plan §4.5: teach `SessionManager`
  to read/write `session_store` files — two classes for one format,
  perpetuating the double-write.)
- The TUI's hand-maintained `self.messages` dual-write (`app.py:883-886,
  1109-1112`) is the **last** thing to die — a view-discipline refactor
  (`self.messages` → a subscribed `transcript_view` over `ConversationTree`),
  stageable *after* this slice alongside session-sprint Phase B/C. Until then it
  stays, kept in step as today.

### 2.7 Tests

Property-style tests over synthetic entry trees, in `tau-agent-core/tests`:

- **fold parity**: `ConversationTree.context_for` == the current
  `_build_active_path` output for a battery of linear + branched + multiply-
  compacted trees (freeze System A's output first, then assert equality — this
  is the regression net for the port).
- **cursor round-trip**: append messages → `navigate(interior)` → append →
  reload → cursor and `context_for` match pre-quit.
- **append-only compaction**: after compaction the file is append-only (no line
  rewritten; byte-prefix stable), `context_for` splices correctly, and
  navigating behind the boundary restores the pre-compaction messages.
- **branch_summary == compaction** splice equivalence.
- camelCase field reads (`parentId`/`firstKeptId`) — the reconciliation guard.

---

## Part 2 — TUI tree-browser + three-mode subtree compaction

The UI is the entire gap here — τ has the storage, pi has the blueprint. Port
pi's `showTreeSelector` flow (`interactive-mode.ts:4446`) onto Textual.

### 3.1 The three modes (pi blueprint, verbatim)

When the user picks a node to branch from, pi prompts three choices
(`interactive-mode.ts:4479-4483`):

1. **No summary** — move the cursor to the chosen node; the abandoned branch
   simply drops out of context (still on disk, still browsable). → append a
   `navigate` entry (§2.2). Zero LLM calls.
2. **Summarize** — summarize the abandoned branch (from chosen node → old leaf)
   and splice the summary in. → `subtree_text` (§2.1) → `summarize_branch`
   engine (`session_manager.py:742`, refactored per below) → `append_branch_summary`
   (§2.4) with cursor moved to the summary node.
3. **Summarize with custom instructions** — same as (2) but a modal collects a
   custom prompt first (pi `showExtensionEditor`, `interactive-mode.ts:4494`),
   threaded into the summarizer's system prompt.

**Fail-Early fix carried here** (plan §3.3): `summarize_branch`'s
truncated-raw-text fallback (`session_manager.py:~730` region) is replaced with
`complete_simple` + raise on failure while it is exposed — we do not ship a
branch-summary path that silently fabricates a summary from truncated text.

### 3.2 `SessionTreeModal` — the overlay

A `ModalScreen[str | None]` (returns the chosen entry id, or `None` on cancel),
copying the one existing modal template, `SystemPromptEditor` (`app.py:44-68`),
and using **`textual.widgets.Tree`** (no Textual `Tree` is used anywhere today —
added fresh; the agent confirmed). Populated from `ConversationTree.tree()`
(§2.5); the current leaf is highlighted (pi passes `realLeafId` to the selector,
`tree-selector.ts` ctor). `Enter` → `dismiss(node_id)`; `Esc` → `dismiss(None)`.
A second tiny `ModalScreen[str]` (or reuse `SystemPromptEditor`'s `TextArea`
shell) collects the custom instructions for mode 3.

The three-mode choice is a minimal `ModalScreen` with three buttons (or a
`RadioSet`) — pi's `showExtensionSelector` (`interactive-mode.ts:4479`).

### 3.3 `backend.navigate_tree` — the one new backend method

Port pi's `navigateTree` (`agent-session.ts:2708`) as the single seam the UI
calls, on `TauBackend` (`backends.py`), alongside the existing
`compact_messages` (`backends.py:137`):

```python
async def navigate_tree(
    self, target_id: str, *,
    summarize: bool = False,
    custom_instructions: str | None = None,
) -> list[dict]:
    """Move the cursor to target_id. If summarize, summarize the abandoned
    branch (target→old leaf) and append a branch_summary; else append a
    navigate entry. Returns the new context_for(cursor) for re-render."""
```

Internally: resolve old leaf; if `summarize`, `tree.subtree_text(target_id)` →
summarizer (custom prompt if given) → `session.append_branch_summary(summary,
target_id)`; else `session.append_navigate(target_id)`. Then move the
`ConversationTree` cursor and return `tree.context_for()`.

### 3.4 Re-render

Reuse the existing compaction re-render path exactly: `action_compact` already
replaces `self.messages` and calls `ChatDisplay.reload_messages`
(`app.py:1363-1378`). The new `action_browse_tree` does the same with
`navigate_tree`'s return value — one rebuild path, two callers.

### 3.5 Wiring — keybinding, slash, palette

Follow the existing triad (`app.py:869-881` BINDINGS, `on_input_submitted`
slash intercept at `:1007`, `get_system_commands` at `:1235`):

- **Binding**: a key (e.g. `ctrl+g`) → `action_browse_tree`. (pi leaves the tree
  key user-assigned, `keybindings.ts:111` `defaultKeys: []`; τ can ship a
  default.)
- **Slash**: `/tree` (browse) and `/fork` (pi aliases, `keybindings.ts:252-253`).
- **Palette**: a "Browse conversation tree…" `SystemCommand` (`app.py:1255`
  neighborhood, next to "Compact Conversation").

When Part-3's command registry (SESSION-UX-REDESIGN Phase C, seam 4) lands,
these collapse into one `Command` exposed three ways; until then, three thin
bindings to `action_browse_tree`, matching the current code.

### 3.6 Tests

- `SessionTreeModal` returns the chosen id / `None` on cancel (Textual
  `Pilot`).
- `navigate_tree(summarize=False)` appends exactly one `navigate` entry, moves
  the cursor, drops the abandoned branch from `context_for` but not from disk.
- `navigate_tree(summarize=True)` against `fake_llm`: appends a `branch_summary`,
  splices it, custom instructions reach the summarizer's system prompt.
- re-render: `reload_messages` shows the post-navigate context.

---

## Part 3 — The external-store seam (database by UUID) — **documented, not built**

> **Goal (verbatim from the request):** "transparently to Tau, I'd like to be
> able to use a database and look up a session by UUID, and return the same
> tree datastructure. Don't implement any theoretical database features, just
> plan what a modified version of Tau would have to monkeypatch or replace."

**This section builds nothing DB-specific** (Fail-Early: no ORM, no schema, no
second store implementation, no speculative config). It (a) identifies the exact
surface a fork replaces, (b) names the one structural change in Part 1 that
makes that surface small and clean, and (c) writes down the ~5-operation
contract the fork's store satisfies.

### 4.1 Why the seam is already almost clean

`ConversationTree` (Part 1) is **I/O-free** — it is a pure function of
`(entries, cursor)`. So durable storage only ever has to do three things:
**append an entry**, **read (header + ordered entries) by id**, and **list
session metadata**. Everything tree-shaped is reconstructed in memory. The
persistence surface is therefore tiny, and it is almost entirely in one class
(`Session`) plus three module functions.

Two properties already point the right way:

- **`base_dir` injection** is threaded through the whole read/create/list
  surface (`session_store.py`: `_sessions_base:75`, `session_dir_for_cwd:80`,
  `create/fork`, `list_sessions:505`, `most_recent:529`). A file fork already
  overrides *where*; a DB fork needs to override *how*.
- **`path is None` (in-memory) mode already exists** — `_persist_header`/
  `_persist_entry` no-op when `path is None` (`session_store.py:394,402`). That
  proves the write path is already funnelled through exactly two methods and is
  already conditionally suppressible. A DB backend is "the `path is None` branch,
  but persisting to a database instead of nowhere."

### 4.2 The one structural prerequisite: identity is the UUID, not the path

System B already keys identity on the header UUID (`session_store.py:166`,
filename `<iso>_<uuid>.jsonl`, `SESSION-UX-REDESIGN.md:147`) — good. But two
places still treat the **filesystem path as identity** and must be severed for a
DB (which has no path):

- `Session.path: Path | None` is used both as *identity* and as *I/O handle*.
  Split the roles: identity = `Session.id` (UUID, already present); the I/O
  handle moves behind the store boundary (§4.3).
- **System A uses the path as the session id outright** —
  `AgentSession.state.session_id = self._session_manager._active_session_path`
  (`agent_session.py:128` region). This dies with System A's retirement (§2.6);
  after Part 1, identity is uniformly the UUID. **This retirement is a
  precondition for the DB seam** — as long as System A is on the path, "look up
  by UUID" has two answers.

### 4.3 The exact surface a fork replaces (the monkeypatch map)

After Part 1, *all* durable I/O lives in these members of `session_store.py`.
A DB fork replaces exactly this set and nothing else:

| Operation | Today (file) | file:line | DB fork replaces with |
|---|---|---|---|
| **write header** | `_persist_header` — `path.open("x")` | `:393-399` | `INSERT` a session row keyed by UUID |
| **append entry** | `_persist_entry` — `path.open("a")` | `:401-405` | `INSERT` an entry row `(session_id, seq, json)` |
| **read one session** | `Session.load(path)` — stream lines | `:276-297` | `SELECT` header + entries by UUID, ordered |
| **list metadata** | `SessionInfo.read(path)` + `list_sessions` + `most_recent` | `:436-497, 505, 529` | `SELECT` a metadata projection, filtered by cwd |
| **locate** | `session_dir_for_cwd` / `_sessions_base` / `_session_filename` | `:80, 75, 110` | no-ops / trivial (UUID is the key; no path) |

The **entire durable write surface is two methods** (`_persist_header`,
`_persist_entry`); the read/list surface is three functions
(`load`, `SessionInfo.read` + its two listers). Note `_append`
(`session_store.py:379`) — the in-memory tree mutation + `_leaf_id` advance —
is **backend-agnostic and stays as-is**; only the `_persist_entry` call inside
it swaps. That is the crux of "transparent to Tau": the tree logic never learns
whether the bytes went to a file or a row.

### 4.4 The contract (what the fork implements)

The seam is this five-operation protocol. Whether τ *ships* it as an explicit
`Protocol`/ABC now, or leaves it as the documented monkeypatch surface above, is
a **maintainer decision** (§5) — Fail-Early cautions against introducing an
abstraction with a single implementation. Either way the contract a DB fork
satisfies is:

```python
# The boundary — a fork provides an object with these five methods.
# (Presented as the CONTRACT, not shipped as code, per Fail-Early.)
class SessionStore(Protocol):
    def write_header(self, session_id: str, header: dict) -> None: ...
    def append_entry(self, session_id: str, entry: dict) -> None: ...
    def read(self, session_id: str) -> tuple[dict, list[dict]]:      # (header, entries)
        ...
    def list(self, cwd: str | None) -> list["SessionInfo"]: ...
    def most_recent(self, cwd: str | None) -> str | None:           # returns a session_id
        ...
```

Injection point (the "replace" story, cleaner than monkeypatching privates): a
module-level `_store` in `session_store.py` defaulting to the file
implementation, selected by a factory that today reads `base_dir` and would read
a store URL/env in a fork — the single line a fork changes. `Session` holds a
`store` reference instead of a `path`; `_persist_header`/`_persist_entry` become
`self._store.write_header(...)`/`append_entry(...)`; `load(id)`/`list_sessions`/
`most_recent` delegate to the store. Nothing above `Session` changes — the TUI,
headless, `AgentSession`, and `ConversationTree` all keep calling the same
`Session`/`entries()`/`append_*` surface. **That is the transparency the request
asks for.**

### 4.5 The "look up by UUID, return the same tree" path, end to end

With the above, resolving a session by UUID from a DB is:

```
store.read(uuid) -> (header, entries)          # the fork's SELECT
Session(store=store, header=header, entries=entries)   # unchanged constructor
ConversationTree(session.entries(), cursor_from_last_entry(entries))  # unchanged (§2.1)
```

The returned `ConversationTree` is byte-for-byte the same structure a file-backed
load produces — because it is a pure function of the entry list, and the entry
list is format-identical whether it came from `.jsonl` lines or DB rows. Resume
(`headless._resolve_session_ref`, `headless.py:139-164`; TUI `Session.load`,
`app.py:1400`) changes from "resolve a path" to "resolve an id" — and `--session
<uuid>` (`SESSION-UX-REDESIGN.md:147`) *already* resolves by id, so the flag
surface is unchanged too.

### 4.6 Explicitly out of scope (Fail-Early)

- **No database is implemented** — no schema, migrations, connection pooling,
  transactions, driver dependency, or `DbSessionStore` class. This section is the
  *map* a fork follows.
- **No speculative config** — no store-URL env var, no `[store]` config block
  until a real backend exists (mirrors the reserved-but-unwired
  `TAU_CODING_AGENT_SESSION_DIR`, `SESSION-UX-REDESIGN.md:130-132`).
- **No abstraction shipped on spec alone** — whether to introduce the
  `SessionStore` Protocol during E3 (vs. leave §4.3 as the documented
  monkeypatch surface) is deferred to the maintainer (§5, decision 3). The
  honest default: don't add the ABC until a second store is real; keep the
  write surface funnelled through the two named methods so the fork is
  mechanical either way.

---

## 5. Sequencing & ROADMAP fit

This slice **is** ROADMAP Tier 11 **E3** made concrete
(`docs/EXTENSIONS-ORCHESTRATION-PLAN.md` §7), split into independently landable
steps, each keeping the Tier-5 gate green (ruff clean + mypy 0):

| Step | Contents | Depends on |
|---|---|---|
| **1a** | `ConversationTree` (§2.1) + `tree()` (§2.5) + fold-parity tests (§2.7) — pure, no wiring | — |
| **1b** | `navigate`/`branch_summary` entry kinds + persisted cursor (§2.2, §2.4) | 1a |
| **1c** | append-only compaction; delete `_persist_entries` rewrite (§2.3) | 1a |
| **1d** | wire `AgentSession`/`TauBackend` onto `ConversationTree`; retire System A persistence (§2.6) | 1a–1c |
| **2** | `SessionTreeModal` + three modes + `navigate_tree` + wiring (§3) | 1d |
| **3** | *(no build)* — the DB seam is realized as a byproduct of §4.2's identity
        cleanup (done in 1d) + §4.3's two-method funnel; document status only | 1d |

**Ordering notes.** 1a is pure and testable in isolation (freeze System A's
fold, assert equality) — the safe first commit. 1c is the "reworks Tier 5's
`apply_compaction`" item (plan §7) — structural only; the summarization engine is
untouched. Part 2 needs 1d (a live `ConversationTree` on the backend). Part 3
requires **no new code beyond 1d** — the UUID-identity cleanup (§4.2) and the
two-method write funnel (§4.3) already exist after Part 1; the section is
documentation of a seam, per the request.

**Open maintainer decisions:**
1. **`navigate` entry kind** — persist the cursor as a first-class entry (§2.2,
   deliberate pi divergence)? *Plan §4.3 assumes yes.*
2. **Default tree-browser keybinding** — ship `ctrl+g` (or leave unbound like pi,
   `keybindings.ts:111`)?
3. **Ship the `SessionStore` Protocol in E3, or leave §4.3 as the documented
   monkeypatch surface?** Fail-Early leans "document now, abstract when a second
   store is real."
