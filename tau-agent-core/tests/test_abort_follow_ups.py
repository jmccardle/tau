"""What happens to queued followUps after an abort (docs/TURN-SETTINGS.md §4-5).

An extension abort (``ctx.abort()``) stops the loop and the drain then runs each
queued followUp on a fresh signal. A user abort (``AgentSession.abort()``) drops
them, writes nothing, and emits ``follow_ups_dropped`` naming them. It also
refuses every ``enqueue`` submission waiting for the turn (§5.1).
"""

from __future__ import annotations

import asyncio
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


def _sub(text: str, strategy: str = "reject") -> Submission:
    return Submission(
        text=text,
        source="interactive",
        submitter="human",
        submission_id=text,
        multitask_strategy=strategy,  # type: ignore[arg-type]
    )


class _HeldProvider(_Provider):
    """A provider whose first answer waits for ``release``, so a turn stays in flight."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def __call__(self, model: Any, context: Any, options: Any = None, **_: Any) -> Any:
        stream = super().__call__(model, context, options)
        if len(self.calls) > 1:
            return stream
        held = self

        class _Held(_Stream):
            def __aiter__(self) -> Any:
                async def gen() -> Any:
                    held.started.set()
                    await held.release.wait()
                    async for event in stream:
                        yield event

                return gen()

        return _Held([])


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


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


class TestEnqueuedSubmissionsAfterAUserAbort:
    """§5.1: a user abort refuses every ``enqueue`` waiting for the turn."""

    async def test_waiting_enqueues_are_refused_and_announced_once(self):
        session = _session(lambda api, ctx: None)
        events: list[AgentEvent] = []
        session.subscribe(lambda e: events.append(e))
        provider = _HeldProvider()
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            first = asyncio.ensure_future(session.submit(_sub("go")))
            await provider.started.wait()
            waiting = [
                asyncio.ensure_future(session.submit(_sub(t, "enqueue")))
                for t in ("second thought", "third thought")
            ]
            await _settle()
            session.abort()
            provider.release.set()
            await first
            results = await asyncio.gather(*waiting)

        assert [r.accepted for r in results] == [False, False]
        assert all("user abort" in (r.rejection_reason or "") for r in results)
        assert provider.calls == ["m"]
        assert _user_texts(session) == ["go"]
        (dropped,) = [e for e in events if e.type == "follow_ups_dropped"]
        assert dropped.dropped == ["second thought", "third thought"]
        assert dropped.cursor_id == session.cursor.id
        assert session.cursor.enqueued == [] and session.cursor.dropped_enqueued == []

    async def test_follow_ups_and_enqueues_share_one_event_follow_ups_first(self):
        holder: dict[str, AgentSession] = {}
        session = _session(lambda api, ctx: api.send_user_message("queued", "followUp"))
        holder["session"] = session
        events: list[AgentEvent] = []
        session.subscribe(lambda e: events.append(e))
        provider = _HeldProvider()
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            first = asyncio.ensure_future(session.submit(_sub("go")))
            await provider.started.wait()
            waiting = asyncio.ensure_future(session.submit(_sub("typed", "enqueue")))
            await _settle()
            session.abort()
            provider.release.set()
            await first
            assert not (await waiting).accepted

        (dropped,) = [e for e in events if e.type == "follow_ups_dropped"]
        assert dropped.dropped == ["queued", "typed"]

    async def test_an_enqueue_submitted_after_the_abort_runs(self):
        session = _session(lambda api, ctx: None)
        provider = _HeldProvider()
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            first = asyncio.ensure_future(session.submit(_sub("go")))
            await provider.started.wait()
            session.abort()
            later = asyncio.ensure_future(session.submit(_sub("after", "enqueue")))
            await _settle()
            provider.release.set()
            await first
            result = await later

        assert result.accepted
        assert _user_texts(session) == ["go", "after"]

    async def test_with_no_turn_to_drain_the_first_refused_wait_announces(self):
        session = _session(lambda api, ctx: None)
        events: list[AgentEvent] = []
        session.subscribe(lambda e: events.append(e))
        cursor = session.cursor
        await cursor.turn_lock.acquire()
        waiting = [
            asyncio.ensure_future(session.submit(_sub(t, "enqueue"))) for t in ("a", "b")
        ]
        await _settle()
        session.abort()
        cursor.turn_lock.release()
        results = await asyncio.gather(*waiting)

        assert [r.accepted for r in results] == [False, False]
        (dropped,) = [e for e in events if e.type == "follow_ups_dropped"]
        assert dropped.dropped == ["a", "b"]
        assert dropped.cursor_id == cursor.id
        assert not cursor.turn_lock.locked()

    async def test_an_extension_abort_leaves_waiting_enqueues_alone(self):
        session = _session(lambda api, ctx: None)
        provider = _HeldProvider()
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            first = asyncio.ensure_future(session.submit(_sub("go")))
            await provider.started.wait()
            waiting = asyncio.ensure_future(session.submit(_sub("next", "enqueue")))
            await _settle()
            session.cursor.abort_signal.abort(reason="extension")
            provider.release.set()
            await first
            result = await waiting

        assert result.accepted
        assert _user_texts(session) == ["go", "next"]


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
