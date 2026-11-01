# D18: Session catalog: where sessions live and how a frontend finds one

Status: accepted (2026-11-01), every Open question as recommended; proposed 2026-10-31; built
2026-11-01 (differences from this text, all in the code's favour: the shared start is
`tau/session_start.py` and the catalog `tau/sessions.py`, so the catalog has no `Driver`
dependency; the start takes the D20 seam as a `system(cwd, loaded)` callable and `-r`'s picker as
`pick_session`; `list_sessions(sessions, *, cwd=None)` lists a row with an `error` in every
listing, since such a row has no `cwd` to match; the prefix read is `first_message(directory)`, in the
native module and not exported by `tau`;
flag combinations the ADR forbids are a `ConfigError` from `SessionFlags`, not an `argparse`
error; a name is stored stripped, at every place a name is set (a new session and a fork too), and is
refused when empty or when it holds a `/` or `:` (a ref would read it as a path or a `:NODE`);
a session named during the run (`/name`) counts as used, so `stop` does not delete it;
`tau sessions name REF` with no name prints nothing and exits 1;
`select_model` runs for a new session only: a continued or forked session's `Config` node
already names its model (D13), so `-c` needs no `default_model`, and `--model` on one is a
`ConfigError` naming `/model`; for an existing session the start takes the lease right after the
`chdir`, before the config, trust and extensions, and gives it back if any later step fails; a
fork reads the model from its copy before it makes the directory; a program that passes its own
`system` text with a prompt flag gets a `ConfigError`; `parse_ref`, `choose_session`,
`SessionChoice` and `export_markdown` are not public (`to_markdown(nodes, of=info)` writes the
export header); the `tau` and `tau -p` programs that call the start are Phase 8's). Phase 7, "Session
catalog" (`docs/plan.md`). Amends D9, D12
and D17 (see Amends). Answers D20's Open 4 and D21's Opens 1 and 2. Points marked
"(review, 2026-10-31)" were decided in review; they stay proposals until the owner accepts
the ADR.

## Context

Every frontend has to pick a session before it can start: a new one, the last one in this
directory, a named one, or a copy of another. Today nothing does this. `examples/chat.py`
opens `$TAU_HOME/chat-session/`, or creates it if it isn't there.

**What exists, checked in the code on 2026-10-31:**

- A session on disk is one directory holding `log` and `scratchpad`, plus `dropped-<offset>`
  after a cut (D12). `Session.create(config, system, now, directory)` refuses a directory that
  already has a log (`Exists`). `Session.load(directory)` takes the lease, a non-blocking
  `flock` on `log`, so a second opener gets `Leased`.
- Nothing names a session or records its working directory or where it came from.
- `Session.create` writes the `Config` root and the `System` node and opens cursor 0 there
  (Q5). `append(...)` syncs on return (one fsync per call); `Store::create` writes a whole
  in-memory session with one sync. The log is synced before the scratchpad is replaced (D12).
- The glossary retires "catalog"; this ADR keeps the plan's title. Its **fork** is "a new
  session copying `walk(n)` from another"; **copied_from** is a same-session per-node list.

**What 0.15 users relied on** (`git show v0.15.0:...`, `session_store.py`,
`session_catalog.py`, `cli.py`, `headless.py`, `session_picker.py`, `export.py`):

- Files at `sessions/<dashed-cwd>/<iso-time>_<uuid>.jsonl` with a header (`id`, `cwd`,
  `timestamp`, `parent`), and no index: every listing re-parsed every file.
- `-c` (newest in the cwd), `-r` (picker over the cwd, Tab for all), `--session REF` and
  `--fork REF` (path, id or unique prefix, searched in the cwd), `-n NAME` and `/name`,
  `--no-session`, `--session-dir`. A second process opening a held session got a read-only
  copy. A file was not written until the first append. Markdown export was a library call.
- tau-code reads the `list_sessions` fields `session_id`, `name`, `title`, `message_count`,
  `modified`, `parent`, `cwd`, `error`; calls `new_session {cwd}`, `fork {session_id,
  cursor_id, at}` and `attach {session_id or prefix}`; and names sessions through `/name`.

## Decision

### 1. Where sessions live

- **The sessions directory** is `$TAU_HOME/sessions`, or `--session-dir DIR` for one run
  (a flag only; `TAU_HOME` already moves everything). It is created with mode `700` when the
  first session is.
- **One subdirectory per session, named by its id**: the UTC creation time plus 6 random hex
  digits, `20261031-142233-7f3a9c`. `ls` lists sessions in creation order, and a date works
  as a prefix. The directory is made with an exclusive `mkdir`, mode `700`. A collision is
  `Exists`, with no retry: 16M ids per second.
- A session directory holds `log`, `scratchpad` and `dropped-*`, all the store's (D12,
  unchanged), and **`info.json`**, which belongs to the catalog. It is written whole (temp
  file, fsync, rename, fsync the directory) before the log is created:

  ```json
  {"cwd": "/home/john/Development/tau", "name": "rewrite the store",
   "forked_from": {"session": "20261030-091502-1c2d3e", "node": 41}}
  ```

  `cwd` is required; `name` and `forked_from` are optional. Core and the store never read it,
  so a session is still a pure function of its log and scratchpad (I7).
- **A session has one working directory** (answers D20 Open 4): `info.json`'s `cwd`, the
  resolved cwd at creation, never changed. **The program changes its own working directory to
  it** (`chdir`) as the first step after resolving the session (§5), before it loads config or
  extensions (review, 2026-10-31). From then on the base tools, the project search, trust
  (D13), extensions, path completion and anything else that asks "where am I" agree, because
  there is one answer. One session per process (D21 §1) makes that safe. If the directory is
  gone, opening fails and names it. `tau export` and `tau sessions` still work.
- Anything in the sessions directory that is not a directory is ignored. A directory that is
  not a readable session (no `log`, a missing or bad `info.json`, a bad header) is listed as a
  row with an `error`, never skipped silently..
- **`--no-session`** creates the session in memory (`directory=None`): never listed, and not
  combinable with `-c`, `-r`, `--session`, `--fork`, `--session-dir` or `-n`.

### 2. Listing: a scan, no index

Listing reads the sessions directory every time. Each row (**`SessionInfo`**: `id`, `path`,
`cwd`, `name`, `forked_from`, `updated`, `first`, `error`) costs one readdir name, one small
read of `info.json`, one stat of `log` (`updated`, the mtime, which every synced write moves),
and, only for rows with no `name`, a read of the log from the start up to the first `User`
node (`first`; no lease). That prefix is the `Config` root and the `System` prompt, a few kB,
so a list does not grow with session length: 1,000 sessions are about 1,000 small reads and
stats, milliseconds warm. An index would be a second source of truth that processes must lock
and that goes stale. It is not built.

- **Dropped from 0.15:** `message_count` and `last_message` (they need a full read of the log;
  `updated` is "last written"). The picker's count column goes, and so does tau-code's.
- `list_sessions(where, cwd)` keeps rows whose `cwd` equals the resolved cwd exactly; `None`
  lists all, newest `updated` first. The title a frontend shows is `name`, else `first` cut to
  one line, else `(no messages)`.
- `tau sessions [--all] [--json]` prints the rows: id, `updated` as an age, the title, and
  the `cwd` with `--all`, so `cut` and `awk` work. **`--json` prints one JSON object per line**
  (review, 2026-10-31), with the fields of `SessionInfo`.

### 3. Names

- A name is optional text in `info.json`, not necessarily unique, stored stripped; it cannot
  be empty or hold a `/` or `:` (a ref would read those as a path or `:NODE`), except that an
  empty string removes a name. No model sees it.
- It is set by `-n NAME` (names a new or forked session, renames an opened one), by `tau
  sessions name REF [NAME]` (without NAME it prints the name), and by **`/name [NAME]`**, the
  one command of the built-in extension **`session_name`**. **The startup adds `session_name`
  after `load_extensions`** (review, 2026-10-31), for a session that has a directory, built
  with that directory. It is not a D19 source: it has no key, `load` does not govern it,
  `--no-extensions` does not remove it, and `tau ext list` shows it as `always`. Over the wire
  `/name` is an ordinary command, which is how tau-code already names sessions.
- **Renaming takes no lease.** A rename replaces `info.json` whole and atomically, and `cwd`
  and `forked_from` never change, so the only thing two concurrent renames can lose is the
  other's name: the last wins. No event announces it; another client's title is stale until it
  lists again.
- **Auto-naming (0.15's example 42) is dropped**: the fallback title is the first message.

### 4. Refs

```
REF     = SESSION [ ":" NODE ]
SESSION = a path containing "/"  |  an id  |  a name  |  a unique id prefix   (tried in that order)
NODE    = a node id, in decimal
```

- `:NODE` is read only when the REF ends in `:` and digits. A REF searches the whole sessions
  directory, not just the cwd. If nothing matches, or a name or prefix matches more than one
  session, the error is **`UnknownSession`** (a `LookupError`), listing the candidates with
  their `cwd` and `updated`.
- `--session REF` takes no `:NODE`; the error says to use `--fork REF:N`.

### 5. Opening and continuing

- **`-c` / `--continue`** opens the newest session (by `updated`) whose `cwd` is this cwd.
  With none it is an error naming the cwd and the sessions directory. Unlike 0.15, it works
  with any frontend, not just `-p`.
- **`-r` / `--resume`** shows the picker (Phase 8) over the cwd's rows, Tab for all (`-p -r`
  is an error). **`--session REF`** opens that session. These, `--fork` and `--no-session`
  cannot be combined with one another.
- **A leased session is an error, never a substitute.** If `-c`'s newest session is held by
  another process, `tau` says so, names the session and suggests `--fork ID`; it does not
  open the next one. D21 decides whether a second `tau` attaches to a `tau serve` holder.
  0.15's read-only second view is dropped: other viewers are clients of the one writer.
- **Which cursor (the first cursor).** The frontend shows, or `-p` sends to, the open cursor
  with the lowest id. If none is open, it is the closed cursor with the lowest id, which is
  reopened (`reopen`: a new id with the old labels), as `examples/chat.py` does today. Fork
  and export make the same choice without writing: the open cursor's `at`, or the closed
  one's last node. Choosing among the others is the frontend's job (D16, D17).
- **The shared start** (one function, so `tau`, `tau -p` and `tau serve` agree; D21 Open 1
  asks D18 to own it, and this ADR does):
  0. Parse the session flags and resolve them to *new*, *existing (path)* or *fork (path,
     node)*. This writes nothing and needs only the sessions directory. Relative paths in the
     flags (`--session-dir`, a path REF, `-e PATH`) are made absolute here, against the
     directory the user typed them in.
  1. **`chdir` to the session's `cwd`** (review, 2026-10-31). For a new session that is the
     process's own cwd, symlinks resolved. For an existing session or a fork it is the
     session's recorded `cwd`.
  2. Run D19's steps (`load_config`, `load_extensions`, `select_model`), then add
     `session_name` (§3). For an existing session the lease is taken between steps 1 and 2:
     `Leased` is said before a trust question or an extension's import side effects, and the
     lease is given back if any later step fails.
  3. Assemble the prompt for *new* only (D20 refuses the prompt flags on the other two).
  4. Create, load or fork (D19's step 4), then start the `Driver`.
- **A session that was never used is removed.** At exit, if the program created the session
  in this run and it has only the nodes it was created with, no pending item and the name it
  was created with (naming it during the run, `/name`, counts as using it), the program
  deletes the directory while still holding the lease, then closes the session. Starting `tau`
  and quitting leaves nothing behind, and `-c` does not land on an empty session. A crash can
  leave one; it is listed as `(no messages)` and removed with `rm -r`.

### 6. `--fork REF`: a copy of one walk

- `--fork SESSION[:NODE]` creates a **new session** whose tree is a copy of `walk(NODE)` in
  the source (without `:NODE`, the first cursor's walk). Every node is copied as a record:
  body, `by` and `time` verbatim, parent links rebuilt, so node *i* of the new session is node
  *i* of that walk and ids are `0..k-1`. `delivers` is emptied, because item ids belong to one
  session.
- **Not copied:** other branches, detached roots, cursors, labels and pending items. The new
  session gets one cursor, 0, at the copied tip with auto-step off, as `Session.create` does.
- **Provenance is recorded once, not per node**: `info.json`'s `forked_from` is `{session,
  node}`, and walk order locates every copied node. A per-node `copied_from` would repeat the
  pair k times and dangle once the source is deleted. `copied_from` stays what the glossary
  says: same-session, for compaction.
- **It copies, never references**: the fork stands alone, and opening it never touches the
  source's lease. **Its working directory is inherited** (Open 1): the copied `System` node
  already names the source's directory (D20 §3), and the start (§5) changes into it.
- `-n` names the fork. D20 refuses the prompt flags with `--fork`; `-m` (Phase 8) applies as
  to a continued session.
- **Built in Rust, in one call.** Core gets one constructor, a session built from another's
  walk ending at a node (`NoSuchNode` if absent); the bindings expose `source.fork(node, now,
  directory) -> Session`, written with one sync through the store's create path. It works on a
  loaded session, a read copy (§7) or the live session. Built from Python appends it would
  cost one fsync per node.

### 7. Reading without the lease

- **`Session.read(directory)`** returns an in-memory copy as of the last sync. It takes no
  lease, cuts nothing and writes nothing (no `dropped-*`). The store reads the scratchpad
  **first** and then the log; D12's order means the log just read is at least as new as the
  scratchpad, so replay drops whatever the newer log delivered, as load does. A torn record
  being appended by the holder ends the scan, as on open.
- Like any in-memory session it accepts changes and never saves them. Fork and export use it
  on a session another process holds.

### 8. Export to Markdown

- `tau export REF [-o FILE] [--thinking]` writes one walk (REF's `:NODE`, else the first
  cursor's) as Markdown, to stdout by default. The library function is **`to_markdown(nodes,
  *, thinking=False, of=None) -> str`**, pure over the nodes (with `of`, the session's row, it
  also writes the header), so a server or a test calls it with no
  session.
- **Deterministic**, so it can be diffed: no export time or node times. The header holds the
  title (§2), the id, `cwd`, the exported node and `forked_from` if any.
- Thinking appears (as a blockquote) only with `--thinking`; 0.15 had it on, but it is long
  and some providers sign or hide it. `Custom` nodes are left out: they never reach a model.
  Each fence is one backtick longer than the longest run in its content, so a tool's output
  cannot break out of its block.
- The tree is not exported: Markdown is linear, so export each branch by `REF:NODE`. HTML
  export stays post-0.16 (`plan.md`).

**Tests will pin the per-node layout:** `System` under `## System` inside `<details>`; `User`
as `## User` (`## User (by gamemaster)` when an extension wrote it, D17); `Assistant` with
text parts verbatim, each call a line naming the tool and call id plus a fenced block of its
`args_raw`, and a stop other than `End` or `Tools` as an italic line; `Results` as one fenced
block per outcome headed with the tool's name and `ok`, `error` or `interrupted`; `Config` as
one italic line.

## Amends, files and format impact

- **D9**: τ 0.16 sessions are directories under `$TAU_HOME/sessions` (or `--session-dir`).
- **D12**: a session directory also holds `info.json`, which the store never reads; the store
  gains a lease-free read (§7) and a prefix read. `log`, `scratchpad` and `VERSION` are
  unchanged.
- **D17**: "a frontend that opens a session with no open cursor restarts one" becomes the
  library's first-cursor rule (§5).
- **D20** is written to match (its `--fork` row names `forked_from`; its Open 4 is answered by
  §1). **`docs/from-0.15.md`**: `--fork`, `--session-dir` and Markdown export become planned;
  message count, last message, the read-only second view, the dashed-cwd layout and
  auto-naming become dropped.
- **New file:** `info.json`, unreleased and so unversioned (D13). A missing, unparsable or
  `cwd`-less one makes the row an error and the session unopenable. **No migration:**
  development sessions without it (`chat-session/`) are listed as errors; delete them.

## Alternatives rejected

| option | why not |
|---|---|
| An index file (`sessions.json`); grouping by encoded cwd (0.15) | §2: a second source of truth that goes stale. The encoding is lossy and breaks when a project moves. |
| The name as a log record | Listing would read whole logs, renaming would need the lease, and core would carry a field it never reads. |
| `cwd` and provenance in the root `Config` | `Config` is per-branch settings; a `cwd` is not per branch, and a fork copies the root verbatim. |
| A read-only flag on `Session` | A check in about 20 methods. A read copy is an in-memory session, already a mode. |
| Defer creating the file until first use (0.15) | Needs a way to persist an in-memory session later; removing a never-used one is ~10 lines. |
| Pass the session's `cwd` to each consumer instead of `chdir` | Every consumer, extensions and a third-party `open("x")` included, would have to be told. One `chdir` makes them agree (review, 2026-10-31). |

## Open (accepted as recommended, 2026-11-01)

1. **A fork's working directory.** Recommendation: inherit the source's `cwd` (§6), so the
   copied `System` node stays true. The alternative takes the directory the fork is made from
   and writes a `System` note ("the working directory is now /b"), as D20 Open 4's fallback
   suggests: it fits "try this in a git worktree", but is a second way to change where tools
   run.
2. **List by exact cwd or by project root.** Recommendation: exact cwd, as in 0.15. Grouping
   by D13's project root would need the project search before the session is chosen, which §5
   orders the other way.
3. **`--resume` versus the glossary's `resume`** (D4's term for the state a crash mid-batch
   leaves). Recommendation: keep `-r/--resume`, and rename D4's glossary heading to **crash
   mid-batch**. No code uses the word.
4. **Removing never-used sessions at exit** (§5). Recommendation: yes. The alternative keeps
   them as `(no messages)`, so `-c` can land on an empty session.
5. **Names as refs.** Recommendation: yes, an exact and unique name, after ids (§4); the
   alternative (ids and paths only) avoids the ambiguity error.

**Interfaces needed from siblings:**

- **D19**: the loader runs after the `chdir` and before a session exists; `session_name` is
  added by the startup, not loaded (§3).
- **D20**: `system_prompt(cwd, ...)` runs for a new session only, in its `cwd`.
- **D21**: the shared start with `tau serve` taking the same session flags; `tau sessions
  --json`; an id safe in a file name (22 characters; unique within a sessions directory, and
  across two `--session-dir`s only by chance, which matters only if both share a
  `TAU_HOME/serve`). `/name` reaches remote clients as a command; export stays local. A
  `Leased` open is the error of `tau serve`'s start. tau-code loses `message_count`;
  `forked_from` replaces its `parent`.

## Glossary additions

- **sessions directory**; **session id** (`YYYYMMDD-HHMMSS-xxxxxx`, UTC, 6 hex): the session
  directory's name.
- **`info.json`**; **`SessionInfo`** (a listing row: `id`, `path`, `cwd`, `name`,
  `forked_from`, `updated`, `first`, `error`).
- **a session's working directory**: `info.json`'s `cwd`, fixed at creation; the program
  changes into it at start.
- **session name** (never seen by a model; `/name`; the `session_name` extension the startup
  adds); **ref**, **`UnknownSession`**; **first cursor**; **shared start**.
- **fork** (refined): `Session.fork(node, now, directory)`; **`forked_from`**.
- **read copy** (`Session.read`); **never-used session**; **`tau sessions`**, **`tau
  export`**, **`to_markdown`**.

## Build (rough lines, tests separate)

| item | lines |
|---|---|
| core `Session::fork`; store `read` and the prefix read; `tau-py` bindings and stub | ~140 |
| `tau/sessions.py`: directory, ids, `info.json` (atomic), list, refs, newest, create, fork, rename, first cursor, never-used removal, the shared start (with the `chdir`) | ~250 |
| `tau/markdown.py` (`to_markdown`, fence sizing); `session_name` | ~150 |
| CLI: `tau sessions [--all] [--json]`, `tau sessions name`, `tau export`; `examples/chat.py` switched | ~120 |
| tests, Rust: a fork's tree equals the walk, reload equals (I7), `read` with a newer scratchpad, a torn tail | ~120 |
| tests, behavior via `tau.cli.main` and `Harness`: list, refs, `-c` with none or leased, first cursor, fork and `forked_from`, the `chdir`, rename while leased, never-used removal, export golden files | ~370 |

**Not verified, inferred:** the listing cost (a bench can take a prefix-read case), the line counts.
