"""A `pre_sample` hook says which tools one sample is offered (D2): `subject.tools`."""

from pathlib import Path

from fakes import FakeProvider, FakeTool, Reply
from harness import Harness, say, scenario

from tau import Cursor, Ending, Part, Stop, Subject


def tools() -> list[FakeTool]:
    return [FakeTool("read", "r"), FakeTool("write", "w"), FakeTool("bash", "b")]


class Offers:
    """Offers `names` at `pre_sample`, `times` samples long (then leaves the offer alone)."""

    name = "offers"

    def __init__(self, names: object, times: int = 1_000, point: str = "pre_sample") -> None:
        self.names, self.times = names, times
        setattr(self, point, self.offer)

    async def offer(self, cursor: Cursor, subject: Subject) -> None:
        if self.times:
            self.times -= 1
            subject.tools = self.names  # type: ignore[assignment]  # pyright: ignore[reportAttributeAccessIssue]


@scenario
async def test_a_sample_is_offered_the_tools_the_hook_names_in_the_drivers_order(
    tmp_path: Path,
) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([say("ok")]),
        tools=tools(),
        extensions=[Offers(["bash", "read"])],
    )
    await h.send("hi")
    await h.settle()
    assert h.provider.requests[0].tool_names == ["read", "bash"]
    await h.close()


@scenario
async def test_none_offers_every_tool_and_an_empty_list_none(tmp_path: Path) -> None:
    for names, offered in ((None, ["read", "write", "bash"]), ([], [])):
        h = await Harness.create(
            tmp_path / str(len(offered)),
            provider=FakeProvider([say("ok")]),
            tools=tools(),
            extensions=[Offers(names)],
        )
        await h.send("hi")
        await h.settle()
        assert h.provider.requests[0].tool_names == offered
        await h.close()


@scenario
async def test_the_offer_is_for_one_sample_only(tmp_path: Path) -> None:
    call = Reply([Part.Call("c", "read", "{}")], Stop.Tools)
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([call, say("done")]),
        tools=tools(),
        extensions=[Offers(["read"], times=1)],
    )
    await h.send("hi")
    await h.settle()
    assert [r.tool_names for r in h.provider.requests] == [
        ["read"],
        ["read", "write", "bash"],
    ]
    await h.close()


@scenario
async def test_a_name_that_is_not_a_tool_fails_the_step_before_any_sample(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([say("never")]),
        tools=tools(),
        extensions=[Offers(["read", "writ"])],
    )
    await h.send("hi")
    await h.settle()
    view = await h.view()
    assert isinstance(view.ending, Ending.Failed)
    assert view.hook_error is not None
    assert view.hook_error.point == "pre_sample"
    assert view.hook_error.extension == "offers"
    assert view.hook_error.error.startswith("ValueError: ")
    assert "unknown names ['writ']" in view.hook_error.error
    assert "['bash', 'read', 'write']" in view.hook_error.error, "it says what the tools are"
    assert h.provider.requests == []
    await h.close()


@scenario
async def test_a_bare_string_is_not_a_list_of_names(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path, tools=tools(), extensions=[Offers("read")], provider=FakeProvider([say("x")])
    )
    await h.send("hi")
    await h.settle()
    view = await h.view()
    assert view.hook_error is not None
    assert view.hook_error.error.startswith("TypeError: ")
    assert h.provider.requests == []
    await h.close()


@scenario
async def test_only_pre_sample_offers_tools(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        tools=tools(),
        extensions=[Offers(["read"], point="pre_reply")],
        provider=FakeProvider([say("x")]),
    )
    await h.send("hi")
    await h.settle()
    view = await h.view()
    assert view.hook_error is not None
    assert view.hook_error.point == "pre_reply"
    assert "only pre_sample says which tools a sample is offered" in view.hook_error.error
    await h.close()
