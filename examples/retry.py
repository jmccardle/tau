"""A provider that retries a failed sample, wrapped around any other provider.

python examples/retry.py [--model NAME]

Setup: copy examples/config.json to ~/.tau/config.json (or point TAU_HOME at a directory
holding it). The model defaults to mock:echo, which never fails; the point of this file is
`RetryProvider`, which you can wrap around the providers of your own program.

Retries are not built in, and this is why. The core never retries (a cursor that does not move
stops the auto-step; it does not try again), and the driver treats a failed sample as a result:
the reply is saved as failed, with whatever arrived. A retry policy is a choice (how many
attempts, which statuses, how long to wait, whether a person is watching), and the choices
differ per program. So it is an ordinary `Provider` that holds another one, and the program
that wants retries builds it: `{n: RetryProvider(p) for n, p in builtin_providers(env).items()}`.
Its `name` is the inner provider's, so it sits in the driver's provider map in its place.

What it retries, always the whole sample and only while nothing has been yielded: a part or a
delta already yielded was seen by the frontend, and a second try would show it twice. After
one, the error propagates and the driver keeps what arrived as a failed reply.

- retried: HTTP 408, 429, 502, 503, 529; a 500 that carries `retry_after`; a failure whose
  `__cause__` is an `httpx.TransportError` (a reset or refused connection, a timeout);
- never retried: `ProviderClosed`; a `SampleError` with no status and no transport cause (a
  conversion error, a bad walk: the same request fails the same way); a plain 500, because a
  server such as llama.cpp answers 500 to a chat template or tool-call argument error.

It waits `retry_after` seconds when the error says, else 1 s, 2 s, 4 s, ..., each at most
`max_wait`. Cancelling during the wait ends the sample at once.
"""

import asyncio
import sys
import tempfile
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from pathlib import Path

import httpx
from common import close_all, rest, run

from tau_next import (
    Body,
    Delta,
    Driver,
    Finish,
    LoadedConfig,
    ModelConfig,
    Node,
    Part,
    Provider,
    ProviderClosed,
    SampleError,
    Session,
    SystemClock,
    Tool,
    builtin_providers,
    model_config,
)

_RETRY_STATUSES = frozenset({408, 429, 502, 503, 529})
_SERVER_ERROR = 500


def retryable(error: SampleError) -> bool:
    """Whether the same request could succeed on a later try."""
    if isinstance(error, ProviderClosed):
        return False
    if error.status == _SERVER_ERROR:
        return error.retry_after is not None
    if error.status is not None:
        return error.status in _RETRY_STATUSES
    return isinstance(error.__cause__, httpx.TransportError)


class RetryProvider:
    """`inner`, with up to `attempts` tries per sample. `sleep` is how it waits (seconds)."""

    def __init__(
        self,
        inner: Provider,
        *,
        attempts: int = 3,
        max_wait: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if attempts < 1:
            raise ValueError(f"attempts must be at least 1, got {attempts}")
        if max_wait < 0:
            raise ValueError(f"max_wait must not be negative, got {max_wait}")
        self._inner = inner
        self._attempts = attempts
        self._max_wait = max_wait
        self._sleep = sleep

    @property
    def name(self) -> str:
        return self._inner.name

    def check(self, model: ModelConfig) -> list[str]:
        return self._inner.check(model)

    async def aclose(self) -> None:
        await self._inner.aclose()

    def wait_for(self, error: SampleError, attempt: int) -> float:
        """Seconds to wait after the `attempt`th failure (1-based)."""
        wanted = error.retry_after if error.retry_after is not None else 2.0 ** (attempt - 1)
        return min(wanted, self._max_wait)

    async def sample(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> AsyncGenerator[Part | Finish | Delta, None]:
        attempt = 1
        while True:
            yielded = False
            stream = self._inner.sample(walk, model, tools)
            try:
                async for item in stream:
                    yielded = True
                    yield item
                return
            except SampleError as e:
                if yielded or attempt >= self._attempts or not retryable(e):
                    raise
                wait = self.wait_for(e, attempt)
            finally:
                await stream.aclose()
            await self._sleep(wait)
            attempt += 1


async def main(name: str, loaded: LoadedConfig) -> None:
    clock = SystemClock()
    providers = {n: RetryProvider(p) for n, p in builtin_providers(loaded.environ).items()}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(
                model_config(name), "You are terse.", clock.now(), Path(tmp) / "s"
            )
            driver = Driver(session, providers, loaded.catalog, clock=clock)
            driver.start()
            cursor = session.cursors()[0]
            await driver.client("example").send(cursor, "What is 17 times 3? Answer briefly.")
            await rest(driver, session, cursor)
            reply = session.tree.node(session.cursor(cursor).at).body
            await driver.stop()
            assert isinstance(reply, Body.Assistant)
            print(f"reply {reply.parts}\nmeta  {reply.meta}")
            session.close()
    finally:
        await close_all(providers)


if __name__ == "__main__":
    if "--model" not in sys.argv:
        sys.argv += ["--model", "mock:echo"]
    run(main)
