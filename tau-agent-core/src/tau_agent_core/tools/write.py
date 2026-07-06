"""τ-agent-core tools: Write tool.

Writes content to files (atomic writes, encoding handling).

Reference: PHASE-2-SUBPHASE-3.md, "write tool" section.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Callable

from tau_agent_core.tools.base import AgentToolResult


class WriteTool:
    """Write tool: Write files (atomic writes, encoding handling).

    Attributes:
        name: Tool name identifier
        label: Human-readable label
        description: Tool description for LLM
        parameters: JSON Schema for arguments
        execution_mode: "sequential"
        cwd: Working directory for relative paths
    """

    name = "write"
    label = "Write File"
    description = (
        "Write content to a file at the given path. "
        "Creates parent directories if they don't exist. "
        "Uses atomic writes (temp file + rename) for safety."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to write (relative or absolute)",
            },
            "content": {
                "type": "string",
                "description": "Content to write to the file",
            },
            "encoding": {
                "type": "string",
                "description": "File encoding (default: utf-8)",
            },
        },
        "required": ["path", "content"],
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
        """Execute the write tool.

        Args:
            tool_call_id: Unique identifier for the tool call
            args: Tool arguments dict with 'path', 'content', optional 'encoding'
            signal: Optional AbortSignal
            on_update: Optional callback for progress updates

        Returns:
            Dict with 'content' list of content blocks and 'details' dict
        """
        path = args.get("path")
        content = args.get("content")
        if not path:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message='Missing required argument: "path"',
                tool_call_id=tool_call_id,
            ).model_dump()
        if content is None:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message='Missing required argument: "content"',
                tool_call_id=tool_call_id,
            ).model_dump()

        encoding = args.get("encoding", "utf-8")

        # Resolve path relative to cwd
        if not os.path.isabs(path):
            resolved_path = os.path.join(self.cwd, path)
        else:
            resolved_path = path

        # Ensure parent directories exist
        parent_dir = os.path.dirname(resolved_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        # Check if target is a directory
        if os.path.isdir(resolved_path):
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message=f"Cannot write to directory: {resolved_path}",
                tool_call_id=tool_call_id,
            ).model_dump()

        # Atomic write: write to temp file then rename
        try:
            dir_name = os.path.dirname(resolved_path) or "."
            fd, temp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp_write_")
            try:
                with os.fdopen(fd, "w", encoding=encoding, errors="replace") as f:
                    f.write(content)
                os.replace(temp_path, resolved_path)
            except Exception:
                # Clean up temp file on error
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message=f"Error writing file: {e}",
                tool_call_id=tool_call_id,
            ).model_dump()

        lines = content.split("\n")
        bytes_written = len(content.encode(encoding, errors="replace"))

        result = AgentToolResult(
            tool_name=self.name,
            tool_call_id=tool_call_id,
            content=[
                {
                    "type": "text",
                    "text": f"Wrote {len(lines)} lines to {path}",
                }
            ],
        )
        result_dict = result.model_dump()
        result_dict["details"] = {
            "path": path,
            "lines": len(lines),
            "bytes": bytes_written,
        }
        return result_dict
