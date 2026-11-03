"""What happens before the screen (D23 section 1, 9): `tau` with no `-p` is checked, and refuses,
before it takes the terminal. Driven through `tau.cli.main` with real files; the terminal is a
stream that says it is one (`Tty`), because no test may take the real one."""

import io
import json

import pytest
from world import Result, World

from tau import ConfigError, LayerText, Session, list_sessions, read_models
from tau.cli import main

MOCK = {
    "default_model": "m",
    "models": {"m": {"model": "m", "provider": "mock", "options": {"delay_ms": 0}}},
}
OLD = "20261030-091502-1c2d3e"


class Tty(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


def tui(world: World, *argv: str, terminal: bool = True) -> Result:
    """`tau` (the terminal UI) with the given flags."""
    stream = Tty if terminal else io.StringIO
    out, err = stream(), io.StringIO()
    code = main(
        list(argv),
        stdin=stream(),
        stdout=out,
        stderr=err,
        environ=world.environ,
        cwd=world.home,
    )
    return Result(code, out.getvalue(), err.getvalue())


@pytest.fixture
def configured(world: World) -> World:
    world.user_config(MOCK)
    return world


def test_without_a_terminal_it_is_a_usage_error_that_starts_no_session(configured: World) -> None:
    result = tui(configured, terminal=False)
    assert result.code == 2
    assert "needs a terminal" in result.err
    assert "tau -p" in result.err
    assert list_sessions(configured.tau / "sessions") == []


@pytest.mark.parametrize("flag", [("--json",), ("--answer", "x=y")])
def test_json_and_answer_are_for_print_mode(configured: World, flag: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as stop:
        tui(configured, *flag)
    assert stop.value.code == 2


def test_a_theme_is_for_the_screen_not_for_print_mode(configured: World) -> None:
    with pytest.raises(SystemExit) as stop:
        tui(configured, "-p", "--theme", "latte", "hello")
    assert stop.value.code == 2


def test_a_message_without_print_is_still_a_usage_error(configured: World) -> None:
    with pytest.raises(SystemExit) as stop:
        tui(configured, "hello")
    assert stop.value.code == 2


def test_an_unknown_theme_names_the_themes_and_starts_no_session(configured: World) -> None:
    result = tui(configured, "--theme", "nope")
    assert result.code == 1
    assert "nope" in result.err
    assert "mocha" in result.err
    assert list_sessions(configured.tau / "sessions") == []


def test_the_configs_theme_is_checked_too(configured: World) -> None:
    configured.user_config({**MOCK, "tui": {"theme": "nope"}})
    result = tui(configured)
    assert result.code == 1
    assert "nope" in result.err


@pytest.mark.parametrize(
    ("tui_key", "complaint"),
    [
        ({"colour": "x"}, "unknown key 'tui.colour'"),
        ({"theme": 3}, "'tui.theme' must be a string"),
        ("mocha", "'tui' must be an object"),
    ],
)
def test_the_tui_key_is_checked_like_the_rest_of_the_config(
    configured: World, tui_key: object, complaint: str
) -> None:
    configured.user_config({**MOCK, "tui": tui_key})
    result = tui(configured)
    assert result.code == 1
    assert complaint in result.err


def test_a_session_another_process_holds_is_leased(configured: World) -> None:
    work = configured.home / "work"
    work.mkdir()
    held = Session.load(configured.session(OLD, cwd=work))
    try:
        result = tui(configured, "--session", OLD)
    finally:
        held.close()
    assert result.code == 1
    assert f"tau --fork {OLD}" in result.err


def test_resume_without_a_terminal_is_a_usage_error_that_picks_nothing(configured: World) -> None:
    configured.session(OLD, cwd=configured.home)
    result = tui(configured, "-r", terminal=False)
    assert result.code == 2
    assert "needs a terminal" in result.err


def test_resume_with_no_session_to_choose_from_says_so_before_any_screen(
    configured: World,
) -> None:
    result = tui(configured, "-r")
    assert result.code == 1
    assert "no session in" in result.err
    assert "to resume" in result.err
    assert list_sessions(configured.tau / "sessions") == []


def test_the_version_names_no_screen(configured: World) -> None:
    result = tui(configured, "--version", terminal=False)
    assert result.code == 0
    assert result.out.startswith("tau ")


def test_a_project_config_may_not_set_the_tui_key() -> None:
    project = LayerText("project", json.dumps({"tui": {"theme": "latte"}}))
    with pytest.raises(ConfigError, match="tui"):
        read_models(None, project)


def test_a_broken_theme_file_reports_its_own_problem_not_an_unknown_theme(
    configured: World,
) -> None:
    (configured.tau / "themes").mkdir()
    (configured.tau / "themes" / "dusk.json").write_text("{not json")
    result = tui(configured, "--theme", "dusk")
    assert result.code == 1
    assert "dusk.json" in result.err
    assert "not valid JSON" in result.err
