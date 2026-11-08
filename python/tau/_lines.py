"""Building a new line (D24 sections 2 and 9): what compaction and prune share.

A **line** is a walk. Compaction and prune each write a shorter one beside the old: under the end
of the prefix they append copies of the nodes they keep (`copied_from`), and any node they
compute (a summary, a pruned transcript, a reply with its reasoning dropped) carries
`derived_from`. The cursor then moves onto the new line, and the old one stays in the tree.
Nothing here decides what to keep; that is the extension's.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from tau._core import Body, Node
from tau._handles import Cursor


@dataclass(frozen=True)
class Planned:
    """One node of a new line before it is written: its body and where it came from."""

    body: Body
    copied_from: int | None = None
    derived_from: int | None = None


def copy_of(node: Node) -> Planned:
    """`node` repeated verbatim."""
    return Planned(node.body, copied_from=node.id)


def prefix_length(walk: Sequence[Node]) -> int:
    """How many nodes at the start of `walk` are its prefix: the root `Config` and the `System`
    nodes before the first other message (the prompt-cache prefix every cursor shares)."""
    length = 1 if walk and isinstance(walk[0].body, Body.Config) else 0
    while length < len(walk) and isinstance(walk[length].body, Body.System):
        length += 1
    return length


def write_line(cursor: Cursor, parent: int, planned: Sequence[Planned]) -> list[int]:
    """Append `planned` as a chain under `parent`, one node under the last, and return the new
    ids in order. Nothing moves a cursor; the caller does that."""
    ids: list[int] = []
    last = parent
    for node in planned:
        last = cursor.append(
            last, node.body, copied_from=node.copied_from, derived_from=node.derived_from
        )
        ids.append(last)
    return ids
