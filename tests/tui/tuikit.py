"""What the TUI tests share: an app over a Harness client, and waiting for the screen."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fakes import Reply
from harness import Harness
from textual.pilot import Pilot

from tau import CallDraft, Cursor, NodeHandle, Part, Progress, Subject
from tau.tui import TauApp

SIZE = (120, 40)
WAIT = 8.0  # seconds a screen may take; none takes a tenth of one


def say(text: str) -> Reply:
    return Reply([Part.Text(text)])


@asynccontextmanager
async def running(
    h: Harness, tmp_path: Path, *, client: str = "me:tui", theme: str | None = None
) -> AsyncIterator[tuple[TauApp, Pilot[None]]]:
    """The app over a new client of the Harness, running headless; the client is closed after."""
    connected = await h.connect(client)
    app = TauApp(
        connected, h.cursor, welcome=None, home=tmp_path / "home", cwd=tmp_path, theme=theme
    )
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: "idle" in app.screen_text() or "working…" in app.screen_text())
        yield app, pilot
    await connected.close()


async def until(pilot: Pilot[None], ready: Callable[[], bool]) -> None:
    """Let the app and the Harness run until `ready()`."""
    deadline = asyncio.get_running_loop().time() + WAIT
    while asyncio.get_running_loop().time() < deadline:
        if ready():
            return
        await pilot.pause()
        await asyncio.sleep(0)
    raise AssertionError("the screen never got there")


async def shows(pilot: Pilot[None], app: TauApp, text: str) -> None:
    """Wait until the screen's text holds `text`."""
    try:
        await until(pilot, lambda: text in app.screen_text())
    except AssertionError:
        raise AssertionError(f"{text!r} is not on the screen:\n{app.screen_text()}") from None


async def type_line(pilot: Pilot[None], text: str) -> None:
    """Type `text` into the editor (a space is the `space` key)."""
    await pilot.press(*("space" if c == " " else c for c in text))


class Gatekeeper:
    """An extension that asks before every tool call: the `permissions` of the behavior suite."""

    name = "permissions"

    def __init__(self) -> None:
        self.verdicts: list[str | None] = []

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        verdict = await node.ask(f"run {call.tool}?", Literal["allow", "deny"])
        self.verdicts.append(verdict)
        return None if verdict == "allow" else "the user said no"


def greet(who: str, shout: bool = False) -> str:
    """Greet someone."""
    return f"HELLO {who.upper()}" if shout else f"hello {who}"


def explode() -> str:
    """A command that fails."""
    raise RuntimeError("it blew up")


def count(progress: Progress) -> str:
    """Count aloud."""
    progress("at 0")
    return "counted"


class Greeter:
    """An extension with commands: `/greet WHO [shout]`, `/explode` and `/count`."""

    name = "greeter"
    commands = (greet, explode, count)


class Showy:
    """An extension that shows a line on the cursor at each step."""

    name = "showy"

    async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
        cursor.show("plan mode on")
