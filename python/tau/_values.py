"""One type language: a Python type hint becomes a schema, a check, and a conversion.

Tool parameters, command parameters, a question's answer and an extension's options are
written as hints. `kind_of(hint)` turns one into a `Kind`:

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
`Model`, `CursorId`, `LabelName`, `NodeId`. Anything else raises `DefinitionError`.

The named types are checked through a `Context` the driver supplies: `Model` against the
catalog, the other three against the session. Options are checked with a catalog alone, so
they may hold a `Model` and no other named type. `CursorId` also accepts `"new"`; `convert`
then opens the cursor, so a caller checks every value before converting any, and nothing is
opened for a call that is refused.

A `list[T]` field's default may be a tuple (a dataclass refuses a list default); it is shown
as an array. A `Field` is one named hint with a default or without: a function parameter or a
dataclass field, through the same `field_of`. A `T | None` with no default may be left out, and
is then `None`. The schema of a set of fields (`object_schema`) carries each default.
"""

import types
from collections.abc import Callable, Mapping, Sequence
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

from tau._core import Session

if TYPE_CHECKING:  # _config_layers imports protocols, which imports handles, which imports us
    from tau._config_layers import ModelCatalog

Json = dict[str, object]
"""A JSON object as Python holds it: what a schema or a decoded JSON object is typed as."""

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


class DefinitionError(TypeError):
    """A tool, command, extension or question that cannot be built: a parameter with no
    annotation or one outside the type language, a default that does not fit its hint, an
    injected parameter the function cannot have, a name taken twice, or an object that breaks the
    `Tool` or `Extension` contract. The message names the extension, function and parameter."""


@dataclass(frozen=True)
class Context:
    """What the named types are checked against: the model `catalog`, and for the other named
    types the `session` and how to open a cursor for `"new"` (at the asker's node). Options are
    checked with the catalog alone: they may not name a type that needs the rest."""

    catalog: "ModelCatalog"
    session: Session | None = None
    new_cursor: Callable[[], int] | None = None


Check = Callable[[object, "Context | None", bool], str | None]
Convert = Callable[[object, "Context"], object]


@dataclass(frozen=True)
class Kind:
    """One hint, understood: its two schemas, and how to check and convert a value.

    `session_only` is true if a value is looked up in the session (a cursor, label or node),
    which options, read without one, cannot hold.
    """

    schema: Json
    model_schema: Json
    session_only: bool
    test: Check
    make: Convert

    def check(self, value: object, context: "Context") -> str | None:
        """`None` if the decoded JSON `value` fits, else the problem."""
        return self.test(value, context, False)

    def check_default(self, value: object) -> str | None:
        """`None` if the Python `value` could be this hint's default: its shape only (a
        `Path` for a `Path`), since a default is not looked up in the session."""
        return self.test(value, None, True)

    def convert(self, value: object, context: "Context") -> object:
        """What the function receives for a checked `value`."""
        return self.make(value, context)

    def described(self, text: str) -> "Kind":
        """This kind with `text` as the `"description"` of both schemas."""
        return Kind(
            {**self.schema, "description": text},
            {**self.model_schema, "description": text},
            self.session_only,
            self.test,
            self.make,
        )


def _identity(value: object, context: Context) -> object:
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


def _scalar(type_name: str, accepts: tuple[str, ...], make: Convert = _identity) -> Kind:
    def check(value: object, context: Context | None, native: bool) -> str | None:
        got = json_type(value)
        return None if got in accepts else f"must be {type_name}, not {got}"

    schema: Json = {"type": type_name}
    return Kind(schema, schema, False, check, make)


def _array(item: Kind) -> Kind:
    def check(value: object, context: Context | None, native: bool) -> str | None:
        if not isinstance(value, list) and not (native and isinstance(value, tuple)):
            return f"must be array, not {json_type(value)}"
        for i, element in enumerate(cast("list[object]", value)):
            problem = item.test(element, context, native)
            if problem is not None:
                return f"item {i} {problem}"
        return None

    def convert(value: object, context: Context) -> object:
        return [item.convert(element, context) for element in cast("list[object]", value)]

    return Kind(
        {"type": "array", "items": item.schema},
        {"type": "array", "items": item.model_schema},
        item.session_only,
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

    def convert(value: object, context: Context) -> object:
        return None if value is None else inner.convert(value, context)

    return Kind(
        {"anyOf": [inner.schema, {"type": "null"}]},
        {"anyOf": [inner.model_schema, {"type": "null"}]},
        inner.session_only,
        check,
        convert,
    )


def _path() -> Kind:
    def check(value: object, context: Context | None, native: bool) -> str | None:
        if native:
            return None if isinstance(value, Path) else f"must be a Path, not {value!r}"
        got = json_type(value)
        if got != "string":
            return f"must be string, not {got}"
        try:
            Path(cast("str", value)).expanduser()
        except RuntimeError as e:  # `~name` of no user
            return f"cannot be expanded: {e}"
        return None

    def convert(value: object, context: Context) -> object:
        return Path(cast("str", value)).expanduser()

    return Kind({"type": "string", "format": "path"}, {"type": "string"}, False, check, convert)


def _named(
    base: Kind,
    format_: str,
    lookup: Callable[[object, Context], str | None],
    *,
    session_only: bool,
) -> Kind:
    """A named type: `base`'s shape, then `lookup` in the context."""

    def test(value: object, context: Context | None, native: bool) -> str | None:
        problem = base.test(value, context, native)
        if problem is not None or context is None:
            return problem
        return lookup(value, context)

    return Kind(
        {**base.schema, "format": format_}, base.model_schema, session_only, test, base.make
    )


def _session(context: Context) -> Session:
    """The session of a context that has one: a named type that looks in the session is
    refused in options, the only values checked without one."""
    return cast("Session", context.session)


def _model_exists(value: object, context: Context) -> str | None:
    if value in context.catalog.models:
        return None
    return f"must be a configured model, not {value!r}"


def _label_exists(value: object, context: Context) -> str | None:
    if any(label == value for label, _ in _session(context).labels()):
        return None
    return f"must be an existing label, not {value!r}"


def _node_exists(value: object, context: Context) -> str | None:
    if 0 <= cast("int", value) < len(_session(context).tree):
        return None
    return f"must be an existing node, not {value!r}"


def _cursor() -> Kind:
    """`CursorId`: an integer or `"new"`. Converting `"new"` opens the cursor."""
    integer = _scalar("integer", ("integer",))
    schema: Json = {"anyOf": [integer.schema, {"type": "string", "enum": [NEW]}]}

    def test(value: object, context: Context | None, native: bool) -> str | None:
        if value == NEW:
            return (
                f"opens a cursor, which only a caller may ask for with {NEW!r}" if native else None
            )
        problem = integer.test(value, context, native)
        if problem is not None or context is None or value in _session(context).cursors():
            return problem
        return f"must be an open cursor or {NEW!r}, not {value!r}"

    def make(value: object, context: Context) -> object:
        if value != NEW:
            return value
        return cast("Callable[[], int]", context.new_cursor)()

    return Kind({**schema, "format": "cursor"}, schema, True, test, make)


_SCALARS: dict[object, tuple[str, tuple[str, ...]]] = {
    str: ("string", ("string",)),
    bool: ("boolean", ("boolean",)),
    int: ("integer", ("integer",)),
}

_NAMED: dict[object, Callable[[], Kind]] = {
    Model: lambda: _named(
        _scalar("string", ("string",)), "model", _model_exists, session_only=False
    ),
    CursorId: _cursor,
    LabelName: lambda: _named(
        _scalar("string", ("string",)), "label", _label_exists, session_only=True
    ),
    NodeId: lambda: _named(
        _scalar("integer", ("integer",)), "node", _node_exists, session_only=True
    ),
}

SUPPORTED = (
    "str, int, float, bool, list[T], Literal of str or int, T | None, Annotated[T, 'text'], "
    "Path, Model, CursorId, LabelName, NodeId"
)


def _annotated(hint: object) -> Kind | None:
    inner, *metadata = get_args(hint)
    if len(metadata) != 1 or not isinstance(metadata[0], str):
        raise DefinitionError(
            f"{hint!r}: Annotated carries one str, the description (got {metadata!r})"
        )
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


def _float(value: object, context: Context) -> object:
    return float(cast("float", value))  # a JSON integer is a number the function gets as float


def kind_of(hint: object) -> Kind | None:
    """The `Kind` of a hint, or `None` if it is not in the language. An `Annotated` whose
    metadata is not a single `str` raises `DefinitionError`."""
    if get_origin(hint) is Annotated:
        return _annotated(hint)
    if hint is float:
        return _scalar("number", ("integer", "number"), _float)
    if hint in _SCALARS:
        return _scalar(*_SCALARS[hint])
    if hint is Path:
        return _path()
    if hint in _NAMED:
        return _NAMED[hint]()
    return _generic(hint)


@dataclass(frozen=True)
class Form:
    """What a client knows of something it fills in: a command (its `name`, typed as `/name`,
    its `description` and the `schema` of its arguments, with the named types' `"format"` and
    each default, and the `extension` that owns it) or an extension's options (the `name` of the
    extension, and a `schema` whose defaults are the values now; `extension` is `None`, as it is
    for `/config`). Frozen, compared by value, and not hashable: the schema is a dict."""

    name: str
    description: str
    schema: Json
    extension: str | None = None

    @property
    def qualified(self) -> str:
        """`extension:name` for a command, which nothing can take the name of; `name` for a form
        with no extension."""
        return self.name if self.extension is None else f"{self.extension}:{self.name}"


@dataclass(frozen=True)
class Field:
    """One named value of the language: its `kind`, and its `default` if it `has_default`.
    It is `nullable` if it is a `T | None` with no default, which is `None` when absent;
    `required` is every other field without a default."""

    name: str
    kind: Kind
    has_default: bool = False
    default: object = None
    nullable: bool = False

    @property
    def required(self) -> bool:
        return not self.has_default and not self.nullable


def field_of(name: str, hint: object, default: object, *, has_default: bool) -> Field:
    """The `Field` for `name: hint = default`. Raises `DefinitionError` for a hint outside the
    language, or a default that does not fit it (`s: str = None` says to annotate
    `str | None`)."""
    kind = kind_of(hint)
    if kind is None:
        raise DefinitionError(f"unsupported annotation {hint!r}; supported: {SUPPORTED}")
    if has_default:
        problem = kind.check_default(default)
        if problem is not None:
            shown = getattr(hint, "__name__", repr(hint))
            advice = f"; annotate it as `{shown} | None`" if default is None else ""
            raise DefinitionError(f"its default {default!r} {problem}{advice}")
    return Field(
        name,
        kind,
        has_default,
        default if has_default else None,
        nullable=not has_default and _optional(hint),
    )


def _optional(hint: object) -> bool:
    """Whether `hint` (under any `Annotated`) is `T | None`."""
    while get_origin(hint) is Annotated:
        hint = get_args(hint)[0]
    return get_origin(hint) in (Union, types.UnionType) and type(None) in get_args(hint)


def plain(value: object) -> object:
    """A Python value of the language as JSON: a `Path` as its string."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list | tuple):
        return [plain(v) for v in cast("Sequence[object]", value)]
    return value


def object_schema(
    fields: tuple[Field, ...], *, model: bool, current: Mapping[str, object] | None = None
) -> Json:
    """The JSON Schema of an object with these `fields`: `required` lists those without a
    default, and no other key is allowed. `model` picks the schema a model is shown (no
    `"format"`, no `"default"`, which no provider API accepts); otherwise each field that is not
    required carries its `"default"`, or its value in `current` (JSON) if there is one."""
    properties: Json = {}
    for f in fields:
        if model:
            properties[f.name] = f.kind.model_schema
            continue
        schema = f.kind.schema
        if f.name in (current or {}):
            schema = {**schema, "default": (current or {})[f.name]}
        elif not f.required:
            schema = {**schema, "default": plain(f.default)}
        properties[f.name] = schema
    schema: Json = {"type": "object", "properties": properties, "additionalProperties": False}
    required = [f.name for f in fields if f.required and not (current and f.name in current)]
    if required:
        schema["required"] = required
    return schema


def check_object(
    fields: tuple[Field, ...],
    values: Mapping[str, object],
    context: Context,
    noun: str = "argument",
) -> str | None:
    """`None` if `values` (a decoded JSON object) has only these fields, every required one,
    and each fitting its kind; else the first problem. `noun` names a field in it
    (`"argument"` of a call, `"option"` of an extension)."""
    by_name = {f.name: f for f in fields}
    extra = [k for k in values if k not in by_name]
    if extra:
        return f"unexpected {noun}s {extra}; takes {list(by_name)}"
    missing = [f.name for f in fields if f.required and f.name not in values]
    if missing:
        return f"missing required {noun}s {missing}"
    for key, value in values.items():
        problem = by_name[key].kind.check(value, context)
        if problem is not None:
            return f"{noun} {key!r} {problem}"
    return None


def convert_object(
    fields: tuple[Field, ...], values: Mapping[str, object], context: Context
) -> dict[str, object]:
    """What the function receives for each of the checked `values`, and `None` for each
    nullable field they leave out."""
    by_name = {f.name: f for f in fields}
    converted = {key: by_name[key].kind.convert(value, context) for key, value in values.items()}
    return {**{f.name: None for f in fields if f.nullable and f.name not in values}, **converted}
