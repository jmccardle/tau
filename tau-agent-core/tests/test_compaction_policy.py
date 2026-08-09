"""H5 — the declared compaction policy, its proof, and the latency partition.

Reference: SIM_SPEC_v2.md §16.8, §16.11 (H5), §5.2, §9 rule 1, §11.1, §7.6, §7.7.

What these tests are defending, stated once so a later reader does not have to
reconstruct it from assertions:

* A measurement run must **declare** what happens when the context fills up.
  "Leave the default" is removed by there being no default to leave.
* A ``turn_cap`` policy claims compaction cannot be reached. That claim is
  **proved**, not estimated: the arithmetic is checked at construction and both
  premises are checked at runtime, and the tests below break each one.
* A compaction-bearing prompt never lands in §5.2's headline latency population.
* **Nothing about τ's compaction gets quieter.** A session with no declared policy
  behaves exactly as shipped, and a failed compaction still raises. Several tests
  here exist only to pin that.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import (
    DEFAULT_COMPACTION_SETTINGS,
    CompactionError,
    CompactionSettings,
)
from tau_agent_core.compaction_policy import (
    SCENARIO_POLICY_MODES,
    CompactionPolicy,
    CompactionPolicyError,
    CompactionPolicyViolation,
    policy_for_scenario,
)
from tau_agent_core.events import AgentEvent
from tau_agent_core.latency import PromptLatencyCollector, summarize
from tau_agent_core.run_manifest import (
    HARNESS,
    build_run_manifest,
    require_compaction_policy,
    write_run_manifest,
)
from tau_agent_core.session_log import InMemorySessionLog
from tau_ai.types import AssistantMessage, Model, TextContent, Usage

# ── shared fakes ──────────────────────────────────────────────────────────
#
# The only thing stubbed is the network boundary. `run_compaction`,
# `prepare_compaction`, the cut-point search, `append_compaction` and the whole
# event path are the real ones — otherwise a test of "did a compaction happen"
# would be a test of the stub.


def _model(model_id: str = "session-model", context_window: int = 128_000) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        api="openai-completions",
        provider="openai",
        base_url="https://example.invalid/v1",
        context_window=context_window,
        max_tokens=4096,
    )


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="session-model",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _EventIterator:
    def __init__(self, events: list) -> None:
        self._events = events
        self._i = 0

    def __aiter__(self) -> _EventIterator:
        return self

    async def __anext__(self):
        if self._i >= len(self._events):
            raise StopAsyncIteration
        self._i += 1
        return self._events[self._i - 1]


class _Stream:
    def __init__(self, events: list) -> None:
        self._events = events

    def __aiter__(self) -> _EventIterator:
        return _EventIterator(self._events)

    async def result(self):
        from tau_ai.streaming import DoneEvent

        for event in self._events:
            if isinstance(event, DoneEvent):
                return event.final
        return None

    def abort(self) -> None:
        pass


def _stream_stub(reply: str = "ok", record: list | None = None):
    async def fake_stream_simple(model, context, options=None):
        from tau_ai.streaming import DoneEvent, TextDeltaEvent

        if record is not None:
            record.append(model)
        return _Stream(
            [
                TextDeltaEvent(delta=reply, partial=_assistant(reply)),
                DoneEvent(
                    final=_assistant(reply),
                    usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
                ),
            ]
        )

    return fake_stream_simple


def _summarizer_stub(record: list | None = None, delay_s: float = 0.0):
    """Stands in for the ONE network call `run_compaction` makes."""

    async def fake_complete_simple(*args, **kwargs):
        if record is not None:
            record.append((args, kwargs))
        if delay_s:
            await asyncio.sleep(delay_s)
        return _assistant("SUMMARY")

    return fake_complete_simple


def _session(policy: CompactionPolicy | None = None, **kwargs) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=kwargs.pop("model", _model()),
        api_key=kwargs.pop("api_key", "session-key"),
        compaction_policy=policy,
        **kwargs,
    )


#: The prompt every "and then it compacts" test below sends SECOND. Patching
#: ``should_compact`` to True is not by itself enough to make a compaction
#: happen: the cut still has to have something on the far side of it, and the
#: shipped ``keep_recent_tokens`` is 20000, so a conversation of two-word prompts
#: produces a cut that keeps everything. ``prepare_compaction`` reports that
#: honestly as "nothing to compact" (``None``) rather than spending a completion
#: to summarise an empty ``<conversation>`` — so a test that wants the summariser
#: reached has to supply a conversation the cut can actually bite into. The
#: padding rides on the SECOND prompt so the cut lands on it and the first turn
#: is what gets summarised (~25000 estimated tokens at ~4 chars/token).
_COMPACTING_PROMPT = "two " + "x" * 100_000


# ══════════════════════════════════════════════════════════════════════════
# Deliverable 1a — the declaration itself
# ══════════════════════════════════════════════════════════════════════════


class TestPolicyDeclarationIsAdmissibleOrRefused:
    def test_there_is_no_default_policy(self):
        # The mechanism by which "leave the default" stops being an option is that
        # there is nothing to leave: `mode` has no default, so a bare
        # CompactionPolicy() cannot be constructed at all.
        with pytest.raises(TypeError):
            CompactionPolicy()  # type: ignore[call-arg]

    def test_exactly_three_modes_are_admissible(self):
        with pytest.raises(CompactionPolicyError, match="unknown compaction policy mode"):
            CompactionPolicy(mode="whatever_the_harness_does")  # type: ignore[arg-type]

    def test_disabled_requires_a_turn_bound(self):
        with pytest.raises(CompactionPolicyError, match="requires max_turns"):
            CompactionPolicy(mode="disabled")

    def test_turn_cap_requires_both_bounds(self):
        with pytest.raises(CompactionPolicyError, match="requires max_tokens_per_turn"):
            CompactionPolicy(mode="turn_cap", max_turns=5)
        with pytest.raises(CompactionPolicyError, match="requires max_turns"):
            CompactionPolicy(mode="turn_cap", max_tokens_per_turn=5)

    def test_local_summarizer_requires_model_and_key(self):
        with pytest.raises(CompactionPolicyError, match="requires summarizer_model"):
            CompactionPolicy(mode="local_summarizer")
        with pytest.raises(CompactionPolicyError, match="requires summarizer_api_key"):
            CompactionPolicy(mode="local_summarizer", summarizer_model=_model())

    def test_a_field_the_mode_does_not_read_is_refused(self):
        # The same failure H1/B2/B5 kept producing: a value that is accepted and
        # consulted nowhere. Refused at declaration rather than ignored.
        with pytest.raises(CompactionPolicyError, match="does not use max_tokens_per_turn"):
            CompactionPolicy(mode="disabled", max_turns=3, max_tokens_per_turn=100)
        with pytest.raises(CompactionPolicyError, match="does not use max_turns"):
            CompactionPolicy(
                mode="local_summarizer",
                summarizer_model=_model(),
                summarizer_api_key="k",
                max_turns=3,
            )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"mode": "disabled", "max_turns": 0},
            {"mode": "disabled", "max_turns": -1},
            {"mode": "turn_cap", "max_turns": 4, "max_tokens_per_turn": 0},
        ],
    )
    def test_non_positive_bounds_are_refused(self, kwargs):
        with pytest.raises(CompactionPolicyError, match="must be positive"):
            CompactionPolicy(**kwargs)

    def test_constructors_produce_the_declared_mode(self):
        assert CompactionPolicy.disabled(max_turns=3).mode == "disabled"
        assert CompactionPolicy.turn_cap(max_turns=3, max_tokens_per_turn=9).mode == "turn_cap"
        local = CompactionPolicy.local_summarizer(model=_model("board"), api_key="local")
        assert local.mode == "local_summarizer"
        assert local.summarizer_model is not None and local.summarizer_model.id == "board"

    def test_only_disabled_switches_compaction_off(self):
        # turn_cap deliberately leaves the shipped mechanism ENABLED — the threshold
        # is kept out of reach by the budget, not by switching compaction off, so
        # the measured system stays the shipped system.
        assert CompactionPolicy.disabled(max_turns=3).compaction_settings.enabled is False
        assert (
            CompactionPolicy.turn_cap(
                max_turns=3, max_tokens_per_turn=9
            ).compaction_settings.enabled
            is True
        )
        assert (
            CompactionPolicy.local_summarizer(
                model=_model(), api_key="k"
            ).compaction_settings.enabled
            is True
        )


# ══════════════════════════════════════════════════════════════════════════
# Deliverable 1b — a declared policy PER SCENARIO
# ══════════════════════════════════════════════════════════════════════════


class TestEveryScenarioDeclaresAPolicy:
    def test_all_five_lettered_scenarios_are_declared(self):
        assert set(SCENARIO_POLICY_MODES) == {"A", "B", "C", "D", "E"}

    def test_every_declared_mode_is_one_of_the_three_admissible(self):
        assert set(SCENARIO_POLICY_MODES.values()) <= {
            "disabled",
            "local_summarizer",
            "turn_cap",
        }

    def test_the_partition_scenarios_do_not_declare_a_remote_summariser(self):
        # §11.1: a partition removes the bus, JMFTS *and* the LLM. D and E must not
        # depend on a model call to survive their own partition.
        for scenario in ("D", "E"):
            assert SCENARIO_POLICY_MODES[scenario] != "local_summarizer"

    def test_an_undeclared_scenario_raises_rather_than_defaulting(self):
        with pytest.raises(CompactionPolicyError, match="no compaction policy is declared"):
            policy_for_scenario("Z", max_turns=4, max_tokens_per_turn=1000)

    def test_scenario_letters_are_case_insensitive(self):
        assert (
            policy_for_scenario("d", max_turns=4, max_tokens_per_turn=1000).mode
            == policy_for_scenario("D", max_turns=4, max_tokens_per_turn=1000).mode
        )

    def test_the_numbers_are_required_of_the_caller(self):
        # This module refuses to invent a per-turn token bound; an invented one
        # turns the turn_cap proof back into the estimate §16.8 rejects.
        with pytest.raises(TypeError):
            policy_for_scenario("D")  # type: ignore[call-arg]

    def test_the_table_and_the_builder_cannot_drift_apart_silently(self):
        with patch.dict(SCENARIO_POLICY_MODES, {"D": "disabled"}):
            with pytest.raises(CompactionPolicyError, match="cannot construct"):
                policy_for_scenario("D", max_turns=4, max_tokens_per_turn=1000)


# ══════════════════════════════════════════════════════════════════════════
# Deliverable 1c — "PROVABLY cannot reach the threshold" means a test
# ══════════════════════════════════════════════════════════════════════════


class TestTurnCapBudgetArithmetic:
    """The construction-time half of the proof: max_turns x per_turn <= budget."""

    def test_a_budget_that_does_not_close_is_refused(self):
        model = _model(context_window=40_000)  # budget = 40_000 - 16_384 = 23_616
        policy = CompactionPolicy.turn_cap(max_turns=10, max_tokens_per_turn=3_000)
        with pytest.raises(CompactionPolicyError, match="budget does not close"):
            policy.bind_to(model)

    def test_the_boundary_is_inclusive_and_one_token_over_is_refused(self):
        model = _model(context_window=40_000)
        budget = 40_000 - DEFAULT_COMPACTION_SETTINGS.reserve_tokens
        assert budget == 23_616
        # Exactly the budget: admissible.
        CompactionPolicy.turn_cap(max_turns=1, max_tokens_per_turn=budget).bind_to(model)
        # One token more: refused. If this ever passes, the "cannot reach" claim is
        # off by exactly the amount that makes it false.
        with pytest.raises(CompactionPolicyError, match="budget does not close"):
            CompactionPolicy.turn_cap(max_turns=1, max_tokens_per_turn=budget + 1).bind_to(model)

    def test_a_model_whose_window_is_under_the_reserve_is_refused(self):
        with pytest.raises(CompactionPolicyError, match="not\\s+greater than reserve_tokens"):
            CompactionPolicy.turn_cap(max_turns=1, max_tokens_per_turn=1).bind_to(
                _model(context_window=1_000)
            )

    def test_the_session_refuses_to_construct_on_an_unprovable_budget(self):
        # The refusal is at session construction, not at turn 40 of a scenario run.
        with pytest.raises(CompactionPolicyError, match="budget does not close"):
            _session(
                CompactionPolicy.turn_cap(max_turns=10, max_tokens_per_turn=3_000),
                model=_model(context_window=40_000),
            )

    def test_switching_model_rechecks_the_proof(self):
        # A policy proven against a 128k window is not proven against a 40k one, and
        # ctx.set_model() would otherwise invalidate it silently.
        big, small = _model("big", 128_000), _model("small", 40_000)
        session = _session(
            CompactionPolicy.turn_cap(max_turns=10, max_tokens_per_turn=3_000), model=big
        )
        session.set_model_resolver(lambda name: small)
        with pytest.raises(CompactionPolicyError, match="budget does not close"):
            session.set_model("small")
        assert session._model is big, "the invalid switch must not take effect"


class TestTurnCapPremisesAreEnforcedAtRuntime:
    """The runtime half: a proof whose premises are assumed is an estimate."""

    def test_p2_turn_bound_raises_past_the_cap(self):
        policy = CompactionPolicy.turn_cap(max_turns=2, max_tokens_per_turn=100)
        policy.admit_turn(1)
        policy.admit_turn(2)
        with pytest.raises(CompactionPolicyViolation, match="max_turns=2"):
            policy.admit_turn(3)

    def test_p1_per_turn_bound_raises_when_exceeded(self):
        policy = CompactionPolicy.turn_cap(max_turns=4, max_tokens_per_turn=100)
        policy.observe_context(turns_used=2, context_tokens=200, context_window=128_000)
        with pytest.raises(CompactionPolicyViolation, match="premise failed"):
            policy.observe_context(turns_used=2, context_tokens=201, context_window=128_000)

    def test_the_claim_itself_is_checked_not_only_its_premise(self):
        # Belt and braces: even if the per-turn arithmetic were satisfied, crossing
        # the actual compaction threshold voids the run.
        policy = CompactionPolicy.turn_cap(max_turns=1, max_tokens_per_turn=1_000_000)
        with pytest.raises(CompactionPolicyViolation, match="claim failed"):
            policy.observe_context(turns_used=1, context_tokens=120_000, context_window=128_000)

    def test_modes_without_a_token_bound_have_nothing_to_violate(self):
        # Stated as a test because the asymmetry is the argument for turn_cap over
        # disabled: `disabled` bounds turn COUNT and says nothing about turn SIZE.
        CompactionPolicy.disabled(max_turns=3).observe_context(
            turns_used=99, context_tokens=10**9, context_window=128_000
        )
        CompactionPolicy.local_summarizer(model=_model(), api_key="k").observe_context(
            turns_used=99, context_tokens=10**9, context_window=128_000
        )


class TestTurnCapOverARealSession:
    """The proof exercised end to end, through prompt() on a real AgentSession."""

    async def test_a_capped_run_never_reaches_the_threshold(self):
        policy = CompactionPolicy.turn_cap(max_turns=4, max_tokens_per_turn=5_000)
        session = _session(policy)
        summarizer_calls: list = []
        with (
            patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub()),
            patch(
                "tau_agent_core.compaction.complete_simple",
                side_effect=_summarizer_stub(summarizer_calls),
            ),
        ):
            collector = PromptLatencyCollector(session)
            for i in range(4):
                with collector.prompt():
                    await session.prompt(f"turn {i}")

        assert summarizer_calls == [], "no compaction model call may happen under a proven cap"
        assert not any(e.get("type") == "compaction" for e in session.session_log.entries())
        artifact = collector.to_latency_json()
        # The measured zero, not the assumed one.
        assert artifact["counts"]["compaction_bearing"] == 0
        assert artifact["counts"]["excluding_compaction_bearing"] == 4

    async def test_the_turn_after_the_cap_raises_before_the_model_is_called(self):
        policy = CompactionPolicy.turn_cap(max_turns=2, max_tokens_per_turn=5_000)
        session = _session(policy)
        models_seen: list = []
        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub(record=models_seen)
        ):
            await session.prompt("one")
            await session.prompt("two")
            assert len(models_seen) == 2
            with pytest.raises(CompactionPolicyViolation, match="this is user turn 3"):
                await session.prompt("three")
        assert len(models_seen) == 2, "the refusal must cost nothing"

    async def test_the_per_turn_bound_is_in_the_units_should_compact_uses(self):
        # Load-bearing for the proof, and NOT obvious: estimate_context_tokens
        # prefers the provider's reported `total_tokens` on the last assistant
        # message (compaction.py `calculate_context_tokens`) and only falls back to
        # the ~4-chars-per-token heuristic. So a long prompt with a small reported
        # usage estimates SMALL. The policy measures the same quantity the threshold
        # does — if it ever stopped, the cap would be proving something else.
        session = _session()
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub()):
            await session.prompt("a prompt comfortably longer than two tokens")
        from tau_agent_core.compaction import estimate_context_tokens

        assert estimate_context_tokens(session.messages).tokens == 2

    async def test_a_run_that_breaks_its_per_turn_bound_dies_loudly(self):
        # The premise is enforced, so the "proof" is a proof. A per-turn bound of 1
        # against a turn the estimator scores at 2 breaks P1 at the compaction site.
        policy = CompactionPolicy.turn_cap(max_turns=4, max_tokens_per_turn=1)
        session = _session(policy)
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub()):
            with pytest.raises(CompactionPolicyViolation, match="premise failed"):
                await session.prompt("a prompt comfortably longer than two tokens")

    async def test_the_violation_is_not_a_compaction_error(self):
        # A policy violation says the run's premise failed; a CompactionError says a
        # summarisation failed. Conflating them would make a void run look like a
        # scenario result, which is the whole failure §16.8 is about.
        policy = CompactionPolicy.turn_cap(max_turns=1, max_tokens_per_turn=1)
        session = _session(policy)
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub()):
            with pytest.raises(CompactionPolicyViolation) as excinfo:
                await session.prompt("a prompt comfortably longer than two tokens")
        assert not isinstance(excinfo.value, CompactionError)


# ══════════════════════════════════════════════════════════════════════════
# Deliverable 1d — the local-summariser option is genuinely implementable
# ══════════════════════════════════════════════════════════════════════════


class TestLocalSummarizerPolicy:
    async def test_compaction_summarises_through_the_declared_model(self):
        board = _model("board-summariser", context_window=128_000)
        policy = CompactionPolicy.local_summarizer(model=board, api_key="board-key")
        session = _session(policy)
        summarizer_calls: list = []
        with (
            patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub()),
            patch(
                "tau_agent_core.compaction.complete_simple",
                side_effect=_summarizer_stub(summarizer_calls),
            ),
        ):
            await session.prompt("one")
            with patch("tau_agent_core.agent_session.should_compact", return_value=True):
                await session.prompt(_COMPACTING_PROMPT)

        assert len(summarizer_calls) == 1
        args, kwargs = summarizer_calls[0]
        used = [a for a in args if isinstance(a, Model)] + [
            v for v in kwargs.values() if isinstance(v, Model)
        ]
        assert used and used[0].id == "board-summariser", (
            "a local_summarizer policy that still summarises through the session model "
            "has declared a capability it does not have"
        )

    def test_a_summariser_that_cannot_hold_the_window_is_refused(self):
        # It would be asked to summarise a full session window and 400 at the
        # provider — mid-partition, as a CompactionError that reads as a result.
        policy = CompactionPolicy.local_summarizer(
            model=_model("tiny", context_window=32_000), api_key="k"
        )
        with pytest.raises(CompactionPolicyError, match="cannot ingest"):
            policy.bind_to(_model(context_window=128_000))

    def test_without_a_policy_the_summariser_is_the_session_model(self):
        session = _session()
        model, key = session._summarizer()
        assert model is session._model
        assert key == "session-key"

    def test_the_summariser_follows_set_model_when_no_policy_declares_one(self):
        # Shipped behaviour read `self._model` live; caching it at construction
        # would have silently pinned compaction to a stale model after set_model.
        other = _model("other")
        session = _session()
        session.set_model_resolver(lambda name: other)
        session.set_model("other")
        assert session._summarizer()[0] is other


# ══════════════════════════════════════════════════════════════════════════
# The guard rail: nothing about τ's compaction gets quieter or more forgiving
# ══════════════════════════════════════════════════════════════════════════


class TestShippedCompactionBehaviourIsUnchanged:
    def test_auto_compaction_is_still_enabled_by_default(self):
        assert DEFAULT_COMPACTION_SETTINGS.enabled is True
        assert _session()._compaction_settings.enabled is True

    def test_a_session_with_no_policy_carries_none(self):
        assert _session()._compaction_policy is None

    async def test_a_failed_compaction_still_raises_and_still_propagates(self):
        # §16.8: the Fail-Early raise is explicitly endorsed. If this ever starts
        # passing silently, the degradation trap §6.3 exists to detect has been
        # walked into by this very task.
        session = _session()

        async def boom(*args, **kwargs):
            raise CompactionError("summarization_failed", "no")

        with (
            patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub()),
            patch("tau_agent_core.compaction.complete_simple", side_effect=boom),
        ):
            await session.prompt("one")
            with patch("tau_agent_core.agent_session.should_compact", return_value=True):
                with pytest.raises(CompactionError):
                    await session.prompt(_COMPACTING_PROMPT)

    async def test_a_failed_compaction_still_raises_under_a_declared_policy(self):
        # A policy must not become a place where a compaction failure gets absorbed.
        session = _session(CompactionPolicy.local_summarizer(model=_model(), api_key="k"))

        async def boom(*args, **kwargs):
            raise CompactionError("summarization_failed", "no")

        with (
            patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub()),
            patch("tau_agent_core.compaction.complete_simple", side_effect=boom),
        ):
            await session.prompt("one")
            with patch("tau_agent_core.agent_session.should_compact", return_value=True):
                with pytest.raises(CompactionError):
                    await session.prompt(_COMPACTING_PROMPT)

    async def test_auto_compaction_still_fires_for_an_undeclared_session(self):
        session = _session()
        summarizer_calls: list = []
        with (
            patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub()),
            patch(
                "tau_agent_core.compaction.complete_simple",
                side_effect=_summarizer_stub(summarizer_calls),
            ),
        ):
            await session.prompt("one")
            with patch("tau_agent_core.agent_session.should_compact", return_value=True):
                await session.prompt(_COMPACTING_PROMPT)
        assert len(summarizer_calls) == 1
        assert any(e.get("type") == "compaction" for e in session.session_log.entries())

    def test_policy_and_settings_together_are_refused(self):
        with pytest.raises(ValueError, match="not both"):
            AgentSession(
                session_log=InMemorySessionLog(),
                model=_model(),
                compaction_settings=CompactionSettings(reserve_tokens=100),
                compaction_policy=CompactionPolicy.disabled(max_turns=3),
            )

    def test_a_policy_supplies_the_settings_it_declares(self):
        session = _session(CompactionPolicy.disabled(max_turns=3))
        assert session._compaction_settings.enabled is False
        assert session._compaction_settings.reserve_tokens == (
            DEFAULT_COMPACTION_SETTINGS.reserve_tokens
        )

    async def test_a_disabled_policy_makes_no_summariser_call(self):
        session = _session(CompactionPolicy.disabled(max_turns=3))
        summarizer_calls: list = []
        with (
            patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub()),
            patch(
                "tau_agent_core.compaction.complete_simple",
                side_effect=_summarizer_stub(summarizer_calls),
            ),
        ):
            await session.prompt("one")
            with patch("tau_agent_core.agent_session.should_compact", return_value=True):
                await session.prompt(_COMPACTING_PROMPT)
        assert summarizer_calls == []

    async def test_an_undeclared_session_does_not_pay_for_the_policy_check(self):
        # The estimate is only computed when a policy is declared, so the default
        # path is byte-for-byte the shipped one.
        session = _session()
        with (
            patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub()),
            patch("tau_agent_core.compaction_policy.CompactionPolicy.observe_context") as observe,
        ):
            await session.prompt("one")
        observe.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════
# Deliverable 2 — the choice is recorded in manifest.json, BESIDE `harness`
# ══════════════════════════════════════════════════════════════════════════


class TestManifestRecordsThePolicy:
    def test_harness_and_compaction_are_siblings(self):
        manifest = build_run_manifest(
            compaction_policy=CompactionPolicy.turn_cap(max_turns=4, max_tokens_per_turn=1_000)
        )
        assert manifest["harness"] == HARNESS
        assert manifest["compaction"]["mode"] == "turn_cap"

    def test_a_manifest_cannot_be_built_without_the_decision(self):
        with pytest.raises(TypeError):
            build_run_manifest()  # type: ignore[call-arg]

    def test_the_record_carries_the_declared_numbers(self):
        record = CompactionPolicy.turn_cap(max_turns=7, max_tokens_per_turn=1_234).to_manifest()
        assert record["declared"] == {"max_turns": 7, "max_tokens_per_turn": 1_234}
        assert record["compaction_enabled"] is True
        assert record["reserve_tokens"] == DEFAULT_COMPACTION_SETTINGS.reserve_tokens

    def test_the_record_identifies_a_local_summariser_without_its_key(self):
        record = CompactionPolicy.local_summarizer(
            model=_model("board", context_window=128_000), api_key="sk-do-not-leak"
        ).to_manifest()
        assert record["declared"]["summarizer_model"]["id"] == "board"
        assert record["declared"]["summarizer_model"]["base_url"] == "https://example.invalid/v1"
        assert "sk-do-not-leak" not in json.dumps(record)

    def test_the_record_is_json_serialisable(self):
        for policy in (
            CompactionPolicy.disabled(max_turns=3),
            CompactionPolicy.turn_cap(max_turns=3, max_tokens_per_turn=100),
            CompactionPolicy.local_summarizer(model=_model(), api_key="k"),
        ):
            assert json.loads(json.dumps(policy.to_manifest()))["mode"] == policy.mode

    def test_an_unlabelled_manifest_is_refused_by_the_reader(self):
        with pytest.raises(KeyError, match="compaction"):
            require_compaction_policy({"harness": "tau", "seed": 0})
        with pytest.raises(KeyError, match="harness"):
            require_compaction_policy({"compaction": {"mode": "disabled"}})

    def test_a_malformed_compaction_block_is_refused(self):
        with pytest.raises(ValueError, match="not a policy record"):
            require_compaction_policy({"harness": "tau", "compaction": "disabled"})

    def test_extra_keys_pass_through_but_cannot_shadow_the_partition_keys(self):
        manifest = build_run_manifest(
            compaction_policy=CompactionPolicy.disabled(max_turns=3), seed=7, run_id="r1"
        )
        assert manifest["seed"] == 7 and manifest["run_id"] == "r1"
        with pytest.raises(ValueError, match="owned by build_run_manifest"):
            build_run_manifest(
                compaction_policy=CompactionPolicy.disabled(max_turns=3),
                **{"compaction": {"mode": "disabled"}},
            )

    def test_a_pi_era_run_records_a_different_harness(self):
        # §5.2: "no pi-era latency is a baseline for a τ-era number". The key exists
        # so the two populations can be told apart, so it must be settable.
        manifest = build_run_manifest(
            harness="pi", compaction_policy=CompactionPolicy.disabled(max_turns=3)
        )
        assert manifest["harness"] == "pi" != HARNESS

    def test_an_unlabelled_manifest_never_reaches_disk(self, tmp_path):
        target = tmp_path / "manifest.json"
        with pytest.raises(KeyError):
            write_run_manifest(target, {"seed": 0})
        assert not target.exists()

    def test_a_labelled_manifest_round_trips(self, tmp_path):
        manifest = build_run_manifest(
            compaction_policy=policy_for_scenario("D", max_turns=6, max_tokens_per_turn=1_000),
            seed=3,
        )
        path = write_run_manifest(tmp_path / "manifest.json", manifest)
        loaded = json.loads(path.read_text())
        assert require_compaction_policy(loaded)["declared"]["max_turns"] == 6
        assert loaded["harness"] == HARNESS


# ══════════════════════════════════════════════════════════════════════════
# Deliverable 3 — compaction-bearing prompts are tagged OUT of latency.json
# ══════════════════════════════════════════════════════════════════════════


def _event(event_type: str, ts: int) -> AgentEvent:
    return AgentEvent(type=event_type, timestamp=ts)  # type: ignore[arg-type]


class TestSummarizeShape:
    def test_shape_is_four_figures_never_one(self):
        stats = summarize([10, 20, 30, 40])
        assert set(stats) == {"n", "median", "p90", "p99", "max"}

    def test_every_reported_figure_was_actually_observed(self):
        values = [5, 9, 100]
        stats = summarize(values)
        for key in ("median", "p90", "p99", "max"):
            assert stats[key] in values, f"{key} was interpolated into a value nobody measured"

    def test_an_empty_population_reports_no_median(self):
        # A fabricated 0.0 median for a population with no members reads as a
        # measurement. Fail-Early: say n=0 and nothing else.
        assert summarize([]) == {"n": 0}


class TestTheBracketMarker:
    def test_a_bare_pair_is_tagged(self):
        session = _session()
        collector = PromptLatencyCollector(session)
        with collector.prompt():
            for event in (_event("agent_start", 100), _event("agent_end", 160)):
                collector._on_event(event)
        sample = collector.samples[0]
        assert sample.bare_brackets == 1
        assert sample.compaction_bearing is True
        assert sample.bare_bracket_ms == 60

    def test_a_bracket_with_anything_inside_it_is_not_tagged(self):
        session = _session()
        collector = PromptLatencyCollector(session)
        with collector.prompt():
            for event in (
                _event("agent_start", 100),
                _event("turn_start", 101),
                _event("turn_end", 150),
                _event("agent_end", 160),
            ):
                collector._on_event(event)
        assert collector.samples[0].compaction_bearing is False

    def test_more_than_one_bare_pair_in_one_prompt_is_counted(self):
        # Measured fact: _end_of_prompt_drain runs _maybe_auto_compact once at the
        # tail and again after every followUp re-entry, so "one per prompt" is false.
        session = _session()
        collector = PromptLatencyCollector(session)
        with collector.prompt():
            for event in (
                _event("agent_start", 0),
                _event("turn_start", 1),
                _event("agent_end", 2),
                _event("agent_start", 3),
                _event("agent_end", 5),
                _event("agent_start", 6),
                _event("turn_start", 7),
                _event("agent_end", 8),
                _event("agent_start", 9),
                _event("agent_end", 13),
            ):
                collector._on_event(event)
        assert collector.samples[0].bare_brackets == 2
        assert collector.samples[0].bare_bracket_ms == 6

    def test_a_bare_pair_outside_a_prompt_window_is_counted_not_dropped(self):
        session = _session()
        collector = PromptLatencyCollector(session)
        collector._on_event(_event("agent_start", 0))
        collector._on_event(_event("agent_end", 9))
        assert collector.bare_brackets_outside_prompt == 1
        assert collector.samples == []

    def test_a_stream_that_breaks_the_marker_refuses_to_produce_an_artifact(self):
        # Never a partition computed from a marker known not to hold.
        session = _session()
        collector = PromptLatencyCollector(session)
        collector._on_event(_event("agent_end", 5))
        assert collector.anomalies
        with pytest.raises(RuntimeError, match="marker did not hold"):
            collector.to_latency_json()

    def test_nested_brackets_are_recorded_as_an_anomaly(self):
        session = _session()
        collector = PromptLatencyCollector(session)
        collector._on_event(_event("agent_start", 0))
        collector._on_event(_event("agent_start", 1))
        assert any("nested agent_start" in a for a in collector.anomalies)

    def test_overlapping_prompt_windows_are_refused(self):
        session = _session()
        collector = PromptLatencyCollector(session)
        with pytest.raises(RuntimeError, match="already open"):
            with collector.prompt():
                with collector.prompt():
                    pass


class TestTheCompactionBearingPromptsAreNotPooled:
    """The Definition-of-Done test: a compacting run and a non-compacting run do
    **not** land in one bucket."""

    async def test_a_compacting_prompt_and_a_clean_prompt_land_in_different_buckets(self):
        session = _session()
        # The compaction summariser is made measurably slow, so the compacting
        # prompt is exactly the outlier §5.2's p99 would otherwise absorb.
        with (
            patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub()),
            patch(
                "tau_agent_core.compaction.complete_simple",
                side_effect=_summarizer_stub(delay_s=0.05),
            ),
        ):
            collector = PromptLatencyCollector(session)
            for i in range(4):
                with collector.prompt():
                    await session.prompt(f"clean {i}")
            with patch("tau_agent_core.agent_session.should_compact", return_value=True):
                with collector.prompt():
                    await session.prompt(_COMPACTING_PROMPT)

        artifact = collector.to_latency_json()
        turns = artifact["assembled_turn_latency"]

        assert turns["excluding_compaction_bearing"]["n"] == 4
        assert turns["compaction_bearing"]["n"] == 1
        assert artifact["compaction_bearing_prompt_indexes"] == [4]

        # The real compaction ran: one summariser call, one durable entry.
        assert sum(1 for e in session.session_log.entries() if e.get("type") == "compaction") == 1
        assert turns["compaction_bearing_committed"]["n"] == 1

        # And the number the sim reports as its headline does not contain it.
        compacting_ms = collector.samples[4].total_ms
        assert compacting_ms >= 50, "the compaction should dominate that prompt"
        assert turns["excluding_compaction_bearing"]["max"] < compacting_ms
        assert turns["compaction_bearing"]["max"] == compacting_ms

    def test_there_is_no_api_that_returns_the_pooled_population(self):
        # Structural, like Trace.arm: a consumer must not be able to have lost the
        # partition key. `to_latency_json` emits named populations and no union.
        session = _session()
        collector = PromptLatencyCollector(session)
        keys = set(collector.to_latency_json()["assembled_turn_latency"])
        assert keys == {
            "excluding_compaction_bearing",
            "compaction_bearing",
            "compaction_bearing_committed",
            "compaction_bearing_uncommitted",
        }
        for banned in ("all", "pooled", "total", "combined", "overall"):
            assert banned not in keys

    async def test_a_bare_bracket_with_no_committed_compaction_is_still_excluded(self):
        # The marker over-tags: prepare_compaction()->None emits the bare pair with
        # no model call at all. Over-tagging is the safe direction for an exclusion,
        # and the corroborator keeps the excluded population decomposable.
        session = _session()
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub()):
            collector = PromptLatencyCollector(session)
            with collector.prompt():
                await session.prompt("clean")
            with (
                patch("tau_agent_core.agent_session.should_compact", return_value=True),
                patch("tau_agent_core.agent_session.prepare_compaction", return_value=None),
            ):
                with collector.prompt():
                    await session.prompt("bare bracket, no model call")

        artifact = collector.to_latency_json()
        assert artifact["counts"]["compaction_bearing"] == 1
        assert artifact["counts"]["compaction_bearing_committed"] == 0
        assert artifact["counts"]["compaction_bearing_uncommitted"] == 1
        assert artifact["counts"]["excluding_compaction_bearing"] == 1

    async def test_a_prompt_that_raises_still_yields_a_sample(self):
        # A raising prompt may still have carried a compaction — _maybe_auto_compact
        # emits agent_end from a finally and then propagates. Dropping the sample
        # would delete exactly the observation the exclusion exists to catch.
        session = _session()

        async def boom(*args, **kwargs):
            raise CompactionError("summarization_failed", "no")

        with (
            patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub()),
            patch("tau_agent_core.compaction.complete_simple", side_effect=boom),
        ):
            collector = PromptLatencyCollector(session)
            with collector.prompt():
                await session.prompt("one")
            with patch("tau_agent_core.agent_session.should_compact", return_value=True):
                with pytest.raises(CompactionError):
                    with collector.prompt():
                        await session.prompt(_COMPACTING_PROMPT)

        assert len(collector.samples) == 2
        assert collector.samples[1].compaction_bearing is True
        assert collector.samples[1].compactions_committed == 0

    async def test_the_marker_has_no_false_negative_on_the_real_path(self):
        # Every compaction path brackets unconditionally, the closing agent_end is in
        # a finally, so a compaction cannot happen unobserved. Proved for the
        # committed, the no-op and the raising case together.
        session = _session()
        seen: list[int] = []
        with (
            patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_stub()),
            patch("tau_agent_core.compaction.complete_simple", side_effect=_summarizer_stub()),
        ):
            collector = PromptLatencyCollector(session)
            with collector.prompt():
                await session.prompt("one")
            with patch("tau_agent_core.agent_session.should_compact", return_value=True):
                with collector.prompt():
                    await session.prompt(_COMPACTING_PROMPT)
            seen = [s.bare_brackets for s in collector.samples]
        assert seen == [0, 1]

    def test_close_unsubscribes(self):
        session = _session()
        collector = PromptLatencyCollector(session)
        collector.close()
        collector.close()  # idempotent
        with collector.prompt():
            pass
        assert collector.samples == []
