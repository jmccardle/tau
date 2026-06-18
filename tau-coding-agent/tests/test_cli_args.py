"""Tests for CLI argument types.

Reference: PHASE-4-SUBPHASE-0.md — CLI argument types
Reference: SUBPHASE-0.0.md — "CLI argument contract" section
"""

import dataclasses

import pytest

from tau_coding_agent.cli import CLIArgs, SessionConfig, parse_cli_args


class TestCLIArgsImport:
    """Test that CLI argument types are importable."""

    def test_cli_args_is_importable(self):
        """CLIArgs must be importable from cli."""
        from tau_coding_agent.cli import CLIArgs as C
        assert C is not None

    def test_session_config_is_importable(self):
        """SessionConfig must be importable from cli."""
        from tau_coding_agent.cli import SessionConfig as S
        assert S is not None

    def test_parse_cli_args_is_importable(self):
        """parse_cli_args must be importable from cli."""
        from tau_coding_agent.cli import parse_cli_args as p
        assert callable(p)


class TestCLIArgs:
    """Tests for CLIArgs dataclass."""

    def test_is_dataclass(self):
        """CLIArgs is a dataclass."""
        import dataclasses
        assert dataclasses.is_dataclass(CLIArgs)

    def test_has_model_field(self):
        """CLIArgs has model field."""
        assert "model" in {f.name for f in dataclasses.fields(CLIArgs)}

    def test_has_provider_field(self):
        """CLIArgs has provider field."""
        assert "provider" in {f.name for f in dataclasses.fields(CLIArgs)}

    def test_has_session_name_field(self):
        """CLIArgs has session_name field."""
        assert "session_name" in {f.name for f in dataclasses.fields(CLIArgs)}

    def test_has_output_field(self):
        """CLIArgs has output field."""
        assert "output" in {f.name for f in dataclasses.fields(CLIArgs)}

    def test_has_verbose_field(self):
        """CLIArgs has verbose field."""
        assert "verbose" in {f.name for f in dataclasses.fields(CLIArgs)}

    def test_has_config_file_field(self):
        """CLIArgs has config_file field."""
        assert "config_file" in {f.name for f in dataclasses.fields(CLIArgs)}

    def test_has_cwd_field(self):
        """CLIArgs has cwd field."""
        assert "cwd" in {f.name for f in dataclasses.fields(CLIArgs)}

    def test_has_context_window_field(self):
        """CLIArgs has context_window field."""
        assert "context_window" in {f.name for f in dataclasses.fields(CLIArgs)}

    def test_has_max_tokens_field(self):
        """CLIArgs has max_tokens field."""
        assert "max_tokens" in {f.name for f in dataclasses.fields(CLIArgs)}

    def test_defaults(self):
        """CLIArgs has correct defaults."""
        args = CLIArgs()
        assert args.model is None
        assert args.provider is None
        assert args.session_name is None
        assert args.output == "text"
        assert args.verbose is False
        assert args.config_file is None
        assert args.cwd is None
        assert args.context_window is None
        assert args.max_tokens is None

    def test_full_construction(self):
        """CLIArgs accepts all fields."""
        args = CLIArgs(
            model="gpt-4",
            provider="openai",
            session_name="test",
            output="json",
            verbose=True,
            config_file="/tmp/config.yaml",
            cwd="/tmp",
            context_window=128000,
            max_tokens=4096,
        )
        assert args.model == "gpt-4"
        assert args.provider == "openai"
        assert args.session_name == "test"
        assert args.output == "json"
        assert args.verbose is True
        assert args.config_file == "/tmp/config.yaml"
        assert args.cwd == "/tmp"
        assert args.context_window == 128000
        assert args.max_tokens == 4096


class TestCLIArgsProperties:
    """Tests for CLIArgs computed properties."""

    def test_is_verbose_true(self):
        """is_verbose returns True when verbose is set."""
        args = CLIArgs(verbose=True)
        assert args.is_verbose is True

    def test_is_verbose_false(self):
        """is_verbose returns False by default."""
        args = CLIArgs()
        assert args.is_verbose is False

    def test_is_json_output_true(self):
        """is_json_output returns True when output='json'."""
        args = CLIArgs(output="json")
        assert args.is_json_output is True

    def test_is_json_output_false(self):
        """is_json_output returns False when output='text'."""
        args = CLIArgs(output="text")
        assert args.is_json_output is False


class TestParseCliArgs:
    """Tests for parse_cli_args function."""

    def test_empty_args(self):
        """parse_cli_args with empty list returns defaults."""
        args = parse_cli_args([])
        assert args.model is None
        assert args.output == "text"
        assert args.verbose is False

    def test_model_short_flag(self):
        """-m flag sets model."""
        args = parse_cli_args(["-m", "gpt-4"])
        assert args.model == "gpt-4"

    def test_model_long_flag(self):
        """--model flag sets model."""
        args = parse_cli_args(["--model", "claude-3"])
        assert args.model == "claude-3"

    def test_provider_short_flag(self):
        """-p flag sets provider."""
        args = parse_cli_args(["-p", "openai"])
        assert args.provider == "openai"

    def test_session_short_flag(self):
        """-s flag sets session_name."""
        args = parse_cli_args(["-s", "my-session"])
        assert args.session_name == "my-session"

    def test_output_short_flag(self):
        """-o flag sets output."""
        args = parse_cli_args(["-o", "json"])
        assert args.output == "json"

    def test_verbose_flag(self):
        """-v flag enables verbose."""
        args = parse_cli_args(["-v"])
        assert args.verbose is True

    def test_verbose_long_flag(self):
        """--verbose flag enables verbose."""
        args = parse_cli_args(["--verbose"])
        assert args.verbose is True

    def test_config_flag(self):
        """--config flag sets config_file."""
        args = parse_cli_args(["--config", "/path/to/config.yaml"])
        assert args.config_file == "/path/to/config.yaml"

    def test_cwd_flag(self):
        """--cwd flag sets cwd."""
        args = parse_cli_args(["--cwd", "/tmp"])
        assert args.cwd == "/tmp"

    def test_context_window_flag(self):
        """--context-window flag sets context_window."""
        args = parse_cli_args(["--context-window", "128000"])
        assert args.context_window == 128000

    def test_max_tokens_flag(self):
        """--max-tokens flag sets max_tokens."""
        args = parse_cli_args(["--max-tokens", "4096"])
        assert args.max_tokens == 4096

    def test_multiple_flags(self):
        """Multiple flags work together."""
        args = parse_cli_args([
            "-m", "gpt-4",
            "-p", "openai",
            "-v",
            "-o", "json",
            "-s", "test",
        ])
        assert args.model == "gpt-4"
        assert args.provider == "openai"
        assert args.verbose is True
        assert args.output == "json"
        assert args.session_name == "test"

    def test_unknown_flag_ignored(self):
        """Unknown flags are ignored."""
        args = parse_cli_args(["--unknown-flag", "value", "-m", "gpt-4"])
        assert args.model == "gpt-4"
        # Unknown flags should not raise


class TestSessionConfig:
    """Tests for SessionConfig dataclass."""

    def test_is_dataclass(self):
        """SessionConfig is a dataclass."""
        import dataclasses
        assert dataclasses.is_dataclass(SessionConfig)

    def test_model_is_required(self):
        """SessionConfig requires model."""
        assert "model" in {f.name for f in dataclasses.fields(SessionConfig)}

    def test_provider_is_required(self):
        """SessionConfig requires provider."""
        assert "provider" in {f.name for f in dataclasses.fields(SessionConfig)}

    def test_defaults(self):
        """SessionConfig has correct defaults."""
        config = SessionConfig(model="gpt-4", provider="openai")
        assert config.model == "gpt-4"
        assert config.provider == "openai"
        assert config.session_name is None
        assert config.system_prompt is None
        assert config.cwd is None
        assert config.context_window is None
        assert config.max_tokens is None
        assert config.tools is None

    def test_full_construction(self):
        """SessionConfig accepts all fields."""
        config = SessionConfig(
            model="gpt-4-turbo",
            provider="openai",
            session_name="project-alpha",
            system_prompt="You are a helpful assistant.",
            cwd="/tmp",
            context_window=128000,
            max_tokens=4096,
            tools=["bash", "read", "write"],
        )
        assert config.model == "gpt-4-turbo"
        assert config.provider == "openai"
        assert config.session_name == "project-alpha"
        assert config.system_prompt == "You are a helpful assistant."
        assert config.cwd == "/tmp"
        assert config.context_window == 128000
        assert config.max_tokens == 4096
        assert config.tools == ["bash", "read", "write"]
