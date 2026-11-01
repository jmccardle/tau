"""What the examples share: choosing a model from the config, and failing loudly.

Each example is `python examples/X.py [--model NAME]`; copy examples/config.json to
~/.tau/config.json (or point TAU_HOME at a directory holding it) first.
"""

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path

from tau import (
    Body,
    ConfigError,
    DotenvError,
    Driver,
    LoadedConfig,
    Provider,
    Session,
    TrustError,
    load_config,
    prompts,
    safe_block,
    select_model,
)

_INTERRUPTED = 130


class SampleFailed(Exception):
    """A sample failed: the session holds a `sample_error` node saying why."""


def choose_model(
    argv: list[str], parents: Sequence[argparse.ArgumentParser] = ()
) -> tuple[str, LoadedConfig]:
    """`--model NAME` (qualified or bare; default: the config's `default_model`) and the
    loaded config. Asks a live terminal to trust a project or to pick among matches. `parents`
    are parsers (`add_help=False`) whose options the program reads for itself."""
    parser = argparse.ArgumentParser(parents=parents)
    parser.add_argument("--model", help="qualified or bare model name (default: default_model)")
    args = parser.parse_args(argv)
    ask = prompts(sys.stdin, sys.stdout)
    loaded = load_config(Path.cwd(), os.environ, ask=ask.ask)
    return select_model(loaded.catalog, args.model, pick=ask.pick), loaded


async def rest(driver: Driver, session: Session, cursor: int) -> None:
    """`driver.rest(cursor)`, then `SampleFailed` if any sample in the session failed."""
    await driver.rest(cursor)
    for i in range(len(session.tree)):
        match session.tree.node(i).body:
            case Body.Custom(kind="sample_error", data=data):
                raise SampleFailed(f"the sample failed: {data}")
            case _:
                pass


async def close_all(providers: Mapping[str, Provider]) -> None:
    """The program that built the providers closes them."""
    for provider in providers.values():
        await provider.aclose()


def run_until_signalled(main: Coroutine[object, object, None]) -> None:
    """`asyncio.run(main)`, where SIGTERM and SIGHUP (a closed terminal) end `main` as SIGINT
    does: by cancelling it, so a running `bash` command is killed on the way out. Without
    this the process dies at once, the command is left running in its own session, and its
    timeout never fires. Any program that runs base tools must do the same. Exits 128 + the
    signal number."""
    caught: list[int] = []

    async def guarded() -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None

        def stop(number: int) -> None:
            caught.append(number)
            task.cancel()

        for number in (signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(number, stop, number)
        await main

    try:
        asyncio.run(guarded())
    except asyncio.CancelledError:
        if not caught:
            raise
        sys.exit(128 + caught[0])


def run(
    main: Callable[[str, LoadedConfig], Coroutine[object, object, None]],
    parents: Sequence[argparse.ArgumentParser] = (),
) -> None:
    """Choose a model, run `main(name, loaded)`; on a failure print one line and exit 1.
    `parents` are as for `choose_model`."""
    try:
        name, loaded = choose_model(sys.argv[1:], parents)
        run_until_signalled(main(name, loaded))
    except (ConfigError, TrustError, DotenvError, SampleFailed) as e:
        sys.exit(f"{Path(sys.argv[0]).name}: {safe_block(str(e))}")
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(_INTERRUPTED)
