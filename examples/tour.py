"""A hands-on tour: base tools, an overflowing tool result, a branch, and a reload.

python examples/tour.py [--model NAME]

Setup: copy examples/config.json to ~/.tau/config.json (or point TAU_HOME at a
directory holding it). --model takes a qualified name (local:qwen) or a bare one;
without it the config's default_model is used.

1. The model uses the Rust `bash` tool on output past the 10 kB limit, so it sees
   the tail plus an overflow file name (`/tmp/tau-<uid>/<id>.out`).
2. We go back to the node before that question and ask something else: the
   session now holds two branches under one node.
3. We close the session and load it from disk: the same tree comes back.
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
    base_tools,
    load_config,
    model_config,
    prompts,
    select_model,
)
from tau_next.providers.openai import OpenAIProvider


def show(body: Body) -> str:
    match body:
        case Body.Assistant(parts=parts):
            return " ".join(f"<{type(p).__name__}> {getattr(p, 'text', p)}" for p in parts)[:160]
        case Body.Results(outcomes=outcomes):
            return "; ".join(str(outcome)[:160] for _, outcome, _ in outcomes)
        case _:
            return str(getattr(body, "text", getattr(body, "config", body)))[:160]


def print_walk(session: Session, at: int) -> None:
    for node in session.tree.walk(at):
        print(f"  {node.id:>3} {type(node.body).__name__:<10} {show(node.body)}")


async def ask(driver: Driver, cursor: int, text: str) -> None:
    driver.send(cursor, "tour", text)
    await driver.rest(cursor)


async def main(name: str, loaded: LoadedConfig) -> None:
    clock = SystemClock()
    with tempfile.TemporaryDirectory() as tmp:
        where = Path(tmp) / "session"
        session = Session.create(
            model_config(name),
            "You are a terse assistant. Use your tools when asked.",
            clock.now(),
            where,
        )
        tools = base_tools(tmp)
        driver = Driver(
            session,
            OpenAIProvider(environ=loaded.environ),
            loaded.catalog,
            tools=tools,
            extensions=[StepLimit(6)],
            clock=clock,
        )
        driver.start()
        cursor = session.cursors()[0]

        print("== 1. bash with output past the 10 kB limit")
        before = session.cursor(cursor).at
        await ask(driver, cursor, "Run `seq 1 5000` with bash and tell me the last number you see.")
        print_walk(session, session.cursor(cursor).at)
        first_branch = session.cursor(cursor).at

        print(f"\n== 2. go back to node {before} and ask something else")
        driver.go_to(cursor, before, "tour")
        await ask(driver, cursor, "Use bash to print the current date, then tell me the weekday.")
        print_walk(session, session.cursor(cursor).at)
        print(f"\n  node {before} now has children {session.tree.children(before)}")
        meet = session.tree.lca(first_branch, session.cursor(cursor).at)
        print(f"  the branches meet at node {meet}")

        await driver.stop()
        size = len(session.tree)
        session.close()

        print("\n== 3. reload from disk")
        again = Session.load(where)
        at = again.cursor(again.cursors()[0]).at
        print(f"  {len(again.tree)} nodes (was {size}); cursor at {at}")
        again.close()


def choose_model(argv: list[str]) -> tuple[str, LoadedConfig]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="qualified or bare model name (default: default_model)")
    args = parser.parse_args(argv)
    ask = prompts(sys.stdin, sys.stdout)
    loaded = load_config(Path.cwd(), os.environ, ask=ask.ask)
    return select_model(loaded.catalog, args.model, pick=ask.pick), loaded


if __name__ == "__main__":
    asyncio.run(main(*choose_model(sys.argv[1:])))
