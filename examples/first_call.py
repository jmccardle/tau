"""The first real call: one turn against an OpenAI-compatible server, printed.

python examples/first_call.py http://192.168.1.100:8100/v1 <model id from /v1/models>
"""

import asyncio
import sys
import tempfile
from pathlib import Path

from tau_next import Body, Driver, ModelConfig, Session, SystemClock
from tau_next.providers.openai import OpenAIProvider


def text(body: Body) -> str:
    match body:
        case Body.Assistant(parts=parts):
            return " ".join(f"<{type(p).__name__}> {getattr(p, 'text', p)}" for p in parts)
        case Body.Config(config=config):
            return config.data
        case _:
            return str(getattr(body, "text", body))


async def main(base_url: str, model: str) -> None:
    clock = SystemClock()
    with tempfile.TemporaryDirectory() as tmp:
        config = ModelConfig(model, base_url).to_config()
        session = Session.create(config, "You are terse.", clock.now(), Path(tmp) / "session")
        driver = Driver(session, OpenAIProvider(), clock=clock)
        driver.start()
        cursor = session.cursors()[0]
        driver.send(cursor, "example", "hello")
        await driver.rest(cursor)
        for node in session.tree.walk(session.cursor(cursor).at):
            print(f"{type(node.body).__name__:<10} {node.by!r:<22} {text(node.body)}")
        await driver.stop()
        session.close()


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:3]))
