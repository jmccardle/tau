"""τ-agent-core tools: Grep tool.

Search files with regex patterns.

Reference: PHASE-2-SUBPHASE-3.md, "grep tool" section.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Callable, Literal

from tau_agent_core.tools.base import AgentToolResult, _WalkAborted


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
    # Annotated rather than left to inference (B1/tau-004): unannotated,
    # `execution_mode = "parallel"` infers `str`, and `ToolDefinition`
    # declares it `Literal["sequential", "parallel"]`. `sdk._resolve_tools`
    # copies this value into a ToolDefinition, so without the annotation mypy
    # cannot check that copy — which is the blindness B1 exists to remove.
    execution_mode: Literal["sequential", "parallel"] = "parallel"

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

        # The whole search runs in a worker thread. It is `os.walk` plus blocking
        # reads with no await anywhere inside it, so on the TUI's event loop —
        # which is also the loop painting the screen (docs/PLAN-0.9.4.md §8) — a
        # grep over a large tree froze painting and input for its whole duration.
        try:
            matches, files_searched = await asyncio.to_thread(
                self._collect, compiled, target_path, files_list, signal
            )
        except _WalkAborted:
            raise asyncio.CancelledError("Grep aborted") from None

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

    def _collect(
        self,
        compiled: re.Pattern,
        target_path: str,
        files_list: list[str] | None,
        signal: Any,
    ) -> tuple[list[str], int]:
        """Walk and search, off the event loop. Returns ``(matches, files_searched)``.

        Runs entirely in a worker thread, so it must not touch the event loop.
        ``signal`` is polled directly instead: :class:`~tau_llm.abort.AbortSignal`
        guards its state with a ``threading.Lock``, so reading it from this thread
        is safe. The poll is per file rather than per directory, because one
        directory of large files is the case where the walk is slow.

        Args:
            compiled: The compiled search pattern.
            target_path: Absolute path to search, used as the relative-path base.
            files_list: Explicit files to search, overriding ``target_path``.
            signal: Optional ``AbortSignal``, polled once per file.

        Returns:
            The match lines (``path:line:text``) and the number of files read.

        Raises:
            _WalkAborted: if ``signal`` reports an abort mid-walk.
        """
        files_searched = 0
        matches: list[str] = []

        def check_abort() -> None:
            if signal is not None and signal.is_aborted():
                raise _WalkAborted()

        if files_list:
            # Search in specific files
            for file_path in files_list:
                check_abort()
                if not os.path.isabs(file_path):
                    file_path = os.path.join(self.cwd, file_path)
                file_path = os.path.abspath(file_path)

                if not os.path.isfile(file_path):
                    continue

                # An explicitly-named file counts as searched even if it turns out
                # to be unreadable — the caller named it, so reporting "0 files
                # searched" for a list of files would be the misleading answer.
                # That was the old behaviour here and it is kept.
                files_searched += 1
                matches.extend(self._search_file(file_path, compiled, target_path) or [])
        elif os.path.isfile(target_path):
            # Single file search
            check_abort()
            files_searched = 1
            matches = self._search_file(target_path, compiled) or []
        else:
            # Directory search - find all files
            search_dir = target_path if os.path.isdir(target_path) else self.cwd
            for root, _dirs, filenames in os.walk(search_dir):
                for fname in filenames:
                    check_abort()
                    if fname.startswith("."):
                        continue
                    file_path = os.path.join(root, fname)

                    # Binary files are skipped by _search_file's own decode
                    # failure. This used to be a separate pre-pass that opened the
                    # file, read it whole into memory, and threw the result away
                    # (`_ = f.read()`) purely to see whether it decoded — so every
                    # file in the tree was read TWICE, and every text file was
                    # held in memory once at full size for no result. The decode
                    # error that pre-pass was watching for is the same one
                    # _search_file already catches.
                    match_lines = self._search_file(file_path, compiled, search_dir)
                    if match_lines is None:
                        continue
                    files_searched += 1
                    matches.extend(match_lines)

        return matches, files_searched

    @staticmethod
    def _search_file(
        file_path: str, pattern: re.Pattern, base_path: str | None = None
    ) -> list[str] | None:
        """Search a single file for pattern matches.

        Returns the matching lines, or ``None`` if the file could not be read as
        UTF-8 text — a binary file, or one the process cannot open. ``None`` and
        ``[]`` are deliberately different answers: ``[]`` means "read it, no
        matches" and counts toward ``files_searched``, ``None`` means "not a file
        this tool can search" and does not.
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except (UnicodeDecodeError, PermissionError, OSError):
            return None

        rel_path = file_path
        if base_path and file_path.startswith(base_path):
            rel_path = os.path.relpath(file_path, base_path)

        results = []
        for i, line in enumerate(lines, 1):
            line_content = line.rstrip("\n\r")
            if pattern.search(line_content):
                results.append(f"{rel_path}:{i}:{line_content}")

        return results
