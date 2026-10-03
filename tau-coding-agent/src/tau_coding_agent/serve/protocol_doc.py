"""Render ``docs/SERVE-PROTOCOL.md`` and ``docs/serve-protocol.schema.json`` (docs/TAU-SERVE.md §5).

Pure functions of :mod:`tau_coding_agent.serve.protocol`, so a test compares
the checked-in files with them and ``scripts/generate_serve_protocol.py`` writes
them. Every count and every table in the Markdown is computed here.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tau_coding_agent.serve import protocol as p


def render_schema() -> str:
    """The JSON Schema file's exact text."""
    return json.dumps(p.json_schema(), indent=2, sort_keys=True) + "\n"


def _type_text(schema: Any) -> str:
    """A short human spelling of one property's schema."""
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
            return " \\| ".join(_type_text(s) for s in schema[key])
    if schema.get("type") == "array":
        return f"list of {_type_text(schema.get('items', {}))}"
    if schema.get("type") == "object" or "additionalProperties" in schema:
        values = schema.get("additionalProperties")
        if isinstance(values, dict) and values:
            return f"object of {_type_text(values)}"
        return "object"
    if "pattern" in schema:
        return f"string matching `{schema['pattern']}`"
    return str(schema.get("type", "any"))


def _cell(text: str) -> str:
    """``text`` safe inside a Markdown table cell."""
    return " ".join(text.split()).replace("|", "\\|")


def _table(schema: dict[str, Any], skip: tuple[str, ...] = ()) -> list[str]:
    """One object schema as a field table."""
    properties = {k: v for k, v in schema.get("properties", {}).items() if k not in skip}
    if not properties:
        return ["No fields."]
    required = set(schema.get("required", []))
    rows = ["| Field | Type | Required | Description |", "|---|---|---|---|"]
    for name, prop in properties.items():
        described = prop.get("description", "") if isinstance(prop, dict) else ""
        rows.append(
            f"| `{name}` | {_type_text(prop)} | {'yes' if name in required else 'no'} "
            f"| {_cell(described)} |"
        )
    return rows


def _md(text: str) -> str:
    """Docstring prose as Markdown: Sphinx roles and double backticks become code spans."""
    return re.sub(r":\w+:`~?([^`]+)`", r"`\1`", text).replace("``", "`")


def _doc(cls: type) -> str:
    """The class docstring, dedented, without its Attributes section."""
    text = (cls.__doc__ or "").strip()
    lines = [line.strip() for line in text.splitlines()]
    if "Attributes:" in lines:
        lines = lines[: lines.index("Attributes:")]
    return _md("\n".join(lines).strip())


def _definition(name: str, schema: Any) -> list[str]:
    """One ``$defs`` entry: its description, then its fields or its members."""
    out = [f"### {name}", ""]
    if isinstance(schema, dict) and schema.get("description"):
        out += [_cell(str(schema["description"]).split("\n\n", 1)[0]), ""]
    if isinstance(schema, dict) and "oneOf" in schema:
        out.append("One of: " + ", ".join(_type_text(member) for member in schema["oneOf"]) + ".")
    elif isinstance(schema, dict):
        out.extend(_table(schema))
    return out


def render_markdown() -> str:
    """The Markdown reference's exact text."""
    schema = p.json_schema()
    defs = schema["$defs"]
    out: list[str] = []
    w = out.append
    w("# τ serve protocol")
    w("")
    w("> **Generated — do not hand-edit.** Run `python scripts/generate_serve_protocol.py`")
    w("> after changing `tau_coding_agent/serve/protocol.py`, and commit both outputs.")
    w("> `tests/test_serve.py` fails if this file or the schema is stale.")
    w(">")
    w("> Design of record: `docs/TAU-SERVE.md` §5–§7.")
    w("")
    w(f"- **Protocol version:** `{p.PROTOCOL_VERSION}`")
    w(f"- **Default port:** `{p.DEFAULT_PORT}`")
    w(
        f"- **Counts:** {len(p.REQUESTS)} requests, {len(p.EVENT_KINDS)} event kinds, "
        f"{len(defs)} schema definitions. Cite this line; never copy the numbers into "
        "hand-written prose."
    )
    w(
        "- **Schema:** `docs/serve-protocol.schema.json` (JSON Schema 2020-12), also "
        "printed by `tau serve --schema` from an installed τ."
    )
    w("")
    w("## Framing")
    w("")
    w(
        "One JSON object per WebSocket text frame. A client's first request is "
        "`hello`; nothing else is served before it. Every request carries an integer "
        "`id` the client chose, and gets exactly one `response` with that `id`. "
        "Responses and events share one ordered stream per connection, so a client "
        "sees an `attach` answer before any event that follows it."
    )
    w("")
    w("## Open and closed records")
    w("")
    w(p.RECORD_RULE)
    w("")
    w(
        "Each request names what it is answered with; in the schema that is the "
        "request's `x-result` and the top-level `Results` map. `Event`'s `x-data` "
        "names each kind's data."
    )
    w("")
    w("## Requests")
    for cls in p.REQUESTS:
        tag = p._type_tag(cls)
        w("")
        w(f"### `{tag}`")
        w("")
        w(_doc(cls))
        w("")
        out.extend(_table(defs[cls.__name__], skip=("type",)))
        w("")
        w(f"**Answered with:** {_type_text(schema['Results'][tag])}.")
    w("")
    w("## Responses")
    w("")
    w(_doc(p.Response))
    w("")
    out.extend(_table(defs["Response"], skip=("type",)))
    w("")
    w(
        "Error codes: "
        + ", ".join(f"`{c}`" for c in defs["Error"]["properties"]["code"]["enum"])
        + "."
    )
    w("")
    w("## Events")
    w("")
    w(_doc(p.Event))
    w("")
    out.extend(_table(defs["EntryOpenEvent"], skip=("type", "kind", "data")))
    w("")
    w("| `kind` | `data` |")
    w("|---|---|")
    for kind, data in defs["Event"]["x-data"].items():
        w(f"| `{kind}` | {_type_text(data)} |")
    w("")
    w(_doc_of_constant("EVENT_DATA"))
    w("")
    w("## `tau serve -d --json`")
    w("")
    w(_doc(p.ServeStarted))
    w("")
    out.extend(_table(defs["ServeStarted"]))
    w("")
    w("## Definitions")
    w("")
    w("Every `$defs` entry the requests above do not already show, by name.")
    shown = {cls.__name__ for cls in p.REQUESTS}
    for name in sorted(defs):
        if name in shown:
            continue
        w("")
        out.extend(_definition(name, defs[name]))
    w("")
    return "\n".join(out)


def _doc_of_constant(name: str) -> str:
    """The docstring written under a module constant of :mod:`protocol`."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(p))
    for index, node in enumerate(tree.body):
        if (
            isinstance(node, ast.Assign | ast.AnnAssign)
            and any(
                isinstance(t, ast.Name) and t.id == name
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
            and index + 1 < len(tree.body)
        ):
            doc = tree.body[index + 1]
            if isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant):
                lines = str(doc.value.value).strip().splitlines()
                return _md("\n".join(line.strip() for line in lines))
    raise RuntimeError(f"{name} has no docstring to render")
