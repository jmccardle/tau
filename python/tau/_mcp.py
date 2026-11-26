"""`Mcp`: the built-in extension, off by default, that gives the model the tools of the MCP
servers the user configured (D24 section 12; the files are `_mcp_config`'s).

With no server configured it starts nothing and offers nothing. Otherwise its `start` starts
every server at once (a stdio process, or an HTTP session), shakes hands and lists each one's
tools, and returns when all are ready, so the first step already has them. Each tool is offered
as `mcp__SERVER__TOOL` (characters outside `[A-Za-z0-9_-]` in the tool's name become `_`; at most
64 characters), described and typed as the server lists it, and goes through `permissions` like
any other (`mcp__SERVER` in a rule names every tool of that server).

**A server that fails to start is a warning** (as an unloadable skill is): a spawn error, a
failed handshake or tool list, or a server still starting when the start's time is up (the D32
start timeout less `_MARGIN`) is skipped, and τ starts without it. Each, and each file or server
the configuration skipped, is said on stderr at the start, listed by `/mcp` (`failed: why`), and
told as an `error` notice, which stands: a client that connects later still gets it (a start runs
before any client is connected). A slow first start (`npx` downloading its package) is given
`extensions.mcp.start_timeout`.

**While it runs**: a server that lists its tools again (`notifications/tools/list_changed`) is
listed again and the tools reassembled (clients get `Commands`); one that exits is told to
clients as an `error` notice, and its tools answer `Err` naming the exit until τ restarts. A call
that is cancelled (an abort) sends the server `notifications/cancelled`. `/mcp` lists the
servers, their state and their tools.

A result's `text` blocks are the call's text; `image` blocks its images (D30); a `resource` with
text adds that text; anything else is a line saying what was left out. A result with no text but
`structuredContent` is that JSON. `isError` makes it an `Err`.
"""

import asyncio
import base64
import binascii
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from tau._core import Image, Outcome
from tau._handles import TreeHandle
from tau._mcp_client import McpClient, McpError, McpExited, McpStartError, McpTool
from tau._mcp_config import PROJECT_FILES, USER_FILE, McpConfigError, McpServer, servers
from tau._protocols import Tool
from tau._startup import LoadedConfig

NAME = "mcp"
PREFIX = "mcp__"
NAME_LIMIT = 64
CONFIG_VARIABLE = "TAU_MCP_CONFIG"
"""The environment variable naming more MCP files (separated by `os.pathsep`), nearest of all: for
a program that starts τ with servers of its own (an editor's, through tau-acp)."""

__all__ = ["Mcp", "McpConfigError", "McpServer", "McpStartError", "tau_name"]


def tau_name(server: str, tool: str) -> str:
    """The τ name of an MCP tool: `mcp__SERVER__TOOL`, the tool's name reduced to
    `[A-Za-z0-9_-]`."""
    return f"{PREFIX}{server}__{re.sub(r'[^A-Za-z0-9_-]', '_', tool)}"


class _ServerTool:
    """One MCP tool as a τ tool."""

    def __init__(
        self, client: McpClient, tool: McpTool, name: str, schema: Mapping[str, object]
    ) -> None:
        self._client = client
        self._tool = tool.name
        self.name = name
        self.description = tool.description
        self.parameters = schema

    async def call(self, args_raw: str) -> Outcome:
        try:
            arguments: object = json.loads(args_raw) if args_raw.strip() else {}
        except ValueError as e:
            return Outcome.Err(f"the arguments are not JSON: {e}")
        if not isinstance(arguments, dict):
            return Outcome.Err("the arguments must be a JSON object")
        server = self._client.name
        try:
            result = await self._client.call(self._tool, cast("dict[str, object]", arguments))
            relisting = self._client.relisting
            if relisting is not None:  # the call changed the list: the next sample has it
                await asyncio.gather(asyncio.shield(relisting), return_exceptions=True)
            return outcome(result)
        except McpExited as e:
            return Outcome.Err(f"{e}; its tools answer this until τ restarts")
        except McpError as e:
            return Outcome.Err(f"MCP server {server}: {e}")


def outcome(result: Mapping[str, Any]) -> Outcome:
    """A `tools/call` result as the call's outcome (the module's docstring says how)."""
    content: object = result.get("content") or []
    if not isinstance(content, list) or not all(
        isinstance(b, dict) for b in cast("list[object]", content)
    ):
        return Outcome.Err(f"the MCP server returned a malformed result: content {content!r:.80}")
    lines: list[str] = []
    images: list[Image] = []
    for block in cast("list[dict[str, Any]]", content):
        _block(block, lines, images)
    structured = result.get("structuredContent")
    if not any(b.get("type") == "text" for b in cast("list[dict[str, Any]]", content)) and (
        structured is not None
    ):
        lines.insert(0, json.dumps(structured))
    text = "\n".join(lines)
    if result.get("isError"):
        return Outcome.Err(text or "the tool reported an error")
    return Outcome.Ok(text, images)


def _block(block: Mapping[str, Any], lines: list[str], images: list[Image]) -> None:
    kind = block.get("type")
    if kind == "text":
        lines.append(str(block.get("text", "")))
    elif kind == "image":
        mime = block.get("mimeType", "image")
        try:
            images.append(Image(base64.b64decode(str(block.get("data", "")), validate=True)))
        except (ValueError, binascii.Error) as e:
            lines.append(f"[{mime} image left out: {e}]")
    elif kind == "resource" and isinstance(block.get("resource"), dict):
        resource = cast("dict[str, Any]", block["resource"])
        if isinstance(resource.get("text"), str):
            lines.append(resource["text"])
        else:
            lines.append(f"[resource {resource.get('uri', '')} left out: it is not text]")
    elif kind == "resource_link":
        lines.append(f"[resource link: {block.get('uri', '')}]")
    else:
        lines.append(f"[{kind} content left out: τ tool results are text and images]")


def _read(path: Path, *, named: bool, problems: list[str]) -> bytes | None:
    """A file's bytes; `None` for none. One that cannot be read is a problem (it is skipped), but
    a file `TAU_MCP_CONFIG` names must be there and readable: the program that named it made a
    mistake, so `McpConfigError`."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        if named:
            raise McpConfigError(f"{path}: no such file ({CONFIG_VARIABLE} names it)") from None
        return None
    except OSError as e:
        if named:
            raise McpConfigError(f"{path}: cannot be read ({e.strerror})") from None
        problems.append(f"{path}: cannot be read ({e.strerror})")
        return None


_Started = tuple[McpClient, list[McpTool]]
"""A server started, and the tools it listed."""
_KILL_GRACE = 0.2  # a server stopped before its handshake ended has nothing to save
_MARGIN = 1.5  # what the extension's own start timeout leaves for stopping the slow servers


class Mcp:
    """The `mcp` built-in: the configured MCP servers' tools, while it is started. `warnings`
    are the files and servers its configuration skipped; `failed` the servers that did not
    start, each with why (both filled by `start`, and told to clients when they connect)."""

    name = NAME

    def __init__(
        self,
        servers: Sequence[McpServer],
        cwd: Path,
        *,
        warnings: Sequence[str] = (),
        start_timeout: float | None = None,
    ) -> None:
        self.servers = tuple(servers)
        self.warnings = tuple(warnings)
        self.failed: dict[str, str] = {}
        self._cwd = cwd
        self._within = _deadline(start_timeout)
        self._clients: dict[str, McpClient] = {}
        self._listed: dict[str, list[McpTool]] = {}  # by server, as each lists them
        self._tools: dict[str, list[_ServerTool]] = {}
        self._tree: TreeHandle | None = None
        self.commands = (self.mcp,)

    @classmethod
    def of(cls, loaded: LoadedConfig, cwd: Path) -> "Mcp":
        """The extension for the files `loaded` can read: `$TAU_HOME/mcp.json`, the trusted
        project's `.mcp.json` and `.tau/mcp.json` as approved, then each file `TAU_MCP_CONFIG`
        names. A file or server that cannot be used is skipped and named in `warnings`;
        `McpConfigError` only for a file `TAU_MCP_CONFIG` names that cannot be read."""
        problems: list[str] = []
        user = loaded.home / USER_FILE
        files: list[tuple[str, bytes | None]] = [
            (str(user), _read(user, named=False, problems=problems))
        ]
        project = loaded.project
        if project is not None:
            files += [(str(project.root / p), project.mcp_files.get(p)) for p in PROJECT_FILES]
        named = loaded.environ.get(CONFIG_VARIABLE, "")
        for path in (cwd / p for p in named.split(os.pathsep) if p):
            files.append((str(path), _read(path, named=True, problems=problems)))
        found, skipped = servers(files, loaded.environ)
        warnings = [f"warning: {p} (skipped)" for p in [*problems, *skipped]]
        start = loaded.catalog.timeouts.get(NAME, (None, None))[0]
        return cls(found, cwd, warnings=warnings, start_timeout=start)

    @property
    def tools(self) -> tuple[Tool, ...]:
        """Every running server's tools, in server name order."""
        return tuple(t for name in sorted(self._tools) for t in self._tools[name])

    async def start(self, tree: TreeHandle) -> None:
        """Start every server at once and list its tools, each within the start's time. A server
        that fails or is too slow, and a tool that cannot be named, are reported (stderr, and an
        `error` notice each that every client gets when it connects); what the configuration
        skipped is in `warnings`, which the start printed, and is told the same way. The rest
        start, and so does τ."""
        self._tree = tree
        for warning in self.warnings:  # the start printed them; clients hear them from here
            tree.tell(warning, level="error")
        tasks = {s.name: asyncio.ensure_future(self._start_one(s)) for s in self.servers}
        if tasks:
            _, slow = await asyncio.wait(tasks.values(), timeout=self._within)
            for task in slow:
                task.cancel()
            await asyncio.gather(*slow, return_exceptions=True)
        started: list[_Started] = []
        for server in self.servers:
            task = tasks[server.name]
            why = _why_not(task, server, self._within)
            if why is None:
                started.append(task.result())
            else:
                self.failed[server.name] = why
                self._report(f"{why}; τ starts without it")
        self._clients = {client.name: client for client, _ in started}
        self._tools = _named(started, self._report)
        self._listed = {c.name: tools for c, tools in started}
        for client, _ in started:
            client.on_change = lambda c=client: self._changed(c)
            client.on_exit = self._exited

    def _report(self, text: str) -> None:
        """Say `text` on stderr now, and as an `error` notice to every client (one told during
        `start` reaches each client when it connects)."""
        print(f"tau: mcp: {text}", file=sys.stderr)
        if self._tree is not None:
            self._tree.tell(text, level="error")

    async def _start_one(self, server: McpServer) -> "_Started":
        client = await McpClient.start(server, self._cwd, cancel_grace=_KILL_GRACE)
        try:
            return client, await client.tools()
        except McpError as e:
            await client.stop()
            raise McpStartError(f"MCP server {server.name}: {e}") from None
        except BaseException:
            await client.stop(grace=_KILL_GRACE)
            raise

    def _changed(self, client: McpClient) -> None:
        if self._tree is not None and client.name in self._clients:
            client.relisting = self._tree.spawn(self._relist(client))

    async def _relist(self, client: McpClient) -> None:
        """List `client`'s tools again and reassemble; a failure is the task's (an `error`
        notice from `mcp`)."""
        try:
            tools = await client.tools()
        finally:
            client.relisting = None
        listed = {**self._listed, client.name: tools}
        self._tools = _named(
            [(self._clients[n], ts) for n, ts in sorted(listed.items())], self._report
        )
        self._listed = listed
        assert self._tree is not None
        self._tree.reassemble()
        count = len(self._tools.get(client.name, []))
        self._tree.tell(f"MCP server {client.name} listed its tools again: {count} tools")

    def _exited(self, why: str) -> None:
        if self._tree is None:
            return
        try:
            self._tree.tell(f"{why}; its tools answer an error until τ restarts", level="error")
        except Exception as e:  # the handle is gone: say it where it can be seen
            print(f"tau: mcp: {why} ({type(e).__name__})", file=sys.stderr)

    async def stop(self) -> None:
        """Stop every server."""
        await self._stop_all()
        self._tree = None

    async def _stop_all(self) -> None:
        clients = list(self._clients.values())
        self._clients = {}
        self._listed = {}
        self._tools = {}
        await asyncio.gather(*(c.stop() for c in clients), return_exceptions=True)

    def mcp(self) -> str:
        """List the MCP servers, whether each runs, and their tools."""
        if not self.servers and not self.warnings:
            return "no MCP servers are configured (mcp.json in TAU_HOME, .mcp.json in a project)"
        lines: list[str] = []
        for server in self.servers:
            client = self._clients.get(server.name)
            if server.name in self.failed:
                state = f"failed: {self.failed[server.name]}"
            else:
                state = "not started" if client is None else (client.exited or "running")
            lines.append(f"{server.name} ({server.transport}: {server.shown()}): {state}")
            lines += [f"  {t.name}" for t in self._tools.get(server.name, [])]
        return "\n".join([*lines, *self.warnings])


def _deadline(start_timeout: float | None) -> float:
    """How long the servers may take to start: the extension's start timeout (D32) less what
    stopping the slow ones takes, so a slow server is mcp's to report, not a failed start."""
    whole = start_timeout or _default_start()
    return whole - _MARGIN if whole > 2 * _MARGIN else whole / 2


def _default_start() -> float:
    from tau._lifecycle import START_TIMEOUT  # noqa: PLC0415 - it imports the built-ins

    return START_TIMEOUT


def _why_not(task: "asyncio.Future[_Started]", server: McpServer, within: float) -> str | None:
    """Why `server` did not start (`None`: it did)."""
    if task.cancelled():
        return (
            f"MCP server {server.name} did not start within {within:g} s ({server.shown()}; a "
            "first `npx` or `uvx` run downloads its package: raise extensions.mcp.start_timeout)"
        )
    error = task.exception()
    if error is None:
        return None
    if isinstance(error, McpStartError):
        return str(error)
    return f"MCP server {server.name}: {type(error).__name__}: {error}"


def _named(
    listed: Sequence[tuple[McpClient, list[McpTool]]], report: Callable[[str], None]
) -> dict[str, list[_ServerTool]]:
    """Each server's tools under their τ names. A tool whose input schema is missing or not a
    JSON object, a name over 64 characters, or a second tool of one name, is left out, and
    `report`ed; the server's other tools are offered."""
    seen: dict[str, str] = {}
    by_server: dict[str, list[_ServerTool]] = {}
    for client, tools in listed:
        made = by_server.setdefault(client.name, [])
        for tool in tools:
            name = tau_name(client.name, tool.name)
            if tool.schema is None:
                report(
                    f"MCP server {client.name}: its tool {tool.name!r} has no input schema that "
                    "is a JSON object (inputSchema), so it is left out"
                )
            elif len(name) > NAME_LIMIT:
                report(
                    f"MCP server {client.name}: its tool {tool.name!r} would be named {name!r}, "
                    f"{len(name)} characters (the limit is {NAME_LIMIT}), so it is left out; "
                    "give the server a shorter name"
                )
            elif name in seen:
                report(
                    f"MCP server {client.name}: its tools {seen[name]!r} and {tool.name!r} are "
                    f"both {name!r}, so the second is left out"
                )
            else:
                seen[name] = tool.name
                made.append(_ServerTool(client, tool, name, tool.schema))
    return by_server
