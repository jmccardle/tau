"""Tools defined from Python functions: `@tool` turns a function into a `Tool`.

The name is the function's, the description its docstring, and the parameters a
JSON Schema built from its type hints. Everything that cannot be turned into a
schema fails at decoration (`ToolDefinitionError`); a call whose arguments do
not fit the signature answers `Err(message)`, which the model reads.
"""

import asyncio
import inspect
import json
import types
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Union, cast, get_args, get_origin, get_type_hints, overload

from tau_next._core import Outcome

Json = dict[str, object]
Check = Callable[[object], str | None]


class ToolDefinitionError(ValueError):
    """A function that cannot be a tool: no docstring, a parameter with no annotation or an
    unsupported one, `*args`/`**kwargs`, or a `describe` naming a parameter that is not there."""


class ToolError(Exception):
    """Why a call failed; `FunctionTool.call` answers it as `Err(message)`, exactly that text.
    A tool function may raise it to say something to the model without a type prefix."""


@dataclass(frozen=True)
class _Kind:
    """One supported annotation: its JSON Schema, and a check of a decoded JSON value."""

    schema: Json
    check: Check


_JSON_TYPES: tuple[tuple[type | None, str], ...] = (
    (None, "null"),
    (bool, "boolean"),  # before int: a bool is an int
    (int, "integer"),
    (float, "number"),
    (str, "string"),
    (list, "array"),
)


def _json_type(value: object) -> str:
    for python_type, name in _JSON_TYPES:
        if value is None and python_type is None:
            return name
        if python_type is not None and isinstance(value, python_type):
            return name
    return "object"


def _scalar(json_type: str, accepts: tuple[str, ...]) -> _Kind:
    def check(value: object) -> str | None:
        got = _json_type(value)
        return None if got in accepts else f"must be {json_type}, not {got}"

    return _Kind({"type": json_type}, check)


def _array(item: _Kind) -> _Kind:
    def check(value: object) -> str | None:
        if not isinstance(value, list):
            return f"must be array, not {_json_type(value)}"
        for i, element in enumerate(cast("list[object]", value)):
            problem = item.check(element)
            if problem is not None:
                return f"item {i} {problem}"
        return None

    return _Kind({"type": "array", "items": item.schema}, check)


def _enum(options: tuple[object, ...]) -> _Kind | None:
    """`Literal[...]` of all `str` or all `int` (never `bool`, which is an `int`)."""
    if all(type(o) is str for o in options):
        json_type = "string"
    elif all(type(o) is int for o in options):
        json_type = "integer"
    else:
        return None

    def check(value: object) -> str | None:
        if type(value) is type(options[0]) and value in options:
            return None
        return f"must be one of {list(options)!r}, not {value!r}"

    return _Kind({"type": json_type, "enum": list(options)}, check)


def _nullable(inner: _Kind) -> _Kind:
    def check(value: object) -> str | None:
        return None if value is None else inner.check(value)

    return _Kind({"anyOf": [inner.schema, {"type": "null"}]}, check)


_SCALARS: dict[object, tuple[str, tuple[str, ...]]] = {
    str: ("string", ("string",)),
    bool: ("boolean", ("boolean",)),
    int: ("integer", ("integer",)),
    float: ("number", ("integer", "number")),
}


def _kind(annotation: object) -> _Kind | None:
    """The `_Kind` of a supported annotation; `None` if it is not one."""
    if annotation in _SCALARS:
        return _scalar(*_SCALARS[annotation])
    origin = get_origin(annotation)
    args = get_args(annotation)
    inner: _Kind | None = None
    if origin is list and len(args) == 1:
        inner = _kind(args[0])
        return None if inner is None else _array(inner)
    if origin is Literal and args:
        return _enum(args)
    if origin in (Union, types.UnionType) and len(args) == 2 and type(None) in args:  # noqa: PLR2004
        inner = _kind(next(a for a in args if a is not type(None)))
        return None if inner is None else _nullable(inner)
    return None


@dataclass(frozen=True)
class _Param:
    name: str
    kind: _Kind
    required: bool
    description: str | None


def _params(fn: Callable[..., object], describe: Mapping[str, str]) -> tuple[_Param, ...]:
    name = fn.__name__
    signature = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except (NameError, SyntaxError, TypeError) as e:
        raise ToolDefinitionError(
            f"tool {name!r}: an annotation cannot be resolved: {type(e).__name__}: {e}"
        ) from e
    unknown = [p for p in describe if p not in signature.parameters]
    if unknown:
        raise ToolDefinitionError(
            f"tool {name!r}: describe names {unknown}, which are not parameters "
            f"(parameters: {list(signature.parameters)})"
        )
    out: list[_Param] = []
    for param in signature.parameters.values():
        where = f"tool {name!r}, parameter {param.name!r}"
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise ToolDefinitionError(f"{where}: *args and **kwargs cannot be described")
        if param.kind is param.POSITIONAL_ONLY:
            raise ToolDefinitionError(
                f"{where}: positional-only parameters cannot be passed by name"
            )
        if param.name not in hints:
            raise ToolDefinitionError(f"{where}: no type annotation")
        kind = _kind(hints[param.name])
        if kind is None:
            raise ToolDefinitionError(
                f"{where}: unsupported annotation {hints[param.name]!r}; supported: str, int, "
                "float, bool, list[T], Literal of str or int, and T | None"
            )
        required = param.default is param.empty
        if not required:
            problem = kind.check(param.default)
            if problem is not None:
                shown = getattr(hints[param.name], "__name__", repr(hints[param.name]))
                hint = f"; annotate it as `{shown} | None`" if param.default is None else ""
                raise ToolDefinitionError(f"{where}: its default {param.default!r} {problem}{hint}")
        out.append(_Param(param.name, kind, required, describe.get(param.name)))
    return tuple(out)


class FunctionTool:
    """A `Tool` made from a function by `tool`. `function` is the function itself, unchanged."""

    def __init__(
        self, function: Callable[..., object], describe: Mapping[str, str] | None = None
    ) -> None:
        if not callable(function) or not hasattr(function, "__name__"):
            raise ToolDefinitionError(f"{function!r} is not a function")
        doc = inspect.getdoc(function)
        if not doc:
            raise ToolDefinitionError(f"tool {function.__name__!r}: no docstring to describe it")
        self.function = function
        self._name = function.__name__
        self._description = doc
        self._params = _params(function, describe or {})
        self._is_async = inspect.iscoroutinefunction(inspect.unwrap(function))

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> Json:
        """The JSON Schema of the arguments object."""
        properties: Json = {}
        for p in self._params:
            schema = dict(p.kind.schema)
            if p.description is not None:
                schema["description"] = p.description
            properties[p.name] = schema
        schema: Json = {"type": "object", "properties": properties, "additionalProperties": False}
        required = [p.name for p in self._params if p.required]
        if required:
            schema["required"] = required
        return schema

    def _validate(self, args: Mapping[str, object]) -> None:
        by_name = {p.name: p for p in self._params}
        extra = [k for k in args if k not in by_name]
        if extra:
            raise ToolError(f"unexpected arguments {extra}; {self._name} takes {list(by_name)}")
        missing = [p.name for p in self._params if p.required and p.name not in args]
        if missing:
            raise ToolError(f"missing required arguments {missing}")
        for key, value in args.items():
            problem = by_name[key].kind.check(value)
            if problem is not None:
                raise ToolError(f"argument {key!r} {problem}")

    def _parse(self, args_raw: str) -> dict[str, object]:
        try:
            value: object = json.loads(args_raw)
        except ValueError as e:
            raise ToolError(f"arguments are not valid JSON: {e}") from e
        if not isinstance(value, dict):
            raise ToolError(f"arguments must be a JSON object, not {_json_type(value)}")
        return cast("dict[str, object]", value)

    async def _run(self, args_raw: str) -> str:
        args = self._parse(args_raw)
        self._validate(args)
        try:
            if self._is_async:
                result = await cast("Awaitable[object]", self.function(**args))
            else:
                result = await asyncio.to_thread(self.function, **args)
        except ToolError:
            raise  # the function talking to the model: its message, exactly
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e
        if not isinstance(result, str):
            raise ToolError(f"{self._name} returned {type(result).__name__}, not str")
        return result

    async def call(self, args_raw: str) -> Outcome:
        """Parse and check `args_raw`, run the function, and answer `Ok(its str)`.

        `Err(message)` for malformed JSON, arguments that do not fit the signature, any
        exception the function raised (named by type), and a return value that is not a
        `str`. A sync function runs in a worker thread, so it never blocks the loop;
        aborting a call stops waiting for it, not the thread. A function wrapped (with
        `functools.wraps`) around an async one counts as async.
        """
        try:
            return Outcome.Ok(await self._run(args_raw))
        except ToolError as e:
            return Outcome.Err(str(e))


@overload
def tool(function: Callable[..., object], /) -> FunctionTool: ...
@overload
def tool(
    *, describe: Mapping[str, str] | None = None
) -> Callable[[Callable[..., object]], FunctionTool]: ...
def tool(
    function: Callable[..., object] | None = None,
    /,
    *,
    describe: Mapping[str, str] | None = None,
) -> FunctionTool | Callable[[Callable[..., object]], FunctionTool]:
    """Make a function (sync or async) a tool: `@tool`, or `@tool(describe={"path": "..."})`.

    Raises `ToolDefinitionError` at decoration.
    """
    if function is not None:
        return FunctionTool(function, describe)
    return lambda fn: FunctionTool(fn, describe)
