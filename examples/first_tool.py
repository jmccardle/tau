"""The first tool round-trip: the model reads a file with a tool, and reports what it found.

python examples/first_tool.py [--model NAME]

Setup: copy examples/config.json to ~/.tau/config.json (or point TAU_HOME at a
directory holding it). --model takes a qualified name (local:qwen) or a bare one;
without it the config's default_model is used.
"""

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from tau_next import (
    Body,
    Driver,
    LoadedConfig,
    Session,
    StepLimit,
    SystemClock,
    load_config,
    model_config,
    prompts,
    select_model,
    tool,
)
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


async def main(name: str, loaded: LoadedConfig) -> None:
    clock = SystemClock()
    with tempfile.TemporaryDirectory() as tmp:
        secret = Path(tmp) / "secret.txt"
        secret.write_text(f"The secret phrase is: {PHRASE}\n")
        config = model_config(name)
        session = Session.create(config, "Use your tools when asked.", clock.now(), Path(tmp) / "s")
        # StepLimit bounds the loop of sample, tools, sample: a model that keeps asking stops.
        driver = Driver(
            session,
            OpenAIProvider(environ=loaded.environ),
            loaded.catalog,
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


def choose_model(argv: list[str]) -> tuple[str, LoadedConfig]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="qualified or bare model name (default: default_model)")
    args = parser.parse_args(argv)
    ask = prompts(sys.stdin, sys.stdout)
    loaded = load_config(Path.cwd(), os.environ, ask=ask.ask)
    return select_model(loaded.catalog, args.model, pick=ask.pick), loaded


if __name__ == "__main__":
    asyncio.run(main(*choose_model(sys.argv[1:])))
