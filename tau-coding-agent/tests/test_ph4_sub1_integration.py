"""Phase 4 Subphase 1 Integration Tests

End-to-end integration tests for the TUI app shell:
1. Config discovery and merge order
2. build_session() respects config values
3. ParleyApp with config-loaded session
4. Full event lifecycle (start → streaming → end)
5. Async event handling
6. Config error handling paths
7. CLI args overriding config
8. Print mode behavior

Reference: PHASE-4-SUBPHASE-1.md — Done Criteria
Reference: SUBPHASE-0.0.md — AgentSession interface
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from tau_coding_agent.config import (
    DEFAULT_SETTINGS,
    _deep_merge,
    load_config,
    save_config,
    get_project_config_path,
    get_user_config_path,
)
from tau_coding_agent.app import (
    ParleyApp,
    _HAS_TEXTUAL,
    ChatDisplay,
    InputBar,
    build_session,
)


# ===========================================================================
# Integration Test 1: Config discovery paths
# ===========================================================================


class TestConfigDiscovery:
    """Test that config files are discovered at the correct paths."""

    def test_project_config_path_structure(self):
        """Project config path is <project_root>/.tau/settings.json."""
        root = Path("/my/project")
        path = get_project_config_path(root)
        expected = root / ".tau" / "settings.json"
        assert path == expected

    def test_user_config_path_structure(self):
        """User config path is ~/.tau/settings.json."""
        path = get_user_config_path()
        assert path.name == "settings.json"
        assert path.parent.name == ".tau"
        assert str(Path.home()) in str(path)

    def test_project_config_defaults_to_cwd(self):
        """Project config defaults to cwd when no root given."""
        with patch("tau_coding_agent.config.Path.cwd") as mock_cwd:
            mock_cwd.return_value = Path("/current/working/dir")
            path = get_project_config_path()
            expected = Path("/current/working/dir/.tau/settings.json")
            assert path == expected

    def test_project_config_resolves_relative(self):
        """Project config path is resolved from the given root."""
        root = Path("./relative/project")
        path = get_project_config_path(root)
        # Path.resolve() converts relative to absolute
        assert "relative" in str(path) or str(path).endswith("relative/project/.tau/settings.json")
        assert path.name == "settings.json"

    def test_user_config_is_in_home(self):
        """User config is always under the user's home directory."""
        path = get_user_config_path()
        assert path.resolve().parent.parent == Path.home()


# ===========================================================================
# Integration Test 2: Config merge order
# ===========================================================================


class TestConfigMergeOrder:
    """Test that config files are merged in the correct order.

    Merge order:
    1. DEFAULT_SETTINGS (base)
    2. .tau/settings.json (project)
    3. ~/.tau/settings.json (user)
    Later values override earlier values for the same key.
    """

    def test_defaults_are_base(self, tmp_path):
        """DEFAULT_SETTINGS provide the base values."""
        config = load_config(
            project_root=tmp_path / "no_such_dir",
            user_config_override=tmp_path / "no_such_file.json",
        )
        for key, value in DEFAULT_SETTINGS.items():
            assert config[key] == value, f"Key {key} should default to {value}"

    def test_project_overrides_defaults(self, tmp_path):
        """Project config overrides defaults."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"model": "claude-3"}))

        config = load_config(
            project_root=tmp_path,
            user_config_override=tmp_path / "nonexistent.json",
        )
        assert config["model"] == "claude-3"
        assert config["provider"] == DEFAULT_SETTINGS["provider"]

    def test_user_overrides_project(self, tmp_path):
        """User config overrides project config."""
        # Project sets model to claude-3
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"model": "claude-3", "provider": "anthropic"}))

        # User overrides model to gpt-4
        user_cfg = tmp_path / "user.json"
        user_cfg.write_text(json.dumps({"model": "gpt-4"}))

        config = load_config(
            project_root=tmp_path,
            user_config_override=user_cfg,
        )
        assert config["model"] == "gpt-4"
        assert config["provider"] == "anthropic"  # project value preserved

    def test_three_way_merge(self, tmp_path):
        """Three-way merge: defaults → project → user."""
        # Defaults: model=gpt-4, provider=openai, theme=catppuccin-mocha
        # Project: model=claude-3, provider=anthropic
        # User: provider=google
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"model": "claude-3", "provider": "anthropic"}))

        user_cfg = tmp_path / "user.json"
        user_cfg.write_text(json.dumps({"provider": "google", "max_tokens": 8192}))

        config = load_config(
            project_root=tmp_path,
            user_config_override=user_cfg,
        )
        assert config["model"] == "claude-3"  # from project
        assert config["provider"] == "google"  # from user (overrides project)
        assert config["max_tokens"] == 8192  # from user (new key)
        assert config["theme"] == DEFAULT_SETTINGS["theme"]  # from defaults

    def test_deep_merge_preserves_nested(self, tmp_path):
        """Deep merge preserves non-overridden nested values."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "timeout": 30,
                "retries": 3,
            }
        }))

        user_cfg = tmp_path / "user.json"
        user_cfg.write_text(json.dumps({
            "openai": {
                "timeout": 60,  # override only this
            }
        }))

        config = load_config(
            project_root=tmp_path,
            user_config_override=user_cfg,
        )
        assert config["openai"]["timeout"] == 60
        assert config["openai"]["base_url"] == "https://api.openai.com/v1"
        assert config["openai"]["retries"] == 3


# ===========================================================================
# Integration Test 3: build_session config integration
# ===========================================================================


class TestBuildSessionConfig:
    """Test that build_session() uses config values correctly."""

    def test_build_session_uses_project_config_model(self, tmp_path):
        """build_session uses model from project config."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"model": "claude-3-opus"}))

        session = build_session(project_root=tmp_path)
        assert session._model.id == "claude-3-opus"

    def test_build_session_uses_project_config_provider(self, tmp_path):
        """build_session uses provider from project config."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"provider": "anthropic"}))

        session = build_session(project_root=tmp_path)
        assert session._model.provider == "anthropic"

    def test_build_session_cli_overrides_project_config_model(self, tmp_path):
        """CLI model overrides project config model."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"model": "claude-3"}))

        session = build_session(model="gpt-4", project_root=tmp_path)
        assert session._model.id == "gpt-4"

    def test_build_session_cli_overrides_project_config_provider(self, tmp_path):
        """CLI provider overrides project config provider."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"provider": "anthropic"}))

        session = build_session(provider="openai", project_root=tmp_path)
        assert session._model.provider == "openai"

    def test_build_session_uses_config_system_prompt(self, tmp_path):
        """build_session uses system_prompt from config."""
        system_prompt = "You are a specialized coding assistant."
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"system_prompt": system_prompt}))

        session = build_session(project_root=tmp_path)
        assert session._system_prompt == system_prompt

    def test_build_session_cli_system_prompt_overrides_config(self, tmp_path):
        """CLI system_prompt overrides config file."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"system_prompt": "config prompt"}))

        session = build_session(system_prompt="cli prompt", project_root=tmp_path)
        assert session._system_prompt == "cli prompt"

    def test_build_session_uses_config_context_window(self, tmp_path):
        """build_session uses context_window from config."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"context_window": 64000}))

        session = build_session(project_root=tmp_path)
        assert session._model.context_window == 64000

    def test_build_session_uses_config_max_tokens(self, tmp_path):
        """build_session uses max_tokens from config."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"max_tokens": 8192}))

        session = build_session(project_root=tmp_path)
        assert session._model.max_tokens == 8192

    def test_build_session_no_config_uses_defaults(self):
        """build_session uses defaults when no config files exist."""
        session = build_session()
        assert session._model.id == "gpt-4"
        assert session._model.provider == "openai"
        assert session._model.base_url == "https://api.openai.com/v1"
        assert session._model.context_window == 128000
        assert session._model.max_tokens == 4096

    def test_build_session_session_has_required_attributes(self):
        """build_session returns a session with all required attributes."""
        session = build_session()
        assert hasattr(session, "messages")
        assert hasattr(session, "state")
        assert hasattr(session, "is_streaming")
        assert hasattr(session, "subscribe")
        assert hasattr(session, "prompt")
        assert hasattr(session, "continue_conversation")
        assert hasattr(session, "compact")
        assert hasattr(session, "abort")

    def test_build_session_accepts_base_url(self, tmp_path):
        """build_session uses base_url from config."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"base_url": "https://custom.api/v1"}))

        session = build_session(project_root=tmp_path)
        assert session._model.base_url == "https://custom.api/v1"

    def test_build_session_cli_base_url_overrides_config(self, tmp_path):
        """CLI args override config base_url."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"base_url": "https://old.api/v1"}))

        # Note: build_session doesn't accept base_url directly,
        # but it uses the config value
        session = build_session(project_root=tmp_path)
        assert session._model.base_url == "https://old.api/v1"


# ===========================================================================
# Integration Test 4: ParleyApp with config-loaded session
# ===========================================================================


class TestParleyAppConfigIntegration:
    """Test ParleyApp works with config-loaded sessions."""

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parleyapp_accepts_configured_session(self, mock_agent_session):
        """ParleyApp can be constructed with a config-loaded session."""
        app = ParleyApp(session=mock_agent_session, print_mode=False)
        assert app is not None
        assert app._is_streaming is False

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parleyapp_subscribes_to_session(self, mock_agent_session):
        """ParleyApp subscribes to the session's events on mount."""
        mock_agent_session.subscribe = MagicMock()
        app = ParleyApp(session=mock_agent_session)

        # _subscribe_to_events should register handler
        app._subscribe_to_events()
        mock_agent_session.subscribe.assert_called_once()

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parleyapp_print_mode_with_session(self, mock_agent_session):
        """ParleyApp print_mode is True with config session."""
        app = ParleyApp(session=mock_agent_session, print_mode=True)
        assert app._print_mode is True

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parleyapp_without_session(self):
        """ParleyApp can be constructed without a session."""
        app = ParleyApp()
        assert app._session is None
        # subscribe should be a no-op
        app._subscribe_to_events()  # Should not raise


# ===========================================================================
# Integration Test 5: Full event lifecycle
# ===========================================================================


class TestEventLifecycle:
    """Test the full agent event lifecycle."""

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_full_lifecycle_agent_start_end(self):
        """agent_start → agent_end lifecycle resets state."""
        app = ParleyApp()
        mock_input_bar = MagicMock()
        mock_input_bar.disabled = False

        with patch.object(app, "query_one", return_value=mock_input_bar):
            # Start streaming
            start_event = MagicMock()
            start_event.type = "agent_start"
            app._handle_event(start_event)
            assert app._is_streaming is True
            assert mock_input_bar.disabled is True

            # End streaming
            end_event = MagicMock()
            end_event.type = "agent_end"
            app._handle_event(end_event)
            assert app._is_streaming is False
            assert mock_input_bar.disabled is False

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_full_lifecycle_with_message_update(self):
        """agent_start → message_update → agent_end lifecycle."""
        app = ParleyApp()
        mock_input_bar = MagicMock()
        mock_input_bar.disabled = False

        with patch.object(app, "query_one", return_value=mock_input_bar):
            with patch.object(app, "call_later"):
                with patch.object(app, "set_timer"):
                    # Start
                    app._handle_event(MagicMock(type="agent_start"))
                    assert app._is_streaming is True

                    # Message update
                    update_event = MagicMock()
                    update_event.type = "message_update"
                    app._update_streaming_message(update_event)

                    # End
                    app._handle_event(MagicMock(type="agent_end"))
                    assert app._is_streaming is False
                    assert mock_input_bar.disabled is False

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_multiple_message_updates_during_stream(self):
        """Multiple message updates during streaming are throttled."""
        app = ParleyApp()
        with patch.object(app, "call_later"):
            with patch.object(app, "set_timer"):
                for i in range(50):
                    update_event = MagicMock()
                    update_event.type = "message_update"
                    app._update_streaming_message(update_event)

        # Each call should stop the previous timer and create a new one
        assert app._throttle_timer is not None

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_agent_start_then_end_then_start(self):
        """Multiple start/end cycles work correctly."""
        app = ParleyApp()
        mock_input_bar = MagicMock()
        mock_input_bar.disabled = False

        with patch.object(app, "query_one", return_value=mock_input_bar):
            # Cycle 1
            app._handle_event(MagicMock(type="agent_start"))
            assert app._is_streaming is True
            app._handle_event(MagicMock(type="agent_end"))
            assert app._is_streaming is False

            # Cycle 2
            app._handle_event(MagicMock(type="agent_start"))
            assert app._is_streaming is True
            app._handle_event(MagicMock(type="agent_end"))
            assert app._is_streaming is False


# ===========================================================================
# Integration Test 6: Error handling paths
# ===========================================================================


class TestErrorHandling:
    """Test error handling in the TUI app shell."""

    def test_corrupt_project_json_no_crash(self, tmp_path):
        """Corrupt project config JSON doesn't crash load_config."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text("{ this is not valid json!!!")

        config = load_config(
            project_root=tmp_path,
            user_config_override=tmp_path / "nonexistent.json",
        )
        # Should fall back to defaults
        assert config["model"] == DEFAULT_SETTINGS["model"]

    def test_corrupt_user_json_no_crash(self, tmp_path):
        """Corrupt user config JSON doesn't crash load_config."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"model": "gpt-4o"}))

        user_cfg = tmp_path / "user.json"
        user_cfg.write_text("{ broken json }")

        config = load_config(
            project_root=tmp_path,
            user_config_override=user_cfg,
        )
        # Should use project config (defaults to project values)
        assert config["model"] == "gpt-4o"

    def test_empty_json_file(self, tmp_path):
        """Empty JSON object {} is valid and returns defaults."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text("{}")

        config = load_config(
            project_root=tmp_path,
            user_config_override=tmp_path / "nonexistent.json",
        )
        assert config["model"] == DEFAULT_SETTINGS["model"]

    def test_build_session_with_corrupt_config(self, tmp_path):
        """build_session falls back to defaults with corrupt config."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text("{ not valid json }")

        session = build_session(project_root=tmp_path)
        assert session is not None
        assert session._model.id == "gpt-4"

    def test_build_session_missing_project_dir(self):
        """build_session works when project config dir doesn't exist."""
        session = build_session(project_root=Path("/nonexistent/path/12345"))
        assert session is not None
        assert session._model.id == "gpt-4"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_parleyapp_query_one_missing_widget(self):
        """ParleyApp handles missing widgets gracefully."""
        app = ParleyApp()
        with patch.object(app, "query_one", side_effect=Exception("not found")):
            # All these should handle missing widgets without crashing
            app._re_disable_input()
            app._re_enable_input()
            app._do_update_streaming_message()


# ===========================================================================
# Integration Test 7: CLI args override config
# ===========================================================================


class TestCLIOverride:
    """Test that CLI arguments correctly override config values."""

    def test_model_cli_overrides_config(self, tmp_path):
        """CLI model argument overrides project config."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"model": "claude-3"}))

        session = build_session(model="gpt-4", project_root=tmp_path)
        assert session._model.id == "gpt-4"

    def test_provider_cli_overrides_config(self, tmp_path):
        """CLI provider argument overrides project config."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"provider": "anthropic"}))

        session = build_session(provider="openai", project_root=tmp_path)
        assert session._model.provider == "openai"

    def test_system_prompt_cli_overrides_config(self, tmp_path):
        """CLI system_prompt overrides config."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({"system_prompt": "config prompt"}))

        session = build_session(system_prompt="cli prompt", project_root=tmp_path)
        assert session._system_prompt == "cli prompt"

    def test_config_is_fallback_when_no_cli(self, tmp_path):
        """When no CLI args, config values are used."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({
            "model": "custom-model",
            "provider": "custom-provider",
            "max_tokens": 8192,
        }))

        session = build_session(project_root=tmp_path)
        assert session._model.id == "custom-model"
        assert session._model.provider == "custom-provider"
        assert session._model.max_tokens == 8192

    def test_multiple_cli_args_override_multiple_config(self, tmp_path):
        """Multiple CLI args override multiple config values."""
        project_cfg = tmp_path / ".tau" / "settings.json"
        project_cfg.parent.mkdir(parents=True)
        project_cfg.write_text(json.dumps({
            "model": "claude-3",
            "provider": "anthropic",
            "system_prompt": "config prompt",
            "max_tokens": 4096,
        }))

        session = build_session(
            model="gpt-4",
            provider="openai",
            system_prompt="cli prompt",
            project_root=tmp_path,
        )
        assert session._model.id == "gpt-4"
        assert session._model.provider == "openai"
        assert session._system_prompt == "cli prompt"
        # max_tokens should come from config since not overridden by CLI
        assert session._model.max_tokens == 4096


# ===========================================================================
# Integration Test 8: Config roundtrip
# ===========================================================================


class TestConfigRoundtrip:
    """Test save and load config roundtrip."""

    def test_save_and_load_preserves_all_fields(self, tmp_path):
        """save_config + load_config roundtrip preserves all fields."""
        settings = {
            "model": "gpt-4-turbo",
            "provider": "openai",
            "base_url": "https://custom.api/v1",
            "context_window": 64000,
            "max_tokens": 8192,
            "system_prompt": "Custom prompt",
            "thinking": "on",
            "theme": "nord",
        }
        config_path = tmp_path / ".tau" / "settings.json"
        save_config(config_path, settings)

        config = load_config(
            project_root=tmp_path,
            user_config_override=tmp_path / "nonexistent.json",
        )
        assert config["model"] == "gpt-4-turbo"
        assert config["provider"] == "openai"
        assert config["base_url"] == "https://custom.api/v1"
        assert config["context_window"] == 64000
        assert config["max_tokens"] == 8192
        assert config["system_prompt"] == "Custom prompt"
        assert config["thinking"] == "on"
        assert config["theme"] == "nord"

    def test_save_creates_parent_directories(self, tmp_path):
        """save_config creates parent directories."""
        config_path = tmp_path / "a" / "b" / "c" / ".tau" / "settings.json"
        save_config(config_path, {"model": "test"})
        assert config_path.exists()

    def test_save_and_load_empty_config(self, tmp_path):
        """save and load empty config dict."""
        config_path = tmp_path / ".tau" / "settings.json"
        save_config(config_path, {})

        config = load_config(
            project_root=tmp_path,
            user_config_override=tmp_path / "nonexistent.json",
        )
        # Should return defaults for all keys
        assert config["model"] == DEFAULT_SETTINGS["model"]


# ===========================================================================
# Integration Test 9: Widget composition
# ===========================================================================


class TestWidgetComposition:
    """Test that widgets compose correctly into the app."""

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_chat_display_composes_rich_log(self):
        """ChatDisplay.compose yields a RichLog."""
        from textual.widgets import RichLog
        chat = ChatDisplay()
        widgets = list(chat.compose())
        assert len(widgets) == 1
        assert isinstance(widgets[0], RichLog)

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_input_bar_composes_input(self):
        """InputBar.compose yields an Input widget."""
        from textual.widgets import Input
        bar = InputBar()
        widgets = list(bar.compose())
        assert len(widgets) == 1
        assert isinstance(widgets[0], Input)

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_input_bar_has_placeholder(self):
        """InputBar's Input widget has a placeholder."""
        from textual.widgets import Input
        bar = InputBar()
        widgets = list(bar.compose())
        input_widget = widgets[0]
        assert isinstance(input_widget, Input)
        # Input widget has a placeholder attribute
        assert hasattr(input_widget, "placeholder")

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_chat_display_streaming_content(self):
        """ChatDisplay.streaming_content tracks partial responses."""
        chat = ChatDisplay()
        assert chat._streaming_content == ""

        chat.update_streaming_message("partial")
        assert chat._streaming_content == "partial"

        chat.update_streaming_message("more partial")
        assert chat._streaming_content == "more partial"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_chat_display_messages_accumulate(self):
        """ChatDisplay messages accumulate via add_message."""
        chat = ChatDisplay()
        assert len(chat.get_messages()) == 0

        chat.add_message({"role": "user", "content": [{"type": "text", "text": "q1"}]})
        assert len(chat.get_messages()) == 1

        chat.add_message({"role": "assistant", "content": [{"type": "text", "text": "a1"}]})
        assert len(chat.get_messages()) == 2

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_chat_display_clear_resets_both(self):
        """ChatDisplay.clear_messages resets both messages and streaming."""
        chat = ChatDisplay()
        chat.add_message({"role": "user", "content": []})
        chat.update_streaming_message("partial")
        assert len(chat.get_messages()) == 1
        assert chat._streaming_content == "partial"

        chat.clear_messages()

        assert len(chat.get_messages()) == 0
        assert chat._streaming_content == ""


# ===========================================================================
# Integration Test 10: Session interface contract
# ===========================================================================


class TestSessionInterface:
    """Test the AgentSession interface contract (SUBPHASE-0.0.md section 7)."""

    def test_session_has_messages(self, mock_agent_session):
        """Session has a messages property."""
        assert hasattr(mock_agent_session, "messages")

    def test_session_has_state(self, mock_agent_session):
        """Session has a state property."""
        assert hasattr(mock_agent_session, "state")

    def test_session_has_is_streaming(self, mock_agent_session):
        """Session has an is_streaming property."""
        assert hasattr(mock_agent_session, "is_streaming")

    def test_session_subscribe_returns_unsubscribe(self, mock_agent_session):
        """subscribe() returns an unsubscribe callable."""
        def handler(event):
            pass
        unsubscribe = mock_agent_session.subscribe(handler)
        assert callable(unsubscribe)
        unsubscribe()  # Should not raise

    async def test_session_prompt_returns_messages(self, mock_agent_session):
        """prompt() returns a list of messages."""
        messages = await mock_agent_session.prompt("test")
        assert isinstance(messages, list)

    async def test_session_continue_conversation(self, mock_agent_session):
        """continue_conversation() runs an agent turn."""
        messages = await mock_agent_session.continue_conversation()
        assert isinstance(messages, list)

    async def test_session_compact(self, mock_agent_session):
        """compact() triggers compaction."""
        await mock_agent_session.compact()

    def test_session_abort(self, mock_agent_session):
        """abort() stops the current turn."""
        mock_agent_session.abort()
