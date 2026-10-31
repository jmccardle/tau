"""`tau config set EXTENSION.FIELD=VALUE ...`: τ writes the user's config file (D1)."""

import json
import os

import pytest
from world import World, loaded, models, tau, trust

from tau import UntrustedProject, load_config


def file(world: World) -> dict[str, object]:
    return json.loads((world.tau / "config.json").read_text(encoding="utf-8"))


def test_set_writes_the_extensions_option_into_the_users_file(world: World) -> None:
    result = tau(world, "config", "set", "step_limit.limit=7")
    assert (result.code, result.err) == (0, "")
    assert "step_limit.limit=7" in result.out
    assert file(world) == {"extensions": {"step_limit": {"limit": 7}}}
    assert loaded(world).catalog.extensions == {"step_limit": {"limit": 7}}
    assert tau(world, "config", "check").code == 0


def test_set_creates_a_private_home_and_file_and_leaves_no_scratch_file(world: World) -> None:
    assert tau(world, "config", "set", "step_limit.limit=7").code == 0
    assert [p.name for p in world.tau.iterdir()] == ["config.json"]
    assert (world.tau / "config.json").stat().st_mode & 0o777 == 0o600
    assert world.tau.stat().st_mode & 0o777 == 0o700


def test_set_keeps_what_else_the_file_holds(world: World) -> None:
    world.user_config({**models(m={}), "default_model": "m", "extensions": {"step_limit": {}}})
    assert tau(world, "config", "set", "step_limit.limit=7").code == 0
    assert file(world) == {
        **models(m={}),
        "default_model": "m",
        "extensions": {"step_limit": {"limit": 7}},
    }


def test_set_reads_each_value_as_slash_config_does(world: World) -> None:
    world.user_config({"extensions": {"step_limit": {"limit": 2}}})
    result = tau(world, "config", "set", "step_limit.limit=0")
    assert result.code == 1
    assert "at least 1" in result.err
    result = tau(world, "config", "set", "step_limit.limit=many")
    assert result.code == 1
    assert "'many' is not an integer" in result.err
    assert file(world) == {"extensions": {"step_limit": {"limit": 2}}}


def test_set_that_the_options_type_refuses_leaves_the_file_as_it_was(world: World) -> None:
    path = world.user_config('{"extensions":   {"step_limit": {"limit": 2}}}')
    before = path.read_bytes()
    result = tau(world, "config", "set", "step_limit.limit=0")
    assert (result.code, result.out) == (1, "")
    assert "extension 'step_limit'" in result.err
    assert path.read_bytes() == before
    assert [p.name for p in world.tau.iterdir()] == ["config.json"]


def test_set_refuses_an_extension_or_field_it_does_not_know(world: World) -> None:
    result = tau(world, "config", "set", "nope.limit=1")
    assert result.code == 1
    assert "'nope' has no options" in result.err
    result = tau(world, "config", "set", "step_limit.speed=1")
    assert result.code == 1
    assert "speed" in result.err
    assert not world.tau.exists()


def test_set_changes_nothing_when_one_of_several_assignments_is_refused(world: World) -> None:
    result = tau(world, "config", "set", "step_limit.limit=3", "nope.x=1")
    assert result.code == 1
    assert not world.tau.exists()


def test_set_refuses_a_file_that_is_not_sound_and_leaves_it(world: World) -> None:
    path = world.user_config('{"extensions": ')
    result = tau(world, "config", "set", "step_limit.limit=7")
    assert result.code == 1
    assert "invalid JSON" in result.err
    assert path.read_text(encoding="utf-8") == '{"extensions": '


@pytest.mark.parametrize("argv", [("config", "set"), ("config", "set", "limit=3")])
def test_set_needs_extension_dot_field_equals_value(world: World, argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as stop:
        tau(world, *argv)
    assert stop.value.code == 2


def test_set_never_writes_the_project_layer_and_the_project_stays_trusted(world: World) -> None:
    root = world.project(config={"extensions": {"step_limit": {"limit": 9}}})
    trust(world, root)
    config = root / ".tau" / "config.json"
    before = config.read_bytes()
    assert tau(world, "config", "set", "step_limit.limit=7", cwd=root).code == 0
    assert config.read_bytes() == before
    assert file(world) == {"extensions": {"step_limit": {"limit": 7}}}
    assert load_config(root, world.environ, ask=None).project is not None, "still trusted"
    assert tau(world, "config", "check", cwd=root).code == 0


def test_set_works_from_a_project_that_is_not_trusted_yet(world: World) -> None:
    root = world.project(config={"extensions": {"step_limit": {"limit": 9}}})
    with pytest.raises(UntrustedProject):
        load_config(root, world.environ, ask=None)
    assert tau(world, "config", "set", "step_limit.limit=7", cwd=root).code == 0
    assert file(world) == {"extensions": {"step_limit": {"limit": 7}}}
    assert os.listdir(root / ".tau") == ["config.json"]
