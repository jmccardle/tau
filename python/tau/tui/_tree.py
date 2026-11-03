"""The tree screen (D23 section 6): the session's nodes as a tree, to go to, open a cursor at, or
label the viewed cursor on.

Carried from τ 0.15: tau-coding-agent/src/tau_coding_agent/tree_browser.py (tag v0.15.0): `_elide`
(here `elide`), the row planner `plan_tree_rows` (a fork opens a level; a user message opens a
level the next user message closes; turn groups mount folded except where a cursor is), `ZoneTree`
(per-row painting of the type tag and of the path to the highlighted node), the screen's layout,
fold keys and detail pane. Not carried (the plan sends them to commands or extensions): marks,
elide, branch-from-marks, copy and paste of subtrees, the hover divergence, the fold of the
detail pane.
The data is 0.16's: the rows come from `Client.nodes` in ranges (`read_nodes`), a row is marked
with the cursors at it and their labels, and the screen returns what was asked (`TreeIntent`), for
the app to do with calls.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar, Literal

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static, Tree
from textual.widgets.tree import TreeNode as WidgetNode

from tau._client import Client
from tau._core import Body, Node, Outcome, Part
from tau._events import CursorView
from tau.tui._dialogs import TauDialog, TextDialog
from tau.tui._transcript import author_name, config_line

PAGE = 500
"""How many nodes one `Client.nodes` call asks for."""
_MIN_WIDTH = 2
_DETAIL_MIN_HEIGHT = 20  # below this many rows the detail pane gives its rows to the tree
_PREVIEW = 200  # characters of a node's text a row keeps before the width elides it

TreeAction = Literal["go", "open", "label"]


@dataclass(frozen=True)
class TreeIntent:
    """What the tree screen was asked to do: `go` to `node` (the viewed cursor), `open` a cursor
    at `node`, or `label` the viewed cursor `text` (`node` is then the node highlighted)."""

    action: TreeAction
    node: int
    text: str | None = None


async def read_nodes(client: Client) -> list[Node]:
    """Every node of the session, read in ranges of `PAGE` (ids are dense and a node never
    changes)."""
    nodes: list[Node] = []
    while page := await client.nodes(len(nodes), PAGE):
        nodes.extend(page)
    return nodes


def elide(text: str, width: int) -> str:
    """`text` cut to `width` cells, ending in `…` when anything was cut. A width with no room for
    a character and the mark is `…` alone: a row that cannot be shortened shows it, and does not
    grow the tree a scrollbar."""
    if width < _MIN_WIDTH:
        return "…"
    return text if len(text) <= width else text[: width - 1] + "…"


def describe(node: Node) -> tuple[str, str]:
    """A node's type tag and its one-line text: `user`, `assistant`, `tool`, `system`, `config` or
    `custom`."""
    match node.body:
        case Body.User(text=text):
            return "user", " ".join(text.split())[:_PREVIEW]
        case Body.Assistant(parts=parts):
            said = " ".join("".join(p.text for p in parts if isinstance(p, Part.Text)).split())
            calls = [p.tool for p in parts if isinstance(p, Part.Call)]
            return "assistant", said[:_PREVIEW] or "calls " + ", ".join(calls)
        case Body.Results(outcomes=outcomes):
            return "tool", f"{len(outcomes)} result{'s' if len(outcomes) != 1 else ''}"
        case Body.System():
            return "system", "system prompt"
        case Body.Config(config=config):
            return "config", config_line(config.data)
        case Body.Custom(kind=kind):
            return "custom", kind
        case _:
            raise TypeError(f"node {node.id} holds a body the tree does not know")


def full_text(node: Node) -> str:
    """What the detail pane shows of `node`: who wrote it and all of its text."""
    tag, short = describe(node)
    head = f"{tag}  ·  node {node.id}  ·  by {author_name(node.by)}\n\n"
    match node.body:
        case Body.User(text=text) | Body.System(text=text):
            return head + text
        case Body.Assistant(parts=parts):
            lines = [
                p.text if isinstance(p, Part.Text) else f"{p.tool}({p.args_raw})"
                for p in parts
                if isinstance(p, Part.Text | Part.Call)
            ]
            return head + "\n".join(lines)
        case Body.Results(outcomes=outcomes):
            return head + "\n".join(f"{call}: {_outcome(o)}" for call, o, _by in outcomes)
        case Body.Custom(data=data):
            return head + data
        case _:
            return head + short


def _outcome(outcome: Outcome) -> str:
    match outcome:
        case Outcome.Ok(value=value):
            return value
        case Outcome.Err(error=error):
            return f"error: {error}"
        case _:
            return "interrupted before it finished"


@dataclass(frozen=True)
class TreeRow:
    """One row the screen draws and where it sits: `parent` is an index into the row list (`None`:
    the widget root), `depth` the widget depth, `expanded` whether its children show."""

    node: Node
    parent: int | None
    depth: int
    expanded: bool
    has_children: bool


def plan_tree_rows(nodes: Sequence[Node], here: Collection[int]) -> list[TreeRow]:
    """What the screen draws, under what, at what depth. Pure, so it is tested without a
    terminal. `nodes` are in id order (a parent before its children); `here` are the nodes
    cursors are at.

    Two nesting rules, which compose. A fork opens a level, so a run of single-child nodes is a
    run of siblings and the indent counts branches, not messages. A `User` node opens a level and
    the next `User` node closes it (it is that group's sibling), so a turn can be folded and a
    hundred linear turns are a hundred rows at depth 0. Turn groups mount folded, except the
    groups a cursor's row is inside; fork rows never fold. Iterative: a linear session is one
    step per node and Python's recursion limit is 1000."""
    children: dict[int, list[Node]] = {}
    roots: list[Node] = []
    for node in nodes:
        if node.parent is None:
            roots.append(node)
        else:
            children.setdefault(node.parent, []).append(node)
    built: list[tuple[Node, int | None, int, bool]] = []
    top: tuple[int | None, int] = (None, 0)
    stack: list[tuple[Node, tuple[int | None, int], tuple[int | None, int]]] = [
        (node, top, top) for node in reversed(roots)
    ]
    while stack:
        node, outer, current = stack.pop()
        is_turn = isinstance(node.body, Body.User)
        container = outer if is_turn else current
        index = len(built)
        built.append((node, container[0], container[1], is_turn))
        kids = children.get(node.id, [])
        mine = (index, container[1] + 1)
        if len(kids) > 1:
            child_outer = child_current = mine
        elif is_turn:
            child_outer, child_current = outer, mine
        else:
            child_outer, child_current = outer, current
        stack.extend((kid, child_outer, child_current) for kid in reversed(kids))
    open_groups = _groups_around(built, here)
    parents = {parent for _n, parent, _d, _t in built if parent is not None}
    return [
        TreeRow(node, parent, depth, not is_turn or i in open_groups, i in parents)
        for i, (node, parent, depth, is_turn) in enumerate(built)
    ]


def _groups_around(
    built: Sequence[tuple[Node, int | None, int, bool]], here: Collection[int]
) -> set[int]:
    """The rows on the widget chain above each row whose node is in `here`: the groups that
    mount open."""
    open_groups: set[int] = set()
    for i, (node, _parent, _depth, _turn) in enumerate(built):
        walk: int | None = i if node.id in here else None
        while walk is not None:
            open_groups.add(walk)
            walk = built[walk][1]
    return open_groups


_KIND_CLASS = {
    "user": "tree--kind-user",
    "assistant": "tree--kind-assistant",
    "tool": "tree--kind-tool",
    "system": "tree--kind-system",
    "config": "tree--kind-structural",
    "custom": "tree--kind-structural",
}


class ZoneTree(Tree[int]):
    """A `Tree` of node ids that paints each row's type tag (`user:`) in the kind's colour and
    the gutter of the rows on the path to the highlighted node. Rows are not DOM nodes and cannot
    carry classes, so it uses `COMPONENT_CLASSES` and `render_label`; every colour is in
    `tau.tcss`."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"tree--zone-path", *_KIND_CLASS.values()}

    def __init__(self, label: str, *, id: str | None = None) -> None:
        super().__init__(label, id=id)
        self._path: frozenset[int] = frozenset()
        self._kinds: dict[int, tuple[str, int]] = {}

    def set_kinds(self, kinds: dict[int, tuple[str, int]]) -> None:
        """Each row's `(class, tag length)`: the span the tag and its colon take. A kind is a
        property of the node, so this is set once, before the first row is drawn."""
        self._kinds = kinds

    def set_path(self, path: frozenset[int]) -> None:
        """The nodes on the path to the highlighted one. The line cache is cleared by hand: its
        key holds no zone state, and the rows whose style changes are all the rows on the old and
        new paths."""
        self._path = path
        self._line_cache.clear()  # pyright: ignore[reportUnknownMemberType]
        self.refresh()

    def render_label(self, node: WidgetNode[int], base_style: Style, style: Style) -> Text:
        """Textual's label with the zone styling spanned over it. The highlighted row is left
        alone: its own style already is the strongest state on the screen."""
        text = super().render_label(node, base_style, style)
        node_id = node.data
        if node_id is None or node is self.cursor_node:
            return text
        label = node.label
        split = len(text.plain) - len(label.plain if isinstance(label, Text) else label)
        kind = self._kinds.get(node_id)
        if kind is not None:
            component, size = kind
            end = min(split + size, len(text.plain))
            if end > split:
                text.stylize(self.get_component_rich_style(component, partial=True), split, end)
        if node_id in self._path and split > 0:
            text.stylize(self.get_component_rich_style("tree--zone-path", partial=True), 0, split)
        return text


def marks_of(
    node: int, cursors: Mapping[int, CursorView], labels: Sequence[tuple[str, int]], viewed: int
) -> str:
    """The cursors at `node` as a row shows them: `◀ 2 main`, the viewed cursor `◀ [1]`."""
    out = ""
    for cursor, view in sorted(cursors.items()):
        if view.at == node:
            names = " ".join(name for name, on in labels if on == cursor)
            out += "  ◀ " + (f"[{cursor}]" if cursor == viewed else str(cursor))
            out += f" {names}" if names else ""
    return out


class TreeScreen(TauDialog[TreeIntent | None]):
    """Browse the session's tree. Enter goes to the highlighted node (the viewed cursor moves
    there; on a busy cursor that aborts its step, which the app asks about), `o` opens a cursor
    there, `l` labels the viewed cursor; Left folds a branch or turn and Right opens it; Esc
    leaves. The highlighted node is shown whole beside the rows. It dismisses with the
    `TreeIntent`, or `None`; the app makes the calls."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "commit", "Go to", priority=True, show=False),
        Binding("left", "collapse", "Fold", show=False),
        Binding("right", "expand", "Unfold", show=False),
        Binding("o", "open", "Open a cursor here", show=False),
        Binding("l", "label", "Label the cursor", show=False),
    ]

    DIALOG_ID = "tree-browser-dialog"
    _HELP = "Enter go to   o open a cursor here   l label this cursor   ←→ fold   Esc leave"

    def __init__(
        self,
        nodes: Sequence[Node],
        cursors: Mapping[int, CursorView],
        labels: Sequence[tuple[str, int]],
        viewed: int,
    ) -> None:
        super().__init__("The session's tree")
        self._by_id = {n.id: n for n in nodes}
        self._plan = plan_tree_rows(nodes, [v.at for v in cursors.values()])
        self._cursors = cursors
        self._labels = labels
        self._viewed = viewed
        self._rows: list[tuple[WidgetNode[int], str, int, bool]] = []

    def compose_body(self) -> ComposeResult:
        with Vertical(id="tree-browser-body"):
            tree = ZoneTree("session", id="tree-browser-tree")
            tree.show_root = False
            tree.guide_depth = 2
            yield tree
            with VerticalScroll(id="tree-detail"):
                yield Static("", id="tree-detail-text", markup=False)
        yield Static(self._HELP, id="tree-browser-help")

    def on_mount(self) -> None:
        tree = self.query_one(ZoneTree)
        tree.set_kinds(
            {
                row.node.id: (_KIND_CLASS[tag], len(tag) + 1)
                for row in self._plan
                for tag in [describe(row.node)[0]]
            }
        )
        widgets: list[WidgetNode[int]] = []
        for row in self._plan:
            label = self._label(row.node)
            parent = tree.root if row.parent is None else widgets[row.parent]
            made = parent.add(
                label, data=row.node.id, expand=row.expanded, allow_expand=row.has_children
            )
            widgets.append(made)
            self._rows.append((made, label, row.depth, row.has_children))
        here = self._cursors[self._viewed].at if self._viewed in self._cursors else None
        target = next((w for w in widgets if w.data == here), None)
        if target is not None:
            tree.call_after_refresh(tree.move_cursor, target)
        tree.call_after_refresh(self._relabel)
        tree.call_after_refresh(self._show_highlighted)
        self._apply_detail()
        tree.focus()

    def _label(self, node: Node) -> str:
        tag, text = describe(node)
        return f"{tag}: {text}" + marks_of(node.id, self._cursors, self._labels, self._viewed)

    def on_resize(self) -> None:
        self._apply_detail()
        self._relabel()

    def _apply_detail(self) -> None:
        self.query_one("#tree-detail").display = self.screen.size.height >= _DETAIL_MIN_HEIGHT

    def _relabel(self) -> None:
        """Fit every row's text to the tree's width: it draws one unwrapped line per node, so a
        longer text would grow a horizontal scrollbar."""
        tree = self.query_one(ZoneTree)
        width = tree.content_size.width - tree.styles.scrollbar_size_vertical
        if width <= 0:
            return
        for made, label, depth, has_children in self._rows:
            toggle = tree.guide_depth if has_children else 0
            made.set_label(elide(label, width - depth * tree.guide_depth - toggle))

    def _path_to(self, node: int) -> frozenset[int]:
        path: set[int] = set()
        step: int | None = node
        while step is not None:
            path.add(step)
            step = self._by_id[step].parent
        return frozenset(path)

    def _show_highlighted(self) -> None:
        tree = self.query_one(ZoneTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return
        tree.set_path(self._path_to(node.data))
        self.query_one("#tree-detail-text", Static).update(full_text(self._by_id[node.data]))

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[int]) -> None:
        event.stop()
        self._show_highlighted()

    def on_tree_node_selected(self, event: Tree.NodeSelected[int]) -> None:
        """A click highlights; only Enter goes."""
        event.stop()
        self.query_one(ZoneTree).move_cursor(event.node)

    def _highlighted(self) -> int | None:
        node = self.query_one(ZoneTree).cursor_node
        return None if node is None else node.data

    def action_commit(self) -> None:
        """Enter: go to the highlighted node."""
        if (node := self._highlighted()) is not None:
            self.dismiss(TreeIntent("go", node))

    def action_open(self) -> None:
        """`o`: open a cursor at the highlighted node."""
        if (node := self._highlighted()) is not None:
            self.dismiss(TreeIntent("open", node))

    def action_label(self) -> None:
        """`l`: ask for a label and put it on the viewed cursor."""
        node = self._highlighted()
        if node is None:
            return

        def labelled(name: str | None) -> None:
            if name is not None:
                self.dismiss(TreeIntent("label", node, name))

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            TextDialog("Label", f"A label for cursor {self._viewed} (it moves from any other)"),
            labelled,
        )

    def action_collapse(self) -> None:
        """Left: fold the branch or turn the highlight is on, else step out to the one around
        it."""
        tree = self.query_one(ZoneTree)
        node = tree.cursor_node
        if node is None:
            return
        if node.children and node.is_expanded:
            node.collapse()
        elif node.parent is not None and node.parent is not tree.root:
            tree.move_cursor(node.parent)

    def action_expand(self) -> None:
        """Right: unfold the branch or turn the highlight is on."""
        node = self.query_one(ZoneTree).cursor_node
        if node is not None and node.children and not node.is_expanded:
            node.expand()
