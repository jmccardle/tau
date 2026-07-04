"""Example 24: Budget — a running-cost guard that warns then aborts (E2 + cost).

A stateful extension that watches token usage as it *lands* (via the notify
``message_end`` event) and, once the run's cumulative spend crosses a configured
ceiling, appends a one-shot ``<system-reminder>`` warning as a DURABLE edit to the
triggering ``tool_result`` node (via the ``tool_result`` hook) and then calls
``ctx.abort()`` — stopping the agent loop at the next turn boundary. It is the pi
"budget guard" (plan D1), reworked onto τ's durable-hook model (E5 §3.2–§3.3 / S32).

## Why a durable ``tool_result`` edit, not the retired ``context`` hook

E5 eliminated the per-call ``context`` transform: under the durable-hook invariant
the model's input for any LLM call is exactly the system prompt + the linear active
path, so an ephemeral per-send injection is a hidden divergence (§1). The warning is
therefore appended to a **real node** — the ``tool_result`` that follows the
completion whose usage tripped the ceiling — making it a durable, reloadable part of
the transcript rather than a message that exists only on one wire. ``ctx.abort()``
is unchanged: the loop's per-turn abort check then breaks before the next turn, so
the warning is the run's last durable node. (Because the abort halts the loop before
another LLM round-trip, the durable warning records *why the run ended* in the
transcript rather than being re-sent to the model — the honest tree-as-truth
tradeoff for the retired ephemeral injection.)

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
``_stream_response`` carries ``message["usage"]``, emitted before the turn's tools
run), while the ``tool_result`` hook fires *after* the turn's tools. So the guard
accumulates on ``message_end`` and, on that same turn's ``tool_result``, sees the
freshly-added spend and can trip. When it trips it APPENDS the warning to that
result's ``content`` (a durable edit) and aborts; the loop's per-turn
``is_aborted()`` check then breaks before the following turn. The trip is one-shot
(``_tripped``) so a second ``tool_result`` in the same turn neither double-appends
nor double-aborts.

## Field contract

The ``message_end`` handler receives a notify ``AgentEvent`` (single argument);
the per-completion usage is ``event.message["usage"]`` — a plain dict with
``input_tokens`` / ``output_tokens`` / ``cache_read_tokens`` /
``cache_write_tokens``. The duplicate tool-turn ``message_end`` that ``run()``
emits has no ``usage`` key and so contributes nothing (no double-counting). The
``tool_result`` handler receives ``(event, ctx)``, edits the result via a returned
``{"content": …}`` patch, and aborts via ``ctx.abort()`` — the live per-prompt abort
signal the session binds onto the context.

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

Reference: EXTENSIONS-IMPLEMENTATION.md §E4 (item 1), §E4.cost, §8 S17;
EXTENSIONS-E5-WIRING.md §3.2–§3.3 / S32 (durable-hook rework — ``context`` retired).
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


def budget_warning_block(detail: str) -> dict[str, Any]:
    """Build the one-shot ``<system-reminder>`` content block announcing the cutoff.

    A ``tool_result`` content block (not a standalone message): it is APPENDED to the
    triggering result's ``content``, so the warning is a durable part of that node.
    """
    body = (
        "<system-reminder>Budget exceeded: "
        f"{detail}. The run is being stopped now; wrap up rather than starting new "
        "work.</system-reminder>"
    )
    return {"type": "text", "text": body}


# ── the stateful budget guard ────────────────────────────────────────────────


class BudgetGuard:
    """Accumulates usage and, past the ceiling, warns once then aborts the run.

    One instance per loaded extension (spend is per-session). Two bound methods
    are the handlers: :meth:`on_message_end` (notify ``message_end``, accumulate)
    and :meth:`on_tool_result` (mutating ``tool_result`` hook, durable warn + abort).

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

    def on_tool_result(self, event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
        """``tool_result`` handler: past the ceiling, warn once (durable) then abort.

        Fires after each landed tool result — which, for the tripping turn, follows
        the ``message_end`` that accumulated the over-ceiling spend. When the running
        total is over the ceiling and the guard has not already tripped, it APPENDS
        the warning to *this* result's ``content`` (a durable edit recording why the
        run is ending) and calls ``ctx.abort()`` — the loop's per-turn abort check
        then stops before the following turn. Returns the patched ``{"content": …}``
        on the trip, else ``None`` (result untouched). One-shot via ``_tripped``.
        """
        if self._tripped or not self.is_over():
            return None
        self._tripped = True
        content = list(event.get("content") or [])
        content.append(budget_warning_block(self._trip_detail()))
        ctx.abort()
        return {"content": content}


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
    and ``tool_result`` (durable warn + abort). The threshold arguments are validated
    by :class:`BudgetGuard` (Fail-Early).
    """

    def budget_extension(api: Any) -> None:
        guard = BudgetGuard(cost=cost, max_usd=max_usd, max_tokens=max_tokens)
        api.on("message_end", guard.on_message_end)
        api.on("tool_result", guard.on_tool_result)

    return budget_extension


#: Deployable default: token mode, :data:`DEFAULT_MAX_TOKENS` ceiling. No ``cost``
#: block, so no dollar figure is fabricated. Swap in ``make_budget_extension`` with
#: your model's price block + ``max_usd`` to threshold on real spend.
budget_extension = make_budget_extension(max_tokens=DEFAULT_MAX_TOKENS)


#: The module-level ``register`` the file-path loader looks up (``tau -e
#: examples/24_budget.py`` → ``getattr(module, "register")``). It IS the deployable
#: token-mode :data:`budget_extension` default; the alias makes the demo loadable
#: through the public ``-e`` surface used by the live procedures
#: (EXTENSIONS-LIVE-PROCEDURES.md; EXTENSIONS-E5-WIRING.md §6 / S37).
register = budget_extension
