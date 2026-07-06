"""τ-agent-core tools: Read tool.

Reads file content with truncation and image support.

Reference: PHASE-2-SUBPHASE-3.md, "read tool" section.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Callable

from tau_agent_core.tools.base import AgentToolResult


class ReadTool:
    """Read tool: Read files (with truncation, image support).

    Attributes:
        name: Tool name identifier
        label: Human-readable label
        description: Tool description for LLM
        parameters: JSON Schema for arguments
        execution_mode: "parallel"
        cwd: Working directory for relative paths
    """

    name = "read"
    label = "Read File"
    description = (
        "Read the contents of a file at the given path. "
        "Supports text files and images (jpg, png, gif, webp). "
        "Returns file content with optional line/byte truncation."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read (relative or absolute)",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (1-indexed, optional)",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read (optional, default 4096)",
            },
        },
        "required": ["path"],
    }
    execution_mode = "parallel"

    DEFAULT_MAX_LINES = 4096
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

    def __init__(self, cwd: str = ".") -> None:
        self.cwd = os.path.abspath(cwd)

    async def execute(
        self,
        tool_call_id: str,
        args: dict,
        signal: Any = None,
        on_update: Callable | None = None,
    ) -> dict:
        """Execute the read tool.

        Args:
            tool_call_id: Unique identifier for the tool call
            args: Tool arguments dict with 'path', optional 'offset', 'limit'
            signal: Optional AbortSignal
            on_update: Optional callback for progress updates

        Returns:
            Dict with 'content' list of content blocks and 'details' dict

        Raises:
            ValueError: If required arguments are missing
        """
        path = args.get("path")
        if not path:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message='Missing required argument: "path"',
                tool_call_id=tool_call_id,
            ).model_dump()

        # Resolve path relative to cwd if not absolute
        if not os.path.isabs(path):
            resolved_path = os.path.join(self.cwd, path)
        else:
            resolved_path = path

        # Follow symlinks
        resolved_path = os.path.realpath(resolved_path)

        # Check file exists
        if not os.path.exists(resolved_path):
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message=f"Error: File not found: {path}",
                tool_call_id=tool_call_id,
            ).model_dump()

        # Check for image files
        _, ext = os.path.splitext(resolved_path)
        if ext.lower() in self.IMAGE_EXTENSIONS:
            return await self._read_image(resolved_path, tool_call_id, signal)

        # Read as text
        return await self._read_text(resolved_path, args, tool_call_id, signal)

    async def _read_text(
        self,
        resolved_path: str,
        args: dict,
        tool_call_id: str,
        signal: Any = None,
    ) -> dict:
        """Read a text file with optional truncation."""
        try:
            with open(resolved_path, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            try:
                with open(resolved_path, "r", encoding="latin-1") as f:
                    content = f.read()
            except Exception as e:
                return AgentToolResult.from_error(
                    tool_name=self.name,
                    error_message=f"Error reading file: {e}",
                    tool_call_id=tool_call_id,
                ).model_dump()

        # Check for binary content
        if "\x00" in content:
            return AgentToolResult(
                tool_name=self.name,
                tool_call_id=tool_call_id,
                content=[{"type": "text", "text": "Binary file"}],
            ).model_dump()

        lines = content.split("\n") if content else []
        # Handle empty file: content="" -> lines=[""] which is 1 empty line
        # We treat a truly empty file as 0 lines
        if content == "":
            lines = []

        offset = args.get("offset", 1)
        limit = args.get("limit", self.DEFAULT_MAX_LINES)

        if offset > 0:
            lines = lines[offset - 1 :]

        truncated = len(lines) > limit
        if truncated:
            lines = lines[:limit]

        result = AgentToolResult(
            tool_name=self.name,
            tool_call_id=tool_call_id,
            content=[{"type": "text", "text": "\n".join(lines)}],
        )
        result_dict = result.model_dump()
        result_dict["details"] = {
            "lines_read": len(lines),
            "truncated": truncated,
            "path": args.get("path", resolved_path),
        }
        return result_dict

    async def _read_image(
        self,
        resolved_path: str,
        tool_call_id: str,
        signal: Any = None,
    ) -> dict:
        """Read an image file and return base64-encoded data."""
        try:
            with open(resolved_path, "rb") as f:
                image_data = f.read()
            b64_data = base64.b64encode(image_data).decode("utf-8")
            _, ext = os.path.splitext(resolved_path)
            mime_map = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }
            mime_type = mime_map.get(ext.lower(), "application/octet-stream")
            text = f"![image]({mime_type};base64,{b64_data[:200]}...)"
            return AgentToolResult(
                tool_name=self.name,
                tool_call_id=tool_call_id,
                content=[{"type": "text", "text": text}],
            ).model_dump()
        except Exception as e:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message=f"Error reading image: {e}",
                tool_call_id=tool_call_id,
            ).model_dump()
