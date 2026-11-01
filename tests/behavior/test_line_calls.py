"""`parse_line`, `complete_line` and `nodes` (D21 section 7): the command line and a range read,
asked of the driver's client, so a remote frontend needs no copy of the forms."""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import pytest
from harness import Harness, scenario

from tau import Cursor, NoSuchNode
from tau.commandline import UnknownCommand, completions, parse


def configure(cursor: Cursor, mode: Literal["fast", "slow"], size: int) -> None:
    """Configure."""


def note(text: str) -> None:
    """Note."""


@dataclass(frozen=True)
class Options:
    size: Annotated[int, "how big"] = 10


class Tuner:
    name = "tuner"
    options = Options
    commands = (configure, note)


async def tuned(tmp_path: Path) -> Harness:
    return await Harness.create(tmp_path, extensions=[Tuner()])


# parse_line


@scenario
async def test_parse_line_reads_a_line_as_parse_does_with_the_drivers_forms(
    tmp_path: Path,
) -> None:
    h = await tuned(tmp_path)
    parsed = await h.client.parse_line(h.cursor, "/configure slow 4")
    assert parsed == parse("/configure slow 4", await h.client.commands())
    assert (parsed.command.qualified, parsed.args, parsed.missing) == (
        "tuner:configure",
        {"mode": "slow", "size": 4},
        (),
    )
    assert (await h.client.parse_line(h.cursor, "/configure")).missing == ("mode", "size")
    await h.close()


@scenario
async def test_parse_line_reads_config_with_the_options_forms_the_cursor_sees(
    tmp_path: Path,
) -> None:
    h = await tuned(tmp_path)
    parsed = await h.client.parse_line(h.cursor, "/config tuner size=7 --save")
    assert (parsed.extension, parsed.args, parsed.save) == ("tuner", {"size": 7}, True)
    assert parsed.command == await h.client.options(h.cursor, "tuner")
    bare = await h.client.parse_line(h.cursor, "/config tuner")
    assert (bare.extension, bare.args) == ("tuner", {})
    await h.close()


@scenario
async def test_parse_line_refuses_as_parse_does(tmp_path: Path) -> None:
    h = await tuned(tmp_path)
    with pytest.raises(UnknownCommand, match="nosuch") as unknown:
        await h.client.parse_line(h.cursor, "/nosuch x")
    assert unknown.value.name == "nosuch"
    with pytest.raises(ValueError, match="starts with /name"):
        await h.client.parse_line(h.cursor, "not a command")
    with pytest.raises(ValueError, match="has no options"):
        await h.client.parse_line(h.cursor, "/config nosuch size=1")
    with pytest.raises(ValueError, match="cannot read the line"):
        await h.client.parse_line(h.cursor, "/configure 'slow 4")
    await h.close()


# complete_line


@scenario
async def test_complete_line_is_completions_on_the_driver(tmp_path: Path) -> None:
    h = await tuned(tmp_path)
    forms = await h.client.commands()
    options = {"tuner": await h.client.options(h.cursor, "tuner")}
    for line in ["/", "/conf", "/configure ", "/configure s", "/config ", "/config tuner ", "no"]:
        found = await h.client.complete_line(h.cursor, line)
        assert found == await completions(line, forms, h.client, h.cursor, options), line
    assert await h.client.complete_line(h.cursor, "/configure s") == ["slow"]
    assert "/config" in await h.client.complete_line(h.cursor, "/conf")
    await h.close()


# nodes


@scenario
async def test_nodes_reads_a_range_in_id_order(tmp_path: Path) -> None:
    h = await tuned(tmp_path)
    for text in ("one", "two", "three"):
        await h.input(text)
    everything = h.nodes()
    assert await h.client.nodes(0, len(everything)) == everything
    assert await h.client.nodes(1, 2) == everything[1:3]
    assert await h.client.nodes(len(everything) - 1, 10) == everything[-1:]
    await h.close()


@scenario
async def test_nodes_past_the_end_or_with_no_limit_is_empty(tmp_path: Path) -> None:
    h = await tuned(tmp_path)
    end = len(h.nodes())
    assert await h.client.nodes(end, 5) == []
    assert await h.client.nodes(end + 100, 5) == []
    assert await h.client.nodes(0, 0) == []
    await h.close()


@scenario
async def test_nodes_refuses_a_negative_start_or_limit(tmp_path: Path) -> None:
    h = await tuned(tmp_path)
    with pytest.raises(ValueError, match="start"):
        await h.client.nodes(-1, 5)
    with pytest.raises(ValueError, match="limit"):
        await h.client.nodes(0, -1)
    with pytest.raises(NoSuchNode):
        await h.client.node(10_000)
    await h.close()
