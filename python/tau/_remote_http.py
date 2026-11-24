"""The client's side of the HTTP edge (D29): a line that `RemoteClient` speaks the wire over, as
it does over a WebSocket.

`HttpLine.open(url, token)` sends `GET /stream` with the bearer token and reads the connection id
(the stream's first event). `send` POSTs one message to `/stream/<id>`; `async for` gives each
message the stream carries. When the line is gone, `send` raises websockets' `ConnectionClosed`,
as a WebSocket's does, and `async for` ends.

POSTs are sent one at a time, each after the last was accepted (202): the server begins calls in
the order they arrive, and two POSTs in flight on two connections could arrive in either order.

Proxies from the environment are not used (`trust_env=False`): the token goes only to the URL the
serve record names.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Final, Self

import httpx
from websockets.exceptions import ConnectionClosed

CONNECT_SECONDS: Final = 10.0
"""How long opening the stream (up to its connection id), or one POST, may take."""
_OK: Final = 200
_ACCEPTED: Final = 202
_NOT_FOUND: Final = 404
_UNAUTHORIZED: Final = (401, 403)


class HttpLine:
    """One stream of a server's HTTP edge and the POSTs that go with it (the module docstring)."""

    def __init__(self, http: httpx.AsyncClient, response: httpx.Response, id: str) -> None:
        self._http = http
        self._response = response
        self.id = id
        self._lines = response.aiter_lines()
        self._sending = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(cls, url: str, token: str) -> Self:
        """Open a stream at `url` (the edge's root) with `token`. `ConnectionRefusedError` if
        nothing listens there, `PermissionError` if the edge refuses the token, `TimeoutError` if
        it does not answer in `CONNECT_SECONDS`, `OSError` for anything else."""
        http = httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(CONNECT_SECONDS, read=None),
            trust_env=False,
        )
        try:
            async with asyncio.timeout(CONNECT_SECONDS):
                response = await _opened(http, url)
                line = cls(http, response, "")
                line.id = await line._first()
        except BaseException:
            await http.aclose()
            raise
        return line

    async def _first(self) -> str:
        """The connection id: the stream's first event, `event: connection`."""
        async for kind, data in self._events():
            if kind == "connection":
                return data
            break
        raise OSError("the server's stream did not begin with its connection id")

    async def _events(self) -> AsyncIterator[tuple[str, str]]:
        """Each event the stream carries, as (its type, its data); comments are skipped."""
        kind, data = "message", list[str]()
        try:
            async for line in self._lines:
                if not line:
                    if data:
                        yield kind, "\n".join(data)
                    kind, data = "message", []
                    continue
                field, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if field == "data":
                    data.append(value)
                elif field == "event":
                    kind = value
        except (httpx.HTTPError, OSError):
            return  # the connection went: the stream ends, as a dropped WebSocket's does

    def __aiter__(self) -> AsyncIterator[str]:
        return self._messages()

    async def _messages(self) -> AsyncIterator[str]:
        async for kind, data in self._events():
            if kind == "message":
                yield data

    async def send(self, text: str) -> None:
        """POST one message. `ConnectionClosed` if the stream is gone (the edge answers 404) or
        the POST fails."""
        if self._closed:
            raise ConnectionClosed(None, None)
        async with self._sending:
            try:
                answer = await self._http.post(
                    f"stream/{self.id}",
                    content=text.encode(),
                    headers={"Content-Type": "application/json"},
                    timeout=CONNECT_SECONDS,
                )
            except httpx.HTTPError:
                # The request may or may not have arrived: the line fails as one, as a WebSocket
                # does, rather than going on with a call lost.
                await self.close()
                raise ConnectionClosed(None, None) from None
        if answer.status_code == _NOT_FOUND:
            raise ConnectionClosed(None, None)
        if answer.status_code != _ACCEPTED:
            await self.close()
            raise OSError(f"the server answered a request with HTTP {answer.status_code}")

    async def close(self) -> None:
        """Close the stream and the client's connections. Idempotent."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(httpx.HTTPError, OSError, RuntimeError):
            await self._response.aclose()
        await self._http.aclose()


async def _opened(http: httpx.AsyncClient, url: str) -> httpx.Response:
    """The response to `GET /stream`, its headers read; the errors `HttpLine.open` names."""
    try:
        response = await http.send(http.build_request("GET", "stream"), stream=True)
    except httpx.ConnectError as e:
        raise ConnectionRefusedError(f"nothing answers at {url}: {e}") from e
    except httpx.TimeoutException as e:
        raise TimeoutError(f"{url} did not answer in {CONNECT_SECONDS:g} s") from e
    except httpx.HTTPError as e:
        raise OSError(f"{url}: {e}") from e
    if response.status_code == _OK:
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            return response
        await response.aclose()
        raise OSError(f"{url} answered with no event stream: is it a tau serve?")
    await response.aread()
    await response.aclose()
    message = f"{url} answered HTTP {response.status_code}: {response.text.strip()}"
    if response.status_code in _UNAUTHORIZED:
        raise PermissionError(message)
    raise OSError(message)
