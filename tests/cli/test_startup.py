"""load_config and select_model: what a frontend does before its first sample (D13)."""

import io

import pytest
from world import EOF, World, models, terminal

from tau_next import (
    ConfigError,
    ModelCatalog,
    UntrustedProject,
    load_config,
    prompts,
    read_models,
    select_model,
)


def test_no_project_reads_the_user_config_and_passes_the_environ_through(world: World) -> None:
    world.user_config(models(a={}))
    work = world.home / "work"
    work.mkdir()
    loaded = load_config(work, world.environ, ask=None)
    assert loaded.catalog.names() == ("a",)
    assert loaded.project is None
    assert loaded.home == world.tau
    assert dict(loaded.environ) == world.environ


def test_no_user_config_at_all_is_an_empty_catalog(world: World) -> None:
    loaded = load_config(world.home, world.environ, ask=None)
    assert loaded.catalog.names() == ()


def test_cli_overrides_are_the_nearest_layer(world: World) -> None:
    world.user_config({**models(a={}), "default_model": "a"})
    cli = {"models": {"a": {"model": "other"}}}
    loaded = load_config(world.home, world.environ, ask=None, cli=cli)
    assert loaded.catalog.resolve("a").model == "other"


def test_an_untrusted_project_without_a_terminal_raises(world: World) -> None:
    root = world.project(config=models(p={}))
    with pytest.raises(UntrustedProject, match="tau trust"):
        load_config(root, world.environ, ask=None)


def test_an_untrusted_project_with_a_yes_is_trusted_and_loaded(world: World) -> None:
    root = world.project(config=models(p={}))
    asked: list[str] = []

    def yes(report: str) -> bool:
        asked.append(report)
        return True

    loaded = load_config(root, world.environ, ask=yes)
    assert loaded.catalog.names() == ("p",)
    assert loaded.project is not None
    assert len(asked) == 1
    again = load_config(root, world.environ, ask=None)  # trusted now: no question needed
    assert again.catalog.names() == ("p",)


def test_a_config_error_propagates(world: World) -> None:
    world.user_config('{"models": ')
    with pytest.raises(ConfigError, match=r"config\.json"):
        load_config(world.home, world.environ, ask=None)


# -- selection


def grouped(default: str | None = None) -> ModelCatalog:
    cfg: dict[str, object] = {
        "models": {
            "free": {"base_url": "http://a/v1", "models": {"sol": {"model": "sol"}}},
            "paid": {"base_url": "http://b/v1", "models": {"sol": {"model": "sol"}}},
            "solo": {"model": "x", "base_url": "http://c/v1"},
        }
    }
    if default:
        cfg["default_model"] = default
    return read_models(cli=cfg)


def test_a_qualified_name_is_exact() -> None:
    assert select_model(grouped(), "paid:sol", pick=None) == "paid:sol"


def test_a_unique_bare_name_is_its_qualified_name() -> None:
    assert select_model(grouped(), "solo", pick=None) == "solo"


def test_an_ambiguous_bare_name_without_a_terminal_lists_the_qualified_names() -> None:
    with pytest.raises(ConfigError, match=r"ambiguous.*free:sol.*paid:sol"):
        select_model(grouped(), "sol", pick=None)


def test_an_ambiguous_bare_name_asks_a_live_terminal() -> None:
    seen: list[tuple[str, ...]] = []

    def pick(names: tuple[str, ...]) -> str:
        seen.append(names)
        return names[1]

    assert select_model(grouped(), "sol", pick=pick) == "paid:sol"
    assert seen == [("free:sol", "paid:sol")]


def test_an_unknown_name_suggests_close_matches() -> None:
    with pytest.raises(ConfigError, match=r"did you mean.*paid:sol"):
        select_model(grouped(), "paid:sool", pick=None)
    with pytest.raises(ConfigError, match=r"did you mean.*solo"):
        select_model(grouped(), "sol0", pick=None)


def test_an_unknown_name_with_nothing_close_lists_what_exists() -> None:
    with pytest.raises(ConfigError, match=r"not in your config.*available: free:sol"):
        select_model(grouped(), "zzzzzz", pick=None)


def test_default_model_is_used_when_nothing_is_requested() -> None:
    assert select_model(grouped("paid:sol"), None, pick=None) == "paid:sol"
    assert select_model(grouped("paid:sol"), "solo", pick=None) == "solo"


def test_no_request_and_no_default_says_how_to_set_one() -> None:
    with pytest.raises(ConfigError, match="default_model"):
        select_model(grouped(), None, pick=None)


def test_suggestions_escape_control_characters() -> None:
    with pytest.raises(ConfigError) as caught:
        select_model(grouped(), "sol\x1b[2J", pick=None)
    assert "\x1b" not in str(caught.value)


def test_an_empty_requested_name_is_not_the_default() -> None:
    with pytest.raises(ConfigError, match="must not be empty"):
        select_model(grouped("solo"), "", pick=None)


PAIR = ("free:sol", "paid:sol")


def test_prompts_are_absent_without_a_terminal() -> None:
    quiet = prompts(io.StringIO(), io.StringIO())
    assert quiet.ask is None
    assert quiet.pick is None


@pytest.mark.parametrize(("typed", "chosen"), [("2\n", "paid:sol"), ("free:sol\n", "free:sol")])
def test_a_live_terminal_picks_by_number_or_by_name(typed: str, chosen: str) -> None:
    with terminal(typed) as term:
        assert term.pick is not None
        assert term.pick(PAIR) == chosen
        assert "1. free:sol" in term.screen()


@pytest.mark.parametrize(("typed", "message"), [("7\n", "no model chosen"), (EOF, "no answer")])
def test_a_live_terminal_refuses_anything_else_and_end_of_input(typed: str, message: str) -> None:
    with terminal(typed) as term:
        assert term.pick is not None
        with pytest.raises(ConfigError, match=message):
            term.pick(PAIR)


def test_the_pick_menu_escapes_control_characters() -> None:
    with terminal("1\n") as term:
        assert term.pick is not None
        term.pick(("a\x1b[2Jb", "c"))
        screen = term.screen()
        assert "a\\x1b[2Jb" in screen
        assert "\x1b[2J" not in screen
