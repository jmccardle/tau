"""The rows of the tree screen (D23 section 6): the `ZoneTree` that paints them and `TreeRows`,
which gives a `TreePlan`'s rows widgets a level at a time, fits their words to the width and
puts the highlight on a node. No keys and no session: `_tree` decides what to draw and when.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual.message import Message
from textual.widgets import Tree
from textual.widgets.tree import TreeNode as WidgetNode

from tau._core import Node
from tau.tui._treeplan import Key, TreePlan, TreeRow, TreeShape, WalkLength
from tau.tui._treetext import KIND_CLASS, Marks, RowLabel, fit, row_label
from tau.tui._view import SessionView


class ZoneTree(Tree[TreeRow]):
    """A `Tree` of rows that paints each row's type tag (`user:`) in the kind's colour, the
    dimmed marks of closed cursors, and the gutter of the rows on the path to the highlighted
    node. Rows are not DOM nodes and cannot carry classes, so it uses `COMPONENT_CLASSES` and
    `render_label`; every colour is in `tau.tcss`. It says when the person moves the highlight
    (`Moved`), as opposed to the screen moving it."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "tree--zone-path",
        "tree--closed",
        "tree--kind-stub",
        *KIND_CLASS.values(),
    }

    class Moved(Message):
        """The person moved the highlight with a key or the mouse."""

    def __init__(self, label: str, *, id: str | None = None) -> None:
        super().__init__(label, id=id)
        self._path: frozenset[int] = frozenset()
        self._paint: dict[Key, tuple[str, int, int | None]] = {}

    def paint(self, key: Key, kind: str, tag_size: int, dim_at: int | None) -> None:
        """Row `key`'s colours: the component class of its first `tag_size` characters and where
        its dimmed part begins in its label (`None`: nowhere)."""
        self._paint[key] = (kind, tag_size, dim_at)

    def forget_paint(self) -> None:
        """Drop every row's colours, before the rows are made again."""
        self._paint.clear()

    def set_path(self, path: frozenset[int]) -> None:
        """The nodes on the path to the highlighted one. The line cache is cleared by hand: its
        key holds no zone state, and the rows whose style changes are all the rows on the old and
        new paths."""
        self._path = path
        self._line_cache.clear()  # pyright: ignore[reportUnknownMemberType]
        self.refresh()

    def get_label_width(self, node: WidgetNode[TreeRow]) -> int:
        """The width of a row's label with its toggle, without styling it: Textual asks for every
        row's whenever the rows change, and styling each (`render_label`) costs a second on a
        long session."""
        icon = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
        label = node.label
        text = label.plain if isinstance(label, Text) else str(label)
        return (cell_len(icon) if node.allow_expand else 0) + cell_len(text)

    def render_label(self, node: WidgetNode[TreeRow], base_style: Style, style: Style) -> Text:
        """Textual's label with the zone styling spanned over it. The highlighted row is left
        alone: its own style already is the strongest state on the screen."""
        text = super().render_label(node, base_style, style)
        row = node.data
        if row is None or node is self.cursor_node:
            return text
        label = node.label
        split = len(text.plain) - len(label.plain if isinstance(label, Text) else label)
        kind, size, dim_at = self._paint.get(row.key, ("", 0, None))
        if kind and (end := min(split + size, len(text.plain))) > split:
            text.stylize(self.get_component_rich_style(kind, partial=True), split, end)
        if dim_at is not None:
            dim = self.get_component_rich_style("tree--closed", partial=True)
            text.stylize(dim, split + dim_at, len(text.plain))
        if row.node.id in self._path and split > 0:
            text.stylize(self.get_component_rich_style("tree--zone-path", partial=True), 0, split)
        return text

    def on_click(self) -> None:
        """A click moves the highlight."""
        self.post_message(self.Moved())

    def action_cursor_up(self) -> None:
        self.post_message(self.Moved())
        super().action_cursor_up()

    def action_cursor_down(self) -> None:
        self.post_message(self.Moved())
        super().action_cursor_down()

    def action_page_up(self) -> None:
        self.post_message(self.Moved())
        super().action_page_up()

    def action_page_down(self) -> None:
        self.post_message(self.Moved())
        super().action_page_down()

    def action_scroll_home(self) -> None:
        self.post_message(self.Moved())
        super().action_scroll_home()

    def action_scroll_end(self) -> None:
        self.post_message(self.Moved())
        super().action_scroll_end()


_REPLACE = 8
"""Most rows a redraw takes out one by one; past that the tree is made again (removing a row
costs a scan of its siblings)."""


@dataclass
class _Shown:
    """A row that has widgets: where, what it is now, its words and how deep it sits."""

    widget: WidgetNode[TreeRow]
    row: TreeRow
    label: RowLabel
    depth: int


class TreeRows:
    """What `tree` shows of one plan. `draw` plans again from the session; a spine that only grew
    (the usual redraw) changes the rows that differ in place and adds the new ones, and a spine
    that changed is made again. Only open rows have children, and `fill` makes a folded row's when
    it is unfolded. The folds the person made (`fold`) outlast a redraw; every other row opens by
    the plan's default."""

    def __init__(self, tree: ZoneTree) -> None:
        self.tree = tree
        self.plan: TreePlan
        self.marks: Marks
        self.shape = TreeShape()
        self.walk = WalkLength()
        self.viewed_at = 0
        """The node the viewed cursor is at (open, or closed and about to be left)."""
        self._top: list[TreeRow] = []
        self._widgets: list[WidgetNode[TreeRow]] = []
        self._folds: dict[Key, bool] = {}
        self._shown: dict[Key, _Shown] = {}
        self._filled: set[Key] = set()
        self._marked: set[int] = set()  # the nodes cursors are at
        self._marked_rows: set[Key] = set()  # the rows with cursors on them, or at them
        self._said: set[Key] = set()  # rows this class folded or unfolded, whose event will come

    def draw(self, nodes: Sequence[Node], session: SessionView, viewed: int) -> None:
        """Plan the tree from `nodes` and `session` with `viewed`'s walk as the spine, and bring
        the rows up to date. `LookupError` if `viewed` is neither open nor closed."""
        views = session.views
        self.viewed_at = views[viewed].at if viewed in views else session.gone[viewed]
        self.shape.extend(nodes)
        self.plan = TreePlan(self.shape, {c: v.at for c, v in views.items()}, session.gone, viewed)
        self.marks = Marks(views, session.gone, session.labels, viewed)
        self.walk.extend(nodes)
        before, self._marked = (
            self._marked,
            {
                *(v.at for v in views.values()),
                *session.gone.values(),
            },
        )
        new = self.plan.top()
        kept = 0
        while kept < min(len(new), len(self._top)) and self._top[kept].key == new[kept].key:
            kept += 1
        if not self._widgets or len(self._top) - kept > _REPLACE:
            self.tree.clear()
            self.tree.forget_paint()
            self._shown.clear()
            self._filled.clear()
            self._marked_rows.clear()
            self._widgets = []
            kept = 0
        else:
            for i in range(kept):
                if self._top[i] is not new[i] and self._top[i] != new[i]:
                    self._update(self._widgets[i], new[i])
            for gone in self._widgets[kept:]:
                self._drop(gone)
            del self._widgets[kept:]
        self._widgets += self._add(self.tree.root, new[kept:], 0)
        self._top = new
        self._relabel_marked(before | self._marked)

    def highlighted(self) -> TreeRow | None:
        """The row under the highlight."""
        node = self.tree.cursor_node
        return None if node is None else node.data

    def cursors_at(self, row: TreeRow) -> list[int]:
        """The open cursors the highlight is on when it is on `row`: a stub's, a folded turn's
        (everything inside it), else those at its node."""
        shown = self._shown.get(row.key)
        if row.kind == "stub" or (row.kind == "turn" and shown and not shown.widget.is_expanded):
            return list(row.cursors)
        return self.marks.open_at.get(row.node.id, [])

    def fold(self, row: TreeRow, opened: bool) -> None:
        """`row` was opened or closed. If the person did it, remember (it outlasts a redraw);
        either way say so in its words."""
        if row.key in self._said:
            self._said.discard(row.key)
        else:
            self._folds[row.key] = opened
        if (shown := self._shown.get(row.key)) is not None:
            self._refresh(shown)

    def fill(self, made: WidgetNode[TreeRow]) -> None:
        """Give the children of `made` widgets, if they have none yet."""
        row = made.data
        if row is None or row.key in self._filled:
            return
        self._filled.add(row.key)
        self._add(made, self.plan.children(row), self._shown[row.key].depth + 1)

    def relabel(self) -> None:
        """Fit every row's text to the tree's width: it draws one unwrapped line per row, so a
        longer text would grow a horizontal scrollbar."""
        for shown in self._shown.values():
            self._show(shown)

    def reveal(self, node_id: int) -> None:
        """Unfold every row on the way to `node_id`, and keep them unfolded."""
        for row in self.plan.locate(node_id)[:-1]:
            if (shown := self._shown.get(row.key)) is not None and not shown.widget.is_expanded:
                self._folds[row.key] = True
                self.fill(shown.widget)
                self._said.add(row.key)
                shown.widget.expand()
                self._refresh(shown)

    def place(self, node_id: int, key: Key | None = None) -> None:
        """Highlight the row `key` (else the one that shows `node_id`), or the nearest one on the
        way that is not folded away."""
        reached = self.tree.root
        for row in self.plan.locate(node_id, key):
            shown = self._shown.get(row.key)
            if shown is None:
                break
            reached = shown.widget
            if row.expandable and not reached.is_expanded:
                break
        if reached is not self.tree.root:
            self.tree.move_cursor(reached)

    # Making and changing rows

    def _add(
        self, parent: WidgetNode[TreeRow], rows: list[TreeRow], depth: int
    ) -> list[WidgetNode[TreeRow]]:
        """Give `rows` widgets under `parent`; the open ones get their children too, the folded
        ones wait to be unfolded."""
        made_all: list[WidgetNode[TreeRow]] = []
        for row in rows:
            opened = row.expandable and self._folds.get(row.key, row.opens)
            label = row_label(row, self.marks, folded=not opened)
            made = parent.add(label.text, data=row, expand=opened, allow_expand=row.expandable)
            shown = _Shown(made, row, label, depth)
            self._shown[row.key] = shown
            self._note_marked(row)
            self._show(shown)
            made_all.append(made)
            if opened:
                self.fill(made)
        return made_all

    def _note_marked(self, row: TreeRow) -> bool:
        """Whether cursors are on or at `row`, remembered."""
        marked = bool(row.cursors or row.closed or row.node.id in self._marked)
        if marked:
            self._marked_rows.add(row.key)
        else:
            self._marked_rows.discard(row.key)
        return marked

    def _update(self, made: WidgetNode[TreeRow], row: TreeRow) -> None:
        """`made` is the same row with other counts: say so, and make its children again if it
        has any."""
        shown = self._shown[row.key]
        shown.row = row
        self._note_marked(row)
        made.data = row
        made.allow_expand = row.expandable
        if row.key in self._filled:
            self._purge(made, keep=True)
            made.remove_children()
            self._filled.discard(row.key)
        opened = row.expandable and self._folds.get(row.key, row.opens)
        if opened != made.is_expanded:
            self._said.add(row.key)  # our own change, not the person's
            if opened:
                made.expand()
            else:
                made.collapse()
        if opened:
            self.fill(made)
        self._refresh(shown)

    def _drop(self, made: WidgetNode[TreeRow]) -> None:
        self._purge(made, keep=False)
        made.remove()

    def _purge(self, made: WidgetNode[TreeRow], *, keep: bool) -> None:
        """Forget `made`'s children (and `made` too unless `keep`): their widgets go with the
        row they are under."""
        for child in made.children:
            self._purge(child, keep=False)
        if not keep and made.data is not None:
            self._shown.pop(made.data.key, None)
            self._filled.discard(made.data.key)
            self._marked_rows.discard(made.data.key)

    def _relabel_marked(self, nodes: set[int]) -> None:
        """Say again what the rows with cursors on them say (a cursor may have started or stopped
        working, or been labelled), and the rows at `nodes`, which cursors came to or left. A row
        whose counts changed was said again when it was updated."""
        keys: set[Key] = set(self._marked_rows)
        for n in nodes:
            keys.add(("node", n))
            keys.add(("turn", n))
        for key in keys:
            if (shown := self._shown.get(key)) is not None:
                self._note_marked(shown.row)
                self._refresh(shown)

    def _refresh(self, shown: _Shown) -> None:
        label = row_label(shown.row, self.marks, folded=not shown.widget.is_expanded)
        if label != shown.label:
            shown.label = label
            self._show(shown)

    def _show(self, shown: _Shown) -> None:
        """Set the row's text, cut to the tree's width, and its colours."""
        label = shown.label
        width = self._width()
        text, dim_at = (
            fit(label, width - self._indent(shown.row, shown.depth))
            if width
            else (label.text, None)
        )
        shown.widget.set_label(text)
        self.tree.paint(shown.row.key, label.kind, label.tag_size, dim_at)

    def _width(self) -> int:
        return max(self.tree.content_size.width - self.tree.styles.scrollbar_size_vertical, 0)

    def _indent(self, row: TreeRow, depth: int) -> int:
        guide = self.tree.guide_depth
        return depth * guide + (guide if row.expandable else 0)
