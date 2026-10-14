"""The child side of the CPython provider. Standard library only.

Run as ``python -I -X disable-remote-debug _cpython_child.py RFD WFD MEMORY``.
It reads newline-delimited JSON requests on ``RFD`` and answers on ``WFD``; its
own stdout is not the channel, so a script's ``print`` cannot forge a message.

Requests: ``{"op": "run", "code", "inputs", "cpu_s"}``, ``{"op": "result",
"call", "ok", "value" | "error"}``. Answers: ``{"op": "call", "call", "name",
"args"}`` and ``{"op": "done", "ok", "output", "value", "error", "error_type"}``.
"""

import ast
import asyncio
import contextlib
import io
import json
import os
import resource
import sys
import traceback
from typing import Any

_VALUE = "__codemode_value__"
_FILENAME = "<codemode>"


class ToolError(RuntimeError):
    """A tool called from the script failed; the message is the tool's error text."""


class _Channel:
    """The JSON-lines pipe pair to the host."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.pending: dict[int, "asyncio.Future[Any]"] = {}
        self.next_id = 0

    def send(self, message: dict) -> None:
        """Queue one message to the host."""
        self.writer.write(json.dumps(message).encode() + b"\n")

    async def call(self, name: str, args: dict) -> Any:
        """Ask the host to run tool ``name`` and wait for its answer."""
        try:
            payload = json.dumps({"op": "call", "call": self.next_id, "name": name, "args": args})
        except (TypeError, ValueError) as exc:
            raise TypeError(f"arguments to {name} must be JSON values: {exc}") from None
        call_id = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[call_id] = future
        self.writer.write(payload.encode() + b"\n")
        answer = await future
        if answer["ok"]:
            return answer["value"]
        raise ToolError(answer["error"])


class _Tools:
    """``tools.<name>(**kwargs)``: every attribute is an async tool call."""

    def __init__(self, channel: _Channel) -> None:
        self._channel = channel

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        channel = self._channel

        async def call(*args: Any, **kwargs: Any) -> Any:
            if args:
                raise TypeError(f"tools.{name}() takes keyword arguments only")
            return await channel.call(name, kwargs)

        call.__name__ = name
        return call

    def __repr__(self) -> str:
        return "<tools>"


def _fresh_globals(channel: _Channel) -> dict:
    """A new script namespace: ``tools`` plus the discovery functions."""

    async def search_tools(query: str, limit: int = 8) -> Any:
        return await channel.call("__search_tools", {"query": query, "limit": limit})

    async def describe_tool(name: str) -> Any:
        return await channel.call("__describe_tool", {"name": name})

    return {
        "__name__": "__main__",
        "__builtins__": __builtins__,
        "tools": _Tools(channel),
        "search_tools": search_tools,
        "describe_tool": describe_tool,
        "ToolError": ToolError,
    }


def _compile(code: str) -> Any:
    """Compile with top-level ``await``; a trailing expression is kept as the value."""
    tree = compile(
        code, _FILENAME, "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
    )
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = tree.body[-1]
        tree.body[-1] = ast.Assign(targets=[ast.Name(id=_VALUE, ctx=ast.Store())], value=last.value)
        ast.copy_location(tree.body[-1], last)
        ast.fix_missing_locations(tree)
    return compile(tree, _FILENAME, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)


def _script_traceback(exc: BaseException) -> str:
    """The traceback restricted to the script's own frames."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == _FILENAME]
    lines = ["Traceback (most recent call last):\n"] if frames else []
    lines += traceback.format_list(frames)
    lines += traceback.format_exception_only(type(exc), exc)
    return "".join(lines)


def _arm_cpu(seconds: float) -> None:
    """Allow this run ``seconds`` more CPU time; SIGXCPU kills the process after."""
    used = resource.getrusage(resource.RUSAGE_SELF)
    spent = used.ru_utime + used.ru_stime
    soft = int(spent + seconds) + 1
    resource.setrlimit(resource.RLIMIT_CPU, (soft, resource.RLIM_INFINITY))


async def _run(channel: _Channel, namespace: dict, request: dict) -> None:
    """Run one script and send its ``done`` message."""
    buffer = io.StringIO()
    namespace.pop(_VALUE, None)
    namespace.update(request.get("inputs") or {})
    _arm_cpu(float(request.get("cpu_s", 30)))
    try:
        code = _compile(request["code"])
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            result = eval(code, namespace)
            if asyncio.iscoroutine(result):
                await result
        value = namespace.pop(_VALUE, None)
        channel.send(
            {
                "op": "done",
                "ok": True,
                "output": buffer.getvalue(),
                "value": None if value is None else repr(value),
            }
        )
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        channel.send(
            {
                "op": "done",
                "ok": False,
                "output": buffer.getvalue(),
                "error": _script_traceback(exc),
                "error_type": type(exc).__name__,
            }
        )
    await channel.writer.drain()


async def _serve(rfd: int, wfd: int) -> None:
    """Read requests until the host closes the pipe."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=1 << 26)
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(rfd, "rb", buffering=0)
    )
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, os.fdopen(wfd, "wb", buffering=0)
    )
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    channel = _Channel(reader, writer)
    namespace = _fresh_globals(channel)
    running: set = set()
    while True:
        line = await reader.readline()
        if not line:
            return
        message = json.loads(line)
        if message["op"] == "run":
            task = asyncio.ensure_future(_run(channel, namespace, message))
            running.add(task)
            task.add_done_callback(running.discard)
        elif message["op"] == "result":
            future = channel.pending.pop(message["call"], None)
            if future is not None and not future.done():
                future.set_result(message)


def main() -> None:
    """Apply the memory limit, then serve."""
    rfd, wfd, memory = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    if memory > 0:
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    asyncio.run(_serve(rfd, wfd))


if __name__ == "__main__":
    main()
