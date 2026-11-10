"""What the TUI tests share: an app over a Harness client, and waiting for the screen."""

import asyncio
import difflib
import html
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from fakes import Reply
from harness import Harness
from textual.app import App
from textual.pilot import Pilot

from tau import CallDraft, Cursor, NodeHandle, Part, Progress, Subject, Welcome
from tau.tui import TauApp

SIZE = (120, 40)
WAIT = 8.0  # seconds a screen may take; none takes a tenth of one


def say(text: str) -> Reply:
    return Reply([Part.Text(text)])


@asynccontextmanager
async def running(  # noqa: PLR0913 - what a test may vary of the app
    h: Harness,
    tmp_path: Path,
    *,
    client: str = "me:tui",
    theme: str | None = None,
    home: Path | None = None,
    sessions: Path | None = None,
    welcome: Welcome | None = None,
    size: tuple[int, int] = SIZE,
) -> AsyncIterator[tuple[TauApp, Pilot[None]]]:
    """The app over a new client of the Harness, running headless; the client is closed after.
    `sessions` is the sessions directory Ctrl+O lists, with `home` the `TAU_HOME` beside it."""
    connected = await h.connect(client)
    app = TauApp(
        connected,
        h.cursor,
        welcome=welcome,
        home=tmp_path / "home" if home is None else home,
        cwd=tmp_path,
        theme=theme,
        sessions=sessions,
    )
    async with app.run_test(size=size) as pilot:
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


async def answer(
    pilot: Pilot[None], app: TauApp, selector: str = ".question-buttons Button"
) -> None:
    """Click a question's button once it has its place on the screen: a click before the layout
    gave it one lands on nothing (seen on Windows)."""
    await until(pilot, lambda: any(b.region.area for b in app.query(selector)))
    await pilot.click(selector)


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


@dataclass(frozen=True)
class Options:
    size: Annotated[int, "how big"] = 10
    mode: Literal["fast", "slow"] = "fast"
    loud: bool = False

    def __post_init__(self) -> None:
        if self.size < 1:
            raise ValueError(f"a size is at least 1, not {self.size}")


class Tuner:
    """An extension with options: `size`, `mode` and `loud`; `size` below 1 is refused."""

    name = "tuner"
    options = Options


SNAPSHOTS = Path(__file__).parent / "__snapshots__"
UPDATE_SNAPSHOTS = "TAU_UPDATE_SNAPSHOTS"


def _rows(svg: str) -> list[str]:
    """The text of an exported screen, one string per terminal row."""
    found: dict[float, list[tuple[float, str]]] = {}
    for m in re.finditer(r'<text class="[^"]*" x="([\d.]+)" y="([\d.]+)"[^>]*>([^<]*)</text>', svg):
        x, y, text = float(m[1]), float(m[2]), html.unescape(m[3]).replace("\xa0", " ")
        found.setdefault(y, []).append((x, text))
    rows: list[str] = []
    for y in sorted(found):
        line = ""
        for x, text in sorted(found[y]):
            line = line.ljust(round(x / 12.2)) + text
        rows.append(line.rstrip())
    return rows


def check_screen(app: App[None], name: str) -> None:
    """Compare the app's screen with the SVG kept as `__snapshots__/NAME.svg`. With
    `TAU_UPDATE_SNAPSHOTS=1` in the environment the file is written instead (look at it, and at
    `git diff`, before keeping it). A difference is reported as the rows of text that changed; if
    the text is the same, the colours or the layout differ, and the SVGs say where."""
    if app.no_color:
        raise AssertionError(
            "NO_COLOR is set in the environment: Textual then draws without colour, and every "
            "snapshot would differ. Unset it to run the snapshots."
        )
    svg = re.sub(r"\bterminal-\d+-([\w-]+)", r"terminal-\1", app.export_screenshot(title="tau"))
    path = SNAPSHOTS / f"{name}.svg"
    if os.environ.get(UPDATE_SNAPSHOTS):
        path.parent.mkdir(exist_ok=True)
        path.write_text(svg, encoding="utf-8")
        return
    if not path.exists():
        raise AssertionError(f"no snapshot {path}: run once with {UPDATE_SNAPSHOTS}=1")
    kept = path.read_text(encoding="utf-8")
    if kept == svg:
        return
    changed = list(difflib.unified_diff(_rows(kept), _rows(svg), "kept", "now", lineterm="", n=1))
    said = "\n".join(changed[:60]) if changed else "the text is the same: colours or layout differ"
    raise AssertionError(f"{name} differs from {path} ({UPDATE_SNAPSHOTS}=1 rewrites it):\n{said}")
