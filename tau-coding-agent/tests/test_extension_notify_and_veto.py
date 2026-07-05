"""E5 §4 (S33) — extension ``api.notify`` paints in the TUI; a veto renders blocked.

Two claims, driven through the REAL app (``App.run_test()`` / Pilot) and the real
``ChatDisplay`` render machine:

1. After every ``create_backend`` the app wires an ``_ExtensionUIDelegate`` onto
   the backend via ``set_ui_delegate`` (proven by a spy AND by the effect: the
   shared ``ExtensionUI`` is in TUI mode). A loaded extension's
   ``api.ui.notify(...)`` then routes to ``App.notify`` — the on-screen sink —
   instead of the headless stderr sink.
2. A ``tool_call`` veto (an ``is_error`` toolResult) renders as a visibly-blocked
   line: the ``ToolBox`` gains the ``box-error`` class and a ``✗`` title mark, and
   its body carries the block reason.

Reference: docs/EXTENSIONS-E5-WIRING.md §4, S33.
"""

from __future__ import annotations

import pytest

from textual.app import App, ComposeResult

from tau_coding_agent.app import ChatDisplay, Parley, _ExtensionUIDelegate
from tau_coding_agent.backends import create_backend
from tau_coding_agent.chat_widgets import ToolBox


@pytest.fixture
def app(monkeypatch, tmp_path):
    """A Parley wired to REAL TauBackends, with sandboxed session storage."""
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    monkeypatch.setattr("tau_coding_agent.app.create_backend", create_backend)
    monkeypatch.setattr(store, "_session_listeners", [])

    a = Parley()
    a.config = {
        "models": {"m": {"backend": "openai", "model": "m", "api_key": "not-needed"}},
        "default_model": "m",
        "system_prompt": "sys",
    }
    return a


# ---------------------------------------------------------------------------
# 1. api.notify is wired into the TUI
# ---------------------------------------------------------------------------

async def test_new_chat_calls_set_ui_delegate(app, monkeypatch):
    """The app calls ``backend.set_ui_delegate`` after creating a backend."""
    from tau_coding_agent.backends import TauBackend

    seen: list[object] = []
    orig = TauBackend.set_ui_delegate

    def spy(self, delegate):
        seen.append(delegate)
        return orig(self, delegate)

    monkeypatch.setattr(TauBackend, "set_ui_delegate", spy)
    app._extension_paths = []
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        # set_ui_delegate was called with the app's notify delegate...
        assert len(seen) == 1
        assert isinstance(seen[0], _ExtensionUIDelegate)
        # ...and the effect landed: the shared ExtensionUI is in TUI mode.
        ctx = app.current_backend.agent_session._extension_api.context
        assert ctx._ui._mode == "tui"


async def test_extension_notify_paints_via_app_notify(app, monkeypatch):
    """An extension's ``api.ui.notify`` reaches ``App.notify`` (the TUI sink).

    Every bound extension's ``api.ui`` IS the session's shared ``ExtensionUI``
    (they all share ``_extension_api.context``), so firing notify on it is exactly
    what a hook's ``api.ui.notify(...)`` does. With the delegate wired by the app,
    it routes to ``App.notify`` — not the headless stderr sink.
    """
    app._extension_paths = []
    app._discover_extensions = False

    painted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        app,
        "notify",
        lambda msg, **kw: painted.append((msg, kw.get("severity", ""))),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        ui = app.current_backend.agent_session._extension_api.context._ui
        ui.notify("budget exceeded", "warning")
        ui.notify("all good", "info")

    assert ("budget exceeded", "warning") in painted
    # "info" maps to Textual's "information" severity.
    assert ("all good", "information") in painted


# ---------------------------------------------------------------------------
# 2. A veto renders as a visibly-blocked line
# ---------------------------------------------------------------------------


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield ChatDisplay()


async def test_veto_renders_as_blocked_toolbox():
    """A tool_call + is_error tool_result (the veto shape) renders as a blocked
    ToolBox: box-error class, ✗ mark, and the reason in the body."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)

        display.add_message("user", "write outside scope")
        await display.begin_exchange()
        display.handle_stream_event({"kind": "turn_start", "turn_index": 0})
        await pilot.pause()
        # These are exactly the events a veto now emits (tool_execution_start ->
        # tool_execution_end(is_error=True)), normalized by TauBackend.stream_chat.
        display.handle_stream_event(
            {"kind": "tool_call", "id": "c1", "name": "write", "arguments": {"path": "/etc/x"}}
        )
        await pilot.pause()
        display.handle_stream_event(
            {
                "kind": "tool_result",
                "id": "c1",
                "name": "write",
                "result": "denied by policy",
                "is_error": True,
            }
        )
        await pilot.pause()

        boxes = list(display.query(ToolBox))
        assert len(boxes) == 1
        box = boxes[0]
        # The blocked call is rendered (not dropped), and rendered DISTINCTLY.
        assert box.has_result is True
        assert box.has_class("box-error")
        assert box.title.startswith("✗")
        assert "denied by policy" in box._result_md._markdown


async def test_extension_veto_renders_blocked_by_extension():
    """S50 (anchor G11): a `tool_call` extension VETO renders DISTINCTLY from a
    generic error — a ⛔ mark, the ``box-blocked`` class, and a
    "blocked by <ext>: <reason>" body naming the vetoing extension.

    This is the tool_result shape TauBackend.stream_chat now emits for a veto: the
    ``blocked``/``blocked_by`` fields threaded off the ``tool_execution_end``
    AgentEvent's new markers (S50)."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)

        display.add_message("user", "write outside scope")
        await display.begin_exchange()
        display.handle_stream_event({"kind": "turn_start", "turn_index": 0})
        await pilot.pause()
        display.handle_stream_event(
            {"kind": "tool_call", "id": "c1", "name": "write", "arguments": {"path": "/etc/x"}}
        )
        await pilot.pause()
        display.handle_stream_event(
            {
                "kind": "tool_result",
                "id": "c1",
                "name": "write",
                "result": "denied by policy",
                "is_error": True,
                "blocked": True,
                "blocked_by": "/home/u/.tau/extensions/30_permission_gate.py",
            }
        )
        await pilot.pause()

        boxes = list(display.query(ToolBox))
        assert len(boxes) == 1
        box = boxes[0]
        assert box.has_result is True
        # Distinct from a plain error: the blocked class + a ⛔ title mark.
        assert box.has_class("box-blocked")
        assert not box.has_class("box-error")
        assert box.title.startswith("⛔")
        # The body reads "blocked by <ext-stem>: <reason>".
        body = box._result_md._markdown
        assert "blocked by 30_permission_gate: denied by policy" in body
