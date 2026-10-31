"""Commands (D16 section 2): plain functions an extension lists in `commands`, run as `/name`.

`Commands` holds the driver's commands and runs them for clients. A command that takes a
`Cursor` runs as a step of that cursor: `run` keeps it in memory and asks for the step
(`Session.step`), the driver gives it to `before` when the step's `pre_step` job is
dispatched, and it runs ahead of the extensions' `pre_step` hooks. Any other command runs at
once, as a task on the loop. The result of either is a `Notice` to the client that ran it, and
so is each `progress` call and a failure (`"Type: msg"`). A failed `Cursor` command also ends
its step `Failed`, as a failing hook does (the cursor's `hook_error` names `pre_step` and the
extension that lists the command).
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from functools import partial
from typing import cast

from tau_next._complete import complete
from tau_next._config_layers import ModelCatalog
from tau_next._core import Answer, Author, Busy, Session
from tau_next._hub import Hub
from tau_next._options import ExtensionOptions, OptionsForm
from tau_next.effects import describe_failure
from tau_next.functions import INJECT_CURSOR, INJECT_NODE, Described, describe, invoke
from tau_next.handles import Handles, TreeHandle
from tau_next.protocols import Clock, ExtensionContractError
from tau_next.tools import FunctionTool
from tau_next.values import Context, Json, check_object, convert_object, object_schema


@dataclass(frozen=True)
class CommandInfo:
    """What a client knows of a command: its `name` (typed as `/name`), `description` (the
    docstring), and `schema`, the JSON Schema of its arguments with the named types' `"format"`.
    Fixed for the life of the driver."""

    name: str
    description: str
    schema: Json


@dataclass(frozen=True)
class _Command:
    function: Callable[..., object]
    described: Described
    owner: str

    @property
    def on_cursor(self) -> bool:
        return self.described.takes(INJECT_CURSOR)

    @property
    def info(self) -> CommandInfo:
        return CommandInfo(
            self.described.name,
            self.described.description,
            object_schema(self.described.fields, model=False),
        )


def assemble_commands(extensions: Sequence[object]) -> dict[str, _Command]:
    """Each extension's `commands` attribute, a plain function (or a `FunctionTool`'s
    function) described as a command, by name. `ExtensionContractError` for a `commands` that
    is not a sequence of functions or a name two commands share; `ToolDefinitionError` for a
    function that cannot be described (a `NodeHandle` parameter is refused)."""
    found: dict[str, _Command] = {}
    for ext in extensions:
        owner = cast("str", getattr(ext, "name"))  # noqa: B009
        listed: object = getattr(ext, "commands", ())
        if not isinstance(listed, Sequence) or isinstance(listed, str):
            raise ExtensionContractError(f"extension {owner!r}: `commands` must be a sequence")
        for item in cast("Sequence[object]", listed):
            function = item.function if isinstance(item, FunctionTool) else item
            if not callable(function):
                raise ExtensionContractError(
                    f"extension {owner!r}: {item!r} in `commands` is not a function"
                )
            described = describe(
                function,
                what="command",
                refused={
                    INJECT_NODE: "a command has a TreeHandle (or a Cursor), not a batch's "
                    "NodeHandle; take a TreeHandle"
                },
            )
            if described.name in found:
                raise ExtensionContractError(
                    f"two commands are named {described.name!r} "
                    f"(extensions {found[described.name].owner!r} and {owner!r})"
                )
            found[described.name] = _Command(function, described, owner)
    return found


@dataclass(frozen=True)
class _Pending:
    """A `Cursor` command waiting for its step's `pre_step` job."""

    command: _Command
    bound: dict[str, object]
    client: str


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

    def infos(self) -> tuple[CommandInfo, ...]:
        """Each command's name, description and client schema."""
        return tuple(c.info for c in self._commands.values())

    def form(self, cursor: int, extension: str) -> OptionsForm:
        """`extension`'s options as a client edits them at `cursor`."""
        return self._options.form(extension, self._session.cursor(cursor).at)

    async def complete(self, cursor: int, named: str, prefix: str) -> list[str]:
        """The values of the named type `named` that start with `prefix`."""
        return await complete(self._session, self._catalog(), cursor, named, prefix)

    # Running

    def run(self, client: str, cursor: int, name: str, args: Mapping[str, object]) -> None:
        """Run command `name` for `client` on `cursor` with the decoded JSON `args`.
        `LookupError` for an unknown command; `ValueError` naming the problem for arguments
        that do not fit (checked against the session, `"new"` last); `Busy` for a command that
        takes a `Cursor` when the cursor is not at rest. Nothing is opened or started for a
        refused call."""
        command = self._commands.get(name)
        if command is None:
            raise LookupError(f"no command {name!r}; the commands are {list(self._commands)}")
        with self._frontend():
            self._session.cursor(cursor)
            context = Context(
                self._session, self._catalog(), partial(self._open, command.owner, cursor)
            )
            problem = check_object(command.described.fields, args, context)
            if problem is not None:
                raise ValueError(f"/{name}: {problem}")
            if command.on_cursor and not self._at_rest(cursor):
                raise Busy(f"cursor {cursor} is not at rest; /{name} runs as a step of it")
            bound = convert_object(command.described.fields, args, context)
            if not command.on_cursor:
                self._spawn(self._background(command, bound, client))
                return
            self._pending[cursor] = _Pending(command, bound, client)
            try:
                self._session.step(cursor)
            except BaseException:
                del self._pending[cursor]
                raise

    def _open(self, owner: str, cursor: int) -> int:
        """Open a cursor at `cursor`'s `at`, for an argument of `"new"`."""
        return self._session.open(
            self._session.cursor(cursor).at, Author.Extension(owner), self._clock.now()
        )

    def runner(self, cursor: int) -> str | None:
        """The client of the command waiting for `cursor`'s step, if one is."""
        pending = self._pending.get(cursor)
        return None if pending is None else pending.client

    def stop(self, cursor: int) -> None:
        """The cursor's step is aborted or left: a command still waiting for its `pre_step`
        is dropped (never run later), and one running is cancelled."""
        self._pending.pop(cursor, None)
        running = self._running.get(cursor)
        if running is not None:
            running.cancel()

    def clear(self) -> None:
        """The driver stopped: forget what was waiting."""
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
                error = await running
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise  # the driver is cancelling this job, not `stop`
                return Answer.Hooks(None, None)
            finally:
                del self._running[cursor]
                handle.retire()
            if error is None:
                return await hooks()
            self._hub.hook_failed(cursor, "pre_step", owner, error)
            return Answer.HookFailed(error)

        return effect

    async def _background(self, command: _Command, bound: dict[str, object], client: str) -> None:
        handle = self._handles.tree(command.owner, client)
        try:
            await self._call(command, bound, client, handle)
        finally:
            handle.retire()

    async def _call(
        self, command: _Command, bound: dict[str, object], client: str, handle: TreeHandle
    ) -> str | None:
        """Run the command with its injected parameters and tell `client` what came of it:
        the result, or the failure. Returns the failure (`"Type: msg"`), or `None`."""
        try:
            result = await invoke(
                command.function,
                command.described,
                bound,
                progress=self._say(client),
                handle=handle,
            )
            if result is not None and not isinstance(result, str):
                raise TypeError(f"{command.described.name} returned {result!r}, not a str or None")
        except Exception as e:
            error = describe_failure(e)
            self._say(client)(error)
            return error
        if result is not None:
            self._say(client)(result)
        return None

    def _say(self, client: str) -> Callable[[str], None]:
        """A `Notice` to `client`, after what the command has written so far is published."""

        def say(text: str) -> None:
            self._on_loop(partial(self._notify, client, text))()

        return say

    def _notify(self, client: str, text: str) -> None:
        self._hub.sync()
        self._hub.notify(client, text)
