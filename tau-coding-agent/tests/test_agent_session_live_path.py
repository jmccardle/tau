"""Live-path wiring — the coding-agent file ``Session`` IS the SessionLog (§2.6).

Decision-4 option (B): on the live path the coding-agent ``session_store.Session``
satisfies ``tau_agent_core.session_log.SessionLog`` *structurally* and is injected
into ``AgentSession``, which then persists this turn's messages through it and
rebuilds context from its entries via ``ConversationTree``. This is the seam that
would let a fork swap in a DB-backed store returning the same tree (Part 3).

The LLM boundary is patched (``agent_loop.stream_simple``) so the full loop runs
without a network call — the same technique tau-agent-core's ``fake_llm`` uses.

Reference: SESSION-TREE-IMPLEMENTATION.md §2.6, §2.7, §4.2, §4.5.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import SessionLog
from tau_coding_agent.session_store import (
    SESSION_BEFORE_COMPACT,
    Session,
    subscribe_session_events,
)


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _EventIterator:
    def __init__(self, events):
        self._events = events
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._i]
        self._i += 1
        return event


class _Stream:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        return _EventIterator(self._events)

    async def result(self):
        for event in self._events:
            if isinstance(event, DoneEvent):
                return event.final
        return None

    def abort(self):
        pass


async def _fake_stream_simple(model, context, options=None):
    text = "ok"
    return _Stream(
        [
            TextDeltaEvent(delta=text, partial=_assistant(text)),
            DoneEvent(
                final=_assistant(text),
                usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            ),
        ]
    )


@pytest.fixture
def fake_llm():
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_stream_simple):
        yield


def test_session_satisfies_sessionlog_protocol():
    session = Session.create_in_memory("/tmp", "gpt-4o", "openai")
    assert isinstance(session, SessionLog)


def test_agent_session_reads_context_via_conversation_tree(fake_llm):
    # An in-memory Session (path=None) is the SessionLog; it already carries a
    # system message from create_in_memory's _init_state.
    store = Session.create_in_memory("/tmp", "gpt-4o", "openai", system_prompt="be brief")
    session = AgentSession(session_log=store, model=_model())

    # AgentSession.messages must be exactly the ConversationTree fold over the
    # live Session's entries + cursor — not a separate System-A view.
    assert session.messages == ConversationTree(store.entries(), store.cursor).context_for()
    assert session.messages[0] == {"role": "system", "content": "be brief"}


def test_prompt_persists_through_the_live_session(fake_llm):
    store = Session.create_in_memory("/tmp", "gpt-4o", "openai")
    before = len(store.entries())
    session = AgentSession(session_log=store, model=_model())

    asyncio.run(session.prompt("hello"))

    # The turn's user + assistant messages were appended to the SAME Session the
    # TUI/headless persist through (append-only: entries only grow).
    after = store.entries()
    assert len(after) > before
    folded = ConversationTree(store.entries(), store.cursor).context_for()
    assert session.messages == folded
    roles = [m["role"] for m in folded]
    assert "user" in roles and "assistant" in roles
    # Identity is the session UUID, never a path (§4.2).
    assert session.state.session_id == store.id


# ── seam-3 lifecycle events reach the extension bus (S21 / §E3c.4) ───────────


async def test_seam3_before_compact_reaches_extension_handler():
    """A coding-agent ``session_before_compact`` event routed through the bridge
    fires an ``api.on("session_before_compact", …)`` extension handler.

    End-to-end through the real seam: a real ``session_store.Session`` (which IS the
    AgentSession's SessionLog on the live path) emits the raw dict via
    ``_emit_session_event`` on ``append_compaction`` — the genuine emit point that
    ``AgentSession.compact`` drives. ``subscribe_session_events`` delivers it to
    ``AgentSession.route_session_event``, which re-emits it onto the session's
    ``EventBus`` on the ``"session_before_compact"`` channel where the extension's
    handler is subscribed. Nothing here is faked: real emitter, real bridge, real
    bus, real ExtensionAPI-registered handler.
    """
    received: list[dict] = []

    def ext(api):
        api.on(SESSION_BEFORE_COMPACT, lambda event: received.append(event))

    store = Session.create_in_memory("/tmp", "gpt-4o", "openai")
    first_kept = store.append_message({"role": "user", "content": "keep me"})
    session = AgentSession(session_log=store, model=_model(), extensions=[ext])

    unsub = subscribe_session_events(session.route_session_event)
    try:
        # The genuine seam-3 emit point (append_compaction), which compact() calls.
        store.append_compaction("summary", first_kept_id=first_kept, tokens_before=42)
        # The bus dispatch is a fire-and-forget task scheduled on the running loop;
        # yield once so it runs before we assert.
        await asyncio.sleep(0)
    finally:
        unsub()

    assert len(received) == 1
    event = received[0]
    assert event["type"] == SESSION_BEFORE_COMPACT
    assert event["session"] is store
    assert event["first_kept_id"] == first_kept


async def test_seam3_channel_isolation():
    """The bridge routes onto the event's OWN string channel — a handler on a
    different channel does NOT fire (separate-channel routing, §7 decision E3-c)."""
    before_compact: list[dict] = []
    other_channel: list[dict] = []

    def ext(api):
        api.on(SESSION_BEFORE_COMPACT, lambda event: before_compact.append(event))
        api.on("session_start", lambda event: other_channel.append(event))

    store = Session.create_in_memory("/tmp", "gpt-4o", "openai")
    first_kept = store.append_message({"role": "user", "content": "keep me"})
    session = AgentSession(session_log=store, model=_model(), extensions=[ext])

    unsub = subscribe_session_events(session.route_session_event)
    try:
        store.append_compaction("summary", first_kept_id=first_kept, tokens_before=0)
        await asyncio.sleep(0)
    finally:
        unsub()

    assert len(before_compact) == 1
    assert other_channel == []
