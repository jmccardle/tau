"""CPython-subprocess sandbox provider (docs/CODE-MODE.md §6.2).

Each instance is ``sys.executable -I -X disable-remote-debug`` running
``_cpython_child.py``, with an environment scrubbed down to an allow-list, a
JSON-lines tool channel on its own pipe pair, ``RLIMIT_AS`` and ``RLIMIT_CPU``,
and a host-side ``SIGKILL`` at the deadline. It is an OS process with the user's
privileges — the same trust as ``bash`` — so what it buys is the four things in
CODE-MODE.md §3, not a boundary against the model.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal as signals
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tau_agent_core.codemode.prompt import CPYTHON_TEMPLATES
from tau_agent_core.codemode.protocol import (
    Lifetime,
    PromptTemplates,
    RunLimits,
    RunResult,
    ToolInvoker,
)
from tau_agent_core.codemode.scoped import ScopedProvider

CHILD = Path(__file__).with_name("_cpython_child.py")

ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")
"""The only host variables a child inherits. Keys and ``TAU_*`` never match."""


@dataclass
class _Child:
    """One running child and its channel."""

    process: asyncio.subprocess.Process
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    tools: list[ToolInvoker] = field(default_factory=list)

    def send(self, message: dict[str, Any]) -> None:
        """Queue one JSON line to the child."""
        self.writer.write(json.dumps(message).encode() + b"\n")


class CPythonProvider(ScopedProvider[_Child]):
    """Full CPython in a child process, one per instance.

    Args:
        lifetime: ``script``, ``turn`` or ``session``.
        cwd: The child's working directory; ``None`` is the host's.
        env_passthrough: Extra variable names to copy from the host.
        prewarm: Keep one spawned child ready, so a ``script`` run does not pay
            the spawn on its own clock.
    """

    name = "cpython"
    language = "python"

    def __init__(
        self,
        lifetime: Lifetime = "script",
        *,
        cwd: str | None = None,
        env_passthrough: Iterable[str] = (),
        prewarm: bool = True,
    ) -> None:
        super().__init__(lifetime)
        self._cwd = cwd
        self._env_names = (*ENV_ALLOW, *env_passthrough)
        self._prewarm = prewarm
        self._spare: asyncio.Task[_Child] | None = None
        self._spare_memory = 0

    def prompt(self) -> PromptTemplates:
        """CPython's default templates."""
        return CPYTHON_TEMPLATES

    def child_env(self) -> dict[str, str]:
        """The scrubbed environment a child gets."""
        return {k: os.environ[k] for k in self._env_names if k in os.environ}

    async def start(self) -> None:
        """Nothing long-lived; a spare child is spawned on first use."""

    async def close(self) -> None:
        """Destroy every instance and the spare."""
        await super().close()
        if self._spare is not None:
            spare, self._spare = self._spare, None
            child = await asyncio.gather(spare, return_exceptions=True)
            if isinstance(child[0], _Child):
                await self._destroy(child[0])

    async def _spawn(self, memory: int) -> _Child:
        """Start one child process and connect its channel."""
        to_child_r, to_child_w = os.pipe()
        from_child_r, from_child_w = os.pipe()
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-X",
                "disable-remote-debug",
                str(CHILD),
                str(to_child_r),
                str(from_child_w),
                str(memory),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=(to_child_r, from_child_w),
                env=self.child_env(),
                cwd=self._cwd,
                start_new_session=True,
            )
        finally:
            os.close(to_child_r)
            os.close(from_child_w)
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=1 << 26)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(from_child_r, "rb", buffering=0)
        )
        transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, os.fdopen(to_child_w, "wb", buffering=0)
        )
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        return _Child(process, reader, writer)

    async def _open(self, limits: RunLimits, tools: ToolInvoker) -> _Child:
        """A child: the spare when its memory limit matches, else a new spawn."""
        child: _Child | None = None
        if self._spare is not None and self._spare_memory == limits.memory_bytes:
            spare, self._spare = self._spare, None
            with contextlib.suppress(Exception):
                child = await spare
            if child is not None and child.process.returncode is not None:
                child = None
        if child is None:
            child = await self._spawn(limits.memory_bytes)
        if self._prewarm and self.lifetime == "script" and self._spare is None:
            self._spare_memory = limits.memory_bytes
            self._spare = asyncio.ensure_future(self._spawn(limits.memory_bytes))
        return child

    async def _destroy(self, instance: _Child) -> None:
        """SIGKILL the child's process group and reap it."""
        with contextlib.suppress(Exception):
            instance.writer.close()
        if instance.process.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(instance.process.pid, signals.SIGKILL)
        with contextlib.suppress(Exception):
            await instance.process.wait()

    async def _answer(self, child: _Child, tools: ToolInvoker, message: dict[str, Any]) -> None:
        """Run one requested call and send its result back."""
        name, args, call = message["name"], message.get("args") or {}, message["call"]
        if name == "__search_tools":
            reply: dict[str, Any] = {
                "ok": True,
                "value": tools.search(str(args.get("query", "")), int(args.get("limit", 8))),
            }
        elif name == "__describe_tool":
            try:
                reply = {"ok": True, "value": tools.describe(str(args.get("name", "")))}
            except KeyError as exc:
                reply = {"ok": False, "error": str(exc.args[0])}
        else:
            outcome = await tools.invoke(name, args)
            reply = (
                {"ok": False, "error": outcome.text}
                if outcome.is_error
                else {"ok": True, "value": outcome.text}
            )
        if child.process.returncode is None:
            child.send({"op": "result", "call": call, **reply})

    async def _execute(
        self,
        instance: _Child,
        code: str,
        tools: ToolInvoker,
        inputs: Mapping[str, Any],
        limits: RunLimits,
    ) -> RunResult:
        """Send the script and serve its tool calls until it is done."""
        instance.send(
            {"op": "run", "code": code, "inputs": dict(inputs), "cpu_s": limits.timeout_s}
        )
        calls: set[asyncio.Task[None]] = set()
        try:
            while True:
                line = await instance.reader.readline()
                if not line:
                    return await self._died(instance)
                message = json.loads(line)
                if message["op"] == "call":
                    task = asyncio.ensure_future(self._answer(instance, tools, message))
                    calls.add(task)
                    task.add_done_callback(calls.discard)
                elif message["op"] == "done":
                    return self._done(message, tools)
        finally:
            for task in calls:
                task.cancel()

    @staticmethod
    def _done(message: dict[str, Any], tools: ToolInvoker) -> RunResult:
        """The result for a ``done`` message."""
        if message["ok"]:
            return RunResult(output=message["output"], value=message.get("value"))
        error_type = message.get("error_type")
        budget = bool(getattr(tools, "budget_exhausted", False))
        if error_type == "MemoryError":
            return RunResult(
                output=message["output"],
                error=message["error"],
                error_kind="sandbox",
                limit="memory",
                reset=True,
            )
        return RunResult(
            output=message["output"],
            error=message["error"],
            error_kind="sandbox" if budget else "script",
            limit="calls" if budget else None,
        )

    @staticmethod
    async def _died(instance: _Child) -> RunResult:
        """The result when the child exited mid-run."""
        code = await instance.process.wait()
        stderr = b""
        if instance.process.stderr is not None:
            with contextlib.suppress(Exception):
                stderr = await asyncio.wait_for(instance.process.stderr.read(), 1.0)
        tail = stderr.decode(errors="replace").strip().splitlines()[-3:]
        if code == -signals.SIGXCPU:
            return RunResult(
                error="the script exceeded its CPU-time limit and was killed",
                error_kind="timeout",
                limit="time",
                reset=True,
            )
        detail = f" ({' | '.join(tail)})" if tail else ""
        memory = any("MemoryError" in line for line in tail)
        return RunResult(
            error=f"the sandbox process exited with status {code}{detail}",
            error_kind="sandbox",
            limit="memory" if memory else None,
            reset=True,
        )
