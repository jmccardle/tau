"""The type language (D16 section 1), seen where a person or a model meets it: a function's
schema as a command's form (what a client reads) and as a tool's parameters (what a model reads),
the checks a call gets, and what the function receives."""

from pathlib import Path
from typing import Annotated, Literal, NewType

import pytest
from fakes import FAKE_MODEL, FakeProvider, Reply
from harness import Harness, build, say, scenario

from tau_next import CursorId, DefinitionError, LabelName, Model, NodeId, Part, Stop

NewTypeOfMine = NewType("NewTypeOfMine", str)
ANYTHING = Model("anything")


def probe(  # noqa: PLR0913, PLR0917 - one parameter per hint of the language
    path: Path,
    model: Model,
    label: LabelName,
    node: NodeId,
    cursor: CursorId,
    paths: list[Path],
    which: Annotated[Model | None, "which model"],
    mode: Literal["a", "b"],
) -> str:
    """Take one of each."""
    return "probed"


class Probes:
    name = "probes"
    tools = commands = (probe,)


CLIENT = {
    "path": {"type": "string", "format": "path"},
    "model": {"type": "string", "format": "model"},
    "label": {"type": "string", "format": "label"},
    "node": {"type": "integer", "format": "node"},
    "cursor": {
        "anyOf": [{"type": "integer"}, {"type": "string", "enum": ["new"]}],
        "format": "cursor",
    },
    "paths": {"type": "array", "items": {"type": "string", "format": "path"}},
    "which": {
        "anyOf": [{"type": "string", "format": "model"}, {"type": "null"}],
        "description": "which model",
        "default": None,
    },
    "mode": {"type": "string", "enum": ["a", "b"]},
}
MODEL = {
    "path": {"type": "string"},
    "model": {"type": "string"},
    "label": {"type": "string"},
    "node": {"type": "integer"},
    "cursor": {"anyOf": [{"type": "integer"}, {"type": "string", "enum": ["new"]}]},
    "paths": {"type": "array", "items": {"type": "string"}},
    "which": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "which model"},
    "mode": {"type": "string", "enum": ["a", "b"]},
}


@scenario
async def test_the_client_schema_names_the_named_types_and_the_model_schema_does_not(
    tmp_path: Path,
) -> None:
    provider = FakeProvider([say("ok")])
    h = await Harness.create(tmp_path, provider=provider, extensions=[Probes()])
    (form,) = [c for c in await h.client.commands() if c.name == "probe"]
    assert form.schema["properties"] == CLIENT
    await h.send("hi")
    await h.settle()
    (offered,) = provider.requests[0].tools
    assert offered.parameters["properties"] == MODEL
    assert form.schema["required"] == offered.parameters["required"]
    assert "which" not in form.schema["required"], "a T | None may be left out"  # type: ignore[operator]
    await h.close()


def fields(
    n: int = 3,
    where: Path = Path("x"),
    note: str | None = None,
    tags: list[str] = ["a"],  # noqa: B006 - the default is only read, to be shown
    format: Path = Path("y"),
) -> str:
    """Take defaults."""
    return "ok"


class Defaults:
    name = "defaults"
    tools = commands = (fields,)


@scenario
async def test_a_default_is_in_the_client_schema_and_not_in_the_model_schema(
    tmp_path: Path,
) -> None:
    provider = FakeProvider([say("ok")])
    h = await Harness.create(tmp_path, provider=provider, extensions=[Defaults()])
    (form,) = [c for c in await h.client.commands() if c.name == "fields"]
    properties: dict[str, dict[str, object]] = form.schema["properties"]  # type: ignore[assignment]
    assert properties["n"] == {"type": "integer", "default": 3}
    assert properties["where"] == {"type": "string", "format": "path", "default": "x"}
    assert properties["note"]["default"] is None
    assert properties["tags"] == {"type": "array", "items": {"type": "string"}, "default": ["a"]}
    assert properties["format"] == {"type": "string", "format": "path", "default": "y"}
    assert "required" not in form.schema
    await h.send("hi")
    await h.settle()
    shown = provider.requests[0].tools[0].parameters["properties"]
    assert all("default" not in p for p in shown.values()), "no provider API takes them"  # type: ignore[attr-defined]
    await h.close()


@pytest.mark.parametrize("hint", [dict[str, str], NewTypeOfMine, Annotated[int, 3]])
def test_what_is_outside_the_language_fails_when_the_driver_is_built(hint: object) -> None:
    def outside(x: hint) -> str:  # type: ignore[valid-type]
        """Doc."""
        return ""

    with pytest.raises(DefinitionError, match=r"'outside'.*'x'"):
        build([], [outside])


# What a call is checked against


def look(model: Model, cursor: CursorId, label: LabelName, node: NodeId) -> str:
    """Look at one of each."""
    return f"{model} {cursor} {label} {node}"


class Looks:
    name = "looks"
    commands = (look,)


@scenario
async def test_named_types_are_checked_against_the_session(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Looks()])
    await h.client.set_label(h.cursor, "main")
    ok: dict[str, object] = {"model": FAKE_MODEL, "cursor": h.cursor, "label": "main", "node": 0}
    assert await h.run("look", ok) == f"{FAKE_MODEL} {h.cursor} main 0"
    assert await h.run("look", {**ok, "cursor": "new"}) == f"{FAKE_MODEL} 1 main 0", "opened last"
    await h.settle()  # the cursor it opened wakes
    for change, problem in [
        ({"model": "nope"}, "argument 'model' must be a configured model, not 'nope'"),
        ({"cursor": 9}, "argument 'cursor' must be an open cursor or 'new', not 9"),
        ({"label": "other"}, "argument 'label' must be an existing label, not 'other'"),
        ({"node": 99}, "argument 'node' must be an existing node, not 99"),
        ({"node": True}, "argument 'node' must be integer, not boolean"),
    ]:
        with pytest.raises(ValueError, match=problem):
            await h.run("look", {**ok, **change})
    with pytest.raises(ValueError, match=r"missing required arguments \['model'\]"):
        await h.run("look", {k: v for k, v in ok.items() if k != "model"})
    await h.close()


def show(
    paths: list[Path], maybe: Path | None, ratio: float, ratios: list[float], n: int | None
) -> str:
    """Show what the function is given."""
    return repr((paths, maybe, ratio, type(ratio).__name__, ratios, n))


class Shows:
    name = "shows"
    commands = (show,)


@scenario
async def test_the_function_receives_paths_floats_and_none_for_what_is_left_out(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Shows()])
    got = await h.run(
        "show", {"paths": ["a", "b/c"], "maybe": None, "ratio": 3, "ratios": [1, 2.5]}
    )
    assert got == repr(([Path("a"), Path("b/c")], None, 3.0, "float", [1.0, 2.5], None))
    await h.close()


@scenario
async def test_the_model_is_checked_too(tmp_path: Path) -> None:
    args = '{"model": "nope", "cursor": 0, "label": "x", "node": 0}'
    provider = FakeProvider(
        [
            Reply([Part.Call("c", "look", args)], Stop.Tools),
            say("ok"),
        ]
    )

    class LooksTool:
        name = "looks_tool"
        tools = (look,)

    h = await Harness.create(tmp_path, provider=provider, extensions=[LooksTool()])
    await h.send("go")
    await h.settle()
    results = h.bodies()[-2]
    assert "must be a configured model" in repr(results)
    await h.close()


# What a definition may not say


def test_a_path_default_is_a_path_and_other_defaults_are_checked_by_shape() -> None:
    def fine(p: Path = Path("x"), m: Model = ANYTHING) -> str:
        """Fine."""
        return ""

    def string_for_path(p: Path = "x") -> str:  # type: ignore[assignment]
        """Doc."""
        return ""

    def none_for_str(s: str = None) -> str:  # type: ignore[assignment]  # noqa: RUF013
        """Doc."""
        return ""

    build([], [fine])
    with pytest.raises(DefinitionError, match="default 'x'"):
        build([], [string_for_path])
    with pytest.raises(DefinitionError, match=r"default None.*`str \| None`"):
        build([], [none_for_str])


def test_a_cursor_id_cannot_default_to_new() -> None:
    def to_new(to: CursorId = "new") -> str:  # type: ignore[assignment]
        """Doc."""
        return ""

    def to_new_list(to: list[CursorId] = ["new"]) -> str:  # type: ignore[list-item]  # noqa: B006
        """Doc."""
        return ""

    def to_zero(to: CursorId = CursorId(0)) -> str:  # noqa: B008
        """Doc."""
        return ""

    with pytest.raises(DefinitionError, match="default 'new'"):
        build([], [to_new])
    with pytest.raises(DefinitionError, match="opens a cursor"):
        build([], [to_new_list])
    build([], [to_zero])
