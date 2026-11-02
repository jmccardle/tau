"""`RemoteClient`: a `Client` of a session served by `tau serve` (D21 sections 3, 6, 9).

`await RemoteClient.connect(socket, client)` dials the server's Unix socket, says `hello` and
returns a client that is held to the `Client` protocol: every call is a JSON-RPC request of the
same name, answered in its own time (calls are concurrent, matched by id), and the events
arrive as notifications, queued for `async for`. One reader task does all the receiving; sending
is the caller's.

The three ways a stream can end are the local ones: it ends (the driver stopped), or raises
`ConnectionError` (`CutOff`; the driver failed, with its `"Type: msg"`; or the connection
dropped without an `end`). Every call on an ended stream raises `ConnectionError` too, so a
frontend's one rule is `except ConnectionError`, then connect again. `ProtocolMismatch` is not
one: it is a `RuntimeError`, so that rule does not loop on it.
"""

import asyncio
import json
from collections.abc import Collection, Mapping
from os import PathLike
from typing import Final, Self, cast

from websockets.asyncio.client import ClientConnection, unix_connect
from websockets.exceptions import ConnectionClosed

from tau._core import Node
from tau._hub import Feed
from tau._protocol import (
    PROTOCOL,
    ProtocolError,
    Welcome,
    decode_event,
    decode_result,
    encode_params,
    end_error,
    exception_for,
)
from tau._values import Form, Json
from tau.commandline import Parsed
from tau.events import Event

_END: Final = object()
"""Queued after the last event: the stream has ended."""


class _Channel:
    """The connection under a `RemoteClient`: the requests in flight, the queued events, how the
    stream ended. A `Client` apart from its calls."""

    id: str
    welcome: Welcome
    """What the server answered `hello`: its protocol and version, the session, its `cwd`, its
    `pid`."""

    def __init__(self, socket: ClientConnection, client: str, buffer: int) -> None:
        self.id = client
        self._socket = socket
        self._ids = 0
        self._waiting: dict[int, asyncio.Future[object]] = {}
        self._feed = Feed(client, buffer, self._released)
        self._ended: ConnectionError | None = None
        self._dropped = False
        self._reader = asyncio.create_task(self._read())

    def _released(self, feed: Feed) -> None:
        """The stream was closed or cut off: this connection has no use."""
        self._dropped = True

    @classmethod
    async def connect(cls, socket: str | PathLike[str], client: str, *, buffer: int = 256) -> Self:
        """Dial the server at `socket` as `client` with an event stream of `buffer`, and say
        `hello`. The snapshot is queued before this returns. `ProtocolMismatch` if the server
        speaks another protocol, `OSError` if nothing answers at `socket`."""
        channel = cls(await unix_connect(socket, max_size=None), client, buffer)
        try:
            welcome = await channel.request(
                "hello", {"protocol": PROTOCOL, "client": client, "buffer": buffer}
            )
        except BaseException:
            await channel.close()
            raise
        channel.welcome = cast("Welcome", welcome)
        return channel

    # The stream

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Event:
        return await anext(self._feed)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Disconnect: the stream ends, what it holds is dropped, and later calls raise
        `ConnectionError`."""
        await self._feed.close()
        self._finish(ConnectionError("the client is closed"))
        await self._socket.close()
        await asyncio.gather(self._reader, return_exceptions=True)

    # Receiving

    async def _read(self) -> None:
        lost = ConnectionError("the connection to the server was lost")
        try:
            async for raw in self._socket:
                self._dispatch(raw)
                if self._dropped:  # cut off: leave, as a client that closed
                    self._finish(ConnectionError("the client fell behind and was cut off"))
                    break
        except ConnectionClosed:
            pass
        except ProtocolError as e:
            self._feed.end(e)
            self._finish(ConnectionError(f"protocol error: {e}"))
        finally:  # however the reader ends, nothing waits on it any more
            self._feed.end(lost)
            self._finish(lost)
            await self._socket.close()

    def _dispatch(self, raw: str | bytes) -> None:
        """Route one message: a response to its request, an event to the queue, `end` to the
        end of the stream. `ProtocolError` for anything else."""
        try:
            message = json.loads(raw)
        except ValueError as e:
            raise ProtocolError(f"the server sent what is not JSON: {e}") from None
        if not isinstance(message, dict):
            raise ProtocolError(f"the server sent {message!r}, not a JSON-RPC message")
        found = cast("Json", message)
        method = found.get("method")
        params = found.get("params")
        if method is None and "id" in found:
            self._answer(found)
        elif method == "event" and isinstance(params, dict):
            try:
                self._feed.offer(decode_event(cast("Json", params)))
            except (TypeError, ValueError) as e:
                raise ProtocolError(f"the server sent an event that does not fit: {e}") from None
        elif method == "end" and isinstance(params, dict):
            failure = end_error(cast("Json", params))
            self._feed.end(failure if isinstance(failure, Exception) else None)
            self._finish(
                failure
                if isinstance(failure, ConnectionError)
                else ConnectionError("the driver is stopped")
            )
        else:
            raise ProtocolError(f"the server sent {found!r}, not a response, event or end")

    def _answer(self, response: Json) -> None:
        future = self._waiting.pop(cast("int", response["id"]), None)
        if future is None or future.done():
            return  # the caller stopped waiting
        error = response.get("error")
        if error is None:
            future.set_result(response.get("result"))
        else:
            found = cast("Json", error)
            data = found.get("data")
            future.set_exception(
                exception_for(
                    cast("int", found.get("code")),
                    cast("str", found.get("message")),
                    cast("Json", data) if isinstance(data, dict) else None,
                )
            )

    def _finish(self, gone: ConnectionError) -> None:
        """The connection is over: fail the requests in flight with `gone`, which later calls
        raise too. The first reason stands."""
        if self._ended is None:
            self._ended = gone
        for future in self._waiting.values():
            if not future.done():
                future.set_exception(self._ended)
        self._waiting.clear()

    # Sending

    async def request(self, method: str, params: Json) -> object:
        """Send request `method` and wait for its result, decoded. `ConnectionError` if the
        stream has ended; the exception the error code names if the server answered with
        one."""
        if self._ended is not None:
            raise self._ended
        self._ids += 1
        request_id = self._ids
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            await self._socket.send(json.dumps(message))
            result = await future
        except ConnectionClosed:
            raise ConnectionError("the connection to the server was lost") from None
        finally:
            self._waiting.pop(request_id, None)
        try:
            return decode_result(method, result)
        except (TypeError, ValueError) as e:
            raise ProtocolError(f"the server's answer to {method} does not fit: {e}") from None

    async def call(self, method: str, **args: object) -> object:
        """`request` for a call's arguments: they are encoded by the call's signature, so a
        value that is not JSON is a `TypeError` here, before anything is sent."""
        return await self.request(method, encode_params(method, args))


class RemoteClient(_Channel):
    """A `Client` of a session served by `tau serve`: build one with `RemoteClient.connect`. Each
    call is documented once, on `Client`. It is also an async context manager that closes."""

    async def input(self, cursor: int, text: str) -> int:
        return cast("int", await self.call("input", cursor=cursor, text=text))

    async def send(self, cursor: int, text: str) -> int:
        return cast("int", await self.call("send", cursor=cursor, text=text))

    async def step(self, cursor: int) -> None:
        await self.call("step", cursor=cursor)

    async def go_to(self, cursor: int, to: int) -> None:
        await self.call("go_to", cursor=cursor, to=to)

    async def set_auto(self, cursor: int, on: bool) -> None:
        await self.call("set_auto", cursor=cursor, on=on)

    async def abort(self, cursor: int) -> None:
        await self.call("abort", cursor=cursor)

    async def open(self, at: int) -> int:
        return cast("int", await self.call("open", at=at))

    async def close_cursor(self, cursor: int) -> None:
        await self.call("close_cursor", cursor=cursor)

    async def reopen(self, cursor: int) -> int:
        return cast("int", await self.call("reopen", cursor=cursor))

    async def set_label(self, cursor: int, label: str) -> None:
        await self.call("set_label", cursor=cursor, label=label)

    async def remove_label(self, label: str) -> None:
        await self.call("remove_label", label=label)

    async def watch(self, cursors: Collection[int]) -> None:
        # The server's catch-up arrives before its answer: be listening for these cursors by then.
        before = self._feed.watching
        self._feed.watching = frozenset(cursors)
        try:
            await self.call("watch", cursors=list(cursors))
        except BaseException:
            self._feed.watching = before
            raise

    async def answer(self, question: int, value: object) -> None:
        await self.call("answer", question=question, value=value)

    async def commands(self) -> tuple[Form, ...]:
        return cast("tuple[Form, ...]", await self.call("commands"))

    async def run(self, cursor: int, command: str, args: Mapping[str, object]) -> str | None:
        return cast("str | None", await self.call("run", cursor=cursor, command=command, args=args))

    async def options(self, cursor: int, extension: str) -> Form:
        return cast("Form", await self.call("options", cursor=cursor, extension=extension))

    async def configure(
        self, cursor: int, extension: str, values: Mapping[str, object], *, save: bool = False
    ) -> None:
        await self.call("configure", cursor=cursor, extension=extension, values=values, save=save)

    async def complete(self, cursor: int, named: str, prefix: str) -> list[str]:
        return cast(
            "list[str]", await self.call("complete", cursor=cursor, named=named, prefix=prefix)
        )

    async def parse_line(self, cursor: int, line: str) -> Parsed:
        return cast("Parsed", await self.call("parse_line", cursor=cursor, line=line))

    async def complete_line(self, cursor: int, line: str) -> list[str]:
        return cast("list[str]", await self.call("complete_line", cursor=cursor, line=line))

    async def node(self, id: int) -> Node:
        return cast("Node", await self.call("node", id=id))

    async def nodes(self, start: int, limit: int) -> list[Node]:
        return cast("list[Node]", await self.call("nodes", start=start, limit=limit))

    async def walk(self, id: int) -> list[Node]:
        return cast("list[Node]", await self.call("walk", id=id))

    async def children(self, id: int) -> list[int]:
        return cast("list[int]", await self.call("children", id=id))
