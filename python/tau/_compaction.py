"""`Compaction`: the built-in extension that summarizes a long branch (D24 section 2).

When a branch nears its model's context window, the next sample is preceded by a summary: the
older part of the branch (the **head**) becomes one `User` node, and the newest part (the
**tail**, at most `keep` tokens unless the newest turn is bigger) is copied. They hang from the
end of the prefix as a new line, and the cursor moves onto it. The old line stays in the tree.
`/compact [focus]` does it now, with text the summary should keep in mind.

The summary is written by a **summarizer cursor**: a cursor opened at the branch's node, sent
the summary prompt, and waited for. It shows in the tree and its reply is counted by `usage`; it
is closed afterwards. It is offered the branch's tools, which keeps the prompt cache warm, and
refused any call it makes (`pre_tool`). Compaction's own `pre_sample` skips those cursors.

Every figure is a `tau.Count` from `Cursor.count` (D26): what the servers reported, and for what
no report covers yet the bytes of the node as a ceiling. Before a summarizer is paid for, the line
must have room (the prefix and the request's overhead, the tail, and the summary allowance); after
it, the tail is cut to what fits beside the summary.

The trigger is proactive only: a provider's "prompt too long" is not caught. `/compact`, then a
step, recovers.
"""

from dataclasses import dataclass
from typing import Annotated, Literal

from tau._config import ConfigError, model_config
from tau._core import Author, Body, CursorClosed, Ending, Node, NoSuchLabel, Part, Stop
from tau._counts import NONE, Count, RequestCount, ceiling_of, reported
from tau._handles import Cursor, NodeHandle, Subject
from tau._lines import Planned, copy_of, prefix_length, write_line
from tau._protocols import CallDraft
from tau._usage import reading, show, tokens
from tau._values import Model

PROMPT = """\
Summarize the conversation above so that you, or another assistant, can carry on the work from \
the summary alone. Do not call any tools; reply with the summary as plain text.

Write the summary as Markdown with these sections, and leave out a section that has nothing in \
it:

## Goal
What the user is trying to get done, in their words where that matters.
## Constraints and preferences
Requirements, decisions and style the user has set.
## Progress
What has been done, and what was learned: files read or changed, commands run and what they showed.
## Open work
What is left, and the next step.
## Facts to keep
Names, paths, identifiers, numbers and errors that later steps will need, exactly as written.

If the conversation already begins with an earlier summary, carry what still matters from it \
forward instead of repeating it."""
FOCUS = "\n\nFor this summary, pay particular attention to: {focus}"
ABORTED = "compaction aborted"
SUMMARY = "The earlier part of this conversation was summarized:\n\n<summary>\n{text}\n</summary>"


@dataclass(frozen=True)
class Options:
    """`Compaction`'s options (`extensions.compaction`; `/config compaction`)."""

    auto: Annotated[bool, "compact by itself, at the soft limit and at the hard one"] = True
    reserve: Annotated[
        int | None,
        "tokens kept free for the reply and for the summary itself (unset: 16384, or a quarter "
        "of the window if less)",
    ] = None
    hard: Annotated[
        int | None,
        "count above which a turn is interrupted and compacted (unset: window - reserve)",
    ] = None
    soft: Annotated[
        int | None, "count above which the turn's end compacts (unset: 80% of the hard limit)"
    ] = None
    keep: Annotated[
        int | None,
        "the most tokens of the newest nodes copied verbatim (unset: 20000, or half the room)",
    ] = None
    model: Annotated[Model | None, "the model that writes the summary (none: the branch's)"] = None

    def __post_init__(self) -> None:
        if self.reserve is not None and self.reserve < 0:
            raise ValueError("reserve is a token count, at least 0")
        for name in ("keep", "hard", "soft"):
            if (value := getattr(self, name)) is not None and value < 1:
                raise ValueError(f"{name} is a token count, at least 1, not {value}")
        if self.soft is not None and self.hard is not None and self.soft > self.hard:
            raise ValueError(f"soft ({self.soft}) is above hard ({self.hard})")


DEFAULT_RESERVE = 16384
"""The `reserve` of a model whose window is at least four times this; a smaller window gets a
quarter of it."""
DEFAULT_KEEP = 20000
"""The `keep` of a model whose soft limit is at least twice this; a smaller one gets half of it."""
SOFT_PERCENT = 80
"""The soft limit, as a share of the hard limit, when `soft` is not set."""
MAX_ALLOWANCE = 8192
"""The most the **summary allowance** (room kept for the summary) can be."""
MIN_RESERVE = 2
"""The smallest `reserve` that leaves a summary an allowance of a token."""

Mode = Literal["manual", "soft", "hard"]
"""Who asked: `/compact`, the soft limit at a turn's end, or the hard limit before a sample."""


class CompactionFailed(RuntimeError):
    """The summary could not be written; nothing was written to the tree."""


@dataclass(frozen=True)
class Limits:
    """The limits of the cursor's model: `soft` and `hard` (both `None`: it has no
    `context_window`), the `keep` to use and the `reserve`. A summary may take half the reserve,
    at most `MAX_ALLOWANCE`: the **summary allowance**."""

    soft: int | None
    hard: int | None
    keep: int
    reserve: int

    @property
    def allowance(self) -> int:
        """Tokens a line must leave room for, for the summary."""
        return min(self.reserve // 2, MAX_ALLOWANCE)

    def room(self, mode: Mode) -> int | None:
        """The limit a line must end under: the soft limit for the soft limit's compaction,
        else the hard one."""
        return self.soft if mode == "soft" else self.hard


def _limits(cursor: Cursor, options: Options) -> Limits:
    """The limits of the cursor's model. Hard is `hard`, else the window less `reserve`; soft is
    `soft`, else 80% of hard; `keep` is the one set, else `DEFAULT_KEEP` or half the soft limit
    if that is less; `reserve`, when unset, is `DEFAULT_RESERVE` or a quarter of the window if
    that is less. `ConfigError` if a limit leaves no room, the reserve leaves none for a summary,
    a `soft` set is above the hard limit, or a `keep` set does not fit under the soft limit."""
    model = cursor.model()
    window = cursor.resolved_model(model).context_window
    if window is None:
        reserve = DEFAULT_RESERVE if options.reserve is None else options.reserve
        return Limits(None, None, DEFAULT_KEEP if options.keep is None else options.keep, reserve)
    reserve = min(DEFAULT_RESERVE, window // 4) if options.reserve is None else options.reserve
    if reserve < MIN_RESERVE:
        raise ConfigError(
            f"{model}: a reserve of {reserve} tokens leaves no room for a summary "
            f"(the window is {window}); raise the window or the reserve"
        )
    hard = window - reserve if options.hard is None else options.hard
    if hard <= 0 or hard > window:
        raise ConfigError(
            f"{model}: the hard limit {hard} is not within its {window}-token window "
            f"(reserve {reserve})"
        )
    soft = hard * SOFT_PERCENT // 100 if options.soft is None else options.soft
    if soft > hard:
        raise ConfigError(f"{model}: soft ({soft}) is above the hard limit ({hard})")
    if options.keep is None:
        return Limits(soft, hard, min(DEFAULT_KEEP, soft // 2), reserve)
    if options.keep >= soft:
        raise ConfigError(
            f"{model}: keep ({options.keep}) does not fit under the soft limit ({soft}); "
            "lower keep, or raise soft, hard or the window"
        )
    return Limits(soft, hard, options.keep, reserve)


def _up(request: RequestCount, node: Node) -> int:
    """The most `node` costs in `request`: nothing for a node that is not sent or is inside a
    lump, whose first node holds the sum."""
    count = request.nodes.get(node.id)
    return 0 if count is None else count.upper


def _cut(request: RequestCount, node: Node) -> bool:
    """Whether a tail may start at `node`: a `User` or `Assistant` node (so a `Results` node
    stays with its batch) that no lump holds inside."""
    return isinstance(node.body, (Body.User, Body.Assistant)) and node.id in request.nodes


def tail_start(body: list[Node], request: RequestCount, keep: int) -> int:
    """Where the tail of `body` (a walk after its prefix) starts: the earliest node a tail may
    start at whose nodes cost at most `keep` tokens (by `request`'s upper counts), or the last
    such node if even that costs more: the newest turn is kept whole. 0 means everything is
    tail."""
    cuts = [i for i, n in enumerate(body) if _cut(request, n)]
    if not cuts:
        return 0
    found = _fit(body, request, keep, cuts[-1])
    return cuts[-1] if found is None else found


def _text(reply: Node) -> str:
    body = reply.body
    if not isinstance(body, Body.Assistant):
        raise CompactionFailed(f"the summarizer's last node ({reply.id}) is not a reply")
    text = "\n".join(p.text for p in body.parts if isinstance(p, Part.Text) and p.text).strip()
    if not text:
        raise CompactionFailed("the summarizer replied with no text")
    if body.stop == Stop.Length:
        raise CompactionFailed("the summary was cut off at the model's output limit")
    return text


def _turn_ended(node: Node) -> bool:
    """Whether `node` is a reply that asks for no tool: the end of the agent's turn."""
    body = node.body
    return isinstance(body, Body.Assistant) and not any(
        isinstance(p, Part.Call) for p in body.parts
    )


def _is_summary(node: Node) -> bool:
    """Whether `node` is a summary this extension wrote."""
    return (
        isinstance(node.body, Body.User)
        and node.by == Author.Extension("compaction")
        and node.derived_from is not None
    )


def _asked(body: list[Node], start: int) -> Node | None:
    """The `User` node the turn in progress began with, when the head holds it: a line that
    summarizes it away leaves the model with a paraphrase of what it is answering. `None` when
    the turn is over or the input is not yet answered (the tail holds it), when the tail holds
    it, or when what the head holds is a summary (the turn began before an earlier compaction)."""
    last = body[-1]
    if isinstance(last.body, Body.User) or _turn_ended(last):
        return None
    users = [i for i, n in enumerate(body) if isinstance(n.body, Body.User)]
    if not users or users[-1] >= start or _is_summary(body[users[-1]]):
        return None
    return body[users[-1]]


def _base(request: RequestCount, prefix: list[Node]) -> Count:
    """The prefix and the request's overhead: the base of the count if a report billed them,
    else the tool definitions it holds and the ceilings of the prefix nodes."""
    total = request.base
    if not request.base_nodes:
        for node in prefix:
            total += request.nodes.get(node.id, NONE)
    return total


def _summary_count(reply: Node, body: Body) -> Count:
    """What the line pays for the summary: the summarizer reply's reported `output` and the
    ceiling of the words around it, or the ceiling of the whole node if the reply reported
    nothing."""
    try:
        usage = reading(reply)
    except ValueError:
        usage = None
    if usage is None:
        return ceiling_of(body)
    return reported(usage.output) + ceiling_of(Body.User(SUMMARY.format(text="")))


def _held(request: RequestCount, nodes: list[Node], asked: Node | None) -> Count:
    """What `nodes` cost together: each one's count, and for the user message `asked` (a node
    that a lump may hold, so it has no count of its own) its ceiling."""
    total = NONE
    for node in nodes:
        total += ceiling_of(node.body) if node is asked else request.nodes.get(node.id, NONE)
    return total


def _crowded(
    plan: "_Plan", wanted: list[Node], room: int, allowance: int
) -> tuple[str, str] | None:
    """Why the prefix and the nodes `wanted` alone already reach `room`, with room for a summary,
    naming the part that is too big, as `(which part, why)` (`None`: they leave room). The newest
    node by itself, or the prefix, is not `keep`'s doing."""
    request, base = plan.request, plan.base
    if base.upper >= room:
        return "prefix", (
            f"the prefix and the request's overhead (system prompt, context files, skills, tool "
            f"definitions) take {show(base)} of the {tokens(room)} tokens the model has room for"
        )
    newest = max((_up(request, n) for n in wanted[-1:]), default=0)
    if newest >= room:
        return "newest", (
            f"the newest node alone takes {tokens(newest)} tokens at most, over the "
            f"{tokens(room)} the model has room for"
        )
    held = _held(request, wanted, plan.asked)
    if base.upper + held.upper + allowance >= room:
        # a tail longer than the newest turn is `keep`'s doing; the newest turn is not
        keeps = f"keep = {plan.keep}, " if plan.start < plan.minimal else ""
        return "kept", (
            f"the prefix ({show(base)}) and the newest nodes kept ({keeps}{show(held)}) leave "
            f"less than the {tokens(allowance)} tokens a summary may take of the {tokens(room)} "
            f"the model has room for" + ("; lower keep" if keeps else "")
        )
    return None


def _fit(tail: list[Node], request: RequestCount, avail: int, floor: int) -> int | None:
    """The first index of `tail` at which a suffix of it may start and costs at most `avail`
    (at most `floor`: the nodes that must stay), or `None` if even that does not fit."""
    cost = [0] * (len(tail) + 1)
    for i in range(len(tail) - 1, -1, -1):
        cost[i] = cost[i + 1] + _up(request, tail[i])
    for i in range(floor + 1):
        if (i in (0, len(tail)) or _cut(request, tail[i])) and cost[i] <= avail:
            return i
    return None


@dataclass(frozen=True)
class _Plan:
    """What `_compact` settled before paying for a summary: the walk and the length of its
    prefix, where the tail starts and where the newest turn alone would start (indexes in the walk
    after the prefix), the user message the turn in progress began with if the head would lose
    it, the figures, and the `keep` used."""

    walk: list[Node]
    prefix: int
    start: int
    minimal: int
    asked: Node | None
    request: RequestCount
    base: Count
    keep: int


class Compaction:
    """The built-in extension `compaction` (the module docstring)."""

    name = "compaction"
    options = Options

    def __init__(self) -> None:
        self._summarizers: set[int] = set()  # the cursors opened here, until they are closed
        self._replied: dict[int, int] = {}  # per cursor: the reply its running step committed
        self._warned: set[tuple[int, Mode, str]] = set()  # (cursor, limit, part) told it is too big
        # cursors whose summary did not fit: the limits, and the node they were on
        self._futile: dict[int, tuple[Limits, int]] = {}
        self._made = 0
        self.commands = (self.compact,)

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> None:
        """The hard limit: compact first, interrupting the turn, if the branch would not fit the
        model's window."""
        if cursor.id in self._summarizers or not cursor.options().auto:
            return
        limits = _limits(cursor, cursor.options())
        if limits.hard is None:
            cursor.show(f"{cursor.model()} has no context_window")
        elif (await cursor.count()).total.upper > limits.hard:
            await self._compact(cursor, "", "hard")

    async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
        """A step begins: it has committed no reply yet."""
        self._replied.pop(cursor.id, None)

    async def post_reply(self, cursor: Cursor, subject: Subject) -> None:
        """Remember the reply this step committed."""
        if subject.node is not None:
            self._replied[cursor.id] = subject.node.id

    async def post_step(self, cursor: Cursor, subject: Subject) -> None:
        """The soft limit: when a turn ends (a step that came to a reply with no calls, the cursor
        coming to rest), compact if the branch is past it. A turn that is still going on is left
        alone until the hard limit. Only a step that committed the reply itself counts: going back
        to a reply, or a cursor opened on one, ends a step there without a turn."""
        if cursor.id in self._summarizers or not isinstance(subject.ending, Ending.Done):
            return
        node = cursor.node()
        if self._replied.pop(cursor.id, None) != node.id or not _turn_ended(node):
            return
        if not cursor.options().auto:
            return
        limits = _limits(cursor, cursor.options())
        if limits.soft is None or self._tried(cursor, limits):
            return
        if (await cursor.count()).total.upper > limits.soft:
            await self._compact(cursor, "", "soft")

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        """Refuse any call a summarizer makes."""
        return "summarizing: no tools" if node.cursor_id in self._summarizers else None

    async def compact(self, cursor: Cursor, focus: str = "") -> str:
        """Summarize this conversation now, on a new line (the old one stays in the tree).
        `focus` is text the summary should keep in mind."""
        return await self._compact(cursor, focus, "manual")

    async def _plan(self, cursor: Cursor, limits: Limits, mode: Mode) -> _Plan | str:
        """Where the tail starts, or why there is nothing to compact. A `/compact` that sets no
        `keep` keeps half of what the line costs if that is less than the default: asked for, it
        summarizes something."""
        request = await cursor.count()
        walk = cursor.walk()
        prefix = prefix_length(walk)
        body = walk[prefix:]
        keep = limits.keep
        if mode == "manual" and cursor.options().keep is None:
            keep = min(keep, max(1, request.total.upper // 2))
        start = tail_start(body, request, keep)
        head = body[:start]
        messages = [
            n for n in head if isinstance(n.body, (Body.User, Body.Assistant, Body.Results))
        ]
        if not messages or all(_is_summary(n) for n in messages):
            return "nothing to compact yet"
        base = _base(request, walk[:prefix])
        minimal = max((i for i, n in enumerate(body) if _cut(request, n)), default=0)
        return _Plan(walk, prefix, start, minimal, _asked(body, start), request, base, keep)

    async def _compact(self, cursor: Cursor, focus: str, mode: Mode) -> str:
        """Compact `cursor`'s branch; the notice (or why nothing was written)."""
        options = cursor.options()
        limits = _limits(cursor, options)
        plan = await self._plan(cursor, limits, mode)
        if isinstance(plan, str):
            if mode == "hard":
                total = show((await cursor.count()).total)
                room = tokens(limits.hard or 0)
                raise CompactionFailed(
                    f"the request takes {total} tokens, more than the {room} the model has room "
                    f"for, and {plan}"
                )
            return plan
        room = limits.room(mode)
        if room is not None:
            wanted = ([plan.asked] if plan.asked is not None else []) + plan.walk[plan.prefix :][
                plan.start :
            ]
            crowded = _crowded(plan, wanted, room, limits.allowance)
            if crowded is not None:
                return self._unfit(cursor, crowded[1], crowded[0], mode, limits, pays=False)
        cursor.show("compacting…")
        try:
            made = await self._summarize(cursor, focus, options.model)
        finally:
            cursor.show(None)
        if made is None:
            if mode == "hard":
                raise CompactionFailed("compaction was stopped, and the sample would not fit")
            return ABORTED
        return self._write(cursor, plan, limits, mode, made)

    def _write(
        self, cursor: Cursor, plan: _Plan, limits: Limits, mode: Mode, made: tuple[str, Node]
    ) -> str:
        """Choose the tail now the summary is known, check the line, and write it."""
        request, body = plan.request, plan.walk[plan.prefix :]
        wrapped = Body.User(SUMMARY.format(text=made[0]))
        paid = _summary_count(made[1], wrapped)
        asked = [] if plan.asked is None else [plan.asked]
        tail = body[plan.start :]
        asked_up = _held(request, asked, plan.asked).upper
        floor = len(tail) - 1 if isinstance(body[-1].body, Body.User) else len(tail)
        room = limits.room(mode)
        begin = 0
        if room is not None:
            fits = _fit(tail, request, room - plan.base.upper - paid.upper - asked_up, floor)
            if fits is None:
                limit = f"{'soft ' if mode == 'soft' else ''}limit of {tokens(room)}"
                why = (
                    f"the summary takes {show(paid)} tokens and, with the prefix "
                    f"({show(plan.base)}), does not fit the {limit}"
                )
                return self._unfit(cursor, why, "fit", mode, limits, pays=True)
            begin = fits
        start = plan.start + begin
        replaced = sum(_up(request, n) for n in body[:start])
        if paid.upper >= replaced:
            why = (
                f"compacting would not make it shorter: the summary takes {show(paid)} tokens "
                f"and the {tokens(replaced)} it replaces take at most that"
            )
            return self._unfit(cursor, why, "shorter", mode, limits, pays=True)
        planned = [copy_of(n) for n in body[:start] if isinstance(n.body, Body.Config)]
        planned.append(Planned(wrapped, derived_from=body[start - 1].id))
        summary_at = len(planned) - 1
        kept = asked + body[start:]
        planned += [copy_of(n) for n in kept]
        after = plan.base + paid + _held(request, kept, plan.asked)
        ids = write_line(cursor, plan.walk[plan.prefix - 1].id, planned)
        cursor.move(ids[-1])
        self._futile.pop(cursor.id, None)
        counted_by = "reported counts only" if cursor.counter is None else f"by {cursor.counter}"
        text = (
            f"compacted {show(request.total)} → {show(after)} tokens "
            f"(nodes {body[0].id}–{body[start - 1].id} summarized as node {ids[summary_at]}; "  # noqa: RUF001
            f"{counted_by})"
        )
        cursor.tell(text)
        return text

    def _tried(self, cursor: Cursor, limits: Limits) -> bool:
        """Whether a summary was paid for on this line, with these limits, and did not fit."""
        failed = self._futile.get(cursor.id)
        if failed is None or failed[0] != limits:
            return False
        return any(n.id == failed[1] for n in cursor.walk())

    def _unfit(  # noqa: PLR0913 - why a line cannot be made, and what that means here
        self,
        cursor: Cursor,
        why: str,
        part: str,
        mode: Mode,
        limits: Limits,
        *,
        pays: bool,
    ) -> str:
        """The line cannot be made under its limit: a failure for `/compact` and at the hard limit
        (the request cannot be sent), a notice at the soft one. A notice is said once per cursor,
        limit and `part` that is too big, since one at every turn end would nag, and a different
        part is told. A summary that was paid for and did not fit is not asked for again at the
        soft limit while the limits are the same and the cursor stays on that line; the hard limit
        and `/compact` always try."""
        if mode != "soft":
            raise CompactionFailed(why)
        if pays:
            self._futile[cursor.id] = (limits, cursor.node().id)
        if (cursor.id, mode, part) not in self._warned:
            self._warned.add((cursor.id, mode, part))
            cursor.tell(f"compaction cannot keep this under its soft limit: {why}")
        return why

    async def _summarize(
        self, cursor: Cursor, focus: str, model: str | None
    ) -> tuple[str, Node] | None:
        """The summary text and its reply: a summarizer cursor at the branch's node, sent the
        prompt. `None` if the summarizer or the branch's cursor was aborted meanwhile (nothing is
        kept); `CompactionFailed` if the summary failed."""
        here = cursor.node()
        prompt = PROMPT + (FOCUS.format(focus=focus) if focus else "")
        at = here.id
        if isinstance(here.body, Body.User) and here.parent is not None:
            # The branch ends in an input not yet answered (the hard limit, on new input): the
            # prompt goes in the same message, so two user messages never sit side by side.
            at, prompt = here.parent, f"{here.body.text}\n\n{prompt}"
        if model is not None:
            at = cursor.append(at, Body.Config(model_config(model)))
        summarizer = cursor.open(at)
        self._summarizers.add(summarizer)
        try:
            cursor.set_label(self._label(cursor), summarizer)
            cursor.send(summarizer, prompt)
            try:
                ending = await cursor.rest(summarizer)
            except CursorClosed:  # someone closed it: as good as an abort
                return None
            if isinstance(ending, Ending.Aborted):
                return None
            if isinstance(ending, Ending.Failed):
                raise CompactionFailed(f"the summary failed: {ending.error}")
            if isinstance(ending, Ending.Halted):
                raise CompactionFailed(f"the summary was halted: {ending.reason}")
            reply = cursor.tree.node(cursor.cursor(summarizer).at)
            return _text(reply), reply
        finally:
            await cursor.close_cursor(summarizer)
            self._summarizers.discard(summarizer)

    def _label(self, cursor: Cursor) -> str:
        """`compaction:N`, the first N that no cursor of the session has."""
        while True:
            self._made += 1
            label = f"compaction:{self._made}"
            try:
                cursor.label(label)
            except NoSuchLabel:
                return label
