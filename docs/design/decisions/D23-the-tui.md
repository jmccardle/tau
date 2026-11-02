# D23: The TUI: `tau` opens a Textual app over a `Client`

Status: accepted 2026-11-02. Phase 8, stage 8C. Depends on D16 (clients, questions, forms), D17
(closing cursors), D18 (the catalog, the shared start), D21 (the wire) and D22 §8 (`open_client`,
attach). Answers `docs/from-0.15.md` §2.9's TUI rows.

## Context

`tau` with no subcommand should open the terminal UI, as in 0.15. The plan: reuse 0.15's
widgets, `tau.tcss` and themes, and write the app around them against `Client`.

**What exists (checked 2026-11-02).** No TUI code; `textual` is not a dependency. A frontend has
`open_client` (D22 §8: start or attach, the snapshot, the first cursor) and a `Client`: calls,
reads (`node`, `nodes`, `walk`, `children`) and one event stream (`CursorView`, `CursorGone`,
`Labels`, `Appended`, `Live`, `Notice`). `examples/chat.py` is a line frontend that already does
the hard parts: placing other authors' messages on the walk, questions answered as typed values,
`/` commands with `parse_line` and Tab, commands run as tasks.

**τ 0.15's TUI** (`tau-coding-agent/src/tau_coding_agent/`, tag `v0.15.0`) was about 13,000 lines.
`app.py` (4,239 lines) was one class holding one session, one `TauBackend`, one cursor and one
message list; every session switch rebuilt the `TauBackend` and reloaded the transcript. Around it:
- presentation that takes plain data (`dialogs.py`, `tau.tcss`, `themes.py`, most of
  `chat_widgets.py` and `editor_widgets.py`);
- widgets fed by 0.15 types (the session picker, the tree browser, the transcript);
- harness logic (`app.py`, `suspension_ui.py`, `steering.py`).

Its tests used a real `TauBackend` with `session._run_one_turn` replaced, `monkeypatch` and
`unittest.mock.patch`, all forbidden here (rule 4). 7 SVG snapshots used
`pytest-textual-snapshot`.

## Decision

### 1. Launch

- `tau [flags]` opens the TUI. Flags: D18's session flags (`-r` included), D19's (`--no-extensions`
  skips discovered sources only, D22 §1), D20's, `-m`, `--set` (D22 §1), `--theme NAME`, `--verbose` (D22 §7: the start report goes to stderr before the screen opens,
  so it is on the terminal after quitting). `--answer` and `--json` are usage errors: there is a
  person, and a screen.
- A positional message without `-p` is a usage error, as in 0.15. The first argument already
  chooses between subcommands and launch (D22 §1), and `tau export` would be a message or a
  command by spelling; revisit with use.
- **Order, one asyncio loop:** `-r` runs the picker first (§5) and becomes `--session ID`; then
  `open_client` with terminal prompts, so a trust question is asked on the plain terminal before
  the screen opens; then `await TauApp(...).run_async()` on the same loop as the driver; then
  `opened.close()`. The start's prompts are synchronous terminal prompts, so they cannot run
  inside the app (gap 4).
- **Attach** (D22 §8): a session held by a live `tau serve` opens as a `RemoteClient`. The
  app is the same code either way. On `ConnectionError` a remote app reconnects with the same id
  and rebuilds from the fresh snapshot; a local app whose driver failed shows the failure, keeps
  the transcript readable, and exits 1 when the person quits.
- `textual` becomes a runtime dependency, imported only when the TUI starts (`tau -p` does not
  pay for it), as `websockets` did in D21: `tau` failing with "install `[tui]`" asks no real
  decision.

### 2. The app's structure

- **`tau.tui`** is a public module (the fourth, beside `tau.cli`, `tau.commandline` and
  `tau.trace`; 8C amends CLAUDE.md's public module list and `docs/api/groups.toml` when it
  builds it),
  so tests can drive it (rule 4) and others can embed it. Public: `TauApp(client,
  cursor, *, welcome, snapshot, theme=None)` and `SessionPicker` (§5). Everything else is private
  modules under `tau/tui/`, each under the ratchet's 800 lines.
- **The app touches only its `Client`** (D16 §5): never `Driver`, `Session` or a provider.
- **One listener**, a worker running `async for event in client`, feeds a **`SessionView`**: a
  plain Python object (no Textual) holding what the stream said: each open cursor's
  `CursorView`, closed cursors, `Labels`, nodes by id (read or `Appended`), and the live layer
  of each watched cursor. Widgets render from it and post Textual messages; nothing else holds
  session state. It is tested without a screen.
- **The viewed cursor.** The transcript shows `walk(at)` of one cursor. A `CursorView` whose `at`
  extends the walk appends the new nodes; one that moves elsewhere (a go-to, another client's
  branch) re-reads `walk(at)`. The app `watch`es the viewed cursor only; switching cursors is a
  local lookup, a `walk` and a new `watch` (D16 §5).
- **The live layer** follows model.md §9's client rules: deltas drawn over the cursor, replaced
  by the node when it arrives, dropped when the view is not busy or `at` moved; tool progress
  keyed by `(node, call_id)` until the `Results` node.
- **Nodes as widgets:** `User` (the author's name when it is not this client, as `chat.py`
  shows `[gamemaster]`), `Assistant` (markdown text; thinking in a collapsed region; each call
  a tool box, its outcome folded in from the `Results` node by call id; usage from `meta`),
  `System` (collapsed, one line), `Config` (one dim line: "model: x"), `Custom` (`sample_error`
  as an error box; others one dim line). A stop other than `End` or `Tools` is a dim line.
- **Status:** the header shows the session's title (name, else first message), id and `cwd`;
  the status bar shows the viewed cursor's `model`, busy, auto-step, the last `ending` and
  `hook_error`, and one line per `shown` extension.
- **Cursor bar:** one tab per open cursor (id, labels, busy, a mark when it has an open
  question). It replaces 0.15's session sidebar and its flat stream strip.
- **Input.** Enter sends (`send`), busy or not: input is a scratchpad item (D2), and the pending
  items of the cursor (`CursorView.items`) are shown above the editor with their authors. There
  is no client-side queue (0.15's steering buffer): the scratchpad is the queue.
- **Keys**, kept from 0.15 where the meaning carries: Esc aborts the viewed cursor; Ctrl+C
  aborts if busy, else clears the draft, else quits on a second press; Ctrl+R folds thinking,
  Ctrl+T tool boxes; Ctrl+G the tree (§6); Ctrl+P the command palette; Ctrl+Z **edits the last
  message** (`go_to` the node before the last `User` node of the walk, and put its text in the
  editor: 0.15's rollback, as a branch). New: Ctrl+O the session picker, Alt+Left/Right the
  previous or next cursor.
- **Notices** are toasts, and are kept in a list the palette opens.

### 3. Questions and forms

Both are one form renderer from a JSON Schema (D16 §1's table: an `enum` is a list or buttons, a
boolean a switch, a string or number a text field read by `commandline.value`, a `list[T]` a
multi-select, a named type a field completed by `complete`). No branch on a command's or an
extension's name (0.15's style guide §2).

- **Questions are inline, not modal:** a question box docked above the editor, titled with the
  asking extension (gap 1), with the text, the field, Answer and Decline (Esc). Several open
  questions stack, oldest first. A question can wait for hours, and the person may need to read
  the transcript to answer it, so it never blocks the screen.
- `answer`'s `ValueError` is shown in the box, which stays; `LookupError` (someone else
  answered first) closes it quietly; a question `to` another connected client is shown read-only
  with its name.
- Questions on other cursors mark that cursor in the cursor bar; viewing it shows them.
- **Forms:** a command line missing arguments (`parse_line`'s `missing`), or a bare `/config
  EXTENSION` (the options `Form`, its defaults the values now, with a "save for every session"
  switch that is `configure(save=True)`), opens the same renderer as a dialog.

### 4. Commands and completion

- `/` opens the command popup from `commands()` (each `Form`'s name and first description line;
  the qualified `/ext:name` where two share a name).
- Tab is `complete_line(cursor, line)`, one call; inside a question or a form field it is
  `commandline.values` for that field's schema. On an `@` word of a plain message it is
  `complete(cursor, "path", prefix)` (D24 §5), since `complete_line` completes command lines only.
- Enter on a `/` line is `parse_line`: `UnknownCommand` warns and sends the line as text;
  `LookupError` for a shared bare name is shown and nothing is sent; `missing` opens the form;
  else `parsed.run` in a worker, so the person can answer a question the command asks. The
  result is a toast; `CommandFailed` and `Busy` are error toasts.

### 5. The session picker on D18's catalog

- `SessionPicker` (from 0.15's `session_picker.py`): rows from `tau.list_sessions`, newest first;
  columns title (D18 §2: name, else first message, else `(no messages)`), age and, in the "all"
  scope, `cwd`. 0.15's message count column is gone (D18 §2). Tab toggles this directory / all;
  `/` filters; Enter picks; Esc cancels. Rows with an `error` are shown, dimmed, and cannot be
  picked. A row held by a live server says "served".
- `tau -r` runs it as its own small app before the start (§1).
- **In the app** (Ctrl+O), picking a session, or Ctrl+N for a new one, stops cleanly (the
  never-used rule applies) and **re-executes** `tau --session ID` (or `tau`) with the same
  extension flags, in the same terminal. One process holds one session and one working
  directory (D18 §1, D21 §1); re-running the start in a live process would import a second
  project's extensions into the first one's interpreter.

### 6. The tree

- Ctrl+G opens the tree screen (0.15's `tree_browser.py`: `plan_tree_rows`, `ZoneTree`, the
  detail pane, the folding keys). The rows come from `nodes(start, limit)` in ranges, children
  from each node's parent; cursors' `at` and `Labels` mark rows (0.15 had no labels).
- Its actions are calls, no surgery: Enter **goes to** the node (`go_to` the viewed cursor; on a
  busy cursor that aborts its step, so it asks first); `o` **opens a cursor** there (`open`);
  `l` sets a label on the viewed cursor. 0.15's elide, branch-from-marks, copy and paste of
  subtrees are not carried (the plan sends them to commands or extensions).
- Branching in the transcript is the same act: go to a node, then send.

### 7. What carries over from 0.15

All paths under `tau-coding-agent/src/tau_coding_agent/` at `v0.15.0`. Each carried file names
its source in its commit (CLAUDE.md). "As is" still means rule 3's renames: `dialogs.py` says
`frame`, `themes.py` `entry`, and `tree_browser.py` `entries` and `backend` (about 90 uses).

| 0.15 | lines | in 0.16 |
|---|---|---|
| `tau.tcss` | 488 | as is, less the rules of the widgets dropped below; no colour literal (the 0.15 test that checks this comes too) |
| `themes.py` | 703 | as is: `mocha` (default), `latte`, `gruvbox`, `ansi`, Textual's own adapted, user themes in `$TAU_HOME/themes/*.json`; its `tau_home` and config imports point at 0.16's |
| `dialogs.py`: `TauDialog`, `ChoiceDialog`, `TextDialog` | 226 | as is |
| `chat_widgets.py`: `MessageBox`, `ReasoningRegion`, `ToolBox`, `ExchangeBox`, `QuietCollapsible`, `MarkdownLineFormatter`, the formatters | ~1,100 | fed from nodes instead of 0.15 message dicts |
| `chat_widgets.py`: `ChatInput` | ~250 | its completion source becomes `complete_line` |
| `editor_widgets.py`: `CommandPopup`, `PendingInput`, `StreamStrip` | ~350 | the popup from `Form`s; pending items from `CursorView.items`; the strip's look becomes the cursor bar |
| `extension_ui.py`: `ExtensionStatusBar`, `WarningsMarker`, `ExtensionRequestBox` | ~200 | `CursorView.shown`; `hook_error`; the question box |
| `modals.py`: `ChoiceList`, `ChoiceField`, `_FieldForm` | ~250 | rewritten over JSON Schema as the one form renderer (§3) |
| `session_picker.py` | 373 | `SessionInfo` from `tau.list_sessions`; count column dropped |
| `tree_browser.py`: row planner, `ZoneTree`, zones | ~800 | data from `nodes`; intents cut to go to, open, label |
| `transcript.py`: `ChatDisplay`, `MessageList`, follow-tail scrolling, the render window | ~900 | presentation only; its event routing is rewritten on `SessionView` |

**Not carried:** `app.py`, `suspension_ui.py`, `steering.py` (harness logic); `ChatSidebar`
(the cursor bar and the picker replace it); `AttachmentBar` (D24 §5 drops it: `@file` is
expanded by a `pre_input` hook, and the `User` node shows what was sent); `ExtensionPanel`/`Host` (0.16 has `show` lines, no panels); `ReadOnlyBanner` (no read-only
view: `Leased`, or attach); `SystemPromptEditor` (the prompt is fixed at creation, D20),
`RollbackPromptModal` (Ctrl+Z is a go-to), `ExtensionChordScreen`, `ExtensionAuthorizationDialog`
(trust is `tau trust`); `compare_view.py` (planned, unspecified; several watched cursors fit it
later); `tagline.py` (`--fun` is dropped); `testing/`, `devshot.py` (the screenshot harness is
after 0.16.0).

### 8. What does not fit the 0.16 model

| 0.15 | 0.16 |
|---|---|
| one conversation per app, one message list | several cursors; the transcript is the viewed cursor's walk; the cursor bar shows the rest |
| steer/enqueue buffer, reclaimable lines | `send` while busy is a pending item, shown from `CursorView.items`; taking one back needs a call (gap 3) |
| one modal per question (`SuspensionScreen`) | questions with ids, inline, any number, on any cursor |
| "foreign streams" with dashed borders | nodes carry their author; another cursor's `Live` only when watched |
| fork the session from the app | a branch is a go-to and a send; a copy is `--fork` (re-exec, as §5) |
| extension panels and dialog kinds | `show` lines, questions and forms from schemas |
| read-only view of a held session | attach, or `Leased` |
| notifications only | `Notice` toasts plus a list |

### 9. Testing

- **Pilot against the `Harness`.** `TauApp(...).run_test(size=(120, 40))` with a client from
  `h.connect("me:tui")`, `FakeProvider` replies and `FakeTool`s; no network, no real keys.
  Tests live in `tests/tui/` and run in both `Harness` modes (local and remote), so the app is
  held to the contract over the wire too. They assert on the session (`h.nodes()`, `h.view()`)
  and on widget text, never on private attributes.
- **First cases:** type and Enter makes a `User` node by `Frontend("me:tui")` and shows the
  reply; Esc aborts a busy cursor; a `Literal` question is answered by a click and the tool
  proceeds; Decline gives the extension `None`; `/gr` Tab completes; a command with a missing
  argument opens the form; a second client's message appears with its author; a branch by
  another client re-reads the walk; switching cursors watches the new one; a `CutOff` remote
  client reconnects.
- **`SessionView`** has plain unit tests (events in, state out) through `tau.tui`.
- **Snapshots** (`pytest-textual-snapshot`, a dev dependency): a few fixed scenes (empty, a
  reply with tools, a question, the tree, the picker), animations off, reviewed as SVG diffs.
- What happens before the screen (usage errors, `-r` with no terminal, `Leased`, choosing to
  attach) is tested through `tau.cli.main`; from `open_client` on, the app is tested by pilot.

### 10. Stages

- **8C-1, the first usable cut:** launch, attach, `SessionView`, the transcript (message, thinking,
  tool boxes), live text, the editor with send, pending items and abort, `/` commands with
  popup, Tab, forms for missing arguments, inline questions, notices, the status bar, themes and
  `tau.tcss`, `--theme`. One cursor viewed: the first. Enough to work in all day.
- **8C-2, the model's shape:** the cursor bar and switching, labels, Ctrl+Z edit-and-resend, the
  tree screen, the session picker (`-r`, Ctrl+O, Ctrl+N by re-exec).
- **8C-3:** `/config` forms with save, a `/model` picker over `complete(cursor, "model", "")`,
  persisted theme (Ruling 3), the render window for long walks, snapshots, the guide page.

## Gaps in the `Client` contract and the shared start

The most important finding: **the contract is nearly enough.** Everything the TUI draws is in
the events and the reads. What is missing:

1. **Who asked a question** (also D22 gap 1): `OpenQuestion` has no extension name, so the
   question box cannot say "permissions asks". Add `OpenQuestion.extension`.
2. **The first cursor needs a `Session`** (D22 gap 2): an attached TUI cannot call
   `first_cursor`. `open_client` computes it from the snapshot.
3. **A pending input cannot be taken back.** No `Client` call removes an item; handles have
   `remove_item`. 0.15's reclaim (Up on an empty editor) has no counterpart. Not needed for 8C-1
   (Ruling 4: `Client.remove_item` arrives in 8C-2).
4. **The start asks on the terminal, synchronously** (`Prompts`, `PickSession`): it must run
   before the app takes the screen, and switching sessions in-app means re-executing (§5). Async
   prompts would allow both in-app, at the cost of an API change; not needed with re-exec.
5. **A local client has no `welcome`.** The header needs the session id and `cwd` either way;
   `Opened.welcome` (D22 §8) supplies it; with `--no-session` there is none, and the header shows
   `cwd` only. A rename by another client sends no event (D18 §3),
   so the title goes stale until the next start: accepted.
6. **`Notice` has no cursor**, so a sub-agent's `tell` cannot be placed on its cursor; it is a
   toast. Accepted for 0.16.

Not gaps: token usage is in `Assistant.meta`; the model list is `complete(cursor, "model", "")`;
the tree is `nodes` plus `Labels`.

## Alternatives

| option | why not |
|---|---|
| Port `app.py` and adapt it | Its state is one session, one cursor, one list; every concept in it is 0.15's. The widgets carry; the app is rewritten small. |
| The TUI reads `Driver`/`Session` when local | Two contracts; the remote one rots (D16). Attaching would be a second code path, as 0.15's `--connect` was. |
| Questions as modals (0.15) | Several can be open, on several cursors, for hours; the person needs the transcript to answer. |
| A client-side input queue (0.15 steering) | The scratchpad already queues input (D2), visibly to every client. |
| Switch sessions in the running process | One process, one working directory, one set of imported extensions (D18 §1). |
| `tau.tui` private, tested through `tau.cli.main` | Pilot needs the `App` object; rule 4 allows only public names. |
| `textual` as an extra (`ffwf-tau[tui]`) | `tau` alone would fail with an install hint; no real decision is asked. |

## Rulings (2026-11-02)

Decided as the drafts recommended, except where noted.

1. **`tau MESSAGE` without `-p`:** a usage error; not in 8C (§1).
2. **In-app session switching** is by re-exec (§5).
3. **Theme:** a top-level `tui` key in the user config, `{"tui": {"theme": "mocha"}}`, written by
   the app through `UserConfig` when the person picks a theme (τ owns `config.json`, D13);
   `--theme` for one run; the project config never sets it. The draft's `enter` key is dropped.
   An unknown top-level key is a `ConfigError` today (`_TOP_KEYS` in `_config_layers.py`), so 8C
   adds `tui` there and amends D13's top-level keys, as it amends CLAUDE.md's public module list
   for `tau.tui`.
4. **`Client.remove_item(cursor, item)`:** yes, in 8C-2, only for `Input` items authored by the
   calling client; `LookupError` once delivered.
5. **Snapshot tests:** a handful, in 8C-3, behind the pilot tests.

## Glossary entries proposed

- **TUI, `tau.tui`, `TauApp`**: the Textual frontend and its public module.
- **`SessionView`**: a frontend's picture of the session built from its stream (views, closed
  cursors, labels, nodes by id, live layers); no Textual.
- **viewed cursor**: the cursor whose walk the transcript shows and which the app watches.
- **cursor bar**; **question box** (inline, per open question); **form renderer** (one widget set
  from a JSON Schema, for questions, missing arguments and options).
- **`SessionPicker`**: the `-r` / Ctrl+O picker over `list_sessions`.
- **tree screen**: rows from `nodes`; go to, open, label.
- **edit last message** (Ctrl+Z): a go-to before the last `User` node, its text in the editor.
- **theme**, **`tui` config key** (`theme` only).
- **re-exec** (session switch in the TUI).
- Amended: **`OpenQuestion`** (`extension`), **Client** (`remove_item`).

## Build (rough lines, tests separate)

| item | lines |
|---|---|
| carried presentation (§7), adapted | ~3,500 carried, ~400 changed |
| launch, `SessionView`, listener, transcript wiring, status and cursor bar | ~900 |
| editor, commands, completion, form renderer, question box | ~600 |
| picker wiring, tree screen actions, re-exec | ~300 |
| tests: pilot in both modes, `SessionView` units, snapshots | ~900 |

**Not verified, inferred:** that `App.run_async` shares the loop with the driver without
trouble under a free-threaded build; the carried line counts (from the survey, not a port); that
0.15's `transcript.py` render window separates from its event routing cleanly.
