# Session UX Redesign — JSONL storage, cwd scoping, modal picker, unified commands

> **Status: DESIGN (approved shape, not yet implemented) — 2026-06-22.**
> This is the spec for moving τ's session layer off the chat-web shape it
> inherited from Parley and onto the coding-agent shape pi/Claude Code use:
> append-only JSONL transcripts, partitioned by working directory, surfaced
> through a Textual modal picker and a unified command surface
> (`--resume` ≡ `/resume` ≡ command-palette "Resume session…").
>
> pi is the source of truth. Parity targets are cited as `file:line` against the
> local pi checkout (`~/Development/pi`). Implemented τ surfaces are cited against
> the current tree. Where τ deliberately diverges (or defers a pi feature), it is
> flagged inline.

## 1. Why

τ's session store is the **chat-web shape**, not the coding-agent shape
(`session_store.py`):

- Flat directory `~/.tau/chats/`, one file `<int(created_at)>.json` per session,
  a single rewritten JSON object (`Chat.save()`, `session_store.py:42`).
- `Chat = {model, backend, messages, created_at, title?}` — **no cwd, no stable
  id, no message count, no explicit `updated_at`** (`session_store.py:25-40`).
- `Chat.list_recent()` globs and sorts by file mtime with **no cwd filter**
  (`session_store.py:57`). The filename is a unix-second, so two sessions started
  in the same second collide — patched with a `+1.0s` loop in headless
  (`headless.py:329`).
- The sidebar (`app.py:291`) mounts **visible by default** and lists *all* recent
  chats globally, showing the title only.

A coding session's value *is* its directory, git state, and files. Web chat
sidebars are flat because every chat is context-free; coding agents key sessions
on the **working directory** so "continue where I was in *this* repo" needs no
arguments. That is the consensus across the cohort (see §3) and pi's design. This
redesign adopts it.

## 2. Decisions (locked)

These were settled with the maintainer before writing this doc:

1. **Storage format → append-only JSONL.** Full pi parity, not an incremental
   patch of the single-object format. No migration of existing `~/.tau/chats`
   sessions — τ is pre-production with no real usage; the old directory is
   abandoned (see §5.6).
2. **cwd directory encoding → pi's `--<dashed-path>--` wrapping.** Honor the pi
   lineage rather than Claude Code's leading-`-` form.
3. **No token totals in the picker.** Message count is the natural, ecosystem-
   standard scan metric; τ will not diverge here. (Token/cost preview is parked
   under §10, "Tau identity".)
4. **Sidebar defaults to closed.** The modal picker + command palette become the
   canonical session surface. The sidebar widget is **kept** (not retired) — the
   decision to repurpose or remove it is deferred to the "Tau identity" track
   (VS Code-style panels, token preview), §10.
5. **Whole plan up front.** This doc designs all three phases coherently; delivery
   is still phased (§9).
6. **Phase A carries four forward-compat seams** (approved 2026-06-22): parameter
   slots, a raw `entries()` accessor, session-lifecycle events, and a dynamic
   command registry — each cheap to build in now and expensive to retrofit, each
   unlocking a later ROADMAP tier (7–12). Specified in §9, Phase A.

## 3. Research summary

Cross-verified across pi (source of truth), Claude Code (double-checked against
the live filesystem), Codex, Gemini CLI, opencode, aider. Full citations at §11.

| Tool | Directory layout | Filename | cwd-scoped picker? | Picker row shows |
|---|---|---|---|---|
| **pi** | `~/.pi/agent/sessions/--home-john-Dev-pi--/` | `<iso-ts>_<uuidv7>.jsonl` | ✅ Tab: Current ↔ All | name/first-msg · count · age · cwd; threaded forks |
| **Claude Code** | `~/.claude/projects/-home-john-Dev-tau/` | `<uuid>.jsonl` | ✅ worktree default; Ctrl+W repo, Ctrl+A global, Ctrl+B branch | summary · relative time · count · **git branch** |
| **Codex** | `~/.codex/sessions/YYYY/MM/DD/` | `rollout-<ts>-<uuid>.jsonl` | ✅ | first prompt · time |
| **opencode** | `~/.local/share/opencode/storage/` (SQLite) | DB rows | ✅ project | title · time |
| **aider** | single `.aider.chat.history.md` in cwd | — (one rolling file) | cwd-implicit | n/a (no picker) |

Three findings drive the design:

- **cwd is the primary key.** Scoping to the working directory is what lets
  "continue" do the obvious thing with zero args. Cost: monorepos blur (mitigated
  by slugging the *exact* cwd), and `mv` orphans history (Claude Code added a
  `/cd` to relocate storage). Accepted tradeoff.
- **Append-only JSONL + UUID filename, metadata inside the file.** UUID gives
  collision-free concurrent writes; the human never reads the filename because the
  picker renders metadata streamed from the file. pi double-encodes time *and*
  uuid in the filename (`<iso-ts>_<uuidv7>`), so `ls` is already chronological —
  meaning a stdlib **uuid4** suffices for τ (the timestamp prefix carries the
  sort order; no uuid7 dependency needed). See §5.2.
- **Terminal agents keep tokens/cost OUT of picker rows** for scannability; only
  the IDE-panel agents (Cline/Roo) show them. This matches decision 3.

## 4. Target architecture at a glance

```
~/.tau/sessions/--home-john-Development-agent-harness-py--/
    2026-06-22T14-03-51-204Z_4f9c…uuid4.jsonl     ← one session, append-only
    2026-06-22T15-12-09-880Z_a1b2…uuid4.jsonl

Session (session_store.py)        ← wraps one .jsonl file; append-on-message
SessionInfo (session_store.py)    ← lightweight streaming reader for the picker
list_sessions(cwd=…)              ← lists a cwd's dir, or all dirs

SessionPickerModal (app.py)       ← ModalScreen[Path] over a DataTable
CommandRegistry (app.py)          ← one handler per action…
   ├─ CLI flag        (--resume)  ← …exposed three ways with one grammar
   ├─ slash command   (/resume)
   └─ palette entry   (Ctrl+P "Resume session…")
```

## 5. On-disk format

### 5.1 Directory layout & cwd encoding

```
~/.tau/sessions/<dashed-cwd>/<filename>.jsonl
```

`<dashed-cwd>` ports pi's `getDefaultSessionDirPath`
(`session-manager.ts:438-442`):

```
dashed = "--" + abspath.lstrip("/\\").replace("/", "-").replace("\\", "-").replace(":", "-") + "--"
```

`/home/john/Development/agent-harness-py` → `--home-john-Development-agent-harness-py--`.

Resolution helper (new): `session_dir_for_cwd(cwd: str) -> Path`. The base dir is
`~/.tau/sessions/`; mirror pi, which derives it from `APP_NAME`
(`config.ts:481-482`, `PI_CODING_AGENT_SESSION_DIR`). A future
`TAU_CODING_AGENT_SESSION_DIR` override is reserved (see `docs/COMMAND_LINE.md`)
but **not** implemented now (Fail-Early: no speculative env plumbing).

### 5.2 Filename

```
<iso-ts-dashes>_<uuid4>.jsonl
```

e.g. `2026-06-22T14-03-51-204Z_4f9c2e1a-7b3d-4c8e-9a16-0d5f6e7a8b90.jsonl`. The
ISO timestamp (colons/periods → `-`) gives chronological `ls` order; the uuid4
(stdlib `uuid.uuid4`) gives collision-safe concurrent creation. This matches pi's
`<iso-ts>_<id>` scheme (`session-manager.ts:845`); τ uses uuid4 rather than
uuidv7 because the timestamp prefix already supplies sortability, so no third-
party uuid7 dependency is taken. The session's canonical **id** is the uuid (also
stored in the header, §5.3) — stable across file moves and the key the picker and
`--session` resolve against.

### 5.3 JSONL schema

**Line 1 — header:**

```json
{"type":"session","version":1,"id":"<uuid4>","timestamp":"<iso>","cwd":"<abs cwd>","parent":null}
```

`parent` is the source session **id** for a fork (§5.5), else `null`. Mirrors pi's
`SessionHeader` (`session-manager.ts:32-39`); τ omits pi's tree-rebuild fields it
doesn't use yet.

**Lines 2..N — entries.** Every entry carries:

```json
{"type":"<kind>","id":"<8-hex>","parentId":"<8-hex|null>","timestamp":"<iso>", …payload}
```

`parentId` points at the previous entry's `id`. τ is **linear** — it always
appends to the current leaf, so `parentId` is just the prior entry — but the field
is present so the on-disk format is already pi's tree shape; in-session branching
(§10) becomes a UI feature, not a format migration.

Entry kinds τ **implements**:

| kind | payload | purpose |
|---|---|---|
| `message` | `"message": <τ message dict>` | the core: one role+content message (system / user / assistant / toolResult), exactly the τ message shape the agent loop consumes |
| `model_change` | `"model": "<config key>", "backend": "<provider>"` | model at creation and on any switch; latest wins on load |
| `thinking_change` | `"level": "<off…xhigh>"` | reasoning level changes (τ has `--thinking`) |
| `session_info` | `"name": "<display title>"` | mutable session name; latest wins (so a session can be renamed after creation) |
| `compaction` | `"summary": "...", "firstKeptId": "<id>", "tokensBefore": <int>` | compaction marker (τ has compaction in agent-core) |

Entry kinds **reserved but not implemented** (present in pi; format-compatible so
adopting them later needs no migration): `branch_summary`, `label`, `custom`.
Per Fail-Early these are not written or read until the feature exists.

### 5.4 The `Session` class (replaces `Chat`)

`session_store.py` evolves; `Chat` → `Session`. The module stays Textual-free
(headless must import it without pulling in the TUI — `session_store.py:8`).

```python
class Session:
    path: Path | None              # None for an in-memory (ephemeral) session
    id: str
    cwd: str
    # reconstructed views:
    @property
    def messages(self) -> list[dict]: ...   # fold message entries → flat loop list
    @property
    def model(self) -> str: ...             # latest model_change
    @property
    def backend(self) -> str: ...
    @property
    def name(self) -> str | None: ...       # latest session_info

    # raw views (seam 2 — export + pi-faithful json need entries, not just messages):
    @property
    def header(self) -> dict: ...           # the line-1 header
    def entries(self) -> list[dict]: ...    # ordered raw entries (all kinds), unfolded

    @classmethod
    def create(cls, cwd: str, model: str, backend: str, *,
               system_prompt: str | None = None, name: str | None = None,
               id: str | None = None,                       # seam 1 → --session-id
               base_dir: Path | None = None) -> "Session":  # seam 1 → --session-dir
        # mkdir <base_dir or ~/.tau/sessions>/<dashed-cwd>; write header (id or uuid4);
        # append model_change; append a system `message` entry if given; emit
        # session_start (seam 3).

    @classmethod
    def create_in_memory(cls, cwd: str, model: str, backend: str, *,
                         system_prompt: str | None = None,
                         name: str | None = None) -> "Session":
        # seam 1 — ephemeral mode (pi createInMemory, session-manager.ts:1430):
        # path=None, entries held in a list, append_* no-op the disk flush — one
        # API serves persisted and unpersisted runs. → --no-session.

    @classmethod
    def load(cls, path: Path) -> "Session": ...   # stream + reconstruct; emit session_start (seam 3)

    @classmethod
    def fork(cls, source: "Session", cwd: str, *,
             base_dir: Path | None = None) -> "Session": ...   # §5.5; emit session_before_fork (seam 3)

    def append_message(self, message: dict) -> str: ...   # append, flush, return entry id
    def append_model_change(self, model: str, backend: str) -> str: ...
    def append_thinking_change(self, level: str) -> str: ...
    def append_session_info(self, name: str) -> str: ...
    def append_compaction(self, summary: str, first_kept_id: str, tokens_before: int) -> str: ...
    # emits session_before_compact (seam 3)
```

**Append-on-message** is the key shift from the current "serialize the whole
object at the end" model (`Chat.save()`, `_persist_session:284`): both the TUI and
headless funnel every produced message through `append_message`, which writes one
line and flushes. The system prompt is stored as the first `message` entry
(role `system`), so reconstruction is uniform (fold all `message` entries in
order). pi parity: `SessionManager.appendMessage` (`session-manager.ts:950`).

### 5.5 Fork semantics

`Session.fork(source, cwd)` creates a **new file** whose header `parent` is the
source's id, **copies the source's entries** into it (self-contained — no load-
time chaining across files), then appends new turns. The source file is never
touched. This matches pi's cross-file lineage (`parentSession` in the header,
`session-manager.ts:38`) while keeping τ's loader trivial (one file = one full
transcript). The `+1.0s` same-second collision hack (`headless.py:329`) is
**deleted** — uuid4 in the filename makes it impossible.

### 5.6 No migration

The new store reads **only** `~/.tau/sessions/`. Existing `~/.tau/chats/*.json`
are abandoned (decision 1) — not read, not auto-converted. The old directory is
left in place; the maintainer may delete it. Per Fail-Early, τ does **not**
fabricate a `cwd` for legacy sessions (they have none) — there is no "guess the
directory" path.

### 5.7 `SessionInfo` — the picker's lightweight reader

A streaming reader that extracts list metadata **without** building agent
messages, so a directory of hundreds of sessions lists fast. Mirrors pi's
`SessionInfo` / `buildSessionInfo` (`session-manager.ts:170-184, 589-667`):

```python
@dataclass
class SessionInfo:
    path: Path
    id: str
    cwd: str
    name: str | None        # latest session_info
    created: datetime       # header.timestamp
    modified: datetime      # last entry timestamp (else header.timestamp) — explicit, not file mtime
    message_count: int      # count of user/assistant `message` entries
    first_message: str      # first user message text (display + search)
    last_message: str       # last user/assistant text (ellipsised in the row)
    parent: str | None      # header.parent (fork lineage)

    @classmethod
    def read(cls, path: Path) -> "SessionInfo | None": ...   # None on parse error (skip, Fail-Early at list edge)
```

`modified` comes from the **last entry's timestamp**, not file mtime — accurate
and copy-safe (pi: `session-manager.ts:589-667`).

### 5.8 Listing & scoping

```python
def session_dir_for_cwd(cwd: str, base_dir: Path | None = None) -> Path: ...   # base_dir: seam 1
def list_sessions(cwd: str | None = None, base_dir: Path | None = None) -> list[SessionInfo]:
    # cwd given  → list that one dashed-cwd dir (cheap: already partitioned)
    # cwd None    → walk all dashed-cwd dirs under <base_dir or ~/.tau/sessions/>
    # sorted by .modified desc
def most_recent(cwd: str | None = None, base_dir: Path | None = None) -> Path | None: ...   # pi: findMostRecentSession (session-manager.ts:538)
```

cwd scoping falls out of the directory layout for free — "current directory only"
is just *list that one dir*, no per-file header scan. pi parity: cwd-filtered
`findMostRecentSession(dir, cwd)` (`session-manager.ts:538-558`).

## 6. The picker — a Textual-native modal

`SessionPickerModal(ModalScreen[Path])` built on **`DataTable(cursor_type="row")`**.
The research is unambiguous that `DataTable` beats `OptionList` (faked per-row
columns that don't align) and `SelectionList` (multi-select, wrong message) — it
is the only one with true aligned columns and a clean `RowSelected.row_key.value`
→ session path. Targets **Textual 8.2.7**.

**Columns** (decision 3 — no tokens): `Title / last message` · `Updated`
(relative, e.g. "5m", "2d") · `Msgs`. This is the near-universal minimum the
research found across every tool with a real picker.

**Behavior:**

- Loaded by a `@work(thread=True)` worker (file I/O is blocking) → sets a
  `reactive[list[SessionInfo]]` → `watch_` repopulates the table. UI thread never
  blocks; `call_from_thread`/reactive marshals the update.
- `Enter` → `self.dismiss(path)` (typed `ModalScreen[Path]`, no `await` on
  dismiss). App receives the path via `push_screen(modal, callback)` **or**
  `await push_screen_wait(modal)` inside an `@work` — one path chosen
  deliberately, not both (Fail-Early: no try/fallback between control flows).
- `Tab` toggles **current-cwd ↔ all** (re-runs the loader with `cwd=` / `cwd=None`).
  pi parity: the picker's Current/All toggle (`session-selector.ts`).
- `/` filters via `textual.fuzzy.Matcher` over `name`/`first_message`/`last_message`.
- `Esc` cancels (built-in `dismiss` with no result).
- `Footer()` renders the key hints from the screen's `BINDINGS`; `DEFAULT_CSS`
  (auto-scoped since Textual 0.38) centers the dialog (`align: center middle`).

**Loading a choice:** reuse the existing load path — the modal's chosen `Path`
flows into the same `Session.load(path)` → render that the sidebar's
`ChatSelected` already drives (`app.py:283`). One loader, two entry points.

Rename (`Ctrl+R` → `append_session_info`) and delete (`Ctrl+D`) are pi-parity
picker actions (`session-selector.ts`); include them in Phase B as sub-modals if
cheap, else defer to §10. Git-branch column and threaded fork display are
explicitly **deferred** (§10).

## 7. Command unification — one action, three surfaces

The cleanest prior art is VS Code: **register a command once; the CLI flag, slash
command, and palette entry are thin bindings to the same handler** with identical
argument grammar (VS Code Commands API; clig.dev naming discipline). pi and
Claude Code keep `--resume`/`/resume`/named-resume coherent the same way. τ has an
advantage neither has: Textual *gives* us a command palette, so the registry is
the spine, not a bolt-on.

```python
@dataclass
class Command:
    id: str                       # "resume", "new", "fork", "rename", "delete"
    title: str                    # "Resume session…"
    handler: Callable[..., Awaitable[None]]
    arg_grammar: str | None       # "<ref>" — shared by /resume and --session
    enabled: Callable[[], bool]   # drives palette/binding enablement
```

**The registry is dynamic (seam 4).** Commands are registered/unregistered at
runtime — *not* a fixed `resume/new/fork` enum — so later subsystems attach
without touching core: prompt templates (Tier 10) each register a `Command`,
theme-switch and trust (Tiers 10/8) register palette entries, and the RPC
dispatch (Tier 12) reuses the registry as its command table. Slash parsing lives
in **one** place with an explicit "unknown `/x` → pass the text through verbatim"
fallthrough (pi `prompt-templates.ts:280`), and a "register palette entries from
a list" seam lets a subsystem contribute entries without editing
`get_system_commands`.

Three adapters over the **same** registry:

- **Command palette (Ctrl+P):** a `textual.command.Provider` over the registry
  yields `Hit`/`DiscoveryHit`s whose `command` is `partial(handler)`; registered
  via `App.COMMANDS`. The palette is the discoverable index of every action.
  (Ctrl+P is the palette key since Textual 0.77.)
- **Slash commands:** the chat input parses a leading `/`; `/resume <ref>` looks
  up `resume` and calls `handler(ref)`. Same grammar as the CLI.
- **CLI flags:** `--resume` runs the `resume` command at TUI startup (opens the
  modal). `--session <ref>` / `--continue` / `--fork <ref>` map to the same
  handlers headlessly. `/resume <ref>` ≡ `--session <ref>` — one grammar.

**`--resume` semantics change** (`cli.py`): today it is **always** rejected
(`cli.py` `main()` rejects it before TUI launch). New behavior:

- `tau --resume` (no `--print`) → launch the TUI and open `SessionPickerModal`.
- `tau -p --resume` → **still an error** — there is no interactive picker without
  a TUI (Fail-Early), with the existing pointer to `--continue`/`--session`. The
  rejection moves from *always* to *only with `--print`*.

Headless cwd scoping: `--continue`/`--session`/`--fork` resolve within the
**current cwd's** dir by default; a `--all-sessions` flag (pi's Tab-to-All
equivalent for headless) widens to every dir. (Flag name open; `--all-sessions`
recommended.)

## 8. Sidebar → closed by default

Flip the sidebar's mount state to closed (decision 4). It currently mounts visible
(`app.py:291`, `ChatSidebar`); change the default and keep the `Ctrl+B` toggle and
the widget itself. The modal + palette are now canonical. The sidebar's list is
re-pointed from `Chat.list_recent()` (global) to `list_sessions(cwd=current)` so
that *when* opened it is cwd-scoped like everything else. Repurposing it as a
VS Code-style panel is parked (§10).

## 9. Phased delivery

Each phase is independently landable, test-gated, and must keep the **Tier-5
quality gate** green (ruff clean + `mypy` 0; baseline: suite 1403/0).

### Phase A — Storage layer (JSONL + cwd partitioning)

**Forward-compat seams (approved 2026-06-22 — build them in now; each unlocks a
later ROADMAP tier with near-zero retrofit):**

- **Seam 1 — parameter slots.** `base_dir: Path|None=None` on
  `session_dir_for_cwd`/`list_sessions`/`most_recent`/`Session.create`/`fork`;
  `id: str|None=None` on `Session.create`; a `Session.create_in_memory` ephemeral
  mode (pi `createInMemory`, `session-manager.ts:1430`). → Tier 7
  (`--session-dir`/`--session-id`/`--no-session`).
- **Seam 2 — raw views.** `Session.entries()` (ordered raw entries, all kinds) and
  `Session.header`, alongside the folded `messages`. → Tier 9 (`--export`,
  pi-faithful `--mode json`).
- **Seam 3 — lifecycle events.** `Session.create/load/fork/append_compaction` emit
  `session_start`/`session_before_fork`/`session_before_compact`/`session_shutdown`
  (no consumer yet). → Tier 11 (extension hooks without a loop retrofit).
- **Seam 4 — dynamic command registry** (Phase C, §7): runtime register/unregister,
  one slash-parser with pass-through, a palette-registration seam. → Tier 10
  (templates/themes), Tier 8 (trust commands), Tier 12 (rpc dispatch).

1. Rewrite `session_store.py`: `Session`, `SessionInfo`, `session_dir_for_cwd`,
   `list_sessions`, `most_recent`; the JSONL header/entry schema (§5.3); cwd
   encoding (§5.1); uuid4+timestamp filename (§5.2); append-on-message (§5.4);
   `fork` (§5.5). Drop `Chat` and the flat format.
2. Update `headless.py`: `_resolve_selector:139` and `_select_chat:167` resolve
   against the current cwd's dir (then all dirs with `--all-sessions`); `run_print:186`
   creates/loads a `Session` and appends per message instead of building a list and
   calling `_persist_session`; delete `_persist_session:284` and the `+1.0s` hack
   (`headless.py:329`).
3. Update `app.py`/`backends.py` load + save call sites: sidebar `Chat` →
   `SessionInfo`; persistence appends to the active `Session`.
4. **Tests:** round-trip (create → append → load → `messages` match); cwd dir
   encoding; `list_sessions` scoping (cwd vs all); `fork` (new file, header
   `parent`, source untouched); `SessionInfo.read` (count, first/last, `modified`
   from last entry); resolver 0/1/many (Fail-Early). **Seams:** `base_dir`
   override, explicit `id`, `create_in_memory` (no disk flush), `entries()`/
   `header` views, lifecycle events emitted.

### Phase B — The picker modal

1. `SessionPickerModal(ModalScreen[Path])` + `DataTable(cursor_type="row")`,
   columns Title/last-msg · Updated · Msgs (§6).
2. `@work(thread=True)` loader → `reactive` → `watch_` repopulate; `Tab` cwd↔all;
   `/` fuzzy filter; `Enter`→`dismiss(path)`; `Esc` cancel; `Footer` + scoped
   `DEFAULT_CSS`.
3. Wire opening + loading into the existing `Session.load` → render path
   (`app.py:283`). Optional: `Ctrl+R` rename / `Ctrl+D` delete sub-modals.
4. **Tests:** modal returns the chosen path; table populated from `SessionInfo`;
   cwd/all toggle re-queries; fuzzy filter narrows rows.

### Phase C — Command unification + sidebar default

1. `Command` registry + dispatch (§7); register `resume`, `new`, `fork`,
   `rename`, `delete`.
2. Textual `Provider` over the registry → Ctrl+P entries; `get_system_commands`
   for the simple ones.
3. Slash-command parsing in the chat input → registry dispatch; `/resume <ref>` ≡
   `--session <ref>`.
4. `cli.py`: `--resume` opens the modal in the TUI, errors only under `--print`;
   add `--all-sessions` for headless global scope.
5. `app.py`: sidebar defaults closed (§8), list re-pointed to `list_sessions(cwd)`.
6. **Tests:** registry dispatch; `/resume` parsing; `--resume` TUI-opens vs
   headless-errors; palette provider yields resume/new/fork.

## 10. Non-goals & parked "Tau identity" items

**Non-goals (Fail-Early — not built until the feature exists):**

- No migration of `~/.tau/chats` (decision 1); the old dir is abandoned, never
  read, no fabricated cwd.
- No in-session branching UI — the format reserves `parentId`/the tree; the
  feature is deferred.
- No token/cost in the picker (decision 3).
- No git-branch column or branch filter yet.
- No `TAU_CODING_AGENT_SESSION_DIR` env override yet (reserved name only).

**Parked under "Tau identity" (deferred, deliberately divergent — decision 4):**

- Repurpose the sidebar as a VS Code-style panel (the reason it is kept, not
  retired).
- Token/cost **preview pane** in the modal (right-hand detail; reconciles "show
  tokens" with row scannability without a token column).
- Git-branch column + branch filter (Claude Code parity; coding-native).
- Live-refresh picker: a `reactive` + worker that updates the list when a headless
  run writes a session in the same cwd.
- Command-palette-as-spine: every action (model switch, thinking level, compaction)
  a registry command, exposed identically as slash + palette + flag.
- Threaded fork display (pi's `parentSession` tree, `session-selector.ts`).

## 11. References

**pi (source of truth, `~/Development/pi/packages/coding-agent`):**

- `src/core/session-manager.ts` — storage: cwd dir encoding `:438-442`; header
  `:32-39`; `SessionInfo` `:170-184`; `newSession` `:824-849`; filename `:845`;
  `appendMessage` `:950`; `findMostRecentSession` `:538-558`; `buildSessionInfo`
  `:589-667`; `listSessionsFromDir` `:713-744`.
- `src/cli/session-picker.ts`, `src/modes/interactive/components/session-selector.ts`
  — the `--resume` picker (Current/All toggle, threaded forks, rename/delete).
- `src/cli/args.ts` — flag surface; `src/core/config.ts:481-482` — session dir var.

**τ (current tree):**

- `tau-coding-agent/src/tau_coding_agent/session_store.py` — `Chat` `:25`,
  `save` `:42`, `list_recent` `:57`.
- `.../headless.py` — `_resolve_selector:139`, `_select_chat:167`, `run_print:186`,
  `_persist_session:284`, collision hack `:329`.
- `.../app.py` — `ChatSidebar:291`, `ChatListItem:270`, `ChatSelected:283`.
- `.../cli.py` — session flag group; `.../backends.py` — `TauBackend.stream_chat`.

**Textual 8.2.7:** `ModalScreen[T]` + `dismiss`/`push_screen_wait`
(textual.textualize.io/guide/screens); command palette `Provider`/`Hit`/
`DiscoveryHit`/`SystemCommand`, Ctrl+P since 0.77 (.../guide/command_palette);
`DataTable(cursor_type="row")` (.../widgets/data_table); `@work(thread=True)` +
`reactive` (.../guide/workers, .../guide/reactivity); `BINDINGS`/`check_action`
(.../guide/actions); scoped `DEFAULT_CSS` (.../guide/widgets).

**This repo:** `docs/CLI-PLAN.md` (flag plan), `docs/COMMAND_LINE.md` (CLI status),
`docs/tau-coding-agent.md` (package design).
