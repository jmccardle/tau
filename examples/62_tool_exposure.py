"""Example 62: tools that call tools, a deferred tool, and typed returns (experimental)

Three tools, each showing one seam from experiments/tool-exposure/RESULTS.md:

- ``todo_scan`` (``direct``) calls the built-in ``grep`` through
  ``ctx.execute_tool``. The call passes the same ``tool_call`` and
  ``tool_result`` hooks a model-issued call does, and it is recorded on
  ``todo_scan``'s result as ``details["nested_calls"]``. It reads ``grep``'s
  structured value, not its text.
- ``tool_search`` (``model-only``) finds ``deferred`` tools by keyword and loads
  them by setting their exposure to ``direct``. The change is a config entry on
  the tree, so a resumed session still has the tool. Its own description never
  changes, which keeps the prompt cache intact until a load happens.
- ``word_stats`` (``deferred``) is built by ``tool_from_function`` from an
  annotated function: its parameters come from the signature and its output
  schema from the return annotation.

On Python 3.11, pydantic only reads a ``TypedDict`` from ``typing_extensions``.

## Usage

```python
import importlib.util
import sys

_spec = importlib.util.spec_from_file_location("ext", "examples/62_tool_exposure.py")
ext = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ext
_spec.loader.exec_module(ext)
```

or ``tau -e examples/62_tool_exposure.py``.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from typing_extensions import TypedDict

from tau_agent_core.tool_schema import tool_from_function


class TodoReport(TypedDict):
    """Open TODO/FIXME markers, counted per file."""

    total: int
    by_file: dict[str, int]


async def todo_scan(ctx: Any, path: str = ".") -> TodoReport:
    """Count TODO and FIXME markers under a path, per file."""
    found = await ctx.execute_tool("grep", {"pattern": r"\b(TODO|FIXME)\b", "path": path})
    counts = Counter(m["path"] for m in found.value["matches"])
    return {"total": sum(counts.values()), "by_file": dict(counts.most_common())}


class WordStats(TypedDict):
    """Word statistics for a text."""

    words: int
    unique: int
    longest: str


def word_stats(text: str) -> WordStats:
    """Count the words and unique words in a text, and find the longest word."""
    words = text.split()
    return {
        "words": len(words),
        "unique": len(set(words)),
        "longest": max(words, key=len, default=""),
    }


async def tool_search(
    tool_call_id: str, params: dict, signal: Any, on_update: Any, ctx: Any
) -> dict:
    """Load every deferred tool whose name, description or tags mention the query."""
    query = str(params["query"]).lower()
    hits = [
        d
        for d in ctx.list_tools(exposure="deferred")
        if query in d.name.lower()
        or query in d.description.lower()
        or query in map(str.lower, d.tags)
    ]
    for d in hits:
        await ctx.set_tool_exposure(d.name, "direct")
    if not hits:
        return {"content": [{"type": "text", "text": f"No deferred tool matches {query!r}."}]}
    stubs = "\n\n".join(d.stub() for d in hits)
    return {"content": [{"type": "text", "text": f"Loaded {len(hits)} tool(s):\n\n{stubs}"}]}


TOOL_SEARCH = {
    "name": "tool_search",
    "label": "Tool search",
    "description": (
        "Find and load additional tools by keyword. Loaded tools can be called "
        "from your next step; the result shows their signatures."
    ),
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "A keyword, e.g. 'text'"}},
        "required": ["query"],
    },
    "execute": tool_search,
    "exposure": "model-only",
}


def register(api: Any) -> None:
    """Register ``todo_scan``, ``tool_search`` and the deferred ``word_stats``."""
    api.register_tool(tool_from_function(todo_scan, tags=["code"]))
    api.register_tool(TOOL_SEARCH)
    api.register_tool(tool_from_function(word_stats, exposure="deferred", tags=["text"]))
