# The TUI opens its session at mount

Built 2026-10-18 (#42). The TUI opens a session before its first frame, and the
session is stored on its first append, so a TUI opened and quit leaves nothing.

## 1. What was wrong

Extensions load into a session. Until 0.14.0 the TUI created none until the
first submission (`action_new_chat`, called from `on_input_submitted`), so on the
startup screen `_extension_command_names()` returned `[]`:

- typing `/budget` (tau-router's command) made the popup say
  `/budget is not a command — this line goes to the model as text`;
- Tab completion offered no extension command;
- the command peek in `on_input_submitted` classified the line as a prompt. It
  still dispatched, because the session was created before `_start_turn` and
  `submit()` re-resolves under `expand_commands=True`. But the line went through
  `_bounce_if_locked` and `_expand_attachments` as a prompt does.

A second fault had the same root. Every TUI session was written when it was
created: `FileSessionCatalog.create` makes the directory, writes the header and
the opening chain. So **ctrl+N then quit left a file holding only a config entry
and a system prompt**. The timing probe below shows it: before, every run left
a directory and a file (`files=[2, 2, …]`). After, it leaves nothing.

## 2. A deferred session, not a cleanup at quit

`SessionCatalog.create_deferred` returns a session that can be read at once and
is written on its first append: the header, the held opening chain, then the
entry, in one go. Until then the catalog does not list it and nothing exists on
disk. `AgentSessionRuntime.new_session(persist=True, deferred=True)` reaches it,
and every session the TUI creates goes through it (mount, `action_new_chat`,
`action_clear_chat`).

The alternative, deleting the empty session at quit, was rejected under Fail
Early. It is a workaround: a crash, a kill or a power cut skips it and leaves the
litter anyway. Under deferral, nothing exists until there is something worth
keeping.

"The first real activity" means the first entry anything appends. That covers a
prompt, `/model` (a config entry), a rename (`session_info`), and an extension
command or hook that writes to the tree. An extension that appends in
`session_start` therefore writes the session at mount. That is the extension's
write, and the store does not second-guess it. None of τ's built-ins do it.

`create_deferred` is concrete on the ABC and raises `NotImplementedError`, so a
catalog opts in. `FileSessionCatalog` and `JmftsSessionCatalog` implement it.
`RemoteCatalog` refuses every create anyway (§4).

## 3. The two stores

**File store.** Entry ids are minted client-side, so deferral costs one flag.
`Session.create(deferred=True)` builds the chain with `path=None`, sets `path`
afterwards, and the first `_persist_entry` calls `_write_held`. The header
timestamp and filename are the *creation* time, not the time of the first write,
because that is when the session began. The listing's `modified` is the first
write.

**JMFTS store, and the trap.** An entry id here *is* a document id, which the
server assigns. A held entry cannot know its id before it is POSTed. So the
opening chain is held under `unwritten-N` ids, and **those stay the mirror's
ids for the life of the object**. `_alias` maps each one to its document id, and
the store translates at the HTTP boundary: a parent id, a cross-reference field
(`targetId`, `firstKeptId`, `fromId`, `copiedFrom`), a finalize target, and a
fork's path. The rejected alternative relabelled the mirror to the real ids once
they were written. That leaves any cursor holding a held id — the head's own
cursor, a compare cursor — pointing at an id the mirror no longer contains, and
its reads fold to nothing without an error. A reload reads real ids from the
server, which is consistent because the server never saw a held id.
`root_doc_id` is `None` until the first write. `enrich` says so and skips at
shutdown. A fork of an unwritten conversation writes the source first, because
the copy is read from the server and a fork is real activity.

## 4. Mount

`TauApp.on_mount` is `async` and awaits `_open_new_session(None)`. It is not a
worker. Textual dispatches `Mount` inside `batch_update()`, so the first frame
and the first keystroke both wait for the session. The alternative was a worker
with a lock: it paints sooner, but the hint is wrong for that window, and every
test that drives the app would race the worker.

Measured with `run_test()`, a real `TauBackend`, the median of 7 runs:

| | mount → ready | mount → session open |
|---|---|---|
| before, no extensions | 80 ms | 85 ms (session created explicitly) |
| after, no extensions | 92 ms | 92 ms |
| before, 4 example extensions | 91 ms | 101 ms |
| after, 4 example extensions | 84 ms | 84 ms |

The difference is inside the run-to-run noise (±10 ms). What is paid at mount is
what the first submission used to pay.

One visible change: the header subtitle on the startup screen now names the
session's model instead of `Ready`. Four TUI snapshots were re-recorded for that
line alone.

`--resume` still opens the picker after mount. Choosing a session replaces the
mount session, which was never written. Under `--connect` no session is opened
at mount, because the daemon owns sessions. The submission path still opens one
when none exists: that is the remote case, and a mount that failed (an unknown
`default_model`, which is notified). If opening fails again, the typed line
stays in the editor.

## 5. What is absent

- **The REPL head** (`tau --mode repl`) still calls `catalog.create` at startup,
  so a REPL opened and quit leaves a file. The fix is one argument
  (`create_deferred`). It was left out to keep this change to the TUI's
  lifecycle.
- **`--connect`** still has the original defect on its startup screen, before a
  daemon session is open: no extension commands in the hint or completion. The
  commands live in the daemon, and opening a daemon session at mount would
  create one there.
- **Double extension load on `--resume`.** The mount session loads extensions,
  then `on_chat_selected` builds a new backend and loads them again, as every
  session switch already did. #35 changes when extensions load, which is the
  place to revisit this.
- **No contract-suite coverage of `create_deferred`.** The file and JMFTS
  behaviour is tested in `test_session_store.py` and `test_store_deferred.py`.
  The shared `SessionCatalogContractTests` would need `InMemorySessionCatalog` to
  implement it first.
