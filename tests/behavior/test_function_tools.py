"""Plain functions as tools (D16 section 2): an extension lists them in `tools`."""

import asyncio
import json
import types
from pathlib import Path
from typing import ClassVar, cast

import pytest
from fakes import FAKE_CONFIG, FakeProvider, Gate, Reply, fake_catalog
from harness import Harness, scenario

from tau_next import (
    Author,
    Body,
    Cursor,
    CursorId,
    Driver,
    Extension,
    FunctionTool,
    Live,
    NodeHandle,
    Outcome,
    Part,
    Progress,
    Session,
    Stop,
    ToolContractError,
    ToolDefinitionError,
    ToolProgress,
    TreeHandle,
    tool,
)

BATCH = 3  # the Assistant node holding the calls: Config 0, System 1, User 2, Assistant 3


def call(tool_name: str, **args: object) -> Reply:
    return Reply([Part.Call("c1", tool_name, json.dumps(args))], Stop.Tools)


def build(tools: list[object], extensions: list[object]) -> None:
    session = Session.create(FAKE_CONFIG, "sys", 0)
    try:
        Driver(
            session,
            {"fake": FakeProvider([])},
            fake_catalog(),
            tools=tools,  # type: ignore[arg-type]
            extensions=extensions,  # type: ignore[arg-type]
        )
    finally:
        session.close()


# D16 section 6, first case: a function that is a tool.


def word_count(path: Path) -> str:
    """Count the words in a file."""
    return f"{len(path.read_text().split())} words"


# The module is the extension: the model calls it; a person types /word_count <Tab>.
words = cast(
    "Extension",
    types.SimpleNamespace(name="words", tools=[word_count], commands=[word_count]),
)


@scenario
async def test_word_count_is_a_tool_the_model_calls(tmp_path: Path) -> None:
    essay = tmp_path / "essay.txt"
    essay.write_text("the quick brown fox")
    provider = FakeProvider([call("word_count", path=str(essay)), Reply([Part.Text("four")])])
    h = await Harness.create(tmp_path, provider=provider, extensions=[words])
    await h.send("count")
    await h.settle()
    (offered,) = provider.requests[0].tools
    assert offered.name == "word_count"
    assert offered.description == "Count the words in a file."
    assert offered.parameters == {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "additionalProperties": False,
        "required": ["path"],
    }, "the model is not shown the path format"
    assert isinstance(offered, FunctionTool)
    assert offered.client_parameters["properties"] == {"path": {"type": "string", "format": "path"}}
    results = h.bodies()[-2]
    assert results == Body.Results([("c1", Outcome.Ok("4 words"), Author.Tools())])
    await h.close()


# Injected parameters


def where(node: NodeHandle, label: str) -> str:
    """Say where the call is."""
    return f"{label}: {node.extension} at {node.id}"


def where_tree(tree: TreeHandle) -> str:
    """Say which extension it is."""
    return tree.extension


class Places:
    name = "places"
    tools = (where, where_tree)


@scenario
async def test_a_handle_is_the_batchs_and_bound_to_the_extension_that_lists_the_function(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        [
            Reply(
                [
                    Part.Call("a", "where", '{"label": "here"}'),
                    Part.Call("b", "where_tree", "{}"),
                ],
                Stop.Tools,
            ),
            Reply([Part.Text("ok")]),
        ]
    )
    h = await Harness.create(tmp_path, provider=provider, extensions=[Places()])
    await h.send("go")
    await h.settle()
    assert h.bodies()[-2] == Body.Results(
        [
            ("a", Outcome.Ok(f"here: places at {BATCH}"), Author.Tools()),
            ("b", Outcome.Ok("places"), Author.Tools()),
        ]
    )
    (offered, _) = provider.requests[0].tools
    assert offered.parameters["properties"] == {"label": {"type": "string"}}, "no handle shown"
    await h.close()


def test_a_function_that_takes_a_handle_is_not_a_tool_of_the_driver() -> None:
    with pytest.raises(ToolContractError, match=r"'where'.*extension"):
        build([tool(where)], [])
    with pytest.raises(ToolContractError, match=r"'where_tree'.*extension"):
        build([tool(where_tree)], [])


def on_a_cursor(cursor: Cursor) -> str:
    """Needs a cursor."""
    return str(cursor.id)


class Cursored:
    name = "cursored"
    tools = (on_a_cursor,)


def test_a_tool_cannot_take_a_cursor() -> None:
    with pytest.raises(ToolDefinitionError, match=r"'on_a_cursor'.*'cursor'.*Cursor"):
        tool(on_a_cursor)
    with pytest.raises(ToolDefinitionError, match="'on_a_cursor'"):
        build([], [Cursored()])


def test_a_function_that_cannot_be_described_fails_when_the_driver_is_built() -> None:
    def bare(x):  # type: ignore[no-untyped-def]
        """Doc."""

    class Bare:
        name = "bare"
        tools = (bare,)

    with pytest.raises(ToolDefinitionError, match=r"'bare'.*'x'.*no type annotation"):
        build([], [Bare()])


def test_tool_names_are_unique_across_the_driver_and_every_extension() -> None:
    class Other:
        name = "other"
        tools = (word_count,)

    with pytest.raises(ToolContractError, match="two tools are named 'word_count'"):
        build([tool(word_count)], [words])
    with pytest.raises(ToolContractError, match="two tools are named 'word_count'"):
        build([], [words, Other()])
    build([tool(word_count)], [Places()])


def test_an_extensions_tools_must_be_a_sequence_of_tools_and_functions() -> None:
    class Wrong:
        name = "wrong"
        tools = "word_count"

    class Unlike:
        name = "unlike"
        tools = (3,)

    with pytest.raises(ToolContractError, match=r"'wrong'.*sequence"):
        build([], [Wrong()])
    with pytest.raises(ToolContractError, match=r"'unlike'.*neither a Tool nor a function"):
        build([], [Unlike()])


def test_a_foreign_tool_object_may_be_listed_by_an_extension() -> None:
    class Hand:
        name = "hand"
        description = "A hand-written tool."
        parameters: ClassVar = {"type": "object", "properties": {}}

        async def call(self, args_raw: str) -> Outcome:
            return Outcome.Ok("hand")

    class Lists:
        name = "lists"
        tools = (Hand(),)

    build([], [Lists()])


# Progress by annotation


STOP = Gate()


async def working(shown: Progress) -> str:
    """Show some work."""
    shown("halfway")
    await STOP.wait()
    return "done"


class Workers:
    name = "workers"
    tools = (working,)


@scenario
async def test_progress_is_injected_by_annotation(tmp_path: Path) -> None:
    provider = FakeProvider([call("working"), Reply([Part.Text("ok")])])
    h = await Harness.create(tmp_path, provider=provider, extensions=[Workers()], gates=[STOP])
    stream = await h.connect("watcher", watching=[h.cursor])
    await h.send("go")
    await h.settle()  # the function shows its progress and waits at the gate
    async with asyncio.timeout(5):
        async for event in stream:
            if isinstance(event, Live) and isinstance(event.delta, ToolProgress):
                assert event.delta == ToolProgress(BATCH, "c1", "halfway")
                break
    assert offered_parameters(provider) == {}, "the callback is not an argument"
    STOP.release()
    await h.settle()
    await stream.close()
    await h.close()


def offered_parameters(provider: FakeProvider) -> object:
    return provider.requests[0].tools[0].parameters["properties"]


# CursorId "new"


def hand_off(tree: TreeHandle, to: CursorId, note: str) -> str:
    """Send a note to another cursor."""
    tree.add_item(to, note)
    return f"sent to cursor {to}"


class Hands:
    name = "hands"
    tools = (hand_off,)


@scenario
async def test_a_new_cursor_is_opened_at_the_batch_after_every_argument_is_checked(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        [
            call("hand_off", to="new", note=3),  # refused: nothing opens
            call("hand_off", to=9, note="x"),  # refused: no such cursor
            call("hand_off", to="new", note="x"),
            Reply([Part.Text("done")]),
            Reply([Part.Text("the other cursor")]),
            Reply([Part.Text("and again")]),
        ]
    )
    h = await Harness.create(tmp_path, provider=provider, extensions=[Hands()])
    await h.send("go")
    await h.settle()
    outcomes = [b.outcomes[0][1] for b in h.bodies() if isinstance(b, Body.Results)]
    assert isinstance(outcomes[0], Outcome.Err)
    assert "'note' must be string, not integer" in outcomes[0].error
    assert isinstance(outcomes[1], Outcome.Err)
    assert "'to' must be an open cursor" in outcomes[1].error
    assert outcomes[2] == Outcome.Ok("sent to cursor 1")
    assert h.session.cursors() == [h.cursor, 1]
    opened_calls = [
        part.args_raw
        for body in h.bodies(1)
        if isinstance(body, Body.Assistant)
        for part in body.parts
        if isinstance(part, Part.Call)
    ]
    assert opened_calls[-1] == '{"to": "new", "note": "x"}', "opened at the batch's node"
    await h.close()
