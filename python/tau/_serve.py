"""The server (D21 sections 1-3, 6): one session's `Driver` on a Unix socket, one `LocalClient`
per connection, JSON-RPC 2.0 over WebSocket.

`Server.start` binds the socket and serves until `close`. A connection says `hello` (the protocol
it speaks, its client id and the bound of its event stream), and is then a `LocalClient` of the
driver: each request is the client call of the same name, run on the loop; each event the
client is sent is a notification `event`; the stream's end is the notification `end`, after
which the server closes. Nothing here describes a value or an error: `tau._protocol` does.

**Order.** Calls begin in the order they arrive, each running to its first wait before the next
begins, then run concurrently. One writer per connection sends everything: before it sends a
response it sends every event the client's stream holds, and a call publishes what it changed
before it returns, so a response arrives after the events its call caused.

**Backpressure** is the stream's: the writer waits on the socket while events stay in the
client's `Feed`, whose bounds decide (`Live` dropped, an `Appended` or `Notice` backlog cut off,
sent as `end`). The server adds no queue, and the driver never waits on a socket.

A connection is not a client: closing it closes its `LocalClient`, and the driver tells the
questions of a client that has no stream left to the pool. A call it began keeps running, and
its response is dropped.
"""

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Final, cast

from websockets.asyncio.server import Server as _Sockets
from websockets.asyncio.server import ServerConnection, unix_serve
from websockets.exceptions import ConnectionClosed

from tau._protocol import (
    CODES,
    METHODS,
    PROTOCOL,
    ProtocolMismatch,
    Welcome,
    decode_params,
    encode_event,
    encode_result,
    end_params,
    error_object,
)
from tau._values import Json
from tau.client import LocalClient
from tau.driver import Driver
from tau.events import Event

BUFFER: Final = 256
"""The bound of a client's event stream when its `hello` names none."""

GRACE: Final = 5.0
"""How long `Server.close` lets connections say `end` before it closes them."""

POLL: Final = 0.25
"""How often the server looks at the driver, to see that it has failed or gone idle."""

Id = int | str


class _Refused(Exception):
    """A message the server answers with an error: `error` is the error object, `id` the
    request's (`None` if it could not be read)."""

    def __init__(self, error: Json, id: Id | None = None) -> None:
        super().__init__(error["message"])
        self.error = error
        self.id = id


def _refuse(name: str, message: str, id: Id | None = None) -> _Refused:
    """The refusal for an error the server sends for its own reasons, by its name in the table."""
    return _Refused({"code": CODES[name], "message": message}, id)


@dataclass(frozen=True)
class _Request:
    id: Id
    method: str
    params: Json


def _request(raw: str | bytes) -> _Request:
    """The request `raw` is. `_Refused` (`ParseError`, `InvalidRequest`, `InvalidParams`) if it is
    not one: not JSON, not an object (a batch), a notification, or params that are not by name."""
    if isinstance(raw, bytes):
        raise _refuse("InvalidRequest", "a message is one WebSocket text message")
    try:
        found = json.loads(raw)
    except ValueError as e:
        raise _refuse("ParseError", f"not JSON: {e}") from None
    if not isinstance(found, dict):
        raise _refuse("InvalidRequest", "a request is a JSON object; there are no batches")
    message = cast("Json", found)
    id = message.get("id")
    if isinstance(id, bool) or not isinstance(id, int | str):
        raise _refuse("InvalidRequest", "a request has an `id` (a notification is not one)")
    method, params = message.get("method"), message.get("params", {})
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        raise _refuse("InvalidRequest", 'a request has "jsonrpc": "2.0" and a `method`', id)
    if not isinstance(params, dict):
        raise _refuse("InvalidParams", "params are by name only: an object", id)
    return _Request(id, method, cast("Json", params))


def _reply(id: Id | None, *, result: object = None, error: Json | None = None) -> str:
    message: Json = {"jsonrpc": "2.0", "id": id}
    if error is None:
        message["result"] = result
    else:
        message["error"] = error
    return json.dumps(message)


def _failed(refused: _Refused) -> str:
    return _reply(refused.id, error=refused.error)


def _notification(method: str, params: Json) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": params})


_CLOSE: Final = object()
"""Put after a response to say: the connection is not to be used any more."""


class _Connection:
    """One WebSocket: the `hello`, then a reader that begins each call and a writer that sends."""

    def __init__(
        self,
        driver: Driver,
        welcome: object,
        calls: set[asyncio.Task[None]],
        socket: ServerConnection,
    ) -> None:
        self._driver = driver
        self._welcome = welcome
        self._socket = socket
        self.client: LocalClient | None = None
        self._outbox: asyncio.Queue[object] = asyncio.Queue()
        self._closing: asyncio.Future[None] | None = None
        self._calls = calls  # the server's: calls run on after their connection is gone

    async def run(self) -> None:
        client = await self._hello()
        if client is None:
            return
        self.client = client
        writer = asyncio.create_task(self._write(client))
        writer.add_done_callback(self._writer_done)
        try:
            async for raw in self._socket:
                self._receive(client, raw)
        except ConnectionClosed:
            pass
        finally:
            await client.close()  # first: a client lost is the driver's news at once
            self.client = None
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    def _writer_done(self, writer: "asyncio.Task[None]") -> None:
        """A writer that died of something (not cancelled, not finished): nothing could be told
        to the client any more, so the connection closes rather than freezing."""
        if not writer.cancelled() and writer.exception() is not None:
            self._closing = asyncio.ensure_future(self._socket.close(code=1011))

    async def _send(self, text: str) -> None:
        await self._socket.send(text)

    async def _hello(self) -> LocalClient | None:
        """The first message: `hello`, answered `Welcome` and then a `LocalClient`. Anything
        else, or a `hello` that cannot be accepted, is answered with its error and the
        connection closed (`None`)."""
        try:
            first = await self._socket.recv()
        except ConnectionClosed:
            return None
        try:
            request = _request(first)
            if request.method != "hello":
                raise _refuse("InvalidRequest", "`hello` comes first", request.id)
            client = self._accepted(request)
        except _Refused as refused:
            await self._finish(_failed(refused))
            return None
        self._outbox.put_nowait(_reply(request.id, result=self._welcome))
        return client

    def _accepted(self, request: _Request) -> LocalClient:
        """The client a `hello` makes. `_Refused` for params that do not fit, another protocol
        (`ProtocolMismatch`) or a bound the stream refuses."""
        try:
            params = decode_params("hello", request.params)
            asked = cast("int", params["protocol"])
            if asked != PROTOCOL:
                raise ProtocolMismatch(
                    f"this server speaks protocol {PROTOCOL}, not {asked}", PROTOCOL
                )
            buffer = cast("int | None", params.get("buffer"))
            return self._driver.client(
                cast("str", params["client"]), buffer=BUFFER if buffer is None else buffer
            )
        except TypeError as e:
            raise _refuse("InvalidParams", str(e), request.id) from None
        except (ProtocolMismatch, ValueError) as e:
            raise _Refused(error_object(e), request.id) from None

    async def _finish(self, text: str) -> None:
        """Send `text` and close: the connection is of no use."""
        with contextlib.suppress(ConnectionClosed):
            await self._send(text)
            await self._socket.close()

    def _receive(self, client: LocalClient, raw: str | bytes) -> None:
        """Begin the call `raw` asks for, or queue the error it earns."""
        try:
            request = _request(raw)
            if request.method == "hello":
                self._outbox.put_nowait(
                    _failed(_refuse("InvalidRequest", "`hello` was already said", request.id))
                )
                self._outbox.put_nowait(_CLOSE)
                return
            if request.method not in METHODS:
                raise _refuse("MethodNotFound", f"no method {request.method!r}", request.id)
            try:
                kwargs = decode_params(request.method, request.params)
            except TypeError as e:
                raise _refuse("InvalidParams", str(e), request.id) from None
        except _Refused as refused:
            self._outbox.put_nowait(_failed(refused))
            return
        task = asyncio.create_task(self._call(client, request, kwargs))
        self._calls.add(task)
        task.add_done_callback(self._calls.discard)

    async def _call(
        self, client: LocalClient, request: _Request, kwargs: dict[str, object]
    ) -> None:
        method = cast("Callable[..., Awaitable[object]]", getattr(client, request.method))
        try:
            result = encode_result(request.method, await method(**kwargs))
        except Exception as e:  # every exception a call raises is an error object
            self._outbox.put_nowait(_reply(request.id, error=error_object(e)))
        else:
            self._outbox.put_nowait(_reply(request.id, result=result))

    async def _write(self, client: LocalClient) -> None:
        """The one writer. It waits for an event or a response. Before a response it sends the
        events the stream holds; when the stream ends it sends the responses that are ready,
        then `end`."""
        event: asyncio.Future[object] | None = None
        queued: asyncio.Future[object] | None = None
        try:
            while True:
                event = event or asyncio.ensure_future(_next(client))
                queued = queued or asyncio.ensure_future(self._outbox.get())
                await asyncio.wait({event, queued}, return_when=asyncio.FIRST_COMPLETED)
                response: object = None
                if queued.done():
                    response, queued = queued.result(), None
                ended, error = False, None
                if event.done():
                    ended, error, first = _outcome(event)
                    event = None
                    if first is not None:
                        await self._send_event(first)
                for held in client.take():
                    await self._send_event(held)
                if await self._respond(response):
                    await self._socket.close()
                    return
                if ended:
                    await self._end(error)
                    return
        finally:
            for future in (event, queued):
                if future is not None:
                    future.cancel()

    async def _send_event(self, event: object) -> None:
        await self._send(_notification("event", encode_event(cast("Event", event))))

    async def _respond(self, response: object) -> bool:
        """Send a queued response, if there is one; whether the connection is to close after
        it."""
        if response is _CLOSE:
            return True
        if response is not None:
            await self._send(cast("str", response))
        return False

    async def _end(self, error: BaseException | None) -> None:
        """The stream ended: the responses that are ready, then `end`, then close."""
        while not self._outbox.empty():
            if await self._respond(self._outbox.get_nowait()):
                break
        await self._send(_notification("end", end_params(error)))
        await self._socket.close()


async def _next(client: LocalClient) -> object:
    return await client.__anext__()


def _outcome(future: "asyncio.Future[object]") -> tuple[bool, BaseException | None, object | None]:
    """How the wait for the stream's next event came out: whether the stream ended, with what
    (`None`: it stopped), and the event."""
    failure = future.exception()
    if failure is None:
        return False, None, future.result()
    if isinstance(failure, StopAsyncIteration):
        return True, None, None
    if isinstance(failure, ConnectionError):
        return True, failure, None
    raise failure


class Server:
    """A session's `Driver` served on a Unix socket (the module docstring). `start` binds
    `path`; `welcome` is what `hello` answers. `wait` returns when the driver has failed, or
    (with `idle`) when no client has been connected and every cursor at rest for that many
    seconds; `close` lets the connections say `end` (stop the driver first) and unbinds."""

    def __init__(
        self, driver: Driver, path: Path, welcome: Welcome, idle: float | None = None
    ) -> None:
        self.driver = driver
        self.path = path
        self._welcome = encode_result("hello", welcome)
        self._idle = idle
        self._poll = POLL if idle is None else min(POLL, idle / 4)
        self._sockets: _Sockets | None = None
        self._connections: set[_Connection] = set()
        self._finished: set[asyncio.Future[None]] = set()
        self._calls: set[asyncio.Task[None]] = set()

    @classmethod
    async def start(
        cls, driver: Driver, path: Path, *, session: str, cwd: Path, idle: float | None = None
    ) -> "Server":
        """Serve `driver` (running, in the loop) at `path`, as session `session` in `cwd`. `idle`
        seconds is how long `wait` lets the server rest before it returns (`None`: never).
        `OSError` if the socket cannot be bound."""
        welcome = Welcome(PROTOCOL, version("ffwf-tau"), session, str(cwd), os.getpid())
        server = cls(driver, path, welcome, idle)
        server._sockets = await unix_serve(server._handle, path, max_size=None)
        return server

    async def _handle(self, socket: ServerConnection) -> None:
        """Handle one connection to its end."""
        connection = _Connection(self.driver, self._welcome, self._calls, socket)
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._connections.add(connection)
        self._finished.add(done)
        try:
            await connection.run()
        finally:
            self._connections.discard(connection)
            self._finished.discard(done)
            done.set_result(None)

    def _connected(self) -> int:
        """How many clients have said `hello` and not yet gone."""
        return sum(1 for c in self._connections if c.client is not None)

    async def wait(self) -> Exception | None:
        """Wait until the driver has failed (returns its failure) or stopped, or, for a server
        with `idle`, has rested that long (returns `None`)."""
        loop = asyncio.get_running_loop()
        resting: float | None = None
        while True:
            state = self.driver.state()
            if state.failure is not None:
                return state.failure
            if state.stopped:
                return None
            quiet = not self._connected() and not state.units and not state.queued
            if not quiet:
                resting = None
            elif resting is None:
                resting = loop.time()
            elif self._idle is not None and loop.time() - resting >= self._idle:
                return None
            await asyncio.sleep(self._poll)

    async def close(self) -> None:
        """Let the connections finish (the driver should be stopped, so each has said `end`; one
        that has not in `GRACE` seconds is closed), then unbind. A call a connection began and
        left is cancelled."""
        if self._finished:
            await asyncio.wait(set(self._finished), timeout=GRACE)
        if self._sockets is not None:
            self._sockets.close()
            await self._sockets.wait_closed()
        for call in self._calls:
            call.cancel()
        await asyncio.gather(*self._calls, return_exceptions=True)
