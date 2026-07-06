"""Example 53: Router Ledger — a cost ledger routes the model, human ratifies (E11, S74).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §7 S74. A *composed* showcase: it promotes
one output (the accreting cost ledger) into an input (the next turn's model), with a
human on the ratify gate. The dial it turns is **ledger → routing**.

## The composition

Three τ surfaces, wired end to end — no new harness code, all on the public API:

* **`ext_kit.ledger`** (S57) — :class:`~ext_kit.ledger.Pricing` prices each landed
  completion's usage; :class:`~ext_kit.ledger.CostLedger` is the cross-session JSONL
  audit trail. Unlike ``43_budget_ledger`` (which keys the ledger by a warn/stop
  *outcome*), this demo keys each record by ``(task-tag, model)``: the ``outcome``
  field carries the **task tag**, and the ``model`` field the completion's model id.
  So the ledger answers "what has each MODEL cost me on each KIND of task?".
* **S45 usage + model access** — the recording hook reads the completion's own
  ``usage`` and ``model`` off the per-completion ``message_end`` event (the same
  authoritative usage the S45 ``ctx.get_usage()`` accessor exposes); ``/route`` reads
  the live active model via ``ctx.get_model()`` and *reassigns* it via
  ``ctx.set_model(name)`` (effective next turn — never mid-stream).
* **S47 confirm** — the reassignment is a HUMAN-RATIFIED action: ``/route`` only calls
  ``ctx.set_model`` after ``ctx.ui.confirm`` returns ``True``. The ledger *recommends*;
  the person *ratifies*. Headless, this obeys the S48 ``--ui-defaults confirm=…``
  policy (no policy → the confirm RAISES rather than silently auto-routing).

## What ``/route`` recommends (and what it will NOT)

For the current task tag and the live active model, ``/route`` finds the cheapest
model — by observed **$/1k tokens** — that ALSO has a priced track record on that
task, and recommends switching to it iff it is strictly cheaper than the active
model's own observed rate on that task. This is deliberately conservative and
Fail-Early:

* It compares only models it has **actually observed** serving the task. It never
  invents the cost of a model it has never run on this task (no fabricated figure).
* It needs a priced baseline for the ACTIVE model on this task too; with none, it
  makes NO recommendation (it will not claim a saving it cannot measure).
* An **unpriced** model (no ``cost`` block in this extension's ``models`` config)
  contributes tokens to the ledger but never a dollar figure, so it never
  participates in a cost recommendation — the honest "cost unknown", never a ``$0``.

## Config (S40) — keyed by this file's stem, ``53_router_ledger``

* ``models`` — a ``{model_id: {input, output, cache_read}}`` map of USD-per-1M price
  blocks, keyed by the model id as it appears on a completion (``message.model``),
  which is the same string ``ctx.set_model`` routes by in a standard τ config
  (``build_model_from_config`` sets ``Model.id`` from the config entry's ``model``
  field, and configs conventionally key a model by that string). A model absent
  from this map is *unpriced* (tokens-only), never priced at ``$0``.
* ``task`` — the current task tag every completion this run is recorded under
  (default :data:`DEFAULT_TASK`).
* ``ledger_name`` — the ``CostLedger`` file stem (default this file's stem), so the
  ledger accretes across sessions at a stable, discoverable path.

## Usage

    tau -e examples/53_router_ledger.py \\
        --ext-config 53_router_ledger.task=refactor

    > ... (turns land, each recorded under (refactor, <active model>)) ...
    > /route
    Task 'refactor' — cost by model ($/1k tokens):
      gpt-4o        3 turn(s)   180000 tokens   $2.7000   ($0.0150/1k)
      gpt-4o-mini   5 turn(s)   300000 tokens   $0.1800   ($0.0006/1k)
    Recommendation: reassign 'gpt-4o' → 'gpt-4o-mini' for task 'refactor'
      ($0.0150/1k → $0.0006/1k, ~96.0% cheaper).
    (a confirm dialog pops up — S47)
    Reassigned: active model is now 'gpt-4o-mini' (effective next turn).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# ``ext_kit`` lives alongside the numbered examples, not inside an installed
# package — add ``examples/`` to the path the same way the other ext_kit-using
# demos do when run standalone or via ``-e``.
_EXAMPLES_DIR = os.path.dirname(os.path.abspath(__file__))
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

from ext_kit import ledger  # noqa: E402  (path insertion must precede the import)

#: This extension's own file stem — the ``api.config`` slice key (S40) and the
#: default ``CostLedger`` file stem (S57), matched so an unconfigured run still
#: gets a stable, discoverable ledger file.
EXTENSION_STEM = "53_router_ledger"

#: The task tag every completion is recorded under when config names none. A
#: documented default (the "unlabelled work" bucket), not a fabricated value —
#: override with ``--ext-config 53_router_ledger.task=<tag>``.
DEFAULT_TASK = "general"


# ── per-(task, model) roll-up over the ledger ────────────────────────────────


@dataclass
class RouteStats:
    """A ``(task, model)`` cell of the router ledger's roll-up.

    ``usd`` is the summed dollar cost over the *priced* records for this cell, or
    ``None`` when every record here was unpriced (cost unknown — never a fabricated
    ``$0``, matching :class:`ext_kit.ledger.OutcomeStats`). :attr:`usd_per_1k_tokens`
    is the efficiency metric ``/route`` ranks models on; it is ``None`` (not
    rankable) whenever the cell is unpriced or carries no tokens.
    """

    task: str
    model: str
    count: int = 0
    tokens: int = 0
    usd: float | None = None

    @property
    def usd_per_1k_tokens(self) -> float | None:
        """Observed cost efficiency: dollars per 1000 tokens, or ``None`` if unrankable."""
        if self.usd is None or self.tokens <= 0:
            return None
        return self.usd / self.tokens * 1000.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "model": self.model,
            "count": self.count,
            "tokens": self.tokens,
            "usd": self.usd,
            "usd_per_1k_tokens": self.usd_per_1k_tokens,
        }


def route_stats(records: list[dict[str, Any]]) -> dict[tuple[str, str], RouteStats]:
    """Roll a :class:`ext_kit.ledger.CostLedger`'s records up per ``(task, model)``.

    ``outcome`` carries the task tag, ``model`` the completion's model id (this is
    how :meth:`RouterLedger.on_message_end` writes them). A record missing its
    ``model`` id is not a routing record and cannot be attributed to a cell, so it
    RAISES rather than being silently bucketed (Fail-Early — a foreign or corrupt
    ledger under this name is a real fault, not something to paper over).
    """
    cells: dict[tuple[str, str], RouteStats] = {}
    for rec in records:
        model = rec.get("model")
        if not model:
            raise ValueError(
                "route_stats: ledger record has no 'model' id — this is not a router "
                f"record (record: {rec!r}). Point ledger_name at a dedicated file."
            )
        task = str(rec.get("outcome"))
        key = (task, str(model))
        cell = cells.setdefault(key, RouteStats(task=task, model=str(model)))
        cell.count += 1
        cell.tokens += int(rec.get("tokens", 0) or 0)
        usd = rec.get("usd")
        if usd is not None:
            cell.usd = (cell.usd or 0.0) + float(usd)
    return cells


# ── the recommendation (ledger → routing) ────────────────────────────────────


@dataclass(frozen=True)
class Recommendation:
    """A single reassignment ``/route`` proposes for the human to ratify.

    Switching from :attr:`from_model` (the live active model) to :attr:`to_model`
    (a strictly cheaper model with a track record on :attr:`task`), quantified by
    the two observed $/1k-token rates.
    """

    task: str
    from_model: str
    to_model: str
    from_rate: float
    to_rate: float

    @property
    def savings_pct(self) -> float:
        """How much cheaper :attr:`to_model` is than :attr:`from_model`, as a percent."""
        if self.from_rate <= 0:
            return 0.0
        return (self.from_rate - self.to_rate) / self.from_rate * 100.0


def recommend_reassignment(
    stats: dict[tuple[str, str], RouteStats],
    task: str,
    active_model: str,
) -> Recommendation | None:
    """The cheapest priced model for ``task`` beating ``active_model``, or ``None``.

    Conservative + Fail-Early: considers only ``(task, model)`` cells with a real
    observed $/1k-token rate (priced, non-empty). Needs a priced baseline for
    ``active_model`` on ``task`` too — with none, there is nothing to measure a
    saving against, so it returns ``None`` rather than guessing. Returns a
    :class:`Recommendation` only when some OTHER model on this task is *strictly*
    cheaper than the active model's own observed rate.
    """
    priced: list[tuple[float, RouteStats]] = []
    for (cell_task, _model), cell in stats.items():
        if cell_task != task:
            continue
        rate = cell.usd_per_1k_tokens
        if rate is None:
            continue
        priced.append((rate, cell))
    if not priced:
        return None

    active = stats.get((task, active_model))
    active_rate = active.usd_per_1k_tokens if active is not None else None
    if active_rate is None:
        return None

    cheapest_rate, cheapest = min(priced, key=lambda pair: pair[0])
    if cheapest.model == active_model or cheapest_rate >= active_rate:
        return None
    return Recommendation(
        task=task,
        from_model=active_model,
        to_model=cheapest.model,
        from_rate=active_rate,
        to_rate=cheapest_rate,
    )


# ── the recording ledger (message_end notify → CostLedger) ───────────────────


class RouterLedger:
    """Records each landed completion's cost under ``(task-tag, model)`` (S57 + S45).

    The metering half of this demo: a ``message_end`` notify handler that prices the
    completion's own usage with a per-model :class:`ext_kit.ledger.Pricing` (from this
    extension's ``models`` config) and appends one :class:`ext_kit.ledger.CostLedger`
    record keyed by the current ``task`` tag (``outcome``) and the completion's model
    id (``model``). The duplicate tool-turn ``message_end`` carries no ``usage`` and
    is skipped (no double-count), matching ``ext_kit.ledger.UsageMeter``.
    """

    def __init__(
        self,
        *,
        cost_ledger: ledger.CostLedger,
        model_prices: Mapping[str, dict[str, float] | None],
        task: str,
    ) -> None:
        if not task:
            raise ValueError("RouterLedger: task tag is required (Fail-Early, no empty tag)")
        self._cost_ledger = cost_ledger
        self._prices: dict[str, dict[str, float] | None] = dict(model_prices)
        self._task = task

    @property
    def task(self) -> str:
        """The task tag every completion this run is recorded under."""
        return self._task

    def pricing_for(self, model_id: str) -> ledger.Pricing:
        """The :class:`ext_kit.ledger.Pricing` for ``model_id``.

        Priced when this extension's ``models`` config carries a ``cost`` block for
        the id; **unpriced** (``cost=None`` → dollars unknown, tokens only) otherwise
        — the honest absence, never a fabricated ``$0`` (Fail-Early).
        """
        return ledger.Pricing(model=model_id, cost=self._prices.get(model_id))

    def on_message_end(self, event: Any) -> None:
        """``message_end`` (notify): append this completion's cost under ``(task, model)``.

        Reads the completion's own ``usage`` and ``model`` off the per-completion
        ``message_end`` (the S45-authoritative usage). The duplicate tool-turn
        ``message_end`` has no ``usage`` and is skipped (no double-count). A
        usage-bearing completion with no ``model`` id is a real fault — it cannot be
        attributed to a routing cell — so it RAISES (Fail-Early).
        """
        message = getattr(event, "message", None)
        if not isinstance(message, Mapping):
            return
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            return
        model_id = message.get("model")
        if not model_id:
            raise ValueError(
                "RouterLedger.on_message_end: a completion carried usage but no 'model' "
                "id, so it cannot be attributed to a (task, model) cell (Fail-Early)."
            )
        pricing = self.pricing_for(str(model_id))
        self._cost_ledger.append(
            outcome=self._task,
            model=str(model_id),
            tokens=ledger.usage_tokens(usage),
            usd=pricing.cost_of(usage),
        )


# ── the /route report + reassignment command (S46 + S47) ─────────────────────


def _rate_str(rate: float | None) -> str:
    """A ``$X/1k`` rate string, or ``unpriced`` when the cell has no dollar figure."""
    return f"${rate:.4f}/1k" if rate is not None else "unpriced"


def route_body(stats: dict[tuple[str, str], RouteStats], task: str) -> str:
    """The per-model cost table for ``task`` (the display-only body of ``/route``)."""
    rows = [cell for (cell_task, _m), cell in stats.items() if cell_task == task]
    if not rows:
        return f"Task {task!r} — no cost recorded yet."
    lines = [f"Task {task!r} — cost by model ($/1k tokens):"]
    for cell in sorted(rows, key=lambda c: c.model):
        usd_str = f"${cell.usd:.4f}" if cell.usd is not None else "unpriced"
        lines.append(
            f"  {cell.model}  {cell.count} turn(s)  {cell.tokens} tokens  "
            f"{usd_str}  ({_rate_str(cell.usd_per_1k_tokens)})"
        )
    return "\n".join(lines)


def recommendation_line(rec: Recommendation) -> str:
    """The one-line reassignment proposal ``/route`` prints (and the confirm summarizes)."""
    return (
        f"Recommendation: reassign {rec.from_model!r} → {rec.to_model!r} for task "
        f"{rec.task!r} ({_rate_str(rec.from_rate)} → {_rate_str(rec.to_rate)}, "
        f"~{rec.savings_pct:.1f}% cheaper)."
    )


# ── entry point ───────────────────────────────────────────────────────────────


def _validate_model_prices(raw: Any) -> dict[str, dict[str, float] | None]:
    """Normalize the ``models`` config into ``{id: cost block | None}``, Fail-Early.

    A ``cost`` block must be a mapping (priced) or ``None`` (unpriced). Any other
    value (a string, a number) is a config mistake that would otherwise be silently
    dropped or mis-priced, so it RAISES naming the offending model.
    """
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"53_router_ledger: 'models' config must be a map of "
            f"{{model_id: cost block}}, got {type(raw).__name__}"
        )
    prices: dict[str, dict[str, float] | None] = {}
    for model_id, block in raw.items():
        if block is None:
            prices[str(model_id)] = None
        elif isinstance(block, Mapping):
            prices[str(model_id)] = {str(k): float(v) for k, v in block.items()}
        else:
            raise ValueError(
                f"53_router_ledger: price block for model {model_id!r} must be a "
                f"mapping or null, got {type(block).__name__}"
            )
    return prices


def router_ledger_extension(api: Any) -> None:
    """Extension entry point: wire the recording hook + the ``/route`` command.

    Reads its ``models`` price map, ``task`` tag, and ``ledger_name`` from
    ``api.config`` (S40, sliced by this file's stem). ``/route`` reports the
    per-model cost for the current task, and — only after ``ctx.ui.confirm`` (S47) —
    applies the cheapest-model recommendation via ``ctx.set_model`` (S45).
    """
    cfg = api.config
    model_prices = _validate_model_prices(cfg.get("models", {}))
    task = cfg.get("task", DEFAULT_TASK)
    ledger_name = cfg.get("ledger_name", EXTENSION_STEM)

    cost_ledger = ledger.CostLedger(ledger_name)
    router = RouterLedger(cost_ledger=cost_ledger, model_prices=model_prices, task=task)

    async def route_command(args: str, ctx: Any) -> str:
        """`/route` (S46 report + S47-gated S45 reassignment)."""
        stats = route_stats(cost_ledger.records())
        active_model = str(ctx.get_model()["id"])
        lines = [route_body(stats, router.task)]

        rec = recommend_reassignment(stats, router.task, active_model)
        if rec is None:
            lines.append(
                f"No reassignment recommended: {active_model!r} is already the cheapest "
                f"model with a track record on task {router.task!r} (or there is no "
                "priced baseline to compare)."
            )
            return "\n".join(lines)

        lines.append(recommendation_line(rec))
        confirmed = await ctx.ui.confirm(
            "Reassign model?",
            f"Ledger recommends {rec.from_model!r} → {rec.to_model!r} for task "
            f"{rec.task!r} (~{rec.savings_pct:.1f}% cheaper). Apply it (effective "
            "next turn)?",
        )
        if confirmed:
            ctx.set_model(rec.to_model)
            lines.append(f"Reassigned: active model is now {rec.to_model!r} (effective next turn).")
        else:
            lines.append(f"Kept {rec.from_model!r} — no change.")
        return "\n".join(lines)

    api.on("message_end", router.on_message_end)
    api.register_command(
        "route",
        {
            "description": "Report per-(task, model) cost and reassign the model on confirm",
            "handler": route_command,
        },
    )


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/53_router_ledger.py`` → ``getattr(module, "register")``).
register = router_ledger_extension
