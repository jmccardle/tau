"""Steps in flight: abort, pending lines, the status bar (D23 sections 2 and 9)."""

from pathlib import Path

from fakes import Fail, FakeProvider, FakeTool, Gate, Reply, edits
from harness import Harness, scenario
from tuikit import Greeter, Showy, running, say, shows, type_line, until

from tau import CallArgs, Ending, Live, Part, RetryProvider, Stop, TextDelta


@scenario
async def test_escape_aborts_a_busy_cursor(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("slow")], gate=gate)])
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        await pilot.press("escape")
        await shows(pilot, app, "aborted")
        view = await h.view()
        assert not view.busy
        assert view.ending == Ending.Aborted()
    await h.close()


@scenario
async def test_ctrl_c_aborts_when_busy_then_clears_the_draft_then_asks_twice_to_quit(
    tmp_path: Path,
) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([Reply([Part.Text("slow")], gate=gate)])
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        await pilot.press("ctrl+c")
        await shows(pilot, app, "aborted")
        await type_line(pilot, "half a thought")
        await pilot.press("ctrl+c")
        assert app.draft == ""
        await pilot.press("ctrl+c")
        assert app.is_running
        await pilot.press("ctrl+c")
        await until(pilot, lambda: not app.is_running)
        assert app.return_code == 0
    await h.close()


@scenario
async def test_a_line_sent_while_busy_is_pending_with_its_author_until_the_step_takes_it(
    tmp_path: Path,
) -> None:
    gate = Gate()
    script = [Reply([Part.Text("one")], gate=gate), say("two")]
    h = await Harness.create(tmp_path, provider=FakeProvider(script))
    other = await h.connect("me:other")
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "first")
        await pilot.press("enter")
        await shows(pilot, app, "working…")
        await type_line(pilot, "second")
        await pilot.press("enter")
        await shows(pilot, app, "> second")
        await other.input(h.cursor, "third")
        await shows(pilot, app, "> [me:other] third")
        gate.release()
        await shows(pilot, app, "[User] second")
        await until(pilot, lambda: "pending" not in app.screen_text())
    await other.close()
    await h.close()


@scenario
async def test_the_status_bar_shows_the_model_and_what_an_extension_shows(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("ok")]), extensions=[Showy()])
    async with running(h, tmp_path) as (app, pilot):
        assert "model: fake  ·  idle" in app.screen_text()
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "showy: plan mode on")
    await h.close()


@scenario
async def test_a_failed_step_is_named_in_the_status_bar(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Fail("boom")]))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "failed: boom")
    await h.close()


@scenario
async def test_a_notice_is_a_toast_and_kept(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Greeter()])
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "/count")
        await pilot.press("enter")
        await until(pilot, lambda: "/count: counted" in app.toasts)
        assert "at 0" in app.toasts, "the command's progress is a notice"
        assert app.session.notices == ["at 0"]
    await h.close()


@scenario
async def test_the_status_bar_says_a_provider_is_waiting_to_retry_until_the_reply_comes(
    tmp_path: Path,
) -> None:
    gate = Gate()
    waits = Gate()

    async def wait(seconds: float) -> None:
        await waits.wait()

    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(
            [
                Fail("HTTP 529 overloaded", status=529),
                Reply([TextDelta("ba"), Part.Text("back")], gate=gate, gate_after=1),
            ]
        ),
        wrap=lambda fake: RetryProvider(fake, sleep=wait),
        gates=[waits, gate],
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "HTTP 529 overloaded: retry 1 of 3 in 1 s")
        waits.release()
        await until(pilot, lambda: "retry 1 of 3" not in app.screen_text())
        gate.release()
        await shows(pilot, app, "back")
    await h.close()


@scenario
async def test_an_app_that_falls_behind_a_reply_of_parallel_calls_draws_it_when_it_lands(
    tmp_path: Path,
) -> None:
    """The mcrf-term crash (`KeyError` in `ChatDisplay.live`): the app's buffer filled during a
    reply of four `edit` calls and lost call 1's `CallNamed`; once it had read some, call 1's
    `CallArgs` came. Now the reply stops drawing live at the first delta lost, and is drawn
    whole when its node lands."""
    gate = Gate()
    _, items = edits(4)
    batch = Reply(items, Stop.Tools, gate=gate, gate_after=6)  # call 1 named, its args to come
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([batch, say("all edited")]),
        tools=[FakeTool("edit", "ok")],
    )
    async with running(h, tmp_path, buffer=3) as (app, pilot):
        await h.send("edit four")
        await until(pilot, lambda: gate.held == 1)
        await shows(pilot, app, "[Assistant, live]")
        gate.release()
        await shows(pilot, app, "all edited")
        assert app.screen_text().count('edit({"a":1})') == 4, "the reply, whole, from its node"
        assert not app.toasts, "a client that fell behind is not an error"
    await h.close()


@scenario
async def test_viewing_the_viewed_cursor_mid_reply_keeps_the_reply_in_flight(
    tmp_path: Path,
) -> None:
    """Viewing the cursor in view again (the tree screen's choice of it does) watches nothing new
    and replays nothing, so the layer is kept (forgetting it lost the call named before, and its
    next `CallArgs` broke the app)."""
    gate = Gate()
    _, items = edits(2)
    batch = Reply(items, Stop.Tools, gate=gate, gate_after=2)  # call 0 named, one piece of args
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([batch, say("all edited")]),
        tools=[FakeTool("edit", "ok")],
    )
    async with running(h, tmp_path) as (app, pilot):
        await h.send("edit two")
        await until(pilot, lambda: gate.held == 1)
        await shows(pilot, app, "[Assistant, live]")
        await app.view(app.cursor)
        gate.release()
        await shows(pilot, app, "all edited")
        assert not app.toasts
    await h.close()


@scenario
async def test_a_call_s_arguments_before_its_name_are_said_and_the_reply_lands_whole(
    tmp_path: Path,
) -> None:
    """No stream says this (the driver refuses it from a provider and a feed drops the rest of a
    reply that lost a delta), so the app hearing it is a bug: an error toast, no crash, and the
    reply drawn from its node."""
    gate = Gate()
    _, items = edits(2)
    batch = Reply(items, Stop.Tools, gate=gate, gate_after=2)  # call 0 named, one piece of args
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([batch, say("all edited")]),
        tools=[FakeTool("edit", "ok")],
    )
    async with running(h, tmp_path) as (app, pilot):
        await h.send("edit two")
        await until(pilot, lambda: gate.held == 1)
        await shows(pilot, app, "[Assistant, live]")
        await app._hear(Live(h.cursor, CallArgs(7, "{}")))  # noqa: SLF001 - a stream no server sends
        assert any("call 7 came before the call was named" in t for t in app.toasts)
        assert "[Assistant, live]" not in app.screen_text()
        gate.release()
        await shows(pilot, app, "all edited")
        assert app.screen_text().count('edit({"a":1})') == 2
    await h.close()
