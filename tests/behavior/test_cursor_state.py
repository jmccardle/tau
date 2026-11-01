"""What an extension reads of its cursor (D3: the model) and shows of it (D4: `show`), and what
every client is sent of both in `CursorView`."""

from pathlib import Path

import pytest
from fakes import FAKE_MODEL, FakeProvider, Gate, Reply
from harness import Harness, scenario
from test_commands import two_models

from tau import (
    Author,
    Body,
    Config,
    Cursor,
    CursorGone,
    CursorView,
    NotInHook,
    Part,
    Stop,
    Subject,
)


async def look(cursor: Cursor) -> str:
    """Say which model this branch is on, and which the catalog has."""
    return f"{cursor.model()} of {list(cursor.models())}"


class Looker:
    name = "looker"
    commands = (look,)


# D3: the model.


@scenario
async def test_a_cursor_view_names_the_model_of_its_branch(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, catalog=two_models())
    other = await h.client.open(h.session.cursor(h.cursor).at)
    assert (await h.view()).model == FAKE_MODEL
    await h.run("model", {"model": "alt"})
    await h.settle()
    assert (await h.view()).model == "alt"
    assert (await h.view(other)).model == FAKE_MODEL, "a model belongs to the branch"
    await h.close()


@scenario
async def test_the_cursor_handle_reads_the_model_and_the_catalogs_names(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, catalog=two_models(), extensions=[Looker()])
    assert await h.run("look") == f"{FAKE_MODEL} of ['{FAKE_MODEL}', 'alt']"
    await h.settle()
    await h.run("model", {"model": "alt"})
    await h.settle()
    assert await h.run("look") == f"alt of ['{FAKE_MODEL}', 'alt']"
    await h.close()


@scenario
async def test_an_unreadable_config_has_no_model_in_the_view_and_fails_the_handle(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[Looker()])
    at = h.session.cursor(h.cursor).at
    broken = h.session.append(at, Body.Config(Config("not json")), Author.Tools(), 0)
    await h.client.go_to(h.cursor, broken)
    await h.settle()
    assert (await h.view()).model is None
    with pytest.raises(Exception, match="config data is not JSON"):
        await h.run("look")
    await h.close()


# D4: show.


async def shine(cursor: Cursor, text: str) -> None:
    """Show text."""
    cursor.show(text)


async def dim(cursor: Cursor) -> None:
    """Show nothing."""
    cursor.show(None)


async def wrong(cursor: Cursor) -> None:
    """Show what is not text."""
    cursor.show(3)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


class Badge:
    name = "badge"
    commands = (shine, dim, wrong)


async def flag_shine(cursor: Cursor, text: str) -> None:
    """Show text, as another extension."""
    cursor.show(text)


class Flag:
    name = "flag"
    commands = (flag_shine,)


@scenario
async def test_what_an_extension_shows_is_in_the_view_every_client_is_sent(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Badge()])
    await h.run("shine", {"text": "plan mode"})
    await h.settle()
    assert (await h.view()).shown == (("badge", "plan mode"),), "a client connecting now"
    shown = {e.shown for e in h.heard if isinstance(e, CursorView) and e.shown}
    assert shown == {(("badge", "plan mode"),)}, "a client that was connected"
    await h.close()


@scenario
async def test_a_later_show_replaces_and_none_clears(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Badge()])
    await h.run("shine", {"text": "one"})
    await h.settle()
    await h.run("shine", {"text": "two"})
    await h.settle()
    assert (await h.view()).shown == (("badge", "two"),)
    await h.run("dim")
    await h.settle()
    assert (await h.view()).shown == ()
    await h.close()


@scenario
async def test_each_extension_has_one_line_per_cursor_in_name_order(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Flag(), Badge()])
    other = await h.client.open(h.session.cursor(h.cursor).at)
    await h.settle()
    await h.run("shine", {"text": "b"})
    await h.settle()
    await h.run("flag_shine", {"text": "f"})
    await h.settle()
    assert (await h.view()).shown == (("badge", "b"), ("flag", "f"))
    assert (await h.view(other)).shown == (), "it is shown on the cursor that showed it"
    await h.run("dim")
    await h.settle()
    assert (await h.view()).shown == (("flag", "f"),), "one extension's None leaves the other's"
    await h.close()


@scenario
async def test_show_is_not_persisted(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Badge()])
    await h.run("shine", {"text": "plan mode"})
    await h.settle()
    before = h.snapshot().tree
    assert (await h.view()).shown == (("badge", "plan mode"),)
    assert not h.session.cursor(h.cursor).items, "not a scratchpad item"
    await h.crash()
    assert h.snapshot().tree == before, "not a node"
    assert (await h.view()).shown == ()
    await h.close()


@scenario
async def test_closing_a_cursor_drops_what_it_showed(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Badge()])
    second = await h.client.open(h.session.cursor(h.cursor).at)
    await h.settle()
    await h.run("shine", {"text": "mine"}, cursor=second)
    await h.settle()
    assert (await h.view(second)).shown == (("badge", "mine"),)
    await h.client.close_cursor(second)
    await h.settle()
    assert any(isinstance(e, CursorGone) and e.cursor == second for e in h.heard)
    again = await h.client.reopen(second)
    await h.settle()
    assert (await h.view(again)).shown == (), "a reopened cursor shows nothing"
    await h.close()


@scenario
async def test_show_takes_text_or_none_and_a_retired_handle_cannot_show(tmp_path: Path) -> None:
    kept: list[Cursor] = []

    class Keeps:
        name = "keeps"

        async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
            kept.append(cursor)

    h = await Harness.create(tmp_path, extensions=[Badge(), Keeps()])
    with pytest.raises(Exception, match="TypeError: show takes a str or None, not int"):
        await h.run("wrong")
    await h.settle()
    await h.step()
    await h.settle()
    with pytest.raises(NotInHook):
        kept[-1].show("late")
    await h.close()


@scenario
async def test_a_show_in_a_hook_is_seen_while_the_step_runs(tmp_path: Path) -> None:
    gate = Gate()

    class Working:
        name = "working"

        async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
            cursor.show("working")

        async def post_step(self, cursor: Cursor, subject: Subject) -> None:
            cursor.show(None)

    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Text("hi")], Stop.End, gate=gate)]),
        extensions=[Working()],
    )
    await h.send("go")
    await h.settle()
    view = await h.view()
    assert view.busy
    assert view.shown == (("working", "working"),)
    gate.release()
    await h.settle()
    assert (await h.view()).shown == ()
    await h.close()
