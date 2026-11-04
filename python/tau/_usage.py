"""`Usage`: the built-in extension that shows what the session has spent (D24 section 3).

After each reply it shows the cursor's status line (`CursorView.shown`):

    12.3k in · 96% cached · 812 out · $0.014 · ctx 41% · $1.27 session

`cached` appears when the server reports cache reads, the dollar figures when the reply's model
has a `price`, and `ctx` when it has a `context_window`. A model with no price shows no cost,
never `$0`, and a session total that leaves out replies of an unpriced model ends in `+`.
`/usage` prints one row per model for the whole session, and a total.

Cost is computed when shown, never stored, from the price in the config now: a reply is priced by
the model its branch was on at that node (`model(at=node)`), so a model changed mid-walk is priced
per reply. Money is `Decimal`: prices are decimal numbers such as 0.30 and 3.75, which a float
cannot hold, and a total of thousands of replies must not drift. Summaries and sub-agents are
counted, since their replies are in the tree. A reply that is a copy (compaction and prune
write copies) is counted once, as its original: `counted` is that rule.

The cache notice: a reply that read nothing from the cache (`cached` reported as 0, not left
out), with at least 4,096 input tokens, shortly after a reply of the same model, tells every
client once per model.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from tau._config import ConfigError, ModelConfig, Price
from tau._core import Body, Node
from tau._handles import Cursor, Subject, TreeHandle

NOTICE_MIN_INPUT = 4096
"""The smallest prompt a cache could be expected to serve (Anthropic caches from 1,024 and
Gemini from about 4,000; below that a miss means nothing)."""
NOTICE_WITHIN_MS = 300_000
"""How soon after the previous reply a cache is still warm, in milliseconds."""
_MILLION = -6  # a price is per million tokens: `scaleb(-6)` is exact
_KILO, _MEGA = 1000, 1_000_000


@dataclass(frozen=True)
class Options:
    """`Usage`'s options (`extensions.usage`; `/config usage`)."""

    cache_notice: Annotated[bool, "tell the clients when a reply read nothing from the cache"] = (
        True
    )


@dataclass(frozen=True)
class Reading:
    """One reply's normalized `meta.usage` (D15): `input` counts every prompt token, `cached` and
    `written` the part read from and written to the cache, and are `None` when the server did not
    report them."""

    input: int
    output: int
    cached: int | None
    written: int | None


def reading(node: Node) -> Reading | None:
    """The `usage` a reply reports, or `None` if its provider reported none. `ValueError` for a
    `usage` that is not the normalized shape or has more cached and written than input; the callers
    (`_read`) turn that into "no usage, and why", so a past node never fails a step."""
    body = node.body
    if not isinstance(body, Body.Assistant) or body.meta is None:
        return None
    meta: object = json.loads(body.meta)
    found = meta.get("usage") if isinstance(meta, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if found is None:
        return None
    if not isinstance(found, dict):
        raise ValueError(f"node {node.id}: meta.usage is not an object: {found!r}")
    fields: dict[str, object] = found  # pyright: ignore[reportUnknownVariableType]

    def count(key: str) -> int:
        value = fields.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"node {node.id}: meta.usage.{key} is not a count: {value!r}")
        return value

    def optional(key: str) -> int | None:
        return count(key) if key in fields else None

    got = Reading(count("input"), count("output"), optional("cached"), optional("written"))
    if (got.cached or 0) + (got.written or 0) > got.input:
        raise ValueError(
            f"node {node.id}: meta.usage reports more cached and written tokens than input"
        )
    return got


def counted(node: Node) -> bool:
    """Whether `node`'s reply counts toward usage: a reply that is not a copy
    (`node.copied_from is None`) and not rewritten from another (`derived_from`: a pruned reply
    keeps the `meta` of the sample it came from). This is the one place the rule lives."""
    return (
        isinstance(node.body, Body.Assistant)
        and node.copied_from is None
        and node.derived_from is None
    )


def cost(usage: Reading, price: Price) -> Decimal:
    """USD for one reply: the prompt tokens neither read from nor written to the cache at the
    `input` price, reads at `cache_read`, writes at `cache_write`, and the reply at `output`."""
    cached, written = usage.cached or 0, usage.written or 0
    fresh = usage.input - cached - written
    total = (
        fresh * price.input
        + cached * price.cache_read
        + written * price.cache_write
        + usage.output * price.output
    )
    return total.scaleb(_MILLION)


def tokens(n: int) -> str:
    """`n` tokens as people write them: `812`, `12.3k`, `1.2M`."""
    if n < _KILO:
        return str(n)
    if n < _MEGA and f"{n / _KILO:.1f}" != "1000.0":  # 999,950 is 1.0M, not 1000.0k
        return f"{n / _KILO:.1f}k"
    return f"{n / _MEGA:.1f}M"


def money(amount: Decimal) -> str:
    """`amount` in dollars, rounded half up: two decimals from `$1` (`$1.27`), three below
    (`$0.014`), `$0` for exactly nothing and `<$0.001` for less than the display holds. An amount
    that rounds up to a dollar is `$1.00`, so the rule never shows `$1.000`."""
    if amount == 0:
        return "$0"
    shown = amount.quantize(Decimal("0.001"), ROUND_HALF_UP)
    if shown >= 1:
        return f"${amount.quantize(Decimal('0.01'), ROUND_HALF_UP)}"
    return f"${shown}" if shown > 0 else "<$0.001"


def percent(part: int, whole: int) -> str:
    """`part` of `whole` as a whole percent, rounded half up."""
    return f"{(200 * part + whole) // (2 * whole)}%"


@dataclass
class _Row:
    """What one model has spent."""

    samples: int = 0
    input: int = 0
    output: int = 0
    written: int = 0
    cached: int = 0
    cached_of: int = 0  # the input of those replies
    silent: int = 0  # replies that reported no usage
    problems: list[str] = field(default_factory=lambda: [])  # why some reported none
    spent: Decimal = Decimal(0)
    priced: bool = False
    configured: bool = True

    def add(self, usage: Reading | None, price: Price | None, problem: str | None) -> None:
        if usage is None:
            self.silent += 1
            if problem is not None:
                self.problems.append(problem)
            return
        self.samples += 1
        self.input += usage.input
        self.output += usage.output
        self.written += usage.written or 0
        if usage.cached is not None:
            self.cached += usage.cached
            self.cached_of += usage.input
        if price is not None:
            self.spent += cost(usage, price)


class Usage:
    """The built-in extension `usage` (the module docstring)."""

    name = "usage"
    options = Options

    def __init__(self) -> None:
        # Per node of the session, once: nodes never change. `None`: no counted reply.
        # (model, its counts, why it has none if its usage could not be read).
        self._replies: dict[int, tuple[str, Reading | None, str | None]] = {}
        self._scanned = 0
        self._told: set[str] = set()
        self.commands = (self.usage,)

    def usage(self, tree: TreeHandle) -> str:
        """What this session has spent: one row per model, and a total."""
        return table(self._rows(tree))

    async def post_reply(self, cursor: Cursor, subject: Subject) -> str | None:
        """Show the reply's counts on the cursor, and tell the clients if it missed the cache."""
        node = subject.node
        if node is None or not counted(node):
            return None
        model = cursor.model(at=node.id)
        config = _configured(cursor, model)
        usage, problem = _read(node)
        if usage is None:
            cursor.show(f"{model} reported no usage" + (f" ({problem})" if problem else ""))
        else:
            cursor.show(self._line(cursor, usage, config))
        if cursor.options().cache_notice and usage is not None:
            self._notice(cursor, node, model, usage)
        return None

    def _line(self, tree: TreeHandle, usage: Reading, config: ModelConfig | None) -> str:
        parts = [f"{tokens(usage.input)} in"]
        if usage.cached is not None and usage.input:
            parts.append(f"{percent(usage.cached, usage.input)} cached")
        parts.append(f"{tokens(usage.output)} out")
        price = None if config is None else config.price
        if price is not None:
            parts.append(money(cost(usage, price)))
        if config is not None and config.context_window is not None:
            parts.append(f"ctx {percent(usage.input + usage.output, config.context_window)}")
        rows = self._rows(tree).values()
        if any(r.priced for r in rows):
            more = "+" if any(not r.priced and r.samples for r in rows) else ""
            parts.append(f"{money(sum((r.spent for r in rows), Decimal(0)))}{more} session")
        return " · ".join(parts)

    def _notice(self, cursor: Cursor, node: Node, model: str, usage: Reading) -> None:
        """The cache notice (the module docstring)."""
        if usage.cached != 0 or usage.input < NOTICE_MIN_INPUT or model in self._told:
            return
        tree = cursor.tree
        before = [n for n in tree.walk(node.id)[:-1] if isinstance(n.body, Body.Assistant)]
        if not before:
            return
        previous = before[-1]
        if (
            not counted(previous)
            or node.time - previous.time >= NOTICE_WITHIN_MS
            or cursor.model(at=previous.id) != model
        ):
            return
        self._told.add(model)
        text = (
            f"{model} read nothing from the prompt cache "
            f"({tokens(usage.input)} tokens sent at full price)."
        )
        config = _configured(cursor, model)
        if config is not None and config.provider == "openai":
            text += (
                " Through a proxy's OpenAI endpoint nothing marks the cache;"
                ' try provider "anthropic".'
            )
        cursor.tell(text)

    def _scan(self, tree: TreeHandle) -> None:
        """Read the session's nodes not read yet."""
        nodes = tree.tree
        for id in range(self._scanned, len(nodes)):
            node = nodes.node(id)
            self._scanned = id + 1  # a node that cannot be read is not read again
            if counted(node):
                usage, problem = _read(node)
                try:
                    model = tree.model(at=id)
                except ConfigError as e:
                    model, usage, problem = "(no model)", None, f"node {id}: {e}"
                self._replies[id] = (model, usage, problem)

    def _rows(self, tree: TreeHandle) -> dict[str, _Row]:
        """One row per model, in the order each first replied, priced by the config now."""
        self._scan(tree)
        rows: dict[str, _Row] = {}
        models: dict[str, ModelConfig | None] = {}
        for model, usage, problem in self._replies.values():
            if model not in models:
                models[model] = _configured(tree, model)
            config = models[model]
            row = rows.setdefault(model, _Row())
            row.configured = config is not None
            row.priced = config is not None and config.price is not None
            row.add(usage, None if config is None else config.price, problem)
        return rows


def _read(node: Node) -> tuple[Reading | None, str | None]:
    """A reply's counts, or none and why they could not be read: a past node never fails a step."""
    try:
        return reading(node), None
    except ValueError as e:
        return None, str(e)


def _configured(tree: TreeHandle, model: str) -> ModelConfig | None:
    """The config of `model` now, or `None` if the catalog no longer has it."""
    try:
        return tree.resolved_model(model)
    except ConfigError:
        return None


_HEAD = ("model", "samples", "input", "cached", "written", "output", "cost")


def _cells(name: str, row: _Row, *, priced: bool, more: str = "") -> list[str]:
    cached = percent(row.cached, row.cached_of) if row.cached_of else "-"
    written = tokens(row.written) if row.written else "-"
    return [
        name + ("" if row.configured else " (not in config)"),
        str(row.samples),
        tokens(row.input),
        cached,
        written,
        tokens(row.output),
        money(row.spent) + more if priced else "-",
    ]


def table(every: dict[str, _Row]) -> str:
    """The `/usage` text for `every` model's row: aligned columns for the models that reported
    usage, a total row when there are several, and a line each for the replies that reported
    none and the models whose cost is missing."""
    rows = {name: row for name, row in every.items() if row.samples}
    silent = _silent(every.values())
    if not rows:
        return "no replies yet" + silent
    lines = [_cells(name, row, priced=row.priced) for name, row in rows.items()]
    if len(rows) > 1:
        priced = any(r.priced for r in rows.values())
        more = "+" if any(not r.priced for r in rows.values()) else ""
        lines.append(_cells("total", _total(rows.values()), priced=priced, more=more))
    text = _aligned([list(_HEAD), *lines]) + silent
    unpriced = [name for name, row in rows.items() if not row.priced]
    return text + ("\nno price, so no cost: " + ", ".join(unpriced) if unpriced else "")


def _aligned(grid: list[list[str]]) -> str:
    """`grid` as columns, the first left and the rest right aligned."""
    widths = [max(len(r[c]) for r in grid) for c in range(len(_HEAD))]
    return "\n".join(
        "  ".join(
            cell.ljust(w) if c == 0 else cell.rjust(w)
            for c, (cell, w) in enumerate(zip(row, widths, strict=True))
        )
        for row in grid
    )


def _total(rows: Iterable[_Row]) -> _Row:
    """The rows added up."""
    total = _Row()
    for row in rows:
        total.samples += row.samples
        total.input += row.input
        total.output += row.output
        total.written += row.written
        total.cached += row.cached
        total.cached_of += row.cached_of
        total.spent += row.spent
    return total


def _silent(rows: Iterable[_Row]) -> str:
    """The line for replies that reported no usage, with why where it could not be read."""
    rows = list(rows)
    count = sum(row.silent for row in rows)
    if not count:
        return ""
    who = "reply reported" if count == 1 else "replies reported"
    why = [p for row in rows for p in row.problems]
    return f"\n{count} {who} no usage and not counted" + (f" ({'; '.join(why)})" if why else "")
