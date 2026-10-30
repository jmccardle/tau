"""What every hosted provider shares over HTTP: private to the package.

Three parts. JSON and number checks (`plain`, `non_finite`, `is_number`, `is_count`, `count`).
Reading a response (`retry_after`, `error_text`, `sse_data`, `events`). And `Connection`, which
holds the one client a provider keeps and runs the sample loop every provider repeats: send the
request, read the body to its end, turn an `httpx` error or a bad status into a `SampleError`.
A provider supplies its request, a `ReplyReader` that reads its events, and how its error
bodies read.
"""

import asyncio
import json
import math
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol, cast

import httpx

from tau_next._core import Part
from tau_next.events import Delta
from tau_next.protocols import Finish, ProviderClosed, SampleError

Json = dict[str, object]
HTTP_OK = 200
DEFAULT_TIMEOUT = httpx.Timeout(10.0, read=300.0)


def retry_after(value: str | None) -> float | None:
    """Seconds to wait from a `Retry-After` header: whole seconds as sent, an HTTP-date as
    seconds from now (0 if past); `None` for no header or anything else."""
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        seconds = float(text)
        return seconds if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def plain(value: object) -> object:
    """`value` as plain JSON: `ModelConfig` options are read-only mappings and tuples."""
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in cast("Mapping[str, object]", value).items()}
    if isinstance(value, tuple | list):
        return [plain(v) for v in cast("tuple[object, ...]", value)]
    return value


def non_finite(value: object) -> list[float]:
    """Every NaN or infinity inside `value`."""
    if isinstance(value, float):
        return [] if math.isfinite(value) else [value]
    if isinstance(value, Mapping):
        return [b for v in cast("Mapping[str, object]", value).values() for b in non_finite(v)]
    if isinstance(value, tuple | list):
        return [b for v in cast("tuple[object, ...]", value) for b in non_finite(v)]
    return []


def is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def is_count(value: object) -> bool:
    """A non-negative integer (not a boolean)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


async def sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """The `data` of each SSE event: its `data:` lines joined by newlines. The body is read to
    its end, so the connection goes back to the pool."""
    lines: list[str] = []
    async for line in response.aiter_lines():
        if line:
            if line.startswith("data:"):
                lines.append(line.removeprefix("data:").removeprefix(" "))
            continue
        if lines:
            yield "\n".join(lines)
            lines = []
    if lines:
        yield "\n".join(lines)


def count(value: object) -> int | None:
    """`value` if it is a non-negative integer, else `None` (a malformed count in a `usage`)."""
    return value if isinstance(value, int) and is_count(value) else None


def meta_text(meta: Json) -> str | None:
    """The `meta` of a reply as `Finish.meta` and `SampleError.meta` take it."""
    return json.dumps(meta) if meta else None


def error_text(status: int, body: bytes, *, typed: bool = False) -> str:
    """`HTTP <status>: <the server's error message, else its body>`, from the `error` object
    most APIs send. With `typed` the object's `type` leads the message (`<type>: <message>`)."""
    text = body.decode("utf-8", errors="replace").strip()
    try:
        parsed: object = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        error = cast("Json", parsed).get("error")
        if isinstance(error, dict):
            fields = cast("Json", error)
            text = str(fields.get("message", text))
            if typed:
                text = f"{fields.get('type', 'error')}: {text}"
    return f"HTTP {status}: {text[:500]}"


def json_object(value: object, what: str) -> Json:
    if not isinstance(value, dict):
        raise SampleError(f"{what} is not a JSON object: {value!r}")
    return cast("Json", value)


async def events(response: httpx.Response, end: str | None = None) -> AsyncIterator[Json]:
    """The decoded SSE events of a response, each a JSON object. The body is read to its end,
    so the connection goes back to the pool; with `end` (OpenAI's `[DONE]`), whatever follows
    that data is read but not looked at."""
    done = False
    async for data in sse_data(response):
        done = done or data == end
        if done:
            continue
        try:
            yield json_object(json.loads(data), "a stream event")
        except ValueError as e:
            raise SampleError(f"malformed SSE data {data[:200]!r}: {e}") from e


class ReplyReader(Protocol):
    """One response being read, as `Connection.sample` needs it. `feed` takes a decoded event
    and returns its parts and deltas; `done` is true once the reply's stop reason has arrived
    (after it only the meta is still read); `stop` is the `Finish` or raises `SampleError`;
    `close` is the part the open run had become, if any."""

    meta: Json

    @property
    def done(self) -> bool: ...
    def feed(self, event: Json, /) -> list[Part | Delta]: ...
    def close(self) -> list[Part | Delta]: ...
    def stop(self) -> Finish: ...


class Connection:
    """The one `httpx.AsyncClient` a provider keeps, and the sample loop over it.

    The client serves every sample (httpx pools connections per host): made on the first,
    closed by `aclose()`, after which a sample fails with `ProviderClosed`. A client belongs to
    the event loop that made it: a sample on another loop drops it unawaited (that loop is gone
    and cannot be awaited here) and makes a new one, so a program may call `asyncio.run` once per
    command. `transport` replaces the network (a test's `httpx.MockTransport`).

    `typed_errors` is whether the provider's error bodies lead with their `type`; `body_wait`
    reads a wait out of an error body when the `Retry-After` header is missing (Google sends
    `RetryInfo` there); `meta_headers` maps response headers to `meta` keys, kept on success
    and on failure alike."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
        *,
        typed_errors: bool = False,
        body_wait: Callable[[bytes], float | None] | None = None,
        meta_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._transport = transport
        self._timeout = timeout or DEFAULT_TIMEOUT
        self._typed = typed_errors
        self._body_wait = body_wait
        self._meta_headers = meta_headers or {}
        self._http: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None  # the loop `_http` belongs to
        self._closed = False

    async def aclose(self) -> None:
        """Close the client and its connections. Later samples fail."""
        self._closed = True
        client, self._http = self._http, None
        if client is not None and self._loop is asyncio.get_running_loop():
            await client.aclose()

    def _client(self) -> httpx.AsyncClient:
        if self._closed:
            raise ProviderClosed("this provider was closed; build a new one to sample again")
        loop = asyncio.get_running_loop()
        if self._http is not None and self._loop is not loop:
            self._http = None  # its loop is gone (a REPL runs `asyncio.run` per command)
        if self._http is None:
            self._http = httpx.AsyncClient(transport=self._transport, timeout=self._timeout)
            self._loop = loop
        return self._http

    async def _refuse(self, response: httpx.Response) -> None:
        """Raise the `SampleError` for an error response, with its status and wait taken before
        the body is read (a body that fails to read still has them)."""
        status = response.status_code
        wait = retry_after(response.headers.get("retry-after"))
        try:
            body = await response.aread()
        except httpx.HTTPError as e:
            raise SampleError(
                f"HTTP {status}: the error body could not be read ({type(e).__name__}: {e})",
                status=status,
                retry_after=wait,
            ) from e
        if wait is None and self._body_wait is not None:
            wait = self._body_wait(body)
        raise SampleError(
            error_text(status, body, typed=self._typed), status=status, retry_after=wait
        )

    async def sample(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Json,
        reply: ReplyReader,
        *,
        end: str | None = None,
    ) -> AsyncGenerator[Part | Finish | Delta, None]:
        """POST `body` and yield what `reply` makes of the events, then its `Finish`. A failure
        raises `SampleError` (with `status` and `retry_after` for an HTTP error) after yielding
        every part the stream had finished or begun, carrying the meta that had arrived. Once
        `reply.done` the reply is complete: a failure after it costs only the meta not yet read.
        `end` is a data string that closes the stream (see `events`)."""
        client = self._client()
        error: SampleError | None = None
        try:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                for header, key in self._meta_headers.items():
                    value = response.headers.get(header)
                    if value:
                        reply.meta[key] = value
                if response.status_code != HTTP_OK:
                    await self._refuse(response)
                async for event in events(response, end):
                    for item in reply.feed(event):
                        yield item
        except httpx.HTTPError as e:
            kind = ProviderClosed if self._closed else SampleError
            error = kind(f"{type(e).__name__}: {e}")
        except SampleError as e:
            error = e
        if error is None or reply.done:
            try:
                finish = reply.stop()
            except SampleError as e:
                error = e
            else:
                yield finish
                return
        assert error is not None
        error.meta = meta_text(reply.meta)
        try:
            leftover = reply.close()
        except SampleError as cut_off:
            # A cut-off call with no id or name is not a part; the failure records what arrived.
            raise type(error)(
                f"{error}; {cut_off}",
                meta_text(reply.meta),
                status=error.status,
                retry_after=error.retry_after,
            ) from error
        for part in leftover:
            yield part
        raise error
