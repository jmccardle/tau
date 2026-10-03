"""Render ``docs/SERVE-PROTOCOL.md`` and ``docs/serve-protocol.schema.json`` (docs/TAU-SERVE.md §5).

Pure functions of :mod:`tau_coding_agent.serve.protocol`, so a test compares
the checked-in files with them and ``scripts/generate_serve_protocol.py`` writes
them. Every count in the Markdown is computed here.
"""

from __future__ import annotations

import json
from typing import Any, get_type_hints

from tau_coding_agent.serve import protocol as p


def render_schema() -> str:
    """The JSON Schema file's exact text."""
    return json.dumps(p.json_schema(), indent=2, sort_keys=True) + "\n"


def _type_text(schema: dict[str, Any]) -> str:
    """A short human spelling of one property's schema."""
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    if "enum" in schema:
        return " \\| ".join(f"`{json.dumps(v)}`" for v in schema["enum"])
    if "anyOf" in schema:
        return " \\| ".join(_type_text(s) for s in schema["anyOf"])
    if schema.get("type") == "array":
        return f"list of {_type_text(schema['items'])}"
    if schema.get("type") == "object":
        return "object"
    return str(schema.get("type", "any"))


def _table(cls: type, defs: dict[str, Any]) -> list[str]:
    """One dataclass as a field table, ``type`` left out (it is the heading)."""
    schema = defs[cls.__name__]
    required = set(schema["required"])
    rows = ["| Field | Type | Required |", "|---|---|---|"]
    for name in get_type_hints(cls):
        if name == "type":
            continue
        prop = schema["properties"][name]
        rows.append(f"| `{name}` | {_type_text(prop)} | {'yes' if name in required else 'no'} |")
    return rows if len(rows) > 2 else ["No fields."]


def _doc(cls: type) -> str:
    """The class docstring, dedented, without its Attributes section."""
    text = (cls.__doc__ or "").strip()
    lines = [line.strip() for line in text.splitlines()]
    if "Attributes:" in lines:
        lines = lines[: lines.index("Attributes:")]
    return "\n".join(lines).strip()


def render_markdown() -> str:
    """The Markdown reference's exact text."""
    defs = p.json_schema()["$defs"]
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
        f"- **Counts:** {len(p.REQUESTS)} requests, {len(p.EVENT_KINDS)} event kinds. "
        "Cite this line; never copy the numbers into hand-written prose."
    )
    w("- **Schema:** `docs/serve-protocol.schema.json` (JSON Schema 2020-12).")
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
    w("## Requests")
    for cls in p.REQUESTS:
        w("")
        w(f"### `{p._type_tag(cls)}`")
        w("")
        w(_doc(cls))
        w("")
        out.extend(_table(cls, defs))
    w("")
    w("## Responses")
    w("")
    w(_doc(p.Response))
    w("")
    out.extend(_table(p.Response, defs))
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
    out.extend(_table(p.Event, defs))
    w("")
    w("Kinds:")
    w("")
    for line in _event_kinds_doc():
        w(line)
    w("")
    w("## Records")
    for cls in (p.Attached, p.CursorState, p.SessionRow, p.SubmitResult):
        w("")
        w(f"### `{cls.__name__}`")
        w("")
        w(_doc(cls))
        w("")
        out.extend(_table(cls, defs))
    w("")
    return "\n".join(out)


def _event_kinds_doc() -> list[str]:
    """The bullet list under ``EVENT_KINDS``'s docstring, as written there."""
    import ast
    import inspect

    source = inspect.getsource(p)
    tree = ast.parse(source)
    for index, node in enumerate(tree.body):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "EVENT_KINDS" for t in node.targets)
            and index + 1 < len(tree.body)
        ):
            doc = tree.body[index + 1]
            if isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant):
                lines = str(doc.value.value).strip().splitlines()
                return [line for line in lines[1:] if line.strip()]
    raise RuntimeError("EVENT_KINDS has no docstring to render")
