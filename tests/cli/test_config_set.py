"""`tau config set EXTENSION.FIELD=VALUE ...`: τ writes the user's config file (D1)."""

import json
import os
import re
import sys

import pytest
from world import World, loaded, models, tau, trust

from tau import ConfigError, UntrustedProject, load_config


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
    if sys.platform != "win32":  # Windows has no mode bits: its st_mode always says 666/777
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


def test_set_writes_through_a_symlinked_file_and_leaves_the_link(world: World) -> None:
    dotfiles = world.home / "dotfiles"
    dotfiles.mkdir()
    target = dotfiles / "config.json"
    target.write_text(json.dumps({"extensions": {"step_limit": {"limit": 5}}}), encoding="utf-8")
    world.tau.mkdir(mode=0o700)
    link = world.tau / "config.json"
    link.symlink_to(target)
    assert tau(world, "config", "set", "step_limit.limit=7").code == 0
    assert link.is_symlink(), "the link is still a link"
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "extensions": {"step_limit": {"limit": 7}}
    }
    assert sorted(p.name for p in dotfiles.iterdir()) == ["config.json"], "no scratch file"


def test_a_dangling_config_symlink_is_an_error_naming_it_not_no_file(world: World) -> None:
    world.tau.mkdir(mode=0o700)
    link = world.tau / "config.json"
    link.symlink_to(world.home / "nowhere" / "config.json")
    result = tau(world, "config", "set", "step_limit.limit=7")
    assert (result.code, result.out) == (1, "")
    assert str(link) in result.err
    assert link.is_symlink()
    assert not (world.home / "nowhere").exists(), "nothing was created behind the link"
    result = tau(world, "config", "check")
    assert result.code != 0
    assert str(link) in result.both
    with pytest.raises(ConfigError, match=re.escape(str(link))):
        loaded(world)


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


def test_set_refuses_a_rule_that_names_a_tool_nothing_provides_and_writes_nothing(
    world: World,
) -> None:
    path = world.user_config({"extensions": {"permissions": {"deny": ["bash"]}}})
    before = path.read_bytes()
    result = tau(world, "config", "set", 'permissions.deny=["bash", "nosuch"]')
    assert (result.code, result.out) == (1, "")
    assert "deny rule 'nosuch' names the tool 'nosuch'" in result.err
    assert path.read_bytes() == before
    assert tau(world, "config", "set", 'permissions.deny=["bash", "write"]').code == 0
    assert tau(world, "config", "check").code == 0


def test_set_refuses_agent_tools_that_nothing_provides(world: World) -> None:
    path = world.user_config({"load": {"agents": True}})
    before = path.read_bytes()
    result = tau(world, "config", "set", 'agents.tools=["read", "grpe"]')
    assert (result.code, result.out) == (1, "")
    assert "tools names 'grpe'" in result.err
    assert path.read_bytes() == before


def test_set_checks_only_the_fields_it_writes(world: World) -> None:
    world.user_config({"extensions": {"permissions": {"deny": ["nosuch"]}}})
    result = tau(world, "config", "set", 'permissions.ask=["bash", "read"]')
    assert result.code == 0, result.err
    assert file(world)["extensions"] == {  # type: ignore[comparison-overlap]
        "permissions": {"deny": ["nosuch"], "ask": ["bash", "read"]}
    }, "the old deny stays a warning"
    assert tau(world, "config", "set", 'permissions.deny=["nosuch"]').code == 1
