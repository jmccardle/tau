"""Prompt-cache hit rate over a conversation: four turns, each reply's usage printed.

python examples/cache_rate.py [--model NAME]

Setup: copy examples/config.json to ~/.tau/config.json (or point TAU_HOME at a
directory holding it). --model takes a qualified name (local:qwen) or a bare one;
without it the config's default_model is used.

The system prompt is long (about 7k tokens) so the prefix clears every provider's
minimum cacheable length. Turn 1 can only write the cache; from turn 2 on, the shared
prefix should be read from it. `hit` is `cached / input` from the reply's normalized
`usage`; `input` already counts the cached tokens.
"""

import json
import tempfile
from pathlib import Path

from common import close_all, rest, run

from tau import (
    Body,
    Driver,
    LoadedConfig,
    Part,
    Session,
    SystemClock,
    builtin_providers,
    model_config,
)

QUESTIONS = ("What is item 7's colour?", "And item 300's?", "And item 212's?", "And item 9's?")
COLOURS = ("red", "green", "blue", "amber", "violet", "teal", "ochre", "grey")


def system_prompt() -> str:
    """A long, fixed system prompt: an inventory to answer questions from."""
    items = "\n".join(
        f"Item {i}: a {COLOURS[i * 7 % len(COLOURS)]} widget, shelf {i % 40}, bin {i * 13 % 97}."
        for i in range(1, 301)
    )
    return f"You answer questions about this inventory in five words or fewer.\n\n{items}"


async def main(name: str, loaded: LoadedConfig) -> None:
    clock = SystemClock()
    providers = builtin_providers(loaded.environ)
    total = {"input": 0, "cached": 0}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(
                model_config(name), system_prompt(), clock.now(), Path(tmp) / "session"
            )
            driver = Driver(session, providers, loaded.catalog, clock=clock)
            driver.start()
            cursor = session.cursors()[0]
            print(f"{'turn':<5} {'input':>7} {'cached':>7} {'hit':>5}  reply")
            for turn, question in enumerate(QUESTIONS, 1):
                await driver.client("example").send(cursor, question)
                await rest(driver, session, cursor)
                reply = session.tree.node(session.cursor(cursor).at).body
                assert isinstance(reply, Body.Assistant)
                usage = json.loads(reply.meta or "{}").get("usage") or {}
                said = " ".join(p.text for p in reply.parts if isinstance(p, Part.Text))
                if not usage:
                    print(f"{turn:<5} {'(no usage reported)':>21}  {said[:40]!r}")
                    continue
                for key in total:
                    total[key] += usage[key]
                hit = usage["cached"] / usage["input"] if usage["input"] else 0.0
                print(
                    f"{turn:<5} {usage['input']:>7} {usage['cached']:>7} {hit:>5.0%}  {said[:40]!r}"
                )
            if total["input"]:
                print(
                    f"{'all':<5} {total['input']:>7} {total['cached']:>7} "
                    f"{total['cached'] / total['input']:>5.0%}"
                )
            await driver.stop()
            session.close()
    finally:
        await close_all(providers)


if __name__ == "__main__":
    run(main)
