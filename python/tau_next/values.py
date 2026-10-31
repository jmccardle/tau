"""One type language: a Python type hint becomes a schema, a check, and a conversion.

Tool parameters, a question's answer, and (later) command parameters and an extension's
options are written as hints. `kind_of(hint)` turns one into a `Kind`:

- `schema`: the JSON Schema a client reads. The named types carry a `"format"`
  (`"path"`, `"model"`, `"cursor"`, `"label"`, `"node"`) a frontend completes by.
- `model_schema`: the same without the `"format"`, which no provider API accepts. `Literal`
  values and descriptions stay.
- `check(value, context)`: `None` if the decoded JSON `value` fits, else the problem as
  words that read after the name of the thing checked ("must be string, not integer").
- `convert(value, context)`: what the function receives. Call it only on a value `check`
  passed.

Supported hints: `str`, `int`, `float`, `bool`, `Literal` of all `str` or all `int`,
`list[T]`, `T | None`, `Annotated[T, "description"]`, `pathlib.Path`, and the named types
`Model`, `CursorId`, `LabelName`, `NodeId`. Anything else raises `HintError`.

The named types are checked against the session through a `Context` the driver supplies.
`CursorId` also accepts `"new"`; `convert` then opens the cursor, so a caller checks every
value before converting any, and nothing is opened for a call that is refused.

A `Field` is one named hint with a default or without: a function parameter now, a
dataclass field when options come, through the same `field_of`.
"""

import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Literal,
    NewType,
    Union,
    cast,
    get_args,
    get_origin,
)

from tau_next._core import Session

if TYPE_CHECKING:  # _config_layers imports protocols, which imports handles, which imports us
    from tau_next._config_layers import ModelCatalog

Json = dict[str, object]

Model = NewType("Model", str)
"""A model name that is configured; a frontend offers the catalog."""
CursorId = NewType("CursorId", int)
"""An open cursor, or `"new"`, which opens one at the asker's node."""
LabelName = NewType("LabelName", str)
"""A label that exists."""
NodeId = NewType("NodeId", int)
"""A node that exists."""

NEW = "new"
"""The `CursorId` value that opens a cursor."""


class HintError(TypeError):
    """A hint that is not in the type language, or a default that does not fit its hint. The
    message names the hint."""


@dataclass(frozen=True)
class Context:
    """What the named types are checked against: the session, the model catalog, and how to
    open a cursor for `"new"` (at the asker's node)."""

    session: Session
    catalog: "ModelCatalog"
    new_cursor: Callable[[], int]


Check = Callable[[object, "Context | None", bool], str | None]
Convert = Callable[[object, "Context | None"], object]


@dataclass(frozen=True)
class Kind:
    """One hint, understood: its two schemas, and how to check and convert a value.

    `needs_session` is true if a value cannot be checked without a `Context`.
    """

    schema: Json
    model_schema: Json
    needs_session: bool
    test: Check
    make: Convert

    def check(self, value: object, context: "Context | None") -> str | None:
        """`None` if the decoded JSON `value` fits, else the problem. A named type needs
        `context`: asking without one is a `ValueError` (a caller that has no session must
        not pass its values off as checked)."""
        if context is None and self.needs_session:
            raise ValueError("this value is checked against a session, and none was given")
        return self.test(value, context, False)

    def check_default(self, value: object) -> str | None:
        """`None` if the Python `value` could be this hint's default: its shape only (a
        `Path` for a `Path`), since a default is not looked up in the session."""
        return self.test(value, None, True)

    def convert(self, value: object, context: "Context | None") -> object:
        """What the function receives for a checked `value`."""
        return self.make(value, context)

    def described(self, text: str) -> "Kind":
        """This kind with `text` as the `"description"` of both schemas."""
        return Kind(
            {**self.schema, "description": text},
            {**self.model_schema, "description": text},
            self.needs_session,
            self.test,
            self.make,
        )


def _identity(value: object, context: Context | None) -> object:
    return value


_JSON_TYPES: tuple[tuple[type | None, str], ...] = (
    (None, "null"),
    (bool, "boolean"),  # before int: a bool is an int
    (int, "integer"),
    (float, "number"),
    (str, "string"),
    (list, "array"),
)


def json_type(value: object) -> str:
    """The JSON type name of a decoded value."""
    for python_type, name in _JSON_TYPES:
        if value is None and python_type is None:
            return name
        if python_type is not None and isinstance(value, python_type):
            return name
    return "object"


def _scalar(type_name: str, accepts: tuple[str, ...]) -> Kind:
    def check(value: object, context: Context | None, native: bool) -> str | None:
        got = json_type(value)
        return None if got in accepts else f"must be {type_name}, not {got}"

    schema: Json = {"type": type_name}
    return Kind(schema, schema, False, check, _identity)


def _array(item: Kind) -> Kind:
    def check(value: object, context: Context | None, native: bool) -> str | None:
        if not isinstance(value, list):
            return f"must be array, not {json_type(value)}"
        for i, element in enumerate(cast("list[object]", value)):
            problem = item.test(element, context, native)
            if problem is not None:
                return f"item {i} {problem}"
        return None

    def convert(value: object, context: Context | None) -> object:
        return [item.convert(element, context) for element in cast("list[object]", value)]

    return Kind(
        {"type": "array", "items": item.schema},
        {"type": "array", "items": item.model_schema},
        item.needs_session,
        check,
        convert,
    )


def _enum(options: tuple[object, ...]) -> Kind | None:
    """`Literal[...]` of all `str` or all `int` (never `bool`, which is an `int`)."""
    if all(type(o) is str for o in options):
        type_name = "string"
    elif all(type(o) is int for o in options):
        type_name = "integer"
    else:
        return None

    def check(value: object, context: Context | None, native: bool) -> str | None:
        if type(value) is type(options[0]) and value in options:
            return None
        return f"must be one of {list(options)!r}, not {value!r}"

    schema: Json = {"type": type_name, "enum": list(options)}
    return Kind(schema, schema, False, check, _identity)


def _nullable(inner: Kind) -> Kind:
    def check(value: object, context: Context | None, native: bool) -> str | None:
        return None if value is None else inner.test(value, context, native)

    def convert(value: object, context: Context | None) -> object:
        return None if value is None else inner.convert(value, context)

    return Kind(
        {"anyOf": [inner.schema, {"type": "null"}]},
        {"anyOf": [inner.model_schema, {"type": "null"}]},
        inner.needs_session,
        check,
        convert,
    )


def _path() -> Kind:
    def check(value: object, context: Context | None, native: bool) -> str | None:
        if native:
            return None if isinstance(value, Path) else f"must be a Path, not {value!r}"
        got = json_type(value)
        return None if got == "string" else f"must be string, not {got}"

    def convert(value: object, context: Context | None) -> object:
        return Path(cast("str", value))

    return Kind({"type": "string", "format": "path"}, {"type": "string"}, False, check, convert)


def _named(base: Kind, format_: str, lookup: Callable[[object, Context], str | None]) -> Kind:
    """A named type: `base`'s shape, then `lookup` against the session."""

    def test(value: object, context: Context | None, native: bool) -> str | None:
        problem = base.test(value, context, native)
        if problem is not None or context is None:
            return problem
        return lookup(value, context)

    return Kind({**base.schema, "format": format_}, base.model_schema, True, test, base.make)


def _model_exists(value: object, context: Context) -> str | None:
    if value in context.catalog.models:
        return None
    return f"must be a configured model, not {value!r}"


def _label_exists(value: object, context: Context) -> str | None:
    if any(label == value for label, _ in context.session.labels()):
        return None
    return f"must be an existing label, not {value!r}"


def _node_exists(value: object, context: Context) -> str | None:
    if 0 <= cast("int", value) < len(context.session.tree):
        return None
    return f"must be an existing node, not {value!r}"


def _cursor() -> Kind:
    """`CursorId`: an integer or `"new"`. Converting `"new"` opens the cursor."""
    integer = _scalar("integer", ("integer",))
    schema: Json = {"anyOf": [integer.schema, {"type": "string", "enum": [NEW]}]}

    def test(value: object, context: Context | None, native: bool) -> str | None:
        if value == NEW:
            return None
        problem = integer.test(value, context, native)
        if problem is not None or context is None or value in context.session.cursors():
            return problem
        return f"must be an open cursor or {NEW!r}, not {value!r}"

    def make(value: object, context: Context | None) -> object:
        if value != NEW:
            return value
        if context is None:
            raise ValueError("a new cursor is opened through a session, and none was given")
        return context.new_cursor()

    return Kind({**schema, "format": "cursor"}, schema, True, test, make)


_SCALARS: dict[object, tuple[str, tuple[str, ...]]] = {
    str: ("string", ("string",)),
    bool: ("boolean", ("boolean",)),
    int: ("integer", ("integer",)),
    float: ("number", ("integer", "number")),
}

_NAMED: dict[object, Callable[[], Kind]] = {
    Model: lambda: _named(_scalar("string", ("string",)), "model", _model_exists),
    CursorId: _cursor,
    LabelName: lambda: _named(_scalar("string", ("string",)), "label", _label_exists),
    NodeId: lambda: _named(_scalar("integer", ("integer",)), "node", _node_exists),
}

SUPPORTED = (
    "str, int, float, bool, list[T], Literal of str or int, T | None, Annotated[T, 'text'], "
    "Path, Model, CursorId, LabelName, NodeId"
)


def _annotated(hint: object) -> Kind | None:
    inner, *metadata = get_args(hint)
    if len(metadata) != 1 or not isinstance(metadata[0], str):
        raise HintError(f"{hint!r}: Annotated carries one str, the description (got {metadata!r})")
    kind = kind_of(inner)
    return None if kind is None else kind.described(metadata[0])


def _generic(hint: object) -> Kind | None:
    """`list[T]`, `Literal[...]` and `T | None`."""
    origin = get_origin(hint)
    args = get_args(hint)
    if origin is list and len(args) == 1:
        item = kind_of(args[0])
        return None if item is None else _array(item)
    if origin is Literal and args:
        return _enum(args)
    if origin in (Union, types.UnionType) and len(args) == 2 and type(None) in args:  # noqa: PLR2004
        inner = kind_of(next(a for a in args if a is not type(None)))
        return None if inner is None else _nullable(inner)
    return None


def kind_of(hint: object) -> Kind | None:
    """The `Kind` of a hint, or `None` if it is not in the language. An `Annotated` whose
    metadata is not a single `str` raises `HintError`."""
    if get_origin(hint) is Annotated:
        return _annotated(hint)
    if hint in _SCALARS:
        return _scalar(*_SCALARS[hint])
    if hint is Path:
        return _path()
    if hint in _NAMED:
        return _NAMED[hint]()
    return _generic(hint)


@dataclass(frozen=True)
class Field:
    """One named value of the language: its `kind`, and `required` unless it has a default."""

    name: str
    kind: Kind
    required: bool


def field_of(name: str, hint: object, default: object, *, has_default: bool) -> Field:
    """The `Field` for `name: hint = default`. Raises `HintError` for a hint outside the
    language, or a default that does not fit it (`s: str = None` says to annotate
    `str | None`)."""
    kind = kind_of(hint)
    if kind is None:
        raise HintError(f"unsupported annotation {hint!r}; supported: {SUPPORTED}")
    if has_default:
        problem = kind.check_default(default)
        if problem is not None:
            shown = getattr(hint, "__name__", repr(hint))
            advice = f"; annotate it as `{shown} | None`" if default is None else ""
            raise HintError(f"its default {default!r} {problem}{advice}")
    return Field(name, kind, not has_default)


def object_schema(fields: tuple[Field, ...], *, model: bool) -> Json:
    """The JSON Schema of an object with these `fields`: `required` lists those without a
    default, and no other key is allowed. `model` picks the schema a model is shown."""
    properties: Json = {f.name: (f.kind.model_schema if model else f.kind.schema) for f in fields}
    schema: Json = {"type": "object", "properties": properties, "additionalProperties": False}
    required = [f.name for f in fields if f.required]
    if required:
        schema["required"] = required
    return schema


def check_object(
    fields: tuple[Field, ...], values: Mapping[str, object], context: Context | None
) -> str | None:
    """`None` if `values` (a decoded JSON object) has only these fields, every required one,
    and each fitting its kind; else the first problem."""
    by_name = {f.name: f for f in fields}
    extra = [k for k in values if k not in by_name]
    if extra:
        return f"unexpected arguments {extra}; takes {list(by_name)}"
    missing = [f.name for f in fields if f.required and f.name not in values]
    if missing:
        return f"missing required arguments {missing}"
    for key, value in values.items():
        problem = by_name[key].kind.check(value, context)
        if problem is not None:
            return f"argument {key!r} {problem}"
    return None


def convert_object(
    fields: tuple[Field, ...], values: Mapping[str, object], context: Context | None
) -> dict[str, object]:
    """What the function receives for each of the checked `values`."""
    by_name = {f.name: f for f in fields}
    return {key: by_name[key].kind.convert(value, context) for key, value in values.items()}
