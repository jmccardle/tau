"""Counting the tokens a text will actually cost, and saying how we know.

Three routes to a number, in descending order of authority:

1. **The provider's own report.** ``Usage`` on an assistant message is the billed
   truth for everything up to that point. Nothing here improves on it.
2. **A local tokenizer** (:class:`TokenizerCounter`) — the model's real
   ``tokenizer.json``. Exact for message *text*; it knows nothing about the chat
   template wrapped around that text on the wire.
3. **Character classes** (:class:`CharClassCounter`) — a linear model over
   lower/upper/digit/space/punct/non-ASCII character counts plus a word count. An
   estimate, and labelled one, for when no tokenizer is available.

Whichever route produced a number, it comes back as a :class:`TokenCount` that
says which one, so a caller can tell a measurement from a guess. That is the
point of the type: the old ``len(text) // 4`` returned a bare ``int`` and a bare
``int`` cannot admit it was invented.

:class:`ContextCalibrator` closes the gap between routes 2/3 and route 1. It
watches ``(messages, payload, billed)`` triples go by and solves
``billed ~= a*messages + r*payload`` online, so the per-message chat-template
cost ``a`` and the payload scale error ``r`` are learned from the endpoint rather
than assumed about it.

Reference: docs/TOKEN-ACCOUNTING.md
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from tau_llm.docs import agent_facing

TokenSource = Literal["usage", "tokenizer", "classes", "calibrated", "none"]
"""Where a token count came from. ``"none"`` is the empty count, which is exact."""

CLASS_FEATURES = ("lower", "upper", "digit", "space", "punct", "nonascii", "words")
"""Feature order for every coefficient tuple in :data:`CLASS_COEFFICIENTS`."""

CLASS_COEFFICIENTS: dict[str, tuple[float, ...]] = {
    "assistant": (0.12064, 0.28097, 1.23453, 0.16176, 0.69643, 0.00000, 0.36154),
    "toolResult": (0.13230, 0.30830, 1.17999, 0.10643, 0.56286, 0.45418, 0.41080),
    "default": (0.11271, 0.23059, 1.08750, 0.07359, 0.66719, 0.34771, 0.51954),
}
"""Cold-start weights, non-negative least squares over 2198 real τ messages.

Qwen3's tokenizer, τ's own session logs, fit on 41 sessions and held out on 18:
77.1% of single messages within 10%, 95.0% over a 32-message span. These are a
labelled prior, not a claim about any other tokenizer — a digit costing ~17× a
space is the durable part, the fifth decimal place is not. ``user`` had 126
training messages, too few to fit on its own, and takes ``default``.

Reference: docs/TOKEN-ACCOUNTING.md §"Where the coefficients came from"
"""

CALIBRATION_MIN_OBSERVATIONS = 3
"""Observations before :class:`ContextCalibrator` will solve its two parameters."""

CALIBRATION_SCALE_BOUNDS = (0.8, 1.5)
"""Admissible range for the payload multiplier ``r``; outside it the fit is junk."""


@agent_facing(topic="messages")
@dataclass(frozen=True)
class TokenCount:
    """A token count that carries its own provenance.

    Attributes:
        tokens: The count itself. Never negative.
        source: Which route produced it — see :data:`TokenSource`.
        exact: True only when the number is the real token count for what was
            counted. A tokenizer is exact about text; it is not exact about a
            request, because the request carries framing the tokenizer never saw.
        includes_template: Whether chat-template framing (role headers, turn
            delimiters, the tool schemas) is inside ``tokens``. A count that omits
            it is an under-count of the request, which is the dangerous direction.
    """

    tokens: int
    source: TokenSource
    exact: bool
    includes_template: bool

    def __post_init__(self) -> None:
        if self.tokens < 0:
            raise ValueError(f"TokenCount.tokens must be >= 0, got {self.tokens}")

    def __int__(self) -> int:
        return self.tokens

    def __add__(self, other: TokenCount) -> TokenCount:
        """Sum two counts, degrading provenance to the weaker of the two.

        Adding an exact count to an estimated one gives an estimate; adding a
        template-inclusive count to a bare one gives a count that only partly
        includes the template, which this reports as not including it. Both
        degradations round toward "trust it less".
        """
        if not isinstance(other, TokenCount):
            return NotImplemented
        if self.tokens == 0 and self.source == "none":
            return other
        if other.tokens == 0 and other.source == "none":
            return self
        source: TokenSource = self.source if self.source == other.source else "calibrated"
        return TokenCount(
            tokens=self.tokens + other.tokens,
            source=source,
            exact=self.exact and other.exact,
            includes_template=self.includes_template and other.includes_template,
        )


ZERO_TOKENS = TokenCount(tokens=0, source="none", exact=True, includes_template=True)
"""The empty count. Exactly zero tokens, and that is not an estimate."""


@agent_facing(topic="messages")
@runtime_checkable
class TextCounter(Protocol):
    """Anything that can say how many tokens a string costs.

    Implementations: :class:`TokenizerCounter` (exact) and
    :class:`CharClassCounter` (estimated). A caller that needs to know which it
    holds reads :attr:`source` rather than testing the concrete type.
    """

    @property
    def source(self) -> TokenSource:
        """Which route this counter is."""
        ...

    @property
    def exact(self) -> bool:
        """Whether :meth:`count` returns the real token count for its input."""
        ...

    def count(self, text: str, *, role: str | None = None) -> TokenCount:
        """Tokens for ``text``, optionally hinted by the message ``role``."""
        ...


@agent_facing(topic="messages")
class TokenizerCounter:
    """Exact text counts from the model's own tokenizer.

    Wraps a HuggingFace ``tokenizers.Tokenizer``. The ``tokenizers`` package is
    an optional extra (``pip install 'ffwf-tau-llm[tokenizers]'``); constructing
    this class without it raises rather than silently falling back to an estimate,
    because a caller asking for exactness and getting a guess is the failure this
    module exists to end.

    ``exact`` is True and ``includes_template`` is False: the tokenizer encodes
    the text it is given, and the chat template that wraps that text on the wire
    is not in it. :class:`ContextCalibrator` is what learns the difference.
    """

    def __init__(self, tokenizer: Any, *, name: str = "<tokenizer>") -> None:
        if not hasattr(tokenizer, "encode"):
            raise TypeError(f"tokenizer must have .encode(); got {type(tokenizer).__name__}")
        self._tokenizer = tokenizer
        self._name = name

    @property
    def name(self) -> str:
        """Where this tokenizer came from, for error messages and display."""
        return self._name

    @property
    def source(self) -> TokenSource:
        return "tokenizer"

    @property
    def exact(self) -> bool:
        return True

    @classmethod
    def from_file(cls, path: str) -> TokenizerCounter:
        """Load a ``tokenizer.json`` off disk.

        Raises:
            TokenizerUnavailable: the ``tokenizers`` package is not installed.
            OSError: the file does not exist or does not parse.
        """
        tokenizers = _require_tokenizers()
        return cls(tokenizers.Tokenizer.from_file(path), name=path)

    @classmethod
    def from_pretrained(cls, repo_id: str) -> TokenizerCounter:
        """Fetch a tokenizer from the HuggingFace hub by repository id.

        Network-dependent and uncached by this class — the caller owns the cache,
        because where a downloaded file lands is a policy question this module has
        no business answering.

        Raises:
            TokenizerUnavailable: the ``tokenizers`` package is not installed.
            Exception: whatever the hub call raises, unchanged.
        """
        tokenizers = _require_tokenizers()
        return cls(tokenizers.Tokenizer.from_pretrained(repo_id), name=repo_id)

    def count(self, text: str, *, role: str | None = None) -> TokenCount:
        """Exact token count for ``text``. ``role`` is accepted and ignored."""
        if not text:
            return ZERO_TOKENS
        n = len(self._tokenizer.encode(text, add_special_tokens=False).ids)
        return TokenCount(tokens=n, source="tokenizer", exact=True, includes_template=False)


@agent_facing(topic="messages")
class TokenizerUnavailable(RuntimeError):
    """Raised when an exact count was asked for and no tokenizer can supply it."""


def _require_tokenizers() -> Any:
    try:
        import tokenizers
    except ImportError as exc:
        raise TokenizerUnavailable(
            "exact token counting needs the 'tokenizers' package: "
            "pip install 'ffwf-tau-llm[tokenizers]'"
        ) from exc
    return tokenizers


def _classify(ch: str) -> int:
    if ord(ch) > 127:
        return 5
    if ch.islower():
        return 0
    if ch.isupper():
        return 1
    if ch.isdigit():
        return 2
    if ch.isspace():
        return 3
    return 4


_CLASS_OF: dict[str, int] = {}


def character_classes(text: str) -> tuple[int, int, int, int, int, int]:
    """Counts of (lower, upper, digit, space, punct, non-ASCII) characters.

    Counts distinct characters once with :class:`collections.Counter` and
    classifies each distinct character, rather than classifying every position:
    a transcript has hundreds of distinct characters and hundreds of thousands of
    positions.
    """
    totals = [0, 0, 0, 0, 0, 0]
    for ch, n in Counter(text).items():
        k = _CLASS_OF.get(ch)
        if k is None:
            k = _CLASS_OF[ch] = _classify(ch)
        totals[k] += n
    return (totals[0], totals[1], totals[2], totals[3], totals[4], totals[5])


@agent_facing(topic="messages")
class CharClassCounter:
    """Estimated token counts from character composition, with no tokenizer.

    A linear model over the six character classes plus a word count, per role.
    It exists because one chars-per-token ratio cannot hold both prose and code:
    in the shipped fit a digit costs 1.23 tokens and a space costs 0.16, and a
    ratio that splits the difference is wrong about both.

    ``exact`` is False and stays False. A caller wanting exactness constructs a
    :class:`TokenizerCounter` and handles :class:`TokenizerUnavailable`.
    """

    def __init__(self, coefficients: dict[str, tuple[float, ...]] | None = None) -> None:
        table = dict(CLASS_COEFFICIENTS if coefficients is None else coefficients)
        if "default" not in table:
            raise ValueError("coefficients must contain a 'default' row")
        for role, row in table.items():
            if len(row) != len(CLASS_FEATURES):
                raise ValueError(
                    f"coefficients[{role!r}] has {len(row)} weights, "
                    f"expected {len(CLASS_FEATURES)} ({', '.join(CLASS_FEATURES)})"
                )
            if any(w < 0 for w in row):
                raise ValueError(
                    f"coefficients[{role!r}] has a negative weight: {row}. A character "
                    "class cannot reduce a token count, so a negative weight makes the "
                    "estimate non-monotone in its own input."
                )
        self._coefficients = table

    @property
    def source(self) -> TokenSource:
        return "classes"

    @property
    def exact(self) -> bool:
        return False

    def count(self, text: str, *, role: str | None = None) -> TokenCount:
        """Estimated token count for ``text`` under ``role``'s weights."""
        if not text:
            return ZERO_TOKENS
        w = self._coefficients.get(role or "", self._coefficients["default"])
        lo, up, di, sp, pu, na = character_classes(text)
        total = (
            w[0] * lo
            + w[1] * up
            + w[2] * di
            + w[3] * sp
            + w[4] * pu
            + w[5] * na
            + w[6] * len(text.split())
        )
        return TokenCount(
            tokens=max(1, math.ceil(total)),
            source="classes",
            exact=False,
            includes_template=False,
        )


@agent_facing(topic="messages")
@dataclass
class CalibrationState:
    """The five running sums and the observation count a calibrator carries.

    Serializable so a session can resume a fit instead of paying the three-turn
    warm-up again. Sums, not samples: the fit is a 2x2 normal-equation solve, so
    the whole history compresses to this regardless of how many turns fed it.
    """

    nn: float = 0.0
    npay: float = 0.0
    pp: float = 0.0
    nr: float = 0.0
    pr: float = 0.0
    count: int = 0

    def to_dict(self) -> dict[str, float | int]:
        """Plain-dict form for persistence."""
        return {
            "nn": self.nn,
            "npay": self.npay,
            "pp": self.pp,
            "nr": self.nr,
            "pr": self.pr,
            "count": self.count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CalibrationState:
        """Rebuild from :meth:`to_dict`. Missing keys are a hard error."""
        missing = {"nn", "npay", "pp", "nr", "pr", "count"} - set(data)
        if missing:
            raise ValueError(f"CalibrationState is missing {sorted(missing)}")
        return cls(
            nn=float(data["nn"]),
            npay=float(data["npay"]),
            pp=float(data["pp"]),
            nr=float(data["nr"]),
            pr=float(data["pr"]),
            count=int(data["count"]),
        )


@agent_facing(topic="messages")
class ContextCalibrator:
    """Learns what an endpoint charges beyond the text, from what it charged.

    Solves ``billed ~= a*messages + r*payload`` by least squares over five running
    sums, where ``payload`` is whatever a :class:`TextCounter` said the messages
    cost. ``a`` absorbs per-message chat-template framing and the amortized system
    prompt and tool schemas; ``r`` absorbs the payload's scale error — tokenizer
    drift, or the class model's fit.

    Both are constrained to physically admissible values: ``a >= 0`` because
    framing cannot refund tokens, and ``r`` inside
    :data:`CALIBRATION_SCALE_BOUNDS` because a payload that needs doubling means
    the payload is measuring something else. An unconstrained solve on real
    traffic produced ``-85 tokens/message`` on one model, which is how the bounds
    got here.

    One calibrator per (model, session): the fit is about an endpoint's framing,
    and the system prompt it amortizes is a property of the session.
    """

    def __init__(self, state: CalibrationState | None = None) -> None:
        self._state = state or CalibrationState()

    @property
    def state(self) -> CalibrationState:
        """The running sums, for persistence."""
        return self._state

    @property
    def observations(self) -> int:
        """How many turns have been observed."""
        return self._state.count

    @property
    def ready(self) -> bool:
        """Whether a solve is possible. False during the warm-up."""
        return self.solve() is not None

    def observe(self, *, messages: int, payload_tokens: int, billed_tokens: int) -> None:
        """Record one turn: ``messages`` messages, ``payload_tokens`` counted, billed.

        Args:
            messages: How many messages were in the request.
            payload_tokens: What the text counter said those messages cost.
            billed_tokens: What the provider said the request's input cost.

        Raises:
            ValueError: on a non-positive message count or a negative token count.
                A turn the caller cannot describe must not silently become a
                zero-weighted observation that drags the fit.
        """
        if messages <= 0:
            raise ValueError(f"messages must be > 0, got {messages}")
        if payload_tokens < 0 or billed_tokens < 0:
            raise ValueError(
                f"token counts must be >= 0, got payload={payload_tokens} billed={billed_tokens}"
            )
        s = self._state
        n, p, r = float(messages), float(payload_tokens), float(billed_tokens)
        s.nn += n * n
        s.npay += n * p
        s.pp += p * p
        s.nr += n * r
        s.pr += p * r
        s.count += 1

    def solve(self) -> tuple[float, float] | None:
        """The fitted ``(a, r)``, or None while the fit is unusable.

        None means one of: fewer than :data:`CALIBRATION_MIN_OBSERVATIONS` turns
        seen, a singular system (every observed turn had the same shape), or a
        multiplier outside :data:`CALIBRATION_SCALE_BOUNDS`. A caller that gets
        None uses the uncalibrated payload and says so.
        """
        s = self._state
        if s.count < CALIBRATION_MIN_OBSERVATIONS:
            return None
        det = s.nn * s.pp - s.npay * s.npay
        if abs(det) < 1e-9 * max(1.0, s.nn * s.pp):
            return None
        a = (s.nr * s.pp - s.pr * s.npay) / det
        r = (s.pr * s.nn - s.nr * s.npay) / det
        if not (math.isfinite(a) and math.isfinite(r)):
            return None
        if a < 0:
            a, r = 0.0, (s.pr / s.pp if s.pp > 0 else 0.0)
        lo, hi = CALIBRATION_SCALE_BOUNDS
        if not (lo <= r <= hi):
            return None
        return a, r

    def predict(self, *, messages: int, payload: TokenCount) -> TokenCount:
        """What the endpoint will bill for ``messages`` messages costing ``payload``.

        Returns ``payload`` unchanged, with its own provenance, when the fit is not
        ready — an uncalibrated number that says it is uncalibrated, rather than a
        calibrated-looking number with nothing behind it.
        """
        if messages < 0:
            raise ValueError(f"messages must be >= 0, got {messages}")
        solved = self.solve()
        if solved is None or messages == 0:
            return payload
        a, r = solved
        return TokenCount(
            tokens=max(0, math.ceil(a * messages + r * payload.tokens)),
            source="calibrated",
            exact=False,
            includes_template=True,
        )


_COUNTER_CACHE: dict[str, TextCounter] = {}


@agent_facing(topic="messages")
def counter_for(model: Any, *, fallback: bool = False) -> TextCounter:
    """The best text counter available for ``model``.

    Reads ``model.tokenizer`` — a ``tokenizer.json`` path or a HuggingFace repo
    id. Unset means no tokenizer was configured and the caller gets a
    :class:`CharClassCounter`.

    Args:
        model: Any object with an optional ``tokenizer`` attribute; a
            :class:`~tau_llm.types.Model` in practice.
        fallback: What to do when ``model.tokenizer`` IS set but will not load.
            False (the default) re-raises: the operator named a tokenizer, so
            silently substituting an estimate would hide a typo in a path behind
            a plausible number. True downgrades to the class counter, for a
            display that must render something rather than crash a UI thread.

    Raises:
        TokenizerUnavailable: ``model.tokenizer`` is set, ``fallback`` is False,
            and the ``tokenizers`` package is missing.
        OSError: ``model.tokenizer`` is set, ``fallback`` is False, and the file
            or repo id does not resolve.
    """
    spec = getattr(model, "tokenizer", None)
    if not spec:
        return CharClassCounter()
    cached = _COUNTER_CACHE.get(spec)
    if cached is not None:
        return cached
    try:
        counter: TextCounter = (
            TokenizerCounter.from_file(spec)
            if spec.endswith(".json")
            else TokenizerCounter.from_pretrained(spec)
        )
    except Exception:
        if not fallback:
            raise
        return CharClassCounter()
    _COUNTER_CACHE[spec] = counter
    return counter


def clear_counter_cache() -> None:
    """Drop every memoized tokenizer. For tests, and for a config reload."""
    _COUNTER_CACHE.clear()
