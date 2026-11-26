"""The built-in `session` (D33): `/clear`, `/branch`, `/tree` and `/export`.

`/clear` hangs a new line under the end of the walk's system prefix (one `Config` first if the
branch's settings differ from the prefix's, then an undo marker), so the session keeps one root
and the next request starts with the bytes the first one did."""

from pathlib import Path

import pytest
from fakes import FAKE_PROVIDER, FakeProvider, FakeTool, Reply, Request
from harness import Harness, say, scenario
from shipped import shipped
from world import World, loaded

from tau import (
    Body,
    CommandFailed,
    Ending,
    Part,
    Skills,
    Stop,
    UndoMarker,
    model_config,
    read_models,
)

MODELS = read_models(
    cli={
        "models": {
            "a": {"model": "a", "provider": FAKE_PROVIDER},
            "b": {"model": "b", "provider": FAKE_PROVIDER},
        }
    }
)


def at(h: Harness, cursor: int | None = None) -> int:
    return h.session.cursor(h.cursor if cursor is None else cursor).at


async def talk(h: Harness, *texts: str) -> None:
    for text in texts:
        await h.send(text)
        await h.settle()


async def session(tmp_path: Path, *texts: str, **kwargs: object) -> Harness:
    return await Harness.create(
        tmp_path,
        provider=FakeProvider([say(t) for t in texts]),
        **{"extensions": [shipped("session")], **kwargs},  # pyright: ignore[reportArgumentType]
    )


@scenario
async def test_clear_hangs_a_new_line_under_the_system_node_and_rests(tmp_path: Path) -> None:
    h = await session(tmp_path, "one", "two")
    await talk(h, "hello")
    before = at(h)
    result = await h.run("clear")
    await h.settle()
    assert result is not None
    assert result.startswith("cleared: a new line under the system prompt")
    walk = h.session.tree.walk(at(h))
    assert [type(n.body) for n in walk] == [Body.Config, Body.System, Body.Custom]
    assert walk[2].parent == 1, "under the System node the session already has"
    assert UndoMarker.of(walk[2].body) == UndoMarker("/clear", before, "clear")
    assert sum(n.parent is None for n in h.nodes()) == 1, "one root"
    view = await h.view()
    assert not view.busy
    assert len(h.provider.requests) == 1, "clearing asks the model nothing"
    await talk(h, "hello")
    first, second = h.provider.requests
    assert second.walk[:2] == first.walk[:2], "the same root and System node: a warm cache"
    await h.close()


@scenario
async def test_clear_keeps_the_branch_settings_in_one_config(tmp_path: Path) -> None:
    h = await session(tmp_path, "one", catalog=MODELS, config=model_config("a"))
    await h.run("model", {"model": "b"})
    await h.settle()
    await talk(h, "hello")
    await h.run("clear")
    await h.settle()
    walk = h.session.tree.walk(at(h))
    assert [type(n.body) for n in walk] == [Body.Config, Body.System, Body.Config, Body.Custom]
    config = walk[2].body
    assert isinstance(config, Body.Config)
    assert config.config == model_config("b"), "the folded settings, which differ from the root"
    assert walk[2].parent == 1
    await h.close()


@scenario
async def test_the_request_after_clear_is_the_one_a_new_session_sends(
    tmp_path: Path, world: World
) -> None:
    """A model chosen before the first message puts a `Config` inside the system prefix, between
    the session's `System` node and the skills list. `/clear` keeps the whole prefix."""
    (world.tau / "skills" / "pdf").mkdir(parents=True)
    (world.tau / "skills" / "pdf" / "SKILL.md").write_text("---\ndescription: d\n---\nb")
    h = await session(
        tmp_path,
        "one",
        "two",
        catalog=MODELS,
        config=model_config("a"),
        extensions=[shipped("session"), Skills.of(loaded(world))],
        tools=[FakeTool("read", "contents")],
    )
    await h.run("model", {"model": "b"})
    await h.settle()
    await talk(h, "hello")
    await h.run("clear")
    await h.settle()
    await talk(h, "hello")
    first, after = h.provider.requests

    def sent(request: Request) -> list[str]:
        bodies = [h.session.tree.node(i).body for i in request.walk]
        return [b.text for b in bodies if isinstance(b, (Body.System, Body.User))]

    assert len(sent(first)) == 3, "the session's System node, the skills list, the message"
    assert sent(after) == sent(first)
    assert after.model == first.model
    await h.close()


@scenario
async def test_clear_with_nothing_said_writes_nothing(tmp_path: Path) -> None:
    h = await session(tmp_path)
    count = len(h.nodes())
    assert await h.run("clear") == "nothing to clear"
    await h.settle()
    assert len(h.nodes()) == count
    await h.close()


@scenario
async def test_branch_opens_a_labelled_cursor_on_this_node(tmp_path: Path) -> None:
    h = await session(tmp_path, "one")
    await talk(h, "hello")
    here = at(h)
    result = await h.run("branch", {"label": "try"})
    await h.settle()
    new = h.session.label("try")
    assert result == f"opened cursor {new} (try) at node {here}"
    assert at(h, new) == here
    assert at(h) == here, "this cursor stays where it is"
    await h.close()


@scenario
async def test_branch_refuses_a_label_another_cursor_has(tmp_path: Path) -> None:
    """Moving the label would leave the cursor that has it unnamed: nothing is opened."""
    h = await session(tmp_path, "one")
    await talk(h, "hello")
    await h.run("branch", {"label": "try"})
    await h.settle()
    first = h.session.label("try")
    open_before = h.session.cursors()
    with pytest.raises(CommandFailed, match=f"cursor {first} is labelled 'try': choose another"):
        await h.run("branch", {"label": "try"})
    await h.settle()
    assert h.session.cursors() == open_before, "no half-done branch"
    assert h.session.label("try") == first
    await h.close()


@scenario
async def test_tree_draws_branches_and_cursors_as_text(tmp_path: Path) -> None:
    h = await session(tmp_path, "one", "two")
    await talk(h, "hello")
    await h.run("clear")
    await h.settle()
    await talk(h, "again")
    drawn = await h.run("tree")
    assert drawn is not None
    assert drawn.splitlines() == [
        "0 Config · model fake",
        "1 System",
        "├─ 2 User · hello",
        "│  3 Assistant · one",
        "└─ 4 Custom · undo /clear",
        "   5 User · again",
        "   6 Assistant · two  ◀ 0",
    ]
    below = await h.run("tree", {"at": 4})
    assert below is not None
    assert below.splitlines()[0] == "4 Custom · undo /clear"
    await h.close()


@scenario
async def test_export_answers_with_markdown_or_writes_a_file(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    h = await session(tmp_path, "the answer", extensions=[shipped("session", work)])
    await talk(h, "the question")
    text = await h.run("export")
    await h.settle()
    assert text is not None
    assert "the question" in text
    assert "the answer" in text
    result = await h.run("export", {"path": "out.md"})
    await h.settle()
    assert result is not None
    assert result.startswith("wrote ")
    assert (work / "out.md").read_text(encoding="utf-8") == text, "in the working directory"
    await h.settle()
    with pytest.raises(CommandFailed, match=r"out\.md exists: name another file"):
        await h.run("export", {"path": "out.md"})
    await h.close()


@scenario
async def test_export_takes_an_absolute_path_as_given(tmp_path: Path) -> None:
    """A path is the filesystem's: where the server's user may write, `/export` writes."""
    work = tmp_path / "work"
    work.mkdir()
    h = await session(tmp_path, "the answer", extensions=[shipped("session", work)])
    await talk(h, "the question")
    target = tmp_path / "elsewhere.md"
    result = await h.run("export", {"path": str(target)})
    await h.settle()
    assert result is not None
    assert result.startswith("wrote ")
    assert "the question" in target.read_text(encoding="utf-8")
    await h.close()


@scenario
async def test_export_names_a_path_it_cannot_write(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    h = await session(tmp_path, "the answer", extensions=[shipped("session", work)])
    await talk(h, "the question")
    with pytest.raises(CommandFailed, match=r"cannot write missing/x\.md: No such file or direc"):
        await h.run("export", {"path": "missing/x.md"})
    await h.settle()
    view = await h.view()
    assert not isinstance(view.ending, Ending.Failed), "a refusal, not a failed step"
    await h.close()


@scenario
async def test_a_command_run_where_the_turn_would_go_on_halts_its_step(tmp_path: Path) -> None:
    """A cursor resting mid-turn (gone back to a batch) is not stepped on by `/export`."""
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Call("c", "work", "{}")], Stop.Tools), say("done")]),
        tools=[FakeTool("work", "ok")],
        extensions=[shipped("session")],
    )
    h.session.set_auto(h.cursor, True)
    await talk(h, "go")
    batch = next(n.id for n in h.session.tree.walk(at(h)) if isinstance(n.body, Body.Assistant))
    await h.go_to(batch)
    await h.settle()
    requests, nodes = len(h.provider.requests), len(h.nodes())
    await h.run("export")
    await h.settle()
    assert (len(h.provider.requests), len(h.nodes())) == (requests, nodes), "nothing ran"
    view = await h.view()
    assert isinstance(view.ending, Ending.Halted)
    assert view.ending.reason.startswith(f"/export left the cursor at node {batch}, mid-turn")
    await h.close()


@scenario
async def test_clear_refuses_a_branch_with_no_system_prompt(tmp_path: Path) -> None:
    h = await session(tmp_path)
    root = h.session.append_root(model_config("fake:m"), h.session.tree.node(0).by, 0)
    await h.go_to(root)
    await h.settle()
    with pytest.raises(CommandFailed, match="no system prompt"):
        await h.run("clear")
    await h.close()
