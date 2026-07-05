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

## S59 — refactored onto ``ext_kit.ledger``

The cost/token accounting and the accumulate-then-trip machinery this demo once
hand-rolled inline are the exact pattern S57 distilled into :mod:`ext_kit.ledger`
(``docs/EXTENSIONS-DEMO-ROADMAP.md §4``: "formalizes what ``24_budget``
hand-rolls"). S59 refactors the demo to CONSUME that kit as the proof the
abstraction is the right one, with behavior preserved:

* :func:`completion_cost_usd` / :func:`completion_tokens` now delegate to
  :class:`ext_kit.ledger.Pricing` / :func:`ext_kit.ledger.usage_tokens`.
* :class:`BudgetGuard` folds usage through a :class:`ext_kit.ledger.UsageMeter`
  (replacing the hand-summed ``_running_usd`` / ``_running_tokens``) and reads its
  one-shot trip off a :class:`ext_kit.ledger.Ceiling` (replacing ``is_over`` /
  ``_tripped``'s value check). The two-mode Fail-Early validation, the durable
  ``tool_result`` edit, and ``ctx.abort()`` are unchanged.

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
EXTENSIONS-E5-WIRING.md §3.2–§3.3 / S32 (durable-hook rework — ``context`` retired);
docs/EXTENSIONS-DEMO-ROADMAP.md §4 S57 / §7 S59 (refactored onto ``ext_kit.ledger``).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable

# ── import the kit (it lives alongside the demos in examples/) ───────────────
# The file-path extension loader (``tau -e examples/24_budget.py``) does not add
# the extension's own directory to ``sys.path``, and the test harness loads this
# file by path too — so bootstrap ``examples/`` onto the path before importing the
# kit, whether run directly, imported, or loaded via ``-e`` (D-E6-3).
_EXAMPLES_DIR = os.path.dirname(os.path.abspath(__file__))
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

from ext_kit import ledger  # noqa: E402  (path insertion must precede the import)

#: Deployable-default token ceiling (token mode). A documented knob, not a
#: fallback: with no ``cost`` block there is nothing to price, so the default
#: guards on tokens. Override via ``make_budget_extension(max_tokens=…)``.
DEFAULT_MAX_TOKENS = 500_000


# ── pure cost / token accounting over one completion's usage ─────────────────


def completion_cost_usd(usage: dict[str, Any], cost: dict[str, Any]) -> float:
    """Dollar cost of one completion's ``usage`` under the ``cost`` price block.

    Thin wrapper over :meth:`ext_kit.ledger.Pricing.cost_of` (S57 — the same
    collapsed pi ``calculateCost`` / τ ``backends.compute_cost_usd`` formula:
    ``sum(price[k] / 1e6 * tokens[k])`` over the priced buckets). ``cost`` is a
    present price block (USD per 1M tokens), so the priced result is always a real
    float — a missing price key prices that bucket at ``0.0`` (unbilled), not a
    fabricated total.
    """
    priced = ledger.Pricing(model=None, cost=cost).cost_of(usage)
    # ``cost`` is a present block, so Pricing is priced and never returns None.
    assert priced is not None
    return priced


def completion_tokens(usage: dict[str, Any]) -> int:
    """Total tokens across every bucket of one completion's ``usage``.

    Delegates to :func:`ext_kit.ledger.usage_tokens` (S57 — sums every bucket).
    """
    return ledger.usage_tokens(usage)


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
                raise ValueError("BudgetGuard: a cost block requires max_usd (the USD ceiling)")
            if max_tokens is not None:
                raise ValueError("BudgetGuard: with a cost block pass max_usd, not max_tokens")
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

        self._max_usd = max_usd
        self._max_tokens = max_tokens

        # S59: the accumulation is an ``ext_kit.ledger.UsageMeter`` (priced in USD
        # mode, tokens-only otherwise), and the ceiling is an
        # ``ext_kit.ledger.Ceiling`` fed the running value — the kit primitives that
        # formalize this demo's hand-rolled totals + one-shot trip (S57).
        if self._mode == "usd":
            assert max_usd is not None
            self._meter = ledger.UsageMeter(ledger.Pricing(model=None, cost=cost))
            self._ceiling = ledger.Ceiling(limit=max_usd, warn_ratio=1.0)
        else:
            assert max_tokens is not None
            self._meter = ledger.UsageMeter(None)
            self._ceiling = ledger.Ceiling(limit=float(max_tokens), warn_ratio=1.0)
        # One-shot abort latch (distinct from the ceiling's value-crossing latch):
        # set only when the ``tool_result`` hook has appended the warning + aborted.
        self._tripped = False

    def _threshold_value(self) -> float:
        """The running total the ceiling thresholds on (dollars, or tokens)."""
        if self._mode == "usd":
            return self._meter.usd or 0.0
        return float(self._meter.tokens)

    # -- introspection (used by tests / a UI) --------------------------------

    @property
    def mode(self) -> str:
        """``"usd"`` or ``"tokens"`` — which unit this guard thresholds on."""
        return self._mode

    @property
    def running_usd(self) -> float:
        """Cumulative dollars spent so far (0.0 in token mode)."""
        return self._meter.usd or 0.0

    @property
    def running_tokens(self) -> int:
        """Cumulative tokens consumed so far."""
        return self._meter.tokens

    @property
    def tripped(self) -> bool:
        """Whether the ceiling has been crossed and the abort fired."""
        return self._tripped

    def is_over(self) -> bool:
        """Whether the running total has reached the active ceiling.

        Reads the ``Ceiling``'s latched stop state — spend is monotonic, so a
        crossed ceiling stays crossed (the same ``running >= ceiling`` boundary the
        demo checked by hand, now owned by the kit's bang-bang controller).
        """
        return self._ceiling.tripped

    def _trip_detail(self) -> str:
        """Human-readable spend/ceiling string for the warning body."""
        if self._mode == "usd":
            return f"${self.running_usd:.4f} spent (ceiling ${self._max_usd})"
        return f"{self.running_tokens} tokens used (ceiling {self._max_tokens})"

    # -- hook handlers --------------------------------------------------------

    def on_message_end(self, event: Any) -> None:
        """``message_end`` handler (notify bus): fold this completion's usage in.

        Delegates to :meth:`ext_kit.ledger.UsageMeter.record_message_end`, which
        reads ``event.message["usage"]`` and skips the tool-turn ``message_end``
        ``run()`` also emits (no ``usage`` key → no double-counting). When a
        completion folds, the fresh running value is fed to the ``Ceiling`` so its
        crossing state stays current.
        """
        if self._meter.record_message_end(event):
            self._ceiling.update(self._threshold_value())
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
