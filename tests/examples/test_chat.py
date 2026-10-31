"""examples/chat.py: a line client over `LocalClient`, driven with scripted input."""

import asyncio
import importlib.util
import sys
from collections.abc import AsyncGenerator, Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from tau_next import (
    Body,
    CallDraft,
    Delta,
    Driver,
    Finish,
    LocalClient,
    ModelConfig,
    Node,
    NodeHandle,
    Part,
    Session,
    Stop,
    TextDelta,
    Tool,
    model_config,
    read_models,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def load_example(name: str) -> ModuleType:
    """The example file as a module; the examples import `common` from their own directory."""
    sys.path.insert(0, str(EXAMPLES))
    try:
        spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / f"{name}.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(EXAMPLES))


chat = load_example("chat")


class Scripted:
    """Answers each sample with the next scripted reply (its parts, then its stop) and keeps
    the walks it was asked."""

    name = "scripted"

    def __init__(self, *replies: tuple[Sequence[Part | Delta], Stop]) -> None:
        self._replies = list(replies)
        self.walks: list[list[Body]] = []

    def check(self, model: ModelConfig) -> list[str]:
        return []

    async def aclose(self) -> None:
        """Nothing is held open."""

    async def sample(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> AsyncGenerator[Part | Finish | Delta, None]:
        self.walks.append([n.body for n in walk])
        parts, stop = self._replies.pop(0)
        for part in parts:
            yield part
        yield Finish(stop, None, None)


class Typed:
    """The lines a person types at the prompts, in order."""

    def __init__(self, *lines: str) -> None:
        self.lines = list(lines)
        self.asked: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.lines.pop(0)


class Screen:
    def __init__(self) -> None:
        self.text = ""

    def __call__(self, text: str) -> None:
        self.text += text


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(10_000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("never happened")


async def start(
    tmp_path: Path, provider: Scripted, typed: Typed, *extras: object
) -> tuple[Any, Driver, Session, Screen]:
    """A `Chat` over the example's extensions, listening."""
    session = Session.create(model_config("scripted"), "sys", 0, tmp_path / "s")
    models = read_models(
        cli={"models": {"scripted": {"model": "scripted", "provider": "scripted"}}}
    )
    driver = Driver(session, {"scripted": provider}, models, extensions=[chat.Demo(), *extras])  # type: ignore[list-item]
    driver.start()
    client = LocalClient(driver, "chat:test")
    screen = Screen()
    window = chat.Chat(client, session.cursors()[0], screen, typed)
    window.commands = await client.commands()
    await client.watch([window.cursor])
    window.listener = asyncio.ensure_future(window.listen())
    return window, driver, session, screen


def test_a_message_streams_the_reply(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = Scripted(([TextDelta("hel"), TextDelta("lo"), Part.Text("hello")], Stop.End))
        window, driver, session, screen = await start(tmp_path, provider, Typed())
        await window.line("hi")
        await until(lambda: "hello" in screen.text)
        await driver.stop()
        session.close()

    asyncio.run(scenario())


def test_slash_alone_lists_the_commands_with_their_parameters(tmp_path: Path) -> None:
    async def scenario() -> None:
        window, driver, session, screen = await start(tmp_path, Scripted(), Typed())
        await window.line("/")
        assert "/model <model>  Switch this branch to a configured model.\n" in screen.text
        assert "/word_count <path>  Count the words in a file.\n" in screen.text
        assert "/branch_from <node>  Go back to a node of this cursor's walk.\n" in screen.text
        await driver.stop()
        session.close()

    asyncio.run(scenario())


def test_a_command_missing_its_arguments_asks_for_each_and_tells_the_result(
    tmp_path: Path,
) -> None:
    file = tmp_path / "words.txt"
    file.write_text("one two three four")

    async def scenario() -> None:
        typed = Typed(str(file))
        window, driver, session, screen = await start(tmp_path, Scripted(), typed)
        await window.line("/word_count")
        await until(lambda: "4 words" in screen.text)
        assert typed.asked == ["path: "]
        await window.line("/model nope")
        assert "configured model" in screen.text, "a refused command says why"
        await driver.stop()
        session.close()

    asyncio.run(scenario())


def test_an_unknown_command_warns_and_goes_to_the_model_as_text(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = Scripted(([Part.Text("ok")], Stop.End))
        window, driver, session, screen = await start(tmp_path, provider, Typed())
        await window.line("/usr/bin/env is where?")
        await driver.rest(window.cursor)
        assert "[no command /usr/bin/env: sent as text]" in screen.text
        (user,) = [b for b in provider.walks[0] if isinstance(b, Body.User)]
        assert user.text == "/usr/bin/env is where?"
        await driver.stop()
        session.close()

    asyncio.run(scenario())


def test_a_permission_question_shows_its_choices_and_is_answered_inline(tmp_path: Path) -> None:
    file = tmp_path / "words.txt"
    file.write_text("a b c")

    async def scenario() -> None:
        call = Part.Call("c1", "word_count", f'{{"path": "{file}"}}')
        provider = Scripted(([call], Stop.Tools), ([Part.Text("three")], Stop.End))
        window, driver, session, screen = await start(tmp_path, provider, Typed())
        await window.line("count it")
        await until(lambda: "[allow/deny]" in screen.text)
        assert "? run word_count" in screen.text
        await window.line("allow")
        await driver.rest(window.cursor)
        results = [b for b in provider.walks[1] if isinstance(b, Body.Results)]
        assert "3 words" in str(results[0].outcomes)
        await driver.stop()
        session.close()

    asyncio.run(scenario())


class Counts:
    """Asks for a number before any tool."""

    name = "counts"

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        how_many = await node.ask("how many?", int)
        return None if how_many is not None else "no number given"


def test_an_empty_answer_declines_whatever_the_question_asks_for(tmp_path: Path) -> None:
    async def scenario() -> None:
        call = Part.Call("c1", "word_count", '{"path": "x"}')
        provider = Scripted(([call], Stop.Tools), ([Part.Text("ok")], Stop.End))
        window, driver, session, screen = await start(tmp_path, provider, Typed(), Counts())
        await window.line("count it")
        await until(lambda: "[allow/deny]" in screen.text)
        await window.line("allow")
        await until(lambda: "? how many?" in screen.text)
        await window.line("")
        await asyncio.wait_for(driver.rest(window.cursor), 5)
        results = [b for b in provider.walks[1] if isinstance(b, Body.Results)]
        assert "no number given" in str(results[0].outcomes)
        await driver.stop()
        session.close()

    asyncio.run(scenario())
