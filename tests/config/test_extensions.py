"""Loading extensions (D19): each source and the one rule for what it exports, load order and
shadowing, `load`, `-e` and `--no-extensions`, every message of a start that does not happen, and
`tau ext`. Through `tau.load_extensions`, `tau.list_extensions` and `tau.cli.main` on real files.
"""

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from world import World, loaded, models, tau, trust

from tau import (
    ExtensionLoadError,
    LoadedConfig,
    UserConfig,
    list_extensions,
    load_extensions,
)

SEARCH_SITE = Path(__file__).parents[1] / "fixtures" / "search_site"
"""A directory on `PYTHONPATH` holding tau-search's packaging shape: the package and a
`.dist-info` registering it in `tau.extensions`, as an install leaves them."""
OTHER_SITE = Path(__file__).parents[1] / "fixtures" / "search_site_other"
PACKAGE = "ffwf-tau-search-fixture 0.4.0"
POINTS_FILE = "entry_points.txt"  # rules: allow(entry): the file a distribution registers in
MODULE = """
name = "{name}"
{extra}
"""
WITH_COMMAND = '''
name = "{name}"

async def {command}(text: str = "") -> str:
    """Say it."""
    return "{name} says " + text

commands = ({command},)
'''


def user_dir(world: World) -> Path:
    path = world.tau / "extensions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def user_file(world: World, key: str, text: str | None = None) -> Path:
    return write(user_dir(world) / f"{key}.py", text or MODULE.format(name=key, extra=""))


def project_file(root: Path, relative: str, text: str) -> Path:
    return write(root / ".tau" / "extensions" / relative, text)


def names(extensions: Sequence[object]) -> list[str]:
    return [str(getattr(e, "name")) for e in extensions]  # noqa: B009


def rows(world: World, *argv: str, cwd: Path | None = None) -> dict[str, dict[str, object]]:
    """`tau ext list --json`, by key (a key listed twice keeps the last row)."""
    result = tau(world, "ext", "list", "--json", *argv, cwd=cwd)
    assert result.out, result.err
    return {r["key"]: r for r in map(json.loads, result.out.splitlines())}


def start(world: World, cwd: Path | None = None) -> LoadedConfig:
    return loaded(world, cwd)


# -- the rule for what a source exports (C3)


def test_a_module_with_a_name_is_the_extension(world: World) -> None:
    user_file(world, "ralph")
    assert names(load_extensions(start(world))) == ["step_limit", "ralph"]


def test_otherwise_its_extensions_list_is_added_in_order(world: World) -> None:
    user_file(
        world,
        "pair",
        "class A:\n    name = 'a'\n\nclass B:\n    name = 'b'\n\nextensions = [A(), B()]\n",
    )
    assert names(load_extensions(start(world)))[-2:] == ["a", "b"]


@pytest.mark.parametrize(
    ("body", "seen"),
    [
        ("x = 1\n", ""),
        ("extensions = []\n", " (its `extensions` is [])"),
        ("extensions = 'ralph'\n", " (its `extensions` is 'ralph')"),
    ],
)
def test_a_module_that_follows_neither_rule_is_an_error_naming_the_source_and_the_rule(
    world: World, body: str, seen: str
) -> None:
    path = user_file(world, "odd", body)
    with pytest.raises(ExtensionLoadError) as caught:
        load_extensions(start(world))
    message = str(caught.value)
    assert f"source: {path}" in message
    assert (
        f"{path} provides no extension: a module with a `name` (a str) is the extension" in message
    )
    assert "`extensions = [...]`, a non-empty list of extension instances" + seen in message


def test_a_registration_must_name_a_module_not_module_colon_attr(tmp_path: Path) -> None:
    result = hermetic(tmp_path, {"colon": ("colon_ext:thing", "name = 'colon'\nthing = 1\n")})
    assert "name a module, not module:attr" in result.out


# -- the sources


def test_a_package_directory_is_a_package_so_relative_imports_work(world: World) -> None:
    directory = user_dir(world) / "tree"
    write(directory / "__init__.py", "from .helper import NAME as name\n")
    write(directory / "helper.py", "NAME = 'tree'\n")
    assert "tree" in names(load_extensions(start(world)))


def test_two_files_with_one_stem_are_two_modules(world: World, tmp_path: Path) -> None:
    user_file(world, "same", MODULE.format(name="from_user", extra="MARK = 1"))
    elsewhere = write(tmp_path / "other" / "same.py", MODULE.format(name="from_e", extra=""))
    extensions = load_extensions(start(world), explicit=[str(elsewhere)])
    assert names(extensions)[-1] == "from_e"  # the same key: -e shadows the user's file


def test_names_that_start_with_dot_or_underscore_and_other_files_are_ignored(world: World) -> None:
    directory = user_dir(world)
    write(directory / "_private.py", "raise SystemExit('must not be imported')\n")
    write(directory / ".hidden.py", "raise SystemExit('must not be imported')\n")
    write(directory / "README.md", "# not code\n")
    (directory / "empty_dir").mkdir()
    write(directory / "__pycache__" / "x.py", "raise SystemExit('must not be imported')\n")
    assert names(load_extensions(start(world))) == ["step_limit"]


def test_a_file_and_a_built_in_come_from_their_places(world: World) -> None:
    user_file(world, "ralph")
    sources = {s.key: s for s in list_extensions(world.home, world.environ)}
    assert sources["tau"].state == "always"
    assert (sources["step_limit"].kind, sources["step_limit"].state) == ("built-in", "on")
    assert (sources["ralph"].kind, sources["ralph"].origin) == (
        "user",
        str(user_dir(world) / "ralph.py"),
    )


def test_an_installed_package_loads_through_its_registration(world: World) -> None:
    """The exit gate: a package with tau-search's registration shape (tests/fixtures)."""
    result = site_tau(world, "ext", "list", "--json")
    assert result.code == 0, result.err
    row = site_rows(result)["search"]
    assert (row["state"], row["kind"]) == ("on", "package")
    assert row["tools"] == ["web_fetch"]
    assert row["commands"] == ["search:find"]
    assert row["extensions"] == ["search"]
    assert str(row["origin"]).startswith(f"package {PACKAGE}")


def test_an_installed_package_comes_after_built_ins_and_before_drop_ins(world: World) -> None:
    user_file(world, "ralph")
    result = site_tau(world, "ext", "list", "--json")
    assert list(site_rows(result)) == ["tau", "session_name", "step_limit", "search", "ralph"]


# -- order and shadowing


def test_the_load_order_is_built_ins_packages_user_project_then_explicit(
    world: World, tmp_path: Path
) -> None:
    user_file(world, "b_user")
    user_file(world, "a_user")
    root = world.project()
    project_file(root, "p_proj.py", MODULE.format(name="p_proj", extra=""))
    trust(world, root)
    flag = write(tmp_path / "z_flag.py", MODULE.format(name="z_flag", extra=""))
    other = write(tmp_path / "y_flag.py", MODULE.format(name="y_flag", extra=""))
    extensions = load_extensions(start(world, root), explicit=[str(flag), str(other)])
    assert names(extensions) == [
        "step_limit",
        "a_user",
        "b_user",
        "p_proj",
        "z_flag",
        "y_flag",
    ]


def test_the_nearer_source_shadows_and_the_shadowed_one_is_never_imported(
    world: World,
) -> None:
    user_file(world, "plan_mode", "raise SystemExit('the user plan_mode must not be imported')\n")
    root = world.project()
    project_file(root, "plan_mode.py", MODULE.format(name="plan_mode", extra="WHICH = 'project'"))
    trust(world, root)
    assert names(load_extensions(start(world, root))).count("plan_mode") == 1
    by_origin = [r for r in list_extensions(root, world.environ) if r.key == "plan_mode"]
    assert [(r.kind, r.state) for r in by_origin] == [("user", "shadowed"), ("project", "on")]
    assert by_origin[0].detail == f"by project {root / '.tau' / 'extensions' / 'plan_mode.py'}"


def test_a_file_named_with_e_shadows_the_package_of_that_key_and_loads_once(
    world: World, tmp_path: Path
) -> None:
    mine = write(tmp_path / "search.py", MODULE.format(name="search", extra="MINE = True"))
    result = site_tau(world, "ext", "list", "--json", "-e", str(mine))
    listed = [r for r in map(json.loads, result.out.splitlines()) if r["key"] == "search"]
    assert [(r["kind"], r["state"]) for r in listed] == [
        ("package", "shadowed"),
        ("explicit", "on"),
    ]
    assert listed[0]["detail"] == f"by -e {mine}"
    assert listed[1]["tools"] == [], "it is my file, not the package"


def test_one_key_twice_in_one_row_is_an_error_naming_both(world: World) -> None:
    user_file(world, "ralph")
    write(user_dir(world) / "ralph" / "__init__.py", "name = 'ralph_dir'\n")
    with pytest.raises(ExtensionLoadError) as caught:
        load_extensions(start(world))
    message = str(caught.value)
    assert message.startswith('extension "ralph" failed to load; tau did not start.')
    assert f"source: {user_dir(world) / 'ralph.py'}" in message
    assert f"source: {user_dir(world) / 'ralph'}" in message
    assert "uninstall or remove one of them" in message


def test_two_extensions_with_one_name_from_different_keys_name_both_sources(
    world: World,
) -> None:
    user_file(world, "first", MODULE.format(name="twin", extra=""))
    user_file(world, "second", MODULE.format(name="twin", extra=""))
    with pytest.raises(ExtensionLoadError) as caught:
        load_extensions(start(world))
    assert (
        f"two extensions are named 'twin': {user_dir(world) / 'first.py'} "
        f"and {user_dir(world) / 'second.py'}"
    ) in str(caught.value)


def test_two_tools_with_one_name_name_both_sources(world: World) -> None:
    tool = "def shared(x: str) -> str:\n    '''Shared.'''\n    return x\n\ntools = (shared,)\n"
    user_file(world, "one", MODULE.format(name="one", extra=tool))
    user_file(world, "two", MODULE.format(name="two", extra=tool))
    with pytest.raises(ExtensionLoadError, match=r"two tools are named 'shared'.*one\.py.*two\.py"):
        load_extensions(start(world))


@pytest.mark.parametrize("name", ["has:colon", "has.dot", "has space"])
def test_an_extension_name_with_other_characters_is_refused(world: World, name: str) -> None:
    user_file(world, "badname", MODULE.format(name=name, extra=""))
    with pytest.raises(ExtensionLoadError, match="a name is letters, digits, `_` and `-`"):
        load_extensions(start(world))


def test_a_key_with_other_characters_is_a_failed_source(world: World) -> None:
    user_file(world, "my.ext")
    row = rows(world)["my.ext"]
    assert row["state"] == "failed"
    assert "is not letters, digits, `_` and `-`" in str(row["detail"])


def test_an_extension_named_for_a_built_in_is_a_name_clash_naming_both(world: World) -> None:
    path = user_file(world, "imposter", MODULE.format(name="tau", extra=""))
    with pytest.raises(
        ExtensionLoadError, match=f"two extensions are named 'tau': built-in tau and {path}"
    ):
        load_extensions(start(world))


def test_an_extension_named_session_name_is_a_name_clash(world: World) -> None:
    path = user_file(world, "imposter", MODULE.format(name="session_name", extra=""))
    with pytest.raises(
        ExtensionLoadError,
        match=f"two extensions are named 'session_name': built-in session_name and {path}",
    ):
        load_extensions(start(world))


def test_ext_list_prints_a_row_per_source_then_its_reason_or_contents(world: World) -> None:
    user_file(world, "ralph", WITH_COMMAND.format(name="ralph", command="go"))
    user_file(world, "off_one")
    world.user_config({"load": {"off_one": False}})
    root = world.project()
    project_file(root, "ralph.py", WITH_COMMAND.format(name="ralph", command="go"))
    trust(world, root)
    result = tau(world, "ext", "list", cwd=root)
    assert result.code == 0
    pad = " " * 14
    lines = result.out.splitlines()
    assert lines[:6] == [
        "tau           always     built-in",
        f"{pad}commands: /model; tools: none",
        "session_name  always     built-in (added by the startup)",
        f"{pad}commands: /name; tools: none",
        "step_limit    on         built-in",
        f"{pad}commands: none; tools: none",
    ]
    config = world.tau / "config.json"
    assert f"{pad}load.off_one = false in {config}" in lines
    shadowed = f"ralph         shadowed   {user_dir(world) / 'ralph.py'}"
    assert shadowed in lines
    assert f"{pad}by project {root / '.tau/extensions/ralph.py'}" in lines
    assert f"{pad}commands: /go; tools: none" in lines


def test_ext_alone_and_a_failed_json_listing_follow_the_usual_exit_codes(world: World) -> None:
    assert tau(world, "ext").code == 2
    user_file(world, "broken", "import tau_agent_core\n")
    result = tau(world, "ext", "list", "--json")
    assert result.code == 1
    assert all(json.loads(line)["key"] for line in result.out.splitlines())


def test_a_user_extensions_directory_writable_by_others_is_refused_with_the_chmod(
    world: World,
) -> None:
    user_file(world, "ralph")
    user_dir(world).chmod(0o777)
    try:
        with pytest.raises(ExtensionLoadError, match=r"extensions is writable .*run: chmod go-w"):
            load_extensions(start(world))
    finally:
        user_dir(world).chmod(0o755)


# -- load, -e and --no-extensions


def test_load_false_turns_a_source_off_and_the_row_says_which_file_did(world: World) -> None:
    user_file(world, "ralph", "raise SystemExit('off means not imported')\n")
    world.user_config({"load": {"ralph": False, "step_limit": False}})
    assert load_extensions(start(world)) == []
    found = rows(world)
    for key in ("ralph", "step_limit"):
        assert found[key]["state"] == "off"
        assert found[key]["detail"] == f"load.{key} = false in {world.tau / 'config.json'}"


def test_load_true_for_a_key_nothing_has_stops_the_start(world: World) -> None:
    world.user_config({"load": {"nope": True}})
    with pytest.raises(ExtensionLoadError) as caught:
        load_extensions(start(world))
    assert f"load.nope = true in {world.tau / 'config.json'}" in str(caught.value)
    assert "no extension source has the key 'nope'" in str(caught.value)
    assert rows(world)["nope"]["state"] == "failed"


def test_load_false_for_a_key_nothing_has_starts_and_check_warns(world: World) -> None:
    world.user_config({"load": {"nope": False}})
    assert names(load_extensions(start(world))) == ["step_limit"]
    result = tau(world, "config", "check")
    assert result.code == 0
    assert "warning: load.nope: no extension source has the key 'nope' (a typo?)" in result.out


def test_the_project_load_layer_overrides_the_users(world: World) -> None:
    user_file(world, "ralph")
    world.user_config({"load": {"ralph": False}})
    root = world.project(config={"load": {"ralph": True}})
    trust(world, root)
    assert "ralph" in names(load_extensions(start(world, root)))


def test_no_extensions_turns_off_packages_and_drop_ins_but_not_built_ins(world: World) -> None:
    user_file(world, "ralph")
    root = world.project()
    project_file(root, "plan_mode.py", MODULE.format(name="plan_mode", extra=""))
    trust(world, root)
    assert names(load_extensions(start(world, root), discover=False)) == ["step_limit"]
    found = site_rows(site_tau(world, "ext", "list", "--json", "--no-extensions", cwd=root))
    for key in ("search", "ralph", "plan_mode"):
        assert (found[key]["state"], found[key]["detail"]) == ("off", "--no-extensions")
    assert found["step_limit"]["state"] == "on"


def test_load_false_turns_an_installed_package_off(world: World) -> None:
    world.user_config({"load": {"search": False}})
    found = site_rows(site_tau(world, "ext", "list", "--json"))
    assert found["search"]["state"] == "off"
    assert found["search"]["detail"] == f"load.search = false in {world.tau / 'config.json'}"


def test_e_with_a_key_loads_that_source_even_when_load_or_no_extensions_turn_it_off(
    world: World,
) -> None:
    user_file(world, "ralph")
    world.user_config({"load": {"search": False}})
    args = ("ext", "list", "--json", "--no-extensions", "-e", "search")
    found = site_rows(site_tau(world, *args))
    assert found["search"]["state"] == "on", "tau --no-extensions -e search: the package alone"
    assert found["ralph"]["state"] == "off"


def test_e_with_a_path_needs_no_trust_and_loads_whatever_load_says(
    world: World, tmp_path: Path
) -> None:
    mine = write(tmp_path / "mine.py", MODULE.format(name="mine", extra=""))
    world.user_config({"load": {"mine": False}})
    assert names(load_extensions(start(world), explicit=[str(mine)], discover=False))[-1] == "mine"


def test_e_with_a_key_nothing_has_or_a_missing_or_compiled_file_is_a_start_error(
    world: World, tmp_path: Path
) -> None:
    for value, why in [
        ("nokey", "no extension source has this key"),
        (str(tmp_path / "gone.py"), "no such file or directory"),
        ("x.pyc", "a compiled file is not a source"),
    ]:
        with pytest.raises(ExtensionLoadError, match=why) as caught:
            load_extensions(start(world), explicit=[value])
        assert f"fix it or drop the flag: -e {value}" in str(caught.value)


def test_a_relative_e_path_is_taken_against_the_directory_the_user_typed_it_in(
    world: World,
) -> None:
    write(world.home / "work" / "mine.py", MODULE.format(name="mine", extra=""))
    result = tau(world, "ext", "list", "--json", "-e", "./mine.py", cwd=world.home / "work")
    assert json.loads(result.out.splitlines()[-1])["origin"] == f"-e {world.home / 'work/mine.py'}"


# -- tau ext enable / disable


def test_disable_and_enable_write_load_into_the_users_file_and_keep_the_rest(
    world: World,
) -> None:
    user_file(world, "ralph")
    world.user_config({"default_model": "m", **models(m={})})
    off = tau(world, "ext", "disable", "ralph")
    assert off.code == 0
    assert off.out == f"set load.ralph = false in {world.tau / 'config.json'}\n"
    saved = json.loads((world.tau / "config.json").read_text(encoding="utf-8"))
    assert saved["load"] == {"ralph": False}
    assert saved["default_model"] == "m", "the rest of the file stays"
    assert rows(world)["ralph"]["state"] == "off"
    assert tau(world, "ext", "enable", "ralph").code == 0
    assert rows(world)["ralph"]["state"] == "on"


def test_a_key_no_source_has_writes_nothing(world: World) -> None:
    result = tau(world, "ext", "disable", "nope")
    assert result.code == 1
    assert "no extension source has the key 'nope'; the keys are ['step_limit']" in (result.err)
    assert not (world.tau / "config.json").exists()


def test_a_project_that_sets_the_key_the_other_way_still_gets_the_write_and_a_note(
    world: World,
) -> None:
    user_file(world, "ralph")
    root = world.project(config={"load": {"ralph": True}})
    trust(world, root)
    result = tau(world, "ext", "disable", "ralph", cwd=root)
    assert result.code == 0
    assert f"the project's {root / '.tau/config.json'} sets load.ralph = true and wins here" in (
        result.out
    )
    saved = json.loads((world.tau / "config.json").read_text(encoding="utf-8"))
    assert saved == {"load": {"ralph": False}}
    project_config = json.loads((root / ".tau" / "config.json").read_text(encoding="utf-8"))
    assert project_config == {"load": {"ralph": True}}, "the project's file is never written"


def test_save_load_keeps_the_files_other_keys(world: World) -> None:
    world.user_config({"load": {"a": True}, "extensions": {"step_limit": {"limit": 3}}})
    catalog = UserConfig(world.tau).save_load("b", False)
    assert dict(catalog.load) == {"a": True, "b": False}
    saved = json.loads((world.tau / "config.json").read_text(encoding="utf-8"))
    assert saved["extensions"] == {"step_limit": {"limit": 3}}


# -- project drop-ins under trust


def test_a_project_drop_in_is_untrusted_until_approved_and_the_report_lists_the_files(
    world: World,
) -> None:
    root = world.project()
    project_file(root, "plan_mode.py", MODULE.format(name="plan_mode", extra=""))
    project_file(root, "tree/__init__.py", "name = 'tree'\n")
    assert rows(world, cwd=root)["plan_mode"]["state"] == "untrusted"
    assert rows(world, cwd=root)["tree"]["state"] == "untrusted"
    report = tau(world, "trust", cwd=root).out
    assert "Its extensions (code that runs with your authority):" in report
    assert "  plan_mode.py  " in report
    assert "  tree/__init__.py  " in report
    assert " bytes" in report
    assert "name = 'tree'" not in report, "the report prints no code"
    assert rows(world, cwd=root)["plan_mode"]["state"] == "on"


def test_a_changed_added_or_removed_file_untrusts_the_project_again(world: World) -> None:
    root = world.project()
    first = project_file(root, "plan_mode.py", MODULE.format(name="plan_mode", extra=""))
    trust(world, root)
    first.write_text(MODULE.format(name="plan_mode", extra="CHANGED = 1"), encoding="utf-8")
    again = tau(world, "trust", cwd=root)
    assert ".tau/extensions/plan_mode.py changed" in again.out
    project_file(root, "late.py", MODULE.format(name="late", extra=""))
    assert rows(world, cwd=root)["late"]["state"] == "untrusted"
    assert ".tau/extensions/late.py added" in tau(world, "trust", cwd=root).out
    first.unlink()
    assert ".tau/extensions/plan_mode.py removed" in tau(world, "trust", cwd=root).out


def test_a_pycache_in_the_extensions_directory_does_not_change_trust(world: World) -> None:
    root = world.project()
    project_file(root, "plan_mode.py", MODULE.format(name="plan_mode", extra=""))
    trust(world, root)
    write(root / ".tau" / "extensions" / "__pycache__" / "x.pyc", "junk")
    assert rows(world, cwd=root)["plan_mode"]["state"] == "on"


def test_a_dot_file_in_the_extensions_directory_does_not_change_trust(world: World) -> None:
    root = world.project()
    project_file(root, "plan_mode.py", MODULE.format(name="plan_mode", extra=""))
    trust(world, root)
    write(root / ".tau" / "extensions" / ".plan_mode.py.swp", "editor junk")
    assert rows(world, cwd=root)["plan_mode"]["state"] == "on"
    assert "swp" not in tau(world, "trust", cwd=root).out


def test_what_runs_is_what_was_approved_not_what_the_file_says_now(world: World) -> None:
    root = world.project()
    path = project_file(root, "plan_mode.py", MODULE.format(name="plan_mode", extra=""))
    trust(world, root)
    approved = start(world, root)
    path.write_text("raise SystemExit('swapped after approval')\n", encoding="utf-8")
    write(root / ".tau" / "extensions" / "added.py", "raise SystemExit('added after approval')\n")
    assert "plan_mode" in names(load_extensions(approved))
    assert "added" not in names(load_extensions(approved))


def test_a_project_package_imports_its_own_modules_from_the_approved_bytes(
    world: World,
) -> None:
    root = world.project()
    project_file(root, "tree/__init__.py", "from .helper import NAME as name\n")
    project_file(root, "tree/helper.py", "NAME = 'tree'\n")
    trust(world, root)
    approved = start(world, root)
    project_file(root, "tree/helper.py", "NAME = 'swapped'\n")
    project_file(root, "tree/extra.py", "NAME = 'added'\n")
    assert names(load_extensions(approved))[-1] == "tree"


def test_a_module_added_after_approval_cannot_be_imported_by_an_approved_one(
    world: World,
) -> None:
    root = world.project()
    project_file(
        root,
        "tree/__init__.py",
        "name = 'tree'\n"
        "def f():\n    import importlib\n    importlib.import_module(__name__ + '.late')\n",
    )
    trust(world, root)
    approved = start(world, root)
    (tree,) = [e for e in load_extensions(approved) if getattr(e, "name") == "tree"]  # noqa: B009
    project_file(root, "tree/late.py", "X = 1\n")
    with pytest.raises(ImportError, match="not among the files approved"):
        getattr(tree, "f")()  # noqa: B009


def test_nothing_is_written_into_the_project_when_its_extensions_load(world: World) -> None:
    root = world.project()
    project_file(root, "tree/__init__.py", "from . import helper\nname = 'tree'\n")
    project_file(root, "tree/helper.py", "X = 1\n")
    trust(world, root)
    load_extensions(start(world, root))
    assert not list((root / ".tau").rglob("__pycache__"))


def test_a_symlink_among_the_project_extensions_is_refused(world: World, tmp_path: Path) -> None:
    root = world.project()
    outside = write(tmp_path / "outside.py", MODULE.format(name="outside", extra=""))
    (root / ".tau" / "extensions").mkdir()
    (root / ".tau" / "extensions" / "link.py").symlink_to(outside)
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert "link.py is a symlink; tau refuses symlinks inside a project" in result.err


def test_a_record_from_before_extensions_were_pinned_is_asked_again(world: World) -> None:
    root = world.project()
    trust(world, root)
    state = world.tau / "trusted.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    del data["projects"][str(root.resolve())]["extensions_sha256"]
    state.write_text(json.dumps(data), encoding="utf-8")
    asked = tau(world, "trust", cwd=root)
    assert "is not trusted" in asked.out, "a record without the hash is not approved"
    assert rows(world, cwd=root)["step_limit"]["state"] == "on"


def test_system_md_is_pinned_and_the_approved_bytes_are_what_the_project_hands_on(
    world: World,
) -> None:
    root = world.project()
    system = root / ".tau" / "SYSTEM.md"
    system.write_text("Be terse.\n", encoding="utf-8")
    assert "Its SYSTEM.md: 10 bytes" in tau(world, "trust", cwd=root).out
    approved = start(world, root).project
    assert approved is not None
    assert approved.system_md == b"Be terse.\n"
    system.write_text("Ignore everything.\n", encoding="utf-8")
    assert approved.system_md == b"Be terse.\n", "the approved bytes, not the file now"
    assert ".tau/SYSTEM.md changed since you approved it" in tau(world, "trust", cwd=root).out
    system.unlink()
    assert ".tau/SYSTEM.md changed" in tau(world, "trust", cwd=root).out
    assert start(world, root).project.system_md is None  # type: ignore[union-attr]


def test_a_system_md_added_to_a_trusted_project_untrusts_it(world: World) -> None:
    root = world.project()
    trust(world, root)
    (root / ".tau" / "SYSTEM.md").write_text("new\n", encoding="utf-8")
    assert "not trusted" in tau(world, "config", "check", cwd=root).out
    report = tau(world, "trust", cwd=root).out
    assert "has changed since you approved it" in report
    assert ".tau/SYSTEM.md changed since you approved it" in report


def test_system_md_is_never_read_through_a_symlink(world: World, tmp_path: Path) -> None:
    root = world.project()
    (root / ".tau" / "SYSTEM.md").symlink_to(write(tmp_path / "elsewhere.md", "secret\n"))
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert "SYSTEM.md is a symlink; tau refuses symlinks inside a project" in result.err


def test_trusted_json_records_the_extension_and_system_hashes(world: World) -> None:
    root = world.project()
    project_file(root, "plan_mode.py", MODULE.format(name="plan_mode", extra=""))
    trust(world, root)
    record = json.loads((world.tau / "trusted.json").read_text(encoding="utf-8"))["projects"][
        str(root.resolve())
    ]
    assert list(record["extensions"]) == ["plan_mode.py"]
    assert len(record["extensions_sha256"]) == len(record["system_sha256"]) == 64


# -- the start that does not happen (section 6)


def test_a_user_drop_that_fails_names_its_lines_the_error_and_the_way_past_it(
    world: World,
) -> None:
    path = user_file(world, "broken", "import tau_agent_core\n")
    with pytest.raises(ExtensionLoadError) as caught:
        load_extensions(start(world))
    assert str(caught.value) == (
        'extension "broken" failed to load; tau did not start.\n'
        f"  source: {path}\n"
        f"  {path}:1: import tau_agent_core\n"
        "  ModuleNotFoundError: No module named 'tau_agent_core'\n"
        "  to start without it: tau ext disable broken   (or: tau --no-extensions)"
    )
    assert isinstance(caught.value.__cause__, ModuleNotFoundError)
    assert isinstance(caught.value, ImportError)
    assert [s.key for s in caught.value.sources] == ["broken"]


def test_the_traceback_shows_only_lines_in_the_sources_own_files(world: World) -> None:
    write(user_dir(world) / "deep" / "__init__.py", "from . import inner\nname = 'deep'\n")
    write(user_dir(world) / "deep" / "inner.py", "import json\njson.loads('{')\n")
    with pytest.raises(ExtensionLoadError) as caught:
        load_extensions(start(world))
    message = str(caught.value)
    assert "inner.py:2: json.loads('{')" in message
    assert "__init__.py:1: from . import inner" in message
    assert "decoder.py" not in message
    assert "importlib" not in message
    assert "JSONDecodeError" in message


def test_a_project_source_that_fails_says_disable_it_or_remove_the_file(world: World) -> None:
    root = world.project()
    project_file(root, "boom.py", "raise RuntimeError('no')\n")
    trust(world, root)
    with pytest.raises(ExtensionLoadError) as caught:
        load_extensions(start(world, root))
    assert caught.value.args[0].endswith(
        "  to start without it: tau ext disable boom, or remove the file"
        "   (or: tau --no-extensions)"
    )
    assert "RuntimeError: no" in str(caught.value)


def test_an_e_source_that_fails_says_fix_it_or_drop_the_flag(world: World, tmp_path: Path) -> None:
    mine = write(tmp_path / "mine.py", "raise RuntimeError('mine')\n")
    with pytest.raises(ExtensionLoadError) as caught:
        load_extensions(start(world), explicit=[str(mine)])
    assert str(caught.value).endswith(f"  fix it or drop the flag: -e {mine}")


def test_every_failure_is_reported_not_just_the_first(world: World) -> None:
    user_file(world, "one", "raise RuntimeError('first')\n")
    user_file(world, "two", "raise RuntimeError('second')\n")
    with pytest.raises(ExtensionLoadError) as caught:
        load_extensions(start(world))
    assert 'extension "one" failed' in str(caught.value)
    assert 'extension "two" failed' in str(caught.value)
    assert len(caught.value.sources) == 2


def test_a_source_that_fails_a_definition_check_is_pinned_to_it(world: World) -> None:
    misspelt = "async def pre_stp(cursor, subject):\n    return None\n"
    user_file(world, "typo", MODULE.format(name="typo", extra=misspelt))
    with pytest.raises(ExtensionLoadError, match="`pre_stp` is not a hook point"):
        load_extensions(start(world))
    row = rows(world)["typo"]
    assert row["state"] == "failed"
    assert "DefinitionError: extension 'typo': `pre_stp` is not a hook point" in str(row["detail"])


def test_a_user_drop_in_directory_writable_by_others_is_refused_with_the_chmod(
    world: World,
) -> None:
    path = user_file(world, "ralph")
    path.chmod(0o666)
    with pytest.raises(ExtensionLoadError, match=r"writable by group or others .*run: chmod go-w"):
        load_extensions(start(world))
    path.chmod(0o644)
    names(load_extensions(start(world)))


def test_a_failed_start_leaves_nothing_on_disk(world: World) -> None:
    user_file(world, "broken", "import tau_agent_core\n")
    before = sorted(p for p in world.home.rglob("*") if "__pycache__" not in p.parts)
    with pytest.raises(ExtensionLoadError):
        load_extensions(start(world))
    assert sorted(p for p in world.home.rglob("*") if "__pycache__" not in p.parts) == before


def test_ext_list_shows_the_failure_as_a_row_with_its_last_own_line_and_exits_1(
    world: World,
) -> None:
    user_file(world, "broken", "import tau_agent_core\n")
    result = tau(world, "ext", "list")
    assert result.code == 1
    assert "broken" in result.out
    assert "ModuleNotFoundError: No module named 'tau_agent_core' (broken.py:1)" in result.out


def test_config_check_loads_extensions_and_a_failed_one_is_an_error(world: World) -> None:
    user_file(world, "broken", "import tau_agent_core\n")
    result = tau(world, "config", "check")
    assert result.code == 1
    assert 'extension "broken" failed to load; tau did not start.' in result.out
    assert "to start without it: tau ext disable broken" in result.out


def test_config_set_and_check_know_the_options_of_a_loaded_extension(world: World) -> None:
    options = (
        "from dataclasses import dataclass\n\n@dataclass(frozen=True)\nclass Options:\n"
        "    depth: int = 2\n\n"
    )
    user_file(world, "deep", options + MODULE.format(name="deep", extra="options = Options"))
    assert tau(world, "config", "set", "deep.depth=5").code == 0
    saved = json.loads((world.tau / "config.json").read_text(encoding="utf-8"))
    assert saved["extensions"] == {"deep": {"depth": 5}}
    world.user_config({"extensions": {"deep": {"depth": "many"}}})
    checked = tau(world, "config", "check")
    assert checked.code == 1
    assert "extensions.deep" in checked.out


# -- the registrations of installed packages, in a process of their own


class Hermetic:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err


def run_tau(environ: dict[str, str], cwd: Path, sites: Sequence[Path], *argv: str) -> Hermetic:
    """`tau` with `argv` in a fresh process whose PYTHONPATH holds `sites`: directories with
    packages and the `.dist-info` that registers them, as an install leaves them."""
    env = {**environ, "PYTHONPATH": os.pathsep.join(str(s) for s in sites)}
    code = "import sys; from tau.cli import main; raise SystemExit(main(sys.argv[1:]))"
    done = subprocess.run(
        [sys.executable, "-c", code, *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        check=False,
    )
    return Hermetic(done.returncode, done.stdout, done.stderr)


def site_tau(
    world: World,
    *argv: str,
    cwd: Path | None = None,
    sites: Sequence[Path] = (SEARCH_SITE,),
) -> Hermetic:
    return run_tau(world.environ, cwd or world.home, sites, *argv)


def site_rows(result: Hermetic) -> dict[str, dict[str, object]]:
    return {r["key"]: r for r in map(json.loads, result.out.splitlines())}


def hermetic(tmp_path: Path, points: dict[str, tuple[str, str]], dist: str = "pkg") -> Hermetic:
    """`tau ext list --json` in a fresh process whose PYTHONPATH holds a distribution written
    here that registers `points` (key to (module, source of the module)) in `tau.extensions`."""
    site = tmp_path / "site"
    info = site / f"{dist}-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 1.0\n")
    lines = "[tau.extensions]\n"
    for key, (module, source) in points.items():
        lines += f"{key} = {module}\n"
        (site / f"{module.partition(':')[0]}.py").write_text(source, encoding="utf-8")
    (info / POINTS_FILE).write_text(lines)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return run_tau({"HOME": str(home)}, home, [site], "ext", "list", "--json")


def test_a_package_that_fails_to_import_names_itself_its_version_and_the_registration(
    tmp_path: Path,
) -> None:
    result = hermetic(
        tmp_path,
        {"zeta": ("zeta_ext", "import tau_agent_core\n")},
        dist="ffwf-tau-zeta",
    )
    assert result.code == 1
    row = site_rows(result)["zeta"]
    assert row["state"] == "failed"
    assert row["origin"] == "package ffwf-tau-zeta 1.0 (zeta_ext)"
    assert row["detail"] == "ModuleNotFoundError: No module named 'tau_agent_core' (zeta_ext.py:1)"


def test_two_packages_registering_one_key_are_an_error_naming_both(world: World) -> None:
    result = site_tau(world, "ext", "list", "--json", sites=[SEARCH_SITE, OTHER_SITE])
    dup = [r for r in map(json.loads, result.out.splitlines()) if r["key"] == "search"]
    assert result.code == 1
    assert [r["state"] for r in dup] == ["failed", "failed"]
    assert {r["origin"] for r in dup} == {
        f"package {PACKAGE} (tau_fixture_search.ext.search)",
        "package ffwf-tau-other 2.0 (tau_fixture_other.ext)",
    }
