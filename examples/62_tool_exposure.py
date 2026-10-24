"""Example 62: tools that call tools, a deferred tool, and typed returns (experimental)

Three tools, each showing one seam from experiments/tool-exposure/RESULTS.md:

- ``todo_scan`` (``direct``) calls the built-in ``grep`` through
  ``ctx.execute_tool``. The call passes the same ``tool_call`` and
  ``tool_result`` hooks a model-issued call does, and it is recorded on
  ``todo_scan``'s result as ``details["nested_calls"]``. It reads ``grep``'s
  structured value, not its text.
- ``tool_search`` and ``call_tool`` (both ``model-only``, from
  ``tau_agent_core.tool_dispatch``) reach ``deferred`` tools without changing the
  declared ``tools`` block. ``tool_search`` returns the matches' stubs in its
  result, and the model calls one through ``call_tool``. The prompt cache holds.
- ``word_stats`` (``deferred``) is built by ``tool_from_function`` from an
  annotated function: its parameters come from the signature and its output
  schema from the return annotation.

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
from typing import Any, TypedDict

from tau_agent_core.tool_dispatch import call_tool_definition, tool_search_definition
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


def register(api: Any) -> None:
    """Register ``todo_scan``, ``tool_search``, ``call_tool`` and the deferred ``word_stats``."""
    api.register_tool(tool_from_function(todo_scan, tags=["code"]))
    api.register_tool(tool_search_definition())
    api.register_tool(call_tool_definition())
    api.register_tool(tool_from_function(word_stats, exposure="deferred", tags=["text"]))
