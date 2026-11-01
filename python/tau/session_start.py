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
2. loads the config, loads the extensions, adds the built-in `session_name`, and for a new session
   selects the model (an existing session or a fork already records its model in its `Config` node,
   so `--model` is refused for them: `/model` switches a running session);
3. asks `system` for the text of the `System` node, for a new session only: by default
   `tau.system_prompt` (D20) with the prompt flags (`--append-system-prompt`, `-nc`;
   `--system-prompt` is a layer of the config, given to `load_config`), so an unreadable context
   file stops the start before a session exists; a program with its own prompt passes `system`,
   and then the prompt flags are refused: they would be ignored;
4. creates or forks the session, names an opened one (`-n`), builds the providers and starts the
   `Driver`.

The result is a `Started`; `stop` ends it, and removes a session that was never used. A session
another process holds is `Leased`, never replaced by another one.
"""

import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tau._builtin import SessionName
from tau._choice import SessionChoice, choose_session
from tau._config import ConfigError, config_fields, model_config
from tau._core import Leased, Session
from tau._extensions import absolute_explicit, load_extensions
from tau._startup import LoadedConfig, Prompts, load_config, select_model
from tau._trust import tau_home
from tau.clock import SystemClock
from tau.context import system_prompt
from tau.driver import Driver
from tau.protocols import Clock, Extension, Provider
from tau.providers import builtin_providers
from tau.sessions import (
    ForkedFrom,
    PickSession,
    SessionFlags,
    first_cursor_node,
    make_session_directory,
    read_name,
    rename_session,
)

SystemText = Callable[[Path, LoadedConfig], str]
"""What a new session's `System` node says: given the session's working directory and the loaded
config. Called once, for a new session only."""


def _assembled(flags: SessionFlags) -> SystemText:
    """D20's assembly, with the flags' appended sections and `-nc`."""

    def assemble(cwd: Path, loaded: LoadedConfig) -> str:
        return system_prompt(
            cwd,
            loaded,
            append=flags.append_system_prompt,
            context_files=not flags.no_context_files,
        ).text

    return assemble


@dataclass
class Started:
    """A running session: its `session` and `driver` (started), the `providers` the program
    closes through `stop`, the `loaded` config and the `model` (selected for a new session, else
    the one recorded at the first cursor), the session's working directory `cwd` (the process is
    in it), the session's `directory` (`None`: in memory) and whether this run `created` it (a new
    session or a fork). A session named during the run counts as used."""

    session: Session
    driver: Driver
    providers: Mapping[str, Provider]
    loaded: LoadedConfig
    model: str
    cwd: Path
    directory: Path | None
    created: bool
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
    """The model the `Config` in force at `node` names (a session's root always names one)."""
    recorded = config_fields(session.tree.config_at(node)).model
    if recorded is None:
        raise ConfigError("the session's Config names no model")
    return recorded


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


def _refuse_ignored_prompt_flags(flags: SessionFlags, system: SystemText | None) -> None:
    """A program with its own `system` text would silently drop the flags that shape D20's."""
    if system is not None and (
        flags.system_prompt is not None or flags.append_system_prompt or flags.no_context_files
    ):
        raise ConfigError(
            "--system-prompt, --append-system-prompt and -nc shape the assembled system prompt, "
            "and this program supplies its own system text, so they would be ignored"
        )


def _refuse_model(choice: SessionChoice, model: str | None) -> None:
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
    choice = choose_session(flags, home=tau_home(environ), cwd=cwd, pick=pick_session)
    _refuse_model(choice, model)
    os.chdir(choice.cwd)
    new = choice.kind == "new"
    held = _lease(choice)
    try:
        cli = None if flags.system_prompt is None else {"system_prompt": flags.system_prompt}
        loaded = load_config(choice.cwd, environ, ask=prompts.ask, cli=cli)
        added = load_extensions(loaded, explicit=explicit, discover=discover)
        text = system if system is not None else _assembled(flags)
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
        providers = builtin_providers(loaded.environ)
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
        len(session.tree),
        None if directory is None else read_name(directory),
    )
