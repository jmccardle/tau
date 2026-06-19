"""CLI entry point for τ-coding-agent.

Parses arguments (argparse, pi-aligned flags), then either runs a headless
``--print`` turn (see :mod:`tau_coding_agent.headless`) or launches the Parley
TUI. Model/provider/tool flags override ``~/.tau/config.json`` per-invocation in
both paths.

Flag set and pi citations live in docs/CLI-PLAN.md. Short-alias divergences from
pi are intentional and documented there; notably ``-v``/``--version`` matches pi
(τ's old ``-v``=verbose is dropped; ``--verbose`` is long-only now).

Reference: docs/CLI-PLAN.md (Core flag set).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tau_coding_agent.headless import CLIError, resolve_model_config, run_print

# τ data dir / config (matches app.py's TAU_DIR).
TAU_DIR = Path.home() / ".tau"


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("tau-coding-agent")
    except Exception:
        return "0.0.0"


@dataclass
class CLIArgs:
    """Parsed τ CLI arguments.

    Kept as a typed dataclass (rather than a bare argparse Namespace) for clean
    attribute access and so callers/tests can construct defaults directly.
    """

    messages: list[str] = field(default_factory=list)
    print_mode: bool = False
    mode: str = "text"  # text | json
    model: str | None = None
    provider: str | None = None
    tools: str | None = None  # comma-separated allowlist
    no_tools: bool = False
    system_prompt: str | None = None
    verbose: bool = False

    @property
    def is_verbose(self) -> bool:
        return self.verbose

    @property
    def is_json_output(self) -> bool:
        return self.mode == "json"


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the τ CLI (Core flag set)."""
    parser = argparse.ArgumentParser(
        prog="tau",
        description="τ — programmable coding agent (TUI + headless CLI).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  tau                         # interactive TUI (default model)\n"
            "  tau --model gpt-4o          # TUI with a specific model\n"
            '  tau -p "explain @main.py"   # headless: print the answer and exit\n'
            '  tau -p --mode json "hi"     # headless, JSONL event stream\n'
            "\n"
            "deferred (see docs/CLI-PLAN.md): --thinking (needs τ-ai reasoning\n"
            "send-path), --continue/--resume/--session (needs session wiring)."
        ),
    )
    parser.add_argument("--version", "-v", action="version", version=f"tau {_version()}")
    parser.add_argument(
        "messages",
        nargs="*",
        help="prompt text and/or @file references (used with --print)",
    )
    parser.add_argument(
        "--print", "-p", dest="print_mode", action="store_true",
        help="run one turn headlessly, print the result, and exit",
    )
    parser.add_argument(
        "--mode", choices=["text", "json"], default="text",
        help="headless output format: text transcript (default) or JSONL events",
    )
    parser.add_argument(
        "--model", "-m", default=None,
        help="model name from ~/.tau/config.json, or provider/id shorthand",
    )
    parser.add_argument(
        "--provider", default=None,
        help="provider/backend override (long-only, matching pi)",
    )
    parser.add_argument(
        "--tools", "-t", default=None,
        help="comma-separated tool allowlist (e.g. read,bash)",
    )
    parser.add_argument(
        "--no-tools", "-nt", dest="no_tools", action="store_true",
        help="disable all tools (read-only agent)",
    )
    parser.add_argument(
        "--system-prompt", dest="system_prompt", default=None,
        help="override the system prompt for this run",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="verbose logging (long-only; pi-aligned, -v is --version)",
    )
    return parser


def parse_cli_args(argv: list[str] | None = None) -> CLIArgs:
    """Parse argv into :class:`CLIArgs`."""
    parser = build_parser()
    ns = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return CLIArgs(
        messages=list(ns.messages),
        print_mode=ns.print_mode,
        mode=ns.mode,
        model=ns.model,
        provider=ns.provider,
        tools=ns.tools,
        no_tools=ns.no_tools,
        system_prompt=ns.system_prompt,
        verbose=ns.verbose,
    )


def load_config() -> dict:
    """Load ``~/.tau/config.json`` (or an empty config if absent)."""
    config_path = TAU_DIR / "config.json"
    if not config_path.exists():
        return {}
    loaded = json.loads(config_path.read_text())
    if not isinstance(loaded, dict):
        raise CLIError(f"{config_path} must contain a JSON object")
    return loaded


def _launch_tui(args: CLIArgs, config: dict) -> int:
    """Launch the Parley TUI, applying model/system-prompt overrides."""
    overrides: dict = {}
    if args.model or args.provider or args.tools or args.no_tools:
        name, model_config = resolve_model_config(config, args)
        # Merge over any existing entry so config-derived keys (api_key, etc.)
        # survive when only some fields are overridden.
        existing = config.get("models", {}).get(name, {})
        overrides["models"] = {name: {**existing, **model_config}}
        overrides["default_model"] = name
    if args.system_prompt is not None:
        overrides["system_prompt"] = args.system_prompt

    from tau_coding_agent.app import Parley

    app = Parley(cli_overrides=overrides or None)
    app.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``tau`` console script."""
    args = parse_cli_args(argv)

    if args.verbose:
        print(f"τ-coding-agent args: {args}", file=sys.stderr)

    try:
        config = load_config()
        # Headless print mode is opt-in via -p/--print. Messages without --print
        # are a usage error (Fail-Early: don't silently ignore them, and don't
        # quietly drop into the TUI discarding the prompt).
        if args.print_mode:
            return asyncio.run(run_print(args, config))
        if args.messages:
            raise CLIError(
                "messages were given without --print; add -p to run headlessly "
                "(e.g. tau -p \"...\"), or omit the message to start the TUI"
            )
        if args.mode == "json":
            raise CLIError("--mode json only applies to headless --print runs")
        return _launch_tui(args, config)
    except CLIError as exc:
        print(f"tau: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
