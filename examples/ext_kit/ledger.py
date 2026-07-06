"""``ext_kit.ledger`` — the *budget / ledger* atom.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §4 S57.

A *ledger* is how an extension answers "what is this run costing, and when do we
stop?". Today ``examples/24_budget.py`` hand-rolls the whole story inline: it
carries a ``cost`` block, sums buckets by hand in ``completion_cost_usd``,
accumulates on ``message_end``, and trips a one-shot ceiling. This module is that
story, factored into four composable pieces on τ's **public** surface only (the
S45 ``ctx.get_usage()`` / ``ctx.get_model()`` accessors + ``~/.tau/config.json``);
it is part of the extension-side kit, **not** the harness:

* :class:`Pricing` — a per-model price block and the one function that turns a
  completion's ``usage`` into dollars. :meth:`Pricing.from_config` formalizes the
  ``config["models"][name]["cost"]`` lookup ``24_budget`` open-codes — one place
  that knows the ``{input, output, cache_read, cache_write}`` USD-per-1M shape and
  the Fail-Early rule (an **absent** block is *unpriced* — tokens only, never a
  fabricated ``$0``; a **present** all-zero block is a real free/local ``$0.00``).
* :class:`UsageMeter` — folds each landed completion's usage (the S45
  ``ctx.get_usage()`` dict, or a ``message_end`` ``event.message["usage"]``) into
  running token + dollar totals. The duplicate tool-turn ``message_end`` that
  carries no ``usage`` contributes nothing (no double-count), matching
  ``BudgetGuard.on_message_end``.
* :class:`CostLedger` — an append-only JSONL of ``{ts, outcome, usd, tokens, …}``
  records under ``~/.tau/ext-state/`` (cross-session, like :class:`ext_kit.state.FileStore`
  but accreting one line per event), with ``$/outcome`` roll-up queries
  (:meth:`CostLedger.by_outcome`, :meth:`CostLedger.total_usd`).
* :class:`Ceiling` — a bang-bang (on/off) controller over a running total: one
  hard ``limit`` plus a soft warn threshold, firing ``on_warn`` once when the
  value crosses the warn line and ``on_stop`` once when it crosses ``limit``. This
  is the generic form of ``BudgetGuard``'s trip, with the warn/stop split pi's
  budget guard lacks.

**Fail-Early.** :meth:`Pricing.from_config` raises ``KeyError`` for a model that
is not in the config (a typo must not silently price at zero); :meth:`Pricing.cost_of`
returns ``None`` — not ``0.0`` — for an unpriced model, so an unpriced meter
reports dollars as *unknown*. :class:`UsageMeter` seeds its running ``usd`` to
``None`` when unpriced and to ``0.0`` only once a price is known. :class:`CostLedger`
lets a corrupt JSONL line raise on read rather than skipping it, and never
fabricates a total: :meth:`CostLedger.total_usd` returns ``None`` when no record
carried a price. :class:`Ceiling` rejects a non-positive limit or a warn threshold
above the limit.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── priced token buckets (pi ``calculateCost`` / backends.compute_cost_usd) ──
#: Usage buckets a ``cost`` block prices, mapped to their price key. ``input`` and
#: ``output`` are always populated; ``cache_read`` is populated on a cache hit.
#: ``cache_write_tokens`` is never populated by today's provider (a real 0), so it
#: carries no price term — the same omission ``compute_cost_usd`` documents.
_PRICED_BUCKETS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "input"),
    ("output_tokens", "output"),
    ("cache_read_tokens", "cache_read"),
)

#: Every token bucket, summed for a completion's raw token count.
_ALL_BUCKETS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def usage_tokens(usage: Mapping[str, Any]) -> int:
    """Total tokens across every bucket of one completion's ``usage`` dict."""
    return sum(int(usage.get(bucket, 0) or 0) for bucket in _ALL_BUCKETS)


# ── pricing ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Pricing:
    """A per-model price block plus the one function that prices a completion.

    ``cost`` is the ``{input, output, cache_read, cache_write}`` block (USD per 1M
    tokens) a ``~/.tau/config.json`` model entry may carry, or ``None`` when the
    model declares no price. The distinction is load-bearing (Fail-Early): a priced
    model yields real dollars from :meth:`cost_of`; an **unpriced** one yields
    ``None`` — the honest "cost unknown", never a made-up ``$0`` — so a meter or
    ledger built on it reports tokens only.
    """

    model: str | None
    cost: dict[str, float] | None

    @classmethod
    def from_config(cls, config: Mapping[str, Any], model_name: str) -> Pricing:
        """Resolve the price block for ``model_name`` from a loaded config.

        Formalizes the ``config["models"][name].get("cost")`` lookup
        ``24_budget`` open-codes. Reads the ``models`` map, requires ``model_name``
        to be present (a missing model raises ``KeyError`` — a typo must not
        silently degrade to unpriced), and takes its optional ``cost`` block. A
        model with no ``cost`` key resolves to an **unpriced** :class:`Pricing`
        (``cost=None``) — tokens-only, by design; a model that is simply absent is
        an error.
        """
        models = config.get("models")
        if not isinstance(models, Mapping) or model_name not in models:
            raise KeyError(
                f"Pricing.from_config: model {model_name!r} is not in config['models'] "
                "— cannot price a model that isn't configured (Fail-Early, no $0 guess)"
            )
        entry = models[model_name]
        cost = entry.get("cost") if isinstance(entry, Mapping) else None
        return cls(model=model_name, cost=dict(cost) if isinstance(cost, Mapping) else None)

    @property
    def priced(self) -> bool:
        """Whether a price block is known (``cost_of`` returns dollars, not ``None``)."""
        return self.cost is not None

    def cost_of(self, usage: Mapping[str, Any]) -> float | None:
        """Dollar cost of one completion's ``usage``, or ``None`` when unpriced.

        Same formula as ``tau_coding_agent.backends.compute_cost_usd`` /
        ``ext_kit.spawn.price_increment``: ``sum(price[k] / 1e6 * tokens[k])`` over
        the priced buckets. An unpriced :class:`Pricing` returns ``None`` (cost
        unknown); a present all-zero block returns a real ``0.0``.
        """
        if self.cost is None:
            return None
        return float(
            sum(
                float(self.cost.get(price_key, 0.0)) / 1_000_000 * int(usage.get(bucket, 0) or 0)
                for bucket, price_key in _PRICED_BUCKETS
            )
        )


# ── metering ──────────────────────────────────────────────────────────────────


@dataclass
class MeterTotals:
    """Running usage totals folded by a :class:`UsageMeter`.

    ``usd`` is ``None`` until (and unless) a price is known — an unpriced meter
    never fabricates a dollar figure. ``completions`` counts the usage-bearing
    completions folded in (not the empty tool-turn ``message_end``).
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    context_tokens: int = 0
    completions: int = 0
    usd: float | None = None

    @property
    def tokens(self) -> int:
        """Total tokens across every bucket."""
        return self.input + self.output + self.cache_read + self.cache_write

    def as_dict(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "context_tokens": self.context_tokens,
            "completions": self.completions,
            "tokens": self.tokens,
            "usd": self.usd,
        }


class UsageMeter:
    """Folds each landed completion's usage into running token + dollar totals.

    The metering half of ``24_budget``'s ``BudgetGuard``, factored out. Construct
    with a :class:`Pricing` (or ``None`` for an unpriced, tokens-only meter);
    :meth:`record` folds one ``usage`` dict (the S45 ``ctx.get_usage()`` shape:
    ``input_tokens`` / ``output_tokens`` / ``cache_read_tokens`` /
    ``cache_write_tokens`` / ``total_tokens``). :meth:`record_message_end` is the
    notify-bus convenience: it pulls ``event.message["usage"]`` and skips the
    duplicate tool-turn ``message_end`` that carries none (returning whether it
    folded anything) — the no-double-count rule ``BudgetGuard`` documents.

    When the meter is priced, running :attr:`totals`.``usd`` starts at ``0.0`` and
    accretes each completion's :meth:`Pricing.cost_of`; unpriced, it stays ``None``.
    """

    def __init__(self, pricing: Pricing | None = None) -> None:
        self._pricing = pricing
        self._totals = MeterTotals(usd=0.0 if (pricing is not None and pricing.priced) else None)

    @property
    def pricing(self) -> Pricing | None:
        return self._pricing

    @property
    def totals(self) -> MeterTotals:
        """The running totals (the live object; treat as read-only)."""
        return self._totals

    @property
    def usd(self) -> float | None:
        """Running dollars spent, or ``None`` when unpriced."""
        return self._totals.usd

    @property
    def tokens(self) -> int:
        """Running total tokens across all completions."""
        return self._totals.tokens

    def record(self, usage: Mapping[str, Any]) -> None:
        """Fold one completion's ``usage`` dict into the running totals.

        Accumulates each token bucket, sets the running context size from
        ``total_tokens``, counts the completion, and — when priced — adds this
        completion's :meth:`Pricing.cost_of` dollars.
        """
        t = self._totals
        t.input += int(usage.get("input_tokens", 0) or 0)
        t.output += int(usage.get("output_tokens", 0) or 0)
        t.cache_read += int(usage.get("cache_read_tokens", 0) or 0)
        t.cache_write += int(usage.get("cache_write_tokens", 0) or 0)
        t.context_tokens = int(usage.get("total_tokens", 0) or 0)
        t.completions += 1
        if self._pricing is not None:
            inc = self._pricing.cost_of(usage)
            if inc is not None and t.usd is not None:
                t.usd += inc

    def record_message_end(self, event: Any) -> bool:
        """Fold a notify ``message_end`` event's usage, if it carries one.

        Reads ``event.message["usage"]``. The per-completion ``message_end`` has a
        ``usage`` dict; the duplicate tool-turn ``message_end`` ``run()`` also
        emits has none and is skipped (no double-count). Returns ``True`` when a
        completion was folded, ``False`` when the event carried no usage.
        """
        message = getattr(event, "message", None)
        if not isinstance(message, Mapping):
            return False
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            return False
        self.record(usage)
        return True


# ── the append-only cost ledger ────────────────────────────────────────────────


def _utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string (the ledger's default timestamp)."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class OutcomeStats:
    """Per-outcome roll-up from :meth:`CostLedger.by_outcome`.

    ``usd`` is ``None`` when *no* record under this outcome carried a price (all
    unpriced) — the honest "cost unknown"; otherwise it is the sum over the priced
    records (unpriced records contribute tokens only, never a fabricated ``$0``).
    """

    count: int = 0
    tokens: int = 0
    usd: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"count": self.count, "tokens": self.tokens, "usd": self.usd}


class CostLedger:
    """An append-only JSONL of cost records with ``$/outcome`` queries.

    Cross-session state — one file, one JSON object per line, accreting a record
    each time an event lands (a spawned child finishes, a turn ends, a run stops).
    Backed by ``<base_dir>/<name>.jsonl`` (default
    ``~/.tau/ext-state/<name>.jsonl``), the same root as :class:`ext_kit.state.FileStore`
    but append-only rather than a single rewritten blob — the right shape for a log
    that only ever grows.

    :meth:`append` writes one record (``{ts, outcome, usd, tokens, …extra}``);
    :meth:`records` reads them all back; :meth:`by_outcome` groups into
    :class:`OutcomeStats`, and :meth:`total_usd` / :meth:`total_tokens` roll up the
    whole ledger. ``usd`` on a record is ``None`` for an unpriced event (from an
    unpriced :class:`Pricing` / :class:`UsageMeter`) — the queries treat that as
    "unknown, not zero" (Fail-Early).
    """

    def __init__(self, name: str, *, base_dir: str | os.PathLike[str] | None = None) -> None:
        if not name:
            raise ValueError("CostLedger: name is required")
        if name != Path(name).name or name in (".", "..") or os.sep in name or "/" in name:
            raise ValueError(
                f"CostLedger: name {name!r} must be a bare filename with no path "
                "separators or '..' (it would escape the state directory)"
            )
        self.name = name
        self._dir = Path(base_dir) if base_dir is not None else Path.home() / ".tau" / "ext-state"
        self.path = self._dir / f"{name}.jsonl"

    def exists(self) -> bool:
        """Whether the ledger file is present on disk."""
        return self.path.exists()

    def append(
        self,
        *,
        outcome: str,
        usd: float | None = None,
        tokens: int = 0,
        model: str | None = None,
        ts: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Append one cost record as a JSONL line and return it.

        ``outcome`` is a caller-defined label (``"success"`` / ``"failure"`` /
        ``"aborted"`` / a task tag). ``usd`` is the event's dollar cost, or
        ``None`` when unpriced (never fabricate a ``$0``). ``ts`` defaults to the
        current UTC ISO time; pass one for deterministic records. The append is a
        single flushed line write (open ``a`` + ``fsync``), the natural atomic unit
        for a growing log.
        """
        if not outcome:
            raise ValueError("CostLedger.append: outcome label is required (Fail-Early)")
        record: dict[str, Any] = {
            "ts": ts if ts is not None else _utcnow_iso(),
            "outcome": outcome,
            "usd": usd,
            "tokens": int(tokens),
        }
        if model is not None:
            record["model"] = model
        record.update(extra)
        self._dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return record

    def records(self) -> list[dict[str, Any]]:
        """Every record in the ledger, oldest first.

        A missing file is an empty ledger (``[]``) — a log that has never been
        written to, not an error. A corrupt line raises ``json.JSONDecodeError``
        rather than being skipped (Fail-Early: a truncated ledger is a real fault a
        caller must see, not silently drop).
        """
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                out.append(json.loads(line))
        return out

    def by_outcome(self) -> dict[str, OutcomeStats]:
        """Roll the ledger up per ``outcome`` label into :class:`OutcomeStats`.

        For each outcome: the record count, total tokens, and total dollars over
        the *priced* records (``None`` when every record under that outcome was
        unpriced — cost unknown, not zero).
        """
        stats: dict[str, OutcomeStats] = {}
        for rec in self.records():
            key = str(rec.get("outcome"))
            s = stats.setdefault(key, OutcomeStats())
            s.count += 1
            s.tokens += int(rec.get("tokens", 0) or 0)
            usd = rec.get("usd")
            if usd is not None:
                s.usd = (s.usd or 0.0) + float(usd)
        return stats

    def total_usd(self) -> float | None:
        """Total dollars over every priced record, or ``None`` when none was priced.

        Unpriced records (``usd is None``) contribute tokens only; if *no* record
        carried a price the total is unknown (``None``), never a fabricated ``0.0``.
        """
        total: float | None = None
        for rec in self.records():
            usd = rec.get("usd")
            if usd is not None:
                total = (total or 0.0) + float(usd)
        return total

    def total_tokens(self) -> int:
        """Total tokens across every record."""
        return sum(int(rec.get("tokens", 0) or 0) for rec in self.records())


# ── the bang-bang ceiling controller ───────────────────────────────────────────

#: Ceiling states, low → high. ``OK`` below the warn line, ``WARN`` at/above warn
#: but below ``limit``, ``STOPPED`` at/above ``limit`` (terminal, latching).
CEILING_OK = "ok"
CEILING_WARN = "warn"
CEILING_STOPPED = "stopped"

#: Default warn threshold as a fraction of ``limit`` when neither ``warn_at`` nor
#: ``warn_ratio`` is given. A documented knob (the soft-alarm point), not a
#: fallback — override with ``warn_ratio=`` or an absolute ``warn_at=``.
DEFAULT_WARN_RATIO = 0.8


class Ceiling:
    """A bang-bang (on/off) controller over a running total, with warn + stop lines.

    The generalized form of ``24_budget``'s one-shot trip: feed it the running
    value (dollars from :attr:`UsageMeter.usd`, tokens, turns — whatever you
    threshold on) via :meth:`update`, and it fires ``on_warn`` once when the value
    first crosses the soft warn line and ``on_stop`` once when it crosses the hard
    ``limit``. "Bang-bang" because the output is discrete state, not a proportional
    signal: :attr:`state` walks ``ok → warn → stopped`` and latches — it never
    steps back down (spend is monotonic; a stopped run stays stopped).

    ``on_warn`` / ``on_stop`` are optional: with neither wired, the controller is
    still fully usable by polling :attr:`state` / :attr:`tripped` / :attr:`warned`
    (they are the authoritative record; a callback is just a side effect on the
    crossing). Each callback receives the crossing value.

    The warn threshold is ``warn_at`` if given (absolute), else ``limit *
    warn_ratio`` (:data:`DEFAULT_WARN_RATIO` by default). A value that leaps past
    both lines in one :meth:`update` fires ``on_warn`` then ``on_stop`` in order —
    both crossings really happened.
    """

    def __init__(
        self,
        limit: float,
        on_warn: Callable[[float], None] | None = None,
        on_stop: Callable[[float], None] | None = None,
        *,
        warn_at: float | None = None,
        warn_ratio: float = DEFAULT_WARN_RATIO,
    ) -> None:
        if limit <= 0:
            raise ValueError(f"Ceiling: limit must be positive, got {limit!r}")
        if warn_at is not None:
            if warn_at < 0:
                raise ValueError(f"Ceiling: warn_at must be non-negative, got {warn_at!r}")
            resolved_warn = float(warn_at)
        else:
            if not (0.0 < warn_ratio <= 1.0):
                raise ValueError(f"Ceiling: warn_ratio must be in (0, 1], got {warn_ratio!r}")
            resolved_warn = float(limit) * warn_ratio
        if resolved_warn > limit:
            raise ValueError(
                f"Ceiling: warn threshold {resolved_warn!r} exceeds limit {limit!r} "
                "(the soft alarm must fire at or before the hard stop)"
            )
        self.limit = float(limit)
        self.warn_at = resolved_warn
        self._on_warn = on_warn
        self._on_stop = on_stop
        self._state = CEILING_OK
        self._value = 0.0

    @property
    def state(self) -> str:
        """Current state: :data:`CEILING_OK` / :data:`CEILING_WARN` / :data:`CEILING_STOPPED`."""
        return self._state

    @property
    def value(self) -> float:
        """The most recent value passed to :meth:`update`."""
        return self._value

    @property
    def warned(self) -> bool:
        """Whether the warn line has been crossed (state is ``warn`` or ``stopped``)."""
        return self._state in (CEILING_WARN, CEILING_STOPPED)

    @property
    def tripped(self) -> bool:
        """Whether the hard ``limit`` has been crossed (state is ``stopped``)."""
        return self._state == CEILING_STOPPED

    def update(self, value: float) -> str:
        """Feed the running total; fire callbacks for any newly-crossed line.

        Advances :attr:`state` monotonically (``ok → warn → stopped``) and, for
        each threshold newly crossed by this value, fires the matching callback
        exactly once — ``on_warn`` at :attr:`warn_at`, ``on_stop`` at :attr:`limit`.
        A single leap past both lines fires both, warn before stop. Returns the new
        state. The state never steps back down even if ``value`` decreases (the
        crossings already happened / latched).
        """
        self._value = float(value)
        if self._state != CEILING_STOPPED and value >= self.warn_at and self._state == CEILING_OK:
            self._state = CEILING_WARN
            if self._on_warn is not None:
                self._on_warn(self._value)
        if self._state != CEILING_STOPPED and value >= self.limit:
            self._state = CEILING_STOPPED
            if self._on_stop is not None:
                self._on_stop(self._value)
        return self._state
