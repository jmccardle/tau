"""Counts (D26 sections 1 and 4): what a number of tokens knows about where it came from.

A **count** is `Count(tokens, basis, bound)`; its `upper` is `tokens + bound`. The
basis is one of five, strongest first: `reported` (the server's own bill, or the difference
between two bills whose shared prefix is proved), `rendered`, `calibrated`, `heuristic` (the last
three belong to a counter extension, D26 T2 and later) and `ceiling` (no estimate: the bound is
the node's UTF-8 bytes plus `CEILING_PER_NODE`). Counts add: tokens and bounds add, and the sum
is as weak as its weakest part. Nothing in tau estimates tokens from characters.

A **request count** is what the walk to a node will cost as a request: a `total`, a `base` (the
walk's first nodes and the request's overhead, counted as one) and one count for each node that
can start a kept tail. A **counter** is an object an extension offers as its `counter`
attribute; it counts only what no report covers.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from tau._config import ModelConfig
from tau._core import Author, Body, Node, Outcome, Part


class Basis(StrEnum):
    """Where a `Count` came from, strongest first. A sum is as weak as its weakest part."""

    REPORTED = "reported"
    RENDERED = "rendered"
    CALIBRATED = "calibrated"
    HEURISTIC = "heuristic"
    CEILING = "ceiling"


_STRENGTH = {p: i for i, p in enumerate(Basis)}

CEILING_PER_NODE = 16
"""Tokens a ceiling adds for each node a request sends, on top of one token per byte: the chat
template's special tokens and the per-message overhead. The assumption (D26 section 4) is that
every token of a byte-level tokenizer covers at least one byte."""


@dataclass(frozen=True)
class Count:
    """A number of tokens and what is known about it. `bound` is how many more it may be."""

    tokens: int
    basis: Basis
    bound: int = 0

    def __post_init__(self) -> None:
        if self.tokens < 0 or self.bound < 0:
            raise ValueError(f"a count is not negative: {self.tokens} + {self.bound}")

    @property
    def upper(self) -> int:
        """The most it can be: `tokens + bound`."""
        return self.tokens + self.bound

    def __add__(self, other: "Count") -> "Count":
        weakest = max(self.basis, other.basis, key=_STRENGTH.__getitem__)
        return Count(self.tokens + other.tokens, weakest, self.bound + other.bound)


NONE = Count(0, Basis.REPORTED)
"""Nothing: the count of a node that is not sent."""


def reported(tokens: int, bound: int = 0) -> Count:
    """A `reported` count."""
    return Count(tokens, Basis.REPORTED, bound)


def sent(body: Body) -> bool:
    """Whether a request carries a node with this body (`Config` and `Custom` are never sent)."""
    return isinstance(body, (Body.System, Body.User, Body.Assistant, Body.Results))


def ceiling_of(body: Body) -> Count:
    """The ceiling of a node: its UTF-8 bytes (text, reasoning, replay data, call ids, names and
    arguments, outcomes) plus `CEILING_PER_NODE`, as the bound of a count of 0 tokens. A node that
    is not sent is `NONE`."""
    if not sent(body):
        return NONE
    return Count(0, Basis.CEILING, _bytes(body) + CEILING_PER_NODE)


LOWEST_BYTES_PER_TOKEN = 8
"""A run of at most 4 of one character, or any other text, costs at least its bytes over 8 tokens
(`lowest_tokens`): real text is 3 to 5 bytes per token."""
_RUN = re.compile(r"(.)\1{3,}", re.DOTALL)


def lowest_tokens(body: Body) -> int:
    """The least a node plausibly costs: its text with every run of one repeated character cut to
    four (the tokenizers have single tokens for long runs of spaces, dashes or equals signs, which
    no ratio of bytes covers), over `LOWEST_BYTES_PER_TOKEN`. A bound for taking a node out of a
    bill, not a count: no tokenizer is asked, and a text that is long runs of something else
    (`ab` over and over) may be cheaper."""
    match body:
        case Body.System(text=text) | Body.User(text=text):
            texts = [text]
        case Body.Assistant(parts=parts):
            texts = [
                p.text if isinstance(p, Part.Text | Part.Thinking) else p.args_raw
                for p in parts
                if isinstance(p, Part.Text | Part.Thinking | Part.Call)
            ]
        case Body.Results(outcomes=outcomes):
            texts = [_outcome_text(o) for _, o, _ in outcomes]
        case _:
            return 0
    squeezed = "".join(_RUN.sub(lambda m: m.group(1) * 4, t) for t in texts)
    return len(squeezed.encode()) // LOWEST_BYTES_PER_TOKEN


def _outcome_text(outcome: Outcome) -> str:
    match outcome:
        case Outcome.Ok(value=value):
            return value
        case Outcome.Err(error=error):
            return error
        case _:
            return ""


def wrapped_count(reply: Count, reply_body: Body, node_body: Body) -> Count:
    """The count of a node that is a reply's words in a wrapper (a summary): the count of the
    reply's words (`Reading.said`: its thinking is not in the node) and the ceiling of the words
    added around them."""
    words = (
        sum(len(p.text.encode()) for p in reply_body.parts if isinstance(p, Part.Text))
        if isinstance(reply_body, Body.Assistant)
        else _bytes(reply_body)
    )
    wrapper = max(_bytes(node_body) - words, 0)
    return reply + Count(0, Basis.CEILING, wrapper + CEILING_PER_NODE)


def _bytes(body: Body) -> int:
    match body:
        case Body.System(text=text) | Body.User(text=text):
            return len(text.encode())
        case Body.Assistant(parts=parts):
            return sum(_part_bytes(p) for p in parts)
        case Body.Results(outcomes=outcomes):
            return sum(len(call.encode()) + _outcome_bytes(o) for call, o, _ in outcomes)
        case _:
            return 0


def _part_bytes(part: Part) -> int:
    match part:
        case Part.Text() | Part.Thinking():
            own = len(part.text.encode())
        case Part.Call():
            own = len(part.id.encode()) + len(part.tool.encode()) + len(part.args_raw.encode())
        case _:
            own = 0
    replay = getattr(part, "replay", None)
    return own + (0 if replay is None else len(replay.data.encode()))


def _outcome_bytes(outcome: Outcome) -> int:
    match outcome:
        case Outcome.Ok(value=value):
            return len(value.encode())
        case Outcome.Err(error=error):
            return len(error.encode())
        case _:
            return len("Interrupted")


class _Described(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def parameters(self) -> Mapping[str, object]: ...


def tools_ceiling(tools: Iterable[_Described]) -> Count:
    """The ceiling of the tool definitions a request carries (name, description and schema of
    each), which the first bill of a line includes and a line with no bill yet does not."""
    size = sum(
        len(t.name.encode()) + len(t.description.encode()) + len(json.dumps(t.parameters).encode())
        for t in tools
    )
    return Count(0, Basis.CEILING, size) if size else NONE


def tools_hash(tools: Iterable[_Described]) -> str:
    """A digest of a tool set: the name, description and schema of each tool, whatever their
    order. Two requests with the same digest carry the same tool definitions."""
    listed = sorted(
        [t.name, t.description, json.dumps(t.parameters, sort_keys=True)] for t in tools
    )
    return hashlib.sha256(json.dumps(listed).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class RequestCount:
    """What the walk to a node will cost as a request (`Cursor.count`).

    `total` is the whole request. `base` counts the walk's first `base_nodes` nodes and the
    request's overhead (tool definitions, hidden prompts, per-message overhead) as one; it is
    `NONE` with `base_nodes` 0 while no reply on the walk has reported its usage. `nodes` has one
    count for each node that is sent, by node id, except a node inside a **lump**: where two
    requests prove that the nodes between them cost a known sum, the first node holds the sum and
    the others have no count of their own, so a cut never lands inside one. `why` says why the
    total is weaker than it could be, or is `None`."""

    total: Count
    base: Count
    base_nodes: int
    nodes: Mapping[int, Count]
    why: str | None = None


SpanProof = Literal["cache", "slot", "prefix"]
"""How a span is proved: `cache` (the second request read exactly the first from the server's
cache), `slot` (llama.cpp's slot held at least the whole first request) or `prefix` (the
provider found the first request to be a byte prefix of the second when it sent it)."""


@dataclass(frozen=True)
class Span:
    """A proved span (D26 section 3): `nodes` are the nodes added between two consecutive
    requests on one walk, from the first request's reply up to the node before the second's
    reply; `cost` is what they cost together, the second bill's `input` less the first's, which
    is exact because the first request is proved to be a prefix of the second; `proof` says
    how."""

    nodes: tuple[Node, ...]
    cost: int
    proof: SpanProof


@dataclass(frozen=True)
class CountJob:
    """What a counter is asked: the walk to count, the model (its qualified name, and
    `resolved`, its settings in the catalog now) and tools of the request, and what core already
    knows (`base`, `base_nodes`, and in `known` the nodes a report covers). The counter answers
    for the rest, `unreported`."""

    walk: tuple[Node, ...]
    model: str
    resolved: ModelConfig
    tools: tuple[str, ...]
    base: Count
    base_nodes: int
    known: Mapping[int, Count]
    unreported: tuple[int, ...]


@dataclass(frozen=True)
class CounterAnswer:
    """A counter's answer: a count for some of the job's `unreported` nodes, and `why` the rest
    stay as weak as they are (or `None`)."""

    nodes: Mapping[int, Count]
    why: str | None = None


@dataclass(frozen=True)
class Observation:
    """One reply that reported its usage, as the driver shows it to the counter once the reply
    is in the tree (D26 section 4). `kind` is `reply` (`count_call` is D26 section 8's, not
    built). `model` is the qualified name of the model that replied and `resolved` its settings
    in the catalog now; `walk` the ids of the nodes from the root to the reply; `usage` the
    reply's normalized `meta.usage`; `markers` its drift markers by name (the served `model`,
    `system_fingerprint`, and llama.cpp's `build_info`, `template_sha256`, `model_size`,
    `n_params` and `n_vocab`, each as text, those the reply has); `tools` the digest of the tool
    set its request offered (`tools_hash`); `span` the proved span that ends at this reply, or
    `None` if the reply proved none; `time` the reply's time in milliseconds."""

    kind: Literal["reply", "count_call"]
    model: str
    resolved: ModelConfig
    walk: tuple[int, ...]
    usage: Mapping[str, int]
    markers: Mapping[str, str]
    tools: str
    span: Span | None
    time: int


class Counter(Protocol):
    """What an extension offers as its `counter` attribute (at most one extension may). Both
    methods are synchronous and do no network IO: the driver runs them in a thread. Core keeps
    every report: an answer for a node a report covers fails the count with a
    `DefinitionError`."""

    def count(self, job: CountJob) -> CounterAnswer:
        """Count the nodes of `job.unreported`."""
        ...

    def observe(self, observation: Observation) -> None:
        """Learn from one reply. The driver calls it after every reply that reports its usage,
        before the reply's `post_reply` hooks; a failure is told to the clients and costs the
        step nothing."""
        ...


class Counting(Protocol):
    """What a handle counts through: the driver's `Counts`."""

    @property
    def counter(self) -> str | None:
        """The key of the extension offering the counter, or `None`."""
        ...

    async def count(self, at: int) -> RequestCount:
        """The request count of the walk to node `at`."""
        ...

    def spans(self, at: int) -> list[Span]:
        """The proved spans of the walk to node `at`."""
        ...

    def observation(self, reply: int, tools: str) -> Observation | None:
        """What a counter is shown of the reply at node `reply`, or `None` if it reported no
        usage."""
        ...


# -- what a reply reports, and how counts are written

REASONING_BOUND = 8
"""How far a reply's count may be off once its thinking is subtracted (D26 section 3.2)."""
_KILO, _MEGA, _GIGA = 1000, 1_000_000, 1_000_000_000


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
