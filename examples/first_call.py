"""The first real call: one turn against an OpenAI-compatible server, printed.

python examples/first_call.py [--model NAME]

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
    SystemClock,
    load_config,
    model_config,
    prompts,
    select_model,
)
from tau_next.providers.openai import OpenAIProvider


def text(body: Body) -> str:
    match body:
        case Body.Assistant(parts=parts):
            return " ".join(f"<{type(p).__name__}> {getattr(p, 'text', p)}" for p in parts)
        case Body.Config(config=config):
            return config.data
        case _:
            return str(getattr(body, "text", body))


async def main(name: str, loaded: LoadedConfig) -> None:
    clock = SystemClock()
    with tempfile.TemporaryDirectory() as tmp:
        session = Session.create(
            model_config(name), "You are terse.", clock.now(), Path(tmp) / "session"
        )
        driver = Driver(
            session, OpenAIProvider(environ=loaded.environ), loaded.catalog, clock=clock
        )
        driver.start()
        cursor = session.cursors()[0]
        driver.send(cursor, "example", "hello")
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
