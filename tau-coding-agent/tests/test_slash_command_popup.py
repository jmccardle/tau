"""Tab completion and the slash-command popup in the chat editor.

Reference: docs/SLASH-COMMANDS.md.

Keys are pressed through a mounted app rather than handlers being called,
because the whole question is which layer gets the keystroke: this widget's
``on_key``, ``TextArea._on_key``, or the App's focus-next binding. Tab is the one
key the editor had spare, and it only stops being a focus key when there is
actually something to insert — that boundary is what most of these tests hold.
"""

from __future__ import annotations

from typing import Any

from tau_agent_core.commands import FRONTEND_COMMANDS
from tau_coding_agent.app import ChatInput, CommandPopup, Parley


class _Backend:
    """A backend with an extension vocabulary and no way to run a turn."""

    def __init__(self, commands: list[tuple[str, str]] | None = None) -> None:
        self._commands = commands if commands is not None else []

    def get_extension_commands(self) -> list[tuple[str, str]]:
        return list(self._commands)

    async def submit_turn(self, submission: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("no test in this module runs a turn")


class _NoExtensionsBackend:
    """A backend from before extensions loaded — no ``get_extension_commands``.

    The ``getattr`` guard in :meth:`Parley._extension_command_table` exists for
    this shape, and completion has to survive it the same way the peek does.
    """

    async def submit_turn(self, submission: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("no test in this module runs a turn")


def _app(make_app, backend: Any = None) -> Parley:
    return make_app(create_backend=lambda cfg: backend or _Backend())


async def _type(pilot, editor: ChatInput, text: str) -> None:
    """Put ``text`` in the editor the way the app's other code paths do."""
    editor.text = text
    editor.move_cursor(editor.document.end)
    await pilot.pause()


class TestThePopupSaysWhetherTheSlashIsReal:
    """The reason this exists. An unknown ``/…`` is sent to the model as ordinary
    text, which is deliberate and, until now, entirely silent."""

    async def test_ordinary_prose_shows_nothing(self, make_app):
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            popup = app.query_one(CommandPopup)
            await _type(pilot, editor, "summarise the readme")
            assert popup.display is False
            assert popup.text == ""

    async def test_a_known_command_shows_its_description(self, make_app):
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            popup = app.query_one(CommandPopup)
            await _type(pilot, editor, "/tree")
            assert popup.display is True
            assert "/tree" in popup.text
            assert "open the session-tree browser" in popup.text

    async def test_an_unknown_command_says_it_goes_to_the_model(self, make_app):
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            popup = app.query_one(CommandPopup)
            await _type(pilot, editor, "/exntesions")
            assert popup.display is True
            assert "is not a command" in popup.text
            assert "goes to the model as text" in popup.text
            assert popup.has_class("command-popup-unknown")

    async def test_the_warning_class_comes_off_again(self, make_app):
        """A stale class would paint a real command in the warning colour."""
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            popup = app.query_one(CommandPopup)
            await _type(pilot, editor, "/exntesions")
            await _type(pilot, editor, "/tree")
            assert popup.has_class("command-popup-unknown") is False

    async def test_a_pasted_path_is_not_warned_about(self, make_app):
        """Pasting a path is the reason unrecognised slashes fall through at all,
        so the warning must not fire on every one of them."""
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            popup = app.query_one(CommandPopup)
            await _type(pilot, editor, "/usr/bin/env is on my PATH")
            assert popup.display is False

    async def test_a_bare_slash_lists_the_vocabulary(self, make_app):
        app = _app(make_app, _Backend([("todo", "manage the todo list")]))
        async with app.run_test() as pilot:
            await app.action_new_chat()
            editor = app.query_one(ChatInput)
            popup = app.query_one(CommandPopup)
            await _type(pilot, editor, "/")
            for name in FRONTEND_COMMANDS:
                assert f"/{name}" in popup.text
            assert "/todo" in popup.text

    async def test_an_extension_command_reaches_the_popup(self, make_app):
        app = _app(make_app, _Backend([("todo", "manage the todo list")]))
        async with app.run_test() as pilot:
            await app.action_new_chat()
            editor = app.query_one(ChatInput)
            popup = app.query_one(CommandPopup)
            await _type(pilot, editor, "/tod")
            assert "/todo" in popup.text
            assert "manage the todo list" in popup.text

    async def test_before_any_chat_an_extension_command_reads_as_unknown(self, make_app):
        """Not a gap in the popup — the popup reporting a gap accurately.

        ``Parley.current_backend`` is built by ``action_new_chat``, which the app
        runs lazily at the FIRST submit, so before then no extension has loaded
        and no extension command exists. ``on_input_submitted``'s own peek reads
        the same empty vocabulary and sends such a line to the model as prose.
        The popup saying so is the first visible sign of that; a popup that
        listed ``/todo`` here would be promising a dispatch that will not happen.
        """
        app = _app(make_app, _Backend([("todo", "manage the todo list")]))
        async with app.run_test() as pilot:
            assert app.current_backend is None
            editor = app.query_one(ChatInput)
            popup = app.query_one(CommandPopup)
            await _type(pilot, editor, "/todo")
            assert "is not a command" in popup.text
            assert app._extension_command_names() == []

    async def test_a_backend_without_extensions_still_shows_the_built_ins(self, make_app):
        app = _app(make_app, _NoExtensionsBackend())
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            popup = app.query_one(CommandPopup)
            await _type(pilot, editor, "/comp")
            assert "/compact" in popup.text

    async def test_clearing_the_editor_hides_it(self, make_app):
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            popup = app.query_one(CommandPopup)
            await _type(pilot, editor, "/tree")
            editor.clear_input()
            await pilot.pause()
            assert popup.display is False


class TestTabCompletes:
    async def test_a_unique_prefix_completes(self, make_app):
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            editor.focus()
            await _type(pilot, editor, "/comp")
            await pilot.press("tab")
            await pilot.pause()
            assert editor.text == "/compact "

    async def test_the_trailing_space_is_kept(self, make_app):
        """pi's ``applyCompletion`` inserts one, and it is where an argument goes.
        A command taking none resolves identically, because ``parse_command``
        strips."""
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            editor.focus()
            await _type(pilot, editor, "/res")
            await pilot.press("tab")
            await pilot.pause()
            assert editor.text.endswith(" ")

    async def test_repeated_tab_cycles(self, make_app):
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            editor.focus()
            await _type(pilot, editor, "/")
            seen = []
            for _ in range(len(FRONTEND_COMMANDS)):
                await pilot.press("tab")
                await pilot.pause()
                seen.append(editor.text.strip())
            assert seen == [f"/{name}" for name in FRONTEND_COMMANDS]

    async def test_the_cycle_wraps(self, make_app):
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            editor.focus()
            await _type(pilot, editor, "/")
            for _ in range(len(FRONTEND_COMMANDS) + 1):
                await pilot.press("tab")
                await pilot.pause()
            assert editor.text.strip() == f"/{next(iter(FRONTEND_COMMANDS))}"

    async def test_typing_ends_the_cycle(self, make_app):
        """The cycle is identified by the editor still holding what the last Tab
        wrote, which is what lets ANY keystroke end it with no mode to clear."""
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            editor.focus()
            await _type(pilot, editor, "/t")
            await pilot.press("tab")
            await pilot.pause()
            assert editor.text == "/tree "
            await _type(pilot, editor, "/f")
            await pilot.press("tab")
            await pilot.pause()
            assert editor.text == "/fork "

    async def test_the_selected_row_is_marked(self, make_app):
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            popup = app.query_one(CommandPopup)
            editor.focus()
            await _type(pilot, editor, "/")
            await pilot.press("tab")
            await pilot.pause()
            first = next(iter(FRONTEND_COMMANDS))
            assert f"▸ [b]/{first}[/b]" in popup.text

    async def test_the_marker_goes_away_when_the_cycle_does(self, make_app):
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            popup = app.query_one(CommandPopup)
            editor.focus()
            await _type(pilot, editor, "/")
            await pilot.press("tab")
            await pilot.pause()
            await _type(pilot, editor, "/tr")
            assert "▸" not in popup.text

    async def test_tab_completes_an_extension_command_too(self, make_app):
        app = _app(make_app, _Backend([("todo", "manage the todo list")]))
        async with app.run_test() as pilot:
            await app.action_new_chat()
            editor = app.query_one(ChatInput)
            editor.focus()
            await _type(pilot, editor, "/tod")
            await pilot.press("tab")
            await pilot.pause()
            assert editor.text == "/todo "


class TestTabIsOnlyClaimedWhenItHasSomethingToInsert:
    """Tab moved focus in this editor before completion existed, and that is the
    only reason completion could have it. Where there is nothing to complete, the
    key is left alone rather than swallowed."""

    async def test_prose_leaves_the_text_alone(self, make_app):
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            editor.focus()
            await _type(pilot, editor, "summarise the readme")
            await pilot.press("tab")
            await pilot.pause()
            assert editor.text == "summarise the readme"

    async def test_an_unknown_command_leaves_the_text_alone(self, make_app):
        app = _app(make_app)
        async with app.run_test() as pilot:
            editor = app.query_one(ChatInput)
            editor.focus()
            await _type(pilot, editor, "/exntesions")
            await pilot.press("tab")
            await pilot.pause()
            assert editor.text == "/exntesions"

    async def test_an_unwired_editor_does_nothing(self):
        """A ``ChatInput`` built without the app has no vocabulary to offer, and
        that is not an error: it is a widget with no completion source, which is
        how most of its own tests build it."""
        editor = ChatInput()
        assert editor.command_completions is None
        assert editor._complete() is False
        assert editor.completion_index is None


class TestTheWindowFollowsTheSelection:
    """A ``Static`` has no selected line to scroll to, so the widget picks the
    slice instead of letting a scrollbar do it."""

    def test_a_long_list_is_capped(self):
        popup = CommandPopup()
        popup.show(_completions(20), selected=None)
        assert len(popup.text.splitlines()) == CommandPopup.MAX_ROWS + 1
        assert "… 12 more" in popup.text

    def test_a_selection_past_the_window_scrolls_it_into_view(self):
        popup = CommandPopup()
        popup.show(_completions(20), selected=15)
        lines = popup.text.splitlines()
        assert any(line.startswith("▸") for line in lines)
        assert "/cmd15" in popup.text
        assert "/cmd00" not in popup.text


def _completions(count: int):
    from tau_agent_core.commands import CommandCompletion, CommandCompletions

    return CommandCompletions(
        token="",
        matches=tuple(
            CommandCompletion(name=f"cmd{i:02d}", description="", performer="core")
            for i in range(count)
        ),
    )
