"""What the Enter key does in the chat editor, and what Ctrl+J does instead.

Reference: docs/ENTER-KEY.md.

τ's default is that Enter breaks the line and Ctrl+J sends. Every other terminal
coding agent, pi included, does the opposite. ``enter_key`` in config.json picks
one, and these tests press the actual keys through a mounted app rather than
calling the handlers, because the whole question is which of several layers gets
the keystroke first: this widget's ``on_key``, ``TextArea._on_key``, or the App's
binding table.
"""

from __future__ import annotations

from typing import Any

import pytest
from tau_coding_agent.app import ChatInput, Parley
from tau_coding_agent.config import ConfigError


class _Backend:
    """A backend that never runs a turn. No test here reaches a model."""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    async def submit_turn(self, submission: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("no test in this module runs a turn")


@pytest.fixture
def enter_app(make_app):
    def _build(mode: str | None = None) -> Parley:
        config = {} if mode is None else {"enter_key": mode}
        return make_app(create_backend=lambda cfg: _Backend(), config=config)

    return _build


class TestTheDefaultIsUnchanged:
    """τ shipped with Enter=newline and that is still what an unset config gets."""

    async def test_enter_breaks_the_line(self, enter_app):
        app = enter_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = app.query_one(ChatInput)
            editor.focus()
            editor.text = "one"
            editor.move_cursor(editor.document.end)
            await pilot.press("enter")
            await pilot.pause()
            assert editor.text == "one\n"

    async def test_ctrl_j_still_sends(self, enter_app):
        """The half of the default the new ``on_key`` branches must not touch.

        Ctrl+J sends here through ``ChatInput.BINDINGS``, which the App checks
        only once the key has bubbled up to it. In ``"newline"`` mode nothing in
        ``on_key`` stops it, so it still gets there.
        """
        app = enter_app()
        sent: list[str] = []
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = app.query_one(ChatInput)
            editor.action_submit = lambda: sent.append(editor.text)  # type: ignore[method-assign]
            editor.focus()
            editor.text = "send me"
            editor.move_cursor(editor.document.end)
            await pilot.press("ctrl+j")
            await pilot.pause()
            assert sent == ["send me"]
            assert editor.text == "send me"

    async def test_an_unset_config_reads_as_newline(self, enter_app):
        app = enter_app()
        assert app._enter_key_mode == "newline"


class TestSubmitMode:
    """``enter_key: "submit"`` — pi's pair, and every other agent's."""

    async def test_enter_does_not_break_the_line(self, enter_app):
        """The half of the swap that ``TextArea`` would otherwise win.

        ``TextArea._on_key`` inserts ``"\\n"`` for Enter and stops the event, so a
        plain ``Binding("enter", …)`` on the widget never fires. Asserting on the
        TEXT rather than on a submit callback is what proves the interception ran
        early enough — a suppression that happened after the insert would leave
        the line broken here even though the message also went out.
        """
        app = enter_app("submit")
        sent: list[str] = []
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = app.query_one(ChatInput)
            # Stubbed so the real submit path does not empty the box behind us —
            # the assertion here is about the line break, not about sending.
            editor.action_submit = lambda: sent.append(editor.text)  # type: ignore[method-assign]
            editor.focus()
            editor.text = "one"
            editor.move_cursor(editor.document.end)
            await pilot.press("enter")
            await pilot.pause()
            assert editor.text == "one"

    async def test_ctrl_j_breaks_the_line_instead(self, enter_app):
        app = enter_app("submit")
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = app.query_one(ChatInput)
            editor.focus()
            editor.text = "one"
            editor.move_cursor(editor.document.end)
            await pilot.press("ctrl+j")
            await pilot.pause()
            assert editor.text == "one\n"

    async def test_ctrl_j_does_not_also_send(self, enter_app):
        """The regression the ``event.stop()`` in ``on_key`` exists for.

        ``ChatInput.BINDINGS`` still carries ``ctrl+j`` → submit, and the App
        checks bindings once a key has bubbled up to it. Inserting the line break
        without stopping the event would break the line AND send the half-written
        message, which is the exact accident this whole setting is meant to stop.
        """
        app = enter_app("submit")
        sent: list[str] = []
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = app.query_one(ChatInput)
            editor.action_submit = lambda: sent.append(editor.text)  # type: ignore[method-assign]
            editor.focus()
            editor.text = "half a prompt"
            editor.move_cursor(editor.document.end)
            await pilot.press("ctrl+j")
            await pilot.pause()
            assert sent == []
            assert editor.text == "half a prompt\n"

    async def test_shift_enter_breaks_the_line(self, enter_app):
        """Reachable only under the kitty keyboard protocol.

        ``Pilot.press`` synthesises the key name directly, so this test asserts
        the BINDING is right, not that any given terminal can send it. A terminal
        that cannot leaves Ctrl+J as the newline key, which is why Ctrl+J is kept
        in the pair rather than replaced by this.
        """
        app = enter_app("submit")
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = app.query_one(ChatInput)
            editor.focus()
            editor.text = "one"
            editor.move_cursor(editor.document.end)
            await pilot.press("shift+enter")
            await pilot.pause()
            assert editor.text == "one\n"

    async def test_enter_submits(self, enter_app):
        app = enter_app("submit")
        sent: list[str] = []
        async with app.run_test() as pilot:
            await pilot.pause()
            editor = app.query_one(ChatInput)
            editor.action_submit = lambda: sent.append(editor.text)  # type: ignore[method-assign]
            editor.focus()
            editor.text = "send me"
            editor.move_cursor(editor.document.end)
            await pilot.press("enter")
            await pilot.pause()
            assert sent == ["send me"]


class TestTheFooterNamesTheKeyThatSends:
    """Exactly one of the two app bindings is live, and it is the true one."""

    async def test_newline_mode_advertises_ctrl_j(self, enter_app):
        app = enter_app("newline")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.check_action("focus_and_send", ()) is True
            assert app.check_action("focus_and_send_on_enter", ()) is False

    async def test_submit_mode_advertises_enter(self, enter_app):
        app = enter_app("submit")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.check_action("focus_and_send", ()) is False
            assert app.check_action("focus_and_send_on_enter", ()) is True


class TestFailEarly:
    """Guessing would hand the user an editor whose Enter does the opposite of
    what they configured, and the way they would find out is by sending half a
    prompt — the accident this setting exists to prevent."""

    def test_a_misspelt_mode_raises_on_read(self, enter_app):
        app = enter_app("sumbit")
        with pytest.raises(ConfigError, match=r"enter_key.*'sumbit'"):
            app._enter_key_mode

    def test_the_message_lists_the_accepted_values(self, enter_app):
        app = enter_app("Submit")
        with pytest.raises(ConfigError, match=r"newline, submit"):
            app._enter_key_mode

    def test_a_bad_value_on_disk_stops_the_app_starting(self, tau_home):
        """The startup check, which the sandbox fixture cannot exercise.

        ``build_parley`` assigns ``app.config`` AFTER ``__init__`` has run, so a
        config passed to the fixture is never seen by the startup check. Only a
        value actually on disk is, which is also the only way a real user reaches
        it. Held here so the failure keeps landing while τ is starting rather
        than at the first keystroke half an hour later.
        """
        import json

        from tau_coding_agent import config as config_module
        from tau_coding_agent.session_store import FileSessionCatalog

        config_module.CONFIG_PATH.write_text(json.dumps({"models": {}, "enter_key": "nope"}))
        with pytest.raises(ConfigError, match="enter_key"):
            Parley(session_catalog=FileSessionCatalog(tau_home / "sessions"))

    def test_the_packaged_default_config_names_a_real_mode(self):
        import json

        from tau_coding_agent.app import ENTER_KEY_CONFIG_KEY, ENTER_KEY_MODES
        from tau_coding_agent.config import DEFAULT_CONFIG_TEMPLATE

        template = json.loads(DEFAULT_CONFIG_TEMPLATE.read_text())
        assert template[ENTER_KEY_CONFIG_KEY] in ENTER_KEY_MODES


class TestTheWidgetAloneKeepsTheDefault:
    """A ``ChatInput`` built without the app is τ's default, not an error.

    ``enter_key_mode`` reports a SETTING, and an unset setting has a documented
    value. The Fail-Early check belongs one layer up, where there is a config
    file to be wrong about, and :class:`TestFailEarly` holds it there.
    """

    def test_unwired_chat_input_breaks_the_line(self):
        editor = ChatInput()
        assert editor.enter_key_mode is None
        assert editor._enter_sends() is False
