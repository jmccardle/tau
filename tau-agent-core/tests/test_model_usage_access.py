"""E6 §2 / S45 — model + usage access from extensions (anchor G14).

Verifies the public accessors that replace private ``ctx._session._model`` reaching
and ``event.message`` digging:

- ``ctx.get_model()`` / ``AgentSession.get_model()`` → ``{id, provider, context_window}``
- ``ctx.set_model(name)`` → resolves NAME through the bound resolver, effective on
  the NEXT turn (mirrors pi ``setModel``); Fail-Early raises when no resolver.
- ``ctx.get_usage()`` → the last completion's usage dict (or ``None``), recorded off
  ``message_end`` BEFORE extension handlers so a budget/ledger handler reads it live.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.extension_types import ExtensionAPI, ExtensionContext
from tau_agent_core.session_log import InMemorySessionLog


def _model(model_id: str = "model-a", context_window: int = 128000) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        api="openai-completions",
        provider="openai",
        base_url="http://localhost",
        context_window=context_window,
        max_tokens=4096,
    )


def _session(model: Model | None = None, resolver=None) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=model or _model(),
        compaction_settings=CompactionSettings(enabled=False),
        model_resolver=resolver,
    )


def _ctx(session: AgentSession) -> ExtensionContext:
    return session._extension_api.context


class _RecordingStream:
    """Canned single-completion stream that records the model id it was built for."""

    def __init__(self, model_id: str):
        text = "ok"
        assistant = AssistantMessage(
            content=[TextContent(text=text)],
            api="openai-completions",
            provider="openai",
            model=model_id,
            stop_reason="stop",
            timestamp=0,
            usage=Usage(input_tokens=3, output_tokens=5, total_tokens=8),
        )
        self._events = [
            TextDeltaEvent(delta=text, partial=assistant),
            DoneEvent(final=assistant, usage=assistant.usage),
        ]

    def __aiter__(self):
        async def _gen():
            for event in self._events:
                yield event

        return _gen()

    async def result(self):
        for event in self._events:
            if isinstance(event, DoneEvent):
                return event.final
        return None

    def abort(self) -> None:
        pass


def _recording_stream_simple(seen: list[str]):
    """A ``stream_simple`` patch echoing each call's ``model.id`` into ``seen``."""

    async def fake(model, context, options=None):
        seen.append(model.id)
        return _RecordingStream(model.id)

    return fake


# ── get_model ────────────────────────────────────────────────────────────────


class TestGetModel:
    def test_projection_shape(self):
        session = _session(_model("gpt-4o", context_window=64000))
        expected = {"id": "gpt-4o", "provider": "openai", "context_window": 64000}
        assert session.get_model() == expected
        assert _ctx(session).get_model() == expected

    def test_reflects_a_prior_set_model(self):
        session = _session(_model("model-a"), resolver=lambda name: _model(name))
        _ctx(session).set_model("model-b")
        assert session.get_model()["id"] == "model-b"


# ── set_model ────────────────────────────────────────────────────────────────


class TestSetModel:
    def test_requires_a_resolver(self):
        session = _session(resolver=None)
        with pytest.raises(RuntimeError, match="no model resolver is bound"):
            _ctx(session).set_model("model-b")

    def test_returns_the_new_projection(self):
        session = _session(_model("model-a"), resolver=lambda name: _model(name, 200000))
        result = _ctx(session).set_model("model-b")
        assert result == {"id": "model-b", "provider": "openai", "context_window": 200000}

    def test_unknown_name_error_propagates(self):
        def resolver(name: str) -> Model:
            raise KeyError(f"unknown model {name!r}")

        session = _session(resolver=resolver)
        with pytest.raises(KeyError, match="unknown model"):
            _ctx(session).set_model("nope")

    def test_non_model_return_is_type_error(self):
        session = _session(resolver=lambda name: {"id": name})  # type: ignore[arg-type,return-value]
        with pytest.raises(TypeError, match="expected a tau_ai.types.Model"):
            _ctx(session).set_model("model-b")

    async def test_next_turn_effect_through_the_loop(self):
        """set_model takes effect on the NEXT completion, never mid-stream."""
        seen: list[str] = []
        session = _session(_model("model-a"), resolver=lambda name: _model(name))
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_recording_stream_simple(seen),
        ):
            await session.prompt("one")
            assert seen == ["model-a"]  # first turn used the original model
            _ctx(session).set_model("model-b")
            await session.prompt("two")
        assert seen == ["model-a", "model-b"]  # the switch reached the next turn


# ── get_usage ────────────────────────────────────────────────────────────────


class TestGetUsage:
    def test_none_before_any_completion(self):
        session = _session()
        assert session.get_usage() is None
        assert _ctx(session).get_usage() is None

    async def test_populated_after_a_completion(self):
        seen: list[str] = []
        session = _session()
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_recording_stream_simple(seen),
        ):
            await session.prompt("hello")
        usage = _ctx(session).get_usage()
        assert usage is not None
        assert usage["input_tokens"] == 3
        assert usage["output_tokens"] == 5
        assert usage["total_tokens"] == 8

    async def test_returns_a_copy(self):
        seen: list[str] = []
        session = _session()
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_recording_stream_simple(seen),
        ):
            await session.prompt("hello")
        usage = session.get_usage()
        assert usage is not None
        usage["total_tokens"] = 9999  # mutate the returned copy …
        assert session.get_usage()["total_tokens"] == 8  # … source is untouched

    async def test_recorded_before_extension_message_end_handler(self):
        """A ``message_end`` handler reads ctx.get_usage() already updated (ordering)."""
        seen: list[str] = []
        session = _session()
        observed: list[dict | None] = []

        # api.on("message_end", …) is a NOTIFY subscription (goes to the event bus);
        # the session's usage recorder was subscribed at construction, BEFORE this,
        # so it runs first for each message_end.
        session._extension_api.on(
            "message_end",
            lambda event: observed.append(session._extension_api.context.get_usage()),
        )
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_recording_stream_simple(seen),
        ):
            await session.prompt("hello")

        # The handler saw a populated, correct usage at the moment message_end fired
        # (not None, and this completion's numbers).
        assert any(u is not None and u["total_tokens"] == 8 for u in observed)


# ── unbound context (Fail-Early) ─────────────────────────────────────────────


class TestUnboundContext:
    def test_get_model_requires_session(self):
        ctx = ExtensionContext()
        with pytest.raises(RuntimeError, match="no session bound"):
            ctx.get_model()

    def test_set_model_requires_session(self):
        ctx = ExtensionContext()
        with pytest.raises(RuntimeError, match="no session bound"):
            ctx.set_model("model-b")

    def test_get_usage_requires_session(self):
        ctx = ExtensionContext()
        with pytest.raises(RuntimeError, match="no session bound"):
            ctx.get_usage()

    def test_bare_api_context_is_unbound(self):
        # A bare ExtensionAPI() makes its own context with no session (Fail-Early).
        api = ExtensionAPI()
        with pytest.raises(RuntimeError, match="no session bound"):
            api.context.get_model()
