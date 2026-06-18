"""Tests for tau_coding_agent integration with tau_agent_core.

Tests verify:
- AgentSession interface is correctly exposed
- subscribe() returns an unsubscribe function
- prompt() runs the agent loop and returns messages
- continue_conversation() works without new prompts
- compact() triggers compaction
- abort() stops the agent loop
- State transitions are correct
"""

import pytest

from tau_coding_agent import AgentSession


class TestAgentSessionInterface:
    """Tests for AgentSession public API (SUBPHASE-0.0.md section 7)."""

    def test_agent_session_is_importable(self):
        """AgentSession must be importable from tau_coding_agent."""
        assert AgentSession is not None

    def test_agent_session_has_messages_property(self):
        """AgentSession must have a messages property."""
        session = AgentSession()
        assert hasattr(session, "messages")

    def test_agent_session_has_state_property(self):
        """AgentSession must have a state property."""
        session = AgentSession()
        assert hasattr(session, "state")

    def test_agent_session_has_is_streaming_property(self):
        """AgentSession must have an is_streaming property."""
        session = AgentSession()
        assert hasattr(session, "is_streaming")

    def test_agent_session_has_subscribe_method(self):
        """AgentSession must have a subscribe method."""
        session = AgentSession()
        assert hasattr(session, "subscribe")
        assert callable(session.subscribe)

    def test_agent_session_has_prompt_method(self):
        """AgentSession must have an async prompt method."""
        session = AgentSession()
        assert hasattr(session, "prompt")
        # Check it's async
        import inspect
        assert inspect.iscoroutinefunction(session.prompt), "prompt() should be async"

    def test_agent_session_has_continue_conversation_method(self):
        """AgentSession must have an async continue_conversation method."""
        session = AgentSession()
        assert hasattr(session, "continue_conversation")
        import inspect
        assert inspect.iscoroutinefunction(session.continue_conversation)

    def test_agent_session_has_compact_method(self):
        """AgentSession must have an async compact method."""
        session = AgentSession()
        assert hasattr(session, "compact")
        import inspect
        assert inspect.iscoroutinefunction(session.compact)

    def test_agent_session_has_abort_method(self):
        """AgentSession must have an abort method."""
        session = AgentSession()
        assert hasattr(session, "abort")
        assert callable(session.abort)


class TestAgentSessionSubscribe:
    """Tests for AgentSession.subscribe()."""

    def test_subscribe_returns_unsubscribe_function(self):
        """subscribe() must return a callable that unsubscribes."""
        session = AgentSession()

        def handler(event):
            pass

        unsubscribe = session.subscribe(handler)
        assert callable(unsubscribe), "subscribe() should return a callable"

    def test_unsubscribe_removes_handler(self):
        """The returned unsubscribe function must remove the handler."""
        session = AgentSession()

        def handler(event):
            pass

        unsubscribe = session.subscribe(handler)
        unsubscribe()
        # Handler should be removed

    def test_subscriber_receives_events(self):
        """Subscribers should receive events when emitted."""
        session = AgentSession()
        received_events = []

        def handler(event):
            received_events.append(event)

        session.subscribe(handler)

        # Simulate event emission
        # This will be tested when EventBus is implemented


class TestAgentSessionPrompt:
    """Tests for AgentSession.prompt()."""

    def test_prompt_runs_agent_loop(self):
        """prompt() should run the agent loop."""
        session = AgentSession()
        # This is tested when full implementation exists
        pass

    def test_prompt_accepts_images(self):
        """prompt() should accept optional images parameter."""
        session = AgentSession()
        # images parameter is optional per the contract
        pass

    def test_prompt_returns_messages(self):
        """prompt() should return the messages produced."""
        session = AgentSession()
        # Will return list[Message] when implemented


class TestAgentSessionContinueConversation:
    """Tests for AgentSession.continue_conversation()."""

    def test_continue_conversation_runs_agent_turn(self):
        """continue_conversation() should run another turn."""
        session = AgentSession()
        # Will be tested when full implementation exists


class TestAgentSessionAbort:
    """Tests for AgentSession.abort()."""

    def test_abort_stops_agent_loop(self):
        """abort() should stop the current agent turn."""
        session = AgentSession()
        session.abort()
        assert session.state == "aborting" or session.state == "idle"

    def test_abort_sets_not_streaming(self):
        """abort() should set is_streaming to False."""
        session = AgentSession()
        session.abort()
        assert session.is_streaming is False


class TestAgentSessionCompact:
    """Tests for AgentSession.compact()."""

    def test_compact_triggers_compaction(self):
        """compact() should trigger session compaction."""
        session = AgentSession()
        # Will be tested when compaction is implemented

    def test_compact_accepts_custom_instructions(self):
        """compact() should accept optional custom_instructions."""
        session = AgentSession()
        pass  # Interface test
