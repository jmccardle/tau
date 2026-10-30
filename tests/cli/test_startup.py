"""load_config and select_model: what a frontend does before its first sample (D13)."""

import io
import os
from pathlib import Path

import pytest
from world import World, models

from tau_next import (
    ConfigError,
    ModelCatalog,
    UntrustedProject,
    approve_project,
    load_config,
    prompts,
    read_models,
    select_model,
    terminal_pick,
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


def test_a_trusted_projects_dotenv_reaches_environ_and_the_shell_wins(world: World) -> None:
    root = world.project(config=models(p={"api_key_env": "P_KEY"}))
    (root / ".env").write_text("P_KEY=from-dotenv\nSHELL=from-dotenv\n", encoding="utf-8")
    approve_project(root, world.tau)
    loaded = load_config(root, {**world.environ, "SHELL": "from-shell"}, ask=None)
    assert loaded.environ["P_KEY"] == "from-dotenv"
    assert loaded.environ["SHELL"] == "from-shell"


def test_an_untrusted_projects_dotenv_is_not_read(world: World) -> None:
    root = world.project(config=models(p={}))
    (root / ".env").write_text("LEAK=1\n", encoding="utf-8")
    with pytest.raises(UntrustedProject):
        load_config(root, world.environ, ask=None)


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


def test_terminal_pick_reads_a_number_or_a_name_and_refuses_the_rest() -> None:
    names = ("free:sol", "paid:sol")
    out = io.StringIO()
    assert terminal_pick(io.StringIO("2\n"), out)(names) == "paid:sol"
    assert "1. free:sol" in out.getvalue()
    assert terminal_pick(io.StringIO("free:sol\n"), out)(names) == "free:sol"
    with pytest.raises(ConfigError, match="no model chosen"):
        terminal_pick(io.StringIO("7\n"), out)(names)
    with pytest.raises(ConfigError, match="no model chosen"):
        terminal_pick(io.StringIO(""), out)(names)


def test_prompts_are_absent_without_a_terminal_and_present_on_one() -> None:
    quiet = prompts(io.StringIO(), io.StringIO())
    assert quiet.ask is None
    assert quiet.pick is None
    master, slave = os.openpty()
    with (
        os.fdopen(os.dup(slave), "w") as tty_out,
        os.fdopen(slave, "r") as tty_in,
        os.fdopen(master, "w") as typed,
    ):
        typed.write("2\n")
        typed.flush()
        live = prompts(tty_in, tty_out)
        assert live.ask is not None
        assert live.pick is not None
        assert live.pick(("free:sol", "paid:sol")) == "paid:sol"


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


def test_a_project_directory_is_found_from_a_subdirectory(world: World) -> None:
    root = world.project(config=models(p={}))
    approve_project(root, world.tau)
    sub: Path = root / "src" / "deep"
    sub.mkdir(parents=True)
    assert load_config(sub, world.environ, ask=None).project is not None
