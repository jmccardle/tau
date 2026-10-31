"""A line chat over a `LocalClient`: the hands-on demo of commands, questions and completion.

python examples/chat.py [--model NAME]

Setup as for the other examples (examples/config.json; TAU_HOME names the directory holding
the config and the session, `chat-session/`, which is reopened on the next run). Try
`--model mock:echo`, which needs no server.

Type a message to send it. `/` alone lists the commands; Tab completes a command name and its
arguments (models, cursors, nodes, paths ...); a command missing arguments asks for each; an
unknown `/name` is sent to the model as text. The model's `word_count` tool asks permission
first, and the question is answered inline (an empty answer declines). Ctrl-D leaves.
"""

import asyncio
import json
import readline
import shlex
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Literal

from common import close_all, run

from tau_next import (
    Appended,
    Body,
    Busy,
    CallDraft,
    CallNamed,
    Client,
    CommandInfo,
    Cursor,
    CursorView,
    Driver,
    Live,
    LoadedConfig,
    LocalClient,
    NodeHandle,
    NodeId,
    Notice,
    OpenQuestion,
    Session,
    StepLimit,
    SystemClock,
    TextDelta,
    builtin_providers,
    model_config,
)
from tau_next.commandline import UnknownCommand, completions, parse

SYSTEM = "You are a terse assistant. Use word_count when asked about a file."


def word_count(path: Path) -> str:
    """Count the words in a file."""
    return f"{len(path.read_text().split())} words"


async def branch_from(cursor: Cursor, node: NodeId) -> None:
    """Go back to a node of this cursor's walk."""
    cursor.move(node)


class Demo:
    """Two commands, one of them also a tool, and a permission question before any tool."""

    name = "demo"
    tools = (word_count,)
    commands = (word_count, branch_from)

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        verdict = await node.ask(f"run {call.tool} {call.args_raw}?", Literal["allow", "deny"])
        return None if verdict == "allow" else "the user said no"


class Chat:
    """One client watching one cursor. `out` writes text as given; `ask` reads one line."""

    def __init__(
        self,
        client: Client,
        cursor: int,
        out: Callable[[str], None],
        ask: Callable[[str], Awaitable[str]],
    ) -> None:
        self.client, self.cursor, self.out, self.ask = client, cursor, out, ask
        self.commands: tuple[CommandInfo, ...] = ()
        self.question: OpenQuestion | None = None
        self.busy = False
        self.error: object = None

    async def listen(self) -> None:
        """Show what the client is sent, until its stream ends."""
        async for event in self.client:
            match event:
                case Live(cursor=c, delta=TextDelta(text=t)) if c == self.cursor:
                    self.out(t)
                case Live(cursor=c, delta=CallNamed(tool=t)) if c == self.cursor:
                    self.out(f"\n[calling {t}]\n")
                case Notice(text=t):
                    self.out(f"\n[{t}]\n")
                case Appended(nodes=nodes):
                    for n in nodes:
                        if isinstance(n.body, Body.Custom) and n.body.kind == "sample_error":
                            self.out(f"\n[sample failed: {n.body.data}]\n")
                case CursorView(cursor=c) if c == self.cursor:
                    self.show(event)
                case _:
                    pass

    def show(self, view: CursorView) -> None:
        if view.hook_error != self.error:
            self.error = view.hook_error
            if view.hook_error:
                self.out(
                    f"\n[{view.hook_error.point} of {view.hook_error.extension} failed: "
                    f"{view.hook_error.error}]\n"
                )
        if self.busy and not view.busy:
            self.out("\n")
        self.busy = view.busy
        question = view.questions[0] if view.questions else None
        if question != self.question and question is not None:
            choices = question.schema.get("enum")
            shown = f" [{'/'.join(map(str, choices))}]" if isinstance(choices, list) else ""
            self.out(f"\n? {question.text}{shown}\n")
        self.question = question

    async def line(self, text: str) -> None:
        """One line typed: an answer, a message, or a command."""
        try:
            if self.question is not None:
                await self.answer(self.question, text)
            elif not text.startswith("/"):
                await self.client.send(self.cursor, text)
            elif text.strip() == "/":
                self.out("".join(self.describe(c) for c in self.commands))
            else:
                await self.command(text)
        except UnknownCommand as e:
            self.out(f"[no command /{e.name}: sent as text]\n")
            await self.client.send(self.cursor, text)
        except (ValueError, LookupError, Busy) as e:
            self.out(f"[{e}]\n")

    async def answer(self, question: OpenQuestion, text: str) -> None:
        schema = question.schema
        value = text if schema.get("type") == "string" else json.loads(text)
        await self.client.answer(question.id, value or None)

    async def command(self, text: str) -> None:
        parsed = parse(text, self.commands)
        while parsed.missing:
            name = parsed.missing[0]
            if parsed.command.name == "config" and name == "values":
                form = await self.client.options(self.cursor, str(parsed.args["extension"]))
                self.out(f"current: {json.dumps(form.value)}\n")
            text += f" {name}={shlex.quote(await self.ask(f'{name}: '))}"
            parsed = parse(text, self.commands)
        await self.client.run(self.cursor, parsed.command.name, parsed.args)

    def describe(self, command: CommandInfo) -> str:
        required = command.schema.get("required", [])
        names = command.schema["properties"]
        params = " ".join(f"<{n}>" if n in required else f"[{n}]" for n in names)  # type: ignore[attr-defined]
        return f"/{command.name} {params}  {command.description.splitlines()[0]}\n"


def tab_completion(chat: Chat, loop: asyncio.AbstractEventLoop) -> None:
    """Tab completes through the client; readline calls this on its own thread."""

    def complete(text: str, state: int) -> str | None:
        line = readline.get_line_buffer()
        wanted = completions(line, chat.commands, chat.client, chat.cursor)
        found = asyncio.run_coroutine_threadsafe(wanted, loop).result()
        matches = [c for c in found if c.startswith(text)]
        return matches[state] if state < len(matches) else None

    readline.set_completer(complete)
    readline.set_completer_delims(" \t\n")
    readline.parse_and_bind("tab: complete")


async def main(name: str, loaded: LoadedConfig) -> None:
    clock = SystemClock()
    providers = builtin_providers(loaded.environ)
    where = loaded.home / "chat-session"
    where.parent.mkdir(parents=True, exist_ok=True)
    if where.exists():
        session = Session.load(where)
    else:
        session = Session.create(model_config(name), SYSTEM, clock.now(), where)
    driver = Driver(session, providers, loaded.catalog, extensions=[Demo(), StepLimit()])
    driver.start()
    client = LocalClient(driver, "chat:line")
    chat = Chat(client, session.cursors()[0], partial(print, end="", flush=True), prompt)
    chat.commands = await client.commands()
    await client.watch([chat.cursor])
    listener = asyncio.create_task(chat.listen())
    tab_completion(chat, asyncio.get_running_loop())
    try:
        while True:
            try:
                text = await prompt("> ")
            except EOFError:
                break
            if text.strip():
                await chat.line(text)
    finally:
        listener.cancel()
        await client.close()
        await driver.stop()
        session.close()
        await close_all(providers)


async def prompt(text: str) -> str:
    return await asyncio.to_thread(input, text)


if __name__ == "__main__":
    run(main)
