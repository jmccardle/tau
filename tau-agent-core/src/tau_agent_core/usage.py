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
session keeps a ledger the meter reads. These helpers are the shared arithmetic.
"""

from __future__ import annotations

from typing import Any

# The Usage fields τ carries end to end (tau_llm.types.Usage). ``total_tokens`` is
# provider-reported and NOT recomputed from the others: providers differ on whether
# cache reads count toward the input total, and second-guessing them here would
# fabricate a number that disagrees with the bill.
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
