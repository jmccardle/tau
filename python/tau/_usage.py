"""`Usage`: the built-in extension that shows what the session has spent (D24 section 3).

After each reply it shows the cursor's status line (`CursorView.shown`):

    12.3k in · 96% cached · 812 out · $0.014 · ctx 41% · $1.27 session

`cached` appears when the server reports cache reads, the dollar figures when the reply's model
has a `price`, and `ctx` when it has a `context_window`. A model with no price shows no cost,
never `$0`, and a session total that leaves out replies of an unpriced model ends in `+`.
`/usage` prints one row per model for the whole session, and a total.

Cost is computed when shown, never stored, from the price in the config the session started with
(a corrected price applies from the next start): a reply is priced by
the model its branch was on at that node (`model(at=node)`), so a model changed mid-walk is priced
per reply. Money is `Decimal`: prices are decimal numbers such as 0.30 and 3.75, which a float
cannot hold, and a total of thousands of replies must not drift. Summaries and sub-agents are
counted, since their replies are in the tree. A reply that is a copy (compaction and prune
write copies) is counted once, as its original: `counted` is that rule.

The budget: with `max_usd` set (unset: no limit), once the session's spend (the total above) reaches
it, `pre_input` refuses new input and `pre_sample` halts a tool loop before its next sample, both
with `budget: $X of $Y spent`. A reply that reported no usage (a finished one, or one an abort
cut) is estimated, never counted as free: its input is what `count` says the request it answered
cost and its output what `count` says its own node costs, both at `upper` (the `counter`
extension's count where there is one, else the byte ceiling), priced at its model. When a later
reply on the same walk reports its usage, the input it holds beyond the nearest earlier reporting
reply's (that one's input and output) is what the nodes between them cost together; it is split
among them in proportion to their estimates, and the estimate of each silent reply becomes its
share. Nothing is edited: the correction is worked out when a reply is read. `/usage` ends a
row's lines with the estimate; the totals leave it out. Only a model with no `price` cannot be
held to a budget: nothing can be estimated, so both hooks refuse naming it.

The cache notice: a reply that read nothing from the cache (`cached` reported as 0, not left
out), with at least 4,096 input tokens, shortly after a reply of the same model, tells every
client once per model. It is about money, so only a model with a `price` gets it.
"""

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, cast

from tau._config import ConfigError, ModelConfig, Price
from tau._core import Author, Body, Node, Stop
from tau._counts import Basis, Count
from tau._handles import Cursor, Subject, TreeHandle

NOTICE_MIN_INPUT = 4096
"""The smallest prompt a cache could be expected to serve (Anthropic caches from 1,024 and
Gemini from about 4,000; below that a miss means nothing)."""
NOTICE_WITHIN_MS = 300_000
"""How soon after the previous reply a cache is still warm, in milliseconds."""
REASONING_BOUND = 8
"""How far a reply's count may be off once its thinking is subtracted (D26 section 3.2)."""
_MILLION = -6  # a price is per million tokens: `scaleb(-6)` is exact
_KILO, _MEGA, _GIGA = 1000, 1_000_000, 1_000_000_000


@dataclass(frozen=True)
class Options:
    """`Usage`'s options (`extensions.usage`; `/config usage`)."""

    cache_notice: Annotated[bool, "tell the clients when a reply read nothing from the cache"] = (
        True
    )
    max_usd: Annotated[
        float | None,
        "stop at this much spend in dollars: refuse new input and halt a tool loop (unset: no "
        "limit; every model in use then needs a price)",
    ] = None

    def __post_init__(self) -> None:
        if self.max_usd is not None and not (math.isfinite(self.max_usd) and self.max_usd >= 0):
            raise ValueError(f"max_usd is an amount of dollars, at least 0, not {self.max_usd}")


@dataclass(frozen=True)
class Reading:
    """One reply's normalized `meta.usage` (D15, D26 section 2): `input` counts every prompt
    token, `cached` and `written` the part read from and written to the cache, `written_1h` the
    part of `written` kept for an hour, `tool_prompt` the part of `input` a built-in tool's use
    added, and `reasoning` the part of `output` that was thinking. Each but `input` and `output`
    is `None` when the server did not report it."""

    input: int
    output: int
    cached: int | None
    written: int | None
    reasoning: int | None = None
    tool_prompt: int | None = None
    written_1h: int | None = None

    @property
    def said(self) -> Count:
        """What the reply's words cost without its thinking: `output` less `reasoning`, a
        `reported` count. Where thinking is taken out the bound is `REASONING_BOUND`, since a
        server's thinking count "may differ by a small number" (D26 section 3.2)."""
        if not self.reasoning:
            return Count(self.output, Basis.REPORTED)
        return Count(max(self.output - self.reasoning, 0), Basis.REPORTED, REASONING_BOUND)


def reading(node: Node) -> Reading | None:
    """The `usage` a reply reports, or `None` if its provider reported none. `ValueError` for a
    `usage` that is not the normalized shape or has more cached and written than input, or for a
    reply whose provider kept the server's report (`usage_raw`) but could not normalize it; the
    callers (`_read`) turn that into "no usage, and why", so a past node never fails a step."""
    body = node.body
    if not isinstance(body, Body.Assistant) or body.meta is None:
        return None
    meta: object = json.loads(body.meta)
    found = meta.get("usage") if isinstance(meta, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if found is None:
        sent = meta.get("usage_raw") if isinstance(meta, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if sent is not None:  # the provider kept what the server said but could not read it
            raise ValueError(f"node {node.id}: the provider could not read the usage {sent!r}")
        return None
    if not isinstance(found, dict):
        raise ValueError(f"node {node.id}: meta.usage is not an object: {found!r}")
    return _read_fields(node.id, found)  # pyright: ignore[reportUnknownArgumentType]


def _read_fields(node: int, fields: dict[str, object]) -> Reading:
    """The `Reading` of a normalized `usage` object; `ValueError` naming `node` for a count that
    is not one, or a part larger than its whole."""

    def count(key: str) -> int:
        value = fields.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"node {node}: meta.usage.{key} is not a count: {value!r}")
        return value

    def optional(key: str) -> int | None:
        return count(key) if key in fields else None

    got = Reading(
        count("input"),
        count("output"),
        optional("cached"),
        optional("written"),
        optional("reasoning"),
        optional("tool_prompt"),
        optional("written_1h"),
    )
    if (got.cached or 0) + (got.written or 0) > got.input:
        raise ValueError(
            f"node {node}: meta.usage reports more cached and written tokens than input"
        )
    if (got.written_1h or 0) > (got.written or 0):
        raise ValueError(
            f"node {node}: meta.usage reports more tokens written for an hour than written"
        )
    return got


def counted(node: Node) -> bool:
    """Whether `node`'s reply counts toward usage: one a provider sampled (`node.by` is
    `Author.Provider()`: an extension's answer to a sample reported no usage because it was never
    billed), that is not a copy (`node.copied_from is None`) and not rewritten from another
    (`derived_from`: a pruned reply keeps the `meta` of the sample it came from). This is the one
    place the rule lives."""
    return (
        isinstance(node.body, Body.Assistant)
        and node.by == Author.Provider()
        and node.copied_from is None
        and node.derived_from is None
    )


def cost(usage: Reading, price: Price) -> Decimal:
    """USD for one reply: the prompt tokens neither read from nor written to the cache at the
    `input` price, reads at `cache_read`, writes at `cache_write` (those kept for an hour at
    `cache_write_1h`), and the reply at `output`."""
    cached, written, hour = usage.cached or 0, usage.written or 0, usage.written_1h or 0
    fresh = usage.input - cached - written
    assert price.cache_write_1h is not None  # `Price` sets it
    total = (
        fresh * price.input
        + cached * price.cache_read
        + (written - hour) * price.cache_write
        + hour * price.cache_write_1h
        + usage.output * price.output
    )
    return total.scaleb(_MILLION)


def tokens(n: int) -> str:
    """`n` tokens as people write them: `812`, `12.3k`, `1.2M`, `1.2G`. A count that rounds up
    to the next unit is written in it: 999,950 is `1.0M`, not `1000.0k`."""
    if n < _KILO:
        return str(n)
    for size, unit in ((_KILO, "k"), (_MEGA, "M"), (_GIGA, "G")):
        if n < size * _KILO and f"{n / size:.1f}" != "1000.0":
            return f"{n / size:.1f}{unit}"
    return f"{n / _GIGA:.1f}G"


def show(count: Count) -> str:
    """A count as people write it (`tokens`), with its basis where it is not a report:
    `~` for a heuristic, and `≤` and its `upper` for a ceiling (D26 section 1)."""
    match count.basis:
        case Basis.REPORTED | Basis.RENDERED | Basis.CALIBRATED:
            return tokens(count.tokens)
        case Basis.HEURISTIC:
            return f"~{tokens(count.tokens)}"
        case Basis.CEILING:
            return f"≤{tokens(count.upper)}"


def how_counted(counter: str | None) -> str:
    """The line `/usage` ends with: how counts are made before a request is sent."""
    if counter is None:
        return "counts: reported only, with byte ceilings for what is not sent yet"
    return f"counts: reported, and by the {counter} extension for what is not sent yet"


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
    aborted: int = 0  # replies that were aborted, and reported none
    problems: list[str] = field(default_factory=lambda: [])  # why some reported none
    spent: Decimal = Decimal(0)
    guessed: Decimal = Decimal(0)  # what the replies that reported none are estimated to cost
    priced: bool = False
    configured: bool = True

    def add(
        self,
        usage: Reading | None,
        price: Price | None,
        problem: str | None,
        guess: tuple[int, int] | None,
        *,
        aborted: bool,
    ) -> None:
        if usage is None:
            if aborted and problem is None:
                self.aborted += 1
            else:
                self.silent += 1
            if problem is not None:
                self.problems.append(problem)
            if price is not None and guess is not None:
                self.guessed += cost(Reading(guess[0], guess[1], None, None), price)
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
        # (model, its counts, why it has none if its usage could not be read, whether aborted).
        self._replies: dict[int, tuple[str, Reading | None, str | None, bool]] = {}
        self._guess: dict[int, tuple[int, int]] = {}  # reply that reported none -> (input, output)
        self._fixed: dict[int, int] = {}  # such a reply -> its output, as a later reply shows
        self._scanned = 0
        self._told: set[str] = set()
        self._windows: dict[str, int] = {}  # model -> the window its server last reported
        self.commands = (self.usage,)

    async def usage(self, tree: TreeHandle) -> str:
        """What this session has spent: one row per model, and a total."""
        rows = await self._rows(tree)
        notes = "".join(f"\n{note}" for note in self._window_notes(tree))
        return f"{table(rows)}{notes}\n{how_counted(tree.counter)}"

    def _window_notes(self, tree: TreeHandle) -> list[str]:
        """One line for each model whose server reports a window other than the config's
        `context_window` (llama.cpp's `n_ctx`): the config's is the one tau uses (D24 ruling 9),
        so a difference is said, never applied."""
        notes: list[str] = []
        for model, served in self._windows.items():
            config = _configured(tree, model)
            ours = None if config is None else config.context_window
            if ours != served:
                has = "sets none" if ours is None else f"says {ours}"
                notes.append(
                    f"{model}: the server reports a window of {served} tokens; "
                    f"the config {has} (context_window)"
                )
        return notes

    async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
        """Refuse new input once the budget (`max_usd`) is spent."""
        return await self._budget(cursor)

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> str | None:
        """Halt before any further sample once the budget (`max_usd`) is spent. Built-ins run
        before user extensions, so a hook that would have answered the sample for free is halted
        too."""
        return await self._budget(cursor)

    async def _budget(self, cursor: Cursor) -> str | None:
        """Why no more may be spent, or `None`: under the budget, or none is set."""
        limit = cursor.options().max_usd
        if limit is None:
            return None
        cap = Decimal(repr(limit))
        rows = await self._rows(cursor)
        here = cursor.model()
        unpriced = [
            m for m, r in rows.items() if (r.samples or r.silent or r.aborted) and not r.priced
        ]
        if here not in unpriced and not _priced(cursor, here):
            unpriced.append(here)
        if unpriced:
            named = ", ".join(unpriced)
            return f"budget: no price for model {named}, set its price or unset usage.max_usd"
        spent = sum((r.spent + r.guessed for r in rows.values()), Decimal(0))
        return f"budget: {money(spent)} of {_limit(cap)} spent" if spent >= cap else None

    async def post_reply(self, cursor: Cursor, subject: Subject) -> str | None:
        """Show the reply's counts on the cursor, and tell the clients if it missed the cache."""
        node = subject.node
        if node is None or not counted(node):
            return None
        model = cursor.model(at=node.id)
        config = _configured(cursor, model)
        usage, problem = _read(node)
        if usage is None and problem is None and _aborted(node):
            cursor.show(f"{model} reply was aborted and reported no usage")
        elif usage is None:
            cursor.show(f"{model} reported no usage" + (f" ({problem})" if problem else ""))
        else:
            cursor.show(await self._line(cursor, usage, config))
        if cursor.options().cache_notice and usage is not None:
            self._notice(cursor, node, model, usage)
        return None

    async def _line(self, tree: TreeHandle, usage: Reading, config: ModelConfig | None) -> str:
        parts = [f"{tokens(usage.input)} in"]
        if usage.cached is not None and usage.input:
            parts.append(f"{percent(usage.cached, usage.input)} cached")
        parts.append(f"{tokens(usage.output)} out")
        price = None if config is None else config.price
        if price is not None:
            parts.append(money(cost(usage, price)))
        if config is not None and config.context_window is not None:
            parts.append(f"ctx {percent(usage.input + usage.output, config.context_window)}")
        rows = (await self._rows(tree)).values()
        if any(r.priced for r in rows):
            more = "+" if any(not r.priced and r.samples for r in rows) else ""
            parts.append(f"{money(sum((r.spent for r in rows), Decimal(0)))}{more} session")
        return " · ".join(parts)

    def _notice(self, cursor: Cursor, node: Node, model: str, usage: Reading) -> None:
        """The cache notice (the module docstring)."""
        if usage.cached != 0 or usage.input < NOTICE_MIN_INPUT or model in self._told:
            return
        config = _configured(cursor, model)
        if config is None or config.price is None:  # "full price" means nothing without a price
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
        cursor.tell(
            f"{model} read nothing from the prompt cache "
            f"({tokens(usage.input)} tokens sent at full price)."
        )

    async def _scan(self, tree: TreeHandle) -> None:
        """Read the session's nodes not read yet, estimating each reply that reported no usage
        and correcting the estimates before it by a reply that reports."""
        nodes = tree.tree
        for id in range(self._scanned, len(nodes)):
            node = nodes.node(id)
            if counted(node):
                usage, problem = _read(node)
                try:
                    model = tree.model(at=id)
                except ConfigError as e:
                    model, usage, problem = "(no model)", None, f"node {id}: {e}"
                self._replies[id] = (model, usage, problem, _aborted(node))
                served = _served_window(node)
                if served is not None:
                    self._windows[model] = served
                if usage is None:
                    self._guess[id] = await _guess(tree, node)
                else:
                    await self._settle(tree, node, usage)
            self._scanned = id + 1  # a node that cannot be read is not read again

    async def _settle(self, tree: TreeHandle, reply: Node, usage: Reading) -> None:
        """Correct the output of the replies that reported none, from `reply`, which did.

        Its input covers every node on its walk, so what it holds beyond the nearest earlier
        reporting reply `before` (that one's input and output) is what the nodes between them
        cost together. That sum is split among those nodes in proportion to their estimates
        (`Count.upper` of `tree.count`), so the split is right whatever scale the estimates are
        on, and each silent reply takes its share. Without such a reply `before` the input holds
        the system prompt and the tools too, and nothing is corrected."""
        between: list[Node] = []
        for node in reversed(tree.tree.walk(reply.id)[:-1]):
            known = self._replies.get(node.id)
            if known is not None and known[1] is not None:
                before = known[1]
                break
            between.append(node)
        else:
            return
        silent = [n.id for n in between if n.id in self._guess]
        if not silent or reply.parent is None:
            return
        counts = (await tree.count(at=reply.parent)).nodes
        guessed = {n.id: counts[n.id].upper for n in between if n.id in counts}
        whole = sum(guessed.values())
        extra = max(usage.input - before.input - before.output, 0)
        for id in silent:
            if id in guessed and whole:
                self._fixed[id] = extra * guessed[id] // whole

    async def _rows(self, tree: TreeHandle) -> dict[str, _Row]:
        """One row per model, in the order each first replied, priced by the config now."""
        await self._scan(tree)
        rows: dict[str, _Row] = {}
        models: dict[str, ModelConfig | None] = {}
        for id, (model, usage, problem, aborted) in self._replies.items():
            if model not in models:
                models[model] = _configured(tree, model)
            config = models[model]
            row = rows.setdefault(model, _Row())
            row.configured = config is not None
            row.priced = config is not None and config.price is not None
            guess = self._guess.get(id)
            if guess is not None and id in self._fixed:
                guess = (guess[0], self._fixed[id])
            row.add(
                usage, None if config is None else config.price, problem, guess, aborted=aborted
            )
        return rows


async def _guess(tree: TreeHandle, reply: Node) -> tuple[int, int]:
    """What a reply that reported no usage probably cost, as tokens `(input, output)`: the
    request it answered (`tree.count` of the walk to its parent) and its own parts (the count of
    its node). Both are `upper`, so a spending limit errs towards having spent: a ceiling where
    nothing counts better, what the `counter` extension (`tokens`) says where there is one."""
    assert reply.parent is not None  # a reply answers something
    asked = (await tree.count(at=reply.parent)).total.upper
    said = (await tree.count(at=reply.id)).nodes[reply.id].upper
    return asked, said


def _served_window(node: Node) -> int | None:
    """The context window the server said it has when it made this reply (`meta.server.n_ctx`,
    from llama.cpp's `/props`), if it said."""
    if not isinstance(node.body, Body.Assistant) or node.body.meta is None:
        return None
    meta: object = json.loads(node.body.meta)
    server = cast("dict[str, object]", meta).get("server") if isinstance(meta, dict) else None
    window = cast("dict[str, object]", server).get("n_ctx") if isinstance(server, dict) else None
    return window if isinstance(window, int) and not isinstance(window, bool) else None


def _aborted(node: Node) -> bool:
    """Whether `node` is a reply that was cut short by an abort."""
    return isinstance(node.body, Body.Assistant) and node.body.stop == Stop.Interrupted


def _read(node: Node) -> tuple[Reading | None, str | None]:
    """A reply's counts, or none and why they could not be read: a past node never fails a step."""
    try:
        return reading(node), None
    except ValueError as e:
        return None, str(e)


def _limit(cap: Decimal) -> str:
    """The limit as the user wrote it: two decimals (`$0.50`), or all of them if it has more
    (`$0.005`)."""
    cents = cap.quantize(Decimal("0.01"))
    return f"${cents}" if cents == cap else f"${cap.normalize():f}"


def _priced(tree: TreeHandle, model: str) -> bool:
    """Whether `model` has a price in the config now."""
    config = _configured(tree, model)
    return config is not None and config.price is not None


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
    """The lines for replies that reported no usage, with why where it could not be read, and
    for replies that were aborted before they reported any."""
    rows = list(rows)
    text = ""
    count = sum(row.silent for row in rows)
    if count:
        who = "reply reported" if count == 1 else "replies reported"
        why = [p for row in rows for p in row.problems]
        text += f"\n{count} {who} no usage and not counted" + (
            f" ({'; '.join(why)})" if why else ""
        )
    cut = sum(row.aborted for row in rows)
    if cut:
        who = "reply was" if cut == 1 else "replies were"
        text += f"\n{cut} {who} aborted and reported no usage"
    guessed = sum((row.guessed for row in rows), Decimal(0))
    if guessed:
        text += f"\n~{money(guessed)} of cost estimated for them: in max_usd, not in these totals"
    return text
