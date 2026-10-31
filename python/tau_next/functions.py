"""A plain function, described: its name, docstring, arguments and injected parameters.

`describe(function, what="tool")` reads a function once. Its parameters are `Field`s of the
type language (`tau_next.values`); those the driver fills rather than the caller are
**injected**, named by annotation:

- `Progress` (or a parameter named `progress`): the running call's progress callback;
- `TreeHandle` or `NodeHandle`: the handle of the batch or the cursor, bound to the extension
  that lists the function;
- `Cursor`: the cursor a command runs on.

A tool refuses `Cursor`, and a command (later) refuses `NodeHandle`: the caller of `describe`
passes `refused`, injections it cannot supply. The model, or a person, is never told of an
injected parameter and cannot pass it.
"""

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Union, get_args, get_origin, get_type_hints

from tau_next.handles import Cursor, NodeHandle, TreeHandle
from tau_next.protocols import Progress
from tau_next.values import Field, HintError, field_of

PROGRESS = "progress"  # a parameter name filled without an annotation, as before annotations did

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


class ToolDefinitionError(ValueError):
    """A function that cannot be a tool: no docstring, a parameter with no annotation or an
    unsupported one, `*args`/`**kwargs`, or an injected parameter this kind of function cannot
    have (a `Cursor` in a tool)."""


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


def _unwrap_optional(hint: object) -> object:
    """`X` for `X | None`, else `hint`."""
    if get_origin(hint) in (Union, type(int | None)):
        args = [a for a in get_args(hint) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return hint


def _injection(param: inspect.Parameter, hints: Mapping[str, object]) -> str | None:
    """The kind of `param` if the driver fills it. `progress` by name must be unannotated or
    `Progress`/`Progress | None`; by annotation it is any name."""
    hint = hints.get(param.name)
    if hint is not None and get_origin(hint) is Annotated:
        hint = get_args(hint)[0]
    by_hint = _BY_HINT.get(_unwrap_optional(hint)) if hint is not None else None
    if by_hint is not None:
        return by_hint
    if param.name != PROGRESS:
        return None
    if hint is not None:
        raise ToolDefinitionError(
            f"`progress` is the callback the driver passes (annotate it as `Progress` "
            f"or `Progress | None`, or leave it unannotated), not an argument of type "
            f"{hint!r}; rename the argument"
        )
    return INJECT_PROGRESS


def describe(
    function: Callable[..., object], *, what: str = "tool", refused: Mapping[str, str] | None = None
) -> Described:
    """Read `function`. `what` names it in errors; `refused` maps an injection kind this
    caller cannot supply to the reason. Raises `ToolDefinitionError` naming the function and
    parameter."""
    if not callable(function) or not hasattr(function, "__name__"):
        raise ToolDefinitionError(f"{function!r} is not a function")
    name = function.__name__
    doc = inspect.getdoc(function)
    if not doc:
        raise ToolDefinitionError(f"{what} {name!r}: no docstring to describe it")
    try:
        hints = get_type_hints(function, include_extras=True)
    except (NameError, SyntaxError, TypeError) as e:
        raise ToolDefinitionError(
            f"{what} {name!r}: an annotation cannot be resolved: {type(e).__name__}: {e}"
        ) from e
    fields: list[Field] = []
    injected: dict[str, str] = {}
    for param in inspect.signature(function).parameters.values():
        where = f"{what} {name!r}, parameter {param.name!r}"
        try:
            kind = _injection(param, hints)
        except ToolDefinitionError as e:
            raise ToolDefinitionError(f"{where}: {e}") from e
        if kind is not None:
            _check_injection(param, kind, where, refused or {})
            injected[param.name] = kind
            continue
        fields.append(_field(param, hints, where))
    return Described(
        name, doc, tuple(fields), injected, inspect.iscoroutinefunction(inspect.unwrap(function))
    )


def _check_injection(
    param: inspect.Parameter, kind: str, where: str, refused: Mapping[str, str]
) -> None:
    if kind in refused:
        raise ToolDefinitionError(f"{where}: {refused[kind]}")
    if param.kind is param.POSITIONAL_ONLY:
        raise ToolDefinitionError(f"{where}: an injected parameter must be passable by keyword")


def _field(param: inspect.Parameter, hints: Mapping[str, object], where: str) -> Field:
    if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
        raise ToolDefinitionError(f"{where}: *args and **kwargs cannot be described")
    if param.kind is param.POSITIONAL_ONLY:
        raise ToolDefinitionError(f"{where}: positional-only parameters cannot be passed by name")
    if param.name not in hints:
        raise ToolDefinitionError(f"{where}: no type annotation")
    try:
        return field_of(
            param.name,
            hints[param.name],
            param.default,
            has_default=param.default is not param.empty,
        )
    except HintError as e:
        raise ToolDefinitionError(f"{where}: {e}") from e
