"""JSON Schema from Python annotations, for the two wires' published schemas (docs/TAU-SERVE.md §5).

:class:`SchemaBuilder` turns dataclasses, TypedDicts, pydantic models and the
markers below into one ``$defs`` map, each named type defined once and referenced
as ``#/$defs/Name``. RPC's capability document and serve's schema are both built
with it, so a record shared by the two has one definition. The Markdown helpers
at the bottom render a ``$defs`` entry as a field table for either reference.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import types
import typing
from dataclasses import dataclass, field
from typing import (
    Annotated,
    Any,
    Literal,
    Never,
    NotRequired,
    Required,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

__all__ = [
    "Given",
    "Named",
    "Pattern",
    "SchemaBuilder",
    "Shape",
    "Tagged",
    "cell_markdown",
    "definition_markdown",
    "summary",
    "table_markdown",
    "type_markdown",
    "typed",
]


@dataclass(frozen=True)
class Named:
    """Marks a union as one ``$defs`` entry whose members a ``const`` field tells apart.

    Attributes:
        name: The ``$defs`` key.
        doc: Its description.
    """

    name: str
    doc: str


@dataclass(frozen=True)
class Shape:
    """Marks a ``dict[str, Any]`` the sender forwards as having the JSON shape of ``hint``."""

    hint: Any


@dataclass(frozen=True)
class Tagged:
    """Marks a record sent flattened with one more ``const`` field, ``{tag: value, **fields}``.

    Attributes:
        name: The ``$defs`` key of the tagged form.
        tag: The added field's name.
        value: Its constant value.
    """

    name: str
    tag: str
    value: str


@dataclass(frozen=True)
class Pattern:
    """Marks a ``str`` as matching ``regex``."""

    regex: str


@dataclass(frozen=True, eq=False)
class Given:
    """Marks a value whose JSON Schema is written out whole, as RPC's command table holds it.

    Attributes:
        name: The ``$defs`` key.
        schema: The schema, used as is but for ``types``.
        types: Nodes of ``schema`` it leaves open, and the type each has, as
            :func:`typed` takes them.
    """

    name: str
    schema: dict[str, Any]
    types: dict[str, Any] = field(default_factory=dict)


_PYDANTIC = "pydantic"
"""The owner recorded for a ``$defs`` entry pydantic generated."""


def _attribute_docs(cls: type) -> dict[str, str]:
    """A Google docstring's ``Attributes:`` entries, by name, each joined onto one line."""
    lines = (cls.__doc__ or "").splitlines()
    docs: dict[str, str] = {}
    current: str | None = None
    current_indent = 0
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == "Attributes:":
            inside = True
            continue
        if not inside:
            continue
        if not stripped:
            current = None
            continue
        indent = len(line) - len(line.lstrip())
        name, sep, text = stripped.partition(": ")
        if sep and name.isidentifier() and (current is None or indent <= current_indent):
            current, current_indent = name, indent
            docs[name] = text
        elif current is not None:
            docs[current] += " " + stripped
    return docs


def _markdown(text: str) -> str:
    """Docstring prose as a schema description: one line, Sphinx roles as Markdown code."""
    text = re.sub(r":\w+:`~?([^`]+)`", r"`\1`", " ".join(text.split()))
    return text.replace("``", "`")


def summary(cls: type) -> str:
    """A docstring's first paragraph, on one line."""
    return _markdown((cls.__doc__ or "").strip().split("\n\n", 1)[0])


def _clean_pydantic(node: Any) -> Any:
    """Pydantic schema without titles, and with each ``const`` property required."""
    if isinstance(node, list):
        return [_clean_pydantic(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in ("properties", "$defs"):
            out[key] = {name: _clean_pydantic(sub) for name, sub in value.items()}
        else:
            out[key] = _clean_pydantic(value)
    consts = [
        n for n, s in out.get("properties", {}).items() if isinstance(s, dict) and "const" in s
    ]
    if consts:
        required = list(out.get("required", []))
        out["required"] = required + [n for n in consts if n not in required]
    return out


def _is_const(hint: Any) -> bool:
    """Whether ``hint`` is a one-value ``Literal``: a discriminator, always sent."""
    return get_origin(hint) is Literal and len(get_args(hint)) == 1


class SchemaBuilder:
    """Builds a ``$defs`` map, one entry per named type, refusing two types under one name.

    Records reachable from :meth:`request` are closed (``additionalProperties:
    false``), because a server refuses an unknown request field. Everything else
    is open, because a client must tolerate an added field.

    Args:
        open_shapes: TypedDicts whose undeclared keys are payload by design, sent
            with ``additionalProperties: true``.
    """

    def __init__(self, open_shapes: frozenset[Any] = frozenset()) -> None:
        self.defs: dict[str, Any] = {}
        self._owners: dict[str, Any] = {}
        self._closed = False
        self._open_shapes = open_shapes

    def _define(self, name: str, owner: Any, build: Any) -> dict[str, Any]:
        """Register ``$defs[name]`` once."""
        ref = {"$ref": f"#/$defs/{name}"}
        if name in self._owners:
            if self._owners[name] != owner:
                raise TypeError(f"two types want $defs/{name}: {self._owners[name]!r}, {owner!r}")
            if self._closed and self.defs[name].get("additionalProperties") is not False:
                raise TypeError(f"$defs/{name} is sent open and also used in a request")
            return ref
        self._owners[name] = owner
        self.defs[name] = {}
        self.defs[name] = build()
        return ref

    def request(self, cls: type) -> None:
        """Define a request's ``$defs`` entry, closed with everything it reaches."""
        self._closed = True
        try:
            self.of(cls)
        finally:
            self._closed = False

    def of(self, hint: Any) -> Any:
        """JSON Schema for one annotation."""
        origin = get_origin(hint)
        if hint is Any or hint is object:
            return {}
        if hint is Never:
            return False
        if hint is type(None):
            return {"type": "null"}
        if hint is str:
            return {"type": "string"}
        if hint is bool:
            return {"type": "boolean"}
        if hint is int:
            return {"type": "integer"}
        if hint is float:
            return {"type": "number"}
        if origin is Annotated:
            return self._annotated(hint)
        if origin in (NotRequired, Required):
            return self.of(get_args(hint)[0])
        if origin is Literal:
            values = list(get_args(hint))
            return {"const": values[0]} if len(values) == 1 else {"enum": values}
        if origin in (typing.Union, types.UnionType):
            return {"anyOf": [self.of(arg) for arg in get_args(hint)]}
        if origin in (list, tuple):
            args = get_args(hint)
            return {"type": "array", "items": self.of(args[0])}
        if origin is dict:
            return {"type": "object", "additionalProperties": self.of(get_args(hint)[1])}
        if isinstance(hint, type) and hasattr(hint, "model_json_schema"):
            return self._pydantic(hint)
        if is_typeddict(hint):
            return self._define(hint.__name__, hint, lambda: self._typed_dict(hint))
        if isinstance(hint, type) and dataclasses.is_dataclass(hint):
            return self._define(hint.__name__, hint, lambda: self.record(hint))
        raise TypeError(f"no JSON Schema for annotation {hint!r}")

    def _annotated(self, hint: Any) -> Any:
        base, marker = get_args(hint)[0], hint.__metadata__[0]
        if isinstance(marker, Shape):
            return self.of(marker.hint)
        if isinstance(marker, Pattern):
            return {"type": "string", "pattern": marker.regex}
        if isinstance(marker, Given):
            return self._define(
                marker.name,
                marker.name,
                lambda: typed(self, marker.schema, marker.types, marker.name),
            )
        if isinstance(marker, Named):
            return self._define(
                marker.name,
                hint,
                lambda: {"oneOf": [self.of(m) for m in get_args(base)], "description": marker.doc},
            )
        if isinstance(marker, Tagged):

            def tagged() -> dict[str, Any]:
                record = self.record(base)
                record["properties"] = {marker.tag: {"const": marker.value}, **record["properties"]}
                record["required"] = [marker.tag, *record["required"]]
                return record

            return self._define(marker.name, hint, tagged)
        raise TypeError(f"no JSON Schema for annotation {hint!r}")

    def record(self, cls: type) -> dict[str, Any]:
        """A dataclass: its fields; required are those with no default, and every constant."""
        hints = get_type_hints(cls, include_extras=True)
        docs = _attribute_docs(cls)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for f in dataclasses.fields(cls):
            prop = self.of(hints[f.name])
            if f.name in docs and isinstance(prop, dict):
                prop = {**prop, "description": _markdown(docs[f.name])}
            properties[f.name] = prop
            no_default = (
                f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
            )
            if no_default or _is_const(hints[f.name]):
                required.append(f.name)
        return self._object(cls, properties, required)

    def _typed_dict(self, cls: Any) -> dict[str, Any]:
        """A TypedDict: its keys, each required unless marked ``NotRequired``.

        Read off the hints, because under postponed annotations the class's own
        ``__required_keys__`` does not see ``NotRequired``.
        """
        hints = get_type_hints(cls, include_extras=True)
        properties = {name: self.of(hint) for name, hint in hints.items()}
        required = [name for name, hint in hints.items() if get_origin(hint) is not NotRequired]
        schema = self._object(cls, properties, required)
        if cls in self._open_shapes:
            schema["additionalProperties"] = True
        return schema

    def _object(self, cls: Any, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "object", "properties": properties, "required": required}
        if self._closed:
            schema["additionalProperties"] = False
        if cls.__doc__:
            schema["description"] = summary(cls)
        return schema

    def _pydantic(self, model: Any) -> dict[str, Any]:
        """A pydantic model's own schema, its nested ``$defs`` hoisted beside it."""
        generated = _clean_pydantic(model.model_json_schema(ref_template="#/$defs/{model}"))
        for name, sub in {**generated.pop("$defs", {}), model.__name__: generated}.items():
            owner = self._owners.get(name)
            if owner is None:
                self._owners[name] = _PYDANTIC
                self.defs[name] = sub
            elif owner != _PYDANTIC or self.defs[name] != sub:
                raise TypeError(f"two types want $defs/{name}")
        return {"$ref": f"#/$defs/{model.__name__}"}


def typed(
    s: SchemaBuilder, schema: dict[str, Any], types: dict[str, Any], where: str
) -> dict[str, Any]:
    """``schema`` with each node ``types`` names replaced by its type's schema, its description kept.

    A path steps into ``properties`` by name and into ``items`` by ``[]``
    (``tools[].parameters``).

    Raises:
        KeyError: a path that names no node, so a table cannot outlive its schema.
    """
    result = copy.deepcopy(schema)
    for path, hint in types.items():
        parent: dict[str, Any] = result
        steps = path.split(".")
        for step in steps[:-1]:
            name = step.removesuffix("[]")
            parent = parent["properties"][name]
            if step.endswith("[]"):
                parent = parent["items"]
        last = steps[-1]
        if last not in parent.get("properties", {}):
            raise KeyError(f"{where}: no node {path!r} to type")
        described = parent["properties"][last].get("description")
        node = dict(s.of(hint))
        parent["properties"][last] = (
            {**node, "description": described} if described is not None else node
        )
    return result


# ─── Markdown ────────────────────────────────────────────────────────────


def type_markdown(schema: Any) -> str:
    """A short human spelling of one property's schema; a ``$ref`` links to its heading."""
    if schema is False:
        return "absent"
    if "$ref" in schema:
        name = str(schema["$ref"]).rsplit("/", 1)[-1]
        return f"[{name}](#{name.lower()})"
    if "const" in schema:
        return f"`{json.dumps(schema['const'])}`"
    if "enum" in schema:
        return " \\| ".join(f"`{json.dumps(v)}`" for v in schema["enum"])
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return " \\| ".join(type_markdown(s) for s in schema[key])
    kinds = schema.get("type")
    if isinstance(kinds, list):
        return " \\| ".join("`null`" if k == "null" else str(k) for k in kinds)
    if kinds == "array":
        return f"list of {type_markdown(schema.get('items', {}))}"
    if kinds == "object" or "additionalProperties" in schema:
        values = schema.get("additionalProperties")
        if isinstance(values, dict) and values:
            return f"object of {type_markdown(values)}"
        return "object"
    if "pattern" in schema:
        return f"string matching `{schema['pattern']}`"
    return str(schema.get("type", "any"))


def cell_markdown(text: str) -> str:
    """``text`` safe inside a Markdown table cell."""
    return " ".join(text.split()).replace("|", "\\|")


def table_markdown(schema: dict[str, Any], skip: tuple[str, ...] = ()) -> list[str]:
    """One object schema as a field table."""
    properties = {k: v for k, v in schema.get("properties", {}).items() if k not in skip}
    if not properties:
        return ["No fields."]
    required = set(schema.get("required", []))
    rows = ["| Field | Type | Required | Description |", "|---|---|---|---|"]
    for name, prop in properties.items():
        described = prop.get("description", "") if isinstance(prop, dict) else ""
        rows.append(
            f"| `{name}` | {type_markdown(prop)} | {'yes' if name in required else 'no'} "
            f"| {cell_markdown(described)} |"
        )
    return rows


def definition_markdown(name: str, schema: Any) -> list[str]:
    """One ``$defs`` entry under a level-3 heading: its description, then its fields or members."""
    out = [f"### {name}", ""]
    if isinstance(schema, dict) and schema.get("description"):
        out += [cell_markdown(str(schema["description"]).split("\n\n", 1)[0]), ""]
    if isinstance(schema, dict) and "oneOf" in schema:
        out.append(
            "One of: " + ", ".join(type_markdown(member) for member in schema["oneOf"]) + "."
        )
    elif isinstance(schema, dict):
        out.extend(table_markdown(schema))
    return out
