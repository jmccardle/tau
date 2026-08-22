"""The session picker — the modal that chooses among SAVED sessions (Phase B).

Not the tree browser (``test_session_tree_browser.py``), which navigates the
conversation tree *inside* one session. The two are separate screens on purpose
and this file is the other one: it asserts that the picker lists what §5.8 says
it should list, that Tab widens the scope, that ``/`` narrows the rows, and that
a pick lands in the chosen session through the *existing* load path rather than a
second one.

Reference: docs/SESSION-UX-REDESIGN.md §6, §5.7, §5.8.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from tau_agent_core.session_catalog import SessionInfo
from tau_coding_agent.app import ChatInput, Parley
from tau_coding_agent.backends import create_backend
from tau_coding_agent.session_picker import (
    SCOPE_ALL,
    SCOPE_CWD,
    SessionPickerModal,
    elide_start,
    format_age,
    home_relative,
    search_text,
)

#: A directory this process is definitely not running in, so a session written
#: for it must NOT appear in the cwd-scoped listing. It never has to exist: the
#: cwd is an on-disk *directory name* under the session base (§5.1), not a path
#: the picker resolves.
OTHER_CWD = "/nowhere/another-project"


@pytest.fixture
def app(make_app) -> Parley:
    # Sandboxing, config and an injected file catalog come from ``make_app``
    # (tests/conftest.py). No real backend is ever built: ``on_chat_selected``
    # only needs ``create_backend`` to return something.
    return make_app(create_backend=lambda cfg: object())


@pytest.fixture
def dispatch_app(make_app) -> Parley:
    """A Parley on REAL ``TauBackend``s, for the ``/resume`` surface.

    A slash command is resolved inside ``AgentSession.submit`` and comes back as
    a ``CommandOutcome``, so a backend that is a bare ``object()`` cannot dispatch
    one at all (it has no ``submit_command``, which the app RAISES over rather
    than quietly sending "/resume" to a model). ``create_backend`` builds no
    client and touches no network in ``__init__``, so this stays hermetic.
    """
    return make_app(create_backend=create_backend)


async def submit(app: Parley, text: str):
    """Type *text* into the chat input and submit it, exactly as a human would."""
    chat_input = app.query_one("#chat-input", ChatInput)
    return await app.on_input_submitted(Input.Submitted(chat_input, text))


def seed(app: Parley, cwd: str, name: str, *, turns: int = 1):
    """Write one named session for *cwd* through the app's own catalog."""
    session = app.session_catalog.create(cwd, "m", "openai", system_prompt="sys", name=name)
    for index in range(turns):
        session.append_message({"role": "user", "content": f"turn {index}"})
        session.append_message({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    return session


async def open_picker(app: Parley, pilot) -> SessionPickerModal:
    """Run the resume action and wait until the modal has its rows."""
    app.action_resume_session()
    for _ in range(40):
        await pilot.pause()
        screen = app.screen
        if isinstance(screen, SessionPickerModal) and not screen._loading:
            # One more, so the reactive's watcher has repopulated the table.
            await pilot.pause()
            return screen
    raise AssertionError(f"the picker never finished loading (screen={app.screen!r})")


def titles(modal: SessionPickerModal) -> list[str]:
    """The Session column of every drawn row, in order."""
    table = modal.query_one(DataTable)
    return [str(table.get_row_at(index)[0]) for index in range(table.row_count)]


def status_text(modal: SessionPickerModal) -> str:
    """The status line's text. ``Static`` keeps its content in ``.content``
    (Textual 8.x); ``.renderable`` is the pre-Visual name and is gone."""
    return str(modal.query_one("#session-picker-status", Static).content)


def refs(modal: SessionPickerModal) -> list[str]:
    table = modal.query_one(DataTable)
    return [str(key.value) for key in table.rows]


# ---------------------------------------------------------------------------
# Pure helpers — the SessionInfo → row reading
# ---------------------------------------------------------------------------


NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=0), "0s"),
        (timedelta(seconds=59), "59s"),
        (timedelta(seconds=60), "1m"),
        (timedelta(minutes=59, seconds=59), "59m"),
        (timedelta(hours=1), "1h"),
        (timedelta(hours=23, minutes=59), "23h"),
        (timedelta(days=1), "1d"),
        (timedelta(days=364), "364d"),
        (timedelta(days=365), "1y"),
        (timedelta(days=900), "2y"),
    ],
)
def test_format_age_uses_the_largest_whole_unit(delta: timedelta, expected: str) -> None:
    """"5m", "2d" — the scan metric §6 asks for, one unit, always truncating down."""
    assert format_age(NOW - delta, NOW) == expected


def test_format_age_clamps_a_future_timestamp() -> None:
    """A session written by a machine whose clock runs ahead is clock skew, not a
    negative age; "-4s" in the Updated column would read as a bug in the table."""
    assert format_age(NOW + timedelta(seconds=30), NOW) == "0s"


def test_format_age_has_no_clock_of_its_own() -> None:
    """Both ends are arguments, so the same inputs give the same string forever.

    The picker reads the wall clock in exactly one place, which is what keeps a
    second source of nondeterminism out of the TUI (``tagline.py``'s rule).
    """
    then = NOW - timedelta(hours=3)
    assert format_age(then, NOW) == format_age(then, NOW) == "3h"


def info(**overrides) -> SessionInfo:
    base = dict(
        ref="/tmp/s.jsonl",
        id="abcdef0123456789",
        cwd="/tmp",
        name=None,
        created=NOW,
        modified=NOW,
        message_count=0,
        first_message="",
        last_message="",
        parent=None,
    )
    base.update(overrides)
    return SessionInfo(**base)  # type: ignore[arg-type]


def test_search_text_spans_name_first_and_last_message() -> None:
    """§6: the filter matches over all three, so a query can span the fields a
    person actually remembers a conversation by."""
    haystack = search_text(
        info(name="compaction anchor", first_message="why is it dropping", last_message="fixed it")
    )
    assert "compaction anchor" in haystack
    assert "why is it dropping" in haystack
    assert "fixed it" in haystack


def test_search_text_survives_an_unnamed_session() -> None:
    """``name`` is ``None`` until a session is named, and ``None`` is not text."""
    assert search_text(info(first_message="hello")).strip() == "hello"


def test_elide_start_cuts_the_front_and_marks_it() -> None:
    """The opposite end from ``app._elide``: a path's identity is its tail."""
    cut = elide_start("/home/john/Development/agent-harness-py", 20)
    assert cut == "…nt/agent-harness-py"
    assert len(cut) == 20
    assert elide_start("/short", 20) == "/short"


def test_elide_start_leaves_text_alone_when_there_is_no_room_for_the_marker() -> None:
    """Matching ``_elide``: a label cut to nothing says less than one that
    overflows where the reader can see it."""
    assert elide_start("/home/john/x", 1) == "/home/john/x"


def test_home_relative_folds_only_an_exact_home_prefix() -> None:
    """``/home/johnny`` is not inside ``/home/john`` and must not be rewritten
    as though it were — a wrong path in the status line is worse than a long one."""
    home = str(Path.home())
    assert home_relative(home) == "~"
    assert home_relative(f"{home}/Development/tau") == "~/Development/tau"
    assert home_relative(f"{home}ny/Development") == f"{home}ny/Development"
    assert home_relative("/srv/work") == "/srv/work"


# ---------------------------------------------------------------------------
# The modal, driven through the real app
# ---------------------------------------------------------------------------


async def test_picker_lists_only_this_directorys_sessions(app) -> None:
    """§5.8: cwd scope is "list that one dashed-cwd dir", and nothing else.

    The session written for ``OTHER_CWD`` sits in the same sandboxed session base
    and would be listed by a global walk. That it is absent is the whole point of
    partitioning by directory.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        seed(app, os.getcwd(), "here: fix the accumulator")
        seed(app, OTHER_CWD, "elsewhere: unrelated work")

        modal = await open_picker(app, pilot)
        assert titles(modal) == ["here: fix the accumulator"]


async def test_tab_widens_the_scope_to_every_directory(app) -> None:
    """Tab re-runs the loader with ``cwd=None`` — pi's Current/All toggle."""
    async with app.run_test() as pilot:
        await pilot.pause()
        seed(app, os.getcwd(), "here: fix the accumulator")
        seed(app, OTHER_CWD, "elsewhere: unrelated work")

        modal = await open_picker(app, pilot)
        assert modal.scope == SCOPE_CWD

        await pilot.press("tab")
        for _ in range(40):
            await pilot.pause()
            if len(titles(modal)) == 2:
                break
        assert modal.scope == SCOPE_ALL
        assert sorted(titles(modal)) == [
            "elsewhere: unrelated work",
            "here: fix the accumulator",
        ]
        # The directory column exists only in all-scope: in cwd scope every row
        # would carry the same value.
        assert [str(column.label) for column in modal.query_one(DataTable).columns.values()] == [
            "Session",
            "Updated",
            "Msgs",
            "Directory",
        ]


async def test_tab_is_not_focus_next_in_the_picker(app) -> None:
    """The screen's own binding beats ``Screen.BINDINGS``' ``tab → focus_next``.

    Asserted because it is an override of a framework default: if a Textual
    upgrade changed binding precedence, Tab would silently start cycling focus and
    the scope toggle would become unreachable rather than fail.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        seed(app, os.getcwd(), "here")
        modal = await open_picker(app, pilot)
        table = modal.query_one(DataTable)
        assert table.has_focus

        await pilot.press("tab")
        await pilot.pause()
        assert table.has_focus, "tab moved focus instead of toggling the scope"


async def test_slash_filters_the_rows(app) -> None:
    """``/`` opens the filter; typing narrows the table by fuzzy match (§6)."""
    async with app.run_test() as pilot:
        await pilot.pause()
        seed(app, os.getcwd(), "port the compaction anchor from pi")
        seed(app, os.getcwd(), "add a NATS bus extension")

        modal = await open_picker(app, pilot)
        assert len(titles(modal)) == 2

        await pilot.press("slash")
        await pilot.pause()
        assert modal.query_one(Input).has_focus, "/ should put the cursor in the filter"

        await pilot.press("n", "a", "t", "s")
        await pilot.pause()
        assert titles(modal) == ["add a NATS bus extension"]

        # Esc out of the filter returns to the list rather than closing the picker.
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is modal
        assert modal.query_one(DataTable).has_focus


async def test_enter_lands_in_the_chosen_session(app) -> None:
    """The pick flows into the SAME load path the sidebar drives (§6).

    ``ChatSelected`` → ``on_chat_selected`` → ``SessionCatalog.load``: one loader,
    two entry points. Asserted on the app's state, not on the modal's return, so
    the wiring is covered and not just the dismiss.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        seeded = seed(app, os.getcwd(), "the one to resume")
        assert app.current_session is None

        modal = await open_picker(app, pilot)
        assert refs(modal) == [str(seeded.path)]

        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if app.current_session is not None:
                break

        assert app.current_session is not None
        assert app.current_session.id == seeded.id
        assert app.messages[-1] == {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
        }
        assert not isinstance(app.screen, SessionPickerModal), "the picker should have closed"


async def test_escape_cancels_without_loading_anything(app) -> None:
    """Esc on the list is "never mind" — no session becomes current."""
    async with app.run_test() as pilot:
        await pilot.pause()
        seed(app, os.getcwd(), "not this one")

        await open_picker(app, pilot)
        await pilot.press("escape")
        for _ in range(20):
            await pilot.pause()
            if not isinstance(app.screen, SessionPickerModal):
                break

        assert not isinstance(app.screen, SessionPickerModal)
        assert app.current_session is None


async def test_an_empty_directory_says_so_rather_than_showing_nothing(app) -> None:
    """Zero rows is ambiguous — no sessions here, or a picker that failed to load?

    This is the case that found the ``always_update`` bug: assigning ``[]`` over
    the reactive's initial ``[]`` is not a change, so the watcher never ran and
    the picker sat on "Loading…" — a load that had finished looking exactly like
    one that had hung.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        seed(app, OTHER_CWD, "somewhere else entirely")

        modal = await open_picker(app, pilot)
        assert titles(modal) == []
        assert status_text(modal).startswith("0 sessions in ")


async def test_the_status_line_counts_the_filtered_rows(app) -> None:
    """"1 of 2" while filtering, "2" when not — the count says which it is."""
    async with app.run_test() as pilot:
        await pilot.pause()
        seed(app, os.getcwd(), "port the compaction anchor from pi")
        seed(app, os.getcwd(), "add a NATS bus extension")

        modal = await open_picker(app, pilot)
        assert status_text(modal).startswith("2 sessions in ")

        modal.filter_query = "nats"
        await pilot.pause()
        assert status_text(modal).startswith("1 of 2 sessions in ")


async def test_the_status_line_never_takes_a_row_from_the_table(app) -> None:
    """One row, whatever the cwd is.

    A ``height: auto`` caption wrapped a long absolute path over three lines on
    an 80-column terminal and took those three rows out of the list — the caption
    growing at the expense of the thing the dialog exists to show.
    """
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        seed(app, os.getcwd(), "one")
        modal = await open_picker(app, pilot)
        assert modal.query_one("#session-picker-status", Static).region.height == 1


async def test_the_status_line_keeps_the_end_of_a_long_path(app) -> None:
    """The cut goes at the FRONT: the last component is what names the project.

    The suite's own cwd is this repo's worktree path, which is long enough on an
    80-column terminal that something has to give.
    """
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        seed(app, os.getcwd(), "one")
        modal = await open_picker(app, pilot)
        status = status_text(modal)
        leaf = os.path.basename(os.getcwd())
        assert status.endswith(leaf), status


async def test_a_long_title_is_elided_rather_than_wrapped(app) -> None:
    """One session is one row. Rich wraps an over-long cell by default, which
    turns a single session into three rows and pushes the rest off the screen."""
    async with app.run_test() as pilot:
        await pilot.pause()
        seed(app, os.getcwd(), "a session name far longer than any column this dialog can give it")

        modal = await open_picker(app, pilot)
        table = modal.query_one(DataTable)
        assert table.row_count == 1
        assert table.get_row_height(next(iter(table.rows))) == 1


# ---------------------------------------------------------------------------
# §7 — one action, three surfaces. The palette entry, the ``/resume`` slash
# command and ``--resume`` are three BINDINGS to ``action_resume_session``, not
# three implementations: what each of these tests pins is the binding, and
# ``test_the_three_surfaces_are_one_handler`` pins that there is only one thing
# behind them. (The CLI end of the third — ``args.resume`` reaching
# ``Parley(resume=…)`` — is tests/test_cli.py's, where the parser lives.)
# ---------------------------------------------------------------------------


async def test_the_palette_offers_resume(app) -> None:
    """Ctrl+P → "Resume session…" is how the picker is reachable at all today.

    Read off ``get_system_commands`` rather than driven through the palette
    widget: what this pins is that the entry EXISTS and calls the action, which
    is the part a later edit to that long generator can drop by accident.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        commands = {command.title: command for command in app.get_system_commands(app.screen)}
        assert "Resume session…" in commands
        assert commands["Resume session…"].callback == app.action_resume_session


async def test_resume_true_opens_the_picker_over_the_first_frame(make_app) -> None:
    """``Parley(resume=True)`` — the seam ``tau --resume`` needs from cli.py.

    Deferred to ``call_after_refresh``: a screen pushed during ``on_mount`` is
    placed against a base screen that has not laid out yet.
    """
    app = make_app(create_backend=lambda cfg: object(), resume=True)
    async with app.run_test() as pilot:
        for _ in range(40):
            await pilot.pause()
            if isinstance(app.screen, SessionPickerModal):
                break
        assert isinstance(app.screen, SessionPickerModal)


async def test_a_plain_tui_start_does_not_open_the_picker(make_app) -> None:
    """The default is off, so ``tau`` still lands in an empty chat."""
    app = make_app(create_backend=lambda cfg: object())
    async with app.run_test() as pilot:
        for _ in range(10):
            await pilot.pause()
        assert not isinstance(app.screen, SessionPickerModal)


async def test_slash_resume_opens_the_picker(dispatch_app) -> None:
    """The third surface (§7). ``/resume`` is resolved by the CORE
    (``tau_agent_core.commands.FRONTEND_COMMANDS``) and performed here, which is
    what makes it the same command the palette lists rather than a second one
    parsed out of the input box.
    """
    async with dispatch_app.run_test() as pilot:
        await pilot.pause()
        await dispatch_app.action_new_chat()
        await pilot.pause()

        messages_before = list(dispatch_app.messages)
        await submit(dispatch_app, "/resume")
        for _ in range(40):
            await pilot.pause()
            if isinstance(dispatch_app.screen, SessionPickerModal):
                break

        assert isinstance(dispatch_app.screen, SessionPickerModal)
        # Chrome, not conversation: "/resume" never became model input.
        assert dispatch_app.messages == messages_before


async def test_slash_resume_with_a_ref_loads_that_session(dispatch_app) -> None:
    """``/resume <ref>`` ≡ ``--session REF`` (§7): same grammar, same loader.

    The ref here is a session ID, not the ``.jsonl`` path the picker's rows
    carry — that is the point. ``on_chat_selected`` resolves through
    ``SessionCatalog.resolve_ref``, so a human typing ``/resume`` gets the
    path / id / unique-id-prefix grammar ``--session`` has always had.

    It does NOT open the picker — a command given the answer must not ask the
    question — and it lands through ``ChatSelected`` like every other resume, so
    there is still exactly one loading path.
    """
    async with dispatch_app.run_test() as pilot:
        await pilot.pause()
        await dispatch_app.action_new_chat()
        await pilot.pause()
        seeded = seed(dispatch_app, os.getcwd(), "resume me by name")

        await submit(dispatch_app, f"/resume {seeded.id}")
        for _ in range(40):
            await pilot.pause()
            if dispatch_app.current_session is not None:
                if dispatch_app.current_session.id == seeded.id:
                    break

        assert dispatch_app.current_session is not None
        assert dispatch_app.current_session.id == seeded.id
        assert not isinstance(dispatch_app.screen, SessionPickerModal)


async def test_the_three_surfaces_are_one_handler(dispatch_app) -> None:
    """The point of §7, asserted directly: replace the ONE handler and all three
    surfaces go quiet.

    Written as a substitution rather than three separate "did it open the modal"
    assertions, because those would still pass if somebody gave the palette entry
    its own copy of the body. This cannot: the only thing that changed is
    ``action_resume_session``.
    """
    calls: list[str] = []

    async with dispatch_app.run_test() as pilot:
        await pilot.pause()
        await dispatch_app.action_new_chat()
        await pilot.pause()

        dispatch_app.action_resume_session = lambda: calls.append("resume")  # type: ignore[method-assign]

        # Surface 1 — the command palette.
        commands = {c.title: c for c in dispatch_app.get_system_commands(dispatch_app.screen)}
        commands["Resume session…"].callback()

        # Surface 2 — the slash command.
        await submit(dispatch_app, "/resume")
        await pilot.pause()

        # Surface 3 — ``--resume``, which is read once in ``on_mount``. Calling
        # the action the same way the mount hook does keeps this a test of the
        # BINDING; that the flag reaches it is
        # ``test_resume_true_opens_the_picker_over_the_first_frame``'s job.
        dispatch_app._resume_on_start = True
        dispatch_app.on_mount()
        for _ in range(20):
            await pilot.pause()
            if len(calls) == 3:
                break

        assert calls == ["resume", "resume", "resume"]


# ---------------------------------------------------------------------------
# The dialog's geometry — the rules test_tui_appearance applies to every scene,
# applied here instead. The picker is deliberately NOT a scene: its Updated
# column is a live clock, and scenes.py's "no live data" rule is what makes the
# scene set snapshotable.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", [(120, 40), (80, 24)], ids=lambda s: f"{s[0]}x{s[1]}")
async def test_the_dialog_fits_and_is_centred(app, size) -> None:
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        seed(app, os.getcwd(), "one")
        modal = await open_picker(app, pilot)
        assert isinstance(modal, ModalScreen)

        width, height = app.size
        (dialog,) = modal.children
        region = dialog.region
        assert (region.x, region.y) >= (0, 0)
        assert region.right <= width
        assert region.bottom <= height
        assert abs(region.x - (width - region.right)) <= 1
        assert abs(region.y - (height - region.bottom)) <= 1


@pytest.mark.parametrize("size", [(120, 40), (80, 24)], ids=lambda s: f"{s[0]}x{s[1]}")
async def test_no_row_overflows_the_screen(app, size) -> None:
    """The scrollbar rule: a table wider than the dialog spends a column on a
    horizontal bar and the reader loses the text under it."""
    from tau_coding_agent.testing.render import render_text

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        for index in range(6):
            seed(app, os.getcwd(), f"session number {index} with a reasonably long name")
        modal = await open_picker(app, pilot)

        for number, row in enumerate(render_text(app).splitlines()):
            assert len(row) <= size[0], f"row {number} is {len(row)} cols"
        table = modal.query_one(DataTable)
        assert table.virtual_size.width <= table.content_size.width
