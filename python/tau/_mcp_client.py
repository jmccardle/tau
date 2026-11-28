"""An MCP client (D24 section 12), from tau-acp's `mcp.py`: the handshake, `tools/list` and
`tools/call`, over stdio or streamable HTTP.

Messages are JSON-RPC 2.0. We offer no client capabilities: a request from the server is answered
`-32601`, except `ping`, which is answered whatever else we offer. A server's
`notifications/tools/list_changed` calls `on_change`. A message that is not JSON-RPC (an HTTP body
that is not JSON, a JSON line that is not an object) fails the requests waiting for an answer (with
`McpMalformed`); the server is still used.

**stdio**: newline-delimited JSON on the server's stdin and stdout, in an environment of its own
(`_mcp_process.environment`). Its stderr, and any stdout line that is not JSON (a banner, a stray
log line: skipped, not a message), are never shown as they come (they would draw over a terminal
UI): their last lines are kept (`tail`), and the last of them ends the message when it exits.
**HTTP**: each message is a POST to the server's URL, answered with
`202`, a JSON body, or an event stream that carries the answer (and what the server sends
before it). The session id the server gives at `initialize` goes with every later POST, and a
`DELETE` ends it at stop. We open no `GET` stream, so an HTTP server's unprompted messages
(`list_changed` among them) reach us only inside the answer to one of our requests.

No `env` or `headers` value is ever logged or put in an error.
"""

import asyncio
import collections
import contextlib
import importlib.metadata
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import httpx

from tau._mcp_config import McpServer
from tau._mcp_process import Child, environment, spawn, stop

PROTOCOL_VERSION = "2025-11-25"
READ_LIMIT = 1 << 26
STOP_GRACE = 1.5  # between closing stdin, SIGTERM and SIGKILL
_TAIL = 20
METHOD_NOT_FOUND = -32601
LIST_CHANGED = "notifications/tools/list_changed"
Message = dict[str, Any]
_END = asyncio.IncompleteReadError  # rules: allow(incomplete): asyncio's name for end of stream


class McpError(Exception):
    """The server answered a request with a JSON-RPC error, or could not be reached; the
    message completes `MCP server NAME: ...`."""


class McpMalformed(McpError):
    """The server sent something that is not JSON-RPC or MCP."""


class McpExited(Exception):
    """The server process exited; the message says so, with its last line of output (`tail`)."""


class McpStartError(Exception):
    """A server could not be started; the message names it and says why."""


@dataclass(frozen=True)
class McpTool:
    """A tool a server lists: its MCP name, description and input schema (`None` if it lists
    none that is a JSON object: such a tool cannot be offered)."""

    name: str
    description: str
    schema: dict[str, Any] | None


class _Transport(Protocol):
    async def send(self, message: Message) -> None: ...
    def post(self, message: Message) -> None: ...
    async def close(self, grace: float) -> None: ...


class McpClient:
    """One running MCP server. `start` connects it and shakes hands; `tools` lists its tools;
    `call` calls one; `stop` ends it. `on_change` is called on `list_changed`, `on_exit` once
    when a stdio server exits by itself."""

    def __init__(self, server: McpServer) -> None:
        self.name = server.name
        self.server = server
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self.exited: str | None = None  # why it is gone, once it is
        self.stopping = False
        self.has_tools = False
        self.on_change: Callable[[], None] = lambda: None
        self.on_exit: Callable[[str], None] = lambda why: None
        self._transport: _Transport | None = None
        self.relisting: asyncio.Task[None] | None = None  # set by its owner while it lists again

    @classmethod
    async def start(
        cls, server: McpServer, cwd: Path, *, cancel_grace: float = STOP_GRACE
    ) -> "McpClient":
        """Start or reach the server and shake hands. `McpStartError` naming it, with nothing
        left running, if that fails; cancelling it stops what it started, with `cancel_grace`
        between each step of the stop."""
        client = cls(server)
        try:
            if server.transport == "stdio":
                client._transport = await _Stdio.open(client, cwd)
            else:
                client._transport = _Http(client)
            await client._handshake()
        except McpExited as e:
            await client.stop()
            raise McpStartError(str(e)) from None
        except McpError as e:
            await client.stop()
            raise McpStartError(f"MCP server {server.name}: {e}") from None
        except BaseException:
            await client.stop(grace=cancel_grace)
            raise
        return client

    async def _handshake(self) -> None:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "tau", "version": _version()},
            },
        )
        self.has_tools = "tools" in (result.get("capabilities") or {})
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def tools(self) -> list[McpTool]:
        """The server's tools, following `nextCursor`; none if it has no `tools` capability.
        `McpError` naming the problem."""
        listed: list[McpTool] = []
        cursor: str | None = None
        while self.has_tools:
            try:
                result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
                for tool in result.get("tools", []):
                    description = str(tool.get("description") or "")
                    schema: object = tool.get("inputSchema")
                    if not isinstance(schema, dict):
                        schema = None
                    usable = cast("dict[str, Any] | None", schema)
                    listed.append(McpTool(str(tool["name"]), description, usable))
            except (KeyError, TypeError, AttributeError) as e:
                raise McpMalformed(f"tools/list answered a malformed list ({e!r:.80})") from None
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return listed

    async def call(self, tool: str, arguments: object) -> dict[str, Any]:
        """`tools/call`. Cancelling this sends `notifications/cancelled`."""
        params = {"name": tool, "arguments": arguments}
        return await self.request("tools/call", params, cancel_reason="cancelled by τ")

    async def request(
        self, method: str, params: dict[str, Any], cancel_reason: str | None = None
    ) -> Any:
        """Send a request and wait for its result."""
        if self.exited is not None:
            raise McpExited(self.exited)
        assert self._transport is not None
        self._next_id += 1
        ident = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        try:
            await self._transport.send(
                {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
            )
            if not future.done() and isinstance(self._transport, _Http):
                raise McpMalformed(f"answered {method} with no result")
            return await future
        except asyncio.CancelledError:
            if cancel_reason is not None and self.exited is None:
                note = {"requestId": ident, "reason": cancel_reason}
                self.post({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": note})
            raise
        finally:
            self._pending.pop(ident, None)

    def post(self, message: Message) -> None:
        """Send a notification or a response; nothing waits for it."""
        if self._transport is not None and self.exited is None:
            self._transport.post(message)

    # What arrives

    def answered(self, ident: object) -> bool:
        """Whether request `ident` is not waiting for an answer any more."""
        future = self._pending.get(ident) if isinstance(ident, int) else None
        return future is None or future.done()

    def fail_pending(self, error: BaseException) -> None:
        """Fail every request waiting for an answer with `error`."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    def ended(self, why: str) -> None:
        """The server is gone: every request waiting fails; `on_exit` unless we stopped it."""
        if self.exited is not None:
            return
        self.exited = why
        self.fail_pending(McpExited(why))
        if not self.stopping:
            self.on_exit(why)

    def receive(self, data: bytes | str) -> None:
        """One message (or a batch) as it came."""
        try:
            message: object = json.loads(data)
        except (ValueError, RecursionError):
            self.fail_pending(McpMalformed("sent a message that is not JSON"))
            return
        self.deliver(message)

    def deliver(self, message: object) -> None:
        """One decoded message (or a batch)."""
        for one in cast("list[object]", message) if isinstance(message, list) else [message]:
            if isinstance(one, dict):
                self._dispatch(cast("Message", one))
            else:
                self.fail_pending(McpMalformed("sent a message that is not a JSON object"))

    def _dispatch(self, message: Message) -> None:
        ident, method = message.get("id"), message.get("method")
        if method is None:
            self._resolve(ident, message)
        elif ident is None:
            if method == LIST_CHANGED:
                self.on_change()
        elif method == "ping":
            self.post({"jsonrpc": "2.0", "id": ident, "result": {}})
        else:
            error = {"code": METHOD_NOT_FOUND, "message": "tau offers no client capabilities"}
            self.post({"jsonrpc": "2.0", "id": ident, "error": error})

    def _resolve(self, ident: object, message: Message) -> None:
        future = self._pending.get(ident) if isinstance(ident, int) else None
        if future is None or future.done():
            return
        error, result = message.get("error"), message.get("result")
        if error is not None:
            said = (
                cast("dict[str, object]", error).get("message") if isinstance(error, dict) else None
            )
            if isinstance(said, str):
                future.set_exception(McpError(said))
            else:
                future.set_exception(McpMalformed(f"returned a malformed error: {error!r:.80}"))
        elif isinstance(result, dict):
            future.set_result(result)
        else:
            future.set_exception(McpMalformed(f"returned a malformed result: {result!r:.80}"))

    async def stop(self, grace: float = STOP_GRACE) -> None:
        """End the server (a stdio process stopped, with `grace` between closing its stdin,
        SIGTERM and SIGKILL; an HTTP session deleted)."""
        self.stopping = True
        if self._transport is not None:
            await self._transport.close(grace)
        self.ended("stopped")


def _version() -> str:
    return importlib.metadata.version("ffwf-tau")


class _Stdio:
    """A server process: messages are lines on its stdin and stdout."""

    def __init__(self, client: McpClient, process: asyncio.subprocess.Process) -> None:
        self._client = client
        self._process = process
        self._child = Child(process)
        self.tail: collections.deque[str] = collections.deque(maxlen=_TAIL)
        # stderr's reader first, and each kept as it starts: under an eager task factory (the
        # terminal UI's) a reader runs at once, to its end if the server has already exited, and
        # `_read` then waits for stderr's, which must exist by then.
        self._stderr_reader = asyncio.ensure_future(self._read_stderr())
        self._stdout_reader = asyncio.ensure_future(self._read())

    @classmethod
    async def open(cls, client: McpClient, cwd: Path) -> "_Stdio":
        server = client.server
        try:
            process = await spawn(
                server.command, server.args, environment(server.env), cwd, READ_LIMIT
            )
        except OSError as e:
            why = e.strerror or str(e)
            where = f"MCP server {server.name}: cannot start {server.command}"
            raise McpExited(f"{where}: {why}") from None
        return cls(client, process)

    async def send(self, message: Message) -> None:
        self.post(message)

    def post(self, message: Message) -> None:
        stdin = self._process.stdin
        assert stdin is not None
        with contextlib.suppress(ConnectionError, RuntimeError):
            stdin.write(json.dumps(message).encode() + b"\n")

    async def _read(self) -> None:
        stdout = self._process.stdout
        assert stdout is not None
        try:
            while True:
                try:
                    line = await stdout.readuntil(b"\n")
                except _END as end:
                    if end.partial.strip():
                        self._line(end.partial)
                    break
                except asyncio.LimitOverrunError as over:
                    await stdout.readexactly(over.consumed)  # discard it, then the rest of it
                    await stdout.readuntil(b"\n")
                    self._client.fail_pending(
                        McpMalformed(f"sent a line longer than {READ_LIMIT} bytes")
                    )
                    continue
                if line.strip():
                    self._line(line)
        except (ConnectionError, _END, asyncio.LimitOverrunError):
            pass
        code = await self._process.wait()
        await asyncio.gather(self._stderr_reader, return_exceptions=True)
        last = f": {self.tail[-1]}" if self.tail else ""
        self._client.ended(f"MCP server {self._client.name} exited (status {code}){last}")

    def _line(self, line: bytes) -> None:
        try:
            message: object = json.loads(line)
        except (ValueError, RecursionError):  # a banner or a stray log line, not a message
            self._keep("[stdout] " + line.decode("utf-8", errors="replace"))
            return
        self._client.deliver(message)

    def _keep(self, text: str) -> None:
        if text := text.strip():
            self.tail.append(text[:300])

    async def _read_stderr(self) -> None:
        stderr = self._process.stderr
        assert stderr is not None
        with contextlib.suppress(ConnectionError, ValueError):
            while line := await stderr.readline():
                self._keep(line.decode("utf-8", errors="replace"))

    async def close(self, grace: float) -> None:
        await stop(self._child, grace)
        await asyncio.gather(self._stdout_reader, self._stderr_reader, return_exceptions=True)


class _Http:
    """A streamable HTTP server: each message is a POST, answered in its response."""

    def __init__(self, client: McpClient) -> None:
        self._client = client
        self._url = client.server.url
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
        self._session: str | None = None
        self._version: str | None = None
        self._posts: set[asyncio.Task[None]] = set()

    def _headers(self) -> dict[str, str]:
        headers = {
            **self._client.server.headers,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self._session is not None:
            headers["Mcp-Session-Id"] = self._session
        if self._version is not None:
            headers["MCP-Protocol-Version"] = self._version
        return headers

    async def send(self, message: Message) -> None:
        try:
            await self._exchange(message)
        except httpx.HTTPError as e:
            raise McpError(f"{self._url} could not be reached: {type(e).__name__}") from None

    def post(self, message: Message) -> None:
        async def run() -> None:
            with contextlib.suppress(httpx.HTTPError, McpError):
                await self._exchange(message)

        task = asyncio.ensure_future(run())
        self._posts.add(task)
        task.add_done_callback(self._posts.discard)

    async def _exchange(self, message: Message) -> None:
        body = json.dumps(message).encode()
        async with self._http.stream(
            "POST", self._url, content=body, headers=self._headers()
        ) as response:
            if response.status_code == 202:  # noqa: PLR2004 - accepted, nothing to answer
                return
            if response.status_code >= 400:  # noqa: PLR2004
                expired = response.status_code == 404 and self._session is not None  # noqa: PLR2004
                why = "its session expired; restart τ" if expired else "refused the request"
                raise McpError(f"{self._url} {why} (HTTP {response.status_code})")
            if message.get("method") == "initialize":
                self._session = response.headers.get("mcp-session-id")
            kind = response.headers.get("content-type", "")
            if kind.startswith("text/event-stream"):
                await self._events(response, message.get("id"))
            else:
                self._client.receive(await response.aread())
            if message.get("method") == "initialize":
                self._version = PROTOCOL_VERSION

    async def _events(self, response: httpx.Response, ident: object) -> None:
        """Dispatch each event's data until the answer to `ident` has come."""
        data: list[str] = []
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
            elif not line and data:
                self._client.receive("\n".join(data))
                data = []
                if self._client.answered(ident):
                    return
        if data:
            self._client.receive("\n".join(data))

    async def close(self, grace: float) -> None:
        for task in list(self._posts):
            task.cancel()
        await asyncio.gather(*self._posts, return_exceptions=True)
        if self._session is not None:
            with contextlib.suppress(httpx.HTTPError):
                await self._http.delete(self._url, headers=self._headers(), timeout=grace)
        await self._http.aclose()
