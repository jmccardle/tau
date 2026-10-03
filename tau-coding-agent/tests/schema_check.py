"""A JSON Schema validator for the subset ``docs/serve-protocol.schema.json`` uses.

``jsonschema`` is not a dependency of this repo, and the serve protocol's schema
uses a dozen keywords, so the tests validate real frames with this instead. An
unknown keyword raises rather than passing, so the subset cannot silently shrink.

``strict`` also refuses a key an object schema does not declare where the schema
leaves ``additionalProperties`` unset: the daemon's records are open for clients,
and the tests hold the daemon to sending only what it declares.
"""

from __future__ import annotations

import re
from typing import Any

_ANNOTATIONS = {"description", "default", "title", "$schema", "x-result", "x-data"}
_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class SchemaError(AssertionError):
    """A value the schema refuses; the message names the JSON path."""


def validate(value: Any, schema: Any, root: dict[str, Any], *, strict: bool = False) -> None:
    """Raise :class:`SchemaError` unless ``value`` satisfies ``schema``."""
    errors = _errors(value, schema, root, "$", strict)
    if errors:
        raise SchemaError("\n".join(errors[:10]))


def _same(a: Any, b: Any) -> bool:
    """JSON equality: ``True`` is not ``1``."""
    return type(a) is type(b) and a == b if isinstance(a, bool) or isinstance(b, bool) else a == b


def _errors(value: Any, schema: Any, root: dict[str, Any], path: str, strict: bool) -> list[str]:
    if schema is True:
        return []
    if schema is False:
        return [f"{path}: no value is allowed here"]
    unknown = (
        set(schema)
        - _ANNOTATIONS
        - {
            "$ref",
            "type",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "const",
            "enum",
            "anyOf",
            "oneOf",
            "not",
            "pattern",
            "minimum",
        }
    )
    if unknown:
        raise NotImplementedError(f"{path}: keyword(s) {sorted(unknown)} not in the subset")
    errors: list[str] = []
    if "$ref" in schema:
        target: Any = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        errors += _errors(value, target, root, path, strict)
    types = schema.get("type")
    allowed = [types] if isinstance(types, str) else types
    if allowed is not None and not any(_TYPES[name](value) for name in allowed):
        return errors + [f"{path}: {value!r:.80} is not {types}"]
    if "const" in schema and not _same(value, schema["const"]):
        errors.append(f"{path}: {value!r:.80} is not {schema['const']!r}")
    if "enum" in schema and not any(_same(value, v) for v in schema["enum"]):
        errors.append(f"{path}: {value!r:.80} is not one of {schema['enum']}")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
    if "minimum" in schema and isinstance(value, int | float) and value < schema["minimum"]:
        errors.append(f"{path}: {value} is below {schema['minimum']}")
    if isinstance(value, dict) and ("properties" in schema or "additionalProperties" in schema):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing {name!r}")
        extra_schema = schema.get("additionalProperties", False if strict and properties else True)
        for name, item in value.items():
            where = f"{path}.{name}"
            if name in properties:
                errors += _errors(item, properties[name], root, where, strict)
            else:
                errors += _errors(item, extra_schema, root, where, strict)
    elif isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing {name!r}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors += _errors(item, schema["items"], root, f"{path}[{index}]", strict)
    if "anyOf" in schema:
        branches = [_errors(value, s, root, path, strict) for s in schema["anyOf"]]
        if all(branches):
            errors.append(f"{path}: matches no anyOf branch: " + " / ".join(b[0] for b in branches))
    if "oneOf" in schema:
        branches = [_errors(value, s, root, path, strict) for s in schema["oneOf"]]
        matched = sum(1 for b in branches if not b)
        if matched != 1:
            detail = " / ".join(b[0] for b in branches if b)
            errors.append(f"{path}: matches {matched} oneOf branches, not 1: {detail}")
    if "not" in schema and not _errors(value, schema["not"], root, path, strict):
        errors.append(f"{path}: matches a schema it must not")
    return errors
