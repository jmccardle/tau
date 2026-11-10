"""Leaving the screen for another session: the picker (Ctrl+O) and a new session (Ctrl+N). The app
only says where the person wants to go (`TauApp.switch`); the launch starts it (tests/serve, in a
terminal of its own)."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fakes import FakeProvider
from harness import Harness, scenario
from textual.app import App
from textual.widgets import DataTable, Input
from tuikit import running, until
from world import World

from tau import PROTOCOL, Session, Welcome
from tau.tui import SessionPicker, Switch

OLD = "20261101-100000-aaaaaa"
NEW = "20261101-110000-bbbbbb"
FAR = "20261101-120000-cccccc"


def catalog(world: World, here: Path) -> Path:
    """Three sessions: two for `here` (the older says something, the newer is named) and one for
    another directory; and a directory that is no session."""
    elsewhere = world.home / "elsewhere"
    elsewhere.mkdir()
    world.session(OLD, cwd=here, says=["what is a monad"], age=500)
    world.session(NEW, cwd=here, says=["ignored, it has a name"], name="the named one", age=100)
    world.session(FAR, cwd=elsewhere, says=["far away work"], age=10)
    sessions = world.tau / "sessions"
    (sessions / "20261101-130000-dddddd").mkdir()
    return sessions


def picker(app: App[None]) -> SessionPicker:
    screen = app.screen
    assert isinstance(screen, SessionPicker)
    return screen


def listed(app: App[None]) -> list[str]:
    """The first cell (title) of each row the picker shows."""
    table = app.screen.query_one(DataTable)
    return [str(table.get_row_at(i)[0]) for i in range(table.row_count)]


@scenario
async def test_ctrl_o_lists_this_directorys_sessions_newest_first_by_title(
    tmp_path: Path, world: World
) -> None:
    sessions = catalog(world, tmp_path)
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path, home=world.tau, sessions=sessions) as (app, pilot):
        await pilot.press("ctrl+o")
        await until(pilot, lambda: isinstance(app.screen, SessionPicker))
        await until(pilot, lambda: bool(listed(app)))
        assert listed(app) == ["the named one", "what is a monad", "20261101-130000-dddddd"], (
            "a name, else the first message; a directory that is no session is last, by its id"
        )
        assert all("far away" not in t for t in listed(app))
    await h.close()


@scenario
async def test_tab_widens_the_picker_to_every_directory_and_slash_filters(
    tmp_path: Path, world: World
) -> None:
    sessions = catalog(world, tmp_path)
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path, home=world.tau, sessions=sessions) as (app, pilot):
        await pilot.press("ctrl+o")
        await until(pilot, lambda: isinstance(app.screen, SessionPicker))
        await pilot.press("tab")
        await until(pilot, lambda: "far away work" in listed(app))
        columns = [str(c.label) for c in app.screen.query_one(DataTable).columns.values()]
        assert columns[-1] == "Directory"
        await pilot.press("slash", "m", "o", "n", "a", "d")
        await until(pilot, lambda: listed(app) == ["what is a monad"])
        assert app.screen.query_one(Input).value == "monad"
        await pilot.press("escape")  # out of the filter, not out of the picker
        assert isinstance(app.screen, SessionPicker)
        await pilot.press("tab")  # back to this directory
        await until(pilot, lambda: [c for c in listed(app)] == ["what is a monad"])
    await h.close()


@scenario
async def test_picking_a_row_leaves_for_that_session(tmp_path: Path, world: World) -> None:
    sessions = catalog(world, tmp_path)
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path, home=world.tau, sessions=sessions) as (app, pilot):
        await pilot.press("ctrl+o")
        await until(pilot, lambda: isinstance(app.screen, SessionPicker))
        await until(pilot, lambda: bool(listed(app)))
        await pilot.press("down", "enter")  # the second row
        await until(pilot, lambda: not app.is_running)
        assert app.switch == Switch(OLD)
        assert app.return_code == 0
    await h.close()


@scenario
async def test_keys_typed_as_the_picker_opens_are_not_lost(tmp_path: Path, world: World) -> None:
    sessions = catalog(world, tmp_path)
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path, home=world.tau, sessions=sessions) as (app, pilot):
        await pilot.press("ctrl+o", "down")  # no wait for the screen or its rows
        width, height = app.size
        await pilot.resize_terminal(width + 1, height)  # a terminal's first size can come late
        await pilot.press("enter")
        await until(pilot, lambda: not app.is_running)
        assert app.switch == Switch(OLD), "the second row, as if the person had waited"
    await h.close()


@scenario
async def test_a_row_with_an_error_is_listed_dimmed_and_cannot_be_picked(
    tmp_path: Path, world: World
) -> None:
    sessions = catalog(world, tmp_path)
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path, home=world.tau, sessions=sessions) as (app, pilot):
        await pilot.press("ctrl+o")
        await until(pilot, lambda: isinstance(app.screen, SessionPicker))
        await until(pilot, lambda: bool(listed(app)))
        cells = app.screen.query_one(DataTable).get_row_at(2)
        assert [getattr(c, "style", "") for c in cells] == ["dim"] * 3, "dimmed"
        await pilot.press("down", "down", "enter")  # the directory that is no session
        await until(pilot, lambda: "cannot be opened" in picker(app).status_text)
        assert app.is_running
        assert app.switch is None
        assert isinstance(app.screen, SessionPicker)
    await h.close()


@scenario
async def test_escape_closes_the_picker_and_the_session_stays(tmp_path: Path, world: World) -> None:
    sessions = catalog(world, tmp_path)
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path, home=world.tau, sessions=sessions) as (app, pilot):
        await pilot.press("ctrl+o")
        await until(pilot, lambda: isinstance(app.screen, SessionPicker))
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, SessionPicker))
        assert app.is_running
        assert app.switch is None
    await h.close()


@scenario
async def test_ctrl_o_needs_a_sessions_directory(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path) as (app, pilot):
        await pilot.press("ctrl+o")
        await until(pilot, lambda: any("sessions directory" in t for t in app.toasts))
        assert app.is_running
    await h.close()


@scenario
async def test_ctrl_n_leaves_for_a_new_session(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path) as (app, pilot):
        await pilot.press("ctrl+n")
        await until(pilot, lambda: not app.is_running)
        assert app.switch == Switch(None)
        assert app.return_code == 0
    await h.close()


def welcomed(session: str, cwd: Path) -> Welcome:
    return Welcome(PROTOCOL, "0", session, str(cwd), os.getpid())


def states(app: App[None]) -> dict[str, str]:
    """Each row's title and its State cell."""
    table = app.screen.query_one(DataTable)
    return {
        str(table.get_row_at(i)[0]): str(table.get_row_at(i)[2]) for i in range(table.row_count)
    }


@scenario
async def test_the_current_session_is_listed_as_current_and_picking_it_does_nothing(
    tmp_path: Path, world: World
) -> None:
    sessions = catalog(world, tmp_path)
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    welcome = welcomed(OLD, tmp_path)
    async with running(h, tmp_path, home=world.tau, sessions=sessions, welcome=welcome) as (
        app,
        pilot,
    ):
        await pilot.press("ctrl+o")
        await until(pilot, lambda: isinstance(app.screen, SessionPicker))
        await until(pilot, lambda: bool(listed(app)))
        assert states(app)["what is a monad"] == "current"
        await pilot.press("down", "enter")
        await until(pilot, lambda: not isinstance(app.screen, SessionPicker))
        assert app.is_running
        assert app.switch is None, "nothing to leave for"
    await h.close()


async def rude_server(world: World, session: str, cwd: Path) -> asyncio.Server:
    """A serve record whose socket accepts a connection and hangs up, as a broken server."""
    serve = world.tau / "serve"
    serve.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket = Path(tempfile.mkdtemp(prefix="tau")) / "s"

    async def hang_up(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_unix_server(hang_up, path=str(socket))
    record = {
        "socket": str(socket),
        "pid": 1,
        "session": session,
        "cwd": str(cwd),
        "protocol": 1,
        "version": "0",
        "since": 0,
        "log": None,
    }
    (serve / f"{session}.json").write_text(json.dumps(record), encoding="utf-8")
    return server


@scenario
@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows: tau serve (Unix domain sockets) is not ported, D21"
)
async def test_a_server_that_does_not_answer_marks_its_row_and_the_picker_still_opens(
    tmp_path: Path, world: World
) -> None:
    sessions = catalog(world, tmp_path)
    server = await rude_server(world, OLD, tmp_path)
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    try:
        async with running(h, tmp_path, home=world.tau, sessions=sessions) as (app, pilot):
            await pilot.press("ctrl+o")
            await until(pilot, lambda: isinstance(app.screen, SessionPicker))
            await until(pilot, lambda: bool(listed(app)))
            assert states(app)["what is a monad"] == "no reply"
            assert states(app)["the named one"] == ""
            await pilot.press("down", "enter")  # the row of the server that does not answer
            await until(pilot, lambda: "did not answer" in picker(app).status_text)
            assert isinstance(app.screen, SessionPicker)
            assert app.switch is None
    finally:
        server.close()
        await h.close()


@scenario
async def test_a_session_another_process_holds_is_in_use_and_cannot_be_picked(
    tmp_path: Path, world: World
) -> None:
    sessions = catalog(world, tmp_path)
    held = Session.load(sessions / OLD)
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    try:
        async with running(h, tmp_path, home=world.tau, sessions=sessions) as (app, pilot):
            await pilot.press("ctrl+o")
            await until(pilot, lambda: isinstance(app.screen, SessionPicker))
            await until(pilot, lambda: bool(listed(app)))
            assert states(app)["what is a monad"] == "in use"
            await pilot.press("down", "enter")
            await until(pilot, lambda: "in use" in picker(app).status_text)
            assert isinstance(app.screen, SessionPicker)
            assert app.switch is None
    finally:
        held.close()
        await h.close()
