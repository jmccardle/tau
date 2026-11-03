"""`/config` forms with "save for every session", the `/model` picker and the theme the person
picks (D23 sections 3, 10; ruling 3): what is written, and to which layer."""

import json
from dataclasses import dataclass
from pathlib import Path

from fakes import FAKE_PROVIDER, FakeProvider
from harness import Harness, scenario
from textual.command import CommandPalette
from textual.widgets import Input, OptionList
from tuikit import Tuner, running, say, shows, type_line, until

from tau import Body, ModelCatalog, UserConfig, read_models


class Other:
    name = "other"

    @dataclass(frozen=True)
    class Options:
        depth: int = 2

    options = Options


def two_models() -> ModelCatalog:
    spec = {"provider": FAKE_PROVIDER}
    return read_models(
        cli={"models": {"fake": {"model": "fake", **spec}, "spare": {"model": "spare", **spec}}}
    )


def saved(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "tau-home" / "config.json").read_text(encoding="utf-8"))


async def harness(tmp_path: Path) -> Harness:
    return await Harness.create(
        tmp_path,
        provider=FakeProvider([say("ok")]),
        catalog=two_models(),
        extensions=[Tuner(), Other()],
        user_config=UserConfig(tmp_path / "tau-home"),
    )


def configs(h: Harness) -> list[str]:
    """The `Config` nodes written since the session began (its own first one names the model)."""
    return [n.body.config.data for n in h.nodes() if isinstance(n.body, Body.Config)][1:]


async def dialog(pilot, app, selector: str) -> None:
    await until(pilot, lambda: bool(app.screen.query(selector)))


@scenario
async def test_config_opens_the_options_with_their_values_and_applies_what_changed(
    tmp_path: Path,
) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/config tuner")
        await pilot.press("enter")
        await dialog(pilot, app, "#options-dialog")
        labels = [str(w.render()) for w in app.screen.query(".form-label")]
        assert any(label.startswith("size") and "how big" in label for label in labels)
        assert [w.placeholder for w in app.screen.query(Input)] == ["10"]
        await type_line(pilot, "4")
        await pilot.click("#form-run")
        await until(pilot, lambda: bool(configs(h)))
        assert json.loads(configs(h)[0]) == {"extensions": {"tuner": {"size": 4}}}
        await until(pilot, lambda: any("size=4 (on this branch)" in t for t in app.toasts))
        assert not (tmp_path / "tau-home" / "config.json").exists(), "this branch only"
    await h.close()


@scenario
async def test_config_with_save_writes_the_users_file_as_well(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/config tuner")
        await pilot.press("enter")
        await dialog(pilot, app, "#options-dialog")
        await type_line(pilot, "7")
        await pilot.click("#form-save")
        await pilot.click("#form-run")
        await until(pilot, lambda: any("size=7 (for every session)" in t for t in app.toasts))
        assert saved(tmp_path) == {"extensions": {"tuner": {"size": 7}}}
        assert json.loads(configs(h)[0]) == {"extensions": {"tuner": {"size": 7}}}
    await h.close()


@scenario
async def test_a_value_the_option_refuses_writes_nothing_and_says_why(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/config tuner")
        await pilot.press("enter")
        await dialog(pilot, app, "#options-dialog")
        await type_line(pilot, "0")
        await pilot.click("#form-save")
        await pilot.click("#form-run")
        await until(pilot, lambda: any("at least 1" in t for t in app.toasts))
        assert configs(h) == []
        assert not (tmp_path / "tau-home" / "config.json").exists()
        assert app.draft == "/config tuner", "the line goes back to the editor"
    await h.close()


@scenario
async def test_config_alone_asks_which_extension_then_shows_its_options(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/config")
        await pilot.press("enter")
        await dialog(pilot, app, "#pick-dialog")
        await type_line(pilot, "oth")
        await pilot.press("enter")
        await dialog(pilot, app, "#options-dialog")
        assert [w.placeholder for w in app.screen.query(Input)] == ["2"]
        await pilot.press("escape")
        await until(pilot, lambda: not app.screen.query("#options-dialog"))
        assert configs(h) == []
    await h.close()


@scenario
async def test_an_unchanged_form_changes_nothing(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/config tuner")
        await pilot.press("enter")
        await dialog(pilot, app, "#options-dialog")
        await pilot.click("#form-run")
        await until(pilot, lambda: any("nothing changed" in t for t in app.toasts))
        assert configs(h) == []
    await h.close()


@scenario
async def test_model_alone_opens_a_picker_of_the_models_and_switches_to_the_one_picked(
    tmp_path: Path,
) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/model")
        await pilot.press("enter")
        await dialog(pilot, app, "#pick-dialog")
        rows = [
            str(app.screen.query_one("#pick-list", OptionList).get_option_at_index(i).prompt)
            for i in range(2)
        ]
        assert rows == ["fake  (current)", "spare"]
        await pilot.press("down")
        await pilot.press("enter")
        await shows(pilot, app, "model: spare")
        assert json.loads(configs(h)[-1]) == {"model": "spare"}
        assert not app.screen.query("#pick-dialog")
    await h.close()


@scenario
async def test_the_model_picker_filters_and_escape_changes_nothing(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/model")
        await pilot.press("enter")
        await dialog(pilot, app, "#pick-dialog")
        await type_line(pilot, "spa")
        await until(pilot, lambda: app.screen.query_one("#pick-list", OptionList).option_count == 1)
        await pilot.press("escape")
        await until(pilot, lambda: not app.screen.query("#pick-dialog"))
        assert configs(h) == []
        assert app.draft == "/model", "the line goes back to the editor"
    await h.close()


async def pick_theme(pilot, app, name: str) -> None:
    await pilot.press("ctrl+p")
    await until(pilot, lambda: isinstance(app.screen, CommandPalette))
    await type_line(pilot, "theme")
    await pilot.pause()
    await pilot.press("enter")
    await dialog(pilot, app, "#pick-dialog")
    await type_line(pilot, name)
    await pilot.press("enter")


@scenario
async def test_a_theme_picked_in_the_palette_is_worn_and_saved_for_every_session(
    tmp_path: Path,
) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path, theme="gruvbox") as (app, pilot):
        assert app.theme == "gruvbox"
        assert not (tmp_path / "home" / "config.json").exists(), "--theme is for this run only"
        await pick_theme(pilot, app, "latte")
        await until(pilot, lambda: app.theme == "latte")
        assert json.loads((tmp_path / "home" / "config.json").read_text()) == {
            "tui": {"theme": "latte"}
        }
        assert "latte (saved for every session)" in "\n".join(app.toasts)
    await h.close()


@scenario
async def test_a_theme_that_cannot_be_saved_is_not_worn(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("{not json", encoding="utf-8")
    async with running(h, tmp_path, home=home) as (app, pilot):
        await pick_theme(pilot, app, "latte")
        await until(pilot, lambda: any(t for t in app.toasts if "config.json" in t))
        assert app.theme == "mocha"
    await h.close()


@scenario
async def test_the_options_form_shows_the_values_now_not_those_from_when_the_app_began(
    tmp_path: Path,
) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path) as (app, pilot):
        other = await h.connect("you:cli")
        await other.configure(h.cursor, "tuner", {"size": 5})
        await until(pilot, lambda: any('"size": 5' in c for c in configs(h)) or bool(configs(h)))
        await type_line(pilot, "/config tuner")
        await pilot.press("enter")
        await dialog(pilot, app, "#options-dialog")
        assert [w.placeholder for w in app.screen.query(Input)] == ["5"]
        await other.close()
    await h.close()


@scenario
async def test_saving_the_value_the_branch_already_has_writes_it_to_the_file(
    tmp_path: Path,
) -> None:
    h = await harness(tmp_path)
    await h.client.configure(h.cursor, "tuner", {"size": 4})  # this branch only
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/config tuner")
        await pilot.press("enter")
        await dialog(pilot, app, "#options-dialog")
        await type_line(pilot, "4")
        await pilot.click("#form-save")
        await pilot.click("#form-run")
        await until(pilot, lambda: any("size=4 (for every session)" in t for t in app.toasts))
        assert saved(tmp_path) == {"extensions": {"tuner": {"size": 4}}}
    await h.close()


@scenario
async def test_saving_with_nothing_marked_leaves_a_file_that_matches_the_branch(
    tmp_path: Path,
) -> None:
    h = await harness(tmp_path)
    UserConfig(tmp_path / "tau-home").save_options({"tuner": {"loud": True}}, lambda _: None)
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/config tuner")
        await pilot.press("enter")
        await dialog(pilot, app, "#options-dialog")
        await pilot.click("#form-save")
        await pilot.click("#form-run")  # nothing marked: the server compares branch and file
        await until(
            pilot, lambda: any("the branch's options (for every session)" in t for t in app.toasts)
        )
        assert saved(tmp_path) == {"extensions": {"tuner": {"loud": True}}}
    await h.close()


@scenario
async def test_picking_the_theme_already_worn_still_saves_it(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    async with running(h, tmp_path, theme="latte") as (app, pilot):
        await pick_theme(pilot, app, "latte")
        await until(pilot, (tmp_path / "home" / "config.json").exists)
        assert json.loads((tmp_path / "home" / "config.json").read_text()) == {
            "tui": {"theme": "latte"}
        }
        assert app.theme == "latte"
    await h.close()
