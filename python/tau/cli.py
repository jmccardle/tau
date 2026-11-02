"""The `tau` command: `tau config check [--show MODEL]`, `tau config set EXTENSION.FIELD=VALUE
...`, `tau trust [DIR]` (D13, D1), `tau ext list|enable|disable` (D19) and `tau sessions
[--all] [--json]`, `tau sessions name REF [NAME]` and `tau export REF` (D18), `tau context
[--show]` (D20) and `tau serve` (D21; `tau._serve_cli`). Starting a session for a person
(`tau`, `tau -p`) is Phase 8's; `tau._session_start.start_session` is their shared start.

`main` takes its streams, environment, and working directory as arguments so a test
drives it with real files and real streams. Exit status: 0 success, 1 a problem
(printed as one message on stderr, never a traceback), 2 a usage error, 130 interrupted.
Everything printed from config data goes through `safe_text`. `config set` and `ext enable` /
`disable` write the user's `<TAU_HOME>/config.json` and never the project's `.tau/config.json`.
`extension_flags` is the `-e` and `--no-extensions` parser a program that starts a session adds
(`parents=[extension_flags()]`), and gives `load_extensions` (D19 section 5); `session_flags` is
the one for the session (`-c`, `-r`, `--session`, `--fork`, `--no-session`, `-n`, `--session-dir`;
D18 section 5), which `start_session` reads; it carries the prompt flags (`--system-prompt`,
`--append-system-prompt`, `-nc`; D20) too.
"""

import argparse
import asyncio
import json
import os
import shlex
import shutil
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TextIO, cast

from tau._config import ConfigError
from tau._config_layers import (
    ConfigProblem,
    LayerText,
    ModelCatalog,
    check_config,
    option_problems,
    read_models,
)
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
from tau._markdown import to_markdown
from tau._options import option_fields, save_options
from tau._protocol import ProtocolError
from tau._protocols import Extension, Provider
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
from tau._startup import LoadedConfig, load_config, prompts, select_model
from tau._trust import (
    TAU_DIR,
    TrustError,
    UntrustedProject,
    create_project,
    ensure_trusted,
    find_project,
    project_environment,
    refuse_special,
    safe_block,
    safe_text,
    tau_home,
    user_config_path,
)
from tau._user_config import UserConfig
from tau._values import Field, Json, object_schema
from tau.commandline import value

_USAGE_ERROR = 2
_INTERRUPTED = 130
_UNTRUSTED = " [untrusted project: not used until `tau trust`]"


@dataclass(frozen=True)
class _Run:
    stdin: TextIO
    stdout: TextIO
    environ: Mapping[str, str]
    cwd: Path
    argv: Sequence[str]  # after the command's name


def _assignment(text: str) -> tuple[str, str, str]:
    """`EXTENSION.FIELD=VALUE` as its three parts."""
    target, equals, given = text.partition("=")
    extension, dot, name = target.partition(".")
    if not (equals and dot and extension and name):
        raise argparse.ArgumentTypeError(f"{text!r} is not EXTENSION.FIELD=VALUE")
    return extension, name, given


def extension_flags() -> argparse.ArgumentParser:
    """A parser (`add_help=False`, for `parents=`) with the flags that choose extensions:
    `-e VALUE` (repeatable; a path, or the key of a source: a discovered one or a built-in) and
    `--no-extensions`.
    `tau.start_session` reads them from the parsed args."""
    flags = argparse.ArgumentParser(add_help=False)
    flags.add_argument(
        "-e",
        "--extension",
        action="append",
        default=[],
        metavar="PATH|KEY",
        help="load this extension file, package directory, or source key even if it is off "
        "(repeatable); naming it is the consent",
    )
    flags.add_argument(
        "--no-extensions",
        action="store_true",
        help="skip every discovered extension source except -e: no installed package or drop-in "
        "(the built-ins, base_tools and step_limit, still follow `load`)",
    )
    return flags


def _session_dir_flag(*, nested: bool = False) -> argparse.ArgumentParser:
    """`--session-dir DIR`, the sessions directory for one run (default: `$TAU_HOME/sessions`).
    `nested`: for a subcommand of a command that has it too, which must not reset it."""
    flags = argparse.ArgumentParser(add_help=False)
    flags.add_argument(
        "--session-dir",
        metavar="DIR",
        default=argparse.SUPPRESS if nested else None,
        help="keep and find sessions in DIR (default: $TAU_HOME/sessions)",
    )
    return flags


def _prompt_flags() -> argparse.ArgumentParser:
    """`--system-prompt TEXT`, `--append-system-prompt TEXT` (repeatable) and `-nc` (D20)."""
    flags = argparse.ArgumentParser(add_help=False)
    flags.add_argument(
        "--system-prompt",
        metavar="TEXT",
        help="the base text of the system prompt, in place of the built-in one (use "
        '"$(cat FILE)" for a file; empty: no base text)',
    )
    flags.add_argument(
        "--append-system-prompt",
        action="append",
        default=[],
        metavar="TEXT",
        help="add a section after the base text (repeatable)",
    )
    flags.add_argument(
        "-nc",
        "--no-context-files",
        action="store_true",
        help="leave out AGENTS.md, CLAUDE.md and .tau/SYSTEM.md",
    )
    return flags


def session_flags() -> argparse.ArgumentParser:
    """A parser (`add_help=False`, for `parents=`) with the flags that choose the session
    (D18 section 5): `-c/--continue`, `-r/--resume`, `--session REF`, `--fork REF` and
    `--no-session` (at most one), `-n/--name NAME` and `--session-dir DIR`; and the prompt flags
    of a new session (D20): `--system-prompt TEXT`, `--append-system-prompt TEXT` (repeatable) and
    `-nc/--no-context-files`. Pass the parsed arguments to `start_session`, which makes paths
    absolute and rejects combinations."""
    flags = argparse.ArgumentParser(add_help=False, parents=[_session_dir_flag(), _prompt_flags()])
    flags.add_argument(
        "-c",
        "--continue",
        dest="continue_last",
        action="store_true",
        help="open the newest session of this directory",
    )
    flags.add_argument("-r", "--resume", action="store_true", help="choose a session to open")
    flags.add_argument(
        "--session", metavar="REF", help="open this session: a path, an id, a name or an id prefix"
    )
    flags.add_argument(
        "--fork",
        metavar="REF",
        help="start a new session as a copy of one walk of REF (REF:NODE for a node)",
    )
    flags.add_argument(
        "--no-session", action="store_true", help="keep the session in memory: never listed"
    )
    flags.add_argument("-n", "--name", metavar="NAME", help="name the session")
    return flags


def _parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser, argparse.ArgumentParser]:
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
        type=_assignment,
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
        "sessions", parents=[_session_dir_flag()], help="list sessions; `sessions name` names one"
    )
    sessions.add_argument("--all", action="store_true", help="every directory, with its cwd")
    sessions.add_argument(
        "--json", action="store_true", help="one JSON object per session, per line"
    )
    sessions_sub = sessions.add_subparsers(dest="sessions_command", metavar="{name}")
    named = sessions_sub.add_parser(
        "name", parents=[_session_dir_flag(nested=True)], help="show or set a session's name"
    )
    named.add_argument("ref", metavar="REF", help="a path, an id, a name or an id prefix")
    named.add_argument("name", nargs="?", metavar="NAME", help="the new name; empty removes it")
    export = sub.add_parser(
        "export", parents=[_session_dir_flag()], help="write a session's walk as Markdown"
    )
    export.add_argument("ref", metavar="REF", help="SESSION or SESSION:NODE")
    export.add_argument("-o", "--output", metavar="FILE", help="write FILE (default: standard out)")
    export.add_argument("--thinking", action="store_true", help="include the model's thinking")
    context = sub.add_parser(
        "context", parents=[_prompt_flags()], help="what a new session here would tell the model"
    )
    context.add_argument(
        "--show", action="store_true", help="print the system prompt instead of its parts"
    )
    served = sub.add_parser(
        "serve",
        parents=[session_flags(), extension_flags()],
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
    return parser, config, ext


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
    known = {cast("str", getattr(e, "name")): e for e in extensions.extensions}  # noqa: B009
    saving: dict[str, tuple[type, tuple[Field, ...], Json]] = {}
    for extension, name, given in assignments:
        options = getattr(known.get(extension), "options", None)
        if options is None:
            raise ConfigError(
                f"extension {extension!r} has no options; those that do: {_with_options(known)}"
            )
        fields = option_fields(extension, options)
        properties = cast("Json", object_schema(fields, model=False)["properties"])
        if name not in properties:
            raise ConfigError(
                f"extension {extension!r} has no option {name!r}; its options: {list(properties)}"
            )
        try:
            decoded = value(given, cast("Json", properties[name]))
        except ValueError as e:
            raise ConfigError(f"{extension}.{name}: {e}") from None
        saving.setdefault(extension, (options, fields, {}))[2][name] = decoded
    save_options(UserConfig(loaded.home), saving)
    shown = " ".join(f"{e}.{n}={g}" for e, n, g in assignments)
    run.stdout.write(safe_text(f"set {shown} in {loaded.home / 'config.json'}") + "\n")
    return 0


def _with_options(known: Mapping[str, Extension]) -> list[str]:
    return [name for name, e in known.items() if getattr(e, "options", None) is not None]


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


def _show(catalog: ModelCatalog, name: str, out: TextIO) -> None:
    config = catalog.resolve(name)
    values = {
        "model": config.model,
        "base_url": config.base_url,
        "max_tokens": config.max_tokens,
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


def _dispatch(run: _Run, args: argparse.Namespace) -> int:
    match args.command:
        case "trust":
            return _trust(run, args.dir)
        case "context":
            return _context(run, args)
        case "serve":
            return serve(run, args)
        case "sessions" | "export":
            return _session_command(run, args)
        case "ext":
            return _ext_command(run, args)
        case _:
            return _config_command(run, args)


def _needs_help(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    config: argparse.ArgumentParser,
    ext: argparse.ArgumentParser,
) -> argparse.ArgumentParser | None:
    """The parser whose help to print because the command line stops at a group of commands."""
    if args.command is None:
        return parser
    if args.command == "config" and args.config_command is None:
        return config
    if args.command == "ext" and args.ext_command is None:
        return ext
    return None


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
    the process's streams, environment, and working directory. Returns the exit status."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    given = sys.argv[1:] if argv is None else list(argv)
    run = _Run(
        sys.stdin if stdin is None else stdin,
        out,
        os.environ if environ is None else environ,
        Path.cwd() if cwd is None else cwd,
        given[1:],
    )
    parser, config, ext = _parser()
    args = parser.parse_args(given)
    wanted = _needs_help(args, parser, config, ext)
    if wanted is not None:
        wanted.print_help(err)
        return _USAGE_ERROR
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
    except OSError as e:  # a session's files: missing, unreadable, or held (`Leased`)
        if args.command not in ("sessions", "export", "serve"):
            raise
        return _failed(args, out, err, e)
    except KeyboardInterrupt:
        err.write("\ntau: interrupted\n")
        return _INTERRUPTED
