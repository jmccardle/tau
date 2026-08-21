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
    mode: str = "text"  # text | json
    model: str | None = None
    provider: str | None = None
    tools: str | None = None  # comma-separated allowlist
    no_tools: bool = False
    # Extensions + tool-filtering flags (E0/S2; pi args.ts:104-153). Threaded into
    # the headless run config; the loader/registry consumers land in E1 (S3+).
    extensions: list[str] = field(default_factory=list)  # --extension/-e (repeatable path)
    no_extensions: bool = False  # -ne → suppress DISCOVERY only; explicit -e still load
    # --bus (H8): declares that this run may reach a message bus, so an extension
    # declaring TOUCHES_BUS is allowed to load. A capability grant, not a
    # connection — the broker URL is per-extension config.
    bus: bool = False
    exclude_tools: str | None = None  # -xt → comma-separated tool denylist
    # -nbt → drop the BUILT-IN set only; extension-registered tools survive.
    # Paired with ``no_tools`` (-nt, zero tools of any kind) by
    # ``headless.resolve_no_tools``, which is where the two become one value —
    # nothing downstream reads either boolean.
    no_builtin_tools: bool = False
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
    # Session store backend (W12): --store file|jmfts overrides config.json
    # "session_store.backend" for this run only. None → let config decide (default
    # "file"). docs/JMFTS-INTEGRATION-PLAN.md §3.1.
    store: str | None = None
    # --session-dir DIR (pi args.ts:112, docs/CLI-PLAN.md §3 "Secondary"): the
    # file store's base directory, i.e. seam 1's `base_dir`. None → each mode's
    # own default: ~/.tau/sessions for the TUI and --print, <tmp>/.tau-<uid>/sessions
    # for --mode rpc (unit S / docs/RPC-TIER-B.md D-6 — an RPC host spawning τ
    # per request must not fill the human's session list, and must not steal
    # their `--continue`). Unlike --name/--store this is NOT rejected under
    # --mode rpc: it is precisely the flag a host uses to opt back INTO the
    # user's session list.
    session_dir: str | None = None
    # One-shot JMFTS import/export commands (W12 §"Expose the importer"). Neither
    # combines with --print/messages/session-continuation flags — each runs the
    # requested round-trip and exits, independent of the agent loop entirely.
    import_session: str | None = None  # --import-session PATH
    export_session: list[str] | None = None  # --export-session REF PATH (nargs=2)
    verbose: bool = False
    # --fun / --no-fun: randomize the TUI's startup tagline. Reaches exactly one
    # string (tau_coding_agent.tagline.pick_tagline) and nothing else; inert in
    # every headless path, which prints no tagline. The DEFAULT is the packaged
    # one — False in a checkout, rewritten to True by package.sh.
    fun: bool = FUN_DEFAULT

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
            '  tau -p --store jmfts "hi"   # headless turn persisted to JMFTS\n'
            "  tau --import-session x.jsonl        # copy a file session into JMFTS\n"
            "  tau --export-session 42 x.jsonl     # copy JMFTS doc 42 to a file\n"
            "  tau --mode rpc --model gpt-4o       # JSON-RPC 2.0 server over stdio\n"
            "\n"
            "--resume (interactive picker) is available in the TUI, not headlessly.\n"
            "--mode rpc runs a persistent protocol server (docs/REMOTE-CONTROL.md); "
            "it does not combine with --print."
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
        choices=["text", "json", "rpc"],
        default="text",
        help=(
            "headless output format: text transcript (default) or JSONL events; "
            "'rpc' runs a persistent JSON-RPC 2.0 server over stdio instead "
            "(docs/REMOTE-CONTROL.md) and does not combine with --print"
        ),
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
    # H8's capability declaration, reaching the console. `bus_available` was a
    # `create_agent_session` parameter that NOTHING in this package set, so the
    # loader refused every TOUCHES_BUS extension in the TUI and in print mode —
    # only a hand-written script could load one. This is the allowance, stated
    # by the operator rather than inferred: it says "this run may reach a message
    # bus", not "connect to one" (the URL is per-extension config).
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
    # The one paired boolean in the flag set, because it is the one flag whose
    # default flips between a source checkout (off) and a release (on) — so BOTH
    # directions have to be expressible. A released τ needs --no-fun to get a
    # reproducible screen back; a checkout needs --fun to see the other taglines.
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
        tools=ns.tools,
        no_tools=ns.no_tools,
        # action="append" yields None when the flag is absent → normalize to [].
        extensions=list(ns.extensions or []),
        no_extensions=ns.no_extensions,
        bus=ns.bus,
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
        store=ns.store,
        session_dir=ns.session_dir,
        import_session=ns.import_session,
        export_session=list(ns.export_session) if ns.export_session else None,
        verbose=ns.verbose,
        fun=ns.fun,
    )


def _launch_tui(args: CLIArgs, config: dict) -> int:
    """Launch the Parley TUI, applying model/system-prompt overrides."""
    overrides: dict = {}
    # Neither ``args.no_tools`` nor ``args.tools`` is a trigger here. Both used to
    # be, and for both that was the only way they took effect in the TUI: this
    # block writes into ONE model ENTRY, so ``/model`` mid-session switched to a
    # different entry and silently handed the tools back — the denied set under
    # ``-nt``, the un-allowlisted set under ``-t``. Run-level policy has to ride in
    # ``run_config`` with the other tool flags (see below), which is where both
    # now go. ``--model``/``--provider``/``--thinking`` stay: those really do
    # describe one model entry.
    if args.model or args.provider or args.thinking:
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
    # them, and -xt/-nt/-nbt/--append-system-prompt don't trigger the override
    # block above): explicit ``-e`` paths + ``-ne`` discovery, plus the tool/prompt
    # flags (E5 §2.2-2.3). Passed even when empty so the app has a definite policy.
    exclude_tools = (
        [t.strip() for t in args.exclude_tools.split(",") if t.strip()]
        if args.exclude_tools
        else []
    )
    # ``--tools``: the built-in ALLOWLIST, parsed here so a malformed value fails
    # before the TUI launches, and carried run-level for the reason above.
    # ``None`` (flag absent) is distinct from a parsed list and means "no
    # allowlist" — the model entry's own ``tools`` key, if any, still decides.
    tool_allowlist: list[str] | None = None
    if args.tools:
        tool_allowlist = [t.strip() for t in args.tools.split(",") if t.strip()]
        if not tool_allowlist:
            raise CLIError("--tools given but no tool names parsed")
    # Per-extension config overrides (S40): parse ``--ext-config`` here so a
    # malformed item surfaces as a clean CLI error BEFORE the TUI launches; the app
    # merges them over config.json's ``"extensions"`` at each backend load.
    ext_config_overrides = parse_ext_config_overrides(list(args.ext_config or []))
    run_config = {
        "extensions": list(args.extensions or []),
        "no_extensions": args.no_extensions,
        # H8 capability, run-level like the extension flags it gates: a model
        # switch must not silently revoke it mid-session.
        "bus": args.bus,
        "exclude_tools": exclude_tools,
        "tools": tool_allowlist,
        # ONE resolved value for -nt/-nbt (``"all"``/``"builtin"``/``None``),
        # collapsed here at the argv boundary exactly as pi does in
        # main.ts:424-428. The app never sees the two booleans, so it has no
        # interaction left to re-derive.
        "no_tools": resolve_no_tools(args),
        "append_system_prompt": list(args.append_system_prompt or []),
        "ext_config": ext_config_overrides,
        # --store (W12): threaded to Parley.__init__, which resolves it via
        # store_factory.build_session_catalog before building the SessionCatalog
        # the TUI's ChatSidebar/session lifecycle uses.
        "store": args.store,
        # --session-dir (unit S): same seam, same call — the TUI's sidebar then
        # lists exactly what lives under DIR, which is how a human reviews the
        # RPC sessions written to <tmp>/.tau-<uid>/sessions.
        "session_dir": args.session_dir,
    }

    # Textual is the ``[tui]`` extra, not a base dependency — ``tau -p`` runs a
    # full turn without it. This lazy import is the ONLY crossing into the Textual
    # app, so it is also the only place the absence can be reported. A bare
    # ModuleNotFoundError naming `textual` tells the user nothing: they installed
    # τ, not Textual, and the fix is an extra they were never shown.
    # Only the two libraries the extra actually provides are translated. Any other
    # ModuleNotFoundError from app.py's import chain is a real defect and must keep
    # its own traceback rather than be relabelled as a packaging problem.
    try:
        from tau_coding_agent.app import Parley
    except ModuleNotFoundError as exc:
        if exc.name not in {"textual", "rich"}:
            raise
        raise CLIError(
            f"the interactive TUI needs the 'tui' extra ({exc.name} is missing): "
            "pip install 'ffwf-tau-coding-agent[tui]'. Headless mode "
            "(tau -p ...) works without it."
        ) from exc

    # `fun` rides as its own argument rather than inside run_config: run_config is
    # threaded into every backend this app builds, and the tagline flag has no
    # business being visible from there. This is the flag's ONLY crossing into the
    # app, and Parley resolves it to a string in __init__.
    app = Parley(cli_overrides=overrides or None, cli_run_config=run_config, fun=args.fun)
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
        # --import-session/--export-session (W12): one-shot JMFTS<->file copies
        # that run instead of a turn/the TUI, so they are dispatched before
        # anything else and reject being combined with run-mode flags (Fail-Early
        # — silently ignoring, say, a stray --print would be confusing).
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
                # --session-dir names a FILE-store location, and these two
                # commands talk to JMFTS directly (see _run_import_session's
                # docstring: they ignore --store for the same reason). Refused
                # rather than accepted-and-ignored, exactly like the pairs above.
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

        # --session-dir names where a PERSISTED session goes; --no-session
        # persists none. Rejected rather than ignored (Fail-Early — the same
        # reasoning headless.run_print applies to --no-session + --continue).
        if args.no_session and args.session_dir is not None:
            raise CLIError(
                "--session-dir names where sessions are stored, but --no-session "
                "stores none (the run is ephemeral); drop one of them"
            )

        # --mode rpc: a persistent JSON-RPC 2.0 server over stdio
        # (docs/REMOTE-CONTROL.md), unreachable before this branch existed —
        # §3 of that doc: "cli.py:141-145 accepts --mode text|json ... and
        # the file contains no reference to RPCHandler." It is its own run
        # mode, neither the TUI nor --print, so it is dispatched explicitly
        # here rather than left to fall through the print/TUI branches below
        # by accident (the precedent this file already sets a few checks
        # down: an incoherent flag combination is refused, not silently
        # resolved one way).
        #
        # It does NOT require --print, and the two are mutually exclusive
        # rather than "rpc implies print": -p means "run one turn from argv
        # text, print it, exit"; --mode rpc means "read an unbounded stream
        # of requests from stdin until told to stop". pi's own rpc
        # invocation (PI_RPC_REPLACEMENT.md §1.1 — the consumer this surface
        # is meant to replace) already proves the shape: `pi --mode rpc
        # --provider ... --model ... --no-session --tools bash`, no print
        # flag anywhere. Session/model/extension setup still goes through
        # the same resolve_model_config/create_backend/load_extensions path
        # every other mode uses (tau_coding_agent.rpc_mode.run_rpc) — this
        # block only decides WHETHER to take that path, not how it runs.
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
                # Unchanged rejection (D-6: "the startup CLI restrictions are
                # untouched"); only the REASON is restated, because the old
                # wording ("does not persist one yet") stopped being true when
                # Blocker 2 moved the startup session onto `catalog.create`.
                # --session-dir is deliberately NOT in this set: it is the one
                # startup session flag --mode rpc honors, since it is how a
                # host chooses between its private <tmp>/.tau-<uid>/sessions default
                # and the user's own list (unit S).
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
    except ConfigError as exc:
        # CLIError subclasses ConfigError, so this catches both a bad flag and a
        # malformed ~/.tau/config.json.
        print(f"tau: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
