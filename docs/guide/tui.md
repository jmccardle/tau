# The terminal UI

`tau` with no `-p` opens a terminal UI on a session: the transcript of one cursor, an editor, and
whatever the session asks of you. It is a frontend like any other (it touches only a `Client`),
so everything it does, a script or an editor can do too ([Writing a frontend](frontends.md)).

```bash skip
tau                          # your default model, a new session
tau -m mock:echo             # no server needed
tau -c --theme latte         # the newest session here, in the light theme
tau -r                       # choose a session to resume
tau --session 20261102-0915-1c2d3e   # one session; attaches if a `tau serve` holds it
```

Standard input and output must be a terminal; without one `tau` says so, and `tau -p` is the way
to script it. `--json` and `--answer` are for `-p`. The flags that choose the session, the model
and the extensions are the same as for `tau -p` ([Quickstart](quickstart.md)); `--theme NAME` is
the terminal UI's own.

## The screen

Top to bottom:

| part | shows |
|---|---|
| header | the first message, the session id, the working directory |
| cursor bar | a tab per open cursor with its labels, `…` while it works, `?` while it has an open question, the viewed cursor in brackets; hidden with one cursor and no label |
| transcript | the walk of the viewed cursor: your messages, the model's replies with their thinking folded and a box per tool call (folded; its mark says whether it worked), the live text of the reply in flight, and a message someone else sent under that author's name |
| questions | one box per open question, titled by the extension that asks |
| pending | what was sent while the cursor was working, with its author if someone else sent it |
| popup, editor | the commands `/` lists; the editor |
| status bar | the cursor's model, whether it is working, auto-step, how the last step ended, a failed hook, and a line for each extension that shows something |

Notices (`cursor.tell`, a command's progress) are toasts.

## Keys

| key | does |
|---|---|
| Enter | send, busy or not: what you send while the cursor works waits above the editor until a step takes it |
| Ctrl+J | a new line in the editor |
| Up, Down | at the first and last line, walk the lines you sent; Up on an empty editor takes back the last line the cursor has not taken yet |
| Tab | complete: a command's name, then each argument by what it is (models, cursors, nodes, paths); an `@path` in a message |
| Esc | abort the step (inside a question box, decline it) |
| Ctrl+C | abort if busy, else empty the editor, else quit when pressed twice |
| Ctrl+Q | quit |
| Ctrl+R, Ctrl+T | fold the thinking, fold the tool boxes |
| Ctrl+Up, Ctrl+Down | a page of the transcript; at its top or bottom, slide the window along a long walk (below) |
| Alt+Left, Alt+Right | view the previous or next open cursor |
| Ctrl+Z | on an empty editor, edit your last message (with text typed, undo) |
| Ctrl+G | the tree |
| Ctrl+O, Ctrl+N | choose another session; start a new one |
| Ctrl+P | the command palette: Theme, Tree, Sessions, New session, Quit |

## Commands, options and models

`/` lists the commands; a prefix narrows the list and Tab completes. A command typed without its
arguments asks for them: a form with a field for each argument (a choice is a list, a yes/no a
switch, anything else a text field Tab completes). An unknown `/name` is sent as text, and a bare
name two extensions share is refused (type `/extension:name`). The result of a command is a
toast.

- **`/model`** with no name opens a picker of the session's models: type to narrow the list (the
  ones that start with what you typed come first), Up and Down move, Enter picks, Esc cancels. The
  one in use says `(current)`. `/model NAME` switches without asking. The model is the cursor's: a
  `Config` node on its branch.
- **`/config`** opens a picker of the extensions that have options, then the options of the one
  you pick: a form with each option showing its value now. Apply sends only what you changed, as
  a `Config` node, so it holds **on this branch**. Turn on *save for every session* to write the
  changes to your own config file (`$TAU_HOME/config.json`) too, so every later session starts
  with them ([Configuration](configuration.md)). `/config EXTENSION` opens the form of that
  extension, and `/config EXTENSION name=value [--save]` changes options without a form. A value
  the extension's options refuse is a toast, nothing is written, and the line goes back to the
  editor.

## Questions

A question an extension asks (a permission, say) is a box above the editor, so you can still read
the transcript while it waits. A choice has a button for each answer; anything else has a field
and an Answer button. Decline, or Esc inside the box, answers nothing. An answer the session
refuses stays in the box with the reason. A question for another client is shown with that
client's name and no buttons.

## Cursors, the tree and sessions

- **Cursors.** Alt+Left and Alt+Right view the previous or next open cursor. Someone else's branch
  of the cursor you view, or a go-to, redraws its walk.
- **Ctrl+Z on an empty editor edits your last message**: the cursor goes to the node before it and
  the message is in the editor, so sending it makes a branch (the old turn stays in the tree). On
  a busy cursor it asks before aborting the step.
- **Ctrl+G is the tree**: the walk of the cursor you view runs straight down from the first node.
  Each turn (your message and what followed) is one row that folds (`user: fix it  +14 · 6 calls`).
  Every other branch hangs off the walk as one folded line, a *stub*: `├┄ 3 nodes, 1 turn ◀ 1 scout …`
  says how big it is and which cursors are on it (`…` working, `?` a question open). Right unfolds
  a stub to its turns; the line it shows goes through the cursor on it, else the longest line.
  Cursors are marked `◀ 1 scout` where they are (a folded turn or stub lists the ones inside it, so
  none is out of sight); a closed cursor is dimmed `○ 2 compaction:1`. The
  highlighted node is shown whole beside the rows, under the length of the viewed walk in nodes and
  tokens. While the tree is open the highlight follows the viewed cursor as it works; moving it
  yourself pauses that (the title says "paused"), and `f` follows again.
  Keys: Up, Down, PgUp, PgDn, Home, End move; Left and Right fold and unfold; Enter goes to the
  node (on a busy cursor it asks before aborting); `[` and `]` jump to the previous and next
  cursor; `v` views the cursor at the highlight and redraws with its walk down the middle; `o`
  opens a new cursor at the node; `l` puts a label on the viewed cursor; Esc leaves.
- **Sessions.** `tau -r` and Ctrl+O open the picker: this directory's sessions, newest first, by
  name or first message; Tab widens it to every directory, `/` filters, Enter opens, Esc cancels.
  A session a `tau serve` holds says `served`; one another `tau` process holds says `in use`, and
  one whose server does not answer says `no reply`: neither can be picked. Opening one, and Ctrl+N
  for a new one, close this session and start `tau` again in the same terminal, with the same
  flags: one process holds one session and one working directory.

## Long sessions

A long walk is not drawn whole: every box on screen slows every update. The transcript draws the
last four turns (a turn is your message and what followed it), or fewer when they hold more than a
hundred nodes, and a row at the top says how many nodes are above. Scroll against the top edge
(the wheel, or Ctrl+Up at the top) and the window slides back one turn; against the bottom edge it
slides forward. Nothing is lost, only not drawn: the session and the tree still hold every node,
and sending a message brings the window back to the end. While you read history, new nodes are
counted in a row below instead of moving the page.

## Themes

The built-in themes are `mocha` (the default), `latte`, `gruvbox` and `ansi`, and Textual's own.
**Ctrl+P, Theme** opens a picker of them; the one you pick is worn at once and **saved**: it is
written as `{"tui": {"theme": "NAME"}}` in your config file, so the next `tau` opens in it. A
file that cannot be written is a toast, and the theme stays as it was. `--theme NAME` picks a
theme for one run and writes nothing. A theme of your own is a file `$TAU_HOME/themes/NAME.json`
([Configuration](configuration.md#the-terminal-uis-settings)).

## Attaching

A session a `tau serve` holds is attached to, not refused: `tau --session ID` shows the same
screen over the server's socket, and anyone else connected sees what you send, under your client
id (`john@laptop:tui`). If the connection drops, the screen says so and connects again with the
same id, then redraws from the server's new snapshot. If the session itself fails, the screen
says so and keeps the transcript readable; `tau` exits 1 when you quit.
