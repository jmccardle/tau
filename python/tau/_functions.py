"""A plain function, described: its name, docstring, arguments and injected parameters.

`describe(function, what="tool")` reads a function once. Its parameters are `Field`s of the
type language (`tau._values`); those the driver fills rather than the caller are
**injected**, named by annotation alone:

- `Progress`: the running call's progress callback;
- `TreeHandle` or `NodeHandle`: the handle of the batch or the cursor, bound to the extension
  that lists the function;
- `Cursor`: the cursor a command runs on.

A tool refuses `Cursor`, and a command refuses `NodeHandle`: the caller of `describe` passes
`refused`, injections it cannot supply. The model, or a person, is never told of an injected
parameter and cannot pass it.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import cast, get_type_hints

from tau._values import DefinitionError, Field, field_of
from tau.handles import Cursor, NodeHandle, TreeHandle
from tau.protocols import Progress

# Injection kinds, by what fills them.
INJECT_PROGRESS = "progress"
INJECT_TREE = "tree"
INJECT_NODE = "node"
INJECT_CURSOR = "cursor"

_BY_HINT: dict[object, str] = {
    Progress: INJECT_PROGRESS,
    TreeHandle: INJECT_TREE,
    NodeHandle: INJECT_NODE,
    Cursor: INJECT_CURSOR,
}


@dataclass(frozen=True)
class Described:
    """A function, read: `fields` are the arguments, `injected` maps each injected
    parameter's name to its kind (`"progress"`, `"tree"`, `"node"`, `"cursor"`)."""

    name: str
    description: str
    fields: tuple[Field, ...]
    injected: Mapping[str, str]
    is_async: bool

    def takes(self, kind: str) -> bool:
        return kind in self.injected.values()


def describe(
    function: object,
    *,
    what: str,
    extension: str | None,
    refused: Mapping[str, str],
) -> Described:
    """Read `function`. `what` (`"tool"` or `"command"`) and the `extension` that lists it
    (`None` for a driver's own tool) name it in errors; `refused` maps an injection kind this
    caller cannot supply to the reason. Raises `DefinitionError` naming the extension,
    function and parameter."""
    owner = "" if extension is None else f"extension {extension!r}: "
    if not callable(function) or not hasattr(function, "__name__"):
        raise DefinitionError(f"{owner}{what} {function!r} is not a function")
    name: str = function.__name__  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownVariableType]
    doc = inspect.getdoc(function)
    if not doc:
        raise DefinitionError(f"{owner}{what} {name!r}: no docstring to describe it")
    try:
        hints = get_type_hints(function, include_extras=True)
    except (NameError, SyntaxError, TypeError) as e:
        raise DefinitionError(
            f"{owner}{what} {name!r}: an annotation cannot be resolved: {type(e).__name__}: {e}"
        ) from e
    fields: list[Field] = []
    injected: dict[str, str] = {}
    for param in inspect.signature(function).parameters.values():
        where = f"{owner}{what} {name!r}, parameter {param.name!r}"
        kind = _BY_HINT.get(hints.get(param.name))
        if kind is not None:
            _check_injection(param, kind, where, refused)
            injected[param.name] = kind
            continue
        try:
            fields.append(_field(param, hints))
        except DefinitionError as e:
            raise DefinitionError(f"{where}: {e}") from e
    return Described(
        name, doc, tuple(fields), injected, inspect.iscoroutinefunction(inspect.unwrap(function))
    )


def _check_injection(
    param: inspect.Parameter, kind: str, where: str, refused: Mapping[str, str]
) -> None:
    if kind in refused:
        raise DefinitionError(f"{where}: {refused[kind]}")
    if param.kind is param.POSITIONAL_ONLY:
        raise DefinitionError(f"{where}: an injected parameter must be passable by keyword")


def _field(param: inspect.Parameter, hints: Mapping[str, object]) -> Field:
    """The argument `param` is; `DefinitionError` (without the parameter's name) if it cannot
    be one."""
    if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
        raise DefinitionError("*args and **kwargs cannot be described")
    if param.kind is param.POSITIONAL_ONLY:
        raise DefinitionError("positional-only parameters cannot be passed by name")
    if param.name not in hints:
        raise DefinitionError("no type annotation")
    return field_of(
        param.name,
        hints[param.name],
        param.default,
        has_default=param.default is not param.empty,
    )


async def invoke(
    function: Callable[..., object],
    described: Described,
    arguments: Mapping[str, object],
    *,
    progress: Progress,
    handle: TreeHandle | None,
) -> object:
    """Call `function` with the checked, converted `arguments` and its injected parameters:
    `progress` for a `Progress`, `handle` for a `TreeHandle`, `NodeHandle` or `Cursor`. An async
    function is awaited on the loop; a sync one runs in a worker thread, so it never blocks the
    loop (aborting stops waiting for it, not the thread). The one place a described function is
    called, for a tool and for a command; what the handle tells the driver is routed back to the
    loop's thread by the driver, whichever thread the function writes from."""
    given: dict[str, object] = {
        INJECT_PROGRESS: progress,
        INJECT_TREE: handle,
        INJECT_NODE: handle,
        INJECT_CURSOR: handle,
    }
    kwargs = {**arguments, **{name: given[kind] for name, kind in described.injected.items()}}
    if described.is_async:
        return await cast("Awaitable[object]", function(**kwargs))
    return await asyncio.to_thread(function, **kwargs)
