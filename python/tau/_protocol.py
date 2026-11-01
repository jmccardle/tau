"""The wire's contract and its coding (D21 sections 4, 5, 8): what a server and a client both call,
so neither describes a value or an error on its own.

- `PROTOCOL`: the integer a `hello` carries. A version marks a released protocol, not a commit.
- `METHODS`: one `Method` per `Client` call (and `hello`), read from the `Client` signatures:
  `decode_params` and `encode_result` for a server, `encode_params` and `decode_result` for a
  client. A call's params are JSON by name; nothing returned is `null`.
- `encode` and `decode`: a value to JSON and back by its type hint. The core values (`Node`,
  `Body`, `Author`, `Item`, `ItemData`, `Ending`) are the log's own encoding, from `tau._core`,
  so a node has one description in the system. An event is `encode_event`: its fields and
  `kind`, the class name; a `Live` delta is tagged the same way.
- `ERRORS`, `error_of`, `exception_for`: the error table. A server sends the code of the first
  class in an exception's MRO that the table names, and a client raises the class the code names.
- `schema()`: the JSON Schema 2020-12 of all of it, generated from the above and the core values'
  `schemars` derives, never written by hand. `docs/protocol.schema.json` holds it.
"""

import dataclasses
import inspect
import json
import types
import typing
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, cast

from tau import _core
from tau._commands import CommandFailed
from tau._config import ConfigError
from tau._core import (
    Author,
    Body,
    Busy,
    CursorClosed,
    Ending,
    Item,
    ItemData,
    Node,
    NoSuchCursor,
    NoSuchLabel,
    NoSuchNode,
)
from tau._values import Json
from tau.client import Client
from tau.commandline import UnknownCommand
from tau.events import CutOff, Event, LiveDelta

PROTOCOL: Final = 1
"""The wire protocol's version. D12: a version marks a released protocol. It goes up by one at a
release whose `docs/protocol.schema.json` differs from the last tag's, never per commit."""

# Errors


class ProtocolError(RuntimeError):
    """The other end broke the protocol: a message that is not JSON-RPC, a method that does not
    exist, a value that does not fit. Not a `ConnectionError`: connecting again would not help."""


class ProtocolMismatch(ProtocolError):
    """`hello` named a protocol the server does not speak. `server` is the one it does."""

    def __init__(self, message: str, server: int) -> None:
        super().__init__(message)
        self.server = server


@dataclass(frozen=True)
class WireError:
    """One row of the error table: the `code` and `name` on the wire, the class a `RemoteClient`
    raises for it, and whether a server sends this code for an exception of that class (`sent`;
    otherwise the server sends it for its own reasons, named by `when`)."""

    code: int
    name: str
    raises: type[BaseException]
    when: str
    sent: bool = True
    data: Callable[[BaseException], Json] | None = None
    build: Callable[[str, Json], BaseException] | None = None


def _mismatch_data(error: BaseException) -> Json:
    return {"server": cast("ProtocolMismatch", error).server}


def _mismatch(message: str, data: Json) -> BaseException:
    return ProtocolMismatch(message, cast("int", data["server"]))


def _unknown_data(error: BaseException) -> Json:
    return {"name": cast("UnknownCommand", error).name}


def _unknown(_message: str, data: Json) -> BaseException:
    return UnknownCommand(cast("str", data["name"]))


ERRORS: Final = (
    WireError(-32700, "ParseError", ProtocolError, "the message is not JSON", sent=False),
    WireError(
        -32600, "InvalidRequest", ProtocolError, "not a request, or before `hello`", sent=False
    ),
    WireError(-32601, "MethodNotFound", ProtocolError, "no such method", sent=False),
    WireError(
        -32602, "InvalidParams", TypeError, "params do not fit the call's signature", sent=False
    ),
    WireError(-32603, "Internal", RuntimeError, "an exception the table does not name", sent=False),
    WireError(-32001, "Busy", Busy, "a step on a cursor that is not at rest"),
    WireError(-32002, "NoSuchCursor", NoSuchCursor, "a cursor that was never opened"),
    WireError(-32003, "CursorClosed", CursorClosed, "a cursor that is closed"),
    WireError(-32004, "NoSuchNode", NoSuchNode, "a node that is not in the tree"),
    WireError(-32005, "NoSuchLabel", NoSuchLabel, "a label that is not set"),
    WireError(
        -32006,
        "ProtocolMismatch",
        ProtocolMismatch,
        "`hello` named another protocol",
        data=_mismatch_data,
        build=_mismatch,
    ),
    WireError(
        -32007, "NotFound", LookupError, "a closed or unknown question, an unknown command in `run`"
    ),
    WireError(
        -32008,
        "UnknownCommand",
        UnknownCommand,
        "`parse_line`: send the line as text, with a warning",
        data=_unknown_data,
        build=_unknown,
    ),
    WireError(-32009, "Refused", ValueError, "a value the driver refuses; the message says why"),
    WireError(-32010, "NotYours", PermissionError, "a question for another connected client"),
    WireError(-32011, "CommandFailed", CommandFailed, "a command raised or was aborted"),
    WireError(-32012, "ConfigError", ConfigError, "options that cannot be read or changed"),
    WireError(-32013, "Stopped", ConnectionError, "the driver has stopped or failed"),
    WireError(-32014, "CutOff", CutOff, "the client fell behind: only in `end`"),
)
"""The error table (D21 section 5), in code order of appearance."""

_BY_CODE: Final = {row.code: row for row in ERRORS}
_SENT: Final = {row.raises: row for row in ERRORS if row.sent}
_INTERNAL: Final = _BY_CODE[-32603]


def error_of(error: BaseException) -> WireError:
    """The row of the first class in `error`'s MRO that the table names (`Internal` for none)."""
    for cls in type(error).__mro__:
        row = _SENT.get(cls)
        if row is not None:
            return row
    return _INTERNAL


def error_object(error: BaseException) -> Json:
    """The JSON-RPC error object for an exception a call raised: `code`, `message` (`str(exc)`;
    `"Type: msg"` for an exception the table does not name) and the row's `data`, if it has
    any."""
    row = error_of(error)
    message = f"{type(error).__name__}: {error}" if row is _INTERNAL else str(error)
    out: Json = {"code": row.code, "message": message}
    if row.data is not None:
        out["data"] = row.data(error)
    return out


def exception_for(code: int, message: str, data: Json | None = None) -> BaseException:
    """The exception a `RemoteClient` raises for an error object. `ProtocolError` for a code the
    table does not have, or data its row needs and lacks."""
    row = _BY_CODE.get(code)
    if row is None:
        return ProtocolError(f"unknown error code {code}: {message}")
    try:
        return row.build(message, data or {}) if row.build is not None else row.raises(message)
    except KeyError as missing:
        return ProtocolError(f"error {row.name} lacks {missing} in its data: {message}")


def end_params(error: BaseException | None) -> Json:
    """The params of the notification `end`: `{"error": null}` when the driver stopped, or the
    error object of the `ConnectionError` the stream ends with."""
    return {"error": None if error is None else error_object(error)}


def end_error(params: Mapping[str, object]) -> BaseException | None:
    """What an `end` notification's params say the stream ends with: `None` (it just ends),
    `CutOff`, or the `ConnectionError` of a stopped driver."""
    raw = params.get("error")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProtocolError(f"end carries an error object or null, not {raw!r}")
    found = cast("Json", raw)
    error = exception_for(
        cast("int", found.get("code")), cast("str", found.get("message")), _data(found)
    )
    if isinstance(error, ConnectionError):
        return error
    raise ProtocolError(f"end carries {_BY_CODE[cast('int', found['code'])].name}: {error}")


def _data(error: Json) -> Json | None:
    data = error.get("data")
    return cast("Json", data) if isinstance(data, dict) else None


# Values

CORE: Final = (Node, Body, Author, Item, ItemData, Ending)
CORE_NAMES: Final = {cls: cls.__name__ for cls in CORE}
_ATOMS: Final = {str: "string", bool: "boolean", int: "integer", float: "number"}
_PAIR: Final = 2  # `tuple[X, ...]` has two arguments, the second an `Ellipsis`


def _members(hint: Any) -> tuple[Any, ...]:
    """The alternatives of a union hint, or the hint alone."""
    if typing.get_origin(hint) in (typing.Union, types.UnionType):
        return typing.get_args(hint)
    return (hint,)


def _tagged() -> frozenset[type]:
    """The dataclasses that are one of several in a union (the events, a `Live` delta): each
    carries its class name as `kind` so a decoder can tell them apart."""
    return frozenset(m for union in (Event, LiveDelta) for m in _members(union))


TAGGED: Final = _tagged()


def shape(hint: Any) -> tuple[str, tuple[Any, ...]]:
    """What a type hint is, in the words both the decoder and the schema use: `any`, `null`,
    `core` (one of the six core values), `record` (a dataclass), `atom` (`str`, `int`, `float`
    or `bool`), `union`, `literal`, `map`, `tuple` (`tuple[X, ...]`), `fixed` (`tuple[A, B]`),
    `list` (any other collection), each with the parts that matter. `TypeError` for a hint the
    wire has no word for."""
    if hint is object or hint is Any:
        return "any", ()
    if hint is None or hint is type(None):
        return "null", ()
    if hasattr(hint, "__supertype__"):  # NewType
        return shape(hint.__supertype__)
    if isinstance(hint, type):
        for kind, test in (("core", CORE_NAMES), ("atom", _ATOMS)):
            if hint in test:
                return kind, (hint,)
        if dataclasses.is_dataclass(hint):
            return "record", (hint,)
    return _generic_shape(hint)


def _generic_shape(hint: Any) -> tuple[str, tuple[Any, ...]]:
    """`shape` of a hint that is a generic alias, a union or a literal."""
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin in (typing.Union, types.UnionType):
        return "union", args
    if origin is Literal:
        return "literal", args
    if origin in (dict, Mapping):
        return "map", (args[1] if args else object,)
    if origin is tuple:
        if len(args) == _PAIR and args[1] is Ellipsis:
            return "tuple", (args[0],)
        return "fixed", args
    if origin in (list, Collection, Sequence):
        return "list", (args[0] if args else object,)
    raise TypeError(f"the wire has no encoding for {hint!r}")


def encode(value: object) -> object:
    """`value` as JSON data: scalars as they are, the core values in the log's encoding, a
    dataclass as an object of its fields (and `kind` if it is one of an event union), a tuple or
    list as an array, a mapping as an object. `TypeError` for what is not JSON (a `Path`): the
    wire carries only what `Client` documents."""
    match value:
        case None | bool() | int() | float() | str():
            return value
        case Node() | Body() | Author() | Item() | ItemData() | Ending():
            return json.loads(_core.wire_encode(value))
        case _ if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return _fields(value)
        case tuple() | list():
            return [encode(v) for v in cast("Sequence[object]", value)]
        case Mapping():
            return {_key(k): encode(v) for k, v in cast("Mapping[object, object]", value).items()}
        case _:
            raise TypeError(f"{type(value).__name__} is not JSON: {value!r}")


def _key(key: object) -> str:
    if not isinstance(key, str):
        raise TypeError(f"a JSON object has string keys, not {key!r}")
    return key


def _fields(value: object) -> Json:
    out: Json = {"kind": type(value).__name__} if type(value) in TAGGED else {}
    for field in dataclasses.fields(cast("Any", value)):
        out[field.name] = encode(getattr(value, field.name))
    return out


def _need(ok: object, wanted: str, data: object) -> None:
    if not ok:
        raise TypeError(f"expected {wanted}, not {data!r}")


def _object(data: object) -> Json:
    _need(isinstance(data, dict), "an object", data)
    return cast("Json", data)


def _decode_atom(parts: tuple[Any, ...], data: object) -> object:
    hint: type = parts[0]
    if hint is float and isinstance(data, int) and not isinstance(data, bool):
        return float(data)
    fits = isinstance(data, hint) and (hint is bool or not isinstance(data, bool))
    _need(fits, hint.__name__, data)
    return data


def _decode_union(args: tuple[Any, ...], data: object) -> object:
    if data is None and type(None) in args:
        return None
    options = [a for a in args if a is not type(None)]
    if len(options) == 1:
        return decode(options[0], data)
    found = _object(data)
    _need("kind" in found, "an object with `kind`", data)
    kind = found["kind"]
    for option in options:
        if getattr(option, "__name__", None) == kind:
            return decode(option, data)
    raise TypeError(f"no kind {kind!r}; the kinds are {[o.__name__ for o in options]}")


def _decode_record(parts: tuple[Any, ...], data: object) -> object:
    cls: type = parts[0]
    found = _object(data)
    hints = typing.get_type_hints(cls)
    built: Json = {}
    for field in dataclasses.fields(cast("Any", cls)):
        if field.name in found:
            built[field.name] = decode(hints[field.name], found[field.name])
        elif field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
            raise TypeError(f"{cls.__name__} lacks {field.name!r}")
    return cls(**built)


def _array(data: object) -> list[object]:
    _need(isinstance(data, list), "an array", data)
    return cast("list[object]", data)


def _decode_map(parts: tuple[Any, ...], data: object) -> object:
    return {k: decode(parts[0], v) for k, v in _object(data).items()}


def _decode_fixed(parts: tuple[Any, ...], data: object) -> object:
    items = _array(data)
    _need(len(items) == len(parts), f"an array of {len(parts)}", data)
    return tuple(decode(h, v) for h, v in zip(parts, items, strict=True))


def _decode_literal(parts: tuple[Any, ...], data: object) -> object:
    _need(data in parts, " or ".join(map(repr, parts)), data)
    return data


_DECODE: Final[dict[str, Callable[[tuple[Any, ...], object], object]]] = {
    "any": lambda _, data: data,
    "null": lambda _, data: _need(data is None, "null", data),
    "core": lambda parts, data: _core.wire_decode(CORE_NAMES[parts[0]], json.dumps(data)),
    "record": _decode_record,
    "atom": _decode_atom,
    "union": _decode_union,
    "literal": _decode_literal,
    "map": _decode_map,
    "tuple": lambda parts, data: tuple(decode(parts[0], v) for v in _array(data)),
    "fixed": _decode_fixed,
    "list": lambda parts, data: [decode(parts[0], v) for v in _array(data)],
}


def decode(hint: Any, data: object) -> object:
    """The value of type `hint` that JSON `data` encodes, rebuilt as the same frozen value, so a
    remote event equals the local one. `TypeError` naming what does not fit."""
    kind, parts = shape(hint)
    return _DECODE[kind](parts, data)


def encode_event(event: Event) -> Json:
    """The params of the notification `event`: the event's fields and its `kind`."""
    return cast("Json", encode(event))


def decode_event(params: Mapping[str, object]) -> Event:
    """The event the params of an `event` notification say. `TypeError` if they say none."""
    return cast("Event", decode(Event, dict(params)))


# Methods


@dataclass(frozen=True)
class Welcome:
    """What `hello` answers: the `protocol` the server speaks, its τ `version`, the `session` id
    it serves, the `cwd` that session runs in (paths complete there), and its `pid`."""

    protocol: int
    version: str
    session: str
    cwd: str
    pid: int


class Handshake(Protocol):
    """The one message that is not a `Client` call, as a signature."""

    async def hello(self, protocol: int, client: str, buffer: int | None = None) -> Welcome:
        """The first message of a connection: the protocol the client speaks, its stable
        `client` id (`john@laptop:vscode`) and the `buffer` of its event stream (the server's
        default if left out). A different protocol is `ProtocolMismatch`. The server answers
        `Welcome`."""
        ...


@dataclass(frozen=True)
class Param:
    """One parameter of a method: `hint`, and whether a call must give it."""

    name: str
    hint: object
    required: bool


@dataclass(frozen=True)
class Method:
    """One call on the wire: its `name` (a JSON-RPC method), `params` (by name only), and the
    `result` hint (`None` for a call that returns nothing)."""

    name: str
    params: tuple[Param, ...]
    result: object
    doc: str


def _method(fn: Callable[..., object]) -> Method:
    hints = typing.get_type_hints(fn)
    params = tuple(
        Param(p.name, hints[p.name], p.default is inspect.Parameter.empty)
        for p in inspect.signature(fn).parameters.values()
        if p.name != "self"
    )
    return Method(fn.__name__, params, hints["return"], inspect.getdoc(fn) or "")


NOT_CALLS: Final = ("close",)
"""`Client` methods that are not messages: closing the connection is how a client disconnects."""


def _methods() -> dict[str, Method]:
    calls = [
        v
        for k, v in vars(Client).items()
        if inspect.iscoroutinefunction(v) and not k.startswith("_") and k not in NOT_CALLS
    ]
    return {m.name: m for m in map(_method, [Handshake.hello, *calls])}


METHODS: Final = _methods()
"""Every message a client may send, by name: `hello`, then one per `Client` call."""


def decode_params(name: str, params: Mapping[str, object]) -> dict[str, object]:
    """The keyword arguments for call `name` from its JSON params. `TypeError` for a name the
    call lacks, a missing required one, or a value that does not fit its hint."""
    method = METHODS[name]
    known = {p.name: p for p in method.params}
    unknown = sorted(set(params) - set(known))
    if unknown:
        raise TypeError(f"{name} takes {list(known)}, not {unknown}")
    missing = [p.name for p in method.params if p.required and p.name not in params]
    if missing:
        raise TypeError(f"{name} needs {missing}")
    try:
        return {k: decode(known[k].hint, v) for k, v in params.items()}
    except TypeError as e:
        raise TypeError(f"{name}: {e}") from None


def encode_params(name: str, args: Mapping[str, object]) -> Json:
    """The JSON params of a call to `name` with these keyword arguments. `TypeError` for an
    argument the call lacks or a value that is not JSON."""
    known = {p.name for p in METHODS[name].params}
    unknown = sorted(set(args) - known)
    if unknown:
        raise TypeError(f"{name} takes {sorted(known)}, not {unknown}")
    return {k: encode(v) for k, v in args.items()}


def encode_result(name: str, value: object) -> object:
    """The JSON a server answers call `name` with."""
    return encode(value) if METHODS[name].result is not None else None


def decode_result(name: str, data: object) -> object:
    """The value a client returns for call `name` from the JSON it was answered with.
    `TypeError` if it does not fit."""
    return decode(METHODS[name].result, data)


# The schema

_DRAFT = "https://json-schema.org/draft/2020-12/schema"


def _ref(name: str) -> Json:
    return {"$ref": f"#/$defs/{name}"}


class _Defs:
    """The `$defs` being collected: a dataclass is defined where a hint first reaches it."""

    def __init__(self, core: Json) -> None:
        self.defs: Json = dict(core)

    def define(self, name: str, schema: Mapping[str, object]) -> None:
        if name in self.defs:
            raise ValueError(f"the schema defines {name!r} twice")
        self.defs[name] = schema

    def hint(self, hint: Any) -> Json:
        """The schema of a type hint."""
        kind, parts = shape(hint)
        return getattr(self, f"_{kind}")(parts)

    def _any(self, _: tuple[Any, ...]) -> Json:
        return {}

    def _null(self, _: tuple[Any, ...]) -> Json:
        return {"type": "null"}

    def _core(self, parts: tuple[Any, ...]) -> Json:
        return _ref(CORE_NAMES[parts[0]])

    def _atom(self, parts: tuple[Any, ...]) -> Json:
        return {"type": _ATOMS[parts[0]]}

    def _union(self, parts: tuple[Any, ...]) -> Json:
        return {"anyOf": [self.hint(a) for a in parts]}

    def _literal(self, parts: tuple[Any, ...]) -> Json:
        return {"enum": list(parts)}

    def _map(self, parts: tuple[Any, ...]) -> Json:
        return {"type": "object"}

    def _tuple(self, parts: tuple[Any, ...]) -> Json:
        return {"type": "array", "items": self.hint(parts[0])}

    _list = _tuple

    def _fixed(self, parts: tuple[Any, ...]) -> Json:
        return {
            "type": "array",
            "prefixItems": [self.hint(a) for a in parts],
            "minItems": len(parts),
            "maxItems": len(parts),
        }

    def _record(self, parts: tuple[Any, ...]) -> Json:
        """The `$ref` to the definition of dataclass `parts[0]`, made if it is new."""
        cls: type = parts[0]
        if cls.__name__ not in self.defs:
            hints = typing.get_type_hints(cls)
            properties: Json = {}
            required: list[str] = []
            if cls in TAGGED:
                properties["kind"] = {"const": cls.__name__}
                required.append("kind")
            for field in dataclasses.fields(cast("Any", cls)):
                properties[field.name] = self.hint(hints[field.name])
                if (
                    field.default is dataclasses.MISSING
                    and field.default_factory is dataclasses.MISSING
                ):
                    required.append(field.name)
            self.define(
                cls.__name__,
                {"description": inspect.getdoc(cls) or "", "type": "object"}
                | {"properties": properties}
                | ({"required": required} if required else {}),
            )
        return _ref(cls.__name__)

    def method(self, method: Method) -> None:
        """Define call `method`: its params object (closed), its `x-result` (open)."""
        required = [p.name for p in method.params if p.required]
        self.define(
            method.name,
            {
                "description": method.doc,
                "type": "object",
                "properties": {p.name: self.hint(p.hint) for p in method.params},
            }
            | ({"required": required} if required else {})
            | {"additionalProperties": False, "x-result": self.hint(method.result)},
        )


def _error_defs() -> dict[str, Json]:
    codes = [row.code for row in ERRORS]
    return {
        "ErrorCode": {
            "description": "The error table (D21 section 5).",
            "enum": codes,
            "x-names": [row.name for row in ERRORS],
            "x-raises": [row.raises.__name__ for row in ERRORS],
        },
        "Error": {
            "description": "A JSON-RPC error object; `data` holds what a row needs to rebuild "
            "its exception (`ProtocolMismatch`: `server`; `UnknownCommand`: `name`).",
            "type": "object",
            "properties": {
                "code": _ref("ErrorCode"),
                "message": {"type": "string"},
                "data": {"type": "object"},
            },
            "required": ["code", "message"],
        },
        "End": {
            "description": "The params of the last message: the notification `end`. `error` is "
            "null when the driver stopped, else `CutOff` or `Stopped`.",
            "type": "object",
            "properties": {"error": {"anyOf": [{"type": "null"}, _ref("Error")]}},
            "required": ["error"],
        },
    }


def schema() -> Json:
    """The JSON Schema 2020-12 of the wire, as a dict: `$defs/<method>` is a call's params
    object with its `x-result`, `$defs/Event` a `oneOf` over `kind`, `$defs/End`,
    `$defs/ErrorCode` an enum with `x-names`, and `x-protocol` at the top. Params are closed,
    results open. Nothing here is listed by hand: it is read from `Client`, the event
    dataclasses, `ERRORS` and the core values' derives."""
    defs = _Defs(cast("Json", json.loads(_core.wire_schema())))
    for method in METHODS.values():
        defs.method(method)
    members = [defs.hint(m) for m in _members(Event)]
    defs.define(
        "Event",
        {"description": "The params of the notification `event`.", "oneOf": members},
    )
    for name, definition in _error_defs().items():
        defs.define(name, definition)
    return {
        "$schema": _DRAFT,
        "title": "τ wire protocol",
        "description": "JSON-RPC 2.0, one message per WebSocket text message (D21).",
        "x-protocol": PROTOCOL,
        "$defs": dict(sorted(defs.defs.items())),
    }


def schema_text() -> str:
    """`schema()` as the text of `docs/protocol.schema.json`: two-space indent, a final newline.
    What `tau serve --schema` prints."""
    return json.dumps(schema(), indent=2, ensure_ascii=False) + "\n"
