"""Typed questions (D16 section 3): `ask(text, expect)` returns the checked, converted answer."""

from pathlib import Path
from typing import Literal

import pytest
from fakes import FakeProvider, FakeTool, Reply
from harness import Harness, scenario

from tau_next import (
    Author,
    Body,
    CallDraft,
    Cursor,
    CursorId,
    DefinitionError,
    Model,
    NodeHandle,
    Outcome,
    Part,
    Stop,
    Subject,
)


def calls(*ids: str) -> Reply:
    return Reply([Part.Call(i, f"t{i}", "{}") for i in ids], Stop.Tools)


# D16 section 6, second case: a typed question from a tool hook.


class Permissions:
    name = "permissions"

    def __init__(self) -> None:
        self.always: set[str] = set()

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        if call.tool in self.always:
            return None
        verdict = await node.ask(f"{call.tool} {call.args_raw}", Literal["allow", "deny", "always"])
        if verdict == "always":
            self.always.add(call.tool)
        return None if verdict in ("allow", "always") else "the user said no"


async def permissions_harness(tmp_path: Path, script: list[Reply]) -> tuple[Harness, FakeTool]:
    tool = FakeTool("ta", "ran")
    h = await Harness.create(
        tmp_path, provider=FakeProvider(script), tools=[tool], extensions=[Permissions()]
    )
    await h.send("go")
    await h.settle()
    return h, tool


@scenario
async def test_a_tool_hook_asks_on_its_batchs_cursor(tmp_path: Path) -> None:
    h, tool = await permissions_harness(tmp_path, [calls("a"), Reply([Part.Text("done")])])
    (question,) = (await h.view()).questions
    assert question.text == "ta {}"
    assert question.to is None
    assert question.schema == {"type": "string", "enum": ["allow", "deny", "always"]}
    assert tool.calls == [], "the tool waits for the answer"
    await h.respond("allow")
    await h.settle()
    assert tool.calls == [{}]
    assert h.bodies()[-2] == Body.Results([("a", Outcome.Ok("ran"), Author.Tools())])
    await h.close()


@scenario
async def test_deny_is_the_hooks_refusal(tmp_path: Path) -> None:
    h, tool = await permissions_harness(tmp_path, [calls("a"), Reply([Part.Text("done")])])
    await h.respond("deny")
    await h.settle()
    assert tool.calls == []
    assert h.bodies()[-2] == Body.Results(
        [("a", Outcome.Err("the user said no"), Author.Extension("permissions"))]
    )
    await h.close()


@scenario
async def test_always_is_asked_once_per_tool(tmp_path: Path) -> None:
    h, tool = await permissions_harness(
        tmp_path, [calls("a"), calls("a"), Reply([Part.Text("done")])]
    )
    await h.respond("always")
    await h.settle()
    assert (await h.view()).questions == (), "the second call is not asked"
    assert tool.calls == [{}, {}]
    await h.close()


@scenario
async def test_a_wrong_answer_is_refused_and_the_question_stays_open(tmp_path: Path) -> None:
    h, tool = await permissions_harness(tmp_path, [calls("a"), Reply([Part.Text("done")])])
    with pytest.raises(ValueError, match="must be one of"):
        await h.respond("maybe")
    with pytest.raises(ValueError, match=r"must be one of .*, not 3"):
        await h.respond(3)
    assert [q.text for q in (await h.view()).questions] == ["ta {}"]
    assert tool.calls == []
    await h.respond("allow")
    await h.settle()
    assert tool.calls == [{}]
    with pytest.raises(LookupError):
        await h.respond("allow")
    await h.close()


@scenario
async def test_declining_answers_none_whatever_was_expected(tmp_path: Path) -> None:
    h, tool = await permissions_harness(tmp_path, [calls("a"), Reply([Part.Text("done")])])
    await h.respond(None)
    await h.settle()
    assert tool.calls == []
    assert h.bodies()[-2] == Body.Results(
        [("a", Outcome.Err("the user said no"), Author.Extension("permissions"))]
    )
    await h.close()


# Other types


class Asks:
    """A `pre_step` hook that asks one question and records what came back."""

    name = "asks"

    def __init__(self, text: str, expect: object, to: str | None = None) -> None:
        self.question = (text, expect, to)
        self.answers: list[object] = []
        self.refusals: list[Exception] = []

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        text, expect, to = self.question
        try:
            self.answers.append(await cursor.ask(text, expect, to=to))
        except DefinitionError as e:
            self.refusals.append(e)
        return None


async def asked(tmp_path: Path, asks: Asks) -> Harness:
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("ok")])]), extensions=[asks]
    )
    await h.send("hi")
    await h.settle()
    return h


@scenario
async def test_an_integer_answer_is_checked_and_returned(tmp_path: Path) -> None:
    asks = Asks("how many?", int)
    h = await asked(tmp_path, asks)
    with pytest.raises(ValueError, match="must be integer, not string"):
        await h.respond("3")
    await h.respond(3)
    await h.settle()
    assert asks.answers == [3]
    await h.close()


@scenario
async def test_a_path_answer_arrives_as_a_path_and_to_is_kept(tmp_path: Path) -> None:
    asks = Asks("which file?", list[Path], to="alice@laptop:tui")
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("ok")])]), extensions=[asks]
    )
    alice = await h.connect("alice@laptop:tui")
    await h.send("hi")
    await h.settle()
    (question,) = (await h.view()).questions
    assert question.to == "alice@laptop:tui"
    assert question.schema == {"type": "array", "items": {"type": "string", "format": "path"}}
    await alice.answer(question.id, ["a.txt", "dir/b.txt"])
    await h.settle()
    assert asks.answers == [[Path("a.txt"), Path("dir/b.txt")]]
    await h.close()


@scenario
async def test_a_model_answer_must_be_configured(tmp_path: Path) -> None:
    asks = Asks("which model?", Model)
    h = await asked(tmp_path, asks)
    with pytest.raises(ValueError, match="configured model, not 'gpt-9'"):
        await h.respond("gpt-9")
    await h.respond("fake")
    await h.settle()
    assert asks.answers == ["fake"]
    await h.close()


@scenario
async def test_a_new_cursor_answer_opens_a_cursor_at_the_asking_cursors_node(
    tmp_path: Path,
) -> None:
    asks = Asks("where to?", CursorId)
    h = await asked(tmp_path, asks)
    with pytest.raises(ValueError, match="open cursor"):
        await h.respond(7)
    assert h.session.cursors() == [h.cursor]
    at = h.session.cursor(h.cursor).at
    await h.respond("new")
    await h.settle()
    assert asks.answers == [1]
    assert h.session.cursors() == [h.cursor, 1]
    assert h.bodies(1)[:2] == h.bodies(h.cursor)[:2], f"the walk to node {at}"
    await h.close()


@scenario
async def test_what_is_not_in_the_language_cannot_be_asked_for(tmp_path: Path) -> None:
    asks = Asks("a dict?", dict[str, str])
    h = await asked(tmp_path, asks)
    assert len(asks.refusals) == 1
    assert "dict" in str(asks.refusals[0])
    assert (await h.view()).questions == ()
    await h.close()


@scenario
async def test_a_question_to_anyone_is_declined_at_once_when_no_client_is_connected(
    tmp_path: Path,
) -> None:
    tool = FakeTool("ta", "ran")
    provider = FakeProvider([calls("a"), Reply([Part.Text("done")])])
    h = await Harness.create(tmp_path, provider=provider, tools=[tool], extensions=[Permissions()])
    await h.client.close()  # headless: nobody can answer
    await h.send("go")
    await h.settle()
    assert tool.calls == []
    assert h.bodies()[-2] == Body.Results(
        [("a", Outcome.Err("the user said no"), Author.Extension("permissions"))]
    )
    assert not h.session.cursor(h.cursor).busy
    await h.close()
