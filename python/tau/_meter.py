"""The meter (D26 section 4): core's answer to "what will this walk cost?", with no counter.

It reads the reports in the walk and nothing else. The walk to a node is the request that
produced the replies on it, so a reply's reported `input` is the bill for the nodes before it.
For the replies that report (the **reports**, in walk order):

- **The base** is the prefix and the request's overhead (tool definitions, hidden prompts,
  per-message overhead), counted as one: the first report's `input` less the body nodes before
  that reply (the first message; a compacted line's summary and copies). A lump among them comes
  out at its cost; any other node comes out at the lowest its bytes allow
  (`lowest_tokens`), and a base made that way is `HEURISTIC`. With no report the base is
  the ceiling of the tool definitions.
- **A span** is the nodes from one report's reply up to the next report's. Its cost is the
  difference of the two `input`s, and that is exact only if the first request is a prefix of the
  second as the server sees it. It is **proved** in one of three ways (`Span.proof`): `cache`,
  the second reports `cached` equal to the first's `input` (the server found the whole first
  request in its cache: Anthropic); `slot`, llama.cpp's `timings` say the second request read at
  least the whole first request from its slot (`cache_n` of the second is no less than
  `prompt_n + cache_n` of the first); `prefix`, the second reply's `meta.sent.extends` names the
  first reply (the provider found the first request to be a byte prefix of the second when it
  sent it: OpenAI and Gemini). A proved span is one **lump**: its first node holds the cost and
  the others have no count of their own.
- **The newest reply** costs its `output`.
- **Everything else** (nodes after the newest report, nodes in a span that is not proved, every
  node while there is no report) is a **ceiling** (`tau._counts.ceiling_of`).
- **A copy** takes its original's count from the original's own walk, when the copy holds the
  whole lump; so the first request after a compaction is mostly exact.

The total is the newest report's `input` plus its reply's `output`, plus the ceilings of the
nodes after it. With no report the total is the ceilings of the whole walk and of the tool
definitions.
"""

import asyncio
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import cast

from tau._config import ConfigError, ModelConfig
from tau._core import Body, Node
from tau._counts import (
    NONE,
    Basis,
    Count,
    Counter,
    CountJob,
    Observation,
    RequestCount,
    Span,
    SpanProof,
    ceiling_of,
    lowest_tokens,
    reported,
    sent,
    wrapped_count,
)
from tau._lines import prefix_length
from tau._usage import Reading, counted, reading
from tau._values import DefinitionError

KEPT = 16
"""How many measured walks a `Meter` keeps."""


@dataclass(frozen=True)
class Measured:
    """A request count and what the meter keeps to answer further questions about it: `anchor`
    (the newest report's `input` and its reply's `output`; `NONE` with no report), `after` (the
    nodes with a count after the newest report, which add to the anchor), `lumps` (the node
    ids each lump of more than one node covers, by its first) and `spans` (the spans the
    walk's own reports prove, oldest first)."""

    request: RequestCount
    anchor: Count
    after: tuple[int, ...]
    lumps: Mapping[int, tuple[int, ...]]
    spans: tuple[Span, ...] = ()


@dataclass(frozen=True)
class _Report:
    """A reply that reports its usage: where it is on the walk, its `usage`, and what else its
    `meta` says that can prove a span: `extends` (`meta.sent.extends`) and `slot` (llama.cpp's
    `timings.prompt_n` and `timings.cache_n`)."""

    at: int
    usage: Reading
    extends: int | None = None
    slot: tuple[int, int] | None = None


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


MARKERS = ("model", "system_fingerprint")
SERVER_MARKERS = ("build_info", "template_sha256", "model_size", "n_params", "n_vocab")


def _markers(meta: Mapping[str, object]) -> dict[str, str]:
    """A reply's drift markers (D26 section 2), by name, each as text: what `meta` says about
    which model and server answered. A window (`n_ctx`) is not one: it changes no count."""
    server = meta.get("server")
    held: Mapping[str, object] = (
        cast("dict[str, object]", server) if isinstance(server, dict) else {}
    )
    found: dict[str, object] = {k: meta.get(k) for k in MARKERS}
    found.update({k: held.get(k) for k in SERVER_MARKERS})
    return {k: str(v) for k, v in found.items() if isinstance(v, str | int) and v != ""}


def _report(at: int, node: Node, usage: Reading) -> _Report:
    assert isinstance(node.body, Body.Assistant)
    assert node.body.meta is not None
    meta = cast("dict[str, object]", json.loads(node.body.meta))
    sent, timings = meta.get("sent"), meta.get("timings")
    extends = cast("dict[str, object]", sent).get("extends") if isinstance(sent, dict) else None
    slot = None
    if isinstance(timings, dict):
        fields = cast("dict[str, object]", timings)
        fresh, read = fields.get("prompt_n"), fields.get("cache_n")
        if _is_count(fresh) and _is_count(read):
            slot = (cast("int", fresh), cast("int", read))
    return _Report(at, usage, cast("int", extends) if _is_count(extends) else None, slot)


def _reports(walk: list[Node]) -> list[_Report]:
    """The replies of `walk` that report their usage, in walk order. A reply whose usage cannot
    be read is a reply with none."""
    found: list[_Report] = []
    for i, node in enumerate(walk):
        if not counted(node):
            continue
        try:
            usage = reading(node)
        except ValueError:
            continue
        if usage is not None:
            found.append(_report(i, node, usage))
    return found


def _base(
    walk: list[Node],
    billed: int,
    first: int,
    nodes: Mapping[int, Count],
    lumps: Mapping[int, tuple[int, ...]],
) -> Count:
    """What the first bill holds besides the prefix: its overhead and the prefix are the base.
    The body nodes before the first reply (the first message, or a compacted line's summary and
    copies) are taken out of the bill: exactly where they make a whole lump (their count is a
    difference of two bills), and otherwise by the lowest their bytes allow
    (`lowest_tokens`), so the base is never less than the prefix and the overhead and
    is a bound on them. A base with a node taken out that way is `HEURISTIC`."""
    out, bounded = 0, False
    for node in walk[prefix_length(walk) : first]:
        count = nodes.get(node.id)
        if count is None:
            continue  # inside a lump, whose first node holds the sum
        if node.id in lumps:
            out += count.tokens
        else:
            out += lowest_tokens(node.body)
            bounded = True
    left = max(billed - out, 0)
    return Count(left, Basis.HEURISTIC if bounded else Basis.REPORTED)


def _proof(walk: list[Node], first: _Report, second: _Report) -> SpanProof | None:
    """How the span from one report to the next is proved (the module docstring), or `None` if it
    is not. Another reply between them, reporting or not, breaks the chain of requests, and a
    second bill smaller than the first is no span at all."""
    before, after = first.usage, second.usage
    if (
        before.input <= 0
        or after.input < before.input
        or any(counted(walk[k]) for k in range(first.at + 1, second.at))
    ):
        return None
    if after.cached == before.input:
        return "cache"
    if first.slot is not None and second.slot is not None and second.slot[1] >= sum(first.slot):
        return "slot"
    if second.extends == walk[first.at].id:
        return "prefix"
    return None


class Meter:
    """Measures the walk to a node, from `node` (a node by id). The last `KEPT` measures are kept
    (nodes never change, so a kept one is always right); a long-lived server asks about a new
    tip every turn, and an older one is measured again if it is asked for."""

    def __init__(self, node: Callable[[int], Node]) -> None:
        self._node = node
        self._kept: OrderedDict[int, Measured] = OrderedDict()

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
        if at in self._kept:
            self._kept.move_to_end(at)
            return self._kept[at]
        measured = self._measure(self.walk(at))
        self._kept[at] = measured
        while len(self._kept) > KEPT:
            self._kept.popitem(last=False)
        return measured

    def _measure(self, walk: list[Node]) -> Measured:
        nodes: dict[int, Count] = {}
        lumps: dict[int, tuple[int, ...]] = {}
        marks = _reports(walk)
        if not marks:
            self._fill(walk, 0, len(walk), nodes, lumps)
            base, covered = self._carried(walk)
            after = tuple(n.id for n in walk[covered:] if n.id in nodes)
            total = base + _sum(nodes[i] for i in after)
            return Measured(RequestCount(total, base, covered, nodes), base, after, lumps)
        first, last = marks[0].at, marks[-1].at
        spans: list[Span] = []
        self._fill(walk, 0, first, nodes, lumps)
        for earlier, later in pairwise(marks):
            i, j = earlier.at, later.at
            proof = _proof(walk, earlier, later)
            if proof is not None:
                ids = tuple(n.id for n in walk[i:j])
                cost = later.usage.input - earlier.usage.input
                nodes[ids[0]] = reported(cost)
                spans.append(Span(tuple(walk[i:j]), cost, proof))
                if len(ids) > 1:
                    lumps[ids[0]] = ids
            else:
                nodes[walk[i].id] = reported(earlier.usage.output)
                self._fill(walk, i + 1, j, nodes, lumps)
        newest = marks[-1].usage
        nodes[walk[last].id] = reported(newest.output)
        self._fill(walk, last + 1, len(walk), nodes, lumps)
        anchor = reported(newest.input) + reported(newest.output)
        after = tuple(n.id for n in walk[last + 1 :] if n.id in nodes)
        total = anchor + _sum(nodes[i] for i in after)
        base = _base(walk, marks[0].usage.input, first, nodes, lumps)
        request = RequestCount(total, base, first, nodes)
        return Measured(request, anchor, after, lumps, tuple(spans))

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
                    nodes[node.id] = self._derived(node)
                j += 1
                continue
            end = j
            while end + 1 < hi and walk[end + 1].copied_from is not None:
                end += 1
            self._inherit(walk[j : end + 1], nodes, lumps)
            j = end + 1

    def _carried(self, walk: list[Node]) -> tuple[Count, int]:
        """The base of a line no reply has reported on yet, and how many nodes it covers: the base
        of the line it was written from (the prefix and the overhead are the same), found through
        the first node derived from another, or `NONE` and 0 if there is none or that line had
        no report either."""
        prefix = prefix_length(walk)
        for node in walk[prefix:]:
            if node.derived_from is not None:
                old = self.measure(node.derived_from).request
                return (old.base, prefix) if old.base_nodes else (NONE, 0)
        return NONE, 0

    def _derived(self, node: Node) -> Count:
        """The count of a node nothing reports: a ceiling, except a `User` node derived from a
        reply that reported (a summary, derived from the summarizer's reply), which is what
        that reply's words cost without its thinking (`Reading.said`) and the ceiling of its
        wrapper."""
        if node.derived_from is not None and isinstance(node.body, Body.User):
            source = self._node(node.derived_from)
            usage = _usage_of(source)
            if usage is not None:
                return wrapped_count(usage.said, source.body, node.body)
        return ceiling_of(node.body)

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


def _usage_of(node: Node) -> Reading | None:
    """The usage a counted reply reports, or `None` (not a counted reply, none, unreadable)."""
    if not counted(node):
        return None
    try:
        return reading(node)
    except ValueError:
        return None


def _sum(counts: Iterable[Count]) -> Count:
    total = NONE
    for count in counts:
        total += count
    return total


class Counts:
    """What the driver counts through: the meter, and the counter if an extension offers one.
    `node` reads a node, `model` the model of the branch at a node, `tools` the base offer, and
    `counter` the extension offering the counter, as `(key, counter)`, and `overhead` the ceiling
    of what a request carries besides its nodes (the tool definitions), which a line with no
    report yet has to add itself."""

    def __init__(  # noqa: PLR0913, PLR0917 - one reader for each thing a count needs
        self,
        node: Callable[[int], Node],
        model: Callable[[int], str],
        resolved: Callable[[str], ModelConfig],
        tools: Callable[[], tuple[str, ...]],
        counter: Callable[[], tuple[str, Counter] | None],
        overhead: Callable[[], Count],
    ) -> None:
        self._meter = Meter(node)
        self._node = node
        self._overhead = overhead
        self._model = model
        self._resolved = resolved
        self._tools = tools
        self._counter = counter

    @property
    def counter(self) -> str | None:
        """The key of the extension offering the counter, or `None`."""
        found = self._counter()
        return None if found is None else found[0]

    def _with_overhead(self, measured: Measured) -> Measured:
        """`measured`, with the overhead added if no report has billed it yet."""
        if measured.request.base_nodes:
            return measured
        overhead = self._overhead()
        request = replace(measured.request, base=overhead, total=measured.request.total + overhead)
        return replace(measured, request=request, anchor=overhead)

    def spans(self, at: int) -> list[Span]:
        """The proved spans of the walk to `at`, oldest first."""
        return list(self._meter.measure(at).spans)

    def observation(self, reply: int, tools: str) -> Observation | None:
        """What a counter is shown of the reply at node `reply`, whose request offered the tool
        set `tools` (its digest): `None` for a node that is not a reply that counts, one that
        reported no usage that can be read, or one whose model the catalog no longer has."""
        node = self._node(reply)
        usage = _usage_of(node)
        if usage is None:
            return None
        assert isinstance(node.body, Body.Assistant)
        assert node.body.meta is not None
        meta = cast("dict[str, object]", json.loads(node.body.meta))
        model = self._model(reply)
        try:
            resolved = self._resolved(model)
        except ConfigError:
            return None
        spans = self._meter.measure(reply).spans
        newest = spans[-1] if spans and spans[-1].nodes[-1].id == node.parent else None
        return Observation(
            "reply",
            model,
            resolved,
            tuple(n.id for n in self._meter.walk(reply)),
            cast("dict[str, int]", meta["usage"]),
            _markers(meta),
            tools,
            newest,
            node.time,
        )

    def observing[T](
        self,
        reply: int,
        tools: str,
        then: Callable[[], Awaitable[T]],
        tell: Callable[[str], None],
    ) -> Callable[[], Awaitable[T]]:
        """`then`, after the counter (if an extension offers one) has observed the reply at node
        `reply` (D26 section 4): the driver's `observe` call, made before the reply's
        `post_reply` hooks. A counter that fails is told to the clients with `tell`; the reply is
        already in the tree, and the step goes on."""

        async def effect() -> T:
            found = self._counter()
            observation = None if found is None else self.observation(reply, tools)
            if found is not None and observation is not None:
                key, counter = found
                try:
                    await asyncio.to_thread(counter.observe, observation)
                except Exception as e:
                    tell(f"extension {key!r} could not observe a reply: {type(e).__name__}: {e}")
            return await then()

        return effect

    async def count(self, at: int) -> RequestCount:
        """The request count of the walk to `at`: the meter's, with a counter's answer for the
        nodes no report covers. `DefinitionError` naming the extension if the counter counts a
        node a report covers or one it was not asked about."""
        measured = self._with_overhead(self._meter.measure(at))
        found = self._counter()
        if found is None:
            return measured.request
        key, counter = found
        request = measured.request
        unreported = tuple(i for i, c in request.nodes.items() if c.basis is not Basis.REPORTED)
        model = self._model(at)
        job = CountJob(
            tuple(self._meter.walk(at)),
            model,
            self._resolved(model),
            self._tools(),
            request.base,
            request.base_nodes,
            {i: c for i, c in request.nodes.items() if c.basis is Basis.REPORTED},
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
