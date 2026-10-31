"""Tools: plain Python functions the driver describes, and any hand-written `Tool`, through the
Harness."""

import asyncio
import functools
import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Optional

import pytest
from fakes import FAKE_CONFIG, FakeProvider, Reply, fake_catalog
from harness import Harness, build, called, offered, scenario

from tau_next import (
    Author,
    Body,
    DefinitionError,
    Driver,
    Outcome,
    Part,
    Session,
    Stop,
    ToolError,
)


def object_of(properties: dict[str, object], *required: str) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


# Definition


@scenario
async def test_name_description_and_schema_come_from_the_function(tmp_path: Path) -> None:
    def find(  # noqa: PLR0913, PLR0917 - one parameter per supported annotation
        pattern: str,
        limit: int = 10,
        exact: bool = False,
        ratio: float = 0.5,
        kinds: list[str] | None = None,
        mode: Literal["a", "b"] = "a",
        depth: Literal[1, 2] = 1,
    ) -> str:
        """Find things.

        Several lines, indented
            like this.
        """
        return ""

    (found,) = await offered(tmp_path, find)
    assert found.name == "find"
    assert found.description == "Find things.\n\nSeveral lines, indented\n    like this."
    assert found.parameters == object_of(
        {
            "pattern": {"type": "string"},
            "limit": {"type": "integer"},
            "exact": {"type": "boolean"},
            "ratio": {"type": "number"},
            "kinds": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
            "mode": {"type": "string", "enum": ["a", "b"]},
            "depth": {"type": "integer", "enum": [1, 2]},
        },
        "pattern",
    )


@scenario
async def test_annotated_adds_parameter_descriptions(tmp_path: Path) -> None:
    def read(path: Annotated[str, "file to read"], tail: int | None) -> str:
        """Read."""
        return ""

    (found,) = await offered(tmp_path, read)
    assert found.parameters["properties"] == {
        "path": {"type": "string", "description": "file to read"},
        "tail": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    }
    assert found.parameters["required"] == ["path"], "a T | None may be left out (D16 section 1)"


@scenario
async def test_a_function_with_no_parameters_has_an_empty_object_schema(tmp_path: Path) -> None:
    def ping() -> str:
        """Ping."""
        return "pong"

    (found,) = await offered(tmp_path, ping)
    assert found.parameters == object_of({})


def shout(text: str) -> str:
    return text


def no_annotation(x) -> str:  # type: ignore[no-untyped-def]
    """Doc."""
    return ""


def unsupported_dict(x: dict[str, str]) -> str:
    """Doc."""
    return ""


@pytest.mark.parametrize(
    ("function", "needles"),
    [
        (shout, ["'shout'", "docstring"]),
        (no_annotation, ["'no_annotation'", "'x'", "no type annotation"]),
        (unsupported_dict, ["'unsupported_dict'", "'x'", "dict"]),
    ],
)
def test_what_cannot_be_a_tool_fails_when_the_driver_is_built_naming_function_and_parameter(
    function: object, needles: list[str]
) -> None:
    with pytest.raises(DefinitionError) as raised:
        build([], [function])
    for needle in needles:
        assert needle in str(raised.value)


def test_an_annotated_with_anything_but_one_description_is_a_definition_error() -> None:
    def read(path: Annotated[str, 3]) -> str:
        """Read."""
        return path

    with pytest.raises(DefinitionError, match=r"'read'.*'path'.*one str"):
        build([], [read])


def test_a_parameter_named_progress_is_an_argument_unless_it_is_annotated_as_progress() -> None:
    def shadowed(progress: int) -> str:
        """Takes an argument that happens to be named progress."""
        return str(progress)

    def unannotated(progress) -> str:  # type: ignore[no-untyped-def]
        """The name alone says nothing to the driver."""
        return ""

    build([], [shadowed])
    with pytest.raises(DefinitionError, match=r"'unannotated'.*'progress'.*no type annotation"):
        build([], [unannotated])


# Calls


def greet(name: str, times: int = 1, tags: list[str] | None = None) -> str:
    """Greet."""
    return " ".join([f"hi {name}"] * times) + (str(tags) if tags else "")


async def agreet(name: str) -> str:
    """Greet, asynchronously."""
    return f"hi {name}"


def explode(why: str) -> str:
    """Raise."""
    raise FileNotFoundError(why)


def count(n: int) -> str:
    """Return the wrong type."""
    return n  # type: ignore[return-value]


@scenario
async def test_a_call_binds_json_arguments_sync_or_async(tmp_path: Path) -> None:
    assert await called(tmp_path, greet, '{"name": "ann", "times": 2}') == Outcome.Ok(
        "hi ann hi ann"
    )
    assert await called(tmp_path, greet, '{"name": "ann", "tags": ["x"]}') == Outcome.Ok(
        "hi ann['x']"
    )
    assert await called(tmp_path, agreet, '{"name": "bo"}') == Outcome.Ok("hi bo")


@pytest.mark.parametrize(
    ("args_raw", "needle"),
    [
        ('{"name": ', "not valid JSON"),
        ("{}", "missing required arguments ['name']"),
        ('{"name": "a", "color": 1}', "unexpected arguments ['color']"),
        ('{"name": 3}', "'name' must be string, not integer"),
        ('{"name": "a", "tags": ["x", 2]}', "item 1 must be string, not integer"),
    ],
)
@scenario
async def test_arguments_that_do_not_fit_are_an_error_the_model_sees(
    tmp_path: Path, args_raw: str, needle: str
) -> None:
    outcome = await called(tmp_path, greet, args_raw)
    assert isinstance(outcome, Outcome.Err)
    assert needle in outcome.error


@scenario
async def test_literals_floats_and_nulls_are_checked(tmp_path: Path) -> None:
    def pick(mode: Literal["a", "b"], ratio: float, note: str | None = None) -> str:
        """Pick."""
        return f"{mode} {ratio} {note}"

    ok = await called(tmp_path, pick, '{"mode": "a", "ratio": 2, "note": null}')
    assert ok == Outcome.Ok("a 2.0 None")
    bad = await called(tmp_path, pick, '{"mode": "c", "ratio": 1}')
    assert isinstance(bad, Outcome.Err)
    assert "must be one of ['a', 'b']" in bad.error


@scenario
async def test_an_exception_and_a_non_str_return_are_errors(tmp_path: Path) -> None:
    assert await called(tmp_path, explode, '{"why": "gone"}') == Outcome.Err(
        "FileNotFoundError: gone"
    )
    assert await called(tmp_path, count, '{"n": 3}') == Outcome.Err("count returned int, not str")


# Through the Harness


@scenario
async def test_a_tool_round_trip_through_the_harness(tmp_path: Path) -> None:
    """The model asks for a file, the tool answers, the model sees the answer."""
    secret = tmp_path / "secret.txt"
    secret.write_text("violet-anchor-42")

    def read_file(path: Annotated[str, "file to read"]) -> str:
        """Read a text file."""
        return Path(path).read_text()

    args = json.dumps({"path": str(secret)})
    provider = FakeProvider(
        [
            Reply([Part.Call("c1", "read_file", args)], Stop.Tools, finish_reason="tool_calls"),
            Reply([Part.Text("The phrase is violet-anchor-42")], finish_reason="stop"),
        ]
    )
    h = await Harness.create(tmp_path, provider=provider, tools=[read_file])
    await h.send("read it")
    await h.settle()
    *_, batch, results, final = h.bodies()
    assert batch == Body.Assistant([Part.Call("c1", "read_file", args)], Stop.Tools, "tool_calls")
    assert results == Body.Results([("c1", Outcome.Ok("violet-anchor-42"), Author.Tools())])
    assert final == Body.Assistant([Part.Text("The phrase is violet-anchor-42")], Stop.End, "stop")
    assert [r.tool_names for r in provider.requests] == [["read_file"], ["read_file"]]
    assert provider.requests[0].tools[0].parameters["required"] == ["path"]
    for body in h.bodies():
        if not isinstance(body, Body.Assistant | Body.Results):
            assert "read_file" not in repr(body), "tool definitions are not recorded"
    await h.close()


@scenario
async def test_a_bad_call_is_an_error_outcome_not_a_crash(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            Reply([Part.Call("c1", "greet", '{"name": 3}')], Stop.Tools),
            Reply([Part.Text("sorry")]),
        ]
    )
    h = await Harness.create(tmp_path, provider=provider, tools=[greet])
    await h.send("hi")
    await h.settle()
    results = h.bodies()[-2]
    assert isinstance(results, Body.Results)
    (_, outcome, by) = results.outcomes[0]
    assert isinstance(outcome, Outcome.Err)
    assert "'name' must be string, not integer" in outcome.error
    assert by == Author.Tools()
    await h.close()


@scenario
async def test_no_tools_means_the_sample_is_given_none(tmp_path: Path) -> None:
    provider = FakeProvider([Reply([Part.Text("x")])])
    h = await Harness.create(tmp_path, provider=provider)
    await h.send("hi")
    await h.settle()
    assert provider.requests[0].tools == ()
    await h.close()


class HandWritten:
    """A tool with no decorator: any object with these attributes is a `Tool`. Does its
    own argument validation, from the raw text, as a tool in another language would."""

    name = "shout"
    description = "Upper-case some text."
    parameters = object_of({"text": {"type": "string"}}, "text")

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def call(self, args_raw: str) -> Outcome:
        self.seen.append(args_raw)
        args = json.loads(args_raw)
        text = args.get("text")
        if not isinstance(text, str):
            return Outcome.Err("text must be a string")
        return Outcome.Ok(text.upper())


@scenario
async def test_a_hand_written_tool_satisfies_the_protocol_through_the_harness(
    tmp_path: Path,
) -> None:
    hand = HandWritten()
    provider = FakeProvider(
        [
            Reply(
                [
                    Part.Call("a", "shout", '{"text":  "hey"}'),
                    Part.Call("b", "shout", '{"text": 1}'),
                ],
                Stop.Tools,
            ),
            Reply([Part.Text("done")]),
        ]
    )
    h = await Harness.create(tmp_path, provider=provider, tools=[hand])
    await h.send("go")
    await h.settle()
    assert hand.seen == ['{"text":  "hey"}', '{"text": 1}'], "the tool gets the raw text"
    results = h.bodies()[-2]
    assert results == Body.Results(
        [
            ("a", Outcome.Ok("HEY"), Author.Tools()),
            ("b", Outcome.Err("text must be a string"), Author.Tools()),
        ]
    )
    assert provider.requests[0].tools == (hand,)
    await h.close()


# Hand-written tools the driver refuses, or that break the contract


class Good:
    name = "good"
    description = "A good tool."
    parameters: Mapping[str, object] = object_of({})

    async def call(self, args_raw: str) -> Outcome:
        return Outcome.Ok("ok")


class NoName(Good):
    name = None  # type: ignore[assignment]


class NumberName(Good):
    name = 3  # type: ignore[assignment]


class NoDescription(Good):
    description = None  # type: ignore[assignment]


class ListParameters(Good):
    parameters = ()  # type: ignore[assignment]


class NotCallable(Good):
    call = "later"  # type: ignore[assignment]


class Other(Good):
    name = "other"


class Twin(Good):
    pass


@pytest.mark.parametrize(
    ("tools", "needle"),
    [
        ([Good(), Twin()], "two tools are named 'good'"),
        ([Good(), NoName()], "tool 1"),
        ([NumberName()], "`name` must be a non-empty str"),
        ([NoDescription()], "tool 'good': `description` must be a str"),
        ([ListParameters()], "tool 'good': `parameters` must be a Mapping"),
        ([NotCallable()], "tool 'good': `call` must be callable"),
    ],
)
def test_the_driver_refuses_what_is_not_a_tool(
    tmp_path: Path, tools: list[object], needle: str
) -> None:
    session = Session.create(FAKE_CONFIG, "sys", 0)
    with pytest.raises(DefinitionError, match=needle):
        Driver(session, {"fake": FakeProvider([])}, fake_catalog(), tools=tools)  # type: ignore[arg-type]
    session.close()


def test_distinct_hand_written_tools_are_accepted() -> None:
    session = Session.create(FAKE_CONFIG, "sys", 0)
    Driver(session, {"fake": FakeProvider([])}, fake_catalog(), tools=[Good(), Other()])
    session.close()


class Returns:
    """Breaks the return contract: `call` answers something that is not `Ok` or `Err`."""

    name = "bad"
    description = "Returns the wrong thing."
    parameters: Mapping[str, object] = object_of({})

    def __init__(self, answer: object) -> None:
        self._answer = answer

    async def call(self, args_raw: str) -> Outcome:
        return self._answer  # type: ignore[return-value]


@pytest.mark.parametrize("answer", ["text", None, Outcome.Interrupted()])
@scenario
async def test_a_tool_that_breaks_the_return_contract_costs_one_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], answer: object
) -> None:
    provider = FakeProvider([Reply([Part.Call("a", "bad", "{}")], Stop.Tools), Reply([])])
    h = await Harness.create(tmp_path, provider=provider, tools=[Returns(answer)])
    await h.send("go")
    await h.settle()  # the driver lives
    (results,) = [b for b in h.bodies() if isinstance(b, Body.Results)]
    message = f"TypeError: tool 'bad' answered {answer!r}, not an Ok or Err"
    assert results.outcomes == [("a", Outcome.Err(message), Author.Tools())]
    assert message in capsys.readouterr().err
    await h.close()


# Definition gaps


def test_an_unresolvable_annotation_is_a_definition_error() -> None:
    def f(x: "Missing") -> str:  # type: ignore[name-defined]  # noqa: F821
        """Doc."""
        return ""

    with pytest.raises(DefinitionError, match=r"'f'.*cannot be resolved"):
        build([], [f])


def test_a_default_that_does_not_fit_its_annotation_is_a_definition_error() -> None:
    def none_default(s: str = None) -> str:  # type: ignore[assignment]  # noqa: RUF013
        """Doc."""
        return s

    def wrong_default(limit: int = "ten") -> str:  # type: ignore[assignment]
        """Doc."""
        return str(limit)

    with pytest.raises(DefinitionError, match=r"'s'.*default None.*`str \| None`"):
        build([], [none_default])
    with pytest.raises(DefinitionError, match=r"'limit'.*default 'ten'.*integer"):
        build([], [wrong_default])


@scenario
async def test_optional_and_equivalent_spellings_make_the_same_schema(tmp_path: Path) -> None:
    def old(x: Optional[int] = None) -> str:  # noqa: UP045
        """Doc."""
        return ""

    def new(x: int | None = None) -> str:
        """Doc."""
        return ""

    (from_old,) = await offered(tmp_path, old)
    (from_new,) = await offered(tmp_path, new)
    assert from_old.parameters == from_new.parameters


# ToolError, wrapped async, and the event loop


def refuse(why: str) -> str:
    """Refuse."""
    raise ToolError(f"I will not: {why}")


@scenario
async def test_a_tool_error_raised_by_the_function_is_the_exact_message(tmp_path: Path) -> None:
    assert await called(tmp_path, refuse, '{"why": "tired"}') == Outcome.Err("I will not: tired")


@scenario
async def test_a_sync_wrapper_around_an_async_function_is_awaited(tmp_path: Path) -> None:
    async def inner(name: str) -> str:
        """Inner."""
        return f"hi {name}"

    @functools.wraps(inner)
    def wrapper(name: str) -> object:
        return inner(name)

    assert await called(tmp_path, wrapper, '{"name": "bo"}') == Outcome.Ok("hi bo")


async def wait_for(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 5), "the tool never started"


@scenario
async def test_a_sync_tool_leaves_the_event_loop_free(tmp_path: Path) -> None:
    started, release = threading.Event(), threading.Event()

    def block() -> str:
        """Block until the test lets go."""
        started.set()
        return "released" if release.wait(5) else "the loop was blocked"

    provider = FakeProvider([Reply([Part.Call("a", "block", "{}")], Stop.Tools), Reply([])])
    h = await Harness.create(tmp_path, provider=provider, tools=[block])
    await h.send("go")
    await wait_for(started)  # only reachable if the loop is running
    release.set()
    await h.settle()
    assert h.bodies()[-2] == Body.Results([("a", Outcome.Ok("released"), Author.Tools())])
    await h.close()


@scenario
async def test_aborting_a_running_sync_tool_is_interrupted(tmp_path: Path) -> None:
    started, release = threading.Event(), threading.Event()

    def block() -> str:
        """Block until the test lets go."""
        started.set()
        release.wait(5)
        return "late"

    provider = FakeProvider([Reply([Part.Call("a", "block", "{}")], Stop.Tools)])
    h = await Harness.create(tmp_path, provider=provider, tools=[block])
    await h.send("go")
    await wait_for(started)
    await h.abort()
    release.set()
    await h.settle()
    assert h.bodies()[-1] == Body.Results([("a", Outcome.Interrupted(), Author.Tools())])
    await h.close()
