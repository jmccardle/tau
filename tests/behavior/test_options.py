"""An extension's options (D16 section 4): layers, `Config` nodes, `configure`, `/config`."""

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

import pytest
from fakes import FAKE_MODEL, FakeProvider, FakeTool, Reply
from harness import Harness, build, say, scenario

from tau_next import (
    Body,
    Cursor,
    ExtensionContractError,
    Model,
    NodeHandle,
    OptionsError,
    Part,
    StepLimit,
    Stop,
    Subject,
    check_options,
    read_models,
)

DEFAULT_MODEL = Model("x")


@dataclass(frozen=True)
class Options:
    size: Annotated[int, "how big"] = 10
    mode: Literal["fast", "slow"] = "fast"


class Tuner:
    """Reads its options at every step and remembers what it saw."""

    name = "tuner"
    options = Options

    def __init__(self) -> None:
        self.seen: list[Options] = []

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        self.seen.append(cursor.options())
        return None


def layered(options: dict[str, object]) -> object:
    models = {FAKE_MODEL: {"model": FAKE_MODEL, "provider": "fake"}}
    return read_models(cli={"models": models, "extensions": {"tuner": options}})


@scenario
async def test_options_are_their_defaults_until_something_sets_them(tmp_path: Path) -> None:
    tuner = Tuner()
    h = await Harness.create(tmp_path, extensions=[tuner])
    await h.step()
    await h.settle()
    assert tuner.seen == [Options(size=10, mode="fast")]


@scenario
async def test_the_layers_set_options_per_field(tmp_path: Path) -> None:
    tuner = Tuner()
    h = await Harness.create(tmp_path, extensions=[tuner], catalog=layered({"size": 3}))  # pyright: ignore[reportArgumentType]
    await h.step()
    await h.settle()
    assert tuner.seen == [Options(size=3, mode="fast")]


@scenario
async def test_config_changes_options_for_this_branch_only(tmp_path: Path) -> None:
    tuner = Tuner()
    h = await Harness.create(tmp_path, extensions=[tuner])
    root = h.session.cursor(h.cursor).at
    other = await h.client.open(root)
    await h.settle()
    tuner.seen.clear()
    await h.run("config", {"extension": "tuner", "values": '{"size": 5}'})
    await h.settle()
    node = h.session.tree.node(h.session.cursor(h.cursor).at)
    assert isinstance(node.body, Body.Config)
    assert node.body.config.data == '{"extensions": {"tuner": {"size": 5}}}', "just the fields"
    await h.step()
    await h.step(cursor=other)
    await h.settle()
    assert tuner.seen[-2:] == [Options(size=5), Options(size=10)]


@scenario
async def test_a_config_node_sets_only_the_options_it_names(tmp_path: Path) -> None:
    tuner = Tuner()
    h = await Harness.create(tmp_path, extensions=[tuner], catalog=layered({"size": 3}))  # pyright: ignore[reportArgumentType]
    await h.step()
    await h.settle()
    assert tuner.seen == [Options(size=3, mode="fast")], "the layers' size, before any node"
    await h.run("config", {"extension": "tuner", "values": '{"mode": "slow"}'})
    await h.settle()
    await h.run("config", {"extension": "tuner", "values": '{"size": 7}'})
    await h.settle()
    assert tuner.seen[-1] == Options(size=7, mode="slow"), "each node adds to the walk's"


@scenario
async def test_config_with_options_that_do_not_fit_fails_the_step_and_writes_nothing(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Tuner()])
    before = len(h.nodes())
    await h.run("config", {"extension": "tuner", "values": '{"size": "big"}'})
    await h.settle()
    (notice,) = h.notices()
    assert notice.startswith("OptionsError: extension 'tuner': option 'size' must be integer")
    assert len(h.nodes()) == before
    await h.run("config", {"extension": "tuner", "values": '{"colour": 1}'})
    await h.settle()
    await h.run("config", {"extension": "tuner", "values": "[1]"})
    await h.settle()
    assert "unexpected options ['colour']" in h.notices()[1]
    assert "must be a JSON object" in h.notices()[2]


@scenario
async def test_config_names_only_the_extensions_that_have_options(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Tuner(), StepLimit()])
    (config,) = [c for c in await h.client.commands() if c.name == "config"]
    assert config.schema["properties"]["extension"] == {  # type: ignore[index]
        "type": "string",
        "enum": ["tuner", "step_limit"],
    }
    with pytest.raises(ValueError, match="one of"):
        await h.run("config", {"extension": "nope", "values": "{}"})
    plain = await Harness.create(tmp_path / "plain")
    assert [c.name for c in await plain.client.commands()] == ["model"]


@scenario
async def test_the_client_gets_the_options_form_at_a_cursor(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Tuner()], catalog=layered({"size": 3}))  # pyright: ignore[reportArgumentType]
    form = await h.client.options(h.cursor, "tuner")
    assert form.extension == "tuner"
    assert form.schema["properties"]["size"] == {  # type: ignore[index]
        "type": "integer",
        "description": "how big",
    }
    assert form.value == {"size": 3, "mode": "fast"}
    await h.run("config", {"extension": "tuner", "values": '{"mode": "slow"}'})
    await h.settle()
    assert (await h.client.options(h.cursor, "tuner")).value == {"size": 3, "mode": "slow"}
    with pytest.raises(OptionsError, match="no options"):
        await h.client.options(h.cursor, "nope")


@scenario
async def test_a_tool_hook_reads_its_options_at_the_batchs_node(tmp_path: Path) -> None:
    seen: list[Options] = []

    class Hooked:
        name = "tuner"
        options = Options

        async def pre_tool(self, node: NodeHandle, call: object) -> str | None:
            seen.append(node.options())
            return None

    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Call("a", "t", "{}")], Stop.Tools), say("done")]),
        extensions=[Hooked()],
        catalog=layered({"mode": "slow"}),  # pyright: ignore[reportArgumentType]
        tools=[FakeTool("t", "ok")],
    )
    await h.send("go")
    await h.settle()
    assert seen == [Options(mode="slow")]


# A required option, and the check of the layers.


@dataclass(frozen=True)
class Needs:
    endpoint: str
    retries: int = 2


class Client:
    name = "client"
    options = Needs

    def __init__(self) -> None:
        self.seen: list[Needs] = []

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        self.seen.append(cursor.options())
        return None


@scenario
async def test_an_option_with_no_default_must_be_configured(tmp_path: Path) -> None:
    ext = Client()
    h = await Harness.create(tmp_path, extensions=[ext])
    await h.step()
    await h.settle()
    view = await h.view()
    assert view.hook_error is not None
    assert "missing required options ['endpoint']" in view.hook_error.error
    await h.run("config", {"extension": "client", "values": '{"endpoint": "http://x"}'})
    await h.settle()
    await h.step()
    await h.settle()
    assert ext.seen == [Needs("http://x")] * 2, "the /config step, and the asked one"


def test_check_options_reports_each_extension_whose_options_do_not_fit() -> None:
    models = {FAKE_MODEL: {"model": FAKE_MODEL, "provider": "fake"}}
    catalog = read_models(cli={"models": models, "extensions": {"step_limit": {"limit": 0}}})
    (limit, endpoint) = check_options(catalog, [StepLimit(), Client()])
    assert limit.path == "extensions.step_limit"
    assert "at least 1" in limit.message
    assert endpoint.path == "extensions.client"
    assert "missing required options ['endpoint']" in endpoint.message
    fine = read_models(
        cli={
            "models": models,
            "extensions": {"step_limit": {"limit": 3}, "client": {"endpoint": "e"}},
        }
    )
    assert check_options(fine, [StepLimit(), Client()]) == ()
    assert check_options(catalog, [Tuner()]) == (), "an extension not given is not looked at"


@scenario
async def test_options_that_set_the_step_limit_through_config(tmp_path: Path) -> None:
    class KeepGoing:
        name = "keep_going"

        async def post_reply(self, cursor: Cursor, subject: Subject) -> str | None:
            cursor.write(Body.User("continue"))
            return None

    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([say(f"r{i}") for i in range(10)]),
        extensions=[KeepGoing(), StepLimit()],
    )
    await h.run("config", {"extension": "step_limit", "values": '{"limit": 2}'})
    await h.settle()
    await h.send("go")
    await h.settle()
    assert len(h.provider.requests) == 2


@scenario
async def test_the_layers_set_the_step_limit(tmp_path: Path) -> None:
    class KeepGoing:
        name = "keep_going"

        async def post_reply(self, cursor: Cursor, subject: Subject) -> str | None:
            cursor.write(Body.User("continue"))
            return None

    models = {FAKE_MODEL: {"model": FAKE_MODEL, "provider": "fake"}}
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([say(f"r{i}") for i in range(10)]),
        extensions=[KeepGoing(), StepLimit()],
        catalog=read_models(cli={"models": models, "extensions": {"step_limit": {"limit": 3}}}),
    )
    await h.send("go")
    await h.settle()
    assert len(h.provider.requests) == 3


# What an options type may be.


def test_options_must_be_a_frozen_dataclass_of_the_values_language() -> None:
    @dataclass
    class Loose:
        a: int = 1

    @dataclass(frozen=True)
    class Listy:
        a: list[int] = field(default_factory=list)

    @dataclass(frozen=True)
    class Named:
        a: Model = DEFAULT_MODEL

    @dataclass(frozen=True)
    class Odd:
        a: dict[str, int] = dataclasses.field(default=None)  # pyright: ignore[reportAssignmentType]

    for options, message in [
        (Loose, "frozen dataclass"),
        (dict, "frozen dataclass"),
        (Listy, "use a plain default"),
        (Named, "checked against a session"),
        (Odd, "unsupported annotation"),
    ]:

        class Ext:
            name = "ext"

        Ext.options = options  # type: ignore[attr-defined]
        with pytest.raises(ExtensionContractError, match=message):
            build([Ext()])
