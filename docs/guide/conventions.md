# Conventions clients and extensions may rely on

τ's wire and its extension API carry everything below already; what this page adds is a promised
*shape*. A convention is a way of using what a session holds (a `Custom` node, a command's
answer) that τ's built-ins follow and keep stable, so that a client can show it and another
extension can build on it without a change to the protocol. A client that does not know a
convention still works: it shows a `Custom` node as its kind, and a command's answer as text.

## Undo markers

A change that `/undo` can take back leaves an **undo marker** in the tree: a `Custom` node of
kind `undo`, written by the extension that made the change, whose `data` is a JSON object.

```json
{"label": "/compact", "return_to": 41, "reverses": "compact", "priority": "move"}
```

| key | what it holds |
|---|---|
| `label` | what was done, as a person would name it: `/compact`, `/clear`, `edit of a.py` |
| `return_to` | the node going back puts the cursor on: where the cursor was before the change |
| `reverses` | the kind of change, a short name its owner chooses: `compact`, `prune`, `clear`, `checkpoint` |
| `priority` | which `/undo` follows it: `move` (the default when the key is absent) or another name, such as `file` |
| anything else | the owner's own data (a checkpoint's commit, say), which `/undo` passes back to the owner |

What a client and an extension may rely on:

- **The writer is the owner.** The node's `by` is `Extension(name)` of the extension that made the
  change. `compaction`, `prune` and `session` (`/clear`) write `move` markers.
- **A marker is never sent to a model**, like every `Custom` node, and it **decides nothing**: the
  cursor rests or goes on as the node before the marker says. Compaction at the hard limit writes
  its marker in the middle of a turn, and the turn carries on under it.
- **The walk is the undo stack.** `/undo` walks back from the cursor's node toward the root, so it
  only ever reaches markers on that cursor's branch, nearest first, and undoing a change moves
  the cursor off its marker.
- **`move` markers need no owner.** Going back is a move of the cursor to `return_to`, so a
  `move` marker still works when the extension that wrote it is switched off.
- **Any other priority is the owner's to reverse.** A plain `/undo` passes over it (so a file
  checkpoint written mid-turn never takes over "edit my last message"); `/undo file` follows the
  nearest `file` marker, and only if the extension that wrote it is loaded and reverses it.
- **Without the `undo` extension**, markers are inert records: the session, the tree screen and
  `tau export` treat them as any `Custom` node.

`tau.UndoMarker` writes and reads them. `UndoMarker.of` gives `None` for any other body and a
`ValueError` naming the problem for a marker whose data is not this shape:

```python
from tau import Body, UndoMarker

marker = UndoMarker("edit of a.py", 12, "checkpoint", "file", {"commit": "abc123"})
body = marker.body()
assert isinstance(body, Body.Custom) and body.kind == "undo"
print(body.data)
print(UndoMarker.of(body) == marker, UndoMarker.of(Body.User("hi")))
```

```text expect
{"commit": "abc123", "label": "edit of a.py", "return_to": 12, "reverses": "checkpoint", "priority": "file"}
True None
```

### Writing a marker from an extension

Write the marker after the change, on the line the cursor goes on from. A command or hook that
moves the cursor to a new line puts the marker at the end of that line and moves onto it; one that
changes something outside the tree writes it under the cursor (`cursor.write`), with `return_to`
the node it should go back to.

An extension whose markers are not `move` reverses them itself: it defines `reverse`, which
`/undo <priority>` calls (through `Cursor.reverse`) before it moves the cursor. `reverse` is given
a cursor handle bound to the extension and the marker. Raising refuses the undo, and the cursor
stays where it was. A file checkpoint, in outline:

```python skip
from tau import Cursor, Subject, UndoMarker


class Checkpoints:
    name = "checkpoints"

    async def post_result(self, cursor: Cursor, subject: Subject) -> None:
        assert subject.node is not None and subject.node.parent is not None
        commit = snapshot_the_working_tree()  # yours
        marker = UndoMarker("edit", subject.node.parent, "checkpoint", "file", {"commit": commit})
        cursor.write(marker.body())

    async def reverse(self, cursor: Cursor, marker: UndoMarker) -> None:
        restore_the_working_tree(marker.extra["commit"])  # yours; raise to refuse
```

`/undo file` then restores the files and puts the cursor back on the batch's node, before its
results. A step that would carry the turn on from there (run the tools again) is halted instead,
and the status bar says so: step to carry on.

### What `/undo` answers

`/undo` answers text for a person (`undid /compact: back to node 41`; for the last message, `back
to node 12, before your message:` and the message). With `json=true` it answers an object a client
acts on:

```json
{"from": 44, "to": 12, "marker": null, "label": "your last message", "user": {"node": 13, "text": "fix the parser"}}
```

`marker` is the marker's node (`null` for the last message), and `user` is what to put back in an
editor: the message as it was typed (`Node.typed`, else its text), or `null` when a change was
undone. Pass `at` (the node the client shows the cursor on) and `/undo` refuses if the cursor has
moved since, so two people pressing undo on one cursor undo one change, not two. The terminal UI's
Ctrl+Z does exactly this when `undo` is loaded.
