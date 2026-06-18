"""CLI entry point for τ-coding-agent.

Launches the Parley TUI with tau-agent-core backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CLIArgs:
    """CLI argument types for the τ coding agent."""

    model: str | None = None
    provider: str | None = None
    session_name: str | None = None
    output: str = "tui"
    verbose: bool = False
    config_file: str | None = None
    cwd: str | None = None
    context_window: int | None = None
    max_tokens: int | None = None

    @property
    def is_verbose(self) -> bool:
        return self.verbose

    @property
    def is_json_output(self) -> bool:
        return self.output == "json"


def parse_cli_args(argv: list[str] | None = None) -> CLIArgs:
    """Parse command-line arguments into CLIArgs."""
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

    Launches the Parley TUI which manages its own config and backend.
    """
    import sys
    from pathlib import Path

    args = parse_cli_args()

    if args.verbose:
        print(f"τ-coding-agent starting with args: {args}")

    # Launch the Parley TUI
    from tau_coding_agent.app import Parley

    app = Parley()

    if args.verbose:
        print(f"τ-coding-agent starting Parley")

    app.run()


if __name__ == "__main__":
    main()
