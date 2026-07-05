"""Tests for ``examples/53_router_ledger.py`` — cost ledger routes the model (S74).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §7 S74. A composed E11 showcase: the
accreting ``(task-tag, model)`` cost ledger (``ext_kit.ledger`` + S45 usage) becomes
the next turn's model, with a human on the ratify gate (S47 ``ctx.ui.confirm`` →
``ctx.set_model``).

Three layers, mirroring ``test_43_budget_ledger``:

* **full-loop** — only ``stream_simple`` is faked; the fake emits real completions
  whose ``message_end`` carries real ``usage`` + ``model``, so the recording hook
  runs through the genuine loop and each turn lands one ``(task, model)`` record.
  Proves the cell keying + a cross-session RELOAD (fresh ``CostLedger`` over the same
  file reports the same records the router's ``/route`` depends on).
* **command** — ``/route`` (S46 report + S47 confirm) via the real session's
  ``run_extension_command``: a headless ``confirm=yes`` policy applies the
  recommendation through ``ctx.set_model`` (resolver bound); ``confirm=no`` keeps
  the model. The reload half seeds the ledger on disk first, proving the
  recommendation is computed from the PERSISTED ledger, not in-memory run state.
* **pure-unit** — the roll-up, the conservative/Fail-Early recommendation logic, the
  unpriced honesty, and config validation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent
from tau_ai.types import AssistantMessage, Model, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.session_log import InMemorySessionLog

# ── load the example module (its filename is not a valid identifier) ─────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MOD_PATH = _REPO_ROOT / "examples" / "53_router_ledger.py"
_spec = importlib.util.spec_from_file_location("router_ledger_example", _MOD_PATH)
assert _spec is not None and _spec.loader is not None
demo = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = demo
_spec.loader.exec_module(demo)


# ── loop harness (a faked network boundary; everything else is real) ──────────


def _tool_call_assistant(call_id: str, model: str, usage: Usage) -> AssistantMessage:
    """An assistant message with one ``write`` tool call, carrying model + usage."""
    return AssistantMessage(
        content=[
            ToolCall(
                type="toolCall",
                id=call_id,
                name="write",
                arguments={"path": "f.py", "content": "x"},
            )
        ],
        api="openai-completions",
        provider="openai",
        model=model,
        stop_reason="toolUse",
        timestamp=0,
        usage=usage,
    )


class _Stream:
    """Minimal async stream matching the stream_simple contract."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> "_Stream":
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._i]
        self._i += 1
        return event

    async def result(self) -> Any:
        for event in self._events:
            if isinstance(event, DoneEvent):
                return event.final
        return None

    def abort(self) -> None:
        pass


def _make_model(model_id: str = "gpt-4o") -> Model:
    return Model(
        id=model_id,
        name=model_id,
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _make_session(model_id: str = "gpt-4o") -> AgentSession:
    # No tools registered: each `write` call yields an error tool result, so the
    # loop keeps taking turns. Compaction disabled: the fake reports large usage on
    # purpose, which would otherwise trip unrelated auto-compaction machinery.
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=_make_model(model_id),
        extensions=[],
        compaction_settings=CompactionSettings(enabled=False),
    )


def _n_turn_fake(model_id: str, usage: Usage, stop_after: int):
    """A fake ``stream_simple``: tool-call turns until ``stop_after``, then a final stop."""
    calls = {"n": 0}

    async def fake(model, context, options=None):
        calls["n"] += 1
        if calls["n"] >= stop_after:
            final = AssistantMessage(
                content=[{"type": "text", "text": "done"}],
                api="openai-completions",
                provider="openai",
                model=model_id,
                stop_reason="stop",
                timestamp=0,
                usage=usage,
            )
            return _Stream([DoneEvent(final=final, usage=Usage())])
        final = _tool_call_assistant(f"call_{calls['n']}", model_id, usage)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


# ── full-loop: real message_end recording, cross-session reload ───────────────


async def test_records_accrue_per_task_model_through_the_loop(tmp_path) -> None:
    """Each usage-bearing completion lands one ``(task, model)`` record via the loop."""
    usage = Usage(input_tokens=40_000, output_tokens=20_000)  # 60k tokens/turn

    session = _make_session("gpt-4o")
    cost_ledger = demo.ledger.CostLedger("router-loop", base_dir=tmp_path)
    router = demo.RouterLedger(
        cost_ledger=cost_ledger,
        model_prices={"gpt-4o": {"input": 3.0, "output": 15.0}},
        task="refactor",
    )
    api = session._bind_extension_api("examples/53_router_ledger.py")
    api.on("message_end", router.on_message_end)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_n_turn_fake("gpt-4o", usage, stop_after=3),
    ):
        await session.prompt("do the refactor")

    records = cost_ledger.records()
    # Three usage-bearing completions (two tool turns + the final stop turn), each a
    # (refactor, gpt-4o) record; the duplicate tool-turn message_end carries no usage.
    assert len(records) == 3
    assert {r["outcome"] for r in records} == {"refactor"}
    assert {r["model"] for r in records} == {"gpt-4o"}
    assert all(r["tokens"] == 60_000 for r in records)
    assert all(r["usd"] is not None for r in records)

    # Cross-session RELOAD-INVARIANCE: a fresh CostLedger over the same file (as a
    # restarted process would build) rolls up to the identical (task, model) cell.
    reloaded = demo.ledger.CostLedger("router-loop", base_dir=tmp_path)
    stats = demo.route_stats(reloaded.records())
    cell = stats[("refactor", "gpt-4o")]
    assert cell.count == 3
    assert cell.tokens == 180_000
    assert cell.usd is not None and cell.usd == pytest.approx(3 * (3.0 * 0.04 + 15.0 * 0.02))


# ── command: /route report + S47-gated reassignment (S45 set_model) ───────────


def _seed_two_model_ledger(tmp_path) -> demo.ledger.CostLedger:
    """A ledger where task 'refactor' cost far more on gpt-4o than on gpt-4o-mini."""
    cl = demo.ledger.CostLedger("router-cmd", base_dir=tmp_path)
    cl.append(outcome="refactor", model="gpt-4o", tokens=100_000, usd=1.5)  # $0.0150/1k
    cl.append(outcome="refactor", model="gpt-4o-mini", tokens=100_000, usd=0.06)  # $0.0006/1k
    return cl


async def _load_router(session: AgentSession, tmp_path, *, confirm: str) -> None:
    """Load the demo through the real loader with a headless confirm policy + resolver."""
    cfg = {
        "53_router_ledger": {
            "models": {
                "gpt-4o": {"input": 2.5, "output": 10.0},
                "gpt-4o-mini": {"input": 0.15, "output": 0.6},
            },
            "task": "refactor",
            "ledger_name": "router-cmd",
        }
    }
    # The demo's CostLedger("router-cmd") defaults base_dir to ~/.tau/ext-state, and
    # the caller sets HOME=tmp_path, so it resolves to the seeded file on disk.
    result = await session.load_extensions([str(_MOD_PATH)], discover=False, extensions_config=cfg)
    assert result.extensions and not result.errors
    session.set_headless_ui_defaults({"confirm": confirm})
    session.set_model_resolver(lambda name: _make_model(name))


async def test_route_applies_reassignment_on_confirm_yes(tmp_path, monkeypatch) -> None:
    """`/route` recommends the cheaper model and, on confirm=yes, calls set_model."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_two_model_ledger(tmp_path / ".tau" / "ext-state")

    session = _make_session("gpt-4o")
    await _load_router(session, tmp_path, confirm="yes")

    assert session.get_model()["id"] == "gpt-4o"
    result = await session.run_extension_command("route", "")
    assert result.handled
    assert "reassign 'gpt-4o' → 'gpt-4o-mini'" in result.output
    assert "Reassigned: active model is now 'gpt-4o-mini'" in result.output
    # The S45 reassignment took effect: the active model switched by name (S47 gate).
    assert session.get_model()["id"] == "gpt-4o-mini"


async def test_route_keeps_model_on_confirm_no(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_two_model_ledger(tmp_path / ".tau" / "ext-state")

    session = _make_session("gpt-4o")
    await _load_router(session, tmp_path, confirm="no")

    result = await session.run_extension_command("route", "")
    assert "Kept 'gpt-4o' — no change." in result.output
    assert session.get_model()["id"] == "gpt-4o"  # unchanged — the human vetoed


async def test_route_reports_but_recommends_nothing_when_active_is_cheapest(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_two_model_ledger(tmp_path / ".tau" / "ext-state")

    session = _make_session("gpt-4o-mini")  # already on the cheapest model
    await _load_router(session, tmp_path, confirm="yes")

    result = await session.run_extension_command("route", "")
    assert "No reassignment recommended" in result.output
    assert session.get_model()["id"] == "gpt-4o-mini"


# ── pure-unit: roll-up + recommendation logic ─────────────────────────────────


def _stats_from(rows: list[dict[str, Any]]) -> dict[tuple[str, str], Any]:
    return demo.route_stats(rows)


def test_route_stats_groups_by_task_and_model() -> None:
    stats = _stats_from(
        [
            {"outcome": "refactor", "model": "a", "tokens": 100, "usd": 1.0},
            {"outcome": "refactor", "model": "a", "tokens": 100, "usd": 1.0},
            {"outcome": "refactor", "model": "b", "tokens": 200, "usd": 0.5},
            {"outcome": "docs", "model": "a", "tokens": 50, "usd": 0.25},
        ]
    )
    assert stats[("refactor", "a")].count == 2
    assert stats[("refactor", "a")].tokens == 200
    assert stats[("refactor", "a")].usd == pytest.approx(2.0)
    assert stats[("refactor", "a")].usd_per_1k_tokens == pytest.approx(10.0)
    assert stats[("refactor", "b")].usd_per_1k_tokens == pytest.approx(2.5)
    assert set(stats) == {("refactor", "a"), ("refactor", "b"), ("docs", "a")}


def test_route_stats_raises_on_a_record_without_a_model() -> None:
    with pytest.raises(ValueError, match="no 'model' id"):
        _stats_from([{"outcome": "refactor", "tokens": 100, "usd": 1.0}])


def test_recommends_the_strictly_cheaper_model() -> None:
    stats = _stats_from(
        [
            {"outcome": "refactor", "model": "big", "tokens": 100, "usd": 1.0},  # 10/1k
            {"outcome": "refactor", "model": "small", "tokens": 100, "usd": 0.1},  # 1/1k
        ]
    )
    rec = demo.recommend_reassignment(stats, "refactor", "big")
    assert rec is not None
    assert rec.from_model == "big" and rec.to_model == "small"
    assert rec.from_rate == pytest.approx(10.0) and rec.to_rate == pytest.approx(1.0)
    assert rec.savings_pct == pytest.approx(90.0)


def test_no_recommendation_when_active_is_already_cheapest() -> None:
    stats = _stats_from(
        [
            {"outcome": "refactor", "model": "big", "tokens": 100, "usd": 1.0},
            {"outcome": "refactor", "model": "small", "tokens": 100, "usd": 0.1},
        ]
    )
    assert demo.recommend_reassignment(stats, "refactor", "small") is None


def test_no_recommendation_without_a_priced_baseline_for_the_active_model() -> None:
    """A cheaper model exists, but the active model has no priced track record here.

    Fail-Early: without a measured baseline for the active model on this task there
    is no saving to claim, so no recommendation (never a guessed figure).
    """
    stats = _stats_from(
        [{"outcome": "refactor", "model": "small", "tokens": 100, "usd": 0.1}],
    )
    assert demo.recommend_reassignment(stats, "refactor", "big") is None


def test_unpriced_cells_never_participate_in_a_recommendation() -> None:
    """An unpriced (tokens-only) model is 'cost unknown', not the cheapest at $0."""
    stats = _stats_from(
        [
            {"outcome": "refactor", "model": "priced", "tokens": 100, "usd": 1.0},
            {"outcome": "refactor", "model": "unpriced", "tokens": 100, "usd": None},
        ]
    )
    assert stats[("refactor", "unpriced")].usd_per_1k_tokens is None
    # Active 'priced' has no cheaper priced rival → no recommendation (the unpriced
    # one is not treated as a free $0 alternative).
    assert demo.recommend_reassignment(stats, "refactor", "priced") is None


def test_recommendation_only_considers_the_same_task() -> None:
    stats = _stats_from(
        [
            {"outcome": "refactor", "model": "big", "tokens": 100, "usd": 1.0},
            {"outcome": "docs", "model": "small", "tokens": 100, "usd": 0.01},  # cheap, wrong task
        ]
    )
    assert demo.recommend_reassignment(stats, "refactor", "big") is None


# ── pure-unit: pricing honesty (S45 usage → CostLedger) ──────────────────────


def test_on_message_end_prices_and_records_the_completion(tmp_path) -> None:
    cl = demo.ledger.CostLedger("unit", base_dir=tmp_path)
    router = demo.RouterLedger(
        cost_ledger=cl,
        model_prices={"m1": {"input": 3.0, "output": 15.0}},
        task="t",
    )

    class _Evt:
        message = {"model": "m1", "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000}}

    router.on_message_end(_Evt())
    (rec,) = cl.records()
    assert rec["outcome"] == "t" and rec["model"] == "m1"
    assert rec["tokens"] == 2_000_000
    assert rec["usd"] == pytest.approx(18.0)  # 3 + 15


def test_on_message_end_records_unpriced_for_an_unknown_model(tmp_path) -> None:
    cl = demo.ledger.CostLedger("unit2", base_dir=tmp_path)
    router = demo.RouterLedger(cost_ledger=cl, model_prices={}, task="t")

    class _Evt:
        message = {"model": "mystery", "usage": {"input_tokens": 10, "output_tokens": 5}}

    router.on_message_end(_Evt())
    (rec,) = cl.records()
    assert rec["tokens"] == 15
    assert rec["usd"] is None  # unpriced: cost unknown, never a fabricated $0


def test_on_message_end_skips_a_usageless_message_end(tmp_path) -> None:
    cl = demo.ledger.CostLedger("unit3", base_dir=tmp_path)
    router = demo.RouterLedger(cost_ledger=cl, model_prices={}, task="t")

    class _Evt:
        message = {"model": "m1"}  # the duplicate tool-turn message_end (no usage)

    router.on_message_end(_Evt())
    assert cl.records() == []


def test_on_message_end_raises_on_usage_without_a_model(tmp_path) -> None:
    cl = demo.ledger.CostLedger("unit4", base_dir=tmp_path)
    router = demo.RouterLedger(cost_ledger=cl, model_prices={}, task="t")

    class _Evt:
        message = {"usage": {"input_tokens": 10}}

    with pytest.raises(ValueError, match="no 'model'"):
        router.on_message_end(_Evt())


# ── pure-unit: config validation + register ──────────────────────────────────


def test_model_prices_validation_rejects_a_bad_block() -> None:
    with pytest.raises(ValueError, match="must be a mapping or null"):
        demo._validate_model_prices({"m1": "cheap"})


def test_model_prices_validation_allows_null_for_unpriced() -> None:
    prices = demo._validate_model_prices({"m1": None, "m2": {"input": 1.0}})
    assert prices == {"m1": None, "m2": {"input": 1.0}}


def test_router_ledger_requires_a_task_tag(tmp_path) -> None:
    cl = demo.ledger.CostLedger("t", base_dir=tmp_path)
    with pytest.raises(ValueError, match="task tag is required"):
        demo.RouterLedger(cost_ledger=cl, model_prices={}, task="")


def test_router_ledger_extension_registers_the_hook_and_command(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    registered_hooks: list[str] = []
    registered_commands: list[str] = []

    class _RecordingApi:
        config: dict[str, Any] = {"task": "refactor"}

        def on(self, event: str, handler: Any) -> None:
            registered_hooks.append(event)

        def register_command(self, name: str, command: dict[str, Any]) -> None:
            registered_commands.append(name)

    demo.router_ledger_extension(_RecordingApi())
    assert registered_hooks == ["message_end"]
    assert registered_commands == ["route"]
