"""`RetryProvider`: a provider that tries a failed sample again (D24 section 4).

It is an ordinary `Provider` that holds another one, and `start_session` wraps every provider in
it unless `load` turns `retry` off (`load: {retry: false}`, or `tau ext disable retry`): a proxy
that already retries, or a provider being debugged, wants it off. A program that builds its own
`Driver` decides for itself: `{n: RetryProvider(p) for n, p in builtin_providers(env).items()}`.
Its `name` is the inner provider's, so it sits in the provider map in its place.

The policy is fixed, and has no options:

- it retries only while the inner provider has yielded nothing: a part or a delta already yielded
  was seen by a frontend, and a second try would show it twice (after one, the error propagates
  and the driver keeps what arrived as a failed reply);
- it retries HTTP 408, 429, 502, 503, 504 and 529, a 500 that carries `Retry-After`, and a
  failure whose `__cause__` is an `httpx.TransportError` (a reset or refused connection, a
  timeout) before any response;
- it never retries `ProviderClosed`, a `SampleError` with no status and no transport cause (a
  conversion error, a bad walk: the same request fails the same way), or a plain 500, because a
  server such as llama.cpp answers 500 to a chat template or tool-call argument error;
- it makes at most 3 retries, waiting the server's `Retry-After` if it gave one, else 1, 2 and
  4 seconds. A `Retry-After` over 60 seconds fails at once, with the wait in the message.

Before each wait it yields a `Retrying` delta (which does not count as output for the first
rule). The wait is part of the sample, so cancelling the sample (an abort) ends it at once.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence

import httpx

from tau._config import ModelConfig
from tau._core import Node, Part
from tau._events import Delta, Retrying
from tau._protocols import Finish, Provider, ProviderClosed, SampleError, Tool

RETRIES = 3
"""Retries after the first try."""
MAX_WAIT = 60.0
"""The longest `Retry-After` waited for, in seconds; a longer one fails the sample."""
_STATUSES = frozenset({408, 429, 502, 503, 504, 529})
_SERVER_ERROR = 500
_REASON_CHARS = 200


def retryable(error: SampleError) -> bool:
    """Whether the same request could succeed on a later try."""
    if isinstance(error, ProviderClosed):
        return False
    if error.status == _SERVER_ERROR:
        return error.retry_after is not None
    if error.status is not None:
        return error.status in _STATUSES
    return isinstance(error.__cause__, httpx.TransportError)


def _reason(error: SampleError) -> str:
    """The first line of the error, cut to a line a frontend can show."""
    lines = str(error).splitlines()
    line = lines[0] if lines else type(error).__name__
    return line if len(line) <= _REASON_CHARS else line[: _REASON_CHARS - 1] + "…"


def _wait_for(error: SampleError, retry: int) -> float:
    """Seconds to wait before retry number `retry` (1-based); `SampleError` for a `Retry-After`
    longer than `MAX_WAIT`."""
    if error.retry_after is None:
        return float(2 ** (retry - 1))
    if error.retry_after > MAX_WAIT:
        raise SampleError(
            f"{error}; the server asks to wait {error.retry_after:g} s, over the {MAX_WAIT:g} s "
            "limit, so tau does not retry",
            error.meta,
            status=error.status,
            retry_after=error.retry_after,
        ) from error
    return error.retry_after


class RetryProvider:
    """`inner`, with up to 3 retries per sample (the module docstring). `sleep` is how it waits
    (seconds); a test passes its own."""

    def __init__(
        self,
        inner: Provider,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._inner = inner
        self._sleep = sleep

    @property
    def name(self) -> str:
        """The inner provider's name."""
        return self._inner.name

    def check(self, model: ModelConfig) -> list[str]:
        """The inner provider's check."""
        return self._inner.check(model)

    async def aclose(self) -> None:
        """Close the inner provider."""
        await self._inner.aclose()

    async def sample(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> AsyncGenerator[Part | Finish | Delta, None]:
        """The inner provider's sample, tried again as the module says."""
        retry = 0
        while True:
            output = False
            stream = self._inner.sample(walk, model, tools)
            try:
                async for item in stream:
                    output = output or not isinstance(item, Retrying)
                    yield item
                return
            except SampleError as e:
                if output or retry >= RETRIES or not retryable(e):
                    raise
                retry += 1
                wait = _wait_for(e, retry)
                why = _reason(e)
            finally:
                await stream.aclose()
            yield Retrying(retry, RETRIES, wait, why)
            await self._sleep(wait)
