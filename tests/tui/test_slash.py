"""`/` commands: the popup, Tab, the form for missing arguments, results and refusals (D23)."""

import os
import sys
from pathlib import Path

import pytest
from fakes import FakeProvider
from harness import Harness, scenario
from tuikit import Greeter, running, say, shows, type_line, until
from world import World, loaded

from tau import Body, Skills


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
    (where / "a b.txt").write_bytes(b"x")
    return where


def make_specials(where: Path) -> None:
    """A link to nothing and a FIFO, which an `@` word never offers."""
    (where / "dangling").symlink_to(where / "nowhere")
    os.mkfifo(where / "fifo")


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
@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no os.mkfifo")
async def test_tab_on_an_at_word_does_not_offer_a_link_to_nothing_or_a_fifo(
    tmp_path: Path, name: str
) -> None:
    where = make_files(tmp_path)
    make_specials(where)
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, f"@{where}/{name}")
        await pilot.press("tab")
        await pilot.pause(0.3)
        assert app.draft == f"@{where}/{name}", f"{name} would not attach, so it is not offered"
    await h.close()


@scenario
async def test_the_argument_hint_is_in_the_list_and_after_the_name(
    tmp_path: Path, world: World
) -> None:
    (world.tau / "prompts").mkdir(parents=True)
    (world.tau / "prompts" / "review.md").write_text(
        "---\ndescription: Review a file\nargument-hint: <file> [focus]\n---\nReview $1",
        encoding="utf-8",
    )
    h = await Harness.create(
        tmp_path, provider=FakeProvider([say("ok")]), extensions=[Skills.of(loaded(world))]
    )
    async with running(h, tmp_path) as (app, pilot):
        await pilot.press("slash")
        await shows(pilot, app, "/review <file> [focus]  Review a file")
        await type_line(pilot, "review ")
        await until(pilot, lambda: app.draft == "/review ")
        await shows(pilot, app, "/review <file> [focus]  Review a file")
        await pilot.press("x")
        await until(pilot, lambda: "<file> [focus]" not in app.screen_text())
    await h.close()


@scenario
async def test_an_unknown_qualified_command_is_an_error_and_stays_in_the_editor(
    tmp_path: Path,
) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/skills:nope")
        await shows(pilot, app, "/skills:nope is not a command")
        assert "goes to the model" not in app.screen_text()
        await pilot.press("enter")
        await until(pilot, lambda: "no command /skills:nope" in app.toasts)
        await until(pilot, lambda: app.draft == "/skills:nope")
        assert [b for b in h.bodies() if isinstance(b, Body.User)] == []
    await h.close()


@scenario
async def test_a_toast_shows_square_brackets_as_they_are(tmp_path: Path) -> None:
    class Brackets:
        name = "brackets"

        def say(self) -> str:
            """Fails with a bracket."""
            raise ValueError("no [draft] [/bold] here")

        @property
        def commands(self) -> tuple[object, ...]:
            return (self.say,)

    h = await Harness.create(tmp_path, provider=FakeProvider([say("ok")]), extensions=[Brackets()])
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/say")
        await pilot.press("enter")
        await until(pilot, lambda: any("no [draft] [/bold] here" in t for t in app.toasts))
        assert any("[draft] [/bold]" in t for t in app.toasts), "its brackets are shown as typed"
    await h.close()


@scenario
async def test_a_template_added_while_the_app_runs_is_in_the_list(
    tmp_path: Path, world: World
) -> None:
    h = await Harness.create(
        tmp_path, provider=FakeProvider([say("ok")]), extensions=[Skills.of(loaded(world))]
    )
    async with running(h, tmp_path) as (app, pilot):
        (world.tau / "prompts").mkdir(parents=True)
        (world.tau / "prompts" / "late.md").write_text("Late $1", encoding="utf-8")
        await pilot.press("slash")
        await shows(pilot, app, "/late")
    await h.close()
