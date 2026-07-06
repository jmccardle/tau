"""Tests for ``examples/ext_kit/ledger.py`` — the S57 *budget / ledger* primitive.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §4 S57.

Four atoms, four stories:

* **Pricing** — ``from_config`` resolves a model's ``cost`` block (missing model
  raises; a model with no ``cost`` is unpriced), and ``cost_of`` prices one
  completion's usage (unpriced → ``None``, present all-zero → real ``0.0``) with
  the same formula as ``backends.compute_cost_usd``.
* **UsageMeter** — folds ``message_end`` / S45 usage into running token + dollar
  totals; the empty tool-turn ``message_end`` contributes nothing; unpriced keeps
  ``usd`` at ``None`` (never a fabricated ``$0``).
* **CostLedger** — append-only JSONL with ``$/outcome`` roll-ups, the
  unknown-vs-zero (Fail-Early) dollar rule, corrupt-line raise, and — the headline
  — a RELOAD/reopen-INVARIANCE proof (persist → new ledger over the same file →
  records + totals survive, à la S29/S56).
* **Ceiling** — the bang-bang ok→warn→stopped walk, one-shot warn/stop callbacks,
  the single-leap-past-both case, latching, and constructor Fail-Early guards.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# ── import the kit as a top-level package (examples/ on the path) ────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = str(_REPO_ROOT / "examples")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)

from ext_kit import ledger  # noqa: E402  (path insertion must precede the import)


# ── event stand-in (mirrors the notify AgentEvent .message shape) ────────────


class _Event:
    """Minimal notify-event stand-in exposing ``.message`` (like ``AgentEvent``)."""

    def __init__(self, message: Any) -> None:
        self.message = message


def _usage_event(**buckets: int) -> _Event:
    return _Event({"role": "assistant", "content": [], "usage": dict(buckets)})


# ── Pricing.from_config ───────────────────────────────────────────────────────


def test_pricing_from_config_reads_cost_block() -> None:
    config = {
        "models": {
            "gpt-4o": {"backend": "openai", "cost": {"input": 3.0, "output": 15.0}},
        }
    }
    pricing = ledger.Pricing.from_config(config, "gpt-4o")
    assert pricing.model == "gpt-4o"
    assert pricing.priced is True
    assert pricing.cost == {"input": 3.0, "output": 15.0}


def test_pricing_from_config_model_without_cost_is_unpriced() -> None:
    # A present model with no ``cost`` key resolves to unpriced (tokens-only), not
    # an error — the honest local/free-model case.
    config = {"models": {"local-llm": {"backend": "openai"}}}
    pricing = ledger.Pricing.from_config(config, "local-llm")
    assert pricing.priced is False
    assert pricing.cost is None
    assert pricing.cost_of({"input_tokens": 1000}) is None


def test_pricing_from_config_missing_model_raises() -> None:
    # Fail-Early: a typo'd / absent model must not silently price at $0.
    with pytest.raises(KeyError):
        ledger.Pricing.from_config({"models": {}}, "nope")
    with pytest.raises(KeyError):
        ledger.Pricing.from_config({}, "nope")


def test_pricing_cost_of_matches_compute_cost_formula() -> None:
    # sum(price[k] / 1e6 * tokens[k]) over input/output/cache_read.
    pricing = ledger.Pricing(model="m", cost={"input": 3.0, "output": 15.0, "cache_read": 0.3})
    usage = {"input_tokens": 1_000_000, "output_tokens": 2_000_000, "cache_read_tokens": 1_000_000}
    # 3.0 + 30.0 + 0.3 = 33.3
    assert pricing.cost_of(usage) == pytest.approx(33.3)


def test_pricing_cost_of_all_zero_block_is_real_zero() -> None:
    # A present all-zero price is a genuine free model → 0.0, distinct from None.
    pricing = ledger.Pricing(model="free", cost={"input": 0.0, "output": 0.0})
    assert pricing.cost_of({"input_tokens": 10_000, "output_tokens": 10_000}) == 0.0


def test_pricing_cost_of_ignores_cache_write() -> None:
    # cache_write is inert (never populated by today's provider); no price term.
    pricing = ledger.Pricing(model="m", cost={"input": 2.0, "cache_write": 99.0})
    usage = {"input_tokens": 1_000_000, "cache_write_tokens": 1_000_000}
    assert pricing.cost_of(usage) == pytest.approx(2.0)


def test_usage_tokens_sums_all_buckets() -> None:
    usage = {
        "input_tokens": 10,
        "output_tokens": 8,
        "cache_read_tokens": 5,
        "cache_write_tokens": 15,
    }
    assert ledger.usage_tokens(usage) == 38


# ── UsageMeter ────────────────────────────────────────────────────────────────


def test_usage_meter_priced_accumulates_tokens_and_dollars() -> None:
    pricing = ledger.Pricing(model="m", cost={"input": 3.0, "output": 15.0})
    meter = ledger.UsageMeter(pricing)
    assert meter.usd == 0.0  # priced → seeded to 0.0
    meter.record({"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    meter.record({"input_tokens": 1_000_000, "output_tokens": 0})
    # tokens: 3_000_000; usd: (3+15) + (3+0) = 21.0
    assert meter.tokens == 3_000_000
    assert meter.usd == pytest.approx(21.0)
    assert meter.totals.completions == 2


def test_usage_meter_unpriced_keeps_usd_none() -> None:
    meter = ledger.UsageMeter()  # no pricing → tokens-only
    assert meter.usd is None
    meter.record({"input_tokens": 500, "output_tokens": 250})
    assert meter.tokens == 750
    assert meter.usd is None  # never fabricates a dollar figure


def test_usage_meter_unpriced_pricing_object_keeps_usd_none() -> None:
    # An explicit unpriced Pricing behaves like no pricing for dollars.
    meter = ledger.UsageMeter(ledger.Pricing(model="local", cost=None))
    meter.record({"input_tokens": 1000})
    assert meter.usd is None
    assert meter.tokens == 1000


def test_usage_meter_records_context_from_total_tokens() -> None:
    meter = ledger.UsageMeter()
    meter.record({"input_tokens": 10, "total_tokens": 4321})
    assert meter.totals.context_tokens == 4321


def test_usage_meter_record_message_end_folds_and_reports() -> None:
    meter = ledger.UsageMeter(ledger.Pricing(model="m", cost={"input": 1.0}))
    folded = meter.record_message_end(_usage_event(input_tokens=1_000_000))
    assert folded is True
    assert meter.usd == pytest.approx(1.0)
    assert meter.totals.completions == 1


def test_usage_meter_message_end_without_usage_contributes_nothing() -> None:
    # The duplicate tool-turn message_end run() emits carries no "usage".
    meter = ledger.UsageMeter()
    assert meter.record_message_end(_Event({"role": "assistant", "content": []})) is False
    assert meter.record_message_end(_Event(None)) is False
    assert meter.tokens == 0
    assert meter.totals.completions == 0


# ── CostLedger ────────────────────────────────────────────────────────────────


def _ledger(tmp_path: Path, name: str = "costs") -> "ledger.CostLedger":
    return ledger.CostLedger(name, base_dir=tmp_path)


def test_cost_ledger_append_and_read_round_trip(tmp_path: Path) -> None:
    cl = _ledger(tmp_path)
    rec = cl.append(
        outcome="success", usd=0.25, tokens=1000, model="gpt-4o", ts="2026-07-05T00:00:00+00:00"
    )
    assert rec["outcome"] == "success"
    assert rec["usd"] == 0.25
    assert rec["model"] == "gpt-4o"
    records = cl.records()
    assert len(records) == 1
    assert records[0] == rec


def test_cost_ledger_extra_fields_persist(tmp_path: Path) -> None:
    cl = _ledger(tmp_path)
    cl.append(outcome="success", usd=0.1, tokens=10, task_tag="refactor")
    assert cl.records()[0]["task_tag"] == "refactor"


def test_cost_ledger_missing_file_is_empty(tmp_path: Path) -> None:
    cl = _ledger(tmp_path)
    assert cl.exists() is False
    assert cl.records() == []
    assert cl.total_usd() is None  # nothing priced → unknown, not 0.0
    assert cl.total_tokens() == 0


def test_cost_ledger_append_requires_outcome(tmp_path: Path) -> None:
    cl = _ledger(tmp_path)
    with pytest.raises(ValueError):
        cl.append(outcome="", usd=1.0)


def test_cost_ledger_by_outcome_rollup(tmp_path: Path) -> None:
    cl = _ledger(tmp_path)
    cl.append(outcome="success", usd=0.10, tokens=100)
    cl.append(outcome="success", usd=0.20, tokens=200)
    cl.append(outcome="failure", usd=0.05, tokens=50)
    stats = cl.by_outcome()
    assert stats["success"].count == 2
    assert stats["success"].tokens == 300
    assert stats["success"].usd == pytest.approx(0.30)
    assert stats["failure"].usd == pytest.approx(0.05)
    assert cl.total_usd() == pytest.approx(0.35)
    assert cl.total_tokens() == 350


def test_cost_ledger_unpriced_records_are_unknown_not_zero(tmp_path: Path) -> None:
    cl = _ledger(tmp_path)
    cl.append(outcome="local", usd=None, tokens=500)
    cl.append(outcome="local", usd=None, tokens=250)
    stats = cl.by_outcome()
    assert stats["local"].usd is None  # all unpriced → unknown
    assert stats["local"].tokens == 750
    assert cl.total_usd() is None
    assert cl.total_tokens() == 750


def test_cost_ledger_mixed_priced_unpriced_sums_priced_only(tmp_path: Path) -> None:
    cl = _ledger(tmp_path)
    cl.append(outcome="mix", usd=0.40, tokens=100)
    cl.append(outcome="mix", usd=None, tokens=100)  # unpriced contributes tokens only
    stats = cl.by_outcome()
    assert stats["mix"].usd == pytest.approx(0.40)
    assert stats["mix"].tokens == 200
    assert cl.total_usd() == pytest.approx(0.40)


def test_cost_ledger_corrupt_line_raises(tmp_path: Path) -> None:
    cl = _ledger(tmp_path)
    cl.append(outcome="ok", usd=0.1, tokens=1)
    with open(cl.path, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    with pytest.raises(json.JSONDecodeError):
        cl.records()


def test_cost_ledger_name_traversal_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ledger.CostLedger("../escape", base_dir=tmp_path)
    with pytest.raises(ValueError):
        ledger.CostLedger("a/b", base_dir=tmp_path)
    with pytest.raises(ValueError):
        ledger.CostLedger("")


def test_cost_ledger_reload_invariance(tmp_path: Path) -> None:
    # Persist through one CostLedger, then reconstruct through a FRESH one over the
    # same file (the cross-session reopen): records + roll-ups survive (S56/S29).
    cl = _ledger(tmp_path, "spend")
    cl.append(outcome="success", usd=0.25, tokens=1000, ts="t1")
    cl.append(outcome="failure", usd=0.05, tokens=200, ts="t2")

    reopened = ledger.CostLedger("spend", base_dir=tmp_path)
    records = reopened.records()
    assert [r["ts"] for r in records] == ["t1", "t2"]
    assert reopened.total_usd() == pytest.approx(0.30)
    assert reopened.total_tokens() == 1200
    assert reopened.by_outcome()["success"].usd == pytest.approx(0.25)

    # A further append through the reopened handle accretes, not truncates.
    reopened.append(outcome="success", usd=0.10, tokens=100, ts="t3")
    assert len(ledger.CostLedger("spend", base_dir=tmp_path).records()) == 3


# ── Ceiling (bang-bang controller) ────────────────────────────────────────────


def test_ceiling_walks_ok_warn_stopped() -> None:
    warns: list[float] = []
    stops: list[float] = []
    c = ledger.Ceiling(10.0, warns.append, stops.append, warn_at=8.0)
    assert c.state == ledger.CEILING_OK
    assert c.update(5.0) == ledger.CEILING_OK
    assert warns == [] and stops == []
    assert c.update(8.0) == ledger.CEILING_WARN
    assert c.warned is True and c.tripped is False
    assert warns == [8.0]
    assert c.update(10.0) == ledger.CEILING_STOPPED
    assert c.tripped is True
    assert stops == [10.0]


def test_ceiling_callbacks_are_one_shot() -> None:
    warns: list[float] = []
    stops: list[float] = []
    c = ledger.Ceiling(10.0, warns.append, stops.append, warn_at=8.0)
    c.update(8.0)
    c.update(9.0)  # still in WARN — no second warn callback
    c.update(10.0)
    c.update(20.0)  # still STOPPED — no second stop callback
    assert warns == [8.0]
    assert stops == [10.0]


def test_ceiling_single_leap_past_both_fires_warn_then_stop() -> None:
    order: list[str] = []
    c = ledger.Ceiling(
        10.0,
        lambda v: order.append("warn"),
        lambda v: order.append("stop"),
        warn_at=8.0,
    )
    assert c.update(50.0) == ledger.CEILING_STOPPED
    assert order == ["warn", "stop"]


def test_ceiling_state_latches_when_value_drops() -> None:
    c = ledger.Ceiling(10.0, warn_at=8.0)
    c.update(10.0)
    assert c.tripped is True
    c.update(0.0)  # spend can't really drop, but state must latch regardless
    assert c.tripped is True
    assert c.state == ledger.CEILING_STOPPED


def test_ceiling_warn_ratio_default() -> None:
    c = ledger.Ceiling(100.0)  # default warn_ratio 0.8 → warn at 80
    assert c.warn_at == pytest.approx(80.0)
    assert c.update(80.0) == ledger.CEILING_WARN


def test_ceiling_works_without_callbacks() -> None:
    # Pure polling usage — no callbacks wired, state is authoritative.
    c = ledger.Ceiling(10.0, warn_at=5.0)
    c.update(5.0)
    assert c.warned is True
    c.update(10.0)
    assert c.tripped is True


def test_ceiling_rejects_bad_construction() -> None:
    with pytest.raises(ValueError):
        ledger.Ceiling(0.0)  # non-positive limit
    with pytest.raises(ValueError):
        ledger.Ceiling(-5.0)
    with pytest.raises(ValueError):
        ledger.Ceiling(10.0, warn_at=20.0)  # warn above limit
    with pytest.raises(ValueError):
        ledger.Ceiling(10.0, warn_ratio=1.5)  # ratio out of (0, 1]
    with pytest.raises(ValueError):
        ledger.Ceiling(10.0, warn_ratio=0.0)


def test_ceiling_metered_dollars_integration() -> None:
    # The realistic wiring: fold usage through a meter, feed running usd to the
    # ceiling. 24_budget's trip, generalized with a warn line.
    pricing = ledger.Pricing(model="m", cost={"input": 3.0, "output": 15.0})
    meter = ledger.UsageMeter(pricing)
    stopped: list[float] = []
    c = ledger.Ceiling(5.0, warn_at=4.0, on_stop=stopped.append)
    for _ in range(3):
        meter.record({"input_tokens": 100_000, "output_tokens": 100_000})  # $1.80/turn
        c.update(meter.usd or 0.0)
    # after 3 turns: $5.40 ≥ 5.0 → stopped
    assert meter.usd == pytest.approx(5.4)
    assert c.tripped is True
    assert stopped == [pytest.approx(5.4)]
