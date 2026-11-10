"""What every hosted provider shares over HTTP: private to the package.

Four parts. JSON and number checks (`strict_loads`, `plain`, `non_finite`, `is_number`,
`is_count`, `count`). The option checks every `check` shares (`option_problems`,
`extra_body_problems`). Reading a response (`retry_after`, `error_text`, `sse_data`, `events`,
`note_usage`, `finish`). And `Connection`, which
holds the one client a provider keeps and runs the sample loop every provider repeats: send the
request, read the body to its end, turn an `httpx` error or a bad status into a `SampleError`.
A provider supplies its request, a `ReplyReader` that reads its events, and how its error
bodies read.
"""

import asyncio
import hashlib
import json
import math
import re
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol, cast

import httpx

from tau._config import ModelConfig
from tau._core import Body, Node, Part, Stop
from tau._events import Delta
from tau._protocols import Finish, ProviderClosed, Rendered, SampleError

Json = dict[str, object]
HTTP_OK = 200
DEFAULT_TIMEOUT = httpx.Timeout(10.0, read=300.0)
PROBE_TIMEOUT = httpx.Timeout(5.0)
UNREACHED = object()
"""What `Connection.get_json` returns when the server gave no answer at all."""
_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")  # Google's `retryDelay`: "30s", "0.5s"
_BAD_HEADER = "the request could not be sent: a request header holds a character HTTP can't carry"


def retry_after(value: str | None) -> float | None:
    """Seconds to wait from a `Retry-After` header: whole seconds as sent, an HTTP-date as
    seconds from now (0 if past); `None` for no header, a non-finite number, or anything else."""
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        seconds = float(text)
        return seconds if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def strict_loads(text: str) -> object:
    """`json.loads` that refuses `NaN`, `Infinity` and `-Infinity` (Python accepts them, JSON
    does not, and the log and the other providers' bodies cannot carry them): a `ValueError`."""
    return json.loads(text, parse_constant=_no_constant)


def has_lone_surrogate(text: str, value: object) -> bool:
    """Whether `value`, parsed from `text`, holds a lone surrogate (the escape `\\ud800`): JSON
    allows it, UTF-8 cannot carry it. Only text with an escape can, so other text is not walked."""
    if "\\u" not in text:
        return False
    try:
        json.dumps(value, ensure_ascii=False).encode()
    except UnicodeEncodeError:
        return True
    return False


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
    """A finite int or float (not a boolean). Never raises: an int too big for a float is not."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def is_count(value: object) -> bool:
    """A non-negative integer (not a boolean)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def option_problems(
    model: ModelConfig,
    known: Sequence[str],
    *,
    numbers: Sequence[str] = (),
    flags: Sequence[str] = (),
) -> list[str]:
    """The problems every provider's `check` finds in `model.options` by name alone: a key not
    in `known`, a `numbers` key that is not a finite number, a `flags` key that is not a
    boolean. The driver hands a provider only models that name it, so no provider checks that."""
    options = model.options
    return (
        [
            f"unknown option {key!r}; known options are {list(known)}"
            for key in options
            if key not in known
        ]
        + [
            f"option {key!r} must be a number"
            for key in numbers
            if key in options and not is_number(options[key])
        ]
        + [
            f"option {key!r} must be true or false"
            for key in flags
            if key in options and not isinstance(options[key], bool)
        ]
    )


def extra_body_problems(
    extra: object, owned: Sequence[str], typed: Mapping[str, object]
) -> list[str]:
    """The problems in an `extra_body` option: not an object, a key in `owned` (the provider
    sets it), a key in `typed` (the request body the model's own settings build), or a NaN or
    infinity anywhere inside."""
    if not isinstance(extra, Mapping):
        return ["option 'extra_body' must be an object"]
    fields = cast("Mapping[str, object]", extra)
    return (
        [
            f"option 'extra_body' may not set {k!r}; the provider owns it"
            for k in fields
            if k in owned
        ]
        + [
            f"option 'extra_body' sets {k!r}, which the model's settings already set"
            for k in typed
            if k in fields
        ]
        + [f"option 'extra_body' holds {bad!r}, which is not JSON" for bad in non_finite(fields)]
    )


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


def nested_counts(usage: Json, raw: Json, wanted: Sequence[tuple[str, str, str]]) -> bool:
    """Add to the normalized `usage` each count a server nests one object down: `wanted` is
    `(key, object, field)`, read as `raw[object][field]`. An object the server leaves out or
    sends as null, or a field it leaves out, adds nothing. False if an object is not one or a
    field that is there is not a count (null included): the report is malformed."""
    for key, holder, field in wanted:
        details = raw.get(holder)
        if details is None:
            continue
        if not isinstance(details, dict):
            return False
        fields = cast("Json", details)
        if field not in fields:
            continue
        if count(fields[field]) is None:
            return False
        usage[key] = fields[field]
    return True


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def rendered(url: str, body: Json, key: str, nodes: Sequence[Sequence[int]]) -> Rendered:
    """The `Rendered` of a request to `url` whose conversation is the list `body[key]`, each
    message of it built from the nodes `nodes` names."""
    conversation = cast("list[object]", body[key])
    rest = {k: v for k, v in body.items() if k != key}
    return Rendered(
        url,
        body,
        tuple(_compact(m) for m in conversation),
        tuple(tuple(ids) for ids in nodes),
        f"{url}\n{_compact(rest)}",
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _chain(messages: Sequence[str]) -> str:
    return _digest("\n".join(messages))  # a message is JSON on one line


def sent_stamp(request: Rendered, walk: Sequence[Node]) -> Json:
    """What a reply's `meta.sent` says of the request that made it (D26 section 3.1): how many
    `messages` it had, a digest of its `fixed` text and one of its messages (`chain`), and
    `extends`, the id of the reply before it on the walk when that reply's own stamp shows its
    request to be a byte prefix of this one (same `fixed`, and its messages are this request's
    first ones). That is the proof of the span between the two replies, made from the bytes
    sent, on a server whose cache report cannot make it."""
    stamp: Json = {
        "messages": len(request.messages),
        "fixed": _digest(request.fixed),
        "chain": _chain(request.messages),
    }
    previous = next((n for n in reversed(walk) if isinstance(n.body, Body.Assistant)), None)
    before = _stamp_of(previous)
    if previous is not None and before is not None:
        held = before.get("messages")
        if (
            isinstance(held, int)
            and 0 < held <= len(request.messages)
            and before.get("fixed") == stamp["fixed"]
            and before.get("chain") == _chain(request.messages[:held])
        ):
            stamp["extends"] = previous.id
    return stamp


def _stamp_of(node: Node | None) -> Json | None:
    """The `meta.sent` of a reply, if it has one."""
    if node is None or not isinstance(node.body, Body.Assistant) or node.body.meta is None:
        return None
    meta: object = json.loads(node.body.meta)
    found = cast("Json", meta).get("sent") if isinstance(meta, dict) else None
    return cast("Json", found) if isinstance(found, dict) else None


def meta_text(meta: Json) -> str | None:
    """The `meta` of a reply as `Finish.meta` and `SampleError.meta` take it. A key whose value
    holds a NaN or infinity is dropped (JSON cannot carry it; the rest of the meta is kept)."""
    kept = {k: v for k, v in meta.items() if not non_finite(v)}
    return json.dumps(kept, allow_nan=False) if kept else None


def _overcounts(usage: Json) -> bool:
    """Whether a normalized `usage` has more cached and written tokens than input, more written
    for an hour than written, or more tool-prompt tokens than input: a part larger than its
    whole. (`reasoning` is not held to `output`: Anthropic says its thinking count "may differ
    by a small number", and what subtracts it stops at 0.)"""

    def part(key: str) -> int:
        return cast("int", usage.get(key, 0))

    whole = cast("int", usage["input"])
    return (
        part("cached") + part("written") > whole
        or part("written_1h") > part("written")
        or part("tool_prompt") > whole
    )


def note_usage(meta: Json, raw: Json, normalize: Callable[[Json], Json | None]) -> None:
    """Keep a `usage` report in `meta`: as sent (`usage_raw`) and normalized (`usage`). The
    latest report is the one that counts, so a malformed one (or one that says more tokens were
    read from and written to the cache than were sent) drops the earlier `usage`; `usage_raw`
    still holds what the server said."""
    meta["usage_raw"] = raw
    normal = normalize(raw)
    if normal is not None and _overcounts(normal):
        normal = None  # more read and written than sent: as malformed as a bad count
    if normal is None:
        meta.pop("usage", None)
    else:
        meta["usage"] = normal


def finish(reason: str | None, stops: Mapping[str, Stop], field: str, meta: Json) -> Finish:
    """The `Finish` for a provider's stop `reason` (`field` is its name for it): a `SampleError`
    when none arrived or `stops` does not know it."""
    if reason is None:
        raise SampleError(f"the stream ended without a {field}")
    stop = stops.get(reason)
    if stop is None:
        raise SampleError(f"unknown {field} {reason!r}")
    return Finish(stop, reason, meta_text(meta))


def delay(error: Json) -> float | None:
    """Seconds from the `RetryInfo` in an error object's `details` (Google's `retryDelay`), if
    it has one."""
    details = error.get("details")
    for detail in cast("list[object]", details) if isinstance(details, list) else []:
        if isinstance(detail, dict):
            wait = cast("Json", detail).get("retryDelay")
            found = _DELAY.match(wait) if isinstance(wait, str) else None
            if found:
                return float(found.group(1))
    return None


def _body_wait(body: bytes) -> float | None:
    """The wait an error body names when the `Retry-After` header is missing."""
    try:
        parsed: object = json.loads(body)
    except ValueError:
        return None
    error = cast("Json", parsed).get("error") if isinstance(parsed, dict) else None
    return delay(cast("Json", error)) if isinstance(error, dict) else None


def error_text(status: int, body: bytes) -> str:
    """`HTTP <status>: <the server's error message, else its body>`, from the `error` object
    most APIs send. The object's `type`, when it has one, leads the message
    (`<type>: <message>`)."""
    text = body.decode("utf-8", errors="replace").strip()
    try:
        parsed: object = strict_loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        error = cast("Json", parsed).get("error")
        if isinstance(error, dict):
            fields = cast("Json", error)
            text = str(fields.get("message", text))
            if isinstance(fields.get("type"), str):
                text = f"{fields['type']}: {text}"
    return f"HTTP {status}: {text[:500]}"


def json_object(value: object, what: str) -> Json:
    if not isinstance(value, dict):
        raise SampleError(f"{what} is not a JSON object: {value!r}")
    return cast("Json", value)


async def events(response: httpx.Response, end: str | None = None) -> AsyncIterator[Json]:
    """The decoded SSE events of a response, each a JSON object. A NaN or infinity, or a lone
    surrogate anywhere in an event, is a `SampleError`. The body is read to its end,
    so the connection goes back to the pool; with `end` (OpenAI's `[DONE]`), whatever follows
    that data is read but not looked at."""
    done = False
    async for data in sse_data(response):
        done = done or data == end
        if done:
            continue
        try:
            parsed = strict_loads(data)
        except ValueError as e:
            raise SampleError(f"malformed SSE data {data[:200]!r}: {e}") from e
        if has_lone_surrogate(data, parsed):
            raise SampleError(
                f"SSE data holds a lone surrogate, which UTF-8 cannot carry: {data[:200]!r}"
            )
        yield json_object(parsed, "a stream event")


class ReplyReader(Protocol):
    """One response being read, as `Connection.sample` needs it.

    `feed` takes a decoded event and returns its parts and deltas. `done` is true once the
    reply's stop reason has arrived: after it only the meta is still read, so `Connection`
    discards what `feed` returns (the reader need only keep reading the meta). If `feed` raises
    after building items for its event, it keeps them for the next `close()`: none is lost.

    `close` ends a stream that broke. It returns the finished parts not yet returned, then the
    open text or thinking run as far as it arrived (thinking unsigned). It never returns a
    partial call and it never raises: a call cut off is dropped whole (D1), and its raw text
    goes in `meta["cut_off_call"]`, which `Connection` moves into the error message. It is
    called only on a failure; a success goes straight to `stop`.

    `stop` is the `Finish`, or raises `SampleError` when no stop arrived."""

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
    the event loop that made it, and a sample on another loop fails with `SampleError`: build
    one provider per event loop (D10). `transport` replaces the network (a test's
    `httpx.MockTransport`).

    An error body's `type` leads its message (`error_text`), and a wait missing from the
    `Retry-After` header is read from the body (Google sends `RetryInfo` there). `meta_headers`
    maps response headers to `meta` keys, kept on success and on failure alike."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
        *,
        meta_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._transport = transport
        self._timeout = timeout or DEFAULT_TIMEOUT
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
            raise SampleError(
                "this provider's HTTP client belongs to another event loop; "
                "build one provider per event loop"
            )
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
        if wait is None:
            wait = _body_wait(body)
            if wait is not None and not math.isfinite(wait):
                wait = None
        raise SampleError(error_text(status, body), status=status, retry_after=wait)

    def _read_meta_headers(self, response: httpx.Response, reply: ReplyReader) -> None:
        for header, key in self._meta_headers.items():
            value = response.headers.get(header)
            if value:
                reply.meta[key] = value

    def _http_failure(self, e: httpx.HTTPError, url: str) -> SampleError:
        """The `SampleError` for an `httpx` error, with `e` as its cause. A `LocalProtocolError`
        is h11 refusing the request, and its text quotes the offending header value, which may be
        the API key: it gets a message of its own and no cause. A `ConnectError` names the
        server it could not reach (scheme, host and port of `url`: never its path or query,
        which may hold a key)."""
        if isinstance(e, httpx.LocalProtocolError):
            if "header" in str(e).lower():
                return SampleError(_BAD_HEADER)
            return SampleError("the request could not be sent: it is not valid HTTP")
        message = f"{type(e).__name__}: {e}"
        if isinstance(e, httpx.ConnectError):
            server = httpx.URL(url)
            message += f" (cannot reach {server.scheme}://{server.netloc.decode('ascii')})"
        error = (ProviderClosed if self._closed else SampleError)(message)
        error.__cause__ = e
        return error

    async def get_json(self, url: str, headers: Mapping[str, str]) -> object:
        """GET `url` and return the JSON it answers with, `None` for any answer that is not a
        200 holding JSON (a 404: the server has no such page), or `UNREACHED` when no answer
        came (the sample that follows will say why). `ProviderClosed` after `aclose()`."""
        client = self._client()
        try:
            response = await client.get(url, headers=dict(headers), timeout=PROBE_TIMEOUT)
        except httpx.HTTPError:
            return UNREACHED
        if response.status_code != HTTP_OK:
            return None
        try:
            return strict_loads(response.text)
        except ValueError:
            return None

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
        what `reply.close()` returns, carrying the meta that had arrived; an `httpx` error is its
        `__cause__`, so a retrying wrapper can test for `httpx.TransportError`. A request that
        cannot be written (a header HTTP cannot carry, a NaN in the body) is a `SampleError`
        whose message never holds the header's value. Once `reply.done` the reply is complete: a
        failure after it costs only the meta not yet read, and what `feed` returns is dropped.
        `end` is a data string that closes the stream (see `events`)."""
        client = self._client()
        content = _encode_request(headers, body)
        headers = {"content-type": "application/json", **headers}
        error: SampleError | None = None
        try:
            async with client.stream("POST", url, headers=headers, content=content) as response:
                self._read_meta_headers(response, reply)
                if response.status_code != HTTP_OK:
                    await self._refuse(response)
                async for event in events(response, end):
                    was_done = reply.done
                    items = reply.feed(event)
                    if not was_done:
                        for item in items:
                            yield item
        except httpx.HTTPError as e:
            error = self._http_failure(e, url)
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
        for part in _close(reply, error):
            yield part
        raise error


def _close(reply: ReplyReader, error: SampleError) -> list[Part | Delta]:
    """What `reply.close()` returns for a failed stream, with the raw text of a call it dropped
    (`meta["cut_off_call"]`, see `ReplyReader`) moved into `error`'s message."""
    leftover = reply.close()
    cut_off = reply.meta.pop("cut_off_call", None)
    if cut_off is not None:
        error.args = (f"{error}; a call was cut off and dropped: {cut_off}",)
    return leftover


def _encode_request(headers: Mapping[str, str], body: Json) -> bytes:
    """The request body as JSON bytes, or a `SampleError` that never quotes a header: a
    header must be ASCII (it may be the API key), and the body must hold no NaN, infinity or
    lone surrogate."""
    for name, value in headers.items():
        if not (name.isascii() and value.isascii()):
            raise SampleError(_BAD_HEADER)
    try:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    except ValueError as e:  # a UnicodeEncodeError is one
        raise SampleError(
            "the request body holds a value JSON can't carry (a NaN, an infinity, "
            "or a lone surrogate)"
        ) from e
