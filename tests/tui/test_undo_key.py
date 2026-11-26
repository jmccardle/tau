"""Ctrl+Z with the `undo` extension loaded (D33): the TUI calls the harness's `/undo`, so the key
also goes back over `/clear`, `/compact` and `/prune`, and still puts the last message back in the
editor. Without the extension it is the client's own edit-last (`test_cursors.py`)."""

from pathlib import Path

from fakes import FakeProvider
from harness import Harness, scenario
from shipped import shipped
from tuikit import running, say, shows, type_line, until

from tau import Attachments, Body

SYS = 1  # the system node: a new session is Config 0, System 1


@scenario
async def test_ctrl_z_runs_undo_and_puts_back_what_was_typed(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("alpha", encoding="utf-8")
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([say("first")]),
        extensions=[Attachments(work), shipped("undo")],
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "explain @a.txt")
        await pilot.press("enter")
        await shows(pilot, app, "first")
        await pilot.press("ctrl+z")
        await until(pilot, lambda: app.draft == "explain @a.txt")
        assert (await h.view()).at == SYS
    await h.close()


@scenario
async def test_ctrl_z_goes_back_over_clear_and_says_so(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([say("first")]),
        extensions=[shipped("session"), shipped("undo")],
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "hello")
        await pilot.press("enter")
        await shows(pilot, app, "first")
        before = (await h.view()).at
        await type_line(pilot, "/clear")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: isinstance(h.session.tree.node(h.session.cursor(0).at).body, Body.Custom),
        )
        await until(pilot, lambda: not h.session.cursor(h.cursor).busy)
        await pilot.press("ctrl+z")
        await until(pilot, lambda: "undo: undid /clear" in app.toasts)
        assert (await h.view()).at == before
        assert app.draft == ""
    await h.close()


@scenario
async def test_ctrl_z_with_nothing_to_undo_says_so(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]), extensions=[shipped("undo")])
    async with running(h, tmp_path) as (app, pilot):
        await until(pilot, lambda: any(f.name == "undo" for f in app.session.commands))
        await pilot.press("ctrl+z")
        await until(pilot, lambda: "nothing to undo on this branch" in app.toasts)
    await h.close()
