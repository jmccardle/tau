"""The HTTP edge (D29): the wire of `tau._serve` over HTTP/1.1, for a client that cannot dial the
Unix socket (a browser, an editor's webview, another host, a container).

The messages are the socket's, one JSON-RPC message each; only the framing differs:

- `GET /stream` opens a **stream**: a `text/event-stream` (SSE) response that stays open. Its first
  event is `event: connection` whose data is the **connection id**; after it, each `data:` line is
  one message the socket would have sent (a response, an `event` or `end`), in the socket's order.
- `POST /stream/<connection id>` carries one request, as the body. It is answered `202 Accepted`
  once the request is queued on that stream (its response arrives on the stream), and `404` with a
  JSON-RPC error body if no open stream has that id. `hello` is the first request, as on the socket.
- `GET /stream/<connection id>` is refused (409): there is no resume. A new stream is a new
  `hello` and a fresh snapshot.
- `POST /shutdown` asks the server to stop as SIGTERM stops it (`tau serve --stop`; D29 section
  10). It is answered `202 Accepted` once the stop is asked for, and `404` by a server that is not
  stopped this way (one a program embeds). It is not a wire message: the wire is the session's.

- Every other `GET` (or `HEAD`) is the **web client**'s (`tau._web_client`): its static files,
  or the help page at `/` when none is installed. No token: they are the same for anyone, and
  hold nothing of the session.

**Access.** Every request to the wire but a CORS preflight carries `Authorization: Bearer
<token>` (401 without it). A request that carries an `Origin` header is refused (403) unless it
is the edge's own origin (its served client, calling home) or the edge's `origins` lists it, so a
web page the user merely visits cannot drive the session.

**Liveness.** A stream says `: ping` every `heartbeat` seconds. A write that fails, or the client
closing its end, is a disconnect: the stream's `LocalClient` closes, so its questions go to the
pool, and the server counts it as gone for its idle exit. A client that vanishes without closing
(its machine went away) is found by the ping: where the platform has `TCP_USER_TIMEOUT` (Linux),
a ping not acknowledged within two heartbeats fails the connection; elsewhere the kernel's own
retransmission limit does, later.

A stream is the server's "line": `recv`, `send`, `close` and `async for`, raising websockets'
`ConnectionClosed` when it is gone, as a WebSocket does, so `tau._serve` has one rule for both.
"""

import asyncio
import contextlib
import errno
import hmac
import ipaddress
import json
import secrets
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

import h11
from websockets.exceptions import ConnectionClosed

from tau._protocol import CODES
from tau._web_client import HEADERS, Page, web_client

HEARTBEAT: Final = 15.0
"""Seconds between a stream's `: ping` comments."""
SOFT_PORT: Final = 3393
"""The port an edge with no port asked for tries first ("ffwf" on a phone keypad)."""
MAX_BODY: Final = 64 * 1024 * 1024
"""The largest request body a POST may carry (413 past it)."""
STREAM: Final = "/stream"
SHUTDOWN: Final = "/shutdown"
READ_SECONDS: Final = 30.0
"""How long a request's head, or its body, may take to arrive (and how long a closing edge waits
for its connections)."""
_READ: Final = 65536
_LOCALHOST: Final = "localhost"


def is_loopback(host: str) -> bool:
    """Whether `host` names this machine only: `localhost` or a loopback address."""
    if host == _LOCALHOST:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class HttpEdge:
    """Where and how a `Server` listens over HTTP (D29). `port` `None` tries 3393 and then any free
    port; `0` takes any free port; any other port is bound or the start fails. `token` is the
    bearer token every request must carry (`None`: the server makes one). `origins` are the web
    origins allowed to call (none by default). `heartbeat` is the seconds between pings. A `host`
    that is not loopback needs `allow_network`: `ValueError` otherwise. `web_root` is a built web
    client's directory to serve (`None`: the installed `tau_web`'s, else the help page, which says
    the install is broken). `web` off serves no client, only a page saying so (`ValueError`
    with a `web_root`)."""

    host: str = "127.0.0.1"
    port: int | None = None
    token: str | None = None
    origins: tuple[str, ...] = ()
    heartbeat: float = HEARTBEAT
    allow_network: bool = False
    web_root: Path | None = None
    web: bool = True

    def __post_init__(self) -> None:
        if not self.web and self.web_root is not None:
            raise ValueError("a web root with the web client off: give one or the other")
        if self.port is not None and not 0 <= self.port <= 65535:  # noqa: PLR2004 - TCP's range
            raise ValueError(f"port {self.port} is not a TCP port (0 to 65535)")
        if self.heartbeat <= 0:
            raise ValueError(f"a heartbeat of {self.heartbeat} s is not a positive interval")
        if self.token == "":
            raise ValueError("an empty token would let anyone in")
        if not self.allow_network and not is_loopback(self.host):
            raise ValueError(
                f"{self.host} is not a loopback address: listening beyond this machine needs "
                "--allow-network"
            )


class Stream:
    """One SSE stream: the server's line to one client (the module docstring)."""

    def __init__(
        self, id: str, conn: h11.Connection, writer: asyncio.StreamWriter, heartbeat: float
    ) -> None:
        self.id = id
        self._conn = conn
        self._writer = writer
        self._heartbeat = heartbeat
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self.gone = False

    # What the server's connection uses

    async def recv(self) -> str:
        """The next request the client POSTed. `ConnectionClosed` once the stream is gone."""
        text = await self._inbox.get()
        if text is None:
            self._inbox.put_nowait(None)  # every later `recv` is told too
            raise ConnectionClosed(None, None)
        return text

    def __aiter__(self) -> AsyncIterator[str]:
        return self._each()

    async def _each(self) -> AsyncIterator[str]:
        while True:
            try:
                yield await self.recv()
            except ConnectionClosed:
                return

    async def send(self, text: str) -> None:
        """Send one message as one SSE event. `ConnectionClosed` if the stream is gone or the
        write fails (which makes it gone)."""
        await self._write(f"data: {text}\n\n")

    async def close(self, code: int = 1000) -> None:
        """End the response and close the connection. `code` is the WebSocket close code the
        socket's line takes; HTTP has nothing to say it with."""
        del code
        if not self.gone:
            with contextlib.suppress(ConnectionClosed):
                await self._write_raw(self._conn.send(h11.EndOfMessage()))
        self._end(abort=False)

    # The edge's side

    def offer(self, text: str) -> bool:
        """Queue a POSTed request; `False` if the stream is gone."""
        if self.gone:
            return False
        self._inbox.put_nowait(text)
        return True

    async def run(
        self, reader: asyncio.StreamReader, handle: "Callable[[Stream], Awaitable[None]]"
    ) -> None:
        """Open the stream (the headers, then the connection id), then `handle` it to its end,
        pinging while it runs and watching for the client to hang up."""
        await self._write(f"event: connection\ndata: {self.id}\n\n")
        pinging = asyncio.create_task(self._ping())
        watching = asyncio.create_task(self._watch(reader))
        try:
            await handle(self)
        finally:
            pinging.cancel()
            watching.cancel()
            await asyncio.gather(pinging, watching, return_exceptions=True)
            self.drop()

    async def _ping(self) -> None:
        """A comment every `heartbeat` seconds: a write that fails, or that the client does not
        take within two heartbeats, is the disconnect it finds."""
        with contextlib.suppress(ConnectionClosed):
            while not self.gone:
                await asyncio.sleep(self._heartbeat)
                await self._write(": ping\n\n")

    async def _watch(self, reader: asyncio.StreamReader) -> None:
        """The client sends nothing more on a stream's connection: its end of file (or a reset)
        is a disconnect."""
        with contextlib.suppress(OSError):
            while await reader.read(_READ):
                pass
        self.drop()

    async def _write(self, chunk: str) -> None:
        await self._write_raw(self._conn.send(h11.Data(data=chunk.encode())))

    async def _write_raw(self, data: bytes | None) -> None:
        if self.gone:
            raise ConnectionClosed(None, None)
        async with self._lock:
            try:
                if data:
                    self._writer.write(data)
                async with asyncio.timeout(self._heartbeat * 2):
                    await self._writer.drain()
            except (OSError, RuntimeError, TimeoutError):  # reset, broken pipe, closing, stalled
                self.drop()
                raise ConnectionClosed(None, None) from None

    def drop(self) -> None:
        """The stream is lost (idempotent): `recv` raises, `offer` refuses, and the connection is
        aborted, dropping what it had not sent (a client that stopped reading must not hold the
        server's shutdown)."""
        self._end(abort=True)

    def _end(self, *, abort: bool) -> None:
        if self.gone:
            return
        self.gone = True
        self._inbox.put_nowait(None)
        if abort:
            self._writer.transport.abort()
        else:
            self._writer.close()


def _bound_unacknowledged(writer: asyncio.StreamWriter, heartbeat: float) -> None:
    """Where the platform can: a write the client has not acknowledged within two heartbeats
    fails the connection, so a ping finds a client that vanished without closing."""
    option = getattr(socket, "TCP_USER_TIMEOUT", None)
    peer = writer.get_extra_info("socket")
    if option is None or peer is None:
        return
    with contextlib.suppress(OSError):
        peer.setsockopt(socket.IPPROTO_TCP, option, max(1, int(heartbeat * 2000)))


@dataclass(frozen=True)
class _Request:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes


class _Refusal(Exception):
    """A request answered with `status` and a JSON `body`, without reaching a stream."""

    def __init__(
        self, status: int, body: object, headers: tuple[tuple[str, str], ...] = ()
    ) -> None:
        super().__init__(status)
        self.status = status
        self.body = body
        self.headers = headers


def _no_stream(id: str, body: bytes) -> _Refusal:
    """The 404 for a POST to a stream that is not open: a JSON-RPC error, with the request's id
    if the body has one, so a client that matched ids can fail the call it made."""
    request_id: object = None
    with contextlib.suppress(ValueError, AttributeError, UnicodeDecodeError):
        request_id = json.loads(body).get("id")
    error = {
        "code": CODES["InvalidRequest"],
        "message": f"no open stream {id!r}: it closed, or never was; open one with GET {STREAM}",
    }
    return _Refusal(404, {"jsonrpc": "2.0", "id": request_id, "error": error})


def _error(status: int, message: str, *headers: tuple[str, str]) -> _Refusal:
    return _Refusal(status, {"error": message}, headers)


class Edge:
    """A listening HTTP edge: `url` is where it listens, `token` what it lets in. `close` stops
    listening and closes every connection; the streams' clients close with them."""

    def __init__(
        self,
        edge: HttpEdge,
        token: str,
        handle: Callable[[Stream], Awaitable[None]],
        stop: Callable[[], None] | None = None,
    ) -> None:
        self._edge = edge
        self._stop = stop
        self._web = web_client(edge.web_root, web=edge.web)
        self._own: frozenset[str] = frozenset()
        self.token = token
        self._handle = handle
        self._streams: dict[str, Stream] = {}
        self._writers: set[asyncio.StreamWriter] = set()
        self._server: asyncio.Server | None = None
        self.host = "127.0.0.1" if edge.host == _LOCALHOST else edge.host
        """The address bound: `localhost` is bound as 127.0.0.1, so the URL names one port."""
        self.port = 0

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}/"

    async def listen(self) -> None:
        """Bind as `HttpEdge.port` says. `OSError` naming the address if it cannot be bound."""
        wanted = self._edge.port
        if wanted is None:
            try:
                self._server = await self._bind(SOFT_PORT)
            except OSError as e:
                if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                    raise
                self._server = await self._bind(0)
        else:
            self._server = await self._bind(wanted)
        self.port = self._server.sockets[0].getsockname()[1]
        self._own = _own_origins(self.host, self.port)

    async def _bind(self, port: int) -> asyncio.Server:
        try:
            return await asyncio.start_server(self._client, self.host, port)
        except OSError as e:
            raise OSError(e.errno, f"cannot listen on {self.host}:{port}: {e.strerror or e}") from e

    def stop_accepting(self) -> None:
        """No new connection; the open ones go on. Idempotent."""
        if self._server is not None:
            self._server.close()

    async def close(self) -> None:
        """Stop listening and abort every connection still open (what a stream had to say was
        said at the server's `close`, within its grace)."""
        self.stop_accepting()
        for stream in list(self._streams.values()):
            stream.drop()
        for writer in list(self._writers):
            writer.transport.abort()
        if self._server is not None:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(READ_SECONDS):
                    await self._server.wait_closed()

    # One TCP connection: requests one after another, until one becomes a stream

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = h11.Connection(h11.SERVER)
        self._writers.add(writer)
        try:
            while True:
                async with asyncio.timeout(READ_SECONDS):
                    request = await self._head(conn, reader)
                if request is None:
                    return
                stream = await self._serve(conn, reader, writer, request)
                if stream is not None:
                    await self._stream(stream, reader)
                    return
                if conn.our_state is not h11.DONE or conn.their_state is not h11.DONE:
                    return
                conn.start_next_cycle()
        except h11.RemoteProtocolError as e:
            await self._malformed(conn, writer, e.error_status_hint)
        except (OSError, TimeoutError, ConnectionClosed, h11.ProtocolError):
            return
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _head(self, conn: h11.Connection, reader: asyncio.StreamReader) -> _Request | None:
        """The next request's head (its body not read yet), or `None` when the client hung up
        between requests."""
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                conn.receive_data(await reader.read(_READ))
            elif isinstance(event, h11.Request):
                headers = {k.decode().lower(): v.decode("latin-1") for k, v in event.headers}
                return _Request(event.method.decode(), event.target.decode(), headers, b"")
            else:  # ConnectionClosed
                return None

    async def _body(self, conn: h11.Connection, reader: asyncio.StreamReader) -> bytes:
        """The rest of the request: its body. `_Refusal` (413) past `MAX_BODY`."""
        body = bytearray()
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                conn.receive_data(await reader.read(_READ))
            elif isinstance(event, h11.Data):
                body += event.data
                if len(body) > MAX_BODY:
                    raise _error(413, "the body is too large")
            elif isinstance(event, h11.EndOfMessage):
                return bytes(body)
            else:  # the client hung up mid-request
                raise ConnectionClosed(None, None)

    async def _malformed(
        self, conn: h11.Connection, writer: asyncio.StreamWriter, status: int
    ) -> None:
        """A request h11 cannot read: `status` (400, or 431 for headers too large), if a
        response can still be sent."""
        with contextlib.suppress(OSError, h11.ProtocolError):
            await self._send(conn, writer, _error(status, "not a request this edge can read"), None)

    async def _serve(
        self,
        conn: h11.Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: _Request,
    ) -> Stream | None:
        """Answer `request`; the `Stream` it opened, if it opened one (its headers are sent). The
        origin and the token are checked on the head: a refused request's body is never read."""
        origin = request.headers.get("origin")
        wire = _is_wire(request.target)
        try:
            self._allowed(origin)
            if wire and request.method != "OPTIONS":
                self._authorized(request)
            async with asyncio.timeout(READ_SECONDS):
                request = replace(request, body=await self._body(conn, reader))
            if request.method == "OPTIONS":
                await self._send(conn, writer, None, origin, preflight=True)
                return None
            if not wire:
                await self._page(conn, writer, request, origin)
                return None
            return await self._route(conn, writer, request, origin)
        except _Refusal as refusal:
            await self._send(conn, writer, refusal, origin)
            return None

    def _allowed(self, origin: str | None) -> None:
        if origin is not None and origin not in self._own and origin not in self._edge.origins:
            raise _error(403, f"requests from origin {origin!r} are refused (--allow-origin)")

    def _authorized(self, request: _Request) -> None:
        given = request.headers.get("authorization", "")
        scheme, _, token = given.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.strip().encode(), self.token.encode()
        ):
            raise _error(401, "a bearer token is required", ("WWW-Authenticate", "Bearer"))

    async def _route(
        self,
        conn: h11.Connection,
        writer: asyncio.StreamWriter,
        request: _Request,
        origin: str | None,
    ) -> Stream | None:
        path = request.target.split("?", 1)[0]
        if path == STREAM:
            if request.method != "GET":
                raise _error(405, f"open a stream with GET {STREAM}", ("Allow", "GET"))
            return await self._open(conn, writer, origin)
        if path.startswith(STREAM + "/") and path.count("/") == 2:  # noqa: PLR2004 - /stream/<id>
            id = path.removeprefix(STREAM + "/")
            if request.method == "GET":
                raise _error(409, "there is no resume: open a new stream and say hello again")
            if request.method != "POST":
                raise _error(405, "send a request with POST", ("Allow", "GET, POST"))
            self._post(id, request.body)
            await self._send(conn, writer, _Refusal(202, None), origin)
            return None
        if path == SHUTDOWN:
            self._shutdown(request)
            await self._send(conn, writer, _Refusal(202, None), origin)
            return None
        raise _error(404, f"no such path; the wire is at {STREAM}")

    def _shutdown(self, request: _Request) -> None:
        """Ask the server to stop (`POST /shutdown`), or refuse."""
        if request.method != "POST":
            raise _error(405, f"stop the server with POST {SHUTDOWN}", ("Allow", "POST"))
        if self._stop is None:
            raise _error(404, "this server is not stopped over HTTP: its program stops it")
        self._stop()

    async def _page(
        self,
        conn: h11.Connection,
        writer: asyncio.StreamWriter,
        request: _Request,
        origin: str | None,
    ) -> None:
        """The web client's file (or the help page) for a GET; its headers alone for a HEAD."""
        if request.method not in ("GET", "HEAD"):
            raise _error(405, "the web client's files are read with GET", ("Allow", "GET, HEAD"))
        page: Page = self._web.page(request.target)
        headers = [("Content-Type", page.content_type), *HEADERS, *self._cors(origin)]
        body = b"" if request.method == "HEAD" else page.body
        await _respond(conn, writer, page.status, headers, body, len(page.body))

    def _post(self, id: str, body: bytes) -> None:
        stream = self._streams.get(id)
        try:
            text = body.decode()
        except UnicodeDecodeError:
            raise _error(400, "a request is UTF-8 JSON") from None
        if stream is None or not stream.offer(text):
            raise _no_stream(id, body)

    async def _open(
        self, conn: h11.Connection, writer: asyncio.StreamWriter, origin: str | None
    ) -> Stream:
        headers = [
            ("Content-Type", "text/event-stream"),
            ("Cache-Control", "no-cache"),
            ("X-Accel-Buffering", "no"),
            *self._cors(origin),
        ]
        writer.write(conn.send(h11.Response(status_code=200, headers=headers)))
        await writer.drain()
        _bound_unacknowledged(writer, self._edge.heartbeat)
        id = secrets.token_urlsafe(12)
        stream = Stream(id, conn, writer, self._edge.heartbeat)
        self._streams[id] = stream
        return stream

    async def _stream(self, stream: Stream, reader: asyncio.StreamReader) -> None:
        try:
            await stream.run(reader, self._handle)
        finally:
            self._streams.pop(stream.id, None)

    def _cors(self, origin: str | None) -> list[tuple[str, str]]:
        """The CORS headers for an allowed `origin` (none for a request without one)."""
        if origin is None:
            return []
        return [("Access-Control-Allow-Origin", origin), ("Vary", "Origin")]

    async def _send(
        self,
        conn: h11.Connection,
        writer: asyncio.StreamWriter,
        refusal: _Refusal | None,
        origin: str | None,
        *,
        preflight: bool = False,
    ) -> None:
        """One whole response: `refusal`'s status and JSON body, or an empty 204 (a preflight)."""
        status = 204 if refusal is None else refusal.status
        body = b"" if refusal is None or refusal.body is None else json.dumps(refusal.body).encode()
        headers = [*self._cors(origin), *(() if refusal is None else refusal.headers)]
        if preflight:
            headers += [
                ("Access-Control-Allow-Methods", "GET, POST"),
                ("Access-Control-Allow-Headers", "Authorization, Content-Type"),
                ("Access-Control-Max-Age", "600"),
            ]
        if body:
            headers.append(("Content-Type", "application/json"))
        await _respond(conn, writer, status, headers, body, len(body))


async def _respond(  # noqa: PLR0913, PLR0917 - a response, whole
    conn: h11.Connection,
    writer: asyncio.StreamWriter,
    status: int,
    headers: list[tuple[str, str]],
    body: bytes,
    length: int,
) -> None:
    """Send one whole response (`length` is the body's, which a HEAD's empty `body` is not)."""
    if conn.our_state not in (h11.IDLE, h11.SEND_RESPONSE):  # IDLE: a request h11 refused
        return
    headers = [*headers, ("Content-Length", str(length))]
    writer.write(conn.send(h11.Response(status_code=status, headers=headers)))
    if body:
        writer.write(conn.send(h11.Data(data=body)))
    writer.write(conn.send(h11.EndOfMessage()))
    await writer.drain()


def _is_wire(target: str) -> bool:
    """Whether a request target is the wire's (`/stream`, `/stream/<id>`) or `/shutdown`, which
    need the token."""
    path = target.split("?", 1)[0]
    return path in {STREAM, SHUTDOWN} or path.startswith(STREAM + "/")


def _own_origins(host: str, port: int) -> frozenset[str]:
    """The origins a page the edge itself served has: its URL's, and on loopback the other
    loopback names a browser may have used for the same socket."""
    names = {f"[{host}]" if ":" in host else host}
    if is_loopback(host):
        names |= {"127.0.0.1", "localhost"}
    return frozenset(f"http://{name}:{port}" for name in names)


async def listen(
    edge: HttpEdge,
    handle: Callable[[Stream], Awaitable[None]],
    stop: Callable[[], None] | None = None,
) -> Edge:
    """An `Edge` for `edge`, listening, whose streams `handle` serves, and whose `POST /shutdown`
    calls `stop` (`None`: refused, 404). Its token is `edge.token` or a new random one."""
    listening = Edge(edge, edge.token or secrets.token_urlsafe(32), handle, stop)
    await listening.listen()
    return listening
