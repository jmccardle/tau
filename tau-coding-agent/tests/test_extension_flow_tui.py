"""An extension gesture reaching the TUI with no head code written for it.

Reference: docs/EXTENSION-FLOWS.md §4.

The claim the whole feature rests on: an extension that declares what its command
takes gets tab completion, a rendered form and a palette argument in this head
WITHOUT a branch anywhere in ``app.py`` naming it. These tests hold that claim by
registering a command this repository has never heard of and then driving the TUI's
own surfaces.

The backend is a stub carrying a real ``AgentSession``, because the vocabulary is
read off the session (``TauApp._vocabulary``) and a fake would be asserting that the
test's own dict reaches the popup rather than that the session's does.
"""

from __future__ import annotations

from typing import Any

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.capabilities import BUILTIN, Argument, Domain
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import Model

from tau_coding_agent import chat_widgets, editor_widgets
from tau_coding_agent.app import TauApp

TODOS = [("t1", "Buy milk"), ("t2", "Ship the release"), ("t3", "Book flights")]


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


class _Backend:
    """A backend whose session has one extension-declared flow on it."""

    def __init__(self, calls: list[str]) -> None:
        self.agent_session = AgentSession(session_log=InMemorySessionLog(), model=_model())
        api = self.agent_session._bind_extension_api("/x/todo.py")

        def handler(args, ctx):
            calls.append(args)
            return f"marked {args} done"

        api.register_flow(
            "done",
            "mark a todo done",
            handler,
            argument=Argument("item", "todo_item", "Which todo."),
            domain=Domain(
                "todo_item", "An open todo.", enumerator="extension", field_kind="select"
            ),
            values=lambda query, limit: [t for t in TODOS if query.lower() in t[1].lower()][:limit],
        )

    def get_extension_commands(self) -> list[tuple[str, str]]:
        return self.agent_session.get_extension_commands()

    async def run_extension_command(self, name: str, args: str = "") -> Any:
        return await self.agent_session.run_extension_command(name, args)

    async def submit_turn(self, submission: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("no test in this module runs a turn")


async def _type(pilot, editor: chat_widgets.ChatInput, text: str) -> None:
    editor.text = text
    editor.move_cursor(editor.document.end)
    await pilot.pause()


async def test_the_command_name_completes(make_app) -> None:
    app = make_app(create_backend=lambda cfg: None)
    async with app.run_test() as pilot:
        app.current_backend = _Backend([])
        editor = app.query_one(chat_widgets.ChatInput)
        popup = app.query_one(editor_widgets.CommandPopup)
        editor.focus()
        await _type(pilot, editor, "/do")
        assert "/done" in popup.text
        assert "mark a todo done" in popup.text


async def test_its_argument_completes_from_the_declared_enumerator(make_app) -> None:
    """The whole point. ``app.py`` contains no mention of ``done`` or ``todo_item``;
    the values come from the callable the extension registered."""
    app = make_app(create_backend=lambda cfg: None)
    async with app.run_test() as pilot:
        app.current_backend = _Backend([])
        editor = app.query_one(chat_widgets.ChatInput)
        popup = app.query_one(editor_widgets.CommandPopup)
        editor.focus()
        await _type(pilot, editor, "/done ")
        assert "Buy milk" in popup.text
        assert "Ship the release" in popup.text


async def test_tab_inserts_a_declared_value(make_app) -> None:
    app = make_app(create_backend=lambda cfg: None)
    async with app.run_test() as pilot:
        app.current_backend = _Backend([])
        editor = app.query_one(chat_widgets.ChatInput)
        editor.focus()
        await _type(pilot, editor, "/done Buy")
        await pilot.press("tab")
        await pilot.pause()
        assert editor.text == "/done t1 "


async def test_a_query_narrows_the_values(make_app) -> None:
    app = make_app(create_backend=lambda cfg: None)
    async with app.run_test() as pilot:
        app.current_backend = _Backend([])
        editor = app.query_one(chat_widgets.ChatInput)
        popup = app.query_one(editor_widgets.CommandPopup)
        editor.focus()
        await _type(pilot, editor, "/done ship")
        assert "Ship the release" in popup.text
        assert "Buy milk" not in popup.text


async def test_a_bound_flow_reaches_the_handler(make_app) -> None:
    """``_perform_ready`` routes an extension flow to its own handler rather than
    looking for a backend method named after it."""
    calls: list[str] = []
    app = make_app(create_backend=lambda cfg: None)
    async with app.run_test() as pilot:
        app.current_backend = _Backend(calls)
        app.notify = lambda message, **kw: None  # type: ignore[method-assign]
        await app.action_run_session_flow("done", "t1")
        await pilot.pause()
        assert calls == ["t1"]


async def test_a_head_with_no_extensions_reads_the_built_ins(make_app) -> None:
    """Before a backend exists there is no session to read, and that is not an
    error — the same first-frame case the command vocabulary already handles."""
    app = make_app(create_backend=lambda cfg: None)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._vocabulary() is BUILTIN
