"""`RetryProvider`, the provider `tau` wraps around every other, used in a program of your own.

python examples/retry.py [--model NAME]

Setup: copy examples/config.json to ~/.tau/config.json (or point TAU_HOME at a directory
holding it). The model defaults to mock:echo, which never fails; the point of this file is
`RetryProvider`, which you wrap around the providers of your own program.

`tau`, `tau -p` and `tau serve` already do this (`start_session` wraps every provider, unless
`load: {retry: false}` or `tau ext disable retry` says not to). A program that builds its own
`Driver` decides for itself, as this one does: `{n: RetryProvider(p) for n, p in
builtin_providers(env).items()}`. Its `name` is the inner provider's, so it sits in the driver's
provider map in its place.

The core never retries (a cursor that does not move stops the auto-step; it does not try again),
and the driver treats a failed sample as a result. A retry has to be inside the sample, because
only there can an abort cancel the wait and only there is it known that nothing has been shown.
So it retries the whole sample, and only while nothing has been yielded: a part or a delta already
yielded was seen by the frontend, and a second try would show it twice.

- retried: HTTP 408, 429, 502, 503, 504, 529; a 500 that carries `retry_after`; a failure whose
  `__cause__` is an `httpx.TransportError` (a reset or refused connection, a timeout);
- never retried: `ProviderClosed`; a `SampleError` with no status and no transport cause (a
  conversion error, a bad walk); a plain 500, because a server such as llama.cpp answers 500 to a
  chat template or tool-call argument error.

It makes at most 3 retries, waiting `retry_after` seconds when the error says (over 60 fails at
once), else 1, 2 and 4 seconds, and yields a `Retrying` delta before each wait. Cancelling during
the wait ends the sample at once. The policy has no options; to change it, write your own wrapper
with this one as the model.
"""

import sys
import tempfile
from pathlib import Path

from common import close_all, rest, run

from tau import (
    Body,
    Driver,
    LoadedConfig,
    RetryProvider,
    Session,
    SystemClock,
    builtin_providers,
    model_config,
)


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
