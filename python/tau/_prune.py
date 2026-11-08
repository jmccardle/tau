"""`Prune`: the built-in extension that shortens a conversation without a model (D24 section 9).

`/prune` rewrites the walk by the user's turns. A turn is a `User` node and everything under it
up to the next `User` node; the latest is turn N.

- Turns 0 to N-2 become a transcript: each turn is the user's text, a count of what was left
  out, and the agent's last reply. The transcript is prepended to the text of turn N-1's user
  message, in one derived `User` node, so two user messages never sit side by side.
- Turn N-1 keeps its messages and calls, with each outcome reduced to `ok` or `error` and the
  reasoning dropped.
- Turn N keeps its calls and their results, with the reasoning dropped.
- In turns N-1 and N a string argument of a call over 200 characters becomes `[pruned: N chars]`.

The result is a new line built as compaction builds one: under the end of the prefix, with a copy
of every node kept (`copied_from`) and a node computed here carrying `derived_from`; the cursor
moves onto it, and the old line stays in the tree. A node is derived only if it differs from the
one it replaces, so a conversation that has nothing to prune writes nothing.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, cast

from tau._core import Body, Node, Outcome, Part
from tau._counts import NONE, Count, ceiling_of
from tau._handles import Cursor
from tau._lines import Planned, copy_of, prefix_length, write_line
from tau._usage import show

LONG = 200
"""A string argument longer than this many characters is pruned."""
_OPEN, _CLOSE = "<pruned>\n", "\n</pruned>\n\n"


def _split(text: str) -> tuple[str | None, str]:
    """A user message as the transcript at its start (if an earlier prune put one there) and the
    user's own text."""
    if text.startswith(_OPEN) and _CLOSE in text:
        inner, rest = text[len(_OPEN) :].split(_CLOSE, 1)
        return inner, rest
    return None, text


@dataclass(frozen=True)
class Options:
    """`Prune`'s options (`extensions.prune`; `/config prune`). It has none yet: automatic
    pruning waits for a way to know the provider's cache has expired (D24 section 9)."""

    auto: Annotated[bool, "prune by itself when the prompt cache has gone cold (not built)"] = False

    def __post_init__(self) -> None:
        if self.auto:
            raise ValueError("prune.auto is not built yet: use /prune")


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _shrink(value: object) -> object:
    """`value` with every string over `LONG` characters replaced, at any depth of lists and
    objects; the same object if there is none."""
    if isinstance(value, str):
        return f"[pruned: {len(value)} chars]" if len(value) > LONG else value
    if isinstance(value, list):
        old = cast("list[object]", value)
        items = [_shrink(v) for v in old]
        return items if items != old else old
    if isinstance(value, dict):
        before = cast("dict[str, object]", value)
        fields = {k: _shrink(v) for k, v in before.items()}
        return fields if fields != before else before
    return value


def _shrunk(args_raw: str) -> str:
    """`args_raw` with every long string argument replaced; untouched text if there is none or it
    is not a JSON object."""
    try:
        args: object = json.loads(args_raw)
    except ValueError:
        return args_raw
    if not isinstance(args, dict):
        return args_raw
    shrunk = _shrink(cast("dict[str, object]", args))
    return args_raw if shrunk is args else json.dumps(shrunk, ensure_ascii=False)


def _without_reasoning(node: Node, body: Body.Assistant) -> Planned:
    """A reply with its reasoning dropped and its long arguments cut: a copy if that changes
    nothing."""
    parts: list[Part] = []
    for part in body.parts:
        match part:
            case Part.Thinking():
                continue
            case Part.Call(id=id, tool=tool, args_raw=args, replay=replay):
                parts.append(Part.Call(id, tool, _shrunk(args), replay))
            case _:
                parts.append(part)
    if parts == body.parts:
        return copy_of(node)
    return Planned(
        Body.Assistant(parts, body.stop, body.finish_reason, body.meta), derived_from=node.id
    )


def _reduced(node: Node, body: Body.Results) -> Planned:
    """A batch of results with each outcome reduced to `ok` or `error`."""
    outcomes = [(id, _reduced_outcome(outcome), by) for id, outcome, by in body.outcomes]
    if outcomes == body.outcomes:
        return copy_of(node)
    return Planned(Body.Results(outcomes), derived_from=node.id)


def _reduced_outcome(outcome: Outcome) -> Outcome:
    match outcome:
        case Outcome.Ok():
            return Outcome.Ok("ok")
        case Outcome.Err():
            return Outcome.Err("error")
        case _:
            return outcome


def _turns(rest: list[Node]) -> tuple[list[Node], list[list[Node]]]:
    """`rest` (a walk after its prefix) as the nodes before the first `User` and then the turns."""
    before: list[Node] = []
    turns: list[list[Node]] = []
    for node in rest:
        if isinstance(node.body, Body.User):
            turns.append([node])
        elif turns:
            turns[-1].append(node)
        else:
            before.append(node)
    return before, turns


def _account(turn: list[Node]) -> str:
    """One old turn in the transcript: the user's text, what was left out, the last reply."""
    user = turn[0].body
    assert isinstance(user, Body.User)
    replies = [n.body for n in turn if isinstance(n.body, Body.Assistant)]
    shown = _last_said(replies)
    calls = sum(isinstance(p, Part.Call) for reply in replies for p in reply.parts)
    left = len(replies) - (shown is not None)
    carried, text = _split(user.text)
    lines = [] if carried is None else [carried]
    lines.append(f"User: {text}")
    if left or calls:
        lines.append(f"[pruned: {_plural(left, 'message')}, {_plural(calls, 'tool call')}]")
    if shown is not None:
        lines.append(f"Assistant: {shown}")
    return "\n".join(lines)


def _last_said(replies: list[Body.Assistant]) -> str | None:
    """The words of the last reply that has any, or `None`."""
    for reply in reversed(replies):
        said = "\n".join(p.text for p in reply.parts if isinstance(p, Part.Text) and p.text)
        if said:
            return said
    return None


def prune_line(walk: list[Node]) -> list[Planned]:
    """The nodes of the pruned line for `walk`, after its prefix."""
    rest = walk[prefix_length(walk) :]
    before, turns = _turns(rest)
    planned = [copy_of(n) for n in before]
    last = len(turns) - 1
    old = turns[: max(last - 1, 0)]  # turns 0 to N-2
    for number, turn in enumerate(turns):
        if number < len(old):
            planned += [copy_of(n) for n in turn if not _message(n)]
            continue
        if old and number == len(old):
            planned.append(_with_transcript(turn[0], old))
        else:
            planned.append(copy_of(turn[0]))
        for node in turn[1:]:
            planned += _kept(node, reduced=number == last - 1)
    return planned


def _message(node: Node) -> bool:
    return isinstance(node.body, (Body.User, Body.Assistant, Body.Results))


def _kept(node: Node, *, reduced: bool) -> list[Planned]:
    """What becomes of a node of turn N-1 (`reduced`) or N below its `User` node."""
    body = node.body
    if isinstance(body, Body.Assistant):
        shown = _without_reasoning(node, body)
        if isinstance(shown.body, Body.Assistant) and not shown.body.parts:
            return []  # nothing left of it to say
        return [shown]
    if isinstance(body, Body.Results) and reduced:
        return [_reduced(node, body)]
    return [copy_of(node)]


def _with_transcript(user: Node, old: list[list[Node]]) -> Planned:
    """Turn N-1's user message with the transcript of the `old` turns before its text."""
    body = user.body
    assert isinstance(body, Body.User)
    accounts = "\n".join(_account(turn) for turn in old)
    return Planned(Body.User(f"{_OPEN}{accounts}{_CLOSE}{body.text}"), derived_from=user.id)


class Prune:
    """The built-in extension `prune` (the module docstring)."""

    name = "prune"
    options = Options

    def __init__(self) -> None:
        self.commands = (self.prune,)

    async def prune(self, cursor: Cursor) -> str:
        """Shorten this conversation without a model: old turns become a transcript and old
        tool output goes, on a new line (the old one stays in the tree)."""
        cursor.options()  # a bad option fails the command
        walk = cursor.walk()
        planned = prune_line(walk)
        if not any(p.derived_from is not None for p in planned):
            return "nothing to prune"
        kept = prefix_length(walk)
        before = _ceilings(n.body for n in walk)
        after = _ceilings(p.body for p in planned) + _ceilings(n.body for n in walk[:kept])
        if after.upper >= before.upper:
            return "nothing to prune: it would not be shorter"
        last = write_line(cursor, walk[kept - 1].id, planned)[-1]
        cursor.move(last)
        return (
            f"pruned {show(before)} → {show(after)} tokens "
            f"(node {last}; the old line is still in the tree)"
        )


def _ceilings(bodies: Iterable[Body]) -> Count:
    """The ceilings of `bodies` added up: both sides of a comparison of lines that are not sent
    yet are counted the same way, so one that is shorter by its bytes is shown shorter."""
    total = NONE
    for body in bodies:
        total += ceiling_of(body)
    return total
