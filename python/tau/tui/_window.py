"""The render window (D23 section 7): how much of a long walk the transcript draws.

Carried in idea from τ 0.15's `transcript.py` (tag v0.15.0: `render_cap_start`, `window_end`,
`turn_starts`), written over a walk of nodes instead of message dicts. Every widget on screen costs
every later update, so the transcript draws a window: the last `CAP_TURNS` turns (a turn begins at
a `User` node), or fewer when they hold more than `CAP_NODES` nodes. A window starts at the first
node of the walk or at a `User` node, so a turn is never cut in half. Pure functions of the walk;
no Textual.
"""

from collections.abc import Sequence

from tau._core import Body, Node

CAP_TURNS = 4
"""Turns the transcript draws at most (a turn is a `User` node and what follows it)."""

CAP_NODES = 100
"""Nodes the transcript draws at most, unless one turn holds more: it is drawn whole."""


def turn_starts(walk: Sequence[Node]) -> list[int]:
    """Every index a window may start at, ascending: 0, and each `User` node."""
    return sorted({0, *(i for i, n in enumerate(walk) if isinstance(n.body, Body.User))})


def tail_start(walk: Sequence[Node]) -> int:
    """Where the window that ends the walk starts: the later of `CAP_TURNS` turns back and the
    earliest turn start that leaves at most `CAP_NODES` nodes. A walk with no `User` node is one
    turn, drawn whole."""
    users = [i for i, n in enumerate(walk) if isinstance(n.body, Body.User)]
    if not users:
        return 0
    by_turns = users[-CAP_TURNS] if len(users) > CAP_TURNS else 0
    fitting = [s for s in (0, *users) if len(walk) - s <= CAP_NODES]
    return max(by_turns, fitting[0] if fitting else users[-1])


def window_end(walk: Sequence[Node], start: int) -> int:
    """One past the last node of the window that starts at `start`, bounded as `tail_start` is.
    At the walk's own tail it agrees: `window_end(walk, tail_start(walk)) == len(walk)`."""
    n = len(walk)
    users = [i for i, node in enumerate(walk) if i >= start and isinstance(node.body, Body.User)]
    later = users[1:]  # turns are counted from the first one in the window, not from `start`
    if not later:
        return n
    by_turns = later[CAP_TURNS - 1] if len(later) >= CAP_TURNS else n
    limit = start + CAP_NODES
    if limit >= n:
        return by_turns
    within = [u for u in later if u <= limit]
    return min(by_turns, within[-1] if within else later[0])
