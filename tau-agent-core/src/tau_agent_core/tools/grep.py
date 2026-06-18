"""τ-agent-core tools: Grep tool.

Search files with regex patterns.

Reference: PHASE-2-SUBPHASE-3.md, "grep tool" section.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable

from tau_agent_core.tools.base import AgentToolResult


class GrepTool:
    """Grep tool: Search files with regex.

    Attributes:
        name: Tool name identifier
        label: Human-readable label
        description: Tool description for LLM
        parameters: JSON Schema for arguments
        execution_mode: "parallel"
        cwd: Working directory for relative paths
    """

    name = "grep"
    label = "Search Files"
    description = (
        "Search for a pattern in files using regex. "
        "Supports searching in a directory or specific files. "
        "Returns file:line:matched_text for each match."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for",
            },
            "path": {
                "type": "string",
                "description": "Path to search in (default: current directory)",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of files to search (overrides path)",
            },
            "ignore_case": {
                "type": "boolean",
                "description": "Case-insensitive search (default: False)",
            },
        },
        "required": ["pattern"],
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
        """Execute the grep tool.

        Args:
            tool_call_id: Unique identifier for the tool call
            args: Tool arguments dict with 'pattern', optional 'path', 'files', 'ignore_case'
            signal: Optional AbortSignal
            on_update: Optional callback for progress updates

        Returns:
            Dict with 'content' list of content blocks and 'details' dict
        """
        pattern = args.get("pattern")
        if not pattern:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message='Missing required argument: "pattern"',
                tool_call_id=tool_call_id,
            ).model_dump()

        try:
            flags = re.IGNORECASE if args.get("ignore_case", False) else 0
            compiled = re.compile(pattern, flags)
        except re.error as e:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message=f"Invalid regex pattern: {e}",
                tool_call_id=tool_call_id,
            ).model_dump()

        target_path = args.get("path", ".")
        files_list = args.get("files")

        if target_path:
            if not os.path.isabs(target_path):
                target_path = os.path.join(self.cwd, target_path)
            target_path = os.path.abspath(target_path)

        files_searched = 0
        matches = []

        if files_list:
            # Search in specific files
            for file_path in files_list:
                if not os.path.isabs(file_path):
                    file_path = os.path.join(self.cwd, file_path)
                file_path = os.path.abspath(file_path)

                if not os.path.isfile(file_path):
                    continue

                files_searched += 1
                match_lines = self._search_file(file_path, compiled, target_path)
                matches.extend(match_lines)
        elif os.path.isfile(target_path):
            # Single file search
            files_searched = 1
            matches = self._search_file(target_path, compiled)
        else:
            # Directory search - find all files
            search_dir = target_path if os.path.isdir(target_path) else self.cwd
            for root, dirs, filenames in os.walk(search_dir):
                for fname in filenames:
                    if fname.startswith("."):
                        continue
                    file_path = os.path.join(root, fname)

                    # Skip binary files
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            _ = f.read()
                    except (UnicodeDecodeError, PermissionError):
                        continue

                    files_searched += 1
                    match_lines = self._search_file(file_path, compiled, search_dir)
                    matches.extend(match_lines)

        if not matches:
            result_text = f"No matches found in {files_searched} file(s)"
        else:
            result_text = "\n".join(matches)

        result = AgentToolResult(
            tool_name=self.name,
            tool_call_id=tool_call_id,
            content=[{"type": "text", "text": result_text}],
        )
        result_dict = result.model_dump()
        result_dict["details"] = {
            "matches": len(matches),
            "files_searched": files_searched,
        }
        return result_dict

    @staticmethod
    def _search_file(file_path: str, pattern: re.Pattern, base_path: str | None = None) -> list[str]:
        """Search a single file for pattern matches."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except (UnicodeDecodeError, PermissionError, OSError):
            return []

        rel_path = file_path
        if base_path and file_path.startswith(base_path):
            rel_path = os.path.relpath(file_path, base_path)

        results = []
        for i, line in enumerate(lines, 1):
            line_content = line.rstrip("\n\r")
            if pattern.search(line_content):
                results.append(f"{rel_path}:{i}:{line_content}")

        return results
