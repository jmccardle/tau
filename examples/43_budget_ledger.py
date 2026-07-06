"""Example 43: Budget Ledger — a metered ceiling with a report (E9, S65).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S65. Upgrade of ``examples/24_budget.py``
(kept for its own narrower narrative — the durable ``tool_result`` edit template) —
this demo SUPERSEDES it as the ledger showcase: it uses ``ext_kit.ledger`` end to
end (:class:`~ext_kit.ledger.Pricing`, :class:`~ext_kit.ledger.UsageMeter`,
:class:`~ext_kit.ledger.Ceiling`, :class:`~ext_kit.ledger.CostLedger`), an S46
``/ledger`` report command, and a two-line ceiling (soft warn, hard stop) instead
of ``24``'s single one-shot trip. No pi original — pi's budget-guard examples don't
separate "approaching" from "over"; this is the τ-native distillation the S57/S65
roadmap entries describe.

## What's new relative to ``24_budget``

* **Two lines, not one.** :class:`ext_kit.ledger.Ceiling` carries a soft ``warn``
  threshold below the hard ``limit``. Approaching the ceiling appends a durable
  WARNING the model actually sees (so it can wrap up); only crossing the hard
  limit aborts the run. ``24_budget`` only had the hard trip.
* **The warn/stop node is a durable ``turn_end`` append (S43), not a `tool_result`
  edit.** ``24_budget`` edits the triggering tool result in place because its one
  trip immediately aborts (no future turn to warn on the way to). Here the warn
  fires *before* the stop, and the loop keeps running afterward — so the
  warning must be visible to the model on the >NEXT< turn, which is exactly what
  the mutating ``turn_end`` hook is for (a handler return becomes a durable
  ``customMessage`` node prepended to the following turn, E6 §1.3 / S43). The stop
  uses the same channel, then calls ``ctx.abort()``.
* **A cross-session ``CostLedger`` + ``/ledger`` report (S46).** Every warn/stop
  crossing appends a JSONL record (``~/.tau/ext-state/<name>.jsonl``, S57) so
  spend is queryable across restarts, not just within one run. ``/ledger`` prints
  the CURRENT run's live totals/state plus the ledger's all-time roll-up — the
  same command-output channel ``38_todo``/``41_bookmarks`` use in place of pi's
  ``ctx.ui.custom``.

## Config (S40) — same two-mode Fail-Early rule as ``24_budget``

``api.config`` (keyed by this file's stem, ``43_budget_ledger``) may carry:

* ``cost`` — a ``{input, output, cache_read}`` USD-per-1M price block. When
  present, ``max_usd`` governs (USD mode); when absent, ``max_tokens`` governs
  (token mode). Pairing the wrong ceiling with the (non-)presence of ``cost``
  raises — pricing a run with no known price would mean inventing a dollar
  figure (Fail-Early, ``ext_kit.ledger.Pricing`` / this module's own check).
* ``max_usd`` / ``max_tokens`` — the hard ceiling, in whichever unit ``cost``
  selects.
* ``warn_ratio`` — the soft line as a fraction of the hard ceiling (default
  :data:`DEFAULT_WARN_RATIO`, i.e. ``ext_kit.ledger.DEFAULT_WARN_RATIO`` = 0.8).
* ``ledger_name`` — the ``CostLedger`` file stem (default the extension's own
  stem, ``"43_budget_ledger"``).

With no config at all the demo runs in token mode against
:data:`DEFAULT_MAX_TOKENS` (the same documented default ``24_budget`` ships) —
never a fabricated dollar figure.

## Usage

    tau -e examples/43_budget_ledger.py \\
        --ext-config 43_budget_ledger.max_tokens=50000

    > ... (a long conversation) ...
    <system-reminder>Budget warning: 41000 tokens used, approaching the
    50000 ceiling.</system-reminder>
    > /ledger
    This run: 41000 tokens (warn), ceiling 50000 tokens.
    All-time: 2 events, 91000 tokens.
    ... (further turns) ...
    <system-reminder>Budget exceeded: 51000 tokens used (ceiling 50000). The
    run is being stopped now; wrap up rather than starting new work.</system-reminder>
    (the run aborts at the next turn boundary)
"""

from __future__ import annotations

import os
import sys
from typing import Any

# ``ext_kit`` lives alongside the numbered examples, not inside an installed
# package — add ``examples/`` to the path the same way the other ext_kit-using
# demos (24_budget, 41_bookmarks, S56/S57) do when run standalone or via ``-e``.
_EXAMPLES_DIR = os.path.dirname(os.path.abspath(__file__))
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

from ext_kit import ledger  # noqa: E402  (path insertion must precede the import)

#: This extension's own file stem — the ``api.config`` slice key (S40) and the
#: default ``CostLedger`` file stem (S57), matched so an unconfigured run still
#: gets a stable, discoverable ledger file.
EXTENSION_STEM = "43_budget_ledger"

#: Deployable-default token ceiling (token mode). Same documented knob as
#: ``24_budget.DEFAULT_MAX_TOKENS`` — with no ``cost`` block there is nothing to
#: price, so the default guards on tokens. Override via config.
DEFAULT_MAX_TOKENS = 500_000

#: Outcome labels this demo writes to the ``CostLedger`` (S57 ``by_outcome``/
#: ``total_usd`` queries key on these).
OUTCOME_WARN = "warn"
OUTCOME_STOP = "stop"


# ── the warn / stop durable nodes (turn_end mutating hook, S43) ──────────────


def _detail(*, mode: str, value: float, limit: float) -> str:
    """Human-readable spend/ceiling string shared by the warn and stop bodies."""
    if mode == "usd":
        return f"${value:.4f} spent (ceiling ${limit:g})"
    return f"{int(value)} tokens used (ceiling {int(limit)})"


def warn_message(*, mode: str, value: float, limit: float) -> dict[str, Any]:
    """The durable ``turn_end`` return for the SOFT warn crossing.

    Unlike ``24_budget``'s trip (which aborts immediately), this run keeps
    going — so the warning must reach the model as real input on the next
    turn, not just sit on a result it will never send again. A mutating
    ``turn_end`` return is exactly that: a durable ``customMessage`` node
    woven in BEFORE the next turn (E6 §1.3 / S43).
    """
    body = (
        f"<system-reminder>Budget warning: {_detail(mode=mode, value=value, limit=limit)}, "
        "approaching the ceiling.</system-reminder>"
    )
    return {
        "message": {
            "customType": "budget_warning",
            "content": [{"type": "text", "text": body}],
        }
    }


def stop_message(*, mode: str, value: float, limit: float) -> dict[str, Any]:
    """The durable ``turn_end`` return for the HARD stop crossing (then ``ctx.abort()``)."""
    body = (
        f"<system-reminder>Budget exceeded: {_detail(mode=mode, value=value, limit=limit)}. "
        "The run is being stopped now; wrap up rather than starting new "
        "work.</system-reminder>"
    )
    return {
        "message": {
            "customType": "budget_stop",
            "content": [{"type": "text", "text": body}],
        }
    }


# ── the stateful ledger guard ────────────────────────────────────────────────


class LedgerGuard:
    """Metered ceiling with warn/stop lines and a cross-session cost ledger.

    Composes all four ``ext_kit.ledger`` pieces (S57): a :class:`~ext_kit.ledger.Pricing`
    (via the :class:`~ext_kit.ledger.UsageMeter`), a :class:`~ext_kit.ledger.Ceiling`
    for the two-line warn/stop state machine, and a :class:`~ext_kit.ledger.CostLedger`
    for the cross-session audit trail the ``/ledger`` command reports from.

    Same two-mode Fail-Early validation as ``24_budget.BudgetGuard``: a ``cost``
    block requires ``max_usd`` (USD mode); no ``cost`` block requires ``max_tokens``
    (token mode) — pairing the wrong ceiling with the (non-)presence of ``cost``
    raises rather than silently fabricating a price.
    """

    def __init__(
        self,
        *,
        cost: dict[str, Any] | None = None,
        max_usd: float | None = None,
        max_tokens: int | None = None,
        warn_ratio: float = ledger.DEFAULT_WARN_RATIO,
        cost_ledger: ledger.CostLedger,
    ) -> None:
        if cost is not None:
            if max_usd is None:
                raise ValueError("LedgerGuard: a cost block requires max_usd (the USD ceiling)")
            if max_tokens is not None:
                raise ValueError("LedgerGuard: with a cost block pass max_usd, not max_tokens")
            self._mode = "usd"
            limit = max_usd
        else:
            if max_usd is not None:
                raise ValueError(
                    "LedgerGuard: max_usd needs a cost block; without one, use max_tokens"
                )
            if max_tokens is None:
                raise ValueError(
                    "LedgerGuard: without a cost block, max_tokens (the token ceiling) "
                    "is required — pricing the run would mean inventing dollar figures"
                )
            self._mode = "tokens"
            limit = float(max_tokens)

        self._limit = limit
        self._meter = ledger.UsageMeter(ledger.Pricing(model=None, cost=cost) if cost else None)
        self._cost_ledger = cost_ledger
        # One-shot latches for the durable append, distinct from the Ceiling's own
        # crossing state (which never resets) — each pending flag is consumed
        # exactly once by the following turn_end. Driven by the Ceiling's OWN
        # on_warn/on_stop callbacks (fired exactly once per crossing) rather than
        # by re-inspecting ``state`` after every update — the latter would
        # re-arm on every subsequent update that merely HOLDS at the warn line
        # (e.g. a later zero-usage completion), double-appending the warning.
        self._pending_warn = False
        self._pending_stop = False
        self._ceiling = ledger.Ceiling(
            limit=limit,
            on_warn=lambda _value: setattr(self, "_pending_warn", True),
            on_stop=lambda _value: setattr(self, "_pending_stop", True),
            warn_ratio=warn_ratio,
        )

    @property
    def mode(self) -> str:
        """``"usd"`` or ``"tokens"`` — which unit this guard thresholds on."""
        return self._mode

    def _value(self) -> float:
        """The running total the ceiling thresholds on (dollars, or tokens)."""
        if self._mode == "usd":
            return self._meter.usd or 0.0
        return float(self._meter.tokens)

    def status_line(self) -> str:
        """A one-line summary of this run's live state (used by ``/ledger``)."""
        value = self._value()
        unit = "tokens" if self._mode == "tokens" else "USD"
        rendered = f"${value:.4f}" if self._mode == "usd" else f"{int(value)} tokens"
        limit_rendered = (
            f"${self._limit:g}" if self._mode == "usd" else f"{int(self._limit)} {unit}"
        )
        return f"This run: {rendered} ({self._ceiling.state}), ceiling {limit_rendered}."

    # -- hook handlers --------------------------------------------------------

    def on_message_end(self, event: Any) -> None:
        """``message_end`` (notify): fold usage, advance the ceiling's state.

        Delegates to :meth:`ext_kit.ledger.UsageMeter.record_message_end`, which
        skips the duplicate tool-turn ``message_end`` (no ``usage`` key, no
        double-count). ``Ceiling.update`` fires ``on_warn``/``on_stop`` exactly
        once per crossing — those callbacks only set the pending latches; the
        durable append happens in :meth:`on_turn_end`, the only handler with a
        ``ctx`` to append through.
        """
        if not self._meter.record_message_end(event):
            return
        self._ceiling.update(self._value())

    async def on_turn_end(self, event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
        """``turn_end`` (mutating, S43): drain a pending warn/stop into a durable node.

        The hard stop takes priority over a stale warn latch (both could be
        pending if a single completion leapt past both lines — ``Ceiling.update``
        already fired both callbacks in order). Records the crossing to the
        cross-session :class:`~ext_kit.ledger.CostLedger` either way, so
        ``/ledger`` can report on it even after this process exits.
        """
        if self._pending_stop:
            self._pending_stop = False
            self._pending_warn = False
            value = self._value()
            self._cost_ledger.append(
                outcome=OUTCOME_STOP,
                usd=value if self._mode == "usd" else None,
                tokens=self._meter.tokens,
            )
            ctx.abort()
            return stop_message(mode=self._mode, value=value, limit=self._limit)
        if self._pending_warn:
            self._pending_warn = False
            value = self._value()
            self._cost_ledger.append(
                outcome=OUTCOME_WARN,
                usd=value if self._mode == "usd" else None,
                tokens=self._meter.tokens,
            )
            return warn_message(mode=self._mode, value=value, limit=self._limit)
        return None


# ── the /ledger report command (S46) ─────────────────────────────────────────


def _ledger_report(guard: LedgerGuard, cost_ledger: ledger.CostLedger) -> str:
    """Build the ``/ledger`` display-only report: live run + all-time roll-up."""
    lines = [guard.status_line()]
    records = cost_ledger.records()
    if not records:
        lines.append("All-time: no events recorded yet.")
    else:
        total_tokens = cost_ledger.total_tokens()
        total_usd = cost_ledger.total_usd()
        usd_part = f", ${total_usd:.4f}" if total_usd is not None else ""
        lines.append(f"All-time: {len(records)} events, {total_tokens} tokens{usd_part}.")
        for outcome, stats in sorted(cost_ledger.by_outcome().items()):
            usd_part = f", ${stats.usd:.4f}" if stats.usd is not None else ""
            lines.append(f"  {outcome}: {stats.count} event(s), {stats.tokens} tokens{usd_part}")
    return "\n".join(lines)


# ── entry point ───────────────────────────────────────────────────────────────


def budget_ledger_extension(api: Any) -> None:
    """Extension entry point: wire the guard's two hooks + the ``/ledger`` command.

    Reads its ceiling config from ``api.config`` (S40, sliced by this file's
    stem). No ``cost``/``max_usd``/``max_tokens`` at all falls back to token mode
    at :data:`DEFAULT_MAX_TOKENS` (a documented default, not a fabricated price —
    same rule ``24_budget`` follows).
    """
    cfg = api.config
    cost = cfg.get("cost")
    max_usd = cfg.get("max_usd")
    max_tokens = cfg.get("max_tokens")
    if cost is None and max_usd is None and max_tokens is None:
        max_tokens = DEFAULT_MAX_TOKENS
    warn_ratio = cfg.get("warn_ratio", ledger.DEFAULT_WARN_RATIO)
    ledger_name = cfg.get("ledger_name", EXTENSION_STEM)

    cost_ledger = ledger.CostLedger(ledger_name)
    guard = LedgerGuard(
        cost=cost,
        max_usd=max_usd,
        max_tokens=max_tokens,
        warn_ratio=warn_ratio,
        cost_ledger=cost_ledger,
    )

    async def ledger_command(args: str, ctx: Any) -> str:
        return _ledger_report(guard, cost_ledger)

    api.on("message_end", guard.on_message_end)
    api.on("turn_end", guard.on_turn_end)
    api.register_command(
        "ledger",
        {
            "description": "Show this run's spend and the all-time cost ledger",
            "handler": ledger_command,
        },
    )


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/43_budget_ledger.py`` → ``getattr(module, "register")``).
register = budget_ledger_extension
