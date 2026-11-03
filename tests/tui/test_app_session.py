"""A session's life under the app: reconnecting, a failed driver, the loop, themes (D23)."""

import asyncio
import json
from pathlib import Path

import pytest
from fakes import FakeProvider, Gate, Reply
from harness import Harness, scenario
from tuikit import SIZE, running, say, shows, type_line, until

from tau import ConfigError, Part, Progress
from tau.tui import TauApp


def flood(progress: Progress) -> str:
    """Say a lot at once."""
    for i in range(20):
        progress(f"line {i}")
    return "flooded"


class Floody:
    name = "floody"
    commands = (flood,)


@scenario
async def test_a_client_that_fell_behind_is_cut_off_and_the_app_reconnects(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, provider=FakeProvider([say("still here")]), extensions=[Floody()]
    )
    slow = await h.connect("me:tui", buffer=2)
    app = TauApp(
        slow,
        h.cursor,
        welcome=None,
        home=tmp_path / "home",
        cwd=tmp_path,
        reconnect=lambda: h.connect("me:tui"),
    )
    async with app.run_test(size=SIZE) as pilot:
        await shows(pilot, app, "idle")
        await type_line(pilot, "/flood")
        await pilot.press("enter")
        await until(pilot, lambda: any("reconnecting" in t for t in app.toasts))
        await until(pilot, lambda: app.client is not slow)
        assert app.failure is None
        # `/flood` met the cut-off: it is back in the editor if the connection was lost under it
        # and not if the command had finished. Wait for that to be settled, then start clean.
        for worker in [w for w in app.workers if w.group == "submit"]:
            await worker.wait()
        if app.draft:
            assert app.draft == "/flood"
            await pilot.press("ctrl+c")
            await until(pilot, lambda: app.draft == "")
        await type_line(pilot, "hello")
        await pilot.press("enter")
        await shows(pilot, app, "still here")
        assert "[User] hello" in app.screen_text()
    await app.client.close()
    await h.close()


@scenario
async def test_a_driver_that_failed_is_shown_and_quitting_exits_1(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("never")], gate=gate)])
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        h.session.close()  # the driver fails when its step next touches the session
        gate.release()
        await until(pilot, lambda: app.failure is not None)
        assert "session lost" in app.screen_text()
        assert "[User] go" in app.screen_text(), "the transcript stays readable"
        await pilot.press("ctrl+q")
        await until(pilot, lambda: not app.is_running)
        assert app.return_code == 1
    await h.driver.stop()


@scenario
async def test_run_async_shares_the_loop_with_the_driver(tmp_path: Path) -> None:
    """D23 "not verified": `App.run_async` runs on the caller's loop, so a local client of a driver
    on that loop is called from the app without a thread in between."""
    h = await Harness.create(tmp_path, provider=FakeProvider([say("same loop")]))
    connected = await h.connect("me:tui")
    app = TauApp(connected, h.cursor, welcome=None, home=tmp_path / "home", cwd=tmp_path)
    running_app = asyncio.ensure_future(app.run_async(headless=True, size=SIZE))
    try:
        await until_async(lambda: app.is_running and "idle" in _text(app))
        await connected.send(h.cursor, "go")
        await until_async(lambda: "same loop" in _text(app))
    finally:
        app.exit()
        await running_app
    await connected.close()
    await h.close()


def _text(app: TauApp) -> str:
    try:
        return app.screen_text()
    except Exception:  # the screen is not up yet
        return ""


async def until_async(ready: "object") -> None:
    assert callable(ready)
    for _ in range(2000):
        if ready():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("never")


@scenario
async def test_a_theme_is_chosen_by_name(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    async with running(h, tmp_path, theme="latte") as (app, _):
        assert app.theme_name == "latte"
    async with running(h, tmp_path) as (app, _):
        assert app.theme_name == "mocha"
    await h.close()


@scenario
async def test_an_unknown_theme_is_refused_naming_the_themes(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    client = await h.connect("me:tui")
    with pytest.raises(ConfigError, match="mocha"):
        TauApp(client, h.cursor, welcome=None, home=tmp_path / "home", cwd=tmp_path, theme="nope")
    await client.close()
    await h.close()


@scenario
async def test_a_user_theme_file_is_a_theme_and_a_bad_one_is_a_toast(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "themes").mkdir(parents=True)
    (home / "themes" / "dusk.json").write_text(json.dumps({"extends": "mocha"}))
    (home / "themes" / "broken.json").write_text("{not json")
    h = await Harness.create(tmp_path)
    client = await h.connect("me:tui")
    app = TauApp(client, h.cursor, welcome=None, home=home, cwd=tmp_path, theme="dusk")
    async with app.run_test(size=SIZE) as pilot:
        assert app.theme_name == "dusk"
        await until(pilot, lambda: any("broken.json" in t for t in app.toasts))
    await client.close()
    await h.close()
