"""Hook order (D28 section 5): `tau ext list --hooks`, and what `tau config check` and a start say
about an order the config gets wrong."""

import json
import re

import pytest
from world import World, tau

MOCK_CONFIG = {
    "default_model": "m",
    "models": {"m": {"model": "m", "provider": "mock", "options": {"delay_ms": 0}}},
}

GUARD = """
name = "guard"
order = {"pre_input": -100}

async def pre_input(cursor, subject):
    return None
"""

PLAIN = """
name = "plain"

async def pre_input(cursor, subject):
    return None
"""

# Shadows the built-in `usage` (D19 section 2) and declares what the built-in declares.
USAGE = """
name = "usage"
order = {"pre_input": -100}

async def pre_input(cursor, subject):
    return None
"""

HOOKLESS = """
name = "hookless"
"""


def drop_in(world: World, name: str, text: str) -> None:
    directory = world.tau / "extensions"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(text, encoding="utf-8")


def pre_input(world: World, *flags: str) -> str:
    """The `pre_input` line of `tau ext list --hooks`."""
    result = tau(world, "ext", "list", "--hooks", *flags)
    assert (result.code, result.err) == (0, "")
    (line,) = [line for line in result.out.splitlines() if line.startswith("pre_input")]
    return line


def test_hooks_lists_each_point_with_its_extensions_in_run_order(world: World) -> None:
    drop_in(world, "plain", PLAIN)
    drop_in(world, "guard", GUARD)
    result = tau(world, "ext", "list", "--hooks")
    assert (result.code, result.err) == (0, "")
    lines = {line.split()[0]: line for line in result.out.splitlines()}
    assert "guard -100" in lines["pre_input"]
    assert lines["pre_input"].index("guard") < lines["pre_input"].index("plain 0")
    assert lines["pre_sample"].endswith("usage -100, agents 0, compaction 0, plan_mode 0")
    widths = {re.match(r"\S+\s+", line).end() for line in lines.values()}  # type: ignore[union-attr]
    assert len(widths) == 1, "the points are aligned"


def test_hooks_marks_what_the_config_set(world: World) -> None:
    drop_in(world, "plain", PLAIN)
    world.user_config({"extensions": {"plain": {"order": -200}}})
    assert pre_input(world).removeprefix("pre_input").strip().startswith("plain -200 (config), ")
    assert tau(world, "ext", "list", "--hooks", "--no-extensions").code == 0


def test_hooks_json_is_one_object_per_point(world: World) -> None:
    drop_in(world, "plain", PLAIN)
    world.user_config({"extensions": {"plain": {"order": {"pre_input": 7}}}})
    result = tau(world, "ext", "list", "--hooks", "--json")
    assert (result.code, result.err) == (0, "")
    points = {o["point"]: o["hooks"] for o in map(json.loads, result.out.splitlines())}
    assert points["pre_input"][-1] == {"extension": "plain", "order": 7, "configured": True}
    assert points["pre_input"][0]["configured"] is False
    assert "pre_tool" in points


def test_a_dropin_that_shadows_a_builtin_keeps_its_place_by_declaring_its_number(
    world: World,
) -> None:
    drop_in(world, "other", PLAIN.replace('"plain"', '"other"'))
    drop_in(world, "usage", USAGE.replace('order = {"pre_input": -100}\n', ""))
    plain = pre_input(world)
    assert plain.index("other 0") < plain.index("usage 0"), "a drop-in loses its place unordered"
    drop_in(world, "usage", USAGE)
    declared = pre_input(world)
    assert declared.index("usage -100") < declared.index("other 0")
    rows = tau(world, "ext", "list").out.splitlines()
    assert [r.split()[:2] for r in rows if r.startswith("usage")] == [
        ["usage", "shadowed"],
        ["usage", "on"],
    ]


@pytest.mark.parametrize(
    ("order", "message"),
    [
        ("first", "extensions.guard.order: must be an integer or an object"),
        ({"pre_input": "x"}, "extensions.guard.order: the order of 'pre_input' must be an integer"),
        (
            {"pre_tool": 1},
            "extension 'guard' has no hook 'pre_tool' (a hook point); its hooks: pre_input",
        ),
        ({"nope": 1}, "has no hook 'nope'"),
    ],
)
def test_a_bad_order_fails_check_list_and_the_start(
    world: World, order: object, message: str
) -> None:
    drop_in(world, "guard", GUARD)
    world.user_config({**MOCK_CONFIG, "extensions": {"guard": {"order": order}}})
    check = tau(world, "config", "check")
    assert check.code == 1
    assert message in check.out + check.err
    listed = tau(world, "ext", "list", "--hooks")
    assert listed.code == 1
    assert message in listed.out + listed.err
    started = tau(world, "-p", "hello")
    assert started.code != 0
    assert message in started.err
    assert "mock: hello" not in started.out


def test_an_int_order_for_an_extension_with_no_hooks_fails_the_check(world: World) -> None:
    drop_in(world, "hookless", HOOKLESS)
    world.user_config({"extensions": {"hookless": {"order": 1}}})
    check = tau(world, "config", "check")
    assert check.code == 1
    assert "extension 'hookless' has no hooks to order" in check.out + check.err


def test_a_sound_check_is_silent(world: World) -> None:
    """`tau config check` prints nothing when nothing is wrong; the table is `tau ext list
    --hooks`'s."""
    drop_in(world, "guard", GUARD)
    check = tau(world, "config", "check")
    assert (check.code, check.out, check.err) == (0, "", "")


def test_an_order_for_an_extension_that_is_not_loaded_is_a_warning(world: World) -> None:
    world.user_config({"extensions": {"ghost": {"order": 1}}})
    check = tau(world, "config", "check")
    assert check.code == 0
    assert "ghost" in check.out + check.err, "an order for what is not loaded is a warning"


def test_set_refuses_an_order_that_is_not_an_integer(world: World) -> None:
    drop_in(world, "guard", GUARD)
    world.user_config(MOCK_CONFIG)
    result = tau(world, "-p", "--set", "guard.order=x", "hello")
    assert result.code != 0
    assert "order" in result.err
    assert "mock: hello" not in result.out
    ok = tau(world, "-p", "--set", "guard.order.pre_input=3", "hello")
    assert (ok.code, ok.out) == (0, "mock: hello\n")


@pytest.mark.parametrize("name", ["tau", "session_name"])
def test_an_order_for_an_extension_every_start_has_is_an_error_in_check_and_start(
    world: World, name: str
) -> None:
    """`tau` and `session_name` have no hooks: an order for them fails the check, as the start."""
    world.user_config({**MOCK_CONFIG, "extensions": {name: {"order": 1}}})
    check = tau(world, "config", "check")
    assert check.code == 1
    assert f"extension {name!r} has no hooks to order" in check.out + check.err
    start = tau(world, "-p", "hi")
    assert start.code != 0
    assert "has no hooks to order" in start.out + start.err
