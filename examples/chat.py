"""A line chat over a `LocalClient`: the hands-on demo of commands, questions and completion.

python examples/chat.py [--model NAME]

Setup as for the other examples (examples/config.json; TAU_HOME names the directory holding
the config and the session, `chat-session/`, which is reopened on the next run). Try
`--model mock:echo`, which needs no server.

Type a message to send it. `/` alone lists the commands; Tab completes a command name and its
arguments (models, cursors, nodes, paths ...); a command missing arguments asks for each; an
unknown `/name` is sent to the model as text. `/config step_limit limit=10` changes an
extension's options on this branch (add `--save` to write them to your config file for every
session), and a bare `/config step_limit` shows them. The model's
`word_count` tool asks permission first, and the question is answered inline, completed with
Tab like an argument (an empty answer declines). A command runs in the background once its
arguments are in, so a question it asks is answered at the prompt like any other; every open
question is shown, and a line answers the first. A message someone else wrote to the cursor
being watched (an extension that sent to it, another client) is shown under their name:
`[gamemaster] text`; what is written to other cursors is not. Ctrl-D leaves.
"""

import asyncio
import readline
import shlex
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Literal

from common import close_all, run

from tau import (
    Appended,
    Author,
    Body,
    Busy,
    CallDraft,
    CallNamed,
    Client,
    CommandFailed,
    Cursor,
    CursorView,
    Driver,
    Form,
    Live,
    LoadedConfig,
    Node,
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
from tau.commandline import Parsed, UnknownCommand, completions, parse, value, values

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
        self.commands: tuple[Form, ...] = ()
        self.options: dict[str, Form] = {}
        self.questions: tuple[OpenQuestion, ...] = ()
        self.busy = False
        self.error: object = None
        self.unplaced: list[Node] = []  # `User` nodes by others, not yet known to be on our branch
        self.running: set[asyncio.Task[None]] = set()  # commands started and not yet finished
        self.failure: BaseException | None = None  # what ended a command task unexpectedly

    @property
    def question(self) -> OpenQuestion | None:
        """The one a typed line answers: the first open."""
        return self.questions[0] if self.questions else None

    async def refresh(self) -> None:
        """Ask for the commands, and for the options of each extension `/config` names."""
        self.commands = await self.client.commands()
        config = next((c for c in self.commands if c.name == "config"), None)
        names = config.schema["properties"]["extension"]["enum"] if config else []  # type: ignore[index]
        self.options = {n: await self.client.options(self.cursor, n) for n in names}

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
                        self.show_node(n)
                case CursorView(cursor=c) if c == self.cursor:
                    await self.place(event.at)
                    self.show(event)
                case _:
                    pass

    def show_node(self, node: Node) -> None:
        """A failed sample is shown; a message somebody else wrote waits in `unplaced` until the
        cursor's next view says whether it is on our branch. What this client sent is not echoed."""
        body = node.body
        if isinstance(body, Body.Custom) and body.kind == "sample_error":
            self.out(f"\n[sample failed: {body.data}]\n")
        elif isinstance(body, Body.User) and node.by != Author.Frontend(self.client.id):
            self.unplaced.append(node)

    async def place(self, at: int) -> None:
        """Show the unplaced messages on the walk to `at`, labelled with who wrote them: an
        extension (`[gamemaster] ...`) or another client. A node is sent before the view of the
        cursor that took it, so by this view each has been placed or is on another cursor."""
        if not self.unplaced:
            return
        on_branch = {n.id for n in await self.client.walk(at)}
        found, self.unplaced = self.unplaced, []
        for node in found:
            if node.id in on_branch and isinstance(node.body, Body.User):
                match node.by:
                    case Author.Extension(name=who) | Author.Frontend(client=who):
                        pass
                    case other:
                        who = repr(other)
                self.out(f"\n[{who}] {node.body.text}\n")

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
        shown_before = {q.id for q in self.questions}
        for question in view.questions:
            if question.id not in shown_before:
                choices = question.schema.get("enum")
                shown = f" [{'/'.join(map(str, choices))}]" if isinstance(choices, list) else ""
                self.out(f"\n? {question.text}{shown}\n")
        self.questions = view.questions

    async def line(self, text: str) -> None:
        """One line typed: an answer, a message, or a command. Raises what a command ended in
        that is not shown to the person (the driver failed, the client closed): that ends us."""
        if self.failure is not None:
            raise self.failure
        try:
            if self.question is not None:
                await self.answer(self.question, text)
            elif not text.strip():
                pass
            elif not text.startswith("/"):
                await self.client.send(self.cursor, text)
            elif text.strip() == "/":
                self.out("".join(self.describe(c) for c in self.commands))
            else:
                await self.command(text)
        except UnknownCommand as e:
            self.out(f"[no command /{e.name}: sent as text]\n")
            await self.client.send(self.cursor, text)
        except (ValueError, LookupError, Busy, CommandFailed) as e:
            self.out(f"[{e}]\n")

    async def answer(self, question: OpenQuestion, text: str) -> None:
        """An empty line declines, whatever the question asks for; otherwise the line is read by
        the question's schema, as a command's argument is."""
        if not text.strip():
            await self.client.answer(question.id, None)
        else:
            await self.client.answer(question.id, value(text, question.schema))
        # Not shown again until the cursor's next view: a fast second line must not re-answer it.
        self.questions = tuple(q for q in self.questions if q.id != question.id)

    async def command(self, text: str) -> None:
        parsed = parse(text, self.commands, self.options)
        if parsed.extension is not None and not parsed.args:  # a bare /config <extension>
            self.out(self.show_options(await self.client.options(self.cursor, parsed.extension)))
            return
        while parsed.missing:
            name = parsed.missing[0]
            text += f" {name}={shlex.quote(await self.ask(f'{name}: '))}"
            parsed = parse(text, self.commands, self.options)
        task = asyncio.create_task(self.finish(parsed))
        self.running.add(task)
        task.add_done_callback(self.ended)

    def ended(self, task: asyncio.Task[None]) -> None:
        """A command task is over. What it raised that `finish` did not show is kept, for `line`
        to raise: nothing else would read it."""
        self.running.discard(task)
        if not task.cancelled() and task.exception() is not None and self.failure is None:
            self.failure = task.exception()
            self.out(f"\n[a command ended in {self.failure!r}; the next line ends the chat]\n")

    async def finish(self, parsed: Parsed) -> None:
        """Run a command whose arguments are complete, and show its result or why it failed. It
        is a task, not part of `line`: a question the command asks is answered by a later line."""
        try:
            result = await parsed.run(self.client, self.cursor)
        except (ValueError, LookupError, Busy, CommandFailed) as e:
            self.out(f"[{e}]\n")
        else:
            if result is not None:
                self.out(f"[{result}]\n")

    async def finished(self) -> None:
        """Wait for the commands started so far."""
        await asyncio.gather(*self.running)

    def show_options(self, form: Form) -> str:
        """An extension's options with their values now, as `/config` takes them."""
        shown = "".join(
            f"  {name}={schema.get('default', '?')}  {schema.get('description', '')}\n"  # type: ignore[attr-defined]
            for name, schema in form.schema["properties"].items()  # type: ignore[attr-defined]
        )
        return f"/config {form.name} name=value ... [--save]\n{shown}"

    def describe(self, command: Form) -> str:
        required = command.schema.get("required", [])
        names = command.schema["properties"]
        params = " ".join(f"<{n}>" if n in required else f"[{n}]" for n in names)  # type: ignore[attr-defined]
        return f"/{command.name} {params}  {command.description.splitlines()[0]}\n"


def tab_completion(chat: Chat, loop: asyncio.AbstractEventLoop) -> None:
    """Tab completes through the client; readline calls this on its own thread."""

    def complete(text: str, state: int) -> str | None:
        client, cursor = chat.client, chat.cursor
        if chat.question is not None:  # the answer is completed as an argument is
            wanted = values(chat.question.schema, text, client, cursor)
        else:
            line = readline.get_line_buffer()
            wanted = completions(line, chat.commands, client, cursor, chat.options)
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
    driver = Driver(
        session,
        providers,
        loaded.catalog,
        extensions=[Demo(), StepLimit()],
        user_config=loaded.user,
    )
    driver.start()
    client = driver.client("chat:line")
    # Every cursor may have been closed (D17): restart the first, which keeps its labels.
    cursor = (
        session.cursors()[0] if session.cursors() else await client.reopen(session.closed()[0][0])
    )
    chat = Chat(client, cursor, partial(print, end="", flush=True), prompt)
    await chat.refresh()
    await client.watch([chat.cursor])
    listener = asyncio.create_task(chat.listen())
    tab_completion(chat, asyncio.get_running_loop())
    try:
        while True:
            try:
                text = await prompt("> ")
            except EOFError:
                break
            await chat.line(text)
    finally:
        for task in chat.running:
            task.cancel()
        listener.cancel()
        await client.close()
        await driver.stop()
        session.close()
        await close_all(providers)


async def prompt(text: str) -> str:
    return await asyncio.to_thread(input, text)


if __name__ == "__main__":
    run(main)
