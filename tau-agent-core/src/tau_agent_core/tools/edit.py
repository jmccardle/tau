"""τ-agent-core tools: Edit tool.

Search and replace in files (with diff rendering).

Reference: PHASE-2-SUBPHASE-3.md, "edit tool" section.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from tau_agent_core.tools.base import AgentToolResult


class EditTool:
    """Edit tool: Search/replace in files (with diff rendering).

    Attributes:
        name: Tool name identifier
        label: Human-readable label
        description: Tool description for LLM
        parameters: JSON Schema for arguments
        execution_mode: "sequential"
        cwd: Working directory for relative paths
    """

    name = "edit"
    label = "Edit File"
    description = (
        "Replace old_string with new_string in a file. "
        "Validates that old_string exists exactly once by default, "
        "or use replace_all=True for multiple replacements. "
        "Returns diff-like output showing changes."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to edit (relative or absolute)",
            },
            "old_string": {
                "type": "string",
                "description": "Exact text to find and replace",
            },
            "new_string": {
                "type": "string",
                "description": "Text to replace with",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences (default: False)",
            },
        },
        "required": ["path", "old_string", "new_string"],
    }
    execution_mode = "sequential"

    def __init__(self, cwd: str = ".") -> None:
        self.cwd = os.path.abspath(cwd)

    async def execute(
        self,
        tool_call_id: str,
        args: dict,
        signal: Any = None,
        on_update: Callable | None = None,
    ) -> dict:
        """Execute the edit tool.

        Args:
            tool_call_id: Unique identifier for the tool call
            args: Tool arguments dict with 'path', 'old_string', 'new_string',
                  optional 'replace_all'
            signal: Optional AbortSignal
            on_update: Optional callback for progress updates

        Returns:
            Dict with 'content' list of content blocks and 'details' dict
        """
        path = args.get("path")
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        if not path:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message='Missing required argument: "path"',
                tool_call_id=tool_call_id,
            ).model_dump()
        if old_string is None:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message='Missing required argument: "old_string"',
                tool_call_id=tool_call_id,
            ).model_dump()
        if new_string is None:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message='Missing required argument: "new_string"',
                tool_call_id=tool_call_id,
            ).model_dump()

        replace_all = args.get("replace_all", False)

        # Resolve path
        if not os.path.isabs(path):
            resolved_path = os.path.join(self.cwd, path)
        else:
            resolved_path = path

        if not os.path.exists(resolved_path):
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message=f"File not found: {path}",
                tool_call_id=tool_call_id,
            ).model_dump()

        # Read original content
        try:
            with open(resolved_path, "r", encoding="utf-8") as f:
                original = f.read()
        except Exception as e:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message=f"Error reading file: {e}",
                tool_call_id=tool_call_id,
            ).model_dump()

        # Count occurrences
        occurrences = original.count(old_string)

        if occurrences == 0:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message=f'String to replace not found in file: "{old_string}"',
                tool_call_id=tool_call_id,
            ).model_dump()

        if occurrences > 1 and not replace_all:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message=(
                    f"Found {occurrences} occurrences of the string. "
                    f"Use replace_all=True to replace all, or make the old_string more specific."
                ),
                tool_call_id=tool_call_id,
            ).model_dump()

        # Perform replacement
        if replace_all:
            new_content = original.replace(old_string, new_string)
        else:
            new_content = original.replace(old_string, new_string, 1)

        # Write back
        try:
            with open(resolved_path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message=f"Error writing file: {e}",
                tool_call_id=tool_call_id,
            ).model_dump()

        replacements = occurrences if replace_all else 1

        # Generate simple diff
        diff = self._generate_diff(original, new_content)

        result = AgentToolResult(
            tool_name=self.name,
            tool_call_id=tool_call_id,
            content=[
                {
                    "type": "text",
                    "text": f"Replaced {replacements} occurrence(s) in {path}",
                }
            ],
        )
        result_dict = result.model_dump()
        result_dict["details"] = {
            "path": path,
            "replacements": replacements,
            "diff": diff,
        }
        return result_dict

    @staticmethod
    def _generate_diff(original: str, new_content: str) -> str:
        """Generate a simple unified diff between original and new content."""
        orig_lines = original.split("\n")
        new_lines = new_content.split("\n")

        diff_lines = []
        orig_idx = 0
        new_idx = 0

        while orig_idx < len(orig_lines) or new_idx < len(new_lines):
            if orig_idx >= len(orig_lines):
                diff_lines.append(f"+ {new_lines[new_idx]}")
                new_idx += 1
            elif new_idx >= len(new_lines):
                diff_lines.append(f"- {orig_lines[orig_idx]}")
                orig_idx += 1
            elif orig_lines[orig_idx] == new_lines[new_idx]:
                diff_lines.append(f"  {orig_lines[orig_idx]}")
                orig_idx += 1
                new_idx += 1
            else:
                diff_lines.append(f"- {orig_lines[orig_idx]}")
                diff_lines.append(f"+ {new_lines[new_idx]}")
                orig_idx += 1
                new_idx += 1

        return "\n".join(diff_lines)
