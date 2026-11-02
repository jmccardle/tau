"""Commands (D16 section 2): plain functions an extension lists in `commands`, run as `/name`.

`Commands` holds the driver's commands and runs them for clients. A command that takes a
`Cursor` runs as a step of that cursor: `run` keeps it in memory and asks for the step
(`Session.step`), the driver gives it to `before` when the step's `pre_step` job is
dispatched, and it runs ahead of the extensions' `pre_step` hooks. Any other command runs at
once, as a task on the loop. `run` answers a future: the command's result (a `str` or `None`),
or `CommandFailed`, which carries the failure as `"Type: msg"`; a failed `Cursor` command also
ends its step `Failed`, as a failing hook does (the cursor's `hook_error` names `pre_step` and
the extension that lists the command). The future is answered when the command returns or
raises, not when its step ends. Each `progress` call of a command is a `Notice` to the client
that ran it.

`/config` is not a function: its values are a mapping, which the type language has no hint
for. `configure` runs the built-in `config` the same way a `Cursor` command runs.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from functools import partial
from typing import cast

from tau import _builtin
from tau._complete import complete
from tau._config import ConfigError
from tau._config_layers import ModelCatalog
from tau._core import Answer, Author, Busy, Session
from tau._effects import describe_failure
from tau._functions import INJECT_CURSOR, INJECT_NODE, Described, describe, invoke
from tau._handles import Handles, TreeHandle
from tau._hub import Hub
from tau._options import ExtensionOptions
from tau._protocols import Clock
from tau._values import (
    Context,
    DefinitionError,
    Form,
    Json,
    check_object,
    convert_object,
    object_schema,
)

CONFIG = "config"
"""The name `/config` has when some extension has options (a built-in, not a function)."""


def ambiguous(name: str, qualified: Sequence[str]) -> str:
    """The message for a bare `/name` that several extensions' commands share."""
    count = {2: "two"}.get(len(qualified), str(len(qualified)))
    return f"/{name} is {count} commands: " + ", ".join(f"/{q}" for q in qualified)


_ABORTED = "Aborted: the cursor's step was aborted or the cursor was moved"


class CommandFailed(RuntimeError):
    """A command raised, or returned what is not a `str` or `None`. The message is the failure
    as `"Type: msg"`. `Client.run` raises it as soon as the command raises; an aborted one raises
    it too."""


def _settle(
    future: "asyncio.Future[str | None]", result: str | None = None, error: Exception | None = None
) -> None:
    """Answer `future`, unless its caller stopped waiting."""
    if future.done():
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(result)


@dataclass(frozen=True)
class _Command:
    function: Callable[..., object]
    described: Described
    owner: str

    @property
    def on_cursor(self) -> bool:
        return self.described.takes(INJECT_CURSOR)

    @property
    def form(self) -> Form:
        return Form(
            self.described.name,
            self.described.description,
            object_schema(self.described.fields, model=False),
            self.owner,
        )

    @property
    def qualified(self) -> str:
        return f"{self.owner}:{self.described.name}"


def assemble_commands(extensions: Sequence[object]) -> dict[str, _Command]:
    """Each extension's `commands` attribute, a plain function described as a command, by
    qualified name (`extension:name`). Two extensions may list one name: the bare `/name` is
    then ambiguous (`Commands.run`). `DefinitionError` for a `commands` that is not a sequence of
    functions, a name two commands of one extension share or `config` (the built-in `/config` is
    named so), or a function that cannot be described (a `NodeHandle` parameter is refused)."""
    found: dict[str, _Command] = {}
    for ext in extensions:
        owner = cast("str", getattr(ext, "name"))  # noqa: B009
        listed: object = getattr(ext, "commands", ())
        if not isinstance(listed, Sequence) or isinstance(listed, str):
            raise DefinitionError(f"extension {owner!r}: `commands` must be a sequence")
        for function in cast("Sequence[object]", listed):
            if not callable(function):
                raise DefinitionError(
                    f"extension {owner!r}: {function!r} in `commands` is not a function"
                )
            described = describe(
                function,
                what="command",
                extension=owner,
                refused={
                    INJECT_NODE: "a command has a TreeHandle (or a Cursor), not a batch's "
                    "NodeHandle; take a TreeHandle"
                },
            )
            if described.name == CONFIG:
                raise DefinitionError(
                    f"extension {owner!r}: a command cannot be named {CONFIG!r}, the built-in "
                    "/config's name"
                )
            command = _Command(function, described, owner)
            if command.qualified in found:
                raise DefinitionError(
                    f"two commands are named {described.name!r} (extension {owner!r})"
                )
            found[command.qualified] = command
    return found


@dataclass(frozen=True)
class _Pending:
    """A `Cursor` command waiting for its step's `pre_step` job."""

    command: _Command
    bound: dict[str, object]
    client: str
    future: "asyncio.Future[str | None]"


class Commands:
    """The driver's commands, and running them for clients (the module docstring)."""

    def __init__(  # noqa: PLR0913 - what running a command needs of the driver
        self,
        commands: Mapping[str, _Command],
        *,
        session: Session,
        catalog: Callable[[], ModelCatalog],
        options: ExtensionOptions,
        handles: Handles,
        hub: Hub,
        clock: Clock,
        frontend: Callable[[], AbstractContextManager[None]],
        at_rest: Callable[[int], bool],
        spawn: Callable[[Coroutine[object, object, None]], None],
        on_loop: Callable[[Callable[[], None]], Callable[[], None]],
    ) -> None:
        self._commands = dict(commands)
        self._session = session
        self._catalog = catalog
        self._options = options
        self._hub = hub
        self._clock = clock
        self._frontend = frontend
        self._at_rest = at_rest
        self._spawn = spawn
        self._handles = handles
        self._on_loop = on_loop  # a sync command's `progress` is called off the loop's thread
        self._pending: dict[int, _Pending] = {}
        self._running: dict[int, asyncio.Future[str | None]] = {}
        config = _builtin.config
        self._config = _Command(
            config,
            Described(CONFIG, cast("str", config.__doc__), (), {"cursor": INJECT_CURSOR}, True),
            _builtin.NAME,
        )

    def use(self, commands: Mapping[str, _Command]) -> None:
        """Run `commands` (`assemble_commands`) from now on."""
        self._commands = dict(commands)

    def forms(self) -> tuple[Form, ...]:
        """Each command's form, and `/config`'s if some extension has options: its one
        parameter is the extension, and `options` has the rest."""
        forms = tuple(c.form for c in self._commands.values())
        names = self._options.names()
        if not names:
            return forms
        schema: Json = {
            "type": "object",
            "properties": {"extension": {"type": "string", "enum": list(names)}},
            "required": ["extension"],
            "additionalProperties": False,
        }
        return (*forms, Form(CONFIG, self._config.described.description, schema))

    def options(self, cursor: int, extension: str) -> Form:
        """`extension`'s options as a client edits them at `cursor`."""
        return self._options.form(extension, self._session.cursor(cursor).at)

    def option_forms(self, cursor: int) -> dict[str, Form]:
        """The options form of every extension that has options, as `cursor` sees them: what
        `commandline.parse` reads `/config <extension>` by."""
        at = self._session.cursor(cursor).at
        return {name: self._options.form(name, at) for name in self._options.names()}

    async def complete(self, cursor: int, named: str, prefix: str) -> list[str]:
        """The values of the named type `named` that start with `prefix`."""
        return await complete(self._session, self._catalog(), cursor, named, prefix)

    # Running

    def run(
        self, client: str, cursor: int, name: str, args: Mapping[str, object]
    ) -> "asyncio.Future[str | None]":
        """Start command `name` for `client` on `cursor` with the decoded JSON `args`; the
        future is its outcome (the module docstring). `LookupError` for an unknown command, or a
        bare name that several extensions' commands share (`/note is two commands: /a:note,
        /b:note`);
        `ValueError` naming the problem for arguments that do not fit (checked against the
        session, `"new"` last), or for `config`; `Busy` for a command that takes a `Cursor`
        when the cursor is not at rest. Nothing is opened or started for a refused call."""
        if name == CONFIG and self._options.names():
            raise ValueError(f"/{CONFIG} is not run with arguments: use configure")
        command = self._named(name)
        with self._frontend():
            self._session.cursor(cursor)
            context = Context(
                self._catalog(),
                self._session,
                partial(self._open, command.owner, cursor),
            )
            problem = check_object(command.described.fields, args, context)
            if problem is not None:
                raise ValueError(f"/{name}: {problem}")
            if command.on_cursor:
                self._need_rest(cursor, name)
            bound = convert_object(command.described.fields, args, context)
            return self._start(command, bound, client, cursor)

    def _named(self, name: str) -> _Command:
        """The command `/name` is: a qualified `extension:name` exactly, a bare `name` if one
        extension has it. `LookupError` for none, or for several (naming each)."""
        if ":" in name:
            found = [self._commands[name]] if name in self._commands else []
        else:
            found = [c for c in self._commands.values() if c.described.name == name]
        if not found:
            raise LookupError(f"no command {name!r}; the commands are {list(self._commands)}")
        if len(found) > 1:
            raise LookupError(ambiguous(name, [c.qualified for c in found]))
        return found[0]

    def configure(
        self,
        client: str,
        cursor: int,
        extension: str,
        values: Mapping[str, object],
        save: bool,
    ) -> "asyncio.Future[str | None]":
        """Start `/config`: change `extension`'s options on `cursor`'s branch to `values` (JSON),
        as a step of the cursor; with `save`, write them into the user's file first.
        `ConfigError` for an extension without options or values its options type refuses (or,
        with `save`, no user config to write), `Busy` unless the cursor is at rest; nothing is
        written then."""
        with self._frontend():
            at = self._session.cursor(cursor).at
            self._need_rest(cursor, CONFIG)
            if extension not in self._options.names():
                raise ConfigError(f"extension {extension!r} has no options")
            self._options.check_config(at, None, {extension: values})
            if save:
                self._options.save(extension, values)
            bound: dict[str, object] = {"extension": extension, "values": dict(values)}
            return self._start(self._config, bound, client, cursor)

    def _need_rest(self, cursor: int, name: str) -> None:
        if not self._at_rest(cursor):
            raise Busy(f"cursor {cursor} is not at rest; /{name} runs as a step of it")

    def _start(
        self, command: _Command, bound: dict[str, object], client: str, cursor: int
    ) -> "asyncio.Future[str | None]":
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        if not command.on_cursor:
            self._spawn(self._background(command, bound, client, future))
            return future
        self._pending[cursor] = _Pending(command, bound, client, future)
        try:
            self._session.step(cursor)
        except BaseException:
            del self._pending[cursor]
            raise
        return future

    def _open(self, owner: str, cursor: int) -> int:
        """Open a cursor at `cursor`'s `at`, for an argument of `"new"`."""
        return self._session.open(
            self._session.cursor(cursor).at, Author.Extension(owner), self._clock.now()
        )

    def stop(self, cursor: int) -> None:
        """The cursor's step is aborted or left: a command still waiting for its `pre_step`
        is dropped (never run later), and one running is cancelled. Their `run` fails."""
        pending = self._pending.pop(cursor, None)
        if pending is not None:
            _settle(pending.future, error=CommandFailed(_ABORTED))
        running = self._running.get(cursor)
        if running is not None:
            running.cancel()

    def clear(self) -> None:
        """The driver stopped or failed: forget what was waiting; nobody will answer a `run`
        that was waiting (it is cancelled, and the client call says why)."""
        for pending in self._pending.values():
            pending.future.cancel()
        self._pending.clear()

    def before(
        self, cursor: int, hooks: Callable[[], Awaitable[Answer]]
    ) -> Callable[[], Awaitable[Answer]]:
        """The effect of the `pre_step` job of `cursor`: `hooks`, preceded by the `Cursor`
        command waiting for this step, if there is one. A command that fails ends the step
        `Failed` before any hook; one that is cancelled (`stop`) answers for the job without
        the hooks."""
        pending = self._pending.pop(cursor, None)
        if pending is None:
            return hooks

        async def effect() -> Answer:
            owner = pending.command.owner
            handle = self._handles.cursor(cursor, owner)
            running = asyncio.ensure_future(
                self._call(pending.command, pending.bound, pending.client, handle)
            )
            self._running[cursor] = running
            try:
                result = await running
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    pending.future.cancel()
                    raise  # the driver is cancelling this job, not `stop`
                _settle(pending.future, error=CommandFailed(_ABORTED))
                return Answer.Hooks(None, None)
            except Exception as e:
                error = describe_failure(e)
                self._hub.hook_failed(cursor, "pre_step", owner, error)
                _settle(pending.future, error=CommandFailed(error))
                return Answer.HookFailed(error)
            finally:
                del self._running[cursor]
                handle.retire()
            _settle(pending.future, result)
            return await hooks()

        return effect

    async def _background(
        self,
        command: _Command,
        bound: dict[str, object],
        client: str,
        future: "asyncio.Future[str | None]",
    ) -> None:
        handle = self._handles.tree(command.owner)
        try:
            _settle(future, await self._call(command, bound, client, handle))
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as e:
            _settle(future, error=CommandFailed(describe_failure(e)))
        finally:
            handle.retire()

    async def _call(
        self, command: _Command, bound: dict[str, object], client: str, handle: TreeHandle
    ) -> str | None:
        """Run the command with its injected parameters; its result, which must be a `str` or
        `None`. Anything it raises is for the caller."""
        result = await invoke(
            command.function,
            command.described,
            bound,
            progress=self._say(client),
            handle=handle,
        )
        if result is not None and not isinstance(result, str):
            raise TypeError(f"{command.described.name} returned {result!r}, not a str or None")
        return result

    def _say(self, client: str) -> Callable[[str], None]:
        """A `Notice` to `client`, after what the command has written so far is published."""

        def say(text: str) -> None:
            self._on_loop(partial(self._notify, client, text))()

        return say

    def _notify(self, client: str, text: str) -> None:
        self._hub.sync()
        self._hub.notify(client, text)
