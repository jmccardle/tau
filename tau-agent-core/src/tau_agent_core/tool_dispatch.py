"""``tool_search`` and ``call_tool``: deferred tools without changing the ``tools`` block.

Experimental (experiments/tool-exposure/RESULTS.md). A ``deferred`` tool is never
declared to the model. ``tool_search`` returns the matches as Python stubs in its
result, which is an appended message, so the cached prefix holds. The model
then calls a match through ``call_tool``, which is declared from the first
request and never changes. ``call_tool`` runs the target through
``ctx.execute_tool``, so the ``tool_call`` and ``tool_result`` hooks apply and the
call is recorded as a nested call. A script in code mode calls the target
directly instead.

Both are ``register_tool`` dicts with ``exposure="model-only"``. An extension
registers them, or an SDK embedder does through an inline extension::

    def register(api):
        api.register_tool(call_tool_definition())
        api.register_tool(tool_search_definition())
"""

from __future__ import annotations

from typing import Any

from tau_llm.docs import agent_facing

from tau_agent_core.tool_catalog import DISPATCHABLE_EXPOSURES, ToolDescription
from tau_agent_core.tools.base import ToolError

__all__ = ["call_tool_definition", "search_deferred", "tool_search_definition"]

CALL_TOOL_DESCRIPTION = (
    "Call a tool that tool_search found. Pass the tool's name and its arguments as "
    "an object; the result is that tool's result."
)

TOOL_SEARCH_DESCRIPTION = (
    "Search for tools that are not listed here, by keyword. The result gives each "
    "match's Python signature; call a match with call_tool."
)


@agent_facing(topic="tools")
def search_deferred(
    descriptions: list[ToolDescription], query: str, limit: int = 8
) -> list[ToolDescription]:
    """The deferred tools whose name, description or tags contain a word of ``query``.

    Case-insensitive substring match per whitespace-separated word, ranked by
    how many words match, then registry order. Only an effective exposure of
    ``deferred`` is searched.

    Args:
        descriptions: The tools to search, as ``ctx.list_tools()`` returns them.
        query: Keywords; one with no words matches nothing.
        limit: The most matches returned.

    Returns:
        At most ``limit`` matches, best first.
    """
    words = [w for w in query.lower().split() if w]
    scored = []
    for index, d in enumerate(descriptions):
        if d.exposure != "deferred":
            continue
        haystack = " ".join([d.name, d.description, *d.tags]).lower()
        score = sum(1 for w in words if w in haystack)
        if score:
            scored.append((-score, index, d))
    return [d for _, _, d in sorted(scored, key=lambda t: (t[0], t[1]))[:limit]]


async def _tool_search(
    tool_call_id: str, params: dict[str, Any], signal: Any, on_update: Any, ctx: Any
) -> dict[str, Any]:
    """Find deferred tools and return their stubs; declares nothing."""
    query = str(params.get("query", "")).strip()
    if not query:
        raise ToolError("tool_search: query must not be empty")
    hits = search_deferred(ctx.list_tools(), query)
    if not hits:
        return {"content": [{"type": "text", "text": f"No tool matches {query!r}."}]}
    stubs = "\n\n".join(d.stub() for d in hits)
    text = f"Found {len(hits)} tool(s). Call them with call_tool(name, arguments).\n\n{stubs}"
    return {
        "content": [{"type": "text", "text": text}],
        "details": {"found": [d.name for d in hits]},
    }


async def _call_tool(
    tool_call_id: str, params: dict[str, Any], signal: Any, on_update: Any, ctx: Any
) -> dict[str, Any]:
    """Run the named tool through ``ctx.execute_tool`` and return its result."""
    name = str(params.get("name", ""))
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ToolError("call_tool: arguments must be an object")
    try:
        described = ctx.describe_tool(name)
    except KeyError:
        raise ToolError(f"call_tool: no tool named {name!r}; use tool_search") from None
    if described.exposure not in DISPATCHABLE_EXPOSURES:
        raise ToolError(
            f"call_tool: {name!r} is not available to you (it is {described.exposure!r})"
        )
    result = await ctx.execute_tool(name, arguments)
    return {"content": result.content, "value": result.value}


@agent_facing(topic="tools")
def tool_search_definition() -> dict[str, Any]:
    """The ``register_tool`` dict for ``tool_search``; its description never changes.

    Returns:
        A ``model-only`` tool taking ``query`` whose result lists each deferred
        match as a Python stub; an empty query is a tool error.
    """
    return {
        "name": "tool_search",
        "label": "Tool search",
        "description": TOOL_SEARCH_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Keywords"}},
            "required": ["query"],
        },
        "execute": _tool_search,
        "exposure": "model-only",
    }


@agent_facing(topic="tools")
def call_tool_definition() -> dict[str, Any]:
    """The ``register_tool`` dict for ``call_tool``, the fixed dispatcher to deferred tools.

    Returns:
        A ``model-only`` tool taking ``name`` and ``arguments`` that runs a
        ``direct`` or ``deferred`` tool through ``ctx.execute_tool`` and returns
        its content and value; any other target is a tool error.
    """
    return {
        "name": "call_tool",
        "label": "Call tool",
        "description": CALL_TOOL_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The tool's name"},
                "arguments": {"type": "object", "description": "The tool's arguments"},
            },
            "required": ["name", "arguments"],
        },
        "execute": _call_tool,
        "exposure": "model-only",
    }
