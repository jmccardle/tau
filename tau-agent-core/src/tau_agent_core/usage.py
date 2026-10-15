"""Token-usage arithmetic for completions that happen OUTSIDE the agent loop.

The agent loop attaches a provider-reported ``usage`` dict to the ``message_end``
event it emits once per completion, and every consumer (the TUI's cost meter, the
headless ``done`` record) sums those events. That accounting is sound for the
loop — and blind to everything else.

τ makes real LLM calls that are not turns:

* **compaction** summarizes the whole conversation, so its INPUT is roughly a full
  context window. It is one of the priciest single calls in a session, and it fires
  AUTOMATICALLY, without the user asking for it.
* **branch summaries** (the tree browser's "Summarize" modes).
* **every** ``ctx.complete()`` — and the constrained fan-out that motivated it
  spends N of them per invocation.

All three go through ``tau_llm.complete_simple``, which takes no event bus and emits
nothing, so none of their tokens could ever reach the meter. The displayed cost was
not merely incomplete, it was *understated* — the direction that lets a session look
cheaper than it is. A cost readout that silently omits the most expensive automatic
call in the system is worse than no readout, because it is believed.

So the completions that spend tokens off-loop report what they spent, and the
session keeps a ledger the meter reads. These helpers are the shared arithmetic,
and :func:`price_usage` is the one place a usage dict becomes dollars
(docs/PRICING-AND-COMPACTION-CONFIG.md §2).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tau_llm.docs import agent_facing

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
)


def zero_usage() -> dict[str, int]:
    """A usage dict with every field at 0 — a true zero, not a missing reading."""
    return dict.fromkeys(USAGE_FIELDS, 0)


def usage_of(message: Any) -> dict[str, int]:
    """The usage an ``AssistantMessage`` reports, as a plain dict.

    A message whose provider reported nothing yields a true zero rather than a
    guess: Fail-Early forbids the ``len(text) // 4`` style approximation that
    fabricates a count which *looks* real (the same fabrication the TUI's own
    usage path was already purged of).
    """
    usage = getattr(message, "usage", None)
    if usage is None:
        return zero_usage()
    if not isinstance(usage, dict):
        usage = usage.model_dump()
    return {field: int(usage.get(field, 0) or 0) for field in USAGE_FIELDS}


def add_usage(into: dict[str, int], *others: dict[str, int]) -> dict[str, int]:
    """Sum usage dicts field-wise, returning a new dict (``into`` is not mutated)."""
    total = dict(into)
    for other in others:
        for field in USAGE_FIELDS:
            total[field] = total.get(field, 0) + int(other.get(field, 0) or 0)
    return total


PRICED_BUCKETS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "input"),
    ("output_tokens", "output"),
    ("cache_read_tokens", "cache_read"),
    ("cache_write_tokens", "cache_write"),
)

_WIRE_SPELLINGS = ("prompt_tokens", "completion_tokens")


@agent_facing(topic="messages")
def price_usage(cost: Mapping[str, Any] | None, usage: Mapping[str, Any]) -> float | None:
    """Dollar cost of ``usage`` under the price block ``cost``, or ``None`` when unpriced.

    ``sum(cost[p] / 1e6 * usage[b])`` over the four :data:`PRICED_BUCKETS`. Every
    provider partitions the prompt into ``input_tokens``, ``cache_read_tokens``
    and ``cache_write_tokens``, so each is priced once and none overlaps.

    Args:
        cost: A config model entry's ``{input, output, cache_read, cache_write}``
            block, USD per 1M tokens. A missing price key prices that bucket at 0.
        usage: τ usage names (``input_tokens``, ``output_tokens``,
            ``cache_read_tokens``, ``cache_write_tokens``); a missing bucket is 0
            and any other key is ignored.

    Returns:
        ``None`` when ``cost`` is ``None`` — unknown, never a fabricated ``$0``;
        otherwise a float, ``0.0`` for a present all-zero block.

    Raises:
        ValueError: ``usage`` uses the OpenAI wire spelling (``prompt_tokens`` /
            ``completion_tokens``) without τ's, which would price as zero.
    """
    wire = [k for k in _WIRE_SPELLINGS if k in usage]
    if wire and not any(bucket in usage for bucket, _ in PRICED_BUCKETS):
        raise ValueError(
            f"price_usage takes τ usage names (input_tokens, output_tokens, ...); got "
            f"{wire}, the OpenAI wire spelling, which would price every bucket at 0"
        )
    if cost is None:
        return None
    return float(
        sum(
            float(cost.get(price, 0.0)) / 1_000_000 * int(usage.get(bucket, 0) or 0)
            for bucket, price in PRICED_BUCKETS
        )
    )
