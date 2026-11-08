"""Session settings (D13): the layer nearest the run, from `--set` and `TAU_SETTINGS`, for this
start only. Through `tau.cli.main` with the mock model; `--verbose` lists what the start loaded."""

import json

import pytest
from world import Result, World, tau

MOCK = {
    "default_model": "m",
    "models": {"m": {"model": "m", "provider": "mock", "options": {"delay_ms": 0}}},
}
RETRY_ROW = "tau: retry (built-in, provider wrapper)"


@pytest.fixture
def configured(world: World) -> World:
    world.user_config(MOCK)
    return world


def p(world: World, *argv: str) -> Result:
    return tau(world, "-p", "--verbose", *argv, "go")


def test_retry_is_on_unless_a_session_setting_turns_it_off(configured: World) -> None:
    assert RETRY_ROW in p(configured).err
    off = p(configured, "--set", "load.retry=false")
    assert (off.code, off.out) == (0, "mock: go\n")
    assert RETRY_ROW not in off.err
    assert RETRY_ROW in p(configured).err, "for that run only"


def test_a_set_writes_no_file(configured: World) -> None:
    before = (configured.tau / "config.json").read_text(encoding="utf-8")
    assert p(configured, "--set", "load.retry=false", "--set", "step_limit.limit=9").code == 0
    assert (configured.tau / "config.json").read_text(encoding="utf-8") == before


def test_the_environment_carries_the_same_settings(configured: World) -> None:
    configured.environ["TAU_SETTINGS"] = json.dumps({"load": {"retry": False}})
    assert RETRY_ROW not in p(configured).err
    configured.environ["TAU_SETTINGS"] = json.dumps({"extensions": {"step_limit": {"limit": 1}}})
    assert tau(configured, "-p", "go").code == 3, "a step limit of 1 stops the run"


def test_set_is_nearer_than_the_environment_and_both_than_the_files(configured: World) -> None:
    configured.user_config({**MOCK, "extensions": {"step_limit": {"limit": 1}}})
    assert tau(configured, "-p", "go").code == 3
    configured.environ["TAU_SETTINGS"] = json.dumps({"extensions": {"step_limit": {"limit": 50}}})
    assert tau(configured, "-p", "go").code == 0, "the environment over the file"
    configured.environ["TAU_SETTINGS"] = json.dumps({"extensions": {"step_limit": {"limit": 1}}})
    assert tau(configured, "-p", "--set", "step_limit.limit=50", "go").code == 0, "--set over it"


def test_the_environment_and_set_merge_by_key(configured: World) -> None:
    configured.environ["TAU_SETTINGS"] = json.dumps({"load": {"retry": False}})
    both = p(configured, "--set", "load.usage=false")
    assert RETRY_ROW not in both.err
    assert "tau: extension usage" not in both.err


@pytest.mark.parametrize(
    ("value", "complaint"),
    [
        ("{", "TAU_SETTINGS is not JSON"),
        ("[1]", "TAU_SETTINGS must be a JSON object"),
        ('{"models": {}}', "TAU_SETTINGS sets 'models'"),
        ('{"load": {"retry": 1}}', "'load.retry' must be true or false"),
        ('{"extensions": {"step_limit": {"limit": 0}}}', "a step limit must be at least 1"),
    ],
)
def test_a_bad_environment_is_a_config_error_before_any_session(
    configured: World, value: str, complaint: str
) -> None:
    configured.environ["TAU_SETTINGS"] = value
    result = tau(configured, "-p", "go")
    assert (result.code, result.out) == (1, "")
    assert complaint in result.err
    assert not (configured.tau / "sessions").exists()


def test_an_environment_naming_an_extension_nobody_has_is_a_warning(configured: World) -> None:
    configured.environ["TAU_SETTINGS"] = json.dumps(
        {"load": {"deleted": False}, "extensions": {"deleted": {"x": 1}}}
    )
    result = tau(configured, "-p", "go")
    assert (result.code, result.out) == (0, "mock: go\n")
    assert "warning: load.deleted: no extension source has the key 'deleted'" in result.err
    assert "warning: extensions.deleted: no loaded extension is named 'deleted'" in result.err


def test_a_set_that_names_no_source_is_a_typo_and_stops_the_start(configured: World) -> None:
    result = tau(configured, "-p", "--set", "load.retyr=false", "go")
    assert (result.code, result.out) == (1, "")
    assert "--set load.retyr: no extension source has the key 'retyr'" in result.err


def test_an_option_the_extension_does_not_have_is_a_warning_and_the_run_goes_on(
    configured: World,
) -> None:
    configured.user_config({**MOCK, "extensions": {"step_limit": {"limit": 5, "dropped": 1}}})
    result = tau(configured, "-p", "go")
    assert (result.code, result.out) == (0, "mock: go\n")
    assert (
        "warning: extensions.step_limit.dropped: extension 'step_limit' has no option" in result.err
    )
    assert tau(configured, "config", "set", "step_limit.dropped=1").code == 1, "setting is strict"
    configured.user_config({**MOCK, "extensions": {"step_limit": {"limit": 0, "dropped": 1}}})
    assert tau(configured, "-p", "go").code == 1, "a refused value is still an error"


def test_a_session_setting_is_named_session_in_a_problem(configured: World) -> None:
    configured.environ["TAU_SETTINGS"] = json.dumps({"load": {"retry": 1}})
    assert "session: error:" in tau(configured, "-p", "go").err
