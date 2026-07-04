"""Example 24: Budget — a running-cost guard that warns then aborts (E2 + cost).

A stateful extension that watches token usage as it *lands* (via the notify
``message_end`` event) and, once the run's cumulative spend crosses a configured
ceiling, injects a one-shot ``<system-reminder>`` warning into the next LLM
payload (via the ``context`` hook) and then calls ``ctx.abort()`` — stopping the
agent loop at the next turn boundary. It is the pi "budget guard" (plan D1): the
warn-then-abort needs the ``context`` hook, so it lands **after** E2.

## Two threshold modes (Fail-Early — never a fabricated ``$0``)

The guard thresholds on whichever unit it can *actually* measure, mirroring the
E4.cost rule that an absent price block yields tokens-only, never a made-up
dollar figure:

* **USD mode** — a per-model ``cost`` block is supplied (``{input, output,
  cache_read}`` in USD per 1M tokens, the same shape ``config.json`` carries and
  ``backends.compute_cost_usd`` prices). The running total is real dollars and the
  ceiling is ``max_usd``.
* **token mode** — no ``cost`` block is known, so pricing the run would require
  inventing numbers. The guard instead sums raw tokens and the ceiling is
  ``max_tokens``.

Exactly one mode is active; :class:`BudgetGuard` raises if the wrong threshold is
paired with the presence/absence of a ``cost`` block (Fail-Early: no silent
degradation to a fictional price).

## How the cost is computed

``completion_cost_usd`` is the collapsed pi ``calculateCost`` (``models.ts:39-48``;
τ ``backends.compute_cost_usd``): ``sum(price[k] / 1e6 * tokens[k])`` over the
priced buckets. ``cache_write`` is inert against today's provider (the bucket is
never populated), so it is left out of the sum — a real 0, not an omission.

## Accumulation vs. the threshold check — the timing

Usage lands *after* a completion (the per-completion ``message_end`` in
``_stream_response`` carries ``message["usage"]``), while the ``context`` hook
fires *before* every LLM call. So the guard accumulates on ``message_end`` and, on
the **next** ``context`` call, sees the freshly-added spend and can trip. When it
trips it injects the warning into *that* turn's payload (so the model is told why
it is being cut off) and aborts; the loop's per-turn ``is_aborted()`` check then
breaks before the following turn — the warning is delivered on the wire *before*
the abort takes hold. The trip is one-shot (``_tripped``) so a same-turn re-entry
neither double-injects nor double-aborts.

## Field contract

The ``message_end`` handler receives a notify ``AgentEvent`` (single argument);
the per-completion usage is ``event.message["usage"]`` — a plain dict with
``input_tokens`` / ``output_tokens`` / ``cache_read_tokens`` /
``cache_write_tokens``. The duplicate tool-turn ``message_end`` that ``run()``
emits has no ``usage`` key and so contributes nothing (no double-counting). The
``context`` handler receives ``(event, ctx)`` and aborts via ``ctx.abort()`` — the
live per-prompt abort signal the session binds onto the context.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.budget import make_budget_extension  # loaded via importlib in tests

session = create_agent_session(
    model="gpt-4o",
    tools=["read", "write", "edit", "bash"],
    extensions=[
        make_budget_extension(
            cost={"input": 3.0, "output": 15.0, "cache_read": 0.3},
            max_usd=5.0,
        )
    ],
)
```

The module-level :data:`budget_extension` is the deployable default: token mode
with a documented :data:`DEFAULT_MAX_TOKENS` ceiling (no ``cost`` block, so no
dollar figure is invented). Swap in ``make_budget_extension`` with your model's
price block and a ``max_usd`` ceiling to threshold on real spend.

Reference: EXTENSIONS-IMPLEMENTATION.md §E4 (item 1), §E4.cost, §8 S17.
"""

from __future__ import annotations

from typing import Any, Callable

# ── the priced token buckets (pi calculateCost, cache_write inert) ───────────
#: Usage buckets the cost block prices, mapped to their ``cost`` key. ``input``
#: and ``output`` are always populated; ``cache_read`` is populated when the
#: provider reports a cache hit. ``cache_write_tokens`` is never populated by
#: today's provider (a real 0), so it carries no price term here.
_PRICED_BUCKETS: dict[str, str] = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_tokens": "cache_read",
}

#: All token buckets summed for the token-mode running total.
_ALL_BUCKETS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)

#: Deployable-default token ceiling (token mode). A documented knob, not a
#: fallback: with no ``cost`` block there is nothing to price, so the default
#: guards on tokens. Override via ``make_budget_extension(max_tokens=…)``.
DEFAULT_MAX_TOKENS = 500_000


# ── pure cost / token accounting over one completion's usage ─────────────────


def completion_cost_usd(usage: dict[str, Any], cost: dict[str, Any]) -> float:
    """Dollar cost of one completion's ``usage`` under the ``cost`` price block.

    Collapsed pi ``calculateCost`` (``models.ts:39-48`` / τ
    ``backends.compute_cost_usd``): ``sum(price[k] / 1e6 * tokens[k])`` over the
    priced buckets. ``cost`` is USD per 1M tokens; a missing price key is treated
    as ``0.0`` (that bucket simply isn't billed), which is the price being zero —
    not a fabricated total.
    """
    return float(
        sum(
            float(cost.get(price_key, 0.0)) / 1_000_000 * int(usage.get(bucket, 0) or 0)
            for bucket, price_key in _PRICED_BUCKETS.items()
        )
    )


def completion_tokens(usage: dict[str, Any]) -> int:
    """Total tokens across every bucket of one completion's ``usage``."""
    return sum(int(usage.get(bucket, 0) or 0) for bucket in _ALL_BUCKETS)


# ── the warning injected on the trip ─────────────────────────────────────────


def budget_warning_message(detail: str) -> dict[str, Any]:
    """Build the one-shot ``<system-reminder>`` user message announcing the cutoff."""
    body = (
        "<system-reminder>Budget exceeded: "
        f"{detail}. The run is being stopped now; wrap up rather than starting new "
        "work.</system-reminder>"
    )
    return {"role": "user", "content": [{"type": "text", "text": body}]}


# ── the stateful budget guard ────────────────────────────────────────────────


class BudgetGuard:
    """Accumulates usage and, past the ceiling, warns once then aborts the run.

    One instance per loaded extension (spend is per-session). Two bound methods
    are the handlers: :meth:`on_message_end` (notify ``message_end``, accumulate)
    and :meth:`on_context` (mutating ``context`` hook, warn + abort).

    Exactly one threshold governs, chosen by whether a ``cost`` block is known:

    * ``cost`` given  → USD mode, ceiling ``max_usd`` (``max_tokens`` must be None).
    * ``cost`` absent → token mode, ceiling ``max_tokens`` (``max_usd`` must be None).

    Pairing the wrong threshold with the presence/absence of ``cost`` raises
    (Fail-Early: refuse to invent a price, and refuse to silently ignore a ceiling
    that cannot apply).
    """

    def __init__(
        self,
        *,
        cost: dict[str, Any] | None = None,
        max_usd: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        if cost is not None:
            if max_usd is None:
                raise ValueError(
                    "BudgetGuard: a cost block requires max_usd (the USD ceiling)"
                )
            if max_tokens is not None:
                raise ValueError(
                    "BudgetGuard: with a cost block pass max_usd, not max_tokens"
                )
            self._mode = "usd"
        else:
            if max_usd is not None:
                raise ValueError(
                    "BudgetGuard: max_usd needs a cost block; without one, use max_tokens"
                )
            if max_tokens is None:
                raise ValueError(
                    "BudgetGuard: without a cost block, max_tokens (the token ceiling) "
                    "is required — pricing the run would mean inventing dollar figures"
                )
            self._mode = "tokens"

        self._cost = cost
        self._max_usd = max_usd
        self._max_tokens = max_tokens
        self._running_usd = 0.0
        self._running_tokens = 0
        self._tripped = False

    # -- introspection (used by tests / a UI) --------------------------------

    @property
    def mode(self) -> str:
        """``"usd"`` or ``"tokens"`` — which unit this guard thresholds on."""
        return self._mode

    @property
    def running_usd(self) -> float:
        """Cumulative dollars spent so far (0.0 in token mode)."""
        return self._running_usd

    @property
    def running_tokens(self) -> int:
        """Cumulative tokens consumed so far."""
        return self._running_tokens

    @property
    def tripped(self) -> bool:
        """Whether the ceiling has been crossed and the abort fired."""
        return self._tripped

    def is_over(self) -> bool:
        """Whether the running total has reached the active ceiling."""
        if self._mode == "usd":
            assert self._max_usd is not None
            return self._running_usd >= self._max_usd
        assert self._max_tokens is not None
        return self._running_tokens >= self._max_tokens

    def _trip_detail(self) -> str:
        """Human-readable spend/ceiling string for the warning body."""
        if self._mode == "usd":
            return f"${self._running_usd:.4f} spent (ceiling ${self._max_usd})"
        return f"{self._running_tokens} tokens used (ceiling {self._max_tokens})"

    # -- hook handlers --------------------------------------------------------

    def on_message_end(self, event: Any) -> None:
        """``message_end`` handler (notify bus): fold this completion's usage in.

        Reads ``event.message["usage"]``; the tool-turn ``message_end`` ``run()``
        also emits has no ``usage`` key and so is skipped (no double-counting).
        Always accumulates tokens; additionally accumulates dollars in USD mode.
        """
        message = getattr(event, "message", None)
        if not isinstance(message, dict):
            return None
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return None
        self._running_tokens += completion_tokens(usage)
        if self._mode == "usd":
            assert self._cost is not None
            self._running_usd += completion_cost_usd(usage, self._cost)
        return None

    def on_context(self, event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
        """``context`` handler: past the ceiling, warn once then ``ctx.abort()``.

        Fires before every LLM call. When the running total is over the ceiling
        and the guard has not already tripped, it appends the warning to *this*
        turn's payload (so the model is told why the run is ending) and aborts —
        the loop's per-turn abort check then stops the following turn. Returns the
        patched ``{"messages": …}`` on the trip, else ``None`` (payload untouched).
        """
        if self._tripped or not self.is_over():
            return None
        self._tripped = True
        messages = event["messages"]
        messages.append(budget_warning_message(self._trip_detail()))
        ctx.abort()
        return {"messages": messages}


# ── entry points ─────────────────────────────────────────────────────────────


def make_budget_extension(
    *,
    cost: dict[str, Any] | None = None,
    max_usd: float | None = None,
    max_tokens: int | None = None,
) -> Callable[[Any], None]:
    """Build a budget-guard extension entry point over the given ceiling.

    Returns a ``register(api)`` callable that constructs a per-session
    :class:`BudgetGuard` and wires its two handlers: ``message_end`` (accumulate)
    and ``context`` (warn + abort). The threshold arguments are validated by
    :class:`BudgetGuard` (Fail-Early).
    """

    def budget_extension(api: Any) -> None:
        guard = BudgetGuard(cost=cost, max_usd=max_usd, max_tokens=max_tokens)
        api.on("message_end", guard.on_message_end)
        api.on("context", guard.on_context)

    return budget_extension


#: Deployable default: token mode, :data:`DEFAULT_MAX_TOKENS` ceiling. No ``cost``
#: block, so no dollar figure is fabricated. Swap in ``make_budget_extension`` with
#: your model's price block + ``max_usd`` to threshold on real spend.
budget_extension = make_budget_extension(max_tokens=DEFAULT_MAX_TOKENS)
