"""CLI argument types for τ-coding-agent.

Defines the CLI argument structure used by the `tau` command.
Arguments are validated and passed to ParleyApp / AgentSession.

Reference: PHASE-4-SUBPHASE-0.md — CLI argument types
Reference: SUBPHASE-0.0.md — "CLI argument contract" section
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CLIArgs:
    """CLI argument types for the τ coding agent.

    These arguments are parsed by the CLI and passed to ParleyApp
    to configure the TUI session.

    Attributes:
        model: LLM model to use (overrides config)
        provider: Provider name (overrides config)
        session_name: Name for the current session
        output: Output format ("text" or "json")
        verbose: Enable verbose logging
        config_file: Path to configuration file
        cwd: Working directory for tool execution
        context_window: Override context window size
        max_tokens: Override max output tokens
    """

    model: str | None = None
    provider: str | None = None
    session_name: str | None = None
    output: str = "text"
    verbose: bool = False
    config_file: str | None = None
    cwd: str | None = None
    context_window: int | None = None
    max_tokens: int | None = None

    @property
    def is_verbose(self) -> bool:
        """Whether verbose logging is enabled."""
        return self.verbose

    @property
    def is_json_output(self) -> bool:
        """Whether JSON output is requested."""
        return self.output == "json"


@dataclass
class SessionConfig:
    """Configuration for a TUI session.

    Merged from CLI args, config file, and defaults.

    Attributes:
        model: Model identifier
        provider: Provider name
        session_name: Session name
        system_prompt: System prompt to use
        cwd: Working directory
        context_window: Context window size
        max_tokens: Max output tokens
        tools: List of enabled tool names
    """

    model: str
    provider: str
    session_name: str | None = None
    system_prompt: str | None = None
    cwd: str | None = None
    context_window: int | None = None
    max_tokens: int | None = None
    tools: list[str] | None = None


def parse_cli_args(argv: list[str] | None = None) -> CLIArgs:
    """Parse command-line arguments into CLIArgs.

    Args:
        argv: Argument list (defaults to sys.argv[1:])

    Returns:
        Parsed CLIArgs instance
    """
    import sys

    args = argv if argv is not None else sys.argv[1:]

    result = CLIArgs()
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--model", "-m"):
            i += 1
            if i < len(args):
                result.model = args[i]
        elif arg in ("--provider", "-p"):
            i += 1
            if i < len(args):
                result.provider = args[i]
        elif arg in ("--session", "-s"):
            i += 1
            if i < len(args):
                result.session_name = args[i]
        elif arg in ("--output", "-o"):
            i += 1
            if i < len(args):
                result.output = args[i]
        elif arg in ("--verbose", "-v"):
            result.verbose = True
        elif arg in ("--config",):
            i += 1
            if i < len(args):
                result.config_file = args[i]
        elif arg in ("--cwd",):
            i += 1
            if i < len(args):
                result.cwd = args[i]
        elif arg in ("--context-window",):
            i += 1
            if i < len(args):
                try:
                    result.context_window = int(args[i])
                except ValueError:
                    pass
        elif arg in ("--max-tokens",):
            i += 1
            if i < len(args):
                try:
                    result.max_tokens = int(args[i])
                except ValueError:
                    pass
        i += 1

    return result


def main():
    """Entry point for the `tau` CLI command.

    Parses CLI arguments, builds an AgentSession from config + args,
    and launches the ParleyApp TUI.

    Configuration precedence (highest to lowest):
    1. CLI arguments (--model, --provider, --config, etc.)
    2. ~/.tau/settings.json (user config)
    3. .tau/settings.json (project config)
    4. Built-in DEFAULT_SETTINGS
    """
    import sys
    from pathlib import Path

    args = parse_cli_args()

    if args.verbose:
        print(f"τ-coding-agent starting with args: {args}")

    # Build AgentSession from CLI args, config files, and defaults
    from tau_coding_agent.app import ParleyApp, build_session

    # Build the session with all configuration sources
    project_root = Path(args.cwd) if args.cwd else None
    if args.config_file:
        config_override = Path(args.config_file)
    else:
        config_override = None

    session = build_session(
        model=args.model,
        provider=args.provider,
        session_name=args.session_name,
        cwd=args.cwd,
        system_prompt=getattr(args, 'system_prompt', None),
        project_root=project_root,
        config_override=str(config_override) if config_override else None,
    )

    # Determine print mode (from --print flag or if prompt was given as CLI args)
    print_mode = args.output == "text" and not args.verbose

    # Launch ParleyApp
    app = ParleyApp(session=session, print_mode=print_mode)

    if args.verbose:
        print(f"τ-coding-agent session: {session.state}")

    # Full TUI would run here
    # app.run()


if __name__ == "__main__":
    main()
