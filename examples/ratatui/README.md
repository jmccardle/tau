# tau-ratatui

A terminal chat client of `tau serve`, written in Rust with [ratatui](https://ratatui.rs). It is
the first client of τ that is not Python, and it shares no code with τ: it speaks the
[wire protocol](../../docs/guide/protocol.md) and nothing else. If it works, the schema is enough
to write a client in another language.

It is a chat client with a tree browser and `/config` forms: a cursor's conversation (messages,
thinking, tool calls and results, the live text of a reply), a line to type, questions, a status
line, the session's tree, and an extension's options as a form. It has no session picker, cursor
bar, themes or Ctrl+Z (`tau` has all of those), and it does not start a server. It shows the
first cursor at first; the tree's `v` changes which one.

## Build and run

From the repository root (`cargo` must be on your `PATH`; `source ~/.cargo/env`):

```bash
cargo build --release --locked --manifest-path examples/ratatui/Cargo.toml
tau serve -d                      # a server for this directory's session; prints its socket
examples/ratatui/target/release/tau-ratatui
```

It finds the server in one of three ways:

| flag | finds |
|---|---|
| `--socket PATH` | the Unix socket, whatever serves it |
| `--session ID` | the server of that session, from its record `$TAU_HOME/serve/ID.json` (`$TAU_HOME` is `~/.tau` unless set) |
| neither | the one `tau serve` whose session runs in the current directory. A record whose server no longer answers is stale and does not count (as for `tau`; the record is left for `tau` to clean up). If several answer, it lists them and stops; if none, it says so and names `tau serve -d` |

`--client ID` sets the client id (default `user@host:ratatui`), the name other clients see on
what you send. A refused `hello` (a server of another protocol, say) or a connection that cannot
be made exits with status 1 and the server's own message.

## Keys

| key | does |
|---|---|
| Enter | sends the line; a line starting with `/` is read by the server (`parse_line`) and run as a command, and an unknown command is sent as text |
| Esc | aborts the cursor's step if it is busy; with a question open, declines it |
| Ctrl+C, Ctrl+D | quit, with the terminal restored (also if the program panics) |
| Left, Right, Home, End, Backspace, Delete, Ctrl+A, Ctrl+E, Ctrl+W, Ctrl+U | edit the line |
| PageUp, PageDown | scroll the conversation |
| Ctrl+G | the tree (below) |

A **question** an extension asks you appears above the editor. If its answer is one of a few
values (`yes`/`no`) pick with Up and Down and press Enter; otherwise type the answer (a number is
checked before it is sent) and press Enter. Esc declines. A question addressed to another client
is not shown. A command with arguments missing needs a form this client does not have for
commands: it says so and puts the line back. `/config` has its form (below).

### The tree (Ctrl+G)

A full-screen view of every node, read through `nodes` pages (it says "reading the tree" first). It stays open across a reconnect after a cut-off; an open form does not (a notice says so).
The viewed cursor's walk runs straight down; each turn is one folded row
(`user: fix it  +14 · 6 calls · 1 branch`), open from the start if a cursor is in it; every other
branch is one folded stub (`├┄ 3 nodes, 1 turn  ◀ 1 scout …`) that unfolds to its own line.
`◀ [0] main` is the viewed cursor, `…` busy, `?` a question; closed cursors are `○ 2 name`, dimmed.
A folded row lists the cursors inside it. The title says **following** (the highlight tracks the
viewed cursor as it moves) or **paused**. The right pane (under the tree on a narrow screen) shows
the highlighted node and the size of the viewed walk: nodes and about chars÷4 tokens.

| key | does |
|---|---|
| Up, Down, PageUp, PageDown, Home, End | move the highlight (pauses following) |
| Left, Right | fold or unfold; Left on a leaf steps out to its row |
| Enter | go to the node (the viewed cursor moves; asks y/n first if it is busy, since that aborts its step); on a folded stub it asks you to unfold |
| `o` | open a cursor at the node |
| `v` | view the cursor at the highlight, or on the branch it is on; resumes following |
| `l` | label the viewed cursor |
| `[`, `]` | previous / next cursor mark, unfolding what hides it |
| `f` | follow the viewed cursor again |
| Esc | back to the chat |

### `/config` forms

`/config EXTENSION` (or `/config` for a list of the extensions that have options) opens a form from
`options(cursor, EXTENSION)`: enums are a row of choices (Left/Right), booleans a switch (Space),
a list of enums a multi-select, strings, numbers, integers and lists of them text fields (a list is
comma-separated, or JSON starting with `[`); `required` and `default` apply. Tab or Down moves
between fields, Enter on Apply sends, Esc cancels. "save for every session" is `configure`'s
`save`. Only what differs from the value the form opened with is sent (with save on and nothing
changed, `save` is still sent with no values); what `save` writes to your file is the server's rule. An error from the server (`ConfigError`, `Busy`) is shown in the form, which stays open,
and nothing is reported saved. A schema outside that list is refused with a message naming the
option, never skipped; `/config EXTENSION name=value` still works.

If the server cuts this client off for falling behind (`end` with `CutOff`), it says `hello`
again and reads the conversation again, as the protocol's "no resume" asks. If the server stops,
it prints that and exits 0; if the connection drops or the session fails, it prints why and exits 1.

## How the wire types are written

Not by hand. `build.rs` reads [`docs/protocol.schema.json`](../../docs/protocol.schema.json) and
has [typify](https://github.com/oxidecomputer/typify) write every type of the wire (nodes, bodies,
events, errors, each call's parameters and result) into `$OUT_DIR/wire.rs`, so the types cannot be
out of date with the schema: a change to the wire is a build error or a changed type. Generating
at build time rather than checking in a generated file means there is nothing to keep current and
nothing to review twice. The cost is that the example builds only inside this repository (its
`build.rs` reads `../../docs/`) and that a build compiles typify.

typify reads an older JSON Schema than the one τ writes, so `build.rs` first respells three things
and changes nothing else: `$defs` as `definitions`, `prefixItems` as a tuple `items`, and
`{"const": x}` as `{"enum": [x]}` (that is what lets `TextDelta` and `ThinkingDelta`, which differ
in nothing else, be told apart by their `kind`). It also tells a call (a definition with
`x-result`) from a type: `node` the call and `Node` the type would be one name otherwise, so each
call becomes `<Method>Params` and `<Method>Result`, and `ErrorCode`'s `x-names` become constants.

What is written by hand is what the schema does not describe or cannot type:

- the JSON-RPC envelope (`src/link.rs`: ids, requests, responses; the schema describes what is
  inside it);
- the shape of a serve record, `$TAU_HOME/serve/<id>.json` (`Record` in `src/find.rs`: `socket`,
  `session`, `cwd`, `pid`), which is a file `tau serve` writes and not part of the wire;
- one rule the schema's text states but cannot type: a command is called by `extension:name`.

## What is where

| file | |
|---|---|
| `build.rs` | the schema to Rust |
| `src/link.rs` | the connection: `hello`, calls matched to responses, the stream of events |
| `src/find.rs` | which socket |
| `src/model.rs` | what the client knows, built from events alone: the walk, the live layer, questions |
| `src/ask.rs` | reading a question's answer schema, checking typed answers |
| `src/run.rs`, `src/run/modes.rs`, `src/ui.rs`, `src/editor.rs` | keys, drawing, the line editor, the screens over the chat |
| `src/plan.rs`, `src/tree.rs`, `src/treetext.rs`, `src/treeui.rs` | the tree: the plan (pure), its state and keys, row words, drawing |
| `src/form.rs`, `src/formui.rs` | a form from a JSON Schema, and drawing it |
| `testdata/session.jsonl` | what a real server sent, replayed by the model's tests |

`cargo test` covers the pure pieces (finding the socket, the model against the recorded stream,
wrapping, the editor). `tests/serve/test_ratatui.py` builds the binary and drives it in a
pseudo-terminal against a real `tau serve` with the mock model: a chat, Esc, questions answered
and declined, the tree (a second client's branch as a stub, `v`, Enter, `o`, `l`, a busy cursor),
`/config` forms (applied, saved, refused), being cut off, and every way of failing to connect.
