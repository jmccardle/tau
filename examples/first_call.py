"""The first real call: one turn against an OpenAI-compatible server, printed.

python examples/first_call.py [--model NAME]

Setup: copy examples/config.json to ~/.tau/config.json (or point TAU_HOME at a
directory holding it). --model takes a qualified name (local:qwen) or a bare one;
without it the config's default_model is used.
"""

import tempfile
from pathlib import Path

from common import close_all, rest, run

from tau_next import (
    Body,
    Driver,
    LoadedConfig,
    Session,
    SystemClock,
    builtin_providers,
    model_config,
)


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
    providers = builtin_providers(loaded.environ)
    with tempfile.TemporaryDirectory() as tmp:
        session = Session.create(
            model_config(name), "You are terse.", clock.now(), Path(tmp) / "session"
        )
        driver = Driver(session, providers, loaded.catalog, clock=clock)
        driver.start()
        cursor = session.cursors()[0]
        driver.send(cursor, "example", "hello")
        await rest(driver, session, cursor)
        for node in session.tree.walk(session.cursor(cursor).at):
            print(f"{type(node.body).__name__:<10} {node.by!r:<22} {text(node.body)}")
        await driver.stop()
        await close_all(providers)
        session.close()


if __name__ == "__main__":
    run(main)
