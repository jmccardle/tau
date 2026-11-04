"""`agents`, the built-in extension that is off by default (D24 section 8, M10): `load` turns it
on, `tau ext enable` writes that, and `-e agents` names it. Through `tau ext` and
`tau.load_extensions` on real files."""

import json

from world import World, loaded, tau

from tau import load_extensions


def row(world: World, *argv: str) -> dict[str, object]:
    result = tau(world, "ext", "list", "--json", *argv)
    assert result.code == 0, result.err
    found = {r["key"]: r for r in map(json.loads, result.out.splitlines())}
    return found["agents"]


def test_it_is_off_by_default_and_the_row_says_how_to_turn_it_on(world: World) -> None:
    found = row(world)
    assert (found["kind"], found["state"]) == ("built-in", "off")
    assert found["detail"] == "off (default); to turn it on: tau ext enable agents"
    assert found["tools"] == []
    assert "agents" not in [e.name for e in load_extensions(loaded(world))]


def test_the_plain_listing_says_off_default(world: World) -> None:
    result = tau(world, "ext", "list")
    assert result.code == 0, result.err
    assert "agents        off        built-in" in result.out
    assert "off (default); to turn it on: tau ext enable agents" in result.out


def test_load_true_turns_it_on_and_it_offers_the_agent_tool(world: World) -> None:
    world.user_config({"load": {"agents": True}})
    found = row(world)
    assert found["state"] == "on"
    assert found["tools"] == ["agent"]
    assert found["extensions"] == ["agents"]
    assert "agents" in [e.name for e in load_extensions(loaded(world))]


def test_ext_enable_writes_load_and_disable_writes_it_back(world: World) -> None:
    assert tau(world, "ext", "enable", "agents").code == 0
    assert json.loads((world.tau / "config.json").read_text()) == {"load": {"agents": True}}
    assert row(world)["state"] == "on"
    assert tau(world, "ext", "disable", "agents").code == 0
    off = row(world)
    assert off["state"] == "off"
    assert str(off["detail"]).startswith("load.agents = false in ")


def test_naming_it_with_dash_e_loads_it_and_no_extensions_does_not(world: World) -> None:
    assert row(world, "-e", "agents")["state"] == "on"
    assert row(world, "--no-extensions")["state"] == "off", "--no-extensions turns on nothing"


def test_a_bad_option_stops_the_start_and_the_check(world: World) -> None:
    world.user_config({"load": {"agents": True}, "extensions": {"agents": {"tools": ["agent"]}}})
    result = tau(world, "config", "check")
    assert result.code != 0
    assert "agents do not nest" in result.both
