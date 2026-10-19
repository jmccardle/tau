"""Code mode's prompt: tool stubs per language, and templates per provider × lifetime.

Two outputs, rendered from one provider's :class:`PromptTemplates` plus config
overrides: the system-prompt section and the ``codemode`` tool description. Both
use ``{{field}}`` placeholders like τ's own system prompt, and an unknown field
raises, for the same reason (``SystemPromptFieldError``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from tau_agent_core.codemode.protocol import (
    LIFETIMES,
    Lifetime,
    PromptTemplates,
    RunLimits,
    ToolStub,
)
from tau_agent_core.tool_schema import StubError, StubNamespace

_FIELD = re.compile(r"\{\{(\w+)\}\}")
_TS_IDENT = re.compile(r"^[A-Za-z_$][\w$]*$")


class PromptFieldError(ValueError):
    """A code-mode template names a field that does not exist."""


def render_template(template: str, fields: Mapping[str, str]) -> str:
    """Substitute ``{{field}}`` placeholders; collapse blank runs left by empty fields.

    Raises:
        PromptFieldError: on a placeholder naming no field in ``fields``.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in fields:
            known = ", ".join(sorted(fields))
            raise PromptFieldError(
                f"codemode prompt names unknown field {{{{{name}}}}}; known: {known}"
            )
        return fields[name]

    text = _FIELD.sub(_sub, template)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def _first_sentence(text: str, cap: int = 160) -> str:
    """The first line of ``text``, cut at its first sentence and at ``cap`` chars."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    end = line.find(". ")
    if end != -1:
        line = line[: end + 1]
    return line if len(line) <= cap else line[: cap - 1] + "…"


def _ts_type(schema: Mapping[str, Any]) -> str:
    """A TypeScript type for a JSON-schema fragment."""
    if "enum" in schema:
        return " | ".join(json.dumps(v) for v in schema["enum"])
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(_ts_type({**schema, "type": k}) for k in kind)
    if kind == "array":
        items = schema.get("items")
        return f"Array<{_ts_type(items)}>" if isinstance(items, Mapping) else "unknown[]"
    return {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "object": "Record<string, unknown>",
        "null": "null",
    }.get(str(kind), "unknown")


def _params(stub: ToolStub) -> list[tuple[str, Mapping[str, Any], bool]]:
    """``(name, schema, required)`` per parameter, required ones first."""
    props = stub.parameters.get("properties") or {}
    required = set(stub.parameters.get("required") or [])
    out = [(n, s, n in required) for n, s in props.items() if isinstance(s, Mapping)]
    return sorted(out, key=lambda p: not p[2])


def _python_signatures(stubs: Sequence[ToolStub], *, self_arg: bool) -> tuple[list[str], list[str]]:
    """The ``TypedDict`` classes the stubs use, and one keyword-only signature per stub.

    Rendered by :class:`~tau_agent_core.tool_schema.StubNamespace`, the renderer
    ``tool_search`` uses, with ``-> str`` because a script receives a tool's text.

    Raises:
        StubError: a ``$ref`` is not a local pointer or does not resolve.
    """
    namespace = StubNamespace()
    classes: list[str] = []
    signatures: list[str] = []
    for stub in stubs:
        new, signature = namespace.signature(
            stub.name, stub.parameters, None, keyword_only=True, self_arg=self_arg
        )
        classes += new
        signatures.append(signature)
    return classes, signatures


def stub_failures(stubs: Sequence[ToolStub], language: str) -> dict[str, str]:
    """The stubs ``language``'s renderer cannot render, each with the reason (docs/CODE-MODE.md §6.3).

    Each stub is rendered alone with :data:`STUB_RENDERERS` ``[language]``, so
    a tool is left out only where that renderer fails; TypeScript's never does.

    Returns:
        ``{real name: reason}``, e.g. ``$ref '#/$defs/X' does not resolve (...)``,
        in ``stubs`` order; empty when every stub renders.
    """
    failures: dict[str, str] = {}
    for stub in stubs:
        try:
            STUB_RENDERERS[language]([stub])
        except StubError as exc:
            failures[stub.name] = exc.reason
    return failures


def unavailable_notice(name: str, reason: str) -> str:
    """The notice every head shows for a tool code mode leaves out."""
    return f"tool {name!r} is unavailable in code mode: {reason}"


def render_python_stubs(stubs: Sequence[ToolStub]) -> str:
    """The callable tools as a Python class body the model can read.

    ``tools`` is an instance of the class; the ``TypedDict`` classes its
    signatures use come first.
    """
    if not stubs:
        return "class tools:  # no tools are callable from scripts\n    pass\n"
    classes, signatures = _python_signatures(stubs, self_arg=False)
    lines = ["class tools:"]
    for stub, signature in zip(stubs, signatures):
        lines.append(f"    {signature}:")
        lines.append(f'        """{_first_sentence(stub.description)}"""')
    return "\n\n".join([*classes, "\n".join(lines)]) + "\n"


def render_type_check_stubs(stubs: Sequence[ToolStub]) -> str:
    """A ``.pyi`` declaring ``tools`` and the discovery functions, for ``ty``."""
    classes, signatures = _python_signatures(stubs, self_arg=True)
    lines = ["from typing import Any, Literal, NotRequired, TypedDict, Unpack", ""]
    for block in classes:
        lines += [block, ""]
    lines.append("class _Tools:")
    lines += [f"    {signature}: ..." for signature in signatures] or ["    pass"]
    lines += [
        "",
        "tools: _Tools",
        "async def search_tools(query: str, limit: int = 8) -> list[dict[str, str]]: ...",
        "async def describe_tool(name: str) -> str: ...",
        "",
    ]
    return "\n".join(lines)


def render_typescript_stubs(stubs: Sequence[ToolStub]) -> str:
    """The callable tools as a TypeScript ambient declaration."""
    lines = ["declare const tools: {"]
    for stub in stubs:
        fields = [
            f"{name if _TS_IDENT.match(name) else json.dumps(name)}"
            f"{'' if required else '?'}: {_ts_type(schema)}"
            for name, schema, required in _params(stub)
        ]
        lines.append(f"  /** {_first_sentence(stub.description)} */")
        lines.append(f"  {stub.attribute}(args: {{ {'; '.join(fields)} }}): Promise<string>;")
    lines.append("};")
    return "\n".join(lines) + "\n"


STUB_RENDERERS = {"python": render_python_stubs, "typescript": render_typescript_stubs}
"""Stub renderer by provider language. A new language adds one entry."""


def describe_stub(stub: ToolStub, language: str) -> str:
    """One tool's full signature, with any classes it uses, and description, for ``describe_tool``."""
    if language == "typescript":
        head = render_typescript_stubs([stub]).splitlines()[2].strip()
    else:
        classes, signatures = _python_signatures([stub], self_arg=False)
        head = "\n\n".join([*classes, signatures[0]])
    return (
        f"{head}\n\n{stub.description.strip()}\n\nparameters: {json.dumps(dict(stub.parameters))}"
    )


def _limits_text(limits: RunLimits) -> str:
    """The limits clause, in words."""
    return (
        f"{limits.timeout_s:g} s wall time (tool time included), "
        f"{limits.memory_bytes // (1024 * 1024)} MiB memory, {limits.max_calls} tool calls, "
        f"{limits.output_chars} characters of output (the rest is saved to a file whose path "
        "the result names)"
    )


def merge_templates(base: PromptTemplates, overrides: Mapping[str, Any] | None) -> PromptTemplates:
    """Apply ``codemode.prompt`` config over a provider's defaults.

    ``lifetime`` merges per key; every other key replaces.

    Raises:
        ValueError: on a key :class:`PromptTemplates` does not have, or a
            ``lifetime`` key that is not a lifetime.
    """
    if not overrides:
        return base
    allowed = {"system", "description", "lifetime", "language_notes", "language_label"}
    unknown = set(overrides) - allowed
    if unknown:
        raise ValueError(
            f"codemode.prompt: unknown keys {sorted(unknown)}; allowed {sorted(allowed)}"
        )
    changes: dict[str, Any] = {k: v for k, v in overrides.items() if k != "lifetime"}
    if "lifetime" in overrides:
        bad = set(overrides["lifetime"]) - set(LIFETIMES)
        if bad:
            raise ValueError(f"codemode.prompt.lifetime: unknown lifetimes {sorted(bad)}")
        changes["lifetime"] = {**base.lifetime, **overrides["lifetime"]}
    for key, value in changes.items():
        if key != "lifetime" and not isinstance(value, str):
            raise ValueError(f"codemode.prompt.{key} must be a string")
    return replace(base, **changes)


def prompt_fields(
    templates: PromptTemplates,
    *,
    provider: str,
    language: str,
    lifetime: Lifetime,
    limits: RunLimits,
    stubs: Sequence[ToolStub] | None,
    tool_name: str = "codemode",
) -> dict[str, str]:
    """Every ``{{field}}`` a code-mode template may name.

    ``stubs=None`` renders no stub block (discovery only), which is the setting
    that keeps the section byte-stable whatever tools register later.
    """
    if lifetime not in templates.lifetime:
        raise ValueError(f"provider {provider!r} has no prompt text for lifetime {lifetime!r}")
    lifetime_text = templates.lifetime[lifetime]
    if stubs is None:
        stub_block = ""
    else:
        fence = "ts" if language == "typescript" else "python"
        stub_block = (
            f"Tools callable from scripts:\n```{fence}\n{STUB_RENDERERS[language](stubs)}```"
        )
    fields = {
        "tool_name": tool_name,
        "provider": provider,
        "language": language,
        "language_label": templates.language_label,
        "lifetime": lifetime,
        "limits": _limits_text(limits),
        "stubs": stub_block,
        "tool_names": ", ".join(s.attribute for s in stubs or []),
    }
    fields["language_notes"] = render_template(templates.language_notes, fields).strip()
    fields["lifetime_text"] = render_template(lifetime_text, fields).strip()
    fields["lifetime_brief"] = _first_sentence(fields["lifetime_text"], cap=200)
    return fields


def render_prompt(
    templates: PromptTemplates,
    *,
    provider: str,
    language: str,
    lifetime: Lifetime,
    limits: RunLimits,
    stubs: Sequence[ToolStub] | None,
    tool_name: str = "codemode",
) -> tuple[str, str]:
    """``(system_section, tool_description)`` for one provider × lifetime."""
    fields = prompt_fields(
        templates,
        provider=provider,
        language=language,
        lifetime=lifetime,
        limits=limits,
        stubs=stubs,
        tool_name=tool_name,
    )
    return render_template(templates.system, fields), render_template(templates.description, fields)


PYTHON_SYSTEM = """\
## Code mode

The `{{tool_name}}` tool runs {{language_label}} source that you write. Inside a \
script every other tool is an async function on the `tools` object, called with \
keyword arguments: `text = await tools.read(path="setup.py")`. A tool returns its \
text output as a `str`; a tool that fails raises `RuntimeError` carrying the tool's \
error message, which you can catch.

Use it when a task needs several tool calls, a loop, or filtering of large output. \
Run independent calls together with `await asyncio.gather(...)` (after \
`import asyncio`) and process results inside the script. Only what the script \
prints, and the value of its last expression, comes back to you: tool results the \
script does not print never enter the conversation, so print what you need, not \
raw dumps.

{{lifetime_text}}

{{language_notes}}

Limits per call: {{limits}}.

`await search_tools("query")` returns `[{"name", "description"}]` for the tools \
that match, and `await describe_tool("name")` returns one tool's signature and \
full description. Use them for any tool not listed here.

{{stubs}}
"""

PYTHON_DESCRIPTION = """\
Run a {{language_label}} script. Every other tool is an async function on `tools` \
(`await tools.read(path="a.txt")`); only what the script prints and its last \
expression's value are returned. {{lifetime_brief}} See "Code mode" in the system \
prompt.
"""

PYTHON_LIFETIMES: dict[str, str] = {
    "script": (
        "Every call starts a fresh interpreter: variables, functions and imports from "
        "earlier calls are gone. Carry anything you need forward by printing it, or "
        "recompute it."
    ),
    "turn": (
        "Calls made while answering one user message share an interpreter: variables, "
        "functions and imports persist from call to call until you reply, then the "
        "interpreter is reset."
    ),
    "session": (
        "The interpreter persists across calls, like a REPL or a notebook kernel: "
        "variables, functions and imports defined in one call are there in the next, "
        "for the rest of this conversation. It is reset after a timeout, a memory-limit "
        "trip or a crash, and the result says so when that happens; redefine what you "
        "need then."
    ),
}

CPYTHON_TEMPLATES = PromptTemplates(
    system=PYTHON_SYSTEM,
    description=PYTHON_DESCRIPTION,
    lifetime=PYTHON_LIFETIMES,
    language_notes=(
        "This is full CPython with its standard library, run in a separate process "
        "with the same file access as the bash tool but no API keys in its "
        "environment. Prefer tools over direct file or network access, so the user "
        "sees what happened."
    ),
    language_label="Python 3",
)

MONTY_TEMPLATES = PromptTemplates(
    system=PYTHON_SYSTEM,
    description=PYTHON_DESCRIPTION,
    lifetime=PYTHON_LIFETIMES,
    language_notes=(
        "The interpreter is Monty, a sandboxed subset of Python. It has functions, "
        "closures, comprehensions, f-strings, try/except, dataclasses and async/await, "
        "and these modules: asyncio, json, re, math, datetime, collections, itertools, "
        "functools, typing, random, time, base64. It does NOT have class inheritance "
        "(so no custom exception classes), generators or `yield` (a generator "
        "expression builds a list, so `next(x for x in ...)` fails: index a list "
        "comprehension instead), `match`, `del`, or "
        "any third-party package, and it has no filesystem or network: use tools for "
        "those. `asyncio.gather` takes no keyword arguments, so there is no "
        "`return_exceptions`: wrap a call in try/except inside a helper coroutine "
        "to keep one failure from failing the gather."
    ),
    language_label="Python (the Monty subset)",
)

TYPESCRIPT_TEMPLATES = PromptTemplates(
    system="""\
## Code mode

The `{{tool_name}}` tool runs {{language_label}} that you write, as the body of an \
async function. Every other tool is an async method on the ambient `tools` object, \
taking one object of named arguments: `const text = await tools.read({ path: \
"setup.py" })`. A tool resolves to its text output; a tool that fails rejects with \
an `Error` carrying the tool's message.

Run independent calls together with `await Promise.all([...])` and process results \
inside the script. Only what you `console.log`, and the value you `return`, comes \
back to you.

{{lifetime_text}}

{{language_notes}}

Limits per call: {{limits}}.

`await searchTools("query")` and `await describeTool("name")` find tools not \
listed here.

{{stubs}}
""",
    description="""\
Run a {{language_label}} script. Every other tool is an async method on `tools` \
(`await tools.read({ path: "a.txt" })`); only `console.log` output and the returned \
value come back. {{lifetime_brief}}
""",
    lifetime={
        "script": "Every call runs in a fresh isolate: nothing from an earlier call survives.",
        "turn": (
            "Calls made while answering one user message share an isolate; globals "
            "persist until you reply."
        ),
        "session": (
            "The isolate persists across calls, like a REPL: top-level `globalThis` "
            "assignments are there in the next call. `const`/`let` are per call."
        ),
    },
    language_notes="There is no `require`, no `fetch` and no filesystem: use tools.",
    language_label="TypeScript",
)
