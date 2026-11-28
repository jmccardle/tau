"""`agents` is a built-in that is on by default (D24 section 8, 0.18), and `bash_guard` and
`router` are built-ins that are off by default (M10): `load` turns an off one on, `tau ext
enable` writes that, and `-e` names it. Through `tau ext` and `tau.load_extensions` on real
files."""

import json

import pytest
from world import World, loaded, tau

from tau import load_extensions

OFF = ("bash_guard", "router")


def row(world: World, key: str, *argv: str) -> dict[str, object]:
    result = tau(world, "ext", "list", "--json", *argv)
    assert result.code == 0, result.err
    found = {r["key"]: r for r in map(json.loads, result.out.splitlines())}
    return found[key]


def test_agents_is_on_by_default_and_offers_the_agent_tool(world: World) -> None:
    found = row(world, "agents")
    assert (found["kind"], found["state"]) == ("built-in", "on")
    assert found["tools"] == ["agent"]
    assert "agents" in [e.name for e in load_extensions(loaded(world))]


def test_ext_disable_writes_load_false_and_enable_writes_it_back(world: World) -> None:
    assert tau(world, "ext", "disable", "agents").code == 0
    assert json.loads((world.tau / "config.json").read_text()) == {"load": {"agents": False}}
    off = row(world, "agents")
    assert off["state"] == "off"
    assert str(off["detail"]).startswith("load.agents = false in ")
    assert off["tools"] == []
    assert "agents" not in [e.name for e in load_extensions(loaded(world))]
    assert tau(world, "ext", "enable", "agents").code == 0
    assert row(world, "agents")["state"] == "on"


@pytest.mark.parametrize("key", OFF)
def test_an_off_one_says_how_to_turn_it_on(world: World, key: str) -> None:
    found = row(world, key)
    assert (found["kind"], found["state"]) == ("built-in", "off")
    assert found["detail"] == f"off (default); to turn it on: tau ext enable {key}"
    assert found["tools"] == []
    assert key not in [e.name for e in load_extensions(loaded(world))]


def test_the_plain_listing_says_off_default(world: World) -> None:
    result = tau(world, "ext", "list")
    assert result.code == 0, result.err
    assert "bash_guard    off        built-in" in result.out
    assert "off (default); to turn it on: tau ext enable bash_guard" in result.out


@pytest.mark.parametrize("key", OFF)
def test_load_true_turns_it_on(world: World, key: str) -> None:
    world.user_config({"load": {key: True}})
    found = row(world, key)
    assert found["state"] == "on"
    assert found["extensions"] == [key]
    assert key in [e.name for e in load_extensions(loaded(world))]


@pytest.mark.parametrize("key", OFF)
def test_ext_enable_writes_load_and_disable_writes_it_back(world: World, key: str) -> None:
    assert tau(world, "ext", "enable", key).code == 0
    assert json.loads((world.tau / "config.json").read_text()) == {"load": {key: True}}
    assert row(world, key)["state"] == "on"
    assert tau(world, "ext", "disable", key).code == 0
    off = row(world, key)
    assert off["state"] == "off"
    assert str(off["detail"]).startswith(f"load.{key} = false in ")


@pytest.mark.parametrize("key", OFF)
def test_naming_it_with_dash_e_loads_it_and_no_extensions_does_not(world: World, key: str) -> None:
    assert row(world, key, "-e", key)["state"] == "on"
    assert row(world, key, "--no-extensions")["state"] == "off", "--no-extensions turns on nothing"


def test_plan_mode_is_not_a_built_in(world: World) -> None:
    """Held back from 0.18.0: it is examples/speculative/plan_mode.py, and `/plan` is not in a
    default install."""
    result = tau(world, "ext", "list", "--json")
    assert result.code == 0, result.err
    assert "plan_mode" not in [json.loads(line)["key"] for line in result.out.splitlines()]


def test_a_bad_option_stops_the_start_and_the_check(world: World) -> None:
    world.user_config({"extensions": {"agents": {"tools": ["agent"]}}})
    result = tau(world, "config", "check")
    assert result.code != 0
    assert "agents do not nest" in result.both
