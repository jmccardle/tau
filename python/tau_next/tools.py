"""Tools defined from Python functions: `tool(fn)` turns a function into a `Tool`, and an
extension may simply list plain functions in its `tools` attribute (`assemble_tools`).

The name is the function's, the description its docstring, and the parameters a JSON Schema
built from its type hints (`tau_next.values`; a parameter is described with
`Annotated[T, "text"]`). Everything that cannot be turned into a schema fails when the
function is described (`ToolDefinitionError`); a call whose arguments do not fit the
signature answers `Err(message)`, which the model reads.

Some parameters are the driver's, found by annotation, and are not arguments: a `Progress`
(or a parameter named `progress`) is the callback for showing partial output as the function
runs, which may be called from any thread; a `TreeHandle` or `NodeHandle` is the batch's
handle, bound to the extension that lists the function (so only a function listed on an
extension may take one). A `Cursor` is refused: a tool does not run on a cursor.
"""

import inspect
import json
from collections.abc import Callable, Sequence
from typing import cast

from tau_next._core import Outcome
from tau_next.functions import (
    INJECT_CURSOR,
    INJECT_NODE,
    INJECT_TREE,
    Described,
    ToolDefinitionError,
    describe,
    invoke,
)
from tau_next.handles import NodeHandle
from tau_next.protocols import Progress, ToolContractError
from tau_next.values import Context, Json, check_object, convert_object, json_type, object_schema

__all__ = ["FunctionTool", "ToolDefinitionError", "ToolError", "assemble_tools", "tool"]


class ToolError(Exception):
    """Why a call failed; `FunctionTool.call` answers it as `Err(message)`, exactly that text.
    A tool function may raise it to say something to the model without a type prefix."""


def _no_progress(text: str) -> None:
    """The `progress` of a function called with nowhere to show it."""


class FunctionTool:
    """A `Tool` made from a function by `tool`. `function` is the function itself, unchanged."""

    def __init__(self, function: Callable[..., object]) -> None:
        self._described: Described = describe(
            function,
            what="tool",
            refused={
                INJECT_CURSOR: "a tool does not run on a cursor (a Cursor is for a command); "
                "take a TreeHandle to read or write the tree"
            },
        )
        self.function = function

    @property
    def name(self) -> str:
        return self._described.name

    @property
    def description(self) -> str:
        return self._described.description

    @property
    def parameters(self) -> Json:
        """The JSON Schema of the arguments object, as a model is shown it."""
        return object_schema(self._described.fields, model=True)

    @property
    def client_parameters(self) -> Json:
        """The same schema with the named types' `"format"`, for a client."""
        return object_schema(self._described.fields, model=False)

    @property
    def takes_handle(self) -> bool:
        """Whether the function takes a `TreeHandle` or `NodeHandle`, which only the
        extension that lists it can give it."""
        return self._described.takes(INJECT_TREE) or self._described.takes(INJECT_NODE)

    @property
    def needs_session(self) -> bool:
        """Whether an argument is checked against the session (a `Model`, `CursorId`,
        `LabelName` or `NodeId`), so `call` needs a `Context`."""
        return any(f.kind.needs_session for f in self._described.fields)

    def _parse(self, args_raw: str) -> dict[str, object]:
        try:
            value: object = json.loads(args_raw)
        except ValueError as e:
            raise ToolError(f"arguments are not valid JSON: {e}") from e
        if not isinstance(value, dict):
            raise ToolError(f"arguments must be a JSON object, not {json_type(value)}")
        return cast("dict[str, object]", value)

    def _arguments(self, args_raw: str, context: Context | None) -> dict[str, object]:
        """The call's arguments, all checked and then converted."""
        if self.needs_session and context is None:
            raise ToolError(
                f"{self.name} checks its arguments against a session; call it by a driver"
            )
        args = self._parse(args_raw)
        problem = check_object(self._described.fields, args, context)
        if problem is not None:
            raise ToolError(problem)
        return convert_object(self._described.fields, args, context)

    async def _run(
        self, arguments: dict[str, object], progress: Progress | None, handle: NodeHandle | None
    ) -> str:
        if handle is None and self.takes_handle:
            raise ToolError(f"{self.name} takes a handle; call it by a driver")
        try:
            result = await invoke(
                self.function,
                self._described,
                arguments,
                progress=progress or _no_progress,
                handle=cast("NodeHandle", handle),
            )
        except ToolError:
            raise  # the function talking to the model: its message, exactly
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e
        if not isinstance(result, str):
            raise ToolError(f"{self.name} returned {type(result).__name__}, not str")
        return result

    async def call(
        self,
        args_raw: str,
        *,
        progress: Progress | None = None,
        context: Context | None = None,
        handle: NodeHandle | None = None,
    ) -> Outcome:
        """Parse and check `args_raw`, run the function, and answer `Ok(its str)`.

        The driver supplies what the function's injected parameters take: `progress` (nothing
        is shown if there is none: a call made outside a driver), `handle` (required if the
        function takes one), and `context` (required if an argument is a named type).

        `Err(message)` for malformed JSON, arguments that do not fit the signature, any
        exception the function raised (named by type), and a return value that is not a
        `str`. A sync function runs in a worker thread, so it never blocks the loop;
        aborting a call stops waiting for it, not the thread. A function wrapped (with
        `functools.wraps`) around an async one counts as async.
        """
        try:
            arguments = self._arguments(args_raw, context)
            return Outcome.Ok(await self._run(arguments, progress, handle))
        except ToolError as e:
            return Outcome.Err(str(e))


def tool(function: Callable[..., object], /) -> FunctionTool:
    """Make a function (sync or async) a tool, for a driver's `tools=`. An extension lists
    plain functions in its own `tools` instead. Raises `ToolDefinitionError`."""
    return FunctionTool(function)


def assemble_tools(
    tools: Sequence[object], extensions: Sequence[object]
) -> tuple[list[object], dict[str, str]]:
    """The driver's `tools` followed by each extension's `tools` attribute, a plain function
    described as a `FunctionTool`, and the owner (extension name) of each tool an extension
    lists. A `FunctionTool` that takes a handle outside an extension, and a `tools` that is
    not a sequence of tools and functions, raise `ToolContractError`; a function that cannot be
    described raises `ToolDefinitionError`; duplicate names are `check_tools`'s to refuse."""
    out: list[object] = []
    for candidate in tools:
        if isinstance(candidate, FunctionTool) and candidate.takes_handle:
            raise ToolContractError(
                f"tool {candidate.name!r} takes a handle, which only the extension that "
                "lists it can give; list it in that extension's `tools`"
            )
        out.append(candidate)
    owners: dict[str, str] = {}
    for ext in extensions:
        name = cast("str", getattr(ext, "name"))  # noqa: B009
        listed: object = getattr(ext, "tools", ())
        if not isinstance(listed, Sequence) or isinstance(listed, str):
            raise ToolContractError(f"extension {name!r}: `tools` must be a sequence")
        for item in cast("Sequence[object]", listed):
            made = item if hasattr(item, "call") else _described(item, name)
            out.append(made)
            owners[cast("str", getattr(made, "name", ""))] = name
    return out, owners


def _described(item: object, extension: str) -> FunctionTool:
    if not (inspect.isroutine(item) or callable(item)):
        raise ToolContractError(
            f"extension {extension!r}: {item!r} in `tools` is neither a Tool nor a function"
        )
    return FunctionTool(cast("Callable[..., object]", item))
