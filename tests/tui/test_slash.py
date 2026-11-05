"""`/` commands: the popup, Tab, the form for missing arguments, results and refusals (D23)."""

import os
from pathlib import Path

import pytest
from fakes import FakeProvider
from harness import Harness, scenario
from tuikit import Greeter, running, say, shows, type_line, until

from tau import Body


async def harness(tmp_path: Path) -> Harness:
    return await Harness.create(
        tmp_path, provider=FakeProvider([say("ok")]), extensions=[Greeter()]
    )


@scenario
async def test_slash_lists_the_commands_and_a_prefix_narrows_them(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await pilot.press("slash")
        await shows(pilot, app, "/greet  Greet someone.")
        assert "/explode  A command that fails." in app.screen_text()
        await pilot.press("g")
        await until(pilot, lambda: "/explode" not in app.screen_text())
        assert "/greet" in app.screen_text()
    await h.close()


@scenario
async def test_a_slash_word_that_is_no_command_says_it_goes_to_the_model(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/nope")
        await shows(pilot, app, "/nope is not a command: this line goes to the model as text")
    await h.close()


@scenario
async def test_tab_completes_a_command_name(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/gr")
        await pilot.press("tab")
        await until(pilot, lambda: app.draft == "/greet ")
    await h.close()


@scenario
async def test_tab_cycles_through_the_candidates(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/")
        await pilot.press("tab")
        await until(
            pilot,
            lambda: app.draft.startswith("/"),
        )
        first = app.draft
        await pilot.press("tab")
        await until(pilot, lambda: app.draft != first)
        assert "▸" in app.screen_text()
    await h.close()


@scenario
async def test_a_command_with_its_arguments_runs_and_its_result_is_a_toast(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/greet world")
        await pilot.press("enter")
        await until(pilot, lambda: "/greet: hello world" in app.toasts)
        assert app.draft == ""
        assert not [n for n in h.nodes() if isinstance(n.body, Body.User)], "nothing is sent"
    await h.close()


@scenario
async def test_a_command_missing_an_argument_opens_the_form(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/greet")
        await pilot.press("enter")
        await until(pilot, lambda: bool(app.screen.query("#form-dialog")))
        await type_line(pilot, "ada")
        await pilot.click("#form-run")
        await until(pilot, lambda: "/greet: hello ada" in app.toasts)
        assert not app.screen.query("#form-dialog")
    await h.close()


@scenario
async def test_the_form_refuses_an_empty_required_field_and_cancel_runs_nothing(
    tmp_path: Path,
) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/greet")
        await pilot.press("enter")
        await until(pilot, lambda: bool(app.screen.query("#form-dialog")))
        await pilot.click("#form-run")
        await until(
            pilot,
            lambda: any(
                "a value is needed" in str(w.render()) for w in app.screen.query(".form-error")
            ),
        )
        assert app.screen.query("#form-dialog"), "the form stays"
        await pilot.press("escape")
        await until(pilot, lambda: not app.screen.query("#form-dialog"))
        assert app.toasts == []
    await h.close()


@scenario
async def test_the_form_reads_a_switch(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/greet who=ada")
        await pilot.press("enter")
        await until(pilot, lambda: "/greet: hello ada" in app.toasts)
        await type_line(pilot, "/greet")
        await pilot.press("enter")
        await until(pilot, lambda: bool(app.screen.query("#form-dialog")))
        await type_line(pilot, "bob")
        await pilot.click("#form-shout Switch")
        await pilot.click("#form-run")
        await until(pilot, lambda: "/greet: HELLO BOB" in app.toasts)
    await h.close()


@scenario
async def test_a_failing_command_is_an_error_toast(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/explode")
        await pilot.press("enter")
        await until(pilot, lambda: any("it blew up" in t for t in app.toasts))
    await h.close()


@scenario
async def test_an_unknown_command_is_sent_as_text(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/nope at all")
        await pilot.press("enter")
        await shows(pilot, app, "[User] /nope at all")
        assert "no command /nope: sent as text" in app.toasts
    await h.close()


@scenario
async def test_a_name_two_extensions_share_is_refused_and_nothing_is_sent(tmp_path: Path) -> None:
    class Twin:
        name = "twin"
        commands = (tuikit_greet,)

    h = await Harness.create(tmp_path, provider=FakeProvider([]), extensions=[Greeter(), Twin()])
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/greet ada")
        await pilot.press("enter")
        await until(pilot, lambda: any("is two commands" in t for t in app.toasts))
        assert app.draft == "/greet ada", "the refused line stays to be edited"
        assert not [n for n in h.nodes() if isinstance(n.body, Body.User)]
    await h.close()


from tuikit import greet as tuikit_greet  # noqa: E402


def make_files(tmp_path: Path) -> Path:
    where = tmp_path / "files"
    where.mkdir()
    (where / "a b.txt").write_text("x", encoding="utf-8")
    (where / "dangling").symlink_to(where / "nowhere")
    os.mkfifo(where / "fifo")
    return where


@scenario
async def test_tab_on_an_at_word_quotes_a_path_with_spaces(tmp_path: Path) -> None:
    where = make_files(tmp_path)
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, f"see @{where}/a")
        await pilot.press("tab")
        await until(pilot, lambda: app.draft == f'see @"{where}/a b.txt"')
    await h.close()


@pytest.mark.parametrize("name", ["d", "f"])
@scenario
async def test_tab_on_an_at_word_does_not_offer_a_link_to_nothing_or_a_fifo(
    tmp_path: Path, name: str
) -> None:
    where = make_files(tmp_path)
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, f"@{where}/{name}")
        await pilot.press("tab")
        await pilot.pause(0.3)
        assert app.draft == f"@{where}/{name}", f"{name} would not attach, so it is not offered"
    await h.close()
