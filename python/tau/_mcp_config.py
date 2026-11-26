"""The MCP servers `mcp` starts (D24 section 12): the files, their shape, and `${VAR}` in them.

The shape is the one Claude Code's `.mcp.json` and most MCP clients write:

    {"mcpServers": {"NAME": {"command": "npx", "args": ["-y", "pkg"], "env": {"K": "V"}},
                    "NAME2": {"type": "http", "url": "https://...", "headers": {"K": "V"}}}}

A server with a `command` is a stdio server (`type` `"stdio"`, or none); one with a `url` is a
streamable HTTP server (`type` `"http"`, or none). `"disabled": true` turns a server off, and
needs nothing else: written in a nearer file, it turns off a server of the same name in a farther
one. Other keys (other clients' `timeout`, `alwaysAllow`, ...) are ignored, so a file written for
another client loads as it is; a key this reader knows with the wrong type is a problem.

Where: `$TAU_HOME/mcp.json` (the user's), then the trusted project's `.mcp.json` (Claude Code's
file, at the project's root) and `.tau/mcp.json`, read from the bytes trust approved. For one
name the nearest wins whole: `.tau/mcp.json` over `.mcp.json` over the user's.

`${VAR}` and `${VAR:-default}` in `command`, `args`, `env` values, `url` and `headers` values are
replaced from the environment τ reads keys from (the shell's over a trusted project's `.env`), so
a token stays out of the file. Its value is never shown, nor is any `env` or `headers` value: a
report names them only.

**What cannot be used is skipped, with a warning** (as `skills` skips a file): a file that is not
JSON, or whose `mcpServers` is not an object, is left out whole; a server with a bad name, a
missing `command` or `url`, a `${VAR}` that is not set and has no default, or the SSE transport is
left out alone (and still hides a farther file's server of its name). `servers` returns the
problems, each naming the file and the server; `mcp` prints them at its start and tells clients.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

USER_FILE = "mcp.json"
"""The user's file, in `TAU_HOME`."""
PROJECT_FILES = (".mcp.json", ".tau/mcp.json")
"""The trusted project's files, relative to its root, farthest first (the last wins a name)."""
KEY = "mcpServers"

_NAME = re.compile(r"[A-Za-z0-9_-]+")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class McpConfigError(ValueError):
    """An MCP file or server that cannot be used: the message names the file and the
    server (several lines when several are wrong)."""


@dataclass(frozen=True)
class McpServer:
    """One server to start: its `name` (the `mcp__NAME__` of its tools), the file it is from
    (`origin`), its `transport`, and for stdio the `command`, `args` and `env` (its environment,
    over the few variables it inherits from τ's), for HTTP the `url` and `headers`. `${VAR}`s
    are replaced. `env` and `headers` are left out of its repr: they may hold a token."""

    name: str
    origin: str
    transport: Literal["stdio", "http"]
    command: str = ""
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict[str, str], repr=False)
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict[str, str], repr=False)

    def shown(self) -> str:
        """What it runs or reaches, for a person: the command and its arguments, or the URL."""
        if self.transport == "http":
            return self.url
        return " ".join([self.command, *self.args])


Servers = dict[str, Mapping[str, object]]
"""A file's servers by name, each as written."""


def servers_in(raw: bytes, origin: str, problems: list[str]) -> Servers:
    """The `mcpServers` of a file's bytes, by name; `McpConfigError` naming `origin` for a file
    that is not JSON, not an object, or whose `mcpServers` is not an object. A server whose name
    is not ASCII letters, digits, `_` and `-`, or that is not an object, is left out, and
    `problems` says so. A file without `mcpServers` has none."""
    try:
        data: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise McpConfigError(f"{origin}: not a JSON file ({e})") from None
    if not isinstance(data, dict):
        raise McpConfigError(f"{origin}: must hold a JSON object with {KEY!r}")
    servers = cast("dict[str, object]", data).get(KEY, {})
    if not isinstance(servers, dict):
        raise McpConfigError(f"{origin}: {KEY!r} must be an object of servers by name")
    found: Servers = {}
    for name, spec in cast("dict[str, object]", servers).items():
        if not _NAME.fullmatch(name) or "__" in name:
            problems.append(
                f"{origin}: MCP server name {name!r} must be ASCII letters, digits, _ and -, "
                "without `__` (it separates the server from the tool in `mcp__SERVER__TOOL`)"
            )
        elif not isinstance(spec, dict):
            problems.append(f"{origin}: MCP server {name!r} must be an object")
        else:
            found[name] = cast("dict[str, object]", spec)
    return found


Files = Sequence[tuple[str, bytes | None]]
"""`(origin, bytes)` of each file, farthest first; `None` is no file."""


def merged(files: Files, problems: list[str]) -> dict[str, tuple[str, Mapping[str, object]]]:
    """Each server's settings and the file it is from, the last file winning a name, in name
    order; disabled ones included. `files` are `(origin, bytes)`, farthest first; `None` is no
    file. A file that cannot be read is left out whole, and `problems` says why."""
    by_name: dict[str, tuple[str, Mapping[str, object]]] = {}
    for origin, raw in files:
        if raw is None:
            continue
        try:
            by_name.update({n: (origin, e) for n, e in servers_in(raw, origin, problems).items()})
        except McpConfigError as e:
            problems.append(str(e))
    return dict(sorted(by_name.items()))


def servers(files: Files, environ: Mapping[str, str]) -> tuple[list[McpServer], list[str]]:
    """The servers to start from `files` (`(origin, bytes)`, farthest first), in name order, with
    their `${VAR}`s replaced from `environ`, and why each file or server that cannot be used is
    left out (the file, or the server, named in each). A disabled server is left out silently. A
    server that cannot be used still hides a farther file's server of its name."""
    found: list[McpServer] = []
    problems: list[str] = []
    for name, (origin, spec) in merged(files, problems).items():
        try:
            server = _server(name, origin, spec, environ)
        except McpConfigError as e:
            problems.append(str(e))
            continue
        if server is not None:
            found.append(server)
    return found, problems


def _server(
    name: str, origin: str, spec: Mapping[str, object], environ: Mapping[str, str]
) -> McpServer | None:
    where = f"{origin}: MCP server {name!r}"
    disabled = spec.get("disabled", False)
    if not isinstance(disabled, bool):
        raise McpConfigError(f"{where}: 'disabled' must be true or false")
    if disabled:
        return None
    kind = spec.get("type")
    if kind is None:
        kind = "http" if "url" in spec and "command" not in spec else "stdio"
    if kind == "sse":
        raise McpConfigError(
            f"{where}: the SSE transport (type 'sse') is not supported; use its streamable "
            "HTTP endpoint (type 'http'), or turn it off with \"disabled\": true"
        )
    if kind not in ("stdio", "http"):
        raise McpConfigError(f"{where}: unknown type {kind!r} (stdio or http)")

    if kind == "http":
        return _http(name, origin, spec, environ)
    return _stdio(name, origin, spec, environ)


def _http(
    name: str, origin: str, spec: Mapping[str, object], environ: Mapping[str, str]
) -> McpServer:
    where = f"{origin}: MCP server {name!r}"
    url = _expand(_text(spec, "url", where), environ, where)
    if not url.startswith(("http://", "https://")):
        raise McpConfigError(f"{where}: 'url' must be an http(s) URL")
    headers = {k: _expand(v, environ, where) for k, v in _strings(spec, "headers", where).items()}
    return McpServer(name, origin, "http", url=url, headers=headers)


def _stdio(
    name: str, origin: str, spec: Mapping[str, object], environ: Mapping[str, str]
) -> McpServer:
    where = f"{origin}: MCP server {name!r}"
    command = _expand(_text(spec, "command", where), environ, where)
    args = spec.get("args", [])
    if not isinstance(args, list) or not all(
        isinstance(a, str) for a in cast("list[object]", args)
    ):
        raise McpConfigError(f"{where}: 'args' must be a list of strings")
    env = {k: _expand(v, environ, where) for k, v in _strings(spec, "env", where).items()}
    words = tuple(_expand(a, environ, where) for a in cast("list[str]", args))
    return McpServer(name, origin, "stdio", command=command, args=words, env=env)


def _text(spec: Mapping[str, object], key: str, where: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value:
        raise McpConfigError(f"{where}: needs {key!r}, a non-empty string")
    return value


def _strings(spec: Mapping[str, object], key: str, where: str) -> dict[str, str]:
    value = spec.get(key, {})
    if not isinstance(value, dict) or not all(
        isinstance(v, str) for v in cast("dict[str, object]", value).values()
    ):
        raise McpConfigError(f"{where}: {key!r} must be an object of strings")
    return cast("dict[str, str]", value)


def _expand(text: str, environ: Mapping[str, str], where: str) -> str:
    """`text` with each `${VAR}` and `${VAR:-default}` replaced; a `VAR` that is not set and
    has no default is `McpConfigError` naming it (never a value)."""

    def one(match: re.Match[str]) -> str:
        var, default = match.group(1), match.group(2)
        if var in environ:
            return environ[var]
        if default is not None:
            return default
        raise McpConfigError(
            f"{where}: ${{{var}}} is not set (set it in the environment or the project's .env, "
            f"or write ${{{var}:-default}})"
        )

    return _VAR.sub(one, text)


def describe(raw: bytes, origin: str) -> list[str]:
    """A file's servers for the trust report, one line each: the name and what it runs or
    reaches, as written (no `${VAR}` replaced), with the names of its `env` or `headers` and
    never their values; or the one line that says why the file cannot be read."""
    try:
        problems: list[str] = []
        found = servers_in(raw, origin, problems)
    except McpConfigError as e:
        return [f"{e} (mcp will skip it, with a warning)"]
    lines: list[str] = []
    for name, spec in found.items():
        if spec.get("disabled") is True:
            lines.append(f"{name}: disabled")
            continue
        target = spec.get("url") if "url" in spec and "command" not in spec else None
        if target is None:
            args = spec.get("args", [])
            words = [str(a) for a in cast("list[object]", args)] if isinstance(args, list) else []
            target = " ".join([str(spec.get("command", "")), *words])
        named = spec.get("headers" if "url" in spec else "env")
        keys = sorted(cast("dict[str, object]", named)) if isinstance(named, dict) else []
        suffix = f"  (sets {', '.join(keys)})" if keys else ""
        lines.append(f"{name}: {target}{suffix}")
    lines += [f"{p} (mcp will skip it, with a warning)" for p in problems]
    return lines or ["(no servers)"]
