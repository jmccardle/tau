"""τ-agent-core tools: Read tool.

Reads file content with truncation and image support.

Reference: PHASE-2-SUBPHASE-3.md, "read tool" section.
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any, Callable, Literal

from tau_agent_core.tools.base import AgentToolResult
from tau_agent_core.tools.image_resize import (
    DEFAULT_MAX_IMAGE_DIMENSION,
    ImageSupportUnavailable,
    resize_image,
)


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
    # Annotated rather than left to inference (B1/tau-004): unannotated,
    # `execution_mode = "parallel"` infers `str`, and `ToolDefinition`
    # declares it `Literal["sequential", "parallel"]`. `sdk._resolve_tools`
    # copies this value into a ToolDefinition, so without the annotation mypy
    # cannot check that copy — which is the blindness B1 exists to remove.
    execution_mode: Literal["sequential", "parallel"] = "parallel"

    DEFAULT_MAX_LINES = 4096
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

    def __init__(
        self,
        cwd: str = ".",
        max_image_dimension: int | None = DEFAULT_MAX_IMAGE_DIMENSION,
    ) -> None:
        """Construct the read tool.

        Args:
            cwd: Working directory that relative paths resolve against.
            max_image_dimension: Largest width or height, in pixels, that an
                image may have when it reaches the model. ``None`` disables the
                cap and sends the file at its on-disk resolution — an explicit
                opt-out, chosen by the operator, and NOT what happens when the
                resize is merely unavailable. Without Pillow installed, reading
                an image fails and names the extra. See
                :mod:`tau_agent_core.tools.image_resize` for why the default is
                a chosen budget rather than a model property.
        """
        self.cwd = os.path.abspath(cwd)
        self.max_image_dimension = max_image_dimension

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
        # Off the event loop (docs/PLAN-0.9.4.md §8): the agent loop runs as an
        # async Textual worker on the app's own loop, with no thread of its own,
        # so a blocking read froze painting and input for its duration. A large
        # file, or one on a slow or network filesystem, is the visible case.
        try:
            content = await asyncio.to_thread(self._read_all, resolved_path, "utf-8")
        except UnicodeDecodeError:
            try:
                content = await asyncio.to_thread(self._read_all, resolved_path, "latin-1")
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

    @staticmethod
    def _read_all(resolved_path: str, encoding: str) -> str:
        """Read a whole text file in a worker thread.

        Args:
            resolved_path: Absolute path to read.
            encoding: Text encoding to decode with.

        Returns:
            The file's full decoded contents.
        """
        with open(resolved_path, "r", encoding=encoding) as f:
            return f.read()

    @staticmethod
    def _read_bytes(resolved_path: str) -> bytes:
        """Read a whole binary file in a worker thread.

        Args:
            resolved_path: Absolute path to read.

        Returns:
            The file's raw bytes.
        """
        with open(resolved_path, "rb") as f:
            return f.read()

    async def _read_image(
        self,
        resolved_path: str,
        tool_call_id: str,
        signal: Any = None,
    ) -> dict:
        """Read an image file and return it as an image content block.

        The block is ``{"type": "image", ...}`` — the serialised form of
        :class:`tau_llm.types.ImageContent`, which is what
        ``ToolResultMessage.content`` has always declared it accepts
        (``list[TextContent | ImageContent]``). Each provider decides how to put
        it on the wire.

        **This used to return text**, and the text was
        ``f"![image]({mime};base64,{b64[:200]}...)"`` — the first 200 characters
        of the base64 and a literal ellipsis. Nothing downstream could recover
        the image from that, so a vision model was handed a filename, a mime
        type and a stub, and did the only thing it could: describe an image it
        had never seen. Measured on a 1.9 MB PNG, the model received 230
        characters. The failure is silent, reads as a model hallucinating, and
        survives any amount of correct multimodal configuration underneath it.

        The text block is kept alongside the image on purpose. All three clients
        carry the image now, each in the place its wire format has for one — a
        ``tool_result`` block for Anthropic, ``functionResponse`` plus a user
        turn for Google, a ``tool`` message plus a user turn for OpenAI — but the
        text is what a model gets when the image cannot travel: an endpoint that
        rejects it, or a model with no vision. Then it learns *that* there is an
        image and what it is, rather than inventing the contents. Degrading to
        "an image was here" is worth one line of prose.

        The image IS bounded: no side exceeds ``max_image_dimension``, 2000px by
        default, matching pi's ``autoResizeImages``. That cap exists because an
        unbounded one took a llama.cpp server down twice — see
        :mod:`tau_agent_core.tools.image_resize` for the measurement and for why
        the number is an operator's budget rather than a model property. An
        image already inside the cap is passed through byte-identical.

        Two limits remain. There is no BYTE budget, so a photographic 2000x2000
        PNG can still exceed Anthropic's 5 MB inline limit (pi re-encodes down a
        quality ladder to 4.5 MB; τ does not). And there is still no modality
        check: τ has no field saying whether a model accepts images at all, so
        it sends one and lets the endpoint answer.
        """
        try:
            image_data = await asyncio.to_thread(self._read_bytes, resolved_path)
            _, ext = os.path.splitext(resolved_path)
            mime_map = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }
            mime_type = mime_map.get(ext.lower(), "application/octet-stream")
            name = os.path.basename(resolved_path)

            # Off the event loop for the same reason the file read is: decoding
            # and rescaling a large image is CPU-bound, and the agent loop runs
            # as an async worker on the Textual app's own loop.
            bounded = None
            if self.max_image_dimension is not None:
                bounded = await asyncio.to_thread(
                    resize_image, image_data, mime_type, self.max_image_dimension
                )
                image_data, mime_type = bounded.data, bounded.mime_type

            detail = f"{mime_type}, {len(image_data)} bytes"
            if bounded is not None:
                detail += f", {bounded.size[0]}x{bounded.size[1]}"
                if bounded.resized:
                    w, h = bounded.original_size
                    detail += f", resized from {w}x{h}"
            b64_data = base64.b64encode(image_data).decode("utf-8")
            return AgentToolResult(
                tool_name=self.name,
                tool_call_id=tool_call_id,
                content=[
                    {"type": "text", "text": f"[image: {name} ({detail})]"},
                    {"type": "image", "data": b64_data, "mime_type": mime_type},
                ],
            ).model_dump()
        except ImageSupportUnavailable as e:
            # Not folded into the generic handler below: this one is actionable,
            # and prefixing it with "Error reading image" would bury the
            # instruction that is the whole point of the message.
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message=str(e),
                tool_call_id=tool_call_id,
            ).model_dump()
        except Exception as e:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message=f"Error reading image: {e}",
                tool_call_id=tool_call_id,
            ).model_dump()
