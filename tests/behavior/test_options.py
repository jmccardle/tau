"""An extension's options (D16 section 4): layers, `Config` nodes, `set_options`, `/config`."""

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

import pytest
from fakes import FAKE_MODEL, FakeProvider, FakeTool, Reply
from harness import Harness, build, say, scenario

from tau import (
    Body,
    CommandFailed,
    ConfigError,
    Cursor,
    CursorId,
    DefinitionError,
    Model,
    NodeHandle,
    Part,
    StepLimit,
    Stop,
    Subject,
    check_config,
    read_models,
)
from tau.commandline import completions, parse

DEFAULT_MODEL = Model(FAKE_MODEL)


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
    await h.configure("tuner", {"size": 5})
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
    await h.configure("tuner", {"mode": "slow"})
    await h.settle()
    await h.configure("tuner", {"size": 7})
    await h.settle()
    assert tuner.seen[-1] == Options(size=7, mode="slow"), "each node adds to the walk's"


@scenario
async def test_config_with_options_that_do_not_fit_is_refused_and_writes_nothing(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Tuner()])
    before = len(h.nodes())
    with pytest.raises(ConfigError, match="extension 'tuner': option 'size' must be integer"):
        await h.configure("tuner", {"size": "big"})
    with pytest.raises(ConfigError, match=r"unexpected options \['colour'\]"):
        await h.configure("tuner", {"colour": 1})
    with pytest.raises(ConfigError, match="no options"):
        await h.configure("nope", {})
    assert len(h.nodes()) == before
    assert not (await h.view()).busy


@scenario
async def test_the_config_form_names_only_the_extensions_that_have_options(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Tuner(), StepLimit()])
    (config,) = [c for c in await h.client.commands() if c.name == "config"]
    assert config.schema["properties"]["extension"] == {  # type: ignore[index]
        "type": "string",
        "enum": ["tuner", "step_limit"],
    }
    with pytest.raises(ValueError, match="configure"):
        await h.run("config", {"extension": "tuner"})
    plain = await Harness.create(tmp_path / "plain")
    assert [c.name for c in await plain.client.commands()] == ["model"]


@scenario
async def test_the_client_gets_the_options_form_at_a_cursor_with_the_current_values_as_defaults(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Tuner()], catalog=layered({"size": 3}))  # pyright: ignore[reportArgumentType]
    form = await h.client.options(h.cursor, "tuner")
    assert form.name == "tuner"
    assert form.schema["properties"] == {
        "size": {"type": "integer", "description": "how big", "default": 3},
        "mode": {"type": "string", "enum": ["fast", "slow"], "default": "fast"},
    }
    assert "required" not in form.schema
    await h.configure("tuner", {"mode": "slow"})
    await h.settle()
    after = await h.client.options(h.cursor, "tuner")
    assert after.schema["properties"]["mode"]["default"] == "slow"  # type: ignore[index]
    with pytest.raises(ConfigError, match="no options"):
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
    form = await h.client.options(h.cursor, "client")
    assert form.schema["required"] == ["endpoint"], "nobody has set it: it has no default"
    assert "default" not in form.schema["properties"]["endpoint"]  # type: ignore[index]
    await h.configure("client", {"endpoint": "http://x"})
    await h.settle()
    await h.step()
    await h.settle()
    assert ext.seen == [Needs("http://x")] * 2, "the /config step, and the asked one"


def test_check_config_reports_each_extension_whose_options_do_not_fit() -> None:
    def problems(extensions: dict[str, object], given: list[object]) -> tuple[object, ...]:
        models = {FAKE_MODEL: {"model": FAKE_MODEL, "provider": "fake"}}
        layer: dict[str, object] = {"models": models, "extensions": extensions}
        return check_config(cli=layer, providers={"fake": FakeProvider([])}, extensions=given)  # type: ignore[arg-type]

    bad: dict[str, object] = {"step_limit": {"limit": 0}}
    (limit, endpoint) = problems(bad, [StepLimit(), Client()])  # type: ignore[misc]
    assert limit.path == "extensions.step_limit"  # type: ignore[attr-defined]
    assert "at least 1" in limit.message  # type: ignore[attr-defined]
    assert endpoint.path == "extensions.client"  # type: ignore[attr-defined]
    assert "missing required options ['endpoint']" in endpoint.message  # type: ignore[attr-defined]
    fine: dict[str, object] = {"step_limit": {"limit": 3}, "client": {"endpoint": "e"}}
    assert problems(fine, [StepLimit(), Client()]) == ()
    assert problems(bad, [Tuner()]) == (), "an extension not given is not looked at"
    assert problems(bad, []) == (), "and by default none is"


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
    await h.configure("step_limit", {"limit": 2})
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


@dataclass(frozen=True)
class Tagged:
    # A list default is a dataclass error, so a list option's default is a tuple.
    tags: Annotated[list[str], "tags"] = ("a,b", "c")  # pyright: ignore[reportAssignmentType]


class Tags:
    name = "tags"
    options = Tagged

    def __init__(self) -> None:
        self.seen: list[Tagged] = []

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        self.seen.append(cursor.options())
        return None


@scenario
async def test_a_list_option_has_a_tuple_default_that_is_a_list_to_everyone(
    tmp_path: Path,
) -> None:
    ext = Tags()
    h = await Harness.create(tmp_path, extensions=[ext])
    form = await h.client.options(h.cursor, "tags")
    assert form.schema["properties"]["tags"]["default"] == ["a,b", "c"]  # type: ignore[index]
    await h.step()
    await h.settle()
    assert ext.seen == [Tagged(tags=["a,b", "c"])]  # pyright: ignore[reportArgumentType]
    assert isinstance(ext.seen[0].tags, list), "the default is a list, as a configured value is"
    await h.configure("tags", {"tags": ["x"]})
    await h.settle()
    await h.step()
    await h.settle()
    assert ext.seen[-1].tags == ["x"]


def test_a_list_options_default_must_still_fit_its_items() -> None:
    @dataclass(frozen=True)
    class Wrong:
        tags: list[str] = (1,)  # pyright: ignore[reportAssignmentType]

    with pytest.raises(DefinitionError, match="item 0 must be string"):
        build(extensions=[type("E", (), {"name": "wrong", "options": Wrong})()])


# What an options type may be.


def test_options_must_be_a_frozen_dataclass_of_the_values_language() -> None:
    @dataclass
    class Loose:
        a: int = 1

    @dataclass(frozen=True)
    class Listy:
        a: list[int] = field(default_factory=list)

    @dataclass(frozen=True)
    class Cursory:
        a: CursorId = 0  # pyright: ignore[reportAssignmentType]

    @dataclass(frozen=True)
    class Odd:
        a: dict[str, int] = dataclasses.field(default=None)  # pyright: ignore[reportAssignmentType]

    for options, message in [
        (Loose, "frozen dataclass"),
        (dict, "frozen dataclass"),
        (Listy, "use a plain default"),
        (Cursory, "looked up in a session"),
        (Odd, "unsupported annotation"),
    ]:

        class Ext:
            name = "ext"

        Ext.options = options  # type: ignore[attr-defined]
        with pytest.raises(DefinitionError, match=message):
            build([Ext()])


@scenario
async def test_config_the_options_type_refuses_is_not_written_and_the_branch_still_steps(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("ok")]), extensions=[StepLimit()])
    before = len(h.nodes())
    with pytest.raises(ConfigError, match=r"extension 'step_limit'.*at least 1"):
        await h.configure("step_limit", {"limit": 0})
    assert len(h.nodes()) == before
    await h.send("hi")
    await h.settle()
    assert (await h.view()).hook_error is None
    assert len(h.provider.requests) == 1


# An extension sets its own options and the branch's model through its Cursor.


def tune(cursor: Cursor, size: int) -> None:
    """Set this extension's size."""
    cursor.set_options(size=size)


def pick(cursor: Cursor, name: str) -> None:
    """Set this branch's model by any name."""
    cursor.set_model(Model(name))


class Picker:
    name = "tuner"
    options = Options
    commands = (tune, pick)


class NoOptions:
    name = "plain"
    commands = (tune,)


@scenario
async def test_set_options_writes_the_extensions_own_options_folded_with_the_walks(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Picker()])
    await h.run("tune", {"size": 4})
    await h.settle()
    node = h.session.tree.node(h.session.cursor(h.cursor).at)
    assert isinstance(node.body, Body.Config)
    assert node.body.config.data == '{"extensions": {"tuner": {"size": 4}}}'
    assert node.by.__class__.__name__ == "Extension"
    assert (await h.client.options(h.cursor, "tuner")).schema["properties"]["size"]["default"] == 4  # type: ignore[index]


@scenario
async def test_set_options_of_an_extension_with_none_fails_the_command(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[NoOptions()])
    before = len(h.nodes())
    with pytest.raises(CommandFailed, match="ConfigError: extension 'plain' has no options"):
        await h.run("tune", {"size": 4})
    assert len(h.nodes()) == before


@dataclass(frozen=True)
class Bounded:
    limit: int = 5

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("a limit is at least 1")


def widen(cursor: Cursor, limit: int) -> None:
    """Set the limit."""
    cursor.set_options(limit=limit)


class Bounds:
    name = "bounds"
    options = Bounded
    commands = (widen,)


@scenario
async def test_set_options_checks_before_it_writes(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Bounds()])
    before = len(h.nodes())
    with pytest.raises(CommandFailed, match="ConfigError: extension 'bounds': a limit is at"):
        await h.run("widen", {"limit": 0})
    assert len(h.nodes()) == before
    await h.settle()
    await h.run("widen", {"limit": 3})
    await h.settle()
    assert len(h.nodes()) == before + 1


@scenario
async def test_set_model_checks_the_model_against_the_catalog(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Picker()])
    before = len(h.nodes())
    with pytest.raises(CommandFailed, match="ConfigError: model 'nope' is not in your config"):
        await h.run("pick", {"name": "nope"})
    assert len(h.nodes()) == before
    await h.settle()
    await h.run("pick", {"name": FAKE_MODEL})
    await h.settle()
    assert len(h.nodes()) == before + 1


# A Model is an option; it is checked against the catalog.


@dataclass(frozen=True)
class Delegates:
    helper: Model = DEFAULT_MODEL


class Delegating:
    name = "delegating"
    options = Delegates


@scenario
async def test_a_model_may_be_an_option_and_is_checked_against_the_catalog(
    tmp_path: Path,
) -> None:
    models = {name: {"model": name, "provider": "fake"} for name in (FAKE_MODEL, "alt")}
    h = await Harness.create(
        tmp_path, extensions=[Delegating()], catalog=read_models(cli={"models": models})
    )
    form = await h.client.options(h.cursor, "delegating")
    assert form.schema["properties"]["helper"] == {  # type: ignore[index]
        "type": "string",
        "format": "model",
        "default": FAKE_MODEL,
    }, "a frontend offers the configured models"
    await h.configure("delegating", {"helper": "alt"})
    await h.settle()
    with pytest.raises(ConfigError, match="option 'helper' must be a configured model"):
        await h.configure("delegating", {"helper": "gpt-9"})


# /config on a command line.


@scenario
async def test_config_on_a_command_line_reads_the_extensions_options_form(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Tuner(), StepLimit()])
    forms = await h.client.commands()
    options = {name: await h.client.options(h.cursor, name) for name in ("tuner", "step_limit")}
    parsed = parse("/config tuner mode=slow 7", forms, options)
    assert parsed.extension == "tuner"
    assert parsed.args == {"mode": "slow", "size": 7}
    assert await parsed.run(h.client, h.cursor) is None
    await h.settle()
    assert (await h.client.options(h.cursor, "tuner")).schema["properties"]["size"]["default"] == 7  # type: ignore[index]
    bare = parse("/config step_limit", forms, options)
    assert (bare.extension, bare.args, bare.missing) == ("step_limit", {}, ())
    assert bare.command == options["step_limit"], "a bare line asks for the form"
    assert parse("/config", forms, options).missing == ("extension",)
    with pytest.raises(ValueError, match="'nope' has no options; those that do: "):
        parse("/config nope", forms, options)
    with pytest.raises(ValueError, match="limit: 'many' is not an integer"):
        parse("/config step_limit limit=many", forms, options)
    assert await completions("/config ", forms, h.client, h.cursor, options) == [
        "tuner",
        "step_limit",
    ]
    assert await completions("/config tuner 5 s", forms, h.client, h.cursor, options) == ["slow"]
    assert await completions("/config tuner mode=s", forms, h.client, h.cursor, options) == [
        "mode=slow"
    ]
