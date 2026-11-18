"""`tau config check --clean` and `--purge`: take out of a config file what it names that τ does
not have. Which file: yours, unless `--layer project` says the project's."""

import json

from world import World, models, tau, trust

DIRTY = {
    **models(a={}),
    "load": {"deleted": True, "usage": True},
    "extensions": {
        "deleted": {"x": 1},
        "retry": {"x": 1},
        "step_limit": {"limit": 4, "bogus": 1},
        "permissions": {"deny": ["nosuch", "bash"], "ask": ["Bash"]},
    },
}


def file(world: World) -> dict[str, object]:
    return json.loads((world.tau / "config.json").read_text(encoding="utf-8"))


def test_check_only_warns_and_changes_nothing(world: World) -> None:
    path = world.user_config(DIRTY)
    before = path.read_bytes()
    result = tau(world, "config", "check")
    assert "warning: load.deleted: no extension source has the key 'deleted'" in result.out
    assert "warning: extensions.deleted: no loaded extension is named 'deleted'" in result.out
    assert path.read_bytes() == before


def test_clean_removes_what_is_missing_or_invalid_and_keeps_the_rest(world: World) -> None:
    world.user_config(DIRTY)
    result = tau(world, "config", "check", "--clean")
    assert result.code == 0, result.out
    assert f"user config {world.tau / 'config.json'}: removed" in result.out
    for gone in (
        "load.deleted",
        "extensions.deleted",
        "extensions.retry",
        "extensions.step_limit.bogus",
        "extensions.permissions.deny: 'nosuch'",
        "extensions.permissions.ask: 'Bash'",
    ):
        assert f"  {gone}\n" in result.out, gone
    assert file(world) == {
        **models(a={}),
        "load": {"usage": True},
        "extensions": {
            "step_limit": {"limit": 4},
            "permissions": {"deny": ["bash"]},
        },
    }
    again = tau(world, "config", "check")
    assert (again.code, again.out) == (0, "")


def test_clean_with_nothing_to_remove_writes_nothing(world: World) -> None:
    path = world.user_config('{"extensions":   {"step_limit": {"limit": 4}}}')
    before = path.read_bytes()
    assert tau(world, "config", "check", "--clean").code == 0
    assert path.read_bytes() == before


def test_purge_turns_the_extension_off_and_drops_all_its_options(world: World) -> None:
    world.user_config(DIRTY)
    result = tau(world, "config", "check", "--purge", "step_limit", "--purge", "deleted")
    assert result.code == 0, result.out
    saved = file(world)
    assert saved["load"] == {"usage": True, "step_limit": False}
    assert "step_limit" not in saved["extensions"]  # type: ignore[operator]
    assert "deleted" not in saved["extensions"]  # type: ignore[operator]
    assert "permissions" in saved["extensions"]  # type: ignore[operator]
    listed = tau(world, "ext", "list").out
    assert "load.step_limit = false" in listed
    assert tau(world, "config", "set", "step_limit.limit=2").code == 1, "it is off: no options"
    assert tau(world, "ext", "enable", "step_limit").code == 0
    assert tau(world, "config", "set", "step_limit.limit=2").code == 0
    assert file(world)["extensions"]["step_limit"] == {"limit": 2}  # type: ignore[index]


def test_the_projects_file_is_written_only_when_named_and_must_be_approved_again(
    world: World,
) -> None:
    world.user_config(DIRTY)
    root = world.project(config={"load": {"deleted": True}})
    trust(world, root)
    project = root / ".tau" / "config.json"
    untouched = tau(world, "config", "check", "--clean", cwd=root)
    assert json.loads(project.read_text(encoding="utf-8")) == {"load": {"deleted": True}}
    assert "load.deleted" in untouched.out
    assert "deleted" not in file(world)["load"]  # type: ignore[operator]
    result = tau(world, "config", "check", "--clean", "--layer", "project", cwd=root)
    assert result.code == 0, result.out
    assert f"project config {project}: removed" in result.out
    assert "run `tau trust` to approve it again" in result.out
    assert json.loads(project.read_text(encoding="utf-8")) == {}
    assert "not trusted" in tau(world, "config", "check", cwd=root).out


SHOUTER = '''
name = "shouter"

async def shout(text: str = "") -> str:
    """Shout."""
    return text.upper()

tools = (shout,)
'''


def shouter(world: World) -> None:
    directory = world.tau / "extensions"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "shouter.py").write_text(SHOUTER, encoding="utf-8")


def test_clean_keeps_the_config_of_an_extension_that_is_only_switched_off(world: World) -> None:
    shouter(world)
    user = {
        **models(a={}),
        "extensions": {
            "shouter": {"x": 1},
            "permissions": {"deny": ["shout", "nosuch"]},
            "agents": {"tools": ["read"]},
        },
    }
    world.user_config(user)
    root = world.project(config={"load": {"shouter": False}})
    trust(world, root)
    quiet = tau(world, "config", "check", cwd=root)
    assert "shouter" not in quiet.out, "options of an off extension are kept quietly"
    assert "agents" not in quiet.out
    result = tau(world, "config", "check", "--clean", cwd=root)
    assert result.code == 0, result.out
    assert "tool names are not checked: shouter" in result.out
    assert file(world) == user, "nothing of a switched-off extension, or its tool, was removed"


def test_clean_says_when_it_is_skipped_because_an_extension_failed_to_load(world: World) -> None:
    world.user_config(DIRTY)
    broken = world.tau / "extensions"
    broken.mkdir(parents=True)
    (broken / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    before = (world.tau / "config.json").read_bytes()
    result = tau(world, "config", "check", "--clean")
    assert result.code == 1
    assert "--clean/--purge skipped: an extension failed to load" in result.out
    assert (world.tau / "config.json").read_bytes() == before


def test_clean_drops_what_it_empties_and_keeps_the_files_mode(world: World) -> None:
    world.user_config({**models(a={}), "extensions": {"permissions": {"deny": ["nosuch"]}}})
    root = world.project(config={"extensions": {"permissions": {"ask": ["nosuch"]}}})
    trust(world, root)
    project = root / ".tau" / "config.json"
    project.chmod(0o644)
    result = tau(
        world, "config", "check", "--clean", "--layer", "user", "--layer", "project", cwd=root
    )
    assert result.code == 0, result.out
    assert file(world) == models(a={}), "the emptied list and the emptied options are gone"
    assert json.loads(project.read_text(encoding="utf-8")) == {}
    assert project.stat().st_mode & 0o777 == 0o644


def test_option_warnings_name_the_file_that_set_them(world: World) -> None:
    path = world.user_config({**models(a={}), "extensions": {"ghost": {"x": 1}}})
    assert f"{path}: warning: extensions.ghost: " in tau(world, "config", "check").out
