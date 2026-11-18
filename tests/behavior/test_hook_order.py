"""Hook order (D28): which extension's hook runs first at a point, by `order` and by the config."""

from dataclasses import dataclass
from pathlib import Path

import pytest
from fakes import FAKE_MODEL, FakeProvider, FakeTool, Reply
from harness import Harness, scenario

from tau import (
    Author,
    Body,
    CallDraft,
    ConfigError,
    Cursor,
    DefinitionError,
    ModelCatalog,
    NodeHandle,
    Outcome,
    Part,
    Stop,
    Subject,
    read_models,
)


def catalog(**orders: object) -> ModelCatalog:
    """The fake model, and `extensions.NAME.order` for each keyword."""
    models = {FAKE_MODEL: {"model": FAKE_MODEL, "provider": "fake"}}
    extensions = {name: {"order": order} for name, order in orders.items()}
    return read_models(cli={"models": models, "extensions": extensions})


def halter(name: str, order: object = None) -> object:
    """An extension that halts every input with its own name."""

    class Halter:
        name: str

        async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
            return f"halted by {self.name}"

    made = Halter()
    made.name = name  # pyright: ignore[reportAttributeAccessIssue]
    if order is not None:
        made.order = order  # pyright: ignore[reportAttributeAccessIssue]
    return made


def answerer(name: str, order: object = None) -> object:
    """An extension whose `pre_tool` answers the call with its own name; a later one changes it."""

    class Answerer:
        name: str

        async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
            call.outcome = Outcome.Ok(self.name)
            return None

    made = Answerer()
    made.name = name  # pyright: ignore[reportAttributeAccessIssue]
    if order is not None:
        made.order = order  # pyright: ignore[reportAttributeAccessIssue]
    return made


async def halted_by(tmp_path: Path, extensions: list[object], cat: ModelCatalog | None) -> str:
    h = await Harness.create(tmp_path, extensions=extensions, catalog=cat)  # pyright: ignore[reportArgumentType]
    await h.send("go")
    await h.settle()
    (notice,) = h.notices()
    await h.close()
    return notice


@scenario
async def test_with_no_order_anywhere_the_first_loaded_halts(tmp_path: Path) -> None:
    assert await halted_by(tmp_path, [halter("a"), halter("b")], None) == "halted by a"


@scenario
async def test_a_lower_declared_order_runs_first(tmp_path: Path) -> None:
    got = await halted_by(tmp_path, [halter("a"), halter("b", -100)], None)
    assert got == "halted by b"


@scenario
async def test_a_declared_mapping_orders_only_the_hooks_it_names(tmp_path: Path) -> None:
    first = [halter("a"), halter("b", {"pre_input": -1})]
    assert await halted_by(tmp_path / "x", first, None) == "halted by b"
    last = [halter("a"), halter("b", {"pre_input": 5})]
    assert await halted_by(tmp_path / "y", last, None) == "halted by a"


@scenario
async def test_the_config_int_beats_the_declared_order(tmp_path: Path) -> None:
    both = [halter("a", -100), halter("b")]
    assert await halted_by(tmp_path / "x", both, None) == "halted by a"
    got = await halted_by(tmp_path / "y", both, catalog(a=50))
    assert got == "halted by b"


@scenario
async def test_the_config_mapping_beats_the_declared_order_per_hook(tmp_path: Path) -> None:
    both = [halter("a", {"pre_input": -100}), halter("b")]
    got = await halted_by(tmp_path, both, catalog(a={"pre_input": 10}))
    assert got == "halted by b"


@scenario
async def test_a_tool_point_is_ordered_too(tmp_path: Path) -> None:
    async def answered(cat: ModelCatalog | None, sub: str, first_order: object) -> str:
        tool = FakeTool("t", "ran")
        script = [Reply([Part.Call("c", "t", "{}")], Stop.Tools), Reply([Part.Text("done")])]
        h = await Harness.create(
            tmp_path / sub,
            provider=FakeProvider(script),
            tools=[tool],
            extensions=[answerer("a", first_order), answerer("b")],  # pyright: ignore[reportArgumentType]
            catalog=cat,
        )
        await h.send("go")
        await h.settle()
        bodies = h.bodies()
        await h.close()
        assert tool.calls == [], "the answered call did not run"
        for who in ("a", "b"):
            if Body.Results([("c", Outcome.Ok(who), Author.Extension(who))]) in bodies:
                return who
        raise AssertionError(f"no answered results in {bodies}")

    # Each pre_tool overwrites the answer: the one that runs last wins.
    assert await answered(None, "plain", None) == "b"
    assert await answered(None, "declared", 100) == "a"
    assert await answered(catalog(b=200), "config", 100) == "b"


@pytest.mark.parametrize(
    ("order", "message"),
    [
        ("first", "extensions.a.order must be an integer or an object"),
        ({"pre_input": "x"}, "extensions.a.order.pre_input must be an integer"),
        ({"pre_tool": 1}, "has no hook 'pre_tool'"),
        ({"nope": 1}, "has no hook 'nope'"),
        (True, "must be an integer or an object"),
    ],
)
@scenario
async def test_a_bad_configured_order_stops_the_start(
    tmp_path: Path, order: object, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        await Harness.create(tmp_path, extensions=[halter("a")], catalog=catalog(a=order))  # pyright: ignore[reportArgumentType]


@scenario
async def test_an_int_order_for_an_extension_with_no_hooks_stops_the_start(tmp_path: Path) -> None:

    class Quiet:
        name = "quiet"

    quiet = Quiet()
    with pytest.raises(ConfigError, match="extension 'quiet' has no hooks to order"):
        await Harness.create(tmp_path, extensions=[quiet], catalog=catalog(quiet=1))  # pyright: ignore[reportArgumentType]


@scenario
async def test_a_bad_declared_order_is_a_definition_error(tmp_path: Path) -> None:
    with pytest.raises(DefinitionError, match="extension 'a'"):
        await Harness.create(tmp_path, extensions=[halter("a", {"nope": 1})])  # pyright: ignore[reportArgumentType]


@scenario
async def test_an_order_for_an_extension_that_is_not_loaded_is_ignored(tmp_path: Path) -> None:
    got = await halted_by(tmp_path, [halter("a")], catalog(ghost="whatever"))
    assert got == "halted by a"


@dataclass(frozen=True)
class Sized:
    size: int = 1


@scenario
async def test_a_config_node_cannot_set_an_order(tmp_path: Path) -> None:
    class Tuned:
        name = "tuned"
        options = Sized

        async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
            return None

    h = await Harness.create(tmp_path, extensions=[Tuned()])
    before = len(h.nodes())
    with pytest.raises(ConfigError, match=r"tuned\.order is not set on a branch"):
        await h.configure("tuned", {"order": 1})
    assert len(h.nodes()) == before
    await h.close()
