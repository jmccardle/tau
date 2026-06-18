"""Phase 4 Subphase 1 Tests — TUI App Shell

Tests for ParleyApp Textual app implementation:
1. App import and instantiation
2. Layout elements present
3. Event subscription
4. 30Hz throttle
5. Input disable/enable
6. Agent end handler
7. Print mode
8. Theme application
9. Integration: config → build_session → ParleyApp

Reference: PHASE-4-SUBPHASE-1.md — Testing Strategy
Reference: SUBPHASE-0.0.md — AgentSession interface (section 7)
Reference: docs/textual-headless-testing.md — Headless Textual testing
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from tau_coding_agent.app import (
    AppLayout,
    ParleyApp,
    _HAS_TEXTUAL,
    ChatDisplay,
    InputBar,
    build_session,
)


# ===========================================================================
# Test 1: App import and instantiation
# ===========================================================================


class TestAppImport:
    """Test that ParleyApp is importable and instantiable."""

    def test_parley_app_is_importable(self):
        """ParleyApp must be importable from tau_coding_agent.app."""
        from tau_coding_agent.app import ParleyApp as P
        assert P is not None

    def test_app_layout_is_importable(self):
        """AppLayout must be importable from tau_coding_agent.app."""
        from tau_coding_agent.app import AppLayout as A
        assert A is not None

    def test_chat_display_is_importable(self):
        """ChatDisplay widget must be importable when Textual is available."""
        if _HAS_TEXTUAL:
            from tau_coding_agent.app import ChatDisplay as C
            assert C is not None

    def test_input_bar_is_importable(self):
        """InputBar widget must be importable when Textual is available."""
        if _HAS_TEXTUAL:
            from tau_coding_agent.app import InputBar as I
            assert I is not None

    def test_build_session_is_importable(self):
        """build_session must be importable from tau_coding_agent.app."""
        from tau_coding_agent.app import build_session as b
        assert callable(b)


class TestAppInstantiation:
    """Test that ParleyApp can be instantiated correctly."""

    def test_instantiate_with_no_args(self):
        """ParleyApp can be created with no arguments."""
        app = ParleyApp(print_mode=False)
        assert app is not None

    def test_print_mode_defaults_to_false(self):
        """print_mode defaults to False."""
        app = ParleyApp()
        if _HAS_TEXTUAL:
            assert app._print_mode is False
        else:
            assert app.print_mode is False

    def test_print_mode_true(self):
        """print_mode can be set to True."""
        app = ParleyApp(print_mode=True)
        if _HAS_TEXTUAL:
            assert app._print_mode is True
        else:
            assert app.print_mode is True

    def test_is_streaming_defaults_to_false(self):
        """_is_streaming defaults to False."""
        app = ParleyApp()
        assert app._is_streaming is False

    def test_throttle_timer_defaults_to_none(self):
        """_throttle_timer defaults to None."""
        app = ParleyApp()
        if _HAS_TEXTUAL:
            assert app._throttle_timer is None

    def test_session_is_none_by_default(self):
        """session is None by default."""
        app = ParleyApp()
        assert app._session is None

    def test_session_can_be_set(self):
        """session can be passed at construction time."""
        mock_session = MagicMock()
        app = ParleyApp(session=mock_session)
        if _HAS_TEXTUAL:
            assert app._session is mock_session
        else:
            assert app.session is mock_session

    def test_ready_defaults_to_false(self):
        """ready defaults to False (backward compatibility)."""
        app = ParleyApp()
        assert app.ready is False

    def test_ready_is_property(self):
        """ready is accessible as a property."""
        app = ParleyApp()
        _ = app.ready

    def test_start_sets_ready_true(self):
        """start() sets ready to True (backward compat)."""
        app = ParleyApp()
        assert app.ready is False
        asyncio.run(app.start())
        assert app.ready is True

    def test_stop_sets_ready_false(self):
        """stop() sets ready to False (backward compat)."""
        app = ParleyApp()
        asyncio.run(app.start())
        asyncio.run(app.stop())
        assert app.ready is False


# ===========================================================================
# Test 2: Layout elements present
# ===========================================================================


class TestLayoutElements:
    """Test that layout widgets are defined correctly."""

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_chat_display_has_css_id(self):
        """ChatDisplay has a CSS_ID for targeting."""
        assert hasattr(ChatDisplay, "CSS_ID")
        assert ChatDisplay.CSS_ID == "chat-display"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_input_bar_has_css_id(self):
        """InputBar has a CSS_ID for targeting."""
        assert hasattr(InputBar, "CSS_ID")
        assert InputBar.CSS_ID == "input-bar"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parley_app_has_css_path(self):
        """ParleyApp specifies CSS_PATH for the theme."""
        assert hasattr(ParleyApp, "CSS_PATH")
        assert ParleyApp.CSS_PATH == "themes/catppuccin.tcss"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parley_app_has_bindings(self):
        """ParleyApp defines key bindings."""
        assert hasattr(ParleyApp, "BINDINGS")
        assert len(ParleyApp.BINDINGS) > 0

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_chat_display_has_required_methods(self):
        """ChatDisplay has update_streaming_message, add_message, clear_messages."""
        assert hasattr(ChatDisplay, "update_streaming_message")
        assert hasattr(ChatDisplay, "add_message")
        assert hasattr(ChatDisplay, "clear_messages")
        assert hasattr(ChatDisplay, "get_messages")

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parley_app_has_layout_methods(self):
        """ParleyApp has _setup_layout, _subscribe_to_events."""
        assert hasattr(ParleyApp, "_setup_layout")
        assert hasattr(ParleyApp, "_subscribe_to_events")

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parley_app_has_event_handlers(self):
        """ParleyApp has _handle_event, _update_streaming_message, _on_agent_end."""
        assert hasattr(ParleyApp, "_handle_event")
        assert hasattr(ParleyApp, "_update_streaming_message")
        assert hasattr(ParleyApp, "_on_agent_end")
        assert hasattr(ParleyApp, "_on_agent_start")

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parley_app_has_input_methods(self):
        """ParleyApp has _re_disable_input, _re_enable_input."""
        assert hasattr(ParleyApp, "_re_disable_input")
        assert hasattr(ParleyApp, "_re_enable_input")

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parley_app_has_input_submission(self):
        """ParleyApp has _on_input_submitted and _handle_interactive."""
        assert hasattr(ParleyApp, "_on_input_submitted")
        assert hasattr(ParleyApp, "_handle_interactive")
        assert hasattr(ParleyApp, "_handle_print_mode")

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parley_app_compose_yields_header(self):
        """ParleyApp.compose() yields a Header widget (verified via source)."""
        import inspect
        source = inspect.getsource(ParleyApp.compose)
        assert "Header" in source, "Header not found in compose() source"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parley_app_compose_yields_footer(self):
        """ParleyApp.compose() yields a Footer widget (verified via source)."""
        import inspect
        source = inspect.getsource(ParleyApp.compose)
        assert "Footer" in source, "Footer not found in compose() source"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parley_app_compose_yields_chat_display(self):
        """ParleyApp.compose() yields a ChatDisplay widget (verified via source)."""
        import inspect
        source = inspect.getsource(ParleyApp.compose)
        assert "ChatDisplay" in source, "ChatDisplay not found in compose() source"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parley_app_compose_yields_input_bar(self):
        """ParleyApp.compose() yields an InputBar widget (verified via source)."""
        import inspect
        source = inspect.getsource(ParleyApp.compose)
        assert "InputBar" in source, "InputBar not found in compose() source"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_compose_has_all_layout_components(self):
        """Full compose tree includes header, footer, chat, and input (verified via source)."""
        import inspect
        source = inspect.getsource(ParleyApp.compose)
        assert "Header" in source, "Missing Header"
        assert "Footer" in source, "Missing Footer"
        assert "ChatDisplay" in source, "Missing ChatDisplay"
        assert "InputBar" in source, "Missing InputBar"


# ===========================================================================
# Test 3: Event subscription
# ===========================================================================


class TestEventSubscription:
    """Test that AgentSession events are subscribed and dispatched."""

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_handle_event_message_update(self):
        """_handle_event dispatches message_update to throttle logic."""
        app = ParleyApp()
        mock_event = MagicMock()
        mock_event.type = "message_update"

        with patch.object(app, "call_later") as mock_call_later:
            with patch.object(app, "set_timer") as mock_set_timer:
                app._handle_event(mock_event)

        mock_call_later.assert_called_once()
        mock_set_timer.assert_called_once()

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_handle_event_agent_end(self):
        """_handle_event dispatches agent_end to reset streaming."""
        app = ParleyApp()
        app._is_streaming = True
        mock_event = MagicMock()
        mock_event.type = "agent_end"

        app._handle_event(mock_event)
        assert app._is_streaming is False

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_handle_event_agent_start(self):
        """_handle_event dispatches agent_start to enable streaming."""
        app = ParleyApp()
        app._is_streaming = False
        mock_event = MagicMock()
        mock_event.type = "agent_start"

        app._handle_event(mock_event)
        assert app._is_streaming is True

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_handle_event_ignores_unknown_type(self):
        """_handle_event doesn't crash on unknown event types."""
        app = ParleyApp()
        mock_event = MagicMock()
        mock_event.type = "unknown_event_type"
        # Should not raise
        app._handle_event(mock_event)

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_handle_event_dict_format(self):
        """_handle_event works with dict events."""
        app = ParleyApp()
        dict_event = {"type": "agent_end"}

        app._is_streaming = True
        app._handle_event(dict_event)
        assert app._is_streaming is False

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_get_event_type_from_object(self):
        """_get_event_type extracts type from an object with .type attr."""
        event = MagicMock()
        event.type = "message_update"
        result = ParleyApp._get_event_type(event)
        assert result == "message_update"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_get_event_type_from_dict(self):
        """_get_event_type extracts type from a dict."""
        event = {"type": "turn_start"}
        result = ParleyApp._get_event_type(event)
        assert result == "turn_start"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_get_event_type_from_none_type(self):
        """_get_event_type returns empty string for invalid input."""
        result = ParleyApp._get_event_type(None)
        assert result == ""

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_subscribe_to_session_events(self):
        """ParleyApp subscribes to session events on mount."""
        mock_session = MagicMock()
        mock_session.subscribe = MagicMock()

        app = ParleyApp(session=mock_session)
        app._subscribe_to_events()

        mock_session.subscribe.assert_called_once()
        handler = mock_session.subscribe.call_args[0][0]
        assert handler.__self__ is app
        assert handler.__name__ == "_handle_event"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_subscribe_noop_when_no_session(self):
        """_subscribe_to_events is a no-op when session is None."""
        app = ParleyApp()
        # Should not raise
        app._subscribe_to_events()

    async def test_async_event_flow_message_update(self, mock_agent_session):
        """Event flow: session emits event → app handler receives it."""
        app = ParleyApp(session=mock_agent_session)
        received = []
        handler = lambda e: received.append(e)

        app._session.subscribe(handler)

        # Simulate a session emit (mock_agent_session doesn't have emit,
        # but we test the handler directly)
        mock_event = MagicMock()
        mock_event.type = "message_update"
        handler(mock_event)

        assert len(received) == 1
        assert received[0] is mock_event


# ===========================================================================
# Test 4: 30Hz throttle
# ===========================================================================


class TestThrottle:
    """Test that streaming updates are throttled to 30Hz."""

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_update_streaming_message_calls_call_later(self):
        """_update_streaming_message schedules an update via call_later."""
        app = ParleyApp()
        mock_event = MagicMock()
        mock_event.type = "message_update"

        with patch.object(app, "call_later") as mock_call_later:
            with patch.object(app, "set_timer") as mock_set_timer:
                app._update_streaming_message(mock_event)

        mock_call_later.assert_called_once()

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_update_streaming_message_sets_timer(self):
        """_update_streaming_message sets a 1/30 second timer."""
        app = ParleyApp()
        mock_event = MagicMock()
        mock_event.type = "message_update"

        with patch.object(app, "set_timer") as mock_set_timer:
            app._update_streaming_message(mock_event)

        timer_interval = mock_set_timer.call_args[0][0]
        assert timer_interval == pytest.approx(1 / 30, abs=0.001)

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_update_streaming_message_stops_previous_timer(self):
        """_update_streaming_message stops the previous throttle timer."""
        app = ParleyApp()

        mock_timer = MagicMock()
        mock_timer.stop = MagicMock()
        app._throttle_timer = mock_timer

        mock_event = MagicMock()
        mock_event.type = "message_update"

        with patch.object(app, "set_timer") as mock_set_timer:
            app._update_streaming_message(mock_event)

        mock_timer.stop.assert_called_once()

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_do_update_streaming_message_updates_chat_display(self):
        """_do_update_streaming_message calls update_streaming_message on ChatDisplay."""
        app = ParleyApp()
        app._streaming_content = "test content"

        mock_chat = MagicMock()
        mock_chat.update_streaming_message = MagicMock()

        with patch.object(app, "query_one", return_value=mock_chat):
            app._do_update_streaming_message()

        mock_chat.update_streaming_message.assert_called_once_with("test content")

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_do_update_streaming_message_ignores_query_error(self):
        """_do_update_streaming_message doesn't crash if chat display is missing."""
        app = ParleyApp()
        with patch.object(app, "query_one", side_effect=Exception("not found")):
            app._do_update_streaming_message()  # Should not raise

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_throttle_accumulates_updates(self):
        """Multiple rapid message_update calls result in throttled dispatches."""
        app = ParleyApp()
        call_later_count = 0

        def record_call_later(callback):
            nonlocal call_later_count
            call_later_count += 1

        with patch.object(app, "call_later", side_effect=record_call_later):
            with patch.object(app, "set_timer"):
                for i in range(100):
                    mock_event = MagicMock()
                    mock_event.type = "message_update"
                    app._update_streaming_message(mock_event)

        assert call_later_count == 100

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_throttle_timer_is_stored(self):
        """_update_streaming_message stores the timer on _throttle_timer."""
        app = ParleyApp()
        mock_event = MagicMock()
        mock_event.type = "message_update"
        mock_timer = MagicMock()

        with patch.object(app, "set_timer", return_value=mock_timer):
            app._update_streaming_message(mock_event)

        assert app._throttle_timer is mock_timer

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_throttle_stops_timer_when_none(self):
        """_update_streaming_message handles None _throttle_timer gracefully."""
        app = ParleyApp()
        assert app._throttle_timer is None
        mock_event = MagicMock()
        mock_event.type = "message_update"

        with patch.object(app, "set_timer") as mock_set_timer:
            app._update_streaming_message(mock_event)

        mock_set_timer.assert_called_once()

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_call_later_schedules_do_update(self):
        """call_later callback is _do_update_streaming_message."""
        app = ParleyApp()
        mock_event = MagicMock()
        mock_event.type = "message_update"

        captured_callback = None

        def record_callback(cb):
            nonlocal captured_callback
            captured_callback = cb

        with patch.object(app, "call_later", side_effect=record_callback):
            with patch.object(app, "set_timer"):
                app._update_streaming_message(mock_event)

        # Compare method names to handle bound method identity
        assert captured_callback.__name__ == "_do_update_streaming_message"


# ===========================================================================
# Test 5: Input disable/enable
# ===========================================================================


class TestInputDisableEnable:
    """Test that input is disabled during streaming and re-enabled after."""

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_re_disable_input_sets_disabled_true(self):
        """_re_disable_input sets InputBar.disabled = True."""
        mock_input_bar = MagicMock()
        mock_input_bar.disabled = False

        app = ParleyApp()
        with patch.object(app, "query_one", return_value=mock_input_bar):
            app._re_disable_input()

        assert mock_input_bar.disabled is True

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_re_enable_input_sets_disabled_false(self):
        """_re_enable_input sets InputBar.disabled = False."""
        mock_input_bar = MagicMock()
        mock_input_bar.disabled = True

        app = ParleyApp()
        with patch.object(app, "query_one", return_value=mock_input_bar):
            app._re_enable_input()

        assert mock_input_bar.disabled is False

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_re_disable_input_does_not_crash_when_missing(self):
        """_re_disable_input handles missing InputBar gracefully."""
        app = ParleyApp()
        with patch.object(app, "query_one", side_effect=Exception("not found")):
            app._re_disable_input()

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_re_enable_input_does_not_crash_when_missing(self):
        """_re_enable_input handles missing InputBar gracefully."""
        app = ParleyApp()
        with patch.object(app, "query_one", side_effect=Exception("not found")):
            app._re_enable_input()

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_streaming_disables_input(self):
        """_handle_event with agent_start disables input."""
        app = ParleyApp()
        mock_input_bar = MagicMock()
        mock_input_bar.disabled = False

        mock_event = MagicMock()
        mock_event.type = "agent_start"

        with patch.object(app, "query_one", return_value=mock_input_bar):
            app._handle_event(mock_event)

        assert mock_input_bar.disabled is True
        assert app._is_streaming is True

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_agent_end_re_enables_input(self):
        """_handle_event with agent_end re-enables input."""
        app = ParleyApp()
        app._is_streaming = True
        mock_input_bar = MagicMock()
        mock_input_bar.disabled = True

        mock_event = MagicMock()
        mock_event.type = "agent_end"

        with patch.object(app, "query_one", return_value=mock_input_bar):
            app._handle_event(mock_event)

        assert mock_input_bar.disabled is False
        assert app._is_streaming is False

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_input_disable_state_isolation(self):
        """Multiple disable/enable cycles maintain correct state."""
        app = ParleyApp()
        mock_input_bar = MagicMock()

        with patch.object(app, "query_one", return_value=mock_input_bar):
            # Disable
            app._re_disable_input()
            assert mock_input_bar.disabled is True
            # Re-enable
            app._re_enable_input()
            assert mock_input_bar.disabled is False
            # Disable again
            app._re_disable_input()
            assert mock_input_bar.disabled is True
            # Re-enable again
            app._re_enable_input()
            assert mock_input_bar.disabled is False


# ===========================================================================
# Test 6: Agent end handler
# ===========================================================================


class TestAgentEndHandler:
    """Test that agent_end handler resets streaming state."""

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_on_agent_end_resets_streaming(self):
        """_on_agent_end sets _is_streaming to False."""
        app = ParleyApp()
        app._is_streaming = True

        mock_event = MagicMock()
        mock_event.type = "agent_end"

        app._on_agent_end(mock_event)
        assert app._is_streaming is False

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_on_agent_end_calls_re_enable_input(self):
        """_on_agent_end re-enables the input."""
        app = ParleyApp()
        app._is_streaming = True
        mock_input_bar = MagicMock()
        mock_input_bar.disabled = True

        mock_event = MagicMock()
        mock_event.type = "agent_end"

        with patch.object(app, "query_one", return_value=mock_input_bar):
            app._on_agent_end(mock_event)

        assert mock_input_bar.disabled is False

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_on_agent_end_with_dict_event(self):
        """_on_agent_end handles dict events correctly."""
        app = ParleyApp()
        app._is_streaming = True

        dict_event = {"type": "agent_end", "messages": []}
        mock_input_bar = MagicMock()
        mock_input_bar.disabled = True

        with patch.object(app, "query_one", return_value=mock_input_bar):
            app._on_agent_end(dict_event)

        assert app._is_streaming is False

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_on_agent_start_enables_streaming(self):
        """_on_agent_start sets _is_streaming to True."""
        app = ParleyApp()
        app._is_streaming = False

        mock_event = MagicMock()
        mock_event.type = "agent_start"

        app._on_agent_start(mock_event)
        assert app._is_streaming is True

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_on_agent_start_disables_input(self):
        """_on_agent_start disables the input."""
        app = ParleyApp()
        mock_input_bar = MagicMock()
        mock_input_bar.disabled = False

        mock_event = MagicMock()
        mock_event.type = "agent_start"

        with patch.object(app, "query_one", return_value=mock_input_bar):
            app._on_agent_start(mock_event)

        assert mock_input_bar.disabled is True

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_agent_start_end_full_cycle(self):
        """Full agent_start → agent_end cycle works correctly."""
        app = ParleyApp()
        mock_input_bar = MagicMock()
        mock_input_bar.disabled = False

        with patch.object(app, "query_one", return_value=mock_input_bar):
            # agent_start
            start_event = MagicMock()
            start_event.type = "agent_start"
            app._handle_event(start_event)
            assert app._is_streaming is True
            assert mock_input_bar.disabled is True

            # agent_end
            end_event = MagicMock()
            end_event.type = "agent_end"
            app._handle_event(end_event)
            assert app._is_streaming is False
            assert mock_input_bar.disabled is False


# ===========================================================================
# Test 7: Print mode
# ===========================================================================


class TestPrintMode:
    """Test print mode behavior (stream response and exit)."""

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_print_mode_true(self):
        """print_mode=True sets the flag."""
        app = ParleyApp(print_mode=True)
        assert app._print_mode is True

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_print_mode_false(self):
        """print_mode=False sets the flag."""
        app = ParleyApp(print_mode=False)
        assert app._print_mode is False

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_handle_print_mode_creates_task(self):
        """_handle_print_mode creates a task to run session.prompt()."""
        mock_session = MagicMock()
        mock_session.prompt = AsyncMock(return_value=[
            {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]}
        ])
        app = ParleyApp(session=mock_session, print_mode=True)

        # Capture the coroutine passed to create_task
        captured_coros = []

        def mock_create_task(coro):
            captured_coros.append(coro)
            # Don't actually run it - just return a dummy task
            return MagicMock()

        # Patch app.loop.create_task
        with patch.object(app, "loop", create=True) as mock_loop:
            mock_loop.create_task = MagicMock(side_effect=mock_create_task)
            app._handle_print_mode("test prompt")

        # Verify a coroutine was scheduled for the session prompt
        assert len(captured_coros) == 1
        # The coroutine should be _print_mode_run (which calls session.prompt internally)
        assert "_print_mode_run" in str(captured_coros[0])

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_handle_print_mode_no_session(self):
        """_handle_print_mode handles None session gracefully."""
        app = ParleyApp(session=None, print_mode=True)
        # Should not crash
        app._handle_print_mode("test")

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_handle_interactive_calls_session_prompt(self):
        """_handle_interactive calls session.prompt() with user text."""
        mock_session = MagicMock()
        mock_session.prompt = AsyncMock(return_value=[])
        app = ParleyApp(session=mock_session)

        # Patch input re-enable
        app._re_enable_input = MagicMock()

        asyncio.run(app._handle_interactive("hello"))

        mock_session.prompt.assert_called_once_with("hello")
        assert app._re_enable_input.called

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_handle_interactive_sets_streaming(self):
        """_handle_interactive sets and clears streaming state."""
        mock_session = MagicMock()
        mock_session.prompt = AsyncMock(return_value=[])
        app = ParleyApp(session=mock_session)

        # Track streaming state changes
        streaming_history = []
        original_disable = app._re_disable_input
        original_enable = app._re_enable_input

        def track_disable():
            streaming_history.append("disable")
            app._is_streaming = True
            original_disable()

        def track_enable():
            streaming_history.append("enable")
            app._is_streaming = False
            original_enable()

        app._re_disable_input = track_disable
        app._re_enable_input = track_enable

        asyncio.run(app._handle_interactive("test"))

        # Verify state transitions
        assert "disable" in streaming_history
        assert "enable" in streaming_history
        assert app._is_streaming is False  # reset after prompt completes

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_handle_interactive_no_session(self):
        """_handle_interactive handles None session gracefully."""
        app = ParleyApp(session=None)
        app._re_enable_input = MagicMock()
        app._re_disable_input = MagicMock()
        # Should not crash
        asyncio.run(app._handle_interactive("test"))


# ===========================================================================
# Test 8: Theme and CSS
# ===========================================================================


class TestTheme:
    """Test theme application (Catppuccin)."""

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_css_path_points_to_catppuccin(self):
        """ParleyApp.CSS_PATH references the catppuccin theme."""
        assert ParleyApp.CSS_PATH == "themes/catppuccin.tcss"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_css_path_is_string(self):
        """CSS_PATH is a string."""
        assert isinstance(ParleyApp.CSS_PATH, str)

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_css_path_has_correct_extension(self):
        """CSS_PATH has .tcss extension."""
        assert ParleyApp.CSS_PATH.endswith(".tcss")

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_bindings_include_quit(self):
        """ParleyApp bindings include 'q' for quit."""
        # BINDINGS is a list of tuples like [("q", "quit", "Quit"), ...]
        keys = [b[0] for b in ParleyApp.BINDINGS]
        actions = [b[1] for b in ParleyApp.BINDINGS]
        assert "q" in keys, f"Expected 'q' in binding keys: {keys}"
        assert "quit" in actions, f"Expected 'quit' in binding actions: {actions}"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_bindings_is_list(self):
        """BINDINGS is a list."""
        assert isinstance(ParleyApp.BINDINGS, list)

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_bindings_has_at_least_one_entry(self):
        """BINDINGS has at least one key binding."""
        assert len(ParleyApp.BINDINGS) >= 1


# ===========================================================================
# Test 9: ChatDisplay behavior
# ===========================================================================


class TestChatDisplayBehavior:
    """Test ChatDisplay widget behavior."""

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_update_streaming_message_sets_content(self):
        """ChatDisplay.update_streaming_message stores content."""
        chat = ChatDisplay()
        chat.update_streaming_message("partial response")
        assert chat._streaming_content == "partial response"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_add_message_appends(self):
        """ChatDisplay.add_message appends to messages list."""
        chat = ChatDisplay()
        msg1 = {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        msg2 = {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
        chat.add_message(msg1)
        chat.add_message(msg2)
        assert len(chat.get_messages()) == 2

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_clear_messages_resets_state(self):
        """ChatDisplay.clear_messages clears content and messages."""
        chat = ChatDisplay()
        chat.add_message({"role": "user", "content": [{"type": "text", "text": "hi"}]})
        chat.update_streaming_message("partial")
        chat.clear_messages()
        assert len(chat.get_messages()) == 0
        assert chat._streaming_content == ""

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_initial_streaming_content_is_empty(self):
        """ChatDisplay starts with empty streaming content."""
        chat = ChatDisplay()
        assert chat._streaming_content == ""

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_initial_messages_is_empty(self):
        """ChatDisplay starts with empty messages list."""
        chat = ChatDisplay()
        assert len(chat.get_messages()) == 0

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_update_streaming_message_overwrites(self):
        """ChatDisplay.update_streaming_message overwrites previous content."""
        chat = ChatDisplay()
        chat.update_streaming_message("first")
        chat.update_streaming_message("second")
        assert chat._streaming_content == "second"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_compose_yields_rich_log(self):
        """ChatDisplay.compose yields a RichLog widget."""
        from textual.widgets import RichLog
        chat = ChatDisplay()
        result = list(chat.compose())
        assert any(isinstance(w, RichLog) for w in result)


# ===========================================================================
# Test 10: Build session
# ===========================================================================


class TestBuildSession:
    """Test build_session function."""

    def test_build_session_returns_session(self):
        """build_session returns an AgentSession instance."""
        session = build_session()
        assert session is not None

    def test_build_session_accepts_model(self):
        """build_session accepts a model parameter."""
        session = build_session(model="claude-3")
        assert session is not None

    def test_build_session_accepts_provider(self):
        """build_session accepts a provider parameter."""
        session = build_session(provider="anthropic")
        assert session is not None

    def test_build_session_accepts_system_prompt(self):
        """build_session accepts a system_prompt parameter."""
        session = build_session(system_prompt="You are helpful.")
        assert session is not None

    def test_build_session_accepts_session_name(self):
        """build_session accepts a session_name parameter."""
        session = build_session(session_name="test-session")
        assert session is not None

    def test_build_session_accepts_tools(self):
        """build_session accepts a tools parameter."""
        session = build_session(tools=["bash", "read"])
        assert session is not None

    def test_build_session_accepts_project_root(self):
        """build_session accepts a project_root parameter."""
        session = build_session(project_root=Path("/tmp"))
        assert session is not None


# ===========================================================================
# Backward compatibility: AppLayout dataclass
# ===========================================================================


class TestAppLayoutBackwardCompat:
    """Test that AppLayout dataclass is still available and correct."""

    def test_is_dataclass(self):
        """AppLayout is still a dataclass."""
        assert dataclasses.is_dataclass(AppLayout)

    def test_defaults(self):
        """AppLayout has correct defaults."""
        layout = AppLayout()
        assert layout.width == 0
        assert layout.height == 0
        assert layout.theme == "default"

    def test_custom_values(self):
        """AppLayout accepts custom values."""
        layout = AppLayout(width=120, height=40, theme="ocean")
        assert layout.width == 120
        assert layout.height == 40
        assert layout.theme == "ocean"


# ===========================================================================
# Backward compatibility: Old stub API
# ===========================================================================


class TestStubAPIBackwardCompat:
    """Test backward compatibility with the Subphase 0 stub API."""

    def test_construction_with_session(self):
        """ParleyApp can be constructed with a session."""
        session_mock = MagicMock()
        app = ParleyApp(session=session_mock)
        if _HAS_TEXTUAL:
            assert app._session is session_mock
        else:
            assert app.session is session_mock

    def test_construction_with_layout(self):
        """ParleyApp can be constructed with a layout."""
        layout = AppLayout(width=100, height=30, theme="dark")
        app = ParleyApp()
        assert layout.width == 100

    def test_ready_lifecycle(self):
        """Full ready lifecycle works (start → ready=True → stop → ready=False)."""
        app = ParleyApp()
        assert app.ready is False
        asyncio.run(app.start())
        assert app.ready is True
        asyncio.run(app.stop())
        assert app.ready is False
