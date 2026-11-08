"""The tree screen (D23 section 6): the session's nodes as a spine and stubs, to go to, open a
cursor at, view, or label the viewed cursor on.

Carried from τ 0.15: tau-coding-agent/src/tau_coding_agent/tree_browser.py (tag v0.15.0): `_elide`
(here `elide`), `ZoneTree` (per-row painting of the type tag and of the path to the highlighted
node), the screen's layout, fold keys and detail pane. Not carried (the plan sends them to
commands or extensions): the 0.15 marks, branch-from-marks, copy and paste of subtrees, the hover
divergence, the fold of the detail pane. 0.15's row planner (a branch opens a level) is replaced by
`TreePlan` (`_treeplan`): the viewed cursor's walk straight down, every other branch one folded
stub.
The data is 0.16's: the rows come from `Client.nodes` in ranges (`read_nodes`) and then from the
`SessionView`, which the app keeps current from the stream; the screen returns what was asked
(`TreeIntent`), for the app to do with calls.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from time import perf_counter
from typing import Any, ClassVar, Literal, Protocol

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Static, Tree

from tau._client import Client
from tau._core import Node
from tau._events import Appended, CursorGone, CursorView, Event, Labels
from tau._usage import show
from tau.tui._dialogs import TauDialog, TextDialog
from tau.tui._treeplan import Key, TreeRow
from tau.tui._treerows import TreeRows, ZoneTree
from tau.tui._treetext import detail_text, plural
from tau.tui._view import SessionView

PAGE = 500
"""How many nodes one `Client.nodes` call asks for."""
BATCH = 0.1
"""Seconds the screen waits to redraw after the session changes, so a fast agent does not redraw
per node."""
_DETAIL_MIN_HEIGHT = 20  # below this many rows the detail pane gives its rows to the tree

TreeAction = Literal["go", "open", "label"]


@dataclass(frozen=True)
class TreeIntent:
    """What the tree screen was asked to do, on `cursor` (the cursor viewed when it was asked):
    `go` to `node`, `open` a cursor at `node`, or `label` the cursor `text` (`node` is then the
    node highlighted)."""

    action: TreeAction
    cursor: int
    node: int
    text: str | None = None


async def read_nodes(client: Client) -> list[Node]:
    """Every node of the session, read in ranges of `PAGE` (ids are dense and a node never
    changes)."""
    nodes: list[Node] = []
    while page := await client.nodes(len(nodes), PAGE):
        nodes.extend(page)
    return nodes


class TreeHost(Protocol):
    """What the tree screen needs of the app, read each time it is needed: the app replaces its
    `session` when it reconnects, and its `cursor` moves when the viewed cursor closes."""

    client: Client
    session: SessionView
    cursor: int

    async def view(self, cursor: int) -> None: ...


class TreeScreen(TauDialog[TreeIntent | None]):
    """Browse the session's tree: the walk of the viewed cursor straight down, every other branch
    a folded stub (see `TreePlan`).

    Keys: Up, Down, PgUp, PgDn, Home, End move the highlight; Left and Right fold and unfold (on
    a leaf, Left steps out to the row around it); Enter goes to the highlighted node (the viewed
    cursor moves there; on a busy cursor that aborts its step, which the app asks about); `[` and
    `]` jump to the previous and next cursor mark; `v` makes the cursor at the highlight the
    viewed one; `o` opens a cursor at the node; `l` labels the viewed cursor; `p` goes to the node
    the highlighted one was copied or derived from; `f` follows the viewed cursor again; Esc
    leaves.

    While it follows, which it does on opening, the highlight tracks the viewed cursor as it
    moves; moving the highlight by hand pauses that. The title says which. The screen reads
    `session` (kept current by the app); the app calls `session_changed` and the screen redraws
    after `BATCH`. If the viewed cursor closes it follows the app's. It dismisses with the
    `TreeIntent`, or `None`; the app makes the calls.

    `redraw_seconds` is how long the last redraw took, from the event to the highlight in place
    (the rows are changed in place, not made again, so it does not grow with the session)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "act('go')", "Go to", priority=True, show=False),
        Binding("left", "collapse", "Fold", show=False),
        Binding("right", "expand", "Unfold", show=False),
        Binding("o", "act('open')", "Open a cursor here", show=False),
        Binding("l", "label", "Label the cursor", show=False),
        Binding("v", "view", "View this cursor", show=False),
        Binding("f", "follow", "Follow the cursor", show=False),
        Binding("p", "source", "Go to the source node", show=False),
        Binding("left_square_bracket", "mark(-1)", "Previous cursor", show=False),
        Binding("right_square_bracket", "mark(1)", "Next cursor", show=False),
    ]

    DIALOG_ID = "tree-browser-dialog"
    _HELP = (
        "Enter go  v view  o open here  l label  [ ] cursors  p source  f follow  ←→ fold  "
        "Esc leave"
    )

    def __init__(self, nodes: list[Node], host: TreeHost, viewed: int) -> None:
        super().__init__("The session's tree")
        self._all = nodes
        self._host = host
        self._viewed = viewed
        self._following = True
        self._rows: TreeRows  # made with the tree, in `compose_body`
        self._walk = ""
        self._pending: Timer | None = None
        self._wanted: int | None = None  # where `[` or `]` put the highlight
        self.redraw_seconds = 0.0
        self._redrawing: float | None = None

    def dialog_title(self) -> str:
        return f"{self._title} · {'following' if self._following else 'paused (f follows)'}"

    def compose_body(self) -> ComposeResult:
        with Vertical(id="tree-browser-body"):
            tree = ZoneTree("session", id="tree-browser-tree")
            tree.show_root = False
            tree.guide_depth = 2
            self._rows = TreeRows(tree)
            yield tree
            with VerticalScroll(id="tree-detail"):
                yield Static("", id="tree-detail-text", markup=False)
        yield Static(self._HELP, id="tree-browser-help")

    def on_mount(self) -> None:
        self.on_resize()
        self._redraw()
        self.query_one(ZoneTree).focus()

    def on_unmount(self) -> None:
        if self._pending is not None:
            self._pending.stop()

    # Keeping up with the session

    def session_changed(self) -> None:
        """The session heard an event that changes the tree: redraw soon, once for a burst."""
        if self._pending is None and self.is_running:
            self._pending = self.set_timer(BATCH, self._redraw)

    def _redraw(self) -> None:
        """Plan the tree again from the session as it is now, bring the rows up to date, and put
        the highlight where it belongs: on the viewed cursor while following, where `[` or `]`
        put it, else where it was."""
        started = perf_counter()
        self._pending = None
        session = self._host.session
        if self._viewed not in session.views and self._host.cursor in session.views:
            self._viewed = self._host.cursor  # the viewed cursor closed: follow the app's
        while len(self._all) in session.nodes:
            self._all.append(session.nodes[len(self._all)])
        if any(v.at >= len(self._all) for v in session.views.values()):
            self.run_worker(self._read_more(), group="tree-nodes", exclusive=True)
            return  # a cursor is at a node this screen has not read (after a reconnect)
        kept = self._rows.highlighted()
        self._rows.draw(self._all, session, self._viewed)
        length, count = self._rows.walk.of(self._rows.viewed_at)
        self._walk = (
            f"cursor {self._viewed}: a walk of {plural(length, 'node')}, {show(count)} tokens\n\n"
        )
        self.query_one(".tau-dialog-title", Static).update(self.dialog_title())
        target: tuple[int, Key | None] = (self._rows.viewed_at, None)
        if not self._following and self._wanted is not None:
            target = (self._wanted, None)
        elif not self._following and kept is not None:
            target = (kept.node.id, kept.key)
        self._redrawing = started
        self.query_one(ZoneTree).call_after_refresh(self._place, *target)

    async def _read_more(self) -> None:
        """Read the nodes the session has that this screen has not, and draw."""
        while page := await self._host.client.nodes(len(self._all), PAGE):
            self._all.extend(page)
        self._redraw()

    def _place(self, node_id: int, key: Key | None = None) -> None:
        self._rows.place(node_id, key)
        self._show_highlighted()
        if self._redrawing is not None:
            self.redraw_seconds = perf_counter() - self._redrawing
            self._redrawing = None

    def on_resize(self) -> None:
        self.query_one("#tree-detail").display = self.screen.size.height >= _DETAIL_MIN_HEIGHT
        self._rows.relabel()

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[TreeRow]) -> None:
        """A fold opened by the person: remember it, and make the rows it shows."""
        event.stop()
        if event.node.data is not None:
            self._rows.fill(event.node)
            self._rows.fold(event.node.data, True)

    def on_tree_node_collapsed(self, event: Tree.NodeCollapsed[TreeRow]) -> None:
        event.stop()
        if event.node.data is not None:
            self._rows.fold(event.node.data, False)

    # The highlight

    def _show_highlighted(self) -> None:
        row = self._rows.highlighted()
        if row is None:
            return
        self.query_one(ZoneTree).set_path(frozenset(self._rows.plan.ancestry(row.node.id)))
        self.query_one("#tree-detail-text", Static).update(
            self._walk + detail_text(row, self._rows.walk.of)
        )

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[TreeRow]) -> None:
        event.stop()
        self._show_highlighted()

    def on_tree_node_selected(self, event: Tree.NodeSelected[TreeRow]) -> None:
        """A click highlights; only Enter goes."""
        event.stop()
        self.query_one(ZoneTree).move_cursor(event.node)

    def on_zone_tree_moved(self, event: ZoneTree.Moved) -> None:
        """The person moved the highlight: stop following."""
        event.stop()
        self._wanted = None
        self._set_following(False)

    def _set_following(self, following: bool) -> None:
        self._following = following
        self.query_one(".tau-dialog-title", Static).update(self.dialog_title())

    def action_follow(self) -> None:
        """`f`: follow the viewed cursor again: the highlight goes to it now."""
        self._set_following(True)
        self._place(self._rows.viewed_at)

    # Actions

    def action_act(self, action: TreeAction) -> None:
        """Enter and `o`: go to, or open a cursor at, the highlighted node. A folded branch is
        not a node: it is asked to be unfolded first."""
        row = self._rows.highlighted()
        if row is None:
            return
        if row.kind == "stub":
            what = "go to" if action == "go" else "open a cursor at"
            self.notify(f"Unfold the branch (→) and pick a node to {what}", severity="warning")
            return
        self.dismiss(TreeIntent(action, self._viewed, row.node.id))

    def action_label(self) -> None:
        """`l`: ask for a label and put it on the viewed cursor."""
        row = self._rows.highlighted()
        if row is None:
            return
        node, viewed = row.node.id, self._viewed

        def labelled(name: str | None) -> None:
            if name is not None:
                self.dismiss(TreeIntent("label", viewed, node, name))

        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            TextDialog("Label", f"A label for cursor {viewed} (it moves from any other)"),
            labelled,
        )

    async def action_view(self) -> None:
        """`v`: view the cursor at the highlight (or on the branch the highlight is on); the tree
        is drawn again with its walk as the spine, and follows it."""
        row = self._rows.highlighted()
        if row is None:
            return
        here = self._rows.cursors_at(row)
        if not here:
            self.notify("No open cursor here", severity="warning")
            return
        cursor = next((c for c in here if c != self._viewed), here[0])
        if cursor != self._viewed:
            try:
                await self._host.view(cursor)
            except (LookupError, ConnectionError) as e:
                self.notify(str(e), severity="error")
                return
            self._viewed = cursor
        self._following = True
        self._redraw()

    def action_mark(self, step: int) -> None:
        """`[` and `]`: highlight the previous or next node with a cursor at it, open or closed,
        in node order, around the ends; a branch folded over it is unfolded."""
        marks = self._rows.plan.marks()
        row = self._rows.highlighted()
        if not marks or row is None:
            self.notify("No cursors to jump to", severity="warning")
            return
        here = row.node.id
        later = [m for m in marks if m > here] if step > 0 else [m for m in marks if m < here][::-1]
        target = later[0] if later else (marks[0] if step > 0 else marks[-1])
        self._set_following(False)
        self._rows.reveal(target)
        self._wanted = target
        self.query_one(ZoneTree).call_after_refresh(self._place, target)

    def action_source(self) -> None:
        """`p`: highlight the node the highlighted one was copied or derived from, unfolding what
        hides it. A node that came from none says so."""
        row = self._rows.highlighted()
        if row is None:
            return
        node = row.node
        source = node.copied_from if node.derived_from is None else node.derived_from
        if source is None:
            self.notify(
                f"Node {node.id} was not copied or derived from another", severity="warning"
            )
            return
        self._set_following(False)
        self._rows.reveal(source)
        self._wanted = source
        self.query_one(ZoneTree).call_after_refresh(self._place, source)

    def action_collapse(self) -> None:
        """Left: fold the turn or branch the highlight is on, else step out to the row around
        it."""
        tree = self.query_one(ZoneTree)
        node = tree.cursor_node
        if node is None:
            return
        if node.children and node.is_expanded:
            node.collapse()
        elif node.parent is not None and node.parent is not tree.root:
            self._set_following(False)
            tree.move_cursor(node.parent)

    def action_expand(self) -> None:
        """Right: unfold the turn or branch the highlight is on."""
        node = self.query_one(ZoneTree).cursor_node
        if node is not None and node.allow_expand and not node.is_expanded:
            self._rows.fill(node)
            node.expand()


def tell_trees(screens: Iterable[Screen[Any]], event: Event) -> None:
    """Tell every tree screen among `screens` that the session heard `event`, if it changes the
    tree."""
    if isinstance(event, Appended | CursorView | CursorGone | Labels):
        for screen in screens:
            if isinstance(screen, TreeScreen):
                screen.session_changed()
