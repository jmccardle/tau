"""CLI entry point for τ-coding-agent.

Parses arguments (argparse, pi-aligned flags), then either runs a headless
``--print`` turn (see :mod:`tau_coding_agent.headless`) or launches the TauApp
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
import sys
from dataclasses import dataclass, field

from tau_llm.models import EXTENDED_THINKING_LEVELS
from tau_coding_agent.config import TAU_DIR, ConfigError, load_config
from tau_coding_agent.headless import (
    CLIError,
    parse_ext_config_overrides,
    resolve_model_config,
    resolve_no_tools,
    run_print,
)
from tau_coding_agent.tagline import FUN_DEFAULT

__all__ = ["TAU_DIR", "load_config", "main"]


def _version() -> str:
    """Return τ's release version (single source: ``tau_coding_agent.__version__``)."""
    from tau_coding_agent import __version__

    return __version__


@dataclass
class CLIArgs:
    """Parsed τ CLI arguments.

    Kept as a typed dataclass (rather than a bare argparse Namespace) for clean
    attribute access and so callers/tests can construct defaults directly.
    """

    messages: list[str] = field(default_factory=list)
    print_mode: bool = False
    mode: str = "text"  # text | json | rpc | repl
    model: str | None = None
    provider: str | None = None
    theme: str | None = None
    tools: str | None = None  # comma-separated allowlist
    no_tools: bool = False
    extensions: list[str] = field(default_factory=list)  # --extension/-e (repeatable path)
    no_extensions: bool = False  # -ne → suppress DISCOVERY only; explicit -e still load
    bus: bool = False
    exclude_tools: str | None = None  # -xt → comma-separated tool denylist
    no_builtin_tools: bool = False
    no_session: bool = False  # --no-session → ephemeral, unpersisted run
    max_turns: int | None = None
    ext_config: list[str] = field(default_factory=list)
    ui_defaults: str | None = None
    append_system_prompt: list[str] = field(default_factory=list)  # repeatable
    system_prompt: str | None = None
    no_context_files: bool = False
    thinking: str | None = None  # off|minimal|low|medium|high|xhigh
    continue_session: bool = False  # --continue/-c → most-recent session
    resume: bool = False
    session: str | None = None  # --session REF → specific session (path|stem)
    fork: str | None = None  # --fork REF → fork a session into a new one
    name: str | None = None  # --name/-n → session display title
    store: str | None = None
    session_dir: str | None = None
    import_session: str | None = None  # --import-session PATH
    export_session: list[str] | None = None  # --export-session REF PATH (nargs=2)
    verbose: bool = False
    fun: bool = FUN_DEFAULT

    @property
    def is_verbose(self) -> bool:
        return self.verbose

    @property
    def is_json_output(self) -> bool:
        return self.mode == "json"


def _positive_int(raw: str) -> int:
    """argparse type for a count that must be at least 1.

    ``--max-turns 0`` is refused here rather than at ``AgentLoopConfig``, whose
    ``ge=1`` would surface as a pydantic ValidationError three layers down, long
    after the TUI had started. Zero is a way of spelling "run no turns", which no
    caller means on purpose; "no ceiling" is spelled by omitting the flag.
    """
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"must be at least 1, got {value} — omit the flag for no limit"
        )
    return value


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
            "  tau --resume                # TUI, pick a saved session to resume\n"
            '  tau -p -c "and then?"       # continue the most recent session\n'
            '  tau -p --session 17188 "go" # resume a session by filename stem\n'
            '  tau -p --store jmfts "hi"   # headless turn persisted to JMFTS\n'
            "  tau --import-session x.jsonl        # copy a file session into JMFTS\n"
            "  tau --export-session 42 x.jsonl     # copy JMFTS doc 42 to a file\n"
            "  tau --mode rpc --model gpt-4o       # JSON-RPC 2.0 server over stdio\n"
            "\n"
            "--resume opens the interactive session picker, so it needs a prompt\n"
            "(the TUI or --mode repl); a headless run names its session with\n"
            "--continue or --session REF.\n"
            "--mode rpc runs a persistent protocol server (docs/REMOTE-CONTROL.md) "
            "and --mode repl an interactive prompt loop (docs/REPL-HEAD.md); "
            "neither combines with --print."
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
        choices=["text", "json", "rpc", "repl"],
        default="text",
        help=(
            "headless output format: text transcript (default) or JSONL events; "
            "'rpc' runs a persistent JSON-RPC 2.0 server over stdio instead "
            "(docs/REMOTE-CONTROL.md) and does not combine with --print; "
            "'repl' runs an interactive prompt loop in the terminal's own "
            "scrollback (docs/REPL-HEAD.md), also without --print"
        ),
    )
    parser.add_argument(
        "--model",
        "-m",
        default=None,
        help="model name from ~/.tau/config.json, or provider/id shorthand",
    )
    parser.add_argument(
        "--theme",
        default=None,
        help=(
            "TUI colour theme for this run only, overriding the 'theme' key in "
            "~/.tau/config.json (built in: mocha, latte, gruvbox, ansi; plus any "
            "~/.tau/themes/<name>.json). Picking a theme from the command palette "
            "still saves; this flag never does"
        ),
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
        help="offer the model no tools at all, built-in or extension-registered",
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
        "--bus",
        dest="bus",
        action="store_true",
        help=(
            "declare that this run may reach a message bus, so extensions "
            "declaring TOUCHES_BUS are allowed to load (e.g. nats_bus)"
        ),
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
        help="disable the built-in tools; extension-registered tools still apply",
    )
    parser.add_argument(
        "--no-session",
        dest="no_session",
        action="store_true",
        help="run ephemerally without persisting a session to disk",
    )
    parser.add_argument(
        "--max-turns",
        dest="max_turns",
        type=_positive_int,
        default=None,
        metavar="N",
        help="stop the agent after N LLM calls in one run (default: no limit; "
        'over ~/.tau/config.json "max_turns")',
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
        '"ui_defaults". --print and --mode rpc only — the TUI and --mode repl '
        "have a human to ask",
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
        help="replace the base system prompt for this run (project context "
        "files still load; add -nc to suppress those too)",
    )
    parser.add_argument(
        "--no-context-files",
        "-nc",
        dest="no_context_files",
        action="store_true",
        help="skip project context discovery (AGENTS.md, CLAUDE.md, .tau/SYSTEM.md) for this run",
    )
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
        help="open the interactive session picker at startup (TUI and --mode "
        "repl — a headless run has no picker; use --continue or --session REF there)",
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
        "--store",
        default=None,
        choices=["file", "jmfts"],
        help="session store backend for this run, overriding ~/.tau/config.json "
        '"session_store.backend" (default: file)',
    )
    parser.add_argument(
        "--session-dir",
        dest="session_dir",
        default=None,
        metavar="DIR",
        help="store sessions under DIR instead of the default location "
        "(~/.tau/sessions for the TUI and --print; a private <tmp>/.tau-<uid>/sessions "
        "for --mode rpc, so an RPC host does not fill your session list). "
        "File store only — it has no meaning for --store jmfts",
    )
    parser.add_argument(
        "--import-session",
        dest="import_session",
        default=None,
        metavar="PATH",
        help="import a .jsonl session file into the configured JMFTS store, then exit",
    )
    parser.add_argument(
        "--export-session",
        dest="export_session",
        nargs=2,
        default=None,
        metavar=("REF", "PATH"),
        help="export a JMFTS-backed session (REF = JMFTS doc id) to a .jsonl file, then exit",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="verbose logging (long-only; pi-aligned, -v is --version)",
    )
    parser.add_argument(
        "--fun",
        action=argparse.BooleanOptionalAction,
        default=FUN_DEFAULT,
        help=(
            "pick the startup tagline at random instead of always the first "
            f"(default: {'on' if FUN_DEFAULT else 'off'}; affects nothing but that one line)"
        ),
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
        theme=ns.theme,
        tools=ns.tools,
        no_tools=ns.no_tools,
        # action="append" yields None when the flag is absent → normalize to [].
        extensions=list(ns.extensions or []),
        no_extensions=ns.no_extensions,
        bus=ns.bus,
        exclude_tools=ns.exclude_tools,
        no_builtin_tools=ns.no_builtin_tools,
        no_session=ns.no_session,
        max_turns=ns.max_turns,
        ext_config=list(ns.ext_config or []),
        ui_defaults=ns.ui_defaults,
        append_system_prompt=list(ns.append_system_prompt or []),
        system_prompt=ns.system_prompt,
        no_context_files=ns.no_context_files,
        thinking=ns.thinking,
        continue_session=ns.continue_session,
        resume=ns.resume,
        session=ns.session,
        fork=ns.fork,
        name=ns.name,
        store=ns.store,
        session_dir=ns.session_dir,
        import_session=ns.import_session,
        export_session=list(ns.export_session) if ns.export_session else None,
        verbose=ns.verbose,
        fun=ns.fun,
    )


def _launch_tui(args: CLIArgs, config: dict) -> int:
    """Launch the TauApp TUI, applying model/system-prompt overrides."""
    overrides: dict = {}
    if args.model or args.provider or args.thinking:
        name, model_config = resolve_model_config(config, args)
        existing = config.get("models", {}).get(name, {})
        overrides["models"] = {name: {**existing, **model_config}}
        overrides["default_model"] = name
    if args.system_prompt is not None:
        overrides["system_prompt"] = args.system_prompt
    if args.theme is not None:
        overrides["theme"] = args.theme

    exclude_tools = (
        [t.strip() for t in args.exclude_tools.split(",") if t.strip()]
        if args.exclude_tools
        else []
    )
    tool_allowlist: list[str] | None = None
    if args.tools:
        tool_allowlist = [t.strip() for t in args.tools.split(",") if t.strip()]
        if not tool_allowlist:
            raise CLIError("--tools given but no tool names parsed")
    ext_config_overrides = parse_ext_config_overrides(list(args.ext_config or []))
    run_config = {
        "extensions": list(args.extensions or []),
        "no_extensions": args.no_extensions,
        "bus": args.bus,
        "exclude_tools": exclude_tools,
        "tools": tool_allowlist,
        "no_tools": resolve_no_tools(args),
        "append_system_prompt": list(args.append_system_prompt or []),
        "no_context_files": args.no_context_files,
        "max_turns": args.max_turns,
        "ext_config": ext_config_overrides,
        "store": args.store,
        "session_dir": args.session_dir,
    }

    try:
        from tau_coding_agent.app import TauApp
    except ModuleNotFoundError as exc:
        if exc.name not in {"textual", "rich"}:
            raise
        raise CLIError(
            f"the interactive TUI needs the 'tui' extra ({exc.name} is missing): "
            "pip install 'ffwf-tau-coding-agent[tui]'. Headless mode "
            "(tau -p ...) works without it."
        ) from exc

    app = TauApp(
        cli_overrides=overrides or None,
        cli_run_config=run_config,
        fun=args.fun,
        resume=args.resume,
    )
    app.run()
    return 0


def _run_import_session(path: str, config: dict) -> int:
    """``--import-session PATH``: materialize a file-store ``.jsonl`` session as
    a JMFTS conversation subtree, then exit (W12 "Expose the importer";
    ``tau_jmfts.importer.import_session`` does the actual work — this is just
    the CLI surface for it, per docs/JMFTS-INTEGRATION-PLAN.md §3.4/importer.py).

    Always targets the configured JMFTS store directly (``session_store.url``/
    ``$JMFTS_API_URL``), independent of ``--store``/``session_store.backend`` —
    an import's whole point is to populate JMFTS, so there is no "file" reading
    of this flag to honor.
    """
    from tau_coding_agent.store_factory import build_jmfts_client, resolve_host_parent_id

    client = build_jmfts_client(config)
    try:
        from tau_jmfts.importer import import_session

        log = import_session(path, client, host_parent_id=resolve_host_parent_id(config))
    finally:
        client.close()
    print(f"imported {path} -> JMFTS document {log.root_doc_id} (session {log.id})")
    return 0


def _run_export_session(ref: str, path: str, config: dict) -> int:
    """``--export-session REF PATH``: write a JMFTS-backed session (REF = doc
    id) as a file-store-shaped ``.jsonl`` the file ``Session.load`` can open
    directly, then exit. See :func:`_run_import_session` for the inverse."""
    from tau_coding_agent.store_factory import build_jmfts_client

    client = build_jmfts_client(config)
    try:
        from tau_jmfts.importer import export_session
        from tau_jmfts.store import JmftsSessionLog

        log = JmftsSessionLog.load(client, ref)
        export_session(log, path)
    finally:
        client.close()
    print(f"exported JMFTS document {ref} -> {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``tau`` console script."""
    args = parse_cli_args(argv)

    if args.verbose:
        print(f"τ-coding-agent args: {args}", file=sys.stderr)

    try:
        if args.import_session is not None or args.export_session is not None:
            if args.import_session is not None and args.export_session is not None:
                raise CLIError("--import-session and --export-session are mutually exclusive")
            if (
                args.print_mode
                or args.messages
                or args.resume
                or args.continue_session
                or args.session
                or args.fork
                or args.session_dir is not None
            ):
                raise CLIError(
                    "--import-session/--export-session run a one-shot JMFTS copy and "
                    "exit; they can't be combined with --print, messages, "
                    "session-continuation flags, or --session-dir (which names a "
                    "file-store location these commands never read)"
                )
            config = load_config()
            if args.import_session is not None:
                return _run_import_session(args.import_session, config)
            assert args.export_session is not None  # the outer `or` guarantees this branch
            ref, path = args.export_session
            return _run_export_session(ref, path, config)

        if args.no_session and args.session_dir is not None:
            raise CLIError(
                "--session-dir names where sessions are stored, but --no-session "
                "stores none (the run is ephemeral); drop one of them"
            )

        if args.mode == "rpc":
            if args.print_mode:
                raise CLIError(
                    "--mode rpc runs a persistent JSON-RPC server over stdio; it "
                    "cannot be combined with --print, which runs a single "
                    "headless turn from argv and exits. Drop -p/--print, or use "
                    "--mode text/json for a headless run."
                )
            if args.messages:
                raise CLIError(
                    "--mode rpc reads requests from stdin (the 'prompt'/'submit' "
                    "RPC methods), not positional arguments; drop the trailing "
                    "message text."
                )
            if args.resume or args.continue_session or args.session or args.fork:
                raise CLIError(
                    "--mode rpc does not support session continuation FLAGS AT "
                    "STARTUP (--continue/--session/--fork/--resume); a run always "
                    "starts a fresh AgentSession of its own. Session lifecycle "
                    "IS reachable once the process is running, over the wire "
                    "(the new_session/fork/switch_session RPC verbs, "
                    "docs/REMOTE-CONTROL.md §4[6]) — connect and call one of "
                    "those instead of asking the CLI to start pre-attached to a "
                    "session."
                )
            if args.name is not None or args.store is not None:
                raise CLIError(
                    "--name/--store name/select a session AT STARTUP, and a "
                    "--mode rpc process always starts on a fresh session of its "
                    "own choosing; it reaches the session store over the wire "
                    "instead (switch_session's session_id resolves against the "
                    "same default store every other mode uses with no --store "
                    "flag). --session-dir IS accepted, and is how a host chooses "
                    "where that startup session lives."
                )
            config = load_config()
            from tau_coding_agent.rpc_mode import run_rpc

            return asyncio.run(run_rpc(args, config))

        if args.mode == "repl":
            if args.print_mode:
                raise CLIError(
                    "--mode repl is an interactive prompt loop; -p/--print runs "
                    "one headless turn and exits. Drop -p, or use --mode "
                    "text/json for a headless run."
                )
            if args.messages:
                raise CLIError(
                    "--mode repl reads prompts from its own prompt line, not "
                    "positional arguments; drop the trailing message text."
                )
            if args.ui_defaults is not None:
                raise CLIError(
                    "--ui-defaults auto-answers extension dialogs when no human is "
                    "present; --mode repl has one at the prompt, so a form is asked "
                    'there. Drop --ui-defaults (config.json "ui_defaults" is ignored '
                    "by this mode too)."
                )
            if args.theme is not None:
                raise CLIError(
                    "--theme selects a TUI stylesheet; --mode repl renders with "
                    "rich in the terminal's own colours."
                )
            config = load_config()
            try:
                from tau_coding_agent.repl import run_repl
            except ModuleNotFoundError as exc:
                if exc.name not in {"rich", "prompt_toolkit"}:
                    raise
                raise CLIError(
                    f"--mode repl needs the 'repl' extra ({exc.name} is missing): "
                    "pip install 'ffwf-tau-coding-agent[repl]'"
                ) from exc

            return asyncio.run(run_repl(args, config))

        if args.resume and args.print_mode:
            raise CLIError(
                "--resume opens an interactive session picker, and --print runs "
                "one headless turn with no screen to open it on. Use --continue "
                "(most recent) or --session REF to name a session headlessly, or "
                "drop -p to pick one in the TUI"
            )
        if (args.continue_session or args.session or args.fork) and not args.print_mode:
            raise CLIError(
                "--continue/--session/--fork require --print (headless); in the "
                "TUI, resume a session with --resume (or /resume, or the command "
                "palette's 'Resume session…')"
            )
        config = load_config()
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
    except ConfigError as exc:
        print(f"tau: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
