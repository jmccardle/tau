"""τ-agent-core tools: Bash tool.

Execute shell commands (with output streaming, temp files, timeout).

Reference: PHASE-2-SUBPHASE-3.md, "bash tool" section.
"""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
from typing import Any, Callable, Literal

from tau_agent_core.tools.base import AgentToolResult


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    grace_period: float = 2.0,
) -> None:
    """Kill a subprocess and everything in its process group.

    The subprocess must have been started with ``start_new_session=True``, so
    its pid is also its process group id (pgid): the shell, every pipeline
    member, and anything it backgrounded (via ``&``) all share that pgid and
    die together, POSIX-style.

    Escalates: SIGTERM to the group, wait up to ``grace_period`` for the
    direct child (the shell) to exit, then SIGKILL to the group regardless —
    a group member that ignored SIGTERM (or a straggler that forked after it
    was sent) does not get to survive. ``ProcessLookupError`` from a signal
    that finds no such process/group is expected once the group is gone and
    is swallowed; any other error is not. A pid-reuse guard (``returncode is
    not None`` before the first signal) protects the initial SIGTERM the same
    way CPython's own ``Process.kill()`` would; it is deliberately not
    reapplied before the final SIGKILL, because that escalation exists
    precisely to reach group members that outlive the shell's own exit.
    """
    pgid = process.pid
    if process.returncode is not None:
        # asyncio already knows this child exited (its watcher reaped it and
        # recorded the exit status locally) — mirror the guard CPython's
        # subprocess.Popen.send_signal() applies via self.poll() (bpo-38630),
        # which is what protects the ordinary Process.kill() path this
        # function replaces. Once every member of a process group has
        # exited, the kernel is free to recycle that pgid onto an unrelated
        # session/group leader; signalling `pgid` here on stale local
        # knowledge could hit that unrelated group instead. If any group
        # member is still alive, the pgid stays reserved and this branch
        # cannot be reached in error, so nothing here can leak an orphan.
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return  # Group is already gone; nothing left to escalate to.
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_period)
    except asyncio.TimeoutError:
        pass
    finally:
        # Always escalate, even if we were cancelled while waiting out the
        # grace period — cleanup must not be skippable by cancellation.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


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
    # Annotated rather than left to inference (B1/tau-004): unannotated,
    # `execution_mode = "sequential"` infers `str`, and `ToolDefinition`
    # declares it `Literal["sequential", "parallel"]`. `sdk._resolve_tools`
    # copies this value into a ToolDefinition, so without the annotation mypy
    # cannot check that copy — which is the blindness B1 exists to remove.
    execution_mode: Literal["sequential", "parallel"] = "sequential"

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
                start_new_session=True,
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
                        await _terminate_process_group(process)
                        raise asyncio.CancelledError("Command aborted")
                    await asyncio.sleep(self.POLL_INTERVAL)

            async def read_stream(stream, chunks, is_stderr):
                """Read from a subprocess stream."""
                nonlocal truncated, line_count
                while True:
                    if signal and signal.is_aborted():
                        raise asyncio.CancelledError("Command aborted")
                    try:
                        line = await asyncio.wait_for(stream.readline(), timeout=0.1)
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
                    # No group kill here, deliberately: the shell exited on
                    # its own, so anything still holding the pgid is a
                    # daemonized process the command asked to outlive it
                    # (e.g. `nohup server >/dev/null 2>&1 &`) — the same
                    # UNIX contract as closing a terminal on a nohup'd job.
                    # This intentionally does NOT implement pi's
                    # trackDetachedChildPid / killTrackedDetachedChildren
                    # (shell.ts:170-183), which persists such pids so a
                    # later, *session*-lifetime kill can still reach them:
                    # that is cross-call process-level signal tracking, out
                    # of scope for this unit (P2/R-T4 cover a single
                    # execute() call's own kill paths, not session teardown).
                except asyncio.TimeoutError:
                    await _terminate_process_group(process)
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
                await _terminate_process_group(process)
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
                    content=[
                        {
                            "type": "text",
                            "text": full_output or f"Command exited with code {exit_code}",
                        }
                    ],
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
