"""A `Notice` says who spoke, about which cursor, and how loud (D32 section 2): the handle fills
in `cursor` and `extension`, never the caller; `level` is `info`, `warning` or `error`."""

from pathlib import Path

from fakes import FakeProvider, FakeTool, Reply
from harness import Harness, say, scenario

from tau import (
    CallDraft,
    Cursor,
    NodeHandle,
    Notice,
    Part,
    Progress,
    Stop,
    Subject,
    TreeHandle,
)


class Teller:
    """Tells at `pre_step`, and records a bad level's error."""

    name = "teller"

    def __init__(self) -> None:
        self.refused: list[str] = []

    async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
        cursor.tell("plain")
        cursor.tell("careful", level="warning")
        try:
            cursor.tell("loud", level="shout")  # pyright: ignore[reportArgumentType]
        except ValueError as e:
            self.refused.append(str(e))


@scenario
async def test_a_cursor_handles_tell_is_about_its_cursor_from_its_extension(tmp_path: Path) -> None:
    teller = Teller()
    h = await Harness.create(tmp_path, provider=FakeProvider([say("ok")]), extensions=[teller])
    await h.send("go")
    await h.settle()
    assert h.notice_events() == [
        Notice("plain", h.cursor, "teller", "info"),
        Notice("careful", h.cursor, "teller", "warning"),
    ]
    levels = "['info', 'warning', 'error']"
    assert teller.refused == [f"a notice's level is one of {levels}, not 'shout'"]
    await h.close()


class ToolTeller:
    name = "tool_teller"

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> None:
        node.tell(f"running {call.tool}", level="error")


@scenario
async def test_a_node_handles_tell_is_about_the_batchs_cursor(tmp_path: Path) -> None:
    provider = FakeProvider([Reply([Part.Call("c1", "echo", "{}")], Stop.Tools), say("done")])
    h = await Harness.create(
        tmp_path, provider=provider, tools=[FakeTool("echo", "e")], extensions=[ToolTeller()]
    )
    await h.send("go")
    await h.settle()
    assert h.notice_events() == [Notice("running echo", h.cursor, "tool_teller", "error")]
    await h.close()


async def report(tree: TreeHandle, progress: Progress) -> str:
    """Report progress."""
    progress("halfway")
    return "done"


class Reporter:
    name = "reporter"
    commands = (report,)


@scenario
async def test_a_commands_progress_is_an_info_notice_from_its_owner_about_its_cursor(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Reporter()])
    assert await h.run("report", {}) == "done"
    await h.settle()
    assert h.notice_events() == [Notice("halfway", h.cursor, "reporter", "info")]
    await h.close()


class Guard:
    name = "guard"

    async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
        return "no thanks"


@scenario
async def test_a_halted_input_tells_its_author_alone_naming_the_hook_and_error(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Guard()])
    other = await h.connect("other")
    await h.send("hi")
    await h.settle()
    assert h.notice_events() == [Notice("no thanks", h.cursor, "guard", "error")]
    await other.close()
    await h.close()
