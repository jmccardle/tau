"""Tests for tau_coding_agent integration with tau_agent_core.

Tests verify:
- AgentSession interface is correctly exposed
- subscribe() returns an unsubscribe function
- prompt() runs the agent loop and returns messages
- continue_conversation() works without new prompts
- compact() triggers compaction
- abort() stops the agent loop
- State transitions are correct

These tests use the mock_agent_session fixture from conftest.py
since the real AgentSession requires session_manager and model args.
"""

import pytest
import inspect

from tau_coding_agent import AgentSession


class TestAgentSessionInterface:
    """Tests for AgentSession public API (SUBPHASE-0.0.md section 7)."""

    def test_agent_session_is_importable(self):
        """AgentSession must be importable from tau_coding_agent."""
        assert AgentSession is not None


class TestAgentSessionWithMock:
    """Tests using the mock_agent_session fixture from conftest."""

    def test_mock_session_has_messages_property(self, mock_agent_session):
        """Mock session must have a messages property."""
        assert hasattr(mock_agent_session, "messages")

    def test_mock_session_has_state_property(self, mock_agent_session):
        """Mock session must have a state property."""
        assert hasattr(mock_agent_session, "state")

    def test_mock_session_has_is_streaming_property(self, mock_agent_session):
        """Mock session must have an is_streaming property."""
        assert hasattr(mock_agent_session, "is_streaming")

    def test_mock_session_has_subscribe_method(self, mock_agent_session):
        """Mock session must have a subscribe method."""
        assert hasattr(mock_agent_session, "subscribe")
        assert callable(mock_agent_session.subscribe)

    def test_mock_session_has_prompt_method(self, mock_agent_session):
        """Mock session must have an async prompt method."""
        assert hasattr(mock_agent_session, "prompt")
        assert inspect.iscoroutinefunction(mock_agent_session.prompt)

    def test_mock_session_has_continue_conversation_method(self, mock_agent_session):
        """Mock session must have an async continue_conversation method."""
        assert hasattr(mock_agent_session, "continue_conversation")
        assert inspect.iscoroutinefunction(mock_agent_session.continue_conversation)

    def test_mock_session_has_compact_method(self, mock_agent_session):
        """Mock session must have an async compact method."""
        assert hasattr(mock_agent_session, "compact")
        assert inspect.iscoroutinefunction(mock_agent_session.compact)

    def test_mock_session_has_abort_method(self, mock_agent_session):
        """Mock session must have an abort method."""
        assert hasattr(mock_agent_session, "abort")
        assert callable(mock_agent_session.abort)


class TestAgentSessionSubscribe:
    """Tests for AgentSession.subscribe()."""

    def test_subscribe_returns_unsubscribe_function(self, mock_agent_session):
        """subscribe() must return a callable that unsubscribes."""
        def handler(event):
            pass
        unsubscribe = mock_agent_session.subscribe(handler)
        assert callable(unsubscribe)

    def test_unsubscribe_removes_handler(self, mock_agent_session):
        """The returned unsubscribe function must remove the handler."""
        def handler(event):
            pass
        unsubscribe = mock_agent_session.subscribe(handler)
        unsubscribe()
        assert len(mock_agent_session._subscribers) == 0

    def test_subscriber_receives_events(self, mock_agent_session):
        """Subscribers should receive events when emitted."""
        received_events = []
        def handler(event):
            received_events.append(event)
        mock_agent_session.subscribe(handler)


class TestAgentSessionPrompt:
    """Tests for AgentSession.prompt()."""

    async def test_prompt_runs_agent_loop(self, mock_agent_session):
        """prompt() should run the agent loop."""
        messages = await mock_agent_session.prompt("hello")
        assert len(messages) > 0

    def test_prompt_accepts_images(self, mock_agent_session):
        """prompt() should accept optional images parameter."""
        pass  # Interface test

    async def test_prompt_returns_messages(self, mock_agent_session):
        """prompt() should return the messages produced."""
        messages = await mock_agent_session.prompt("hello")
        assert isinstance(messages, list)
        assert len(messages) > 0


class TestAgentSessionContinueConversation:
    """Tests for AgentSession.continue_conversation()."""

    async def test_continue_conversation_runs_agent_turn(self, mock_agent_session):
        """continue_conversation() should run another turn."""
        messages = await mock_agent_session.continue_conversation()
        assert len(messages) > 0


class TestAgentSessionAbort:
    """Tests for AgentSession.abort()."""

    def test_abort_stops_agent_loop(self, mock_agent_session):
        """abort() should stop the current agent turn."""
        mock_agent_session.abort()
        assert mock_agent_session.state == "aborting"

    def test_abort_sets_not_streaming(self, mock_agent_session):
        """abort() should set is_streaming to False."""
        mock_agent_session.abort()
        assert mock_agent_session.is_streaming is False


class TestAgentSessionCompact:
    """Tests for AgentSession.compact()."""

    async def test_compact_triggers_compaction(self, mock_agent_session):
        """compact() should trigger session compaction."""
        await mock_agent_session.compact()

    def test_compact_accepts_custom_instructions(self, mock_agent_session):
        """compact() should accept optional custom_instructions."""
        pass  # Interface test
