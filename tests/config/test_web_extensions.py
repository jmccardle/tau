"""`fetch` and `search`, the built-in web extensions (D24 section 9), as `tau ext` and a start see
them: `fetch` is on, `search` is off until it is turned on and has its key. Through `tau` on real
config files."""

import json

from world import World, tau

KEY = "sk-test-0123456789abcdef"
MOCK = {
    "default_model": "mock:echo",
    "models": {"mock": {"provider": "mock", "models": {"echo": {"model": "echo"}}}},
}


def row(world: World, key: str, *argv: str) -> dict[str, object]:
    result = tau(world, "ext", "list", "--json", *argv)  # exit 1 when a row failed
    return {r["key"]: r for r in map(json.loads, result.out.splitlines())}[key]


def test_fetch_is_on_and_offers_its_tool(world: World) -> None:
    found = row(world, "fetch")
    assert (found["kind"], found["state"]) == ("built-in", "on")
    assert found["tools"] == ["fetch"]
    assert found["extensions"] == ["fetch"]


def test_search_is_off_by_default_and_the_row_says_how_to_turn_it_on(world: World) -> None:
    found = row(world, "search")
    assert (found["kind"], found["state"]) == ("built-in", "off")
    assert found["detail"] == "off (default); to turn it on: tau ext enable search"
    assert found["tools"] == []


def test_enabling_search_without_its_key_warns_and_starts_without_the_tool(world: World) -> None:
    """A missing key is a secret not yet set, not a mistake in a file: τ starts, says which
    variable to set, and the model is not offered a tool that could only fail."""
    world.user_config(MOCK)
    enabled = tau(world, "ext", "enable", "search")
    assert enabled.code == 0
    assert enabled.out.splitlines()[-1] == (
        "search: the brave provider needs an API key in BRAVE_API_KEY, which is not set; τ starts "
        "without the search tool. Set it in the environment, or in a trusted project's .env file"
    )
    found = row(world, "search")
    assert (found["state"], found["tools"]) == ("on", [])
    assert "BRAVE_API_KEY, which is not set" in str(found["detail"])
    assert tau(world, "config", "check").code == 0
    started = tau(world, "-p", "--verbose", "hi")  # `tau -p` shows stderr with --verbose
    assert started.code == 0, started.err
    assert "tau: search: the brave provider needs an API key in BRAVE_API_KEY" in started.err


def test_a_wrong_provider_or_address_still_stops_the_start(world: World) -> None:
    """What is wrong in a file stays an error: only the missing secret is a warning."""
    world.user_config(
        {**MOCK, "load": {"search": True}, "extensions": {"search": {"provider": "bing"}}}
    )
    result = tau(world, "-p", "hi")
    assert result.code != 0
    assert "extensions.search.provider is 'bing'" in result.both


def test_enabling_search_with_its_key_offers_the_tool_and_shows_no_key(world: World) -> None:
    assert tau(world, "ext", "enable", "search").code == 0
    world.environ["BRAVE_API_KEY"] = KEY
    found = row(world, "search")
    assert (found["state"], found["tools"]) == ("on", ["search"])
    assert KEY not in tau(world, "ext", "list").both
    assert KEY not in tau(world, "config", "check").both


def test_a_bad_constant_stops_the_start_and_the_check(world: World) -> None:
    world.user_config({"extensions": {"fetch": {"allow_private": "yes"}}})
    result = tau(world, "config", "check")
    assert result.code != 0
    assert "extensions.fetch.allow_private must be true or false, not 'yes'" in result.both


def test_the_constants_are_not_options_a_client_could_set(world: World) -> None:
    """No `options`: the config form has none to edit and `/config` refuses the extension."""
    world.user_config({"extensions": {"fetch": {"allow_private": True}}})
    assert tau(world, "config", "check").code == 0
    result = tau(world, "-p", "-m", "mock:echo", "--set", "fetch.allow_private=false", "hi")
    assert result.code != 0
    assert "fetch" in result.both
