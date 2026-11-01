"""The command line (D16 section 5): `parse` and `completions`, and the client's `complete`."""

from pathlib import Path
from typing import Literal

import pytest
from fakes import FakeProvider
from harness import Harness, say, scenario

from tau import Cursor, CursorId, Form, LabelName, Model, NodeId, TreeHandle
from tau.commandline import UnknownCommand, completions, parse, value, values


def configure(cursor: Cursor, mode: Literal["fast", "slow"], size: int, ratio: float = 1.0) -> None:
    """Configure."""


def tag(tree: TreeHandle, label: LabelName, to: CursorId, nodes: list[NodeId]) -> None:
    """Tag."""


def load(paths: list[Path], model: Model, force: bool = False, note: str | None = None) -> None:
    """Load."""


def search(limit: int, query: str) -> None:
    """Search."""


def plan(task: str | None = None) -> None:
    """Plan."""


def pick(mode: Literal["a", "b"]) -> None:
    """Pick."""


def mark(name: LabelName) -> None:
    """Mark."""


class Lines:
    name = "lines"
    commands = (
        configure,
        tag,
        load,
        search,
        plan,
        pick,
        mark,
    )


async def commands(h: Harness) -> tuple[Form, ...]:
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
async def test_a_list_starting_with_a_bracket_is_a_json_array_so_items_may_hold_commas(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Lines()])
    infos = await commands(h)
    assert parse("""/load '["a,b.txt", "c"]' force=yes""", infos).args["paths"] == ["a,b.txt", "c"]
    assert parse("/tag fixes new '[1, 2]'", infos).args["nodes"] == [1, 2]
    assert parse("/load '[]'", infos).args["paths"] == []
    with pytest.raises(ValueError, match=r"/load paths: .* is not a JSON array"):
        parse("/load '[a,b'", infos)
    assert await complete(h, """/load '["a""") == [], "a JSON array is typed whole"


@scenario
async def test_a_list_of_texts_that_start_with_a_bracket_says_how_to_type_it(
    tmp_path: Path,
) -> None:
    infos = await commands(await Harness.create(tmp_path, extensions=[Lines()]))
    with pytest.raises(ValueError, match=r"""type \["\[0-9\]\+", "foo"\]"""):
        parse("/load [0-9]+,foo", infos)
    assert parse("""/load '["[0-9]+", "foo"]'""", infos).args["paths"] == ["[0-9]+", "foo"]
    with pytest.raises(ValueError, match=r"read as a JSON array") as raised:
        parse("/tag fixes new '[1,'", infos)
    assert "type" not in str(raised.value).split("array", 1)[1], "no string hint for numbers"


@scenario
async def test_the_last_str_parameter_takes_the_rest_of_the_line_as_typed(tmp_path: Path) -> None:
    infos = await commands(await Harness.create(tmp_path, extensions=[Lines()]))
    assert parse("/search 3 what is 2+2", infos).args == {"limit": 3, "query": "what is 2+2"}
    assert parse("/search limit=3 what is 2+2", infos).args == {"limit": 3, "query": "what is 2+2"}
    assert parse('/search 3 don\'t  panic "x" limit=9', infos).args == {
        "limit": 3,
        "query": 'don\'t  panic "x" limit=9',
    }, "quotes and spacing in the rest are the text, and are not read as shell words"
    assert parse("/search 3 what", infos).missing == ()
    assert parse("/search 3", infos).missing == ("query",), "no rest, no value"
    assert parse('/search 3 "what is 2+2"', infos).args["query"] == "what is 2+2", "one word"
    assert parse("/search 3 'what is' 2", infos).args["query"] == "'what is' 2", "two words: raw"
    assert parse("/search", infos).missing == ("limit", "query")


@scenario
async def test_a_str_or_none_parameter_takes_the_rest_too_and_may_be_left_out(
    tmp_path: Path,
) -> None:
    infos = await commands(await Harness.create(tmp_path, extensions=[Lines()]))
    assert parse("/plan add retry", infos).args == {"task": "add retry"}
    assert parse("/plan", infos).args == {}
    assert parse("/plan\tadd retry", infos).args == {"task": "add retry"}, "a tab ends the name"
    assert parse("/plan task='add retry'", infos).args == {"task": "add retry"}, "named: as before"
    with pytest.raises(ValueError, match="/plan takes 1 arguments"):
        parse("/plan task=add retry", infos)


@scenario
async def test_only_a_plain_str_takes_the_rest(tmp_path: Path) -> None:
    infos = await commands(await Harness.create(tmp_path, extensions=[Lines()]))
    with pytest.raises(ValueError, match="/pick takes 1 arguments"):
        parse("/pick a b", infos)
    with pytest.raises(ValueError, match="/mark takes 1 arguments"):
        parse("/mark a b", infos)
    with pytest.raises(ValueError, match="/configure takes 3 arguments"):
        parse("/configure slow 3 0.5 more", infos)
    with pytest.raises(ValueError, match="/load takes 4 arguments"):
        parse("/load a fake yes note=x more", infos)  # `note` was named: no rest is left


@scenario
async def test_the_driver_stays_the_authority_on_named_values(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Lines()])
    parsed = parse("/tag nolabel 99 5", await commands(h))
    assert parsed.missing == ()
    with pytest.raises(ValueError, match="existing label"):
        await parsed.run(h.client, h.cursor)


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
    assert await complete(h, "/") == [
        "/model",
        "/configure",
        "/tag",
        "/load",
        "/search",
        "/plan",
        "/pick",
        "/mark",
    ]
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
    assert await complete(h, "/search 3 wh") == [], "the rest of the line is text"
    assert await complete(h, "/search 3 what is 2+2 and ") == []


@scenario
async def test_named_types_complete_through_the_client(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("a")]), extensions=[Lines()])
    await h.send("hi")
    await h.settle()
    await h.client.set_label(h.cursor, "keep")
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


@scenario
async def test_a_leading_tilde_is_the_home_directory_in_a_path_and_its_completion(
    tmp_path: Path,
) -> None:
    home = Path.home()
    visible = sorted(p.name for p in home.iterdir() if not p.name.startswith("."))
    h = await Harness.create(tmp_path, extensions=[Lines()])
    assert await h.client.complete(h.cursor, "path", "~") == ["~/"]
    found = await h.client.complete(h.cursor, "path", "~/")
    assert [f.removeprefix("~/").removesuffix("/") for f in found] == visible


def expand(path: Path) -> str:
    """Say which path it is."""
    return str(path)


class Expands:
    name = "expands"
    commands = (expand,)


@scenario
async def test_a_leading_tilde_in_a_path_argument_is_the_home_directory(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Expands()])
    assert await h.run("expand", {"path": "~/a.txt"}) == str(Path.home() / "a.txt")
    await h.close()


# value and values: one value of any schema, such as a question's answer typed as text.


@scenario
async def test_value_reads_text_and_values_completes_it_as_a_commands_argument_is(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Lines()])
    forms = {form.name: form for form in await commands(h)}
    cursor_schema = forms["tag"].schema["properties"]["to"]  # type: ignore[index]
    assert value("new", cursor_schema) == "new"
    assert value("4", cursor_schema) == 4
    with pytest.raises(ValueError, match="is not an integer"):
        value("four", cursor_schema)
    assert await values(cursor_schema, "n", h.client, h.cursor) == ["new"]
    assert await values(cursor_schema, "", h.client, h.cursor) == [str(h.cursor), "new"]
    mode_schema = forms["configure"].schema["properties"]["mode"]  # type: ignore[index]
    assert value("slow", mode_schema) == "slow"
    assert await values(mode_schema, "f", h.client, h.cursor) == ["fast"]
    await h.close()


# Qualified names (D19 section 7).


def note_a(tree: TreeHandle) -> str:
    """A's note."""
    return "from a"


def note_b(tree: TreeHandle) -> str:
    """B's note."""
    return "from b"


def only_a(tree: TreeHandle) -> None:
    """Only A has it."""


note_a.__name__ = note_b.__name__ = "note"


class A:
    name = "a"
    commands = (note_a, only_a)


class B:
    name = "b"
    commands = (note_b,)


@scenario
async def test_a_qualified_name_parses_and_a_shared_bare_name_is_ambiguous(tmp_path: Path) -> None:
    infos = await commands(await Harness.create(tmp_path, extensions=[A(), B()]))
    assert parse("/b:note", infos).command.extension == "b"
    assert parse("/only_a", infos).command.qualified == "a:only_a"
    with pytest.raises(LookupError, match="/note is two commands: /a:note, /b:note") as caught:
        parse("/note", infos)
    assert not isinstance(caught.value, UnknownCommand), "a frontend must not send it as text"
    with pytest.raises(UnknownCommand):
        parse("/c:note", infos)


@scenario
async def test_a_qualified_name_runs_the_command_of_that_extension(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[A(), B()])
    parsed = parse("/b:note", await commands(h))
    assert await parsed.run(h.client, h.cursor) == "from b"
    await h.close()


@scenario
async def test_completion_offers_bare_names_of_unique_commands_and_qualified_shared_ones(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[A(), B()])
    assert await complete(h, "/") == ["/model", "/a:note", "/only_a", "/b:note"]
    assert await complete(h, "/on") == ["/only_a"]
    assert await complete(h, "/a") == ["/a:note"]
    assert await complete(h, "/b:") == ["/b:note"]
    assert await complete(h, "/a:n") == ["/a:note"]
    await h.close()
