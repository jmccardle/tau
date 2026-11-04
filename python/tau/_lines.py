"""Building a new line (D24 sections 2 and 9): what compaction and prune share.

A **line** is a walk. Compaction and prune each write a shorter one beside the old: under the end
of the prefix they append copies of the nodes they keep (`copied_from`), and any node they
compute (a summary, a pruned transcript, a reply with its reasoning dropped) carries
`derived_from`. The cursor then moves onto the new line, and the old one stays in the tree.
Nothing here decides what to keep; that is the extension's.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from tau._core import Body, Node, Outcome, Part
from tau._handles import Cursor

CHARS_PER_TOKEN = 4
"""The estimate's whole model: characters divided by this are tokens, never treated as exact."""


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


def characters(body: Body) -> int:
    """The characters a node adds to a request: text, reasoning, call names and arguments, and
    outcomes. A `Config` or `Custom` node is not sent, so it adds none."""
    match body:
        case Body.System(text=text) | Body.User(text=text):
            return len(text)
        case Body.Assistant(parts=parts):
            return sum(_part_characters(p) for p in parts)
        case Body.Results(outcomes=outcomes):
            return sum(len(_outcome_text(o)) for _, o, _ in outcomes)
        case _:
            return 0


def _part_characters(part: Part) -> int:
    match part:
        case Part.Text(text=text) | Part.Thinking(text=text):
            return len(text)
        case Part.Call(args_raw=args):
            return len(args)
        case _:
            return 0


def _outcome_text(outcome: object) -> str:
    match outcome:
        case Outcome.Ok(value=value):
            return value
        case Outcome.Err(error=error):
            return error
        case _:
            return ""


def tokens_of(chars: int) -> int:
    """`chars` characters as estimated tokens."""
    return chars // CHARS_PER_TOKEN
