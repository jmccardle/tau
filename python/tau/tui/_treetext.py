"""What a tree row says (D23 section 6): the type tag and text of a node, the summary of a folded
turn or stub, and the marks of the cursors at a row. Pure strings, no Textual: `_tree` fits them to
the width and paints them.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from tau._core import Body, Node, Outcome, Part
from tau._counts import Count
from tau._events import CursorView
from tau._usage import show
from tau.tui._transcript import author_name, config_line
from tau.tui._treeplan import TreeRow

_MIN_WIDTH = 2
_PREVIEW = 200  # characters of a node's text a row keeps before the width elides it
STUB_GLYPH = "├┄"
OPEN_MARK = "◀"
CLOSED_MARK = "○"
DERIVED_MARK = "⧉"
COPY_MARK = "≡"

KIND_CLASS = {
    "user": "tree--kind-user",
    "assistant": "tree--kind-assistant",
    "tool": "tree--kind-tool",
    "system": "tree--kind-system",
    "config": "tree--kind-structural",
    "custom": "tree--kind-structural",
}


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


def provenance_mark(node: Node) -> str:
    """What a row says of where its node came from (D24 M1): `⧉` for a node computed from another
    (a summary, a pruned reply), `≡ (copy of 7)` for a verbatim copy; `""` for the rest."""
    if node.derived_from is not None:
        return f"  {DERIVED_MARK}"
    if node.copied_from is not None:
        return f"  {COPY_MARK} (copy of {node.copied_from})"
    return ""


def provenance_text(node: Node, sizes: Callable[[int], tuple[int, Count]]) -> str:
    """The detail pane's lines on where `node` came from, with the walk to the source (before)
    and the walk to `node` (after) in nodes and tokens (`tau._usage.show`); `""` for a node that
    came from none. `sizes(id)` is a walk's length and its count."""
    for what, source in (("derived from", node.derived_from), ("copied from", node.copied_from)):
        if source is not None:
            before, after = sizes(source), sizes(node.id)
            return (
                f"{what} node {source}\n"
                f"walk before: {plural(before[0], 'node')}, {show(before[1])} tokens\n"
                f"walk after: {plural(after[0], 'node')}, {show(after[1])} tokens\n\n"
            )
    return ""


def detail_text(row: TreeRow, sizes: Callable[[int], tuple[int, Count]] | None = None) -> str:
    """What the detail pane shows of `row`: a node whole, or what a stub holds and how it begins.
    With `sizes` a node that is a copy or derived says from what, and how the walk changed."""
    if row.kind != "stub":
        return (provenance_text(row.node, sizes) if sizes else "") + full_text(row.node)
    shown = [f"{plural(row.size, 'node')} and {plural(row.turns, 'turn')} on this branch"]
    shown += [f"cursor {c} is on it" for c in row.cursors]
    shown += [f"cursor {c} (closed) was on it" for c in row.closed]
    return "\n".join(shown) + "\n\nIt begins:\n\n" + full_text(row.node)


def plural(count: int, word: str, many: str | None = None) -> str:
    """`1 node`, `3 nodes`; `many` is the plural when it is not `word` and an s."""
    return f"{count} {word}" if count == 1 else f"{count} {many or word + 's'}"


class Marks:
    """The cursors a row can be marked with: each open cursor (`◀ 1 scout`, the viewed one
    `◀ [0] main`, `…` while it works and `?` while a question is open) and each closed one,
    which stays as its restart record (`○ 2 compaction:1`, drawn dimmed)."""

    def __init__(
        self,
        views: Mapping[int, CursorView],
        gone: Mapping[int, int],
        labels: Sequence[tuple[str, int]],
        viewed: int,
    ) -> None:
        self.views = views
        self.labels = labels
        self.viewed = viewed
        self.open_at: dict[int, list[int]] = {}
        self.closed_at: dict[int, list[int]] = {}
        for cursor, view in sorted(views.items()):
            self.open_at.setdefault(view.at, []).append(cursor)
        for cursor, at in sorted(gone.items()):
            self.closed_at.setdefault(at, []).append(cursor)

    def open_text(self, cursors: Sequence[int]) -> str:
        """The marks of open `cursors`, each led by two spaces."""
        out = ""
        for cursor in cursors:
            view = self.views[cursor]
            out += f"  {OPEN_MARK} " + (f"[{cursor}]" if cursor == self.viewed else str(cursor))
            parts = [name for name, on in self.labels if on == cursor]
            flags = ("…" if view.busy else "") + ("?" if view.questions else "")
            out += "".join(f" {p}" for p in [*parts, flags] if p)
        return out

    def closed_text(self, cursors: Sequence[int]) -> str:
        """The marks of closed `cursors`, each led by two spaces."""
        out = ""
        for cursor in cursors:
            names = " ".join(name for name, on in self.labels if on == cursor)
            out += f"  {CLOSED_MARK} {cursor}" + (f" {names}" if names else "")
        return out


@dataclass(frozen=True)
class RowLabel:
    """A row's words. `body` may be cut to fit; `suffix` (the fold summary and the marks) never is;
    `dim_at` is where in `suffix` the closed cursors' marks begin (`None`: there are none).
    `kind` is the component class that colours the first `tag_size` characters."""

    body: str
    suffix: str
    dim_at: int | None
    kind: str
    tag_size: int

    @property
    def text(self) -> str:
        """The whole label, uncut."""
        return self.body + self.suffix


def turn_summary(row: TreeRow) -> str:
    """What a folded turn hides: `  +14 · 6 calls · 1 branch`; `""` for a turn of one node."""
    parts = [
        f"+{row.size}" if row.size else "",
        plural(row.calls, "call") if row.calls else "",
        plural(row.branches, "branch", "branches") if row.branches else "",
    ]
    return "  " + " · ".join(p for p in parts if p) if any(parts) else ""


def row_label(row: TreeRow, marks: Marks, *, folded: bool = True) -> RowLabel:
    """The words of `row`: a node's tag and text with its fold summary and the cursors at it, or a
    stub's counts and the cursors on its branch. A folded turn lists every cursor inside it, so
    none is out of sight; an open one only those at its own node (the rest show below it)."""
    if row.kind == "stub":
        body = f"{STUB_GLYPH} {plural(row.size, 'node')}, {plural(row.turns, 'turn')}"
        kind, tag_size = "tree--kind-stub", len(STUB_GLYPH)
        shown, closed = list(row.cursors), list(row.closed)
        summary = ""
    else:
        tag, text = describe(row.node)
        body, kind, tag_size = f"{tag}: {text}", KIND_CLASS[tag], len(tag) + 1
        if row.kind == "turn" and folded:
            shown, closed = list(row.cursors), list(row.closed)
        else:
            shown = marks.open_at.get(row.node.id, [])
            closed = marks.closed_at.get(row.node.id, [])
        summary = turn_summary(row) if row.kind == "turn" else ""
    before = (provenance_mark(row.node) if row.kind != "stub" else "") + summary
    before += marks.open_text(shown)
    after = marks.closed_text(closed)
    return RowLabel(body, before + after, len(before) if after else None, kind, tag_size)


def fit(label: RowLabel, width: int) -> tuple[str, int | None]:
    """`label` cut to `width` cells, and where the dimmed part begins in the result (`None`:
    nothing is dimmed). The body gives way first; if even the suffix does not fit, the whole is
    cut and nothing is dimmed."""
    room = width - len(label.suffix)
    if room >= _MIN_WIDTH:
        body = elide(label.body, room)
        return body + label.suffix, None if label.dim_at is None else len(body) + label.dim_at
    return elide(label.text, width), None
