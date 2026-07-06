# Phase 2 Subphase 3 — Built-in Tools

> **Topic**: Implement the core built-in tool suite that ships with τ-agent-core.

## Scope

This subphase implements the built-in tools that are always available:
1. `read` — Read files (with truncation, image support)
2. `write` — Write files (atomic writes, encoding handling)
3. `edit` — Search/replace in files (with diff rendering)
4. `bash` — Execute shell commands (with output streaming, temp files, timeout)
5. `grep` — Search files with regex
6. `find` — Find files by glob or regex
7. `ls` — List directory contents
8. `truncate` — Line/byte truncation utilities

These tools are registered automatically by `create_all_tools(cwd)`.

## Reference

- `SUBPHASE-0.0.md` lines 160-180: tool definition contract
- `docs/tau-agent-core.md` lines 450-550: tool design
- `docs/IMPLEMENTATION-PLAN.md` lines 180-250: tool specs
- pi's `tools/` directory (reference for each tool's behavior)

## Implementation Outline

### `tau_agent_core/tools/__init__.py`

```python
from .read import ReadTool
from .write import WriteTool
from .edit import EditTool
from .bash import BashTool
from .grep import GrepTool
from .find import FindTool
from .ls import LsTool

def create_all_tools(cwd: str) -> list[AgentTool]:
    """Create all built-in tools for the given working directory."""
    return [
        ReadTool(cwd=cwd),
        WriteTool(cwd=cwd),
        EditTool(cwd=cwd),
        BashTool(cwd=cwd),
        GrepTool(cwd=cwd),
        FindTool(cwd=cwd),
        LsTool(cwd=cwd),
    ]

def create_coding_tools(cwd: str) -> list[AgentTool]:
    """Create tools suitable for coding (includes all)."""
    return create_all_tools(cwd)

def create_read_only_tools(cwd: str) -> list[AgentTool]:
    """Create read-only tools (no write, edit, or bash)."""
    return [
        ReadTool(cwd=cwd),
        GrepTool(cwd=cwd),
        FindTool(cwd=cwd),
        LsTool(cwd=cwd),
    ]
```

### Common Tool Structure

Each tool implements:

```python
class SomeTool:
    name = "some_tool"
    label = "Some Tool"
    description = "..."
    parameters = {...}  # JSON Schema
    execution_mode = "parallel"  # or "sequential"

    async def execute(
        self,
        tool_call_id: str,
        args: dict,
        signal: AbortSignal | None,
        on_update: Callable | None,
    ) -> dict:  # {content, details, terminate?}
        ...
```

### Tool-Specific Details

#### read tool
- Parameters: `path` (str, required), `offset` (int, optional), `limit` (int, optional)
- Behavior: Reads file content, truncates if too large (default 4096 lines)
- Handles binary files: returns "Binary file" message
- Handles images: returns image data as base64 with mime type
- Handles symlinks: follows them (like cat)
- Returns: `{content: [{text: "..."}], details: {lines_read, truncated, path}}`

#### write tool
- Parameters: `path` (str, required), `content` (str, required), `encoding` (str, optional, default "utf-8")
- Behavior: Writes content to file, creating parent directories
- Atomic writes: writes to temp file, then renames
- Handles encoding errors: uses "replace" error handler by default
- Returns: `{content: [{text: f"Wrote {lines} lines to {path}"}], details: {path, lines, bytes}}`

#### edit tool
- Parameters: `path` (str, required), `old_string` (str, required), `new_string` (str, required), `replace_all` (bool, optional, default False)
- Behavior: Replaces old_string with new_string in file
- Validates that old_string exists exactly once (or use replace_all=True)
- Returns diff-like output: `{content: [{text: "Replaced X occurrences"}], details: {path, replacements, diff}}`

#### bash tool
- Parameters: `command` (str, required), `timeout` (int, optional, default 30000ms)
- Behavior: Executes command via subprocess, streams output in real-time
- Handles large output: uses temp files for output, streams in chunks
- Handles errors: returns stderr as error content
- Returns: `{content: [{text: "..."}], details: {exit_code, truncated, bytes_written}}`
- Abort-aware: checks signal during execution

#### grep tool
- Parameters: `pattern` (str, required), `path` (str, optional, default "."), `files` (list[str], optional), `ignore_case` (bool, optional)
- Behavior: Searches for pattern in files
- Returns: `{content: [{text: "file.py:10:matched line\n..."}], details: {matches, files_searched}}`

#### find tool
- Parameters: `path` (str, optional, default "."), `name` (str, optional, glob pattern), `type` (str, optional, "f"|"d"|"l"), `regex` (str, optional)
- Behavior: Finds files matching criteria
- Returns: `{content: [{text: "file1\nfile2\n..."}], details: {paths, count}}`

#### ls tool
- Parameters: `path` (str, optional, default "."), `all` (bool, optional, default False), `long` (bool, optional, default False)
- Behavior: Lists directory contents
- Returns: `{content: [{text: "file1\nfile2\n..."}], details: {paths, count}}`

## Done Criteria

- `create_all_tools(cwd)` returns a list of 7 tools
- Each tool has valid `parameters` (JSON Schema)
- Each tool's `execute()` method works correctly for valid inputs
- Each tool handles invalid inputs gracefully (returns error content)
- `read` handles: text files, binary files, images, symlinks, large files
- `write` handles: new files, existing files, missing directories, encoding
- `edit` handles: single replacement, multiple replacements, no match
- `bash` handles: normal commands, errors, large output, timeout, abort
- `grep` handles: normal search, no match, binary files
- `find` handles: glob patterns, regex, directory listing
- `ls` handles: normal listing, hidden files, long format
- All tools are async
- All tools support abort signal
- All tools have proper error handling (no uncaught exceptions)

## Testing Strategy

### Test 1: Tool creation

```python
async def test_create_all_tools():
    tools = create_all_tools("/tmp")
    assert len(tools) == 7
    tool_names = [t.name for t in tools]
    assert set(tool_names) == {"read", "write", "edit", "bash", "grep", "find", "ls"}
```

### Test 2: Read tool — text file

```python
async def test_read_text_file(tmp_path):
    tool = ReadTool(cwd=str(tmp_path))
    test_file = tmp_path / "test.txt"
    test_file.write_text("line1\nline2\nline3")

    result = await tool.execute("tc1", {"path": "test.txt"}, None, None)
    assert result["content"][0]["text"] == "line1\nline2\nline3"
    assert result["details"]["lines_read"] == 3
    assert result["details"]["truncated"] is False
```

### Test 3: Read tool — large file truncation

```python
async def test_read_large_file_truncation(tmp_path):
    tool = ReadTool(cwd=str(tmp_path))
    test_file = tmp_path / "big.txt"
    # Write 10000 lines
    test_file.write_text("\n".join(f"line{i}" for i in range(10000)))

    result = await tool.execute("tc1", {"path": "big.txt"}, None, None)
    assert result["details"]["truncated"] is True
    assert result["details"]["lines_read"] == 4096  # default limit
```

### Test 4: Write tool

```python
async def test_write_file(tmp_path):
    tool = WriteTool(cwd=str(tmp_path))
    result = await tool.execute("tc1", {"path": "output.txt", "content": "hello world"}, None, None)
    assert (tmp_path / "output.txt").exists()
    assert (tmp_path / "output.txt").read_text() == "hello world"
    assert result["details"]["lines"] == 1
```

### Test 5: Edit tool

```python
async def test_edit_file(tmp_path):
    tool = EditTool(cwd=str(tmp_path))
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world")

    result = await tool.execute("tc1", {
        "path": "test.txt",
        "old_string": "world",
        "new_string": "universe",
    }, None, None)
    assert test_file.read_text() == "hello universe"
```

### Test 6: Bash tool

```python
async def test_bash_command(tmp_path):
    tool = BashTool(cwd=str(tmp_path))
    result = await tool.execute("tc1", {"command": "echo hello"}, None, None)
    assert "hello" in result["content"][0]["text"]
    assert result["details"]["exit_code"] == 0
```

### Test 7: Bash tool — error

```python
async def test_bash_error(tmp_path):
    tool = BashTool(cwd=str(tmp_path))
    result = await tool.execute("tc1", {"command": "exit 1"}, None, None)
    assert result["details"]["exit_code"] == 1
```

### Test 8: Bash tool — abort

```python
async def test_bash_abort(tmp_path):
    tool = BashTool(cwd=str(tmp_path))
    signal = AbortSignal()
    # Start bash
    task = asyncio.create_task(
        tool.execute("tc1", {"command": "sleep 10"}, signal, None)
    )
    await asyncio.sleep(0.05)
    signal.abort()
    await asyncio.sleep(0.1)
    # Task should have been cancelled or returned
```

### Test 9: Grep tool

```python
async def test_grep(tmp_path):
    tool = GrepTool(cwd=str(tmp_path))
    test_file = tmp_path / "test.py"
    test_file.write_text("import os\nimport sys\nimport json")

    result = await tool.execute("tc1", {"pattern": "import", "path": "."}, None, None)
    assert "test.py:1:import os" in result["content"][0]["text"]
```

### Test 10: Find tool

```python
async def test_find(tmp_path):
    tool = FindTool(cwd=str(tmp_path))
    (tmp_path / "test.txt").touch()
    (tmp_path / "test.py").touch()
    (tmp_path / "subdir").mkdir()

    result = await tool.execute("tc1", {"path": ".", "name": "*.txt"}, None, None)
    assert "test.txt" in result["content"][0]["text"]
```

### Test 11: ls tool

```python
async def test_ls(tmp_path):
    tool = LsTool(cwd=str(tmp_path))
    (tmp_path / "file1.txt").touch()
    (tmp_path / "file2.txt").touch()

    result = await tool.execute("tc1", {"path": "."}, None, None)
    assert "file1.txt" in result["content"][0]["text"]
    assert "file2.txt" in result["content"][0]["text"]
```

### Test 12: Read-only tools

```python
async def test_read_only_tools():
    tools = create_read_only_tools("/tmp")
    tool_names = {t.name for t in tools}
    assert "read" in tool_names
    assert "write" not in tool_names
    assert "edit" not in tool_names
    assert "bash" not in tool_names
```

### Test 13: Invalid arguments

```python
async def test_invalid_tool_arguments():
    tool = ReadTool(cwd="/tmp")
    with pytest.raises(ValueError):
        await tool.execute("tc1", {"nonexistent": "path"}, None, None)
```

## Success Signal

All 13 test categories pass. Each tool works correctly for valid inputs and handles errors gracefully. Tools don't throw uncaught exceptions — they always return a result dict (even on error). The `create_all_tools()` factory returns all 7 tools with the right names.
