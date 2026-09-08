"""tree_browser — split out of app.py."""

from typing import Optional, Any, Callable, ClassVar, Literal
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static, Tree
from textual.binding import Binding
from textual import events
from tau_agent_core.conversation_tree import ConversationTree, TreeNode
from tau_agent_core.tree_surgery import (
    COPYABLE_KINDS,
    branch_refusal_reason,
    paste_refusal_reason,
    plan_paste,
    selection_order,
    tool_group,
)
from tau_coding_agent import transcript
from tau_coding_agent.dialogs import ChoiceDialog, TauDialog, TextDialog
from dataclasses import replace, dataclass
from rich.style import Style
from rich.text import Text
from textual.message import Message
from textual.widgets.tree import TreeNode as WidgetTreeNode


_ELIDE_MIN_WIDTH = 2


_ELIDE_TOO_NARROW = "…"


def _elide(text: str, width: int) -> str:
    """``text`` cut to ``width`` cells, ending in ``…`` when anything was cut.

    Below :data:`_ELIDE_MIN_WIDTH` the label is *replaced* by
    :data:`_ELIDE_TOO_NARROW` rather than returned whole. The previous behaviour
    returned the text unchanged on the theory that a label elided to nothing tells
    the reader less than one that overflows. It tells them less either way, and the
    overflow is not free: ``textual.widgets.Tree`` renders one unwrapped line per
    node and sizes ``virtual_size`` to the widest of them, so one un-elided row grows
    a horizontal scrollbar across the whole browser — the exact defect this function
    exists to prevent, manufactured by the function itself
    (TREE-BROWSER-AS-EDITOR.md §1.1, "a Fail-Early inversion in its own right").

    A row that cannot be shortened is a bug worth showing. ``…`` in a column with no
    room for anything else shows it, and costs one cell instead of the row's whole
    length (TREE-BROWSER-AS-EDITOR.md §2).
    """
    if width < _ELIDE_MIN_WIDTH:
        return _ELIDE_TOO_NARROW
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


TreeAction = Literal["navigate", "revise", "elide", "branch", "paste"]


@dataclass(frozen=True)
class TreeIntent:
    """What the browser was asked to do, and to which nodes (§5.3, §11.1).

    Replaces the bare ``Optional[str]`` the modal used to dismiss with. Every
    operation §1.3 lists needs more than a node id — a subtree summary is one node
    plus an action, a traversal summary is a *set* of nodes — so the return type is
    widened once, here, rather than rewritten per operation.

    ``TreeIntent("navigate", (id,))`` is the degenerate case §5.3 names: exactly
    what ``dismiss(id)`` used to mean, said in the wider vocabulary.

    Frozen, and ``ids`` is a tuple rather than a list, because the intent crosses a
    screen boundary: the modal is gone by the time the caller reads it, and a
    mutable answer would let the caller edit a record of what it was told.
    """

    action: TreeAction
    ids: tuple[str, ...]

    @property
    def sole_id(self) -> str:
        """The one id this intent applies to.

        Fail-Early for the callers that can only act on a single node (both of
        today's): an intent carrying zero or several ids means the modal answered a
        question the caller did not ask, and reading ``ids[0]`` would act on an
        arbitrary one of them instead of saying so.
        """
        if len(self.ids) != 1:
            raise ValueError(f"{self.action!r} intent names {len(self.ids)} ids, expected 1")
        return self.ids[0]


@dataclass(frozen=True)
class ElidePlan:
    """A legal ``elide_span`` call, worked out from the marked node and the cursor.

    **The two ends bracket what is KEPT, not what is removed.** This is the thing
    about an elide that a reader guesses backwards, and it is worth stating in
    the type rather than only in the manual: over ``[1,2,3,4,5,6]``, pairing 2
    with 4 leaves ``[2,3,4]``, not ``[1,5,6]``. An elide is the summary-less form
    of the compaction anchor, and a compaction keeps a tail and drops the head —
    ``ConversationTree._active_path_entries`` emits the anchor and then its
    ancestors from ``firstKeptId`` onward, so the kept region is always ONE
    contiguous run ending at the anchor. Cutting a span out of the middle is not
    a shape this operation can express at all.

    ``anchor`` is where the fold jumps FROM — the elide entry is appended under it
    and the conversation continues there. ``first_kept`` is where it jumps TO: the
    oldest entry the fold keeps.

    **Which of the two nodes is which is decided by the tree, not by the gesture
    order.** The two ends of an elide are an ancestor and a descendant of each
    other; the deeper one is always the anchor, because the shallower one is by
    construction on its path and the reverse is impossible. So the reader marks
    one node and puts the cursor on the other and does not have to remember which
    they picked first — which is what was asked for.

    Two counts, because they answer two different questions and the first one
    alone under-reports:

    * ``folded`` — entries the fold itself drops, measured at the ANCHOR. This is
      the number ``TauBackend.elide_span`` computes, and an elide that folds 0 is
      what it refuses, so this is what gates the offer.
    * ``dropped`` — entries that leave the context the model can see RIGHT NOW.
      Never smaller than ``folded``, and larger whenever the anchor is not the
      current tip: moving the cursor back to it abandons everything newer. Over
      ``[1..6]`` with the cursor at 6, pairing 2 with 4 folds ``[1]`` and drops
      ``[1,5,6]``. This is what the reader loses, so it is what the offer says.

    ``moves_cursor`` is that same difference stated as a fact rather than a
    number: the conversation will continue somewhere other than where it is now.
    """

    anchor: str
    first_kept: str
    folded: int
    dropped: int
    moves_cursor: bool


@dataclass(frozen=True)
class TreeRow:
    """One row the tree browser will draw, and where it sits (PLAN-0.9.4 §4).

    The output of :func:`plan_tree_rows`. ``parent`` is an INDEX into the row list
    rather than a node id, because two rows can name the same id only if the
    planner is broken, and an index makes the widget build a single pass with no
    lookup table. ``None`` means the widget root.

    ``depth`` is the WIDGET depth — what ``_relabel`` spends ``guide_depth`` cells
    on per level — and is not the ``parentId`` depth. See :meth:`_index` for the
    data depth, which is a different number and stays a property of the log.
    """

    node: TreeNode
    parent: Optional[int]
    depth: int
    expanded: bool
    has_children: bool


def _row_is_hidden(node: TreeNode) -> bool:
    """Whether this entry gets no row at all (PLAN-0.9.4 §4, item 4).

    A ``navigate`` entry records that the cursor moved. It carries no message, it
    is not a branch target worth naming, and it sits between an assistant message
    and the user message that forked off it — which is the one place an extra row
    does the most damage to the shape the reader is trying to read. Its children
    attach to its nearest drawn ancestor, which reads as what actually happened:
    the new turn hangs off the node it was forked from.

    Two exceptions, and neither is tidiness:

    * **The cursor is never hidden.** A browser that will not say where you are
      has failed at the one thing it must do.
    * **A ``navigate`` with more than one child is a real fork point.** Hiding it
      would draw two branches as one run — a shape the log does not have.

    Only ``navigate``. ``model_change`` and ``agent_spec`` carry no message
    either, but each records a real change to what the model is and what it was
    told, which is worth seeing while browsing history.
    """
    return node.kind == "navigate" and not node.is_leaf and len(node.children) <= 1


def _drawn_children(node: TreeNode) -> list[TreeNode]:
    """``node``'s children with hidden ones spliced out, in order.

    Recurses only through runs of hidden nodes (a ``navigate`` under a
    ``navigate``), which are at most a handful long.
    """
    drawn: list[TreeNode] = []
    for child in node.children:
        if _row_is_hidden(child):
            drawn.extend(_drawn_children(child))
        else:
            drawn.append(child)
    return drawn


def plan_tree_rows(roots: list[TreeNode]) -> list[TreeRow]:
    """Decide what the browser draws, under what, at what depth (PLAN-0.9.4 §4).

    Pure, and separate from the widget build, because these are the rules the
    owner's feedback was about and they are worth testing without a terminal.

    **Two nesting rules, and they compose.**

    1. **A fork opens a level** — TREE-BROWSER-AS-EDITOR.md §2, unchanged. A run
       of single-child entries is a run of SIBLINGS, so indent depth counts
       branches rather than messages and does not grow as a conversation does.
    2. **A user message opens a level, and the next user message closes it.** A
       user turn is the boundary §2 did not use: everything from a user message
       down to the next one is that turn, so it gets a widget parent and can be
       folded. The next user message is that group's SIBLING, not its child,
       which is what keeps rule 1's bound intact — a hundred linear turns is a
       hundred rows at depth 0, each holding its own tool traffic.

    The walk carries two containers to make rule 2 work. ``current`` is where an
    ordinary row attaches (inside the open turn group). ``outer`` is where the
    NEXT user message attaches, which is the group's own parent — that is the
    whole of "the group closes at the next user message". A fork sets both, since
    a fork's branches are the next turns.

    **Turn groups mount collapsed; everything else mounts open.** The exception is
    the groups the cursor row is actually inside — its WIDGET ancestors, not its
    ``parentId`` ancestors, and the difference is the whole rule. In a linear
    conversation every earlier user message is a ``parentId`` ancestor of the
    cursor but none of them is a widget ancestor, because rule 2 makes them
    siblings; keying off the data chain would leave every turn in the session
    open, which is the state the owner asked to get out of. Off the widget chain,
    exactly one turn opens — the one you are in. Fork rows never collapse: a
    browser that opens without showing where you are has failed at the one thing
    it must do, and folding the branch you are on is a gesture, not a default.

    Iterative rather than recursive: a linear conversation is one frame per entry
    and Python's default limit is 1000, so the recursive build this replaces would
    have raised on a long session. ``ConversationTree.tree`` went iterative for the
    same reason.
    """
    # (node, widget parent index or None, widget depth, opens a turn group)
    built: list[tuple[TreeNode, Optional[int], int, bool]] = []
    # A container is (row index or None for the widget root, depth for its rows).
    top: tuple[Optional[int], int] = (None, 0)
    drawn_roots: list[TreeNode] = []
    for root in roots:
        drawn_roots.extend([root] if not _row_is_hidden(root) else _drawn_children(root))
    stack: list[tuple[TreeNode, tuple[Optional[int], int], tuple[Optional[int], int]]] = [
        (node, top, top) for node in reversed(drawn_roots)
    ]
    while stack:
        node, outer, current = stack.pop()
        is_turn = node.kind == "message" and node.role == "user"
        container = outer if is_turn else current
        index = len(built)
        built.append((node, container[0], container[1], is_turn))
        children = _drawn_children(node)
        mine: tuple[Optional[int], int] = (index, container[1] + 1)
        if len(children) > 1:
            child_outer = child_current = mine
        elif is_turn:
            child_outer, child_current = outer, mine
        else:
            child_outer, child_current = outer, current
        for child in reversed(children):
            stack.append((child, child_outer, child_current))

    open_groups: set[int] = set()
    walk: Optional[int] = next((i for i, (n, _p, _d, _t) in enumerate(built) if n.is_leaf), None)
    while walk is not None:
        open_groups.add(walk)
        walk = built[walk][1]
    parents = {parent for _n, parent, _d, _t in built if parent is not None}
    return [
        TreeRow(
            node=node,
            parent=parent,
            depth=depth,
            expanded=not is_turn or i in open_groups,
            has_children=i in parents,
        )
        for i, (node, parent, depth, is_turn) in enumerate(built)
    ]


@dataclass(frozen=True)
class TreeZones:
    """The four selection sets the browser renders against (§5.3).

    §5.3's list, with set 3 ("derived — what the cursor node covers or connects")
    split into the three roles §3's class table actually distinguishes:

    1. ``cursor`` — one node; drives :class:`TreeDetailPane`.
    2. ``marked`` — the multi-select set; drives the counts and the lowest common
       ancestor.
    3. derived: ``path`` (the cursor's ancestor chain), ``folded`` (entries on that
       chain that the active splice anchor drops) and ``covered`` (``folded``, when
       the cursor IS that anchor).
    4. ``hidden`` — collapsed or archived. **View state** (§11.2): nothing is
       appended for either, and this is computed from the modal, never read from
       the log.

    ``copied`` is §7's set: the node ``c`` put on the clipboard and everything under
    it, which is what ``v`` would re-create. It was declared here, and in
    :attr:`ZoneTree.COMPONENT_CLASSES`, for two steps before it had a producer —
    with no default value, because a constructor that quietly fills in an empty set
    is how a real producer gets forgotten. :meth:`SessionTreeModal._copied_zone` is
    that producer.

    ``summary``/``abandoned`` are §4.3's pair, and ``hover_common``/
    ``hover_divergent`` are §3's hover divergence (§10 step 5). Both arrived after
    §5.3's list was written and neither is a *selection* set — the first is a fixed
    property of the log's shape, the second is a function of the mouse. They live
    here anyway so there is one zone container, one "nothing painted" value
    (:data:`_NO_ZONES`) and one place a renderer has to look. What differs is how
    they are REFRESHED: see :meth:`ZoneTree.set_hover_zones`.

    Frozen sets, because the renderer holds this across many ``render_label`` calls
    and a set mutated underneath it would paint two rows from two different states.
    """

    cursor: Optional[str]
    marked: frozenset[str]
    path: frozenset[str]
    folded: frozenset[str]
    covered: frozenset[str]
    hidden: frozenset[str]
    copied: frozenset[str]
    summary: frozenset[str]
    abandoned: frozenset[str]
    ineligible: frozenset[str]
    hover_common: frozenset[str]
    hover_divergent: frozenset[str]


_NO_ZONES = TreeZones(
    cursor=None,
    marked=frozenset(),
    path=frozenset(),
    folded=frozenset(),
    covered=frozenset(),
    hidden=frozenset(),
    copied=frozenset(),
    summary=frozenset(),
    abandoned=frozenset(),
    ineligible=frozenset(),
    hover_common=frozenset(),
    hover_divergent=frozenset(),
)


_TREE_KIND_CLASS: dict[str, str] = {
    # message roles
    "user": "tree--kind-user",
    "assistant": "tree--kind-assistant",
    "toolResult": "tree--kind-tool",
    "system": "tree--kind-system",
    "compaction": "tree--kind-structural",
    "branch_summary": "tree--kind-structural",
    "elide": "tree--kind-structural",
    "navigate": "tree--kind-structural",
    "model_change": "tree--kind-structural",
    "agent_spec": "tree--kind-structural",
    "customEntry": "tree--kind-structural",
}


def tree_kind_span(node: TreeNode) -> tuple[str, int] | None:
    """``(component class, tag length)`` for ``node``'s label, or ``None``.

    The tag length counts the tag AND its colon — ``"user:"`` is 5 — which is the
    range :meth:`ZoneTree.render_label` paints. ``None`` for a tag the table does
    not know: an unmapped kind renders in the row's ordinary colour rather than
    borrowing a hue that means something else (Fail-Early — an unknown kind should
    look unknown, not look like a tool result).
    """
    tag = node.role or node.kind
    component = _TREE_KIND_CLASS.get(tag)
    if component is None:
        return None
    return component, len(tag) + 1


class ZoneTree(Tree[str]):
    """A ``Tree`` that paints per-row *zone* styling (TREE-BROWSER-AS-EDITOR.md §3).

    Textual ``Tree`` rows are not DOM nodes and cannot carry per-row CSS classes,
    so §3 uses the two hooks that exist instead: a ``COMPONENT_CLASSES`` frozenset
    resolved through ``get_component_styles`` (textual 8.2.7, ``dom.py:601`` /
    ``widget.py:1175``), and an override of ``render_label`` (``_tree.py:877``),
    which Textual calls once per row.

    The classes name zone **roles**, not branches. A class per branch would mint an
    unbounded vocabulary that no stylesheet can enumerate; branch-distinguishing
    colour, when it is wanted, cycles a small fixed palette modulo N instead (§3) —
    not implemented here, and deliberately not faked with a role class.

    This subclass exists for the styling alone. It adds no state the tree does not
    already have except :attr:`zones`, and every colour lives in ``tau.tcss``.
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "tree--zone-path",
        # On the path, dropped by a splice anchor: in the chain, not in the context.
        "tree--zone-folded",
        # The same span, when the cursor is the anchor doing the dropping.
        "tree--zone-covered",
        # In the multi-select set.
        "tree--zone-marked",
        # Collapsed or archived (§11.2: view state, never read from the log).
        "tree--zone-hidden",
        "tree--zone-copied",
        "tree--zone-summary",
        "tree--zone-abandoned",
        "tree--zone-ineligible",
        "tree--zone-hover-common",
        "tree--zone-hover-divergent",
        "tree--kind-user",
        "tree--kind-assistant",
        "tree--kind-tool",
        "tree--kind-system",
        "tree--kind-structural",
    }

    _LABEL_ZONES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("marked", "tree--zone-marked"),
        ("ineligible", "tree--zone-ineligible"),
        ("hidden", "tree--zone-hidden"),
        ("copied", "tree--zone-copied"),
        ("covered", "tree--zone-covered"),
        ("folded", "tree--zone-folded"),
        ("summary", "tree--zone-summary"),
        ("abandoned", "tree--zone-abandoned"),
        ("path", "tree--zone-path"),
    )

    _HOVER_ZONES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("hover_divergent", "tree--zone-hover-divergent"),
        ("hover_common", "tree--zone-hover-common"),
    )

    _GUTTER_ZONES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("covered", "tree--zone-covered"),
        ("folded", "tree--zone-folded"),
    )

    class HoverChanged(Message):
        """The ROW under the mouse changed — not the mouse (§3, step 5).

        ``Tree.hover_line`` is a ``var`` (textual 8.2.7, ``_tree.py:655``), so
        ``watch_hover_line`` runs only when the line number actually changes.
        Sliding the mouse along one row therefore costs the assignment in
        ``_on_mouse_move`` and nothing else, and this message is posted at most once
        per row the pointer crosses. That is what makes it affordable to recompute
        anything at all on hover.

        Carries the ENTRY id rather than the widget node, because the divergence is
        a fact about ``parentId`` ancestry and the widget nesting counts forks (§2)
        — the two are deliberately different shapes. ``None`` when the pointer left
        the tree, or landed on a row with no entry behind it.
        """

        def __init__(self, zone_tree: "ZoneTree", entry_id: Optional[str]) -> None:
            super().__init__()
            self.zone_tree = zone_tree
            self.entry_id = entry_id

        @property
        def control(self) -> "ZoneTree":
            return self.zone_tree

    def __init__(self, label: str, *, id: Optional[str] = None) -> None:
        super().__init__(label, id=id)
        self._zones = _NO_ZONES
        self._kinds: dict[str, tuple[str, int]] = {}

    @property
    def zones(self) -> TreeZones:
        """The sets the next repaint will render against."""
        return self._zones

    def set_kinds(self, kinds: dict[str, tuple[str, int]]) -> None:
        """Tell the renderer each row's type tag: ``id -> (class, tag length)``.

        Handed in whole, once, at build time. Unlike every zone this is not state
        a gesture can move — a row's kind is a property of its entry — so there is
        no cache to clear and no repaint to ask for: it is set before the first
        row is drawn. Rows missing from the map render their tag plainly.
        """
        self._kinds = kinds

    def watch_hover_line(self, previous_hover_line: int, hover_line: int) -> None:
        """Tell the screen which row the pointer moved onto (§3, step 5).

        ``super()`` first: the base flips ``_hover`` on the two nodes and refreshes
        their regions, which is what drives ``tree--highlight-line`` and the guide
        hover. This adds the announcement the *divergence* highlight needs, and
        computes nothing itself — the ancestry it would need is the ``parentId``
        chain, which lives in :class:`SessionTreeModal` and not in a widget that
        nests by fork.

        ``_get_node`` is private and is used deliberately: it is the base's own way
        of turning a line number into a node (``_tree.py:1102``, called by the
        watcher this overrides), the mapping is not exposed publicly, and
        re-deriving it from ``_tree_lines`` would touch the same privates one level
        deeper.
        """
        super().watch_hover_line(previous_hover_line, hover_line)
        node = self._get_node(hover_line)
        data = None if node is None else node.data
        self.post_message(self.HoverChanged(self, None if data is None else str(data)))

    def set_zones(self, zones: TreeZones) -> None:
        """Replace the zone sets and repaint the visible rows.

        The line cache has to be dropped by hand. ``Tree._render_line``'s cache key
        (textual 8.2.7, ``_tree.py:1325-1332``) is ``(y, is_hover, width,
        self._updates, pseudo_class_state, per-node _updates)`` — zone state appears
        in none of it, and only the two nodes whose ``_selected`` flips get a new
        per-node ``_updates`` when the cursor moves (``_tree.py:178-181``). Every
        OTHER row on the old and new ancestor chains would keep serving the strip it
        was painted with, which is precisely the set of rows ``tree--zone-path``
        exists to change.

        ``self._line_cache.clear()`` rather than ``Tree._invalidate()``: the latter
        also drops ``_tree_lines_cached`` and asks for a layout pass, which rebuilds
        every row's width. Zone styling changes no row's WIDTH — ``render_label``
        adds spans, never characters — so the geometry is still correct and the cost
        should be the visible rows, not the whole tree, on every arrow key.
        """
        self._zones = zones
        self._line_cache.clear()
        self.refresh()

    def set_hover_zones(self, common: frozenset[str], divergent: frozenset[str]) -> None:
        """Replace ONLY the hover divergence, leaving the selection sets alone (step 5).

        The separate write path is the whole performance story of this step.
        :meth:`SessionTreeModal._refresh_zones` walks the conversation twice
        (``ConversationTree.path`` and ``context_entries``) and every widget row
        (``_hidden``); the hover divergence is two ``parentId`` walks and a common
        prefix, bounded by the tree's DEPTH. Rebuilding the whole
        :class:`TreeZones` on hover would put the first cost on every row the
        pointer crosses, so ``replace`` swaps the two hover fields and nothing else.

        **The no-op guard is not an optimisation detail.** The commonest hover is
        along the cursor's own path, where there IS no divergence and both sets stay
        empty; without this the reader would pay a full repaint per row for a frame
        that is identical to the last one. Returning early also keeps ``refresh``
        out of the ``_on_leave`` → already-empty case.
        """
        if common == self._zones.hover_common and divergent == self._zones.hover_divergent:
            return
        self._zones = replace(self._zones, hover_common=common, hover_divergent=divergent)
        self._line_cache.clear()
        self.refresh()

    def render_label(self, node: WidgetTreeNode[str], base_style: Style, style: Style) -> Text:
        """Paint one row's zone styling over Textual's own label (§3).

        Composes with the base rather than replacing it: ``super()`` assembles the
        expand toggle and the label with Textual's styles, and this adds spans over
        character RANGES of the result — the gutter portion gets the span zones, the
        text portion gets the selection zones. Rich combines span styles
        attribute-wise, so a zone that sets only ``color`` leaves the row's
        background and weight alone.

        A fourth range, ahead of those three: the row's TYPE TAG — the ``user:`` /
        ``toolResult:`` prefix — is painted from :meth:`set_kinds`. It is what the
        reader scans a long tree with, and it is not a zone (see the comment on
        the ``tree--kind-*`` entries in :attr:`COMPONENT_CLASSES`).

        **The cursor row is left alone.** Its style is resolved with ``partial=False``
        (``_tree.py:1424-1427``) and, when the tree has focus, sets a foreground
        against the cursor's own background; a zone colour layered on top wins the
        foreground and loses the contrast that made the row readable. The cursor is
        already the strongest state on the screen and needs no second marking. The
        cost is that marking the row under the cursor shows no change on that row —
        which is why :meth:`SessionTreeModal._marks_summary` reports the count. The
        hover divergence is skipped there for the same reason, and can only ever
        want the cursor row for its COMMON half anyway: the divergent tail is by
        construction the part of the hovered chain the cursor's does not contain.
        """
        text = super().render_label(node, base_style, style)
        entry_id = node.data
        if entry_id is None or entry_id == self._zones.cursor:
            return text
        label = node.label
        label_len = len(label.plain if isinstance(label, Text) else label)
        split = len(text.plain) - label_len
        kind = self._kinds.get(entry_id)
        if kind is not None:
            component, tag_len = kind
            tag_end = min(split + tag_len, len(text.plain))
            if tag_end > split:
                text.stylize(self.get_component_rich_style(component, partial=True), split, tag_end)
        gutter_class = self._first_zone(self._GUTTER_ZONES, entry_id)
        if gutter_class is not None and split > 0:
            text.stylize(self.get_component_rich_style(gutter_class, partial=True), 0, split)
        label_class = self._first_zone(self._LABEL_ZONES, entry_id)
        if label_class is not None:
            text.stylize(
                self.get_component_rich_style(label_class, partial=True),
                split,
                len(text.plain),
            )
        hover_class = self._first_zone(self._HOVER_ZONES, entry_id)
        if hover_class is not None:
            text.stylize(
                self.get_component_rich_style(hover_class, partial=True),
                0,
                len(text.plain),
            )
        return text

    def _first_zone(self, order: tuple[tuple[str, str], ...], entry_id: str) -> Optional[str]:
        """The first component class in ``order`` whose set holds ``entry_id``."""
        for field_name, component_class in order:
            members: frozenset[str] = getattr(self._zones, field_name)
            if entry_id in members:
                return component_class
        return None


class SessionTreeModal(TauDialog[Optional[TreeIntent]]):
    """Browse the conversation tree and pick a node to branch from (§3.2).

    Port of pi's ``showTreeSelector`` (interactive-mode.ts:4446): a
    ``textual.widgets.Tree`` populated from ``ConversationTree.tree()``, the current
    leaf highlighted. ``Enter`` dismisses with a :class:`TreeIntent`; ``Esc`` cancels
    (``None``). Copies the ``SystemPromptEditor`` modal template.

    Selecting and committing are two gestures, not one (TREE-BROWSER-AS-EDITOR.md
    §5.1). A click moves the cursor and leaves the browser open; only ``Enter``
    dismisses. ``left`` collapses a fork, or moves to the enclosing one (§5.2).

    The widget nesting handed to ``Tree`` counts **forks, not messages** (§2): see
    :meth:`on_mount`.

    **It takes the whole ``ConversationTree``, not ``roots`` plus a resolver**
    (§5.3). Three of the four selection sets are derived rather than handed in —
    ``path`` is ``ConversationTree.path``, ``folded``/``covered`` are the difference
    between that and ``context_entries``, and the lowest common ancestor of the
    marked set comes off the ``_parent_of`` map built at :meth:`_index`. A resolver
    alone cannot answer any of them. Collapsing ``roots`` and ``resolve_entry`` into
    the one object also removes the way they could disagree: the rows and the bodies
    are now provably the same log, where before a caller could pass a ``roots`` graph
    built from one tree and a resolver closed over another.

    This does not weaken the standing contract that a body must be showable — it
    strengthens it. ``resolve_entry`` was required because "a browser that cannot
    show a body is the elided-preview draft this replaced". A ``ConversationTree``
    cannot be passed without one: :meth:`~ConversationTree.entry` answers for every
    id :meth:`~ConversationTree.tree` produced, by construction.

    **It holds no ``SessionLog`` and performs no durable operation** (§11.1). Every
    gesture accumulates in-memory state and the commit returns one intent for the
    caller to apply. The rejected alternative — injecting a live editor the modal
    calls — is on the record in §11.1; the visible consequence of not taking it is
    that this class is constructible from a ``ConversationTree`` alone, which is what
    every test across four files does.

    ``title``/``help_text`` exist for the SECOND pick of the elide flow (W3), which
    asks a different question of the same browser — "where does the fold resume?"
    rather than "where do we branch from?". One reused browser with a different
    caption, not a second widget: the tree, the leaf highlight and the key handling
    are identical, and only the sentence above them is not.

    Rows sit beside a :class:`TreeDetailPane` showing the highlighted node in full
    — the rows say *which* node, the pane says *what it is*.
    """

    # Textual merges BINDINGS across the MRO, so escape/cancel comes from TauDialog.
    BINDINGS = [
        Binding("enter", "commit", "Choose", priority=True, show=False),
        Binding("left", "collapse", "Collapse", show=False),
        Binding("right", "expand", "Expand", show=False),
        Binding("space", "toggle_mark", "Mark", priority=True, show=False),
        Binding("ctrl+d", "toggle_detail", "Detail pane", priority=True, show=False),
        Binding("ctrl+e", "elide", "Elide", priority=True, show=False),
        Binding("ctrl+b", "branch", "Branch from marks", priority=True, show=False),
        Binding("c", "copy", "Copy subtree", priority=True, show=False),
        Binding("v", "paste", "Paste subtree", priority=True, show=False),
    ]

    DIALOG_ID = "tree-browser-dialog"

    DETAIL_MIN_HEIGHT = 20

    def __init__(
        self,
        tree: ConversationTree,
        *,
        title: str = "Browse Conversation Tree",
        help_text: str = (
            "↵ pick Space mark ←→ fold ^E elide ^B branch c copy v paste ^D pane Esc"
        ),
        copied: Optional[str] = None,
    ) -> None:
        super().__init__(title)
        self._tree = tree
        self._roots = tree.tree()
        self._resolve_entry: Callable[[str], dict[str, Any]] = tree.entry
        self._help_text = help_text
        self._rows: list[tuple[Any, str, int, bool]] = []
        self._marked: set[str] = set()
        self._by_id: dict[str, TreeNode] = {}
        self._parent_of: dict[str, str] = {}
        self._depth_of: dict[str, int] = {}
        for root in self._roots:
            self._index(root, 0)
        self._summary_zone, self._abandoned_zone = self._branch_summary_pairs()
        self._hovered: Optional[str] = None
        self._copied: Optional[str] = (
            copied if copied is not None and tree.contains(copied) else None
        )
        self._detail_folded = False
        self._elide_line = self._line_through(self._elide_other_end())

    def _index(self, node: TreeNode, depth: int) -> None:
        self._by_id[node.id] = node
        self._depth_of[node.id] = depth
        for child in node.children:
            self._parent_of[child.id] = node.id
            self._index(child, depth + 1)

    def compose_body(self) -> ComposeResult:
        with Vertical(id="tree-browser-body"):
            tree = ZoneTree("session", id="tree-browser-tree")
            tree.show_root = False
            tree.guide_depth = 2
            yield tree
            yield transcript.TreeDetailPane(self._resolve_entry)
            yield Static(
                "▸ detail pane hidden — ctrl+D, or click here, to show it",
                id="tree-detail-folded",
            )
        yield Static(self._marks_summary(), id="tree-browser-marks")
        yield Static(self._help_text, id="tree-browser-help")

    # -- the detail pane's window on the tree --------------------------------

    def _view_of(self, node_id: str) -> "transcript.DetailView | None":
        """The three-node window around ``node_id``, or ``None`` if unknown.

        ``None`` rather than a raise: ``Tree.NodeHighlighted`` also fires for the
        widget's own hidden root, whose ``data`` is ``None`` and which names no
        conversation node at all.
        """
        selected = self._by_id.get(node_id)
        if selected is None:
            return None
        parent_id = self._parent_of.get(node_id)
        previous = self._by_id.get(parent_id) if parent_id is not None else None
        following = selected.children[0] if selected.children else None
        return transcript.DetailView(
            selected=selected,
            previous=previous,
            following=following,
            earlier=self._depth_of[previous.id] if previous is not None else 0,
            later=self._subtree_size(following) - 1 if following is not None else 0,
            branches=len(selected.children),
        )

    @staticmethod
    def _subtree_size(node: TreeNode) -> int:
        total = 0
        stack = [node]
        while stack:
            current = stack.pop()
            total += 1
            stack.extend(current.children)
        return total

    async def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """Move the detail pane, and the zone sets, to what the cursor now sits on."""
        await self._show_node(event.node.data)
        self._refresh_zones()

    def on_tree_node_collapsed(self, event: Tree.NodeCollapsed) -> None:
        """Folding a branch changes set 4 (``hidden``), so the zones are stale.

        The labels are NOT refitted here. A fold changes how many rows the tree
        holds and therefore whether it has a vertical scrollbar — but
        :meth:`_relabel` reserves that width whether the bar is there or not, so
        the answer it gives does not depend on a fold. See the comment there for
        why chasing the current state instead is a loop.
        """
        self._refresh_zones()

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        """The other direction. Not posted during :meth:`on_mount`'s build — the
        rows are added with ``add(expand=…)``, which sets the flag without a
        message (textual 8.2.7, ``_tree.py:426-431``), so this does not fire once
        per row at startup."""
        self._refresh_zones()

    async def _show_cursor_node(self) -> None:
        """Draw the pane for wherever the cursor already is.

        ``Tree`` posts ``NodeHighlighted`` only when ``cursor_line`` *changes*, so
        a session whose current leaf is also the first row — a conversation with
        no branches yet, which is most of them — never emits one, and the pane
        would open blank next to a highlighted row. Called once after the initial
        layout; :meth:`TreeDetailPane.show` dedupes it against the event that a
        session with a deeper leaf does emit.
        """
        node = self.query_one("#tree-browser-tree", Tree).cursor_node
        await self._show_node(None if node is None else node.data)

    async def _show_node(self, node_id: object) -> None:
        pane = self.query_one(transcript.TreeDetailPane)
        if not pane.display:
            return
        view = None if node_id is None else self._view_of(str(node_id))
        if view is not None:
            await pane.show(view)

    def _apply_detail_pane(self) -> None:
        """Show or hide the pane for the current height and fold state.

        The one place the pane's ``display`` is written, mirroring
        ``TauApp._apply_side_columns``: an inline style set from two places is
        permanent and invisible to the other.

        Two reasons the pane can be absent and they are not the same reason. The
        HEIGHT rule (:attr:`DETAIL_MIN_HEIGHT`) is the layout's — below it the
        pane cannot say what it exists to say, so it gives its rows to the tree
        and the one-row marker would be a worse use of the last of them. The FOLD
        is the reader's, and it gets the marker, because a choice needs a way back.
        """
        pane = self.query_one(transcript.TreeDetailPane)
        marker = self.query_one("#tree-detail-folded", Static)
        tall_enough = self.app.size.height >= self.DETAIL_MIN_HEIGHT
        pane.display = tall_enough and not self._detail_folded
        marker.display = tall_enough and self._detail_folded

    async def action_toggle_detail(self) -> None:
        """``ctrl+D``: fold the detail pane away, or bring it back (§4a).

        The pane takes half the body, and a reader who is following the SHAPE of
        a conversation rather than reading a message wants those rows. Nothing
        but the terminal's height used to be able to hide it.

        Redrawing on the way back rather than on the way out: :meth:`_show_node`
        does nothing while the pane is hidden (there is no audience), so the
        cursor may have moved several rows since the last frame the pane drew.
        :meth:`TreeDetailPane.show` dedupes, so a cursor that did not move costs
        nothing here.
        """
        self._detail_folded = not self._detail_folded
        self._apply_detail_pane()
        if not self._detail_folded:
            await self._show_cursor_node()

    async def on_click(self, event: events.Click) -> None:
        """Double-click the pane to fold it; single-click the marker to unfold.

        ``event.widget is pane`` and not "the pane or anything in it": a click
        inside the pane lands on a :class:`MessageBox`, a tool box or a markdown
        block, and those are things the reader is *reading*. The pane itself is
        reachable only at its border and padding, which is the gesture asked for.

        The marker is one row of text with nothing to read, so one click is
        enough — a fold you have to double-click your way out of is a trap.
        """
        pane = self.query_one(transcript.TreeDetailPane)
        if event.widget is pane and event.chain >= 2:
            await self.action_toggle_detail()
            return
        if event.widget is self.query_one("#tree-detail-folded", Static):
            await self.action_toggle_detail()

    def on_mount(self) -> None:
        """Build the widget tree from :func:`plan_tree_rows`.

        The nesting rules live in that function, which is pure and has its own
        tests; this is the widget build alone. Two rules: a fork opens a level
        (TREE-BROWSER-AS-EDITOR.md §2 — indent counts branches, not messages, so
        it does not grow with the conversation) and a user message opens a level
        that the next user message closes (PLAN-0.9.4 §4, the turn group). Line
        ORDER is a depth-first walk either way, so the rows appear in the sequence
        the log has.

        ``data=node.id`` is deliberately unchanged — the widget nesting is a
        rendering decision and ``parentId`` stays the property of the data, so
        :meth:`_view_of`, :class:`TreeDetailPane` and every caller reading
        ``node.data`` are unaffected. :attr:`_depth_of` (built in :meth:`_index`)
        remains the *data* depth the pane counts with; the depth recorded in
        :attr:`_rows` is the *widget* depth, because that is what :meth:`_relabel`
        needs to know how much indentation a row is paying for. A row the planner
        drops is dropped from the DRAWING only: it keeps its entry, its place in
        :attr:`_by_id` and its ancestry.

        The cost is on the record: Textual highlights the hovered row's ancestry
        through its guide rails, and a flattened run has no rails between siblings.
        §3's ``tree--zone-path`` is the replacement.
        """
        self._apply_detail_pane()
        tree = self.query_one("#tree-browser-tree", Tree)
        if isinstance(tree, ZoneTree):
            tree.set_kinds(
                {
                    node_id: span
                    for node_id, node in self._by_id.items()
                    if (span := tree_kind_span(node)) is not None
                }
            )
        leaf_widget: list[Any] = []
        self._rows = []

        widgets: list[Any] = []
        for row in plan_tree_rows(self._roots):
            label = self._label(row.node)
            parent = tree.root if row.parent is None else widgets[row.parent]
            widget_node = parent.add(
                label,
                data=row.node.id,
                expand=row.expanded,
                allow_expand=row.has_children,
            )
            widgets.append(widget_node)
            self._rows.append((widget_node, label, row.depth, row.has_children))
            if row.node.is_leaf:
                leaf_widget.append(widget_node)

        if leaf_widget:
            leaf_node = leaf_widget[0]
            tree.call_after_refresh(tree.move_cursor, leaf_node)
        tree.call_after_refresh(self._relabel)
        tree.call_after_refresh(self._show_cursor_node)
        tree.call_after_refresh(self._refresh_zones)
        tree.focus()

    def on_resize(self, event: object) -> None:
        self._apply_detail_pane()
        self._relabel()

    def _relabel(self) -> None:
        """Fit every row's label to the tree's current width.

        ``textual.widgets.Tree`` renders one physical line per node and does not
        wrap, so a preview longer than the row is not shortened — it runs off the
        edge and the tree grows a horizontal scrollbar, which is a poor way to
        read a sentence. Eliding puts the truncation where the reader can see it.

        An elided preview is a preview the reader cannot finish, which is what
        :class:`TreeDetailPane` beside these rows is for: the row identifies the
        node, the pane shows it whole and wrapped.
        """
        if not self._rows:
            return
        tree = self.query_one("#tree-browser-tree", Tree)
        width = tree.content_size.width - tree.styles.scrollbar_size_vertical
        if width <= 0:
            return
        for widget_node, label, depth, has_children in self._rows:
            toggle = tree.guide_depth if has_children else 0
            available = width - depth * tree.guide_depth - toggle
            widget_node.set_label(_elide(label, available))

    @staticmethod
    def _label(node: TreeNode) -> str:
        tag = node.role or node.kind
        text = node.preview or f"({node.kind})"
        marker = "  ◀ current" if node.is_leaf else ""
        return f"{tag}: {text}{marker}"

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """A click SELECTS; it does not commit (TREE-BROWSER-AS-EDITOR.md §5.1).

        ``Tree._on_click`` sets ``cursor_line`` and then runs ``select_cursor``
        (textual 8.2.7, _tree.py:1453-1466), which posts this message — the same
        message ``Enter`` used to arrive by. Dismissing here is why a click jumped
        straight out of the browser: the reader could not point at a node to read it
        in the detail pane without leaving. The screen's priority ``enter`` binding
        now owns the commit, so this message can only have come from a click.

        ``move_cursor`` rather than nothing: the click has already moved the cursor,
        but a ``NodeSelected`` raised any other way should still leave the cursor —
        and therefore the detail pane — on the node the reader named.
        """
        event.stop()
        self.query_one("#tree-browser-tree", Tree).move_cursor(event.node)

    def action_commit(self) -> None:
        """``Enter``: dismiss with an intent naming the cursor node (§5.1).

        ``TreeIntent("navigate", (id,))`` is §5.3's degenerate case, and is exactly
        what ``dismiss(id)`` meant before the return type widened (§11.1). One id,
        because the cursor is one node; the marked set is not committed here — no
        gesture consumes it yet, and inventing one would be a producer for an
        operation §6 has not built.

        **Two actions, because pointing at a user message means something else**
        (PLAN-0.9.4 §4, item 2). ``navigate`` continues from BELOW the named node,
        which is right for an assistant or tool row. A user message's below is the
        one place a conversation cannot go — two user turns in a row — and what
        the reader means by pointing at one is "ask this differently", which is a
        fork from that message's PARENT with its text in hand to edit. That is
        ``revise``, and the id it carries is still the node the reader named.

        The id, not the parent: this modal reports what was pointed at and the
        CALLER knows the question (§5.3 / §11.1). The elide flow asks a different
        question of this same browser and reads ``sole_id``, which both actions
        carry — see :meth:`TauApp._elide_span_flow`, which says so rather than
        relying on it.

        No cursor means nothing was named, so there is nothing to answer with and
        the browser stays open. Dismissing with ``None`` here would be indexed as a
        cancel, which is a different thing than "Enter on an empty tree".
        """
        node = self.query_one("#tree-browser-tree", Tree).cursor_node
        if node is None or node.data is None:
            return
        entry_id = str(node.data)
        picked = self._by_id.get(entry_id)
        is_user = picked is not None and picked.kind == "message" and picked.role == "user"
        self.dismiss(TreeIntent("revise" if is_user else "navigate", (entry_id,)))

    def action_collapse(self) -> None:
        """``left``: fold this fork, or step out to the enclosing one (§5.2).

        ``left`` is unbound in ``Tree.BINDINGS`` (textual 8.2.7, _tree.py:524-551 —
        only ``shift+left`` is ``cursor_parent``), so the standard file-tree idiom
        was simply missing. A node with widget children is a fork or a user turn
        (PLAN-0.9.4 §4 added the second), which makes "collapse what the cursor is
        on, else go to what contains it" the gesture for folding away a branch or a
        turn rather than for hiding one message.

        The widget root is skipped: ``show_root`` is ``False``, so it occupies no
        line and ``move_cursor`` onto it would clear the cursor rather than move it.
        """
        tree = self.query_one("#tree-browser-tree", Tree)
        node = tree.cursor_node
        if node is None:
            return
        if node.children and node.is_expanded:
            node.collapse()
            return
        parent = node.parent
        if parent is not None and parent is not tree.root:
            tree.move_cursor(parent)

    def action_expand(self) -> None:
        """``right``: unfold the fork or turn the cursor is on.

        The counterpart :meth:`action_collapse` needs, and the gesture that opens a
        turn group, which is how most rows now arrive (PLAN-0.9.4 §4: a group
        mounts collapsed unless the cursor is inside it). ``space`` used to be
        ``Tree``'s expand/collapse toggle and now marks (see :attr:`BINDINGS`), so
        without this a collapsed row could not be reopened from the keyboard at
        all. A row with no widget children has nothing to unfold; moving the cursor
        into the subtree on ``right`` is deliberately NOT done — ``down`` already
        goes there, and the pair here is about folding.
        """
        node = self.query_one("#tree-browser-tree", Tree).cursor_node
        if node is not None and node.children and not node.is_expanded:
            node.expand()

    def action_toggle_mark(self) -> None:
        """``space``: add or remove the cursor node from the marked set (§5.3 set 2).

        The key is this implementation's choice; §5.3 names the set and binds
        nothing to it. Marks are in-memory and per-open: §11.1 keeps the modal free
        of a ``SessionLog``, so nothing here is durable and closing the browser
        forgets them.

        **A mark takes its tool group with it**
        (:func:`~tau_agent_core.tree_surgery.tool_group`). An assistant message that
        made tool calls and the results answering them are one unit to every
        provider, so marking either end marks both, and unmarking either unmarks
        both. The alternative — let the reader build the half-selection and refuse it
        at the commit — teaches the rule by rejection, one attempt at a time; this
        way the group lights up on the rows and the reader can see what a branch
        would have to carry.
        """
        node = self.query_one("#tree-browser-tree", Tree).cursor_node
        if node is None or node.data is None:
            return
        entry_id = str(node.data)
        group = tool_group(self._tree, entry_id)
        if entry_id in self._marked:
            self._marked -= group
        else:
            self._marked |= group
        self._elide_line = self._line_through(self._elide_other_end())
        self._refresh_zones()

    # -- branch, copy and paste (TREE-BROWSER-AS-EDITOR.md §6, §7) -------------

    def action_branch(self) -> None:
        """``ctrl+B``: build a branch out of the marked messages.

        Dismisses with every marked id, in row order. The caller asks which of the
        two attach modes the reader wants and performs the commit
        (:meth:`TauApp._branch_flow`); this screen still writes nothing (§11.1).

        Refused here, with the reason, while the tree is still on screen — the same
        rule ``ctrl+E`` follows. The refusals are
        :func:`~tau_agent_core.tree_surgery.branch_refusal_reason`'s, computed
        against the tree this browser was built from, so an offer made here is one
        the backend accepts.
        """
        if not self._marked:
            self.app.notify(
                "Nothing marked. Space marks the row under the cursor; ^B branches "
                "from every marked message.",
                severity="warning",
            )
            return
        refusal = branch_refusal_reason(self._tree, self._marked, drop_context=False)
        if refusal is not None:
            self.app.notify(f"Cannot branch from this selection: {refusal}", severity="warning")
            return
        self.dismiss(TreeIntent("branch", selection_order(self._tree, self._marked)))

    def action_copy(self) -> None:
        """``c``: copy the subtree rooted at the cursor node.

        Nothing is written and the browser stays open: a copy is a note about which
        node ``v`` will re-create, held in :attr:`_copied` until the browser closes.
        The copied rows are painted with ``tree--zone-copied``, which until now was a
        declared class with no producer.
        """
        node = self.query_one("#tree-browser-tree", Tree).cursor_node
        if node is None or node.data is None:
            return
        entry_id = str(node.data)
        kind = str(self._resolve_entry(entry_id).get("type", ""))
        if kind not in COPYABLE_KINDS:
            self.app.notify(
                f"A {kind!r} entry cannot be copied — it is structure, not a message.",
                severity="warning",
            )
            return
        self._copied = entry_id
        self._refresh_zones()

    def action_paste(self) -> None:
        """``v``: re-create the copied subtree under the cursor node.

        Dismisses with ``(copied, target)``; the caller performs the mint and
        re-opens this browser so the copy can be seen and navigated onto. The paste
        does NOT move the cursor — it edits the tree, and what the model sees changes
        only when the reader chooses a node with ``Enter``.
        """
        node = self.query_one("#tree-browser-tree", Tree).cursor_node
        target = None if node is None or node.data is None else str(node.data)
        if self._copied is None:
            self.app.notify(
                "Nothing copied. `c` copies the subtree under the cursor, `v` pastes it.",
                severity="warning",
            )
            return
        if target is None:
            self.app.notify("Put the cursor on the node to paste under.", severity="warning")
            return
        try:
            plan = plan_paste(self._tree, self._copied, target)
            refusal = paste_refusal_reason(self._tree, plan)
        except ValueError as exc:
            self.app.notify(str(exc), severity="warning")
            return
        if refusal is not None:
            self.app.notify(f"Cannot paste here: {refusal}", severity="warning")
            return
        self.dismiss(TreeIntent("paste", (self._copied, target)))

    def action_cancel(self) -> None:
        self.dismiss(None)

    # -- the elide, from inside the browser (PLAN-0.9.4 §4) -------------------

    def _elide_other_end(self) -> Optional[str]:
        """The node the cursor is being paired WITH, or ``None`` if there isn't one.

        Exactly one mark is the pairing the reader asked for. **No** mark still
        elides — against the current leaf, which is the ordinary case ("fold the
        history behind where I am and keep going") and the one that would
        otherwise cost a mark to say. More than one mark is refused: an elide has
        two ends and a set of three does not name them.
        """
        if len(self._marked) == 1:
            return next(iter(self._marked))
        if self._marked:
            return None
        return self._leaf_id()

    def _leaf_id(self) -> Optional[str]:
        """The ``◀ current`` node — the log's cursor as the browser drew it."""
        for node in self._by_id.values():
            if node.is_leaf:
                return node.id
        return None

    def _line_through(self, node_id: Optional[str]) -> frozenset[str]:
        """Every node on a root→leaf line through ``node_id``: its ancestry and its
        descendants.

        This is exactly the set an elide's other end can come from, because
        ``elide_span`` requires the resume point to be on the anchor's path — the
        fold's forward scan only ever walks ancestors, and a boundary it cannot
        reach would empty the context in silence (``TauBackend.elide_span``).

        Both directions, because either node can turn out to be the anchor: the
        deeper of the two always is (see :class:`ElidePlan`).
        """
        if node_id is None:
            return frozenset()
        line = {node_id}
        walk = self._parent_of.get(node_id)
        while walk is not None:
            line.add(walk)
            walk = self._parent_of.get(walk)
        stack = list(self._by_id[node_id].children)
        while stack:
            node = stack.pop()
            line.add(node.id)
            stack.extend(node.children)
        return frozenset(line)

    def _copied_zone(self) -> frozenset[str]:
        """The copied node and its descendants — what ``v`` would re-create.

        The whole subtree, not the one row, because that is what a paste mints
        (:func:`~tau_agent_core.tree_surgery.plan_paste`) and the reader should be
        able to see the size of what they are about to duplicate before they press
        the key.
        """
        if self._copied is None:
            return frozenset()
        zone = {self._copied}
        stack = list(self._by_id[self._copied].children)
        while stack:
            node = stack.pop()
            zone.add(node.id)
            stack.extend(node.children)
        return frozenset(zone)

    def _elide_ineligible(self) -> frozenset[str]:
        """Rows to grey out: everything off the line, while a span is being chosen.

        Only when exactly ONE node is marked. With none, the reader is browsing
        and the elide is merely available; with several, nothing has been named
        and greying to a set of three would be a guess.

        This is the ANCESTRY rule alone. The other way an elide can be illegal —
        a legal pair whose span happens to be empty — is one row, and it is
        refused by name when ``ctrl+E`` is pressed rather than greyed here,
        because computing it for every row means one context walk per row.
        """
        if len(self._marked) != 1:
            return frozenset()
        return frozenset(node_id for node_id in self._by_id if node_id not in self._elide_line)

    def _elide_plan(self, cursor: Optional[str]) -> "ElidePlan | None":
        """The elide ``cursor`` and the other end would make, or ``None``.

        Every rejection ``TauBackend.elide_span`` performs is performed here
        first, on the same rules and against the same tree, so the help line can
        only offer an elide the backend will accept. That is the point of doing
        it here: the reported problem was learning the pick was illegal by
        landing back in the conversation with an error.

        **The refusal is measured at the anchor and the COST at the cursor**, and
        those are two different sets — see :class:`ElidePlan`. Measuring both at
        the anchor is the defect this fixed: over ``[1..6]`` with the cursor at 6,
        pairing 2 with 4 was offered as "elide 1 message" when three leave, because
        ``context_entries(anchor)`` cannot see the two the cursor move abandons.
        Measuring both at the cursor would be wrong the other way — it would offer
        an elide whose fold hides nothing, which the backend then refuses.
        """
        other = self._elide_other_end()
        if cursor is None or other is None or cursor == other:
            return None
        if self._depth_of[cursor] > self._depth_of[other]:
            anchor, first_kept = cursor, other
        else:
            anchor, first_kept = other, cursor
        path_ids = [entry["id"] for entry in self._tree.path(anchor)]
        if first_kept not in path_ids:
            return None
        kept = set(path_ids[path_ids.index(first_kept) :])
        folded = [e for e in self._tree.context_entries(anchor) if e["id"] not in kept]
        if not folded:
            return None
        dropped = [e for e in self._tree.context_entries() if e["id"] not in kept]
        return ElidePlan(
            anchor=anchor,
            first_kept=first_kept,
            folded=len(folded),
            dropped=len(dropped),
            moves_cursor=anchor != self._tree.cursor,
        )

    def action_elide(self) -> None:
        """``ctrl+E``: fold the span between the cursor and the other end.

        Dismisses with the pair; the caller performs it. This screen still holds
        no ``SessionLog`` and still writes nothing (§11.1).

        An illegal pick is refused HERE, with the reason, and the browser stays
        open — which is the whole change. It used to be discovered one modal
        later, after the browser had closed, as an error notification over a
        conversation the reader could no longer see the shape of.
        """
        node = self.query_one("#tree-browser-tree", Tree).cursor_node
        cursor = None if node is None or node.data is None else str(node.data)
        plan = self._elide_plan(cursor)
        if plan is not None:
            self.dismiss(TreeIntent("elide", (plan.anchor, plan.first_kept)))
            return
        self.app.notify(self._elide_refusal(cursor), severity="warning")

    def _elide_refusal(self, cursor: Optional[str]) -> str:
        """Why the elide the reader just asked for is not one. One sentence each.

        Ordered from "nothing was named" to "this pair is legal but empty", which
        is the order the reader hits them in.
        """
        if cursor is None:
            return "Put the cursor on a node first."
        if len(self._marked) > 1:
            return (
                f"{len(self._marked)} nodes are marked. An elide has two ends — "
                "mark one node, and put the cursor on the other."
            )
        other = self._elide_other_end()
        if other is None:
            return "There is no current node to fold back to."
        if cursor == other:
            return "That is both ends of the elide. Move the cursor, or mark another node."
        if cursor not in self._elide_line:
            return (
                "Those two nodes are on different branches. An elide folds a span "
                "of ONE line of the conversation, so the two ends have to be on it."
            )
        return "That would hide nothing — the span between those two nodes is already empty."

    # -- the four selection sets (§5.3) --------------------------------------

    def _refresh_zones(self) -> None:
        """Recompute all four sets and hand them to the renderer.

        One entry point, called from every gesture that can move a set: the cursor
        (``NodeHighlighted``), a mark (``space``), a fold (``NodeCollapsed`` /
        ``NodeExpanded``) and the deferred initial layout. Recomputing all four
        rather than patching the one that moved — ``path``/``folded``/``covered``
        are all functions of the cursor, and a partial update is how two of them
        end up describing different cursors.

        This is the EXPENSIVE path — two walks of the conversation plus one of every
        widget row — and it is deliberately not what a hover runs. See
        :meth:`ZoneTree.set_hover_zones`.
        """
        if not self._rows:
            return
        tree = self.query_one("#tree-browser-tree", ZoneTree)
        node = tree.cursor_node
        cursor = None if node is None or node.data is None else str(node.data)
        path, folded, covered = self._derived(cursor)
        hover_common, hover_divergent = self._hover_divergence(cursor, self._hovered)
        tree.set_zones(
            TreeZones(
                cursor=cursor,
                marked=frozenset(self._marked),
                path=path,
                folded=folded,
                covered=covered,
                hidden=self._hidden(),
                copied=self._copied_zone(),
                summary=self._summary_zone,
                abandoned=self._abandoned_zone,
                ineligible=self._elide_ineligible(),
                hover_common=hover_common,
                hover_divergent=hover_divergent,
            )
        )
        self.query_one("#tree-browser-marks", Static).update(self._marks_summary(cursor))

    def _derived(
        self, cursor: Optional[str]
    ) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
        """§5.3's set 3, as the three roles §3's class table distinguishes.

        ``path`` is the cursor's raw ancestor chain (``ConversationTree.path``,
        every kind, no splice). ``folded`` is what the fold at the cursor drops from
        that chain — the difference between the raw walk and ``context_entries``,
        which is exactly "on the path, dropped by a splice anchor".

        ``covered`` is the same span, reported separately when the cursor IS the
        anchor doing the dropping. Which anchor that is comes out of
        ``context_entries`` rather than out of a kind test:
        ``_active_path_entries`` emits ``[anchor] + kept + after``
        (``conversation_tree.py:383-391``), so element 0 of a folded result is the
        anchor by construction. Reading it there instead of re-testing
        ``entry["type"] in ("compaction", "elide")`` keeps the anchor vocabulary in
        the one module that owns it — ``_SPLICE_ANCHOR_KINDS`` is private to
        ``conversation_tree`` and a second copy in the TUI would be a fifth kind
        away from being wrong.

        ``folded`` deliberately still contains ``covered``: the two are the same
        rows seen from two positions, and :attr:`ZoneTree._LABEL_ZONES` resolves
        which class wins rather than the sets pre-subtracting each other.
        """
        empty: frozenset[str] = frozenset()
        if cursor is None:
            return empty, empty, empty
        path_ids = frozenset(entry["id"] for entry in self._tree.path(cursor))
        kept = self._tree.context_entries(cursor)
        kept_ids = {entry["id"] for entry in kept}
        folded = frozenset(entry_id for entry_id in path_ids if entry_id not in kept_ids)
        anchor = kept[0]["id"] if kept else None
        covered = folded if (folded and anchor == cursor) else empty
        return path_ids, folded, covered

    def _branch_summary_pairs(self) -> tuple[frozenset[str], frozenset[str]]:
        """§4.3's two-row relation: each ``branch_summary`` and what it summarizes.

        §1.2 established that no structural change is needed —
        ``SessionStore.append_branch_summary`` (``session_store.py:665``) moves the
        leaf to ``from_id`` *before* appending, mirroring pi's ``branchWithSummary``
        (``session-manager.ts:1272``), so ``parentId == fromId`` and the summary is
        already a sibling of the abandoned branch's first message. What was missing
        is that a reader cannot see it. §4.3 was attempted inside ``_preview_of``
        and correctly bounced there: that renders one line for one node, and this is
        a relation BETWEEN two rows. So it is zone work (§3).

        **Which sibling.** The immediately PRECEDING one, in the order the browser
        already draws them (``ConversationTree.tree`` sorts children by timestamp;
        roots keep load order, which for an append-only log is the same order). Not
        "every sibling that is not the summary": a branch point can be abandoned
        more than once, and ``b1, S1, b2, S2`` then pairs correctly — ``S1`` looks
        back at ``b1``, ``S2`` at ``b2`` — where a set-difference rule would blame
        ``S2`` for ``b1`` as well. It is also the phrase §4.3 uses: *the* abandoned
        branch's first message, singular.

        A ``branch_summary`` with no earlier sibling is left out of both sets rather
        than paired with something. That shape means a branch was summarized before
        it existed; painting half a pair would state a relation that is not there.

        Computed on the ``TreeNode`` graph, so it needs no payload lookup — ``kind``
        is on the node (``conversation_tree.py:173``) and the ``fromId`` the payload
        carries would only re-state the ``parentId`` the graph is already built
        from.
        """
        summary: set[str] = set()
        abandoned: set[str] = set()
        stack: list[list[TreeNode]] = [self._roots]
        while stack:
            siblings = stack.pop()
            for position, node in enumerate(siblings):
                if node.children:
                    stack.append(node.children)
                if node.kind != "branch_summary" or position == 0:
                    continue
                summary.add(node.id)
                abandoned.add(siblings[position - 1].id)
        return frozenset(summary), frozenset(abandoned)

    def on_zone_tree_hover_changed(self, event: "ZoneTree.HoverChanged") -> None:
        """Repaint the divergence for the row the pointer moved onto (§3, step 5).

        §2 removed the guide-rail ancestry highlight — after flattening, a
        30-message run is 30 siblings at one level with no rails between them — and
        §3 is "the replacement, not an embellishment". ``tree--zone-path`` replaced
        the CURSOR's ancestry in step 3; this replaces the HOVER's, and says
        something the rails never did: not just "these are the hovered row's
        ancestors" but *where that ancestry stops agreeing with where you are*.

        Nothing here recomputes a selection set. The two ancestry walks in
        :meth:`_hover_divergence` are the whole per-hover cost.
        """
        event.stop()
        self._hovered = event.entry_id
        tree = event.zone_tree
        node = tree.cursor_node
        cursor = None if node is None or node.data is None else str(node.data)
        tree.set_hover_zones(*self._hover_divergence(cursor, self._hovered))

    def _hover_divergence(
        self, cursor: Optional[str], hovered: Optional[str]
    ) -> tuple[frozenset[str], frozenset[str]]:
        """Split ``hovered``'s ancestry where it leaves ``cursor``'s (§3, step 5).

        The common prefix and the divergent tail are different facts and the
        stylesheet reads them differently: the prefix is context the two nodes
        share, the tail is what you would be picking up if you went there.

        **A node ON the cursor's path reports no divergence.** Its chain is then a
        PREFIX of the cursor's, the tail is empty by construction, and this returns
        two empty sets rather than a prefix with nothing to contrast against —
        painting the shared half alone would show a highlight that means "you are
        already here", which reads as a divergence that is not there. Hovering the
        cursor itself, or leaving the tree entirely, lands in the same case.

        A DESCENDANT of the cursor is not on the cursor's path and does diverge:
        the rows below the cursor are exactly what the cursor's context does not
        contain, which is the question this answers.

        ``k == 0`` — two different roots, which ``ConversationTree.tree`` really can
        produce from an orphaned entry — gives an empty prefix and the hovered
        chain entire. That is the honest answer, and it is the same answer
        :meth:`_lowest_common_ancestor` gives for the same shape.

        Cost is O(depth), twice: :meth:`_ancestry` is a ``_parent_of`` walk, and the
        prefix scan is a ``zip``. No conversation walk, no row walk — which is the
        constraint, because this runs on every row the pointer crosses.
        """
        empty: frozenset[str] = frozenset()
        if cursor is None or hovered is None:
            return empty, empty
        hover_chain = self._ancestry(hovered)
        cursor_chain = self._ancestry(cursor)
        shared = 0
        for mine, theirs in zip(hover_chain, cursor_chain):
            if mine != theirs:
                break
            shared += 1
        if shared == len(hover_chain):
            # `hovered` is the cursor, or one of its ancestors. Nothing diverges.
            return empty, empty
        return frozenset(hover_chain[:shared]), frozenset(hover_chain[shared:])

    def _hidden(self) -> frozenset[str]:
        """§5.3's set 4: rows the reader has folded out of sight.

        View state, per §11.2 — computed from the widget tree, never read from the
        log, and nothing is appended for it. The *archived* half of §11.2's decision
        has no gesture yet, so this is the collapsed half alone; a row in it is not
        currently drawn, which is why ``tree--zone-hidden`` has no visible effect
        today and why :meth:`_marks_summary` is where the set is observable at all.

        The walk stops one short of the widget root. ``Tree.__init__`` builds its
        root with ``expand=False`` (textual 8.2.7, ``_tree.py:783`` →
        ``_add_node``'s default) and ``_build`` adds the root's children anyway
        when ``show_root`` is ``False`` (``_tree.py:1272-1275``) — so the root's
        collapsed flag means nothing here, and counting it would report every
        top-level row as folded away.
        """
        hidden: set[str] = set()
        for widget_node, _label, _depth, _has_children in self._rows:
            walk = widget_node.parent
            while walk is not None and walk.parent is not None:
                if not walk.is_expanded:
                    hidden.add(str(widget_node.data))
                    break
                walk = walk.parent
        return frozenset(hidden)

    def _lowest_common_ancestor(self, ids: set[str]) -> Optional[str]:
        """The deepest node every id in ``ids`` descends from, itself included.

        §5.3: the ``_parent_of`` map :meth:`_index` already builds "gives the lowest
        common ancestor for free". Root→node chains, then the longest common prefix.

        ``None`` when the marked nodes are in different roots — an orphaned entry
        (broken parent chain) is its own root in ``ConversationTree.tree``, so two
        marks really can have no ancestor in common, and saying so beats naming an
        arbitrary one.
        """
        if not ids:
            return None
        chains = [self._ancestry(entry_id) for entry_id in sorted(ids)]
        common: Optional[str] = None
        for step in zip(*chains):
            if len(set(step)) != 1:
                break
            common = step[0]
        return common

    def _ancestry(self, entry_id: str) -> list[str]:
        """``entry_id``'s root→self chain through :attr:`_parent_of`."""
        chain = [entry_id]
        walk = self._parent_of.get(entry_id)
        seen = {entry_id}
        while walk is not None and walk not in seen:
            chain.append(walk)
            seen.add(walk)
            walk = self._parent_of.get(walk)
        chain.reverse()
        return chain

    def _marks_summary(self, cursor: Optional[str] = None) -> str:
        """The marked set's count, its lowest common ancestor and its size (§5.3).

        **The size is a labelled estimate, and says so.** ``compaction.estimate_tokens``
        is a ~4-chars-per-token heuristic over the payload; the only measured number
        in a session is ``usage.input_tokens`` on an assistant message
        (``agent_loop.py:819``), which is a measurement of one request rather than of
        an arbitrary set of entries. §5.3: "a row may state a measured
        ``input_tokens``; a selection total may only state an estimate, and must say
        so". So this prints ``~N tokens (estimate)`` and never a bare number.

        **The offer goes FIRST when there is one.** This line is one row
        (``#tree-browser-marks`` is ``height: 1``), so it clips rather than
        wrapping — and the readout plus the offer runs to about 96 columns, which
        on an 80-column terminal means whatever is last is what disappears. The
        offer is the only part of the line that is an action.

        **At most ONE offer, and which one is a priority, not a merge.** Three
        gestures can apply to the same row now (§6, §7) and two of their offers do
        not fit on one row together, so they are ordered by how specific the state
        that produced them is: a pending paste (the reader is mid-gesture and the
        clipboard is holding something) beats a legal elide (which needs a legal pair
        of ends) beats a branch (which needs only a mark). The narrow collision is
        one mark with a legal elide, where the branch offer is not shown and the help
        line is what names ``^B``.
        """
        offer = self._paste_offer(cursor) or self._elide_offer(cursor) or self._branch_offer()
        if not self._marked:
            base = "nothing marked · space marks the row under the cursor"
            return f"{offer} · {base}" if offer else base
        count = len(self._marked)
        noun = "node" if count == 1 else "nodes"
        folded_away = len(self._marked & self._hidden())
        out_of_sight = f" ({folded_away} folded away)" if folded_away else ""
        ancestor = self._lowest_common_ancestor(self._marked)
        where = f"common ancestor {ancestor}" if ancestor is not None else "no common ancestor"
        line = (
            f"{count} {noun} marked{out_of_sight} · {where} · "
            f"~{self._estimated_tokens(self._marked)} tokens (estimate)"
        )
        return f"{offer} · {line}" if offer else line

    def _branch_offer(self) -> str:
        """``^B: branch from 3 marked messages`` — or ``""`` when nothing is marked.

        Same rule as :meth:`_elide_offer`: the offer appears exactly when pressing
        the key would do something. It states the COUNT rather than the shape of the
        branch, because the shape (which marks are kept in place and which are
        copied) is worked out by
        :func:`~tau_agent_core.tree_surgery.plan_branch` and is not something the
        reader has to decide or predict — what they chose is the set.
        """
        if not self._marked:
            return ""
        noun = "message" if len(self._marked) == 1 else "messages"
        return f"^B: branch from {len(self._marked)} marked {noun}"

    def _paste_offer(self, cursor: Optional[str]) -> str:
        """``v: paste 4 entries under this node`` — or ``""``.

        Shown while the clipboard holds something and the cursor is somewhere the
        copy can legally land. "Legally" here is only the cheap half of the check —
        that the target is outside the copied subtree, which is the refusal a reader
        walks into by moving one row. The tool-result check
        (:func:`~tau_agent_core.tree_surgery.paste_refusal_reason`) needs the whole
        plan and is run on the key press, where paying for it once is right; running
        it per cursor move would cost a subtree walk per arrow key to hide an offer
        that is almost always legal.
        """
        if self._copied is None or cursor is None:
            return ""
        zone = self._copied_zone()
        if cursor in zone:
            return ""
        noun = "entry" if len(zone) == 1 else "entries"
        return f"v: paste {len(zone)} copied {noun} under this node"

    def _elide_offer(self, cursor: Optional[str]) -> str:
        """``ctrl+E: keep this span, drop the other 14`` — or ``""``.

        The empty string is the feature, not a fallback: the offer appears exactly
        when pressing the key would do something, so a reader who has never used
        the gesture meets it on the row where it applies rather than in a list of
        keys they have to test one at a time. This is what was asked for in place
        of the mode-chooser's ``Elide a span ending here…`` button, which named the
        operation on every node whether or not it could be performed on that one.

        **"keep this span" is load-bearing wording.** It read ``elide N messages``,
        which every reader parses as "remove the N between these two rows" — the
        opposite of what an elide does (:class:`ElidePlan`). The frame has to be
        stated where the gesture is, not only in the manual, because the manual is
        not open at the moment somebody presses the key.

        ``and move back to it`` is appended when the anchor is not the current tip,
        because that is a second thing happening: the conversation resumes
        somewhere else, and the entries newer than the anchor are part of the
        ``dropped`` count precisely for that reason.

        ``cursor`` is passed in rather than read off the tree: :meth:`compose`
        writes the first version of this line, and the tree it would query does
        not exist yet at that point. ``None`` there is honest — no row is under
        the cursor until one is drawn.
        """
        plan = self._elide_plan(cursor)
        if plan is None:
            return ""
        noun = "entry" if plan.dropped == 1 else "entries"
        move = ", and move back to it" if plan.moves_cursor else ""
        return f"ctrl+E: keep this span, drop the other {plan.dropped} {noun}{move}"

    def _estimated_tokens(self, ids: set[str]) -> int:
        """A character-based token estimate over the entries ``ids`` names.

        ``compaction.estimate_tokens`` for the entries that carry a message, and the
        same arithmetic over the summary text for the kinds that carry one instead
        (``compaction``, ``branch_summary`` — what ``context_for`` injects for them
        is a message built from that string, ``conversation_tree.py:78-96``). Kinds
        with neither, such as ``navigate`` and ``elide``, contribute nothing to a
        context and so contribute nothing here.

        Never presented without the word "estimate" beside it — see
        :meth:`_marks_summary`.
        """
        from tau_agent_core.compaction import estimate_tokens

        total = 0
        for entry_id in ids:
            entry = self._resolve_entry(entry_id)
            message = entry.get("message")
            if isinstance(message, dict):
                total += estimate_tokens(message)
                continue
            summary = entry.get("summary")
            if isinstance(summary, str):
                total += estimate_tokens({"role": "user", "content": summary})
        return total


class TreeModeModal(ChoiceDialog[Optional[str]]):
    """The mode chooser after a node is picked (§3.1).

    pi's ``showExtensionSelector`` (interactive-mode.ts:4479-4483): "No summary" /
    "Summarize" / "Summarize with custom instructions". Dismisses with
    ``"navigate"`` / ``"summarize"`` / ``"custom"`` (or ``None`` on cancel).

    **``elide`` used to be a fourth button here and is not one any more**
    (PLAN-0.9.4 §4). It never fitted: the other three treat the picked node as a
    BRANCH POINT and move the cursor back to it, while an elide treats it as the
    fold's ANCHOR and needs a second node before it can do anything — so choosing
    it re-opened the whole tree browser to ask for that second node, and an
    illegal pick was reported after both screens had closed. It is a key in the
    browser now (``ctrl+E``, :meth:`SessionTreeModal.action_elide`), which is the
    one place both nodes are visible at once and the only place a refusal can be
    stated while the reader can still see what they picked.
    """

    DIALOG_ID = "tree-mode-dialog"
    BUTTONS_ID = "tree-mode-buttons"
    TITLE_TEXT = "Act on selected node"
    CHOICES = (
        ("Branch: no summary", "mode-navigate", "navigate"),
        ("Branch: summarize abandoned branch", "mode-summarize", "summarize"),
        ("Branch: summarize with custom instructions…", "mode-custom", "custom"),
        ("Cancel", "mode-cancel", None),
    )


class BranchModeModal(ChoiceDialog[Optional[str]]):
    """How much context the branch from the marked messages keeps (§6).

    Two modes, and the difference is one ``elide``:

    * ``"keep"`` — the branch hangs off the deepest mark that is already on the
      path, and everything above that stays in context. This is "carefully choose
      what is added to what is already here".
    * ``"only"`` — the same branch, followed by an elide resuming at the root-most
      mark, so the context becomes the system prompt plus the marked messages and
      nothing else. This is "start again from these, and only these".

    Named as a separate screen rather than a fourth button on :class:`TreeModeModal`
    for the reason that took the elide off that screen: the three buttons there
    treat one node as a branch point and choose what to do about the branch being
    abandoned, while this chooses what a *new* branch inherits. Same shape, opposite
    question.
    """

    DIALOG_ID = "tree-mode-dialog"
    BUTTONS_ID = "tree-mode-buttons"
    TITLE_TEXT = "Branch from the marked messages"
    CHOICES = (
        ("Keep the context above them", "branch-keep", "keep"),
        ("Keep only the system prompt", "branch-only", "only"),
        ("Cancel", "branch-cancel", None),
    )


class TreeCustomInstructionsModal(TextDialog):
    """Collect the custom summarizer instructions for mode 3 (§3.1).

    pi's ``showExtensionEditor`` (interactive-mode.ts:4494). Reuses the shared
    :class:`~tau_coding_agent.dialogs.TextDialog` shell; Summarize dismisses with the
    text, Cancel with ``None``.
    """

    TITLE_TEXT = "Custom Summary Instructions"
    SUBMIT_LABEL = "Summarize"
    SUBMIT_ID = "custom-save"
    CANCEL_ID = "custom-cancel"
