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

from tau_ai.models import EXTENDED_THINKING_LEVELS
from tau_coding_agent.headless import (
    CLIError,
    parse_ext_config_overrides,
    resolve_model_config,
    run_print,
)

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
    # Extensions + tool-filtering flags (E0/S2; pi args.ts:104-153). Threaded into
    # the headless run config; the loader/registry consumers land in E1 (S3+).
    extensions: list[str] = field(default_factory=list)  # --extension/-e (repeatable path)
    no_extensions: bool = False  # -ne → suppress DISCOVERY only; explicit -e still load
    exclude_tools: str | None = None  # -xt → comma-separated tool denylist
    no_builtin_tools: bool = False  # -nbt → (degenerates to --no-tools until E1)
    no_session: bool = False  # --no-session → ephemeral, unpersisted run
    # Per-extension config overrides (S40): repeatable --ext-config NAME.KEY=VALUE.
    # Applied over ~/.tau/config.json "extensions" per key (CLI > config.json).
    ext_config: list[str] = field(default_factory=list)
    # Headless dialog policy (S48): --ui-defaults METHOD=ANSWER[,METHOD=ANSWER].
    # With no policy a headless extension dialog RAISES; this opts back into an
    # explicit auto-answer (over config.json "ui_defaults", CLI wins). Headless-only.
    ui_defaults: str | None = None
    append_system_prompt: list[str] = field(default_factory=list)  # repeatable
    system_prompt: str | None = None
    thinking: str | None = None  # off|minimal|low|medium|high|xhigh
    # Session continuation (headless): resume/fork a persisted ~/.tau/chats
    # session. continue/resume/session/fork are mutually exclusive (argparse
    # group). `name` sets the session title and may combine with any of them.
    continue_session: bool = False  # --continue/-c → most-recent session
    resume: bool = False  # --resume/-r → interactive picker (TUI-only)
    session: str | None = None  # --session REF → specific session (path|stem)
    fork: str | None = None  # --fork REF → fork a session into a new one
    name: str | None = None  # --name/-n → session display title
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
            "  tau --thinking high         # TUI, request high reasoning effort\n"
            '  tau -p -c "and then?"       # continue the most recent session\n'
            '  tau -p --session 17188 "go" # resume a session by filename stem\n'
            "\n"
            "--resume (interactive picker) is available in the TUI, not headlessly."
        ),
    )
    parser.add_argument("--version", "-v", action="version", version=f"tau {_version()}")
    parser.add_argument(
        "messages",
        nargs="*",
        help="prompt text and/or @file references (used with --print)",
    )
    parser.add_argument(
        "--print",
        "-p",
        dest="print_mode",
        action="store_true",
        help="run one turn headlessly, print the result, and exit",
    )
    parser.add_argument(
        "--mode",
        choices=["text", "json"],
        default="text",
        help="headless output format: text transcript (default) or JSONL events",
    )
    parser.add_argument(
        "--model",
        "-m",
        default=None,
        help="model name from ~/.tau/config.json, or provider/id shorthand",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="provider/backend override (long-only, matching pi)",
    )
    parser.add_argument(
        "--tools",
        "-t",
        default=None,
        help="comma-separated tool allowlist (e.g. read,bash)",
    )
    parser.add_argument(
        "--no-tools",
        "-nt",
        dest="no_tools",
        action="store_true",
        help="disable all tools (read-only agent)",
    )
    # Extensions + tool-filtering + ephemeral-session flags (pi args.ts:104-153).
    parser.add_argument(
        "--extension",
        "-e",
        dest="extensions",
        action="append",
        default=None,
        metavar="PATH",
        help="load an extension from PATH (repeatable)",
    )
    parser.add_argument(
        "--no-extensions",
        "-ne",
        dest="no_extensions",
        action="store_true",
        help="disable extension DISCOVERY (explicit --extension paths still load)",
    )
    parser.add_argument(
        "--exclude-tools",
        "-xt",
        dest="exclude_tools",
        default=None,
        metavar="LIST",
        help="comma-separated tool denylist (e.g. bash,write)",
    )
    parser.add_argument(
        "--no-builtin-tools",
        "-nbt",
        dest="no_builtin_tools",
        action="store_true",
        help="disable built-in tools (currently degenerates to --no-tools; see docs)",
    )
    parser.add_argument(
        "--no-session",
        dest="no_session",
        action="store_true",
        help="run ephemerally without persisting a session to disk",
    )
    parser.add_argument(
        "--ext-config",
        dest="ext_config",
        action="append",
        default=None,
        metavar="NAME.KEY=VALUE",
        help="override a per-extension config value (repeatable; CLI > config.json). "
        "VALUE is JSON-decoded when it parses (e.g. budget.ceiling=5.0), else a string",
    )
    parser.add_argument(
        "--ui-defaults",
        dest="ui_defaults",
        default=None,
        metavar="METHOD=ANSWER,...",
        help="headless dialog auto-answers, else a headless dialog raises "
        "(e.g. confirm=yes,select=first,input=default); over config.json "
        '"ui_defaults". Headless (--print) only',
    )
    parser.add_argument(
        "--append-system-prompt",
        dest="append_system_prompt",
        action="append",
        default=None,
        metavar="TEXT",
        help="append TEXT to the system prompt (repeatable)",
    )
    parser.add_argument(
        "--system-prompt",
        dest="system_prompt",
        default=None,
        help="override the system prompt for this run",
    )
    # Session continuation (headless --print). continue/resume/session/fork are
    # mutually exclusive; --name combines with any of them (or a fresh run).
    sess = parser.add_mutually_exclusive_group()
    sess.add_argument(
        "--continue",
        "-c",
        dest="continue_session",
        action="store_true",
        help="continue the most recent session (use with --print)",
    )
    sess.add_argument(
        "--resume",
        "-r",
        action="store_true",
        help="resume via interactive picker (TUI only; not headless)",
    )
    sess.add_argument(
        "--session",
        default=None,
        metavar="REF",
        help="resume a specific session by path or filename stem",
    )
    sess.add_argument(
        "--fork",
        default=None,
        metavar="REF",
        help="fork a session (path or stem) into a new one and continue it",
    )
    parser.add_argument(
        "--name",
        "-n",
        default=None,
        help="set the session display title",
    )
    parser.add_argument(
        "--thinking",
        default=None,
        choices=list(EXTENDED_THINKING_LEVELS),
        help="reasoning effort: off, minimal, low, medium, high, xhigh "
        "(requires a reasoning-capable model)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
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
        # action="append" yields None when the flag is absent → normalize to [].
        extensions=list(ns.extensions or []),
        no_extensions=ns.no_extensions,
        exclude_tools=ns.exclude_tools,
        no_builtin_tools=ns.no_builtin_tools,
        no_session=ns.no_session,
        ext_config=list(ns.ext_config or []),
        ui_defaults=ns.ui_defaults,
        append_system_prompt=list(ns.append_system_prompt or []),
        system_prompt=ns.system_prompt,
        thinking=ns.thinking,
        continue_session=ns.continue_session,
        resume=ns.resume,
        session=ns.session,
        fork=ns.fork,
        name=ns.name,
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
    if args.model or args.provider or args.tools or args.no_tools or args.thinking:
        name, model_config = resolve_model_config(config, args)
        # Merge over any existing entry so config-derived keys (api_key, etc.)
        # survive when only some fields are overridden.
        existing = config.get("models", {}).get(name, {})
        overrides["models"] = {name: {**existing, **model_config}}
        overrides["default_model"] = name
    if args.system_prompt is not None:
        overrides["system_prompt"] = args.system_prompt

    # Run-level flags apply to EVERY backend the TUI creates, so they ride
    # separately from the per-model ``overrides`` (a model switch must not drop
    # them, and -xt/-nbt/--append-system-prompt don't trigger the override block
    # above): explicit ``-e`` paths + ``-ne`` discovery, plus the tool/prompt
    # flags (E5 §2.2-2.3). Passed even when empty so the app has a definite policy.
    exclude_tools = (
        [t.strip() for t in args.exclude_tools.split(",") if t.strip()]
        if args.exclude_tools
        else []
    )
    # Per-extension config overrides (S40): parse ``--ext-config`` here so a
    # malformed item surfaces as a clean CLI error BEFORE the TUI launches; the app
    # merges them over config.json's ``"extensions"`` at each backend load.
    ext_config_overrides = parse_ext_config_overrides(list(args.ext_config or []))
    run_config = {
        "extensions": list(args.extensions or []),
        "no_extensions": args.no_extensions,
        "exclude_tools": exclude_tools,
        "no_builtin_tools": args.no_builtin_tools,
        "append_system_prompt": list(args.append_system_prompt or []),
        "ext_config": ext_config_overrides,
    }

    from tau_coding_agent.app import Parley

    app = Parley(cli_overrides=overrides or None, cli_run_config=run_config)
    app.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``tau`` console script."""
    args = parse_cli_args(argv)

    if args.verbose:
        print(f"τ-coding-agent args: {args}", file=sys.stderr)

    try:
        # --resume is an interactive picker; it has no headless meaning and the
        # TUI uses the sidebar, so reject it clearly rather than no-op (Fail-Early).
        if args.resume:
            raise CLIError(
                "--resume opens an interactive picker, which isn't available "
                "headlessly; use --continue (most recent) or --session REF, or "
                "pick a session from the TUI sidebar"
            )
        # Session continuation is a headless feature; in the TUI you resume from
        # the sidebar. Requiring --print keeps the flag from silently no-op'ing.
        if (args.continue_session or args.session or args.fork) and not args.print_mode:
            raise CLIError(
                "--continue/--session/--fork require --print (headless); in the "
                "TUI, resume a session from the sidebar"
            )
        config = load_config()
        # Headless print mode is opt-in via -p/--print. Messages without --print
        # are a usage error (Fail-Early: don't silently ignore them, and don't
        # quietly drop into the TUI discarding the prompt).
        if args.print_mode:
            return asyncio.run(run_print(args, config))
        if args.messages:
            raise CLIError(
                "messages were given without --print; add -p to run headlessly "
                '(e.g. tau -p "..."), or omit the message to start the TUI'
            )
        if args.mode == "json":
            raise CLIError("--mode json only applies to headless --print runs")
        return _launch_tui(args, config)
    except CLIError as exc:
        print(f"tau: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
