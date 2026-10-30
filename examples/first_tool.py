"""The first tool round-trip: the model reads a file with a tool, and reports what it found.

python examples/first_tool.py http://192.168.1.100:8100/v1 <model id from /v1/models>
"""

import asyncio
import sys
import tempfile
from pathlib import Path

from tau_next import Body, Driver, Session, StepLimit, SystemClock, model_config, read_models, tool
from tau_next.providers.openai import OpenAIProvider

PHRASE = "violet anchor 42"


@tool(describe={"path": "path of the file to read"})
def read_file(path: str) -> str:
    """Read a text file and return its contents."""
    return Path(path).read_text()


def text(body: Body) -> str:
    match body:
        case Body.Assistant(parts=parts, stop=stop, finish_reason=reason):
            shown = " ".join(f"<{type(p).__name__}> {getattr(p, 'text', p)}" for p in parts)
            return f"{shown} [{stop} / {reason}]"
        case Body.Results(outcomes=outcomes):
            return "; ".join(f"{call}: {outcome}" for call, outcome, _ in outcomes)
        case Body.Config(config=config):
            return config.data
        case _:
            return str(getattr(body, "text", body))


async def main(base_url: str, model: str) -> None:
    clock = SystemClock()
    with tempfile.TemporaryDirectory() as tmp:
        secret = Path(tmp) / "secret.txt"
        secret.write_text(f"The secret phrase is: {PHRASE}\n")
        # Wave 3 reads a config file; until then the CLI layer holds the one model.
        models = read_models(cli={"models": {"example": {"model": model, "base_url": base_url}}})
        config = model_config("example")
        session = Session.create(config, "Use your tools when asked.", clock.now(), Path(tmp) / "s")
        # StepLimit bounds the loop of sample, tools, sample: a model that keeps asking stops.
        driver = Driver(
            session,
            OpenAIProvider(),
            models,
            tools=[read_file],
            extensions=[StepLimit(6)],
            clock=clock,
        )
        driver.start()
        cursor = session.cursors()[0]
        driver.send(cursor, "example", f"Read the file {secret} and tell me the secret phrase.")
        await driver.rest(cursor)
        for node in session.tree.walk(session.cursor(cursor).at):
            print(f"{type(node.body).__name__:<10} {node.by!r:<22} {text(node.body)}")
        await driver.stop()
        session.close()


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:3]))
