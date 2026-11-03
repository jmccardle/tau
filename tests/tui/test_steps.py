"""Steps in flight: abort, pending lines, the status bar (D23 sections 2 and 9)."""

from pathlib import Path

from fakes import Fail, FakeProvider, Gate, Reply
from harness import Harness, scenario
from tuikit import Greeter, Showy, running, say, shows, type_line, until

from tau import Ending, Part


@scenario
async def test_escape_aborts_a_busy_cursor(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("slow")], gate=gate)])
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        await pilot.press("escape")
        await shows(pilot, app, "aborted")
        view = await h.view()
        assert not view.busy
        assert view.ending == Ending.Aborted()
    await h.close()


@scenario
async def test_ctrl_c_aborts_when_busy_then_clears_the_draft_then_asks_twice_to_quit(
    tmp_path: Path,
) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("slow")], gate=gate)])
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        await pilot.press("ctrl+c")
        await shows(pilot, app, "aborted")
        await type_line(pilot, "half a thought")
        await pilot.press("ctrl+c")
        assert app.draft == ""
        await pilot.press("ctrl+c")
        assert app.is_running
        await pilot.press("ctrl+c")
        await until(pilot, lambda: not app.is_running)
        assert app.return_code == 0
    await h.close()


@scenario
async def test_a_line_sent_while_busy_is_pending_with_its_author_until_the_step_takes_it(
    tmp_path: Path,
) -> None:
    gate = Gate()
    script = [Reply([Part.Text("one")], gate=gate), say("two")]
    h = await Harness.create(tmp_path, provider=FakeProvider(script))
    other = await h.connect("me:other")
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "first")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        await type_line(pilot, "second")
        await pilot.press("enter")
        await shows(pilot, app, "> second")
        await other.input(h.cursor, "third")
        await shows(pilot, app, "> [me:other] third")
        gate.release()
        await shows(pilot, app, "[User] second")
        await until(pilot, lambda: "pending" not in app.screen_text())
    await other.close()
    await h.close()


@scenario
async def test_the_status_bar_shows_the_model_and_what_an_extension_shows(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("ok")]), extensions=[Showy()])
    async with running(h, tmp_path) as (app, pilot):
        assert "model: fake  ·  idle" in app.screen_text()
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "showy: plan mode on")
    await h.close()


@scenario
async def test_a_failed_step_is_named_in_the_status_bar(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Fail("boom")]))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "failed: boom")
    await h.close()


@scenario
async def test_a_notice_is_a_toast_and_kept(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Greeter()])
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/count")
        await pilot.press("enter")
        await until(pilot, lambda: "/count: counted" in app.toasts)
        assert "at 0" in app.toasts, "the command's progress is a notice"
        assert app.session.notices == ["at 0"]
    await h.close()
