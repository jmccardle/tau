"""TUI live-Session bind — the AgentSession is the SOLE persister (E3-ctx / D3, S18).

The interactive app used to run its backend's ``AgentSession`` against a throwaway
``InMemorySessionLog`` while ``app.py`` separately appended every produced message
to its own live ``Session`` — two write paths, and the scratch log meant an
agent-driven ``compact``/``navigate`` would mutate the wrong store. S18 retires that
split: the TUI rebinds the backend's ``AgentSession`` onto the live ``Session``
(``TauBackend.bind_session_log``), drops its own ``append_message`` writes, and
rebuilds ``self.messages`` from ``session.context`` at turn-end (a VIEW over the
``ConversationTree``, pi ``rebuildChatFromMessages``).

These drive the REAL ``TauBackend`` through the app with the LLM boundary faked
(``agent_loop.stream_simple``), asserting:

- the turn's user + assistant land in the live session EXACTLY once (no double
  persist — the old app-side append is gone);
- ``self.messages`` after the turn is exactly ``session.context`` (the view), not an
  independently-accumulated list.
"""

from __future__ import annotations

import pytest

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, TextContent, Usage
from tau_coding_agent.app import Parley


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5),
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
    text = "ANSWER"
    return _Stream(
        [
            TextDeltaEvent(delta=text, partial=_assistant(text)),
            DoneEvent(final=_assistant(text), usage=_assistant(text).usage),
        ]
    )


@pytest.fixture
def app(monkeypatch, tmp_path):
    """Real ``TauBackend`` + a faked LLM, session persistence sandboxed to tmp."""
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    monkeypatch.setattr("tau_agent_core.agent_loop.stream_simple", _fake_stream_simple)

    a = Parley()
    a.config = {
        "models": {
            "m": {
                "backend": "openai",
                "model": "m",
                "base_url": "http://localhost/v1",
                "api_key": "not-needed",
                "tools": [],  # single completion, deterministic
            }
        },
        "default_model": "m",
        "system_prompt": "sys",
    }
    return a


class _Submit:
    """Duck-typed Input.Submitted — on_input_submitted only reads ``.value``."""

    def __init__(self, value: str) -> None:
        self.value = value


def _roles(entries) -> list[str]:
    return [e["message"]["role"] for e in entries if e.get("type") == "message"]


async def test_turn_persists_through_live_session_exactly_once(app):
    async with app.run_test() as pilot:
        await pilot.pause()

        await app.on_input_submitted(_Submit("hello"))
        await app.workers.wait_for_complete()
        await pilot.pause()

        session = app.current_session
        assert session is not None

        # The turn's user + assistant were recorded through the live session ONCE.
        # (The old code double-persisted; a second writer would show up as an extra
        # user/assistant entry here.)
        roles = _roles(session.entries())
        assert roles.count("user") == 1
        assert roles.count("assistant") == 1
        # The user turn was persisted by the AgentSession, not app.py.
        user_entries = [
            e
            for e in session.entries()
            if e.get("type") == "message" and e["message"]["role"] == "user"
        ]
        assert user_entries[0]["message"]["content"] == [{"type": "text", "text": "hello"}]


async def test_working_list_is_a_view_over_session_context(app):
    async with app.run_test() as pilot:
        await pilot.pause()

        await app.on_input_submitted(_Submit("hello"))
        await app.workers.wait_for_complete()
        await pilot.pause()

        session = app.current_session
        assert session is not None

        # self.messages is rebuilt from the authoritative log at turn-end — it is a
        # VIEW, byte-identical to session.context, not a separately accumulated list.
        assert app.messages == list(session.context)
        # And it carries the full turn: system prompt, the user turn, the answer.
        assert app.messages[0] == {"role": "system", "content": "sys"}
        assert any(m.get("role") == "user" for m in app.messages)
        assert any(m.get("role") == "assistant" for m in app.messages)


async def test_second_turn_does_not_duplicate_the_first(app):
    """Two turns in a row: the live log grows by exactly one exchange each time —
    the history-duplication failure mode (context re-fed and re-persisted) cannot
    occur when the session is the single writer."""
    async with app.run_test() as pilot:
        await pilot.pause()

        await app.on_input_submitted(_Submit("first"))
        await app.workers.wait_for_complete()
        await pilot.pause()

        await app.on_input_submitted(_Submit("second"))
        await app.workers.wait_for_complete()
        await pilot.pause()

        session = app.current_session
        assert session is not None
        roles = _roles(session.entries())
        # system + (user, assistant) × 2 — no exchange re-appended.
        assert roles == ["system", "user", "assistant", "user", "assistant"]
        assert app.messages == list(session.context)
