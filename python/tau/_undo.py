"""`Undo`: the built-in extension behind `/undo` (D33).

`/undo` goes back over the last change on the cursor's branch. It walks back from the cursor's
node, toward the root, to the first of:

- an undo marker (`tau._markers`) whose `priority` is the one asked for (`move` unless given):
  the cursor goes to its `return_to`. A marker of any priority but `move` is reversed by the
  extension that wrote it first (`Cursor.reverse`); if that extension is not loaded, or cannot,
  nothing moves and the command says why;
- for `move` only, a `User` node (edit-last, D23's Ctrl+Z): the cursor goes to its parent, and
  the message is given back to refill an editor (its `typed` line, else its text).

Everything it follows is on the walk, so an undo never reaches a change made on another branch,
and the walk is the undo stack: the marker of a change, once undone, is off the walk. The cursor
moves as a go-to does (auto-step off). The command runs as a step of the cursor, so a busy cursor
refuses it with `Busy`, as every cursor command does; `at` refuses it when the cursor is not on
the node the caller saw, so two clients cannot both undo one change. A cursor left mid-turn (a
marker's `return_to` that would sample) halts that step before the model is asked (`Stays`).

Without this extension, markers are inert `Custom` nodes and nothing else changes.
"""

import json
from typing import Annotated, Literal

from tau._commands import CommandFailed
from tau._core import Author, Body, Node, NoSuchNode
from tau._handles import Cursor, Subject
from tau._markers import MOVE, PRIORITY, Stays, UndoMarker
from tau._values import NodeId


def _target(walk: list[Node], priority: str) -> tuple[Node, UndoMarker | None] | None:
    """The node `/undo <priority>` goes back over, nearest the tip first: a marker of that
    priority, or (for `move`) a `User` node. `None` if the walk has neither."""
    for node in reversed(walk):
        try:
            marker = UndoMarker.of(node.body)
        except ValueError as e:
            raise CommandFailed(f"node {node.id} is a malformed undo marker: {e}") from None
        if marker is not None and marker.priority == priority:
            return node, marker
        if priority == MOVE and isinstance(node.body, Body.User) and node.parent is not None:
            return node, None
    return None


def _nothing(walk: list[Node], priority: str) -> CommandFailed:
    """Why `/undo <priority>` found nothing on `walk`, saying how the word was read: a word that
    is no priority, or the priorities this branch does have markers of."""
    if priority == MOVE:
        return CommandFailed("nothing to undo on this branch")
    if not PRIORITY.fullmatch(priority):
        return CommandFailed(
            f"{priority!r} is not a priority: one is lower-case letters, digits, _ and -"
        )
    markers = (UndoMarker.of(n.body) for n in walk)
    marked = sorted({m.priority for m in markers if m is not None})
    have = f", which has markers of priority {', '.join(marked)}" if marked else ""
    return CommandFailed(
        f"no undo marker of priority {priority!r} on this branch{have}; "
        "a plain /undo follows move markers and your last message"
    )


def _refill(node: Node) -> str:
    """What a person typed for the `User` node `node`: its `typed` line, else its text."""
    assert isinstance(node.body, Body.User)
    return node.body.text if node.typed is None else node.typed


class Undo:
    """The built-in extension `undo` (the module docstring)."""

    name = "undo"

    def __init__(self) -> None:
        self._stays = Stays()
        self.commands = (self.undo,)

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        """Halt the step of a `/undo` that left the cursor mid-turn."""
        return self._stays.halt(cursor.id)

    async def post_step(self, cursor: Cursor, subject: Subject) -> str | None:
        """Forget what an aborted step's `/undo` left."""
        self._stays.forget(cursor.id)
        return None

    async def undo(
        self,
        cursor: Cursor,
        priority: Annotated[str, "which markers to follow (move: the tree's own changes)"] = MOVE,
        at: Annotated[NodeId | None, "refuse unless the cursor is on this node"] = None,
        format: Annotated[Literal["text", "json"], "json: the same answer, for a program"] = "text",
    ) -> str:
        """Go back over the last change on this branch: /compact, /prune, /clear, or else your
        last message, which is given back to edit. The old line stays in the tree."""
        state = cursor.cursor(cursor.id)
        if at is not None and state.at != at:
            raise CommandFailed(
                f"cursor {cursor.id} is at node {state.at}, not {at}: it moved; nothing undone"
            )
        walk = cursor.walk()
        found = _target(walk, priority)
        if found is None:
            raise _nothing(walk, priority)
        node, marker = found
        if marker is None:
            assert node.parent is not None
            to, label, user = node.parent, "your last message", node
        else:
            to, label, user = await self._reverse(cursor, node, marker), marker.label, None
        cursor.move(to)
        cursor.set_auto(False)
        self._stays.leave(cursor, "/undo")
        if format == "json":
            return json.dumps(
                {
                    "from": state.at,
                    "to": to,
                    "marker": None if marker is None else node.id,
                    "label": label,
                    "user": None if user is None else {"node": user.id, "text": _refill(user)},
                },
                ensure_ascii=False,
            )
        if user is None:
            return f"undid {label}: back to node {to}"
        return f"back to node {to}, before your message:\n\n{_refill(user)}"

    async def _reverse(self, cursor: Cursor, node: Node, marker: UndoMarker) -> int:
        """Where the marker `node` sends the cursor, once what it marks is reversed: by its
        owner, for any priority but `move` (which moving the cursor reverses)."""
        try:
            target = cursor.tree.walk(marker.return_to)
        except NoSuchNode:
            raise CommandFailed(
                f"the marker at node {node.id} ({marker.label}) returns to node "
                f"{marker.return_to}, which is not in this session"
            ) from None
        if any(n.id == node.id for n in target):
            raise CommandFailed(
                f"the marker at node {node.id} ({marker.label}) returns to node "
                f"{marker.return_to}, below itself: undoing it would never leave it"
            )
        if marker.priority == MOVE:
            return marker.return_to
        if not isinstance(node.by, Author.Extension):
            raise CommandFailed(
                f"{marker.label} (node {node.id}) was written by {node.by}, not an extension, so "
                "nothing can reverse it: nothing undone"
            )
        if not await cursor.reverse(node.id):
            raise CommandFailed(
                f"{marker.label} (node {node.id}) is reversed by the extension {node.by.name!r}, "
                "which is not loaded or has no reverse: nothing undone"
            )
        return marker.return_to
