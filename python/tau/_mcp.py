"""`Mcp`: the built-in extension, off by default, that gives the model the tools of the MCP
servers the user configured (D24 section 12; the files are `_mcp_config`'s).

With no server configured it starts nothing and offers nothing. Otherwise its `start` starts
every server at once (a stdio process, or an HTTP session), shakes hands and lists each one's
tools, and returns when all are ready, so the first step already has them. Each tool is offered
as `mcp__SERVER__TOOL` (characters outside `[A-Za-z0-9_-]` in the tool's name become `_`; at most
64 characters), described and typed as the server lists it, and goes through `permissions` like
any other (`mcp__SERVER` in a rule names every tool of that server).

**A server that fails to start stops τ** (Fail Early): the start fails with every failing server
named and why (with the last line its process wrote to stderr), and the others are stopped. A
start runs before any client is connected, so a notice would reach no one, and a session that
quietly lacks a server's tools misleads the model and the person alike. To start without it, fix
it, write `"disabled": true` on it (a nearer file may do so by name), or `tau ext disable mcp`. A
slow first start (`npx` downloading its package) is given `extensions.mcp.start_timeout` (D32);
when the start is cancelled for taking too long, the servers still starting are named on stderr.

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
import re
import sys
from collections.abc import Mapping, Sequence
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

__all__ = ["Mcp", "McpConfigError", "McpServer", "McpStartError", "tau_name"]


def tau_name(server: str, tool: str) -> str:
    """The τ name of an MCP tool: `mcp__SERVER__TOOL`, the tool's name reduced to
    `[A-Za-z0-9_-]`."""
    return f"{PREFIX}{server}__{re.sub(r'[^A-Za-z0-9_-]', '_', tool)}"


class _ServerTool:
    """One MCP tool as a τ tool."""

    def __init__(self, client: McpClient, tool: McpTool, name: str) -> None:
        self._client = client
        self._tool = tool.name
        self.name = name
        self.description = tool.description
        self.parameters: Mapping[str, object] = tool.schema

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


_Started = tuple[McpClient, list[McpTool]]
"""A server started, and the tools it listed."""


class Mcp:
    """The `mcp` built-in: the configured MCP servers' tools, while it is started."""

    name = NAME

    def __init__(self, servers: Sequence[McpServer], cwd: Path) -> None:
        self.servers = tuple(servers)
        self._cwd = cwd
        self._clients: dict[str, McpClient] = {}
        self._listed: dict[str, list[McpTool]] = {}  # by server, as each lists them
        self._tools: dict[str, list[_ServerTool]] = {}
        self._tree: TreeHandle | None = None
        self.commands = (self.mcp,)

    @classmethod
    def of(cls, loaded: LoadedConfig, cwd: Path) -> "Mcp":
        """The extension for the files `loaded` can read: `$TAU_HOME/mcp.json`, and the trusted
        project's `.mcp.json` and `.tau/mcp.json` as approved. `McpConfigError` naming every
        server that cannot be used."""
        user = loaded.home / USER_FILE
        try:
            raw = user.read_bytes()
        except FileNotFoundError:
            raw = None
        except OSError as e:
            raise McpConfigError(f"{user}: cannot be read ({e.strerror})") from None
        files: list[tuple[str, bytes | None]] = [(str(user), raw)]
        project = loaded.project
        if project is not None:
            files += [(str(project.root / p), project.mcp_files.get(p)) for p in PROJECT_FILES]
        return cls(servers(files, loaded.environ), cwd)

    @property
    def tools(self) -> tuple[Tool, ...]:
        """Every running server's tools, in server name order."""
        return tuple(t for name in sorted(self._tools) for t in self._tools[name])

    async def start(self, tree: TreeHandle) -> None:
        """Start every server at once and list its tools; `McpStartError` naming each that
        failed, with the others stopped."""
        self._tree = tree
        tasks = {s.name: asyncio.ensure_future(self._start_one(s)) for s in self.servers}
        try:
            if tasks:
                await asyncio.wait(tasks.values())
        except asyncio.CancelledError:
            await self._cancelled(tasks)
            raise
        started = [t.result() for t in tasks.values() if t.exception() is None]
        self._clients = {client.name: client for client, _ in started}
        try:
            failures = [str(t.exception()) for t in tasks.values() if t.exception() is not None]
            if failures:
                raise McpStartError("\n".join(failures))
            self._tools = _named(started)
        except BaseException:
            await self._stop_all()
            raise
        self._listed = {c.name: tools for c, tools in started}
        for client, _ in started:
            self._clients[client.name] = client
            client.on_change = lambda c=client: self._changed(c)
            client.on_exit = self._exited

    async def _cancelled(self, tasks: Mapping[str, "asyncio.Future[_Started]"]) -> None:
        """The start was cancelled (it took too long): say which servers were still starting,
        and stop every one."""
        slow = [s for s in self.servers if not tasks[s.name].done()]
        print(
            "tau: mcp: the start was cancelled with these servers still starting: "
            + "; ".join(f"{s.name} ({s.shown()})" for s in slow)
            + " (a first `npx` run downloads its package: raise extensions.mcp.start_timeout)",
            file=sys.stderr,
        )
        for task in tasks.values():
            task.cancel()
        done = await asyncio.gather(*tasks.values(), return_exceptions=True)
        clients = [d[0] for d in done if not isinstance(d, BaseException)]
        await asyncio.gather(*(c.stop() for c in clients), return_exceptions=True)

    async def _start_one(self, server: McpServer) -> "_Started":
        client = await McpClient.start(server, self._cwd)
        try:
            return client, await client.tools()
        except McpError as e:
            await client.stop()
            raise McpStartError(f"MCP server {server.name}: {e}") from None
        except BaseException:
            await client.stop()
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
        self._tools = _named([(self._clients[n], ts) for n, ts in sorted(listed.items())])
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
        if not self.servers:
            return "no MCP servers are configured (mcp.json in TAU_HOME, .mcp.json in a project)"
        lines: list[str] = []
        for server in self.servers:
            client = self._clients.get(server.name)
            state = "not started" if client is None else (client.exited or "running")
            lines.append(f"{server.name} ({server.transport}: {server.shown()}): {state}")
            lines += [f"  {t.name}" for t in self._tools.get(server.name, [])]
        return "\n".join(lines)


def _named(listed: Sequence[tuple[McpClient, list[McpTool]]]) -> dict[str, list[_ServerTool]]:
    """Each server's tools under their τ names; `McpStartError` for a name over 64 characters
    or two tools of one name."""
    seen: dict[str, str] = {}
    by_server: dict[str, list[_ServerTool]] = {}
    for client, tools in listed:
        made = by_server.setdefault(client.name, [])
        for tool in tools:
            name = tau_name(client.name, tool.name)
            if len(name) > NAME_LIMIT:
                raise McpStartError(
                    f"MCP server {client.name}: its tool {tool.name!r} would be named {name!r}, "
                    f"{len(name)} characters (the limit is {NAME_LIMIT}); give the server a "
                    "shorter name"
                )
            if name in seen:
                raise McpStartError(
                    f"MCP server {client.name}: its tools {seen[name]!r} and {tool.name!r} are "
                    f"both {name!r}"
                )
            seen[name] = tool.name
            made.append(_ServerTool(client, tool, name))
    return by_server
