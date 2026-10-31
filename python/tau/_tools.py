"""Tools defined from Python functions: the driver (or an extension, in its `tools` attribute)
lists plain functions and `assemble_tools` turns each into a `Tool`.

The name is the function's, the description its docstring, and the parameters a JSON Schema
built from its type hints (`tau._values`; a parameter is described with
`Annotated[T, "text"]`). Everything that cannot be turned into a schema fails when the driver
is built (`DefinitionError`); a call whose arguments do not fit the signature answers
`Err(message)`, which the model reads.

Some parameters are the driver's, found by annotation, and are not arguments: a `Progress` is
the callback for showing partial output as the function runs, which may be called from any
thread; a `TreeHandle` or `NodeHandle` is the batch's handle, bound to the extension that lists
the function (so only a function listed on an extension may take one). A `Cursor` is refused: a
tool does not run on a cursor.
"""

import json
from collections.abc import Callable, Sequence
from typing import cast

from tau._core import Outcome
from tau._functions import (
    INJECT_CURSOR,
    INJECT_NODE,
    INJECT_TREE,
    Described,
    describe,
    invoke,
)
from tau._values import (
    Context,
    DefinitionError,
    Json,
    check_object,
    convert_object,
    json_type,
    object_schema,
)
from tau.handles import NodeHandle
from tau.protocols import Progress

__all__ = ["FunctionTool", "ToolError", "assemble_tools"]


class ToolError(Exception):
    """Why a call failed; the call answers it as `Err(message)`, exactly that text. A tool
    function may raise it to say something to the model without a type prefix."""


class FunctionTool:
    """A `Tool` made from a function. `function` is the function itself, unchanged."""

    def __init__(self, function: object, extension: str | None) -> None:
        self._described: Described = describe(
            function,
            what="tool",
            extension=extension,
            refused={
                INJECT_CURSOR: "a tool does not run on a cursor (a Cursor is for a command); "
                "take a TreeHandle to read or write the tree"
            },
        )
        self.function = cast("Callable[..., object]", function)

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
    def takes_handle(self) -> bool:
        """Whether the function takes a `TreeHandle` or `NodeHandle`, which only the
        extension that lists it can give it."""
        return self._described.takes(INJECT_TREE) or self._described.takes(INJECT_NODE)

    def _arguments(self, args_raw: str, context: Context) -> dict[str, object]:
        """The call's arguments, all checked and then converted."""
        try:
            value: object = json.loads(args_raw)
        except ValueError as e:
            raise ToolError(f"arguments are not valid JSON: {e}") from e
        if not isinstance(value, dict):
            raise ToolError(f"arguments must be a JSON object, not {json_type(value)}")
        args = cast("dict[str, object]", value)
        problem = check_object(self._described.fields, args, context)
        if problem is not None:
            raise ToolError(problem)
        return convert_object(self._described.fields, args, context)

    async def call(
        self,
        args_raw: str,
        *,
        progress: Progress,
        context: Context,
        handle: NodeHandle | None,
    ) -> Outcome:
        """Parse and check `args_raw`, run the function, and answer `Ok(its str)`.

        `Err(message)` for malformed JSON, arguments that do not fit the signature, any
        exception the function raised (named by type), and a return value that is not a
        `str`. A sync function runs in a worker thread, so it never blocks the loop;
        aborting a call stops waiting for it, not the thread. A function wrapped (with
        `functools.wraps`) around an async one counts as async. `handle` is the batch's, for a
        function that takes one.
        """
        try:
            arguments = self._arguments(args_raw, context)
            result = await invoke(
                self.function, self._described, arguments, progress=progress, handle=handle
            )
        except ToolError as e:
            return Outcome.Err(str(e))  # the function talking to the model: its message, exactly
        except Exception as e:
            return Outcome.Err(f"{type(e).__name__}: {e}")
        if not isinstance(result, str):
            return Outcome.Err(f"{self.name} returned {type(result).__name__}, not str")
        return Outcome.Ok(result)


def assemble_tools(
    tools: Sequence[object], extensions: Sequence[object]
) -> tuple[list[object], dict[str, str]]:
    """The driver's `tools` followed by each extension's `tools` attribute, and the owner
    (extension name) of each tool an extension lists. An item with a `call` is a `Tool` and
    stays as it is; a function is described as a `FunctionTool`. A function that takes a handle
    outside an extension, a `tools` that is not a sequence, and a function that cannot be
    described raise `DefinitionError`; duplicate names are `check_tools`'s to refuse."""
    out: list[object] = [_made(item, None) for item in tools]
    owners: dict[str, str] = {}
    for ext in extensions:
        name = cast("str", getattr(ext, "name"))  # noqa: B009
        listed: object = getattr(ext, "tools", ())
        if not isinstance(listed, Sequence) or isinstance(listed, str):
            raise DefinitionError(f"extension {name!r}: `tools` must be a sequence")
        for item in cast("Sequence[object]", listed):
            made = _made(item, name)
            out.append(made)
            owners[cast("str", getattr(made, "name", ""))] = name
    return out, owners


def _made(item: object, extension: str | None) -> object:
    if hasattr(item, "call"):
        return item
    if not callable(item):
        where = "" if extension is None else f"extension {extension!r}: "
        raise DefinitionError(f"{where}{item!r} in `tools` is neither a Tool nor a function")
    made = FunctionTool(item, extension)
    if extension is None and made.takes_handle:
        raise DefinitionError(
            f"tool {made.name!r} takes a handle, which only the extension that lists it can "
            "give; list it in that extension's `tools`"
        )
    return made
