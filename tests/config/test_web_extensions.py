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
        "warning: search: the brave provider needs an API key in BRAVE_API_KEY, which is not set; "
        "τ starts without the search tool. Set it in the environment, or in a trusted project's "
        ".env file"
    )
    found = row(world, "search")
    assert (found["state"], found["tools"]) == ("on", [])
    assert "BRAVE_API_KEY, which is not set" in str(found["detail"])
    checked = tau(world, "config", "check")
    assert checked.code == 0
    assert "warning: search: the brave provider needs an API key in BRAVE_API_KEY" in checked.out
    started = tau(world, "-p", "hi")
    assert started.code == 0, started.err
    assert started.err.startswith(
        "tau: warning: search: the brave provider needs an API key in BRAVE_API_KEY"
    )


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


def test_administrator_keys_are_honoured_without_a_warning(world: World) -> None:
    """`fetch` has no options but declares administrator keys: setting them is no mistake."""
    world.user_config({**MOCK, "extensions": {"fetch": {"allow_private": True, "timeout": 6}}})
    checked = tau(world, "config", "check")
    assert (checked.code, checked.both) == (0, "")
    started = tau(world, "-p", "--verbose", "hi")
    assert started.code == 0, started.err
    assert "has no options" not in started.err


def test_config_check_names_an_unknown_administrator_key(world: World) -> None:
    world.user_config({"extensions": {"fetch": {"timeuot": 6}}})
    result = tau(world, "config", "check")
    assert result.code != 0
    assert "extensions.fetch.timeuot: 'fetch' has no such setting" in result.both


def test_the_command_line_writes_an_administrator_key_and_no_client_sets_one(
    world: World,
) -> None:
    """`tau config set` is an administrator at the command line writing a config file; `--set`
    is one run, after fetch was built; `/config` is a client's, and refused."""
    world.user_config(MOCK)
    written = tau(world, "config", "set", "fetch.timeout=5")
    assert written.code == 0, written.err
    assert json.loads((world.tau / "config.json").read_text())["extensions"]["fetch"] == {
        "timeout": 5
    }
    wrong = tau(world, "config", "set", "fetch.timeout=soon")
    assert wrong.code != 0
    assert "fetch.timeout" in wrong.both
    unknown = tau(world, "config", "set", "fetch.timeuot=5")
    assert "no administrator key 'timeuot'; its keys: ['allow_private', 'timeout']" in unknown.both
    one_run = tau(world, "-p", "--set", "fetch.allow_private=true", "hi")
    assert one_run.code != 0
    assert "fetch.allow_private is an administrator key" in one_run.both
    assert "tau config set fetch.allow_private=VALUE" in one_run.both
    client = tau(world, "-p", "/config fetch allow_private=true")
    assert client.code != 0
    assert "administrator keys are not options, and no client sets them" in client.both


def test_a_fetch_rule_with_a_scheme_or_port_is_a_config_error(world: World) -> None:
    """Such a rule could never match: a deny that cannot match must not pass quietly."""
    rules = {"deny": ["fetch:http://127.0.0.1:8300"]}
    world.user_config({"extensions": {"permissions": rules}})
    result = tau(world, "config", "check")
    assert result.code != 0
    assert "rule 'fetch:http://127.0.0.1:8300': a fetch pattern is a host only" in result.both
