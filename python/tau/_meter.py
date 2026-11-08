"""The meter (D26 section 4): core's answer to "what will this walk cost?", with no counter.

It reads the reports in the walk and nothing else. The walk to a node is the request that
produced the replies on it, so a reply's reported `input` is the bill for the nodes before it.
For the replies that report (the **reports**, in walk order):

- **The base** is the first report's `input`: the nodes before that reply, and the request's
  overhead. It over-counts the prefix and the overhead, in the safe direction. Less the nodes
  among them that a report already counts exactly (a compaction's copies, below), which have
  their own counts, so a line that was compacted does not carry its tail in its base.
- **A span** is the nodes from one report's reply up to the next report's. Its cost is the
  difference of the two `input`s, and that is exact only if the first request is a prefix of the
  second as the server sees it. It is **proved** when the second reports `cached` equal to the
  first's `input`: the server found the whole first request in its cache. A proved span is one
  **lump**: its first node holds the cost and the others have no count of their own.
- **The newest reply** costs its `output`.
- **Everything else** (nodes after the newest report, nodes in a span that is not proved, every
  node while there is no report) is a **ceiling** (`tau._counts.ceiling_of`).
- **A copy** takes its original's count from the original's own walk, when the copy holds the
  whole lump; so the first request after a compaction is mostly exact.

The total is the newest report's `input` plus its reply's `output`, plus the ceilings of the
nodes after it. With no report the total is the ceilings of the whole walk.
"""

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from itertools import pairwise

from tau._core import Node
from tau._counts import (
    NONE,
    Count,
    Counter,
    CountJob,
    Provenance,
    RequestCount,
    ceiling_of,
    reported,
    sent,
)
from tau._usage import Reading, counted, reading
from tau._values import DefinitionError


@dataclass(frozen=True)
class Measured:
    """A request count and what the meter keeps to answer further questions about it: the walk,
    `anchor` (the newest report's `input` and its reply's `output`; `NONE` with no report),
    `after` (the nodes with a count after the newest report, which add to the anchor) and `lumps`
    (the node ids each lump of more than one node covers, by its first)."""

    request: RequestCount
    walk: tuple[Node, ...]
    anchor: Count
    after: tuple[int, ...]
    lumps: Mapping[int, tuple[int, ...]]


def _reports(walk: list[Node]) -> list[tuple[int, Reading]]:
    """The replies of `walk` that report their usage, by index. A reply whose usage cannot be
    read is a reply with none."""
    found: list[tuple[int, Reading]] = []
    for i, node in enumerate(walk):
        if not counted(node):
            continue
        try:
            usage = reading(node)
        except ValueError:
            continue
        if usage is not None:
            found.append((i, usage))
    return found


def _proved(walk: list[Node], first: tuple[int, Reading], second: tuple[int, Reading]) -> bool:
    """Whether the span from one report to the next is proved (the module docstring). Another
    reply between them, reporting or not, breaks the chain of requests."""
    (i, before), (j, after) = first, second
    return (
        before.input > 0
        and after.cached == before.input
        and not any(counted(walk[k]) for k in range(i + 1, j))
    )


class Meter:
    """Measures the walk to a node, from `node` (a node by id); kept per node, since nodes never
    change."""

    def __init__(self, node: Callable[[int], Node]) -> None:
        self._node = node
        self._kept: dict[int, Measured] = {}

    def walk(self, at: int) -> list[Node]:
        """The nodes from the root to `at`."""
        found: list[Node] = []
        node: Node | None = self._node(at)
        while node is not None:
            found.append(node)
            node = None if node.parent is None else self._node(node.parent)
        found.reverse()
        return found

    def measure(self, at: int) -> Measured:
        """The walk to `at` measured."""
        if at not in self._kept:
            self._kept[at] = self._measure(self.walk(at))
        return self._kept[at]

    def _measure(self, walk: list[Node]) -> Measured:
        nodes: dict[int, Count] = {}
        lumps: dict[int, tuple[int, ...]] = {}
        marks = _reports(walk)
        if not marks:
            self._fill(walk, 0, len(walk), nodes, lumps)
            after = tuple(n.id for n in walk if n.id in nodes)
            total = _sum(nodes[i] for i in after)
            request = RequestCount(total, NONE, 0, nodes)
            return Measured(request, tuple(walk), NONE, after, lumps)
        first, last = marks[0][0], marks[-1][0]
        self._fill(walk, 0, first, nodes, lumps)
        for earlier, later in pairwise(marks):
            (i, before), (j, then) = earlier, later
            if _proved(walk, earlier, later):
                ids = tuple(n.id for n in walk[i:j])
                nodes[ids[0]] = reported(then.input - before.input)
                if len(ids) > 1:
                    lumps[ids[0]] = ids
            else:
                nodes[walk[i].id] = reported(before.output)
                self._fill(walk, i + 1, j, nodes, lumps)
        newest = marks[-1][1]
        nodes[walk[last].id] = reported(newest.output)
        self._fill(walk, last + 1, len(walk), nodes, lumps)
        anchor = reported(newest.input) + reported(newest.output)
        after = tuple(n.id for n in walk[last + 1 :] if n.id in nodes)
        total = anchor + _sum(nodes[i] for i in after)
        # the first bill less the nodes before it that a report already counts exactly (copies of
        # a compaction's tail): what is left is the prefix, the overhead and the nodes only
        # their bytes bound
        counted = sum(
            c.tokens
            for n in walk[:first]
            if (c := nodes.get(n.id)) and c.provenance is Provenance.REPORTED
        )
        base = reported(max(marks[0][1].input - counted, 0))
        request = RequestCount(total, base, first, nodes)
        return Measured(request, tuple(walk), anchor, after, lumps)

    def _fill(
        self,
        walk: list[Node],
        lo: int,
        hi: int,
        nodes: dict[int, Count],
        lumps: dict[int, tuple[int, ...]],
    ) -> None:
        """Count `walk[lo:hi]` with no report: a ceiling for each sent node, or for a copy its
        original's count."""
        j = lo
        while j < hi:
            node = walk[j]
            if node.copied_from is None:
                if sent(node.body):
                    nodes[node.id] = ceiling_of(node.body)
                j += 1
                continue
            end = j
            while end + 1 < hi and walk[end + 1].copied_from is not None:
                end += 1
            self._inherit(walk[j : end + 1], nodes, lumps)
            j = end + 1

    def _inherit(
        self, run: list[Node], nodes: dict[int, Count], lumps: dict[int, tuple[int, ...]]
    ) -> None:
        """Count a run of copies from the walk of the original of its last: a copy holding a
        whole lump takes the lump's count, any other a ceiling."""
        origins = [n.copied_from for n in run]
        last = origins[-1]
        assert last is not None
        original = self.measure(last)
        k = 0
        while k < len(run):
            node, origin = run[k], origins[k]
            assert origin is not None
            ids = original.lumps.get(origin)
            if ids is not None and tuple(origins[k : k + len(ids)]) == ids:
                nodes[node.id] = original.request.nodes[origin]
                lumps[node.id] = tuple(n.id for n in run[k : k + len(ids)])
                k += len(ids)
                continue
            if ids is None and origin in original.request.nodes:
                nodes[node.id] = original.request.nodes[origin]
            elif sent(node.body):
                nodes[node.id] = ceiling_of(node.body)
            k += 1


def _sum(counts: Iterable[Count]) -> Count:
    total = NONE
    for count in counts:
        total += count
    return total


class Counts:
    """What the driver counts through: the meter, and the counter if an extension offers one.
    `node` reads a node, `model` the model of the branch at a node, `tools` the base offer, and
    `counter` the extension offering the counter, as `(key, counter)`."""

    def __init__(
        self,
        node: Callable[[int], Node],
        model: Callable[[int], str],
        tools: Callable[[], tuple[str, ...]],
        counter: Callable[[], tuple[str, Counter] | None],
    ) -> None:
        self._meter = Meter(node)
        self._model = model
        self._tools = tools
        self._counter = counter

    @property
    def counter(self) -> str | None:
        """The key of the extension offering the counter, or `None`."""
        found = self._counter()
        return None if found is None else found[0]

    async def count(self, at: int) -> RequestCount:
        """The request count of the walk to `at`: the meter's, with a counter's answer for the
        nodes no report covers. `DefinitionError` naming the extension if the counter counts a
        node a report covers or one it was not asked about."""
        measured = self._meter.measure(at)
        found = self._counter()
        if found is None:
            return measured.request
        key, counter = found
        request = measured.request
        unreported = tuple(
            i for i, c in request.nodes.items() if c.provenance is not Provenance.REPORTED
        )
        job = CountJob(
            measured.walk,
            self._model(at),
            self._tools(),
            request.base,
            request.base_nodes,
            {i: c for i, c in request.nodes.items() if c.provenance is Provenance.REPORTED},
            unreported,
        )
        answer = await asyncio.to_thread(counter.count, job)
        stray = [i for i in answer.nodes if i not in unreported]
        if stray:
            raise DefinitionError(
                f"extension {key!r} counted node {stray[0]}, which a report covers or which "
                "is not in the request"
            )
        merged = {**request.nodes, **answer.nodes}
        total = measured.anchor + _sum(merged[i] for i in measured.after)
        return replace(request, total=total, nodes=merged, why=answer.why or request.why)
