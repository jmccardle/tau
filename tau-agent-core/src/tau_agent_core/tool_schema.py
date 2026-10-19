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
  JSON Schema by hand. Code mode's ``tools`` class renders through the same
  :class:`StubNamespace`, and :func:`stub_names` maps a stubbed tool name back.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import keyword
import re
import sys
import types
import typing
from collections.abc import Iterable, Mapping
from typing import Any, Callable

from pydantic import TypeAdapter, create_model
from tau_llm.docs import agent_facing

__all__ = [
    "StubError",
    "StubNamespace",
    "output_schema_for",
    "output_schema_of",
    "render_stub",
    "render_stubs",
    "stub_clashes",
    "stub_name",
    "stub_names",
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


def _raw_annotations(fn: Callable[..., Any]) -> dict[str, Any]:
    """``fn``'s annotations, evaluating none that would fail to resolve."""
    if sys.version_info >= (3, 14):
        import annotationlib

        return dict(annotationlib.get_annotations(fn, format=annotationlib.Format.FORWARDREF))
    return dict(inspect.get_annotations(fn))


def _resolved_hints(fn: Callable[..., Any], names: Iterable[str]) -> dict[str, Any]:
    """``typing.get_type_hints(fn)`` restricted to ``names``; the rest are never evaluated.

    Raises:
        NameError: one of ``names`` does not resolve.
    """
    raw = _raw_annotations(fn)
    shim = types.FunctionType(_SHIM_CODE, getattr(inspect.unwrap(fn), "__globals__", {}))
    shim.__annotations__ = {n: raw[n] for n in names if n in raw}
    if hasattr(fn, "__type_params__"):
        setattr(shim, "__type_params__", fn.__type_params__)  # noqa: B010
    return typing.get_type_hints(shim)


_SHIM_CODE = (lambda: None).__code__


def _signature(fn: Callable[..., Any]) -> inspect.Signature:
    """``fn``'s signature, with no annotation evaluated."""
    if sys.version_info >= (3, 14):
        import annotationlib

        return inspect.signature(fn, annotation_format=annotationlib.Format.FORWARDREF)
    return inspect.signature(fn)


def _return_type(fn: Callable[..., Any]) -> Any:
    """``fn``'s resolved return annotation, or ``inspect.Signature.empty``."""
    return _resolved_hints(fn, ["return"]).get("return", inspect.Signature.empty)


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
        NameError: the return annotation does not resolve; parameter
            annotations are never evaluated.
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
        NameError: an annotation other than ``ctx``'s does not resolve.
    """
    tool_name = name or fn.__name__
    text = description or _first_paragraph(fn.__doc__)
    if not text:
        raise ValueError(
            f"tool_from_function({tool_name!r}): no description — pass description= "
            "or give the function a docstring; the model is never sent an empty one"
        )
    signature = _signature(fn)
    wants_ctx = _CTX_PARAM in signature.parameters
    hints = _resolved_hints(fn, [*(p for p in signature.parameters if p != _CTX_PARAM), "return"])
    if "return" not in hints:
        raise TypeError(
            f"tool_from_function({tool_name!r}): the return annotation is required, "
            "because it is the tool's output schema"
        )
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


def _plain_key(key: str) -> bool:
    """Whether ``key`` can be written as a class-body field or a parameter name."""
    mangled = key.startswith("__") and not key.endswith("__")
    return key.isidentifier() and not keyword.iskeyword(key) and not mangled


@agent_facing(topic="tools")
class StubError(ValueError):
    """A tool's schema cannot be stubbed: a ``$ref`` that is not local or does not resolve.

    Attributes:
        tool: The tool's real name.
        reason: Why, naming the ``$ref``, e.g. ``$ref '#/$defs/X' does not resolve``.
    """

    def __init__(self, tool: str, reason: str) -> None:
        super().__init__(f"tool {tool!r}: {reason}, so it has no type to stub")
        self.tool = tool
        self.reason = reason


class _StubWriter:
    """Renders JSON Schema as Python annotations, collecting ``TypedDict`` classes.

    One writer serves every tool in a module, so a class name is never bound to
    two different bodies: a second shape under a taken name gets ``Name_2``.
    """

    def __init__(self) -> None:
        self.classes: dict[str, str] = {}
        self.new: list[str] = []
        self.tool = ""
        self.root: dict[str, Any] = {}
        self.root_title = ""
        self._refs: dict[Any, str] = {}
        self._active: set[Any] = set()

    def begin(self, tool: str) -> None:
        """Start one tool: its name for errors, and an empty list of classes it adds."""
        self.tool = tool
        self._refs = {}
        self.new = []

    def at_root(self, root: dict[str, Any], title: str) -> None:
        """Resolve ``$ref`` against ``root`` from now on; ``#`` names a class ``title``."""
        self.root = root
        self.root_title = str(root.get("title") or title)

    def define(self, title: str, schema: dict[str, Any], ref: Any = None) -> str:
        """The identifier of ``schema``'s class, rendering it under a free name if new.

        Classes it uses are inserted before it. ``ref`` keys a ``$ref`` target,
        so a self-reference resolves to the name being rendered.
        """
        if ref is not None and ref in self._refs:
            return self._refs[ref]
        base = _identifier(title)
        n = 1
        while True:
            ident = base if n == 1 else f"{base}_{n}"
            if ref is not None:
                self._refs[ref] = ident
            classes, new, refs = dict(self.classes), list(self.new), dict(self._refs)
            body = self.typeddict(schema, ident)
            existing = self.classes.get(ident)
            if existing is None:
                self.classes[ident] = body
                self.new.append(ident)
                return ident
            if existing == body:
                return ident
            self.classes, self.new, self._refs = classes, new, refs
            n += 1

    def annotation(self, schema: Any, hint: str = "Object") -> str:
        """The annotation for ``schema``; an object with properties becomes a class.

        Raises:
            StubError: a ``$ref`` does not resolve (:meth:`reference`).
        """
        if not isinstance(schema, dict) or not schema:
            return "Any"
        if "$ref" in schema:
            return self.reference(str(schema["$ref"]), hint)
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
                return self.define(str(schema.get("title") or hint), schema)
            extra = schema.get("additionalProperties")
            return (
                f"dict[str, {self.annotation(extra, hint) if isinstance(extra, dict) else 'Any'}]"
            )
        return "Any"

    def reference(self, ref: str, hint: str) -> str:
        """The annotation for ``{"$ref": ref}``, an RFC 6901 pointer into the current root.

        A target object with properties is a class named for the pointer's last
        token (or the root's title, for ``#``), so a recursive reference names
        it. Any other target renders inline; a cycle through one renders ``Any``.

        Raises:
            StubError: ``ref`` is not a local ``#`` pointer, or does not resolve.
        """
        key = (id(self.root), ref)
        if key in self._refs:
            return self._refs[key]
        name, target = self._resolve(ref)
        if isinstance(target, dict) and target.get("properties"):
            return self.define(str(target.get("title") or name or self.root_title), target, key)
        if key in self._active:
            return "Any"
        self._active.add(key)
        try:
            return self.annotation(target, hint)
        finally:
            self._active.discard(key)

    def _resolve(self, ref: str) -> tuple[str, Any]:
        """The last token of ``ref`` (empty for ``#``) and the schema it points at."""
        if not ref.startswith("#") or (len(ref) > 1 and ref[1] != "/"):
            raise StubError(self.tool, f"$ref {ref!r} is not a local '#/...' pointer")
        node: Any = self.root
        token = ""
        for raw in ref[2:].split("/") if len(ref) > 1 else []:
            token = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and token in node:
                node = node[token]
            elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
                node = node[int(token)]
            else:
                raise StubError(
                    self.tool, f"$ref {ref!r} does not resolve (no {token!r} in the schema)"
                )
        return token, node

    def typeddict(self, schema: dict[str, Any], ident: str) -> str:
        """A ``TypedDict`` named ``ident`` for an object schema.

        Class syntax when every key can be a field name; otherwise the functional
        form, which keeps a key such as ``from`` or ``my-arg`` exactly.
        """
        required = set(schema.get("required", []))
        summary = _first_paragraph(schema.get("description"))
        properties = schema.get("properties") or {}
        fields = []
        for key, sub in properties.items():
            ann = self.annotation(sub, f"{ident}_{_identifier(key)}")
            fields.append((key, ann if key in required else f"NotRequired[{ann}]"))
        if all(_plain_key(key) for key, _ in fields):
            lines = [f"class {ident}(TypedDict):"]
            if summary:
                lines.append(f'    """{summary}"""')
            lines += [f"    {key}: {ann}" for key, ann in fields] or ["    pass"]
            return "\n".join(lines)
        items = ", ".join(f"{json.dumps(key)}: {ann}" for key, ann in fields)
        head = [f"# {summary}"] if summary else []
        return "\n".join([*head, f"{ident} = TypedDict({ident!r}, {{{items}}})"])


@agent_facing(topic="tools")
def stub_name(name: str) -> str:
    """The identifier a stub gives tool ``name``.

    A character an identifier cannot contain becomes ``_`` (``my-tool`` →
    ``my_tool``) and a keyword gains a trailing ``_`` (``from_``). A result
    that cannot start an identifier, or that starts with ``_`` when ``name``
    did not, gains ``t_`` (``1tool`` → ``t_1tool``, ``-x`` → ``t__x``), because
    Monty refuses an attribute starting with ``_``. A real leading ``_`` is
    kept.

    Args:
        name: The real tool name.

    Returns:
        A Python identifier; ``call_tool`` and code mode resolve it back
        through :func:`stub_names`, which raises on a clash.
    """
    ident = "".join(c if f"a{c}".isidentifier() else "_" for c in name)
    if keyword.iskeyword(ident):
        return f"{ident}_"
    if not ident.isidentifier() or (ident.startswith("_") and not name.startswith("_")):
        return f"t_{ident}"
    return ident


@agent_facing(topic="tools")
def stub_clashes(names: Iterable[str]) -> dict[str, list[str]]:
    """The stub names two or more of ``names`` share, each with the real names sharing it.

    Args:
        names: Real tool names.

    Returns:
        ``{stub: [real, ...]}`` for every shared stub; empty when there is none.
    """
    groups: dict[str, list[str]] = {}
    for name in dict.fromkeys(names):
        groups.setdefault(stub_name(name), []).append(name)
    return {stub: real for stub, real in groups.items() if len(real) > 1}


def clash_message(clashes: dict[str, list[str]]) -> str:
    """``clashes`` as one sentence naming every tool involved."""
    listed = "; ".join(
        f"{', '.join(repr(r) for r in real)} all stub as {stub!r}" for stub, real in clashes.items()
    )
    return f"tool names collide once made identifiers: {listed}; rename one"


@agent_facing(topic="tools")
def stub_names(names: Iterable[str]) -> dict[str, str]:
    """Map each tool's stub name back to its real name, for one set of tools.

    Args:
        names: Real tool names.

    Returns:
        ``{stub_name(n): n}``; a name that is already an identifier maps to itself.

    Raises:
        ValueError: two names share a stub name (``my-tool`` and ``my_tool``),
            so neither stub could say which tool it calls.
    """
    unique = list(dict.fromkeys(names))
    clashes = stub_clashes(unique)
    if clashes:
        raise ValueError(clash_message(clashes))
    return {stub_name(name): name for name in unique}


@agent_facing(topic="tools")
class StubNamespace:
    """Signatures for tools that share one module, so no class name means two shapes.

    :func:`render_stub`, :func:`render_stubs` and code mode's ``tools`` class
    all render through this.
    """

    def __init__(self) -> None:
        self._writer = _StubWriter()

    def signature(
        self,
        name: str,
        parameters: Mapping[str, Any],
        output_schema: Mapping[str, Any] | None,
        *,
        keyword_only: bool = False,
        self_arg: bool = False,
    ) -> tuple[list[str], str]:
        """One tool's ``async def`` line and the ``TypedDict`` classes it adds.

        Required parameters come first; optional ones default to ``None``,
        because the schema states no default. If a parameter name cannot be a
        Python parameter (``from``, ``my-arg``), the signature is
        ``**kwargs: Unpack[<stub name>_arguments]`` over a functional
        ``TypedDict`` that keeps every name exactly.

        Args:
            name: The tool name; the function is :func:`stub_name` of it.
            parameters: The argument JSON Schema.
            output_schema: The value's JSON Schema; ``None`` renders ``-> str``,
                which is what a caller gets from it: the text.
            keyword_only: Put a bare ``*`` before the parameters.
            self_arg: Lead with ``self``, for a method.

        Returns:
            The classes not yet written in this namespace, each before the
            classes that use it, and ``async def f(...) -> T`` with no colon.

        Raises:
            StubError: a ``$ref`` is not a local pointer or does not resolve.
        """
        writer = self._writer
        writer.begin(name)
        func_name = stub_name(name)
        writer.at_root(dict(parameters), f"{func_name}_arguments")
        props = parameters.get("properties") or {}
        if all(_plain_key(p) for p in props):
            required = [p for p in props if p in set(parameters.get("required", []))]
            optional = [p for p in props if p not in required]
            args = [f"{p}: {writer.annotation(props[p], p)}" for p in required]
            args += [f"{p}: {writer.annotation(props[p], p)} | None = None" for p in optional]
            if keyword_only and args:
                args.insert(0, "*")
        else:
            arguments = {"properties": props, "required": parameters.get("required", [])}
            args = [f"**kwargs: Unpack[{writer.define(f'{func_name}_arguments', arguments)}]"]
        if self_arg:
            args.insert(0, "self")
        returns = "str"
        if output_schema is not None:
            out = dict(output_schema)
            writer.at_root(out, f"{func_name}_result")
            returns = writer.annotation(out, f"{name}_result")
        signature = f"async def {func_name}({', '.join(args)}) -> {returns}"
        return [writer.classes[ident] for ident in writer.new], signature


def _stub_blocks(namespace: StubNamespace, d: Any) -> list[str]:
    """The blocks one tool adds to a module: new classes, then the ``async def``."""
    classes, signature = namespace.signature(d.name, d.parameters, d.output_schema)
    doc = " ".join(d.description.split())
    return [*classes, f'{signature}:\n    """{doc}"""']


@dataclasses.dataclass(frozen=True)
class _Described:
    """The four fields a stub reads."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    output_schema: Mapping[str, Any] | None


@agent_facing(topic="tools")
def render_stub(
    name: str,
    description: str,
    parameters: dict[str, Any],
    output_schema: dict[str, Any] | None,
) -> str:
    """A Python stub for one tool, preceded by the ``TypedDict`` classes it uses.

    See :meth:`StubNamespace.signature` for the signature's shape.

    Args:
        name: The tool name; the stub defines :func:`stub_name` of it.
        description: The stub's docstring, whitespace-collapsed.
        parameters: The argument JSON Schema.
        output_schema: The value's JSON Schema, or ``None`` for ``-> str``.

    Returns:
        The classes, then the ``async def``, separated by blank lines.

    Raises:
        StubError: a ``$ref`` is not a local pointer or does not resolve.
    """
    described = _Described(name, description, parameters, output_schema)
    return "\n\n".join(_stub_blocks(StubNamespace(), described))


@agent_facing(topic="tools")
def render_stubs(descriptions: list[Any]) -> str:
    """Stubs for several tools under one ``typing`` import header.

    Args:
        descriptions: Objects with ``name``, ``description``, ``parameters`` and
            ``output_schema``, such as
            :class:`~tau_agent_core.tool_catalog.ToolDescription`.

    Returns:
        One module's text, ending in a newline. A class is written once; a
        different shape under a name already taken is renamed ``Name_2``, and
        the stubs that use it say so.

    Raises:
        ValueError: two tools share a stub name (:func:`stub_names`).
        StubError: a ``$ref`` is not a local pointer or does not resolve.
    """
    stub_names(d.name for d in descriptions)
    header = "from typing import Any, Literal, NotRequired, TypedDict, Unpack"
    namespace = StubNamespace()
    bodies: list[str] = []
    for d in descriptions:
        bodies += _stub_blocks(namespace, d)
    return "\n\n\n".join([header, *bodies]) + "\n"
