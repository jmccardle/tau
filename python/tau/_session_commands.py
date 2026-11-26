"""`SessionCommands`: the built-in extension `session` (D33): `/clear`, `/branch`, `/tree`,
`/export`.

- `/clear` starts the conversation again without a new session: it hangs a new line under the
  last `System` node of the cursor's prefix (the one the walk already has, so the prompt the model
  is sent starts with the same bytes and its cache stays warm). If the branch's settings (the
  model, and the extension options its `Config` nodes set, folded along the walk) differ from the
  root's, one `Config` carrying them comes first; then an undo marker that returns to where the
  cursor was. The cursor moves onto the marker and rests there. There is still one root.
- `/branch [label]` opens a new cursor on the node this one is on (a branch of the conversation
  to try something else on), and labels it.
- `/tree [at]` draws the session's tree as text, for a client that has no tree of its own: one
  line per node, a branch indented under the node it leaves, each open cursor where it is.
- `/export [path] [thinking]` writes the cursor's walk as Markdown, as `tau export` does: to the
  new file `path` (relative to the session's working directory; an existing file is refused, never
  overwritten), or as the command's answer.

There is no `/new`: a new session is a client's move (Ctrl+N in the TUI), not the harness's.
`/clear`, `/branch` and `/export` run as steps of the cursor (`Busy` when it is not at rest), and
halt that step if it would carry a turn on from where the cursor stands (`Stays`).
"""

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Annotated

from tau._commands import CommandFailed
from tau._config import config_fields, model_config
from tau._core import Body, Node, Part, Tree
from tau._handles import Cursor, Subject, TreeHandle
from tau._lines import Planned, prefix_length, write_line
from tau._markdown import to_markdown
from tau._markers import Stays, UndoMarker
from tau._values import NodeId

_SHOWN = 60
"""The most characters of a node's text a line of `/tree` shows."""

Settings = tuple[str | None, Mapping[str, Mapping[str, object]]]
"""A branch's model and extension options, folded along a walk's `Config` nodes."""


def folded(walk: list[Node]) -> Settings:
    """The settings the `Config` nodes of `walk` add up to, each a delta over the ones before."""
    model: str | None = None
    extensions: dict[str, dict[str, object]] = {}
    for node in walk:
        if isinstance(node.body, Body.Config):
            fields = config_fields(node.body.config)
            model = fields.model if fields.model is not None else model
            for name, options in fields.extensions.items():
                extensions[name] = {**extensions.get(name, {}), **options}
    return model, extensions


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= _SHOWN else line[: _SHOWN - 1] + "…"


def describe_node(node: Node) -> str:
    """A node as one line of `/tree`: its id, its kind and the start of what it says."""
    match node.body:
        case Body.System(text=text):
            what = f"system: {_first_line(text)}"
        case Body.User(text=text):
            what = f"user: {_first_line(node.typed or text)}"
        case Body.Assistant(parts=parts):
            said = " ".join(p.text for p in parts if isinstance(p, Part.Text))
            calls = sum(isinstance(p, Part.Call) for p in parts)
            more = f" [{calls} call{'s' if calls != 1 else ''}]" if calls else ""
            what = f"assistant: {_first_line(said)}{more}"
        case Body.Results(outcomes=outcomes):
            what = f"results: {len(outcomes)}"
        case Body.Config(config=config):
            what = f"config: {' '.join(config.data.split())}"
        case Body.Custom(kind=kind):
            try:
                marker = UndoMarker.of(node.body)
            except ValueError:  # a malformed marker is drawn by its kind
                marker = None
            what = kind if marker is None else f"{kind}: {marker.label}"
        case _:
            what = "?"
    return f"{node.id} {what}"


def tree_lines(tree: Tree, root: int, at: Mapping[int, str]) -> Iterator[str]:
    """The lines of `/tree` for the nodes from `root` down: a chain stays at one indent, and each
    child of a node with several starts a branch (`├─`, the last `└─`) indented under it. `at`
    is what to show after a node a cursor is on."""
    stack: list[tuple[int, str, str]] = [(root, "", "")]  # (node, its line's lead, under it)
    while stack:
        node, lead, under = stack.pop()
        while True:
            shown = describe_node(tree.node(node))
            yield f"{lead}{shown}{at.get(node, '')}"
            children = tree.children(node)
            if len(children) != 1:
                break
            node, lead = children[0], under
        for i, child in reversed(list(enumerate(children))):
            last = i == len(children) - 1
            branch, rest = ("└─ ", "   ") if last else ("├─ ", "│  ")
            stack.append((child, under + branch, under + rest))


class SessionCommands:
    """The built-in extension `session` (the module docstring)."""

    name = "session"

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd
        self._stays = Stays()
        self.commands = (self.clear, self.branch, self.tree, self.export)

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        """Halt the step of a command that left its cursor mid-turn."""
        return self._stays.halt(cursor.id)

    async def post_step(self, cursor: Cursor, subject: Subject) -> None:
        self._stays.forget(cursor.id)

    async def clear(self, cursor: Cursor) -> str:
        """Start this conversation again, keeping the system prompt and this branch's settings
        (/undo goes back). The old line stays in the tree."""
        walk = cursor.walk()
        prefix = prefix_length(walk)
        if prefix == 0 or not isinstance(walk[prefix - 1].body, Body.System):
            raise CommandFailed("this branch has no system prompt to start again from")
        if not any(
            isinstance(n.body, (Body.User, Body.Assistant, Body.Results)) for n in walk[prefix:]
        ):
            return "nothing to clear"
        here = walk[-1].id
        planned: list[Planned] = []
        model, extensions = folded(walk)
        if (model, extensions) != folded(walk[:1]):
            planned.append(Planned(Body.Config(model_config(model, extensions or None))))
        planned.append(Planned(UndoMarker("/clear", here, "clear").body()))
        ids = write_line(cursor, walk[prefix - 1].id, planned)
        cursor.move(ids[-1])
        cursor.set_auto(False)
        kept = " and this branch's settings" if len(planned) > 1 else ""
        return (
            f"cleared: a new line under the system prompt{kept} (node {ids[-1]}; /undo goes back)"
        )

    async def branch(
        self, cursor: Cursor, label: Annotated[str | None, "a label for the new cursor"] = None
    ) -> str:
        """Open a new cursor on the node this one is on, to try something else from here."""
        here = cursor.cursor(cursor.id).at
        new = cursor.open(here)
        if label:
            cursor.set_label(label, new)
        self._stays.leave(cursor, "/branch")
        named = f" ({label})" if label else ""
        return f"opened cursor {new}{named} at node {here}"

    async def tree(
        self,
        tree: TreeHandle,
        at: Annotated[NodeId | None, "draw only the part below this node"] = None,
    ) -> str:
        """Draw the session's tree as text, with each open cursor where it is."""
        here: dict[int, str] = {}
        for cursor in tree.cursors():
            state = tree.cursor(cursor)
            labels = " ".join(state.labels)
            mark = f"{cursor} {labels}".rstrip()
            here[state.at] = here.get(state.at, "") + f"  ◀ {mark}"
        roots = [at] if at is not None else [n.id for n in _roots(tree.tree)]
        return "\n".join(line for root in roots for line in tree_lines(tree.tree, root, here))

    async def export(
        self,
        cursor: Cursor,
        path: Annotated[Path | None, "a new Markdown file to write (none: answer with it)"] = None,
        thinking: Annotated[bool, "include the model's thinking"] = False,
    ) -> str:
        """Write this conversation as Markdown, as `tau export` does."""
        text = to_markdown(cursor.walk(), thinking=thinking)
        self._stays.leave(cursor, "/export")
        if path is None:
            return text
        target = path if path.is_absolute() else self._cwd / path
        if target.exists():
            raise CommandFailed(f"{path} exists: name another file, or remove it first")
        target.write_text(text, encoding="utf-8")
        return f"wrote {len(text.splitlines())} lines to {path}"


def _roots(tree: Tree) -> Iterator[Node]:
    """Every root of `tree`: the first, and any detached one."""
    for id in range(len(tree)):
        node = tree.node(id)
        if node.parent is None:
            yield node
