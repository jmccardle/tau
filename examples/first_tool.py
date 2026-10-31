"""The first tool round-trip: the model reads a file with a tool, and reports what it found.

python examples/first_tool.py [--model NAME]

Setup: copy examples/config.json to ~/.tau/config.json (or point TAU_HOME at a
directory holding it). --model takes a qualified name (local:qwen) or a bare one;
without it the config's default_model is used.
"""

import tempfile
from pathlib import Path
from typing import Annotated

from common import close_all, rest, run

from tau import (
    Body,
    Driver,
    LoadedConfig,
    Session,
    StepLimit,
    SystemClock,
    builtin_providers,
    model_config,
)

PHRASE = "violet anchor 42"


def read_file(path: Annotated[str, "path of the file to read"]) -> str:
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


async def main(name: str, loaded: LoadedConfig) -> None:
    clock = SystemClock()
    providers = builtin_providers(loaded.environ)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "secret.txt"
            secret.write_text(f"The secret phrase is: {PHRASE}\n")
            config = model_config(name, {"step_limit": {"limit": 6}})
            session = Session.create(
                config, "Use your tools when asked.", clock.now(), Path(tmp) / "s"
            )
            # StepLimit bounds the loop of sample, tools, sample: a model that keeps asking stops.
            driver = Driver(
                session,
                providers,
                loaded.catalog,
                tools=[read_file],
                extensions=[StepLimit()],
                clock=clock,
            )
            driver.start()
            cursor = session.cursors()[0]
            await driver.client("example").send(
                cursor, f"Read the file {secret} and tell me the secret phrase."
            )
            await rest(driver, session, cursor)
            for node in session.tree.walk(session.cursor(cursor).at):
                print(f"{type(node.body).__name__:<10} {node.by!r:<22} {text(node.body)}")
            await driver.stop()
            session.close()
    finally:
        await close_all(providers)


if __name__ == "__main__":
    run(main)
