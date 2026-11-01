"""forest: the session tree is the search tree.

`/search 3 question` branches 3 cursors from the current node, one sample each, in parallel.
A judge branch (a cursor opened at each candidate, sent a judge persona) scores every candidate.
The score is a `Custom` node under the candidate, so it is persisted, replayed and sent to every
client as an `Appended`; nothing else is stored. Visits and backed-up means (MCTS) are computed
from those nodes when asked (I7: a function of the tree). The next round expands the node UCB
picks, n refinements of it, until `max_samples` would be passed. The main cursor then goes to the
best-scoring reply and the result is the explored tree.

    #5  0.20  (n=1, mean 0.20)  ans A
    #6  0.90  (n=4, mean 0.74)  ans B
      #19  0.95  (n=1, mean 0.95)  ans B, better

The judge may be searched: `judge_depth=1` rates each verdict with a smaller fan of judges.

While a search runs, its cursor shows the round (`cursor.show`): every client sees it, and it is
gone when the search ends.

A fan is `open`, `send` to each branch, `await rest` on each, `close_cursor` each once its reply
is read. Aborting the search aborts the branches in flight, so it stops spending.
"""

import asyncio
import json
import math
import re
from collections.abc import Coroutine, Iterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from tau import Body, Cursor, Ending, NodeId, Part, Stop, Tree, TreeHandle

SCORE = "forest/score"
JUDGE = (
    "You are a strict judge. Rate the assistant's last message from 0 to 1 for correctness. "
    "Reply with exactly `SCORE: <number>`."
)


@dataclass(frozen=True)
class Options:
    max_samples: Annotated[int, "no round starts that would pass this many samples"] = 12
    judges: Annotated[int, "judge samples per candidate"] = 1
    judge_depth: Annotated[int, "how many levels deep the judges are themselves judged"] = 0
    refine: Annotated[str, "what rounds after the first say to the chosen reply"] = (
        "Improve on that answer."
    )
    explore: Annotated[float, "UCB exploration constant"] = 1.0
    steer: Annotated[bool, "ask after each round whether to expand the chosen reply"] = False

    def __post_init__(self) -> None:
        if min(self.max_samples, self.judges) < 1 or self.judge_depth < 0:
            raise ValueError("max_samples and judges must be at least 1, judge_depth at least 0")


@dataclass(frozen=True)
class Scored:
    reply: int  # the candidate: the node the score hangs under
    value: float
    came_from: int | None  # the candidate this one refines; None: a first-round sample
    verdicts: tuple[int, ...]  # the judge replies (side branches of `reply`)


async def together[T](*calls: Coroutine[Any, Any, T]) -> list[T]:
    """Run `calls` at once. If one raises, the rest are cancelled (their `finally` clauses abort
    the branches they started, while the command is still live) and that error is raised."""
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(c) for c in calls]
    except BaseExceptionGroup as failed:
        raise failed.exceptions[0] from failed
    return [t.result() for t in tasks]


def below(tree: Tree, root: int) -> Iterator[int]:
    stack = [root]
    while stack:
        yield stack[-1]
        stack.extend(reversed(tree.children(stack.pop())))


def scores(tree: Tree, start: int) -> dict[int, Scored]:
    """Every score under `start`, by candidate: the whole of what a search remembers."""
    found: dict[int, Scored] = {}
    for id in below(tree, start):
        node = tree.node(id)
        if (
            isinstance(node.body, Body.Custom)
            and node.body.kind == SCORE
            and node.parent is not None
        ):
            d = json.loads(node.body.data)
            found[node.parent] = Scored(node.parent, d["value"], d["from"], tuple(d["verdicts"]))
    return found


def spent(tree: Tree, start: int) -> tuple[int, int]:
    """(samples, tokens) under `start`: every `Assistant` node, and the usage its `meta` reports."""
    samples = tokens = 0
    for id in below(tree, start):
        body = tree.node(id).body
        if isinstance(body, Body.Assistant):
            samples += 1
            usage = json.loads(body.meta or "{}").get("usage", {})
            tokens += usage.get("input", 0) + usage.get("output", 0)
    return samples, tokens


def backed_up(scored: dict[int, Scored], node: int | None) -> tuple[int, float]:
    """MCTS statistics of `node`'s subtree: scored visits, and the sum of their scores."""
    n, w = (1, scored[node].value) if node is not None else (0, 0.0)
    for s in scored.values():
        if s.came_from == node:
            kn, kw = backed_up(scored, s.reply)
            n, w = n + kn, w + kw
    return n, w


def ucb(scored: dict[int, Scored], node: int, total: int, explore: float) -> float:
    n, w = backed_up(scored, node)
    return w / n + explore * math.sqrt(math.log(total) / n)


def select(scored: dict[int, Scored], explore: float) -> int | None:
    """Descend from the start by UCB1 to the candidate to expand; `None`: the start itself."""
    node: int | None = None
    while kids := [s.reply for s in scored.values() if s.came_from == node]:
        total = max(backed_up(scored, node)[0], 1)
        node = max(kids, key=lambda k: ucb(scored, k, total, explore))
    return node


def text_of(tree: Tree, id: int) -> str:
    body = tree.node(id).body
    if not (isinstance(body, Body.Assistant) and body.stop in (Stop.End, Stop.Length)):
        raise RuntimeError(f"node {id} is not a finished reply: {body}")
    return "".join(p.text for p in body.parts if isinstance(p, Part.Text))


def render(tree: Tree, start: int) -> str:
    scored = scores(tree, start)
    lines: list[str] = []

    def show(parent: int | None, depth: int) -> None:
        for s in sorted(
            (s for s in scored.values() if s.came_from == parent), key=lambda s: s.reply
        ):
            n, w = backed_up(scored, s.reply)
            said = text_of(tree, s.reply).splitlines()[0][:40]
            lines.append(
                f"{'  ' * depth}#{s.reply}  {s.value:.2f}  (n={n}, mean {w / n:.2f})  {said}"
            )
            show(s.reply, depth + 1)

    show(None, 0)
    return "\n".join(lines)


def judge_cost(width: int, depth: int) -> int:
    return width * (1 + (judge_cost(max(1, width // 2), depth - 1) if depth else 0))


def forest_tree(tree: TreeHandle, node: NodeId) -> str:
    """Show the explored tree below a node, with scores and visits."""
    return render(tree.tree, node)


class Forest:
    name = "forest"
    options = Options

    def __init__(self) -> None:
        self.commands = (self.search, forest_tree)

    async def search(
        self, cursor: Cursor, n: Annotated[int, "candidates per round"], question: str
    ) -> str:
        """Tree-search an answer: n samples per round, a judge scores each, the best is expanded."""
        try:
            return await self._search(cursor, n, question)
        finally:
            cursor.show(None)

    async def _search(self, cursor: Cursor, n: int, question: str) -> str:
        opt, tree = cursor.options(), cursor.tree
        start = cursor.cursor(cursor.id).at
        used0 = spent(tree, start)[0]
        cost = n * (1 + judge_cost(opt.judges, opt.judge_depth))
        if cost > opt.max_samples:
            raise ValueError(f"one round costs {cost} samples, max_samples is {opt.max_samples}")
        point, say, rounds, previous = None, question, 0, ""
        while spent(tree, start)[0] - used0 + cost <= opt.max_samples:
            rounds += 1
            cursor.show(
                f"round {rounds}: sampling" + (f" (previous: {previous})" if previous else "")
            )
            replies = [
                r
                for r in await self._fan(cursor, start if point is None else point, say, n)
                if r is not None
            ]
            if not replies:
                raise RuntimeError(f"round {rounds}: every sample was aborted")
            await together(*(self._record(cursor, r, point, opt) for r in replies))
            scored = scores(tree, start)
            marks = [round(scored[r].value, 2) for r in replies]
            used = spent(tree, start)[0] - used0
            line = f"round {rounds}: {marks}, {used}/{opt.max_samples} samples"
            previous = line
            point, say = select(scored, opt.explore), opt.refine
            if (
                opt.steer
                and await cursor.ask(f"{line}. Expand #{point}?", Literal["yes", "no"]) != "yes"
            ):
                break
        best = max(scores(tree, start).values(), key=lambda s: (s.value, -s.reply))
        cursor.move(best.reply)
        samples, tokens = spent(tree, start)
        summary = (
            f"best #{best.reply} ({best.value:.2f}); {samples - used0} samples, {tokens} tokens"
        )
        return f"{render(tree, start)}\n{summary}"

    async def _record(
        self, cursor: Cursor, reply: int, came_from: int | None, opt: Options
    ) -> None:
        value, verdicts = await self._rate(cursor, reply, opt.judges, opt.judge_depth)
        data = json.dumps({"value": value, "from": came_from, "verdicts": verdicts})
        cursor.append(reply, Body.Custom(SCORE, data))

    async def _rate(
        self, cursor: Cursor, reply: int, width: int, depth: int
    ) -> tuple[float, list[int]]:
        """The judges' score of `reply`: `width` judge branches. With `depth`, each verdict is
        itself rated by half as many judges, and the best-rated verdict's score stands."""
        verdicts = [v for v in await self._fan(cursor, reply, JUDGE, width) if v is not None]
        if not verdicts:
            raise RuntimeError(f"every judge of node {reply} was aborted")
        marks = [mark(text_of(cursor.tree, v)) for v in verdicts]
        if depth:
            half = max(1, width // 2)
            weights = await together(*(self._rate(cursor, v, half, depth - 1) for v in verdicts))
            marks = [marks[max(range(len(marks)), key=lambda i: weights[i][0])]]
        return sum(marks) / len(marks), verdicts

    async def _fan(self, cursor: Cursor, at: int, say: str, n: int) -> list[int | None]:
        """Branch `n` cursors from `at`, send each `say`, wait until all rest. Each one's reply
        node; `None` for a sample a client aborted. If this is cancelled, so are the branches.
        A branch is closed once its reply is read: the reply stays in the tree, the cursor goes."""
        kids = [cursor.open(at) for _ in range(n)]
        try:
            for k in kids:
                cursor.send(k, say)
            endings = await together(*(cursor.rest(k) for k in kids))
        finally:  # an abort of the search stops the samples it started
            for k in kids:
                cursor.abort(k)
        for k, ending in zip(kids, endings, strict=True):
            if not isinstance(ending, Ending.Done | Ending.Aborted):  # `None`: not known
                raise RuntimeError(f"cursor {k} ended {ending}")
        replies = [
            None if isinstance(e, Ending.Aborted) else cursor.cursor(k).at
            for k, e in zip(kids, endings, strict=True)
        ]
        await together(*(cursor.close_cursor(k) for k in kids))
        return replies


def mark(verdict: str) -> float:
    found = re.search(r"SCORE:\s*([01](?:\.\d+)?)", verdict)
    if found is None:
        raise ValueError(f"the judge gave no score: {verdict!r}")
    return float(found.group(1))
