"""The first real call: one turn against an OpenAI-compatible server, printed.

python examples/first_call.py http://192.168.1.100:8100/v1 <model id from /v1/models>
"""

import asyncio
import sys
import tempfile
from pathlib import Path

from tau_next import Body, Driver, Session, SystemClock, model_config, read_models
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
        # Wave 3 reads a config file; until then the CLI layer holds the one model.
        models = read_models(cli={"models": {"example": {"model": model, "base_url": base_url}}})
        session = Session.create(
            model_config("example"), "You are terse.", clock.now(), Path(tmp) / "session"
        )
        driver = Driver(session, OpenAIProvider(), models, clock=clock)
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
