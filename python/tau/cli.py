"""The `tau` command: `tau config check [--show MODEL]`, `tau config set EXTENSION.FIELD=VALUE
...`, `tau trust [DIR]` (D13, D1), `tau ext list|enable|disable` (D19) and `tau sessions
[--all] [--json]`, `tau sessions name REF [NAME]` and `tau export REF` (D18), `tau context
[--show]` (D20), `tau serve` (D21; `tau._serve_cli`) and the launch command line: `tau -p
[MESSAGE...]` (D22; `tau._print_mode`), `tau` (D23; the terminal UI, `tau.tui`) and `tau
--version`. A first argument that names none of the commands is the launch command line
(`tau._launch`). `tau._session_start.start_session` is the shared start of `tau`, `tau -p`,
`tau serve` and the programs that follow.

`main` takes its streams, environment, and working directory as arguments so a test
drives it with real files and real streams. Exit status: 0 success, 1 a problem
(printed as one message on stderr, never a traceback), 2 a usage error, 130 interrupted.
Everything printed from config data goes through `safe_text`. `config set` and `ext enable` /
`disable` write the user's `<TAU_HOME>/config.json` and never the project's `.tau/config.json`.
`extension_flags` is the `-e` and `--no-extensions` parser a program that starts a session adds
(`parents=[extension_flags()]`), and gives `load_extensions` (D19 section 5); `option_flags` is
`--set EXTENSION.FIELD=VALUE`, the options of one run (D22); `session_flags` is
the one for the session (`-c`, `-r`, `--session`, `--fork`, `--no-session`, `-n`, `--session-dir`;
D18 section 5), which `start_session` reads; it carries the prompt flags (`--system-prompt`,
`--append-system-prompt`, `-nc`; D20) too.
"""

import argparse
import asyncio
import io
import json
import os
import shlex
import shutil
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import NamedTuple, TextIO

from tau._config import ConfigError, Price
from tau._config_layers import (
    LayerText,
    ModelCatalog,
    check_config,
    option_problems,
    read_models,
)
from tau._config_tree import ConfigProblem
from tau._context import system_prompt
from tau._core import (
    BadRecord,
    Corrupt,
    Leased,
    NoSuchNode,
    NotASession,
    Session,
    Undecodable,
    Version,
)
from tau._dotenv import DotenvError
from tau._extensions import (
    ExtensionLoadError,
    ExtensionSource,
    absolute_explicit,
    inspect_extensions,
    list_extensions,
    switch_source,
)
from tau._launch import (
    UsageError,
    assignment,
    extension_flags,
    launch_parser,
    option_flags,
    prompt_flags,
    session_dir_flag,
    session_flags,
    verbose_flag,
)
from tau._markdown import to_markdown
from tau._options import save_options
from tau._print_mode import SIGPIPE_STATUS, detach, print_mode
from tau._project_read import TrustError, safe_block, safe_text
from tau._protocol import PROTOCOL, ProtocolError
from tau._protocols import Provider
from tau._providers import builtin_providers
from tau._refs import absolute_ref, parse_ref
from tau._serve_cli import error_line, serve
from tau._serve_record import ServeFailed
from tau._sessions import (
    UnknownSession,
    age_of,
    find_session,
    first_cursor_node,
    list_sessions,
    read_name,
    rename_session,
    sessions_directory,
)
from tau._startup import LoadedConfig, load_config, option_changes, prompts, select_model
from tau._trust import (
    TAU_DIR,
    UntrustedProject,
    create_project,
    ensure_trusted,
    find_project,
    project_environment,
    refuse_special,
    tau_home,
    user_config_path,
)
from tau._user_config import UserConfig

__all__ = ["extension_flags", "main", "option_flags", "session_flags"]

_USAGE_ERROR = 2
_INTERRUPTED = 130
_UNTRUSTED = " [untrusted project: not used until `tau trust`]"


@dataclass(frozen=True)
class _Run:
    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    environ: Mapping[str, str]
    cwd: Path
    argv: Sequence[str]  # after the command's name


class _Parsers(NamedTuple):
    top: argparse.ArgumentParser
    config: argparse.ArgumentParser
    ext: argparse.ArgumentParser
    commands: tuple[str, ...]


def _parser() -> _Parsers:
    parser = argparse.ArgumentParser(prog="tau", description="τ next (Trusty).")
    sub = parser.add_subparsers(dest="command")
    config = sub.add_parser("config", help="inspect configuration")
    config_sub = config.add_subparsers(dest="config_command", metavar="{check,set}")
    check = config_sub.add_parser("check", help="report every problem in the config files")
    check.add_argument("--show", metavar="MODEL", help="print MODEL's resolved values and sources")
    saved = config_sub.add_parser(
        "set", help="write extension options into your config file (every session)"
    )
    saved.add_argument(
        "assignments",
        nargs="+",
        type=assignment,
        metavar="EXTENSION.FIELD=VALUE",
        help="a value is read as `/config` reads it",
    )
    ext = sub.add_parser("ext", help="the extensions that load")
    ext_sub = ext.add_subparsers(dest="ext_command", metavar="{list,enable,disable}")
    listing = ext_sub.add_parser(
        "list", parents=[extension_flags()], help="every source, and why it is on or off"
    )
    listing.add_argument("--json", action="store_true", help="one JSON object per source, per line")
    for name, what in (("enable", "load"), ("disable", "not load")):
        switch = ext_sub.add_parser(name, help=f"{what} the source KEY at every start")
        switch.add_argument("key", metavar="KEY", help="the source's key (see `tau ext list`)")
    sessions = sub.add_parser(
        "sessions", parents=[session_dir_flag()], help="list sessions; `sessions name` names one"
    )
    sessions.add_argument("--all", action="store_true", help="every directory, with its cwd")
    sessions.add_argument(
        "--json", action="store_true", help="one JSON object per session, per line"
    )
    sessions_sub = sessions.add_subparsers(dest="sessions_command", metavar="{name}")
    named = sessions_sub.add_parser(
        "name", parents=[session_dir_flag(nested=True)], help="show or set a session's name"
    )
    named.add_argument("ref", metavar="REF", help="a path, an id, a name or an id prefix")
    named.add_argument("name", nargs="?", metavar="NAME", help="the new name; empty removes it")
    export = sub.add_parser(
        "export", parents=[session_dir_flag()], help="write a session's walk as Markdown"
    )
    export.add_argument("ref", metavar="REF", help="SESSION or SESSION:NODE")
    export.add_argument("-o", "--output", metavar="FILE", help="write FILE (default: standard out)")
    export.add_argument("--thinking", action="store_true", help="include the model's thinking")
    context = sub.add_parser(
        "context", parents=[prompt_flags()], help="what a new session here would tell the model"
    )
    context.add_argument(
        "--show", action="store_true", help="print the system prompt instead of its parts"
    )
    served = sub.add_parser(
        "serve",
        parents=[session_flags(), extension_flags(), option_flags(), verbose_flag()],
        help="serve a session on a Unix socket (D21)",
    )
    served.add_argument(
        "--socket",
        metavar="PATH",
        help="listen here (default: $TAU_HOME/serve/<session id>.sock)",
    )
    mode = served.add_mutually_exclusive_group()
    mode.add_argument(
        "-d", "--detach", action="store_true", help="run in the background; print the server"
    )
    mode.add_argument(
        "--stop", action="store_true", help="stop the server of the session SESSION-FLAGS name"
    )
    mode.add_argument("--schema", action="store_true", help="print the wire's JSON Schema and exit")
    served.add_argument(
        "--json", action="store_true", help="one JSON object per line: the record, or an error"
    )
    served.add_argument("--detached-log", help=argparse.SUPPRESS)
    trust = sub.add_parser("trust", help="approve a project directory's .tau/")
    trust.add_argument(
        "dir", nargs="?", type=Path, help="a directory in the project (default: the current)"
    )
    return _Parsers(parser, config, ext, tuple(sub.choices))


@dataclass
class _Findings:
    """What `config check` prints, and whether any of it stops startup."""

    lines: list[str] = field(default_factory=lambda: [])
    failed: bool = False

    def error(self, where: Path, message: str) -> None:
        self.lines.append(f"{where}: error: {message}")
        self.failed = True


def _project_findings(
    root: Path, home: Path, environ: Mapping[str, str], found: _Findings
) -> tuple[LayerText | None, Mapping[str, str], bool]:
    """The project's config layer, the environment to check keys against, and whether the
    project is trusted. Everything that would make startup refuse is added to `found`."""
    try:
        project = ensure_trusted(root, home, ask=None)
    except UntrustedProject as e:
        command = f"tau trust {shlex.quote(str(root))}"
        found.error(root / TAU_DIR, f"project is not trusted; run `{command}`")
        return e.layer, environ, False
    except TrustError as e:  # a refused file, or TAU_HOME or trusted.json writable by others
        found.error(e.where or root / TAU_DIR, str(e))
        return None, environ, False
    try:
        return project.layer(), project_environment(project, environ), True
    except DotenvError as e:  # approved, so it parsed; only a changed parser gets here
        found.error(root / ".env", str(e))
        return project.layer(), environ, True


def _check(run: _Run, show: str | None) -> int:
    home = tau_home(run.environ)
    root = find_project(run.cwd, run.environ).root
    found = _Findings()
    layer: LayerText | None = None
    environ = run.environ
    trusted = True
    if root is not None:
        layer, environ, trusted = _project_findings(root, home, run.environ, found)
    providers = builtin_providers(environ)
    try:
        # The options of the extensions are checked once they are loaded, below.
        problems = check_config(user_config_path(home), layer, environ=environ, providers=providers)
    finally:  # a check opens no connection; closing is the builder's duty all the same
        asyncio.run(_close(providers))
    for p in problems:
        untrusted = not trusted and layer is not None and p.source == layer.source
        found.lines.append(str(p) + (_UNTRUSTED if untrusted else ""))
    if found.failed or any(p.severity == "error" for p in problems):
        _print(run, found.lines)
        return 1
    loaded = load_config(run.cwd, run.environ, ask=None)
    _extension_findings(loaded, found)
    _print(run, found.lines)
    if found.failed:
        return 1
    if show is not None:
        name = select_model(loaded.catalog, show, pick=prompts(run.stdin, run.stdout).pick)
        _show(loaded.catalog, name, run.stdout)
    return 0


def _print(run: _Run, lines: Sequence[str]) -> None:
    for line in lines:
        run.stdout.write(safe_block(line) + "\n")


def _extension_findings(loaded: LoadedConfig, found: _Findings) -> None:
    """Load the extensions, as a start would: a failed source is an error with the message a
    start prints, a `load` key no source has is a warning, and the options the layers set for
    the loaded extensions are checked against their types."""
    result = inspect_extensions(loaded)
    for source in result.sources:
        if source.kind == "load" and source.state == "off":
            key = source.key
            where = loaded.catalog.load_sources[key]
            note = f"{source.detail}"
            found.lines.append(str(ConfigProblem("warning", note, where, f"load.{key}")))
    if result.errors:
        found.lines += result.errors
        found.failed = True
        return
    for problem in option_problems(loaded.catalog, result.extensions):
        found.lines.append(str(problem))
        found.failed = found.failed or problem.severity == "error"


def _user_only(run: _Run) -> LoadedConfig:
    """What `config set` and `ext` load: the project's layer if it is trusted, else the user's
    alone (a project that is not trusted is neither asked about nor read)."""
    try:
        return load_config(run.cwd, run.environ, ask=None)
    except UntrustedProject:
        home = tau_home(run.environ)
        catalog = read_models(user_config_path(home))
        return LoadedConfig(catalog, run.environ, None, home, UserConfig(home))


def _set(run: _Run, assignments: Sequence[tuple[str, str, str]]) -> int:
    """Set each `extension.field` in the user's file, all or none: every extension's options
    are built from the file as it would be before it is written. The extensions are those a
    start loads (a project's own only while it is trusted); the project's config is never
    written."""
    loaded = _user_only(run)
    extensions = inspect_extensions(loaded)
    extensions.raise_failures()
    save_options(UserConfig(loaded.home), option_changes(extensions.extensions, assignments))
    shown = " ".join(f"{e}.{n}={g}" for e, n, g in assignments)
    run.stdout.write(safe_text(f"set {shown} in {loaded.home / 'config.json'}") + "\n")
    return 0


def _commands_shown(source: ExtensionSource, sources: Sequence[ExtensionSource]) -> str:
    """Its commands as typed: the bare name where only one source has it, else qualified."""
    count: dict[str, int] = {}
    for other in sources:
        for command in other.commands:
            count[command.partition(":")[2]] = count.get(command.partition(":")[2], 0) + 1
    return ", ".join(
        "/" + (c.partition(":")[2] if count[c.partition(":")[2]] == 1 else c)
        for c in source.commands
    )


def _ext_rows(sources: Sequence[ExtensionSource]) -> list[str]:
    """`tau ext list`'s lines: a row per source, then its reason or its contents."""
    width = max(len(s.key) for s in sources)
    lines: list[str] = []
    for s in sources:
        lines.append(f"{s.key:<{width}}  {s.state:<9}  {s.origin}")
        pad = " " * (width + 2)
        if s.detail is not None:
            lines.append(f"{pad}{s.detail}")
        if s.state in ("on", "always"):
            commands = _commands_shown(s, sources) or "none"
            tools = ", ".join(s.tools) or "none"
            lines.append(f"{pad}commands: {commands}; tools: {tools}")
    return lines


def _ext_list(run: _Run, args: argparse.Namespace) -> int:
    sources = list_extensions(
        run.cwd,
        run.environ,
        explicit=absolute_explicit(args.extension, run.cwd),
        discover=not args.no_extensions,
    )
    if args.json:
        for source in sources:
            run.stdout.write(json.dumps(asdict(source)) + "\n")
    else:
        _print(run, _ext_rows(sources))
    return 1 if any(s.state == "failed" for s in sources) else 0


def _ext_switch(run: _Run, key: str, on: bool) -> int:
    note = switch_source(run.cwd, run.environ, key, on)
    path = tau_home(run.environ) / "config.json"
    run.stdout.write(safe_text(f"set load.{key} = {str(on).lower()} in {path}") + "\n")
    if note is not None:
        run.stdout.write(safe_text(note) + "\n")
    return 0


async def _close(providers: Mapping[str, Provider]) -> None:
    for provider in providers.values():
        await provider.aclose()


def _price_text(price: Price) -> dict[str, str]:
    return {k: str(getattr(price, k)) for k in ("input", "output", "cache_read", "cache_write")}


def _show(catalog: ModelCatalog, name: str, out: TextIO) -> None:
    config = catalog.resolve(name)
    values = {
        "model": config.model,
        "base_url": config.base_url,
        "max_tokens": config.max_tokens,
        "context_window": config.context_window,
        "price": None if config.price is None else json.dumps(_price_text(config.price)),
        "options": json.dumps(config.options, default=dict),
        "provider": config.provider,
        "api_key_env": config.api_key_env,
        "api_key": "(set)" if config.literal_api_key is not None else None,
    }
    out.write(f"{safe_text(name)}\n")
    for origin in catalog.explain(name):
        group = f", group {origin.group}" if origin.group else ""
        value = values.get(origin.key)
        out.write(safe_text(f"  {origin.key:<12} {value}  [{origin.source}{group}]") + "\n")
    prompt = catalog.system_prompt
    if prompt is None:
        out.write("  system_prompt  (built in)\n")
    else:
        out.write(
            safe_text(
                f"  system_prompt  {len(prompt.encode())} bytes  [{catalog.system_prompt_source}]"
            )
            + "\n"
        )


def _trust(run: _Run, given: Path | None) -> int:
    home = tau_home(run.environ)
    start = (run.cwd / given).resolve() if given is not None else run.cwd.resolve()
    refuse_special(start, run.environ)
    if not start.is_dir():
        raise TrustError(f"{start} is not a directory")
    search = find_project(start, run.environ)
    root = search.root
    created = root is None
    if root is None:
        # A `.tau` passed over further up does not matter; one at `start` is in the way.
        if search.skipped is not None and os.path.lexists(start / TAU_DIR):
            raise TrustError(f"no project: {search.skipped}")
        create_project(start)
        root = start
    asked = False

    def show_report(report: str) -> bool:
        nonlocal asked
        asked = True
        run.stdout.write(safe_block(report) + "\n")
        return True

    try:
        ensure_trusted(root, home, ask=show_report)
    except BaseException:
        if created:  # leave no empty `.tau/` that startup would then find and refuse
            shutil.rmtree(root / TAU_DIR, ignore_errors=True)
        raise
    run.stdout.write(safe_block(f"trusted {root}" if asked else f"already trusted: {root}") + "\n")
    return 0


def _context(run: _Run, args: argparse.Namespace) -> int:
    """The assembly a new session here would do, without creating one: each part with its origin
    and size in bytes, or with `--show` the text."""
    cwd = run.cwd.resolve()
    cli = None if args.system_prompt is None else {"system_prompt": args.system_prompt}
    loaded = load_config(cwd, run.environ, ask=prompts(run.stdin, run.stdout).ask, cli=cli)
    assembled = system_prompt(
        cwd, loaded, append=args.append_system_prompt, context_files=not args.no_context_files
    )
    if args.show:
        run.stdout.write(safe_block(assembled.text) + "\n")
    else:
        for source in assembled.sources:
            run.stdout.write(safe_text(f"{source.size:>8}  {source.origin}") + "\n")
    return 0


def _sessions_dir(run: _Run, given: str | None) -> Path:
    where = None if given is None else Path(os.path.abspath(run.cwd / given))
    return sessions_directory(tau_home(run.environ), where)


def _sessions(run: _Run, args: argparse.Namespace) -> int:
    rows = list_sessions(_sessions_dir(run, args.session_dir), cwd=None if args.all else run.cwd)
    now = time.time()
    for row in rows:
        if args.json:
            data = {**asdict(row), "path": str(row.path)}
            run.stdout.write(json.dumps(data) + "\n")
            continue
        title = f"error: {row.error}" if row.error else row.title
        where = f"{row.cwd or '?'}  " if args.all else ""
        run.stdout.write(safe_text(f"{row.id}  {age_of(row, now):>4}  {where}{title}") + "\n")
    return 0


def _session_name(run: _Run, args: argparse.Namespace) -> int:
    sessions = _sessions_dir(run, getattr(args, "session_dir", None))
    session, node = parse_ref(absolute_ref(args.ref, run.cwd))
    if node is not None:
        raise UnknownSession(f"a session's name is not about a node: {args.ref!r}")
    info = find_session(sessions, session)
    if info.error is not None:
        raise NotASession(f"{info.path}: {info.error}")
    if args.name is None:
        name = read_name(info.path)
        if name is not None:
            run.stdout.write(safe_text(name) + "\n")
        return 0 if name is not None else 1
    rename_session(info.path, args.name)
    named = args.name.strip()
    shown = f"named {info.id}: {named}" if named else f"removed the name of {info.id}"
    run.stdout.write(safe_text(shown) + "\n")
    return 0


def _export(run: _Run, args: argparse.Namespace) -> int:
    session, node = parse_ref(absolute_ref(args.ref, run.cwd))
    info = find_session(_sessions_dir(run, args.session_dir), session, first=True)
    if info.error is not None:
        raise NotASession(f"{info.path}: {info.error}")
    copy = Session.read(info.path)  # no lease: a session another process holds exports too
    try:
        at = first_cursor_node(copy) if node is None else node
        copy.tree.node(at)
        text = to_markdown(copy.tree.walk(at), thinking=args.thinking, of=info)
    finally:
        copy.close()
    if args.output is None:
        run.stdout.write(text)
    else:
        (run.cwd / args.output).write_text(text, encoding="utf-8")
    return 0


def _fail(err: TextIO, message: str) -> int:
    err.write("tau: " + safe_block(message) + "\n")
    return 1


def _failed(args: argparse.Namespace, out: TextIO, err: TextIO, error: BaseException) -> int:
    """Report `error`: one message on stderr; for `tau serve`, one JSON line, on stdout for
    `--json`, and on stderr (a detached server's log) where its parent reads it."""
    if args.command == "serve" and args.detached_log is not None:
        err.write(error_line(error) + "\n")
        return 1
    if args.command == "serve" and args.json:
        out.write(error_line(error) + "\n")
        return 1
    if args.command == "print" and args.json:
        out.write(error_line(error) + "\n")
    return _fail(err, str(error))


def _session_command(run: _Run, args: argparse.Namespace) -> int:
    if args.command == "export":
        return _export(run, args)
    if args.sessions_command == "name":
        return _session_name(run, args)
    return _sessions(run, args)


def _ext_command(run: _Run, args: argparse.Namespace) -> int:
    if args.ext_command == "list":
        return _ext_list(run, args)
    return _ext_switch(run, args.key, args.ext_command == "enable")


def _config_command(run: _Run, args: argparse.Namespace) -> int:
    if args.config_command == "set":
        return _set(run, args.assignments)
    return _check(run, args.show)


def _program(run: _Run, args: argparse.Namespace) -> int:
    """`tau serve`, `tau -p` or `tau`: the programs that start a session."""
    if args.command == "tui":
        from tau.tui._run import tui_mode  # noqa: PLC0415 - Textual, which -p never imports

        return tui_mode(run, args)
    return (serve if args.command == "serve" else print_mode)(run, args)


def _dispatch(run: _Run, args: argparse.Namespace) -> int:
    match args.command:
        case "trust":
            return _trust(run, args.dir)
        case "context":
            return _context(run, args)
        case "serve" | "print" | "tui":
            return _program(run, args)
        case "sessions" | "export":
            return _session_command(run, args)
        case "ext":
            return _ext_command(run, args)
        case _:
            return _config_command(run, args)


def _needs_help(args: argparse.Namespace, parsers: _Parsers) -> argparse.ArgumentParser | None:
    """The parser whose help to print because the command line stops at a group of commands."""
    if args.command is None:
        return parsers.top
    if args.command == "config" and args.config_command is None:
        return parsers.config
    if args.command == "ext" and args.ext_command is None:
        return parsers.ext
    return None


def _launch(
    run: _Run, given: Sequence[str], commands: Sequence[str]
) -> tuple[argparse.Namespace, int | None]:
    """The launch command line (`tau -p`, `tau --version`) and, if it is all done, its status:
    only `-p` runs anything yet, so a line without it shows help (2), and a message without it
    names no command."""
    launcher = launch_parser([session_flags(), extension_flags(), option_flags()], commands)
    args = launcher.parse_intermixed_args(list(given))
    if args.version:
        run.stdout.write(f"tau {version('ffwf-tau')} (protocol {PROTOCOL})\n")
        return args, 0
    if args.message and not args.print:
        launcher.error(f"{args.message[0]!r} is not a command; to send a message: tau -p MESSAGE")
    if args.print and args.theme:
        launcher.error("--theme is for the terminal UI, which -p does not open")
    if not args.print and (args.json or args.answer):
        launcher.error("--json and --answer are for -p: the terminal UI has a person")
    args.command = "print" if args.print else "tui"
    return args, None


def _parse(
    run: _Run, given: Sequence[str], parsers: _Parsers
) -> tuple[argparse.Namespace, int | None]:
    """The parsed command line and, if there is nothing to run, the status to leave with."""
    if not given or given[0] not in parsers.commands:
        return _launch(run, given, parsers.commands)
    args = parsers.top.parse_args(given)
    wanted = _needs_help(args, parsers)
    if wanted is None:
        return args, None
    wanted.print_help(run.stderr)
    return args, _USAGE_ERROR


def main(  # noqa: PLR0913 - the process, injected so a test can supply real ones
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> int:
    """Run `tau` with `argv` (default `sys.argv[1:]`); the other arguments default to
    the process's streams, environment, and working directory. Returns the exit status.

    The process's streams may be missing or go away, as any Unix program's may. A closed standard
    input (`<&-`) reads as empty; a closed standard error says nothing; a closed standard output
    is an error (status 1, said on standard error); and a standard output whose reader has gone
    (`tau -p ... | head -1`) ends the run quietly with status 141, 128 plus SIGPIPE, as a program
    killed by that signal would."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    if err is None:
        err = io.StringIO()
    if out is None:
        err.write("tau: standard output is closed\n")
        return 1
    try:
        given = sys.argv[1:] if argv is None else list(argv)
        source = sys.stdin if stdin is None else stdin
        run = _Run(
            io.StringIO("") if source is None else source,
            out,
            err,
            os.environ if environ is None else environ,
            Path.cwd() if cwd is None else cwd,
            given[1:],
        )
        status = _main(given, run)
        try:
            out.flush()
        except BrokenPipeError:
            raise
        except OSError as e:  # the last write failed (a full disk): the answer did not get out
            detach(out)
            return status or _fail(err, f"cannot write standard output: {e.strerror or e}")
    except BrokenPipeError:
        detach(out)
        return SIGPIPE_STATUS
    return status


def _main(given: Sequence[str], run: _Run) -> int:
    out, err = run.stdout, run.stderr
    args, status = _parse(run, given, _parser())
    if status is not None:
        return status
    try:
        return _dispatch(run, args)
    except (
        ConfigError,
        TrustError,
        DotenvError,
        ExtensionLoadError,
        UnknownSession,
        NoSuchNode,
        NotASession,
        Version,
        Corrupt,
        Undecodable,
        BadRecord,
        Leased,
        ProtocolError,
        ServeFailed,
    ) as e:
        return _failed(args, out, err, e)
    except UsageError as e:
        if args.command == "print" and args.json:
            out.write(error_line(e) + "\n")
        _fail(err, str(e))
        return _USAGE_ERROR
    except BrokenPipeError:  # not a failure of ours: `main` ends quietly
        raise
    except OSError as e:  # a session's files: missing, unreadable, or held (`Leased`)
        if args.command not in ("sessions", "export", "serve", "print", "tui"):
            raise
        return _failed(args, out, err, e)
    except KeyboardInterrupt:
        err.write("\ntau: interrupted\n")
        return _INTERRUPTED
