"""τ-agent-core tools: Grep tool.

Search files with regex patterns.

Reference: PHASE-2-SUBPHASE-3.md, "grep tool" section.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Callable, Literal, TypedDict

from tau_agent_core.tools.base import AgentToolResult, _WalkAborted


class GrepMatch(TypedDict):
    """One matching line: its file relative to the search root, 1-based line, and text."""

    path: str
    line: int
    text: str


class GrepResult(TypedDict):
    """What the grep tool returns to a nested caller."""

    matches: list[GrepMatch]
    files_searched: int


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
            value = await self.run(
                pattern,
                args.get("path", "."),
                files=args.get("files"),
                ignore_case=bool(args.get("ignore_case", False)),
                signal=signal,
            )
        except re.error as e:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message=f"Invalid regex pattern: {e}",
                tool_call_id=tool_call_id,
            ).model_dump()

        matches = value["matches"]
        if not matches:
            result_text = f"No matches found in {value['files_searched']} file(s)"
        else:
            result_text = "\n".join(f"{m['path']}:{m['line']}:{m['text']}" for m in matches)

        result = AgentToolResult(
            tool_name=self.name,
            tool_call_id=tool_call_id,
            content=[{"type": "text", "text": result_text}],
        )
        result_dict = result.model_dump()
        result_dict["details"] = {
            "matches": len(matches),
            "files_searched": value["files_searched"],
        }
        result_dict["value"] = value
        return result_dict

    async def run(
        self,
        pattern: str,
        path: str = ".",
        *,
        files: list[str] | None = None,
        ignore_case: bool = False,
        signal: Any = None,
    ) -> GrepResult:
        """Search for ``pattern`` and return the matches as data.

        Raises:
            re.error: ``pattern`` is not a valid regex.
            asyncio.CancelledError: ``signal`` aborted the walk.
        """
        compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        target_path = path
        if target_path:
            if not os.path.isabs(target_path):
                target_path = os.path.join(self.cwd, target_path)
            target_path = os.path.abspath(target_path)
        try:
            matches, files_searched = await asyncio.to_thread(
                self._collect, compiled, target_path, files, signal
            )
        except _WalkAborted:
            raise asyncio.CancelledError("Grep aborted") from None
        return GrepResult(matches=matches, files_searched=files_searched)

    def _collect(
        self,
        compiled: re.Pattern,
        target_path: str,
        files_list: list[str] | None,
        signal: Any,
    ) -> tuple[list[GrepMatch], int]:
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
            The matches and the number of files read.

        Raises:
            _WalkAborted: if ``signal`` reports an abort mid-walk.
        """
        files_searched = 0
        matches: list[GrepMatch] = []

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

                    match_lines = self._search_file(file_path, compiled, search_dir)
                    if match_lines is None:
                        continue
                    files_searched += 1
                    matches.extend(match_lines)

        return matches, files_searched

    @staticmethod
    def _search_file(
        file_path: str, pattern: re.Pattern, base_path: str | None = None
    ) -> list[GrepMatch] | None:
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
                results.append(GrepMatch(path=rel_path, line=i, text=line_content))

        return results
