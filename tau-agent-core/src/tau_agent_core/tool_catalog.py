"""What an extension can learn about the tools and extensions loaded beside it.

Experimental (experiments/tool-exposure/RESULTS.md). Discovery returns
metadata only. A tool's ``execute`` is never handed out: the one way to run
another tool is ``ctx.execute_tool``, which goes through the hooks, so nothing
found here is a way around a permission gate.

A tool's *effective* exposure is its declared ``exposure`` unless the cursor's
path carries a ``tool_exposure`` config entry naming it (docs/CURSORS.md §5),
so a resumed or forked conversation declares what it declared before.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any

from tau_llm.docs import agent_facing

from tau_agent_core.tool_schema import render_stub
from tau_agent_core.tools.base import AgentTool

__all__ = [
    "CALLABLE_EXPOSURES",
    "DECLARED_EXPOSURES",
    "ExtensionInfo",
    "ToolDescription",
    "describe_tool",
    "match_tools",
]

DECLARED_EXPOSURES = frozenset({"direct", "model-only"})
"""Exposures the model is offered."""

CALLABLE_EXPOSURES = frozenset({"direct", "callable", "deferred"})
"""Exposures a tool, extension or script may call through ``ctx.execute_tool``."""


@agent_facing(topic="extensions")
@dataclass(frozen=True)
class ToolDescription:
    """One tool as discovery reports it.

    Attributes:
        name: What the model and ``ctx.execute_tool`` call it.
        label: The human label.
        description: What the model reads.
        namespace: ``"core"``, or the extension (``<ext>`` / ``<ext>.<sub>``) that declared it.
        exposure: The effective exposure on the asking cursor.
        declared_exposure: The author's default.
        tags: Discovery labels.
        parameters: The argument JSON Schema.
        output_schema: The structured value's JSON Schema, or ``None``.
        execution_mode: ``"parallel"`` or ``"sequential"``.
    """

    name: str
    label: str
    description: str
    namespace: str
    exposure: str
    declared_exposure: str
    tags: tuple[str, ...]
    parameters: dict[str, Any]
    output_schema: dict[str, Any] | None
    execution_mode: str

    @property
    def declared_to_model(self) -> bool:
        """Whether the model is offered this tool."""
        return self.exposure in DECLARED_EXPOSURES

    @property
    def callable(self) -> bool:
        """Whether ``ctx.execute_tool`` may run this tool."""
        return self.exposure in CALLABLE_EXPOSURES

    def stub(self) -> str:
        """This tool as a Python stub (:func:`~tau_agent_core.tool_schema.render_stub`)."""
        return render_stub(self.name, self.description, self.parameters, self.output_schema)


@agent_facing(topic="extensions")
@dataclass(frozen=True)
class ExtensionInfo:
    """One loaded extension as discovery reports it.

    Attributes:
        name: The extension's name (its file stem, or an inline factory's name).
        path: Its path label.
        tools: The tool names it registered.
        commands: The qualified command names it registered.
    """

    name: str
    path: str
    tools: tuple[str, ...]
    commands: tuple[str, ...]


def describe_tool(tool: AgentTool, exposure: str) -> ToolDescription:
    """Describe ``tool`` with ``exposure`` as its effective exposure."""
    d = tool.definition
    return ToolDescription(
        name=d.name,
        label=d.label,
        description=d.description,
        namespace=d.namespace or "core",
        exposure=exposure,
        declared_exposure=d.exposure,
        tags=tuple(d.tags),
        parameters=d.parameters,
        output_schema=d.output_schema,
        execution_mode=d.execution_mode,
    )


@agent_facing(topic="extensions")
def match_tools(
    descriptions: list[ToolDescription],
    pattern: str | None = None,
    *,
    namespace: str | None = None,
    tag: str | None = None,
    exposure: str | None = None,
) -> list[ToolDescription]:
    """Filter ``descriptions``; every filter given must match, in registry order.

    Args:
        descriptions: What to filter.
        pattern: A glob over the tool name (``"git_*"``); an exact name is a glob too.
        namespace: A glob over the namespace (``"mcp.*"``, ``"core"``).
        tag: A tag the tool must carry.
        exposure: The effective exposure the tool must have.
    """
    found = []
    for d in descriptions:
        if pattern is not None and not fnmatch.fnmatchcase(d.name, pattern):
            continue
        if namespace is not None and not fnmatch.fnmatchcase(d.namespace, namespace):
            continue
        if tag is not None and tag not in d.tags:
            continue
        if exposure is not None and d.exposure != exposure:
            continue
        found.append(d)
    return found
