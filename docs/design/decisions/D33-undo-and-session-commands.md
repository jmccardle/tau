# D33: Undo markers, `/undo`, and the session commands

**Status:** accepted for 0.18.0 (unreleased), built. **Amends** D23 §2 (Ctrl+Z calls the harness's
`/undo` when the `undo` extension is loaded; `/undo`, `/clear`, `/branch`, `/tree` and `/export`
become slash commands, where `from-0.15.md` §2.9 said such moves are keys), D24 §2 and §9
(compaction and prune end their new line in an undo marker), and the `Extension` contract (one
more optional attribute, `reverse`). Proposal trail: akb #639 (discussion), #640 (review:
markers do not always rest; `Config` nodes are folded deltas; `Node.typed` is `None` unless a
hook changed the text), #641 §1 CV-01 and §2 EX-20/EX-21, #642 (Q-a: `/clear` reuses the
existing `System` node). John's rulings there are **fixed**.

## Context

τ has had edit-last since 0.16 (D23 §2, Ctrl+Z): a client move that goes to the node before the
last `User` node and puts the message back in the editor. It knows nothing of the other changes a
person may want to take back: `/compact` and `/prune` write a new line and move the cursor onto
it, and the way back is the tree screen. 0.18 adds `/clear`, which does the same. A file
checkpoint extension (EX-40) would add changes outside the tree that only it can reverse.

The rulings (**fixed**, #641 §0, #642):

1. `/undo` is a **convention over nodes the tree already holds**, which are never sent to the
   model: not a protocol change. `undo` is a removable extension; any extension that makes an
   undoable change writes a marker `/undo` follows.
2. No multiple roots. `/clear` hangs its line under the walk's existing `System` node, with one
   `Config` if the folded branch settings differ from the root's, then the marker (Q-a).
3. A client implements a command only where it can act on it: no `/new` in the harness, which is
   a client's move.

## Decision

### 1. The undo marker (CV-01)

A `Custom` node of kind `undo`, whose `data` is a JSON object:

```json
{"label": "/compact", "return_to": 41, "reverses": "compact", "priority": "move"}
```

| key | what |
|---|---|
| `label` | what was done, as a person names it: `/compact`, `automatic compaction (hard limit)`, `edit of a.py` |
| `return_to` | the node going back puts the cursor on: where it was before the change |
| `reverses` | the kind of change, a short name its owner chooses: `compact`, `prune`, `clear`, `checkpoint` |
| `priority` | which `/undo` follows it (§2); `move` when absent. Lower-case letters, digits, `_`, `-` |
| any other | the owner's own (a checkpoint's commit), kept as `UndoMarker.extra` |

- The extension that made the change writes it, so its `by` names the owner.
- It is a `Custom` node: never sent (providers' walks skip it), and it **decides nothing**. #639
  proposed that markers always rest; #640 showed that harmful (a checkpoint after a `Results`
  forced to rest would stop the tool loop). Where it sits decides: core's `deciding` skips it to
  the node before. Compaction's hard limit writes its marker mid-turn, and the turn goes on under
  it.
- `tau.UndoMarker` writes and reads it: `UndoMarker(label, return_to, reverses, priority="move",
  extra={}).body()` and `UndoMarker.of(body)` (`None` for another body, `ValueError` naming what
  is wrong for a malformed one).
- A marker copied onto a new line (prune copies `Custom` nodes; compaction copies the tail) still
  marks the same change, with the same `return_to`.

The convention is documented for clients and extensions in `docs/guide/conventions.md`.

### 2. `/undo` (EX-20)

`/undo [priority] [at=NODE] [json=true]`, the built-in `undo`, default-on, removable.

- It walks back from the cursor's node toward the root, to the first of: a marker whose priority
  is the one asked (`move` unless given); for `move` only, a `User` node (edit-last). The walk is
  the undo stack: undoing a change moves the cursor off its marker, so the next `/undo` reaches
  the change before it, and a change made on another branch is never on this walk.
- **Priority.** `move` is a change to the tree alone, which moving the cursor back reverses
  (`/compact`, `/prune`, `/clear`), and edit-last is one too. Any other priority is followed only
  when asked for (`/undo file`), so a checkpoint written mid-turn never takes over edit-last, and
  a plain `/undo` passes over it.
- **The owner's reversal.** For a marker of any priority but `move`, the owner reverses what it
  marks before the cursor moves: `Cursor.reverse(marker)` calls the owning extension's `async def
  reverse(self, cursor, marker)` (§3). If the owner is not loaded, has no `reverse`, or the marker
  was not written by an extension, nothing moves and `/undo` says why (fail early: a cursor moved
  back over files that were not put back would be a lie). What `reverse` raises fails `/undo`.
- **The move** is a go-to's: `cursor.move(return_to)` and auto-step off. A `return_to` that is
  not a node, or whose walk passes through the marker itself (it would never leave it), is
  refused, naming the marker.
- **Busy.** `/undo` runs as a step of its cursor, so a busy cursor refuses it with `Busy`, as
  every cursor command (`/compact`, `/config`) is refused. The TUI's Ctrl+Z on a busy cursor stays
  its own edit-last (it asks, then a go-to aborts the step), §5.
- **Mid-turn.** A cursor put on a node that would sample or run tools (a checkpoint's
  `return_to`, a hard compaction's) rests there: a command's step ends when the command returns
  (D16 §2, amended 2026-11-26), so going back never starts the model, and the step ends `Done`.
  A command that moves and *wants* the model to go on (a regenerate) turns auto-step on
  (`cursor.set_auto(True)`). *Superseded:* as first built, a command's step went on after the
  command, and `undo` and `session` halted it in their own `pre_step` (`Stays`, `halted: /undo
  left the cursor at node N, mid-turn: step to carry on from there`); the ruling of 2026-11-26
  made that the driver's rule for every command, and `Stays` was removed.
- **Two clients.** `at=NODE` refuses unless the cursor is on that node, so of two clients that saw
  the same node and undo, one moves the cursor and the other is told it moved. (The two cannot run
  at once in any case: the second is `Busy`.)
- **The answer** is text for a line client: `undid /compact: back to node 41`, or for edit-last
  `back to node 12, before your message:` and the message, so a client with no editor to refill
  still shows it. `json=true` answers `{"from", "to", "marker", "label", "user"}`, with `user`
  `{"node", "text"}` for edit-last (the node's `typed`, else its text) and `null` otherwise: what
  a client refills its editor with. A command's result is a string (D16 §2); a JSON form chosen by
  an argument keeps that, and needs no wire change.

Without `undo`, markers are inert `Custom` nodes, and nothing else changes.

### 3. The undo hook: `reverse`

An extension that writes markers it must reverse itself defines:

```python
async def reverse(self, cursor: Cursor, marker: UndoMarker) -> None: ...
```

It is a **driver-level attribute, as `commands` is, not a hook point** (#640): the driver calls it
only through `Cursor.reverse(node)`, which finds the marker's owner among the loaded extensions,
hands it a `Cursor` for the same cursor bound to the owner (its writes are the owner's), and
returns whether one did (`False`: not loaded, or no `reverse`). `reverse` that is not a coroutine
function is a `DefinitionError` when the driver is built, as `start` and `stop` are. The name
`reverse` is taken: an extension that had an attribute of that name meaning something else must
rename it.

Why not a hook point: a hook runs for every extension at a point of every step; a reversal is one
extension's answer about one node it wrote, asked by one command. Why not a wire call: ruling 1;
`/undo` is a command like any other.

### 4. The session commands (EX-21)

The built-in `session`, default-on, removable. `/clear`, `/branch` and `/export` run as steps of
the cursor (`Busy` unless at rest), and each step ends when its command returns, so none carries
a turn on from where it leaves the cursor (§2); `/tree` runs at once.

- **`/clear`**: hang a new line under the last `System` node of the walk's prefix (the root
  `Config` and its `System` nodes, `prefix_length`). If the branch's settings, folded along the
  walk's `Config` nodes (each a delta, #640), differ from the root's, one `Config` with them comes
  first (`model_config(model, extensions)`); then an undo marker (`/clear`, `clear`) that returns
  to where the cursor was. The cursor moves onto the marker, auto-step off, and rests (the marker
  and the `Config` decide nothing; the `System` node decides `Wait`). One root; the system prompt
  is not repeated, so the first request after `/clear` is byte for byte the first request of a new
  session with the same settings, and a provider's prompt cache stays warm. A branch with nothing
  said writes nothing; a walk with no `System` node in its prefix (a detached root) is refused.
- **`/branch [label]`**: open a new cursor on this cursor's node, and label it. This cursor stays.
- **`/tree [at]`**: the session's tree as text for a line client (trusty, the web client's
  command line): one line per node (`12 user: fix the parser`), a single child at the same indent,
  the children of a node with several drawn as branches (`├─`, `└─`), each open cursor where it is
  (`◀ 0 main`). `at` draws only the part below a node.
- **`/export [path] [thinking]`**: the cursor's walk as Markdown, `tau export`'s format: the
  answer, or written to `path` (relative to the session's working directory). A file that exists
  is refused, never overwritten: a client's command does not clobber the server's files.
- **No `/new`** (ruling 3): a new session is a client's move (Ctrl+N in the TUI).

### 5. Clients

- **The TUI.** Ctrl+Z on an empty editor, with `undo:undo` among the commands and the cursor at
  rest, runs `/undo` with `json=true` and `at` set to the node it viewed: the message it gives
  back goes into the editor, a change it undid is a toast (`undo: undid /clear`), a refusal a
  warning toast. Without the extension, or on a busy cursor, Ctrl+Z is its own edit-last, as
  before. This is one-way composition (#640 SC-D3): a client move may call a harness command;
  the harness never calls a client.
- **trusty and the web client** need nothing: a slash line goes to `run` as typed.

## Consequences

- Built-ins: `undo` and `session` are on by default; `tau ext disable undo` (or `session`) turns
  either off, and nothing else depends on them.
- New public names: `tau.UndoMarker`, `Cursor.reverse`. New attribute: `Extension.reverse`.
- Compaction's and prune's lines end in a marker, so a program that reads "the last node of the
  new line" reads the marker; the line's last message is the node before it.
- No wire change: `PROTOCOL` stays 3, the schema is unchanged.

## Open

- An undo of edit-last gives back the message's text, not its images (D31): an image a person
  attached is not refilled. A client that wants it reads the `User` node (`user.node`).
- `/undo` follows one marker per call. A checkpoint extension that writes one per tool result
  leaves `/undo file` to step back one result at a time; a "back to the start of the turn" would be
  a priority or an argument of its own, decided with EX-40.
