"""The `tau` command: `tau config check [--show MODEL]`, `tau config set EXTENSION.FIELD=VALUE
...`, `tau trust [DIR]` (D13, D1) and `tau ext list|enable|disable` (D19).

`main` takes its streams, environment, and working directory as arguments so a test
drives it with real files and real streams. Exit status: 0 success, 1 a problem
(printed as one message on stderr, never a traceback), 2 a usage error, 130 interrupted.
Everything printed from config data goes through `safe_text`. `config set` and `ext enable` /
`disable` write the user's `<TAU_HOME>/config.json` and never the project's `.tau/config.json`.
`extension_flags` is the `-e` and `--no-extensions` parser a program that starts a session adds
(`parents=[extension_flags()]`), and gives `load_extensions` (D19 section 5).
"""

import argparse
import asyncio
import json
import os
import shlex
import shutil
import sys
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
from tau._dotenv import DotenvError
from tau._extensions import (
    ExtensionLoadError,
    ExtensionSource,
    absolute_explicit,
    inspect_extensions,
    list_extensions,
    switch_source,
)
from tau._options import option_fields, save_options
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
from tau.protocols import Extension, Provider
from tau.providers import builtin_providers

_USAGE_ERROR = 2
_INTERRUPTED = 130
_UNTRUSTED = " [untrusted project: not used until `tau trust`]"


@dataclass(frozen=True)
class _Run:
    stdin: TextIO
    stdout: TextIO
    environ: Mapping[str, str]
    cwd: Path


def _assignment(text: str) -> tuple[str, str, str]:
    """`EXTENSION.FIELD=VALUE` as its three parts."""
    target, equals, given = text.partition("=")
    extension, dot, name = target.partition(".")
    if not (equals and dot and extension and name):
        raise argparse.ArgumentTypeError(f"{text!r} is not EXTENSION.FIELD=VALUE")
    return extension, name, given


def extension_flags() -> argparse.ArgumentParser:
    """A parser (`add_help=False`, for `parents=`) with the flags that choose extensions:
    `-e VALUE` (repeatable; a path, or the key of a discovered source) and `--no-extensions`.
    Pass `absolute_explicit(args.extension, cwd)` and `discover=not args.no_extensions` to
    `load_extensions`."""
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
        help="load no installed package or drop-in (built-ins follow `load`; -e still loads)",
    )
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


def _fail(err: TextIO, message: str) -> int:
    err.write("tau: " + safe_block(message) + "\n")
    return 1


def _dispatch(run: _Run, args: argparse.Namespace) -> int:
    if args.command == "trust":
        return _trust(run, args.dir)
    if args.command == "ext":
        if args.ext_command == "list":
            return _ext_list(run, args)
        return _ext_switch(run, args.key, args.ext_command == "enable")
    if args.config_command == "set":
        return _set(run, args.assignments)
    return _check(run, args.show)


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
    run = _Run(
        sys.stdin if stdin is None else stdin,
        out,
        os.environ if environ is None else environ,
        Path.cwd() if cwd is None else cwd,
    )
    parser, config, ext = _parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if args.command is None:
        parser.print_help(err)
        return _USAGE_ERROR
    if args.command == "config" and args.config_command is None:
        config.print_help(err)
        return _USAGE_ERROR
    if args.command == "ext" and args.ext_command is None:
        ext.print_help(err)
        return _USAGE_ERROR
    try:
        return _dispatch(run, args)
    except (ConfigError, TrustError, DotenvError, ExtensionLoadError) as e:
        return _fail(err, str(e))
    except KeyboardInterrupt:
        err.write("\ntau: interrupted\n")
        return _INTERRUPTED
