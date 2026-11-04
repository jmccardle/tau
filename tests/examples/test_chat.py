"""examples/chat.py: a line client over `LocalClient`, driven with scripted input."""

import asyncio
import importlib.util
import sys
from collections.abc import AsyncGenerator, Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import pytest
from world import World

from tau import (
    Body,
    CallDraft,
    Cursor,
    CursorId,
    Delta,
    Driver,
    Extension,
    Finish,
    ModelConfig,
    Node,
    NodeHandle,
    Part,
    Session,
    StepLimit,
    Stop,
    TextDelta,
    Tool,
    TreeHandle,
    load_config,
    load_extensions,
    model_config,
    read_models,
)
from tau.cli import extension_flags

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


BUILT_INS = [
    "attachments",
    "base_tools",
    "compaction",
    "prune",
    "skills",
    "step_limit",
    "usage",
]  # the built-in extensions


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
    extensions = [chat.Demo(), StepLimit(), *extras]
    driver = Driver(session, {"scripted": provider}, models, extensions=extensions)  # type: ignore[arg-type]
    driver.start()
    client = driver.client("chat:test")
    screen = Screen()
    window = chat.Chat(client, session.cursors()[0], screen, typed)
    await window.refresh()
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


def test_a_command_missing_its_arguments_asks_for_each_and_shows_the_result(
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
        await window.finished()
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


def test_config_changes_an_extensions_options_and_a_bare_one_shows_them(tmp_path: Path) -> None:
    async def scenario() -> None:
        window, driver, session, screen = await start(tmp_path, Scripted(), Typed())
        await window.line("/")
        assert "/config <extension>" in screen.text
        await window.line("/config step_limit")
        assert "limit=50  unattended steps before auto-step turns off" in screen.text
        await window.line("/config step_limit limit=7")
        await window.finished()
        await driver.rest(window.cursor)
        await window.line("/config step_limit")
        assert "limit=7" in screen.text, "the form shows the values now"
        await window.line("/config step_limit limit=many")
        assert "limit: 'many' is not an integer" in screen.text
        await window.line("/config step_limit limit=0")
        await window.finished()
        assert "at least 1" in screen.text, "the driver refuses what the options type refuses"
        await driver.stop()
        session.close()

    asyncio.run(scenario())


def test_a_failing_command_says_what_failed(tmp_path: Path) -> None:
    async def scenario() -> None:
        window, driver, session, screen = await start(tmp_path, Scripted(), Typed())
        await window.line(f"/word_count {tmp_path / 'missing.txt'}")
        await window.finished()
        assert "[FileNotFoundError: " in screen.text
        await driver.stop()
        session.close()

    asyncio.run(scenario())


class Picks:
    """Asks which cursor to send the next tool call's note to."""

    name = "picks"

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        picked = await node.ask("which cursor?", CursorId)
        return None if picked is not None else "no cursor given"


def test_an_answer_is_read_by_the_questions_schema_and_new_opens_a_cursor(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        call = Part.Call("c1", "word_count", '{"path": "x"}')
        provider = Scripted(([call], Stop.Tools), ([Part.Text("ok")], Stop.End))
        window, driver, session, screen = await start(tmp_path, provider, Typed(), Picks())
        await window.line("count it")
        await until(lambda: "[allow/deny]" in screen.text)
        await window.line("allow")
        await until(lambda: "? which cursor?" in screen.text)
        await window.line("seven")
        assert "is not an integer" in screen.text, "a wrong answer is refused, the question stays"
        await window.line("new")
        await asyncio.wait_for(driver.rest(window.cursor), 5)
        assert len(session.cursors()) == 2, "a new cursor was opened at the batch's node"
        await driver.stop()
        session.close()

    asyncio.run(scenario())


class Confirms:
    """A command that asks in its body, and a tool-less way to ask two questions at once."""

    name = "confirms"

    def __init__(self) -> None:
        self.commands = (self.confirm,)

    async def confirm(self, cursor: Cursor) -> str:
        """Ask before doing anything."""
        verdict = await cursor.ask("really?", Literal["yes", "no"])
        return f"you said {verdict}"


def test_a_command_that_asks_in_its_body_is_answered_at_the_prompt(tmp_path: Path) -> None:
    async def scenario() -> None:
        window, driver, session, screen = await start(tmp_path, Scripted(), Typed(), Confirms())
        await window.line("/confirm")  # returns while the command waits for its answer
        await until(lambda: "? really? [yes/no]" in screen.text)
        assert window.running, "the command is still running"
        await window.line("yes")
        await window.finished()
        assert "[you said yes]" in screen.text
        await driver.stop()
        session.close()

    asyncio.run(scenario())


def test_an_empty_answer_declines_a_commands_question(tmp_path: Path) -> None:
    async def scenario() -> None:
        window, driver, session, screen = await start(tmp_path, Scripted(), Typed(), Confirms())
        await window.line("/confirm")
        await until(lambda: "? really?" in screen.text)
        await window.line("")
        await window.finished()
        assert "[you said None]" in screen.text
        await driver.stop()
        session.close()

    asyncio.run(scenario())


def test_an_empty_line_with_no_question_does_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = Scripted()
        window, driver, session, screen = await start(tmp_path, provider, Typed())
        await window.line("")
        await window.line("   ")
        await asyncio.wait_for(driver.rest(window.cursor), 5)  # a sent input would run a step
        assert provider.walks == []
        assert screen.text == ""
        await driver.stop()
        session.close()

    asyncio.run(scenario())


def test_a_command_that_ends_in_something_unshown_ends_the_chat(tmp_path: Path) -> None:
    async def scenario() -> None:
        window, driver, session, screen = await start(tmp_path, Scripted(), Typed())
        await driver.stop()  # the next command meets a stopped driver, which is not for the screen
        await window.line("/model scripted")
        await until(lambda: not window.running)
        assert "the driver is stopped" in screen.text  # said at once, not at the next line
        with pytest.raises(ConnectionError, match="stopped"):
            await window.line("")  # the next line raises it, as the loop of `main` sees
        session.close()

    asyncio.run(scenario())


class AsksTwice:
    """Two questions open at once on one cursor."""

    name = "twice"

    def __init__(self) -> None:
        self.commands = (self.both,)

    async def both(self, cursor: Cursor) -> str:
        """Ask two questions together."""
        first, second = await asyncio.gather(
            cursor.ask("first?", Literal["a", "b"]), cursor.ask("second?", Literal["c", "d"])
        )
        return f"{first}{second}"


def test_every_open_question_is_shown_and_a_line_answers_the_first(tmp_path: Path) -> None:
    async def scenario() -> None:
        window, driver, session, screen = await start(tmp_path, Scripted(), Typed(), AsksTwice())
        await window.line("/both")
        await until(lambda: "? second?" in screen.text)
        assert "? first? [a/b]" in screen.text
        assert "? second? [c/d]" in screen.text
        await window.line("a")
        await window.line("d")  # at once: the second line answers the second question
        await asyncio.wait_for(window.finished(), 5)
        assert "[ad]" in screen.text
        await driver.stop()
        session.close()

    asyncio.run(scenario())


class Nudger:
    """A command that sends text to a cursor, as this extension."""

    name = "nudger"

    def __init__(self) -> None:
        self.commands = (self.nudge,)

    def nudge(self, tree: TreeHandle, cursor: CursorId, text: str) -> None:
        """Send `text` to a cursor."""
        tree.send(cursor, text)


def test_a_message_another_author_wrote_is_shown_with_their_name(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = Scripted(
            ([TextDelta("noted"), Part.Text("noted")], Stop.End),
            ([TextDelta("ok"), Part.Text("ok")], Stop.End),
            ([TextDelta("hm"), Part.Text("hm")], Stop.End),
        )
        window, driver, session, screen = await start(tmp_path, provider, Typed(), Nudger())
        await window.line("hi")
        await until(lambda: "noted" in screen.text)
        assert "hi" not in screen.text, "what this client sent is not echoed"
        await window.line('/nudge cursor=0 text="mind the gap"')
        await window.finished()
        await until(lambda: "ok" in screen.text)
        assert "[nudger] mind the gap" in screen.text
        elsewhere = driver.client("john:tui")
        await elsewhere.send(window.cursor, "from the other terminal")
        await until(lambda: "[john:tui] from the other terminal" in screen.text)
        await elsewhere.close()
        await driver.stop()
        session.close()

    asyncio.run(scenario())


def test_a_message_written_to_another_cursor_is_not_shown(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = Scripted(
            ([Part.Text("aside")], Stop.End),
            ([TextDelta("main"), Part.Text("main")], Stop.End),
        )
        window, driver, session, screen = await start(tmp_path, provider, Typed())
        elsewhere = driver.client("john:tui")
        other = await elsewhere.open(session.cursor(window.cursor).at)
        await elsewhere.send(other, "a private aside")
        await driver.rest(other)
        await elsewhere.send(window.cursor, "for the chat")
        await until(lambda: "main" in screen.text)
        assert "[john:tui] for the chat" in screen.text, "the watched cursor's messages are shown"
        assert "private aside" not in screen.text
        await elsewhere.close()
        await driver.stop()
        session.close()

    asyncio.run(scenario())


# `-e PATH`: extensions loaded from a Python file named on the command line, by the library's
# loader (D19; its own rules are tested in tests/config/test_extensions.py).

SPECULATIVE = EXAMPLES / "speculative"


def written(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source)
    return path


def load(world: World, *files: Path) -> list[Extension]:
    """What the chat adds after its own: the built-ins, and the `-e` files."""
    loaded = load_config(world.home, world.environ, ask=None)
    return load_extensions(loaded, explicit=[str(f) for f in files], discover=False)


def test_the_chat_takes_the_librarys_extension_flags() -> None:
    flags = extension_flags()
    args = flags.parse_known_args(["--model", "m", "-e", "a.py", "-e", "b", "--no-extensions"])[0]
    assert args.extension == ["a.py", "b"]
    assert args.no_extensions is True
    assert flags.parse_known_args([])[0].extension == []


def test_the_step_limit_comes_from_the_loader_not_from_the_chat(world: World) -> None:
    assert [e.name for e in load(world)] == BUILT_INS


@pytest.mark.parametrize("path", sorted(SPECULATIVE.glob("*.py")), ids=lambda p: p.stem)
def test_every_speculative_example_loads_unchanged_and_builds_a_driver(
    tmp_path: Path, world: World, path: Path
) -> None:
    loaded = load(world, path)
    assert [e.name for e in loaded] == [*BUILT_INS, path.stem]
    session = Session.create(model_config("scripted"), "sys", 0, tmp_path / "s")
    models = read_models(
        cli={"models": {"scripted": {"model": "scripted", "provider": "scripted"}}}
    )
    Driver(session, {"scripted": Scripted()}, models, extensions=[chat.Demo(), *loaded])  # type: ignore[arg-type]
    session.close()


def test_a_loaded_class_is_a_working_extension_in_the_chat(tmp_path: Path, world: World) -> None:
    file = written(
        tmp_path,
        "shout.py",
        "from tau import TreeHandle\n\n\n"
        "async def shout(tree: TreeHandle, text: str) -> str:\n"
        '    """Say it loud."""\n'
        "    return text.upper()\n\n\n"
        "class Shouter:\n"
        '    name = "shouter"\n'
        "    commands = (shout,)\n\n\n"
        "extensions = [Shouter()]\n",
    )

    async def scenario() -> None:
        extras = load(world, file)[len(BUILT_INS) :]  # past the built-ins: the chat adds its own
        window, driver, session, screen = await start(tmp_path, Scripted(), Typed(), *extras)
        await window.line("/shout hello")
        await window.finished()
        assert "[HELLO]" in screen.text
        await driver.stop()
        session.close()

    asyncio.run(scenario())


def test_two_files_with_the_same_name_are_two_modules(tmp_path: Path, world: World) -> None:
    """Each module is looked up by where its file is, so a type hint in one is resolved in it."""
    loaded: list[Extension] = []
    for folder, extension in (("a", "A"), ("b", "B")):
        (tmp_path / folder).mkdir()
        file = written(
            tmp_path / folder,
            "ext.py",
            "from dataclasses import dataclass\n\n"
            f"Size = {'int' if folder == 'a' else 'str'}\n\n\n"
            "@dataclass(frozen=True)\nclass Options:\n"
            f'    size: "Size" = {1 if folder == "a" else repr("big")}\n\n\n'
            f'class {extension}:\n    name = "{folder}"\n    options = Options\n\n\n'
            f"extensions = [{extension}()]\n",
        )
        loaded += load(world, file)[
            len(BUILT_INS) :
        ]  # the one key is `ext`: a second file in one load shadows
    session = Session.create(model_config("scripted"), "sys", 0, tmp_path / "s")
    models = read_models(
        cli={"models": {"scripted": {"model": "scripted", "provider": "scripted"}}}
    )
    Driver(session, {"scripted": Scripted()}, models, extensions=loaded)  # type: ignore[arg-type]
    session.close()


def test_two_extensions_that_share_a_command_list_it_qualified(tmp_path: Path) -> None:
    def note_a(tree: TreeHandle) -> str:
        """A's."""
        return "a"

    def note_b(tree: TreeHandle) -> str:
        """B's."""
        return "b"

    note_a.__name__ = note_b.__name__ = "note"

    class A:
        name = "a"
        commands = (note_a,)

    class B:
        name = "b"
        commands = (note_b,)

    async def scenario() -> None:
        window, driver, session, screen = await start(tmp_path, Scripted(), Typed(), A(), B())
        await window.line("/")
        assert "/a:note" in screen.text
        assert "/b:note" in screen.text
        await window.line("/note")
        assert "[/note is two commands: /a:note, /b:note]" in screen.text
        await window.line("/b:note")
        await window.finished()
        assert "[b]" in screen.text
        await driver.stop()
        session.close()

    asyncio.run(scenario())
