"""Render ``docs/SERVE-PROTOCOL.md`` and ``docs/serve-protocol.schema.json`` (docs/TAU-SERVE.md §5).

Pure functions of :mod:`tau_coding_agent.serve.protocol`, so a test compares
the checked-in files with them and ``scripts/generate_serve_protocol.py`` writes
them. Every count and every table in the Markdown is computed here.
"""

from __future__ import annotations

import json
import re

from tau_agent_core.json_schema import (
    cell_markdown as _cell,
    definition_markdown as _definition,
    table_markdown as _table,
    type_markdown as _type_text,
)
from tau_coding_agent.serve import protocol as p


def render_schema() -> str:
    """The JSON Schema file's exact text."""
    return json.dumps(p.json_schema(), indent=2, sort_keys=True) + "\n"


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
        f"- **Counts:** {len(p.REQUESTS) + len(p.RPC_VERBS)} requests "
        f"({len(p.RPC_VERBS)} of them RPC verbs), {len(p.EVENT_KINDS)} event kinds, "
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
        "JSON-RPC 2.0, one message per WebSocket text frame. A request is "
        '`{"jsonrpc": "2.0", "id", "method", "params"}`: `method` is a request name '
        "below and `params` its fields. The `id` is an integer or a string the client "
        "chose, and the request gets exactly one answer with that `id`. A client's "
        "first request is `hello`; nothing else is served before it. Answers and "
        "events share one ordered stream per connection, so a client sees an `attach` "
        "answer before any event that follows it."
    )
    w("")
    w(
        "Not served: a batch (an array of requests) and a notification (a request "
        "with no `id`), each answered with `-32600`. `params` must be an object when "
        "present."
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
        out.extend(_table(defs[cls.__name__]))
        w("")
        w(f"**Answered with:** {_type_text(schema['Results'][tag])}.")
    w("")
    w("## RPC verbs")
    w("")
    w(_doc(p.RpcCall))
    w("")
    w(
        "Each takes RPC's params as documented in `docs/RPC-PROTOCOL.md`, plus "
        "`session_id` and `cursor_id` in the same `params` object, and answers RPC's "
        "result."
    )
    for verb in p.RPC_VERBS:
        name = p._camel(verb)
        w("")
        w(f"### `{verb}`")
        w("")
        w(_cell(defs[name]["description"]))
        w("")
        w(f"**Answered with:** {_type_text(schema['Results'][verb])}.")
    w("")
    w("## Answers")
    w("")
    w(_cell(defs["Response"]["description"]))
    for cls in (p.Success, p.Failure):
        w("")
        w(f"### `{cls.__name__}`")
        w("")
        w(_doc(cls))
        w("")
        out.extend(_table(defs[cls.__name__]))
    w("")
    w("### Error codes")
    w("")
    w(
        "The table RPC uses (`tau_agent_core.rpc.dialect`); a code means the same on both "
        "wires. The schema carries each code's name in the `code` property's `x-names`."
    )
    w("")
    w("| `code` | Name | Meaning |")
    w("|---|---|---|")
    names = p.error_code_names()
    for code in sorted(p.ERROR_CODES, reverse=True):
        w(f"| `{code}` | `{names[code]}` | {p.ERROR_CODES[code]} |")
    w("")
    w("## Events")
    w("")
    w(
        f'Sent as `{{"jsonrpc": "2.0", "method": "{p.EVENT_METHOD}", "params": Event}}`: '
        "a notification, with no `id` and no answer."
    )
    w("")
    w(_doc(p.Event))
    w("")
    out.extend(_table(defs["EntryOpenEvent"], skip=("kind", "data")))
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
    w("## `tau serve --stop --json`")
    w("")
    w(_doc(p.ServeStopped))
    w("")
    out.extend(_table(defs["ServeStopped"]))
    w("")
    w("## Definitions")
    w("")
    w("Every `$defs` entry the requests above do not already show, by name.")
    shown = {cls.__name__ for cls in p.REQUESTS} | {p._camel(v) for v in p.RPC_VERBS}
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
