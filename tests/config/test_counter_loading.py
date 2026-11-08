"""The counter (D26 section 4): at most one loaded extension offers one, and two sources that
do are a start that does not happen, naming both."""

from pathlib import Path

import pytest
from world import World, loaded

from tau import ExtensionLoadError, load_extensions

COUNTER = """
name = "{name}"

class Counter:
    def count(self, job):
        raise AssertionError("never asked")

counter = Counter()
"""


def extension(world: World, key: str) -> Path:
    path = world.tau / "extensions" / f"{key}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(COUNTER.format(name=key), encoding="utf-8")
    return path


def test_one_counter_loads(world: World) -> None:
    extension(world, "ones")
    assert "ones" in [str(getattr(e, "name")) for e in load_extensions(loaded(world))]  # noqa: B009


def test_two_sources_offering_a_counter_name_both(world: World) -> None:
    first, second = extension(world, "first"), extension(world, "second")
    with pytest.raises(ExtensionLoadError) as caught:
        load_extensions(loaded(world))
    assert f"two sources offer a counter: {first} and {second}" in str(caught.value)
