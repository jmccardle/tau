"""The counter (D26 section 4): at most one loaded extension offers one, and two sources that
do are a start that does not happen, naming both. The built-in `tokens` offers one and is on by
default, so another counter loads only with `tokens` turned off."""

from pathlib import Path

import pytest
from world import World, loaded

from tau import ExtensionLoadError, load_extensions

COUNTER = """
name = "{name}"

class Counter:
    def count(self, job):
        raise AssertionError("never asked")

    def observe(self, observation):
        raise AssertionError("never shown")

counter = Counter()
"""


def extension(world: World, key: str) -> Path:
    path = world.tau / "extensions" / f"{key}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(COUNTER.format(name=key), encoding="utf-8")
    return path


def names(world: World) -> list[str]:
    return [str(getattr(e, "name")) for e in load_extensions(loaded(world))]  # noqa: B009


def test_the_built_in_counter_loads_by_default_and_not_when_turned_off(world: World) -> None:
    assert "tokens" in names(world)
    world.user_config({"load": {"tokens": False}})
    assert "tokens" not in names(world)


def test_one_counter_loads_once_tokens_is_off(world: World) -> None:
    extension(world, "ones")
    world.user_config({"load": {"tokens": False}})
    assert "ones" in names(world)


def test_a_counter_beside_the_built_in_one_names_both_and_how_to_turn_one_off(
    world: World,
) -> None:
    mine = extension(world, "ones")
    with pytest.raises(ExtensionLoadError) as caught:
        load_extensions(loaded(world))
    said = str(caught.value)
    assert f"two sources offer a counter: built-in tokens and {mine}" in said
    assert "tau ext disable tokens" in said


def test_two_sources_offering_a_counter_name_both(world: World) -> None:
    first, second = extension(world, "first"), extension(world, "second")
    world.user_config({"load": {"tokens": False}})
    with pytest.raises(ExtensionLoadError) as caught:
        load_extensions(loaded(world))
    assert f"two sources offer a counter: {first} and {second}" in str(caught.value)


def test_a_counter_that_cannot_observe_is_refused(world: World) -> None:
    path = extension(world, "half")
    path.write_text(path.read_text().replace("def observe", "def watch"), encoding="utf-8")
    world.user_config({"load": {"tokens": False}})
    with pytest.raises(ExtensionLoadError, match=r"callable `observe\(observation\)` method"):
        load_extensions(loaded(world))
