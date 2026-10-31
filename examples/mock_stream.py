"""Live events: a reply streams in delta by delta, then arrives whole as an appended node.

python examples/mock_stream.py [--model NAME]

Setup: copy examples/config.json to ~/.tau/config.json (or point TAU_HOME at a
directory holding it). The model defaults to mock:echo, which echoes the message with no
network; `--model local:qwen` streams the same question from a real server, thinking
deltas included.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

from common import close_all, rest, run

from tau_next import (
    Appended,
    Body,
    Driver,
    Live,
    LoadedConfig,
    LocalClient,
    Session,
    SystemClock,
    TextDelta,
    ThinkingDelta,
    builtin_providers,
    model_config,
)


async def main(name: str, loaded: LoadedConfig) -> None:
    clock = SystemClock()
    providers = builtin_providers(loaded.environ)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(
                model_config(name), "You are terse.", clock.now(), Path(tmp) / "s"
            )
            driver = Driver(session, providers, loaded.catalog, clock=clock)
            driver.start()
            cursor = session.cursors()[0]
            client = LocalClient(driver, "example")
            await client.watch([cursor])

            async def show() -> None:
                async for event in client:
                    match event:
                        case Live(delta=TextDelta(text=text)):
                            print(f"live  text      {text!r}")
                        case Live(delta=ThinkingDelta(text=text)):
                            print(f"live  thinking  {text!r}")
                        case Live(delta=delta):
                            print(f"live  {delta}")
                        case Appended(nodes=nodes):
                            print(f"appended {[type(n.body).__name__ for n in nodes]}")

            shown = asyncio.ensure_future(show())
            await client.send(cursor, "What is 17 times 3? Answer briefly.")
            await rest(driver, session, cursor)
            reply = session.tree.node(session.cursor(cursor).at).body
            await driver.stop()  # ends the event stream after what it holds
            await shown
            assert isinstance(reply, Body.Assistant)
            print(f"reply {reply.parts}\nmeta  {reply.meta}")
            session.close()
    finally:
        await close_all(providers)


if __name__ == "__main__":
    if "--model" not in sys.argv:
        sys.argv += ["--model", "mock:echo"]
    run(main)
