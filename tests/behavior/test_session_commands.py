"""The built-in `session` (D33): `/clear`, `/branch`, `/tree` and `/export`.

`/clear` hangs a new line under the walk's own `System` node (one `Config` first if the branch's
settings differ from the root's, then an undo marker), so the session keeps one root and the
next request starts with the bytes the first one did."""

from pathlib import Path

import pytest
from fakes import FAKE_CONFIG, FAKE_PROVIDER, FakeProvider, FakeTool, Reply
from harness import Harness, say, scenario
from shipped import shipped

from tau import Body, CommandFailed, Ending, Part, Stop, UndoMarker, model_config, read_models


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
        extensions=[shipped("session")],
        **kwargs,  # pyright: ignore[reportArgumentType]
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
    models = read_models(
        cli={
            "models": {
                "a": {"model": "a", "provider": FAKE_PROVIDER},
                "b": {"model": "b", "provider": FAKE_PROVIDER},
            }
        }
    )
    h = await session(tmp_path, "one", catalog=models, config=model_config("a"))
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
async def test_tree_draws_branches_and_cursors_as_text(tmp_path: Path) -> None:
    h = await session(tmp_path, "one", "two")
    await talk(h, "hello")
    await h.run("clear")
    await h.settle()
    await talk(h, "again")
    drawn = await h.run("tree")
    assert drawn is not None
    assert drawn.splitlines() == [
        "0 config: " + " ".join(FAKE_CONFIG.data.split()),
        "1 system: sys",
        "├─ 2 user: hello",
        "│  3 assistant: one",
        "└─ 4 undo: /clear",
        "   5 user: again",
        "   6 assistant: two  ◀ 0",
    ]
    below = await h.run("tree", {"at": 4})
    assert below is not None
    assert below.splitlines()[0] == "4 undo: /clear"
    await h.close()


@scenario
async def test_export_answers_with_markdown_or_writes_a_file(tmp_path: Path) -> None:
    h = await session(tmp_path, "the answer")
    await talk(h, "the question")
    text = await h.run("export")
    await h.settle()
    assert text is not None
    assert "the question" in text
    assert "the answer" in text
    target = tmp_path / "out.md"
    result = await h.run("export", {"path": str(target)})
    await h.settle()
    assert result is not None
    assert result.startswith("wrote ")
    assert target.read_text(encoding="utf-8") == text
    with pytest.raises(CommandFailed, match="exists: name another file"):
        await h.run("export", {"path": str(target)})
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
