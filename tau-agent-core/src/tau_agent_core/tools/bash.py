"""τ-agent-core tools: Bash tool.

Execute shell commands (with output streaming, temp files, timeout).

Reference: PHASE-2-SUBPHASE-3.md, "bash tool" section.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any, Callable

from tau_agent_core.tools.base import AgentToolResult


class BashTool:
    """Bash tool: Execute shell commands.

    Attributes:
        name: Tool name identifier
        label: Human-readable label
        description: Tool description for LLM
        parameters: JSON Schema for arguments
        execution_mode: "sequential"
        cwd: Working directory for command execution
    """

    name = "bash"
    label = "Run Command"
    description = (
        "Execute a shell command via subprocess. "
        "Handles output streaming and large output via temp files. "
        "Supports timeout and abort signals."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in milliseconds (default: 30000)",
            },
        },
        "required": ["command"],
    }
    execution_mode = "sequential"

    DEFAULT_TIMEOUT_MS = 30000
    MAX_OUTPUT_LINES = 4096
    POLL_INTERVAL = 0.02  # Check abort signal every 20ms

    def __init__(self, cwd: str = ".") -> None:
        self.cwd = os.path.abspath(cwd)

    async def execute(
        self,
        tool_call_id: str,
        args: dict,
        signal: Any = None,
        on_update: Callable | None = None,
    ) -> dict:
        """Execute the bash tool.

        Args:
            tool_call_id: Unique identifier for the tool call
            args: Tool arguments dict with 'command', optional 'timeout'
            signal: Optional AbortSignal
            on_update: Optional callback for progress updates

        Returns:
            Dict with 'content' list of content blocks and 'details' dict
        """
        command = args.get("command")
        if not command:
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message='Missing required argument: "command"',
                tool_call_id=tool_call_id,
            ).model_dump()

        timeout_ms = args.get("timeout", self.DEFAULT_TIMEOUT_MS)
        timeout_secs = timeout_ms / 1000.0 if timeout_ms else self.DEFAULT_TIMEOUT_MS / 1000.0

        # Check if already aborted before starting
        if signal and signal.is_aborted():
            return AgentToolResult(
                tool_name=self.name,
                tool_call_id=tool_call_id,
                content=[{"type": "text", "text": "Command was aborted"}],
            ).model_dump()

        # Create temp file for large output
        output_fd, output_path = tempfile.mkstemp(prefix="bash_output_", suffix=".txt")

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )

            # Collect output
            stdout_chunks = []
            stderr_chunks = []
            truncated = False
            line_count = 0
            aborted = False

            async def check_abort():
                """Periodically check signal and abort if needed."""
                nonlocal aborted
                while True:
                    if signal and signal.is_aborted():
                        aborted = True
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        raise asyncio.CancelledError("Command aborted")
                    await asyncio.sleep(self.POLL_INTERVAL)

            async def read_stream(stream, chunks, is_stderr):
                """Read from a subprocess stream."""
                nonlocal truncated, line_count
                while True:
                    if signal and signal.is_aborted():
                        raise asyncio.CancelledError("Command aborted")
                    try:
                        line = await asyncio.wait_for(
                            stream.readline(), timeout=0.1
                        )
                        if not line:
                            break
                        text = line.decode("utf-8", errors="replace")
                        if is_stderr:
                            stderr_chunks.append(text)
                        else:
                            stdout_chunks.append(text)
                        line_count += text.count("\n")
                        if line_count > self.MAX_OUTPUT_LINES and not truncated:
                            truncated = True
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        raise

            try:
                # Run the abort checker in the background
                abort_task = asyncio.create_task(check_abort())
                try:
                    # Read both streams concurrently with timeout
                    await asyncio.wait_for(
                        asyncio.gather(
                            read_stream(process.stdout, stdout_chunks, False),
                            read_stream(process.stderr, stderr_chunks, True),
                        ),
                        timeout=timeout_secs,
                    )
                    # Wait for process to fully finish to get exit code
                    await process.wait()
                except asyncio.TimeoutError:
                    process.kill()
                    truncated = True
                    stderr_chunks.insert(0, f"[Command timed out after {timeout_secs}s]\n")
                    await process.wait()
                finally:
                    abort_task.cancel()
                    try:
                        await abort_task
                    except asyncio.CancelledError:
                        pass

            except asyncio.CancelledError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await process.wait()
                except Exception:
                    pass
                return AgentToolResult(
                    tool_name=self.name,
                    tool_call_id=tool_call_id,
                    content=[{"type": "text", "text": "Command was aborted"}],
                ).model_dump()

            exit_code = process.returncode

            stdout_text = "".join(stdout_chunks)
            stderr_text = "".join(stderr_chunks)

            # Truncate large output
            if truncated:
                stdout_text = stdout_text[:10000] + "\n... [output truncated]"
                stderr_text = stderr_text[:10000] + "\n... [output truncated]"

            # Combine output
            output_parts = []
            if stdout_text:
                output_parts.append(stdout_text)
            if stderr_text:
                output_parts.append(f"stderr:\n{stderr_text}")

            full_output = "\n".join(output_parts) if output_parts else ""

            # Write output to temp file for reference
            try:
                with os.fdopen(output_fd, "w") as f:
                    f.write(full_output)
            except OSError:
                pass

            # Determine if it was an error
            if exit_code != 0 and stderr_text.strip():
                result = AgentToolResult(
                    tool_name=self.name,
                    tool_call_id=tool_call_id,
                    content=[{"type": "text", "text": full_output or "Command failed"}],
                    is_error=True,
                )
            elif exit_code != 0:
                result = AgentToolResult(
                    tool_name=self.name,
                    tool_call_id=tool_call_id,
                    content=[{"type": "text", "text": full_output or f"Command exited with code {exit_code}"}],
                )
            else:
                result = AgentToolResult(
                    tool_name=self.name,
                    tool_call_id=tool_call_id,
                    content=[{"type": "text", "text": full_output}],
                )

            result_dict = result.model_dump()
            result_dict["details"] = {
                "exit_code": exit_code,
                "truncated": truncated,
                "bytes_written": len(full_output.encode("utf-8", errors="replace")),
            }
            return result_dict

        except Exception as e:
            try:
                os.close(output_fd)
            except OSError:
                pass
            return AgentToolResult.from_error(
                tool_name=self.name,
                error_message=f"Error executing command: {e}",
                tool_call_id=tool_call_id,
            ).model_dump()

        finally:
            # Clean up temp file
            try:
                os.unlink(output_path)
            except OSError:
                pass
