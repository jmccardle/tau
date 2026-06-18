"""τ-agent-core tools: Find tool.

Find files by glob or regex pattern.

Reference: PHASE-2-SUBPHASE-3.md, "find tool" section.
"""

from __future__ import annotations

import os
import fnmatch
import re
from typing import Any, Callable

from tau_agent_core.tools.base import AgentToolResult


class FindTool:
    """Find tool: Find files by glob or regex.

    Attributes:
        name: Tool name identifier
        label: Human-readable label
        description: Tool description for LLM
        parameters: JSON Schema for arguments
        execution_mode: "parallel"
        cwd: Working directory for relative paths
    """

    name = "find"
    label = "Find Files"
    description = (
        "Find files matching criteria. "
        "Supports glob patterns, regex patterns, and file type filters. "
        "Returns list of matching file paths."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Root path to search in (default: current directory)",
            },
            "name": {
                "type": "string",
                "description": "Glob pattern to match filenames (e.g., '*.txt')",
            },
            "type": {
                "type": "string",
                "description": "File type filter: 'f' (file), 'd' (directory), 'l' (symlink)",
            },
            "regex": {
                "type": "string",
                "description": "Regex pattern to match filenames",
            },
        },
    }
    execution_mode = "parallel"

    def __init__(self, cwd: str = ".") -> None:
        self.cwd = os.path.abspath(cwd)

    async def execute(
        self,
        tool_call_id: str,
        args: dict,
        signal: Any = None,
        on_update: Callable | None = None,
    ) -> dict:
        """Execute the find tool.

        Args:
            tool_call_id: Unique identifier for the tool call
            args: Tool arguments dict with optional 'path', 'name', 'type', 'regex'
            signal: Optional AbortSignal
            on_update: Optional callback for progress updates

        Returns:
            Dict with 'content' list of content blocks and 'details' dict
        """
        search_path = args.get("path", ".")
        name_pattern = args.get("name")
        type_filter = args.get("type")
        regex_pattern = args.get("regex")

        if not search_path:
            search_path = "."

        if not os.path.isabs(search_path):
            search_path = os.path.join(self.cwd, search_path)
        search_path = os.path.abspath(search_path)

        if not os.path.exists(search_path):
            return AgentToolResult(
                tool_name=self.name,
                tool_call_id=tool_call_id,
                content=[{"type": "text", "text": f"Path not found: {search_path}"}],
            ).model_dump()

        # Compile regex if provided
        regex_compiled = None
        if regex_pattern:
            try:
                regex_compiled = re.compile(regex_pattern)
            except re.error as e:
                return AgentToolResult.from_error(
                    tool_name=self.name,
                    error_message=f"Invalid regex pattern: {e}",
                    tool_call_id=tool_call_id,
                ).model_dump()

        # Store raw glob pattern for fnmatch
        raw_name_pattern = name_pattern

        results = []

        for root, dirs, files in os.walk(search_path):
            # Check directories
            for d in dirs:
                full_path = os.path.join(root, d)
                rel_path = os.path.relpath(full_path, search_path)

                if not self._matches(rel_path, d, raw_name_pattern, regex_compiled, type_filter):
                    continue

                if type_filter == "d":
                    results.append(rel_path)

            # Check files
            for f in files:
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, search_path)

                if not self._matches(rel_path, f, raw_name_pattern, regex_compiled, type_filter):
                    continue

                if type_filter == "f":
                    results.append(rel_path)
                elif type_filter == "l" and os.path.islink(full_path):
                    results.append(rel_path)
                elif type_filter is None:
                    results.append(rel_path)

        result_text = "\n".join(results) if results else "No files found"

        result = AgentToolResult(
            tool_name=self.name,
            tool_call_id=tool_call_id,
            content=[{"type": "text", "text": result_text}],
        )
        result_dict = result.model_dump()
        result_dict["details"] = {
            "paths": results,
            "count": len(results),
        }
        return result_dict

    @staticmethod
    def _matches(
        rel_path: str,
        basename: str,
        name_pattern: Any,
        regex_compiled: Any,
        type_filter: Any,
    ) -> bool:
        """Check if a file matches all filters."""
        # Check glob pattern (name_pattern is the raw glob string like "*.txt")
        if name_pattern and not fnmatch.fnmatch(basename, name_pattern):
            return False

        # Check regex pattern
        if regex_compiled and not regex_compiled.search(basename):
            return False

        return True
