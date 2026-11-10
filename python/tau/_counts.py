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

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from tau._core import Body, Node, Outcome, Part


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
    """What a counter is asked: the walk to count, the model and tools of the request, and what
    core already knows (`base`, `base_nodes`, and in `known` the nodes a report covers). The
    counter answers for the rest, `unreported`."""

    walk: tuple[Node, ...]
    model: str
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


class Counter(Protocol):
    """What an extension offers as its `counter` attribute (at most one extension may). `count`
    is synchronous and does no network IO: the driver runs it in a thread. Core keeps every
    report: an answer for a node a report covers fails the count with a `DefinitionError`."""

    def count(self, job: CountJob) -> CounterAnswer:
        """Count the nodes of `job.unreported`."""
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
