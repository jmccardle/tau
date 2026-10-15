"""What happens to queued followUps after an abort (docs/TURN-SETTINGS.md §4-5).

An extension abort (``ctx.abort()``) stops the loop and the drain then runs each
queued followUp on a fresh signal. A user abort (``AgentSession.abort()``) drops
them, writes nothing, and emits ``follow_ups_dropped`` naming them.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent
from tau_agent_core.follow_ups import follow_ups_dropped_notice
from tau_agent_core.rpc.wire_events import WireEventProjector
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission

_TS = 1_700_000_000_000


def _model(mid: str = "m") -> Model:
    return Model(
        id=mid,
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name=mid,
        context_window=8192,
        max_tokens=256,
    )


class _Stream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for event in self._events:
                yield event

        return gen()

    def abort(self) -> None:
        pass


class _Provider:
    """Answers every request with text; records each request's model id."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, model: Any, context: Any, options: Any = None, **_: Any) -> Any:
        self.calls.append(model.id)
        final = AssistantMessage(
            content=[TextContent(text="ok")],
            api="openai-completions",
            provider="openai",
            model=model.id,
            stop_reason="stop",
            timestamp=_TS,
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        )
        return _Stream(
            [TextDeltaEvent(delta="ok", partial=final), DoneEvent(final=final, usage=final.usage)]
        )


def _sub(text: str) -> Submission:
    return Submission(text=text, source="interactive", submitter="human", submission_id=text)


def _first_turn_end(action: Any) -> Any:
    """An extension factory running ``action(api, ctx)`` in the first ``turn_end`` only."""

    def factory(api: Any) -> None:
        fired = {"n": 0}

        def on_turn_end(event: Any, ctx: Any) -> None:
            fired["n"] += 1
            if fired["n"] == 1:
                action(api, ctx)

        api.on("turn_end", on_turn_end)

    return factory


def _session(action: Any) -> AgentSession:
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        tools=[],
        extensions=[_first_turn_end(action)],
    )
    session.set_model_resolver(lambda name: _model(name))
    return session


def _user_texts(session: AgentSession) -> list[str]:
    texts = []
    for entry in session.session_log.entries():
        message = entry.get("message") or {}
        if message.get("role") == "user":
            texts.append("".join(b.get("text", "") for b in message.get("content", [])))
    return texts


class TestAfterAnExtensionAbort:
    async def test_a_follow_up_queued_before_ctx_abort_runs(self):
        def governor(api: Any, ctx: Any) -> None:
            ctx.set_model("cheap")
            api.send_user_message("continue on the cheap model", "followUp")
            ctx.abort()

        session = _session(governor)
        provider = _Provider()
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            await session.submit(_sub("go"))
        assert provider.calls == ["m", "cheap"]
        assert _user_texts(session) == ["go", "continue on the cheap model"]
        assert session.cursor.follow_up_queue == []

    async def test_each_follow_up_gets_a_fresh_signal(self):
        def governor(api: Any, ctx: Any) -> None:
            api.send_user_message("one", "followUp")
            api.send_user_message("two", "followUp")
            ctx.abort()

        session = _session(governor)
        provider = _Provider()
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            await session.submit(_sub("go"))
        assert len(provider.calls) == 3
        assert _user_texts(session) == ["go", "one", "two"]


class TestAfterAUserAbort:
    async def test_queued_follow_ups_are_dropped_and_announced(self):
        holder: dict[str, AgentSession] = {}

        def user_stops(api: Any, ctx: Any) -> None:
            api.send_user_message("never sent", "followUp")
            holder["session"].abort()

        session = _session(user_stops)
        holder["session"] = session
        events: list[AgentEvent] = []
        session.subscribe(lambda e: events.append(e))
        provider = _Provider()
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            await session.submit(_sub("go"))

        assert provider.calls == ["m"]
        assert _user_texts(session) == ["go"]
        assert session.cursor.follow_up_queue == []
        (dropped,) = [e for e in events if e.type == "follow_ups_dropped"]
        assert dropped.dropped == ["never sent"]
        assert dropped.submission_id == "go" and dropped.cursor_id == session.cursor.id
        assert [e.type for e in events].index("agent_end") < events.index(dropped)

    async def test_a_user_abort_after_ctx_abort_still_drops(self):
        holder: dict[str, AgentSession] = {}

        def both(api: Any, ctx: Any) -> None:
            api.send_user_message("never sent", "followUp")
            ctx.abort()
            holder["session"].abort()

        session = _session(both)
        holder["session"] = session
        provider = _Provider()
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            await session.submit(_sub("go"))
        assert provider.calls == ["m"]
        assert _user_texts(session) == ["go"]

    async def test_nothing_queued_emits_nothing(self):
        holder: dict[str, AgentSession] = {}
        session = _session(lambda api, ctx: holder["session"].abort())
        holder["session"] = session
        events: list[AgentEvent] = []
        session.subscribe(lambda e: events.append(e))
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_Provider()):
            await session.submit(_sub("go"))
        assert not [e for e in events if e.type == "follow_ups_dropped"]


class TestTheEventOnTheWire:
    def test_the_wire_carries_what_was_dropped(self):
        event = AgentEvent(type="follow_ups_dropped", timestamp=_TS, dropped=["a", "b"])
        (wire,) = WireEventProjector().project(event)
        assert wire["type"] == "follow_ups_dropped"
        assert wire["dropped"] == ["a", "b"]
        assert follow_ups_dropped_notice(wire) == follow_ups_dropped_notice(event.model_dump())

    def test_the_notice_names_each_follow_up_and_cuts_a_long_one(self):
        notice = follow_ups_dropped_notice({"dropped": ["short", "x" * 100]})
        assert notice.startswith("Aborted: dropped 2 queued follow-ups: 'short', 'xxx")
        assert notice.endswith("…'")
        assert len(notice) < 140
