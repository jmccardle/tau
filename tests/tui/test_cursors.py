"""Several cursors under one app: the cursor bar, switching the viewed cursor, labels, editing the
last message, and taking back a pending line (D23 sections 2 and 10, 8C-2)."""

from pathlib import Path

from fakes import FakeProvider, FakeTool, Gate, Reply
from harness import Harness, scenario
from textual.app import App
from textual.widgets import Tree
from tuikit import Gatekeeper, answer, running, say, shows, type_line, until

from tau import Attachments, Author, Body, ItemData, Part, Stop, TextDelta

SYS = 1  # the system node: a new session is Config 0, System 1


def users(h: Harness) -> list[tuple[int | None, str, Author]]:
    return [(n.parent, n.body.text, n.by) for n in h.nodes() if isinstance(n.body, Body.User)]


@scenario
async def test_the_cursor_bar_appears_with_a_second_cursor_and_shows_labels(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path) as (app, pilot):
        assert "[0]" not in app.screen_text(), "one cursor needs no tabs"
        other = await h.client.open(SYS)
        await h.set_auto(False, other)
        await shows(pilot, app, "[0] 1 ")
        await h.client.set_label(other, "scout")
        await shows(pilot, app, "[0] 1 scout ")
        await h.client.set_label(h.cursor, "main")
        await shows(pilot, app, "[0 main] 1 scout ")
        await h.client.close_cursor(other)
        await until(pilot, lambda: "scout" not in app.screen_text())  # a closed cursor has no tab
    await h.close()


@scenario
async def test_a_tab_marks_a_working_cursor_and_an_open_question(tmp_path: Path) -> None:
    gate = Gatekeeper()
    script = [Reply([Part.Call("a", "ta", "{}")], Stop.Tools), say("done")]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        tools=[FakeTool("ta", "ran")],
        extensions=[gate],
    )
    async with running(h, tmp_path) as (app, pilot):
        other = await h.client.open(SYS)
        await h.client.send(other, "go")
        await shows(pilot, app, "1 …?")
        assert "run ta?" not in app.screen_text(), "its question shows when it is viewed"
    await h.close()


@scenario
async def test_alt_arrows_view_the_next_cursor_and_its_walk(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first"), say("second")]))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "one")
        await pilot.press("enter")
        await shows(pilot, app, "first")
        scout = await h.client.open(SYS)
        await h.set_auto(False, scout)
        await shows(pilot, app, "[0] 1 ")
        await pilot.press("alt+right")
        await shows(pilot, app, " 0 [1]")
        assert "[User] one" not in app.screen_text(), "cursor 1 is at the system prompt"
        assert app.cursor == scout
        await type_line(pilot, "two")
        await pilot.press("enter")
        await shows(pilot, app, "second")
        assert [(p, t) for p, t, _ in users(h)] == [(SYS, "one"), (SYS, "two")]
        await pilot.press("alt+left")
        await shows(pilot, app, "[User] one")
        assert app.cursor == h.cursor
        assert "second" not in app.screen_text()
    await h.close()


@scenario
async def test_switching_watches_the_new_cursor_and_its_sample_in_flight(tmp_path: Path) -> None:
    gate = Gate()
    reply = Reply(
        [TextDelta("par"), TextDelta("tial"), Part.Text("partial")], gate=gate, gate_after=2
    )
    h = await Harness.create(tmp_path, provider=FakeProvider([reply]))
    async with running(h, tmp_path) as (app, pilot):
        scout = await h.client.open(SYS)
        await h.client.send(scout, "go")
        await shows(pilot, app, "1 …")
        assert "[Assistant, live]" not in app.screen_text(), "an unwatched cursor streams nothing"
        await pilot.press("alt+right")
        await shows(pilot, app, "[Assistant, live] partial")
        await pilot.press("alt+left")
        await shows(pilot, app, " 1 …")
        assert "[Assistant, live]" not in app.screen_text()
        gate.release()
        await pilot.press("alt+right")
        await shows(pilot, app, "[Assistant] partial")
        assert "[Assistant, live]" not in app.screen_text()
    await h.close()


@scenario
async def test_a_question_on_another_cursor_shows_when_that_cursor_is_viewed(
    tmp_path: Path,
) -> None:
    gate = Gatekeeper()
    script = [Reply([Part.Call("a", "ta", "{}")], Stop.Tools), say("done")]
    h = await Harness.create(
        tmp_path, provider=FakeProvider(script), tools=[FakeTool("ta", "ran")], extensions=[gate]
    )
    async with running(h, tmp_path) as (app, pilot):
        scout = await h.client.open(SYS)
        await h.client.send(scout, "go")
        await shows(pilot, app, "1 …?")
        await pilot.press("alt+right")
        await shows(pilot, app, "run ta?")
        await answer(pilot, app)  # the switch draws that cursor's walk again
        await shows(pilot, app, "done")
        assert gate.verdicts == ["allow"]
        await pilot.press("alt+left")
        await until(pilot, lambda: "run ta?" not in app.screen_text())
    await h.close()


@scenario
async def test_a_single_cursor_has_nowhere_to_go(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path) as (app, pilot):
        await pilot.press("alt+right")
        await until(pilot, lambda: "there is only one cursor" in app.toasts)
        assert app.cursor == h.cursor
    await h.close()


@scenario
async def test_closing_the_viewed_cursor_moves_the_view_to_another(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path) as (app, pilot):
        scout = await h.client.open(SYS)
        await h.set_auto(False, scout)
        await shows(pilot, app, "[0] 1 ")
        await pilot.press("alt+right")
        await shows(pilot, app, " 0 [1]")
        await h.client.close_cursor(scout)
        await until(pilot, lambda: app.cursor == h.cursor)
        assert any("was closed" in t for t in app.toasts)
    await h.close()


# Ctrl+Z: edit the last message


@scenario
async def test_ctrl_z_goes_before_the_last_message_and_puts_its_text_in_the_editor(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first"), say("second")]))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "one")
        await pilot.press("enter")
        await shows(pilot, app, "first")
        await pilot.press("ctrl+z")
        await until(pilot, lambda: app.draft == "one")
        await until(pilot, lambda: "[User] one" not in app.screen_text())
        assert "first" not in app.screen_text()
        assert (await h.view()).at == SYS
        await type_line(pilot, " again")
        await pilot.press("enter")
        await shows(pilot, app, "second")
        assert [(p, t) for p, t, _ in users(h)] == [(SYS, "one"), (SYS, "one again")], (
            "the old turn stays in the tree as a branch"
        )
    await h.close()


@scenario
async def test_ctrl_z_puts_back_what_was_typed_so_resending_attaches_each_file_once(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("alpha", encoding="utf-8")
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([say("first"), say("second")]),
        extensions=[Attachments(work)],
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "explain @a.txt")
        await pilot.press("enter")
        await shows(pilot, app, "first")
        assert "[User] explain @a.txt" in app.screen_text(), "the transcript shows the line"
        assert "<attachment" not in app.screen_text(), "not the files a hook inlined"
        await pilot.press("ctrl+z")
        await until(pilot, lambda: app.draft == "explain @a.txt")
        await pilot.press("enter")
        await shows(pilot, app, "second")
        sent = [text for _, text, _ in users(h)]
        assert [t.count("<attachment") for t in sent] == [1, 1]
    await h.close()


@scenario
async def test_ctrl_z_with_no_message_says_so(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path) as (app, pilot):
        await pilot.press("ctrl+z")
        await until(pilot, lambda: "there is no message to edit" in app.toasts)
        assert app.draft == ""
    await h.close()


@scenario
async def test_ctrl_z_on_a_busy_cursor_asks_before_aborting(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("slow")], gate=gate)])
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        await pilot.press("ctrl+z")
        await until(pilot, lambda: bool(app.screen.query("#confirm-dialog")))
        await pilot.click("#confirm-no")
        await until(pilot, lambda: not app.screen.query("#confirm-dialog"))
        assert (await h.view()).busy, "No leaves the step running"
        assert app.draft == ""
        await pilot.press("ctrl+z")
        await until(pilot, lambda: bool(app.screen.query("#confirm-dialog")))
        await pilot.click("#confirm-yes")
        await until(pilot, lambda: app.draft == "go")
        # The abort cancels the sample as a task: the step has ended a pass of the loop later.
        await until(pilot, lambda: not h.session.cursor(h.cursor).busy)
        assert (await h.view()).at == SYS
    await h.close()


# Taking a pending line back


@scenario
async def test_up_on_an_empty_editor_takes_back_the_pending_line(tmp_path: Path) -> None:
    gate = Gate()
    script = [Reply([Part.Text("one")], gate=gate), say("two")]
    h = await Harness.create(tmp_path, provider=FakeProvider(script))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        await type_line(pilot, "wait, no")
        await pilot.press("enter")
        await shows(pilot, app, "> wait, no")
        await pilot.press("up")
        await until(pilot, lambda: app.draft == "wait, no")
        await until(pilot, lambda: "> wait, no" not in app.screen_text())
        assert (await h.view()).items == ()
        gate.release()
        await shows(pilot, app, "[Assistant] one")
        assert [t for _, t, _ in users(h)] == ["go"], "the line was never sent"
    await h.close()


@scenario
async def test_up_does_not_take_back_another_clients_line(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("one")], gate=gate), say("two")])
    )
    other = await h.connect("me:other")
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        await other.input(h.cursor, "theirs")
        await shows(pilot, app, "[me:other] theirs")
        await pilot.press("up")
        await until(pilot, lambda: app.draft == "go")  # the history, not their line
        assert [
            i.data.text for i in (await h.view()).items if isinstance(i.data, ItemData.Input)
        ] == ["theirs"]
        gate.release()
    await other.close()
    await h.close()


@scenario
async def test_a_line_the_cursor_took_is_not_taken_back(tmp_path: Path) -> None:
    gate = Gate()
    script = [Reply([Part.Text("one")], gate=gate), say("two")]
    h = await Harness.create(tmp_path, provider=FakeProvider(script))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        await type_line(pilot, "late")
        await pilot.press("enter")
        await shows(pilot, app, "> late")
        gate.release()  # the step ends and takes the line
        await shows(pilot, app, "[User] late")
        await pilot.press("up")
        await until(pilot, lambda: app.draft == "late")  # the history again, no reclaim
        assert [t for _, t, _ in users(h)] == ["go", "late"], "the node stays"
    await h.close()


# The tree screen


def tree_rows(app: App[None]) -> list[str]:
    """The tree screen's rows as text, in order (a folded branch's rows are listed too)."""
    rows: list[str] = []
    stack = list(reversed(app.screen.query_one(Tree).root.children))
    while stack:
        node = stack.pop()
        rows.append(str(node.label))
        stack.extend(reversed(node.children))
    return rows


@scenario
async def test_ctrl_g_shows_the_nodes_with_the_cursor_marked(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first")]))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "one")
        await pilot.press("enter")
        await shows(pilot, app, "first")
        await h.client.set_label(h.cursor, "main")
        await shows(pilot, app, "[0 main]")
        await pilot.press("ctrl+g")
        await until(pilot, lambda: bool(app.screen.query(Tree)))
        rows = tree_rows(app)
        assert rows[0].startswith("config:")
        assert any(r.startswith("system:") for r in rows)
        assert any(r.startswith("user: one") for r in rows)
        assert rows[-1] == "assistant: first  ◀ [0] main", "the viewed cursor's mark and label"
        await pilot.press("escape")
        await until(pilot, lambda: not app.screen.query(Tree))
        assert (await h.view()).at == len(h.nodes()) - 1, "leaving moved nothing"
    await h.close()


@scenario
async def test_enter_in_the_tree_goes_to_the_highlighted_node(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first"), say("second")]))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "one")
        await pilot.press("enter")
        await shows(pilot, app, "first")
        await pilot.press("ctrl+g")
        await until(pilot, lambda: bool(app.screen.query(Tree)))
        rows = tree_rows(app)
        for _ in range(len(rows) - 1 - rows.index(next(r for r in rows if r.startswith("system")))):
            await pilot.press("up")
        await pilot.pause()
        await pilot.press("enter")
        await until(pilot, lambda: not app.screen.query(Tree))
        await until(pilot, lambda: "[User] one" not in app.screen_text())
        assert (await h.view()).at == SYS
        await type_line(pilot, "two")
        await pilot.press("enter")
        await shows(pilot, app, "second")
        assert [(p, t) for p, t, _ in users(h)] == [(SYS, "one"), (SYS, "two")]
    await h.close()


@scenario
async def test_o_in_the_tree_opens_a_cursor_at_the_node(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first")]))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "one")
        await pilot.press("enter")
        await shows(pilot, app, "first")
        await pilot.press("ctrl+g")
        await until(pilot, lambda: bool(app.screen.query(Tree)))
        await pilot.press("up")  # the node before the cursor's: the user message
        await pilot.pause()
        await pilot.press("o")
        await shows(pilot, app, "[0] 1 ")
        views = [e for e in h.heard if getattr(e, "cursor", None) == 1]
        assert views, "the session has a second cursor"
        assert (await h.view(1)).at == 2, "at the user message"
        assert app.cursor == h.cursor, "opening does not move the view"
    await h.close()


@scenario
async def test_l_in_the_tree_labels_the_viewed_cursor(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path) as (app, pilot):
        await pilot.press("ctrl+g")
        await until(pilot, lambda: bool(app.screen.query(Tree)))
        await pilot.press("l")
        await until(pilot, lambda: bool(app.screen.query("#text-dialog")))
        await type_line(pilot, "mine")
        await pilot.press("enter")
        await shows(pilot, app, "[0 mine]")
        assert [name for name, _ in h.session.labels()] == ["mine"]
    await h.close()


# Moves that wait on a dialog hold on to the cursor they began on


@scenario
async def test_ctrl_z_types_as_undo_and_edits_only_on_an_empty_editor(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first")]))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "one")
        await pilot.press("enter")
        await shows(pilot, app, "first")
        at = (await h.view()).at
        await type_line(pilot, "half typed")
        await pilot.pause()
        await pilot.press("ctrl+z")
        await until(pilot, lambda: app.draft != "half typed")
        assert (await h.view()).at == at, "undo is the editor's: the cursor stayed"
        await pilot.press("ctrl+c")  # empty the editor
        await until(pilot, lambda: app.draft == "")
        await pilot.press("ctrl+z")
        await until(pilot, lambda: app.draft == "one")
    await h.close()


@scenario
async def test_ctrl_z_does_nothing_behind_the_tree(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("first")]))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "one")
        await pilot.press("enter")
        await shows(pilot, app, "first")
        at = (await h.view()).at
        await pilot.press("ctrl+g")
        await until(pilot, lambda: bool(app.screen.query(Tree)))
        await pilot.press("ctrl+z")
        for _ in range(10):
            await pilot.pause()
        assert app.screen.query(Tree), "the tree is still up"
        assert (await h.view()).at == at
        assert app.draft == ""
    await h.close()


@scenario
async def test_ctrl_n_on_a_busy_cursor_asks_before_leaving(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("slow")], gate=gate)])
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        await pilot.press("ctrl+n")
        await until(pilot, lambda: bool(app.screen.query("#confirm-dialog")))
        await pilot.click("#confirm-no")
        await until(pilot, lambda: not app.screen.query("#confirm-dialog"))
        assert app.is_running
        assert app.switch is None
        await pilot.press("ctrl+n")
        await until(pilot, lambda: bool(app.screen.query("#confirm-dialog")))
        await pilot.click("#confirm-yes")
        await until(pilot, lambda: not app.is_running)
        assert app.switch is not None
    await h.close()


@scenario
async def test_a_line_that_met_a_lost_connection_may_have_been_delivered(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([]))
    async with running(h, tmp_path) as (app, pilot):
        await app.client.close()
        await type_line(pilot, "did this arrive")
        await pilot.press("enter")
        await until(pilot, lambda: any("may have reached the session" in t for t in app.toasts))
        assert app.draft == "did this arrive", "kept, so nothing typed is lost"
    await h.close()
