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
  cursor, *, welcome, home, snapshot=(), theme=None, reconnect=None)` (8C-1: `home` is `TAU_HOME`,
  for user themes; `reconnect` makes a new client for an attached session) and `SessionPicker` (§5,
  8C-2), with `SessionView` (§9: tested through `tau.tui`). Everything else is private
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
- **Status:** the header shows the session's title (the first message; 8C-1 has no name, which is
  not in `Welcome`), id and `cwd`;
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
- **Notices** are toasts, and are kept in a list (`TauApp.toasts`; the palette that opens it is
  not built in 8C-1).

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
| `dialogs.py`: `TauDialog`, `ChoiceDialog`, `TextDialog` | 226 | `TauDialog` as is; the other two wait for a dialog that needs them (8C-1 built none) |
| `chat_widgets.py`: `MessageBox`, `ReasoningRegion`, `ToolBox`, `QuietCollapsible`, `MarkdownLineFormatter`, the formatters (`ExchangeBox` is not carried, 8C-1: nodes are the unit, §2, and a fold per exchange hides the reply) | ~1,100 | fed from nodes instead of 0.15 message dicts |
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
- **Snapshots** (a checked-in SVG per scene, compared by `tests/tui/tuikit.check_screen`; no plugin): a few fixed scenes (empty, a
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

## Built: 8C-1 (2026-11-03)

`tau` opens the app; `SessionView`, the transcript, live text, the editor, pending lines, abort,
`/` commands with the popup and Tab, forms for missing arguments, inline questions, toasts, the
status bar, themes, `tau.tcss`, `--theme` and the `tui` key. Where the code differs from the text
above (the text is fixed in place where it was wrong):

- **`App.run_async` shares the driver's loop** (the "not verified" line, free-threaded build
  aside, which this machine cannot run): it is a coroutine of the caller's loop and starts none, so
  a `LocalClient` is called from the app with no thread between. `tests/tui/test_session.py` runs
  the app by `run_async` beside a local driver; every pilot test runs in both Harness modes.
- **Reconnect belongs to 8C-1** (§1): `TauApp(reconnect=)`, given by the launch for an attached
  session; tested with a `CutOff` client.
- **A `sample_error` is not on the walk** (model.md §6): core writes it beside the node the cursor
  is at. The app draws it, from `Appended`, under the node it is at; a later read of the walk does
  not show it.
- **`Labels` does not end a snapshot for a late reader**: a `CursorView` republished since the
  connect moves to the end of the stream, after `Labels` (model.md §9). `open_client` reads at once,
  so its snapshot is whole; the app depends on no order.
- **The top-level `tui` key** is the user's alone (ruling 3): a project layer that sets it is a
  `ConfigError`, found as the layers are read (`ModelCatalog.tui` holds the user's and the
  command line's). D13's top-level keys gain it.
- **`/config`**: a bare `/config` or `/config EXTENSION` shows the extensions or the options as a
  toast; the form with "save for every session" is 8C-3, as §10 says. `/config EXT name=value
  [--save]` runs.
- **Not in 8C-1**: the palette (Ctrl+P) and its notices list, the cursor bar, Ctrl+G/O/N/Z and
  Alt+arrows (8C-2), `/model` and the render window (8C-3). `ChatSidebar`, `ExchangeBox`,
  `StreamStrip` are not carried.

## Built: 8C-2 (2026-11-03)

The cursor bar and switching the viewed cursor, labels (in the bar, the tree and the tree's `l`),
Ctrl+Z, `Client.remove_item` (local and over the wire) with Up on an empty editor, the tree screen,
the session picker (`-r`, Ctrl+O), Ctrl+N and re-exec. Where the code differs from the text above:

- **`remove_item(cursor, item)`**: `NoSuchItem` (a `LookupError`) for an item not on the cursor's
  scratchpad (delivered, or never there); `PermissionError` (wire code `NotYours`) for an `Input`
  item another client added or an `Extension` item; `NoSuchCursor` for a cursor never opened.
  Additive on the wire: one method in the schema, no `PROTOCOL` bump. It makes `Client`,
  `LocalClient` and `Calls` 26 methods, so each carries a `[justified]` line in
  `scripts/ratchet-allow.toml`. (Review: no justification; `Client` is split in two private
  parts, `_TreeReads` and `_CommandCalls`, the wire read from its MRO, and `Calls` hands its
  command-line calls to `Commands`.)
- **Review fixes.** The picker lists the current session (`current`, a no-op), and marks `in use`
  (another process's lease), `no reply` (a serve record that does not answer; the listing still
  opens) and `served`; the lease is checked again before leaving. Moves hold on to the cursor
  viewed when they began (a closed one is `NoSuchCursor`, a toast). Ctrl+Z is the editor's key:
  undo with text, edit-last on an empty editor. Ctrl+N asks first on a busy cursor. `remove_item`
  is refused (`LookupError`) for the input a `pre_input` hook is being offered (core:
  `Step::offers`), with a readable message. A `ConnectionError` on a sent line says it may have
  reached the session.
- **Taking a line back is Up** on an empty editor when one of this client's lines is pending
  (0.15's reclaim key). With none pending Up walks the history as before.
- **Ctrl+Z asks first only on a busy cursor**, as the tree's Enter does; the message goes into the
  editor in front of any draft already there, so nothing typed is lost.
- **`o` in the tree opens the cursor and says so (a toast); it does not move the view**:
  Alt+Right views it. The new cursor's view arrives on the stream after `open` returns, and
  waiting for it to switch would be a wait with nothing to show for it. `l` labels the viewed
  cursor and leaves the tree.
- **The tree is carried less than §6 listed** (reworked in TUI follow-up 1, below): the row planner (`plan_tree_rows`; `navigate` rows
  do not exist in 0.16, and the groups left open are those a cursor is inside, not only the
  first leaf), `ZoneTree` (the type tag's colour and the path to the highlighted node; no marks,
  copied, hover or elide zones) and the screen's layout, folding keys and detail pane. The detail
  pane is a plain text view of the node, not 0.15's boxes, and has no fold key.
- **The cursor bar is text** (`[0 main] 1 scout …?`), hidden with one cursor and no label; it is
  not clickable. Alt+Left and Alt+Right wrap around.
- **Closing the viewed cursor** (another client's `close_cursor`) moves the view to the lowest
  open cursor with a toast; with none left it is the 8C-1 message.
- **`TauApp` gains `sessions=`** (the sessions directory Ctrl+O lists; without it Ctrl+O is
  refused with a toast), `cwd` is now a public attribute, `view(cursor)` and `switch`. Leaving for
  another session sets `TauApp.switch` (`tau.tui.Switch`) and exits cleanly; the launch
  (`tau.tui._run`) closes the session and replaces the process. `tau.tui` also exports
  `SessionPicker`, `Switch` and `TauDialog` (the base of `SessionPicker`, which the API check needs
  published).
- **`-r` runs the picker before the event loop starts** (`PickerApp`, a small app of its own),
  then becomes `--session ID`; with no session it is `UnknownSession` before any screen, and a
  cancelled picker exits 1 ("no session chosen"). `-r` with no terminal is the generic "needs a
  terminal" usage error the launch gives first.
- **The re-exec chdirs to the directory `tau` was typed in** before `execve`, so relative flags
  mean what they meant and Ctrl+N starts the new session there. The flags carried are the sessions
  directory and theme, `-e`, `--no-extensions`, `--set` (dropped when the target is held by a
  server, which would refuse them) and, for a new session, `-m`, the prompt flags and
  `--no-session`. `--verbose` is not carried.
- **The picker's "served" column** is read by probing each serve record (`served_sessions`) when it
  opens; a server that does not answer `hello` is a `ServeFailed` and no picker. The search text of
  `/` is id, name and first message (0.15's last message is not in the catalog).
- **`TauApp` stays at 25 methods** by moving the popup/editor wiring into `Composer`
  (`_editor.py`), which also holds the Enter-to-submit task.

## Built: 8C-3 (2026-11-03)

`/config` forms with "save for every session", the `/model` picker, a theme picker that saves, the
render window, snapshot tests and the guide page (`docs/guide/tui.md`). Where the code differs from
the text above:

- **`/config` and `/model` are dialogs over the same renderer.** `/config EXTENSION` (or a bare
  `/config`, which first asks which extension in a `PickDialog`) opens `OptionsDialog`: the options'
  schema, each field showing its value now (`Client.options`), and a switch "save for every
  session". Apply sends only what changed: `configure(cursor, extension, changed, save=switch)`, so
  the `Config` node is this branch's and the switch also writes the **user's** file (D13: the project
  layer has no write; the server picks what to write). `/model` with no name is a `PickDialog` over `complete(cursor, "model", "")`
  with the current model marked. This is not a branch on the command's name: any missing argument
  whose named type (`format`) is a closed set (`model`, `cursor`) is picked from a list; the rest
  are form fields. (`/config` itself is still named in the line code, as in 8C-1.)
- **The theme picker is the palette's** (Ctrl+P, **Theme**), a `PickDialog`: Ctrl+P is built now,
  with `TauCommands` (Theme, Tree, Sessions, New session, Quit) in place of Textual's system
  commands. Picking writes `{"tui": {"theme": NAME}}` through the new `UserConfig.save_theme`, then
  wears it; if the file cannot be written the theme is not changed and a toast says why.
  `--theme` writes nothing. The notices list (`toasts`) still has no palette entry.
  `TauApp.theme_name` (8C-1) is gone: `TauApp.theme` is Textual's own and is the theme worn.
- **The render window** (§7, "Not verified"): **it separates from event routing cleanly, because
  0.15's did not need to be carried with it.** The 0.15 window (`render_cap_start`, `window_end`,
  `turn_starts`, the trim at the tail, the slide at an edge) is a function of the message list plus
  a few flags; what tangled it was that its renderers, its streams and its exchange boxes all
  knew the window. Here the bounds are three pure functions of the walk (`_window.py`), and a
  `WindowedWalk` base class (`_windowed.py`) owns the window, the rows above and below, the slide
  and the trim, and asks its subclass only to draw one node and forget one. Four turns or 100
  nodes as in 0.15. Differences: the unit is a node and a turn starts at a `User` node; there are
  no exchange boxes to unwrap; a record beside the walk (`sample_error`) is kept with its parent
  and redrawn with it; the trim waits while a reply is live or the reader has scrolled up;
  sliding down is not offered at the tail. Ctrl+Up and Ctrl+Down are new (0.15 slid on the arrow
  keys of a focused transcript, and the editor holds the focus here). The reader's intent to
  follow the tail is now kept across a resize (a question box opening shrinks the transcript),
  which the snapshots showed was racy.
- **`tau -r --session ID` was already refused before any picker** (a `ConfigError`, status 1, as
  the other flags that each choose the session are); a test now holds it.
- **Snapshots** are in `tests/tui/test_snapshots.py`: the empty session, a reply with thinking and
  a tool, a question, the tree, the session picker, the `/model` picker and an options form, kept
  as SVG in `tests/tui/__snapshots__/`, local mode only. The pilot's own app is exported
  (`export_screenshot`) and compared with the file; `TAU_UPDATE_SNAPSHOTS=1` rewrites them, a
  difference is reported as the rows of text that changed, and `NO_COLOR` in the environment is a
  clear failure (Textual then draws without colour). No plugin: `pytest-textual-snapshot` runs its
  own loop, which a Harness cannot share, and its syrupy pins `pytest<9`. The SVGs are Textual's
  drawing, so the dev group pins `textual==8.2.8` and `rich==15.0.0`.
- **Review fixes (8C-3).** The window's slide and deferred trim run under the app's drawing lock
  with every other mutation of the transcript. Tool progress for a call that is not drawn is
  ignored. While the window is slid back a reply in flight is not drawn (it would land under the
  "later" row); coming to the tail draws it from the live layer, whole. A reconnect keeps the
  reader's place; a send brings the window to the tail. `/config` reads the options when the
  form opens; with the save switch on it sends what the person wrote a value for, and the server
  decides what the file gets (D13: the options `values` and the branch's `Config` nodes set that differ from the file), so the
  form reads no file and saving the branch's current value works for any client. Picking the
  theme already worn saves it.

## Built: tree view (TUI follow-up 1, 2026-11-03)

The Ctrl+G screen reworked on the owner's decisions of 2026-11-03: same full-size modal (tree left,
detail pane right), new rows and movement. No wire or protocol change: the rows come from
`nodes(start, limit)` pages and then from the `SessionView`, which the app already feeds from
`Appended`, `CursorView`, `CursorGone` and `Labels`.

- **Spine and stubs.** The viewed cursor's walk is drawn straight down at depth 0. Each turn off
  the spine is one folded row (`user: text  +14 · 6 calls · 1 branch`); the turn the viewed cursor
  is in, or any turn with a cursor mark in it, starts open. Every other branch at a branch point
  is one folded stub row (`├┄ 3 nodes, 1 turn  ◀ 1 scout …`, closed cursors `○ 2 compaction:1`
  dimmed). A branch at the last node of a turn hangs level with the turn (folding the turn does not
  hide it); a branch in the middle hangs inside the turn. **The rule for a stub's own line**: from
  the stub's first node it goes on through the child toward the lowest-numbered open cursor below
  it, else the child with the longest line below it (lowest id on a tie); other children are stubs
  again, recursively. Below the spine's last node (the viewed cursor's own) every child, an only
  child included, is a stub. `v` redraws with another cursor's walk as the spine.
- **Following** is on while the screen opens: a redraw (batched to 100 ms after `Appended`,
  `CursorView`, `CursorGone` or `Labels`) puts the highlight on the viewed cursor's row. Up, Down,
  PgUp, PgDn, Home, End, a click, `[` `]` and Left stepping out of a row pause it; `f` resumes it;
  `v` resumes it on the new viewed cursor. The title says "following" or "paused".
- **Long trees.** `TreePlan` (`_treeplan.py`, private) makes a row's children only when asked,
  and `TreeShape` keeps what nodes say that never changes (parents, sizes, heights, the turn rows
  nothing marks) between plans. The screen gives a row widgets only when it is open, and a redraw
  changes the rows that differ in place and adds the new ones; it makes the tree again only when
  the spine changed (`v`, a go-to far back, a branch hung earlier on the spine). A fold the person
  made is kept across redraws; the default (a cursor in the turn) applies to the rest. Measured
  through the screen on 30,004 nodes (`tests/tui/test_tree_long.py`, a node arrives with the tree
  open): a redraw takes about 70 ms from the event to the highlight in place (some 175 ms when two
  fall together); the first draw of 15,000 turn rows takes about half a second; a full re-make
  (the spine changed) the same. The first version made all the rows again on each arrival and took
  0.75 s.
- **Cursors are always visible.** A folded turn's row lists the open and closed cursors inside it
  (`user: fix it  +14  ◀ 1`), as a stub's does, and `v` works on it. An unfolded turn shows only
  those at its own node; the rest show on their rows below.
- **When the viewed cursor closes** while the screen is open, it follows the app's cursor (the
  app moves its own to the lowest open one) and says "following". The screen reads the app's
  `session` each time, so a reconnect (which replaces it) does not leave it stale; nodes it has not
  read are read with `nodes`.
- **Keys that changed.** New: `v`, `f`, `[`, `]`. Enter, `o`, `l`, Left, Right, Esc as before,
  except that Enter and `o` on a folded stub say to unfold it first (a stub is not a node), and
  Left on a leaf steps out and so pauses following. `TreeIntent` gained `cursor`: the cursor the
  screen was viewing when it was asked, which `v` can change after the screen opened.
- **Detail pane**: the node as before, under `cursor N: a walk of X nodes, about Y tokens`
  (characters the model reads over four, D24's estimate; `Config` and `Custom` nodes count none).
- **Copy and derived marks, `p`** were built with 8E-3 (D24 "Built: 8E-3"): `⧉`, `≡ (copy of N)`,
  `copied from` / `derived from` with the walk before and after in the detail pane, and `p`.
- **Not built**: previewing another branch
  in the transcript, diffing, a shape-only read. A closed cursor shows only if this client heard
  its `CursorGone` (the stream gives one per closed cursor on connecting).
- **Deviations**: the owner's "turns off the spine start folded" is "turns start folded unless
  a cursor is in them" (sensible at 15,000 turns, and the cursor's own turn is where one looks).
  The stub row has no preview of its text (the detail pane shows the branch's first
  node when it is highlighted). `[` and `]` go in node order, around the ends, not in screen order.

## Gaps in the `Client` contract and the shared start

The most important finding: **the contract is nearly enough.** Everything the TUI draws is in
the events and the reads. What is missing:

1. **Who asked a question** (also D22 gap 1): `OpenQuestion` has no extension name, so the
   question box cannot say "permissions asks". Add `OpenQuestion.extension`.
2. **The first cursor needs a `Session`** (D22 gap 2): an attached TUI cannot call
   `first_cursor`. `open_client` computes it from the snapshot.
3. **A pending input cannot be taken back.** No `Client` call removed an item; handles have
   `remove_item`. 0.15's reclaim (Up on an empty editor) had no counterpart. Closed in 8C-2:
   `Client.remove_item` (Ruling 4), and Up.
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
- **edit last message** (Ctrl+Z): a go-to before the last `User` node, what the person typed (`Node.typed`, D17 §1a; else its text) in the editor. The transcript and the header title show the typed line too, not the files a hook inlined; the tree screen shows the model's text.
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
