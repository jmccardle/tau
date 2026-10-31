"""A one-off LLM call: no driver, no cursor, no saved session. Ask once, print the reply.

python examples/one_off.py [--model NAME]

Setup: copy examples/config.json to ~/.tau/config.json (or point TAU_HOME at a
directory holding it). Try `--model mock:echo` for no network.

The recipe (there is no helper; it is the existing API):

1. `Session.create(config, system, now)` with no directory: the session lives in memory.
2. `session.append(...)` a `Body.User` node under the root.
3. `provider.sample(session.tree.walk(node), model, [])`: the same stream the driver reads
   (deltas, whole parts, one `Finish`). Call `provider.check(model)` first, as the driver
   does: `sample` assumes it passed. `[]` offers no tools.

A failure raises `SampleError`; nothing here retries (see examples/retry.py).
"""

import asyncio
import sys

from common import choose_model, close_all

from tau import (
    Author,
    Body,
    Finish,
    Part,
    Session,
    SystemClock,
    builtin_providers,
    model_config,
)


async def ask(argv: list[str]) -> None:
    name, loaded = choose_model(argv)
    clock = SystemClock()
    providers = builtin_providers(loaded.environ)
    try:
        model = loaded.catalog.resolve(name)
        provider = providers[model.provider]
        if problems := provider.check(model):
            raise SystemExit("\n".join(problems))
        session = Session.create(model_config(name), "You are terse.", clock.now())
        root = session.cursor(session.cursors()[0]).at
        question = Body.User("Say hello in five words.")
        node = session.append(root, question, Author.Frontend("one_off"), clock.now())
        async for item in provider.sample(session.tree.walk(node), model, []):
            match item:
                case Part.Text(text=text):
                    print(text)
                case Finish(stop=stop, meta=meta):
                    print(f"[{stop}] {meta}", file=sys.stderr)
                case _:
                    pass  # deltas and thinking: this recipe waits for whole parts
        session.close()
    finally:
        await close_all(providers)


if __name__ == "__main__":
    asyncio.run(ask(sys.argv[1:]))
