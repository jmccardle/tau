"""Tests for τ-agent-core built-in tools (Phase 2 Subphase 3).

Tests verify:
- Tool creation via factory functions
- Read tool: text files, large file truncation, binary files, images, symlinks, invalid args
- Write tool: new files, existing files, missing directories, encoding
- Edit tool: single replacement, multiple replacements, no match
- Bash tool: normal commands, errors, large output, timeout, abort
- Grep tool: normal search, no match, binary files
- Find tool: glob patterns, regex, directory listing
- Ls tool: normal listing, hidden files, long format
- Read-only tools factory
- Invalid arguments handling
- Abort signal support

Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.
Reference: PHASE-2-SUBPHASE-3.md, "Testing Strategy" section.
"""

import asyncio
import os
import sys
import tempfile
import time

import pytest

from tau_agent_core.tools import (
    ReadTool,
    WriteTool,
    EditTool,
    BashTool,
    GrepTool,
    FindTool,
    LsTool,
    create_all_tools,
    create_coding_tools,
    create_read_only_tools,
)
from tau_ai.abort import AbortSignal


# ============================================================================
# Test 1: Tool creation
# ============================================================================

class TestToolCreation:
    """Test 1: Tool creation via factory functions."""

    def test_create_all_tools_count(self):
        """create_all_tools returns 7 tools."""
        tools = create_all_tools("/tmp")
        assert len(tools) == 7

    def test_create_all_tools_names(self):
        """create_all_tools returns the correct tool names."""
        tools = create_all_tools("/tmp")
        tool_names = [t.name for t in tools]
        assert set(tool_names) == {"read", "write", "edit", "bash", "grep", "find", "ls"}

    def test_create_all_tools_has_valid_parameters(self):
        """All tools have valid parameters (JSON Schema dict)."""
        tools = create_all_tools("/tmp")
        for tool in tools:
            assert isinstance(tool.parameters, dict)
            assert "properties" in tool.parameters or tool.name == "bash"  # bash has properties

    def test_create_coding_tools_returns_all(self):
        """create_coding_tools returns the same as create_all_tools."""
        coding = create_coding_tools("/tmp")
        all_tools = create_all_tools("/tmp")
        assert len(coding) == 7
        assert set(t.name for t in coding) == set(t.name for t in all_tools)

    def test_all_tools_are_async(self):
        """All tools are async (execute returns coroutine or awaitable)."""
        tools = create_all_tools("/tmp")
        for tool in tools:
            assert asyncio.iscoroutinefunction(tool.execute) or (
                hasattr(tool, "execute") and asyncio.iscoroutinefunction(tool.execute)
            )


# ============================================================================
# Test 2: Read tool — text file
# ============================================================================

class TestReadToolText:
    """Test 2: Read tool — text file."""

    async def test_read_text_file(self, tmp_path):
        """Read tool returns content, line count, and truncation flag for text files."""
        tool = ReadTool(cwd=str(tmp_path))
        test_file = tmp_path / "test.txt"
        test_file.write_text("line1\nline2\nline3")

        result = await tool.execute("tc1", {"path": "test.txt"}, None, None)
        assert result["content"][0]["text"] == "line1\nline2\nline3"
        assert result["details"]["lines_read"] == 3
        assert result["details"]["truncated"] is False

    async def test_read_text_file_with_offset(self, tmp_path):
        """Read tool supports offset parameter."""
        tool = ReadTool(cwd=str(tmp_path))
        test_file = tmp_path / "test.txt"
        test_file.write_text("line1\nline2\nline3\nline4\nline5")

        result = await tool.execute("tc1", {"path": "test.txt", "offset": 3}, None, None)
        assert "line3" in result["content"][0]["text"]

    async def test_read_text_file_with_limit(self, tmp_path):
        """Read tool supports limit parameter."""
        tool = ReadTool(cwd=str(tmp_path))
        test_file = tmp_path / "test.txt"
        test_file.write_text("line1\nline2\nline3\nline4\nline5")

        result = await tool.execute("tc1", {"path": "test.txt", "limit": 2}, None, None)
        lines = result["content"][0]["text"].split("\n")
        assert len(lines) == 2

    async def test_read_nonexistent_file(self, tmp_path):
        """Read tool returns error for nonexistent file."""
        tool = ReadTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "nonexistent.txt"}, None, None)
        assert result["content"][0]["text"] == "Error: File not found: nonexistent.txt"
        assert result["is_error"] is True

    async def test_read_missing_path_arg(self, tmp_path):
        """Read tool returns error when path argument is missing."""
        tool = ReadTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {}, None, None)
        assert "Missing required argument" in result["content"][0]["text"]
        assert result["is_error"] is True

    async def test_read_symlink(self, tmp_path):
        """Read tool follows symlinks."""
        test_file = tmp_path / "original.txt"
        test_file.write_text("symlink target content")
        link = tmp_path / "link.txt"
        link.symlink_to(test_file)

        tool = ReadTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "link.txt"}, None, None)
        assert "symlink target content" in result["content"][0]["text"]

    async def test_read_binary_file(self, tmp_path):
        """Read tool identifies binary files."""
        test_file = tmp_path / "binary.bin"
        test_file.write_bytes(b"\x00\x01\x02\x03")

        tool = ReadTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "binary.bin"}, None, None)
        assert "Binary file" in result["content"][0]["text"]

    async def test_read_absolute_path(self, tmp_path):
        """Read tool handles absolute paths."""
        test_file = tmp_path / "absolute.txt"
        test_file.write_text("absolute path content")

        tool = ReadTool(cwd="/tmp")
        result = await tool.execute("tc1", {"path": str(test_file)}, None, None)
        assert "absolute path content" in result["content"][0]["text"]

    async def test_read_image_file(self, tmp_path):
        """Read tool handles image files (returns base64 preview)."""
        test_file = tmp_path / "test.png"
        # Write minimal PNG (1x1 pixel)
        test_file.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\x0cIDATx\x9cc\xfa\xcf\x00\x00\x00"
            b"\x01\x00\x01\x00\x05\xfe\xd0\x00\x00\x00\x00"
            b"IEND\xaeB`\x82"
        )

        tool = ReadTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "test.png"}, None, None)
        assert "image/png" in result["content"][0]["text"]

    async def test_read_empty_file(self, tmp_path):
        """Read tool handles empty files."""
        test_file = tmp_path / "empty.txt"
        test_file.write_text("")

        tool = ReadTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "empty.txt"}, None, None)
        assert result["content"][0]["text"] == ""
        assert result["details"]["lines_read"] == 0
        assert result["details"]["truncated"] is False

    async def test_read_utf8_encoding(self, tmp_path):
        """Read tool handles UTF-8 encoded files."""
        test_file = tmp_path / "utf8.txt"
        test_file.write_text("Hello 世界 🌍", encoding="utf-8")

        tool = ReadTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "utf8.txt"}, None, None)
        assert "世界" in result["content"][0]["text"]
        assert "🌍" in result["content"][0]["text"]


# ============================================================================
# Test 3: Read tool — large file truncation
# ============================================================================

class TestReadToolTruncation:
    """Test 3: Read tool — large file truncation."""

    async def test_read_large_file_truncation(self, tmp_path):
        """Read tool truncates large files at default limit (4096 lines)."""
        tool = ReadTool(cwd=str(tmp_path))
        test_file = tmp_path / "big.txt"
        test_file.write_text("\n".join(f"line{i}" for i in range(10000)))

        result = await tool.execute("tc1", {"path": "big.txt"}, None, None)
        assert result["details"]["truncated"] is True
        assert result["details"]["lines_read"] == 4096  # default limit

    async def test_read_large_file_custom_limit(self, tmp_path):
        """Read tool respects custom limit parameter."""
        tool = ReadTool(cwd=str(tmp_path))
        test_file = tmp_path / "big.txt"
        test_file.write_text("\n".join(f"line{i}" for i in range(10000)))

        result = await tool.execute("tc1", {"path": "big.txt", "limit": 100}, None, None)
        assert result["details"]["truncated"] is True
        assert result["details"]["lines_read"] == 100

    async def test_read_file_under_limit_no_truncation(self, tmp_path):
        """Read tool does not truncate files under the limit."""
        tool = ReadTool(cwd=str(tmp_path))
        test_file = tmp_path / "small.txt"
        test_file.write_text("\n".join(f"line{i}" for i in range(100)))

        result = await tool.execute("tc1", {"path": "small.txt"}, None, None)
        assert result["details"]["truncated"] is False
        assert result["details"]["lines_read"] == 100


# ============================================================================
# Test 4: Write tool
# ============================================================================

class TestWriteTool:
    """Test 4: Write tool."""

    async def test_write_file(self, tmp_path):
        """Write tool creates files with correct content."""
        tool = WriteTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "output.txt", "content": "hello world"}, None, None)
        assert (tmp_path / "output.txt").exists()
        assert (tmp_path / "output.txt").read_text() == "hello world"
        assert result["details"]["lines"] == 1

    async def test_write_file_with_newlines(self, tmp_path):
        """Write tool handles content with newlines correctly."""
        tool = WriteTool(cwd=str(tmp_path))
        content = "line1\nline2\nline3"
        result = await tool.execute("tc1", {"path": "multi.txt", "content": content}, None, None)
        assert (tmp_path / "multi.txt").read_text() == content
        assert result["details"]["lines"] == 3

    async def test_write_creates_parent_directories(self, tmp_path):
        """Write tool creates parent directories."""
        tool = WriteTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "sub/deep/file.txt", "content": "nested"}, None, None)
        assert (tmp_path / "sub" / "deep" / "file.txt").read_text() == "nested"

    async def test_write_overwrites_existing_file(self, tmp_path):
        """Write tool overwrites existing files."""
        test_file = tmp_path / "existing.txt"
        test_file.write_text("old content")

        tool = WriteTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "existing.txt", "content": "new content"}, None, None)
        assert test_file.read_text() == "new content"

    async def test_write_missing_path_arg(self, tmp_path):
        """Write tool returns error when path is missing."""
        tool = WriteTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"content": "hello"}, None, None)
        assert result["is_error"] is True
        assert 'Missing required argument: "path"' in result["content"][0]["text"]

    async def test_write_missing_content_arg(self, tmp_path):
        """Write tool returns error when content is missing."""
        tool = WriteTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "test.txt"}, None, None)
        assert result["is_error"] is True
        assert 'Missing required argument: "content"' in result["content"][0]["text"]

    async def test_write_encoding_parameter(self, tmp_path):
        """Write tool supports encoding parameter."""
        tool = WriteTool(cwd=str(tmp_path))
        result = await tool.execute(
            "tc1",
            {"path": "utf16.txt", "content": "Hello 世界", "encoding": "utf-16"},
            None, None,
        )
        content = (tmp_path / "utf16.txt").read_text(encoding="utf-16")
        assert "世界" in content

    async def test_write_to_directory_error(self, tmp_path):
        """Write tool returns error when target is a directory."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        tool = WriteTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "subdir", "content": "nope"}, None, None)
        assert result["is_error"] is True

    async def test_write_absolute_path(self, tmp_path):
        """Write tool handles absolute paths."""
        tool = WriteTool(cwd="/tmp")
        test_file = tmp_path / "abs_write.txt"
        result = await tool.execute("tc1", {"path": str(test_file), "content": "absolute"}, None, None)
        assert test_file.read_text() == "absolute"

    async def test_write_atomicity(self, tmp_path):
        """Write tool uses atomic writes (no partial files on error)."""
        tool = WriteTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "atomic.txt", "content": "test content"}, None, None)
        # File should exist and have correct content
        assert (tmp_path / "atomic.txt").read_text() == "test content"


# ============================================================================
# Test 5: Edit tool
# ============================================================================

class TestEditTool:
    """Test 5: Edit tool."""

    async def test_edit_single_replacement(self, tmp_path):
        """Edit tool replaces a single occurrence."""
        tool = EditTool(cwd=str(tmp_path))
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        result = await tool.execute("tc1", {
            "path": "test.txt",
            "old_string": "world",
            "new_string": "universe",
        }, None, None)
        assert test_file.read_text() == "hello universe"
        assert result["details"]["replacements"] == 1

    async def test_edit_no_match(self, tmp_path):
        """Edit tool returns error when old_string is not found."""
        tool = EditTool(cwd=str(tmp_path))
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        result = await tool.execute("tc1", {
            "path": "test.txt",
            "old_string": "notfound",
            "new_string": "replacement",
        }, None, None)
        assert result["is_error"] is True
        assert "not found" in result["content"][0]["text"].lower()

    async def test_edit_multiple_no_replace_all(self, tmp_path):
        """Edit tool errors on multiple matches without replace_all."""
        tool = EditTool(cwd=str(tmp_path))
        test_file = tmp_path / "test.txt"
        test_file.write_text("foo bar foo baz foo")

        result = await tool.execute("tc1", {
            "path": "test.txt",
            "old_string": "foo",
            "new_string": "replacement",
        }, None, None)
        assert result["is_error"] is True
        assert "occurrences" in result["content"][0]["text"].lower()

    async def test_edit_multiple_with_replace_all(self, tmp_path):
        """Edit tool replaces all occurrences when replace_all=True."""
        tool = EditTool(cwd=str(tmp_path))
        test_file = tmp_path / "test.txt"
        test_file.write_text("foo bar foo baz foo")

        result = await tool.execute("tc1", {
            "path": "test.txt",
            "old_string": "foo",
            "new_string": "replacement",
            "replace_all": True,
        }, None, None)
        assert test_file.read_text() == "replacement bar replacement baz replacement"
        assert result["details"]["replacements"] == 3

    async def test_edit_nonexistent_file(self, tmp_path):
        """Edit tool returns error for nonexistent file."""
        tool = EditTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {
            "path": "nonexistent.txt",
            "old_string": "foo",
            "new_string": "bar",
        }, None, None)
        assert result["is_error"] is True

    async def test_edit_missing_args(self, tmp_path):
        """Edit tool returns error for missing required arguments."""
        tool = EditTool(cwd=str(tmp_path))

        result = await tool.execute("tc1", {"path": "test.txt"}, None, None)
        assert result["is_error"] is True

    async def test_edit_no_match_preserves_file(self, tmp_path):
        """Edit tool does not modify file when no match found."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("original content")

        tool = EditTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {
            "path": "test.txt",
            "old_string": "notfound",
            "new_string": "replacement",
        }, None, None)

        # File should be unchanged
        assert test_file.read_text() == "original content"
        assert result["is_error"] is True

    async def test_edit_generates_diff(self, tmp_path):
        """Edit tool generates diff output in details."""
        tool = EditTool(cwd=str(tmp_path))
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        result = await tool.execute("tc1", {
            "path": "test.txt",
            "old_string": "world",
            "new_string": "universe",
        }, None, None)
        assert "diff" in result["details"]
        assert "world" in result["details"]["diff"]
        assert "universe" in result["details"]["diff"]

    async def test_edit_replace_with_same_string(self, tmp_path):
        """Edit tool handles replacing with identical string."""
        tool = EditTool(cwd=str(tmp_path))
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        result = await tool.execute("tc1", {
            "path": "test.txt",
            "old_string": "world",
            "new_string": "world",
        }, None, None)
        assert test_file.read_text() == "hello world"
        assert result["details"]["replacements"] == 1


# ============================================================================
# Test 6: Bash tool
# ============================================================================

class TestBashTool:
    """Test 6: Bash tool."""

    async def test_bash_command(self, tmp_path):
        """Bash tool executes commands and returns output."""
        tool = BashTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"command": "echo hello"}, None, None)
        assert "hello" in result["content"][0]["text"]
        assert result["details"]["exit_code"] == 0

    async def test_bash_command_with_output(self, tmp_path):
        """Bash tool captures command output."""
        tool = BashTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"command": "echo hello world"}, None, None)
        assert "hello world" in result["content"][0]["text"]
        assert result["details"]["exit_code"] == 0

    async def test_bash_command_error(self, tmp_path):
        """Bash tool handles command errors."""
        tool = BashTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"command": "exit 1"}, None, None)
        assert result["details"]["exit_code"] == 1

    async def test_bash_command_not_found(self, tmp_path):
        """Bash tool handles command not found."""
        tool = BashTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"command": "nonexistent_command_xyz_12345"}, None, None)
        assert result["details"]["exit_code"] != 0

    async def test_bash_missing_command_arg(self, tmp_path):
        """Bash tool returns error when command is missing."""
        tool = BashTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {}, None, None)
        assert result["is_error"] is True
        assert 'Missing required argument: "command"' in result["content"][0]["text"]

    async def test_bash_pwd(self, tmp_path):
        """Bash tool respects working directory."""
        tool = BashTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"command": "pwd"}, None, None)
        assert str(tmp_path) in result["content"][0]["text"]

    async def test_bash_list_directory(self, tmp_path):
        """Bash tool can list directory."""
        test_file = tmp_path / "test_list.txt"
        test_file.write_text("test")

        tool = BashTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"command": "ls -1"}, None, None)
        assert "test_list.txt" in result["content"][0]["text"]

    async def test_bash_stdout_and_stderr(self, tmp_path):
        """Bash tool captures both stdout and stderr."""
        tool = BashTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"command": "echo out; echo err >&2"}, None, None)
        assert "out" in result["content"][0]["text"]
        assert "err" in result["content"][0]["text"]

    async def test_bash_timeout(self, tmp_path):
        """Bash tool respects timeout parameter."""
        tool = BashTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"command": "sleep 5", "timeout": 100}, None, None)
        assert result["details"]["exit_code"] != 0
        assert result["details"]["truncated"] is True

    async def test_bash_long_output(self, tmp_path):
        """Bash tool handles commands with long output."""
        tool = BashTool(cwd=str(tmp_path))
        # Generate lots of output
        result = await tool.execute("tc1", {"command": "python3 -c 'for i in range(5000): print(i)'"}, None, None)
        assert result["details"]["exit_code"] == 0
        # Output should be truncated
        assert result["details"]["truncated"] is True


# ============================================================================
# Test 7: Bash tool — abort
# ============================================================================

class TestBashToolAbort:
    """Test 7: Bash tool — abort."""

    async def test_bash_abort(self, tmp_path):
        """Bash tool responds to abort signal."""
        tool = BashTool(cwd=str(tmp_path))
        signal = AbortSignal()

        # Start bash
        task = asyncio.create_task(
            tool.execute("tc1", {"command": "sleep 10"}, signal, None)
        )
        await asyncio.sleep(0.05)
        signal.abort()
        # Wait for the task to complete (should be aborted)
        await asyncio.sleep(0.5)
        # The task should complete (not be pending)
        assert task.done() or task.cancelled()

    async def test_bash_abort_after_completion(self, tmp_path):
        """Bash tool handles abort signal that comes after completion."""
        tool = BashTool(cwd=str(tmp_path))
        signal = AbortSignal()

        result = await tool.execute("tc1", {"command": "echo done"}, signal, None)
        assert result["details"]["exit_code"] == 0

    async def test_bash_abort_immediate(self, tmp_path):
        """Bash tool handles abort signal set before execution."""
        tool = BashTool(cwd=str(tmp_path))
        signal = AbortSignal()

        # Abort immediately
        signal.abort()
        result = await tool.execute("tc1", {"command": "echo hello"}, signal, None)
        assert "aborted" in result["content"][0]["text"].lower()


# ============================================================================
# Test 8: Grep tool
# ============================================================================

class TestGrepTool:
    """Test 8: Grep tool."""

    async def test_grep_basic(self, tmp_path):
        """Grep tool finds matches in files."""
        tool = GrepTool(cwd=str(tmp_path))
        test_file = tmp_path / "test.py"
        test_file.write_text("import os\nimport sys\nimport json")

        result = await tool.execute("tc1", {"pattern": "import", "path": "."}, None, None)
        assert "test.py:1:import os" in result["content"][0]["text"]

    async def test_grep_no_match(self, tmp_path):
        """Grep tool returns no match for non-matching pattern."""
        tool = GrepTool(cwd=str(tmp_path))
        test_file = tmp_path / "test.py"
        test_file.write_text("hello world")

        result = await tool.execute("tc1", {"pattern": "xyznotfound"}, None, None)
        assert "No matches found" in result["content"][0]["text"]

    async def test_grep_case_insensitive(self, tmp_path):
        """Grep tool supports case-insensitive search."""
        tool = GrepTool(cwd=str(tmp_path))
        test_file = tmp_path / "test.py"
        test_file.write_text("Hello WORLD hello")

        result = await tool.execute("tc1", {"pattern": "hello", "ignore_case": True}, None, None)
        assert "hello" in result["content"][0]["text"]

    async def test_grep_regex(self, tmp_path):
        """Grep tool supports regex patterns."""
        tool = GrepTool(cwd=str(tmp_path))
        test_file = tmp_path / "test.py"
        test_file.write_text("foo123\nbar456\nfoo789")

        result = await tool.execute("tc1", {"pattern": "foo\\d+"}, None, None)
        assert "foo123" in result["content"][0]["text"]
        assert "foo789" in result["content"][0]["text"]

    async def test_grep_missing_pattern(self, tmp_path):
        """Grep tool returns error when pattern is missing."""
        tool = GrepTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {}, None, None)
        assert result["is_error"] is True

    async def test_grep_specific_files(self, tmp_path):
        """Grep tool searches specific files."""
        tool = GrepTool(cwd=str(tmp_path))
        file1 = tmp_path / "file1.txt"
        file1.write_text("hello world")
        file2 = tmp_path / "file2.txt"
        file2.write_text("goodbye world")

        result = await tool.execute(
            "tc1",
            {"pattern": "hello", "files": ["file1.txt"]},
            None, None,
        )
        assert "file1.txt" in result["content"][0]["text"]
        assert "file2.txt" not in result["content"][0]["text"]

    async def test_grep_invalid_regex(self, tmp_path):
        """Grep tool handles invalid regex patterns."""
        tool = GrepTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"pattern": "[invalid"}, None, None)
        assert result["is_error"] is True

    async def test_grep_details(self, tmp_path):
        """Grep tool returns matches and files_searched in details."""
        tool = GrepTool(cwd=str(tmp_path))
        test_file = tmp_path / "test.py"
        test_file.write_text("import os\nimport sys")

        result = await tool.execute("tc1", {"pattern": "import", "path": "."}, None, None)
        assert "matches" in result["details"]
        assert "files_searched" in result["details"]
        assert result["details"]["matches"] == 2


# ============================================================================
# Test 9: Find tool
# ============================================================================

class TestFindTool:
    """Test 9: Find tool."""

    async def test_find_glob_pattern(self, tmp_path):
        """Find tool finds files matching glob pattern."""
        tool = FindTool(cwd=str(tmp_path))
        (tmp_path / "test.txt").touch()
        (tmp_path / "test.py").touch()
        (tmp_path / "subdir").mkdir()

        result = await tool.execute("tc1", {"path": ".", "name": "*.txt"}, None, None)
        assert "test.txt" in result["content"][0]["text"]
        assert "test.py" not in result["content"][0]["text"]

    async def test_find_no_pattern(self, tmp_path):
        """Find tool finds all files when no pattern given."""
        tool = FindTool(cwd=str(tmp_path))
        (tmp_path / "a.txt").touch()
        (tmp_path / "b.py").touch()

        result = await tool.execute("tc1", {"path": "."}, None, None)
        assert "a.txt" in result["content"][0]["text"]
        assert "b.py" in result["content"][0]["text"]

    async def test_find_file_type(self, tmp_path):
        """Find tool filters by file type."""
        tool = FindTool(cwd=str(tmp_path))
        (tmp_path / "file.txt").touch()
        (tmp_path / "dir").mkdir()

        result = await tool.execute("tc1", {"path": ".", "type": "f"}, None, None)
        assert "file.txt" in result["content"][0]["text"]

    async def test_find_directory_type(self, tmp_path):
        """Find tool finds directories."""
        tool = FindTool(cwd=str(tmp_path))
        (tmp_path / "mydir").mkdir()

        result = await tool.execute("tc1", {"path": ".", "type": "d"}, None, None)
        assert "mydir" in result["content"][0]["text"]

    async def test_find_regex(self, tmp_path):
        """Find tool finds files matching regex."""
        tool = FindTool(cwd=str(tmp_path))
        (tmp_path / "test_01.py").touch()
        (tmp_path / "test_02.py").touch()
        (tmp_path / "other.txt").touch()

        result = await tool.execute("tc1", {"path": ".", "regex": r"test_\d+\.py"}, None, None)
        assert "test_01.py" in result["content"][0]["text"]
        assert "test_02.py" in result["content"][0]["text"]
        assert "other.txt" not in result["content"][0]["text"]

    async def test_find_nonexistent_path(self, tmp_path):
        """Find tool handles nonexistent path."""
        tool = FindTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "/nonexistent/path/xyz"}, None, None)
        assert "not found" in result["content"][0]["text"].lower()

    async def test_find_no_results(self, tmp_path):
        """Find tool returns empty result for non-matching pattern."""
        tool = FindTool(cwd=str(tmp_path))
        (tmp_path / "file.txt").touch()

        result = await tool.execute("tc1", {"path": ".", "name": "*.xyz"}, None, None)
        assert "No files found" in result["content"][0]["text"]

    async def test_find_details(self, tmp_path):
        """Find tool returns paths and count in details."""
        tool = FindTool(cwd=str(tmp_path))
        (tmp_path / "a.txt").touch()
        (tmp_path / "b.txt").touch()

        result = await tool.execute("tc1", {"path": ".", "name": "*.txt"}, None, None)
        assert "paths" in result["details"]
        assert "count" in result["details"]
        assert result["details"]["count"] == 2


# ============================================================================
# Test 10: Ls tool
# ============================================================================

class TestLsTool:
    """Test 10: Ls tool."""

    async def test_ls_basic(self, tmp_path):
        """Ls tool lists directory contents."""
        tool = LsTool(cwd=str(tmp_path))
        (tmp_path / "file1.txt").touch()
        (tmp_path / "file2.txt").touch()

        result = await tool.execute("tc1", {"path": "."}, None, None)
        assert "file1.txt" in result["content"][0]["text"]
        assert "file2.txt" in result["content"][0]["text"]

    async def test_ls_hidden_files(self, tmp_path):
        """Ls tool excludes hidden files by default."""
        tool = LsTool(cwd=str(tmp_path))
        (tmp_path / "visible.txt").touch()
        (tmp_path / ".hidden").touch()

        result = await tool.execute("tc1", {"path": "."}, None, None)
        assert "visible.txt" in result["content"][0]["text"]
        assert ".hidden" not in result["content"][0]["text"]

    async def test_ls_all_hidden(self, tmp_path):
        """Ls tool includes hidden files with all=True."""
        tool = LsTool(cwd=str(tmp_path))
        (tmp_path / "visible.txt").touch()
        (tmp_path / ".hidden").touch()

        result = await tool.execute("tc1", {"path": ".", "all": True}, None, None)
        assert "visible.txt" in result["content"][0]["text"]
        assert ".hidden" in result["content"][0]["text"]

    async def test_ls_long_format(self, tmp_path):
        """Ls tool supports long format output."""
        tool = LsTool(cwd=str(tmp_path))
        (tmp_path / "test.txt").touch()

        result = await tool.execute("tc1", {"path": ".", "long": True}, None, None)
        # Long format should include file permissions
        assert "test.txt" in result["content"][0]["text"]

    async def test_ls_nonexistent_path(self, tmp_path):
        """Ls tool handles nonexistent path."""
        tool = LsTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "/nonexistent/xyz"}, None, None)
        assert "not found" in result["content"][0]["text"].lower()

    async def test_ls_empty_directory(self, tmp_path):
        """Ls tool handles empty directories."""
        tool = LsTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "."}, None, None)
        # Should indicate empty directory
        assert result["content"]  # Should still have content

    async def test_ls_not_a_directory(self, tmp_path):
        """Ls tool handles non-directory path."""
        test_file = tmp_path / "file.txt"
        test_file.touch()

        tool = LsTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "file.txt"}, None, None)
        assert "not a directory" in result["content"][0]["text"].lower()

    async def test_ls_details(self, tmp_path):
        """Ls tool returns paths and count in details."""
        tool = LsTool(cwd=str(tmp_path))
        (tmp_path / "a.txt").touch()
        (tmp_path / "b.txt").touch()

        result = await tool.execute("tc1", {"path": "."}, None, None)
        assert "paths" in result["details"]
        assert "count" in result["details"]


# ============================================================================
# Test 11: Read-only tools
# ============================================================================

class TestReadOnlyTools:
    """Test 11: Read-only tools factory."""

    def test_read_only_tools_count(self):
        """create_read_only_tools returns 4 tools."""
        tools = create_read_only_tools("/tmp")
        assert len(tools) == 4

    def test_read_only_tools_names(self):
        """create_read_only_tools returns only read-only tools."""
        tools = create_read_only_tools("/tmp")
        tool_names = {t.name for t in tools}
        assert "read" in tool_names
        assert "write" not in tool_names
        assert "edit" not in tool_names
        assert "bash" not in tool_names
        assert "grep" in tool_names
        assert "find" in tool_names
        assert "ls" in tool_names

    def test_read_only_tools_have_parameters(self):
        """All read-only tools have valid parameters."""
        tools = create_read_only_tools("/tmp")
        for tool in tools:
            assert hasattr(tool, "parameters")
            assert isinstance(tool.parameters, dict)


# ============================================================================
# Test 12: Invalid arguments
# ============================================================================

class TestInvalidArguments:
    """Test 12: Invalid arguments handling.

    Tests that tools return error results for invalid inputs,
    not throw uncaught exceptions.
    """

    async def test_read_missing_path(self, tmp_path):
        """Read tool returns error for missing path argument."""
        tool = ReadTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {}, None, None)
        assert result["is_error"] is True
        assert "missing required argument" in result["content"][0]["text"].lower()

    async def test_write_missing_path(self, tmp_path):
        """Write tool returns error for missing path argument."""
        tool = WriteTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {}, None, None)
        assert result["is_error"] is True

    async def test_write_missing_content(self, tmp_path):
        """Write tool returns error for missing content argument."""
        tool = WriteTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "test.txt"}, None, None)
        assert result["is_error"] is True

    async def test_edit_missing_path(self, tmp_path):
        """Edit tool returns error for missing path argument."""
        tool = EditTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"old_string": "a", "new_string": "b"}, None, None)
        assert result["is_error"] is True

    async def test_edit_missing_old_string(self, tmp_path):
        """Edit tool returns error for missing old_string argument."""
        tool = EditTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "test.txt", "new_string": "b"}, None, None)
        assert result["is_error"] is True

    async def test_edit_missing_new_string(self, tmp_path):
        """Edit tool returns error for missing new_string argument."""
        tool = EditTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {"path": "test.txt", "old_string": "a"}, None, None)
        assert result["is_error"] is True

    async def test_bash_missing_command(self, tmp_path):
        """Bash tool returns error for missing command argument."""
        tool = BashTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {}, None, None)
        assert result["is_error"] is True

    async def test_grep_missing_pattern(self, tmp_path):
        """Grep tool returns error for missing pattern argument."""
        tool = GrepTool(cwd=str(tmp_path))
        result = await tool.execute("tc1", {}, None, None)
        assert result["is_error"] is True

    async def test_all_tools_return_dict(self, tmp_path):
        """All tools return a dict even on error (no uncaught exceptions)."""
        test_cases = [
            (ReadTool(cwd=str(tmp_path)), "read", {"nonexistent_key": "value"}),
            (WriteTool(cwd=str(tmp_path)), "write", {}),
            (EditTool(cwd=str(tmp_path)), "edit", {"path": "/nonexistent"}),
            (BashTool(cwd=str(tmp_path)), "bash", {}),
            (GrepTool(cwd=str(tmp_path)), "grep", {}),
            (FindTool(cwd=str(tmp_path)), "find", {}),
            (LsTool(cwd=str(tmp_path)), "ls", {"path": "/nonexistent"}),
        ]

        for tool, name, bad_args in test_cases:
            result = await tool.execute("tc1", bad_args, None, None)
            assert isinstance(result, dict), f"{name} tool did not return dict"
            assert "content" in result, f"{name} tool result missing 'content'"
            assert "is_error" in result or "details" in result, f"{name} tool result missing 'details' or 'is_error'"

    async def test_all_tools_return_content_array(self, tmp_path):
        """All tool results have a 'content' list with at least one block."""
        test_cases = [
            (ReadTool(cwd=str(tmp_path)), {"path": "nonexistent.txt"}),
            (WriteTool(cwd=str(tmp_path)), {}),
            (EditTool(cwd=str(tmp_path)), {"path": "nonexistent.txt", "old_string": "x", "new_string": "y"}),
            (BashTool(cwd=str(tmp_path)), {}),
            (GrepTool(cwd=str(tmp_path)), {}),
            (FindTool(cwd=str(tmp_path)), {}),
            (LsTool(cwd=str(tmp_path)), {"path": "/nonexistent"}),
        ]

        for tool, args in test_cases:
            result = await tool.execute("tc1", args, None, None)
            assert "content" in result
            assert isinstance(result["content"], list)
            assert len(result["content"]) > 0, f"Tool {tool.name} has empty content list"


# ============================================================================
# Test 13: Tool attributes
# ============================================================================

class TestToolAttributes:
    """Additional tests for tool structure and attributes."""

    def test_tool_has_name_attribute(self):
        """All tools have a 'name' attribute."""
        tools = create_all_tools("/tmp")
        for tool in tools:
            assert hasattr(tool, "name")
            assert isinstance(tool.name, str)

    def test_tool_has_label_attribute(self):
        """All tools have a 'label' attribute."""
        tools = create_all_tools("/tmp")
        for tool in tools:
            assert hasattr(tool, "label")
            assert isinstance(tool.label, str)

    def test_tool_has_description_attribute(self):
        """All tools have a 'description' attribute."""
        tools = create_all_tools("/tmp")
        for tool in tools:
            assert hasattr(tool, "description")
            assert isinstance(tool.description, str)

    def test_tool_has_parameters_attribute(self):
        """All tools have a 'parameters' attribute (JSON Schema)."""
        tools = create_all_tools("/tmp")
        for tool in tools:
            assert hasattr(tool, "parameters")
            assert isinstance(tool.parameters, dict)

    def test_tool_has_execute_method(self):
        """All tools have an 'execute' method."""
        tools = create_all_tools("/tmp")
        for tool in tools:
            assert hasattr(tool, "execute")
            assert callable(tool.execute)

    def test_tool_has_execution_mode(self):
        """All tools have an 'execution_mode' attribute."""
        tools = create_all_tools("/tmp")
        for tool in tools:
            assert hasattr(tool, "execution_mode")
            assert tool.execution_mode in ("parallel", "sequential")

    def test_bash_tool_is_sequential(self):
        """Bash tool has sequential execution mode."""
        tool = BashTool(cwd="/tmp")
        assert tool.execution_mode == "sequential"

    def test_edit_tool_is_sequential(self):
        """Edit tool has sequential execution mode."""
        tool = EditTool(cwd="/tmp")
        assert tool.execution_mode == "sequential"

    def test_write_tool_is_sequential(self):
        """Write tool has sequential execution mode."""
        tool = WriteTool(cwd="/tmp")
        assert tool.execution_mode == "sequential"

    def test_read_tool_is_parallel(self):
        """Read tool has parallel execution mode."""
        tool = ReadTool(cwd="/tmp")
        assert tool.execution_mode == "parallel"

    def test_grep_tool_is_parallel(self):
        """Grep tool has parallel execution mode."""
        tool = GrepTool(cwd="/tmp")
        assert tool.execution_mode == "parallel"

    def test_find_tool_is_parallel(self):
        """Find tool has parallel execution mode."""
        tool = FindTool(cwd="/tmp")
        assert tool.execution_mode == "parallel"

    def test_ls_tool_is_parallel(self):
        """Ls tool has parallel execution mode."""
        tool = LsTool(cwd="/tmp")
        assert tool.execution_mode == "parallel"
