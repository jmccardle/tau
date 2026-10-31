"""A tool's progress: watch `bash` output arrive live while the command is still running.

python examples/bash_progress.py

No config, no network. The "model" is a small provider defined here (the public `Provider`
protocol): its first reply calls `bash` on a slow command, its second says it is done. The
mock provider only produces text, so it cannot make a tool call; this one can.

Each line is stamped with the seconds since the command was sent. `a` shows at about 0 s,
`b` at about 1 s, `c` at about 2 s, and the result is saved only after `c`.
"""

import asyncio
import json
import tempfile
import time
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path

from tau import (
    Appended,
    Body,
    Delta,
    Driver,
    Finish,
    Live,
    ModelConfig,
    Node,
    Part,
    Session,
    Stop,
    SystemClock,
    Tool,
    ToolProgress,
    base_tools,
    model_config,
    read_models,
)

COMMAND = "echo a; sleep 1; echo b; sleep 1; echo c"


class Scripted:
    """A provider that calls `bash` on the first sample and answers on the second."""

    name = "scripted"

    def __init__(self) -> None:
        self._samples = 0

    def check(self, model: ModelConfig) -> list[str]:
        return []

    async def sample(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> AsyncGenerator[Part | Finish | Delta, None]:
        self._samples += 1
        if self._samples == 1:
            yield Part.Call("call-1", "bash", json.dumps({"command": COMMAND}))
            yield Finish(Stop.Tools)
        else:
            yield Part.Text("The command finished.")
            yield Finish(Stop.End)

    async def aclose(self) -> None:
        """Nothing is held open."""


async def main() -> None:
    clock = SystemClock()
    catalog = read_models(cli={"models": {"m": {"model": "m", "provider": "scripted"}}})
    provider = Scripted()
    with tempfile.TemporaryDirectory() as tmp:
        session = Session.create(
            model_config("m"), "You run commands.", clock.now(), Path(tmp) / "s"
        )
        tools = base_tools(tmp, overflow_dir=Path(tmp) / "overflow")
        driver = Driver(session, {"scripted": provider}, catalog, tools=tools, clock=clock)
        driver.start()
        cursor = session.cursors()[0]
        client = driver.client("example")
        await client.watch([cursor])
        start = time.monotonic()

        async def show() -> None:
            async for event in client:
                at = f"{time.monotonic() - start:4.1f}s"
                match event:
                    case Live(delta=ToolProgress(call_id=call_id, text=text)):
                        print(f"{at}  live   {call_id}  {text!r}")
                    case Appended(nodes=nodes):
                        print(f"{at}  saved  appended {[type(n.body).__name__ for n in nodes]}")

        shown = asyncio.ensure_future(show())
        await client.send(cursor, "run it")
        await driver.rest(cursor)
        for node in session.tree.walk(session.cursor(cursor).at):
            if isinstance(node.body, Body.Results):
                print(f"result  {node.body.outcomes}")
        await driver.stop()  # ends the event stream after what it holds
        await shown
        session.close()


if __name__ == "__main__":
    asyncio.run(main())
