"""`Compaction`: the built-in extension that summarizes a long branch (D24 section 2).

When a branch nears its model's context window, the next sample is preceded by a summary: the
older part of the branch (the **head**) becomes one `User` node, and the newest part (the
**tail**, about `keep` tokens) is copied. They hang from the end of the prefix as a new line, and
the cursor moves onto it. The old line stays in the tree. `/compact [focus]` does it now, with
text the summary should keep in mind.

The summary is written by a **summarizer cursor**: a cursor opened at the branch's node, sent
the summary prompt, and waited for. It shows in the tree and its reply is counted by `usage`; it
is closed afterwards. It is offered the branch's tools, which keeps the prompt cache warm, and
refused any call it makes (`pre_tool`). Compaction's own `pre_sample` skips those cursors.

The trigger is proactive only: a provider's "prompt too long" is not caught. `/compact`, then a
step, recovers.
"""

from dataclasses import dataclass
from typing import Annotated

from tau._config import ConfigError, model_config
from tau._core import Body, Ending, Node, NoSuchLabel, Part, Stop
from tau._handles import Cursor, NodeHandle, Subject
from tau._lines import (
    Planned,
    characters,
    copy_of,
    prefix_length,
    tokens_of,
    write_line,
)
from tau._protocols import CallDraft
from tau._usage import counted, reading, tokens
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

    auto: Annotated[bool, "compact before a sample that would not fit"] = True
    reserve: Annotated[
        int | None,
        "tokens kept free for the reply and for the summary itself (unset: 16384, or a quarter "
        "of the window if less)",
    ] = None
    hard: Annotated[
        int | None,
        "estimate above which a turn is interrupted and compacted (unset: window - reserve)",
    ] = None
    soft: Annotated[
        int | None, "estimate above which the turn's end compacts (unset: 80% of the hard limit)"
    ] = None
    keep: Annotated[
        int | None, "tokens of the newest nodes copied verbatim (unset: 20000, or half the room)"
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


class CompactionFailed(RuntimeError):
    """The summary could not be written; nothing was written to the tree."""


def estimate(walk: list[Node]) -> int:
    """The tokens `walk` will cost as a request. The newest reply that reported its usage and is
    not a copy gives an exact count (its input and output); each node after it adds its
    characters divided by 4. With no such reply the whole walk is counted by characters. An
    estimate by design, never treated as exact."""
    for i in range(len(walk) - 1, -1, -1):
        node = walk[i]
        if not counted(node):
            continue
        try:
            usage = reading(node)
        except ValueError:
            continue  # a reply whose usage cannot be read is a reply with none
        if usage is not None:
            return usage.input + usage.output + _estimate(walk[i + 1 :])
    return _estimate(walk)


def _estimate(nodes: list[Node]) -> int:
    return tokens_of(sum(characters(n.body) for n in nodes))


def tail_start(body: list[Node], keep: int) -> int:
    """Where the tail of `body` (a walk after its prefix) starts: walking back from the end until
    `keep` tokens are reached, then back to the nearest `User` or `Assistant` node, so a
    `Results` node stays with its batch. 0 means everything is tail."""
    held = 0
    i = len(body)
    while i > 0 and tokens_of(held) < keep:
        i -= 1
        held += characters(body[i].body)
    while i > 0 and not isinstance(body[i].body, (Body.User, Body.Assistant)):
        i -= 1
    return i


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


def _check_fits(
    walk: list[Node], kept: int, planned: list[Planned], room: int | None
) -> tuple[int, int]:
    """The tokens before and after, both counted by characters so neither is biased.
    `CompactionFailed` unless the planned line is shorter and under `room`."""
    before = _estimate(walk)
    after = _estimate(walk[:kept]) + sum(tokens_of(characters(p.body)) for p in planned)
    over = room is not None and after >= room
    if after >= before or over:
        raise CompactionFailed(
            f"compacting would not make it shorter than {tokens(before)} tokens"
            + (f" or fit {tokens(room)}" if over and room is not None else "")
            + f" (the new line is {tokens(after)}); nothing was written"
        )
    return before, after


def _crowded(
    walk: list[Node], kept: int, tail: list[Node], keep: int, room: int | None
) -> str | None:
    """Why the prefix and the tail alone already reach `room`, naming the part that is too big
    (`None`: they leave room). The newest node by itself, or the prefix, is not `keep`'s doing."""
    if room is None:
        return None
    prefix = _estimate(walk[:kept])
    newest = _estimate(walk[-1:])
    if prefix >= room:
        return (
            f"the prefix (system prompt, context files, skills) takes {prefix} of the {room} "
            "tokens the model has room for"
        )
    if newest >= room:
        return (
            f"the newest node alone takes {newest} tokens, over the {room} the model has room for"
        )
    if prefix + _estimate(tail) >= room:
        return (
            f"the prefix and the newest nodes kept (keep = {keep}) take {prefix + _estimate(tail)} "
            f"of the {room} tokens the model has room for; lower keep"
        )
    return None


def _limits(cursor: Cursor, options: Options) -> tuple[int | None, int | None, int]:
    """The soft and hard limits of the cursor's model (both `None`: it has no `context_window`)
    and the `keep` to use. Hard is `hard`, else the window less `reserve`; soft is `soft`, else
    80% of hard; `keep` is the one set, else `DEFAULT_KEEP` or half the hard limit if that is
    less; `reserve`, when unset, is `DEFAULT_RESERVE` or a quarter of the window if that is less.
    `ConfigError` if a limit leaves no room, a `soft` set is above the hard limit, or a
    `keep` set does not fit under the soft limit."""
    model = cursor.model()
    window = cursor.resolved_model(model).context_window
    if window is None:
        return None, None, DEFAULT_KEEP if options.keep is None else options.keep
    reserve = min(DEFAULT_RESERVE, window // 4) if options.reserve is None else options.reserve
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
        return soft, hard, min(DEFAULT_KEEP, soft // 2)
    if options.keep >= soft:
        raise ConfigError(
            f"{model}: keep ({options.keep}) does not fit under the soft limit ({soft}); "
            "lower keep, or raise soft, hard or the window"
        )
    return soft, hard, options.keep


class Compaction:
    """The built-in extension `compaction` (the module docstring)."""

    name = "compaction"
    options = Options

    def __init__(self) -> None:
        self._summarizers: set[int] = set()  # the cursors opened here, until they are closed
        self._replied: dict[int, int] = {}  # per cursor: the reply its running step committed
        self._warned: set[int] = set()  # cursors told their soft limit cannot be met
        self._made = 0
        self.commands = (self.compact,)

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> None:
        """The hard limit: compact first, interrupting the turn, if the branch would not fit the
        model's window."""
        if cursor.id in self._summarizers or not cursor.options().auto:
            return
        _, hard, _ = _limits(cursor, cursor.options())
        if hard is None:
            cursor.show(f"compaction: {cursor.model()} has no context_window")
        elif estimate(cursor.walk()) > hard:
            await self._compact(cursor, "")

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
        soft, _, _ = _limits(cursor, cursor.options())
        if soft is not None and estimate(cursor.walk()) > soft:
            await self._compact(cursor, "", soft=True)

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        """Refuse any call a summarizer makes."""
        return "summarizing: no tools" if node.cursor_id in self._summarizers else None

    async def compact(self, cursor: Cursor, focus: str = "") -> str:
        """Summarize this conversation now, on a new line (the old one stays in the tree).
        `focus` is text the summary should keep in mind."""
        return await self._compact(cursor, focus)

    async def _compact(self, cursor: Cursor, focus: str, *, soft: bool = False) -> str:
        """Compact `cursor`'s branch; the notice (or why nothing was written). `soft`: it was the
        soft limit that called, so the line must end under that one."""
        options = cursor.options()
        walk = cursor.walk()
        kept = prefix_length(walk)
        body = walk[kept:]
        soft_limit, hard_limit, keep = _limits(cursor, options)
        room = soft_limit if soft else hard_limit
        start = tail_start(body, keep)
        head, tail = body[:start], body[start:]
        if not any(isinstance(n.body, (Body.User, Body.Assistant, Body.Results)) for n in head):
            return "nothing to compact yet"
        crowded = _crowded(walk, kept, tail, keep, room)
        if crowded is not None:
            return self._crowded_out(cursor, crowded, soft=soft)
        cursor.show("compacting…")
        try:
            summary = await self._summarize(cursor, focus, options.model)
        finally:
            cursor.show(None)
        if summary is None:
            return ABORTED
        planned = [copy_of(n) for n in head if isinstance(n.body, Body.Config)]
        planned.append(Planned(Body.User(SUMMARY.format(text=summary)), derived_from=head[-1].id))
        summary_at = len(planned) - 1
        planned += [copy_of(n) for n in tail]
        before, after = _check_fits(walk, kept, planned, room)
        ids = write_line(cursor, walk[kept - 1].id, planned)
        cursor.move(ids[-1])
        text = (
            f"compacted {tokens(before)} → {tokens(after)} tokens "
            f"(nodes {head[0].id}–{head[-1].id} summarized as node {ids[summary_at]})"  # noqa: RUF001
        )
        cursor.tell(text)
        return text

    def _crowded_out(self, cursor: Cursor, crowded: str, *, soft: bool) -> str:
        """The prefix and tail alone reach the limit: a failure at the hard limit, a notice (said
        once per cursor, since a failure at every turn end would nag) at the soft one."""
        if not soft:
            raise CompactionFailed(crowded)
        if cursor.id not in self._warned:
            self._warned.add(cursor.id)
            cursor.tell(f"compaction cannot keep this under its soft limit: {crowded}")
        return crowded

    async def _summarize(self, cursor: Cursor, focus: str, model: str | None) -> str | None:
        """The summary text: a summarizer cursor at the branch's node, sent the prompt. `None`
        if the summarizer or the branch's cursor was aborted meanwhile (nothing is kept);
        `CompactionFailed` if the summary failed."""
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
            ending = await cursor.rest(summarizer)
            if isinstance(ending, Ending.Aborted):
                return None
            if isinstance(ending, Ending.Failed):
                raise CompactionFailed(f"the summary failed: {ending.error}")
            if isinstance(ending, Ending.Halted):
                raise CompactionFailed(f"the summary was halted: {ending.reason}")
            return _text(cursor.tree.node(cursor.cursor(summarizer).at))
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
