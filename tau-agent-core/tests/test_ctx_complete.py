"""W6/C1 — ``ctx.complete()``: direct completions through the model registry.

The primitive behind every "classify / extract / draft" extension story, and the
building block of the retrieval-review fan-out: N concurrent constraint-verified
verdicts. Deliberately stateless — it must touch neither the entry log nor the cursor,
so it is safe under ``asyncio.gather`` at any fan-out.

Reference: docs/JMFTS-INTEGRATION-PLAN.md §9.1.
"""

from __future__ import annotations

import asyncio

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_types import ExtensionContext
from tau_agent_core.session_log import InMemorySessionLog
from tau_ai.types import AssistantMessage, Model, TextContent


def _model(name: str = "primary") -> Model:
    return Model(
        id=name,
        name=name,
        api="openai-completions",
        provider="openai",
        base_url="http://x/v1",
        context_window=1000,
        max_tokens=100,
    )


def _reply(text: str, model: str = "primary") -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model=model,
        stop_reason="stop",
        timestamp=0,
    )


@pytest.fixture
def ctx_and_log(monkeypatch):
    """An ExtensionContext bound to a session, with complete_simple stubbed."""
    log = InMemorySessionLog()
    session = AgentSession(session_log=log, model=_model(), api_key="k")
    session.set_model_resolver(lambda name: _model(name))

    calls: list[dict] = []

    async def fake_complete_simple(model, context, options=None):
        calls.append({"model": model, "messages": context["messages"], "options": options or {}})
        return _reply("ANSWER", model=model.id)

    monkeypatch.setattr("tau_ai.client.complete_simple", fake_complete_simple)

    ctx = ExtensionContext()
    ctx._session = session
    return ctx, log, calls


class TestModelRouting:
    async def test_defaults_to_the_sessions_model(self, ctx_and_log):
        ctx, _, calls = ctx_and_log

        await ctx.complete([{"role": "user", "content": "hi"}])

        assert calls[0]["model"].id == "primary"

    async def test_a_name_resolves_through_the_config_registry(self, ctx_and_log):
        """The point of C1: an extension names a model from the same config the TUI uses."""
        ctx, _, calls = ctx_and_log

        await ctx.complete([{"role": "user", "content": "hi"}], model="local-llm-small")

        assert calls[0]["model"].id == "local-llm-small"

    async def test_a_model_object_passes_through(self, ctx_and_log):
        ctx, _, calls = ctx_and_log

        await ctx.complete([{"role": "user", "content": "hi"}], model=_model("explicit"))

        assert calls[0]["model"].id == "explicit"

    async def test_unresolvable_name_raises_when_no_resolver_is_bound(self, monkeypatch):
        session = AgentSession(session_log=InMemorySessionLog(), model=_model())
        ctx = ExtensionContext()
        ctx._session = session

        with pytest.raises(RuntimeError, match="no model resolver is bound"):
            await ctx.complete([{"role": "user", "content": "hi"}], model="nope")


class TestStatelessness:
    async def test_writes_nothing_to_the_tree(self, ctx_and_log):
        """C1 is session-free: no entries, no cursor move. This is what makes the
        fan-out safe under asyncio.gather."""
        ctx, log, _ = ctx_and_log
        before_entries, before_cursor = log.entries(), log.cursor

        await ctx.complete([{"role": "user", "content": "hi"}])

        assert log.entries() == before_entries
        assert log.cursor == before_cursor

    async def test_concurrent_fan_out(self, ctx_and_log):
        """The retrieval-review shape: N concurrent completions over one session."""
        ctx, log, calls = ctx_and_log
        # Baseline, not `== []`: construction already wrote its own non-authoritative
        # `agent_spec` provenance record (W2, NODE-ADDRESSABLE-AGENTS.md) — the
        # property under test is that ctx.complete() writes nothing FURTHER, not
        # that the log is pristine.
        before = log.entries()

        results = await asyncio.gather(
            *[ctx.complete([{"role": "user", "content": f"doc {i}"}]) for i in range(10)]
        )

        assert len(results) == 10
        assert len(calls) == 10
        assert log.entries() == before  # still no tree writes


class TestFailEarly:
    async def test_an_errored_completion_raises(self, ctx_and_log, monkeypatch):
        """An AssistantMessage with stop_reason="error" handed back would read as a
        successful (empty) answer."""
        ctx, _, _ = ctx_and_log

        async def erroring(model, context, options=None):
            return AssistantMessage(
                content=[],
                api="openai-completions",
                provider="openai",
                model="primary",
                stop_reason="error",
                error_message="upstream exploded",
                timestamp=0,
            )

        monkeypatch.setattr("tau_ai.client.complete_simple", erroring)

        with pytest.raises(RuntimeError, match="upstream exploded"):
            await ctx.complete([{"role": "user", "content": "hi"}])

    async def test_complete_text_raises_on_empty(self, ctx_and_log, monkeypatch):
        ctx, _, _ = ctx_and_log

        async def empty(model, context, options=None):
            return _reply("   ")

        monkeypatch.setattr("tau_ai.client.complete_simple", empty)

        with pytest.raises(RuntimeError, match="empty response"):
            await ctx.complete_text([{"role": "user", "content": "hi"}])

    async def test_no_session_bound_raises(self):
        ctx = ExtensionContext()
        with pytest.raises(RuntimeError, match="no session bound"):
            await ctx.complete([{"role": "user", "content": "hi"}])


class TestConstraintsThread:
    async def test_constraints_reach_the_provider_options(self, ctx_and_log):
        """G3: the constraint parameter is in C1's signature from day one."""
        from tau_ai.constraints import DecodeConstraints

        ctx, _, calls = ctx_and_log
        constraints = DecodeConstraints(choices=["include", "exclude"])

        await ctx.complete([{"role": "user", "content": "hi"}], constraints=constraints)

        assert calls[0]["options"]["constraints"] is constraints

    async def test_complete_text_returns_the_text(self, ctx_and_log):
        ctx, _, _ = ctx_and_log

        assert await ctx.complete_text([{"role": "user", "content": "hi"}]) == "ANSWER"


class TestConstraintsEcho:
    """G4/C: ``ctx.complete()`` echoes the constraint that shaped it as a record.

    Retires the "``describe()`` has zero non-test callers" debt — the one place a
    real DecodeConstraints exists at completion time is ``ctx.complete()``, so it is
    the honest producer of the ``{"kind": "constraints", ...}`` extension record.
    """

    async def test_a_constrained_completion_emits_one_describe_record(self, ctx_and_log):
        from tau_ai.constraints import DecodeConstraints

        ctx, _, _ = ctx_and_log
        records: list[dict] = []
        ctx._ui.set_record_sink(records.append)

        constraints = DecodeConstraints(choices=["include", "exclude"])
        await ctx.complete([{"role": "user", "content": "hi"}], constraints=constraints)

        assert len(records) == 1
        assert records[0] == {
            "type": "extension",
            "kind": "constraints",
            "extension": None,
            "constraints": constraints.describe(),
        }
        # The echo IS describe() verbatim — the display-only summary, never replayed.
        assert records[0]["constraints"] == {"kind": "choices", "choices": ["include", "exclude"]}

    async def test_no_constraints_emits_nothing(self, ctx_and_log):
        """An unconstrained completion has nothing to echo — no record, never a
        fabricated ``{"kind":"none"}`` placeholder (Fail-Early)."""
        ctx, _, _ = ctx_and_log
        records: list[dict] = []
        ctx._ui.set_record_sink(records.append)

        await ctx.complete([{"role": "user", "content": "hi"}])

        assert records == []

    async def test_emit_constraints_drops_a_none_summary(self):
        """The C2 emitter guard: even handed a ``{"kind":"none"}`` summary directly,
        it emits nothing — the "no placeholder" invariant is local to the emitter."""
        from tau_agent_core.extension_types import ExtensionUI

        ui = ExtensionUI(mode="headless")
        records: list[dict] = []
        ui.set_record_sink(records.append)

        ui.emit_constraints({"kind": "none"})

        assert records == []
