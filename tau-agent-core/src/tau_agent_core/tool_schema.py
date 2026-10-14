"""Typed tool returns: output schemas from type hints, and Python stubs from schemas.

Experimental (experiments/tool-exposure/RESULTS.md). A tool's structured value
is what a nested caller or a sandboxed script receives; the model still reads
text. The value's shape is declared once, as a Python return annotation, and
everything else is derived from it:

- :func:`output_schema_of` reads a function's return annotation and returns the
  JSON Schema pydantic generates for it (``TypedDict``, dataclass, pydantic
  model, primitives, and containers of those).
- :func:`tool_from_function` turns an annotated function into the dict
  ``ExtensionAPI.register_tool`` takes, with ``parameters`` from the signature
  and ``output_schema`` from the return annotation.
- :func:`render_stub` goes the other way, from a registry's JSON Schemas to a
  Python stub such as ``async def grep(pattern: str, ...) -> GrepResult``, so a
  sandbox prompt can be written from any tool, including one whose author wrote
  JSON Schema by hand.
"""

from __future__ import annotations

import inspect
import json
import keyword
import re
import typing
from typing import Any, Callable

from pydantic import TypeAdapter, create_model
from tau_llm.docs import agent_facing

__all__ = [
    "output_schema_for",
    "output_schema_of",
    "render_stub",
    "render_stubs",
    "tool_from_function",
]

_CTX_PARAM = "ctx"


@agent_facing(topic="tools")
def output_schema_for(tp: Any) -> dict[str, Any]:
    """The JSON Schema of values of type ``tp``.

    Args:
        tp: Any type pydantic can describe.

    Returns:
        ``TypeAdapter(tp).json_schema()``; nested types sit under ``$defs``.

    Raises:
        TypeError: pydantic cannot build a schema for ``tp``.
    """
    try:
        schema: dict[str, Any] = TypeAdapter(tp).json_schema()
    except Exception as exc:
        raise TypeError(f"no JSON Schema for return type {tp!r}: {exc}") from exc
    return schema


def _return_type(fn: Callable[..., Any]) -> Any:
    """``fn``'s resolved return annotation, or ``inspect.Signature.empty``."""
    hints = typing.get_type_hints(fn)
    return hints.get("return", inspect.Signature.empty)


@agent_facing(topic="tools")
def output_schema_of(fn: Callable[..., Any]) -> dict[str, Any] | None:
    """The output schema implied by ``fn``'s return annotation, or ``None`` if it has none.

    An ``async def f() -> X`` declares ``X``; the coroutine wrapper is not part
    of the annotation, so nothing is unwrapped.

    Args:
        fn: The function to read.

    Returns:
        :func:`output_schema_for` the return annotation, or ``None``.

    Raises:
        TypeError: the annotation names a type pydantic cannot describe.
        NameError: any of ``fn``'s annotations does not resolve.
    """
    returns = _return_type(fn)
    if returns is inspect.Signature.empty:
        return None
    return output_schema_for(returns)


def _first_paragraph(doc: str | None) -> str | None:
    """The docstring's first paragraph, whitespace-collapsed, or ``None``."""
    if not doc:
        return None
    paragraph = inspect.cleandoc(doc).split("\n\n", 1)[0]
    return " ".join(paragraph.split()) or None


@agent_facing(topic="tools")
def tool_from_function(
    fn: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
    label: str | None = None,
    render: Callable[[Any], str] | None = None,
    exposure: str = "direct",
    tags: list[str] | None = None,
    execution_mode: str = "parallel",
) -> dict[str, Any]:
    """Build a ``register_tool`` dict from an annotated Python function.

    Every parameter except one named ``ctx`` must be annotated; ``ctx``, if
    present, receives the bound ``ExtensionContext``. The return annotation is
    required and becomes ``output_schema``. The model reads ``render(value)``,
    or the value as compact JSON when ``render`` is ``None``::

        class WordCount(TypedDict):
            words: int
            lines: int

        async def word_count(text: str) -> WordCount:
            '''Count the words and lines in a string.'''
            return {"words": len(text.split()), "lines": text.count("\\n") + 1}

        api.register_tool(tool_from_function(word_count))

    Args:
        fn: The implementation, sync or async.
        name: The tool name; defaults to ``fn.__name__``.
        description: What the model reads; defaults to the docstring's first
            paragraph, and a function with neither raises.
        label: The human label; ``register_tool`` defaults it to ``name``.
        render: Turns the returned value into the model's text.
        exposure: The tool's default exposure.
        tags: Discovery labels.
        execution_mode: ``"parallel"`` or ``"sequential"``.

    Returns:
        ``name``, ``description``, ``parameters``, ``execute``, ``exposure``,
        ``tags``, ``execution_mode`` and ``output_schema``, plus ``label`` when given.

    Raises:
        TypeError: a parameter or the return is unannotated, a parameter is
            ``*args``/``**kwargs``, or a type has no JSON Schema.
        ValueError: there is no description to give the model.
    """
    tool_name = name or fn.__name__
    text = description or _first_paragraph(fn.__doc__)
    if not text:
        raise ValueError(
            f"tool_from_function({tool_name!r}): no description — pass description= "
            "or give the function a docstring; the model is never sent an empty one"
        )
    hints = typing.get_type_hints(fn)
    if "return" not in hints:
        raise TypeError(
            f"tool_from_function({tool_name!r}): the return annotation is required, "
            "because it is the tool's output schema"
        )
    signature = inspect.signature(fn)
    wants_ctx = _CTX_PARAM in signature.parameters
    fields: dict[str, Any] = {}
    for param in signature.parameters.values():
        if param.name == _CTX_PARAM:
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise TypeError(f"tool_from_function({tool_name!r}): *args/**kwargs have no schema")
        if param.name not in hints:
            raise TypeError(
                f"tool_from_function({tool_name!r}): parameter {param.name!r} is unannotated"
            )
        default = ... if param.default is param.empty else param.default
        fields[param.name] = (hints[param.name], default)
    args_model = create_model(f"{tool_name}_arguments", **fields)
    parameters = args_model.model_json_schema()
    parameters.setdefault("properties", {})
    out = TypeAdapter(hints["return"])
    output_schema = output_schema_for(hints["return"])

    async def execute(
        tool_call_id: str, params: dict[str, Any], signal: Any, on_update: Any, ctx: Any
    ) -> dict[str, Any]:
        """Validate ``params``, call ``fn``, and return text plus the structured value."""
        validated = args_model.model_validate(params)
        kwargs = {key: getattr(validated, key) for key in fields}
        if wants_ctx:
            kwargs[_CTX_PARAM] = ctx
        value = fn(**kwargs)
        if inspect.isawaitable(value):
            value = await value
        structured = out.dump_python(value, mode="json")
        shown = render(value) if render is not None else json.dumps(structured, ensure_ascii=False)
        return {"content": [{"type": "text", "text": shown}], "value": structured}

    definition: dict[str, Any] = {
        "name": tool_name,
        "description": text,
        "parameters": parameters,
        "execute": execute,
        "exposure": exposure,
        "tags": list(tags or []),
        "execution_mode": execution_mode,
        "output_schema": output_schema,
    }
    if label is not None:
        definition["label"] = label
    return definition


_PRIMITIVES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "null": "None",
}


def _identifier(name: str) -> str:
    """``name`` made into a Python identifier: non-word characters become ``_``."""
    ident = re.sub(r"\W", "_", name)
    if not ident or ident[0].isdigit() or keyword.iskeyword(ident):
        ident = f"_{ident}"
    return ident


class _StubWriter:
    """Renders JSON Schema as Python annotations, collecting ``TypedDict`` classes."""

    def __init__(self, defs: dict[str, Any]) -> None:
        self.defs = defs
        self.classes: dict[str, str] = {}
        self._pending: set[str] = set()

    def _define(self, title: str, schema: dict[str, Any]) -> None:
        """Render ``title`` once; classes it uses are inserted before it."""
        if title in self.classes or title in self._pending:
            return
        self._pending.add(title)
        body = self.typeddict(schema, title)
        self._pending.discard(title)
        self.classes[title] = body

    def annotation(self, schema: Any, hint: str = "Object") -> str:
        """The annotation for ``schema``; an object with properties becomes a class."""
        if not isinstance(schema, dict) or not schema:
            return "Any"
        if "$ref" in schema:
            ref = str(schema["$ref"]).rsplit("/", 1)[-1]
            if ref in self.defs:
                self._define(ref, self.defs[ref])
            return _identifier(ref)
        for key in ("anyOf", "oneOf"):
            if key in schema:
                parts = [self.annotation(s, hint) for s in schema[key]]
                return " | ".join(dict.fromkeys(parts))
        if "const" in schema:
            return f"Literal[{schema['const']!r}]"
        if "enum" in schema:
            return "Literal[" + ", ".join(repr(v) for v in schema["enum"]) + "]"
        kind = schema.get("type")
        if isinstance(kind, list):
            return " | ".join(self.annotation({**schema, "type": k}, hint) for k in kind)
        if kind in _PRIMITIVES:
            return _PRIMITIVES[kind]
        if kind == "array":
            return f"list[{self.annotation(schema.get('items'), hint)}]"
        if kind == "object" or "properties" in schema:
            if schema.get("properties"):
                title = _identifier(str(schema.get("title") or hint))
                self._define(title, schema)
                return title
            extra = schema.get("additionalProperties")
            return (
                f"dict[str, {self.annotation(extra, hint) if isinstance(extra, dict) else 'Any'}]"
            )
        return "Any"

    def typeddict(self, schema: dict[str, Any], title: str) -> str:
        """A ``class Title(TypedDict)`` for an object schema."""
        required = set(schema.get("required", []))
        lines = [f"class {_identifier(title)}(TypedDict):"]
        summary = _first_paragraph(schema.get("description"))
        if summary:
            lines.append(f'    """{summary}"""')
        properties = schema.get("properties") or {}
        for key, sub in properties.items():
            ann = self.annotation(sub, f"{title}_{key}")
            if key not in required:
                ann = f"NotRequired[{ann}]"
            lines.append(f"    {key}: {ann}")
        if not properties:
            lines.append("    pass")
        return "\n".join(lines)


@agent_facing(topic="tools")
def render_stub(
    name: str,
    description: str,
    parameters: dict[str, Any],
    output_schema: dict[str, Any] | None,
) -> str:
    """A Python stub for one tool, preceded by the ``TypedDict`` classes it uses.

    Required parameters come first, optional ones default to ``None`` because
    the schema states no default.

    Args:
        name: The tool name; made into a Python identifier.
        description: The stub's docstring, whitespace-collapsed.
        parameters: The argument JSON Schema.
        output_schema: The value's JSON Schema; ``None`` renders ``-> str``,
            which is what a nested caller gets from it: the text.

    Returns:
        The classes, then the ``async def``, separated by blank lines.
    """
    defs = {**(parameters.get("$defs") or {}), **((output_schema or {}).get("$defs") or {})}
    writer = _StubWriter(defs)
    props = parameters.get("properties") or {}
    required = [p for p in props if p in set(parameters.get("required", []))]
    optional = [p for p in props if p not in required]
    args = [f"{p}: {writer.annotation(props[p], p)}" for p in required]
    args += [f"{p}: {writer.annotation(props[p], p)} | None = None" for p in optional]
    returns = "str" if output_schema is None else writer.annotation(output_schema, f"{name}_result")
    doc = " ".join(description.split())
    func = f'async def {_identifier(name)}({", ".join(args)}) -> {returns}:\n    """{doc}"""'
    classes = list(writer.classes.values())
    return "\n\n".join([*classes, func])


@agent_facing(topic="tools")
def render_stubs(descriptions: list[Any]) -> str:
    """Stubs for several tools under one ``typing`` import header.

    Args:
        descriptions: Objects with ``name``, ``description``, ``parameters`` and
            ``output_schema``, such as
            :class:`~tau_agent_core.tool_catalog.ToolDescription`.

    Returns:
        One module's text, ending in a newline; a class whose text repeats an
        earlier one exactly is written once.
    """
    header = "from typing import Any, Literal, NotRequired, TypedDict"
    bodies: list[str] = []
    seen: set[str] = set()
    for d in descriptions:
        for block in render_stub(d.name, d.description, d.parameters, d.output_schema).split(
            "\n\n"
        ):
            if block.startswith("class "):
                if block in seen:
                    continue
                seen.add(block)
            bodies.append(block)
    return "\n\n\n".join([header, *bodies]) + "\n"
