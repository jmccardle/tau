"""τ-agent-core tools: LS tool.

List directory contents.

Reference: PHASE-2-SUBPHASE-3.md, "ls tool" section.
"""

from __future__ import annotations

import os
import stat
from datetime import datetime
from typing import Any, Callable

from tau_agent_core.tools.base import AgentToolResult


class LsTool:
    """Ls tool: List directory contents.

    Attributes:
        name: Tool name identifier
        label: Human-readable label
        description: Tool description for LLM
        parameters: JSON Schema for arguments
        execution_mode: "parallel"
        cwd: Working directory for relative paths
    """

    name = "ls"
    label = "List Directory"
    description = (
        "List directory contents. "
        "Supports listing hidden files and long format output."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to list (default: current directory)",
            },
            "all": {
                "type": "boolean",
                "description": "Include hidden files (default: False)",
            },
            "long": {
                "type": "boolean",
                "description": "Use long format (default: False)",
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
        """Execute the ls tool.

        Args:
            tool_call_id: Unique identifier for the tool call
            args: Tool arguments dict with optional 'path', 'all', 'long'
            signal: Optional AbortSignal
            on_update: Optional callback for progress updates

        Returns:
            Dict with 'content' list of content blocks and 'details' dict
        """
        list_path = args.get("path", ".")
        show_all = args.get("all", False)
        long_format = args.get("long", False)

        if not list_path:
            list_path = "."

        if not os.path.isabs(list_path):
            list_path = os.path.join(self.cwd, list_path)
        list_path = os.path.abspath(list_path)

        if not os.path.exists(list_path):
            return AgentToolResult(
                tool_name=self.name,
                tool_call_id=tool_call_id,
                content=[{"type": "text", "text": f"Path not found: {list_path}"}],
            ).model_dump()

        if not os.path.isdir(list_path):
            return AgentToolResult(
                tool_name=self.name,
                tool_call_id=tool_call_id,
                content=[{"type": "text", "text": f"Not a directory: {list_path}"}],
            ).model_dump()

        entries = os.listdir(list_path)

        # Filter hidden files
        if not show_all:
            entries = [e for e in entries if not e.startswith(".")]

        # Sort entries
        entries.sort()

        if long_format:
            result_text = self._long_format(list_path, entries)
        else:
            result_text = "\n".join(entries) if entries else "(empty directory)"

        result = AgentToolResult(
            tool_name=self.name,
            tool_call_id=tool_call_id,
            content=[{"type": "text", "text": result_text}],
        )
        result_dict = result.model_dump()
        result_dict["details"] = {
            "paths": entries,
            "count": len(entries),
        }
        return result_dict

    @staticmethod
    def _long_format(dir_path: str, entries: list[str]) -> str:
        """Format directory listing in long format."""
        lines = []
        total_size = 0

        # Calculate total size first
        for entry in entries:
            full_path = os.path.join(dir_path, entry)
            try:
                st = os.lstat(full_path)
                if stat.S_ISREG(st.st_mode):
                    total_size += st.st_size
            except OSError:
                pass

        lines.append(f"total {total_size // 1024}")

        for entry in entries:
            full_path = os.path.join(dir_path, entry)
            try:
                st = os.lstat(full_path)
                mode = stat.filemode(st.st_mode)
                nlinks = st.st_nlink
                size = st.st_size
                mtime = datetime.fromtimestamp(st.st_mtime).strftime("%b %d %H:%M")

                if stat.S_ISLNK(st.st_mode):
                    try:
                        target = os.readlink(full_path)
                        name = f"{entry} -> {target}"
                    except OSError:
                        name = entry
                elif stat.S_ISDIR(st.st_mode):
                    name = f"{entry}/"
                else:
                    name = entry

                lines.append(f"{mode:<10} {nlinks:<4} {size:>10} {mtime} {name}")
            except OSError:
                lines.append(f"{'?':<10} ?            ? ? {entry}")

        return "\n".join(lines)
