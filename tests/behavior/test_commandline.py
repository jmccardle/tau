"""The command line (D16 section 5): `parse` and `completions`, and the client's `complete`."""

from pathlib import Path
from typing import Literal

import pytest
from fakes import FakeProvider
from harness import Harness, say, scenario

from tau_next import CommandInfo, Cursor, CursorId, LabelName, Model, NodeId, TreeHandle
from tau_next.commandline import UnknownCommand, completions, parse


def configure(cursor: Cursor, mode: Literal["fast", "slow"], size: int, ratio: float = 1.0) -> None:
    """Configure."""


def tag(tree: TreeHandle, label: LabelName, to: CursorId, nodes: list[NodeId]) -> None:
    """Tag."""


def load(paths: list[Path], model: Model, force: bool = False, note: str | None = None) -> None:
    """Load."""


class Lines:
    name = "lines"
    commands = (
        configure,
        tag,
        load,
    )


async def commands(h: Harness) -> tuple[CommandInfo, ...]:
    return await h.client.commands()


# parse


@scenario
async def test_positional_words_fill_the_parameters_in_order(tmp_path: Path) -> None:
    infos = await commands(await Harness.create(tmp_path, extensions=[Lines()]))
    parsed = parse("/configure slow 4 0.5", infos)
    assert parsed.command.name == "configure"
    assert parsed.args == {"mode": "slow", "size": 4, "ratio": 0.5}
    assert parsed.missing == ()


@scenario
async def test_named_words_set_any_parameter_and_positional_ones_take_the_rest(
    tmp_path: Path,
) -> None:
    infos = await commands(await Harness.create(tmp_path, extensions=[Lines()]))
    parsed = parse("/configure size=4 slow", infos)
    assert parsed.args == {"size": 4, "mode": "slow"}


@scenario
async def test_the_missing_required_parameters_are_reported_in_order(tmp_path: Path) -> None:
    infos = await commands(await Harness.create(tmp_path, extensions=[Lines()]))
    assert parse("/configure", infos).missing == ("mode", "size")
    assert parse("/configure ratio=2", infos).missing == ("mode", "size")
    assert parse("/configure slow", infos).missing == ("size",)
    assert parse("/configure slow 3", infos).missing == ()


@scenario
async def test_words_are_shell_words_and_values_are_converted_by_the_schema(
    tmp_path: Path,
) -> None:
    infos = await commands(await Harness.create(tmp_path, extensions=[Lines()]))
    parsed = parse("/load 'my dir/a.txt',b.txt,c force=yes note=\"two words\"", infos)
    assert parsed.args == {
        "paths": ["my dir/a.txt", "b.txt", "c"],
        "force": True,
        "note": "two words",
    }
    assert parse("/tag fixes new 1,2,3", infos).args == {
        "label": "fixes",
        "to": "new",
        "nodes": [1, 2, 3],
    }
    assert parse("/tag fixes 4 7", infos).args["to"] == 4
    assert parse("/load a force=false", infos).args["force"] is False


@scenario
async def test_the_driver_stays_the_authority_on_named_values(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Lines()])
    parsed = parse("/tag nolabel 99 5", await commands(h))
    assert parsed.missing == ()
    with pytest.raises(ValueError, match="existing label"):
        await h.client.run(h.cursor, parsed.command.name, parsed.args)


@scenario
async def test_an_unknown_command_is_reported_as_such(tmp_path: Path) -> None:
    infos = await commands(await Harness.create(tmp_path, extensions=[Lines()]))
    with pytest.raises(UnknownCommand) as caught:
        parse("/usr/bin/env ls", infos)
    assert caught.value.name == "usr/bin/env"
    with pytest.raises(LookupError):
        parse("/nope", infos)


@scenario
async def test_a_line_that_cannot_be_read_is_refused_with_why(tmp_path: Path) -> None:
    infos = await commands(await Harness.create(tmp_path, extensions=[Lines()]))
    for line, why in [
        ("hello", "starts with /name"),
        ("/", "starts with /name"),
        ("/configure 'slow", "cannot read the line"),
        ("/configure medium 3", "/configure mode: 'medium' is not one of"),
        ("/configure slow four", "/configure size: 'four' is not an integer"),
        ("/configure slow 1 2 3", "takes 3 arguments"),
        ("/load a force=maybe", "force: 'maybe' is not true or false"),
        ("/tag x nope 1", "to: "),
    ]:
        with pytest.raises(ValueError, match=why):
            parse(line, infos)


# completions


async def complete(h: Harness, line: str) -> list[str]:
    return await completions(line, await commands(h), h.client, h.cursor)


@scenario
async def test_command_names_complete_after_a_slash(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Lines()])
    assert await complete(h, "/") == ["/model", "/configure", "/tag", "/load"]
    assert await complete(h, "/l") == ["/load"]
    assert await complete(h, "/zz") == []
    assert await complete(h, "no slash") == []


@scenario
async def test_literal_values_complete_from_the_schema(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Lines()])
    assert await complete(h, "/configure ") == ["fast", "slow"]
    assert await complete(h, "/configure s") == ["slow"]
    assert await complete(h, "/configure mode=f") == ["mode=fast"]
    assert await complete(h, "/configure slow 3 1.0 ") == [], "past the last parameter"
    assert await complete(h, "/nope ") == []
    assert await complete(h, "/load x force=") == ["force=true", "force=false"]


@scenario
async def test_named_types_complete_through_the_client(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("a")]), extensions=[Lines()])
    await h.send("hi")
    await h.settle()
    await h.client.set_label("keep", h.cursor)
    other = await h.client.open(h.session.cursor(h.cursor).at)
    await h.settle()
    assert await complete(h, "/tag k") == ["keep"]
    assert await complete(h, "/tag keep ") == [str(h.cursor), str(other), "new"]
    assert await complete(h, "/tag keep n") == ["new"]
    assert await complete(h, "/tag keep new ") == ["3", "2", "1", "0"]
    assert await complete(h, "/tag keep new 4,") == ["4,3", "4,2", "4,1", "4,0"]
    assert await complete(h, "/tag keep new 4,3") == ["4,3"]
    assert await complete(h, "/model ") == ["fake"]
    assert await complete(h, "/model f") == ["fake"]


@scenario
async def test_paths_complete_on_the_drivers_machine_with_directories_marked(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("")
    (tmp_path / "docs sub").mkdir()
    (tmp_path / "notes.txt").write_text("")
    (tmp_path / ".hidden").write_text("")
    h = await Harness.create(tmp_path / "s", extensions=[Lines()])
    base = f"{tmp_path}/"
    assert await h.client.complete(h.cursor, "path", base + "d") == [
        base + "docs/",
        base + "docs sub/",
    ]
    assert await h.client.complete(h.cursor, "path", base + "docs/") == [base + "docs/a.md"]
    assert await h.client.complete(h.cursor, "path", base + "n") == [base + "notes.txt"]
    assert await h.client.complete(h.cursor, "path", base + ".") == [base + ".hidden"]
    assert await h.client.complete(h.cursor, "path", base + "missing/") == []
    # Words with a space are quoted, so the line still reads back as the same value.
    quoted = await complete(h, f"/load {base}docs")
    assert quoted == [base + "docs/", f"'{base}docs sub/'"]
    assert parse(f"/load {quoted[1]}", await commands(h)).args["paths"] == [base + "docs sub/"]


@scenario
async def test_complete_refuses_what_it_does_not_know(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    with pytest.raises(ValueError, match="cannot complete 'colour'"):
        await h.client.complete(h.cursor, "colour", "")
    with pytest.raises(LookupError):
        await h.client.complete(99, "label", "")
