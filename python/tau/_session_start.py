"""The shared start (D18 section 5): what `tau`, `tau -p` and `tau serve` all do before the first
message, so they agree.

`start_session` takes the parsed flags (`tau.cli.session_flags` and `extension_flags` as parents)
and:

0. resolves the session flags to a new session, an existing one or a fork, writing nothing, with
   every relative path in the flags made absolute against `cwd`;
1. changes the process's working directory (`os.chdir`) to the session's `cwd`, so the base tools,
   the project search, trust, extensions and path completion all answer "where am I" alike, and
   for an existing session takes its lease at once (`Leased` is said before a trust question or
   an extension's import);
2. loads the config, loads the extensions, adds the built-in `session_name`, applies the run's
   `--set EXTENSION.FIELD=VALUE` options (the config's `cli` layer, checked against the loaded
   extensions' options), and for a new session selects the model (an existing session or a fork
   already records its model in its `Config` node, so `--model` is refused for them: `/model`
   switches a running session);
3. asks `system` for the text of the `System` node, for a new session only: by default
   `tau.system_prompt` (D20) with the prompt flags (`--append-system-prompt`, `-nc`;
   `--system-prompt` is a layer of the config, given to `load_config`), so an unreadable context
   file stops the start before a session exists; a program with its own prompt passes `system`,
   and then the prompt flags are refused: they would be ignored;
4. creates or forks the session, names an opened one (`-n`), builds the providers (each wrapped
   in a `RetryProvider` unless `load` turns `retry` off, D24 section 4) and starts the `Driver`.

The result is a `Started`, which keeps what `--verbose`'s start report tells; `stop` ends it, and
removes a session that was never used. A session another process holds is `Leased`, never replaced
by another one.
"""

import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tau._builtin import SessionName
from tau._choice import SessionChoice, choose_session
from tau._clock import SystemClock
from tau._config import ConfigError, config_fields, model_config
from tau._config_layers import option_problems
from tau._context import SystemPrompt, system_prompt
from tau._core import Body, Leased, Session
from tau._driver import Driver
from tau._extensions import (
    RETRY,
    ExtensionSource,
    Inspection,
    absolute_explicit,
    inspect_extensions,
)
from tau._options import make_layer_options
from tau._protocols import Clock, Extension, Provider
from tau._providers import builtin_providers
from tau._retry import RetryProvider
from tau._serve import Server, serve_session
from tau._serve_record import checked_socket, serve_directory, socket_path
from tau._session_settings import SessionSettings
from tau._sessions import (
    ForkedFrom,
    PickSession,
    SessionFlags,
    first_cursor_node,
    make_session_directory,
    read_name,
    rename_session,
    session_info,
)
from tau._startup import (
    LoadedConfig,
    Prompts,
    load_config,
    load_key_warnings,
    off_keys,
    option_changes,
    select_model,
)
from tau._trust import tau_home, user_config_path
from tau._values import Context, Json

SystemText = Callable[[Path, LoadedConfig], str]
"""What a new session's `System` node says: given the session's working directory and the loaded
config. Called once, for a new session only."""


class _Assembled:
    """D20's assembly, with the flags' appended sections and `-nc`; keeps what it assembled for
    the start report."""

    def __init__(self, flags: SessionFlags) -> None:
        self._flags = flags
        self.prompt: SystemPrompt | None = None

    def __call__(self, cwd: Path, loaded: LoadedConfig) -> str:
        self.prompt = system_prompt(
            cwd,
            loaded,
            append=self._flags.append_system_prompt,
            context_files=not self._flags.no_context_files,
        )
        return self.prompt.text


@dataclass
class Started:
    """A running session: its `session` and `driver` (started), the `providers` the program
    closes through `stop`, the `loaded` config and the `model` (selected for a new session, else
    the one recorded at the first cursor), the session's working directory `cwd` (the process is
    in it), the session's `directory` (`None`: in memory) and whether this run `created` it (a new
    session or a fork). `sources` are the extension sources the start planned, one per row of
    `tau ext list`, `warnings` what the config names that this start lacks (one line each;
    frontends print them), and `prompt` the parts of the system prompt it assembled (`None`:
    the session already had one, or the program supplied the text). A session named during the
    run counts as used."""

    session: Session
    driver: Driver
    providers: Mapping[str, Provider]
    loaded: LoadedConfig
    model: str
    cwd: Path
    directory: Path | None
    created: bool
    sources: tuple[ExtensionSource, ...]
    warnings: tuple[str, ...]
    prompt: SystemPrompt | None
    _nodes: int
    _name: str | None

    @property
    def never_used(self) -> bool:
        """This run created the session and it still has only the nodes it was created with, no
        pending item and the name it was created with."""
        if not self.created or self.directory is None or len(self.session.tree) != self._nodes:
            return False
        if read_name(self.directory) != self._name:
            return False
        return not any(self.session.cursor(c).items for c in self.session.cursors())

    async def serve(self, socket: Path | None = None, *, idle: float | None = None) -> "Server":
        """Serve this session as `tau serve` does, so `tau -c` in its working directory attaches
        (D21 section 9): bind `socket` (default `<home>/serve/<id>.sock`, in the private serve
        directory), and write the serve record `<home>/serve/<id>.json`, naming this process.
        The home, id and working directory are this `Started`'s. `idle` is `Server.start`'s.
        `Server.close` is the whole teardown: it stops the driver, unbinds the socket, calls
        `stop` and removes the record; do not call `stop` as well. `ConfigError` for a session
        in memory (`--no-session`: it has no id to name its files) or a socket path too long;
        `TrustError` if the serve directory is not a private directory of yours; `OSError` if
        the socket cannot be bound."""
        if self.directory is None:
            raise ConfigError(
                "a session in memory has no id to name a socket and a serve record: "
                "serve a kept session"
            )
        directory = serve_directory(self.loaded.home)
        session = self.directory.name
        path = socket_path(directory, session) if socket is None else checked_socket(socket)
        return await serve_session(
            self.driver,
            path.absolute(),
            session=session,
            cwd=self.cwd,
            home=self.loaded.home,
            release=self.stop,
            idle=idle,
        )

    async def stop(self) -> None:
        """Stop the driver; delete the session's directory if it was never used (while the lease
        is still held); release the lease; close the providers."""
        await self.driver.stop()
        try:
            if self.directory is not None and self.never_used:
                shutil.rmtree(self.directory)
        finally:
            self.session.close()
            await _close(self.providers)


def start_report(started: Started) -> list[str]:
    """What `--verbose` says before the first message, one line each: the session (its id and
    directory, or in memory; new, opened or forked), the working directory, the model, the
    config files read, each extension loaded with its source key and origin, and for a new
    session the parts of the system prompt with their sizes in bytes."""
    lines = [_session_line(started), f"working directory {started.cwd}", f"model {started.model}"]
    loaded = started.loaded
    files = [user_config_path(loaded.home)]
    if loaded.project is not None:
        root = loaded.project.root
        files += [root / ".tau" / "config.json"] * (loaded.project.config_text is not None)
        files += [root / ".env"] * (loaded.project.env_text is not None)
    lines += [f"config {path}" for path in files if path is not None]
    for source in started.sources:
        if source.state in ("on", "always"):
            where = (
                source.origin
                if source.origin.startswith(source.kind)
                else f"{source.kind}: {source.origin}"
            )
            if where.endswith(")"):  # "built-in (added by the startup)": no parentheses in them
                where = where.replace(" (", ", ")[:-1]
            lines.append(f"{'extension ' if source.extensions else ''}{source.key} ({where})")
    if started.prompt is not None:
        lines += [f"system prompt {part.size:>8} {part.origin}" for part in started.prompt.sources]
    return lines


def _session_line(started: Started) -> str:
    if started.directory is None:
        return "session in memory (new)"
    how = "opened"
    if started.created:
        forked = session_info(started.directory).forked_from
        how = "new" if forked is None else f"forked from {forked.session} at node {forked.node}"
    return f"session {started.directory.name} ({how}) in {started.directory}"


def _providers(loaded: LoadedConfig, sources: Sequence[ExtensionSource]) -> dict[str, Provider]:
    """The built-in providers, each wrapped in a `RetryProvider` unless `load` turns `retry` off."""
    providers = builtin_providers(loaded.environ)
    if not any(s.key == RETRY and s.state == "on" for s in sources):
        return providers
    return {name: RetryProvider(p) for name, p in providers.items()}


async def _close(providers: Mapping[str, Provider]) -> None:
    for provider in providers.values():
        await provider.aclose()


def _create(
    choice: SessionChoice, loaded: LoadedConfig, model: str, system: SystemText, now: int
) -> tuple[Session, Path | None]:
    """A new session, in a new directory unless the choice is `--no-session`."""
    text = system(choice.cwd, loaded)
    directory = None
    if choice.sessions is not None:
        directory = make_session_directory(choice.sessions, now, choice.cwd, choice.name, None)
    try:
        return Session.create(model_config(model), text, now, directory), directory
    except BaseException:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise


def _recorded_model(session: Session, node: int) -> str:
    """The model in force at `node`: the one the latest `Config` on its walk that names a model
    set (a `Config` that only sets an extension's options names none; a root always names one)."""
    current: int | None = node
    while current is not None:
        here = session.tree.node(current)
        if isinstance(here.body, Body.Config):
            recorded = config_fields(here.body.config).model
            if recorded is not None:
                return recorded
        current = here.parent
    raise ConfigError("the session's Config names no model")


def _lease(choice: SessionChoice) -> Session | None:
    """The existing session, its lease taken (`None` unless the choice is an existing session);
    `Leased` names the session and the way to a copy."""
    if choice.kind != "existing":
        return None
    assert choice.source is not None
    source = choice.source
    try:
        return Session.load(source.path)
    except Leased as e:
        raise Leased(
            f"session {source.id} is in use by another process; to work on a copy of it: "
            f"tau --fork {source.id}"
        ) from e


def _fork(choice: SessionChoice, now: int) -> tuple[Session, Path, str]:
    """A new session copying one walk of the source, which is read without its lease, and the
    model it records. Everything that can fail on the source fails before the directory is made."""
    assert choice.source is not None
    assert choice.sessions is not None
    source = choice.source
    copy = Session.read(source.path)
    try:
        node = first_cursor_node(copy) if choice.node is None else choice.node
        copy.tree.node(node)  # NoSuchNode, before anything is written
        model = _recorded_model(copy, node)
        origin = ForkedFrom(source.id, node)
        directory = make_session_directory(choice.sessions, now, choice.cwd, choice.name, origin)
        try:
            return copy.fork(node, now, directory), directory, model
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
    finally:
        copy.close()


def _driver(
    session: Session,
    loaded: LoadedConfig,
    extensions: Sequence[Extension],
    providers: Mapping[str, Provider],
    clock: Clock,
) -> Driver:
    driver = Driver(
        session,
        providers,
        loaded.catalog,
        extensions=extensions,
        user_config=loaded.user,
        clock=clock,
    )
    driver.start()
    return driver


def _session(  # noqa: PLR0913 - the pieces of one start
    choice: SessionChoice,
    held: Session | None,
    loaded: LoadedConfig,
    system: SystemText,
    *,
    selected: str | None,
    now: int,
) -> tuple[Session, Path | None, str]:
    """The session the choice is, with its directory and model: created (with the `selected`
    model), forked, or the `held` one, named if `-n` says so. What this creates is removed if it
    fails."""
    match choice.kind:
        case "new":
            assert selected is not None
            session, directory = _create(choice, loaded, selected, system, now)
            return session, directory, selected
        case "fork":
            return _fork(choice, now)
        case "existing":
            assert held is not None
            assert choice.source is not None
            selected = _recorded_model(held, first_cursor_node(held))
            if choice.name is not None:
                rename_session(choice.source.path, choice.name)
            return held, choice.source.path, selected


def _overridden(  # noqa: PLR0913, PLR0917 - the start's inputs, passed on
    loaded: LoadedConfig,
    cwd: Path,
    environ: Mapping[str, str],
    prompts: Prompts,
    settings: SessionSettings,
    cli: Mapping[str, object],
    extensions: Sequence[object],
) -> LoadedConfig:
    """`loaded` again with the `--set` options laid on the session layer `cli`: every value is
    read by its option's type, and every extension's options are built from the layers as they
    are now (`ConfigError` otherwise), so a bad `--set` fails before a session exists."""
    changes = option_changes(extensions, settings.options)
    layer = settings.with_options(cli, {name: values for name, (_, _, values) in changes.items()})
    again = load_config(cwd, environ, ask=prompts.ask, cli=layer)
    for name, (options, fields, _) in changes.items():
        raw: Json = dict(again.catalog.extensions.get(name, {}))
        make_layer_options(name, options, fields, raw, Context(again.catalog))
    return again


def _configured(  # noqa: PLR0913, PLR0917 - the start's inputs, passed on
    choice: SessionChoice,
    environ: Mapping[str, str],
    prompts: Prompts,
    flags: SessionFlags,
    settings: SessionSettings,
    own: Sequence[Extension],
    *,
    explicit: Sequence[str],
    discover: bool,
) -> tuple[LoadedConfig, Inspection, tuple[str, ...]]:
    """The layers (the session settings nearest), the extensions they load, and the start's
    warnings. `ConfigError` or `ExtensionLoadError` for what the start cannot go on with."""
    cli = {
        **({} if flags.system_prompt is None else {"system_prompt": flags.system_prompt}),
        **settings.layer,
    }
    loaded = load_config(choice.cwd, environ, ask=prompts.ask, cli=cli)
    found = inspect_extensions(loaded, explicit=explicit, discover=discover)
    found.raise_failures()
    settings.refuse_missing([s.key for s in found.sources if s.kind != "load"])
    every = [*own, *found.extensions]
    if settings.options:
        loaded = _overridden(loaded, choice.cwd, environ, prompts, settings, cli, every)
    refuse_bad_options(loaded, every)
    return loaded, found, start_warnings(loaded, found.sources, every)


def start_warnings(
    loaded: LoadedConfig, sources: Sequence[ExtensionSource], extensions: Sequence[object]
) -> tuple[str, ...]:
    """What the config names that this start does not have, one line each: a `load` key no
    source has, options of an extension that is not loaded or has none, a tool no loaded
    extension provides. Each is ignored; none stops the start."""
    found = load_key_warnings(loaded.catalog, sources)
    off = off_keys(sources)
    found += [
        p for p in option_problems(loaded.catalog, extensions, off) if p.severity == "warning"
    ]
    return tuple(str(p) for p in found)


def refuse_bad_options(loaded: LoadedConfig, extensions: Sequence[Extension]) -> None:
    """`ConfigError` naming every option the layers set that its extension refuses (a step limit
    of 0), so the start fails once instead of every step."""
    problems = [p for p in option_problems(loaded.catalog, extensions) if p.severity == "error"]
    if problems:
        raise ConfigError("\n".join(f"{p.path}: {p.message}" for p in problems))


def _refuse_ignored_prompt_flags(flags: SessionFlags, system: SystemText | None) -> None:
    """A program with its own `system` text would silently drop the flags that shape D20's."""
    if system is not None and (
        flags.system_prompt is not None or flags.append_system_prompt or flags.no_context_files
    ):
        raise ConfigError(
            "--system-prompt, --append-system-prompt and -nc shape the assembled system prompt, "
            "and this program supplies its own system text, so they would be ignored"
        )


def refuse_model(choice: SessionChoice, model: str | None) -> None:
    if choice.kind != "new" and model is not None:
        raise ConfigError(
            "--model names the model of a new session; this session records its own: "
            "switch a running session with /model"
        )


async def start_session(  # noqa: PLR0913 - one start, every input it has
    args: object,
    *,
    environ: Mapping[str, str],
    cwd: Path,
    system: SystemText | None = None,
    prompts: Prompts | None = None,
    pick_session: PickSession | None = None,
    own: Sequence[Extension] = (),
    model: str | None = None,
    clock: Clock | None = None,
) -> Started:
    """Start the session `args` (the parsed `session_flags()` and `extension_flags()`) choose, for
    a program started in `cwd` with `environ`, as the module says. `own` are the program's own
    extensions, ahead of the loaded ones; `model` is `--model`; `prompts` ask a person to trust a
    project or to pick a model; `pick_session` is what `-r` asks; `system` is the text of a new
    session's `System` node (default: D20's `tau.system_prompt`, from the prompt flags in `args`).

    Raises what the steps raise: `ConfigError`, `TrustError`, `ExtensionLoadError`,
    `UnknownSession`, `Leased`, `FileNotFoundError` for a session whose working directory is
    gone. Nothing is created when a step before the session's fails, and a session this call
    created is removed if a later step fails."""
    clock = clock or SystemClock()
    prompts = prompts or Prompts(None, None)
    flags = SessionFlags.of(args, cwd)
    _refuse_ignored_prompt_flags(flags, system)
    explicit = absolute_explicit(getattr(args, "extension"), cwd)  # noqa: B009
    discover = not getattr(args, "no_extensions")  # noqa: B009
    settings = SessionSettings.of(environ, getattr(args, "set_options", None) or ())
    choice = choose_session(flags, home=tau_home(environ), cwd=cwd, pick=pick_session)
    refuse_model(choice, model)
    os.chdir(choice.cwd)
    new = choice.kind == "new"
    held = _lease(choice)
    try:
        loaded, found, warnings = _configured(
            choice, environ, prompts, flags, settings, [*own], explicit=explicit, discover=discover
        )
        added = list(found.extensions)
        assembled = _Assembled(flags)
        text = system if system is not None else assembled
        selected = select_model(loaded.catalog, model, pick=prompts.pick) if new else None
        session, directory, selected = _session(
            choice, held, loaded, text, selected=selected, now=clock.now()
        )
    except BaseException:
        if held is not None:
            held.close()
        raise
    created = choice.kind != "existing"
    extensions = [*own, *added] + ([SessionName(directory)] if directory else [])
    providers: Mapping[str, Provider] = {}
    try:
        providers = _providers(loaded, found.sources)
        driver = _driver(session, loaded, extensions, providers, clock)
    except BaseException:
        session.close()
        if created and directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        await _close(providers)
        raise
    return Started(
        session,
        driver,
        providers,
        loaded,
        selected,
        choice.cwd,
        directory,
        created,
        found.sources,
        warnings,
        assembled.prompt,
        len(session.tree),
        None if directory is None else read_name(directory),
    )
