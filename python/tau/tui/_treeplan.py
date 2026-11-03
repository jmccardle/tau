"""The tree plan (D23 section 6): what the tree screen draws, as data, built a level at a time.

The viewed cursor's walk is the **spine**: drawn straight down, one row per node, with each turn
(a `User` node and what follows it down the line) folded into one row. At a node with more than one
child the line goes on through one of them and every other child is a **stub**: one folded row for
the whole branch, with its node and turn counts and the cursors on it. A stub unfolds to its own
line, drawn the same way: from the stub's first node it goes on through the child toward the
lowest-numbered open cursor below it, else through the child with the longest line below it
(the lowest id on a tie); the other children are stubs again.

A branch at the last node of a turn hangs level with the turn, so folding the turn does not hide
it; a branch in the middle of a turn hangs inside it.

Pure and Textual-free, so it is tested without a terminal. Planning is one pass over the nodes
(sizes, heights and the cursors below each node); a row's children are made only when asked for, so
a session of tens of thousands of nodes costs rows for what is unfolded, not for what exists.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from tau._core import Body, Node, Outcome, Part

RowKind = Literal["node", "turn", "stub"]
Key = tuple[RowKind, int]
"""A row's identity across plans: its kind and the id of its first node."""

_Cover = dict[int, int]  # node id -> index of the row of a level that shows it


def estimated_chars(node: Node) -> int:
    """The characters of a node the model reads (D24's estimate): text, thinking, call arguments
    and outcomes. `Config` and `Custom` nodes never reach the model and count nothing."""
    match node.body:
        case Body.System(text=text) | Body.User(text=text):
            return len(text)
        case Body.Assistant(parts=parts):
            return sum(_part_chars(p) for p in parts)
        case Body.Results(outcomes=outcomes):
            return sum(_outcome_chars(o) for _call, o, _by in outcomes)
        case Body.Config() | Body.Custom():
            return 0
        case _:
            raise TypeError(f"node {node.id} holds a body the tree does not know")


def _part_chars(part: Part) -> int:
    match part:
        case Part.Text(text=text) | Part.Thinking(text=text):
            return len(text)
        case Part.Call(args_raw=args):
            return len(args)
        case _:
            raise TypeError(f"a reply part the tree does not know: {part!r}")


def _outcome_chars(outcome: Outcome) -> int:
    match outcome:
        case Outcome.Ok(value=value):
            return len(value)
        case Outcome.Err(error=error):
            return len(error)
        case Outcome.Interrupted():
            return 0
        case _:
            raise TypeError(f"an outcome the tree does not know: {outcome!r}")


class WalkLength:
    """The length of the walk to any node, in nodes and approximate tokens (the characters the
    model would read, divided by 4). Nodes never change, so what is counted is kept: `extend`
    with the nodes that arrived, then ask."""

    def __init__(self) -> None:
        self._nodes: list[int] = []
        self._chars: list[int] = []

    def extend(self, nodes: Sequence[Node]) -> None:
        """Count the nodes in `nodes` not counted yet (ids are dense, a parent first)."""
        for node in nodes[len(self._nodes) :]:
            if node.parent is None:
                self._nodes.append(1)
                self._chars.append(estimated_chars(node))
            else:
                self._nodes.append(self._nodes[node.parent] + 1)
                self._chars.append(self._chars[node.parent] + estimated_chars(node))

    def of(self, node_id: int) -> tuple[int, int]:
        """The walk to `node_id`: its length in nodes and its tokens."""
        return self._nodes[node_id], self._chars[node_id] // 4


@dataclass(frozen=True)
class TreeRow:
    """One row to draw. A `node` row is a node that opens nothing; a `turn` row is a `User` node,
    which opens the rest of its turn; a `stub` row is a folded branch whose first node is `node`.
    `opens` is whether it starts unfolded. A turn's `size` counts the nodes after its `User` node,
    a stub's all the nodes and `turns` the `User` nodes of the branch; `calls` and `branches` are
    a turn's tool calls and the stubs hung inside it. `cursors` are the open cursors on a stub's
    branch, or at or inside a turn, and `closed` the closed ones. What a turn holds is not part of
    its identity (`tail`, its last node, stands for it)."""

    kind: RowKind
    node: Node = field(compare=False)
    opens: bool = False
    expandable: bool = False
    size: int = 0
    turns: int = 0
    calls: int = 0
    branches: int = 0
    cursors: tuple[int, ...] = ()
    closed: tuple[int, ...] = ()
    tail: int = 0
    members: tuple[int, ...] = field(default=(), compare=False, repr=False)
    hung: tuple[tuple[int, int], ...] = field(default=(), compare=False, repr=False)

    @property
    def key(self) -> Key:
        """The row's identity."""
        return (self.kind, self.node.id)


_BULK = 64
"""More new nodes than this and the subtree counts are made again in one pass, not by walking up
from each."""


class TreeShape:
    """What nodes say that never changes, kept between plans: each node's parent, whether it is a
    `User` node, its tool calls and its children; how big, how many turns and how tall the branch
    below it is; and the turn rows already made that nothing marks. `extend` takes the nodes that
    arrived (ids are dense, a parent first). `ValueError` for nodes that are not."""

    def __init__(self) -> None:
        self.nodes: list[Node] = []
        self.parent: list[int] = []  # -1 for a root
        self.user: list[bool] = []
        self.calls: list[int] = []
        self.kids: list[list[int]] = []
        self.size: list[int] = []
        self.turns: list[int] = []
        self.height: list[int] = []
        self.roots: list[int] = []
        self.turn_rows: dict[tuple[int, int], TreeRow] = {}
        """Turn rows by their first and last node, for those with no cursor or branch in them."""

    def extend(self, nodes: Sequence[Node]) -> None:
        """Take in the nodes of `nodes` not taken in yet."""
        start = len(self.nodes)
        new = nodes[start:]
        for i, node in enumerate(new, start):
            if node.id != i or (node.parent is not None and not 0 <= node.parent < i):
                raise ValueError(f"node {node.id} is not at place {i} after its parent")
            parent = -1 if node.parent is None else node.parent
            body = node.body
            self.nodes.append(node)
            self.parent.append(parent)
            self.user.append(isinstance(body, Body.User))
            self.calls.append(
                sum(isinstance(p, Part.Call) for p in body.parts)
                if isinstance(body, Body.Assistant)
                else 0
            )
            self.kids.append([])
            if parent == -1:
                self.roots.append(i)
            else:
                self.kids[parent].append(i)
        if len(new) > _BULK:
            self._count_all()
        else:
            for i in range(start, len(self.nodes)):
                self._count_up(i)
        if self.nodes and len(self.roots) != 1:
            raise ValueError(f"the tree has {len(self.roots)} roots")

    def _count_all(self) -> None:
        count = len(self.nodes)
        self.size = [1] * count
        self.turns = [int(u) for u in self.user]
        self.height = [0] * count
        for i in range(count - 1, 0, -1):
            if (p := self.parent[i]) != -1:
                self.size[p] += self.size[i]
                self.turns[p] += self.turns[i]
                self.height[p] = max(self.height[p], self.height[i] + 1)

    def _count_up(self, i: int) -> None:
        self.size.append(1)
        self.turns.append(int(self.user[i]))
        self.height.append(0)
        parent, size, turns, height = self.parent, self.size, self.turns, self.height
        add, p = turns[i], parent[i]
        while p != -1:
            size[p] += 1
            turns[p] += add
            p = parent[p]
        tall, p = 1, parent[i]
        while p != -1 and height[p] < tall:
            height[p] = tall
            tall += 1
            p = parent[p]


def _ancestry(shape: TreeShape, node_id: int) -> set[int]:
    found: set[int] = set()
    step = node_id
    while step != -1:
        found.add(step)
        step = shape.parent[step]
    return found


class TreePlan:
    """The rows of one picture of the tree. `shape` holds every node in id order; `open_at` and
    `gone_at` are the node each open and each closed cursor is at; `viewed` is the cursor whose
    walk is the spine (open, or closed and about to be left). `LookupError` for a viewed cursor
    that is neither."""

    def __init__(
        self,
        shape: TreeShape,
        open_at: Mapping[int, int],
        gone_at: Mapping[int, int],
        viewed: int,
    ) -> None:
        self.shape = shape
        self.nodes = shape.nodes
        self._marked = {*open_at.values(), *gone_at.values()}
        self._open_at, self._gone_at = open_at, gone_at
        self._above = {c: _ancestry(shape, at) for c, at in open_at.items()}
        self._gone_above = {c: _ancestry(shape, at) for c, at in gone_at.items()}
        walk = self._above[viewed] if viewed in open_at else self._gone_above[viewed]
        self._next = {shape.parent[n]: n for n in walk if shape.parent[n] != -1}
        self._spine: tuple[list[TreeRow], _Cover] | None = None

    # The rows

    def top(self) -> list[TreeRow]:
        """The rows at the top: the spine from the root to the viewed cursor, its branches hung."""
        return self._top()[0]

    def _top(self) -> tuple[list[TreeRow], _Cover]:
        if self._spine is None:
            roots = self.shape.roots
            self._spine = self._line(roots[0], self._next.get) if roots else ([], {})
        return self._spine

    def children(self, row: TreeRow) -> list[TreeRow]:
        """What unfolding `row` shows: a turn's remaining nodes and the stubs hung in it; a stub's
        own line."""
        if row.kind == "stub":
            return self._line(row.node.id, self._pick)[0]
        hung: dict[int, list[int]] = {}
        for at, root in row.hung:
            hung.setdefault(at, []).append(root)
        out: list[TreeRow] = []
        for node_id in row.members:
            if node_id != row.node.id:
                out.append(TreeRow("node", self.nodes[node_id]))
            out.extend(self._stub(s) for s in hung.get(node_id, ()))
        return out

    def locate(self, node_id: int, key: Key | None = None) -> list[TreeRow]:
        """The rows from the top down to the one that shows `node_id` (a `node` or `turn` row), or
        to the row with `key` on the way (a stub); each is the one to unfold to reach the next.
        `LookupError` for a node not in the plan."""
        if not 0 <= node_id < len(self.nodes):
            raise LookupError(f"node {node_id} is not in the tree")
        chain: list[TreeRow] = []
        rows, cover = self._top()
        while True:
            step = node_id
            while step != -1 and step not in cover:
                step = self.shape.parent[step]
            if step == -1:
                raise LookupError(f"no row shows node {node_id}")
            row = rows[cover[step]]
            chain.append(row)
            if row.key == key or (row.kind != "stub" and row.node.id == node_id):
                return chain
            if row.kind == "stub":
                rows, cover = self._line(row.node.id, self._pick)
            else:
                rows = self.children(row)
                cover = {r.node.id: i for i, r in enumerate(rows)}

    def ancestry(self, node_id: int) -> set[int]:
        """`node_id` and every node above it."""
        return _ancestry(self.shape, node_id)

    def marks(self) -> list[int]:
        """The nodes with a cursor at them, open or closed, in id order."""
        return sorted(self._marked)

    # Making rows

    def _open_below(self, node_id: int) -> tuple[int, ...]:
        return tuple(c for c in sorted(self._above) if node_id in self._above[c])

    def _closed_below(self, node_id: int) -> tuple[int, ...]:
        return tuple(c for c in sorted(self._gone_above) if node_id in self._gone_above[c])

    def _pick(self, node_id: int) -> int | None:
        """The child a stub's line goes on through."""
        kids = self.shape.kids[node_id]
        if not kids:
            return None
        for cursor in sorted(self._above):
            for kid in kids:
                if kid in self._above[cursor]:
                    return kid
        return max(kids, key=lambda k: (self.shape.height[k], -k))

    def _stub(self, root: int) -> TreeRow:
        return TreeRow(
            "stub",
            self.nodes[root],
            expandable=True,
            size=self.shape.size[root],
            turns=self.shape.turns[root],
            cursors=self._open_below(root),
            closed=self._closed_below(root),
        )

    def _line(
        self, start: int, follow: Callable[[int], int | None]
    ) -> tuple[list[TreeRow], _Cover]:
        """The rows of the line from `start` through `follow`: nodes before the first turn, then
        a row for each turn, and the stubs hanging level with them; and which row shows each node
        of the line (and each stub hung in a turn)."""
        shape = self.shape
        kids, user, nodes = shape.kids, shape.user, self.nodes
        out: list[TreeRow] = []
        cover: _Cover = {}
        turn: list[int] | None = None
        hung: list[tuple[int, int]] = []
        cur: int | None = start
        while cur is not None:
            nxt = follow(cur)
            stubs = [k for k in kids[cur] if k != nxt] if len(kids[cur]) > 1 else []
            if user[cur]:
                turn, hung = [], []
            elif turn is None:
                cover[cur] = len(out)
                out.append(TreeRow("node", nodes[cur]))
                self._hoist(out, cover, stubs)
                cur = nxt
                continue
            assert turn is not None
            turn.append(cur)
            if nxt is None or user[nxt]:
                at = len(out)
                cover.update(dict.fromkeys(turn, at))
                if hung:
                    cover.update(dict.fromkeys((root for _at, root in hung), at))
                out.append(self._turn(turn, hung))
                self._hoist(out, cover, stubs)
                turn = None
            else:
                hung.extend((cur, s) for s in stubs)
            cur = nxt
        return out, cover

    def _hoist(self, out: list[TreeRow], cover: _Cover, stubs: list[int]) -> None:
        for root in stubs:
            cover[root] = len(out)
            out.append(self._stub(root))

    def _turn(self, members: list[int], hung: list[tuple[int, int]]) -> TreeRow:
        shape = self.shape
        touched = not self._marked.isdisjoint(members)
        plain = not hung and not touched
        if plain and (row := shape.turn_rows.get((members[0], members[-1]))) is not None:
            return row
        inside = [r for _at, r in hung]
        shown: set[int] = set()
        gone: set[int] = set()
        if touched:
            shown = {c for c, at in self._open_at.items() if at in members}
            gone = {c for c, at in self._gone_at.items() if at in members}
        opens = touched
        for root in inside:
            below = self._open_below(root)
            shown.update(below)
            gone.update(self._closed_below(root))
            opens = opens or bool(below)
        row = TreeRow(
            "turn",
            self.nodes[members[0]],
            opens=opens,
            expandable=len(members) > 1 or bool(hung),
            size=len(members) - 1,
            calls=sum(shape.calls[m] for m in members),
            branches=len(hung),
            cursors=tuple(sorted(shown)),
            closed=tuple(sorted(gone)),
            tail=members[-1],
            members=tuple(members),
            hung=tuple(hung),
        )
        if plain:
            shape.turn_rows[(members[0], members[-1])] = row
        return row
